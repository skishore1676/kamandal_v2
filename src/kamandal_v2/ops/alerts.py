"""Kamandal-owned operator alerts through Lathi Bus."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from typing import Any, Literal


AlertMode = Literal["off", "spool", "live"]

_URL_SECRET_RE = re.compile(r"(code|session|client_id)=([^&\s]+)")
_TOKEN_FIELD_RE = re.compile(r'("?access_token"?|"?refresh_token"?|"?id_token"?)([=:]\s*"?)?[^",\s}]+')
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._-]+")
_DANGER_LEVELS = {"error", "critical", "fatal", "failure", "failed"}
_WARNING_LEVELS = {"warning", "warn"}
DEFAULT_ALERT_BODY_MAX_CHARS = 3200


@dataclass(slots=True)
class AlertResult:
    attempted: bool = False
    ok: bool = False
    mode: AlertMode = "off"
    command: list[str] | None = None
    cwd: str | None = None
    return_code: int | None = None
    live_send_requested: bool | None = None
    network_call_performed: bool | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def send_lathi_alert(
    *,
    title: str,
    body: str,
    level: str = "error",
    mode: AlertMode = "live",
    profile: str | None = None,
    command: list[str] | None = None,
    cwd: str | Path | None = None,
    timeout_seconds: float | None = None,
) -> AlertResult:
    """Send a short operator alert through Lathi Bus."""

    if mode == "off":
        return AlertResult(mode="off")

    profile = profile or default_lathi_bus_profile()
    if command is None:
        command, cwd = default_lathi_invocation(cwd)
    cwd_path = Path(cwd).expanduser() if cwd else None
    prepared_body = prepare_alert_body(body, level)
    args = [
        *command,
        "telegram-notify",
        "--profile",
        profile,
        "--title",
        decorate_title(title, level),
        "--body",
        prepared_body,
        "--level",
        level,
    ]
    if mode == "live":
        args.append("--live")

    try:
        env = os.environ.copy()
        populate_secret_fallbacks(env)
        completed = subprocess.run(  # noqa: S603
            args,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd_path) if cwd_path else None,
            env=env,
            timeout=timeout_seconds or float(os.getenv("KAMANDAL_ALERT_TIMEOUT_SECONDS", "30")),
        )
    except Exception as exc:  # noqa: BLE001 - alerts should report their own failure.
        return AlertResult(
            attempted=True,
            ok=False,
            mode=mode,
            command=args,
            cwd=str(cwd_path) if cwd_path else None,
            error=redact(str(exc)),
        )

    receipt = parse_lathi_receipt(completed.stdout)
    live_send_requested = optional_bool(receipt.get("live_send_requested"))
    network_call_performed = optional_bool(receipt.get("network_call_performed"))
    ok = completed.returncode == 0
    if mode == "live":
        ok = ok and network_call_performed is True

    return AlertResult(
        attempted=True,
        ok=ok,
        mode=mode,
        command=args,
        cwd=str(cwd_path) if cwd_path else None,
        return_code=completed.returncode,
        live_send_requested=live_send_requested,
        network_call_performed=network_call_performed,
        stdout_tail=tail(redact(completed.stdout)),
        stderr_tail=tail(redact(completed.stderr)),
    )


def default_lathi_invocation(cwd: str | Path | None = None) -> tuple[list[str], str | Path | None]:
    raw = os.getenv("KAMANDAL_LATHI_BUS_CMD", "").strip()
    if raw:
        return shlex.split(raw), cwd or os.getenv("KAMANDAL_LATHI_BUS_CWD") or None
    configured_cwd = cwd or os.getenv("KAMANDAL_LATHI_BUS_CWD")
    if configured_cwd:
        return ["python3", "-m", "lathi_bus.cli"], configured_cwd
    if shutil.which("lathi-bus"):
        return ["lathi-bus"], None
    for candidate in (Path.home() / "code" / "lathi-bus", Path("/Users/sunny/code/lathi-bus")):
        if (candidate / "lathi_bus" / "cli.py").is_file():
            return ["python3", "-m", "lathi_bus.cli"], candidate
    return ["lathi-bus"], None


def default_lathi_bus_profile() -> str:
    return os.getenv("KAMANDAL_LATHI_BUS_PROFILE") or os.getenv("KAMANDAL_LATHI_PROFILE") or "kamandal-northstar"


def populate_secret_fallbacks(env: dict[str, str]) -> None:
    token_fallback = Path.home() / ".lane-host" / "secrets" / "telegram_gate_token"
    user_fallback = Path.home() / ".lane-host" / "secrets" / "telegram_gate_user_id"
    chat_fallback = Path.home() / ".lane-host" / "secrets" / "telegram_gate_chat_id"
    if "LATHI_BUS_TG_TOKEN_FILE" not in env and token_fallback.is_file():
        env["LATHI_BUS_TG_TOKEN_FILE"] = str(token_fallback)
    if "LATHI_BUS_TG_USER_ID_FILE" not in env and user_fallback.is_file():
        env["LATHI_BUS_TG_USER_ID_FILE"] = str(user_fallback)
    if "LATHI_BUS_TG_CHAT_ID_FILE" not in env and chat_fallback.is_file():
        env["LATHI_BUS_TG_CHAT_ID_FILE"] = str(chat_fallback)


def parse_lathi_receipt(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def decorate_title(title: str, level: str) -> str:
    normalized = level.lower().strip()
    if normalized in _DANGER_LEVELS:
        return f"KAMANDAL FAILURE: {title}"
    if normalized in _WARNING_LEVELS:
        return f"KAMANDAL WARNING: {title}"
    return title


def decorate_body(body: str, level: str) -> str:
    normalized = level.lower().strip()
    if normalized in _DANGER_LEVELS:
        return "\n".join(
            [
                "ACTION REQUIRED",
                "KAMANDAL FAILURE",
                "",
                body,
                "",
                "Trading may be blocked or fail closed until this is fixed.",
            ]
        )
    if normalized in _WARNING_LEVELS:
        return "\n".join(["KAMANDAL WARNING", "", body])
    return body


def prepare_alert_body(body: str, level: str, *, max_chars: int | None = None) -> str:
    limit = max_chars
    if limit is None:
        limit = int(os.getenv("KAMANDAL_ALERT_BODY_MAX_CHARS", str(DEFAULT_ALERT_BODY_MAX_CHARS)))
    clipped_body = clip_text(redact(body), max_chars=max(limit - 180, 400))
    return redact(decorate_body(clipped_body, level))


def clip_text(text: str, *, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = f"\n... [truncated {len(text) - max_chars} chars; see Kamandal launchd logs for full output]"
    keep = max(max_chars - len(marker), 0)
    return text[:keep].rstrip() + marker


def redact(text: str) -> str:
    text = _URL_SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    return _TOKEN_FIELD_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)


def tail(text: str, *, max_lines: int = 40, max_chars: int | None = None) -> str:
    result = "\n".join(text.splitlines()[-max_lines:])
    return clip_text(result, max_chars=max_chars) if max_chars else result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("notify", choices=["notify"])
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument("--level", default="info")
    parser.add_argument("--mode", choices=["off", "spool", "live"], default=os.getenv("KAMANDAL_LAUNCHD_ALERT_MODE", "live"))
    parser.add_argument("--profile", default=default_lathi_bus_profile())
    args = parser.parse_args(argv)

    result = send_lathi_alert(
        title=args.title,
        body=args.body,
        level=args.level,
        mode=args.mode,
        profile=args.profile,
    )
    print(json.dumps(result.to_dict(), sort_keys=True))
    return 0 if result.ok or args.mode == "off" else 2


if __name__ == "__main__":
    raise SystemExit(main())

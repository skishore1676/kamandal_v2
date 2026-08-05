"""Provider-neutral JSON LLM client.

Default implementation routes through Agent Broker (the repo-wide brain — one
failover chain, one credential hub, one model hierarchy). The legacy Codex-CLI
client is kept as a graceful fallback (agent-broker not installed) and an
explicit `llm.provider: codex_cli` escape hatch.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Protocol

from kamandal_v2.paths import PROJECT_ROOT, resolve_path

LOGGER = logging.getLogger(__name__)


class JsonLlmClient(Protocol):
    def chat_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        ...


class BrokerJsonClient:
    """Hire a JSON turn through Agent Broker, bound to a kamandal actor/brain.
    """

    def __init__(
        self,
        *,
        actor: str = "agent",
        timeout_seconds: int = 300,
        fallback: JsonLlmClient | None = None,
    ) -> None:
        self.actor = actor
        self.timeout_seconds = timeout_seconds
        self.policy_path = resolve_path(PROJECT_ROOT) / ".agent-broker.yaml"
        self._fallback = fallback

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        try:
            return self._chat_json_broker(system_prompt, user_prompt)
        except Exception:
            raise

    def _chat_json_broker(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        from agent_broker import (  # noqa: PLC0415 - optional dependency
            AgentBroker,
            AgentContext,
            AgentSpec,
            AgentTask,
            load_policy,
        )

        broker = AgentBroker(load_policy(self.policy_path))
        spec = AgentSpec(lane_id="kamandal", actor=self.actor, role=self.actor)
        task = AgentTask(
            task_id=f"kamandal-{self.actor}-{uuid.uuid4().hex[:8]}",
            objective=f"{self.actor}: produce the requested JSON.",
            context=AgentContext(system=system_prompt),
            raw_prompt=user_prompt,  # kamandal owns its prompt -> verbatim
            allowed_outcomes=(),
            timeout_seconds=self.timeout_seconds,
        )
        receipt = broker.run(spec, task).receipt
        text = (receipt.output_text or "").strip()
        if receipt.status != "succeeded" or not text:
            raise RuntimeError(
                f"agent-broker failed ({receipt.failure_summary or 'no output'}); "
                f"chain={list(receipt.provider_chain)}"
            )
        return _extract_json_object(text)


class CodexCliJsonClient:
    def __init__(
        self,
        *,
        binary: str | None = None,
        model: str | None = None,
        workdir: str | Path | None = None,
        timeout_seconds: int = 300,
    ) -> None:
        self.binary = _discover_codex(binary)
        self.model = model or ""
        self.workdir = resolve_path(workdir or PROJECT_ROOT)
        self.timeout_seconds = timeout_seconds

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        prompt = "\n\n".join(
            [
                "<system>",
                system_prompt.strip(),
                "</system>",
                "<user>",
                user_prompt.strip(),
                "</user>",
            ]
        )
        args = [
            self.binary,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "--json",
            "--ephemeral",
            "-C",
            str(self.workdir),
        ]
        if self.model:
            args.extend(["--model", self.model])
        args.append("-")
        result = subprocess.run(
            args,
            input=prompt,
            capture_output=True,
            text=True,
            check=False,
            timeout=self.timeout_seconds,
            env=_subprocess_env(),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"Codex CLI failed with exit code {result.returncode}: {detail}")
        message = _extract_codex_message(result.stdout)
        return _extract_json_object(message)


def _agent_broker_available() -> bool:
    try:
        import agent_broker  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def build_llm_client(config: dict[str, Any], *, actor: str = "agent") -> JsonLlmClient:
    """Build the JSON LLM client for an actor.

    Default provider is ``agent_broker`` (the repo-wide brain); it falls back to
    the legacy ``codex_cli`` client if agent-broker is not installed. Pin
    ``llm.provider: codex_cli`` in config to force the legacy path.
    """
    llm_config = config.get("llm") or {}
    provider = str(llm_config.get("provider") or "agent_broker")
    timeout = int(llm_config.get("codex_timeout_seconds") or 300)

    def _legacy() -> JsonLlmClient:
        return CodexCliJsonClient(
            binary=str(llm_config.get("codex_binary") or "") or None,
            model=str(llm_config.get("model") or "") or None,
            workdir=str(llm_config.get("codex_workdir") or "") or PROJECT_ROOT,
            timeout_seconds=timeout,
        )

    if provider == "agent_broker" and _agent_broker_available():
        return BrokerJsonClient(actor=actor, timeout_seconds=timeout)
    # Explicit legacy path (llm.provider: codex_cli) or broker unavailable.
    return _legacy()


def _discover_codex(configured: str | None) -> str:
    candidates = []
    if configured:
        candidates.append(str(Path(configured).expanduser()))
    which = shutil.which("codex")
    if which:
        candidates.append(which)
    candidates.append("/Applications/Codex.app/Contents/Resources/codex")
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError("Codex CLI binary not found. Set llm.codex_binary in config/control.yaml.")


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    extra_path = env.get("KAMANDAL_EXTRA_PATH") or ":".join(
        [
            "/usr/local/bin",
            "/usr/local/opt/node@22/bin",
            "/usr/local/Cellar/node@22/22.22.0_1/bin",
            "/opt/homebrew/bin",
            str(Path.home() / ".nvm/versions/node/v22.22.0/bin"),
            str(Path.home() / ".nvm/versions/node/v20.20.0/bin"),
        ]
    )
    env["PATH"] = f"{extra_path}:{env.get('PATH', '')}"
    return env


def _extract_codex_message(stdout: str) -> str:
    message = ""
    for raw_line in stdout.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            message = str(item.get("text") or message)
    if not message:
        raise RuntimeError("Codex CLI did not return a final agent message")
    return message


def _clean_json_string(s: str) -> str:
    """Sanitize raw LLM JSON output to fix unescaped newlines/control chars inside string values."""
    import re
    in_string = False
    escaped = False
    result = []
    for char in s:
        if char == '"' and not escaped:
            in_string = not in_string
            result.append(char)
        elif in_string and char == "\n":
            result.append("\\n")
        elif in_string and char == "\r":
            result.append("\\r")
        elif in_string and char == "\t":
            result.append("\\t")
        else:
            result.append(char)
        escaped = (char == "\\" and not escaped)
    cleaned = "".join(result)
    cleaned = re.sub(r",\s*([\}\]])", r"\1", cleaned)
    return cleaned


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        try:
            payload = json.loads(_clean_json_string(stripped))
        except json.JSONDecodeError:
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start < 0 or end <= start:
                raise
            substring = stripped[start : end + 1]
            try:
                payload = json.loads(substring)
            except json.JSONDecodeError:
                payload = json.loads(_clean_json_string(substring))
    if not isinstance(payload, dict):
        raise ValueError("LLM response must be a JSON object")
    return payload

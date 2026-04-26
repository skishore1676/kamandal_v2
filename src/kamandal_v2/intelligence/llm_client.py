"""Provider-neutral JSON LLM client with a Codex CLI implementation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Protocol

from kamandal_v2.paths import PROJECT_ROOT, resolve_path


class JsonLlmClient(Protocol):
    def chat_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        ...


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


def build_llm_client(config: dict[str, Any]) -> JsonLlmClient:
    llm_config = config.get("llm") or {}
    provider = str(llm_config.get("provider") or "codex_cli")
    if provider != "codex_cli":
        raise RuntimeError(f"Unsupported LLM provider for this build: {provider}")
    return CodexCliJsonClient(
        binary=str(llm_config.get("codex_binary") or "") or None,
        model=str(llm_config.get("model") or "") or None,
        workdir=str(llm_config.get("codex_workdir") or "") or PROJECT_ROOT,
        timeout_seconds=int(llm_config.get("codex_timeout_seconds") or 300),
    )


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
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("LLM response must be a JSON object")
    return payload

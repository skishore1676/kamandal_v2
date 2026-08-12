"""Durable stage receipts for kill-safe scheduled-job diagnosis."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Iterator


SCHEMA = "kamandal.stage_receipt.v1"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class StageReceipt:
    def __init__(self, path: Path | None, run_id: str) -> None:
        self.path = path
        self.run_id = run_id

    @classmethod
    def from_env(cls) -> "StageReceipt":
        raw_path = os.getenv("KAMANDAL_STAGE_RECEIPT_PATH", "").strip()
        run_id = os.getenv("KAMANDAL_STAGE_RUN_ID", "").strip()
        return cls(Path(raw_path).expanduser() if raw_path and run_id else None, run_id)

    def update(self, stage: str, status: str, *, error: str = "") -> None:
        if self.path is None:
            return
        now = _now()
        payload = self._load()
        if payload.get("run_id") != self.run_id:
            payload = {
                "schema": SCHEMA,
                "run_id": self.run_id,
                "job": "live-reconciliation",
                "started_at": now,
                "status": "running",
                "stages": [],
            }
        stages = list(payload.get("stages") or [])
        current = next((item for item in stages if item.get("name") == stage), None)
        if current is None:
            current = {"name": stage, "started_at": now}
            stages.append(current)
        current["status"] = status
        current["updated_at"] = now
        if error:
            current["error"] = error[:1000]
        if status == "completed":
            current["completed_at"] = now
        payload["stages"] = stages
        payload["current_stage"] = stage
        payload["updated_at"] = now
        if status == "failed":
            payload["status"] = "failed"
            payload["error"] = error[:1000]
        elif stage == "completed" and status == "completed":
            payload["status"] = "completed"
            payload["completed_at"] = now
        self._write(payload)

    def _load(self) -> dict:
        if self.path is None:
            return {}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    def _write(self, payload: dict) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)


@contextmanager
def reconciliation_stage(name: str) -> Iterator[None]:
    receipt = StageReceipt.from_env()
    receipt.update(name, "running")
    try:
        yield
    except BaseException as exc:
        receipt.update(name, "failed", error=f"{type(exc).__name__}: {exc}")
        raise
    else:
        receipt.update(name, "completed")

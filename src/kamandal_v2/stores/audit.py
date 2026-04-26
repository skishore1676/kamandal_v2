"""JSON audit writing helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kamandal_v2.domain.models import utc_now
from kamandal_v2.paths import resolve_path


class AuditWriter:
    def __init__(self, audit_dir: str | Path = "data/audit") -> None:
        self.audit_dir = resolve_path(audit_dir)
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.audit_dir / "events.jsonl"

    def write_json(self, name: str, payload: Any) -> Path:
        path = self.audit_dir / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def event(self, event_type: str, payload: dict[str, Any], *, level: str = "info") -> None:
        record = {
            "ts": utc_now(),
            "level": level,
            "event_type": event_type,
            "payload": payload,
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


"""Research-only import boundary for Market Cartographer seeded chart evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from kamandal_v2.paths import resolve_path

SOURCE_SCHEMA = "market_cartographer.seed_evaluation.v1"
WATCH_SCHEMA = "kamandal.chart_seed_watch.v1"
RECEIPT_SCHEMA = "kamandal.chart_seed_import_receipt.v1"
_TIMESTAMP = TypeAdapter(datetime)
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class ChartSeedImportResult:
    import_id: str
    watch_path: Path
    review_path: Path
    receipt_path: Path
    created: bool
    watch_count: int
    source_id: str
    chart_run_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RECEIPT_SCHEMA,
            "status": "succeeded",
            "import_id": self.import_id,
            "source_id": self.source_id,
            "chart_run_id": self.chart_run_id,
            "created": self.created,
            "watch_count": self.watch_count,
            "watch_path": str(self.watch_path),
            "review_path": str(self.review_path),
            "receipt_path": str(self.receipt_path),
            "planner_eligible": False,
            "effects": _effects(),
        }


def import_chart_seed_evaluation(
    input_path: str | Path,
    *,
    output_dir: str | Path = "data/research/chart_seeds",
) -> ChartSeedImportResult:
    source_path = resolve_path(input_path)
    source_text = source_path.read_text(encoding="utf-8")
    try:
        payload = json.loads(source_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"chart seed evaluation must be valid JSON: {exc.msg}") from exc
    validated = validate_chart_seed_evaluation(payload)
    source_id = str(validated["source"]["source_id"])
    chart_run_id = str(validated["run_id"])
    import_id = hashlib.sha256(f"{source_id}|{chart_run_id}".encode()).hexdigest()[:16]
    run_dir = resolve_path(output_dir) / _safe(source_id) / _safe(chart_run_id)
    watch_path = run_dir / "watch.json"
    review_path = run_dir / "review.md"
    receipt_path = run_dir / "receipt.json"

    watch = _watch_payload(validated, import_id=import_id)
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "status": "succeeded",
        "import_id": import_id,
        "source_id": source_id,
        "chart_run_id": chart_run_id,
        "source_sha256": hashlib.sha256(source_text.encode()).hexdigest(),
        "watch_path": str(watch_path),
        "review_path": str(review_path),
        "receipt_path": str(receipt_path),
        "planner_eligible": False,
        "effects": _effects(),
    }
    serialized_watch = json.dumps(watch, indent=2, sort_keys=True) + "\n"
    rendered_review = _render_review(watch)
    serialized_receipt = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    artifacts = (
        (watch_path, serialized_watch),
        (review_path, rendered_review),
        (receipt_path, serialized_receipt),
    )
    for path, content in artifacts:
        _assert_idempotent(path, content)
    run_dir.mkdir(parents=True, exist_ok=True)
    created = False
    for path, content in artifacts:
        if not path.exists():
            path.write_text(content, encoding="utf-8")
            created = True
    return ChartSeedImportResult(
        import_id=import_id,
        watch_path=watch_path,
        review_path=review_path,
        receipt_path=receipt_path,
        created=created,
        watch_count=len(watch["watches"]),
        source_id=source_id,
        chart_run_id=chart_run_id,
    )


def validate_chart_seed_evaluation(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("chart seed evaluation root must be an object")
    if payload.get("schema") != SOURCE_SCHEMA:
        raise ValueError(f"chart seed evaluation schema must be {SOURCE_SCHEMA}")
    run_id = _text(payload.get("run_id"), "run_id")
    if not re.fullmatch(r"[A-Za-z0-9._-]{8,128}", run_id):
        raise ValueError("run_id is invalid")
    _timestamp(payload.get("as_of"), "as_of")
    source = payload.get("source")
    if not isinstance(source, dict):
        raise ValueError("source must be an object")
    source_id = _text(source.get("source_id"), "source.source_id")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("data must be an object")
    _text(data.get("mode"), "data.mode")
    _text(data.get("freshness"), "data.freshness")
    effects = payload.get("effects")
    if not isinstance(effects, dict):
        raise ValueError("effects must be an object")
    protected = {
        "broker",
        "orders",
        "auth",
        "schedule",
        "external_send",
        "planner_admission",
    }
    if set(effects) != protected or any(effects.get(key) is not False for key in protected):
        raise ValueError("all Market Cartographer protected effects must be explicitly false")
    evaluations = payload.get("evaluations")
    if not isinstance(evaluations, list) or not evaluations:
        raise ValueError("evaluations must be a non-empty array")
    seen: set[str] = set()
    for index, evaluation in enumerate(evaluations):
        if not isinstance(evaluation, dict):
            raise ValueError(f"evaluations[{index}] must be an object")
        symbol = _text(evaluation.get("symbol"), f"evaluations[{index}].symbol").upper()
        if not _SYMBOL.fullmatch(symbol):
            raise ValueError(f"evaluations[{index}].symbol is invalid")
        if symbol in seen:
            raise ValueError(f"duplicate evaluation symbol: {symbol}")
        seen.add(symbol)
        if evaluation.get("planner_eligible") is not False:
            raise ValueError(f"evaluations[{index}] must set planner_eligible=false")
        context = evaluation.get("source_context")
        if not isinstance(context, dict) or context.get("source_id") != source_id:
            raise ValueError(f"evaluations[{index}] source identity does not match packet")
        _text(evaluation.get("evaluation_status"), f"evaluations[{index}].evaluation_status")
        _validate_price_object(evaluation.get("primary_boundary"), f"evaluations[{index}].primary_boundary")
        _validate_price_object(evaluation.get("confirmation_trigger"), f"evaluations[{index}].confirmation_trigger")
        _validate_price_object(evaluation.get("failure_condition"), f"evaluations[{index}].failure_condition")
    return payload


def _watch_payload(payload: dict[str, Any], *, import_id: str) -> dict[str, Any]:
    watches = []
    for evaluation in payload["evaluations"]:
        watches.append(
            {
                "symbol": str(evaluation["symbol"]).upper(),
                "status": "research_only",
                "review_status": "needs_review",
                "planner_eligible": False,
                "source_id": payload["source"]["source_id"],
                "chart_run_id": payload["run_id"],
                "as_of": payload["as_of"],
                "data_mode": payload["data"]["mode"],
                "data_freshness": payload["data"]["freshness"],
                "requested_setup_family": evaluation.get("requested_setup_family"),
                "observed_setup_family": evaluation.get("observed_setup_family"),
                "source_alignment": evaluation.get("source_alignment"),
                "signal_state": evaluation.get("signal_state"),
                "evaluation_status": evaluation.get("evaluation_status"),
                "primary_boundary": evaluation.get("primary_boundary"),
                "confirmation_trigger": evaluation.get("confirmation_trigger"),
                "failure_condition": evaluation.get("failure_condition"),
                "reasons": list(evaluation.get("reasons") or []),
                "counter_evidence": list(evaluation.get("counter_evidence") or []),
                "evidence_refs": list(evaluation.get("evidence_refs") or []),
                "source_context": dict(evaluation["source_context"]),
            }
        )
    return {
        "schema": WATCH_SCHEMA,
        "import_id": import_id,
        "status": "research_only",
        "planner_eligible": False,
        "source": dict(payload["source"]),
        "chart_run_id": payload["run_id"],
        "as_of": payload["as_of"],
        "algorithm_version": payload.get("algorithm_version"),
        "data": dict(payload["data"]),
        "watches": watches,
        "effects": _effects(),
    }


def _render_review(watch: dict[str, Any]) -> str:
    lines = [
        "# Seeded Chart Review",
        "",
        f"- Source: `{watch['source']['source_id']}`",
        f"- Chart run: `{watch['chart_run_id']}`",
        f"- Observation: `{watch['as_of']}`",
        f"- Data mode: `{watch['data']['mode']}`",
        "- Status: `research_only`",
        "- Planner eligible: `false`",
        "",
        "| Symbol | Requested | Observed | Alignment | State | Trigger | Failure |",
        "| --- | --- | --- | --- | --- | ---: | ---: |",
    ]
    for item in watch["watches"]:
        trigger = item.get("confirmation_trigger") or {}
        failure = item.get("failure_condition") or {}
        lines.append(
            "| {symbol} | {requested} | {observed} | {alignment} | {state} | {trigger} | {failure} |".format(
                symbol=item["symbol"],
                requested=item.get("requested_setup_family") or "-",
                observed=item.get("observed_setup_family") or "-",
                alignment=item.get("source_alignment") or "-",
                state=item.get("signal_state") or "-",
                trigger=_price(trigger.get("price")),
                failure=_price(failure.get("price")),
            )
        )
    lines.extend(
        [
            "",
            "This packet is chart evidence for review. It is not an idea YAML, planner input,",
            "options recommendation, approval, or execution instruction.",
            "",
        ]
    )
    return "\n".join(lines)


def _assert_idempotent(path: Path, content: str) -> None:
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise RuntimeError(f"idempotent artifact collision: {path}")


def _validate_price_object(value: object, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object or null")
    for key in ("price", "lower", "upper"):
        if key in value and not isinstance(value[key], (int, float)):
            raise ValueError(f"{label}.{key} must be numeric")


def _timestamp(value: object, label: str) -> None:
    text = _text(value, label)
    try:
        parsed = _TIMESTAMP.validate_python(text)
    except ValidationError as exc:
        raise ValueError(f"{label} must be a valid timezone-aware timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")


def _text(value: object, label: str) -> str:
    if not isinstance(value, (str, int)) or not str(value).strip():
        raise ValueError(f"{label} is required")
    return " ".join(str(value).split())


def _safe(value: str) -> str:
    return _SAFE_ID.sub("-", value).strip("-") or "unknown"


def _price(value: object) -> str:
    return f"{float(value):.2f}" if isinstance(value, (int, float)) else "-"


def _effects() -> dict[str, bool]:
    return {
        "broker": False,
        "orders": False,
        "sheet_write": False,
        "planner_admission": False,
        "shadow_admission": False,
        "external_send": False,
    }

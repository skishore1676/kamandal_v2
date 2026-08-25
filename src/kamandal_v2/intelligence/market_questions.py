"""Kamandal side of the source-neutral Market Cartographer question exchange."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from kamandal_v2.paths import resolve_path

QUESTION_REQUEST_SCHEMA = "market_cartographer.question_request.v1"
QUESTION_RESPONSE_SCHEMA = "market_cartographer.question_response.v1"
QUESTION_KIND = "directional_setup"
RANGE_QUESTION_KIND = "range_regime"
QUESTION_KINDS = {QUESTION_KIND, RANGE_QUESTION_KIND}
_TIMESTAMP = TypeAdapter(datetime)

CommandRunner = Callable[[list[str], Path], str]


@dataclass(frozen=True, slots=True)
class MarketQuestionResult:
    status: str
    question_count: int
    request_path: Path | None
    response_path: Path | None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "question_count": self.question_count,
            "request_path": str(self.request_path) if self.request_path else None,
            "response_path": str(self.response_path) if self.response_path else None,
            "error": self.error,
        }


def build_market_question_request(
    packet: dict[str, Any], profile: dict[str, Any]
) -> dict[str, Any] | None:
    questions: list[dict[str, Any]] = []
    for record in packet.get("records") or []:
        classification = record.get("classification") or {}
        family = (profile.get("families") or {}).get(
            str(classification.get("type") or "")
        )
        if not isinstance(family, dict) or family.get("mode") != "chart_watch":
            continue
        source = record.get("source") or {}
        for symbol_item in (record.get("literal") or {}).get("symbols") or []:
            symbol = str((symbol_item or {}).get("symbol") or "").strip().upper()
            if not symbol:
                continue
            source_id = str(
                source.get("source_id") or record.get("signal_id") or ""
            ).strip()
            identity = f"{profile['profile_id']}|{source_id}|{symbol}|{QUESTION_KIND}"
            questions.append(
                {
                    "question_id": "mq-"
                    + hashlib.sha256(identity.encode()).hexdigest()[:20],
                    "question": QUESTION_KIND,
                    "symbol": symbol,
                    "direction_hint": str(family.get("direction") or "neutral").lower(),
                    "source_claim": str(
                        (record.get("literal") or {}).get("text") or ""
                    )[:5000],
                    "source": {
                        "kind": str(source.get("kind") or "public_correspondent_post"),
                        "source_id": source_id,
                        "source_url": source.get("source_url"),
                        "published_at": source.get("published_at"),
                        "author_handle": source.get("author_handle"),
                        "observation_sources": list(
                            source.get("observation_sources") or []
                        ),
                    },
                }
            )
    if not questions:
        return None
    return {
        "schema": QUESTION_REQUEST_SCHEMA,
        "as_of": packet["generated_at"],
        "questions": questions,
    }


def build_range_regime_request(
    symbols: list[str],
    *,
    as_of: str,
    playbook_id: str,
) -> dict[str, Any] | None:
    questions = []
    for symbol in sorted({str(item).strip().upper() for item in symbols if str(item).strip()}):
        source_id = f"{playbook_id}:{symbol}:{as_of}"
        identity = f"{source_id}|{RANGE_QUESTION_KIND}"
        questions.append(
            {
                "question_id": "mq-" + hashlib.sha256(identity.encode()).hexdigest()[:20],
                "question": RANGE_QUESTION_KIND,
                "symbol": symbol,
                "direction_hint": "neutral",
                "source_claim": "Evaluate whether the current chart remains inside a deterministic range.",
                "source": {
                    "kind": "kamandal_market_scan",
                    "source_id": source_id,
                    "source_url": None,
                    "published_at": None,
                    "author_handle": None,
                    "observation_sources": ["kamandal_unified_planner"],
                },
            }
        )
    if not questions:
        return None
    return {"schema": QUESTION_REQUEST_SCHEMA, "as_of": as_of, "questions": questions}


def run_market_question_exchange(
    packet: dict[str, Any],
    profile: dict[str, Any],
    settings: dict[str, Any],
    *,
    command_runner: CommandRunner | None = None,
) -> MarketQuestionResult:
    request = build_market_question_request(packet, profile)
    if request is None:
        return MarketQuestionResult("not_needed", 0, None, None)
    maximum = max(1, int(settings.get("max_symbols_per_request") or 8))
    request["questions"] = request["questions"][:maximum]
    if settings.get("enabled") is not True:
        return MarketQuestionResult("disabled", len(request["questions"]), None, None)

    profile_id = str(profile["profile_id"])
    request_root = resolve_path(
        settings.get("request_dir") or "data/research/market_questions/requests"
    )
    output_root = resolve_path(
        settings.get("evaluation_dir")
        or settings.get("output_dir")
        or "data/research/market_questions"
    )
    request_dir = request_root / profile_id
    output_dir = output_root / profile_id
    request_id = hashlib.sha256(_stable_json(request).encode()).hexdigest()[:16]
    request_path = request_dir / f"question-request-{request_id}.json"
    _write_idempotent(
        request_path, json.dumps(request, indent=2, sort_keys=True) + "\n"
    )

    cartographer_bin = resolve_path(
        settings.get("cartographer_bin")
        or "../market-cartographer/.venv/bin/market-cartographer"
    )
    provider = str(settings.get("provider") or "mala")
    args = [
        str(cartographer_bin),
        "answer-questions",
        "--input",
        str(request_path),
        "--provider",
        provider,
        "--output",
        str(output_dir),
    ]
    if provider == "mala":
        data_root = resolve_path(settings.get("data_root") or "../mala_v2/data")
        args.extend(["--data-root", str(data_root)])
    runner = command_runner or _run_command
    try:
        if not cartographer_bin.is_file():
            raise FileNotFoundError(
                f"Market Cartographer CLI not found: {cartographer_bin}"
            )
        runner(args, cartographer_bin.parent.parent.parent)
        response_path = output_dir / "question-response.json"
        response = validate_market_question_response(
            json.loads(response_path.read_text(encoding="utf-8"))
        )
        _assert_response_matches_request(request, response)
        return MarketQuestionResult(
            "succeeded", len(request["questions"]), request_path, response_path
        )
    except Exception as exc:  # noqa: BLE001 - a failed enrichment parks only dependent ideas
        return MarketQuestionResult(
            "failed",
            len(request["questions"]),
            request_path,
            None,
            f"{type(exc).__name__}: {exc}",
        )


def run_range_regime_exchange(
    symbols: list[str],
    settings: dict[str, Any],
    *,
    as_of: str,
    playbook_id: str,
    command_runner: CommandRunner | None = None,
) -> MarketQuestionResult:
    request = build_range_regime_request(symbols, as_of=as_of, playbook_id=playbook_id)
    if request is None:
        return MarketQuestionResult("not_needed", 0, None, None)
    if settings.get("enabled") is not True:
        return MarketQuestionResult("disabled", len(request["questions"]), None, None)

    request_root = resolve_path(settings.get("request_dir") or "data/research/market_questions/requests")
    output_root = resolve_path(settings.get("evaluation_dir") or "data/research/market_questions")
    request_id = hashlib.sha256(_stable_json(request).encode()).hexdigest()[:16]
    request_dir = request_root / RANGE_QUESTION_KIND
    output_dir = output_root / RANGE_QUESTION_KIND / request_id
    request_path = request_dir / f"question-request-{request_id}.json"
    _write_idempotent(request_path, json.dumps(request, indent=2, sort_keys=True) + "\n")

    cartographer_bin = resolve_path(settings.get("cartographer_bin") or "../market-cartographer/.venv/bin/market-cartographer")
    provider = str(settings.get("provider") or "mala")
    args = [
        str(cartographer_bin),
        "answer-questions",
        "--input",
        str(request_path),
        "--provider",
        provider,
        "--output",
        str(output_dir),
    ]
    if provider == "mala":
        args.extend(["--data-root", str(resolve_path(settings.get("data_root") or "../mala_v2/data"))])
    runner = command_runner or _run_command
    try:
        if not cartographer_bin.is_file():
            raise FileNotFoundError(f"Market Cartographer CLI not found: {cartographer_bin}")
        runner(args, cartographer_bin.parent.parent.parent)
        response_path = output_dir / "question-response.json"
        response = validate_market_question_response(json.loads(response_path.read_text(encoding="utf-8")))
        _assert_response_matches_request(request, response)
        return MarketQuestionResult("succeeded", len(request["questions"]), request_path, response_path)
    except Exception as exc:  # noqa: BLE001 - a failed range gate fails dependent candidates closed.
        return MarketQuestionResult(
            "failed",
            len(request["questions"]),
            request_path,
            None,
            f"{type(exc).__name__}: {exc}",
        )


def validate_market_question_response(payload: object) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != QUESTION_RESPONSE_SCHEMA
    ):
        raise ValueError(
            f"market question response schema must be {QUESTION_RESPONSE_SCHEMA}"
        )
    _text(payload.get("run_id"), "run_id")
    _timestamp(payload.get("as_of"), "as_of")
    effects = payload.get("effects")
    protected = {
        "broker",
        "orders",
        "auth",
        "schedule",
        "external_send",
        "planner_admission",
    }
    if (
        not isinstance(effects, dict)
        or set(effects) != protected
        or any(effects.values())
    ):
        raise ValueError(
            "market question response must declare every protected effect false"
        )
    answers = payload.get("answers")
    if not isinstance(answers, list) or not answers:
        raise ValueError("market question response answers must be a non-empty array")
    seen: set[str] = set()
    for index, answer in enumerate(answers):
        if not isinstance(answer, dict):
            raise TypeError(f"answers[{index}] must be an object")
        question_id = _text(answer.get("question_id"), f"answers[{index}].question_id")
        if question_id in seen:
            raise ValueError(f"duplicate market question answer: {question_id}")
        seen.add(question_id)
        question_kind = answer.get("question")
        if question_kind not in QUESTION_KINDS:
            raise ValueError(f"answers[{index}].question is invalid")
        if answer.get("planner_eligible") is not False:
            raise ValueError(f"answers[{index}] cannot grant planner eligibility")
        if answer.get("direction") not in {"bullish", "bearish", "neutral"}:
            raise ValueError(f"answers[{index}].direction is invalid")
        source = answer.get("source_context")
        if not isinstance(source, dict):
            raise TypeError(f"answers[{index}].source_context must be an object")
        _text(source.get("source_id"), f"answers[{index}].source_context.source_id")
        if question_kind == RANGE_QUESTION_KIND:
            state = answer.get("range_state")
            if answer.get("answer_status") == "evaluated" and state not in {
                "confirmed_range",
                "broken",
                "no_range",
            }:
                raise ValueError(f"answers[{index}].range_state is invalid")
    return payload


def _run_command(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    # Market Cartographer uses exit 2 for a valid, persisted response whose
    # answer is "insufficient evidence".  That is negative chart evidence, not
    # a transport failure; validation below still rejects a missing or malformed
    # response artifact.  All other non-zero exits remain hard failures.
    if completed.returncode not in {0, 2}:
        raise subprocess.CalledProcessError(
            completed.returncode,
            args,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed.stdout


def _assert_response_matches_request(
    request: dict[str, Any], response: dict[str, Any]
) -> None:
    expected = {
        item["question_id"]: (item["symbol"], item["source"]["source_id"])
        for item in request["questions"]
    }
    actual = {
        item["question_id"]: (item["symbol"], item["source_context"]["source_id"])
        for item in response["answers"]
    }
    if actual != expected:
        raise ValueError("Market Cartographer response does not answer the exact request")
    if _timestamp(response["as_of"], "response.as_of") != _timestamp(
        request["as_of"], "request.as_of"
    ):
        raise ValueError("Market Cartographer response observation time does not match request")


def _write_idempotent(path: Path, content: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8") != content:
        raise ValueError(f"market question request identity collision: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _text(value: object, label: str) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        raise ValueError(f"{label} is required")
    return text


def _timestamp(value: object, label: str) -> str:
    text = _text(value, label)
    parsed = _TIMESTAMP.validate_python(text)
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.isoformat()


def _stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


__all__ = [
    "QUESTION_REQUEST_SCHEMA",
    "QUESTION_RESPONSE_SCHEMA",
    "MarketQuestionResult",
    "build_market_question_request",
    "run_market_question_exchange",
    "validate_market_question_response",
]

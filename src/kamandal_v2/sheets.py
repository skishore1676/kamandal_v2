"""Google Sheets bootstrap for the configuration cockpit."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import json
import fcntl
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Sequence, TypeVar

from kamandal_v2.config import google_credentials_path, spreadsheet_id
from kamandal_v2.live.entry_hygiene import market_today


T = TypeVar("T")
TRANSIENT_SHEETS_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass
class BootstrapResult:
    spreadsheet_id: str
    tabs: dict[str, int]


class GoogleSheetClient:
    def __init__(
        self,
        *,
        credentials_path: Path,
        spreadsheet_id_value: str,
        retry_attempts: int = 3,
        retry_base_delay_seconds: float = 1.0,
        retry_max_delay_seconds: float = 4.0,
        connect_timeout_seconds: float = 10.0,
        read_timeout_seconds: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        try:
            import gspread  # type: ignore
            from gspread.exceptions import WorksheetNotFound
            from google.oauth2.service_account import Credentials
        except ImportError as exc:
            raise RuntimeError(
                "Google Sheets dependencies are missing. Install project deps or run with the old kamandal venv."
            ) from exc

        credentials = Credentials.from_service_account_file(
            str(credentials_path),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        self._client = gspread.authorize(credentials)
        self._client.set_timeout(
            (
                max(float(connect_timeout_seconds), 0.1),
                max(float(read_timeout_seconds), 0.1),
            )
        )
        self._worksheet_not_found = WorksheetNotFound
        self._retry_attempts = max(int(retry_attempts), 1)
        self._retry_base_delay_seconds = max(float(retry_base_delay_seconds), 0.0)
        self._retry_max_delay_seconds = max(float(retry_max_delay_seconds), self._retry_base_delay_seconds)
        self._sleep = sleep
        self._spreadsheet = self._retry(
            lambda: self._client.open_by_key(spreadsheet_id_value),
            operation="open spreadsheet",
        )
        self.spreadsheet_id = spreadsheet_id_value

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "GoogleSheetClient":
        retry = ((config.get("google_sheets") or {}).get("retry") or {})
        return cls(
            credentials_path=google_credentials_path(config),
            spreadsheet_id_value=spreadsheet_id(config),
            retry_attempts=int(retry.get("attempts") or 3),
            retry_base_delay_seconds=float(retry.get("base_delay_seconds") or 1.0),
            retry_max_delay_seconds=float(retry.get("max_delay_seconds") or 4.0),
            connect_timeout_seconds=float(retry.get("connect_timeout_seconds") or 10.0),
            read_timeout_seconds=float(retry.get("read_timeout_seconds") or 30.0),
        )

    def replace_tab(
        self,
        title: str,
        *,
        header: Sequence[str],
        rows: Sequence[Sequence[Any]],
    ) -> int:
        worksheet = self._worksheet(title, rows=max(len(rows) + 10, 100), cols=max(len(header), 26))
        self._retry(worksheet.clear, operation=f"clear worksheet {title!r}")
        values = [list(header)]
        for row in rows:
            padded = list(row) + [""] * (len(header) - len(row))
            values.append([_cell(value) for value in padded[: len(header)]])
        self._retry(
            lambda: worksheet.update(
                range_name=f"A1:{_col_letter(len(header))}{len(values)}",
                values=values,
                value_input_option="USER_ENTERED",
            ),
            operation=f"update worksheet {title!r}",
        )
        self._retry(lambda: worksheet.freeze(rows=1), operation=f"freeze worksheet {title!r}")
        return len(rows)

    def replace_plan_values(
        self, title: str, *, header: Sequence[str], rows: Sequence[Sequence[Any]],
    ) -> int:
        """Publish a complete cockpit value matrix without an empty-sheet interval."""
        worksheet = self._worksheet(title, rows=max(len(rows) + 10, 100), cols=max(len(header), 26))
        previous = self._retry(worksheet.get_all_values, operation=f"read worksheet {title!r}") or []
        values = [list(header)] + [
            [_cell(value) for value in (list(row) + [""] * len(header))[:len(header)]]
            for row in rows
        ]
        values.extend([[""] * len(header) for _ in range(max(len(previous) - len(values), 0))])
        self._retry(
            lambda: worksheet.update(
                range_name=f"A1:{_col_letter(len(header))}{len(values)}",
                values=values, value_input_option="RAW",
            ),
            operation=f"publish plan values {title!r}",
        )
        self._retry(lambda: worksheet.freeze(rows=1), operation=f"freeze worksheet {title!r}")
        return len(rows)

    def read_tab(self, title: str) -> list[dict[str, str]]:
        worksheet = self._worksheet(title, rows=100, cols=26)
        values = self._retry(worksheet.get_all_values, operation=f"read worksheet {title!r}") or []
        if not values:
            return []
        header = [str(cell).strip() for cell in values[0]]
        rows: list[dict[str, str]] = []
        for raw in values[1:]:
            if not any(str(cell).strip() for cell in raw):
                continue
            padded = list(raw) + [""] * (len(header) - len(raw))
            rows.append({header[index]: str(padded[index]).strip() for index in range(len(header)) if header[index]})
        return rows

    def read_existing_tab(self, title: str) -> list[dict[str, str]]:
        """Read a policy tab without creating it when missing."""
        worksheet = self._retry(lambda: self._spreadsheet.worksheet(title), operation=f"find worksheet {title!r}")
        values = self._retry(worksheet.get_all_values, operation=f"read worksheet {title!r}") or []
        if not values:
            return []
        header = [str(cell).strip() for cell in values[0]]
        rows: list[dict[str, str]] = []
        for raw in values[1:]:
            if not any(str(cell).strip() for cell in raw):
                continue
            padded = list(raw) + [""] * (len(header) - len(raw))
            rows.append({header[index]: str(padded[index]).strip() for index in range(len(header)) if header[index]})
        return rows

    def read_tab_values(self, title: str) -> list[list[str]]:
        """Read the exact populated value matrix without changing the tab."""
        worksheet = self._worksheet(title, rows=100, cols=26)
        values = self._retry(worksheet.get_all_values, operation=f"read worksheet values {title!r}") or []
        return [list(row) for row in values]

    def tab_dimensions(self, title: str) -> tuple[int, int]:
        worksheet = self._worksheet(title, rows=100, cols=26)
        return int(worksheet.row_count), int(worksheet.col_count)

    def resize_tab(self, title: str, *, rows: int | None = None, cols: int | None = None) -> None:
        worksheet = self._worksheet(title, rows=max(rows or 100, 1), cols=max(cols or 26, 1))
        self._retry(
            lambda: worksheet.resize(rows=rows, cols=cols),
            operation=f"resize worksheet {title!r}",
        )

    def batch_update_tab(self, title: str, updates: Sequence[dict[str, Any]]) -> None:
        """Apply explicitly bounded range updates without clearing the worksheet."""
        if not updates:
            return
        worksheet = self._worksheet(title, rows=100, cols=26)
        payload = [
            {"range": str(item["range"]), "values": [list(row) for row in item["values"]]}
            for item in updates
        ]
        self._retry(
            lambda: worksheet.batch_update(payload, value_input_option="USER_ENTERED"),
            operation=f"batch update worksheet {title!r}",
        )

    def batch_clear_tab(self, title: str, ranges: Sequence[str]) -> None:
        if not ranges:
            return
        worksheet = self._worksheet(title, rows=100, cols=26)
        self._retry(
            lambda: worksheet.batch_clear(list(ranges)),
            operation=f"batch clear worksheet {title!r}",
        )

    def append_tab_rows(self, title: str, *, header: Sequence[str], rows: Sequence[Sequence[Any]]) -> int:
        """Append a bounded range without clearing existing cells or formatting."""
        if not rows:
            return 0
        worksheet = self._worksheet(title, rows=100, cols=max(len(header), 26))
        values = self._retry(worksheet.get_all_values, operation=f"read worksheet {title!r} before append") or []
        if values:
            actual_header = [str(cell).strip() for cell in values[0][: len(header)]]
            if actual_header != list(header):
                raise ValueError(f"worksheet {title!r} header does not match bounded append contract")
        else:
            raise ValueError(f"worksheet {title!r} has no header; bounded append refuses to bootstrap it")
        start = len(values) + 1
        normalized = []
        for row in rows:
            padded = list(row) + [""] * (len(header) - len(row))
            normalized.append([_cell(value) for value in padded[: len(header)]])
        end = start + len(normalized) - 1
        self._retry(
            lambda: worksheet.update(
                range_name=f"A{start}:{_col_letter(len(header))}{end}",
                values=normalized,
                value_input_option="USER_ENTERED",
            ),
            operation=f"append worksheet {title!r}",
        )
        return len(normalized)

    def _worksheet(self, title: str, *, rows: int, cols: int) -> Any:
        try:
            return self._retry(
                lambda: self._spreadsheet.worksheet(title),
                operation=f"find worksheet {title!r}",
            )
        except self._worksheet_not_found:
            return self._retry(
                lambda: self._spreadsheet.add_worksheet(title=title, rows=rows, cols=cols),
                operation=f"create worksheet {title!r}",
            )

    def _retry(self, call: Callable[[], T], *, operation: str) -> T:
        return retry_transient_sheet_call(
            call,
            operation=operation,
            attempts=self._retry_attempts,
            base_delay_seconds=self._retry_base_delay_seconds,
            max_delay_seconds=self._retry_max_delay_seconds,
            sleep=self._sleep,
        )


def retry_transient_sheet_call(
    call: Callable[[], T],
    *,
    operation: str,
    attempts: int = 3,
    base_delay_seconds: float = 1.0,
    max_delay_seconds: float = 4.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Retry only rate limits, server failures, and transient transport errors."""

    total_attempts = max(int(attempts), 1)
    for attempt in range(1, total_attempts + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - the predicate narrows retries.
            if attempt >= total_attempts or not is_transient_sheet_error(exc):
                raise
            delay = min(
                max(float(base_delay_seconds), 0.0) * (2 ** (attempt - 1)),
                max(float(max_delay_seconds), 0.0),
            )
            print(
                f"Google Sheets transient failure during {operation}; "
                f"retrying attempt {attempt + 1}/{total_attempts} in {delay:.1f}s: {_safe_sheet_error(exc)}",
                file=sys.stderr,
            )
            sleep(delay)
    raise AssertionError("unreachable")


def is_transient_sheet_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    raw_status = getattr(response, "status_code", None)
    try:
        status = int(raw_status) if raw_status is not None else None
    except (TypeError, ValueError):
        status = None
    if status in TRANSIENT_SHEETS_STATUS_CODES:
        return True

    message = str(exc)
    status_match = re.search(r"(?:\[|status[=: ]+)(429|500|502|503|504)(?:\]|\b)", message, flags=re.IGNORECASE)
    if status_match:
        return True

    name = type(exc).__name__.lower()
    module = type(exc).__module__.lower()
    return (
        "timeout" in name
        or "connectionerror" in name
        or module.startswith("requests.")
        or module.startswith("urllib3.")
    )


def _safe_sheet_error(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    return message[:240]


def bootstrap_sheet(
    config: dict[str, Any],
    *,
    headers: dict[str, list[str]],
    seed_tables: dict[str, list[list[Any]]],
) -> BootstrapResult:
    client = GoogleSheetClient.from_config(config)
    tab_names = ((config.get("google_sheets") or {}).get("tabs") or {})
    written: dict[str, int] = {}
    for logical_name, header in headers.items():
        title = str(tab_names.get(logical_name) or logical_name)
        written[title] = client.replace_tab(
            title,
            header=header,
            rows=seed_tables.get(logical_name, []),
        )
    return BootstrapResult(spreadsheet_id=client.spreadsheet_id, tabs=written)


def pull_sheet_tables(config: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    client = GoogleSheetClient.from_config(config)
    tab_names = ((config.get("google_sheets") or {}).get("tabs") or {})
    return {
        logical_name: client.read_tab(str(tab_names.get(logical_name) or logical_name))
        for logical_name in ("universe", "playbooks", "daily_plan", "trade_sources")
    }


def pull_portfolio_sleeves(config: dict[str, Any]) -> list[dict[str, str]]:
    client = GoogleSheetClient.from_config(config)
    tab_names = ((config.get("google_sheets") or {}).get("tabs") or {})
    return client.read_existing_tab(str(tab_names.get("portfolio_sleeves") or "portfolio_sleeves"))


def pull_trade_sources(config: dict[str, Any]) -> list[dict[str, str]]:
    client = GoogleSheetClient.from_config(config)
    tab_names = ((config.get("google_sheets") or {}).get("tabs") or {})
    return client.read_existing_tab(str(tab_names.get("trade_sources") or "trade_sources"))


def write_trade_source_activity(
    config: dict[str, Any],
    rows: list[list[Any]],
    header: list[str],
) -> int:
    """Replace the bounded machine projection; never treat it as policy."""

    client = GoogleSheetClient.from_config(config)
    tab_names = ((config.get("google_sheets") or {}).get("tabs") or {})
    title = str(tab_names.get("trade_source_activity") or "trade_source_activity")
    # One RAW values write avoids a transient empty dashboard and keeps source
    # text from becoming executable Sheet formulas. Preserve formatting and
    # columns outside this machine-owned projection.
    worksheet = client._worksheet(title, rows=max(len(rows) + 10, 100), cols=max(len(header), 26))
    values = [list(header), *[list(row) for row in rows]]
    values.extend([[""] * len(header) for _ in range(worksheet.row_count - len(values))])
    client._retry(
        lambda: worksheet.update(range_name=f"A1:{_col_letter(len(header))}{len(values)}",
                                 values=values, value_input_option="RAW"),
        operation="replace trade source activity",
    )
    return len(rows)


def write_translation_review(config: dict[str, Any], rows: list[list[Any]]) -> int:
    """Update translation columns in stable rows; never rewrite operator corrections."""
    from kamandal_v2.schemas import TRADE_SOURCE_ACTIVITY_HEADER, TRADE_SOURCE_REVIEW_HEADER
    header = TRADE_SOURCE_REVIEW_HEADER
    lock_path = Path("data/runlocks/translation_review_publish.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        client = GoogleSheetClient.from_config(config)
        title = str((((config.get("google_sheets") or {}).get("tabs") or {}).get("trade_source_activity")) or "trade_source_activity")
        worksheet = client._worksheet(title, rows=max(len(rows) + 10, 100), cols=len(header))
        previous = client._retry(worksheet.get_all_values, operation="read translation review") or []
        old_header = list(previous[0]) if previous else []
        if old_header not in ([], header, TRADE_SOURCE_ACTIVITY_HEADER):
            raise ValueError("translation review header is unrecognized; refusing to overwrite operator content")
        if old_header != header:
            if any(any(cell for cell in row[len(TRADE_SOURCE_ACTIVITY_HEADER):]) for row in previous):
                raise ValueError("unexpected content outside old activity columns; refusing to remove it")
            # Explicit migration of this machine-owned audit tab to translation review.
            width = max(len(old_header), len(header))
            values = [list(header), *[list(row) for row in rows]]
            values = [row + [""] * (width - len(row)) for row in values]
            values.extend([[""] * width for _ in range(max(len(previous) - len(values), 0))])
            client._retry(lambda: worksheet.update(
                range_name=f"A1:{_col_letter(width)}{len(values)}", values=values,
                value_input_option="RAW"), operation="reset translation review")
            client._retry(lambda: worksheet.resize(cols=len(header)), operation="remove audit-only columns")
            return len(rows)

        # Preserve row positions, including blank gaps: corrections in column G
        # stay attached to their source even if the incoming sort order changes.
        merged = [(list(row[:6]) + [""] * 6)[:6] for row in previous[1:]]
        positions = {(str(row[0]), str(row[1])): index for index, row in enumerate(merged) if row[1]}
        for row in rows:
            key = (str(row[0]), str(row[1]))
            if key in positions:
                merged[positions[key]] = list(row[:6])
            else:
                positions[key] = len(merged)
                merged.append(list(row[:6]))
        if len(merged) + 1 > worksheet.row_count:
            client._retry(lambda: worksheet.resize(rows=len(merged) + 10), operation="extend translation review")
        values = [list(header[:6]), *merged]
        client._retry(lambda: worksheet.update(range_name=f"A1:F{len(values)}", values=values,
                     value_input_option="RAW"), operation="refresh translation review")
        return sum(bool(row[1]) for row in merged)


def write_trade_source_brief(
    config: dict[str, Any],
    summary: list[list[Any]],
    decisions: list[list[Any]],
) -> int:
    """Publish a compact brief, carrying every operator correction by identity."""
    from kamandal_v2.schemas import TRADE_SOURCE_BRIEF_HEADER, TRADE_SOURCE_REVIEW_HEADER

    lock_path = Path("data/runlocks/translation_review_publish.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        client = GoogleSheetClient.from_config(config)
        title = str((((config.get("google_sheets") or {}).get("tabs") or {}).get("trade_source_activity")) or "trade_source_activity")
        worksheet = client._worksheet(title, rows=max(len(decisions) + 15, 100), cols=8)
        previous = client._retry(worksheet.get_all_values, operation="read source brief") or []
        old_review = bool(previous and list(previous[0]) == TRADE_SOURCE_REVIEW_HEADER)
        old_brief = bool(len(previous) >= 7 and list(previous[6])[:8] == TRADE_SOURCE_BRIEF_HEADER)
        if previous and not (old_review or old_brief):
            raise ValueError("trade_source_activity header is unrecognized; refusing to overwrite operator content")

        corrections_by_key: dict[str, str] = {}
        corrections_by_post: dict[str, str] = {}
        old_rows = previous[1:] if old_review else previous[7:] if old_brief else []
        for row in old_rows:
            correction = str(row[6] if len(row) > 6 else "").strip()
            if not correction:
                continue
            post = str(row[1] if len(row) > 1 else "")
            key = str(row[7] if old_brief and len(row) > 7 else "")
            if key:
                corrections_by_key[key] = correction
            elif post:
                corrections_by_post[post] = correction
        merged: list[list[Any]] = []
        used_keys: set[str] = set()
        used_posts: set[str] = set()
        for raw in decisions:
            row = (list(raw) + [""] * 8)[:8]
            post, key = str(row[1]), str(row[7])
            if key in corrections_by_key:
                row[6] = corrections_by_key[key]
                used_keys.add(key)
            elif post in corrections_by_post:
                row[6] = corrections_by_post[post]
                used_posts.add(post)
            merged.append(row)
        # A correction never disappears because its source aged out of the
        # bounded brief; keep only those older rows with human content.
        for old in old_rows:
            correction = str(old[6] if len(old) > 6 else "").strip()
            if not correction:
                continue
            post = str(old[1] if len(old) > 1 else "")
            key = str(old[7] if old_brief and len(old) > 7 else "")
            if key in used_keys or post in used_posts:
                continue
            guru = str(old[0] if old else "")
            opening = str(old[2] if len(old) > 2 else "")
            merged.append([guru, post, opening, "Correction retained", "Review the source correction", "", correction, key or f"post:{post}"])

        summary_rows = [(list(row) + [""] * 8)[:8] for row in summary[:5]]
        summary_rows.extend([[""] * 8] * (5 - len(summary_rows)))
        values = [*summary_rows, [""] * 8, TRADE_SOURCE_BRIEF_HEADER, *merged]
        if len(values) > worksheet.row_count:
            client._retry(lambda: worksheet.resize(rows=len(values) + 10), operation="extend source brief")
        if worksheet.col_count < 8:
            client._retry(lambda: worksheet.resize(cols=8), operation="extend source brief columns")
        values.extend([[""] * 8 for _ in range(max(len(previous) - len(values), 0))])
        client._retry(
            lambda: worksheet.update(range_name=f"A1:H{len(values)}", values=values, value_input_option="RAW"),
            operation="publish source brief",
        )
        return len(merged)


@contextmanager
def daily_plan_publication():
    """One cross-process read/modify/write lease for every cockpit publisher."""
    lock_path = Path("data/runlocks/daily_plan_publish.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@daily_plan_publication()
def write_daily_plan(
    config: dict[str, Any], rows: list[list[Any]], header: list[str], *,
    replace_lanes: set[str] | None = None,
) -> int:
    return _write_daily_plan_locked(config, rows, header, replace_lanes=replace_lanes)


def _write_daily_plan_locked(
    config: dict[str, Any], rows: list[list[Any]], header: list[str], *,
    replace_lanes: set[str] | None = None,
) -> int:
    incoming = [dict(zip(header, row)) for row in rows]
    today = market_today(config)
    if replace_lanes and any(
        str(row.get("plan_date") or "") != today or _row_lane(row) not in replace_lanes
        for row in incoming
    ):
        raise ValueError("daily_plan publication must contain only current-day rows for the owned lanes")
    client = GoogleSheetClient.from_config(config)
    title = (((config.get("google_sheets") or {}).get("tabs") or {}).get("daily_plan") or "daily_plan")
    merged = incoming
    if replace_lanes:
        existing = client.read_tab(title)
        kept = [row for row in existing if not (
            str(row.get("plan_date") or "") == today and _row_lane(row) in replace_lanes
        )]
        # Historical duplicate projections are not distinct decisions. Preserve
        # the latest row for each identity, including operator notes/actions.
        unique = {}
        for row in kept + incoming:
            key = (str(row.get("plan_date") or ""), _row_lane(row),
                   str(row.get("plan_id") or ""), str(row.get("plan_rank") or ""))
            unique[key] = row
        merged = list(unique.values())
    return client.replace_plan_values(title, header=header, rows=[_row_from_dict(row, header) for row in merged])


def write_live_book(config: dict[str, Any], header: list[str], rows: list[list[Any]]) -> int:
    client = GoogleSheetClient.from_config(config)
    tab_names = ((config.get("google_sheets") or {}).get("tabs") or {})
    title = str(tab_names.get("live_book") or "live_book")
    return client.replace_tab(
        title,
        header=header,
        rows=rows,
    )


def write_universe_proposals(config: dict[str, Any], proposals: list[dict[str, str]]) -> int:
    """Append disabled universe rows; discovery provenance belongs to the ledger.

    Does not clear or rewrite the tab.  Existing rows (including their formulas,
    formatting, validation, and operator notes) are untouched; this
    function may append only previously unseen proposed symbols.
    """
    if not proposals:
        return 0
    client = GoogleSheetClient.from_config(config)
    tab_names = ((config.get("google_sheets") or {}).get("tabs") or {})
    title = str(tab_names.get("universe") or "universe")
    existing = client.read_tab(title)
    from kamandal_v2.schemas import UNIVERSE_HEADER

    values = client.read_tab_values(title)
    existing_headers = [str(cell).strip() for cell in (values[0] if values else [])]
    if len(set(existing_headers)) != len(existing_headers) or not set(UNIVERSE_HEADER).issubset(existing_headers):
        missing = sorted(set(UNIVERSE_HEADER) - set(existing_headers))
        unexpected = sorted(set(existing_headers) - set(UNIVERSE_HEADER))
        raise ValueError(
            f"worksheet {title!r} universe proposal schema mismatch: "
            f"missing={missing} unexpected={unexpected}"
        )
    # Column position belongs to the operator Sheet.  Proposal columns may be
    # appended after the legacy notes column; map values by the actual header
    # instead of forcing a destructive column reorder.
    header = existing_headers
    existing_by_symbol = {str(row.get("symbol") or "").upper(): row for row in existing}
    appendable = [
        proposal
        for proposal in proposals
        if str(proposal.get("symbol") or "").upper() not in existing_by_symbol
    ]
    if any(str(proposal.get("enabled") or "").upper() != "FALSE" for proposal in appendable):
        raise ValueError("new universe proposals must explicitly set enabled=FALSE")
    rows = [[proposal.get(col, "") for col in header] for proposal in appendable]
    written = client.append_tab_rows(title, header=header, rows=rows)
    readback = {str(row.get("symbol") or "").upper(): row for row in client.read_tab(title)}
    mismatches: list[str] = []
    # Verify all three operator columns before committing review evidence.
    machine_owned = set(UNIVERSE_HEADER)
    for proposal in appendable:
        symbol = str(proposal.get("symbol") or "").upper()
        observed = readback.get(symbol)
        if observed is None:
            mismatches.append(f"{symbol}: missing")
            continue
        for field in machine_owned:
            expected = str(proposal.get(field, "")).strip()
            actual = str(observed.get(field, "")).strip()
            if expected != actual:
                mismatches.append(f"{symbol}:{field} expected={expected!r} actual={actual!r}")
    if mismatches:
        raise RuntimeError(f"universe proposal append readback mismatch: {'; '.join(mismatches)}")
    return written


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return value


def _row_from_dict(row: dict[str, Any], header: list[str]) -> list[Any]:
    return [row.get(column, "") for column in header]


def _row_lane(row: dict[str, Any]) -> str:
    detail = row.get("plan_detail_json") or ""
    if detail:
        try:
            parsed = json.loads(detail)
            lane = str(parsed.get("lane") or "")
            if lane:
                return lane
        except Exception:
            pass
    return str(row.get("mode") or "")


def _col_letter(index: int) -> str:
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters

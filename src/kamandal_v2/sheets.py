"""Google Sheets bootstrap for the configuration cockpit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Sequence, TypeVar

from kamandal_v2.config import google_credentials_path, spreadsheet_id


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


def write_daily_plan(
    config: dict[str, Any],
    rows: list[list[Any]],
    header: list[str],
    *,
    replace_lanes: set[str] | None = None,
) -> int:
    client = GoogleSheetClient.from_config(config)
    tab_names = ((config.get("google_sheets") or {}).get("tabs") or {})
    title = str(tab_names.get("daily_plan") or "daily_plan")
    merged_rows = rows
    if replace_lanes:
        existing = client.read_tab(title)
        today = date.today().isoformat()
        keep = [
            _row_from_dict(row, header)
            for row in existing
            if not (
                str(row.get("plan_date") or "") == today
                and _row_lane(row) in replace_lanes
            )
        ]
        merged_rows = keep + rows
    return client.replace_tab(
        title,
        header=header,
        rows=merged_rows,
    )


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
    """Append up to 5/day tier=proposed rows to the existing universe tab.

    Does not clear or rewrite the tab.  Existing rows (including their formulas,
    formatting, validation, tier, and operator notes) are untouched; this
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
    if len(existing_headers) != len(UNIVERSE_HEADER) or set(existing_headers) != set(UNIVERSE_HEADER):
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
    rows = [[proposal.get(col, "") for col in header] for proposal in appendable]
    written = client.append_tab_rows(title, header=header, rows=rows)
    readback = {str(row.get("symbol") or "").upper(): row for row in client.read_tab(title)}
    mismatches: list[str] = []
    machine_owned = {"symbol", "enabled", "tier", "proposal_source", "proposal_reason", "proposal_date"}
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

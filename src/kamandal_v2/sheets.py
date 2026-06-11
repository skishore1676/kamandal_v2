"""Google Sheets bootstrap for the configuration cockpit."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
from datetime import date
from typing import Any, Sequence

from kamandal_v2.config import google_credentials_path, spreadsheet_id


@dataclass
class BootstrapResult:
    spreadsheet_id: str
    tabs: dict[str, int]


class GoogleSheetClient:
    def __init__(self, *, credentials_path: Path, spreadsheet_id_value: str):
        try:
            import gspread  # type: ignore
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
        self._spreadsheet = self._client.open_by_key(spreadsheet_id_value)
        self.spreadsheet_id = spreadsheet_id_value

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "GoogleSheetClient":
        return cls(
            credentials_path=google_credentials_path(config),
            spreadsheet_id_value=spreadsheet_id(config),
        )

    def replace_tab(
        self,
        title: str,
        *,
        header: Sequence[str],
        rows: Sequence[Sequence[Any]],
    ) -> int:
        worksheet = self._worksheet(title, rows=max(len(rows) + 10, 100), cols=max(len(header), 26))
        worksheet.clear()
        values = [list(header)]
        for row in rows:
            padded = list(row) + [""] * (len(header) - len(row))
            values.append([_cell(value) for value in padded[: len(header)]])
        worksheet.update(
            range_name=f"A1:{_col_letter(len(header))}{len(values)}",
            values=values,
            value_input_option="USER_ENTERED",
        )
        worksheet.freeze(rows=1)
        return len(rows)

    def read_tab(self, title: str) -> list[dict[str, str]]:
        worksheet = self._worksheet(title, rows=100, cols=26)
        values = worksheet.get_all_values() or []
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

    def _worksheet(self, title: str, *, rows: int, cols: int) -> Any:
        try:
            return self._spreadsheet.worksheet(title)
        except Exception:
            return self._spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


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
        for logical_name in ("universe", "playbooks", "daily_plan")
    }


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

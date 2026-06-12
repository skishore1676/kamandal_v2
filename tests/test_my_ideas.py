from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from kamandal_v2.planner.idea_loader import load_ideas
from kamandal_v2.schemas import MY_IDEAS_HEADER
from kamandal_v2.sources.my_ideas import convert_rows, import_my_ideas

TODAY = date(2026, 6, 12)
UNIVERSE = {"AMZN", "TSLA", "SPY", "NVDA"}


def _row(**overrides: str) -> dict[str, str]:
    base = {
        "date": TODAY.isoformat(),
        "ticker": "AMZN",
        "type_of_trade": "Contrarian",
        "direction": "Bull",
        "horizon_days": "30",
        "notes": "felt oversold after the news",
        "conviction": "high",
        "status": "",
        "idea_id": "",
        "imported_at": "",
    }
    base.update(overrides)
    return base


def test_converts_journal_style_row_to_idea() -> None:
    ideas, statuses = convert_rows([_row()], universe_symbols=UNIVERSE, today=TODAY)

    assert statuses[0]["status"] == "imported"
    idea = ideas[0]
    assert idea["idea_id"] == "op_2026-06-12_AMZN_bull"
    assert idea["source"] == "operator_sheet"
    assert idea["direction"] == "bullish"
    assert idea["thesis_tags"] == ["mean_revert", "oversold"]
    assert idea["horizon_days"] == 30
    assert idea["operator_status"] == "approved"
    assert idea["notes"] == "felt oversold after the news"


def test_taxonomy_mapping_handles_direction_variants() -> None:
    rows = [
        _row(ticker="TSLA", type_of_trade="Breakout", direction="Bear"),
        _row(ticker="SPY", type_of_trade="Neutral High IV", direction="Neutral"),
        _row(ticker="NVDA", type_of_trade="high_iv, range_bound", direction="Neutral"),
    ]
    ideas, statuses = convert_rows(rows, universe_symbols=UNIVERSE, today=TODAY)

    assert [status["status"] for status in statuses] == ["imported"] * 3
    assert ideas[0]["thesis_tags"] == ["breakdown", "momentum"]
    assert "theta_harvest" in ideas[1]["thesis_tags"]
    assert ideas[2]["thesis_tags"] == ["high_iv", "range_bound"]


def test_guardrails_reject_naked_unknown_ticker_and_bad_direction() -> None:
    rows = [
        _row(type_of_trade="High Vol - Naked"),
        _row(ticker="ZZZZ"),
        _row(direction="sideways"),
        _row(ticker=""),
    ]
    ideas, statuses = convert_rows(rows, universe_symbols=UNIVERSE, today=TODAY)

    assert ideas == []
    assert statuses[0]["status"] == "rejected_naked_structures_not_allowed"
    assert statuses[1]["status"] == "not_in_universe_add_to_universe_tab"
    assert statuses[2]["status"] == "invalid_direction_use_bull_bear_neutral"
    assert statuses[3]["status"] == "empty_ticker"


def test_skips_example_stale_and_duplicate_rows() -> None:
    rows = [
        _row(status="example"),
        _row(date="2026-06-10", status="imported"),
        _row(),
        _row(),
        _row(date=""),
    ]
    ideas, statuses = convert_rows(rows, universe_symbols=UNIVERSE, today=TODAY)

    assert [status["status"] for status in statuses] == [
        "example",
        "imported",
        "imported",
        "duplicate_row_skipped",
        "duplicate_row_skipped",
    ]
    assert len(ideas) == 1


class FakeClient:
    def __init__(self, rows: list[dict[str, str]], universe_rows: list[dict[str, str]]):
        self.tabs: dict[str, list[dict[str, str]]] = {"my_ideas": rows, "universe": universe_rows}
        self.replaced: dict[str, list[list[Any]]] = {}

    def read_tab(self, title: str) -> list[dict[str, str]]:
        return list(self.tabs.get(title, []))

    def replace_tab(self, title: str, *, header: list[str], rows: list[list[Any]]) -> int:
        self.replaced[title] = rows
        return len(rows)


def test_import_writes_yaml_loadable_by_planner_and_statuses_back(tmp_path: Path) -> None:
    client = FakeClient(
        rows=[_row(), _row(ticker="ZZZZ")],
        universe_rows=[{"symbol": "AMZN", "enabled": "TRUE"}, {"symbol": "TSLA", "enabled": "TRUE"}],
    )

    result = import_my_ideas({}, ideas_dir=tmp_path / "active", client=client, today=TODAY)

    assert result["imported"] == 1
    assert result["statuses"] == ["imported", "not_in_universe_add_to_universe_tab"]
    ideas_path = Path(result["ideas_path"])
    assert ideas_path.name == "2026-06-12_my_ideas.yaml"
    loaded = load_ideas([ideas_path])
    assert len(loaded) == 1
    assert loaded[0].underlying == "AMZN"
    assert loaded[0].source == "operator_sheet"
    assert loaded[0].operator_status == "approved"
    written = client.replaced["my_ideas"]
    status_index = MY_IDEAS_HEADER.index("status")
    assert written[0][status_index] == "imported"
    assert written[1][status_index] == "not_in_universe_add_to_universe_tab"
    idea_id_index = MY_IDEAS_HEADER.index("idea_id")
    assert written[0][idea_id_index] == "op_2026-06-12_AMZN_bull"


def test_bootstrap_creates_tab_with_example_row(tmp_path: Path) -> None:
    client = FakeClient(rows=[], universe_rows=[])

    result = import_my_ideas({}, ideas_dir=tmp_path, client=client, bootstrap=True, today=TODAY)

    assert result["status"] == "bootstrapped"
    rows = client.replaced["my_ideas"]
    assert len(rows) == 1
    assert rows[0][MY_IDEAS_HEADER.index("status")] == "example"


def test_blank_date_gets_stamped_on_import_writeback(tmp_path: Path) -> None:
    client = FakeClient(
        rows=[_row(date="")],
        universe_rows=[{"symbol": "AMZN", "enabled": "TRUE"}],
    )

    import_my_ideas({}, ideas_dir=tmp_path, client=client, today=TODAY)

    written = client.replaced["my_ideas"]
    assert written[0][MY_IDEAS_HEADER.index("date")] == "2026-06-12"
    assert written[0][MY_IDEAS_HEADER.index("status")] == "imported"

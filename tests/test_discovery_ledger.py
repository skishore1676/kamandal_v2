from __future__ import annotations

from datetime import date

from kamandal_v2.sources.my_ideas import import_my_ideas
from kamandal_v2.stores.sqlite import LocalStore


def test_discovery_ledger_deduplicates_replays_and_counts_source_diversity(tmp_path) -> None:
    store = LocalStore(tmp_path / "kamandal.db")

    assert store.record_discovery_evidence(symbol="ABCD", source_profile="x", source_record_id="1", exclusion_reason="outside", evidence_ref="x:1")
    assert not store.record_discovery_evidence(symbol="ABCD", source_profile="x", source_record_id="1", exclusion_reason="outside", evidence_ref="x:1")
    assert store.record_discovery_evidence(symbol="ABCD", source_profile="youtube", source_record_id="9", exclusion_reason="outside", evidence_ref="youtube:9")

    [candidate] = store.discovery_candidates()
    assert candidate["symbol"] == "ABCD"
    assert candidate["mention_count"] == 2
    assert candidate["source_profiles"] == ["x", "youtube"]


def test_my_ideas_retains_outside_universe_evidence_without_creating_a_tradable_idea(tmp_path) -> None:
    class Client:
        def read_tab(self, name):  # noqa: ANN001
            if name == "my_ideas":
                return [{"date": "2026-08-15", "ticker": "ABCD", "direction": "bull", "type_of_trade": "breakout"}]
            return [{"symbol": "SPY", "enabled": "TRUE"}]

    store = LocalStore(tmp_path / "kamandal.db")
    result = import_my_ideas(
        {"google_sheets": {"tabs": {"my_ideas": "my_ideas", "universe": "universe"}}},
        client=Client(),
        store=store,
        write_sheet=False,
        today=date(2026, 8, 15),
        ideas_dir=tmp_path / "ideas",
    )

    assert result["imported"] == 0
    assert result["discovery_evidence_recorded"] == 1
    assert result["statuses"] == ["not_in_universe_add_to_universe_tab"]
    assert store.discovery_candidates()[0]["symbol"] == "ABCD"

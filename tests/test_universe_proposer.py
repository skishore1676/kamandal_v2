from __future__ import annotations

import json
import os
from datetime import UTC, datetime

from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.tools.universe_proposer import collect_out_of_universe_symbols, micro_stock_guard


def test_micro_stock_guard_requires_sourced_price_and_liquidity() -> None:
    assert micro_stock_guard("GOOD", market_facts={"price": 80, "avg_dollar_volume": 50_000_000, "market_cap": 5_000_000_000})
    assert not micro_stock_guard("LOW", market_facts={"price": 8, "avg_dollar_volume": 50_000_000, "market_cap": 5_000_000_000})
    assert not micro_stock_guard("THIN", market_facts={"price": 80, "avg_dollar_volume": 1_000_000, "market_cap": 5_000_000_000})
    assert not micro_stock_guard("UNK", market_facts={"price": None, "avg_dollar_volume": None, "market_cap": None})


def test_proposer_reads_plan_diagnostics_dedupes_sheet_and_records_evidence(tmp_path) -> None:
    audit = tmp_path / "latest_plan_run.json"
    audit.write_text(json.dumps({"idea_diagnostics": [
        {"underlying": "GOOD", "status": "out_of_universe"},
        {"underlying": "GOOD", "status": "out_of_universe"},
        {"underlying": "EXIST", "status": "out_of_universe"},
    ]}))
    os.utime(audit, (datetime.now(UTC).timestamp(), datetime.now(UTC).timestamp()))
    store = LocalStore(tmp_path / "kamandal.db")

    proposals = collect_out_of_universe_symbols(
        store,
        existing_symbols={"EXIST"},
        audit_path=audit,
        market_facts_loader=lambda _symbol: {
            "price": 75.0,
            "avg_dollar_volume": 40_000_000.0,
            "market_cap": 4_000_000_000.0,
        },
    )

    assert [proposal["symbol"] for proposal in proposals] == ["GOOD"]
    assert "verified price=75.00" in proposals[0]["notes"]
    assert proposals[0]["enabled"] == "FALSE"

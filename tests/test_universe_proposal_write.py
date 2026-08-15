from __future__ import annotations

from kamandal_v2.schemas import UNIVERSE_HEADER
from kamandal_v2 import sheets


def test_universe_proposals_append_only_and_preserve_existing_rows(monkeypatch) -> None:  # noqa: ANN001
    existing = [{key: "" for key in UNIVERSE_HEADER}]
    existing[0].update({"symbol": "EXIST", "tier": "held", "notes": "operator note"})
    calls: list[dict] = []

    class Client:
        def read_tab(self, _title):  # noqa: ANN001
            return existing

        def append_tab_rows(self, title, *, header, rows):  # noqa: ANN001
            calls.append({"title": title, "header": header, "rows": rows})
            for row in rows:
                existing.append(dict(zip(header, row, strict=True)))
            return len(rows)

    monkeypatch.setattr(sheets.GoogleSheetClient, "from_config", lambda _config: Client())
    count = sheets.write_universe_proposals(
        {},
        [
            {"symbol": "EXIST", "enabled": "FALSE", "tier": "proposed"},
            {"symbol": "NEW", "enabled": "FALSE", "tier": "proposed", "proposal_source": "durable_discovery"},
        ],
    )

    assert count == 1
    assert existing[0]["tier"] == "held"
    assert existing[0]["notes"] == "operator note"
    assert calls[0]["rows"][0][UNIVERSE_HEADER.index("symbol")] == "NEW"


def test_universe_proposals_require_exact_machine_owned_readback(monkeypatch) -> None:  # noqa: ANN001
    existing = [{key: "" for key in UNIVERSE_HEADER}]

    class Client:
        def read_tab(self, _title):  # noqa: ANN001
            return existing

        def append_tab_rows(self, _title, *, header, rows):  # noqa: ANN001
            appended = dict(zip(header, rows[0], strict=True))
            appended["tier"] = "held"
            existing.append(appended)
            return 1

    monkeypatch.setattr(sheets.GoogleSheetClient, "from_config", lambda _config: Client())

    try:
        sheets.write_universe_proposals({}, [{"symbol": "NEW", "enabled": "FALSE", "tier": "proposed"}])
    except RuntimeError as exc:
        assert "tier" in str(exc)
    else:
        raise AssertionError("proposal write must reject an inexact readback")

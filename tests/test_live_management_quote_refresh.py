from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from kamandal_v2.domain.models import ChainSnapshot, OptionQuote
from kamandal_v2.live.management import _refresh_live_group_quotes
from kamandal_v2.stores.sqlite import LocalStore


class _Broker:
    def __init__(self) -> None:
        self.expiration_dates = ["2026-07-24"]
        self.calls: list[tuple[str, list[str]]] = []

    def available(self) -> bool:
        return True

    def chain_snapshot(self, underlying: str) -> ChainSnapshot:
        self.calls.append((underlying, list(self.expiration_dates)))
        return ChainSnapshot(
            chain_snapshot_id=f"test_{underlying}",
            underlying=underlying,
            captured_at=datetime.now(tz=UTC).isoformat(),
            underlying_price=100.0,
            quotes=[
                OptionQuote(
                    underlying=underlying,
                    expiration=self.expiration_dates[0],
                    option_type="put",
                    strike=95.0,
                    bid=1.0,
                    ask=1.1,
                    delta=-0.2,
                    gamma=0.0,
                    theta=-0.01,
                    vega=0.1,
                    iv=0.25,
                    open_interest=100,
                )
            ],
            source="test",
        )


def test_live_quote_refresh_includes_existing_position_expirations(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    broker = _Broker()
    position_expiration = (date.today() + timedelta(days=30)).isoformat()
    listed_expiration = (date.today() + timedelta(days=37)).isoformat()
    broker.expiration_dates = [listed_expiration]
    store = LocalStore(tmp_path / "kamandal.db")
    groups = [
        {
            "group_id": "live_group_1",
            "underlying": "DELL",
            "candidate": {
                "underlying": "DELL",
                "legs": [
                    {"expiration": position_expiration, "option_type": "put", "strike": 95.0},
                    {"expiration": position_expiration, "option_type": "put", "strike": 100.0},
                ],
            },
        }
    ]
    config = {"live": {"exit_pricing": {"require_fresh_quotes": True}}}
    monkeypatch.setattr("kamandal_v2.live.management.broker_adapter", lambda _config: broker)

    refreshed = _refresh_live_group_quotes(config, store, groups)

    assert refreshed == {"DELL"}
    assert broker.calls == [("DELL", [position_expiration, listed_expiration])]

from kamandal_v2.domain.models import Playbook, UniverseEntry
from kamandal_v2.planner.config_validator import validate_config


def _playbook(playbook_id: str, **overrides) -> Playbook:
    base = {
        "playbook_id": playbook_id,
        "enabled": True,
        "strategy_family": "put_spread",
        "structure": "put_spread",
        "variant": "default",
        "leg_count": 2,
        "profiles": ["large_stocks"],
        "applicable_direction": ["bullish"],
        "applicable_thesis_tags": ["support_bounce"],
        "iv_percentile_min": 30,
        "iv_percentile_max": 80,
    }
    base.update(overrides)
    return Playbook(**base)


def test_validate_config_errors_for_enabled_unsupported_structure() -> None:
    result = validate_config(
        [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")],
        [_playbook("unsupported", structure="not_real", strategy_family="not_real")],
    )

    assert not result.ok
    assert "enabled_playbook_missing_builder:unsupported:not_real" in result.errors
    assert "enabled_playbook_missing_validator:unsupported:not_real" in result.errors


def test_validate_config_errors_for_missing_tables() -> None:
    result = validate_config([], [])

    assert not result.ok
    assert "config_missing_universe_rows" in result.errors
    assert "config_missing_playbook_rows" in result.errors


def test_validate_config_errors_when_no_playbooks_enabled() -> None:
    result = validate_config(
        [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")],
        [_playbook("disabled", enabled=False)],
    )

    assert not result.ok
    assert "config_missing_enabled_playbooks" in result.errors


def test_validate_config_errors_when_enabled_playbook_is_unreachable_from_universe() -> None:
    result = validate_config(
        [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks", allowed_playbooks=["put_spread"])],
        [_playbook("short_strangle_high_iv", strategy_family="short_strangle", structure="short_strangle", profiles=["index_etf"])],
    )

    assert not result.ok
    assert "enabled_playbook_unreachable_from_universe:short_strangle_high_iv:short_strangle" in result.errors


def test_validate_config_allows_enabled_playbook_routed_by_structure() -> None:
    result = validate_config(
        [UniverseEntry(symbol="SPY", enabled=True, profile="index_etf", allowed_playbooks=["short_strangle"])],
        [_playbook("short_strangle_high_iv", strategy_family="short_strangle", structure="short_strangle", profiles=["index_etf"])],
    )

    assert result.ok


def test_validate_config_errors_for_enabled_unknown_thesis_tag() -> None:
    result = validate_config(
        [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")],
        [_playbook("put_spread_default", applicable_thesis_tags=["support_bounce", "needs_confirmation"])],
    )

    assert not result.ok
    assert "enabled_playbook_unknown_thesis_tag:put_spread_default:needs_confirmation" in result.errors


def test_validate_config_allows_disabled_unknown_thesis_tag() -> None:
    result = validate_config(
        [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")],
        [
            _playbook("put_spread_default"),
            _playbook("experimental", enabled=False, applicable_thesis_tags=["needs_confirmation"]),
        ],
    )

    assert result.ok
    assert not result.errors


def test_validate_config_warns_on_overlapping_enabled_variants() -> None:
    result = validate_config(
        [UniverseEntry(symbol="TSLA", enabled=True, profile="large_stocks")],
        [
            _playbook("put_spread_default", iv_percentile_min=30, iv_percentile_max=80),
            _playbook("put_spread_high_ivr", variant="high_ivr", iv_percentile_min=70, iv_percentile_max=100),
        ],
    )

    assert result.ok
    assert any("overlapping_enabled_variants:put_spread" in warning for warning in result.warnings)

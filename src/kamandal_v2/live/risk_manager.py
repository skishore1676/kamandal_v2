"""Portfolio-level risk circuit breakers for live entries.

Disabled by default (`risk_manager.enabled: false`); flip via
`KAMANDAL_RISK_MANAGER_ENABLED=true` in `.env` once the knobs are tuned.

Breakers only ever block NEW entries — exits and management are never blocked,
so a tripped breaker can only de-risk the book, not trap a position.

Drawdown is measured on `account_size` from account snapshots, so deposits and
withdrawals inside the window distort it (a deposit masks drawdown, a
withdrawal fakes one). Acceptable for v1; keep transfers in mind when reading
breaker events.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from kamandal_v2.stores.sqlite import LocalStore

BREAKER_DAILY_DRAWDOWN = "risk_daily_drawdown_breaker"
BREAKER_WEEKLY_DRAWDOWN = "risk_weekly_drawdown_breaker"
BREAKER_CONSECUTIVE_LOSSES = "risk_consecutive_loss_cooldown"
BREAKER_DAILY_NEW_POSITIONS = "risk_daily_new_position_cap"
REASON_CLUSTER_AT_CAP = "risk_cluster_at_cap"

_SNAPSHOT_ID_RE = re.compile(r"(\d{8}T\d{6})Z?$")


@dataclass
class RiskDecision:
    enabled: bool
    blocked: bool = False
    reasons: list[dict[str, Any]] = field(default_factory=list)
    clusters_at_cap: dict[str, list[str]] = field(default_factory=dict)
    checked_at: str = ""

    def reason_codes(self) -> list[str]:
        return [str(reason.get("code") or "") for reason in self.reasons]

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "blocked": self.blocked,
            "reasons": self.reasons,
            "clusters_at_cap": self.clusters_at_cap,
            "checked_at": self.checked_at,
        }


def risk_manager_config(config: dict[str, Any] | None) -> dict[str, Any]:
    return dict((config or {}).get("risk_manager") or {})


def risk_manager_enabled(config: dict[str, Any] | None) -> bool:
    raw = risk_manager_config(config).get("enabled")
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def evaluate_entry_risk(
    store: LocalStore,
    config: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> RiskDecision:
    """Evaluate all entry-side circuit breakers. Cheap no-op when disabled."""

    now = now or datetime.now(UTC)
    decision = RiskDecision(enabled=risk_manager_enabled(config), checked_at=now.isoformat())
    if not decision.enabled:
        return decision

    settings = risk_manager_config(config)
    _check_drawdown(
        store,
        decision,
        now=now,
        window_days=1,
        max_drawdown_pct=_float_value(settings.get("max_daily_drawdown_pct")),
        code=BREAKER_DAILY_DRAWDOWN,
    )
    _check_drawdown(
        store,
        decision,
        now=now,
        window_days=7,
        max_drawdown_pct=_float_value(settings.get("max_weekly_drawdown_pct")),
        code=BREAKER_WEEKLY_DRAWDOWN,
    )
    _check_consecutive_losses(
        store,
        decision,
        now=now,
        loss_limit=_int_value(settings.get("consecutive_loss_limit")),
        cooldown_days=_int_value(settings.get("cooldown_days")) or 2,
    )
    _check_daily_new_positions(
        store,
        decision,
        now=now,
        max_new_positions=_int_value(settings.get("max_new_positions_per_day")),
    )
    _check_cluster_concentration(
        store,
        decision,
        clusters=settings.get("correlation_clusters") or {},
        max_per_cluster=_int_value(settings.get("max_positions_per_cluster")),
    )
    return decision


def cluster_capped_symbols(decision: RiskDecision) -> set[str]:
    """Symbols that new entries must avoid because their cluster is at cap."""
    symbols: set[str] = set()
    for cluster_symbols in decision.clusters_at_cap.values():
        symbols.update(cluster_symbols)
    return symbols


def cluster_for_symbol(config: dict[str, Any] | None, symbol: str) -> str:
    clusters = risk_manager_config(config).get("correlation_clusters") or {}
    wanted = str(symbol or "").strip().upper()
    for name, members in clusters.items():
        if wanted in {str(member).strip().upper() for member in (members or [])}:
            return str(name)
    return ""


def _check_drawdown(
    store: LocalStore,
    decision: RiskDecision,
    *,
    now: datetime,
    window_days: int,
    max_drawdown_pct: float | None,
    code: str,
) -> None:
    if not max_drawdown_pct or max_drawdown_pct <= 0:
        return
    cutoff = now - timedelta(days=window_days)
    points: list[tuple[datetime, float]] = []
    for snapshot in store.recent_account_snapshots(limit=2000):
        stamp = _parse_snapshot_timestamp(str(snapshot.get("_snapshot_id") or ""))
        size = _float_value(snapshot.get("account_size"))
        if stamp is None or size is None or size <= 0:
            continue
        if stamp < cutoff or stamp > now:
            continue
        points.append((stamp, size))
    if len(points) < 2:
        return
    points.sort()
    peak = max(size for _, size in points)
    latest = points[-1][1]
    drawdown_pct = (peak - latest) / peak * 100.0
    if drawdown_pct > max_drawdown_pct:
        decision.blocked = True
        decision.reasons.append(
            {
                "code": code,
                "severity": "red",
                "detail": (
                    f"account down {drawdown_pct:.2f}% from {window_days}d peak "
                    f"({peak:.2f} -> {latest:.2f}), limit {max_drawdown_pct:.2f}%"
                ),
                "drawdown_pct": round(drawdown_pct, 2),
                "peak_account_size": round(peak, 2),
                "latest_account_size": round(latest, 2),
            },
        )


def _check_consecutive_losses(
    store: LocalStore,
    decision: RiskDecision,
    *,
    now: datetime,
    loss_limit: int | None,
    cooldown_days: int,
) -> None:
    if not loss_limit or loss_limit <= 0:
        return
    closed = store.closed_live_position_groups(limit=max(loss_limit * 4, 20))
    streak = 0
    latest_loss_closed_at: datetime | None = None
    for group in closed:  # most recent first
        pnl = _closed_group_pnl(store, group)
        if pnl is None:
            continue
        if pnl > 0:
            break
        streak += 1
        if latest_loss_closed_at is None:
            latest_loss_closed_at = _parse_db_timestamp(
                str(group.get("_closed_at") or group.get("_opened_at") or "")
            )
    if streak < loss_limit:
        return
    cooldown_until = (
        latest_loss_closed_at + timedelta(days=cooldown_days) if latest_loss_closed_at else None
    )
    if cooldown_until is not None and now >= cooldown_until:
        return
    decision.blocked = True
    decision.reasons.append(
        {
            "code": BREAKER_CONSECUTIVE_LOSSES,
            "severity": "red",
            "detail": (
                f"{streak} consecutive losing closes (limit {loss_limit}); "
                f"entries paused until {cooldown_until.isoformat() if cooldown_until else 'operator review'}"
            ),
            "consecutive_losses": streak,
            "cooldown_until": cooldown_until.isoformat() if cooldown_until else "",
        },
    )


def _check_daily_new_positions(
    store: LocalStore,
    decision: RiskDecision,
    *,
    now: datetime,
    max_new_positions: int | None,
) -> None:
    if not max_new_positions or max_new_positions <= 0:
        return
    since = now.strftime("%Y-%m-%d 00:00:00")
    opened_today = store.count_live_position_groups_opened_since(since)
    if opened_today >= max_new_positions:
        decision.blocked = True
        decision.reasons.append(
            {
                "code": BREAKER_DAILY_NEW_POSITIONS,
                "severity": "red",
                "detail": f"{opened_today} position groups opened today, cap {max_new_positions}",
                "opened_today": opened_today,
            },
        )


def _check_cluster_concentration(
    store: LocalStore,
    decision: RiskDecision,
    *,
    clusters: dict[str, Any],
    max_per_cluster: int | None,
) -> None:
    """Clusters at cap do not block globally; execution blocks per-symbol."""
    if not clusters or not max_per_cluster or max_per_cluster <= 0:
        return
    membership: dict[str, str] = {}
    for name, members in clusters.items():
        for member in members or []:
            membership[str(member).strip().upper()] = str(name)
    counts: dict[str, int] = {}
    for group in store.open_live_position_groups():
        symbol = _group_underlying(group)
        cluster = membership.get(symbol)
        if cluster:
            counts[cluster] = counts.get(cluster, 0) + 1
    for name, count in sorted(counts.items()):
        if count >= max_per_cluster:
            decision.clusters_at_cap[name] = sorted(
                symbol for symbol, cluster in membership.items() if cluster == name
            )
            decision.reasons.append(
                {
                    "code": REASON_CLUSTER_AT_CAP,
                    "severity": "yellow",
                    "detail": f"cluster {name} holds {count} open positions (cap {max_per_cluster}); new {name} entries blocked",
                    "cluster": name,
                    "open_positions": count,
                },
            )


def _closed_group_pnl(store: LocalStore, group: dict[str, Any]) -> float | None:
    mark = store.latest_live_position_mark(str(group.get("group_id") or ""))
    if not mark:
        return None
    # Mark payloads store the mid-quote P&L as pnl_mid (the pnl DB column is derived from it).
    return _float_value(mark.get("pnl_mid") if mark.get("pnl_mid") is not None else mark.get("pnl"))


def _group_underlying(group: dict[str, Any]) -> str:
    raw = (
        group.get("underlying")
        or (group.get("candidate") or {}).get("underlying")
        or next(
            (position.get("underlying") for position in group.get("positions") or [] if position.get("underlying")),
            "",
        )
    )
    return str(raw or "").strip().upper()


def _parse_snapshot_timestamp(snapshot_id: str) -> datetime | None:
    match = _SNAPSHOT_ID_RE.search(snapshot_id.strip())
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _parse_db_timestamp(raw: str) -> datetime | None:
    text = str(raw or "").replace("T", " ").replace("Z", "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=UTC)
    except ValueError:
        return None


def _float_value(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _int_value(raw: Any) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None

"""Configuration loading for Kamandal V2."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from kamandal_v2.paths import CONFIG_DIR, PROJECT_ROOT, resolve_path


def _load_dotenv(path: Path | None = None) -> None:
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _set_nested(config: dict[str, Any], dotted_key: str, value: Any) -> None:
    keys = dotted_key.split(".")
    node = config
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = value


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    return int(raw)


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    return float(raw)


def _env_float_list(name: str) -> list[float] | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    values = [float(item.strip()) for item in raw.split(",") if item.strip()]
    return values or None


def load_control(config_path: str | Path | None = None) -> dict[str, Any]:
    """Load local control config plus environment overrides."""

    _load_dotenv()
    path = Path(config_path) if config_path else CONFIG_DIR / "control.yaml"
    if not path.exists():
        raise FileNotFoundError(f"control config not found: {path}")
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    overrides = {
        "runtime.mode": os.environ.get("KAMANDAL_MODE"),
        "runtime.trading_enabled": _env_bool("KAMANDAL_TRADING_ENABLED"),
        "runtime.halt": _env_bool("KAMANDAL_HALT"),
        "portfolio.target_max_bpr_utilization_pct": _env_float("KAMANDAL_TARGET_MAX_BPR_UTILIZATION_PCT"),
        "portfolio.hard_max_bpr_utilization_pct": _env_float("KAMANDAL_HARD_MAX_BPR_UTILIZATION_PCT"),
        "portfolio.max_bpr_per_underlying_pct": _env_float("KAMANDAL_MAX_BPR_PER_UNDERLYING_PCT"),
        "portfolio.max_positions": _env_int("KAMANDAL_MAX_POSITIONS"),
        "portfolio.delta_guard.enabled": _env_bool("KAMANDAL_PORTFOLIO_DELTA_GUARD_ENABLED"),
        "portfolio.delta_guard.mode": os.environ.get("KAMANDAL_PORTFOLIO_DELTA_GUARD_MODE"),
        "portfolio.delta_guard.apply_modes": os.environ.get("KAMANDAL_PORTFOLIO_DELTA_GUARD_APPLY_MODES"),
        "portfolio.delta_guard.min_delta": _env_float("KAMANDAL_PORTFOLIO_DELTA_GUARD_MIN_DELTA"),
        "portfolio.delta_guard.max_delta": _env_float("KAMANDAL_PORTFOLIO_DELTA_GUARD_MAX_DELTA"),
        "portfolio.delta_guard.allow_improvement_when_outside": _env_bool(
            "KAMANDAL_PORTFOLIO_DELTA_GUARD_ALLOW_IMPROVEMENT"
        ),
        "risk_manager.enabled": _env_bool("KAMANDAL_RISK_MANAGER_ENABLED"),
        "risk_manager.max_account_snapshot_age_minutes": _env_int("KAMANDAL_RISK_MAX_ACCOUNT_SNAPSHOT_AGE_MINUTES"),
        "risk_manager.max_daily_drawdown_pct": _env_float("KAMANDAL_RISK_MAX_DAILY_DRAWDOWN_PCT"),
        "risk_manager.max_weekly_drawdown_pct": _env_float("KAMANDAL_RISK_MAX_WEEKLY_DRAWDOWN_PCT"),
        "risk_manager.consecutive_loss_limit": _env_int("KAMANDAL_RISK_CONSECUTIVE_LOSS_LIMIT"),
        "risk_manager.cooldown_days": _env_int("KAMANDAL_RISK_COOLDOWN_DAYS"),
        "risk_manager.max_new_positions_per_day": _env_int("KAMANDAL_RISK_MAX_NEW_POSITIONS_PER_DAY"),
        "risk_manager.max_positions_per_cluster": _env_int("KAMANDAL_RISK_MAX_POSITIONS_PER_CLUSTER"),
        "shadow.account_size_override": _env_float("KAMANDAL_SHADOW_ACCOUNT_SIZE"),
        "shadow.buying_power_override": _env_float("KAMANDAL_SHADOW_BUYING_POWER"),
        "shadow.bpr_used_override": _env_float("KAMANDAL_SHADOW_BPR_USED"),
        "shadow.max_positions_override": _env_int("KAMANDAL_SHADOW_MAX_POSITIONS"),
        "shadow.max_new_positions_per_plan": _env_int("KAMANDAL_SHADOW_MAX_NEW_POSITIONS_PER_PLAN"),
        "shadow.max_new_plans_per_day": _env_int("KAMANDAL_SHADOW_MAX_NEW_PLANS_PER_DAY"),
        "shadow.basket.target_new_bpr_pct": _env_float("KAMANDAL_SHADOW_BASKET_TARGET_NEW_BPR_PCT"),
        "shadow.basket.hard_new_bpr_pct": _env_float("KAMANDAL_SHADOW_BASKET_HARD_NEW_BPR_PCT"),
        "shadow.basket.min_marginal_score": _env_float("KAMANDAL_SHADOW_BASKET_MIN_MARGINAL_SCORE"),
        "shadow.basket.max_new_positions_per_plan": _env_int("KAMANDAL_SHADOW_BASKET_MAX_NEW_POSITIONS_PER_PLAN"),
        "shadow.idea_cooldown_days": _env_int("KAMANDAL_SHADOW_IDEA_COOLDOWN_DAYS"),
        "shadow.match_gate_mode": os.environ.get("KAMANDAL_SHADOW_MATCH_GATE_MODE"),
        "shadow.candidate_filter_mode": os.environ.get("KAMANDAL_SHADOW_CANDIDATE_FILTER_MODE"),
        "live.entry_approval_mode": os.environ.get("KAMANDAL_LIVE_ENTRY_APPROVAL_MODE"),
        "live.exit_approval_mode": os.environ.get("KAMANDAL_LIVE_EXIT_APPROVAL_MODE"),
        "live.auto_submit_entries": _env_bool("KAMANDAL_LIVE_AUTO_SUBMIT_ENTRIES"),
        "live.auto_submit_exits": _env_bool("KAMANDAL_LIVE_AUTO_SUBMIT_EXITS"),
        "live.telegram_approval.target": os.environ.get("KAMANDAL_TELEGRAM_APPROVAL_TARGET"),
        "live.telegram_approval.account": os.environ.get("KAMANDAL_TELEGRAM_APPROVAL_ACCOUNT"),
        "live.telegram_approval.expiry_minutes": _env_int("KAMANDAL_TELEGRAM_APPROVAL_EXPIRY_MINUTES"),
        "live.telegram_approval.max_pending_requests": _env_int("KAMANDAL_TELEGRAM_APPROVAL_MAX_PENDING_REQUESTS"),
        "live.telegram_approval.enabled": _env_bool("KAMANDAL_TELEGRAM_APPROVAL_ENABLED"),
        "live.allow_same_day_exits": _env_bool("KAMANDAL_ALLOW_SAME_DAY_LIVE_EXITS"),
        "live.allow_same_day_exits_after": os.environ.get("KAMANDAL_ALLOW_SAME_DAY_LIVE_EXITS_AFTER"),
        "live.min_entry_legs": _env_int("KAMANDAL_LIVE_MIN_ENTRY_LEGS"),
        "live.max_new_positions_per_plan": _env_int("KAMANDAL_LIVE_MAX_NEW_POSITIONS_PER_PLAN"),
        "live.max_orders_per_plan": _env_int("KAMANDAL_LIVE_MAX_ORDERS_PER_PLAN"),
        "live.max_live_entry_submits_per_run": _env_int("KAMANDAL_LIVE_MAX_ENTRY_SUBMITS_PER_RUN"),
        "live.exit_submit_source": os.environ.get("KAMANDAL_LIVE_EXIT_SUBMIT_SOURCE"),
        "live.max_close_submits_per_run": _env_int("KAMANDAL_LIVE_MAX_CLOSE_SUBMITS_PER_RUN"),
        "live.max_new_plans_per_day": _env_int("KAMANDAL_LIVE_MAX_NEW_PLANS_PER_DAY"),
        "live.max_live_baskets_per_day": _env_int("KAMANDAL_LIVE_MAX_BASKETS_PER_DAY"),
        "live.basket.target_new_bpr_pct": _env_float("KAMANDAL_LIVE_BASKET_TARGET_NEW_BPR_PCT"),
        "live.basket.hard_new_bpr_pct": _env_float("KAMANDAL_LIVE_BASKET_HARD_NEW_BPR_PCT"),
        "live.basket.min_marginal_score": _env_float("KAMANDAL_LIVE_BASKET_MIN_MARGINAL_SCORE"),
        "live.basket.max_new_positions_per_plan": _env_int("KAMANDAL_LIVE_BASKET_MAX_NEW_POSITIONS_PER_PLAN"),
        "live.max_bpr_per_order": _env_float("KAMANDAL_LIVE_MAX_BPR_PER_ORDER"),
        "live.max_bpr_per_order_pct": _env_float("KAMANDAL_LIVE_MAX_BPR_PER_ORDER_PCT"),
        "live.max_bpr_per_underlying_pct": _env_float("KAMANDAL_LIVE_MAX_BPR_PER_UNDERLYING_PCT"),
        "live.mentioned_strategy_policy": os.environ.get("KAMANDAL_LIVE_MENTIONED_STRATEGY_POLICY"),
        "live.entry_pricing.mode": os.environ.get("KAMANDAL_ENTRY_PRICING_MODE"),
        "live.entry_pricing.improvement_pct_of_spread": _env_float("KAMANDAL_ENTRY_PRICE_IMPROVEMENT_PCT_OF_SPREAD"),
        "live.entry_pricing.low_oi_improvement_pct_of_spread": _env_float("KAMANDAL_ENTRY_PRICE_LOW_OI_IMPROVEMENT_PCT_OF_SPREAD"),
        "live.entry_pricing.very_low_oi_improvement_pct_of_spread": _env_float("KAMANDAL_ENTRY_PRICE_VERY_LOW_OI_IMPROVEMENT_PCT_OF_SPREAD"),
        "live.entry_pricing.good_oi_threshold": _env_int("KAMANDAL_ENTRY_PRICE_GOOD_OI_THRESHOLD"),
        "live.entry_pricing.low_oi_threshold": _env_int("KAMANDAL_ENTRY_PRICE_LOW_OI_THRESHOLD"),
        "live.entry_pricing.min_improvement": _env_float("KAMANDAL_ENTRY_PRICE_MIN_IMPROVEMENT"),
        "live.entry_pricing.max_improvement": _env_float("KAMANDAL_ENTRY_PRICE_MAX_IMPROVEMENT"),
        "live.entry_pricing.max_improvement_by_liquidity_tier.tight": _env_float("KAMANDAL_ENTRY_PRICE_TIGHT_MAX_IMPROVEMENT"),
        "live.entry_pricing.max_improvement_by_liquidity_tier.normal": _env_float("KAMANDAL_ENTRY_PRICE_NORMAL_MAX_IMPROVEMENT"),
        "live.entry_pricing.max_improvement_by_liquidity_tier.wide": _env_float("KAMANDAL_ENTRY_PRICE_WIDE_MAX_IMPROVEMENT"),
        "live.entry_pricing.max_improvement_by_liquidity_tier.very_wide": _env_float("KAMANDAL_ENTRY_PRICE_VERY_WIDE_MAX_IMPROVEMENT"),
        "live.entry_pricing.max_improvement_by_liquidity_tier.extreme": _env_float("KAMANDAL_ENTRY_PRICE_EXTREME_MAX_IMPROVEMENT"),
        "live.entry_pricing.max_improvement_pct_of_premium": _env_float("KAMANDAL_ENTRY_PRICE_MAX_IMPROVEMENT_PCT_OF_PREMIUM"),
        "live.entry_pricing.normal_bid_ask_pct": _env_float("KAMANDAL_ENTRY_PRICE_NORMAL_BID_ASK_PCT"),
        "live.entry_pricing.width_improvement_max_pct_of_spread": _env_float("KAMANDAL_ENTRY_PRICE_WIDTH_IMPROVEMENT_MAX_PCT_OF_SPREAD"),
        "live.entry_pricing.width_improvement_curve": _env_float("KAMANDAL_ENTRY_PRICE_WIDTH_IMPROVEMENT_CURVE"),
        "live.entry_pricing.apply_to_credit": _env_bool("KAMANDAL_ENTRY_PRICE_APPLY_TO_CREDIT"),
        "live.entry_pricing.apply_to_debit": _env_bool("KAMANDAL_ENTRY_PRICE_APPLY_TO_DEBIT"),
        "live.entry_reprice.enabled": _env_bool("KAMANDAL_ENTRY_REPRICE_ENABLED"),
        "live.entry_reprice.after_minutes": _env_int("KAMANDAL_ENTRY_REPRICE_AFTER_MINUTES"),
        "live.entry_reprice.max_reprices": _env_int("KAMANDAL_ENTRY_REPRICE_MAX_REPRICES"),
        "live.entry_reprice.improvement_multiplier": _env_float("KAMANDAL_ENTRY_REPRICE_IMPROVEMENT_MULTIPLIER"),
        "live.entry_reprice.expire_after_minutes": _env_int("KAMANDAL_ENTRY_REPRICE_EXPIRE_AFTER_MINUTES"),
        "live.entry_reprice.terminal_unfilled_receipt.enabled": _env_bool("KAMANDAL_ENTRY_TERMINAL_RECEIPT_ENABLED"),
        "live.entry_reprice.terminal_unfilled_receipt.mode": os.environ.get("KAMANDAL_ENTRY_TERMINAL_RECEIPT_MODE"),
        "live.exit_reprice.enabled": _env_bool("KAMANDAL_LIVE_EXIT_REPRICE_ENABLED"),
        "live.exit_reprice.after_minutes": _env_int("KAMANDAL_LIVE_EXIT_REPRICE_AFTER_MINUTES"),
        "live.exit_reprice.max_reprices": _env_int("KAMANDAL_LIVE_EXIT_REPRICE_MAX_REPRICES"),
        "live.exit_reprice.expire_after_minutes": _env_int("KAMANDAL_LIVE_EXIT_REPRICE_EXPIRE_AFTER_MINUTES"),
        "live.liquidity_policy.low_oi_mode": os.environ.get("KAMANDAL_LIVE_LOW_OI_MODE"),
        "live.liquidity_policy.wide_bid_ask_mode": os.environ.get("KAMANDAL_LIVE_WIDE_BID_ASK_MODE"),
        "live.liquidity_policy.absurd_bid_ask_pct": _env_float("KAMANDAL_LIVE_ABSURD_BID_ASK_PCT"),
        "live.reconciliation.enabled": _env_bool("KAMANDAL_LIVE_RECONCILIATION_ENABLED"),
        "live.reconciliation.broker_flat_confirmations_required": _env_int("KAMANDAL_LIVE_RECONCILIATION_FLAT_CONFIRMATIONS_REQUIRED"),
        "live.reconciliation.auto_retire_ghost_after_confirmations": _env_bool("KAMANDAL_LIVE_RECONCILIATION_AUTO_RETIRE_GHOST"),
        "live.reconciliation.auto_local_repair_enabled": _env_bool("KAMANDAL_LIVE_RECONCILIATION_AUTO_LOCAL_REPAIR"),
        "live.reconciliation.block_management_on_open_issues": _env_bool("KAMANDAL_LIVE_RECONCILIATION_BLOCK_MANAGEMENT"),
        "live.reconciliation.order_reconciliation_enabled": _env_bool("KAMANDAL_LIVE_ORDER_RECONCILIATION_ENABLED"),
        "live.reconciliation.stale_close_approval_minutes": _env_int("KAMANDAL_LIVE_STALE_CLOSE_APPROVAL_MINUTES"),
        "live.reconciliation.stale_entry_approval_minutes": _env_int("KAMANDAL_LIVE_STALE_ENTRY_APPROVAL_MINUTES"),
        "live.reconciliation.expire_stale_close_approvals": _env_bool("KAMANDAL_LIVE_EXPIRE_STALE_CLOSE_APPROVALS"),
        "live.reconciliation.retire_stale_close_failures": _env_bool("KAMANDAL_LIVE_RETIRE_STALE_CLOSE_FAILURES"),
        "live.operator_review.enabled": _env_bool("KAMANDAL_OPERATOR_REVIEW_ENABLED"),
        "live.operator_review.transport": os.environ.get("KAMANDAL_OPERATOR_REVIEW_TRANSPORT"),
        "live.operator_review.target": os.environ.get("KAMANDAL_OPERATOR_REVIEW_TARGET"),
        "live.operator_review.account": os.environ.get("KAMANDAL_OPERATOR_REVIEW_ACCOUNT"),
        "live.operator_review.lathi_profile": os.environ.get("KAMANDAL_OPERATOR_REVIEW_LATHI_BUS_PROFILE") or os.environ.get("KAMANDAL_OPERATOR_REVIEW_LATHI_PROFILE"),
        "live.operator_review.lathi_mode": os.environ.get("KAMANDAL_OPERATOR_REVIEW_LATHI_BUS_MODE") or os.environ.get("KAMANDAL_OPERATOR_REVIEW_LATHI_MODE"),
        "live.operator_review.expiry_minutes": _env_int("KAMANDAL_OPERATOR_REVIEW_EXPIRY_MINUTES"),
        "live.operator_review.max_pending_requests": _env_int("KAMANDAL_OPERATOR_REVIEW_MAX_PENDING_REQUESTS"),
        "live.operator_review.use_inline_buttons": _env_bool("KAMANDAL_OPERATOR_REVIEW_USE_INLINE_BUTTONS"),
        "live.operator_review.text_fallback": _env_bool("KAMANDAL_OPERATOR_REVIEW_TEXT_FALLBACK"),
        "live.exit_pricing.profit_target_trigger_pct": _env_float("KAMANDAL_EXIT_PROFIT_TARGET_TRIGGER_PCT"),
        "live.exit_pricing.min_profit_to_trigger": _env_float("KAMANDAL_EXIT_MIN_PROFIT_TO_TRIGGER"),
        "live.exit_pricing.profit_floor_pct": _env_float("KAMANDAL_EXIT_PROFIT_FLOOR_PCT"),
        "live.exit_pricing.require_fresh_quotes": _env_bool("KAMANDAL_EXIT_REQUIRE_FRESH_QUOTES"),
        "live.exit_pricing.max_loss_action": os.environ.get("KAMANDAL_EXIT_MAX_LOSS_ACTION"),
        "live.exit_pricing.max_loss_requires_confirmation": _env_bool("KAMANDAL_EXIT_MAX_LOSS_REQUIRES_CONFIRMATION"),
        "live.exit_pricing.short_strike_buffer_pct": _env_float("KAMANDAL_EXIT_SHORT_STRIKE_BUFFER_PCT"),
        "live.exit_pricing.loss_watch_max_leg_bid_ask_pct": _env_float("KAMANDAL_EXIT_LOSS_WATCH_MAX_LEG_BID_ASK_PCT"),
        "live.exit_pricing.loss_watch_confirmations_required": _env_int("KAMANDAL_EXIT_LOSS_WATCH_CONFIRMATIONS_REQUIRED"),
        "live.exit_pricing.loss_watch_window_minutes": _env_int("KAMANDAL_EXIT_LOSS_WATCH_WINDOW_MINUTES"),
        "live.exit_pricing.default_max_loss_multiple_credit": _env_float("KAMANDAL_EXIT_DEFAULT_MAX_LOSS_MULTIPLE_CREDIT"),
        "live.exit_pricing.default_max_loss_multiple_debit": _env_float("KAMANDAL_EXIT_DEFAULT_MAX_LOSS_MULTIPLE_DEBIT"),
        "live.health.stale_close_order_minutes": _env_int("KAMANDAL_LIVE_HEALTH_STALE_CLOSE_ORDER_MINUTES"),
        "live.health.exit_pipeline_stalled_minutes": _env_int("KAMANDAL_LIVE_HEALTH_EXIT_PIPELINE_STALLED_MINUTES"),
        "live.health.urgent_close_order_stale_minutes": _env_int("KAMANDAL_LIVE_HEALTH_URGENT_CLOSE_ORDER_STALE_MINUTES"),
        "live.health.block_entries_on_red": _env_bool("KAMANDAL_LIVE_HEALTH_BLOCK_ENTRIES_ON_RED"),
        "planner.vertical_width_search.enabled": _env_bool("KAMANDAL_PLANNER_WIDTH_SEARCH_ENABLED"),
        "planner.vertical_width_search.widths": _env_float_list("KAMANDAL_PLANNER_WIDTH_SEARCH_WIDTHS"),
        "execution.approval_mode": os.environ.get("KAMANDAL_APPROVAL_MODE"),
        "google_sheets.spreadsheet_id": os.environ.get("KAMANDAL_SHEET_ID"),
        "google_sheets.credentials_file": os.environ.get("GOOGLE_API_CREDENTIALS_PATH"),
        "google_sheets.retry.attempts": _env_int("KAMANDAL_SHEETS_RETRY_ATTEMPTS"),
        "google_sheets.retry.base_delay_seconds": _env_float("KAMANDAL_SHEETS_RETRY_BASE_DELAY_SECONDS"),
        "google_sheets.retry.max_delay_seconds": _env_float("KAMANDAL_SHEETS_RETRY_MAX_DELAY_SECONDS"),
        "broker.active": os.environ.get("KAMANDAL_BROKER"),
        "broker.market_metrics_provider": os.environ.get("KAMANDAL_MARKET_METRICS_PROVIDER"),
        "broker.public.secret_token": os.environ.get("PUBLIC_SECRET_TOKEN"),
        "broker.public.account_id": os.environ.get("PUBLIC_ACCOUNT_ID"),
        "broker.public.api_base_url": os.environ.get("PUBLIC_API_BASE_URL"),
        "broker.public.auth_endpoint": os.environ.get("PUBLIC_AUTH_ENDPOINT"),
        "broker.public.session_file": os.environ.get("PUBLIC_SESSION_FILE"),
        "broker.public.account_cache_file": os.environ.get("PUBLIC_ACCOUNT_CACHE_FILE"),
        "broker.public.api_requests_per_second": os.environ.get("API_REQUESTS_PER_SECOND"),
        "broker.public.api_burst_limit": os.environ.get("API_BURST_LIMIT"),
        "broker.tastytrade.client_id": os.environ.get("TASTYTRADE_CLIENT_ID"),
        "broker.tastytrade.client_secret": os.environ.get("TASTYTRADE_CLIENT_SECRET"),
        "broker.tastytrade.refresh_token": os.environ.get("TASTYTRADE_REFRESH_TOKEN"),
        "broker.tastytrade.account_number": os.environ.get("TASTYTRADE_ACCOUNT_NUMBER"),
        "broker.tastytrade.api_base_url": os.environ.get("TASTYTRADE_API_BASE_URL"),
        "broker.tastytrade.api_version": os.environ.get("TASTYTRADE_API_VERSION"),
        "broker.tastytrade.oauth_scopes": os.environ.get("TASTYTRADE_OAUTH_SCOPES"),
        "broker.tastytrade.session_file": os.environ.get("TASTYTRADE_SESSION_FILE"),
        "broker.tastytrade.account_cache_file": os.environ.get("TASTYTRADE_ACCOUNT_CACHE_FILE"),
        "llm.provider": os.environ.get("KAMANDAL_LLM_PROVIDER"),
        "llm.model": os.environ.get("KAMANDAL_LLM_MODEL"),
        "llm.codex_binary": os.environ.get("KAMANDAL_CODEX_BINARY"),
        "llm.codex_workdir": os.environ.get("KAMANDAL_CODEX_WORKDIR"),
        "llm.codex_timeout_seconds": _env_int("KAMANDAL_CODEX_TIMEOUT_SECONDS"),
        "source_intelligence.x_bookmarks.latest_state_file": os.environ.get("KAMANDAL_X_BOOKMARK_LATEST_STATE"),
        "source_intelligence.x_bookmarks.trial_root": os.environ.get("KAMANDAL_X_BOOKMARK_TRIAL_ROOT"),
        "source_intelligence.x_digest.db_path": os.environ.get("KAMANDAL_X_DIGEST_DB_PATH"),
        "source_intelligence.x_digest.latest_state_file": os.environ.get("KAMANDAL_X_DIGEST_LATEST_STATE"),
        "source_intelligence.x_digest.trial_root": os.environ.get("KAMANDAL_X_DIGEST_TRIAL_ROOT"),
        "source_intelligence.x_digest.sources": os.environ.get("KAMANDAL_X_DIGEST_SOURCES"),
    }
    for key, value in overrides.items():
        if value is not None:
            _set_nested(config, key, value)
    return config


def google_credentials_path(config: dict[str, Any]) -> Path:
    raw = (config.get("google_sheets") or {}).get("credentials_file") or ""
    if not raw:
        raise RuntimeError("GOOGLE_API_CREDENTIALS_PATH is not configured")
    path = resolve_path(str(raw))
    if not path.exists():
        raise FileNotFoundError(f"Google credentials file not found: {path}")
    return path


def spreadsheet_id(config: dict[str, Any]) -> str:
    raw = (config.get("google_sheets") or {}).get("spreadsheet_id") or ""
    if not raw:
        raise RuntimeError("KAMANDAL_SHEET_ID is not configured")
    return extract_spreadsheet_id(str(raw))


def extract_spreadsheet_id(raw: str) -> str:
    marker = "/spreadsheets/d/"
    if marker not in raw:
        return raw.strip()
    return raw.split(marker, 1)[1].split("/", 1)[0].strip()

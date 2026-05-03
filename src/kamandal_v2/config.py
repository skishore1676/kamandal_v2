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
        "shadow.account_size_override": _env_float("KAMANDAL_SHADOW_ACCOUNT_SIZE"),
        "shadow.buying_power_override": _env_float("KAMANDAL_SHADOW_BUYING_POWER"),
        "shadow.bpr_used_override": _env_float("KAMANDAL_SHADOW_BPR_USED"),
        "shadow.candidate_filter_mode": os.environ.get("KAMANDAL_SHADOW_CANDIDATE_FILTER_MODE"),
        "execution.approval_mode": os.environ.get("KAMANDAL_APPROVAL_MODE"),
        "google_sheets.spreadsheet_id": os.environ.get("KAMANDAL_SHEET_ID"),
        "google_sheets.credentials_file": os.environ.get("GOOGLE_API_CREDENTIALS_PATH"),
        "broker.public.secret_token": os.environ.get("PUBLIC_SECRET_TOKEN"),
        "broker.public.account_id": os.environ.get("PUBLIC_ACCOUNT_ID"),
        "broker.public.api_base_url": os.environ.get("PUBLIC_API_BASE_URL"),
        "broker.public.auth_endpoint": os.environ.get("PUBLIC_AUTH_ENDPOINT"),
        "broker.public.session_file": os.environ.get("PUBLIC_SESSION_FILE"),
        "broker.public.account_cache_file": os.environ.get("PUBLIC_ACCOUNT_CACHE_FILE"),
        "broker.public.api_requests_per_second": os.environ.get("API_REQUESTS_PER_SECOND"),
        "broker.public.api_burst_limit": os.environ.get("API_BURST_LIMIT"),
        "llm.provider": os.environ.get("KAMANDAL_LLM_PROVIDER"),
        "llm.model": os.environ.get("KAMANDAL_LLM_MODEL"),
        "llm.codex_binary": os.environ.get("KAMANDAL_CODEX_BINARY"),
        "llm.codex_workdir": os.environ.get("KAMANDAL_CODEX_WORKDIR"),
        "llm.codex_timeout_seconds": _env_int("KAMANDAL_CODEX_TIMEOUT_SECONDS"),
        "source_intelligence.x_bookmarks.latest_state_file": os.environ.get("KAMANDAL_X_BOOKMARK_LATEST_STATE"),
        "source_intelligence.x_bookmarks.trial_root": os.environ.get("KAMANDAL_X_BOOKMARK_TRIAL_ROOT"),
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

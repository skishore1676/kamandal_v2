#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export PYTHONUNBUFFERED=1
export KAMANDAL_MARKET_TZ="${KAMANDAL_MARKET_TZ:-America/Chicago}"

if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.env"
  set +a
fi

KAMANDAL_BIN="${KAMANDAL_BIN:-$REPO_ROOT/.venv/bin/kamandal}"

log() {
  printf '[%s] %s\n' "$(TZ="$KAMANDAL_MARKET_TZ" date '+%Y-%m-%d %H:%M:%S %Z')" "$*"
}

market_dow() {
  TZ="$KAMANDAL_MARKET_TZ" date '+%u'
}

market_hhmm() {
  TZ="$KAMANDAL_MARKET_TZ" date '+%H%M'
}

force_run_enabled() {
  case "${KAMANDAL_FORCE_RUN:-${KAMANDAL_FORCE_LOOP:-}}" in
    1|true|TRUE|yes|YES|on|ON)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

require_trading_day() {
  if force_run_enabled; then
    log "KAMANDAL_FORCE_RUN enabled; bypassing trading-day guard."
    return 0
  fi
  local dow
  dow="$(market_dow)"
  if (( dow > 5 )); then
    log "Outside trading days; exiting."
    exit 0
  fi
}

require_market_window() {
  if force_run_enabled; then
    log "KAMANDAL_FORCE_RUN enabled; bypassing market-hours guard."
    return 0
  fi
  local now start end
  now=$((10#$(market_hhmm)))
  start=$((10#${KAMANDAL_MARKET_START_HHMM:-0830}))
  end=$((10#${KAMANDAL_MARKET_END_HHMM:-1515}))
  if (( now < start || now > end )); then
    log "Outside market window ${start}-${end}; exiting."
    exit 0
  fi
}

with_lock() {
  local name="$1"
  shift
  local lockroot="$REPO_ROOT/data/runlocks"
  local lockdir="$lockroot/$name.lock"
  mkdir -p "$lockroot"
  if ! mkdir "$lockdir" 2>/dev/null; then
    log "Lock exists for $name; exiting."
    exit 0
  fi
  trap 'rmdir "$lockdir"' EXIT
  "$@"
}

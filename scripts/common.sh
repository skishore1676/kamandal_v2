#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export PYTHONUNBUFFERED=1
export KAMANDAL_MARKET_TZ="${KAMANDAL_MARKET_TZ:-America/Chicago}"
export PATH="${KAMANDAL_EXTRA_PATH:-/usr/local/bin:/usr/local/opt/node@22/bin:/usr/local/Cellar/node@22/22.22.0_1/bin:/opt/homebrew/bin:$HOME/.nvm/versions/node/v22.22.0/bin:$HOME/.nvm/versions/node/v20.20.0/bin}:$PATH"

if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.env"
  set +a
fi

KAMANDAL_BIN="${KAMANDAL_BIN:-$REPO_ROOT/.venv/bin/kamandal}"
KAMANDAL_PYTHON="${KAMANDAL_PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$KAMANDAL_PYTHON" ]]; then
  KAMANDAL_PYTHON="$(command -v python3)"
fi

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
  if market_holiday; then
    log "Market holiday; exiting."
    exit 0
  fi
}

market_holiday() {
  if [[ "${KAMANDAL_MARKET_HOLIDAY_CALENDAR:-nyse}" == "off" ]]; then
    return 1
  fi
  local today
  today="$(TZ="$KAMANDAL_MARKET_TZ" date '+%Y-%m-%d')"
  case "$today" in
    2026-01-01|2026-01-19|2026-02-16|2026-04-03|2026-05-25|2026-06-19|2026-07-03|2026-09-07|2026-11-26|2026-12-25|\
    2027-01-01|2027-01-18|2027-02-15|2027-03-26|2027-05-31|2027-06-18|2027-07-05|2027-09-06|2027-11-25|2027-12-24)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
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
  trap "rmdir '$lockdir'" EXIT
  "$@"
}

prepare_current_ideas_dir() {
  local source_dir="$1"
  local lane="$2"
  CURRENT_IDEAS_DIR="$source_dir"
  if [[ "${KAMANDAL_FILTER_ACTIVE_IDEAS_TODAY:-true}" != "true" ]]; then
    return 0
  fi

  local today dest current_root
  today="$(TZ="$KAMANDAL_MARKET_TZ" date '+%Y-%m-%d')"
  current_root="${KAMANDAL_CURRENT_IDEAS_ROOT:-$REPO_ROOT/data/run/current_ideas}"
  dest="$current_root/$lane"
  rm -rf "$dest"
  mkdir -p "$dest"

  local file target
  local -i kept=0
  while IFS= read -r -d '' file; do
    target="$(cd "$(dirname "$file")" && pwd)/$(basename "$file")"
    ln -s "$target" "$dest/$(basename "$file")"
    kept+=1
  done < <(
    find "$source_dir" -maxdepth 1 -type f \( \
      -name "*$today*.yaml" -o \
      -name "*$today*.yml" -o \
      -name "*$today*.json" -o \
      -name "correspondent_*.yaml" -o \
      -name "correspondent_*.yml" -o \
      -name "correspondent_*.json" \
    \) -print0 2>/dev/null
  )

  CURRENT_IDEAS_DIR="$dest"
  log "Filtered active ideas to current day: source=$source_dir current=$dest kept=$kept date=$today."
}

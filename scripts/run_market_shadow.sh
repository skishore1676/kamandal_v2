#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_market_shadow() {
  require_trading_day
  require_market_window
  export KAMANDAL_MODE=shadow

  local ideas_dir provider
  ideas_dir="${KAMANDAL_ACTIVE_IDEAS_DIR:-data/ideas/active}"
  prepare_current_ideas_dir "$ideas_dir" market_shadow
  ideas_dir="$CURRENT_IDEAS_DIR"
  provider="${KAMANDAL_MARKET_PROVIDER:-public}"

  mkdir -p "$ideas_dir" data/logs
  if ! find "$ideas_dir" -maxdepth 1 \( -name '*.yaml' -o -name '*.yml' -o -name '*.json' \) | grep -q .; then
    log "No current-day active idea files in $ideas_dir; exiting without sheet/Public work."
    exit 0
  fi

  log "Validating sheet config."
  "$KAMANDAL_BIN" validate-config --config-source sheet

  log "Managing open shadow positions before planning."
  "$KAMANDAL_BIN" manage-shadow-positions --config-source sheet

  local write_args
  write_args=()
  if [[ "${KAMANDAL_MARKET_WRITE_SHEET:-true}" == "true" ]]; then
    write_args+=(--write-sheet)
  fi

  log "Running shadow cycle provider=$provider approval_mode=${KAMANDAL_APPROVAL_MODE:-config/control.yaml} ideas=$ideas_dir write_sheet=${KAMANDAL_MARKET_WRITE_SHEET:-true}."
  "$KAMANDAL_BIN" run-shadow-cycle \
    --ideas "$ideas_dir" \
    --config-source sheet \
    --provider "$provider" \
    "${write_args[@]}"
}

with_lock market_shadow run_market_shadow

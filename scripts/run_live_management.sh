#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_live_management() {
  require_trading_day
  require_market_window
  export KAMANDAL_MODE=live
  log "Running live close advisory and evaluating approved close orders."
  "$KAMANDAL_BIN" live-management-plan --config-source sheet --write-sheet
  local submit_args=()
  if [[ "${KAMANDAL_LIVE_SUBMIT:-0}" == "1" ]]; then
    submit_args+=(--submit)
  fi
  "$KAMANDAL_BIN" execute-live-approved-closes "${submit_args[@]}"
  "$KAMANDAL_BIN" sync-live-orders
}

with_lock live_management run_live_management

#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_live_approved_orders() {
  require_trading_day
  require_market_window
  export KAMANDAL_MODE=live
  local submit_args=()
  if [[ "${KAMANDAL_LIVE_SUBMIT:-0}" == "1" ]]; then
    submit_args+=(--submit)
  fi
  log "Evaluating sheet-approved live open orders submit=${KAMANDAL_LIVE_SUBMIT:-0}."
  "$KAMANDAL_BIN" execute-live-approved "${submit_args[@]}"
  log "Syncing live order status."
  "$KAMANDAL_BIN" sync-live-orders
}

with_lock live_approved_orders run_live_approved_orders

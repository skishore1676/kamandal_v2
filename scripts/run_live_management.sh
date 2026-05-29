#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_live_management() {
  require_trading_day
  require_market_window
  export KAMANDAL_MODE=live
  log "Syncing and reconciling live positions before close management."
  "$KAMANDAL_BIN" sync-live-orders
  "$KAMANDAL_BIN" reconcile-live-positions --write-sheet --send-review
  log "Running live close advisory and evaluating approved close orders."
  "$KAMANDAL_BIN" live-management-plan --config-source sheet --write-sheet
  local submit_args=()
  submit_args+=(--submit-auto)
  local result_file
  result_file="$(mktemp)"
  "$KAMANDAL_BIN" execute-live-approved-closes ${submit_args+"${submit_args[@]}"} | tee "$result_file"
  local message
  message="$(notify_live_execution_result "exit" "$result_file" || true)"
  rm -f "$result_file"
  if [[ -n "$message" ]]; then
    send_telegram_receipt "$message"
  fi
  "$KAMANDAL_BIN" sync-live-orders
  "$KAMANDAL_BIN" cleanup-live-approvals
}

with_lock live_management run_live_management

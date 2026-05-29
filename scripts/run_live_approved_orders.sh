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
  submit_args+=(--submit-auto)
  log "Evaluating sheet-approved live open orders submit=${KAMANDAL_LIVE_SUBMIT:-0}."
  local result_file
  result_file="$(mktemp)"
  "$KAMANDAL_BIN" execute-live-approved ${submit_args+"${submit_args[@]}"} | tee "$result_file"
  local message
  message="$(notify_live_execution_result "entry" "$result_file" || true)"
  rm -f "$result_file"
  if [[ -n "$message" ]]; then
    send_telegram_receipt "$message"
  fi
  log "Syncing live order status."
  "$KAMANDAL_BIN" sync-live-orders
  log "Cleaning stale live approval cells."
  "$KAMANDAL_BIN" cleanup-live-approvals
}

with_lock live_approved_orders run_live_approved_orders

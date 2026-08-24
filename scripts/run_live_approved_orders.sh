#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_live_approved_orders() {
  require_trading_day
  require_market_window
  export KAMANDAL_MODE=live
  local ideas_dir="${KAMANDAL_ACTIVE_IDEAS_DIR:-data/ideas/active}"
  prepare_current_ideas_dir "$ideas_dir" live_entry_recovery
  ideas_dir="$CURRENT_IDEAS_DIR"
  local submit_args=()
  submit_args+=(--submit-auto)
  # This runner owns open submissions only.  Poll current broker status as a
  # fail-closed entry precondition, but leave repricing, expiry, close recovery,
  # and approval cleanup to the unified lifecycle cycle.
  log "Refreshing live order status before evaluating staged open approvals."
  "$KAMANDAL_BIN" sync-live-orders --read-only
  log "Evaluating sheet-approved live open orders submit=${KAMANDAL_LIVE_SUBMIT:-0}."
  "$KAMANDAL_BIN" execute-live-approved \
    ${submit_args+"${submit_args[@]}"} \
    --recover-stale-selected \
    --recovery-ideas "$ideas_dir" \
    --recovery-config-source sheet \
    --recovery-provider public
}

with_lock live_approved_orders run_live_approved_orders

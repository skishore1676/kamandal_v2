#!/bin/bash
set -euo pipefail

echo "retired: live-management has no independent owner; use run_unified_lifecycle_management.sh" >&2
exit 64

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
  "$KAMANDAL_BIN" execute-live-approved-closes ${submit_args+"${submit_args[@]}"}
  "$KAMANDAL_BIN" sync-live-orders
  "$KAMANDAL_BIN" cleanup-live-approvals
}

with_lock live_management run_live_management

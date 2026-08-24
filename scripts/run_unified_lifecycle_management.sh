#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_unified_lifecycle_management() {
  require_trading_day
  require_market_window
  local database="${KAMANDAL_CSA_DB:-data/kamandal_v2.db}"
  local provider="${KAMANDAL_MARKET_PROVIDER:-public}"
  local cycle_status=0
  local step_status=0

  log "Syncing broker orders before live lifecycle management."
  if "$KAMANDAL_BIN" sync-live-orders; then
    log "Evaluating live lifecycles before guarded close and adjustment execution."
    "$KAMANDAL_BIN" unified-lifecycle-management \
      --db "$database" \
      --provider "$provider" \
      --branch live || cycle_status=$?

    # Complete live effects even when one lifecycle reported an isolated error;
    # successful lifecycles may already have staged required management tickets.
    "$KAMANDAL_BIN" execute-live-approved-closes --submit-auto || cycle_status=$?

    log "Syncing broker orders after live lifecycle execution."
    "$KAMANDAL_BIN" sync-live-orders || cycle_status=$?
    "$KAMANDAL_BIN" cleanup-live-approvals || cycle_status=$?
  else
    step_status=$?
    cycle_status=$step_status
    log "Skipping live lifecycle effects because broker-order synchronization failed."
  fi

  # Shadow is broker-inert and comes after the complete live effect cycle.  Its
  # failure is recorded without being able to suppress live close execution.
  log "Evaluating broker-inert shadow lifecycles."
  "$KAMANDAL_BIN" unified-lifecycle-management \
    --db "$database" \
    --provider "$provider" \
    --branch shadow || cycle_status=$?

  return "$cycle_status"
}

with_lock unified_lifecycle_management run_unified_lifecycle_management

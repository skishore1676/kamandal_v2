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
  local live_status=0
  local pre_sync_status=0
  local lifecycle_status=-1
  local close_status=-1
  local post_sync_status=-1
  local cleanup_status=-1
  local shadow_status=0

  log "Syncing broker orders before live lifecycle management."
  if "$KAMANDAL_BIN" sync-live-orders; then
    log "Evaluating live lifecycles before guarded close and adjustment execution."
    "$KAMANDAL_BIN" unified-lifecycle-management \
      --db "$database" \
      --provider "$provider" \
      --branch live || lifecycle_status=$?
    if (( lifecycle_status < 0 )); then
      lifecycle_status=0
    elif (( live_status == 0 )); then
      live_status=$lifecycle_status
    fi

    # Complete live effects even when one lifecycle reported an isolated error;
    # successful lifecycles may already have staged required management tickets.
    "$KAMANDAL_BIN" execute-live-approved-closes --submit-auto || close_status=$?
    if (( close_status < 0 )); then
      close_status=0
    elif (( live_status == 0 )); then
      live_status=$close_status
    fi

    log "Syncing broker orders after live lifecycle execution."
    "$KAMANDAL_BIN" sync-live-orders || post_sync_status=$?
    if (( post_sync_status < 0 )); then
      post_sync_status=0
    elif (( live_status == 0 )); then
      live_status=$post_sync_status
    fi
    "$KAMANDAL_BIN" cleanup-live-approvals || cleanup_status=$?
    if (( cleanup_status < 0 )); then
      cleanup_status=0
    elif (( live_status == 0 )); then
      live_status=$cleanup_status
    fi
  else
    pre_sync_status=$?
    live_status=$pre_sync_status
    log "Skipping live lifecycle effects because broker-order synchronization failed."
  fi

  # Shadow is broker-inert and comes after the complete live effect cycle.  Its
  # failure is recorded without being able to suppress live close execution.
  log "Evaluating broker-inert shadow lifecycles."
  "$KAMANDAL_BIN" unified-lifecycle-management \
    --db "$database" \
    --provider "$provider" \
    --branch shadow || shadow_status=$?

  printf 'KAMANDAL_LIFECYCLE_TERMINAL={"status":"%s","exit_code":%d,"steps":{"pre_sync":%d,"live_lifecycle":%d,"execute_live_closes":%d,"post_sync":%d,"cleanup_live_approvals":%d,"shadow_lifecycle":%d}}\n' \
    "$([[ "$live_status" -eq 0 ]] && printf succeeded || printf failed)" \
    "$live_status" "$pre_sync_status" "$lifecycle_status" "$close_status" \
    "$post_sync_status" "$cleanup_status" "$shadow_status"
  return "$live_status"
}

with_lock unified_lifecycle_management run_unified_lifecycle_management

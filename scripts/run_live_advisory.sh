#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_live_advisory() {
  require_trading_day
  export KAMANDAL_MODE=live
  local ideas_dir="${KAMANDAL_ACTIVE_IDEAS_DIR:-data/ideas/active}"
  prepare_current_ideas_dir "$ideas_dir" live_advisory
  ideas_dir="$CURRENT_IDEAS_DIR"
  if ! find "$ideas_dir" -maxdepth 1 \( -name '*.yaml' -o -name '*.yml' -o -name '*.json' \) | grep -q .; then
    log "No current-day active idea files in $ideas_dir; exiting without sheet/Public work."
    exit 0
  fi
  log "Reconciling live positions before advisory planning."
  "$KAMANDAL_BIN" sync-live-orders
  "$KAMANDAL_BIN" reconcile-live-positions --write-sheet --send-review
  log "Running live advisory provider=public ideas=$ideas_dir write_sheet=true."
  "$KAMANDAL_BIN" live-advisory-plan \
    --ideas "$ideas_dir" \
    --config-source sheet \
    --provider public \
    --write-sheet
}

with_lock live_advisory run_live_advisory

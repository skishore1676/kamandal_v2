#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_live_reconciliation() {
  require_trading_day
  export KAMANDAL_MODE=live
  log "Reconciling broker live positions against Kamandal live ledger."
  "$KAMANDAL_BIN" sync-live-orders
  "$KAMANDAL_BIN" reconcile-live-positions --write-sheet --send-review
}

with_lock live_reconciliation run_live_reconciliation

#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_unified_lifecycle_management() {
  require_trading_day
  require_market_window
  "$KAMANDAL_BIN" sync-live-orders
  "$KAMANDAL_BIN" reconcile-live-positions --write-sheet --send-review
  "$KAMANDAL_BIN" live-management-plan --config-source sheet --write-sheet
  "$KAMANDAL_BIN" csa-live-management --db "${KAMANDAL_CSA_DB:-data/kamandal_v2.db}" --provider "${KAMANDAL_MARKET_PROVIDER:-public}"
  "$KAMANDAL_BIN" csa-shadow-management --db "${KAMANDAL_CSA_DB:-data/kamandal_v2.db}" --provider "${KAMANDAL_MARKET_PROVIDER:-public}"
  "$KAMANDAL_BIN" execute-live-approved-closes --submit-auto
  "$KAMANDAL_BIN" sync-live-orders
  "$KAMANDAL_BIN" cleanup-live-approvals
}

with_lock unified_lifecycle_management run_unified_lifecycle_management

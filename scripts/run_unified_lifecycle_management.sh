#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_unified_lifecycle_management() {
  require_trading_day
  require_market_window
  "$KAMANDAL_BIN" unified-lifecycle-management \
    --db "${KAMANDAL_CSA_DB:-data/kamandal_v2.db}" \
    --provider "${KAMANDAL_MARKET_PROVIDER:-public}"
  "$KAMANDAL_BIN" execute-live-approved-closes --submit-auto
}

with_lock unified_lifecycle_management run_unified_lifecycle_management

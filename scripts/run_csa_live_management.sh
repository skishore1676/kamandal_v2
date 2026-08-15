#!/bin/bash
set -euo pipefail

echo "retired: csa-live-management has no independent owner; use run_unified_lifecycle_management.sh" >&2
exit 64

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_csa_live_management() {
  require_trading_day
  require_market_window
  mkdir -p data/logs data/reports/csa1
  "$KAMANDAL_BIN" csa-live-management \
    --db "${KAMANDAL_CSA_DB:-data/kamandal_v2.db}" \
    --provider "${KAMANDAL_MARKET_PROVIDER:-public}"
}

with_lock csa_live_management run_csa_live_management

#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_csa_shadow_management() {
  require_trading_day
  require_market_window
  mkdir -p data/logs data/reports/csa1
  "$KAMANDAL_BIN" csa-shadow-management \
    --db "${KAMANDAL_CSA_DB:-data/kamandal_v2.db}" \
    --provider "${KAMANDAL_MARKET_PROVIDER:-public}"
}

with_lock csa_shadow_management run_csa_shadow_management

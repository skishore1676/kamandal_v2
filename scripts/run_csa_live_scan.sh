#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_csa_live_scan() {
  require_trading_day
  require_market_window
  mkdir -p data/logs data/reports/csa1
  "$KAMANDAL_BIN" csa-validate-policy
  "$KAMANDAL_BIN" csa-live-scan \
    --db "${KAMANDAL_CSA_DB:-data/kamandal_v2.db}" \
    --provider "${KAMANDAL_MARKET_PROVIDER:-public}" \
    --ideas "${KAMANDAL_ACTIVE_IDEAS_DIR:-data/ideas/active}"
}

with_lock csa_live_scan run_csa_live_scan

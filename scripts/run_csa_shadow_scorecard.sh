#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_csa_shadow_scorecard() {
  require_trading_day
  mkdir -p data/logs data/reports/csa1
  "$KAMANDAL_BIN" csa-shadow-scorecard \
    --db "${KAMANDAL_CSA_DB:-data/kamandal_v2.db}" \
    --output-dir "${KAMANDAL_CSA_REPORT_DIR:-data/reports/csa1}"
}

with_lock csa_shadow_scorecard run_csa_shadow_scorecard

#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_unified_planning() {
  require_trading_day
  require_market_window
  local ideas_dir="${KAMANDAL_ACTIVE_IDEAS_DIR:-data/ideas/active}"
  prepare_current_ideas_dir "$ideas_dir" unified_planning
  "$KAMANDAL_BIN" unified-plan \
    --db "${KAMANDAL_CSA_DB:-data/kamandal_v2.db}" \
    --provider "${KAMANDAL_MARKET_PROVIDER:-public}" \
    --ideas "$CURRENT_IDEAS_DIR" \
    --config-source sheet \
    --write-sheet
}

with_lock unified_planning run_unified_planning

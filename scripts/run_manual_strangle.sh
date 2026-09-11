#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"
if [[ $# -ne 1 || ! "$1" =~ ^[A-Za-z][A-Za-z0-9.]{0,9}$ ]]; then
  echo "Usage: $0 TICKER (imports today's Strangle row, plans it, then requests guarded execution)" >&2
  exit 2
fi
manual_symbol="$1"
run_manual_strangle() {
  require_trading_day
  require_market_window
  "$KAMANDAL_BIN" unified-plan \
    --db "${KAMANDAL_CSA_DB:-data/kamandal_v2.db}" \
    --provider "${KAMANDAL_MARKET_PROVIDER:-public}" \
    --config-source sheet --manual-strangle "$manual_symbol" --write-sheet || return $?
  # A failed or empty plan exits above. Hold the normal planning lock until
  # execution reads this selection, preventing a scheduled plan from replacing it.
  "$SCRIPT_DIR/run_live_approved_orders.sh"
}
with_lock unified_planning run_manual_strangle

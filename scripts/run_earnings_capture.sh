#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_earnings_capture() {
  require_trading_day

  local provider
  provider="${KAMANDAL_EARNINGS_PROVIDER:-yfinance}"
  log "Capturing earnings calendar provider=$provider."
  "$KAMANDAL_BIN" capture-earnings --config-source sheet --provider "$provider"
}

with_lock earnings_capture run_earnings_capture

#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_shadow_eod_report() {
  require_trading_day

  mkdir -p data/logs data/reports/eod
  log "Writing shadow EOD report."
  "$KAMANDAL_BIN" shadow-eod-report --config-source sheet --output-dir data/reports/eod
}

with_lock shadow_eod_report run_shadow_eod_report

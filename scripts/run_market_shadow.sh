#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_market_shadow() {
  require_trading_day
  require_market_window

  local ideas_dir provider
  ideas_dir="${KAMANDAL_ACTIVE_IDEAS_DIR:-data/ideas/active}"
  provider="${KAMANDAL_MARKET_PROVIDER:-public}"

  mkdir -p "$ideas_dir" data/logs

  log "Validating sheet config."
  "$KAMANDAL_BIN" validate-config --config-source sheet

  log "Running shadow cycle provider=$provider approval_mode=${KAMANDAL_APPROVAL_MODE:-config/control.yaml} ideas=$ideas_dir write_sheet=true."
  "$KAMANDAL_BIN" run-shadow-cycle \
    --ideas "$ideas_dir" \
    --config-source sheet \
    --provider "$provider" \
    --write-sheet
}

with_lock market_shadow run_market_shadow

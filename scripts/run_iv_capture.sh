#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_iv_capture() {
  require_trading_day

  local provider config_source
  provider="${KAMANDAL_IV_PROVIDER:-public}"
  config_source="${KAMANDAL_IV_CONFIG_SOURCE:-sheet}"

  mkdir -p data/logs
  log "Capturing Tastytrade-first daily IV metrics with provider=$provider local fallback config_source=$config_source."
  "$KAMANDAL_BIN" capture-iv \
    --config-source "$config_source" \
    --provider "$provider"
}

with_lock iv_capture run_iv_capture

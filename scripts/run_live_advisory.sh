#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_live_advisory() {
  require_trading_day
  export KAMANDAL_MODE=live
  local ideas_dir="${KAMANDAL_ACTIVE_IDEAS_DIR:-data/ideas/active}"
  log "Running live advisory provider=public ideas=$ideas_dir write_sheet=true."
  local result_file
  result_file="$(mktemp)"
  
  if "$KAMANDAL_BIN" live-advisory-plan \
    --ideas "$ideas_dir" \
    --config-source sheet \
    --provider public \
    --write-sheet > "$result_file" 2>&1; then
    
    log "live-advisory-plan generated. Requesting kamandal_ops review..."
    if ! timeout 60 openclaw run kamandal_ops "Review the latest live advisory plan. Output: $(cat "$result_file")" > /dev/null 2>&1; then
      log "kamandal_ops timeout or failure. Falling back to mechanical telegram."
      send_telegram "Live Advisory Fallback: $(cat "$result_file" | tail -n 20)"
    fi
  else
    log "live-advisory-plan failed."
    send_telegram "Live Advisory Failed: $(cat "$result_file" | tail -n 20)"
  fi
  rm -f "$result_file"
}

with_lock live_advisory run_live_advisory

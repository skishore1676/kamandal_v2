#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_universe_proposer() {
  require_trading_day
  log "Proposing universe symbols from recent out-of-universe plan diagnostics (max 5/day)."
  result="$("$KAMANDAL_BIN" propose-universe-symbols --limit 5 --lookback-days 3 --write-sheet 2>&1)"
  log "$result"
  # Lathi surfaces failures via scheduled_job_health; proposer itself is best-effort
}

with_lock universe_proposer run_universe_proposer

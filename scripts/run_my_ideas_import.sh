#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

run_my_ideas_import() {
  require_trading_day
  log "Importing operator ideas from my_ideas sheet tab."
  "$KAMANDAL_BIN" import-my-ideas --bootstrap
}

with_lock my_ideas_import run_my_ideas_import

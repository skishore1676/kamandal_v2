#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_weekly_reviewer() {
  require_trading_day

  local today ideas_dir ideas_arg
  today="$(TZ="$KAMANDAL_MARKET_TZ" date '+%Y-%m-%d')"
  ideas_dir="${KAMANDAL_ACTIVE_IDEAS_DIR:-data/ideas/active}"
  ideas_arg=""
  if [[ -f "$ideas_dir/llm_imported_$today.yaml" ]]; then
    ideas_arg="--ideas $ideas_dir/llm_imported_$today.yaml"
  fi

  if [[ ! -f data/audit/latest_plan_run.json ]]; then
    log "No latest plan run to review; exiting."
    exit 0
  fi

  log "Running weekly rejection reviewer."
  # shellcheck disable=SC2086
  "$KAMANDAL_BIN" review-rejections \
    --latest-run data/audit/latest_plan_run.json \
    $ideas_arg \
    --output-dir "data/reviews/weekly/$today"
}

with_lock weekly_reviewer run_weekly_reviewer

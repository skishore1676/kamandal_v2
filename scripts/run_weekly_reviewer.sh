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

  if [[ -f data/audit/latest_plan_run.json ]]; then
    log "Running weekly rejection reviewer."
    # LLM review is advisory. Its failure must not erase the independent,
    # deterministic Friday discovery review and boundary commit.
    # shellcheck disable=SC2086
    if ! "$KAMANDAL_BIN" review-rejections \
      --latest-run data/audit/latest_plan_run.json \
      $ideas_arg \
      --output-dir "data/reviews/weekly/$today"; then
      log "Weekly rejection reviewer failed; continuing with universe review."
    fi
  else
    log "No latest plan run for LLM rejection review; continuing with universe review."
  fi

  log "Running bounded Friday universe aggregation and append/readback review."
  "$KAMANDAL_BIN" review-universe --write-sheet
}

with_lock weekly_reviewer run_weekly_reviewer

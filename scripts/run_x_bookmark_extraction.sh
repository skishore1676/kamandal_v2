#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_x_bookmark_extraction() {
  require_trading_day

  local today source_root digest_dir ideas_dir limit import_json source_doc_dir source_doc activation_json
  today="$(TZ="$KAMANDAL_MARKET_TZ" date '+%Y-%m-%d')"
  source_root="${KAMANDAL_X_SOURCE_DOC_DIR:-data/source_docs/x_digest}"
  digest_dir="${KAMANDAL_X_BOOKMARK_DIGEST_DIR:-data/digest/x_bookmarks/$today}"
  ideas_dir="${KAMANDAL_ACTIVE_IDEAS_DIR:-data/ideas/active}"
  limit="${KAMANDAL_X_BOOKMARK_LIMIT:-50}"

  mkdir -p "$source_root" "$digest_dir" "$ideas_dir"

  log "Importing Birdclaw canonical X digest into source docs."
  if import_json="$("$KAMANDAL_BIN" import-x-digest \
      --output-dir "$source_root" \
      --digest-dir "$digest_dir" \
      --limit "${KAMANDAL_X_DIGEST_LIMIT:-$limit}" \
      --since-hours "${KAMANDAL_X_DIGEST_SINCE_HOURS:-96}" \
      --sources "${KAMANDAL_X_DIGEST_SOURCES:-bookmarks,timeline}" \
      --config-source sheet \
      --filter-universe 2>&1)"; then
    log "$import_json"
    source_doc_dir="$("$REPO_ROOT/.venv/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["source_doc_dir"])' <<< "$import_json")"
  else
    log "Canonical X digest import failed; falling back to legacy bookmark export. Output: $import_json"
    source_root="${KAMANDAL_X_BOOKMARK_SOURCE_DOC_DIR:-data/source_docs/x_bookmarks}"
    mkdir -p "$source_root"
    import_json="$("$KAMANDAL_BIN" import-x-bookmarks \
      --output-dir "$source_root" \
      --digest-dir "$digest_dir" \
      --limit "$limit" \
      --config-source sheet \
      --filter-universe)"
    log "$import_json"
    source_doc="$("$REPO_ROOT/.venv/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["source_doc_path"])' <<< "$import_json")"
    source_doc_dir="$(dirname "$source_doc")"
  fi

  if [[ "${KAMANDAL_X_EXTRACTION_IMPORT_ONLY:-0}" != "1" ]]; then
    # Market Cartographer enrichment for correspondent weekly_ideas — autonomous.
    # Generates a seed request from the latest correspondent translation's pending
    # weekly_ideas (chart_evaluation_missing), then evaluates it. No human input.
    # Control surface remains Google Sheet / Telegram / Obsidian via Lathi on failure.
    if [[ "${KAMANDAL_CHART_SEED_ENABLED:-0}" == "1" ]]; then
      chart_output="${KAMANDAL_CHART_SEED_OUTPUT:-data/research/chart_seeds}"
      chart_provider="${KAMANDAL_CHART_SEED_PROVIDER:-mala}"
      chart_request="${KAMANDAL_CHART_SEED_REQUEST:-}"
      # Autonomously generate request if not provided explicitly
      if [[ -z "$chart_request" || ! -f "$chart_request" ]]; then
        generated_request="$("$KAMANDAL_PYTHON" -c "
from kamandal_v2.tools.chart_seed_request import latest_translation_path, build_seed_request_from_translation
from pathlib import Path
import os
output_root = os.environ.get('KAMANDAL_CHART_SEED_OUTPUT', 'data/research/chart_seeds')
request_dir = os.environ.get('KAMANDAL_CHART_SEED_REQUEST_DIR', 'data/research/chart_seeds/requests')
translation = latest_translation_path('data/research/correspondent_signals')
if translation:
    req = build_seed_request_from_translation(translation, output_dir=request_dir, max_symbols=8)
    print(str(req) if req else '')
else:
    print('')
" 2>&1)"
        if [[ -n "$generated_request" && -f "$generated_request" ]]; then
          chart_request="$generated_request"
          log "Auto-generated chart seed request: $chart_request"
        else
          log "No pending weekly_ideas requiring chart evaluation; skipping cartographer."
          chart_request=""
        fi
      fi
      chart_bin="${KAMANDAL_MARKET_CARTOGRAPHER_BIN:-$REPO_ROOT/../market-cartographer/.venv/bin/market-cartographer}"
      if [[ ! -x "$chart_bin" ]]; then
        chart_bin="$(command -v market-cartographer 2>/dev/null || true)"
      fi
      if [[ -n "$chart_request" && -f "$chart_request" && -n "$chart_bin" ]]; then
        log "Running Market Cartographer seed evaluation: $chart_request -> $chart_output (provider=$chart_provider)"
        if "$chart_bin" evaluate-seeds --input "$chart_request" --provider "$chart_provider" --output "$chart_output" 2>&1 | while IFS= read -r line; do log "chart: $line"; done; then
          log "Chart seed evaluation succeeded."
        else
          log "Chart seed evaluation failed (non-fatal, Lathi will surface via health)."
        fi
      elif [[ -n "$chart_request" ]]; then
        log "Chart request ready at $chart_request but Market Cartographer is unavailable; leaving the request pending without treating it as an evaluation."
      fi
    fi
    log "Activating configured correspondent signals for the planner."
    activation_json="$("$KAMANDAL_BIN" activate-correspondent-signals \
      --config-source sheet \
      --active-ideas-dir "$ideas_dir")"
    log "$activation_json"
  fi

  if [[ -z "$source_doc_dir" || ! -d "$source_doc_dir" ]]; then
    log "X source doc directory missing: $source_doc_dir"
    exit 1
  fi
  if ! find "$source_doc_dir" -type f \( -name '*.txt' -o -name '*.md' \) -print -quit | grep -q .; then
    log "No X source docs produced in $source_doc_dir; skipping extraction."
    exit 0
  fi

  if [[ "${KAMANDAL_X_EXTRACTION_IMPORT_ONLY:-0}" == "1" ]]; then
    log "Import-only smoke mode complete; source docs produced in $source_doc_dir."
    exit 0
  fi

  find "$ideas_dir" -maxdepth 1 -type f -name 'x_bookmarks_imported_*.yaml' ! -name "x_bookmarks_imported_$today.yaml" -delete

  log "Extracting X ideas with Codex LLM from $source_doc_dir."
  "$KAMANDAL_BIN" extract-ideas-llm \
    --source-dir "$source_doc_dir" \
    --digest-dir "$digest_dir/llm" \
    --ideas-dir "$ideas_dir" \
    --output-prefix x_bookmarks_imported \
    --config-source sheet \
    --filter-universe
}

with_lock x_bookmark_extraction run_x_bookmark_extraction

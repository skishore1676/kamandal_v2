#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_x_bookmark_extraction() {
  require_trading_day

  local today source_root digest_dir ideas_dir limit import_json source_doc_dir source_doc
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

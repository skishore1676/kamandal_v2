#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_x_bookmark_extraction() {
  require_trading_day

  local today source_root digest_dir ideas_dir limit import_json source_doc
  today="$(TZ="$KAMANDAL_MARKET_TZ" date '+%Y-%m-%d')"
  source_root="${KAMANDAL_X_BOOKMARK_SOURCE_DOC_DIR:-data/source_docs/x_bookmarks}"
  digest_dir="${KAMANDAL_X_BOOKMARK_DIGEST_DIR:-data/digest/x_bookmarks/$today}"
  ideas_dir="${KAMANDAL_ACTIVE_IDEAS_DIR:-data/ideas/active}"
  limit="${KAMANDAL_X_BOOKMARK_LIMIT:-50}"

  mkdir -p "$source_root" "$digest_dir" "$ideas_dir"

  log "Importing sanitized X bookmarks into source docs."
  import_json="$("$KAMANDAL_BIN" import-x-bookmarks \
    --output-dir "$source_root" \
    --digest-dir "$digest_dir" \
    --limit "$limit" \
    --config-source sheet \
    --filter-universe)"
  log "$import_json"

  source_doc="$("$REPO_ROOT/.venv/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["source_doc_path"])' <<< "$import_json")"
  if [[ -z "$source_doc" || ! -f "$source_doc" ]]; then
    log "X bookmark source doc missing: $source_doc"
    exit 1
  fi

  find "$ideas_dir" -maxdepth 1 -type f -name 'x_bookmarks_imported_*.yaml' ! -name "x_bookmarks_imported_$today.yaml" -delete

  log "Extracting X bookmark ideas with Codex LLM from $(dirname "$source_doc")."
  "$KAMANDAL_BIN" extract-ideas-llm \
    --source-dir "$(dirname "$source_doc")" \
    --digest-dir "$digest_dir/llm" \
    --ideas-dir "$ideas_dir" \
    --output-prefix x_bookmarks_imported \
    --config-source sheet \
    --filter-universe
}

with_lock x_bookmark_extraction run_x_bookmark_extraction

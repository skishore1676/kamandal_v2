#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_youtube_extraction() {
  require_trading_day

  local today transcript_dir digest_dir ideas_dir queue_file languages raw_ids ids
  today="$(TZ="$KAMANDAL_MARKET_TZ" date '+%Y-%m-%d')"
  transcript_dir="${KAMANDAL_YOUTUBE_TRANSCRIPT_DIR:-data/transcripts/youtube/$today}"
  digest_dir="${KAMANDAL_YOUTUBE_DIGEST_DIR:-data/digest/youtube/$today}"
  ideas_dir="${KAMANDAL_ACTIVE_IDEAS_DIR:-data/ideas/active}"
  queue_file="${KAMANDAL_YOUTUBE_QUEUE_FILE:-data/youtube_queue.txt}"
  languages="${KAMANDAL_YOUTUBE_LANGUAGES:-en}"

  mkdir -p "$transcript_dir" "$digest_dir" "$ideas_dir"

  raw_ids="${KAMANDAL_YOUTUBE_VIDEO_IDS:-}"
  if [[ -z "$raw_ids" && -f "$queue_file" ]]; then
    raw_ids="$(grep -Ev '^[[:space:]]*($|#)' "$queue_file" | paste -sd, -)"
  fi
  if [[ -z "$raw_ids" ]]; then
    log "No YouTube videos configured. Set KAMANDAL_YOUTUBE_VIDEO_IDS or $queue_file."
    exit 0
  fi

  IFS=',' read -r -a ids <<< "$raw_ids"
  for raw_id in "${ids[@]}"; do
    local video_id
    video_id="$(printf '%s' "$raw_id" | xargs)"
    if [[ -z "$video_id" ]]; then
      continue
    fi
    log "Fetching YouTube transcript: $video_id"
    "$KAMANDAL_BIN" fetch-youtube-transcript \
      --video-id "$video_id" \
      --transcript-dir "$transcript_dir" \
      --languages "$languages"
  done

  find "$ideas_dir" -maxdepth 1 -type f -name 'llm_imported_*.yaml' ! -name "llm_imported_$today.yaml" -delete

  log "Extracting ideas with Codex LLM from $transcript_dir"
  "$KAMANDAL_BIN" extract-ideas-llm \
    --source-dir "$transcript_dir" \
    --digest-dir "$digest_dir" \
    --ideas-dir "$ideas_dir" \
    --config-source sheet \
    --filter-universe
}

with_lock youtube_extraction run_youtube_extraction

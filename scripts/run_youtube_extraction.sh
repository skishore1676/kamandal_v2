#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_youtube_extraction() {
  require_trading_day

  local today transcript_dir digest_dir ideas_dir queue_file channel_file languages raw_ids ids
  today="$(TZ="$KAMANDAL_MARKET_TZ" date '+%Y-%m-%d')"
  transcript_dir="${KAMANDAL_YOUTUBE_TRANSCRIPT_DIR:-data/transcripts/youtube/$today}"
  digest_dir="${KAMANDAL_YOUTUBE_DIGEST_DIR:-data/digest/youtube/$today}"
  ideas_dir="${KAMANDAL_ACTIVE_IDEAS_DIR:-data/ideas/active}"
  queue_file="${KAMANDAL_YOUTUBE_QUEUE_FILE:-data/youtube_queue.txt}"
  channel_file="${KAMANDAL_YOUTUBE_CHANNEL_FILE:-config/youtube_channels.txt}"
  languages="${KAMANDAL_YOUTUBE_LANGUAGES:-en}"

  mkdir -p "$transcript_dir" "$digest_dir" "$ideas_dir"

  raw_ids="${KAMANDAL_YOUTUBE_VIDEO_IDS:-}"
  if [[ -z "$raw_ids" && -f "$queue_file" ]]; then
    raw_ids="$(grep -Ev '^[[:space:]]*($|#)' "$queue_file" | paste -sd, -)"
  fi
  local channel_ids
  channel_ids="${KAMANDAL_YOUTUBE_CHANNEL_IDS:-}"
  if [[ -z "$channel_ids" && -f "$channel_file" ]]; then
    channel_ids="$(grep -Ev '^[[:space:]]*($|#)' "$channel_file" | paste -sd, -)"
  fi
  if [[ -n "$channel_ids" ]]; then
    local channel_args discovered_file
    channel_args=()
    IFS=',' read -r -a ids <<< "$channel_ids"
    for raw_id in "${ids[@]}"; do
      local channel_id
      channel_id="$(printf '%s' "$raw_id" | xargs)"
      if [[ -n "$channel_id" ]]; then
        channel_args+=(--channel-id "$channel_id")
      fi
    done
    discovered_file="data/youtube_discovered_$today.txt"
    log "Discovering YouTube channel videos: $channel_ids limit=${KAMANDAL_YOUTUBE_CHANNEL_LIMIT:-1}"
    "$KAMANDAL_BIN" list-youtube-channel-videos \
      "${channel_args[@]}" \
      --limit "${KAMANDAL_YOUTUBE_CHANNEL_LIMIT:-1}" \
      --include-keywords "${KAMANDAL_YOUTUBE_INCLUDE_KEYWORDS:-}" \
      --exclude-keywords "${KAMANDAL_YOUTUBE_EXCLUDE_KEYWORDS:-}" \
      --output "$discovered_file"
    if [[ -s "$discovered_file" ]]; then
      raw_ids="${raw_ids:+$raw_ids,}$(paste -sd, - < "$discovered_file")"
    fi
  fi
  if [[ -z "$raw_ids" ]]; then
    log "No YouTube videos configured. Set KAMANDAL_YOUTUBE_VIDEO_IDS, $queue_file, KAMANDAL_YOUTUBE_CHANNEL_IDS, or $channel_file."
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

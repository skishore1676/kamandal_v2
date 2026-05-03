#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL_PREFIX="${KAMANDAL_LAUNCHD_LABEL_PREFIX:-com.kamandal.v2}"
BEGIN_MARKER="# BEGIN KAMANDAL_V2"
END_MARKER="# END KAMANDAL_V2"
CRON_TMP="$(mktemp)"
CRON_NEW="$(mktemp)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

cleanup() {
  rm -f "$CRON_TMP" "$CRON_NEW"
}
trap cleanup EXIT

mkdir -p "$REPO_ROOT/data/logs"

remove_launch_agent() {
  local label="$1"
  local plist="$LAUNCH_AGENTS/$label.plist"
  if [[ -f "$plist" ]]; then
    launchctl unload "$plist" 2>/dev/null || true
    rm -f "$plist"
    echo "removed launch agent $plist"
  fi
}

remove_launch_agent "$LABEL_PREFIX.youtube"
remove_launch_agent "$LABEL_PREFIX.x_bookmarks"
remove_launch_agent "$LABEL_PREFIX.market_shadow"
remove_launch_agent "$LABEL_PREFIX.weekly_reviewer"

crontab -l > "$CRON_TMP" 2>/dev/null || true
awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
  $0 == begin { skip = 1; next }
  $0 == end { skip = 0; next }
  skip != 1 { print }
' "$CRON_TMP" > "$CRON_NEW"

cat >> "$CRON_NEW" <<CRON

$BEGIN_MARKER
55 8 * * 1-5 /usr/bin/caffeinate -i /bin/bash "$REPO_ROOT/scripts/run_x_bookmark_extraction.sh" >> "$REPO_ROOT/data/logs/cron_x_bookmarks.log" 2>&1
15 9 * * 1-5 /usr/bin/caffeinate -i /bin/bash "$REPO_ROOT/scripts/run_youtube_extraction.sh" >> "$REPO_ROOT/data/logs/cron_youtube.log" 2>&1
45 11 * * 1-5 /usr/bin/caffeinate -i /bin/bash "$REPO_ROOT/scripts/run_youtube_extraction.sh" >> "$REPO_ROOT/data/logs/cron_youtube.log" 2>&1
30 14 * * 1-5 /usr/bin/caffeinate -i /bin/bash "$REPO_ROOT/scripts/run_youtube_extraction.sh" >> "$REPO_ROOT/data/logs/cron_youtube.log" 2>&1
0,15,30 8 * * 1-5 /usr/bin/caffeinate -i /bin/bash "$REPO_ROOT/scripts/run_market_shadow.sh" >> "$REPO_ROOT/data/logs/cron_market_shadow.log" 2>&1
*/15 9-15 * * 1-5 /usr/bin/caffeinate -i /bin/bash "$REPO_ROOT/scripts/run_market_shadow.sh" >> "$REPO_ROOT/data/logs/cron_market_shadow.log" 2>&1
45 8 * * 1-5 /usr/bin/caffeinate -i /bin/bash "$REPO_ROOT/scripts/run_iv_capture.sh" >> "$REPO_ROOT/data/logs/cron_iv_capture.log" 2>&1
0 10 * * 5 /usr/bin/caffeinate -i /bin/bash "$REPO_ROOT/scripts/run_weekly_reviewer.sh" >> "$REPO_ROOT/data/logs/cron_weekly_reviewer.log" 2>&1
$END_MARKER
CRON

crontab "$CRON_NEW"
echo "installed Kamandal V2 cron schedule"
crontab -l | sed -n "/$BEGIN_MARKER/,/$END_MARKER/p"

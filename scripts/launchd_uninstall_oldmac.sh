#!/bin/bash
set -euo pipefail

LABEL_PREFIX="${KAMANDAL_LAUNCHD_LABEL_PREFIX:-com.kamandal.v2}"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

remove_launch_agent() {
  local label="$1"
  local plist="$LAUNCH_AGENTS/$label.plist"
  if [[ -f "$plist" ]]; then
    launchctl unload "$plist" 2>/dev/null || true
    rm -f "$plist"
    echo "removed launch agent $plist"
  else
    echo "launch agent not present: $plist"
  fi
}

remove_launch_agent "$LABEL_PREFIX.youtube"
remove_launch_agent "$LABEL_PREFIX.market_shadow"
remove_launch_agent "$LABEL_PREFIX.weekly_reviewer"

launchctl list | grep "$LABEL_PREFIX" || true

#!/bin/bash
set -euo pipefail

# Installs a single weekday launchd job that runs `capture-iv` once per trading
# day in/after market hours, targeting ~15:45 US/Eastern.
#
# NOTE on time zone: launchd StartCalendarInterval fires on the machine's LOCAL
# wall clock. The operator box runs on America/Chicago (Central), like every other
# plist in launchd_install_oldmac.sh, so 15:45 US/Eastern == 14:45 Central. The
# trigger Hour/Minute below are therefore expressed in Central. Override with
# KAMANDAL_IV_AFTERNOON_HOUR / KAMANDAL_IV_AFTERNOON_MINUTE if the host TZ differs.
#
# This writes the plist but DOES NOT load/start it. To activate:
#   launchctl load "$HOME/Library/LaunchAgents/com.kamandal.v2.iv_afternoon.plist"
# To run once immediately for a smoke test (after loading):
#   launchctl start com.kamandal.v2.iv_afternoon

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL_PREFIX="${KAMANDAL_LAUNCHD_LABEL_PREFIX:-com.kamandal.v2}"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
HOUR="${KAMANDAL_IV_AFTERNOON_HOUR:-14}"     # Central; 14:45 CT = 15:45 ET
MINUTE="${KAMANDAL_IV_AFTERNOON_MINUTE:-45}"
mkdir -p "$LAUNCH_AGENTS" "$REPO_ROOT/data/logs"

write_plist() {
  local label="$1"
  local script="$2"
  local schedule_xml="$3"
  local plist="$LAUNCH_AGENTS/$label.plist"
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$REPO_ROOT/scripts/$script</string>
  </array>
$schedule_xml
  <key>StandardOutPath</key>
  <string>$REPO_ROOT/data/logs/$label.out.log</string>
  <key>StandardErrorPath</key>
  <string>$REPO_ROOT/data/logs/$label.err.log</string>
</dict>
</plist>
PLIST
  chmod 644 "$plist"
  echo "wrote $plist (not loaded)"
  echo "to activate: launchctl load \"$plist\""
}

iv_afternoon_schedule="  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
  </array>"

write_plist "$LABEL_PREFIX.iv_afternoon" "run_iv_capture.sh" "$iv_afternoon_schedule"

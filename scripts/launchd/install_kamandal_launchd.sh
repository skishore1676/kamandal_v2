#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${KAMANDAL_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
LABEL_PREFIX="${KAMANDAL_LAUNCHD_LABEL_PREFIX:-com.kamandal.v2}"
LAUNCHD_DIR="${KAMANDAL_LAUNCHD_DIR:-$HOME/Library/LaunchAgents}"
LOG_DIR="${KAMANDAL_LAUNCHD_LOG_DIR:-$REPO_ROOT/data/logs/launchd}"
ACTION="${1:-install}"
BEGIN_MARKER="# BEGIN KAMANDAL_V2"
END_MARKER="# END KAMANDAL_V2"

mkdir -p "$LAUNCHD_DIR" "$LOG_DIR"

write_plists() {
  python3 - "$REPO_ROOT" "$LAUNCHD_DIR" "$LOG_DIR" "$LABEL_PREFIX" <<'PY'
import plistlib
import sys
from pathlib import Path

repo = Path(sys.argv[1])
launchd_dir = Path(sys.argv[2])
log_dir = Path(sys.argv[3])
label_prefix = sys.argv[4]
runner = repo / "scripts" / "launchd" / "run_kamandal_job.sh"


def weekdays(hour, minute):
    return [{"Weekday": day, "Hour": hour, "Minute": minute} for day in range(1, 6)]


def weekly(weekday, hour, minute):
    return [{"Weekday": weekday, "Hour": hour, "Minute": minute}]


def every_minutes(start_hour, start_minute, end_hour, end_minute, step):
    entries = []
    hour, minute = start_hour, start_minute
    while (hour, minute) <= (end_hour, end_minute):
        entries.extend(weekdays(hour, minute))
        minute += step
        while minute >= 60:
            hour += 1
            minute -= 60
    return entries


jobs = [
    ("x_bookmarks", "x-bookmarks", weekdays(8, 55)),
    ("youtube", "youtube", weekdays(9, 15) + weekdays(11, 45) + weekdays(14, 30)),
    ("my_ideas", "my-ideas", weekdays(8, 5) + weekdays(9, 20)),
    ("live_reconciliation", "live-reconciliation", weekdays(8, 35) + weekdays(10, 30) + weekdays(12, 30) + weekdays(14, 30)),
    ("live_advisory", "live-advisory", weekdays(9, 25) + weekdays(11, 55) + weekdays(14, 40)),
    ("live_approved_orders", "live-approved-orders", every_minutes(9, 0, 15, 15, 5)),
    (
        "live_management",
        "live-management",
        every_minutes(9, 0, 14, 45, 15) + weekdays(14, 50) + weekdays(15, 5),
    ),
    ("live_health_report", "live-health-report", weekdays(9, 10) + weekdays(11, 45) + weekdays(14, 45) + weekdays(15, 20)),
    ("scheduled_job_health", "scheduled-job-health", every_minutes(9, 15, 15, 45, 15)),
    ("earnings", "earnings", weekdays(8, 40)),
    ("iv", "iv", weekdays(8, 45)),
    ("iv_afternoon", "iv-afternoon", weekdays(14, 45)),
    ("weekly_reviewer", "weekly-reviewer", weekly(5, 10, 0)),
]

for suffix, job, schedule in jobs:
    label = f"{label_prefix}.{suffix}"
    plist = {
        "Label": label,
        "ProgramArguments": ["/bin/bash", str(runner), job],
        "StartCalendarInterval": schedule,
        "WorkingDirectory": str(repo),
        "StandardOutPath": str(log_dir / f"{label}.out.log"),
        "StandardErrorPath": str(log_dir / f"{label}.err.log"),
    }
    path = launchd_dir / f"{label}.plist"
    path.write_bytes(plistlib.dumps(plist, sort_keys=True))
    path.chmod(0o600)
    print(label)
PY
}

labels() {
  python3 - "$LABEL_PREFIX" <<'PY'
import sys
prefix = sys.argv[1]
for suffix in [
    "x_bookmarks",
    "youtube",
    "my_ideas",
    "live_reconciliation",
    "live_advisory",
    "live_approved_orders",
    "live_management",
    "live_health_report",
    "scheduled_job_health",
    "earnings",
    "iv",
    "iv_afternoon",
    "weekly_reviewer",
]:
    print(f"{prefix}.{suffix}")
PY
}

legacy_labels() {
  python3 - "$LABEL_PREFIX" <<'PY'
import sys
prefix = sys.argv[1]
for suffix in ["market_shadow", "shadow_eod_report"]:
    print(f"{prefix}.{suffix}")
PY
}

remove_kamandal_cron_block() {
  local current new
  current="$(mktemp)"
  new="$(mktemp)"
  trap 'rm -f "$current" "$new"' RETURN
  crontab -l > "$current" 2>/dev/null || true
  awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
    $0 == begin { skip = 1; next }
    $0 == end { skip = 0; next }
    skip != 1 { print }
  ' "$current" > "$new"
  crontab "$new"
  echo "removed Kamandal cron block if present"
}

case "$ACTION" in
  install|"")
    chmod +x "$REPO_ROOT/scripts/launchd/run_kamandal_job.sh"
    write_plists
    uid="$(id -u)"
    while IFS= read -r label; do
      launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
      rm -f "$LAUNCHD_DIR/$label.plist"
    done < <(legacy_labels)
    while IFS= read -r label; do
      plist="$LAUNCHD_DIR/$label.plist"
      launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
      launchctl bootstrap "gui/$uid" "$plist"
      launchctl enable "gui/$uid/$label"
      echo "LOADED $label"
    done < <(labels)
    remove_kamandal_cron_block
    launchctl list | grep "$LABEL_PREFIX" || true
    ;;
  uninstall)
    uid="$(id -u)"
    while IFS= read -r label; do
      launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
      rm -f "$LAUNCHD_DIR/$label.plist"
      echo "UNLOADED $label"
    done < <(labels)
    launchctl list | grep "$LABEL_PREFIX" || true
    ;;
  uninstall-cron)
    remove_kamandal_cron_block
    ;;
  *)
    echo "usage: $0 [install|uninstall|uninstall-cron]" >&2
    exit 2
    ;;
esac

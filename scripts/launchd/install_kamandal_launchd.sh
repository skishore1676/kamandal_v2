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
  local scope="${1:-all}"
  python3 - "$REPO_ROOT" "$LAUNCHD_DIR" "$LOG_DIR" "$LABEL_PREFIX" "$scope" <<'PY'
import plistlib
import sys
from datetime import timedelta
from pathlib import Path

repo = Path(sys.argv[1])
launchd_dir = Path(sys.argv[2])
log_dir = Path(sys.argv[3])
label_prefix = sys.argv[4]
scope = sys.argv[5]
runner = repo / "scripts" / "launchd" / "run_kamandal_job.sh"
sys.path.insert(0, str(repo / "src"))

from kamandal_v2.ops.launchd_registry import DISABLED_BY_DEFAULT, JOB_LABEL_SUFFIXES, JOB_SCHEDULES  # noqa: E402


def calendar_entries(schedule):
    entries = []
    weekdays = [schedule.weekday + 1] if schedule.weekday is not None else range(1, 6)
    for fixed in schedule.fixed_times:
        entries.extend(
            {"Weekday": day, "Hour": fixed.hour, "Minute": fixed.minute}
            for day in weekdays
        )
    if (
        schedule.cadence_minutes is not None
        and schedule.window_start is not None
        and schedule.window_end is not None
    ):
        current = schedule.window_start
        while current <= schedule.window_end:
            entries.extend(
                {"Weekday": day, "Hour": current.hour, "Minute": current.minute}
                for day in weekdays
            )
            stepped = (
                timedelta(hours=current.hour, minutes=current.minute)
                + timedelta(minutes=schedule.cadence_minutes)
            )
            total_minutes = int(stepped.total_seconds() // 60)
            current = current.replace(
                hour=(total_minutes // 60) % 24,
                minute=total_minutes % 60,
            )
    return entries


for job, schedule in JOB_SCHEDULES.items():
    if scope == "unified" and job not in {"unified-planning", "unified-lifecycle-management"}:
        continue
    suffix = JOB_LABEL_SUFFIXES[job]
    label = f"{label_prefix}.{suffix}"
    plist = {
        "Label": label,
        "ProgramArguments": ["/bin/bash", str(runner), job],
        "StartCalendarInterval": calendar_entries(schedule),
        "WorkingDirectory": str(repo),
        "StandardOutPath": str(log_dir / f"{label}.out.log"),
        "StandardErrorPath": str(log_dir / f"{label}.err.log"),
        "Disabled": job in DISABLED_BY_DEFAULT,
    }
    path = launchd_dir / f"{label}.plist"
    path.write_bytes(plistlib.dumps(plist, sort_keys=True))
    path.chmod(0o600)
    print(label)
PY
}

labels() {
  python3 - "$REPO_ROOT" "$LABEL_PREFIX" <<'PY'
import sys
from pathlib import Path

repo = Path(sys.argv[1])
prefix = sys.argv[2]
sys.path.insert(0, str(repo / "src"))

from kamandal_v2.ops.launchd_registry import JOB_LABEL_SUFFIXES  # noqa: E402

for suffix in JOB_LABEL_SUFFIXES.values():
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

retired_owner_labels() {
  python3 - "$LABEL_PREFIX" <<'PY'
import sys
prefix = sys.argv[1]
for suffix in [
    "universe_proposer",
    "live_advisory",
    "live_management",
    "csa_policy_snapshot",
    "csa_shadow_scan",
    "csa_live_scan",
    "csa_shadow_management",
    "csa_live_management",
    "csa_shadow_scorecard",
]:
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
  render)
    write_plists
    ;;
  render-unified)
    write_plists unified
    ;;
  install|install-unified|"")
    chmod +x "$REPO_ROOT/scripts/launchd/run_kamandal_job.sh"
    write_plists
    uid="$(id -u)"
    while IFS= read -r label; do
      launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
      rm -f "$LAUNCHD_DIR/$label.plist"
    done < <(legacy_labels)
    while IFS= read -r label; do
      launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
      rm -f "$LAUNCHD_DIR/$label.plist"
      echo "RETIRED $label"
    done < <(retired_owner_labels)
    while IFS= read -r label; do
      plist="$LAUNCHD_DIR/$label.plist"
      launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
      if /usr/libexec/PlistBuddy -c 'Print :Disabled' "$plist" 2>/dev/null | grep -qi true; then
        launchctl disable "gui/$uid/$label"
        launchctl bootstrap "gui/$uid" "$plist" || true
        echo "LOADED-DISABLED $label"
      else
        launchctl bootstrap "gui/$uid" "$plist"
        launchctl enable "gui/$uid/$label"
        echo "LOADED $label"
      fi
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
    while IFS= read -r label; do
      launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
      rm -f "$LAUNCHD_DIR/$label.plist"
    done < <(retired_owner_labels)
    launchctl list | grep "$LABEL_PREFIX" || true
    ;;
  uninstall-cron)
    remove_kamandal_cron_block
    ;;
  *)
    echo "usage: $0 [render|render-unified|install|install-unified|uninstall|uninstall-cron]" >&2
    exit 2
    ;;
esac

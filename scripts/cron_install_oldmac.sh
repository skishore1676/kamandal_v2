#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "Kamandal now uses launchd for scheduled jobs; installing launchd labels and removing the old Kamandal cron block."
exec "$SCRIPT_DIR/launchd/install_kamandal_launchd.sh" install

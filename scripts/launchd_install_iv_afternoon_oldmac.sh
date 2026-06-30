#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "The afternoon IV job is now managed by the unified Kamandal launchd installer."
exec "$SCRIPT_DIR/launchd/install_kamandal_launchd.sh" install

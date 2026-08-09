#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

run_csa_policy_snapshot() {
  require_trading_day
  mkdir -p data/run/strategy_policy data/logs
  "$KAMANDAL_BIN" csa-policy-snapshot \
    --output-dir "${KAMANDAL_STRATEGY_POLICY_SNAPSHOT_DIR:-data/run/strategy_policy}"
}

with_lock csa_policy_snapshot run_csa_policy_snapshot

# Kamandal Risk Manager

Kamandal's risk manager is a portfolio-level circuit-breaker layer for live
entries. It is the right concept, but it is intentionally not ready to be the
default live authority yet.

## Current state

- The module lives in `src/kamandal_v2/live/risk_manager.py`.
- It is disabled by default in `config/control.yaml`.
- It can be enabled with `KAMANDAL_RISK_MANAGER_ENABLED=true`.
- It only blocks new live entries. It never blocks exits or live management.
- Its decision is included in `kamandal live-health --json`.
- Enabled decisions are recorded as `risk_manager_decision` events when live
  health runs.
- `execute_live_approved` consults the live health gate before submitting new
  entries.

Current checks:

- daily account drawdown breaker;
- weekly account drawdown breaker;
- consecutive losing close cooldown;
- max new position groups per day;
- static correlation cluster caps.
- account snapshot freshness breaker.

Cluster caps are deliberately narrower than full risk blocking: a capped
cluster blocks new entries in that cluster, but does not block unrelated
symbols and never blocks exits.

## Why it stays off for now

The current implementation is a useful guardrail, but several inputs are still
too approximate for always-on live authority:

- Drawdown uses `account_size`, so deposits and withdrawals can distort the
  measured move.
- Consecutive-loss cooldown uses the latest stored position mark for closed
  groups, not a final realized P&L ledger.
- Correlation clusters are static symbol lists. They do not yet understand
  direction, delta, hedge intent, or overlapping index exposure.
- Daily new-position counting uses position-group timestamps rather than an
  explicit market-session ledger.
- Account snapshot freshness is enforced before entry-side risk decisions.
- Daily new-position counting uses the configured market day instead of a raw
  UTC calendar day.
- Live-health records `risk_manager_decision` rows; entry submission records
  `risk_manager_entry_gate_decision` rows.

## Required before switching on

Before setting `KAMANDAL_RISK_MANAGER_ENABLED=true` in live runtime, the
following should be true:

1. Drawdown adjusts for deposits, withdrawals, or other non-market cash moves.
2. Closed-trade streaks use realized close economics, not only latest marks.
3. Cluster caps understand directional exposure well enough to avoid blocking
   legitimate hedges.
4. The first live enablement is done in an explicit observation window with
   `live-health`, `live-approved-orders`, and launchd logs checked after each
   scheduled cycle.

## Health interpretation

Risk-manager signals should be read as entry-side protection:

- `enabled=false`: risk-manager code is deployed but advisory only.
- `blocked=false`: no global entry block is active.
- `risk_cluster_at_cap`: new entries in those symbols should be blocked, but
  unrelated entries and all exits remain allowed. This is self-handled by
  Kamandal and should not page the operator by itself.
- drawdown or loss-cooldown reasons: new entries should be blocked until the
  cooldown or operator reset condition is met.

Kamandal health can still be YELLOW or RED for reasons outside the risk manager,
such as stale close approvals, failed close orders, reconciliation blockers, or
target-reached positions. Those should be triaged through live health first:

- reconciliation blockers usually require repair or review;
- failed close orders need order-lifecycle triage;
- stale close approvals should expire or be resubmitted by policy;
- target-reached positions should be handled by live management, unless close
  approvals are stale or blocked.

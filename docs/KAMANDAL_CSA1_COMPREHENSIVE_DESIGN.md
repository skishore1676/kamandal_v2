# Kamandal Core Strategy Alignment Release (CSA-1)

Status: Implementation-authoritative, shadow-only release design
Version: 1.2
Updated: 2026-08-08
Repository: `skishore1676/kamandal_v2`
Target branch: `codex/core-strategy-alignment-csa1`
Source: operator-provided `KAMANDAL_CSA1_COMPREHENSIVE_DESIGN_V1_1.md`

## Executive Summary

CSA-1 is an additive strategy overlay inside Kamandal v2. It does not create a
second trading application or replace the current intelligence-driven workflow.
Baseline and any eventually promoted CSA lane share one Public account, broker
truth, risk manager, position ledger, order queue, reconciliation contract, and
operator Sheet.

This release builds and deploys CSA in isolated shadow mode. Shadow may read real
market, account, position, event, and non-submitting broker-preflight data, but it
must never create live order intents, modify baseline plans or positions, reserve
real buying power, or submit/replace/cancel an order. Promotion of any CSA lane is
a separate operator decision after five completed trading days of evidence.

## Decisions And Options Considered

1. Kamandal is already an intentional live and profitable multileg executor.
2. Public preflight BPR is already authoritative for short strangles; the local
   estimate is labeled fallback evidence. This is a completed prerequisite.
3. Google Sheet `playbooks` rows are the canonical operator surface for ordinary
   entry and management policy. No price, IV-rank, delta, DTE, target, roll, or
   loss threshold may silently fall back to a repository policy value.
4. `control.yaml` may hold structural safety, schedules, evidence paths, feature
   enablement, and emergency stops. Environment variables may hold host permission
   and emergency overrides. Neither replaces normal Sheet policy.
5. The strangle expansion row currently reads from the Sheet as price 20-250,
   IV-rank 30-100, enabled. CSA reuses that operator policy; these numbers are not
   copied into source configuration.
6. The original fixed August 10-17 calendar is replaced with a relative operating
   gate: deploy shadow, observe five completed trading days, then produce a
   promotion packet. No live-pilot date is presumed.
7. Existing daily-report truth fixes, alert deduplication, universe-proposer
   hardening, and broker-BPR work are retained rather than rebuilt.

## Problem

The current workflow is deliberately idea-driven and close-oriented. It does not
fully reproduce the operator's recurring market-wide strangle discovery,
portfolio-delta call-vertical sourcing, strangle adjustment lifecycle, or
diagonal short-leg/cost-basis management. Earnings calendars mostly exist but
need event-aware admission and expiration selection.

The missing behavior must be added without allowing a shadow experiment to alter
the working live system or creating a second account brain.

## Users And Jobs

- The operator configures ordinary strategy and lifecycle policy in the existing
  Google Sheet, reviews daily evidence, and decides whether a lane may advance.
- Kamandal discovers and normalizes opportunities, explains admission and
  rejection, simulates complete lifecycles, and produces restart-safe receipts.
- The runtime operator deploys an exact reviewed commit, verifies independent
  sidecars, and can disable CSA without disturbing baseline jobs.
- A reviewer can trace each decision to market evidence, broker evidence, Sheet
  policy, code version, lifecycle version, and the resulting shadow action.

## Goals

- Preserve baseline outputs when CSA is disabled or shadow-only.
- Discover strangles and portfolio hedges without requiring an Idea.
- Adapt existing Ideas for diagonals and earnings calendars.
- Produce structured admission and scoring evidence for selected and rejected
  candidates.
- Simulate entries, fills, lifecycle actions, adjustments, and exits through the
  same decision and ticket path that a future live adapter would consume.
- Persist restart-safe CSA state in additive SQLite tables.
- Operate CSA through separate monitored launchd jobs and scorecards.
- Produce a five-trading-day promotion packet without enabling live CSA trading.

## Success Metrics

- Zero CSA broker order effects and zero unexplained baseline behavior changes.
- Every candidate, rejection, action, and fill has complete provenance and a
  deterministic replay result.
- All required policy resolves from the operator Sheet or fails closed.
- All four lanes complete their deterministic entry-to-exit scenarios and
  restart/reconciliation tests.
- Five completed trading-day reports reconcile to durable receipts without an
  unexplained missing scheduled run.

## User Scenarios

1. A Sheet-enabled market-scan playbook discovers a short-strangle candidate
   without an Idea, records all rejected alternatives, and uses Public BPR lineage.
2. Excessive positive portfolio delta creates a defined-risk call-vertical hedge
   opportunity whose score explains the proposed risk reduction.
3. An existing diagonal Idea opens in shadow, manages the near short, and carries
   cumulative cashflow and active cost basis across a restart.
4. An earnings-calendar Idea is enriched with a known event and fails only its
   specialization when event evidence is missing or conflicting.
5. The operator disables a CSA stage in the Sheet and the next cycle creates no
   new risk while preserving management and diagnostic evidence as configured.

## Non-goals

- Replacing or globally re-ranking the baseline planner.
- Creating new Google Sheet tabs.
- Automatically adopting existing live positions.
- Proving long-run strategy profitability from five trading days.
- Enabling live CSA lanes or submitting CSA orders in this release.
- Duplicating existing calendar, broker, reconciliation, or full-close machinery.

## Requirements

The following invariants, policy rules, shared contracts, and lane requirements
are normative. Requirement identifiers used in the implementation-status map are
`INV-*`, `POL-*`, `SHR-*`, `LANE-*`, `ACT-*`, `DB-*`, `ISO-*`, `OPS-*`, and
`TEST-*` in their document order.

### Non-negotiable invariants

1. One live account authority and one combined live risk decision.
2. CSA shadow cannot mutate baseline candidates, plans, Sheet live rows, order
   intents, live positions, management decisions, or permissions.
3. CSA shadow uses a separate virtual ledger and idempotency namespace.
4. Broker state is authoritative for live ownership; SQLite is lifecycle evidence.
5. At most one action proposal per group and lifecycle version.
6. Unknown lane, policy, BPR source, event state, or ownership blocks new risk.
7. Original candidates are immutable; active legs and cashflow lineage evolve.
8. Adjustments never increase short contract count in CSA-1.
9. Shadow and future live produce identical decisions and tickets before the
   execution adapter boundary.
10. Rollback disables CSA independently and leaves baseline operation intact.

### Operator policy contract

The existing `playbooks` tab receives three CSA metadata columns:

- `csa_stage`: blank/baseline, shadow, pilot_live, or live.
- `source_mode`: idea, market_scan, or portfolio_hedge.
- `management_policy_json`: operator-visible thresholds and permissions not
  already represented by existing playbook columns.

Lane identity is derived from `playbook_id`, `strategy_family`, and `structure`.
Existing playbook fields continue to own DTE, delta, IV, liquidity, sizing, BPR,
profit, event, and structure policy. `management_policy_json` owns only missing
lifecycle policy such as tested-side confirmation, roll constraints, adjustment
limits, inversion permission, cooldown, and loss stages.

Missing or invalid required Sheet policy fails closed. Code may define numeric
domain bounds for validation, but not an operational trading choice. Every
decision records the resolved value, Sheet field, policy hash, and read timestamp.

### Architecture

Create `src/kamandal_v2/strategy_lanes/` with focused modules:

- `models.py`: opportunity, admission, lifecycle, action, and receipt types.
- `policy.py`: Sheet policy parsing, validation, hashing, and evidence.
- `registry.py`: explicit lane registration and fail-closed dispatch.
- `sources.py`: market scan, portfolio hedge, and Idea adapters.
- `admission.py`: source, market, structure, broker, and portfolio stages.
- `scoring.py`: transparent lane component scores.
- `action_arbiter.py`: precedence, debouncing, cooldown, and one-action rule.
- `tickets.py`: deterministic open, close, adjustment, and duration-roll tickets.
- `strangle.py`, `call_vertical.py`, `diagonal.py`,
  `earnings_calendar.py`: lane-specific deltas over existing primitives.
- `shadow_execution.py`: non-submitting conservative fills and lifecycle adoption.
- `reports.py`: daily and five-day evidence.

Working baseline behavior is not moved for symmetry. Shared extraction requires a
golden baseline test proving identical output with CSA disabled.

### Shared opportunity and admission contract

Sources normalize to `StrategyOpportunity` with immutable identity, lane, source
mode, underlying, observation time, evidence, market/event/portfolio context, and
confidence. Admission runs five deterministic stages: source, market, structure,
broker, and portfolio. It preserves all applicable rejection codes and one primary
blocker. Every admitted candidate exposes a 0-100 score with named components and
penalties; configured Sheet priorities and weights must affect the visible math.

### Lane requirements

#### Short strangle

- Source: market scan over Sheet-enabled universe and playbook policy.
- Construct same-expiration put/call pairs and bound delta asymmetry.
- Use recognized Public preflight BPR as authoritative.
- Missing Public BPR blocks future live admission; shadow may use labeled fallback.
- Persist total credit, adjusted breakevens, Greeks, BPR lineage, roll counters,
  inversion, and executable close estimates.
- Support tested-side debouncing, untested-side same-expiry credit rolls, duration
  rolls, bounded inversion, profit/time/event exits, staged loss handling, and
  management blocking on ambiguous broker/order state.

#### Call vertical

- Sources: existing bearish Ideas and excessive-positive-delta portfolio context.
- Reuse the existing builder with bounded Sheet-configured width search.
- Use actual defined max loss and one canonical BPR cap source.
- Expose expected-move location, measured call richness, hedge benefit, and
  defined-loss percentage.
- Keep management close-oriented for CSA-1.

#### Directional diagonal

- Source: existing Idea adapters.
- Reuse call/put diagonal primitives while enforcing far-after-near expiration,
  debit-versus-width, long intrinsic/extrinsic, short-credit coverage, and
  strike-grid-derived width.
- Persist initial debit, cumulative short cashflows, active cost basis, active
  near short, far long, and front-expiry roll count.
- Support full close, short-leg roll/resale, and approval-gated long-only state.

#### Earnings calendar

- Source: external/manual Idea enriched by EarningsStore.
- Reuse existing call/put calendar construction, Public order flow,
  reconciliation, marking, and full-position close.
- Require a known event and select near/far expirations around it according to
  Sheet policy.
- Unknown/conflicting event data rejects only the earnings specialization.
- Do not add repeated short-leg sales or a duplicate calendar manager.

### Action and ticket contract

The global arbiter orders: working-order conflict, ownership/reconciliation
ambiguity, hard emergency, mandatory event exit, executable profit, time decision,
lane adjustment, routine management, hold. Actions are deterministic over group,
lifecycle version, type, and legs.

Mixed actions use per-leg `BUY|SELL` and `OPEN|CLOSE`. Every future live action
requires fresh Public preflight. Credit-order repricing uses cancel-confirm-new
identity when atomic replace is unavailable. Partial or ambiguous broker results
remain blocked until reconciliation proves active legs.

### Persistence

Add idempotent tables for scan runs, opportunities, admission decisions,
lifecycles, adjustments, and shadow order intents. Add nullable lane/action lineage
columns to live intents only when absent. Migrations inspect schema first, execute
transactionally, back up the database with checksum, run integrity checks, and do
not synthesize history for existing positions.

### Shadow isolation and fill model

CSA shadow reads real current context but writes only CSA tables and reports. It
cannot call order placement, cancellation, or replacement. It persists the exact
ticket a future live adapter would receive. Fills use conservative executable-side
quotes, bounded repricing, and explicit miss/slippage evidence.

### Scheduling and reports

Add separate `csa-shadow-scan`, `csa-shadow-management`, and
`csa-shadow-scorecard` commands/scripts/jobs. Exact schedules are structural
configuration and must not change baseline job timing. Reports live under
`data/reports/csa1/` as JSON, Markdown, and CSV and summarize lane funnel,
preflight/BPR, fills, lifecycle actions, P&L/MFE/MAE, policy hash, failures, and
baseline non-interference.

### Test contract

Tests must cover baseline golden equivalence, Sheet policy flow/failure, discovery
without Ideas, same-expiry strangle pairing, broker BPR, admission reasons,
transparent scores, width search, diagonal invariants, event calendars, lifecycle
accounting, roll/inversion math, mixed tickets, one-action arbitration, shadow/live
decision parity, persistence/restart, duplicate prevention, reconciliation
blocking, reporting, CLI, migrations, and launchd registration. Existing tests may
change only for an intentional documented contract change.

## Dependencies

- Existing playbook Sheet ingestion and cached/offline fail-closed behavior.
- Existing Public broker preflight BPR, order construction, reconciliation, and
  position ownership contracts; shadow uses only non-effectful reads/preflight.
- Existing builders for verticals, diagonals, and calendars.
- Existing SQLite store, launchd registry, reports, CLI, and oldmac runtime.
- External earnings and market data with freshness and conflict evidence.

## Rollout And Migration

1. Build from current `main` on `codex/core-strategy-alignment-csa1`.
2. Maintain `docs/CSA1_IMPLEMENTATION_STATUS.md` mapping requirements to code and
   tests.
3. Back up oldmac SQLite, Sheet, effective config names, launchd definitions, and
   current commit before protected mutation.
4. Validate local and oldmac tests, compile, shell syntax, migration dry run,
   config, deterministic scenarios, and baseline golden comparison.
5. Deploy with CSA live lanes empty, baseline permissions unchanged, and a hard
   non-submission guard.
6. Install/monitor only the CSA shadow sidecars. Do not force baseline live jobs.
7. Read back commit, schema, policy hash, jobs, reports, zero CSA live intents,
   and zero order-side effects.
8. Observe five completed trading days and produce a lane-by-lane promotion
   packet. Do not promote automatically.

## Acceptance criteria

- All baseline and CSA tests and deterministic scenarios pass.
- CSA disabled/shadow produces no unexplained baseline output change.
- No core-path TODO, stub, or unimplemented required action remains.
- Migration is additive, idempotent, backed up, and integrity-checked.
- Sheet policy compiles with provenance and no repository trading thresholds.
- Shadow and dry-live decisions/tickets match before execution.
- Oldmac runs monitored CSA sidecars with live submission impossible.
- Daily evidence exists for five trading days.
- Promotion packet distinguishes operational proof from strategy-edge uncertainty.
- No CSA live lane or broker order is enabled without a separate operator gate.

## Edge Cases And Failure States

- Missing, malformed, stale, or conflicting Sheet policy blocks new risk and
  identifies the exact field and source revision.
- Missing broker BPR, crossed/stale quotes, absent expirations, invalid strike
  grids, or event conflicts produce durable rejection evidence.
- Duplicate cycles, crashes between ticket and fill, partial lifecycle state, and
  clock/session boundaries replay idempotently.
- Unknown broker ownership, working-order conflicts, or reconciliation ambiguity
  block management that could increase or incorrectly close risk.
- A disabled lane performs no new entry; recovery behavior is explicit and does
  not silently orphan a simulated lifecycle.

## Risks

- Stop if implementation would require weakening baseline gates or tests.
- Stop if Public capability/BPR cannot be proven without a real order.
- Stop before database migration, Sheet mutation, launchd installation, runtime
  restart, or broker API mutation unless the exact protected action is approved.
- Stop promotion on any unexplained baseline mutation, duplicate action,
  reconciliation ambiguity, unknown BPR, policy drift, or missing rollback proof.

## Open Questions

- Which exact existing playbook rows will receive CSA stages after local proof?
- Which lifecycle fields not already represented in the Sheet belong in
  `management_policy_json` for each lane?
- Which oldmac launchd schedule windows avoid every baseline operational window?
- Whether five clean days justify a longer shadow window remains an operator
  judgment; this release produces evidence rather than assuming the answer.

## Assumptions

- The existing Public preflight path remains the authoritative broker BPR source.
- The current Sheet stays the canonical operator surface and adds columns rather
  than tabs.
- Oldmac remains the runtime source of truth and has the existing market, account,
  and earnings dependencies required for read-only shadow context.
- The user-provided v1.1 strategy intent remains valid except where this revision
  records an explicit decision.

## Completion Audit

Required Evidence for completion includes a merged implementation, synchronized laptop/GitHub/oldmac,
backup and migration receipts, full test and scenario receipts, Sheet readback,
launchd and report readback, five-day shadow scorecard, baseline non-interference
evidence, self-review, independent evaluator findings, rollback instructions, and
an explicit statement that CSA placed no live order.

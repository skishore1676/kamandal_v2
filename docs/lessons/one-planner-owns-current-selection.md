---
title: One planner owns the current portfolio selection
type: decision
area: live execution
date: 2026-09-10
tags: [portfolio, fallback-retirement, cockpit, publication]
refs: [src/kamandal_v2/strategy_engine/planning.py, src/kamandal_v2/live/execution.py, src/kamandal_v2/sheets.py]
---

# One planner owns the current portfolio selection

A failed order is execution evidence for the next scheduled planner, not a reason
for the executor or reconciler to become another portfolio decision-maker.

The September 10 audit found a September 9 fallback campaign repeatedly replacing
current-day live Sheet rows with an old blocked plan. The writer removed today's
rows but preserved yesterday's duplicate publications. The executor saw no approved
rows and reported no selected failure. Rank-one-only ticket staging, shipped in
c01535f, had not removed automatic fallback.

The operator approved retirement and deployment on September 10. Remove the
portfolio fallback coordinator, registration, inline replan/submit path, and
configuration overrides. Also remove executor-side stale-plan rebuilding. Preserve
same-order bounded pricing, authoritative order lineage, broker uncertainty,
partial-fill adoption, shared risk gates, and lifecycle management.

The scheduled unified planner alone publishes new live selections. Serialize
read/merge/write, reject dates outside the current owned lane, deduplicate historical
plan identities, and publish values in one RAW write with a blank tail instead of
clearing first. The selected-plan receipt detects lost pending automatic handoffs;
it does not authorize a broker order or bypass the Sheet.

Retained campaign events become inert history. Do not delete them, replay old
fallback tickets, reset broker identities, or cancel working orders as migration.
Existing hygiene handles stale pending tickets; actual broker exposure stays under
reconciliation. The next natural planner refreshes the cockpit and compacts repeated
history. No manual planner/executor job is needed for migration.

Acceptance: old fallback receipts and even legacy enabled config cannot trigger
planning/publication; stale writes fail before touching Sheets; repeated writes
preserve other lanes and operator notes; missing selection alerts once; stale entry
alerts without rebuilding; pricing, complete and partial fills, uncertain submission,
and normal unified planning remain covered by the full suite.

Tradeoff: replacement opportunities wait for the next regular planning cycle. If
latency later proves material, reconsider the existing planner schedule with evidence;
do not restore a second portfolio-selection owner inside order reconciliation.

## Planner quote latency

The first manual run after retirement took about 17 minutes: candidate construction
and diagnostics each requested complete chains for scan symbols before testing IV
or thesis eligibility. Public fetched expirations serially, multiplying the work.
Each `run_plan` now owns one `PlanningMarketCache`, shared by source groups,
source-exact supplements, and diagnostics. Preserve capture timestamps and complete
expiration coverage (including calendar/diagonal far legs and fallback dates).
Never extend this cache into execution or across invocations; preflight capabilities
remain delegated and uncached. Test quote-independent rejections first, defer the
strangle price gate until quotes exist, and respect permissive research matching.
Diagnostics report only proven rejections when price was not fetched. Plan metrics
record requests, cache hits, and symbols fetched; measure the next natural cycle
before claiming an observed runtime improvement.

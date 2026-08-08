# CSA-1 Implementation Status

Status: Release candidate complete; protected shadow deployment pending
Updated: 2026-08-08
Baseline commit: `68bdc89afd11d5d7de651d85595baa7c5176b4e1`
Authoritative design: `docs/KAMANDAL_CSA1_COMPREHENSIVE_DESIGN.md`

## Verdict

Kamandal now contains the additive CSA shadow overlay: strict Sheet policy,
typed opportunity/admission/lifecycle/action/ticket contracts, four strategy
lanes, CSA-only persistence, a broker-free fill adapter, three commands and
disabled-by-default jobs, and daily evidence outputs. The current planner and
live execution authority remain in place and are byte-for-byte equivalent on the
deterministic baseline fixture when CSA is disabled.

The completed strangle BPR correction remains a prerequisite, not new CSA work. Public
preflight records broker BPR provenance and applies the broker value directly to
short strangles. A local calculation remains visible as fallback evidence; CSA
shadow may record it, but a future live admission must fail when broker BPR is
missing.

## Status Legend

- **Existing:** usable prerequisite with current tests.
- **Partial:** useful primitive exists, but the CSA contract is incomplete.
- **Missing:** must be implemented in this release.
- **Deferred:** deliberately outside the shadow release.

## Requirement Map

| ID | Requirement | Baseline status | Current evidence | Release work / proof |
|---|---|---|---|---|
| INV-1 | One account authority and combined live risk | Existing | `live/risk_manager.py`, `live/execution.py` | CSA never creates a second live authority; isolation test |
| INV-2 | Shadow cannot mutate baseline/live state | Partial | Existing shadow fills are separate, but no CSA boundary exists | Dedicated store API and execution adapter; zero-call instrumentation |
| INV-3 | Separate virtual ledger and idempotency | Partial | `shadow_fills` and stable candidate IDs exist | CSA-specific lifecycle/action/ticket namespace and restart tests |
| INV-4 | Broker owns live position truth | Existing | live position/order reconciliation modules | Reuse read-only evidence; ambiguity blocks CSA actions |
| INV-5 | One action per lifecycle version | Missing | No global action arbiter | Deterministic arbiter and duplicate/restart proof |
| INV-6 | Unknown policy/BPR/event/ownership fails closed | Partial | Many existing strict gates; ordinary `Playbook` fields also have legacy defaults | Strict CSA policy compiler and staged rejection reasons |
| INV-7 | Immutable origin, evolving active legs/cashflow | Missing | Existing candidate payload is immutable-like; no CSA lifecycle lineage | Lifecycle and cashflow models/store |
| INV-8 | Adjustments do not increase short count | Missing | No CSA adjustment engine | Ticket invariant and lifecycle scenarios |
| INV-9 | Shadow/live decision parity before adapter | Missing | Current shadow and live planner paths differ | Shared decision/ticket contracts and parity tests |
| INV-10 | Independent disable/rollback | Missing | Existing jobs can be controlled individually | CSA stage gate, independent jobs, rollback runbook |
| POL-1 | Sheet-canonical CSA stage/source/management | Missing | Existing Sheet loader reaches column AZ and owns strangle expansion | Add typed fields and provenance; no runtime Sheet write during build |
| POL-2 | Existing fields own entry/sizing/BPR policy | Partial | `Playbook` already carries these fields | Strict CSA presence validation instead of repository defaults |
| POL-3 | Invalid or stale policy fails closed | Missing | Config validation exists; no timestamp/hash CSA policy | Policy errors, hash, read timestamp, tests |
| SHR-1 | Normalized opportunities from three sources | Missing | Ideas and market/portfolio primitives exist separately | Source adapters and immutable opportunity IDs |
| SHR-2 | Five-stage admission with all reasons | Missing | Candidate rejection is mostly flat strings | Typed stage results, primary blocker, full reasons |
| SHR-3 | Transparent 0-100 Sheet-weighted score | Missing | Baseline score has repository constants | CSA component score resolved entirely from Sheet policy |
| LANE-S-1 | Market-scan strangle discovery | Partial | Sheet universe expansion can match existing entries, but planning remains Idea-driven | Scan source and same-expiry lane scenario |
| LANE-S-2 | Broker-authoritative strangle BPR | Existing | `market/public.py`, `planner/candidate_builder.py`; Public adapter tests | Preserve and add CSA broker/fallback admission proof |
| LANE-S-3 | Strangle adjustment lifecycle | Missing | Baseline management is close-oriented | Debounce, roll, inversion, duration roll, staged exits |
| LANE-V-1 | Idea and portfolio-hedge call vertical | Partial | Call-spread builder and width-search tests exist | Portfolio source, hedge score, canonical BPR lineage |
| LANE-V-2 | Close-oriented vertical management | Existing/partial | Generic live exit management exists | Adapt to CSA lifecycle without live effects |
| LANE-D-1 | Idea-driven directional diagonal entry | Existing/partial | Call/put diagonal builders and shape validation exist | Enforce strict Sheet policy and richer accounting |
| LANE-D-2 | Diagonal short-leg and cost-basis lifecycle | Missing | No repeated short-leg lifecycle | Active legs, cashflow lineage, roll/resale actions |
| LANE-E-1 | Event-aware earnings calendar entry | Partial | Calendar builders and `EarningsStore` exist | Specialization adapter and expiration/event admission |
| LANE-E-2 | Reuse full close; no duplicate manager | Existing | Generic full-position close path | Integration/parity proof only |
| ACT-1 | Global deterministic action arbiter | Missing | Existing management decisions are lane-local | Precedence, cooldown, debouncing, one-action tests |
| ACT-2 | Mixed OPEN/CLOSE leg tickets | Missing | Opening and full-close ticket builders exist | Neutral mixed ticket model and serialization |
| ACT-3 | Fresh preflight for future live action | Existing/partial | Public ticket preflight exists | Parity contract; live promotion remains deferred |
| DB-1 | Additive idempotent CSA tables | Missing | `LocalStore` creates baseline schema idempotently | Explicit schema version/migration and store APIs |
| DB-2 | Backup, checksum, integrity, no fake history | Missing | Deployment scripts do not provide CSA migration receipt | Migration CLI/dry run/runbook; execute only at deploy gate |
| ISO-1 | Read real context, write only CSA state | Missing | No CSA modules/tables | Boundary tests against baseline table snapshots |
| ISO-2 | Broker-inert conservative shadow fills | Missing | Existing shadow fills use planned candidates | Separate non-submitting adapter and fill evidence |
| OPS-1 | Independent scan/manage/scorecard commands | Missing | CLI and registry patterns exist | Three commands/scripts/jobs, disabled until deploy |
| OPS-2 | One truthful daily aggregation path | Partial | `ops/daily_report.py` is canonical for baseline | CSA report reconciles from durable CSA receipts |
| OPS-3 | JSON/Markdown/CSV reports and five-day packet | Missing | Reporting patterns exist | CSA report module and fixture-backed output tests |
| TEST-1 | Baseline golden equivalence | Missing | Full baseline suite passes | Capture deterministic baseline receipt and compare disabled CSA |
| TEST-2 | Lane/scenario/restart/parity/migration proof | Missing | Builder and live tests provide fixtures | Add comprehensive CSA suites and proof scripts |
| DEPLOY-1 | Shadow-only oldmac deployment | Deferred until protected gate | Runtime baseline is synchronized at the baseline commit | Exact mutation approval, backup, deploy, readback |
| OBS-1 | Five completed trading-day scorecards | Deferred until deployment | No CSA runtime exists | Scheduled observation and promotion recommendation |

## Existing Assets To Reuse

- Domain: `Playbook`, option quotes/legs, candidates, portfolio state, broker
  preflight result, deterministic IDs, and JSON serialization patterns.
- Strategy construction: short strangle, call spread, call/put diagonal, and
  call/put calendar builders plus structure validators and width search.
- Evidence and authority: Public preflight, broker order/position reconciliation,
  earnings store, plan audit artifacts, daily report, and operator review.
- Operations: local SQLite store, command dispatch, scripts, launchd registry,
  log/health conventions, and oldmac deploy script.

Reuse does not mean moving working baseline code. CSA wraps or calls stable
primitives only where a golden equivalence test proves non-interference.

## Baseline Evidence

- Laptop branch, GitHub `main`, and oldmac runtime were synchronized at
  `68bdc89afd11d5d7de651d85595baa7c5176b4e1` before CSA edits.
- The full pre-CSA repository suite passes (439 collected tests).
- The live Sheet strangle row supplies expansion enablement, price bounds, and
  IV-rank bounds through the existing loader; values are not repeated here as
  repository policy.
- No `strategy_lanes` or CSA implementation package existed at baseline.

## Release Candidate Evidence

- Full repository suite: 485 tests pass.
- Golden baseline: pre-CSA and current normalized planner payloads are both
  57,701 bytes with SHA-256
  `a563b90f52b5f1760dac2a95640c8084cdbc03a8a5c84c77e500fff724bac259`.
- Migration: dry run leaves the database byte-identical; apply adds only ten
  `csa_*` tables, creates a checksum backup, returns `integrity_check=ok`, and a
  second apply adds no tables.
- Runtime isolation: end-to-end scan, management, restart, duplicate prevention,
  baseline-table counts, broker-inert fills, policy failure, and three-format
  scorecards are covered by tests.
- Adversarial fixes include Sheet expansion bypass of the old allowlist,
  live-ledger portfolio delta, timestamp-relative management, inward-only
  strangle defense, bounded inversion, Sheet cooldown, duration-roll credit,
  and event-capturing earnings expirations.
- Full proof packet: `docs/CSA1_RELEASE_PROOF.md`.

## Protected Runtime Work

Local implementation and fixture-backed proof do not authorize runtime effects.
Before deployment, the operator receives the exact commit, database migration,
Sheet range/values, oldmac checkout action, launchd definitions, rollback, and
readback commands. CSA live enablement and every broker order effect remain a
separate gate and are not part of this release.

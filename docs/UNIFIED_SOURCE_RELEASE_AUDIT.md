# Unified strategy engine — source release audit

## Bottom line

**Superseded source-release claim.** The earlier `c89d4e7` assertion was
withdrawn by the 2026-08-15 architecture-conformance review because returned
planning objects and helper checks did not prove the scheduled persisted
effects. The subsequent repairs are checkpointed through `bd76402`; they need
a fresh independent Phase 8 conformance review before this document may claim
protected Phase 9 readiness. This is not an oldmac deployment authorization and
does not establish any live P&L or broker claim.

The next action is a separate review of
`UNIFIED_STRATEGY_ENGINE_CUTOVER_RUNBOOK.md`, followed only by explicit
authorization for the listed oldmac, Sheet, database, launchd, and broker
operations.

## What the evidence says

| Question | Source finding | Consequence |
| --- | --- | --- |
| Can one policy compiler select all enabled rows? | `strategy_engine.policy` resolves `strategy_family` as capability, checks structure, and produces only `live` or `shadow` modes. | Legacy `csa_stage` is compatibility input; explicit `mode` is authoritative after migration. |
| Do all opportunity modes reach one optimizer? | The unified planner routes file ideas, enabled-universe market scans, and delta-triggered portfolio hedges through source-specific playbook groups before one candidate and portfolio-plan pass. | A market-scan strangle cannot become a separate per-opportunity winner. |
| Are live and shadow isolated? | Each book has a separate mode, portfolio configuration, audit root, planning receipt, and healthy-zero projection. | A shadow failure is reported without erasing a live book. |
| Are strangle and event contracts enforced? | Fixture tests cover same-side episodes, two-effect replacements, economics, paired diagonals, BMO/AMC event exits, and time boundaries. | These rules are source-proven; real broker confirmation remains a Phase 9/10 runtime proof. |
| Is one scheduler owner rendered? | A temporary render produced only `unified_planning.plist` and `unified_lifecycle_management.plist`. Retired owner scripts exit 64 before sourcing helpers. | Target topology cannot schedule baseline/CSA competitors together. |
| Is the migration reversible? | Fixture apply is idempotent, records a backup SHA-256, passes SQLite integrity checks, and restores byte-for-byte. | The production inventory still must block any unmatched oldmac position/order before mutation. |

## Local proof receipt

The historical `c89d4e7` proof list below is retained for traceability only; it
is not a current readiness verdict. The fresh repair proof is recorded in the
Super Goal evidence and must be independently reviewed against the amended
Phase 3/4/5/7/8 criteria.

- full `pytest -q` suite: passed;
- focused planner, lifecycle, management, event-timing, session, proposal,
  migration, history, launchd, and guarded dry-run tests: passed;
- `python -m compileall -q src`, shell syntax checks, and `git diff --check`:
  passed;
- two fresh fixture `unified-plan` invocations: byte-identical command output;
- fixture migration/apply/reapply/restore: passed;
- temporary-only launchd render: exactly two unified owner plists.

These proofs created only temporary local fixture databases and plist files.
They did not contact oldmac, a Google Sheet, a broker, an authentication
provider, launchd, or an external notification channel.

## How to understand the ownership change

Before the cutover, separate schedule names could each decide work for the same
trading domain. After it, `run_unified_planning.sh` is the sole planning
entrypoint and `run_unified_lifecycle_management.sh` is the sole management
entrypoint. The latter runs live branches before shadow branches and records
branch failures independently. Reconciliation, the guarded order executor,
health, and reports remain supporting jobs rather than competing strategy
owners.

For example, a short-strangle market scan now becomes a stable synthetic
opportunity for an enabled universe symbol, is matched only against
market-scan playbooks, and competes with the rest of that book in the same
portfolio optimizer. It does not prove the trade should be taken; it proves
that source routing cannot sidestep the portfolio selection and safety path.

## What remains unknown until Phase 9

- The current oldmac position/order inventory may contain an adoption blocker.
- The current Sheet header, validation, formulas, and reviewed
  earnings-calendar row still require the protected exact mapping/readback.
- The deployed runtime’s loaded-label replacement and broker-facing natural
  behavior have intentionally not been touched.
- A separate reviewer must evaluate this packet before an oldmac cutover; this
  local audit is evidence, not an independent runtime verdict.

## Phase 9 decision

**Do not authorize Phase 9 from this superseded audit.** First obtain a fresh
independent conformance verdict for the repaired source path. Even a positive
source verdict remains separate from the protected oldmac/Sheet/database/
launchd/broker authorization.

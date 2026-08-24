# Unified strategy engine — source release audit

## Bottom line

**Source release candidate is ready for the protected runtime inspection.**
The earlier `c89d4e7` assertion was correctly withdrawn. Repairs through
`0ab9519` fixed daily-policy authorization, frozen lifecycle management, market
holidays, and the live earnings target. The final review then found and fixed
two additional effect-path gaps: the scheduled executor now consumes typed
close and adjustment tickets, and the production cutover can adopt existing
live groups with frozen policy plus reconstructed entry economics. The same
review added a hash-bound, rollback-capable production runner and an installer
that removes every competing owner.

The complete repository suite, compileall, shell syntax, bounded Sheet
apply/rollback tests, copied-database adoption tests, and diff checks pass. This
is a source result only: oldmac and operator-Sheet truth must still pass the
runner's read-only inspection before any mutation, and no alpha or P&L claim is
made here.

## What the evidence says

| Question | Source finding | Consequence |
| --- | --- | --- |
| Can one policy compiler select all enabled rows? | `strategy_engine.policy` resolves `strategy_family` as capability, checks structure, and produces only `live` or `shadow` modes. | Legacy `csa_stage` is compatibility input; explicit `mode` is authoritative after migration. |
| Do all opportunity modes reach one optimizer? | The unified planner routes file ideas, enabled-universe market scans, and delta-triggered portfolio hedges through source-specific playbook groups before one candidate and portfolio-plan pass. | A market-scan strangle cannot become a separate per-opportunity winner. |
| Are live and shadow isolated? | Each book has a separate mode, portfolio configuration, audit root, planning receipt, and healthy-zero projection. | A shadow failure is reported without erasing a live book. |
| Are strangle and event contracts enforced? | Fixture tests cover same-side episodes, two-effect replacements, economics, paired diagonals, BMO/AMC event exits, and time boundaries. | These rules are source-proven; real broker confirmation remains a Phase 9/10 runtime proof. |
| Is one scheduler owner rendered? | A temporary render produced only `unified_planning.plist` and `unified_lifecycle_management.plist`. Retired owner scripts exit 64 before sourcing helpers. | Target topology cannot schedule baseline/CSA competitors together. |
| Is the migration reversible? | Fixture apply is idempotent and byte-restorable; the production runner requires exact DB/Sheet hashes, verifies its SQLite backup, performs bounded Sheet writes, and restores both surfaces on failure. | The live inventory still blocks before mutation if any group, policy, economics, integrity, or working-intent check fails. |

## Local proof receipt

The historical `c89d4e7` proof list below is retained for traceability. The
current candidate repeated these checks after the real executor, adoption,
Sheet transaction, direction-aware earnings, and launchd convergence repairs.

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
entrypoint. The latter synchronizes order state, completes live evaluation and
guarded close/adjust effects, synchronizes again, and only then runs shadow.
Branch failures remain independently recorded and cannot suppress already
staged live work. The separate approved-order runner performs a read-only
status refresh and owns open submissions only; active-order recovery remains
inside the unified lifecycle cycle. Reconciliation, health, and reports remain
supporting jobs rather than
competing strategy owners.

For example, a short-strangle market scan now becomes a stable synthetic
opportunity for an enabled universe symbol, is matched only against
market-scan playbooks, and competes with the rest of that book in the same
portfolio optimizer. It does not prove the trade should be taken; it proves
that source routing cannot sidestep the portfolio selection and safety path.

## What remains unknown until runtime inspection

- The current oldmac position/order inventory may contain an adoption blocker.
- The current Sheet header, validation, formulas, and reviewed
  earnings-calendar row still require the protected exact mapping/readback.
- The deployed runtime’s loaded-label replacement and broker-facing natural
  behavior have intentionally not been touched.
- Natural scheduled behavior remains unproven until the first market sessions;
  deployment proves topology and operational joins, not trading alpha.

## Phase 9 decision

**Proceed only if the protected inspection is ready and the operator has
explicitly authorized the oldmac, Sheet, database, and launchd effects.** The
runner does not place, modify, or cancel broker orders, and deployment must not
manually trigger a trading cycle.

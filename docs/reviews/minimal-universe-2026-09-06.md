# Minimal universe: migration and risk ownership

The universe now owns only `symbol`, `enabled`, and `notes`. The operator
approved removing stock-level strategy/profile/IV restrictions and unused
risk/event controls on 2026-09-06. This deliberately broadens which enabled
strategies can consider each enabled symbol; it does not enable more symbols,
change a strategy's live/shadow mode, or bypass strategy/portfolio gates.

## Dependencies corrected

- `UniverseEntry` parses only the three fields; blank enablement fails closed.
  Retired fields in historical snapshots cannot restore a restriction.
- Candidate matching and market-scan source eligibility no longer consult
  stock profiles/allowlists. Legacy playbook `profiles` is ignored, including
  when detecting overlapping strategy variants. Actual strategy IV, DTE,
  direction, event, liquidity, BPR, and source rules remain applicable.
- The short-strangle price/IV-rank criteria remain strategy requirements;
  missing/out-of-range criteria still block candidate construction.
- Schema validation, seeds, review prompts and proposal publication accept the
  minimal schema. Publication maps the observed header order and can also
  tolerate the old layout during deployment; it never clears the whole tab.
- Weekly review stores full proposals in `universe_review_commits.payload`.
  The publication cap is read from that ledger, not deleted Sheet metadata.
  The older write command shares the same review workflow. Preview/failure
  does not advance the review boundary.

## Risk audit: oldmac effective configuration

The risk manager is enabled. It checks account freshness, drawdown, loss
cooldown, daily new entries, configured correlation groups, and open positions
per underlying. The current underlying limit is **3**, not a one-position
policy. Group limits are megacap tech 5, semis 4, broad index 3 and crypto
adjacent 3; unlisted symbols are not assigned a correlation group automatically.
The configured daily new-position limit is 4, portfolio position limit 15,
and portfolio hard BPR utilization limit 55%. These settings were not changed.

`strategy_engine/planning.py` invokes `live/advisory._live_candidate_policy`
before live staging. This blocks capped symbols/groups and duplicate open
ideas/contracts. `live/execution.py` also consults the entry health/risk gate
before submission. `planner/plan_generator.py` selects at most one candidate
per underlying/idea per basket and applies portfolio/per-underlying BPR limits.
This is configured risk control, not a claim that arbitrary similar stocks are
automatically detected or that every possible correlation is modeled.

## Migration and acceptance

Archive the complete old Sheet and actual policy tables on both machines under
`outputs/minimal-universe-20260906/`. Preserve all symbol/enablement values and
operator notes. Shorten only exact known generated proposal notes after archiving
their original discovery evidence. Delete the retired columns, then verify the
native Sheet, source policy compilation and oldmac readback.

Verification covers the full suite, minimal and legacy policy snapshots,
proposal publication/readback and ledger provenance/cap, disabled symbols,
strategy IV requirements, live concentration/duplicate gates and unified plans.
No model, broker order, or scheduled trading job is invoked for acceptance.
Tuesday's natural scheduled run remains the operational end-to-end proof.

Rollback must restore both the archived Sheet layout and the prior code together;
the prior code expects the retired universe columns. Preserve any subsequent
operator decisions when applying a rollback.

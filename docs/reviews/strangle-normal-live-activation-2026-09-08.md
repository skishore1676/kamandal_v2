# Short-strangle normal-live activation — September 8, 2026

## Decision

At 21:15 CT on September 8, Suman explicitly authorized
`short_strangle_high_iv` to leave the finite pilot envelope and become a normal
live Kamandal strategy under its existing risk limits. This is an execution-stage
decision, not an alpha claim and not authorization to enlarge sizing.

The canonical Google Sheet was read immediately before writing. The unique
`playbooks` row with `playbook_id=short_strangle_high_iv` was row 10 and had
`mode=shadow`, `csa_stage=shadow`. One atomic update changed only:

- `playbooks!C10` (`csa_stage`): `shadow` to `live`
- `playbooks!BD10` (`mode`): `shadow` to `live`

The update preserved formatting and the existing strict `csa_stage` validation.
Readback confirmed both cells are `live`.

## Unchanged safety envelope

- enabled: `TRUE`
- execution venue: `tasty_primary`
- sizing: `fixed_contracts`, one contract per leg
- maximum live BPR per order: `$2,500`
- accepted inputs: `market_scan,exact_package`
- source exact-package scope: Greg Harmon and Mike Butler remain live only for
  `short_strangle`
- quote freshness, earnings, liquidity, exact-leg Tastytrade dry-run BPR,
  concentration, portfolio, risk-manager, reconciliation, execution-window,
  uncertain-submission, and complete-fill gates remain mandatory

`csa_stage=live` removes the pilot policy's one-canary reservation limit. It does
not bypass candidate selection or any broker and portfolio control.

## Validation and effect boundary

The post-write canonical Sheet-policy validator passed on oldmac at
2026-09-09T02:15:46Z: 100 universe rows, 19 playbooks, 15 enabled playbooks,
four source policies, zero errors, and the two pre-existing overlapping-variant
warnings. The new compiled Sheet hash is
`439fa275366636dafdede577ba5651d6ae5f87f752b20d862419bb3536099a0d`.

The September 8 immutable daily policy snapshot remains the earlier shadow
snapshot `ac8e86c8b6d9b67eed9303488bdc7b3ad6efd279d1c88e6c54b579e180417369`.
Therefore this edit cannot submit an order on September 8. It becomes eligible
for effect only after the next natural daily policy snapshot.

No planner, executor, reconciliation, report, or broker job was triggered by
hand. Post-write Tastytrade readback showed zero positions, zero orders since
September 1, and zero nonterminal orders. Current Kamandal risk management was
unblocked with zero reconciliation blockers. Live health remained YELLOW only
for self-healing Public close orders deferred after the product cutoff; those
venue-local incidents do not disable a Tastytrade entry while shared controls
remain green.

## First-live-day proof

The September 9 observer must use only natural scheduled runs and classify:

1. whether the frozen policy snapshot contains `mode=live` and `csa_stage=live`;
2. the live planner funnel and any selected short-strangle candidate;
3. exact-leg Tastytrade dry-run BPR and every admission or rejection reason;
4. reservation, guarded intent, broker acknowledgement, working/fill state, and
   complete per-leg fill details, if any;
5. lifecycle-management and reconciliation state without replaying an uncertain
   submission; and
6. zero-candidate or gated outcomes as valid natural evidence.

Observation does not authorize manual job triggers, broker-order changes, risk
limit changes, or a retry whose outcome is uncertain.

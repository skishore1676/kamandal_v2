# Future Sheet management-policy change manifest

Date: 2026-08-28

Status: Prepared only; **not applied**

This manifest makes the reviewed future-entry changes exact and reviewable. It
does not authorize a Google Sheet write, policy capture, planning cycle,
deployment, runtime activation, or broker action. Open lifecycles retain their
frozen compiled policy even after a future Sheet edit.

| Playbook rows | Field | Current runtime snapshot | Proposed future-entry value | Unchanged companion rules |
| --- | --- | ---: | ---: | --- |
| `put_spread_default`, `put_spread_high_ivr`, `call_spread_default`, `call_spread_high_ivr` | `max_loss_multiple` | 1.5 | 2.0 | 50% profit, 21 DTE, half-time, two valid adverse observations, pre-event exit |
| `call_calendar_low_iv`, `put_calendar_low_iv` | `profit_target_pct` | 40 | 25 | paired close, 14 DTE, half-time, full-debit loss, pre-event exit |
| `put_diagonal_overextended`, `call_diagonal_oversold` | `profit_target_pct` | 40 | 30 | paired close, 50% debit loss, 14 near-leg DTE, no half-time, pre-event exit |

## Required apply/readback sequence

1. Re-read the current oldmac checkout and newest captured policy; stop on drift.
2. Fetch the named Sheet rows and verify the current values above by playbook ID,
   not by row number.
3. Show the exact before/after cells to Suman and obtain the separate Sheet gate.
4. Change only the named fields and rows.
5. Re-fetch the Sheet ranges and capture a new policy snapshot/hash.
6. Run policy validation and confirm no unrelated row or field changed.
7. Confirm representative existing open lifecycles still carry their original
   compiled policy hashes and values.
8. Observe the next natural planning cycle; do not trigger it manually unless
   separately authorized.

The resting-profit platform policy is not a Sheet playbook change and is not
part of this manifest. Its shadow/live activation remains a separate config and
money-path decision.

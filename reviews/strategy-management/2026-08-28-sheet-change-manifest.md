# Applied Sheet management-policy change manifest

Date: 2026-08-28

Status: **Applied and verified** on 2026-08-28

This manifest records the reviewed future-entry changes made in the canonical
[`kamandal_v2` Google Sheet](https://docs.google.com/spreadsheets/d/16Vjgrj80VDeTIGg0y60w4LHenZg7R-tGGvOyLNFdFsE/edit).
Only the named cells were changed. The writes did not run a planning or
lifecycle cycle or take a broker action. Open and already-pending lifecycles
retain their frozen compiled policy.

| Playbook rows | Field | Previous Sheet value | Applied future-entry value | Unchanged companion rules |
| --- | --- | ---: | ---: | --- |
| `put_spread_default`, `put_spread_high_ivr`, `call_spread_default`, `call_spread_high_ivr` | `max_loss_multiple` | 1.5 | 2.0 | 50% profit, 21 DTE, half-time, two valid adverse observations, pre-event exit |
| `call_calendar_low_iv`, `put_calendar_low_iv` | `profit_target_pct` | 40 | 25 | paired close, 14 DTE, half-time, full-debit loss, pre-event exit |
| `put_diagonal_overextended`, `call_diagonal_oversold` | `profit_target_pct` | 40 | 30 | paired close, 50% debit loss, 14 near-leg DTE, no half-time, pre-event exit |
| all 23 playbook rows | `resting_profit_enabled`, `resting_profit_arm_progress_pct` | columns absent | 12 enabled live multileg rows `TRUE`; enabled shadow rows `TRUE`; disabled live rows `FALSE`; 25% arming progress | runtime live switch is on; shadow switch remains off; existing open and pending lifecycles are not opted in |
| `narrative_ignition_long`, `narrative_ignition_short` | `mode` | live | shadow | structural-break requirement and all economics unchanged |
| `put_spread_default`, `iron_condor_tight`, `jade_lizard_high_iv` | `rationale` / `notes` | contradicted executable cells | prose aligned with the existing 50% target and enabled/live state | no executable economics changed |

## Apply and readback receipt

1. The approved change was resolved by playbook ID, then written atomically to
   `playbooks!AJ2:AJ5`, `playbooks!AI12:AI13`, and `playbooks!AI14:AI15` using
   value-only updates. No row position was inferred without first matching its
   playbook ID.
2. Native Sheet readback returned `50/2` for rows 2–5, `25/1` for rows 12–13,
   and `30/0.5` for rows 14–15 (`profit_target_pct/max_loss_multiple`). Visual
   inspection confirmed the surrounding formatting and operator controls were
   intact.
3. The deployed oldmac validator read the canonical Google Sheet at
   `2026-08-28T21:20:17Z`: planner, CSA compatibility, and unified policy all
   passed with no errors. It compiled 19 enabled policies with snapshot hash
   `61606b567c79f64ecdb35bbf319f85e5c66e6d77e690f5946ee0ad8cee59337f`.
   Existing overlap warnings were unchanged and are not validation failures.
4. Read-only lifecycle inspection found five open and one pending-live lifecycle.
   The four open `call_diagonal_oversold` lifecycles still hold their original
   40% target and compiled policy hash; the pending `put_spread_default`
   lifecycle still holds its original 1.5x loss multiple. This is the intended
   per-trade policy freeze, not drift from the Sheet.
5. Independent architecture review found that the prior deployed validator could let
   blank management cells on legacy `baseline` rows reach model/config
   fallbacks. Deployed source now makes every enabled row supply explicit
   operator-owned quote and lifecycle controls, independent of `csa_stage`.
   Negative tests cover missing and malformed values; all 788 tests pass on both
   development Mac and oldmac. The corrected oldmac validator read this same
   live Sheet and passed all 19 enabled rows at deployed commit `a3d7f45`.
6. Columns `BY:BZ` were appended with native boolean and 0-100 validation. The
   initial readback confirmed 23 explicit row values, seven enabled shadow
   permissions, no live permission, and a 25% arming threshold on every row.
   Live activation later changed only `BY2:BY7`, `BY11:BY15`, and `BY20` to
   `TRUE`, covering the 12 enabled live multileg rows. Disabled live rows remain
   `FALSE`; shadow permissions and every arming threshold are unchanged.
7. No planning or lifecycle cycle was manually triggered. The next natural
   cycle remains the first runtime observation of the new future-entry policy.

## Live activation receipt

- Operator authorization was applied at both required controls: the 12 named
  live Sheet permissions and `live.resting_profit.live_enabled: true` in
  deployed configuration. `shadow_enabled` remains `false`.
- The activation source commit is `d6bf77d`. At post-deploy readback, oldmac
  matched `origin/main`; the effective configuration returned live `true` and
  shadow `false`.
- The deployed validator compiled all 19 enabled policies from the canonical
  Sheet at `2026-08-28T22:33:11Z`, with snapshot hash
  `e7a1d7895577a7911d1f068374ada001e0a72e2a6280ba113e07584f7312d013`.
  Planner, unified-policy, and CSA compatibility validation all passed.
- All 788 tests and bytecode compilation passed on development Mac and oldmac.
- The natural launchd jobs load from `/Users/sunny/Documents/kamandal_v2`, run
  every five minutes from 08:30 through 15:15 CT on trading weekdays, had last
  exit status 0, and were idle at readback. They have no environment override
  for resting-profit authorization, so each fresh process reads the deployed
  configuration; no launchd reload was needed.
- Read-only lifecycle inspection still found five open and one pending-live
  lifecycle with no resting-profit permission in their frozen compiled policy.
  Activation therefore preserves all existing positions and the pending entry.

The Sheet now owns per-playbook resting-profit permission and arming progress.
The runtime shadow/live flags remain separate upper-bound kill switches. Live
is on for the approved future-entry behavior; shadow remains off.

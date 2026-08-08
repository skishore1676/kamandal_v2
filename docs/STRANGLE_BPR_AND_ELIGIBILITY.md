# Short Strangle BPR and Eligibility Contract

Date: 2026-08-08
Status: Implemented locally; not deployed by this change

## Operator decisions

Kamandal is an intentional live multileg executor. The August concentration-cap
increase was operator-directed so the portfolio can grow buying-power utilization.
After roughly a month of live operation without strangles, the operator identified
two separate constraints: the app overstated strangle BPR and too few enabled
universe symbols could reach the strangle playbook.

## BPR authority

Short strangles are undefined-risk positions whose broker margin depends on the
broker's account and portfolio rules. Kamandal therefore uses this order:

1. Public preflight `buyingPowerRequirement` is authoritative when present.
2. Kamandal records the local estimate alongside it as fallback evidence only.
3. If broker preflight succeeds but omits BPR, the local estimate may be used and
   the candidate is labeled `bpr_source=local_fallback` and
   `broker_bpr_missing=true`.
4. All portfolio, per-underlying, basket, and structure caps consume the resolved
   BPR before ranking or selection.

Defined-risk structures retain the existing safety-floor behavior: Kamandal does
not let an unexpectedly small preflight value shrink known maximum loss.

## Eligibility overlay

The overlay does not invent symbols and does not bypass the operator universe. It
only broadens which already-enabled universe rows can reach the `short_strangle`
playbook.

An additional enabled symbol qualifies only when the operator sets all of these
fields on the relevant Google Sheet `playbooks` row:

- `universe_expansion_enabled=TRUE`;
- `underlying_price_min` and `underlying_price_max` contain the operator's range;
- `iv_rank_min` and `iv_rank_max` contain the operator's range; and
- the idea, playbook, DTE/delta, earnings/event, quote integrity, option OI,
  liquidity, broker preflight, BPR, portfolio, concentration, health, ranking,
  approval, session, and execution gates all pass.

Existing explicit strangle permissions remain valid even outside this range. The
overlay removes only universe-profile and per-row allowed-playbook mismatches for
`short_strangle`; it does not relax any other rejection reason.

The Google Sheet is canonical. The repository contains no price/IV fallback for
this expansion. A missing switch or bound fails closed and leaves only existing
explicit universe/playbook permissions active.

## Deployment proof required

Before oldmac activation:

1. review the diff and full test receipt;
2. run a broker-inert candidate replay showing broker BPR replacing the fallback;
3. read back the operator-entered Sheet fields and inspect which current enabled
   symbols become newly reachable;
4. confirm no job is in flight, deploy at a session boundary, and read back head,
   config, tests, and launchd state; and
5. observe the first real strangle preflight without forcing an order.

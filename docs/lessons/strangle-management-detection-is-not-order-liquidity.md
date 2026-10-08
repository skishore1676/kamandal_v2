# Strangle management: detection is not order liquidity

## Decision and evidence — October 8, 2026

The MS October 30 $190 put / $235 call strangle had 1,201 retained canonical
management observations from September 17 through October 7. Every observation
failed the old whole-position quote gate. Because the manager counted strike
tests only after that gate, its confirmation counter stayed at zero, and the
operator surface reported ordinary hold. No adjustment was proposed or filled.

A closing short call can have a legitimate zero bid and positive ask after its
premium decays. Buying it back uses the ask. Applying the entry-style per-leg
percentage-spread gate to that buyback disabled precisely the management action
for which the untested option had become cheap. Detection, action selection,
valuation, and executable order quotes must remain distinct.

## What tested means

Retain a precise, research-supported definition: a put is tested when fresh,
finite, positive underlying price is at or below its current short-put strike;
a call is tested at or above its current short-call strike. Two distinct fresh
snapshot observations on the same side confirm the episode. Option liquidity
cannot suppress those observations. Stale or invalid underlying evidence cannot
advance or re-arm the episode. Record both distances as percentages of the
respective strikes, with positive values inside the strikes; those distances
are diagnostics, not extra trading triggers.

The primary sources reviewed were tastylive's [strangle strategy page](https://www.tastylive.com/concepts-strategies/strangle)
and [rolling research summary](https://www.tastylive.com/news-insights/why-traders-roll-positions).
Both describe rolling the untested side when the other strike is breached.
The [Rolling 101 episode description](https://www.tastylive.com/shows/trade-managers/episodes/rolling-101-adjusting-strikes-08-28-2018)
provides context, but its available page does not establish a numerical early
trigger. Secondary discussions disagree about delta-based triggers. We did not
invent an arbitrary percent-distance threshold or represent it as validated
research. An earlier delta/proximity trigger is a separate strategy-policy change,
not necessary to correct the observed failure. The sources do not prove an alpha
edge for MS or guarantee a successful adjustment.

## Execution contract

- Detection uses fresh underlying evidence, independent of option spread gates.
- A confirmed test requests an adjustment even if no executable replacement is
  available. That state is `adjust / waiting_valid_quote`, with an explicit
  blocker, rather than ordinary hold. Cooldown, consumed episodes, exhausted
  adjustment count, and missing detection evidence have distinct hold reasons.
- A strangle close permits zero bid on a short option being bought back, while
  retaining finite, nonnegative, non-crossed quotes, a positive ask, freshness,
  and the frozen **whole-package** spread limit. Other lanes retain their
  existing per-leg quote rules.
- A paired roll prices only the old untested buyback and new short sale. The new
  short must have a positive bid and pass the frozen liquidity/delta limits.
  Its bid minus the old option's ask must meet the frozen minimum credit.
  Selection remains strictly inward, same expiry, same quantity, non-crossing.
  The untouched tested leg's quote does not veto the roll's execution liquidity.
- The manager starts at midpoint, freezes a natural-price boundary, and the
  executor uses its existing cancel/replace protocol and repricing cadence.
  Adjustments now participate in that protocol, retain adjustment trading hours,
  require broker preflight, and cannot reprice below minimum credit or to debit.
  Source/sleeve identity checks for new openings do not apply to authorized
  lifecycle management; the frozen lifecycle remains its authority.
- Maximum two filled side adjustments, confirmation/re-arm rules, 30-minute
  cooldown, ownership, reconciliation, and higher-priority exit rules remain.
  No lifecycle snapshot or entry policy is rewritten by this repair.

The live book preserves `adjust` versus `close` and displays quote blockers.
Existing health reporting escalates persistent adjustment quote blocks using
its existing stall threshold; no separate monitoring job is introduced.

## Acceptance evidence

An isolated replay of actual retained MS lifecycle and chains compares the old
and repaired code without broker calls or runtime writes. September 30 now
confirms the put test and stages the October 30 $235-to-$200 call roll. October
7's opening package remains too wide; its afternoon quote stages the required
joint close despite a zero call bid. These are order-decision counterfactuals,
not executable fill or P&L claims.

Regression coverage includes invalid/stale/crossed quotes, repeat snapshots,
missing replacements, natural-credit floor, limit preservation, exit precedence,
health escalation, adjustment repricing and trading-window enforcement. Runtime
completion still requires natural scheduled broker admission and fill/reconcile
receipts after deployment.

## Bounded confirmation followed by action — October 8 follow-up

Operator intent: once an exit is due, repeated reasonable zero-bid quotes should
lead to a bounded execution attempt, not endless observation. Stable quotes are
not proof of a fill. Ordinary valid exits proceed immediately.

The narrow exception applies only to short-strangle buybacks whose sole failed
check is package percentage spread. All quotes must remain fresh, complete and
valid; positive-bid legs still meet the frozen leg-spread limit. Total zero-bid
buyback cost is capped at **$50**, total package midpoint-to-natural concession
at **$25**, including quantities, the standard 100 multiplier and rounding
the natural boundary up to the existing nickel price increment. A midpoint below
one nickel starts at one nickel within that budget. These are
conservative operational budgets, not research-derived strategy thresholds.
Two chronologically distinct snapshots within the quote freshness interval must
have total absolute ask changes no greater than **$5**. Cached repeats do not
advance confirmation; invalid evidence, changed legs/version, excessive gaps or
price changes reset it. Budgets are total dollars, not per-contract allowances.

On confirmation, the existing joint-close executor starts at midpoint and uses
its existing tick-aware repricing toward the frozen, tick-rounded natural limit. Profit floors,
venue preflight, ownership, market hours and approval controls still apply.
Emergency, event and adverse-loss exits acquire no additional quote-confirmation
wait; the same absolute bounds and existing loss debounce remain. The exception
cannot authorize opening or rolling positions, or selling a zero-bid long leg.

The lifecycle mark and ticket retain confirmation and dollar-bound receipts.
Exceeding the exception bounds on a selected close immediately raises operator
attention; unconfirmed exits escalate through the existing stall threshold.
Existing working-order/failure health covers admitted but unfilled orders.
No new scheduler, Sheet field, frozen-policy rewrite or manual order is needed.

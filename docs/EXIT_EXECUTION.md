# Shared exit execution contract

Operator reaffirmed this policy on October 12, 2026. It applies to held positions across all five lifecycle lanes, including previously opened positions. Entry selection and roll-opening-leg checks retain their existing limits.

- With platform resting-profit execution enabled, strangles offer their frozen target on the first valid maintenance observation, even when current P&L is negative. Other positions offer it after their frozen percentage of target is reached (25% when absent). Historical per-playbook enable flags do not override this universal execution contract.
- Profit offers are DAY orders, renewed through ordinary maintenance, and never reprice away from the target. Target dollars and existing lifecycle policy hashes remain frozen.
- Loss, event and time exits start at the validated package midpoint. Wide percentage spreads and valid zero bids are pricing evidence, not admission vetoes for closing held risk.
- Existing maintenance reprices an unfilled forced exit after ten minutes: halfway toward its recorded natural boundary, then to that boundary, with two replacements maximum. This is bounded limit execution and does not guarantee a fill. Tiny debit buybacks retain the existing minimum nickel exception.
- Freshness, complete finite quotes, nonnegative bids, positive asks, noncrossed markets, ownership, broker checks, market sessions, existing loss debounce, and cancel-before-replace remain required. A roll opens risk and retains its opening-leg eligibility and minimum-credit checks.
- An uncertain earnings event cannot veto an independently valid expiry or emergency close. Earnings-calendar trigger timing remains its specialized policy; this execution change does not invent loss thresholds for frozen policies lacking them.

## Why earlier changes did not establish this contract

The downstream repricer already supported forced-exit concessions, but the upstream shared observer could reject spread width before producing an order. The later zero-bid repair admitted only a narrow strangle exception with repeated-quote and small-dollar requirements. Separately, per-playbook switches and the ordinary 25% arming threshold still delayed strangle target offers. Fixing a symptom in one layer left the other layers inconsistent.

The shared manager now explicitly requests close-execution observations and owns universal resting-profit admission. Default observations remain strict for entry/research consumers. Tests cover all lane identities, first-observation wide exits, invalid/stale quotes, loss debounce, target arming, forced replacement precedence, duplicate prevention, and repricing bounds. When editing exits, verify the entire observation → action → ticket → replacement path, not just the downstream repricer.

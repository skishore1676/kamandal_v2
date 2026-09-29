# Exact Guru package execution

Source policy and independently verified opening contracts determine the copy
route. Supported exact structures now include long calls, 1:2:1 call/put
butterflies, call crabs, and atomic groups of two or three calendars, alongside
existing strangles, calendars, diagonals, and iron condors.

A verified copy does not have to pass the income planner's marginal-score floor.
It receives ranking priority within the unchanged hard portfolio, concentration,
liquidity, reconciliation, and sleeve constraints. Ordinary candidate scoring is
unchanged. Broker preflight is mandatory and its BPR cannot exceed the order cap.

The Guru Sheet migration promotes the four existing shadow rows in place and
adds `guru_exact_calendar_bundle`. Legacy IDs retain `_shadow` to preserve
historical attribution; `mode` and `csa_stage` both become `live`. Ordinary rows,
source on/off switches, and the 40/40/80 sleeve limits are preserved. Each new
shape retains a $1,200 package cash/risk cap. Long calls are one contract; a
butterfly/crab is one exact 1:2:1 package. This does not raise the ordinary
one-contract gate. Long-call DTE remains 1–120; LEAPS remain outside that policy.

Butterflies support expiry-day entry only with validated entry/exit clock
buffers: stop entry 60 minutes before close and begin closing 30 minutes before
close, including configured early sessions. SPX requires broker-resolved SPXW
contracts. Crabs require at least two days until the front expiry and close at
one DTE. Profit/loss exits still apply. A close intent does not guarantee a fill.

Calendar groups require the same source, post, underlying and publication time,
complete independent verification of every member, disjoint unit-quantity legs,
and one shared near/far expiry pair. They submit as one four- or six-leg order;
never split submissions. Both constituent calendar routes must be authorized.
The derived revision binds every member. Reconstructing it from the latest feed
before submission revokes an approval if a member changes or disappears.
Pending/open duplicate ownership includes every component opportunity ID, even
when call and put interpretations originally had separate IDs. Planner receipts
are projected back to each original source event for operator attribution.

All these lifecycles use frozen entry policy and full-package close-only
management. Source switches stop new entries and do not abandon existing trades.
No historical entries are replayed when enabling a route.

Activation: `python scripts/apply_guru_live_packages_sheet.py` validates the
proposed complete Sheet policy without writes. `--apply` performs a concurrent
readback check, one atomic cell update, native exact-input validation repairs,
and full post-write policy verification. Deploy code first at an idle boundary.

Public documents 2–6 leg orders and ratio quantities:
https://public.com/api/docs/resources/order-placement/place-multileg-order
Actual account acceptance still requires its own non-order preflight; API
support alone is not proof that a particular package can trade.

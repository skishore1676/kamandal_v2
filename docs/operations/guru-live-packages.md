# Exact Guru package execution

The October 1 [two-entry-pathway contract](../TWO_ENTRY_PATHWAYS.md) is
authoritative for entry selection, evidence and sleeve ownership. Guru copying
uses source contracts and deterministic capital admission; the ideas optimizer
does not rank or reject Guru openings. Single-pass text/image evidence and exact
vertical capability replace the older image-only admission boundary as part of
this release. Deployment and natural proof must be read back separately.

Source policy and deterministically bound opening contracts determine the copy
route. Supported exact structures now include long calls, 1:2:1 call/put
butterflies, call crabs, and atomic groups of two or three calendars, alongside
existing strangles, calendars, diagonals, iron condors, and exact call/put verticals.

A source copy uses FIFO capital admission and never enters ideas optimization.
Ideas delta, IV, DTE, yield, concentration and score preferences do not select it.
Current executable quotes, source freshness and binding, supported management,
duplicate/overlap, reconciliation, account health, sleeve/account cash limits and
broker preflight remain mandatory. Broker BPR cannot exceed the order cap.

The Guru Sheet migration promotes the four existing shadow rows in place and
adds `guru_exact_calendar_bundle`. Legacy IDs retain `_shadow` to preserve
historical attribution; `mode` and `csa_stage` both become `live`. Ordinary rows,
source on/off switches, and the 40/40/80 sleeve limits are preserved. Each new
shape retains a $1,200 package cash/risk cap. Long calls are one contract; a
butterfly/crab is one exact 1:2:1 package. This does not raise the ordinary
one-contract gate. Construction DTE hints do not filter exact openings; actual
expiration and the configured exit window still govern manageability.

Butterflies support expiry-day entry only with validated entry/exit clock
buffers: stop entry 60 minutes before close and begin closing 30 minutes before
close, including configured early sessions. SPX requires broker-resolved SPXW
contracts. Crabs require at least two days until the front expiry and close at
one DTE. Profit/loss exits still apply. A close intent does not guarantee a fill.

Calendar groups require the same source, post, underlying and publication time,
complete source binding of every member, disjoint unit-quantity legs,
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

October 1 activation: `python scripts/apply_two_entry_pathways_sheet.py` validates
the proposed complete Sheet policy without writes. It adds the two exact vertical
rows using existing Guru sizing and management, extends source capabilities and
clarifies the ideas sleeve label. `--apply` performs a fresh concurrency check,
one atomic cell update with inherited native row formatting/validation, and full
post-write policy verification. Deploy code first at an idle boundary. The prior
`apply_guru_live_packages_sheet.py` migration is retained as historical tooling.

Public documents 2–6 leg orders and ratio quantities:
https://public.com/api/docs/resources/order-placement/place-multileg-order
Actual account acceptance still requires its own non-order preflight; API
support alone is not proof that a particular package can trade.

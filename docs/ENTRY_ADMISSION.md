# Entry admission and Guru timing

The Sheet remains the policy authority for 40% current ideas, 40% exact Guru
packages, and 80% total BPR ceilings. Source freshness, supported contracts,
sizing, quotes, broker preflight, venue/account buying power, duplicate protection,
and health checks remain mandatory at the guarded submission boundary.

In `auto_top_plan`, the unified planner and its existing Guru-only intake commit
frozen, stage-authorized tickets to one automatic entry ledger. The cockpit
shows that decision; its displayed `APPROVE_LIVE` cells are not a second automatic
submission authority. Manual and Telegram approval routes retain their existing
contracts. The first natural automatic planning handoff installs the new ledger
authority; existing historical tickets are not bulk-promoted during deployment.

Full-book planning calculates against a proposed replacement of older rank-one,
unsubmitted automatic reservations. It releases nothing during calculation.
A successful frozen handoff atomically inserts the successor queue, records the
selection, and retires captured safe predecessors before cockpit publication.
A successful empty selection explicitly withdraws those reservations. A failed
handoff or a predecessor claimed during planning leaves the prior authority and
risk reservation intact. Broker evidence, attempted submission, partial or
uncertain fills, replacement children, and independent Guru intake are protected.
The final pre-POST claim cannot revive a ticket retired during preflight.

The existing X intake evaluates current Guru evidence through `unified-plan
--guru-only`. Source interpretation remains cached; admission is reevaluated on
later scheduled observations of unchanged evidence, because buying power, quotes,
preflight, and health can change. Pending and filled source identities prevent
duplicate commitments. Independent exact entries retain unchanged contracts,
original source deadlines, and frozen policy, without replacing the ideas
selection. A failed correspondent leaves the job degraded, while a valid sibling
still reaches the existing planner.

The executor examines the shared queue in creation order. A ticket-local or
venue-local hold yields to later tickets; a global risk breaker stops the queue.
Existing per-cycle submission bounds remain enforced. Daily basket capacity is
three baskets independently for `current_idea` and `guru_exact` (the configured
`live.max_live_baskets_per_day` applies to each sleeve). Each ticket retains
its venue, current-day authority, frozen policy, and final fresh admission checks.
No executor, reconciler, or fallback campaign becomes another portfolio planner.

Birdclaw's existing digest owner adds correspondent-only afternoon captures;
Kamandal intake follows by one minute. Frequency rises from 15 minutes after
13:00 CT to 2–5 minutes near the close. Generic digest extraction retains its
original slots and stable publication. Executor ticks at 14:51–14:54 CT follow
the last capture/intake at 14:52/14:53 CT and support the Guru cutoff at 14:55 CT.
Ordinary entries retain 14:40 CT. Product/early-close calendars and the opening
buffer still apply. A preflight beginning inside the window and completing after
the cutoff cannot submit. Capture → interpretation → admission → executor elapsed
time and remaining deadline margin still require natural runtime receipts;
schedule ordering alone does not prove timely entry or a fill.

Regression coverage includes `test_automatic_entry_authority.py`,
`test_portfolio_sleeves.py`, `test_live_lane.py`, `test_option_sessions.py`,
`test_observed_package_planning.py`, `test_correspondent_activation.py`, and
`test_launchd_schedule_contract.py`.

## Daily basket slots — October 13, 2026

A basket consumes one slot in a sleeve when that sleeve reaches broker admission
or has a working, uncertain, partial, or filled commitment. Replacement attempts
share the original plan's slot. A mixed plan consumes a slot in each sleeve only
when that sleeve has an attempted commitment; an unsubmitted sibling is not a fill.
Filled baskets retain their daily slot even after their positions close.

A slot is released only when all attempted commitments in that plan/sleeve have
confirmed terminal zero-fill broker receipts. Local cancellation alone, absent or
malformed fill quantities, any partial-fill history, active replacements, or an
unresolved later submission keep the slot. Complete native Tastytrade terminal
leg receipts may prove zero fills using the existing adapter normalization.
Refunds are derived from the ledger; historical receipts are never rewritten.
A previously refunded plan must pass the sleeve limit again before resubmission.

The shared executor, per-cycle submission rate, source deadlines, duplicate and
ownership checks, 40/40/80 BPR ceilings, and broker/venue safeguards are unchanged.
This is not a same-session symbol re-entry prohibition. The separately proposed
DHR churn restriction has not been authorized by this change.

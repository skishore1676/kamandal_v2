# Greg and Mike: Tuesday operating contract

Suman explicitly requested both sources' ideas enter Kamandal's normal portfolio process, both sources' reconstructable exact entries stay in shadow, and a daily Sheet expose interpretation, treatment, and residuals. The September 5 deployment is extended to activate that contract.

## Operator policy

The existing `trade_sources` tab is the sole authority:

| Source | Ideas | Exact packages |
|---|---|---|
| Greg Harmon | live ceiling | shadow ceiling |
| Mike Butler | live ceiling | shadow ceiling |

A live idea ceiling lets the ordinary portfolio process choose applicable live or shadow playbooks. It is not an instruction to buy every idea. Exact packages stay broker-inert: complete contracts, supported shape, market data, source age, eligibility, and portfolio capacity still determine admission. Unsupported, incomplete, stale, and rejected outputs remain visible with reasons. No order, trading job, or strategy manager was manually triggered for this rollout.

Kamandal constructs/manages admitted ideas and manages admitted exact shadow entries using its existing frozen lifecycle policy. Guru exits and adjustments remain source observations, not commands to mimic their management. The same underlying opportunity can legitimately have an idea result and a separate exact shadow result; their identities and lifecycle links remain separate.

## One interpretation owner

Both source profiles declare their X author handles. The generic X digest importer excludes configured correspondent authors (case-insensitively, including source URLs) and reports the excluded count. Disabling a source or setting it to observation does not silently route its posts through generic extraction. This prevents duplicate ingestion and bypassing the source policy. Source identity is still verified by Birdclaw; this routing metadata does not change acquisition authority. New guru onboarding requires matching Birdclaw/Kamandal profiles, author ownership, and evaluation fixtures.

## Daily review surface

Use the existing workbook's **trade_source_activity** tab:
https://docs.google.com/spreadsheets/d/16Vjgrj80VDeTIGg0y60w4LHenZg7R-tGGvOyLNFdFsE/edit#gid=1701169706

The bounded view shows the latest retained outputs, source/post links, idea/exact/residual classification, readable interpretation or leg terms, planner treatment, effective mode, blockers, and linked lifecycle status/IDs. Lifecycle reads include closed positions and distinguish idea from exact-entry identities. `no_linked_lifecycle` means no attributable lifecycle was found; it must not be read as proof that an old/unlinked trade never happened.

The latest 500 outputs are a machine-owned review window; canonical events and lifecycle records remain in Kamandal's database. Raw normalized evidence stays available in the Sheet. The view is not policy and should not be used to arm trades or as a row-position-based annotation store.

The Sheet is refreshed after source activation and planning and now also by the existing passive daily-report job. On oldmac's Central-time schedules, X intake runs at 08:15, 09:15, 11:45, and 14:00; portfolio planning at 08:50, 09:25, 11:55, and 14:15; the final daily report follows management at 15:25. These are existing schedules, not newly created timers. Trading-calendar guards still apply.

A standalone `kamandal project-trade-source-activity` command refreshes only the Sheet from a read-only database connection, with zero model calls, no planner, and no orders. It is safe for projection verification/recovery. The projection now uses one RAW values write rather than clearing the Sheet before rewriting, preserving formatting and preventing source text from being interpreted as formulas. Exact-evidence failures have separate output IDs so they cannot overwrite an otherwise valid idea's review row. Report projection failure is non-blocking and visible in the report job result; the next existing cycle retries it.

## Verification boundary

Source settings were changed only in `trade_sources!C3:D4`, then validated through the canonical oldmac Sheet policy compiler. Playbook, universe, risk, approval, and execution settings were untouched. Reprojection populates the Sheet from actual retained records; it does not relabel old records with today's permissions or imply a fresh interpretation. Tuesday's naturally captured posts and portfolio outcomes remain future observations, not preclaimed results.

# Guru opening triage at the first natural September 23 run

The deployed `source-evidence-v2` run succeeded at 13:15 UTC on oldmac. Its retained episode batches are `f3741bc32148cb1e` for Greg Harmon and `aa1db135e02d3023` for Mike Butler, under `data/research/correspondent_signals/source_episodes/runs/`. Both acquisition references still say `incomplete` because a ten-post fetch hit its limit. Adjacent retained pulls overlap except for one bounded Greg gap on September 7; this is evidence of continuity for the other intervals, not a proof that all posts or linked media were captured. The 22 observed-package failures were all `source_too_old` from the historical reinterpretation sweep.

The retained interpreted window is **September 2–22**, roughly three weeks, not yet the requested four to five. Counts below are distinct non-template opening opportunity IDs in the two latest episode batches. A package marked complete means the interpreter supplied all required fields; it is **not independent image verification** or broker eligibility.

| Source | Non-template openings | Model-complete exact openings | Main observed structures and gaps |
| --- | ---: | ---: | --- |
| Greg Harmon | 12 | 0 | Eleven openings have no reconstructable structure because linked article/media terms are missing; one broken-wing call fly still lacks an exact October date. Fifty-four template menu items are excluded from the opening denominator. |
| Mike Butler | 27 | 26 | Butterfly 6, call diagonal 5, call crab 4, call calendar 3; put calendar, put diagonal, long call, short strangle, financed calls, super bull, covered call, and put spread appear once each. One opening lacks its image. Some labeled packages contain multiple subtrades or a nonstandard leg count. |
| **Total** | **39** | **26** | Model-complete field coverage is at most 26/39 (67%). Source-verified coverage is unmeasured and cannot be reported as 67%. |

The exact-copy executor now has explicit structural code for short strangles, simple two-leg call/put calendars, and simple two-leg call/put diagonals. The Sheet `live_structures` list is still `short_strangle` for both gurus. Adding a name to the CSV cannot activate butterfly, call crab, covered call, mixed-leg structures, or an opening made of multiple packages. Those need new shape, risk, broker, and management validation. A multi-package opening is parked until it can be submitted and managed as one complete source opportunity; selecting just one leg package would not be exact copying.

## Next evidence work

1. Close Greg's missing source-media/article coverage and the one September 7 capture gap, then extend the retained interpretation window into late August. Reconcile post edits by source opportunity, not text similarity alone.
2. Independently compare a source-linked sample of each frequent Mike structure against images: side, type, expiration, strike, quantity ratio, and displayed price. Count unresolved media and edits in the denominator.
3. Prioritize butterfly (six openings) and call crab (four) for separate capability work. The calendar/diagonal code is available for a later bounded live promotion after Public payload, reconciliation, and Kamandal exit readback for that structure. Do not infer their readiness from a comma-separated Sheet edit.

The operator `trade_source_activity` brief should show each distinct opening as entered, queued, shadowed, unsupported, stale, or needing evidence, with a concrete reason and a preserved correction cell. The raw episode and order receipts remain in the local ledger.

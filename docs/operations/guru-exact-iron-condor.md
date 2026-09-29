# Exact Guru iron condors

`iron_condor` is supported by the independent source-contract verification and
exact-package route. The four source legs must be equal-quantity openings in a
single expiration, with valid defined-risk wings. Quotes must resolve every leg.
An SPX package must resolve to PM-settled `SPXW` contracts; the broker symbols are
retained through entry, lifecycle ownership, and the full-package close. Never
construct an SPX symbol from strikes alone.

The operator migration `scripts/apply_guru_iron_condor_sheet.py` defaults to a
read-only preview. `--apply` adds `guru_exact_iron_condor` and adds `iron_condor` to
both existing exact source allowlists in one atomic Sheet request. It preserves
source modes, existing playbooks, universe, and sleeve limits. It inherits the
ordinary condor's liquidity, credit, profit, loss, and per-order risk limits,
requires the existing cap to be at most $500, and limits entries to one contract.
The new row accepts only independently verified exact packages, not inferred
ideas. Existing source switches continue to control new entries.

The new policy permits 0–50 DTE (or a lower inherited maximum). Same-day entries
stop 60 minutes before the configured session close; the full-package time exit
becomes due 30 minutes before that close. Early-close configuration is respected.
These two buffers live in `management_policy_json.lifecycle`; compilation rejects
missing or invalid buffers, nonzero `exit_dte_min`, and half-time exits on this
route. Profit, loss, ownership, working-order, quote, and submission checks remain
in force. Timed exit is an instruction to attempt a close, not a guaranteed fill.

No source timestamp is refreshed. For an opening condor in a differently classified
post, the configured exact-opening age limit is used; expiry-day entry cutoff
further shortens that limit. Old/expired contracts cannot reopen through this path.
Unsupported multi-package opening groups remain blocked. The last-mile sleeve
check retains the approved risk floor if broker preview BPR is smaller.

Validation covers Public INDEX requests, SPXW identity, regular/early-close windows,
policy failures, source age, exact entry → fill adoption → hold → four-leg close →
closed lifecycle, and the sleeve risk floor. Before activation, validate the live
Sheet and a non-order broker preview. Deploy at an idle boundary; prove actual
trading only through natural scheduled receipts. Never replay old openings or
trigger trading jobs to establish proof.

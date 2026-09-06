# Kamandal V2 Sheet Schema

Date: 2026-09-04
Status: trade-source controls and richer source-episode activity projection deployed; natural Sheet readback pending

The currently deployed policy uses these tabs:

- `universe`
- `playbooks`
- `daily_plan`

The approved trade-source migration adds:

- `trade_sources` — operator-owned source routing;
- `trade_source_activity` — machine-owned, bounded observation projection.

Do not create or edit these tabs manually before the matching code and atomic
Sheet migration are ready. See [Trade Source Routing](TRADE_SOURCE_ROUTING.md).

Runtime control is intentionally not a sheet tab for now. Keep these in env or
local config:

```text
KAMANDAL_MODE=shadow        # shadow | live
KAMANDAL_TRADING_ENABLED=false
KAMANDAL_HALT=false
KAMANDAL_SHEET_ID=
GOOGLE_API_CREDENTIALS_PATH=
KAMANDAL_LLM_PROVIDER=codex_cli
KAMANDAL_LLM_MODEL=
```

## `universe`

The operator surface has exactly three columns:

```text
symbol
enabled
notes
```

`enabled=TRUE` admits a symbol for consideration. FALSE (including a blank
cell) prevents new candidates. Keep disabled rows to remember a rejection and
prevent repeated proposals. Notes are optional operator context.

No symbol-level profile, strategy allowlist, IV range, BPR/position limit,
earnings settings, or tier participates in eligibility. Legacy snapshots may
still contain those fields; they are ignored. Legacy `playbooks.profiles` is
also ignored, so deleting a universe column cannot leave a hidden profile veto.
Strategy IV/DTE/delta/event/liquidity rules, source-routing policies, and the
portfolio/risk manager continue to own their respective decisions.

The Friday 10:00 CT discovery review appends at most five disabled proposals
per UTC day. New rows contain only symbol, FALSE, and a short note. Full source,
reason, date, and market-check evidence live in `universe_review_commits.payload`
alongside the existing discovery ledger. Exact publication readback precedes
commit; previews and failed publications do not advance the review boundary.
The legacy `propose-universe-symbols --write-sheet` uses this same workflow and
ledger cap. No per-row approval tier or extra operator tab is required.

Migration 2026-09-06: the complete pre-migration Sheet and policy tables are
archived in `outputs/minimal-universe-20260906/` on development and oldmac.
This supersedes the September 5 proposal completion template. Existing enabled
values and operator notes are preserved; obsolete generated policy guidance is
removed from machine proposal notes. See
[the migration and risk audit](reviews/minimal-universe-2026-09-06.md).

## `playbooks`

This is where trader knowledge lives and grows over time.

Suggested columns:

```text
playbook_id
enabled
strategy_family
structure
variant
leg_count
profiles
iv_percentile_min
iv_percentile_max
iv_rank_min
iv_rank_max
iv_abs_min
iv_abs_max
dte_min
dte_max
short_delta_min
short_delta_max
long_delta_min
long_delta_max
spread_width
min_credit_to_width_ratio
max_debit_pct_bpr
live_max_bpr_per_order
profit_target_pct
max_loss_multiple
exit_dte_min
half_time_exit
avoid_earnings
universe_expansion_enabled
underlying_price_min
underlying_price_max
csa_stage
source_mode
management_policy_json
notes
accepted_inputs
```

Examples:

- `put_spread_standard`
- `iron_condor_high_iv`
- `call_calendar`
- `earnings_call_calendar`

Notes:

- `enabled`: true/false.
- `structure`: mechanical structure, such as `put_spread`, `iron_condor`,
  `call_calendar`.
- `variant`: context-specific flavor, such as `standard`, `high_iv`, or
  `earnings`.
- `max_loss_multiple`: for credit spreads, the close-debit multiple of entry
  credit that starts loss-watch review. A value of `2` means a $1.00 credit
  spread enters loss-watch when the mid close debit reaches about $2.00.
  Runtime config can require repeated loss-watch observations before a review
  or close recommendation is surfaced.
  For a debit directional diagonal, the same legacy column is interpreted as
  the fraction of entry debit lost and must be in `(0, 1]`; for example `0.5`
  means a 50% loss of entry debit. A value above `1` is unreachable for an
  ordinary debit package and fails policy compilation.
  Live behavior can be flipped without changing the sheet by setting
  `KAMANDAL_EXIT_MAX_LOSS_ACTION`, `KAMANDAL_EXIT_LOSS_WATCH_CONFIRMATIONS_REQUIRED`,
  or `KAMANDAL_EXIT_LOSS_WATCH_WINDOW_MINUTES` in the runtime environment.
- `max_bid_ask_pct`: the per-playbook quote-quality limit for both entry and
  management. Every open lifecycle freezes this value with its policy. If any
  active leg or the package fails the limit, Kamandal records a wide-quote hold
  and may not use that observation for a price-derived profit, loss, or
  adjustment action. Natural price remains execution evidence; midpoint is the
  economic decision mark after quote validation.
- `live_max_bpr_per_order`: authoritative Sheet-owned dollar cap for one live
  contract. It also bounds the worst entry debit after conversion to the
  per-share option price used by the broker.
- `max_debit_to_width_ratio`: directional-diagonal-only hard entry gate. The
  absolute midpoint debit divided by the resulting strike distance must not
  exceed this fraction; `0.75` means the debit may be at most 75% of width.
  This field does not choose or constrain width. It rejects structurally poor
  economics after both legs have independently matched their Sheet delta/DTE
  windows.
- `max_debit_pct_bpr`: retained only for existing-Sheet compatibility. Current
  rows contain mixed fraction/percent units, so this field does not authorize
  an entry price. Do not edit it to tune live entry behavior; use
  `live_max_bpr_per_order` until a protected Sheet migration defines one unit
  and updates every row.
- `iv_percentile_min/max`: optional distribution percentile gate.
- `iv_rank_min/max`: optional min/max-rank gate. Tastytrade-native IV Rank is
  preferred; a labeled local 252-day formula is the fallback.
- `universe_expansion_enabled`: optional operator switch. For a short-strangle
  row, `TRUE` allows already-enabled universe symbols outside the row's normal
  profile/allowlist routing to be considered. It does not add symbols or bypass
  any other gate. For `short_strangle_high_iv`, the operator-approved intent is
  all enabled universe profiles rather than indices only. Its IV-percentile
  cells are intentionally blank: IV Rank is the volatility admission signal,
  while IV and IV percentile remain stored research/reporting evidence.
- `underlying_price_min/max`: required Sheet-owned bounds when universe expansion
  is enabled. The same row's `iv_rank_min/max` are also required. Missing values
  fail closed; the repository provides no fallback range.
- `csa_stage` (column BA): deprecated compatibility evidence from the cutover.
  It does not select an active runtime path when `mode` is present. `mode` is
  the authoritative `shadow|live` operator switch; no new policy should be
  authored against `csa_stage`.
- `source_mode` (column BB): currently `idea`, `market_scan`,
  `portfolio_hedge`, or legacy shadow-only `observed_package`. It becomes a
  compatibility column after the trade-source migration; no new policy is
  authored against it.
- `accepted_inputs`: approved pending replacement for `source_mode`. Allowed
  values are any comma-separated combination of `idea`, `market_scan`,
  `portfolio_hedge`, and `exact_package`. Migration begins from each row's
  current `source_mode`, so existing non-idea lanes do not change. A blank
  historical `source_mode` resolves to `idea`, but every enabled row in the new
  Sheet must be explicit. Initially, exactly one enabled call calendar, put
  calendar, call diagonal, and put diagonal playbook may accept exact packages.
  An exact package keeps every observed contract term, receives canonical leg
  roles, and uses that existing playbook's eligibility, portfolio, effect, and
  lifecycle policy. Zero matching managers park as `unsupported`; multiple
  matches park as `ambiguous_playbook_match`. The migration appends this field
  after every currently deployed playbook column; it does not shift the stable
  positions of `management_policy_json` or `notes`.
- `source_profiles`: legacy person-specific observed-package allowlist. It is
  removed with the four `mike_*_observed` rows during the trade-source migration;
  `trade_sources` becomes the only source authorization surface.
- `management_policy_json` (column BC): operator-visible CSA lifecycle policy
  that is not already represented by a normal playbook column. It must contain a
  non-empty `lifecycle` object. The existing `score_weight_credit/pop/liquidity/spread`
  columns remain canonical for scoring; duplicating `score_weights` inside JSON
  fails closed. CSA does not supply repository numeric fallbacks for missing values.
- The protected unified cutover appended (without moving existing columns)
  `mode`, explicit strangle management controls, and event-calendar timing
  controls. `mode` now wins; `csa_stage` remains read-only compatibility
  evidence until a separately reviewed cleanup removes the redundant column.
  The cutover manifest, not this document, is the authority for exact Sheet
  ranges and validation copy.
- `iv_abs_min/max`: optional absolute ATM IV gate, useful for avoiding
  low-volatility false positives.
- `execution_venue`: immutable route for newly created trades. Supported values
  are `public_primary` and `tasty_primary`. A later Sheet flip affects only new
  candidates; an open lifecycle and every adjustment/close retain the venue
  frozen at entry. All ordinary strategy rows remain `public_primary`; only the
  short-strangle row is routed to `tasty_primary`, and that row remains
  `shadow`. Changing a row never migrates an existing position.
- `target_dte`: preferred expiration inside the inclusive `dte_min/max` window.
  The short-strangle row uses 45 inside 35-50; the builder chooses the nearest
  available expiration without weakening the hard window.
- Directional diagonals intentionally leave `target_dte` and `spread_width`
  blank. The builder targets the midpoint of the short and long DTE/delta
  windows independently. Blank `spread_width` means no width constraint, not
  `$5` and not one strike-grid interval. The current call/put mirror policy is
  short 20-30 DTE at 20-30 absolute delta and long 45-60 DTE at 40-55 absolute
  delta. If either hard window has no valid liquid leg, no candidate is built.
- `range_gate_required` and `range_gate_max_age_days`: retired compatibility
  columns. They are retained so historical snapshots and all later appended
  columns keep stable positions, but the compiler and planner ignore them. The
  canonical short-strangle row sets the former to `FALSE` and leaves the latter
  blank.
- `loss_close_multiple`: canonical package-loss close threshold for strangles.
  The short-strangle policy uses 3x entry credit. The older
  `max_loss_multiple` remains the ordinary credit-spread loss-watch control and
  must not override this strangle lifecycle value.
- `half_time_exit`: true/false. If true, the engine can recommend exit around
  half the original DTE. The unified manager measures original DTE from the
  completed opening fill to the earliest active expiration and closes the full
  strategy package; it does not manage one leg independently.
- For directional diagonals, `exit_dte_min` is evaluated on the near short leg
  and closes both legs. It is never evaluated only on the far long leg.
- `exit_pre_event_days`: optional nonnegative calendar-day threshold. For
  ordinary strategies, diagonals, and strangles, the unified manager compares
  it with the latest captured earnings date and closes the full package when
  due. Earnings-calendar rows intentionally leave this blank because their
  separate contract holds through the confirmed event and exits afterward.
- Shadow rows use these same frozen management fields after entry. Their final
  adapter remains broker-inert and quote-based: a selected entry may work across
  bounded retries or become `entry_missed`; shadow does not use live Plan 2.
- `resting_profit_enabled`: explicit per-playbook permission for a non-conceding
  full-package DAY limit at the frozen profit target. Missing is invalid on an
  enabled row. The runtime live/shadow switches may disable this permission but
  cannot create it.
- `resting_profit_arm_progress_pct`: midpoint progress, from 0 through 100,
  required before that exact target offer may be staged. This value freezes on
  lifecycle open with the rest of the compiled policy.

The 2026-08-23 corrective management design originally required no new Sheet
column. The later autonomous-strangle migration appended five controls for
venue routing, preferred DTE, the chart-range gate, and one canonical 3x
strangle loss close. The resting-profit extension appends two more controls so
permission and arming economics remain on the operator surface. The
existing `mode`, `max_bid_ask_pct`, `profit_target_pct`, `max_loss_multiple`,
`half_time_exit`, `exit_pre_event_days`, and lifecycle JSON already express the
operator-owned strategy policy. Opening/closing adverse-loss buffers,
confirmation semantics, quote validity, and execution-envelope preservation
are shared platform safety behavior. For a short strangle,
`management_policy_json.lifecycle.loss_stages` must retain both the explicit
`watch_multiple` and `close_multiple`; the dedicated `loss_close_multiple`
column is the canonical close threshold and must agree with the JSON close
value during migration. Before deployment, Old Mac must run
`kamandal validate-sheet-policy`. That read-only command validates every enabled
row, including `max_bid_ask_pct`, against the planner, unified compiler, and
strict CSA compatibility compiler from one live Sheet snapshot. Kamandal must
not silently invent a fallback for a missing operator value.

## `trade_sources`

Operator-owned routing ceiling for translated people or feeds. There is exactly
one row per `(source_id, output_kind)` and therefore two rows per source in the
initial contract.

```text
source_id
output_kind
mode
notes
```

Notes:

- `output_kind`: `idea` or `exact_package`.
- `mode`: `off`, `observe`, `shadow`, or `live`.
- The source mode is a ceiling, not execution permission. The effective mode is
  the safer of source mode and matched playbook mode, followed by all existing
  portfolio and safety gates.
- `exact_package=live` is invalid in the first release. Exact packages remain
  broker-inert shadow even if a broader source or playbook mode says `live`.
- Missing, duplicate, or invalid rows fail only that source activation closed.
  They do not block management or exits for existing positions.

## `trade_source_activity`

Machine-owned, bounded projection of canonical trade-source receipts. This tab
is for observation and debugging; it is not policy or a database.

```text
observed_at
source_id
post_ref
output_id
acquisition_status
classification
normalized_output
action
symbol
structure
link_status
evidence_status
interpretation_confidence
capability_support
planner_disposition
effective_mode
reason
```

Every normalized output receives one row, including residual, unsupported, and
ambiguous results. A projection outage retries from canonical receipts and must
not block planning, existing lifecycle management, exits, or reconciliation.

The additional interpretation columns are machine-written observations, not
operator controls. Confidence is informational and cannot bypass missing
evidence, exact-package validation, source mode, playbook mode, portfolio gates,
or broker safety. The only source controls remain the two `trade_sources` rows.

## `daily_plan`

Engine-written ranked portfolio plans for operator visibility and audit. In
automatic mode this is not a trade-by-trade approval queue.

The planner should not only rank individual trade candidates. It should rank
combinations of candidates. For example, 20 scraped/generated ideas might reduce
to:

- plan rank 1: a bundle of 3 trades that best fits buying power and Greeks
- plan rank 2: a bundle of 5 smaller trades
- plan rank 3: a single strong trade

The operator chooses one whole plan. In auto mode, the machine can choose the
top-ranked eligible plan.

Keep this sheet plan-level: one row per plan. Candidate-level leg details,
per-trade Greeks, exact order payloads, and preflight responses belong in local
SQLite/audit files. The sheet should answer, "Which portfolio plan should I
choose?", not force the operator to reconstruct a plan from many trade rows.

JSON in a cell is allowed where it improves visibility without exploding the
sheet into many tabs or many repeated rows. The human-readable columns should be
good enough for quick selection; JSON columns provide drilldown.

Suggested columns:

```text
plan_date
plan_rank
plan_id
plan_status
plan_trade_count
plan_score
plan_summary
trade_bundle
trade_bundle_json
plan_total_bpr
plan_bpr_utilization_pct
buying_power_after
portfolio_delta_before
portfolio_delta_after
portfolio_delta_change
portfolio_gamma_before
portfolio_gamma_after
portfolio_gamma_change
portfolio_theta_before
portfolio_theta_after
portfolio_theta_change
portfolio_vega_before
portfolio_vega_after
portfolio_vega_change
mode
plan_reasons
blocked_by
plan_metrics_json
plan_detail_json
operator_action
operator_notes
```

Notes:

- `mode` mirrors env at generation time: `shadow` or `live`.
- `plan_rank` ranks the bundle.
- `trade_bundle` is a compact human-readable summary, such as
  `SPY put_spread; TLT short_put; QQQ call_spread`.
- `trade_bundle_json` contains the structured list of trades in the plan:
  candidate id, idea id, underlying, playbook, structure, expirations, strikes,
  credit/debit, BPR, and compact reasons.
- `plan_reasons` explains the portfolio-level decision: BPR fit, delta/gamma
  shape, theta improvement, concentration, event risk, and IV fit.
- `plan_metrics_json` contains the structured before/after/change metrics used
  by the scorer.
- `plan_detail_json` contains the full plan object suitable for local replay or
  debugging. It should mirror a local SQLite/audit record id when available.
- `operator_action` applies to the whole plan; later values can be `approve`,
  `reject`, or `hold`.
- Exact order payloads, preflight responses, fills, and grouped positions stay
  local in SQLite/audit files rather than expanding the sheet.

## Digest Output

Do not create a digest tab yet. Write digest/newsletter output as local Markdown:

```text
data/digest/YYYY-MM-DD.md
```

This keeps Google Sheets light while preserving human-readable summaries.

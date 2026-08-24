# Kamandal V2 Sheet Schema

Date: 2026-08-22
Status: Current lean operator contract

Create one blank Google Sheet with these tabs:

- `universe`
- `playbooks`
- `daily_plan`

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

Operator-owned tradable universe and per-symbol/profile constraints.

Suggested columns:

```text
symbol
enabled
profile
tradable_iv_percentile_min
tradable_iv_percentile_max
max_bpr_pct
max_positions
earnings_sensitive
event_avoid_days_before
event_avoid_days_after
allowed_playbooks
notes
```

Notes:

- `enabled`: true/false.
- `profile`: for grouping, such as `index_etf`, `liquid_single_name`,
  `bond_etf`, `commodity_etf`.
- `tradable_iv_percentile_min/max`: the IV percentile range where the symbol is
  eligible for new entries.
- `allowed_playbooks`: optional comma-separated allowlist. Blank means use all
  enabled playbooks compatible with the profile.

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
- `max_debit_pct_bpr`: retained only for existing-Sheet compatibility. Current
  rows contain mixed fraction/percent units, so this field does not authorize
  an entry price. Do not edit it to tune live entry behavior; use
  `live_max_bpr_per_order` until a protected Sheet migration defines one unit
  and updates every row.
- `iv_percentile_min/max`: optional distribution percentile gate.
- `iv_rank_min/max`: optional min/max-rank gate against the local lookback.
- `universe_expansion_enabled`: optional operator switch. For a short-strangle
  row, `TRUE` allows already-enabled universe symbols outside the row's normal
  profile/allowlist routing to be considered. It does not add symbols or bypass
  any other gate.
- `underlying_price_min/max`: required Sheet-owned bounds when universe expansion
  is enabled. The same row's `iv_rank_min/max` are also required. Missing values
  fail closed; the repository provides no fallback range.
- `csa_stage` (column BA): deprecated compatibility evidence from the cutover.
  It does not select an active runtime path when `mode` is present. `mode` is
  the authoritative `shadow|live` operator switch; no new policy should be
  authored against `csa_stage`.
- `source_mode` (column BB): `idea`, `market_scan`, or `portfolio_hedge`. The
  value must be compatible with the row's structure and fails closed otherwise.
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
- `range_gate_required`: when true, eligible candidates must receive a fresh
  `confirmed_range` answer from Market Cartographer before optimization. A
  missing, stale, failed, or broken-range answer rejects that candidate only.
- `range_gate_max_age_days`: maximum age of the Cartographer daily observation.
- `loss_close_multiple`: canonical package-loss close threshold for strangles.
  The short-strangle policy uses 3x entry credit. The older
  `max_loss_multiple` remains the ordinary credit-spread loss-watch control and
  must not override this strangle lifecycle value.
- `half_time_exit`: true/false. If true, the engine can recommend exit around
  half the original DTE. The unified manager measures original DTE from the
  completed opening fill to the earliest active expiration and closes the full
  strategy package; it does not manage one leg independently.
- `exit_pre_event_days`: optional nonnegative calendar-day threshold. For
  ordinary strategies, diagonals, and strangles, the unified manager compares
  it with the latest captured earnings date and closes the full package when
  due. Earnings-calendar rows intentionally leave this blank because their
  separate contract holds through the confirmed event and exits afterward.
- Shadow rows use these same frozen management fields after entry. Their final
  adapter remains broker-inert and quote-based: a selected entry may work across
  bounded retries or become `entry_missed`; shadow does not use live Plan 2.

The 2026-08-23 corrective management design originally required no new Sheet
column. The later autonomous-strangle migration appends five controls for venue
routing, preferred DTE, the chart-range gate, and one canonical 3x strangle
loss close. The
existing `mode`, `max_bid_ask_pct`, `profit_target_pct`, `max_loss_multiple`,
`half_time_exit`, `exit_pre_event_days`, and lifecycle JSON already express the
operator-owned strategy policy. Opening/closing adverse-loss buffers,
confirmation semantics, quote validity, and execution-envelope preservation
are shared platform safety behavior. Before deployment, Kamandal must read back
that every enabled row has a valid `max_bid_ask_pct`; it must not silently
invent a fallback for a missing value.

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

# Kamandal V2 Sheet Schema

Date: 2026-04-25
Status: Initial blank-sheet schema

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
dte_min
dte_max
short_delta_min
short_delta_max
long_delta_min
long_delta_max
spread_width
min_credit_to_width_ratio
max_debit_pct_bpr
profit_target_pct
max_loss_multiple
exit_dte_min
half_time_exit
avoid_earnings
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
- `half_time_exit`: true/false. If true, the engine can recommend exit around
  half the original DTE.

## `daily_plan`

Engine-written ranked portfolio plans for human review.

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
operator_action
operator_notes
```

Notes:

- `mode` mirrors env at generation time: `shadow` or `live`.
- `plan_rank` ranks the bundle.
- `trade_bundle` is a compact human-readable summary, such as
  `SPY put_spread; TLT short_put; QQQ call_spread`.
- `plan_reasons` explains the portfolio-level decision: BPR fit, delta/gamma
  shape, theta improvement, concentration, event risk, and IV fit.
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

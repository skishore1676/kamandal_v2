# Build Inputs Needed

Date: 2026-04-25
Status: Historical input snapshot; scaffold, sheet load, shadow loop, and
oldmac scheduling are now implemented. See `README.md` for the current runbook.

I do not have more conceptual blockers. The initial concrete inputs from Suman
have been captured below.

## 1. Google Sheet

- Create a blank Google Sheet with tabs:
  - `universe`
  - `playbooks`
  - `daily_plan`
- Sheet URL:
  `https://docs.google.com/spreadsheets/d/16Vjgrj80VDeTIGg0y60w4LHenZg7R-tGGvOyLNFdFsE/edit`
- Codex writes the headers from `docs/SHEET_SCHEMA.md`.

## 2. Credentials And Env

Place these locally, not in chat:

```text
KAMANDAL_SHEET_ID=
GOOGLE_API_CREDENTIALS_PATH=
PUBLIC_SECRET_TOKEN=
PUBLIC_ACCOUNT_ID=
KAMANDAL_MODE=shadow
KAMANDAL_TRADING_ENABLED=false
KAMANDAL_HALT=false
KAMANDAL_LLM_PROVIDER=codex_cli
```

Google credentials are referenced from old Kamandal's access pattern:
`../public_api_trading_v3/config/google-credentials.json`.

Public credentials can be copied locally from old Kamandal's `.env` when needed,
but should not be printed or committed.

## 3. Initial Universe

Seed from old Kamandal's cached/configured universe.

```text
symbol, profile, tradable_iv_percentile_min, tradable_iv_percentile_max,
max_bpr_pct, max_positions, earnings_sensitive
```

Example profiles:

- `index_etf`
- `liquid_single_name`
- `bond_etf`
- `commodity_etf`

## 4. Initial Playbooks

Seed from old Kamandal's template library and strategy/profile approvals, with
the new vision's lean surface.

Suggested first set:

- `short_put`
- `put_spread`
- `call_spread`
- `iron_condor`
- `call_calendar`

For each, the useful fields are:

```text
enabled, profiles, iv_percentile_min/max, dte_min/max,
short_delta_min/max, long_delta_min/max, spread_width,
min_credit_to_width_ratio, max_debit_pct_bpr,
profit_target_pct, max_loss_multiple, exit_dte_min, half_time_exit,
avoid_earnings
```

## 5. Portfolio Guardrails

Initial account-level constraints:

- target max BPR utilization: 90%
- hard max BPR utilization: 90%
- max BPR per underlying/position seed: 25%
- max positions: 5
- delta: slightly negative portfolio delta preferred
- gamma/theta: record and research rather than force an early preference
- vega: record for now

## 6. Approval Behavior

Choose the first operating behavior:

- `daily_plan_only`: write ranked plans, no preflight
- `shadow_preflight_after_approval`: preflight rows only when
  `operator_action=approve`
- `shadow_auto_top_plan`: auto-select the top eligible shadow plan for loop
  testing, without live order submission

Current mode: `shadow_auto_top_plan`, controlled by
`execution.approval_mode` or `KAMANDAL_APPROVAL_MODE`.

## 7. Build Permission

Confirmed: scaffold the Python project in `kamandal_v2` and copy selected code
or patterns from old `kamandal`/`bhiksha` where it cleanly fits, while keeping
the new design lean.

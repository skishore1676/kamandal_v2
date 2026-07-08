# Playbook Expectancy Scorecard

Status: **design approved, ready to implement** (2026-07-08)
Owners: Suman (product) + Claude (design/review) + implementing agent
Scope: read-only analytics over existing ledger data + one new sheet tab +
weekly reviewer integration. No trading-path changes.

---

## 1. Problem

Every tuning decision so far (profit target 40→50, stop 2.0→1.5x, credit gate
→0.28) was made from one month of hand-queried data. There is no standing
answer to "which playbooks make money?" even though the data exists:
`live_position_groups` + `live_position_marks` (live lane), `shadow_fills` +
`shadow_marks` (shadow lane), `live_order_attempts` (actual broker fills).

Concrete stake: June's closed verticals ran 56% win rate with avg win $92 vs
avg loss $143 — negative expectancy that nobody saw for a month because no
report computed it.

## 2. Design

### 2.1 Metrics, per playbook × lane (live / shadow)

For trailing windows of 30 days, 60 days, and all-time:

| metric | definition |
|---|---|
| `trades_closed` | closed position groups in window |
| `win_rate` | share with pnl > 0 |
| `avg_win`, `avg_loss` | mean pnl of winners / losers (dollars) |
| `expectancy` | mean pnl per trade |
| `expectancy_per_bpr` | mean (pnl / entry BPR) — capital-normalized edge |
| `avg_hold_days` | close − open |
| `mfe_capture` | mean (realized pnl / MFE) over winners — are exits leaving money? |
| `mae_breach` | share of losers whose MAE exceeded the stop threshold — is the stop working? |
| `current_streak` | signed consecutive wins(+)/losses(−), most recent first |
| `pnl_source` | `broker_fill` or `last_mark` (see 2.2) |

### 2.2 P&L source hierarchy — be honest about it

1. **Broker fill prices** where recoverable: entry fill from the open order's
   recorded status/attempts, exit fill from the close order's. Label
   `broker_fill`.
2. Fallback: last `live_position_marks.pnl` before close. Label `last_mark`.

Never mix silently — the report carries the label per playbook (worst label
among its trades). Shadow lane always uses shadow marks and is labeled
`shadow_mark`; shadow and live are **never aggregated together**.

### 2.3 Outputs

1. `kamandal playbook-scorecard [--window 30|60|all] [--write-sheet]` CLI —
   prints the table, writes `data/reports/playbook_scorecard_<date>.json`.
2. New sheet tab `playbook_scorecard` (replace-tab write, like `live_book`):
   one row per playbook × lane × window, plus a `recommendation` column.
3. Weekly reviewer (Friday job) embeds the 30-day table in its markdown
   report and calls out any playbook with `trades_closed ≥ 5` and negative
   expectancy.

### 2.4 Recommendation column — advisory only, never auto-acting

- `keep` — expectancy > 0 over trailing 30d (or insufficient data, < 5 trades:
  `insufficient_data`)
- `review` — negative expectancy, 5–9 closed trades
- `disable_candidate` — negative expectancy, ≥ 10 closed trades

The system NEVER flips a playbook's `enabled` cell itself. The operator reads
the scorecard and edits the playbooks tab. (Rationale: playbook enable/disable
is a live-money policy surface; see the trader's history of automation trust —
recommendations earn trust before actions do.)

## 3. Implementation plan

1. New module `src/kamandal_v2/analytics/playbook_scorecard.py`:
   `compute_scorecard(store, *, windows=(30, 60, None)) -> dict` — pure
   function over `LocalStore`, no network. Broker-fill recovery via existing
   store accessors for order attempts/status; do not add new tables.
2. Store additions only if strictly needed (prefer existing accessors:
   `closed_live_position_groups` exists since the risk-manager work).
3. CLI command in `cli.py` (`playbook-scorecard`), sheet writer using the
   `replace_tab` pattern, header in `schemas.py` (`PLAYBOOK_SCORECARD_HEADER`).
4. Weekly reviewer hook: `review-rejections` path appends the 30d table.
5. Tests (`tests/test_playbook_scorecard.py`): synthetic store with known
   closes → exact expectancy/win-rate assertions; broker-fill vs last-mark
   labeling; shadow/live separation; empty store → clean empty output;
   streak sign correctness.

## 4. Acceptance

- Running the CLI on oldmac's real DB produces a table where June's numbers
  reproduce the known baseline (put_spread expectancy ≈ −$7.8/trade on 12
  closed, long_call +$395 on 1) — sanity anchor, exact values may differ
  slightly by pnl_source.
- Sheet tab appears and refreshes on the weekly job without touching other
  tabs.
- Zero writes to any trading table; job is safe to run any time.

## 5. Review checklist

- [ ] Shadow and live never aggregated; labels correct
- [ ] pnl_source hierarchy implemented and surfaced, not silent
- [ ] No auto-disable, no writes to `playbooks` tab
- [ ] Pure compute module — analytics importable without broker/sheet deps
- [ ] Windows computed from close time, not open time

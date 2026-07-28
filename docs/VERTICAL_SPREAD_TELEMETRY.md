# Vertical Spread Prospective Telemetry v1

This is an isolated, prospective quote collector for the vertical-credit exit
research. It uses Kamandal's Public option-chain adapter but does **not** invoke
the planner, portfolio optimizer, preflight, order-ticket construction, Google
Sheets, live ledger, or broker order submission.

## Safety boundary

Every invocation deep-copies the normal Kamandal config and forces:

```yaml
runtime:
  mode: shadow
  trading_enabled: false
execution:
  submit_to_broker: false
live:
  auto_submit_entries: false
  auto_submit_exits: false
  entry_approval_mode: disabled
  exit_approval_mode: disabled
```

A fail-closed assertion checks these values before any chain request. The
collector writes only to its dedicated SQLite database:

```text
data/research/vertical_telemetry_v1.db
```

It never writes `data/kamandal_v2.db`.

## What it captures

At a real Mala opportunity, open observation lanes using the actual live chain:

- nearest listed expiration to requested 1/3/7/14 DTE,
- 30-delta short and 15-delta long leg,
- strict bull-put or bear-call ordering,
- actual bid, ask, midpoint, Greeks, OI, and volume,
- midpoint entry credit,
- natural entry credit (`short bid - long ask`),
- spread width and estimated maximum buying-power risk.

The entry snapshot is stored as mark 0. Every later mark retrieves the exact
same contracts and stores:

- midpoint closing debit,
- natural closing debit (`short ask - long bid`),
- midpoint and natural P&L,
- midpoint and natural credit capture,
- peak natural capture,
- current short-leg delta,
- missing-quote status.

The immediate entry mark intentionally shows the round-trip cost implied by the
same quote snapshot.

## Commands

Open one observation family after a bullish IWM signal:

```bash
uv run kamandal-vertical-telemetry \
  --config config/control.yaml \
  --db data/research/vertical_telemetry_v1.db \
  open --idea IWM:LONG:mala_event_123 --dtes 1,3,7,14
```

Mark all open lanes on the five-minute schedule:

```bash
uv run kamandal-vertical-telemetry \
  --db data/research/vertical_telemetry_v1.db \
  mark
```

Read coverage and latest marks:

```bash
uv run kamandal-vertical-telemetry \
  --db data/research/vertical_telemetry_v1.db \
  report
```

## Rollout

1. Start with manual `open` calls tied to real Mala events.
2. Schedule `mark` every five minutes only after a successful Public-chain smoke
   test.
3. Each lane completes after its entry snapshot plus 24 future marks, or at the
   session boundary. Collect at least 30 sessions before comparing DTE and exit
   policies.
4. Treat natural P&L as primary and midpoint P&L as diagnostic.
5. Do not create full shadow positions or modify live trades until the corrected
   market_pulse v2 experiment produces a frozen paper candidate.

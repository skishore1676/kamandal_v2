# Improvement Queue

How we drive changes (established with the exit-pipeline rework, 2026-07-02):

1. **Design doc** — problem evidence, design, implementation plan, rollout
   flag, acceptance criteria, review checklist. Written/approved here first.
2. **Implementation** — a coding agent (Codex / Sonnet) builds exactly to the
   doc, tests included, behind the doc's rollout flag.
3. **Review** — independent audit of the implementation against the doc's
   review checklist plus a production readback after the flag flips.

Every change ships dark behind a flag; flips happen in oldmac `.env` during a
watched session; rollback is flipping the flag back.

## Queue (2026-07-08)

| # | Doc | Priority | Status |
|---|-----|----------|--------|
| 1 | [CANDIDATE_GATE_SEARCH.md](CANDIDATE_GATE_SEARCH.md) — builder width search so verticals solve the 0.28 credit gate instead of dying on it | P0 — currently zero eligible verticals in afternoon runs | ready to implement |
| 2 | [PLAYBOOK_EXPECTANCY_REPORT.md](PLAYBOOK_EXPECTANCY_REPORT.md) — per-playbook scorecard so tuning stops being anecdotal | P1 | ready to implement |
| 3 | [HOUSE_IDEAS_IV_SCAN.md](HOUSE_IDEAS_IV_SCAN.md) — IV-regime house ideas feeding neutral/vol structures | P1 (enable after risk manager is on) | ready to implement |
| 4 | Max-loss close escalation alert (spec inline below) | P2 — small | ready to implement |
| — | [EXIT_PIPELINE_DESIGN.md](EXIT_PIPELINE_DESIGN.md) | shipped 2026-07-02, flag live | done; residuals tracked below |

## Ops actions (no code)

- **Flip the risk manager on** (`KAMANDAL_RISK_MANAGER_ENABLED=true` in oldmac
  `.env`). All breakers built and tested 2026-07-02; book is small (3
  positions) so now is the cheap time. Note: the live book currently holds two
  same-direction NVDA positions — exactly what the cluster caps manage.
- **Watch the first live close** through the new ledger/ladder path end to
  end (floor annotation fixed in code but unproven in production).

## Inline spec: max-loss close escalation alert (small)

Problem: a `max_loss`/`pre_event` close that sits unfilled has no escalation —
first signal is the generic 120-minute stale yellow. Design intent
(EXIT_PIPELINE_DESIGN.md §4.5): alert the operator at ~30 minutes.

Implementation: in the close-order sweep (`sync_live_orders` close path or
`run_live_health`), when a broker-working close ticket with
`exit_reason in {max_loss, pre_event}` has age ≥
`live.exit_reprice.urgent_alert_after_minutes` (new knob, default 30), emit
`live_urgent_close_unfilled` event once per ticket (dedupe on ticket_hash) and
send a Lathi/Telegram alert through the existing ops-alert path. Health: the
finding escalates to RED. Tests: alert fires once, dedupes, only for urgent
reasons, only while broker-working. Env override:
`KAMANDAL_LIVE_URGENT_CLOSE_ALERT_AFTER_MINUTES`.

## Exit-pipeline residuals (from the 2026-07-02 audit)

- ~~Profit floor annotation never populates~~ — fixed in code; **verify on the
  first production close**.
- Close-reprice sign math assumes debit-closes — wrong for credit-received
  closes (calendars, longs). Gate the ladder to `entry_kind == "credit"` or
  make it sign-aware before calendars trade live.
- Reconcile 120-min expiry backstop doesn't cover
  `approved_close_pending_submit`.
- Sheet close lane is not yet the full mirror of §4.3 (audit-trail gap only).

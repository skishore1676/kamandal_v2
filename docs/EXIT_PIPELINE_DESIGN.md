# Exit Pipeline Redesign: Ledger-Authoritative Close Submission

Status: **implemented behind rollout flag** (2026-07-02)
Owners: Suman + Codex
Scope: live exit path only (`live-management` / `live-approved-orders` jobs). Entries are untouched except where noted.

---

## 1. Problem

The live exit pipeline deadlocks whenever more than one position needs closing at
the same time. Single closes work end-to-end; concurrent closes starve. Verified
on oldmac 2026-07-02 against `data/kamandal_v2.db`, the daily_plan sheet, and
launchd logs.

### Evidence trace (2026-07-02, times UTC)

- `16:15:04` reconcile expires the previous generation of close approvals
  (`expired_stale_close_approval`, age 134 min).
- `16:15:22-25` management decides `close` for **five** groups (GOOGL dte_target,
  QQQ profit_target, IWM/AMD/DELL dte_target), stages five
  `pending_close_approval` tickets, writes five `APPROVE_LIVE_CLOSE` rows.
- `16:15:35` the submitter processes **one** row (`rows[:1]`) — GOOGL submits and
  **fills**. The other four rows are never read.
- `16:30` next cycle: the four unsubmitted groups now hold local pending tickets,
  so their decision flips to `hold: working_close_order` and they emit **no
  rows**. GOOGL (not yet reconciled closed) re-stages, producing one row — and
  `write_daily_plan(replace_lanes={"live_close_advisory"})` **wipes the four
  still-approved rows** from the sheet.
- The four tickets sit local-only for 120 minutes, expire, re-stage — and the
  loop repeats. QQQ has cycled stage→expire→re-stage **since June 9** with zero
  broker submissions. Effective throughput with N ready closes: ~1 per 2.25 h.

### The three compounding defects

| # | Defect | Location |
|---|--------|----------|
| D1 | Submitter processes one approved sheet row per run (`rows[:1]`) | `src/kamandal_v2/live/execution.py` `execute_live_approved` |
| D2 | Lane replacement erases still-approved, never-submitted rows: each cycle rewrites the whole `live_close_advisory` lane with only newly-staged rows; holds emit no row | `src/kamandal_v2/live/management.py` (`write_daily_plan(..., replace_lanes=...)`) |
| D3 | Dedup treats a local `pending_close_approval` as a "working close order" for 120 min until expiry, blocking re-stage while nothing is at the broker | `src/kamandal_v2/live/management.py` `_working_close_order` + `NONTERMINAL_CLOSE_STATUSES` |

A fourth, secondary gap: close orders that do reach the broker have **no
reprice/expire management** (that machinery is entry-only, gated on
`intent_type == "open"`), and health labels local staged tickets
`working_close_order` (yellow), which hid this failure for three weeks.

### Root architectural cause

A human-approval surface (the Sheet) was repurposed as an execution queue for a
mode (`auto_rules`) where no human is in the loop. It inherited human-queue
semantics — one item at a time, stale items tidied away — and those semantics
are what starve concurrent exits. Hardening the bridge does not fix this; moving
execution authority to the ledger does.

---

## 2. Design principles

1. **The ledger (SQLite) is the exit state machine.** A policy decision to exit
   *is* the approval in `auto_rules` — staged→approved happens in the same
   transaction. No sheet round-trip on the execution path.
2. **The Sheet is a projection and a command channel, never a gate for auto
   exits.** State flows ledger→sheet (write-after audit). Operator intent flows
   sheet→ledger as explicit commands. State never flows sheet→execution.
3. **Exits are greedy.** Every approved close submits in the same run. Entries
   may queue; risk-shedding does not.
4. **Exits never silently give up.** Entry orders may expire into "no trade";
   a close's terminal state is *filled* or *escalated* (alert / resting floor
   order), never "quietly hold what policy said to sell".
5. **"Working" means the broker acknowledged it.** A local staged/approved
   intent is pipeline state, not book state; if it persists, that is an
   application failure and must be RED.

---

## 3. Target state machine (per close intent, in `live_order_intents`)

```
                       auto_rules: same transaction
 decision=close ──> close_staged ──────────────────────> close_approved
                        │                                      │
                        │ sheet_approval:                      │ submitter drains ALL
                        │ operator APPROVE_LIVE_CLOSE          v
                        └──────────────────────────────> submitted ──> close_filled
                                                              │   ^
                                              ladder step:    │   │ cancel+replace
                                                              └──> repriced (child)
 terminal failures: blocked_preflight_failed, submit_failed,
                    expired_eod (restaged next session), cancelled,
                    rejected_by_operator
```

Status mapping vs today (minimal migration):

| Today | Target | Notes |
|---|---|---|
| `pending_close_approval` | `close_staged` (sheet_approval mode only) | keeps its "waiting on human" meaning; **no longer used in auto_rules** |
| — (new) | `approved_close_pending_submit` | cleared to submit; in auto_rules this is the staging status |
| `submitted` / `repriced` | unchanged | the only statuses that count as "working" |
| `expired_stale_close_approval` | retained as backstop only | should never fire in auto mode once the drain works; firing = bug |

One active close intent per group, enforced at staging (existing ticket-hash +
seed-salt machinery keeps resubmission idempotent).

---

## 4. Component changes

### 4.1 Submitter (`execution.py`)

- New source for closes: drain the ledger —
  `live_order_intents_by_type("close", statuses={"approved_close_pending_submit"})`
  — processing **all** intents up to `live.max_close_submits_per_run`
  (default 10; a throttle backstop, not a queue).
- Each submission: fresh preflight (existing), submit, record status. Preflight
  failure → `blocked_preflight_failed` + health event (unchanged semantics).
- The Sheet is not consulted for auto closes. In `sheet_approval` mode, a
  pre-step reads operator rows and transitions `close_staged →
  approved_close_pending_submit` in the ledger; the same drain then runs.
- Entries keep their current `rows[:1]` behavior — out of scope here, noted as
  a separate decision.

### 4.2 Management (`management.py`)

- `auto_rules`: stage the ticket directly as `approved_close_pending_submit`
  and call the submitter in the same process pass (already the job sequence).
- Dedup: any nonterminal intent still suppresses staging a *duplicate*, but the
  suppressed state is now visible (see 4.4) and self-heals because the drain
  picks up approved intents regardless of which cycle staged them.
- Stale-approved refresh: if an approved-unsubmitted intent is older than one
  cycle, management re-tickets it at current quotes (supersede, don't
  duplicate) rather than waiting for the 120-min expiry.

### 4.3 Sheet projection (`management.py` + `sheets.py`)

- The `live_close_advisory` lane becomes a **full mirror**: each cycle projects
  *all* of today's close intents (staged / approved / submitted / filled /
  failed) with their current status, order id, and fill info. Lane replacement
  is then safe by construction — the lane is derived state.
- Sheet write failure: event + Lathi alert; the exit path proceeds regardless.
- Operator command channel (auto mode): setting `operator_action=REJECT_CLOSE`
  on a projected row cancels/retires that intent on the next cycle.
  `APPROVE_LIVE_CLOSE` remains the gate only in `sheet_approval` mode.

### 4.4 Health (`health.py`)

- `working_close_order` → only for broker-acknowledged tickets
  (`submitted`/`repriced` with recent broker status).
- New RED reason `exit_pipeline_stalled`: an
  `approved_close_pending_submit` intent older than 2 cycles while
  `exit_approval_mode=auto_rules`. This is the alarm that was missing in June.
- `close_order_stale` (broker-working too long) stops being a passive yellow —
  it feeds the reprice ladder (4.5).

### 4.5 Close reprice ladder (extend the existing entry-reprice engine)

Reuse the entry mechanism (cancel + replace with new order identity, fresh
preflight, nickel handling, `improvement_multipliers` schedule). What changes
is the parameterization and terminal state, per exit reason:

| Exit reason | Start price | Regress toward | Floor / terminal |
|---|---|---|---|
| `profit_target` | improved mid | partway to natural | stop at retained-profit floor: max(`exit_pricing.min_profit_to_trigger`, `target_profit * profit_floor_pct`) |
| `dte_target`, `half_time` | mid | natural by end of day | must be flat by EOD; alert if not |
| `max_loss`, `pre_event` | natural | cross the spread | alert operator if unfilled ~30 min; never end the day holding |

Why per-reason parameters are load-bearing (real numbers from the book,
2026-07-01 DELL put spread): entry credit $220, close at mid $112.50 → keep
~$107 profit; close at natural $215 → keep ~$5. One fixed "regress to natural"
ladder donates the entire win on profit-target exits; refusing to pay natural
on a max-loss exit risks far more than the spread. Same engine, different
endpoints.

- DAY close orders that die at the bell: mark `expired_eod`; management
  re-stages automatically next session (no operator wake-up needed).
- Sweeper cadence: the every-5-min `live-approved-orders` job becomes the
  retry / reprice / status-sweep loop. Management remains the decision maker.

---

## 5. Config surface

```yaml
live:
  exit_submit_source: sheet        # sheet | ledger  — rollout flag, see §7
  max_close_submits_per_run: 10
  exit_reprice:
    enabled: true
    after_minutes: 10              # per-step wait before cancel/replace
    max_reprices: 2
    step_multipliers: [0.5, 1.0]   # first move halfway, then to the reason-aware bound
    expire_after_minutes: 390
  exit_pricing:
    profit_target_trigger_pct: 95
    min_profit_to_trigger: 5
    profit_floor_pct: 50           # retain at least 50% of target profit on profit-target reprices
  health:
    exit_pipeline_stalled_minutes: 20
    urgent_close_order_stale_minutes: 30
```

Env overrides (same pattern as the risk manager; flags live in `.env` on
oldmac):

- `KAMANDAL_LIVE_EXIT_SUBMIT_SOURCE` (`sheet` | `ledger`) — master rollout flag
- `KAMANDAL_LIVE_MAX_CLOSE_SUBMITS_PER_RUN`
- `KAMANDAL_LIVE_EXIT_REPRICE_ENABLED`
- `KAMANDAL_LIVE_EXIT_REPRICE_AFTER_MINUTES`
- `KAMANDAL_LIVE_EXIT_REPRICE_MAX_REPRICES`
- `KAMANDAL_LIVE_EXIT_REPRICE_EXPIRE_AFTER_MINUTES`
- `KAMANDAL_EXIT_PROFIT_FLOOR_PCT`
- `KAMANDAL_LIVE_HEALTH_EXIT_PIPELINE_STALLED_MINUTES`
- `KAMANDAL_LIVE_HEALTH_URGENT_CLOSE_ORDER_STALE_MINUTES`

`sheet_approval` and `disabled` exit modes keep working unchanged.

---

## 6. Implementation status

### Phase 0 — Regression tests that reproduce the bug (implemented)

- Test: two groups hit target in one cycle → **both** must reach `submitted`
  (fails today at `rows[:1]`).
- Test: approved-unsubmitted intent survives a subsequent management cycle's
  sheet write (fails today via lane wipe).
- Test: local staged ticket in auto mode does not block re-approval for 120
  minutes; health reports `exit_pipeline_stalled` when the drain is disabled.
- Files: covered in `tests/test_live_lane.py` and `tests/test_live_health.py`
  so the assertions sit beside the existing live lifecycle fixtures.

### Phase 1 — Ledger-authoritative drain (implemented)

- `execution.py`: ledger drain behind `exit_submit_source=ledger`;
  `max_close_submits_per_run`; sheet_approval pre-step (sheet commands →
  ledger transition).
- `management.py`: stage-as-approved in auto mode; stale-approved re-ticket;
  dedup unchanged in shape but now self-healing.
- `management.py`: projected rows describe ledger-approved closes without
  `APPROVE_LIVE_CLOSE` in ledger mode. A full historical mirror of every close
  intent remains a follow-up; `live_book` is the complete per-position read
  model today.
- `health.py`: `working_close_order` narrowed to broker-acknowledged;
  `exit_pipeline_stalled` RED; REASON_ORDER updated.
- `config.py` + `control.yaml` + `.env.example`: flag + knobs.
- Exit criteria: Phase 0 tests green; deployed first with
  `exit_submit_source=sheet` (dormant), then cut over by env on oldmac.

### Phase 2 — Close reprice ladder (implemented, first pass)

- Close orders now reuse the cancel + replace pattern from entries, with close
  child tickets linked by `parent_ticket_hash`.
- Profit-target closes can improve toward natural but never violate the
  min-profit floor. DTE/half-time closes move toward natural. Max-loss and
  pre-event exits may cross by a nickel per reprice attempt.
- DAY close orders can be cancelled and marked `expired_eod`; management can
  re-stage next session because that status is terminal for dedup purposes.
- Follow-up: reason-specific Lathi escalation for `terminal: alert*` presets.

### Phase 3 — Operator polish (partially implemented)

- `REJECT_CLOSE` command honored from the sheet in ledger auto mode for local,
  not-yet-submitted close intents. It retires the ledger ticket as
  `rejected_by_operator`; it does not cancel broker-working orders.
- Live-book and health semantics now reserve `working_close_order` for
  broker-acknowledged statuses (`submitted`/`repriced`). Local approvals are
  `exit_pipeline_pending` / `exit_pipeline_stalled`.
- Runbook section in `docs/KAMANDAL_LAUNCHD_AND_ALERTS.md` for
  `exit_pipeline_stalled`.

### Rollout (§7) is its own step after Phase 1, not after Phase 3 — the ladder
improves fills, but the drain is what un-sticks the book.

---

## 7. Rollout & rollback

1. Merge Phase 1 with `exit_submit_source: sheet` (current behavior, bug
   included, unchanged by default).
2. On oldmac, during a market session with someone watching:
   `KAMANDAL_LIVE_EXIT_SUBMIT_SOURCE=ledger` in `.env`. No restart needed —
   launchd jobs read config fresh each run.
3. Watch one management cycle: every past-target/past-DTE group must produce a
   broker submission (events `live_order_execution_evaluated`, live_book
   showing order ids). The stuck cohort (DELL/AMD/IWM/QQQ as of 2026-07-02)
   is the acceptance test.
4. Rollback: flip the env var back to `sheet`. Ledger statuses degrade
   gracefully — approved intents simply stop being drained and the old
   expiry behavior resumes.

## 8. Acceptance criteria

- N simultaneous eligible closes → N broker submissions in one cycle.
- Zero occurrences of `expired_stale_close_approval` in auto mode over a
  normal week (backstop may fire only during broker outages).
- Health: no `exit_pipeline_stalled` under normal operation; RED within 2
  cycles when the drain is artificially disabled.
- Profit-target closes never submit below the min-profit floor.
- Sheet lane always reflects every close intent's current state for the day.
- `sheet_approval` mode behavior is byte-for-byte unchanged.

## 9. Out of scope / follow-ups

- Entries remain `rows[:1]` (deliberate for now; revisit with the ranking
  diversity work).
- Public cancel/replace uses cancel + new order id (as entry reprice does
  today); true atomic replace is not available on the API.
- Multi-day GTC close orders: not used; DAY + re-stage keeps state simple.

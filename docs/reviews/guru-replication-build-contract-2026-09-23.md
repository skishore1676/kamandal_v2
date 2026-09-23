# Guru replication: build and activation contract

September 23, 2026. This records the operator decisions and activation criteria. Implementation is in progress on `codex/guru-sleeves-live`; the Sheet controls are staged, but the oldmac live limit remains 55% until the session-boundary deployment passes readback.

## Intended behavior

- Copy only source-verified **opening contracts** from Greg Harmon and Mike Butler on the `exact_package` route. Kamandal owns subsequent management and exits. Templates, follow-ups, edits with unresolved lineage, and incomplete contracts cannot open a live position.
- Keep the current planner and adapted `idea` routes in `current_idea`. Charge exact guru openings to `guru_exact`. A guru idea and its exact package must not open the same source opportunity twice.
- The 40% `current_idea`, 40% `guru_exact`, and 80% `portfolio_total` figures are **maximum buying-power requirements as a percentage of account equity**, not spending targets. Ordinary broker buying power, concentration, eligibility, entry approval, and exit controls still apply.
- There is no grandfathering mode or forced rebalance. An existing `current_idea` book above 40% blocks new entries in that lane until it falls below the cap. With 54.8% already used and an 80% global cap, guru capacity is at most 25.2% of equity, subject to its own 40% cap, pending orders, broker buying power, and other gates.

## One Sheet policy owner

Create a small `portfolio_sleeves` tab with three required rows:

| lane | max_bpr_pct | meaning |
| --- | ---: | --- |
| `current_idea` | 40 | Current planner and adapted guru ideas |
| `guru_exact` | 40 | Confirmed guru opening packages |
| `portfolio_total` | 80 | Hard account-wide entry ceiling, including exposure outside either lane |

`max_bpr_pct` is operator-editable and validated as a number from 0 to 100. The compiler rejects missing or duplicate rows, invalid values, or lane caps above the total. The current 55% hard cap in `config/control.yaml` and `KAMANDAL_HARD_MAX_BPR_UTILIZATION_PCT` must cease to be alternate **live** policy owners when this tab is activated. Remove or relabel the old 55% target display so it does not imply an allocation target. A failed or stale Sheet read blocks new entries; it does not block management or exits. Capture the compiled policy and its source timestamp in the run receipt, but evaluate the latest policy again before each entry POST.

Keep `trade_sources` as the route switch. Give `mode` a validated, color-coded dropdown: **Live** (eligible for entry after every gate), **Shadow** (evaluate without entry), **Observe** (capture/interpret only), **Off** (no new candidate from that route). Retain `live_structures` as an explicit permission list, not a claim that adding a comma-separated name implements execution. Recheck the relevant source/output row and effective structure permission immediately before submission. Off/Shadow must stop already-staged opening tickets; an Off switch must never disable exits for positions already opened.

## Accounting and enforcement

Persist `sleeve_id`, source/output route, and stable source-opportunity identity on each candidate, ticket, order intent, and opened lifecycle/group. Classify existing Kamandal positions without exact provenance into `current_idea` for entry admission; no migration flag is needed. Charge broker/account BPR that cannot be matched to a group to `current_idea` as a conservative residual, as well as the global limit. Show the discrepancy in the brief. If a live account BPR reading or a pending-order BPR is unavailable, block new entries.

Before selecting a plan, calculate each lane's existing open BPR plus its pending entry BPR, then add the proposed plan. At the final broker boundary, re-read the Sheet policy, obtain fresh account/preflight BPR, and repeat lane and total checks. Include staged, submitted, partially filled, and uncertain entry intents; deduplicate replacement lineage and avoid silently dropping broker-reserved orders. A conservative double count may reduce entry capacity; an uncounted order must not increase it. The global check covers the broker-reported book and pending commitments as well as both lanes. A later cap reduction blocks new entries without closing existing positions.

The source-independent planner must enforce the same caps for mixed plans: `current_idea` additions can be zero while an otherwise eligible `guru_exact` addition is allowed by the remaining total headroom. Do not veto the whole planning cycle solely because one lane is already above cap.

## Exact-package capability and evidence

First build a four-to-five-week, source-linked corpus of distinct opening opportunities for both gurus. Reconcile acquisition gaps, images, and edited posts. Triage each source event as opening, template, follow-up/adjustment, exit, duplicate, or unresolved. Measure (1) captured opening opportunities and (2) openings whose leg side, option type, strike, expiration, ratio, and any displayed source price are source-verified. Report occurrence-weighted **understanding coverage**, aiming for 80–90%, separately from **live executable coverage**. Missing source/media and unresolved edits stay in the denominator and are not converted into entries by inference.

Prioritize new live structures by observed frequency and review value. For each structure, implement leg/ratio validation, unchanged contract hydration, Public payload and fresh multi-leg preflight/BPR behavior, position reconciliation, Kamandal management/exit handling, and focused tests before adding it to `live_structures`. The current live path is short-strangle-only; calendar or vertical permission by itself cannot make those structures executable. Broker preflight is an estimate and must be checked against current account state before placement; see [Public's multi-leg order guide](https://public.com/api/docs/templates/place-multi-leg-options-order).

## `trade_source_activity`: chief-of-staff view

Keep the raw episode, planner, order, and lifecycle events in the ledger. The Sheet should answer these operator questions in a brief at the top, followed by only actionable or recent distinct opening decisions:

| Question | Sheet answer |
| --- | --- |
| Are the routes on, and how much room is left? | As-of time; each guru idea/exact mode; current/guru/total used, cap, and available headroom. |
| What arrived and what happened? | Distinct openings captured, source-verified, entered, shadowed, blocked, and awaiting evidence over a stated window. |
| What needs attention? | Up to three material blockers or requested corrections, with source links and next action. |
| Why was this opportunity not entered? | One row per distinct opening: guru, source link, concise opening, decision, specific reason, and `Your correction`. |

Do not put normalized JSON, revision IDs, routine polling events, or every duplicate into visible cells. Preserve the existing operator correction through refresh by stable source-opportunity identity; the current post-level correction column must not be discarded or reassigned when a post contains multiple packages. Deterministic receipts own counts and decisions. Agent Broker may write a short narrative synthesis if deterministic facts are insufficient, but its prose cannot authorize an order or alter a gate.

## Required proof before activation

1. Confirm a natural scheduled source run under `source-evidence-v2`, then close or explicitly bound capture gaps. Do not treat a historical reinterpretation sweep as fresh entries.
2. Replay the four-to-five-week corpus and independently verify sampled images/contracts, edit lineage, the coverage denominators, and every promoted structure.
3. Test policy changes from Sheet read to compiled policy to planner and final submission: `live`, `shadow`, `observe`, `off`; staged ticket switched Off; cap lowered below existing use; mixed-lane plan; pending/uncertain order; restart; missing Sheet; broker/ledger mismatch; exits while route Off.
4. Shadow the full path with no broker submissions and inspect the concise `trade_source_activity` projection, including preserved corrections and accurate disposition joins.
5. At an operator-approved session boundary, deploy and read back the active Sheet rows, compiled policy, loaded oldmac code/jobs, broker account baseline, and final no-order dry run. Set `current_idea=40`, `guru_exact=40`, and `portfolio_total=80` together for activation; do not leave the old 55% YAML/environment value as an effective live override. Begin a bounded live canary only after those readbacks pass.

The September 23 readiness report remains the evidence snapshot. This document is the implementation and acceptance contract; it does not assert that any of these gates are already complete.

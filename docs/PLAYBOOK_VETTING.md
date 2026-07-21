# Kamandal V2 — Playbook Vetting

Date: 2026-06-19
Status: Working draft — Phase 1 (tags & taxonomy)
Owner decision authority: Suman breaks all ties.

## Why this exists

A playbook row is only as good as the rules encoded in it. If the *when* (entry
gates, tags) or the *how* (construction, management) are wrong, the deterministic
engine faithfully executes garbage. This doc is the durable ledger for vetting
those rules: every questioned cell gets a current value, evidence for/against, a
verdict, and an action — so every change is explainable later.

## Agreed approach

- **Tags & taxonomy first.** Cheapest, needs no external data, and it is the
  layer most likely to be garbage today. It also defines the schema every later
  research agent must fill.
- **Scope: all 18 playbooks**, including the off-by-default ones
  (`short_put_*`, `long_call_directional`, `long_put_directional`,
  `iron_condor_tight`).
- **Operator breaks ties.** The ledger surfaces conflicts and a recommendation;
  it never auto-resolves. Verdicts marked `DECIDE` are yours.
- **Promotion gate.** No change reaches a live playbook until it is either
  (a) confirmed by ≥2 independent evidence classes, or (b) validated in shadow
  for N cycles. Anecdotes (interviews) can only spawn shadow experiments, never
  flip a parameter directly.

## The three layers of a playbook (each vetted differently)

1. **Edge exists?** — is there a documented reason this structure earns in its
   regime. Source: backtested research + variance-risk-premium literature.
2. **Gates aimed right?** — the IV / delta / DTE / direction numbers point at
   where the edge lives. Source: research + own forward data.
3. **Routing taxonomy** — `applicable_direction` + `applicable_thesis_tags` +
   horizon correctly connect an *idea* to the *structure*. Source: first-
   principles design + consistency audit. **This doc is layer 3.**

## Evidence classes (ranked, for the later phases)

`backtested quantitative study` > `own shadow/forward data` >
`practitioner heuristic` > `interview anecdote`.

Interviews are hypothesis generators, not confirmers.

## Ledger schema

Each questioned item is one row:

| field | meaning |
|---|---|
| `item_id` | stable id (e.g., `TAG-001`) |
| `playbook_id` / `field` | the exact cell(s) in scope |
| `current_value` | what the sheet says today |
| `issue_type` | contradiction / redundant / over_broad / coverage_gap / orphan / axis_mismatch |
| `finding` | what's wrong and why it matters |
| `recommendation` | proposed fix |
| `evidence_class` | which class supports the recommendation (here: first-principles) |
| `verdict` | `DECIDE` (operator) / `auto` (mechanical, safe) / `shadow` (test first) |
| `operator_decision` | **blank — Suman fills** |

When we move past tags, the same ledger absorbs research/data claims by adding
`source`, `source_class`, `confidence`, and `regime` columns.

---

# Tag taxonomy

## The core problem: one column, three axes

`applicable_thesis_tags` currently mixes three different kinds of statement into
a single comma list, which is why routing is mushy and contradictions hide:

- **A. Price/setup state** — what the chart is doing: `support_bounce`,
  `oversold`, `capitulation`, `resistance_rejection`, `overextended`,
  `blow_off_top`, `distribution`, `breakout`, `breakdown`, `range_bound`,
  `mean_revert`, `post_event_consolidation`, `pre_event_anticipation`,
  `low_realized_vol`.
- **B. Volatility view** — what IV is expected to do: `vol_contraction`,
  `vol_expansion`, `low_iv`, `high_iv`.
- **C. Intent / modifier** — *why* the operator wants it, not a market state:
  `theta_harvest`, `catalyst`, `momentum`, `acquire_at_discount`,
  `dividend_play`.

**Recommendation (DECIDE):** split these into three columns —
`setup_tags` (A), `vol_view` (B, and reconcile with the numeric IV gates), and
`intent_tags` (C). Routing then matches on setup + direction + IV regime, and
intent becomes a scoring/explanation modifier rather than a routing key. This
single change removes most of the contradictions below at the source.

## Proposed canonical dictionary

Axis A — price/setup (mutually-informative, a structure may list several):

| tag | one-line definition |
|---|---|
| `support_bounce` | price reacting up off a defined support level |
| `oversold` | stretched-down momentum/RSI, no confirmed reversal yet |
| `capitulation` | climactic flush / panic low |
| `mean_revert` | expectation of reversion toward a mean (direction-neutral) |
| `range_bound` | contained between levels, no trend |
| `post_event_consolidation` | digesting after a known catalyst has passed |
| `low_realized_vol` | realized (not implied) vol compressed |
| `resistance_rejection` | price rejecting down off a defined resistance level |
| `overextended` | stretched-up, vulnerable to pullback |
| `blow_off_top` | climactic buying exhaustion high |
| `distribution` | topping/selling-into-strength character |
| `breakout` | confirmed move up out of a range/level |
| `breakdown` | confirmed move down out of a range/level |
| `pre_event_anticipation` | positioning *into* a scheduled catalyst |

Axis B — volatility view:

| tag | definition | note |
|---|---|---|
| `vol_contraction` | expect IV to fall | pairs with premium selling |
| `vol_expansion` | expect IV to rise | pairs with calendars/long vega |
| `low_iv` / `high_iv` | **redundant** with `iv_percentile_min/max` | should be *derived*, not hand-tagged |

Axis C — intent/modifier (do not route on these alone):

| tag | definition |
|---|---|
| `theta_harvest` | the position's job is to collect time decay (must be net-positive theta) |
| `catalyst` | a discrete event is the trigger |
| `momentum` | thesis relies on trend follow-through |
| `acquire_at_discount` | willing to be assigned the underlying |
| `dividend_play` | dividend capture is part of the thesis |

---

# Audit findings (Phase 1 ledger)

`verdict` legend: **DECIDE** = your call · **auto** = safe mechanical fix ·
**shadow** = needs a test before changing.

### Contradictions — a tag fights the structure or another tag on the same row

| id | playbook · field | current | finding | recommendation | verdict |
|---|---|---|---|---|---|
| TAG-001 | `long_put_directional` · thesis | `…, high_iv, theta_harvest` | A **long** (debit) put is net-**negative** theta and wants **low** IV — yet it's tagged `theta_harvest` and `high_iv`. Both directly contradict the structure, and `high_iv` also contradicts its own `iv_percentile 0–50` gate. Pure garbage-in. | Drop `theta_harvest` and `high_iv`. | DECIDE |
| TAG-002 | `put_spread_default`, `call_spread_default` · thesis | both list `mean_revert` **and** `momentum` | Mean-reversion (fade the move) and momentum (ride the move) are opposite regimes on the same row, so the row matches an idea and its opposite. | Keep `mean_revert` (fits a credit spread fading a stretch); move `momentum` to a directional/diagonal home. | DECIDE |
| TAG-003 | `put_diagonal_overextended`, `call_diagonal_oversold` · thesis | both list `mean_revert` + `momentum` | Same opposition as TAG-002 on the diagonals. Diagonals are directional-with-theta, so `momentum` is plausible but `mean_revert` muddies it. | Pick one stance per variant (overextended→`mean_revert`; an explicit momentum diagonal could be a *new* variant). | DECIDE |

### Non-discriminating / over-broad routing

| id | playbook · field | current | finding | recommendation | verdict |
|---|---|---|---|---|---|
| TAG-004 | global · `catalyst`, `momentum` | each appears on **8** playbooks, bullish and bearish | These two carry no regime information — they match almost everything, so they don't actually route. They're intent modifiers, not setups. | Reclassify both to Axis C (intent); exclude from the routing match key. | DECIDE |
| TAG-005 | `put_spread_default` · thesis | 7 tags (`support_bounce, mean_revert, theta_harvest, oversold, post_event_consolidation, catalyst, momentum`) | So permissive that nearly any bullish idea lands here — the "default" swallows everything, starving the more specific variants. | Trim to 2–3 core setups (e.g., `support_bounce, mean_revert, oversold`); push intent tags to Axis C. | DECIDE |

### Axis mismatch — volatility encoded inconsistently

| id | playbook · field | current | finding | recommendation | verdict |
|---|---|---|---|---|---|
| TAG-006 | global · `direction` vs thesis | `direction` includes `vol_up` (calendars) but there is **no** `vol_down`; contraction lives as a *thesis tag* (`vol_contraction`) instead | The volatility view is split across two columns inconsistently — vol-up is a "direction," vol-down is a "tag." Hard to reason about, easy to mis-route. | Make volatility one explicit axis (`vol_view`: up/down/neutral); remove `vol_up` from `direction`. | DECIDE |
| TAG-007 | calendars · `low_iv`/`high_iv` tags | `call_calendar_low_iv`, `put_calendar_low_iv` tag `low_iv` while also setting `iv_percentile 0–40` | The tag duplicates the numeric gate; two sources of truth can drift apart. | Derive low/high-IV labels from the numeric gate; drop the hand tags. | auto |

### Coverage gap — operator idea vocabulary doesn't map to playbook tags

| id | item · field | current | finding | recommendation | verdict |
|---|---|---|---|---|---|
| TAG-008 | `my_ideas.type_of_trade` ↔ `playbooks.thesis` | ideas use `Breakout, Contrarian, High Vol - Naked, Neutral High IV`; **none** are in the 23-tag playbook vocabulary | The operator/LLM idea namespace and the playbook namespace are disjoint. Whatever bridges them is undocumented — the single biggest garbage-in vector, because mis-mapping silently routes ideas to the wrong structure. | Define an explicit idea→tag mapping table (controlled vocabulary), and validate it. e.g., `Contrarian`→`mean_revert`/`oversold`; `Neutral High IV`→`range_bound`+`vol_contraction`. | DECIDE |
| TAG-009 | `my_ideas.direction` ↔ `playbooks.applicable_direction` | ideas say `Bull`/`Neutral`; playbooks say `bullish`/`neutral` | Even the direction strings don't match (case/format). A normalization layer must exist or ideas silently fail to match — needs verification in code. | Confirm/normalize in the matcher; add a test that every `my_ideas` direction maps to a valid playbook direction. | auto |

### Orphans / questionable singletons (low priority, list for completeness)

| id | playbook · field | current | finding | recommendation | verdict |
|---|---|---|---|---|---|
| TAG-010 | `short_put_acquisition` · `dividend_play` | only use of `dividend_play` | Dividend capture is an *intent*, and a thinly-justified entry trigger for a short put. | Move to Axis C; confirm it should drive entry at all. | DECIDE |
| TAG-011 | `iron_condor_tight` · `low_realized_vol` | only use of `low_realized_vol` | Singleton; fine conceptually but undefined relative to *implied* vol gates. | Define it (realized-vol percentile?) or fold into the range-bound setup. | DECIDE |

## What's actually fine (so we don't churn it)

- Direction↔structure mappings are sound: `put_spread`=bullish/neutral,
  `call_spread`=bearish/neutral, `iron_condor`/`strangle`=neutral,
  `jade_lizard`=bullish/neutral. No fixes needed.
- IV-regime gates are coherent per structure: premium sellers gate to high IV,
  calendars/diagonals to low IV. The numbers may still get tuned in the
  research phase, but the *direction* of the gating is right.
- `narrative_ignition_*` intentionally carry a single tag + a Mala
  `structural_break` annotation requirement; their narrowness is by design.

## Next actions

1. Operator passes through the `DECIDE` rows above and fills `operator_decision`.
2. Implement the `auto` fixes (TAG-007, TAG-009) once confirmed.
3. Build the idea→tag mapping table (TAG-008) — this likely becomes a small new
   sheet/config the matcher validates against.
4. Once the taxonomy is frozen, fan out the research/interview/own-data agents
   against this same ledger schema for layers 1–2 (gates & edge).

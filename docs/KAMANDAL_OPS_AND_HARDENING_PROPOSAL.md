# Kamandal Ops And Execution Hardening Proposal

Status: draft for operator review  
Date: 2026-05-29

## Why This Proposal Exists

Kamandal is now good enough to expose the next class of product questions:

- The LLM/intelligence layer should make ideas richer without bypassing deterministic risk rules.
- The live ledger must reconcile against broker truth, not merely remember what Kamandal thought happened.
- Candidate construction should prefer practical fills, not just mathematically valid legs.
- Public should remain the live broker lane for now, but Tastytrade market data should be wired as a comparable quote/OI lane.

The goal is not to make the agent submit trades. The goal is to let the agent annotate ambiguity and context inside a bounded schema, then let deterministic matching, validation, pricing, and reconciliation decide what is actionable.

## North Star

```text
Source content
  -> LLM extractor
  -> bounded idea object
  -> kamandal_ops annotation
  -> deterministic matcher
  -> deterministic candidate builder
  -> deterministic risk, liquidity, pricing, and reconciliation gates
  -> Sheet/Telegram/live automation based on configured approval mode
```

kamandal_ops should become a bounded trading-context annotator, not an execution agent.

It can say:

- "This sounds like a catalyst thesis, but trade horizon should likely be 30-45 DTE."
- "This is momentum continuation, not mean reversion."
- "Prefer monthly liquidity if individual-stock weeklies are thin."
- "This idea is directionally ambiguous; reduce conviction or route for review."

It must not:

- Construct broker orders.
- Override playbook constraints.
- Approve trades.
- Mutate strategy sheet rows silently.
- Mark a candidate as safe when deterministic checks fail.

## Part 1: Bounded kamandal_ops Idea Annotation

### Current Problem

The extractor produces structured ideas, but the horizon and profile fields can be too literal. A video may discuss a catalyst "this week," while the right option expression is still a 30-45 DTE spread. A fully deterministic fix helps, but it will always be a little blunt.

### Proposed Role

Add an optional annotation step after extraction and before matching:

```text
Idea -> IdeaAnnotation -> AnnotatedIdea
```

The annotation output must be schema-bound:

```yaml
idea_id: string
annotation_version: string
trade_horizon_suggestion:
  min_days: int | null
  max_days: int | null
  confidence: low | medium | high
  reason: string
profile_suggestions:
  thesis_profile:
    - catalyst
    - momentum
    - mean_reversion
    - volatility_contraction
    - volatility_expansion
    - range_bound
  confidence: low | medium | high
direction_sanity:
  direction: bullish | bearish | neutral | vol_up | vol_down | ambiguous
  confidence: low | medium | high
liquidity_preference:
  prefer_monthly: true | false | null
  avoid_weeklies: true | false | null
  reason: string
review_flags:
  - direction_ambiguous
  - catalyst_date_unclear
  - likely_educational
  - ticker_uncertain
  - off_universe
notes: string
```

Important boundary: `review_flags` are not thesis tags. They are metadata about extraction quality, chain quality, or operator review need. They should never go into `thesis_tags`.

### Deterministic Consumption

The matcher may use annotations only as soft inputs:

- If `trade_horizon_suggestion` is present, use it as the idea's suggested trade horizon.
- If absent, keep current catalyst-horizon fallback.
- If `direction_sanity.direction=ambiguous`, reduce idea conviction or require review depending on config.
- If `liquidity_preference.prefer_monthly=true`, add a monthly-expiry score bonus during candidate construction.

The matcher still owns:

- Universe eligibility.
- Enabled playbooks.
- IV/rank/absolute IV gates.
- Earnings/event gates.
- Shape validation.
- BPR and position limits.
- Live allowed structures.

### Acceptance Criteria

- Annotation schema validation rejects unknown fields.
- Annotation never creates candidates directly.
- The audit report shows both original extraction and annotation.
- A short catalyst idea can still match 30-45 DTE playbooks when annotation supports that.
- Ambiguous annotations degrade rank or route review, rather than inventing trades.

## Part 2: Broker Reconciliation And Manual Review Flags

### Current Finding

AMZN is currently marked as an open Kamandal live position in the local ledger, but the Public account parser reports `positions_count=0`. That means Kamandal has a ghost local position.

This is not a trade-decision bug; it is a reconciliation gap.

### Current Kamandal Behavior

Kamandal already has:

- `sync-live-orders`: polls submitted order statuses.
- `live_positions` and `live_position_groups` tables.
- Live management marks and close-ticket generation from local groups.
- `record-manual-live-fill` for emergency ledger alignment.

But it does not yet have a broker-position reconciliation pass that says:

- Broker has a position Kamandal does not know about.
- Kamandal thinks a position is open, but broker does not show it.
- Broker quantity/contract differs from local group.
- Open order status implies a close happened, but local group is still open.
- A group is unknown/orphaned and should halt automated management.

### Borrowed Pattern

`public_api_trading_v3` has the right conceptual split:

- broker snapshot first,
- compare broker positions/orders to app ledger,
- classify orphan/ghost/stuck states,
- auto-resolve only safe cases,
- otherwise create a manual intervention item.

Kamandal should borrow the concept, not the full async worker complexity.

### Proposed Kamandal Reconciliation Model

Add a deterministic command:

```bash
kamandal reconcile-live-positions
```

Pipeline:

1. Fetch Public account/positions/open orders.
2. Read Kamandal open live position groups.
3. Normalize broker positions into option-leg keys:
   - underlying
   - OCC symbol
   - expiration
   - option type
   - strike
   - side/quantity
4. Compare broker groups to local groups.
5. Emit reconciliation issues.
6. Apply safe auto-resolutions only when configured.
7. Write issues to SQLite and daily/readable status.

### Issue Types

```text
ghost_local_position
  Kamandal says open, broker has no matching position.

orphan_broker_position
  Broker has position, Kamandal has no matching open group.

quantity_mismatch
  Same contract exists, but quantity differs.

leg_mismatch
  Multileg group is incomplete or differs from broker state.

stale_close_intent
  Close ticket exists, but broker/local state no longer supports it.

unknown_broker_payload
  Broker payload cannot be normalized safely.
```

### Safe Auto-Resolution Defaults

For live mode, start conservative:

- `ghost_local_position`: mark group `needs_manual_review` first, not closed, unless broker snapshot is confirmed twice.
- `orphan_broker_position`: create issue and halt management for that underlying; do not adopt automatically.
- `quantity_mismatch`: manual review.
- `leg_mismatch`: manual review.
- `stale_close_intent`: retire stale close intent if broker has no position and local group is already closed; otherwise manual review.

After confidence:

- `ghost_local_position` can auto-close local group after two consecutive broker snapshots with no position.
- `orphan_broker_position` can be adoptable by explicit command:

```bash
kamandal adopt-live-position --issue-id ...
```

### Sheet / Operator Surface

Do not overload `daily_plan` with every detail. Add a compact line only when action is required:

```text
mode = live_reconciliation
plan_status = needs_manual_review
trade_bundle = Reconcile AMZN ghost_local_position
operator_action = blank
operator_notes = suggested action
plan_detail_json = full broker/local diff
```

Telegram/status heartbeat should summarize:

```text
RED: live reconciliation mismatch
- AMZN local open, broker flat
- action: review/close local group or confirm manual broker close
```

### Acceptance Criteria

- AMZN current case is detected as `ghost_local_position`.
- The command does not submit orders.
- Live management refuses to create close tickets for groups with open reconciliation issues.
- A repeated broker-flat result can retire a ghost local group only when the auto-resolve config is enabled.
- The audit trail records local payload, broker payload summary, issue type, and resolution.

## Part 3: Liquidity-Sensitive Candidate Construction And Pricing

### Current Finding

The current candidate builder chooses short legs mostly by delta/DTE closeness, then rejects later on OI, bid/ask, or credit/width. This can choose thin weekly options even when a nearby monthly expiry is far more liquid.

Example observed on 2026-05-29:

- NOW July 10 puts near candidate strikes had OI around `0-6`.
- NOW July 17 monthly puts near similar strikes had OI in the thousands.

### Operator Preference

Do not hard-ban weeklies.

Instead:

- Use expiry/leg liquidity as a score and nudge.
- Prefer monthlies for individual stocks when comparable.
- Keep weeklies available when they offer a materially better expression.
- If OI is low but still allowed, demand more price improvement.
- If OI is strong, accept less improvement.

### Proposed Candidate Selection Change

Add a `leg_quality_score`:

```text
leg_quality_score =
  oi_score
  + spread_score
  + expiry_quality_score
  + quote_completeness_score
```

Inputs:

- Open interest.
- Bid/ask width percentage.
- Bid/ask absolute width.
- Expiry type:
  - monthly bonus for single-name equities,
  - no blanket penalty for index/ETF weeklies,
  - weekly allowed but must earn its place.
- DTE closeness.
- Delta closeness.
- Existing playbook preferences.

Candidate generation should rank potential short legs by:

```text
delta_fit
+ dte_fit
+ leg_quality_score
+ optional annotation liquidity preference
```

Then pick long legs by:

```text
target_width_fit
+ long_leg_liquidity_score
+ resulting credit/width
+ total spread width sanity
```

### Proposed Pricing Change

Keep the current `improved_mid` concept, but make improvement dynamic:

```text
credit order:
  target limit = mid + improvement

debit order:
  target limit = mid - improvement
```

Improvement should increase when liquidity is worse:

```text
improvement =
  base_improvement
  + oi_penalty_improvement
  + wide_spread_improvement
```

Example default behavior:

```yaml
live:
  entry_pricing:
    mode: liquidity_adjusted_mid
    base_improvement_pct_of_spread: 0.10
    low_oi_improvement_pct_of_spread: 0.20
    very_low_oi_improvement_pct_of_spread: 0.35
    max_improvement_pct_of_spread: 0.45
    good_oi_threshold: 500
    low_oi_threshold: 100
```

Interpretation:

- OI >= 500: close to midpoint with modest improvement.
- OI 100-499: ask for more edge.
- OI < 100: either block in strict live mode or ask for much more edge in shadow/permissive mode.

### Filter Versus Price Behavior

Live strict mode:

- If OI is below absolute minimum, block.
- If OI is marginal but above minimum, demand more improvement.

Shadow/permissive mode:

- Allow low OI candidates through with warnings.
- Price them more aggressively.
- Log whether they would likely be fillable.

### Acceptance Criteria

- Candidate audit explains why an expiry was chosen.
- NOW-style case prefers July 17 monthly over July 10 weekly when liquidity dominates and DTE is still valid.
- Low-OI allowed candidates get more favorable entry prices.
- High-OI candidates keep smaller improvement so we do not leave too much fill probability on the table.
- The old credit/width calculation stays intact and auditable.

## Part 4: Tastytrade Market Data Wiring

### Current Finding

The Tastytrade adapter currently supports:

- OAuth/account state.
- Order/preflight payloads.
- Market metrics such as IV percentile/absolute IV.
- Static nested option-chain discovery.

It does not yet implement full option quote/OI/Greeks chain snapshots because DXLink market-data streaming is not wired.

This is a Kamandal implementation gap, not proof that Tastytrade cannot provide the data. Tastytrade publicly describes real-time quotes and option chains as part of its API, and its developer docs expose streaming market-data/DXLink flows.

### Proposed Implementation

Add a Tastytrade market-data adapter path:

```text
REST nested option-chain
  -> collect streamer symbols for target expirations/strikes
  -> DXLink quote token
  -> subscribe Quote / Greeks / Summary events
  -> assemble ChainSnapshot
```

Data targets:

- Bid.
- Ask.
- Mid/mark.
- Delta/gamma/theta/vega.
- Volume.
- Open interest via Summary event when available.
- Quote timestamp/freshness.

### Use In Kamandal

Phase 1: comparison lane only.

```bash
kamandal compare-market-data --symbols NVDA,DELL,NOW --provider-a public --provider-b tastytrade
```

Phase 2: fallback/enrichment.

- Public remains broker/preflight provider.
- Tastytrade can enrich OI/quote diagnostics.
- Candidate audit can show provider disagreement.

Phase 3: possible provider switch.

- Tastytrade becomes full market provider only after quote, Greeks, OI, and preflight parity tests pass.

### Acceptance Criteria

- Tastytrade can produce a `ChainSnapshot` for one symbol/expiry with bid/ask/Greeks/OI.
- Public and Tastytrade quote comparison report flags large discrepancies.
- Planner can optionally use Tastytrade OI as an enrichment signal without changing broker execution.
- No live order path switches broker without explicit config.

## Proposed Build Order

1. Live reconciliation command and issue table.
2. Block live management on unresolved reconciliation issues.
3. Liquidity-aware candidate ranking.
4. Liquidity-adjusted entry pricing.
5. kamandal_ops annotation schema and offline command.
6. Tastytrade DXLink market-data prototype.
7. Provider comparison report.
8. Optional controlled use of Tastytrade enrichment in planner audits.

## Open Questions For Review

1. For `ghost_local_position`, should we auto-retire after two broker-flat snapshots, or always require manual confirmation first?
2. Should monthly-expiry preference apply only to single-name equities, or also liquid ETFs like SPY/QQQ/IWM?
3. Should low OI in live strict mode be a hard blocker below 100, or should we allow it with very aggressive improvement?
4. Should kamandal_ops annotations be run for every idea, or only for ideas that fail deterministic matching?
5. Should Tastytrade become a comparison-only provider first, or should we immediately wire it as an optional OI enrichment fallback?

## References

- Current Kamandal live ledger: `live_positions`, `live_position_groups`, `sync-live-orders`, and live management commands.
- `public_api_trading_v3` reconciliation concept: broker snapshot, orphan/ghost/stuck classification, manual intervention path.
- Tastytrade API public material: real-time quotes, option chains, market metrics, and streaming market-data/DXLink documentation.

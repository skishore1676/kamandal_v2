# Kamandal V2 Vision

Date: 2026-04-25
Status: Initial product/architecture vision

## Thesis

Kamandal V2 should be a local-first multileg options decision and management
system for one human trader.

It is not a generic trading bot and not an autonomous hedge-fund-in-a-box. It is
a disciplined cockpit plus deterministic engine that turns Suman's existing
checklist into explicit data, rules, plans, and audit trails.

The center of gravity is simple:

1. Runtime control lives in environment/config: shadow vs live, global halt,
   trading enabled, credentials, and provider selection.
2. Google Sheets is the human configuration cockpit for universe, playbooks, and
   the daily plan.
3. A deterministic Python engine loads env/config, the sheet, local state, and
   market/broker data.
4. The engine proposes entries, sends approved or allowed orders, and builds
   exit plans.
5. LLM modules listen to content and produce structured ideas, playbook notes,
   and human newsletters, but they do not directly create executable decisions.
6. Human intervention shrinks over time only where rules have hardened through
   observation.

Kamandal is allowed to start in shadow mode, but it should be designed live-ready
from the first implementation: same order objects, same broker payload builders,
same preflight gates, same audit trail, with only the final submission switch
controlled by env/config.

## What To Borrow From Existing Projects

### From `bhiksha`

Borrow:

- Public broker auth/client/account patterns.
- Public preflight-first execution discipline.
- Schwab/Polygon separation for market data vs broker execution if needed.
- Typed config with Pydantic validators.
- Option selection by DTE/delta/liquidity constraints.
- Event log, runtime health, and session summary habits.
- Lifecycle thinking: avoid duplicate entries, reconcile broker state, make exits
  first-class.

Avoid for now:

- Intraday bar-daemon runtime complexity.
- Strategy plugin system for underlying-bar signals as the main abstraction.
- Full live loop orchestration until the options workflow proves itself in
  read-only/shadow mode.
- The single-leg, same-day execution assumption.

### From `mala_v2`

Borrow:

- Research artifact discipline: durable local evidence for what was tested,
  proposed, preflighted, filled, and managed.
- Bounded command surfaces rather than freeform agent behavior.
- Staged gates and promotion language.
- Google Sheet table client ideas.
- Research Ops mental model: reconstruct state from durable artifacts, then
  propose next actions.

Avoid for now:

- M1-M5 backtesting machinery in the first Kamandal V2 slice.
- Strategy discovery as a blocker for basic trade planning.
- Treating research outputs as execution truth without a separate approval lane.
- Any wording that demotes Google Sheets from configuration cockpit. In
  Kamandal, Sheets drive the operator-owned configuration; local artifacts store
  execution evidence and history.

### From old `kamandal`

Borrow:

- Strategy template plus underlying profile separation.
- Core option domain models: strategy type, filters, management rules, Greeks,
  market snapshot, trade idea, position, portfolio, pending order.
- Public multileg preflight and order payload formation.
- Position grouping from raw broker legs into strategy bundles.
- Shadow portfolio concept.
- SQLite store with JSON audit mirrors.
- Sheet parsing/cache patterns, while keeping the new sheet surface much
  smaller.
- YouTube/RSS/transcript intake and LLM summarization modules.

Avoid for now:

- One giant end-to-end daily pipeline.
- Backtester, optimizer, idea scraper, reflection loop, source intelligence, and
  live broker execution all landing at once.
- "Ticker mentioned in content" as the primary source of trades.
- Any design that requires LLM judgment in the hot path.

## Bhiksha vs Kamandal

Bhiksha and Kamandal should not be treated as two versions of the same runtime.

Bhiksha is mostly about fast single-leg option execution from intraday signals.
Timing, session state, and same-day entry/exit mechanics matter a lot.

Kamandal is about longer-duration options portfolio construction. Its edge is
not timestamp precision. If an idea is discussed at 9:00 AM and entered at noon,
that usually does not matter much for the kinds of trades Kamandal is built to
handle. What matters is whether the idea belongs in the portfolio at all.

Kamandal's core power is portfolio-constrained selection:

- scrape or collect many possible ideas
- normalize them into comparable candidates
- look at current positions, buying power, and portfolio Greeks
- decide which few candidates the account can responsibly absorb
- rank or propose the optimized plan with clear reasons

Example: the scraper may surface 20 trade ideas today, but buying power may
support only five, and the portfolio's delta/gamma/theta profile may only make
three of them desirable. Kamandal should be good at saying which three, why
those three, and what should be ignored for now.

The first implementation can be mostly deterministic. Over time, an LLM or agent
may help generate candidates, challenge rankings, or propose playbook changes.
That is an open design choice, but the execution-grade decision surface should
remain structured, auditable, and explainable.

Kamandal must be excellent at:

- constructing 2-leg, 3-leg, 4-leg, and later more complex strategies
- selecting expirations, deltas, widths, credits/debits, and quantities from
  rules of thumb
- estimating buying power and Greek impact before entry
- grouping broker legs back into strategy bundles
- managing complete multileg positions safely over days or weeks
- adding new variants over time without rewriting the whole engine

Example growth path:

- start with `call_calendar`
- later add `earnings_call_calendar`
- later add richer calendar/diagonal variants

The base structure and leg mechanics should be reusable; the playbook variant
should carry the context-specific filters, management rules, and event policy.

## Public.com And IV Percentile

Public should be treated as the broker and option-chain/Greeks surface, not as
the source of all derived analytics.

As of 2026-04-25, Public's official API docs show option Greeks including
`delta`, `gamma`, `theta`, `vega`, `rho`, and `impliedVolatility`. The docs and
changelog show Greeks on the option details endpoint and recently added to the
option chain endpoint, but they do not document IV rank or IV percentile as API
fields.

Therefore Kamandal V2 should maintain its own IV analytics store:

- Store daily IV, IV Rank, and IV percentile per enabled underlying.
- Prefer Tastytrade-native market metrics; use the 30-45 DTE near-ATM Public
  chain approximation only as a labeled local fallback.
- For fallback, compute IV rank and IV percentile over a configurable lookback,
  defaulting to 252 trading days, and retain the actual available observation
  count when history is incomplete.
- Record the source and formula version on every value so the number is
  auditable and can be recalculated later.

Practical defaults:

- `iv_rank = (current_iv - min_iv) / (max_iv - min_iv) * 100`
- `iv_percentile = count(history_iv < current_iv) / count(history_iv) * 100`

The UI and planner should show which definition is being used, because traders
and brokers often use "IV rank" and "IV percentile" inconsistently.

## Trader Checklist As Product Model

For each underlying, Kamandal answers:

- Is the ticker enabled in my universe?
- What is its current IV percentile and IV rank?
- Is its IV percentile inside the tradable range I configured for this
  ticker/profile?
- Is there earnings or another event in the avoid window?
- Is there big news or an operator-set avoid flag?
- Is the option chain liquid enough?
- Does the portfolio need more or less delta, theta, gamma, or vega exposure?
- How much buying power is available after current and planned positions?
- Which strategy structures are allowed for this ticker/profile today?

Then, for each strategy family, Kamandal answers:

- Is this a 1-leg, 2-leg, 3-leg, or 4-leg setup?
- What DTE range is allowed?
- What delta range is preferred?
- What width, credit-to-width, or debit/risk rule applies?
- What is the estimated buying power requirement?
- What is the expected portfolio Greek impact?
- What is the management plan before the order is shipped?

After an order is shipped, Kamandal manages from simple rules first:

- Close credit strategies at the configured profit target, commonly 25% or 50%
  of credit depending on strategy.
- Close or roll by the half-time rule: for a 45 DTE entry, be out around 22 DTE
  remaining unless the strategy says otherwise.
- Exit or block new exposure before events the operator does not want to manage.
- Never automate ambiguous partial closes on multileg positions.

## Target Architecture

```text
Env / Local Config
  -> shadow vs live, global halt, trading enabled, credentials, provider choices

Google Sheet Configuration Cockpit
  -> universe, playbooks, daily plan

Local SQLite Store
  -> IV history, market snapshots, ideas, candidates, orders, fills, positions,
     events, audit records

Deterministic Engine
  -> load config
  -> refresh universe snapshot
  -> compute IV rank/percentile
  -> evaluate event/news/risk gates
  -> build strategy candidates
  -> score portfolio fit
  -> preflight/order
  -> manage exits

LLM Intelligence Layer
  -> source summaries
  -> structured idea candidates
  -> playbook notes
  -> digest/newsletter files
  -> proposed rule changes for human approval
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for
the current engine design, including what we accepted and rejected from the
external PRD review.

Loaded configuration is defined by:

- environment/local config for runtime control and secrets
- `universe` sheet for tradable symbols and per-profile constraints
- `playbooks` sheet for strategy knowledge, enabled flags, and management rules

Execution artifacts are local: orders, fills, broker payloads, positions,
snapshots, and event logs should not require Google Sheets to remain auditable.

## First-Class Objects

Keep the object model small at first:

- `Underlying`: ticker, profile, enabled flag, caps, earnings/news policy.
- `IvObservation`: ticker, date, current IV, IV rank, IV percentile, source,
  formula version.
- `MarketSnapshot`: local record with ticker, price, IV metrics,
  earnings/event flags, liquidity, and source timestamps.
- `Playbook`: strategy structure, leg count, DTE/delta/width/credit rules,
  management rules, risk caps, enabled flag, and variant name.
- `Idea`: manual or LLM-generated source input; useful, but not executable by
  itself.
- `Candidate`: deterministic trade proposal with all gates and reasons.
- `OrderPlan`: exact legs, prices, preflight result, approval mode.
- `Position`: grouped strategy bundle, not raw legs only.
- `ExitPlan`: close/roll/hold recommendation with trigger reason.
- `Event`: append-only audit fact.

## Google Sheet Cockpit

Start with only these tabs:

- `universe`: symbol, enabled, profile, tradable IV percentile range, max BPR,
  max positions, earnings-sensitive flag, and notes.
- `playbooks`: enabled playbook rules: strategy family, structure, variant, leg
  count, IV/DTE/delta/risk/management rules, and notes.
- `daily_plan`: ranked candidates and reasons.

Do not start with these as sheet tabs:

- `control`: keep this in env/local config for now.
- `market_snapshot`: keep this local unless a read-only diagnostics tab becomes
  useful later.
- `orders`: keep this local; the operator should not need to act from it.
- `positions`: keep this local; management should be surfaced through plans and
  reports, not a heavy sheet tab.
- `digest`: write digest/newsletter Markdown files under a local folder first so
  the sheet does not become heavy.

Sheets drive configuration and review. SQLite and local files remain the source
of durable execution state.

## Engine Loops

### 1. Snapshot Loop

Refreshes the enabled universe:

- prices
- option chains and Greeks
- IV history-derived rank/percentile
- earnings/events/news flags
- account buying power and current positions

Output: SQLite snapshot records and optional local reports.

### 2. Planning Loop

Builds candidates and ranked portfolio plans:

- manual/LLM ideas can bias attention
- universe scanning can create candidates without a source mention
- every candidate carries pass/fail gates and reasons
- ranking is portfolio-aware: buying power, current exposure, delta/gamma/theta,
  event risk, IV percentile, and playbook fit all matter
- timing is secondary unless a playbook explicitly says otherwise
- the output is a set of plan bundles, not just isolated trades
- a plan can contain one trade, three trades, five trades, or any subset that
  fits the account constraints
- the operator chooses one whole plan; in auto mode the machine can choose the
  highest-ranked eligible plan
- `daily_plan` should stay plan-level: individual candidate/leg impacts are
  useful for audit, but the operator decision is based on plan-level BPR,
  portfolio Greeks, concentration, event risk, and reasons
- JSON cells in `daily_plan` are acceptable for drilldown, as long as the sheet
  remains one row per plan

Output: `daily_plan` rows and candidate records.

### 3. Execution Loop

Starts conservative:

- read-only first
- then Public preflight/shadow
- then live only after explicit approval

Output: order plans, broker payloads, preflight responses, pending/submitted
orders stored locally.

### 4. Management Loop

Runs before new entries:

- checks profit target
- checks half-time DTE rule
- checks event avoidance
- checks max-loss or manual review flags when configured
- emits full-position close plans for multileg positions

Output: `ExitPlan` records and local grouped-position updates.

### 5. Intelligence Loop

LLM/non-deterministic lane:

- summarize YouTube/podcast/newsletter inputs
- classify as trade idea, digest, playbook lesson, or noise
- create structured idea candidates
- write strategy newsletter for the human
- propose playbook changes, but never silently apply them

Output: local idea candidates, local digest/newsletter Markdown, and proposed
rule changes.

## Phased Build Boundary

The build should be phased, but the target is the full system. The first phases
should keep the system small without painting us into a shadow-only corner.

Phase 0 creates the foundation:

1. Project scaffold, typed models, config, SQLite schema, tests.
2. Env/local control for `shadow` vs `live`, global halt, and trading enabled.
3. Sheet bootstrap and local cache for `universe`, `playbooks`, and
   `daily_plan`.
4. Local digest folder and local execution/audit artifacts.

Phase 1 builds the planning brain:

1. Universe snapshot with placeholder/manual IV import first, then Public
   chain/Greeks-based IV computation.
2. Deterministic candidate builder for a small strategy set:
   - short put
   - put spread
   - call spread
   - iron condor
   - call calendar
3. Plan-bundle generator that ranks combinations of valid candidates against
   buying power and portfolio Greeks.
4. Position grouping and management recommendations.

Phase 2 makes multileg execution real:

1. Public preflight for single-leg and multileg plans.
2. Shadow fills and local grouped positions.
3. Live-ready broker payloads and idempotent order IDs.
4. Global env switch keeps the default in shadow until explicitly changed.

Phase 3 hardens management:

1. Profit-target exits.
2. Half-time/DTE exits.
3. Event-risk exits.
4. Full-group close safety for multileg structures.

Phase 4 enables live:

1. Same deterministic order path as shadow.
2. Explicit env/local config switch to live.
3. Conservative size and playbook gates.
4. Audit trail proving what was planned, preflighted, submitted, filled, and
   managed.

Phase 5 adds intelligence:

1. Local Codex CLI as the default LLM runner.
2. Optional provider switch for other LLMs.
3. Source summaries, digest/newsletter files, idea candidates, and playbook
   proposals.
4. No direct mutation of execution policy without human approval.

## Explicit Non-Goals For The First Slice

- No live autonomous trading.
- No multi-agent architecture.
- No full backtester.
- No broad strategy miner.
- No hidden policy mutation by LLM output.
- No Schwab or Polygon dependency unless Public data is insufficient for a
  specific field.
- No complicated optimizer before the simpler multileg rules produce trustworthy
  daily plans.

## Product North Star

Kamandal V2 should feel like this:

> "Show me the trades I would have considered anyway, with the IV/event/risk
> context already checked, the legs built from my rules of thumb, the portfolio
> impact made explicit, the best few choices ranked against my buying power and
> Greeks, and the exit plan attached before I enter."

When the system cannot explain why a trade is valid, it should not trade.

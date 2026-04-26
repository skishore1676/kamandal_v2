# Kamandal V2 Architecture

Date: 2026-04-25
Status: Working architecture after PRD review

## Purpose

This document reconciles:

- Suman's operating model
- lessons from `bhiksha`, `mala_v2`, and old `kamandal`
- the external Claude PRD supplied during planning

The goal is to preserve the good ideas without importing complexity that does
not match the current vision.

## Core Product Decision

Kamandal is a portfolio-plan builder.

The operator decision is not:

> Which single trade should I take?

It is:

> Given my current portfolio, buying power, approved universe, playbooks, and
> ideas on the table, which portfolio plan should I choose?

Therefore `daily_plan` is one row per ranked plan. Details about individual
candidates, legs, preflight responses, and Greek contributions stay local in
SQLite/audit artifacts.

The sheet may include JSON cells for visibility, especially `trade_bundle_json`,
`plan_metrics_json`, and `plan_detail_json`. This gives the operator full
drilldown without turning `daily_plan` into a multi-row-per-plan surface.

## What We Accept From The Claude PRD

### Idea Is A Thesis

Accept.

An idea is intent, not an executable trade. Examples:

- `TSLA`, bullish/vol-up, call calendar discussed
- `NVDA`, neutral/high-IV, strangle discussed
- `SPY`, bearish/hedge, call spread idea

The engine may use the idea as attention or constraint, but it still has to
construct concrete candidates from playbooks and option-chain data.

### Playbook Variants

Accept with a simpler sheet surface.

A playbook is trader knowledge. It can have variants:

- `call_calendar`
- `earnings_call_calendar`
- `iron_condor_high_iv`
- `put_spread_standard`

The PRD's concept of structure-specific legs, strike rules, DTE rules, entry
filters, sizing, and exit rules is right. The first sheet schema is flatter for
human editing, but the internal model should eventually compile each row into a
structured playbook object.

### Shape Validators

Accept strongly.

New multileg structures must have code-level invariants. This is one of the
most important lessons from both the PRD and old `kamandal`.

Examples:

- iron condor must have valid put wing and call wing ordering
- call calendar must have same call strike and short-near/long-far expiries
- strangle must have short put below spot and short call above spot
- full-group close must include every leg

Adding a new `structure_type` should require a validator. That friction is good.

### Chain Snapshots And Audit

Accept.

Every planning run should persist what the machine saw:

- option chain snapshot
- account state
- portfolio state
- IV metrics used
- candidate set
- preflight responses
- selected plans

This enables replay and prevents "why did it do that?" archaeology.

### Fail Closed

Accept.

When data is missing, stale, malformed, or broker state disagrees with local
state, Kamandal should skip, halt, or request review rather than invent
confidence.

### Replay

Accept as a later phase.

The PRD is right that deterministic replay matters. But it should come after
the first planner shape exists, not before we can produce a useful daily plan.

## What We Reject Or Defer

### More Sheet Tabs

Reject for now.

The PRD adds `idea`, `positions`, `tags`, `structure_types`, and read-only
mirrors. That is not aligned with the current cockpit design.

Keep the sheet lean:

- `universe`
- `playbooks`
- `daily_plan`

Keep everything else local until there is a repeated operator need.

### One-Idea Proposal Model

Reject.

The PRD proposes candidates per idea and then a greedy portfolio pack. That is
useful as an implementation primitive, but not as the product model. Kamandal
must rank plan bundles.

The engine may first create candidate proposals per idea, but the visible output
is a portfolio plan composed of multiple candidates.

### Single Highest Candidate Per Idea

Reject.

Sometimes the second-best candidate for one idea is better inside a portfolio
bundle because it uses less BPR, balances delta, or avoids gamma concentration.
Candidate quality is contextual. The plan scorer decides, not the per-idea score
alone.

### Greedy-Only Portfolio Construction

Reject as final design.

Greedy is acceptable for a smoke-test planner, but not enough. We need bounded
combinatorial search with pruning, likely beam search:

- generate top candidates per idea/playbook
- keep only feasible candidates
- expand plan bundles incrementally
- prune by BPR, concentration, max positions, and Greek risk
- keep top N partial plans at each depth

This avoids brute force while still evaluating combinations.

### Long-Running Tick Loop First

Defer.

Bhiksha needs a tight loop because timing matters. Kamandal can start with
explicit commands:

- load sheet/config
- refresh snapshots
- build daily plans
- optionally preflight approved plan
- evaluate management actions

A daemon can come later once the command surfaces are trustworthy.

### IV Provider Returning None

Reject as default.

The PRD suggests skipping all IV-gated playbooks until a real provider exists.
That would block core behavior. Instead:

- define `iv_percentile_source`
- allow manual/imported IV percentile at first
- mark derived values clearly
- later compute from Public chain history

No silent fake precision, but also no dead system.

## Machine Plan-Building Model

The machine builds plans in six stages.

### 1. Load Inputs

Inputs:

- `control.yaml` and env control
- `universe` sheet
- `playbooks` sheet
- local idea store or imported idea file
- current broker/account state
- current local positions
- IV history/percentile store
- event/earnings/news flags where available

The sheet defines tradable configuration. Local SQLite stores machine state and
audit artifacts.

### 2. Normalize Ideas

Ideas can come from:

- scraper/LLM
- manual file
- future sheet import
- universe scan without a source idea

Normalized idea fields:

- `idea_id`
- `source`
- `underlying`
- `direction`: `bullish`, `bearish`, `neutral`, `vol_up`, `vol_down`
- `strategy_hint`: optional, such as `call_calendar`, `strangle`, `put_spread`
- `thesis_tags`
- `horizon_days`
- `confidence`: source confidence, not a risk input
- `operator_status`: `pending`, `approved`, `rejected`, `expired`
- `notes`

For the first build, ideas can be local JSON/YAML fixtures. We do not need an
`idea` sheet tab yet.

### 3. Build Concrete Candidates

For each idea, find compatible playbooks:

- ticker/profile enabled in universe
- playbook enabled
- strategy hint matches or is compatible
- direction/thesis/horizon compatible when those fields exist
- IV percentile inside universe and playbook range
- event/earnings rules pass

Then fetch chain/quotes and build a bounded candidate set.

Important: do not enumerate everything. Each playbook needs a selector that
produces a small, meaningful set:

- target DTE plus near alternatives
- target delta plus near alternatives
- liquidity-filtered legs only
- strike/expiry combinations that satisfy the shape validator
- cap candidates per idea/playbook, for example top 3-5

Candidate examples:

- TSLA call calendar at 45/60 DTE near 0.45 delta
- TSLA call calendar at 30/45 DTE near 0.40 delta
- NVDA short strangle at 45 DTE near 0.16 delta
- NVDA iron condor alternative if undefined risk is blocked

### 4. Preflight And Enrich Candidates

For each surviving candidate:

- compute estimated credit/debit
- compute mid/natural/spread quality
- compute or fetch Greeks
- estimate BPR
- preflight with Public when needed
- attach event/earnings status
- attach rejection reasons when invalid

Public is useful here because it can provide:

- option chain and quotes
- Greeks and implied volatility
- account/portfolio state
- buying power
- preflight results for single-leg and multileg orders

Public should not be assumed to provide:

- IV rank
- IV percentile
- strategy judgment

### 5. Generate Plan Bundles

Now build plans from candidates.

Constraints:

- max positions
- max portfolio BPR utilization
- max BPR per underlying
- no duplicate underlying unless allowed
- no duplicate idea unless allowed
- event-risk exclusion
- portfolio Greek guardrails
- liquidity minimums

Use bounded search:

```text
start with empty plan
for each depth 1..max_positions:
  expand each partial plan by adding one compatible candidate
  reject plans that violate hard constraints
  score surviving partial plans at the plan level
  keep top K partial plans
return top N complete/partial plans
```

This is deterministic and explainable. It avoids brute force but still considers
combinations.

### 6. Score Plans

Plan scoring is portfolio-level.

A plan score can include:

- buying power fit
- theta improvement
- target slight negative delta
- gamma risk reduction or containment
- diversification
- concentration penalty
- IV/playbook fit
- liquidity quality
- event-risk penalty
- number of positions vs account size

The sheet only needs plan-level fields:

- plan rank
- trade bundle summary
- BPR used
- buying power after
- portfolio Greeks before/after/change
- plan reasons
- blocked_by if not eligible
- JSON detail cells for the trade bundle, metrics, and full plan object

Candidate-level details remain local.

## Role Of LLM / Agent

Do not put an agent in the execution-grade planning path at first.

Use LLMs for:

- summarizing sources
- extracting rough idea intent
- deduplicating and classifying content
- writing human digest/newsletter files
- proposing playbook changes
- explaining top plans in human language

Do not use LLMs for:

- broker order construction
- risk enforcement
- shape validation
- choosing exact strikes without deterministic validation
- silently changing playbooks

Future agent role:

- challenge plan rankings
- identify missing playbook variants
- propose "why not this?" alternatives
- recommend research tasks

But the deterministic engine remains the source of executable truth.

## Revised Internal Package Structure

The code should grow toward this layout:

```text
src/kamandal_v2/
  cli.py
  config.py
  paths.py
  schemas.py
  sheets.py
  seed.py

  domain/
    models.py              # Idea, Playbook, Candidate, Plan, Position, Greeks
    enums.py
    serialization.py

  stores/
    sqlite.py              # local durable state
    audit.py               # JSONL and JSON mirrors

  market/
    public_chain.py        # Public option chain/quotes/greeks adapter
    iv_history.py          # local IV percentile/rank store
    events.py              # earnings/news/event flags

  broker/
    public_client.py
    public_orders.py
    preflight.py

  planner/
    idea_loader.py
    playbook_matcher.py
    leg_selectors.py
    shape_validators.py
    candidate_builder.py
    candidate_filters.py
    plan_generator.py
    plan_scorer.py
    daily_plan_writer.py

  management/
    position_grouping.py
    exit_rules.py
    exit_planner.py

  intelligence/
    codex_cli.py
    source_digest.py
    idea_extraction.py
    playbook_proposals.py
```

We do not need to create every folder immediately, but this is the intended
shape. It separates the core risks:

- market/broker data
- deterministic planning
- management
- LLM intelligence
- local persistence

## First Implementation After Scaffold

The next useful build is not an agent. It is a deterministic planner smoke test.

Build:

1. Domain models for `Idea`, `Candidate`, and `Plan`.
2. A local fixture idea file with examples like TSLA calendar and NVDA strangle.
3. A fixture market/preflight adapter so the planner works without live API
   calls.
4. Candidate builder for:
   - `call_calendar`
   - `short_put`
   - `put_spread`
   - `call_spread`
   - `iron_condor`
5. Shape validators for the same structures.
6. Beam-search plan generator.
7. Plan-level daily plan writer.

Only after this works should we wire Public chain/preflight live.

## Open Questions

Resolved product choices:

1. Where should human-approved ideas live first?
   - Recommendation: local `data/ideas/*.yaml` for now, not a sheet tab.
2. Should approval happen at idea level, plan level, or both?
   - Decision: plan-level approval first. Idea-level approval can come later if
     scraper noise becomes painful.

No blocking open questions remain for the deterministic planner smoke test.

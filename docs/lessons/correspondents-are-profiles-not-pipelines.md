---
title: Correspondents are profiles, not pipelines
type: decision
area: source-intelligence
date: 2026-09-04
tags: [correspondents, birdclaw, source-episodes, agent-broker, planner, provenance]
refs: [docs/SOURCE_EPISODE_COMPILER.md, docs/CORRESPONDENT_SIGNAL_PIPELINE.md, tests/fixtures/trade_source_interpretation/gold-v0.jsonl, src/kamandal_v2/intelligence/correspondent_signals.py]
---

# Correspondents are profiles, not pipelines

## What We Learned

A trusted individual's recurring publishing grammar should be represented by paired
Birdclaw and Kamandal profiles. Do not clone capture, translation, or planner code for
each person. Capture every sanitized post first; make missing data, unknown meaning,
unsupported structure, lifecycle action, recency, and universe membership explicit
admission blockers.

The profiles may have genuinely different deterministic grammar, prompts, examples,
and history recipes. Reuse the bounded orchestration, not one person's semantics. A
post is an evidence envelope that may contain several atomic events; it is not one
exclusive action label.

## Context and Evidence

The first Greg-only chart seam solved weekly enrichment but not the three-family product
flow. The reusable implementation separates:

- Birdclaw author/classification/literal-extraction rules.
- Market Cartographer point-in-time chart evidence.
- Kamandal family semantics, lifecycle, and planner admission.
- Existing planner construction and portfolio gates.

The cross-application seam must be a source-neutral question and answer, not a
Greg-shaped Cartographer API. Kamandal's profile decides whether chart evidence is
needed and how to map bullish/bearish answers to allowed structures. Cartographer owns
only point-in-time direction, trigger, invalidation, fingerprints, and evidence, and
always returns `planner_eligible=false`.

Sequence matters: export the current Birdclaw packet, build questions from that exact
packet, answer them, then translate once. Building a request from `latest_translation`
asked yesterday's question and also allowed a stale response to masquerade as current.
The production exchange now binds response question IDs, symbols, source IDs, and
observation time back to the exact request.

`scripts/replay_correspondent_signal_fixture.py` proves that Greg's three families and a
second `sample_person` family reach the same code path. The planner receives only
eligible records and enforces `Idea.allowed_structures`; it never parses source prose.

The 2026-09-03 operator review exposed the limit of the original one-result prompt:
Mike's mixed SNOW/NDX/MSFT post contains a scale-out, a hold, and a roll, while the
current interpreter copies one action over symbols. The same corpus also showed that
close and expiry posts must be ignored for new entry but retained as source-lifecycle
evidence. `docs/SOURCE_EPISODE_COMPILER.md` defines the bounded correction.

## When It Applies

Use a new profile for every new source. Add shared code only when the source needs a
genuinely reusable compiler capability, such as multimodal event decomposition or
history linkage. Do not force a new source into another person's grammar merely
because both eventually emit `idea` or `exact_package`.

## Apply It Next Time

Add two profiles and fixtures, then replay classification, at least one parked state,
the current-packet question exchange when required, planner loading, structure
matching, and all-false protected effects. Preserve existing Cartographer contracts by
adding a versioned question type rather than changing another consumer's signal. If an
author-specific branch appears in `live`, `planner`, or broker code, stop and move the
meaning back into the profile/translation boundary.

When one atomic opportunity yields both an idea and an exact package, give both the
same opportunity-group identity and allow the portfolio planner to select at most one.
Otherwise better extraction can silently double exposure. Missing required media or
history must park regardless of model confidence.

## Dead Ends

### Model comparisons must separate interpretation from adapters

The September 5 comparison retained raw answers before compiler normalization.
It exposed three non-model problems: `call_vertical` was graded as a false new
entry instead of a call-spread synonym; the first matching calendar regex
overwrote a literal, correctly identified resulting diagonal; and the episode
prompt's `Aug 28 2026` date format was rejected by the exact-leg adapter, which
accepted only ISO dates or yearless `Aug 28`. Explicit years must be preserved,
including on historical evidence, rather than rolled into a later year.

Use `scripts/evaluate_source_episode_models.py` for raw answers, reported usage
and separate non-template idea recovery. Use
`scripts/evaluate_source_episode_vision.py` for the actual seven-image corpus.
Keep legacy scores alongside semantic scores; do not inflate accuracy with the
twenty deterministic earnings-template ideas or claim missing-image tests prove
vision quality. Rescore saved answers after adapter fixes without another model
call. A stronger model can use fewer tokens if it avoids repairs; reported
Codex token totals do not establish subscription-credit usage or API cost.

- A bullish-only seeded evaluator could not express bearish put-diagonal watches.
- A Kamandal request shaped as `source: string` plus `symbols: []` did not satisfy
  Cartographer's provenance-rich seed schema (`seed request source must be an object`).
- Running enrichment before current activation necessarily read the previous
  translation; moving the same shell block earlier or later could not repair that
  ownership error.

## September 5 historical validation

Interpretation failures can come from acquisition or model routing even when the prompt is sound. A public photo URL in post text does not prove an actual cached image, and an actor requesting Astra does not prove Astra executed: verify the Birdclaw public media hash/path and Agent Broker winner receipt. Keep provider identifier corruption visible and require independent source evidence before promoting a symbol.

Retire a tuned holdout into regression material. Replaying saved model answers is useful for validator/projection fixes and costs no model tokens, but it is not a new accuracy measurement. Preserve a guru's original structure when explicitly reexpressing its thesis through an existing Kamandal playbook. Read the [bounded result](../reviews/guru-history-results-2026-09-05.md) for the sample, missing coverage, and exact scope.

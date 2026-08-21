---
title: Correspondents are profiles, not pipelines
type: decision
area: source-intelligence
date: 2026-08-01
tags: [correspondents, birdclaw, cartographer, planner, provenance]
refs: [docs/CORRESPONDENT_SIGNAL_PIPELINE.md, src/kamandal_v2/intelligence/market_questions.py, src/kamandal_v2/intelligence/correspondent_signals.py, src/kamandal_v2/planner/candidate_builder.py]
---

# Correspondents are profiles, not pipelines

## What We Learned

A trusted individual's recurring publishing grammar should be represented by paired
Birdclaw and Kamandal profiles. Do not clone capture, translation, or planner code for
each person. Capture every sanitized post first; make missing data, unknown meaning,
unsupported structure, lifecycle action, recency, and universe membership explicit
admission blockers.

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

## When It Applies

Use a new profile when another source fits `chart_watch`, `numbered_template`,
`trade_journal`, or `ignore`. Add code only when the new source requires a genuinely
new reusable semantic mode or literal extractor.

## Apply It Next Time

Add two profiles and fixtures, then replay classification, at least one parked state,
the current-packet question exchange when required, planner loading, structure
matching, and all-false protected effects. Preserve existing Cartographer contracts by
adding a versioned question type rather than changing another consumer's signal. If an
author-specific branch appears in `live`, `planner`, or broker code, stop and move the
meaning back into the profile/translation boundary.

## Dead Ends

- A bullish-only seeded evaluator could not express bearish put-diagonal watches.
- A Kamandal request shaped as `source: string` plus `symbols: []` did not satisfy
  Cartographer's provenance-rich seed schema (`seed request source must be an object`).
- Running enrichment before current activation necessarily read the previous
  translation; moving the same shell block earlier or later could not repair that
  ownership error.

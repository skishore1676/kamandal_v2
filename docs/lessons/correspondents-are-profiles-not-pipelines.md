---
title: Correspondents are profiles, not pipelines
type: decision
area: source-intelligence
date: 2026-08-01
tags: [correspondents, birdclaw, planner, provenance]
refs: [docs/CORRESPONDENT_SIGNAL_PIPELINE.md, src/kamandal_v2/intelligence/correspondent_signals.py, src/kamandal_v2/planner/candidate_builder.py]
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

`scripts/replay_correspondent_signal_fixture.py` proves that Greg's three families and a
second `sample_person` family reach the same code path. The planner receives only
eligible records and enforces `Idea.allowed_structures`; it never parses source prose.

## When It Applies

Use a new profile when another source fits `chart_watch`, `numbered_template`,
`trade_journal`, or `ignore`. Add code only when the new source requires a genuinely
new reusable semantic mode or literal extractor.

## Apply It Next Time

Add two profiles and fixtures, then replay classification, at least one parked state,
planner loading, structure matching, and all-false protected effects. If an
author-specific branch appears in `live`, `planner`, or broker code, stop and move the
meaning back into the profile/translation boundary.

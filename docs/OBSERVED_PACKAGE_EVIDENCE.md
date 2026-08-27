# Observed Package Evidence

Date: 2026-08-27
Status: source-side contract implemented locally; planner admission and runtime activation intentionally not implemented

## Purpose

Some correspondents publish an option package as an image rather than as a
plain-language idea. Kamandal must preserve what the source actually showed
before deciding whether the package belongs in any experiment.

The boundary is:

```text
Birdclaw public post + sanitized image
    -> Agent Broker transcription
    -> Kamandal deterministic validation
    -> ObservedPackageEvidence
    -> [future explicit selection decision]
    -> ordinary Kamandal candidate / plan / shadow lifecycle
```

`ObservedPackageEvidence` is deliberately upstream of `Idea`, `SourceMode`, and
the planner. A verified source package is a fact about the source; it is not yet
a portfolio recommendation, an approved plan, a simulated fill, or a trade.

## Ownership

- Birdclaw owns read-only public-X acquisition, author identity, sanitization,
  bounded media caching, and evidence provenance.
- Agent Broker owns provider routing and image-capable model execution. It does
  not own the trading interpretation.
- Kamandal owns the extraction schema, deterministic package validation,
  canonical identity, and any later planner/lifecycle behavior.
- The Google Sheet remains the operator policy surface. No source profile is
  allowed to activate a Kamandal lane merely by existing.
- TradeLab may eventually compare source-replication and Kamandal-selected
  outcomes, but it does not select or manage trades.

## Evidence contract

An accepted package records only observable facts: source profile and post,
stable media/package locator, action, displayed timestamp and price, product,
and exact legs. The model is not asked for confidence, portfolio fit, risk,
expected return, or a recommendation.

Three identities stay separate:

- `source_event_id`: the stable profile/post/media/package location;
- `package_signature`: the canonical ordered legs; and
- `evidence_revision_id`: the source event plus image, prompt, schema, and
  normalized-output hashes.

A corrected transcription supersedes an evidence revision without pretending
that the source published a second trade. Multiple packages in one image keep
distinct source-event identities.

Incomplete or ambiguous evidence is parked. It never falls back to an
arbitrary multileg structure. Follow-up posts are recorded as exact, ambiguous,
or unlinked benchmarks; they cannot create a new opening merely because they
mention a close or roll.

## Supported source structures

Only structures demonstrated in the reviewed Mike fixture corpus are currently
recognized: call/put calendars and diagonals, double calendars, butterflies,
super bull/bear packages, and long/short straddles. A roll or adjustment is a
follow-up action, not an opening structure.

This is a transcription capability, not a promise that every product can be
quoted, selected, filled, or managed by Kamandal.

## Current proof and stop line

The bounded calibration corpus contains six public posts and seven original
source images. Two repeated extraction passes produced 12 of 12 exact results,
with no provider failures and no falsely complete packages. That proves the
current examples can be interpreted; it does not yet prove production capture
completeness or future-image reliability.

Development stops before planner admission. The next architecture decision
must explicitly separate:

1. a source-replication cohort that accounts for every validated package but
   creates no Kamandal fill; and
2. a Kamandal-selected counterfactual that uses the existing optimizer and
   conservative shadow fill/lifecycle path.

The source image's displayed price and the first valid market midpoint are
observations, not fills. Any later shadow fill must retain Kamandal's existing
quote-quality, limit, and fill-friction rules. No live broker, live buying
power, account permission, Sheet write, schedule, or runtime activation belongs
to this source-side contract.

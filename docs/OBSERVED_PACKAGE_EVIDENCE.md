# Observed Package Evidence

Date: 2026-09-03
Status: evidence path deployed; source-neutral Sheet routing approved and pending migration

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
    -> passive evidence ledger
    -> Sheet-authorized exact-package candidate
    -> ordinary Kamandal candidate / plan / shadow lifecycle
```

`ObservedPackageEvidence` is deliberately not converted into a thesis `Idea`.
A verified source package is first a fact about the source. The approved public
name for this output is `exact_package`; the current implementation name
`observed_package` remains until migration. A complete opening may become an
exact-leg candidate only when the `trade_sources` row permits it and exactly
one existing source-independent playbook accepts exact packages for its
structure. No generic exact-only replacement row is added. During the first
migration, that admission remains shadow-only.

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

## Planner admission contract

Every evidence revision is appended to `observed_package_evidence` before any
selection. Close, roll, adjust, incomplete, unsupported-product, unquoted, and
unauthorized packages stay in that passive ledger and emit a precise receipt;
they cannot create a candidate or lifecycle action.

For an authorized opening, Kamandal fetches the current chain and matches every
source expiration, strike, option type, side, and ratio exactly. Missing or
duplicate contract matches park the package. Deterministic shape and liquidity
guards then run without broker/account preflight. The first actionable complete-
package midpoint is frozen in passive source accounting even if a separate BPR,
debit/credit, portfolio, or optimizer rule rejects the candidate. Stale, crossed,
one-sided, incomplete, or over-width markets cannot establish that first mark.
The existing planner may reject or not select the candidate; a selected
candidate alone proceeds through the existing conservative shadow adapter and
unified lifecycle manager.

This adds no person-specific selector, score boost, fill engine, manager, or
service. The exact package uses one compatible existing playbook's ordinary
management fields; the separate `trade_sources` tab controls source
participation. Zero compatible playbooks park as `unsupported`, and multiple
compatible playbooks park as `ambiguous_playbook_match`.

For calendars and diagonals, Kamandal deterministically maps the observed legs
to `short_near` and `long_far` before the existing lifecycle manager sees them.
This normalization may not change an expiration, strike, option type, side,
quantity, or ratio.

## Natural runtime seam

The existing shared X job remains the only scheduler. Birdclaw refreshes every
acquisition-enabled correspondent, and Kamandal's existing correspondent
activation command exports each sanitized packet. In the current implementation,
an `observed_package` profile transcribes only classified package records with
public cached media, caches the normalized evidence revision, and atomically
publishes one checksummed `observed_package_feed`. The approved migration
replaces this profile-wide branch with per-output routing so one post and
profile may produce ideas, exact packages, and residuals together. The later
`unified-plan` invocation reads the feed and supplies it to the same planner.

There is no Mike-only job. A corrupt source feed records a rejected-feed receipt
and, after migration, clears or parks only that source; it cannot erase another
source's ideas or stop the live planning book. A valid unchanged post reuses
its evidence cache rather than spending another model call. The Sheet rows
state `source_exact_legs` in their operator notes: DTE/delta fields required by
the registered capability remain validation metadata and never reconstruct an
exact package.

The shared X job requires the current canonical Birdclaw digest and never falls
back to retired bookmark snapshots. If Birdclaw is unavailable, Kamandal marks
the X input lane degraded for that cycle; it does not claim that live trading is
blocked, and the other idea lanes, planner, and portfolio manager continue.

## Current proof and stop line

The bounded calibration corpus contains six public posts and seven original
source images. Two repeated extraction passes produced 12 of 12 exact results,
with no provider failures and no falsely complete packages. That proves the
current examples can be interpreted; it does not yet prove production capture
completeness or future-image reliability.

The accepted architecture separates:

1. a source-replication cohort that accounts for every validated package but
   creates no Kamandal fill; and
2. a Kamandal-selected counterfactual that uses the existing optimizer and
   conservative shadow fill/lifecycle path.

Focused local and deployed proof now shows one exact call calendar flowing from immutable
evidence to candidate, optimizer selection, retry, ticket, open lifecycle,
unified management, and profit-target close with source identity unchanged and
broker effects false. Adversarial fixtures separately prove stale/wide parking,
no-policy receipts, replay-stable identity, and first-mark independence from an
economic rejection. Birdclaw commit `6c92b60` and Kamandal commit `15f71e6`
are deployed on oldmac. The current Google Sheet contains four enabled,
shadow-only Mike rows. The approved migration removes those person-specific rows
without replacing them. Existing call/put calendar and diagonal playbooks gain
an `accepted_inputs=idea,exact_package` marker and supply Kamandal's normal
eligibility and management policy. Source permission moves to exactly two
`trade_sources` rows per person. See
[Trade Source Routing](TRADE_SOURCE_ROUTING.md).

The X API spend cap was refreshed on 2026-08-28 and direct read probes for both
Greg and Mike succeeded. The remaining proof is natural: the next acquisition
and unified-planning cycles must prove media capture, transcription, feed
publication, optimizer admission, and the shadow lifecycle. No credential,
billing, live broker, live buying-power, account-permission, or live-promotion
change is granted by this contract.

## Migration rule

The four current Mike rows may be removed only as part of the atomic
source-routing migration. Their source-specific management settings are
intentionally retired rather than copied: an exact package preserves the
source's legs but is managed by one existing compatible Kamandal playbook.
Existing open lifecycles retain the policy frozen at entry; working Mike entries
must be absent or explicitly retired before row removal. The current runtime has
no recorded Mike exact-package lifecycle, but that fact must be rechecked
immediately before the protected Sheet write.

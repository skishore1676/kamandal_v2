---
title: Source activity revisions are not translation work
type: bug
area: source intelligence
date: 2026-09-10
tags: [source-episodes, caching, sheets, provenance]
refs: [src/kamandal_v2/intelligence/source_episode_projection.py, src/kamandal_v2/intelligence/trade_source_activity.py, src/kamandal_v2/intelligence/llm_extractor.py]
---

# Source activity revisions are not translation work

The activity tab previously displayed an interpreted event alongside every exact
package revision. Batch prompt hashes changed revision IDs even when the source
trade stayed unchanged. A row count therefore measured neither model calls nor
unique trades.

Exact episode-package revisions now depend on source event, leg signature, image,
action, symbol, structure, and displayed source price. Batch prompt provenance is
still retained but does not define trade identity. Candidate identity continues to
use the existing opportunity, playbook, and package signature; no historical IDs,
order lineage, or lifecycle state are rewritten.

The observation projector groups by source, post, source event, and package
signature. It attaches the related idea and exact-failure receipts, retains separate
idea/exact decisions and execution modes, and carries revision references in
`normalized_output.activity_history`. Different packages in a post remain separate.
All events remain in SQLite. The existing bounded event window still applies; the
tab is a current observation view, not a complete historical ledger.

Generic X extraction reuses validated raw responses for identical same-day content,
prompts, and configured provider/model. Normalization and universe checks still run.
New content, a changed prompt/universe/model, or a new day causes a miss. Malformed
cache data is ignored; a file lock and atomic replacement protect concurrent calls.
Poll timestamps and seen-counts stay in acquisition records rather than semantic
source documents. Explicit cache-hit/model-call counts are included in receipts.
Guru episode interpretation already has its own unchanged-record reuse mechanism.
This change does not alter X polling or cache fresh market quotes or admission.

A separate publication bug let `unified-plan --config-source sheet` write activity
even without `--write-sheet`. Its fixture test used an empty temporary database and
could clear the real activity tab on an authenticated host. Activity publication now
requires the write flag. The regression test forbids a Sheet connection and asserts
that no publication was attempted. To restore the observation view, use the existing
`project-trade-source-activity` command against the canonical oldmac database, with
operator authorization. Do not trigger planning or execution to repair a display.

Verification: focused tests cover cache hits/invalidation/corruption, stable source
documents, revision identity, distinct-package preservation, current exact failures,
and absence of unintended Sheet writes. Runtime readback must verify both the
published row count and representative collapsed trades.

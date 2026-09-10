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

The operator tab is now a translation review surface: Guru, Source post, Symbols,
Our understanding, Trade details, Missing or uncertain, and Your correction. It
shows one row per post from the latest translation batch, not planner receipts or
revision IDs. The operator reset boundary in
`source_intelligence.translation_review.since` filters on the post's first retained
observation. Polling an old post again cannot refill the cleared review queue.

`activity_rows` remains an internal audit serializer: it groups revisions by source,
post, source event, and package signature, retaining idea/exact decisions and
history. All underlying events remain in SQLite; historical data is not deleted.

The review publisher preserves row positions and only writes columns A:F on normal
refreshes. Column G belongs to the operator and is never overwritten, including
while source interpretations change or new posts arrive. It retains earlier review
rows rather than dropping corrections when a post falls out of the bounded input
window. Changing a reset boundary alone does not remove already published review
rows: a future operator reset must explicitly clear those rows as well. The initial
migration from the old audit header clears its contents and removes audit-only
columns. An unrecognized header fails closed to protect operator content.

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
published row count, review-only headers, reset persistence, and preserved corrections.

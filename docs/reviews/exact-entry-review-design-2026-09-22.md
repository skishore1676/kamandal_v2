# Exact guru entry review — implementation

The working contract is exact guru entry contracts with Kamandal-managed exits. Allocation and live structure support are unchanged. Following subsequent guru exits/rolls is a separate capability and remains unresolved product scope.

## Implemented

- Hedge language reaches the interpreter even when acquisition classified an opaque smart-tag post as irrelevant. Symbols are not guessed from crypto identifiers; existing verified image interpretation must supply the evidence.
- Matching author, normalized text, and image hashes within two minutes flag possible edited-post duplicates. Both remain preserved, but their idea/exact-package projections are parked. Similarity is not asserted as authoritative edit lineage, and neither post is silently discarded.
- Interpretation-rule versioning invalidates older cached results while retaining stable episode and event identity. The first run after deployment will reinterpret retained source records once. Current-rule receipts then reuse normally. Duplicate holds also apply to reused episodes.
- A symbol-less portfolio report is no longer converted into an entry merely because the text says performance “added” a percentage.
- Each successful source compilation produces an `exact_entry_review/<guru>/<compilation>.json` artifact. It separates templates, follow-ups, unresolved contracts/expiration dates, invalid legs, covered-call share requirements, lifecycle gaps, possible edits, and live structure support. It never grants execution authorization.
- `scripts/review_exact_entries.py` renders JSON and CSV from a saved runtime audit snapshot with no API or trading calls.

## Acceptance boundaries

“Reviewable contracts” means available structured entry evidence has passed the listed checks, not independent correctness verification, executable quotes, verified positions, matching total guru size, or authorization to submit. Contract quantities describe the reconstructed package; they are not a capital allocation rule. Acquisition completeness remains explicit. Existing live freshness, structure, source policy, broker, and risk gates continue to own admission.

The runtime snapshot review flags both versions of the missing September 17 SPX hedge. It cannot retrospectively recover the image translation without a new interpreter pass; the empty old episode is not relabeled as successfully translated. Regression tests prove that the repaired filter invokes interpretation and obsolete cached empty episodes are not reused.

## Deployment considerations

This change is prepared separately from the already deployed TypeSafe comparison. It changes source interpretation and tightens duplicate admission; do not conflate its test results with production proof. Before deployment, review the one-time re-interpretation scope and hold dispositions. Existing historical source freshness checks must remain enabled. No capital allocation or live-structure expansion is included.

Remaining work before a broad live exact-copy experiment: source acquisition pagination/continuity in Birdclaw, authoritative edit lineage, expiration resolution from verified contract calendars, share-coverage verification, and supported execution/management contracts for additional structures. No guessed expiry, blind crypto-to-index alias, or widened execution gate was introduced.

Offline review:

```sh
.venv/bin/python scripts/review_exact_entries.py \
  --snapshot outputs/guru-translation-audit-20260922/runtime-snapshot.json \
  --output outputs/exact-entry-review-20260922
```

# TypeSafe guru interpretation experiment — 22 September 2026

**Recommendation: retain the current interpreter. Do not integrate TypeSafe into production on this evidence.** Jev is inexpensive and fast, but this first text-decision adapter made consequential interpretation mistakes. Conservative triage offered no incremental skips beyond existing rules. Retain the offline module for future comparisons; a separate production service is not justified.

## What was built and run

`scripts/evaluate_typesafe_gurus.py` submits source-only text decisions to TypeSafe, using `TYPE_SAFE_KEY` from the environment or local `.env`. It constructs separate symbol questions, allowing mixed closing/opening posts, and supplies up to ten earlier posts from the same author. Future posts, annotations, expected answers, baseline outputs, image transcriptions, and private account data are not submitted. The request prompt explicitly warns against treating retrospective quotations as new entries.

The module saves exact requests, returned model versions, answers, token receipts, elapsed times, and request hashes. Completed requests are reused on resume. Calls are bounded, have a timeout, and are not automatically retried. Invalid answers fail validation. No Sheet writer, acquisition job, planner, or broker is invoked. The key stayed on the development machine; no oldmac transfer was needed.

Executed 38 requests against **jev-1.13.0**, using the existing retained August 22–September 4 corpus. This is an incomplete two-week capture, not a complete timeline. Labels were previously produced through source inspection; they are not all operator-approved. It contains eight labeled directional ideas and five posts with retained media. This first adapter tests direction and entry recognition, not full translation or exact option-package extraction.

## Results

| Measure | TypeSafe text adapter | Saved Astra baseline |
|---|---:|---:|
| Correct directional ideas | 7 / 8 | 8 / 8 |
| Directional recall | 87.5% | 100% |
| Directional precision | 70% | 100% |
| False new entries | 3 | 0 |
| Exact option packages | Not implemented/supported by this adapter | 6 / 6 |
| Requests / model attempts | 38 | 3 |
| Reported tokens | 79,821 input tokens | 41,183 total tokens |
| Recorded aggregate elapsed time | 15.18 seconds | 229.78 seconds |

The saved baseline is the prior `gpt-6-astra`, low-reasoning evaluation of the same corpus, not a fresh run of today's production configuration. It processes images and batches posts, while this adapter processes text one post at a time. Timing and token figures therefore describe different workflows and are not controlled model-speed or cost comparisons. TypeSafe median request latency was 0.348 seconds; maximum was 1.733 seconds.

At the published price of $0.042 per million input tokens, estimated TypeSafe inference cost was **$0.003352482** (about one third of one cent). This is a calculated estimate, not an invoice. The saved Codex baseline reports total tokens, not billable dollars or subscription credits; no valid percentage dollar saving can be calculated. Replaying retained posts required **zero X API calls**. Lower price per token did not mean fewer tokens in this implementation.

### Historical week slices

| Retained window | Posts | Correct / expected ideas | False new entries |
|---|---:|---:|---:|
| August 24–30 | 20 | 4 / 4 | 2 |
| August 31–September 4 | 13 | 3 / 4 | 1 |

Five earlier posts remain in the complete 38-post result and chronological context. Neither weekly slice represents complete provider capture. There are too few positive examples for a reliable general accuracy estimate; one missed idea changes aggregate recall by 12.5 percentage points.

## Consequential mistakes

1. **AFRM retrospective trade:** a quoted bullish opening followed by “sell to close” and expiring puts was labeled as a new bullish entry. Choice confidence was **0.84**.
2. **AAPL / PYPL mixed post:** “Closed $200 winner in $WDC and $AAPL New call diagonal in $PYPL” correctly produced PYPL, but also incorrectly produced a new bullish AAPL entry, at **0.80** confidence.
3. **DOCU expiry update:** a quoted old trade followed by “looks to expire” was labeled as a new bullish entry, at **0.41** confidence.
4. **SPX image-dependent entry:** the captured text had a corrupted symbol. The adapter's literal cashtag candidate generation could not recover SPX from the image, so this was a missed idea. This is a text-input/adapter limitation, not evidence that Jev selected the wrong symbol from a correct candidate list.

These errors matter more than high overall post-classification accuracy. The first three would introduce ideas that the guru was not newly proposing. High model confidence did not establish correctness.

## Hybrid check

After seeing the first-pass results, an explicitly exploratory policy was evaluated from saved responses:

- Run the current deterministic compiler rules first.
- Skip only non-media posts with both new-entry probability and need-for-more-evidence probability at or below 0.10.
- Send everything else to the existing interpreter.

Result: **14 deterministic bypasses, 0 additional skips, 24 LLM fallbacks**. Thus this conservative policy adds a model call to unresolved work without demonstrating reduced LLM work. This is a routing simulation, not an executed hybrid interpretation run. Thresholds were selected after observing the first pass and must not be presented as held-out validation. Cache reuse would further reduce the remaining production workload; these counts do not simulate production cache state.

We should not tune thresholds repeatedly on these same 38 examples and then claim success. A future challenge would need a different held-out corpus and a sufficiently larger set of actionable entries.

## Final recommendation

Keep the current multimodal interpreter and existing deterministic/cache layers. The TypeSafe-only adapter falls below the proposed 90% recall target and has unacceptable false-entry behavior on this sample. It cannot currently replace the full option-leg translation job. Conservative triage did not demonstrate savings beyond current rules.

If a concrete future need appears, test TypeSafe as an advisory classification or translation-consistency checker, where errors lead to review rather than invented trade ideas. That remains a hypothesis: this experiment did not test a verifier. No live deployment is recommended from the present result.

## Reproduction and evidence

Run from the repository root:

```sh
.venv/bin/python scripts/evaluate_typesafe_gurus.py --output outputs/typesafe-gurus-20260922
# Verify saved responses and recompute scores with no new calls:
.venv/bin/python scripts/evaluate_typesafe_gurus.py --output outputs/typesafe-gurus-20260922 --max-calls 0
.venv/bin/python -m pytest -q tests/test_typesafe_guru_evaluation.py tests/test_guru_history_evaluation.py
```

All **9 focused tests passed**, including label isolation, bounded context, mixed-symbol candidate handling, malformed-response rejection, and unknown-answer exclusion. Zero-call replay completed successfully. No production code path or runtime configuration was modified.

Evidence files:

- `outputs/typesafe-gurus-20260922/receipts.jsonl`: 38 request/response receipts; no credentials.
- `outputs/typesafe-gurus-20260922/results.json`: per-post labels, predictions, mistakes, usage, baseline identity, exploratory routing, and effect flags.
- `outputs/guru-history-20260905/holdout-baseline.json`: saved comparison baseline.
- `tests/fixtures/guru_history_20260905/manifest.json`: capture and annotation limitations.

Official references: [TypeSafe API](https://docs.typesafe.ai/api), [text-only input support](https://docs.typesafe.ai/concepts/state), [published pricing](https://typesafe.ai/blog/introducing-system-one-models-and-jev).

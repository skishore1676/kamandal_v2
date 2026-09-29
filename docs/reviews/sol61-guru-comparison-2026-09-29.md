# Kamandal Sol 6.1 qualification — 29 September 2026

**Decision: retain GPT-6 Astra, low reasoning, for source_episode_interpreter.** Sol 6.1 remains on the other broker-selected application routes already migrated. A same-prompt check exposed omission of five opening option packages that current Astra preserved.

The preceding fleet rollout changed this route before the quality comparison. At the operator's request to test before deciding, the runtime interpreter was restored to Astra during evaluation. This decision persists that exception in source defaults, the future history evaluator and broker monitoring. The Codex-only chain, no-fallback behavior, read-only sandbox, approval-never, schedules and trading gates remain unchanged.

## Results

| Test | Astra low | Sol 6.1 low |
| --- | --- | --- |
| Saved labeled 38-post corpus: correct directional ideas | 8/8 | 8/8 |
| Same corpus: false openings | 0 | 0 |
| Same corpus: exact opening packages | 6/6; no wrong complete packages | 6/6; no wrong complete packages |
| Fresh current-compiler labeled control | 8/8 ideas; 6/6 packages; no false openings | Same |
| September 21 Greg snapshot, fresh current-compiler control | Five specified opening packages retained | All five omitted; directional ideas retained |
| September 29 natural inference snapshots | Historical reference | All five new posts agree on opening decisions |

### Controlled labeled comparison

Source profiles, initial prompts and five attached images matched exactly between fresh Astra and Sol 6.1. Both made two successful calls with no compiler failures or repair calls.

| Measure | Current Astra | Sol 6.1 |
| --- | ---: | ---: |
| Reported total tokens | 40,883 | 42,701 |
| Aggregate elapsed seconds | 176.400 | 172.095 |
| Attempts | 2 | 2 |

The older saved Astra run used 41,183 tokens, 229.777 seconds and three attempts. Its profile/prompt hashes differ from the current compiler; it is a historical score reference, not a controlled latency/cost comparison.

In the current controlled run, Sol 6.1 used about 4.4% more reported total tokens and about 2.4% less aggregate elapsed time. This is one run of each model, partly alongside another evaluator, not a stable latency estimate. Native CLI totals lack billable input/output/cache splits; realized credit savings cannot be calculated from these receipts.

A closing-post taxonomy/direction difference appeared in the labeled control. Opening-package legs and prices agreed; this was not a new-entry difference. The package scorer checks symbol, action and every leg's side/effect, strike, expiration, type and quantity. It does not prove downstream admission, displayed-price accuracy across all cases, or economic performance.

### Confirmed package-retention difference

Two September 21/22 Greg snapshots were replayed. Because historical profiles and cache/compiler versions differ, the first snapshot was also run with current Astra under the **same current prompts, profile, packet, history and image availability** as Sol 6.1.

Both models recognized the following as bullish scale-in ideas. Current Astra also returned complete packages; Sol 6.1 returned none for these five structures:

| Source text | Astra's retained opening terms | Sol 6.1 |
| --- | --- | --- |
| added $BBY Oct 2 Exp 95 calls | Oct 2, 2026; buy 95 call | Idea only |
| added some $BRK.B Oct 2 Exp 515/525 call spreads | Oct 2, 2026; buy 515 call / sell 525 call | Idea only |
| added some $OXY Oct 2 Exp 64 calls and $DVN Oct 2 Exp 52 calls | OXY Oct 2 buy 64 call; DVN Oct 2 buy 52 call | Both ideas only |
| Added $BP Oct 2 Exp 47.50 calls | Oct 2, 2026; buy 47.50 call | Idea only |

The posts provide expiration, strike and option type directly. Astra used normalized one-unit packages, including a 1:1 spread ratio; that does not establish the author's actual position size. These recent posts have no operator-approved per-post labels. Nonetheless, dropping those supplied terms is material for guru-opening-contract replication. Equal directional recall does not establish equivalent exact-package retention.

The existing Greg grammar permits standard shorthand to provide normalized ratios where strikes and expiry are explicit. This supports withholding replacement; it does not establish that every Astra interpretation is correct. We did not tune prompts to make Sol 6.1 pass these examples or treat disagreement as independent alpha evidence.

### Today: all four actual inference snapshots

Cache-only compilations were excluded. These are all September 29 compilations with new Astra interpreter calls:

| Source / original UTC compilation time | Newly challenged posts | Opening decision/package changes |
| --- | ---: | ---: |
| Greg 13:15:11 | 1 | 0 |
| Greg 14:15:19 | 2 | 0 |
| Mike 13:16:04 | 1 | 0 |
| Mike 16:46:46 | 1 | 0 |

All reconstructed compiler prompt hashes match the historical compilation hashes. The challenger adds the standard evaluation preamble forbidding tools, browsing and local file reads; this is not byte-identical to the entire production harness. History was reconstructed only from earlier retained runs and checked for future publication times. Challenger outputs never enter that history.

There are **five distinct newly challenged posts**, not hundreds of independent cached examples. The CCL follow-up differs in retained incomplete closing-package diagnostics, but both keep it as adjustment/residual with planner_new_entry=false. ISRG's incomplete-expiration wording differs but both hold the package incomplete and retain the same directional idea. Neither is a new-entry disagreement.

## Scope and safety

A 14-snapshot manifest was frozen before natural challenger output: first inference run per source per weekday September 21–25, plus all four September 29 inference runs. After the identical-prompt Astra control confirmed package loss, optional older repetitions were stopped and today's four cases were completed separately. **This is not a completed 14-case sweep.** Two older cases completed; a third was interrupted. Its saved requests and metering evidence are retained, but its full response and usage are unavailable in a completed case report and excluded from scored totals.

The harness caps each invocation at 32 attempts / 750,000 reported tokens, reuses completed case files and records exact public-source requests and image hashes. It never sends labels or the current post's baseline answer to the challenger. Earlier baseline interpretations are supplied only as chronological context available to the original run. The labeled test separately caps attempts at six / 150,000 tokens.

No acquisition, planner, source activation, Sheet writer, broker order client, live/shadow admission or external send was invoked. All effect flags are false. Existing native authentication was used. Tests consume existing Codex usage, not a paid API purchase or invoice.

Three harness tests plus four existing history/routing tests passed: frozen selection, no-future history, budget-before-call behavior, normalized contract representations, meaningful price/quantity differences and preservation of unrelated actor routes.

## Evidence

Canonical oldmac directory: /Users/sunny/Documents/kamandal_v2/outputs/sol61-guru-20260929/

- holdout-sol61.json: labeled challenger.
- holdout-astra-current.json: controlled current Astra reference.
- controlled-differences.json: initial labeled-control diagnostics.
- natural/manifest.json and natural/inputs/: original frozen selection and source inputs.
- natural/results/: two completed older challenger snapshots.
- natural/stop-receipt.json: stop reason and partial-evidence scope.
- natural-astra-control/results/: fresh controlled Greg reference.
- natural-astra-controlled-differences.json: four posts / five omitted packages, with identical current prompt hashes.
- natural-today/results/ and natural-today/analysis.json: today's four comparisons.
- natural/analysis.json: historical normalized diagnostics, with compiler drift identified.

The comparator distinguishes opening-entry changes from follow-up diagnostics and normalizes leg dates/order, numeric formatting and harmless blocker wording. Raw answers and requests remain available; package omission is not normalized away.

Analyze saved current-day results without new calls:

    cd /Users/sunny/Documents/kamandal_v2
    PYTHONPATH=src:scripts .venv/bin/python scripts/compare_sol61_history.py --output outputs/sol61-guru-20260929/natural-today --analyze-only

Use a new output directory for future challengers. Re-running an unfinished original case can consume more model calls even though completed case files are reused.

## Next qualification requirement

Keep Astra for this exact interpreter. Before a future switch, retain the supplied opening expiry/strike/type and normalized package grammar in the four shorthand posts above while passing the labeled false-opening and exact-leg checks. Do not invent author position size. This is a concrete capability gap; another broad rollout or blended live interpreter is unnecessary.

Official [Codex pricing](https://learn.chatgpt.com/docs/pricing) gives Sol 6.1 one-fifth Astra's standard input/output rates and one-tenth its cached-input rate. Potential savings do not compensate for losing the source contract terms this owner workflow needs.

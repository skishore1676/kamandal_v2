# Greg and Mike interpretation: model and machinery decision

September 5, 2026. Local evaluation and proposed changes; not deployed.

**Use Astra at low reasoning as the next candidate for the shared source interpreter.** The strongest improvement came from supplying source conventions, keeping images available, and fixing the adapters around the model. There is no evidence from this experiment that a multi-agent workflow or high/extra-high reasoning is necessary.

The product objective is **guru entry ideas under Kamandal management**. The interpreter should recover a source's actionable meaning and evidence, then let the existing portfolio process decide admission, sizing, execution, and management. Results would measure that combination, not the guru's independently managed returns.

## What was tested

The existing operator-reviewed text corpus contains 29 posts. It represents 28 expected idea opportunities, but 20 are generated through deterministic earnings-template expansion. The useful model headline is therefore recovery of the **eight non-template ideas**. Core scoring checks opening/scale-in category, symbol, direction, and template identity; structure agreement is reported separately. Exact option terms require a separate test.

The image test uses six previously reviewed Mike posts containing seven original screenshot images. Its 13 labeled packages contain seven openings and six management packages. The opening score below requires exact agreement on symbol, action, leg expiry, strike, option type, side/effect, and quantity. It does not assess displayed prices, locator agreement, execution eligibility, or profitability.

| Model and effort | Text ideas recovered, out of 8 | Screenshot openings, out of 7 | Reported text tokens | Reported image tokens |
|---|---:|---:|---:|---:|
| Terra medium | 7 | 5 | 24,845 | 41,442 |
| Terra high, exploratory | 6 | Not tested | 44,358 | — |
| Sol low | 6 | 7 | 21,000 | 45,447 |
| Astra low, first pass | 7 | 7 | 21,727 | 18,186 |
| Astra low, repeat | 6 | 7 | 17,307 | 21,017 |
| Astra low, clarified source brief | 8 | Not rerun | 20,357 | — |

Text figures use direction-aware interpretation scoring with compiler corrections, not the old exact-string score. Earlier answers were reprocessed locally where available; this consumed no additional model calls. Terra high used an older CLI and an earlier harness, so its result is exploratory, not a controlled estimate of the benefit of higher reasoning.

Before the source-brief clarification, Astra's text results varied between 75% and 87.5%. In the final run it recovered all eight core ideas and seven matching structures. The eighth was COP `bull_call_spread` versus the canonical `call_spread`; replaying the same answer after adding that alias recovered all eight structures too. The replay passed the legacy hard gate, with zero false new entries and zero invented media packages. It is a replay of the same answer, not another independent model success.

Astra recovered all 13 image packages in both runs; Sol recovered all 13 in one run. Terra medium recovered five, all openings, missing the complex SPX and NG openings. The first Astra and Terra image results initially failed in the adapter; their scores above come from replaying their saved answers after correcting the expiration parser.

## What actually needed improvement

1. **Source briefs were missing conventions already present in Suman's annotations.** Greg's standalone ticker, expiry, and structure can be a proposed opening without a verb. Mike's call crab can carry an independently understandable bullish idea even when its exact structure is unsupported. The profiles now state these conventions and keep unresolved exact dates or missing exact-leg images separate from an independently supported idea.
2. **The exact-leg adapter rejected a date format the prompt explicitly allowed.** `Aug 28 2026` was rejected even when the model had read the right date. It now accepts the explicit year and preserves historical years. A month without a day remains insufficient for exact legs.
3. **Normalization could erase correct interpretation.** The first matching calendar rule overwrote a correctly identified resulting diagonal in a roll. Literal evidence now supports retaining the model's matching resulting structure. Profile aliases normalize `call_vertical`, `put_vertical`, and `bull_call_spread` without creating new planner playbooks.
4. **Evaluation hid the difference between model and application errors.** It now retains pre-compiler answers, attempt receipts, prompt hashes, token totals, and semantic scores alongside legacy scores. A real-image regression harness checks exact leg fields. The old score mistook synonyms for false entries and did not check direction.

These are local changes on `codex/guru-interpretation-evaluation`, based on runtime/origin commit `1bf7832`, in `/private/tmp/kamandal-guru-eval-20260905`. Profile versions were incremented to invalidate cached interpretation under older guidance. Production actor bindings, operating modes, and execution settings were not changed.

## Keep the machinery small

Use one shared path:

**Captured post/transcript + verified media + source profile + bounded relevant history → one multimodal interpreter → atomic events and evidence → deterministic validation → common Kamandal idea intake and portfolio decisions.**

One post may contain a close, an old quoted entry, and a new idea. Split these into events. Retain enough history to avoid reopening a quoted or repeated entry; this does not require reproducing the guru's management system. Preserve source and opportunity identity for attribution and deduplication. If both an idea and exact-entry representation exist, they must not create duplicate exposure.

Adding a guru should mean adding capture/profile configuration and reviewed examples. It should not require another planner, executor, or independently operating agent. The interpreter should understand unsupported structures and retain their meaning; deterministic policy still decides whether Kamandal can use them.

That last boundary is not completely solved by this patch: unsupported structures can still park at the current idea adapter. The current exact projection also requires verified image provenance and excludes scale-in exact actions, leaving some text-only Greg entries unsupported. This work does not claim every recovered idea now reaches the portfolio or every exact entry can be simulated.

## Tokens and model availability

Astra low used fewer reported tokens in these batches than Terra medium or Sol low, especially for images. Astra completed each image run in one turn; Terra and Sol each used a repair turn. Text batch time was about 112 seconds for Terra medium, 103 seconds for Sol low, and 143–193 seconds for the initial Astra runs. The clarified Astra run took about 142 seconds. Image times were about 90 seconds for Terra, 132 seconds for Sol, and 93–95 seconds for Astra. Fewer tokens did not consistently mean lower latency.

These are native Codex total-token receipts, including reported attempts. They have no reliable input/output/cache breakdown and are not a subscription-credit or dollar estimate. Official model documentation lists different API rates for [Astra](https://developers.openai.com/api/docs/models/gpt-6-astra) and [Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol); this experiment cannot establish which is cheaper on Suman's Codex plan.

The laptop's default Codex 0.144.4 rejected Astra as requiring a newer CLI. Evaluation succeeded through a private Codex 0.153.4 installation, using the existing native authentication path. No global installation or authentication was changed. Production-host model compatibility still needs a read-only canary before any approved route change.

## Proof and remaining decision

All model runs used the existing Agent Broker evaluation lane. The harness did not publish ideas, invoke the planner, admit shadow/live positions, write Sheets, or place orders. It instructed models to use only supplied evidence, isolated their working directory, and did not put answer labels into the prompt. The text harness does derive missing-media placeholders partly from fixture metadata and uses synthetic publication timestamps; it is not a complete real-world capture replay.

The final local verification passed **72 tests** across the compiler, extraction, scoring, image harness, correspondent activation/signals, and observed-package planning. `git diff --check` passed. An initial verification invocation imported the older installed checkout because `PYTHONPATH` was missing; rerunning against the worktree's `src` resolved collection and passed.

**The result supports Astra-low as the next candidate, not an 80–90% production-accuracy claim.** These are small, familiar examples, and the final brief was tuned against them. There is no fresh holdout or newly scheduled production run in this evidence. Nor does this establish alpha.

Next, assess a frozen set of newly captured Greg and Mike posts, including actual cached images and relevant prior context, before further prompt tuning. Score new-idea precision and recall, direction, unsupported-but-understood ideas, exact opening fields, and old-entry reopening separately. Keep deterministic template results separate. Then verify a natural intake cycle and the adapter-to-common-portfolio handoff. Any deployment or operating-mode change remains an explicit operator decision.

## Evidence locations

- Annotated workbook: `/Users/suman/code/kamandal_v2/outputs/01a03dc0-3e42-7cd0-843d-fbd328069c01/trade-source-routing-review-2026-09-03.xlsx`, `Review!F:G`.
- Model runs, raw answers, receipts and zero-call replays: `/Users/suman/code/kamandal_v2/outputs/guru-lane-audit-20260905/model-comparison/`.
- Final text evidence: `astra-low-source-guidance/latest.json` and `astra-low-source-guidance/reprocessed.json` within that directory.
- Image evidence: `vision-astra-low-rescored.json`, `vision-astra-low-repeat.json`, `vision-terra-medium-rescored.json`, and `vision-sol-low.json`.
- Earlier runtime audit: `/Users/suman/code/kamandal_v2/outputs/guru-lane-audit-20260905/REVIEW.md`.

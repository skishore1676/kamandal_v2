# Guru translation and exact-replication audit — 22 September 2026

**Conclusion: not all captured trades were translated correctly, and the current lane is not ready to be described as exact guru replication.** The TypeSafe deployment did not cause these findings: it adds an isolated comparison after the existing X job; Astra remains the primary interpreter.

## Scope and evidence

Read oldmac at deployed commit `5347393`. Latest retained activation was September 21 at 19:00:13 UTC. Examined all 104 retained posts and their episode outputs: Greg 68 and Mike 36, spanning September 2–21 (Greg starts September 4). All 104 have an episode artifact; that is processing coverage, not semantic correctness. A browser review of decisive examples checked source wording, edited-post identity, and original order-ticket images. This was not independent visual validation of every image or a measured 104-post accuracy score.

The source owner reports **incomplete acquisition for both gurus**. Its latest requests each returned the full requested limit of ten posts with `truncated_possible`. This does not prove posts were missed, but it prevents a claim that every guru tweet was acquired.

Full retained inputs and outputs: `outputs/guru-translation-audit-20260922/runtime-snapshot.json`.
Per-post inventory with flags: `outputs/guru-translation-audit-20260922/all-posts.csv` (104 rows).

## Browser-confirmed missed SPX hedge

The browser displays “Downside hedge in $SPX” for Mike's [September 17 post](https://x.com/TraderMikeyB/status/2100602638781829237). The earlier [post version](https://x.com/TraderMikeyB/status/2100602580208357436) explicitly links to that latest version.

The order-ticket image shows an SPX put butterfly expiring September 30:

| Action | Quantity | Strike | Type |
|---|---:|---:|---|
| Buy to open | 1 | 7400 | Put |
| Sell to open | 2 | 7500 | Put |
| Buy to open | 1 | 7600 | Put |

Displayed debit: 11.50. The ticket says this is a downside hedge. This is source reconstruction, not an instruction to enter a now-historical trade.

Our packet instead stores an Ethereum smart-tag address in the text, no literal symbol, and classification `irrelevant`. Both versions have the same cached image hash. `_obvious_noise` does not recognize `hedge` as trade language, so it emits an empty event list before image interpretation. **One identifiable trade was lost across two captured edit versions.** Repair must both preserve hedge/media evidence and consolidate edit lineage; merely letting both versions through could introduce duplicate entry opportunities. The source smart-tag should not be mapped blindly: the image supplies the index identity.

## Confirmed correct or appropriately incomplete examples

The [September 21 IBIT/NDX post](https://x.com/TraderMikeyB/status/2102031372281901556) matches the stored option legs:

- NDX September 30 put butterfly: buy 29400, sell two 29500, buy 29600; displayed 3.50 debit.
- IBIT October 16 51 call sold for displayed 1.00 credit **against 100 existing shares**.

The IBIT option leg is transcribed correctly, but copying that leg alone is not copying the covered position. Share coverage is stated in prose, not represented as an equity leg in the option package. No claim is made that the current executor would submit it; the live exact-structure gate currently blocks it.

Greg's [AMGN/ETN post](https://x.com/harmongreg/status/2102047501297586285) confirms the retained wording. ETN's October 9 split call fly is reconstructed as buy 435, sell 455, sell 460, buy 470. AMGN's 395/420 call spread and the nearby [LLY risk reversal](https://x.com/harmongreg/status/2102052394724929664) specify only October; the compiler retains a monthly-expiration blocker rather than inventing a calendar date. Directional understanding exists, but exact replication remains incomplete until expiration is resolved against authoritative contract evidence.

## Other findings from all retained episode outputs

- **22 posts have unresolved or ambiguous lifecycle links.** Some closing tickets have complete leg transcription while linkage to the opening event is still unresolved. This is not equivalent to 22 wrong translations, but it prevents a blanket claim of exact lifecycle replication.
- **10 posts produce numbered template openings.** These are configured source ideas, not proof that the guru actually entered every generated structure. They must be separated from confirmed personal trades in a replication experiment.
- **6 posts contain non-template entry events without complete packages.** This includes missing source content and unresolved terms; it is not an error count.
- A Greg portfolio performance post saying performance “added 1.255%” is normalized to `scale_in` with no symbol, while its thesis correctly says no individual trading action was supplied. It is parked, so this is a labeling defect rather than evidence of a submitted trade.
- The two empty SPX hedge episodes are a confirmed interpretation-coverage defect. Many other empty episodes are ordinary non-trading posts.

## Execution is a separate limitation

Verified directly in oldmac's `src/kamandal_v2/planner/observed_package_candidates.py`:

- The live exact-package path only admits `short_strangle`; other structures are parked as `unsupported_live_exact_structure`.
- Futures options are unsupported there.
- Close/roll/adjust packages are retained as `benchmark_only_action`, not automatically replayed as guru management instructions.

Both gurus' retained activation modes say idea `live` and exact package `live`, but those broad mode labels do not override the structure gates. Much of Mike's calendar/diagonal/butterfly activity and Greg's call spreads therefore cannot currently be copied as live exact packages through this path.

## What changed

The previous deployment added `run_typesafe_shadow.py`, the historical evaluator, tests, trial documentation, and a bounded hook after the existing X job. It did not replace Astra, expand live exact structures, change the allocation, or enable following guru exits. During this audit, no production interpretation, sizing, eligibility, or execution changes were made. Only audit artifacts were written.

## Required improvements before the proposed capital experiment

1. Repair hedge/media relevance filtering and preserve smart-tag evidence; add the confirmed SPX example as a regression case.
2. Consolidate edited posts and make cache invalidation deliberate so a corrected interpretation is neither skipped forever nor replayed as a duplicate entry.
3. Separate source suggestions/templates from confirmed guru entries, and label incomplete exact packages explicitly in the review surface.
4. Resolve exact expirations, required existing holdings, and lifecycle references from source evidence. Keep unresolved cases out of exact replication.
5. Establish complete enough acquisition and prompt delivery for short-dated trades; a ten-post truncated acquisition receipt is not a completeness guarantee.
6. Decide whether replication means exact entry contracts under Kamandal management, or following the guru's subsequent adjustments/exits as well. The latter is a separate execution capability, not a translation tweak.
7. Verify the intended structures through the actual supported execution path before changing allocation. Forty percent allocation alone does not specify the risk budget, contract scaling, or treatment of uncovered/portfolio-dependent positions.

The appropriate next result is a source-to-contract review set with explicit pass/block reasons and no unresolved duplicates. Confidence filtering and cheap classification do not address the demonstrated capture/filtering and execution-boundary gaps.

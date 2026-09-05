# Guru history validation and rollout — September 5, 2026

The approved bounded experiment supports using Astra low for the shared source interpreter. It also exposed two concrete infrastructure defects: missing X photo expansions and Agent Broker rewriting exact model choices through the High tier. Both need deployment alongside the interpretation changes. No guru-specific manager or execution engine is introduced.

## Evidence and limits

The August 22–September 4 retained export contained 73 posts. Removing every previously evaluated text/image post left 38 unseen posts: 30 Greg and 8 Mike. All 38 were frozen before the model call; the previous operator-annotated corpus remained the development set. This uses the available sample rather than pretending to have 100 examples or complete timelines. Labels reflect source inspection and existing operator conventions, not new operator-approved annotations.

Five opening-image posts were checked against the signed-in X browser before scoring. Greg's MOS and CELH additions were also browser-confirmed after the run. The SPX source text was corrupted into an opaque blockchain identifier by the provider; its original browser post and option image identify SPX. The fixture retains the corrupted input and verified image rather than silently correcting it.

| Check | Result |
|---|---|
| Unambiguous directional ideas recovered | 8 / 8 |
| False directional openings | 0 |
| Exact opening packages, all normalized leg fields | 6 / 6 |
| Incorrect complete opening packages | 0 |
| Native model turns, including one schema repair | 3 |
| Reported total tokens | 41,183 |
| Aggregate model elapsed time | 229.777 seconds |

The headline idea score covers symbol and direction, excluding deterministic earnings-template expansion. Exact-package scoring covers expiration, strike, type, side/effect, and ratio; it does not score displayed price or image-locator consistency. Small positive denominators and incomplete capture prevent a general 90% accuracy claim. This is interpretation evidence, not alpha, fill, return, or portfolio-performance evidence. Native CLI total-token receipts are not converted into dollar estimates.

The baseline preceded the final profile/prompt corrections. A zero-model-call replay of its saved answers on the final compiler retained 8/8 ideas and 6/6 exact contracts. At each post's publication time, with a test universe containing all eight symbols, seven ideas passed the real common idea loader; SPX correctly parked as unsupported. This replay is a regression check, not a second unseen evaluation or proof of current live eligibility.

## Smallest useful changes

- One shared `source_interpreter` profile selects Astra low with bounded JSON/image input, read-only sandbox, no tools, and ephemeral native CLI execution. Other Kamandal actors keep their current assignment.
- Agent Broker adds `model_policy_tier: literal` for an explicitly evaluated actor. Existing overrides default to High as before. `scripts/configure_source_interpreter.py` inspects by default; its authorized `--apply` changes only `kamandal::source_episode_interpreter`, asserting unchanged fleet profile and other effective routes.
- Birdclaw's bounded photo enrichment requests `attachments.media_keys` and public media fields through its existing xurl boundary. On oldmac, the corrected capture accepted 10 Mike posts and cached 10 images with eight bounded enrichment reads. No authentication or X mutation changed. The receipt still says `truncated_possible` because the configured ten-post limit was reached; acquisition success does not imply complete history.
- Opaque provider identifiers cannot become equity entries without independent literal-symbol or verified-image package evidence. Missing images remain evidence blockers.
- Mike's `super_bull` remains its original four-leg structure instead of being normalized to one call-spread component. Its planner mapping remains unsupported.
- Repeated bullish call-crab examples (GOOGL and the prior GLD example) justify an explicit **idea** conversion into the existing `call_diagonal` construction family. Original source shape and `idea_reexpression=true` are retained. This expresses a bullish thesis under Kamandal's construction and management; it does not claim equal payoff, risk, or exact-copy support. Bearish crab intent does not use this mapping.

There is a separate, existing coverage gap in Greg's deterministic earnings menu. Seven symbol/post opportunities each expand into four alternative templates. The put ratio, call spread plus short put, and call calendar plus short put remain unsupported; the short-strangle template already has a mapping. Those 28 template alternatives are excluded from the model-accuracy score and should not be represented as 28 independent directional convictions. This rollout does not solve their construction, risk, or management requirements. Likewise, acceptance by the common idea loader does not prove an eligible executable playbook exists for every emitted structure.

No new full strategy is justified by this sample alone. Keep collecting distinct unsupported opportunities and add shared payoff support only when recurrence and portfolio utility justify risk, entry, reconciliation, and management work. Do not rerun all history or compare every model by default. This evaluated sample is now regression material; any later confidence claim needs newly frozen evidence.

## Verification and operations

Local suites: 857 Kamandal tests, 250 Agent Broker tests. Birdclaw public request, refresh, sanitizer/export, and media-cache suites pass locally and on oldmac. Agent Broker's 250 tests also pass on oldmac using Kamandal's test Python (the broker service environment itself does not install pytest).

Deployment order: Birdclaw `963acd9`, Agent Broker `fce4402`, then this Kamandal change at an idle scheduled-job boundary. Apply the one actor route only after the broker supports `literal`. Read back the route and run a bounded, effect-free compilation through the production actor. Verify the canonical Sheet policy before/after; no source ceiling, playbook eligibility, live risk setting, scheduler, order state, or portfolio manager is changed by this rollout.

The receipt and raw answers live in the private local artifact folder `outputs/guru-history-20260905/`: `holdout-baseline.json`, `final-code-replay.json`, `mike-capture-after.json`, and deployment readbacks. The frozen source corpus and capped harness are committed under `tests/fixtures/guru_history_20260905/` and `scripts/evaluate_guru_history.py`. A historical model run allows at most six turns and stops before another turn once reported tokens reach 150,000 (a single turn can overshoot). Production compilation remains bounded by its existing packet and one-repair limits.

Historical posts were evaluated in isolation and never published into the active idea directory. A natural scheduled ingestion and subsequent portfolio decision are separate evidence stages; deployment or a canary must not be labeled as those stages.

## Completed oldmac readback

Functional deployment: Kamandal `98cb3a4`, Agent Broker `fce4402`, Birdclaw `963acd9`. Kamandal's full oldmac suite passed **857 tests** in 81.10 seconds. All Kamandal launchd jobs were idle with zero last exits at the deployment boundary; existing untracked runtime databases and outputs were preserved.

The one actor route was applied and read back as `gpt-6-astra`, `reasoning_effort=low`, `model_policy_tier=literal`; fleet profile stayed `terra-medium`, and all other effective routes were asserted unchanged. Native Codex on oldmac reports **0.153.4**. The canonical Sheet policy passed before and after with the identical hash `d635c3ffc2c621913919732ace3190619480d6099622005addbc731aaf141bbd`; two existing diagonal-overlap warnings remain unchanged.

A fresh canary used the ordinary production actor with **no explicit binding or routing bypass**. Its two actual Birdclaw posts supplied three cached public images. Both winning receipts report Codex/Astra and succeeded: two turns including one schema repair, **47,049 reported tokens**, 100.564 seconds. It identified the SNOW and GLD openings and DELL/SNOW closes without falsely reopening the closes. SNOW's textual 350-strike calendar lacked visible exact legs, so the SNOW idea remained ready for source policy while its exact-package projection parked. This is a route/input/classification canary, not a complete accuracy benchmark.

Both this canary and the historical run needed one repair for signed sell quantities. A final prompt clarification explicitly requires positive quantity magnitudes and expresses side/effect only through order codes; the strict validator is unchanged. That clarification is not claimed to have a newly measured model success rate or token saving. Saved-answer regression and focused compiler tests verify compatibility without another model sweep.

Total metered model use for this bounded history experiment plus production canary: **88,232 tokens over five turns**. Earlier development/model comparisons are separate. The canary wrote only isolated output evidence and Agent Broker metering; it did not publish active ideas, invoke the portfolio planner, or touch orders. Natural scheduled intake, source-policy admission, and economic outcomes remain subsequent evidence stages.

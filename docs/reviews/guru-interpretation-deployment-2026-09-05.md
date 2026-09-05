# Guru interpretation deployment receipt

On September 5, 2026, Suman explicitly authorized upgrading Codex on oldmac and deploying the reviewed interpretation fixes. The application deployment completed at 21:27 UTC (16:27 CT).

## Installed and verified

- Runtime: `/Users/sunny/Documents/kamandal_v2`, branch `main`.
- Application change: `1bf783231adfd574537817a382c2996d5b785134` → `ce56fa8f62ba0235281301b43d0aea16c246a04c`, fast-forward only.
- Scope: Greg/Mike source briefs and aliases, expiration parsing, structure normalization, evaluation receipts/scoring, image evaluation, tests, and accompanying docs.
- Oldmac native CLI: `0.149.1` → `0.153.4`, installed through npm into the existing `/Users/sunny/.local` prefix. Both `/Users/sunny/.local/bin/codex` and `/usr/local/bin/codex` report `0.153.4`. Existing native authentication was used; no credentials were read, copied, or changed. The laptop's global CLI was not changed.
- Oldmac Astra-low read-only smoke: succeeded with no tool calls, correctly returning two bullish HAL/COP ideas from the supplied public post. This checks model access and basic text output, not the entire production pipeline.
- Full application tests: **850 passed in 78.27 seconds** on oldmac; the full local suite also passed before deployment.
- Canonical Sheet deployment gate: passed before and after the application update. Snapshot hash remained `d635c3ffc2c621913919732ace3190619480d6099622005addbc731aaf141bbd`. Four trade-source policies and 15 enabled playbooks compiled successfully. Two pre-existing overlapping-variant warnings remain for call/put diagonal variants; no new validation errors appeared.
- No active Kamandal launchd job at the deployment boundary. No launchd edits, restarts, manually triggered trading jobs, Sheet writes, or order actions. Existing untracked runtime databases and outputs were preserved.

The deployment uses the [official Codex CLI installation/update path](https://learn.chatgpt.com/docs/codex/cli), pinned to the version already validated in local Astra evaluation.

## Deliberately separate states

The fixes are deployed, and Astra is available on oldmac. The production `source_episode_interpreter` still uses the existing `balanced` Agent Broker assignment. This deployment did not change the model route, global brain policy, source shadow/live ceilings, or strategy eligibility settings. The profile changes can improve interpretation under the existing source policy on the next natural run.

A new natural scheduled capture → interpretation → common portfolio handoff has not yet been observed after this deployment. Successful tests and a CLI smoke are not that evidence, and do not prove alpha.

The [model decision](guru-interpretation-model-decision-2026-09-05.md) records the preceding experiment. The [historical-validation plan](guru-history-validation-plan-2026-09-05.md) recommends one capped, independently labeled two-week sample, with a frozen holdout and one Astra-low pass. It also defines how to rank missing strategy support before implementing anything. The larger replay and new strategies have not been started.

## Retained evidence

Oldmac: `outputs/deploy-guru-interpretation-20260905/{receipt.json,pytest.txt,sheet-policy.json}` under the runtime checkout. Astra smoke: `/tmp/kamandal-astra-canary-20260905.json`.

Persistent laptop copy: `/Users/suman/code/kamandal_v2/outputs/guru-lane-audit-20260905/deployment/`.

Rollback, if later authorized and needed: revert application commit `ce56fa8` at an idle session boundary, preserving subsequent changes; the previous oldmac CLI package was `@openai/codex@0.149.1`. Do not reset runtime data or change operator Sheet modes as part of code rollback.

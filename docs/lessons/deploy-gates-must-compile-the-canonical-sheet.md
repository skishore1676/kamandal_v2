---
title: Deploy gates must compile the canonical Sheet
type: gotcha
area: strategy-policy
date: 2026-08-24
tags: [google-sheets, deployment, policy-compiler, fail-closed]
refs: [commit:ab8e4a7, src/kamandal_v2/strategy_engine/sheet_policy_gate.py, scripts/git_deploy_kamandal.sh]
---

# Deploy gates must compile the canonical Sheet

## What We Learned

Green tests and seed validation do not prove that the operator-authored Google
Sheet still satisfies every runtime compiler. A production deploy gate must
read the canonical Sheet once and compile that same snapshot through every
active policy contract.

## Context and Evidence

The 2026-08-24 08:50 and 09:25 CT unified planners failed closed because
`short_strangle_high_iv` lacked
`lifecycle.loss_stages.watch_multiple`. Repository fixtures already contained
the required value, so the test suite did not expose the live Sheet regression.
The repaired row compiled with `watch_multiple=2`, JSON `close_multiple=3`, and
column `loss_close_multiple=3`.

Commit `ab8e4a7` added the read-only `kamandal validate-sheet-policy` command and
made `scripts/git_deploy_kamandal.sh` run it after tests and before activation.
Its receipt includes one snapshot hash plus planner, unified, and CSA results.

## When It Applies

Use this pattern whenever configuration is authored outside Git and more than
one parser, compatibility layer, or runtime path consumes it. The gate should
remain read-only; daily snapshot capture, database writes, market-data calls,
and broker effects belong to their normal runtime owners.

## Apply It Next Time

Before activating a Kamandal commit on Old Mac, run:

```bash
.venv/bin/kamandal validate-sheet-policy
```

Require `ok=true`, record the snapshot hash, and treat any compiler error as a
deployment blocker. Do not substitute `validate-config --config-source sheet`
or fixture-only tests for this check.

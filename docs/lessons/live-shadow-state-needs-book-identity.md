---
title: Live and shadow state needs book identity at persistence
type: bug
area: unified planning and portfolio risk
date: 2026-08-18
tags: [live-shadow, account-snapshots, risk, reporting]
refs: [6dff40f, src/kamandal_v2/stores/sqlite.py, src/kamandal_v2/live/risk_manager.py, src/kamandal_v2/ops/daily_report.py]
---

# Live and shadow state needs book identity at persistence

## What We Learned

Running live and shadow sequentially is not isolation. Every persisted portfolio
fact must carry its book identity, and every safety/report consumer must request
one book explicitly.

## Context and Evidence

The unified planner saved both books into `account_snapshots` without a mode.
Shadow's `$20,000` paper account became the apparent peak for the roughly
`$11,500` live account, so live drawdown looked close to 42% and blocked every
new entry. The same unscoped latest-row query made the daily report present
shadow BPR as live BPR.

Commit `6dff40f` stores `live|shadow` in the payload and storage identity, makes
live risk/health query only live history, and reports the two books separately.
The regression test saves both account series and proves the shadow peak cannot
trip the live breaker.

The same rule applies across migrations, not only across execution modes. After
the unified manager made typed `csa_lifecycles` canonical, the daily RYG report
continued reading `shadow_fills` and displayed zero open shadow packages while
the canonical store contained two. Legacy rows remain useful historical
evidence, but current-state reporting must prefer the canonical owner and fall
back only when the typed store has never contained that book.

## When It Applies

Use this invariant for any state written by both execution modes: account
snapshots, capacity, positions, lifecycle marks, working orders, and evidence.
It is not needed for immutable source facts that are deliberately shared.

## Apply It Next Time

When a live metric resembles a paper-account default, inspect persistence
identity before tuning risk thresholds. Add a mixed-book fixture and prove each
consumer returns only its requested mode. When a unified migration leaves old
tables in place, inventory every current-state consumer and prove canonical
state wins over deliberately contradictory legacy fixtures.

## Dead Ends

Do not infer mode later from account size, write order, or naming conventions.
Those heuristics are brittle and can silently become wrong when account values
or scheduling changes.

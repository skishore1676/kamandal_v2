# House Ideas: IV-Regime Scanner

Status: **design approved, ready to implement** (2026-07-08)
Owners: Suman (product) + Claude (design/review) + implementing agent
Scope: new idea source feeding the existing normalize→match→build pipeline.
No changes to candidate building, execution, or exits.

---

## 1. Problem

Idea inflow is external (X bookmarks, YouTube) and almost entirely
directional, which maps to verticals by construction. Neutral and vol-regime
theses — the trades a 30-min/day premium-selling account should live on —
arrive only if a stranger happens to tweet the ticker. Meanwhile the system
already captures IV metrics for the whole 73-symbol universe twice daily
(`iv` / `iv_afternoon` jobs → IV observations with percentile/rank) and does
nothing proactive with them.

Evidence: in June, `short_strangle` built 6 candidates *all month*; calendars
33+11 — not because filters rejected them but because no neutral/vol ideas
existed to seed them. July 6–8: 5–13 ideas/day, still mostly directional.

## 2. Design

### 2.1 The scan (one pass per day, after morning IV capture)

For each enabled universe symbol with a fresh IV percentile:

| condition | synthetic idea emitted |
|---|---|
| IV percentile ≥ `high_ivp_min` (default 70) AND no earnings inside the symbol's event-avoid window | direction=`neutral`, thesis tags for premium-selling (matches iron_condor / defined-risk premium playbooks) |
| IV percentile ≤ `low_ivp_max` (default 30) AND no earnings inside window | direction=`neutral`, thesis tags for vol_up (matches calendars/diagonals) |

Idea fields: `idea_id = <date>_house_iv_<SYMBOL>_<high|low>`,
`source = house_iv_scan`, conviction scaled by IV extremity (e.g. ≥90th or
≤10th percentile → high, else medium), horizon from the matched playbook
family's DTE norms, notes carrying the raw metric values for audit.

### 2.2 Guardrails (the point is a trickle, not a firehose)

- `max_ideas_per_day` (default 5), ranked by IV extremity — the rest are
  logged, not emitted.
- Skip symbols with an open live position or any active idea (same-symbol
  cooldown reuses the existing idea-dedup/cooldown machinery — do not build a
  second dedup path).
- Earnings gate uses the existing earnings store + per-symbol
  `event_avoid_days_before/after` from the universe tab.
- Ideas flow through the exact same normalizer/matcher as imported ideas —
  house ideas get zero special treatment downstream, so all existing gates
  (universe, playbook match, credit gate, portfolio caps, health gate, risk
  manager) apply untouched.

### 2.3 Thesis-tag mapping — reuse, don't invent

Use the tag taxonomy already consumed by `applicable_thesis_tags` /
`applicable_direction` in the playbooks sheet (see docs/PLAYBOOK_VETTING.md).
The implementing agent must read the matcher to pick tags that actually route:
high-IV neutral → the tags iron_condor/jade_lizard playbooks accept;
low-IV neutral → the tags calendar/diagonal playbooks accept. A house idea
that matches zero playbooks is a bug (add a test).

## 3. Config

```yaml
source_intelligence:
  house_iv_scan:
    enabled: false          # rollout flag
    high_ivp_min: 70
    low_ivp_max: 30
    max_ideas_per_day: 5
    skip_if_open_position: true
```

Env overrides: `KAMANDAL_HOUSE_IV_SCAN_ENABLED`,
`KAMANDAL_HOUSE_IV_HIGH_MIN`, `KAMANDAL_HOUSE_IV_LOW_MAX`,
`KAMANDAL_HOUSE_IV_MAX_PER_DAY`.

## 4. Implementation plan

1. New module `src/kamandal_v2/intelligence/house_ideas.py`:
   `scan_house_ideas(store, config, *, today) -> list[Idea]` — pure over the
   IV observations + earnings store + open positions.
2. Wire into the `my-ideas` launchd job (runs 08:05/09:20, after 08:45 IV
   capture the 09:20 pass has fresh data) OR a dedicated `house-ideas` job at
   08:50 — implementing agent picks whichever needs less launchd churn and
   documents the choice.
3. Ideas written through the same persistence path as sheet-imported ideas
   (ideas store + `current_ideas` dir) with `source=house_iv_scan`.
4. Tests (`tests/test_house_ideas.py`):
   - high/low IV symbols emit correctly-tagged ideas; mid-IV emits nothing
   - earnings inside window → skipped
   - open-position symbol → skipped
   - daily cap enforced by extremity ranking
   - **routing test**: emitted ideas match ≥1 enabled playbook via the real
     matcher (catches tag-taxonomy drift)
   - disabled flag → no-op
5. Run full suite.

## 5. Rollout & acceptance

1. Merge with `enabled: false`, deploy to oldmac.
2. Flip env for ONE day with the risk manager conversation in mind: house
   ideas increase entry pressure, so ideally enable the risk manager (daily
   new-position cap) the same week.
3. Acceptance: house ideas appear in the my_ideas/idea flow with
   `source=house_iv_scan`, produce candidates for neutral/vol structures in
   the advisory metrics (`ideas_with_playbook_match` includes them), and the
   daily cap holds. Zero house ideas on earnings-cluster days is correct
   behavior, not a bug.

## 6. Review checklist

- [ ] Zero downstream special-casing — house ideas are ordinary ideas
- [ ] Dedup/cooldown reuses existing machinery
- [ ] Routing test against the real matcher exists and passes
- [ ] Cap + extremity ranking correct
- [ ] Earnings gate honors per-symbol universe windows, not a global constant

# Candidate Builder: Solve for the Credit Gate (Width Search)

Status: **design approved, ready to implement** (2026-07-08)
Owners: Suman (product) + Claude (design/review) + implementing agent
Scope: vertical spread construction in the planner. Iron condors optionally in
phase 2. No changes to exits, execution, or the sheet schema.

---

## 1. Problem

Since the 2026-07-02 entry-quality tightening (`min_credit_to_width_ratio`
0.20/0.25 → 0.28 on the four vertical playbooks), the builder constructs
verticals that the gate then rejects wholesale. Verified on oldmac July 6–8:

- Afternoon advisory runs built 17–31 candidates with **0 eligible** —
  `credit_width_ratio_below_min: 0.11–0.25 < 0.28` is the dominant rejection
  (~100 vertical candidates in two trading days).
- The market in the current low-IV regime does not pay 0.28 credit/width at
  the strikes the builder happens to pick. The builder never tries anything
  else.

### Root cause: construct-then-filter with a fixed template

`src/kamandal_v2/planner/candidate_builder.py`:

- `_put_spread_candidates` (line ~492) / `_call_spread_candidates` (~512):
  pick up to 4 short strikes in the playbook delta window, then exactly **one**
  long each — the strike nearest `playbook.spread_width or 5.0`.
- The credit gate runs *after* construction in `_filter_rejections`
  (line ~1214): `net_credit / _risk_width(candidate) < min_credit_to_width_ratio`
  → reject.

The short-strike dimension is already searched (4 shorts across the delta
window). The missing dimension is **width**. Credit/width ratio generally
*rises* with width for OTM verticals (the long wing you sell off gets cheap
faster than the short loses premium), so a 7.5-wide often clears a gate the
5-wide fails — at higher BPR.

### The binding interaction: BPR caps bound the search

Widening costs buying power: BPR ≈ `width × (1 − ratio) × 100` per contract.
With `live.max_bpr_per_order_by_structure.put_spread = 500` and ratio 0.28,
max width ≈ 500 / 72 ≈ **6.9 points**. The width search MUST respect the
per-structure BPR cap — a construction that clears the credit gate but blows
the BPR cap is not a candidate. This is why "just use 10-wides" is wrong.

---

## 2. Design

### 2.1 Width search for verticals

For each short strike in the existing delta window (unchanged), enumerate
long strikes across a bounded width set instead of picking one:

```
width_targets = sorted({playbook.spread_width or 5.0} | set(config widths))
                filtered to width_max_bpr_ok(width, playbook, structure, config)
```

For each `(short, width_target)` pick the long nearest that width (existing
nearest-strike logic), build the candidate, compute `ratio = net_credit /
risk_width` and estimated BPR. Then select per short:

1. Drop constructions that fail the BPR cap for the structure.
2. Among constructions that pass the credit gate, keep the **narrowest**
   (least capital for compliant credit; narrower also caps loss tighter).
3. If none pass the gate, keep the single best-ratio construction and let the
   existing filter reject it — preserving today's rejection telemetry.

Per-idea candidate caps are unchanged; the search multiplies constructions
*considered*, not candidates *emitted* (still ≤1 per short per playbook).

### 2.2 Gate-unmet telemetry (the regime signal)

When an idea+playbook produces raw constructions but zero pass the credit
gate at any width, record a per-run metric:

- `metrics.vertical_gate_unmet` (count of idea×playbook pairs)
- per-candidate reasons already carry the best ratio achieved; add
  `widths_tried=[5.0,7.5]` to the candidate `reasons` list.

This is deliberately *telemetry only* in v1 — no automatic loosening of the
gate, no automatic structure substitution. The playbook-matching layer already
attempts calendars/diagonals for the same idea; a starved vertical lane
naturally promotes them in ranking (observed live: NVDA call_diagonal traded
2026-07-06 precisely because verticals were gate-rejected). A future
`regime_pivot` that boosts long-vol structures on high gate-unmet days is
explicitly out of scope until the expectancy scorecard
(`docs/PLAYBOOK_EXPECTANCY_REPORT.md`) can judge it.

### 2.3 What we are NOT doing (alternatives rejected)

- **IV-conditional gate** (0.25 base / 0.30 high-IVR): hides the problem —
  the gate exists because sub-0.25 verticals were the negative-expectancy
  engine in June. Keep the gate honest; fix construction.
- **Searching deltas beyond the playbook window**: moves strikes toward the
  money and silently raises assignment/touch risk; the delta window is a
  policy boundary, not a tuning knob.
- **Sheet schema changes**: width search config lives in `control.yaml`, not
  new playbook columns (revisit if per-playbook widths prove necessary).

---

## 3. Config

```yaml
planner:
  vertical_width_search:
    enabled: false            # rollout flag — flip after one shadow day
    widths: [5.0, 7.5, 10.0]  # union'd with playbook.spread_width per playbook
    respect_bpr_cap: true     # never emit a construction over the per-order cap
```

Env overrides (same pattern as existing knobs, wired in `config.py`):

- `KAMANDAL_PLANNER_WIDTH_SEARCH_ENABLED`
- `KAMANDAL_PLANNER_WIDTH_SEARCH_WIDTHS` (comma-separated floats)

BPR cap source: `live.max_bpr_per_order_by_structure[structure]` falling back
to `default`, exactly as the executor resolves it — factor the resolution into
a shared helper if it is currently inline, so planner and executor cannot
drift.

---

## 4. Implementation plan

1. **Extract** the long-strike selection in `_put_spread_candidates` /
   `_call_spread_candidates` into a `_vertical_long_for_width(quotes, short,
   width_target, option_type)` helper (pure, testable).
2. **Add** `_width_targets(playbook, structure, config)` implementing §2.1
   including the BPR-cap filter (estimate BPR as
   `width × (1 − assumed_ratio) × 100` is NOT acceptable — compute from the
   actual constructed legs' net_credit: `(width − net_credit) × 100`).
3. **Rewire** the two vertical builders to search and select per §2.1 behind
   the flag; flag off → byte-identical behavior to today (assert in tests).
4. **Telemetry**: add `vertical_gate_unmet` to run metrics; append
   `widths_tried` to candidate reasons.
5. **Tests** (`tests/test_strategy_builders.py` pattern, synthetic chains):
   - 5-wide yields ratio 0.16, 7.5-wide yields 0.29 → candidate emitted at
     7.5, passes gate.
   - 10-wide clears gate but exceeds $500 BPR cap → not emitted; 7.5 chosen.
   - Nothing clears gate at any width → single best-ratio candidate emitted
     and rejected with existing reason string; `vertical_gate_unmet`
     incremented.
   - Flag disabled → constructions identical to current builder output.
   - Two widths both clear gate → narrowest wins.
6. **Docs**: one paragraph in README planner section; this doc's status line
   flipped to implemented.

Run the full suite (`.venv/bin/python -m pytest -q`); all 290+ tests must
stay green.

## 5. Rollout & acceptance

1. Merge with `enabled: false`. Deploy to oldmac (git push → pull).
2. Flip `KAMANDAL_PLANNER_WIDTH_SEARCH_ENABLED=true` in oldmac `.env`.
3. Acceptance, next trading day: afternoon advisory runs show
   `candidates_eligible > 0` on days where any width ≤ BPR cap clears 0.28;
   no emitted candidate exceeds its structure's per-order BPR cap; rejection
   log shows `widths_tried` on gate rejections.
4. Rollback: flip the env var.

## 6. Review checklist (for the reviewing agent)

- [ ] Flag off ⇒ provably unchanged output (test exists and is meaningful)
- [ ] BPR computed from constructed legs, not an assumed-ratio formula
- [ ] BPR cap resolution shared with executor, not re-implemented
- [ ] Narrowest-compliant selection, not best-ratio (capital efficiency)
- [ ] Gate-unmet telemetry visible in the advisory run JSON log
- [ ] No sheet schema changes, no exit-path changes

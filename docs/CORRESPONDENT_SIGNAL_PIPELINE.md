# Correspondent signal pipeline

Kamandal translates Birdclaw correspondent batches through declarative profiles. Greg
Harmon is the first profile, not a dedicated subsystem.

```text
configured read-only Birdclaw acquisition
  -> sanitized canonical Birdclaw posts + coverage receipt
  -> one bounded LLM opportunity question
  -> profile classification and deterministic validation
  -> optional source-neutral Cartographer question/answer
  -> durable Kamandal signal records
  -> eligibility and lifecycle gates
  -> constrained planner Idea artifact
  -> existing planner candidate and plan gates
```

## Greg's current semantics

Greg uses `interpretation_posture: inference_allowed`. The model returns only:

```json
{"action":"enter|update|exit|ignore","symbol":"AAPL","direction":"bullish|bearish|neutral","strategy_hint":"short_strangle","reason":"one short sentence"}
```

`enter` means a new opportunity to investigate now or retain as a conditional
watch; it is not a broker instruction. Only `enter` continues toward planner
eligibility. Language such as "looks to expire", "holding", "rolled", "trimmed",
or "closed" is an update or exit, not a fresh trade. The source record, text,
time, profile, and interpreter provenance are attached by Kamandal and are not
extra model questions.

- `earnings_idea`: explicit strategy language outranks the numbered template. A new
  earnings announcement without a number defaults to Greg Trade Idea 4, a
  `short_strangle`. Explicit Ideas 1–3 remain captured but parked because the existing
  planner cannot construct their compound structures.
- `weekly_ideas`: every symbol is retained for up to ten days. Kamandal asks the
  source-neutral Cartographer contract for direction, trigger, and invalidation. A
  triggered bullish answer permits call vertical/diagonal playbooks; a triggered
  bearish answer permits put diagonal playbooks. Missing OHLCV, inconclusive direction,
  waiting triggers, and out-of-universe names are explicit blockers.
- `trade_journal`: strategy and action are derived from the Kamandal profile. A bullish
  or bearish opening is handed to compatible directional vertical/diagonal playbooks;
  Kamandal does not blindly reproduce Greg's exact long option. Adjustments and closes
  remain source lifecycle facts and do not manage Kamandal positions.
- `unknown` and `irrelevant`: preserved for audit/review and never sent to the planner.

The planner artifact constrains `allowed_structures`. Existing candidate matching,
universe, IV, event, liquidity, buying-power, portfolio, preflight, ranking, and plan
gates remain authoritative. A translated source signal can therefore reach the planner
and still produce no candidate or plan.

Birdclaw's acquisition health is carried into the translation, review, and receipt as
`source_acquisition`. This records whether the configured-account poll succeeded,
failed, may have hit its result limit, or was missing. It does not invalidate an
otherwise authentic captured post, but it prevents Kamandal review from confusing
"we translated what we captured" with "Birdclaw captured everything visible on X."

## Commands

```bash
kamandal import-correspondent-signals \
  --input <birdclaw-correspondent-signals.json> \
  --profile config/correspondents/greg_harmon.yaml \
  --chart-evaluation <optional-seed-evaluation.json> \
  --config-source seed \
  --output-dir data/research/correspondent_signals
```

The import command writes immutable record snapshots, a batch translation, review
Markdown, a lifecycle projection, receipt, and `planner-ideas.yaml`. It does not run
the planner by itself.

Production activation is profile-driven through
`source_intelligence.correspondents` in `config/control.yaml`:

```bash
kamandal activate-correspondent-signals --config-source sheet
```

The scheduled X-intelligence job runs this command before its ordinary LLM extraction.
It exports each enabled profile from Birdclaw, translates it against the current Sheet
universe, and atomically replaces `data/ideas/active/correspondent_<profile>.yaml`.
Eligible ideas therefore participate in the existing planner and live-advisory flow.
The activation does not itself run the planner, write a Sheet, call a broker, admit a
live order, or place an order; all existing downstream gates remain authoritative.

The X job runs at 08:15, 09:15, 11:45, and 14:00 CT. Birdclaw refreshes its
canonical sanitized digest ten minutes before each intraday activation (and
separately at 05:30 for the morning paper), so every unified planning window
receives a bounded fresh correspondent packet without another collector or
planner lane.

For `numbered_template`, a profile may declare `bundle_idea_numbers`. When an
announcement has no literal idea number, Kamandal expands the source into those
numbered records using the existing `idea_number_map`. Explicit `Trade Idea N`
updates remain singular. Greg's earnings bundle expands to Ideas 1--4, ignores
the inaccessible article and bonus idea, parks unsupported exact structures,
and publishes only currently compatible planner ideas.

For each profile, activation exports the current Birdclaw packet first. It then builds
questions from that same packet, invokes the sibling Market Cartographer, validates the
all-effects-false response, and translates the packet once. The former path that built
a request from the previous translation has been retired. If Cartographer or its data
is unavailable, only the chart-dependent ideas remain parked; unrelated earnings or
journal opportunities can still proceed through their normal gates.

An empty translation replaces the active file with an empty idea list. Any acquisition
or translation failure fails the scheduled job and also clears every configured
correspondent active file before returning an error. A prior signal can therefore not
linger merely because the newest refresh failed.

The upstream sequence is:

```bash
# Birdclaw's normal refresh includes every acquisition-enabled correspondent.
./birdclawctl control refresh-digest-now --json

# Export only sanitized, classified evidence for this profile.
./birdclawctl export correspondent-signals \
  --profile greg_harmon --since-hours 336 --json > <bounded-local-packet.json>

# Translate the packet into constrained Kamandal planner ideas.
kamandal import-correspondent-signals \
  --input <bounded-local-packet.json> \
  --profile config/correspondents/greg_harmon.yaml \
  --config-source seed
```

These commands remain separate operational receipts. Acquisition can fail without
fabricating a clean packet, translation can succeed without automatically running the
planner, and production activation is independently visible in
`data/research/correspondent_signals/activation/latest.json`.

## Add another correspondent

1. Add a Birdclaw source profile identifying the author and post families; enable its
   bounded `acquisition` section when the account should be deliberately monitored.
2. Add `config/correspondents/<profile>.yaml`, choose
   `interpretation_posture: explicit_only|inference_allowed`, and map family names to one of the
   supported modes: `chart_watch`, `numbered_template`, `trade_journal`, or `ignore`.
3. Configure strategy regexes, numbered templates, direction-to-structure mappings,
   thesis tags, horizons, recency windows, and whether a family asks Cartographer.
4. Add one Birdclaw fixture and one Kamandal fixture.
5. Replay capture, translation, planner loading, and at least one parked case.

No Python or JavaScript change is needed for a new person whose publishing grammar fits
those modes. A genuinely new semantic family should add one reusable mode rather than
an author-specific branch.

Before changing a correspondent posture or prompt, replay a bounded recent production
window and compare Kamandal's action, symbol, direction, and reason with operator
labels. Treat that comparison as a calibration set: plumbing blockers and semantic
disagreements are separate findings, and human labels inform a later profile change
rather than silently changing the current run.

## Safety boundary

The import and fixture replay perform no broker call, order, Sheet write, external send,
shadow admission, or live admission. Production activation publishes only eligible
`Idea` records to the existing active-idea directory; it does not bypass planner,
portfolio, health, risk, preflight, ranking, live-approval, or execution gates. The
emitted prices in fixture replays are `DEMO DATA`.

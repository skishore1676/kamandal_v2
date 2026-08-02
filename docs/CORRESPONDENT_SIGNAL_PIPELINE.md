# Correspondent signal pipeline

Kamandal translates Birdclaw correspondent batches through declarative profiles. Greg
Harmon is the first profile, not a dedicated subsystem.

```text
configured read-only Birdclaw acquisition
  -> sanitized canonical Birdclaw posts + coverage receipt
  -> profile classification
  -> durable Kamandal signal records
  -> optional Market Cartographer enrichment
  -> eligibility and lifecycle gates
  -> constrained planner Idea artifact
  -> existing planner candidate and plan gates
```

## Greg's current semantics

- `earnings_idea`: explicit strategy language outranks the numbered template. Idea 4
  may emit a fresh `short_strangle` planner input. Ideas 1–3 remain captured but parked
  because the existing planner cannot construct their compound structures.
- `weekly_ideas`: every symbol is retained. A symbol needs a supplied Market
  Cartographer evaluation and a triggered confirmation before planner handoff. Missing
  OHLCV, no actionable boundary, waiting triggers, and out-of-universe names are
  explicit blockers.
- `trade_journal`: strategy and action are derived from the Kamandal profile. Only an
  opening event may become a new planner idea. Adjustments and closes stay in the
  lifecycle index.
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

The command writes immutable record snapshots, a batch translation, review Markdown,
a lifecycle projection, receipt, and `planner-ideas.yaml`. It does not run the planner.
To evaluate that artifact deliberately, pass its exact path to the ordinary `kamandal
plan` command without `--write-sheet`.

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

These commands are intentionally separate operational receipts. Acquisition can fail
without fabricating a clean packet, and translation can succeed without automatically
running the planner or promoting a plan.

## Add another correspondent

1. Add a Birdclaw source profile identifying the author and post families; enable its
   bounded `acquisition` section when the account should be deliberately monitored.
2. Add `config/correspondents/<profile>.yaml` mapping those family names to one of the
   supported modes: `chart_watch`, `numbered_template`, `trade_journal`, or `ignore`.
3. Configure strategy regexes, action regexes, numbered templates, allowed structures,
   thesis tags, horizons, and recency windows.
4. Add one Birdclaw fixture and one Kamandal fixture.
5. Replay capture, translation, planner loading, and at least one parked case.

No Python or JavaScript change is needed for a new person whose publishing grammar fits
those modes. A genuinely new semantic family should add one reusable mode rather than
an author-specific branch.

## Safety boundary

The import and fixture replay perform no broker call, order, Sheet write, external send,
shadow admission, or live admission. Nothing is deployed or scheduled by these tools.
The emitted prices in fixture replays are `DEMO DATA`.

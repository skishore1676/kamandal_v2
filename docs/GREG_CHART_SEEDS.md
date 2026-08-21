# Greg weekly chart seeds (legacy research contract)

Production correspondent activation now uses
`market_cartographer.question_request.v1` and
`market_cartographer.question_response.v1`. This seed importer remains available for
historical fixture and research replay; production no longer builds a seed request
from the previous Greg translation.

This chart-enrichment seam is now consumed by the reusable correspondent pipeline. See
`docs/CORRESPONDENT_SIGNAL_PIPELINE.md` for classification, lifecycle, and planner
handoff behavior.

Kamandal consumes Market Cartographer's versioned seeded-chart result through a
research-only quarantine:

```bash
uv run kamandal import-chart-seeds \
  --input /path/to/seed-evaluation.json \
  --output-dir data/research/chart_seeds
```

The importer validates `market_cartographer.seed_evaluation.v1`, requires every
protected effect and every evaluation's `planner_eligible` field to be false, preserves
Birdclaw source identity and Market Cartographer run identity, and writes an idempotent
`watch.json`, `review.md`, and `receipt.json` under a source/run-specific directory.

The resulting `kamandal.chart_seed_watch.v1` packet is intentionally not stored under
`data/ideas`, is not loaded by the planner, is not admitted to shadow trading, and has
no Sheet, broker, notification, or order effect. It exists so Suman can compare Greg's
source claim with the chart system's observed setup, boundary, trigger, failure, and
counter-evidence before any options-construction policy is designed.

Ownership remains:

- Birdclaw: sanitized X post, URLs, author, publication time, and symbol provenance.
- Market Cartographer: point-in-time OHLCV structure and deterministic chart facts.
- Kamandal: research review now; any later options construction only after an explicit
  promotion design and safety gate.

Run the complete three-repository fixture contract without network or trading effects:

```bash
uv run python scripts/replay_greg_chart_seed_fixture.py
```

The replay creates a temporary sanitized Birdclaw SQLite fixture under ignored
`data/research`, invokes all three real CLIs, verifies source identity and protected
effect flags, and writes `replay-receipt.json`. All resulting prices are synthetic and
remain prominently labeled `DEMO DATA`.

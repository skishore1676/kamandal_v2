# CSA-1 Shadow Operations

Status: implementation runbook; protected runtime steps require exact approval

## Operator Surface

Google Sheet `playbooks` remains canonical. CSA adds only columns BA-BC:

| Column | Field | Meaning |
|---|---|---|
| BA | `csa_stage` | Blank/baseline, shadow, pilot_live, or live. CSA-1 runtime accepts only shadow. |
| BB | `source_mode` | idea, market_scan, or portfolio_hedge. |
| BC | `management_policy_json` | Lifecycle fields missing from normal playbook columns. |

Existing columns continue to own DTE, delta, IV rank, underlying price bounds,
liquidity, sizing, BPR cap, profit/loss exits, and the four score weights. JSON
must not duplicate `score_weights`; duplication fails closed.

## Required Lifecycle JSON

Every CSA row requires:

```json
{"lifecycle":{"fill":{"max_attempts":"<Sheet number>","price_increment":"<Sheet number>"}}}
```

The placeholders above describe operator values; they are not repository
defaults. Lane-specific required keys are:

- Short strangle: `tested_side_confirmation`, `roll.min_credit`,
  `roll.duration_trigger_dte`, `adjustment_limit`, `inversion.allowed`,
  `inversion.max_width`, `cooldown.minutes`, `loss_stages.watch_multiple`, and
  `loss_stages.close_multiple`.
- Portfolio call vertical: `close_only`, `portfolio_delta_trigger`, and
  `hedge_underlyings`.
- Directional diagonal: `short_leg.roll`, `short_leg.roll_dte`, and
  `long_only.requires_approval`.
- Earnings calendar: `event_expiration.near_before_days`,
  `event_expiration.far_after_days`, and `close_only`.

`kamandal csa-validate-policy` reports every missing or incompatible field and
does not admit the affected row.

## Commands

```bash
kamandal csa-validate-policy
kamandal csa-migrate-db --db data/kamandal_v2.db
kamandal csa-shadow-scan --db data/kamandal_v2.db --provider public --ideas data/ideas/active
kamandal csa-shadow-management --db data/kamandal_v2.db --provider public
kamandal csa-shadow-scorecard --db data/kamandal_v2.db --output-dir data/reports/csa1
```

The migration command is a non-mutating dry run unless `--apply` is supplied.
Applying it creates a SQLite backup and checksum, preserves every baseline table,
records one migration identity, and requires `PRAGMA integrity_check=ok`.

## Isolation And Failure Behavior

- Scan and management write only `csa_*` tables. Baseline tables and Google
  Sheets are read-only to CSA runtime commands.
- The shadow adapter has no broker client and exposes no submit, replace, or
  cancel method.
- Missing Sheet policy, market data, event evidence, quotes, BPR, ownership, or
  reconciliation blocks new risk and remains visible in decisions/reports.
- Public preflight is authoritative for short-strangle BPR. Local BPR may appear
  only as labeled shadow fallback evidence.
- Duplicate underlying/playbook lifecycles and overlap with known live contracts
  are blocked before a ticket is created.
- CSA launchd definitions are rendered disabled by default. Enabling them is a
  protected deployment action.
- `install-csa-shadow`, `enable-csa-shadow`, and `disable-csa-shadow` operate on
  only the three CSA labels; they do not reload baseline Kamandal jobs.

## Daily Truth

`kamandal daily-report` remains the canonical daily aggregator and now embeds a
`csa_shadow` block. The CSA scorecard also writes JSON, Markdown, and CSV under
`data/reports/csa1/`. A green operational scorecard requires zero CSA live intents
and no unexplained run errors; it does not prove strategy edge.

## Rollback

Disable the three CSA jobs, keep the additive tables for evidence, and leave
baseline jobs untouched. New entries stop immediately. Existing shadow
lifecycles remain available to the management and scorecard commands. Reverting
the checkout is independent of database rollback because the additive CSA tables
are ignored by baseline code.

## Protected Deployment Checklist

1. Record laptop/GitHub/oldmac commit identity and oldmac working-tree status.
2. Capture database integrity, schema, backup location/checksum, Sheet BA:BC
   target rows/values, and current launchd inventory.
3. Dry-run the migration against an SQLite snapshot.
4. Obtain approval for the exact Git publish, Sheet range, migration, checkout,
   plist files, and three job enables.
5. Apply at a session boundary; never force a baseline live job.
6. Read back commit, Sheet policy hashes, schema/migration row, job state, logs,
   scorecard, baseline health, zero CSA live intents, and zero broker effects.

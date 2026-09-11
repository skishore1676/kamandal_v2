# Manual strangle entry

Use the existing `my_ideas` tab in the Kamandal cockpit. Add one row:

| Field | Value |
| --- | --- |
| date | Today's date, YYYY-MM-DD |
| ticker | Your enabled-universe ticker |
| type_of_trade | Strangle |
| direction | Neutral |
| horizon_days | 45 |
| notes | Optional thesis |
| conviction | high, medium, or low |
| status | Leave blank |

The row requests a short strangle; the planner selects contracts using the live
strangle playbook. It does not interpret exact strikes or expirations from notes.
Adding a row stages it; ordinary scheduled scans do not execute manual strangle
rows. `skip`, `cancelled`, and `disabled` exclude a row from subsequent imports.

To import the current row, plan only that ticker, and request normal guarded entry,
run on oldmac from `/Users/sunny/Documents/kamandal_v2`:

```bash
./scripts/run_manual_strangle.sh MS
```

Replace `MS` with your row's ticker. This command can submit a real order. It
uses the normal planner and execution locks, refreshes the Sheet intake, excludes
scan/correspondent alternatives, publishes the selected live plan, and calls the
existing executor. A missing row, invalid row, or empty/failed plan stops before
execution. Repeating a command does not bypass existing lifecycle/overlap guards.
The command replaces the current live selection, while existing positions remain
under their normal lifecycle manager.

A planning-only inspection is available through `unified-plan --manual-strangle MS`
without `--write-sheet`; it can still persist local planning receipts, but does not
invoke execution. Manual entry requires `operator_idea` in the strangle playbook's
`accepted_inputs`. This input is distinct from generic correspondent `idea` input.

The planner reserves the effective live BPR cap (currently $2,500) against portfolio
capacity. Fresh broker BPR below that cap is allowed even when it differs from the
planning estimate. IV/event/quote eligibility, trading hours, live enablement,
concentration, bounded pricing, and broker acceptance still apply. No fill is
promised by successful planning or dry-run.

For today's initial rollout, preserve the prior daily policy snapshot and capture
the explicitly approved `operator_idea` addition at the deployment boundary. New
entries then bind to that snapshot; existing position management uses its frozen
lifecycle policy. Later days capture the canonical Sheet policy normally.

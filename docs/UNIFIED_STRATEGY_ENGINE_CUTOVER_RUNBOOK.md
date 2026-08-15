# Unified strategy-engine cutover runbook

This is the protected **Phase 9** runbook. It is deliberately documentation,
not an executable deployment script. Nothing in this file authorizes an oldmac,
Google Sheet, database, launchd, broker, auth, or external-send effect.

## Preconditions

1. Approve one exact source commit and capture `git rev-parse HEAD` from this
   repository. The oldmac checkout is `/Users/sunny/Documents/kamandal_v2`.
2. Stop if any open group, order lineage, active leg, cost basis, or working
   order cannot be represented by the dry-run adoption manifest. A blocker is
   a no-go, not a migration exception.
3. At a session boundary, wait for in-flight Kamandal jobs and broker orders to
   reach a reconciled terminal or explicitly blocked state. Do not cut over
   while an entry, close, or adjustment is working.
4. Capture immutable pre-cutover receipts: runtime commit/status, launchd plist
   copies and labels, Sheet header/validation/value snapshot, database backup
   checksum, `PRAGMA integrity_check`, open-group/order inventory, and the
   rendered target plist hashes.

## Exact Sheet change set

The Phase 9 runner reads the current `playbooks` header and rows, calls
`build_sheet_mapping_manifest`, and applies only the manifest after a second
exact readback. It must preserve existing column order, formulas, formatting,
and validation; it must never clear or replace the tab.

- Append these columns only when absent: `mode`,
  `management_delta_target`, `management_delta_max`,
  `tested_side_confirmations`, `rearm_inside_confirmations`,
  `filled_side_adjustment_limit`, `dte_action`, `dte_action_threshold`,
  `duration_roll_limit`, `inversion_enabled`, `event_timing`,
  `event_near_expiry_after_days`, `paired_order_required`, and
  `post_event_exit`.
- Map `baseline`, `pilot_live`, and `live` to `mode=live`; map `shadow` to
  `mode=shadow`. `mode` is then the authoritative compiler input.
- For `short_strangle_high_iv`, retain `mode=shadow` and its existing 40%
  profit target, $0.10 credit floor, 30-minute cooldown, and two-adjustment
  limit. Set target/max management delta to `0.30`/`0.40`, same-side and
  re-arm confirmations to `2`, `dte_action=close`,
  `dte_action_threshold=21`, `duration_roll_limit=0`, and
  `inversion_enabled=FALSE`.
- Keep `call_calendar_low_iv` and `put_calendar_low_iv` as generic fixed-DTE
  calendar rows. Remove only their unused `event_expiration` JSON member.
- Append one disabled, separately reviewed `earnings_calendar` row. It must
  accept bullish and bearish direction as the call/put selector, use a 45–60
  DTE far leg and 5–7 DTE near leg after the confirmed event, require a paired
  package, enter in the final eligible pre-event session, and close in the
  first eligible post-event session. The exact approved row values are a
  required Phase 9 input; an absent row is intentionally a manifest blocker.

## Database and lifecycle procedure

1. Copy the runtime SQLite file to a timestamped backup in the same volume;
   record SHA-256 and `PRAGMA integrity_check=ok` for both source and backup.
2. Run the read-only cutover inventory against the copied snapshot. Record each
   `create`, `retain`, or `block` decision. No `block` decision may proceed.
3. Reconcile open positions and working order lineage one final time, then
   apply lifecycle adoption once. Re-run the manifest: it must be idempotent
   and report the same lifecycle IDs. Re-run integrity check.
4. Verify each adopted lifecycle has exact active legs, source/playbook
   identity, cashflow/cost-basis evidence, policy hash or `policy_at_adoption`,
   and one owner. Any mismatch triggers rollback before scheduler replacement.

The source helper `apply_cutover_fixture` is fixture-only. A production runner
must be separately reviewed and cannot reuse a test apply switch as authority.

## Scheduler replacement

Replace ownership atomically at the session boundary:

- Retire: `universe-proposer`, `live-advisory`, `live-management`,
  `csa-policy-snapshot`, `csa-shadow-scan`, `csa-live-scan`,
  `csa-shadow-management`, `csa-live-management`, and
  `csa-shadow-scorecard`.
- Add: `unified-planning` and `unified-lifecycle-management`.
- Retain source collection, event/IV refresh, guarded executor,
  reconciliation, health, reporting, and weekly review jobs.

First render the exact plist set to an explicit empty temporary directory and
compare it with the captured source set. Only after the database and Sheet
readbacks succeed may the authorized operator unload the retired labels and
load the rendered target labels. The target must show one planning and one
lifecycle-management owner; never leave old and new managers loaded together.

## Immediate readback and rollback

Read back runtime commit, database integrity, lifecycle inventory, Sheet ranges
and validation, loaded labels, job receipts, and guarded-ledger state. Roll back
immediately on any duplicate owner, unmapped position/order, stale or failed
manager, unexpected Sheet cell, integrity failure, unguarded ticket, broker
effect outside the established ledger, or time-window violation.

Rollback restores, in one session-boundary operation, the captured source
commit, SQLite backup (after checksum verification), Sheet header/value/
validation snapshot, and prior launchd plist set. Then verify that exactly the
pre-cutover owners are loaded and reconcile the broker before allowing another
scheduled action.

## Observation boundary

For five natural trading days, retain all planning, management, reconciliation,
ticket, fill, lifecycle-history, and health receipts. Report natural market
branches separately from replay-only proof; five days of clean operation is an
operational result, not an alpha claim.

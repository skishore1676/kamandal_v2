# Two entry pathways: release validation

The October 1 release implements [the approved contract](../TWO_ENTRY_PATHWAYS.md):
ideas optimize within their own exposures; Guru openings preserve source contracts
and use chronological capital admission. Both enter the existing execution and
frozen-policy management path.

## Verified before deployment

- Full offline regression suite: 1,127 tests passed.
- Acceptance tests vary Guru Greeks and ideas preferences independently, check
  measured sleeve/account rejection receipts, preserve source/preflight gates,
  and reject non-positive or non-finite candidate risk.
- Complete text can become exact evidence through one interpreter and deterministic
  source binding. Changed text and changed ticket strikes fail integrity checks.
  Confirmed openings and opening adds cannot fall back into adapted ideas.
- Supported package tests cover candidate handoff, simulated fill adoption,
  frozen-policy hold and complete close intents, including exact call/put verticals.
- The proposed Sheet migration passes ordinary, unified and CSA policy compilation,
  with all existing policy hashes, source switches and 40/40/80 ceilings preserved.
  It adds two exact vertical rows, source capabilities and a corrected sleeve label.
- A broker-inert replay of retained candidates from `run_20261001T142505Z`, using
  the latest read-only account state, admits the previously delta-vetoed AMZN and
  GOOGL copies. The ideas sleeve remains capacity blocked. This is a selection
  replay, not current quote/preflight, submission, fill or live-management proof.

## Deployment acceptance

Deploy only the tested commit at an idle job boundary, apply the scoped Sheet
migration, then read back head, complete Sheet gate, source modes, sleeve caps,
job availability and pending-entry state. Preserve runtime files and job schedules.
Do not run entry, management or intake jobs manually to manufacture proof.

Natural scheduled intake/planning/execution and later management receipts remain
required. The existing monitor follows those outcomes in this chat. No source
deadline is renewed to make a historical opening eligible.

## First natural cycle and follow-up repair

The 11:55 CT planning run (`run_20261001T165512Z`) admitted the AMZN and GOOGL
Guru calendars and selected NTAP from the ideas sleeve. The calendars reached
entry intents at 12:01:47; the 12:05 executor rejected them because their original
source deadline was 12:03:57. They were not submitted or filled.

That run exposed a pre-existing capital-scope mismatch: planning aggregated both
live venues, but submission used only the destination venue's equity for the
40% sleeve check. The follow-up records venue scope on new tickets and refreshes
all participating accounts before submission, retaining independent destination
buying-power and 80% checks. Legacy staged tickets do not gain a new account scope.
Final capital receipts retain measured exposure, budget, scope and limits.

The Guru brief remained at its pre-execution state. Existing execution and
management jobs now refresh it after source-linked outcomes change, with no
Sheet calls for unchanged outcomes and a later-cycle retry after publication
failure. Source expiry and closed positions are explicit outcomes.

Follow-up validation: 152 focused tests and the full 1,135-test offline suite
passed. Coverage includes combined-account eligibility, unchanged legacy scope,
shared/venue limits, unavailable accounts, ticket scope through fill/exit handoff,
report retry/deduplication and closed-position reporting. Natural fill and
management acceptance remains outstanding. A new SPX put butterfly was interpreted
and source-bound by the natural intake after the planner had loaded its inputs;
it awaits the next scheduled plan rather than a forced replay.

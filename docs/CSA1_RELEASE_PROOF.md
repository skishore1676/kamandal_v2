# CSA-1 Shadow Release Proof

Date: 2026-08-08
Baseline: `68bdc89afd11d5d7de651d85595baa7c5176b4e1`
Release state: code-complete; protected runtime deployment not yet applied

## Verdict

CSA-1 is ready to publish and stage as a broker-inert shadow overlay. It does not
create a second live authority, submit broker orders, or write baseline planning
tables. Google Sheet policy is mandatory for every active CSA row; missing,
stale, incompatible, or invalid policy fails closed.

## Verification

| Proof | Result |
|---|---|
| Full repository suite | 487 passed |
| Python compilation | passed |
| Shell syntax | passed for installer and all CSA runners |
| Git whitespace check | passed |
| Baseline golden | exact SHA-256 match |
| Migration dry run | source DB SHA unchanged |
| Migration apply | ten additive `csa_*` tables; integrity `ok` |
| Migration repeat | no additional tables |
| Runtime isolation | baseline table counts unchanged in end-to-end tests |
| Broker effect | shadow adapter accepts quote maps only; zero live intents in scorecard |

The normalized baseline and current planner payloads were each 57,701 bytes and
hashed to:

`a563b90f52b5f1760dac2a95640c8084cdbc03a8a5c84c77e500fff724bac259`

Normalization replaced only the time-derived `plan_run_id`. Candidate, plan,
portfolio, diagnostic, rejection, and metric payloads remained part of the hash.

## Adversarial Findings Closed

1. Sheet-enabled strangle expansion no longer remains trapped behind a legacy
   per-underlying allowlist.
2. Portfolio-hedge opportunities use the open live ledger's aggregate delta,
   not an empty market-provider account shell.
3. Management DTE and event calculations use the run timestamp, not wall-clock
   `date.today()`.
4. A strangle's untested side can only roll inward in the same expiration;
   crossing the tested strike requires Sheet-permitted bounded inversion.
5. Strangle cooldown uses the last filled adjustment timestamp and Sheet minutes.
6. Duration rolls require the Sheet minimum credit.
7. Earnings calendars require a known event captured before the near expiration
   and within both Sheet-provided event/expiration bounds.
8. CSA reads earnings evidence through a read-only SQLite connection and does
   not run baseline schema DDL.
9. Invalid lifecycle booleans and non-finite numbers fail during policy
   compilation rather than later in a runtime cycle.
10. Every management cycle re-checks read-only live contract and active-order
    evidence; overlap or working intent selects `block` and creates no new ticket.
11. CSA launchd install/enable/disable actions target only the three CSA labels;
    baseline jobs are not reloaded during rollout or rollback.
12. Blank diagonal `spread_width` is not replaced by a repository constant or
    by the current option strike grid. CSA selects the near short and far long
    independently from their Sheet-owned DTE/delta windows and records the
    resulting actual width as evidence only.

## Strategy Sources Used For Operator Defaults

The proposed Sheet values follow tastylive's public mechanics: manage short
premium winners around 50% or 21 DTE; defend strangles by rolling the untested
side, rolling duration, or bounded inversion; keep short vertical management
simple and close-oriented; manage calendars as defined-risk debit positions; and
manage diagonals through the nearer short option. These sources guide the initial
shadow policy only; the Google Sheet remains canonical:

- https://www.tastylive.com/news-insights/how-to-use-options-strategies-amp-key-mechanics-takeaways
- https://www.tastylive.com/news-insights/managing-short-vertical-spreads
- https://www.tastylive.com/concepts-strategies/calendar-spread
- https://www.tastylive.com/concepts-strategies/diagonal-spread

## Remaining Protected Effects

The live Sheet currently ends at AZ. The following still require one exact
operator gate: add BA:BC, populate selected shadow rows, publish the release
commit, migrate the oldmac database with backup, update the oldmac checkout,
render/install the three disabled-by-default CSA jobs, enable only those three
shadow jobs, and read back policy hashes, schema, scheduler state, reports,
baseline health, and zero broker effect.

`pilot_live`, `live`, and every CSA broker submit/cancel/replace path remain
outside this release.

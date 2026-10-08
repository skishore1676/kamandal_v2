# Trade Source Routing

Date: 2026-09-04
Status: source routing deployed; October 1 two-pathway entry contract supersedes earlier mixed selection

See [Two entry pathways](TWO_ENTRY_PATHWAYS.md) for the current approved contract.
Confirmed Guru openings belong exclusively to `guru_exact`; ordinary directional
ideas belong to `current_idea`. Source modes govern entry permission, not fallback
between these pathways. Complete text can supply exact contracts without an
image. Historical shadow-only and independent-image requirements in this
document do not describe the October 1 target.

## Implementation status

The source-neutral compiler, per-output activation, failure isolation,
source-ceiling planner routing, exact-package adapter, activity projection, and
daily policy snapshot integration are implemented. The protected migration is
`scripts/apply_trade_source_routing_sheet.py`; it is dry-run by default and
replaces the retired four-row Mike migration.

The implementation was deployed to oldmac at commit `72a05d2` on 2026-09-03
after the market session. The bounded Sheet migration passed its complete
readback gate at snapshot hash
`ca834b95640d2253d9faa26cbc8f70c496b4031016cda1f482d1885e984a3fb6`:
19 named playbooks, 15 enabled unified policies, four source-policy rows, no
retired Mike playbooks, and exactly one exact-package acceptor for each supported
calendar/diagonal structure. All scheduled jobs remained loaded and idle; no
planner, broker, entry, exit, or lifecycle cycle was triggered manually.

Source-ready, deployed, and Sheet-activated are now proven. The next natural
Birdclaw activation, daily policy snapshot, unified planning run, and activity
projection remain the required proof of scheduled consumption and behavior.

## Decision

Trusted people such as Greg Harmon and Mike Butler are **trade sources**, not
strategy lanes. Birdclaw captures their sanitized public evidence. Kamandal
turns each post into zero or more normalized outputs, applies the Google Sheet's
source policy, and sends supported outputs into ideas optimization or exact
capital admission, then the shared execution path and lifecycle manager.

The two executable output kinds are deliberately plain:

- `idea`: a thesis or opportunity for which Kamandal chooses a compatible
  Sheet playbook and constructs the trade; and
- `exact_package`: observable option legs that Kamandal validates and quotes
  exactly rather than reconstructing.

Anything incomplete, ambiguous, unsupported, or non-actionable is retained as
`residual` evidence with a precise reason. `Residual` is a status, not a third
planner or execution lane.

One post may produce multiple outputs and may mix output kinds. The parent post
therefore owns an `outputs[]` collection; the system must not force the entire
post into one exclusive classification.

The source-episode compiler in
[Source Episode Compiler](SOURCE_EPISODE_COMPILER.md) is deployed and satisfies
the atomic mixed-post contract. It gives each
source an independent semantic profile while reusing one bounded orchestration
and every existing downstream control.

Confirmed openings and complete opening adds belong exclusively to the exact
pathway; an adapted idea projection cannot enter even if the exact route is Off
or blocked. Commentary can produce ideas. Stable opportunity IDs prevent
duplicate admission and follow each source event through execution.

## Stable ownership

```text
Birdclaw sanitized post and media
                 |
                 v
Kamandal-owned normalization contract
  (Agent Broker supplies bounded model labor when needed)
                 |
                 v
parent post receipt + outputs[]
       idea | exact_package | residual
                 |
                 v
append-only evidence and decision history
                 |
                 v
Sheet trade-source policy ceiling
                 |
       +---------+----------+
       |                    |
       v                    v
ordinary Idea adapter   exact-leg candidate adapter
       |                    |
       +---------+----------+
                 v
ideas optimization | Guru FIFO capital admission
                 |
                 v
safer(source mode, playbook mode) + existing safety gates
                 |
                 v
one shadow/live adapter and one frozen-policy lifecycle manager
```

- **Birdclaw** owns public-X acquisition, identity, sanitization, public media,
  capture completeness, and source freshness. It never decides what to trade.
- **Agent Broker** is a model-execution dependency. It does not own prompts,
  schemas, trading meaning, portfolio fit, or authorization.
- **Kamandal** owns the normalization questions and schemas, deterministic
  validation, evidence identity, capability matching, portfolio selection,
  execution envelopes, lifecycle management, and canonical outcomes.
- **Google Sheet** is the sole operator control surface for whether each source
  output is off, observed, shadow-eligible, or allowed to approach the normal
  live gates.
- **TradeLab** may analyze canonical evidence and economics but cannot select,
  manage, or execute a trade.

No correspondent gets a dedicated scheduler, planner, manager, database, or
broker path. The existing shared Birdclaw refresh and Kamandal activation jobs
remain the only schedule.

Source-specific deterministic rules, prompts, examples, and bounded history
recipes are profiles inside Kamandal. They are not source-specific trading
pipelines. Agent Broker executes the selected model turn; it does not define
those semantics.

## Google Sheet contract

### `trade_sources` — operator-owned

Exactly one row is required for each `(source_id, output_kind)` pair. With the
current two output kinds, that means exactly two rows per configured source.

```text
source_id
output_kind       # idea | exact_package
mode              # off | observe | shadow | live
notes
```

There is no separate `enabled` column; `mode=off` is the explicit disabled
state.

| Mode | Meaning |
| --- | --- |
| `off` | Birdclaw may retain public evidence, but Kamandal performs no inference or planner admission for this output kind. |
| `observe` | Normalize and retain the output and project activity; never enter the planner. |
| `shadow` | Planner admission is permitted, but the effective result cannot exceed broker-inert shadow. |
| `live` | The source does not impose a shadow ceiling. The matched playbook and all existing live safety, health, session, broker, approval, and reconciliation gates still bind. |

The source row is an authorization **ceiling**, never a live authorization by
itself. Effective mode is the safer of source mode and matched playbook mode.
During the first implementation, `exact_package=live` remains invalid because
the currently deployed exact-package contract is shadow-only. Live exact-package
support requires a separately approved money-path release.

Initial migration values preserve or reduce current authority:

| Source | Output kind | Initial mode | Reason |
| --- | --- | --- | --- |
| `greg_harmon` | `idea` | `live` | Preserve current access to ordinary live and shadow playbooks; the playbook still decides the final book. |
| `greg_harmon` | `exact_package` | `observe` | Learn from any reconstructable package without authorizing execution. |
| `mike_butler` | `idea` | `observe` | Make interpretation visible without opening a new live idea source. |
| `mike_butler` | `exact_package` | `shadow` | Continue the currently authorized broker-inert experiment. |

Repository YAML may identify profiles, schema versions, and code-owned profile
paths. It may not retain competing effect switches such as profile `enabled`,
profile `source_mode`, or a global activation mode once the Sheet contract is
live. Missing, duplicate, or invalid Sheet rows fail source activation closed.

### Existing Kamandal playbooks own management

The four current `mike_*_observed` rows are removed without creating four
replacement templates. They are accidental combinations of a person, a source
route, a structure, and a management policy. Preserving that duplication under
generic names would simplify the label without simplifying the machine.

Existing playbooks gain one small source-neutral field:

```text
accepted_inputs     # comma-separated: idea, market_scan, portfolio_hedge, exact_package
```

`accepted_inputs` replaces `source_mode` as the prospective input contract.
During migration, each existing row starts with its current `source_mode` value,
so `market_scan` and `portfolio_hedge` behavior is not accidentally converted to
`idea`. Historical snapshots with a blank legacy `source_mode` resolve to
`idea`; every enabled row in the new Sheet must then contain an explicit value.
Each supported exact structure has exactly one compatible accepting playbook.
Zero matches park as unsupported and multiple matches park as
`ambiguous_playbook_match`; ranking never selects management semantics. Dedicated
`guru_exact_*` rows supply bounded local sizing and management for source copies.
Ordinary ideas continue to use construction playbooks.

Kamandal assigns canonical leg roles, normalizes source quantity to local size
without altering ratios, resolves current contracts and checks executable quotes.
Complete source text or a cached image can provide the contract evidence. One
interpreter plus deterministic source/contract binding is the scheduled path;
independent image re-transcription remains an optional audit.

Exact openings are admitted in source-time order subject to source mode, current
source revision, freshness, duplicate/overlap protection, supported management,
per-order cash limits, available broker capital and 40/40/80 sleeve/account caps.
They do not pass ordinary construction DTE/delta/IV/event/yield preferences or
ideas portfolio-fit selection. Actual expiration and configured exit windows
remain hard manageability checks. See [Two entry pathways](TWO_ENTRY_PATHWAYS.md)
and [Exact Guru package execution](operations/guru-live-packages.md).

### `trade_source_activity` — machine-owned observation surface

The same workbook contains one bounded, generated activity tab. It is a
projection of canonical receipts, not policy and not a second database.

```text
observed_at
source_id
post_ref
output_id
acquisition_status
classification
normalized_output
action
symbol
structure
link_status
evidence_status
interpretation_confidence
capability_support
planner_disposition
effective_mode
reason
```

Each normalized output gets one row. The row must let the operator answer:

1. Did Birdclaw capture the post?
2. What did Kamandal think it meant?
3. Was it an idea, exact package, or residual?
4. If exact, what legs were observed?
5. Did Kamandal support the structure?
6. Did the planner select it?
7. Did it enter shadow/live, or why not?

Projection is best effort. A Google Sheets outage cannot block exits, existing
lifecycle management, reconciliation, or unrelated planning. A failed activity
projection stays visible in health and retries from canonical receipts rather
than asking the model to interpret the post again.

## Evidence and measurement

Every exact package is persisted before source policy or planner selection. The
passive source benchmark is derived from this existing evidence, its first
actionable quote, subsequent observations, and linked source follow-ups. It is
not a second fill engine and creates no plan, ticket, position, or lifecycle.

This distinction is mandatory:

- the planner decides whether Kamandal allocates shadow or live capacity; but
- the planner does not decide whether the source's reconstructable opportunity
  is measured.

Otherwise planner rejection would hide source performance and the experiment
would measure only Kamandal's selection filter.

Source close, roll, and adjustment posts remain benchmark evidence during the
first implementation. Kamandal manages any selected shadow lifecycle using the
policy frozen at its own entry. Source-directed management can become a future
capability only after deterministic linkage, supported actions, and separate
operator approval.

## Failure isolation

One source failure may clear or park only that source's newly generated outputs.
It must not erase another source's active ideas or block live exits and existing
lifecycle management. The shared job still reports degraded acquisition or
translation with the affected source and stage.

An unchanged post keeps stable parent and output identities. Retry reuses the
canonical evidence and extraction cache. It cannot create a second opportunity
or silently change a prior interpretation.

## Migration

This is one bounded, prospective migration:

1. Add and compile `trade_sources`; include it in `validate-sheet-policy`.
2. Make one post capable of emitting multiple typed outputs.
3. Replace profile-wide `source_mode` branching with per-output Sheet routing.
4. Isolate activation failure and active-output replacement per source.
5. Add `accepted_inputs` to existing playbooks, initially authorizing exact
   packages on one call/put calendar/diagonal row per concrete structure.
6. Remove the four Mike rows without copying their special management values.
7. Add the activity projection from canonical receipts.
8. Preflight the complete proposed Sheet snapshot. Every non-Mike policy except
   the four rows receiving `accepted_inputs=idea,exact_package` must keep its
   prior policy hash.
9. Immediately before the Sheet write, verify no working Mike exact-package
   entry would be retired. Existing open lifecycles, if any appear, retain their
   frozen policy regardless of row removal.
10. Apply the code and Sheet migration at one session boundary. Preserve the
   current day's immutable policy snapshot; the next natural snapshot consumes
   the new policy.
11. Let natural Birdclaw, activation, planning, shadow, management, and activity
    projection runs prove the complete path.

## Acceptance gates

Implementation is incomplete until tests and deployed readback prove:

- one post may emit zero, one, or multiple mixed outputs;
- mixed idea/exact projections from one opportunity are mutually exclusive in
  portfolio selection;
- close, roll, adjustment, hold, and expiry language cannot become a new entry;
- incomplete required media or history parks rather than being inferred;
- every output has stable replay identity and becomes executable input or an
  explicit residual;
- exact evidence is durable before admission and unsupported packages park;
- source mode can only reduce, never raise, playbook/effect authority;
- `exact_package=live` remains blocked;
- existing global risk, quote, BPR, session, broker, approval, and
  reconciliation controls remain unchanged;
- the four Mike rows are absent and no replacement source-specific or
  exact-only management rows were created;
- exactly one existing playbook accepts exact packages for each initially
  supported structure;
- exact calendar and diagonal legs receive the canonical `short_near` and
  `long_far` roles without changing any observed contract term;
- zero compatible exact-package managers park as `unsupported`, while multiple
  managers park as `ambiguous_playbook_match`;
- unrelated playbook policy hashes are unchanged;
- one broken source does not clear or block another source;
- activity counts reconcile post -> outputs -> planner dispositions -> effects;
- activity projection failure does not block money-path management;
- no new scheduler, planner, manager, fill engine, database, or executor exists;
  and
- the next natural runs, not a replay alone, populate the activity tab and any
  selected Mike exact package remains broker-inert shadow.

## Non-goals

- Blindly following every public post.
- Treating an LLM response as an order authorization.
- Making every recognized option structure executable.
- Copying source-directed adjustments into Kamandal management in this release.
- Promoting exact packages live.
- Creating per-person playbooks, jobs, managers, or performance engines.
- Letting the activity tab become a control or historical source of truth.

The sentence to remember is: **two Sheet controls per source, reusable strategy
capabilities, one Kamandal money path, and every output visible.**

## Activated Tuesday contract (September 5 approval)

The initial modes above are historical rollout defaults. At the October 1 pre-deploy readback, both sources had ideas and exact modes **live**; the Sheet remains the live authority. See [Tuesday operating contract](reviews/guru-tuesday-readiness-2026-09-08.md) for daily tracking, source ownership, and verification. The Sheet remains authoritative; seeded defaults do not override it.

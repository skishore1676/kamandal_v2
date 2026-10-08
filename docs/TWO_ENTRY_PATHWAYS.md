# Two entry pathways, shared execution and management

Approved by Suman on October 1, 2026. This is the current operating contract.
It supersedes earlier descriptions that route exact Guru packages through the
ideas optimizer or require an independent image transcription for every copy.
Implementation, activation and natural execution proof are separate milestones.

## The three modules

1. **Intake** retains source identity, original publication time, text/media and
   interpreted intent. Sources include Guru posts, X, YouTube and operator ideas.
2. **Entry decisions** have two distinct responsibilities:
   - `current_idea`: construct alternatives and optimize an ideas portfolio;
   - `guru_exact`: copy a confirmed source opening, preserving contracts and
     relative quantities, subject to local sizing and execution feasibility.
3. **Management** adopts filled trades from either pathway into the same ledger,
   reconciliation and lifecycle engine. The operator Sheet supplies management
   rules, frozen on each position at entry. Source exits, rolls and adjustments
   do not replace Kamandal management.

Both pathways use the existing broker submitter. No second executor, scheduler,
position database or management engine is introduced.

## Capital and selection

The Sheet's `portfolio_sleeves` rows remain the sole live allocation owner:
`current_idea=40`, `guru_exact=40`, `portfolio_total=80`, expressed as maximum BPR
percentages of account equity. These are ceilings, not utilization targets.
Unknown broker exposure and pending/uncertain commitments consume capacity;
neither pathway can borrow capacity by dropping those commitments.

Planning records the participating live broker venues on each new entry ticket.
Submission refreshes all those accounts for the same sleeve/account denominator,
then checks the destination broker's own buying power and 80% ceiling separately.
Balances are never frozen into permission to spend. Tickets staged before this
scope field existed retain their original single-account check; a fresh scheduled
plan is required to use the corrected scope.

The Sheet's per-package cash cap and sleeve limits are the allocation authority.
The quote midpoint is a cost estimate, not another operator-imposed ceiling.
Before selection, both broker adapters check every permitted entry-pricing step
and reserve the largest buying-power requirement, including broker fees and
the structural risk of accepting less credit. That reservation must fit the
Sheet caps and remains frozen on the ticket. Repricing rechecks current risk
inside that reservation; it does not increase an existing ticket's authority.
Public previously checked only the first price, which could leave a valid
midpoint retry unfunded. The October 1 correction applies to newly planned
tickets and preserves existing orders and positions.

The ideas optimizer uses only ideas-sleeve positions for delta fit, portfolio
preferences, concentration and candidate ranking. Guru positions remain in the
real account capital and broker constraints. They do not become an input to the
ideas sleeve's preferred market exposure.

Guru openings do not compete on income score, marginal score, preferred delta,
IV, ordinary strategy DTE windows, credit yield or debit-to-width preference.
Changing those preferences must not alter Guru admission. Source contracts are
never substituted to satisfy a preference. Guru admission uses deterministic
oldest-publication-first order, with stable source/opportunity identity as the
tie breaker. An unaffordable package receives its actual blocker; it does not
prevent later independently affordable packages from being considered.

Both entry decisions hand off the same candidate/ticket contract, including
source opportunity, sleeve, exact legs, local size, entry budget, evidence
revision, current source deadline and frozen management policy. Existing
submission throughput limits remain operational bounds, not alpha selection.

## Entry checks by purpose

| Check | Ideas | Guru copies |
| --- | --- | --- |
| Strategy matching, preferred IV/DTE/delta, income score and portfolio fit | Apply | Do not apply |
| Sleeve budget, account/venue buying power, total 80% cap and pending commitments | Apply | Apply |
| Per-package capital/quantity limits | Apply | Apply; preserve leg ratios |
| Valid supported contracts, correct opening sides and manageable lifecycle | Apply | Apply |
| Actual expiration and session/expiry-day execution deadlines | Apply | Apply |
| Fresh actionable quotes, bounded entry prices, broker preflight | Apply | Apply |
| Source freshness, current revision, duplicate/overlap ownership | Apply where sourced | Apply |
| Halt, account-health and reconciliation protections | Apply | Apply |
| Market-selection concentration, preferred delta and ranking | Ideas exposure only | Do not apply |

Quote validity and bounded price execution remain meaningful even when source
selection is trusted. An expired contract, unknown contract, unmanageable
structure or unavailable capital is not cured by removing a strategy filter.
Global emergency/account-health protections are not an alternative Guru alpha
model. Every rejection identifies which kind of control produced it.

## Interpretation and ownership

Use one interpretation pass with deterministic contract validation. Either
literal text or a hash-verified source image may supply contracts. An image is
not required for complete text. Model confidence is recorded evidence, not a
calibrated probability of correctness. Measure whole-package accuracy against
reviewed examples; do not demand perfect historical accuracy before operation.

Missing or contradictory sides, strikes, expirations or ratios stay explicit
exceptions. Source-declared, documented shorthand may resolve terms; missing
terms cannot be silently invented. A confirmed add with complete opening legs
is a new opening opportunity; a roll, exit, template, proposed alternative or
confirmation of an already identified opening cannot create duplicate entry.
Greg's declared monthly convention also resolves a standalone confirmed call
calendar such as `added some $MU Oct/Nov 1150 call calendars`: matching text
legs retain their 1:1 ratio and resolve to the owner calendar's monthly dates.
Conflicting dates, strikes, sides, ratios and non-opening actions stay blocked.
The original source publication time and freshness deadline are preserved.

Confirmed Guru opening opportunities are owned by `guru_exact`, even if the
exact route is Off, waiting for evidence, unsupported or out of capacity. They
must not fall through to an adapted ideas trade. General directional commentary
without a source opening continues through `current_idea` when its source route
is enabled. Both routes retain the same stable source opportunity for history.

Record evidence basis honestly: a structurally valid single-pass interpretation
is not labelled independent corroboration. Hash/revision checks bind the exact
source and interpreted package at planning and again at submission. A revised
or removed source package invalidates a staged entry. Historical evidence is
never given a fresh publication timestamp to make it executable.

## Operator result

Every fresh opening must join to one outcome: needs evidence, unsupported,
blocked by a named capital/execution rule, queued, submitted, missed/unfilled,
filled and managed, or closed. A decision receipt contains the rule, measured
value and limit where meaningful, source, sleeve and planning run. A successful
job and the phrase `portfolio_optimizer` are not sufficient outcome records.
Use the existing ledger and `trade_source_activity` operator surface.
Existing execution and management jobs refresh that brief when a source-linked
entry, fill or close changes state. Unchanged outcomes do not republish the Sheet;
publication failures are non-blocking and retry on the next natural cycle.

## Acceptance and rollout

- Changing Guru exposure does not change ideas ranking/delta eligibility, except
  through genuine shared capital or execution constraints.
- Changing ideas delta/IV/DTE/score preferences does not change Guru admission.
- Text and image openings retain source legs through projection, candidate,
  ticket, simulated fill adoption, frozen-policy management and full close.
- No adapted fallback opens a confirmed Guru opportunity, including when its
  exact route is Off or its contracts are incomplete.
- Over-budget, duplicate, stale, changed-source and unresolvable orders remain
  blocked with a specific receipt; route Off does not stop management.
- Live and shadow exercise the same entry decisions and management contracts;
  only execution effects differ, with existing synthetic-capital semantics
  explicitly distinguished from live account capacity.
- Deploy tested code and necessary Sheet capability rows at an idle session
  boundary; read back exact revisions, source modes, 40/40/80 controls, loaded
  jobs and no unintended intents. Do not manually run trading jobs for proof.
- Natural scheduled receipts must then demonstrate the approved pathways. A
  simulation, successful deployment or first fill alone is not complete
  end-to-end management proof.

Suman authorized documentation, implementation and deployment on October 1.
This authorization does not request manual orders, credential changes, forced
historical entries or changes to existing positions' frozen management rules.


## October 5 split-wing repair

Greg's `added some $QQQ Oct 23 Exp 760/775-780/795 split wing butterfly call
spreads` is one `split_call_fly`, with BTO 760 / STO 775 / STO 780 / BTO 795,
all October 23, 2026 calls at 1:1:1:1. It must not be reduced to a two-leg
vertical or split into independent tickets. The exact geometry validator,
source policy, current quote/broker BPR checks and generic full-package manager
cover this shape. Unequal wings include their additional terminal loss in the
local risk bound and debit ceiling. No ideas construction is enabled for it.

The narrow cached-text repair accepts only that explicit grammar with four
matching retained legs and a leg-count blocker. It preserves original source
publication/deadline, event identity and history; it does not replay a job or
extend source eligibility. Conflicting contracts remain blocked.

The operator migration `scripts/apply_split_call_fly_sheet.py` is dry-run by
default. It adds one exact-only row from the existing managed calendar template
and Greg's shape permission, preserving existing policy hashes, source switches,
capital limits and unrelated cells. It uses one package, $1200 maximum BPR and
the existing full-package close policy.

Timing is a separate acceptance gap: deployed Birdclaw collection is 13:50 then
15:05 Chicago; Kamandal intake is 14:00 then 15:15, and its last planner is
14:15. A 14:32 post cannot enter that afternoon. Proposed coordinated late cycle:
Birdclaw 14:35, Kamandal intake 14:45, planner 14:50, retaining the existing
closing collection. This requires a separate upstream collection/scheduler gate;
classification support alone does not prove timely acquisition or a natural fill.


## October 6 consumed-opening and Sheet publication repair

Filled source openings remain consumed after position closure and across trading
days. Planner admission and submission both consult the retained opening-intent
ledger, including terminal partial fills and all source opportunity aliases. An
unfilled retry can exclude only its own pending reservation; it cannot exclude
a completed fill. New source adds have distinct opportunity identities.

The cockpit omits duplicate candidate/preflight diagnostics only when a plan's
JSON cell exceeds the safe size budget. Executable tickets, hashes, control
flags and basket metadata remain intact. The cell identifies the omitted
fields and original plan/ticket ledger records. Oversized executable content
still fails closed rather than being truncated or partially published.

## October 7 no-concession campaign repair

A positive, valid concession allowance smaller than one tick now omits only
that concession attempt. Improved-price and midpoint attempts remain eligible;
the midpoint is rounded in the operator's favor. The frozen campaign records
`concession_omitted_reason`, and the executor stops after its last retained
price. Broker preflight still reserves the maximum risk, including fees, across
all retained attempts. Missing or invalid bounds, stale quotes, and exhausted
economic headroom still block entry. Existing frozen tickets are unchanged.

For the retained QQQ pricing geometry (midpoint 4.285, improvement 0.013,
allowance bound 0.0065), the debit campaign is 4.27 then 4.28. This repairs future
eligible candidates; it does not revive the expired October 5 source opening.

NDX remains unsupported: the current Public adapter selects INDEX instruments
only for SPX. NDX requires proved broker chain/contract resolution, quote and tick
handling, fee-inclusive preflight, and reconciliation before live admission.
Changing the source-package allowlist alone is insufficient.

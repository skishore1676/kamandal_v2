# Strangle readiness for Tuesday, September 8

The right design is one Kamandal portfolio and lifecycle engine with two explicit
broker adapters. Keep direct REST execution. The September 6 audit found concrete
routing, management-state, and broker-contract defects and repaired them in source.
A bounded one-contract Tuesday pilot is the next proof step; autonomous live
strangle management is not yet demonstrated by natural runtime evidence.

## What changes for Mike and Greg

An exact opening short strangle can enter the same strangle playbook as a market
scan. The source's two contracts, expiration, strikes, and equal quantities stay
intact. Kamandal rejects an ineligible package instead of changing its legs or
resizing it. Source freshness, enabled universe, IV/price/event restrictions,
DTE/delta/liquidity, portfolio controls, and native Tastytrade dry-run BPR apply.
The source event and signature are retained in the live lifecycle handoff.

The proposed Sheet change adds `live_structures` to `trade_sources`. Both
`greg_harmon/exact_package` and `mike_butler/exact_package` become `live` with scope
`short_strangle`. Their other exact structures remain shadow. The strangle
playbook accepts `market_scan,exact_package`; it stays shadow until the separately
scheduled Tuesday gate. This structure scope prevents a source-wide LIVE switch
from promoting calendars or diagonals accidentally. Existing idea routes remain.

The exact-source route previously stopped at shadow. Another independent defect
would reject Tastytrade preflight BPR because the final gate expected Public's
response shape. Both are repaired. Fresh submission BPR must now fit the approved
entry risk budget, and a source cannot expire between planning and submission.

## What happens when a leg is tested

The current policy uses two distinct actionable observations to confirm a breach.
Duplicate or stale quotes cannot manufacture confirmation. After an adjustment,
rearming requires two consecutive inside observations; a new breach resets that
inside count. The former code could incorrectly count interrupted observations.

Illustrative example: with a short 90 put and short 110 call, a downside test can
lead to buying back the untested 110 call and selling a closer call, such as 100,
in one two-leg order at the same broker. The replacement must satisfy the policy's
delta, credit, liquidity, cooldown, and non-inversion constraints. The example
explains which leg moves; it does not predict that the 100 call will qualify.

Current controls: replacement delta target 0.30, maximum 0.40; minimum roll credit
$0.10 per share; at most two adjustments; 30-minute cooldown; no inversion and no
expiration extension. Unsupported duration-roll/inversion settings now fail
compilation instead of suggesting capabilities the manager does not implement.
An inert legacy nested inversion flag is proposed for cleanup to match the active
FALSE setting.

Whole-package management includes 40% opening-credit profit capture, 21-DTE,
half-time and pre-event exits, and a buyback cost reaching 3x opening credit.
The last value is a buyback threshold, not a claim of 3x net P&L loss. Adjustments
and fills must still reconcile through the shared lifecycle. This audit verifies
local behavior and contracts; no natural tested-side adjustment exists in the
runtime evidence inspected.

## Two brokers, one engine

| Shared ownership | Broker-specific ownership |
|---|---|
| Candidate selection, risk, lifecycle, audit ledger | Account, native order payload, dry-run BPR |
| Frozen policy and immutable execution venue | Submit, cancel, replace, order status and fills |
| Portfolio exposure and reconciliation orchestration | Positions and capacity for that venue |

A persisted lifecycle retains its venue for entry, adjustment, exit, and
reconciliation. Position keys include venue and option symbol. Reserved broker
aliases cannot be remapped across brokers. The unused environment route was
removed so default adapter creation agrees with the venue registry.

Public/shared quotes remain a dependency even for Tastytrade execution. Separate
order venues do not imply independent quote infrastructure. A venue-specific
Public order incident differs from missing actionable quotes for a Tastytrade
candidate; fresh usable quotes remain mandatory.

## Official API review and resulting fixes

Public uses its multileg preflight/order contract, including BUY/SELL and
open/close indicators. Tastytrade ordinary multileg options use `/orders`, native
action strings, and positive price plus Credit/Debit effect. Tastytrade
`complex-orders` refers to contingent order arrangements, not ordinary two-leg
strangles. These differences belong in adapters, not duplicated strategy engines.
See [Public preflight](https://public.com/api/docs/resources/order-placement/preflight-multi-leg),
[Public order example](https://public.com/api/docs/templates/place-multi-leg-options-order),
and [Tastytrade order concepts](https://developer.tastytrade.com/docs/concepts/orders-and-order-types/).

Three material broker fixes followed the documentation review:

- Replacement checks dry-run errors even on HTTP success, then sends PATCH without
  legs. A strategy roll is a new close/open order, not a price edit of a position.
  [Tastytrade PATCH contract](https://developer.tastytrade.com/reference/orders/patchAccountsAccountNumberOrdersId/).
- Filled status may precede complete leg-fill details. The adapter waits for
  complete returned-leg fills and derives net package price from actual cashflows.
  Missing details cannot be replaced with the requested limit price.
  [Order lifecycle](https://developer.tastytrade.com/docs/concepts/order-lifecycle/).
- `external-identifier` correlates orders but does not deduplicate submissions.
  The ledger records uncertainty before POST, blocks blind retry and fallback,
  and recovers only from a unique broker lookup. An absent or ambiguous match stays
  blocked for review. [Idempotency and retries](https://developer.tastytrade.com/docs/guides/idempotency-and-retries/).

Use direct REST for this deterministic executor. Tastytrade's official MCP is a
self-hosted tool interface with separate write-confirmation behavior. It would add
another execution interface without replacing Kamandal's lifecycle obligations.
A future read-only MCP could support conversational inspection; it is unnecessary
for Tuesday. [Official MCP documentation](https://developer.tastytrade.com/docs/sdks-and-tools/mcp-server/).

## Evidence and practical limits

On September 6, oldmac was at `0a83a1d`, tracked-clean, with Kamandal launchd jobs
idle and last exit zero. The strangle lifecycle table had four closed shadow,
five missed shadow entries, and two open shadow lifecycles; no live strangles.
Selected strangle actions included eleven opens, six holds and four closes,
with no adjustments. The production observed-package evidence table was empty.
These are machinery observations, not evidence of profitability or successful
Mike/Greg live execution.

The full local suite passed 894 tests. Two additional uncertainty-recovery tests
were then added and passed with the existing uncertainty test. The exact-source
integration test exercises the unified planning-to-live-lifecycle handoff and
one-canary reservation using fixtures, preserving source identity and Tastytrade
venue. Adapter tests cover actual entry/roll fill arithmetic, delayed fill
information, replacement errors, and client-identity lookup. None placed orders.

Both prospective policy tables pass planner, unified, CSA compatibility, and
source-policy validation: 100 universe rows, 19 playbooks, 15 enabled, four source
rows, zero errors. Two pre-existing overlapping-variant warnings remain. These
checks use saved tables and do not represent a write to the current Sheet.
A separate read-only validation on oldmac at 18:36:52 UTC also passed against
the actual operator Sheet, with the same row counts and zero errors.

The existing Tuesday automation is active for 07:45/14:45 CT and still references
an older reviewed baseline. Deployment approval should include requiring this
repair revision in that morning gate and reading this report. Keep its finite
one-contract, $2,500 BPR ceiling and afternoon return to shadow. Arming only lets
natural scheduling evaluate a genuine candidate; no candidate is a valid outcome.

## Concrete deployment decision

Approve the reviewed source revision for oldmac deployment at a session boundary,
the scoped source/accepted-input Sheet edits described above, cleanup of the inert
inversion flag, and the Tuesday automation's required-revision update. Read current
Sheet cells before any write and preserve unrelated changes. Revalidate the live
Sheet and deployed revision after migration. Keep the strangle row shadow until
the already authorized Tuesday fresh gates pass.

Runtime closeout must distinguish deployed code, accepted source evidence,
reservation, native order acknowledgement, complete fills, tested-side management,
and reconciliation. Fresh Tuesday account capacity and usable quotes cannot be
certified on Sunday. Never manually manufacture a candidate or invoke a trading
job to obtain those receipts.

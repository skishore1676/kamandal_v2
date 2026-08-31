# Tastytrade live handoff

## Current boundary

Kamandal has the broker-order primitives needed for the short-strangle lane:
two-leg open and close, mixed close/open adjustment, broker dry-run, submit,
status, cancel, atomic replacement, partial-fill normalization, and
venue-qualified reconciliation. The strategy remains `shadow` during the evidence
week. Suman has granted the specific conditional authority described below; these
capabilities alone grant no broader authority to place an order.

The oldmac production runtime already has an explicit account number, OAuth
material, and the pinned Orders API version. Natural shadow candidates on
2026-08-26 through 2026-08-28 recorded `bpr_source=tastytrade_dry_run`, proving
that the configured production authentication, exact-leg dry-run, and BPR path
are reachable. No additional account or credential setup is required for the
short-strangle pilot.

## Operator boundary

On 2026-08-30 Suman authorized both the Google Sheet promotion and one real
one-contract Tastytrade canary, conditionally: Friday, 2026-09-04 must be `GO`,
Tuesday, 2026-09-08 final gates must remain green, and the normal planner and
executor must select and submit the order. Otherwise the row remains shadow.
There is no remaining credential or routine setup task for Suman.

Existing credentials remain secret and deploy-preserved. If rotation is ever
needed, do not paste values into Codex, chat, Git, the Google Sheet, an Obsidian
note, or a shell command. Use the hidden oldmac prompt:

```bash
ssh oldmac
cd /Users/sunny/Documents/kamandal_v2
.venv/bin/python scripts/configure_tastytrade_runtime.py --rotate-oauth
```

The helper atomically updates the owner-only runtime environment and prints key
names only, never values.

## Required verification ladder

1. `kamandal tastytrade-readiness` must report production configuration ready,
   account configured, the documented host, the pinned Orders API version, and
   all multileg payload capabilities except the separately known DXLink gap.
2. A natural shadow candidate must retain an exact-leg production
   `tastytrade_dry_run` BPR receipt. The 2026-08-26 through 2026-08-28 TLT/IEF
   candidates already establish this path historically; the corrected shadow
   week supplies fresh evidence.
3. The natural planner must prove that the retired range veto and corrected
   low-OI pricing behavior no longer create machinery blockers.
4. Immediately before promotion, read production account capacity, current
   positions, exact candidate dry-run BPR, live health, Sheet policy hash, and
   venue-qualified reconciliation readiness without submitting an order.
5. Under the recorded conditional operator approval, promote only the bounded
   one-contract pilot and permit one canary lifecycle. Submission, status,
   management, close, and
   reconciliation receipts determine whether the lane remains live or returns
   to `shadow`.

See [STRANGLE_PILOT_RUNBOOK.md](STRANGLE_PILOT_RUNBOOK.md) for the dated checklist,
state machine, wakeups, and exception policy.

The default readiness check remains broker-inert and does not authenticate or
use the network:

```bash
.venv/bin/kamandal tastytrade-readiness
```

## Optional certification sandbox

A separate certification sandbox may be used when a future broker-adapter change
needs disposable order-state experiments. It is not a promotion prerequisite for
this pilot: it needs separate credentials, supplies delayed quotes, resets trade
state daily, and cannot prove production fill quality or strategy economics.

If intentionally used later, configure it in its own ignored file:

```bash
.venv/bin/python scripts/configure_tastytrade_runtime.py \
  --env-file .env.tastytrade-sandbox --sandbox --rotate-oauth
```

See
[`docs/lessons/production-dry-run-can-replace-separate-broker-sandbox-gate.md`](lessons/production-dry-run-can-replace-separate-broker-sandbox-gate.md)
for the decision test.

## Broker references

- [Tastytrade Orders API](https://developer.tastytrade.com/open-api-spec/orders/)
- [Tastytrade sandbox](https://developer.tastytrade.com/sandbox/)
- [Tastytrade API FAQ](https://developer.tastytrade.com/faq/)

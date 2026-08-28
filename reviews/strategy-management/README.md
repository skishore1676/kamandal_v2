# Strategy management reviews

This directory preserves dated, human-readable reviews of Kamandal's strategy
management policy. These reviews are decision records and drift checks; they
are not executable policy and do not authorize Sheet, broker, deployment, or
runtime changes.

## Cadence

Run the review monthly while management rules are changing. Move to every two
months after two consecutive reviews find no unresolved policy drift.

Each review should:

1. read the current oldmac runtime checkout and newest captured Sheet policy;
2. compare current Sheet values with frozen policy on representative open and
   recently closed lifecycles;
3. inspect recent profit, loss, time, event, adjustment, and quote-quality
   decisions, including fills and realized cashflow where available;
4. compare only the relevant strategies with current official tastylive or
   other explicitly chosen practitioner guidance;
5. record Suman's Thinkorswim example as the operator specification;
6. classify each verdict as keep, change candidate, shadow experiment, retire,
   or unresolved; and
7. separate documentation decisions from Sheet-only, code, deployment, and
   live-policy actions. No action crosses those gates without explicit approval.

## Reviews

- [2026-08-28](2026-08-28.md) — initial consolidated baseline and operator
  verdict pass.

## Next review focus

- Run the separately approved bounded shadow activation for the implemented,
  disabled-by-default resting profit-target policy and review natural evidence.
- Compare 1.5x versus 2.0x credit-vertical close-debit outcomes using new and
  historical lifecycle evidence.
- Review a real directional-diagonal Thinkorswim example after the construction
  specification is settled.
- Confirm that future calendar and diagonal entries reflect only approved Sheet
  changes; never infer that open lifecycles were rewritten.

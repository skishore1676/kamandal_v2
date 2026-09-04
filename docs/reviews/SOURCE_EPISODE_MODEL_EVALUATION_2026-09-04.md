# Source Episode Model Evaluation — 2026-09-04

## Verdict

Use **Terra** for the Source Episode Compiler. The compiler and its projection
adapters may be deployed under the existing Sheet ceilings: Greg ideas retain
their existing live planner ceiling, Mike ideas remain observe-only, and Mike
exact packages remain shadow-only.

Terra passed the hard safety gate in all three repeated runs. Luna failed all
three repeated runs and also failed a later post-hardening check because it
reversed explicit structure language in two cases. Deterministic source grammar
was strengthened after the bakeoff so literal source phrases outrank model
labels, but that does not erase Luna's observed instability.

The implementation now connects the effect-free compiler to the existing idea
and exact-package seams. It does not add an execution path: the existing Sheet
policy, optimizer, lifecycle manager, and broker gates remain authoritative.

## Frozen-corpus results

| Checkpoint | Hard gates | Event recall | Event precision | Entry recall | False entries | Invented media packages | Compiler failures |
|---|---:|---:|---:|---:|---:|---:|---:|
| Terra, three repeated runs | 3/3 | 90.58% | 79.68% | 53.85% | 0 | 0 | 0 |
| Luna, three repeated runs | 0/3 | 52.90% | 68.80% | 46.15% | 2 | 0 | 3 |
| Terra, post-hardening check | 1/1 | 95.65% | 83.02% | 46.15% | 0 | 0 | 0 |
| Luna, post-hardening check | 0/1 | 82.61% | 70.37% | 76.92% | 2 | 0 | 0 |

These numbers are not alpha, P&L, or trading performance. They measure whether
the interpreter represents the operator-reviewed posts correctly.

## What the evaluation changed

The first smoke run showed both models could mistake Greg management language
such as `looks to expire` for a new opening. That is stable source grammar, so
the fix belongs in Greg's deterministic profile rather than a more elaborate
prompt. `bought to close`, `looks to expire`, expiry/close instructions, and
`added` now receive deterministic action semantics.

The compiler also now:

- canonicalizes source vocabulary such as `strangle` to `short_strangle`;
- lets explicit structure text override a conflicting model label;
- keeps idea readiness separate from exact-package media completeness;
- groups several same-thesis package variants under one atomic event instead
  of creating duplicate planner ideas;
- accepts legitimate futures symbols such as `/MES`;
- drops an optional non-numeric displayed price instead of failing a complete
  source batch; and
- allows one repair pass, then fails the source closed and records the failure.

## Remaining proof gates

The 29-case workbook-derived corpus does not contain the original public images
for every image-dependent case. Therefore the required 100% exact-leg agreement
gate has **not** been tested. The Birdclaw repair is intended to make future
cached-media replay possible, but a green unit test is not evidence that oldmac
has captured those images naturally.

Before any exact-package promotion beyond the current shadow ceiling:

1. naturally verify Birdclaw's public-media repair on oldmac;
2. add a held-out replay containing actual cached public images and thread
   context;
3. rerun the complete effect-free replay against the deployed compiler; and
4. verify natural scheduled projection and downstream consumption receipts.

The deployment does not change the four operator-owned `trade_sources` rows.
Missing media, missing history, unsupported structures, and ambiguity continue
to park without a broker effect.

# Source Episode Compiler

Date: 2026-09-04
Status: September 5 interpretation fixes deployed on oldmac; post-update natural scheduled execution pending

See the [September 5 deployment receipt](reviews/guru-interpretation-deployment-2026-09-05.md) for CLI, tests, unchanged policy, and model-assignment readback.

## Decision

Every trade source has its own interpretation profile, while Kamandal owns one
shared episode compiler.

A profile may contain source-specific deterministic grammar, examples, agent
instructions, and a bounded history-retrieval recipe. Greg's grammar must not
be reused as Mike's grammar, and a future source receives its own profile. The
profiles nevertheless run through the same compiler, evidence store, Sheet
policy, portfolio planner, execution adapter, and lifecycle manager.

This is the minimum extensible shape:

```text
Birdclaw post, thread, and public media
                  |
                  v
        source-specific profile
   deterministic rules + agent brief
                  |
                  v
       one shared episode compiler
   preparse -> context -> interpret ->
        validate/link -> project
                  |
                  v
       atomic source events[]
                  |
        +---------+----------+
        |                    |
        v                    v
      idea             exact_package
        |                    |
        +---------+----------+
                  v
     existing Sheet ceiling + planner
                  |
                  v
 existing shadow/live adapter and manager
```

There is no guru-specific scheduler, database, planner, strategy row, manager,
or broker path.

## Why a source episode, not a tweet label

One post can say that a prior package closed, a second package was adjusted,
and a third opportunity was opened. Therefore the parent post is evidence, not
the trading unit. The compiler decomposes it into atomic source events before
anything is projected as an idea or exact package.

The terms are deliberately strict:

- `idea` is a new thesis or opportunity. Kamandal chooses the compatible
  playbook and constructs the legs.
- `exact_package` is a complete, observed package. Kamandal preserves its
  strikes, expirations, option types, sides, and normalized ratios.
- `residual` is retained evidence that is commentary, incomplete, unsupported,
  a follow-up benchmark, or otherwise not planner input.
- `episode` is the post plus the atomic events and links derived from it. It is
  not a new execution lane.

A source phrase whose declared grammar unambiguously defines a standard ratio
may normalize that ratio. For example, a `212.5/220/225 bw call fly` can
normalize to `+1/-2/+1`. The compiler does not infer the source's actual account
size, repair a missing strike, or invent a contract from market data.

## Ownership

- **Birdclaw** owns acquisition, immutable public-post identity, thread
  references, sanitization, media capture, and capture-completeness metadata.
- **Kamandal's source profile** owns what the source's language means, including
  deterministic grammar, examples, context policy, and the model prompt.
- **Agent Broker** supplies bounded model execution. It does not own the schema,
  source meaning, model acceptance, or trading authorization.
- **Kamandal's compiler** owns event decomposition, deterministic validation,
  historical linkage, stable identity, and output projection.
- **Google Sheet** remains the sole operator surface for the two effect ceilings
  per source: `idea` and `exact_package`.
- **The existing planner and manager** remain the only owners of portfolio
  selection, entry, execution, and lifecycle management.

## Source profile contract

Each source profile supplies only source semantics:

```text
profile_id
profile_version
deterministic_rules
agent_instructions
few_shot_examples
context_policy
```

`deterministic_rules` cover reliable source grammar: action phrases, recurring
numbered templates, option shorthand, and standard structure ratios.
`agent_instructions` explain how to read that source's prose and images.
`context_policy` states which thread, recent posts, and open source lifecycles
may be retrieved. These fields are versioned in the repository because they
define machine meaning, not operator authorization.

The Sheet does not choose prompts, models, or per-source schemas. It continues
to contain exactly two rows per source and controls only the effect ceiling.

## Shared bounded compiler

The compiler uses the following stages for every profile:

1. **Capture gate.** Verify the canonical post, source identity, timestamps,
   thread references, and declared media completeness. Missing required media
   is an evidence blocker, not an invitation to infer.
2. **Deterministic preparse.** Apply only the active profile's exact grammar and
   obvious noise rules. This can finish a simple case without a model.
3. **Context assembly.** Build a bounded packet from the current post and media
   plus the source's most recent compiled episodes. The deployed first version
   uses recency-bounded context; targeted thread, symbol, and explicit reply or
   quote retrieval is a later accuracy improvement, not current behavior.
4. **Agent interpretation.** Ask for atomic events, not one label for the whole
   post. The agent may use both text and images but must cite its evidence
   locators. A large acquisition packet is divided into bounded batches of at
   most 20 new records; each record is still interpreted exactly once.
5. **Deterministic validation and linkage.** Validate event shape and exact
   legs, then link closes, rolls, and adjustments to prior source events. An
   update with no defensible link parks; it cannot become a new opening.
6. **One bounded repair pass.** A model response that fails deterministic schema
   validation gets one repair turn with the failed check. Semantically ambiguous
   or unlinked results park; the deployed first version does not run a separate
   semantic critique turn. There is no open-ended agent loop.
7. **Projection.** Emit `idea`, `exact_package`, or `residual` children and then
   apply the existing Sheet source ceiling and downstream gates.

Simple deterministic cases do not pay for an agent turn. Complex cases use the
same orchestration but different profile instructions and context.

## Event and projection contract

The durable event needs a small, testable schema:

```text
event_id
opportunity_group_id
action                    # open | scale_in | close | scale_out | roll | adjust | hold | commentary | discovery
symbol
direction                 # bullish | bearish | neutral | unknown
structure_hint
thesis
exact_packages[]          # zero or more observed packages for this atomic event
field_provenance          # text | image | declared_grammar | linked_history
link_state                # not_needed | linked | needs_history | ambiguous
links_to[]
evidence_state            # complete | needs_media | needs_history | ambiguous | unsupported
interpretation_confidence # calibrated 0..1, informational
evidence_refs[]
projections[]              # idea | exact_package | residual, with disposition/reason
```

Confidence is intentionally one compact model signal. It never repairs missing
evidence or authorizes an effect. Exact-package completeness, lifecycle linkage,
shape support, Sheet mode, portfolio gates, and broker safety remain hard,
deterministic decisions.

When one source event produces both an `idea` and one or more `exact_package`
projections, all carry
the same `opportunity_group_id`. They are alternative representations of one
opportunity. The portfolio planner may select at most one candidate from that
group. This prevents the richer interpreter from creating two positions for
one source trade.

## Interpretation corrections from the operator review

The annotated 2026-09-03 workbook is the first validation source, with these
architectural corrections:

- A Greg earnings announcement is an **idea bundle**, not an exact package.
  The known grammar yields Ideas 1--4; only the short-strangle idea is currently
  planner-supported. Kamandal selects its own DTE and delta. No paywalled legs
  are invented.
- `took idea #4` is a source lifecycle/open confirmation linked to the earlier
  earnings bundle. It does not by itself reveal exact legs and must not create
  a second planner opportunity.
- A close, hold, roll, or expiry comment is ignored for **new entry**, but is
  retained as benchmark/lifecycle evidence rather than discarded.
- A Mike image post may yield a thesis idea and an exact package for the same
  opening. They share one opportunity group. A management post yields linked
  benchmark events, not fresh entries.
- A complete but unsupported structure, such as a crab or a three-leg diagonal
  plus short put, remains exact evidence and an explicit unsupported residual;
  it is not forced into the nearest Kamandal playbook.
- The AAPL source post says `October 295 put`; the workbook note says `395`.
  Validation uses the source evidence and records the disagreement rather than
  silently choosing the annotation.
- A capability announcement such as a broker MCP is retained as a `discovery`
  residual. It never enters the idea or exact-package planner paths.

The companion corpus lives under
`tests/fixtures/trade_source_interpretation/`. Partial cases explicitly require
media or history; they test correct parking until that evidence exists.

## Validation and model choice

Before changing model tiers, read the
[model-versus-adapter evaluation lesson](lessons/correspondents-are-profiles-not-pipelines.md#model-comparisons-must-separate-interpretation-from-adapters).
Raw interpretation, normalized events and planner admission need separate scores.

The first gate is the 29-post operator-reviewed corpus. A broader historical
holdout follows only after those cases are represented faithfully. The same
input packet, profile, prompt, and schema must be replayed against candidate
models.

Measure:

- atomic-event recall and action accuracy;
- symbol, direction, and structure accuracy;
- exact-leg and normalized-ratio accuracy;
- lifecycle-link accuracy;
- ignore/commentary precision;
- false-new-entry count;
- stable replay identity and deduplication;
- latency and Agent Broker cost.

Release gates:

- zero false new entries on closes, rolls, holds, commentary, and replies;
- every accepted exact package has 100% field agreement with its gold evidence;
- missing media, missing required history, or ambiguity always parks;
- at least 90% atomic-event recall on fully specified trade-bearing cases;
- replay produces stable identities and no duplicate lifecycle;
- an opportunity with two projections can produce at most one selected plan
  candidate;
- replay has no Sheet, planner-effect, shadow/live, broker, order, or external-
  send side effects; and
- a broken source remains isolated from other sources and existing management.

The repository routes the compiler actor to Agent Broker's `balanced` brain.
The 2026-09-04 frozen-corpus bakeoff selected `gpt-5.6-terra`: Terra passed the
hard gate in three of three repeated runs, while Luna did not. This is local
model-quality evidence, not oldmac deployment or natural-run proof. See
[the evaluation report](reviews/SOURCE_EPISODE_MODEL_EVALUATION_2026-09-04.md).

Do not choose Luna or Terra by reputation. Run each route three times over the
same gold corpus and held-out historical set. Prefer the cheapest model that
passes every safety and quality gate. Do not prebuild a permanent two-model
cascade; add escalation only if the bakeoff proves that a cheaper first pass
plus Terra materially improves cost without weakening the hard gates.

## Document and implementation plan

This document owns source interpretation. Existing documents keep narrower
responsibilities:

- [Architecture](ARCHITECTURE.md) names the compiler and preserves one engine.
- [Trade Source Routing](TRADE_SOURCE_ROUTING.md) owns Sheet ceilings and output
  routing.
- [Observed Package Evidence](OBSERVED_PACKAGE_EVIDENCE.md) owns exact-leg
  evidence and passive benchmark accounting.
- [Sheet Schema](SHEET_SCHEMA.md) owns control and generated activity columns.

Implementation is split into bounded phases. As of 2026-09-04, phases 1--5 and
the frozen text-corpus portion of phase 6 are complete locally:

1. **Built locally:** repair Birdclaw's public-photo enrichment so an unusable
   `unknown` media descriptor is replaced rather than reused.
2. **Built locally:** finish the 29-case gold corpus, including the operator's
   corrections.
3. **Built locally:** add the profile contract, event schema, context assembler, and deterministic
   validator/linker without connecting outputs to planner effects.
4. **Built locally:** add bounded model/repair orchestration through Agent Broker.
5. **Built locally:** project events into the existing `idea`, `exact_package`, and
   `residual` routes. Both executable projections use one opportunity identity,
   so the existing optimizer's one-candidate-per-idea rule supplies mutual
   exclusion without a second selection mechanism. Unchanged source records are
   reused from the episode store while model-visible history remains bounded.
6. **Partially complete:** replay the text corpus, compare Luna and Terra, and
   produce a review report. Cached public-media and held-out historical replay
   remain required before activation.
7. **Pending:** deploy at a session boundary and let natural scheduled runs prove
   activity projection and downstream consumption. A source opening must also
   pass its profile age limit; the first deployment will not backfill stale
   packages into the shadow book.

No phase adds a new Sheet control, scheduler, portfolio optimizer, manager,
database, or broker adapter.

# Bounded historical validation and strategy coverage

Decision: perform one source-grounded, two-week validation before another model sweep. This is a proposed research plan, not an activated job or permission to add live strategies.

## Why it is worth doing

The existing 29-post development corpus contains only eight non-template opening ideas. Repeating those cases measures stability on familiar examples. A different date window can expose new shorthand, missing images, multi-event posts, quoted entries, unsupported structures, and capture omissions. Its purpose is interpretation and handoff coverage, not an estimate of guru alpha.

The signed-in X browser is available. On September 5, direct browser inspection confirmed:

- [Greg's September 2 HAL/COP post](https://x.com/harmongreg/status/2095239465744716107) says he added September 38 HAL calls and September 138/144 COP call spreads. These are two source opportunities.
- [Mike's September 2 mixed post](https://x.com/TraderMikeyB/status/2095183216424419546) separates a DELL calendar close, new SNOW calendars, and a new GLD call crab, with two attached images. Text-only capture cannot prove all exact legs.

These are spot checks of familiar examples, not a fresh benchmark. Browser verification should ground labels and resolve missing context; it should not become another production collector alongside Birdclaw.

## Proposed finite experiment

1. Inventory the previous 14 complete calendar days of each source's captured posts. Deduplicate by source ID, retain actual publication times, distinguish originals/replies/quotes, and account for unavailable posts and images. Do not claim a complete timeline from a truncated export.
2. Select at most 100 previously unevaluated posts across both gurus and dates. Include a representative sample plus an explicitly separate set of rare or difficult examples: mixed actions, bare shorthand, images, repeated entries, unsupported structures, and irrelevant posts. Group linked conversations so examples from the same opportunity do not cross the development/holdout split.
3. Before running the model, independently record expected meaning from the original browser post, original images, and only context available at that time. Suman's existing annotations define source conventions. Mark genuinely ambiguous intent as ambiguous rather than allowing another LLM's guess to become gold. Give the operator only the unresolved examples for review if needed.
4. Reserve roughly 30 posts for diagnosis and 70 as a frozen holdout, adjusting if fewer exist. Freeze prompts and profiles before scoring the holdout. Once a holdout error is used to tune a prompt, retire that sample into the development set; do not advertise its replay as unseen accuracy.
5. Run Astra low once in bounded batches, using the existing evaluation lane. Replay saved answers for adapter changes without paid calls. Repeat only ambiguous/high-impact cases or one small random subset to measure variability. Do not repeat Terra/Sol unless Astra fails a defined requirement.
6. Stop after the initial sample and a report of distinct failure causes. Expand dates only if the sample has too few actual openings or new failure types are still appearing. Agree a new ceiling before a larger sweep. No background automation or uncapped multi-week model loop is proposed.

Report opening precision and recall separately, per guru and combined, with numerators and denominators. Also report direction/structure agreement, exact opening leg-field accuracy, false reopening of old entries, ambiguity/abstention, missing-capture rate, and understood-but-parked ideas. Keep deterministic earnings-template expansion out of the model-accuracy headline. The representative and deliberately difficult subsets should not be conflated into a population estimate.

A useful acceptance target is at least 90% recovery of unambiguous opening ideas on the frozen sample, with no invented complete contracts or false reopening of old entries, plus clear reasons for every parked case. This is a proposed target, not a measured result or statistical guarantee. Small denominators require more evidence before broad accuracy claims.

Meter every batch and report tokens and elapsed time. Do not convert broker total-token receipts into dollar or subscription-credit estimates. Reusing captured evidence, batching posts, and avoiding broad model comparisons should reduce repeated work. Set an absolute token/credit ceiling before actually launching the historical run; this planning step does not authorize an unbounded spend.

## When to add a strategy

Separate three kinds of missing support:

| Gap | Smallest useful response |
|---|---|
| The interpreter does not understand the source phrase | Add a source convention, alias, or evidence-backed example. No executor change. |
| The thesis is understood but the idea adapter cannot represent it | Preserve source structure and directional thesis; define an explicit, justified mapping to an existing compatible playbook if appropriate. Do not silently replace a guru structure just to make it tradable. |
| The portfolio should construct or copy a genuinely new payoff structure | Add shared strategy support only when repeated, useful opportunities justify its construction, risk, venue, and management work. |

Current profiles explicitly park Mike's crab and butterfly exact structures, and several Greg earnings-template composites have no allowed planner structures. These are coverage candidates, not yet priorities ranked by two weeks of evidence. Conversely, a structure name present in a registry does not prove new-entry construction and execution support; for example, `long_call` appears in close-only cutover handling.

Rank candidates by distinct source opportunities lost, evidence completeness, applicability to the current universe/venue, reuse of existing construction and Kamandal management, and engineering cost. Start with one frequently recurring family whose complete lifecycle can reuse existing components. Define contract legs/ratios, price convention, bounded risk and buying-power semantics, entry construction or exact-copy admission, reconciliation, and Kamandal exits before calling it supported. Paper/shadow validation precedes any separate live enablement.

A two-week sample can identify useful implementation priorities. It cannot establish the strategy's alpha or justify adding every structure the gurus happen to mention.

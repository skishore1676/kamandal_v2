# Trade-source interpretation gold corpus

`gold-v0.jsonl` is the architecture-correct companion to the operator-annotated
workbook at:

`outputs/01a03dc0-3e42-7cd0-843d-fbd328069c01/trade-source-routing-review-2026-09-03.xlsx`

The workbook remains the human source. This corpus preserves the post text and
adds expected atomic events using the definitions in
`docs/SOURCE_EPISODE_COMPILER.md`.

`gold_status=complete` means the text and operator annotation are sufficient to
score the listed fields. `partial_needs_media` and `partial_needs_history` are
also gold outcomes: the interpreter must park rather than fabricate the missing
package or link. An exact package with `disposition=benchmark_only` or
`unsupported` is retained evidence, not planner input.

The corpus is inert. A replay harness must prohibit Sheet writes, active-idea
publication, planning effects, shadow/live admission, broker calls, orders, and
external sends.


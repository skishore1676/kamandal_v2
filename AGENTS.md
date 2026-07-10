# kamandal_v2 — bootstrap contract

**Identity:** kamandal_v2 is the **multileg options cockpit** of Suman's trading
family — LLM idea extraction, review workflows, and shadow trading. It retains full
ownership of: multileg cockpit behavior, review semantics, shadow-trading runtime,
and its own launchd jobs. It is NOT the live-money executor (that is bhiksha).

## Where runtime truth lives

- Runs on **oldmac** from `/Users/sunny/Documents/kamandal_v2` (NOT `~/code`), with
  its own launchd jobs (owned and defined by this repo).
- **Safe read-only inspection:** `git log` / `git status` on the runtime checkout;
  `launchctl list` filtered to this app's labels; read its logs and artifacts.
  Open any SQLite state read-only (`-cmd ".timeout 8000"` on-host, `immutable=1`
  for snapshots). Never write, never trigger its jobs by hand.

## Money / deploy gates

- Shadow trading may relax evidence gates, **never safety gates**; nothing here may
  promote itself to live-money behavior without the operator's explicit gate.
- Deploys and launchd changes are operator-gated, done at a session boundary with
  readback against the runtime checkout.

## The family brain

Durable family knowledge (which app owns what, current state, ADRs, queue) lives in
the **private repo `tradelab`** — clone location `~/code/tradelab` on both machines.
Entry point: `docs/brain/INDEX.md`. The brain's kamandal_v2 section starts explicitly
empty — "No verified facts yet" — and grows only via evidence-backed candidates; do
not treat absence of facts as an invitation to infer them. Trust order: **runtime
evidence > diary > brain summary**.

## Forbidden by default (reading the brain grants NO authority)

- No placing, modifying, or cancelling orders anywhere in the family.
- No writes to any operator Sheet or arming surface.
- No deploys, restarts, or launchd changes to this or any trading app.
- No auth/token/credential changes; never read or copy secrets.
- No external sends without an explicit operator gate.
- Default stance: read-and-recommend. Execution authority is granted per lane by the
  operator, never inherited from documentation.

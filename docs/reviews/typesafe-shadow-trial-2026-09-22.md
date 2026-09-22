# Seven-day TypeSafe parallel comparison

Astra remains the production interpreter. The TypeSafe lane reads retained guru packets and matching Astra episodes after the existing X job completes. It writes only `data/research/typesafe_shadow`; it cannot publish ideas, write the operator Sheet, or call a broker. Existing Astra results represent fallbacks without additional Astra calls.

Configuration in oldmac `.env`:

- `TYPE_SAFE_SHADOW_ENABLED=1` enables the comparison; `0` disables it.
- `TYPE_SAFE_CONFIDENCE_THRESHOLD=0.85` sets the configured decision threshold. Both 0.85 and 0.88 are also evaluated from every saved response, without extra API calls.
- `TYPE_SAFE_MODEL=jev-1.13.0` pins the model for this trial.
- `TYPE_SAFE_SHADOW_MAX_CALLS=20` caps new calls per scheduled invocation (hard ceiling 50).
- `TYPE_SAFE_SHADOW_UNTIL` is the explicit UTC trial deadline, set to seven days after deployment. After it passes, no new requests are made.
- `TYPE_SAFE_KEY` supplies the credential. Never print it.

The lane has an independent file lock, 120-second work deadline, bounded HTTP timeouts, and no automatic retries. Exact source revisions must match Astra evidence. Inference identity excludes changing observation metadata; a content change can create a new comparison. Requests are reserved durably before sending so uncertain failures are not silently rebilled. Seed records are excluded from the prospective trial. Production job failures do not run the comparison; comparison failures do not alter the successful production result.

Images or photo links require Astra directly and incur no TypeSafe call. Other posts use TypeSafe only for typed text decisions. A candidate is accepted only when every symbol decision clears the threshold, no direction is unknown, the new-entry probability agrees, and missing-evidence probability is below the complementary threshold. Acceptance means **decision labels only**, not exact option-package reconstruction. Other cases are recorded as requiring Astra.

Receipts contain the source input, actual model response/usage, matching primary episode, routes at both thresholds, and directional agreement. Astra agreement is not independently verified accuracy. At week end, review disagreements against source evidence, count false entries and missed entries, separate image fallbacks, and measure actual complete-post work avoided. Do not treat accepted direction labels as avoided full translations.

Inspect `data/research/typesafe_shadow/summary.json` for counts, dates, usage, and routes; individual hash-named JSON files contain comparisons. Regenerate summary without model calls by loading the normal environment and running `scripts/run_typesafe_shadow.py --report-only`. Seed once with `--seed` before enabling scheduled collection. No new scheduler or live trading configuration is introduced.

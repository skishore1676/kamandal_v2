# Guru intake regression repair — September 29, 2026

## Confirmed cause and impact

Monday September 28 recorded no new live order intents in the oldmac Kamandal ledger. Birdclaw captured Mike's BA/MU opening post (2104591155346419717) and Greg's ISRG call-spread post (2104627103568375986). After incremental capture was deployed, Kamandal rejected both profiles because the new `incremental`, `incremental_coverage`, and `page_count` receipt fields were absent from its strict allowlist. The 14:00 CT activation receipt was degraded with zero records and zero observed packages. The planner's successful process status did not establish healthy source intake.

A second defect predates incremental capture: a later text-only observation replaced canonical media metadata. Incremental capture removed the repeated reads that previously could replenish that evidence. Mike's photo was present in the retained sanitized 11:35 CT export with hash `ed4355676d265df1ce429188c73e7f64567ed800a0fa811e1acab3c47e12b5ff`; the subsequent canonical export had no media.

## Repair

- Kamandal accepts and type-checks the three incremental receipt fields, including the bounded cursor object. Unknown fields remain rejected.
- Birdclaw preserves previously captured photos when the same post is observed without media, and preserves cached evidence for matching media keys/URLs when an uncached descriptor arrives. Different post IDs do not inherit images.
- A Birdclaw dry-run-first recovery command restores missing media from retained public exports after post identity, cache path, and SHA-256 verification. It changes only media metadata, not observation timestamps, text, cursors, or trade freshness.
- The X job logs degraded activation output before failing. Planner output separately reports the source owner's intake health, so zero packages no longer conceals a degraded activation.

## Verification boundaries

Saved Monday public packets are regression fixtures. Offline activation replay covers both gurus, with a recording interpreter stub: it proves packet delivery to the episode compiler, including Mike's actual hash-verified cached image after restoration. It does not claim a fresh LLM accuracy evaluation or broker preview. Existing episode/planner tests cover subsequent validation and eligibility behavior. No X reads, model calls, Sheet writes, live order submissions, or production jobs are needed for these tests.

## Deployment and readback

Deploy Kamandal compatibility first, then Birdclaw media preservation. Before deployment, export both current public correspondent packets and verify them against the candidate Kamandal validator. On oldmac, dry-run `node src/restore-public-media.mjs data/public-export/xurl-correspondent-mike_butler-tradermikeyb-20260928-163533.media.public-export.json`; retain its receipt before authorizing `--apply`. Re-export Mike's post to confirm the original cached image is present and timestamps are unchanged.

Do not replay historical entries into the live money path. Require the next scheduled activation to show both profiles without source failures, followed by planner source-health readback. Successful local tests are not deployed or natural-run proof. No eligibility, allocation, structure support, or exit-policy relaxation is part of this repair.

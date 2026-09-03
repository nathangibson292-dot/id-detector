# Plan reviews

Four rounds of independent review by OpenAI Codex (`gpt-5.6-sol`, reasoning `xhigh`) against `docs/PLAN.md`, run 2026-09-03. Each round's findings and its status table for prior findings are in `plan-review-round-<N>.md`.

| Round | Verdict | P0 | P1 | P2 | Plan revision that responded |
|---|---|---|---|---|---|
| 1 | CHANGES_REQUESTED | 3 | 15 | 1 | rev 2 |
| 2 | CHANGES_REQUESTED | 3 | 12 | 1 | rev 3 |
| 3 | CHANGES_REQUESTED | 2 | 11 | 2 | rev 4 |
| 4 | CHANGES_REQUESTED | 2 | 14 | 0 | rev 5 (final pre-build) |

The review cap was four rounds. Revision 5 fixes both round-4 P0s (transform algebra reversed; over-claimed boundary bounds) and the round-4 P1s that change contracts (durations partition, rescan generations, scanner trial/cache keys, retry ownership, timestamp parsing, certification per profile×dimension×tier, association rule, corpus construction, identity text-merge, time-varying roles, committed-corpus policy, Panako sequencing, Job-Object launcher).

## Round-4 items intentionally deferred to implementation

These are acknowledged and will be handled in the named stage rather than in further plan text:

- Exact per-event precision/recall thresholds beyond 80/80 — tune in Stage 4c against the controlled corpus.
- Learned timing likelihoods for every hint kind — Stage 5 once `calibration` data exists.
- Provider correlation estimates — Stage 4c ablations.
- Full `benchmark_report` metric field list — the schema in Stage 0 enumerates the metrics named in the plan; additions are versioned.

## Standing owner questions (unchanged across rounds)

1. Provisional tiers for v1 (default) vs funding a certified corpus.
2. AudD/ACRCloud trial credentials and a hard test budget before Stage 3.
3. JDK for a minimum Panako path before profile freeze, or exclude reference-pool recognition from v1.

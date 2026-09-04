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

## Code-review rounds (per build stage)

Each build stage was reviewed read-only by an agent separate from its builder, against the plan and
the diff. Stages 0–4a were reviewed by **Codex** (`gpt-5.6-sol`, reasoning `xhigh`); from Stage 4b on,
the Codex usage limit was reached (2026-09-04), so the reviews were run by **Claude (Opus)** under the
same prompt and read-only discipline, still with reviewer and builder as separate agents.

| Stage | Reviewer | Verdict | P0/P1/P2 | Outcome |
|---|---|---|---|---|
| 0 | Codex | FIX_FIRST | 0/7/4 | all 11 fixed, committed `6fbfde3` |
| 1 | Codex | FIX_FIRST | 0/9/3 | all 12 fixed, committed `9fda080` |
| 2a | Codex | FIX_FIRST | 0/6/3 (+1 by Claude) | all 10 fixed, committed `3bcf29a` |
| 2b | Codex | FIX_FIRST | 0/9/3 (+2 by Claude) | 13 fixed, 1 owner-blocked, committed `3ee2c61` |
| 3 | Codex | FIX_FIRST | 0/5/3 | all 8 fixed, committed `53a5bae` |
| 4a | Codex | FIX_FIRST | 0/9/3 | 11 fixed, 1 owner-blocked, committed `c4ddd4a` |
| [4b](code-review-stage-4b.md) | Claude | FIX_FIRST | 1/5/4 | P0 fixed (proved bounds + evidence support now come only from the per-trial selected votes, never a rejected/minority sibling — rev 5.2); P1s addressed (content-based insertion assertions; 0/0 precision handling) |
| [4c](code-review-stage-4c.md) | Claude | OK_TO_COMMIT | 0/0/6 | no P0/P1; all six P2s are transparency/hardening notes, disclosed in the stage report (e.g. `jump` cluster lower bound, event-match horizon, `reset` unexercised) |
| [5](code-review-stage-5.md) | Claude | FIX_FIRST | 0/2/2 | both P1s fixed: `certify` threads `--profile` into the real-mix branch, and `recording_supported`/`n_competing_candidates` were extracted into shared helpers so fit-time and analyse-time compute them identically |

Stages **4d, 6, 7 and 9** had no separate code-review round (Codex unavailable); their verification is
in each stage report, and Stage 9 re-ran the full lint/format/audit/test gates for the whole tree.

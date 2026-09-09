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

## v2 plan review rounds (2026-09-09, Codex gpt-5.6-sol xhigh, read-only)

Object under review: `docs/PLAN-v2.md`. Sources: `v2-review-engines.md`, `v2-review-ui.md`,
`v2-review-hosting.md`, `../research/05-market-2026-09.md`.

| Round | Plan rev | Verdict | P0/P1/P2 | Outcome |
|---|---|---|---|---|
| [1](plan-v2-review-round-1.md) | 1 | CHANGES_REQUESTED | 9/7/2 | rev 2: qualified findings, Deep scan v2, recipes/ledgers, pricing recomputed; P0 #2 (remove Shazam from hosted defaults) declined by the owner (D1) |
| [2](plan-v2-review-round-2.md) | 2 | CHANGES_REQUESTED | 8/9/1 | rev 3: two frozen recipes, reservation semantics, bundles in Phase 1, Deep-on-Free = AudD sweep, sidecar pruning, ledgers, status table, 24 cycles |
| [3](plan-v2-review-round-3.md) | 3 | CHANGES_REQUESTED | 6/9/1 | rev 4: explicit `serves()` table, attempt state machine, `bundle_id`, lots + allocations, `provider_attempts`, fenced backups, M1/M2 milestones |
| [4](plan-v2-review-round-4.md) | 4 | CHANGES_REQUESTED | 6/8/1 | rev 5: overlap rule fixed, status matrix, per-dispatch USD admission, intake job, scope in `analysis_key`, executable smoke/readiness gates, 29 + 6 cycles |
| [5](plan-v2-review-round-5.md) | 5 | CHANGES_REQUESTED | 7/8/1 | final round (owner cap); rev 6 applies all seven P0s and the cheap P1/P2s (auth/quota outcomes, `/healthz` in 0a-i, provisional `targeting:0`, density rule fixed, provisional subscriber reservations + payer transfer, `BUILD_ID` readiness, lock-before-snapshot backups) without a further review; 33 M1 + 6 M2 cycles; build starts at 0a-i |

## v2 build cycles (Codex gpt-5.6-sol xhigh builds; Codex read-only diff reviews; owner's orchestrator verifies + commits)

| Cycle | Build report | Diff review | Verdict → outcome | Commit |
|---|---|---|---|---|
| 0a-i | [build-0a-i](build-0a-i.md) | [diff-review-0a-i](diff-review-0a-i.md) | FIX_FIRST (4 P0 / 2 P1 / 1 P2) → items 2–7 fixed with regressions; item 1 (PowerShell host) overridden by the orchestrator (`pwsh` not installed; Windows PowerShell accepted in the plan); 594 passed | see git log |
| 0a-ii | [build-0a-ii](build-0a-ii.md) | (in report, § Review + fix pass) | Codex build interrupted by a session restart; completed by a Claude Opus builder (4 gaps: pricing path, settlement on mid-dispatch crash, free zero-cap, journal-before-settle); Opus review OK_TO_COMMIT after fixing P0 (bare `--profile max_accuracy` silently billed AudD) + 4 P1; 620 passed | see git log |

**Model switch (2026-09-09, owner):** from cycle 0a-iv builds run on Claude **Fable** subagents and reviews/fixes on Claude **Opus**; Codex is parked until its weekly quota resets. Agent prompts carry a hard no-live-provider-calls rule (the CLI auto-loads `.env`).
| 0a-iv + 0a-iii (merged) | [build-0a-iv-0a-iii](build-0a-iv-0a-iii.md) | (in report, § Review + fix pass) | Fable build; Opus review OK_TO_COMMIT after P0 (`--allow-degrade` could fire after a billable ambiguous attempt) + 2 P1 (Windows connection reset on early-rejected POSTs; `mixcloud` hints switch); plan gate amended to a fresh work root per run (Windows long-path cache files); 661 passed | see git log |

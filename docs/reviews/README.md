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
| 0b-i + 0b-iii (merged) | [build-0b-i-0b-iii](build-0b-i-0b-iii.md) | (in report, § Review + fix pass) | Fable build; Opus review OK_TO_COMMIT, no P0; 2 P1 fixed (AudD's own 429 body read as terminal quota_error; unit left outstanding when an adapter failed after admission); 727 passed | see git log |
| 0b-ii | [build-0b-ii](build-0b-ii.md) | (in report, § Review + fix pass) | Fable build; last Phase-0 cycle: effective config under a profile (H6) with `config show --profile`, novelty guarded + hoisted (M2), Shazam malformed bodies penalise the limiter (S1), true scanned window set (M10), Local Free golden. Opus review OK_TO_COMMIT, no P0; 3 P1 fixed (`config show` blamed an absent `idea.toml`; the throttle line's denominator counted transform siblings and always claimed the run ends partial; the M10 scanned subset widened the rescan request budget past `--max-requests`); 756 passed; owner test-drive follows | see git log |
| 1b-iii (scorer) | [build-1b-iii-scorer](build-1b-iii-scorer.md) | (in report, § Review + fix pass) | Fable build; scorer half only (breaker deferred): `scripts/score_corpus.py` (run list -> presentation floor -> the existing benchmark scorer per mix -> pooled e4 counts, `truth_status` label, `l3` block, `--print` paragraph), `tests/fixtures/corpus-mini/` with `expected.json`, `pooled_metrics` + `prediction_set_from_fusion` helpers. Opus review: 2 P0 fixed (the scorer crashed on every real run list — identities were the newest generation beside the episodes, not the one the completion sidecars name; and crowd rows violate the `ScoredEpisode` proved-bound contract) + 3 P1 (no media-key check, so a mistyped run list scored another mix; a duplicated mix double-weighted itself; `l3.met` claimed L3 on the thresholds alone); preliminary Free score over the 5 draft-truth release-1 mixes runs end to end | uncommitted |
| 1b-iii (scorer ii) | [build-1b-iii-scorer-ii](build-1b-iii-scorer-ii.md) | pending | Fable build; orchestrator follow-up to the scorer: `--match auto|time|work` in `scripts/score_corpus.py` — work-only (time-agnostic) matching for order-only truth (the rekordbox-playlist drafts, detected from the seed's placeholder equal-slice timings), through fusion's own normaliser and word-set rule (`hints.relations._normalise`, `fuse.identity._word_sets_corroborate`); `work_precision_e4` / `work_recall_e4` / `likely_precision_e4` with `listed_precision_e4` and `l3.thresholds_met` null in that mode, per-mix missed / wrong lists and a `work-match.json`; the time path unchanged (regression-pinned on `corpus-mini`) and given the work-only numbers as a secondary line; `corpus-mini/expected.json` (work) + `expected-time.json` (time) | uncommitted |
| 1b-iii (scorer iii) | [build-1b-iii-scorer-iii](build-1b-iii-scorer-iii.md) | pending | Fable build; orchestrator follow-up: (1) timed-mode identity parity — the certified scorer is untouched, the wrapper canonicalises what it is handed (`parity_identities`: each truth row the work matcher pairs lends its own scorer key to that prediction's work, fusion's own crowd-label mechanism) and the featuring marker (ft/feat/featuring) is dropped from the word set, so `--match time` scores identity as `--match work` does; (2) `idea truth seed --overlays` — 'w/' lines become layered truth episodes linked both ways, `data/corpus/release-1/mph-youtube-set/ground_truth.json` re-seeded (54 -> 63 episodes, bullets gone); (3) crowd-label cleanup at the source (`hints/parse.py`: enumerators, bracket/dash bullets, TRACK LIST headers, repeated `@` mentions stripped before the split; no `@handle` ever in a label); per-timed-mix `median_offset_ms` diagnostic. MPH by time 2/11 -> 9/11 likely, 7/38 -> 29/38 listed, 7/53 -> 29/62 recall | uncommitted |
| 1b-i | [build-1b-i](build-1b-i.md) | (in report, § Review + fix pass) | Fable build; Opus review OK_TO_COMMIT after 1 P1 (unreadable window clip failed the whole run over a tie-break) + 2 P2; allocation hand-verified + 400-case probe; 881 passed | see git log |
| 1b-ii | [build-1b-ii](build-1b-ii.md) | (in report, § Review + fix pass) | Fable build; corroboration across trust families (catalogue {audd, acrcloud} / shazam / local_index {panako}) on selected votes with supports overlapping >= `overlap_min_ms`, pooled per normalised work; one agreement = `engine_corroborated` ("confirmed twice"), two >= `separation_min_ms` apart = `engine_corroborated_separated`, the only cross-engine bypass of suppression and the 30 s floor (the `likely` rule untouched, E-S6); trial attribution by family (E-L5); crowd rows: plausible labels only, position order, contradictory answers at one timestamp -> one row + alternatives (E-M4, U-F13); E-M3/E-M6 verified (stale `--recipe` help fixed); recipe thresholds threaded `cli` -> `orchestrate` -> `fuse`; page "confirmed twice", `PAGE_VERSION` 18, golden +1 key; matcher edges decided on the release-1 A/B (feat-marker fold and short-word trailing-letter rule adopted, digit/letter split rejected as a no-op); pooled likely precision 61/68 unchanged, work precision 74.7 -> 75.1 %, recall 63.8 % held; Opus review OK_TO_COMMIT after 2 P1 (catalogue trust family now derived from `COMMERCIAL_PROVIDERS` so review M1 cannot be re-opened by a new engine; a crowd row could still be *displayed* as a pasted `FULL TRACK LIST:` line) + 1 fixed P2 (a recipe threshold of `0` swallowed by `or`) + 4 P2 notes; corpus A/B reproduced independently by offline re-fuse; 905 passed | uncommitted |

**Model switch (2026-09-11, owner: "switch to astra medium to continue the build"):** Phase 1 builds run on
Codex **`gpt-6-astra`** at medium reasoning effort (`codex exec -m gpt-6-astra -c model_reasoning_effort=medium
-s danger-full-access`; requires codex-cli >= 0.154.0 — the bare id `astra` is refused on a ChatGPT account).
Reviews are **two passes** from here: Codex **`gpt-5.6-sol`** at xhigh reads the diff read-only, then a Claude
**Opus** reviewer forms its own findings, folds in sol's, fixes every P0/P1 with regressions, and re-runs every
gate (sol's sandbox cannot run pytest or the PowerShell gates, so its test claims are inspection-only). The
orchestrator re-runs all gates independently and commits.

| 1a-i + 1a-iii (merged) | [build-1a-i-1a-iii](build-1a-i-1a-iii.md) | (in report, §§ Review + fix pass, Second review (sol xhigh) + fix pass) | astra build; immutable bundles + sealed manifests + frozen fuse runs + publication invariant + `present/current` + refresh, `/media/<media_key>/audio`, cached-open via `work/index.json`, legacy-layout fallbacks, `scripts/gate_local_mode.ps1`. Opus pass: 1 P0 (a `degraded`/`partial` run's bundle was unreachable — not in the library, 404 on its page, invisible to `_load_cached`) + 4 P1 (URL parsing crashes from the web form; every audio range request re-hashed the whole library; a damaged publication bricked that run permanently; a lost verified-original guarantee). Sol pass then found 1 P0 + 3 P1 Opus missed: acquisition/refresh resolved run rows and run identity separately, so a newer **partial** run's tracklist could be published under an older **complete** run's status and promoted to `current` (now one locked run snapshot); a previously sealed but damaged frozen run was resealed from today's mutable fuse; manifests were unvalidated so one damaged file broke the whole library; duplicate media aliases were dropped; the claimed in-memory index fallback did not exist. 16 regressions added; 942 passed; owner's `work/` untouched and corpus metrics unchanged | 0665f04 |
| 1a-ii | [build-1a-ii](build-1a-ii.md) | (in report, § Review + fix pass (sol xhigh second review folded in)) | astra build; `analysis_key`, `serves()` (`compat_version 1`), Deep-on-Free reuse, `source_changed`. Review order reversed (sol read-only first, then Opus reviewing independently and fixing both sets): 3 P0 — `serve_free_from_deep`/`compat_version` were taken from user `[cache]` config instead of the `pricing.toml` authority; `find_result` searched only one media directory, so the same bytes reached by a second URL re-reserved and **re-paid the AudD sweep**; web acquisition reopened `present/current`, delivering and writing acquisition onto a different run than compatibility selected — plus 2 P1 (Free results stamped with Deep's algorithm/adapter versions; Deep-on-Free money assertions incomplete) and 3 P2. Verified as built: Deep-on-Free reserves primary only with **0** Shazam requests, the §3.4 table row by row, `hints_snapshot_id` stable and content-sensitive, exit 5 publishes nothing. 983 passed; corpus metrics identical to `0665f04` | e4fb76b |
| 1b-iii (breaker) | [build-1b-iii-breaker](build-1b-iii-breaker.md) | (in report, § Review + fix pass (sol xhigh second review folded in)) | astra build; per-process breaker (rate rule, daily budget, three-strike latch, `waiting`/`degraded` effects). Sol then Opus: 1 P0 — while rule (b) (daily budget) was open, rule (a) was skipped and the trip counter cleared at 00:00 UTC, so a throttled run could fail all day and Shazam would re-enable with **no latch**, defeating D8; the builder's own test codified the defect and was replaced. 4 P1: a cached compatible Free result was refused although it needs no Shazam call; a breaker refusal surfaced as a `failed` job (new `WAITING` state added); the `--allow-degrade` refusal journalled a null settlement and never released the real Deep reservation; **`gate_local_mode.ps1` itself failed** once the kill-switch became real (the builder never ran it). Boundaries attacked and judged correct: strict `>`, opens at exactly 20 samples, window eviction, latch surviving day rollover, hard-off short-circuit, no proxy, no singleton. 1033 passed; corpus metrics unchanged. **Owner ratification needed:** the 2,000/day attempt budget is provisional until the S3 spike | 9e36179 |

**Phase 1 complete (2026-09-11).** Next in plan order: 2b (retention and sidecar pruning), then 3a-i.

**Model switch (2026-09-11, owner: "continue with the build on sol medium"):** builds moved from `gpt-6-astra`
medium to **`gpt-5.6-sol` medium**; reviews stay two-pass (sol xhigh read-only, then an Opus reviewer-fixer that
folds both sets of findings in). From 2b onwards a second Claude session shares the tree (its playlists module,
`tests/test_playlists.py`, `README.md`, `idea.cmd` and `present/theme.py` are excluded from my cycles' reviews;
its five-point result-page contract is a constraint on every presentation cycle).

| 2b | [build-2b](build-2b.md) | (in report, § Review + fix pass (sol xhigh second review folded in)) | sol-medium build; `idea gc --policy local|hosted` (opt-in, dry-run default), `work/.trash/<date>` with a 7-day purge, per-status retention, `pruned_upstream` markers + re-derivation, `--keep-intermediates`. sol then Opus: 3 P0 — retention judged from a run's newest journal entry alone, so a failed refresh after a successful **local** run deleted that run's original and cancelled an earlier keep pin; directory walking followed symlinks/junctions with no containment check (demonstrably moving an **external** tree into the trash and `rmtree`-ing an unrelated external tree); the plan was computed **before** taking `.media.lock` while an unreadable bundle counted as absent, so a result could be collected as it was published. Plus 5 P1 and 3 found by the fixer (trash purged at ~6.5 days at the date boundary; a `pruned_upstream` marker was never re-checked so a re-derived upstream with different bytes verified clean; a bundle past Windows MAX_PATH read as absent). 1077 passed | 6825ade |
| 3a-i | [build-3a-i](build-3a-i.md) | (in report, § Review + fix pass (sol xhigh review folded in; resumed after an interrupted pass)) | sol-medium build; one canonical projection behind page/hero/legend/timeline/card/Copy/CUE/Markdown/JSON, immutable re-render of page **and** exports from the frozen snapshot, M3U gone, Markdown drops Version/Role (U-F2/F3/F14). sol then Opus (two passes, the first interrupted by the owner and stashed): 3 P0 — suppression applied **after** collapse (a contradicted track resurfaced as an alternative with `suppressed_count == 0`; a suppressed primary hid its own valid alternatives); the hero "identified" % bypassed the projection (90 % where shown tracks cover 50 %); an unrelated `analysis_key` field added to the frozen Local Free golden. Both code P0s turned out to be already fixed but **shipped untested** because the gate hardcoded `collapse=False` while production defaults to `True` — the tests now run both modes, production first, and Copy is exercised by running the shipped `tracklistText()` under node. The fixer then found a further P0 of its own: coverage summed durations naively, so overlapping tracks double-counted (340 s of evidence in a 300 s mix clamped to 100 % instead of an honest 60 %) — now a union partition with the fuser's exact-cover invariant asserted. Playlist contract verified intact. 1094 passed; projection gate 5 → 16; corpus metrics unmoved | 8639742 |


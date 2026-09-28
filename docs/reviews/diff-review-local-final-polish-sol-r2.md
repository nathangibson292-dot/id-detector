## A — AudD codes

902–905, 19, 31337 and 611 are conservatively handled. They map to a stopping reason but journal as billable `malformed` ([paid_clip.py:189](src/id_detector/paid_clip.py:189), [paid_clip.py:330](src/id_detector/paid_clip.py:330)). Settlement counts each dispatched request before the stop is raised ([paid_clip.py:832](src/id_detector/paid_clip.py:832), [paid_clip.py:841](src/id_detector/paid_clip.py:841)); resume treats `malformed` as settled and never re-sends it ([run_ledger.py:291](src/id_detector/run_ledger.py:291)).

The exact owner-approved exceptions match: 900/901 and HTTP 401/402/403 remain zero-cost ([paid_clip.py:187](src/id_detector/paid_clip.py:187), [paid_clip.py:362](src/id_detector/paid_clip.py:362)). Numeric-code precedence prevents any named billed code reaching a refund path.

## B — spend wording

The pipeline freezes settlement before returning the refusal ([pipeline.py:1254](src/id_detector/pipeline.py:1254)). `spent_words` renders that value ([paid_clip.py:247](src/id_detector/paid_clip.py:247)); CLI and browser pass `RunResult.usd_e6_spent` ([cli.py:979](src/id_detector/cli.py:979), [runner.py:394](src/id_detector/webapp/runner.py:394)). Both page implementations prioritize positive settled spend ([server.py:802](src/id_detector/present/server.py:802), [server.py:1175](src/id_detector/present/server.py:1175)).

The test is genuinely concurrent: event barriers keep request one in flight until request two refuses, and `max_in_flight >= 2` is asserted; the journal must contain `auth_error` plus charged `timeout_post`, with $0.01 shown ([test_final_polish.py:411](tests/test_final_polish.py:411), [test_final_polish.py:483](tests/test_final_polish.py:483)). Reverting spend propagation directly breaks both CLI/browser assertions; the recorded F3 revert proof reports both failures.

## C — `--runner`

Locked down at both boundaries. The four exact offline factories are allowlisted; test mode, an empty token and exact membership are mandatory before import ([local.py:1255](src/idea_web/jobs/local.py:1255), [local.py:1262](src/idea_web/jobs/local.py:1262)). The worker repeats validation and verifies the imported module resolves inside this checkout’s tests directory ([local.py:1280](src/idea_web/jobs/local.py:1280)); `idea serve` applies the same check ([cli.py:1187](src/id_detector/cli.py:1187)). No supplied arbitrary `module:factory` path remains.

## D — follow-ups and scope

Cached paid answers plus no token now produce CLI/browser notices ([pipeline.py:1388](src/id_detector/pipeline.py:1388), [runner.py:464](src/id_detector/webapp/runner.py:464)); unknown-length `idea cost` prints its notice before exit ([cli.py:573](src/id_detector/cli.py:573)).

The retry pass uses `max(10, ceil(10%))`, respects breaker/per-media budgets, and stops after three consecutive unreachable retries ([recognise.py:701](src/id_detector/recognise.py:701), [recognise.py:735](src/id_detector/recognise.py:735)).

The trace contains only synthetic timing, localhost/PID data and `mix-25min`; elapsed continues 2m10s → 2m15s → 2m20s ([browser-trace.json:304](docs/reviews/final-polish-shots/browser-trace.json:304)). The expanded screenshot has no personal detail; all PNGs contain only IHDR/IDAT/IEND chunks.

Diff/status exclude fusion, scorer, recipes, corpus, profiles and the second-session files. The golden remains blob `316b6171c42f0772a9bce3066cf716491f22d181`; the deepscan tree’s newest timestamp predates this pass.

## Required fixes

### Realistic

- P0/P1/P2: None.

### Adversarial

- P0/P1/P2: None.

## Follow-ups, not blockers

- P2: Pre-existing message-only fallbacks still classify an unrecognized or missing code containing “auth”, “quota”, “credit”, or “limit reached” as zero-cost ([paid_clip.py:340](src/id_detector/paid_clip.py:340)). That is broader than the owner’s exact-code rationale, but none of the named billed codes can reach it.
- P2: Although ignored by Git, this worktree contains [work/index.json:1](work/index.json:1), created at 11:45 today. Thus `git status` is clean for `work/`, but “untouched” is not literally true; it should be cleaned separately if required.

VERDICT: OK_TO_COMMIT

## A — Atomic approval

- Closed. `approve_offer()` performs offer read, paid-job insert, and conditional offer consumption within one `BEGIN IMMEDIATE` transaction (`src/idea_web/jobs/local.py:420-490`; `src/idea_web/database.py:212-223`).
- Follow-up job/run IDs derive deterministically from the offer ID (`local.py:267-276`), preventing retries after cancellation, ambiguity, or committed enqueue from creating another paid run.
- Approve/Approve and Approve/Skip are genuinely concurrent: barrier-released threads use independent `LocalJobs`/database connections against one SQLite file (`tests/test_additive_deep.py:834-901`).
- The mid-transaction failure test aborts after insertion and proves rollback, one successful retry, one paid sweep, and one reservation (`tests/test_additive_deep.py:904-930`). The recorded revert proof reports all three tests failing when the fix is removed (`docs/reviews/build-local-additive-deep.md:557-564`).
- There is no nested write transaction: `insert_job()` accepts the existing connection (`src/idea_web/jobs/worker.py:1645-1680`). SQLite serializes contenders; errors roll back, leaving the offer skippable or retryable. No realistic deadlock or stranded-offer path found.

## B — No rename

- Closed at fusion. `_Graph.presented()` captures both exported artist/title and work label exactly (`src/id_detector/additive.py:255-267`); `_preserves()` rejects replacement when either differs (`additive.py:291-325`) before `additive_merge()` decides which row survives (`additive.py:398-414`).
- The reproduction genuinely disagrees on artist and/or title while both provider nodes identify one candidate, and asserts the combined row would say something other than “Zulu — Free” (`tests/test_additive_deep.py:936-973`). The merge instead retains the original Free episode and flattened label (`:974-986`).
- Page, JSON/Markdown/CUE exports, and tracklist use the same canonical projection (`src/id_detector/present/bundles.py:429-473`; `src/id_detector/present/exports.py:724-828`). Thus the reviewer’s “Alpha — Paid” replacement cannot reach any renderer.
- The recorded revert proof reports all three disagreeing-metadata cases failing without the check (`docs/reviews/build-local-additive-deep.md:564`).

## C — Follow-ups and measurement

- Refusion assertions are exact again: five calls and `(5, 2, 0, 5)` target/span/cache/request counts (`tests/test_refusion.py:350-364,400-414`). The report now explicitly admits the earlier weakening (`docs/reviews/build-local-additive-deep.md:543-546`).
- Unsendable clips bypass reservation unless a prior authorization/reservation exists (`src/id_detector/pipeline.py:1372-1407`), with zero reserved/spent asserted for the partly cached, credentialless case (`tests/test_additive_deep.py:989-1009`).
- `no_network()` yields a fresh per-block list (`scripts/offline_guard.py:34-52`), tested across consecutive blocks (`tests/test_additive_deep.py:1012-1020`).
- The recorded cache-only remeasurement remains 56/107 with hints and 50/107 without; clip counts remain 1807/1228/1454 and 1807/1214/1674, with no Free loss and guard count zero (`docs/reviews/build-local-additive-deep.md:532-539`). This review made no provider calls.

## D — Scope

- Scoped diff/status are empty for `work/`, `data/corpus/`, `profiles/`, all named second-session files, and the golden file. The golden remains blob `316b6171c42f0772a9bce3066cf716491f22d181`.
- The main checkout’s protected-path status is also empty. Current deepscan newest timestamp remains `2026-09-27 18:43:51Z`, matching the report’s 19:43 local timestamp and its before/after listing claim.
- `git diff --check` is clean. Added-line and untracked-file scans found no credential, private key, personal absolute path, or other material unsuitable for a public repository.

## Required fixes

### Realistic

- P0/P1/P2: None.

### Adversarial

- P0/P1/P2: None.

## Follow-ups, not blockers

- P2: The new golden test catches JSON type/value drift, but it parses and canonically reserializes the produced file before comparing bytes (`tests/test_golden_local_free.py:101-108`). It therefore does not literally compare the produced raw serialization or line endings.
- P2: The crash test injects a SQLite abort rather than killing a separate process; the transaction structure nevertheless gives the required crash atomicity.

VERDICT: OK_TO_COMMIT

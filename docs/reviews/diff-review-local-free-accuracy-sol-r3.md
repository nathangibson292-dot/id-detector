## A — Replays

The round-two P1 is closed. `separate_playing` has no elapsed-time threshold: it requires a non-overlapping, `possible`-or-better different work wholly between each appearance; otherwise it returns false ([episodes.py:490](src/id_detector/fuse/episodes.py:490), [episodes.py:1312](src/id_detector/fuse/episodes.py:1312)).

The 150-second regression verifies both directions—two rows with the separator, one without it ([test_free_accuracy.py:800](tests/test_free_accuracy.py:800)). Restoring the 180-second floor produced only the first row, as recorded by the revert proof ([build-local-free-accuracy.md:846](docs/reviews/build-local-free-accuracy.md:846)).

The 23:00 Original Don row is an evidence-distinct replay of a truth work, not a new wrong work. The owner’s truth contains `Riko Dan, Y U QT – Original Don (Extended)` ([ground_truth.json:1](data/corpus/release-1/redo-of-best-set/ground_truth.json:1)); the uploader answer names it at 23:00 ([hints.jsonl:15](work/a5986744cf646ccc2e05a6665164cf35ac3015ea1266494f6cab593de4aaf526/99b8ceebb852dcbf767733380a27072feed1ce1b8f9af1eb72bd7f7ff983c4d0/hints/hints.jsonl:15)); a listed `Mears – Be My Lover` episode at 23:24–23:54 lies wholly before the engine-backed Original Don occurrence at 24:09–24:30 ([episodes.json:1]((scratch copy outside the repo))). Both Original Don rows map to truth row 10 and the same work ID ([work-match.json:1]((scratch copy outside the repo))).

## B — Read-only measurement

`load_index_read_only` catches missing/damaged indexes, rejects stale ones through `_usable`, and calls the in-memory builder directly—never `rebuild_index` or an atomic writer ([index.py:88](src/id_detector/present/index.py:88)). `media_map_read_only` completes legacy/incomplete entries by scanning source records without persistence ([index.py:120](src/id_detector/present/index.py:120)).

`make_run_list` resolves through that map and only writes its explicitly guarded external output ([make_run_list.py:97](scripts/make_run_list.py:97), [make_run_list.py:164](scripts/make_run_list.py:164)). `measure_refusion` enumerates read-only and writes solely into an OS temporary directory ([measure_refusion.py:74](scripts/measure_refusion.py:74)). Current-result loading takes only an in-process `RLock`, not a work-tree lock file ([bundles.py:35](src/id_detector/present/bundles.py:35), [bundles.py:563](src/id_detector/present/bundles.py:563)).

Normal product behavior is unchanged: analysis still calls `cached_media_dir`, whose unchanged path uses writable `load_index`/`rebuild_index` ([ingest.py:189](src/id_detector/ingest.py:189), [index.py:188](src/id_detector/present/index.py:188)); serve upkeep still re-fuses stale results in its background startup pass ([server.py:92](src/idea_web/server.py:92)).

Both regressions fingerprint every directory entry and every file’s size plus nanosecond mtime ([test_free_accuracy.py:584](tests/test_free_accuracy.py:584), [test_cost_preview.py:34](tests/test_cost_preview.py:34)). The recorded one-line reversion to `load_index` makes both fail on `index.json` ([build-local-free-accuracy.md:422](docs/reviews/build-local-free-accuracy.md:422)).

I independently compared the four inventories: each has 38,640 files and 6,642,184,643 bytes; their SHA-256 hashes are identical and the before/after three-field comparison has zero differences ([before.csv:1]((scratch copy outside the repo)), [after.csv:1]((scratch copy outside the repo))).

## C — The two INCOMPLETE reasons

1. The score explanation is correct. The generated real-cache run list contains seven uniformly `fusion:1` results, while the 164/218, 167/198, 63/63, 217-row artifact is uniformly `fusion:5` after re-fusing the same evidence. A uniform historical score is valid but clearly prefixed “Every run was decided by fusion:1”; mixed versions are refused, comparisons against fusion:5 are refused, and refusion prints an explicit stale-side warning ([score_corpus.py:1212](scripts/score_corpus.py:1212), [score_corpus.py:1516](scripts/score_corpus.py:1516), [measure_refusion.py:186](scripts/measure_refusion.py:186)). This is not a hidden scoring defect.

2. The money-resume failure is unrelated. The test’s outcome depends on retry/resume accounting and `primary_resolved`, and asserts only that the resumed run is not partial ([test_followup_money_resume.py:359](tests/test_followup_money_resume.py:359)). No money, retry, paid-clip, recognition, pipeline, service, or test file on that path changed. Fusion runs after recognition and cannot produce `partial/primary_not_achieved`; index and scorer code are not on this test’s path. The quiet 4/4 test passes and 22/22 file pass are therefore consistent with load-related flaking.

## D — Scope

The golden remains byte-identical at SHA-256 `78FF49D38F53E323DE0CC63542BFE133C121610BAFAA8057FAD2001C31973931` ([tracklist.json:1](tests/golden/local-free/tracklist.json:1)).

Status and diff contain no changes to `profiles/`, truth/corpus, second-session files, `tests/conftest.py`, `src/idea_web/**`, providers, `paid_clip.py`, `pipeline.py`, or `recognise.py`. `tests/test_phase0a_status.py` changes only exact fusion-version expectations, not CLI invocation ([test_phase0a_status.py:235](tests/test_phase0a_status.py:235)). No assertion is loosened, and no credential, generated owner data, or unsuitable public artifact is present.

## Required fixes

### Realistic

- P0/P1/P2: None.

### Adversarial

- P0/P1/P2: None.

## Follow-ups, not blockers

- None.

VERDICT: OK_TO_COMMIT

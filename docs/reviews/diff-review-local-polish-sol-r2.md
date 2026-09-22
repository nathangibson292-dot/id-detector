## A — Occurrence guard

The P1 is closed. Recognised plays are indexed per work and occurrence from actual audio groups ([episodes.py:776](src/id_detector/fuse/episodes.py:776)); an untimed hint attaches only when that map contains exactly one play ([episodes.py:787](src/id_detector/fuse/episodes.py:787)). No longest, earliest, or scoring fallback exists.

The guard runs before `supporting_hints`, flags, and evidence are constructed ([episodes.py:821](src/id_detector/fuse/episodes.py:821), [episodes.py:890](src/id_detector/fuse/episodes.py:890)); presentation merely consumes `hint_supported`, so downstream code cannot reattach the hint.

The regression creates two separated recogniser occurrences at 60–96 seconds and 3,000–3,012 seconds, then proves the untimed hint backs neither and only the timed occurrence publishes ([test_hint_corroboration.py:605](tests/test_hint_corroboration.py:605)). The report records that reverting only this guard fails that test ([build-local-polish.md:342](docs/reviews/build-local-polish.md:342)).

## B — Reversed range

The diagnosis is correct: at `e591971`, `_track_entry` independently combined a role-aware/best start with `best_end_ms`, so crossed single-window proof bounds already produced reversed rows. Hint attachment merely made one such pre-existing row visible.

The correction changes only crossed rows: `_display_bounds()` retains the former values unless `end_ms < start_ms`, then uses the evidence hull ([exports.py:160](src/id_detector/present/exports.py:160)). Previously well-formed rows are therefore unchanged.

All publishing paths are covered: flat and suppressed rows use `_track_entry`; collapsed rows retain their existing robust grouping hull ([grouping.py:148](src/id_detector/present/grouping.py:148), [grouping.py:384](src/id_detector/present/grouping.py:384)); track and ID-gap rows then pass one final projection-wide chronological invariant ([exports.py:518](src/id_detector/present/exports.py:518), [exports.py:595](src/id_detector/present/exports.py:595)). `flatten_tracklist` delegates to that canonical projection ([exports.py:693](src/id_detector/present/exports.py:693)). End-before-start is consequently excluded across the published projection, not just one export branch.

## C — Measurement

The evidence supports the corrected measurement. A read-only in-memory replay found 18 recognised-work untimed hints, 16 attached, exactly two `MPH — One Sixty` hints refused, eight episodes gaining support, and the affected mix moving 40 → 41 rows with only `The Bug — Jah War (feat. Flowdan)` added. Across the fourteen mixes, including the unchanged unrebuildable BENWAL artifact, rows are 386 → 387, matching the recorded table ([build-local-polish.md:380](docs/reviews/build-local-polish.md:380)).

Occurrence verification is genuinely timed: truth contains only the opening `One Sixty` occurrence and the 4,693,000–4,807,000 ms `Jah War` occurrence ([ground_truth.json:1](data/corpus/release-1/mph-youtube-set/ground_truth.json:1)). The replay produced `Jah War` at 4,761,000–4,773,000 ms, retained only opening `One Sixty`, and left its later recognised fragment hidden.

The report explicitly retracts the old inference, explains that the work matcher is time-blind, and replaces it with the occurrence-level conclusion ([build-local-polish.md:396](docs/reviews/build-local-polish.md:396)); the matcher itself confirms that time is never consulted ([score_corpus.py:528](scripts/score_corpus.py:528)).

## D — Follow-ups and scope

The progress regression now persists a queue snapshot, releases the failed claim, reloads through a replacement `run_once()`, advances its clock by 17 seconds, and verifies the 207-second carry and monotonic bar ([test_local_queue.py:154](tests/idea_web/test_local_queue.py:154), [test_local_queue.py:166](tests/idea_web/test_local_queue.py:166)). The report correctly states that `JobManager._execute()` overwrites `started_at` ([jobs.py:823](src/id_detector/webapp/jobs.py:823), [build-local-polish.md:367](docs/reviews/build-local-polish.md:367)).

`git status --short -uall` and scoped diffs confirm `data/corpus/`, `work/`, `profiles/`, the Local Free golden, and every named second-session file are untouched. Timestamp checks independently found zero post-cutoff files in both corpus trees and the 38,640-file owner work tree.

Money authority remains conservative: any proved Deep provenance still triggers refusal before re-fusion ([refusion.py:123](src/id_detector/refusion.py:123), [refusion.py:439](src/id_detector/refusion.py:439)). Modified pinned assertions are exact `fusion:3` → `fusion:4` updates or stronger additions. `git diff --check` is clean, and the added material contains no credential, private URL, personal absolute path, or other unsuitable public-repository content.

## Required fixes

### Realistic

- **P0:** None.
- **P1:** None.
- **P2:** None.

### Adversarial

- **P0:** None.
- **P1:** None.
- **P2:** None.

## Follow-ups, not blockers

- None. Both round-one follow-ups are closed.

VERDICT: OK_TO_COMMIT

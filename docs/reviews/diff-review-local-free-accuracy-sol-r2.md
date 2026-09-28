## A — Replays

The reviewer’s Testpress regression is fixed: the test produces rows at 1:35 and 9:55, and neutralising the fix back to `known_work_ids` leaves only the first row ([test_free_accuracy.py:680](tests/test_free_accuracy.py:680)).

The 180-second threshold is not safe. `separate_playing` rejects every gap below 180 seconds before considering the intervening track ([episodes.py:501](src/id_detector/fuse/episodes.py:501)). An offline case with Testpress plays 150 seconds apart and a confident different track wholly between produced only one Testpress row. That loses a genuine replay.

The other direction is guarded for ordinary continuous play: without an intervening track, the distant Testpress mentions remain one row ([test_free_accuracy.py:698](tests/test_free_accuracy.py:698)). A long crowd-only track continuing underneath a fully enclosed overlay can still be split; the available evidence cannot prove that the first track stopped.

## B — One-word titles and chaining

One-word changed-word tolerance is closed: exact/joined matching precedes the guard, while stem/phonetic matching requires at least two words ([identity.py:710](src/id_detector/fuse/identity.py:710)). Artist slips beside one-word titles are also rejected ([identity.py:876](src/id_detector/fuse/identity.py:876)). Dream/Dreams, Nite/Night, and Tomlison/Feel fail.

All nine genuine credits survive: seven use the established rule, FORZ4 uses `spaced`, and Feel Emotion uses `stem`; the scorer regression covers the latter two ([test_free_accuracy.py:667](tests/test_free_accuracy.py:667)).

Loose crowd links are resolved against fixed established components and added without iteration ([identity.py:1241](src/id_detector/fuse/identity.py:1241)). Anderson/Andersson/Anderon remain three works. A separate four-spelling path (`Anderson → Andersson → Anderssson → Andersssson`) produced four works and zero tolerant links, confirming no chaining.

## C — Trade-off and small fixes

Long Season correctly returns to three rows because its two ends meet only through the middle spelling ([test_free_accuracy.py:241](tests/test_free_accuracy.py:241)). This matches the fusion:4/pre-build state.

The Long Season assertion is in the new, untracked test file, so no pre-existing Long Season assertion was changed. No pre-existing assertion was loosened.

Contradictory run-list provenance is refused at [score_corpus.py:515](scripts/score_corpus.py:515), with direct coverage at [test_free_accuracy.py:726](tests/test_free_accuracy.py:726).

The 17:15 row is relabelled only for display via `_fuller_crowd_label`; identity membership is untouched ([exports.py:143](src/id_detector/present/exports.py:143), [test_free_accuracy.py:703](tests/test_free_accuracy.py:703)). It again shows the full `KETTAMA - ID (LET ME SEE U / ROK DA HOUSE!)` label and regains its scorer credit.

## D — Measurement and scope

The stored results are internally consistent: fusion:4 gives recall 164/218, work precision 168/208 = 80.8%, likely 63/64 = 98.4%, and 228 rows; fusion:5 gives 164/218, 167/198 = 84.3%, 63/63 = 100%, and 216 rows. Each artifact reports exactly one fusion version. The scorer uses the displayed label and `same_work_labels` ([score_corpus.py:433](scripts/score_corpus.py:433), [score_corpus.py:666](scripts/score_corpus.py:666)).

The only wrong row returned relative to the first-pass result is `Long Season Intro Edit - Mall Grab`; it was already listed at `361fe94`, so no wrong row is newly listed relative to the pre-build baseline.

The golden blob is unchanged on both sides (`316b6171c42f0772a9bce3066cf716491f22d181`). Status/diff show no changes under `profiles/`, truth/corpus, second-session files, `src/idea_web/**`, `paid_clip.py`, `pipeline.py`, `recognise.py`, or providers. No secret or unsuitable public artifact was found.

## Required fixes

### Realistic

- **P1:** Remove or replace the unconditional 180-second minimum. A genuine replay closer than three minutes, despite a confident different track wholly between, is currently suppressed at [episodes.py:508](src/id_detector/fuse/episodes.py:508).

### Adversarial

- None.

## Follow-ups, not blockers

- **P2:** A long crowd-only track continuing beneath a shorter listed overlay can be split into two rows.
- **P2:** The literal claim that no assertion from `361fe94` changed is inaccurate: the artistless scorer expectation changed from `{0: 0}` to the stricter `{}` at [test_score_corpus.py:906](tests/test_score_corpus.py:906). It was tightened, not loosened.

VERDICT: FIX_FIRST

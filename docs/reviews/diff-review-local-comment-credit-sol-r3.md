## A — rule

- YouTube supplies `parent_source_id` from threaded comments ([youtube.py:79](src/id_detector/hints/connectors/youtube.py:79), [youtube.py:121](src/id_detector/hints/connectors/youtube.py:121)). SoundCloud supplies permalink and waveform timestamp from its threaded fetch ([soundcloud.py:119](src/id_detector/hints/connectors/soundcloud.py:119), [soundcloud.py:161](src/id_detector/hints/connectors/soundcloud.py:161)); the parser requires one exact mention/timestamp match ([parse.py:650](src/id_detector/hints/parse.py:650)).
- Missing, unknown, duplicate, or scalar parent IDs safely resolve false; available non-ID parents disable the route ([parse.py:665](src/id_detector/hints/parse.py:665)). Properly spaced separators run first and remain independent ([parse.py:368](src/id_detector/hints/parse.py:368)).
- The absent-parent fallback is unsafe: its finite reaction list ([parse.py:99](src/id_detector/hints/parse.py:99)) misses comparable replies.
- Parent qualification is also not the same detector: `_is_id_question` broadens `is_track_question` to any standalone `ID` ([parse.py:201](src/id_detector/hints/parse.py:201), [parse.py:209](src/id_detector/hints/parse.py:209)).

## B — tests and measurement

- Direct probes confirm the three named reactions are rejected and the parented genuine answer is accepted, matching regressions at [test_stage4a_parser.py:304](tests/test_stage4a_parser.py:304) and [test_stage4a_parser.py:316](tests/test_stage4a_parser.py:316). Recorded revert proof shows all four fail when the pass-2 fix is removed ([build-local-comment-credit.md:461](docs/reviews/build-local-comment-credit.md:461)).
- Independent read-only comparison reproduced 14 media, 11 with comments, 1,150 unique comments, exactly eight changes, and zero changed existing properly-spaced splits; this matches [build-local-comment-credit.md:499](docs/reviews/build-local-comment-credit.md:499).
- The golden is absent from the diff, and pass 2 only added parser assertions; the recorded scope confirms the golden and protected trees unchanged ([build-local-comment-credit.md:609](docs/reviews/build-local-comment-credit.md:609)).

## Required fixes

### Realistic

- **P1:** Comparable reactions still become 9,000-confidence tracks when structure is absent: `@user nice one -mate`, `@user appreciate it -legend`, and `@user awesome -bro`.
- **P1:** `Love this ID` is treated as an ID question; beneath it, `@user nice one -mate` is likewise promoted. Reuse the actual question detector and add both regression classes.

### Adversarial

- **P0/P1/P2:** None.

## Follow-ups, not blockers

None.

VERDICT: FIX_FIRST

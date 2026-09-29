## A — rule

- Confirmed: the one-sided split is enabled only by `parent_is_track_question is True`; parent lookup requires one matching record and `is_track_question`, with timestamp equality when both timestamps exist ([parse.py:528](src/id_detector/hints/parse.py:528), [parse.py:650](src/id_detector/hints/parse.py:650)). No route-specific reaction/gratitude fallback remains.
- Independent probes found all four reactions identical to `67bee21` and unparsed without a parent; all remain unparsed beneath `Love this ID`. Properly spaced separators still precede the one-sided route ([parse.py:359](src/id_detector/hints/parse.py:359)); regressions cover the four strings ([test_stage4a_parser.py:267](tests/test_stage4a_parser.py:267)).
- Unknown, duplicate, wrong-connector and timestamp-mismatched parents failed closed without crashing. `Love this ID` remains false ([test_stage4a_parser.py:302](tests/test_stage4a_parser.py:302)).

## B — question detector consequences

- The shared detector is broadened at [parse.py:194](src/id_detector/hints/parse.py:194). The checksum-proven replay reproduced 1,150 comments, exactly 98 promotions, and zero network attempts. All 98 are genuine requests; none is praise/chat.
- Questions have no identity fields and are skipped by identity fusion ([identity.py:1180](src/id_detector/fuse/identity.py:1180)). Corroboration, contradiction and crowd-only rows accept answers/corrections, not questions ([episodes.py:437](src/id_detector/fuse/episodes.py:437), [episodes.py:525](src/id_detector/fuse/episodes.py:525), [episodes.py:1152](src/id_detector/fuse/episodes.py:1152)). Edge rules derive solely from unchanged episodes.
- Re-fusing every affected mix produced identical identities, episodes, listed IDs, badges, suppression, scores, Deep gap targets and paid-window selections. The scorer consumes those episode predictions ([scorer.py:900](src/id_detector/benchmark/scorer.py:900)); Deep targets only episode-coverage gaps ([additive.py:45](src/id_detector/additive.py:45)).
- Observable metadata changes are limited to +98 hint/question counts, one gap’s `n_hint_events`, and 16 additional `question_cluster` plan regions ([episodes.py:1447](src/id_detector/fuse/episodes.py:1447), [episodes.py:1601](src/id_detector/fuse/episodes.py:1601)). Current owner/default settings use zero rescan generations, so those plans are not executed ([base.py:115](src/id_detector/providers/base.py:115)). Progress remains a fixed one-unit hints phase and ETA uses phase time, not hint count ([pipeline.py:725](src/id_detector/pipeline.py:725), [jobs.py:73](src/id_detector/webapp/jobs.py:73)).

## C — gains and scope

- All eight gains remain: `winter vol2`, `Mall grab`, `mg`, both `metaphyscial/Metaphysical`, `effy`, `Freedom`, and `the days`; all parse as reported at [build-local-comment-credit.md:702](docs/reviews/build-local-comment-credit.md:702).
- All 310 existing properly spaced stored identities were unchanged. The golden is absent from the diff; the parser-test change is 85 additions and zero deletions, so no assertion was loosened.
- All three new tests pass current code and are documented failing under the pass-2 reversion ([build-local-comment-credit.md:658](docs/reviews/build-local-comment-credit.md:658), [build-local-comment-credit.md:666](docs/reviews/build-local-comment-credit.md:666)). Imports and full in-memory fusion completed; the scoped diff contains no secret.

## Required fixes

### Realistic

- P0: None.
- P1: None.
- P2: None.

### Adversarial

- P0: None.
- P1: None.
- P2: None.

## Follow-ups, not blockers

- If rescans are explicitly enabled later, the 16 new genuine-question regions will intentionally add Shazam work and could then affect subsequent rows or Deep gap targets; current owner settings keep them dormant.

VERDICT: OK_TO_COMMIT

## A — split

- Identity decisions use `_identity_candidate_label`: grouping/collapse at [grouping.py:312](src/id_detector/present/grouping.py:312), scorer filtering/matching at [score_corpus.py:790](scripts/score_corpus.py:790) and [score_corpus.py:1028](scripts/score_corpus.py:1028), additive comparison at [additive.py:202](src/id_detector/additive.py:202), and truth review at [truth_review.py:387](src/id_detector/truth_review.py:387).
- Display wording is overlaid only after grouping, de-duplication, same-artist lifting, suppression and overlap filtering at [exports.py:684](src/id_detector/present/exports.py:684), then consumed by exports and the page timeline at [page.py:1286](src/id_detector/present/page.py:1286).
- The only non-output caller receiving display-decorated rows is `measure_refusion.counts`; it reads only `len(...)` after identity-based grouping at [measure_refusion.py:50](scripts/measure_refusion.py:50). Wording cannot affect that count.

## B — regression and measurement

- The 17:15 display/identity test and both placeholder regressions pass: 3 passed. The new regression pins two displayed rows, identities `artist - a`/`artist - b`, and independent truth assignments at [test_free_accuracy.py:800](tests/test_free_accuracy.py:800). Substituting the round-one display resolver into grouping reproduces one collapsed row; the recorded revert failure is at [build-local-free-accuracy.md:623](docs/reviews/build-local-free-accuracy.md:623).
- Stored measurement confirms 164/218 recall, 166/198 work precision, 63/63 `likely`, 217 rows, and zero network attempts at [build-local-free-accuracy.md:639](docs/reviews/build-local-free-accuracy.md:639).
- Exactly one prediction changes: Mall Grab episode `2271…` goes from the placeholder label credited to truth 5 to unmatched `kettama - let me see`; the other six work-match files are byte-identical. Pass-4/pass-5 `rows.json` is byte-identical, SHA-256 `3FB090…BA`.
- No test was loosened. The 17:15 assertion now separately pins the fuller display, real identity, existing real work, and absence of a placeholder node at [test_free_accuracy.py:751](tests/test_free_accuracy.py:751); no score fixture expectation changed.

## C — scope

- The Local Free golden is byte-identical to main, SHA-256 `78FF49D38F53E323DE0CC63542BFE133C121610BAFAA8057FAD2001C31973931`. Protected-path status is empty.
- No credential, token, private key, owner data, or other secret was added.
- One public accuracy claim remains stale: [STATUS.md:170](docs/STATUS.md:170) and [reviews/README.md:117](docs/reviews/README.md:117) still say 84.3%, the old 167/198 result, instead of 83.8% for 166/198.

## Required fixes

### Realistic

- **P0:** None.
- **P1:** Correct the two public headline precision claims from 84.3% to 83.8%; otherwise this accuracy release overstates its measured result.
- **P2:** None.

### Adversarial

- **P0/P1/P2:** None.

## Follow-ups, not blockers

- None.

VERDICT: FIX_FIRST

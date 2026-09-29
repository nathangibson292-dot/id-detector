## A — Collapsed rows

Confirmed. Group metadata uses `any(hint_supported)` and `all(hint_only)` across every member, including de-duplicated alternatives ([grouping.py:396](src/id_detector/present/grouping.py:396)). It replaces provenance only after visibility judging ([exports.py:676](src/id_detector/present/exports.py:676)), leaving grouping, primary selection, badges, order, scoring, and the short-row floor unchanged.

The canonical flags feed the card, row tag, timeline, Markdown, and CUE ([page.py:454](src/id_detector/present/page.py:454), [page.py:1331](src/id_detector/present/page.py:1331), [exports.py:910](src/id_detector/present/exports.py:910), [exports.py:963](src/id_detector/present/exports.py:963), [exports.py:1089](src/id_detector/present/exports.py:1089)).

The collapsed-row regression covers all requested surfaces, mixed provenance, and unchanged visibility ([test_stage7_page.py:439](tests/test_stage7_page.py:439)). Its recorded round-one failure is present at [build-local-comment-credit.md:271](docs/reviews/build-local-comment-credit.md:271).

## B — Parser

The single-dash lookarounds reject both doubled-dash orientations ([parse.py:46](src/id_detector/hints/parse.py:46)); one-sided splitting requires a leading mention and passes through the request veto ([parse.py:474](src/id_detector/hints/parse.py:474), [parse.py:524](src/id_detector/hints/parse.py:524)). Properly spaced `Artist - Title` remains on the unchanged earlier route ([parse.py:355](src/id_detector/hints/parse.py:355)).

The four round-one examples behave correctly. Both request orientations are rejected; doubled dashes remain unsplit; mention-led prose containing `pre-order` is unchanged from `67bee21`. The former `I|we` special case is subsumed by the general `need|want|love` veto ([parse.py:93](src/id_detector/hints/parse.py:93)).

However, `@user cheers -legend` is newly parsed as artist `cheers`, title `legend`, confidence 9,000. A leading mention alone does not establish that the reply is an answer, and `cheers` is absent from the request/reaction guard.

## C — Measurement and scope

An independent read-only comparison of all 14 indexed media entries and 1,150 unique stored comments found exactly eight changes versus `67bee21`; all eight are answers, and zero existing answers changed split. This agrees with [build-local-comment-credit.md:339](docs/reviews/build-local-comment-credit.md:339). No provider or network path was invoked.

The zero-comment case again broadly rejects `"from comments"` ([test_projection.py:503](tests/test_projection.py:503)). No assertion was loosened.

The golden Local Free file, `theme.py`, playlist-owned paths, `README.md`, `idea.cmd`, `work/`, `data/`, and deepscan are absent from the diff/status. `git diff --check` is clean; no credential or public-repository issue was found.

## Required fixes

### Realistic

- **P1:** Reject gratitude/reaction replies such as `@user cheers -legend` from the one-sided-dash route and add this regression. It currently turns a realistic non-answer into a high-confidence track.

### Adversarial

- **P0/P1/P2:** None.

## Follow-ups, not blockers

None.

VERDICT: FIX_FIRST

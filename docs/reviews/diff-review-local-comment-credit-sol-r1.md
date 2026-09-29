## A — Card

The normal path correctly deduplicates `audio_work_key()` values from displayed projection rows. Hidden rows are absent from exported `tracklist.json`, so the card cannot count them. Replays, duplicate answers, and comment-only/audio-backed occurrences are counted once per work; the two subsets independently preserve the intended Original Don result.

Zero produces no note, all-comment-only produces `N from comments only`, and mixed results produce `N named in comments · M only there`. FastAPI delegates to the legacy card renderer, so both use the same function.

The reported changes follow from the counts:

- Effy/Kettama: `+1` → `10 named · 1 only`.
- IPC/Flowdan: `+2` → `7 named · 2 only`.
- IPC/Skream: `+2` → `10 named · 2 only`.
- Kettama/Nia Archives: none → `3 named in comments`.

There is one blocking aggregation defect: collapsed display tracks inherit flags only from `track.primary` in [exports.py](src/id_detector/present/exports.py:590). A comment-backed member can collapse beneath a stronger unbacked primary. I reproduced this with the existing page fixture: episode 2 was `hint_supported`, but after collapsing its displayed row reported `hint_supported=False`. The card then omits a work the comments named.

## B — Row marker and page

Outside that collapsed-row defect, the change is display-only: it does not modify fusion, badges, tiers, confidence, grouping, ordering, or scorer inputs. `hint_only` rows are explicitly excluded from both markers.

`PAGE_VERSION = 26` correctly advances 25. `theme.py` is byte-identical to `67bee21`; styling remains in `page.py` and follows existing inline-positioned timeline elements and `.hint` tags. The row contains visible text, while the decorative timeline is `aria-hidden`, so meaning is not conveyed by colour alone.

The same collapsed-primary defect also suppresses the row tag, timeline marker, Markdown note, and CUE note because [page.py](src/id_detector/present/page.py:1332) consults only the primary episode.

The recorded JavaScript gate passed all 53 inline scripts across 22 renders plus `app.js`.

## C — Exports

Markdown’s `COMMENT CONFIRMED` is clear. `REM COMMENT_CONFIRMED` appears within the relevant `TRACK` block after `PERFORMER` and before `INDEX 01`, which is valid CUE placement and harmless to DJ software that ignores unknown remarks.

JSON remains compatible, and no in-repository consumer or pinned test depends on the former `+HINT` text.

## D — Parser

`Jay-Z - Title`, `Jay-Z- Title`, titles such as `Up-Tempo`, and the named hyphenated artists work correctly. Leading `- ` bullets are stripped before parsing. Dates, compact time ranges, and arbitrary URLs retain pre-existing odd low-confidence behavior; allowed provider URLs still become pointers first.

The cache measurement is reproducible: 1,150 distinct comment records, eight changed parses. All eight are plausible answers, and none was previously an `answer` with a changed split.

The new rule nevertheless promotes realistic non-answers:

- `Thanks -for uploading` → artist `Thanks`, title `for uploading`.
- `Can anyone tell me Odd Mob- Superstylin remix ID` → a 9000-confidence answer.
- `Great set-- loved it` bypasses the doubled-dash guard from the opposite spacing direction and is upgraded from a low-confidence unsplit answer to a 9000-confidence artist/title pair.

`I NEED that Odd Mob- Superstylin remix ID` is rejected only by the narrow `I|we + need|want|love` pattern in [parse.py](src/id_detector/hints/parse.py:350), not a general request/prose rule.

## E — Reach and scope

The reach statement is correct. Version-26 page regeneration and card reading use existing stored flags on the next `idea serve`; re-fusion loads checksum-proven stored `HintRecord`s, so the eight newly parsed answers require a re-scan.

The golden file, playlist paths, `README.md`, `idea.cmd`, `theme.py`, and `tests/test_playlists.py` are byte-identical to `67bee21`. `work/`, hint data, and deepscan have no writes after the brief began. No secret or unsuitable repository content was added.

The reported memory failure subsequently passed alone (`1 passed`) and in the clean shard rerun (`552 passed`). One assertion was narrowed, noted below.

## Required fixes

### Realistic

- **P1:** Preserve comment provenance across every member of a collapsed display track using display-only metadata; do not change visibility, scoring, badges, or grouping.
- **P1:** Replace the narrow one-sided-dash prose/request guard with a structural rule covering generic requests and both doubled-dash orientations. Add regression cases for the examples above.

### Adversarial

- None beyond the realistic blockers.

## Follow-ups, not blockers

- **P2:** [test_projection.py](tests/test_projection.py:503) weakened the zero-comment assertion: it no longer rejects legacy `+N from comments`. Restore the broader absence check.
- **P2:** Compact dates, arbitrary URLs, and blank compact time-range units remain pre-existing parser debt.

VERDICT: FIX_FIRST

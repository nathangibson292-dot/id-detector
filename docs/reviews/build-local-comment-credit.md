# Build review — local comment credit

## Outcome

- Library cards now count every distinct displayed work named by a comment, whether the row is
  comment-only or audio-backed. The wording is `N named in comments · M only there`; if every
  named work is comment-only it contracts to `N from comments only`, and if none is named it is
  absent. The legacy server and the FastAPI `idea serve` path use the same renderer.
- The saved-page hero uses the same credit. A `hint_supported` row (but never a `hint_only` row)
  now says `confirmed in comments` in the row and carries a `comment ✓` timeline marker. Its
  confidence badge, ordering, grouping, and count are unchanged.
- JSON already carries `hint_supported`; Markdown now says `COMMENT CONFIRMED`, and CUE adds
  `REM COMMENT_CONFIRMED`. `PAGE_VERSION` is 26, so saved pages refresh on startup.
- The parser accepts `Artist -Title` and `Artist- Title` while retaining `Jay-Z`, `Rok-da-House`,
  `Lo-Fi`, `Pre-Order`, and `X-Press 2` as whole fields. Doubled-dash prose and requests such as
  `I NEED that Odd Mob- Superstylin remix ID` are not promoted to answers.
- `src/id_detector/present/theme.py`, playlist-owned paths, `README.md`, `idea.cmd`, the real
  `work/`, the raw hint cache, and `tests/golden/local-free/tracklist.json` were unchanged.

## Four SET mixes — current stored card to current in-memory re-fusion

The measurement rebuilt fusion 5 from each result's checksum-proven stored inputs with the default
local configuration, entirely in memory under `scripts.offline_guard.no_network`. It made zero
network attempts and wrote nothing to `work/`. The owner-observed Interplanetary Criminal/Skream
card was previously 25 tracks; the current cached result supplied for this build is already 26.

### SET — Effy, Kettama, Joy Orbison, Flowdan, Diffrent, Tommy Holohan Skream

- Before: `22 tracks · mostly likely · +1 from comments`
- After: `22 tracks · mostly likely · 10 named in comments · 1 only there`
- Comment-backed page rows:
  `0:00 Skream — Devon Analogue Raver`;
  `2:45 DJ IP & Pegassi — Wanna Feel`;
  `6:30 WOLTERS — WILDIN' 125`;
  `15:12 Bicep — CHROMA 004 ROLA`;
  `17:36 Tibasko — The Limit`;
  `23:27 Champion & Bushbaby — We Multiply (feat. Killa P)`;
  `24:57 Dominus, Daffy & Riko Dan — Warlord (Soul Mass Transit System Remix)`;
  `30:39 Efan & Dizzee Rascal — Filthy Bassline`;
  `36:03 Effy — CLUBGRLS`.

### SET — Interplanetary Criminal, Flowdan, MPH, Kwengface, Bushbaby

- Before: `31 tracks · mostly possible · +2 from comments`
- After: `31 tracks · mostly possible · 7 named in comments · 2 only there`
- Comment-backed page rows:
  `4:39 Bakey — Senses`;
  `11:00 Becking — Tella Soundboi`;
  `14:00 Bushbaby & Eloq — Breaka Breaka`;
  `32:00 Gentlemens Club & MPH — GOOD 4 U`;
  `40:51 MPH — Raw`.

### SET — Interplanetary Criminal, Skream, Kettama, Notion, Effy, Skin on Skin

- Before: `26 tracks · mostly possible · +2 from comments`
- After: `26 tracks · mostly possible · 10 named in comments · 2 only there`
- Comment-backed page rows:
  `0:00 Sam Alfred — Feel The Friction`;
  `8:18 NOTION & Charlotte Plank — TEMPORARY FRIENDS`;
  `9:39 Bklava & bullet tooth — Makes Me (Wanna Move)`;
  `11:09 Malugi — Baby`;
  `14:54 ATW, Interplanetary Criminal & Main Phase — 100%`;
  `24:12 Y U QT — Original Don (feat. Riko Dan)`;
  `26:18 Flowdan — Shell a Verse`;
  `29:09 Effy — RUNNING`;
  `32:27 Overmono — So U Kno`.

### SET Kettama, Nia Archives, Interplanetary Criminal, Sammy Virji, Chase & Status, Joy Orbison

- Before: `41 tracks · mostly possible`
- After: `41 tracks · mostly possible · 3 named in comments`
- Comment-backed page rows:
  `34:15 bullet tooth — Move Your Body (feat. Xpansions)`;
  `36:48 Skeptic & Sophia Violet — Want Me`;
  `1:02:54 NOTION, Unknown T & D38 — TENTEN`.

## Raw-comment parser measurement

All 14 media entries in the owner's current `work/index.json` were included. Eleven have stored raw
SoundCloud/YouTube comment inputs and three have none (Speed Garage & Bassline Mix 2025, BASSLINE /
UK BASS / SPEED GARAGE MIX, and DJ Three 60 min Boiler Room mix). Across the 1,150 unique raw
comments, exactly these eight parse differently. Every one is a plausible track answer; none is
junk. Old `[]` means the old parser emitted no unit.

1. `@user-480750305 effy -stone x next hype on bandcamp ( I’ve edited an extended version of it I’m happy to send if you dm me)`
   — `[]` → answer `effy` / `stone x next hype on bandcamp`.
2. `@user-915363248 \n\nMall grab- tremors`
   — `[]` → answer `Mall grab` / `tremors`.
3. `@user-437725651 winter vol2- mg unreleased`
   — keyword with unsplit title → answer `winter vol2` / `mg unreleased`.
4. `@gemma-sommerville metaphyscial- Mall Grab`
   — `[]` → answer `metaphyscial` / `Mall Grab`.
5. `@jacobmi11er: mg- 1ofthozedaze`
   — `[]` → answer `mg` / `1ofthozedaze`.
6. `@sharon-du-pon Metaphysical- Mall Grab`
   — `[]` → answer `Metaphysical` / `Mall Grab`.
7. `@jeroen-van-duin-2 Freedom -Jamo (unreleased)`
   — keyword with unsplit title → answer `Freedom` / `Jamo (unreleased)`.
8. `@user-184780529 the days -notion remix`
   — `[]` → answer `the days` / `notion remix`.

The measurement ended `NETWORK_ATTEMPTS=0`. The display/card changes reach existing saved mixes on
the next `idea serve` startup through re-fusion/page refresh. The two newly readable answers do not:
hints were parsed at scan time, and re-fusion consumes stored `HintRecord`s, so those answers appear
only after a re-scan. No re-parse path was added.

## Tests and revert proof

Focused implementation run: `119 passed in 12.22s` across the parser, page, projection, and export files;
the page-version/refusion group separately reported `57 passed, 5 warnings`.

For the requested revert proof, only the four production edits were temporarily stashed while the
new tests stayed present. The exact run reported:

```text
FFFF                                                                     [100%]
FAILED tests/test_stage4a_parser.py::test_a_dash_spaced_on_only_one_side_separates_without_splitting_words_or_junk
FAILED tests/test_projection.py::test_comment_credit_counts_distinct_works_not_rows
FAILED tests/test_stage7_page.py::test_comment_backing_is_marked_on_the_track_row_and_timeline
FAILED tests/test_stage7_page.py::test_comment_confirmation_reaches_json_markdown_and_cue_exports
4 failed in 2.48s
```

After restoring the implementation, the identical four-node run reported:

```text
....                                                                     [100%]
4 passed in 1.27s
```

## Full foreground suite

Collection:

```text
2206/2303 tests collected (97 deselected) in 11.30s
```

The four final green shard results account for every selected test:

```text
tests/idea_web
395 passed, 1 warning in 473.78s (0:07:53)

root test_a through test_f
309 passed, 1 warning in 461.23s (0:07:41)

root test_g through test_p (clean rerun)
552 passed, 1 warning in 356.74s (0:05:56)

root test_q through test_z
948 passed, 2 skipped, 97 deselected, 1 warning in 684.65s (0:11:24)
```

Totals: `395 + 309 + 552 + 948 + 2 skipped = 2206` selected tests.

The first `g–p` run encountered a machine-level Windows `0x8007000e` warning and one consequent
`MemoryError` while hashing generated HTML:

```text
FAILED tests/test_phase0b_audd.py::test_the_recipe_runs_four_clips_at_once - MemoryError
1 failed, 551 passed, 1 warning in 874.16s (0:14:34)
```

The exact test immediately passed alone, then the whole shard passed as shown above:

```text
1 passed, 1 warning in 13.09s
552 passed, 1 warning in 356.74s (0:05:56)
```

None of the three failures documented as load-sensitive in the brief occurred.

## Required gates

```text
$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
445 files already formatted

$ uv run python scripts/audit_fixtures.py
audited 564 files
fixture audit passed

$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

`git diff --check` produced no output. The final `git diff --stat` and `git status --short` are
pasted below; as usual, the untracked review file itself is visible in status but absent from diff
stat until the orchestrator stages it.

```text
$ git diff --stat
 src/id_detector/hints/parse.py        | 17 ++++++++++
 src/id_detector/present/exports.py    | 33 ++++++++++++++++--
 src/id_detector/present/page.py       | 48 +++++++++++++++++++++++---
 src/id_detector/present/server.py     | 12 +++----
 tests/fixtures/present/crowd.json     |  4 ++-
 tests/idea_web/test_refusion_money.py |  4 +--
 tests/test_projection.py              | 64 +++++++++++++++++++++++++++++++----
 tests/test_refusion.py                |  2 +-
 tests/test_stage4a_parser.py          | 26 ++++++++++++++
 tests/test_stage7_page.py             | 50 ++++++++++++++++++++++++++-
 10 files changed, 235 insertions(+), 25 deletions(-)

$ git status --short
 M src/id_detector/hints/parse.py
 M src/id_detector/present/exports.py
 M src/id_detector/present/page.py
 M src/id_detector/present/server.py
 M tests/fixtures/present/crowd.json
 M tests/idea_web/test_refusion_money.py
 M tests/test_projection.py
 M tests/test_refusion.py
 M tests/test_stage4a_parser.py
 M tests/test_stage7_page.py
?? docs/reviews/build-local-comment-credit.md
```

BUILD: COMPLETE

## Fix pass 1

### Collapsed rows keep comment credit

`group_display_tracks` now carries two presentation-only facts calculated from every member of a
collapsed group, including repeated same-candidate episodes that are deliberately de-duplicated out
of the visible alternatives: whether any member is `hint_supported`, and whether every member is
`hint_only`. `exports.py` retains the primary episode's original flags until after suppression and
the short-row floor have been judged, then replaces only the displayed row's public provenance
flags. This ordering is what keeps the change display-only: inherited credit cannot make a short
row visible, alter a badge, select a primary, change grouping/order, or reach a scorer input.

The page timeline now reads those flags from the canonical projected row instead of looking back at
the primary episode. The card, row tag, timeline marker, JSON, Markdown and CUE therefore agree.
`hint_only` is true only when every member is comment-only; a group containing any audio-backed
member is not described as “comments only.”

`test_collapsed_row_preserves_comment_confirmation_from_any_member` uses the reviewer's existing
page fixture shape: episode 2 is comment-backed beneath the stronger unbacked episode-1 primary.
It checks the row tag, timeline marker, page credit, projected JSON flag, Markdown note, CUE note and
card-summary count. It also checks that a comment-only primary plus an audio-backed member is not
comment-only, and that inherited support does not lift an `unclear` short row through the visibility
floor.

### One-sided dash is reply-shaped evidence

A one-sided dash is now an artist/title separator only when the whole comment begins with an
`@mention` (including an answer placed on a following line). Question/request language such as `?`,
`ID`, `anyone`, `tell me`, `what is`, `need`, `want`, `love`, `looking for` and equivalent direct
requests vetoes this weak separator. The dash regexp requires one dash, so both `-- ` and ` --`
orientations remain unsplit. The properly spaced `Artist - Title` route is still evaluated first and
is byte-for-byte unchanged.

The parser regression covers both one-sided orientations, a genuine mention-led reply, a
mention-then-newline reply, all five named hyphenated words, the three review examples, the `I NEED`
example, a request that itself begins with a mention, and both doubled-dash orientations. `Great
set-- loved it` retains its pre-existing low-confidence unsplit parse; it is no longer promoted to a
9,000-confidence pair.

### Restored zero-credit assertion

The zero-comment projection case once again asserts the broad `"from comments" not in card`, so it
rejects the legacy `+N from comments` wording as well as the new all-comment-only wording.

### Revert proofs

The two new regression functions were installed before their production fixes and run against the
reviewed code. Both failed at the intended first missing behaviour:

```text
FF.................                                                      [100%]
FAILED tests/test_stage4a_parser.py::test_a_dash_spaced_on_only_one_side_separates_without_splitting_words_or_junk
FAILED tests/test_stage7_page.py::test_collapsed_row_preserves_comment_confirmation_from_any_member
```

The parser failed because `Thanks -for uploading` was promoted to `Thanks` / `for uploading` at
confidence 9,000. The collapsed-row test failed because the episode-1 primary's row did not contain
`confirmed in comments`. After the fixes, the full parser/page/projection/collapse focus passed:

```text
130 passed in 18.48s
```

Thus each new regression function fails with its corresponding fix absent. The restored P2 check is
an assertion strengthening inside the existing zero-comment parameterized test, not a new test.

### Four SET mixes re-measured

The four results were again rebuilt in memory from their checksum-proven stored fusion inputs under
`scripts.offline_guard.no_network`, using the local configuration and writing nothing to `work/`.
No current SET has a marked row whose comment credit is inherited from a collapsed non-primary
member (`INHERITED=0` for all four), so every card and marked-row list is unchanged from the original
build report.

#### SET — Effy, Kettama, Joy Orbison, Flowdan, Diffrent, Tommy Holohan Skream

- Before: `22 tracks · mostly likely · +1 from comments`
- After: `22 tracks · mostly likely · 10 named in comments · 1 only there`
- Marked rows: `0:00 Skream — Devon Analogue Raver`; `2:45 DJ IP & Pegassi — Wanna Feel`;
  `6:30 WOLTERS — WILDIN' 125`; `15:12 Bicep — CHROMA 004 ROLA`; `17:36 Tibasko — The
  Limit`; `23:27 Champion & Bushbaby — We Multiply (feat. Killa P)`; `24:57 Dominus, Daffy &
  Riko Dan — Warlord (Soul Mass Transit System Remix)`; `30:39 Efan & Dizzee Rascal — Filthy
  Bassline`; `36:03 Effy — CLUBGRLS`.

#### SET — Interplanetary Criminal, Flowdan, MPH, Kwengface, Bushbaby

- Before: `31 tracks · mostly possible · +2 from comments`
- After: `31 tracks · mostly possible · 7 named in comments · 2 only there`
- Marked rows: `4:39 Bakey — Senses`; `11:00 Becking — Tella Soundboi`; `14:00 Bushbaby & Eloq
  — Breaka Breaka`; `32:00 Gentlemens Club & MPH — GOOD 4 U`; `40:51 MPH — Raw`.

#### SET — Interplanetary Criminal, Skream, Kettama, Notion, Effy, Skin on Skin

- Before: `26 tracks · mostly possible · +2 from comments`
- After: `26 tracks · mostly possible · 10 named in comments · 2 only there`
- Marked rows: `0:00 Sam Alfred — Feel The Friction`; `8:18 NOTION & Charlotte Plank —
  TEMPORARY FRIENDS`; `9:39 Bklava & bullet tooth — Makes Me (Wanna Move)`; `11:09 Malugi —
  Baby`; `14:54 ATW, Interplanetary Criminal & Main Phase — 100%`; `24:12 Y U QT — Original Don
  (feat. Riko Dan)`; `26:18 Flowdan — Shell a Verse`; `29:09 Effy — RUNNING`; `32:27 Overmono —
  So U Kno`.

#### SET Kettama, Nia Archives, Interplanetary Criminal, Sammy Virji, Chase & Status, Joy Orbison

- Before: `41 tracks · mostly possible`
- After: `41 tracks · mostly possible · 3 named in comments`
- Marked rows: `34:15 bullet tooth — Move Your Body (feat. Xpansions)`; `36:48 Skeptic & Sophia
  Violet — Want Me`; `1:02:54 NOTION, Unknown T & D38 — TENTEN`.

The display/card changes still reach saved mixes at the next `idea serve` startup. Parser output is
stored at scan time, so the eight newly readable answers still require a re-scan; no re-parse path
was added.

### Parser measurement re-run

The re-run covered the same 14 indexed media entries and 1,150 distinct stored raw SoundCloud and
YouTube comments. Eleven mixes have comment inputs; the same three have none. Exactly the same eight
comments differ from `67bee21`, all are mention-led replies, all are plausible answers, and no
existing answer changes its split:

1. `@user-480750305 effy -stone x next hype on bandcamp ( I’ve edited an extended version of it I’m happy to send if you dm me)`
   — `[]` → answer `effy` / `stone x next hype on bandcamp` (9,000).
2. `@user-915363248 \n\nMall grab- tremors`
   — `[]` → answer `Mall grab` / `tremors` (9,000).
3. `@user-437725651 winter vol2- mg unreleased`
   — keyword with unsplit title → answer `winter vol2` / `mg unreleased` (9,000).
4. `@gemma-sommerville metaphyscial- Mall Grab`
   — `[]` → answer `metaphyscial` / `Mall Grab` (9,000).
5. `@jacobmi11er: mg- 1ofthozedaze`
   — `[]` → answer `mg` / `1ofthozedaze` (9,000).
6. `@sharon-du-pon Metaphysical- Mall Grab`
   — `[]` → answer `Metaphysical` / `Mall Grab` (9,000).
7. `@jeroen-van-duin-2 Freedom -Jamo (unreleased)`
   — keyword with unsplit title → answer `Freedom` / `Jamo (unreleased)` (9,000).
8. `@user-184780529 the days -notion remix`
   — `[]` → answer `the days` / `notion remix` (9,000).

```text
MEDIA=14
COMMENTS=1150
CHANGES=8
NETWORK_ATTEMPTS=0
```

### Full foreground suite

Collection:

```text
2207/2304 tests collected (97 deselected) in 5.57s
```

The four foreground shards account for every selected test:

```text
tests/idea_web
395 passed, 1 warning in 405.62s (0:06:45)

root test_a through test_f
309 passed, 1 warning in 440.52s (0:07:20)

root test_g through test_p
552 passed, 1 warning in 344.61s (0:05:44)

root test_q through test_z
949 passed, 2 skipped, 97 deselected, 1 warning in 721.56s (0:12:01)
```

Totals: `395 + 309 + 552 + 949 + 2 skipped = 2207` selected tests. All four shards passed on
their first run. None of the three documented load-sensitive tests failed.

### Required gates and final status

```text
$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
445 files already formatted

$ uv run python scripts/audit_fixtures.py
audited 564 files
fixture audit passed

$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js

$ git diff --check
(no output)

$ git status --short
 M src/id_detector/hints/parse.py
 M src/id_detector/present/exports.py
 M src/id_detector/present/grouping.py
 M src/id_detector/present/page.py
 M src/id_detector/present/server.py
 M tests/fixtures/present/crowd.json
 M tests/idea_web/test_refusion_money.py
 M tests/test_projection.py
 M tests/test_refusion.py
 M tests/test_stage4a_parser.py
 M tests/test_stage7_page.py
?? docs/reviews/build-local-comment-credit.md
```

`src/id_detector/present/theme.py`, playlist-owned paths, `tests/test_playlists.py`, `README.md`,
`idea.cmd`, the golden Local Free file, the real `work/`, the raw hint cache and deepscan remain
untouched.

BUILD: COMPLETE

## Fix pass 2

### Structural rule and fallback

The raw SoundCloud records do not expose a thread ID or parent-comment ID. They do expose the
comment timestamp and the comment author's permalink, while a reply begins with the parent's
`@permalink`. The `HintInput` contract also already carries `parent_source_id` for connectors such
as YouTube. The one-sided-dash route now uses both forms of structural evidence:

- when `parent_source_id` is present, its unique parent must be an ID question (and must have the
  same timestamp when both records have one);
- for SoundCloud, the leading mention must resolve to exactly one comment by that permalink at the
  identical waveform timestamp, and that comment must ask for an ID;
- when no parent can be resolved because the provider supplied no thread structure, the previous
  mention-led fallback remains but now vetoes gratitude/reaction openings: `cheers`, `thank(s)`,
  `ty`, `legend`, `glad you like it`, and `love this`, as well as the existing request veto.

An available non-ID parent explicitly disables the weak separator. A structurally parented answer
does not need a redundant leading mention, so `Example Artist -Signal Path` is accepted beneath
`What's this tune?`. The ordinary spaced `Artist - Title` route is still evaluated before this rule
and is unchanged.

All eight stored gains have a unique exact SoundCloud parent match. Their parents are, in corpus
order: `What's this tune? ID plz`, `I need the id`, `Id???`, `Id?`, `Id please?`, `Track ID`, `ID?`,
and `Tuuuuune !!! Id???`. Thus the structural rule costs none of the genuine gains.

### Regressions and revert proof

The parser regression adds the three required reaction replies and a genuine one-sided answer with
an explicit ID-question parent. The same structural test also proves that a mention-led one-sided
pair beneath an available non-ID parent is refused. Each newly collected test failed before the
production fix was installed:

```text
$ uv run pytest -q tests/test_stage4a_parser.py -k "reaction_reply or one_sided_dash_answer_is_accepted"
FFFF                                                                     [100%]
FAILED tests/test_stage4a_parser.py::test_a_reaction_reply_is_not_promoted_by_a_one_sided_dash[@user cheers -legend]
FAILED tests/test_stage4a_parser.py::test_a_reaction_reply_is_not_promoted_by_a_one_sided_dash[@user thanks -mate]
FAILED tests/test_stage4a_parser.py::test_a_reaction_reply_is_not_promoted_by_a_one_sided_dash[@user glad you like it -bro]
FAILED tests/test_stage4a_parser.py::test_a_one_sided_dash_answer_is_accepted_under_an_id_question
4 failed, 77 deselected in 0.74s
```

The first three failures were the unwanted 9,000-confidence artist/title pairs. The fourth failed
with `StopIteration` because the unmentioned but structurally parented genuine answer was not
parsed. With the fix present:

```text
$ uv run pytest -q tests/test_stage4a_parser.py -k "reaction_reply or one_sided_dash_answer_is_accepted or dash_spaced_on_only_one_side"
.....                                                                    [100%]
5 passed, 76 deselected in 0.28s

$ uv run pytest -q tests/test_stage4a_parser.py tests/test_stage4a_connectors.py tests/test_stage4a_relations_fusion.py
........................................................................ [ 66%]
....................................                                     [100%]
108 passed in 6.14s

$ uv run pytest -q tests/test_cache_safety.py tests/test_stage4a_pipeline.py
............                                                             [100%]
12 passed, 1 warning in 14.22s
```

### Stored-comment measurement re-run

The comparison again read all 14 entries in the owner's current `work/index.json`, selected the
checksum-referenced cached SoundCloud/YouTube connector results, de-duplicated records by connector
and source ID, and compared the current parser to `67bee21`. It ran under
`scripts.offline_guard.no_network`, wrote nothing to `work/` or `data/`, and removed its temporary
measurement harness afterward. Exactly eight of 1,150 comments change, all are genuine answers,
and no existing split changes:

1. `@user-915363248 \n\nMall grab- tremors`
   — `[]` → answer `Mall grab` / `tremors` (9,000).
2. `@user-437725651 winter vol2- mg unreleased`
   — keyword with unsplit title → answer `winter vol2` / `mg unreleased` (9,000).
3. `@gemma-sommerville metaphyscial- Mall Grab`
   — `[]` → answer `metaphyscial` / `Mall Grab` (9,000).
4. `@jacobmi11er: mg- 1ofthozedaze`
   — `[]` → answer `mg` / `1ofthozedaze` (9,000).
5. `@sharon-du-pon Metaphysical- Mall Grab`
   — `[]` → answer `Metaphysical` / `Mall Grab` (9,000).
6. `@user-480750305 effy -stone x next hype on bandcamp ( I’ve edited an extended version of it I’m happy to send if you dm me)`
   — `[]` → answer `effy` / `stone x next hype on bandcamp` (9,000).
7. `@jeroen-van-duin-2 Freedom -Jamo (unreleased)`
   — keyword with unsplit title → answer `Freedom` / `Jamo (unreleased)` (9,000).
8. `@user-184780529 the days -notion remix`
   — `[]` → answer `the days` / `notion remix` (9,000).

```text
MEDIA=14
COMMENTS=1150
CHANGES=8
NETWORK_ATTEMPTS=0
```

### Full foreground suite

Collection after the four new parameter instances:

```text
$ uv run pytest --collect-only -q
2211/2308 tests collected (97 deselected) in 3.93s
```

The four foreground shards account for every selected test. The web shard had one load-sensitive
GET/HEAD parity failure: the dynamic `/` page changed length between the two sequential requests.
The required isolated rerun passed immediately, so no production or test code was changed for it.

```text
$ uv run pytest -q tests/idea_web
FAILED tests/idea_web/test_parity.py::test_head_answers_carry_the_get_status_and_headers_on_every_route
1 failed, 394 passed, 1 warning in 434.57s (0:07:14)

$ uv run pytest -q tests/idea_web/test_parity.py::test_head_answers_carry_the_get_status_and_headers_on_every_route
.                                                                        [100%]
1 passed, 1 warning in 4.94s

$ uv run pytest -q <root test_a through test_f files>
........................................................................ [ 46%]
........................................................................ [ 93%]
.....................                                                    [100%]
309 passed, 1 warning in 460.80s (0:07:40)

$ uv run pytest -q <root test_g through test_p files>
........................................................................ [ 52%]
........................................................................ [ 91%]
................................................                         [100%]
552 passed, 1 warning in 435.81s (0:07:15)

$ uv run pytest -q <root test_q through test_z files>
........................................................................ [ 60%]
.....................................................s.................. [ 67%]
................................s....................................... [ 98%]
...................                                                      [100%]
953 passed, 2 skipped, 97 deselected, 1 warning in 771.08s (0:12:51)
```

Totals: `395 + 309 + 552 + 953 + 2 skipped = 2211` selected tests. Every selected test passed
across the shards plus the required isolated rerun.

### Required gates and final status

```text
$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
445 files already formatted

$ uv run python scripts/audit_fixtures.py
audited 564 files
fixture audit passed

$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js

$ git diff --check
(no output)

$ git status --short
 M src/id_detector/hints/parse.py
 M src/id_detector/present/exports.py
 M src/id_detector/present/grouping.py
 M src/id_detector/present/page.py
 M src/id_detector/present/server.py
 M tests/fixtures/present/crowd.json
 M tests/idea_web/test_refusion_money.py
 M tests/test_projection.py
 M tests/test_refusion.py
 M tests/test_stage4a_parser.py
 M tests/test_stage7_page.py
?? docs/reviews/build-local-comment-credit.md
```

No commit or branch was created. `theme.py`, playlists, `README.md`, `idea.cmd`, the golden,
`work/`, `data/`, and the deep-scan tree remain untouched.

BUILD: COMPLETE

## Fix pass 3

### Structural rule and shared question detector

The reaction/request fallback is gone. A dash spaced on exactly one side is considered an
artist/title separator only when the comment's parent resolves uniquely and
`is_track_question(parent.text)` is true:

- YouTube replies resolve through `parent_source_id` within the same connector. Missing, unknown,
  duplicate, and non-question parents resolve false; the already-confirmed timestamp equality
  check still applies when both parent and reply have timestamps.
- SoundCloud replies resolve through one exact leading `@author_permalink` match at the identical
  waveform timestamp. Missing, unknown, duplicate, and non-question matches resolve false.
- A scalar/standalone comment has no resolved parent and therefore receives the exact `67bee21`
  parsing behaviour. A leading mention is no longer evidence by itself.
- Properly spaced separators are still evaluated first in `_artist_title` and remain independent
  of this rule.

The pass-2 `_is_id_question` broadener was removed. Parent qualification now calls the project's
shared `is_track_question` detector directly. That shared detector was genuinely too narrow for
the owner's real questions, so it now recognises bare/question-shaped `ID` requests, `what's the
ID`, `need the ID`, punctuation-free `what's this tune`, and an elongated `tu+ne`. The regression
covers `ID?`, `Id??????`, `ID on this?`, `What's the ID here?`, `Track id?`, `Song id??`, `what's
this tune`, `Tuuuuune !!! Id???`, and `I need the id`. `Love this ID` remains false, as does the
existing `Can I see your ID?` negative.

All eight prior genuine one-sided answer gains retain a unique exact SoundCloud parent match, and
all eight parents now pass the shared detector. No gain was lost. In particular, the eighth
parent, `Tuuuuune !!! Id???`, is handled by the general elongated-`tune` rule, not by an answer or
source-record special case.

### Tests removed, replacements, and revert proof

Two tests existed only for the removed mention/reaction fallback and were removed:

- `test_a_dash_spaced_on_only_one_side_separates_without_splitting_words_or_junk`
- `test_a_reaction_reply_is_not_promoted_by_a_one_sided_dash`

Their replacement makes all four required strings (`nice one -mate`, `appreciate it -legend`,
`awesome -bro`, and `cheers -legend`) fail closed both without a parent and beneath the
non-question parent `Love this ID`. Separate regressions cover the shared detector and acceptance
of `Example Artist -Signal Path` beneath `ID?`; that test also requires `ID?` itself to materialise
as a question.

With pass 3 present:

```text
$ uv run pytest -q tests/test_stage4a_parser.py -k "one_sided or shared_detector"
...                                                                      [100%]
3 passed, 76 deselected in 0.30s
```

For the revert proof, production `parse.py` was temporarily restored to the pass-2 private
detector and mention-led request/reaction fallback while the new tests stayed in place. Every new
test failed, after which pass 3 was restored:

```text
$ uv run pytest -q tests/test_stage4a_parser.py -k "one_sided or shared_detector"
FFF                                                                      [100%]
FAILED tests/test_stage4a_parser.py::test_a_one_sided_dash_needs_a_genuine_question_parent
FAILED tests/test_stage4a_parser.py::test_real_id_questions_use_the_shared_detector
FAILED tests/test_stage4a_parser.py::test_a_one_sided_dash_answer_is_accepted_under_an_id_question
3 failed, 76 deselected in 0.92s
```

The first failure was the unwanted 9,000-confidence `nice one` / `mate` parse. The second showed
that pass 2 still rejected `ID?` in the shared detector. The third reached the pass-2 structurally
accepted answer but failed because its `ID?` parent was absent from parsed questions. The restored
focused run above proves the temporary revert left no residue.

The parser/connector/relations/cache/pipeline focused run also passed:

```text
$ uv run pytest -q tests/test_stage4a_parser.py tests/test_stage4a_connectors.py \
  tests/test_stage4a_relations_fusion.py tests/test_cache_safety.py tests/test_stage4a_pipeline.py
........................................................................ [ 61%]
..............................................                           [100%]
118 passed, 1 warning in 23.07s
```

### Stored-comment measurement re-run

The read-only comparison again covered all 14 entries in the owner's current `work/index.json`,
including the 11 with comments. It selected only checksum-proven cached SoundCloud/YouTube results
named by each `hints.done.json`, de-duplicated by connector/source record ID, parsed in memory
under `scripts.offline_guard.no_network`, and compared with `67bee21`. It wrote nothing to `work/`,
`data/`, or the deep-scan tree.

There are 106 changed parses among 1,150 comments: the same eight genuine answer improvements and
98 newly recognised genuine questions caused by correcting the shared detector. No existing
properly spaced split changed. The eight answer changes are:

1. `@jacobmi11er: mg- 1ofthozedaze`
   — `[]` → answer `mg` / `1ofthozedaze` (9,000).
2. `@gemma-sommerville metaphyscial- Mall Grab`
   — `[]` → answer `metaphyscial` / `Mall Grab` (9,000).
3. `@sharon-du-pon Metaphysical- Mall Grab`
   — `[]` → answer `Metaphysical` / `Mall Grab` (9,000).
4. `@user-437725651 winter vol2- mg unreleased`
   — keyword with unsplit title → answer `winter vol2` / `mg unreleased` (9,000).
5. `@user-915363248 \n\nMall grab- tremors`
   — `[]` → answer `Mall grab` / `tremors` (9,000).
6. `@user-480750305 effy -stone x next hype on bandcamp ( I’ve edited an extended version of it
   I’m happy to send if you dm me)`
   — `[]` → answer `effy` / `stone x next hype on bandcamp` (9,000).
7. `@jeroen-van-duin-2 Freedom -Jamo (unreleased)`
   — keyword with unsplit title → answer `Freedom` / `Jamo (unreleased)` (9,000).
8. `@user-184780529 the days -notion remix`
   — `[]` → answer `the days` / `notion remix` (9,000).

Every question change is `[]` → question with confidence 8,000. To list every stored occurrence
without repeating identical text dozens of times, the exhaustive inventory below groups identical
text; each reference is `<first 8 media-key characters>/<source_record_id>`:

- `I need the id`: `66511186/2226359909`
- `ID ?`: `66511186/2496411732`
- `Id ?`: `66511186/2076168567`
- `ID ? :)`: `99b8ceeb/2433871799`
- `ID ???`: `6c00a79d/2351208785`
- `ID ?🤩`: `66511186/1914423676`
- `Id on this?`: `a53c5eed/2502251759`
- `Id on this?😵‍💫`: `fcc32360/2456046935`
- `Id please?`: `66511186/1886766247`
- `ID?`: `66511186/1871728383`, `66511186/1905626176`, `66511186/1941669477`,
  `66511186/1946694099`, `66511186/1951120779`, `66511186/2051322664`,
  `66511186/2148129443`, `66511186/2289073644`, `66511186/2344379747`,
  `66511186/2570056008`, `6c00a79d/2321100851`, `819e1ee8/2527687103`,
  `819e1ee8/2527768979`, `819e1ee8/2527896143`, `819e1ee8/2528419902`,
  `819e1ee8/2529215883`, `819e1ee8/2529466232`, `819e1ee8/2529880683`,
  `819e1ee8/2529946118`, `819e1ee8/2530110539`, `819e1ee8/2530459518`,
  `819e1ee8/2531453043`, `819e1ee8/2531576831`, `819e1ee8/2531891942`,
  `819e1ee8/2531893784`, `819e1ee8/2531976870`, `819e1ee8/2532203504`,
  `819e1ee8/2532248886`, `819e1ee8/2535547340`, `819e1ee8/2535548670`,
  `819e1ee8/2569738803`, `819e1ee8/2572789697`, `a53c5eed/2505944885`,
  `a53c5eed/2581172345`, `d3252340/2510132567`
- `Id?`: `66511186/1884448669`, `66511186/1890240216`, `66511186/1909368348`,
  `66511186/2077309183`, `66511186/2297051136`, `66511186/2336917758`,
  `6c00a79d/2396313981`, `819e1ee8/2529599370`, `819e1ee8/2530232088`,
  `819e1ee8/2531150499`, `819e1ee8/2532124515`, `819e1ee8/2535217244`,
  `819e1ee8/2535458507`, `819e1ee8/2538627434`, `819e1ee8/2552107734`
- `id?`: `66511186/1862855712`, `66511186/1888176778`, `66511186/1924543870`,
  `66511186/1931866408`, `66511186/1931870331`, `66511186/1937558859`,
  `66511186/2148132575`, `66511186/2429870771`, `66511186/2429879975`,
  `66511186/2460069483`, `66511186/2568673515`, `819e1ee8/2529929276`
- `ID?!`: `819e1ee8/2534744612`, `819e1ee8/2555343504`
- `Id?!`: `99b8ceeb/2446904234`
- `ID?! 🙏🙏`: `819e1ee8/2557213742`
- `Id?!!!`: `819e1ee8/2571038732`
- `ID??`: `66511186/1993580209`, `66511186/2050407354`, `66511186/2458391862`,
  `819e1ee8/2560739745`, `819e1ee8/2587821620`
- `Id??`: `354fbd61/2556345662`, `66511186/2048165701`
- `ID?? 🥹🥹🥹`: `819e1ee8/2534263319`
- `ID???`: `66511186/2329088846`, `819e1ee8/2569444400`, `819e1ee8/2572206897`,
  `a53c5eed/2556740477`
- `Id???`: `66511186/1940110461`
- `id???`: `66511186/1860758361`
- `Id????`: `66511186/1896214650`
- `Id?????`: `66511186/2213010672`
- `Id??????`: `d3252340/2446491876`
- `need id!!!`: `354fbd61/2577743394`
- `Tuuuuune !!! Id???`: `99b8ceeb/2387038248`
- `What is the ID here skipper? Unreal stuff`: `6c00a79d/2320286846`
- `What's the ID for this absolute Heat 🥵 🔥`: `a53c5eed/2508604239`
- `What's the ID here?`: `6c00a79d/2394645962`

```text
MEDIA=14
MEDIA_WITH_COMMENTS=11
COMMENTS=1150
CHANGES=106
QUESTION_PROMOTIONS=98
ANSWER_CHANGES=8
NETWORK_ATTEMPTS=0
```

### Full foreground suite

Collection after consolidating the fallback regressions into structural tests:

```text
$ uv run pytest --collect-only -q
2209/2306 tests collected (97 deselected) in 3.94s
```

The four foreground shards account for every selected test. All passed on their first run; none of
the three documented load-sensitive tests failed, so no isolated rerun was needed.

```text
$ uv run pytest -q tests/idea_web
395 passed, 1 warning in 442.13s (0:07:22)

$ uv run pytest -q <top-level test_a through test_f files>
309 passed, 1 warning in 447.30s (0:07:27)

$ uv run pytest -q <top-level test_g through test_p files>
552 passed, 1 warning in 552.88s (0:09:12)

$ uv run pytest -q <top-level test_q through test_z files>
951 passed, 2 skipped, 97 deselected, 1 warning in 996.31s (0:16:36)
```

Totals: `395 + 309 + 552 + 951 + 2 skipped = 2209` selected tests.

### Required gates and final status

```text
$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
445 files already formatted

$ uv run python scripts/audit_fixtures.py
audited 564 files
fixture audit passed

$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js

$ git diff --check
(no output)

$ git status --short
 M src/id_detector/hints/parse.py
 M src/id_detector/present/exports.py
 M src/id_detector/present/grouping.py
 M src/id_detector/present/page.py
 M src/id_detector/present/server.py
 M tests/fixtures/present/crowd.json
 M tests/idea_web/test_refusion_money.py
 M tests/test_projection.py
 M tests/test_refusion.py
 M tests/test_stage4a_parser.py
 M tests/test_stage7_page.py
?? docs/reviews/build-local-comment-credit.md
```

No commit or branch was created. `theme.py`, playlists, `README.md`, `idea.cmd`, the golden,
`work/`, `data/`, and the deep-scan tree remain untouched.

BUILD: COMPLETE

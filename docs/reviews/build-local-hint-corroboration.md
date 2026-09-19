# Build — local accuracy: hint corroboration

Owner-requested local accuracy work, uncommitted on a worktree at `3ff64d2` for review. No
dependency added; `profiles/`, `data/corpus/`, `work/`, the golden Local Free output and every other
session's file are untouched. No live provider call and no network hint fetch was made: every
measurement re-ran parsing, relations, fusion and presentation in memory from COPIES of the cached
inputs (observations, windows, connector results) placed in a scratch directory outside the repo,
with `AUDD_API_TOKEN=` and `IDEA_ENGINE_SHAZAM=off` set for those scripts only. Recognition was
never triggered.

## 1. The state found (before anything was changed)

The brief's diagnosis is two weeks old. What is true at `3ff64d2`:

- **Hint corroboration is not dead.** Re-fusing the fourteen cached mixes with the unchanged code
  gives 47 audio episodes flagged `hint_supported` (59 listed rows), and 68 of 229 eligible named
  hints attach. The field-order swap was fixed the same day it was diagnosed (`837cbb3`, then
  `3edecc2`, `1f75269`, and 1b-ii `68173fe`): `fuse/identity.py` pools a hint's two fields into one
  word set and joins it to an audio label when one set contains the other (at least two words), or
  when a single long word is one edit apart, or a short word one trailing letter apart; the
  featuring marker is dropped. So "senses - bakey" and "GOOD 4 U - MPH" already corroborate.
- `fuse/episodes.py` flags an episode `hint_supported` when an eligible hint (any positioned
  answer or correction; a tracklist line only from the uploader, a pinned comment, MixesDB or
  1001TL) resolves to the episode's **work** and its position range **intersects one of the
  episode's matched windows**. One supporting provenance group is worth one full trial towards the
  work tier, keeps a short row listed, and makes the episode immune to suppression. Hints never
  vote for the version tier.
- `hints/relations.py` (`_identity_key`, `_copy_confidence`) is order-sensitive, but it is **copy
  detection** (is this comment a copy of that tracklist line?), not corroboration. Left as it is
  on purpose: an answer typed the other way round is evidence it was NOT copied from the list.
- Hint-only rows exist and are marked (`hint_only`); that policy is unchanged.

What was still failing, measured on the cached mixes (161 eligible named hints did not attach):

| Why a named hint did not attach | Hints |
|---|---|
| Work matched, but the hint sat outside every matched window (time) | 36 |
| Work matched, hint has no position at all (untimed YouTube comment lists) | 18 |
| No audio work matched, although the recogniser did hear the track (label mess) | 8 fixed here, plus 5 bare-hyphen answers the table does not count, plus a handful left (section 5) |
| No audio work matched because the recogniser never heard it (nothing to corroborate) | the rest |

## 2. Causes and fixes

**Cause A — time. The hint is where the track is first heard; the matched windows start later.**
Of the 36 timed hints whose work matched, 30 sat BEFORE the episode's first matched window
(median 45 s, the furthest 178 s), 3 sat between two matched windows, 3 just after the last (4 s,
29 s, 87 s). The corpus scorer sees the same lag from the other side: the tool's start is a median
17 s, 48 s and 51 s after the hand-checked start on the three timed mixes. The old test required
the hint's own +/-5 s range to hit a 12 s window.
*Fix (`fuse/episodes.py`):* `hint_reaches_supports` — a hint is about a play when it falls from
`HINT_LEAD_IN_MS` (180 s) before the first matched window to `HINT_TRAIL_MS` (90 s) after the
last, gaps included. `hint_backed_plays` decides per work: a hint backs every play it sits on, as
before; otherwise only the ONE play in reach with the most proved on-air time (nearest on a tie),
so a stray 12 s fragment of the same track is not pulled onto the tracklist as a second row. The
reach never decides WHAT a hint names: the work must already match.

**Cause B — the both-parts rule was missing.** The word-set rule accepts any hint whose words are
all in the audio label, so "Run - MPH" corroborated "MPH - Run It" (both tracks are in the owner's
corpus mix). *Fix (`fuse/identity.py`, `hint_label_corroborates`):* the old word-set rule must
still pass, AND the hint must name at least one credited name (artist, featured guest or remixer)
AND the whole title (or the whole of a bracketed subtitle), in either field order.

**Cause C — label mess compared differently on the two sides.** Same function, same folding on
both sides: accents ("Tiësto"), dotted initials ("C.R.T.B." is "CRTB"), two words typed as one
("bullettooth" / "bullet tooth", "Shella" / "Shell a"; joined length 5 or more, never across the
artist/title boundary), a release note in brackets on the hint ("(unreleased)", "(UR)"), the audio
label's version bracket set aside for the near-spelling test ("feeling emotion - kettama" against
"… Feeling Emotions (Extended mix)"), a trailing un-bracketed version word ("… Edit"), and hedging
words at the two ends of a field ("pretty sure its …", "… i think") — tried both with and without,
because "This Is Oldschool" is a title. A featuring bracket is NOT set aside: a different featured
artist stays a different work (the 1b-ii decision and its test stand).

**Cause D — answers typed with a bare hyphen.** "juice-mall grab" reaches fusion as a title with
no artist, so it had no identity at all. *Fix:* such an answer may JOIN the one audio work it
names (both parts required); it gets no node of its own, so it can never become a track.

**Cause E — a release note read as a title.** "Artist - unreleased" merged into "Artist & Other -
Take My Hand (unreleased)" (its two words were a subset) and listed that track at the wrong time.
*Fix:* `names_a_title` — a title made only of release-note words names no work.

Versions: hints vote for the WORK and never for the version (existing design; the parser already
takes a hint's "(… Remix)" off its title). A hint naming another remix therefore still backs the
work, and the version tier stays unverified — pinned by a test rather than changed.

## 3. Before and after (offline, same harness both sides)

Fourteen cached mixes, named by media key prefix. "Eligible named" = verified, not ID-unknown,
both fields, answer/correction or trusted tracklist line.

| All 14 mixes | Before | After |
|---|---|---|
| Eligible named hints | 229 | 229 |
| … attached to an audio episode | 68 | 112 |
| … work matched but not attached | 54 | 18 (all 18 have no position) |
| … no audio work | 107 | 99 |
| Bare-hyphen answers attached (not counted above) | 0 | 5 |
| Audio episodes flagged `hint_supported` | 47 | 71 |
| Listed rows that are hint-supported | 59 | 79 |
| Hint-only rows | 15 | 13 |

Per mix (only the ones that changed): `6651118675bd` attached 19 to 45, hint-supported episodes 9
to 19, listed 28 to 32; `84232b822c6a` 4 to 12, 4 to 12, one row `possible` to `likely`;
`819e1ee83956` 20 to 26, 11 to 13; `99b8ceebb852` 8 to 11, 6 to 9; `6c00a79d755e` 8 to 9, 8 to 9.
The other nine mixes are identical. Hint-to-audio label pairs: 13 gained, 0 lost.

Corpus score (`scripts/score_corpus.py`, the seven release-1 mixes, draft truth, re-fused copies):

| | found | listed | work recall | work precision | likely |
|---|---|---|---|---|---|
| Before | 616 | 200 | 140/218 · 64.2 % | 141/187 · 75.4 % | 61/67 · 91.0 % |
| After | 615 | 210 | 144/218 · 66.1 % | 145/195 · 74.4 % | 62/68 · 91.2 % |

By time, on the timed mixes: `6651118675bd` recall 6/25 to 9/25, listed 6/19 to 9/28;
`84232b822c6a` likely 3/3 to 4/4, listed 10/24 unchanged; the third is unchanged.

**Read the precision line with this in mind.** Eight more distinct works are listed; the scorer
credits four. The other four it calls wrong are the right track under a label the draft truth
spells differently — "Jordon Alexander - Marathon (feat. Mall Grab)" (truth: "Mall Grab -
Marathon (UR)"), "Surusinghe - Likshot" (truth: "Surusinghe - ID (UR)"), "KETTAMA - Feeling
Emotions (Extended mix)" (truth: "KETTAMA - Feel Emotion (UR)") and "C.R.T.B. - It Never Ends"
(truth has another title at that time; MixesDB and the comments say "It Never Ends"). In a fifth
case the audio row "Flowdan - Shell a Verse" replaced the crowd row "… Shella Verse", which the
scorer's matcher no longer pairs with the truth's spelling. No newly listed row names a track
that was not played. The truth files are read-only, so these were not touched.

Rows the owner will see change: on `6651118675bd` eight real tracks appear (Marathon, Likshot,
Get Down, Juice twice — the truth has both plays — Feeling Emotions, It Never Ends,
Metaphysical); on `99b8ceebb852` "Makes Me (Wanna Move)" appears, "Original Don" rises from
`unclear` to `possible`, and a phantom "Adagio for Strings" under it is now hidden.

One row is lost: on `819e1ee83956` the crowd row "Take My Hand" at 1:45:55 is gone. It was there
only through Cause E (an "Artist - unreleased" comment at that time), i.e. at the wrong time. The
real answers for that track sit under another unsuppressed episode's span, which the existing
hint-only policy skips; that policy was not changed.

## 4. Fixture and tests

`tests/fixtures/deep/hint-corroboration.json` — an authored two-hour mix, ten recognised tracks,
twenty comments: both field orders, a reply under an "ID?" question, a collaborator left out,
"ft." against "(feat. …)", hedging, initials plus a release note, joined words, a near-spelling
against "(Extended Mix)", a bare hyphen, and answers ahead of, between and just after the matched
windows; near-misses that must back nothing (same artist other title, same title other artist, a
title that merely starts the same, title only, artist only, the right name ten minutes away).

`tests/test_hint_corroboration.py` (31 tests). Against the ORIGINAL source the file cannot even be
collected (it imports the new functions), and a scratch script that runs the fixture through the
original code without them reports exactly the fixture's `needs_the_fix` list as missing (9 of 13
attachments) plus one near-miss wrongly attached ("run - low tide unit" backing "Run It").

## 5. Not done, and why

- **Untimed comment tracklists (18 hints, all on `ed4ca55359f9`).** Their work matches but they
  have no position, and the existing rule only lets a positioned hint corroborate. Letting an
  untimed list back a recognised track would be a policy change; it is the biggest remaining win.
- **Abbreviations and two-word misspellings** ("MG" for the artist, "rock the house" for "Rok da
  House"): left unmatched — any rule for them would be looser than the brief allows.
- **`hints/relations.py` order-sensitivity:** deliberately unchanged (section 1).
- **MixesDB quote-wrapped "ID" rows** parse as artist/title with stray quote marks; harmless to
  corroboration, not touched.
- The report's numbers come from a scratch harness, not a committed script.

## 6. Required command outputs

### 6.1 Full suite, in shards covering every collected file

`uv run pytest --collect-only -q -p no:cacheprovider`:

```text
1905/2002 tests collected (97 deselected) in 14.74s
```

Each shard is `uv run pytest -q -p no:cacheprovider <files>`, run one at a time and waited for.
No `IDEA_ENGINE_SHAZAM` was exported around the suite.

| Shard | Files | Result |
|---|---|---|
| 1 | `tests/test_{a,c,d,e,f,g,h,i,l}*.py` (includes the new file) | `178 passed, 1 warning in 98.98s (0:01:38)` |
| 2 | `tests/test_p*.py` | `455 passed, 1 warning in 685.50s (0:11:25)` |
| 3 | `tests/test_sc*.py test_se*.py test_stage1_*.py test_stage10_*.py test_stage2*.py` | `275 passed, 23 deselected, 1 warning in 122.04s (0:02:02)` |
| 4 | `tests/test_stage3*.py` … `test_stage9*.py` | `319 passed, 1 skipped, 70 deselected, 1 warning in 23.95s` |
| 5 | `tests/test_t*.py` | `303 passed, 1 skipped, 4 deselected, 1 warning in 127.86s (0:02:07)` |
| 6 | `tests/idea_web/test_{a,b,c}*.py` | `120 passed, 1 warning in 81.09s (0:01:21)` |
| 7 | `tests/idea_web/test_{f,h,l,o,p,w}*.py` | `253 passed, 1 warning in 216.42s (0:03:36)` |

178 + 455 + 275 + 320 + 304 + 120 + 253 = **1905** (1903 passed, 2 skipped, 0 failed) — the
collected total. Neither known flake fired. Shard 2 outran the 10-minute foreground limit of the
tool running it and finished in the background; it was waited for before anything else ran.
After the last shard one over-long line in the new test file was wrapped; the new file and the
fixture-audit test were re-run (`36 passed`).

The golden Local Free output (`tests/test_golden_local_free.py`) passes unchanged; nothing under
`profiles/` or `tests/golden/` was touched.

### 6.2 `uv run ruff check .`

```text
All checks passed!
```

### 6.3 `uv run ruff format --check .`

```text
392 files already formatted
```

### 6.4 `uv run python scripts/audit_fixtures.py`

```text
audited 524 files
fixture audit passed
```

### 6.5 `git diff --stat` (new files shown with intent-to-add, then un-added again)

```text
 docs/reviews/build-local-hint-corroboration.md | 160 +++++++++++++
 src/id_detector/fuse/episodes.py               |  90 +++++++-
 src/id_detector/fuse/identity.py               | 297 +++++++++++++++++++++---
 tests/fixtures/deep/hint-corroboration.json    |  79 +++++++
 tests/test_hint_corroboration.py               | 305 +++++++++++++++++++++++++
 5 files changed, 893 insertions(+), 38 deletions(-)
```

(The report's own line count is from before this section was appended.)

### 6.6 `git status --short`

```text
 M src/id_detector/fuse/episodes.py
 M src/id_detector/fuse/identity.py
?? docs/reviews/build-local-hint-corroboration.md
?? tests/fixtures/deep/hint-corroboration.json
?? tests/test_hint_corroboration.py
```

### 6.7 Corpus score headlines (verbatim)

Before (unchanged code, re-fused copies):

```text
Of the 616 tracks the tool found, the presentation floor hid 123 as buried, 32 as contradicted, 30 as scatter, 231 as short, leaving 200 listed; it named 140 of the 218 distinct tracks actually played (work recall 64.2%); 141 of the 187 distinct tracks it listed were really played (work precision 75.4%); and 61 of the 67 it marked 'likely' or better were right (likely precision 91.0%)
```

After:

```text
Of the 615 tracks the tool found, the presentation floor hid 125 as buried, 28 as contradicted, 30 as scatter, 222 as short, leaving 210 listed; it named 144 of the 218 distinct tracks actually played (work recall 66.1%); 145 of the 195 distinct tracks it listed were really played (work precision 74.4%); and 62 of the 68 it marked 'likely' or better were right (likely precision 91.2%)
```

## 7. Fix pass (review verdict FIX_FIRST: three P1, one P2, no P0)

Sections 1-6 describe the first pass and are left as written; where this section disagrees with
them, this section is what the code now does. Still uncommitted; same rules (copies only, no
network, no provider, golden and profiles untouched).

### 7.1 P2 — the "Mears - Be My Lover" row was NOT surfaced by this change

The first-pass chat summary said the change had surfaced a wrong row "Mears - Be My Lover" on
`99b8ceebb852`. That was wrong; the reviewer's replay is right. Replayed again under the original
code, the first pass and the fix pass:

| | Episode id | Proved support | Badge | Flags | Suppressed | Supporting hint | Floor decision |
|---|---|---|---|---|---|---|---|
| Mears, first play — original | `7e8a7cf7c2b66d6dace6fa54c161f6d6159e97dd` | 1404-1434 s (30 s on air) | possible | none | no | none | listed (30 s meets the 30 s floor) |
| same — after | the same id | the same | possible | none | no | none | listed |
| Mears, second play — original | `0543d6785de5869870fd83644c00dde1ff454c6e` | four windows 1485-1560 s (48 s on air) | unclear | none | no | none | listed (48 s) |
| same — after | the same id | the same | unclear | none | no | none | listed |

Both episodes are byte-for-byte the same before and after, carry no `hint_supported`, and no hint
resolves to their work. Row by row (`collapse=False`) the tracklist is identical. What changed is
only the page's grouping: before, the two Mears episodes were stacked into ONE row (1416-1560 s)
by the same-track bridge; the episode between them, "Original Don" (`ca98bab9…`, 1449-1470 s),
is now hint-supported and `possible`, so it sits between them as a confident row and the bridge
no longer spans it. The first-pass comparison printed the second half of an already-listed row
as "new". The same thing explains the "Torres De Lara" row moving from 1443 s to 1479 s. The
pre-change accuracy report already lists Mears among the wrong rows.

### 7.2 P1 — label matching is field-level (`fuse/identity.py`)

`hint_label_corroborates` no longer pools the hint's words. It tries the two orientations
(field A = artist, field B = title; then the other way round) and in one of them requires BOTH:

- **the title field IS the recognised title** — the same words in the same order after the
  clean-up both sides get (case, punctuation, accents, dotted initials, version brackets, trailing
  version words such as "Original Mix" / "Edit", featuring credits bracketed or not, a release
  note, two words typed as one). A longer or shorter title is another title. One spelling slip is
  allowed only as a dropped or added letter in a word of six letters or more ("Temors" /
  "Tremors", "Emotion" / "Emotions"), or the 1b-ii trailing letter ("Kno" / "Know") when at least
  two other title words agree. A substitution is never a slip.
- **the artist field holds one whole credited name** of the recognised label (an artist, a
  featured guest or a remixer), every word of it that identifies anybody: "Alice" for "DJ Alice";
  "DJ", "The", "MC" alone identify nobody. Chatter around the name is tolerated in the artist
  field only ("thanks dude … i think it is mall grab"); hedging may be cut from the two ends of
  either field, as before.

Also: a placeholder field ("ID", "TBC", "TBA", "unknown", "unreleased", "UR" …) names nothing —
it never matches and gets no identity of its own, so "Artist - ID" can neither back a track nor
be listed as a crowd row. Two hints with different featured guests are different works (the
1b-ii test stands). A bare-hyphen answer is split at its hyphen and read the same two ways.

**Ambiguity veto.** A hint joins ONE recognised work or none. "Recognised work" is what existed
before any hint was read: the provider-linked components, with two labels that differ only by a
version bracket or a featuring credit counted as one ("Party Drumz" and "Party Drumz (Club
Mix)" — otherwise every track Shazam returns under two release labels would be "ambiguous"). A
hint that fits two of them backs nothing and gets no node, so it can neither promote the wrong
one nor list a third row. Hints no longer merge two recognised works into one.

**One more hole closed (found while doing this, not in the review).** The hint-to-hint merge
(1f75269) still uses the loose word-set rule, and it did not actually exclude audio-matched hints
as its comment claimed — so "Run - Artist" could ride into the recognised "Run It" through a
well-spelled neighbour. Audio-matched hint nodes are now excluded; only the field-level test
carries a hint into a recognised work.

Reviewer's false negatives, now accepted: "Artist - Song" against "Artist - Song feat. Guest" and
against "Artist - Song Original Mix".

### 7.3 P1 — one hint, at most one episode; indirect hints stay humble (`fuse/episodes.py`)

`hint_backed_play` returns one play or none.

- **Direct** (the hint sits on a matched window): the play it overlaps most, then the one with
  more proved on-air time, then the earlier start, then the smaller key.
- **Indirect** (on no window, inside the -180 s / +90 s reach): a play whose hull it falls
  inside comes first; then the most proved on-air time; then the nearer; then the earlier start;
  then the smaller key. On-air time ranks above distance on purpose: two plays of one work within
  270 s are all but always pieces of one performance, and ranking by distance would promote a
  stray 12 s fragment to a second row (the first pass measured exactly that). Every key is a
  total order over the play's own numbers, so the choice is deterministic.
- An episode backed ONLY by indirect hints still gets the vote and the short-row exemption, but
  it is no longer immune to `scatter` or `contradicted` — neither through the `hint_supported`
  flag nor through a `likely` badge that the one extra trial bought (the test caught that second
  route). `buried` immunity is kept, as the brief allowed. Such an episode is also kept out of
  the confident spans that bury others when it is itself scattered or contradicted.

### 7.4 P1 — regression tests (26 new, 57 in the file)

Run against the FIRST-PASS source (swapped in from a scratch copy, then restored): **25 of the 26
fail** — every one that guards an unsafe rule. The 26th (`two labels of one work are not an
ambiguity`) is the guard against over-tightening and passes on both, as it should.

| Risk | Test | First-pass result |
|---|---|---|
| inverse title substring | `a_longer_title_that_contains_the_recognised_one…` (3 cases) | fail |
| common artist words | `a_common_word_is_not_a_credited_name` (3) | fail |
| cross-field swaps | `a_word_of_one_field_never_stands_in_for_the_other` (3) | fail |
| one-edit distinct titles | `a_title_one_edit_away_is_another_title` (4) + the slips that must still pass | fail |
| `Artist - ID` | `a_placeholder_names_nothing_and_gets_no_identity` (5, through fusion) | fail |
| ambiguity | `a_hint_that_fits_two_recognised_works_backs_neither…` | fail |
| chained merge | `a_loosely_spelled_answer_is_not_carried_in…` | fail |
| adjacent previous / next | `the_previous_and_the_next_track_each_keep_their_own_hints` (tracks 4 min long, back to back, an answer on the blend) | fail |
| one hint, two episodes | `one_hint_on_two_episodes_backs_the_one_it_overlaps_most` | fail |
| indirect vs scatter | `an_indirect_hint_never_lifts_a_scattered_episode…` | fail |
| indirect vs contradicted | `an_indirect_hint_never_lifts_a_contradicted_episode` | fail |

The fixture gained two near-misses: the right name ten seconds outside the reach, and an
"Artist - ID" placeholder on a recognised track.

### 7.5 Re-measured (same harness, same copies)

| All 14 mixes | Original | First pass | Fix pass |
|---|---|---|---|
| Eligible named hints attached | 68 | 112 | 110 |
| Bare-hyphen answers attached | 0 | 5 | 3 |
| Work matched but not attached | 54 | 18 | 18 (all untimed) |
| Audio episodes `hint_supported` | 47 | 71 | 71 |
| Listed rows that are hint-supported | 59 | 79 | 78 |
| Hint-only rows | 15 | 13 | 13 |
| Listed track rows (page view) | 358 | 366 | 366 |

| Release-1 (7 mixes) | listed | work recall | work precision | likely |
|---|---|---|---|---|
| Original | 200 | 140/218 · 64.2 % | 141/187 · 75.4 % | 61/67 · 91.0 % |
| First pass | 210 | 144/218 · 66.1 % | 145/195 · 74.4 % | 62/68 · 91.2 % |
| Fix pass | 211 | 144/218 · 66.1 % | 145/196 · 74.0 % | 62/68 · 91.2 % |

Hidden as scatter / contradicted: 30 / 28 in both passes — on these mixes no indirectly-backed
episode was scattered or contradicted, so the new guard costs nothing here.

**The nine genuine new rows: all nine survive.** The page tracklist (collapsed view) is
identical to the first pass, row for row, on all fourteen mixes; no row is lost.

What the tightening cost, and why none of it was loosened back:

- Label pairs lost (no row changed): a MixesDB line whose title is "ID (… / ROK DA HOUSE!)" (a
  placeholder title); "Intro ID: In The Air - …" and "… - The Rhythm Is Here, came out today on
  …" (chatter inside the TITLE field, which field-level equality refuses); two bare-hyphen
  answers of the shape "X-Club „Say no more”" (no separator between artist and title at all, so
  the hyphen split is the wrong split). Gained: "MG & Flansie - No One Else Will" and "feeling
  emotion by Kettama. …" (chatter in the ARTIST field, which is tolerated).
- The one scorer difference (listed 210 to 211, precision 145/195 to 145/196) is on
  `84232b822c6a`: the MixesDB line for "Corona - The Rhythm Of The Night (… Remix)" used to fit
  three recognised labels and merge them; it now fits only the two spelled with "The" (one
  recognised work), so its vote goes to a 12 s fragment of that work instead of the 39 s "Lee
  Marrow" label. The page still shows one row (the fragment folds into it); the row-by-row scorer
  counts one more distinct label, which the draft truth calls wrong because it names the remixer.
  Not a new track, and not loosened.

### 7.6 Commands (fix pass, all foreground, each waited for)

`uv run pytest --collect-only -q -p no:cacheprovider` → `1931/2028 tests collected (97 deselected)`.

| Shard | Files | Result |
|---|---|---|
| 1 | `tests/test_{a,c,d,e,f,g,h,i,l}*.py` (includes the new file) | `204 passed, 1 warning in 93.95s` |
| 2a | `tests/test_phase0a*.py` | `78 passed, 1 warning in 101.97s` |
| 2b | `tests/test_phase0b*.py test_pa*.py test_pl*.py test_pr*.py` | `126 passed, 1 warning in 62.38s` |
| 2c | `tests/test_phase1a*.py` | `78 passed, 1 warning in 83.15s` |
| 2d | `tests/test_phase1b*.py` | `96 passed, 1 warning in 56.65s` |
| 2e | `tests/test_phase2*.py test_phase3*.py` | `77 passed, 1 warning in 43.73s` |
| 3 | `tests/test_sc*.py test_se*.py test_stage1_*.py test_stage10_*.py test_stage2*.py` | `275 passed, 23 deselected, 1 warning in 109.38s` |
| 4 | `tests/test_stage3*.py` … `test_stage9*.py` | `319 passed, 1 skipped, 70 deselected, 1 warning in 31.55s` |
| 5 | `tests/test_t*.py` | `303 passed, 1 skipped, 4 deselected, 1 warning in 101.92s` |
| 6 | `tests/idea_web/test_{a,b,c}*.py` | `120 passed, 1 warning in 74.19s` |
| 7 | `tests/idea_web/test_{f,h,l,o,p,w}*.py` | `253 passed, 1 warning in 199.05s` |

204 + 455 + 275 + 320 + 304 + 120 + 253 = **1931**, the collected total (1929 passed, 2 skipped,
0 failed; neither known flake fired; no shard went near the ten-minute limit). The golden Local
Free test passes unchanged.

```text
uv run ruff check .            -> All checks passed!
uv run ruff format --check .   -> 392 files already formatted
uv run python scripts/audit_fixtures.py -> audited 524 files / fixture audit passed
```

`git status --short`:

```text
 M src/id_detector/fuse/episodes.py
 M src/id_detector/fuse/identity.py
?? docs/reviews/build-local-hint-corroboration.md
?? tests/fixtures/deep/hint-corroboration.json
?? tests/test_hint_corroboration.py
```

`git diff --stat` (tracked files): `episodes.py | 183`, `identity.py | 435`, 543 insertions, 75
deletions.

### 7.7 Still open

- The reviewer's blast-radius note stands: the same fusion code serves Free and Deep, and a
  changed `hint_supported` also changes which `hint_cluster` rescans a full re-run would ask for.
- The slip rule still lets a long plural through ("Friend" / "Friends" by the same credited
  artist). Judged acceptable because the artist must match as well; stated so it is not a surprise.
- Untimed comment tracklists (18 hints) still cannot attach — a policy question, unchanged.

## 8. Fix pass 2 (review round 2: two P1, everything else confirmed DONE)

Only `fuse/identity.py` and the test file changed; `fuse/episodes.py` and everything the review
confirmed are as they were. Still uncommitted, same rules.

### 8.1 P1 — the ambiguity veto has no exact-label shortcut

A hint whose text was a recognised label letter for letter used to be assigned to that label
without being read against the others (`named = [] if text_node in audio_text_nodes`). Now
EVERY ordinary hint is read against every recognised label first; if the labels it fits span
more than one recognised work, it gets no identity and no assertion — it backs nothing, lists
nothing and, having no work, contradicts nothing. An exact label that fits one work still backs
it, and now also joins that work's other labels exactly as a non-exact hint would.

### 8.2 P1 — featured guests are whole names

A featured credit is now compared as a whole credited name (its words minus the common ones:
"DJ Boring" is `boring`, "Bob Jones" is both words), never word by word. So "… (feat. DJ
Seinfeld)" is not "… (feat. DJ Boring)", and "… (feat. Bob Smith)" is not "… (feat. Bob Jones)".

The recognised-work grouping no longer erases featured credits. Labels with the same artist names
and title are one work unless they credit DIFFERENT featured guests: with at most one distinct
credit among them they are joined (an uncredited "Song" and the single "Song (feat. X)" — the
ordinary two-label case); with two or more, only equal credits are joined, so an uncredited
label can never bridge "… (feat. Bob Jones)" and "… (feat. Bob Smith)". An uncredited hint that
fits both is vetoed by 8.1 and merges nothing. The key also reads the artist names without an
"ft. X" tail, so "MPH ft. Cecelia - Rush" and "MPH - Rush (feat. Cecelia)" still key alike.

### 8.3 Regression tests (6 new, 63 in the file)

Run against the fix-pass-1 `identity.py` (swapped in, then restored): **5 of the 6 fail.** The
sixth guards against over-tightening and passes on both.

| Test | Fix-pass-1 result |
|---|---|
| `an_exact_label_that_also_fits_the_next_track_backs_and_contradicts_neither` — two adjacent 4-minute plays, solo then collaboration, the hint is the solo label in the exact field orientation, typed on the collaboration: neither is `hint_supported`, neither is `contradicted`, the hint is in no evidence, no crowd row, the two works stay two; with only the solo heard, the same hint backs it | fail |
| `featured_guests_are_compared_as_whole_names` — `DJ Boring` / `DJ Seinfeld`, `Bob Jones` / `Bob Smith`, label level and through fusion | fail (both) |
| `two_works_with_different_featured_guests_are_never_merged_by_a_hint` — same two pairs through fusion: the uncredited hint is vetoed, the credited one backs only its own work, the works stay distinct, nothing is contradicted | fail (both) |
| `an_uncredited_label_still_joins_the_one_credited_version_there_is` | passes on both (guard) |

### 8.4 Re-measured (same harness, same copies)

| All 14 mixes | Original | First pass | Fix pass 1 | Fix pass 2 |
|---|---|---|---|---|
| Eligible named hints attached | 68 | 112 | 110 | 109 |
| Bare-hyphen answers attached | 0 | 5 | 3 | 3 |
| Audio episodes `hint_supported` | 47 | 71 | 71 | 70 |
| Listed rows that are hint-supported | 59 | 79 | 78 | 79 |
| Hint-only rows | 15 | 13 | 13 | 13 |
| Listed track rows (page view) | 358 | 366 | 366 | 366 |

| Release-1 (7 mixes) | listed | work recall | work precision | likely |
|---|---|---|---|---|
| Original | 200 | 140/218 · 64.2 % | 141/187 · 75.4 % | 61/67 · 91.0 % |
| First pass | 210 | 144/218 · 66.1 % | 145/195 · 74.4 % | 62/68 · 91.2 % |
| Fix pass 1 | 211 | 144/218 · 66.1 % | 145/196 · 74.0 % | 62/68 · 91.2 % |
| Fix pass 2 | 210 | 144/218 · 66.1 % | 145/195 · 74.4 % | 62/68 · 91.2 % |

**All nine genuine new rows survive; no row is lost.** Against fix pass 1 exactly two things
moved, both from 8.1:

- `84232b822c6a`: the MixesDB line "Corona - The Rhythm Of The Night" is a recognised label
  letter for letter AND fits "Neptunica & Corona - The Rhythm of the Night", a different
  recognised work. It is now vetoed (one attachment and one `hint_supported` episode fewer), so
  the 12 s fragment fix pass 1 had promoted is a short hidden row again — which is why the scorer
  is back to 210 listed and 145/195. The page row is unchanged.
- `819e1ee83956`: the exact label "The Crazy Drummer - Party Drumz" now joins "… Party Drumz
  (Club Mix)" as one work (one recognised work under 8.2's grouping), so the page's single
  "Party Drumz" row starts at its first play, 1:14:00, instead of 1:16:51 — where the tracklist
  puts it. Same track, still one row.

Nothing correct was lost to the tightening, so nothing needed defending or loosening.

### 8.5 Commands (all foreground, each waited for)

`uv run pytest --collect-only -q -p no:cacheprovider` → `1937/2034 tests collected (97 deselected)`.

| Shard | Files | Result |
|---|---|---|
| 1 | `tests/test_{a,c,d,e,f,g,h,i,l}*.py` (includes the new file) | `210 passed, 1 warning in 63.32s` |
| 2a | `tests/test_phase0a*.py` | `78 passed, 1 warning in 82.03s` |
| 2b | `tests/test_phase0b*.py test_pa*.py test_pl*.py test_pr*.py` | `126 passed, 1 warning in 63.82s` |
| 2c | `tests/test_phase1a*.py` | `78 passed, 1 warning in 83.33s` |
| 2d | `tests/test_phase1b*.py` | `96 passed, 1 warning in 56.56s` |
| 2e | `tests/test_phase2*.py test_phase3*.py` | `77 passed, 1 warning in 38.86s` |
| 3 | `tests/test_sc*.py test_se*.py test_stage1_*.py test_stage10_*.py test_stage2*.py` | `275 passed, 23 deselected, 1 warning in 107.36s` |
| 4 | `tests/test_stage3*.py` … `test_stage9*.py` | `319 passed, 1 skipped, 70 deselected, 1 warning in 25.66s` |
| 5 | `tests/test_t*.py` | `303 passed, 1 skipped, 4 deselected, 1 warning in 104.75s` |
| 6 | `tests/idea_web/test_{a,b,c}*.py` | `120 passed, 1 warning in 70.74s` |
| 7 | `tests/idea_web/test_{f,h,l,o,p,w}*.py` | `253 passed, 1 warning in 246.62s` |

210 + 455 + 275 + 320 + 304 + 120 + 253 = **1937**, the collected total (1935 passed, 2 skipped,
0 failed; neither known flake fired; the longest shard took just over four minutes). The golden
Local Free test passes unchanged.

```text
uv run ruff check .            -> All checks passed!
uv run ruff format --check .   -> 392 files already formatted
uv run python scripts/audit_fixtures.py -> audited 524 files / fixture audit passed
```

`git status --short` is unchanged: the two `fuse/` modules modified; the report, the fixture and
the test file untracked.

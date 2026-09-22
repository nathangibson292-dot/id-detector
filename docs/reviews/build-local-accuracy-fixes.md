# Build — local accuracy: three measured fixes

Owner-directed local accuracy work, uncommitted on a worktree fast-forwarded to `21589a0`. It
implements changes 2, 3 and 4 of `docs/accuracy/release-1-miss-analysis.md` (section 8). No
dependency added; `profiles/`, `data/corpus/`, `work/`, every truth file, the golden Local Free
output, `truth.py`, `benchmark/scorer.py`, `calibrate/`, `scripts/score_corpus.py` and every other
session's file are untouched. No live provider call and no network fetch was made: every number
below comes from COPIES of the cached inputs (observations, windows, hints, cached hint-connector
results, `pcm.json`, `source.json`) of all fourteen cached mixes, placed in a scratch directory
outside the repo and re-fused, re-presented and re-scored there (`IDEA_TEST_MODE=1`,
`AUDD_API_TOKEN=` empty and `IDEA_ENGINE_SHAZAM=off` set inside the scratch scripts only).
Recognition was never triggered.

## 0. Status of the two items first reported as "not done"

Both are now done, at the coordinator's direction — see **section 7, "Reaching existing
mixes"**: the fusion version is `fusion:3` with an offline re-fusion path, and `PAGE_VERSION`
is 25. Sections 1-6 are the first pass as written; their command outputs are superseded by 7.7.

## 1. Headline (shipped default: rescans OFF — this is the honest baseline)

The analysis's 144/218 was only reached with one mix's cached rescan data; with the inputs the
shipped default uses, the same code gives 143. Both are shown; the first line is the headline.

| Seven release-1 mixes, draft truth | listed rows | work recall | work precision | `likely` precision |
|---|---|---|---|---|
| **Before — rescans off (shipped default)** | 208 | **143/218 · 65.6 %** | 143/193 · 74.1 % | 63/69 · 91.3 % |
| **After — rescans off (shipped default)** | 222 | **154/218 · 70.6 %** | 156/204 · 76.5 % | **63/64 · 98.4 %** |
| Before — with the one mix's cached rescans (the analysis's baseline) | 210 | 144/218 · 66.1 % | 145/195 · 74.4 % | 62/68 · 91.2 % |
| After — with the one mix's cached rescans | 223 | 154/218 · 70.6 % | 157/205 · 76.6 % | 62/63 · 98.4 % |

The analysis's combined target was 151/218. The build measures **154**: the simulation flipped
`suppressed` flags on already-fused episodes, so it could not see that the phantoms' hulls were
also blocking three comment-only rows on one set (section 3.1). Wrong listed labels fall from 50
to 48. Hidden rows: buried 120 → 53, contradicted 28 → 27, scatter 29 → 34, short 222 → 277 (most
of what the phantoms used to bury is a 12 s fragment and is now hidden as short instead).

Each fix alone, rescans off (they are independent; the sum is exact: 143 + 7 + 3 + 1 = 154):

| | listed | recall | precision | likely |
|---|---|---|---|---|
| Fix 1 only | 218 | 150/218 | 152/200 | 63/64 |
| Fix 2 only | 211 | 146/218 | 146/196 | 63/69 |
| Fix 3 only | 209 | 144/218 | 144/194 | 63/69 |

Per mix, rescans off, work-only numbers (recall · precision · likely):

| Mix (set folder, media key) | Before | After |
|---|---|---|
| `boomtown-mix` (`d3252340303c`) | 35/40 · 35/40 · 15/16 | 38/40 · 38/43 · 15/16 |
| `redo-of-best-set` (`99b8ceebb852`) | 16/18 · 16/25 · 10/10 | 16/18 · 17/27 · 10/10 |
| `new-mix-jan-24th` (`a53c5eed4b59`) | 25/30 · 25/30 · 10/10 | 26/30 · 26/31 · 10/10 |
| `final-new-set-christmas` (`6c00a79d755e`) | 19/23 · 19/22 · 11/11 | unchanged |
| `mall-grab-boiler-room-melbourne-22` (`6651118675bd`) | 9/24 · 9/24 · 3/7 | 12/24 · 13/26 · 3/3 |
| `mph-youtube-set` (`ed4ca55359f9`) | 29/62 · 29/37 · 10/11 | 33/62 · 33/40 · 10/10 |
| `dj-heartstring-youtube-set` (`84232b822c6a`) | 10/21 · 10/15 · 4/4 | unchanged |

By time, on the three timed sets: `6651118675bd` listed 9/28 → 11/33, recall 9/25 → 11/25, likely
3/7 → 2/3 (see 3.4 — the same three correct `likely` rows are listed; it is a pairing artefact);
`ed4ca55359f9` listed 29/37 → 33/40, recall 29/62 → 33/62, likely 10/11 → 10/10; `84232b822c6a`
unchanged.

All fourteen cached mixes — listed rows (one row per episode, as the scorer counts / page rows
after the same-track grouping). Only the seven release-1 mixes have truth; the other seven can
only be diffed.

| Mix | Before | After |
|---|---|---|
| `354fbd61f5bb` (no truth) | 31 / 33 | unchanged |
| `6651118675bd` | 28 / 25 | 33 / 28 |
| `6c00a79d755e` | 22 / 22 | unchanged |
| `819e1ee83956` (no truth) | 23 / 20 | 35 / 30 |
| `84232b822c6a` | 24 / 21 | unchanged |
| `91a0554a7d03` (no truth) | 19 / 18 | unchanged |
| `99b8ceebb852` | 26 / 26 | 28 / 27 |
| `a53c5eed4b59` | 30 / 30 | 31 / 31 |
| `c3121055e46f` (no truth) | 15 / 15 | unchanged |
| `d3252340303c` | 41 / 41 | 44 / 44 |
| `de0f005862e7` (no truth; has second-engine data) | 33 / 33 | unchanged |
| `ec0a562ab791` (no truth) | 12 / 10 | unchanged |
| `ed4ca55359f9` | 37 / 37 | 40 / 40 |
| `fcc323605143` (no truth; has second-engine and local-index data) | 33 / 34 | unchanged |
| **All 14** | 374 / 365 | 400 / 386 |

## 2. The three mechanisms

### Fix 1 — a `likely` badge counted up from smeared windows (`fuse/episodes.py`)

How the phantom got through, measured on the cached data: `likely` is earned by COUNTING (four
non-overlapping windows across 40 s, no competitor, a "global alignment"). A famous track "heard"
for one window here and another ten minutes later counts just as well, and the alignment
condition is trivially met because only ONE of its windows carries a usable offset anchor (the
others return several conflicting offsets and carry none) — one anchored point is 100 % of the
anchored points. The badge then made the episode immune to `scatter`, and every immune episode
buried whatever lay under its **hull**, first window to last.

Two changes, no track name anywhere:

- **The badge alone is no longer proof against `scatter`.** A `likely`/`verified` episode that
  trips the unchanged scatter test (hull ≥ 120 s and ≥ 3 × its on-air time) keeps its immunity
  only if it has a **core**: one unbroken stretch of matched audio of at least
  `LIKELY_CORE_RUN_MS` = 45 s (five back-to-back windows at the 9 s hop). This is the guard the
  analysis asked for. A comment ON one of its windows, or two separated engine agreements, still
  make it immune exactly as before.
- **A confident episode buries only under the audio it proved.** Its windows are read as runs
  (windows less than `COVER_RUN_GAP_MS` = 60 s apart) and only the runs cover; the 60 % rule is
  unchanged. The existing quirk that a confident episode with exactly the target's hull does not
  bury it is kept.

The phantom rows this removes: `6651118675bd` "Airwave" (177 s on air over a 21-minute hull,
longest stretch 30 s), "Out of the Blue" (138 s over 65 minutes), "Sandstorm" (210 s over 63
minutes), "Out of the Blue 2010" (48 s over 25 minutes); `ed4ca55359f9` "Sandstorm" (57 s over ten
minutes); and on a mix with no truth, `819e1ee83956` "For An Angel" (69 s over 45 minutes).

**The correct row the guard keeps:** `99b8ceebb852` "Skin On Skin - Burn Dem Bridges (Extended
Mix)", `likely`, hull 318 s against 99 s on air (so it trips the scatter test) but with a 57 s
unbroken core. It stays listed and `likely`. Without the guard it would have been hidden.

What is left: `d3252340303c` "Sandstorm" stays listed as `likely` (the one wrong row of the 64).
It is 132 s on air in a 282 s hull with a 48 s core — by its own evidence it cannot be told from a
real track, and the brief rules out a name list. What changed is that it no longer hides "Effy -
Pitched" and "Cotto - Murda Sound", which sit in the 132 s hole between its two runs.

### Fix 2 — short rows by an artist solidly in the mix (`present/exports.py`)

`mark_same_artist_rows` runs once per projection, before the floor is judged, and marks a row
`same_artist_supported`; `short_track` honours the mark. The mark is on the row itself (and only
on a lifted row, so every other row is byte-for-byte what it was), so the page, the exports and
the corpus scorer's own second `hidden_reason` call all decide the same way. A row is lifted only
when ALL of this holds:

- it is hidden ONLY as `short` (a buried, scattered or contradicted row is never lifted);
- it has at least `SAME_ARTIST_MIN_ON_AIR_MS` = 20 s on air — two windows at different positions;
  one 12 s window, or a stack of rescans of one window, is never enough;
- one whole credited name of its artist field equals a whole credited name of the artist field of
  a **solid** row (names are read by fusion's own `audio_work_key`: "MJ Cole & MPH" shares "MPH";
  featured guests and remixers do not count; "Unknown artist" / "Various" name nobody);
- its own track is not already listed under any label (same work id, or a shared artist name and
  one title that starts the other once version brackets are set aside).

**"Solid", defined conservatively:** a row that is listed, audio-backed (not a comment-only row),
badged **`likely` or better**, with at least `SOLID_ARTIST_ROW_MS` = 45 s on air. The analysis's
45 s alone was not enough: measured, a 66 s `possible` row ("Fragma - Toca Me", which the analysis
classes as a sample heard over another track) would have vouched for a second wrong label
("Toca's Miracle"). Requiring `likely` — the tier that is 63/64 right after fix 1 — keeps all
three target rows and lets in nothing else. A lifted row is never `likely`, so the rule cannot
chain. Also measured and refused: a fragment of the solid row's own track under a slightly
different label (`fcc323605143`, "Where's The Party" against "Where's The Party At?"), and repeat
fragments of an already-listed track ("De Barrio", "It Ain't Over") — that is why the two repeat
rows of a wrong phantom the analysis warned about do NOT appear.

### Fix 3 — a comment at the very edge of a long track (`fuse/episodes.py`)

`contradicted` still means a verified answer naming a different work inside the episode's span.
For a LONG episode — at least `EDGE_ANSWER_MIN_ON_AIR_MS` = 60 s of matched audio, which is
stricter than the simulation's 60 s hull — an answer that only touches the first or last
`EDGE_ANSWER_MS` = 15 s is about the neighbouring track in the blend and is not counted; one that
reaches any further in still is. Short or thinly matched episodes keep the old rule unchanged.
Measured: exactly one row changes ("Bushbaby - Pumpin Jumpin", 123 s on air, the answer's range
begins 1.8 s before its last window ends). The long episodes contradicted from the inside stay
hidden: `6c00a79d755e` "TechTrance" (147 s), `6651118675bd` "Amokk" (105 s) and "Miss You" (84 s).

## 3. The exact rows that appear and disappear (rescans off; one row per episode)

RIGHT / WRONG is the corpus scorer's own work match against the draft truth.

```text
== 6651118675bd listed 28 -> 33
   + 0:17:15   10s possible KETTAMA - ID (LET ME SEE U / ROK DA HOUSE!) (UR)   [new comment row] RIGHT
   + 0:21:12   90s possible KETTAMA - Rok da House                              [was buried] RIGHT
   + 0:34:56   10s possible Mall Grab - Bear Witness                            [new comment row] RIGHT
   + 1:02:30   10s possible mallgrab - BB MG (UR)                               [new comment row] WRONG (see 3.2)
   + 1:13:33   30s possible Clouds - Plastyx                                    [was buried] RIGHT
   + 1:18:03   30s possible Clouds - Plastyx                                    [was buried] RIGHT
   + 1:18:23   10s possible Mall Grab - Madman                                  [new comment row] RIGHT
   + 1:21:21   10s possible 1ofthozedaze - Mall Grab                            [new comment row] RIGHT
   + 1:21:48   10s possible MG - 1ofthozedaze                                   [new comment row] WRONG (see 3.2)
   - 0:15:27  177s likely   Rank 1 - Airwave (Radio Vocal Edit)                 [now scatter] WRONG
   - 0:17:54  138s likely   System F - Out of the Blue                          [now scatter] WRONG
   - 0:19:51  210s likely   Darude - Sandstorm                                  [now scatter] WRONG
   - 0:20:27   48s likely   System F - Out of the Blue 2010 (Original Violin Edit) [now scatter] WRONG
== 99b8ceebb852 listed 26 -> 28
   + 0:10:06   33s possible Tiësto - Adagio for Strings                         [was buried] WRONG (see 3.3)
   + 0:43:42  168s possible Skin On Skin - Burn Dem Bridges                     [was buried] RIGHT
== a53c5eed4b59 listed 30 -> 31
   + 0:08:54  123s possible Bushbaby - Pumpin Jumpin                            [was contradicted] RIGHT   (fix 3)
== d3252340303c listed 41 -> 44
   + 0:17:27   57s possible Effy - Pitched                                      [was buried] RIGHT
   + 0:18:21   48s possible Cotto - Murda Sound                                 [was buried] RIGHT
   + 1:14:00   24s possible Chase & Status - 5am                                [was short] RIGHT          (fix 2)
== ed4ca55359f9 listed 37 -> 40
   + 0:22:51   21s unclear  MPH - Raw                                           [was short] RIGHT          (fix 2)
   + 0:50:00   21s unclear  MPH - Doubt                                         [was short] RIGHT          (fix 2)
   + 1:10:24   66s possible MPH - Overrated                                     [was buried] RIGHT
   + 1:11:45   72s possible MPH - Hold On                                       [was buried] RIGHT
   - 1:02:09   57s likely   Darude - Sandstorm                                  [now scatter] WRONG
== 819e1ee83956 (no truth) listed 23 -> 35
   + 0:10:57   60s possible 4 Strings - Daytime (Free State Full Vocal Mix)     [was buried]
   + 0:14:54   33s possible 4 Strings - Daytime (Free State Full Vocal Mix)     [was buried]
   + 0:21:30   30s possible 4 Strings - Daytime (Free State Full Vocal Mix)     [was buried]
   + 0:32:00   42s possible Groove Coverage - Summer Rain (Extended Version)    [was buried]
   + 0:38:36   36s possible Prime Mover - Perfect Organism                      [was buried]
   + 0:39:38   10s possible Benwal & Freddi - Take My Hand                      [new comment row]
   + 0:44:18   30s possible The Sax Brothers - Careless Whisper (South East Players Mix) [was buried]
   + 0:45:21   33s possible Benwal, Entasia & Baron Von Trax - Copycat          [was buried]
   + 0:46:06   60s possible Surge Recordings - Got Dance (Continuously Mixed) [Disc 1] [was buried]
   + 0:47:42   10s possible Mr Polska - Hausa Wausa                             [new comment row]
   + 0:50:54   33s possible Blu Peter - The Pictures In Your Mind (Club Mix)    [was buried]
   + 0:52:15   48s possible Blu Peter - The Pictures In Your Mind (Club Mix)    [was buried]
   + 0:55:36   10s possible Jordan George - Let The Music                       [new comment row]
   - 0:11:36   69s likely   Paul van Dyk - For An Angel (PvD's E-Werk Club Mix) [now scatter]
```

With the one mix's cached rescans instead, the only difference is on `d3252340303c`: "Effy -
Pitched" is already listed before (that is the analysis's 144), so it gains two rows, not three.
The other eight cached mixes are identical row for row, in both views — including the two that
carry second-engine / local-index observations.

**No correct row is lost, in either view, on any of the fourteen mixes.** Nothing was loosened.

### 3.1 Why 154 and not 151

Fusion lists a comment-only row only where no listed episode's span already covers the comment.
The four phantoms on `6651118675bd` spanned 15:27 to 1:23:00 between them, so they silently
blocked every comment-only row in that hour. With them hidden as `scatter`, five comment rows
appear; three name truth tracks Shazam never heard at all ("Bear Witness", "Madman",
"1ofthozedaze" — all in the analysis's cause A list). The same happens on `819e1ee83956` (three
comment rows), where "Take My Hand" comes back at 0:39:38, the row the previous build lost.

### 3.2 Two "wrong" comment rows that are really right

"mallgrab - BB MG (UR)" is the truth's "Mall Grab - BB MG (Soundcloud)" with the artist typed as
one word; "MG - 1ofthozedaze" is a second listener's spelling of the row listed 27 s earlier. The
scorer's matcher pairs neither (the analysis's section 6 already flags this class). No track that
was not played is named. They are crowd rows, marked "from comments".

### 3.3 The one genuinely wrong row that returns

`99b8ceebb852` "Tiësto - Adagio for Strings" (3 windows, 33 s, `possible`), exactly as the analysis
predicted. It used to be buried by the hull of a correct `likely` episode; its three windows sit
in a hole of that episode's evidence, not under its audio. It is a recurring phantom with too
little evidence for any per-mix rule to tell apart; the analysis's cross-library "recurring
phantom" check would be the tool for it. Not loosened, not special-cased.

### 3.4 Duplicates and a scorer artefact

- `6651118675bd` now lists "Rok da House" three times (the `likely` row, the 90 s row the
  phantoms buried, and the catalogue-site line as a comment row) and "Plastyx" twice; all are the
  right track at the right time, under labels the same-track grouping does not join.
- That is also why the by-time `likely` figure reads 3/7 → 2/3: the same three correct `likely`
  rows are listed before and after, but the by-time scorer pairs one truth occurrence with one
  prediction and now pairs "Rok da House" with one of the other two rows. By work it is 3/3.

## 4. Tests

`tests/fixtures/deep/accuracy-fixes.json` — invented names on the REAL window positions of the
evidence the analysis lists (the smeared phantom and the two tracks it buried; the gapped phantom
and the two tracks in its hole; the stretched correct row with and without its core; the
edge-comment episode and its next track; the two-window rows and their solid row), including which
windows carried an offset anchor in the cached run, because that is what makes the phantom
`likely`. `tests/test_accuracy_fixes.py` — 21 tests through the real Shazam converter, comment
parser, relations pass, fusion and presentation.

Each fix was neutralised in turn (its constants set so the old behaviour returns) and the file
re-run:

| Neutralised | Result | Tests that fail |
|---|---|---|
| Fix 1 | 3 failed, 18 passed | smeared `likely` is scatter and buries nothing; a listed `likely` buries only under proved audio; the stretched row without a core |
| Fix 2 | 6 failed, 15 passed | the two-window rows are listed; the four near-misses (they assert the rule is live in the same mix); page view equals per-episode view |
| Fix 3 | 1 failed, 20 passed | the edge answer is not a contradiction |

Near-misses that must stay hidden: a fragment UNDER a phantom's run (still `buried`); the
stretched row with a 39 s core (`scatter`); an answer mid-track and one 25 s from the end (still
`contradicted`); the same edge answer over a 39 s episode (still `contradicted`); a one-window row
by the solid artist; two windows by another artist; the solid track's radio-edit label and its
shorter-title label (checked by mutation: both are lifted if the same-track refusal is removed);
a same-work row under another name; a `possible` backer however long; a `likely` badge bought by
a comment on a 36 s row; a two-window row that fusion buried. Must stay listed: the stretched
correct row with its 57 s core; a smeared row with a comment on one of its windows; and with the
floor off nothing is marked.

## 5. Not done, and observations

- `PAGE_VERSION` and `algorithm_version` — done, section 7.
- One phantom `likely` row remains (`d3252340303c` "Sandstorm"), and so do the `possible` phantom
  rows the analysis lists ("Kernkraft 400", "De Barrio", "Adagio", "Airwave" across four sets);
  per-mix evidence cannot separate them. Observation for whoever takes the cross-library check: on
  the cached data a phantom's windows almost never carry a reliable offset anchor (1 of 5, 1 of 13)
  while a real track's nearly all do (10 of 10) — a cheaper signal than a library-wide count, not
  measured beyond these three episodes and not used here.
- The seven mixes without truth can only be diffed; the twelve rows that appear on `819e1ee83956`
  were all hidden by one phantom's 45-minute hull and cannot be labelled here.
- The harness is a set of scratch scripts, not a committed tool.

## 6. Required command outputs

### 6.1 Full suite, in foreground shards covering every collected file

`uv run pytest --collect-only -q -p no:cacheprovider`:

```text
1970/2067 tests collected (97 deselected) in 9.28s
```

Each shard is
`uv run pytest -q -p no:cacheprovider <files>`, run one at a time in the foreground and waited
for. No `IDEA_ENGINE_SHAZAM` was exported around the suite.

| Shard | Files | Result |
|---|---|---|
| 1 | `tests/test_{a,c,d,e,f,g,h,i,l}*.py` (includes the new file) | `231 passed, 1 warning in 78.20s (0:01:18)` |
| 2 | `tests/test_phase0a*.py test_phase0b*.py test_pa*.py test_pl*.py test_pr*.py` | `212 passed, 1 warning in 157.25s (0:02:37)` |
| 3 | `tests/test_phase1a*.py test_phase2*.py test_phase3*.py` | `155 passed, 1 warning in 255.10s (0:04:15)` |
| 4 | `tests/test_phase1b*.py test_stage3*.py` … `test_stage9*.py` | `415 passed, 1 skipped, 70 deselected, 1 warning in 82.54s (0:01:22)` |
| 5 | `tests/test_sc*.py test_se*.py test_stage1_*.py test_stage10_*.py test_stage2*.py` | `275 passed, 23 deselected, 1 warning in 133.61s (0:02:13)` |
| 6 | `tests/test_t*.py` | `303 passed, 1 skipped, 4 deselected, 1 warning in 134.22s (0:02:14)` |
| 7 | `tests/idea_web/test_{a,b,c}*.py` | `1 failed, 119 passed, 1 warning in 91.96s (0:01:31)` |
| 8 | `tests/idea_web/test_{f,h,l,o,p,w}*.py` | `257 passed, 1 warning in 246.35s (0:04:06)` |

231 + 212 + 155 + 416 + 275 + 304 + 120 + 257 = **1970**, the collected total. The one failure is
the known flake `tests/idea_web/test_auth.py::test_every_state_changing_route_needs_the_sessions_
synchroniser_token`; re-run alone: `1 passed, 1 warning in 3.84s`. No shard went near the
ten-minute limit. After the shards, one docstring in `exports.py` was reworded (no code); shard 1
(`231 passed … in 70.99s`) and shard 4 plus `test_pr*.py test_sc*.py` (`506 passed, 1 skipped, 70
deselected … in 124.34s`) were run again on the final tree. The golden Local Free test (`tests/test_golden_local_free.py`, shard 1) passes
unchanged; nothing under `profiles/` or `tests/golden/` was touched.

### 6.2 `uv run ruff check .`

```text
All checks passed!
```

### 6.3 `uv run ruff format --check .`

```text
402 files already formatted
```

### 6.4 `uv run python scripts/audit_fixtures.py`

```text
audited 533 files
fixture audit passed
```

### 6.5 `git diff --stat` (tracked files; the three new files are untracked)

```text
 src/id_detector/fuse/episodes.py   | 102 +++++++++++++++++++++++++++++-------
 src/id_detector/present/exports.py | 103 ++++++++++++++++++++++++++++++++++++-
 2 files changed, 185 insertions(+), 20 deletions(-)
```

### 6.6 `git status --short`

```text
 M src/id_detector/fuse/episodes.py
 M src/id_detector/present/exports.py
?? docs/reviews/build-local-accuracy-fixes.md
?? tests/fixtures/deep/accuracy-fixes.json
?? tests/test_accuracy_fixes.py
```

### 6.7 `git status --short -- data work`

```text
```

(empty)

### 6.8 Corpus score headlines (verbatim, `scripts/score_corpus.py --print`)

Before, rescans off (original code):

```text
Of the 607 tracks the tool found, the presentation floor hid 120 as buried, 28 as contradicted, 29 as scatter, 222 as short, leaving 208 listed; it named 143 of the 218 distinct tracks actually played (work recall 65.6%); 143 of the 193 distinct tracks it listed were really played (work precision 74.1%); and 63 of the 69 it marked 'likely' or better were right (likely precision 91.3%)
```

After, rescans off:

```text
Of the 613 tracks the tool found, the presentation floor hid 53 as buried, 27 as contradicted, 34 as scatter, 277 as short, leaving 222 listed; it named 154 of the 218 distinct tracks actually played (work recall 70.6%); 156 of the 204 distinct tracks it listed were really played (work precision 76.5%); and 63 of the 64 it marked 'likely' or better were right (likely precision 98.4%)
```

Before, with the one mix's cached rescans (reproduces the analysis's baseline exactly):

```text
Of the 615 tracks the tool found, the presentation floor hid 125 as buried, 28 as contradicted, 30 as scatter, 222 as short, leaving 210 listed; it named 144 of the 218 distinct tracks actually played (work recall 66.1%); 145 of the 195 distinct tracks it listed were really played (work precision 74.4%); and 62 of the 68 it marked 'likely' or better were right (likely precision 91.2%)
```

After, with the one mix's cached rescans:

```text
Of the 621 tracks the tool found, the presentation floor hid 54 as buried, 27 as contradicted, 35 as scatter, 282 as short, leaving 223 listed; it named 154 of the 218 distinct tracks actually played (work recall 70.6%); 157 of the 205 distinct tracks it listed were really played (work precision 76.6%); and 62 of the 63 it marked 'likely' or better were right (likely precision 98.4%)
```

## 7. Reaching existing mixes

### 7.1 What was found first

- **Every one of the owner's fourteen cached mixes is a PRE-BUNDLE result** (flat `fuse/` and
  `present/` files, no `present/bundles`, no compatibility stamp). The compatibility lookup only
  reads bundles, so a version bump ALONE would never have reached them: `idea analyse` on such a
  mix starts a whole new analysis today, bump or no bump.
- For bundle-era results a plain bump is worse than useless for a paid run: the stale result stops
  being served and the pipeline falls through to a NEW analysis — a new reservation and a new AudD
  sweep for Deep, and live Shazam requests for any expired cache entry for Free.

So the bump is paired with a dedicated **offline re-fusion** (`src/id_detector/refusion.py`).

### 7.2 What was built

- `recipes.py`: `FUSION_VERSION = 3`; Free is `fusion:3`, Deep is `targeting:1,fusion:3`.
- **Re-fusion.** A stored result is re-fused from the evidence ITS OWN fusion named: the
  completion sidecar of its final `episodes.genN.json` lists every observation file (primary,
  secondary, local index — every generation), the window files and `hints/hints.jsonl`, each with
  its SHA-256. They are read back, hash-checked, and fused by today's `build_identity_graph` /
  `build_episodes` IN MEMORY with the stored recipe's corroboration thresholds. It never calls a
  provider, never fetches, never reads or writes a recognition cache, never reserves or spends,
  and never writes to the mutable `fuse/` tree, the legacy `present/` files or the old bundle.
  If an input is missing or its hash differs, or the stored scores came from a calibration model
  the caller does not hold, it does nothing and the result stays exactly as it was. Window
  records are the one tolerated absence (retention collects `windows/` at once): the scanned
  supports are then rebuilt from the answered observations, which carry the same intervals.
- **Publication.** The re-fusion seals its OWN frozen run (`fuse/runs/refuse0003-<hash>/`:
  `episodes.json`, `presentation-identities.json`, `refusion.json` provenance with every input's
  hash, and the carried `shazam-observations.json` so a later Deep can still reuse a Free sweep),
  then publishes a normal immutable bundle through `publish_result`. The run id is deterministic
  (an interrupted pass resumes into the same run) and sorts after any hex or `legacy-` id and
  before `refuse0004-…`, so `present/current`, `result_dir` and the library index move on by the
  existing ordering rule. The old bundle and its frozen run stay byte-for-byte where they were:
  superseded, not corrupted or orphaned. The new manifest carries a `refusion` block (source run,
  source bundle, fusion version); no other manifest gains a key.
- **Compatibility (`compat.py`).** `serves()` is unchanged: a `fusion:2` result is never served
  as it is. New: `fusion_stale_only` (same recipe, same adapters, same non-fusion components such
  as `targeting:1`, an OLDER `fusion:N`), `serves_once_refused`, `find_result(…,
  fusion_stale=True)`, and `same_evidence` so a Deep request may still reuse a `fusion:2` Free
  sweep as recognition evidence. `analysis_key` hashes the recipe id, which hashes the version, so
  a stored Free key can never equal today's; the lookup compares `non_recipe_key` (unchanged) and
  the recipe by name instead, and the re-fused bundle is stamped with the request's own key.
- **`analyse` / the worker (`pipeline.py`).** When no stored result serves and `--refresh` was not
  asked for, the bundle that WOULD serve once re-fused is re-fused and then served through the
  existing compatible-result branch. That branch sits before the breaker check, the reservation
  and every dispatch, so THIS run's spend is zero by the same code that makes a cache hit zero.
- **Opening (`idea_web/server.py`).** `refresh_stale_pages` — the start-up pass `idea serve` runs
  before it answers its first request — now re-fuses each stale current result first (bundle or
  pre-bundle), under the media lock (a busy media is skipped: its run publishes a current result
  itself), then refreshes pages as before. Nothing was added to any GET route; a test proves a GET
  leaves a stale result exactly as it is.
- **`PAGE_VERSION` 24 → 25** in `src/id_detector/present/page.py`. That file is not on the list of
  the other session's files (`playlists/**`, `tests/test_playlists.py`, `README.md`, `idea.cmd`,
  `present/theme.py`); it is a one-line change. Stale pages still refresh only in the start-up
  pass or when a worker publishes — the 4a-ii rule is untouched.

### 7.3 Limits, stated plainly

- A PRE-BUNDLE mix is reached by OPENING (`idea serve` / `idea.cmd` start-up), not by `idea
  analyse`: it has no compatibility stamp to prove which inputs produced it, so `analyse` treats
  it as it always has (a new analysis). Its re-fused bundle is stamped with no compatibility
  either, exactly as today's page-refresh republish does. Not loosened.
- Re-fusion uses the hints the stored run fused, not a fresh parse. On one set an old parse left
  a "53." list number inside a hint's artist field; the page still reads "MPH — My Mind", but the
  corpus scorer picks that spelling and counts the (correct) `likely` row wrong — hence 62/64
  below against the harness's 63/64 (which re-parsed hints with today's parser).
- The first start-up after upgrading takes longer once: 14 mixes re-fused in 21-35 s here.
- If a source alias's media is locked by another running job, or the recorded evidence is
  damaged, `analyse` falls through to a new analysis as after any other bump.
- The flat `fuse/episodes.json` working copy of an old run is deliberately NOT rewritten, so a
  tool that reads the flat file rather than the published bundle still sees the old fusion.

### 7.4 The golden Local Free file

Regenerated with `uv run python scripts/make_golden.py`; the whole diff was ONE line:

```diff
--- a/tests/golden/local-free/tracklist.json
+++ b/tests/golden/local-free/tracklist.json
@@ -1,5 +1,6 @@
 {
   "achieved": "free",
+  "analysis_key": "a685b2be10eb58cd8ace7cb04fcb2fd5e1aba3d97eba12e062b9ce79f75000ea",
   "duration_ms": 60000,
   "entries": [
```

No row, time, badge or count moves: on that fixture the three fixes and the hint fix change
nothing. The one line is not a version field either — it is an identity field a later cycle
started writing (it is in the golden test's own `IGNORED_KEYS`), whose value derives from the
recipe id. Since that is strictly "something else", and the committed golden needs no change to
pass, **the regenerated file was discarded and the committed golden is untouched** (the status of
`tests/golden` is clean). `tests/golden/invocation_journal_entry.json` still says `fusion:2`: it
is a schema sample of a historical journal line, read only by a parsing test.

### 7.5 Tests (each fails when its change is neutralised)

`tests/test_refusion.py` (9) drives the REAL pipeline with the scripted fakes over a generated
300 s sweep: a phantom label in two runs with a 114 s hole, two real tracks inside the hole. The
first run is made under the old rules and stamped `fusion:2`; it lists the phantom only.

| Test | What it proves |
|---|---|
| `analyse_re_fuses_a_fusion_2_result_without_a_single_provider_request` | second `analyse`: exit 0, **fake Shazam 0 requests, fake AudD 0 calls**, decode forbidden, tracklist goes from {phantom} to {phantom, both real tracks}, manifest `fusion:3` + `refusion`, every byte of the old bundle, its frozen run, `recognise/`, `windows/` and flat `fuse/` unchanged, `present/current` moved, third `analyse` is a plain cache hit |
| `a_deep_result_is_re_fused_from_its_recorded_attempts_and_reserves_nothing` | a Deep `fusion:2` result, re-requested with `max_usd_e2 = 0` (any reservation would be refused): exit 0, AudD 0, Shazam 0, both engines' recorded observation files re-fused, the attempt ledgers and `recognise/` byte-identical, any journal line at zero |
| `the_start_up_pass_re_fuses_stored_results_offline[bundle / pre-bundle]` | `refresh_stale_pages` with every socket refused: 1 result, the three rows, page stamp 25, flat files untouched, second pass 0, no socket attempt |
| `a_get_never_re_fuses_or_publishes` | library and result-page GETs leave `present/` and `fuse/` byte-identical |
| `a_result_whose_recorded_evidence_changed_is_left_exactly_as_it_is` | one byte added to an observation file: nothing is published |
| `collected_window_records_do_not_stop_a_re_fusion` | `windows/` removed: same three rows |
| `a_fusion_2_result_is_stale_for_serving_and_reusable_for_recognition`, `find_result_names_the_stale_bundle_only_when_asked_for_it` | the contract table: never served as is; stale only when ONLY fusion moved (not `targeting`, adapters, a future version, other inputs, a partial status) |

`tests/idea_web/test_refusion_money.py` (1) — the money authority of `bb57b70`: a Deep job through
the real `LocalWorker`, its bundle re-stamped `fusion:2`, then a second job: succeeded, AudD 0
calls, no "recognising" in its log, **no new row in `run_dispatches` or `run_reservations`**, none
for the run, exactly one settlement row at 0 reserved / 0 spent that equals its projected journal
line and names the re-fused bundle; the first run's settlement still holds its real spend.

Neutralised and re-run: with the stale lookup switched off, 5 of the 10 fail (the two `analyse`
tests, the contract table, the lookup test and the money test); with the start-up pass reduced to
its old page-only form, the two start-up tests fail. Pinned assertions updated from `fusion:2` to
`fusion:3`: `test_phase0a_money.py` (5), `test_phase0a_status.py` (2), `test_phase1a_compat.py`
(3), `test_phase1b_targeting.py` (4).

### 7.6 Re-measured through the NORMAL path

A copy of the whole of `work/` (every file except audio; 26,374 files) was placed in a short temp
directory outside the repo, and the owner's open path was run on it:
`idea_web.server.refresh_stale_pages(work_copy, AppConfig())`, the function `idea serve` calls
before serving — with `socket.connect` replaced by a counter that refuses. No `--refresh`, no
harness fusion.

```text
refresh_stale_pages -> 14 results brought up to date in 35.2s
socket connection attempts: 0
   release1-boomtown-mix: 44 page rows
   release1-redo-of-best-set: 27 page rows
   release1-new-mix-jan-24th: 31 page rows
   release1-final-new-set-christmas: 22 page rows
   release1-mall-grab-boiler-room-melbourne-22: 28 page rows
   release1-mph-youtube-set: 40 page rows
   release1-dj-heartstring-youtube-set: 21 page rows
page rows on the seven published tracklists: 213
second start-up pass -> 0 ; socket attempts: 0
```

The page-row counts equal the harness's "After" column in section 1 mix for mix. The published
frozen runs, scored with `scripts/score_corpus.py`:

```text
Of the 613 tracks the tool found, the presentation floor hid 53 as buried, 27 as contradicted, 34 as scatter, 277 as short, leaving 222 listed; it named 154 of the 218 distinct tracks actually played (work recall 70.6%); 156 of the 204 distinct tracks it listed were really played (work precision 76.5%); and 62 of the 64 it marked 'likely' or better were right (likely precision 96.9%)
```

**So the owner sees 154/218 the next time he starts the app — no `--refresh`, no provider call,
no network.** Listed 222, precision 156/204; `likely` 62/64 for the scorer-spelling reason in
7.3 (the two "wrong" `likely` rows are the remaining "Sandstorm" and the correct "My Mind").
Nothing under the real `work/` or `data/corpus/` was modified (no file there is newer than the
start of this work).

### 7.7 Command outputs (final tree)

`uv run pytest --collect-only -q -p no:cacheprovider` → `1980/2077 tests collected (97 deselected)`.
Foreground shards, one at a time, each waited for:

| Shard | Files | Result |
|---|---|---|
| 1 | `tests/test_{a,c,d,e,f,g,h,i,l}*.py` | `231 passed, 1 warning in 70.16s` |
| 2 | `tests/test_phase0a*.py test_phase0b*.py test_pa*.py test_pl*.py test_pr*.py` | `212 passed, 1 warning in 136.58s` |
| 3 | `tests/test_phase1a*.py test_phase2*.py test_phase3*.py` | `155 passed, 1 warning in 162.72s` |
| 4 | `tests/test_phase1b*.py test_stage3*.py` … `test_stage9*.py` | `415 passed, 1 skipped, 70 deselected, 1 warning in 86.32s` |
| 5 | `tests/test_r*.py test_sc*.py test_se*.py test_stage1_*.py test_stage10_*.py test_stage2*.py` | `284 passed, 23 deselected, 1 warning in 162.28s` |
| 6 | `tests/test_t*.py` | `303 passed, 1 skipped, 4 deselected, 1 warning in 128.20s` |
| 7 | `tests/idea_web/test_{a,b,c}*.py` | `120 passed, 1 warning in 86.02s` |
| 8 | `tests/idea_web/test_{f,h,l,o,p,r,w}*.py` | `258 passed, 1 warning in 245.52s` |

231 + 212 + 155 + 416 + 284 + 304 + 120 + 258 = **1980**, the collected total; 0 failed, no flake
fired this time. The golden Local Free test passes against the untouched committed golden.

```text
uv run ruff check .                      -> All checks passed!
uv run ruff format --check .             -> 405 files already formatted
uv run python scripts/audit_fixtures.py  -> audited 533 files / fixture audit passed
```

Status of the tree (short form, untracked files listed):

```text
 M src/id_detector/compat.py
 M src/id_detector/fuse/episodes.py
 M src/id_detector/pipeline.py
 M src/id_detector/present/bundles.py
 M src/id_detector/present/exports.py
 M src/id_detector/present/page.py
 M src/id_detector/recipes.py
 M src/idea_web/server.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_status.py
 M tests/test_phase1a_compat.py
 M tests/test_phase1b_targeting.py
?? docs/reviews/build-local-accuracy-fixes.md
?? src/id_detector/refusion.py
?? tests/fixtures/deep/accuracy-fixes.json
?? tests/idea_web/test_refusion_money.py
?? tests/test_accuracy_fixes.py
?? tests/test_refusion.py
```

Diff stat (tracked files): `12 files changed, 417 insertions(+), 41 deletions(-)`. The status of
`data` and `work` is empty.

## 8. Fix pass (Codex)

Finished 2026-09-21 from the paused, uncommitted tree. I first read the handoff, this report, the
round-1 review and the miss analysis, then compared the existing diff before editing. The paused
builder had already landed most of the review response: strict recorded digests and same-byte
parsing, the offline stale lookup, an explicit non-spending stale-result status, the safe refusal
of Deep re-fusion, 12-second burial-run continuity, edge-correction handling, locked page refresh
with distinct `busy` / `unrebuildable` outcomes, pointer-following backup code, and corresponding
draft tests. What was still incomplete was important: production still spent up to three seconds
re-fusing (and always enumerated the whole library) before readiness; backup only followed
`present/current` for media SQLite had already discovered; two old server tests still required
refresh before the first GET; and no final measurement, mutation proof or full suite had run.

### 8.1 Round-1 finding by finding

1. **Never silently re-pay (P0).** `run_analysis` looks for a fusion-stale result before hints,
   breaker checks, reservation or dispatch. `_refuse_stored` raises a plain `NotRebuildable`; the
   caller writes one zero-money failed settlement and returns an explicit `stale_result` message
   saying nothing was spent or sent and naming `--refresh` as the separate action required for a
   fresh analysis. Deep is refused even with intact evidence because its old secondary target set
   is not proved. Tests cover intact/missing/mismatched Deep evidence, missing/mismatched Free
   evidence, and all three Deep cases through the real `LocalWorker`: zero provider calls,
   reservations and dispatches, one zero settlement, original paid settlement unchanged.
2. **Readiness (P0).** Production's readiness budget is zero. The deadline is checked before even
   enumerating media, and discovery plus per-mix work runs on the `idea-upkeep` daemon thread. One
   exception is recorded and the pass continues. The regression injects 24 mixes at 0.6 s each,
   blocks the first re-fusion until `/healthz` answers, asserts discovery ran on `idea-upkeep`,
   confirms pages serve during the pass, and checks the damaged mix is reported while 23 finish.
3. **Backup/restore (P0).** A valid `present/current` is now an independent root of the backup
   graph, even when SQLite has no run or bundle row. Under the media lock, backup follows it to the
   verified sealed bundle and that manifest's verified direct child of `fuse/runs`. The test
   deletes every `analysis_runs` and `result_bundles` row, backs up, restores elsewhere, and proves
   the re-fused current pointer, bundle and frozen-run bytes survive.
4. **Input proof (P1).** Every named observation, hint and present window input needs a lowercase
   64-hex digest and equal bytes. An absent window is the sole retention exception and is rebuilt
   from answered observation supports; a present changed window is rejected. Six adversarial
   cases cover missing/malformed/pruned observation digests and changed/malformed window data.
5. **Hints offline (P1).** `find_stale_result` reconstructs the request with the stored bundle's
   own hints-snapshot id before `run_hints`; no connector or HTTP client is constructed. The test
   wires `run_hints`, `httpx.AsyncClient` and every non-loopback socket to fail.
6. **Deep targeting (P1).** The cheap safe option was taken: no Deep bundle is re-fused. Its status
   explains that fusion 2 selected the secondary windows and those choices cannot be claimed as
   fusion 3. Only explicit `--refresh` can start a new paid run.
7. **Burial support (P1).** Confident coverage joins only truly adjacent windows (`< 12 s`), not
   every gap below 60 s. A correct 39-second track wholly inside a 59-second hole stays listed;
   the same test proves a density-2 six-second seam still covers.
8. **Edge corrections (P1).** The 15-second edge allowance applies only to ordinary answers.
   `correction` hints contradict anywhere in the episode span, allowing the corrected work to
   become a crowd-only row. The paired ordinary-answer case remains non-contradictory.
9. **Locking (P1).** Plain page refresh acquires the media lock and rechecks staleness under it.
   `busy` never falls through to either re-fusion or page publication; the next upkeep pass works
   after the holder releases the lock.
10. **Same bytes (P2).** Each evidence file is read once; SHA-256 and model parsing consume that
    one in-memory payload. The regression counts those reads and fails under the former
    hash-path-then-reopen implementation.

### 8.2 Re-measurement through normal start-up

The owner's real `work/` remained read-only. I copied all 26,374 non-audio files (185,750,019
bytes) to `C:\irf-codex-0921b\w`, excluding `.wav`, `.pcm`, `.m4a` and `.webm`, then ran the normal
`make_server` / `run_in_background` path on port 0 with `IDEA_TEST_MODE=1`, `AUDD_API_TOKEN=` and
`IDEA_ENGINE_SHAZAM=off`. A socket guard allowed loopback only. `/healthz` answered in **703 ms**,
with **0** mixes completed at that instant; upkeep then re-fused all **14** mixes in **24.0 s**.
There were no busy, skipped or failed mixes and **zero network/provider attempts**. A second
independent copy produced byte-identical frozen `episodes.json` for all 14 mixes.

Scoring the seven release-1 frozen runs with `scripts/score_corpus.py --print --match auto`,
rescans off:

```text
Before the three accuracy fixes: 143/218 recall; 143/193 work precision; likely 63/69.
First build (60-second burial joins): 154/218 recall; 156/204 work precision; likely 62/64.
Final fix pass (true adjacency): 154/218 recall; 156/207 work precision; likely 62/64.
Of the 613 tracks the tool found, the presentation floor hid 27 as buried, 27 as contradicted,
34 as scatter and 298 as short, leaving 227 listed; work recall 154/218 (70.6%), work precision
156/207 (75.4%), likely precision 62/64 (96.9%).
```

The required 59-second-gap correction removes 26 over-burials and exposes five more page rows
(three additional distinct false works), hence the precision-denominator change from 204 to 207.
The correct-work numerator remains 156 and recall remains 154/218: **no correct row was removed**.
The old `53. MPH - My Mind` stored-hints/scorer spelling artefact remains as documented in 7.3;
nothing was loosened to optimise the score.

### 8.3 Reversion proof for the new regressions

I copied `src/` outside the worktree and neutralised each fix independently. These were expected
failing runs (the real tree stayed unchanged):

```text
60-second burial join + edge allowance for corrections -> 2 failed, 1 passed
readiness pre-deadline discovery check removed          -> readiness test failed (present-server)
current-pointer discovery removed from backup           -> backup/restore failed (no artefacts)
hint connectors restored before stale lookup            -> offline-hints test failed immediately
Deep refusal removed                                    -> intact Deep test returned re-fused, failed
NotRebuildable swallowed (old paid fall-through)        -> missing Deep reached forbidden decode
plain page refresh made unlocked                        -> busy-media test published/returned fresh
malformed digests accepted                              -> 2 malformed-digest cases failed
path hashed, then file reopened                         -> same-bytes test failed (0 byte reads)
```

The section 4 neutralisations still cover the original three accuracy mechanisms. Every test
added by this fix pass therefore fails when the behavior it protects is reverted.

### 8.4 Full suite and required command outputs

`uv run pytest --collect-only -q -p no:cacheprovider`:

```text
2003/2100 tests collected (97 deselected) in 4.18s
```

Every shard ran in the foreground, one at a time, with `IDEA_TEST_MODE=1`; Shazam was **not**
disabled around the suite. Selected plus skipped totals sum exactly to 2,003:

| Shard | Files | Final output |
|---|---|---|
| 1 | `tests/test_{a,c,d,e,f,g,h,i,l}*.py` | `234 passed, 1 warning in 96.70s (0:01:36)` |
| 2 | `test_phase0a*.py test_phase0b*.py test_pa*.py test_pl*.py test_pr*.py` | `212 passed, 1 warning in 185.89s (0:03:05)` |
| 3 | `test_phase1a*.py test_phase2*.py test_phase3*.py` | `155 passed, 1 warning in 176.79s (0:02:56)` |
| 4 | `test_phase1b*.py test_stage3*.py` through `test_stage9*.py` | `415 passed, 1 skipped, 70 deselected, 1 warning in 78.60s (0:01:18)` |
| 5 | `test_r*.py test_sc*.py test_se*.py test_stage1_*.py test_stage10_*.py test_stage2*.py` | `299 passed, 23 deselected, 1 warning in 288.97s (0:04:48)` |
| 6 | `tests/test_t*.py` | `303 passed, 1 skipped, 4 deselected, 1 warning in 142.92s (0:02:22)` |
| 7 | `tests/idea_web/test_{a,b,c}*.py` | `120 passed, 1 warning in 84.54s (0:01:24)` |
| 8 | `tests/idea_web/test_{f,h,l,o,p,r,w}*.py` | `263 passed, 1 warning in 253.23s (0:04:13)` |

No known flake fired. During the first pass, two old synchronous-refresh assertions failed; they
were updated to wait for background upkeep after first proving `/healthz` was available, then
their complete shards were rerun clean as shown. The golden Local Free test passed in shard 1
without changing `profiles/`, `tests/test_golden_local_free.py` or a golden file.

`uv run ruff check .`:

```text
All checks passed!
```

`uv run ruff format --check .`:

```text
406 files already formatted
```

`uv run python scripts/audit_fixtures.py`:

```text
audited 533 files
fixture audit passed
```

`git diff --stat` (tracked files only; new files remain untracked as required):

```text
 src/id_detector/compat.py          | 132 +++++++++++++++++++++++++++-
 src/id_detector/fuse/episodes.py   | 112 +++++++++++++++++++-----
 src/id_detector/pipeline.py        | 168 +++++++++++++++++++++++++++++------
 src/id_detector/present/bundles.py |  44 ++++++++++
 src/id_detector/present/exports.py | 103 +++++++++++++++++++++-
 src/id_detector/present/page.py    |   2 +-
 src/id_detector/present/refresh.py |  40 +++++++--
 src/id_detector/recipes.py         |  30 ++++++-
 src/idea_web/backup.py             |  66 +++++++++++++-
 src/idea_web/server.py             | 175 ++++++++++++++++++++++++++++++++-----
 tests/idea_web/test_parity.py      |   2 +
 tests/test_phase0a_money.py        |  10 +--
 tests/test_phase0a_status.py       |   4 +-
 tests/test_phase1a_compat.py       |   6 +-
 tests/test_phase1b_targeting.py    |   8 +-
 tests/test_stage7_server.py        |   6 +-
 16 files changed, 808 insertions(+), 100 deletions(-)
```

`git status --short -uall`:

```text
 M src/id_detector/compat.py
 M src/id_detector/fuse/episodes.py
 M src/id_detector/pipeline.py
 M src/id_detector/present/bundles.py
 M src/id_detector/present/exports.py
 M src/id_detector/present/page.py
 M src/id_detector/present/refresh.py
 M src/id_detector/recipes.py
 M src/idea_web/backup.py
 M src/idea_web/server.py
 M tests/idea_web/test_parity.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_status.py
 M tests/test_phase1a_compat.py
 M tests/test_phase1b_targeting.py
 M tests/test_stage7_server.py
?? docs/reviews/build-local-accuracy-fixes.md
?? src/id_detector/refusion.py
?? tests/fixtures/deep/accuracy-fixes.json
?? tests/idea_web/test_refusion_money.py
?? tests/idea_web/test_refusion_upkeep.py
?? tests/test_accuracy_fixes.py
?? tests/test_refusion.py
```

`git status --short -- data work`:

```text
```

(empty). No commit or branch was created; the temporary paused-handoff marker was removed so it
will not be swept into the merge.

## 9. Fix pass 2 (Codex)

Finished 2026-09-21 from the same uncommitted worktree, after reading
`review-round2-accuracy.md` in full. Everything the review marked DONE was retained. No branch or
commit was created, the owner `work/` and `data/corpus/` were read-only, and no live provider was
called.

### 9.1 Findings, fixes, tests, and reversion proof

1. **Legacy Deep money hole (P0).** `find_stale_result` now includes a pre-bundle result only from
   its own validated completed `InvocationJournalEntry`. A missing manifest is never evidence of
   Free. The oldest format has no compatibility object, so a validated top-level
   `achieved=deep` is sufficient only to refuse another Deep request; it is never sufficient to
   publish or infer Free. Any legacy Deep result requires explicit `--refresh`, even when optional
   request inputs differ. Discovery now precedes decode, hints, reservation and dispatch.
   `_refuse_stored` also performs the Deep check from this metadata before loading evidence, so
   missing, changed or retention-pruned evidence cannot escape into a new paid run.

   `tests/test_refusion.py::test_a_legacy_stale_deep_result_is_refused_before_any_paid_action`
   covers intact, missing, mismatched and real retention-pruned evidence through direct
   `analyse`; it makes decode and reservation fatal, asserts zero fake AudD/Shazam requests, an
   unchanged attempt ledger and result tree, a zero-money `stale_result` settlement, and also runs
   upkeep first to prove the result is left untouched. The same four cases run through the real
   `LocalWorker` in `tests/idea_web/test_refusion_money.py`; each asserts no new reservation or
   dispatch row, no row for the new run, no provider request, exactly one zero-money settlement,
   and the old paid settlement unchanged. The three earlier bundle cases remain.

   Reversion proof used an isolated `src/` copy: disabling legacy discovery made the direct test
   reach forbidden decode, and the complete worker matrix ended `4 failed, 3 passed` (all four
   legacy jobs wrongly succeeded). The real tree was not modified by the mutation.

2. **Legacy Deep is not Free (P1).** `is_deep` accepts the validated legacy metadata alongside a
   bundle manifest. Background upkeep checks it before `load_run_snapshot`; all four direct legacy
   cases assert `unrebuildable`, the paid explanation and byte-identical stored output before they
   invoke `analyse`. Legacy Free upkeep still uses the mutable result, but never because an absent
   manifest was interpreted as Free.

3. **Complete provenance chain (P1).** Re-fusion now proves `fuse/episodes.json` against its final
   sidecar, requires exactly one recorded generation reference, checks that reference against the
   generation file and its own sidecar, validates every generation upstream (including
   `decode/pcm.json` and `fuse/identities.genN.json`), parses the PCM from the proven bytes, and
   requires both the final and selected episode generations plus duration to agree with the stored
   snapshot before freezing or publishing. Windows remain the sole explicitly prunable input.

   `test_legacy_refusion_proves_the_complete_provenance_chain_before_publication` independently
   changes the final reference, stored generation, PCM and identities, and proves all four are
   `unrebuildable` with no publication. In the isolated old-behaviour mutation all four published;
   together with the legacy and hosted mutations the run ended `6 failed`.

4. **Hosted stale-Free intake (P1).** Hosted `_prepare_intake` performs the offline stale lookup
   immediately after ingest and before decode/hints, reuses the stored hint-snapshot identity, and
   therefore does not construct connectors or clients. The hosted regression drives the real
   intake method with `run_hints`, `httpx.AsyncClient`, and every non-loopback socket wired to fail.
   Moving the lookup back after intake made that regression fail at the connector seam.

5. **Readiness/removal overlap.** The stricter proof made an existing Windows race reproducible:
   background upkeep's open `.media.lock` handle could make a simultaneous recoverable library
   rename return HTTP 500. The local POST now retries that Windows-only `PermissionError` for at
   most one second, then returns 409 for a genuinely busy mix. The affected phase-3 contract and
   the complete shard pass; GET publication and zero-budget readiness remain unchanged.

6. **Adversarial spawned-process P2.** A new three-scenario spawned-process harness was not added.
   It would require a new crash seam and cross-process orchestration in the money-critical path,
   disproportionate to this fix pass. The production locks are non-blocking, alias discovery
   chooses one globally newest stored result, and the existing atomic freeze/pointer, busy-lock,
   retry, backup and byte-preservation tests remain. No deterministic dual-lock ordering was added
   because the normal alias lookup does not choose opposing roots; a failed second lock is already
   a non-spending `stale_result`, not a wait/deadlock.

### 9.2 Normal startup re-measurement

I copied the owner's `work/` outside the repository to `C:\irf-codex-pass2-0921\w`, excluding
audio (`.wav`, `.pcm`, `.m4a`, `.webm`): 26,568 files / 193,309,699 bytes. The normal
`make_server` / `run_in_background` startup path ran on port 0 with `IDEA_TEST_MODE=1`, an empty
AudD token, Shazam off only for this measurement, and a socket guard that allowed loopback only.

```text
/healthz: 985 ms (well inside 30 seconds, while 14 stale mixes existed)
upkeep finished: 32.391 s
updated: 13; page-refreshed only: 1; busy: 0; failed: 0
network/provider attempts: 0
```

The one untouched result was `BENWAL | FULL CLOSING SET | DGTL AMSTERDAM 2026 | 5.4.26`, outside
release-1. Its final sidecar records generation 0 as `4f600d…`, while that generation's own
sidecar records `c38286…`; full-chain proof correctly refused to publish and left the old result
as-is. No release-1 row was lost.

Release-1, rescans off, through the seven normally published frozen runs:

```text
Baseline: 143/218 work recall.
Fix pass 2: 154/218 work recall (70.6%), 156/207 work precision (75.4%),
62/64 likely precision (96.9%); 613 found, 227 listed.
```

Thus the required 143 -> 154 improvement remains. This pass costs no correct release-1 row and
does not loosen provenance to recover the unrelated refused mix.

Both scratch trees (`C:\irf-codex-pass2-0921`, `C:\irf-mut-pass2-0921`) resolved outside the
repository, owner `work/`, and `data/`, and were removed successfully (long paths required the
Windows extended-path spelling). No scratch directory remains.

### 9.3 Full verification outputs

`uv run pytest --collect-only -q -p no:cacheprovider`:

```text
2016/2113 tests collected (97 deselected) in 7.05s
```

Every collected file ran in foreground shards with `IDEA_TEST_MODE=1`; Shazam was not disabled
around the suite. The selected totals are exactly 2,014 passed + 2 skipped = 2,016. The phase-3
shard initially exposed the Windows upkeep/removal race described above; after the bounded fix its
complete rerun is the result below. The two final affected shards were rerun again after the last
legacy guard tightening.

```text
tests/test_[acdefghil]*.py                         234 passed in 112.53s
phase0a/phase0b/paid/playlists/progress/projection 212 passed in 195.36s
phase1a/phase2/phase3                              155 passed in 184.92s
phase1b + stages 3-9                               415 passed, 1 skipped, 70 deselected in 125.80s
refusion/scan/score/semantics/service              159 passed in 375.87s
stages 1/2/10                                      148 passed, 23 deselected in 46.52s
truth/corpus                                       303 passed, 1 skipped, 4 deselected in 180.59s
idea_web accounts/auth/backup/coalescing            120 passed in 120.95s
idea_web followups/headers/local/ops/parity/refusion/worker
                                                   268 passed in 344.37s
```

`uv run ruff check .`:

```text
All checks passed!
```

`uv run ruff format --check .`:

```text
406 files already formatted
```

`uv run python scripts/audit_fixtures.py`:

```text
audited 533 files
fixture audit passed
```

`git diff --stat` (tracked files only; the review and new test/module files remain untracked):

```text
 src/id_detector/compat.py          | 196 ++++++++++++++++++++++++++++++++-
 src/id_detector/fuse/episodes.py   | 112 +++++++++++++++----
 src/id_detector/pipeline.py        | 215 ++++++++++++++++++++++++++++++-------
 src/id_detector/present/bundles.py |  72 +++++++++++++
 src/id_detector/present/exports.py | 103 +++++++++++++++++-
 src/id_detector/present/page.py    |   2 +-
 src/id_detector/present/refresh.py |  40 +++++--
 src/id_detector/recipes.py         |  30 +++++-
 src/idea_web/application.py        |  15 ++-
 src/idea_web/backup.py             |  66 +++++++++++-
 src/idea_web/jobs/worker.py        |  54 +++++++---
 src/idea_web/server.py             | 175 +++++++++++++++++++++++++-----
 tests/idea_web/test_parity.py      |   2 +
 tests/test_phase0a_money.py        |  10 +-
 tests/test_phase0a_status.py       |   4 +-
 tests/test_phase1a_compat.py       |   6 +-
 tests/test_phase1b_targeting.py    |   8 +-
 tests/test_stage7_server.py        |   6 +-
 18 files changed, 988 insertions(+), 128 deletions(-)
```

`git status --short`:

```text
 M src/id_detector/compat.py
 M src/id_detector/fuse/episodes.py
 M src/id_detector/pipeline.py
 M src/id_detector/present/bundles.py
 M src/id_detector/present/exports.py
 M src/id_detector/present/page.py
 M src/id_detector/present/refresh.py
 M src/id_detector/recipes.py
 M src/idea_web/application.py
 M src/idea_web/backup.py
 M src/idea_web/jobs/worker.py
 M src/idea_web/server.py
 M tests/idea_web/test_parity.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_status.py
 M tests/test_phase1a_compat.py
 M tests/test_phase1b_targeting.py
 M tests/test_stage7_server.py
?? docs/reviews/build-local-accuracy-fixes.md
?? src/id_detector/refusion.py
?? tests/fixtures/deep/accuracy-fixes.json
?? tests/idea_web/test_refusion_money.py
?? tests/idea_web/test_refusion_upkeep.py
?? tests/test_accuracy_fixes.py
?? tests/test_refusion.py
```

`git status --short -- data work`:

```text
```

(empty). `profiles/`, the golden Local Free file, playlist files, and all prohibited accuracy and
calibration sources remain unchanged.

### Recorded follow-ups

- The generation-sidecar follow-up was small enough to build in this pass: re-fusion now requires
  at least one recorded window input, exactly the selected generation's identity input category,
  and the PCM record, rather than merely validating whichever upstream keys remain.
- The measured phantom-core and same-artist heuristic limitations remain known accuracy risks.

## 10. Fix pass 3 (Codex)

Finished 2026-09-21 in the same uncommitted worktree after reading
`review-round3-accuracy.md` in full. No branch or commit was created. The owner `work/` and
`data/corpus/` were read-only; both measurement/reversion copies were outside the repository and
were deleted afterwards. No live provider or non-loopback network call was made.

### 10.1 Findings, fixes, tests, and reversion proof

1. **Legacy Deep survives upkeep (P0).** `Refusion` now tells upkeep when even a page-only
   publication is unsafe. A legacy Deep result and an unsafe legacy journal return
   `may_refresh_page=False`, so `Upkeep.run_all` records the skip and does not turn the flat result
   into a compatibility-less bundle. `find_stale_result` now considers flat legacy provenance
   whether or not page-only/unrelated bundles also exist. The page-24 regression runs full upkeep,
   proves byte-identical publication, requests Deep directly, deliberately creates a page-only
   bundle, and requests Deep again; both requests stop before decode/reservation with zero fake
   provider calls. The existing real-`LocalWorker` money-authority case now creates its intact
   legacy page at `PAGE_VERSION=24`, runs full upkeep, and then asserts no new reservation,
   dispatch, provider request, or nonzero settlement.

2. **Unreadable metadata fails closed (P0).** A flat legacy result is detected independently of
   its journal. Missing, empty, malformed, truncated, invalid or duplicate/conflicting invocation
   association returns an explicit unsafe marker, never an inferred Free recipe. Direct analyse,
   real `LocalWorker`, and upkeep all stop and preserve the result until `--refresh`. The parser
   also strictly recognizes the complete older `status=succeeded` schema used by the owner; for
   that schema, the selected generation sidecar's actual provider inputs establish Deep (AudD) or
   Free provenance. Mutable fusion output without a published flat page is not treated as a legacy
   result, so interrupted runs still resume normally.

   `test_unreadable_legacy_metadata_blocks_direct_deep_and_upkeep` covers all four requested damage
   shapes through direct analyse and upkeep. The four corresponding
   `test_unreadable_legacy_metadata_stops_the_real_worker_before_money` cases assert zero provider
   calls, unchanged reservation/dispatch counts, no rows for the blocked run, a zero-money
   settlement, and unchanged publication.

3. **Spawned-process harness (P2).** `ProcessLock` maps every `.media.lock` for one media key to one
   work-root lock. Source aliases therefore cannot hold two media locks, eliminating a reversed
   dual-lock order. The spawned harness races two re-fusions, kills one process after its frozen
   run is sealed but before pointer publication and resumes it in a new process, then holds the
   first source alias while a second process probes the other. It proves one immutable result,
   recovery without a premature pointer, and `locked` across aliases.

4. **Expected provenance categories.** `load_fusion_inputs` now requires window, PCM and selected
   generation identity categories. Three independent sidecar-key deletion regressions remain
   `unrebuildable` and publish nothing.

5. **Reversion proof.** An isolated `C:\irf-mut-pass3-0921\src` copy removed all five production
   changes while the real tree stayed untouched. Every new selected case failed:

```text
13 failed, 40 deselected, 1 warning in 98.99s
```

This is one failure for page-24 upkeep/bundle suppression, four direct/upkeep metadata cases, four
real-worker metadata cases, three required sidecar categories, and the two-process/alias harness.

### 10.2 Normal-startup measurement and the 14 legacy mixes

The owner's 14-result `work/` was copied to `C:\irf-codex-pass3-0921\w` outside the repository,
excluding audio. The normal `make_server` / `run_in_background` path used port 0,
`IDEA_TEST_MODE=1`, an empty AudD token, Shazam off only in that measurement process, and a socket
guard allowing loopback only.

```text
/healthz: 1,062 ms (well inside 30 seconds while 14 stale mixes existed)
upkeep finished: 34.812 s
updated: 11; page-refreshed only: 1; skipped: 3; busy: 0; failed: 0
non-loopback/provider attempts: 0
```

The release-1 seven were all among the 11 successful re-fusions. With rescans off, the normal
startup result is unchanged beside the original baseline:

```text
Baseline: 143/218 work recall.
Fix pass 3: 154/218 work recall (70.6%), 156/207 work precision (75.4%),
62/64 likely precision (96.9%); 613 found, 227 listed.
```

No correct release-1 row was lost, and nothing was loosened to recover a row.

**The owner's 14 legacy mixes do not all re-fuse successfully: 11 do.** The stricter, now-valid
legacy provenance proves these two are Deep and therefore must remain untouched until explicit
`--refresh`:

- `Speed Garage & Bass Mix - Holly Olivia (March 26)`
- `Garage Mix - Dec 25`

Their selected generation sidecars name AudD inputs from their own invocation, so treating either
as Free would recreate the paid-credit defect. `BENWAL | FULL CLOSING SET | DGTL AMSTERDAM 2026 |
5.4.26` remains the third skip because its final generation reference disagrees with that
generation's own sidecar; upkeep safely refreshed only its page. The two Deep mixes now silently
stop receiving the offline accuracy re-fusion they previously received, so under the owner's stated
acceptance rule this is a blocker even though all release-1 numbers remain 143 -> 154.

Both `C:\irf-codex-pass3-0921` and `C:\irf-mut-pass3-0921` were resolved and deleted after the
measurement/proof; both paths were verified absent.

### 10.3 Verification outputs

`uv run pytest --collect-only -q -p no:cacheprovider`:

```text
2029/2126 tests collected (97 deselected) in 5.58s
```

Every `test_*.py` file ran in one of eight deterministic foreground shards (file index modulo
eight), with `IDEA_TEST_MODE=1` and without globally disabling Shazam. The initial shards selected
exactly 2,029 tests: 2,022 passed, 2 skipped and 5 failed. One failure was the existing Windows PID
file creation/content race; two fixture expectations still expected unsafe no-journal pages to be
republished; two exposed false legacy detection from mutable fusion output belonging to an
interrupted/bundle-era run. After the two scoped fixes, all five exact cases passed, followed by a
complete affected-path rerun. Effective final total: **2,027 passed + 2 skipped = 2,029**.

```text
shard 1: 350 passed, 1 failed in 130.11s
shard 2: 263 passed, 1 failed, 1 deselected in 180.43s
shard 3: 350 passed, 1 failed in 321.30s
shard 4: 209 passed, 1 skipped, 61 deselected in 171.36s
shard 5: 212 passed, 2 deselected in 174.80s
shard 6: 199 passed, 1 failed, 18 deselected in 117.02s
shard 7: 220 passed, 1 failed, 11 deselected in 287.63s
shard 8: 219 passed, 1 skipped, 4 deselected in 351.20s
five exact reruns: 4 passed, 1 assertion-path failure in 15.85s
final affected suite: 57 passed in 325.34s
```

`uv run ruff check .`:

```text
All checks passed!
```

`uv run ruff format --check .`:

```text
406 files already formatted
```

`uv run python scripts/audit_fixtures.py`:

```text
audited 533 files
fixture audit passed
```

`git diff --stat`:

```text
 src/id_detector/compat.py          | 201 ++++++++++++++++++++++++++++++++-
 src/id_detector/fuse/episodes.py   | 112 +++++++++++++++----
 src/id_detector/jobs.py            |   8 ++
 src/id_detector/pipeline.py        | 222 ++++++++++++++++++++++++++++++-------
 src/id_detector/present/bundles.py | 171 ++++++++++++++++++++++++++++
 src/id_detector/present/exports.py | 103 ++++++++++++++++-
 src/id_detector/present/page.py    |   2 +-
 src/id_detector/present/refresh.py |  40 +++++--
 src/id_detector/recipes.py         |  30 ++++-
 src/idea_web/application.py        |  15 ++-
 src/idea_web/backup.py             |  66 ++++++++++-
 src/idea_web/jobs/worker.py        |  54 +++++++--
 src/idea_web/server.py             | 177 +++++++++++++++++++++++++----
 tests/idea_web/test_parity.py      |  12 +-
 tests/test_phase0a_money.py        |  10 +-
 tests/test_phase0a_status.py       |   4 +-
 tests/test_phase1a_compat.py       |   6 +-
 tests/test_phase1b_targeting.py    |   8 +-
 tests/test_stage7_server.py        |  20 ++--
 19 files changed, 1123 insertions(+), 138 deletions(-)
```

`git status --short`:

```text
 M src/id_detector/compat.py
 M src/id_detector/fuse/episodes.py
 M src/id_detector/jobs.py
 M src/id_detector/pipeline.py
 M src/id_detector/present/bundles.py
 M src/id_detector/present/exports.py
 M src/id_detector/present/page.py
 M src/id_detector/present/refresh.py
 M src/id_detector/recipes.py
 M src/idea_web/application.py
 M src/idea_web/backup.py
 M src/idea_web/jobs/worker.py
 M src/idea_web/server.py
 M tests/idea_web/test_parity.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_status.py
 M tests/test_phase1a_compat.py
 M tests/test_phase1b_targeting.py
 M tests/test_stage7_server.py
?? docs/reviews/build-local-accuracy-fixes.md
?? src/id_detector/refusion.py
?? tests/fixtures/deep/accuracy-fixes.json
?? tests/idea_web/test_refusion_money.py
?? tests/idea_web/test_refusion_upkeep.py
?? tests/test_accuracy_fixes.py
?? tests/test_refusion.py
```

`git status --short -- data work`:

```text
```

(empty). `profiles/`, the golden Local Free file, playlist files, `data/corpus/`, owner `work/`,
and all prohibited accuracy/calibration sources remain unchanged.

## 11. Fix pass 4 (Codex)

Finished 2026-09-21 in the same uncommitted worktree after reading
`review-round4-accuracy.md` in full. No branch or commit was created. The confirmed Deep refusal
and BENWAL provenance refusal remain intact. No live provider call was made; test runs used
`IDEA_TEST_MODE=1` with fake providers. The owner `work/` and `data/corpus/` were not changed.

### 11.1 Canonical sibling-alias lock

`_refuse_stored` now constructs the stored and already-held `ProcessLock` identities and compares
their canonical paths. When two source aliases contain the same media key, both `.media.lock`
paths map to the same work-root lock, so the pipeline reuses its existing ownership instead of
trying to acquire that lock a second time. A genuinely different lock is still acquired and
released around the stored evidence.

Two regressions use identical audio bytes under different local filenames:

- `test_pipeline_re_fuses_stale_result_found_through_a_sibling_source_alias` drives the real
  pipeline and asserts successful publication into the stored alias with zero AudD and zero
  Shazam calls.
- `test_real_worker_re_fuses_a_stale_result_across_source_aliases_without_providers` drives the
  production pipeline through a real `LocalWorker`, asserts a succeeded job and new re-fused
  publication, zero AudD/Shazam calls, and unchanged reservation/dispatch counts.

Reversion proof: the canonical-path comparison was temporarily removed on the real tree, only for
the proof run. Both new cases failed with the false
`another analysis of the same audio is running right now` refusal:

```text
2 failed, 1 warning in 16.15s
```

The fix was restored, and the final focused restoration run included both cases successfully.

### 11.2 Completed upkeep status for the owner

Upkeep reads each valid `ingest/source.json` and records the mix title, not a truncated source/media
hash. `UpkeepReport.owner_status_lines()` is the single wording source for both surfaces. The
`idea serve` CLI prints those lines when background upkeep completes. The local browser initially
shows a calm background-check status and polls `/upkeep/status`; when the pass finishes, the same
summary, titles, exact reasons, `--refresh` remedy and paid-credit warning replace it without a
reload. This remains informational, not an error state. A page-only refresh of a skipped mix such
as BENWAL does not inflate the number of mixes whose results improved.

`test_owner_visible_upkeep_skips` creates a real protected Deep result and a real Free result with
BENWAL's inconsistent final-generation reference, runs upkeep for both, and pins their title/reason
pairs. It then pins the actual 11-improved/3-left status in the read-only browser, live status
endpoint, polling asset and `idea serve` command output. Its exact owner-visible wording is:

```text
Saved-result upkeep complete: 11 mixes improved; 3 deliberately left as they are.
Speed Garage & Bass Mix - Holly Olivia (March 26): The result was left as it is because it is a paid (Deep) result, and the windows its second opinion checked were chosen by the older fusion rules, so it cannot simply be rebuilt.
Garage Mix - Dec 25: The result was left as it is because it is a paid (Deep) result, and the windows its second opinion checked were chosen by the older fusion rules, so it cannot simply be rebuilt.
BENWAL | FULL CLOSING SET | DGTL AMSTERDAM 2026 | 5.4.26: The result was left as it is because its stored records disagree with each other: the final generation reference disagrees with its generation sidecar.
To analyse a skipped mix again, re-run it with --refresh. Warning: re-running a paid (Deep) mix will spend real AudD credit.
```

Reversion proof: title lookup and owner status generation were temporarily returned to the old
truncated-hash/no-status behavior. The new regression failed at its first title assertion:

```text
1 failed, 1 warning in 12.04s
```

Both reporting changes were restored. The focused final restoration run (the two alias tests, the
owner-status test, and the readiness/upkeep regression) then reported:

```text
4 passed, 1 warning in 52.04s
```

### 11.3 Final verification outputs

One complete clean, unsharded `uv run pytest` suite ran in the foreground on the final production
and test tree, with `IDEA_TEST_MODE=1`. This is the suite total to quote; it is not an aggregation
of shards or affected-path reruns:

```text
============================= test session starts =============================
platform win32 -- Python 3.12.14, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\Users\natha\Documents\Music\id-detector\.claude\worktrees\agent-abb9ec8cc9ac490fd
configfile: pyproject.toml
testpaths: tests
plugins: anyio-4.15.0
collected 2129 items / 97 deselected / 2032 selected

============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.claude\worktrees\agent-abb9ec8cc9ac490fd\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=== 2030 passed, 2 skipped, 97 deselected, 1 warning in 2076.95s (0:34:36) ====
```

`uv run ruff check .`:

```text
All checks passed!
```

`uv run python scripts/audit_fixtures.py`:

```text
audited 533 files
fixture audit passed
```

`git diff --stat`:

```text
 src/id_detector/cli.py               |  12 +-
 src/id_detector/compat.py            | 201 +++++++++++++++++++++++++++++-
 src/id_detector/fuse/episodes.py     | 112 ++++++++++++++---
 src/id_detector/jobs.py              |   8 ++
 src/id_detector/pipeline.py          | 229 ++++++++++++++++++++++++++++-------
 src/id_detector/present/bundles.py   | 171 ++++++++++++++++++++++++++
 src/id_detector/present/exports.py   | 103 +++++++++++++++-
 src/id_detector/present/page.py      |   2 +-
 src/id_detector/present/refresh.py   |  40 ++++--
 src/id_detector/recipes.py           |  30 ++++-
 src/idea_web/application.py          |  27 ++++-
 src/idea_web/backup.py               |  66 +++++++++-
 src/idea_web/jobs/worker.py          |  54 +++++++--
 src/idea_web/pages.py                |  45 +++++++
 src/idea_web/server.py               | 222 +++++++++++++++++++++++++++++----
 src/idea_web/templates/home.html     |   1 +
 src/idea_web/templates/index.html    |   4 +-
 tests/idea_web/local_runner_fakes.py |   6 +-
 tests/idea_web/test_parity.py        |  12 +-
 tests/test_phase0a_money.py          |  10 +-
 tests/test_phase0a_status.py         |   4 +-
 tests/test_phase1a_compat.py         |   6 +-
 tests/test_phase1b_targeting.py      |   8 +-
 tests/test_stage7_server.py          |  20 +--
 24 files changed, 1250 insertions(+), 143 deletions(-)
```

`git status --short`:

```text
 M src/id_detector/cli.py
 M src/id_detector/compat.py
 M src/id_detector/fuse/episodes.py
 M src/id_detector/jobs.py
 M src/id_detector/pipeline.py
 M src/id_detector/present/bundles.py
 M src/id_detector/present/exports.py
 M src/id_detector/present/page.py
 M src/id_detector/present/refresh.py
 M src/id_detector/recipes.py
 M src/idea_web/application.py
 M src/idea_web/backup.py
 M src/idea_web/jobs/worker.py
 M src/idea_web/pages.py
 M src/idea_web/server.py
 M src/idea_web/templates/home.html
 M src/idea_web/templates/index.html
 M tests/idea_web/local_runner_fakes.py
 M tests/idea_web/test_parity.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_status.py
 M tests/test_phase1a_compat.py
 M tests/test_phase1b_targeting.py
 M tests/test_stage7_server.py
?? docs/reviews/build-local-accuracy-fixes.md
?? src/id_detector/refusion.py
?? tests/fixtures/deep/accuracy-fixes.json
?? tests/idea_web/test_refusion_money.py
?? tests/idea_web/test_refusion_upkeep.py
?? tests/test_accuracy_fixes.py
?? tests/test_refusion.py
```

`git status --short -- data work`:

```text
```

(empty). `profiles/`, the golden Local Free file, playlist files, `data/corpus/`, owner `work/`,
and all prohibited accuracy/calibration sources remain unchanged. `git diff --check` also produced
no output.

# Build 1b-iii (scorer, part ii) — work-only (time-agnostic) matching for order-only truth

Orchestrator follow-up to the committed `scripts/score_corpus.py` (`1fbdd29`), justified by
`docs/PLAN-v2.md` §6.3 L3 and `data/corpus/release-1/README.md` § "What order only means for
scoring". Everything is uncommitted on `main` for review. No dependency was added;
`docs/PLAN-v2.md`, `profiles/`, `data/corpus/`, fusion, targeting and the breaker are untouched.
No provider was contacted: the script is offline by construction, every test runs on the committed
fixture, every command ran with `AUDD_API_TOKEN=` and `IDEA_ENGINE_SHAZAM=off` exported, the only
CLI started was `idea serve` through the smoke script, and `work/` was only ever READ (the cached
runs `data/local/release-1-runs-free.json` names); the real smoke wrote under `%TEMP%/idea-runs/`.

**The problem.** The committed scorer matches predictions to truth rows by time overlap, and the
four owner truth drafts are ORDER-ONLY (rekordbox playlists seeded with placeholder equal-slice
timings), so a run known to find most of `release1-new-mix-jan-24th`'s 30 tracks scored ~1/30; it
also compared titles strictly, so "Feel For You (Extended Mix)" (truth) never met "Feel For You"
(engine). **Now:** `--match auto|time|work` (default `auto`) matches order-only truth by
normalised work identity alone, through fusion's own normaliser and word-set rule, and the same
mix scores **24/30 (80.0 %) work recall, 24/30 work precision, 10/10 likely precision**; the timed
Mall Grab set keeps its time numbers and gets the work-only ones beside them.

---

## What changed, file by file

- `scripts/score_corpus.py` — `--match {auto,time,work}` (default `auto`). `truth_timing(truth)`
  reads the seed's fingerprint (every row `audible_rule == "manual annotation required"` at the
  equal-slice point `i * duration // n`) → `order-only` / `timed`; `auto` = `work` for order-only,
  `time` otherwise. `work_identity(artist, title)` = `hints.relations._normalise` on each field
  (NFKC, casefold, a parenthesised mix descriptor dropped, non-alphanumerics folded) plus the word
  set of both; `match_works(truth_works, listed)` matches each listed prediction to at most one
  truth row — exact normalised key first, else `fuse.identity._word_sets_corroborate` (one word set
  contains the other with ≥ 2 words, or a single near-spelled long token), closest word set wins,
  ties to the earlier row; a truth row may be named by any number of predictions (a track listed
  twice is not a wrong ID). `WorkCounts` (rows, likely rows, distinct listed works by the identity
  graph's `work_id`, distinct truth works by normalised key; pooled by sum; ratios through the
  scorer's own `_ratio_e4`), `ListedWork`, `WorkMatch`. `score_mix(..., match=)` always builds and
  contract-checks the `PredictionDocument`, writes `predictions.json` and a new `work-match.json`
  (every assignment, for the owner to audit), and runs the benchmark scorer / writes `report.json`
  only when the mix is matched by time. `score_run_list(..., match=)`: the run's `match_mode` is
  `work` if any mix is (the work-only numbers are the only ones every mix has), pooled from
  `WorkCounts`; otherwise the committed pooled-`ScoreState` path. Output gains `match_requested`,
  `match_mode`, `work_precision_e4` (time mode: `identification_work.precision_e4`), a `work_only`
  block (top level and per mix), `counts.timing`, and per mix `timing`, `match_mode`,
  `unmatched_truth` (what was missed), `unmatched_predictions` (what was wrong, with tier and row
  count), `work_match`; in work mode `listed_precision_e4`, `counts.listed`, `report` and
  `l3.thresholds_met` are `null` and `l3.certifiable` is false. `--print` now writes the paragraph
  (which names the mode and why), a work-only secondary line in time mode, then one line per mix
  (numbers, missed, wrong); stdout is reconfigured to `backslashreplace` so an exotic title cannot
  crash a finished score on a cp1252 console. The `--out`-only one-liner shows `listed n/a` in
  work mode. Module docstring rewritten for the two modes.
- `tests/fixtures/corpus-mini/expected.json` — regenerated: the default run now matches by work
  (both fixture truths are placeholder-timed, exactly like the release-1 drafts). Same three
  numbers as before (8000 / 5714 / 6667; the fixture was designed so every time association is
  also a work match), `listed_precision_e4: null`, plus the new fields.
- `tests/fixtures/corpus-mini/expected-time.json` — new: the exact `--match time` output. Its
  three headline numbers and every count are the committed scorer's, unchanged.
- `tests/test_score_corpus.py` — 11 tests added, several re-pointed at `--match time` where they
  assert time-mode semantics (44 total, below).
- `docs/reviews/README.md` — the 1b-iii (scorer ii) row.

### Tests added / changed (`tests/test_score_corpus.py`, 44 total; 789 → 800 in the suite)

New:
- `test_time_mode_numbers_on_corpus_mini_are_unchanged` — the regression the cycle asked for:
  `--match time` reproduces `expected-time.json` exactly and the literal numbers / counts
  (8000 / 5714 / 6667; pooled 4/5, 4/7, 4/6; per mix 10000/6000/10000 and 5000/5000/3333; `l3`)
  are pinned in code, not only in the fixture file.
- `test_headline_numbers_are_pooled_counts_not_a_mean_of_ratios` — now parametrised over both
  modes; the `work_only` per-mix counts also sum to the pooled ones.
- `test_print_in_work_mode_explains_the_mode_and_lists_missed_and_wrong` — the paragraph names
  WORK-ONLY matching and why, the three work numbers, "reported as null", "cannot be judged from a
  work-only score"; no secondary line; the mini-b line is asserted verbatim
  (`Missed: Mini Artist - Iota; Mini Artist - Mu. Wrong: Mini Artist - Kappa [likely].`).
- `test_order_only_truth_is_detected_from_the_seeds_placeholders` — both fixture truths are
  order-only; one real start range, or one real `audible_rule`, makes a truth timed; verification
  alone (placeholders kept) does not; a re-seeded 4-row truth moves its placeholders with `n`.
- `test_work_identity_is_fusions_normaliser_not_a_second_one` — "(Extended Mix)" / case /
  "(Original Mix)" / "(Edit)" fold exactly as `_normalise` folds them; a "(ft. …)" clause and a
  bare "(Extended)" are NOT stripped (the word-set rule absorbs them); swapped artist/title is one
  word set.
- `test_match_works_ignores_time_mix_suffix_case_order_and_featured_artists` — seven listed rows
  against seven truth works: case + suffix (exact after normalising), "&" vs "," plus a feat.
  clause (subset), swapped fields, two candidate rows where the exact key ("Raw") beats the
  word-set one ("Raw Dub"), fusion's "clubgrls"/"clubgirls" near-spelling, a wrong row, and the same
  work listed twice under another version label; exact `assignments`, `WorkCounts(6, 7, 3, 4, 5,
  6, 5, 7)`, headline 7500 / 8333 / 7143, `unmatched_truth`, `unmatched_predictions`.
- `test_match_works_counts_distinct_works_on_both_sides` — a track played twice is one truth work;
  a track listed twice is one listed work and its second row is not wrong; two wrong rows of one
  label collapse to one entry carrying the higher tier; nothing listed gives zeros, not a
  division error.
- `test_work_mode_fixture_titles_differ_only_by_suffix_case_and_feat` — **the cycle's fixture**:
  mini-a's truth re-seeded (in-test, with the seed's own placeholder arithmetic) as
  `Mini Artist feat. Guest - GAMMA`, `Mini Artist - Alpha (Extended Mix)`, `Mini Artist - Omega`
  (never listed), `mini artist - Beta (Original Mix)`, in a different order from the tool's
  listing. Exact counts: work 3/5 listed, 3/4 truth, likely 3/3 → 10000 / 6000 / 7500; missed
  `Omega`; wrong `Delta [possible]`, `Eta [unclear]`; pooled with mini-b 8000 / 5714 / 5714; the
  `work-match.json` audit names each row's truth index; and `--match time` on the same fixture
  scores mini-a's recall and listed precision **0** (strict key, wrong slices) while its
  `work_only` block still says 7500 — the reason the mode exists.
- `test_a_timed_truth_is_matched_by_time_and_still_gets_work_only_numbers` — mini-b given real
  ranges: `auto` → `time`, `report.json` written, headline 5000 / 5000 / 5000 / 3333,
  `thresholds_met` false, the full `work_only` block and `unmatched_truth` beside it.
- `test_a_mixed_run_list_pools_work_only_and_keeps_the_timed_mixs_time_numbers` — one order-only
  + one timed mix: run mode `work`, `counts.timing == {order-only: 1, timed: 1}`, pooled
  8000 / null / 5714 / 6667, `thresholds_met` null; the timed mix's row keeps `match_mode: time`,
  its report and its `counts.listed`; `--print` says "because 1 of the 2 truth file(s) are
  order-only"; forced `--match work` / `--match time` apply to every mix and say so.
- `test_an_unknown_match_choice_is_a_usage_error` — `--match fuzzy` exits 2.

Changed: the gate test asserts the default run is `work` with no `report` and a `work-match.json`;
`test_headline_fields_come_from_the_scorer_report_by_name`, `test_pooled_metrics_sums_states…`,
`test_min_track_ms_zero…` (now also checks work precision moves 4/7 → 4/9), `test_truth_status…`
(also: a verified-but-placeholder truth is still matched by work and is never `certifiable`),
`test_print_mode…` (paragraph, then the work-only line, then one line per mix; the mini-a line
asserted verbatim), `test_print_without_out…`, `test_free_recipe…` and the crowd-row regression
(both modes) were re-pointed at the mode they assert. All other tests are unchanged.

---

## Required command outputs

### 1. `uv run pytest -q`

```text
800 passed, 93 deselected, 1 warning in 188.54s (0:03:08)   # the usual pydub audioop DeprecationWarning
```

(789 before this cycle + 11.)

### 2. `uv run ruff check .`

```text
All checks passed!
```

### 3. `uv run ruff format --check .`

```text
236 files already formatted
```

(`uv run ruff format` was run on the two edited Python files first.)

### 4. `uv run python scripts/audit_fixtures.py`

```text
audited 408 files
fixture audit passed
```

(408 before this report existed; re-run after it was written, since the audit scans `docs/`, it
reads `audited 409 files` / `fixture audit passed` — see § 6.)

### 5a. Phase gate — `uv run pytest tests/test_score_corpus.py -q`

```text
............................................                             [100%]
44 passed, 1 warning in 29.48s
```

### 5b. `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

### 5c. The real smoke (offline; cached runs; read-only on `work/`)

`uv run python scripts/score_corpus.py --run-list data/local/release-1-runs-free.json --out
%TEMP%/idea-runs/release-1-free-work.json --print` — run from Git Bash, so `%TEMP%` was given as
`"$TEMP/idea-runs/release-1-free-work.json"` (the same `C:\Users\…\AppData\Local\Temp\idea-runs\`).
Exit 0. Printed as one paragraph plus one line per mix; wrapped here. Two edits to the paste: the
console rendered "Tiësto" through cp1252 (the JSON has the name right), and one Mall Grab crowd
label begins with a commenter's platform handle, redacted below as the committed-corpus policy
requires.

```text
The free recipe was scored over 5 mix(es) (release1-boomtown-mix, release1-redo-of-best-set,
release1-new-mix-jan-24th, release1-final-new-set-christmas,
release1-mall-grab-boiler-room-melbourne-22) against DRAFT truth (seeded tracklists with
placeholder timings, not yet verified), so these are working numbers, not release numbers.
Matching is WORK-ONLY (time-agnostic) because 4 of the 5 truth file(s) are order-only (a tracklist
still carrying the seed's placeholder equal-slice timings): each listed track is matched to the
tracklist by normalised artist and title alone (mix suffixes, case, word order and featured
artists ignored), never by when it played. Of the 413 tracks the tool found, the presentation
floor hid 107 as buried, 32 as contradicted, 20 as scatter, 117 as short, leaving 137 listed; it
named 100 of the 135 distinct tracks actually played (work recall 74.1%); 100 of the 133 distinct
tracks it listed were really played (work precision 75.2%); and 49 of the 54 it marked 'likely' or
better were right (likely precision 90.7%). L3 asks for likely >= 90.0%, listed >= 80.0% and
recall >= 70.0% for free; listed precision and every timing number need timed truth (reported as
null), so the L3 bar cannot be judged from a work-only score, and the two numbers it can see are
indicative only (a work-only match is looser than a timed one): likely precision 90.7% >= 90.0%;
work recall 74.1% >= 70.0%. Full numbers: C:/Users/natha/AppData/Local/Temp/idea-runs/release-1-free-work.json.

- release1-boomtown-mix [order-only; matched by work]: work-only: recall 35/40 (87.5%),
  precision 35/40 (87.5%), likely 15/16 (93.8%). Missed: Effy - Pitched; Cotto - Murda Sound
  (Original Mix); NOTION, Cameron Hayes - SECRETS (Original Mix); Cesco - Move Too Slow; Chase &
  Status - 5am. Wrong: Darude - Sandstorm [likely]; Fragma - Toca Me (Clubmix) [possible];
  NCKNAME - Tryna Dance [possible]; Zombie Nation - Kernkraft 400 [possible]; 2 Bad Mice -
  Bombscare [possible].
- release1-redo-of-best-set [order-only; matched by work]: work-only: recall 15/18 (83.3%),
  precision 15/24 (62.5%), likely 10/10 (100.0%). Missed: Bklava, bullet tooth - Makes Me (Wanna
  Move); DJ SCHEMA - SELF CONTROL (Extended); Riko Dan, Y U QT - Original Don (Extended). Wrong:
  Mass Medium - Gotta Have It (Extended Mix) [possible]; CHRYSTAL & NOTION - The Days (NOTION
  Remix) [possible]; Tiësto - Adagio for Strings [possible]; Ayla - Ayla (DJ Taucher Remix)
  [possible]; Jupiter 8000 - Inside [possible]; Mears - Be My Lover [possible]; Torres De Lara -
  De Barrio [possible]; Effy & Mall Grab - iluv [possible]; Rollercoaster NL - Come With Me (Raw
  Bounce Mix) [possible].
- release1-new-mix-jan-24th [order-only; matched by work]: work-only: recall 24/30 (80.0%),
  precision 24/30 (80.0%), likely 10/10 (100.0%). Missed: Beau James - 4 Raws Edit; Bushbaby -
  Pumpin Jumpin (Extended Mix); Soul Mass Transit System - Feelin U (Original Mix); Tim Reaper,
  Special Request, OD - Pull Up (Extended Mix); Beau James - 18Hunna; Faster Horses - Get On Ya
  Knees. Wrong: Architechs - Body Groove (feat. Nana) [Mix Mc Version] [possible]; Tiësto -
  Adagio for Strings [possible]; Special Request, ODF & Tim Reaper - Pull Up (Extended Mix)
  [possible]; Headie One - 18HUNNA (feat. Dave) [possible]; Heparyna - WISH YOU WERE MINE REMIX
  [possible]; 4raws - beau james [possible].
- release1-final-new-set-christmas [order-only; matched by work]: work-only: recall 19/23
  (82.6%), precision 19/22 (86.4%), likely 11/11 (100.0%). Missed: Upper90 - I Am Ready 2;
  Testpress - FORZ4; Ollie Lishman - THA WAY; Bushbaby - DESIRE96. Wrong: t e s t p r e s s -
  Forz4 [possible]; Eatmyflesh - Gorejust Summer Mix [possible]; I'm ready - Upper90 [possible].
- release1-mall-grab-boiler-room-melbourne-22 [timed; matched by time]: by time: likely 1/7
  (14.3%), listed 2/19 (10.5%), recall 2/25 (8.0%); work-only: recall 7/24 (29.2%), precision
  7/17 (41.2%), likely 3/7 (42.9%). Missed: - Mall Grab - Winter (UR); - Mall Grab - Mirror
  Break (4/4 Mix) (UR); - Mall Grab - Inside (UR) [and 14 more rows, 11 of them "(UR)", every one
  printed with the leading "- " the finding below explains]. Wrong: [crowd
  handle redacted] Jordon Alexander - Winter [possible]; Rank 1 - Airwave (Radio Vocal Edit)
  [likely]; System F - Out of the Blue [likely]; Darude - Sandstorm [likely]; System F - Out of
  the Blue 2010 (Original Violin Edit) [likely]; Flansie & Mall Grab - No One Else Will
  [possible]; Brandon Allan - HevE Tac (Brandon Allan Remix) [possible]; Long Season Intro Edit -
  Mall Grab [possible]; Be your best when your best is needed - clarkie [possible]; MG - Long
  Season Intro Edit [possible].
```

Pooled JSON (top level): `match_mode: work`, `match_requested: auto`, `likely_precision_e4: 9074`,
`listed_precision_e4: null`, `work_precision_e4: 7519`, `work_recall_e4: 7407`,
`counts.timing: {order-only: 4, timed: 1}`, `counts.work: {correct: 100, predicted: 133, truth:
135, truth_matched: 100}`, `range_claims: 9`, `l3.thresholds_met: null`, `l3.certifiable: false`.
Per mix: boomtown 9375 / null / 8750 / 8750; redo 10000 / null / 6250 / 8333; jan-24th
10000 / null / 8000 / 8000; christmas 10000 / null / 8636 / 8261; Mall Grab (time)
1429 / 1053 / 1176 / 800 with its `report.json` written and `work_only` 4286 / 4118 / 2917.

**Sanity expectation (jan-24th work recall 0.8–0.95, ≈ 27/30).** It lands at **24/30 = 0.80**,
inside the band at its low edge, so the normaliser was investigated before declaring done. The
six misses, read against the "wrong" list:

| Truth row | What the tool listed | Why it did not match |
|---|---|---|
| `Bushbaby - Pumpin Jumpin (Extended Mix)` | nothing | not listed at all (genuine miss) |
| `Soul Mass Transit System - Feelin U (Original Mix)` | nothing | genuine miss |
| `Faster Horses - Get On Ya Knees` | nothing | genuine miss |
| `Beau James - 18Hunna` | `Headie One - 18HUNNA (feat. Dave)` | the engine named the original; the tracklist names the DJ's edit — a different work by every rule, correctly unmatched |
| `Beau James - 4 Raws Edit` | `4raws - beau james` | a crowd label with the compound token `4raws`; fusion's word sets are `{beau, james, 4, raws, edit}` vs `{4raws, beau, james}` — neither contains the other and `4raws`/`4` is not a one-edit spelling |
| `Tim Reaper, Special Request, OD - Pull Up (Extended Mix)` | `Special Request, ODF & Tim Reaper - Pull Up (Extended Mix)` | differs by the single token `od`/`odf`, which fusion's near-spelling rule admits only for tokens of ≥ 4 characters |

So 3 are true misses, 1 is a different work, and 2 are edge cases of the *fuser's* rules
(`_word_sets_corroborate`'s length guard and its whitespace tokenisation). Those two, plus the
Headie One row, are exactly the gap to the "≈ 27" the owner remembered. They were left alone on
purpose: the cycle note says the scorer must not become a second normaliser, and relaxing either
rule changes how fusion attaches crowd hints for every mix — a fusion-cycle decision, recorded
under Findings. The same reading holds for the christmas set's `t e s t p r e s s - Forz4` and
`I'm ready - Upper90` (label quality, not matching).

### 6. `git status --short` and `git diff --stat`

Captured after this report and the README row were written (the report itself gains this section
afterwards; nothing else changed), then the fixture audit was re-run because it scans `docs/`.

```text
$ git status --short
 M docs/reviews/README.md
 M scripts/score_corpus.py
 M tests/fixtures/corpus-mini/expected.json
 M tests/test_score_corpus.py
?? docs/reviews/build-1b-iii-scorer-ii.md
?? tests/fixtures/corpus-mini/expected-time.json

$ git diff --stat
 docs/reviews/README.md                   |   1 +
 scripts/score_corpus.py                  | 631 +++++++++++++++++++++++++++----
 tests/fixtures/corpus-mini/expected.json | 155 ++++++--
 tests/test_score_corpus.py               | 619 +++++++++++++++++++++++++++---
 4 files changed, 1269 insertions(+), 137 deletions(-)
```

Untracked additions: `tests/fixtures/corpus-mini/expected-time.json` (232 lines) and this report.
Every written file was checked to be LF-only (no "CRLF will be replaced" notice this time). The
fixture audit re-run with the report present: `audited 409 files` / `fixture audit passed`.

---

## Plan ambiguities resolved (and how)

1. **How to tell order-only truth apart.** The cycle note offered detection (the seed's
   `audible_rule == "manual annotation required"` plus equal-slice `start_ms_range`) or a new
   `timing: "order-only"` marker the seed writes. Detection was chosen: it needs no contract
   change, no re-seeding of the committed drafts (`data/corpus/` is off-limits this cycle), and it
   keeps working for a verifier who leaves the placeholders in place, which the corpus README
   names as the work-only option. Checked read-only against all five release-1 files: the four
   rekordbox drafts are exact equal-slice placeholders and the Mall Grab set (minute timestamps)
   is not, so the rule separates them with nothing to configure. A marker can still be added later
   without disturbing this (the detector would simply also accept it).
2. **"The same normalisation the fuser uses".** Two private helpers are imported by name —
   `hints.relations._normalise` (the only place fusion strips mix descriptors) and
   `fuse.identity._word_sets_corroborate` (the order-independent rule that attaches crowd hints) —
   plus the scorer's private `_ratio_e4` so the rounding cannot drift. Renaming them public would
   be a drive-by refactor of fusion for the script's benefit; aliasing would be noise; the imports
   carry a comment saying why. Nothing in fusion changed. Note what this implies: "feat./ft."
   clauses are not *stripped* by the shared normaliser (it only drops parenthesised mix
   descriptors); they are absorbed by the word-set containment rule, which is how the fuser treats
   them too. A bare "(Extended)" is likewise not stripped and is absorbed the same way.
3. **What `work_precision_e4` / `work_recall_e4` count.** Distinct works on both sides, mirroring
   the benchmark's `identification_work`: listed works are distinct by the identity graph's
   `work_id` (the tool's own unit of "a track"), truth works by normalised key; the recall
   numerator is truth-side (`truth_matched`), so two listed works naming one truth row inflate
   neither number. `likely_precision_e4` is per listed row (as `tier_work` is in time mode): a
   `likely`-or-better row is right iff it names any truth row, exactly the cycle note's words.
   "Each listed prediction to at most one truth row" is honoured; a truth row may be named by
   several predictions, because the tool listing a track twice is not a wrong ID and an order-only
   truth cannot say which occurrence was which.
4. **Mixed run lists.** The release-1 list has four order-only mixes and one timed one, and the
   cycle's smoke runs it as a whole. In `auto` the run's `match_mode` is `work` whenever any mix is
   order-only (the pooled headline is the work-only score, the only one every mix has); the timed
   mix keeps its own time numbers, `report.json` and `counts.listed` on its row, and its work-only
   numbers are pooled with the others. `--match time` forces the time path on everything (the
   paragraph then says the order-only files' time numbers are not meaningful) — needed for the
   regression pin; `--match work` forces work-only on everything.
5. **L3 in work mode.** `listed_precision_e4` is `null`, so `l3.thresholds_met` is `null` (not
   `false`: the bar is unjudged, not failed) and `l3.certifiable` is false even for verified
   truth, since a work-only score cannot back L3's listed-precision threshold. The paragraph says
   so and shows the two thresholds it can see as "indicative only" (a work-only match is looser
   than a timed one).
6. **What is written per mix in work mode.** `predictions.json` (the contract-checked document)
   and a new `work-match.json` (every listed row → truth index, every truth row → the rows that
   named it, the counts); no `report.json`, because the benchmark scorer's time-based numbers on
   placeholder slices would be noise dressed as a report. Time mode writes all three.
7. **`expected.json` follows the default run**, which on the fixture is now work mode (both
   fixture truths are seeded-style placeholders); `expected-time.json` pins `--match time`. The
   gate ("the scorer on `corpus-mini` reproduces `expected.json` exactly") still holds for the
   default invocation, and the time-mode regression pins the old numbers in code as well as in the
   file.
8. **`--print` is no longer one line.** The cycle asked for per-mix rows listing missed and
   wrong tracks and, for timed truth, a secondary work-only line, so the output is: paragraph,
   [work-only line in time mode], one line per mix. Tests assert the structure and the mini lines
   verbatim.

## Findings for the owner (not changed this cycle)

- **Two fuser rules cost jan-24th two matches** (table above): the ≥ 4-character guard on the
  near-spelling rule (`OD` vs `ODF`) and whitespace tokenisation (`4raws` vs `4 Raws`). Both are
  `fuse/identity._word_sets_corroborate` behaviour shared with crowd-hint attachment; loosening
  them is a fusion decision with a corpus-wide effect, not a scorer one.
- **The Mall Grab truth's artist fields begin with a literal `- `** (`- Mall Grab`,
  `- Fishmans`): the tracklist was bulleted and `truth.py`'s `_TRACKLIST` regex kept the bullet
  as part of the artist. Matching is unaffected (the normaliser folds `-` to a space) but the
  labels print with it; a re-seed from a de-bulleted tracklist, or a bullet-tolerant regex, fixes
  it. Its 25 rows fold to 24 distinct work keys under the shared normaliser (a "(… Mix)"
  descriptor difference), which is why the work-only denominator reads 24.
- **A crowd label carries the commenter's platform handle** (`@… Jordon Alexander - Winter`): the
  hint parser let a leading mention through into the artist field. It reached only the scratch
  output; it is redacted above and must never enter `docs/` or the corpus.
- **Work-only recall on the drafts, Free recipe:** boomtown 87.5 %, redo 83.3 %, jan-24th 80.0 %,
  christmas 82.6 % — consistent with the ~27/30 the owner remembered once the three
  label/edit cases are counted; Mall Grab (the underground stress case) 29.2 %, mostly unreleased
  "(UR)" rows no catalogue can name.

## Not done (with reasons)

- **No change to `hints/relations.py`, `fuse/identity.py` or `truth.py`** — see the findings;
  each would change fusion or seeding behaviour outside this cycle's scope.
- **No `timing: "order-only"` marker in the truth contract** — detection covers the committed
  drafts without touching `data/corpus/` (ambiguity 1).
- **The breaker half of 1b-iii** — still deferred, as in part i.
- **Gate wall time** — `tests/test_score_corpus.py` runs in ~30 s (the benchmark bootstrap per
  time-scored mix); the work-mode runs are fast.

---

## Review + fix pass

Adversarial review of the uncommitted diff (Opus), then the fixes, on `main`. Every gate below was
re-run here, never trusted from the paste. No provider was contacted: every command ran with
`AUDD_API_TOKEN=` and `IDEA_ENGINE_SHAZAM=off` exported, the only CLI started was `idea serve`
through the smoke script, `work/` was READ ONLY (the five cached runs
`data/local/release-1-runs-free.json` names, plus that Mall Grab run's `fuse/` and `present/` files
for the privacy finding), and the real smoke wrote under `%TEMP%/idea-runs/`. `docs/PLAN-v2.md`,
`profiles/`, `data/corpus/` and everything under `src/` are untouched by the review as they were by
the build.

### Findings

**P0-1 — `truth_timing()` called a half-verified truth "timed", so `auto` scored its untimed rows as
misses** (`scripts/score_corpus.py:410-430`, pre-fix). The detector required *every* row to be a seed
placeholder, so a truth with even one real start time read `timed` and `auto` sent it down the time
path — where the rows still on placeholders are matched against an arbitrary equal-slice point, i.e.
as misses. That is precisely what a half-finished `idea truth verify` leaves behind:
`src/id_detector/truth.py:404-438` keeps the seed's `audible_rule` and, when the verifier answers the
"start range START_MS,END_MS (blank keeps draft)" prompt with a blank, the seed's range — while
clearing `draft` either way. Reproduced on the fixture: `mini-a` re-seeded as an order-only tracklist
with **one** row timed scored `0 / 0 / 0` (likely / listed / work recall) where its work-only score is
recall 3/4 (75.0 %), precision 3/5 (60.0 %), likely 3/3 (100.0 %); the `--print` paragraph still said
only "1 of the 2 truth file(s) are order-only", hiding that the other file was 3/4 placeholders. On
frozen, verified truth the same run also reads `l3.certifiable: true` beside those zeros — a published
release number for a mix the tool actually got 75 % right.

**P0-2 — the placeholder fingerprint matched a genuinely timed first track** (found while fixing
P0-1; same lines). Checking only `start_ms_range` against `i * duration // n` makes row 0 of *any*
timed tracklist that begins at 0:00 look like a placeholder, because `0 * duration // n == 0`. The
Mall Grab set is exactly that (1 of its 25 rows), so with the P0-1 fix alone it would have flipped
from `timed` to `partial` and silently lost the corpus's only boundary numbers.

**P1-1 — `match_works()`'s exact-key branch had no emptiness guard, so two meaningless labels scored
as a correct ID** (`scripts/score_corpus.py:461-465`, pre-fix: `truth_key == key`). A truth row
`??? - (Original Mix)` and a prediction `!!! - (Extended Mix)` both normalise away to `("", "")`
(`_normalise` drops the parenthesised mix descriptor and folds `???` to nothing), so the prediction
was counted as a correct identification and inflated work precision, work recall *and* likely
precision. Fusion — the rule the cycle note says to reuse rather than loosen — refuses exactly this:
`hints/relations._identity_key:26-29` returns `None` when either normalised field is empty, and
`fuse/identity._word_sets_corroborate:214-215` requires two words. The scorer's exact branch was the
one place looser than the fuser.

**P2-1 (note, unchanged) — many-to-one matching is by design and is looser than the time path.** Two
*distinct* listed works that both corroborate one truth row are both counted correct
(`works_correct` 2/2 while `truth_matched` is 1/1); verified directly against `match_works`. The cycle
note only asks that each listed prediction match at most one truth row, and the builder recorded the
reading (ambiguity 3), so this is not a violation — but the work-only precision can never fall below
the benchmark's one-to-one `identification_work.precision_e4` on the same rows, which is why the
paragraph's "a work-only match is looser than a timed one" matters. Related: `unmatched_predictions`
is keyed by raw label, so its length need not equal `works − works_correct`, and the same label in two
different cases prints as two entries.

**P2-2 (note) — colliding truth keys shrink the recall denominator.** The Mall Grab set's two mixes of
"Juice" fold to one work key, which is why its denominator reads 24 of 25 rows (already in the
builder's findings).

**P2-3 (note, deliberately not changed) — `l3.certifiable` can still be `true` on placeholder-timed
verified truth if the operator forces `--match time`** (`scripts/score_corpus.py:852`:
`status == "verified" and mode == "time"`). Left as is on purpose: the plan's L3 command (§6.3) passes
no `--match`, so `auto` sends such truth to work mode and `certifiable` is false; and making
`certifiable` depend on the timing heuristic would let one false `partial` reading block a legitimate
release. The per-mix `timing` field records the state either way and the paragraph says the forced
time numbers "are not meaningful". Tightening it would also change 1b-iii part i's committed behaviour
on the fixture (`test_truth_status_...` asserts `certifiable is True` there).

**P2-4 (note) — the two `likely` denominators are not the same population.** Work mode counts every
listed row with tier >= likely; the time path counts only the `evaluable` rows
(`benchmark/scorer._prediction_is_evaluable:539-545` — support not wholly inside a truth `regions`
entry). Every release-1 truth has `regions: []`, so the two agree today.

**P2-5 (note) — `truth_timing` still cannot tell a timed tracklist whose every row lands on both slice
points.** `tests/fixtures/corpus-mini` is built exactly that way, so the fixture is matched by work
under `auto` (which is what `expected.json` now pins) even though its spans are "real". Closing that
needs a `timing:` marker written by `idea truth seed` — `src/id_detector/truth.py` plus a re-seed of
`data/corpus/`, both outside this cycle.

**P2-6 (note, for cycle 3a-ii) — the crowd label carrying a commenter's platform handle.** Confirmed:
it exists **only** in run artefacts under `work/` — `git grep` over the whole tree finds no `@`-led
artist in any tracked file, and the build report redacted it. In the cached Mall Grab run it lives in
`work/<source>/<media>/fuse/identities.gen0.json` as a `text:` node id **and** label
(`@<handle> Jordon Alexander - Winter`, the handle redacted here as the committed-corpus policy
requires, plus a second such label on another crowd row), and in that node's candidate / work `member_nodes` and one `hint_text_match` assertion.
Where it surfaces:

- **Today, in the scorer's own outputs.** `benchmark/corpus._label_for_candidate:137-148` prefers the
  *work*'s `text:` node labels and takes `min()`, and `@` (0x40) sorts before any letter — so the
  handle beats the provider label and reaches `predictions.json`, the new `work-match.json` and the
  `--print` "Wrong:" line. That is where the builder saw it.
- **The page and every export, for a crowd-only row.** `present/exports._candidate_label:61-75`
  prefers non-`text` nodes, so a hint-*supported* row (this one) shows the provider label — the run's
  `present/tracklist.md` reads "Jordon Alexander — Winter (feat. Mall Grab)", clean. But a `hint_only`
  candidate has *only* text nodes and falls through to `min()` over the work's labels, so a handle-led
  label becomes `entries[].artist` in `present/tracklist.json`, the Track cell of `tracklist.md`, the
  `PERFORMER` line of `tracklist.cue`, `tracklist.m3u`, the page rows (`present/page.py:440,462`) and
  `alternatives[].track` / `overlap_labels`. The same run proves the mechanism with a sibling defect:
  one of its four `hint_only` rows exports the artist `FULL TRACK LIST: - Fishmans`.
- **Root cause** is upstream of both: the mention survived into `hint.artist`.
  `hints/parse._artist_title:254` does strip a leading `@mention` (`_MENTION_PREFIX`), so the unit that
  produced this row took a path that cleaning did not cover — most plausibly the multi-line tracklist
  block, which is also what left `FULL TRACK LIST:` in an artist field. Queue for **3a-ii**
  (belt-and-braces on the display side) with a `hints/parse` fix for the source; out of scope here
  (`hints/` and `present/` belong to other cycles).

### Confirmations the review was asked for

- **Order-only detection both ways.** A genuinely timed truth can no longer be read as order-only by
  its first track (P0-2); a partly timed one is no longer read as fully timed (P0-1). Checked
  read-only against all five release-1 truths with the new fingerprint: 40/40, 23/23, 30/30 and 18/18
  placeholder rows for the four rekordbox drafts (`order-only`) and **0** of 25 for Mall Grab
  (`timed`) — the same split the build reported, now for the right reason.
- **One-to-one assignment.** A prediction cannot be matched to two rows (`assignments` maps a listed
  index to a single row). There is no greedy order effect: each prediction picks its own best row
  independently, ties to the earlier row, so the outcome is order-independent and deterministic. A
  truth row matched twice is intentional (P2-1). Truth repeats (`occurrence_index > 0`) collapse to one
  distinct work on both sides — numerator `matched_keys`, denominator `first_row_by_key` — verified
  directly.
- **`likely_precision_e4` in work mode** is per listed row, `index in assignments`: a row is correct
  iff its one matched truth row exists, and a truth row named by several rows is still one
  `truth_matched`, so nothing double-counts across occurrences. Same shape as the time path's
  `empirical_tier_precision_e4["likely"]` (`scorer._populate_tiers:806-821`: tier >= likely, correct
  iff associated).
- **The time mode's numbers on `corpus-mini` are byte-identical to the committed `expected.json`.**
  Diffed key by key against `git show HEAD:tests/fixtures/corpus-mini/expected.json`:
  `expected-time.json` **adds** `match_requested`, `match_mode`, `counts.timing`, `work_precision_e4`,
  `work_only` and, per mix, `timing`, `match_mode`, `work_precision_e4`, `work_only`,
  `unmatched_truth`, `unmatched_predictions`, `work_match` — and changes or removes **nothing**.
- **The shared normaliser is reused, not re-implemented** (`from id_detector.hints.relations import
  _normalise`, `from id_detector.fuse.identity import _word_sets_corroborate`, plus the scorer's own
  `_ratio_e4`), and `git status --short` shows nothing under `src/` — `fuse/` and `hints/` are
  untouched.
- **No release-1 number moved.** The cycle's real smoke re-run on the cached runs after the fixes:
  pooled `likely_precision_e4 9074`, `listed_precision_e4 null`, `work_precision_e4 7519`,
  `work_recall_e4 7407`, `counts.timing {"order-only": 4, "timed": 1}`, `counts.work {correct 100,
  predicted 133, truth 135, truth_matched 100}`, `range_claims 9`, `l3.thresholds_met null`,
  `certifiable false`; per mix boomtown 87.5/87.5/93.8, redo 83.3/62.5/100.0, jan-24th 80.0/80.0/100.0,
  christmas 82.6/86.4/100.0, Mall Grab still `[timed; matched by time]` 14.3/10.5/8.0 with work-only
  29.2/41.2/42.9 — identical to the build report.

### What changed

- `scripts/score_corpus.py`
  - `Timing = Literal["timed", "partial", "order-only"]`; `truth_timing()` now counts placeholder rows
    instead of demanding all of them, and a placeholder row must match the seed's start **and** end
    slice points (`(i + 1) * duration // n`), returning `timed` (no placeholders), `order-only` (all)
    or `partial` (some). Docstring rewritten to say why both bounds are read.
  - `score_mix()`: `auto` takes the time path only for `timed` truth, so a partly timed file is matched
    by work like an order-only one.
  - `_matching_note()`: when any file is `partial`, the paragraph says "N of the M truth file(s) still
    carry the seed's placeholder equal-slice timings on some or all rows (X order-only, Y only partly
    timed)"; the forced-`--match time` sentence likewise says "on some or all rows". The wording for a
    run with no partial file is unchanged, so every committed print assertion still holds.
  - `match_works()`: the exact-key branch runs only when both normalised fields are non-empty (fusion's
    `_identity_key` rule); an empty artist with a real multi-word title still matches through the
    word-set rule. Docstring says why.
  - Module docstring: `auto` described as time for fully timed truth, work for `order-only` **or**
    `partial`.
- `tests/test_score_corpus.py` — three regressions (46 in the file, 802 in the suite):
  - `test_a_half_verified_truth_is_matched_by_work_not_by_its_placeholder_slices` (P0-1) — `mini-a`
    re-seeded order-only with one row really timed: `truth_timing` is `partial`, `auto` matches it by
    **work** with `10000 / null / 6000 / 7500`, `counts.timing == {"order-only": 1, "partial": 1}`, and
    the paragraph names the half-finished file; forced `--match time` on the same file scores
    `0 / 0 / 0 / 0` with `work_only.work_recall_e4 7500` beside it — the bug this fix removes from
    `auto`.
  - `test_order_only_truth_is_detected_from_the_seeds_placeholders` — the two one-row-edit cases now
    assert `partial` (they asserted `timed`), a fully listened-to truth asserts `timed`, and (P0-2) a
    timed truth whose **first track begins at 0:00** with the seed's `audible_rule` still asserts
    `timed`, which is the Mall Grab shape.
  - `test_labels_that_normalise_away_to_nothing_never_match` (P1-1) — `??? - (Original Mix)` vs
    `!!! - (Extended Mix)`: no assignment, `WorkCounts(0, 1, 0, 1, 0, 1, 0, 2)`, the row lands in
    `unmatched_predictions`; and an empty artist with a real multi-word title still matches.

No fixture was regenerated: `corpus-mini`'s two truths are fully order-only under the tightened
fingerprint as they were under the old one, so `expected.json` and `expected-time.json` are the
builder's files byte for byte.

### Final gate outputs

`uv run pytest -q`

```text
802 passed, 93 deselected, 1 warning in 157.33s (0:02:37)
```

`uv run ruff check .`

```text
All checks passed!
```

`uv run ruff format --check .`

```text
237 files already formatted
```

`uv run python scripts/audit_fixtures.py`

```text
audited 409 files
fixture audit passed
```

Phase gate — `uv run pytest tests/test_score_corpus.py -q`

```text
46 passed, 1 warning in 23.52s
```

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

The real smoke — `uv run python scripts/score_corpus.py --run-list data/local/release-1-runs-free.json
--out $TEMP/idea-runs/release-1-free-work-review.json --print` (offline; cached runs; read-only on
`work/`) — exit 0, every printed number identical to § 5c above (see "No release-1 number moved").

# Build 1b-iii (scorer, part iii) — timed-mode identity parity, overlay truth rows, crowd-label cleanup

Orchestrator follow-up to the scorer (parts i and ii, `docs/reviews/build-1b-iii-scorer.md` and
`build-1b-iii-scorer-ii.md`); not a numbered plan cycle, so the cycle note in the build prompt was
the contract and `docs/PLAN-v2.md` (untouched) the constraint. Everything is uncommitted on `main`
for review. No dependency was added; `docs/PLAN-v2.md`, `profiles/`, `src/id_detector/benchmark/`
(the certified scorer), `src/id_detector/fuse/` and the breaker are untouched. No provider was
contacted: every command ran with `AUDD_API_TOKEN=` and `IDEA_ENGINE_SHAZAM=off` exported, no
analysis was started, `work/` was only ever READ (the cached runs `data/local/release-1-runs-free.json`
names, plus two runs' `hints/hints.jsonl` to see the raw comment shapes behind the bad labels), the
only CLI processes started were `idea truth seed` (offline) and `idea serve` through the smoke
script, and the real smoke wrote under `%TEMP%/idea-runs/`. Under `data/corpus/` only
`release-1/mph-youtube-set/ground_truth.json` changed (the re-seed the cycle asked for); the
`dj-heartstring-youtube-set/` folder the orchestrator was committing was not touched — it appeared
in the run list mid-cycle as the seventh mix and is scored below.

**The three defects, in one line each.** (1) `--match time` handed the certified scorer strings
it compares strictly, so "MPH ft. Cecelia - Rush" (truth) vs "MPH - Rush (feat. Cecelia)" (engine)
was a miss *and* a wrong ID at the same time — now the wrapper canonicalises what the scorer is
handed, and the time path scores identity exactly as the work matcher does. (2) `idea truth seed`
could not hold a second track blended in at the same time, so the nine MPH "w/" overlays were
parked in a text file and their correct predictions scored WRONG — now `--overlays` seeds them
as layered episodes, linked both ways. (3) Crowd labels kept list furniture, tracklist headers
and commenters' `@` handles — now stripped at the source, before the "Artist - Title" split, and
no `@handle` can reach a label. Plus the per-timed-mix `median_offset_ms` diagnostic.

**MPH by time: likely 2/11 → 9/11, listed 7/38 → 29/38, work recall 7/53 → 29/62** (the eight
"wrong" rows that became correct are listed under § 5c).

---

## What changed, file by file

- `scripts/score_corpus.py` — **parity:** `parity_identities(identities, truth, listed,
  work_match)` builds the identity graph the certified scorer is handed in time mode: for every
  truth row the work matcher paired with a listed prediction, the truth row's own scorer key
  (`text:<work_key>`, already in the scorer's normal form) joins that prediction's work as one
  more `text:` node — the mechanism fusion itself uses for a corroborating crowd label — so the
  scorer's strict comparison sees identical strings for identical works; one work per truth row
  (closest word set, ties to the earlier listed row), a key another work already holds is left
  there, an existing node is reused. Recorded as `run_config.parity_keys` in the per-mix
  `predictions.json`. **Featuring marker:** `work_identity` drops `ft`/`feat`/`featuring` from
  the word set (`_FEATURING`), never the featured name — the one addition to fusion's rule, in
  both modes (parity). **Offset:** `placeholder_rows(truth)` (extracted from `truth_timing`,
  which now uses it), `is_range_claim(episode)` (extracted from `proved_bounds`, which now uses
  it), `median_offset(listed, work_match, truth, range_claims=)` = lower median of (first engine
  evidence − truth `start_ms_range[0]`) over work-matched pairs, excluding seed-placeholder rows
  and crowd range claims; per-mix row gains `median_offset_ms` and `offset_pairs`; `mix_line`
  prints "median offset (tool start minus tracklist start) +13.0 s over 28 work-matched pairs"
  for timed/partial mixes (or "n/a" when no usable pair) and nothing for order-only ones.
  `score_mix` reordered: prediction set → range-claim rows → proved bounds → listed works →
  work match → (time mode) parity graph → document → offset → scorer. `MixScore` gains
  `parity_keys`, `median_offset_ms`, `offset_pairs`. Module docstring and `mix_line` docstring
  updated; the `unmatched_*` lists are unchanged and keep the original labels.
- `src/id_detector/truth.py` — `seed_truth(..., overlays=None)`; `_tracklist_entries(path,
  what=)` / `_unique_entries` extracted from `_seed_entries`; `_overlay_entries` (every line must
  carry a timestamp; a trailing "(w/ overlay)" is dropped from the title); each overlay is
  attached to the main row playing at its time (the last row whose start ≤ its timestamp; errors
  before the first row and at/after the mix end), inserted right after that row with
  `start_ms_range = [t, t]`, `end_ms_range` = that row's end, one `layer` role segment,
  `overlaps_with` both ways, a `note` saying it was seeded as an overlay; occurrence indexes are
  counted in the final order; overlays with an untimed (placeholder) tracklist are refused.
  `_TRACKLIST` tolerates a bullet after the time ("0:17:09 - Artist - Title"), which is both the
  overlays file's line form and what had seeded "- MPH" / "- Mall Grab" into the drafts.
  `_format_ms` for the error messages. The truth contract's schema is untouched.
- `src/id_detector/cli.py` — `idea truth seed --overlays <file>` (help text), passed through.
- `src/id_detector/hints/parse.py` — `_MENTIONS` (zero or more `@handle` / `[mention]` prefixes)
  shared by `_ANSWER_PREFIX`, `_CORRECTION_PREFIX`, `_CORRECTION_CLEAN`, `_MENTION_PREFIX`;
  `_MENTION_ANYWHERE` + `_scrub_mentions()` applied in `_artist_title` before the split and to
  the qualifier and label afterwards, so no `@handle` token is ever emitted in a label;
  `_TRACKLIST_PREFIX` widened ("FULL TRACK LIST:", "Tracklist -", …) and applied per line as well
  as to the whole text; `_LEADING_NOISE` + `_strip_noise()` strip enumerators (`\d{1,3}[.)]`
  followed by whitespace or "["), empty/ticked bracket bullets (`[]`, `[ ]`, `[x]`), `()` and
  dash/dot bullets at the start of a unit — before the cue is read in `_strip_cues` and again from
  what is left after the cue is removed. `raw_text` is untouched.
- `data/corpus/release-1/mph-youtube-set/ground_truth.json` — re-seeded from
  `tracklists/mph-youtube-set.txt` + `tracklists/mph-youtube-set.overlays.txt` (both kept): 54 → 63
  draft episodes, 9 of them `layer` rows linked both ways, no artist begins with "- " any more;
  `selection_basis` now says the overlays were seeded; `set_id`, `source`, `split`, `stratum`,
  `corpus_version` identical.
- `tests/fixtures/corpus-mini/expected.json`, `expected-time.json` — regenerated: the only change is
  the two new per-mix fields (`median_offset_ms: null`, `offset_pairs: 0` on both mixes); every
  number and count is byte-identical to the committed files (`git diff` shows 4 added lines each).
- `tests/fixtures/hints/crowd-label-cleanup-authored.json` — new: 20 synthetic comment shapes
  (enumerators, bracket/dash/dot bullets, headers, repeated `[mention]` placeholders, and the
  cues/labels/dotted timestamps that must survive), with the expected units. No handle appears
  in it (the fixture audit forbids one; the literal `@` forms are tested from the test module with
  made-up handles).
- `tests/test_score_corpus.py`, `tests/test_stage2a_truth.py`, `tests/test_stage4a_parser.py` —
  tests below.
- `docs/reviews/README.md` — the 1b-iii (scorer iii) row.

### Tests added / changed (802 → 846 in the suite)

`tests/test_score_corpus.py` (46 → 53; two existing tests re-pinned, one extended):
- `test_featuring_marker_spelling_never_separates_one_work` — the cycle's pair: fusion's key keeps
  the marker (`("mph ft cecelia", "rush")` vs `("mph", "rush feat cecelia")`) and its word-set rule
  refuses the two sets (asserted against `fuse.identity._word_sets_corroborate` directly); the
  scorer's word sets are both `{mph, cecelia, rush}`; "featuring" folds the same way; a different
  featured artist is a different set; `match_works` pairs "MPH - Rush (feat. Cecelia)" and
  "MPH & AC Slater - Lights On (feat. Eloise Keeble)" with their truth rows and leaves
  "Rush (feat. Somebody Else)" wrong.
- `test_parity_identities_grants_each_truth_key_to_one_work_only` — two listed works corroborate
  one truth row; the key `text:mini artist feat guest|theta` goes to the closer one only, as a new
  node with the truth's label, the run's own graph untouched; a key some work already holds is
  left there and nothing is granted (`(identities, 0)` returned); strict equality grants nothing.
- `test_timed_truth_with_a_near_duplicate_label_is_matched_by_time_only_through_parity` — **the
  cycle's new mini fixture** (built in-test from `corpus-mini`): `mini-b` really timed, its Theta row
  written "Mini Artist ft. Guest - THETA (Extended Mix)" in the truth and "Mini Artist - Theta
  (feat. Guest)" in the run's identity graph. `auto` → time; numbers exactly the untouched timed
  fixture's (5000 / 5000 / 5000 / 3333; listed 1/2, truth 3); `parity_keys == 1` and the granted
  node sits in the Theta work beside the engine's label; the missed / wrong lists carry the
  original strings; `predictions.json` keeps the engine's label; and **the certified scorer,
  handed the same document with the run's original graph, scores 0 for 3** (`score_set` directly:
  `identification_work.correct == 0`, `occurrence.correct == 0`, `tier_work["likely"] == (0, 2)`).
- `test_time_mode_numbers_on_corpus_mini_are_unchanged` (extended) — the explicit assertion the
  cycle asked for: on the mini fixture `parity_keys == 0` for both mixes and the graph inside each
  `predictions.json` equals the fixture's `identities.gen0.json` node for node; the numbers
  (8000 / 5714 / 6667, per mix 10000/6000/10000 and 5000/5000/3333) stay pinned in code.
- `test_median_offset_reads_engine_evidence_against_really_timed_rows_only` — on timed `mini-b`:
  (+5 000 ms, 1 pair) for Theta; a range-claim row is excluded (`(None, 0)`); a seed-placeholder
  truth gives `(None, 0)`; end to end the row reads `(5000, 1)` and the `--print` line says
  "+5.0 s over 1 work-matched pair"; an order-only mix line has no offset text; a timed mix with no
  pair says "median offset n/a".
- `test_median_offset_is_the_lower_median_of_signed_offsets` — four timed rows on `mini-a` with
  offsets +10 s, +5 s, −10 s, +10 s → +5 s over 4 pairs.
- `test_an_overlay_seeded_truth_scores_the_blended_track_by_time` — `seed_truth(...,
  overlays=)` on `mini-b`'s media (three timed rows, Kappa as a "(w/ overlay)" line at 1:40):
  episodes Theta, Iota, **Kappa (layer, [100 s, 200 s], `overlaps_with` both ways)**, Mu; `timed`;
  the Kappa prediction, WRONG before, is right by time: 10000 / 10000 / 10000 / 5000, listed 2/2 of
  4 truth rows, no wrong row, offset (0, 2), and the real `report.json` says recall 5000.
- `test_is_range_claim_is_the_predicate_behind_proved_bounds`.
- Re-pinned: `test_work_identity_is_fusions_normaliser_not_a_second_one` (the word set no longer
  carries "ft"; the key still does) and
  `test_a_half_verified_truth_is_matched_by_work_not_by_its_placeholder_slices` (forced
  `--match time` on the half-verified file now scores the one really timed row — 3333 / 2000 /
  2000 / 2500 instead of 0 / 0 / 0 / 0 — because parity lets the strict key see "Alpha (Extended
  Mix)"; the untimed rows are still misses; plus the offset over its two non-placeholder rows).

`tests/test_stage2a_truth.py` (12 → 21):
- `test_seed_overlays_become_layered_episodes_linked_both_ways` — a bulleted main list plus an
  overlays file with a comment line, a "(w/ overlay)" line, a bare line and a duplicate: five
  episodes in time order, the overlay after its row, `[60 s, 60 s]`–`[120 s, 120 s]`, role
  `layer`, `overlaps_with` symmetric, the last overlay ending at the mix end, main rows still
  `uncertain` and ending at the next row, all draft, reloads equal.
- `test_seed_overlay_of_a_repeated_work_counts_its_occurrence` — an overlay of a track already in
  the list gets `occurrence_index 1`.
- `test_seed_overlays_reject_what_cannot_be_placed` (5 cases) — untimed overlay line, before the
  first row, at/after the mix end, order-only main list, malformed line; nothing written.
- `test_seed_strips_a_leading_bullet_from_a_timed_tracklist_row` — "0:00:00 - Artist A - One" and
  "0:17:09 – Royal-T - Tokyo Dub" seed as "Artist A" / "Royal-T"; a bulleted untimed row still
  trips the existing mixed-timing rule.
- `test_truth_seed_cli_exposes_overlays` — `idea truth seed --help` names `--overlays`.

`tests/test_stage4a_parser.py` (37 → 65):
- `test_list_furniture_headers_and_mentions_never_reach_a_label` — the 20 fixture cases
  (`kind, artist, title, label, cue_ms` per unit), including the MPH shapes ("14. [21:40] …",
  "53. … [Label]", "07. [10:22] …"), the Mall Grab header ("FULL TRACK LIST: 0:00:00 - …"), a header
  on a later line, "Tracklist -", two `[mention]`s before an answer and before a correction, and
  the guards: "track 1.02?" is still a dotted cue, "1.02 Example Artist" is not an enumerator,
  "[75] …" is still a minute cue, a trailing "[Label]" is kept.
- `test_handles_are_scrubbed_wherever_they_sit` (6 cases, synthetic handles built in the test) —
  two leading handles, a handle before "this is" / "actually" prefixes, a handle inside the artist,
  a handle after a cue; "Live @ Fabric" (a sign not glued to a name) is not a handle.
- `test_no_label_field_ever_carries_a_handle` — the privacy invariant over every fixture case plus
  five handle-bearing shapes, as parsed units and as `HintRecord`s through `parse_hint_inputs`.
- `test_the_cleanup_fixture_itself_carries_no_handle`.

The existing `tests/test_stage4a_*` tests (74) and `tests/test_golden_local_free.py` pass unchanged;
the Local Free golden is byte-for-byte the committed file (`git status` shows nothing under
`tests/golden/`).

---

## Required command outputs

### 1. `uv run pytest -q`

```text
846 passed, 93 deselected, 1 warning in 219.11s (0:03:39)   # the usual pydub audioop DeprecationWarning
```

(802 before this cycle + 7 scorer + 9 seeder + 28 parser.)

### 2. `uv run ruff check .`

```text
All checks passed!
```

### 3. `uv run ruff format --check .`

```text
237 files already formatted
```

(`uv run ruff format` was run on the edited files first.)

### 4. `uv run python scripts/audit_fixtures.py`

```text
audited 414 files
fixture audit passed
```

(413 before this report existed; the audit scans `docs/`, so it was re-run after the report was
written.)

### 5a. Phase gate — `uv run pytest tests/test_score_corpus.py tests/test_stage2a_scorer.py tests/test_stage4d_profiles.py tests/test_golden_local_free.py -q`

```text
82 passed, 1 warning in 48.28s
```

### 5b. All `tests/test_stage4a_*.py` — `uv run pytest tests/test_stage4a_connectors.py tests/test_stage4a_parser.py tests/test_stage4a_pipeline.py tests/test_stage4a_relations_fusion.py -q`

```text
102 passed, 1 warning in 5.73s
```

### 5c. `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

### 5d. The real smoke (offline; cached runs; read-only on `work/`)

`uv run python scripts/score_corpus.py --run-list data/local/release-1-runs-free.json --out
%TEMP%/idea-runs/release-1-free-7.json --print` — run from Git Bash, so `%TEMP%` was given as
`"$TEMP/idea-runs/release-1-free-7.json"`. Exit 0. The run list held **seven** mixes by then (the
orchestrator's `release1-dj-heartstring-youtube-set` had arrived). Printed as one paragraph, then
one line per mix; wrapped here. Two edits to the paste: two names the console rendered through
cp1252 are written correctly (Tiësto, Riky López), and the one Mall Grab crowd label that begins
with a commenter's platform handle (a cached run's label; see § "Not done") is redacted as the
committed-corpus policy requires.

```text
The free recipe was scored over 7 mix(es) (release1-boomtown-mix, release1-redo-of-best-set,
release1-new-mix-jan-24th, release1-final-new-set-christmas,
release1-mall-grab-boiler-room-melbourne-22, release1-mph-youtube-set,
release1-dj-heartstring-youtube-set) against DRAFT truth (seeded tracklists with placeholder
timings, not yet verified), so these are working numbers, not release numbers. Matching is
WORK-ONLY (time-agnostic) because 4 of the 7 truth file(s) are order-only (a tracklist still
carrying the seed's placeholder equal-slice timings): each listed track is matched to the
tracklist by normalised artist and title alone (mix suffixes, case, word order and featured
artists ignored), never by when it played. Of the 609 tracks the tool found, the presentation
floor hid 118 as buried, 32 as contradicted, 29 as scatter, 231 as short, leaving 199 listed; it
named 139 of the 218 distinct tracks actually played (work recall 63.8%); 139 of the 186 distinct
tracks it listed were really played (work precision 74.7%); and 61 of the 68 it marked 'likely' or
better were right (likely precision 89.7%). L3 asks for likely >= 90.0%, listed >= 80.0% and
recall >= 70.0% for free; listed precision and every timing number need timed truth (reported as
null), so the L3 bar cannot be judged from a work-only score, and the two numbers it can see are
indicative only (a work-only match is looser than a timed one): likely precision 89.7% < 90.0%;
work recall 63.8% < 70.0%. Full numbers: C:/Users/natha/AppData/Local/Temp/idea-runs/release-1-free-7.json.

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
- release1-mall-grab-boiler-room-melbourne-22 [timed; matched by time]: by time: likely 3/7
  (42.9%), listed 7/19 (36.8%), recall 7/25 (28.0%); work-only: recall 7/24 (29.2%), precision
  7/17 (41.2%), likely 3/7 (42.9%); median offset (tool start minus tracklist start) +48.0 s over
  8 work-matched pairs. Missed: - Mall Grab - Winter (UR); - Mall Grab - Mirror Break (4/4 Mix)
  (UR); - Mall Grab - Inside (UR); - Mall Grab - Marathon (UR); - Mall Grab & Flansie - Love
  Yourself; - Mall Grab - Bear Witness (UR); - Surusinghe - ID (UR); - Effy - Get Down (UR); -
  Mall Grab & C.R.T.B. - Juice (MG Remix) (UR); - KETTAMA - Feel Emotion (UR); - Mall Grab -
  Escape From Belanglo (UR); - Mall Grab - BB MG (Soundcloud); - Sambaboys - ID (UR); - C.R.T.B.
  - Nothing Moves You; - Mall Grab - Madman (UR); - Mall Grab - 1ofthozedaze (Edit) (Soundcloud);
  - Mall Grab - Metaphysical. Wrong: [crowd handle redacted] Jordon Alexander - Winter
  [possible]; Rank 1 - Airwave (Radio Vocal Edit) [likely]; System F - Out of the Blue [likely];
  Darude - Sandstorm [likely]; System F - Out of the Blue 2010 (Original Violin Edit) [likely];
  Flansie & Mall Grab - No One Else Will [possible]; Brandon Allan - HevE Tac (Brandon Allan
  Remix) [possible]; Long Season Intro Edit - Mall Grab [possible]; Be your best when your best is
  needed - clarkie [possible]; MG - Long Season Intro Edit [possible].
- release1-mph-youtube-set [timed; matched by time]: by time: likely 9/11 (81.8%), listed 29/38
  (76.3%), recall 29/62 (46.8%); work-only: recall 29/62 (46.8%), precision 29/38 (76.3%), likely
  9/11 (81.8%); median offset (tool start minus tracklist start) +13.0 s over 28 work-matched
  pairs. Missed: Boy Better Know - Too Many Man (NOTION Edit); 33 Below & MPH & NOTION - Run Da
  Riddim; MPH & Capo Lee - Smoothies; MPH & Zero - Gold Coast (UR); TNGHT - Higher Ground (Higgo
  Edit); Redlight ft. Sweetie Irie - Zum Zum; Royal-T - Tokyo Dub; Soul Mass Transit System -
  Bomb; MPH - Raw; MPH - ID; NOTION - Bring In The Katz Dub; MPH - Brainwashing; Wost - Calentura
  Vaginal (Hard Drum Edit); MPH & Gentlemens Club - Kill Bill; Oppidan - Move Your Feet (VIP); 33
  Below & MPH & NOTION - Light It Up; Groove Armada - Superstylin' (DJ Q Bootleg); Champion & MPH
  - Badderman Refix; Adam F - Circles (Oppidan Bootleg); Simula - Daddy Issues; MPH - Doubt;
  Sammy Virji - ID; MPH - Spend The Night; Chase & Status - Headtop (ft. IRAH) (Original Dub Mix);
  The Chemical Brothers - Swoon (MPH Edit); Uffie ft. Pharrell Williams - Add Suv (Armand van
  Helden Club Mix); 33 Below & MPH & NOTION - ID; MPH - Overrated (VIP); MJ Cole & MPH - Hold On;
  Flava D & P Money - Dutty; The Bug ft. Flowdan - Jah War; MPH - My Mind (time approx.); MPH -
  Home T Dub (time approx.). Wrong: Mark Krupp - Too Many Man [possible]; 17. [] MPH - LA NYC
  [possible]; Me & My Toothbrush - Everybody [possible]; Energy Reflect & Paul Miller - Ascend
  (Extended Mix) [possible]; Simula - Descent [possible]; Darude - Sandstorm [likely]; 53. MPH -
  My Mind [likely]; Riky López - Canary Sound [possible]; Armand Van Helden - You Don't Know Me
  (feat. Duane Harden) [possible].
- release1-dj-heartstring-youtube-set [timed; matched by time]: by time: likely 3/3 (100.0%),
  listed 10/24 (41.7%), recall 10/21 (47.6%); work-only: recall 10/21 (47.6%), precision 10/15
  (66.7%), likely 3/3 (100.0%); median offset (tool start minus tracklist start) +51.0 s over 13
  work-matched pairs. Missed: Unknown - ?; DJ Seinfeld - Rythm of the Night (x Biicla - SWAG);
  Kettama, DJ Heartstring - If U Want My Heart; Shugz - The Drums (UR); Empire of the Sun - Alive
  (DJ Heartstring Remix); DJ Heartstring - ID (UR); DJ Seinfeld - Plush; DJ Heartstring - Another
  Year Alone (UR); DJ Heartstring - Staring into the Sun; The Groovaholics - Wake Up The Funk
  (Ragel Mood Remix); DJ Heartstring, DMA'S, Saidah - A Thousand Lies. Wrong: Corona - Rhythm of
  the Night (Lee Marrow Space Mix) [possible]; Rank 1 - Airwave (Radio Vocal Edit) [possible];
  Torres De Lara - De Barrio [possible]; DJ HEARTSTRING - Another Year Alone (i love you)
  [possible]; DJ Hazel & svdst - Dropsik [possible].
```

Pooled JSON (top level): `match_mode: work`, `match_requested: auto`, `likely_precision_e4: 8971`
(61/68), `listed_precision_e4: null`, `work_precision_e4: 7473` (139/186), `work_recall_e4: 6376`
(139/218), `counts.timing: {order-only: 4, timed: 3}`, `range_claims: 10`, `l3.thresholds_met:
null`, `certifiable: false`. Per mix (likely / listed / work precision / recall): boomtown
9375 / null / 8750 / 8750; redo 10000 / null / 6250 / 8333; jan-24th 10000 / null / 8000 / 8000;
christmas 10000 / null / 8636 / 8261 — all four **identical to part ii** (no order-only number
moved: the featuring fold changed nothing there); Mall Grab (time) 4286 / 3684 / 4118 / 2800 with
`work_only` 4286 / 4118 / 2917 (unchanged), `parity_keys 5`, offset +48 s / 8; MPH (time)
8182 / 7632 / 7632 / 4677 with `work_only` 8182 / 7632 / 4677, `parity_keys 17`, offset
+13 s / 28; DJ Heartstring (time) 10000 / 4167 / 6667 / 4762 with `work_only` 10000 / 6667 / 4762,
`parity_keys 6`, offset +51 s / 13.

**What moved on the two mixes that existed before, by time (before → after).** Baseline captured
on the same cached runs with the committed code before any change (`release-1-free-6-baseline`):

| Mix | likely | listed | work recall | work-only recall |
|---|---|---|---|---|
| MPH | 2/11 (18.2 %) → **9/11 (81.8 %)** | 7/38 (18.4 %) → **29/38 (76.3 %)** | 7/53 (13.2 %) → **29/62 (46.8 %)** | 21/53 → 29/62 |
| Mall Grab | 1/7 (14.3 %) → **3/7 (42.9 %)** | 2/19 (10.5 %) → **7/19 (36.8 %)** | 2/25 (8.0 %) → **7/25 (28.0 %)** | 7/24 (unchanged) |

On both, the by-time identity numbers now equal the work-only ones exactly (29/38 = 29/38,
7/19 correct = 7/17 correct rows' works) — that is what "parity" means: the time path no longer
loses a track to spelling, only to time.

**Which MPH "wrong" items became correct after (1)+(2)** — eight of the seventeen:

| Was "Wrong" | Now | Through |
|---|---|---|
| MPH - Funk Master [likely] | correct (truth row 12, overlay of Royal-T - Tokyo Dub) | (2) overlays |
| Sammy Virji - Bogeyman [possible] | correct (overlay of Wost - Calentura Vaginal) | (2) |
| Sammy Virji & 33 Below - Dub It In [possible] | correct (overlay of Oppidan - Move Your Feet) | (2) |
| MPH - Run It [likely] | correct (overlay of MPH - Doubt) | (2) |
| NOTION - AFTER THE BREAK [possible] | correct (overlay of Joy Orbison - Flight FM) | (2) |
| Modelle - Bloodgulch [possible] | correct (overlay of Sammy Virji - Never Let You Go) | (2) |
| MPH - Rush (feat. Cecelia) [likely] | correct (truth "MPH ft. Cecelia - Rush") | (1) featuring fold + parity |
| MPH & AC Slater - Lights On (feat. Eloise Keeble) [possible] | correct (truth "AC Slater & MPH ft. Eloise Keeble - Lights On") | (1) |

The other 14 of the 29 by-time matches were rows already right by work but wrong by time — the
strict key alone had lost them (e.g. "Bushbaby & ELOQ - Breaka Breaka" vs the engine's label,
"Fred again.. & Baby Keem - leavemealone (MPH Bootleg)" vs "…leavemealone", the 17 keys parity
granted). The "Missed" list correspondingly lost "MPH ft. Cecelia - Rush" and "AC Slater & MPH
ft. Eloise Keeble - Lights On" and gained the three overlays the tool did not find (Simula - Daddy
Issues, Chase & Status - Headtop, The Bug ft. Flowdan - Jah War). Still wrong, and why: "17. []
MPH - LA NYC" and "53. MPH - My Mind" are **cached** crowd labels from the run's `hints.jsonl`
(the parser fix (3) applies when hints are parsed, and no analysis may be started this cycle);
without the enumerator "MPH - My Mind" ⊂ "MPH - My Mind (time approx.)" would match on a fresh
run, while "MPH - LA NYC" would still be wrong because the owner's list says "MPH - ID" at 26:51.
"Simula - Descent" vs the overlay "Simula - Daddy Issues" is a different title; "Mark Krupp - Too
Many Man" vs "Boy Better Know - Too Many Man (NOTION Edit)" shares only the title; the rest are not
on the tracklist.

### 6. `git status --short` and `git diff --stat`

Captured after this report and the README row were written (the report itself gains this section
afterwards; nothing else changed):

```text
$ git status --short
 M data/corpus/release-1/mph-youtube-set/ground_truth.json
 M docs/reviews/README.md
 M scripts/score_corpus.py
 M src/id_detector/cli.py
 M src/id_detector/hints/parse.py
 M src/id_detector/truth.py
 M tests/fixtures/corpus-mini/expected-time.json
 M tests/fixtures/corpus-mini/expected.json
 M tests/test_score_corpus.py
 M tests/test_stage2a_truth.py
 M tests/test_stage4a_parser.py
?? docs/reviews/build-1b-iii-scorer-iii.md
?? tests/fixtures/hints/crowd-label-cleanup-authored.json

$ git diff --stat
 .../release-1/mph-youtube-set/ground_truth.json    |   2 +-
 docs/reviews/README.md                             |   1 +
 scripts/score_corpus.py                            | 253 +++++++++++--
 src/id_detector/cli.py                             |  12 +
 src/id_detector/hints/parse.py                     |  63 +++-
 src/id_detector/truth.py                           | 159 ++++++---
 tests/fixtures/corpus-mini/expected-time.json      |   4 +
 tests/fixtures/corpus-mini/expected.json           |   4 +
 tests/test_score_corpus.py                         | 396 ++++++++++++++++++++-
 tests/test_stage2a_truth.py                        | 168 +++++++++
 tests/test_stage4a_parser.py                       |  96 +++++
 11 files changed, 1060 insertions(+), 98 deletions(-)
```

Untracked additions: `tests/fixtures/hints/crowd-label-cleanup-authored.json` and this report.
Every changed file was checked to be LF-only against `HEAD` (one, `tests/test_stage2a_truth.py`,
had come back CRLF from the append and was converted before this capture). The corpus truth file
is one line (compact JSON, as the seeder writes it), hence the "2 +-".

---

## Plan ambiguities resolved (and how)

1. **No plan section exists for this cycle.** The build prompt names a `### 1b-iii — corpus scorer
   part iii …` heading in `docs/PLAN-v2.md` that is not there (the plan's 1b-iii section is the
   original breaker + scorer cycle). The cycle notes in the prompt were taken as the contract; the
   plan was not edited.
2. **"Canonicalise both sides' artist/title strings."** The certified scorer never reads
   `ScoredEpisode.work` at all: `_IdentityResolver.work_equivalent` asks whether the predicted
   work's `text:` node ids contain the truth row's `work_key`. So rewriting label strings on either
   side would have changed nothing, and rewriting the truth *file* into a canonical copy would have
   cost it its freeze manifest (the copy's hash is not in `corpus-version.json`, so
   `truth_is_frozen_verified` fails and the per-mix report's `unverified_seed_comparison` flips for
   verified truth). The canonical string is therefore granted where the scorer looks: the truth
   row's own scorer key joins the predicted work as a `text:` node — precisely how fusion attaches a
   corroborating crowd label — in the graph embedded in that mix's `predictions.json` only. The
   run's artefacts, the truth file and every label in the output (including `unmatched_*`) are
   untouched.
3. **`_normalise` alone does not unify the cycle's example.** `hints.relations._normalise` keeps the
   featuring marker ("mph ft cecelia" / "rush feat cecelia") and
   `fuse.identity._word_sets_corroborate` refuses the two word sets (neither contains the other and
   its near-spelling tolerance only covers tokens of ≥ 4 letters, so "ft" / "feat" never meet) — the
   test proves both. The smallest change that makes the pair one work is to drop the marker itself
   (`ft`, `feat`, `featuring`) from the scorer's word set, never the featured name; applied in
   `work_identity`, i.e. in both modes, so the two modes stay in parity. Fusion is untouched
   (a fusion-cycle decision, flagged below); the order-only mixes' numbers did not move.
4. **Many-to-one.** The work matcher may pair several listed works with one truth row; the scorer
   resolves a truth to at most one work (`truth_work_id` raises otherwise). The key goes to the
   closest word set (ties to the earlier listed row); a key some work already holds is left where
   it is, since the scorer already resolves that truth there.
5. **Overlay placement.** "The row it overlaps" = the main row playing at the overlay's timestamp
   (the last row whose start ≤ it), never a row of its own in the end-derivation chain; end = that
   row's end; role `layer` for the overlay, the main row keeps the seed's `uncertain`; the overlay is
   inserted right after its row so the file reads in time order; `overlaps_with` links overlay ↔
   row only (two overlays on one row are not linked to each other — none in the MPH data); an
   overlay needs a timestamp and a timed main list (a placeholder list cannot say which row plays
   at a time); a `note` on the overlay row records the assumption about its end for the verifier.
6. **Bullets in the seeder.** The overlays file's line form is "H:MM:SS - Artist - Title", which the
   old `_TRACKLIST` would have read as artist "- Artist" — the same bullet that had seeded "- MPH" and
   "- Mall Grab" into the drafts. `_TRACKLIST` now tolerates one bullet after the time (and at the
   line start). The re-seeded MPH truth has clean artists; the Mall Grab draft still carries its
   bullets (only the MPH re-seed was in scope) — harmless for matching (both normalisers fold the
   dash), only its printed labels show it.
7. **`median_offset_ms`.** Prediction start = the first engine evidence support start (the raw
   "first heard it here"); truth start = `start_ms_range[0]`; pairs = the work matcher's (so an offset
   is visible even when time-matching fails), excluding truth rows still on seed placeholders (via
   `placeholder_rows`, which `truth_timing` now shares) and listed rows that are crowd range claims
   (their range comes from the same comments, so it can say nothing about the video); lower median
   (`statistics.median_low`) for an even count; `offset_pairs` beside it so the owner can weigh it;
   printed for timed and partial mixes only. Not corrected for, as instructed.
8. **Fixture handles.** `scripts/audit_fixtures.py` rejects any `@name` under `tests/fixtures/`, so
   the committed fixture uses the parser's existing `[mention]` placeholder for mentions and the
   literal `@` forms are exercised from the test module with made-up handles (`@fan_alpha_7`,
   `@fan-beta-8`), which the audit does not scan.
9. **What counts as list furniture.** An enumerator is `\d{1,3}[.)]` followed by whitespace or "["
   (so "1.02" / "track 1.02?" are not enumerators — pinned in the fixture); bracket bullets are
   empty or single-tick (`[]`, `[ ]`, `[x]`, `[•]`); dash/dot bullets need a following space; headers
   may carry a leading "full / complete / entire / whole / my / the / updated / final" and end in
   ":" or a dash. A leading "w/ " (the overlay marker, 18 MPH crowd lines) is deliberately *not*
   stripped: it was not in the cycle's list, and it never blocks a match (the word-set rule absorbs
   the extra token, which is how the six overlay rows scored correct above).
10. **The seventh mix.** `release1-dj-heartstring-youtube-set` entered the run list mid-cycle; the
    smoke scored it without incident and its numbers are reported, but nothing under its corpus
    folder was read for any other purpose or touched.

## Findings for the owner (not changed this cycle)

- **A real clock offset on two YouTube sets is plausible.** MPH's median offset is +13 s over 28
  pairs — about one recognition window, i.e. the engine simply hears a track a little after it
  starts, and the comment tracklist's clock matches the video. Mall Grab reads **+48 s over 8
  pairs** and DJ Heartstring **+51 s over 13 pairs**: both comment tracklists seem to start ~45–50 s
  *before* the video does (an intro or a cut). A ~50 s shift on multi-minute tracks still leaves
  most engine supports inside the truth span (plus the scorer's 30 s association margin), which
  is why the by-time identity numbers on both sets still equal the work-only ones here; what it
  does corrupt is every boundary number in those mixes' `report.json`, and the first / last tracks
  of a set are the ones a shift pushes across a row edge. Worth a listen before the first
  verification pass; if confirmed, the verifier's real start times will fix it, and the scorer
  should keep reporting rather than correcting.
- **Cached labels keep the old furniture.** "53. MPH - My Mind", "17. [] MPH - LA NYC" and the
  handle-led Mall Grab label live in the cached runs' `hints/hints.jsonl` and `fuse/` artefacts; the
  parser fix takes effect on the next analysis (not started here). Until then those rows print as
  they are; the handle reaches only scratch output and is redacted above.
- **Fusion's own word-set rule has the featuring blind spot** (`fuse/identity._word_sets_corroborate`
  + `_words`): a crowd label "X ft. Y - Z" will not corroborate an engine label "X - Z (feat. Y)"
  either, so such a hint lands in its own identity and is listed twice. Same fix, one line, but in
  fusion — a fusion-cycle decision with corpus-wide effect.
- **Two MPH rows carry "(time approx.)" in their title** (rows 53 and 54, as the owner wrote them);
  the seeder keeps it. They match through the word-set rule anyway once the label is clean; the
  verifier will want to move the note out of the title.
- **`present/exports._candidate_label`** (the page/export side of the handle finding in part ii)
  is unchanged — queued for 3a-ii as before; with the source fixed, new runs no longer produce such
  labels.

## Not done (with reasons)

- **No re-analysis of any mix** — the cycle forbids starting an analysis, so the cached runs'
  labels are what the smoke scores; the parser fix is proven on the fixture and on the raw comment
  shapes copied out of the cached `hints.jsonl` (in-memory only).
- **The Mall Grab draft was not re-seeded** (bullets remain in its printed labels) — only the MPH
  re-seed was in scope under `data/corpus/`.
- **No `timing:` marker, no fusion change, no display-side scrub** — outside this cycle, as above.
- **The breaker half of 1b-iii** — still deferred, as in parts i and ii.
- **Gate wall time** — `tests/test_score_corpus.py` runs in ~37 s (the benchmark bootstrap per
  time-scored mix; three more time-scored runs than part ii).

---

## Review + fix pass

Adversarial review of the uncommitted tree (Opus), then the fixes. Nothing was committed, branched
or pushed. No provider was contacted: every command ran with `AUDD_API_TOKEN=` and
`IDEA_ENGINE_SHAZAM=off` exported, no analysis was started, `work/` was read only through the
cached runs `data/local/release-1-runs-free.json` names, and the smoke wrote under the session
scratchpad. `docs/PLAN-v2.md`, `profiles/`, `data/corpus/`, `src/id_detector/benchmark/`,
`tests/golden/` and `docs/schemas/` were not edited by this pass.

### What I verified before looking for faults

- **The certified scorer is byte-for-byte untouched.** `git diff -- src/id_detector/benchmark/` is
  empty; so are `profiles/`, `tests/golden/` and `docs/schemas/`. `uv run python
  scripts/export_schemas.py` rewrites all 28 schemas byte-identically (`git status docs/schemas/`
  clean afterwards), so the truth contract's schema really is unchanged.
- **`corpus-mini` is unmoved.** `tests/fixtures/corpus-mini/expected.json` and `expected-time.json`
  each gained exactly the two new keys (`median_offset_ms: null`, `offset_pairs: 0`); every metric,
  count, list and hash is byte-identical to the committed files.
- **The MPH re-seed is deterministic and reproducible.** Calling `seed_truth(...)` twice with the
  committed set's own arguments plus `--overlays` gives byte-identical output, and that output
  equals the committed `data/corpus/release-1/mph-youtube-set/ground_truth.json` exactly (63
  episodes, 9 `layer` rows, `overlaps_with` symmetric both ways). Positive duration is guaranteed,
  not hoped for: `TruthRoleSegment._ordered` (`contracts.py:597`) rejects `to_ms <= from_ms`, and an
  overlay is attached to the **last** row whose start is `<= at_ms`, so that row's end (the next
  row's start, or the mix end) is strictly later than the overlay's start.
- **Parity never manufactures a match.** Empirically, on the seven cached release-1 runs, with
  `parity_identities` stubbed out to a no-op versus live: the truth **denominators are identical**
  (Mall Grab work 25/25 and listed 25/25, MPH 62/62 and 63/63, DJ Heartstring 21/21 and 23/23) —
  parity only moves `correct` (Mall Grab 2 to 7, MPH 12 to 29, DJ Heartstring 4 to 10) — and on
  every mix the by-time `correct` **equals, never exceeds**, the work-only `correct` (7/7, 29/29,
  10/10). A truth row also cannot credit two predictions: `associate_occurrences`
  (`benchmark/scorer.py:494`) removes each prediction from `unused` as it is paired, and
  `parity_identities` grants each truth key to at most one work (`ranked`, one entry per truth row).
- **No handle reaches a committed artefact.** `grep -rn "@" tests/fixtures/hints/
  data/fixtures/hints/ tests/golden/` returns nothing.
- **`median_offset_ms` sign convention is one story everywhere:** prediction minus truth. Module
  docstring ("first engine evidence of a listed track minus the tracklist's start of the truth row"),
  `MixScore.median_offset_ms`, `median_offset`'s docstring, the `--print` line ("median offset
  (tool start minus tracklist start) +48.0 s over 8 work-matched pairs") and this report agree.

### Findings

**P1 — the tracklist-header strip matches the first two letters of an artist, and now does it on
every line.** `src/id_detector/hints/parse.py:76-79` widened the header pattern, and `_split_mega`
(`parse.py:200-205`) newly applies it to **every** line of a comment rather than only to the whole
text once. `(?:track\s*list|tracklist|tl)` has no word boundary, so it matches the "TL" of any
artist whose name starts with those letters: `TLC - No Scrubs` parses as artist `C`. Where the
committed code exposed only a comment's first line, the diff exposes every line of a pasted
tracklist. Verified against `git show HEAD:src/id_detector/hints/parse.py`: line 2 of
`"Tracklist:\nTLC - No Scrubs"` was `TLC`, became `C`.
**Fixed:** the header word must end there (a negative lookahead on word characters and an
apostrophe), and the redundant `tracklist` alternative (already covered by `track\s*list`) is gone.
Genuine headers ("FULL TRACK LIST:", "Tracklist -", "TL so far:", "TRACKLIST: 0:00:00 - ...") all
still strip.
**Regression test:** `tests/test_stage4a_parser.py::test_a_header_word_only_counts_when_the_word_ends_there`
(3 shapes: alone, under a header, mid-paste, plus the genuine-header assertion).

**P1 — a handle inside a bracket left the bracket stranded in the title.** `_scrub_mentions` was
applied to the identity fields inside `_artist_title` but to the qualifier and label only *after*
`_flags_and_qualifiers` had read the **unscrubbed** body (`parse.py:450-453` in the build). The two
sides then disagreed: `Example Artist - Signal Path [@x Records]` gave label `Records` and title
`Signal Path [ Records]`, and `_clean_identity_field`'s `\[Records\]` no longer matched the
`[ Records]` the scrub had left, so the bracket survived in the title. Committed code produced
`Signal Path`. Same for a `(@x Remix)` qualifier, which became `Signal Path ( Remix)`.
**Fixed:** the body is scrubbed **once**, before both readers, and `_scrub_mentions` closes the gap
the removal opens inside a bracket — but only when it actually removed something, so text with no
handle is byte-for-byte as the build had it (`Title ( Extended Mix )` is still left alone).
**Regression test:** `tests/test_stage4a_parser.py::test_a_handle_inside_a_label_or_qualifier_takes_the_whole_bracket_with_it`.

**P2 (fixed anyway, one line) — overlays were attached in file order, not time order.**
`src/id_detector/truth.py:196`: main rows are sorted by timestamp, overlays were appended in the
order the file lists them, so an overlays file written out of order produced truth episodes out of
chronological order (with `occurrence_index` counted in that order). **Fixed** with a stable sort by
timestamp, so an already-sorted file — the committed MPH one — seeds byte-identically (re-verified
after the change).
**Regression test:** `tests/test_stage2a_truth.py::test_seed_overlays_of_one_row_are_ordered_by_time_whatever_the_file_says`.

**Tests the review prompt asked for and the build lacked** — an artist that *looks* like list
furniture: `tests/test_stage4a_parser.py::test_an_artist_that_looks_like_list_furniture_is_never_stripped`
pins "2 Bad Mice", "4 Hero", "-Ziq" and "u-Ziq" (micro sign), and that a real bullet or enumerator
in front of a digit-led artist still goes. All four already survived `_LEADING_NOISE` — the
enumerator needs its `.` or `)` and the bullet needs a following space — but nothing pinned it.
(NFKC folds the micro sign to a mu before any of this runs; the test records that.)

### P2 notes, not fixed, with the reason

- **Parity could in principle shrink the by-time recall denominator.** Two truth rows with
  *different* work keys can both be granted to the same engine `work_id`, and `truth_works`
  (`benchmark/scorer.py:889`) then counts them as one. It does not happen on any release-1 mix (see
  the denominators above), and the obvious guard — one granted key per work — would wrongly split
  truth rows that really are the same work spelled two ways, which is the whole point of parity.
  Left as a note for the owner to watch if a future mix's by-time truth count dips below its
  work-only one.
- **A work with two listed episodes hands the granted key to both**, so `associate_occurrences` may
  credit the truth row to the sibling episode rather than the one the work matcher paired. Harmless:
  association is one-to-one, both episodes are the same work by fusion's own grouping, and the
  credit is one row either way.
- **`--print` still emits a real commenter handle** from *cached* run labels (a handle-led label on
  the Mall Grab mix). Fix (3) applies when hints are parsed, and no analysis may be started this
  cycle, so the label can only be cleaned by re-analysing. Nothing to do in scope — but the
  paragraph must not be pasted anywhere before that (the build report hand-redacted it).
- **`data/corpus/release-1/tracklists/mph-youtube-set.txt` and `.overlays.txt` still say the
  overlays are "not seeded — attach via overlaps_with in verification"**, which the re-seed made
  false. `data/corpus/` is off-limits to this pass; the owner should drop those two comment lines.
- **The other release-1 truth files still carry the bulleted artists the old `_TRACKLIST` seeded**
  ("- Mall Grab - Winter (UR)"); only MPH was re-seeded, per the cycle's scope. Cosmetic — both
  normalisers reduce "- Mall Grab" to "mall grab", so no number moves — but the missed lists read
  oddly until they are re-seeded.
- **`parity_keys` reaches only `predictions.json`'s `run_config`,** not the per-mix row the owner
  reads (`median_offset_ms` does). Adding it would move the `corpus-mini` expected fixtures a third
  time this cycle for no measurement gain.
- **`proved_bounds` recomputes the `spans` / `lo` / `hi` that `is_range_claim` just computed**
  (`scripts/score_corpus.py:625-640`). No behaviour effect; folding them would touch the extraction
  the cycle deliberately kept minimal.
- **The mention scrubber also removes a handle-shaped token glued to a word** ("Boiler Room
  @Melbourne" becomes "Boiler Room Melbourne"). Deliberate under the cycle's privacy rule; "Live @
  Fabric" (the sign not glued to a name) is preserved and tested.

### Final gate outputs (after the fixes)

`uv run pytest -q`

```text
858 passed, 93 deselected, 1 warning in 181.80s (0:03:01)
```

(846 from the build + 12 from this pass: 6 furniture-lookalike artists, 3 header-word shapes, 2
bracketed-handle shapes, 1 overlay ordering.)

`uv run ruff check .`

```text
All checks passed!
```

`uv run ruff format --check .`

```text
238 files already formatted
```

`uv run python scripts/audit_fixtures.py`

```text
audited 414 files
fixture audit passed
```

Phase gate — `uv run pytest tests/test_score_corpus.py tests/test_stage2a_scorer.py
tests/test_stage4d_profiles.py tests/test_golden_local_free.py -q`

```text
82 passed, 1 warning in 43.41s
```

All `tests/test_stage4a_*.py` — `uv run pytest tests/test_stage4a_connectors.py
tests/test_stage4a_parser.py tests/test_stage4a_pipeline.py tests/test_stage4a_relations_fusion.py -q`

```text
113 passed, 1 warning in 6.16s
```

`uv run pytest tests/test_stage2a_truth.py -q`

```text
22 passed, 1 warning in 4.94s
```

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

Real smoke — `uv run python scripts/score_corpus.py --run-list
data/local/release-1-runs-free.json --out <scratchpad>/release-1-free-7b.json --print` — exit 0, and
**every pooled and per-mix number is identical to the build's run** (the parser fix only bites when
hints are parsed, which a cached run does not redo; the only differing fields are artefact paths):

```text
likely_precision_e4 8971 == 8971 | listed_precision_e4 null == null
work_precision_e4   7473 == 7473 | work_recall_e4      6376 == 6376
per-mix rows differ only in "report" / "work_match" paths
```

The paragraph and per-mix lines are the ones already pasted in section 5d above (re-verified
verbatim, including "median offset (tool start minus tracklist start) +48.0 s over 8 work-matched
pairs", "+13.0 s over 28", "+51.0 s over 13").

`git status --short`

```text
 M data/corpus/release-1/mph-youtube-set/ground_truth.json
 M docs/reviews/README.md
 M scripts/score_corpus.py
 M src/id_detector/cli.py
 M src/id_detector/hints/parse.py
 M src/id_detector/truth.py
 M tests/fixtures/corpus-mini/expected-time.json
 M tests/fixtures/corpus-mini/expected.json
 M tests/test_score_corpus.py
 M tests/test_stage2a_truth.py
 M tests/test_stage4a_parser.py
?? docs/reviews/build-1b-iii-scorer-iii.md
?? tests/fixtures/hints/crowd-label-cleanup-authored.json
```

**VERDICT: OK_TO_COMMIT** — no P0; both P1s fixed with regression tests, the one P2 worth a line
fixed too, all gates green, the certified scorer / frozen profiles / Local Free golden / schemas
untouched.

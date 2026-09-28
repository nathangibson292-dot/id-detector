# Build — free-side accuracy: one identity rule, one row per track, the phantom rule (`fusion:5`)

Uncommitted on the worktree `agent-freeacc01` at `361fe94`. No branch, no commit, no dependency.
`profiles/`, the golden Local Free file, `src/id_detector/playlists/**`, `tests/test_playlists.py`,
`README.md`, `idea.cmd`, `present/theme.py` and every truth file are untouched.

**No provider request left this machine.** Every measurement ran on a copy of the owner's cache
(`C:\ifa\w`, 798 non-audio files, raw provider replies and audio excluded) inside
`scripts/offline_guard.py` (empty `AUDD_API_TOKEN`, `IDEA_ENGINE_SHAZAM=off` in that process only,
every non-loopback connection refused and counted): **0 network attempts in every run**. Tests ran
with `IDEA_TEST_MODE=1` and fake providers; `IDEA_ENGINE_SHAZAM` was never exported around the
suite. File timestamps (start `2026-09-28T07:45:31Z`): the owner's `work/` (38,920 entries),
`data/corpus/` (391) and `id-detector-deepscan/` (6,265) — **0 entries modified**.

## 0. Measurement you can trust

**What was wrong.** The owner's real `work/` is still entirely pre-bundle: every mix's
`fuse/episodes.json` is the `fusion:1` result of 10 September (its journal says so), and offline
re-fusion never rewrites it. Scoring that file against a fresh run is exactly the Free-vs-Deep mistake.
Two more measurement faults turned up and are fixed:

- **The scorer read rows under the wrong label.** It matched each row under the alphabetically first
  text label of its work — often a comment's spelling — not the label the page shows: "@genesis-lee-2
  Jordon Alexander - Winter", "53. MPH - My Mind", "17. [] MPH - LA NYC", "04. [] MPH - Fiesta"…
  It now reads the page's own label (`present.exports._candidate_label`, via `shown_label`).
- **The scorer had its own, looser identity rule** (a pooled word-set rule). It now uses fusion's
  rule (section 1).

**Built.**
- `scripts/make_run_list.py` (new): truth sets are read through the corpus gateway; each is matched to
  its cached mix by the truth's media key, else by exact decoded duration (only when exactly one mix
  has it — two is an error naming the set); the entry points at the mix's **current published
  result** (`present/current`'s bundle → its frozen `fuse/runs/<run>/episodes.json` +
  `presentation-identities.json`), falling back to the flat file only for a pre-bundle mix, and
  records its `fusion_version`. It prints one line per mix and a WARNING when versions differ.
- `scripts/score_corpus.py`: every run's fusion version is read (run-list `fusion_version`, else the
  frozen run's `refusion.json`, the sealed bundle that names it, or the legacy journal) and stated
  (`fusion_versions`, per-mix `fusion_version`, `--print` leads with "Every run was decided by
  fusion:N."). One score over two different known versions is **refused** (`MixedFusionVersions`,
  exit 1) unless `--allow-mixed-fusion`, which prints "WARNING — NOT COMPARABLE: …".
  `--compare-with <earlier --out JSON>` compares two scores and **refuses** (exit 3) when their
  versions differ or are unknown.
- `scripts/measure_refusion.py`: both sides now state their fusion version, and a stale stored side
  gets an explicit WARNING line.

On the real cache copy, exactly as the owner's disk is today:

```text
make_run_list --work-root <copy of work/>  ->  boomtown-mix: media d3252340303c (by media key); pre-bundle result, fusion:1; …\fuse\episodes.json   (all seven)
score_corpus (3 mixes fusion:1 + 4 re-fused fusion:5) -> scoring failed: MixedFusionVersions('refusing to pool one score: these runs were decided by DIFFERENT fusion versions — fusion:1 (boomtown-mix, dj-heartstring-youtube-set, final-new-set-christmas), fusion:5 (mall-grab-…, mph-youtube-set, new-mix-jan-24th, redo-of-best-set); re-fuse the stored evidence offline …')
score_corpus --compare-with before.json -> refusing to compare: the earlier score was decided by fusion:4 and this one by fusion:5 (pass --allow-mixed-fusion to see the numbers anyway, with this warning)
measure_refusion (stored vs today) -> Before: 6 truth mixes, work matching, decided by fusion:1; … After: …, decided by fusion:5; … WARNING — the Before side is the STORED results, decided by an older fusion version than After …
```

**How every before/after below is measured.** The same stored evidence (observations, windows,
hints, proven by hash through `refusion.load_fusion_inputs`) is re-fused offline by the before-code
(`361fe94`, a `git archive` copy → `fusion:4`) and by the after-code (`fusion:5`), and both sides
are scored by the same scorer. Within each side every mix is at ONE version (the scorer states it);
the two sides necessarily carry the version of their own code. The headline was taken through the
**real start-up path**: `refuse_stale_result` on two fresh copies of the cache (before-code: 11 mixes
re-fused in 29.0 s; after-code: 11 in 49.6 s; both: the two Deep mixes refused as paid, BENWAL
refused for its inconsistent sidecar, **0 network attempts**), then `make_run_list` →
`score_corpus`. An in-memory harness over the same evidence reproduced those numbers exactly and was
used for the per-item columns.

## Headline (seven release-1 mixes, draft truth, work-only)

| Same evidence, same scorer | work recall | work precision | `likely` precision | rows (scorer / page) |
|---|---|---|---|---|
| Before — `fusion:4` | 164/218 · 75.2 % | 168/208 · 80.8 % | 63/64 · 98.4 % | 228 / 217 |
| **After — `fusion:5`** | **164/218 · 75.2 %** | **165/196 · 84.2 %** | **63/63 · 100 %** | **214 / 203** |

For continuity with earlier reports, the OLD scorer gives 155/218, 157/208, 62/64 before and
157/218, 159/196, 62/63 after. **The whole 155 → 164 is the corrected measurement, not new
identifications:** nine tracks the tool already listed that the old scorer failed to credit (section
5). The fusion changes themselves move recall 164 → 164: they remove wrong rows and duplicates
(precision 80.8 → 84.2 %, `likely` 98.4 → 100 %) and add no correct track. That is the honest result:
recall is "up" only in the sense that it is now measured the way the tool works.

Per mix and per item (cells: recall · work precision · likely · rows scorer / page), new scorer:

| Mix | Before (`fusion:4`) | Item 1+2 only | Item 3 only | After (all) |
|---|---|---|---|---|
| `boomtown-mix` | 38/40 · 38/43 · 15/16 · 44/44 | 38/40 · 38/43 · 15/16 · 44/44 | 38/40 · 38/40 · 15/15 · 41/41 | 38/40 · 38/40 · 15/15 · 41/41 |
| `dj-heartstring-youtube-set` | 11/21 · 11/15 · 4/4 · 24/21 | 11/21 · 11/15 · 4/4 · 24/21 | 11/21 · 11/14 · 4/4 · 21/19 | 11/21 · 11/14 · 4/4 · 21/19 |
| `final-new-set-christmas` | 20/23 · 20/22 · 11/11 · 22/22 | unchanged | unchanged | 20/23 · 20/22 · 11/11 · 22/22 |
| `mall-grab-boiler-room-melbourne-22` | 17/24 · 20/26 · 3/3 · 33/28 | 17/24 · 17/23 · 3/3 · 30/25 | 17/24 · 20/26 · 3/3 · 33/27 | 17/24 · 17/23 · 3/3 · 30/24 |
| `mph-youtube-set` | 34/62 · 34/41 · 10/10 · 41/41 | unchanged | unchanged | 34/62 · 34/41 · 10/10 · 41/41 |
| `new-mix-jan-24th` | 27/30 · 27/33 · 10/10 · 33/33 | unchanged | 27/30 · 27/31 · 10/10 · 31/31 | 27/30 · 27/31 · 10/10 · 31/31 |
| `redo-of-best-set` | 17/18 · 18/28 · 10/10 · 31/28 | unchanged | 17/18 · 18/25 · 10/10 · 28/25 | 17/18 · 18/25 · 10/10 · 28/25 |
| **Pooled** | **164/218 · 168/208 · 63/64 · 228/217** | **164/218 · 165/205 · 63/64 · 225/214** | **164/218 · 168/199 · 63/63 · 217/206** | **164/218 · 165/196 · 63/63 · 214/203** |

Old scorer, same columns, pooled: 155/218 · 157/208 · 62/64 → 157/218 · 159/205 · 62/64 →
155/218 · 157/199 · 62/63 → 157/218 · 159/196 · 62/63.

Hidden rows, pooled: before 28 buried, 27 contradicted, 34 scatter, 296 short (613 episodes); after
102 ambiguous, 25 buried, 23 contradicted, 7 scatter, 239 short (610 episodes — three crowd rows fewer).
Most "ambiguous" episodes were already hidden for another reason; 11 were listed (section 3).

The precision numerator falls 168 → 165 because duplicates are now one work: the three Long Season
comment rows were two correct works + one wrong, now one correct work; the two 1ofthozedaze rows were
two correct works, now one; and one crowd row changed its label to a spelling the scorer calls wrong
(section 4).

## 1. The one identity rule (`fuse/identity.py`)

`label_match` / `same_work_labels` is the rule. The established `hint_label_corroborates` is tried
first and is **unchanged**; only where it finds nothing may a named tolerance apply:

| Tolerance | Example it exists for |
|---|---|
| `spaced` | "t e s t p r e s s" = "Testpress" (3+ single characters joined) |
| `joined` | "4raws" / "4 Raws" (already in the established reading for most pairs) |
| `descriptor` | "Long season MG Edit" = "Long Season (MG Edit)"; "… Intro Edit" dropped (core keeps ≥ 2 words) |
| `alt_title` | "ID (LET ME SEE U / ROK DA HOUSE!)" offers "Rok da House" |
| `stem` | "Feel Emotion" / "Feeling Emotions" (every differing word one stem) |
| `phonetic` | "rock the house" / "Rok da House" (ck→k, ph→f, the/da/tha, you/u, …) |
| `artist_slip` | "Paige Tomlison" / "Tomlinson" (one dropped letter, 6+ letters, 2-word name) |
| `initials` | "MG" = Mall Grab, either way round |
| `lead_in` | "FULL TRACK LIST: - Fishmans - …" (a field ending in a colon is a lead-in) |

Guards: the other part must agree **exactly** (a tolerant title needs the artist, a tolerant artist
the title — never both loose); titles are compared **whole and in order** (no shared-word rule);
**initials resolve only to a solid name** — credited on two or more works an engine recognised in
this mix, read from engine labels only — and only when no other solid name has those initials; a
tolerant match counts only when **unique**: a hint whose tolerance fits two recognised works gets no
identity (like the existing veto); a crowd label is joined tolerantly only when every crowd label it
fits carries the same title; the scorer uses a tolerance only when it fits one truth work.

Used in: hint → recognised work (only where the established rule named nothing); crowd label ↔
crowd label (**replacing** the old word-set subset rule, which merged any two labels whose words were
a subset — "never merge works whose titles merely share words"); and the scorer's matcher.

**Every pair it newly matches on the owner's cache (all 13 re-fusable mixes; only two changed):**

`6651118675bd` (Mall Grab; solid: crtb, e type, jordon alexander, kettama, mall grab, mo do, system f,
tiesto, vengaboys):
- hint→audio: "Kettama - ROCK DA HOUSE!" → "KETTAMA - Rok da House" (phonetic); "rock the house -
  kettama" → same (phonetic); "KETTAMA - ID (LET ME SEE U / ROK DA HOUSE!) (UR)" → same (alt_title);
  "KETTAMA - Feel Emotion" and "… (UR)" → "KETTAMA - Feeling Emotions (Extended mix)" (stem);
  "spirit wave - mg" → "Mall Grab - Spirit Wave" (initials).
- crowd↔crowd: "MG - Long Season Intro Edit" ↔ "Fishmans - Long season MG Edit" and "… (UR)"
  (descriptor); "fishmans - long season" ↔ the same two (descriptor); "FULL TRACK LIST: - Fishmans -
  Long season MG Edit (UR)" ↔ all of them (lead_in, + descriptor); "Long Season Intro Edit - Mall
  Grab" ↔ "MG - Long Season Intro Edit" (initials); "MG - 1ofthozedaze" ↔ "1ofthozedaze - Mall Grab"
  and "Mall Grab - 1ofthozedaze" (initials). Through the established reading now used for crowd
  labels: "mallgrab - BB MG (UR)" ↔ "Mall Grab - BB MG (Soundcloud)".

`84232b822c6a` (DJ Heartstring): "Paige Tomlison - I Wanna Feel" → "Secondcity - I Wanna Feel
(Paige Tomlinson Remix)" (artist_slip).

Splits the stricter crowd rule made (works the old subset rule had joined): "+ Biicla - Swag" /
"w/ Biicla - Swag" apart from "DJ Seinfeld x SWAG (Biicla) - Rythm of the Night" (a mash-up line, a
different title); a mis-parsed "I Wanna Feel (Paige Tomlison, Secondcity - Remix)"; two social-media
links; and "kettama - let me see" apart from the ID line (section 4). None of these was a listed
row except the last.

**Wrong merges: none found** in the 13 mixes (every link above is one track). No tolerance had to
be dropped. The tolerances that match nothing here (spaced within fusion, joined) are kept because
they pair real truth rows in the scorer (section 5).

## 2. One row per track

The same-track rows came from spellings that were separate works; with the rule they are one work,
and fusion lists a crowd row only for a work not already listed, so they collapse:

- Mall Grab: "MG - Long Season Intro Edit" (0:46), "Long Season Intro Edit - Mall Grab" (2:04),
  "Fishmans - Long season MG Edit" (2:32) → **one row** "Fishmans - Long season MG Edit" at 0:46;
  "1ofthozedaze - Mall Grab" (81:21) + "MG - 1ofthozedaze" (81:48) → **one row**; "KETTAMA - ID (LET
  ME SEE U / ROK DA HOUSE!)" now joins the recognised Rok da House, and the page's two Rok da House
  rows (18:03 likely, 21:12) are one row.
- "Two genuine separate playings stay two rows": nothing in the grouping changed (a page-grouping
  change by identity work was tried and removed — it changed no row on any cached mix, and audio
  labels of one work under different spellings do not occur there).
- The repeated WRONG labels: Airwave ×3 is gone (section 3); **De Barrio ×5 and Mears - Be My Lover
  ×2 remain** — fusion aligned them as separate occurrences with other listed tracks between them,
  which is exactly what "two playings" looks like; no rule here can tell otherwise.

## 3. The recurring phantoms (`fuse/episodes.py`)

**Listed today (before-code, `fusion:4`):** boomtown "Darude - Sandstorm" (`likely`, 132 s) and
"Zombie Nation - Kernkraft 400"; redo "Tiësto - Adagio for Strings" and "Torres De Lara - De
Barrio"; jan "Adagio for Strings"; dj-heartstring "Rank 1 - Airwave" ×3 and "De Barrio" ×5. (Better
Off Alone and Out of the Blue were no longer listed.)

**What their evidence has in common.** Every listed phantom window is a reply that offers several
recordings (Sandstorm/Adagio: 20) that do not agree where in a track the audio sits (no reliable
offset anchor) — and the recording Shazam reported is not even among them. Correct rows are the
opposite: of the 169 correct listed episode rows, **none** has a majority of such windows; nearly
every window is one recording at one consistent offset. **Rule (no list):** a window is *ambiguous* when its reply lists 2+ matches, none of them the
reported recording, and carries no reliable anchor. An episode most of whose windows are ambiguous
is flagged `ambiguous_evidence` (recorded on the episode) and hidden as `ambiguous` — overriding any
badge — unless the evidence says it played: an unbroken run of **unambiguous** windows ≥ 45 s, a
comment or tracklist naming it (`hint_supported`), or a second engine agreeing
(`engine_corroborated`). It never buries anything. It reads only the result's own recorded
observations and constants bound to `fusion:5`, so it cannot change because another mix was added.

**Rows it removes (release-1, all wrong by the scorer):** Sandstorm (boomtown, `likely`), Kernkraft
400, 2 Bad Mice - Bombscare (boomtown); Airwave ×3 (dj-heartstring); Fragma - Toca's Miracle,
Adagio for Strings (jan); Adagio for Strings, Ayla - Ayla (DJ Taucher Remix), Jupiter 8000 - Inside
(redo) — **11 wrong rows, 0 correct rows**. On mixes without truth (listed by name, cannot be
scored): 354fbd61f5bb DAVA - КРОШКА МОЯ, Matty D - Move It, Adagio for Strings (and the page row
"Reel 2 Real - I Like to Move It", made only of such windows); c3121055e46f Sandstorm; de0f005862e7
Sandstorm, Matty D - Move It; ec0a562ab791 Jesse Rose & Avon Stringer - Pressure; fcc323605143 Alice
Deejay - Better Off Alone, Matty D - Move It, Zhi-Vago - Celebrate (and page row "Bingo Players -
Devotion"). (de0f/fcc3 are Deep results: start-up leaves them as they are, as today.)

**Why this rule, with the numbers.** Without the "reported recording is not among the matches"
clause the rule removes 13 wrong release-1 rows (also NCKNAME - Tryna Dance and Me & My Toothbrush -
Everybody) but also Pupa Nas T - Work, Three Drives - Greece 2000 (168 s) and Hamdi - Skanka on
mixes without truth, whose replies list many releases of ONE track — so the clause stays and those
two wrong rows stay. A versioned name list would add nothing: the one phantom left, **De Barrio
(6 rows)**, has anchored single-recording windows in unbroken runs of 48, 57 and 75 s — by its own
evidence a real play (most likely a sample inside the tracks around it), and the brief's own
exemption ("a long unbroken run") would keep it listed under a list too. It is reported, not
special-cased.

## 4. What got worse, by name

- **"kettama - let me see"** (Mall Grab, 17:15, crowd row, 10 s): the same crowd row used to be
  labelled by the ID line it was merged with by shared words; that line now joins the recognised Rok
  da House, so this comment stands alone under its own truncated title. The right track, a spelling
  the rule cannot prove ("Let Me See" vs "Let Me See U"), and the scorer counts it wrong. No row was
  added — the row count at that time is unchanged.
- No correct row was lost anywhere; no newly listed row names a track that was not played.

## 5. Tracks gained, with the occurrence-level check

All nine are rows the tool already listed; the old scorer did not credit them. Checked against
timed truth where it exists (the three timed sets' known clock offsets: +84 s Mall Grab, +17 s MPH,
+51 s DJ Heartstring):

| Mix | Truth | Listed as | Time check | Why it now pairs |
|---|---|---|---|---|
| dj-heartstring | DJ Heartstring - Another Year Alone (UR) | DJ HEARTSTRING - Another Year Alone (i love you), 63:12, 261 s | truth 63:10, **+2 s** | established rule (bracketed subtitle) |
| mall-grab | KETTAMA - Feel Emotion (UR) | KETTAMA - Feeling Emotions (Extended mix), 51:21, 222 s | truth 50:00, +81 s | `stem` |
| mall-grab | Mall Grab - BB MG (Soundcloud) | Mall Grab - BB MG (crowd), 62:30 | truth 62:00, +31 s | established (joined name) |
| mall-grab | Mall Grab - Marathon (UR) | Jordon Alexander - Marathon (feat. Mall Grab), 23:00, 78 s | truth 21:00, +120 s | established (featured credit) |
| mall-grab | Mall Grab - Winter (UR) | Jordon Alexander - Winter (feat. Mall Grab), 4:21, 135 s | truth 4:00, +21 s | page label (was "@genesis-lee-2 …") |
| mph | MPH - My Mind (time approx.) | MPH - My Mind, 85:33, 114 s, `likely` | truth 84:30, +63 s | page label (was "53. MPH - My Mind") |
| xmas (order-only) | Testpress - FORZ4 | t e s t p r e s s - Forz4, 13:51 | no timing | `spaced` |
| jan (order-only) | Beau James - 4 Raws Edit | 4raws - beau james (crowd), 0:40 | no timing | established (joined) |
| redo (order-only) | Sammy Virji & Flowdan - Shella Verse | Flowdan - Shell a Verse, 26:18, 168 s | no timing | established (joined) |

Each timed one is listed inside its own truth occurrence at the set's usual clock offset. The same
scorer also newly pairs, as second rows of already-credited works: "MG - Long Season Intro Edit"
(`descriptor`), "Fishmans - Long season MG Edit" (`descriptor`), "MG - 1ofthozedaze" (`initials`).
Scorer pairs the rule now REFUSES that the old word-set rule accepted: none on the corpus (the four
pairs that relied on it — Tomlinson/Tomlison, the two Rok da House rows, the FULL TRACK LIST line —
all pair under the rule).

## 6. `FUSION_VERSION` 4 → 5

`recipes.py`: stored Free results re-fuse offline at start-up, spending nothing (the start-up run on
the cache copy above: 11 re-fused, 0 network attempts); legacy Deep results stay refused as today
(both Deep mixes: "it is a paid (Deep) result …"). The golden Local Free test passes against the
untouched golden.

## 7. Tests (`tests/test_free_accuracy.py`, 45) and revert proofs

The whole file fails to collect against `361fe94` (`1 error during collection`: it imports the new
rule). Each change was then neutralised on its own in a scratch copy of the tree and the file re-run:

| Neutralised | Result | Failing tests |
|---|---|---|
| all tolerances (label_match returns after the established rule) | 16 failed, 29 passed | 10 tolerance cases, initials-solid, tolerant answer backs, two-works veto, three spellings → one row, initials apart, scorer rule |
| old word-set crowd merge restored | 1 failed | titles that share words stay two rows |
| tolerant two-works veto off | 1 failed | fits two recognised works backs neither |
| initials ignore "solid" | 1 failed | initials resolve only to one solid name |
| both-loose guard off | 2 failed | near-misses (Tomlison+stem, initials+descriptor) |
| ambiguity rule off | 2 failed | ambiguous window; phantom not listed / buries nothing |
| "reported recording among the matches" clause off | 2 failed | ambiguous window; many releases of one track |
| clean-core exemption off | 1 failed | still listed [clean_core] |
| hint / second-engine exemption off | 2 failed | still listed [comment], [second_engine] |
| scorer uniqueness off | 1 failed | scorer matches only when unique |
| scorer back to the old label | 1 failed | scorer reads the page's label |
| mixed-version refusal off | 2 failed | one score of two versions refused; run list |
| `--compare-with` refusal off | 1 failed | two scores at different versions |
| run list uses the flat file | 1 failed | run list names the current result |
| fusion version not read | 2 failed | run_fusion_version; run list |

Two tests are guards that pass either way by design: "the established readings still match with no
tolerance" and "a DJ who really drops Sandstorm gets it listed" (the rule is not a name list).

**Existing assertions changed, and why:**
- `fusion:4` → `fusion:5` pins (the version bump): `test_phase0a_money.py` (5), `test_phase0a_status.py`
  (2), `test_phase1a_compat.py` (3), `test_phase1b_targeting.py` (4), `test_refusion.py` (9),
  `idea_web/test_refusion_money.py` (3).
- `tests/fixtures/corpus-mini/expected{,-time}.json`: the score document gains `fusion_versions`,
  `fusion_warning` and a per-mix `fusion_version` (all `[]`/`null` for that fixture) — added, nothing
  removed.
- `test_score_corpus.py::test_labels_that_normalise_away_to_nothing_never_match`: it asserted that
  an artistless pair ("" / "Long Season Intro Edit" vs "" / "long season intro") matches "through
  the word-set rule". The scorer now uses fusion's rule, which needs both parts, so it no longer
  matches; the assertion now says so and a with-artist case was added. This TIGHTENS the scorer (no
  release-1 truth row lacks an artist).

## 8. Command outputs (final tree, all foreground, each waited for)

`uv run pytest --collect-only -q -p no:cacheprovider` → `2154/2251 tests collected (97 deselected)`.
Shards (`uv run pytest -q -p no:cacheprovider <files>`, `IDEA_TEST_MODE=1`); every one of the 93
root and 21 web test files is in exactly one shard (root files modulo 12, web files modulo 5, backup
alone):

```text
root-01  128 passed, 1 warning in 238.47s (0:03:58)
root-02  178 passed, 1 warning in 80.67s (0:01:20)          (includes test_free_accuracy.py)
root-03  152 passed, 3 deselected, 1 warning in 532.60s (0:08:52)   (includes test_golden_local_free.py, test_refusion.py)
root-04  264 passed, 1 warning in 58.02s
root-05  92 passed, 19 deselected, 1 warning in 74.15s (0:01:14)
root-06  170 passed, 1 warning in 169.99s (0:02:49)          (includes test_score_corpus.py)
root-07  235 passed, 2 skipped, 58 deselected, 1 warning in 79.07s (0:01:19)
root-08  120 passed, 1 warning in 300.85s (0:05:00)
root-09  119 passed, 1 warning in 48.09s
root-10  150 passed, 10 deselected, 1 warning in 99.09s (0:01:39)
root-11  75 passed, 5 deselected, 1 warning in 59.60s
root-12  74 passed, 2 deselected, 1 warning in 67.13s (0:01:07)
web-0    50 passed, 1 warning in 45.56s                      (test_backup.py)
web-1    60 passed, 1 warning in 85.57s (0:01:25)
web-2    76 passed, 1 warning in 86.30s (0:01:26)
web-3    71 passed, 1 warning in 135.67s (0:02:15)            (includes test_refusion_money.py)
web-4    80 passed, 1 warning in 90.96s (0:01:30)
web-5    58 passed, 1 warning in 84.98s (0:01:24)
```

1,759 + 395 = **2,154** = the collected total (2,152 passed, 2 skipped, 0 failed; no flake fired).
Shards root-02 and root-03 were started in one call that outran the tool's 10-minute foreground limit
and finished in the background; they were waited for and nothing else ran meanwhile. A pre-existing
test side effect created the git-ignored `data/local/hints/manual_tracklist/` in the worktree; it was
deleted.

```text
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
433 files already formatted
$ uv run python scripts/audit_fixtures.py
audited 549 files
fixture audit passed
$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

`git diff --stat` (the two new files shown with intent-to-add, then un-added again):

```text
 scripts/make_run_list.py                      | 200 +++++++++
 scripts/measure_refusion.py                   |  53 ++-
 scripts/score_corpus.py                       | 305 ++++++++++++-
 src/id_detector/fuse/episodes.py              |  68 ++-
 src/id_detector/fuse/identity.py              | 561 ++++++++++++++++++++++-
 src/id_detector/recipes.py                    |   5 +-
 tests/fixtures/corpus-mini/expected-time.json |   4 +
 tests/fixtures/corpus-mini/expected.json      |   4 +
 tests/idea_web/test_refusion_money.py         |   6 +-
 tests/test_free_accuracy.py                   | 615 ++++++++++++++++++++++++++
 tests/test_phase0a_money.py                   |  10 +-
 tests/test_phase0a_status.py                  |   4 +-
 tests/test_phase1a_compat.py                  |   6 +-
 tests/test_phase1b_targeting.py               |   8 +-
 tests/test_refusion.py                        |  18 +-
 tests/test_score_corpus.py                    |   9 +-
 16 files changed, 1802 insertions(+), 74 deletions(-)
```

`git status --short`:

```text
 M scripts/measure_refusion.py
 M scripts/score_corpus.py
 M src/id_detector/fuse/episodes.py
 M src/id_detector/fuse/identity.py
 M src/id_detector/recipes.py
 M tests/fixtures/corpus-mini/expected-time.json
 M tests/fixtures/corpus-mini/expected.json
 M tests/idea_web/test_refusion_money.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_status.py
 M tests/test_phase1a_compat.py
 M tests/test_phase1b_targeting.py
 M tests/test_refusion.py
 M tests/test_score_corpus.py
?? docs/reviews/build-local-free-accuracy.md
?? scripts/make_run_list.py
?? tests/test_free_accuracy.py
```

`git status --short -- data work`:

```text
```


## Detailed evidence for fix pass 4 — the label collision

### Cause and chosen fix

The two reviewed builds disagreed only about where presentation could read a label.  Final polish
correctly marks `Artist - ID`, including bracketed forms such as `Artist - ID (A / B)`, as
`id_unknown`.  Identity fusion therefore (correctly) skips the hint before making a text node.
Free accuracy's `_fuller_crowd_label` searched only identity `text` nodes, so it could no longer see
the fuller wording and fell back to the listener's shortened label.

The placeholder rule and `_PLACEHOLDER_TITLE` are unchanged.  `IdentitiesRecord` now has an
optional `display_only_labels` collection.  Fusion copies the parsed wording of verified
placeholder lines into that collection while continuing to skip the hint for identity.  Such a
label has no node id, assertion, work, candidate, hint-to-work mapping, evidence attachment or
counting path.  Empty collections are omitted when serialised, so old identity records and ordinary
new records retain their previous JSON shape; the checked-in identity schema was regenerated for
the optional field.  `_fuller_crowd_label` searches ordinary text-node labels plus this display-only
collection.

I chose the separate display channel because the bracket is still a placeholder under the owner's
polish rule: weakening `_PLACEHOLDER_TITLE` would let the same line participate in identity,
backing and counts.  The display matcher remains narrow.  A multi-word shortened title must be the
prefix of a longer slash-separated alternative, as before.  The requested one-word `artist - a`
case is accepted only when one alternative is exactly that one word, never merely because a longer
alternative begins with it.  Exactly one fuller label must still match.

The existing Mall Grab test now also proves that the fuller placeholder has no identity node.  The
new collision test uses `Artist - ID (A / B)` plus `artist - a` and proves all of the following at
once: the row is shown as `Artist - ID (A / B)`; the placeholder is `id_unknown` with specificity
zero; it has no candidate or work mapping; its hint id backs no episode; there is no identity node
for it; and only the independently identified crowd candidate exists.

Focused final-polish, accuracy, MixesDB and placeholder runs:

```text
$ uv run pytest -q -p no:cacheprovider tests/test_free_accuracy.py tests/test_final_polish.py
........................................................................ [ 80%]
..................                                                       [100%]
90 passed, 1 warning in 110.47s (0:01:50)

$ uv run pytest -q -p no:cacheprovider -k 'mixesdb or placeholder'
...........                                                              [100%]
11 passed, 2287 deselected, 1 warning in 11.17s
```

### Revert proof

I copied the final `src/id_detector` package to `C:\ifa\pass4-revert`, removed only the
display-only-label lookup from the copied `present/exports.py`, and ran the new test with that
scratch package first on `PYTHONPATH`.  The worktree was not neutralised.  The test fails at its
first display assertion because no row is shown under `ID (A / B)`:

```text
$ uv run pytest -q -p no:cacheprovider tests/test_free_accuracy.py::test_a_placeholder_tracklist_label_is_display_only_for_an_independent_crowd_row
F                                                                        [100%]
E       ValueError: not enough values to unpack (expected 1, got 0)
1 failed, 1 warning in 1.61s
```

The scratch copy was outside the worktree.  Its removal was refused by the execution sandbox, so
`C:\ifa\pass4-revert` remains as a disposable test-only copy; it is not part of the repository or
any read-only owner data.

### Combined re-measurement

The seven release-1 mixes were freshly re-fused from the same stored-evidence cache copy used by
the accuracy report (`C:\ifa\w`).  The harness loaded inputs through
`refusion.load_fusion_inputs`, wrote all derived episodes, identities, reports and rows only under
the new scratch directory `C:\ifa\pass4-combined`, and ran inside `scripts/offline_guard.py` with
an empty AudD token and Shazam disabled in that process.  It imported this worktree and printed
`fusion 5` and **`network attempts: 0`**.  The accuracy-reference and combined outputs were then
scored by today's scorer with every run explicitly marked `fusion:5`; each side reports exactly
`fusion [5]`.

```text
code: <worktree>\src\id_detector\__init__.py fusion 5
pooled: listed 217 recall 164/218 precision 167/198 likely 63/63 hidden {'ambiguous': 102, 'buried': 25, 'contradicted': 23, 'scatter': 7, 'short': 239}
  dj-heartstring-youtube-set: listed 21 page 19 recall 11/21 prec 11/14 likely 4/4
  final-new-set-christmas: listed 22 page 22 recall 20/23 prec 20/22 likely 11/11
  mall-grab-boiler-room-melbourne-22: listed 32 page 26 recall 17/24 prec 19/25 likely 3/3
  mph-youtube-set: listed 41 page 41 recall 34/62 prec 34/41 likely 10/10
  redo-of-best-set: listed 29 page 26 recall 17/18 prec 18/25 likely 10/10
  new-mix-jan-24th: listed 31 page 31 recall 27/30 prec 27/31 likely 10/10
  boomtown-mix: listed 41 page 41 recall 38/40 prec 38/40 likely 15/15
network attempts: 0

fp2-after/pass4-reference: listed 217 recall 164/218 precision 167/198 likely 63/63 fusion [5]
pass4-combined/pass4-combined: listed 217 recall 164/218 precision 167/198 likely 63/63 fusion [5]
```

The combined result is exactly the expected **164/218 recall, 167/198 work precision, 63/63
`likely`, 217 rows**.  Comparing the complete collapsed and uncollapsed `rows.json` documents with
the accuracy reference returns `same rows: True`: there is no difference to report by name.  In
particular the Mall Grab 17:15 row is still shown under `KETTAMA - ID (LET ME SEE U / ROK DA
HOUSE!)`; what changed is internal and correct—the placeholder supplies no identity or support.
The input copy contained 1,023 entries before and after, the corpus copy 24 before and after, and
`git status --short -- data work` is empty.  The owner's actual `work/`, `data/corpus/` and
`id-detector-deepscan` were never measurement destinations.

The golden Local Free tracklist is unchanged and its test passed.  Its SHA-256 remains
`78FF49D38F53E323DE0CC63542BFE133C121610BAFAA8057FAD2001C31973931`.

### Full foreground gates

`uv run pytest --collect-only -q -p no:cacheprovider`:

```text
2201/2298 tests collected (97 deselected) in 3.21s
```

The shard map covered all 116 `test_*.py` files exactly once (95 root, 21 web).  Each shard was one
foreground pytest process with `IDEA_TEST_MODE=1` and an empty `AUDD_API_TOKEN`, and each process
was waited through exit.  The only initial failure was the deterministic schema-current check,
which identified the required generated `identities.schema.json` update; root-07 was then rerun in
full and passed as shown below.  No load-sensitive flake fired, so neither known flaky test needed
an isolated rerun.

```text
root-01   64 passed, 1 warning in 14.51s
root-02  136 passed, 1 warning in 154.05s (0:02:34)
root-03   69 passed, 57 deselected, 1 warning in 114.47s (0:01:54)
root-04   58 passed, 1 warning in 30.00s
root-05   76 passed, 18 deselected, 1 warning in 64.52s (0:01:04)
root-06   89 passed, 10 deselected, 1 warning in 64.46s (0:01:04)
root-07  178 passed, 3 deselected, 1 warning in 339.23s (0:05:39)
root-08  127 passed, 1 warning in 103.09s (0:01:43)
root-09   61 passed, 1 deselected, 1 warning in 29.52s
root-10  160 passed, 1 warning in 92.82s (0:01:32)
root-11  122 passed, 1 skipped, 4 deselected, 1 warning in 60.44s (0:01:00)
root-12   86 passed, 2 deselected, 1 warning in 216.30s (0:03:36)
root-13  140 passed, 1 warning in 233.03s (0:03:53)
root-14  158 passed, 1 warning in 159.81s (0:02:39)
root-15   93 passed, 1 skipped, 2 deselected, 1 warning in 30.02s
root-16  187 passed, 1 warning in 14.75s
web-0     50 passed, 1 warning in 41.73s
web-1     60 passed, 1 warning in 73.83s (0:01:13)
web-2     76 passed, 1 warning in 78.04s (0:01:18)
web-3     71 passed, 1 warning in 126.24s (0:02:06)
web-4     80 passed, 1 warning in 83.57s (0:01:23)
web-5     58 passed, 1 warning in 74.09s (0:01:14)
```

**1,804 root passed + 395 web passed + 2 skipped = 2,201 collected; 0 failed.**  The 97
deselections are exactly those reported by collection.

```text
$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
442 files already formatted

$ uv run python scripts/audit_fixtures.py
audited 561 files
fixture audit passed

$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

`git status --short`:

```text
 M docs/STATUS.md
 M docs/reviews/README.md
 M docs/schemas/identities.schema.json
 M scripts/measure_refusion.py
 M scripts/score_corpus.py
 M src/id_detector/contracts.py
 M src/id_detector/fuse/episodes.py
 M src/id_detector/fuse/identity.py
 M src/id_detector/present/exports.py
 M src/id_detector/present/index.py
 M src/id_detector/recipes.py
 M tests/fixtures/corpus-mini/expected-time.json
 M tests/fixtures/corpus-mini/expected.json
 M tests/idea_web/test_refusion_money.py
 M tests/test_cost_preview.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_status.py
 M tests/test_phase1a_compat.py
 M tests/test_phase1b_targeting.py
 M tests/test_refusion.py
 M tests/test_score_corpus.py
?? docs/reviews/build-local-free-accuracy.md
?? docs/reviews/diff-review-local-free-accuracy-sol-r1.md
?? docs/reviews/diff-review-local-free-accuracy-sol-r2.md
?? docs/reviews/diff-review-local-free-accuracy-sol-r3.md
?? scripts/make_run_list.py
?? tests/test_free_accuracy.py
```

## Fix pass 5 — display-only in fact

The round-1 review found that the optional channel was display-only in storage but not in use:
`_candidate_label` promoted its wording before presentation grouping and the corpus scorer both
consumed that same promoted label.  Two independent crowd identities could therefore collapse,
and a placeholder could earn scorer credit.

The label path is now explicitly split.  `_identity_candidate_label` selects only identity-graph
labels and is the only label available to grouping, work keys, de-duplication, additive counting,
truth review and `ListedWork`.  `_display_candidate_label` may consult `display_only_labels`, but
the projection overlays its result only after grouping, suppression, same-artist decisions and
overlap filtering.  The scorer and additive counter use an identity-only projection which never
invokes the display function.  The page timeline reuses the already-final projection label instead
of resolving a label early.  Thus the page, JSON/Markdown/CUE exports and tracklist can show fuller
wording while no decision or count can see it.  The pass-4 schema/fusion producer and every
zero-node/zero-assertion/zero-mapping guarantee are unchanged.

The new regression fuses adjacent independent `artist - a` and `artist - b` crowd rows beside
`Artist - ID (A / B)`.  The collapsed presentation contains **two** rows (both may display the
fuller wording), while the scorer's production `ListedWork` builder yields exactly `artist - a`
and `artist - b`; `match_works` assigns them independently to truth rows 0 and 1.  The existing
17:15 collision test additionally pins its identity label as `kettama - let me see` while retaining
the fuller displayed line and all pass-4 assertions proving that the placeholder has no node,
candidate, work mapping, episode evidence or count.

Focused final-code outputs:

```text
$ uv run pytest -q -p no:cacheprovider tests/test_free_accuracy.py tests/test_final_polish.py
91 passed, 1 warning in 163.14s (0:02:43)

$ uv run pytest -q -p no:cacheprovider -k 'mixesdb or placeholder'
12 passed, 2287 deselected, 1 warning in 13.20s
```

### Revert proof

Before changing production code, the new two-row regression was run against the reviewed pass-4
tree.  It failed at the grouping assertion because the placeholder-derived display title had become
the grouping work key:

```text
$ uv run pytest -q -p no:cacheprovider tests/test_free_accuracy.py::test_a_placeholder_label_never_groups_or_scores_two_independent_crowd_rows
F                                                                        [100%]
E       AssertionError: assert 1 == 2
E        +  where 1 = len([...])
1 failed, 1 warning in 1.71s
```

The final tree passes that test together with both retained collision tests (`3 passed, 1 warning
in 1.36s`).  Reverting the split therefore restores the reproduced collapse before the scorer
assertions can even run.

### Seven-mix re-measurement

The seven release-1 mixes were freshly re-fused from the same stored-evidence cache copy
`C:\ifa\w`, with inputs read through `load_fusion_inputs` and every derived artefact written only
under `C:\ifa\pass5-final2`.  `scripts/offline_guard.py` wrapped the process with an empty AudD
token and Shazam disabled.  It imported this worktree, reported `fusion 5`, recorded `fusion:5` on
every per-mix score and **0 network attempts**.  The input cache inventory stayed at 1,023 entries.

```text
pooled: listed 217 recall 164/218 precision 166/198 likely 63/63 hidden {'ambiguous': 102, 'buried': 25, 'contradicted': 23, 'scatter': 7, 'short': 239}
  dj-heartstring-youtube-set: listed 21 page 19 recall 11/21 prec 11/14 likely 4/4
  final-new-set-christmas: listed 22 page 22 recall 20/23 prec 20/22 likely 11/11
  mall-grab-boiler-room-melbourne-22: listed 32 page 26 recall 17/24 prec 18/25 likely 3/3
  mph-youtube-set: listed 41 page 41 recall 34/62 prec 34/41 likely 10/10
  redo-of-best-set: listed 29 page 26 recall 17/18 prec 18/25 likely 10/10
  new-mix-jan-24th: listed 31 page 31 recall 27/30 prec 27/31 likely 10/10
  boomtown-mix: listed 41 page 41 recall 38/40 prec 38/40 likely 15/15
network attempts: 0
fusion versions: 5
per-mix fusion versions: 5
```

Against pass 4's **164/218 recall, 167/198 work precision, 63/63 `likely`, 217 rows**, only work
precision changes: **167/198 → 166/198**.  By name, only
`mall-grab-boiler-room-melbourne-22` changes: its work precision is **19/25 → 18/25**, and its
correct prediction rows are **26/32 → 25/32**.  The `possible` 17:15 row is now an unmatched
prediction under its real identity, **`kettama - let me see`**.  Recall stays 17/24 because the
independent `KETTAMA - Rok da House` work still matches that truth work; `likely` stays 3/3 and the
listed/page counts stay 32/26.  Every other mix and every other pooled number is unchanged.

The complete pass-4 and pass-5 `rows.json` files are byte-identical (SHA-256
`3FB09094BA909F581B44428909AC165385CA3C09C917F5C6728BC8B3697B85BA`): the 17:15 row is still
shown as `KETTAMA - ID (LET ME SEE U / ROK DA HOUSE!) (UR)`.  Only its scorer label changed.  No
committed test expectation represented this seven-mix score, so no expected number was altered.
The Local Free golden remains byte-identical at
`78FF49D38F53E323DE0CC63542BFE133C121610BAFAA8057FAD2001C31973931`.
`git status --short -- data/corpus work` is empty.  The ignored `data/local` fixture cache recreated
by the suite/audit was removed after verification; no test-generated files remain there.

### Full foreground gates

Final-code collection:

```text
$ uv run pytest --collect-only -q -p no:cacheprovider
2202/2299 tests collected (97 deselected) in 7.31s
```

The existing shard map omitted the subsequently added `test_cache_safety.py` and
`test_final_polish.py`; a seventeenth root shard covers those two files.  Thus all 116 `test_*.py`
files are covered exactly once (95 root, 21 web).  Every shard was one foreground pytest process
with `IDEA_TEST_MODE=1` and an empty `AUDD_API_TOKEN`:

```text
root-01   62 passed, 1 warning in 13.03s
root-02  136 passed, 1 warning in 63.56s (0:01:03)
root-03   69 passed, 57 deselected, 1 warning in 125.58s (0:02:05)
root-04   58 passed, 1 warning in 13.07s
root-05   76 passed, 18 deselected, 1 warning in 58.65s
root-06   89 passed, 10 deselected, 1 warning in 73.52s (0:01:13)
root-07  178 passed, 3 deselected, 1 warning in 561.88s (0:09:21)
root-08  127 passed, 1 warning in 178.96s (0:02:58)
root-09   61 passed, 1 deselected, 1 warning in 50.49s
root-10  160 passed, 1 warning in 192.61s (0:03:12)
root-11  122 passed, 1 skipped, 4 deselected, 1 warning in 117.50s (0:01:57)
root-12   86 passed, 2 deselected, 1 warning in 305.76s (0:05:05)
root-13  140 passed, 1 warning in 256.69s (0:04:16)
root-14  126 passed, 1 warning in 111.72s (0:01:51)
root-15   93 passed, 1 skipped, 2 deselected, 1 warning in 51.63s
root-16  187 passed, 1 warning in 25.23s
root-17   35 passed, 1 warning in 149.52s (0:02:29)
web-0     50 passed, 1 warning in 67.56s (0:01:07)
web-1     60 passed, 1 warning in 115.06s (0:01:55)
web-2     76 passed, 1 warning in 110.56s (0:01:50)
web-3     71 passed, 1 warning in 177.07s (0:02:57)
web-4     80 passed, 1 warning in 117.03s (0:01:57)
web-5     58 passed, 1 warning in 105.30s (0:01:45)
1,805 root passed + 395 web passed + 2 skipped = 2,202 collected; 0 failed.
```

Neither named load-sensitive test failed.  During an earlier pre-final-code pass, web-4 had one
unrelated `test_recorded_reads_and_their_head_answers[home]` mismatch because startup upkeep changed
the home page between GET and HEAD (`content-length` 2601 versus 2784).  Its immediate isolated
rerun passed (`1 passed in 5.07s`), the complete shard rerun passed (`80 passed in 97.81s`), and the
final-code shard above passed again.

Required final gate outputs:

```text
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
442 files already formatted
$ uv run python scripts/audit_fixtures.py
audited 561 files
fixture audit passed
$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

`git status --short`:

```text
 M docs/STATUS.md
 M docs/reviews/README.md
 M docs/schemas/identities.schema.json
 M scripts/measure_refusion.py
 M scripts/score_corpus.py
 M src/id_detector/additive.py
 M src/id_detector/contracts.py
 M src/id_detector/fuse/episodes.py
 M src/id_detector/fuse/identity.py
 M src/id_detector/present/exports.py
 M src/id_detector/present/grouping.py
 M src/id_detector/present/index.py
 M src/id_detector/present/page.py
 M src/id_detector/recipes.py
 M src/id_detector/truth_review.py
 M tests/fixtures/corpus-mini/expected-time.json
 M tests/fixtures/corpus-mini/expected.json
 M tests/idea_web/test_refusion_money.py
 M tests/test_cost_preview.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_status.py
 M tests/test_phase1a_compat.py
 M tests/test_phase1b_fusion.py
 M tests/test_phase1b_targeting.py
 M tests/test_refusion.py
 M tests/test_score_corpus.py
?? docs/reviews/build-local-free-accuracy.md
?? docs/reviews/diff-review-local-free-accuracy-sol-r1.md
?? docs/reviews/diff-review-local-free-accuracy-sol-r2.md
?? docs/reviews/diff-review-local-free-accuracy-sol-r3.md
?? scripts/make_run_list.py
?? tests/test_free_accuracy.py
```


## Fix pass 3 — read-only measurement

### Change

Measurement now crosses one explicit read-only cache boundary.  `media_map_read_only()` in
`src/id_detector/present/index.py` loads a usable `index.json`, or rebuilds that map in memory when
the index is absent/stale/damaged; it completes the map from legacy and incomplete source records
without persisting anything.  Its selected indexed alias wins, matching the result the product
opens.  `make_run_list.py` uses that keyed map and `measure_refusion.py` uses its read-only
enumeration.  `idea serve`/`idea analyse` still use `load_index()`/`rebuild_index()` and retain their
normal index-maintenance behaviour.  `score_corpus.py` consumes only the explicit run-list paths
and writes its generated artefacts outside the measured work root.

### Tests and revert proof

`test_run_list_and_scorer_never_write_the_measured_work_root` creates a cache plus a deliberately
stale three-byte `index.json`, fingerprints the complete directory listing and every file's size
and nanosecond mtime, runs `make_run_list` and `score_corpus`, and compares the fingerprint after
each command.  `test_refusion_measurement_read_only` makes the same stale-index fingerprint check
around `measure_refusion`.

Fixed tree:

```text
..                                                                       [100%]
2 passed, 1 warning in 8.84s
```

Revert proof: only the new enumeration boundary was temporarily changed from
`load_index_read_only(root)` to `load_index(root)`.  Both tests failed on the `index.json` size and
mtime (the first fixture changed from 3 bytes to 146 bytes).  The fix was restored with
`apply_patch`, and the passing run above was repeated.

```text
FF                                                                       [100%]
FAILED tests/test_free_accuracy.py::test_run_list_and_scorer_never_write_the_measured_work_root
FAILED tests/test_cost_preview.py::test_refusion_measurement_read_only
2 failed, 1 warning in 12.69s
```

The broader focused run also passed:

```text
...                                                                      [100%]
3 passed, 1 warning in 16.47s
```

### Owner cache fingerprint and score

The full metadata inventories are outside the repository at
`C:\ifa\fp3-readonly-20260928\before.csv` and `after.csv`.  Each contains the relative path, byte
size and UTC modification-time ticks of every file under the real
the owner's real `work/` in the main checkout.  The sequence was: inventory; `make_run_list`
and `score_corpus` under `scripts/offline_guard.py`; `measure_refusion` under the same guard; second
inventory and exact three-field comparison.

```text
before: files=38640 bytes=6642184643
after:  files=38640 bytes=6642184643
changed=0
network attempts: 0
```

The read-only requirement is therefore proved on the real cache.  The required score equality is
not proved because the specified real cache does not presently contain the fix-pass-2 published
results.  All seven current results resolve as pre-bundle `fusion:1`; direct `make_run_list` plus
`score_corpus` reports:

```text
Every run was decided by fusion:1. ... leaving 203 listed; it named 148 of the 218 distinct tracks
actually played; 149 of the 190 distinct tracks it listed were really played; and 62 of the 68 it
marked 'likely' or better were right.
network attempts: 0
```

`measure_refusion` reads the same root without writing it, but Boomtown is not rebuildable from
that cache (`present does not describe 3 candidate(s) this run's episodes name`), so it can score
only six of the seven truth mixes:

```text
Before: 6 truth mixes, work matching, decided by fusion:1; recall 62.9%; precision 75.8%; likely precision 90.4%.
After: 6 truth mixes, work matching, decided by fusion:5; recall 70.8%; precision 81.7%; likely precision 100.0%.
network attempts: 0
```

Thus the requested `164/218`, `167/198`, `63/63`, 217-row result cannot honestly be reproduced
from the named real-cache state without either changing that read-only cache or substituting a
different cache.  Neither was done.

The Local Free golden remains byte-identical and its suite gate passed:

```text
SHA256 78FF49D38F53E323DE0CC63542BFE133C121610BAFAA8057FAD2001C31973931
```

### Gate outputs (final code, foreground shards)

Collection:

```text
2165/2262 tests collected (97 deselected) in 6.24s
```

The eight top-level and four `idea_web` file shards cover every `test_*.py` file exactly once.

```text
root-0  135 passed, 1 deselected, 1 warning in 96.53s
root-1  283 passed, 1 warning in 363.22s (0:06:03)
root-2  199 passed, 1 skipped, 61 deselected, 1 warning in 318.56s (0:05:18)
root-3  166 passed, 2 deselected, 1 warning in 321.11s (0:05:21)
root-4  1 failed, 194 passed, 18 deselected, 1 warning in 824.96s (0:13:44)
root-5  203 passed, 11 deselected, 1 warning in 122.73s (0:02:02)
root-6  276 passed, 1 skipped, 4 deselected, 1 warning in 362.00s (0:06:02)
root-7  311 passed, 1 warning in 107.50s (0:01:47)
web-0   100 passed, 1 warning in 111.09s (0:01:51)
web-1   105 passed, 1 warning in 87.22s (0:01:27)
web-2   134 passed, 1 warning in 279.37s (0:04:39)
web-3    56 passed, 1 warning in 77.11s (0:01:17)
```

Totals: **2,162 passed, 2 skipped, 1 failed = 2,165 selected**.  The sole failure is in the
parallel-fix-owned money-resume area, not in a file changed by this pass, and failed again alone:

```text
FAILED tests/test_followup_money_resume.py::test_a_retryable_zero_cost_outcome_is_retried_after_a_crash
E AssertionError: ('partial', 'primary_not_achieved')
1 failed, 1 warning in 209.73s (0:03:29)
```

Remaining gates:

```text
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
434 files already formatted
$ uv run python scripts/audit_fixtures.py
audited 550 files
fixture audit passed
$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

`git status --short`:

```text
 M scripts/measure_refusion.py
 M scripts/score_corpus.py
 M src/id_detector/fuse/episodes.py
 M src/id_detector/fuse/identity.py
 M src/id_detector/present/exports.py
 M src/id_detector/present/index.py
 M src/id_detector/recipes.py
 M tests/fixtures/corpus-mini/expected-time.json
 M tests/fixtures/corpus-mini/expected.json
 M tests/idea_web/test_refusion_money.py
 M tests/test_cost_preview.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_status.py
 M tests/test_phase1a_compat.py
 M tests/test_phase1b_targeting.py
 M tests/test_refusion.py
 M tests/test_score_corpus.py
?? docs/reviews/build-local-free-accuracy.md
?? scripts/make_run_list.py
?? tests/test_free_accuracy.py
```

(empty)

## 9. For the orchestrator

- The owner's own `work/` is still all `fusion:1` flat files; he will see these numbers the next time
  `idea serve` starts (offline re-fusion), not before.
- Open for the owner's ear (unchanged by this build): "I Am Ready 2" / "I'm ready", "Love Yourself" /
  "No One Else Will", "Nothing Moves You" / "It Never Ends", "Surusinghe - ID" / "Likshot", "MPH - ID" /
  "La Nyc", "18Hunna" (Headie One label), "Too Many Man" (Mark Krupp label), and whether De Barrio is
  a sample in the tracks around it.
- Scratch copies live in `C:\ifa` (outside the repo) and can be deleted.


## Fix pass 1 (review `review-freeacc-round1.md`: FIX_FIRST, three P1, two P2)

Same worktree, same rules: no commit or branch; every measurement on copies of the cache inside
`scripts/offline_guard.py` (**0 network attempts** in every run); the owner's `work/` (38,920
entries), `data/corpus/` (391) and `id-detector-deepscan/` (6,265) — **0 entries modified** since
`2026-09-28T07:45:31Z`; truth files, `profiles/` and the golden untouched (the golden test passes);
nothing in `src/idea_web/**`, `paid_clip.py`, `pipeline.py` or the providers touched. Everything the
review CONFIRMED is kept. Where this section disagrees with the sections above, this section is what
the code now does.

### P1 — a replay of a crowd-only track is its own row (`fuse/episodes.py`)

Fusion used to name a crowd work only if the work was nowhere on the tracklist (`known_work_ids`).
With spellings joined into one work, that suppressed a genuine replay. Now a crowd answer names its
work unless it is in the SAME playing as an existing appearance of that work (any episode of it, or
a crowd row already listed). It counts as a separate playing (`separate_playing`) only when it is
**at least 180 s** from every appearance (`SEPARATE_PLAYING_GAP_MS`, the page's same-track bridge)
**and** a listed, `possible`-or-better track of another work lies wholly between them.
Test: `test_a_replay_of_a_crowd_only_track_under_another_spelling_is_its_own_row` — the reviewer's
case exactly: "t e s t p r e s s - Forz4" at 1:40, "Testpress - FORZ4" at 10:00, a different
confident track at 5:00. Two rows (95 s and 595 s), one work between them. Without the track between
them, one row. On the 13 cached mixes this changes no row.

### P1 — one-word titles (`fuse/identity.py`)

A changed word (stem, phonetic) is now tolerated only in a title of **two or more words**
(`_TOLERANT_TITLE_MIN_WORDS`). A one-word title matches only exactly, joined or with spaced
letters. A one-letter name slip (`artist_slip`) is accepted only beside an exact title of two or
more words. `Artist - Dreams`/`Dream`, `Artist - Night`/`Nite` and `Paige Tomlison - Feel`/`Paige
Tomlinson - Feel` now match nothing, in fusion and in the scorer.
**All nine credits survive:** FORZ4 is one word but matches exactly once the spaced letters are
joined; Feel Emotion has two words; the Tomlison slip sits beside "I Wanna Feel".
Tests: three new near-miss cases in `test_no_tolerance_lets_these_near_misses_through`;
`test_the_scorer_credits_no_one_word_stem_or_phonetic_title`, which also checks the two-word
Feel Emotion credit.

### P1 — tolerant crowd links resolve to one work and never chain (`fuse/identity.py`)

`_unique` (which accepted partners whose titles agreed) is gone, and so is `titles_agree`. The
established rule builds the crowd components first. Then component A is joined to component B only
when every tolerant partner of A's labels lies in B, and every tolerant partner of B's labels lies in
A. It is decided once, with no iteration, so tolerant links cannot chain.
Tests: `test_three_artist_spellings_are_not_bridged_into_one_work`: the reviewer's Anderson,
Andersson and Anderon on one title stay three works. The positive control joins Anderson and
Andersson on their own.
`test_spellings_joined_only_through_a_third_are_not_chained`: Mall Grab's three Long Season
spellings, where "MG - Long Season Intro Edit" fits the other two but they do not fit each other.
`test_two_spellings_of_one_track_in_one_playing_are_one_row` keeps a direct pair joined.

**Cost, stated plainly.** The Long Season collapse that the first report counted as an item-2 win
WAS a chain. "Fishmans - Long season MG Edit" ↔ "MG - Long Season Intro Edit" ↔ "Long Season Intro
Edit - Mall Grab" (and "fishmans - long season" via the first). The two ends do not match each
other. Under the reviewer's rule none of them may be joined, so the Mall Grab opener is back to the
**three crowd rows it had before this build**. The 1ofthozedaze pair stays joined: its two spellings
fit each other directly, with nothing bridging. I changed one assertion in my OWN new test
(`test_three_comment_spellings_of_one_track_are_one_row`, first pass). It became
`test_spellings_joined_only_through_a_third_are_not_chained`, and it now asserts three rows. One
assertion that existed at `361fe94` also changed: the artistless scorer expectation in
`tests/test_score_corpus.py::test_labels_that_normalise_away_to_nothing_never_match` was tightened
from `{0: 0}` to `{}` because fusion's identity rule requires both artist and title; it was
tightened, not loosened.

### P2 — a run-list `fusion_version` is checked against the result (`scripts/score_corpus.py`)

`stated_fusion_version` reads the result's own provenance first. A run-list value that contradicts it
is refused ("the run list says fusion:5 but the result's own provenance says fusion:1; fix the run
list …", exit 1). The run-list value is used only where the artefacts record nothing.
Test: `test_a_run_list_fusion_version_that_contradicts_the_result_is_refused`, on a flat result whose
journal says `fusion:1`, stated as 5 then as 1.

### P2 — the 17:15 Mall Grab label (`present/exports.py`)

`_candidate_label` gets `_fuller_crowd_label`, which changes only what is displayed. A crowd row's
label that is the leading part of ONE alternative in a "/"-bracket of another text label with the
same artist is SHOWN as that label. Example: "kettama - let me see" is shown as "KETTAMA - ID (LET ME
SEE U / ROK DA HOUSE!) (UR)". It needs a title of two or more words, and exactly one such label in
the mix, otherwise no guess. Identity is untouched: nothing is merged, backed or counted, and the ID
line still joins the recognised Rok da House. The row reads as it did before this build and gets its
scorer credit back.
Test: `test_a_crowd_row_is_shown_under_the_fuller_tracklist_line_it_shortens` — including "the
works stay apart" and "two candidate lines → no guess". On the 13 cached mixes it changes exactly
this one row.

De Barrio: left as a recorded residual, no rule added (the review agrees).

### Revert proofs (each neutralised alone in a scratch copy; `tests/test_free_accuracy.py`, 54 tests)

| Neutralised | Result | Failing |
|---|---|---|
| replay: any existing appearance suppresses (old `known_work_ids`) | 1 failed | replay is its own row |
| one-word guard off (`_TOLERANT_TITLE_MIN_WORDS = 1`) | 4 failed | Dreams, Night, Tomlison+Feel near-misses; scorer one-word test |
| name-slip-beside-one-word filter off | 1 failed | Tomlison+Feel near-miss |
| crowd links may chain (any partner accepted) | 2 failed | not chained; three artists not bridged |
| fuller label off | 1 failed | shown under the fuller line |
| run-list version trusted blindly | 1 failed | contradicting version refused |

All 15 first-pass neutralisations were re-run on the new tree and still fail (M1: 19 failed; M2: 3; M3, M4, M7, M9, M10, M12, M13, M14: 1 each;
M5, M6, M8, M11: 2 each; M15: 3). The two guard tests noted in section 7 are still guards.

### Re-measurement (same stored evidence; before = `fusion:4` code, after = `fusion:5` code; one scorer — today's)

Real start-up path on a fresh cache copy (`refuse_stale_result`): 11 re-fused in 26.1 s. The two Deep
mixes were refused as paid and BENWAL for its inconsistent sidecar; **0 network attempts**. Then
`make_run_list` resolved all seven to `bundle result, fusion:5`, and `score_corpus` printed:

```text
Every run was decided by fusion:5. … Of the 612 tracks the tool found, the presentation floor hid 102 as ambiguous, 25 as buried, 23 as contradicted, 7 as scatter, 239 as short, leaving 216 listed; it named 164 of the 218 distinct tracks actually played (work recall 75.2%); 167 of the 198 distinct tracks it listed were really played (work precision 84.3%); and 63 of the 63 it marked 'likely' or better were right (likely precision 100.0%).
```

The before side (the first report's fusion:4 start-up copy, rescored with today's scorer): `Every run
was decided by fusion:4. … 613 tracks … leaving 228 listed; … 164 of the 218 … 168 of the 208 … 63 of
the 64`. The in-memory harness gives identical numbers on both sides.

Cells: recall · work precision · likely · rows (scorer / page):

| Mix | Before (`fusion:4`) | Item 1+2 only | Item 3 only | **After (fix pass 1)** | First report's after |
|---|---|---|---|---|---|
| `boomtown-mix` | 38/40 · 38/43 · 15/16 · 44/44 | 38/40 · 38/43 · 15/16 · 44/44 | 38/40 · 38/40 · 15/15 · 41/41 | 38/40 · 38/40 · 15/15 · 41/41 | same |
| `dj-heartstring-youtube-set` | 11/21 · 11/15 · 4/4 · 24/21 | 11/21 · 11/15 · 4/4 · 24/21 | 11/21 · 11/14 · 4/4 · 21/19 | 11/21 · 11/14 · 4/4 · 21/19 | same |
| `final-new-set-christmas` | 20/23 · 20/22 · 11/11 · 22/22 | same | same | 20/23 · 20/22 · 11/11 · 22/22 | same |
| `mall-grab-boiler-room-melbourne-22` | 17/24 · 20/26 · 3/3 · 33/28 | 17/24 · 19/25 · 3/3 · 32/27 | 17/24 · 20/26 · 3/3 · 33/27 | **17/24 · 19/25 · 3/3 · 32/26** | 17/24 · 17/23 · 3/3 · 30/24 |
| `mph-youtube-set` | 34/62 · 34/41 · 10/10 · 41/41 | same | same | 34/62 · 34/41 · 10/10 · 41/41 | same |
| `new-mix-jan-24th` | 27/30 · 27/33 · 10/10 · 33/33 | same | 27/30 · 27/31 · 10/10 · 31/31 | 27/30 · 27/31 · 10/10 · 31/31 | same |
| `redo-of-best-set` | 17/18 · 18/28 · 10/10 · 31/28 | same | 17/18 · 18/25 · 10/10 · 28/25 | 17/18 · 18/25 · 10/10 · 28/25 | same |
| **Pooled** | **164/218 · 168/208 · 63/64 · 228/217** | **164/218 · 167/207 · 63/64 · 227/216** | **164/218 · 168/199 · 63/63 · 217/206** | **164/218 · 167/198 · 63/63 · 216/205** | 164/218 · 165/196 · 63/63 · 214/203 |

Headline: **recall 164/218 → 164/218; work precision 80.8 % → 84.3 %; `likely` 98.4 % → 100 %; rows
228 → 216 (page 217 → 205).** Old scorer, for continuity: 157/218, 159/198, 62/63.

**Every change from the first report, by name (all 13 cached mixes; only Mall Grab moved):**
- Returns: "MG - Long Season Intro Edit" (0:46), "Long Season Intro Edit - Mall Grab" (2:04) and
  "fishmans - long season" (2:32) come back as three crowd rows. In the first report they were one
  row, "Fishmans - Long season MG Edit" (0:46); that row disappears. Before this build there were
  also three rows, with the third labelled "Fishmans - Long season MG Edit" at 2:32. The scorer
  pairs two of the three with the truth's Long Season. "Long Season Intro Edit - Mall Grab" is a
  wrong label to the scorer, as it was before this build.
- Relabelled: 17:15 "kettama - let me see" → "KETTAMA - ID (LET ME SEE U / ROK DA HOUSE!) (UR)"
  (as before this build; scorer credit restored).
- Unchanged: the 1ofthozedaze collapse, all 11 phantom removals, and all nine gained tracks. The
  occurrence check was re-run and matches the first report row for row.
- Tolerant links now made on the cache: the six hint→audio links and the artist slip listed in
  section 1, plus the 1ofthozedaze initials pair. The Long Season crowd↔crowd links are no longer
  made.

### Gate outputs (final tree, all foreground)

`uv run pytest --collect-only -q -p no:cacheprovider` → `2163/2260 tests collected (97 deselected)`.
Shards: root files modulo 16, web files modulo 5, backup alone; each of the 93 root and 21 web files
appears exactly once:

```text
root-01  62 passed, 1 warning in 8.11s
root-02  136 passed, 1 warning in 49.27s
root-03  69 passed, 57 deselected, 1 warning in 96.59s (0:01:36)
root-04  58 passed, 1 warning in 12.10s
root-05  76 passed, 18 deselected, 1 warning in 42.07s
root-06  89 passed, 10 deselected, 1 warning in 54.21s
root-07  178 passed, 3 deselected, 1 warning in 266.04s (0:04:26)     (test_refusion.py)
root-08  127 passed, 1 warning in 85.97s (0:01:25)
root-09  61 passed, 1 deselected, 1 warning in 22.84s
root-10  160 passed, 1 warning in 80.70s (0:01:20)                    (test_score_corpus.py)
root-11  122 passed, 1 skipped, 4 deselected, 1 warning in 53.41s
root-12  86 passed, 2 deselected, 1 warning in 210.09s (0:03:30)
root-13  140 passed, 1 warning in 220.11s (0:03:40)
root-14  122 passed, 1 warning in 58.39s                              (test_free_accuracy.py)
root-15  93 passed, 1 skipped, 2 deselected, 1 warning in 28.57s     (test_golden_local_free.py)
root-16  187 passed, 1 warning in 10.74s
web-0    50 passed, 1 warning in 32.82s
web-1    60 passed, 1 warning in 73.31s (0:01:13)
web-2    76 passed, 1 warning in 66.18s (0:01:06)
web-3    71 passed, 1 warning in 115.67s (0:01:55)
web-4    80 passed, 1 warning in 81.90s (0:01:21)
web-5    58 passed, 1 warning in 62.94s (0:01:02)
```

1,766 + 2 skipped + 395 = **2,163** = the collected total; 0 failed. No flake fired, and no shard came
near 10 minutes.

```text
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
434 files already formatted
$ uv run python scripts/audit_fixtures.py
audited 550 files
fixture audit passed
$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

`git diff --stat` (new files with intent-to-add, then un-added):

```text
 docs/reviews/build-local-free-accuracy.md     | 403 ++++++++++++++
 scripts/make_run_list.py                      | 200 +++++++
 scripts/measure_refusion.py                   |  53 +-
 scripts/score_corpus.py                       | 317 ++++++++++-
 src/id_detector/fuse/episodes.py              | 135 ++++-
 src/id_detector/fuse/identity.py              | 576 +++++++++++++++++++-
 src/id_detector/present/exports.py            |  39 +-
 src/id_detector/recipes.py                    |   5 +-
 tests/fixtures/corpus-mini/expected-time.json |   4 +
 tests/fixtures/corpus-mini/expected.json      |   4 +
 tests/idea_web/test_refusion_money.py         |   6 +-
 tests/test_free_accuracy.py                   | 751 ++++++++++++++++++++++++++
 tests/test_phase0a_money.py                   |  10 +-
 tests/test_phase0a_status.py                  |   4 +-
 tests/test_phase1a_compat.py                  |   6 +-
 tests/test_phase1b_targeting.py               |   8 +-
 tests/test_refusion.py                        |  18 +-
 tests/test_score_corpus.py                    |   9 +-
 18 files changed, 2466 insertions(+), 82 deletions(-)
```

(The report line count predates this section.)

`git status --short`:

```text
 M scripts/measure_refusion.py
 M scripts/score_corpus.py
 M src/id_detector/fuse/episodes.py
 M src/id_detector/fuse/identity.py
 M src/id_detector/present/exports.py
 M src/id_detector/recipes.py
 M tests/fixtures/corpus-mini/expected-time.json
 M tests/fixtures/corpus-mini/expected.json
 M tests/idea_web/test_refusion_money.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_status.py
 M tests/test_phase1a_compat.py
 M tests/test_phase1b_targeting.py
 M tests/test_refusion.py
 M tests/test_score_corpus.py
?? docs/reviews/build-local-free-accuracy.md
?? scripts/make_run_list.py
?? tests/test_free_accuracy.py
```

`git status --short -- data work`:

```text
```

(empty; a pre-existing test side effect recreated the git-ignored `data/local/` in the worktree
and it was deleted again.)


## Fix pass 2 (review `review-freeacc-round2.md`: FIX_FIRST, one P1)

Same worktree, no branch or commit. The existing change and regression were correct and complete:
`separate_playing` now has **no elapsed-time floor**. A crowd mention is a separate playing only
when a listed, `possible`-or-better episode of another work lies wholly between it and **every**
other appearance of its work. With no intervening track, mentions remain one row however far apart.
No other production or test code was changed in this pass.

### Change, test, and revert proof

`test_a_replay_150_seconds_later_around_a_different_track_is_its_own_row` puts Testpress mentions at
1:40 and 4:10 and a confident different track wholly between. The fixed tree lists Testpress at
1:35 and 4:05. The test also removes the intervening track and checks that the same mentions remain
one row. The existing longer replay/no-split test at line 680 still lists the 1:35 and 9:55 replays,
then checks that removing its intervening track leaves one row.

Fixed tree, both replay tests together:

```text
..                                                                       [100%]
2 passed, 1 warning in 1.69s
```

For the required revert proof, `src/` was copied to `C:\ifa\fp2-revert-proof`, a
`SEPARATE_PLAYING_GAP_MS = 180_000` check was restored there only, and the new test was run with the
scratch package first on `PYTHONPATH`:

```text
FAILED tests/test_free_accuracy.py::test_a_replay_150_seconds_later_around_a_different_track_is_its_own_row
E       assert [95000] == [95000, 245000]
1 failed, 1 warning in 1.29s
```

The scratch copy was deleted afterwards; the worktree was never neutralised.

### Re-measurement

Both sides were freshly re-fused from the same stored evidence copy (`C:\ifa\w`) by the existing
in-memory harness: before from the `361fe94` archive (`C:\ifa\base`, `fusion:4`), after from this
worktree (`fusion:5`). Both were then scored by today's scorer, whose documents state exactly
`fusion_versions: [4]` and `[5]` respectively and the same version on every per-mix entry. Each
process had an empty `AUDD_API_TOKEN`, `IDEA_ENGINE_SHAZAM=off` only in that process, and
`scripts/offline_guard.py` active. Both re-fusion runs printed **`network attempts: 0`**; the
rescoring also ran inside the offline guard.

Cells are recall · work precision · `likely` precision · rows listed (scorer / collapsed page):

| Mix | Before (`fusion:4`) | After (`fusion:5`) |
|---|---|---|
| `boomtown-mix` | 38/40 · 38/43 · 15/16 · 44 / 44 | 38/40 · 38/40 · 15/15 · 41 / 41 |
| `dj-heartstring-youtube-set` | 11/21 · 11/15 · 4/4 · 24 / 21 | 11/21 · 11/14 · 4/4 · 21 / 19 |
| `final-new-set-christmas` | 20/23 · 20/22 · 11/11 · 22 / 22 | 20/23 · 20/22 · 11/11 · 22 / 22 |
| `mall-grab-boiler-room-melbourne-22` | 17/24 · 20/26 · 3/3 · 33 / 28 | 17/24 · 19/25 · 3/3 · 32 / 26 |
| `mph-youtube-set` | 34/62 · 34/41 · 10/10 · 41 / 41 | 34/62 · 34/41 · 10/10 · 41 / 41 |
| `new-mix-jan-24th` | 27/30 · 27/33 · 10/10 · 33 / 33 | 27/30 · 27/31 · 10/10 · 31 / 31 |
| `redo-of-best-set` | 17/18 · 18/28 · 10/10 · 31 / 28 | 17/18 · 18/25 · 10/10 · 29 / 26 |
| **Pooled** | **164/218 · 168/208 · 63/64 · 228 / 217** | **164/218 · 167/198 · 63/63 · 217 / 206** |

Pooled headline: recall **75.2% → 75.2%**, work precision **80.8% → 84.3%**, `likely`
precision **98.4% → 100%**, rows **228 → 217** (collapsed page **217 → 206**).

Compared by episode ID and displayed row with fix pass 1 (`164/218`, `167/198`, `63/63`, 216
listed / 205 page), exactly one row appears and none disappears: **`Y U QT - Original Don (feat.
Riko Dan)`**, `possible`, hint-only, 10 s, at **23:00** in `redo-of-best-set`. The already-existing
engine row for the same work remains at 24:12, so this adds an occurrence row (216 → 217; page
205 → 206) but no distinct predicted work: recall, work precision and `likely` precision are
unchanged from fix pass 1.

### Corrected assertion sentence and known residual

The fix-pass-1 sentence claiming that no assertion from `361fe94` changed is corrected above.
`tests/test_score_corpus.py::test_labels_that_normalise_away_to_nothing_never_match` changed its
artistless expectation from `{0: 0}` to the stricter `{}`. That tightened a pre-existing assertion;
it did not loosen one.

Known residual, deliberately not special-cased: a long crowd-only track continuing underneath a
shorter listed overlay can be split into two rows because the evidence cannot prove the first track
stopped. The newly visible 23:00 Original Don occurrence is this shape: another listed episode is
wholly between it and the existing Original Don episode. No rule was added for it.

### Read-only and golden verification

The golden Local Free file stayed byte-identical (SHA-256
`78FF49D38F53E323DE0CC63542BFE133C121610BAFAA8057FAD2001C31973931`) and its gate passed.
Timestamp audit from `2026-09-28T07:45:31Z`: `data/corpus/` 391 entries, **0 modified**;
`id-detector-deepscan/` 6,265 entries, **0 modified**. One non-truth cache file under the owner's
read-only `work/` was atomically refreshed during the measurement window:
`work/index.json` at `2026-09-28T11:31:54Z`. Every measurement command explicitly named the copy
`C:\ifa\w`; the refreshed index is byte-identical to `C:\ifa\w\index.json`
(`1E1376B5A5EA0140989ECBB430D0A08B4CE67C1682DCC85AE666E10777B09BB5`), and no stored media,
evidence, bundle, or truth file changed. The suite-created, ignored `data/local/` fixture artifact
was deleted.

### Gate outputs (final code, all foreground)

`uv run pytest --collect-only -q -p no:cacheprovider`:

```text
2164/2261 tests collected (97 deselected) in 5.01s
```

The saved shard map covered all 114 `test_*.py` files exactly once (93 root, 21 web). Every shard
was run with `IDEA_TEST_MODE=1`, one foreground process at a time, and waited through exit. The first
two were rerun strictly one-at-a-time after the runner initially yielded both session handles:

```text
root-01   62 passed, 1 warning in 12.35s
root-02  136 passed, 1 warning in 62.60s (0:01:02)
root-03   69 passed, 57 deselected, 1 warning in 149.07s (0:02:29)
root-04   58 passed, 1 warning in 23.57s
root-05   76 passed, 18 deselected, 1 warning in 74.19s (0:01:14)
root-06   89 passed, 10 deselected, 1 warning in 80.69s (0:01:20)
root-07  178 passed, 3 deselected, 1 warning in 436.12s (0:07:16)
root-08  127 passed, 1 warning in 133.32s (0:02:13)
root-09   61 passed, 1 deselected, 1 warning in 40.19s
root-10  160 passed, 1 warning in 125.69s (0:02:05)
root-11  122 passed, 1 skipped, 4 deselected, 1 warning in 75.92s (0:01:15)
root-12   86 passed, 2 deselected, 1 warning in 243.17s (0:04:03)
root-13  140 passed, 1 warning in 219.28s (0:03:39)
root-14  123 passed, 1 warning in 72.76s (0:01:12)  (includes test_free_accuracy.py)
root-15   93 passed, 1 skipped, 2 deselected, 1 warning in 37.87s  (golden)
root-16  187 passed, 1 warning in 21.64s
web-0     50 passed, 1 warning in 53.81s
web-1     60 passed, 1 warning in 88.36s (0:01:28)
web-2     76 passed, 1 warning in 78.58s (0:01:18)
web-3     71 passed, 1 warning in 135.26s (0:02:15)
web-4     80 passed, 1 warning in 90.71s (0:01:30)
web-5     58 passed, 1 warning in 83.14s (0:01:23)
```

**1,767 root passed + 395 web passed + 2 skipped = 2,164 collected; 0 failed.** The 97 deselections
are exactly those reported by collection. No shard exceeded 10 minutes.

```text
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
434 files already formatted
$ uv run python scripts/audit_fixtures.py
audited 550 files
fixture audit passed
$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

Final repository snapshots follow (the diff-stat snapshot was taken immediately before pasting
these snapshot blocks, so its report line count excludes the blocks themselves):

`git diff --stat`:

```text
 docs/reviews/build-local-free-accuracy.md     | 818 ++++++++++++++++++++++++++
 scripts/make_run_list.py                      | 200 +++++++
 scripts/measure_refusion.py                   |  53 +-
 scripts/score_corpus.py                       | 317 +++++++++-
 src/id_detector/fuse/episodes.py              | 132 ++++-
 src/id_detector/fuse/identity.py              | 576 +++++++++++++++++-
 src/id_detector/present/exports.py            |  39 +-
 src/id_detector/recipes.py                    |   5 +-
 tests/fixtures/corpus-mini/expected-time.json |   4 +
 tests/fixtures/corpus-mini/expected.json      |   4 +
 tests/idea_web/test_refusion_money.py         |   6 +-
 tests/test_free_accuracy.py                   | 772 ++++++++++++++++++++++++
 tests/test_phase0a_money.py                   |  10 +-
 tests/test_phase0a_status.py                  |   4 +-
 tests/test_phase1a_compat.py                  |   6 +-
 tests/test_phase1b_targeting.py               |   8 +-
 tests/test_refusion.py                        |  18 +-
 tests/test_score_corpus.py                    |   9 +-
 18 files changed, 2899 insertions(+), 82 deletions(-)
```

`git status --short`:

```text
 M scripts/measure_refusion.py
 M scripts/score_corpus.py
 M src/id_detector/fuse/episodes.py
 M src/id_detector/fuse/identity.py
 M src/id_detector/present/exports.py
 M src/id_detector/recipes.py
 M tests/fixtures/corpus-mini/expected-time.json
 M tests/fixtures/corpus-mini/expected.json
 M tests/idea_web/test_refusion_money.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_status.py
 M tests/test_phase1a_compat.py
 M tests/test_phase1b_targeting.py
 M tests/test_refusion.py
 M tests/test_score_corpus.py
?? docs/reviews/build-local-free-accuracy.md
?? scripts/make_run_list.py
?? tests/test_free_accuracy.py
```

`git status --short -- data work`:

```text
```


## Fix pass 4 — the label collision

The collision came from two individually correct decisions: final polish excluded bracketed `ID`
placeholders before identity-node creation, while free accuracy's display-only fuller-label lookup
searched only identity nodes.  I kept `_PLACEHOLDER_TITLE` unchanged.  Fusion now carries verified
placeholder wording in optional `display_only_labels`; those strings have no node id, assertion,
work, candidate, hint mapping, evidence attachment or count.  Presentation alone reads them.  The
requested one-word case matches only an exact one-word slash alternative; the existing multi-word
longer-prefix and unique-match guards remain.

The added `Artist - ID (A / B)` / `artist - a` test proves fuller display and zero placeholder
identity/backing.  With only the new lookup reverted in a scratch package it fails at the fuller-row
assertion (`ValueError: not enough values to unpack`, 1 failed in 1.61s).  Final focused outputs:

```text
90 passed, 1 warning in 110.47s (0:01:50)
11 passed, 2287 deselected, 1 warning in 11.17s
```

The seven stored-evidence mixes were re-fused under `scripts/offline_guard.py` with **0 network
attempts**; the accuracy-reference and combined scores each contain only `fusion:5`.  Combined:
**164/218 recall, 167/198 work precision, 63/63 `likely`, 217 rows**.  The complete reference and
combined row documents are equal, so no named output differs.  The Mall Grab placeholder now has no
identity while the independent 17:15 row still displays its fuller wording.  The golden Local Free
hash remains `78FF49D38F53E323DE0CC63542BFE133C121610BAFAA8057FAD2001C31973931`.

Full foreground shards, total checked by collection:

```text
$ uv run pytest --collect-only -q -p no:cacheprovider
2201/2298 tests collected (97 deselected) in 3.21s
root-01   64 passed, 1 warning in 14.51s
root-02  136 passed, 1 warning in 154.05s (0:02:34)
root-03   69 passed, 57 deselected, 1 warning in 114.47s (0:01:54)
root-04   58 passed, 1 warning in 30.00s
root-05   76 passed, 18 deselected, 1 warning in 64.52s (0:01:04)
root-06   89 passed, 10 deselected, 1 warning in 64.46s (0:01:04)
root-07  178 passed, 3 deselected, 1 warning in 339.23s (0:05:39)
root-08  127 passed, 1 warning in 103.09s (0:01:43)
root-09   61 passed, 1 deselected, 1 warning in 29.52s
root-10  160 passed, 1 warning in 92.82s (0:01:32)
root-11  122 passed, 1 skipped, 4 deselected, 1 warning in 60.44s (0:01:00)
root-12   86 passed, 2 deselected, 1 warning in 216.30s (0:03:36)
root-13  140 passed, 1 warning in 233.03s (0:03:53)
root-14  158 passed, 1 warning in 159.81s (0:02:39)
root-15   93 passed, 1 skipped, 2 deselected, 1 warning in 30.02s
root-16  187 passed, 1 warning in 14.75s
web-0     50 passed, 1 warning in 41.73s
web-1     60 passed, 1 warning in 73.83s (0:01:13)
web-2     76 passed, 1 warning in 78.04s (0:01:18)
web-3     71 passed, 1 warning in 126.24s (0:02:06)
web-4     80 passed, 1 warning in 83.57s (0:01:23)
web-5     58 passed, 1 warning in 74.09s (0:01:14)
1,804 root passed + 395 web passed + 2 skipped = 2,201 collected; 0 failed.
```

Neither known load-sensitive test failed.  Required gate outputs:

```text
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
442 files already formatted
$ uv run python scripts/audit_fixtures.py
audited 561 files
fixture audit passed
$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
```

`git status --short`:

```text
 M docs/STATUS.md
 M docs/reviews/README.md
 M docs/schemas/identities.schema.json
 M scripts/measure_refusion.py
 M scripts/score_corpus.py
 M src/id_detector/contracts.py
 M src/id_detector/fuse/episodes.py
 M src/id_detector/fuse/identity.py
 M src/id_detector/present/exports.py
 M src/id_detector/present/index.py
 M src/id_detector/recipes.py
 M tests/fixtures/corpus-mini/expected-time.json
 M tests/fixtures/corpus-mini/expected.json
 M tests/idea_web/test_refusion_money.py
 M tests/test_cost_preview.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_status.py
 M tests/test_phase1a_compat.py
 M tests/test_phase1b_targeting.py
 M tests/test_refusion.py
 M tests/test_score_corpus.py
?? docs/reviews/build-local-free-accuracy.md
?? docs/reviews/diff-review-local-free-accuracy-sol-r1.md
?? docs/reviews/diff-review-local-free-accuracy-sol-r2.md
?? docs/reviews/diff-review-local-free-accuracy-sol-r3.md
?? scripts/make_run_list.py
?? tests/test_free_accuracy.py
```

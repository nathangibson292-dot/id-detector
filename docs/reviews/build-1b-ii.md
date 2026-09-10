# Build 1b-ii — Corroboration and crowd rows

One plan cycle, uncommitted on `main` at `54d5c21` for review. No dependency was added;
`docs/PLAN-v2.md`, `profiles/` and `data/corpus/` are untouched. No live provider call was made:
every command ran with `AUDD_API_TOKEN=` and `IDEA_ENGINE_SHAZAM=off` exported, every pipeline run
used the injected fakes (`tests/fakes/providers.py`), no analysis was started, and `work/` was only
ever READ — the corpus A/B copies each cached run's generation-0 inputs into `%TEMP%/idea-runs/`
and re-fuses them there.

## File-by-file changes

- `src/id_detector/fuse/episodes.py` — plan §2.3.4 step 5. `TRUST_FAMILIES` / `trust_family`
  (`catalogue` {audd, acrcloud}, `shazam`, `local_index` {panako}; an unknown provider is its own
  family; crowd hints stay hint corroboration), `CORROBORATION_OVERLAP_MIN_MS` /
  `CORROBORATION_SEPARATION_MIN_MS` (the Deep values, 6 000 / 60 000, and what any other fuse
  uses), `engine_agreements` (pairs of selected votes from different families whose supports
  overlap ≥ `overlap_min_ms`, as overlap intervals), `separated_agreements` (two agreements ≥
  `separation_min_ms` apart, start to start), `_engine_corroborated` reworked on top of them.
  `_independent_trials_e4` attributes a trial by family (E-L5): the half prior applies only when
  *every* engine that voted in the trial is a discounted second commercial engine.
  `build_episodes(..., overlap_min_ms, separation_min_ms)`: agreements are pooled per normalised
  WORK (an AudD ISRC and a Shazam key are two recording candidates of one work and never share a
  vote bucket); an episode carries the agreements inside its own supports as
  `engine_corroborated` ("confirmed twice") and, when two lie ≥ `separation_min_ms` apart,
  `engine_corroborated_separated`; the suppression-immunity set (`confident_spans`,
  `_suppressed_reason`) and the rescan-eligibility mirror of the floor (`_episode_listed`) now
  key on the separated flag — the `likely` rule is byte-for-byte as it was (E-S6). Crowd rows
  (E-M4, U-F13): `plausible_crowd_label`, `crowd_answer_clusters` (listable answers in position
  order, clustered where their position ranges intersect — a transitive sweep), `rank_crowd_works`
  (independent answers → trusted voice → parse confidence → position → work id); the winner is
  listed with every one of its answers as evidence and the other works' candidates as
  `alternatives`; `listed_spans` is extended as rows are made. `fuse_generation` /
  `fuse_generation_zero` thread the two thresholds.
- `src/id_detector/fuse/identity.py` — the two matcher edges the corpus A/B adopted (below):
  `_FEATURING` (ft / feat / featuring) carries no identity in the hint↔audio word set, and
  `_word_sets_corroborate` accepts a single short alphabetic word one *trailing* letter apart
  ("kno"/"know", "od"/"odf"; never a substitution, a digit or a single letter).
- `src/id_detector/orchestrate.py` — `run_generation_loop(..., overlap_min_ms, separation_min_ms)`
  passed to both `fuse_generation` calls (generation 0 and every rescan generation).
- `src/id_detector/cli.py` — `_fuse` passes the requested recipe's `overlap_min_ms` /
  `separation_min_ms` (the Free recipe defines neither → the fuser's defaults); the `--recipe`
  help no longer claims the legacy `max_accuracy` profile defaults to deep (E-M6).
- `src/id_detector/present/exports.py` — the entry carries `engine_corroborated_separated`;
  `short_track` exempts a row on that flag only (one agreement is "confirmed twice" but still
  short); a `hint_only` row's `alternatives` are its contradicting answers
  (`_crowd_alternative_summary`, kept ahead of the grouping's folded-in versions); Markdown
  `+CONFIRMED TWICE` (was `+2ENGINES`) and "also named in the comments — …" for crowd rows.
- `src/id_detector/present/page.py` — the tag reads "confirmed twice" (was "cross-checked"), a
  crowd row's disclosure reads "N other answer(s) in the comments", `PAGE_VERSION` 17 → 18 so
  already-analysed mixes pick the wording up on open.
- `tests/golden/local-free/tracklist.json` — regenerated with `scripts/make_golden.py`; the diff is
  exactly three added lines, `"engine_corroborated_separated": false` on each entry. The fusion
  change alone was verified against the *committed* golden first (green); only the presentation
  key needed the regeneration.
- `tests/test_engine_corroboration.py` — updated to the family / overlap API (AudD + ACRCloud is
  one family, Shazam + Panako two, a 3 s coincidence is not an agreement).
- `tests/fixtures/deep/{c3-scenario,single-coincidence,two-separated,crowd-contradiction}.json`
  — **new**, the four plan-named fixtures.
- `tests/test_phase1b_fusion.py` — **new**, the phase gate (20 tests).
- `docs/reviews/README.md` — the 1b-ii row.

## Fixtures — `tests/fixtures/deep/`

*Vote* fixtures name the frozen windows each engine matched; the test turns them into
observations through the real adapter converters (`shazam.response_to_observation` with the
shipped measured `shazam-v3` config, `audd.clip_response_to_observation`), so trial sources,
anchors and provider ids are what a run produces. `crowd-contradiction` is comment text read by
the real parser and relations pass.

| Fixture | What it pins |
|---|---|
| `c3-scenario` | 7 Shazam + 7 AudD windows agreeing on a 60 s mix: 14 selected votes, no `hypothesis_rejected`, badge stays `likely` (T_ind 4, span 60 s, global alignment — the unchanged rule), 8 agreements (every shared window plus the two tail windows overlapping 9 s across engines), not separated (starts 0–48 s apart), listed |
| `single-coincidence` | one shared window: `engine_corroborated`, 1 agreement, badge `unclear`, no bypass — hidden `short` at the 30 s floor |
| `two-separated` | agreements at windows 0 and 13 (117 s apart) on a 24 s-on-air, 129 s-hull track: `engine_corroborated_separated`, scatter suppression bypassed, floor bypassed; the `variant` (AudD on window 0 only) is "confirmed twice" but stays suppressed `scatter` |
| `crowd-contradiction` | three answers at ~3:00 (two independent listeners name X — one written "Title - Artist" — one names Y) → one row, X listed with both answers as evidence and Y as its alternative; a paragraph answer ("… by MPH? …") and a question dropped; a lone answer at 5:00 listed |

## Tests added — `tests/test_phase1b_fusion.py` (20)

Families and defaults; `c3-scenario` (plus: every AudD vote carries `simultaneous_source=audd`
and a reliable timecode anchor; fourteen selected votes still count four independent trials);
`single-coincidence`; `two-separated` and its variant; the separation rule (start to start, two
agreements, the threshold is a parameter); agreement across families not providers (AudD +
ACRCloud never, Shazam + Panako yes); the overlap floor and that the recipe can move it (3 s
floor admits adjacent windows, 13 s floor refuses a shared one); agreements pooled per work across
recording candidates (Shazam key + AudD ISRC → 2 candidates, 1 work, both episodes confirmed);
trial attribution by family (L5: a mixed trial counts full, alphabetical attribution would not);
`crowd-contradiction` end to end through fusion, `flatten_tracklist`, the page ("1 other answer
in the comments") and the Markdown export ("also named in the comments — …"); the plausibility
rule on real and comment-shaped labels; ranking (uploader beats a fan on a tie, two fans outvote
the uploader, 10 s-apart answers are two clusters, a listed span swallows answers under it); the
two adopted matcher edges (featuring marker; trailing-letter rule with its negatives); E-M3 (a lone
AudD phantom is a `listed_not_confident` candidate probed ahead of the blanks around it); E-M6
(`--profile max_accuracy` alone → free, with `--recipe deep` or `--engine audd` → deep; the help
text); the floor bypass needs the separated flag; "confirmed twice" on the page and in the Markdown
(`PAGE_VERSION ≥ 18`); two end-to-end Deep runs on `overlap-allowed` (default recipe → the Shazam-
probed tracks are confirmed twice in `fuse/episodes.json`, `tracklist.json` and the Markdown, never
separated on a 60 s tone; `overlap_min_ms=13 000` in the recipe → none), which proves the
thresholds reach the fuse from `_analyse`.

## Corpus A/B

**Harness.** `scripts/score_corpus.py` scores committed artefacts, so re-fusing the seven cached
Free runs needed an offline path: a scratch script copies each run's generation-0 inputs (the
observation, window and hint files its `fuse/episodes.gen0.done.json` sidecar names) out of
`work/` into `%TEMP%/idea-runs/rf-<tag>/<n>/m/`, calls `fuse_generation` there with a stub PCM
path, and writes a run list with explicit `identities` and `media_key`. Check: with the unchanged
code the re-fused `episodes` arrays are identical to the cached ones on all seven mixes, and the
score reproduces the baseline exactly (8971 / 6376).

| Run (7 mixes, draft truth, work-only pooling) | found | listed | work recall | work precision | likely |
|---|---|---|---|---|---|
| baseline (cached artefacts) | 609 | 199 | 139/218 · 63.8 % | 139/186 · 74.7 % | 61/68 · 89.7 % |
| re-fused, unchanged code (harness parity) | 609 | 199 | 139/218 | 139/186 | 61/68 |
| fusion change (families, crowd rows) | 608 | 198 | 138/218 · 63.3 % | 138/185 · 74.6 % | 61/68 |
| + a: featuring marker folded | 608 | 198 | 138/218 | 138/185 | 61/68 |
| + b: digit/letter split (`4raws` → `4 raws`) | 608 | 198 | 138/218 | 138/185 | 61/68 |
| + c: short word one trailing letter apart | 608 | 198 | 139/218 · 63.8 % | 139/185 · 75.1 % | 61/68 |
| **final (a + c, alphabetic-only)** | 608 | 198 | 139/218 · 63.8 % | 139/185 · 75.1 % | 61/68 |

What moved, read off the re-fused episodes rather than inferred:

- **Fusion change.** Audio episodes are identical on every mix. Crowd rows: the MPH run's
  paragraph row ("salute into Nova by MPH? For a moment I thought … - Can we talk about that crazy
  transition from Perfect", a YouTube comment the parser's "X by Y" rule had turned into an
  artist/title) is gone — the work-only matcher had been crediting it as "MPH - Nova" because the
  paragraph's word set contains {mph, nova}, which is the one recall point lost; Mall Grab's
  "Fishmans - Long season MG Edit (UR)" row now carries all four answers that name it (evidence
  1 → 4) and starts at the first of them (152 s, was 159 s). Nothing else changed.
- **a.** Three duplicate works merged in MPH — the description's tracklist lines "AC Slater & MPH
  ft. Eloise Keeble - Lights On", "MPH ft. Cecelia - Rush" and "w/ The Bug ft. Flowdan - Jah War"
  joined the audio works "… (feat. Eloise Keeble)", "… (feat. Cecelia)", "… (feat. Flowdan)"; no
  listed row, badge, flag or evidence set changed (those lines are not trusted tracklist sources, so
  they cannot vote). Score neutral. The scorer already folds the marker, so this is fusion catching
  up with what scorer-iii asked it to decide.
- **b.** Identical fusion output on every mix: the only corpus instance ("4raws - beau james" vs
  the truth's "Beau James - 4 Raws Edit") is a crowd row against a *truth* row, which is the
  scorer's matching, not fusion's, and the scorer's own tokeniser was not part of the experiment.
  **Rejected** — a change with no effect.
- **c.** One fusion merge, redo-of-best-set: the crowd answer "So u know - Overmono" now
  corroborates the audio "Overmono - So U Kno" (the `likely` episode gains `hint_supported`,
  evidence 16 → 17). The +1 recall / +1 precision on jan-24th is the shared helper matching
  "Special Request, ODF & Tim Reaper - Pull Up" to the truth's "… OD - Pull Up" in the scorer —
  both instances are the same track. The adopted form additionally requires both tokens to be
  alphabetic so two numbered titles ("Untitled 1" / "Untitled 12") can never merge.

**Decision.** Pooled likely precision is 61/68 in every row, so nothing is rejected on the
plan's criterion. **a** and **c** are adopted (each with a regression test); **b** is rejected as
a no-op. The corpus is Shazam-only, so the cross-family corroboration itself cannot move these
numbers; it is covered by the fixtures and the two end-to-end Deep runs on the fakes.

## Required command outputs

### 1. `uv run pytest -q -p no:cacheprovider`

```text
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
902 passed, 93 deselected, 1 warning in 176.91s (0:02:56)
```

### 2. `uv run ruff check .`

```text
All checks passed!
```

### 3. `uv run ruff format --check .`

```text
242 files already formatted
```

(`uv run ruff format .` had been run on every touched file first.)

### 4. `uv run python scripts/audit_fixtures.py`

```text
audited 430 files
fixture audit passed
```

### 5a. Phase gate — `uv run pytest tests/test_phase1b_fusion.py tests/test_golden_local_free.py tests/test_stage4d_profiles.py -q`

```text
33 passed, 1 warning in 7.25s
```

### 5b. Local mode still serves — `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

### 5c. Corpus A/B (verbatim headline lines)

Before — `uv run python scripts/score_corpus.py --run-list data/local/release-1-runs-free.json --out %TEMP%/idea-runs/1b-ii-before.json --print`:

```text
Of the 609 tracks the tool found, the presentation floor hid 118 as buried, 32 as contradicted, 29 as scatter, 231 as short, leaving 199 listed; it named 139 of the 218 distinct tracks actually played (work recall 63.8%); 139 of the 186 distinct tracks it listed were really played (work precision 74.7%); and 61 of the 68 it marked 'likely' or better were right (likely precision 89.7%).
```

After (final, re-fused offline with the adopted code) — `--run-list %TEMP%/idea-runs/rf-final/run-list.json --out %TEMP%/idea-runs/1b-ii-final.json --print`:

```text
Of the 608 tracks the tool found, the presentation floor hid 118 as buried, 32 as contradicted, 29 as scatter, 231 as short, leaving 198 listed; it named 139 of the 218 distinct tracks actually played (work recall 63.8%); 139 of the 185 distinct tracks it listed were really played (work precision 75.1%); and 61 of the 68 it marked 'likely' or better were right (likely precision 89.7%).
```

### 6. `git status --short`

```text
 M src/id_detector/cli.py
 M src/id_detector/fuse/episodes.py
 M src/id_detector/fuse/identity.py
 M src/id_detector/orchestrate.py
 M src/id_detector/present/exports.py
 M src/id_detector/present/page.py
 M tests/golden/local-free/tracklist.json
 M tests/test_engine_corroboration.py
?? tests/fixtures/deep/c3-scenario.json
?? tests/fixtures/deep/crowd-contradiction.json
?? tests/fixtures/deep/single-coincidence.json
?? tests/fixtures/deep/two-separated.json
?? tests/test_phase1b_fusion.py
```

(plus ` M docs/reviews/README.md` and `?? docs/reviews/build-1b-ii.md`, written after the snapshot.)

### 7. `git diff --stat`

```text
 src/id_detector/cli.py                 |  18 ++-
 src/id_detector/fuse/episodes.py       | 279 +++++++++++++++++++++++++++++----
 src/id_detector/fuse/identity.py       |  23 ++-
 src/id_detector/orchestrate.py         |  17 +-
 src/id_detector/present/exports.py     |  66 ++++++--
 src/id_detector/present/page.py        |  15 +-
 tests/golden/local-free/tracklist.json |   3 +
 tests/test_engine_corroboration.py     |  40 +++--
 8 files changed, 394 insertions(+), 67 deletions(-)
```

Untracked additions: `tests/test_phase1b_fusion.py` 866 lines, four fixtures 106 lines. Total
≈ 1,430 changed lines, under the plan's 1,500-line split guideline.

## Plan ambiguities resolved

1. **"Selected votes for the same normalised work."** Agreements are computed over the selected
   votes of every candidate in a work, not per candidate: an AudD match carrying an ISRC and a
   Shazam match carrying its key are two recording components of one work (nothing asserts
   `same_recording` between them, `semantics.merge_recording_identities` needs two independent
   sources for that), so per-candidate votes would never corroborate in a real Deep run. An
   episode carries the work's agreements that fall inside its own supports.
2. **"Two agreements ≥ `separation_min_ms` apart."** Start to start of the two overlap intervals,
   the farthest pair — the same reading 1b-i gave the reserve rule's "≥ 30 s apart".
3. **What one agreement grants.** The display ("confirmed twice"), and it stays in
   `scan_targeting.CONFIDENT_FLAGS` for the secondary / local-index targeting, as before; the
   bypass of suppression, of the 30 s floor and of the rescan-eligibility mirror of the floor
   needs the separated flag, a `likely`/`verified` badge or a supporting hint — the `likely` rule
   itself is untouched (E-S6).
4. **Thresholds for a recipe that defines none.** The Free recipe leaves `overlap_min_ms` /
   `separation_min_ms` unset, yet Shazam + a local Panako index can agree under it; such a fuse
   uses the fuser's defaults, which are the Deep values. `cli._fuse` passes the recipe's values
   when present.
5. **"Plausible `Artist – Title`."** A structural rule on the whole label — both fields present, no
   `?`, link, handle or newline, ≤ 12 words, ≤ 120 characters. Not per field and not a
   question-opener stoplist: on the corpus, answers are as often "Title - Artist" as the other way
   round, and real titles start with "Is", "What" and "Who" and run to nine words. The MPH
   paragraph and a question the parser reads as an answer are refused; "Be your best when your
   best is needed - clarkie" and "I like the way you talk - Dave era" (an uploader's answer) pass.
6. **"At one timestamp."** Intersecting position ranges — a comment answer's range is ±5 s, so
   two answers within 10 s of each other are one cluster; a transitive sweep in position order
   makes the clusters disjoint. Two answers 10 s apart naming different tracks stay two rows, as
   before.
7. **"Best-supported."** Distinct independent answers (provenance groups) naming the work, then
   a trusted voice (uploader / pinned), then the strongest parse, then the earliest position, then
   the work id.
8. **"The other as an alternative."** `EpisodeRecord.alternatives` (candidate ids) on the listed
   crowd row; the presentation renders them for `hint_only` rows only — for an audio row the
   candidate's `alternatives` are its sibling recordings, which the grouping already folds as
   "could also be", so rendering them there would list every version twice.
9. **The crowd row's badge.** Still `possible` (a contract Literal): U-F13's "no badge, a
   distinct treatment" is the honesty pass (3a-ii); this cycle makes the rows honest in content.
10. **E-C3 "trial source".** AudD's `simultaneous_source` landed in 0b-i and Panako has its own
    scanner trial keys, so no code moved; the gate pins that both engines' votes are selected on a
    shared window and none is `hypothesis_rejected`.
11. **E-M3.** Verified under `targeting:1`: a lone 12 s AudD phantom is a `listed_not_confident`
    candidate probed *ahead* of the blanks around it, never a hole the free pass is kept out of.
12. **E-M6.** `--profile max_accuracy` alone already selected the Free recipe (0a-ii); only the
    `--recipe` help text was stale. The web runner's paid tier (`max_accuracy` → deep) is a
    different, deliberate choice and is unchanged.
13. **The golden.** The fusion change keeps the committed golden green; the additive presentation
    key needed one regeneration (three lines). Verified in that order.
14. **Matcher edges.** Decided on the A/B above; the adopted trailing-letter rule is narrower than
    the experiment (alphabetic tokens only).

## Could not do

- **`pwsh`** is not installed; Windows PowerShell 5.1 ran the smoke script, as the plan's
  "PowerShell host" note allows.
- **A Deep-recipe corpus A/B** is impossible offline: the cached release-1 runs are Free (Shazam
  only), so no cross-family agreement exists in them; the corroboration flags are proved on the
  fixtures and the two end-to-end Deep runs on the fakes, not on real mixes.
- Nothing live was needed or run.

BUILD: COMPLETE

## Review + fix pass

Adversarial review of the uncommitted 1b-ii tree against `docs/PLAN-v2.md` §2.3.4 step 5, §5
"1b-ii", §6.1 and the four source reviews. Every gate re-run here, never trusted from the report;
every corpus number reproduced independently. No live provider call: every command ran with
`AUDD_API_TOKEN=` and `IDEA_ENGINE_SHAZAM=off`, `work/` was only ever READ (the re-fuse harness
copies each cached run's generation-0 inputs into `%TEMP%/idea-runs/` and fuses there), and no
analysis was started.

### What was verified before looking for faults

- **E-S6, the `likely` rule is byte-for-byte.** Programmatic compare of the block from
  `independent_trials_e4 = _independent_trials_e4(votes)` to `candidate = candidate_by_id[...]`
  (`fuse/episodes.py:663-706`) against `HEAD`: **identical**. No threshold moved — `4 x FULL_TRIAL_E4`,
  `span >= 40_000`, `not competing`, `has_global` and the `possible` fallback are unchanged, as is
  the hint vote. The only change reaching the rule is `_independent_trials_e4`'s *attribution*,
  which is E-L5 and explicitly in this cycle's scope (`v2-review-engines.md:325`).
- **Corroboration fires only across families, only on real overlap.** Direct probes of
  `engine_agreements` / `separated_agreements`:

  | case | result |
  |---|---|
  | audd + acrcloud, identical 12 s support | `[]` (same family) |
  | audd + acrcloud + audd, overlapping | `[]` |
  | shazam + audd, supports disjoint | `[]` |
  | shazam + audd, 1 ms overlap | `[]` |
  | shazam + audd, overlap exactly 6 000 ms | `[(6000, 12000)]` |
  | shazam + audd, overlap 5 999 ms | `[]` |
  | shazam + panako, same window | `[(0, 12000)]` |
  | separation exactly 60 000 / 59 999 ms (start to start) | `True` / `False` |
  | one agreement | `False` |
  | 30 random input permutations | one identical result |

- **A single agreement bypasses nothing.** `single-coincidence` through the real fuse: badge
  `unclear`, flags `['engine_corroborated']`, `suppressed=None`, row `hidden="short"` at the 30 s
  floor. `two-separated`: both flags, `hidden=None` on 24 s of on-air. Every bypass site
  (`fuse/episodes.py:921,944,1157`, `present/exports.py:141`) keys on the separated flag.
- **Crowd rows.** Through the real parser and fuse, with an `@handle` comment, a question, two
  answers naming X and one naming Y at one timestamp: **one** listed row (X), Y as its single
  alternative, X's two answers as evidence, the handle and the question dropped — and identical
  output under 25 random input permutations.
- **Corpus A/B, reproduced twice.** (a) The command as written,
  `score_corpus.py --run-list data/local/release-1-runs-free.json`, over the committed cached
  artefacts with this tree's helpers: 609 found, 199 listed, recall 140/218, precision 140/186,
  **likely 61/68 (89.7 %)**. (b) An independent offline re-fuse of all seven cached runs
  (generation 0; each run's `episodes.json` equals its `episodes.gen0.json`, checked by hash) and
  a fresh score: **608 found, 198 listed, recall 139/218 (63.8 %), precision 139/185 (75.1 %),
  likely 61/68 (89.7 %)** — the report's "final" row exactly. Pooled likely precision is 61/68 in
  every state, so nothing is rejected on the plan's criterion.
  Audio episodes re-fused identically on 5 of 7 mixes; the two that differ are the report's:
  redo-of-best-set's `likely` episode gains `hint_supported` (evidence 16 -> 17, badge unchanged)
  from the adopted trailing-letter rule, and three MPH episodes gain sibling `alternatives` from
  the featuring fold (no badge, suppression or evidence change). Crowd rows: MPH's paragraph row
  gone, Mall Grab's row 1 -> 4 answers. Nothing else moved.
- **Frozen artefacts.** `git status` on `profiles/`, `data/corpus/` and `docs/PLAN-v2.md`: clean.
  The golden diff is exactly the three `"engine_corroborated_separated": false` lines.
  `PAGE_VERSION` 18: no test pins the number literally (`test_stage7_page.py:472`,
  `test_stage7_server.py:194,205` all read the constant), so nothing needed updating.
- **a5718d5's crowd rendering still holds.** Rendering the `crowd-contradiction` fixture: the
  `hint.crowd` dashed "from comments" chip (no `HINT` chip), `tr.track.crowd` dashed edge,
  `.tl-lane[data-crowd="1"] .tl-extent{display:none}` dashed lane, `.lg-crowd` legend, Markdown
  `POSSIBLE FROM COMMENTS`. The new alternatives disclosure reads "1 other answer in the
  comments", never "other version".
- **E-M3** holds under `targeting:1`: a lone AudD phantom is `listed_not_confident`
  (`secondary_targeting.py:176-180`), probed *ahead* of the blanks. **E-M6**: the only remaining
  `max_accuracy`/deep text is the corrected help line and the web tier's deliberate comment.

### Findings

**P0 — none.**

**P1 · `fuse/episodes.py:98-108` (pre-fix) — the M1 invariant was enforced by a second,
hand-maintained table.** `TRUST_FAMILIES` restated `audd`/`acrcloud` -> `catalogue`, duplicating
`COMMERCIAL_PROVIDERS` (the existing single source of truth, also used by
`fuse/scanners.py:80`), and `trust_family` gives any provider outside the table its own family.
`provider` is a free-form `str` (`contracts.py:265`), so a commercial engine added to
`COMMERCIAL_PROVIDERS` — or registered under a variant id — without a `TRUST_FAMILIES` entry
corroborates AudD: exactly review M1's "one catalogue queried twice", now able to show "confirmed
twice" and, with two agreements `separation_min_ms` apart, to bypass suppression and the 30 s
floor. Probe: `trust_family("audd_v2") == "audd_v2"` and `engine_agreements(audd, audd_v2)` ->
`[(0, 12000)]`.
*Fixed:* the catalogue half of `TRUST_FAMILIES` is derived from `COMMERCIAL_PROVIDERS`, so the two
can never diverge. *Test:* `test_a_commercial_catalogue_engine_can_never_become_its_own_trust_family`.

**P1 · `present/exports.py:_candidate_label` + `fuse/episodes.py:plausible_crowd_label` — a crowd
row could still be *displayed* as comment text.** The U-F13 rule gates which *answers* may become
rows, but the row is rendered from the candidate's label, and a text-only candidate (every crowd
ID) falls back to lexicographic `min()` over the whole *work's* node labels — which include the
tracklist/comment lines a listener pasted. On the release-1 corpus this listed Mall Grab's crowd
row as `FULL TRACK LIST: - Fishmans - Long season MG Edit (UR)` while all four of its answers were
clean (`Fishmans - Long Season`): comment boilerplate rendered as an artist name, the precise
defect U-F13 names, and it survived the new plausibility rule.
*Fixed:* (a) `plausible_crowd_label` refuses a field ending in `:` — a comment lead-in
("FULL TRACK LIST:", "ID:", "Track:") is never a name, and it is the one shape the length and
punctuation rules let through; (b) `_candidate_label` prefers a plausible label **in its work
fallback only**, so audio candidates (which take the non-text branch) are untouched, and a work
with no plausible label at all still gets its name rather than "Unknown".
*Measured:* exactly **one** candidate label changed across all seven corpus mixes (the row above,
now `Fishmans - Long season MG Edit`); the re-fused episodes are byte-identical and the score is
unchanged (608 / 198 / 139 / 139 / 61-of-68). *Test:*
`test_a_comment_lead_in_is_not_a_track_label_and_never_names_a_crowd_row`.

**P2 (fixed) · `cli.py:949` — `recipe.overlap_min_ms or DEFAULT` swallowed a deliberate `0`.**
For these two thresholds `0` is meaningful ("no floor"), unlike the neighbouring
`audd_concurrency or 1`, so a recipe setting `0` silently got the Deep value instead.
*Fixed:* explicit `is None`. *Test:*
`test_a_recipe_threshold_of_zero_is_not_mistaken_for_an_unset_one` — an end-to-end Deep run on
`overlap-allowed` with `overlap_min_ms=0, separation_min_ms=0` produces two agreements per episode
and the separated flag; it fails against the `or` form (verified by reverting).

**P2 (comment only) · `fuse/episodes.py:1012` — `listed_spans.append(...)` is a no-op.**
`crowd_answer_clusters` is called once, before the loop, so appending cannot change cluster
membership; and it never needs to — the clusters are disjoint by construction (a cluster's hull
cannot even jump over a listed span, since any answer bridging two sub-ranges would itself
intersect that span and be filtered), and each cluster yields at most one row. Review M4's
duplicate is prevented by the clustering, not by this list. Left in place as a defensive guard
with a comment saying exactly that, so it does not read as the fix.

**P2 (note) · `scan_targeting.py:23` keeps `engine_corroborated` in `CONFIDENT_FLAGS`.** One
agreement therefore still tells the paid/secondary targeting "do not pay to rescan this". That is
not the suppression/floor bypass plan step 5 governs, and the new definition (cross-family, real
overlap) is strictly *narrower* than the committed one, so targeting became more conservative, not
less. Builder's ambiguity 3. No change.

**P2 (note) · a crowd row's alternative carries a `POSSIBLE` badge chip.**
`_crowd_alternative_summary` copies the row's badge and `_alternatives_html` renders
`alt-badge badge-possible`. U-F13's "never give a crowd row a confidence badge" is the honesty
pass (3a-ii, builder's ambiguity 9); the alternative simply inherits the badge the primary crowd
row already carries. One more badge for 3a-ii to strip, not a new claim.

**P2 (note) · `algorithm_version` stays `targeting:1,fusion:1`.** This cycle changes fused
semantics: what `engine_corroborated` means, what bypasses suppression and the floor, crowd-row
selection, trial attribution. The plan's §2.1 table schedules no bump at 1b-ii, so results stored
by the previous build stay compatible and servable (§3.4). Harmless today — the free corpus has no
cross-family agreements and no Deep result has shipped — but the owner should decide whether
1b-ii warrants `fusion:2` before hosted results are served to third parties.

**P2 (note, pre-existing, out of scope) · the hint parser still emits an `@handle` inside a
label.** Mall Grab carries `kind=answer artist='@genesis-lee-2 Jordon Alexander' title='Winter'`,
against scorer-iii's "no `@handle` ever in a label". The new plausibility rule correctly refuses it
as a crowd row and `_candidate_label` prefers that work's Shazam node, so it never reaches the page
or the exports — it does reach `score_corpus.py`'s per-mix "Wrong:" diagnostic. Parse-side
(`hints/parse.py`), which this cycle is told not to redo.

**Not a finding · "I like the way you talk — Dave era" (U-F13's screenshot row) is still listed,
and should be.** `data/corpus/release-1/redo-of-best-set/ground_truth.json` contains
`{"artist": "DAVE ERA", "title": "I Like The Way You Talk"}` — it is a correct ID, and the scorer
counts it as one. The builder's structural rule (shape of the whole label, not a word stoplist or
a field order) is the right reading of U-F13; a stoplist would have deleted a true positive.

### Files changed by this pass

- `src/id_detector/fuse/episodes.py` — catalogue family derived from `COMMERCIAL_PROVIDERS`;
  `plausible_crowd_label` refuses a trailing-colon field; the `listed_spans.append` comment.
- `src/id_detector/present/exports.py` — `_split_label` extracted; `_candidate_label` prefers a
  plausible label in its work fallback.
- `src/id_detector/cli.py` — `is None` instead of `or` for the two recipe thresholds.
- `tests/test_phase1b_fusion.py` — three regression tests (23 in the file, gate 36).

### Final gates (all re-run after the fixes)

```text
$ uv run pytest -q -p no:cacheprovider
905 passed, 93 deselected, 1 warning in 207.72s (0:03:27)

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
243 files already formatted

$ uv run python scripts/audit_fixtures.py
audited 431 files
fixture audit passed

$ uv run pytest tests/test_phase1b_fusion.py tests/test_golden_local_free.py tests/test_stage4d_profiles.py -q
36 passed, 1 warning in 7.02s

$ powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'

$ uv run python scripts/score_corpus.py --run-list data/local/release-1-runs-free.json --out %TEMP%/idea-runs/review-1b-ii.json --print
Of the 609 tracks the tool found, ... leaving 199 listed; it named 140 of the 218 distinct tracks
actually played (work recall 64.2%); 140 of the 186 distinct tracks it listed were really played
(work precision 75.3%); and 61 of the 68 it marked 'likely' or better were right (likely precision
89.7%).

$ uv run python scripts/score_corpus.py --run-list <independent offline re-fuse of the same 7 runs> --print
Of the 608 tracks the tool found, ... leaving 198 listed; it named 139 of the 218 distinct tracks
actually played (work recall 63.8%); 139 of the 185 distinct tracks it listed were really played
(work precision 75.1%); and 61 of the 68 it marked 'likely' or better were right (likely precision
89.7%).
```

VERDICT: OK_TO_COMMIT

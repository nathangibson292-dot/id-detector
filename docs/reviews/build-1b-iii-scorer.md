# Build 1b-iii (scorer half) — corpus scorer for launch gate L3

The scorer part of plan cycle 1b-iii only; the per-process breaker is deferred to its own cycle.
Everything is uncommitted on `main` at `1ca6132` for review. No dependency was added;
`docs/PLAN-v2.md`, `profiles/`, `data/corpus/`, fusion, targeting and the breaker are untouched.
No provider was contacted: the script is offline by construction (it reads a run's fuse artefacts
and a truth file), every test runs on the committed `tests/fixtures/corpus-mini/` fixture, and the
only CLI started was `idea serve` through the smoke script. `work/` was neither read nor written.

---

## What changed, file by file

- `scripts/score_corpus.py` — new. `--run-list <json>` (`{"recipe": "deep"|"free", "runs":
  [{"mix_id", "truth", "episodes", "min_track_ms": 30000}]}`, validated by a closed pydantic model;
  relative paths resolve against the run list's own directory; identities are the newest
  `identities.gen*.json` beside the episodes or an explicit `"identities"` path), `--out <json>`,
  `--print`. Per mix: flatten every episode to its own row (`collapse=False`) and drop the ones
  `present.exports.hidden_reason` hides (the on-air floor and fusion's `suppressed` reasons);
  build a `PredictionDocument` (profile = recipe, `unverified_seed_comparison` from
  `truth_is_frozen_verified`) and hand it to the existing `score_corpus_detailed` — the function
  behind `idea benchmark score` — writing that mix's `predictions.json` and full `report.json`
  under `<out stem>-mixes/<mix_id>/`. Across mixes the raw `ScoreState`s are summed (**pooled
  counts**) and the three e4 numbers are read from the pooled `BenchmarkMetrics` under the plan's
  field names: `likely_precision_e4` = `empirical_tier_precision_e4["likely"]`,
  `listed_precision_e4` = `selective_precision_e4` (post-floor), `work_recall_e4` =
  `identification_work.recall_e4`. The output also carries `truth_status`
  (`draft` | `unverified` | `verified`, the worst across mixes), the counts used (pooled and per
  mix, plus `hidden_by_reason`), the per-mix rows with their own three numbers, and an `l3` block
  (the §6.3 thresholds for the recipe, `met`, `certifiable` = verified truth). `--print` writes
  one plain-English paragraph for the owner. Exit 2 for an invalid run list, 1 for a scoring
  failure, 0 otherwise.
- `src/id_detector/benchmark/scorer.py` — `pooled_metrics(states, *, physical_attempts=0)`, a
  public wrapper over the private aggregate-then-metrics path `score_corpus` already uses for
  `overall`, so the script's pooled ratios use the scorer's own rounding and cannot drift from an
  in-corpus `overall`.
- `src/id_detector/benchmark/corpus.py` — the episode → identity-labelled prediction mapping is
  extracted from `_prediction_set` into public `prediction_set_from_fusion(set_id, identities,
  episodes)`; `_prediction_set` now calls it (same output, asserted by a test), and the script
  feeds it the episodes left after the floor instead of duplicating the mapping. `EpisodesFile`
  joins the module-level contract imports for the annotation.
- `tests/fixtures/corpus-mini/` — new: two hand-designed draft-truth mixes (`mini-a`: 7 episodes
  over 3 truth works; `mini-b`: 3 over 3) laid out like real runs (`<mix>/ground_truth.json`,
  `<mix>/fuse/episodes.json`, `<mix>/fuse/identities.gen0.json`), `run-list.json`, and
  `expected.json` — the script's exact output. The design (documented in the test module
  docstring) makes every pooled number differ from the mean of the per-mix ratios and exercises
  each floor rule: a 12 s `possible` row is hidden as `short`, a 12 s `likely` row stays, a
  `suppressed: buried` row is hidden whatever its badge, a 40 s `unclear` row is listed.
- `tests/test_score_corpus.py` — new (24 tests, below).
- `docs/reviews/README.md` — the 1b-iii (scorer) row.

### Tests added

`tests/test_score_corpus.py` (24):
- **Gate** — the fixture reproduces `expected.json` exactly and the per-mix `predictions.json` /
  `report.json` artefacts exist; the headline numbers are the pooled counts (4/5 → 8000, 4/7 →
  5714, 4/6 → 6667) and not a macro mean (7500 / 5500), and every per-mix count sums to the
  pooled one; each per-mix row's three numbers equal the real benchmark report's
  `overall.empirical_tier_precision_e4["likely"]`, `overall.selective_precision_e4` and
  `overall.identification_work.recall_e4`, the report holds one set with the truth's `set_id`,
  `profile` is the recipe, and only the listed episodes reached the scorer;
  `pooled_metrics` over the two states gives the pooled numbers and a single state pooled equals
  that set's own metrics.
- **Presentation floor** — `listed_episodes` on `mini-a` hides exactly `{buried: 1, short: 1}`,
  keeps the short `likely` row and the listed `possible`/`unclear` rows, leaves gaps and
  durations alone; with `min_track_ms: 0` only `buried` is hidden and listed precision moves to
  4/9 → 4444 while the likely tier and recall are untouched; an omitted `min_track_ms` defaults
  to 30 000 and reproduces the expected numbers.
- **Truth status** — the fixture is `draft` (per mix and overall, `l3.certifiable` false);
  settled-but-unfrozen rows read `unverified` with identical numbers; a frozen, hash-checked
  `corpus-version.json` reads `verified` (and the per-mix report says
  `unverified_seed_comparison: false`); one draft mix drags the run back to `draft`.
- **`--print`** — one line, equal to `summary()`, naming the recipe, the mixes, DRAFT truth, what
  the floor hid, the three numbers as percentages, the L3 thresholds, "not met" and the
  non-verified caveat; `--print` without `--out` scores into a scratch directory and leaves
  nothing beside the fixture; a `free` document uses the 70 % recall bar and can read "met".
- **Run list** — relative paths resolve against the run list's directory (not the CWD);
  identities are the numerically newest generation beside the episodes, an explicit path wins,
  none raises; seven invalid run lists (unknown recipe, no runs, duplicate `mix_id`, unknown key,
  path-unsafe `mix_id`, negative floor, missing recipe) exit 2 without writing anything; a
  missing episodes file and a truth directory holding two sets exit 1; neither `--out` nor
  `--print` is a usage error.
- **Extraction** — `prediction_set_from_fusion` is byte-for-byte the mapping `_prediction_set`
  produces from the same fuse directory, with the expected work/version labels.

---

## Required command outputs

### 1. `uv run pytest -q`

```text
780 passed, 93 deselected, 1 warning in 255.34s (0:04:15)   # the usual pydub audioop DeprecationWarning
```

(Run with `AUDD_API_TOKEN=` and `IDEA_ENGINE_SHAZAM=off` exported; 756 before this cycle + 24.)

### 2. `uv run ruff check .`

```text
All checks passed!
```

### 3. `uv run ruff format --check .`

```text
235 files already formatted
```

(`uv run ruff format` was run on the four new/edited Python files first.)

### 4. `uv run python scripts/audit_fixtures.py`

```text
audited 399 files
fixture audit passed
```

### 5a. Phase gate — `uv run pytest tests/test_score_corpus.py -q`

```text
........................                                                 [100%]
24 passed, 1 warning in 39.39s
```

### 5b. `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

### 5c. The script itself on the fixture (`--print`)

```text
$ uv run python scripts/score_corpus.py --run-list tests/fixtures/corpus-mini/run-list.json --out <scratch>/out.json --print
The deep recipe was scored over 2 mix(es) (mini-a, mini-b) against DRAFT truth (seeded tracklists
with placeholder timings, not yet verified), so these are working numbers, not release numbers.
Of the 10 tracks the tool found, the presentation floor hid 1 as buried, 2 as short, leaving 7
listed; 4 of those were tracks really played there (listed precision 57.1%); 4 of the 5 it marked
'likely' or better were right (likely precision 80.0%); and it named 4 of the 6 distinct tracks
actually played (work recall 66.7%). L3 asks for likely >= 90.0%, listed >= 80.0% and recall >=
75.0% for deep: the L3 bar is not met (likely precision 80.0% < 90.0%; listed precision 57.1% <
80.0%; work recall 66.7% < 75.0%), and a non-verified score cannot clear L3 either way. Full
numbers: <scratch>/out.json.
```

(Printed as one line; wrapped here.) A read-only check that the four seeded
`data/corpus/release-1/*/ground_truth.json` files load through `load_truth_directory` and resolve
to `truth_status = draft` also passed; no episodes exist for them in the repo, so no real mix was
scored.

### 6. `git status --short` and `git diff --stat`

Captured after the report and the README row were written (the report itself gains this
section afterwards; nothing else changed):

```text
$ git status --short
 M docs/reviews/README.md
 M src/id_detector/benchmark/corpus.py
 M src/id_detector/benchmark/scorer.py
?? docs/reviews/build-1b-iii-scorer.md
?? scripts/score_corpus.py
?? tests/fixtures/corpus-mini/
?? tests/test_score_corpus.py

$ git diff --stat
 docs/reviews/README.md              |  1 +
 src/id_detector/benchmark/corpus.py | 13 +++++++++++++
 src/id_detector/benchmark/scorer.py | 12 ++++++++++++
 3 files changed, 26 insertions(+)
```

Untracked additions: `scripts/score_corpus.py` (462 lines), `tests/test_score_corpus.py` (467),
`tests/fixtures/corpus-mini/` (8 files, 1 364 lines of JSON), this report. Git first printed a
"CRLF will be replaced by LF" notice for the two edited source files: the editing tool had
rewritten them with CRLF (untouched tracked files are LF). Both were converted back to LF
byte-for-byte before this capture; the diff is the same 13 + 12 insertions and the notice is gone.

---

## Plan ambiguities resolved (and how)

1. **Test file and gate.** The plan's 1b-iii gate names `tests/test_phase1b_breaker_scorer.py`
   (breaker + scorer together); the cycle note names `tests/test_score_corpus.py` and its gate
   command for this scorer-only cycle. Followed the cycle note. The breaker cycle can add its own
   file or fold this one in.
2. **"Calls `idea benchmark score` per mix".** The cycle note prefers the underlying function, so
   the script calls `score_corpus_detailed` (the function the CLI command wraps) per mix, and
   still writes what the command would have — `predictions.json` and the full `report.json` under
   `<out stem>-mixes/<mix_id>/` — so a reviewer can open every mix's complete benchmark report.
3. **The floor is applied per episode.** The scorer scores episodes one by one, so each episode
   is flattened to its own row (`collapse=False`) and judged by `hidden_reason` on its own on-air
   time. Under the page's default collapse a near-duplicate group's on-air time is pooled across
   its members, so a short alternative folded into a long group could appear on the page yet be
   dropped here (never the reverse). Simplest reading that satisfies the gate; noted for review.
4. **Run-list paths** resolve against the run list's own directory (absolute paths stand), so
   `data/corpus/release-1/runs-deep.json` can name `boomtown-mix/ground_truth.json` directly.
5. **Identities** are the numerically newest `identities.gen*.json` beside the episodes (the same
   rule as `enrich/run.py`'s `final_identities_path`), with an optional explicit `"identities"`.
6. **`min_track_ms`** defaults to 30 000 when omitted; must be ≥ 0.
7. **`truth_status`** has three values, not two: `draft` (any row `draft: true`), `unverified`
   (settled rows, no frozen manifest) and `verified` (frozen, hash-checked, every row verified);
   the cycle note only named `draft`, and a settled-but-unfrozen file is honestly neither.
8. **`l3` block.** The plan's L3 row states the thresholds against exactly these three outputs, so
   the output carries `l3.thresholds` (per recipe), `l3.met` and `l3.certifiable`, and the
   `--print` paragraph says whether the bar is met and that a non-verified score cannot clear it.
   Small, owner-facing addition; flagged in case the reviewer prefers it out.
9. **Two small public helpers in library code** (`scorer.pooled_metrics`,
   `corpus.prediction_set_from_fusion`) rather than importing private names or duplicating the
   episode → prediction mapping in the script. Neither changes any existing behaviour (the
   extraction is asserted equal to the legacy path).
10. **`profile`** of each per-mix `PredictionDocument` is the recipe name; `mix_id` is restricted
    to `[A-Za-z0-9][A-Za-z0-9._-]*` because it names the per-mix artefact directory.

## Not done (with reasons)

- **`data/corpus/release-1/runs-<recipe>.json`** was not created: `data/corpus/` is off-limits this
  cycle and there are no committed runs to point at. When the background analyses finish, a run
  list is e.g. `{"recipe": "free", "runs": [{"mix_id": "release1-boomtown-mix", "truth":
  "boomtown-mix/ground_truth.json", "episodes": "../../../work/<media_key>/fuse/episodes.json",
  "min_track_ms": 30000}, …]}` saved beside the truth folders, then
  `uv run python scripts/score_corpus.py --run-list data/corpus/release-1/runs-free.json --out
  docs/accuracy/release-1-free.json --print`. Until the drafts are verified and frozen the output
  will say `truth_status: draft`.
- **The breaker half of 1b-iii** — deferred by the cycle note.
- **Gate wall time** is ~40 s: each end-to-end run performs the scorer's bootstrap (2 000
  replicates for the certification and stratum intervals) per mix. Acceptable; not optimised.

---

## Review + fix pass

Adversarial review of the uncommitted scorer half of 1b-iii against `docs/PLAN-v2.md`
(`### 1b-iii`, §6.3 L3, §2.3/§3.4–3.5, §6.1 D1–D8) and the cycle note, then every P0/P1 fixed in
place. No live provider was contacted: every command ran with `AUDD_API_TOKEN=` and
`IDEA_ENGINE_SHAZAM=off` exported, the script is offline by construction, and `work/` was only ever
READ (the preliminary run below), never written.

### Findings

**P0-1 — the scorer crashed on every real run list (`StopIteration`).**
`scripts/score_corpus.py:146` took the *numerically newest* `identities.gen*.json` beside the
episodes. A media directory can hold the artefacts of more than one analysis: the boomtown run dir
holds `identities.gen0/1/2.json`, while its final `episodes.json` is generation 0 — its own
completion sidecar says so (`episodes.done.json` → `upstream: {"fuse/episodes.gen0.json": …}`, and
`episodes.gen0.done.json` → `upstream: {"fuse/identities.gen0.json": …}`; gen1/gen2 come from a
different `recognise/invocations/…` id). A later generation re-clusters the identity graph, so 3 of
the gen0 episodes' `candidate_id`s are absent from `identities.gen2.json` and
`present/exports.py:64 _candidate_label` raised a bare `StopIteration` through
`flatten_tracklist`. (The reported `identities.gen0.done.json` sidecar is *not* the cause —
`_IDENTITIES_GEN` already excludes `.done.json`, verified: `match("identities.gen0.done.json")` is
`None`, and the picker returned the real 294 KB `identities.gen0.json` for the Mall Grab dir.)
**Fix:** `identities_path(entry, episodes)` now follows the completion-sidecar chain
(`episodes.done.json` → the `fuse/episodes.gen<N>.json` it was copied from → the
`fuse/identities.gen<N>.json` that generation was fused from), then pairs by the generation the
episodes themselves declare, and only then falls back to the newest; plus
`assert_identities_cover()` fails loudly naming the file and a missing candidate id instead of
`StopIteration`.

**P0-2 — every real Free run then failed the prediction contract (found re-running the repro).**
Crowd rows (fusion's `hint_only` episodes, `fuse/episodes.py:816`) carry the comment's *position
range* as their support with `start_no_later_than_ms == range start` and
`end_no_earlier_than_ms == range end`. `ScoredEpisode._ordered` (`benchmark/scorer.py:143`) demands
engine-proved bounds (`start < snlt <= end`, `start <= enet < end`), so all five release-1 mixes
died with `2 validation errors for PredictionDocument` (9 rows in total: 2 + 2 + 1 + 4 across
redo-of-best-set, new-mix-jan-24th, final-new-set-christmas, mall-grab). **Fix:** `proved_bounds()`
re-expresses a range claim in the contract's convention — "played somewhere in `[lo, hi]`" is
exactly "started no later than `hi`, ended no earlier than `lo`" (the contract explicitly allows
one-sided proofs to cross), `best_start_ms`/`best_end_ms` follow as it requires. Nothing else
changes and none of the three L3 numbers reads those fields. The count is surfaced as
`range_claims` per mix, in `counts`, and in each mix's `predictions.json` `run_config`, so a crowd
ID with no audio proof is never invisible. *(Follow-up for the plan owner, not this cycle: either
fusion should write crowd bounds in the proved convention or `ScoredEpisode` should admit a range
claim natively; until then the scorer normalises and counts them.)*

**P1-1 — a run list could silently score the wrong mix.** Nothing tied a run's episodes to the
truth's media. Reproduced: pointing `mini-b`'s row at `mini-a`'s episodes returned exit 0 and a
plausible paragraph (`listed precision 30.0%`, `work recall 50.0%`) for a mix whose truth is 300 s
against episodes spanning 600 s. **Fix:** `assert_media_matches()` — the run's media key (from
`<media_dir>/ingest/source.json`, else the media directory's own name under `work/<source_key>/`,
else an explicit `"media_key"` in the entry) must equal `truth.source.media_key`; a run whose media
cannot be identified at all is refused. All five real release-1 rows pass (their `work/…/<media_key>`
segments match their truth). The fixture's run list now states each mix's `media_key`.

**P1-2 — a duplicated mix silently double-weighted itself.** `mix_id` uniqueness was enforced but
truth-set uniqueness was not, so a copy-pasted row pooled one mix twice and moved every number the
*permissive* way (fixture: recall 6667 → 7778, likely 8000 → 8750, exit 0). **Fix:**
`score_run_list` refuses a truth `set_id` already scored under another `mix_id`.

**P1-3 — `l3.met` claimed the release gate on the three thresholds alone.** §6.3 L3 also requires
≥ 5 owner-verified mixes, ≥ 3 DJs, ≥ 2 platforms, ≥ 4 h and two-pass truth; a 1-mix verified run
would have published `"met": true`. The corpus facts cannot be derived honestly from the truth
files as seeded (`uploader_ref` is a per-mix placeholder, so counting distinct uploaders would
*over*-count DJs), so the field is renamed **`thresholds_met`** and `--print` names what the score
does not check (`L3_CORPUS_NOTE`).

**P2 (fixed)** — the `--print` paragraph said "N of those" against the *listed row* count while the
precision denominator is the *evaluable* predictions (they diverge when a truth has unknown
regions); now "N of the M scored". — `KeyError`/`StopIteration` from an inconsistent identity graph
escaped as a traceback; now `scoring failed: …` with exit 1. — the redundant function-local
`from id_detector.contracts import EpisodesFile` left in `benchmark/corpus.py:_load_fusion` after
the extraction is removed (the module-level import the builder added covers it).

**P2 (noted, not fixed — outside this cycle)** — `enrich/run.py:73 final_identities_path` has the
same "newest generation" rule, so `idea present --refresh` / `acquire` / `show` on the boomtown run
dir would crash the same way P0-1 did; it wants the sidecar chain too (fusion/present cycle, not
the scorer). — the scorer scores one row per episode (`collapse=False`), matching the committed
benchmark convention (`benchmark/corpus.py:253` "the benchmark scores one row per episode"), so a
short row folded into a long collapsed group on the page is dropped here, and a suppressed group
*primary* hides its alternatives on the page but not here; bounded and pessimistic in the common
direction. — the plan's 1b-iii gate names `tests/test_phase1b_breaker_scorer.py`; the cycle note's
`tests/test_score_corpus.py` was followed, so the breaker cycle must reconcile the two. —
`expected.json` embeds `out-mixes/<mix>/report.json`, which couples the fixture to an `--out` named
`out.json`. — a shrinking run list leaves stale per-mix artefact directories under
`<out stem>-mixes/`. — `scripts/smoke_serve.ps1` printed its pass line and then exited 1 on the
first of two runs because `taskkill /T` lost a race with an already-exiting child (pre-existing
teardown flake, unrelated to this cycle; second run exit 0).

### What changed

- `scripts/score_corpus.py` — `identities_path(entry, episodes)` follows the completion-sidecar
  chain (`_sidecar_upstream`); `assert_identities_cover`, `run_media_key`, `assert_media_matches`,
  `proved_bounds` added; `RunEntry.media_key`; duplicate-truth-set guard in `score_run_list`;
  `l3.met` → `l3.thresholds_met` + `L3_CORPUS_NOTE` in the paragraph; `range_claims` in the output
  and in `run_config`; `KeyError`/`StopIteration` handled.
- `src/id_detector/benchmark/corpus.py` — dropped the now-redundant function-local `EpisodesFile`
  import. The `_prediction_set` extraction itself is byte-for-byte the committed body (checked
  against `git show HEAD:` and asserted by the existing extraction test), so the frozen profiles'
  `idea benchmark score` path is untouched. `benchmark/scorer.py` is a pure addition
  (`pooled_metrics`), unchanged by this pass.
- `tests/fixtures/corpus-mini/` — `mini-b/fuse/` now carries a real run's completion-sidecar chain
  (`episodes.done.json` → `episodes.gen0.json` → `identities.gen0.json`, each with its
  `.done.json`) plus a stale, NEWER `identities.gen2.json` whose candidate ids are entirely
  different: the P0-1 regression trap. `run-list.json` states each mix's `media_key`;
  `expected.json` regenerated (`l3.thresholds_met`, `range_claims`) — the three headline numbers and
  every count are unchanged (`8000 / 5714 / 6667`, pooled 4/5, 4/7, 4/6).
- `tests/test_score_corpus.py` — 9 tests added, 1 rewritten (33 total): the sidecar chain beats a
  newer generation on the fixture; a stale graph exits 1 naming `identities.gen2.json` and
  "does not describe 3 candidate(s)"; the generation/newest/explicit fallbacks and the
  `FileNotFoundError`; a `<media_key>/fuse/` layout states its own key and scores identically;
  another mix's episodes exit 1 with "points at another mix's run"; an unidentifiable run directory
  exits 1; `ingest/source.json` outranks the directory name; one mix twice exits 1 with "already
  scored as mini-a"; `proved_bounds` swaps a range claim and leaves an engine row untouched; and a
  crowd row appended to `mini-b` is listed, scored and counted as a `range_claim` (exit 0) instead
  of failing the contract.
- `docs/reviews/README.md` — the 1b-iii row records this pass.

### Gates (all re-run after the fixes)

```text
$ uv run pytest -q
789 passed, 93 deselected, 1 warning in 324.04s (0:05:24)   # 780 before this pass + 9

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
236 files already formatted

$ uv run python scripts/audit_fixtures.py
audited 407 files
fixture audit passed

$ uv run pytest tests/test_score_corpus.py -q      # the cycle gate
33 passed, 1 warning in 34.21s

$ powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

### Preliminary real-run smoke (the reported repro, offline, cached runs only)

```text
$ uv run python scripts/score_corpus.py --run-list data/local/release-1-runs-free.json \
    --out <scratch>/idea-runs/release-1-free-preliminary.json --print
The free recipe was scored over 5 mix(es) (release1-boomtown-mix, release1-redo-of-best-set,
release1-new-mix-jan-24th, release1-final-new-set-christmas,
release1-mall-grab-boiler-room-melbourne-22) against DRAFT truth (seeded tracklists with
placeholder timings, not yet verified), so these are working numbers, not release numbers. Of the
413 tracks the tool found, the presentation floor hid 107 as buried, 32 as contradicted, 20 as
scatter, 117 as short, leaving 137 listed; 15 of the 137 scored were tracks really played there
(listed precision 10.9%); 7 of the 54 it marked 'likely' or better were right (likely precision
13.0%); and it named 15 of the 136 distinct tracks actually played (work recall 11.0%). L3 asks for
likely >= 90.0%, listed >= 80.0% and recall >= 70.0% for free: the L3 bar is not met (likely
precision 13.0% < 90.0%; listed precision 10.9% < 80.0%; work recall 11.0% < 70.0%), and a
non-verified score cannot clear L3 either way. Full numbers: <scratch>/idea-runs/release-1-free-preliminary.json.
```

(Printed as one line; wrapped here. Exit 0; `range_claims: 9`; nothing was written under `work/`.)
**These are not accuracy numbers.** The release-1 truth is still `draft` with placeholder
equal-slice timings, and association is temporal, so almost every correct name misses its truth
occurrence. The figure to read here is that the tool now runs the whole corpus end to end; the
numbers only become meaningful after the two-pass verification L3 requires.

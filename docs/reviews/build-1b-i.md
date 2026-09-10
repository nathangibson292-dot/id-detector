# Build 1b-i — Secondary targeting v2 (`targeting:1`)

One plan cycle, uncommitted on `main` at `b52776b` for review. No dependency was added;
`docs/PLAN-v2.md`, `profiles/` and `data/corpus/` are untouched. No live provider call was made:
every pipeline run used `IDEA_TEST_MODE=1 --fake-providers audd,shazam` with
`AUDD_API_TOKEN=` / `IDEA_ENGINE_SHAZAM=off` exported, or injected fakes through `_analyse`.
Fusion, corroboration and crowd rows (1b-ii) are not touched.

## File-by-file changes

- `src/id_detector/secondary_targeting.py` — the `targeting:1` scheduler (plan §2.3.4 step 4).
  Kept from `targeting:0`: `PRIORITY`, `SecondaryCandidate`, `secondary_capacity`, `blank_spans`
  (complement of the listed episodes' evidence hulls — the fuse's `gaps` miss the tail after a
  stopped primary), `select_secondary_candidates`, `frozen_windows`. New: `secondary_reserve`
  (`R = ⌊fraction × C⌋`), `allocation_order` (class → longer span → earlier start),
  `rank_windows` (step iii: intersection desc, RMS energy desc, start asc, id), `SecondaryPick`
  (window + candidate + round), `allocate_secondary_windows` (step i then step ii with exact
  integer largest-remainder, ties by earlier start; a duplicate pick takes the span's next-ranked
  window; a span with no eligible window left returns its quota), `distribute_secondary_windows`
  (step ii alone — the unused reserve), `confirmation_windows` / `serve_confirmations` (the reserve
  rule, first-come until `R` is gone), `window_rms_energy` / `energy_reader` (RMS of the window
  clip, read once per run), `listed_text_keys` and `new_identity_discoveries` (which blank probes
  named a track no listed episode carries). `schedule_secondary_windows` (the provisional queue) is
  retired; the private `_merge` import noted by the 0a-iv review (P2-7) stays because the module
  still needs it for `blank_spans`.
- `src/id_detector/recipes.py` — Deep `algorithm_version` → `targeting:1,fusion:1` (`recipe_id`
  changes; the Free recipe is untouched).
- `src/id_detector/cli.py` — the Deep secondary: capacity `C`, reserve `R`, allocation `A = C − R`
  through `allocate_secondary_windows` with the run's window energies; pass 1 over the allocation;
  discoveries from pass 1 (blank probes naming an unlisted identity) served confirmations from `R`
  first-come; unused `R` shared out by step (ii); pass 2 over confirmations + reserve remainder in
  its own Shazam invocation; `secondary_allocated` = every window queued, `secondary_resolved` =
  allocated − failures across both passes (the 80 % rule); new counts `secondary_reserve`,
  `secondary_discoveries`, `secondary_confirmed`, `secondary_uncorroborated`,
  `secondary_confirmation_clips`; one re-fuse with both passes' observations. The
  `recognise_windows` closure takes an optional `run_label` that derives a distinct invocation
  directory for the second pass (recognition artefacts are immutable per invocation and
  generation, so a second pass over new windows must not rewrite the first pass's files). The
  phase-2 comment describes the new scheduler. Imports: `Sequence`.
- `tests/fakes/providers.py` — an optional per-provider `"labels": {"<index>": "D"}` table so a
  script can make one engine find a track the other never reported (letters A–G map to fixture
  tones); the default A/B/C-by-index labels are unchanged, so no existing script changes meaning.
- `tests/test_phase0a_status.py` — the gate assertion pins `targeting:1,fusion:1`; the two
  `targeting:0` queue-order tests are rewritten for `targeting:1` (candidate classification and
  the ≥ 4 s eligibility assertions unchanged; the queue expectation is now one best window per
  candidate in class order, no duplicates, rescan/transformed windows never picked); the module
  docstring notes `R = 0` on the 60 s fixture.
- `tests/test_phase0a_money.py` — three `algorithm_version` pins → `targeting:1,fusion:1`.
- `tests/test_phase0a_crash_cache.py` — a comment no longer names `targeting:0`.
- `tests/fixtures/deep/` — **new**, the ten plan-named fixtures (below).
- `tests/test_phase1b_targeting.py` — **new**, the phase gate (21 tests).

## Fixtures — `tests/fixtures/deep/`

Two shapes. *Scheduler* fixtures carry synthetic windows (`hop_ms`/`window_ms` over
`duration_ms`), per-window energies, the first fuse's candidate spans and the expected picks;
*provider* fixtures are fake-provider scripts plus `audio_s`, the tone length the test generates
with the committed `scripts/make_audio_fixtures.py` generator (60 s = the committed fixture; 360 s
generated once per session, ≈ 10 s).

| Fixture | Shape | What it pins |
|---|---|---|
| `hint-only-first` | scheduler | the short, late `hint_only` span takes the first pick ahead of a long, early blank; energy breaks the blank's intersection tie |
| `overflow` | scheduler | 30 candidates, C = 12, R = 3 → exactly 9 first-round picks, nothing for (ii) |
| `largest-remainder-ties` | scheduler | A = 5 over 60/30/30 s spans: floors 1/0/0 in (ii), the leftover to the tied remainders by earlier start |
| `replacement-after-duplicate` | scheduler | the loudest window ranks first for two overlapping spans; the second span takes its next-ranked window |
| `duration-scaling` | scheduler | 120 min → C = 240, R = 60, A = 180 picks, none repeated |
| `overlap-allowed` | provider, 60 s | both densities: Shazam requests > 0 and ≥ 1 window coincides with an AudD window |
| `secondary-80pct` | provider, 60 s | 1 of 2 secondary windows resolved → `degraded` / `secondary_not_achieved` |
| `stopped-primary-tail` | provider, 60 s | quota error after two matches: the 21–60 s tail is blank (not a gap) and gets the second clip; the track found there is a discovery listed uncorroborated (R = 0) |
| `reserve-confirmation` | provider, 360 s | C = 12, R = 3: one discovery (D) confirmed with two clips ≥ 30 s apart inside the blank, the third reserve clip handed back by (ii), 12/12 resolved |
| `reserve-exhausted` | provider, 360 s | three blanks hide D, F, G: two clips to D, one to F, none to G — the third discovery is listed uncorroborated |

## Tests added — `tests/test_phase1b_targeting.py` (21)

The recipe identity: Deep is `targeting:1,fusion:1` at both densities, the `recipe_id` differs
from the `targeting:0` recipe's, and a stored `targeting:0` result fails §3.4's equal-version test.
Capacity/reserve/allocation arithmetic (`duration-scaling`, small `R` values). The four scheduler
fixtures, exact picks and rounds. `overflow`'s nine first-round picks. A span with no eligible
window returns its quota; a span with fewer windows than its share keeps what it can and the rest
is re-shared; nothing eligible anywhere → no pick. Ranking: the 4 s minimum exactly, then energy,
then start. Confirmations: the blank's farthest eligible pair ≥ 30 s apart, already-picked windows
skipped, one clip when only one is left or the pair is too close, none when nothing qualifies.
`serve_confirmations` first-come with R = 3/4/7/0 and never a repeated window. The unused reserve
by (ii) without repeats. Discoveries: only blank probes, only unlisted text keys (a suppressed
episode's identity is not "known"), the same identity once, start order. `window_rms_energy` on
generated clips (loud > quiet, non-16-bit → 0) and the reader's caching. End to end:
`overlap-allowed` at density 1 and 2, `secondary-80pct` → `degraded` with `tracklist.json`
`degraded`, `stopped-primary-tail`, the two reserve runs (counts, pass split 9 + 3, the expected
confirmations recomputed in the test from the plan's rule rather than pinned, a separate Shazam
invocation for the second pass, the discovered tracks listed by the re-fuse), plus the
scheduler-level tail case.

## Required command outputs

### 1. `uv run pytest -q`

```text
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
879 passed, 93 deselected, 1 warning in 198.24s (0:03:18)
```

(Run in the background while the PowerShell gate and the smoke script ran in the foreground; no
test was re-run.)

### 2. `uv run ruff check .`

```text
All checks passed!
```

### 3. `uv run ruff format --check .`

```text
240 files already formatted
```

(`uv run ruff format .` had been run on every touched file first.)

### 4. `uv run python scripts/audit_fixtures.py`

```text
audited 425 files
fixture audit passed
```

### 5a. Phase gate — `uv run pytest tests/test_phase1b_targeting.py -q`

```text
21 passed, 1 warning in 25.36s
```

### 5b. 0a-iv PowerShell gate, verbatim from the plan with the new `algorithm_version`
expectation (`pwsh` is not installed; Windows PowerShell 5.1 via
`powershell -NoProfile -ExecutionPolicy Bypass -File`, `AUDD_API_TOKEN=""` and
`IDEA_ENGINE_SHAZAM=off` exported for the run)

```powershell
$env:IDEA_TEST_MODE = "1"; $env:IDEA_FAKE_SCRIPT = "tests/fakes/scripts/gate0a-deep.json"
$w = Join-Path $env:TEMP ("idea-gate0a-" + [guid]::NewGuid().ToString("N"))
uv run idea analyse tests/fixtures/audio/tone-60s.wav --recipe deep --fake-providers audd,shazam --work-root $w
uv run python scripts/assert_journal.py --work-root $w --expect status=complete algorithm_version=targeting:1,fusion:1 usd_e6_reserved=36750 usd_e6_spent=35000 usd_e2_spent=4
```

```text
complete; 6 matches; 0 failures; 2 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=C:\Users\natha\AppData\Local\Temp\idea-gate0a-4a5039279f9d41289368e400139f7f44\a63408abeea9adf1c0d4452a6509d9ee4e5399d192f32ee7307bbcef39c83e27\e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d\present\tracklist.json
journal assertion passed at C:\Users\natha\AppData\Local\Temp\idea-gate0a-4a5039279f9d41289368e400139f7f44\a63408abeea9adf1c0d4452a6509d9ee4e5399d192f32ee7307bbcef39c83e27\e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d\invocations.jsonl:1
gate exit=0
```

### 5c. `uv run pytest tests/test_phase0a_status.py -q`

```text
24 passed, 1 warning in 29.74s
```

### 5d. `uv run pytest tests/test_phase0a_money.py tests/test_phase0a_crash_cache.py -q`

```text
37 passed, 1 warning in 46.05s
```

### 5e. Local mode still serves — `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

### 6. `git status --short`

```text
 M src/id_detector/cli.py
 M src/id_detector/recipes.py
 M src/id_detector/secondary_targeting.py
 M tests/fakes/providers.py
 M tests/test_phase0a_crash_cache.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_status.py
?? tests/fixtures/deep/
?? tests/test_phase1b_targeting.py
```

(plus `?? docs/reviews/build-1b-i.md`, this report, written after the snapshot.)

### 7. `git diff --stat`

```text
 src/id_detector/cli.py                 | 156 +++++++++---
 src/id_detector/recipes.py             |   2 +-
 src/id_detector/secondary_targeting.py | 430 ++++++++++++++++++++++++++++++---
 tests/fakes/providers.py               |  29 ++-
 tests/test_phase0a_crash_cache.py      |   4 +-
 tests/test_phase0a_money.py            |   6 +-
 tests/test_phase0a_status.py           |  53 ++--
 7 files changed, 572 insertions(+), 108 deletions(-)
```

Untracked additions: `tests/test_phase1b_targeting.py` 768 lines, ten fixtures 669 lines (281 of
them the 30-candidate `overflow` table). Total ≈ 1,450 changed lines, under the plan's 1,500-line
split guideline.

## Plan ambiguities resolved

1. **"Eligible windows" in the reserve rule.** Read with the definition step 4 gives two sentences
   earlier: windows overlapping *the span* — the blank the identity was found in — by
   ≥ `eligibility_min_intersection_ms`, *and* starting in `[m₀ − 45 s, m₁ + 45 s]`. The first cut
   used the start range alone and a confirmation for a 54 s discovery landed at 9 s inside the
   listed neighbour, which cannot confirm anything; the restriction keeps confirmations in the
   blank (a clip may overhang its edge by a few seconds).
2. **"One if only one exists" and a reserve of one.** Each discovery is served
   `min(2, R_remaining)` clips: the farthest pair when two are allowed and ≥ 30 s apart, otherwise
   the earliest eligible window (also when the pair is closer than 30 s). With R = 3 the second
   discovery gets one clip, the third none.
3. **What is a discovery.** A pass-1 `match` on a window picked for a `blank` candidate whose
   `work_text_key` (`artist|title`, casefolded — the identity graph's text node) is not among the
   text nodes of the identities the first fuse's *listed* (non-suppressed) episodes name. A
   suppressed episode is not coverage, so its identity is not "known" either. The same key found by
   a second blank probe is not a second discovery. "First-come" is the probes' start order (the
   resolution order under concurrency is not stable).
4. **Two Shazam passes, not more.** Discoveries are taken from the allocation pass only; a new
   identity turned up by a confirmation or a reserve-remainder clip is listed uncorroborated. The
   confirmations and the unused reserve (step ii over every candidate, already-picked windows
   excluded) go out together in the second pass.
5. **"Allocated" for the 80 % rule** counts every window queued in the run: the `A` picks, the
   confirmations and the redistributed reserve.
6. **Step (ii) mechanics.** Every candidate takes part (including the ones step i served); shares
   are exact integers (`quota × span / total`), floors first, the leftover to the largest integer
   remainders with ties by earlier start; a span whose share exceeds its eligible windows keeps what
   it can and the surplus is shared out again among the spans that still have windows, until the
   quota is spent or nothing can absorb it. Step (iii)'s final tie-break after start is the window
   id.
7. **RMS energy.** No contract carries it; it is the RMS of the frozen window's 16-bit WAV clip as
   a fraction of full scale, read once per window per run (a non-16-bit clip ranks as 0, so ties
   fall through to start order).
8. **The second pass's artefacts.** `recognise_generation` names its files by generation and its
   directory by `run_id`, and the files are immutable, so pass 2 runs under the derived run id
   `<run_id>:secondary-2` (its own `recognise/invocations/<key>/`); the journal's `invocation_id`
   is unchanged and both observation paths feed the re-fuse's sidecars. `idea sources` lists one
   more invocation directory per Deep run with a second pass.
9. **"Old results not served (test)".** `serves()` is 1a-ii and does not exist yet; the test pins
   the bump at both densities, the `recipe_id` change, and applies §3.4's equal-`algorithm_version`
   rule to a stored `targeting:0` result (incompatible while its `adapter_versions` still match).
10. **Fixture shapes.** Some plan-named fixtures are inherently end to end (`overlap-allowed` at
    both densities, `secondary-80pct` → `degraded`), the others describe an allocation; both live
    under `tests/fixtures/deep/` and the tests are explicit about which is which. The reserve runs
    need `C ≥ 12` for `R = 3`, so they use a 360 s tone generated per session by the committed
    generator; and they need one engine to find a track the other never reported, which the fakes'
    position-derived labels cannot express — hence the small `labels` table.
11. **The 0a-iv scheduler tests.** Their queue-order expectations described `targeting:0`
    (priority then start order, all eligible windows) and cannot hold under `targeting:1`; they
    were rewritten around the same fixtures with the classification/eligibility assertions intact.
    The 0a-iv gate's `algorithm_version` expectation was updated in the test that pins it and in
    the gate re-run above; `docs/PLAN-v2.md` is untouched.
12. **`tracklist.json` and "listed uncorroborated".** The presentation layer already drops
    Shazam-only episodes under its on-air floor, so the reserve tests assert "listed" on the
    re-fused `fuse/episodes.json` (a non-suppressed episode inside each blank). How an
    uncorroborated discovery is shown is 1b-ii/3a.

## Notes for the reviewer

- Counts on a 60 s run (`C = 2`, `R = 0`) are unchanged from 0a-iv — `secondary_allocated = 2`
  everywhere the old tests asserted it — so `test_phase0a_status.py`, `test_phase0a_money.py`
  and `test_phase0a_crash_cache.py` only needed the version pins.
- `tests/test_phase1b_targeting.py` checks the second invocation directory with the repo's
  long-path helpers: under `tmp_path` the media directory is past `MAX_PATH` and `Path.glob`
  silently returns nothing there (the files exist).
- The fixtures and the two edited test files were normalised to LF after Python-side edits left
  CRLF; `git diff` is clean of line-ending noise.

## Could not do

- **`pwsh`** is not installed; Windows PowerShell 5.1 ran the gate and the smoke script, as the
  plan's "PowerShell host" note allows.
- Nothing live was needed or run.

BUILD: COMPLETE

## Review + fix pass

Adversarial review of the uncommitted 1b-i tree against `docs/PLAN-v2.md` §2.3.4 step 4, §2.3.5,
§3.4, §6.1 and the cycle notes. Every gate below was re-run from scratch, never trusted from the
report. No live provider call: every command ran with `AUDD_API_TOKEN=` and `IDEA_ENGINE_SHAZAM=off`
exported, and every pipeline run used the injected fakes or `--fake-providers audd,shazam`.

### What was hand-checked and holds

- **Allocation arithmetic.** `overflow` recomputed by hand: 30 equal-class 12 s spans, `C = 12`,
  `R = floor(0.25 x 12) = 3`, `A = 9`; `allocation_order` is class -> longer span -> earlier start, so
  step (i) serves candidates 0-8 and breaks, leaving `quota = 0` for (ii) — exactly nine `first`
  picks. `largest-remainder-ties` recomputed: (i) 126 000 / 0 / 63 000, then (ii) `shares =
  {120 000, 60 000, 60 000}` over `total = 120 000` -> floors 1/0/0, `leftover = 1` to the tied
  remainders by earlier start -> 135 000 then 9 000. `hint-only-first`, `replacement-after-duplicate`
  and `duration-scaling` likewise match the fixtures.
- **Surplus return cannot loop or over-allocate.** `sum(quotas) == quota` exactly (floors plus a
  `leftover` in `[0, len(active)]`), `_take` caps each span at its quota, and every `while` iteration
  either takes at least one (so `quota` strictly decreases) or `active` empties. A 400-case randomised
  probe over mixed classes, overlapping spans and `A` in `[0, 40]` found no over-allocation, no repeated
  window and identical output on a re-run.
- **Only same-engine duplicates are excluded.** The exclusion set is seeded solely from Shazam picks
  (`allocate_secondary_windows(..., picked=...)`, `secondary_targeting.py:349-379`); nothing reads AudD
  observations. `test_overlap_allowed_probes_coincide_with_audd_windows` is parametrised over
  density 1 **and** 2 and asserts `set(shazam windows) & set(audd windows)` is non-empty at both.
- **Reserve and the second pass.** `serve_confirmations` passes `limit=min(2, reserve)` and decrements,
  so it is first-come and can never go negative; `A + confirmations + redistributed <= C`.
  `secondary_allocated` counts both passes (`cli.py:1096-1101`), so the 80 % rule sees the reserve
  clips' failures too. Verified empirically on `reserve-confirmation`: `attempts.jsonl` holds only the
  AudD run id (40 x prepared/dispatched/resolved) — the derived run id `<run_id>:secondary-2` never
  reaches the attempt journal or `UsdAdmitter` (neither is wired into `recognise_generation`);
  `budgets` holds one `shazam` row (`max 100, used 12, reserved 0`) because `ensure_budget` is
  `INSERT OR IGNORE`; 12 Shazam jobs / 12 physical attempts, no window in both passes.
  `usd_e6_spent = 200 000 = 40 x 5 000` — AudD only.
- **`algorithm_version` bump.** `recipe_id = sha256(canonical_json)` covers `algorithm_version`, so the
  Deep id changes and the Free recipe is untouched; the only `targeting:0` string left in the tree is
  the one the new test constructs. `docs/PLAN-v2.md` is unmodified; the gate expectation moved only in
  `tests/test_phase0a_status.py:217`, and the plan's 0a-iv gate command re-run verbatim with
  `algorithm_version=targeting:1,fusion:1` exits 0.
- **Frozen artefacts.** `git status --short -- profiles/ tests/golden/ data/corpus/ docs/PLAN-v2.md`
  is empty; the Local Free golden test is green in the full suite.
- **Rewritten 0a-iv tests.** Both keep their classification and 4 s eligibility assertions; only the
  `targeting:0` queue-order expectation changed, as it must.

### Findings

| Tag | Where | What / why |
|---|---|---|
| **P1-1** | `src/id_detector/secondary_targeting.py:215-224` (pre-fix) | `window_rms_energy` raised on a window clip that could not be read. Probed: a missing clip -> `FileNotFoundError`, a non-WAV byte stream -> `wave.Error: file does not start with RIFF id`. RMS energy is only the *second* ranking key, and the secondary runs **after** the paid AudD primary is already spent, so a pruned/truncated/corrupt clip would fail the whole run (`failed`, exit 1) over a lost tie-break hint. A non-16-bit clip already fell back to `0.0`; an unreadable one must do the same. |
| **P2-1** (fixed) | `src/id_detector/secondary_targeting.py:319-326` (pre-fix) | `_proportional` computed `shares[index] // total` where `total` is the sum of the active spans' lengths. With `min_intersection_ms = 0` every window "intersects" a zero-length span, so such a span becomes active with no weight and `total` can be 0 -> `ZeroDivisionError`. `blank_spans(..., min_ms=0)` really does emit zero-length spans (`>= min_ms` admits `start == cursor`). Not reachable from the shipped Deep recipe (`eligibility_min_intersection_ms = 4_000`), but a one-line guard in a crash path is worth taking. |
| **P2-2** | `src/id_detector/cli.py:1039-1099` | Neither Shazam pass polls `cancel_token` — only the paid sweep does (`paid_clip.py:446`). Pre-existing from 0a-iv; the plan scopes cancellation to the paid clip engine (E-H4, and the 0b-iii gate is written entirely in clips), and 4b-iv owns cancel/drain. 1b-i widens the window slightly (a cancelled Deep run now runs up to two Shazam passes instead of one), but adding the poll would turn runs that complete today into `cancelled` — a behaviour change outside this cycle. Left as a note. |
| **P2-3** | `tests/test_phase1b_targeting.py:277-287` | `test_deep_algorithm_version_is_bumped_and_a_targeting_0_result_is_incompatible` re-implements §3.4's compatibility predicate inside the test, so `assert not compatible` is tautological. The load-bearing assertions around it (`recipe_id` differs, `algorithm_version` differs, `adapter_versions` identical) are real, and the production `serves()` check is 1a-ii — nothing more can be asserted this cycle. |
| **P2-4** | `src/id_detector/cli.py:2014` | `idea sources` now lists one more recognition invocation directory per Deep run that takes a second pass. Intended (immutable per-invocation artefacts); no contract or test pins the count. |
| **P2-5** | `src/id_detector/secondary_targeting.py:440-442` | When the farthest eligible pair is closer than `reserve_min_separation_ms` the builder serves **one** clip rather than two. The plan only says "one if only one exists". Correct as a choice — two clips under the separation floor could never satisfy §2.3.4 step 5's bypass rule, so spending the second is waste — but it is an ambiguity resolution, not the plan's letter. |
| **P2-6** | `src/id_detector/secondary_targeting.py:106` | `secondary_reserve` computes `floor(f x C)` in floating point. Exact for the only value in use (0.25). Probed `f` in `{0.1, 0.25, 0.29, 0.3, 1.0}` against `math.floor` — no discrepancy today; a future fraction could floor one low. |
| **P2-7** | `src/id_detector/cli.py:1039` | The second pass's `on_window` callback resets `recognise_progress` to the small follow-up batch, so the web app's per-window ETA restarts at "of 3". Cosmetic, and the first pass already does this against the primary. |
| **P2-8** | `tests/test_phase0a_status.py:472-480` | The rewritten queue test proves "every eligible window is picked exactly once" but no longer proves *ineligible* windows are refused for a given span (the `targeting:0` order carried that implicitly). Strengthened below rather than left as a note. |

No P0. Nothing outside the cycle's scope was found in the diff; the `tests/fakes/providers.py`
`labels` table is the shared test infrastructure the plan's §5 anticipates and leaves the default
A/B/C-by-index labelling byte-identical.

### What was changed

1. **P1-1** — `src/id_detector/secondary_targeting.py:215-233`: `window_rms_energy` now wraps the read
   in `try/except (OSError, wave.Error, EOFError)` returning `0.0`, and guards a short/odd frame buffer
   (`len(frames) < 2`, and `np.frombuffer` over an even-length slice) so a truncated data chunk cannot
   raise either. The docstring says why the fallback is deterministic: energy is the second key, so 0.0
   drops the tie through to start order.
2. **P2-1** — `src/id_detector/secondary_targeting.py:328-337`: step (ii)'s `active` set now excludes
   zero-length spans, with a comment naming both reasons (no proportional weight; `total` would be 0).
3. **P2-8** — `tests/test_phase0a_status.py:483-487`: the rewritten 0a-iv queue test now asserts that
   every pick overlaps *the span that chose it* by at least 4 s, restoring the eligibility assertion the
   rewrite had made implicit.

### Tests added

- `tests/test_phase1b_targeting.py:636` —
  `test_a_clip_that_cannot_be_read_ranks_as_zero_instead_of_failing_the_run`: a missing clip, a
  non-WAV byte stream and a truncated RIFF header all read `0.0`, and `allocate_secondary_windows`
  still runs over them and ranks by start order.
- `tests/test_phase1b_targeting.py:397` —
  `test_a_zero_length_span_never_divides_the_proportional_share_by_zero`: pins that
  `blank_spans(..., min_ms=0)` emits zero-length spans, that step (ii) skips them instead of dividing
  by zero, and that a real span beside them still takes the whole proportional quota.
- Both were confirmed to **fail** against the pre-fix source (`FileNotFoundError` and
  `ZeroDivisionError` respectively) and pass after.

### Final gate outputs

`uv run pytest -q`

```text
881 passed, 93 deselected, 1 warning in 150.43s (0:02:30)
```

`uv run ruff check .`

```text
All checks passed!
```

`uv run ruff format --check .`

```text
241 files already formatted
```

`uv run python scripts/audit_fixtures.py`

```text
audited 426 files
fixture audit passed
```

Phase gate — `uv run pytest tests/test_phase1b_targeting.py -q`

```text
23 passed, 1 warning in 17.65s
```

0a-iv PowerShell gate, verbatim from the plan with the new `algorithm_version` expectation
(`powershell -NoProfile -ExecutionPolicy Bypass -File`; `pwsh` is not installed on this machine)

```text
complete; 6 matches; 0 failures; 2 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=...\present\tracklist.json
journal assertion passed at ...\invocations.jsonl:1
gate exit=0
```

Local mode — `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

`git status --short` / `git diff --stat`

```text
 M src/id_detector/cli.py
 M src/id_detector/recipes.py
 M src/id_detector/secondary_targeting.py
 M tests/fakes/providers.py
 M tests/test_phase0a_crash_cache.py
 M tests/test_phase0a_money.py
 M tests/test_phase0a_status.py
?? docs/reviews/build-1b-i.md
?? tests/fixtures/deep/
?? tests/test_phase1b_targeting.py

 src/id_detector/cli.py                 | 156 +++++++++---
 src/id_detector/recipes.py             |   2 +-
 src/id_detector/secondary_targeting.py | 443 ++++++++++++++++++++++++++++++---
 tests/fakes/providers.py               |  29 ++-
 tests/test_phase0a_crash_cache.py      |   4 +-
 tests/test_phase0a_money.py            |   6 +-
 tests/test_phase0a_status.py           |  59 +++--
 7 files changed, 591 insertions(+), 108 deletions(-)
```

Nothing was committed, branched or pushed.

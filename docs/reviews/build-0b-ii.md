# Build 0b-ii — Effective config, novelty, throttle handling, golden

The last Phase-0 cycle (E-H6, E-M2, E-M10, E-S1 plus the Local Free golden). Everything is
uncommitted on `main` at `804e665` for review. No dependency was added; `docs/PLAN-v2.md`,
`profiles/`, `data/corpus/`, `recipes.py` and `pricing.toml` are untouched. No live provider call
was made: every pipeline run used injected fakes through `_analyse` or
`IDEA_TEST_MODE=1 --fake-providers …`. Because the owner test-drives the tool after this cycle,
local mode was left alone except for the four fixes in scope; the checks below include a Free
and a Deep run through the real CLI (fakes), `config show` in every shape, and the smoke script.

---

## What changed, file by file

- `src/id_detector/profiles.py` — `PROFILE_FIXED_FIELDS` (the nine geometry fields a frozen
  profile owns: transform policy/rates/semitones, generation-0 and rescan window/hop/phase);
  `effective_app_config(file_config, profile)` — the **one** resolver: every other `AppConfig`
  field is carried from the owner's file *by iterating the dataclass fields*, so a knob added
  later cannot be dropped again; the file's `[rescan] max_generations` still caps the profile and
  a profile that turns hints off does so on top of the file's `[hints] enabled`;
  `profile_fixed_fields(file_config, profile)` — the plain-English note per decided field
  (`fixed by profile "free"`, `capped by this file (profile "free" allows 3)`, `turned off by
  profile "free"`).
- `src/id_detector/cli.py` — `analyse` resolves a profile through `effective_app_config`
  (H6: `present_min_track_ms`, `collapse`, `same_track_bridge_ms`, `shazam_requests_per_minute`,
  `recognise_concurrency` — and everything else in the file — now survive a `--profile` or a
  `default_profile`); `idea config show [--profile X]` prints the **effective** values with the
  profile's lines marked, a three-line header saying which profile decided them and why, and a
  trailer listing what the profile fixes that no config line controls (engines, novelty, hints);
  `_analyse` computes the novelty change points **once**, before the first fuse and only when
  `max_generations > 0`, and hands them to every fuse (M2); every fuse receives the true scanned
  window set (M10); a Free primary or a Deep secondary with failed Shazam windows logs how many
  got no usable answer and what that means for the status (S1).
- `src/id_detector/webapp/runner.py` — `_resolve_settings` calls the same `effective_app_config`
  (the second H6 call site); the hand-maintained `replace(...)` twin is gone.
- `src/id_detector/config_template.py` — `render_effective_config(config, *, fixed_by=,
  trailer=)` appends `  # <note>` to a marked field's line and comment lines at the end (still
  valid TOML — a test round-trips it); the template's precedence comment now says plainly that
  everything except engines/geometry/toggles applies under a profile and points at
  `idea config show --profile free`.
- `idea.example.toml` — the same comment (byte-identical with the template; the existing test
  asserts it).
- `src/id_detector/orchestrate.py` — `run_generation_loop(novelty_change_points_ms=,
  scanned_windows=)`: the points are taken from the caller when given, else computed only when
  `novelty_enabled and max_generations > 0` (M2); `scanned_windows(windows, observations)` —
  the windows a resolved `match|no_match` clip observation names as `window:<id>`; the loop fuses
  and reports coverage over that set (default: every window, the free-recipe truth) (M10).
- `src/id_detector/shazam.py` — `InjectedHTTPClient._malformed()`: a non-JSON body, a JSON body
  whose root is not an object, or an object without a `matches` list now **penalises the limiter
  as well as tripping the breaker** and raises `ShazamHTTPError` (never retried at HTTP 200,
  never cached — before, a JSON error object such as `{"error": "rate limited"}` was recorded and
  cached as a `no_match` for 30 days) (S1).
- `tests/fakes/providers.py` — a script's `"windows"` table is now keyed by the window's
  position in the mix: a clip is ranked among the `.wav` files of its directory
  (`<start_ms:010d>-<variant>.wav`, all written before the first request). Before, the index was
  first-seen order, which for Shazam follows the job-store lease order (`created_at, id`) and was
  measured to differ run to run even at concurrency 1; the golden needs the mapping stable. A
  path with no such directory (synthetic clips in unit tests) keeps first-seen numbering.
- `tests/fakes/scripts/golden-free.json` — the golden's script: every window a match except the
  fourth (`no_match`).
- `scripts/make_golden.py` — new: runs the Free recipe offline over `tone-60s.wav` with the
  scripted Shazam fake (`run_local_free`), writes `tests/golden/local-free/tracklist.json` as
  canonical pretty JSON, and owns `IGNORED_KEYS`, `is_timestamp_key` and `semantic()` (the
  recursive strip) so the test and the generator cannot disagree.
- `tests/golden/local-free/tracklist.json` — the Local Free golden (three tracks, `complete`,
  `achieved=free`).
- `tests/test_phase0a_crash_cache.py` — `test_provider_http_clients_ignore_proxy_environment`
  fed one AudD-shaped mock body (`{"status": "success", "result": null}`) to both HTTP clients;
  the Shazam client now refuses a body with no `matches` list (S1), so the Shazam leg of that
  test answers `{"matches": []}`. Its purpose (`trust_env=False` on both clients) is unchanged.
- `docs/reviews/README.md` — the 0b-ii row.

### Tests added

`tests/test_phase0b_config.py` (21):
- **H6** — `effective_app_config` over *every* `AppConfig` field (geometry from the profile even
  when the file tries to move it; all else from the file; rescan cap `min`; hints AND); the rescan
  ceiling (default 0 caps the profile's 3; a generous file leaves the profile's own 3) and a
  hints-off profile; `idea analyse --profile free` **and** `default_profile = "free"` through the
  real CLI (monkeypatched `_analyse`) carry the five knobs, the web runner's `_resolve_settings`
  carries them too, and **the two sites produce an equal `AppConfig`** (the lock-step assertion)
  plus the same capped `max_generations`; `config show --profile free` marks the geometry lines,
  leaves the file's lines unmarked, prints the trailer, parses as TOML to the effective values and
  leaks no credential name; the "capped by this file" note and `default_profile` attribution;
  no profile → no markers; unknown profile → exit 2.
- **M2** — the novelty pass is never run with rescans off (`counts.novelty_change_points == 0`);
  with rescans on and a Deep run that fuses twice (secondary re-fuse), `novelty_change_points`
  runs **exactly once** and both fuses receive the same points; `--no-novelty` still wins; the
  loop's own guard.
- **S1** — `InjectedHTTPClient` over an `httpx.MockTransport`: six malformed bodies (HTML, empty,
  `[]`, `{"error": …}`, a bare string, `{"matches": "nope"}`) each raise, are accounted before
  network I/O, drop the limiter's rate and count a breaker failure; a real `{"matches": []}` is
  untouched; a 429 still penalises. Through `_analyse`: `free-thin` (two malformed windows) ends
  `partial` / 2 failures, logs "2 of 7 windows got no usable answer from Shazam … 80 %", and the
  fuser receives exactly the five answered windows.
- **M10** — `scanned_windows` on synthetic records (resolved only, `window:` ids only, order
  kept, Panako span queries ignored); a Deep density-2 run: the first fuse sees exactly AudD's
  four even-indexed windows while all seven still reach the sidecars, the re-fuse adds the
  secondary's probes, proved evidence never exceeds the time listened to, and every gap's
  `n_windows` counts answered windows only; a complete Free run still scans the whole mix
  (`unscanned_ms == 0`, unchanged behaviour).

`tests/test_golden_local_free.py` (4): the golden and its script exist and the ignore list is
exactly the plan's; `semantic()` strips the identity fields and every `*_at`/`timestamp` key at
any depth and nothing else; a fresh run matches the golden semantically and certifies three
time-ordered `track` entries, `complete`, `achieved=free`; the golden is canonical JSON with LF
endings.

---

## Required command outputs

### 1. `uv run pytest -q`

```text
752 passed, 93 deselected, 1 warning in 146.62s (0:02:26)   # the usual pydub audioop DeprecationWarning
```

### 2. `uv run ruff check .`

```text
All checks passed!
```

### 3. `uv run ruff format --check .`

```text
230 files already formatted
```

(`uv run ruff format .` was run on the new/edited files first. The full suite was run twice: the
first pass surfaced the `test_provider_http_clients_ignore_proxy_environment` mock body, fixed as
listed above; the pasted run is the second, in the foreground with `AUDD_API_TOKEN=` exported
empty.)

### 4. `uv run python scripts/audit_fixtures.py`

```text
audited 377 files
fixture audit passed
```

### 5a. Phase gate — `uv run pytest tests/test_phase0b_config.py tests/test_golden_local_free.py -q`

```text
.........................                                                [100%]
25 passed, 1 warning in 16.59s
```

### 5b. `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

(exit 0; `pwsh` is not installed, Windows PowerShell 5.1 ran it as the plan's host note allows.)

### 5c. Regression evidence — the 0a-iv Deep gate through the real CLI (fakes, fresh work root),
with the M2 count added to the expectation

```powershell
$env:IDEA_TEST_MODE = "1"; $env:IDEA_FAKE_SCRIPT = "tests/fakes/scripts/gate0a-deep.json"
$w = Join-Path $env:TEMP ("idea-gate0a-" + [guid]::NewGuid().ToString("N"))
uv run idea analyse tests/fixtures/audio/tone-60s.wav --recipe deep --fake-providers audd,shazam --work-root $w
uv run python scripts/assert_journal.py --work-root $w --expect status=complete algorithm_version=targeting:0,fusion:1 usd_e6_reserved=36750 usd_e6_spent=35000 usd_e2_spent=4 paid_attempts=7 paid_requests=7 novelty_change_points=0
```

```text
complete; 6 matches; 0 failures; 2 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=<workroot>\present\tracklist.json
journal assertion passed at <workroot>\invocations.jsonl:1
gate exit=0
```

### 5d. Regression evidence — a Free run through the real CLI (fakes) and `config show`

```text
$ IDEA_TEST_MODE=1 IDEA_FAKE_SCRIPT=tests/fakes/scripts/golden-free.json uv run idea analyse tests/fixtures/audio/tone-60s.wav --recipe free --fake-providers shazam --work-root <tmp>
complete; 6 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=<workroot>\present\tracklist.json
$ uv run python scripts/assert_journal.py --work-root <tmp> --expect status=complete achieved=free usd_e6_spent=0 novelty_change_points=0 requests=7
journal assertion passed at <workroot>

$ uv run idea config show --config show.toml --profile free        # show.toml sets [recognise] 7/2 and [present] false/1234/5000
# source: show.toml + defaults + profile "free" (chosen by --profile)
# Lines marked "fixed by profile" come from the frozen profile "free"; the file's value is ignored for those.
# Every other line is exactly what a run under this profile uses.
…
[recognise]
requests_per_minute = 7
concurrency = 2
…
[schedule]
window_ms = 12000  # fixed by profile "free"
hop_ms = 9000  # fixed by profile "free"
phase_ms = 0  # fixed by profile "free"

[rescan]
window_ms = 12000  # fixed by profile "free"
hop_ms = 5000  # fixed by profile "free"
phase_ms = 0  # fixed by profile "free"
max_generations = 0  # capped by this file (profile "free" allows 3)
…
[present]
collapse = false
same_track_bridge_ms = 1234
min_track_ms = 5000

# Profile "free" also fixes what no config line controls:
#   engines = shazam  (--recipe deep adds the paid AudD sweep)
#   novelty change points = on  (rescan triggers; computed only when max_generations > 0)
#   hints = on
$ uv run idea config show --config no-such.toml --profile max_accuracy | head -1
# source: built-in defaults (no-such.toml not found) + profile "max_accuracy" (chosen by --profile)
$ uv run idea config show --config show.toml --profile nope ; echo $?
unknown profile 'nope'; frozen profiles are: free, max_accuracy
2
```

Also run: `uv run pytest tests/test_stage4c_generations.py tests/test_stage2b_pipeline.py -m slow -q`
(the slow generation-loop suite, since `run_generation_loop` changed): `14 passed`.

### 6. `git status --short`

```text
 M docs/reviews/README.md
 M idea.example.toml
 M src/id_detector/cli.py
 M src/id_detector/config_template.py
 M src/id_detector/orchestrate.py
 M src/id_detector/profiles.py
 M src/id_detector/shazam.py
 M src/id_detector/webapp/runner.py
 M tests/fakes/providers.py
 M tests/test_phase0a_crash_cache.py
?? docs/reviews/build-0b-ii.md
?? scripts/make_golden.py
?? tests/fakes/scripts/golden-free.json
?? tests/golden/local-free/
?? tests/test_golden_local_free.py
?? tests/test_phase0b_config.py
```

### 7. `git diff --stat`

```text
 docs/reviews/README.md             |   1 +
 idea.example.toml                  |  13 +++--
 src/id_detector/cli.py             | 117 +++++++++++++++++++++++++++----------
 src/id_detector/config_template.py | 108 ++++++++++++++++++++++++----------
 src/id_detector/orchestrate.py     |  52 ++++++++++++++++-
 src/id_detector/profiles.py        |  63 +++++++++++++++++++-
 src/id_detector/shazam.py          |  23 +++++++-
 src/id_detector/webapp/runner.py   |  39 ++++---------
 tests/fakes/providers.py           |  36 +++++++++++-
 tests/test_phase0a_crash_cache.py  |  13 +++--
 10 files changed, 359 insertions(+), 106 deletions(-)
```

---

## Plan ambiguities resolved

1. **What a profile "fixes".** The plan and the template say a profile fixes "the engines and
   the transform/schedule/rescan geometry and the hint/novelty toggles". Read literally:
   `PROFILE_FIXED_FIELDS` is exactly the nine geometry fields; engines, novelty and hints are not
   `AppConfig` fields and are reported in `config show`'s trailer. `allow_third_party_upload`
   stays the file's (as both call sites already had it). Carrying by dataclass iteration rather
   than a second hand-written list is what makes "no documented knob is silently ignored" a
   property instead of a promise.
2. **`config show` without `--profile` when the file has `default_profile`.** H6 notes the bug
   "bites even without `--profile`", so the command applies the file's `default_profile` exactly
   as `analyse` does and says so (`chosen by default_profile in idea.toml`). Without either, the
   output is unchanged from before (the existing Stage-9 tests still pass).
3. **`rescan_max_generations` under a profile** is `min(profile, file)` as before; the note says
   which side won ("fixed by profile" when the profile's number is the smaller, "capped by this
   file (profile allows N)" otherwise) so the owner sees why a profile that certifies 3 rescans
   runs 0.
4. **"Novelty computed only when `max_generations > 0`".** `max_generations` is the value the
   caller resolved (CLI flag → config cap → profile), so a profile that certifies novelty on
   still computes nothing under the default rescan ceiling of 0 — novelty only ever feeds rescan
   triggers. `counts.novelty_change_points` is therefore 0 on default runs (it used to be a
   non-zero count that nothing consumed); the 0a-iv gate expectation gained
   `novelty_change_points=0` as evidence. `--no-novelty` still wins when rescans are on.
5. **"Scanned" (M10)** = a `match|no_match` clip observation from any provider names the window
   (`window:<id>` in `source_ids`). An `error` observation means no engine answered, so the
   window is not scanned; a Panako span query names no window and never was one. On the Free
   recipe every answered window is scanned, so a complete Free run is unchanged (`unscanned_ms ==
   0`, asserted). The loop's rescan budget/`existing_shapes` also see the scanned set, which is
   the honest input (an unscanned frozen window is a legitimate rescan target); rescans are off
   by default so nothing changes for the owner's runs.
6. **What the partition reports for unscanned time.** `partition_durations` gives an episode's
   unresolved boundary precedence over unscanned time, so on the 60 s fixture the holes a
   density-2 sweep leaves next to episode edges show as `unresolved_boundary_ms`, not
   `unscanned_ms`; the test therefore asserts the set the fuser receives and the invariant
   "proved evidence ≤ time listened to" rather than a particular `unscanned_ms`.
7. **S1 "decode errors".** Read as the three shapes a throttled Shazam produces past its 429s:
   a non-JSON body (HTML/empty), a JSON root that is not an object, and an object with no
   `matches` list. All three penalise the limiter like a 429, trip the breaker and fail the window
   (status 200 is not retryable, as before). Every genuine recognition — match or not — carries a
   `matches` list (the committed fixtures and shazamio's own no-signature answer
   `{"matches": []}` included), so no real answer is rejected. The retry policy for Shazam is
   unchanged ("existing limiter", §2.3.1).
8. **`free` → `partial` below 80 %** was already the §2.3.5 row from 0a-iv (`_run_status` /
   `_achieved` reused, not re-implemented); this cycle adds the surfacing (the log line, and the
   failed windows no longer masquerading as scanned no-evidence time) and the regression through
   `free-thin.json`.
9. **The golden's run settings** are the free recipe's defaults except three speed-only or
   fixture-driven choices recorded in `scripts/make_golden.py`: one Shazam worker with an
   unbounded ceiling (the fake answers instantly; results are content-addressed and
   concurrency-independent in production) and `min_track_ms = 0`, because the fixture's tones
   each play ~20 s — under the 30 s on-air floor a real set needs, the tracklist would be empty and
   the golden would certify nothing.
10. **"Compares entries semantically".** The whole document is compared after stripping, not only
    `entries` — stricter, and the stripped keys are exactly the plan's six plus any timestamp key
    (`*_at`, `timestamp`), so 1a-i's `run_id`/`analysis_key`/`presentation_version` will not break
    the gate when they appear.

## Bugs found and fixed on the way (each with a regression test)

- `shazam.py`: a throttled Shazam answering HTTP 200 with a JSON error object was stored and
  cached as a `no_match` for 30 days (`recognise.py` derives the state from `matches`/`track`
  presence). Now refused as malformed — `test_malformed_shazam_bodies_penalise_the_limiter_and_fail_the_window[{"error"…}]`.
- `tests/fakes/providers.py`: the scripts' window indices were dispatch-order, so a Shazam-side
  script such as `free-thin.json`'s `"0"/"1"` did not name windows 0 and 1 — measured to vary run
  to run even with one worker (job leases order by `created_at`, millisecond resolution, then id).
  Fixed by directory rank; `test_free_run_with_malformed_replies_is_partial_and_says_so` now
  asserts the answered windows are exactly the five after the first two, and the golden gate
  depends on it.

## Could not do

- **`pwsh scripts/…`** — PowerShell 7 is not installed; Windows PowerShell 5.1 ran the smoke
  script and the CLI gate, as the plan's host note allows.
- **A live throttle** — the S1 shapes are asserted against an `httpx.MockTransport` and the
  scripted fake; the hard rule forbids reproducing them against the real endpoint.
- **A visible `unscanned_ms` on the fixture** — see ambiguity 6; the fixture is too short for a
  hole that is not adjacent to an episode edge, so the M10 test asserts the scanned set the fuser
  receives and the evidence invariant instead.

BUILD: COMPLETE

---

## Review + fix pass

Adversarial review on Opus, then the fixes. Every check was re-run here, never trusted from the
report. No live provider call was made: every CLI run used `IDEA_TEST_MODE=1 --fake-providers`
with `AUDD_API_TOKEN=` empty, and every unit run used injected fakes or `httpx.MockTransport`.
`docs/PLAN-v2.md`, `profiles/`, `data/corpus/`, `pricing.toml` and `recipes.py` were not touched.

**No P0.** The cycle's scope matches the plan section (config carry-over at both call sites,
`idea config show [--profile X]`, novelty guarded + hoisted, Shazam decode errors to limiter
penalty + failures, `free` to `partial` below 80 %, true scanned window set, the Local Free
golden with the plan's exact ignore list), the gate passes, and `git diff --stat` lists only this
cycle's files.

### P1 — fixed

**P1-1 · `src/id_detector/profiles.py:800` + `src/id_detector/cli.py:370` — `config show`
blamed a config file that does not exist.**
`profile_fixed_fields` always wrote `capped by this file (profile "free" allows 3)`, so
`uv run idea config show --profile free` with no `idea.toml` printed that line three lines under
its own header `# source: built-in defaults (idea.toml not found)`. That is the owner's very
first local run, and it points a non-programmer at a file to edit that is not there.
*Fix:* `profile_fixed_fields(..., capped_by=...)`; `config show` passes `"this file"` only when
`config.is_file()`, else `"the built-in default"`.
*Test:* `tests/test_phase0b_config.py::test_config_show_with_no_config_file_blames_the_built_in_default_not_a_file`.

**P1-2 · `src/id_detector/cli.py:830` — the S1 throttle line had the wrong denominator and
announced a verdict it had not checked.**
It read `f"{recognised.failures} of {frozen_count} windows got no usable answer ... below 80 %
resolved the run ends partial"`. Two defects:

1. `recognised.failures` counts **every window sharing a failed query's cache key**, transform
   siblings included (`src/id_detector/recognise.py:563`), while `frozen_count` counts only the
   frozen ones. Under the documented `[transforms] policy = "global"` a throttled run therefore
   prints nonsense such as "15 of 7 windows got no usable answer".
2. The clause "the run ends partial" was emitted whatever the fraction: one failed window out of
   a hundred (99 % resolved, status `complete`) still told the owner the run ends partial.

*Fix:* the numerator is now `primary_planned - primary_resolved` and the denominator
`primary_planned` — exactly the two numbers `_run_status` feeds `_achieved` — and the verdict is
computed with `_achieved` itself, so the line states what happened to *this* run
("5 of 7 resolved, below the 80 % this recipe needs, so the run ends partial" /
"6 of 7 resolved, still at or above the 80 % this recipe needs").
*Tests:* `test_free_run_with_malformed_replies_is_partial_and_says_so` strengthened to assert the
whole sentence; new `test_one_unanswered_window_says_the_run_can_still_complete` and
`test_no_throttle_line_when_every_planned_window_was_answered`.

**P1-3 · `src/id_detector/orchestrate.py:201` — M10's scanned subset silently widened the rescan
request budget.**
`run_generation_loop` set `spent_windows = len(all_windows)` and
`budget = max(request_budget, len(all_windows))`, and `all_windows` had just become the
*answered* subset. The rescan allowance is `max(0, request_budget - spent_windows)`, so it grew
by every unanswered window. Under a Deep density-2 sweep half the windows go unanswered **by
design**, so with rescans enabled (`[rescan] max_generations > 0`) the loop could issue up to
half a mix's worth of Shazam requests beyond the `--max-requests` ceiling the owner set — the
one knob bounding a run against R11 throttling. The change was a side effect: M10 is about what
the fuser *sees*, not about what the run may *spend*.
*Fix:* the budget accounting is back on `windows.records` (windows generated); the fused set
stays the scanned subset.
*Test:* `test_the_scanned_subset_does_not_widen_the_rescan_request_budget` — 13 windows, three
answered, `request_budget = 13`. Verified to **fail before the fix** (`final_generation == 1`:
the loop ran a whole extra rescan generation) and pass after.

### P2 — notes (one fixed because it was free)

- **P2-1 · `cli.py` (fixed).** `novelty_points` was assigned *after* the `_fuse` closure that
  reads it — correct only because the first call happens later; any reordering would have been a
  run-time `NameError` on every `analyse`. The computation now sits above `async def _fuse`,
  still inside `fuse_ms`, so timings and behaviour are unchanged.
- **P2-2 · `orchestrate.py:170`.** The `scanned_windows` keyword shadows the module-level
  `scanned_windows()` inside `run_generation_loop`. Harmless today; renaming the public keyword
  would churn `cli.py` and the new tests, so left as a note.
- **P2-3 · `orchestrate.py:277`.** `existing_shapes=window_shapes(all_windows)` no longer
  registers unanswered generation-0 shapes, so a rescan may re-cut an identical shape. Deliberate
  per the build report (an unanswered window is a legitimate rescan target) and inert with
  rescans off by default; recorded rather than reverted.
- **P2-4 · `tests/fakes/providers.py:123`.** `_directory_ordinal` ranks *all* `.wav` files beside
  the clip, so under `[transforms] policy = "global"` a script's window index would count
  transform siblings, and a fake that mixes directory-ranked with first-seen paths can collide on
  an ordinal. No committed script hits either case; test-fake only.
- **P2-5 · `scripts/make_golden.py`.** `main()` leaves its `tempfile.mkdtemp` work root behind.
  Deliberate (it prints the path for inspection) but it is litter on a developer machine.
- **P2-6 · not this cycle.** `idea analyse` passes `progress=None`, so `_recognise_log` — and
  therefore the new S1 line — is a no-op on the CLI; only the web app shows it. On the CLI the
  owner still gets `partial (primary_not_achieved); ... ; 2 failures` in the summary. Changing the
  CLI's progress plumbing is outside 0b-ii.
- **P2-7 · cosmetic.** `config show --profile free` still prints the `[deep]` table, which the
  free recipe never uses; the trailer's `engines = shazam` is the only hint.

### Checks the review ran that the report did not

- **Golden determinism:** `tests/test_golden_local_free.py` run **three** times back to back —
  green each time — and `uv run python scripts/make_golden.py` re-generated
  `tests/golden/local-free/tracklist.json` **byte-identically** (2 918 bytes, unchanged SHA-256
  `2b9f0865c4e1725a...`), so the committed golden is the generator's current output and the
  fake's window mapping really is stable.
- **Ignore list vs the plan:** `IGNORED_KEYS` is exactly the plan's six, asserted by the test.
- **Frozen profiles:** `tests/test_stage4d_profiles.py` (incl.
  `test_committed_profiles_rederive_byte_for_byte`) — 9 passed.
- **Total Shazam outage** (a throttle that answers nothing), the scenario M10 makes newly
  reachable with an empty scanned set: `IDEA_TEST_MODE=1 --fake-providers shazam` with a
  `default: malformed` script gives `partial (primary_not_achieved); 0 matches; 7 failures`,
  exit 0, tracklist written. No crash in `fuse_generation` with `windows=[]`.
- **`shazamio` boundary:** confirmed `Shazam.recognize` issues exactly one request through the
  injected client (`api.py:568` to `send_recognize_request_v2`); `GeoService` is never reached by
  the recognition path, so requiring a `matches` list cannot reject a legitimate response.
- **Cache safety of S1:** `cache_valid` (`recognise.py:196`) returns `False` for any state other
  than `succeeded`/`no_match`, so a malformed body is stored as an error payload with state
  `permanent_failure` and is re-attempted next run, never served as a 30-day `no_match`.
- **Local mode, read as a non-programmer:** `idea config show` and `idea config show --profile
  free` both read cleanly after P1-1 — the header names the source, the marked lines say who
  decided them, the trailer names what no config line controls. A real Free scan
  (`IDEA_TEST_MODE=1 --fake-providers audd,shazam` on `tests/fixtures/audio/tone-60s.wav`) ends
  `complete; 6 matches; 0 failures; ... 3 episodes` with `novelty_change_points: 0` in the
  journal.

### Final gates (all re-run after the fixes)

```text
$ uv run pytest -q
756 passed, 93 deselected, 1 warning in 127.93s (0:02:07)

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
230 files already formatted

$ uv run python scripts/audit_fixtures.py
audited 377 files
fixture audit passed

$ uv run pytest tests/test_phase0b_config.py tests/test_golden_local_free.py -q   # cycle gate
29 passed, 1 warning in 15.20s

$ uv run pytest tests/test_stage4d_profiles.py -q                                 # frozen profiles
9 passed, 1 warning in 1.27s

$ uv run pytest tests/test_golden_local_free.py -q   (x3, determinism)
4 passed, 1 warning in 2.27s
4 passed, 1 warning in 2.65s
4 passed, 1 warning in 2.34s

$ powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

REVIEW: OK_TO_COMMIT

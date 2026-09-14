# Build — owner truth review

Built one out-of-plan, local-first owner cycle: `idea truth review --set <set-id>` opens a
loopback-only review screen for one selected truth record. It exists to clear the unverified
`release-1` bottleneck before launch gate L3; no plan contract was changed.

## File-by-file changes

- `src/id_detector/truth_review.py` — new bounded review application: safe set lookup by contract
  `set_id`, review-session state, read-only cached-audio resolution, range audio route, prediction
  reveal endpoint, CSRF/Origin/Host gates, corpus-policy validation, offset preview arithmetic,
  first-pass save, and the complete single-page CSS/JavaScript UI.
- `src/id_detector/cli.py` — added `idea truth review --set <set-id> [--corpus ...] [--port
  8797] [--no-open]`. It binds only loopback, reports missing audio without refusing review, and
  follows the existing `serve` lifecycle/browser-open pattern.
- `src/id_detector/truth.py` — extended the existing verification path to accept an in-memory
  independent annotation record and optional review provenance. The existing annotation writer
  remains authoritative. First-pass annotation is atomically replaced before truth; if truth
  replacement fails, the prior annotation is restored (or the newly created one removed), so the
  original `ground_truth.json` remains intact.
- `src/id_detector/present/index.py` — split index-document construction from persistence and added
  a read-only media-key lookup. A stale/missing `work/index.json` is rebuilt in memory for review,
  never written.
- `scripts/check_page_js.py` — added the generated truth-review page to the existing Node syntax
  gate. The gate now checks 53 inline scripts across 22 real page renders.
- `tests/fixtures/truth-review/fixture-set/ground_truth.json` and its fixture README — committed a
  three-row synthetic draft only. Tests generate the short tone WAV at runtime through
  `scripts/make_audio_fixtures.py`; no audio binary was committed.
- `tests/test_truth_review.py` — added ten offline tests covering rendering and the full keyboard
  legend; read-only indexed range audio and missing-audio operation; exact first-pass transition
  and untouched-field preservation; offset uniformity/clamping and write-free preview; explicit
  offset save; prediction absence/reveal/durable provenance; CSRF and foreign-Origin refusal; set
  and audio traversal refusal; injected atomic-write failure; and fixture audit after save.
- `docs/reviews/README.md` — recorded this owner-priority addition without editing the plan.
- `docs/reviews/build-truth-review.md` — this report.

No file under `data/`, `work/`, `profiles/`, or the second session's owned paths was changed.

## Keyboard map

| Key | Action |
|---|---|
| `Space` | Play/pause |
| `J` / `L` | Seek −5 s / +5 s |
| `Shift+J` / `Shift+L` | Seek −30 s / +30 s |
| `Enter` | Confirm the current row as the owner's truth |
| `E` | Edit artist/title inline |
| `T` | Stamp the current row's start from the playhead |
| `N` / `P` | Select next/previous row |
| `S` | Explicitly save the complete first pass |
| `?` | Open/close the help overlay containing this map |

Clicking a truth row also selects it and seeks to its claimed start. Navigation, editing,
confirmation, stamping, offset preview, offset confirmation, and undo are browser-local; only
Save writes.

## Provenance and corpus independence

The first annotation pass records this `review_provenance` object:

```json
{
  "predictions_visible_during_review": false,
  "tool": "idea truth review",
  "version": "0.1.0",
  "reviewed_at_utc": "<ISO-8601 UTC timestamp ending Z>"
}
```

`predictions_visible_during_review` is sticky: revealing predictions once marks the session; a
later explicit save records `true`, and reopening reads and visibly preserves that taint. Initial
HTML contains no prediction payload or labels. Reveal is a same-origin, CSRF-protected POST and
returns separately styled “Our guess” rows. Guesses never populate truth fields and there is no
accept-guess action. Confirm always confirms the independently supplied owner tracklist text.

## Judgement calls

- A first-pass save requires every still-draft row to be confirmed. This preserves
  `verify_truth`'s completed-independent-annotation contract instead of inventing a partial web
  state. “Needs-listening” remains an explicit unsaved state; already verified rows are read-only
  so this tool cannot step backwards over second-pass/resolution ownership.
- `annotator_ref` is the local role token `owner`, since the requested command has no annotator
  option and is explicitly owner tooling.
- The +48 s Mall Grab and +51 s DJ Heartstring suggestions are constants sourced from the existing
  scorer diagnostics in `docs/accuracy/release-1-free-draft.md`. They are labelled suggestions and
  never auto-applied.
- Offset preview shifts start/end range values together. Interior values move uniformly; boundary
  values clamp to `[0, duration]`. Starts clamp at `duration - 1 ms` so a verified episode can
  retain the truth contract's required positive audible span. The UI reports how many values were
  clamped.
- Revealing predictions does not itself write, because the screen's contract says nothing is
  written on navigation and Save is explicit. The durable visibility flag is written with the
  annotation pass on Save.
- Labels are capped and rejected if they contain a URL, an `@handle`, or a long numeric identifier.
  All non-owned episode fields are copied from the selected original record.

## Verification outputs

### 1. `uv run pytest -q`

```text
........................................................................ [ 97%]
........................                                                 [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
1176 passed, 93 deselected, 1 warning in 605.53s (0:10:05)
```

### 2. `uv run ruff check .`

```text
All checks passed!
```

### 3. `uv run ruff format --check .`

```text
286 files already formatted
```

### 4. `uv run python scripts/audit_fixtures.py`

```text
audited 455 files
fixture audit passed
```

### 5. Focused and compatibility gates

`uv run pytest tests/test_truth_review.py -q`:

```text
..........                                                               [100%]
10 passed in 8.70s
```

`uv run pytest tests/test_stage2a_truth.py tests/test_score_corpus.py tests/test_playlists.py tests/test_phase3a_honesty.py tests/test_golden_local_free.py tests/test_stage7_server.py tests/test_phase0a_security.py -q`:

```text
........................................................................ [ 50%]
......................................................................   [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
142 passed, 1 warning in 69.37s (0:01:09)
```

`uv run python scripts/check_page_js.py`:

```text
page JavaScript check passed: 53 inline scripts across 22 page renders
```

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1` (with
`IDEA_TEST_MODE=1` in the parent environment):

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/gate_local_mode.ps1` (with
`IDEA_TEST_MODE=1` in the parent environment) functionally passed, then the first attempt exited 1
because `taskkill.exe` lost a browser-child cleanup race after success:

```text
local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok
Evidence: C:\Users\natha\AppData\Local\Temp\idea-local-gate-70864f68cd2445aa90d058fb1c0def6b
taskkill.exe : ERROR: The process with PID 69948 (child process of PID 27876) could not be terminated.
```

The clean rerun used `-Port 8793` only to avoid a possibly retiring listener and exited 0:

```text
complete; 6 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\?\C:\Users\natha\AppData\Local\Temp\idea-local-gate-9be6d91363534f5eae09c04b4ad4340f\work\a63408abeea9adf1c0d4452a6509d9ee4e5399d192f32ee7307bbcef39c83e27\e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d\present\bundles\cd5ef524df7ee38e5023628bdf3a64297a0bbb9033e8af82303acc69659b328a\tracklist.json
prepared offline cached mix e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d
local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok
Evidence: C:\Users\natha\AppData\Local\Temp\idea-local-gate-9be6d91363534f5eae09c04b4ad4340f
```

### 6. Final repository state

`git status --short`:

```text
 M docs/reviews/README.md
 M scripts/check_page_js.py
 M src/id_detector/cli.py
 M src/id_detector/present/index.py
 M src/id_detector/truth.py
?? docs/reviews/build-truth-review.md
?? src/id_detector/truth_review.py
?? tests/fixtures/truth-review/
?? tests/test_truth_review.py
```

`git diff --stat` (Git does not include untracked files in this stat):

```text
 docs/reviews/README.md           |  4 +++
 scripts/check_page_js.py         | 17 +++++++++
 src/id_detector/cli.py           | 51 +++++++++++++++++++++++++++
 src/id_detector/present/index.py | 41 ++++++++++++++++++++--
 src/id_detector/truth.py         | 75 +++++++++++++++++++++++++++++++++-------
 5 files changed, 174 insertions(+), 14 deletions(-)
```

The status shows no path under `work/` or `data/`.

## Could not do

Nothing in requested scope remains undone. No live provider call, real URL analysis, branch, commit,
push, real-corpus save, or real-`work/` garbage collection was performed.

BUILD: COMPLETE


## Review + fix pass (sol xhigh review folded in)

Adversarial review of the uncommitted cycle, then the fixes, in one pass.  Every finding below
was reproduced here before it was fixed; the first reviewer could not execute anything, so each
of its claims was re-derived rather than taken on trust.  The tool was exercised only against
`tests/fixtures/truth-review/`; `data/corpus/release-1/` was read (never written) once, to see
what the corpus actually contains.

### Findings

| Tag | Source | Where (pre-fix) | Verdict |
|---|---|---|---|
| P0-1 | sol | `truth_review.py:173` | **Confirmed, and worse than reported** |
| P0-2 | sol | `truth_review.py:275`, `:289` | **Confirmed** |
| P1-3 | sol | `truth_review.py:354`, `:362` | **Confirmed** |
| P1-4 | sol | `truth_review.py:355` vs `:159` | **Confirmed** |
| P1-5 | sol | `truth_review.py:260`, `:284`; `truth.py:404` | **Confirmed** |
| P1-6 | sol | `cli.py:1712`, `truth_review.py:52` | **Confirmed, with one nuance** |
| P1-7 | sol | the page's `keydown` handler | **Confirmed** |
| P1-8 | sol | test suite | **Confirmed** |
| P1-9 | sol | gates | **Done** |
| R-10 | this review | `truth_review.py` session guards | **New** |
| R-11 | this review | `truth_review.py` `/csrf` GET | **New (hardening)** |

**P0-1 — confirmation destroyed role annotations, and fabricated a claim.**  `reviewed_record`
replaced every draft row's `role_segments` with
`[TruthRoleSegment(from_ms=start[0], to_ms=end[1], role="dominant")]`.  Reading the real corpus
read-only makes the stake concrete: its 222 role segments across seven sets are **all**
`uncertain`, except nine `layer` spans in `release1-mph-youtube-set`.  So confirming a set would
not merely have flattened its segments — it would have relabelled every one of them `dominant`,
asserting a confidence about layering the owner explicitly declined to assert, and deleted the
nine hand-made `layer` spans outright.  Sol's finding stands; its severity was understated.

*Fixed* by `reconciled_role_segments()`, which never invents a role: an unchanged or purely
translated span (every endpoint moved by the same delta — what a bulk offset does) moves the
existing segments by that delta, preserving layering and any time-varying structure exactly; any
other timing edit keeps the same segments and roles and clips them into the new audible span,
dropping only a segment the edit moved entirely outside it.  An episode with no role annotation
still has none.  The string `"dominant"` no longer appears anywhere in `truth_review.py`.

*Tests:* `test_confirmation_preserves_hand_made_role_segments` (an `incoming` + `layer` pair
survives byte-for-byte), `test_offset_translates_role_segments_and_never_invents_dominant`,
`test_non_uniform_timing_edit_clips_roles_without_relabelling`.  The existing
`test_confirm_and_save_uses_verify_contract_and_preserves_other_fields` had *asserted* the defect
(`role_segments[0].role == "dominant"`) and now asserts `new.role_segments == old.role_segments`.

**P0-2 — prediction exposure was not durable.**  Reproduced: `reveal_predictions` set an
in-memory flag and only `save` wrote it, so reveal → stop the server → reopen → save recorded
`false` after the owner had seen our answers.  `_previous_prediction_visibility` read the flag
back from `annotation-first.json`, which does not exist until the first save, so nothing carried
it across the gap.

*Fixed* by making the exposure a first-class durable record, written **before** any prediction
can reach the screen: `reveal_predictions()` writes `<set>/review-exposure.json` and only then
calls `_predictions()`.  A failed write refuses the reveal and leaves the flag `false` — the flag
may be pessimistic, never optimistic.  Session start reads the sidecar **or** any saved
annotation; `save()` re-reads both inside the cross-process write lock and records the OR.  The
flag is one-way and survives a restart, a crash and a failed save.

*Tests:* `test_reveal_is_durable_across_a_restart_before_any_save`,
`test_reveal_survives_a_failed_save`,
`test_predictions_are_withheld_when_the_exposure_cannot_be_recorded`,
`test_reveal_route_records_before_it_answers` (asserts the file is already on disk at the moment
`_predictions` is first called).

**P1-3 — offset undo discarded unrelated edits.**  Reproduced in the shipped script: `applyOffset`
pushed a deep copy of the whole `rows` array and `undo` restored it wholesale, so
apply-offset → correct an artist → undo silently reverted the correction; and `preview()`
re-seeded `rows` from its snapshot on every keystroke in the offset box, discarding anything typed
during a preview.

*Fixed* by making the offset own only what it writes.  `rows` is now mutated in place and never
replaced; `offsetBase` and `offsetUndo` hold only `{start, end}` pairs, and `restoreTimes()` puts
back only `start_ms_range` / `end_ms_range`.  Artist, title, confirmation and touch state are
outside the offset's reach.  Stamping a start while a preview is pending is refused with a
message rather than silently fighting the preview.

*Tests:* `test_shipped_js_offset_undo_keeps_unrelated_edits`,
`test_shipped_js_editing_during_a_preview_is_not_discarded` — both run the page's own script.

**P1-4 — a boundary preview could be impossible to save.**  Reproduced: a −60 s preview clamps
both endpoints of an early row to zero; the preview reports only "boundary value(s) clamped", and
the save then fails `reviewed_record`'s positive-audible-span check with no indication of which
row.

*Fixed* on both sides.  `collapsed_rows()` names the offending rows (1-based) using exactly the
rule the server enforces, and `preview_bulk_offset` returns them as a third value.  In the page,
`isCollapsed` marks those rows `no-span` and tints them, the preview message names every one of
them and says why they cannot be saved, `applyOffset` refuses to apply, and `save` refuses to send.

*Tests:* `test_collapsing_negative_offset_is_named_per_row_not_discovered_at_save` (Python side,
including the row-specific save refusal) and `test_shipped_js_blocks_a_collapsed_preview_with_a_row_reason`.

**P1-5 — concurrent review processes could overwrite each other.**  Reproduced: the session lock
was a plain `threading.Lock`, so two processes both passed the digest check; and
`_write_first_pass_atomically`'s rollback restored the annotation bytes it had read before the
attempt, which could sit on top of another writer's annotation.

*Fixed* by `truth_write_lock()` in `truth.py`: a reentrant, cross-process advisory lock per truth
record (`msvcrt.locking` on Windows, `fcntl.flock` elsewhere) over a lock file in the system temp
directory keyed by a digest of the record's resolved path — nothing is created beside the corpus
record, so locking never adds a file to a corpus set and never writes beneath `work/`.  The
session's digest re-check, the exposure re-read, `verify_truth`, the annotation replacement, the
truth replacement and the rollback now all run inside one hold of that lock, so the rollback can
only ever restore an annotation this writer actually displaced.  The second-pass and resolution
commits in `truth.py` take the same lock, so all three writers of a record serialise against each
other.  `_SAVE_LOCK_TIMEOUT` bounds the wait (20 s) and a conflict is reported, not hung on.

*Tests:* `test_second_process_cannot_commit_inside_the_first_writers_window` (a real second
process holds the lock; the save refuses and the record stays draft),
`test_concurrent_sessions_cannot_clobber_each_others_annotation`,
`test_write_lock_is_reentrant_within_one_process` (a non-reentrant lock would deadlock here).

**P1-6 — the prohibition on writing beneath `work/` was unenforced.**  Reproduced:
`--corpus work` was accepted and a truth record beneath the work tree was fully writable.  One
nuance worth recording: a symlinked *directory* was already unreachable, because `Path.rglob` does
not recurse into directory symlinks — but a symlinked *file* and a `--corpus` pointed straight at
`work/` were both writable.

*Fixed* by `reject_work_destination()`, comparing `os.path.normcase(os.path.realpath(...))` so
symlinks, junctions, substituted drives, Windows 8.3 short names and letter case all normalise to
one spelling.  It is applied to the resolved corpus root, to every candidate record found (before
the containment test, so a link out of the corpus and into `work/` is refused out loud rather than
quietly skipped) and again in `TruthReviewSession.__init__`.  `cli.py` gained `--work-root` and
passes it through; `idea truth review --corpus <work tree>` now exits 2 with
`refusing to review a truth record beneath the work tree: ... resolves inside ...`.

*Tests:* `test_corpus_beneath_the_work_root_is_refused`,
`test_relative_and_aliased_work_paths_are_refused`,
`test_symlinked_record_into_the_work_root_is_refused`,
`test_symlinked_directory_into_the_work_root_is_never_traversed`.

**P1-7 — the documented keyboard loop did not work.**  Reproduced: `E` focused the artist input
and nothing ever moved focus out of it.  `Enter` in a field confirmed the row but left focus and
the `editing` class in place, so the next `N`, `P` or `S` was typed into the artist box; `Escape`
was let through the input guard but had no branch at all, so it did nothing.  `Enter` in the
offset box confirmed a truth row.

*Fixed* with explicit focus transitions — see the map below.  `edit()` records the field values so
`Escape` can restore them; `endEdit()` drops the `editing` class and blurs; `confirmRow()` ends the
edit before advancing; `select()` ends any open edit.  The offset box has its own `Enter`
(apply the preview, leave the field) and `Escape` (leave the field, keep the preview).  `render()`
no longer overwrites the field the owner is typing in.

*Tests:* `test_shipped_js_keyboard_loop_edits_confirms_and_navigates` (E → type → Enter → N → P,
asserting focus returns to the body and `current` actually moves) and
`test_shipped_js_escape_discards_the_edit_and_leaves_the_field`.

**P1-8 — regressions that exercise the shipped JavaScript.**  The suite went from 10 tests to 32.
Six of them run the page's **real** inline script — extracted from the rendered page with
`scripts/check_page_js._inline_scripts`, not retyped — under Node against a small DOM stub, and
drive it with synthetic `keydown` and `input` events.  Injected-failure coverage now includes a
truth-replacement failure with an existing annotation present, an exposure-write failure, and a
second process holding the write lock.

**P1-9 — gates.**  All re-run in this environment; outputs pasted below.

### Checked independently of sol

* **Predictions absent from the served HTML while hidden.**  Verified against the response body,
  not the CSS: no prediction artist, title, tier or timestamp appears, and `_predictions` is never
  called at all while rendering the page (the test counts invocations).  The only occurrence of
  the words "our guess" in the initial HTML is the protective copy and the reveal checkbox label.
* **No one-click path accepts our answer as truth.**  Guesses are appended as separate
  `<tr class="guess">` rows with no handlers, and never populate a truth field; there is no
  accept-guess action.  `test_shipped_js_never_writes_a_guess_into_a_truth_row` asserts the guess
  rows carry zero event listeners and that no truth row's artist changed.
* **Untouched fields are preserved byte-for-byte.**  `occurrence_index`, `overlaps_with`,
  `version`, `in_reference_pool`, `note`, `second_pass_ref` and `disagreement_resolution` are
  carried by `model_copy`; `draft` transitions `true → false` exactly once, through
  `verify_truth`'s existing contract.  Asserted per episode.
* **Only the reviewed set's files are written.**  New test seeds a sibling corpus set and a work
  tree with a sentinel, saves, and asserts both are byte-identical afterwards and that the
  reviewed set's directory contains exactly `ground_truth.json` and `annotation-first.json`
  (plus `review-exposure.json` once predictions have been revealed).  `git status --short -- work
  data` is empty.
* **R-10 (new) — state-machine guard was incomplete.**  The session refused a set carrying
  `second_pass_ref`, but not one carrying `disagreement_resolution`, while `_truth_with_content`
  clears that field — so a resolved set could have had its resolution silently dropped.  Now
  refused: `truth review cannot reopen a resolved set`.  `draft → verified → second-pass →
  resolved → frozen` and the `GroundTruthRecord` schema are otherwise unchanged; this tool only
  ever performs the first transition.
* **CSRF / Origin / no-mutation-on-GET / traversal.**  Hold.  Every mutating route is POST behind
  `_cross_site()` (loopback `Host`, same-origin `Origin`) plus a constant-time token check; GET
  serves only the page, the token and the one exact audio path `/media/<media_key>/audio`
  (string equality, so `%2e%2e` traversal 404s); `--set` is regex-gated and then matched against
  record *content*, never used as a path.  **R-11 (new hardening):** the `/csrf` GET now applies
  the same `Host` check, so a DNS-rebound page cannot even collect the token.  Tested.
* **`scripts/check_page_js.py` was extended, not weakened.**  It still renders every page type as
  the server renders it and hands every inline script to `node --check`; the truth-review page was
  added to the list (52 → 53 scripts across 21 → 22 renders).  No skip, no filter, no
  hand-assembled payload.
* **`scripts/audit_fixtures.py` passes on the fixture corpus after a save.**  The audit test now
  reveals predictions first, so the new `review-exposure.json` is audited too; no URL, handle or
  long numeric id enters any file this tool writes.

### What changed

| File | Change |
|---|---|
| `src/id_detector/truth_review.py` | `reconciled_role_segments`, `collapsed_rows`, `preview_bulk_offset` returns collapsed rows, `review-exposure.json` helpers, `_real` / `reject_work_destination`, `find_truth_path(work_root=…)`, resolved-set guard, locked `save`, `_SAVE_LOCK_TIMEOUT`, `/csrf` Host check, rewritten page script, collapsed-row CSS, expanded help overlay |
| `src/id_detector/truth.py` | `truth_write_lock` / `_TruthLock` (reentrant, cross-process), `_replace_first_pass` with rollback inside the lock, second-pass and resolution commits under the same lock |
| `src/id_detector/cli.py` | `--work-root` on `truth review`, work-tree destination refused (exit 2) |
| `tests/test_truth_review.py` | 10 → 32 tests, six driving the shipped script under Node |
| `docs/reviews/build-truth-review.md` | this appendix |

`src/id_detector/playlists/**`, `tests/test_playlists.py`, `README.md`, `idea.cmd`,
`src/id_detector/present/theme.py`, `docs/PLAN-v2.md`, `profiles/`, `data/` and `work/` were not
touched.

### Keyboard map as implemented

| Key | Action |
|---|---|
| `Space` | Play / pause (ignored while typing in a field) |
| `J` / `L` | Seek −5 s / +5 s |
| `Shift+J` / `Shift+L` | Seek −30 s / +30 s |
| `E` | Edit the current row's artist / title; focus lands in the artist field |
| `Enter` (in a row field) | Commit the edit, leave the field, confirm the row, advance to the next |
| `Enter` (on a row, not typing) | Confirm the current row and advance |
| `Enter` (in the offset box) | Apply the previewed offset and leave the field |
| `Escape` (in a row field) | Discard this edit, restore the field, leave the field; selection unchanged |
| `Escape` (in the offset box) | Leave the field; the preview is kept |
| `Escape` (help open) | Close the help overlay |
| `Escape` (otherwise) | Clear the message line |
| `T` | Stamp the current row's start from the playhead (refused while an offset preview is pending) |
| `N` / `P` | Select next / previous row |
| `S` | Save the complete first pass |
| `?` | Open / close the help overlay containing this map |

Clicking a row still selects it and seeks to its claimed start.  Navigation, editing,
confirmation, stamping, offset preview, apply and undo remain browser-local.

### Provenance actually persisted, and when

1. **The moment predictions are revealed — before any prediction is returned** —
   `<set>/review-exposure.json`:

   ```json
   {
     "schema_version": "1.0.0",
     "generated_by": "id-detector/0.1.0",
     "set_id": "<set id>",
     "predictions_visible_during_review": true,
     "first_revealed_at_utc": "<ISO-8601 UTC, Z>",
     "tool": "idea truth review"
   }
   ```

2. **On an explicit save, inside the cross-process write lock** — `annotation-first.json`'s
   `review_provenance`:

   ```json
   {
     "predictions_visible_during_review": false,
     "tool": "idea truth review",
     "version": "0.1.0",
     "reviewed_at_utc": "<ISO-8601 UTC, Z>"
   }
   ```

   `predictions_visible_during_review` is the OR of the sidecar, any prior annotation, and this
   session, so it can only ever move from `false` to `true`.

Nothing is written on navigation, editing, preview, apply or undo.

### Could not fix in scope (P2)

1. **A first pass is all-or-nothing.**  Every still-draft row must be confirmed in one browser
   session before Save is accepted — 63 rows in a single sitting for
   `release1-mph-youtube-set`, with only the `beforeunload` prompt protecting the work.  Fixing it
   means inventing a partial/resumable review state in the corpus, which is a contract change and
   a different cycle.
2. **`truth.py`'s older interactive seed-review path still writes a single `dominant` span**
   (the same fabrication as P0-1, in the pre-existing `idea truth verify` command).  Left alone:
   it is not this cycle's code and changing it alters a shipped CLI's output.
3. **`verify_second_pass` and `resolve_truth` read their inputs before taking the write lock**;
   only their commit region is serialised.  Closing that read-check-write window needs those
   functions restructured around their interactive prompts.
4. **Lock files in the system temp directory are never removed** — one small empty file per
   reviewed record.  Deleting one races with another process acquiring it.

### Gate outputs

#### `uv run pytest -q`

```text
........................................................................ [ 96%]
..............................................                           [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
1198 passed, 93 deselected, 1 warning in 659.80s (0:10:59)
```

#### `uv run pytest tests/test_truth_review.py -q`

```text
................................                                         [100%]
32 passed in 8.04s
```

#### Compatibility suites

`uv run pytest tests/test_stage2a_truth.py tests/test_score_corpus.py tests/test_playlists.py
tests/test_phase0a_security.py tests/test_phase3a_honesty.py tests/test_golden_local_free.py
tests/test_stage7_server.py tests/idea_web/test_worker.py tests/test_service_api.py -q`:

```text
import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
189 passed, 1 warning in 126.68s (0:02:06)
```

#### `uv run python scripts/check_page_js.py`

```text
page JavaScript check passed: 53 inline scripts across 22 page renders
```

#### `uv run ruff check .`

```text
All checks passed!
```

#### `uv run ruff format --check .`

```text
286 files already formatted
```

#### `uv run python scripts/audit_fixtures.py`

```text
audited 455 files
fixture audit passed
```

#### `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

Exit code 0.

#### `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/gate_local_mode.ps1`

```text
complete; 6 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes
prepared offline cached mix e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d
local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok
Evidence: C:\Users\natha\AppData\Local\Temp\idea-local-gate-f1db1c95466c4c42a2f71a0864c10ba1
```

Exit code 0, first attempt — the `taskkill` cleanup race the build report hit did not recur.

#### Repository state

`git status --short -- work data` is **empty**.  `git status --short`:

```text
 M docs/reviews/README.md
 M scripts/check_page_js.py
 M src/id_detector/cli.py
 M src/id_detector/present/index.py
 M src/id_detector/truth.py
?? docs/reviews/build-truth-review.md
?? src/id_detector/truth_review.py
?? tests/fixtures/truth-review/
?? tests/test_truth_review.py
```

Not committed, branched or pushed.  No live provider call, no real URL analysis, no write to the
real corpus, no `idea gc` against the real work tree.

REVIEW + FIX: COMPLETE

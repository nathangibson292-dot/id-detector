# Build review — 4a-i Service API and packaging

## Changes

- `src/id_detector/service.py` — added the public typed service seam, local checkpoint adapter, target types, result projection, resume money restoration, and local pipeline options adapter.
- `src/id_detector/cli.py` — made `idea analyse` a thin `service.run` caller and added the minimum engine hooks needed for caller-owned IDs, checkpoints, resume, progress/cancellation, and injected journals/admitters.
- `src/id_detector/paid_clip.py` — accepts the service's injected attempt journal; this small change outside the post-Phase-3 list is necessary for the contract's `attempt_journal` to govern the real paid dispatch path rather than a parallel imitation.
- `src/id_detector/webapp/runner.py` — now constructs a typed request and calls `service.run`, as required, while retaining its existing job/result/acquisition behavior.
- `src/idea_web/__init__.py` — added the real future web package marker and package version only; there is no FastAPI app, route, template, or new dependency.
- `pyproject.toml` — packages both `src/id_detector` and `src/idea_web` in the wheel.
- `tests/test_service_api.py` — added the complete 4a-i seam, checkpoint, resume, cancellation, result/journal, hosted-path refusal, and CLI-parity gate coverage.
- `docs/reviews/build-4a-i.md` — this build record.

No frozen profile, corpus file, playlist-owned file, `README.md`, `idea.cmd`, theme file, or `present/server.py` was changed. No dependency was added. `uv sync` completed after the package configuration changed.

## Public definitions

The exact request definition is:

```python
@dataclass(frozen=True)
class RunRequest:
    run_id: str
    analysis_key: str
    target: PlatformUrl | UploadId | LocalPath
    recipe: Recipe
    accept_degraded: bool
    hints_snapshot_policy: str
    manual_tracklist: bytes | None
    checkpoint_store: CheckpointStore
    attempt_journal: AttemptJournal | None
    usd_admitter: UsdAdmitter | None
    progress: Progress | None
    cancel_token: CancelToken | None
```

The exact result definition is:

```python
@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: str
    reason: str | None
    achieved: str | None
    bundle_id: str | None
    usd_e6_reserved: int
    usd_e6_spent: int
    attempts: int
```

`PlatformUrl(url: str)`, `UploadId(upload_id: str)`, and `LocalPath(path: Path)` are frozen wrappers, so the service never accepts a raw string target. `LocalPath` is refused unless the injected checkpoint store declares local mode. The local `UploadId` adapter resolves only beneath `<work-root>/.uploads/` and rejects missing or escaping IDs.

The plan does not put configuration or a work root in `RunRequest`. I chose the smallest non-expanding interpretation: those execution details live on `LocalCheckpointStore` as `PipelineOptions`, while all host-neutral inputs remain exactly the listed request fields. Local callers pass `analysis_key=""` so the existing canonical intake calculation remains authoritative; a worker that already performed intake may supply its computed key.

## Checkpoint semantics

The only phase names, in order, are:

```text
ingest, decode, windows, primary, hints, fuse1, secondary, fuse2, present
```

`LocalCheckpointStore` atomically writes `<work-root>/.checkpoints/<run_id>.json`. A write first verifies every referenced artefact with the repository's Windows-long-path-safe file check. The engine calls it only after the phase writer has returned: completion sidecars exist for ingest/decode/windows/hints/Free recognition; the paid observation file exists for primary; fuse files exist for both fuse checkpoints; and the sealed bundle manifest exists before `present` is recorded.

Completed decode and windows phases are loaded directly. A completed Deep `primary` is reconstructed from the checkpointed immutable observation file and saved counts, and its saved reservation/spend is surfaced through a resumed admitter. It never calls the AudD adapter. Other downstream generators retain their existing content-addressed/cache-safe behavior. Cancellation during paid primary lets in-flight attempts resolve and settle, journals `cancelled`, and leaves only earlier completed phases, so the same request can resume.

## Tests added

`tests/test_service_api.py` covers:

- the dataclass field lists exactly matching §4.3;
- a full local service run and a CLI run selecting the same immutable bundle;
- a caller-supplied `run_id` in the result, journal, manifest, and bundle identity;
- hosted `LocalPath` refusal;
- all nine checkpoint writes in exact order, with artefacts already present at every write;
- resume from `primary` with `FakeAudD.calls == 0`;
- cancellation during the paid primary leaving `ingest`, `decode`, and `windows` complete but not `primary`;
- `RunResult` equality with journal status, reason, achieved recipe, reservation, spend, and paid-attempt count.

## Playlist guarantees

The second session's playlist files were not edited. The result publication path remains the existing `publish_result` path, so `row_actions_html`, both `PLAYLIST_CSS`/`PLAYLIST_JS` injection points, shown-row-only controls (including hidden `file://` rows), row IDs/data attributes, and the `/{source_key}/{media_key}/present/index.html` URL shape are untouched. `tests/test_playlists.py` passed unmodified as part of the 276-test neighboring gate and the full suite.

## Verification outputs

`uv run pytest -q`

```text
1126 passed, 93 deselected, 1 warning in 621.00s (0:10:20)
```

The warning is the pre-existing Python 3.13 removal warning for stdlib `audioop`, emitted by `pydub`.

`uv run ruff check .`

```text
All checks passed!
```

`uv run ruff format --check .`

```text
273 files already formatted
```

`uv run python scripts/audit_fixtures.py`

```text
audited 450 files
fixture audit passed
```

`uv run pytest tests/test_service_api.py -q`

```text
7 passed, 1 warning in 26.71s
```

`uv build`

```text
Building source distribution...
Building wheel from source distribution...
Successfully built dist\id_detector-0.1.0.tar.gz
Successfully built dist\id_detector-0.1.0-py3-none-any.whl
```

The exact wheel assertion completed silently with exit code 0:

```text
uv run python -c "import zipfile,glob;z=zipfile.ZipFile(glob.glob('dist/*.whl')[0]);assert any(n.startswith('idea_web/') for n in z.namelist())"
```

An additional installed-entry-point check, `uv run idea --help`, printed the normal command help and exited 0.

The named neighboring gate:

```text
276 passed, 1 warning in 260.33s (0:04:20)
```

`uv run python scripts/check_page_js.py`

```text
page JavaScript check passed: 52 inline scripts across 21 page renders
```

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/gate_local_mode.ps1`

```text
complete; 6 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=<temporary local gate bundle>/tracklist.json
prepared offline cached mix e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d
local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok
Evidence: C:\Users\natha\AppData\Local\Temp\idea-local-gate-b1f88448c2504e13a6ee75f400ab266d
```

Nothing in scope could not be completed.

`git status --short`

```text
 M pyproject.toml
 M src/id_detector/cli.py
 M src/id_detector/paid_clip.py
 M src/id_detector/webapp/runner.py
?? docs/reviews/build-4a-i.md
?? src/id_detector/service.py
?? src/idea_web/
?? tests/test_service_api.py
```

`git diff --stat`

```text
pyproject.toml                   |   2 +-
src/id_detector/cli.py           | 240 +++++++++++++++++++++++++++++++++------
src/id_detector/paid_clip.py     |   3 +-
src/id_detector/webapp/runner.py |  59 +++++++---
4 files changed, 251 insertions(+), 53 deletions(-)
```

As expected, Git's unstaged `--stat` output does not include the four untracked deliverables listed by status. `git status --short -- work data` produced no output. The protected-file diff audit also produced no output.

BUILD: COMPLETE

## Review + fix pass (sol xhigh review folded in)

Reviewer: Opus 5, adversarial review-then-fix. Every finding below was re-verified in the working
tree before it was fixed, and every gate was re-run here (nothing is quoted from the build report).
Sol's correction is confirmed: `docs/reviews/build-4a-i.md` did end with `BUILD: COMPLETE`.

### Findings

**P0-1 — outcome handling had several paths and the extraction was half-done. UPHELD.**
`service.run` called back into `cli._analyse` (`service.py:395` as reviewed); the CLI mapped status →
exit code at `cli.py:1929` and the runner mapped it again at `webapp/runner.py:276`; the runner then
found "this run's" journal entry by wall-clock proximity (`webapp/runner.py:43-69`, used at `:94`),
whose one-second tolerance could hand an earlier run's spend to a cache hit; and ~1,040 lines of
pipeline still lived in `cli.py`, which plan §4.2 forbids after Phase 3.

*Fixed, in full.* The pipeline now lives in a new service-owned module, `src/id_detector/pipeline.py`
(`run_analysis`, plus `_report` / `_windows_in_spans` / `_settle_money` / `_money_journal_fields` /
`_achieved` / `_run_status` / `PROJECT_ROOT` / `_PAID_ENGINES`). `cli.py` fell from 2,977 to 1,788
lines and holds no pipeline; it re-exports the moved helpers (`cli.py:48-59`) so `idea rescan`,
`idea acquire`, `scripts/make_golden.py` and the gate scripts are unchanged. `service.run` drives
`pipeline.run_analysis` directly. Outcome reporting is now one object: `pipeline.PipelineOutcome`
(`pipeline.py:206-227`) is filled in by `_finish` at every terminal path — waiting, served-compatible,
budget_exhausted, provider_unavailable, success, source_changed, cancelled, failed — and
`service.run` builds the `RunResult` from it, reading no journal at all. Exit mapping is
`service.STATUS_EXIT_CODES` + `service.exit_code_for` (`service.py:64-77`, `:300`); both callers call
it and neither carries a status table. The runner takes status, reason and spend from the
`RunResult` (`webapp/runner.py:80-112`, `:286-300`) and reads the journal only for the last completed
stage, matched on the run's own id (`_entry_by_run_id`), so the clock heuristic is gone.

Two further defects fell out of doing the extraction properly, both fixed:

* a **served compatible result reported the stored bundle's money as this run's spend**. A cache hit
  spends nothing; it now reports the served result's status/reason/achieved with spend 0
  (`pipeline.py`, the `compatible is not None` branch).
* the builder's runner turned a **cancelled job into a failed one**: `service.run` suppresses the
  `CancelledError` the pipeline journalled, so exit 130 fell through to `RuntimeError`. The runner
  now re-raises a cancellation for the job manager (`webapp/runner.py`, before the waiting check).

**P0-2 — the hosted local-path refusal was bypassable. UPHELD.** `PlatformUrl.url` was returned
unvalidated and `ingest._load_cached`/`ingest` treat an existing path as local audio, so a hosted
store accepted an absolute fixture path as a `"platform"` analysis; `UploadId` had containment
checking but no opaque-id validation. *Fixed at the boundary* — `service.validate_platform_url` and
`service.validate_upload_id` run inside `_target_value`, before any resolution or ingestion: an
http/https scheme allowlist, a real host, no credentials, no UNC (`\\…`, `//…`), no `file:`, no drive
letter, no control characters, and a refusal if the string resolves to an existing path; upload ids
must be short opaque `[A-Za-z0-9_-]` tokens, and `resolve_upload` keeps its containment check.
Refusals raise `service.TargetRefused` (a `ValueError`, so existing callers are unaffected).

**P0-3 — an interrupted primary could re-bill and lose its earlier spend. UPHELD.** Restoration ran
only after a completed `primary` checkpoint; otherwise a fresh `UsdAdmitter` was created, the sweep
re-queried cached `no_match` bodies under the default `refresh_states` (`paid_clip.py:516`) and
re-dispatched the same clips, so a resumed run paid twice and settled only the second pass's spend.
*Fixed.* Before any paid request the pipeline folds the attempt journal into
`service.recover_paid_attempts` and pre-charges the recovered units to the admitter with
`service.charge_restored_units`, against the ORIGINAL reservation; the sweep never re-dispatches a
clip this run already resolved (`paid_clip.py`, `already_resolved`), reading its cached body
regardless of `refresh`/`refresh_states`, and counting it as `recovered_resolved`. A reservation that
cannot even cover the recovered spend stops the primary as `reservation_exhausted` instead of
dispatching.

**P0-4 — the injected journal was not the recovery authority. UPHELD.** `paid_clip.py:415` read
`attempts_path(media_dir)` while writing to the injected journal at `:416`. *Fixed:* the ledger and
the recovery both read `journal.path`, so the journal that is written is the journal that is read.

**P1-5 — a cancelled primary was not resumable. UPHELD.** Cancellation wrote partial observations
with no `primary` checkpoint, and the resume reused `live-audd-clip-<run_id[:12]>`, whose
`observations.gen0.jsonl` is immutable — so the resume died of `FileExistsError` *after* paying for
its provider calls. *Fixed:* `paid_clip._invocation_dir` gives each pass its own directory
(`…-r1`, `…-r2`), and because the recovered clips are re-derived from the content-addressed raw
cache the resumed file is a complete superset.

**P1-6 — retained results failed after pruning. UPHELD.** The ingest checkpoint required
`ingested.original_path`, which retention prunes while the retained result stays servable, so a
post-retention cache hit failed before the compatibility lookup. *Fixed:* the ingest checkpoint names
the durable source record and includes the original only when it is still on disk.

**P1-7 — completed downstream phases re-ran. UPHELD.** Only decode, windows and the paid primary had
recovery branches. *Fixed:* the Free primary is restored from its checkpointed observation file and
counts (requests, physical attempts, failures, cache hits) exactly as the paid one is; a completed
`secondary` restores the secondary and local-index observation paths, its allocation/resolution
counts and its blocked reason, then re-fuses with that evidence and skips both the probe and the
index block — so a restart with an open breaker no longer discards a finished secondary and degrades
the result. Artefact references are stored media-relative (`_reference`), and `state()` is now part of
the `CheckpointStore` protocol and called directly, not discovered with `getattr`.

**P1-8 — degraded Free work could resume as achieved Deep. UPHELD.** *Fixed:* the primary checkpoint
records the substitution — `achieved`, `paid_first`, `degrade_reason`, `primary_planned` — and
recovery restores it before the branch is chosen, so a degraded Free pass resumes as Free.

**P1-9 — a lowered cap could erase restored money. UPHELD.** *Fixed:* the recovered spend is computed
before `reserve_usd`, and a `BudgetExhausted` refusal journals `service.restored_settlement(...)`,
which reports that spend instead of zero.

**P1-10 — gates.** Re-run here in a writable environment, including a FRESH `uv build` wheel
installed into an isolated venv, whose `idea --help` exits 0 and whose `idea_web`,
`id_detector.pipeline` and `id_detector.service` all import.

### Final state of the extraction

`src/id_detector/pipeline.py` (new, service-owned) holds the whole analysis pipeline: ingest, decode,
windows, the paid and free primaries, hints, both fuses, the secondary and the local index, the
status rule, money settlement, journalling, the nine checkpoints and every recovery branch.
`src/id_detector/service.py` holds the §4.3 request/result types, target validation, the local
checkpoint store, recovery (money, resolved paid work, phase state) and the single status → exit-code
mapping. `src/id_detector/cli.py` is Typer wiring: it builds a `RunRequest`, calls `service.run` and
raises `typer.Exit(service.exit_code_for(result))`; `webapp/runner.py` does the same and renders the
result onto the job. Why this split: the 3a-i four-tracklists defect came from two callers walking
two paths; with one implementation, one outcome object and one exit mapping there is no second path
to drift. `paid_clip.py` keeps a small, necessary change (the injected journal as both writer and
recovery authority, same-run resolved-clip recovery, per-pass invocation directory) because the
contract's `attempt_journal` has to govern the real dispatch path — recorded here as a deliberate,
minimal step outside §4.2's post-Phase-3 list, as the builder recorded its own.

`RunResult.attempts` reports the dispatches *this pass* made; the attempt journal remains the
cumulative authority (a resumed run's earlier attempts are in it, and its spend is in
`usd_e6_spent`).

### Tests added (all in `tests/test_service_api.py`, deterministic fakes only)

* `test_a_platform_url_can_never_name_a_filesystem_path` — 13 refused targets (absolute path, `file:`
  URI, UNC in both spellings, drive letter, `/etc/passwd`, traversal, `ftp:`, credentials, no host,
  empty, whitespace) plus one accepted https URL. (P0-2)
* `test_an_upload_id_must_be_an_opaque_token` — 7 refused ids; a minted id resolves inside
  `.uploads`; an unknown opaque id is refused. (P0-2)
* `test_the_service_is_the_only_place_a_status_becomes_an_exit_code` — every status maps through
  `exit_code_for`, an unknown status is 1, neither caller carries a status table or pipeline
  machinery, and `cli.py` has no `_analyse`. (P0-1)
* `test_the_web_runner_reports_the_services_spend_not_a_journal_lookup` (P0-1) and
  `test_a_served_compatible_result_reports_this_runs_zero_spend` — a cache hit reports the stored
  status with spend 0, zero AudD calls and no journal entry of its own. (P0-1)
* `test_a_crashed_primary_resumes_without_re_billing_and_keeps_its_spend` — a REAL crash (the
  progress hook dies after 4 of 7 paid clips), then a resume: `FakeAudD.calls` on the resume is
  exactly the unfinished remainder, and the settled spend is 7 units in total, once. (P0-3)
* `test_recovery_reads_the_injected_attempt_journal` — the journal is outside the media tree, the
  per-media default is never created, and recovery still finds the run's billed units and issues
  zero AudD calls for them. (P0-4)
* `test_a_lowered_cap_refuses_the_resume_without_erasing_its_spend` — `budget_exhausted`, zero new
  AudD calls, and the crashed pass's spend in both the `RunResult` and the journal. (P1-9)
* `test_a_cancelled_primary_resumes_instead_of_colliding_with_its_own_artefacts` — REAL cancellation
  through the cancel token, then a resume that completes, pays for the remainder only, and leaves two
  invocation directories (`…`, `…-r1`). (P1-5)
* `test_a_retained_result_is_served_after_retention_pruned_the_original` — the original audio is
  deleted, the retained bundle is still served, zero Shazam requests. (P1-6)
* `test_a_resumed_free_run_re_recognises_nothing` — a real crash entering `fuse`, then a resume with
  `FakeShazamHTTP.requests == 0`. (P1-7)
* `test_a_completed_secondary_is_restored_and_an_open_breaker_cannot_degrade_it` — a real crash
  entering `present`, then a resume with the breaker open: zero Shazam requests, zero AudD calls, and
  the status is not `degraded`. (P1-7)
* `test_a_degraded_free_primary_resumes_as_free_and_never_as_deep` — AudD refuses the credential,
  `--allow-degrade` substitutes Free, a real crash, and the resume stays `achieved=free` with zero
  AudD calls and zero spend. (P1-8)

Zero-AudD-on-resume is asserted on the fake adapter's call count everywhere, never on log text.

Existing tests were repointed, not weakened: 14 files now patch `pipeline.run_analysis` (and
`pipeline.run_generation_loop` / `_load_cached` / `reserve_usd` / `append_invocation` / `ingest` /
`decode` / `run_hints` / `find_result`) where they patched `cli`. One of those,
`tests/test_stage4d_profiles.py`, was patching a name the CLI no longer used and had started making
a real network call — a stale patch that would have masked a live-provider path.
`tests/test_phase3a_honesty.py` now exercises `_entry_by_run_id` (match on the run's own id) in place
of the deleted clock heuristic, with the same U-F15 guarantee.

### The five playlist contract points

None of the second session's files was touched (`git status --short` on
`src/id_detector/playlists`, `tests/test_playlists.py`, `README.md`, `idea.cmd`,
`src/id_detector/present/theme.py` is empty). Result publication still goes through the one
`present.bundles.publish_result` call, moved verbatim with the pipeline, so `row_actions_html`, both
`PLAYLIST_CSS` / `PLAYLIST_JS` injection points, shown-row-only controls (hidden `file://` rows
included), the row ids/data attributes and the
`/{source_key}/{media_key}/present/index.html` URL shape are unchanged. `tests/test_playlists.py`
passes unmodified.

### Gate outputs (all re-run here, on the final tree)

`IDEA_TEST_MODE=1 uv run pytest -q`

```text
1139 passed, 93 deselected, 1 warning in 679.24s (0:11:19)
```

The one warning is the pre-existing stdlib `audioop` removal warning from `pydub`.

`uv run pytest tests/test_service_api.py -q`

```text
20 passed, 1 warning in 68.09s
```

The named neighbouring gate (`test_phase0a_money`, `test_phase0a_status`, `test_phase0a_crash_cache`,
`test_phase0b_attempts`, `test_phase1a_compat`, `test_phase1b_breaker_scorer`, `test_phase2b_retention`,
`test_phase3a_honesty`, `test_projection`, `test_playlists`, `test_golden_local_free`,
`test_stage10_webapp`, `test_service_api`)

```text
309 passed, 1 warning in 361.57s (0:06:01)
```

`uv build`

```text
Building source distribution...
Building wheel from source distribution...
Successfully built dist\id_detector-0.1.0.tar.gz
Successfully built dist\id_detector-0.1.0-py3-none-any.whl
```

`uv run python -c "import zipfile,glob;z=zipfile.ZipFile(glob.glob('dist/*.whl')[0]);assert any(n.startswith('idea_web/') for n in z.namelist())"`

```text
idea_web packaged ok
```

The FRESH wheel installed into an isolated `uv venv` (P1-10: not the pre-existing one):

```text
uv pip install dist/id_detector-0.1.0-py3-none-any.whl   ->  + yt-dlp==2026.8.19 (etc.)
<isoenv>/Scripts/idea.exe --help   ->  "Usage: idea [OPTIONS] COMMAND [ARGS]..."  EXIT=0
<isoenv>/Scripts/python.exe -c "import idea_web, id_detector.pipeline, id_detector.service"
    idea_web 0.1.0
    pipeline+service import ok
```

`uv run python scripts/check_page_js.py`

```text
page JavaScript check passed: 52 inline scripts across 21 page renders
```

`uv run ruff check .`

```text
All checks passed!
```

`uv run ruff format --check .`

```text
274 files already formatted
```

`uv run python scripts/audit_fixtures.py`

```text
audited 450 files
fixture audit passed
```

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/gate_local_mode.ps1` (`present/server.py`
is still the local server and still works; it is retired in 4a-ii, not here)

```text
complete; 6 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=...\present\bundles\5fbc3aa88e58f2edb4d734e111b055378f1276e4549005c9ac18fda984068cfe\tracklist.json
prepared offline cached mix e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d
local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok
Evidence: C:\Users\natha\AppData\Local\Temp\idea-local-gate-3a3073d7197749bdaa68b8029b24232a
```

`git status --short -- work data`

```text
(empty)
```

`git status --short` on the second session's files (`src/id_detector/playlists`,
`tests/test_playlists.py`, `README.md`, `idea.cmd`, `src/id_detector/present/theme.py`) and on
`docs/PLAN-v2.md` and `profiles/`

```text
(empty)
```

`PYTHONIOENCODING=utf-8 uv run python scripts/score_corpus.py --run-list data/local/release-1-runs-free.json --out "$TEMP/4a-i-check.json"`

```text
scored 7 mix(es) [draft truth, matched by work]: likely 8971/10000, listed n/a, recall 6422/10000
```

Unchanged from the release-1 pooled expectation (likely 8971 / recall 6422): this cycle moved code and
added recovery, and it moved no accuracy.

`git diff --stat` plus the untracked deliverables

```text
 pyproject.toml                       |    2 +-
 src/id_detector/cli.py               | 1131 ++--------------------------------
 src/id_detector/paid_clip.py         |   60 +-
 src/id_detector/webapp/runner.py     |  166 +++--
 tests/... (14 test files repointed)  |  110 +--
 19 files changed, 283 insertions(+), 1186 deletions(-)
?? docs/reviews/build-4a-i.md
?? src/id_detector/pipeline.py
?? src/id_detector/service.py
?? src/idea_web/
?? tests/test_service_api.py
```

Nothing was committed, branched or pushed.

REVIEW: OK_TO_COMMIT

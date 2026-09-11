# Build 1a-i + 1a-iii

Implemented the two requested cycles in one uncommitted working-tree change. No branches,
commits, pushes, dependencies, retention policy, compatibility rules, engine changes, or live
provider calls were introduced. `docs/PLAN-v2.md`, frozen profiles, and `data/corpus/` are unchanged.

## Files changed

- `src/id_detector/present/bundles.py`: canonical bundle IDs, sealed manifests, immutable fuse snapshots, presentation revisions, complete-run pointer selection, and result-path resolution.
- `src/id_detector/present/index.py`: atomic, rebuildable media index with legacy entries, stale/missing recovery, path validation, and in-memory fallback if index writing fails.
- `src/id_detector/present/exports.py`: optional destination directory so bundle exports are written together.
- `src/id_detector/present/page.py`: optional bundle destination and `/media/<media_key>/audio`; `PAGE_VERSION` 18 ? 19 because the HTML audio URL changes.
- `src/id_detector/present/refresh.py`: renders from frozen run inputs into a new bundle, preserving every legacy file and prior bundle.
- `src/id_detector/present/server.py`: resolves bundle/legacy library results, refreshes through the publication path, and serves traversal-safe audio using existing range/HEAD support.
- `src/id_detector/ingest.py`: separates presentation cached-open from ingest's original-byte verification; the CLI-imported `_load_cached` works without original audio.
- `src/id_detector/cli.py`: publishes analysis and acquisition results as bundles before journal references.
- `src/id_detector/webapp/runner.py`: delivers the initiating run's bundle, including partial/degraded results and subsequent acquisition revisions.
- `src/id_detector/io.py`: explicit directory fsync helper and Windows write-through atomic file replacement.
- `src/id_detector/journal.py`: directory durability after journal replacement; explicit null bundle/fuse references until publication succeeds.
- `src/id_detector/contracts.py`: nullable bundle/fuse references with legacy journal parsing.
- `docs/schemas/invocation_journal_entry.schema.json`: updates the journal schema to match the contract.
- `tests/golden/invocation_journal_entry.json`: adds explicit null bundle/fuse fields to the journal golden.
- `scripts/make_golden.py`: resolves the published bundle's tracklist without changing semantic comparison strictness.
- `idea.cmd`: forwards optional serve arguments, retaining normal double-click behavior.
- `scripts/gate_local_mode.ps1`: launches `idea.cmd` against an isolated offline fixture, opens its cached result in headless Edge/Chrome, and requires the probe's `audio-ok` after metadata and seeking.
- `tests/test_phase1a_bundles.py`: the immutable-bundle phase tests and publication/refresh regressions.
- `tests/test_phase1a_cached_open.py`: the cached-open/audio/index phase tests and integration regressions.
- `tests/test_phase0a_crash_cache.py`: follows published bundle paths in existing assertions.
- `tests/test_phase0a_status.py`: follows the initiating run's bundle, preserving partial/degraded assertions.
- `tests/test_phase0b_config.py`: resolves the bundle tracklist in the existing pipeline assertion.
- `tests/test_phase1b_fusion.py`: resolves bundle exports; fusion and comparison logic are unchanged.
- `tests/test_phase1b_targeting.py`: resolves the initiating degraded run's export.
- `tests/test_stage7_server.py`: expects refresh to preserve legacy bytes and serve a new bundle.
- `docs/reviews/build-1a-i-1a-iii.md`: this report and the command transcripts.

## Added tests

`tests/test_phase1a_bundles.py` covers canonical identity; exhaustive file/hash/size manifests;
refresh after removal of mutable fuse inputs; second-bundle creation and identical-input reuse;
newest-complete selection against partial runs and older refreshes; injected failure after file
and directory barriers but before the pointer; successful pipeline journal publication order;
failed pipeline journals without dangling bundle references; pointer-free shared publication;
carrying an existing analysis key; refusal to overwrite damaged bundles; legacy journal parsing;
changed preference revisions; acquisition revisions and the web job's selected result.

`tests/test_phase1a_cached_open.py` covers cached-open after deleting the retained original with
no PCM metadata; result HTML audio routing; index deletion, malformed JSON, stale signatures,
and missing entries; read-only legacy library/CLI opening; full, bounded, suffix and open-ended
ranges, HEAD and 416; unknown keys and traversal; an original path escaping its media directory;
deleted local-input lookup; recovery without `current`; the web runner's bundle result path;
and successful cached-open when the rebuilt index cannot be written. The hashed fixture paths
exercise Windows extended-length paths without shortening the normal work layout.

## Contract choices and reliable paths

- `bundle_id` is SHA-256 of canonical UTF-8 JSON `[run_id, presentation_version]`, sorted keys,
  compact separators, no newline, with an integer presentation version. The code documents this
  exactly where the ID is computed.
- `PAGE_VERSION` stamps the HTML build; the initial presentation revision starts at that value.
  Changed presentation inputs (including settings/acquisition or a new HTML build) advance the
  per-run presentation revision when an ID would collide. Identical inputs reuse the sealed
  bundle. This resolves the same-version refresh ambiguity while preserving both immutability
  and existing acquisition behavior. A `render_key` detects equality; it is not an analysis key
  and is not an input to `bundle_id`.
- `analysis_key` is copied from supplied/journal metadata when available, otherwise null;
  the one-line code comment explicitly leaves key construction to 1a-ii.
- `present/current` is an atomically replaced UTF-8 text file containing a bundle ID, without
  symlink/admin requirements. It selects the newest complete run by start time, then run ID
  for ties, then that run's presentation revision. Partial/degraded bundles are available to
  their initiating job but cannot replace this pointer. Shared publication accepts `local=False`
  and requires neither `current` nor the work index.
- A historical run with a usable invocation journal retains that invocation ID; a legacy mix
  without one uses deterministic `legacy-<media_key>` when refreshed. Its old files are untouched.
- Durable result paths are `work/<source_key>/<media_key>/present/bundles/<bundle_id>/index.html`,
  sibling `tracklist.json`, `.md`, `.cue`, `.m3u`, `source.json`, optional `acquire.json`, and
  `manifest.json`. Source metadata and duration permit opening/refreshing without original/PCM.
  Every manifest lists each other file with SHA-256 and byte size; it excludes itself to avoid a
  self-hash. No artifact scheduled for a later deletion cycle was deleted.
- Frozen analysis paths are `work/<source_key>/<media_key>/fuse/runs/<run_id>/...`, with a manifest
  and a frozen `presentation-identities.json`. Existing `fuse/episodes.json`, generation files,
  identities and their siblings remain the newest-run working copies. Existing benchmark and
  scorer readers may continue using those flat paths. All seven episode paths in
  `data/local/release-1-runs-free.json` were checked read-only: **7 checked; 0 missing**.
- Legacy `present/index.html` and exports remain readable. Existing local HTTP aliases
  `/<source_key>/<media_key>/present/<filename>` resolve to the selected bundle when available;
  explicit bundle URLs remain immutable. On-disk consumers of new presentation results use the
  shared resolver or `current`, not new flat presentation copies.
- `work/index.json` contains `media` (media-key ? source key, relative media directory, newest
  complete run, bundle and input/canonical URLs) and a tree signature. Missing/stale indexes
  rebuild lazily from retained metadata/manifests. Index-write failure leaves a usable in-memory
  map, so the index is not a single point of failure.
- Audio is served from the retained original when it exists. Removing both the external source
  and cached audio still permits opening results; the audio endpoint then returns 404. No
  downloading, re-derivation or retention policy was added.
- Files/manifests use `atomic_write_bytes` (including JSON writes), with file fsync before replace
  and directory barriers before references. POSIX uses directory fsync; Windows uses
  `MoveFileExW(REPLACE_EXISTING | WRITE_THROUGH)` after flushing each temporary file, since it
  has no POSIX directory-fsync operation. The explicit directory helper documents that platform
  distinction. The tests inject failures at the publication boundary; they do not simulate
  physical device power loss. Unsealed/damaged directories are never published or overwritten.
- The four-source-review reference resolves to three `docs/reviews/v2-review-*.md` files plus
  the plan-linked market report at `docs/research/05-market-2026-09.md`.

## Validation

Final result: **927 passed, 93 deselected**; both phase gates **11 passed**; the combined
golden/webapp/scorer gate **88 passed**. Both Windows gates passed, including browser audio seek.
Ruff and the fixture audit also passed after this report was populated.

The final commands below ran with `AUDD_API_TOKEN` explicitly empty, `IDEA_ENGINE_SHAZAM=off`,
and `IDEA_TEST_MODE=1`. A Python subprocess launcher preserves the empty token on Windows
(PowerShell 5.1 removes empty environment assignments). Pipeline tests use the committed fakes;
the browser gate uses the scripted Local Free fixture. No real source URL was analysed.
`uv run ruff format .` was run before the final format check. No new dependencies required sync.

Initial test runs exposed Windows MAX_PATH handling and schema/golden synchronization issues;
both were fixed and regression-covered. The first browser probe's virtual-time DOM dump raced
media loading; the gate now waits for a bounded real-time callback after the actual seek.
All required checks and the additional owner browser gate were executable here; nothing was
left for a live provider or unavailable browser. Test/gate artifacts remain in temporary folders.

```text
> uv run pytest -q
........................................................................ [  7%]
........................................................................ [ 15%]
........................................................................ [ 23%]
........................................................................ [ 31%]
........................................................................ [ 38%]
........................................................................ [ 46%]
........................................................................ [ 54%]
........................................................................ [ 62%]
........................................................................ [ 69%]
........................................................................ [ 77%]
........................................................................ [ 85%]
........................................................................ [ 93%]
...............................................................          [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
927 passed, 93 deselected, 1 warning in 203.73s (0:03:23)

exit=0

> uv run pytest tests/test_phase1a_bundles.py -q
...........                                                              [100%]
============================== warnings summary ===============================
tests/test_phase1a_bundles.py::test_pipeline_journal_names_only_durable_files
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
11 passed, 1 warning in 8.47s

exit=0

> uv run pytest tests/test_phase1a_cached_open.py -q
...........                                                              [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
11 passed, 1 warning in 11.51s

exit=0

> uv run pytest tests/test_golden_local_free.py tests/test_stage10_webapp.py tests/test_score_corpus.py -q
........................................................................ [ 81%]
................                                                         [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
88 passed, 1 warning in 27.39s

exit=0

> powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'

exit=0

> powershell -NoProfile -ExecutionPolicy Bypass -File scripts/gate_local_mode.ps1
complete; 6 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=\\?\C:\Users\natha\AppData\Local\Temp\idea-local-gate-600f90680bec465ca40074568de7c02c\work\a63408abeea9adf1c0d4452a6509d9ee4e5399d192f32ee7307bbcef39c83e27\e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d\present\bundles\375eecbff6bdd2da21c6926f86d38b39193f4a67b7dddbcb12d7828d95747000\tracklist.json
prepared offline cached mix e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d
local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok
Evidence: C:\Users\natha\AppData\Local\Temp\idea-local-gate-600f90680bec465ca40074568de7c02c

exit=0

> uv run ruff check .
All checks passed!

exit=0

> uv run ruff format --check .
248 files already formatted

exit=0

> uv run python scripts/audit_fixtures.py
audited 432 files
fixture audit passed

exit=0
```

## Working-tree snapshot

The stat is Git's tracked-file diff; the new, untracked files are listed in the status.

```text
> git status --short
 M docs/schemas/invocation_journal_entry.schema.json
 M idea.cmd
 M scripts/make_golden.py
 M src/id_detector/cli.py
 M src/id_detector/contracts.py
 M src/id_detector/ingest.py
 M src/id_detector/io.py
 M src/id_detector/journal.py
 M src/id_detector/present/exports.py
 M src/id_detector/present/page.py
 M src/id_detector/present/refresh.py
 M src/id_detector/present/server.py
 M src/id_detector/webapp/runner.py
 M tests/golden/invocation_journal_entry.json
 M tests/test_phase0a_crash_cache.py
 M tests/test_phase0a_status.py
 M tests/test_phase0b_config.py
 M tests/test_phase1b_fusion.py
 M tests/test_phase1b_targeting.py
 M tests/test_stage7_server.py
?? docs/reviews/build-1a-i-1a-iii.md
?? scripts/gate_local_mode.ps1
?? src/id_detector/present/bundles.py
?? src/id_detector/present/index.py
?? tests/test_phase1a_bundles.py
?? tests/test_phase1a_cached_open.py

> git diff --stat
warning: in the working copy of 'idea.cmd', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'scripts/make_golden.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/cli.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/contracts.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/ingest.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/journal.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/present/exports.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/present/page.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/present/refresh.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/present/server.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/webapp/runner.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'tests/test_phase0a_crash_cache.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'tests/test_phase0a_status.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'tests/test_phase0b_config.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'tests/test_phase1b_fusion.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'tests/test_phase1b_targeting.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'tests/test_stage7_server.py', CRLF will be replaced by LF the next time Git touches it
 docs/schemas/invocation_journal_entry.schema.json |  2 +-
 idea.cmd                                          |  4 +-
 scripts/make_golden.py                            |  5 +-
 src/id_detector/cli.py                            | 75 ++++++-----------
 src/id_detector/contracts.py                      |  9 +++
 src/id_detector/ingest.py                         | 25 +++++-
 src/id_detector/io.py                             | 24 +++++-
 src/id_detector/journal.py                        |  4 +
 src/id_detector/present/exports.py                | 10 ++-
 src/id_detector/present/page.py                   | 10 +--
 src/id_detector/present/refresh.py                | 99 ++++++++++++-----------
 src/id_detector/present/server.py                 | 43 +++++++---
 src/id_detector/webapp/runner.py                  |  4 +-
 tests/golden/invocation_journal_entry.json        |  2 +-
 tests/test_phase0a_crash_cache.py                 |  5 +-
 tests/test_phase0a_status.py                      |  9 ++-
 tests/test_phase0b_config.py                      |  3 +-
 tests/test_phase1b_fusion.py                      | 11 ++-
 tests/test_phase1b_targeting.py                   |  5 +-
 tests/test_stage7_server.py                       |  5 +-
 20 files changed, 216 insertions(+), 138 deletions(-)
```


## Review + fix pass

Adversarial review of the uncommitted 1a-i + 1a-iii change, then the fixes. Every check below was
re-run here (never trusting the transcript above) with `AUDD_API_TOKEN` empty, `IDEA_ENGINE_SHAZAM=off`
and `IDEA_TEST_MODE=1`; no live provider call and no real URL was analysed. The owner's `work/` and
`data/` trees were never written to: `git status --short -- work data` is empty, no file under either
tree has changed today, and `work/` still contains no `present/bundles/`, no `present/current`, no
`fuse/runs/` and no `index.json`. Backward compatibility was exercised against **copies** of three of
the owner's real pre-bundle mixes (one with `enrich/acquire.json`, one with no `invocations.jsonl`)
and against a 14-mix copy of the whole library.

### Findings

**P0-1 — a `degraded` or `partial` result becomes unreachable.**
`src/id_detector/present/bundles.py:75` (`result_dir`) accepted only `status == "complete"`, and the
pipeline no longer writes flat `present/` files. So a run ending `degraded`/`partial` published a
bundle nothing could find: `_discover_sets` skipped it (`src/id_detector/present/server.py:189`),
`rebuild_index` dropped it (`src/id_detector/present/index.py:34`), `GET /<sk>/<mk>/present/index.html`
and `.../tracklist.json` returned **404**, and `_load_cached` returned `None`, so `idea acquire`,
`idea rescan` and the web app's cached-open could not reopen it. Only the initiating job's page
(`shown_result_dir`, last journal line) could reach it. This contradicts plan §2.3.5 ("Results with
status `complete|degraded|partial` are **shown** to the requester") and 0a-iv's committed
`--allow-degrade` behaviour ("shown, with a banner"); §3.4 constrains the **pointer**, not
discoverability. Reproduced end-to-end before the fix (library 0 sets, both URLs 404).

**P1-2 — `cached_media_dir` raises on ordinary user input.**
`src/id_detector/present/index.py:91` computed `Path(target).expanduser().resolve().as_uri()`
unguarded. `http://[::1]/mix` raised `ValueError: relative path can't be expressed as a file URI`, a
target with an embedded NUL raised `ValueError: embedded null character in path`, and `~unknownuser/x`
raises `RuntimeError`. `validate_target` accepts bracketed-host URLs, so this reached
`cli._load_cached` from the web form (`src/id_detector/webapp/runner.py:194`) and from `idea acquire`
/ `idea rescan` (`src/id_detector/cli.py:1601`, `:1763`) as an uncaught crash where "not cached" is
the right answer.

**P1-3 — one audio range request re-hashed the whole library.**
`/media/<media_key>/audio` (`server.py:1336`) calls `_load_cached` -> `load_index`, and `load_index`'s
validation loop called `result_dir(...)` per media entry (`index.py:76`), i.e. `read_manifest` ->
SHA-256 of every file of every bundle. Measured on a copy of the owner's 14-mix library: **0.233 s per
call**, and a browser seek issues several range requests against a single-threaded server. The check
was also redundant: `tree_stamp` already covers every `source.json`, `present/current`, legacy
`index.html` and bundle `manifest.json` (mtime + size). `_discover_sets` additionally called
`load_index()` purely for its side effect (`server.py:189`), paying the same cost again on every
library page.

**P1-4 — one interrupted publication bricked a run forever.**
`bundles.py:205` raised `ValueError("unsealed or damaged bundle; refusing to overwrite")`, and the
revision loop advanced only while a *valid* manifest was found, so a directory that exists without a
valid manifest could never be superseded at the next revision. `fuse/runs/<run_id>` had the same
problem via `mkdir(..., exist_ok=False)`. Consequence: after a crash mid-publication, or a single
corrupted byte, `refresh` and `idea acquire` for that mix raise forever (silently swallowed by
`ensure_fresh_page`, so the page stays stale for good). "Never overwrite" is right; "never recover"
is not.

**P1-5 — the job player lost its verified-original guarantee.**
`_resolve_job_audio` (`server.py:1027`) documents that `ingest._load_cached` "verifies the completion
sidecar and the media-key hash, so a partial download is never served". 1a-iii redefined
`_load_cached` as a non-verifying resolver (`src/id_detector/ingest.py:170`), so the docstring became
false and an altered or corrupt original would be served to the analysing page.

**P2 (noted, not changed)**

- `cli.py:1194` + `cli.py:1237`: `_settle_money(usd_admitter)` is now called twice per run. Harmless —
  `UsdAdmitter.settle()` is explicitly idempotent (`money.py:159`) — but the second call is redundant.
- `io.py:98`: `ctypes.WinDLL("kernel32")` is constructed on every atomic write. Measured overhead of
  `MoveFileExW(REPLACE_EXISTING|WRITE_THROUGH)` vs `os.replace` over 200 writes: 0.412 s vs 0.387 s,
  about 6 % — acceptable for the durability it buys.
- `idea.cmd`: `uv run idea serve --open %*` relies on a later `--no-open` overriding `--open` (which
  Typer does). It works, and the 1a-iii owner gate needs argument forwarding, but it is fragile.
- `page.py:1177`: the page's `<audio src>` is now the absolute route `/media/<media_key>/audio`, so a
  bundle's `index.html` opened directly over `file://` has no audio. This is exactly what the plan
  asks for ("Page audio via `/media/<media_key>/audio` (local)"); flagged only because the screenshot
  workflow sometimes opens pages from disk.
- Toggling a presentation preference back and forth mints a fresh bundle each time
  (`test_changed_preferences_create_revision_without_rewriting_original`). Unbounded growth is the
  price of immutability; cleanup belongs to the 2b retention cycle.

**Scope (C) — all four flagged files are justified; nothing reverted.**
`journal.py` / `contracts.py` / `docs/schemas/invocation_journal_entry.schema.json` /
`tests/golden/invocation_journal_entry.json` carry the local equivalent of §3.4's "DB row" — the
journal's `bundle_id`/`fuse_run` reference — which the publication invariant is defined in terms of;
the nullable fields plus the before-validator keep every pre-bundle journal line parseable. `io.py`
adds the fsync / `WRITE_THROUGH` primitives the invariant needs. `idea.cmd` forwards arguments because
the 1a-iii owner gate must run `idea.cmd` against an isolated work root. `scripts/make_golden.py` only
follows the moved tracklist path.

**Tests (D)** — the edits to `test_phase0a_crash_cache.py`, `test_phase0a_status.py`,
`test_phase0b_config.py`, `test_phase1b_fusion.py`, `test_phase1b_targeting.py` and
`test_stage7_server.py` follow moved paths (`shown_result_dir` / `result_dir`) only; no assertion was
relaxed, and `test_stage7_server.py:209` is strictly stronger (legacy bytes must now be preserved).

### What was changed

- `src/id_detector/present/bundles.py` — `result_dir` falls back to the newest `degraded`/`partial`
  bundle when no `complete` one exists (P0-1); `_publish_current` ignores such a fallback so a
  complete run always claims `present/current`; the revision loop advances past an existing directory
  that is not a sealed bundle instead of raising, and an unsealed `fuse/runs/<run_id>` is completed in
  place rather than refused (P1-4).
- `src/id_detector/present/index.py` — `cached_media_dir` treats a target that is not expressible as a
  local path as "not a local file" instead of raising (P1-2); `load_index`'s warm-path validation is
  structural only, with a comment saying why re-hashing is wrong there (P1-3).
- `src/id_detector/present/server.py` — dropped the side-effect-only `load_index(work_root)` from
  `_discover_sets` (P1-3); `_resolve_job_audio` uses the verifying `ingest._load_ingest_cached`, and
  its docstring now names it (P1-5).

Measured after the fix, same 14-mix library: `_load_cached` **0.233 s -> 0.064 s**, `_discover_sets`
**0.433 s -> 0.221 s**. Bundle IDs for the owner's three real legacy mixes are byte-identical before
and after the fix (`c3bf0466...`, `27e3eea2...`, `83b3ec10...`), and their legacy `present/` files are
untouched.

### Tests added (all offline and deterministic)

`tests/test_phase1a_bundles.py`

- `test_degraded_result_is_shown_but_never_becomes_the_served_pointer` — degraded, then partial, then
  an older complete run: `result_dir` follows the shown result while `present/current` stays absent,
  then flips to the complete run (P0-1).
- `test_degraded_result_opens_from_the_library_the_url_and_the_cli` — a degraded-only mix is listed by
  `_discover_sets`, indexed with `newest_complete_run: null`, found by `_load_cached`, and served 200
  on both `present/index.html` and `present/tracklist.json` (P0-1).
- `test_damaged_or_unsealed_bundle_is_never_overwritten_and_publication_recovers` — replaces the
  previous raise-only test; still asserts the damaged bytes are never rewritten, and now also that
  publication lands in a new revision that becomes `current` (P1-4).
- `test_interrupted_fuse_freeze_is_completed_rather_than_refused` — a half-written, unsealed
  `fuse/runs/<run_id>` is finished and sealed, not refused (P1-4).

`tests/test_phase1a_cached_open.py`

- `test_non_local_targets_never_raise_from_the_cached_lookup` — `http://[::1]/mix`, `~nosuchuser/...`,
  an embedded NUL, `""` and `C:` all return `None` (P1-2).
- `test_warm_index_does_not_rehash_every_bundle_on_a_cached_open` — a warm `load_index` calls neither
  `result_dir` nor `read_manifest` (P1-3).
- `test_job_audio_is_only_served_for_a_verified_original` — an original whose bytes do not hash to the
  media key is not served to the analysing page (P1-5).

### Final gate outputs

```text
> uv run pytest -q
933 passed, 93 deselected, 1 warning in 271.49s (0:04:31)

> uv run pytest tests/test_phase1a_bundles.py -q
14 passed, 1 warning in 19.13s

> uv run pytest tests/test_phase1a_cached_open.py -q
14 passed, 1 warning in 20.48s

> uv run pytest tests/test_golden_local_free.py tests/test_stage10_webapp.py tests/test_score_corpus.py tests/test_stage7_server.py -q
95 passed, 1 warning in 40.95s

> uv run ruff check .
All checks passed!

> uv run ruff format --check .
248 files already formatted

> uv run python scripts/audit_fixtures.py
audited 432 files
fixture audit passed

> powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'

> powershell -NoProfile -ExecutionPolicy Bypass -File scripts/gate_local_mode.ps1
prepared offline cached mix e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d
local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok

> uv run python scripts/score_corpus.py --run-list data/local/release-1-runs-free.json --out $TEMP/1a-check.json
scored 7 mix(es) [draft truth, matched by work]: likely 8971/10000, listed n/a, recall 6422/10000
```

All seven runs in `data/local/release-1-runs-free.json` resolved to readable `fuse/episodes.json`
files (7 checked, 0 missing). Pooled: `likely_precision_e4` **8971**, `listed_precision_e4` **null**
(work-only matching, because 4 of the 7 truths are order-only), `work_recall_e4` **6422**,
`work_precision_e4` **7527**. `docs/accuracy/release-1-free-draft.md` records 89.7 % likely precision,
63.8 % recall (139/218) and 74.7 % work precision. Likely precision is unchanged; recall and work
precision moved by a single work-match (139 -> 140) because of the already-committed
`1f75269 fix(identity): merge two crowd IDs of the same track into one row`, which changes
scoring-time identity merging. Nothing in this diff touches the scoring path
(`scripts/score_corpus.py` is unmodified, and `export_tracklist`'s new `output_dir` argument defaults
to the previous behaviour), and the same numbers were produced before and after the fixes.


## Second review (sol xhigh) + fix pass

A second adversarial reviewer (Codex gpt-5.6-sol, xhigh, read-only sandbox) reviewed the tree after
the first fix pass and returned FIX_FIRST. Each finding was re-verified here against the code before
being fixed; all seven were real. The sol reviewer could not execute anything, so every gate below
was run here. `work/` and `data/` remain untouched (`git status --short -- work data` empty; no
`present/bundles/`, `present/current`, `fuse/runs/` or `index.json` under `work/`).

### Findings and verdicts

**P0 — acquisition mixes two runs and promotes incomplete data as complete. Confirmed.**
`_acquire` read the rows from the *mutable* tree (`load_analysis(media_dir)`, and again inside
`enrich_media_dir` at `src/id_detector/enrich/run.py:325`) but took the run identity from
`run_metadata(media_dir)` -> `result_dir`, which deliberately prefers the newest **complete** run.
With `complete C` followed by `partial P`, `idea acquire` therefore rendered **P's** rows under
**C's** run id and `complete` status, published them as a revision of C, and advanced
`present/current` — a partial result served to every later open, and a bundle whose
`manifest["fuse_run"]` points at a frozen snapshot that does not match its own rendered rows.
`regenerate_page` resolved `result_dir` twice (once directly, once through `run_metadata`), the same
split-resolution shape.
*Fix:* `bundles.load_run_snapshot()` resolves the selected result **and** its source, frozen
episodes/identities, duration and acquisition data together under `_PUBLICATION_LOCK`, and
`bundles.publish_snapshot()` publishes a revision of that same run. `regenerate_page` and `_acquire`
both go through it, and `enrich_media_dir` now accepts pinned `episodes`/`identities` so the links it
resolves and the rows the page renders are the same run's.

**P1 — a previously sealed but damaged frozen run is rewritten in place. Confirmed.**
`publish_result` treated "never sealed", "interrupted" and "was sealed, now damaged" identically and
re-derived the snapshot from today's mutable `fuse/` tree — which by then may belong to a different
run — then resealed it, silently changing the refresh inputs of an already-published bundle. (This
was a regression introduced by my own first-pass `exist_ok=True`; the reviewer is right that my test
covered only the never-sealed case.)
*Fix:* the three states are now distinguished by whether `manifest.json` exists. No manifest at all
means nothing can reference it, so an interrupted freeze is completed in place; a `manifest.json`
that exists but fails verification raises `damaged frozen run; refusing to reseal`.

**P1 — manifests are not structurally validated. Confirmed.**
`read_manifest` verified only `files`, while `_order` indexes `manifest["run_id"]` and
`manifest["presentation_version"]` — so a JSON-valid damaged manifest raised `KeyError` out of
`result_dir` and broke the whole library, not just that bundle. Reuse also republished a pointer
without revalidating the frozen run it names.
*Fix:* `read_bundle_manifest()` requires `run_id`, `presentation_version`, `status` and `fuse_run`,
checks their types, checks `sha256(run_id, presentation_version)` equals the directory name, and
requires `index.html`, `tracklist.json` and `source.json` in `files`; a damaged bundle is skipped, not
raised. Every selection site (`result_dir`, `shown_result_dir`, `run_metadata`, `_publish_current`,
the revision loop, `rebuild_index`) uses it. On the reuse path `_frozen_run()` now re-verifies the
referenced fuse run **before** the pointer names it.

**P1 — duplicate media aliases are dropped. Confirmed.**
`rebuild_index` keyed one entry per `media_key`, so a second source directory holding the same bytes
overwrote the first; once originals are gone the verified-ingest fallback cannot recover the dropped
URL, which then resolves to nothing.
*Fix:* each entry keeps an `aliases` list of every `(source_key, media_dir, input_url,
canonical_url)`; `cached_media_dir` matches against all of them and returns the matching alias's own
directory. `_usable` validates every alias.

**P1 — the claimed in-memory index fallback did not exist. Confirmed.**
`rebuild_index` swallowed the write error and returned the document, but nothing retained it, so
every later `load_index` rebuilt from scratch — once per audio range request on a read-only or
locked work root.
*Fix:* a module-level `_MEMORY` map keyed by work root holds the last good document when
`index.json` cannot be replaced, and is cleared as soon as a write succeeds. `load_index` now takes
the tree stamp once and tries disk, then memory, before rebuilding.

**P2 — `test_stage7_server.py` missing-artefact case was hollow. Confirmed.**
After the fresh bundle exists, damaging the now-unselected legacy page makes `ensure_fresh_page`
return early on the bundle's current stamp, so the failing render was never reached — the assertion
passed for the wrong reason.
*Fix:* the case now runs on a second, genuinely selected legacy result (a `mixcloud` source with no
`decode/pcm.json`): the refresh fails, no bundle is created, the bytes are untouched, and the server
still serves the old page with 200.

**P2 — dead code and the duplicate settlement. Confirmed.** `_tracklist_run_fields` lost its only
caller when the flat re-export went away, and the second `_settle_money(usd_admitter)` only
recomputed idempotent figures. Both removed.

### Tests added

`tests/test_phase1a_bundles.py`
- `test_acquire_never_publishes_a_newer_partial_run_under_an_older_complete_one` — complete C,
  partial P in the mutable tree, then acquisition: the snapshot yields C's frozen rows and the
  published revision stays `run-c`/`complete`.
- `test_idea_acquire_enriches_and_renders_the_same_run` — the whole `cli._acquire` path with
  `enrich_media_dir` replaced by a recorder: enrichment receives C's rows, not P's, and the bundle
  and pointer stay on C. (Pre-fix this fails: `_acquire` passed no pinned rows at all.)
- `test_refresh_renders_the_selected_run_not_the_newest_mutable_fuse` — the same for refresh.
- `test_a_run_published_between_snapshot_and_publish_keeps_the_newest_pointer` — a concurrent
  publication may win `present/current`; it never changes what the snapshot renders.
- `test_a_previously_sealed_but_damaged_frozen_run_is_never_resealed`.
- `test_a_damaged_manifest_is_skipped_instead_of_breaking_the_library` — each required field removed
  in turn, plus a mismatched identity: `read_bundle_manifest` returns `None`, `result_dir` still
  selects the intact bundle, nothing raises.
- `test_a_reused_bundle_is_not_pointed_at_when_its_frozen_run_is_damaged`.

`tests/test_phase1a_cached_open.py`
- `test_every_source_alias_for_the_same_audio_stays_findable` — two source directories sharing a
  media key; both URLs resolve to their own directory with no original on disk.
- `test_repeated_cached_open_on_an_unwritable_work_root_rebuilds_once` — three lookups under a
  `PermissionError` on `index.json` cause exactly one rebuild.

`tests/test_stage7_server.py` — the restored missing-artefact refresh regression described above.

### Final gate outputs

```text
> uv run pytest -q
942 passed, 93 deselected, 1 warning in 383.58s (0:06:23)

> uv run pytest tests/test_phase1a_bundles.py -q
21 passed, 1 warning in 38.03s

> uv run pytest tests/test_phase1a_cached_open.py -q
16 passed, 1 warning in 18.80s

> uv run pytest tests/test_golden_local_free.py tests/test_stage10_webapp.py tests/test_score_corpus.py tests/test_stage7_server.py -q
95 passed, 1 warning in 46.96s

> uv run ruff check .
All checks passed!

> uv run ruff format --check .
248 files already formatted

> uv run python scripts/audit_fixtures.py
audited 432 files
fixture audit passed

> powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'

> powershell -NoProfile -ExecutionPolicy Bypass -File scripts/gate_local_mode.ps1
prepared offline cached mix e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d
local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok

> uv run python scripts/score_corpus.py --run-list data/local/release-1-runs-free.json --out $TEMP/1a-check3.json
scored 7 mix(es) [draft truth, matched by work]: likely 8971/10000, listed n/a, recall 6422/10000

> git status --short -- work data
(empty)
```

The first gate run of this pass hung because an `idea serve` from an earlier `gate_local_mode.ps1`
run survived the script's `taskkill` and kept port 8792; the stale gate processes were killed (only
processes whose command line named an `idea-local-gate-*` temp directory) and the gate then passed
cleanly. The script's cleanup not always reaching the `uv run` grandchild is a **P2** worth tidying
in a later cycle; it does not affect the shipped code.

Backward compatibility was re-verified after this pass against copies of three of the owner's real
pre-bundle mixes: each refreshes to the **same bundle id** as before the fixes (`c3bf0466...`,
`27e3eea2...`, `83b3ec10...`), `load_run_snapshot` resolves the same run the manifest names
(105 / 50 / 88 episodes, acquisition data preserved on the third), the legacy `present/` files are
untouched, and `_load_cached` resolves every input URL. Corpus numbers are unchanged from the first
pass: `likely_precision_e4` 8971, `listed_precision_e4` null, `work_recall_e4` 6422.

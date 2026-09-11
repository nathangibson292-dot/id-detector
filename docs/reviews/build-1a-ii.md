# Build 1a-ii — Analysis keys and compatibility

Implemented this cycle only, without branches, commits, pushes, new dependencies, or live
provider calls. The plan, frozen profiles, and corpus are unchanged.

## Files changed

- `src/id_detector/compat.py`: canonical analysis inputs, runtime compatibility stamps, pure serving rules, sealed-bundle lookup, hint/index identities, and frozen Free evidence loading.
- `src/id_detector/cli.py`: identity-aware lookup before reservation, local degraded opt-in, scoped inputs, Free evidence freezing, Deep-on-Free reuse, selected-result paths, and source-changed exit 5.
- `src/id_detector/ingest.py`: request-aware cached lookup, local-file identity verification, and rejection of changed re-fetched bytes before publication.
- `src/id_detector/providers/base.py`: validated `cache.serve_free_from_deep`, default false.
- `src/id_detector/config_template.py`: document and display the new cache flag.
- `idea.example.toml`: keep the committed example synchronized with the packaged template.
- `src/id_detector/contracts.py`: nullable analysis identity/compatibility metadata and `source_changed`, retaining legacy journal parsing.
- `src/id_detector/journal.py`: carry the run's identity and compatibility metadata into terminal journal entries.
- `src/id_detector/present/bundles.py`: publish the analysis key in tracklist JSON and preserve compatibility metadata in sealed manifests and presentation revisions.
- `src/id_detector/webapp/runner.py`: explicitly select a recipe and deliver the compatible bundle selected by the shared pipeline.
- `docs/schemas/invocation_journal_entry.schema.json`: synchronize the journal schema.
- `tests/golden/invocation_journal_entry.json`: synchronize nullable journal fields.
- `tests/fakes/scripts/phase1a-compat.json`: deterministic matching AudD/Shazam responses for the cycle gate.
- `tests/test_phase1a_compat.py`: the cycle's pure, integration, source-change, config, and web-runner tests.
- `tests/test_phase0a_crash_cache.py`: isolate two existing raw-cache tests from derived-result lookup, preserving their provider-refresh assertions.
- `docs/reviews/build-1a-ii.md`: this report and validation transcripts.

## Tests added

The phase test file covers every serving-table row, both Deep densities, the launch flag,
algorithm/adapter/compatibility version mismatches, exact Free keys, non-recipe input mismatches,
hosted partial/degraded exclusion, local degraded opt-in, and isolation between users.

Pipeline tests cover primary-only Free-to-Deep reservation (36,750 microdollars for seven
AudD windows), zero new FakeShazam requests, frozen evidence reuse, identity agreement between
journal/export/manifest, zero-budget dense-to-sparse cache hits, sparse-to-dense rejection,
Free-from-Deep flag behavior, unchanged historical bundles, changed local-source CLI exit 5,
changed re-fetched bytes through a fake downloader, retained-result lookup without original
audio or decode, altered retained audio needed for new work, hints/manual-tracklist identity
changes, and upload-owner isolation.
Additional tests cover hint ordering/content stability, index-manifest identity changes,
configuration validation, recipe version bumps, and both web recipe selections.

## Contract choices

- Canonical analysis JSON is the seven named inputs, sorted keys, compact separators, UTF-8,
  `ensure_ascii=False`, without a newline, matching recipe identity serialization. The second
  hash excludes only `recipe_id`; the manifest also retains all seven inputs.
- The local owner is defined once as `user:local`. Uploads, manual tracklists, and local Panako
  indexes require an owner scope. Ordinary platform/local inputs use `public`; explicit owner
  scopes remain distinct. No account or hosted submission system was introduced.
- `hints_snapshot_id` hashes the sorted complete JSON HintRecords actually passed to fusion.
  Sorting uses each record's canonical digest. Empty/disabled hints hash the empty list.
  Connector timing, retry status, and cache bookkeeping are excluded because they are not
  fusion inputs. Hint content, positions, identities, provenance and relations are included.
  Hints resolve during intake, before compatibility lookup and paid provider reservation.
- Panako identity hashes its label and the SHA-256 of its existing `index.json` reference
  manifest. A requested but missing manifest has an explicit empty manifest digest. Runtime
  lock files and query caches do not affect the reference index identity. Local indexes are
  conservatively private.
- The plan's cross-recipe serving rows require equal version stamps, while the existing Free
  recipe has `fusion:2`/Shazam only and Deep has `targeting:1,fusion:2`/both adapters. Recipes are
  unchanged. The new `compatibility` object stamps the installed engine stack for both recipes:
  `targeting:1,fusion:2`, `audd_clip:2`, `shazam:1`. Per-recipe version overrides replace their
  corresponding runtime components, so a bump invalidates serving. The existing journal's
  recipe-specific `algorithm_version` retains its historical meaning; compatibility compares
  the new object's exact algorithm and adapter fields. `compat_version=1` is separate.
- `cache.serve_free_from_deep=false` remains the default until L3. `--accept-degraded` maps to
  the local `RunRequest.accept_degraded`; hosted requests (`local=False`) cannot opt in.
- Legacy results remain directly openable, but lack the identity/version evidence needed to
  serve a new analysis request. They are never silently relabelled as compatible. The shared
  lookup searches all sealed bundles, so the current presentation pointer cannot hide an
  older compatible run. Explicit `--refresh` bypasses derived-result and Free-evidence reuse;
  raw response cache behavior is unchanged.
- A Free run freezes observations from all its recognition generations with its fuse bundle.
  Deep-on-Free uses those observations after AudD's primary sweep and re-fuses, with no new
  secondary or rescan allocation. The existing AudD-only reservation calculation is unchanged.
- Cached result presentation does not require retained original audio. If the external local
  source still exists, cached open re-hashes it. Re-fetch mismatch is checked before moving
  downloaded bytes into the durable media tree. A mismatch appends a `source_changed` journal
  entry with exit 5 and no bundle; prior bundles and exports remain intact. Missing local files
  retain the preceding cycle's cached-open behavior.
  Before intake has established an analysis identity, terminal journal entries retain null
  identity fields rather than inventing a key from hints that were never resolved.
- The web UI still exposes its existing profile labels; the runner translates them to an
  explicit `Recipe`, the internal equivalent of CLI `--recipe`, and shares `_analyse` lookup.
  Its initiating job receives the selected bundle rather than whichever bundle is newest.
- Coalescing, credits, accounts, alias revalidation policy, and retention remain in their named
  later cycles. No files scheduled for later deletion were removed.

## Validation

All CLI/pipeline checks run with `AUDD_API_TOKEN` explicitly empty, `IDEA_ENGINE_SHAZAM=off`,
and `IDEA_TEST_MODE=1`. A Python subprocess launcher preserves the empty token on Windows,
where PowerShell 5.1 removes an empty environment assignment. Pipeline tests inject the
committed fakes; the source re-fetch test intercepts the downloader. No real URL was analysed.

Validation transcripts follow below. No live-only checks are required by this cycle.

The initial full run found two raw-cache tests intercepted by the new result cache and one
example-template synchronization failure. The raw-cache tests now explicitly bypass only
derived-result lookup; their existing request-count and refresh assertions are unchanged.
The example was synchronized. The first transcript launcher's Windows stdout encoding also
failed when printing a failure diagnostic; subsequent validation uses UTF-8 explicitly.

Final validation: full suite **978 passed, 93 deselected**; phase gate **37 passed**;
combined bundle/cached-open/golden/webapp gate **72 passed**; Ruff, fixture audit, and the
Windows serve smoke gate passed. The retained-audio regression was added after the full
suite collected its tests; it passed individually and in the final 37-test phase gate.
The full suite also includes `tests/test_score_corpus.py`; scoring code is unchanged.
`uv run ruff format .` was run before the final format check. There are no outstanding
implementation items or checks requiring live providers. No dependencies were added, so
`uv sync` was unnecessary. The protected-path diff and `git diff --check` were clean.

```text
> uv run pytest -q
........................................................................ [  7%]
........................................................................ [ 14%]
........................................................................ [ 22%]
........................................................................ [ 29%]
........................................................................ [ 36%]
........................................................................ [ 44%]
........................................................................ [ 51%]
........................................................................ [ 58%]
........................................................................ [ 66%]
........................................................................ [ 73%]
........................................................................ [ 80%]
........................................................................ [ 88%]
........................................................................ [ 95%]
..........................................                               [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
978 passed, 93 deselected, 1 warning in 415.71s (0:06:55)

exit=0

> uv run ruff check .
All checks passed!

exit=0

> uv run ruff format --check .
251 files already formatted

exit=0

> uv run python scripts/audit_fixtures.py
audited 433 files
fixture audit passed

exit=0

> uv run pytest tests/test_phase1a_compat.py -q
.....................................                                    [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
37 passed, 1 warning in 41.89s

exit=0

> uv run pytest tests/test_phase1a_bundles.py tests/test_phase1a_cached_open.py tests/test_golden_local_free.py tests/test_stage10_webapp.py -q
........................................................................ [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
72 passed, 1 warning in 58.67s

exit=0

> powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'

exit=0

```

## Working-tree snapshot

Git stat covers tracked files; new files appear in status.

```text
> git status --short
 M docs/schemas/invocation_journal_entry.schema.json
 M idea.example.toml
 M src/id_detector/cli.py
 M src/id_detector/config_template.py
 M src/id_detector/contracts.py
 M src/id_detector/ingest.py
 M src/id_detector/journal.py
 M src/id_detector/present/bundles.py
 M src/id_detector/providers/base.py
 M src/id_detector/webapp/runner.py
 M tests/golden/invocation_journal_entry.json
 M tests/test_phase0a_crash_cache.py
?? docs/reviews/build-1a-ii.md
?? src/id_detector/compat.py
?? tests/fakes/scripts/phase1a-compat.json
?? tests/test_phase1a_compat.py

> git diff --stat
warning: in the working copy of 'idea.example.toml', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/cli.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/config_template.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/contracts.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/ingest.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/journal.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/present/bundles.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/providers/base.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'src/id_detector/webapp/runner.py', CRLF will be replaced by LF the next time Git touches it
warning: in the working copy of 'tests/test_phase0a_crash_cache.py', CRLF will be replaced by LF the next time Git touches it
 docs/schemas/invocation_journal_entry.schema.json |   2 +-
 idea.example.toml                                 |   2 +
 src/id_detector/cli.py                            | 176 ++++++++++++++++++----
 src/id_detector/config_template.py                |   3 +
 src/id_detector/contracts.py                      |  11 +-
 src/id_detector/ingest.py                         |  37 ++++-
 src/id_detector/journal.py                        |   4 +
 src/id_detector/present/bundles.py                |   8 +-
 src/id_detector/providers/base.py                 |   5 +
 src/id_detector/webapp/runner.py                  |  32 ++--
 tests/golden/invocation_journal_entry.json        |   2 +-
 tests/test_phase0a_crash_cache.py                 |   9 +-
 12 files changed, 238 insertions(+), 53 deletions(-)

```

## Review + fix pass (sol xhigh second review folded in)

Independent adversarial review of the uncommitted cycle, then the fixes. Findings were formed
from the plan (`### 1a-ii`, §2.3.4 step 1, §3.3, §3.4, §3.5, §6.1) and the diff first, then
cross-checked against the first reviewer's list. Every test/gate claim below was re-run here;
nothing was taken from pasted output. Baseline before the fixes: `uv run pytest -q` **979 passed**.

### Findings

**P0-1 — launch-controlled settings bypassed the pricing authority** (mine + sol; sol correct).
`providers/base.py:190-193` read `serve_free_from_deep` from the owner's `[cache]` table and
`compat.py:14` hard-coded `COMPAT_VERSION = 1`, while `pricing.toml:9-10` already carries both and
`pricing.py:69-70,122-126,158-159` already parses them (plan §3.3: "single pricing authority").
`[cache] serve_free_from_deep = true` in `idea.toml` would have served Deep results to Free
requests with the authority still saying `false` — i.e. before launch gate L3.
*Fix:* both values now come only from `PricingConfig` (`providers/base.py` in all three
construction paths; `compat.py:17`). The key in an owner config is now a hard error naming
`pricing.toml`. `idea.example.toml` is byte-identical to `0665f04` again, the template line is
gone, and `config show` prints the value as a comment (`(pricing.toml)`) so the effective config
stays complete without implying the file can set it.

**P0-2 — compatible results were not found across source aliases, so AudD was re-billed**
(mine + sol; sol correct). Ingest files bytes under `work/<source_key>/<media_key>`
(`ingest.py:286`), so identical bytes reached under two URLs occupy two directories while sharing
one `analysis_key`; `find_result` searched only the requester's directory, and the AudD clip cache
(`paid_clip.py`) is per-directory, so the second alias reserved and paid the whole sweep again.
*Fix:* `compat._alias_dirs` makes the serving lookup work-root-wide over that `media_key`
(requester's directory first), with the candidate manifest's own `media_key` re-checked before it
can serve. Frozen-Free-evidence reuse deliberately stays directory-local: fusion records every
observation file `relative_to(media_dir)` (`fuse/episodes.py:1353`), so a cross-directory reuse
would raise — and it costs no money either way, because a cross-alias Deep run pays the same AudD
sweep with or without reused Shazam evidence. Left as **P2**: re-publishing a cross-alias hit under
the requester's alias metadata (§3.4 "Presentation inputs") needs the frozen fuse run copied into
the requester's directory, which belongs with the alias/coalescing work (4b-iv), so today a served
cross-alias bundle shows the first alias's title/embed for byte-identical audio.

**P0-3 — web acquisition discarded the compatibility-selected bundle** (mine + sol; sol correct,
and the consequence is worse than reported). With `ctx.acquire` the runner called `cli._acquire`,
which reopened `present/current` through `load_run_snapshot(media_dir)` → `result_dir` = the newest
*complete* run. A Free request with `serve_free_from_deep` off, an older compatible Free result and
a newer Deep run therefore both *delivered* the Deep bundle and wrote the acquisition links onto
the Deep run instead of the served one.
*Fix:* `load_run_snapshot(media_dir, directory=…)` takes the selected bundle (and refuses a
missing/damaged one rather than silently falling back to the legacy snapshot); `_acquire` accepts
`bundle=` / `result_paths=`, derives `media_dir` from the selected bundle and reports the revision
it published; the runner threads the selection through and delivers that revision.

**P1-4 — Free results were stamped with algorithms and adapters they never ran** (mine + sol; sol
correct). `RunRequest.__post_init__` synthesised `targeting:1,fusion:2` and added `audd_clip: 2`
for the Free recipe so that cross-recipe comparison could work, which meant a Deep-only bump would
have retired every Free result and left two meanings for `algorithm_version`.
*Fix:* an explicit projection. A result is stamped with the versions of the recipe that actually
produced it (`RunRequest.metadata(achieved)`, so a `--allow-degrade` run is stamped Free, matching
`_money_journal_fields`' rule), and `compat.versions_current` compares a stored stamp against that
stored recipe as this build ships it. A bump to a recipe retires that recipe's results for every
request; a Deep-only bump leaves Free results servable and still usable as Deep evidence.

**P1-5 — Deep-on-Free coverage asserted the reservation but not the money** (sol; correct).
Extended: exact reservation (36,750 microdollars), exact spend (7 × 5,000), the released remainder
(1,750), the e2 figures, and exactly one terminal journal entry for the run.

**P2-6 — duplicate compatibility lookup in `ingest._load_cached`** (sol; correct). The
`request=`/`serve_free_from_deep=` parameters were production-dead (one test used them) and a
second place to choose which result answers a request. Removed; the test now calls
`compat.find_result` directly.

**P2-7 (mine) — `duration_ms` was not refreshed by the late decode.** When the retained-bundle
shortcut supplied `duration_ms` from the manifest and the run then re-ingested and decoded, only
`ffmpeg_version` was updated, so the window schedule, the Deep reservation and the secondary
capacity ran on the manifest's figure. Now the decoded PCM's own duration is taken.

**P2-8 (mine) — the `source_changed` entry journalled `_settle_money(None)` unconditionally.**
No reachable path reserves before source identity is proven (the check runs before any window is
cut), but the entry now settles the live admitter so a future reordering cannot silently drop
spend. Settlement itself is idempotent (`money.py:159-176`), so there is no double-settle path.

### Verified and judged correct as built (no change)

- **Deep-on-Free money.** The reservation is the AudD primary only (`planned = primary_planned`)
  and reused Free evidence sets `max_generations = 0` and skips the secondary block, so the run
  issues **zero** Shazam requests (asserted). Nothing re-bills AudD: the serving lookup precedes
  `reserve_usd` (proved with `max_usd_e2=0`), and settlement is idempotent.
- **§3.4 table, row by row.** Free ← Free requires equal `analysis_key`; Free ← Deep only with the
  flag, any density; Deep d=2 ← d=2 or d=1; Deep d=1 ← d=1 only; `partial` never; `degraded` only
  local + `accept_degraded`; `tenant_scope` must match; legacy bundles carry no `compatibility`
  object and so are opened but never served.
- **`analysis_key` / `hints_snapshot_id`.** All seven plan inputs are hashed, and only `recipe_id`
  is excluded from the non-recipe key. `HintRecord` carries no timestamps, retry state or cache
  bookkeeping, so the snapshot is stable across identical re-runs; the hash is over the full list
  sorted by per-record digest, so ordering does not matter and duplicates are preserved. Hints
  disabled and hints-found-nothing hash alike, which is right — fusion receives the same input.
- **`source_changed` → exit 5.** Raised before any window is cut, publishes no bundle, appends one
  journal entry with `bundle_id: null`, and leaves prior bundles, exports and `present/current`
  intact (asserted byte-for-byte).
- **The owner's data.** `git status --short` shows nothing under `work/` or `data/`; nothing there
  was moved, rewritten or deleted. The seven release-1 runs still score, reproducing the
  `0665f04` baseline exactly.

### Tests added or extended (`tests/test_phase1a_compat.py`)

- `test_launch_flag_and_compat_version_come_from_pricing_only` — authoritative `true`/`false` both
  reach `AppConfig` (with and without an owner config), an owner `[cache]` key is refused, the key
  is absent from the packaged template, and `COMPAT_VERSION` equals the authority's.
- `test_compat_version_bump_retires_every_stored_result` — a bumped authority version retires
  stored Free and Deep results.
- `test_recipe_version_bumps_invalidate_serving` — now simulates a real bump (the shipped recipe
  registry moves, the stored stamp does not).
- `test_results_are_stamped_with_what_they_ran_and_deep_bumps_spare_free` — Free stamps are
  `fusion:2` / `{shazam: 1}`; a Deep-only bump retires Deep results and spares Free ones.
- `test_second_alias_with_identical_bytes_pays_nothing` — two paths, identical bytes, two
  directories: the second Deep request makes 0 AudD and 0 Shazam calls under a zero USD cap and is
  served the first alias's bundle. Confirmed failing before the fix.
- `test_web_acquisition_republishes_the_selected_bundle` — a newer Deep run holds
  `present/current`; the acquisition revision belongs to the selected Free run, carries
  `acquire.json`, and is what the job delivers, while the Deep bundle is untouched. Confirmed
  failing before the fix.
- `test_free_to_deep_primary_only_and_frozen_secondary` — exact spend, released remainder and the
  single terminal settlement entry.

### Gate outputs (re-run here)

```text
> uv run pytest -q
983 passed, 93 deselected, 1 warning in 249.69s (0:04:09)

> uv run pytest tests/test_phase1a_compat.py -q
41 passed, 1 warning in 37.12s

> uv run pytest tests/test_phase1a_bundles.py tests/test_phase1a_cached_open.py
    tests/test_golden_local_free.py tests/test_stage10_webapp.py tests/test_score_corpus.py -q
125 passed, 1 warning in 59.24s

> uv run ruff check .
All checks passed!

> uv run ruff format --check .
251 files already formatted

> uv run python scripts/audit_fixtures.py
audited 433 files
fixture audit passed

> powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'

> powershell -NoProfile -ExecutionPolicy Bypass -File scripts/gate_local_mode.ps1
prepared offline cached mix e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d
local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok

> PYTHONIOENCODING=utf-8 uv run python scripts/score_corpus.py
    --run-list data/local/release-1-runs-free.json --out "$TEMP/1a-ii-check.json"
scored 7 mix(es) [draft truth, matched by work]: likely 8971/10000, listed n/a, recall 6422/10000

> git status --short -- work data
(no output)
```

Pooled release-1 (free) numbers: `likely_precision_e4` **8971**, `listed_precision_e4` **null**
(work-only matching), `work_recall_e4` **6422** — identical to the `0665f04` record.

Every CLI, pipeline and gate invocation ran with `AUDD_API_TOKEN` explicitly empty,
`IDEA_ENGINE_SHAZAM=off` and `IDEA_TEST_MODE=1`; both PowerShell gates use their own temporary
work roots and the committed fakes. No real URL was analysed and no live provider was contacted.
No dependencies were added; `docs/PLAN-v2.md`, `profiles/`, `data/` and `work/` are untouched;
recipe `algorithm_version` remains `targeting:1,fusion:2`.

### Remaining P2 notes (out of this cycle's scope)

1. A served cross-alias bundle is presented with the storing alias's `source.json`; §3.4 wants the
   requester's alias metadata, which needs a cross-directory re-publish of the frozen fuse run —
   alias/coalescing work (4b-iv).
2. Frozen-Free-evidence reuse (Deep-on-Free) is directory-local for the reason above; a cross-alias
   Deep run therefore re-runs its own Shazam secondary. No money effect.
3. A served cache hit writes no journal entry, so local `cache_hits` accounting (§3.5) and the
   hosted uncounted-hit ledger still belong to their own cycles.

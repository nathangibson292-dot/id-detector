# Build 0a-iv + 0a-iii — Status matrix, `--allow-degrade`, provisional secondary, Phase-0 Deep gate; Security and dead paths (local)

Two adjacent plan cycles built in one pass because they touch the same files (`cli.py`,
`paid_clip.py`, the web runner and server). Everything is uncommitted on `main` at `b48768b` for
review. No dependency was added; `docs/PLAN-v2.md`, `profiles/` and `data/corpus/` are untouched.
No live provider call was made: every pipeline run used `--fake-providers audd,shazam` under
`IDEA_TEST_MODE=1` or injected fakes through `_analyse`.

---

## Cycle 0a-iv — Status matrix, `--allow-degrade`, provisional secondary, Phase-0 Deep gate

### File-by-file changes

- `src/id_detector/secondary_targeting.py` — **new**: the provisional `targeting:0` scheduler
  (plan §2.3.4 step 4): `secondary_capacity` (`C = ⌈duration_min × 2⌉`), `blank_spans`,
  `select_secondary_candidates` (`hint_only → listed_not_confident → suppressed_challengeable →
  blank`, start order within a class), `schedule_secondary_windows` (frozen windows overlapping a
  candidate span ≥ `eligibility_min_intersection_ms`, queued in priority then start order up to
  `C`, a window queued once), `frozen_windows`.
- `src/id_detector/money.py` — `TERMINAL_PROVIDER_OUTCOMES` (`auth_error`, `quota_error`) and
  `UNREACHABLE_OUTCOMES` (`connect_error`, `timeout_pre`) beside the billable/zero-cost sets.
- `src/id_detector/paid_clip.py` — `PaidScanResult` records every live dispatch outcome in order
  (`outcomes`), the terminal-provider outcome that stopped the sweep (`provider_stopped`) and an
  `unreachable` count; a terminal-provider outcome now stops the primary at once on both the
  transport-error and the error-body paths (§2.3.3; 0a-ii review P2-4); the summary line reports
  `cached` and `not sent` truthfully instead of counting un-dispatched windows as cached; module
  docstring updated for the recipe-primary role.
- `src/id_detector/cli.py` — the §2.3.5 matrix: `_achieved` (exact integer fraction test) and
  `_run_status` (`partial` outranks `degraded`); `provider_unavailable` only for a
  terminal-provider or unreachable outcome (or an unconfigured adapter) before any resolved
  attempt, with the outcome as `reason`; `--allow-degrade` / `allow_degrade=` restarts the request
  as the Free recipe before any paid work (`requested_recipe_id` stays Deep, `achieved=free`,
  `algorithm_version=fusion:1`, status `degraded`, reason `provider_unavailable`); the Deep
  secondary replaces the old gap fill — candidates, capacity, schedule, one Shazam pass over the
  scheduled windows through the existing injected client, `secondary_*` counts, one re-fuse; the
  Free primary's achieved fraction (frozen windows resolved ≥ 80 %); `status`, `reason`,
  `achieved` written to the journal and `tracklist.json`; `_acquire` carries them forward when it
  re-exports; the summary line starts with the status; the frozen-window plan is computed once for
  both recipes.
- `src/id_detector/contracts.py` — journal `status` vocabulary is the matrix's (`complete`,
  `degraded`, `partial`, `provider_unavailable`, `budget_exhausted`, `failed`, `cancelled`;
  `succeeded` retired) plus the new `achieved: Literal["free","deep"] | None`.
- `src/id_detector/journal.py` — `InvocationTimer.entry(..., achieved=...)`.
- `src/id_detector/present/exports.py` — `tracklist.json` carries `status`, `reason`, `achieved`
  (null when a re-export does not know them).
- `src/id_detector/webapp/runner.py` — every non-zero `_analyse` exit (1, 3, 4, 5) fails the
  browser job with its matrix meaning; previously only 3 did.
- `docs/schemas/invocation_journal_entry.schema.json`, `tests/golden/invocation_journal_entry.json`
  — regenerated / updated for the new vocabulary and field.
- `tests/fakes/scripts/{quota-first,quota-midrun,primary-thin,secondary-thin,free-thin,all-timeout-pre}.json`
  — the matrix scenarios; `tests/fakes/scripts/money-refunds.json` reordered so the terminal
  `http_401` comes last (it now stops the sweep).
- `tests/test_phase0a_crash_cache.py`, `tests/test_phase0a_money.py` — assertions moved to the
  matrix where the interim 0a-i/0a-ii rule differed (see "Plan ambiguities"): all-`malformed` and
  the 429 storm are `partial`/exit 0; all-401 makes one dispatch, not seven; the Deep secondary's
  two Shazam clips are expected where the old tests asserted zero Shazam requests.

### Tests added — `tests/test_phase0a_status.py` (23)

Every matrix row end to end through `_analyse` on the 60 s fixture: `http_401` first, `quota_error`
first and an unreachable sweep → exit 3, `provider_unavailable`, the outcome as `reason`,
`achieved=null`, nothing spent, no `present/` written, zero Shazam requests, and one dispatch for the
terminal outcomes; `quota_error` after two matches → `partial` / `provider_unavailable_midrun`, three
dispatches, two units spent, the secondary still allocated; AudD 6/7 → `partial` /
`primary_not_achieved` and billed; a 429 storm → `partial`, unspent, secondary over the blank mix;
Free 5/7 → `partial`, Free 7/7 → `complete`; secondary 0/2 → `degraded` /
`secondary_not_achieved` with the primary paid; the gate scenario → `complete` with 36 750 / 35 000
/ 4 and the secondary allocated = resolved = 2; cap 0 → exit 4, `budget_exhausted`,
`achieved=null`, no result. `--allow-degrade`: for a refused credential and for an unreachable
provider → `degraded`, `achieved=free`, `requested_recipe_id` = Deep, `algorithm_version=fusion:1`,
zero further AudD attempts, seven Shazam clips, $0, reservation recorded and released; never after
paid work (mid-run quota stays `partial`) and never for a Free request; the CLI flag is threaded and
off by default. Status arithmetic (`_achieved`, `_run_status` precedence). The scheduler on
synthetic records: class order then start order, ≥ 4 s eligibility, capacity cut, no duplicates,
blanks under suppressed episodes, rescan and transformed windows never queued, `blank_spans` covering
the unresolved tail after a stopped primary, the capacity formula. The web runner fails exit 4 and 5
without attaching a result. `tracklist.json` fields asserted on every shown outcome.

---

## Cycle 0a-iii — Security and dead paths (local)

### File-by-file changes

- `src/id_detector/cli.py` — the `run_paid_scanners` call site and import are gone (E-H8's
  second charge is unreachable); the Free path's paid-clip lever (`want_clips`, dead since 0a-ii)
  is gone with it and only the Panako lever remains in phase 2; `--engine acrcloud` is refused with
  exit 2 (E-L4) and `--engine` accepts only `audd`; `--i-own-this-audio-or-have-permission` is
  hidden and inert (a notice says so); `_windows_in_spans` keeps only frozen generation-0
  untransformed windows (E-M9).
- `src/id_detector/present/server.py` — `upload_consent` is no longer read from JSON or form
  bodies (E-H8); loopback CSRF (U-F5): a per-server token minted in `make_server`, served by
  `GET /csrf`, embedded as a hidden `csrf_token` field in the home/new forms and as `CSRF_TOKEN` in
  the job page's cancel `fetch` (`X-CSRF-Token`); every `POST` is refused (403) unless `Host` ∈
  {`127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>`} and any `Origin` is `http://` one of
  those; `/analyse` and `/jobs/<id>/cancel` additionally require the token (header, form field or
  JSON field); a refused body is drained before the 403 so Windows delivers the response rather than
  an aborted connection.
- `src/id_detector/webapp/jobs.py` — `upload_consent` removed from `Job`, `JobContext` and
  `JobManager.submit`.
- `src/id_detector/webapp/runner.py` — `local_index_label="default"` (with the matching index root
  and Panako tool dir) is passed to `_analyse` whenever the job asked for `build_index` (E-H9,
  D3); the consent argument is no longer passed; the build step and the query share one set of
  constants.
- `src/id_detector/providers/base.py` — `DEFAULT_DISABLED_HINT_CONNECTORS = {"tl1001"}`; the
  `[hints]` loader starts from it and an explicit `tl1001 = true` opts in.
- `src/id_detector/config_template.py`, `idea.example.toml` — `tl1001 = false` with the reason.
- `src/id_detector/hints/pipeline.py` — `_CONNECTOR_SWITCHES` maps the `tl1001` switch onto the
  `tl1001_search` connector (the names never matched, so the switch had been a silent no-op); the
  disabled branch now appends its empty output to `status_outputs` — before, honouring any switch
  crashed `_parse_counts`'s strict `zip` and with it the whole hints stage.
- `scripts/spike_ingest_vps.sh` — S2: metadata + full download of a SoundCloud, a Mixcloud and a
  YouTube URL through the pinned yt-dlp, direct and (YouTube) via an optional proxy, appending a
  sanitised table (no URLs, titles, handles or long IDs — `docs/` is audited) to
  `docs/spikes/ingest-vps.md`; refuses `IDEA_TEST_MODE=1`.
- `scripts/spike_shazam_vps.py` — S3: real Shazam recognitions at a fixed `--ceiling` for
  `--minutes` over synthetic tone clips (real signatures; only the transport verdict matters), a
  per-minute table, first-429 minute and sustained clean rate appended to
  `docs/spikes/shazam-vps.md`; refuses `IDEA_TEST_MODE=1` and pytest.
- `tests/test_stage10_webapp.py`, `tests/test_stage9_config.py` — the existing POST tests fetch the
  token; the config defaults include `tl1001`.

### Tests added — `tests/test_phase0a_security.py` (14)

`POST /analyse` without a token, with a foreign `Origin`, with a non-loopback `Host` or with a wrong
token is 403 and creates no job; the header token with a loopback `Origin` creates one (200) and the
hidden form field works (303); cancel needs the token; `/rescan` is origin-gated on the read-only
server too, and the read-only server still has no `/analyse`; the served home/new/job pages carry
exactly the token `/csrf` serves. `upload_consent: true` in a body creates a job that has no such
attribute and `submit(upload_consent=)` is a `TypeError`. `cli` has no `run_paid_scanners`; with
consent open and both paid families enabled the Free recipe pays nothing and neither recipe times a
`scan_ms` stage or counts `paid_matches`. `--engine acrcloud` refused, `--engine other` unknown,
`--engine audd` accepted, the consent flag inert and absent from `--help`. `tl1001` disabled by
default, by an empty `[hints]`, and by a missing file; `tl1001 = true` opts in; through `run_hints`
with a recording `MockTransport` on a SoundCloud source the `tl1001_search` connector is `disabled`
and 1001tracklists is never contacted, while an empty switch set does contact it. `_windows_in_spans`
drops a transformed and a rescan window. The web runner passes `local_index_label="default"`,
`index_root` and `panako_tool_dir` when `build_index` is set and `None` otherwise, and never passes
`cli_confirmation`. Both spike scripts exist, compile, name their reports and refuse a test context.

---

## Required command outputs

### 1. `uv run pytest -q`

```text
.venv\Lib\site-packages\pydub\utils.py:14
  C:\Users\natha\Documents\Music\id-detector\.venv\Lib\site-packages\pydub\utils.py:14: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
657 passed, 93 deselected, 1 warning in 129.10s (0:02:09)
```

### 2. `uv run ruff check .`

```text
All checks passed!
```

### 3. `uv run ruff format --check .`

```text
221 files already formatted
```

(`uv run ruff format .` was run first: `221 files left unchanged`.)

### 4. `uv run python scripts/audit_fixtures.py`

```text
audited 371 files
fixture audit passed
```

### 5a. 0a-iv gate (PowerShell, verbatim from the plan; `pwsh` is not installed, so the Windows
PowerShell host the plan names ran it: `powershell -NoProfile -ExecutionPolicy Bypass -File`)

```powershell
$env:IDEA_TEST_MODE = "1"; $env:IDEA_FAKE_SCRIPT = "tests/fakes/scripts/gate0a-deep.json"
$w = "$env:TEMP\idea-gate0a"; Remove-Item -Recurse -Force $w -ErrorAction SilentlyContinue
uv run idea analyse tests/fixtures/audio/tone-60s.wav --recipe deep --fake-providers audd,shazam --work-root $w
uv run python scripts/assert_journal.py --work-root $w --expect status=complete algorithm_version=targeting:0,fusion:1 usd_e6_reserved=36750 usd_e6_spent=35000 usd_e2_spent=4
```

```text
complete; 6 matches; 0 failures; 2 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=C:\Users\natha\AppData\Local\Temp\idea-gate0a\a63408abeea9adf1c0d4452a6509d9ee4e5399d192f32ee7307bbcef39c83e27\e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d\present\tracklist.json
journal assertion passed at C:\Users\natha\AppData\Local\Temp\idea-gate0a\a63408abeea9adf1c0d4452a6509d9ee4e5399d192f32ee7307bbcef39c83e27\e2179d997e03bdd132bbd451535df92691a581bdeb9b0a6bb73358b54b08575d\invocations.jsonl:1
gate exit=0
```

### 5b. `uv run pytest tests/test_phase0a_status.py -q`

```text
.......................                                                  [100%]
23 passed, 1 warning in 28.75s
```

### 5c. `uv run pytest tests/test_phase0a_security.py -q`

```text
..............                                                           [100%]
14 passed, 1 warning in 11.93s
```

### 5d. Local mode still serves — `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

### 5e. `--allow-degrade` through the real CLI (fakes; not a plan gate, evidence only)

```text
uv run idea analyse tests/fixtures/audio/tone-60s.wav --recipe deep --allow-degrade --fake-providers audd,shazam --work-root <tmp>   # IDEA_FAKE_SCRIPT=tests/fakes/scripts/all-http-401.json
degraded (provider_unavailable); 0 matches; 0 failures; 7 physical attempts; 1 generations (stop=max_generations); 0 episodes; tracklist=...\present\tracklist.json
uv run python scripts/assert_journal.py --work-root <tmp> --expect status=degraded achieved=free reason=provider_unavailable algorithm_version=fusion:1 usd_e6_spent=0 exit_code=0 paid_requests=1
journal assertion passed
uv run idea analyse tests/fixtures/audio/tone-60s.wav --recipe deep --fake-providers audd,shazam --work-root <tmp2>   # same script, no --allow-degrade
exit=3
uv run python scripts/assert_journal.py --work-root <tmp2> --expect status=provider_unavailable reason=auth_error exit_code=3 usd_e6_spent=0
journal assertion passed
```

### 6. `git status --short`

```text
 M docs/schemas/invocation_journal_entry.schema.json
 M idea.example.toml
 M src/id_detector/cli.py
 M src/id_detector/config_template.py
 M src/id_detector/contracts.py
 M src/id_detector/hints/pipeline.py
 M src/id_detector/journal.py
 M src/id_detector/money.py
 M src/id_detector/paid_clip.py
 M src/id_detector/present/exports.py
 M src/id_detector/present/server.py
 M src/id_detector/providers/base.py
 M src/id_detector/webapp/jobs.py
 M src/id_detector/webapp/runner.py
 M tests/fakes/scripts/money-refunds.json
 M tests/golden/invocation_journal_entry.json
 M tests/test_phase0a_crash_cache.py
 M tests/test_phase0a_money.py
 M tests/test_stage10_webapp.py
 M tests/test_stage9_config.py
?? scripts/spike_ingest_vps.sh
?? scripts/spike_shazam_vps.py
?? src/id_detector/secondary_targeting.py
?? tests/fakes/scripts/all-timeout-pre.json
?? tests/fakes/scripts/free-thin.json
?? tests/fakes/scripts/primary-thin.json
?? tests/fakes/scripts/quota-first.json
?? tests/fakes/scripts/quota-midrun.json
?? tests/fakes/scripts/secondary-thin.json
?? tests/test_phase0a_security.py
?? tests/test_phase0a_status.py
```

(plus `?? docs/reviews/build-0a-iv-0a-iii.md`, this report, written after the snapshot.)

### 7. `git diff --stat`

```text
 docs/schemas/invocation_journal_entry.schema.json |   2 +-
 idea.example.toml                                 |   4 +-
 src/id_detector/cli.py                            | 470 +++++++++++++---------
 src/id_detector/config_template.py                |   4 +-
 src/id_detector/contracts.py                      |   6 +-
 src/id_detector/hints/pipeline.py                 |  17 +-
 src/id_detector/journal.py                        |   2 +
 src/id_detector/money.py                          |   4 +
 src/id_detector/paid_clip.py                      |  68 +++-
 src/id_detector/present/exports.py                |  13 +
 src/id_detector/present/server.py                 | 111 ++++-
 src/id_detector/providers/base.py                 |  17 +-
 src/id_detector/webapp/jobs.py                    |  10 -
 src/id_detector/webapp/runner.py                  |  40 +-
 tests/fakes/scripts/money-refunds.json            |   9 +-
 tests/golden/invocation_journal_entry.json        |   2 +-
 tests/test_phase0a_crash_cache.py                 |  26 +-
 tests/test_phase0a_money.py                       |  22 +-
 tests/test_stage10_webapp.py                      |  25 +-
 tests/test_stage9_config.py                       |   4 +-
 20 files changed, 569 insertions(+), 287 deletions(-)
```

Untracked additions: `secondary_targeting.py` 180 lines, the two spike scripts 372, the two test
files 972, six fake scripts 69. Two cycles in one pass put the change above the plan's 1,500-line
split guideline; the split point, if wanted, is the 0a-iii server/runner/config files, which the
0a-iv code does not depend on.

---

## Plan ambiguities resolved

1. **Row 1's "connect_error after retries".** Read as the unreachable class — `connect_error`
   and `timeout_pre` (both are `ProviderUnavailable`, pre-receipt, cost 0) — with **zero resolved
   attempts** at the end of the sweep. Retries themselves are 0b-i, so today it is "after the one
   attempt". A sweep that resolves nothing on non-terminal, non-unreachable outcomes (a 429 storm,
   all-`malformed`, all-`http_5xx`) is **row 3**: AudD 0/N < 95 % → `partial`, exit 0, spent
   whatever the outcomes cost. This moved two earlier regression assertions
   (`test_throttled_run_refunds_every_unit_and_bills_nothing`: exit 3 → `partial`/$0;
   `test_identity_less_audd_result_is_malformed_and_never_cached`: exit 3 → `partial`/35 000 e6)
   — the 0a-i report had noted its broader "no resolved attempt → 3" rule was interim.
2. **`reason` tokens.** `provider_unavailable` carries the outcome (`auth_error`, `quota_error`,
   `connect_error`, `timeout_pre`, or `not_configured` when the adapter could not be built, e.g. no
   token); `partial` → `provider_unavailable_midrun` | `reservation_exhausted` |
   `primary_not_achieved`; `degraded` → `secondary_not_achieved` | `provider_unavailable` (the
   `--allow-degrade` substitution).
3. **"Nothing spent" on row 1** holds by construction (every terminal/unreachable outcome is
   zero-cost). If a billable ambiguous outcome preceded the terminal one (`http_500` then `401`) the
   journal records the true spend rather than a hard-coded zero — the stance the 0a-ii review took
   (P2-5).
4. **`achieved` and `algorithm_version` after a degrade.** `requested_recipe_id` stays Deep;
   `achieved=free`; `algorithm_version` is the achieved recipe's (`fusion:1`), because it describes
   the result that was produced and §3.4 gates serving on it. `achieved` is `null` for
   `provider_unavailable`, `budget_exhausted`, `failed`, `cancelled` (no result). If the Free sweep
   of a degraded request is itself < 80 %, the run is `partial`/`primary_not_achieved`, not
   `degraded`.
5. **The secondary runs for every Deep run that reaches the fuse**, including `partial` ones (it is
   free Shazam and improves a thin result); `partial` outranks `degraded`. Re-fuse happens once
   whenever the secondary produced observations (no-match evidence informs the gap statistics).
6. **"Blank" and candidate spans.** Blank = the mix not covered by any listed (non-suppressed)
   episode's evidence hull, not the fuse's `gaps` records — those only describe `no_evidence`
   stretches between observations, so the tail after a stopped primary (an "unresolved boundary")
   would never have been probed. Candidate spans are the evidence hull (first support start → last
   support end) because `best_start_ms`/`best_end_ms` cross on short episodes.
   `suppressed_challengeable` counts `episode.evidence` ids that are not hint ids as selected votes.
   `listed_not_confident` uses the codebase's existing confidence rule (`likely`/`verified` badge or
   a corroboration flag).
7. **`source_changed` (exit 5)** is not added to the journal enum: re-fetch detection is 1a-ii.
8. **The whole-file scanner.** The `run_paid_scanners` *call site* is removed as the plan says; the
   module, `require_upload_permission` and `AudDAdapter.scan_file` stay for M2 7f's tagged deletion.
   The Free path's paid-clip lever (`want_clips`) went with it — `paid_clip.py`'s own note and the
   0a-ii review (P2-8) scheduled its removal for this cycle. `--i-own-this-audio-or-have-permission`
   is hidden and inert rather than deleted so old command lines keep working; `_analyse` keeps its
   `cli_confirmation` parameter (tests use it) but nothing reads it any more.
9. **CSRF scope.** The `Host`/`Origin` gate covers every `POST`; the token is required on the app
   routes (`/analyse`, `/jobs/<id>/cancel`). `/rescan` gets the gate only: the result page that
   posts to it is a static file that cannot carry the token, and the route is scheduled for removal
   (U-F11). Browsers always send `Origin` on a cross-site `POST`, so the gate alone already stops
   another website reaching `/analyse`.
10. **`tl1001` default-disabled needed a mapping fix.** The `[hints]` switch names and the
    pipeline's connector names differ (`tl1001` vs `tl1001_search`), so the switch had never
    reached the connector; `_CONNECTOR_SWITCHES` maps it. The `mixcloud` switch has the same
    mismatch (`mixcloud_graphql`, `mixcloud_description`) — observed, left alone as out of scope.
11. **`_windows_in_spans`** is fixed and tested as the plan names it, but the provisional scheduler
    superseded its only production caller (the paid-first gap fill); it now has no runtime caller
    and belongs with M2 7f's dead-lever cleanup.
12. **Web runner exit codes.** The matrix's non-zero exits (4, 5) now fail a browser job like 3
    does; before, a `budget_exhausted` run would have been reported as a success with no result.

## Bugs found and fixed on the way (each with a regression test)

- `hints/pipeline.py`: a disabled connector appended a status without an output, so the first
  honoured `[hints] x = false` aborted the hints stage with `zip() argument 2 is shorter` — masked
  until now by the name mismatch above. Covered by
  `test_hint_pipeline_maps_the_tl1001_switch_onto_the_search_connector[disabled0]`.
- `present/server.py`: refusing a `POST` before its body was read made Windows report the 403 as an
  aborted connection under load (seen once in the full suite); the body is now drained first.
  Covered by `test_post_analyse_requires_the_token_and_a_loopback_origin` in the full run.
- `paid_clip.py`: the sweep summary counted un-dispatched windows as "cached" (0a-ii P2-8, cosmetic)
  — now `cached` / `not sent`; asserted by the all-401 crash-cache test.

## Could not do

- **`pwsh scripts/...`** — PowerShell 7 is not installed; the plan's "PowerShell host" note allows
  Windows PowerShell 5.1, which ran both the gate and the smoke script (outputs above).
- **S2/S3 spike scripts were not executed** — they are live by definition (real platforms, real
  Shazam from a VPS egress). They are syntax-checked (`sh -n`, `py_compile`), lint-clean, and their
  `IDEA_TEST_MODE=1` / pytest refusal is tested; the owner runs them per Phase S.
- **Retries** — "after retries" cannot be exercised until 0b-i lands the bounded AudD retry loop;
  the classification is in place for it.

BUILD: COMPLETE

---

## Review + fix pass

Adversarial review of the uncommitted 0a-iv + 0a-iii change against `docs/PLAN-v2.md`
§2.3.1–§2.3.5, §3.4, §3.5, §5 (both cycle entries) and §6.1, then the fixes. Every command below
was re-run here, never trusted from the report. No live provider call was made: every pipeline run
used injected fakes or `IDEA_TEST_MODE=1 --fake-providers audd,shazam`.

### Findings

#### P0-1 — `--allow-degrade` restarted a Deep run as Free *after* AudD had already been billed

`src/id_detector/cli.py:665-706` (pre-fix). The row-1 branch gates the degrade on
`primary_clip.resolved == 0`, but an **ambiguous-but-billable** outcome (`http_5xx`, `malformed`,
`timeout_post`) charges a unit without resolving anything. With a sweep of `http_500` then
`http_401` (new fake `tests/fakes/scripts/spend-then-401.json`) the run charged 5 000 usd_e6, then
restarted the whole mix as the Free recipe, sent seven Shazam clips and journalled
`degraded` / `achieved=free` / exit 0 — a status §3.5 settles at **100 %** and §3.4 serves with
`accept_degraded`, on top of real spend. Plan §2.3.5 row 1 says the restart happens "**before any
paid work**".

Fix (`cli.py:684-693`): the restart additionally requires `primary_clip.billable_units == 0`. When a
unit has already been billed the run ends `provider_unavailable` / exit 3 with the true spend
recorded, and a progress line states that `--allow-degrade` was not applied and why.
Regression test: `tests/test_phase0a_status.py::
test_allow_degrade_never_restarts_after_a_billable_outcome_was_already_spent` — verified failing
against the pre-fix tree (`assert 0 == 3`, `degraded (provider_unavailable); 7 matches`).

#### P1-2 — every POST refusal that answers without reading the body resets the connection

`src/id_detector/present/server.py`. The builder added `_drain_body()` for the cross-site 403 only.
Four other POST paths still answered with the request body in flight: the `route != "/rescan"` 404
(which is exactly how a read-only server answers `POST /analyse`), the `/rescan` "bad length" 400,
the `/analyse` over-8 KiB 400, and `_handle_job_cancel` (403/404). On Windows that is delivered to
the client as an aborted connection, not as the status code.

Measured, not inferred: `tests/test_stage10_webapp.py::test_read_only_server_has_no_analyse_routes`
failed **2 of 10** runs on the working tree and took the full suite down once here
(`httpx.ReadError: [WinError 10053]`, so the builder's "657 passed" is not reproducible); a
`git worktree` at HEAD (`b48768b`) failed **1 of 10**, so the defect predates the cycle — but the
cycle owns the drain mechanism and applied it to one path out of five.

Fix: `_drain_body` is now idempotent per request (`server.py:1130-1135`, `1172-1184`; draining twice
on a keep-alive connection would block on the *next* request's bytes), `do_POST` resets the flag
(`server.py:1377`), and the drain is called on all remaining refusal paths (`server.py:1380`,
`1394`, `1399`, `1453`, `1518`); the two body readers mark the body consumed (`1402`, `1446`).
Regression test: `tests/test_phase0a_security.py::
test_every_refused_post_delivers_its_answer_instead_of_resetting_the_connection` — 25 iterations
over all five refusal shapes with a real body; verified failing with the 404 drain removed. The
previously flaky test now passes 15/15.

#### P1-3 — `[hints] mixcloud = false` was a silent no-op

`src/id_detector/hints/pipeline.py:383-390`. The cycle introduced `_CONNECTOR_SWITCHES` precisely
because the `[hints]` switch names and the pipeline connector names differ, and fixed `tl1001` →
`tl1001_search`. The report observed the identical mismatch for `mixcloud` →
`mixcloud_graphql` / `mixcloud_description` and left it, so a switch the packaged template documents
(`config_template.py`, `idea.example.toml`) still contacted Mixcloud twice per run when set to
`false`. Fixed by mapping both connectors. Regression test: `tests/test_phase0a_security.py::
test_hint_pipeline_maps_the_mixcloud_switch_onto_both_mixcloud_connectors` (parametrised over
disabled/enabled, asserts connector state **and** that no `mixcloud` host is contacted) — verified
failing without the two map entries.

#### P2-4 — the 0a-iv gate is not repeatable on Windows (plan-owned command; not fixed)

The gate's `Remove-Item -Recurse -Force $w -ErrorAction SilentlyContinue` cannot delete
`…/recognise/invocations/<id>/raw/<64-hex>.json`: those paths exceed `MAX_PATH`, Windows
PowerShell 5.1 reports *"Could not find a part of the path"* for each, and `-ErrorAction
SilentlyContinue` hides it. Measured: 56 files before, **11 surviving** — all of them AudD/Shazam
raw-response cache entries. On the next run those 7 AudD entries are cache hits, only the cached
`no_match` is re-queried (the default `--refresh-states no_match`), and the gate reports
`complete` with `paid_requests=1`, `paid_cache_hits=6`, `usd_e6_spent=5000` → the money assertions
fail. Reproduced twice; the first run in a genuinely fresh work root passes (output below).

Not fixable inside this cycle: it needs either the plan's gate command changed (a unique `$w`, or
`cmd /c rd /s /q \\?\$w`) or a shorter on-disk artefact layout — both outside 0a-iv/0a-iii, and
`docs/PLAN-v2.md` is not editable here. **Owner action:** run the gate against a fresh
`--work-root` each time.

#### P2-5 — `provider_unavailable` can carry a non-zero spend

The same sweep as P0-1 without `--allow-degrade` journals `provider_unavailable` with
`usd_e6_spent=5000`, while plan §2.3.5 row 1 says "nothing spent". Recording the true spend is the
right stance (builder ambiguity 3, 0a-ii review P2-5) — noted so it is understood that the row's
"nothing spent" holds only when *every* outcome in the stopped sweep was zero-cost.

#### P2-6 — `_windows_in_spans` has no production caller

`cli.py:373`. Fixed and tested exactly as the plan names it (E-M9), but the provisional scheduler
superseded its only call site. Dead until M2 7f's tagged deletion (builder note 11); left in place
because the plan's 0a-iii scope asks for the fix, not the removal.

#### P2-7 — private import across modules

`src/id_detector/secondary_targeting.py:20` imports `_merge` from `scan_targeting`. Harmless today;
`targeting:1` (1b-i) rewrites this module anyway.

#### P2-8 — `_drain_body` is bounded at 1 MiB

A refused body larger than that still leaves bytes on the socket. Both POST readers cap at 8 KiB, so
no honest client reaches it, and the bound is deliberate (an unbounded drain is a DoS lever).

#### P2-9 — `benchmark shortlist` still wires consent into the whole-file scanner

`cli.py:2149` passes `--i-own-this-audio-or-have-permission` into `run_shortlist` →
`run_paid_scanners`. Outside 0a-iii's scope, which names the `analyse` call site; tagged for
M2 7f alongside `scan.py`, `require_upload_permission` and `AudDAdapter.scan_file`.

#### P2-10 — retiring `succeeded` invalidates older journal lines

`contracts.py:925-940`. Journal entries written before this cycle no longer validate against
`InvocationJournalEntry` (status `succeeded`, missing `achieved`). Nothing re-reads them with the
model — `scripts/assert_journal.py` parses raw JSON — so local history stays readable; noted for the
hosted migration.

### Checked and found correct (no finding)

- **Every §2.3.5 row**, including exit codes and that `partial` never masks a crash: an exception
  still journals `failed`/1 and `cancelled`/130 with `achieved=null` (`cli.py:1004-1032`), and
  `_run_status` reaches `partial` only from the four explicit predicates. Row 2 can only be entered
  with `resolved ≥ 1` because row 1 returns first. `_achieved` is exact integer arithmetic and
  treats an empty plan as achieved.
- **`targeting:0`**: `C = ⌈duration_ms × cpm / 60 000⌉`; a window is queued at most once (`seen`
  keyed by `window.id`); only generation-0 untransformed windows are eligible; capacity truncates
  mid-class; `blank_spans` covers the tail a stopped primary leaves, which the fuse's `gaps` never
  describe.
- **CSRF/Origin**: a cross-site form POST always carries `Origin`, so the gate refuses it; `Origin:
  null` (sandboxed iframe / `data:`) fails the `http` scheme test; a missing `Origin` still has to
  pass the loopback `Host` test, and `/analyse` and `/jobs/<id>/cancel` additionally require the
  token; DNS rebinding can read `GET /csrf` but its POST then carries a non-loopback `Host` and is
  refused; the token is compared with `secrets.compare_digest` and an unset token fails closed.
- **`upload_consent`**: unreachable from every request shape — not read from JSON or form bodies,
  absent from `Job`/`JobContext`, a `TypeError` on `JobManager.submit`, and the runner no longer
  passes `cli_confirmation`; the only surviving `require_upload_permission` callers
  (`scan_file`/`scan_url`/`execute_job`) have no path from `analyse`.
- **Spike scripts cannot run inside tests**: `spike_shazam_vps.py` refuses `IDEA_TEST_MODE=1` and
  `PYTEST_CURRENT_TEST` before parsing arguments; `spike_ingest_vps.sh` refused only the former, so
  the same `PYTEST_CURRENT_TEST` guard was added (`scripts/spike_ingest_vps.sh:18-22`) and the
  existing gate test now asserts both names appear.
- Frozen artefacts untouched: `profiles/`, `data/corpus/`, `recipes.py`, `pricing.toml` are not in
  the diff, and the gate's `recipe_id`-derived figures still reconcile.

### Files changed by this pass

- `src/id_detector/cli.py` — P0-1.
- `src/id_detector/present/server.py` — P1-2.
- `src/id_detector/hints/pipeline.py` — P1-3.
- `scripts/spike_ingest_vps.sh` — pytest guard.
- `tests/fakes/scripts/spend-then-401.json` — new.
- `tests/test_phase0a_status.py`, `tests/test_phase0a_security.py` — four new tests (+4 cases).

### Final gate outputs (re-run after the fixes)

`uv run pytest -q`

```text
661 passed, 93 deselected, 1 warning in 130.42s (0:02:10)
```

`uv run ruff check .`

```text
All checks passed!
```

`uv run ruff format --check .`

```text
222 files already formatted
```

`uv run python scripts/audit_fixtures.py`

```text
audited 372 files
fixture audit passed
```

0a-iv gate (PowerShell 5.1 via `powershell -NoProfile -ExecutionPolicy Bypass -File`, work root a
fresh `$env:TEMP\idea-gate0a-<random>` — see P2-4):

```text
complete; 6 matches; 0 failures; 2 physical attempts; 1 generations (stop=max_generations); 3 episodes; tracklist=<workroot>\present\tracklist.json
journal assertion passed at <workroot>\invocations.jsonl:1
gate exit=0
```

`uv run pytest tests/test_phase0a_status.py -q`

```text
24 passed, 1 warning in 31.09s
```

`uv run pytest tests/test_phase0a_security.py -q`

```text
17 passed, 1 warning in 17.59s
```

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_serve.ps1`

```text
smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'
```

`tests/test_stage10_webapp.py::test_read_only_server_has_no_analyse_routes` × 15 after the P1-2 fix:
15 passed (2 of 10 failed before it).

REVIEW: OK_TO_COMMIT

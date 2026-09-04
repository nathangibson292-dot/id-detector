# Stage 4a - Hints

## What was built

### File map

- `src/id_detector/hints/parse.py`: deterministic comment/tracklist parser with component-count
  timestamps, context-gated dotted cues, cue ranges, ordered separators, block-consistent no-space
  hyphens, mega-comment splitting, questions, answers/corrections, flags, specificity, and temporal
  precision. Persisted connector inputs enforce the two-line, non-decreasing, in-duration block
  rule.
- `src/id_detector/hints/relations.py`: bounded SoundCloud mention replies, explicit parent replies,
  correction inheritance/confidence scaling, fuzzy cross-connector copy detection, and stable
  provenance groups.
- `src/id_detector/hints/mirrors.py`: conservative mirror release predicate requiring source
  identity/uploader-date agreement, duration within 2%, and at least two distinct
  timeline/provenance agreements, or explicit manual confirmation.
- `src/id_detector/hints/pipeline.py`: platform-specific primary-flow orchestration, durable
  connector jobs, ignored raw/result caching, parser/status materialisation, completion sidecars,
  accepted-block counts, and non-blocking connector failure recording.
- `src/id_detector/hints/connectors/base.py`: bounded HTTP helpers, five-failures-per-minute circuit
  breaker, connector context/result serialization, and secret-redacted raw JSON caching.
- `src/id_detector/hints/connectors/soundcloud.py`: api-v2 resolve/comments pagination, 5,000-item
  cap, client-id discovery/cache/refresh, `next_href` client-id restoration, uploader recognition,
  and local ingest-description reuse.
- `src/id_detector/hints/connectors/youtube.py`: local info-description/chapters and bounded
  `python -m yt_dlp --write-comments` extraction for 200 parents plus 20 replies per parent, with
  parsed-count enforcement and nonzero-exit rejection.
- `src/id_detector/hints/connectors/mixesdb.py`: URL search, both MediaWiki page response shapes,
  main-slot revision parsing, Tracklist section lines, and Player mirror extraction.
- `src/id_detector/hints/connectors/mixcloud.py`, `tl1001.py`, `pointer.py`, and `manual.py`:
  optional GraphQL sections, discovery-only 1001TL search, strict HTTPS pointer import with exact
  hosts/redirect/body/time limits, and bounded strict-UTF-8 manual tracklists.
- `src/id_detector/jobs.py`: connector-job ensure/lease/heartbeat/checkpoint/finish/reset/list/get
  lifecycle and stale-lease recovery on the existing `connector_jobs` table.
- `src/id_detector/fuse/identity.py` and `fuse/episodes.py`: work-only text candidates, one
  provenance-collapsed authoritative hint vote, audio-only version tier, hint/question rescan
  requests, and hint evidence flags. Stage 4a writes requests but does not execute later
  generations.
- `src/id_detector/present.py`: `hint_supported` JSON fields and `+HINT` Markdown badges.
- `src/id_detector/benchmark/hints.py` and `benchmark/corpus.py`: paired audio-only/fused gate,
  duration-weighted union of badge-eligible evidence, one-sided set-cluster bound, 1 pp precision
  non-inferiority, and hint policy in the benchmark config hash.
- `src/id_detector/cli.py` and `README.md`: `analyse --tracklist/--no-hints`, `hints`, and
  `benchmark hints` commands plus usage.
- `tests/fixtures/hints/`: authored, audit-safe connector fixtures for SoundCloud, YouTube,
  MixesDB, Mixcloud, 1001TL, and pointer import.
- `tests/test_stage4a_parser.py`, `test_stage4a_connectors.py`,
  `test_stage4a_relations_fusion.py`, and `test_stage4a_pipeline.py`: synthetic and derived parser
  corpus, positive/negative connector and security boundaries, relations/provenance, mirror gate,
  fusion policy, artifact/job determinism, coverage union, CLI, and missing-truth gate tests.

The former module `src/id_detector/hints.py` became the `id_detector.hints` package while keeping
its public parsing imports compatible. `yt-dlp>=2026.8.19` was already present in the uv project
from the earlier ingestion work and is invoked through `python -m yt_dlp`; the installed lock
resolved 2026.08.19. `shazamio==0.8.1` and `shazamio-core==1.1.2` remain exactly pinned. No new
secret or environment variable is required.

## How to run it

From the repository root in PowerShell:

```powershell
uv sync --dev

uv run id-detector hints <PUBLIC_MIX_URL>
uv run id-detector hints <PUBLIC_MIX_URL> --tracklist .\tracklist.txt
uv run id-detector hints <PUBLIC_MIX_URL> --confirm-mirror <ALLOWLISTED_MIRROR_URL>
uv run id-detector analyse <PUBLIC_MIX_URL>
uv run id-detector analyse <PUBLIC_MIX_URL> --tracklist .\tracklist.txt
uv run id-detector analyse <PUBLIC_MIX_URL> --no-hints

uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv lock --check
uv run python scripts/audit_fixtures.py
```

The exact six-set live connector loop used was:

```powershell
$links = Get-Content data/local/source_links.json -Raw -Encoding utf8 | ConvertFrom-Json
1..6 | ForEach-Object {
  $ref = 'source-dev1-set-{0:D3}' -f $_
  uv run id-detector hints $links.$ref --work-root data/local/work-hints-live
}
```

Raw responses are in ignored `data/local/hints/<connector>/...`; per-media parsed artifacts are
under the selected work root at `hints/hints.jsonl` and `hints/connector_status.json`.

## What was verified and how

### Parser, connectors, relations, fusion, and determinism

The default test run covers every case in
`data/fixtures/hints/synthetic/parsing_traps.json`, every derived JSONL under
`data/fixtures/hints/derived/`, negative questions, invalid/reversed blocks, both MixesDB revision
shapes, all connector fixtures, pointer host/redirect/2 MB limits, connector cursor/caps, mirror
quarantine, correction/reply/copy relations, work-only voting, version non-escalation, question
clusters, missing-cache recovery, sidecars, and byte-identical cached reruns.

Original pre-review `uv run pytest -q` output:

```text
........................................................................ [ 25%]
........................................................................ [ 50%]
........................................................................ [ 75%]
.......................................................................  [100%]
287 passed, 3 deselected, 1 warning in 59.03s
```

The warning is pydub's Python 3.13 `audioop` deprecation; this project uses Python 3.12.

Original pre-review `uv run ruff check .` output and supplementary checks:

```text
All checks passed!
106 files already formatted
Resolved 53 packages in 1ms
audited 164 files
fixture audit passed
git diff --check: exit 0
```

Environment verification included:

```text
yt-dlp module       PASS    2026.08.19
Python              PASS    3.12.14
ffmpeg              PASS    8.1.2
ffprobe             PASS    8.1.2
Node                PASS    v20.18.1
Shazam signature    PASS    shazamio 0.8.1, shazamio-core 1.1.2
Panako              WARN    JDK not found — Panako disabled
```

### Six research-set live connector yields

These are direct live CLI runs, not pytest live tests. The sets and any comparison truth are the
existing **unverified seeds**; no accuracy claim or threshold tuning is made. Parse success is the
percentage of fetched source records that emitted at least one accepted hint. Final MixesDB counts
include quarantined Player mirror pointer records in addition to parsed wikitext lines. Search
results were discovery-only and stayed quarantined. No connector hit a cap or truncated a body.

| Unverified set | Platform | Final hints by connector | Source-record parse success | Accepted tracklist blocks | Quarantined mirrors |
|---|---|---|---|---:|---:|
| dev1-set-001 | SoundCloud | comments 369; description 1; MixesDB 26; 1001 search 20 | comments 7.30% of 4,270; others 100% | 6 | 23 |
| dev1-set-002 | SoundCloud | comments 157; description 2; MixesDB 29; 1001 search 20 | comments 9.75% of 1,282; others 100% | 4 | 23 |
| dev1-set-003 | YouTube | comments 96; description 21; chapters 15; MixesDB 15; 1001 search 20 | comments 19.50% of 200; others 100% | 8 | 21 |
| dev1-set-004 | SoundCloud | comments 81; description 1; MixesDB 0; 1001 search 20 | comments 12.91% of 627; description/search 100%; MixesDB no match | 0 | 20 |
| dev1-set-005 | SoundCloud | comments 51; description 1; MixesDB 32; 1001 search 20 | comments 7.92% of 404; others 100% | 2 | 23 |
| dev1-set-006 | SoundCloud | comments 96; description 1; MixesDB 0; 1001 search 20 | comments 11.25% of 604; description/search 100%; MixesDB no match | 2 | 20 |

Across the six sets, 1,114 final hints were emitted: 754 SoundCloud comments, 6 SoundCloud
description units, 102 MixesDB units/pointers, 96 YouTube comments, 21 YouTube description units,
15 YouTube chapters, and 120 discovery results. By kind: 245 answers, 1 correction, 14 keywords,
153 pointers, 377 questions, and 324 tracklist lines. The parser accepted 22 tracklist blocks and
reported 130 quarantined mirrors.

The review-fix rerun of the sole YouTube set used the corrected
`max_comments=4200,200,4000,20,1` extractor cap. Before and after final yields were both 96
YouTube-comment hints from 200 fetched records. The returned payload contained 200 parent threads
and zero replies, so this source did not exercise the newly available reply allowance:

```text
before: yt_comments input_records=200; hints_emitted=96
after:  yt_comments input_records=200; parents=200; replies=0; hints_emitted=96
```

There was no Mixcloud source among the six specified research sets and no applicable allow-listed
pointer in the YouTube primary flow, so the optional Mixcloud GraphQL and pointer HTTP flows were
not live-invoked. Their response/security behavior is covered by authored fixtures and bounded
HTTP tests. Applicable SoundCloud, YouTube, MixesDB, and 1001 search flows all ran live.

### Unverified-seed fused versus audio-only comparison

The paired smoke used dev1-set-004 because its Stage 2b Shazam observations were already cached;
it made no new recognition call. It is explicitly an **unverified-seed comparison**. This set had
102 hints but no accepted authoritative tracklist block, so the policy correctly made no tier or
coverage change:

```text
audio exit=0; evidence_supported_ms=1578000; episodes=39
fused exit=0; evidence_supported_ms=1578000; episodes=39; hint_supported=0
audio unverified=True; segment P/R=10/8; work P/R=270/455
fused unverified=True; segment P/R=10/8; work P/R=270/455
```

Artifact evidence coverage was 6,249/10,000 (62.49%) in both runs: fused minus audio-only was
**0.00 percentage points**. Segment precision/recall and work precision/recall deltas were also
zero. This is a plumbing/non-inflation observation, not the formal gate result.

### Formal Stage 4a gate contingency

The requested held-out corpus is not present as owner-verified frozen truth. The command refuses
before any network work, as intended:

```text
exit=1
formal Stage 4a gate pending owner-verified frozen dev-2 truth
```

The formal gate is therefore **pending owner verification**. Once `dev-2` contains non-draft
ground truth plus a hash-valid `frozen: true` corpus manifest, run exactly:

```powershell
uv run id-detector benchmark hints --corpus dev-2 `
  --out data/corpus/dev-2/hints-gate.json `
  --work-root data/local/work-hints-gate
```

That command performs paired audio-only/fused runs and writes the +5 pp duration-weighted evidence
coverage gate, its one-sided 95% set-cluster lower bound, and 1 pp segment-precision
non-inferiority result.

## Plan-silent decisions

- `temporal_precision_ms` is 60,000 for minute-only cues, 5,000 for `MM:SS`/`H:MM:SS`, the width
  for explicit ranges/comment windows, and 1,000 for structured chapters/sections. Position ranges
  remain the literal plan ranges.
- Low-level single-line fixture inspection retains a timestamped line classification so the
  supplied `title_first` synthetic case is honoured; all persisted connector materialisation uses
  strict two-line block acceptance. Structured platform chapters/sections retain their explicit
  platform position.
- Parse success counts a fetched source record once when it emits one or more accepted hints;
  `hints_emitted` separately counts all units produced by mega-comments/blocks.
- CLI “top blocks” group accepted lines by connector and authority and show a bounded sample. The
  hint contract intentionally does not expose raw connector record ids.
- Current MediaWiki `formatversion=2` list-shaped pages are accepted alongside the keyed shape in
  the authored fixture; both resolve the requested page id before reading the main slot.
- Connector job identity uses a versioned hash over target, source/manual input content,
  connector policy, and caps. Pointer metadata uses standard HTML meta or JSON-LD fields; absent
  or ambiguous metadata leaves the mirror quarantined.
- Manual mirror confirmations are deterministic inputs to a run and are recorded, with whether
  they matched an imported page, in `connector_status.json`.

## Known gaps

- The Stage 4a benchmark acceptance gate cannot be claimed until the owner creates, verifies, and
  freezes held-out `dev-2` truth. The six research sets are tuning inputs with unverified seeds.
- Search/MixesDB mirror candidates remain quarantined because no candidate was manually confirmed
  or proved against all identity, duration, and two-hint timeline conditions. Discovery results
  are intentionally never auto-fetched.
- Optional Mixcloud GraphQL availability and live pointer-page HTML remain service-dependent and
  were not represented in the six-set live sample. Their failures are non-blocking and recorded.
- Question/hint clusters only queue generation-zero rescan requests. Executing those requests is
  Stage 4c; learned connector/kind likelihoods and calibrated tiers remain Stage 5.

## Deviations from plan

`docs/PLAN.md` is unchanged. Stage 4a remains an implementation report rather than an accepted
stage because its required held-out `dev-2` gate cannot run without owner-verified frozen truth.
No gate result was fabricated.

## What the next stage needs to know

- Stage 4b should preserve `hints/hints.jsonl` and include its completion-sidecar hash whenever
  transform/schedule work re-fuses the same media. Hint evidence must stay one work vote and must
  never leak into version-tier calculation.
- `rescan_plan.gen0.jsonl` can now contain `hint_cluster` and `question_cluster`; Stage 4b should
  not execute it, and Stage 4c should deduplicate it using its existing natural key/input hashes.
- Connector caches and raw public responses are local-only. Do not move them into fixtures or
  reports; derive any new fixture through the existing privacy pipeline.
- Run the exact `benchmark hints` command above only after owner-verified frozen `dev-2` exists;
  the command will reject drafts or an invalid freeze manifest before network access.

## Review fixes

This is the authoritative post-review addendum and supersedes the earlier verification counts.
Finding IDs follow the order in `docs/reviews/code-review-stage-4a.md`.

### Finding to change to test

- **CR-4A-01 — acceptance gate:** blocked, not passed; see **Blocked findings** below.
- **CR-4A-02 — YouTube replies:** changed the yt-dlp limits to
  `4200,200,4000,20,1`, retained at most 200 parents and 20 replies per selected parent after
  parsing, capped totals at 4,200/4,000, and added an explicit return-code guard. Tested by
  `test_youtube_enforces_200_parents_and_20_replies_per_thread` and
  `test_youtube_rejects_nonzero_extractor_exit_even_with_json`.
- **CR-4A-03 — pointer natural keys:** appended the full SHA-256 of the validated final URL to
  every imported source-record ID. Tested by `test_pointer_pages_have_unique_natural_keys`.
- **CR-4A-04 — stale connector caches:** connector IDs now hash a version, target digest,
  source/manual content digest, connector configuration, and caps. SoundCloud reassembly reads
  only page numbers below the current durable checkpoint. Tested by the edited-in-place branch of
  `test_manual_pipeline_is_cached_deterministic_and_job_backed`,
  `test_connector_cache_key_covers_input_config_and_caps`, and
  `test_soundcloud_refresh_ignores_stale_pages_beyond_checkpoint`.
- **CR-4A-05 — precision bootstrap:** the gate reparses each prediction set and passes raw
  `(segment_tp, segment_tp + segment_fp)` counts into the paired bootstrap. Tested with strongly
  unequal set sizes by `test_precision_gate_uses_raw_segment_counts_for_unequal_sets`.
- **CR-4A-06 — MixesDB page selection:** keyed and list response shapes now return content only
  for the exact requested page ID. Tested by `test_mixesdb_requires_the_exact_requested_page` and
  the authored two-shape fixture test.
- **CR-4A-07 — mirror release:** pointer results carry URL-bound metadata and source-record IDs;
  after relation/provenance grouping, quarantine releases only on identity/uploader-date,
  two-percent duration, and two distinct timeline/provenance anchors. Repeatable
  `--confirm-mirror <url>` imports and releases manually confirmed pages and persists the action.
  Tested by `test_quarantined_hints_never_vote_and_mirror_release_requires_all_conditions`,
  `test_pipeline_releases_mirrors_after_provenance_or_manual_confirmation`,
  `test_pointer_extracts_release_metadata`, and
  `test_manual_mirror_confirmation_is_imported_released_and_audited`.
- **CR-4A-08 — standalone locking:** `hints` now owns the same source and media locks for ingest,
  job work, artifact publication, and sidecars. Windows long-path lock names normalize `\\?\`
  spellings. Tested cross-process by
  `test_standalone_hints_holds_source_and_media_locks_across_processes`.
- **CR-4A-09 — credential-bearing pointers:** pointer validation now uses
  `url_has_credentials`; parsed/artifact-facing text and cached connector output redact URL query
  credentials, and MixesDB/pointer mirror extraction drops them. Tested for token, signature,
  credential, and API-key query forms by the pointer policy parameterization and
  `test_credential_pointer_text_never_reaches_hint_artifacts`.
- **CR-4A-10 — canonical ordering:** final hint records are sorted by ID before JSONL
  serialization and return. Tested by the ordering assertion in
  `test_manual_pipeline_is_cached_deterministic_and_job_backed`.
- **CR-4A-11 — breaker and pointer wall clock:** breakers are shared per connector/target host;
  transient transport/408/429/5xx failures are retained as `retryable_failure`; the entire pointer
  redirect and streamed-body operation has one absolute 20-second deadline. Tested by
  `test_breakers_are_shared_per_connector_and_host`,
  `test_retryable_connector_failure_is_recorded_for_later_retry`, and
  `test_pointer_wall_clock_timeout_is_retryable`.
- **CR-4A-12 — asserted fixture expectations:** the synthetic parser corpus is parameterized by
  case and explicitly checks every timestamp, range, boolean, and named expectation. Edited-file,
  shortened-pagination, multi-pointer, breaker, and Windows-concurrency cases were added as
  listed above. Conflicting corrections are covered by
  `test_conflicting_corrections_remain_distinct_and_target_the_same_parent`.

### Blocked findings

- **CR-4A-01:** no owner-verified frozen `dev-2` exists. Stage 4a acceptance therefore remains
  unmet. Exact command and observed result:

```powershell
uv run id-detector benchmark hints --corpus dev-2 `
  --out data/corpus/dev-2/hints-gate.json `
  --work-root data/local/work-hints-gate
```

```text
formal Stage 4a gate pending owner-verified frozen dev-2 truth
exit=1
```

### Disputed findings

- **CR-4A-02, nonzero-exit subclaim only:** `run_process` already defaults to `check=True` and
  raises `ProcessError` before JSON parsing on nonzero exit. The connector nevertheless now has an
  explicit defensive return-code check, and the injected-result regression test proves it. The
  incorrect reply limits themselves were a defect and were fixed.

### Deferred

None. All P1 implementation findings and all three P2 findings were fixed. The acceptance gate is
owner-blocked, not deferred.

### Review verification

Final required command outputs:

```text
uv run pytest -q
338 passed, 3 deselected, 1 warning in 63.59s (0:01:03)

uv run ruff check .
All checks passed!

uv run ruff format --check .
107 files already formatted

uv run python scripts/audit_fixtures.py
audited 165 files
fixture audit passed
```

### [P1] The required Stage 4a acceptance gate is still unmet

**What:** The plan requires a held-out `dev-2` result showing ≥5 pp coverage improvement, a positive one-sided 95% cluster bound, and precision non-inferiority. The report instead records a 0.00 pp tuning-set smoke result and says the formal gate is pending. That is disclosed honestly, but it means Stage 4a cannot yet be considered accepted, and “Deviations from plan: None” is misleading.

**Where:** `docs/PLAN.md:189`; `docs/stage-reports/stage-4a.md:180-204`; `docs/stage-reports/stage-4a.md:224-237`

**Fix:** Freeze owner-verified `dev-2`, run the formal gate, and record its result; otherwise label this as an implementation report pending Stage 4a acceptance.

### [P1] YouTube extraction fetches far fewer replies than the plan requires

**What:** Pinned yt-dlp interprets `max_comments` as `max_total,max_parents,max_replies,max_replies_per_thread,max_depth`. The configured `200,200,20,1` therefore permits only 20 replies total and one reply per thread, not the required top 200 threads × 20 replies. The subprocess return code is also ignored if stdout happens to contain valid JSON.

**Where:** `src/id_detector/hints/connectors/youtube.py:18`; `src/id_detector/hints/connectors/youtube.py:102-128`; `tests/test_stage4a_connectors.py:95`

**Fix:** Use limits equivalent to `4200,200,4000,20,1`, enforce them after parsing, and reject nonzero yt-dlp exits. Test the resulting parent/reply counts instead of asserting the configuration string.

### [P1] Different pointer pages generate duplicate hint natural keys

**What:** Every imported 1001TL page uses `source_record_id="imported-tracklist"` and every fallback page uses `"imported-page"`. Because the hint natural key is `connector + source_record_id`, two pages produce identical IDs for corresponding lines. An in-memory reproduction with two different pointer URLs produced four hint records but only two unique IDs, corrupting relations and provenance.

**Where:** `src/id_detector/hints/connectors/pointer.py:123-145`; `src/id_detector/hints/parse.py:531-538`

**Fix:** Include a stable digest of the validated final URL or connector-job ID in each imported source-record ID, then add a multi-pointer uniqueness test.

### [P1] Connector cache identity and refresh handling can return stale hints

**What:** Connector jobs are keyed only by media, connector, and target URL. Editing a manual tracklist in place keeps the same file URI, so a succeeded job returns the old cached contents without rereading the file. Caps/configuration are also absent from the key. Separately, SoundCloud refresh resets database counters but retains old `page-*.json` files; the parser later globs every old page, so a shortened comment feed retains deleted/stale comments.

**Where:** `src/id_detector/jobs.py:864-900`; `src/id_detector/hints/pipeline.py:106-130`; `src/id_detector/hints/pipeline.py:375-381`; `src/id_detector/hints/connectors/soundcloud.py:182-215`

**Fix:** Use an unambiguous, versioned connector cache key containing input-content hash and connector configuration/caps. Assemble SoundCloud results only from pages belonging to the current attempt, or from explicit page numbers below the current checkpoint.

### [P1] The precision gate bootstraps rounded per-set percentages instead of segment counts

**What:** Each set’s rounded precision is passed as `(precision_e4, 10000)`. The cluster bootstrap therefore computes an equally weighted mean of rounded set precisions, not the report’s micro precision from pooled true-positive/prediction counts. Unequal-sized sets can consequently pass the 1 pp gate when the required micro statistic fails.

**Where:** `src/id_detector/benchmark/hints.py:109-128`

**Fix:** Pass each set’s raw `(tp, tp + fp)` counts into `paired_non_inferiority`, and test highly unequal cluster sizes.

### [P1] MixesDB can silently consume a different page than requested

**What:** If the requested page ID is missing, both supported response shapes fall back to the first available page. This can attribute an unrelated tracklist to the mix. A direct reproduction requesting page `17` from a response containing only page `99` returned page 99’s content. This contradicts the stage report’s claim that the requested page ID is resolved first.

**Where:** `src/id_detector/hints/connectors/mixesdb.py:33-47`; `docs/stage-reports/stage-4a.md:219-220`

**Fix:** Return `None` unless the exact requested page exists, causing the connector to fail safely. Add mismatched and multi-page fixtures.

### [P1] Mirror quarantine cannot actually be released

**What:** Pointer imports always hardcode `quarantined`, and the pipeline never calls `mirror_is_verified` or exposes manual confirmation. Thus the plan’s “quarantined until conditions or manual confirmation” transition is unreachable. Moreover, the predicate counts duplicate mirror records as separate timeline agreements; the test explicitly passes using two copies of the same hint.

**Where:** `src/id_detector/hints/connectors/pointer.py:123-145`; `src/id_detector/hints/mirrors.py:73-80`; `tests/test_stage4a_relations_fusion.py:216-232`

**Fix:** Integrate release after relation/provenance processing, require two distinct timeline/provenance anchors, and persist an auditable manual-confirmation action.

### [P1] The standalone hints command does not participate in workspace locking

**What:** `analyse` acquires source and media locks, but `hints` acquires neither. It can race analysis over ingest, `jobs.sqlite`, and hint artifact/sidecar writes. The SQLite lock is released before `hints.jsonl` and its sidecars are materialized, leaving an additional race window, especially relevant on Windows.

**Where:** `src/id_detector/cli.py:97-106`; `src/id_detector/cli.py:300-329`; `src/id_detector/hints/pipeline.py:382-423`

**Fix:** Have the standalone command acquire the same source/media locks for the complete operation, including artifact and sidecar publication, and add a Windows concurrent-process test.

### [P1] Credential-bearing pointer URLs can enter artifacts

**What:** Pointer validation rejects userinfo but accepts sensitive query parameters. A URL such as `https://1001.tl/a?token=secret` is accepted and can be preserved in `raw_text`, the pointer title, connector-job target, and cached output, contrary to the plan’s no-secrets artifact rule.

**Where:** `src/id_detector/hints/connectors/pointer.py:34-47`; `src/id_detector/hints/parse.py:315-345`; `src/id_detector/hints/parse.py:540-568`

**Fix:** Reject URLs for which `url_has_credentials()` is true and redact artifact-facing raw text. Add tests for token, signature, credential, and API-key query parameters.

### [P2] `hints.jsonl` uses the wrong canonical ordering

**What:** Hint records have no `start_ms`, so the plan requires ordering by ID. Relations instead return them by position and ID, and the pipeline serializes that order unchanged.

**Where:** `src/id_detector/hints/relations.py:238-244`; `src/id_detector/hints/pipeline.py:401`

**Fix:** Sort final hint records by ID immediately before serialization.

### [P2] The advertised breaker and 20-second pointer limit are not end-to-end bounds

**What:** A new breaker is created for every connector job, so failures across multiple pointer/search jobs never accumulate to five per minute. The HTTPX timeout is an inactivity timeout per operation, not a 20-second wall-clock limit; a slow-drip response can exceed the plan’s cap.

**Where:** `src/id_detector/hints/pipeline.py:139-149`; `src/id_detector/hints/connectors/pointer.py:50-87`

**Fix:** Share breakers per connector/host for the run, record retryable HTTP failures, and wrap the complete redirect/stream operation in an absolute timeout.

### [P2] Parser tests iterate fixtures without asserting many claimed expectations

**What:** The generic loop checks timestamps, boolean questions, and ranges only. String expectations such as `separator_priority`, `standalone_answer`, and `contested_relation` are ignored unless separately hardcoded. Thus the test can “cover every case” while not testing its claimed property. The unchanged-file cache test similarly misses cache invalidation.

**Where:** `tests/test_stage4a_parser.py:30-71`; `tests/test_stage4a_pipeline.py:24-69`; `docs/stage-reports/stage-4a.md:94-100`

**Fix:** Parameterize every fixture expectation into explicit structured assertions and add edited-in-place, shortened-pagination, multi-pointer, breaker, and Windows concurrency cases.

## Verified

- `uv run pytest -q` — exit 1 before pytest started: `uv.exe` is a WinGet symlink that could not be executed in this read-only environment. The reported `287 passed` result was not reproduced.
- `uv run ruff check .` — same environment-level `uv.exe` failure. Direct `.venv\Scripts\ruff.exe check .` returned `All checks passed!`.
- `uv run id-detector doctor` — same `uv.exe` failure. Direct doctor exited 1 because uv/ffmpeg/ffprobe were unavailable and no writable temporary directory existed.
- Direct pytest also stopped before collection with `No usable temporary directory found`.
- `python scripts/audit_fixtures.py` — `audited 164 files` / `fixture audit passed`.
- `git diff --check` — exit 0.
- No new Stage 4a test references `data/raw/`; local hint/raw paths are ignored.

REVIEW VERDICT: FIX_FIRST
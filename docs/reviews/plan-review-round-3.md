# Plan review — round 3

## Summary

Revision 3 is substantially stronger, especially on provider capabilities, overlapping episodes, independent truth, Windows recovery, and provisional confidence labels. However, the boundary formula still creates unsupported hard bounds, and the benchmark design cannot yet certify the advertised tiers or fairly score uncertain boundaries. Several supposedly executable contracts remain internally inconsistent, particularly transformed Shazam spans, run identity, episode/gap semantics, confidence calculation, and paid-provider submission recovery. Shazam-first is reasonable for the free profile, but final engine profiles and Panako/custom-catalogue work are still sequenced too late for an accuracy-first project.

## Findings

### [P0] Boundary ranges still infer absence from missing detections

**What:** `D_max = hop + window − L_min` is only a scheduling quantity under an assumption that every sufficiently audible window matches. In a real mix, the first positive may arrive minutes after the true mix-in because of low transition gain, EQ, a difficult musical passage, masking, or recognizer failure. Likewise, the last positive does not bound when the track stops. The proposed finite lower start bound and upper end bound therefore contradict “a no-match never tightens a range.”

**Why it matters:** The emitted interval can exclude the true boundary while presenting itself as a range guaranteed to contain it. Calibration cannot turn an empirical latency quantile into a hard maximum, especially outside the measured gain/genre/transform distribution.

**Concrete change to the plan:** Positive evidence alone should produce only one-sided facts: the track started no later than `first_support_end − L_min` and ended no earlier than `last_support_start + L_min`. Represent the other side as open/censored unless it is supported by direct alignment, human cue truth, or explicitly validated competing positive evidence. If empirical latency is used, label the result as a calibrated prediction interval with a stated coverage target and report both interval coverage and width; do not call a percentile `D_max`.

### [P0] The benchmark still cannot certify the confidence promises

**What:** The quota of 400 verified episodes across development, calibration, test, four strata, multiple profiles, and three confidence dimensions does not imply approximately 300 error-free **test predictions at the Verified tier**. Certification depends on predictions emitted at that tier, not the number of truth episodes. Synthetic transformations of the same tracks are also correlated and cannot certify performance on real DJ mixes. Boundary-tier correctness has no defined event or tolerance, while the current “zero error whenever ranges overlap” rule rewards arbitrarily wide ranges.

**Why it matters:** The scorer can declare excellent boundary accuracy by emitting vague intervals, and the proposed corpus can remain far below the sample size needed for a 99% lower confidence bound even after meeting its headline quota.

**Concrete change to the plan:** Pre-register certification separately for each profile and dimension, including the target population, unit of analysis, confidence-interval method, and required number of tier-qualified test predictions. Do not pool controlled renders with real mixes to certify real-mix precision. Score start and end separately; report interval coverage, median/p90 width, a proper interval score, and point/range error at explicit tolerances. Replace the later “CI-overlap” test with a paired, one-sided non-inferiority test using a declared margin.

### [P1] Transformed-window support is factually wrong

**What:** Both proposed undo transforms change duration. For example, a 12-second query with `r=1.08` becomes about 12.96 seconds. It therefore cannot simultaneously be fingerprinted as a full 12-second Shazam segment, avoid cropping, and retain `support_ms == [start_ms,end_ms]`. `shazamio-core` exposes an integer `segment_duration_seconds`, so fractional transformed durations need an explicit policy. The controlled benchmark also includes pitch-only changes, but the implementation has no pitch-only compensation hypothesis. Confidence: high. [`shazamio-core` source](https://github.com/shazamio/shazamio-core/blob/master/src/lib.rs)

**Why it matters:** Incorrect support coordinates bias anchors, fitted rates, boundaries, and cache equivalence precisely on the pitched material transforms are intended to recover.

**Concrete change to the plan:** Define an output-sample-to-original-sample mapping for every transform, including encoder delay, padding, and any center crop. Derive `support_ms` from the samples actually fingerprinted. State whether transformed output is capped at 12 seconds, shortened at the input, or rejected. Add pitch-only compensation or explicitly classify it as an unsupported case, and test every factor with known insertions.

### [P1] The Shazam anchor contract is asserted rather than demonstrated

**What:** Shazam returns a `matches[]` array that can contain several offsets and skew values, but an observation has one anchor and no rule for selecting, clustering, or summarizing those matches. Upstream `shazamio` passes the response through; it does not document that `offset` is exactly the reference position at the first fingerprinted sample. The fixed 500 ms uncertainty is also unsupported. Confidence is high that the contract is unverified; confidence is only medium that the assumed anchor itself is wrong. [`shazamio` API source](https://github.com/shazamio/ShazamIO/blob/master/shazamio/api.py)

**Why it matters:** A systematic anchor-origin error or unstable choice among offsets becomes an alignment intercept error and can split or join episodes incorrectly.

**Concrete change to the plan:** Preserve every native match tuple and specify an adapter aggregation rule based on offset clustering and dispersion. Estimate anchor bias and uncertainty from insertion tests at several reference positions, query lengths, partial overlaps, and transform factors. Stage 1 must not hard-code the current semantic or 500 ms value before those tests pass.

### [P1] The artifact contracts remain internally inconsistent

**What:** `run_manifest.json` contains both ingest-time and decode-time fields without saying whether it is mutated; `run_key = sha256(canonical_url)` aliases replaced or reuploaded media; `run_id`, `created_at`, and `observed_at` make reruns non-identical despite deterministic-artifact claims. The blanket requirements that every derived value has `source_ids` and every JSONL file is sorted by `start_ms` are not reflected in the schemas and cannot apply to records without `start_ms`. Float transform factors are also used in deterministic natural keys without canonical serialization.

**Why it matters:** An agent can produce stale cache hits after media changes, invalidate downstream sidecars unpredictably, or generate different IDs and hashes for semantically identical runs.

**Concrete change to the plan:** Define immutable artifacts owned by individual stages, a source-location key distinct from a media-revision key, and a separate invocation journal for nondeterministic execution metadata. Specify canonical JSON/decimal encoding, exact natural keys, artifact migration rules, and per-record ordering. Either add field-level provenance wrappers to the schemas or narrow the “every derived value” claim to records.

### [P1] Fusion and episode construction are not executable algorithms yet

**What:** Stage 1 is asked to implement confidence from observation counts, but only the `<2 independent trials → unclear` case is defined. There are no deterministic rules for assigning other tiers, converting feature values into confidences, splitting an occurrence from a distant replay, or distinguishing a hot-cue reset from a new episode. The identity graph’s connected-component rule can also merge A–B and B–C transitively despite an A–C conflict, and “one source plus corroborating text” need not be independent.

**Why it matters:** Two agents can implement materially different systems while satisfying the prose. Transitive identity mistakes can manufacture engine agreement and then promote the wrong remix or sampled source.

**Concrete change to the plan:** Specify a complete conservative baseline fuser, including minimum observations/span, segment creation and break rules, replay-gap rules, outlier treatment, conflict precedence, and the exact provisional-tier mapping. Prevent union across conflict edges and require evidence-independent pairwise recording assertions. Keep heuristic scores explicitly typed as uncalibrated rather than presenting arbitrary 0–1 values as confidence.

### [P1] Entirely missed short layers cannot be labelled as claimed

**What:** The plan says unrecoverable short overlays are marked `below_recognizer_minimum`, but a completely undetected overlay produces no episode to carry that flag. `gaps` has no schema, and coverage is undefined because episodes contain uncertain start/end ranges rather than a single interval. There is no definition of certain coverage, possible coverage, or how a gap is derived from overlapping uncertain intervals.

**Why it matters:** Production output may imply that all short failures were detected and honestly classified when the system cannot know that an event occurred. Coverage and “ID gap” metrics will vary by implementation.

**Concrete change to the plan:** Define the gap record, including bounds, observable evidence, truncation status, and reason confidence. Distinguish guaranteed identified duration from possibly identified duration. Treat wholly unseen short layers as a benchmark-measured limitation, not a production label, unless a novelty detector or hint provides independent evidence of the event.

### [P1] Paid-job recovery still has a double-submission hole

**What:** The stale-job policy depends on knowing whether submission occurred, but the job schema has no durable `submission_started_at/submitted_at` transition. AudD is a long synchronous POST; ACRCloud returns a remote file ID that is then polled. If an ACRCloud upload succeeds but the response containing the ID is lost, it is just as unrecoverable locally as AudD unless the provider offers searchable idempotency. Merely storing an `idempotency_key` does not make the provider idempotent. [AudD enterprise contract](https://docs.audd.io/enterprise/), [ACRCloud file-scanning lifecycle](https://docs.acrcloud.com/reference/console-api/file-scanning/file-scanning)

**Why it matters:** Crash recovery can resubmit and rebill work despite the “no double billing” acceptance criterion.

**Concrete change to the plan:** Persist a pre-network `submission_started` state transactionally, document provider-supported idempotency or reconciliation, and send every ambiguous disconnect to `outcome_unknown`. Add uniqueness constraints for logical queries/cache keys and failure-injection tests before upload, during upload, after acceptance, after remote-ID persistence, and during polling.

### [P1] Windows concurrency, retries, and cache refresh remain underspecified

**What:** “One SQLite writer process” does not say how async recognition workers communicate with it. Retry counts, HTTP timeouts, 429 handling, cancellation points, and heartbeat behavior during long blocking uploads are unspecified. Positive matches are cached indefinitely even though remote catalogues and erroneous matches change; negative results have no user-forced refresh path. Assigning a running subprocess to a Windows Job Object can also race with descendants unless process creation/assignment is implemented carefully.

**Why it matters:** The result can be stuck leases, database contention, unbounded library retries, descendants retaining files, or permanently stale incorrect IDs.

**Concrete change to the plan:** Specify the writer architecture and queue, explicit connect/read/overall timeouts, retry ceilings, heartbeat task, and shutdown protocol. Add `--refresh`/maximum-age policies and freeze raw provider responses for benchmarks. Test multiple workers and forced termination at each subprocess/network boundary, including yt-dlp-spawned ffmpeg children.

### [P1] Engine profiles are selected before the final recognition system exists

**What:** Stage 2 chooses `free` and `max_accuracy` profiles before transforms, hints, rescans, full identity resolution, and file-scanner fusion are implemented. Those features can change both marginal engine value and error correlation. Panako, the only selected engine specifically designed for scale changes against a held reference pool, remains a full Stage 7 feature after enrichment and UI. Its JDK and AGPL constraints are correctly characterized. [Panako project](https://github.com/JorenSix/Panako)

**Why it matters:** The plan can lock in the wrong ensemble before measuring the difficult cases that motivated an ensemble. A Shazam-first cascade is appropriate for the free profile, but not for a maximum-accuracy profile.

**Concrete change to the plan:** Stage 2 should shortlist engines and verify entitlements, not finalize profiles. Freeze profiles only after Stage 3c ablations. When enabled, move a minimum Panako or ACRCloud custom-bucket path before profile selection; otherwise explicitly exclude reference-pool coverage from v1. Require `max_accuracy` to run all selected independent engines without Shazam-based suppression.

### [P1] Hint parsing and fetching still leave agent-visible ambiguities

**What:** The question regex accepts many unrelated questions ending in `?`; dotted timestamps can be decimals; the bare-hyphen fallback still damages names; and component bounds such as `1:72` are not specified. SoundCloud correction inheritance depends on an undefined “compatible time,” while a low-confidence guessed reply can still negatively weight another assertion. The plan also says “1001TL search only” while pointer import implies fetching a supplied 1001.tl page. Redirect handling for comment-supplied URLs is not defined.

**Why it matters:** Incorrect parsing can attach high-weight identities to the wrong time, and untrusted pointers can escape the intended host allowlist. The current weights being called “features” does not resolve how they affect fusion.

**Concrete change to the plan:** Add component-range validation, context requirements for dotted timestamps, separator-preservation cases, and negative examples for non-track questions. Apply correction effects in proportion to relation confidence. Resolve the 1001tracklists fetch contradiction and specify exact host/scheme allowlists, redirect revalidation, response-size/time limits, and truncation records. Clip all position ranges to media duration.

### [P1] Several stage gates remain passable without meaningful accuracy

**What:** Stage 2 accepts an unspecified “initial dev set”; Stage 3c requires any p90 improvement; “loop cases recovered” and Stage 7’s “improves measurably” have no denominators or regression constraints. Stage 3a uses CI overlap as evidence of non-inferior precision, which is not a valid non-inferiority test. Stage 1’s live Shazam result is also nondeterministic as a sole adapter gate.

**Why it matters:** An AI agent can satisfy the literal acceptance text with one set, a negligible boundary change, or a precision regression hidden by wide intervals.

**Concrete change to the plan:** Give every gate a frozen corpus version, minimum sample count, paired delta, one-sided confidence bound, non-inferiority margin, and no-regression metrics. Define the 5% hint gain as absolute or relative. Keep an opt-in live Shazam smoke test, but add recorded adapter responses and deterministic signature/normalization tests.

### [P1] Fixture documentation and release legal gates are still inconsistent

**What:** The required `data/fixtures/comments/README.md` is absent; only `data/fixtures/README.md` exists. That file still specifies deterministic six-character handle hashes, contradicting PLAN.md’s random fixture-local identities. Raw dumps are correctly covered by `.gitignore`, so that part is addressed. Commercial restrictions mention SoundCloud prominently but need equivalent explicit gates for YouTube/Mixcloud ingestion, unofficial GraphQL/API access, third-party audio uploads, and UK personal-data handling of comments.

**Why it matters:** The first commit can encode the already-rejected pseudonymisation design, while future product work may inherit connectors that cannot simply be enabled commercially.

**Concrete change to the plan:** Reconcile the paths and fixture specification before commit, and add an automated fixture audit for handles, URLs, platform IDs, and searchable incidental text. Require explicit opt-in before uploading third-party mixes to AudD/ACRCloud. Add a commercial release checklist disabling unofficial Shazam, SoundCloud api-v2, Mixcloud GraphQL, and 1001tracklists scraping until reviewed; cover all ingestion platforms, provider retention, deletion handling, and Panako AGPL/service obligations.

### [P2] The initial schedule is reasonable but not coverage-complete

**What:** A 12-second window with a 10-second hop is a sensible baseline, but with `L_min=3s`, the condition for every three-second event to fit wholly inside some window is `hop ≤ window − L_min`, i.e. at most nine seconds. Some proposed combinations, such as six-second windows with a 15-second hop, have much larger blind phases.

**Why it matters:** Short-track comparisons can measure predictable scheduling holes rather than recognizer capability.

**Concrete change to the plan:** Classify schedules as coverage-complete or deliberately sparse for each target minimum duration. Include five-second-hop schedules in the accuracy profile and compare request cost separately rather than allowing cost to dominate the primary selection objective.

### [P2] Export and player policies discard uncertainty

**What:** CUE requires one non-overlapping timestamp, while episodes have boundary ranges and may overlap. The web UI seeks to `start_ms_range.lo`, which can intentionally land well before any evidence of the track.

**Why it matters:** Correct internal uncertainty can become a misleading flat export or poor listening experience.

**Concrete change to the plan:** Define flattening, overlap precedence, and timestamp selection for each export. In the UI, display the range and use a configurable lead-in around a central/best estimate rather than treating the lower bound as the start.

## Prior findings status (rounds > 1 only)

### Round 1

| Finding | Status in Revision 3 |
|---|---|
| Reference offset does not reveal mix-in time | **Partially addressed:** alignment and boundaries are separated; the replacement boundary bounds remain invalid. |
| Circular/unrepresentative ground truth | **Partially addressed:** independent verification and strata exist; certification population, interval scoring, and realistic power remain open. |
| Missing stage contracts | **Partially addressed:** many records were added, but lifecycle and determinism contradictions remain. |
| One accuracy score hides failures | **Partially addressed:** metric families exist; uncertain-boundary and certification semantics remain defective. |
| Window schedule has holes | **Largely addressed:** 12/10 overlaps and a tail exists; minimum-duration phase coverage is still not guaranteed. |
| Tempo compensation underspecified | **Partially addressed:** filters and factor direction exist; transformed support and pitch-only compensation do not. |
| Timeline cannot represent transitions/repeats | **Partially addressed:** the schema permits them; construction and occurrence-splitting rules are missing. |
| Confidence OR rules too permissive | **Partially addressed:** calibration targets replaced OR rules; the actual model and test power are still undefined. |
| One provider interface does not fit all engines | **Largely addressed:** capability classes exist; ambiguous submission recovery remains. |
| Engine selection happens too late | **Partially addressed:** adapters moved earlier, but final profiles are now selected too early. |
| Recording/version identity undefined | **Partially addressed:** a graph exists; transitive merging and conflict semantics remain unsafe. |
| Text weights are pseudo-probabilities | **Partially addressed:** renamed as features and provenance-grouped; no executable fusion model exists. |
| Comment parsing/fetch failure rules | **Partially addressed:** many fixtures and safeguards were added; regex, correction, URL, and connector contradictions remain. |
| Jobs not resumable/budget-safe | **Partially addressed:** leases and budgets exist; ambiguous submissions can still be retried or double-billed. |
| Windows/media-time acceptance | **Largely addressed:** canonical PCM and process tests exist; worker architecture and Job Object race handling remain unspecified. |
| Panako not executable without Java | **Addressed:** conditional on an explicit JDK decision. |
| Stage 3 too large | **Partially addressed:** split into increments, but Stage 1/3c overlap and profile sequencing remain. |
| Raw public-comment fixtures | **Partially addressed:** raw data is ignored; the surviving fixture README still specifies short stable hashes. |
| Commercial model understated | **Largely addressed:** separate discovery is explicit; platform-wide release gates need tightening. |
| Acquisition links must be non-authoritative | **Addressed.** |

### Round 2

| Finding | Status in Revision 3 |
|---|---|
| Episode boundary ranges invalid | **Not addressed:** a new formula still converts absent positives into finite hard bounds. |
| Benchmark cannot substantiate tiers | **Partially addressed:** scoring and quotas improved; boundary correctness, per-tier power, and population separation remain unresolved. |
| Build order invalidates calibration | **Partially addressed:** calibration moved after feature work; engine profiles are still chosen before that work. |
| Shazam contract incomplete | **Partially addressed:** WAV, explicit duration, and live smoke were added; transformed spans and multi-offset normalization remain wrong/undefined. |
| Provider offset normalization unsafe | **Partially addressed:** native fields and normalized anchors exist; the Shazam anchor is still asserted without evidence. |
| Single affine alignment inadequate | **Largely addressed:** piecewise segments and events were added; event/occurrence rules need specification. |
| Short-transition recovery overpromised | **Partially addressed:** limitations and short tests exist; wholly missed layers cannot be labelled in production. |
| Confidence conflates work/version | **Largely addressed:** separate tiers and a minimum badge were added; confidence construction and certification remain incomplete. |
| Canonical identity unsafe | **Partially addressed:** the graph replaces precedence keys, but connected-component merging remains unsafe. |
| Contracts incompatible/non-executable | **Partially addressed:** query/job/billing records and sidecars improved; run identity, artifact ownership, gaps, and determinism remain inconsistent. |
| Paid-engine feasibility assumed | **Largely addressed:** entitlement tests and lifecycle differences are recognized; ambiguous upload outcomes and early profile choice remain. |
| Hint timing/corrections/dependence | **Partially addressed:** ranges, correction edges, and provenance groups exist; relation confidence and parser rules remain incomplete. |
| Hint fetching conflicts with architecture | **Partially addressed:** connectors are now non-blocking and Mixcloud/manual flows were added; fetch-cap and pointer behavior conflict. |
| Crash recovery/Windows handling incomplete | **Partially addressed:** leases, heartbeats, WAL, budgets, and Job Objects were added; durable submission state is missing. |
| Fixture/legal risk | **Partially addressed:** PLAN.md changed, but the actual fixture README and expected path did not. |
| Acceptance criteria can pass without value | **Partially addressed:** hints and link audits improved; benchmark and later recognition gates remain weak. |

## Questions for the owner

1. Will v1 deliberately ship with provisional tiers, or will you fund and participate in a sufficiently large frozen real-mix test set with legally held references and second-pass annotation?
2. Will AudD and ACRCloud trial credentials plus a hard test budget be available before engine shortlisting?
3. Will you install a JDK for an early Panako experiment, or should reference-pool identification be explicitly excluded from v1?

## VERDICT

VERDICT: CHANGES_REQUESTED
# Plan review — round 2

## Summary

Revision 2 materially improves the architecture, especially overlapping episodes, provider capability classes, independent truth, and persistent jobs. However, the boundary algorithm remains mathematically invalid, the benchmark is not specified strongly enough to support a 99% “Verified” claim, and the build order attempts the engine bake-off before the paid adapters exist and calibrates confidence before the final evidence sources are added. Several contracts still cannot be implemented independently, particularly Shazam query spans, provider-offset normalization, file-scanner caching, canonical identities, and crash recovery. The plan also overstates what fixed-window recognizers can recover from short overlays, loops, mashups, and live edits.

## Findings

### [P0] Episode boundary ranges are still invalid

**What:** `start_ms_range = [first observation start − hop, first observation start]` assumes a track was audible by the beginning of its first matching window. A track can enter halfway through that window, so its true start can be later than the range’s upper bound. “End analogously” is undefined, and reducing hop size cannot by itself reduce boundary uncertainty below the recognizer’s effective query duration or its unknown detection latency.

**Why it matters:** The system will emit ranges that do not contain the true boundary, especially during crossfades. Boundary p90, IoU, and the UI’s seek target will then be systematically misleading.

**Concrete change to the plan:** Define boundary inference using effective query-support intervals, adjacent positive and negative evidence, and a calibrated recognizer-latency model. A no-match must not be treated as proof of absence. Define “audible” for controlled truth using a reproducible contribution/gain threshold, and prohibit ranges narrower than the evidence supports. Keep subsequence alignment against held references as the route to genuinely precise boundaries.

### [P0] The benchmark cannot substantiate the advertised confidence tiers

**What:** The metrics are named but their scoring semantics, sample-size requirements, and annotation protocol are absent. There is no one-to-one episode-matching rule, overlap scoring rule, definition of duration weighting with simultaneous tracks, treatment of alternate acceptable versions, or definition of “true unknown.” Exact-recording truth may be marked `verified_against: "audio"` even though listening alone usually cannot distinguish an edit, remaster, or near-identical remix. Six seed sets divided three ways are also far too few for bootstrap-by-set inference or a defensible 99% precision claim; roughly 300 independent error-free decisions are needed even for a one-sided 95% lower bound near 99%, before accounting for within-set correlation.

**Why it matters:** Different scorer implementations can produce materially different results, and “Verified ≥ 0.99” could become an unsupported label rather than a measured guarantee. Preselecting a “catalogue-covered” stratum based on successful engine recognition would also bias coverage upward.

**Concrete change to the plan:** Before Stage 2, specify an overlap-aware bipartite episode-matching algorithm; top-candidate handling; exact/work/version equivalence rules; range-to-range boundary error; micro, macro-by-set, and set-valued per-millisecond metrics; and distinct regions for silence/speech, known-but-out-of-pool tracks, and genuinely unresolved truth. Establish corpus quotas and a power calculation across DJs, genres, platforms, transform severity, short episodes, repeats, and overlaps. Exact-version truth must require comparison with a source recording or equivalent authoritative evidence, with a blinded second-pass audit for the frozen test set. Freeze configuration before test evaluation; “shortfall documented” must not count as passing the accuracy gate.

### [P0] The build order is not executable and invalidates calibration

**What:** Stage 2 promises an AudD/ACRCloud bake-off, but those providers are not built until Stage 3d. Stage 3c calibrates tiers before Stage 3d adds file scanners and dense rescans, although both change the feature distribution. Stage 1 also applies placeholder rules involving `score_norm`, which does not exist until calibration, and freezes an episode schema before Stage 3c implements its full semantics.

**Why it matters:** An agent cannot satisfy the Stage 2 acceptance criteria in order. Any calibration from Stage 3c becomes stale immediately, and using the test set again after fixing a shortfall contaminates it.

**Concrete change to the plan:** Build thin, fixture-backed paid-provider adapters and perform account/entitlement smoke tests before the Stage 2 bake-off. Finish the selected observation policies, identity resolver, rescans, and fusion features before calibrating each named profile. Evaluate the frozen test set only after the profile and thresholds are locked; material later changes require a new test version.

### [P1] The Shazam implementation contract is technically incomplete

**What:** `shazamio-core` generates a signature but does not perform the network lookup; the HTTP request is implemented by `shazamio`. Its default is also a centered 10-second excerpt, so passing a 12-second window without explicit options silently fingerprints seconds 1–11. Transforms change the encoded duration and therefore change which original samples that centered excerpt represents. In addition, the core’s byte/path methods decode a container; the planned headerless `audio.pcm` slices are not a supported input contract. These points are confirmed with high confidence by the upstream [`shazamio-core` implementation](https://github.com/shazamio/shazamio-core/blob/master/src/lib.rs) and [`shazamio` API](https://github.com/shazamio/ShazamIO/blob/master/shazamio/api.py).

**Why it matters:** The claimed two-second overlap may be zero in practice, timestamps can be shifted, and Stage 1 may fail when raw PCM is handed to a container decoder.

**Concrete change to the plan:** Explicitly choose and pin `shazamio` plus `shazamio-core`, or specify the custom HTTP client. Wrap slices in deterministic WAV containers or expose a tested raw-buffer path. Record both the requested mix interval and the effective fingerprinted interval after transform and centering. Add a known-track end-to-end smoke test on Windows, not merely an import test.

### [P1] Provider offsets cannot safely share the current normalization rule

**What:** The schema asserts that every `ref_offset_ms` is the reference position at `mix_start_ms`, but adapters have different anchors. AudD distinguishes file-chunk `offset`, song `timecode`, and matched `start_offset`/`end_offset`; Shazam’s anchor must account for its centered signature; ACRCloud has sample-side and database-side offsets whose availability varies by product. AudD documents these distinct meanings explicitly in its [enterprise endpoint documentation](https://docs.audd.io/enterprise/).

**Why it matters:** A one-window anchor error becomes a biased alignment intercept and can incorrectly split, join, or reject episodes.

**Concrete change to the plan:** Require each adapter to retain native offset fields and emit a normalized `(mix_anchor_ms, ref_anchor_ms, anchor_uncertainty_ms)` pair with a documented conversion formula. Provide contract tests using a known reference inserted at a known mix time, including transformed and partial-overlap cases.

### [P1] A single affine alignment cannot represent several stated target cases

**What:** `ref = rate × mix + intercept` assumes continuous playback at a constant rate. Hot-cue jumps, loops, live edits, backspins, and tempo adjustments produce resets, flat sections, discontinuities, or changing slopes. The current plan mentions these cases but would reject their observations as inconsistent or fragment them unpredictably.

**Why it matters:** Correct identity evidence can be discarded precisely on the difficult DJ material the product is intended to handle.

**Concrete change to the plan:** Model an episode as one or more piecewise alignment segments with explicit jump, loop, and reset events. Permit identity support without a global alignment when observations are otherwise strong, while lowering boundary/version confidence. Add controlled loop, cue-jump, repeated-section, and drifting-tempo cases to Stage 2.

### [P1] The plan overpromises transition and short-episode recovery

**What:** A 10–12 second single-result recognizer often returns only the dominant recording from a crossfade and may never see enough clean audio from a track shorter than the query or hop. Dense rescanning changes hop but not window duration, purity, or phase. The episode schema can represent two tracks, but the observation process does not ensure evidence for both.

**Why it matters:** Overlap, mashup, acapella, and short-overlay recall may remain near zero while the architecture appears to support them.

**Concrete change to the plan:** Benchmark episodes of 3, 5, 10, 20, and 30 seconds and crossfades at multiple gain ratios. Dense rescans must test shorter windows and shifted phases where supported, not just smaller hops. Report recall separately for dominant and secondary layers and state an explicit “below recognizer minimum” limitation rather than manufacturing an ID. Treat source separation as a measured later experiment, not an assumed capability.

### [P1] Confidence tiers conflate work identity with an exact track ID

**What:** The single displayed tier is calibrated to work-level `identity_confidence`, while `version_confidence` may remain low. That permits “Verified” for the right composition but the wrong remix, bootleg, or edit. Stage 1’s Shazam rules rely on unavailable `score_norm`; transform variants and overlapping windows are not independent; and the fixed 0.5 commercial-engine correlation discount is unsupported.

**Why it matters:** The highest-cost error—confidently naming the wrong version—can still receive the strongest product badge and acquisition link.

**Concrete change to the plan:** Define separate user-visible work, exact-version, and boundary tiers, or require all relevant dimensions for an unqualified “Verified” label. Group all transform hypotheses from one source window as one correlated trial and estimate provider/window error correlation from the benchmark. Calibrate only after all profile features are frozen, and require confidence-interval-aware tier gates rather than point estimates alone.

### [P1] Canonical identity cannot be a precedence-ordered key

**What:** `ISRC > MusicBrainz > provider ID > normalized text` treats provider metadata as authoritative and does not specify which MusicBrainz entity is meant. ISRCs can be absent, reused incorrectly, or attached to the wrong provider result; provider IDs do not reconcile across engines. The meaning of each candidate’s `relation` is also unclear because relations are inherently between entities.

**Why it matters:** One bad identifier can merge distinct recordings and create false multi-engine agreement that propagates into confidence and purchase links.

**Concrete change to the plan:** Add a versioned identity-assertion graph with namespaced, typed IDs, provenance, conflicts, and pairwise relations. Do not union candidates solely because one provider supplies the same ISRC. Define the canonical-candidate artifact consumed by fusion and enrichment, plus explicit work, recording, release, edit, and sampled-source entities.

### [P1] Several stage contracts remain incompatible or non-executable

**What:** `hints.jsonl` delegates much of its schema to prose in another document. The cache key assumes a window and transform, so it does not cover whole-file scanners. File-scan results can produce many observations from one billable job, making per-observation cost ambiguous. Retry errors and eventual success may coexist without a rule for fusion. `coverage` is undefined when episodes overlap, and neither stable ID generation, path relativity, artifact completion markers, nor run-resume identity is specified.

**Why it matters:** Independently implemented stages can double-count cost, consume stale or partial artifacts, fuse failed attempts, or report more identified duration than the mix contains.

**Concrete change to the plan:** Make Stage 0 contracts self-contained and add semantic test vectors, not only JSON Schema validation. Define separate query/job/result/billing records; cache keys per capability; final-attempt selection; union-duration coverage; deterministic IDs; relative-path rules; upstream artifact hashes; atomic completion manifests; and an explicit resume key distinct from a newly created `run_id`.

### [P1] Paid-engine feasibility is assumed rather than proven

**What:** ACRCloud File Scanning requires containers, buckets, credentials, and product entitlements; its official Python scan tool says played-duration output requires provider permission and notes a Windows Visual C++ runtime requirement. AudD exposes a long-running POST rather than the remote job/poll identifier assumed by `file_scan_jobs`. Panako/custom-catalogue work is postponed until after enrichment and UI, so Stage 2 cannot meaningfully evaluate the underground/reference-pool stratum. See the [ACRCloud scan tool](https://github.com/acrcloud/acrcloud_scan_files_python3) and [File Scanning API](https://docs.acrcloud.com/reference/console-api/file-scanning).

**Why it matters:** Trial accounts may not expose the fields required by the observation contract, and the supposed maximum-accuracy profile may be selected without its most relevant underground strategy.

**Concrete change to the plan:** Add entitlement, metadata-field, cost, upload-size, runtime, and credential smoke tests before engine selection. Include a small legally held reference-pool experiment in Stage 2 using either Panako or an enabled custom bucket. For the maximum-accuracy profile, run every selected whole-file engine across the set; do not cascade paid recognition only after Shazam gaps, because confident Shazam false positives would suppress corrective evidence.

### [P1] Hint timing, corrections, and dependence are modeled too confidently

**What:** Minute-only cues are described both as a 60-second range and as ±60 seconds. `MM:SS` cues receive an unvalidated ±5 seconds, while the measured comment distribution concerns questions—not answers, corrections, or replies—and contains substantial tails beyond the hard `[−120s,+15s]` range. Corrections are modeled as additional hints rather than superseding or negatively weighting the corrected assertion. Exact normalized-text grouping misses copied tracklists with reformatted timestamps or minor edits.

**Why it matters:** Correlated crowd errors can still be counted repeatedly, and correct corrections may lose to the erroneous high-like parent tracklist.

**Concrete change to the plan:** Define source- and hint-kind-specific timing likelihoods from development data rather than hard universal windows. Model `corrects`/`supersedes` and negative evidence explicitly. Perform line-level fuzzy provenance grouping and default MixesDB/1001tracklists/mirror copies to correlated unless independence is demonstrated. Fix cue semantics and add adversarial parser fixtures, including standalone `ID - ID`, dotted decimals, ranges, malformed hours, and conflicting corrections.

### [P1] Hint fetching conflicts with the non-blocking architecture

**What:** Stage 1 ingestion runs yt-dlp with unbounded `--write-comments`, although comments are not implemented until Stage 3a and the plan says hint connectors never block recognition. Section 04 gives no fetch order for a primary Mixcloud URL. Boiler Room-style tracklist pointers are detected but no connector or manual import path consumes them. Title search on 1001tracklists and a two-percent duration check for mirrors are insufficient to distinguish alternate, censored, or reuploaded cuts.

**Why it matters:** A large or failing comment crawl can stall the audio spine, while some of the highest-quality human tracklists are missed or attached to the wrong cut.

**Concrete change to the plan:** Keep core ingest to media and bounded metadata; run comments and external hints as independently resumable connector jobs with explicit pagination caps. Add a Mixcloud-primary flow, an allowlisted pointer/manual-tracklist import, and stronger mirror identity checks using platform IDs, uploader/event/date, duration, and timeline agreement. Make 1001tracklists scraping and undocumented Mixcloud GraphQL optional connectors with fixtures and circuit breakers.

### [P1] Crash recovery and Windows process handling are incomplete

**What:** Clean Ctrl-C handling does not recover jobs left `running` after a crash, reboot, or forced termination. AudD may finish and bill after the local client loses the response, while ACRCloud has a recoverable remote file ID; these are different failure states. SQLite concurrency, budget reservation, secret redaction, and Windows descendant-process termination are not defined.

**Why it matters:** Long jobs can become permanently stuck, be resubmitted and billed twice, or exceed a nominal hard ceiling. Killing only the direct ffmpeg process can leave descendants holding files open on Windows.

**Concrete change to the plan:** Add leases, heartbeats, stale-job reclamation, and an `outcome_unknown` state. Reserve budget transactionally before submission and reconcile actual usage afterward. Use provider-specific idempotency/recovery rules, SQLite WAL with `busy_timeout` and one-writer discipline, schema-enforced secret redaction, and Windows Job Objects or an explicitly tested process-tree mechanism.

### [P1] Fixture minimization and reference acquisition still expose avoidable legal risk

**What:** Raw dumps are now correctly ignored, but six-character deterministic handle hashes are susceptible to collisions and dictionary matching, while retained comment text can itself contain handles, URLs, or uniquely searchable personal content. The research document still incorrectly says raw corpora live under `data/fixtures/comments/`; that requested README does not exist, and the actual file is `data/fixtures/README.md`. Stage 6 still proposes acquiring uploader and label-mate audio for indexing; deleting audio after fingerprinting does not cure scraping, access, or reproduction restrictions.

**Why it matters:** The first commit can still redistribute identifiable UGC, and a future commercial system cannot inherit the proposed candidate-pool acquisition path unchanged.

**Concrete change to the plan:** Use synthetic fixtures by default and retain only minimal real snippets where indispensable; redact mentions, URLs, names, and incidental personal text, using random fixture-local identities rather than short stable hashes. Correct the documentation paths before commit. Restrict reference indexing to user-supplied or explicitly licensed files; candidate discovery may emit links but must not automatically rip them. Before commercial use, disable unofficial/scraping connectors until platform-specific review and provider upload/retention terms are complete.

### [P2] Several acceptance criteria can pass without delivering value

**What:** “Fused ≥ audio-only” can pass by ignoring every hint, “every track has a link” can pass with a generated search URL, and the web seek test verifies arithmetic rather than boundary accuracy. The Stage 2 acceptance criterion requires only that a report exist.

**Why it matters:** An AI coding agent can satisfy the literal stage gates while making no accuracy improvement.

**Concrete change to the plan:** Require minimum supported-coverage gain at non-inferior precision for hints, link correctness audits stratified by version ambiguity, and benchmark quality/completeness gates before accepting Stage 2. Keep UI seek correctness separate from measured boundary error.

## Prior findings status (rounds > 1 only)

| Round-1 finding | Status in Revision 2 |
|---|---|
| Reference offset does not reveal mix-in time | **Partially addressed.** Alignment and boundaries are separated, but the new boundary interval is invalid. |
| Circular/unrepresentative ground truth | **Partially addressed.** Independent verification, strata, and whole-set splits were added; sample size, exact-version verification, and annotation/scoring rules remain open. |
| Missing stage contracts | **Partially addressed.** Named schemas and fields were added, but several capability-specific and cross-stage semantics remain incompatible. |
| One accuracy score hides failures | **Partially addressed.** The requested metric families were added, but their exact definitions are missing. |
| Window schedule has holes | **Partially addressed.** A 10-second hop and tail were added; Shazam’s effective centered excerpt and short-track limitations are not handled. |
| Tempo compensation underspecified | **Addressed in principle.** Separate filters, factor conventions, and mapping are present; effective transformed query spans still need specification. |
| Timeline cannot represent transitions/repeats | **Addressed structurally.** Overlapping episodes and occurrence indices were added; piecewise loop/edit alignment remains missing. |
| Confidence OR rules too permissive | **Partially addressed.** Calibration targets and contradictions were added; version-tier semantics, unavailable Shazam scores, and evidence dependence remain unresolved. |
| One provider interface does not fit all engines | **Partially addressed.** Capability classes were added; AudD and ACRCloud still require different lifecycle/cache semantics. |
| Engine selection happens too late | **Not operationally addressed.** The bake-off was moved to Stage 2, but the necessary adapters remain in Stage 3d. |
| Recording/version identity undefined | **Partially addressed.** Raw labels and relation types were added; canonicalization remains unsafe and underspecified. |
| Text weights are pseudo-probabilities | **Partially addressed.** They are now called features and question clusters no longer raise confidence; calibration and dependence handling remain incomplete. |
| Comment parsing/fetch failure rules | **Partially addressed.** Most requested safeguards were added; correction semantics, Mixcloud-primary order, pointer ingestion, and alternate-cut validation remain. |
| Jobs not resumable/budget-safe | **Partially addressed.** SQLite, ceilings, and atomic writes were added; crash leases, uncertain billing, and capability-specific recovery remain absent. |
| Windows/media-time acceptance | **Partially addressed.** Canonical PCM and several Windows tests were added; raw-PCM/Shazam compatibility and concrete process-tree handling remain unresolved. |
| Panako not executable without Java | **Addressed.** It is conditional on an explicit JDK decision with a Windows smoke test and alternatives. |
| Stage 3 too large | **Partially addressed.** It was split into scored increments, but those increments are misordered around calibration and engine selection. |
| Raw public-comment fixtures | **Addressed in the plan and `.gitignore`.** Raw data is ignored and derived fixtures are proposed; residual minimization/path issues are captured above. |
| Commercial model understated | **Addressed.** Commercialization is now a separate discovery stage with licensing, ingestion, multi-tenancy, and spend risks acknowledged. |
| Acquisition links must be non-authoritative | **Addressed.** Enrichment is downstream and direct links require stronger identity evidence. |

## Questions for the owner

1. Are you willing to fund and participate in a benchmark large enough to support the 99% tier claim, including legally held references and a second-human audit of the frozen test set? If not, that claim and exact-version metrics need to be narrowed.
2. Will you create AudD and ACRCloud trial accounts and authorize a small explicit overage ceiling if trial credits or required metadata fields are insufficient?
3. Will you install a JDK for an early Panako reference-pool experiment, or should Stage 2 use only a paid custom catalogue and treat local self-indexing as out of scope?

## VERDICT

VERDICT: CHANGES_REQUESTED
# Plan review — round 4

## Summary

Revision 4 is materially stronger than earlier versions, especially around provider lifecycles, provisional labels, overlapping episodes, one-sided boundary representation, and fixture hygiene. Two accuracy-critical defects remain: the transformed-window duration/time mapping is mathematically reversed, and the claimed “proved” boundary formulas still infer more than a positive fingerprint establishes. The rescan loop, file-scanner contracts, episode construction, exact-version logic, and certification design also remain insufficiently executable for independent stage-by-stage implementation. Shazam as the free default and an all-engine `max_accuracy` profile are sensible, but Panako/reference-pool work is still sequenced inconsistently. I found no other research claim that I can confidently call outdated as of the stated research date; claims about catalogue superiority, tolerance, and provider cost are appropriately treated as hypotheses or anecdotal estimates.

## Findings

### [P0] Transform spans and sample maps are reversed

**What:** For both `asetrate=16000/r,aresample=16000` and `atempo=1/r`, output duration is approximately `input_duration × r`. Therefore a 12-second output requires an input span of `12000/r`, not `12000·r`. With `r=1.08`, the planned 12.96-second input produces approximately 14.0 seconds, contradicting `output_ms = 12000`; the corresponding output-to-original sample map is approximately `k/r`, not `k·r`. This is a high-confidence factual error based on FFmpeg’s documented tempo semantics. [FFmpeg filter documentation](https://ffmpeg.org/ffmpeg-filters.html#atempo)

**Why it matters:** Every transformed observation would carry the wrong support interval, anchor, alignment slope relationship, boundary evidence, and cache identity. Cropping the unexpectedly long result would add another unrecorded time shift.

**Concrete change to the plan:** Correct the duration algebra and define `r` unambiguously as playback-rate hypothesis versus correction factor. Express every sample map in named units using rational or fixed-point coefficients, including crop/padding and measured WSOLA uncertainty. Store pitch hypotheses with an explicit signed semitone field as well as their ratio, and make known-insertion vectors verify duration, first/last mapped samples, anchor bias, and slope—not merely recognition success.

### [P0] The “proved” boundary equations still overclaim

**What:** A positive match conditionally proves only that recognizable evidence occurred somewhere in its effective support. It does not prove that at least `L_min` contiguous seconds occurred at a known edge. Consequently `support_end − L_min` is not a hard latest-start bound, and `support_start + L_min` is not a hard earliest-end bound. A finite experiment can estimate a latency quantile but cannot establish a universal recognizer minimum. The plan also allows an ordinary `manual_tracklist` cue or any reset event to set a hard censored side; neither necessarily represents an independently verified audible boundary.

**Why it matters:** The system can publish bounds that exclude the true transition, then certify or display them as facts. This is especially likely with low-gain crossfades, sparse percussion, acapellas, loops, late recognizer pickup, and reference playback that begins behind a closed fader.

**Concrete change to the plan:** Without directly localized evidence, use only `start ≤ effective_support_end` and `end ≥ effective_support_start`, conditional on identity correctness. Treat `L_min`, fingerprint latency, ordinary human cues, and alignment extrapolation as prediction evidence. Permit hard opposite-side bounds only from audited ground truth or a defined source-to-mix alignment that localizes audible contribution. Define the controlled audible rule with its measurement window, smoothing, hysteresis, minimum run length, silence handling, and per-stem calculation.

### [P1] Coverage and gap semantics contradict censored boundaries

**What:** Before prediction intervals exist, `possible_identified_ms` is set equal to the proved inner interval. Yet the missing boundary sides are explicitly censored, meaning the episode may extend beyond that interval. Everything outside these inner intervals can consequently become a `gap`, despite the rule that no-match evidence cannot establish absence. `certain_identified_ms` is also a misleading name when it includes episodes whose badge is merely `possible`.

**Why it matters:** The UI and aggregate metrics will overstate “ID” gaps and understate plausible track coverage, particularly around every transition and sparse recognition region.

**Concrete change to the plan:** Separate evidence-supported duration, predicted episode duration, unresolved boundary duration, scanned-no-match duration, and unscanned duration. Do not call censored episode tails gaps. Add partition and union test vectors proving that all duration fields remain within media duration and that overlap cannot double-count time.

### [P1] Adaptive rescanning has no valid stage contract

**What:** Fusion queues rescans after `windows.jsonl` and `observations.jsonl` have already been produced, but no rescan-request artifact, iteration protocol, convergence rule, or ownership transition exists. Having fusion add windows would violate the one-owner rule; rerunning the windows stage from fusion output creates a circular dependency absent from the sidecars.

**Why it matters:** Agents can implement incompatible loops, continually rewrite supposedly immutable artifacts, lose earlier observations, or rescan indefinitely until a budget is exhausted.

**Concrete change to the plan:** Define an explicit rescan-plan artifact and an orchestrator-owned iteration contract: deterministic request IDs, input artifact hashes, append/union semantics, maximum rounds, budget ordering, termination criteria, and how each new recognition/fusion generation is versioned. Fusion should request work, not mutate window or query artifacts.

### [P1] Alignment does not collapse correlated transform hypotheses

**What:** `T` counts one vote per `trial_group_id`, but alignment consumes all final matching observations. Several transform variants of the same parent window can therefore create multiple points at the same mix time with different supports, rates, or reference offsets. Trying more hypotheses also increases false-match opportunity; grouping votes alone does not remove that multiple-testing effect.

The event state machine is also inconsistent: a mix gap over 30 seconds creates a new episode before reference continuity, loops, or resets are considered. A correct match after a 31-second recognition drought is therefore split even when its reference offset advances perfectly, while a quick replay/reset may remain in the old episode.

**Why it matters:** Transform compensation can degrade otherwise valid alignments, and long difficult passages, loops, cue jumps, or repeated tracks will be split or merged arbitrarily.

**Concrete change to the plan:** Specify per-trial hypothesis selection or a robust multi-hypothesis assignment before fitting. Retain rejected hypotheses as evidence, but allow at most one fitted point per logical trial unless a provider genuinely reports simultaneous sources. Determine continuation from reference-offset consistency, not an unconditional time gap. Define precedence for continuation, loop, reset, replay, jump, drift, and outlier decisions, with precision as well as recall tests for each event type.

### [P1] Certification is neither statistically nor semantically complete

**What:** The stated zero-error Clopper–Pearson counts are correct only for independent predictions. Episodes from the same mix share genre, mastering, source quality, provider coverage, and transition style; reporting a bootstrap-by-set result without using it in the certification gate does not repair the independence assumption. “Boundary correctness” is also not defined for tier certification: the report lists 5/10/30-second tolerances, bound violations, and interval coverage without selecting the event whose correctness is tested. It is also unclear whether tier populations are exact-tier or cumulative at-or-above-tier.

The schema has one global `calibration.status`, although certification is defined separately by profile, dimension, and tier.

**Why it matters:** A profile could be labelled certified using an invalidly narrow interval or an arbitrary boundary definition, while some dimensions and tiers have insufficient evidence.

**Concrete change to the plan:** Pre-register cumulative tier populations, exact correctness events for work/version/start/end/boundary, and cluster-aware confidence gates with a minimum number of independent sets. Store certification status and test counts per `(profile, dimension, tier)`. Treat the 299/29/9 counts as lower bounds before clustering, not sufficient totals.

### [P1] Benchmark episode matching conflates identification with boundary completeness

**What:** Hungarian matching requires IoU ≥ 0.3 between the proved inner interval and the full truth hull. A correctly identified five-minute episode with only forty seconds of fingerprint support has IoU around 0.13 and is scored as an identity false positive plus false negative. Thus identity precision and recall can fail solely because the deliberately conservative boundary representation is incomplete.

**Why it matters:** The benchmark cannot tell whether recognition, occurrence assignment, or boundary inference failed, undermining ablations and confidence calibration.

**Concrete change to the plan:** Associate predictions to truth using identity plus temporal compatibility of their actual supporting observations, with an explicit one-to-one occurrence rule. Score identification, occurrence counting, segment coverage, and start/end boundaries separately. Continue reporting episode IoU, but do not make a boundary-sensitive IoU threshold the sole identity association mechanism.

### [P1] Stage 2 does not create the corpus Stage 4 requires

**What:** Stage 2 requires only six sets and sixty episodes, while the larger calibration/test corpus is described as an “input” with no stage responsible for selecting, acquiring, annotating, auditing, freezing, or accepting it. Stage 1 implements a complex fuser before the scorer and controlled generator exist. “Catalogue-covered” is also not given an engine-independent inclusion rule, creating a risk that easy, successfully recognized sets define the benchmark.

**Why it matters:** Stage 4 can be reached without enough data to calibrate or certify anything. Ground-truth assembly—especially exact versions, overlapping stems, and boundaries—is likely to be the dominant human task, not a small tooling subtask.

**Concrete change to the plan:** Make corpus construction a named, blocking or explicitly optional deliverable with set and episode quotas per split, independent selection rules, locally held references, annotation blinding, second-pass disagreement handling, and a freeze manifest. Include representative open-world mixes rather than only known catalogue successes. Move scorer vectors and a small controlled truth vertical slice before baseline-fuser acceptance.

### [P1] Exact-version confidence and identity merging remain unsafe

**What:** A version qualifier “agreed by all sources” is vacuously satisfied by one source and does not establish that the heard audio is that version. Conversely, providers often omit qualifiers. The graph permits a one-source `same_recording` union when normalized text matches, which can merge radio, extended, remastered, instrumental, and otherwise identically labelled recordings. The phrase “no transitive closure beyond what unions produce” is internally contradictory because union-find is transitive; `work_id` construction is not specified.

**Why it matters:** The system can manufacture exact-version agreement and recommend the wrong release—the owner’s most costly error class.

**Concrete change to the plan:** Do not use normalized text alone as recording equivalence. Define separate work and recording components, typed evidence required for each relation, conflict propagation, and deterministic candidate/work IDs. Keep exact version `unclear` unless supported by recording-specific identifiers with corroboration, an aligned held reference, or independently audited version evidence. Add adversarial original/remix/edit/radio/extended/instrumental fixtures.

### [P1] Mashups, acapellas, samples, and changing roles are still not executable concepts

**What:** An episode has one unspecified `role`, while a track can move from incoming to dominant to outgoing and can be an acapella layer over another track. The identity graph lacks a `mashup_of`/component-performance relationship, and a detected sample or vocal source can be mistaken for the recording actually being played. Classifying overlaps longer than 60 seconds as layers is duration-based rather than evidence-based.

**Why it matters:** The data model can store two overlapping names but cannot express what those detections mean. Work-level component recognition may inadvertently promote an unsupported exact track or mashup claim.

**Concrete change to the plan:** Define time-varying role segments and explicit composite/component relations. Separate “audio contains evidence of work X” from “recording X is the performed episode.” Require benchmark confusion metrics for played recording versus sampled/acapella component, plus dominant and secondary-layer precision and recall.

### [P1] Several core contracts remain incompatible

**What:** Important unresolved contract defects include:

- File-scanner observations require `trial_group_id` and `transform`, although no window sibling or transform necessarily exists.
- The file-scan cache key omits whether `original` or `pcm` was uploaded and the uploaded byte hash.
- `sample_map.a` and `.b` have no units or canonical numeric encoding.
- Connector jobs still contain an ellipsis instead of an executable schema.
- Raw responses and measured provider configurations are referenced but are not declared stage-owned artifacts with hashes.
- Identity assertions/candidates do not have complete natural-key rules.
- `benchmark_report.json` is described only indirectly.

**Why it matters:** Stage 0’s agent must invent material semantics, so later agents can validate schemas while still disagreeing about identity, time, caching, and provenance.

**Concrete change to the plan:** Require self-contained schemas and semantic vectors for these cases. Make clip trial grouping nullable or define a capability-neutral logical-trial ID. Include the exact uploaded asset hash in scanner queries/cache keys. Version measured provider configuration immutably rather than “writing results” into mutable configuration.

### [P1] Hint parsing rejects common DJ-set notation

**What:** Requiring `mm ≤ 59` rejects common two-component cues such as `75:30`; two-component timestamps should allow unbounded minutes subject to media duration. The `hh ≤ 23` limit is similarly unnecessary for long recordings. “Bare hyphens never split” avoids damage to names but also fails the empirically observed Bigos/MixesDB form `(05)Artist-Title`. The research weights are correctly described as hand features, but Stage 3a still does not define how hints affect the baseline fuser before Stage 4 calibration.

The research fetch order covers SoundCloud and YouTube but not a primary Mixcloud URL. A 1001tracklists title search is performed, yet its result cannot be consumed unless an existing hint already names a specific `1001.tl` URL.

**Why it matters:** High-value complete tracklists will be missed, while an agent may invent arbitrary feature weights to meet the Stage 3a coverage gate.

**Concrete change to the plan:** Parse `MM:SS` and `H:MM:SS` by component count and total duration. Use block-level delimiter consistency to handle no-space hyphens conservatively rather than banning them. Define primary fetch flows for every supported platform and quarantine search-discovered mirrors until metadata/timeline verification or manual confirmation. Keep questions as rescan signals only, and evaluate hint features on held-out sets rather than the same six-set development corpus used to tune them.

### [P1] Build stages duplicate scope and remain too large for an autonomous agent

**What:** Stage 1 already implements transforms, segments/events, gaps, coverage, exports, job recovery, process containment, and a one-hour end-to-end run. Stage 3b then “builds” transform variants, while Stage 3c builds the “full algorithm above” for alignment and episodes a second time. Several table gates also omit the corpus version, confidence bound, non-regression measure, or event precision required by the general gate rule.

**Why it matters:** A vibe-coding owner cannot reliably determine whether a stage is complete, and agents can interpret later stages as rewrites rather than incremental, benchmarked changes.

**Concrete change to the plan:** Assign each capability to one stage and make earlier stages expose only the minimal contract needed by the next. Give every recognition gate a frozen input report, exact command, denominators, paired comparison, precision/recall safeguards, and artifact expected on success. In particular, do not accept loop/cue-jump recall without corresponding false-event precision.

### [P1] Reference-pool recognition is frozen before it exists

**What:** Stage 3d may include a JDK-enabled reference-pool path, but Stage 2 provides only an optional Panako smoke test and the full Panako provider is not built until Stage 7. A tiny index smoke test cannot support the Stage 3c ablation or the Stage 3d profile decision.

**Why it matters:** The accuracy profile can be frozen before testing the only planned free engine designed specifically for scale changes against held references.

**Concrete change to the plan:** If Panako is selected, build the minimum index/query/normalization path before Stage 3c and profile freeze. Otherwise explicitly exclude Panako and reference-pool certification from v1; an enabled ACRCloud custom bucket is a valid alternative only after its entitlement and evidence fields have been demonstrated. The current Shazam-first free profile and all-engine `max_accuracy` policy are otherwise correctly ordered.

### [P1] Retry ownership is not pinned

**What:** The plan specifies at most five Shazam retries, but the current upstream ShazamIO client constructs an internal retry policy with twenty attempts unless a custom HTTP client is supplied. An outer five-retry adapter could therefore produce far more network attempts than the job store records. [ShazamIO API source](https://github.com/ShazamIO/ShazamIO/blob/master/shazamio/api.py)

**Why it matters:** Rate limits, circuit breakers, request ceilings, cancellation, and failure-injection assertions become inaccurate.

**Concrete change to the plan:** Designate exactly one retry owner, inject a configured HTTP client, and count every physical network attempt against telemetry and request ceilings. Pin and test this behavior using a fake HTTP server. Also name the Windows API/library used for suspended-process Job Object assignment; standard asyncio subprocess creation does not itself provide that complete lifecycle.

### [P1] Fixture hygiene does not yet cover benchmark artifacts

**What:** The earlier raw-comment fixture issue is addressed: `data/raw/` is ignored and the fixture README now requires random fixture-local identities and an audit. However, `ground_truth.json` includes source URLs and uploader fields, benchmark responses are to be frozen into a corpus, and a baseline report is committed. The plan does not say whether these pass the fixture audit or remain local.

**Why it matters:** Public handles, platform IDs, comment text, provider payloads, and source linkage can re-enter version control outside `data/fixtures/`, despite the corrected fixture policy.

**Concrete change to the plan:** Define a repository-wide committed-corpus policy. Keep source-linkage maps and raw provider/comment responses local unless redistribution is authorized; commit pseudonymous truth, aggregates, and synthetic/redistributable fixtures only. Extend auditing to every committed benchmark/report artifact. Before commercial use, explicitly include yt-dlp YouTube comment extraction among the unofficial data-access paths requiring review.

## Prior findings status (rounds > 1 only)

The table consolidates duplicated findings across rounds. “Partial” means a material part remains unaddressed.

| Earlier finding | Rounds | Status in Revision 4 |
|---|---:|---|
| Reference offsets are not audible boundaries | 1–3 | **Partial:** alignment and boundary confidence are separated, but `L_min`, reset, and manual-cue hard bounds remain invalid. |
| Circular or unrepresentative truth | 1–3 | **Partial:** strata, whole-set splits, and verification exist; corpus construction, open-world selection, blinding, and Stage 4 feasibility do not. |
| Missing/incompatible stage contracts | 1–3 | **Partial:** substantially improved, but scanner trials/cache keys, rescan generations, raw-response ownership, identity IDs, and connector schemas remain open. |
| Metrics hide abstention/failure modes | 1–3 | **Largely addressed:** the metric inventory is strong; episode association and certification correctness are still defective. |
| Baseline schedule has blind holes | 1, 3 | **Addressed:** 12/9 plus a tail is coverage-complete for a theoretical three-second contained event. Recognizer recall at three seconds remains an empirical question. |
| Tempo/pitch compensation semantics | 1–3 | **Not addressed for transformed support:** separate behaviors and pitch-only hypotheses were added, but the current duration and sample-map formulas are wrong. |
| Timeline cannot represent overlaps/repeats | 1–3 | **Partial:** overlapping episodes and piecewise events exist; repeat splitting, transform deduplication, changing roles, and composite performances remain underspecified. |
| Confidence OR rules and work/version conflation | 1–3 | **Partial:** provisional tiers and separate dimensions are improvements; exact-version promotion and certification semantics remain unsafe. |
| One provider interface does not fit all engines | 1–2 | **Largely addressed:** capability classes and lifecycles exist; file-scanner trial and cache contracts still need correction. |
| Engine selection/profile timing | 1–3 | **Partial:** paid adapters moved earlier and profile freeze moved later; Panako/reference-pool implementation is still after profile freeze. |
| Canonical recording identity unsafe | 1–3 | **Partial:** the assertion graph and conflict veto help, but text-based recording unions and work construction remain unsafe. |
| Text weights are pseudo-probabilities | 1–3 | **Partial:** they are now features and provenance is modeled; no executable pre-calibration fusion policy or adequate held-out validation exists. |
| Hint timing/parsing/fetch failures | 1–3 | **Partial:** most safeguards were added; long `MM:SS`, no-space tracklists, Mixcloud-primary flow, and search-result handling remain. |
| Long jobs not resumable or budget-safe | 1–3 | **Addressed in substance:** durable submission states, reservations, leases, and `outcome_unknown` now exist. |
| Paid submission can double bill | 2–3 | **Addressed in substance:** pre-network state and manual acknowledgement exist; provider reconciliation must still be tested rather than assumed. |
| Windows/media/cache handling | 1–3 | **Largely addressed:** canonical PCM, Job Objects, writer queue, TTLs, and failure tests exist; retry ownership and the concrete suspended launcher remain. |
| Panako impossible without Java | 1–3 | **Addressed conditionally:** the JDK decision is explicit, but its build order is not. |
| Stage 3/build order too large or invalid | 1–3 | **Partial:** stages were split, but Stage 1/3b/3c duplicate substantial scope and benchmarking still arrives late. |
| Entirely missed short layers cannot be labelled | 2–3 | **Addressed conceptually:** the plan now calls them a benchmark limitation; gap/possible-coverage semantics still need correction. |
| Raw public-comment fixtures | 1–3 | **Addressed for fixtures:** raw files are ignored and random local identities/auditing are specified. Benchmark artifacts remain a separate gap. |
| Commercial model and platform terms understated | 1–3 | **Largely addressed:** commercialization is a separate discovery stage with provider and platform gates. |
| Acquisition links must be non-authoritative | 1–2 | **Addressed.** |
| Shazam anchor semantics unverified | 2–3 | **Mostly addressed:** all matches, clustering, and insertion tests are present; measured bias must explicitly be applied, not merely recorded. |
| Acceptance gates can pass without value | 2–3 | **Partial:** several gates improved, but Stage 2, event detection, Panako, and the general/table gate mismatch remain. |
| Export/player discard uncertainty | 3 | **Addressed:** flattening and configurable lead-in are defined. |

## Questions for the owner

1. Will you provide legally held source recordings and fund the annotation effort needed for a representative calibration/test corpus, or should v1 explicitly stop at provisional confidence?
2. Will AudD and ACRCloud credentials plus a hard test budget be available before Stage 2?
3. Will you install a JDK and include a minimum Panako implementation before profile freeze, or should reference-pool recognition be excluded from v1?

## VERDICT

VERDICT: CHANGES_REQUESTED
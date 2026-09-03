# Plan review — round 1

## Summary

The plan has a promising evidence-fusion direction, but its current benchmark cannot reliably validate accuracy and its timestamp method confuses a reference-track offset with the audible start of a track in the mix. The pipeline is described narratively rather than through versioned stage contracts, so independent AI-agent implementation would produce incompatible assumptions about time, identity, retries, and confidence. Recognition scheduling, confidence tiers, and text weights are plausible hypotheses, not defensible defaults yet, and several important DJ cases require overlapping track episodes rather than a single linear tracklist. The local-first v1 is feasible after restructuring, but the two-week estimate and “hosted-ready packaging job” claim are unrealistic.

## Findings

### [P0] Reference offset does not reveal the track’s mix-in time

**What:** The proposed formula, `window start − position in song`, estimates when position zero of the reference would align with the mix. DJs routinely cue from the middle, loop sections, jump cue points, or play edits. Tempo changes also make the subtraction invalid without a scale factor.

**Why it matters:** This can report starts minutes before the track actually becomes audible. It particularly breaks repeated tracks, loops, live edits, acapellas, and tracks introduced from a nonzero cue point.

**Concrete change to the plan:** Treat observations as `(mix_time, reference_time)` pairs and robustly fit `reference_time = rate × mix_time + intercept` for match consistency. Keep the resulting reference alignment separate from the audible episode boundary. Estimate episode start/end from first/last supporting observations plus dense boundary sampling; optionally refine against legally held source audio using subsequence alignment. Give identity and boundary separate confidence values.

### [P0] The proposed benchmark ground truth is circular and not representative

**What:** The six seed sets have no assembled ground truth, and the plan proposes deriving it from the same comments, MixesDB entries, and crowd tracklists that the system will consume as input. Mixotic is valuable for self-indexed fingerprint evaluation, but its CC catalogue does not establish commercial-engine catalogue coverage on present-day club music.

**Why it matters:** A copied error can appear as both prediction and truth, materially inflating text-hint and fusion accuracy. Approximate crowd cue times also cannot validate the promised “true start time.”

**Concrete change to the plan:** Build independently audited gold annotations containing recording/version identity, audible start and end ranges, overlaps, repeated occurrences, and uncertainty. Separate the benchmark into:

- Current released music covered by global recognition catalogues.
- Underground/unreleased material with a known reference pool.
- Controlled pitch, tempo-with-key-lock, coupled pitch/tempo, EQ, looping, and crossfade transformations.
- Mixotic/UnmixDB-style self-index evaluation.

Split by entire set—not windows—into development, calibration, and frozen test sets. Report audio-only, hints-only, and fused results separately. Any comments used to assemble truth must be independently verified against the audio and source recordings.

### [P0] Stage boundaries lack executable data contracts

**What:** There are no schemas for ingestion manifests, windows, engine observations, normalized identities, hints, episodes, failures, or benchmark annotations. Time units, coordinate systems, nullability, provenance, partial-run behavior, and schema evolution are unspecified.

**Why it matters:** An AI agent implementing one stage cannot know what the next stage expects. The most likely result is incompatible JSON, lost raw evidence, incorrect offset interpretation, and expensive rewrites during Stage 2 or 3.

**Concrete change to the plan:** Before Stage 1 implementation, specify versioned schemas and golden examples for at least:

- `run_manifest`: source identity, canonical URL, media hash, duration, timebase, metadata snapshot and tool versions.
- `window`: integer millisecond bounds, transform type/factor, decoded-audio hash and parent asset.
- `engine_observation`: provider/config version, match/no-match/error status, raw identity, provider IDs and score, sample/reference offsets, billable units and raw-response reference.
- `hint`: the proposed model, with identity, parser and positional confidence separated.
- `track_episode`: possibly overlapping start/end bounds, candidates, evidence, identity confidence, boundary confidence and tier.
- Ground truth and scorer outputs.

Define deterministic ordering, units, retry/error semantics, and acceptance fixtures for every stage.

### [P1] “One accuracy score” would conceal the important failure modes

**What:** “Found / missed / wrong / start-time error per tier” is insufficient, especially for an abstaining system whose stated goal is to avoid confident errors.

**Why it matters:** A system can improve apparent accuracy by emitting fewer tracks, or improve recall by flooding the output with false “Possible” matches. Averages also hide catastrophic performance on particular sets.

**Concrete change to the plan:** Require, per set and per benchmark stratum:

- Episode-level precision, recall and F1 at exact-recording and work/title level.
- Duration-weighted precision and recall.
- Selective precision/recall and coverage as confidence thresholds change.
- Empirical precision for every tier, calibration error and false-discovery rate.
- Start/end median absolute error, p90 error, percentages within 5/10/30 seconds, and segment IoU.
- Recall for repeated occurrences and multilayer/overlap regions.
- Unknown-duration ratio and false filling of true unknowns.
- Per-engine oracle coverage and marginal contribution through ablations.
- Requests, billable seconds, estimated cost and wall time.

Bootstrap uncertainty by set and never tune thresholds on the frozen test split.

### [P1] The baseline window schedule is neither overlapping nor coverage-complete

**What:** Twelve-second windows every fifteen seconds leave three-second holes. Tracks or overlays shorter than the hop can be completely missed, and adaptive rescanning only in apparent gaps will not revisit a region hidden by a false positive.

**Why it matters:** This directly conflicts with the accuracy-first requirement and the plan’s description of “overlapping pieces.”

**Concrete change to the plan:** Make sampling engine-specific. Benchmark Shazam hops such as 5, 10 and 15 seconds, with 10 seconds a more defensible initial accuracy-oriented default. Always include an end-anchored tail window and handle inputs shorter than one window. Trigger dense rescanning around change points, low-confidence regions, conflicting identities and suspiciously long detections—not only empty gaps.

### [P1] Tempo compensation is technically underspecified

**What:** “Pitch-corrected copies at −8/−4/+4/+8%” does not say whether FFmpeg should resample, change tempo while retaining pitch, or shift pitch without changing tempo. Those represent different DJ behaviors. The sign convention and expected relationship between transformed offsets and original mix time are also absent.

**Why it matters:** CDJ playback with key lock changes tempo but not pitch; without key lock, pitch and tempo move together. Implementing the wrong transform can reduce recognition while multiplying requests fivefold.

**Concrete change to the plan:** Define separate hypotheses for coupled speed/pitch change and tempo-only key-lock playback, including exact filters, factor conventions and time-coordinate mapping. Preserve the unmodified query. Benchmark transform grids rather than declaring ±4/8 optimal, and record the transform in every observation/cache key. Use offset slopes to validate a hypothesized playback rate.

### [P1] The timeline model cannot represent normal DJ transitions

**What:** “Suppress bouncing” assumes only one valid track can occupy a region. Crossfades, mashups, acapellas, loops and samples often produce two legitimate identities. Grouping solely by track name can also merge separate appearances of the same track.

**Why it matters:** The system will discard valid transition detections, mislabel mashups, and collapse tracks replayed later in the set.

**Concrete change to the plan:** Model occurrences as independent track episodes with overlapping intervals. Cluster by temporal continuity and alignment, not global identity. Preserve simultaneous candidates during transitions and allow roles such as primary, incoming, outgoing, acapella/sample or uncertain layer. Flatten to a conventional tracklist only in exporters that require it.

### [P1] The confidence-tier OR rules are too permissive

**What:** “Two engines agree” immediately yields Verified even though engines and metadata catalogues are not necessarily independent. Three nearby windows can repeatedly recognize the same loop or sampled source. “Steadily,” “nearby,” “clean match quality,” and “weak hit” have no numeric definitions.

**Why it matters:** The system may confidently name the sampled original instead of the played remix, precisely the high-cost error the owner wants to avoid.

**Concrete change to the plan:** Define minimum supported mix span, allowed gaps, offset-fit residuals, plausible rate range, provider-score normalization, contradictions, and version agreement. Treat correlated evidence and copied text sources as less than independent votes. Calibrate tiers to explicit holdout precision targets—e.g. Verified around 99% identity precision—rather than assigning tiers through fixed OR clauses. Separate confidence in work identity, exact version and boundary.

### [P1] One “engine plug-in” interface does not fit the selected engines

**What:** Shazam is a clip recognizer; AudD Enterprise accepts a long file and meters server-side 12-second chunks; ACRCloud File Scanning uses file-level scanning policies and containers; Panako depends on a local indexed catalogue. “Send each window to every enabled engine” is therefore inaccurate. ACRCloud’s official interface explicitly supports traverse scanning against selected buckets rather than requiring client-generated windows. [ACRCloud File Scanning documentation](https://docs.acrcloud.com/reference/console-api/file-scanning)

**Why it matters:** A lowest-common-denominator clip interface loses provider-native timelines, complicates offsets, duplicates preprocessing, and makes resumability and cost accounting incorrect.

**Concrete change to the plan:** Define provider capabilities such as `clip_recognizer`, `file_scanner`, `local_index_query`, `supports_custom_catalogue`, `returns_reference_offset`, and `billing_unit`. Normalize all results into the observation schema, but retain provider-native raw results and lifecycle. File scanners need upload/job/poll/resume handling rather than the Shazam window queue.

### [P1] Engine selection is being validated too late for an accuracy-first project

**What:** Stage 2 benchmarks only the rough Shazam spine; AudD and ACRCloud trials arrive inside the overloaded Stage 3. Claims such as ACRCloud having the best underground coverage and AudD having weaker coverage are plausible but not demonstrated on the owner’s target mixes.

**Why it matters:** Fusion, schemas and scheduling may be designed around Shazam before learning that another engine produces materially different evidence or much better coverage.

**Concrete change to the plan:** Keep Shazam as the free runtime default, but use trial credits during Stage 2 to measure all candidate engines on the same frozen subset. Report each engine, pairwise agreement, union/oracle coverage and marginal gain. Select default “free” and “maximum accuracy” profiles from evidence rather than making the paid providers late add-ons.

### [P1] Recording identity and remix/version resolution are undefined

**What:** Artist/title normalization is treated as sufficient for agreement. DJ contexts routinely contain bootlegs, VIPs, live edits, reissues, aliases, featured artists, mashups and an engine identifying the underlying original.

**Why it matters:** Aggressive normalization creates false engine agreement and can produce an acquisition link for the wrong recording.

**Concrete change to the plan:** Preserve immutable provider labels and identifiers, then create a separate canonical candidate entity with recording-level IDs such as ISRC where available. Define exact-version, work-level and sampled-source relationships. Fuzzy title normalization may support clustering but must never silently erase remix/edit qualifiers.

### [P1] Text weights are unsupported pseudo-probabilities

**What:** Values such as MixesDB `1.0`, Mixcloud `0.95`, and question-boundary `0.3` are called multiplicative priors without a probabilistic model. MixesDB, 1001tracklists and highly liked comments may copy one another, so their votes are correlated. “Human-verified plus engine is near-certain” is too strong.

**Why it matters:** The fusion layer can become confidently wrong while appearing mathematically principled.

**Concrete change to the plan:** Treat source, parse quality, author authority, identity specificity, temporal precision and independence as separate features. Learn or calibrate their influence on the development set, with conservative hand rules only until sufficient data exists. A question cluster should guide resampling or mark listener interest; it should not move an episode boundary or identity confidence directly.

### [P1] Comment parsing and fetch heuristics need stronger failure rules

**What:** Resolving a SoundCloud `@handle` to the nearest earlier comment is not a reliable thread reconstruction. Minute-only MixesDB cues are treated too much like exact timestamps; bare-hyphen splitting can damage names; single-line timestamp splitting can split unrelated durations or text. Search results and cross-platform “mirrors” may refer to different edits of a set. Mixcloud GraphQL is an undocumented application endpoint and should be expected to break.

**Why it matters:** Incorrectly linked answers or mismatched mirrors become high-weight identity evidence at the wrong time.

**Concrete change to the plan:** Make reply linkage explicitly uncertain unless there is a unique recent addressed user and compatible time. Represent cue precision/ranges, validate mirror duration and set identity, and require block-level monotonicity/coherence before parsing tracklist lines. Fetch connectors independently and best-effort after canonical source resolution; no external hint source should block audio recognition. Cache raw responses and include fixtures for malformed timestamps, Unicode, title-first lines, multi-hour sets, deleted comments, pagination and alternate cuts.

### [P1] Long-running jobs are not adequately resumable or budget-safe

**What:** “Rate-limited, retried, and cached” omits a persistent job state model, cancellation behavior, partial files, cache versioning and provider budget limits. AudD explicitly advises setting a hard `limit` because its enterprise endpoint meters every processed 12-second chunk. [AudD cost-control guidance](https://audd.io/resources/concepts/enterprise-cost-control)

**Why it matters:** A multi-hour Windows run can be lost to a reboot, Ctrl-C, endpoint failure or corrupt partial cache. A development mistake can also consume an entire paid-file budget.

**Concrete change to the plan:** Persist per-provider work in SQLite or an equivalent transactional store with `pending/running/succeeded/no_match/retryable/permanent_failure` states, attempts, next retry and raw response. Use atomic file replacement and cache locks. Cache keys must include audio hash, interval, transform, encoder/tool version, provider version and relevant configuration; negative matches need a deliberate TTL. Add per-run hard request/cost ceilings, clean cancellation and file-scanner resume/poll behavior.

### [P1] Windows and media-time handling need explicit acceptance criteria

**What:** “Nothing is transcoded” is imprecise: yt-dlp extraction can remux or transcode depending on the selected source, while fingerprint queries still require deterministic decoding. HLS discontinuities, VBR seeking, Unicode paths, locked temporary files and child-process cancellation are not addressed.

**Why it matters:** Millisecond timestamps are meaningless if window extraction drifts, and Windows subprocess failures are a common source of stuck or unrecoverable runs.

**Concrete change to the plan:** Add an environment preflight for `uv`, Python 3.12, ffmpeg/ffprobe, Node and supported wheel imports. Define one decoded PCM contract per recognizer, accurate-seek strategy and duration validation against ffprobe. Require tests for spaces/Unicode/long paths, short and corrupt media, cancellation, timeout process-tree cleanup, restart after partial extraction and simultaneous cache access.

### [P1] Panako Stage 6 is not executable in the stated environment

**What:** The machine has no Java, while Panako officially requires JDK 11 or later. Its own README also flags AGPL licensing and potential fingerprinting-patent considerations. [Panako README](https://github.com/JorenSix/Panako)

**Why it matters:** An AI agent cannot complete Stage 6 from the declared toolchain. “Run it as a separate process” does not by itself settle AGPL combination questions, and a commercial shared index also needs rights to acquire and fingerprint the reference catalogue.

**Concrete change to the plan:** Make Stage 6 conditional on an explicit JDK installation decision and add a Windows smoke test before adopting Panako. Benchmark ACRCloud/AudD custom-catalogue options as alternatives. Before commercial use, obtain a specific licence assessment covering Panako integration, relevant patents, and reference-audio acquisition; deleting audio after fingerprinting does not cure an earlier ToS or copyright violation.

### [P1] Stage 3 is too large to support evidence-based development

**What:** It combines tempo transforms, hint parsing, two paid integrations, offset logic, adaptive sampling, fusion and confidence tuning. The “one week” estimate has no entry/exit criteria.

**Why it matters:** If accuracy changes, the cause will be unknowable; failures will be hard for a vibe-coding owner to review, and an agent may declare the stage complete with only happy-path demos.

**Concrete change to the plan:** Define benchmark and schema work before the rough combiner, then make each Stage 3 capability a separately scored increment with regression gates and saved reports. Stabilize the output schema before interleaving the web page. Replace time estimates with acceptance criteria, fixtures, commands and required benchmark deltas. Keep per-run provider usage telemetry early, but defer subscription-cap machinery.

### [P1] Raw public-comment fixtures should not be committed as described

**What:** The raw fixture directory is currently untracked, which is an opportunity to fix it before commit. It contains usernames, handles, IDs, timestamps and searchable comment bodies. “Don’t redistribute beyond this repo” is contradictory because committing or sharing the repository is redistribution.

**Why it matters:** Public availability does not grant unrestricted dataset redistribution rights. The corpus also creates avoidable privacy, deletion-request and platform-ToS exposure.

**Concrete change to the plan:** Keep raw dumps outside version control and add them to ignore rules. Commit a minimized derived fixture set with synthetic or transformed handles, removed IDs/profile URLs, reduced timestamps and only the text necessary for parser behavior; prefer authored synthetic cases for most tests. Maintain a local provenance/retention record. Before commercial use, review each platform’s API/scraping and UGC terms, implement deletion/retention controls, and avoid persisting credentials or browser cookies in manifests and logs.

### [P1] The commercial model understates both engineering and unit-cost risk

**What:** “Hosted later is a packaging job” omits queues, multi-tenancy, abuse controls, secrets, user-data deletion, content rights, observability and provider contracts. Browser-side Shazam moves traffic but does not license unofficial automated access; Vibra demonstrates technical WASM support, not commercial permission. [Vibra project](https://github.com/BayernMuller/vibra) An uncapped “Unlimited” tier is also incompatible with nonzero per-hour provider costs.

AudD’s public material is itself inconsistent: its enterprise documentation says plans start at $2/1,000 chunks, while its main pricing page lists $5/1,000 pay-as-you-go—about $0.60 versus $1.50 for a fully scanned hour. Therefore the plan’s single $0.60 figure is unsafe with high confidence. [AudD enterprise documentation](https://docs.audd.io/enterprise/), [AudD pricing](https://www.audd.io/) The ACRCloud $1.40/hour and “best coverage” statements are anecdotal rather than verified pricing/benchmark facts; confidence medium.

**Why it matters:** The proposed margins are not predictable, and the legal ingestion model could require architectural changes rather than deployment work.

**Concrete change to the plan:** Treat commercialisation as a separate discovery stage. Obtain written provider pricing/licensing, use user uploads or another explicitly licensed ingestion path, add enforceable spend/fair-use caps, and exclude unofficial Shazam from any commercial entitlement calculation. Retain clean provider boundaries now, but remove the claim that hosted operation requires no rewrite.

### [P2] Acquisition links must remain candidates, not identification evidence

**What:** Deezer, iTunes, MusicBrainz and retailer search results may canonicalize to a more popular original or similarly named release.

**Why it matters:** Correct recognition can be followed by a confidently wrong download or purchase link, especially for remixes and white labels.

**Concrete change to the plan:** Keep enrichment downstream and non-authoritative. Record source, query and match confidence; require exact identifiers or strong artist/title/version agreement before presenting a direct item. Otherwise show a labelled search link. Do not let enrichment rewrite the recognized identity.

## Questions for the owner

1. Can you provide legally held source recordings—or enough access to obtain them—and spend time manually auditing start/end boundaries for a frozen benchmark? Without this, exact boundary accuracy cannot be measured credibly.
2. Are you willing to install a JDK for Stage 6, or should Panako be excluded from the Windows v1 and treated as a later experiment?

## VERDICT

VERDICT: CHANGES_REQUESTED
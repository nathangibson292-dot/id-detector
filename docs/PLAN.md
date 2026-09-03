# id-detector — project plan

*Revision 5, 2026-09-03 — final pre-build revision after Codex reviews [1](reviews/plan-review-round-1.md), [2](reviews/plan-review-round-2.md), [3](reviews/plan-review-round-3.md), [4](reviews/plan-review-round-4.md). Round-4 P0s are fixed here; remaining round-4 P1s are addressed where they change contracts, and the rest are tracked in [reviews/README.md](reviews/README.md). Priority: **accuracy of identification** over cost or speed.*

## What we're building, in one paragraph

You paste a link to a DJ set (SoundCloud, YouTube, Mixcloud). The tool downloads the audio, decodes it once, slices it into short overlapping windows, asks **several** recognition engines "what is this?", reads tracklist hints from comments/descriptions, and combines that evidence into a timeline of **track episodes** — each with separate **work / version / boundary** confidences, overlapping episodes allowed, boundaries expressed as **exactly what the evidence proves** (one-sided bounds) plus clearly-labelled predictions, and unknown stretches honestly marked. For each identified track it offers **where you can get it**, and a **local web page** lets you **click a track and jump to that moment**.

## Guiding principles

1. **Accuracy is the goal.**
2. **Never trust a single engine or a single window.** Agreement — discounted for dependence — is what counts. Every transform hypothesis of one window is one logical trial.
3. **Identity, version and boundary are three questions** with three confidences and three tiers. "Verified" badge requires work *and* version verified.
4. **Episodes, not a list.** Overlaps, loops, repeats, layers and time-varying roles are first-class.
5. **A positive match proves two one-sided facts only:** the track had started by the end of the fingerprinted span, and had not ended before its start — *conditional on the identity being right*. Everything else about boundaries is prediction and is labelled so.
6. **Claim only what the benchmark can certify**, per profile, dimension and tier, on real-mix test predictions with cluster-aware confidence. Otherwise: *provisional*, visibly.
7. **Absence of a match proves nothing.** Regions without matches are "no evidence", never "no track".
8. **Free by default; licensed engines when they earn it**, behind hard ceilings and explicit opt-in for uploading third-party audio.
9. **Local-first; hosting is a separate discovery effort.**

## Engines

| Engine | Capability | Cost | Strengths | Weaknesses | Role |
|---|---|---|---|---|---|
| **Shazam** (unofficial; pinned `shazamio` + `shazamio-core`; **our own injected HTTP client with library retries disabled — the job store is the only retry owner**) | `clip_recognizer`; `matches[]` with `offset`, `frequencyskew`, `timeskew` | Free | Largest released-music catalogue | ~±3% pitch/speed; ~20 req/min/IP; unofficial | Free-profile default |
| **Shazam + transform hypotheses** | same | Free × hypotheses | Recovers off-speed/pitch playback | One logical trial per parent window; more hypotheses = more false-match opportunity (measured) | Policy by benchmark |
| **AudD** enterprise | `file_scanner`: one synchronous POST, server-side 12 s chunks, offsets, score, `limit`/`skip`/`every` | $0.60–1.50/h (plan on $1.50); 300 free | Long-file native; offsets | No remote id; lost response = `outcome_unknown` | Shortlist in Stage 3; profile in 4d |
| **ACRCloud** File Scanning | `file_scanner`: upload → remote id → poll; offsets, score, custom bucket | Gated pricing (~$1.40/h anecdotal); trial | Reportedly best electronic coverage; own bucket | Entitlements; VC++ runtime | Shortlist in Stage 3; profile in 4d |
| **Panako** (self-hosted) | `local_index_query`; rate & pitch factor | Free; **JDK ≥ 11 (not installed)**; AGPL | Designed for scale changes vs a held reference pool | Only indexed audio | **Conditional:** minimum index/query/normalisation path built in Stage 3 *if* JDK enabled, so it participates in 4c ablations; otherwise reference-pool recognition is **excluded from v1** |
| **Text hints** | evidence connectors | Free | Sometimes complete tracklists | Sparse; imprecise; copied | Always-on, non-blocking |

`max_accuracy` runs **all** selected independent engines over the whole set with no suppression.

## Pipeline and iteration

```
 ingest ─► decode ─► windows(gen0) ─► recognise(gen0) ─► fuse(gen0) ─► rescan_plan(gen0)
                          ▲                                                  │
                          └──────── windows(gen1) ◄── orchestrator ◄─────────┘   … up to max_generations
 hints (job store, non-blocking) ─────────────────────────────────────────► fuse
 fuse(final) ─► enrich ─► present / exports
```

- **Generations.** The orchestrator owns iteration. `fuse` never writes windows or queries; it emits `fuse/rescan_plan.gen<N>.jsonl`. The orchestrator turns requests into `windows/windows.gen<N+1>.jsonl` and `recognise/queries.gen<N+1>.jsonl`; recognise appends `observations.gen<N+1>.jsonl`; fuse reads the **union of all generations** and writes `fuse/episodes.gen<N+1>.json`. Termination: no requests, `max_generations` (default 3), or budget exhausted. Each generation's sidecar lists the hashes of every input generation. `fuse/episodes.json` is a symlink-free copy of the final generation with `generation` recorded.
- Hint connectors are job-store jobs; none blocks the audio path.

## Artefact identity and determinism

- `source_key = sha256(canonical_url)`; `media_key = sha256(original bytes)`; work dir `work/<source_key>/<media_key>/`.
- **Stage-owned immutable artefacts:** `ingest/source.json`; `decode/pcm.json`; `windows/windows.gen<N>.jsonl`; `recognise/queries.gen<N>.jsonl`, `recognise/observations.gen<N>.jsonl`, `recognise/raw/<cache_key>.json` + `recognise/raw_index.json` (hash list); `provider_configs/<provider>-v<K>.json` (**immutable, versioned measured configuration** — measuring writes a new version, never edits); `hints/hints.jsonl`, `hints/connector_status.json`; `fuse/identities.gen<N>.json`, `fuse/episodes.gen<N>.json`, `fuse/rescan_plan.gen<N>.jsonl`; `enrich/acquire.json`; `present/…`. Nondeterministic data (run ids, timestamps, timings, counts, costs) lives only in `invocations.jsonl`.
- **Canonical JSON:** UTF-8, sorted keys, no insignificant whitespace, **no floats anywhere in artefacts**: rates as `rate_e4` (integer, 1.08 → 10800), semitones as signed integer `semitones`, sample-map coefficients as integer rationals.
- **Ids:** `sha1(media_key ‖ record_type ‖ natural_key)`; natural keys are spelled out per record.
- **Ordering:** records with `start_ms` by `(start_ms, id)`; others by `id`. **Sidecars** `X.done.json` = `{sha256, upstream: {path: sha256}, schema_version}`. **Atomic writes.** **Record-level provenance** via `source_ids`. **No secrets** in artefacts; logger redacts `client_id`, `oauth_token`, `api_key`, `Authorization`, `Cookie`.

## Data contracts (Stage 0: schema + golden + semantic vectors for every record)

### `ingest/source.json` (key: `canonical_url`)
`source_key`, `media_key`, `input_url`, `canonical_url`, `platform`, `platform_id`, `uploader_id`, `uploader_name`, `title`, `upload_date`, `original {path, sha256, container, codec, bitrate, ytdlp_format_id}`, `metadata {description, chapters, comment_count}`, `config_snapshot`.

### `decode/pcm.json`
`media_key`, `pcm {path, sha256, sample_rate 16000, channels 1, sample_format "s16le", duration_ms, ffprobe_duration_ms}`, `decoder {ffmpeg_version, filtergraph}`.

### `window` (key: `generation ‖ start_ms ‖ support_end_ms ‖ transform.type ‖ transform.rate_e4 ‖ transform.semitones`)
`id`, `generation`, `start_ms`, `support_ms [s0, s1]` (span of **original** audio whose samples are in the fingerprinted output), `output_ms` (12000 unless short-input), `transform {type: none|resample|tempo|pitch, rate_e4, semitones}`, `sample_map {a_num, a_den, b_samples, uncertainty_ms}` (**output sample k ↔ original sample `(a_num/a_den)·k + b_samples`**, all integers), `wav_path`, `wav_sha256`, `logical_trial_id` (= id of the `none` sibling in the same generation), `reason: schedule|tail|rescan`, `rescan_request_id` (nullable).

### `query` (key: `provider ‖ capability ‖ target ‖ provider_config_version ‖ scan_policy`)
`id`, `generation`, `provider`, `capability`, `target {window_id} | {asset: "original"|"pcm", asset_sha256}`, `provider_config_version` (= filename of the immutable provider config), `scan_policy`, `cache_key`. Unique on `cache_key` per media key.

### `jobs.sqlite`
```
jobs(id PK, media_key, query_id UNIQUE, provider, state, lease_owner, lease_expires_at, heartbeat_at,
     attempts, physical_attempts, next_retry_at, submission_started_at, submitted_at, remote_ref,
     reserved_units, reserved_usd, actual_units, actual_usd, result_path, error, created_at, updated_at)
state ∈ {pending, leased, submission_started, submitted, succeeded, no_match, retryable_failure,
         permanent_failure, outcome_unknown, cancelled}
budgets(media_key, provider, max_requests, max_usd, reserved_requests, reserved_usd, used_requests, used_usd)
connector_jobs(id PK, media_key, connector, target_url, cursor, page, page_cap, item_cap, items_fetched,
               state, lease_owner, lease_expires_at, heartbeat_at, attempts, next_retry_at,
               result_path, truncated, error, created_at, updated_at)
```
Submission protocol: `leased → submission_started` committed **before** any network I/O; `submitted` when the provider acknowledges; on recovery, `submission_started` without `submitted_at` → `outcome_unknown`, never auto-resubmitted; manual `retry --acknowledge-billing`. ACRCloud uploads are named by `cache_key` and the adapter lists the container before re-upload; AudD has no reconciliation. **`physical_attempts` counts every network attempt** (the injected HTTP client reports each one) and is charged against ceilings. Failure-injection tests at five points must each yield exactly one submission. Single process, single asyncio loop, one **writer task** owning SQLite (WAL, `busy_timeout=5000`) fed by a queue; heartbeat task every 15 s; startup reclaims leases older than 2×lease. Timeouts: Shazam 10/30/60 s, ≤ 5 retries (2 s·2ⁿ, cap 60 s, `Retry-After` honoured), breaker 5/60 s; scanners 30 min upload, poll 30 s → 5 min, ≤ 48 h.

### `observation` (key: `query_id ‖ mix_span ‖ raw_label_hash ‖ native_index`)
`id`, `generation`, `query_id`, `provider`, `capability`, `status match|no_match|error`, `is_final`, `mix_span_ms [a, b]` (original timebase via `sample_map` or scanner chunk), `support_ms`, `transform` (nullable for scanners), `logical_trial_id` (clip: parent window id; scanner: `sha1(provider ‖ chunk_index)`), `raw_label {artist, title, album, label, release_date}`, `provider_ids {…}`, `native {…}` (all native tuples; Shazam full `matches[]`), `anchor {mix_anchor_ms, ref_anchor_ms, uncertainty_ms, reliable, method, bias_applied_ms}` (nullable), `score_raw` (nullable), `quality` (nullable), `raw_response_ref`, `source_ids`.

Cache keys: clip `sha256(wav_sha256 ‖ provider ‖ provider_config_version)`; file scan `sha256(asset_kind ‖ asset_sha256 ‖ provider ‖ provider_config_version ‖ scan_policy)`; local index `sha256(wav_sha256 ‖ index_id ‖ index_version)`. Positive max-age 180 d; `no_match` 30 d; errors never; `--refresh`.

### `hint` (key: `connector ‖ source_record_id`)
`id`, `connector`, `kind tracklist_line|answer|correction|question|pointer|keyword`, `raw_text`, `artist`, `title`, `version_qualifier`, `label`, `flags {unreleased, id_unknown, mashup_with, edit, bootleg}`, `position_range_ms` (clipped to media), `position_kind cue_hms|cue_minute|comment_timestamp|chapter|section|none`, `author {pseudo_id, is_uploader, is_verified, follower_count, like_count}`, `is_pinned`, `parse_confidence`, `identity_specificity`, `temporal_precision_ms`, `relations [{type replies_to|corrects|copies, hint_id, confidence}]`, `provenance_group`, `mirror_of`, `mirror_status verified|quarantined`, `truncated`.

Parsing: timestamps by component count — two components = `MM:SS` with **unbounded minutes**, three = `H:MM:SS` with **unbounded hours**; only `ss ≤ 59` (and `mm ≤ 59` when three components) are enforced, plus ≤ media duration. Dotted `1.02` only after `at|around|@|track`. Spaced separators in priority `' - ', ' – ', ' — ', ' ~ ', ' : ', ' | '`. **No-space hyphens** are split only by **block-level delimiter consistency**: if ≥ 3 lines in a block share the `X-Y` form and none has a spaced separator, split on the first hyphen for that block with `parse_confidence 0.5`; otherwise unsplit, `parse_confidence ≤ 0.3`, work-level only. A *question* needs a track keyword and (`?` or `id`), with negative fixtures. Corrections scale by relation confidence; compatible time = overlap or within 60 s. Cue ranges: minute-only `[m·60000, (m+1)·60000]`; `MM:SS` ±5000 initial; SoundCloud question timestamp `[ts−120000, ts+15000]` initial; all replaced by learned per-connector/kind likelihoods. **Pre-calibration hint policy (Stage 4a):** hints (a) propose candidates and rescans; (b) a `tracklist_line` from uploader/pinned-tracklist/MixesDB/1001TL with position overlap counts as **one** independent-trial-equivalent vote for the work tier; (c) never raise the version tier; (d) questions only queue rescans. Hint features are evaluated on held-out `dev-2` sets, not the six tuning sets.

Connectors and caps: SoundCloud api-v2 comments (≤ 5,000), YouTube via yt-dlp (top 200 threads × 20 replies), MixesDB by URL (1 page), Mixcloud GraphQL (optional), 1001TL search (discovery) + page fetch only for pointed/manual `1001.tl/<id>` (optional, breaker), pointer import (allowlist: https only; hosts exactly `www.1001tracklists.com`, `1001.tl`, `www.mixesdb.com`, `boilerroom.tv`, `www.boilerroom.tv`, `blrrm.tv`; ≤ 3 redirects with host re-validation; 2 MB / 20 s), manual tracklist import. **Primary flows:** SoundCloud → resolve → MixesDB by URL → comments → mirrors; YouTube → info.json description/chapters → top comments → pointers → MixesDB; **Mixcloud → GraphQL sections → description → MixesDB by URL → mirrors.** Search-discovered mirrors are `quarantined` until platform-ID/uploader/date agreement + duration within 2% + ≥ 2 hints agreeing on the timeline, or manual confirmation.

### `identities` (keys: node `ns:value`; assertion `a ‖ b ‖ relation ‖ source.record_id`; candidate `sorted member node ids`; work `normalised artist|title`)
`nodes [{id, ns ∈ {isrc, mb_recording, mb_work, mb_release, shazam, deezer, apple, spotify, acr, audd, beatport, soundcloud, text}, label}]`, `assertions [{id, a, b, relation same_recording|same_work|same_release|edit_of|sampled_from|mashup_of|component_of|conflicts, source {kind, record_id}, independent_of, confidence}]`, `works [{work_id, member_nodes}]`, `candidates [{canonical_id, work_id, member_nodes, alternatives, contested: bool, conflicts}]`.

**Merge rules.** Two separate component structures: *works* and *recordings*. Text equality (normalised `artist|title`) yields **`same_work` only**. `same_recording` unions require **recording-specific identifiers** (`isrc`, `mb_recording`, or a provider recording id) asserted by ≥ 2 independent sources, or an aligned held reference, or audited truth. Union-find is transitive by construction; the **conflict veto** refuses any union across a `conflicts` assertion; a conflict discovered after a union marks the component `contested` and forces version tier `unclear`. Version tier is `unclear` unless supported by recording-specific ids with corroboration, an aligned held reference, or audited evidence. Adversarial fixtures: original/remix/edit/radio/extended/instrumental/remaster with identical labels.

### `episodes` (episode key: `candidate_id ‖ occurrence_index ‖ first_support_start_ms`; gap key: `start_ms ‖ end_ms`)
Top-level: `generation`, `episodes`, `gaps`, `durations`, `certification {profile, per: [{dimension, tier, status certified|provisional, n_test_predictions, lower_bound_e4, test_version}]}`.

`episode` = `id`, `candidate_id`, `alternatives`, `claim performed|component_evidence` (a sampled/acapella/work-level detection is `component_evidence` unless recording-level evidence supports a performed recording), **proved bounds** `start_no_later_than_ms = min over final matches of support_ms[1]`, `end_no_earlier_than_ms = max over final matches of support_ms[0]` (**conditional on identity**), `evidence_support_ms [[s0, s1], …]` (union of final match supports), **censored sides** `start_no_earlier_than_ms`, `end_no_later_than_ms` (nullable; set **only** from audited ground truth or a source-to-mix alignment against a held reference that localises audible contribution — never from `L_min`, manual cues, or alignment extrapolation), **predictions** `start_pi`, `end_pi {lo, hi, coverage_target, method, calibrated}` (nullable until Stage 5; inputs include latency distributions, reset extrapolation, manual cues), `best_start_ms`, `best_end_ms` (= PI centre if calibrated, else `start_no_later_than_ms` / `end_no_earlier_than_ms`), `role_segments [{from_ms, to_ms, role incoming|dominant|outgoing|layer|component|uncertain}]`, `occurrence_index`, `overlaps`, `alignment_segments [{mix_from_ms, mix_to_ms, rate_e4, intercept_ms, residual_ms, n_obs}]`, `alignment_events [{at_ms, type jump|loop|reset|drift}]`, `has_global_alignment`, `scores {work, version, boundary}`, `score_kind heuristic|calibrated`, `tiers {work, version, boundary}`, `badge`, `evidence`, `flags`, `rescan_state`.

`gap` = `id`, `start_ms`, `end_ms`, `bounded_by`, `evidence {n_windows, n_no_match, n_error, n_unclear_candidates, n_hint_events, n_novelty_events}`, `reason no_evidence|unclear_only`, `truncated`, `best_unclear_candidate`.

`durations` (a **partition** of `media_duration_ms`; test vectors prove the parts sum exactly and overlap is counted once): `evidence_supported_ms` (union of `evidence_support_ms` over episodes with badge ≥ possible), `predicted_episode_ms` (union of calibrated PI hulls beyond evidence; 0 before calibration), `unresolved_boundary_ms` (from each censored side outward until the next episode's evidence or **120 s**, whichever first — these are **not** gaps), `unclear_ms` (evidence supports of unclear-only episodes), `no_evidence_ms` (scanned, no final match, outside the above — **gaps** are maximal such regions ≥ 45 s), `unscanned_ms`.

### `rescan_request` (key: `generation ‖ trigger ‖ start_ms ‖ end_ms ‖ policy`)
`id`, `generation`, `trigger gap|contested|edge|long_episode|novelty|hint_cluster|question_cluster`, `start_ms`, `end_ms`, `policy {window_ms, hop_ms, phase_ms, transforms}`, `priority`, `input_hashes`.

### `ground_truth` (per set; key `set_id`)
`set_id`, `source {url_ref, media_key, duration_ms, platform, uploader_ref, event_ref, date}` (refs resolve through a **local** `data/local/source_links.json`, never committed), `stratum`, `split`, `corpus_version`, `selection_basis`, `episodes [{work {artist, title}, version {qualifier, ids}, version_verified, verified_against audio|source_recording|authoritative_metadata, start_ms_range, end_ms_range, audible_rule, role_segments, overlaps_with, occurrence_index, in_reference_pool, annotator_ref, second_pass_ref, disagreement_resolution, note}]`, `regions [{start_ms, end_ms, type silence_or_speech|out_of_pool|unresolved}]`.

### `benchmark_report` (key `corpus_version ‖ profile ‖ config_hash`)
`corpus_version`, `profile`, `config_hash`, `sets [{set_id, stratum, split, metrics{…}}]`, `strata [{stratum, metrics{…}, ci{…}}]`, `overall`, `engines [{provider, oracle_coverage, pairwise_agreement, ablation_delta}]`, `cost {requests, physical_attempts, billable_seconds, usd_e2, wall_ms}`, `certification [{dimension, tier, n, errors, lower_bound_e4, n_sets, status}]`, `regression {baseline_report_ref, deltas, gates [{name, pass}]}`.

## Recognition design

### Shazam contract
Pinned libraries; **injected HTTP client** with library retries disabled; signature from the window WAV with `segment_duration_seconds = 12`; `support_ms` from `sample_map`. **Anchor aggregation:** keep all `matches[]`; cluster `offset` within 1,500 ms; largest cluster ≥ 50% → `ref_anchor_ms = median·1000 − bias_applied_ms`, `mix_anchor_ms = support_ms[0]`, `uncertainty_ms = max(dispersion, bias_uncertainty)`, `reliable = true`; else `anchor = null`. The relation between `offset` and the first fingerprinted sample is **measured** by insertion tests (held released track, positions ×5, query lengths 6/8/12 s, partial overlaps, each transform factor) and written to an immutable `provider_configs/shazam-v<K>.json` (`adapter_bias_ms`, `adapter_bias_uncertainty_ms`, `L_min_ms` latency quantiles); the adapter **applies** the bias. Until measured: `reliable = false`. Adapter tests use recorded responses via a **fake HTTP server** that also verifies `physical_attempts`.

### Window schedule
`hop ≤ window − L` gives coverage-completeness for contained events of length `L`. Default **12,000 ms window, 9,000 ms hop**, end-anchored tail, short-input rule. Benchmark compares coverage-complete vs sparse schedules (6/8/12 s × 5/9/15 s × phases) with cost reported separately.

### Transform hypotheses (algebra corrected in r5)
`r` = **hypothesised playback rate** of the original in the mix (`r = 1.08` ⇒ played 8% faster). The correction slows/stretches the slice by `1/r`; **output duration = input duration × r**. To output exactly 12,000 ms, the adapter slices an original span of **`round(12000 / r)` ms**.

| Type | DJ behaviour | ffmpeg (undo) | `support_ms` | `sample_map` (output k → original) |
|---|---|---|---|---|
| `resample` | Pitch fader, no key-lock | `asetrate=16000/r,aresample=16000` | `[start, start + 12000/r]` | exact linear: `a = 1/r` → `a_num = 10000`, `a_den = rate_e4`; `b = 0`; `uncertainty_ms = 0` |
| `tempo` | Key-lock on | `atempo=1/r` (chain within [0.5, 2]) | `[start, start + 12000/r]` | approx linear (WSOLA): same `a`; `uncertainty_ms = 100` |
| `pitch` | Key shift, tempo kept | `asetrate=16000/p,aresample=16000,atempo=p` with `p = 2^(semitones/12)` | `[start, start + 12000]` | identity: `a_num = a_den = 1`, `b = 0`; `uncertainty_ms = 100` |

Rates stored as `rate_e4`; pitch as signed `semitones` plus derived `rate_e4`. Known-insertion vectors verify **output duration, first and last mapped samples, anchor bias and fitted slope** for every factor — not merely recognition success. Grid `rate_e4 ∈ {9200, 9600, 10400, 10800}` × {resample, tempo}, `semitones ∈ {−2, −1, +1, +2}` initially; benchmark decides.

### Alignment (executable baseline)
1. **One point per logical trial:** among a trial's matching variants, keep the variant whose candidate agrees with the trial's majority candidate and has the smallest `|frequencyskew| + |timeskew|`; the rest are retained as `hypothesis_rejected` evidence. Providers that genuinely report simultaneous sources keep one point per source.
2. Points sorted by `mix_anchor_ms`; a segment starts with the hypothesis rate; accept the next point if `|residual| ≤ 1,500 ms`; refit (Theil–Sen) after ≥ 3 points; require `rate ∈ [0.84, 1.20]` and hypothesis agreement ±2%.
3. **Precedence on rejection:** (i) *continuation* is decided by reference consistency, not time — a consistent point after any gap ≤ 120 s continues the segment; (ii) `loop` if ref goes back > 2,000 ms into the segment's own ref range; (iii) `reset` if ref within 5,000 ms of 0; (iv) `jump` if rate consistent but intercept shifts; (v) `drift` if rate changes > 3% over ≥ 3 points; (vi) **replay** (new episode, `occurrence_index + 1`) only if ref is inconsistent with the current segment **and** mix gap > 30 s, or the same ref region recurs after > 30 s; (vii) `outlier` if inconsistent with both neighbours (dropped, recorded). Event tests measure **precision and recall** per event type on controlled cases.
4. `has_global_alignment` iff one segment spans ≥ 80% of evidence support.

### Boundary inference (r5, one-sided only)
- **Proved (conditional on identity):** `start_no_later_than_ms = min support_ms[1]`; `end_no_earlier_than_ms = max support_ms[0]` over final matches. Nothing else is proved by audio evidence.
- **Censored sides** are `null` unless audited truth or a held-reference alignment localises audible contribution.
- **Predictions** (Stage 5): `start_pi`/`end_pi` learned on the calibration split from (true boundary − proved bound), using latency quantiles, reset extrapolation, manual cues and hint positions as **features**; reported with achieved coverage, width and Winkler score.
- **Controlled audible rule:** per stem from the render's gains: momentary loudness (400 ms window, 100 ms hop) of the stem vs the mix; relative level ≥ −20 dB → on, ≤ −26 dB → off (hysteresis); 3-frame median smoothing; minimum run 2,000 ms; frames with mix < −50 LUFS excluded as silence; truth range = first/last "on" frame ± 100 ms.

### Episodes, roles, layers
Roles are time-varying `role_segments`: in an overlap the earlier-started episode is `outgoing` and the later `incoming`; **`layer`** is evidence-based — both candidates have ≥ 2 independent logical trials within the overlap; `dominant` elsewhere; `component` when `claim = component_evidence`. Composite relations `mashup_of`/`component_of` live in the identity graph. Benchmark reports confusion between performed recording and sampled/acapella component, and dominant vs secondary precision/recall.

### Provisional tiers (baseline, `score_kind: heuristic`)
`T` = independent logical trials with a final match; `S` = span of evidence support; `C` = competing candidate covering ≥ 50% of the same span. Work: `likely` if `T ≥ 4 ∧ S ≥ 40,000 ∧ ¬C ∧ has_global_alignment`; `possible` if `T ≥ 2 ∧ S ≥ 20,000 ∧ ¬C`; else `unclear`. Version: `unclear` unless recording-specific ids corroborated by ≥ 2 independent sources (then = work tier). Boundary: `possible` if `has_global_alignment`, else `unclear`. Never `verified` while provisional.

### Rescan triggers
Gaps; contested regions; episode edges; episodes > 12 min; spectral-novelty change points; hint clusters lacking evidence; question clusters. Policies use shorter windows (6–8 s) and shifted phases; budgets ordered by priority.

### Process safety (concrete)
Windows: `pywin32` — `win32job.CreateJobObject` with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`; child created via `win32process.CreateProcess(..., CREATE_SUSPENDED)`, `win32job.AssignProcessToJobObject`, then `win32process.ResumeThread` on the primary thread handle; stdout/stderr via pipes handed to asyncio. Non-Windows: `psutil` tree kill. Tests terminate at each subprocess/network boundary including yt-dlp-spawned ffmpeg.

## Benchmark

### Strata and populations
1 catalogue-covered real mixes (**inclusion rule: chosen from an a-priori list of DJs/genres/platforms fixed before any engine runs, including open-world mixes**); 2 reference-pool real mixes; 3 controlled transforms; 4 self-index. Certification populations: strata 1–2 only.

### Corpus construction (Stage 2b — named deliverable)
Quotas: `dev-1` ≥ 6 sets / 60 episodes; `dev-2` (hint held-out) ≥ 4 sets / 40; `calibration` ≥ 10 sets / 120; `test` ≥ 12 sets / 150 with blinded second pass and third-annotator disagreement resolution; controlled ≥ 100 boundaries and ≥ 30 cases per event type. Freeze manifest per `corpus_version`. Sets beyond `dev-1` are **optional** if the owner declines the effort — then all tiers stay provisional.

### Certification (pre-registered per `profile × dimension × tier`)
Population = **cumulative at-or-above-tier** predictions on real-mix test sets. Correctness events: work = work-equivalent; version = exact-equivalent; start = `best_start_ms` within 10 s of the truth start range; end likewise; boundary = start ∧ end. Gate = **all** of: Clopper–Pearson one-sided 95% lower bound ≥ target (necessary; ≥ 299/29/9 error-free), **cluster (by set) bootstrap** lower bound ≥ target, and ≥ 10 independent sets. Status stored per `(profile, dimension, tier)`.

### Association and scoring
Predictions are associated to truth by **identity equivalence + temporal compatibility of supporting observations** (any evidence support within the truth hull ± 30 s), one-to-one per occurrence in time order. Identification, occurrence counting, segment coverage, and start/end boundaries are scored **separately**; episode IoU is reported but never gates identity. Set-valued per-ms P/R (micro, macro-by-set). Unknown regions as before. Non-inferiority: paired one-sided cluster bootstrap, margin 1 pp absolute.

### Metrics
As r4, plus event precision/recall per type, performed-vs-component confusion, dominant/secondary layer P/R, `physical_attempts`.

## Build order (each capability owned by exactly one stage; every gate names `corpus_version`, command, denominators, paired bound, non-regression metrics, expected artefact)

| Stage | Builds | Acceptance |
|---|---|---|
| **0. Preflight & contracts** | `uv` project; `doctor`; schemas + goldens + semantic vectors for every record (transform algebra incl. `12000/r` spans and rational sample maps; one-sided bounds; durations partition; anchor aggregation; identity merge/veto; rescan request); `derive_fixtures.py`, `audit_fixtures.py` (repo-wide: fixtures, truth, reports); synthetic hint fixtures | doctor passes; goldens validate; vectors pass; derive + audit pass |
| **1. Plumbing** | `ingest`, `decode`, `windows` (gen0 schedule + tail, `none` transform only, `sample_map`), Shazam adapter (injected client, fake-server tests, anchor aggregation, insertion-test harness writing `provider_configs/shazam-v1.json`), job store + submission protocol + writer task + budgets, cache, Job-Object launcher, `observations.gen0.jsonl`, invocation journal, `analyse --raw` (matches listing only) | Injection tests: one submission each; `physical_attempts` == fake-server count; 1-hour set completes, survives Ctrl-C + crash, cache re-run; Windows path/media/process tests |
| **2a. Scorer & controlled slice** | Scorer with vectors; controlled-transform generator (audible rule) producing ≥ 20 boundaries; truth tooling (seed → verify → second pass) | Scorer vectors pass; controlled slice truth validates |
| **2b. Corpus dev-1 + baseline fuser** | `dev-1` assembled and frozen; identity graph (works/recordings, veto), alignment baseline, one-sided bounds, `durations` partition, gaps, provisional tiers, `episodes.gen0.json`, exports | Baseline report on `dev-1` committed (pseudonymous); partition vectors pass |
| **3. Adapters & shortlist** | AudD + ACRCloud adapters (recorded fixtures; opt-in upload flag; entitlement smoke); **Panako minimum path if JDK enabled**; per-engine baseline numbers on `dev-1` | Engines shortlisted with numbers; entitlements documented; reference-pool status decided (in v1 or excluded) |
| **4a. Hints** | Connector jobs, parser, relations, provenance, mirrors/quarantine, pointer/manual import, pre-calibration hint policy | Parser fixtures; on `dev-2`: +5 pp absolute duration-weighted evidence coverage, one-sided 95% cluster bound > 0, precision non-inferior at 1 pp |
| **4b. Transforms & schedule** | `resample`/`tempo`/`pitch` variants with insertion vectors; schedule/phase options; per-trial hypothesis selection | Insertion vectors pass; paired benchmark deltas choose grid/schedule with false-match rate reported |
| **4c. Rescans, scanners, events** | Generation loop; rescan policies; file-scanner fusion; event state machine; Panako in fusion if enabled; per-engine ablations | Controlled (≥ 100 boundaries): `best_start` p90 −20% relative paired; event recall ≥ 80% **and** precision ≥ 80% per type on ≥ 30 cases; no double submission |
| **4d. Profile freeze** | `free`, `max_accuracy` from 4c ablations | Documented with numbers |
| **5. Calibration & test** | Calibrated scores/tiers per profile; PIs; single frozen-test evaluation; certification per pre-registration | Report with per-(dimension, tier) status and CIs |
| **6. Where to get it** | Lookups; SoundCloud flags; search links | Direct links only on exact-ID/strong agreement; audit ≥ 95% on ≥ 60 links |
| **7. Web page** | Player; timeline with evidence support, PI shading, unresolved zones, gaps; badges; seek to `best_start_ms − lead_in` | Seek within 1 s of target |
| **8. Panako full (conditional)** | Full provider; index from user-supplied/licensed files; discovery emits links | Stratum-2 recall +10 pp absolute on ≥ 20 episodes, paired |
| **9. Polish** | CUE flattening, config, docs | Daily-usable |

## Committed-corpus policy
Committed: synthetic fixtures, pseudonymous truth (no URLs/handles/platform IDs — refs resolve via local `data/local/source_links.json`), aggregate reports. Local only: raw dumps, raw provider/comment responses, source-link maps. `audit_fixtures.py` runs over **every** committed fixture, truth and report file. Uploading third-party mixes to AudD/ACRCloud requires `allow_third_party_upload = true` + CLI confirmation.

## Commercial release checklist (separate discovery stage)
Written provider pricing/licensing; disable unofficial Shazam, SoundCloud api-v2, **yt-dlp YouTube comment extraction**, Mixcloud GraphQL and 1001TL scraping until reviewed; ingestion terms for SoundCloud, YouTube and Mixcloud; provider upload/retention; UK personal-data handling; deletion; fair-use caps; Panako AGPL/patents; queues, tenancy, abuse, secrets, observability.

## Open questions for the owner
1. Fund a representative calibration/test corpus with held references and second-pass annotation, or ship v1 with provisional tiers (default)?
2. AudD/ACRCloud trial credentials + hard test budget before Stage 3?
3. JDK for a minimum Panako path before profile freeze, or exclude reference-pool recognition from v1?

## Decisions log
Python/uv; ensemble with measured dependence; three tiers, badge = work ∧ version (r2); **one-sided proved bounds only; L_min/cues/extrapolation are prediction features** (r3–r4); **transform algebra: input span `12000/r`, output→original `k/r`, rational sample maps** (r4); generations + rescan-plan artefact (r4); durations partition, censored tails ≠ gaps (r4); one point per logical trial; ref-consistency-first continuation (r4); certification per profile×dimension×tier with cluster gates (r4); association by identity + evidence compatibility (r4); text ⇒ `same_work` only (r4); time-varying roles, `claim` (r4); single retry owner with injected client (r4); Job-Object launcher named (r4); committed-corpus policy (r4); corpus construction a named deliverable (r4); Panako minimum path before freeze if enabled (r4).

## Risks
| Risk | Handling |
|---|---|
| Shazam change | Pinned; injected client; fake-server tests; paid fallback |
| Throttling | ≤ 18 req/min; cache-first; ceilings; `physical_attempts` counted |
| Paid-engine surprises | Entitlement tests; transactional budgets; submission protocol; opt-in uploads |
| Corpus too small | Provisional by default; controlled stratum for exact truth |
| Short overlays | Measured, not labelled |
| Wrong version | Version tier needs recording-specific corroboration; veto; contested flag |
| Windows processes | Job Objects with suspended spawn; writer task; explicit tests |
| Crowd errors | Provenance groups; corrections scaled; learned timing; held-out hint evaluation |

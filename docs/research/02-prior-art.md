# Research: existing DJ-mix track-ID tools (prior art)

*Compiled 2026-09-03.*

## 1. Open-source end-to-end "mix → tracklist" tools

### betmoar/tracklistify — most complete OSS implementation
- https://github.com/betmoar/tracklistify — Python 3.11+, MIT, 71 stars, created Nov 2024, **last push 2026-09-03**, v0.11.3.
- Backend: **Shazam via `shazamio>=0.8.1`**. An ACRCloud provider exists in code but its SDK dependency is commented out and the factory was broken until recently — effectively Shazam-only.
- Segmentation (`.env.example`): `SEGMENT_LENGTH=60`, `OVERLAP_DURATION=10`, `OVERLAP_STRATEGY=weighted|longest`, `MIN_SEGMENT_LENGTH=10`. **Caveat:** the Shazam provider passes the whole 60s file, but shazamio-core fingerprints only ~10s from the middle → it actually samples ~10s of every 50s.
- Confidence: `match_score = freq_score*0.6 + time_score*0.4` from Shazam's `frequencyskew`/`timeskew`, capped by `SHAZAM_SKEW_CAP`, scaled 0–100. `MIN_CONFIDENCE` was a no-op until v0.9.0.
- Dedup (v0.9.0): proximity window `2*(segment_length-overlap)` (100s default), identity anchored on first detection, collapses `feat.`/remix title variants. Removed `max_duplicates` because long tracks legitimately hit 4+ windows.
- **Infrastructure worth copying:** per-provider RPM limiter (`SHAZAM_MAX_RPM=25`, `MAX_CONCURRENT=1`, `COOLDOWN_SECONDS=2.25`), circuit breaker (5 failures / 60s reset), exponential backoff, identification cache keyed `provider:sha256(segment_bytes)` with 30-day TTL, `FFMPEG_SEGMENT_TIMEOUT`.
- Output: JSON, Markdown, M3U with `#EXTVLCOPT:start-time`. Downloads via yt-dlp. Enrichment via MusicBrainz ISRC → Spotify/Deezer/Tidal, and Beatport (BPM/key/label; "70–80% recall across four Tomorrowland sets").
- Issues: mostly yt-dlp staleness, Windows/asyncio problems, Python 3.13 `audioop` removal. Nothing on mix accuracy.

### skip5this/mix-id — simplest working Node CLI
- https://github.com/skip5this/mix-id — Node 18+, MIT, 22 stars, one-shot release Feb 2026, v1.0.6.
- Backend: npm `shazam-api@^0.3.0` (GPL-2.0, JS port of the reverse-engineered signature algorithm, 16kHz mono PCM).
- Segmentation: `--segment` 18s, `--step` 30s (≤1h) or 60s; serial; `RATE_LIMIT_MS = 3000`; backoff `10_000ms` base, 3 retries; detects rate-limiting by `<!doctype` HTML in response.
- Dedup: naive consecutive title+artist equality. Timestamp = segment start. **No confidence.**
- Output: TXT, CUE, JSON. README warns "Shazam sometimes bounces between two tracks during a mix."

### b1scoito/ripthatset — best clustering/confidence design
- https://github.com/b1scoito/ripthatset — Python 3.12, **GPL-3.0**, 4 stars, Jan 2025 (inactive).
- Backend: shazamio + acrcloud fallback.
- Segmentation: `--segment-length` 12000ms default (README: 8–10s catches more), parallel via `--cpu-count`, `--proxy` rotating IPs "HIGHLY RECOMMENDED".
- **Clustering worth copying:** `--min-matches` (2), `--max-gap` (3 segments), `--min-cluster` (2), `--min-confidence`; outputs MM:SS timestamp, **confidence 0–1**, segment list, match count; `--show-gaps`/`--min-gap-duration` reports unidentified stretches.

### Others
- **lukaflpvc/Pyzam** — Python, MIT, 71 stars, Jun 2025. `--mixtape` mode every `-d` seconds (≤12s). Documents hard Shazam limits: **max 12s sample, ~20 req/min before 429**. No dedup/confidence. [HN thread](https://news.ycombinator.com/item?id=40143255): pitch/tempo + effects defeat Shazam on minimal/microhouse; one commenter preferred Panako.
- **in0vik/Shazam-Tool** — Python, MIT, 22 stars, Dec 2025. 1-minute chunks → shazamio → text. No timestamps (open request). Issue #3 "General Poor Song Identification" on DJ sets.
- **chefkjd/MixSplitR** — Python desktop app, MIT, 86 stars, Apr 2026. Multi-source (MusicBrainz+AcoustID default, Shazam, ACRCloud optional). Positioned for vinyl/cassette rips. No confidence, split method undisclosed.
- **elvista/CrateDigger** — Python+Node, no license, Feb–May 2026. ACRCloud (required) + AudD (optional), Spotify export.
- **chrisport/SoundcloudToTrackID** — Go+Python, MIT, 2017–2018. ACRCloud. Historical only.
- **lonewsk/nts-tracklist-pirate** — shells out to SongRec per chunk. Pattern reference.

## 2. Fingerprinting backends — what each gives you

| Backend | Segment cap | Offsets returned | Confidence | Cost / limits | Notes |
|---|---|---|---|---|---|
| **Shazam (unofficial)** via shazamio / shazamio-core / SongRec / vibra / shazam-api | ~12s (≥15s returns empty) | `offset` into matched track, `timeskew`, `frequencyskew` | none native; derive from skews or vote clustering | free; ~20 req/min → 429 | Best catalogue for released music. shazamio high-level lib has unanswered breakage reports (Sep 2025–Feb 2026). **shazamio-core** (Rust, MIT, pushed 2026-09-02), **SongRec** (Rust, GPL-3, 1.9k stars, Aug 2026), **vibra** (C++, GPLv3, Python ctypes + WASM, Sep 2026) are the healthiest signature generators. |
| **ACRCloud** | 10–20s clips | `sample_begin/end_time_offset_ms`, `db_begin/end_time_offset_ms`, `play_offset_ms` | `score` 70–100 | not public; ~$2.50 per 1.8h reported | Official **File Scanning** tool ([acrcloud_scan_files_python3](https://github.com/acrcloud/acrcloud_scan_files_python3)) scans long files in 10s steps → CSV/JSON with offsets. Accepts YouTube URLs and 500MB uploads. Custom fingerprint bucket for own reference audio. |
| **AudD enterprise** ([docs](https://docs.audd.io/enterprise)) | server-side 12s chunks | `offset` in file, `timecode` in song, `start_offset/end_offset`; `accurate_offsets=true` | `score` | 300 free, then **$2/1,000 requests; 1 request = 12s** → 1h mix ≈ 300 req ≈ $0.60; `skip`/`every` to thin | Cheapest turnkey "long file → all tracks with offsets". Docs example is literally a DJ mix. |
| **AcoustID / Chromaprint** | full-file | n/a | n/a | free | **Not suitable** — designed for whole files ([FAQ](https://acoustid.org/faq)). |
| **Self-hosted landmark** — dejavu (dead 2020), audfprint (2019), **Panako** (Java, AGPL-3, 2022), **Olaf** (C, AGPL-3, pushed Jun 2026) | arbitrary | yes | match counts | free; you own the reference audio | Only Panako is pitch/tempo-robust. Olaf `--fragmented` chops long queries into 30s pieces. |

## 3. Research prior art
- **Sonnleitner, Arzt, Widmer, "Landmark-Based Audio Fingerprinting for DJ Mix Monitoring," ISMIR 2016** ([PDF](https://archives.ismir.net/ismir2016/paper/000187.pdf)). Compares Qfp, Panako, audfprint on real DJ mixes with 20s non-overlapping queries. Released the **mixotic dataset**: 10 CC mixes + 723 reference tracks with ground-truth boundaries ([dataset](https://www.cp.jku.at/datasets/fingerprinting/)). See 03-fingerprinting.md for the numbers.
- **mir-aidj** ([org](https://github.com/mir-aidj)): `djmix-analysis` does mix-to-track subsequence DTW alignment *given a known tracklist* — finds precise boundaries and cue points; `djmix-dataset` (mixes + tracklists, DAFx 2022); `transition-analysis` reverse-engineers fader/EQ curves. This is the missing "second stage" for precise boundaries.

## 4. Hosted services and what they reveal
- **trackid.net** — £20/yr; "users, AI and Shazam"; multi-engine + community correction.
- **setlist.id** — explicitly **ACRCloud**; segments matched individually; YouTube/SoundCloud/Mixcloud URLs; Tidal export.
- **set79.com** — SoundCloud-focused; **cross-references SoundCloud comments** with audio ID; "leaves gaps rather than guess"; 7–10 min per mix. ([blog](https://set79.com/blog/why-shazam-doesnt-work-on-dj-mixes))
- **TrackSniff** — confidence tiers **Unknown / Unclear / Possible / Likely / Verified**; 50 free/month, $9–19/mo. Reviewer found it confused similar piano-house records.
- **Beatport Track ID** (May 2026) — in-app only, built with seeqnc, marketed as pitch/time-stretch/overlap robust. No API.
- Brizm, songtools.ai, TracklistAI, djtracks.io, mixprism.eu, trackradar.ai — opaque "AI" marketing.

## 5. Synthesis

**State of the art (OSS):** fixed-stride sampling (12–60s windows every 30–60s) → reverse-engineered Shazam → naive consecutive dedup → timestamp = segment start. tracklistify has the only production plumbing but is Shazam-only and under-samples. Nobody publishes accuracy.

**Gaps nobody in OSS fills:**
1. **Precise boundaries** — everyone reports segment start, not track start. Shazam's `offset` (and ACRCloud/AudD offsets) give the real start for free: `track_start ≈ window_start − offset_in_track`.
2. **Multi-signal fusion with calibrated confidence** — no tool combines text hints + audio votes across overlapping windows.
3. **Evaluation** — mixotic and djmix-dataset exist; nobody benchmarks against them.
4. **Coverage vs rate limits** — short tracks need ~12s windows every ~15–20s, 3–4× the request volume of current tools.
5. **Pitch/tempo robustness** — only addressed in research (Panako/Qfp).
6. **Context signals** (comments, descriptions, MixesDB/1001TL) — used by set79, absent from OSS.

**Reuse:** yt-dlp + ffmpeg; shazamio-core or vibra for signatures; tracklistify's rate-limiter/circuit-breaker/cache and dedup-window derivation; ripthatset's `min-matches/max-gap/min-cluster` clustering; mir-aidj djmix-analysis for boundary refinement; mixotic + djmix-dataset for evaluation; CUE/M3U exporters.

**Build:** the orchestration and fusion layer — overlapping-window scheduler with per-backend budget, vote/cluster fusion into calibrated confidence, offset-based start-time estimation, explicit gap reporting, benchmark script. That combination doesn't exist publicly.

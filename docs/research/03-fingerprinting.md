# Research: fingerprinting engines vs. DJ-mixed audio

*Compiled 2026-09-03. Covers robustness to pitch/tempo change, EQ, effects, crossfades; catalogue coverage; cost; ToS.*

## 1. Shazam via unofficial APIs

**shazamio (Python)** — https://github.com/shazamio/ShazamIO, Python 3.10+, asyncio.
- Nothing raw is sent. Rust `shazamio-core` (PyPI wheels for Linux/macOS/**Windows**) downsamples to 16kHz, extracts spectral peaks in Shazam's 4 bands (250–520, 520–1450, 1450–3500, 3500–5500 Hz), builds the proprietary signature locally. `recognize()` POSTs `{signature: {uri: data:audio/vnd.shazam.sig;base64,…, samplems}, timestamp, timezone}` to `amp.shazam.com/discovery/v5/…` with spoofed iPhone headers and rotating User-Agent. Retries via `aiohttp-retry`.
- Sample length: default `segment_duration_seconds=10`, cut from the *middle* of the input. Server accepts at most ~12s. **Chunk the mix yourself; one call per chunk.**
- Rate limit: **~20 req/min per IP → HTTP 429**. ripthatset recommends rotating residential proxies. Issue #127: app recognises, shazamio doesn't — chunking at 10s with several offsets recovers some.
- Node equivalents: `node-shazam-api` (asivery, GPL-2.0, 16kHz mono s16le PCM), `node-shazam`, `shazam-api`, `unofficial-shazam`. Rust desktop: SongRec.
- **Pitch/tempo robustness:** Wang-style constellation hashes bin absolute frequency — time-stretch alone is somewhat tolerated, pitch change breaks hashes. CDJ pitch fader without key-lock changes both (worst case). audfprint (same hash family) "does not recover after pitch-shifts of more than three percent". **Practical: ±2–3% usually fine, ±4–8% (typical beatmatching) hit-or-miss.**
- Catalogue: Apple Music-distributed — most Beatport/Bandcamp label releases present; SoundCloud-only, dubplates, unreleased: absent.
- ToS: Apple Media Services T&Cs forbid automated access/reverse engineering. Consequences in practice = IP bans. Not a commercial foundation. Official ShazamKit is on-device SDK only, requires Apple Developer Program, no server API.

## 2. AudD
- 300 free requests, no card. PAYG $5/1,000; volume $450/100k, $800/200k, $1,800/500k per month.
- Standard endpoint: `POST https://api.audd.io/` with `url` or `file` ≤10MB → artist/title/album/label/release_date/`timecode` + optional Apple Music/Spotify/Deezer/MusicBrainz blocks.
- **Enterprise endpoint** (`enterprise.audd.io`) for "hours- or even days-long audio": server-side 12s chunks, per-chunk `timecode` (in song), `offset` (in file), `start_offset`/`end_offset`. **1 request per 12s** → ~300 req/hour of mix; from $2/1,000 → **~$0.60–1.50 per hour of mix**. `skip`/`every` to thin. Docs example is a DJ mix. Marketing claims neural fingerprinting tolerates "remixes, slowed+reverb, pitched versions" — no published numbers.
- Note: `recognizeWithOffset` is the humming endpoint, not a mix feature.
- SDKs: official `audd-python`, `@audd/sdk` (Node), all MIT, updated Jul 2026.
- Catalogue anecdotally weaker than ACRCloud on underground electronic.

## 3. ACRCloud
- 14-day free trial, no card; rate card behind console login. Data points: ~$0.0045/request (CN site); File Scanning a 1h47m set ≈ $2.50 (~$1.40/hour). Catalogue claim 150M+ — best commercial coverage of electronic; powers setlist.id, Scanamix, 45mixtrackr, hearthis.at.
- **File Scanning** (Console API): upload <500MB, or audio URL, or YouTube/TikTok/Vimeo URL. Returns timeline: `music[]` with `offset`, `played_duration`, `play_offset_ms`, `sample_begin/end_time_offset_ms`, score, ISRC, Spotify/Deezer/MusicBrainz IDs; plus `custom_files[]` if you attach **your own fingerprint bucket** — key for unreleased tracks: upload SoundCloud rips as references, get them back in the same timeline. Official CLI: [acrcloud_scan_files_python3](https://github.com/acrcloud/acrcloud_scan_files_python3).
- Robustness: 2016 blog claims pitch/time-shift recognition, no range given. Third-party ACRCloud services claim ±10% tempo tolerance.

## 4. AcoustID / Chromaprint — confirmed unsuitable
FAQ: "designed for identifying full audio files". Lalinský: "Partial matching is not supported by AcoustID and most likely never will be." Fingerprints cover first 120s; index matches whole files. Only useful for tagging a reference library with MusicBrainz metadata.

## 5. Open-source self-hosted systems

| System | Lang / storage | License | Maintained? | Own index + segment query | Pitch/tempo robustness |
|---|---|---|---|---|---|
| **Panako** (JorenSix) | Java 11+, LMDB, ffmpeg; Docker image | AGPL-3.0 | v2.1 May 2022; "activity bursts" | `panako store *.mp3`, `panako query seg.mp3`, `panako monitor mix.mp3` (25s windows, 5s overlap); reports query/ref start-stop, **time factor and frequency factor** | **Designed for it.** 2021 version: top-1 for 20s queries sped up 10% rose 18%→83%; README tests 93–107%. ~40× real-time single-thread |
| **Olaf** (JorenSix) | C (Zig build, WASM), LMDB | AGPL-3.0 | JOSS 2023, active | `olaf store`, `olaf query --fragmented` | Wang-style; **no** pitch/tempo claim; >2× faster than Panako. Use only with tempo pre-compensation |
| **audfprint** (dpwe) | Python, pickle | MIT | 2019 | `new/add/match`, `--find-time-range` | Fails >3% pitch shift; claimed wrong track ~50% on real mixes (ISMIR 2016) |
| **Dejavu** | Python, MySQL/Postgres | MIT | dead (2020) | returns offsets | None |
| **qfp** (mbortnyck) | Python, SQLite | — | unclear | yes | Python re-implementation of Sonnleitner/Widmer quads, aimed at DJ sets; unproven |
| **PeakNetFP** (ISMIR 2025) | TF 2.15, Faiss | research | yes | Top-1 >90% at time-stretch 50–200%; **no pitch-shift** augmentation |
| **neural-audio-fp** | TF, Faiss | MIT | stable | yes | noise/reverb only; no pretrained model |

**Panako is the only turnkey option that is both maintained and scale-invariant.** It also prints the detected speed factor — use as a consistency check across windows.

## 6. Research on DJ-mix identification
- **Sonnleitner & Widmer, quad-based fingerprinting** (DAFx 2014; TASLP 2016): quads of spectral peaks give translation/scale-invariant hashes; on 100k songs, >95% accuracy / 99% precision for queries modified up to ±30% in time and/or pitch, and recovers scale factors. Not open-sourced (qfp is a re-implementation).
- **Sonnleitner, Arzt, Widmer, ISMIR 2016** — the key paper. Disco set: 8 real club mixes, 7h16m, 296 refs. Mixotic set: 10 CC mixes, 11h23m, 723 refs (118 played). 20s non-overlapping queries. **Results:** "just between 25% and 74% of detectable seconds were assigned to the correct reference track"; "Audfprint and Panako claim a wrong track in around 50% of the cases where the correct track should be identifiable"; specificity audfprint ~50%, Panako ~75%, Qfp 94%. Qfp vs 430k-track Jamendo DB: accuracy 0.69 / precision 0.80 / specificity 0.71. Conclusion: "automated audio identification on DJ mixes is a challenging problem." Dataset: cp.jku.at/datasets/fingerprinting.
- **Panako 2.0** (ISMIR 2021 LBD) / **JOSS 2022**: DJ-set analysis cited as primary use case; "During a DJ-set speed changes are almost always present."
- **UnmixDB** (Schwarz & Fourer, ISMIR LBD 2018; [Zenodo 1422385](https://zenodo.org/records/1422385), CC BY-NC-ND): from 10 mixotic mixes, 12 variants each (4 effect × 3 time-scaling), with cue-region/tempo ground truth. Their CMMR 2019 follow-up estimates cue points and fade curves *given known tracks*.
- **Kim et al., ISMIR 2020** ([arXiv](https://arxiv.org/abs/2008.10267)): 1,557 mixes from 1001Tracklists; mix-to-track subsequence alignment recovers cue points/transition lengths — assumes tracklist. Code: mir-aidj/djmix-analysis.
- **DJ Mix Transcription with Multi-Pass NMF** ([arXiv 2410.04198](https://arxiv.org/abs/2410.04198), 2024): given source tracks, estimates gains via NMF.
- Field is moving to learned embeddings for extreme tempo change; pitch invariance still weak.

**What works, distilled:** scale-invariant geometric hashes beat Wang-style decisively; the hard part on real mixes is *specificity* (refusing false claims); use ~20s windows, require consistent reference-offset progression and stable speed factor across consecutive windows, expect ambiguous crossfade regions.

## 7. Unreleased / SoundCloud-only strategy
Technically feasible and cheap: yt-dlp pulls SoundCloud (128kbps MP3 / 64kbps Opus; Go+ = 30s previews), Bandcamp (128kbps previews), YouTube; `panako store` indexes thousands of tracks in minutes; query mix windows and vote. Or upload the same audio to an ACRCloud custom bucket.

Prior art: werthen/dj-mix-ground-truth-extractor (2018 thesis; Panako + known sources), slskdN (Soulseek client fusing MusicBrainz/AcoustID/SongRec/Panako/audfprint evidence). **No established open project builds a Panako index from a scraped SoundCloud candidate pool.**

Candidate generation: the DJ's + label-mates' SoundCloud uploads, label Bandcamp pages, 1001Tracklists "ID" hints. Never-uploaded dubplates stay "ID" (45mixtrackr: "near 0%").

Legal: SoundCloud ToU forbids ripping/scraping; Bandcamp AUP forbids scrapers. Mitigations for personal research: keep only fingerprints, delete audio, throttle, don't redistribute.

## Recommended layered pipeline
1. **Preprocess:** mono, loudness-normalise, 20s windows with 10s hop (Panako default 25s/5s overlap).
2. **Commercial first pass** — zero-budget: shazamio at ≤20 req/min (1h mix at 30s hop ≈ 120 calls ≈ 6 min); optionally submit tempo-compensated variants (−8/−4/0/+4/+8%) of each window. Paid upgrade: ACRCloud File Scanning or AudD enterprise.
3. **Self-hosted long tail:** Panako index of candidate pool; query windows the first pass left empty/low-confidence.
4. **Fusion:** accept a track only when ≥2–3 consecutive windows agree, reference offsets advance monotonically at a speed factor within ±10% and consistent across windows, segment ≥30s; suppress transition "bounces"; emit CUE/JSON with confidence and explicit "ID" gaps.
5. **Validate** thresholds on UnmixDB / mixotic before trusting them.

## Risk summary
- shazamio/Node clones — Apple ToS breach, ~20/min throttling, can break on endpoint changes.
- AudD/ACRCloud — licensed, but pricing gated and you're uploading possibly-infringing mix audio to a third party.
- Panako/Olaf — AGPL-3.0 (fine internally; copyleft if shipped as a service).
- Scraping SoundCloud/Bandcamp — ToS breach + copyright reproduction; tolerable only for private, non-redistributed research.

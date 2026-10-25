# Build report: song genre recognition (Genre page)

Worktree `agent-genre01`, branch `worktree-agent-genre01`, on top of `2bd2217`. Nothing committed.
No Shazam or AudD call was made. Ports used: 8797 (my own dev server, killed by PID) and 9333
(headless Edge's debugging port, killed by PID). Recognise/Shazam/fusion code, `playlists/**`,
`README.md`, `idea.cmd` untouched.

## 1. What it does

`/genre` (top bar "Genre", next to Playlists; plus a compact "What genre is a track?" box on the
home page). Paste a SoundCloud / YouTube / Spotify link or type "artist - title". The page shows
live steps (Finding the track -> Listening -> Looking up -> Done, plus a one-time "Downloading the
genre model" bar), then:

* the analysed track (cover, title, uploader, platform, length, "Open on ...", "Looked up as");
  "Not the right track? Pick another match" lists the other search results, one click re-runs;
* the answer: top style in big type with its share, parent genre chip, an agreement pill
  (Sources agree / Mostly agree / Sources disagree / Listening only), a one-line summary, the top 3
  styles as bars with where each came from (Heard / Discogs / Last.fm);
* tiles for **BPM**, **Key + Camelot** (with "Not certain - could also be X" when two key
  profiles disagree, and a note when another tempo scored nearly as well), and **Evidence**;
* "How this was worked out": the model's own top-8 ranking with raw probabilities, what Discogs and
  Last.fm returned (styles/tags, weight used), the fusion rule with the actual weights, where the
  15 s excerpts were taken, and timings;
* "Recent checks" (saved answers; a click is instant).

Look-ups are optional: with no keys the page says "Look-ups off ... add `DISCOGS_API_TOKEN` and
`LASTFM_API_KEY` to the .env file". `DISCOGS_TOKEN` is accepted as an alias.

### Code
* `src/id_detector/genre/frontend.py` - Essentia's MusiCNN mel frontend in numpy + patching.
* `src/id_detector/genre/model.py` - pinned download (SHA-256, atomic, one at a time), ONNX
  inference, patch averaging.
* `src/id_detector/genre/source.py` - input rules, SoundCloud+YouTube search via yt-dlp
  (`scsearch5`/`ytsearch5`, flat), candidate ranking, Spotify page metadata, stream resolve,
  parallel ffmpeg seeks (3 x 15 s at 30/50/70 % of the song) - all through `run_process`.
* `src/id_detector/genre/lookup.py` - Discogs search + Last.fm tags, parsers + clients.
* `src/id_detector/genre/vocab.py` + `resources/discogs400-styles.txt` - the 400 labels and the
  tag/synonym mapping (class names only; no weights are committed).
* `src/id_detector/genre/fuse.py` - the fusion rule.
* `src/id_detector/genre/tempo_key.py` - BPM and key.
* `src/id_detector/genre/service.py` - one check end to end, background threads (2 at a time),
  live steps, per-track + per-question JSON cache in `<work>/.genre/`, cover images saved there.
* `src/idea_web/genre_page.py`, `src/idea_web/genre.js`, `src/id_detector/present/genre_css.py`
  - page shell, static script (bundled into `app.<hash>.js`; DOM built with `textContent` only),
  styles (part of the one app stylesheet).
* `src/idea_web/application.py` - `GET /genre`, `POST /genre/check` (JSON, CSRF token, local mode
  only), `GET /genre/check/<id>`, `GET /genre/art/<id>`; `create_app(genre_service=...)` seam.
* `theme.py` top-bar link; `home.html` entry; `PAGE_VERSION` 28 -> 29 so saved result pages get
  the new top-bar link on the next start-up refresh.

Dependency added: `uv add onnxruntime` (1.31.0) - runs the official ONNX export of the model on
CPU on Windows; `essentia-tensorflow` has no Windows wheels. No librosa/TensorFlow needed.

## 2. Model, and proof the preprocessing is faithful

Model: MTG Essentia **discogs-effnet-bsdynamic-1.onnx** (official ONNX export, 18.0 MB, SHA-256
`a280825b...e43c`) + its metadata JSON, downloaded on first use into `models/genre/` (new
`.gitignore` entry `models/`; override with `IDEA_GENRE_MODEL_DIR`). **Licence CC BY-NC-SA 4.0 -
personal, non-commercial use is fine; the weights are never committed or redistributed.**

* **The ONNX `activations` output *is* the genre_discogs400 head.** I parsed the published head
  (`genre_discogs400-discogs-effnet-1.pb`, no TensorFlow: a small GraphDef reader) - it is one dense
  layer 1280->400 + sigmoid. Applying its weights to the ONNX `embeddings` reproduces the ONNX
  `activations` to **max |diff| 2.4e-7**. So one file gives the official 400-style answer.
* **Frontend ported line by line** from Essentia master source (FrameCutter centred frames,
  Hann symmetric unnormalised, |FFT| 512, MelBands 96 slaneyMel 0-8 kHz linear weighting unit_tri
  power, log10(1 + 10000 x), 128-frame patches every 62, tail discarded).
* **Bit-level check against Essentia's own C++** (essentia.js 0.1.3 = Essentia compiled to WASM,
  run in node on the identical 16 kHz samples):

```
vignesh.wav (Essentia's regression audio): frames 195/195, mel max |diff| 2.96e-04
t1 Double 99 - RIP Groove        frames 939/939 mel max|diff| 8.2e-05; activations max|diff| 1.4e-06; top-3 identical: True
t2 Sub Focus - Timewarp          frames 939/939 mel max|diff| 1.6e-04; activations max|diff| 3.3e-06; top-3 identical: True
t3 Skream - Midnight Request Line frames 939/939 mel max|diff| 1.8e-04; activations max|diff| 1.4e-06; top-3 identical: True
t4 Mr. Fingers - Can You Feel It frames 939/939 mel max|diff| 2.4e-04; activations max|diff| 1.6e-06; top-3 identical: True
t5 Stanton Warriors - Da Antidote frames 939/939 mel max|diff| 2.9e-04; activations max|diff| 5.7e-06; top-3 identical: True
```
  (Values range 0..5.5; the differences are float32 rounding.) Essentia's own published regression
  mean for vignesh differs from *both* my port and Essentia-WASM by up to 0.30 in a few bands - that
  comes from decoding/resampling the file differently (Essentia's MonoLoader resampler vs ffmpeg),
  not from the frontend. A regression test (`test_mel_frontend_matches_essentias_own_cpp_output`)
  pins my frontend to Essentia-WASM output for a deterministic signal (tolerance 1e-3).
* **Sanity set** (5 public tracks, 3 x 15 s each; model alone vs fused, live Discogs + Last.fm):

| Track | Expected | Model alone (top 3) | Fused answer |
|---|---|---|---|
| Double 99 - RIP Groove | Speed Garage | Speed Garage .55, UK Garage .41, Bassline .30 | **Speed Garage 51%** · UK Garage 33% · Bassline 8% (agree) |
| Sub Focus - Timewarp | Drum n Bass | Hardcore .25, Drum n Bass .21, Dubstep .16 | **Drum n Bass 55%** · Hardcore 15% · Dubstep 9% |
| Skream - Midnight Request Line | Dubstep | Grime .56, Dubstep .25, Grime (Hip Hop) .24 | **Dubstep 39%** · Grime 34% (mostly agree) |
| Mr. Fingers - Can You Feel It | Deep House | House .52, Deep House .50 | **Deep House 53%** · House 27% · Acid House 9% |
| Stanton Warriors - Da Antidote | Breaks | Bassline .50, Breakbeat .38, Breaks .27 | **Breakbeat 30%** · Breaks 24% · Bassline 21% |

  Model alone: right style #1 on 2/5 and in its top 3 on 5/5 (its misses are near neighbours:
  Grime for Dubstep, Hardcore for DnB, Bassline for Breaks). Fused: 5/5 right at #1.

## 3. Look-ups, mapping, fusion

* **Discogs** `database/search?type=release&artist=&track=` (fallback `q=artist title`), at most 2
  requests. Releases weighted `1/(1+0.5 rank)`, compilations /3, other artists skipped, a
  release's weight shared across its styles; quality from the artist match.
* **Last.fm** `track.getTopTags` (often empty, as the coordinator found), fallback
  `artist.getTopTags` at half weight. Tags < 10 dropped.
* **Mapping**: names normalised (case, accents, `&`/`n` -> and, punctuation); ~150 synonyms
  ("ukg"/"2step" -> UK Garage, "niche"/"bassline house" -> Bassline, "dnb"/"drum & bass" ->
  Drum n Bass, "breaks" -> Breakbeat + Breaks, ...); junk ("seen live", decades, places, moods) and
  umbrellas ("electronic", "dance") ignored for styles (umbrellas only vote for the parent).
* **Rule** (shown on the page):
  `fused = (heard + wD x discogs + wL x lastfm) / (1 + wD + wL)`, heard = model probabilities
  squared and normalised to 100 %; `wD = 0.7 x Discogs match quality`, `wL = 0.5 x Last.fm quality`
  (track 1, artist 0.5); broad styles a look-up gives (House, Techno, Breakbeat, Electro, ...) count
  half, because Discogs often files a specific record under the umbrella only. Agreement adds up;
  disagreement keeps both sides visible with their sources. The model's ranking is shown unchanged.
* Spotify: the public track page's meta tags give artist, title, length, cover (no key); a
  playable copy is then searched (length-aware ranking).
* Track choice: shared words, song-like length (30 s previews and > 15 min mixes penalised),
  mix/live/cover words penalised, YouTube "- Topic" uploads preferred. DRM/private uploads fall
  through to the next candidate (up to 4 tries) - common on label SoundCloud uploads.

## 4. BPM and key

* **BPM**: onset envelope (positive log-spectral flux, 100 fps) -> zero-padded periodicity
  spectrum -> comb over 60-200 BPM using 4 multiples, broad prior at 125 BPM; heard styles with a
  distinctive band (DnB/Jungle 158-186, Dubstep/Grime 132-152, ...) pick the octave; above 160 with
  no such style the half tempo wins if half as strong; refined on a 0.01 BPM grid with 8 multiples.
  Synthetic clicks: 120, 124, 128, 128.4, 130, 132.5, 135, 138, 140, 145, 150, 170, 174 exact
  (+-0.3); 174 with a DnB hint -> 174, without -> 87 (intended); off-beat-hat 70 BPM -> 140
  (genuinely ambiguous). Real tracks vs Essentia's PercivalBpmEstimator (16 kHz):
  128.0 vs 128.2 · 173.7 vs (unstable 87-127) · 140.0 vs 70.1 (half) · 118.6 vs 119.05 · 130.0 vs
  130.4 (Percival is lag-quantised to about +-1 BPM; mine is consistent across all 3 excerpts).
* **Key**: a faithful port of Essentia's `KeyExtractor` (SpectralPeaks, SpectralWhitening, HPCP 12
  bins with 4 harmonics, pcp gate 0.2, Key correlation), profile `edma` (Faraldo 2016, EDM);
  `bgate` computed too and shown as "could also be" when it differs. Against Essentia-WASM
  `KeyExtractor(edma)` on the same excerpts: **whole-track answers identical on 5/5 (strengths
  .60/.60, .86/.84, .80/.80, .82/.84, .91/.90)**, per-excerpt 13/15 (the 2 differences are a
  relative major/minor and a fifth on single 15 s excerpts). Synthetic chord progressions: 24/24
  keys correct. Camelot table checked (A minor 8A, C major 8B, Ab minor 1A).

## 5. Timings (this laptop, Discogs + Last.fm on)

| Case | Time |
|---|---|
| Cold: model download (18 MB) + first check | **12.8 s** (download ~2 s overlaps "finding the track") |
| Warm: model on disk, new answer (fresh process) | **12.0 s** (find 6.5 s = yt-dlp search 2.9 s + resolve 3.6 s; ffmpeg 3 excerpts ~1 s; inference 0.25 s; look-ups 2-3 s in parallel; key in parallel; BPM 0.2 s) |
| In the running server, new answer | 8-10 s |
| Same question again / saved answer | instant (0.0 s, no network) |

## 6. Screenshots (`docs/screenshots/genre/`, 1.8 MB, viewed and polished)

`result-1280-dark`, `result-1280-light`, `result-390-dark`, `result-390-light`, `why-1280-light`
(details open), `mrfingers-1280-dark`, `download-1280-dark` (one-time model download bar),
`running-390-dark`, `failed-1280-light`, `empty-1280-dark`, `empty-390-light`, `home-1280-dark`,
`home-390-light`. Rendered from my server on 127.0.0.1:8797 by headless Edge driven over CDP
(390 px with mobile emulation). Polish done from looking at them: real cover art (YouTube's listed
"maxres" thumbnail often 404s -> use mqdefault), Evidence tile, chevron on the picker, integer BPM
in Recent, search icon hidden on phones, failure message not repeated.

axe-core 4.10.2 (vendored) on every rendered state: **0 violations** in all 12 checked shots
(dark + light, 1280 + 390: result, why, failed, empty, download, home); no horizontal scroll at
390 px. Keyboard: the form, picker buttons, details summaries and links are native controls with
the shared 2 px focus ring; focus moves to the answer heading when it arrives.

## 7. Tests added (all offline, fakes only)

* `tests/test_genre.py` (77): mel frontend vs Essentia C++ reference fixture
  (`tests/fixtures/genre/essentia_musicnn_reference.json`), zero input, frame count, filterbank,
  patching, patch averaging; model download (checksum, progress, corrupt download refused, pinned
  and git-ignored); vocabulary and 13 tag mappings + junk; Discogs/Last.fm parsers and clients over
  `httpx.MockTransport` (artist-tag fallback, token alias, keys off, 429 reported); fusion (heard
  only, agreement raises, disagreement shows both, broad styles half, empty look-ups neutral);
  BPM (6 tempos, octave rule, silence), key (5 progressions), Camelot; input classification and
  refusals, title cleaning, ranking, excerpt placement, Spotify page parsing; whole service with a
  fake yt-dlp/ffmpeg runner + fake model: search check, cache hit with zero calls, DRM fall-through,
  pasted link with fused look-ups, Spotify link, picking another match, nothing found, cover hosts.
* `tests/idea_web/test_genre_page.py` (14): page + strict CSP (no inline script), look-up notes,
  top bar/home entry, CSRF required, start + poll + pick, bad input, cover route (and no traversal),
  script bundled + `node --check` + no `innerHTML`.

No existing test was edited.

## 8. Gate outputs (foreground, `IDEA_TEST_MODE=1`, empty `AUDD_API_TOKEN`)

Each shard was run in foreground pieces under the tool's 10-minute limit.

```
Shard A
$ uv run pytest -q tests/idea_web/test_[a-l]*.py           -> 301 passed, 1 warning in 301.82s
$ uv run pytest -q tests/idea_web/test_[m-z]*.py           -> 110 passed, 1 warning in 114.53s
$ uv run pytest -q tests/test_[a-f]*.py                    -> 314 passed, 1 warning in 454.79s
$ uv run pytest -q tests/test_[g-o]*.py tests/test_phase*.py -> 584 passed, 1 warning in 374.52s   (includes tests/test_genre.py)
Shard B
$ uv run pytest -q tests/test_paid_clip.py tests/test_playlists.py tests/test_progress_wallclock.py tests/test_projection.py tests/test_refusion.py -> 95 passed, 1 warning in 257.02s
$ uv run pytest -q tests/test_scan.py tests/test_scan_targeting.py tests/test_score_corpus.py tests/test_semantics.py tests/test_service_api.py tests/test_stage10_webapp.py tests/test_stage1*.py -> 201 passed, 1 deselected, 1 warning in 187.01s
$ uv run pytest -q tests/test_stage2*.py tests/test_stage3*.py tests/test_stage4*.py tests/test_stage5*.py tests/test_stage6*.py -> 326 passed, 91 deselected, 1 warning in 32.67s
$ uv run pytest -q tests/test_stage7*.py tests/test_stage8*.py tests/test_stage9*.py tests/test_truth*.py -> 381 passed, 2 skipped, 5 deselected, 1 warning in 96.15s
```
Total **2312 passed, 2 skipped, 0 failed**. On the way, `test_auth.py::test_local_assets_and_start_up_imports_are_untouched_by_accounts`
failed once because I had appended the genre CSS outside `legacy._APP_CSS`; I moved it into
`_APP_CSS` (assertion untouched) and the whole `idea_web` a-l shard then passed. None of the three
known load-sensitive tests failed.

```
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
466 files already formatted
$ uv run python scripts/audit_fixtures.py
audited 603 files
fixture audit passed
$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
$ git diff --stat
 .env.example                      |  2 +
 .gitignore                        |  3 ++
 pyproject.toml                    |  1 +
 src/id_detector/present/page.py   |  2 +-
 src/id_detector/present/server.py |  2 +
 src/id_detector/present/theme.py  |  5 +-
 src/idea_web/application.py       | 97 ++++++++++++++++++++++++++++++++++++++-
 src/idea_web/pages.py             |  2 +
 src/idea_web/templates/home.html  |  1 +
 uv.lock                           | 43 +++++++++++++++++
$ git status --short
 M .env.example
 M .gitignore
 M pyproject.toml
 M src/id_detector/present/page.py
 M src/id_detector/present/server.py
 M src/id_detector/present/theme.py
 M src/idea_web/application.py
 M src/idea_web/pages.py
 M src/idea_web/templates/home.html
 M uv.lock
?? docs/reviews/build-genre.md
?? docs/screenshots/genre/
?? src/id_detector/genre/
?? src/id_detector/present/genre_css.py
?? src/id_detector/resources/discogs400-styles.txt
?? src/idea_web/genre.js
?? src/idea_web/genre_page.py
?? tests/fixtures/genre/
?? tests/idea_web/test_genre_page.py
?? tests/test_genre.py
```
(`models/genre/` holds the downloaded weights and is git-ignored.)

## 9. Notes for review

* Network use for verification: model files (once), 5 public tracks x 3 excerpts (plus re-runs of
  the same tracks), a handful of Discogs/Last.fm look-ups with the owner's keys (never printed),
  one Spotify page, a few searches. Nothing in tests touches the network.
* The genre checker runs inside the web server process (quick, background threads, two at once);
  it is local-mode only - a hosted server has no `/genre` routes.
* Saved answers carry `RESULT_VERSION` (now 2); bump it whenever the method changes so stale answers
  are redone (this caught answers saved before the key port).
* Known limits: the Discogs taxonomy has no "Bass House"/"Future Garage" styles (mapped to the
  nearest); look-up quality depends on how the upload is titled; with look-ups off, the model alone
  is right at #1 about 2 times in 5 on the sanity set but has the right style in its top 3 each time.
* Not checked in a real (non-headless) browser.

BUILD: COMPLETE

# Stage 7 — "Web page"

*docs/PLAN.md rev 5.2. Delivers the Stage 7 build-order row: player; timeline with evidence
support, PI shading, unresolved zones, gaps; badges + version status + roles + acquire links; seek
to `best_start_ms − lead_in`; local read-only server; rescan queue; CUE/JSON exports.*

## What was built (file map)

`src/id_detector/present/` is now a package (was a single `present.py`):

- `present/exports.py` — the pre-existing flattened `tracklist.{json,md}` exports (unchanged API:
  `export_tracklist`, `flatten_tracklist`, `ExportResult`) **plus** `render_cue` and CUE writing.
  `export_tracklist` now also writes `present/tracklist.cue` and accepts an optional `title`.
- `present/page.py` — the Stage 7 generator. `render_page(...)` returns one self-contained HTML
  string (inline CSS/JS; the only external resources are the chosen platform's own player
  script/iframe). `generate_page(...)` atomically writes `present/index.html` + a `.done.json`
  sidecar. Also exports the shared seek arithmetic `seek_target_ms` / `seek_argument` and
  `plan_embed`.
- `present/server.py` — the loopback-only read-only server and the rescan queue:
  `make_server` / `serve_in_background` (both refuse any non-loopback host), the `GET` file server
  scoped strictly to `work/**/present/`, the curated index page, the `POST /rescan` handler, and the
  pure queue functions `build_rescan_request`, `append_rescan_request`, `read_rescan_queue`,
  `consume_rescan_queue`.
- `present/__init__.py` — re-exports the public surface so `from id_detector.present import
  export_tracklist, flatten_tracklist, …` keeps working for Stage 2b/6 callers.

CLI (`src/id_detector/cli.py`):
- `analyse` and `acquire` now call `generate_page(...)` after `export_tracklist(...)`, so every run
  writes `present/index.html`, `tracklist.{json,md,cue}`.
- new `id-detector serve [--port 8765] [--host 127.0.0.1] [--work-root work]` — read-only server,
  loopback only.
- new `id-detector rescan <url> [--max-generations 1] [--dry-run]` — consumes
  `present/rescan_queue.jsonl` and runs another analysis generation.

`src/id_detector/ingest.py` — additive: `config_snapshot` now carries `embeddable_by` from
info.json so the page can honour an embedding-disabled set.

Tests: `tests/test_stage7_page.py` (11), `tests/test_stage7_server.py` (6).

## The page

- **Player embed** chosen from `source.json.platform`: SoundCloud widget (`w.soundcloud.com/player`
  + `api.js`, `SC.Widget(iframe).seekTo(ms)`), YouTube IFrame API (`new YT.Player`, so `origin` is
  handled by the API; `player.seekTo(seconds, true)`), Mixcloud widget
  (`Mixcloud.PlayerWidget(iframe)…seek(seconds)`). One embed per page — never a catalogue.
- **Embedding terms**: `plan_embed` reads SoundCloud `embeddable_by` (`all|me|none`; only `all`
  embeds) and a generic `embed_disabled`, and falls back to a plain "Open the set" link.
- **Timeline**: per episode a light *extent* band `[best_start, best_end]`, solid *evidence*
  segments from `evidence_support_ms` (proved, conditional on identity), `start_pi`/`end_pi` shading
  when calibrated, hatched *unresolved-boundary* zones drawn outward from a proved bound to the next
  evidence or 120 s (only where the censored side is `null` and there is no PI), and gap bands with
  their window/no-match/error counts in the tooltip.
- **Tracklist**: badge (work tier, capped at `likely` unless version verified) + `version_status` +
  primary role + track label + hint marker + acquire chips (Free DL / Gate / Buy / direct / Search
  from `acquire.json`). Click (or Enter/Space) a row → `seekTo(best_start_ms − lead_in)`.
- **Lead-in control**: a numeric input (default 5000 ms) rebinds the seek live.
- **Rescan**: a "rescan" button per track edge and per ID gap POSTs to `/rescan`, which the local
  server appends to `present/rescan_queue.jsonl` (no provider calls).
- Light/dark via `prefers-color-scheme`; keyboard-accessible rows; works offline except the player.

## Privacy

The page renders no usernames and no comment text (the audit patterns are reused in the tests). The
only free text is the set **title** (the same field the server index shows). Hints contribute only a
boolean "hint" marker. Confirmed on the real dev-1 page: no at-handle match (CSS at-rules excluded),
no identifier-field match, and neither `uploader_id` nor `platform_id` values appear in the page.

## How to run it

```bash
# per run (generates present/index.html + tracklist.{json,md,cue})
uv run id-detector analyse "<mix-url>"
uv run id-detector acquire "<mix-url>"        # adds acquire links to the page

# browse analysed sets, read-only, loopback only
uv run id-detector serve --port 8765          # serves on 127.0.0.1:8765

# consume the page's queued "rescan here" requests and run another generation
uv run id-detector rescan "<mix-url>"
uv run id-detector rescan "<mix-url>" --dry-run   # list queued requests only
```

## What I verified and how

- `uv run pytest -q` → **527 passed, 3 deselected, 1 warning** (17 new Stage 7 tests, up from 510;
  the 3 deselected are the live-marked (`pytest.mark.live`) tests). All Stage 7 tests are
  deterministic and
  network-free; the server tests use only the loopback interface and tear the server down on a
  background thread with a join timeout, so no process is left hung.
- **Seek-correctness test passes.** `test_page_seek_lands_within_one_second_of_target`
  (soundcloud/youtube/mixcloud) + `test_seek_python_matches_documented_formula` all PASS. It is a
  JS-free Python test: it reads each row's `data-best-start-ms` and the embedded `leadInMs`/platform
  from the page, applies the **same** `seek_argument` the page's inline JS emits (the formula is
  shared, not duplicated — asserted by checking `Math.max(0, bestStartMs - leadInMs)` is literally
  in the page), reconstructs the realised millisecond target, and asserts it is within 1 s of
  `max(0, best_start_ms − lead_in)`. **This is UI arithmetic only and is separate from measured
  boundary error** — it proves the click-to-seek maths, not the accuracy of `best_start_ms`.
- HTML validity test: the page parses (tag-nesting validator, no crossing/stray tags), every
  episode id and gap id appears, and the audit `_HANDLE`/`_ID_FIELD` patterns find nothing.
- `uv run ruff check .` → **All checks passed!**
- `uv run ruff format --check .` → **153 files already formatted**
- `uv run python scripts/audit_fixtures.py` → **audited 333 files / fixture audit passed**

### Live check (real cached dev-1 set, network-free)

The cached dev-1 `work/…` SoundCloud set (399 windows / 399 observations) had no `fuse/` result
(the Stage 6 report noted its old `episodes.json` predated `rejected_evidence`). I re-fused it
offline from the cached observations (no network), then generated and served the page:

- re-fuse → **55 episodes, 0 gaps**, duration 3,587,506 ms. (The task's "41 rows" predates the
  rev-5.2 fuser changes — `T_ind`/selected-observation bounds — and is not reproducible from the
  current fuser; the deterministic test instead asserts the served page renders exactly
  `len(episodes)+len(gaps)` rows.)
- `generate_page` → `present/index.html`, SoundCloud embed present, **55 track rows / 0 gap rows**,
  55 timeline lanes, 87 solid evidence segments.
- `serve_in_background(work)` + `httpx.get(...)` → **HTTP 200**, served page has **55 rows**; the
  index page lists the set by title.
- Local URL (loopback, ephemeral port):
  `127.0.0.1:<port>/<source_key>/<media_key>/present/index.html`
- HTML outline: `h1` (title) → `section.player` (SoundCloud embed) → `div.controls` (lead-in) →
  `div.timeline` → `div.legend` → `table` (55 track rows).

## Deviations from plan

- **Module path.** The task named `id_detector/present/page.py`; `present` was a single module, so I
  promoted it to a package and moved the old code to `present/exports.py`, preserving the import
  surface. No behaviour change for existing callers.
- **"41 rows".** Not reproduced — see the live check above. The number changed with the rev-5.2
  fuser; the row-count check is now data-driven.
- **`rescan` seeding.** The orchestrator computes its own rescan plan from fusion and does not accept
  externally-seeded requests, so `id-detector rescan` consumes/archives the queue (durable record of
  the user's manual intent) and then re-runs the analysis generation, which re-emits rescans for the
  same gaps/edges. Consuming the queue is a pure, unit-tested function; the re-run itself needs the
  network to recognise new windows, so it is not exercised by an offline test.
- No change to `docs/PLAN.md`.

## Known gaps / for the next stage

- `rescan`'s re-analysis is network-bound for genuinely new windows (inherent). Feeding the queued
  regions straight into the orchestrator as seed requests would let `rescan` target exactly the
  clicked region; today it relies on the same region re-surfacing as a gap/edge.
- Timeline unresolved-boundary zones use the 120 s / next-evidence rule directly rather than reading
  the `durations.unresolved_boundary_ms` aggregate; the two agree in intent but are computed
  independently.
- The YouTube embed relies on the IFrame API creating the iframe (so `origin` is automatic); a
  future hardening could pin `enablejsapi=1&origin=<loopback>` explicitly.
- Stage 9 (polish) owns CUE flattening/config/docs; the CUE here already applies the plan's
  primary-role precedence via `flatten_tracklist` and emits ID gaps as `ID` tracks.

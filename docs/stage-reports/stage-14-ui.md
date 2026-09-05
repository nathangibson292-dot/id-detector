# Stage 14 — Web UI overhaul ("club mode")

*Owner brief: "make this the best-looking website possible for what's intended … have fun while
they're on it … gamify the progress … as sexy as humanly possible."  Built on top of Stage 10's
web app and the multi-mix library (commit `4638027`).  Pure presentation: no analysis artefact,
contract, provider call or CLI behaviour changed.*

## What changed (file map)

**Shared look — `src/id_detector/present/theme.py` (new):**
- One design system for every page: dark "club mode" tokens (near-black ground with soft
  violet/pink/cyan glows, one brand gradient), Segoe UI Variable / system display + text stacks,
  a tabular monospace stack for times, the sticky blurred top bar (animated equaliser brand mark,
  *Your mixes* / *+ New mix*), buttons, chips, status pills, toast, favicon (an inline data-URI
  SVG, so the static page still works offline).  Pure strings, no I/O, nothing user-derived.

**Result page — `src/id_detector/present/page.py`:**
- Hero: platform chip, length / profile / generation chips, the set title as a display heading,
  and four stat tiles — **tracks found**, **% of the set identified** (an animated ring: proved
  evidence time + calibrated predicted time over the set length — honest coverage, not a guess at
  the tracklist), a **confidence mix** bar (verified / likely / possible / unclear counts), and
  **ID gaps**.
- Timeline: taller, with every lane **coloured by its confidence tier** (so the strip reads as a
  confidence map), a time ruler underneath, a glowing pink playhead with a time pill, hover sync
  (hovering a tracklist row lights its lane), and the same semantics as before (extent, proved
  evidence, prediction interval, unresolved hatch, ID-gap hatch).  Lead-in is now edited in
  seconds (converted to ms in the page; `CONFIG.leadInMs` and the seek arithmetic are unchanged).
- Tracklist: mono time column with an animated equaliser on the playing row, confidence pills,
  *Artist — **Title*** with small tags only where they carry information (`layer`/`outgoing`
  roles, `verified`/`contested` versions, `hint`), the "▸ N other versions" disclosure restyled,
  acquire chips, a rescan button revealed on hover, ID-gap rows hatched in rose.  Rows animate in
  with a short stagger; `↑`/`↓` move between rows, `Enter`/`Space` seek (existing).
- **NOW pill** in the top bar while the mix plays (time + track; click scrolls to the row) and a
  `body.playing` state that animates the brand mark.
- **Export row**: *Copy tracklist* (plain text `time  artist — title`, `ID` for gaps, via the
  clipboard API with a textarea fallback) plus CUE / M3U / Markdown / JSON download links.
- **Short matches** (`min_track_ms`, wired from `[present] min_track_ms`, default 30 s): on-air
  duration cleanly separates real tracks from false positives (most false positives are a single
  12 s window), so a track whose proved on-air span is under the floor — unless it is
  likely/verified or hint-supported — is **dropped from the exports** and, on the page, **kept but
  hidden** behind a "N short matches (under 30 s) hidden · show" toggle, and left out of the
  stat tiles, the timeline and the playhead partition.  `exports.short_track()` is the single
  predicate; `on_air_ms` (evidence hull) is now on every flattened track entry.
- `<meta name="id-detector-page" content="N">` version stamp (`PAGE_VERSION = 3`).
- Everything the Stage 7/11/12 tests pin is intact: byte-identical seek + playhead JS, the
  `EPISODE_SPANS` partition, `.current` row/lane highlight, per-platform position hooks, the
  `closest('a,button,details,summary')` guard, valid nesting, no handles / identifier fields.

**Stale-page refresh — `src/id_detector/present/refresh.py` (new):**
- `page_version(index_html)`, `regenerate_page(media_dir)` (re-renders from `ingest/source.json`,
  `fuse/…`, `decode/pcm.json`, optional `enrich/acquire.json`; honours `id-detector.toml` lead-in
  / collapse) and `ensure_fresh_page(media_dir)`.  The server calls the latter before serving any
  `present/index.html`, so **already-analysed mixes get the new page the first time they are
  opened — no re-analysis**.  Best-effort: a missing artefact leaves the old page in place.  The
  two existing mixes under `work/` were regenerated this way.

**Server pages — `src/id_detector/present/server.py`:**
- **Home**: a hero *"Drop a mix. Get the tracklist."* with the drop-a-link form (platform
  auto-detect chip, an *Options* tray with a Free / Max-accuracy segmented control and two
  toggles), live **In progress** cards (each polls its job and animates a gradient bar; failed
  = red, cancelled = grey), a **Your mixes** strip of three big numbers (mixes analysed, tracks
  identified, music listened to — read from each `present/tracklist.json`), and a card grid
  (platform, length, title, track count, a mini confidence bar).  The read-only `--no-analyse`
  index shares the shell without the form.
- **New mix** (`/new`, also `/new?url=…` as the "try again" landing) — the same form plus a
  four-step "how it works".
- **Progress** (`/jobs/<id>`): a big gradient percentage; the phase name + message; window / ETA
  / elapsed / listening-speed tiles (the ETA and speed follow the *observed* windows-per-minute once
  a few windows are done, falling back to 18/min only at cold start); the **scanner strip** — one cell per query window (capped at 240,
  proportionally mapped), lighting up left-to-right in the brand gradient with a pop on each newly
  finished cell and a pulsing "next" cell; rotating, honest flavour lines per phase; a step
  tracker (Index? → Fetch → Decode → Slice → Listen → Hints → Stitch → Links? → Page) with
  done / active / pending states; the tab title shows the percentage.  On success: confetti, a
  "Tracklist ready — N track episodes found after listening to M windows" card and a big *Open the
  tracklist* button with a 5-second auto-open (and *stay here*).  On failure: the error, *Try
  again* (prefilled) and the log.  Cancel asks for confirmation and disappears once terminal;
  the log is collapsed under *Show the log*.
- `webapp/jobs.py`: a phase is now logged when it **completes with a new message** as well as
  when it starts (`ingest: <set title>`, `windows: 212 windows`, `fuse: 41 episodes`), which is
  what the progress page uses for the set title and the done card.  Status JSON shape unchanged.

## Verification

```
uv run pytest -q                       → 529 passed, 93 deselected (non-live)
uv run pytest tests/test_stage7_page.py tests/test_stage7_server.py tests/test_collapse.py \
              tests/test_stage10_webapp.py -q   → 59 passed (3 new: version stamp, stale-page
                                                  regeneration on open, phase-completion log)
node --check on every page's inline <script>  → home / new / job / results OK
uv run ruff check src/id_detector/present src/id_detector/webapp tests/… → clean
uv run python scripts/audit_fixtures.py       → fixture audit passed
```

Every page state was rendered headlessly (Edge) against the real "Gemfest Set 2026" analysis (71
display tracks) and a scripted job runner: home (desktop + narrow), new, running (42 %), queued,
done, failed, and the regenerated result page.

## Guarantees kept
- Loopback-only server, unchanged routes (`/`, `/new`, `/analyse`, `/jobs/<id>[/status|/cancel]`,
  `/rescan`, `/present/…`); `GET /new` additionally reads a `url` query for prefill.
- No usernames or comment text on any page (the privacy tests strip `<style>` and apply the
  fixture-audit handle / identifier patterns to home, new, job and result pages).
- The static result page still needs no external resource beyond the platform player.
- The CLI `analyse` path is unchanged; the progress hook contract (`phase, done, total, message`)
  is unchanged — the UI only consumes it.

## Notes
- The look is deliberately dark-only ("one committed look"); `prefers-reduced-motion` disables the
  animations.
- `color-mix()` is used for pill tints — fine in current Edge/Chrome/Firefox/Safari; an older
  browser just shows untinted pills.
- Coordination: this landed after `id-detector-f1`'s multi-mix library commit, and alongside
  `id-detector-71`'s recognition-speed work, which owns `recognise.py` / `shazam.py` / the
  top-level `jobs.py` / `cli.py` flags and has agreed to keep the progress-hook contract.

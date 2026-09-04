# Stage 11 — Live playhead on the results-page timeline

*Adds a moving vertical playhead to the Stage 7 result page's confidence timeline. As the embedded
mix plays (or is scrubbed), the playhead tracks the player position across the colour-coded
evidence/episode/PI/gap timeline, the current track row and its timeline lane light up, and the
timeline itself becomes click-to-seek. Everything stays in the one self-contained `127.0.0.1`-only
`present/index.html` — inline CSS/JS, only the platform's own player script/iframe external — and the
page still carries no usernames or comment text. The CLI `analyse` path is unchanged.*

## What was built (file map)

**Shared playhead arithmetic — `src/id_detector/present/page.py`:**
- `playhead_x(position_ms, duration_ms, width_px) -> float` is the single source of truth for the
  position→pixel mapping, alongside the existing `seek_target_ms`/`seek_argument`. The fraction
  `position_ms / duration_ms` is clamped to `[0, 1]` and scaled by the timeline's **measured** pixel
  width; a non-positive duration or width collapses to 0. `render_page` emits the byte-identical
  `playheadX` JavaScript in the new `_PLAYHEAD_JS` block (with a matching `formatTime` for the
  `m:ss` / `h:mm:ss` label), so the page's arithmetic is provably the same as the Python the test
  imports.

**Playhead element — `_timeline_html`:**
- An absolutely-positioned `<div class="playhead" id="playhead" hidden>` with a nested
  `<span id="playhead-time">` is appended inside the existing `.timeline`. It starts hidden
  (revealed on the first position event), sits above the lanes at `z-index:4`, and is
  `pointer-events:none` so clicks fall through to the timeline's click-to-seek handler.
- CSS: `.playhead` (2 px accent line + soft glow), `.playhead-time` (small tabular `m:ss` chip),
  `tr.track.current` (subtle row tint + inset accent rule), `.tl-lane.current .tl-extent` (accent
  ring on the current episode's extent bar), and `.timeline{cursor:pointer}`.

**Current-track partition — `render_page`:**
- Episodes are ordered by `best_start_ms` and each is given a span `{id, start, end}` where
  `end` is the next episode's start (the plan's `[best_start_ms, next episode start)` interval); the
  last episode owns the tail of the set. Emitted as `const EPISODE_SPANS = [...]`. `highlightCurrent`
  finds the span containing the live position and toggles `.current` on the matching `tr.track` row
  and `.tl-lane` (cleared when out of range).

**Player wiring (one shared script, gated at runtime by `CONFIG.embedKind`):**
- **SoundCloud** — reuses the single `SC.Widget(iframe)` instance; on `READY` reads `getDuration`,
  binds `PLAY_PROGRESS` (→ `e.currentPosition` ms) and `SEEK` to `updatePlayhead`.
- **YouTube** — the IFrame `onReady` reads `getDuration`; `onStateChange` starts a 250 ms
  `setInterval` that polls `getCurrentTime() * 1000` while `PLAYING` and stops it otherwise.
- **Mixcloud** — subscribes to `mcWidget.events.progress` (seconds → ms) when present; otherwise no
  playhead, the rest of the page still works.
- The whole binding block is wrapped in `try/catch`; when embedding is disabled (plain-link
  fallback) or an API is missing, the timeline still renders and nothing throws.

**Seek reuse — `seekPlayerArg` / `seekToMs` / `seekToPositionMs`:**
- The platform switch is factored into `seekPlayerArg(arg)`. Tracklist rows keep seeking with the
  live lead-in (`seekToMs = seekPlayerArg(seekArgument(bestStartMs, LEAD_IN_MS))`); a timeline click
  seeks exactly to the clicked position via `seekToPositionMs = seekPlayerArg(seekArgument(ms, 0))`
  — same shared arithmetic, zero lead-in. The timeline click uses `getBoundingClientRect` for the
  actual pixel width, and a `resize` listener re-runs `updatePlayhead(CURRENT_POSITION_MS)` so the
  playhead stays aligned after the window changes size.

**Tests — `tests/test_stage7_page.py`:**
- `test_playhead_x_matches_documented_formula` imports `playhead_x` and pins clamping at 0 and at
  the duration, past-the-end / before-the-start clamps, the midpoint (`400.0` of `800`), and the
  zero-duration / zero-width collapse — the JS-free, deterministic analogue of the seek test.
- `test_playhead_markup_and_hooks_present` (parametrised over the three platforms) asserts the
  hidden playhead element, its time label, `playheadX`/`updatePlayhead`, the `EPISODE_SPANS`
  partition (a span per episode), `highlightCurrent` + the `.current` row/lane CSS, all three
  per-platform position hooks, and the timeline click-to-seek reuse — all in the generated HTML.
- The existing HTML-validity, seek-correctness, embed-policy and privacy tests
  (`test_page_contains_no_usernames_or_comment_text`) still pass unchanged, so the new markup adds
  no usernames/comment text and the page stays well-formed.

## Verification

- `uv run pytest -q` → **511 passed, 93 deselected** (network suites deselected by default).
- `uv run ruff check .` → **All checks passed!**
- `uv run ruff format --check .` → **173 files already formatted**.
- `uv run python scripts/audit_fixtures.py` → **audited 346 files / fixture audit passed**.
- `uv run id-detector analyse "<gemfest set url>" --max-generations 0` (the cached
  `soundcloud.com/user-205223046/gemfest-set-2026` run) re-fused gen-0 from cache (0 network, 82
  episodes) and rewrote the gemfest `present/index.html` (93,914 -> 106,253 bytes). The regenerated
  page contains the playhead element, `playheadX`/`updatePlayhead`, `EPISODE_SPANS` (82 spans), the
  SoundCloud/YouTube/Mixcloud position hooks and `seekToPositionMs`, and a fresh handle /
  identifier-field scan of it finds nothing.

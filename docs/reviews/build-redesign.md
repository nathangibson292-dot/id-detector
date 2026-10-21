# Build report: ID'er UI redesign

Worktree `agent-ui01`, branch `worktree-agent-ui01`, on top of `596e24d`. Nothing committed.

## 1. Every screen the owner can reach (`idea serve` → `idea_web.application`, local mode)

| Route | Renderer | States |
|---|---|---|
| `/` (analyse enabled) | `templates/home.html` + `present/server.py` fragments | hero + paste form (Free / Deep, More options: links toggle, paste-a-tracklist box, inline form error re-render), start-up upkeep note (checking / finished with skipped-result details), "Recent analyses" (running, queued, waiting, failed, cancelled, Deep-offer awaiting approval; Dismiss), library (stats, search + Free/Deep filter, mix cards, failed/cancelled attempt cards with cost + Remove, empty state) |
| `/` (read-only) | `templates/index.html` | "Analysed sets" library, no form |
| `/new` | redirect to `/` (prefill) | — |
| `/jobs/<id>` | `templates/job.html` + `_scan_html` / `_outcome_html` | queued, running (by phase), overrun/stall, succeeded (confetti, Open the tracklist), succeeded + paid offer (Approve / Skip), paid notice, failed (cause, how far, cost, Try again), cancelled, waiting (free service paused); listen-while-it-works player (loading / playing / can't play) |
| `/<src>/<media>/present/index.html` (+ bundle URLs) | `present/page.py` (pre-rendered, offline-capable) | hero + run banner (degraded/partial), player (local audio, or SoundCloud/YouTube/Mixcloud embed, or link fallback), timeline, tracklist rows (track, short, crowd/from-comments, gap/ID, alternatives disclosure, comment-confirmed / confirmed twice / verified/contested tags, acquire links), exports (copy, CUE, Markdown, JSON), likes/playlists buttons, playback lead-in setting |
| `/playlists`, `/playlists/p/<id>` | `playlists/view.py` (not edited) | index, playlist, not found — inherits the new base system |
| truth review (`idea truth review`) | `id_detector/truth_review.py` | inherits the base system (system theme only) |

## 2. Design system (`src/id_detector/present/theme.py`)

* **Colour tokens** on `:root` — dark studio palette (`--bg #08080d`, `--card`, `--card2`, `--raise`, three line weights, `--fg/--muted/--dim`, brand pink/violet/cyan + gradient, confidence `--verified/--likely/--possible/--unclear/--gap`, state tokens `--hover/--sunken/--track/--glass/--accent-soft`). Matching light palette `LIGHT_CSS`, applied by `prefers-color-scheme` unless the owner chose dark, or by `data-theme="light"`. Every text token clears AA on every surface in both themes (`test_contrast_is_aa_in_both_themes_against_the_real_shipped_tokens` passes against the shipped values).
* **Theme toggle** in the top bar, remembered in `localStorage` (every access in try/catch), applied before first paint: inline in the head of the offline result page (inside its existing first script, so the page still has exactly three inline scripts), and as a tiny static `/static/theme.<hash>.js` in the head of live pages (their CSP still names no inline script except the job constants). Hidden where no script runs.
* **Scales**: space `--s1…--s9` (4 px base), radii `--r1…--r4`, shadows `--sh1…--sh3`, motion `--t1/--t2/--t3` = 150/200/250 ms on one easing; all motion off under `prefers-reduced-motion`.
* **Type**: local stack only (`Inter, "Segoe UI Variable", system-ui …`, mono `Cascadia Mono …`), no network fonts, no CDN; tabular numerals on every time/number. Visible 2 px focus rings everywhere.

## 3. What changed per screen

* **Top bar**: glass bar, ghost nav, theme toggle, gradient "+ New mix".
* **Home**: big two-line hero over a breathing brand-gradient equaliser (decorative, byte-stable), paste box as the focal card with focus glow and arrow button, Free/Deep as selectable cards with radio dots, refined options tray. Upkeep report is now one calm line with "Details" (all wording unchanged). Activity cards with flowing bars. Library: stats strip (mixes, tracks, hours listened), client-side search + All/Free/Deep filter (no requests), mix cards with a **mini timeline strip of the whole mix** (each track a block by confidence, gaps striped), Free/Deep badge, length, date, confidence bar; quiet Remove on hover; failed cards with red edge.
* **Progress page**: four-stage stepper (Fetch → Listen → Decide → Done) above a 76 px gradient percentage, phase and message, tiles (time left, live elapsed, step x of y), a thick animated bar, the listening map (cells now capped to what fits, so phones show it), the calm "Taking longer than usual" note, outcome card, player, and a "Steps and log" panel holding the detailed pipeline steps and log.
* **Result page**: hero + stat strip; a **deck** (sticky on desktop) with transport (previous / play-pause / next, position, NOW playing, Playback settings popover), the native audio hidden behind it, and the **signature timeline**: every track a block coloured by confidence with its title where it fits, gaps striped, crowd tracks dashed, evidence band underneath, comment markers, hover time line, rich tooltip (title, artist, confidence, span), playhead with time. Click anywhere seeks exactly there; the blocks are one tab stop (arrows/Home/End walk them, Enter plays that track from its start with the lead-in). Tracklist header with count, keyboard hints and export buttons; rows show title over artist, confidence chip, tags, alternatives; current row highlighted and gently auto-scrolled into view (only if off screen and the owner hasn't scrolled in 6 s); store links and Save buttons quiet until hover/focus (colour, not opacity, so AA holds). Keyboard: Space play/pause (not while typing), J/K (or ↑/↓ on a row) move, Enter plays. Phones: card rows, empty acquire cells dropped, 44 px targets.

## 4. The progress-bar fix (`src/id_detector/webapp/jobs.py`)

Cause: the bar is `elapsed / (elapsed + remaining)`. When a phase overruns, `remaining` stops shrinking but `elapsed` keeps growing, so the bar climbs with the clock (decode stalled 5 min: 1 % → 20 %); and an engine restart ("recognise 0 of 1") collapsed the remaining estimate so a stall flung the bar 51 % → 97 %.

Fix (wall-clock proportionality and the recent-rate ETA are unchanged):
* Time inside the current phase counts only up to that phase's **ceiling** (its expected, speed-scaled duration; 30 s for an unlisted phase), so an overrunning phase holds the bar there.
* Listening holds the bar (never lowers it) while its pass has heard no window of its own yet, or when nothing has been heard for longer than `max(45 s, 3 × the recent window gap)`. New `Job.advanced_at` (snapshotted, not carried into a retry) records the last real movement; a document without it is never held.
* `Job.overrun()` → `status_dict()["overrun"]`; the page shows "Taking longer than usual. Nothing is wrong — the bar is holding until this step finishes." only once the overrun is real (listening: stalled; other phases: 20 s or half the phase past its ceiling), and pauses the bar's motion.
* Measured on the old vs new code: decode stall 1 % → 20 % becomes 1 % → 2 %; restart stall 51 % → 97 % becomes 51 % → 51 %.

Tests added in `tests/test_progress_wallclock.py` (all existing assertions unchanged and passing):
`test_an_overrunning_phase_holds_the_bar_at_its_ceiling_and_says_so`, `test_a_listening_stall_on_the_cautious_prior_does_not_creep`, `test_a_steady_run_never_reports_an_overrun`.

## 5. Tests changed

* `tests/test_progress_wallclock.py` — three tests added (above); nothing loosened.
* `tests/test_refusion.py::test_the_upkeep_pass_re_fuses_stored_results_offline` — asserted the literal stamp `content="26"`, which was already stale at HEAD (PAGE_VERSION 27 since `596e24d`); now asserts `content="{PAGE_VERSION}"` (28). Markup-version only; the behaviour asserted (the re-fused bundle carries the current page) is unchanged.

No other test was edited. `PAGE_VERSION` 27 → 28. The golden Local Free file (`tests/golden/local-free/tracklist.json`) is data, not markup, and is unchanged.

## 6. Screenshots (`docs/screenshots/redesign/`, 2.9 MB)

`home`, `running`, `overrun`, `result` × {1280, 390} × {dark, light} (16), plus `done-1280-dark.png` and `failed-1280-light.png`. Rendered from the committed presentation fixtures plus a scratch hour-long showcase fixture in a temporary work root, served on 127.0.0.1:8797 with a fixed fake job queue (no provider, no network); headless Edge with `--blink-settings=preferredColorScheme`; phone shots through a same-origin 390 px iframe. The result shots inject one `updatePlayhead()` call so the playing state is visible. Server killed by PID; temp root deleted.

## 7. axe-core 4.10.2 (vendored), injected into the served pages

```
home dark: AXE 0 violations;        home light: AXE 0 violations;
running dark: AXE 0 violations;     running light: AXE 0 violations;
overrun dark: AXE 0 violations;     overrun light: AXE 0 violations;
done dark: AXE 0 violations;        done light: AXE 0 violations;
failed dark: AXE 0 violations;      failed light: AXE 0 violations;
result dark: AXE 0 violations;      result light: AXE 0 violations;
playlists dark: AXE 0 violations;   playlists light: AXE 0 violations;
```
(Two light-theme contrast hits found on the way — status pills and the dimmed store links — were fixed.)

## 8. Gates

Full suite, foreground, `IDEA_TEST_MODE=1`, empty `AUDD_API_TOKEN`. Each shard was run in foreground pieces under the tool's 10-minute limit:

```
Shard A
$ uv run pytest -q tests/idea_web                      -> 396 passed, 1 warning in 550.06s
$ uv run pytest -q tests/test_[a-f]*.py                -> 309 passed, 1 warning in 542.39s
$ uv run pytest -q tests/test_[g-o]*.py tests/test_phase*.py -> 506 passed, 1 warning in 448.20s
Shard B
$ uv run pytest -q tests/test_paid_clip.py tests/test_playlists.py tests/test_progress_wallclock.py tests/test_projection.py tests/test_refusion.py -> 93 passed, 1 warning in 515.21s
$ uv run pytest -q tests/test_scan.py tests/test_scan_targeting.py tests/test_score_corpus.py tests/test_semantics.py tests/test_service_api.py tests/test_stage10_webapp.py tests/test_stage1*.py -> 201 passed, 1 deselected, 1 warning in 327.44s
$ uv run pytest -q tests/test_stage2*.py tests/test_stage3*.py tests/test_stage4*.py tests/test_stage5*.py tests/test_stage6*.py -> 326 passed, 91 deselected, 1 warning in 63.18s
$ uv run pytest -q tests/test_stage7*.py tests/test_stage8*.py tests/test_stage9*.py tests/test_truth*.py -> 381 passed, 2 skipped, 5 deselected, 1 warning in 231.57s
```
Total 2212 passed, 2 skipped, 0 failed. None of the three known load-sensitive tests failed. (A baseline run of shard A taken while I was editing showed one failure in `tests/idea_web/test_auth.py::test_every_state_changing_route_needs_the_sessions_synchroniser_token`; it passed on both later runs.)

```
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
450 files already formatted
$ uv run python scripts/audit_fixtures.py
audited 587 files
fixture audit passed
$ uv run python scripts/check_page_js.py
page JavaScript check passed: 53 inline scripts across 22 page renders, plus the static app.js
$ node --check theme.js   (the static theme script, extracted)
ok
$ git diff --stat
 src/id_detector/present/page.py   | 976 ++++++++++++++++++++++++++++----------
 src/id_detector/present/server.py | 904 +++++++++++++++++++++++++----------
 src/id_detector/present/theme.py  | 315 +++++++++---
 src/id_detector/truth_review.py   |   3 +-
 src/id_detector/webapp/jobs.py    |  98 +++-
 src/idea_web/application.py       |   4 +
 src/idea_web/pages.py             |  26 +-
 src/idea_web/progress.py          |   1 +
 src/idea_web/templates/home.html  |   5 +-
 src/idea_web/templates/index.html |   2 +-
 src/idea_web/templates/job.html   |  21 +-
 tests/test_progress_wallclock.py  | 120 +++++
 tests/test_refusion.py            |   3 +-
 13 files changed, 1871 insertions(+), 607 deletions(-)
$ git status --short
 M src/id_detector/present/page.py
 M src/id_detector/present/server.py
 M src/id_detector/present/theme.py
 M src/id_detector/truth_review.py
 M src/id_detector/webapp/jobs.py
 M src/idea_web/application.py
 M src/idea_web/pages.py
 M src/idea_web/progress.py
 M src/idea_web/templates/home.html
 M src/idea_web/templates/index.html
 M src/idea_web/templates/job.html
 M tests/test_progress_wallclock.py
 M tests/test_refusion.py
?? docs/screenshots/redesign/
```
(plus this report, `docs/reviews/build-redesign.md`).

## 9. Notes for review

* `playlists/**`, `README.md`, `idea.cmd`, `work/`, `data/` and the deepscan folder untouched; no provider call, no network; ports 8765/8791/8792 never used.
* The playlists and truth-review pages have no theme toggle (they cannot load the theme script without editing their files or their server); they follow the system theme.
* The library search/filter runs on the live server pages only (the legacy read-only renderer ships no script, as its gate test requires).
* Not checked in a real browser with real audio: transport play/pause and auto-scroll were exercised in headless Edge with a simulated playhead only.

BUILD: COMPLETE

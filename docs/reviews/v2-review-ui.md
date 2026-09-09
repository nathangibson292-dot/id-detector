# IDea — brutal product / UX / front-end review

Reviewed 2026-09-09 against `main` @ `27c36fd`. Read-only: nothing under the repo was created,
modified or deleted (`git status` clean, all `work/**/present/index.html` page stamps unchanged).
All rendering was done from copies in the scratchpad.

**How this was tested**

- `uv run idea serve --port 8791 --no-open` — the real library + home + form (read-only browsing only;
  no result page was opened *through* the server, because `ensure_fresh_page` rewrites stale pages
  on GET and every stored page is stale — see F-14).
- A second instance on `:8792` driven by a scripted fake runner (the `tests/test_stage10_webapp.py`
  pattern) for the progress / failed / cancelled / queued states. No network, no new analyses.
- The result pages are **freshly re-rendered at the current `PAGE_VERSION = 17`** into the
  scratchpad (`regenerate_page` on copied artefacts), because every stored page on disk is v6–v16.
- Headless Edge screenshots at 1280 / 640 / 420 CSS px (narrow widths via an iframe wrapper, because
  headless Edge clamps its own window to ~500 px and produces a misleading crop otherwise).
- `node --check` on all four inline script blocks: **all parse clean, no JS syntax errors, no console
  errors captured.** The bugs below are design/data bugs, not script crashes.

---

## 1. Screenshot index

All under `docs/screenshots/v2-before/`

| # | File | What it shows |
|---|---|---|
| 01 | `01-home-1280.png` | Home at 1280: hero + URL box + Free/Max-accuracy segment + library of 10 mixes with confidence mini-bars |
| 02 | `02-home-options-open.png` | **Options panel open**: "Buy / download links" toggle + "Know part of the tracklist?" paste box |
| 03 | `03-home-narrow-420.png` | Home rendered at a clamped 420 px window (headless artefact — content clipped; kept as a caution, use 04/05) |
| 04 | `04-home-iframe-390.png` | Home at a true 390 px viewport (iframe) — layout does adapt to one column |
| 05 | `05-home-iframe-600.png` | Home at 600 px — form wraps, Analyse button drops below the field |
| 06 | `06-result-top-1280.png` | **Result page top**: stat tiles, native `<audio>`, export row, timeline + legend, first ~28 track rows |
| 07 | `07-result-shortmatches-shown.png` | Same page with **"show" hidden matches** on and all `<details class="alts">` expanded — the junk reveal |
| 08 | `08-result-boiler-top.png` | "DJ Three 60 min Boiler Room mix": **10 tracks found** here vs **55 tracks** on its library card; whole "Where to get it" column is `—` |
| 09 | `09-progress-running.png` | **Progress page** mid-recognise: 14%, 15/212 windows, ETA, scanner strip, 8-step tracker |
| 10 | `10-home-inprogress.png` | Home with the "In progress" list (raw URLs, raw phase key `recognise`, queued sorted above running) + empty-library state |
| 11 | `11-progress-failed.png` | **Failure state**: "That one didn't work" + raw `yt-dlp` / HTTP 403 error text |
| 12 | `12-progress-cancelled.png` | Cancelled state: "Stopped before it finished — nothing was saved." |
| 13 | `13-new-mix-page.png` | `/new` — a second, near-identical copy of the analyse form + "How it works" |
| 14 | `14-result-narrow-600.png` | Result page at 600 px — **"Where to get it" column has vanished** |
| 15 | `15-error-badurl-json.png` | **Error state**: submitting a URL without a scheme dumps the user on a raw JSON page |
| 16 | `16-result-crowd-gap.png` | Result page with `HINT` / `FROM COMMENTS` rows (comment text rendered as a track), all-`—` acquire column |
| 17 | `17-result-bottom-gaprow.png` | Bottom of a result page: the **ID gap row** with a raw `no_evidence` token, footer |
| 18 | `18-result-trackrows-closeup.png` | **Track rows close-up**: badges, `cross-checked`, `layer`, "N other versions", acquire chips |
| 19 | `19-result-narrow-390.png` | **Result page on a phone**: ruler labels collide into `0:005:0010:0015:00…`, titles wrap to 4 lines, no acquire column |

---

## 2. Findings, ranked

### CRITICAL

**F-1 · A bad URL dumps the user on a raw JSON page and eats their input.** *(home / `/new` form → `POST /analyse`)*
Screenshot 15. Type `soundcloud.com/holly-olivia/my-mix` (no scheme — the single most common paste
mistake) and the browser navigates to `{"error": "only an http(s) URL or a local file is accepted"}`
on a bare white/black page. No brand, no back link, and the pasted known-tracklist and every option
choice are gone. `server.py::_handle_analyse` returns `_send_json(400, …)` for a form POST as well as
a JSON POST.
*Why it matters:* this is the very first interaction. Someone who just paid, or is deciding whether
to, hits a developer error dump within ten seconds.
*Fix:* validate client-side first (normalise a missing scheme to `https://`, show an inline red
message under the field); server-side, re-render the form page with the error and every field
re-populated, and only return JSON when `Content-Type: application/json`.

**F-2 · The library card and the result page report different track counts for the same mix.** *(home ↔ result)*
Screenshots 01 + 08. "DJ Three 60 min Boiler Room mix" card says **55 tracks**; open it and the hero
says **10 tracks found** with "40 short matches (under 30 s) hidden". `_set_summary` counts every
`kind == "track"` entry in the stored `present/tracklist.json`, which was written by an older run
with a different `min_track_ms`; the page applies today's floor. Across the library the mini
confidence bar on that card is mostly grey (42 `unclear`) while the page shows 5 likely / 5 possible
and no unclear at all.
*Why it matters:* two numbers for the same thing on two screens is the fastest way to make a paid
tool look broken. The card also over-promises before the click and under-delivers after it.
*Fix:* one source of truth. Have the card read the same filtered projection the page uses (or
regenerate `tracklist.json` whenever `ensure_fresh_page` regenerates the HTML — see F-3).

**F-3 · The downloads don't match the page.** *(result page, Export row)*
`refresh.regenerate_page` rewrites **only** `present/index.html`. `tracklist.cue`, `.m3u`, `.md`,
`.json` are left at whatever the original analyse wrote. For the Boiler Room mix the page lists 10
tracks and the CUE/M3U/MD list 55, including `Cher — Believe` and `Gillette — Short Dick Man`. On top
of that, "Copy tracklist" copies only the *visible* DOM rows. **One result page therefore yields three
different tracklists** depending on which button you press.
Two more problems in the same row: the Markdown export ships columns the UI deliberately hides
(`| Badge | Version | Role |` → `POSSIBLE | UNVERIFIED | incoming`), and the M3U is functionally dead —
every entry is `#EXTINF:-1` pointing at the same SoundCloud *page* URL with a VLC-only
`#EXTVLCOPT:start-time`, so no normal player will play it.
*Fix:* regenerate exports alongside the page; make Copy, CUE, MD and JSON all render from the same
filtered entry list; drop M3U or make it point at the local audio file.

**F-4 · On a phone the product's payoff disappears.** *(result page, ≤720 px)*
Screenshots 14 + 19. `page.py::_CSS` `@media (max-width:720px){ th:nth-child(6), td.acquire{display:none} }`
hides the entire **"Where to get it"** column — the buy / free-download links, i.e. the thing a DJ
actually wants and the thing you'd charge for. Also on mobile: the ruler tick labels overlap into an
unreadable smear (`0:005:0010:0015:00…`), the NOW pill is hidden, "Your mixes" is hidden, and every
track title wraps to 3–4 lines because TIME + CONFIDENCE eat ~45 % of the width, so a 33-track list
becomes an enormous scroll.
*Fix:* on narrow widths switch the table to a stacked card layout — time + badge on one line, artist
— title on the next, acquire chips on a third; drop every second ruler label below 600 px.

**F-5 · There is no concept of a user, and the library is global.** *(everything)*
`_discover_sets(work_root)` scans the whole `work/` tree; every browser tab, every visitor, sees every
mix anyone has analysed, with no login, no ownership, no delete, no share link and no privacy
boundary. `POST /analyse` and `POST /rescan` have no auth, no CSRF token and no `Origin` check — even
today, any website the owner visits can silently `POST` a form to `http://127.0.0.1:8765/analyse` and
start a **Max accuracy** run that costs real money. Job state lives only in memory, so a restart
loses every in-flight job and the whole "In progress" list.
*Fix:* this is the hosted rebuild (section 4). Minimum before hosting: sessions, per-user mix
ownership, CSRF tokens on every POST, and a per-user job quota.

**F-6 · The result page streams the full downloaded mix from the server.** *(result page player)*
`_embed_html` prefers `<audio src="../ingest/original.m4a">` over the platform embed whenever the
fetched original exists, and `_send_file_range` serves it Range-capable. Locally that's a nice call.
Hosted, it means **your servers redistribute a full copy of someone else's copyrighted DJ set** to
anyone with the URL, plus ~100 MB of egress per page view.
*Fix:* hosted, fall back to the platform embed (SoundCloud / YouTube / Mixcloud widgets are already
implemented and the playhead bindings already exist); keep local audio only for the desktop/local mode.

**F-7 · The page hides more than it shows, then offers to show you the mess.** *(result page, "N matches … hidden · show")*
Screenshots 06 + 07. The Garage mix header reads **"56 matches (46 short, 10 suppressed) hidden · show"**
above 33 visible tracks. Click "show" and you get `Gillette — Short Dick Man · short · 12s`,
`Cher — Believe · short · 12s`, `TOM LECHEF — Run With · short · 12s`, all UNCLEAR, all with `—` for
where to get it. The word "suppressed" is internal vocabulary; so are the reveal labels
("buried under a surer track", "scattered detections").
*Why it matters:* you are advertising that the engine produced 89 guesses and threw away 63 % of them.
A first-time payer reads that as "this thing is mostly noise". The reveal has no upside for them.
*Fix:* delete the counter and the toggle from the default page. If it must stay, bury it in an
"Advanced / diagnostics" disclosure at the very bottom, phrased as "63 low-confidence fragments were
filtered out".

**F-8 · A whole confidence dimension is dead data.** *(result page, hidden columns + exports)*
Across all 10 analysed mixes (305 tracks) `version_status` is **`unverified` for 100 % of rows** —
`verified` and `contested` never occur once. The `VERIFIED` badge never occurs either (only
likely / possible / unclear). `primary_role` is `incoming` or `dominant` for 302 of 305 rows, and
neither ever renders a tag. Yet `.ver` and `.role` still ship as `display:none` cells: **89 dead
`<td class="ver">` + 91 dead `<td class="role">` per page**, plus two columns in the `<thead>` and two
columns in the Markdown export where they *are* visible as `UNVERIFIED | incoming`.
*Fix:* delete `version_status` and `primary_role` from the page and the Markdown export. Keep them in
the JSON export if the data model needs them. (`tests/test_stage7_page.py` pins some of these hooks —
they'll need updating.)

### HIGH

**F-9 · Progress is step-index arithmetic, not wall-clock.** *(progress page)*
Screenshot 09. `_JOB_JS::overall()`: `ingest = 2 %`, `decode = 4 %`, `windows = 6 %`, then recognise
maps linearly onto `8 → 90 %` by window count, then `90 → 99 %` for the tail. So the download of an
hour-long mix — which really can take a minute — sits at 2 %, then the bar jumps to 8 % and crawls;
and with a warm cache the recognise phase bursts through 80 points in seconds. This is the owner's
known complaint and the code confirms it exactly.
*Fix:* weight each phase by a rolling measured median duration (you already record `started_at`,
`recognise_started_at`, `finished_at` per job), and interpolate *within* the active phase by elapsed
time, not by unit count. Blend with the observed-rate ETA you already compute. Target: 10 % of the bar
≈ 10 % of the expected wall time.

**F-10 · "Where to get it" is empty on 8 of 10 mixes, and where it isn't, half the chips are searches dressed as stores.** *(result page)*
Screenshots 08 + 16: 8 of 10 mixes have no `acquire` data at all, so a ~25 %-wide column shows nothing
but `—` on every row. Where it is populated (screenshot 18), `apple · Buy` is a real Apple Music
product link, but `bandcamp` and `beatport` are `bandcamp.com/search?q=…` — **search pages**, styled
identically. Worse: for `MUCKANIKS — Baggidup` the acquire data already contains a real
`purchase_url` of `https://hardlinesounds.bandcamp.com/album/hardline13`, and `_acquire_links_html`
deliberately throws it away in favour of a Bandcamp *search*. `traxsource` (often the right store for
this music) is dropped by a hard `[:2]` cap.
*Fix:* only render a chip when it resolves to a product; label search fallbacks visibly
("search Bandcamp"); prefer a known `purchase_url` over a search; hide the column entirely when the
mix has no acquire data instead of drawing a column of dashes.

**F-11 · The `rescan` button is a dead control that tells web users to run a CLI command.** *(result page, every row)*
Every track row and every gap row carries a hover-revealed `rescan` button — 90 of them in one page.
Clicking it appends to `present/rescan_queue.jsonl` and toasts **"Rescan queued — run `idea rescan`"**.
Opened as a file (no server) it toasts "Rescan needs the local server (idea serve)". Per the project's
own findings, rescans produce zero real recall, add phantoms and cost hours, and are off by default.
*Fix:* delete the button, the `<td class="ops">` cell, `requestRescan`, and the `POST /rescan` route
from the web surface. (Keep `idea rescan` as a CLI escape hatch.)

**F-12 · The result page's footer states a privacy guarantee that is false.** *(result page vs home)*
Result page: `🔒 ran entirely on this machine — nothing leaves 127.0.0.1`.
Home / progress: `🔒 runs on your machine — no account, only short clips go to the recognizers`.
The second is true; the first is not — audio clips go to Shazam and, in Max accuracy, to a paid engine.
Two contradictory privacy claims two clicks apart.
*Fix:* one sentence, used everywhere, that is true in both local and hosted modes.

**F-13 · Comment text is rendered as a track, and it contradicts the stated privacy model.** *(result page, `FROM COMMENTS` rows)*
Screenshot 16, row at 2:40: **`I like the way you talk — Dave era`** with a `POSSIBLE` badge and a
`FROM COMMENTS` tag. That is a fragment of somebody's SoundCloud comment being presented as an
artist/title pair with a confidence rating. `page.py`'s own module docstring says "the page never
contains usernames or comment text".
*Fix:* require crowd IDs to parse into a plausible `Artist - Title` and to corroborate against the
catalogue before they are shown; otherwise drop them. Never give a crowd row a confidence badge —
give it a distinct "someone in the comments said" treatment with no badge.

**F-14 · Opening any old mix silently rewrites files on disk, and only half of them.** *(server, `GET …/present/index.html`)*
`_Handler.do_GET` calls `ensure_fresh_page` before serving, which re-renders and atomically rewrites
`index.html` when the stamp is stale. Every stored page in `work/` is stale (v6–v16 vs v17), so the
first open of each mix is a write-on-read. Exports are not refreshed (F-3), so the page and its
downloads drift apart at exactly that moment. Concurrent readers of the same mix would race.
*Fix:* regenerate the whole `present/` bundle in one atomic step, behind the existing lock, and do it
on a version-migration pass rather than inside a GET handler.

**F-15 · Failure copy contradicts itself, hides where it failed, and says nothing about money.** *(progress page, failed)*
Screenshot 11. "The analysis stopped with an error. **The log below has the details.**" — while the
details are already displayed *above* that sentence, and the log is a collapsed `Show the log` link
further down. The error text is raw: `ingest failed: HTTP 403 from the platform (bot challenge).
yt-dlp: Unable to download webpage` — it names a third-party downloader to a paying customer. The
8-step tracker renders every step greyed with **none marked failed** (`STEP_INDEX['failed']` is
`undefined`, so `setSteps` marks nothing), so the one thing the tracker exists for is missing exactly
when it matters. And for a Max-accuracy run there is no statement of whether anything was charged.
Same gap on Cancel: "**nothing was saved**" is false — the audio is on disk and any paid clips are
already spent.
*Fix:* map known failures to plain causes + remedies ("SoundCloud blocked the download. Try again in
a few minutes, or paste the YouTube version"); mark the failed step red; state credit impact
explicitly ("this run was not charged" / "12 of your 150 clips were used").

**F-16 · The badge vocabulary is seven overlapping systems with no on-page glossary.** *(result page rows)*
On one page a row can carry: a confidence pill (`LIKELY` / `POSSIBLE` / `UNCLEAR` / `ID`), a
gradient-filled `HINT` pill, a green `CROSS-CHECKED` pill, a dashed `FROM COMMENTS` pill, a
`layer` / `outgoing` / `uncertain` role tag, a `short · 12s` tag, and a `▸ 2 other versions`
disclosure. Nothing on the page defines any of them; `HINT` and `CROSS-CHECKED` explain themselves
only in a `title=` tooltip (invisible on touch and to keyboard users). The `HINT` pill is filled with
the brand gradient, making it the loudest element in the row while meaning the least.
*Fix:* one confidence word per row, plus at most one qualifier. Put a single "what these mean" line
under the Tracklist heading. De-emphasise `HINT` to a quiet grey tag or fold it into the confidence
calculation and stop showing it.

**F-17 · The timeline legend is statistics jargon, and the timeline itself is an unlabelled barcode.** *(result page)*
Six legend entries: *evidence (proved, coloured by confidence)*, *episode extent*, *prediction
interval*, *unresolved boundary*, *ID gap*, *from comments (no audio match)*. "Prediction interval",
"episode extent" and "unresolved boundary" are terms from the internal model, not from a DJ's
vocabulary; "episode" appears nowhere else in the UI. The strip carries no track labels at all, so
what a user actually sees is 33 coloured tick marks (screenshots 06, 16).
*Fix:* keep the strip, cut the legend to two lines ("colour = how sure we are", "striped = nothing
identified here"), delete the PI / extent / unresolved distinctions from the *legend* (keep the
drawing), and show the track name on hover as a label rather than only a `title` tooltip.

**F-18 · Home's "In progress" list speaks a different language from the progress page.** *(home)*
Screenshot 10. The activity card shows the **raw URL** (`https://soundcloud.com/holly-olivia/speed-…`)
where the progress page shows the resolved mix title, and shows the **raw phase key**
`recognise — recognising windows · 17 / 212 windows` where the progress page says "Listen". The
queued job is sorted *above* the running one. Failed jobs stay in the list forever with no dismiss.
*Fix:* reuse the same title extraction and the same step labels; sort running first; let a terminal
card be dismissed.

**F-19 · Contrast: `--dim` fails WCAG AA everywhere it's used.** *(all pages)*
`--dim: #5f5f78` on `--card: #13131b` measures **2.98 : 1** (and 3.18 : 1 on `--bg`), against a 4.5 : 1
requirement for body text and 3 : 1 for large text. It is used for: every table column header
(`th`), the footer, the timeline ruler labels, the generic `.tag` text, the `.exports` "EXPORT"
label, the `.eyebrow`, the "click a row to jump there" hint, and `.step small` on the progress page.
Also colour-only meaning: the library card's `conf-mini` bar carries its meaning purely in colour with
only a `title` tooltip, and the timeline is `role="img"` with the label "Evidence timeline" — which
describes nothing.
*Fix:* lift `--dim` to ≈ `#8b8ba6` (4.6 : 1) or stop using it for text; add a text key under the
library mini-bar; give the timeline a real `aria-label` summarising the tracks, or `aria-hidden` it
and rely on the table.

**F-20 · Table semantics are broken for assistive tech, and there are ~200 tab stops per page.** *(result page)*
Every `<tr class="track">` is `tabindex="0" role="button"` — which tells a screen reader the element
is a button, not a table row, destroying row/column association for the whole table. There are 89 such
rows in one page (33 visible), 90 `rescan` buttons, 170 acquire links and 11 `<details>` summaries.
No `<caption>`, no `scope="col"` on the headers.
*Fix:* leave the `<tr>` alone; put the seek affordance on a real `<button>` in the time cell
("play from 2:33"). Delete the rescan buttons (F-11). Add `scope="col"` and a caption.

### MEDIUM

**F-21 · Stat tiles measure things nobody asked for.** The "**ID gap**" tile reads `0` on 7 of 10 mixes and
`1` on the other 3 — a tile that is constant is not a statistic. The ring says "**62 % of the set
identified** / listening time backed by evidence", which is an internal coverage metric, and on the
Boiler Room mix it sits next to "**0 ID gaps**" — 33 % unidentified and zero gaps at the same time,
which reads as a contradiction. The confidence-mix tile has **no label at all**, just a bar and two
dot-legends (screenshot 06, third tile).
*Fix:* three tiles — "33 tracks", "1 h 00 m", "identified 62 % of the set" — and put the confidence
mix inline under the Tracklist heading with a caption, not in an unlabelled card.

**F-22 · `profile free` chip.** "Profile" is a config concept. Rename to "Free scan" / "Max accuracy",
or drop it once the hosted plan makes the mode obvious.

**F-23 · The player is an unstyled native `<audio>` element.** Screenshots 06/08/16: the single largest
interactive element on the page is Chrome's default grey audio bar sitting inside an otherwise
carefully designed dark card. It also gives no waveform, no track markers, and no relationship to the
timeline directly below it.
*Fix:* a custom transport (play/pause, scrubber, current time, prev/next track) drawn in the theme,
and merge it with the timeline into one component.

**F-24 · Library vanity metrics.** "9 h 03 m of music listened to" is a stat about the tool, not about
the user's work. "Mixes analysed" and "tracks identified" are fine. No mix shows **when** it was
analysed, and there is no search, sort, filter, rename, or delete anywhere in the library.

**F-25 · `▸ N other versions` on a third of rows.** 11 of 33 rows on the Garage mix carry an
alternatives disclosure, and expanding them (screenshot 07) mostly reveals the same track's other
pressings (`Hamdi — Skanka` / `Skanka (VIP)` / `Skanka (Kayzo Remix)`). Useful occasionally, noisy
always.
*Fix:* show it only when the alternatives are a genuinely different *track*, not a different pressing;
otherwise fold silently.

**F-26 · The "Lead-in … s before each track" number spinner is in prime real estate.** It sits in the
Timeline header on every result page. It is a power-user calibration knob and most users will never
touch it or understand it.
*Fix:* move into a small settings popover, or drop and hard-code 5 s.

**F-27 · "Max accuracy" makes a money claim with no mechanics.** "a paid engine leads and confirms the
tracks (about $2 a mix)" — it doesn't say which engine, whether a key is required, what happens if you
haven't got one, or what you get if it fails. `build_index` and `upload_consent` are still accepted by
`POST /analyse` but have no UI at all — dead server-side parameters.
*Fix:* in the hosted product this becomes "uses 1 credit" and the mechanics disappear; before then,
say what happens with no key. Delete the unreachable POST parameters.

**F-28 · The "More options" summary only reports one of the options.** `_FORM_JS::summary()` builds its
label from the `acquire` checkbox alone, so it always says "download links on / off" — a pasted
tracklist (the most consequential option in there) is never reflected in the collapsed summary.

**F-29 · Transport: HTTP/1.0, no gzip, no cache headers.** `curl -D-` on the home page and a result page
returns only `Content-Type`, `Content-Length`, `X-Content-Type-Options`. The result page is **178 KB
of uncompressed HTML** (19.9 KB inline CSS + ~16 KB inline JS re-sent on every single page load; the
CSS and JS are byte-identical across pages and could be one cached file). ~55 KB gzipped would be
typical. The `Server:` header also leaks `Python/3.12.14`.

**F-30 · The success screen auto-navigates after 5 seconds.** `countdown()` sets
`window.location.href = url` after 5 s unless you find and click the small "stay here" link. If you
were reading the log, you get yanked away.
*Fix:* make the redirect opt-in, or extend it to ~15 s and make "stay here" a proper button.

**F-31 · Machine tokens leak into the UI.** The gap row shows a tag reading `no_evidence` (screenshot 17).
The hidden-match reveal shows `buried under a surer track` / `contradicted by comments` /
`scattered detections`. The progress step "Stitch — build track **episodes**" uses a word that appears
nowhere else in the product.

**F-32 · The tracklist stops before the mix does.** On the 48:49 "Interplanetary Criminal" set the last
row is at 45:24 and there is no gap row for the remaining 3½ minutes (screenshot 17) — the set just
ends silently. The "0 ID gaps" tile agrees with nothing.

**F-33 · A failed analysis leaves no trace in the library.** `work/87cc7de7…/` (BENWAL, DGTL Amsterdam)
has ingest artefacts but no `present/index.html`, so `_discover_sets` skips it and the mix simply
does not exist as far as the UI is concerned. A user who analysed it and closed the tab has no record
that they ever tried.

**F-34 · Two identical analyse forms.** `/` and `/new` render `_form_html()` verbatim; `/new` adds a
"How it works" strip. The top-bar "+ New mix" on the home page navigates away from a page that
already has the form.
*Fix:* one form. Make "+ New mix" on the home page focus the field; keep `/new` only as the
"try again" landing.

**F-35 · Copy inconsistencies.** "The set is analy**z**ed track by track" on `/new` next to the
British "Analyse" button and "analysed sets" elsewhere. The read-only home says "Analyse a set from
the command line and it will appear here" — a CLI instruction in a web UI.

### LOW

- **F-36** Confetti fires on every success, including a run that found 10 tracks with 40 hidden.
- **F-37** The progress, failed and cancelled pages leave two-thirds of a 1080p viewport empty
  (screenshots 09, 11, 12).
- **F-38** `.btn.hide-sm` hides "Your mixes" on mobile; the logo is the only way back.
- **F-39** No `<meta description>`, no Open Graph tags, no share preview — nothing about these pages
  survives being pasted into a chat.
- **F-40** `tr.track:focus{outline:none}` replaced by a 3 px inset bar on the first cell only — a very
  quiet focus state for the primary interaction on the page.
- **F-41** No BPM, key, genre, label, year or artwork on any row. For a paying DJ these are table
  stakes and their absence is the loudest missing feature after the ID itself.
- **F-42** Good, keep: `prefers-reduced-motion` is honoured globally; the equaliser logo animation on
  `body.analysing`; the platform-coloured dots; the `pop` animation on newly-lit scanner cells; the
  NOW pill; the mix cards' hover lift.

---

## 3. Remove / change / add — every visible element

### Home

| Element | Verdict | Reason |
|---|---|---|
| Eyebrow "DJ-SET TRACK IDENTIFIER · RUNS ON THIS MACHINE" | **RENAME** | Half of it stops being true when hosted |
| "Drop a mix. / Get the tracklist." | **KEEP** | Best copy in the product |
| Lede paragraph | **RENAME** | Cut "listens in short windows / asks the recognition engines" — mechanism, not benefit |
| URL box + platform indicator | **KEEP** | Works, reads well, detects platform live |
| Free / Max accuracy segment | **RENAME** | Becomes "Free scan / Deep scan (1 credit)"; drop the "$2 a mix" mechanics |
| "More options" tray | **KEEP** | Right call to collapse it |
| — "Buy / download links" toggle | **REMOVE** | Nobody turns off the best part of the output; make it always on |
| — "Know part of the tracklist?" textarea | **KEEP** (move) | Genuinely useful; promote to a visible secondary action, not a hidden option |
| — Options summary text | **CHANGE** | Reports only the acquire toggle (F-28) |
| Stat: "N mixes analysed" | **KEEP** | |
| Stat: "N tracks identified" | **KEEP** | |
| Stat: "9h 03m of music listened to" | **REMOVE** | Vanity; a hosted product needs the usage meter here instead |
| Mix card: platform chip | **KEEP** | |
| Mix card: duration | **KEEP** | |
| Mix card: "N tracks" | **CHANGE** | Must match the result page (F-2) |
| Mix card: colour-only confidence mini-bar | **CHANGE** | Add a text summary ("mostly likely"); it currently disagrees with the page |
| Mix card: analysed date | **ADD** | Absent everywhere |
| Library search / sort / delete | **ADD** | None exist |
| "In progress" card: raw URL as the title | **CHANGE** | Use the resolved title (F-18) |
| "In progress" card: `recognise — recognising windows` | **RENAME** | Raw phase key in the UI |
| Footer "no account, only short clips…" | **RENAME** | Must survive the hosted rewrite |

### `/new`

| Element | Verdict | Reason |
|---|---|---|
| The whole page | **REMOVE** (merge) | Duplicate of the home form (F-34); keep only as the "try again" landing |
| "How it works" 1-2-3-4 | **KEEP** (move) | Good copy — belongs on the marketing page |

### Progress page

| Element | Verdict | Reason |
|---|---|---|
| Big % number | **KEEP**, **CHANGE** arithmetic | Must be wall-clock proportional (F-9) |
| Phase name + message | **KEEP** | |
| Tile "N / M windows" | **REMOVE** | "Window" is internal vocabulary; nothing actionable |
| Tile "time left" | **KEEP** | The only number anyone cares about |
| Tile "elapsed" | **HIDE-BEHIND-DETAILS** | Useful only when something looks stuck |
| Tile "37.8/min windows / min" | **REMOVE** | Jargon, redundantly double-labelled, meaningless to a user |
| Scanner cell strip | **KEEP** | Best moment in the product; keep even after retiring "windows" as a word |
| Flavour text rotation | **KEEP** | Genuinely charming and buys patience |
| 8-step tracker | **KEEP**, **RENAME** | "Stitch — build track episodes" → "Line up the tracks" |
| Step tracker on failure | **FIX** | Marks nothing; should mark the failed step (F-15) |
| "Listen while it works" audio | **KEEP** local / **REMOVE** hosted | Copyright + egress (F-6) |
| "Show the log" | **HIDE-BEHIND-DETAILS** | Already is — but the failure copy points at it wrongly |
| Raw error text | **CHANGE** | Map to plain causes; never show `yt-dlp` to a customer |
| "opening in 5s" auto-redirect | **CHANGE** | Make opt-in or slower (F-30) |
| Cancel → "nothing was saved" | **RENAME** | False; must state credit impact |
| "You can close this tab" reassurance | **ADD** | Said on `/new`, never on the page where it matters |

### Result page

| Element | Verdict | Reason |
|---|---|---|
| Platform chip | **KEEP** | |
| `length 1:00:11` chip | **KEEP** | |
| `profile free` chip | **RENAME** | Config jargon (F-22) |
| `generation N` chip | **KEEP** as-is (already conditional) | Correctly hidden when 0; keep it that way |
| Title | **KEEP** | |
| Tile "N tracks found" | **KEEP** | |
| Tile "62 % of the set identified / listening time backed by evidence" | **RENAME** | Keep the ring, lose the subtitle |
| Tile: unlabelled confidence bar | **REMOVE** (move) | Give it a caption and put it under the Tracklist heading (F-21) |
| Tile "N ID gaps" | **REMOVE** | Constant at 0 or 1 on every mix analysed to date |
| Analysed date + engine used | **ADD** | Absent |
| Native `<audio>` | **CHANGE** | Unstyled default control (F-23) |
| "Open the set ↗" | **KEEP** | |
| Export: "Copy tracklist" | **KEEP** | The one people will use |
| Export: CUE sheet | **KEEP** | Real DJ format |
| Export: M3U playlist | **REMOVE** | Functionally dead as written (F-3) |
| Export: Markdown | **KEEP**, **CHANGE** | Drop the Version / Role columns |
| Export: JSON | **HIDE-BEHIND-DETAILS** | Developer output in a consumer button row |
| **Share link** | **ADD** | There is no way to share a result at all |
| Timeline strip | **KEEP** | |
| Timeline legend (6 entries) | **REMOVE** 3 of 6 | "prediction interval", "episode extent", "unresolved boundary" (F-17) |
| Time ruler | **KEEP**, **FIX** at narrow widths | Labels collide below ~600 px |
| "Lead-in … s before each track" | **HIDE-BEHIND-DETAILS** | Power-user knob in prime space (F-26) |
| "N matches (46 short, 10 suppressed) hidden · show" | **REMOVE** | The single most damaging element on the page (F-7) |
| "click a row to jump there ↑↓ move ↵ play" | **KEEP** | Good affordance hint; fix its contrast |
| Column: TIME | **KEEP** | |
| Column: CONFIDENCE | **KEEP** | |
| Column: VERSION (`display:none`) | **REMOVE** | 100 % `unverified` across 305 tracks (F-8) |
| Column: ROLE (`display:none`) | **REMOVE** | 99 % `incoming`/`dominant`, never rendered |
| Column: TRACK | **KEEP** | |
| Column: WHERE TO GET IT | **KEEP**, **CHANGE** | Hide the column when empty; never hide it on mobile (F-4, F-10) |
| Column: ops / `rescan` button | **REMOVE** | Dead control that instructs users to run a CLI (F-11) |
| Badge LIKELY / POSSIBLE / UNCLEAR | **KEEP** | |
| Badge VERIFIED | **REMOVE** | Never occurs |
| `HINT` pill | **REMOVE** | Loudest thing in the row, means the least, explained only in a tooltip |
| `CROSS-CHECKED` pill | **RENAME** | "confirmed twice" — and make it the visible proof of what Deep scan buys |
| `FROM COMMENTS` pill + row | **CHANGE** | Must not render raw comment text as a track (F-13) |
| `layer` / `outgoing` / `uncertain` tag | **REMOVE** | 1 occurrence in 305 rows |
| `short · 12s` tag | **REMOVE** | Only visible after the reveal that should be removed |
| `▸ N other versions` | **CHANGE** | Only when a genuinely different track (F-25) |
| Acquire chips (`apple · Buy`, `bandcamp`, `beatport`) | **CHANGE** | Distinguish product links from search links (F-10) |
| Gap row `no evidence for 45:24–46:21` + `no_evidence` tag | **RENAME** | "Couldn't identify 45:24–46:21"; drop the token |
| NOW pill in the top bar | **KEEP** | Nice; currently hidden on mobile, where it would help most |
| Toast | **KEEP** | |
| Footer "nothing leaves 127.0.0.1" | **REMOVE** | False (F-12) |
| BPM / key / genre / label / artwork | **ADD** | The most conspicuous missing value for the paying audience |

---

## 4. Hosted-product UX proposal

### 4.1 Page map

```
PUBLIC
  /                     Landing / marketing
  /pricing              Free vs Pro
  /s/<token>            Public shared tracklist (read-only, no chrome, OG tags)
  /login  /signup       Email magic link + Google
APP (auth required)
  /app                  Library  ── the current home, minus the hero
  /app/new              Analyse form (the current hero form, promoted)
  /app/jobs/<id>        Progress
  /app/mix/<id>         Result
  /app/account          Plan, usage meter, billing portal, delete data
SYSTEM
  /api/…                JSON for the SPA-ish bits (status polling, share toggle)
  /webhooks/stripe
```

### 4.2 Wireframes (text)

**Landing `/`** — Keep club mode exactly as it is; it is the product's biggest asset.
Top bar: logo · Pricing · Log in · **Start free** (gradient).
Hero: `Drop a mix. / Get the tracklist.` over the existing radial-gradient wash, with the **live URL
box right there** and a "Try it free — 1 mix a week, no card" microcopy under it. Pasting a link and
hitting Analyse routes an anonymous visitor to signup with the URL preserved.
Below: a **real screenshot of a result page** (this is the sell — nobody will believe the claim without
seeing the tracklist), then the existing `How it works` 1-2-3-4 strip lifted verbatim from `/new`,
then three testimonial/format cards (CUE for Rekordbox, share link, buy links), then pricing summary,
then footer.

**Sign-up / sign-in** — one card, centred, on the same gradient ground. Email + "Send me a link", or
Continue with Google. No password. After signup, land straight on `/app/new` with the URL they pasted
already in the field and the form focused — the fastest possible first result.

**Pricing `/pricing`** — two cards side by side, the Pro card lifted with the brand gradient top-rule
(reuse `.mix::before`).
*Free* — 1 mix / week · standard scan · 30-day history · share links · CUE + Markdown export.
*Pro £9/mo* — 30 mixes / month · **Deep scan** on every mix (the paid engine, sold as "we check every
track twice — that's the `confirmed twice` tag") · unlimited history · buy/download links · priority
queue · API.
One line under both: "Fair use: a mix is one analysis of one link. Re-running the same link is free."
Stripe Checkout, no card for Free.

**Library `/app`** — the current home minus the marketing hero.
Row 1: `[ paste a link ………………………… ] [Analyse]` (one line, always at the top).
Row 2: usage meter — `▓▓▓░░░  2 of 3 mixes used this week · resets Monday · Upgrade`.
Row 3: `In progress` cards (title, not URL; running first).
Row 4: `Your mixes` — the existing card grid, plus per-card: **analysed date**, a "shared" indicator,
and a `⋯` menu with Rename / Copy share link / Re-analyse / Delete. Add a search field and a
sort toggle (newest / longest / most tracks) once there are more than ~12 cards.
Empty state: keep "No mixes yet — Paste a link above and find out what is in it", and add two example
mixes the user can open without spending a credit.

**Analyse form `/app/new`** — the current form, with the mode segment relabelled:
`Free scan (included)` / `Deep scan — 1 credit · 28 left`. If they're out of credits the Deep card
becomes an upgrade prompt in place rather than a disabled control. Keep the collapsed tray, keep the
paste-a-tracklist box, drop the acquire toggle (always on). Add one line of expectation-setting under
the button: **"About 6 minutes for an hour-long mix. You can close this tab — we'll email you."**

**Progress `/app/jobs/<id>`** — keep the scanner strip, the flavour rotation and the step tracker.
Changes: wall-clock-weighted percentage (F-9); tiles reduced to **time left** and **step 4 of 8**;
"windows" and "windows/min" gone; a persistent "You can close this tab — we'll email you when it's
done" line; no auto-redirect (a big "Open the tracklist →" button instead). Drop the audio player
(F-6) and replace it with the platform embed, which doubles as proof you got the right mix.
On failure: plain cause, remedy button, the failed step marked red, and an explicit
"**No credit was used**" / "1 credit used" line.

**Result `/app/mix/<id>`** — the current page with section 3's removals applied, plus:
- Top bar right: `[ Share ]` → toggles a public `/s/<token>` link, copies it, shows a "Anyone with the
  link can view · Turn off" popover. This is the single biggest growth lever the product is missing —
  a tracklist is inherently a thing people paste into group chats.
- Header meta row: platform · length · **analysed 3 Sep** · **Deep scan**.
- Three tiles only: tracks, length, identified %.
- Custom player merged with the timeline into one component.
- Table: Time · Confidence · Track · Where to get it. On ≤720 px it becomes stacked cards, keeping
  the acquire chips.
- Under the Tracklist heading: the confidence mix bar with a caption, and one line —
  "**Likely** = we're confident. **Possible** = probably right, check it. **ID** = we couldn't tell."
- No hidden-match counter, no rescan buttons, no version/role columns.

**Public share `/s/<token>`** — the result page, read-only: no exports except Copy, no account chrome,
a small "Made with IDea — analyse your own mix" CTA in the footer, and proper OG/Twitter tags so it
previews as `<mix title> — 33 tracks identified`.

**Account `/app/account`** — plan, the same usage meter larger, invoices (Stripe billing portal link),
"Export all my data", "Delete my account". For Pro also: remaining Deep-scan credits and an API key.

**Upgrade prompt when capped** — never a dead end. When the weekly cap is hit, the analyse form
stays visible and the button becomes `Upgrade to analyse this mix →`, with the pasted URL preserved
through Checkout and the analysis started automatically on return. A second, softer prompt on the
result page: "Deep scan finds ~30 % more tracks in sets like this" next to the confidence mix.

### 4.3 Where the current architecture stretches — and where it doesn't

**Stretches (keep):**
- `present/page.py::render_page` is a pure `records → HTML string` function. That's a perfectly good
  server-side template layer for a real framework; nothing about it is tied to `http.server`.
- `present/theme.py` (tokens, top bar, chips) is already a shared design system — lift it wholesale.
- The content-addressed `work/<source_key>/<media_key>/` artefact tree maps cleanly onto object
  storage with the same keys, and the completion sidecars give you idempotency for free.
- `webapp/jobs.py`'s phase/`windows_done`/`eta_seconds` contract is exactly the payload a real
  queue's status endpoint should return — port the shape, not the implementation.
- The `EmbedPlan` / platform-widget bindings already exist and are what the hosted player should use.

**Does not stretch (replace):**
- `make_server` **refuses** to bind anything but loopback by design (`ValueError` on a routable host),
  and it's `BaseHTTPRequestHandler` over HTTP/1.0 with no TLS, no gzip, no cache headers, no keep-alive.
- **One `JobManager` with one worker thread**, deliberately serial to respect the Shazam rate limit.
  Hosted, that means *all* customers share a single lane — mix #4 in the queue waits half an hour.
  You need a real queue (RQ / Celery / Cloud Tasks), per-provider rate-limit budgets shared across
  workers, and per-user concurrency caps. This is the single hardest hosted problem, and it also caps
  how generous the free tier can be.
- Job state is **in-memory only** — a deploy loses every running job and the whole progress list.
  Needs a jobs table.
- **No sessions, no auth, no CSRF, no per-user ownership**, and result URLs are derivable from the
  media hash. Needs Postgres (users, mixes, jobs, usage, share tokens) and signed URLs for artefacts.
- `ensure_fresh_page` **writes on GET** (F-14) — unsafe under concurrency; becomes a migration job.
- `POST /rescan` writes an unauthenticated queue file that only a CLI consumes — delete it.
- Serving the fetched original audio (F-6) — delete it hosted.

**Suggested target:** FastAPI/Starlette behind a CDN, Postgres, S3-compatible storage for artefacts,
a worker pool for the pipeline, Stripe Checkout + webhooks, and a per-user rate-limit ledger. The
pipeline itself (`ingest → decode → windows → recognise → hints → fuse → enrich → present`) needs no
change beyond removing the assumption that `work/` is a local directory.

---

## 5. Top 10 UI improvements, ranked by user value ÷ effort

| # | Improvement | Value | Effort | Why it's top |
|---|---|---|---|---|
| 1 | **Delete the dead data**: the "N matches hidden · show" counter and toggle (F-7), the `rescan` button and column (F-11), the `Version` + `Role` columns (F-8), the `layer` tag, the "ID gaps" tile, the `HINT` pill | High | Very low | Pure subtraction. Removes the single most trust-destroying element on the page and ~180 dead cells per render. One afternoon. |
| 2 | **Make the invalid-URL path human** (F-1) — normalise a missing scheme client-side, inline error, keep the form state | High | Low | It is the first thing a new user can hit, and today it dumps them on raw JSON. |
| 3 | **One track count, one tracklist** (F-2, F-3) — card counts and exports regenerated from the same filtered list as the page | High | Low–medium | Contradictory numbers are the fastest route to "this tool is broken". |
| 4 | **Plain-English confidence** (F-16, F-17) — one badge per row, a one-line glossary under the Tracklist heading, legend cut from six terms to two | High | Low | Removes "prediction interval", "episode extent", "unresolved boundary", "suppressed", "episode" from the customer's vocabulary in one edit. |
| 5 | **Fix the mobile result page** (F-4) — keep the acquire column, stack rows into cards, thin the ruler | High | Medium | DJs read tracklists on their phone. Today the buy links, the thing they'd pay for, are `display:none`. |
| 6 | **Add a share link** (F-39 + §4.2) — public `/s/<token>` + OG tags | High | Medium | The only viral surface a tracklist product has, and it doesn't exist. |
| 7 | **Wall-clock-proportional progress** (F-9) + "you can close this tab" | Medium–high | Medium | The owner's own complaint; also the difference between "it's stuck" and "it's working". |
| 8 | **Honest, consistent footers and error copy** (F-12, F-15) — one true privacy line; failures with a plain cause, a remedy, the failed step marked, and credit impact | Medium–high | Low | A false privacy claim on the result page is a liability, not a polish item. |
| 9 | **Fix `--dim` contrast and table semantics** (F-19, F-20) — raise `--dim` to ~`#8b8ba6`, drop `role="button"` from `<tr>`, add `scope="col"` | Medium | Low | Two token changes plus an attribute swap fixes every failing text colour and the broken table semantics at once. |
| 10 | **Analysed date, delete, and search in the library** (F-24, F-33) — plus a visible record of failed runs | Medium | Medium | Turns a folder listing into something that survives having 50 mixes in it. |

*Just below the line, high value but real effort:* a custom player merged with the timeline (F-23), and
BPM/key/artwork on every row (F-41) — the latter is probably the strongest single reason a DJ would
pay a monthly fee, and it is currently entirely absent.

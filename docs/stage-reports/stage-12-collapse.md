# Stage 12 — Collapse competing near-duplicate matches into one row with alternatives

*A heavily-sampled vocal (the canonical case is Pupa Nas-T / Denise Belfon "Work") makes Shazam
match many **different** official releases of the **same** underlying track across consecutive
windows, so the raw tracklist shows six-plus near-duplicate "Work (X Remix)" rows over ~72 s when it
is really one track. Stage 12 folds such a contiguous cluster into ONE display row: the closest
match is shown and the other candidates ride along as collapsible "could also be" alternatives. This
is a pure, deterministic **presentation** transform — it never touches the committed
`fuse/episodes.json`, the fusion/certification contracts, or the calibration path. The Stage 11
live playhead and current-row highlight keep working, now against the collapsed rows. The page stays
one self-contained `127.0.0.1`-only `present/index.html` with no usernames or comment text.*

## What was built (file map)

**Grouping — `src/id_detector/present/grouping.py` (new, pure + deterministic):**
- `group_display_tracks(episodes, identities, duration_ms, *, gap_ms=20_000) -> list[DisplayTrack]`
  returns ordered `DisplayTrack{primary, alternatives, start_ms, end_ms}`.
- **Work key.** `normalise_title` casefolds, drops bracketed `(...)`/`[...]` asides and `feat.`
  credits, normalises `&`/punctuation, then strips standalone version qualifiers
  (`remix/edit/bootleg/vip/acapella/dub/mix/…`) whenever a content word survives — so
  `"Work (Kevin McKay ViP)"`, `"Work Dub (feat. Denise Belfon)"` and `"Work (Full Acapella) […]"`
  all reduce to `work`, while `"Ibiza (Bootleg Version)"` reduces to `ibiza` and stays distinct.
  `work_key` = `normalise_artist(...)|normalise_title(...)`.
- **Merge rule.** Episodes are ordered by their evidence-support span. A run joins one display track
  when spans overlap or sit within `gap_ms` (20 s) **and** they name the same underlying track
  (equal title stem, or equal full work key, or a mutually-competing ≥ 50 % support overlap whose
  title tokens are a subset). Multiple open groups are kept per time-region, so a distinct work
  wedged between two appearances (the 11:54 "Ibiza") never dissolves the cluster around it. A group
  is retired once the cursor moves a clean > 20 s past it, so a genuine re-appearance later in the
  set (occurrence > 0, separated by other tracks) stays a **separate** display track.
- **Primary ("closest match").** Highest badge → most independent trials → largest evidence support
  → widest proved-present span → earliest start → candidate/episode-id tie-break. The rest become
  `alternatives`, de-duplicated by candidate and ordered by the same score.

**Exports — `src/id_detector/present/exports.py`:** `flatten_tracklist`/`export_tracklist` gain
`collapse=True`. Collapsed, each display track is one row = the primary (time, badge, version, role,
track, acquire) plus a compact `also: N other versions matched — a; b; …` note in Markdown and an
`alternatives` array (`{badge, version_status, artist, title, track, …}`) in JSON. CUE/M3U use the
primary only (one cue per entry). The folded-in versions are removed from the primary's CUE
overlap/REM note. `collapse=False` restores the old one-row-per-episode view; the benchmark corpus
export pins `collapse=False` so scoring is unchanged.

**Page — `src/id_detector/present/page.py`:** `render_page`/`generate_page` gain `collapse=True`.
Each collapsed row shows the primary and, when it has alternatives, a native `<details class="alts">`
`▸ N other versions` disclosure that expands inline (no extra requests; the row's click-to-seek
ignores `a,button,details,summary`). Timeline lanes, the `EPISODE_SPANS` partition and the
current-row/lane highlight all key off the display track's **primary** id, so the Stage 11 playhead
lights the display track containing the position. `collapse=False` keeps the exact per-episode lanes.

**Config / CLI:** `AppConfig.collapse` (default `true`) reads `[present] collapse`; `analyse` gains
`--collapse/--no-collapse` (flag wins over config). Template, example TOML and `config show` updated.

**Tests — `tests/test_collapse.py` (new, deterministic, network-free):** (a) six synthetic "Work"
variants collapse to ONE display track with the `possible` release as primary and five
alternatives; (b) two different adjacent tracks stay two rows; (c) a candidate returning 10 min
later (occurrence 1) is not merged with occurrence 0; (d) a clean > 20 s gap prevents a merge;
(e) the work-key normaliser strips remix/vip/acapella/feat; (f) the page carries the disclosure
markup and a working playhead against the collapsed row (parseable HTML, one span per display
track); (g) the collapsed page has no usernames/comment text (fixture-audit patterns). Existing
page/exports/server tests that pin the one-row-per-episode view were switched to `collapse=False`.

## Verification

- `uv run pytest -q` → **518 passed, 93 deselected** (network suites deselected by default).
- `uv run ruff check .` → **All checks passed!**
- `uv run ruff format --check .` → **176 files already formatted**.
- `uv run python scripts/audit_fixtures.py` → **audited 347 files / fixture audit passed**.
- `uv run id-detector analyse "<gemfest set url>" --max-generations 0` re-fused gen-0 from cache
  (0 network, **82 episodes**) and rewrote the gemfest page/exports. The tracklist now shows **72**
  display-track rows (was 82 episodes → 82 rows). The **"Work" cluster (11:18–12:48) collapses from
  8 same-vocal rows to ONE** row (POSSIBLE "Work (Kevin McKay ViP)" + 6 alternatives); the 11:54
  "Ibiza (Bootleg Version)" and 13:06 "Murderation" stay their own rows. The regenerated
  `index.html` carries 4 `<details class="alts">` disclosures, 72 `EPISODE_SPANS`, the playhead
  element / `updatePlayhead` / current-row highlight, and a fresh handle/identifier scan finds
  nothing.

### The collapsed "Work" cluster, from the regenerated `tracklist.md`

```
| 11:18 | POSSIBLE | UNVERIFIED | incoming | Pupa Nas T, Kevin McKay & Denise Belfon — Work (Kevin McKay ViP)<br>also: 6 other versions matched — Puppah Nas-T — Work (Full Acapella) [feat. Denise "Saucey Wow" Belfon]; Kevin McKay, Pupa Nas T & Denise Belfon — Work (CVMPANILE & Draxx Remix); Pupa Nas T & SHUFFA — Work Dub (feat. Denise Belfon); Chris Lorenzo, Denise & Puppah Nas-T — Work; Masters At Work — Work (DJ's Of The Planet Remix); Masters At Work — Work | — | — | — | — |
| 11:54 | UNCLEAR | UNVERIFIED | incoming | Desaparecidos & Walter Master J — Ibiza (Bootleg Version) | — | — | — | — |
| 13:06 | LIKELY | UNVERIFIED | incoming | Soul Mass Transit System — Murderation (4 The Bristol Crew Mix) | — | — | — | — |
```

Previously those same 11:18–12:48 minutes rendered as eight near-duplicate "Work (…)" rows plus the
interleaved "Ibiza" row.

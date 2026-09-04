# Stage 13 — Stack same-exact-track detections across a longer gap

*Stage 12 folds a contiguous run of competing near-duplicate matches of one underlying track into a
single display row, but only when the appearances sit within a 20 s adjacency gap. Two detections of
the **same exact track** a bit further apart therefore stayed two rows even though it is one
continuous play — the canonical case is **Harry Mariani — E-Tales** matched at **29:45** and
**31:03** (78 s apart, nothing between). Stage 13 adds a same-exact-track **bridge** on top of the
Stage 12 collapse: two appearances of one track (equal work key) up to ~3 min apart stack into ONE
row, as long as no **different** confident (≥ `possible`) track starts between them. A genuine repeat
later in the set — after a confident different track, or beyond the bridge — stays its own row. This
is the same pure, deterministic **presentation** transform as Stage 12: it never touches the
committed `fuse/episodes.json`, the fusion/certification contracts, or the calibration path. The
Stage 11 live playhead and current-row highlight and the Stage 12 collapse keep working; the page
stays one self-contained `127.0.0.1`-only `present/index.html` with no usernames or comment text.*

## What changed (file map)

**Grouping — `src/id_detector/present/grouping.py`:**
- New `DEFAULT_SAME_TRACK_BRIDGE_MS = 180_000` (≈ a typical track length, 3 min) and a
  `_SEPARATOR_BADGE_RANK` (= `possible`).
- `group_display_tracks(..., *, gap_ms=20_000, same_track_bridge_ms=180_000)` gains the bridge.
  Groups are now retired only once the cursor moves clean past `max(gap_ms, same_track_bridge_ms)`,
  so a same-work group stays open long enough to bridge. Merge selection (`_choose_target`):
  1. the **existing** adjacency / competing-versions merge still wins when spans overlap or sit
     within `gap_ms` (20 s) **and** `_same_underlying` holds — unchanged;
  2. **added** same-exact-track bridge — an episode with an **equal non-empty work key** joins an
     earlier group across a wider gap (`0 ≤ gap ≤ same_track_bridge_ms`) **unless** that group is
     `bridge_blocked`.
- A `_OpenGroup.bridge_blocked` flag is set whenever a **different** confident (badge rank ≤
  `possible`) track is placed after the group's latest member — the hard separator. The group the
  separator itself lands in is exempt (it is the same play, not a separator for itself). An
  intervening **UNCLEAR** different-work blip is treated as noise and does **not** block bridging.
  The competing-versions / ≤ 20 s rules are untouched; only exact same-work bridges reach 3 min.

**Config — `AppConfig` / `config_template.py` / `id-detector.example.toml`:** new
`present.same_track_bridge_ms` (default `180000`, validated as a non-negative integer). Threaded
through `flatten_tracklist` / `export_tracklist` / `render_page` / `generate_page` (a `None` sentinel
falls back to the grouping default) and passed from both `analyse` and the acquire-refresh page
rewrite in `cli.py`. No new CLI flag. The `--no-collapse` path is unaffected (no grouping at all).

**Tests — `tests/test_collapse.py`:** added (a) two same-track episodes 78 s apart with nothing
between → **ONE** row spanning both; (b) a **POSSIBLE** different track between two same-track
appearances → **TWO** rows (three with the intervening track); (c)
`test_clean_gap_wider_than_bridge_prevents_merge` — same track 3 min + 8 s apart (> bridge) → **TWO**
rows (repurposed from the old 28 s test, whose 28 s gap now correctly bridges); (d) an intervening
**UNCLEAR** different-work blip does **not** prevent bridging → **ONE** row. The existing Stage 12
tests still pass: the "Work" cluster still collapses to one row, the genuine repeat 10 min later
stays separate, and the page still carries the disclosure markup + working playhead.

## Verification

- `uv run pytest -q` → **521 passed, 93 deselected** (network suites deselected by default; +3 over
  Stage 12's 518).
- `uv run ruff check .` → **All checks passed!**
- `uv run ruff format --check .` → **177 files already formatted**.
- `uv run python scripts/audit_fixtures.py` → **audited 348 files / fixture audit passed**.
- `uv run id-detector analyse "<gemfest set url>" --max-generations 0` re-fused gen-0 from cache
  (0 network, 0 physical attempts, **82 episodes**) and rewrote the gemfest page/exports. The
  tracklist now shows **71** display-track rows (was **72**). The two **E-Tales** detections
  (29:45 + 31:03) are now **ONE** row at 29:45 (the 31:03 detection is the same candidate, so it
  folds in with no extra "also" line). The **"Work" cluster (11:18)** still collapses to ONE row
  (POSSIBLE "Work (Kevin McKay ViP)" + 6 alternatives); **"Ibiza" (11:54)** and **"Murderation"
  (13:06)** stay their own rows. The regenerated `index.html` carries 71 `<tr class="track">` rows,
  71 `EPISODE_SPANS`, 4 `<details class="alts">` disclosures, the playhead element /
  `updatePlayhead` / current-row highlight, and a fresh handle/identifier scan finds nothing.

### The E-Tales stack, from the regenerated `tracklist.md` (29:00–32:00)

Before (Stage 12) those minutes rendered as **two** E-Tales rows plus the following tracks:

```
| 29:45 | UNCLEAR | UNVERIFIED | incoming | Harry Mariani — E-Tales | … |
| 31:03 | UNCLEAR | UNVERIFIED | incoming | Harry Mariani — E-Tales | … |
```

After (Stage 13) the two E-Tales detections stack into one row; the next tracks (a confident
`Freedom 2` etc.) are genuinely different and start after the play, so they are unaffected:

```
| 29:45 | UNCLEAR | UNVERIFIED | incoming | Harry Mariani — E-Tales | — | — | — | — |
| 31:24 | UNCLEAR | UNVERIFIED | incoming | Saavage & Digital Burst — Hiclipse | — | — | — | — |
| 31:33 | POSSIBLE | UNVERIFIED | incoming | Kwengface, Joy Orbison & Overmono — Freedom 2 | — | — | — | — |
```

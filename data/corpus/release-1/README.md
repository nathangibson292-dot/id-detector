# release-1 — the owner-verified real-mix corpus (launch gate L3)

*Started 2026-09-10. Status: **all 7 drafts seeded**; not yet verified or frozen. First working numbers (Free recipe): [docs/accuracy/release-1-free-draft.md](../../docs/accuracy/release-1-free-draft.md).*

Each set folder holds a `ground_truth.json` created by `idea truth seed` from the matching file in
`tracklists/`. The committed-corpus policy applies: no URLs, no platform handles or numeric IDs in this
folder. The set-id → source-URL map lives privately in `data/local/corpus-sources.json` (git-ignored);
runs are matched to truth by `media_key`, not by URL.

| Set id | Split | Tracks | Source of truth | Notes |
|---|---|---|---|---|
| `release1-boomtown-mix` | test | 40 | owner's rekordbox playlist, order only | 81-min set |
| `release1-redo-of-best-set` | test | 18 | owner's rekordbox playlist, order only | 49 min |
| `release1-final-new-set-christmas` | test | 23 | owner's rekordbox playlist, order only | 46 min |
| `release1-new-mix-jan-24th` | **dev-1** | 30 | owner's rekordbox playlist, order only | 55 min; **tuned on** (the 30 s on-air floor) — report separately, never in the clean number |
| `release1-mall-grab-boiler-room-melbourne-22` | test | 25 | public tracklist with minute-level timestamps; many unreleased ("UR") and three unnamed "ID"s | second DJ; the underground stress case; **seeded** (timed); tracklist clock ≈ +48 s vs the video |
| `release1-mph-youtube-set` | test | 54 + 9 overlays | public YouTube comment tracklist with timestamps; "w/" overlays kept as overlapping entries | third DJ, **second platform (YouTube)**; live recording with crowd noise; **seeded** (54 + 9 overlays via `--overlays`) |
| `release1-dj-heartstring-youtube-set` | test | 23 | public YouTube comment tracklist with timestamps; 4 unknown IDs | fourth DJ, YouTube; **seeded** (timed); tracklist clock ≈ +51 s vs the video |

## What "order only" means for scoring

The rekordbox playlists give the tracks and their order but not when each started, so these sets can
score **work recall / precision** (did we name the right tracks) but not boundary accuracy. The seeded
drafts split the mix into equal slices as placeholders (`start_ms_range` / `end_ms_range`); a verifier
should either leave them (work-only scoring) or replace them with real start times by listening.
The Mall Grab set has timestamps and can score boundaries.

## How to verify a draft (owner workflow)

```powershell
uv run idea truth verify --help        # first pass: confirm each row, fix titles, add start times if known
uv run idea truth second-pass --help   # a second person, blind
uv run idea truth resolve --help       # settle disagreements
uv run idea truth freeze --help        # freeze the corpus version (immutable manifest)
```

Then run each mix (`uv run idea analyse <url> --recipe free|deep`) and score with
`scripts/score_corpus.py` (built in plan cycle 1b-iii; until then `idea benchmark score --truth
<ground_truth.json> --episodes <run>/fuse/episodes.json --out <json>` per mix).

## Gaps against the L3 gate

L3 asks for ≥ 5 mixes, ≥ 3 DJs, ≥ 2 platforms, ≥ 4 h total. Present (2026-09-10): 7 mixes, 4 DJs (owner,
Mall Grab, MPH, DJ Heartstring), 2 platforms (SoundCloud, YouTube), ~8.2 h. **The corpus-shape gate is met;
what remains is verification** (first pass by the owner, a blind second pass, resolve, freeze).

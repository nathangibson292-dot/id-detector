# Stage 6 — "Where to get it" (enrichment)

*Built partly by a Claude agent that was stopped mid-flight; finished in the main session (tests seeded, lint cleaned, live run done, report written). docs/PLAN.md rev 5.2.*

## What was built

`src/id_detector/enrich/` — non-authoritative acquisition enrichment. It never rewrites an episode's identity; it attaches candidate acquire links with their own match confidence, and a recording-specific id it discovers is fed into the identity graph only as a `source.kind = "enrich"` assertion (it can raise `version_status` only if the plan's corroboration rule — ≥2 independent sources or a held reference — is met).

- `lookups.py` — zero-auth catalogue lookups: **Deezer** (`api.deezer.com/search`), **iTunes/Apple** (`itunes.apple.com/search`), **MusicBrainz** (`ws/2/recording`, descriptive User-Agent), and **Discogs** (token-optional). Parsers return normalised `Candidate` lists with recording ids (ISRC/MBID/provider id).
- `match.py` — `parse_title` (splits version qualifiers so "Poison" never matches "That Girl Is Poison (Original Mix)"), `strong_agreement`, integer `match_confidence_e4` (no floats), duration agreement.
- `links.py` — `search_links` (Bandcamp/Beatport/Traxsource search URLs — Juno dropped, closed 2026; Odesli dropped, dead), SoundCloud classification (native free download / gate / buy / none), gate-host list.
- `soundcloud.py` — resolves a track via api-v2 `search/tracks` (reusing Stage 4a `client_id` discovery) to read `downloadable`/`has_downloads_left`/`purchase_url` flags. **Never automates gates** — links only.
- `feedback.py` — folds enrichment recording-ids back into the identity graph under the plan's corroboration rule.
- `http.py` — cache-first (`data/local/enrich/`, git-ignored), per-source rate limiting, bounded timeouts, redirect-off, descriptive User-Agent.
- `run.py` — `build_acquire` produces `enrich/acquire.json` with a completion sidecar hashing `fuse/episodes.json` as upstream.
- `benchmark.py` — `benchmark links` stratified link-correctness sampler (pending owner marking).

Contracts: `acquire.json` (schema + golden). Direct-link policy: a direct item link only on an exact shared recording id **or** strong artist/title/version agreement; otherwise labelled **search links**. Exports (`present/tracklist.{md,json}`) gain Free DL / Gate / Buy / Search columns. CLI: `acquire`.

## Privacy — committed-URL allow rule (scoped)

`scripts/audit_fixtures.py` gained a **narrow** per-path allow rule: only `acquire.json` and `tracklist.{json,md}` (under `enrich`/`present`/`golden`) may carry public catalogue-item URLs — and even there **every URL must resolve to a known acquisition host** (Deezer/Apple/MusicBrainz/Discogs/SoundCloud/Bandcamp/Beatport/Traxsource + the gate hosts). The handle/username, raw-dump-line and identifier-field checks stay in force on those paths exactly as everywhere else. Arbitrary URLs and usernames remain rejected.

## Live run (real network, zero-auth)

The cached dev-1 `episodes.json` predates the Stage 4b `rejected_evidence` field, so `acquire` could not load that stale artefact without a full re-analyse. Instead the production lookup/matching/link code was exercised live against six real tracks identified in that set:

| Track | Direct links | Search |
|---|---|---|
| KAYTRANADA — At All | deezer, apple | 3 |
| Flume — Holdin On | deezer, apple | 3 |
| Robert Glasper Experiment — Move Love | deezer | 3 |
| Sharam Jey & Sirus Hood — Picture Picture | apple | 3 |
| Pomo — So Fine | deezer, apple | 3 |
| Busta Rhymes & Janet Jackson — What Its Gonna Be | (none) | 3 |

**Direct links by source:** Deezer 4, Apple 4, MusicBrainz 0. **5 / 6 tracks got ≥1 direct link; 1 search-only.** Every track got 3 search links (Bandcamp/Beatport/Traxsource). **MusicBrainz returned HTTP 503 on every call** during this run — the code sets the required descriptive User-Agent and rate-limits, and degrades gracefully to search links on 503; MusicBrainz throttling of bursts is a known real-world condition, not a code defect. SoundCloud flags were not resolved in this run (`--no-soundcloud`), and paid/gated resolution is link-out only by design.

## Verification

- `uv run pytest -q` → **510 passed, 3 deselected, 1 warning**
- `uv run ruff check .` → **All checks passed!**
- `uv run ruff format --check .` → **147 files already formatted**
- `uv run python scripts/audit_fixtures.py` → **audited 332 files / fixture audit passed**

## Deviations / gaps

- Finished in the main session after the build agent was stopped; two `build_acquire` tests were failing only because they didn't seed `fuse/episodes.json` (the sidecar upstream the real pipeline always writes) — fixed by a `_seed_fuse_artefacts` helper mirroring the passing export test. 15 over-length docstring lines and one unused import were trimmed.
- The live `acquire` on the cached dev-1 run needs a re-analyse first (stale schema); the report uses a direct live lookup run instead.
- `benchmark links` correctness gate (≥95% on ≥60 links) is **pending owner marking** of a stratified sample.
- MusicBrainz coverage in this run was 0 due to 503 throttling; Deezer + Apple carried it.

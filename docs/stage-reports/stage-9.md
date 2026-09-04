# Stage 9 — "Polish"

*docs/PLAN.md rev 5.2. Final v1 stage. Delivers the Stage 9 build-order row (CUE flattening, config,
docs → daily-usable) plus the consolidation and the authoritative resolution of the dev-1 "39 → 55"
episode-count question. Stage 8 (Panako full) stays excluded from v1 pending the owner's JDK
decision — not built.*

## What was built (file map)

**Exports (task 1) — `src/id_detector/present/exports.py`:**
- `render_cue` now emits `REM OVERLAP "<label>"` / `REM LAYER "<label>"` lines inside each TRACK for
  co-sounding episodes, so the flat CUE sheet no longer silently drops overlapping tracks. The
  primary-role flattening was already present (incoming starts at `best_start_ms`, so the previous
  track's out point is the next INDEX).
- New `render_m3u` writes `present/tracklist.m3u`: an extended M3U where every entry carries
  `#EXTVLCOPT:start-time=<seconds>` against one `media_target` (the mix URL), so opening it in VLC and
  picking a track jumps to that moment. ID gaps become their own labelled entries.
- `flatten_tracklist` enriches each track entry with `overlap_labels` and `has_layer`.
- `export_tracklist` gains `media_target`; writes `tracklist.m3u` (+ sidecar) alongside
  `tracklist.{json,md,cue}`. `ExportResult` gains `m3u_path`. `present/__init__.py` re-exports
  `render_m3u`.

**Config (task 2) — `src/id_detector/providers/base.py`, `config_template.py`, `cli.py`:**
- `AppConfig` gains `default_profile`, `max_requests`, `lead_in_ms`, `cache_positive_max_age_days`,
  `cache_no_match_max_age_days`, `hints_enabled`, `disabled_hint_connectors`, plus
  `cache_*_max_age_seconds` properties. `AppConfig.load` parses new top-level keys and `[cache]` /
  `[hints]` tables, rejecting unknown hint connectors and invalid values.
- New `src/id_detector/config_template.py`: the single documented `CONFIG_TEMPLATE` (profiles,
  budgets, transforms, schedule, rescan, cache, hints on/off, `allow_third_party_upload`, lead-in,
  with the env-var precedence and no-secrets rules as comments) and `render_effective_config`.
- New `id-detector config show` (prints the resolved config; never a secret) and `id-detector config
  init [--path] [--force]` (writes the template). The committed `id-detector.example.toml` is
  regenerated to be byte-identical to `CONFIG_TEMPLATE` (a test locks this).
- Wiring: `lead_in_ms` → the page; `[cache]` TTLs → `recognise_generation`/`cache_valid`;
  `disabled_hint_connectors` → `run_hints` (a disabled connector runs no I/O, recorded `disabled`);
  `max_requests` → the `--max-requests -1` sentinel default; `default_profile` → analyse when
  `--profile` is omitted (a profile stays the authority on engines/geometry while the file supplies
  lead-in/budget/cache/hint prefs). `--max-requests` help updated.

**CLI ergonomics (task 3):** the command tree now reads `doctor · config show|init · analyse ·
acquire · serve · rescan · hints · calibrate-shazam · show · retry · benchmark … · truth …`. Help
text on the new commands and the reworked `--max-requests` is plain-language; existing progress
output (windows/requests/generations/stop-reason) and named-fix error messages are unchanged.

**Docs (tasks 4, 5):**
- `README.md` — rewritten for a non-programmer owner: doctor → analyse → serve quick start; what the
  badges and version status and "provisional" mean; what is excluded from v1 and why (Panako/JDK,
  unverified corpus); how to add paid engines and their cost expectations; how to verify truth and
  freeze a corpus; the personal-use / link-out-to-gates / opt-in-upload legal notes. A short
  developer section keeps the test/lint/schema commands.
- `docs/STATUS.md` — new: honest per-stage acceptance summary (met / met-controlled-only /
  met-Shazam-only / pending-owner / excluded) with links to every stage report and review, the
  standing owner decisions, and the full 39-vs-55 resolution.
- `docs/reviews/README.md` — code-review rounds table extended with stages 4b/4c/5 (verdict,
  P0/P1/P2, outcome) and a note that 4d/6/7/9 had no separate Codex round.

**Housekeeping (task 6):** `tests/conftest.py` tags four controlled-render / full-pipeline modules
`slow`; `pyproject.toml` default `addopts` is now `-m 'not slow and not live'` (full offline run:
`pytest -m "not live"`; live: `pytest -m live`). New tests: `tests/test_stage9_config.py` (13),
`tests/test_stage9_exports.py` (7), and two alignment regression guards in
`tests/test_stage2b_alignment.py`.

## How to run it

```powershell
uv run id-detector config init            # write a documented id-detector.toml
uv run id-detector config show            # print the effective settings (no secrets)
uv run id-detector analyse "<mix-url>"    # writes present/index.html + tracklist.{json,md,cue,m3u}
uv run id-detector acquire "<mix-url>"    # add where-to-get links
uv run id-detector serve                  # browse results, loopback only
uv run pytest -q                          # fast default suite (slow + live deselected)
uv run pytest -m "not live"               # full offline suite (includes slow)
uv run ruff check . ; uv run ruff format --check .
uv run python scripts/audit_fixtures.py
```

## What I verified and how (key output)

- **Default suite:** `uv run pytest -q` → **459 passed, 92 deselected, 1 warning in 52.6 s** (under
  the 90 s target; the 92 deselected = 89 slow + 3 live). New Stage 9 + guard tests included.
- **Full offline suite:** `uv run pytest -q -m "not live"` → see final summary (all slow tests still
  pass).
- **Lint/format/audit:** `ruff check .` → All checks passed; `ruff format --check .` → 159 files
  already formatted; `audit_fixtures.py` → fixture audit passed; `uv lock --check` → clean; built
  wheel smoke test (`test_stage1_wheel`) passes.
- **Config:** `config init` output is byte-identical to `id-detector.example.toml`; `config show`
  with no file prints built-in defaults and contains no credential name; the template parses to a
  fresh `AppConfig()`.
- **End-to-end demonstration (cached dev-1, network-free).** On the cached DJ Three set
  (`work/c5dc…/ec0a…/`, 399 obs) I re-fused offline and ran the full presentation pipeline: **55
  episodes / 0 gaps**; `export_tracklist` wrote `tracklist.{json,md,cue,m3u}` (CUE carried **81 REM
  OVERLAP lines**; M3U carried `#EXTVLCOPT:start-time=…` per entry against the mix URL);
  `generate_page` wrote a 68 KB `index.html`; `serve_in_background` + loopback GET returned **HTTP
  200** for the index and the set page (55 track rows) and shut down cleanly (no hung process). No
  recognition/hints/acquire network was re-run.

  `tracklist.md` (first rows):

  ```
  | Time | Badge | Version | Role | Track | Free DL | Gate | Buy | Search |
  | 0:00  | LIKELY   | UNVERIFIED | dominant | Anthony Collins — Feeling Ok (Original Mix;Digital Exclusive) |
  | 0:21  | UNCLEAR  | UNVERIFIED | incoming | UNKLE & Keinemusik — Only You … [&ME Remix] |
  | 0:30  | UNCLEAR  | UNVERIFIED | incoming | Tony Romera, ASDEK & Karina Ramage — All I Know (Edit) |
  | 14:54 | POSSIBLE | UNVERIFIED | incoming | Javier Logares — Silicon Drift (Roman Flügel Remix) |
  …  (55 tracks total)
  ```

  `durations` (a partition — the parts sum to the 3,587,506 ms duration exactly):

  ```json
  { "evidence_supported_ms": 2414506, "predicted_episode_ms": 0,
    "unresolved_boundary_ms": 753000, "unclear_ms": 378000,
    "no_evidence_ms": 42000, "unscanned_ms": 0 }
  ```
  (2414506 + 753000 + 378000 + 42000 = 3,587,506.)

## The dev-1 "39 → 55" resolution (authoritative)

**Granularity / conflation of two different mixes — NOT fragmentation. No code change was needed.**

The premise (that the *same* cached gen-0 observations re-fuse to a different count) is mistaken. 39
and 55 are **two different Boiler Room sets**, both used as `dev-1` captures:

| | Set A (39) | Set B (55) |
|---|---|---|
| Set | **Kaytranada** Boiler Room Montreal | **DJ Three** 60-min Boiler Room mix |
| Work dir | `data/local/work-dev1-live/9474…/1501…/` | `work/c5dc…/ec0a…/` |
| Duration / gen-0 obs | 2,525,123 ms / 281 | 3,587,506 ms / 399 |
| Committed `episodes.json` | 39 episodes, 37 candidates | 55 episodes, 48 candidates |
| Re-fuse with current `build_episodes` | **39** (identical) | **55** (identical) |

Set A's committed `episodes.gen0.done.json` sidecar names its exact upstreams; those five files were
hashed and confirmed byte-for-byte (observations `3f67bdd6…`, windows `96d709a1…`, identities
`41d27f2d…`, hints `b468d644…`, pcm `fc59b31b…`, invocation `3cef4e5fb3c08345f5a8`). Re-fusing those
exact inputs offline gave **39/0/37** — identical to the committed file. So 39 vs 55 is a 42-minute
Kaytranada set (37 tracks) versus a 60-minute DJ Three set (48 tracks): **more distinct tracks in a
longer, different mix.**

**Fragmentation ruled out.** Every multi-occurrence candidate in both sets (Set A: 2; Set B: 7) is a
genuine reference recurrence — the reference returns to the same or an earlier region after a > 30 s
mix gap (e.g. Set A `dcd68806bb24`: the intro region, ref ≈ 0, replayed 36 s later), or two isolated
single-point matches minutes apart — never a continuous forward-advancing track chopped over a
sub-30 s gap. `align_candidate_points` applies continuation-by-reference-consistency *before* replay:
a consistent forward point continues one occurrence for any gap ≤ 120 s (guarded only against a
same-region recurrence after > 30 s). Locked in by `test_replay_and_continuation_gap_boundaries`,
`test_reference_consistent_forward_run_across_a_long_drought_stays_one_occurrence`,
`test_anonymised_real_anchor_excerpt_is_one_continuous_occurrence`, and the new
`test_real_dev1_intro_replay_is_granularity_not_fragmentation` (anonymised mix/ref anchor pairs from
the real Set A intro-replay candidate; no labels). The old 39 file predates rev 5.2
(`rejected_evidence` absent) yet its count is invariant under the Stage 4b `T_ind` and Stage 4c
alignment changes (39 → 39) — those move tiers/bounds/provenance, not the number of occurrences.

Full evidence in `docs/STATUS.md`.

## Deviations from plan

- **M3U media target.** The plan names "M3U with `#EXTVLCOPT:start-time`" without fixing the media
  URI; the CLI passes the mix's `canonical_url` so double-clicking the playlist streams the set and
  seeks. When no target is known, the URI falls back to `audio` (matching the CUE `FILE "audio"`).
- **CUE REM.** Overlaps are noted as `REM OVERLAP`/`REM LAYER` per co-sounding episode (the plan says
  "overlapping layers noted in REM lines"). The label keyword is `LAYER` when the episode has a
  `layer` role segment, else `OVERLAP`.
- **`default_profile` + config prefs.** Passing `--profile` (or `default_profile`) now still lets
  `id-detector.toml` supply lead-in/budget/cache/hint preferences; previously `--profile` ignored the
  file entirely. This is additive and existing profile tests (which load no file) are unchanged.
- No change to `docs/PLAN.md`.

## Known gaps / for the owner

- Every real-mix accuracy tier is `provisional` (no funded, second-pass-verified corpus). Paid
  engines (AudD/ACRCloud) and Panako/reference-pool are wired but gated on owner action — see the
  three standing decisions in `docs/STATUS.md`.
- The completion-sidecar name collides for `tracklist.{json,md,cue,m3u}` (all map to
  `tracklist.done.json` via `with_suffix`); pre-existing behaviour, harmless (the export set is
  written atomically together), noted for a future cleanup.
- Per-connector hint toggles skip a connector's I/O and record it `disabled`; the exhaustive
  cross-product of connector toggles is not benchmarked (out of scope for polish).

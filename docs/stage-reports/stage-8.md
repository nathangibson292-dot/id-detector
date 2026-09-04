# Stage 8 — "Panako full (unreleased / SoundCloud-only long tail)"

*docs/PLAN.md rev 5.2, Build-order row 8. Turns the Stage 3 disabled skeleton into a working
`local_index_query` reference-pool matcher: robust JDK discovery, a pinned+verified Panako jar,
an index `store`/`query` provider that normalises Panako output into the observation contract, a
candidate-pool builder that catches unreleased / SoundCloud-only tracks, scanner-path fusion
wiring, and a Panako-enabled `max_accuracy` v2 profile derivation. A JDK is now installed, so the
plan's Open-Question 3 ("JDK for a minimum Panako path, or exclude reference-pool from v1?") is
resolved **in favour of including reference-pool recognition** — proven end-to-end on this
machine.*

## What was built (file map)

- `src/id_detector/providers/panako.py` — full rewrite of the skeleton:
  - **JDK discovery** `resolve_java()` in the plan's fixed order — `$JAVA_HOME/bin/java(.exe)` →
    newest match of the Adoptium/Temurin/Microsoft/Zulu/Corretto/`jdk-*` install globs →
    `shutil.which("java")` — with every dependency injectable so the order is unit-testable
    without a second JDK. `doctor_detail()` now runs `java -version` and reports
    **`PASS  JDK <version> at <path>`**.
  - **`PanakoRuntime`** (resolved java + pinned jar + JDK-16+ `--add-opens` flags) and
    **`PanakoIndexPaths`** (per-index LMDB + cache dirs, manifest path).
  - **`PanakoProvider.store` / `.query_wav`** — run java as a subprocess under the existing Windows
    Job-Object launcher (`process.run_process`) with per-call timeouts.
  - **Output parsers** `parse_store_output` / `parse_query_output` and **`normalise_matches`**,
    which maps each Panako match to an `ObservationRecord`: `capability=local_index_query`,
    `transform=null`, `logical_trial_id = sha1("panako" ‖ chunk_index)` (the shared scanner helper),
    `mix_span_ms` = window start + Panako's in-query offset, an `anchor` mapping that mix time to
    the reference offset (`method="panako_query_offset_to_reference_offset"`), `score_raw` = the
    fingerprint match count, and the time/frequency scale factors stored as integer e4 in `native`.
  - **Windows LMDB workaround** `ensure_index_ready` + `sparse_extend` (see "Two Windows realities").
- `src/id_detector/providers/panako_setup.py` — download the pinned jar, **verify size + sha256**
  (never trust an unverified binary), write the platform-correct `config.properties`, create the
  git-ignored index root, and confirm the jar starts. Fails gracefully with exact manual steps.
- `scripts/setup_panako.py` — thin CLI wrapper around the above.
- `src/id_detector/candidates.py` — candidate-pool discovery + indexing. `parse_flat_playlist`
  (network-free), live `discover_candidates` (uploader uploads via a set→uploader resolution, artist
  `scsearch`, explicit URLs), `index_candidates` (download → fingerprint → **delete audio**, keeping
  only the DB), manifest with `index_id`/`index_version` for the local-index cache key.
- `src/id_detector/cli.py` — new `id-detector panako-setup` and `id-detector build-index`.
- `src/id_detector/fuse/scanners.py` — registers the Panako anchor conversion and treats
  `local_index_query` as a scanner-path capability (`SCANNER_CAPABILITIES`); Panako is a self-hosted
  free **independent** source (never on the commercial dependence prior), no cascading suppression.
- `src/id_detector/profiles.py` — `derive_profile(..., panako_index=…)` and
  `derive_max_accuracy_v2(...)`: with a JDK + built reference pool present, Panako becomes an
  *enabled* engine in a **`max_accuracy` v2** profile. Passing `panako_index=None` reproduces the
  committed `free`/`max_accuracy` v1 profiles byte-for-byte (verified: golden `profile.json` and all
  Stage 4d tests still pass).
- Tests: `tests/test_stage8_panako.py` (18: parsers, normalisation + anchor/factor vectors, JDK
  discovery order, config, sparse-extend, the "audio deleted after fingerprinting" guarantee, and a
  `slow` end-to-end that actually runs java+Panako), `tests/test_stage8_candidates.py` (8).
  Updated the two now-obsolete "Panako disabled" assertions in `test_stage3_providers.py` and
  `test_stage4c_scanners.py` to the Stage 8 reality (JDK discovered; jar-gate). Fixtures under
  `tests/fixtures/panako/` and `tests/fixtures/candidates/` (authored, synthetic, sanitised of
  URLs/handles/IDs per the committed-corpus policy; audit-clean).

## Obtaining Panako (pinned + verified)

| field | value |
|---|---|
| Release tag | `joss` (GitHub release name **v2.1.0**, published 2022-10-11) |
| Asset | `Panako-2.1-all.jar` (self-contained shadow jar) |
| Size | 6 431 377 bytes |
| **sha256** | `767cdd2cd0991658c4a25a0b8e887f9a2a38f69ae17781b02fe1652e1a7173d4` |
| URL | `github.com/JorenSix/Panako/releases/download/joss/Panako-2.1-all.jar` (over https) |
| Location | `data/local/panako/Panako-2.1-all.jar` (git-ignored) |
| Storage dir | `data/local/panako-db/<index-label>/` (git-ignored) |

`java -jar Panako-2.1-all.jar configuration` starts and prints its configuration (strategy
`OLAF`, 16 kHz, LMDB) — captured by `panako-setup` as `Panako starts: #Configration currently in
use:`.

## Two Windows realities (both handled; both essential to a working store/query)

1. **lmdbjava on JDK 21** needs `--add-opens java.base/java.nio=ALL-UNNAMED` (and `sun.nio.ch`)
   or `java.nio.Buffer.address` is inaccessible and the store never opens. The provider always
   passes these (harmless on JDK 11).
2. **Panako hardcodes a 1 TiB LMDB map size** (`setMapSize(1_099_511_627_776L)`). On Windows LMDB
   grows the data file to the full map size when it opens, which fails with *disk full* (Win32 112)
   on any disk < 1 TiB. Fix (`ensure_index_ready` → `sparse_extend`): let Panako write its valid
   initial meta pages (the open fails at the grow, leaving an 8 KiB valid `data.mdb`), then mark the
   file **sparse** and extend it to the map size via pywin32 `FSCTL_SET_SPARSE` + `SetEndOfFile`, so
   the reopen maps an already-large file **without consuming disk**. Confirmed: after indexing, free
   disk is unchanged. POSIX LMDB grows a sparse file natively, so this is a no-op off Windows.

Also required and set in `config.properties` (Panako reads it from beside its jar, at construction
— command-line overrides do not reach the decoder): the decoder pipe environment must be
**`cmd.exe /C`** on Windows (the packaged default `/bin/bash` does not exist). `OLAF_STORAGE=FILE`
was ruled out — its `processQueryQueue` is a no-op, so it cannot be queried; only LMDB (persistent)
or MEM (in-process) can, hence the LMDB workaround above.

## Live smoke — reference-pool recognition works on this machine now

Built a tiny index from the 3 Stage-2a synthetic references (`synthesize_test_sources`), then
queried a 15 s window cut from source 1 (`ffmpeg -ss 20 -t 15 -ar 16000 -ac 1`):

- store: `2920`, `2333`, `2901` fingerprints for synthetic-1/2/3 (Panako assigns each a numeric
  resource id; source 1's is `129284753`).
- query (Panako CSV row):
  `1 ; 1 ; query.wav ; 0.360 ; 14.496 ; synthetic-1.wav ; 129284753 ; 20.360 ; 34.496 ; 516 ;
  1.000 % ; 1.000 %; 1.00` — **a real Panako match** (score 516, time/freq factor 1.000).
- normalised observation: `status=match`, `mix_span_ms=(20360, 34496)`, `score_raw=516`,
  `anchor.mix_anchor_ms=20360`, `anchor.ref_anchor_ms=20360`, `time_factor_e4=10000`,
  `transform=null`. The `slow` end-to-end test reproduces this and passed (`1 passed`).

The same path drives the `build-index` CLI end-to-end: indexing 2 local files wrote the manifest
(`index_id`, `index_version`, `panako_version 2.1`, jar sha256) and **left no audio behind** (the
downloads dir is empty after fingerprinting).

## Candidate pool ("never auto-rips")

`id-detector build-index <set-url> [--extra-artist …] [--from-hints] [--extra-url …] [--file …]
[--index]`. Default discovery **prints a candidate list only** — the uploader's own uploads (set →
`uploader_url` → `/tracks`), artists named in the parsed hints and `--extra-artist` (`scsearch`),
and any `--extra-url`. Audio is downloaded and fingerprinted only for candidates confirmed with
`--index` or supplied explicitly (`--extra-url` / `--file`). Every downloaded/decoded file is
deleted immediately after fingerprinting (tested, including on a fingerprinting failure).

## Fusion + profile

Panako observations enter the existing fuser exactly like the scanner path (`transform=null`,
chunk-indexed logical trials, independent source, no cascading). Self-hosted/free → a **full**
independent trial, never the 0.5 commercial dependence prior. `max_accuracy` v2 enables Panako when
a JDK + built pool are present (`enabled_engines = ['shazam', 'panako']`); `free` and the v1
profiles are untouched.

## Verification (exact outputs)

- doctor JDK line:
  `Panako   PASS   JDK 21.0.12.1 at C:\Program Files\Eclipse Adoptium\jdk-21.0.12.101-hotspot\bin\java.exe`
- `uv run pytest -q` → `484 passed, 93 deselected, 1 warning in 54.64s`
- `uv run pytest -q -m slow -k end_to_end` (runs java+Panako) → `1 passed, 17 deselected`
- `uv run ruff check .` → `All checks passed!`
- fixture audit → `audited 344 files` / `failures: []`

## Notes / limits

- The Stage 8 acceptance gate ("stratum-2 recall +10 pp on ≥ 20 episodes, paired") needs a real
  reference pool and the dev-2 corpus, which do not exist here; this stage delivers the working
  provider + builder + fusion + profile and proves the mechanism end-to-end on synthetic audio.
- `max_accuracy-v2.json` is *derivable* (`derive_max_accuracy_v2`) but is intentionally **not**
  frozen to `profiles/` here — freezing it is an explicit operator step once a pool is built, and
  the committed profiles must stay unchanged.
- The pinned jar, its `config.properties`, and all index stores live under `data/local/` (git-
  ignored; explicit `.gitignore` entries added for `data/local/panako/`, `data/local/panako-db/`,
  `tools/panako/`). Nothing Panako-related is committed.

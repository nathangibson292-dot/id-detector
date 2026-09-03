# Stage 0 — Preflight and contracts

## What was built

### Project and runtime

- `pyproject.toml`, `uv.lock`: Python 3.12 uv project, `id-detector` console entry point,
  runtime dependencies, and pytest/ruff development dependencies. The compatible Shazam pair is
  pinned exactly at `shazamio==0.8.1` and `shazamio-core==1.1.2`.
- `.env.example`: names only for optional later-stage credentials; all values are blank.
- `README.md`: Stage 0 quick start and schema regeneration command.
- `src/id_detector/cli.py`, `doctor.py`: Typer CLI and pass/warn/fail preflight table. The doctor
  checks uv, Python 3.12, ffmpeg, ffprobe, the yt-dlp module, Node, both Shazam packages, offline
  signature generation, the Visual C++ runtime, and free disk. Timed-out child trees are cleaned
  up with repeated psutil enumeration, bounded waits, and a bounded post-kill pipe drain.

### Contracts and semantics

- `src/id_detector/contracts.py`: strict, closed Pydantic models for source, PCM, window, query,
  observation, hint, identity nodes/assertions/works/candidates and aggregate, episode, gap,
  durations, rescan request, episodes aggregate and certification, ground truth, benchmark report,
  invocation journal entry, raw-index entry, and provider config. Source keys, complete window
  transform records, query target/capability pairs, and provider measurement state receive semantic
  validation. Nested provider data and benchmark metrics reject floats. Nullable fields are
  required. Artefact models reject credential keys.
- `src/id_detector/io.py`: compact sorted UTF-8 JSON, Windows-safe atomic writes, SHA-256 helpers,
  completion-sidecar writer/verifier, and logging redaction.
- `src/id_detector/semantics.py`: transform algebra, proved bounds, interval union/subtraction,
  exact duration partition and gaps, Shazam offset aggregation, and conflict-aware identity merge.
- `src/id_detector/hints.py`: timestamp-by-component parsing and conservative track-question
  classification.
- `docs/schemas/*.schema.json`: 21 exported JSON Schemas. `docs/schemas/jobs.sql` contains the three
  plan tables and exact column sets. `docs/schemas/README.md` records natural keys and Stage 0
  fixed-point decisions.

### Tests and fixtures

- `tests/golden/*.json`: one validated example for each of the 21 exported record schemas.
- `tests/test_contracts.py`: schema currency, golden validation and cross-record recomputation,
  strict/explicit-null/no-float rules, complete transform vectors, query-union vectors, provider
  measurement consistency, and exact SQLite columns.
- `tests/test_semantics.py`: all requested semantic vectors, including the exact 50 percent Shazam
  cluster threshold, privileged identity sources, conflict ordering, timestamp traps, and
  adversarial same-label version cases.
- `tests/test_doctor.py`: exercises a timed-out command whose descendant inherits its output pipes,
  proving cleanup returns within a bound and terminates the descendant.
- `tests/test_fixture_audit.py`: runs the committed-corpus audit during every default pytest run and
  proves the clean-clone audit and fixture derivation properties without the raw corpus.
- `scripts/export_schemas.py`, `derive_fixtures.py`, `audit_fixtures.py`: reproducible schema export,
  privacy-minimising corpus derivation, and committed-file auditing.
- `data/fixtures/hints/synthetic/parsing_traps.json`: authored cases for every named hint trap and
  negative question cases.
- `data/fixtures/identities/adversarial_versions.json`: original/remix/edit/radio/extended/
  instrumental/remaster collision cases.
- `data/fixtures/hints/derived/`: 15 pseudonymous source-set files derived from the local raw corpus;
  author tokens are deterministic fixture-local sequences and filenames contain no platform ids.

## Review fixes

| Finding | Change | Regression test |
|---|---|---|
| P1 — deterministic/semantic goldens | Rebuilt linked goldens from one media key; corrected source, natural-key and cache hashes; added a second independent identity assertion; made proved bounds, evidence union, gaps and the 3,600,000 ms partition agree. | `test_golden_ids_keys_and_cross_record_references_are_deterministic`; `test_golden_identity_bounds_evidence_union_and_duration_partition_are_coherent` |
| P1 — invalid window spans/maps | `Transform` validates type parameters; `WindowRecord` validates output length, ordered support, transform span, exact rational map, uncertainty and the none-window logical trial. | `test_window_model_validates_transform_span_map_and_mapped_samples`; `test_window_model_rejects_contradictory_records` |
| P1 — query target shape | Replaced nullable three-field target with closed `{window_id}` / `{asset, asset_sha256}` union and enforced capability compatibility. | `test_query_target_union_accepts_both_branches_and_checks_capability` |
| P1 — false provider measurement | Golden is now `shazam-unmeasured.json`, with null outputs and no provenance; model and schema couple `measured` to outputs and evidence. | `test_provider_measurement_state_is_consistent` |
| P1 — privileged identity veto | Merge eligibility is now `(recording id and two sources) or privileged`, while early conflicts veto and late conflicts contest. | `test_privileged_identity_sources_merge_without_recording_ids_and_honor_conflicts` |
| P1 — question classification | Track context is limited to `track`, `tune`, or `song`; `id` is only the required marker alternative to `?`. | `test_hint_timestamp_respects_media_duration_and_question_negatives`; synthetic negative fixtures |
| P1 — clean-clone privacy audit | Added closed derived-record grammar, safe vocabulary, sequential author and filename checks, plus contextual rejection of opaque raw IDs; raw comparison remains an optional extra. | `test_derived_fixture_audit_is_effective_without_raw_data`; `test_fixture_audit_rejects_opaque_contextual_ids_without_raw_data` |
| P2 — four-part timestamps | Colon timestamps cannot begin or end next to another colon. | `test_hint_timestamp_component_rules`; synthetic four-component fixture |
| P2 — integer coercion | Enabled strict Pydantic validation while allowing only JSON-array-to-tuple conversion for spans. | `test_contract_integer_fields_are_strict` |
| P2 — nondeterministic derivation | Noise selection is stable and authors are allocated as `author_001`, `author_002`, and so on; truncation no longer cuts a safe token in half. | `test_fixture_derivation_is_byte_deterministic_with_sequential_authors` |
| P2 — doctor timeout cleanup | Descendants are enumerated repeatedly before root termination; all cleanup/drain waits are bounded. | `test_timed_out_command_cleans_descendants_without_unbounded_pipe_wait` |

## Disputed findings

None.

## Deferred

None.

## How to run it

From the repository root in PowerShell:

```powershell
uv sync --dev
uv run id-detector doctor
uv run python scripts/export_schemas.py
uv run python scripts/derive_fixtures.py
uv run python scripts/audit_fixtures.py
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

Live tests remain opt-in:

```powershell
uv run pytest -q -m live
```

## Verification

No live or network test was run. Signature generation was deliberately offline.

`uv run id-detector doctor`:

```text
uv                  PASS    uv 0.12.5
Python              PASS    3.12.14
ffmpeg              PASS    ffmpeg version 8.1.2-full_build
ffprobe             PASS    ffprobe version 8.1.2-full_build
yt-dlp module       PASS    2026.08.19
Node                PASS    v20.18.1
Shazam signature    PASS    shazamio 0.8.1, shazamio-core 1.1.2; 12 s WAV signed offline
Visual C++ runtime  PASS    vcruntime140 and vcruntime140_1 loadable
Free disk           PASS    491 GiB available
```

`uv run python scripts/derive_fixtures.py` key output after deterministic regeneration:

```text
derived 732 records from 15 source sets
```

An immediate second derivation was compared by SHA-256:

```text
fixture regeneration deterministic: 15 files unchanged
```

`uv run python scripts/audit_fixtures.py` after this report was added:

```text
audited 74 files
fixture audit passed
```

`uv run pytest -q`:

```text
........................................................................ [ 71%]
.............................                                            [100%]
101 passed in 2.02s
```

`uv run ruff check .`:

```text
All checks passed!
```

`uv run ruff format --check .`:

```text
30 files already formatted
```

`uv lock --check`:

```text
Resolved 49 packages in 1ms
```

## Known gaps

- This is deliberately only Stage 0. Ingestion, decode, window materialisation, providers, job
  execution, fusion, and the full hint parser begin in later stages.
- The provider-config golden is explicitly unmeasured. Stage 1 must write the first measured,
  immutable `shazam-v1.json` after real bias/insertion measurements.
- Visual C++ discovery is best-effort. Disk is a pass at 5 GiB or more, a warning from 1 to 5 GiB,
  and a failure below 1 GiB; revision 5 does not prescribe thresholds.
- Raw files that contain no comment records produce empty derived files so their source set remains
  auditable.
- The doctor uses bounded repeated psutil cleanup. The shared Windows Job Object launcher remains a
  Stage 1 deliverable, as assigned by the build order.

## Deviations from plan

- The instruction to reject handle/URL/identifier patterns in every historical file under `docs/`
  conflicts with keeping `docs/PLAN.md` unchanged and preserving the existing research and review
  documents, which intentionally contain citations, API endpoints, identifiers, and quoted mention
  examples. The audit reads every such file and still checks it against raw lines, but exempts only
  the pre-existing plan, research, review, and fixture-policy documents from those lexical pattern
  rules. Structured identifier checks still run wherever JSON can be parsed, and all derived hint
  fixtures receive the clean-clone grammar/vocabulary checks. All new schemas, stage reports,
  goldens, and fixture data receive the applicable full checks. No historical document was changed
  to hide those citations.

## What Stage 1 needs to know

- Import models and key/cache helpers from `id_detector.contracts`; do not duplicate contract
  shapes. Regenerate and test schemas after any versioned contract change.
- Use `id_detector.io` for every immutable artefact and completion sidecar. It rejects floats and
  credential-bearing keys before writing.
- Instantiate `docs/schemas/jobs.sql` unchanged, keep the single SQLite writer architecture, and
  interpret all plan-named USD columns as integer cents.
- Build the real Shazam config as a new immutable `shazam-v1.json`. The Stage 0
  `shazam-unmeasured.json` golden does not certify bias, uncertainty, or recognition latency.
- The doctor validates local signature generation only. Stage 1 still owns injected HTTP transport,
  retry ownership, fake-server physical-attempt accounting, and Windows Job Object launching.

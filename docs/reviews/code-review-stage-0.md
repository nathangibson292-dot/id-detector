### [P1] Goldens violate the deterministic and semantic contracts

**What:** The golden test checks only schema/model round-tripping. Several “valid” examples violate revision 5:

- `source_key` is not `sha256(canonical_url)`.
- Window/query IDs do not derive from their natural keys, and the query cache key does not match the required clip formula.
- The identities aggregate merges two recording nodes with only one independent assertion; the plan requires two.
- `episodes.json` reports `201000` evidence-supported milliseconds, but its two support intervals total only `24000`; `201000` is their hull, which the plan explicitly forbids.

**Where:** `tests/test_contracts.py:29`, `tests/golden/source.json:1`, `tests/golden/query.json:1`, `tests/golden/identities.json:1`, `tests/golden/episodes.json:1`

**Fix:** Generate internally coherent goldens and add tests that recompute every source key, natural-key ID, cache key, identity component, proved bound, evidence union, and duration partition.

### [P1] Window contracts accept invalid transform spans and sample maps

**What:** `Transform`, `SampleMap`, and `WindowRecord` enforce only primitive types. A resample window with `rate_e4=10800`, a 12,000 ms original support span, and an identity sample map validates successfully. Revision 5 requires `round(12000/r)` original spans and `a_num/a_den = 10000/rate_e4` for resample/tempo, while pitch must retain a 12,000 ms span and identity map. Reversed spans are also accepted.

The tests exercise `transform_spec()` in isolation, so they pass even though the exported artifact model accepts contradictory records.

**Where:** `src/id_detector/contracts.py:29`, `src/id_detector/contracts.py:118`, `src/id_detector/contracts.py:131`, `tests/test_semantics.py:40`

**Fix:** Add semantic validation or a mandatory validated factory for transform/type/rate/semitone, output duration, support span, and sample-map consistency. Add negative model tests and validate first/last mapped samples through a complete `WindowRecord`.

### [P1] Query target shape deviates from the plan’s union contract

**What:** The plan specifies `target {window_id} | {asset, asset_sha256}`. The implementation instead requires all three properties, with unused properties explicitly null. Consequently, a plan-conformant `{"window_id": ...}` is rejected, and query natural keys include extra null fields.

**Where:** `src/id_detector/contracts.py:146`, `docs/schemas/README.md:53`, `docs/schemas/query.schema.json:1`

**Fix:** Model two closed target objects and use their union. Add golden/vector coverage for both branches and capability/target compatibility.

### [P1] The illustrative provider config falsely claims a completed measurement

**What:** The golden declares `measured: true`, names itself `shazam-v1.json`, supplies bias/L-min values, and cites an insertion suite. The stage report admits no real bias/insertion measurement exists and says Stage 1 must create the first measured config. Revision 5 requires unmeasured anchors to remain unreliable and measured configurations to be immutable evidence-backed artifacts.

**Where:** `tests/golden/provider_config.json:1`, `docs/stage-reports/stage-0.md:133`, `docs/stage-reports/stage-0.md:158`

**Fix:** Mark this example unmeasured, null its measurement outputs, remove fictitious provenance, and reserve the real versioned filename for Stage 1. Validate consistency between `measured` and the measurement fields.

### [P1] Audited truth and held-reference identity merges are incorrectly vetoed

**What:** Eligibility is implemented as `has_recording_id AND (two sources OR privileged source)`. The plan requires `(recording ID AND two sources) OR aligned held reference OR audited truth`. Therefore, audited truth or alignment cannot merge two text/provider-neutral nodes even though those are independent permitted grounds.

**Where:** `src/id_detector/semantics.py:323`, `tests/test_semantics.py:179`

**Fix:** Change the condition to `(has_recording_id and corroborated) or privileged` and add vectors for both privileged source kinds, including conflicts before and after union.

### [P1] Question classification does not require a track keyword

**What:** `_TRACK_WORD` treats words such as `anyone` and `what's` as track keywords, while `id` satisfies both sides of the condition. Thus `"Anyone ?"` is classified as a track question. Revision 5 requires a track keyword and either `?` or standalone `id`; false questions later trigger unnecessary rescans.

**Where:** `src/id_detector/hints.py:9`, `src/id_detector/hints.py:43`, `tests/test_semantics.py:340`

**Fix:** Separate actual track-context keywords from the question/ID marker and add negative vectors such as `"Anyone?"`, `"What's this?"`, and other non-track uses.

### [P1] The clean-clone privacy audit cannot prove its claimed properties

**What:** When ignored raw data is absent, `_raw_fragments()` returns an empty set, making the verbatim-line check vacuous. Its platform-ID checks also detect only numeric forms, so opaque/alphanumeric platform IDs or ordinary raw lines without a URL/handle pass. The default pytest test therefore does not establish the plan’s clean-clone privacy policy.

The present local audit did pass with the raw corpus available; this finding concerns the claimed durable test guarantee.

**Where:** `scripts/audit_fixtures.py:16`, `scripts/audit_fixtures.py:53`, `tests/test_fixture_audit.py:4`

**Fix:** Add self-contained checks for the derived fixture grammar/vocabulary and reject non-null platform/user/comment/source identifier fields contextually. Keep raw comparison as an optional additional check, not the sole verbatim safeguard.

### [P2] Four-component timestamps are accepted as valid three-component timestamps

**What:** `1:02:03:04` is parsed as `1:02:03` because the regex does not exclude an adjacent colon. Revision 5 permits exactly two or three components.

**Where:** `src/id_detector/hints.py:7`, `src/id_detector/hints.py:18`, `tests/test_semantics.py:325`

**Fix:** Prevent matches adjacent to another colon and add malformed four-component vectors.

### [P2] Runtime models coerce non-integers into integer fields

**What:** The shared Pydantic configuration is not strict. For example, `output_ms="12000"` and `output_ms=true` validate as integers, diverging from the JSON Schema and weakening the integer-only contract.

**Where:** `src/id_detector/contracts.py:49`

**Fix:** Enable strict validation and add rejection tests for strings and booleans in integer fields.

### [P2] Fixture derivation is nondeterministic

**What:** Random noise selection and freshly random author tokens rewrite committed fixtures on every derivation. This makes regeneration noisy and undermines deterministic review while providing no extra privacy over fixture-local sequential pseudonyms.

**Where:** `scripts/derive_fixtures.py:15`, `scripts/derive_fixtures.py:181`, `scripts/derive_fixtures.py:185`

**Fix:** Select records deterministically and allocate sequential fixture-local author tokens that contain no handle-derived material.

### [P2] Doctor timeout cleanup can still hang or leave Windows descendants

**What:** The doctor enumerates descendants once, kills survivors without waiting again, and then calls unbounded `communicate()`. A newly spawned or handle-inheriting descendant can escape the enumeration and keep the pipe open. This is weaker than the plan’s Windows Job Object process-tree guarantee and the report’s cleanup claim.

**Where:** `src/id_detector/doctor.py:29`, `src/id_detector/doctor.py:59`, `docs/stage-reports/stage-0.md:14`

**Fix:** Use the shared Job Object launcher on Windows, or at minimum perform bounded post-kill waits and repeated descendant cleanup without an unbounded `communicate()`.

## Verified

- `uv run pytest -q` — exit 1 before pytest: `uv.exe` is an inaccessible WinGet symlink in this read-only sandbox.
- `uv run ruff check .` — exit 1 for the same `uv.exe` launch failure.
- `uv run id-detector doctor` — exit 1 for the same `uv.exe` launch failure.
- Local Ruff fallback: `All checks passed!`
- Read-only pytest fallback excluding the single `tmp_path` test: `80 passed, 1 deselected in 0.79s`; the full fallback could not obtain a writable temporary directory.
- Local doctor fallback ran, but reported failures for uv, ffmpeg, ffprobe, and Shazam temp-file creation because those host resources are unavailable inside this sandbox. Thus the stage report’s 81-pass and all-PASS doctor outputs were not reproducible here.
- Fixture audit during pytest: `audited 73 files`, passed with the local raw corpus present.

REVIEW VERDICT: FIX_FIRST
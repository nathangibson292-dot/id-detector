### [P1] Stage 2b’s named corpus deliverable is incomplete

**What:** `docs/PLAN.md:166-167,185` requires `dev-1` to be frozen and a full pseudonymous `dev-1` baseline committed. The implementation instead commits six draft sets and a one-set unverified smoke report. The stage report acknowledges this deviation, but that does not satisfy revision 5.

**Where:** `data/corpus/dev-1/corpus-version.json:1`; `docs/stage-reports/stage-2b.md:193-228`

**Fix:** Complete first-pass verification and freeze all six sets, then generate the full `dev-1` baseline. Alternatively, formally revise the plan before declaring Stage 2b complete.

### [P1] Episode proofs omit final matches

**What:** `docs/PLAN.md:103,144` requires proved bounds and `evidence_support_ms` to use every final match. The implementation first reduces observations to one hypothesis per logical trial and calculates episodes only from those selected observations. A focused probe produced an expected minimum support end of `10000` but emitted `start_no_later_than_ms=12000`.

Rejected hypotheses are also absent from `evidence`, while a global rejection flag is attached to every episode.

**Where:** `src/id_detector/fuse/episodes.py:245-266`, `src/id_detector/fuse/episodes.py:294-312`, `src/id_detector/fuse/episodes.py:390`

**Fix:** Use trial selection only for alignment fitting and voting. Retain all final matches for evidence unions and one-sided proofs, associating rejected hypotheses with the correct candidate and occurrence.

### [P1] Two providers asserting the same recording ID are not corroborated

**What:** `docs/PLAN.md:98` permits version support when a recording-specific identifier is asserted by at least two independent sources. The code instead requires at least two recording nodes in the merged component. Two providers both asserting only `isrc:I1` produce one node and remain unsupported; the focused probe confirmed `recording_supported=False`.

**Where:** `src/id_detector/fuse/identity.py:314-326`

**Fix:** Track independent source provenance per recording identifier/component and mark support when the same recording ID has two independent assertions, even if it is represented by one node.

### [P1] Conflict behavior depends on assertion hash ordering

**What:** `docs/PLAN.md:98` requires all known conflicts to veto a union; only a conflict genuinely discovered after a prior union should mark an existing component contested. The graph sorts assertions by ID before using an order-sensitive merge helper, so an arbitrary SHA-1 ordering decides whether identical evidence is “early” or “late.” The test explicitly simulates time by changing the conflict ID from all-zeroes to all-`f`.

Additionally, vetoed candidates receive a non-empty `conflicts` list while remaining non-contested, but the scorer rejects every such candidate.

**Where:** `src/id_detector/fuse/identity.py:202-207`, `src/id_detector/fuse/identity.py:251-286`; `tests/test_stage2b_fuser.py:103-128`; `src/id_detector/benchmark/scorer.py:142-154`

**Fix:** Apply all conflicts present in the current input as vetoes. Represent genuinely late discovery using prior-generation state or explicit provenance, not ID ordering. Permit separated, vetoed candidates to record conflicts without requiring `contested=true`.

### [P1] Alignment violates the outlier and replay predicates

**What:** `docs/PLAN.md:140` says an outlier must be inconsistent with both neighbours. The implementation uses outlier as the fallback whenever a valid three-point lookahead is unavailable. A shifted-intercept sequence with two mutually consistent new points was classified as two outliers instead of a jump/pending segment.

Likewise, a gap over 120 seconds bypasses continuation and then becomes replay merely because it exceeds 30 seconds, even if it remains reference-consistent; the plan requires reference inconsistency or recurrence.

**Where:** `src/id_detector/fuse/alignment.py:289-337`; `tests/test_stage2b_alignment.py:42-121`

**Fix:** Evaluate consistency with both adjacent points, retain pending points until enough evidence exists, and encode the replay predicate literally. Add negative and boundary tests around two-point transitions and 30/120-second gaps.

### [P1] The controlled baseline accepts arbitrary audio as the claimed source

**What:** `docs/PLAN.md:49,113` binds truth to the SHA-256 `media_key`. Corpus discovery selects media by filename without checking that hash or duration. The fake recognizer then constructs truth-derived responses for whatever window hashes it receives; its pipeline test deliberately uses random noise and still obtains truth matches. Consequently, stale, corrupt, or mislabeled media can still generate apparently perfect identification metrics.

**Where:** `src/id_detector/benchmark/corpus.py:319-337`; `src/id_detector/local_fixture.py:215-224`; `tests/test_stage2b_pipeline.py:74-90`

**Fix:** Verify source SHA-256 and decoded duration before recognition. Use a fixed recorded-response map keyed by expected window hashes, so mutated or unrelated content produces no match. Add a negative mutation test.

### [P1] Draft status is caller-controlled and can permit false accuracy claims

**What:** `docs/PLAN.md:16,166-170` requires claims and certification to rely on frozen, verified truth. `unverified_seed_comparison` defaults to `false`, is not required by the schema, and is copied from the prediction document rather than derived from loaded truth. `run_corpus` checks only `episode.draft`, not the freeze manifest. A direct score can therefore label draft/non-frozen truth as verified and potentially certify it.

**Where:** `src/id_detector/contracts.py:823-824`; `src/id_detector/benchmark/scorer.py:158-180`, `src/id_detector/benchmark/scorer.py:1135-1211`; `src/id_detector/benchmark/corpus.py:347-365`

**Fix:** Make the marker required, derive it from validated truth and manifest state, reject contradictory caller values, and prohibit certification whenever the corpus is unverified.

### [P1] Benchmark natural keys do not identify the executed configuration

**What:** `docs/PLAN.md:115` keys reports by `corpus_version ‖ profile ‖ config_hash`. The hash covers only a constant scoring snapshot; it omits provider/config version, fuser policy, schedule, budget, and `set_id`. The committed local-fixture and Shazam reports consequently share the same config hash, and partial/full runs of one corpus collide under the same natural key.

**Where:** `src/id_detector/benchmark/corpus.py:295-305`, `src/id_detector/benchmark/corpus.py:339-355`; `data/corpus/dev-1/unverified-seed-comparison-free.json:1`

**Fix:** Hash a complete canonical run configuration, including selected set population and every behavior-affecting provider/fuser/window setting. Alternatively, prevent partial runs from producing normal corpus report artifacts.

### [P1] Regression gates can pass without a paired population

**What:** `docs/PLAN.md:173` requires paired cluster-bootstrap non-inferiority. The implementation silently intersects set IDs, skips paired precision gates when the intersection is empty, and still compares aggregate recall from potentially different populations. Partial overlap also mixes paired precision with unpaired aggregate recall.

**Where:** `src/id_detector/benchmark/corpus.py:242-292`

**Fix:** Require identical expected set populations, profiles, and compatible report metadata. Fail the comparison when any pair is missing and calculate every regression metric from the same paired units.

### [P2] Best-point validation has a Stage 5 hole

**What:** `docs/PLAN.md:103` requires a best point to equal the PI centre when calibrated and otherwise equal its proved bound. The validator checks the bound only when the PI is `None`; an uncalibrated non-null PI permits any best point, and calibrated PIs are never checked against their centres.

**Where:** `src/id_detector/benchmark/scorer.py:119-124`

**Fix:** Branch on `pi.calibrated`, enforce the defined integer centre for calibrated PIs, and enforce the proved bound for absent or uncalibrated PIs.

### [P2] The tests overstate the properties they cover

**What:** The event tests contain one positive sequence per label but no adversarial neighbour or threshold cases, so they miss the alignment failures above. The corpus test merely parses the committed baseline and asserts its stored numbers; it does not regenerate it. The pipeline test uses truth-labelled random noise, so it cannot verify content-bound recorded responses.

**Where:** `tests/test_stage2b_alignment.py:42-121`; `tests/test_stage2b_corpus.py:28-50`; `tests/test_stage2b_pipeline.py:74-142`

**Fix:** Add false-positive/threshold vectors, regenerate a small committed controlled baseline during testing, and assert that unknown or mutated content cannot receive recorded responses.

### [P2] Long episodes never produce the required rescan trigger

**What:** `docs/PLAN.md:155-156` includes episodes longer than 12 minutes among rescan triggers. Generation zero emits only gap, contested, and edge requests.

**Where:** `src/id_detector/fuse/episodes.py:500-523`

**Fix:** Emit deterministic `long_episode` requests for episode hulls exceeding 12 minutes and add a semantic vector for the natural key and policy.

## Verified

- `uv run pytest -q`: did not start; exit 1 because the external WinGet `uv.exe` symlink was unavailable (“No application is associated with the specified file”).
- `uv run ruff check .`: same `uv.exe` failure.
- `uv run id-detector doctor`: same `uv.exe` failure.
- Supplementary direct checks: `.venv\Scripts\ruff.exe check .` reported `All checks passed!`; 198/199 tests collected with one deselected; 14 non-writing Stage 2b tests passed. A full direct pytest run was blocked by the read-only sandbox having no usable temporary directory.
- Direct doctor found Python/Node/VC++ but could not find `uv`, `ffmpeg`, or `ffprobe` on the sandbox PATH and could not create its signature-test temporary file.
- Privacy audit: `audited 145 files`, `fixture audit passed`.
- Both corpus manifests had zero hash mismatches; all six available `dev-1` media hashes matched their truth records.
- All 35 corpus JSON files parsed, validated, and contained no floating-point values. `git diff --check` passed.

REVIEW VERDICT: FIX_FIRST
### [P1] Real-set run shows episode fragmentation: one continuous track becomes many episodes

What: On the live dev-1 run (`data/local/work-dev1-live/**/fuse/episodes.json`), the same candidate is emitted as consecutive episodes roughly every 45 s with `occurrence_index` climbing 0 → 1 → 2 → 3 (e.g. candidate `ab5975e1…` at start≤255 s, 300 s, 345 s, 399 s, each with a single merged support interval), and the tracklist export repeats the same track as successive "incoming" rows (Flume at 4:15, 5:00, 5:45, 6:39). Mix gaps between those episodes are far below the 30 s replay threshold and well within the 120 s continuation window, so either the reference-consistency test is failing for consistent anchors (check the residual computation against `anchor.mix_anchor_ms`/`ref_anchor_ms` and the initial rate hypothesis), or the replay predicate fires without the "ref inconsistent AND gap > 30 s" conjunction the plan requires. Consequence: `T` never accumulates, so tiers stay low, and `occurrence_index` is meaningless.

Where: `src/id_detector/fuse/alignment.py` (segment growth / replay predicate), `src/id_detector/fuse/episodes.py` (episode chaining), live artefact above.

Fix: Reproduce with the cached observations of the live run (`id-detector analyse <same url>` re-fuses from cache with zero network calls). Add a regression test built from an anonymised excerpt of those observations (mix/ref anchor pairs only — no labels needed) asserting one episode per continuous playback. After the fix, report before/after: episode count, distribution of `T`, and the tracklist row count for that set.

### [P1] Badge/durations rule made every row UNCLEAR — apply plan rev 5.1

What: `badge = min(work, version)` and `durations` keyed off `badge` produced `evidence_supported_ms: 0`, `unclear_ms: 1,749,000` and an all-UNCLEAR tracklist on the real set, because the version tier can never exceed `unclear` with a single engine (recording-specific corroboration from ≥ 2 sources is impossible). `docs/PLAN.md` has been amended (rev 5.1, see the episode contract and Decisions log): `badge = tiers.work` capped at `likely` unless `tiers.version == verified`; a new `version_status verified|unverified|contested` field is displayed alongside; `durations` and exports key off `badge`.

Where: `src/id_detector/fuse/episodes.py` (badge), `src/id_detector/present.py` (exports), `src/id_detector/contracts.py` + schema/golden for `version_status`, `semantics.py` durations partition inputs.

Fix: Implement rev 5.1 exactly; update schema, golden, and the durations/partition tests; the tracklist export must show the badge and the version status in separate columns. Re-run on the cached live set and report the new tier distribution and `durations`.

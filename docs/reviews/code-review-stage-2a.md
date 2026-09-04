### [P1] “Proved” bounds are neither derived from nor validated against evidence

What: The scorer trusts caller-supplied bounds. The committed generator derives them from truth instead of evidence, so all 28 prediction episodes violate the required formula while the report records zero violations. For example, support `[1700,3700]` requires `start_no_later_than_ms=3700` and `end_no_earlier_than_ms=1700`, but the fixture supplies `1200` and `4200`.

Where: [scorer.py:48](C:/Users/natha/Documents/Music/id-detector/src/id_detector/benchmark/scorer.py:48), [scorer.py:641](C:/Users/natha/Documents/Music/id-detector/src/id_detector/benchmark/scorer.py:641), [make_controlled_predictions.py:32](C:/Users/natha/Documents/Music/id-detector/scripts/make_controlled_predictions.py:32). The plan requires `min(support end)` and `max(support start)` at [PLAN.md:103](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:103) and [PLAN.md:143](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:143).

Fix: Derive these fields from `evidence_support_ms`, reject mismatches at input validation, regenerate predictions/report, and add a negative vector proving forged bounds fail.

### [P1] Exact-version scoring bypasses recording identity and conflict veto

What: Work equivalence is only normalized text, while exact equivalence accepts a match in any shared namespace and ignores contradictory IDs. I confirmed that matching `mb_work` IDs with different qualifiers returns exact-equivalent, as does a shared ISRC accompanied by conflicting `mb_recording` IDs. This can credit a remix/original conflict as exact.

Where: [scorer.py:225](C:/Users/natha/Documents/Music/id-detector/src/id_detector/benchmark/scorer.py:225) and [scorer.py:246](C:/Users/natha/Documents/Music/id-detector/src/id_detector/benchmark/scorer.py:246). The plan requires separate work/recording components, recording-specific corroboration, and an absolute conflict veto at [PLAN.md:95](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:95)-[PLAN.md:98](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:98).

Fix: Score resolved canonical work and recording identities from the identity graph. Restrict exact equivalence to recording-specific identifiers and reject or mark unclear any contested/conflicting component.

### [P1] Certification can be issued against unregistered thresholds and a false config key

What: Thresholds are hard-coded once per tier as 80/95/99%, ignoring the required profile × dimension preregistration. The stage report acknowledges that revision 5 states no targets, yet the code can still emit `certified`. Additionally, when `config_hash` is omitted—as in the checked-in predictions—it hashes the entire predictions document, including results, seed, and cost, rather than an immutable configuration. That breaks the report’s natural key.

Where: [scorer.py:45](C:/Users/natha/Documents/Music/id-detector/src/id_detector/benchmark/scorer.py:45), [scorer.py:947](C:/Users/natha/Documents/Music/id-detector/src/id_detector/benchmark/scorer.py:947), and [scorer.py:977](C:/Users/natha/Documents/Music/id-detector/src/id_detector/benchmark/scorer.py:977). The plan requires preregistration per profile/dimension/tier at [PLAN.md:169](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:169), and defines the report key as corpus/profile/config at [PLAN.md:115](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:115).

Fix: Require a versioned preregistration/config snapshot, verify its hash, look up targets by profile × dimension × tier, and leave status provisional when no matching registration exists.

### [P1] Work-only truth cannot be frozen and is scored as a version failure

What: Freezing rejects every episode whose exact version is unknown. The contract deliberately separates work truth from `version_verified`; evidence is required when claiming an exact version, not for every valid work annotation. The scorer then includes every prediction in the version-precision denominator while only verified truth can be correct. I confirmed a correct work prediction against `version_verified=false` scores zero version precision instead of being excluded as unevaluable.

Where: [truth.py:394](C:/Users/natha/Documents/Music/id-detector/src/id_detector/truth.py:394) and [scorer.py:602](C:/Users/natha/Documents/Music/id-detector/src/id_detector/benchmark/scorer.py:602). See the separate work/version contract at [PLAN.md:112](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:112) and [PLAN.md:169](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:169).

Fix: Permit frozen work-level episodes with `version_verified=false`; exclude those truth episodes and associated predictions from exact-version metrics and certification.

### [P1] The truth workflow can freeze biased and non-independent annotations

What: First-pass verification can only accept/reject seeded entries and adjust boundaries. It cannot correct identity, add missed tracks, edit version IDs, annotate regions, overlaps, or time-varying roles; accepted episodes are overwritten with one `dominant` span. The second pass may use the same annotator reference as the first, and that annotator resolves disagreements by choosing “first” or “second.” Freeze checks only that a second-pass reference exists. This does not establish the independent, third-resolved test truth required by the plan and makes a hint-seeded corpus incapable of discovering omitted tracks.

Mixed timed/untimed seeds are also unsafe: untimed entries receive `index * duration / count` after sorting, which can place them before a late timed cue and construct a backwards episode.

Where: [truth.py:124](C:/Users/natha/Documents/Music/id-detector/src/id_detector/truth.py:124), [truth.py:241](C:/Users/natha/Documents/Music/id-detector/src/id_detector/truth.py:241), [truth.py:302](C:/Users/natha/Documents/Music/id-detector/src/id_detector/truth.py:302), and [truth.py:396](C:/Users/natha/Documents/Music/id-detector/src/id_detector/truth.py:396). The plan requires blinded second pass and third-annotator disagreement resolution at [PLAN.md:166](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:166)-[PLAN.md:167](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:167).

Fix: Store independent passes separately, require distinct annotator refs, record a third resolver, and enforce these at freeze. First pass must support adding/editing episodes, roles, overlaps, regions, and versions. Interpolate or explicitly request missing cue positions rather than deriving them from the sorted index.

### [P1] Ground-truth validation accepts impossible timelines

What: Episode ranges and unknown regions are only pairs of non-negative integers; ordering and media-duration bounds are not validated. I confirmed `GroundTruthRecord` accepts `start_ms_range=[2000,1000]` and an unresolved region `[5000,4000]`. Freeze does not add semantic validation, while the scorer assumes ordered ranges.

Where: [contracts.py:573](C:/Users/natha/Documents/Music/id-detector/src/id_detector/contracts.py:573), [contracts.py:608](C:/Users/natha/Documents/Music/id-detector/src/id_detector/contracts.py:608), and [truth.py:386](C:/Users/natha/Documents/Music/id-detector/src/id_detector/truth.py:386). Stage 2a requires controlled truth to validate at [PLAN.md:184](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:184).

Fix: Add record-level validation for ordered ranges/regions, bounds within `duration_ms`, coherent episode start/end, valid symmetric overlap indexes, roles within audible spans, and occurrence-index uniqueness.

### [P2] `out_of_pool` scoring contradicts the report’s binary policy

What: The report says this is a binary “some ID emitted” target, but the scorer accumulates milliseconds. Emitting an ID during only part of a region therefore produces partial recall, and the implementation has no path to an unknown-region false positive, making its precision largely meaningless.

Where: [scorer.py:388](C:/Users/natha/Documents/Music/id-detector/src/id_detector/benchmark/scorer.py:388) and [stage-2a.md:142](C:/Users/natha/Documents/Music/id-detector/docs/stage-reports/stage-2a.md:142).

Fix: Score one target per `out_of_pool` region, matched when any eligible ID support intersects it, and define/count false-positive unknown emissions explicitly.

### [P2] Controlled tests do not verify that transformations happened

What: Tests inspect manifest parameter labels and final WAV duration, but never measure pitch, tempo, coupled/resample mapping, source span, or mapped samples. Removing the actual transform filters while retaining manifest fields would still pass. The generator also calls the plan’s `resample` case `coupled`, complicating later contract integration.

Where: [test_stage2a_controlled.py:60](C:/Users/natha/Documents/Music/id-detector/tests/test_stage2a_controlled.py:60) and [controlled.py:254](C:/Users/natha/Documents/Music/id-detector/src/id_detector/benchmark/controlled.py:254). The exact `12000/r` and rational mapping requirements are at [PLAN.md:126](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:126)-[PLAN.md:135](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:135).

Fix: Add impulse/chirp insertion vectors that verify source span, first/last mapped samples, rate, pitch, and output sample count for every factor; use the contract name `resample`.

### [P2] Controlled rendering bypasses the project’s Windows-safe path and transactional-write handling

What: FFmpeg paths, `os.replace`, and cleanup use ordinary paths rather than the existing extended-path helper. Long Windows output paths can therefore fail. Rendering also writes directly into an existing corpus directory, so cancellation or a changed-seed rerun can leave new files under an old manifest or stale sets.

Where: [controlled.py:89](C:/Users/natha/Documents/Music/id-detector/src/id_detector/benchmark/controlled.py:89), [controlled.py:128](C:/Users/natha/Documents/Music/id-detector/src/id_detector/benchmark/controlled.py:128), and [controlled.py:480](C:/Users/natha/Documents/Music/id-detector/src/id_detector/benchmark/controlled.py:480). The plan requires Windows-safe paths and atomic artifacts at [PLAN.md:53](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:53) and [PLAN.md:158](C:/Users/natha/Documents/Music/id-detector/docs/PLAN.md:158).

Fix: Use the common native-path helpers throughout, render into a validated sibling staging directory, and publish the completed corpus/manifest atomically without retaining stale sets.

## Verified

- Inspected all 37 untracked files; `git diff --check` produced no findings.
- Each requested `uv run …` command failed before project execution: PowerShell reported `uv.exe` could not run because no application was associated with the file. The visible WinGet `uv.exe` is a symlink whose target is inaccessible in this sandbox.
- `.venv\Scripts\ruff.exe check .`: `All checks passed!`
- `.venv\Scripts\python.exe -m pytest -q`: failed before collection because the read-only environment has no usable temporary directory. Four no-temp Stage 2a tests run with `-s` passed (`4 passed, 1 warning`). Thus the report’s `149 passed` result was not reproduced here.
- `.venv\Scripts\id-detector.exe doctor`: exit 1; `uv`, `ffmpeg`, `ffprobe`, and Shazam signature checks failed in this environment. Python, yt-dlp, Node, VC++ runtime, and disk checks passed.
- Direct fixture audit: `audited 160 files` / `fixture audit passed`, matching the report. Direct model validation accepted all 25 controlled truth files and the 25-set benchmark report.

REVIEW VERDICT: FIX_FIRST
### [P1] Rendered controlled audio lives under data/fixtures/ and is silently git-ignored

What: `data/fixtures/controlled/stage-2a/**` contains 53 rendered `.wav` files (~37 MB) alongside 28 JSON truth/manifest files. `.gitignore` ignores `*.wav` globally, so only the JSON would be committed. Any test or command that reads those WAVs passes locally but fails on a clean clone; conversely, committing 37 MB of generated audio would be wrong.

Where: `data/fixtures/controlled/stage-2a/*/mix.wav`, `stem-*.wav`; `.gitignore:28`

Fix: Rendered audio must not live under `data/fixtures/`. Render into `data/local/controlled/<corpus_version>/` (git-ignored) or a pytest `tmp_path`; keep only the deterministic render manifest + truth JSON committed, and make every test that needs audio regenerate it from the seed (the renderer is deterministic) rather than reading committed files. Add a test asserting no audio file exists under `data/fixtures/` and that the committed manifest's hashes match a fresh regeneration.

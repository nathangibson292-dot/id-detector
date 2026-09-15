## A — Audit of the fixer’s verdicts

The change is not ready: two realistic P0 certification/corpus-safety holes remain.

- The record marks every original retro finding reproducible; none was dismissed as “not reproducible.” Static inspection supports those verdicts.
- The two “already refused” variants are genuine:
  - A set swapped into `work/` is rejected by the pinned-directory final-location check.
  - A linked draft-manifest root is rejected before enumeration or manifest reading.
- The regression tests for the original findings are structurally revert-sensitive. The exact link-refusal assertion is restored, and the two-process ledger barrier is inside the read/modify/write window and deterministic.
- Exact-depth handling is present for seed, verify, second pass, resolution, freeze, draft manifest, ledger append, review save/reveal, and controlled corpus output. `open_corpus` performs ancestry refusal before `path_key`, locking, or manifest reading and repeats it under the mutation lock.
- Current `release-1`, `dev-1`, controlled corpora, and ordinary pytest fixture layouts do not trigger the ancestor rule. However, any ordinary ancestor such as Downloads containing `<child>/ground_truth.json` is treated as a corpus and blocks deeper corpora. That is a conservative P2 usability false positive inherent in the current filename-only definition.
- The controlled marker is a regular non-link file and is bound to target set names and truth hashes. `release-1` has no such marker and cannot accidentally pass the JSON-output check. Successful same-target re-rendering and byte determinism are covered.
- The default calibration report is under git-ignored `data/local/calibration/`.
- Missing and surplus on-disk sets are rejected by `require_frozen_inventory`, but caller-subset certification remains open as described below.
- All 32 listed entry points reach `open_corpus`; repository-wide inspection found no present corpus reader outside the gateway. The literal guard has only three CLI-help allowlist entries.
- Both committed frozen manifests passed an independent read-only static check: exact inventory, every truth SHA-256, corpus version, and required verified fields matched for `controlled-synth-1` (25 sets) and `controlled-events-1` (145 sets).

## B — Correctness bugs in the fix

1. **P0 — `--audio-out` can replace the real corpus.** `src/id_detector/benchmark/controlled.py:685-688` resolves `audio_dir` without link or corpus containment checks; only `out_dir` enters the gateway at `:695`. `_publish_directory` then renames and deletes any existing target at `:645-661`, invoked for audio at `:842`. For example, a valid new `--out` combined with `--audio-out data/corpus/release-1` replaces the entire hand corpus with rendered audio and deletes the backup. A symlink resolving there works too. The owner-facing option is at `src/id_detector/cli.py:1254-1277`.

2. **P0 — an exposed frozen sibling can be omitted from an L3 claim.** `scripts/score_corpus.py:425-441` checks each truth file separately; `score_mix` preserves that per-file scope at `:807-818`, and `score_run_list` declares the selected subset certifiable at `:1034-1056`. In `src/id_detector/benchmark/scorer.py:1349-1359` the on-disk corpus inventory is checked, but `_truth_candidates` loads only the named record; `corpus_independent` likewise considers only that candidate and the caller’s subset at `:1486-1507`. A frozen 11-set corpus with one exposed set can therefore produce a certifiable ten-set L3 report simply by omitting the exposed set from the run list.

3. **P1 — post-freeze ledger evidence is not manifest-bound.** `src/id_detector/truth.py:511-516` permits a ledger append on a frozen corpus. Verification checks only that manifest-recorded ledger digests remain present, not that no new digest appeared (`src/id_detector/benchmark/scorer.py:1402-1418`). While the new line exists, independence fails; deleting it later restores verified/independent status because the manifest never recorded it. This regresses the prior frozen-terminal rule. The real review route currently refuses a frozen reveal before predictions (`src/id_detector/truth_review.py:507`), but the production ledger entry point itself violates the combined contract.

4. **P1 — the acknowledged overlapping-seed race remains.** `seed_truth` locks the inferred corpus root at `src/id_detector/truth.py:191-194`. Two mistyped paths whose roots are parent/child take different locks. Both can finish the final checks at `:346-359` before either create-only write at `:362`, leaving both `C/a/ground_truth.json` and `C/a/b/ground_truth.json`. This is a two-tab accident explicitly inside the threat model, not a hostile-program scenario.

5. **P1 — calibration scratch is not guaranteed outside the configured work root.** `_score_certification` discards `work_root` and uses the process-wide temporary directory at `src/id_detector/calibrate/validate.py:488-518`. The CLI permits an arbitrary `--work-root` at `src/id_detector/cli.py:1483-1497`. If that root is the system temporary directory, the scratch corpus is inside it. The regression test explicitly asserts that `_score_certification` has no `work_root` parameter, so it cannot verify the stated invariant.

6. **P2 — namespace spelling is not canonical at the ledger pin.** `src/id_detector/truth.py:514-516` passes `normcase(str(corpus_dir))` as `expected_key`, while the opened Windows directory is returned prefix-free. A valid `\\?\C:\...` truth path can therefore be rejected during reveal even though containment and lock identity use canonical `path_key`.

7. **P2, adversarial — duplicate marker entries satisfy the claimed schema.** `src/id_detector/benchmark/controlled.py:727-742` checks list length, then collapses entries into a dictionary. Two identical entries produce `set_count == 2` but only one effective set and can still authorize replacement.

8. **P2, adversarial — programmatic `model_out` is unguarded.** `src/id_detector/calibrate/validate.py:407-411` writes the model and completion sidecar without `refuse_write_inside_corpus`. The CLI does not expose this parameter, but a direct caller can target a truth or manifest path.

## C — Regressions

- The post-freeze ledger relaxation is a regression from the rounds 2–6 frozen-terminal guarantee.
- No static regression was found in role preservation, bulk-offset role endpoints/clamping/undo, crash recovery, mixed-annotator refusal, or the audit’s furniture handling. `*NSYNC`, `-M-`, and Unicode dash/minus forms without marker-separating whitespace remain accepted.
- Truth-review remains loopback-bound through `LoopbackServer`; POST mutations retain Host/Origin middleware and CSRF, GET/HEAD are non-mutating, audio uses one exact media-key route, and exposure precedes prediction assembly.
- No change was made to `idea.cmd`, the 4a-ii infrastructure, `io.py`, `jobs.py`, `retention.py`, or `tests/test_phase2b_retention.py`.

## D — Test quality

- I could not run `uv`, pytest, or the full fixture audit in this read-only sandbox. A direct Python import was also unavailable because this environment lacks `pydantic`; execution claims are therefore limited to the fixer’s record.
- Read-only Git, source inspection, JSON parsing, and SHA-256 checks were run.
- Missing regressions:
  - controlled `audio_dir` targeting or linking into a real corpus;
  - an L3 run list omitting an exposed frozen sibling;
  - frozen ledger append followed by deletion;
  - both overlapping seeds paused after their final checks;
  - scratch containment relative to the actual configured `work_root`;
  - namespace-prefixed ledger/reveal;
  - duplicate marker set IDs.
- The exact directory-link assertion at `tests/test_truth_review.py:786` is restored.
- The successful same-target controlled re-render at `tests/test_stage2a_controlled.py:41-47` is followed by byte comparison against an independent identical render.
- The 32-entry gateway test proves the seam is reached, but not that each caller supplies the correct `work_root`, mutation mode, or complete certification population.

## E — Scope

- `git status --short -- data work` is empty: no corpus or work-tree files were changed.
- The prohibited second-session paths, `tests/test_phase0a_security.py`, `io.py`, `jobs.py`, `retention.py`, and `tests/test_phase2b_retention.py` are untouched.
- No dependency or lockfile changed; `git diff --check` is clean.
- The golden truth schema sample was renamed rather than semantically weakened.
- Every untracked file was inspected. No live provider, real URL, or `idea gc` operation was invoked.

## Required fixes

### Realistic

1. **P0:** Validate and pin `audio_dir` before staging and again before publication; reject links, any path inside/over a corpus, and replacement of an audio directory lacking a renderer-owned marker. Add a test proving `release-1` remains byte-identical.
2. **P0:** Before setting L3 `certifiable`, require all run-list truth paths to belong to one frozen corpus and exactly equal its manifest inventory; evaluate independence across that complete inventory.
3. **P1:** Restore frozen refusal for ledger appends and keep frozen review refusal before prediction assembly; also require exact manifest/current ledger equality during verification.
4. **P1:** Serialize seed creation using a lock keyed by the common nearest existing ancestor, and add a deterministic barrier with both overlapping seeds past their final validation.
5. **P1:** Pass `work_root` into `_score_certification` and validate the scratch destination is outside it and every corpus before creating any file.
6. **P2:** Use `path_key(corpus_dir)` for the ledger pin identity and test ordinary versus `\\?\` spellings through the full reveal path.

### Adversarial

7. **P2:** Validate controlled markers with a typed/full schema and reject duplicate `set_id` entries before building the dictionary.
8. **P2:** Apply corpus/link destination guards to `model_out` and its completion sidecar immediately before both writes.

VERDICT: FIX_FIRST
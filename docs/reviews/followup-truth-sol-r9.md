## A — Audit of the fixer's verdicts

- **B1, certification moratorium: only partially fixed.** `idea truth freeze`, the generic scorer, `idea benchmark score`, `idea benchmark certify`, manifest `certifiable`, and calibration validation are gated correctly. No environment variable, config option, or CLI flag opens `CERTIFICATION_ENABLED`; only the pytest fixture monkeypatches it. The benchmark schema’s added status value matches the Pydantic contract.
- However, production paths still emit forbidden positive claims; see B1.
- **B2, output protection: only partially fixed.** The six named scorer/report modules validate their principal outputs before work and again before publication. Several other production writers remain unguarded; see B2.
- **B3, bounded ancestry: only partially fixed.** Existing real corpus paths require only fixed-name ancestor probes and remain fast. New-root parent listing is capped, but can occur twice, and the recorded manifest-less-corpus gap is realistic; see B3–B4.
- **B4, `_guard_test_version`: fixed.** The direct test calls the guard without opening certification and would fail if duplicate rejection were removed.
- Round 9 marked no finding “not reproducible” or “already fixed.” Round-8 adversarial P2s 5 and 6 remain accepted and were not reopened.

I could not execute `uv`, pytest, or the project verifier in this read-only sandbox.

## B — Correctness bugs in the fix

1. **Realistic P0 — the moratorium still permits positive certification claims.**

   - [scripts/score_corpus.py:1036](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/scripts/score_corpus.py:1036) can emit `"thresholds_met": true` while certification is disabled, and [scripts/score_corpus.py:1247](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/scripts/score_corpus.py:1247) prints “all three thresholds are met.” Appending the disabled message later does not retract the prohibited claim.
   - [src/id_detector/enrich/benchmark.py:139](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/enrich/benchmark.py:139) emits `"status": "certified"` for a passing link sample. Public `idea benchmark links-score` reaches it at [src/id_detector/cli.py:1638](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/cli.py:1638) without consulting the gate.
   - `idea benchmark freeze-profiles` emits `certified: true` feature claims from [src/id_detector/profiles.py:260](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/profiles.py:260), `:284`, `:329`, and `:378`.
   - [src/id_detector/fuse/episodes.py:581](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/fuse/episodes.py:581) also propagates `"certified"` from any loaded calibration model without checking the gate. No current real-mix model activates this path, but it is structurally ungated.

2. **Realistic P0 — unguarded public writers can still overwrite corpus truth.**

   The guard audit covers only six hard-coded modules. Concrete ungated destructive paths include:

   - `idea config init --force --path <set>/ground_truth.json`, which truncates the truth at [src/id_detector/cli.py:381](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/cli.py:381).
   - `idea benchmark links --out <set>/ground_truth.json` and `links-score --out ...`, which replace it at [src/id_detector/cli.py:1630](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/cli.py:1630) and [src/id_detector/cli.py:1648](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/cli.py:1648).
   - `idea benchmark hints --out ...`, whose writer is unguarded at [src/id_detector/benchmark/hints.py:149](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/benchmark/hints.py:149).
   - [scripts/make_controlled_predictions.py:154](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/scripts/make_controlled_predictions.py:154), which accepts an arbitrary positional output.
   - Calibration validation and profile freezing still use weaker or no centralized protection at [src/id_detector/calibrate/validate.py:494](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/calibrate/validate.py:494) and [src/id_detector/profiles.py:659](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/profiles.py:659).

3. **Realistic P1 — the recorded manifest-less-corpus gap is a plausible owner workflow.**

   An initial seed to `release-1/new-group/new-set/ground_truth.json` is refused because `new-group` is absent, with instructions at [src/id_detector/truth.py:204](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/truth.py:204) to create that directory. If the owner follows those instructions and retries, `creating` is false, the parent `release-1` is never listed, and nested truth is accepted. The next normal open of `release-1` then rejects its newly corrupted layout. This is realistic, not merely hostile-local-program behavior.

4. **Realistic P1 — a new corpus root’s parent is listed twice.**

   [src/id_detector/truth.py:1313](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/truth.py:1313) performs the creating-root check before locking; [src/id_detector/truth.py:1350](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/truth.py:1350) repeats it with the captured `creating=True`. A fresh controlled-render destination therefore scans its parent twice, contrary to the claimed one-listing bound.

## C — Regressions

- Existing `release-1` review and verify flows remain unaffected; only seeding into a missing root changed.
- Round 9 did not modify `src/idea_web/truth_review.py` or `idea.cmd`; the loopback, Host/Origin, CSRF, GET immutability, traversal, exposure-before-response, and locking protections remain as confirmed in round 8.
- The golden truth sample was renamed without weakening schema/model validation. No material 4a-ii or double-click regression was found.

## D — Test quality

- The direct duplicate-version regression is sound.
- The gate tests cover freeze, generic scoring, CLI score/certify, and `certifiable`, but never assert that `thresholds_met` cannot be true or that the printed “all three thresholds” sentence disappears.
- The output heuristic at [tests/test_truth_gateway_guard.py:120](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/tests/test_truth_gateway_guard.py:120) is not a meaningful repository-wide guarantee: it scans only six modules, omits names such as `path`, `out_dir`, `report_path`, and `validation_path`, and accepts one guard call anywhere in a function.
- Removing the final pre-write guard from corpus, ablations, schedule, or shortlist would leave that heuristic passing; only the scorer has a direct race-regression test. Under the requested rule, that is a P1 test gap.
- No test counts parent listings, and no test retries seed after creating the directory named by the missing-root error.
- Changed existing assertions reflect the moratorium or stronger seed contract; I found no unrelated assertion weakening.

## E — Scope

- `git status --short -- data/corpus work` is empty.
- `io.py`, `jobs.py`, `retention.py`, `tests/test_phase2b_retention.py`, all second-session files, `idea.cmd`, and `tests/test_phase0a_security.py` are untouched.
- No dependency or lockfile changed. `git diff --check` is clean.
- Every untracked file was inspected.
- Read-only inventory and SHA-256 recomputation found no missing, surplus, or hash-mismatched truth/annotation entry in `controlled-synth-1` or `controlled-events-1`. This is not a claim that I executed the project verifier.
- The round-8 furniture implementation and real-corpus result are unchanged.

## Required fixes

### Realistic

1. **P0:** Route every positive certification emitter through the shared gate: suppress/null `thresholds_met` and its printed success sentence, gate link-score status, profile `certified` flags, and calibrated episode certification propagation.
2. **P0:** Apply `refuse_generated_output` at initial validation and immediately before every write for all user-selectable production destinations, including config init, links, links-score, hints, calibration, profiles, and output-taking scripts.
3. **P1:** Treat an existing but empty/unmarked seed root as a new corpus root, bounded-check its parent once, and refuse the `release-1/new-group/...` retry scenario.
4. **P1:** Perform the creating-root parent listing only once per mutation, preferably under the corpus lock.
5. **P1:** Replace the six-module/name-based output heuristic with writer-aware coverage, plus direct tests proving every final pre-write revalidation fails when removed.

### Adversarial

None beyond the explicitly accepted round-8 P2 residuals 5 and 6.

VERDICT: FIX_FIRST
## A — Audit of the fixer's verdicts

I independently inspected the record, retro review, round-7 review, all tracked changes, and every untracked file.

- The original eight retro findings were correctly marked **Reproduced**. The fixes for exposure persistence, pinned destinations, transactional annotation/truth replacement, forward-only state, Windows path identity, bulk offset handling, and furniture detection are present.
- The round-8 `--audio-out` checks run before staging and immediately before publication. Real corpora, corpus ancestors/descendants, `work_root`, links, files, and unmarked non-empty directories are refused.
- Frozen ledger append and frozen reveal are refused. Frozen verification compares current ledger evidence with the manifest.
- Seed lock order is machine-wide seed lock → corpus lock → record lock. I found no reverse acquisition path through review, freeze, or normal mutations.
- The `path_key` ledger pin, duplicate controlled-manifest `set_id` rejection, and both `model_out`/sidecar guards are implemented.
- Bulk-offset preview remains in memory; role endpoints receive the row shift and endpoint-specific clamp; undo restores offset-owned fields.
- The audit heuristic accepts `*NSYNC`, `-M-`, and deliberate Unicode hyphen/minus names while detecting whitespace/BOM/zero-width furniture. A read-only scan of the worktree’s committed `release-1` labels found 444 labels and no rejection.
- The claim that certification is globally disabled is false; see B1.
- The round-8 audio marker is syntactically valid rather than demonstrably renderer-owned; see B5.

I could not run `uv` or pytest in the mandated read-only sandbox. Runtime claims are therefore limited to source inspection and read-only data/hash checks.

## B — Correctness bugs in the fix

1. **Realistic P0 — freezing and certification remain enabled through public paths.**  
   [src/id_detector/truth.py:2347](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/truth.py:2347) emits `"certifiable": true` for an independent set, while [src/id_detector/cli.py:1836](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/cli.py:1836) still executes `idea truth freeze` and reports success. Separately, [src/id_detector/benchmark/scorer.py:1607](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/benchmark/scorer.py:1607) emits `"status": "certified"` without consulting `CERTIFICATION_ENABLED`; public `idea benchmark score` reaches it at [src/id_detector/cli.py:1230](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/cli.py:1230). A normal independent corpus can consequently be frozen as certifiable and scored into certified dimension results despite the round-8 freeze/certification moratorium. The printable L3 summary also says all thresholds are met without the disabled message at [scripts/score_corpus.py:1238](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/scripts/score_corpus.py:1238).

2. **Realistic P0 — ordinary benchmark outputs can overwrite truth.**  
   [src/id_detector/benchmark/scorer.py:1642](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/benchmark/scorer.py:1642) writes an unrestricted `out_path`. Thus:
   `idea benchmark score --truth <set> --episodes ... --out <set>/ground_truth.json`
   reads the truth and then atomically replaces it with a benchmark report. [scripts/score_corpus.py:1354](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/scripts/score_corpus.py:1354) likewise accepts an unrestricted output and creates a derived artifact directory beside it. Similar unguarded user-selected report writers remain in `benchmark/corpus.py`, `ablations.py`, `transforms_schedule.py`, and `shortlist.py`.

3. **Realistic P1 — every corpus open can enumerate unbounded ancestor folders.**  
   [src/id_detector/truth.py:1002](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/truth.py:1002) lists every immediate child of an ancestor, and [src/id_detector/truth.py:1025](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/truth.py:1025) repeats that through the filesystem root. For the stated corpus path this includes the repository, `Documents`, `C:\Users\natha`, `C:\Users`, and `C:\`. Reveal performs gateway checks twice while holding its locks. The fixer’s recorded timeout with a 7,500-entry ancestor is therefore a realistic review-page timeout/flakiness risk, not merely theoretical.

4. **Realistic P1 — the repeated-test-version guard lost its direct regression.**  
   [tests/test_stage5_calibration.py:345](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/tests/test_stage5_calibration.py:345) is still named `test_certify_refuses_repeated_test_version`, but now only proves the earlier `CertificationDisabled` exception. No test calls `_guard_test_version` or expects `DuplicateTestVersion`. Reverting or deleting that retained guard would not fail this test suite.

5. **Adversarial P2 — the audio replacement marker is forgeable and unbound.**  
   [src/id_detector/benchmark/controlled.py:766](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/benchmark/controlled.py:766) accepts any JSON object with `kind: controlled-audio` and unique string `set_ids`, including an empty list. It ignores the written schema version, generator, corpus version, audio inventory, and hashes. Copying or hand-writing that tiny marker authorizes replacement of an arbitrary non-corpus directory. The separate corpus checks still protect an actual corpus.

6. **Adversarial P2 — “exact ledger equality” discards order and multiplicity.**  
   [src/id_detector/truth.py:1289](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-a1e319efe359a9781/src/id_detector/truth.py:1289) reduces ledger lines to a set of hashes. Duplicating or reordering existing lines therefore passes the purported exact-equality check.

## C — Regressions

- I found no regression to the truth-review route protections: loopback binding, Host/Origin middleware, CSRF on mutations, mutation-free GET routes, constrained audio lookup, exposure-before-prediction ordering, and the cross-process write lock remain.
- `src/idea_web/truth_review.py` is unchanged; the service still defaults to `127.0.0.1`.
- No regression to the 4a-ii serving move or the `idea.cmd` double-click path was introduced.
- Apart from B1–B4, the rounds 2–7 pinned-path, frozen-population, transaction, provenance, state-transition, and renderer protections remain present by static inspection.

## D — Test quality

- The round-8 audio, frozen-ledger, seed-lock, path-alias, duplicate-marker, model-output, `run_certify`, CLI-certify, and L3-flag tests directly exercise their changes and would fail if those specific checks were removed.
- They do not test the two certification emitters in B1. Indeed, `test_truth_corpus_followup_sol.py:175` still expects independent truth to receive generic `"certified"` status.
- No regression test protects benchmark/report output destinations against a real corpus.
- The changed repeated-version test no longer exercises the behavior in its name.
- I could not execute pytest, formatting, or JavaScript gates because `uv` and pytest execution were prohibited.

## E — Scope

- `git status` and path-limited diffs show no changes under `data/corpus/` or `work/`.
- `io.py`, `jobs.py`, `retention.py`, `tests/test_phase2b_retention.py`, playlists, `tests/test_playlists.py`, `README.md`, `idea.cmd`, theme code, and `tests/test_phase0a_security.py` are untouched.
- `pyproject.toml` and `uv.lock` are untouched; no dependency was added.
- Every untracked file listed by `git status --short` was inspected.
- `git diff --check` reports no whitespace errors.
- Independent read-only inventory/SHA-256 checks found no problem in either committed frozen controlled manifest (`controlled-synth-1` and `controlled-events-1`). This was not execution of the project verifier.

## Required fixes

### Realistic

1. **P0:** Put freeze and every certification-producing path behind one shared disabled gate: refuse `freeze_truth`/`idea truth freeze`, prevent generic scorer `"certified"` results, force every L3 `certifiable` field false, and surface the exact disabled message in JSON and printed summaries.
2. **P0:** Apply a centralized generated-output destination guard to every user-selectable benchmark/script output and derived artifact directory, rejecting links and any destination that is inside, is, contains, or would overwrite a corpus; revalidate immediately before publication.
3. **P1:** Replace ancestor sibling enumeration with an O(path-depth) fixed corpus-root sentinel check. Seed new corpora with that sentinel and provide a bounded, fail-closed legacy migration path.
4. **P1:** Add a direct `_guard_test_version` regression that proves a reused version raises `DuplicateTestVersion` without enabling production certification.

### Adversarial

5. **P2:** Validate the complete audio marker schema and bind it to the exact destination inventory using set identifiers and audio hashes before allowing replacement.
6. **P2:** Record and verify ledger count/order, or a canonical full-ledger digest, instead of comparing sets of line hashes.

VERDICT: FIX_FIRST
## A — Claim paths

**1. DONE.** The production gate is a literal `False` at `src/id_detector/truth.py:438`; no environment, config, or CLI override exists.

| Production path | Closed-gate result |
|---|---|
| `idea truth freeze` | Refuses before corpus access at `truth.py:2346-2350`. |
| `idea benchmark score` | Every certification status becomes the disabled message at `benchmark/scorer.py:1750-1759`; CLI explains the gate at `cli.py:1271-1272`. |
| `idea benchmark certify` | Refuses before opening a corpus or producing output at `calibrate/certify.py:273-275`. |
| `idea benchmark links-score` | A passing score emits `pass: null` and the disabled status at `enrich/benchmark.py:125-145`. |
| `idea benchmark freeze-profiles` | All positive feature flags pass through `certifiable_under_gate` at `profiles.py:262,286,331,380`. |
| `scripts/score_corpus.py` | Positive `thresholds_met` is changed to `null` at `score_corpus.py:1055-1058`; `certifiable` is gated at `:1090`; the positive sentence is suppressed at `:1307-1317`. |
| Calibrated episodes | Loaded `"certified"` entries are downgraded to provisional at `fuse/episodes.py:664-682`. |
| Calibration validation | The final belt-and-braces check permits only provisional, zero-population entries at `calibrate/validate.py:571-584`. |

The emitter regression is byte-identical to `28d8fe1`; it scans all positive patterns at `tests/test_truth_corpus_followup_r10.py:47-58,209-245`. The production-default test independently passed.

## B — Defect 1 and draft scoring

**4. DONE.** `certification_scope` canonicalizes roots, requires one frozen corpus, requires exact manifest inventory, and checks exposure across every frozen record at `benchmark/scorer.py:1540-1645`. Duplicate sets are rejected at `scripts/score_corpus.py:1020-1030`; frozen inventory and ledger changes are refused at `truth.py:1478-1539`.

Draft/unfrozen run lists remain scoreable. They receive `truth_status: draft`, `certifiable: false`, and an actionable development-only reason at `benchmark/scorer.py:1590-1600` and `scripts/score_corpus.py:1063-1099`. Both gate states are covered at `tests/test_certification_followup.py:330-349`.

The known open-gate `thresholds_met` discrepancy is correctly recorded as a prerequisite, not reachable in production.

## C — Defect 2

**2. DONE.** `scratch_corpus_ancestor` checks corpus markers and truth-bearing children at every ancestor through the filesystem root at `truth.py:1261-1283`. `refuse_scratch_destination` then rejects links, anything within `work_root`, existing paths, and corpus descendants before creation at `:1286-1316`.

The actual scratch path is validated immediately before create-only `os.mkdir` at `calibrate/validate.py:550-561`. Regression cases include ordinary and multi-level directories inside a manifest-less corpus at `tests/test_certification_followup.py:428-460`.

A read-only runtime probe against `data/corpus/release-1/ordinary/a/b/...` was refused without creating anything.

## D — Certify outputs and freezing

**3. DONE.** Report, predictions, and registry are all guarded with `work_root` before corpus access at `calibrate/certify.py:276-294`. `_publish_certification` rechecks each destination immediately before its individual write at `:235-260`. The CLI selects `evaluated` when `n_certified == 0` at `cli.py:1500-1505`.

**5. DONE.** Freezing remains an explicit owner command at `cli.py:1898-1903`, is currently gate-blocked, and its underlying validation rejects every draft row at `truth.py:2466-2528`.

Read-only checks found:

- `release-1`: no manifest; 7 sets, all 222 rows draft.
- `dev-1`: `frozen:false` at `data/corpus/dev-1/corpus-version.json:1`; all 120 rows draft.
- `controlled-events-1`: 145 records, frozen hash verification and independence both true.
- `controlled-synth-1`: 25 records, frozen hash verification and independence both true.

`docs/STATUS.md:114-130` plainly says certification remains off. The diff from `0915f85` touches none of `data/corpus/`, `work/`, `profiles/`, the golden output, or the protected second-session files.

## E — Tests

**1 and 5 test aspects: DONE, with sandbox execution limits.**

- `certification_gate_open` and `certification_gate_closed` are test-only monkeypatch fixtures at `tests/conftest.py:47-70`.
- Among the pre-existing gate-opening modules, only `test_truth_corpus_followup_r7.py` differs from current `main`, exactly for the reported `work_root` signature update at `:445-458`.
- Existing scorer and certify assertions were expanded across both states at `tests/test_score_corpus.py:462-507` and `tests/test_stage5_calibration.py:340-374`; none was weakened.
- `git diff --check` passed.

`uv` could not launch through its WinGet link. Direct pytest passed `test_the_production_gate_is_closed`, but tmp-path tests could not start because the read-only sandbox has no usable temporary directory. Those regressions were reviewed statically; no provider was called.

## Required fixes

### Realistic

None under the Round 2 stop rule.

### Adversarial

None required for this commit.

## Follow-ups, not blockers

- **P0 before reopening the gate:** keep `CERTIFICATION_ENABLED = False` until threshold wording, corpus-wide freeze independence, non-corpus certification semantics, and durable test-version reservation are implemented as recorded at `docs/reviews/followup-certification.md:507-535`.
- **P2 accepted risk:** if the threat model expands, pin or identity-check the temporary parent through scratch creation and cleanup (`docs/reviews/followup-certification.md:537-546`).

VERDICT: OK_TO_COMMIT
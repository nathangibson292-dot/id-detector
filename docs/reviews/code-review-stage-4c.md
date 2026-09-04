# Stage 4c code review — rescans, scanners, events

*Reviewer: Claude (Opus) — Codex usage limit reached 2026-09-04. Read-only review against docs/PLAN.md rev 5.2.*

**Verdict: OK_TO_COMMIT.** No P0 or P1 issues. The implementation is faithful to the plan contracts (orchestrator-owned generation loop, immutable generations with sidecars listing every input-generation hash, union-of-generations fusion, deterministic rescan ids, `T_ind` tiers, proved bounds from selected per-trial votes only, scanner fusion with `transform=null` / `logical_trial_id=sha1(provider‖chunk_index)` / 0.5 second-commercial-engine discount, no cascading, honest termination and budget handling). Every headline number in the stage report matched the committed `ablations.json`.

Findings are all P2 (transparency / hardening), most already disclosed in the stage report:

### [P2] Event gate passes on the point estimate while jump precision's cluster lower bound is below target
`jump` precision 83.78% (point) passes the ≥80% point-estimate gate, but its one-sided 95% cluster lower bound is 70.73%. Legitimate (4c's gate is point-estimate; cluster bounds are a Stage 5 concern) and disclosed. The 6 spurious jumps come from 3 legacy Stage-2a sets (`controlled-019/021/022`) whose renders contain several discontinuities but whose truth records only one. **Fix for 4d/5:** annotate those sets' full event truth or drop them from the event stratum. `ablations.py:516-521`.

### [P2] Event matching uses a wide asymmetric [at−2s, at+30s] window
Because each replicate has exactly one discontinuity, the 30 s horizon means "detected anywhere in the episode"; the strict ±2 s numbers are reported alongside. Revisit the horizon per event type once real-provider lag is measured. `scorer.py:749-783`.

### [P2] The p90 cluster lower bound carries no statistical information on this corpus
Genuine paired set-clustered comparison (10,800→4,800 ms = 55.55%), but the bootstrap has zero variance because every controlled set shares identical geometry, so the lower bound equals the point estimate. Honestly flagged. Keep the caveat prominent for 4d. `ablations.py:426-473`.

### [P2] No-double-submission test proves recovery idempotency, not a real cross-generation content collision
The three injection tests prove `max(submitted per content) == 1`; geometry dedup already prevents byte-identical windows across generations, so the content-cache branch is defensive redundancy the tests don't directly exercise. Optional: add a periodic-signal test that feeds two generations a byte-identical window. `tests/test_stage4c_generations.py:239-320`.

### [P2] `rescan_request.input_hashes` empty inside `build_episodes` (filled by `fuse_generation`); benchmark-internal only, no committed artefact affected.
### [P2] Report's ablation table lists 7 arms; the run and committed artefact have 8 (`schedule_12_9_0_gen0_only` omitted from the markdown). Documentation only.

## Verified
`pytest -q` 452 passed / 3 deselected; `ruff check` clean; `ruff format --check` 122 files; `uv lock --check` clean; fixture audit 317 files passed; doctor all PASS (Panako WARN, no JDK); `git diff --check` exit 0. `ablations.json`: corpus `controlled-events-1`, 145 sets, 356 boundaries, event cases drift 31 / jump 31 / loop 32 / replay 30 / reset 0; all gates `pass:true` except reset (unexercised, honestly excluded).

REVIEW VERDICT: OK_TO_COMMIT

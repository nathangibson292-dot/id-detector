# Review outcome: FIX_FIRST

## A — Money

1. **PARTIAL — P0.** Bundle-era `fusion:2` Deep results now stop before reservation and dispatch, with a zero-money settlement and explicit `stale_result` status ([pipeline.py:741](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:741), [test_refusion_money.py:50](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/idea_web/test_refusion_money.py:50)). Hosted Deep remains structurally refused before intake ([worker.py:2660](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/jobs/worker.py:2660)).

   **P0 gap:** stale discovery scans only `present/bundles` and rejects anything without compatibility inputs ([compat.py:274](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/compat.py:274), [compat.py:279](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/compat.py:279)). All fourteen owner mixes are reported as pre-bundle ([build-local-accuracy-fixes.md:403](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/docs/reviews/build-local-accuracy-fixes.md:403)). Therefore `analyse` or the local worker can miss the stale result and continue to a new Deep reservation and paid sweep ([pipeline.py:686](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:686), [pipeline.py:938](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:938), [pipeline.py:1133](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:1133)). No separate `--refresh` is required.

   Existing regressions manufacture or re-stamp bundles ([test_refusion.py:119](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/test_refusion.py:119), [test_refusion_money.py:28](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/idea_web/test_refusion_money.py:28)); none attacks a pre-bundle Deep result through `analyse` or `LocalWorker`.

## B — Readiness

2. **DONE — P0.** Production’s synchronous budget is zero; discovery is also deferred, and per-mix exceptions are contained ([server.py:30](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/server.py:30), [server.py:127](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/server.py:127), [server.py:146](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/server.py:146)). The regression blocks the first re-fusion until `/healthz` answers, asserts discovery ran on `idea-upkeep`, uses 24 slow mixes, and proves one failure does not stop the other 23 ([test_refusion_upkeep.py:50](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/idea_web/test_refusion_upkeep.py:50)). This proves readiness ordering, not merely fixture speed.

No GET-side publication was found. The FastAPI routes use read-only discovery, and the GET regression compares both trees byte-for-byte ([test_refusion.py:391](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/test_refusion.py:391)).

## C — Durability and concurrency

3. **DONE — P0.** Backup discovers a valid `present/current` independently of SQLite, follows it under the media lock, and includes the sealed bundle and direct frozen run ([backup.py:433](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/backup.py:433), [backup.py:546](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/backup.py:546), [backup.py:800](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/backup.py:800)). The backup/restore regression deletes both relevant SQLite tables and proves the restored pointer, bundle and frozen bytes survive ([test_refusion_upkeep.py:162](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/idea_web/test_refusion_upkeep.py:162)).

4. **PARTIAL — P1.** Observation, window and hint payloads use strict lowercase SHA-256 checks and are hashed and parsed from the same bytes ([refusion.py:177](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/refusion.py:177), [refusion.py:189](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/refusion.py:189)). Present-but-changed windows are rejected; absent windows alone are reconstructed.

   However, the final `episodes.done.json` digest is used only to select a generation and is never validated, while other recorded upstreams such as `decode/pcm.json` and `fuse/identities.genN.json` are ignored entirely ([refusion.py:166](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/refusion.py:166), [refusion.py:197](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/refusion.py:197)). For a pre-bundle result, `load_run_snapshot` reads the mutable PCM record for duration ([bundles.py:468](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/present/bundles.py:468)); a changed PCM record can therefore influence the new result despite failing its recorded checksum. This does not satisfy “every recorded digest.”

5. **PARTIAL — P1.** Direct `analyse` checks stale bundles before `run_hints`, and its connector/socket regression is strong ([pipeline.py:686](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:686), [test_refusion.py:225](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/test_refusion.py:225)). The hosted queue still runs `run_hints` during intake before the service can perform stale lookup ([worker.py:2858](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/jobs/worker.py:2858), [worker.py:3272](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/jobs/worker.py:3272)). Thus a hosted stale Free request is not offline end-to-end and can construct network connectors first.

9. **DONE — P1.** Plain refresh acquires and rechecks under the media lock; `busy` is distinct and upkeep never falls through to publication ([refresh.py:49](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/present/refresh.py:49), [server.py:108](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/server.py:108)). The held-lock regression proves zero tree change and later recovery ([test_refusion.py:512](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/test_refusion.py:512)).

10. **DONE — P2.** Each evidence payload is read once, hashed, then parsed from that payload ([refusion.py:177](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/refusion.py:177), [test_refusion.py:487](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/test_refusion.py:487)).

For covered bundle paths, publication ordering is sound: frozen run and bundle are sealed before the atomic pointer move; retries reuse or advance past incomplete directories ([bundles.py:265](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/present/bundles.py:265), [bundles.py:370](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/present/bundles.py:370)). Media locks serialize worker, CLI, upkeep, backup and GC. The old-bundle byte-preservation regression is adequate.

## D — Accuracy fixes

6. **PARTIAL — P1.** Bundle-era Deep results are correctly refused. Pre-bundle results have `manifest=None`; `is_deep` examines only the manifest, even though legacy invocation metadata is available separately ([refusion.py:119](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/refusion.py:119), [refusion.py:296](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/refusion.py:296), [bundles.py:468](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/present/bundles.py:468)). Startup therefore re-fuses pre-bundle Deep evidence even though its secondary windows were selected by fusion 2. The reported “all 14 re-fused, zero skipped” is consistent with this gap.

7. **DONE — P1.** Burial joins only gaps under 12 seconds and the regression keeps a 39-second track wholly inside a 59-second hole while retaining density-2 seam coverage ([episodes.py:1157](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/fuse/episodes.py:1157), [test_accuracy_fixes.py:132](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/test_accuracy_fixes.py:132)).

8. **DONE — P1.** Corrections use the full episode span, while ordinary answers receive the edge allowance; the corrected work becomes a crowd-only row ([episodes.py:1102](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/fuse/episodes.py:1102), [test_accuracy_fixes.py:240](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/test_accuracy_fixes.py:240)).

The three heuristics otherwise match their intended scope. A phantom can still satisfy the 45-second core, and a wrong 45-second `likely` row can still vouch for a short same-artist false positive ([exports.py:282](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/present/exports.py:282)); these are measured residual risks, not additional blockers. Explicit corrections close the dangerous edge case. Fresh Deep runs intentionally use fusion 3 before secondary targeting; calibration code was untouched.

## E — Tests and scope

The builder reports 2,003 passing tests. I did not rerun them because this review was required to modify no files; pytest would create temporary/cache artefacts. `git diff --check` is clean.

Reversion evidence is recorded at [build-local-accuracy-fixes.md:694](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/docs/reviews/build-local-accuracy-fixes.md:694):

- 1: bundle stale/refusal regressions fail; **legacy/pre-bundle path untested**.
- 2: readiness regression fails when pre-deadline work is restored.
- 3: backup/restore fails when pointer discovery is removed.
- 4: malformed observation/window regressions fail; **final-sidecar and PCM digests untested**.
- 5: direct connector regression fails; **hosted intake untested**.
- 6: bundle Deep-refusal regression fails; **pre-bundle Deep untested**.
- 7–8: burial and correction regressions fail under the old rules.
- 9: busy-media regression fails when refresh is unlocked.
- 10: same-byte regression fails when path hashing/reopening returns.

Accuracy tests contain the requested 39/45-second core, 59-second hole, edge/mid-track correction, possible backer, one-window, duplicate-work and buried-row near-misses. Pinned assertions changed only for version strings; the two server tests were updated to assert health before awaiting upkeep, which is the intended contract rather than a weakening.

No second-session file, `profiles/`, golden Local Free file, or `data/corpus/` is modified. `work/` does not exist in this worktree. Every untracked file was read.

The four scratch directories found are `C:\irf-codex-0921a`, `C:\irf-codex-0921b`, `C:\irf-mut-0921`, and `C:\Users\natha\AppData\Local\Temp\idea-refusion-codex-d59856fef8a545e7887479c06ecd4dd3`; all resolve outside the repository, `work/`, and `data/corpus/`.

### Follow-ups, not blockers

- The surviving 48-second phantom core and same-artist support premise remain heuristic limitations already disclosed by the measurements.
- A true spawned-process publication/crash regression is still absent, although the static media-lock and atomic-publication design is sound for same-media competition.

## Required fixes

### Realistic

- **P0:** Detect legacy/pre-bundle stale results before hints, reservation or dispatch; use their invocation metadata to refuse Deep, return `stale_result`, and require `--refresh`. Add direct-`analyse` and real-`LocalWorker` cases for intact, missing, mismatched and retention-pruned legacy Deep evidence, asserting zero provider, reservation and dispatch rows.
- **P1:** Never classify a pre-bundle Deep result as Free merely because its bundle manifest is absent; pass validated legacy metadata into the Deep check and leave that result unchanged during upkeep.
- **P1:** Validate the complete recorded provenance chain, including the final generation reference and PCM digest, and require the selected generation to equal the stored episode generation before publishing.
- **P1:** Move hosted stale lookup ahead of queue hint intake, or reuse the stored hints snapshot there; add a hosted stale-Free test with every connector and HTTP client wired to fail.

### Adversarial

- **P2:** Add a spawned two-process regression covering competing re-fusions, a crash after freezing but before pointer publication, and cross-alias lock acquisition; impose deterministic media-lock ordering if the cross-alias case can acquire two locks.

VERDICT: FIX_FIRST
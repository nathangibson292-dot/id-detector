## A — The legacy money path

1. **DONE — upkeep preserves legacy Deep protection.** Upkeep returns `may_refresh_page=False` for validated Deep or unsafe metadata, preventing the compatibility-less page bundle ([refusion.py:501](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/refusion.py:501)). Stale lookup independently considers the flat legacy result even with bundles present ([compat.py:339](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/compat.py:339)).

   The page-24 regression runs full upkeep and then direct Deep analysis with publication unchanged and decode/reservation/provider seams forbidden ([test_refusion.py:526](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/test_refusion.py:526)). The real-`LocalWorker` legacy/intact case also starts at page 24, runs upkeep, and asserts zero provider calls, reservations, dispatches, and spend ([test_refusion_money.py:89](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/idea_web/test_refusion_money.py:89)). The reported reversion proof records all selected cases failing ([build-local-accuracy-fixes.md:1082](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/docs/reviews/build-local-accuracy-fixes.md:1082)).

2. **DONE — unreadable metadata fails closed.** A flat result is recognized independently of its journal; missing, blank, malformed, truncated, invalid, or duplicate association returns an explicit error ([bundles.py:219](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/present/bundles.py:219)). Pipeline refusal occurs before decode, hints, reservation at line 962, and dispatch at line 1157 ([pipeline.py:684](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:684)).

   All four requested damage shapes run through direct analysis plus upkeep ([test_refusion.py:616](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/test_refusion.py:616)) and the real worker with unchanged publication and zero reservation/dispatch/spend ([test_refusion_money.py:173](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/idea_web/test_refusion_money.py:173)).

3. I found no new resend, second-settlement, or claim-fence bypass. The stale path settles once at zero and returns before paid admission ([pipeline.py:765](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:765)); the real-worker test checks `run_reservations`, `run_dispatches`, `run_settlements`, and the original settlement.

## B — Legacy re-fusion still works

Read-only validation against the owner’s actual 14 directories produced exactly:

- 11 Free results with valid window, PCM, identity, selected-generation, and digest chains.
- Two valid Deep results: “Speed Garage & Bass Mix - Holly Olivia (March 26)” and “Garage Mix - Dec 25”.
- BENWAL as Free metadata but an invalid generation chain.

**The Deep blocking is correct and safe, not the over-strict-loss alternative.** Their sidecars prove which historical observations were consumed, but do not prove that fusion 3 would have selected the same paid second-opinion target set. Re-fusing those observations would cost zero, but stamping the result as a current Deep execution would overclaim recipe equivalence. The refusal at [pipeline.py:224](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:224) is therefore the intended protection confirmed in earlier rounds.

To make future Deep re-fusion provable, the code would need to replay current first-pass fusion and targeting, derive the complete expected secondary window set, and prove exact equality against every recorded attempted window—including no-match/error attempts—before restamping.

BENWAL is a genuine stored-data inconsistency, not a validator bug: `episodes.done.json` records generation digest `4f600d…`, while that generation’s sidecar records `c38286…`. The comparison at [refusion.py:188](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/refusion.py:188) correctly refuses publication.

**MISSING — owner-visible reporting.** A later direct/worker request clearly returns `stale_result`, says nothing was spent, and explains `--refresh`. Normal startup does not. It stores skips only in an in-memory `UpkeepReport`, identifies mixes by truncated hashes, and emits a warning without the remedy ([server.py:99](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/server.py:99), [server.py:115](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/server.py:115)). `idea serve` never surfaces that report in its returned browser status. Thus the three skips are safe but not clearly disclosed to the owner.

## C — Concurrency and durability

3. **DONE — spawned-process harness.** It uses real `subprocess.Popen` children ([test_refusion.py:797](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/test_refusion.py:797)), races two re-fusions, kills after sealing and before pointer publication, resumes in another process, and probes two aliases. Aliases map to one canonical lock, so no dual-lock ordering is needed ([jobs.py:47](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/jobs.py:47)).

However, that canonicalization introduces a realistic integration regression: the pipeline already holds the canonical lock for the requesting alias at [pipeline.py:641](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:641), then `_refuse_stored` tries to acquire the sibling alias’s lock at [pipeline.py:213](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:213). Both now resolve to the same lock, so `_ACTIVE_LOCKS` rejects the second acquisition. A valid stale result found through a source alias therefore reports “another analysis … running” forever instead of re-fusing.

Durability otherwise remains sound: the frozen run is sealed before publication ([bundles.py:510](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/present/bundles.py:510)), the bundle is sealed before the atomic pointer move ([bundles.py:497](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/present/bundles.py:497)), crash recovery retains the prior pointer, and backup follows and verifies both pointer targets ([backup.py:546](C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/backup.py:546)).

Readiness is preserved: production’s synchronous budget is zero, discovery runs in the background, and per-mix exceptions are isolated. The reported owner measurement was `/healthz` in 1.062 seconds with 14 stale mixes.

## D — Tests and scope

- No protected second-session file, `data/corpus/`, `work/`, `profiles/`, or golden file appears in status or diff.
- Every untracked file was read; `git diff --check` is clean.
- No pinned assertion was weakened improperly. The changed legacy-page assertions now pin fail-closed preservation, while recipe-version assertions were advanced to fusion 3.
- I did not run pytest because this review is explicitly read-only. The builder reports 2,027 passed and 2 skipped, but the final tree received affected-path reruns rather than one completely clean final full-suite pass.

## Required fixes

### Realistic

- **P1:** Avoid reacquiring the canonical media lock when `_refuse_stored` follows a sibling source alias. Compare canonical lock identities or pass held-lock ownership through, then add a real pipeline/worker cross-alias stale re-fusion regression asserting successful publication and zero provider calls.
- **P1:** Surface completed upkeep skips in an owner-visible browser/CLI status using the mix title, exact reason, and explicit remedy (`--refresh`, with a paid-run warning). Cover both protected Deep and BENWAL’s inconsistent provenance.

### Adversarial

None. The requested P2 spawned-process coverage is present.

## Follow-ups, not blockers

- Run one complete clean suite on the final tree rather than aggregating earlier shard passes with affected-path reruns.
- Retain the documented phantom-core and same-artist heuristic risks.
- A future Deep re-fusion may be enabled only after exact current-target-set equivalence is proved as described above.

VERDICT: FIX_FIRST
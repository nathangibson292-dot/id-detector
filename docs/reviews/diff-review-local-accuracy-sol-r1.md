## A — Money

- **P0: failed re-fusion silently becomes a fresh paid Deep analysis.** When a stale candidate is found, `_refuse_stored()` may return `None`; the pipeline then continues without warning into planning, reservation, and AudD dispatch ([pipeline.py:709](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:709), [pipeline.py:889](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:889), [pipeline.py:1084](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:1084)). Missing or changed mandatory inputs cause exactly that `None` ([refusion.py:145](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/refusion.py:145), [refusion.py:269](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/refusion.py:269)). This is the pre-existing behavior of an incompatible version bump, and the run remains subject to the Deep recipe cap and SQLite reservation fence, but it is **not clearly signalled**. Per the requested rule, this silent paid rerun is P0.

- A successful re-fusion itself has no money path. It returns through the ordinary compatible-result branch before reservation and dispatch, and a job-owned invocation writes one zero-money settlement for the new run ([pipeline.py:731](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:731), [pipeline.py:744](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:744)). It does not rewrite the source run’s settlement.

- The local worker test does exercise the production runner and SQLite authority: zero additional reservation/dispatch rows, one zero settlement, and the original paid settlement unchanged ([test_refusion_money.py:36](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/idea_web/test_refusion_money.py:36), [test_refusion_money.py:69](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/tests/idea_web/test_refusion_money.py:69)).

- Hosted Deep cannot spend: it is refused before intake and again at reservation/dispatch ([worker.py:2660](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/jobs/worker.py:2660), [worker.py:566](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/jobs/worker.py:566)). Hosted Free can reach the common pipeline re-fusion path but has a structural zero-dollar recipe.

## B — Network

- Direct `refusion.py` and the startup pass are offline: they read local evidence, call identity/fusion, freeze, and publish; no provider, hint connector, or enrichment API is imported or invoked ([refusion.py:269](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/refusion.py:269), [refusion.py:323](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/refusion.py:323)).

- **P1: the `analyse` re-fusion route is not offline.** It runs `run_hints()` before searching for the stale bundle ([pipeline.py:659](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:659), [pipeline.py:702](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:702)). `run_hints()` constructs an HTTP client and executes SoundCloud, YouTube, MixesDB, Mixcloud and 1001Tracklists connectors on a cache miss ([pipeline.py:429](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/hints/pipeline.py:429), [pipeline.py:503](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/hints/pipeline.py:503)). Fresh hints can also change the compatibility key and bypass re-fusion into the paid fallback above.

- A worker job submitted with `acquire=True` performs enrichment after a successful re-fusion ([runner.py:375](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/webapp/runner.py:375), [cli.py:814](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/cli.py:814)). That is separately owner-requested, but it makes the absolute “no enrichment lookup reachable from the re-fuse branch” claim false.

## C — Durability and concurrency

- The core publication order is sound: the frozen run and bundle are sealed before the atomic `present/current` write ([bundles.py:265](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/present/bundles.py:265), [bundles.py:370](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/present/bundles.py:370)). A crash before the pointer moves leaves the old result selected. Re-fusion callers hold the cross-process media lock ([refusion.py:346](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/refusion.py:346), [pipeline.py:603](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:603)); deterministic run IDs make retry safe. The source bundle is not rewritten.

- **P0: startup-created re-fusions are not included consistently in backup/restore.** Backup discovers bundles and frozen runs from SQLite only ([backup.py:387](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/backup.py:387), [backup.py:398](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/backup.py:398)). Startup publication creates neither `result_bundles` nor `analysis_runs` rows. The extras logic copies `present/current` but explicitly excludes `present/bundles` and `fuse/runs` ([backup.py:499](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/backup.py:499), [backup.py:513](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/backup.py:513)). A snapshot can therefore contain a pointer to an omitted re-fused bundle; restore falls back to the old result and loses the new result.

- **P1: hash validation fails open in two cases.** A malformed or missing recorded digest is accepted because mismatch is rejected only when the value is already a valid 64-hex digest ([refusion.py:130](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/refusion.py:130)). A changed window file is treated like a retention-deleted window and reconstructed instead of rejected ([refusion.py:151](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/refusion.py:151)). An owner accident can publish a current result from evidence that did not pass its recorded hash.

- The plain stale-page fallback still publishes without acquiring the media lock ([refresh.py:47](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/present/refresh.py:47)). Thus a second server that finds re-fusion busy immediately proceeds to an unlocked page publication ([server.py:52](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/server.py:52)). Immutable directories and atomic writes limit damage, but this does not satisfy the stated cross-process lock contract.

- GC itself takes the media lock and treats any bundle entry or current pointer as a publication reference, so no new direct collection-loss path was found.

## D — Start-up

- **P0: startup blocks HTTP readiness.** `refresh_stale_pages()` runs synchronously before `uvicorn.Server.run()` ([server.py:105](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/server.py:105), [server.py:147](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/server.py:147)). The socket is listening, but `/healthz` cannot be answered. The reported 21–35 seconds for 14 mixes already crosses the 30-second gate; hundreds scale serially and can delay service for minutes.

- A bad individual mix does not stop later mixes or startup: both re-fusion and page-refresh exceptions are caught per medium ([server.py:48](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/idea_web/server.py:48)). Failures are, however, swallowed without diagnostics.

## E — Accuracy fixes

- Fix (a) can still let a phantom through if it produces five correlated windows over a 45-second run; conversely, a genuinely chopped/mashup track with ample total evidence but no 45-second core is newly hidden as scatter ([episodes.py:1084](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/fuse/episodes.py:1084), [episodes.py:1094](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/fuse/episodes.py:1094)).

- **P1: “only under matched audio” is not implemented.** `_runs()` fills every gap shorter than 60 seconds, so a complete 20–50-second real track between two phantom matches can still be buried ([episodes.py:1149](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/fuse/episodes.py:1149), [episodes.py:1176](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/fuse/episodes.py:1176)). The test only exercises a hole at or above the threshold.

- Fix (b) is deliberately conservative but not proof against false support: any wrong `likely` row with 45 seconds of evidence—including a hint-supported audio row—can vouch for unrelated short catalogue matches by the same artist ([exports.py:282](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/present/exports.py:282), [exports.py:309](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/present/exports.py:309)). The measured surviving false `likely` row demonstrates that this premise is not absolute.

- Fix (c) exempts both ordinary answers and explicit corrections near an edge ([episodes.py:1099](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/fuse/episodes.py:1099)). A genuinely wrong long episode can therefore survive an edge correction and then prevent that correction from becoming a crowd-only row because all unsuppressed hulls block crowd rows ([episodes.py:1200](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/fuse/episodes.py:1200)).

- **P1: stale Deep evidence is not necessarily equivalent to a fresh `fusion:3` run.** The first fuse’s suppression states directly determine secondary Shazam candidates ([pipeline.py:1474](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/pipeline.py:1474), [secondary_targeting.py:169](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/secondary_targeting.py:169)). These fixes change those states, so a fusion-2 Deep run may have probed different windows. Nevertheless, compatibility declares its recognition evidence exactly what the new build would gather and stamps it current ([compat.py:103](/C:/Users/natha/Documents/Music/id-detector/.claude/worktrees/agent-abb9ec8cc9ac490fd/src/id_detector/compat.py:103)). Calibration values are not directly changed, but Deep targeting and final evidence can differ.

## F — Tests

- The new accuracy suite has useful near-misses for 39/45-second cores, mid-track answers, short episodes, possible backers, one-window rows, duplicate works, and fusion-suppressed rows.

- The real LocalWorker money test proves zero provider calls, zero reservation/dispatch rows, and one zero settlement for the happy-path Deep re-fusion.

- Missing coverage: failed/missing/hash-mismatched evidence through the real worker; platform-URL re-fusion with empty hint cache; backup/restore of a startup-created re-fusion; two-process publication; startup readiness over 30 seconds; equivalence against a fresh fusion-3 Deep secondary allocation; a 59-second burial hole; and an edge `correction`.

- Existing pinned assertions were changed only from `fusion:2` to `fusion:3`; none was weakened. Forbidden second-session files, corpus, profiles, `work/`, and the golden Local Free file are clean in Git status.

- Tests could not be executed in this read-only sandbox: `uv.exe` could not launch, and direct pytest failed because no writable temporary directory was available. The test assessment is therefore static; no live provider was called.

## Required fixes

### Realistic

- **P0:** If a stale candidate exists but re-fusion cannot prove its inputs, end non-spending with an explicit status; require a separate explicit force/refresh action before any new Deep reservation or dispatch.
- **P0:** Move bulk re-fusion off the synchronous readiness path, or bound/defer it through a worker so `/healthz` answers well inside 30 seconds.
- **P0:** Make backup follow `present/current` to its sealed bundle and referenced frozen run, then add a backup/restore test proving the re-fused result survives.
- **P1:** Require every recorded digest to be present, well-formed, and equal; distinguish a genuinely absent retained window file from a present-but-mismatched one.
- **P1:** Add an offline hints-snapshot reuse path and perform stale lookup/re-fusion without running network connectors.
- **P1:** Either bump Deep targeting compatibility or prove the recorded secondary window set matches what fusion 3 would choose before stamping a re-fused Deep bundle current.
- **P1:** Use actual matched support for burial, or reduce the join threshold to true adjacent-window continuity; add a correct track wholly inside a 59-second gap.
- **P1:** Keep explicit corrections contradictory at track edges, and test that the corrected work can still become a crowd-only row.
- **P1:** Acquire the media lock for plain page refresh, and distinguish “busy” from “not re-fusable” so startup never falls through to an unlocked publication.

### Adversarial

- **P2:** For defense in depth against an out-of-scope hostile same-user race, hash and parse the same in-memory bytes rather than hashing a path and reopening it afterward.

VERDICT: FIX_FIRST
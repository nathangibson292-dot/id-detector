## A — The legacy money path

1. **PARTIAL — blocker.** For an untouched, valid legacy Deep layout, direct `analyse` correctly discovers staleness at `src/id_detector/pipeline.py:685`, before hints at `pipeline.py:718` and reservation at `pipeline.py:955`. `_refuse_stored` classifies Deep before loading evidence at `pipeline.py:216`, then records a zero-money `stale_result` failure at `pipeline.py:771`.

2. **MISSING — P0 startup path.** Background upkeep does not leave a legacy Deep result unchanged. After `refuse_stale_result` returns `unrebuildable`, upkeep falls through from `src/idea_web/server.py:115` to `refresh_page` at `server.py:118`. Because `PAGE_VERSION` moved to 25, a real pre-bundle page is stale; `src/id_detector/present/refresh.py:72` publishes a new bundle through `src/id_detector/present/bundles.py:530`.

   That page-only bundle has `compatibility=None` (`bundles.py:378`). On the next Deep request, `find_stale_result` sets `has_bundle=True` at `src/id_detector/compat.py:323`, cannot consider the bundle because it lacks `analysis_inputs` (`compat.py:286`), and suppresses legacy discovery entirely at `compat.py:337`. The run then reaches hints and the paid reservation. This recreates the owner-money hole merely by starting the app.

3. **MISSING — P0 damaged metadata.** `legacy_result_metadata` returns `None` when no completed invocation validates (`bundles.py:230-243`). Direct stale lookup then returns no legacy candidate (`compat.py:341-380`). Upkeep likewise evaluates `is_deep(None, None)` as false (`src/id_detector/refusion.py:121-130`, `refusion.py:478-490`) and may re-fuse using the permissive legacy fallback at `bundles.py:205-215`. Missing or damaged Deep metadata therefore fails open instead of preserving the result and refusing payment.

4. **DONE — hosted stale-Free ordering.** Hosted intake performs stale lookup at `src/idea_web/jobs/worker.py:2848`, reuses its inputs, and gates hint intake at `worker.py:2886`. The regression makes `run_hints`, `httpx.AsyncClient`, and non-loopback sockets fatal at `tests/idea_web/test_refusion_money.py:158-222`.

## B — Provenance and Deep classification

1. **PARTIAL.** Validated legacy `achieved=deep` is classified correctly before evidence access, including missing, mismatched and retention-pruned evidence. Classification is not fail-closed when that metadata is absent or invalid, and the upkeep/page-refresh sequence above converts it into an unclassifiable bundle.

2. **DONE — recorded provenance.** Re-fusion validates:

   - the final sidecar and exactly one generation reference (`src/id_detector/refusion.py:179-192`);
   - final and selected generation bytes and generation equality (`refusion.py:232-246`);
   - every recorded upstream digest, including identities and PCM (`refusion.py:248-285`);
   - equality with the snapshot generation and duration before freezing (`refusion.py:378-384`).

   The four provenance mutations are pinned at `tests/test_refusion.py:372-417`.

3. **DONE — legitimate owner results.** The reported normal-startup measurement updated 13 owner mixes and correctly refused one internally inconsistent non-release-1 result; release-1 recall remained 143 → 154 with zero provider calls (`docs/reviews/build-local-accuracy-fixes.md:887-915`). No evidence shows the stricter proof wrongly rejecting a valid release-1 mix.

## C — Readiness and durability

1. **DONE — readiness.** Production’s synchronous budget is zero (`src/idea_web/server.py:28-31`); discovery and all work are deferred to `idea-upkeep` (`server.py:127-152`). The regression proves health and a page respond while 24 slow mixes are pending and that one failure does not stop the rest (`tests/idea_web/test_refusion_upkeep.py:50-107`). The reported real measurement was `/healthz` in 985 ms and upkeep in 32.391 s.

2. **DONE — publication and locking, apart from the money-path defect above.** Re-fusion holds the media lock (`src/id_detector/refusion.py:463-505`), seals its frozen run before bundle publication (`src/id_detector/present/bundles.py:411-447`), and atomically advances `present/current` only after verification (`bundles.py:187-195`, `bundles.py:398-405`). Page refresh, backup and CLI/worker publication share that lock.

3. **DONE — backup consistency.** Backup discovers `present/current` independently of SQLite and follows the verified bundle and frozen run under the media lock (`src/idea_web/backup.py:433-447`, `backup.py:546-582`, `backup.py:799-808`). Restore coverage is at `tests/idea_web/test_refusion_upkeep.py:162-196`.

4. No new resend, second-settlement or claim-fence bypass was found after stale discovery succeeds. The realistic defect is that discovery can be bypassed, after which the ordinary reservation and dispatch path is reachable.

## D — Tests and scope

- The four direct legacy cases are at `tests/test_refusion.py:327-369`; they make decode/reservation fatal and assert zero provider calls and unchanged attempt/result trees.
- The four real-`LocalWorker` cases are included in `tests/idea_web/test_refusion_money.py:74-155`; they assert zero new `run_reservations`, zero `run_dispatches`, zero requests and one zero-money settlement.
- Reported reversion runs fail all four legacy direct/worker cases (`docs/reviews/build-local-accuracy-fixes.md:836-847`). They do not exercise full `Upkeep.run_all` with a stale page, nor damaged invocation metadata.
- The reported suite is 2,014 passed plus 2 skipped, with ruff, formatting and fixture audit clean (`build-local-accuracy-fixes.md:921-965`). I did not run pytest because this review was explicitly read-only; `git diff --check` is clean.
- No protected second-session file, `profiles/`, golden file, `data/corpus/`, or `work/` appears in status. Every untracked file was read.

## Required fixes

### Realistic

- **P0:** Prevent upkeep from page-refreshing a legacy Deep result into a compatibility-less bundle, and make stale selection continue considering validated legacy Deep provenance even when unrelated or page-only bundles exist. Add a `PAGE_VERSION=24` legacy Deep test through full `Upkeep.run_all`, followed by direct and real-`LocalWorker` Deep requests asserting unchanged publication, zero provider calls, reservations and dispatches.
- **P0:** Fail closed for missing, malformed, truncated, or ambiguously associated legacy invocation metadata. Such a result must remain untouched and block Deep without `--refresh`; add direct, worker and upkeep regressions.

### Adversarial

- **P2:** Add the deferred spawned-process harness for competing re-fusions, crash-after-freeze/before-pointer publication, and cross-alias lock acquisition.

## Follow-ups, not blockers

- Require the generation sidecar to contain the expected window/PCM/identity input categories, not merely validate every key that remains recorded.
- The previously documented phantom-core and same-artist heuristic limitations remain measured accuracy risks, not blockers.

VERDICT: FIX_FIRST
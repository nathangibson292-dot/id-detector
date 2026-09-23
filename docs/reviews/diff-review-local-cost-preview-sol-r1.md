## A — estimate

The estimate is money-correct. `pricing.toml:2` supplies 5,000 µUSD/request through `AppConfig.load` (`providers/base.py:145-160`), and `cost.py:115-132` uses that value rather than hard-coding it. Window count comes from the production scheduler (`windows.py:125-138`), density uses ceiling division (`cost.py:72-78`), and the Deep ceiling remains $9 (`recipes.py:126-165`).

Against the real cached 5,402,215 ms mix, the implementation reports 90.04 minutes, 600 density-1 clips, $3.00, 300 density-2 clips, and $1.50. That matches the expected order and arithmetic.

The output says “approximately” and “Estimate only, not a promise.” Missing duration exits 2 without a number (`cli.py:559-572`). `idea cost` only reads stored records or explicit `--minutes`; there is no fetch/decode/provider path.

## B — confirmation gate

The gate is correctly before money:

- Deep-only callback wiring: `cli.py:865-906`.
- `--yes`, non-interactive refusal, default-no prompt: `cli.py:575-594`.
- Gate: `pipeline.py:938-945`.
- Reservation computation and durable recording occur later: `pipeline.py:981-1011`.
- AudD dispatch occurs later still: `pipeline.py:1176-1202`.
- Refusal calls only `_finish`, which records the in-memory outcome and writes no journal or settlement: `pipeline.py:571-601`.

Thus refusal creates no reservation, attempt, settlement, or spend journal and returns 130 through `cli.py:925-933`. Free never installs or reaches the callback.

Resume behavior is sound: any durable reservation, injected admitter, or recovered attempt evidence bypasses the fresh authorization prompt (`pipeline.py:938-942`). Existing authorized work therefore cannot be stranded or re-prompted into an unsafe branch.

The `bb57b70` authority is not weakened: `money.py`, `run_ledger.py`, `paid_clip.py`, and `idea_web/jobs/worker.py` are unchanged. Dispatch admission remains immediately before the durable dispatched event (`paid_clip.py:509-519`); ambiguous outstanding requests still settle as spent (`money.py:181-204`); claim/lease and SQLite settlement machinery remain unchanged.

## C — non-interactive paths

`PipelineOptions.cli_paid_confirm` defaults to `None` (`service.py:206-238`). Only `cli.analyse` assigns it, and the service forwards it only when non-null and local (`service.py:790-795`). Neither the web app nor worker creates that callback, so neither can prompt.

Hosted Deep remains refused before intake, run creation, reservation, or dispatch (`idea_web/jobs/worker.py:2649-2665`), with the second coalescing fence still present at `worker.py:3035-3038`.

## D — comparison runner

Dry-run is the parser default; only `--spend` enables execution (`compare_deep.py:293-329`). Before database creation or worker execution it checks total estimate, reservation headroom, per-run caps, and baseline scorability (`compare_deep.py:160-191`).

After acquiring the supervisor lock it rereads durable jobs/reservations and rechecks the aggregate bound (`compare_deep.py:192-207`). Every mix then receives a fixed cap whose sum is within the budget (`compare_deep.py:227-237`); the existing `UsdAdmitter` refuses each dispatch beyond its durable reservation. Cumulative, ambiguity-inclusive spend is folded from SQLite after each job (`compare_deep.py:240-247`).

Resume uses stable job IDs plus the existing queue, reservation, attempt, and settlement tables (`compare_deep.py:97-105,145-148,208-253`). Existing jobs are not enqueued again, including completed, failed, or cancelled jobs. There is no second spend ledger.

The source work tree is only copied; `--scan-root` must be outside both it and the repository (`compare_deep.py:108-116,210-224`). Truth and corpus inputs are read-only.

## E — measurement script and scope

`measure_refusion.py` reads cached records, performs fusion in memory, and writes derived files only under an OS temporary directory (`measure_refusion.py:45-57,118-146`). It calls `score_run_list` from `score_corpus.py`; no scorer or provider path is duplicated.

Only the eight reported files differ. `data/corpus/`, `work/`, `profiles/`, the Local Free golden, playlists, `tests/test_playlists.py`, `README.md`, `idea.cmd`, and `present/theme.py` are unchanged. Existing tests were not edited, so no pinned assertion was weakened. No credential, token, private data, or other publish-blocking material was added.

I independently confirmed collection is 2,180 offline tests out of 2,183 total. The four reported shards sum to 2,177 passed plus 3 skipped, matching that collection.

## Required fixes

### Realistic

- None — no P0, P1, or P2 required fix.

### Adversarial

- None — no P0, P1, or P2 required fix.

## Follow-ups, not blockers

- P2: Render estimated spend from integer microdollars or show three decimals; two-decimal floating formatting can understate exact half-cent totals such as $1.355 by half a cent.
- P2: Add an explicit post-job `spend <= budget` assertion. The current sum-of-durable-reservations invariant already prevents overspend, but the assertion would document and regression-test it directly.
- P2: Replace the single U+FFFD replacement character in `docs/reviews/build-local-cost-preview.md`; it is an encoding blemish, not a secret or functional defect.

VERDICT: OK_TO_COMMIT

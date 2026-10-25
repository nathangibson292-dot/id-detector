# Build: faster set scans (2026-10-25)

Brief: `id-detector-handoff/SCAN-SPEED.md`. Worktree `agent-speed01`, nothing committed. No network
or provider call was made at any point; every number below comes from stored evidence copied out
of the READ-ONLY `work/` (audio, raw bodies and job stores left behind) or from fakes on a
simulated clock.

## Plain-English summary

* **Asking about fewer windows: nothing qualified.** 21 sampling policies were replayed on the
  15 stored mixes that can be rebuilt. Every policy that saves a meaningful number of requests
  changes the tracklist on most mixes. The only one with an empty diff on 14 of 15 mixes
  (`nonecausal6`) saves 21 of 6,637 requests (0.3 %) and still changes one mix. Nothing was
  wired in. The reason: today's fusion uses every window. A skipped matched window shortens a
  track's on-air time (windows overlap by 3 s, so each skipped interior window removes about
  6 s), which moves start and end times and the "short" and duration filters. A skipped no-match
  window removes the evidence for an unidentified gap row.
* **Shipped: Shazam 429s no longer waste the request allowance.** When two throttles arrive in
  a row, the whole pool now pauses (about 10 s, doubling up to about 60 s, Retry-After honoured)
  and sends a single probe. The window that was refused is asked again after the pause, and its
  429s no longer use up its 5 retries. Replaying the owner's 2026-10-21 throttle pattern:
  **429s drop from 834 to 138 (−83 %), requests from 1,603 to 946 (−41 %), and windows left
  unanswered from 39 to 0** (in the real run, 101 were lost). The new code reaches the old run's
  final coverage at the same point (100.5 min vs 100.4 min) and then finishes the windows the
  old run gave up on (102.4 min for all 808).
* **Scan time for the 808-window example:** at 18 requests/min with no throttling, 808 ÷ 18 ≈
  **45 min of listening**, the same as before, because nothing was skipped. Under a throttle
  like 2026-10-21's, Shazam's own allowance sets the pace (about 200 answers per 30 min), so
  expect about **1 h 40 min – 2 h for a complete result**. The old code took 1 h 37 min and
  left 101 windows unanswered. The saving is wasted requests and a complete result, not
  wall-clock time. No client can go faster than Shazam's allowance.

## 1. Sampling: simulation method

`scripts/simulate_sampling.py <copy-of-work> <report.json>` (new, offline):

1. For each media folder, `present.bundles.load_run_snapshot` → `refusion._source_fuse_dir` →
   `refusion.load_fusion_inputs` loads the exact recorded evidence of the current result
   (observations, windows, hints, PCM duration), proven by its completion sidecars, exactly as
   the re-fuse upkeep does. `87cc7de7/819e1ee8` cannot be rebuilt ("its stored records disagree
   with each other: the final generation reference disagrees with its generation sidecar"), so
   it cannot be evaluated by anyone; 15 mixes remain (6,637 generation-0 windows).
2. **Baseline**: today's fusion (`build_identity_graph` + `build_episodes` with the stored
   corroboration thresholds) over ALL observations, then `present.exports.flatten_tracklist`
   (the publish projection: collapse, same-track bridge, minimum track length) with hidden rows
   included. Sanity check: the baseline equals the stored, published tracklist (artist, title,
   badge, hidden reason) on 13 of 15 mixes. The other two (`3683dbd4`, `4b4c2a5c`) also carry
   AudD/Panako evidence and were published under older settings. Every policy is compared
   against the baseline under identical fusion, so this does not affect the comparison.
3. **Policy**: the Shazam generation-0 sweep is put in mix order with each window's cached
   answer (match key / no-match / error). A policy decides, causally, which windows it would
   have asked. Only those windows' observations are kept (all other evidence unchanged),
   `scanned_windows` is recomputed from what was kept (as the pipeline does), and the result is
   re-fused and diffed.
4. **Diff**: every row, hidden ones included, keyed on (artist, title, confidence badge,
   hidden reason, start ms, end ms, on-air ms). "Strict" = all of it. The "rows+labels" column
   ignores the three times, and is shown only to explain why policies fail. A policy ships only
   if the strict diff is empty on every mix.

Policies (n = every n-th window first):

* `everyN`: ask only every N-th window (a lower bound, for scale).
* `refineN`: ask every N-th window, then fill every gap whose two ends disagree or are not
  matches. `-none` also skips gaps between two no-matches. `-bisect` fills by halving instead of
  asking everything in between.
* `runskipKxS`: after K identical matches in a row, skip S windows and probe the next one. If
  the probe disagrees, fill in the skipped windows. `-none` also counts runs of no-matches.
* `noneskipK` (an oracle, unfairly optimistic): skip every other window inside a no-match run
  of at least K windows. `nonecausalK` (the causal version): ask every even window, and skip an
  odd window only when the K asked windows on each side are all no-matches.

## 1a. Results: requests saved and tracklist diff, every policy

Pooled over 15 mixes, 6637 generation-0 windows (the diff counts every row, hidden ones included).

| Policy | Requests saved (pooled) | Mixes with EMPTY tracklist diff | Mixes with same rows+labels (times may differ) | Rows changed (removed/added, pooled) |
|---|---:|---:|---:|---:|
| `every2` | 3308 (49.8 %) | 0/15 | 0/15 | -971/+672 |
| `every3` | 4409 (66.4 %) | 0/15 | 0/15 | -1043/+625 |
| `refine2` | 1391 (21.0 %) | 0/15 | 0/15 | -427/+366 |
| `refine3` | 1664 (25.1 %) | 0/15 | 0/15 | -407/+339 |
| `refine4` | 1719 (25.9 %) | 0/15 | 0/15 | -379/+306 |
| `refine2-none` | 1829 (27.6 %) | 0/15 | 0/15 | -478/+379 |
| `refine4-bisect` | 2136 (32.2 %) | 0/15 | 0/15 | -505/+404 |
| `refine8-bisect` | 2437 (36.7 %) | 0/15 | 0/15 | -541/+420 |
| `runskip2x1` | 1226 (18.5 %) | 0/15 | 0/15 | -373/+330 |
| `runskip3x1` | 1011 (15.2 %) | 0/15 | 2/15 | -313/+285 |
| `runskip3x2` | 1300 (19.6 %) | 0/15 | 0/15 | -304/+265 |
| `runskip4x1` | 779 (11.7 %) | 0/15 | 0/15 | -254/+236 |
| `runskip5x1` | 667 (10.0 %) | 0/15 | 3/15 | -220/+199 |
| `runskip3x1-none` | 1329 (20.0 %) | 0/15 | 0/15 | -340/+288 |
| `runskip10x1` | 187 (2.8 %) | 2/15 | 10/15 | -69/+63 |
| `runskip20x1` | 12 (0.2 %) | 12/15 | 15/15 | -4/+4 |
| `noneskip5` | 338 (5.1 %) | 13/15 | 13/15 | -3/+0 |
| `noneskip10` | 182 (2.7 %) | 13/15 | 13/15 | -3/+0 |
| `nonecausal1` | 438 (6.6 %) | 4/15 | 4/15 | -56/+18 |
| `nonecausal3` | 119 (1.8 %) | 10/15 | 10/15 | -13/+4 |
| `nonecausal6` | 21 (0.3 %) | 14/15 | 14/15 | -4/+1 |

Per mix: requests saved, then the strict diff (rows removed/added); `ok` = empty diff.

| Policy | 068c85d9 | 0ece4b63 | 1baeb545 | 3683dbd4 | 4b4c2a5c | 4c90c7ab | 5cad3fdb | 66770a61 | 7b5c94cd | a5986744 | b8f88620 | c183f27c | c3b93048 | c5dc37ca | c799ca1e |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| windows (shown rows) | 605 (21) | 398 (24) | 307 (22) | 386 (31) | 401 (30) | 600 (27) | 808 (46) | 602 (42) | 249 (18) | 326 (26) | 367 (31) | 393 (29) | 541 (41) | 399 (9) | 255 (15) |
| `every2` | 302 -74/+39 | 198 -40/+29 | 153 -43/+29 | 192 -69/+49 | 200 -87/+61 | 299 -71/+42 | 403 -90/+69 | 300 -80/+53 | 124 -34/+31 | 162 -63/+46 | 183 -56/+46 | 196 -64/+51 | 270 -86/+62 | 199 -45/+22 | 127 -69/+43 |
| `every3` | 402 -83/+38 | 264 -43/+29 | 204 -47/+28 | 256 -73/+45 | 266 -88/+50 | 399 -89/+42 | 538 -90/+56 | 400 -88/+55 | 165 -36/+28 | 216 -63/+41 | 244 -59/+45 | 261 -65/+47 | 360 -97/+66 | 265 -47/+24 | 169 -75/+31 |
| `refine2` | 74 -20/+18 | 47 -17/+15 | 95 -28/+22 | 100 -33/+28 | 76 -36/+28 | 73 -26/+22 | 149 -38/+35 | 103 -39/+37 | 57 -18/+17 | 96 -24/+23 | 110 -29/+27 | 107 -35/+31 | 175 -53/+42 | 109 -19/+12 | 20 -12/+9 |
| `refine3` | 88 -22/+19 | 54 -17/+16 | 114 -29/+22 | 116 -29/+26 | 98 -36/+26 | 80 -21/+18 | 188 -33/+29 | 130 -36/+31 | 70 -17/+16 | 112 -22/+25 | 136 -34/+29 | 130 -34/+29 | 194 -47/+36 | 122 -17/+10 | 32 -13/+7 |
| `refine4` | 84 -18/+13 | 60 -14/+11 | 114 -27/+22 | 108 -27/+22 | 78 -21/+18 | 87 -25/+23 | 207 -31/+27 | 111 -31/+29 | 81 -18/+16 | 126 -31/+24 | 153 -35/+28 | 117 -28/+26 | 213 -45/+33 | 156 -22/+10 | 24 -6/+4 |
| `refine2-none` | 178 -31/+19 | 113 -21/+16 | 101 -28/+22 | 117 -35/+28 | 95 -38/+28 | 116 -33/+24 | 215 -43/+36 | 167 -48/+38 | 59 -18/+17 | 97 -24/+23 | 112 -31/+28 | 120 -37/+32 | 175 -53/+42 | 140 -20/+12 | 24 -18/+14 |
| `refine4-bisect` | 110 -27/+20 | 75 -20/+16 | 137 -32/+25 | 140 -36/+29 | 108 -38/+28 | 116 -34/+30 | 254 -42/+37 | 148 -45/+41 | 100 -21/+19 | 150 -35/+29 | 181 -39/+32 | 150 -38/+33 | 260 -57/+43 | 173 -27/+12 | 34 -14/+10 |
| `refine8-bisect` | 134 -31/+19 | 81 -20/+16 | 147 -32/+25 | 160 -44/+34 | 120 -40/+29 | 170 -45/+33 | 305 -45/+38 | 156 -45/+41 | 112 -21/+19 | 160 -35/+31 | 196 -40/+33 | 159 -38/+33 | 297 -61/+44 | 198 -28/+14 | 42 -16/+11 |
| `runskip2x1` | 72 -20/+16 | 37 -15/+14 | 89 -27/+23 | 88 -28/+25 | 63 -30/+25 | 60 -20/+18 | 134 -31/+29 | 86 -35/+34 | 50 -15/+15 | 80 -20/+22 | 108 -32/+27 | 104 -35/+31 | 146 -41/+36 | 93 -16/+11 | 16 -8/+4 |
| `runskip3x1` | 52 -15/+14 | 34 -13/+13 | 70 -22/+19 | 72 -23/+24 | 49 -21/+19 | 46 -14/+12 | 103 -29/+26 | 73 -34/+31 | 42 -14/+13 | 71 -17/+17 | 90 -28/+26 | 77 -24/+23 | 137 -40/+36 | 90 -17/+10 | 5 -2/+2 |
| `runskip3x2` | 82 -19/+16 | 38 -11/+10 | 96 -25/+18 | 88 -22/+23 | 58 -21/+19 | 58 -11/+9 | 134 -28/+25 | 80 -25/+24 | 58 -14/+13 | 86 -15/+15 | 126 -35/+30 | 100 -25/+23 | 170 -35/+29 | 114 -14/+9 | 12 -4/+2 |
| `runskip4x1` | 49 -14/+13 | 15 -8/+7 | 60 -19/+17 | 59 -21/+20 | 30 -16/+16 | 34 -10/+9 | 87 -23/+22 | 44 -23/+22 | 28 -9/+9 | 55 -13/+15 | 76 -27/+24 | 67 -23/+22 | 98 -30/+27 | 72 -15/+11 | 5 -3/+2 |
| `runskip5x1` | 32 -6/+6 | 19 -8/+8 | 47 -15/+15 | 47 -17/+18 | 26 -13/+11 | 25 -11/+9 | 65 -19/+17 | 41 -22/+19 | 26 -7/+7 | 54 -14/+12 | 58 -23/+21 | 52 -20/+20 | 96 -29/+27 | 76 -14/+7 | 3 -2/+2 |
| `runskip3x1-none` | 136 -26/+15 | 83 -14/+13 | 77 -23/+19 | 81 -23/+24 | 57 -21/+19 | 78 -18/+13 | 155 -33/+27 | 114 -37/+31 | 42 -14/+13 | 71 -17/+17 | 90 -28/+26 | 86 -24/+23 | 137 -40/+36 | 113 -18/+10 | 9 -4/+2 |
| `runskip10x1` | 24 -4/+4 | 0 ok | 18 -8/+8 | 12 -6/+6 | 3 -2/+2 | 10 -5/+4 | 12 -2/+2 | 1 -1/+1 | 4 -2/+2 | 20 -7/+7 | 21 -9/+7 | 15 -5/+5 | 23 -10/+9 | 24 -8/+6 | 0 ok |
| `runskip20x1` | 3 -2/+2 | 0 ok | 0 ok | 0 ok | 0 ok | 0 ok | 6 -1/+1 | 0 ok | 0 ok | 3 -1/+1 | 0 ok | 0 ok | 0 ok | 0 ok | 0 ok |
| `noneskip5` | 88 -2/+0 | 57 ok | 7 ok | 13 ok | 13 ok | 32 ok | 47 -1/+0 | 42 ok | 0 ok | 0 ok | 0 ok | 11 ok | 0 ok | 26 ok | 2 ok |
| `noneskip10` | 63 -2/+0 | 28 ok | 0 ok | 0 ok | 5 ok | 17 ok | 29 -1/+0 | 15 ok | 0 ok | 0 ok | 0 ok | 7 ok | 0 ok | 18 ok | 0 ok |
| `nonecausal1` | 104 -12/+2 | 66 -4/+1 | 6 ok | 17 -2/+0 | 19 -2/+0 | 43 -7/+2 | 66 -7/+3 | 64 -9/+1 | 2 ok | 1 ok | 2 -2/+1 | 13 -2/+1 | 0 ok | 31 -3/+2 | 4 -6/+5 |
| `nonecausal3` | 47 -8/+1 | 16 -1/+0 | 0 ok | 1 ok | 2 ok | 10 -1/+0 | 19 -2/+3 | 10 -1/+0 | 0 ok | 0 ok | 0 ok | 2 ok | 0 ok | 12 ok | 0 ok |
| `nonecausal6` | 17 -4/+1 | 1 ok | 0 ok | 0 ok | 0 ok | 0 ok | 3 ok | 0 ok | 0 ok | 0 ok | 0 ok | 0 ok | 0 ok | 0 ok | 0 ok |

(`scripts/simulate_sampling.py` re-run on the final tree reproduces these numbers exactly.)

**Verdict: no policy qualifies.** Every policy that skips matched windows breaks on-air times on
almost every mix, and most also break labels (rows go `short`, badges drop from `likely` to
`possible`, and `contradicted` rows disappear). Skipping only inside long no-match stretches
comes closest. Even the oracle loses unidentified-gap rows (`068c85d9` 2616–2715 s and
4470–4542 s; `5cad3fdb` 5595–5649 s). The one causal variant with no visible loss on 14 mixes
(`nonecausal6`) saves 0.3 % and still changes `5cad3fdb`. Nothing ships. A future fusion that
needed less window density would have to be designed and benchmarked for that; it is not a
sampling change.

## 2. 429 handling: what shipped

`src/id_detector/shazam.py`: `TokenBucket` gains an opt-in **pause on throttle**
(`throttle_pause_s > 0`). It is off by default for every other caller, so the paid AudD sweep's
bucket is unchanged.

* A 429 or 503 still halves the rate toward the floor, with a 2 s cooldown so that one burst
  counts as one cut (unchanged).
* When two throttles arrive in a row with no answer between them (`throttle_streak = 2`), the
  whole pool pauses for 10 s. Each throttled probe doubles the pause, up to 60 s, with a
  deterministic 0.75×–1.25× golden-ratio jitter. While paused, only one probe is in flight. An
  answer ends the pause, and the rate climbs back additively, as before. A throttle from a
  request sent before the pause began does not lengthen it.
* A **Retry-After** header (seconds or an HTTP date) pauses the whole pool for that long, even
  for a single throttle. Before, it only delayed the one window's retry.
* **Give-up**: if a paused pool has had no answer for an hour, every remaining window fails at
  once, before anything is sent (no request, no budget used, no journal dispatch). The run ends
  `partial`, as it always has when Shazam does not answer.
* `InjectedHTTPClient` passes Retry-After to `penalize`, and frees the probe slot (`settle`)
  after every outcome.

`src/id_detector/recognise.py`: the default Shazam limiter built by `recognise_generation` (the
path that the CLI, the web app and the queue all use) turns this on (`SHAZAM_THROTTLE_PAUSE_S=10`,
`_MAX_S=60`, `_STREAK=2`, `_GIVE_UP_S=3600`). The per-window retry decision moved into
`next_attempt_after_failure`, which the job runner and the replay proof both use. Under a pausing
limiter, a 429 or 503 is asked again after the pool's pause and does **not** count toward the
window's `MAX_RETRIES` (a backstop of 120 throttled asks per window remains). Every other failure
keeps the old 2 s-doubling backoff and the 5-retry cap. An injected HTTP boundary (the fakes)
bypasses the limiter, so it keeps the old behaviour.

"Re-queue for later" is done in place: the window keeps its lease (the heartbeat keeps it alive)
and waits, with the rest of the pool, for the pause to end. Nothing is sent meanwhile, so the
effect is the same as putting it back in the queue, and the attempt journal's parent chain stays
linear. Every attempt is still journalled `prepared → dispatched → resolved`, and only attempts
actually sent count toward the request budget and the breaker's daily allowance.

**Progress bar:** `windows_total` still equals the windows that will actually be asked, since
nothing is skipped. `windows_done` still advances once per finished window. During a pause the
bar holds, which the reworked wall-clock ETA already handles (`test_progress_wallclock.py`
untouched and green).

**Cached mixes / re-fuse upkeep:** no observation, query, cache key, fusion rule or stored-result
contract changed. Cache hits never reach the limiter, and re-fusion never touches recognise. No
version bump is needed, and none was made.

## 2a. The 429 replay

`tests/fixtures/shazam/throttle-replay-2026-10-21.json` keeps exactly two things per request
from that job's `recognise/shazam-attempts.jsonl`: when it was sent (ms after the first request)
and whether it was a 429 or answered. That is 1,518 requests and 811 429s. The log shows three
near-total blocks (minutes 9–30, 40–60 and 69–90) and short bursts about every two minutes in
between.

Two fake Shazams, each driven on a **simulated clock** (an asyncio loop that jumps to the next
timer; no real time and no network):

* **Replay**: a request sent at time t gets the outcome of the owner's request sent most
  recently before t. The 96-min log repeats after it ends, following its ~30-min cycle.
* **Quota** (a model of the cause): at most 200 answers per 30 min and 24 per minute. 429s cost
  nothing.

The sweep under test is the real request path: the real `TokenBucket`, the real
`InjectedHTTPClient` over `httpx.MockTransport`, the real `next_attempt_after_failure`, and the
end-of-sweep retry pass's rule (one more ask per errored window, at most 10 %). Settings are
today's defaults: 45/min ceiling, 3 workers, 808 windows. "Old" is the same code with the pause
off. Running the **original HEAD source** through the same replay (`time.monotonic` patched to
the simulated clock, old retry loop verbatim) gives identical numbers: replay 100.4 min / 1,603 /
834 / 39 unanswered, and quota 98.4 min / 1,538 / 771 / 41.

| Model | Code | Wall time | Requests | 429s | Answered | Unanswered | Time to old's coverage |
|---|---|---:|---:|---:|---:|---:|---:|
| Replay | old | 100.4 min | 1,603 | 834 | 769 | 39 | — |
| Replay | **new** | 102.4 min | **946** | **138** | **808** | **0** | 100.5 min |
| Quota | old | 98.4 min | 1,538 | 771 | 767 | 41 | — |
| Quota | **new** | 122.3 min | **951** | **143** | **808** | **0** | 99.2 min |

(For calibration, the real run took 96 min and made 1,518 requests, 811 of them 429s, leaving
101 windows unanswered. The old code on the replay reproduces that closely.)

The new code's wall time is longer only because it answers the 39–41 windows the old code gave
up on. Under the quota those last windows have to wait for Shazam's allowance to refill.

**Parameters were chosen from a sweep, not guessed** (808 windows, both models):

| Variant | Replay: wall / 429s / time to old coverage | Quota: wall / 429s / time to old coverage |
|---|---|---|
| pause 5–60 s | 103.7 / 135 / 101.8 | 122.0 / 143 / 99.7 |
| pause 10–60 s | 126.8 / 151 / 102.6 | 122.8 / 140 / 99.6 |
| pause 15–60 s | 132.5 / 140 / 130.6 | 122.3 / 128 / 99.7 |
| pause 10–120 s | 130.9 / 110 / 128.5 | 123.5 / 102 / 101.1 |
| pause 15–300 s | 198.0 / 96 / 195.8 | 130.0 / 75 / 105.4 |
| **pause 10–60 s, streak 2 (shipped)** | **102.4 / 138 / 100.5** | **122.3 / 143 / 99.2** |
| pause 10–120 s, streak 2 | 102.7 / 116 / 101.5 | 125.3 / 107 / 102.4 |
| pause 5–60 s, streak 2 | 129.5 / 194 / 104.6 | 121.9 / 147 / 98.6 |
| 10–60 s + learned ceiling (rejected) | 225.6 / 258 / 200.9 | 123.0 / 96 / 100.8 |

Rejected along the way:

* A "learned ceiling" (recover only to 0.9× the throttled rate). It ratchets the rate down to
  the floor, doubling the replay's time.
* Un-jittered pauses. Probes every 120 s fell into step with Shazam's ~2-minute burst cycle and
  missed every open moment for an hour.
* Long maximum pauses (300 s). They lose minutes after each block ends.

Replay wall times fall into two groups (about 102 or about 127 min), depending on whether the
last windows finish before the fourth block. "Time to old coverage" is the stable measure.

Side benefit: the breaker's daily allowance (2,000 Shazam requests per egress per day) used to
lose 834 of a 1,603-request scan to 429s. The same scan now uses about 946 requests, which
leaves room for a second mix that day.

## 3. Tests added (`tests/test_shazam_throttle_replay.py`, 8 tests)

1. `test_the_replayed_2026_10_21_throttle_wastes_far_fewer_requests_and_answers_every_window`
   (replay: all 808 answered, ≤ 1/5 of old 429s, ≤ 70 % of old requests, old coverage reached
   within +5 % of old wall time)
2. `test_against_a_sliding_quota_the_new_path_answers_everything_and_wastes_little`
3. `test_retry_after_is_honoured_by_the_whole_pool` (nothing new sent inside a 120 s Retry-After)
4. `test_a_pool_that_never_hears_back_gives_up_without_sending_more` (old: ≥ 1,200 requests for
   200 windows; new: < 100, done within the give-up hour)
5. `test_the_pause_is_off_for_other_callers_of_the_token_bucket` (AudD/default bucket unchanged)
6. `test_a_throttled_probe_doubles_the_pause_and_an_answer_ends_it` (streak, doubling, cap,
   jitter, sibling throttles do not lengthen the pause)
7. `test_only_one_probe_is_in_flight_while_paused`
8. `test_the_real_recognise_path_waits_out_a_block_and_answers_every_window`: end to end through
   `recognise_generation` with its OWN default limiter (offline `MockTransport`). Shazam refuses
   the first 25 requests. The new code ends with 0 failures and exactly windows + 25 requests.

**Proof they fail on the old code** (HEAD `src/` extracted with `git archive` and put first on
`PYTHONPATH`):

```
$ PYTHONPATH=<old>/src pytest tests/test_shazam_throttle_replay.py
ERROR tests/test_shazam_throttle_replay.py      (collection: the old code has no pause / decision API)
!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!

# test 8's body, with the new-only names made optional, run against the old code:
--- OLD src
E       assert 3 == 0          (result.failures: every window left an error after 6 tries + 1 retry)
1 failed, 1 warning in 4.22s
--- NEW src
1 passed, 1 warning in 4.46s
```

The behavioural "old" side of tests 1, 2 and 4 is the original HEAD code, as shown in §2a (the
same numbers to the request).

## 4. Outputs

Shard A and shard B are run as the brief specifies, but each is split into sub-shards because
the tool limits one foreground command to 10 minutes. Together the sub-shards cover exactly the
brief's file sets. All are foreground, with `IDEA_TEST_MODE=1` and an empty `AUDD_API_TOKEN`.
No load-sensitive test needed a re-run.

```
$ pytest tests/idea_web                                  # shard A, part 1
397 passed, 1 warning in 433.00s (0:07:13)
$ pytest tests/test_[a-h]*.py                            # shard A, part 2
387 passed, 1 warning in 397.72s (0:06:37)
$ pytest tests/test_[i-o]*.py                            # shard A, part 3
21 passed, 1 warning in 7.39s
$ pytest tests/test_phase*.py                            # shard A, part 4
413 passed, 1 warning in 343.56s (0:05:43)
                                       shard A total: 1218 passed, 0 failed
$ pytest $(ls tests/test_[p-r]*.py | grep -v test_phase) # shard B, part 1
95 passed, 1 warning in 236.70s (0:03:56)
$ pytest tests/test_s*.py                                # shard B, part 2
613 passed, 1 skipped, 93 deselected, 1 warning in 285.73s (0:04:45)
$ pytest tests/test_[t-z]*.py                            # shard B, part 3
303 passed, 1 skipped, 4 deselected, 1 warning in 151.93s (0:02:31)
                                       shard B total: 1011 passed, 2 skipped, 97 deselected (slow/live by default), 0 failed
$ pytest tests/test_shazam_throttle_replay.py
8 passed, 1 warning in 7.23s
$ uv run ruff check .
All checks passed!
$ uv run ruff format --check .
456 files already formatted
$ uv run python scripts/audit_fixtures.py
audited 591 files
fixture audit passed
$ git diff --stat
 src/id_detector/recognise.py |  83 ++++++++++++++++++++++--
 src/id_detector/shazam.py    | 148 ++++++++++++++++++++++++++++++++++++-------
 2 files changed, 203 insertions(+), 28 deletions(-)
$ git status --short
 M src/id_detector/recognise.py
 M src/id_detector/shazam.py
?? docs/reviews/build-scan-speed.md
?? scripts/simulate_sampling.py
?? tests/fixtures/shazam/throttle-replay-2026-10-21.json
?? tests/test_shazam_throttle_replay.py
```

The shards ran on the final `src/` and test code. Added after them: only `scripts/simulate_sampling.py` (no test imports or scans `scripts/`) and this report (the fixture audit, re-run above, covers it).
The untracked files are new: the report, the simulation script, the replay fixture (13.6 KB) and the test file. `git diff --stat` shows only tracked files.

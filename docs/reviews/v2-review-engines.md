# IDea — adversarial review of engine integration, sequencing, cost, speed and dead weight

Scope: read-only review of the engine/fusion/sequencing path as input to a hosted-product plan.
Repo `C:/Users/natha/Documents/Music/id-detector`, branch `main` at `27c36fd`. No files modified, no
network calls, no `git` writes. Everything below is from reading the code; where I verified a claim
mechanically I say how. Speculation is labelled **[SPECULATIVE]**.

---

## 1. Findings, ranked by severity

### CRITICAL

---

**C1 — Paid-first (`max_accuracy`) crashes with `UnboundLocalError` *after* the money is spent.
`src/id_detector/cli.py:806` and `src/id_detector/cli.py:811`.**

`matches` and `recognised` are read at lines 806 and 811, but their only binding sites are inside
`if not paid_first:` at `cli.py:507-513`. When the paid engine leads, that branch never runs.

Verified mechanically, not by eye — `dis` on `_analyse.__code__` reports exactly one `STORE_FAST`
for each name (both inside the free branch) and `LOAD_FAST_CHECK` for both at the summary lines.
`LOAD_FAST_CHECK` is precisely the opcode CPython emits for a local that may be unbound; it raises
`UnboundLocalError`.

Failure scenario (the shipped web path): user picks "Max accuracy" →
`webapp/runner.py:149-151` injects `WEB_PAID_ENGINE = "audd"` into the engine set and
`webapp/runner.py:167` sets `primary_engine="audd"` → `cli.py:477` makes `paid_first` True → AudD
runs over **all ~400 windows** (~$2.00) → `primary_clip.observations` is non-empty so `paid_first`
stays True → the full Shazam pass is skipped → hints, fuse, gap-fill, export and
`present/index.html` all complete → line 811 raises → `except Exception` at `cli.py:858` writes a
`failed` invocation entry and re-raises → `JobManager._execute` (`webapp/jobs.py:399-404`) marks the
job FAILED with "cannot access local variable 'matches'". `ctx.set_result(...)` in
`webapp/runner.py:181-185` is never reached, so the user is not even given the page that was
written. The customer paid for the paid tier, waited ~15 minutes, and got a red error.

Why nothing caught it: `grep -rn "primary_engine\|paid_first" --include=*.py .` returns exactly one
hit outside `cli.py` — `webapp/runner.py:167`. **No test anywhere sets `primary_engine`.** And the
two full-pipeline test modules (`test_stage2b_pipeline`, `test_stage4c_generations`) are force-marked
`slow` by `tests/conftest.py:14-26` and deselected by the default `addopts`, so the default suite
never drives `_analyse` end to end at all.

Fix: bind both unconditionally. E.g. right after the engine branch:
`matches = [o for o in gen0_observations if o.status == "match"]`, and report failures from
`recognised` only when the free branch ran (`free_failures = recognised.failures if not paid_first
else 0`). Then add one integration test that runs `_analyse` with `primary_engine="audd"` and a fake
adapter — the `paid_scan_adapters` injection point already exists (`cli.py:390`).

---

**C2 — Paid-first structurally destroys the one thing AudD is actually worth: corroboration.**
`src/id_detector/cli.py:663-671` + `src/id_detector/scan_targeting.py:120-137`.

Your own live testing says AudD's value on real mixes is *corroboration*, not new recall. The
mechanism is `_engine_corroborated` (`fuse/episodes.py:148-156`), which fires when ≥ 2 distinct
providers voted on one occurrence. That flag is load-bearing in three places:

- `scan_targeting.py:23` — `CONFIDENT_FLAGS` → the span stops being a paid target;
- `fuse/episodes.py:719-721, 743-744` — makes an episode immune to `scatter`/`contradicted`/`buried`
  suppression;
- `present/exports.py:139` — exempts the row from the 30 s on-air floor, the single change that
  fixed precision on your benchmark (60 rows → 36).

In paid-first, the free engine runs **only** over `select_gap_targets(...)`, which is the complement
of `any_coverage` — the spans where AudD produced *no observation of any confidence*. By
construction the two engines' matched spans are disjoint. `_engine_corroborated` can therefore never
return True in paid-first mode. The mode spends ~4× more money and deletes the precision mechanism
that justified paying at all.

Fix: never exclude AudD-covered spans from the free pass. Corroboration requires overlap by
definition.

---

**C3 — Adding AudD to a run *downgrades* tiers, because AudD observations have no anchor and win the
per-trial selection.** `src/id_detector/providers/audd.py:357,361` +
`src/id_detector/fuse/alignment.py:85-94,115-124,137-174`.

Two bugs compounding:

1. `clip_response_to_observation` sets `anchor=None` (`audd.py:361`). `point_from_observation`
   (`alignment.py:115-124`) drops any observation without a reliable anchor, so an AudD-only
   candidate gets `alignment=None` → `has_global=False` → `episodes.py:499-505` can never award the
   `likely` tier. **AudD evidence alone can only ever reach `possible`.**
2. `select_logical_trial_points` (`alignment.py:137-174`) groups observations by
   `(logical_trial_id, source_key)` and keeps exactly **one** per group. AudD reuses the window's
   `logical_trial_id` (`audd.py:357`) and sets no `simultaneous_source`, so a Shazam match and an
   AudD match on the *same window for the same track* land in the same group. The tie-break is
   `min(eligible, key=lambda i: (_native_skew_cost(i[0]), i[0].id))`. `_native_skew_cost`
   (`alignment.py:85-94`) reads `native["matches"][…]["frequencyskew_e6"/"timeskew_e6"]`, which AudD
   never emits → cost `0`; Shazam's is usually non-zero → **AudD deterministically wins every
   contested window** and Shazam's observation is pushed into `hypothesis_rejected`
   (`episodes.py:534`), which then raises a `hypothesis_rejected` flag on the episode
   (`episodes.py:549-550`).

Concrete failure: a track Shazam matched cleanly over 7 windows (T_ind ≥ 4, span ≥ 40 s, global
alignment → badge `likely`). Turn on AudD; AudD agrees on all 7; all 7 selected votes are now
anchor-less AudD observations; `has_global` is False; badge drops to `possible`; the episode gains a
spurious "hypothesis rejected" flag; and if its on-air union is under 30 s it is now *hidden*
by `short_track`. Free-first + AudD-on-uncertain is only partially protected because AudD matches a
*subset* of Shazam's windows, leaving some Shazam-owned trials in `votes` — which is also why
corroboration works there today, by accident rather than by design.

Fix: make the second engine a separate trial source — set
`native["simultaneous_source"] = "audd"` on clip observations (the fuser already keys on it,
`alignment.py:171` / `episodes.py:275-283`), or key the group on `(trial, provider)`. Independently,
`_engine_corroborated` should read `evidence` (all observations in the bucket) rather than `votes`
(selected only), so agreement is counted even when selection collapses.

---

**C4 — AudD error responses are written into the permanent content-addressed cache, poisoning the
mix.** `src/id_detector/paid_clip.py:170-190`.

`paid_clip.py:180` does `atomic_write_json(raw_path, canonicalize_provider_json(response))` **before**
the response is parsed. AudD returns HTTP 200 with `{"status": "error", …}` for out-of-credits,
quota-exceeded and several auth failures; `clip_response_to_observation` then raises at
`audd.py:313-314`, the window is logged and skipped — but the error JSON is already on disk at
`recognise/invocations/live-audd-clip-v1/raw/<cache_key>.json`, keyed only by clip content. There is
no TTL and no state check on the read path (`paid_clip.py:170-176`), unlike `recognise.cache_valid`
(`recognise.py:189-205`) which is explicit that "errors are never cacheable".

Failure scenario: trial credits run out 200 clips into a mix. You top the account up. Re-running
that mix reads 200 cached error blobs and silently produces zero AudD evidence forever. The only
escape is `--refresh`, which re-bills *everything* including the 200 clips you already paid for.

Fix: cache only after a successful parse, or gate the cache read on
`isinstance(cached.get("result"), (dict, type(None))) and cached.get("status") == "success"`.

---

### HIGH

**H1 — No budget, no cap, no spend accounting on the whole-mix paid pass.**
`cli.py:495` passes `max_clips=len(windows.records) + 1` with the comment "the whole mix, no per-mix
cap". `--max-paid-clips` / `DEFAULT_MAX_CLIPS = 150` are simply not applied in paid-first.
`PaidScanResult.usd_e2` (`scan.py:145`) is never populated by `run_paid_clip_recognition`, and
`_analyse` journals `costs={"usd_e2": 0}` unconditionally at `cli.py:820`, `:850`, `:864`. `AppConfig`
has no USD field at all, so the frozen profile's `budget.max_usd_e2` is dropped by
`profile_app_config` (`profiles.py:741-753`) and never reaches the clip path. A 3-hour set is ~1,200
windows = **$6.00 with no ceiling and no record that it happened**. For an operator-pays product this
is the single most important missing control.

**H2 — Gap-fill request counts are overwritten with zero.** `cli.py:676` does
`counts["requests"] = counts.get("requests", 0) + gap_rec.requests`, then `cli.py:682` calls
`_refuse_with`, whose `counts.update` at `cli.py:649-655` sets `counts["requests"] =
orchestrated.requests`. In paid-first `gen0_requests` is 0 (`cli.py:481`, never assigned) and rescans
are off, so `orchestrated.requests` is 0 — the gap-fill's real Shazam usage is wiped.
`failures` and `cache_hits` also stay 0 in paid-first. `invocations.jsonl` is the only per-run
telemetry that exists, so per-mix unit economics cannot be measured from it.

**H3 — Silent downgrade from paid tier to free tier, with no signal to the user.**
`cli.py:499-506`: *any* condition that leaves `primary_clip.observations` empty flips
`paid_first = False` and quietly runs the free engine as if that were the plan. That path is taken
for: no `AUDD_API_TOKEN` (`audd.py:73-77` → `ProviderUnavailable`, `paid_clip.py:147-150`); an
invalid token (every window raises `ProviderProtocolError("AudD HTTP 401")` at `audd.py:435-436`,
caught and `continue`d at `paid_clip.py:182-185`); an exhausted quota; and a network outage
(`AmbiguousProviderOutcome`). The only trace is a line in the progress ring buffer. Nothing in
`present/index.html`, the exports, or the invocation journal records which tier actually ran. The
customer is billed for max accuracy and receives free.
Fix: return a status ("unavailable" vs "ran, no matches") from `run_paid_clip_recognition`, surface
it in the page header and the export JSON, and fail the job rather than degrading silently.

**H4 — The paid pass has no cancellation point and reports no progress.**
`run_paid_clip_recognition` is a plain sequential `for window in selected:` loop
(`paid_clip.py:161-197`); `log` is called only on an error or once at the end (`:203`).
`JobContext.check_cancel` is reached only through `progress()` (`webapp/jobs.py:211-214`). So a
max_accuracy job is **uncancellable for the entire paid phase** (400 sequential HTTP uploads) and
keeps billing after the user clicks cancel. Worse, `_report(progress, "recognise", 0, 1, …)` at
`cli.py:484` sets `windows_total=1, windows_done=0` (`jobs.py:216-221`), so `eta_seconds()` computes
`1/45*60 ≈ 1 second` and the progress page tells the user the job is nearly done for the whole
10-minute paid phase.

**H5 — The paid pass is sequential, unrated and unretried; transient failures both lose the window
and re-bill next run.** `paid_clip.py` has no `TokenBucket`, no concurrency, and no retry loop —
compare `recognise.py:274-290`, where Shazam gets 5 retries with backoff on 429/5xx. A timeout
becomes `AmbiguousProviderOutcome` (`audd.py:433-434`), is caught at `paid_clip.py:182`, the window
is dropped, and **no cache entry is written** — AudD may well have billed the request, and the next
run bills it again.

**H6 — Documented `idea.toml` knobs are silently ignored whenever a profile is active, and
`config show` lies about it.** `profile_app_config` (`profiles.py:738-753`) builds a fresh
`AppConfig` from the profile; the CLI then `replace(...)`s only 9 fields (`cli.py:990-1010`), and
`webapp/runner.py:57-72` mirrors the same list. **Not carried over:** `recognise_concurrency`,
`shazam_requests_per_minute`, `collapse`, `same_track_bridge_ms`, and — most importantly —
**`present_min_track_ms`**. All five are written into the user's `idea.toml` by `config init`
(`config_template.py:54-55, 109-115`) and printed as effective by `idea config show`, which never
consults the profile (`cli.py:303-318`). So the operator cannot tune the 30 s on-air floor (the main
precision/recall dial) or the request rate (the main throughput dial), and the tool reports values
it does not use. Since `default_profile` in the config also triggers this, it bites even without
`--profile`.

**H7 — Hosted-deployment blockers in the job manager and server.**
- `webapp/jobs.py` is one in-memory worker thread processing one job at a time
  (`JobManager._run_loop`, `_next_job`). No persistence: a restart loses every queued and running
  job silently. No auth, no tenancy, no per-user quota. With 9–20 min per 60-min mix, ten concurrent
  users means the tenth waits 2–3 hours.
- `validate_target` (`jobs.py:57-79`) accepts **any existing local path**. `ingest` copies it into
  `work/` (`ingest.py:186-190`) and `present/server.py:1204-1257` serves it back over
  `/jobs/<id>/audio`. The moment the server is reachable by anyone but the owner, that is arbitrary
  local-file read and exfiltration. Loopback binding is currently enforced
  (`server.py:1447-1448`), so this is a "must fix before hosting", not a live vuln.
- `ProcessLock.acquire` (`jobs.py:57-90`) is **non-blocking**: it raises `JobStoreLocked`, it does
  not queue. Any horizontal scaling breaks on the most likely collision of all — two users analysing
  the same mix, which hits the same content-addressed `media_dir/.media.lock`. That collision is
  exactly the case the content-addressed cache is supposed to make cheap.

**H8 — `upload_consent` and `build_index` are still accepted from the POST body after being removed
from the form.** `present/server.py:1373-1406` reads both from JSON and form bodies; the rendered
form only has the profile radios (`server.py:811-814`). An API caller sending
`{"upload_consent": true}` makes `run_paid_scanners` (`cli.py:527-547`) run the **whole-file
enterprise AudD scan** — the path `scan.py`'s own module docstring says must not be extended —
*in addition to* the clip path. That is a second, larger, unbudgeted charge triggered by an
untrusted client-controlled field.

**H9 — The web app's "build reference index" builds an index that is never queried.**
`webapp/runner.py:130-135` runs `_run_build_index` (downloads and fingerprints the uploader's
tracks), but the `cli._analyse(...)` call at `webapp/runner.py:155-172` passes no
`local_index_label`, so `want_index` at `cli.py:636` is always `False` in the web app. The entire
Panako discovery/download/fingerprint cost is paid and then discarded.

---

### MEDIUM

**M1 — `engine_corroborated` treats two commercial catalogues as independent.**
`_engine_corroborated` (`episodes.py:148-156`) counts distinct provider strings. The codebase
already encodes the dependence prior — `COMMERCIAL_PROVIDERS = {"audd","acrcloud"}` and
`discounted_providers` (`episodes.py:92-108`) halve a second commercial engine's trial weight — but
the corroboration *flag* ignores it entirely. Given your live finding that ACRCloud ≈ AudD in
coverage, `audd + acrcloud` agreeing would grant the confident tier, suppression immunity and floor
exemption on what is effectively one catalogue queried twice.
Fix: require the corroborating provider to be outside `COMMERCIAL_PROVIDERS` (shazam or panako), or
require ≥ 2 *independent families*.

**M2 — Spectral novelty is recomputed on every fuse and is dead weight with rescans off.**
`run_generation_loop` calls `compute_novelty_change_points` at `orchestrate.py:140` on every
invocation, and `_analyse` calls `_fuse` 1–3 times (initial + one per `_refuse_with`). Each call
re-reads the whole ~115 MB PCM and runs a full log-mel/flux pass (`novelty.py:174-193`, 36,000
frames for a 60-min mix). With `LIVE_DEFAULT_MAX_GENERATIONS = 0` the change points are consumed by
nothing. Skip it when `max_generations == 0` and hoist it out of the re-fuse loop.

**M3 — AudD phantoms block the free gap-fill in exactly the regions paid-first claims to hand to
Shazam.** `select_gap_targets` uses `any_coverage` (`scan_targeting.py:60-68`) = every non-suppressed
episode, *including* lone-window matches that the 30 s presentation floor will later hide. A single
AudD false positive removes ~12 s (plus 3 s padding each side, plus 4 ms-scale bridging effects) of
audio from the free pass. Apply the same on-air floor when computing coverage.

**M4 — Hint-only ("crowd ID") rows are order-sensitive and can duplicate.**
`episodes.py:797-845` iterates `sorted(hints, key=lambda item: item.id)` — an opaque content hash —
so when two comment answers name different tracks for the same gap, which one becomes the listed row
is effectively arbitrary rather than by position, reply-count or author trust. Separately,
`listed_spans` (`episodes.py:790-794`) is computed once and never extended with the hint-only
episodes the loop creates, so two contradictory crowd answers at the same timestamp both get listed
as separate tracks.

**M5 — `--refresh` silently re-bills the paid engine.** `refresh` flows straight into
`run_paid_clip_recognition` (`cli.py:494` → `paid_clip.py:170`) and bypasses the raw cache. The
flag's help text says only "Bypass positive/no-match TTLs" (`cli.py:884`). On a max_accuracy mix
that is another ~$2.00 with no confirmation prompt.

**M6 — `--profile max_accuracy` on the CLI is byte-for-byte the free profile.** Both frozen profiles
list `enabled_engines: ["shazam"]` (`profiles/max_accuracy-v1.json`), and `paid_first` requires
`"audd" in enabled_engines` (`cli.py:477`). So the CLI needs `--profile max_accuracy --engine audd`;
`--profile max_accuracy` alone gives the free tier under a paid-sounding name. The web app papers
over this by injecting `WEB_PAID_ENGINE` (`runner.py:149-151`), so CLI and web disagree about what
"max accuracy" means.

**M7 — `_load_cached` is O(all mixes ever analysed) plus a full-file SHA-256 on the hit.**
`ingest.py:143-167` globs `work/*/*/ingest/source.json`, parses every one, and on a match hashes the
entire original (`sha256_file(original_path) == record.media_key`) plus verifies a completion
sidecar. This runs on every `analyse`, on `_load_cached` in the web runner (`runner.py:181`) and on
every `acquire`. At 1,000 hosted mixes that is 1,000 JSON parses plus a ~100 MB hash per run.
An index (sqlite or a single JSON map keyed by canonical URL) removes it.

**M8 — Disk grows ~330–490 MB per mix and nothing ever reclaims it. Measured on this machine.**
`du -sh work` = **4.9 GB across 10 mixes** (range 208 MB – 750 MB). Breakdown of the 60-minute
DJ Three set (`work/c5dc…/ec0a…`, 332 MB total):

| Component | Size | Needed after the run? |
|---|---|---|
| `windows/` — 400 × 12 s WAV clips | **147 MB** | No (recognition only; re-derivable from PCM) |
| `decode/` — canonical 16 kHz mono s16 PCM | **110 MB** | No (novelty + rescans only, both off by default) |
| `ingest/` — downloaded original | 70 MB | Only to re-derive; could go to cold storage |
| `recognise/` — raw provider JSON | 5.0 MB | Yes — this is the actual cache asset |
| `fuse/` + `present/` + `jobs.sqlite` | < 1 MB | Yes |

So **77 % of the footprint (`windows` + `decode`, 257 of 332 MB) is disposable the moment the fuse
succeeds**, and the piece that carries the real cross-user value — the content-addressed raw
responses — is 1.5 % of it. At 1,000 hosted mixes that is ~400 GB of which ~310 GB is garbage; disk
will dominate hosting cost long before the AudD bill does.

**M9 — `_windows_in_spans` does not filter transformed windows.** `cli.py:262-276` keeps every
gen-0 record whose start falls in a span, unlike `paid_clip._windows_in_targets`
(`paid_clip.py:100-116`) and `local_index._windows_in_targets`, which both skip
`transform.type != "none"`. Harmless while `transforms_policy == "rescan_only"` (the frozen value),
but becomes a ~9× request multiplier on the gap-fill the moment anyone sets `policy = "global"`.

**M10 — In paid-first the fuser is told the whole mix was scanned by the free engine.**
`run_generation_loop` passes `scanned_window_shapes=window_shapes(all_windows)` and `fuse_generation`
computes `scanned` from every window (`episodes.py:846`), but in paid-first only AudD saw most
windows and there are no Shazam `no_match` observations outside the gaps. `GapEvidence.n_windows` /
`n_no_match` (`episodes.py:869-874`) and the coverage statistics on the page therefore describe a
pass that never happened.

---

### LOW

- **L1** — `cli.py:19-31` imports `benchmark/`, `calibrate/` and `truth` at module import, so every
  invocation pays for subsystems no shipped command touches. Measured:
  `import id_detector.cli` = **3.17 s** vs `import id_detector.fuse.episodes` = **1.04 s** → ~2.1 s
  of pure startup tax on `idea analyse`, `idea serve`, everything.
- **L2** — `PROJECT_ROOT = Path.cwd()` is captured at import time (`cli.py:97`), so profile lookup,
  provider-config lookup and `.env` loading silently depend on the working directory a service is
  launched from.
- **L3** — `run_paid_scanners` runs on every free-first `--engine audd` run before the clip path
  starts (`cli.py:527-547`) and logs "paid engine audd skipped: <consent reason>", which reads to a
  user as "the paid engine didn't run" when in fact the clip path is about to.
- **L4** — `--engine acrcloud` cannot reach the clip path: `PAID_CLIP_ENGINES = ("audd",)`
  (`paid_clip.py:52`), so `want_clips` (`cli.py:635`) is False and the flag silently means
  "whole-file, consent-gated" — despite `ACRCloudAdapter.recognize_clip` existing at
  `providers/acrcloud.py:144` and being tested by `tests/test_acrcloud_clip.py`.
- **L5** — `discounted_providers` attributes each trial to `min(provider)` alphabetically
  (`episodes.py:131-132`), so a trial with acrcloud+audd+shazam votes is attributed to `acrcloud`
  and may be discounted even though Shazam also voted.

---

## 2. Cost / speed / accuracy per mode — 60-minute mix

**Assumptions (all derived from code constants except where noted):**

- `window_ms=12000`, `hop_ms=9000`, `phase_ms=0` (`providers/base.py:14-19`, frozen in both
  profiles). `schedule_windows` (`windows.py:125-138`) → starts `0…3,582,000` step `9,000` (399) plus
  a `tail` start at `3,588,000` = **400 gen-0 windows**. Confirms your observed ~400.
- Rescans off (`LIVE_DEFAULT_MAX_GENERATIONS = 0`), transforms `rescan_only` → no transform variants
  at generation 0.
- Shazam throughput: ceiling 45 req/min, concurrency 3 (`providers/base.py:49-50`), degrading to
  ~20/min under 429s → 400 windows in **9–20 min cold**; cached runs are seconds.
- AudD: $0.005/request. Sequential, no limiter, no concurrency (`paid_clip.py:161-197`); assume
  ~1.5 s per 12 s clip upload+response → ~1.5 s × N. **[SPECULATIVE: latency not measured live.]**
- Panako: one JVM launch per window (`local_index.py:183-190`), assume ~2 s each, $0.
- `select_scan_targets` after a free pass: assume ~40 % of the timeline is non-confident → ~160
  candidate windows, capped at `DEFAULT_MAX_CLIPS = 150` and spread by `_subsample_evenly`.
  **[SPECULATIVE — this fraction is the biggest uncertainty in the table.]**
- `select_gap_targets` after an AudD whole-mix pass: AudD covering ~65–75 % leaves ~15–20 min blank
  → ~110–140 windows. **[SPECULATIVE.]**
- Recall anchored on your 30-track benchmark: Shazam-alone ceiling 27/30; 3–4 tracks in no
  commercial catalogue.

| Mode | Shazam req | AudD req | Panako queries | $/mix | Cold wall-clock | Warm | Expected recall | Expected precision |
|---|---|---|---|---|---|---|---|---|
| **free** | 400 | 0 | 0 | **$0.00** | 9–20 min | seconds | ~27/30 (90 %) | good — the 30 s on-air floor does the work (60 rows → 36) |
| **free + audd-on-uncertain (cap 150)** | 400 | ≤ 150 | 0 | **≤ $0.75** | 9–20 min + ~4 min = **13–24 min** | seconds (AudD raws cached) | ~27–28/30 — AudD adds little *new* recall | **best of the four** — corroboration lifts possible→confident and exempts genuine short tracks from the floor |
| **paid-first (as shipped)** | ~110–140 (gaps only) | **400, uncapped** | 0 | **~$2.00** | ~10 min AudD + 3–7 min Shazam = **13–17 min** | seconds | **0/30 today — the run crashes (C1)**; ≤ 27/30 once fixed | **worse than free**: corroboration impossible (C2), no anchors → no `likely` tier (C3) → more real rows fall under the 30 s floor |
| **paid-first + panako** | ~110–140 | 400 | ≤ 400 over uncertain spans | **~$2.00** | above + 3–13 min of JVM launches | seconds | +0 on both real mixes you tested | unchanged |

Scaling is linear in duration: a 3-hour set ≈ 1,200 windows → free 27–60 min; paid-first **$6.00**,
still uncapped, still unrecorded.

---

## 3. Is the sequencing right?

**No.** Recommendation: **free-first, paid-to-corroborate-and-fill, with the paid engine *sampled*,
not swept.**

**Why paid-first is wrong as built:**

1. *The speed premise does not hold.* The comment at `cli.py:474-476` justifies paid-first with "the
   paid engine … is ~4-6x faster and has no rate limit". But `paid_clip.py` is a strictly sequential
   loop with no concurrency and no limiter: 400 requests at ~1.5 s ≈ **10 min**, versus Shazam's
   400 windows at 45/min × 3 workers ≈ **9 min**. Paid-first buys somewhere between −1 and +5 minutes
   and costs $2.00.
2. *It deletes the benefit.* C2 — corroboration is impossible when the engines are given disjoint
   spans by construction.
3. *It degrades tiers.* C3 — AudD wins the per-trial selection and carries no anchor, so `likely`
   becomes unreachable.
4. *It is the only mode with no cost ceiling.* H1.

**The rate-limit argument, taken seriously.** It is true that a hosted product cannot hammer an
unofficial Shazam endpoint from one IP for many users — but the conclusion drawn ("make the paid
engine primary") is the expensive answer to a cheap problem. Three cheaper levers exist first:
(a) the cache is already content-addressed by media hash + window (`contracts.clip_cache_key`), so a
popular mix is analysed **once globally** and every later user is free and instant — the more users
you have, the less Shazam you need per user; (b) per-tenant pacing plus a rotating/residential egress
pool costs cents per mix, not dollars; (c) the observed limiter already sustains 20–45 req/min per
egress, which is ~1 mix per 10–20 min per IP — a handful of egresses covers a lot of paying users.
Paid-primary is the answer only when Shazam access actually becomes the binding constraint, and even
then it should be *sampled*, not swept.

**The economic argument against sweeping.** A DJ track plays 3–6 minutes = 20–40 windows. Paying for
40 confirmations of the same track is pure waste. One paid clip every ~45 s is enough to corroborate
every track and to probe every blank.

**Recommended tiers, with numbers (60-min mix):**

| Tier | Sequence | Requests | COGS | Wall-clock |
|---|---|---|---|---|
| **Free** | Shazam over all 400 windows. Unchanged. | 400 Shazam | $0.00 | 9–20 min (seconds warm) |
| **Paid ("Max accuracy")** | Shazam first (or free from cache on a re-analysis) → **one AudD pass of ~80 evenly-spread clips**: ~40 sampled *inside* listed-but-not-confident episodes (corroboration) + ~40 inside blanks (recall) → single re-fuse | 400 Shazam + 80 AudD | **$0.40** | +~2 min |
| **Contingency: Shazam access degrades** (sustained < 15 req/min, or the egress IP is blocked) | AudD sampled at 1 clip / 30 s = 120 requests as primary → Shazam over the **whole** mix at whatever rate is available, for corroboration — never only over gaps | 120 AudD + up to 400 Shazam | **$0.60** | ~15 min |

Margin check: at **$0.40** COGS/mix a paid tier supports a $5–10/month or $1–2/mix retail price with
comfortable margin. At the shipped **$2.00** uncapped (and **$6.00** for a 3-hour set) it does not —
one power user analysing 20 long sets a month costs $120.

Two prerequisites before any of this ships: fix C3 (give AudD its own trial source so both engines'
votes survive) — otherwise corroboration is unreliable in *every* mode — and add a per-run USD
ceiling (H1).

---

## 4. Keep / remove / quarantine

| Subsystem | On shipped path? | Tested? | Value on real mixes | Cost of keeping | Recommendation |
|---|---|---|---|---|---|
| **Rescans + transform grid** (`rescan.py`, `windows.py` transform code, `orchestrate` loop) | Reachable but **off** by default (`LIVE_DEFAULT_MAX_GENERATIONS = 0`) | Heavily: `test_stage4c_rescans` (17), `test_stage4c_events` (12), `test_stage4c_generations` (10, slow-only) | **Zero** real recall; phantoms + hours (your measurement) | Large: the transform grid, rescan window generation, budget planner and half the orchestrator exist for it | **Quarantine.** Keep the code path (the frozen profiles certify `rescans=3` and must stay byte-identical) but treat it as frozen legacy: no new work, no new tests, and delete the transform-grid *ffmpeg* rendering path if the profiles can be re-frozen later. |
| **`calibrate/` ML + `certify`** (5 modules, ~1,500 lines) | `load_calibration` is called (`cli.py:1017`) but returns `None` — no real-mix model is committed | `test_stage5_calibration` (16) | Zero — every tier is `provisional`, `n_test_predictions: 0` per `docs/STATUS.md` | Moderate; also pulls into `cli` import cost (L1) | **Quarantine.** Move behind a lazy import; keep `load_calibration`'s null path on the hot path only. Revisit only if you fund the labelled corpus. |
| **`benchmark/`** (7 modules, ~5,000 lines: scorer, shortlist, ablations, controlled, corpus, hints, transforms_schedule) | Only via `idea benchmark …` | ~45 default tests + 2 slow modules | It produced the frozen profiles; that job is done | High: biggest single chunk of the codebase, imported eagerly by `cli.py` | **Quarantine.** Split into a `dev` extra / separate entry point so `idea analyse` never imports it. Do not delete — re-freezing profiles needs it. |
| **`scan.py` whole-file path + `require_upload_permission`** | Yes, still runs on every free-first `--engine` run (`cli.py:527-547`) | `test_scan` (5), `test_stage4c_scanners` (8) | Zero — superseded by the clip path; its own docstring says do not extend | Moderate — and it is an *active liability* (H8: reachable via an untrusted POST field, double-billing) | **Remove.** Delete the call site at `cli.py:527-547`, drop `upload_consent` from the POST handler (`server.py:1378,1386`), keep `require_upload_permission` only if you ever ship a genuine owner-upload feature. |
| **ACRCloud adapter** (`providers/acrcloud.py`, 840 lines) | Unreachable via the clip path (L4); reachable only via `--engine acrcloud` + consent → whole-file | `test_acrcloud_clip` (4), part of `test_stage3_providers` (32) | Live-tested ≈ same coverage as AudD; recovered 1/8 of AudD's misses | High: 840 lines + tests + a `_Bundle` in `scan.py` + a branch in `discounted_providers` | **Remove** (or archive to a branch). A second commercial catalogue is redundant *and* it is the main way `engine_corroborated` can be gamed (M1). |
| **Panako / local index** (`providers/panako.py` 688 + `panako_setup.py` 178 + `local_index.py` 226 + `candidates.py` 397) | Reachable from the CLI (`--local-index`); **unreachable from the web app** (H9) | `test_stage8_panako` (17), `test_stage8_candidates` (8), `test_local_index` (4) | 0 tracks recovered across 2 real mixes; only helps when the DJ plays their own indexed uploads | High: JDK dependency, jar download, JVM-per-window, ~1,500 lines | **Quarantine.** Keep the CLI path for the one real use case (a DJ analysing *their own* set) and either wire it into the web app or remove the dead `build_index` UI plumbing. Do not invest further. |
| **`novelty.py`** | Runs on every fuse, output consumed by nothing (M2) | Covered inside rescan tests | Zero with rescans off | Small code, but 2–5 s × 1–3 calls per run of pure waste | **Quarantine.** Guard with `if max_generations > 0`. |
| **`truth` commands** (`truth.py`, 710 lines) | Only via `idea truth …` | `test_stage2a_truth` (12) | Zero — no owner-verified corpus exists (`docs/STATUS.md` Stage 5) | Moderate; imported eagerly by `cli.py` | **Quarantine** with `benchmark/`. |
| **Hints: `sc_comments`** | Yes | `test_stage4a_*` | **High** — the only lever that recovers tracks in no commercial catalogue (`hint_only` "crowd ID" rows) | `client_id` scraped by regex from SoundCloud HTML (`connectors/soundcloud.py:23,41-69`) — fragile | **Keep and invest.** This is the differentiator. |
| **Hints: `manual` / pasted tracklist** | Yes (`--tracklist`, web paste box) | Yes | High — universal, no bot-blocking | Small | **Keep.** |
| **Hints: `1001tl`** (`connectors/tl1001.py`, 59 lines) | Yes, enabled | `test_stage4a_connectors` | **Zero** — site is JS-gated; the connector only emits *quarantined* pointers that need `--confirm-mirror` anyway | Small code, but a network call + circuit-breaker slot on every run | **Remove** (or default-disable via `disabled_hint_connectors`). |
| **Hints: `mixesdb`** | Yes | Yes | Low-moderate — only covers well-known sets | Small | **Keep**, low cost. |
| **Hints: `yt_comments`** | Yes | Yes | Moderate for YouTube sources; uses yt-dlp comment extraction (slow, block-prone) | Moderate | **Keep**, but rate-limit/timeout it hard in a hosted deployment. |
| **Hints: `mixcloud`** | Yes | Yes | Low — Mixcloud GraphQL TrackSection is only present when the uploader filled it in | Small | **Keep**, low cost. |
| **Hints: `pointer_import`** (279 lines, allow-listed) | Yes, but requires manual `--confirm-mirror` | Yes | Zero in practice (nobody confirms mirrors interactively in a web product) | Moderate | **Quarantine.** Not reachable from the web UI at all. |
| **CUE / M3U exports** (`present/exports.py`) | Yes | `test_stage9_exports` (5) | Real — DJs import CUE/M3U into Rekordbox/VLC | Small, self-contained, well-tested | **Keep.** |
| **`local_fixture.py`** (556 lines) | Test/benchmark oracle only; excluded from every production profile | Yes | n/a | Moderate | **Quarantine** with `benchmark/`. |

---

## 5. Test-suite health

**Numbers (measured, not estimated):**

- `uv run pytest --collect-only -q` → **581 selected / 674 total (93 deselected)** in 1.59 s.
- 54 files in `tests/`, 14,692 lines.
- Timed samples (wall-clock including ~1.2 s interpreter start per invocation):
  `test_contracts` 1.7 s · `test_stage10_webapp` 6.2 s · `test_stage7_page` 2.4 s ·
  `test_stage4c_rescans` 1.2 s · `test_stage6_enrich` 2.1 s.
  A batch of 11 engine-related files: **84 tests in 4.39 s** → ~33 ms/test marginal.
- **Estimated full default run: ~40–60 s.** Sound and fast.
- The 4 `slow` modules add substantially: `test_stage4c_generations` alone (10 tests) takes
  **28.6 s**. `uv run pytest -m "not live"` is realistically **3–6 min**.

**Structural problems:**

1. **The default suite never runs `_analyse`.** `tests/conftest.py:14-26` marks
   `test_stage2b_pipeline` and `test_stage4c_generations` (the only full-pipeline modules) `slow`,
   and `pyproject.toml` deselects `slow` by default. That is precisely the gap that let C1 (a
   guaranteed crash on the flagship paid path) ship. **Highest-value test-suite fix: one fast
   `_analyse` integration test per mode (free / free+audd / paid-first) using the existing
   `paid_scan_adapters` injection point and a tiny synthetic WAV.**
2. **A test that gives false confidence.** `tests/test_engine_corroboration.py` (3 tests) exercises
   `_engine_corroborated` against hand-rolled `_Vote(provider=...)` dataclasses. It never touches
   `select_logical_trial_points`, so it cannot see that the selection collapse (C3) makes the flag
   unreliable in real runs, nor that paid-first makes it impossible (C2). It reads as coverage of the
   paid tier's central claim while covering none of it.
3. **Tests pinning quarantined/dead subsystems** — roughly **75–100 of the 581 default tests
   (~15 %)**, plus 2 of the 4 slow modules:
   `test_stage2a_scorer` (16), `test_stage2a_truth` (12), `test_stage2b_corpus` (6),
   `test_stage3_shortlist` (9), `test_stage4c_ablations` (7), `test_stage5_calibration` (16),
   `test_stage8_panako` (17), `test_stage8_candidates` (8), `test_acrcloud_clip` (4), plus the
   deselected `test_stage2a_controlled` and `test_stage4b_transforms_schedule`. These are honest
   tests of code that delivers nothing on real mixes; they are not the problem so much as a symptom
   of how much surface the product carries.
4. `test_stage3_entitlements.py` is 100 % `live`-marked — it never runs in CI or locally.

---

## 6. Top 10 improvements, ranked by (accuracy or cost benefit) ÷ effort

1. **Fix the paid-first crash (C1).** Two lines: bind `matches` from `gen0_observations` and guard
   `recognised.failures`. Turns a 100 %-failure flagship tier into a working one. *~15 min for the
   fix; the real work is the integration test that should have caught it.*

2. **Cap and account for paid spend (H1, H2).** Honour `--max-paid-clips` in paid-first, return
   `requests`/`usd_e2` from `run_paid_clip_recognition`, stop clobbering `counts["requests"]` in
   `_refuse_with`, and journal the real figure instead of a hardcoded `0`. Without this you cannot
   price the product. *~2 h.*

3. **Stop caching AudD error responses (C4).** Move `atomic_write_json` after a successful parse.
   One-line move; prevents permanently poisoning mixes analysed during a quota outage. *~20 min.*

4. **Give AudD its own trial source, and read corroboration from `evidence` not `votes` (C3, M1).**
   Set `native["simultaneous_source"] = "audd"` in `clip_response_to_observation`; change
   `_engine_corroborated(votes)` → `_engine_corroborated(evidence)`; require the second provider to
   be outside `COMMERCIAL_PROVIDERS`. Stops AudD from *downgrading* Shazam's `likely` tracks and
   makes corroboration actually reliable. *~3 h including tests.*

5. **Replace paid-first with sampled corroborate-and-fill (§3).** Shazam over the whole mix, then
   ~80 evenly-spread AudD clips split between uncertain-but-listed spans and blanks. Cuts COGS from
   $2.00 to $0.40 per mix (−80 %) and *restores* the precision mechanism. *~1 day.*

6. **Surface the tier that actually ran, and fail instead of silently degrading (H3).** Return a
   status from the paid stage; put "paid cross-check: ran / unavailable (reason)" in the page header
   and the export JSON. Pure trust/billing integrity for a paid product. *~3 h.*

7. **Make the paid pass cancellable, concurrent and retried (H4, H5).** Report progress every N
   clips (which also gives the web ETA something real instead of "1 second"), add 3–4 way concurrency
   with a token bucket, add the same retry/backoff Shazam has. Cuts the paid phase from ~10 min to
   ~3 min and stops billing after cancel. *~4 h.*

8. **Stop dropping `[present]` and `[recognise]` config under a profile (H6).** Carry
   `present_min_track_ms`, `collapse`, `same_track_bridge_ms`, `shazam_requests_per_minute`,
   `recognise_concurrency` through the `replace(...)` in both `cli.analyse` and
   `webapp/runner._resolve_settings`. This is what makes the 30 s on-air floor — your single biggest
   precision/recall dial — tunable at all, and stops `config show` from lying. *~30 min.*

9. **Reclaim disk: delete window WAVs (and optionally the PCM) after a successful fuse (M8).**
   Measured: 147 MB (`windows/`) + 110 MB (`decode/`) of a 332 MB mix — **77 % reclaimed** — both
   re-derivable from the retained original, while the 5 MB of raw provider JSON that carries the
   actual cross-user cache value stays. `work` is already 4.9 GB for 10 mixes.
   *~4 h including a re-derive path.*

10. **Skip novelty when rescans are off, and hoist it out of the re-fuse loop (M2); lazy-import
    `benchmark/`+`calibrate/`+`truth` from `cli.py` (L1).** Removes 2–15 s of pure waste per run and
    2.1 s of startup from every command. Trivially safe. *~1 h.*

*(Runners-up, deliberately below the line: index `_load_cached` (M7) — matters at ~500+ mixes, not
before; and the hosted-deployment work in H7 — necessary before launch, but it is a project, not an
improvement.)*

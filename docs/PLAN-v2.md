# IDea v2 — from a local tool to a product people pay for

*Plan rev 6 · 2026-09-09 · from four independent adversarial reviews (engines/fusion, UI/product, market,
hosting/payments) against `main` @ `27c36fd`; revised after Codex plan-review rounds 1–5
(`docs/reviews/plan-v2-review-round-{1..5}.md`) and the owner's decisions (§6.1). Rev 6 applies round 5's P0s
and the cheap P1/P2s without a further review round (the owner capped reviews at five); Appendix A maps every
finding and every round-5 required change to where it is resolved or why it is deferred. This document is the
**build contract** for the cycles in §5; a cycle may specify its own internals at build time as long as nothing
here is contradicted, and a cycle whose diff exceeds ~1,500 changed lines is split at build time.*

---

## 0. The verdicts, in plain English

| Question Nathan asked | Verdict |
|---|---|
| **Is the business model right** (free capped weekly, paid capped monthly)? | **Yes, with three changes.** (1) Meter **minutes of audio**. (2) The paid allowance is **Deep-scan minutes**; every new Pro mix is a Deep scan. (3) A mix already analysed is served free and uncounted when the stored result is *compatible* with the request (§3.4); a Deep request on a Free result pays only for the AudD sweep. Subscription and non-expiring packs both (D5). §3. |
| **Could we charge for it?** | **Not today; yes after Phases 0–1.** The paid tier crashes after spending whenever AudD returns any parseable result (E-C1); with an exhausted key it silently runs the free engine and caches the error bodies (E-H3, E-C4); no spend cap (E-H1); and as built it throws away the benefit it buys (E-C2/E-C3). |
| **Is it the best implementation?** | **No.** Pipeline core sound and tested; paid sequencing broken in four specific ways (§2.3); web layer not hostable (§2.2). |
| **Is the engine order right?** | **Paid engine first, then Shazam — yes (D2), but not as built.** Shazam must sample the uncertain spans *and* the blanks, on windows that overlap AudD's; AudD must carry a validated anchor and its own vote; spend is admitted per request against a hard reservation; the run must not crash. §2.3. |
| **Does the UI work / show useless data?** | Keep the look. Fix the numbers and words (§2.5). |
| **What should be removed?** | Whole-file paid scan + consent gate, ACRCloud, 1001tracklists connector, rescan button/route, version/role page columns, hidden-matches reveal, M3U, duplicate `/new`, dead POST params — disabled before the beta, deleted after (§5 M2). Quarantined: rescans, calibration ML, benchmark, truth. **Kept and fixed: Panako** (D3). §2.6. |
| **Does it fill a genuine gap?** | **Partly — and not where we assumed** (§2.7, D6). |

**Shape:** spikes/owner gates (S) in parallel; **M1 = 33 build cycles** to "M1 build complete" (free tier +
admin-granted Deep, billing off, admin-created accounts, SoundCloud/Mixcloud links only); **private-beta
activation is a separate, recorded owner sign-off of L1/L2/L3/L5**; **M2 = 6 cycles** after the beta (billing
simulation, Stripe sandbox, uploads/YouTube egress, sharing, signup controls, polish, deletions). Every cycle
≤ ~1 day, one commit, a named fixture and an exact command.

---

## 1. What was reviewed and how

| Review | Scope | Method |
|---|---|---|
| **E — engines** (`docs/reviews/v2-review-engines.md`) | `cli._analyse`, `paid_clip`, `scan_targeting`, `fuse/*`, `providers/*`, jobs, disk, tests | code + `dis` + `du` + timing |
| **U — UI** (`docs/reviews/v2-review-ui.md`; `docs/screenshots/v2-before/`) | every screen | live server, screenshots, `node --check`, data audit |
| **M — market** (`docs/research/05-market-2026-09.md`) | 21 competitors, metering, engine terms, demand | web research, cited |
| **H — hosting** (`docs/reviews/v2-review-hosting.md`) | yt-dlp/Shazam from a server, AudD, Stripe, framework, sizing | web research + code |
| **Codex rounds 1–5** | this plan | independent code verification |

Offline suite: **581 passed in 44 s** (93 deselected `slow`/`live`); the full-pipeline modules never run by
default — which is how E-C1 shipped.

---

## 2. Findings that decide the plan

### 2.1 The paid tier does not work (Critical; re-verified by Codex in every round)

| ID | Finding | Evidence |
|---|---|---|
| **E-C1** | Paid-first crashes after presentation whenever any parseable AudD observation exists: `matches`/`recognised` read at `cli.py:794-815`, bound only in the free branch (`:507-521`); unavailable/all-error AudD falls back to free (`:499-506`, E-H3). | `dis` → `LOAD_FAST_CHECK` |
| **E-C2** | Paid-first sends the free engine only to the complement of non-suppressed AudD episodes (`scan_targeting.py:61-69,127-146`; `cli.py:659-682`); `_engine_corroborated` checks no temporal overlap (`fuse/episodes.py:148-156`). | code |
| **E-C3** | AudD clip observations: `anchor=None`, no own trial source (`providers/audd.py:344-365`) → shared `primary` group (`fuse/alignment.py:137-150`); anchorless winners make no alignment point (`:115-124`). The enterprise parser builds an anchor from `timecode` (`audd.py:242-250`) and accepts negative values. | code |
| **E-C4** | AudD error bodies cached before parsing (`paid_clip.py:182-190`), trusted on read (`:175-181`). | code |
| **E-H1** | No spend cap or record; `usd_e2` journaled `0` at `cli.py:821,833,845`. | code |
| **E-H2** | Gap-fill counts overwritten (`cli.py:649-655,676`). | code |
| **E-H3** | Silent paid→free downgrade (`cli.py:499-506`). | code |
| **E-H4** | Paid pass uncancellable; ETA "1 s" (`paid_clip.py:161-197`, `webapp/jobs.py:211-221`). | code |
| **E-H5** | Paid pass sequential/unretried; timeout → re-billed next run (`paid_clip.py`). | code |
| **E-H6** | Runtime settings dropped under a profile (`cli.py:985-1007`, `webapp/runner.py:48-72`); `config show` prints file config (`cli.py:303-318`). | code |
| **E-H8** | `upload_consent`/`build_index` accepted from POST bodies; `upload_consent=true` also runs the whole-file scan (`present/server.py:1373-1406`). | code |
| **E-H9** | Web "build index" never queried (`webapp/runner.py:130-172`). | code |
| **E-M1** | Corroboration counts provider strings; `COMMERCIAL_PROVIDERS` is a discount set (`fuse/episodes.py:91-108,148-156`). | code |
| **E-M2** | Novelty (~0.7 GB RAM/hour) on every fuse, unused with rescans off (`orchestrate.py:140`). | code |
| **E-M3** | `select_gap_targets` uses any coverage (`scan_targeting.py:60-68`). | code |
| **E-M4** | Crowd rows hash-ordered; contradictions both listed (`fuse/episodes.py:790-845`). | code |
| **E-M8** | ~330–370 MB/hour on disk; 10–25 MB durable. | measured |
| **E-L1** | 2.1 s import tax. | measured |
| **E-S1** | Malformed Shazam bodies under throttle → `ShazamHTTPError` (`shazam.py:200-204`) → failure observations (`recognise.py:307-340,521-553`), poorly surfaced. | code |
| **E-S2** | Completion sidecars verify every upstream file (`io.py:148-176`); the gen-N fuse sidecar lists all window files + PCM (`fuse/episodes.py:1136-1151`). | code |
| **E-S3** | Both frozen profiles: `budget.max_usd_e2 = 0`; `profile_app_config` drops it. | `profiles/*.json` |
| **E-S4** | `paid_clip.py` imports `PaidScanResult` from `scan.py`; `httpx` clients inherit proxy env unless `trust_env=False`. | code |
| **E-S5** | Production signatures the fakes must match: `AudDAdapter.recognize_clip(path: Path, on_attempt) -> dict` (`providers/audd.py:412`); Shazam via `HTTPClientInterface` / `InjectedHTTPClient` (`shazam.py:146`). Scorer output used by L3: `overall.identification_work.{precision_e4,recall_e4}`, `empirical_tier_precision_e4[tier]`, `selective_precision_e4` (`benchmark/scorer.py:1003-1043`). | code |
| **E-S6** (new) | The current `likely` rule is `T_ind ≥ 4`, span ≥ 40 s, no competition, global alignment (`fuse/episodes.py:496-504`) — the plan must not replace it. | code |

### 2.2 The web layer cannot be hosted as-is (verified)

One in-memory worker thread (`webapp/jobs.py`); restart loses jobs. No users/sessions/CSRF — any website the
owner visits can `POST /analyse` to `127.0.0.1:8765` (U-F5); `validate_target` accepts `file://` and any path
(`webapp/jobs.py:59-84`). Loopback-only by design; HTTP/1.0; no `/healthz`; 178 KB pages (U-F29).
`ProcessLock` raises rather than coalesces (E-H7). Pages emit `<audio src="../ingest/original.*">`
(`present/page.py:1168-1172`; U-F6). Stale-page GET rewrites `index.html` but not exports (U-F14/F3).
`fuse/episodes.json` and `present/tracklist.*` are mutable singletons. `_load_cached` hashes the retained
original (`ingest.py:143-164`). The wheel packages only `src/id_detector` (`pyproject.toml:35-36`).

### 2.3 Engine sequencing — Deep scan v2

**Owner decision D2:** AudD sweeps first, Shazam second.

#### 2.3.1 Recipes (frozen data in `src/id_detector/recipes.py`; `recipe_id = sha256(canonical JSON of every field)`)

| Field | `free` | `deep` |
|---|---|---|
| `primary_engine` / `primary_density` | shazam / 1 | audd / 1 (config `deep.primary_density=2` = even-indexed frozen windows) |
| `secondary_engine` | — | shazam |
| `secondary_clips_per_minute` | — | 2 → `C = ⌈duration_min × 2⌉` |
| `secondary_reserve_fraction` | — | 0.25 → `R = ⌊0.25 × C⌋` |
| `secondary_priority` | — | `hint_only → listed_not_confident → suppressed_challengeable → blank` |
| `eligibility_min_intersection_ms` | — | 4000 |
| `suppressed_min_votes` | — | 2 (**defines `suppressed_challengeable`: a suppressed candidate with ≥ 2 selected votes**) |
| `reserve_search_ms` / `reserve_min_separation_ms` | — | 45000 / 30000 |
| `overlap_min_ms` / `separation_min_ms` | — | 6000 / 60000 |
| `primary_achieved_fraction` / `secondary_achieved_fraction` | 0.80 / — | 0.95 / 0.80 |
| `anchor_max_ms` / `anchor_slack_ms` | — | 86 400 000 / 12 000 |
| `audd_concurrency` | — | 4 |
| `retry_policy` | shazam: existing limiter | audd: `connect_error`, `timeout_pre`, `http_429`, `http_503` only; ≤ 3; backoff 1/2/4 s |
| `max_usd_e2` (hard per-run cap) | **0 = paid calls forbidden** | **900** |
| `adapter_versions` | shazam:1 | shazam:1, audd_clip:2 |
| `algorithm_version` | `fusion:1` | **`targeting:0,fusion:1` in Phase 0 (provisional scheduler, §2.3.4 step 4) → `targeting:1,fusion:1` from 1b-i**; results with a different `algorithm_version` are never served (§3.4) |
| `requires` | `shazam_sweep` | `audd_sweep`, `shazam_secondary` |

`presentation_version` is not in the recipe. Frozen profiles are untouched; their `budget.max_usd_e2` is never
read by the clip path (E-S3). `max_paid_clips` is retired.

#### 2.3.2 Money: reservation, admission, settlement

- Price: `audd_usd_e6_per_request` from `pricing.toml` (§3.3), recorded on the run.
- **Outcome costs (units of one request):** `match`, `no_match`, `timeout_post`, `http_5xx` (≠ 503),
  `malformed` → 1; `connect_error`, `timeout_pre`, `http_429`, `http_503`, **`auth_error`, `quota_error`** → 0
  (assumption: AudD does not bill throttled/refused requests; L1 confirms; `bill_on_throttle=false`).
- **Reservation** (before any network I/O): `planned = ⌈windows / density⌉`;
  `usd_e6_reserved = planned × price_e6 × 1.05`; `usd_e2_reserved = ⌈usd_e6_reserved / 10⁴⌉`; refused with
  `budget_exhausted` if `usd_e2_reserved > min(recipe.max_usd_e2, AppConfig.max_usd_e2 or ∞,
  account_month_remaining, global_day_remaining)` (hosted-only last two).
- **Admission (the hard cap):** immediately before each AudD dispatch, atomically decrement the run's
  `usd_e6_remaining` by one unit; if it would go negative, do not dispatch → primary stops → `partial`
  (`reason=reservation_exhausted`). Zero-cost outcomes refund their unit on resolution. Retries re-admit.
- **Settlement** at terminal status: `usd_e6_spent = Σ unit costs`; `usd_e2_spent = ⌈usd_e6_spent / 10⁴⌉`;
  remainder released.

#### 2.3.3 Attempt state machine (every provider request)

Durable, append-only (`recognise/attempts.jsonl` locally; `provider_attempt_events` hosted, each row with its
own `event_id` and `seq` per attempt): `prepared(attempt_id, query_id = clip cache key, ordinal,
parent_attempt_id, unit_usd_e6)` → **`dispatched` (written before entering network I/O)** → `resolved(outcome
∈ {match, no_match, http_429, http_503, http_5xx, malformed, connect_error, timeout_pre, timeout_post,
auth_error, quota_error})`. `auth_error` = HTTP 401/403; `quota_error` = HTTP 402 or an AudD error body
naming quota/credits. Derived classes: **ambiguous** = {`timeout_post`, `http_5xx`, `malformed`} — spent,
never auto-retried; **terminal-provider** = {`auth_error`, `quota_error`} — cost 0, never retried, and the
primary stops immediately. On resume: `prepared` without `dispatched` → re-issue; `dispatched` without
`resolved` → ambiguous. Response cache receives only `match|no_match`; `--refresh-states` (default
`no_match`) selects which **cached** states are re-queried — attempts with other outcomes were never cached and
are simply re-run.

#### 2.3.4 Steps (deep)

1. **Intake and resolve** — worker resolves the alias, fetches (or takes the upload), computes `media_key`,
   duration, `hints_snapshot_id`; `analysis_key = sha256(media_key, recipe_id, source_kind ∈ {platform,
   upload, local}, tenant_scope ∈ {public, user:<id>}, hints_snapshot_id, manual_tracklist_sha256 | "",
   panako_index_id | "")` — `tenant_scope = user:<id>` whenever `source_kind = upload`, a manual tracklist is
   present, or a private index is used; then one transaction: serve a compatible stored result (§3.4), or
   attach to a non-terminal run (with a provisional credit reservation, §3.5), else reserve credits + USD and
   start.
2. **Primary sweep (AudD)** — admission per dispatch; `simultaneous_source="audd"`; anchor from `timecode` iff
   numeric, `0 ≤ timecode_ms ≤ anchor_max_ms`, and `≤ duration + anchor_slack_ms` when the match carries a
   duration; else `anchor=None`. `audd_sweep` achieved iff resolved `match|no_match` ≥ 95 % of `planned`.
3. **Hints → first fuse.**
4. **Secondary (Shazam).** *Provisional scheduler (`targeting:0`, Phase 0):* candidates in priority order,
   eligible windows = frozen windows intersecting the span ≥ `eligibility_min_intersection_ms`, all eligible
   windows queued in priority order then start order up to `C`, no reserve. *Full scheduler (`targeting:1`,
   1b-i):* allocation with `A = C − R`: (i) one window per candidate in priority order, ties by longer span then
   earlier start, until `A` is exhausted; (ii) remaining by largest-remainder proportional to span duration,
   ties by earlier start; (iii) within a span rank by intersection desc, RMS energy desc, start asc; a duplicate
   pick is replaced by the next-ranked window in the same span; a span with no eligible windows returns its quota
   to (ii). **Shazam windows may and should coincide with AudD windows; the only exclusion is a Shazam window
   already picked in this run.** Reserve `R`: a new identity found in a blank at `[m₀, m₁]` queues the two
   eligible windows with starts in `[m₀ − 45 s, m₁ + 45 s]` (excluding the probe) that are farthest apart and
   ≥ 30 s apart (one if only one exists); **confirmations are served first-come until `R` is exhausted, then
   further discoveries are listed uncorroborated**; unused `R` is distributed by (ii). `shazam_secondary`
   achieved iff resolved ≥ 80 % of allocated.
5. **Re-fuse once.** Corroboration = **selected votes** for the same normalised work with supports overlapping
   ≥ `overlap_min_ms`, from different families — `catalogue` {audd, acrcloud}, `shazam`, `local_index`
   {panako}; crowd hints stay hint corroboration. One cross-family agreement → "confirmed twice" shown;
   bypassing suppression/the 30 s floor needs two agreements ≥ `separation_min_ms` apart **or the existing
   `likely` rule exactly as implemented today (E-S6) — the plan does not change it**.

#### 2.3.5 Status matrix (0a-iv tests assert exactly this)

| Situation | `free` | `deep` |
|---|---|---|
| A terminal-provider outcome (`auth_error`, `quota_error`) or `connect_error` after retries **before any resolved `match|no_match`** | — | **`provider_unavailable`**, exit 3, nothing spent, no result stored. Local CLI `--allow-degrade` **restarts the request as the `free` recipe before any paid work** (`requested=deep`, `achieved=free`, status `degraded`, exit 0) |
| A terminal-provider outcome **after** at least one resolved attempt | — | primary stops → **`partial`** (`reason=provider_unavailable_midrun`) |
| Primary achieved fraction not met (`free`: Shazam < 80 %; `deep`: AudD < 95 %, incl. `reservation_exhausted`) | **`partial`**, exit 0 | **`partial`**, exit 0 |
| Primary met, `shazam_secondary` < 80 % | — | **`degraded`**, exit 0 |
| All requirements met | `complete` | `complete` |
| Reservation > effective cap | — | `budget_exhausted`, exit 4, nothing spent |
| Re-fetched bytes ≠ `media_key` | `source_changed`, 5 | same |
| Exception / cancel | `failed` 1 / `cancelled` 130 | same |

Results with status `complete|degraded|partial` are **shown** to the requester (banner for the latter two);
only `complete` (and `degraded` with `accept_degraded`, local only) is **served** to later requests (§3.4).

**Breaker (Shazam):** qualifying failures = `http_429`, `http_503`, `http_5xx`, `malformed`, `timeout_post`;
denominator = all resolved Shazam attempts on that egress in the rolling 5-minute window; **minimum sample
20**; (a) failure rate > 30 % → open 30 min; (b) `shazam_daily_budget_per_egress` exhausted → open until 00:00
UTC; (c) latch: three (a)-opens in one UTC day → off until an admin re-enables. Open → new free jobs
`waiting`; running deep secondary skipped → `degraded`; running free primary continues. Per-process in 1b-iii;
service-wide over `provider_attempt_events` from 4b-ii. Manual `IDEA_ENGINE_SHAZAM=off`. Never a mid-run
engine swap; never proxied; "sparse AudD" free is post-L1 operator-only configuration.

#### 2.3.6 Cost per 60-minute mix (400 windows; $5/1,000 walk-up · $2/1,000 subscription)

| Recipe | AudD req | Shazam req | AudD $ | Wall-clock (after 0b) |
|---|---|---|---|---|
| `free` | 0 | 400 | $0 | 9–60 min |
| `deep` d=1 | 400 | ≤ 120 | $2.00 · $0.80 | ~4 + 3–10 min |
| `deep` d=2 | 200 | ≤ 120 | $1.00 · $0.40 | ~2 + 3–10 min |

ACRCloud removed (same family as AudD). Panako kept (D3) as `local_index`; `panako_index_id` in `analysis_key`.

### 2.4 Accuracy: where the real edge is

Shazam alone caps at ~27/30 on the owner's benchmark; misses are in no commercial catalogue. Levers: crowd IDs;
a DJ's own indexed uploads. Cross-family agreement is the strongest precision signal. **No accuracy number is
advertised before L3.**

### 2.5 UI: remove / change / add (review U; unchanged from rev 4)

**Remove:** hidden-matches reveal (U-F7); `Version`/`Role` page+Markdown columns (U-F8; JSON keeps them; shown
only when verified/contested); rescan button/route (U-F11); `HINT` pill; `layer`/`outgoing`; `ID gap` tile;
"9h 03m listened"; M3U; duplicate `/new`; false footer (U-F12); internal tokens (U-F31); `upload_consent`.
**Change:** bad URL → inline error (U-F1); one typed entry list for page/Copy/CUE/Markdown/JSON/card
(U-F2/F3/F14); mobile keeps "Where to get it" (U-F4); `purchase_url` before search (U-F10); wall-clock progress
(U-F9); failure copy + credit statement (U-F15); one confidence word + glossary; legend 6 → 2 (U-F16/17);
contrast + table semantics (U-F19/20); "Free scan"/"Deep scan" chip; three tiles (U-F21); alternatives only for
a different work (U-F25); lead-in behind details (U-F26); options summary (U-F28); in-progress cards (U-F18);
no auto-redirect (U-F30); crowd rows honest (U-F13); one privacy line; run status banner; local audio via
`/media/<media_key>/audio`.
**Add:** analysed date; per-user library with delete; "you can close this tab"; failed runs visible (U-F33);
share links (M2).
**Keep:** theme, logo, scanner strip, cards, `prefers-reduced-motion`, NOW pill, CUE/Markdown/JSON, paste box.

### 2.6 Remove / quarantine / keep

| Subsystem | Decision |
|---|---|
| Whole-file `scan.py` path, `run_paid_scanners`, consent gate, `upload_consent` | 0a-iii: unreachable + refused; `PaidScanResult` → `paid_clip.py` (0a-i); M2 7f: delete (tag `pre-v2-removals`) |
| ACRCloud adapter; `--engine acrcloud` | 0a-iii: refused; M2 7f: delete |
| `tl1001` connector | 0a-iii: default-disabled; M2 7f: delete |
| **Panako** + web `build_index` | **keep; 0a-iii wires `local_index_label`**; not in the hosted image |
| Rescans + transform grid | quarantine |
| `calibrate/`, `benchmark/`, `truth.py`, `local_fixture.py`, `pointer_import` | quarantine; lazy import + `dev` extra in M2 7f |
| `novelty.py` | guarded + hoisted (0b-ii) |
| `sc_comments`, `manual`, `mixesdb`, `yt_comments`, `mixcloud` | keep (L2); hard timeouts hosted |
| CUE / Markdown / JSON | keep; M3U removed |

### 2.7 Market (review M; unchanged from rev 3)

21 hosted competitors at $0.99–2.99/mix or €6–$11/month, traffic falling; comments-reading and blank gaps
already shipped by set79/TrackRadar; nobody ships work-vs-recording or an evidence trail; demand large
(MixesDB 21.9 % of 366 K mixes untracklisted; #DJSET +39 % YoY), willingness to pay weak (Set2Tracks: "people
love to find this for free but wouldn't pay"); Beatport Track ID bundles live ID. Lessons: minutes, never cripple
free results, non-expiring packs, refund on failure, cached catalogue as SEO. AudD: $5/1,000 walk-up,
"$2/1,000" subscription (per-clip applicability unconfirmed — L1); trial licence evaluation-only + attribution;
paid terms unreadable by fetchers. No legal server-side Shazam; no enforcement found; competitors run it openly.
Uniqueness → the pro/report buyer (D6); consumer angle → scene-specific.

---

## 3. Product definition v2

### 3.1 Two modes, one codebase

**Local mode** (`idea.cmd`): passes `scripts/gate_local_mode.ps1` at every boundary. **Hosted mode**
(`IDEA_MODE=hosted`): **startup requires `/data/hosted-ready.json` whose `build_id` equals the running image's
`/app/BUILD_ID`** (baked at image build from `--build-arg BUILD_ID=<git sha>`), regardless of bind address;
the file is written only by the gate service (6a-iv). The bypass `IDEA_HOSTED_READY=gate` is honoured only
when `IDEA_SERVICE=gate` (set solely by `compose.gate.yml`, which has no Caddy and binds loopback).

### 3.2 Scans

| | Free scan (`free`) | Deep scan (`deep`) |
|---|---|---|
| Engines (D1) | Shazam sweep + crowd hints | AudD sweep → hints → Shazam sampled (overlapping) → re-fuse |
| User sees | badges, "from comments", run status | + "confirmed twice", tighter boundaries, rescued short tracks when doubly confirmed |
| API cost / hour | $0 (Shazam capacity) | $0.80–2.00 (d=1) |
| Shazam throttled | `partial` with failed-window count; breaker pauses new free scans | `degraded` (AudD-only) |

### 3.3 Tiers, caps, prices

Unit = **minutes of new audio**, rounded up. Free lots reset weekly (Monday 00:00 UTC). Pro lots (M2) are
created monthly by the scheduler (7a-i); in M1 all Deep minutes are admin lots. Packs never expire; allocation
order = soonest-expiring lot first.

**Single pricing authority: `pricing.toml`** (repo root; created in 0a-ii; `pricing_version`). Fields:
`audd_usd_e6_per_request = 5000`, `bill_on_throttle = false`, `free.minutes_per_week = 150`,
`free.max_mix_minutes = 150`, `free.sources = ["soundcloud","mixcloud"]`, `pro.price_gbp_month = 9`,
`pro.price_gbp_year = 90`, **`pro.deep_minutes_per_month = 90`** (provisional: walk-up, d=1, per the table),
`pro.max_mix_minutes = 240`, `pro.sources = ["soundcloud","mixcloud","youtube","upload"]`,
**`pack.small = {gbp = 6, minutes = 55}`**, `usd_cap_account_month_e2 = 2000`, `usd_cap_global_day_e2 = 5000`,
`cache_hits_per_day = 20`, `hints_max_age_days = 7`, `alias_revalidate_days = 7`,
**`serve_free_from_deep = false`**, `compat_version = 1`. `AppConfig` loads `audd_usd_e6_per_request` and
`max_usd_e2` (optional) from it; the recipe's `max_usd_e2` is never raised by pricing. Every reservation records
`pricing_version`.

| | **Free** | **Pro** | **Packs** |
|---|---|---|---|
| Price (GBP incl. VAT) | £0, no card | £9 / month; £90 / year | from £6 |
| Allowance | 150 min / week, `free` recipe | Deep minutes / month per `pricing.toml` | Deep minutes, never expire |
| Sources | SoundCloud, Mixcloud links (D7) | + YouTube links, uploads (M2) | as Pro |
| Mix length cap | 150 min | 240 min | 240 min |
| History | 30 days | unlimited | 90 days |
| Compatible cached result | free, uncounted, `cache_hits_per_day` | same; Deep-on-Free pays the AudD sweep | same |

**COGS formula (reproducible):** `deep_cost_per_hour = audd_req_per_hour × price × 1.05 + proxy_share +
infra`, with `proxy_share = 0.5 × $0.30 = $0.15` (Pro only) and `infra = $0.10`; `allowance_minutes =
⌊60 × (net_revenue × 0.5 − failure_reserve) / deep_cost_per_hour⌋` with `failure_reserve = 0.03 × gross`.
£9 → $11.50 gross, ≈ $8.90 net after VAT 20 % and Stripe (1.5 % + 20p + 0.7 %); £6 → ≈ $5.75 net.

| AudD rate | `deep_cost_per_hour` | Pro £9 → minutes | Pack £6 → minutes |
|---|---|---|---|
| walk-up, d=1 | $2.35 | **90** | **55** |
| walk-up, d=2 | $1.30 | 165 | 100 |
| subscription, d=1 | $1.09 | 195 | 120 |
| subscription, d=2 | $0.67 | 320 | 200 |

**No price is published and no third-party Deep scan runs before L1** (beta included).

### 3.4 Results, compatibility, coalescing

- **Result bundle** = `present/bundles/<bundle_id>/` (`bundle_id = sha256(run_id, presentation_version)`): page,
  exports, `manifest.json` (files + hashes + sizes, `run_id`, `analysis_key`, `requested_recipe_id`,
  `achieved`, `status`, `presentation_version`, `pricing_version`, `usd_e2_spent`); fuse artefacts in
  `fuse/runs/<run_id>/` with their own `manifest.json`. **Publication invariant: artefact files and manifests
  are written and fsynced before any DB row, checkpoint or pointer references them.** Bundles are immutable;
  refresh writes a new bundle for the same run. Local mode keeps `present/current` → the newest `complete`
  run's latest bundle; hosted mode has no global pointer.
- **`serves(stored, request)`** (`compat_version = 1`):

| Request | Served by a stored result iff |
|---|---|
| `free` | stored `free complete` with equal `analysis_key`; **or** stored `deep complete` (any density) with the same non-recipe key inputs **only when `serve_free_from_deep = true`** (set after L3) |
| `deep` d=2 | stored `deep complete` d=2 **or d=1**, same non-recipe key inputs |
| `deep` d=1 | stored `deep complete` d=1 only |
| any | **equal** `algorithm_version` and **equal** `adapter_versions` (a bump invalidates serving; cache raw responses remain reusable); `degraded` only with `accept_degraded` (a `RunRequest` field, local only); `partial` never; `tenant_scope` must match |

  **Deep-on-Free:** a `deep` request finding only a `free complete` result (same inputs, same scope) reuses its
  Shazam observations as secondary evidence, runs the AudD sweep, re-fuses; reservation = primary only.
- **Coalescing key = `analysis_key`**; a request with a non-terminal run attaches as a subscriber with a
  provisional reservation (§3.5). Provider raw responses are shared across users; derived private results are
  scoped.
- **Submission order:** alias → media → compatible-result lookup **first**; a compatible result is served (a
  cache hit, uncounted) even at zero balance; only a request that would start a new analysis is refused for
  zero balance.
- **Presentation inputs** (title, uploader, embed) come from the requester's alias (`library_items.alias_id`).
- **Sidecars after pruning** (E-S2): `pruned_upstream` entries; verifier accepts them when the artefact hash
  matches; consumers re-derive (windows ← PCM ← original ← re-fetch); re-fetched bytes must hash to
  `media_key` or the run ends `source_changed`.
- **Source aliases:** `(url, canonical_url, media_key, valid_from, valid_to, last_verified)`; revalidated after
  `alias_revalidate_days` via `yt-dlp --skip-download` metadata; mismatch closes the row.

### 3.5 Credits: lots, allocations, settlement

- `credit_grants(id, account, kind ∈ {plan, pack, admin}, minutes_total, minutes_remaining, expires_at,
  source_ref)`; `credit_allocations(reservation_id, grant_id, minutes)`; `credit_events` (append-only). Admin
  comps are `kind = admin` lots.
- **Reservation** in the worker after intake, one `BEGIN IMMEDIATE` transaction with the balance check,
  allocations from soonest-expiring lots, the USD reservation (initiator only) and the run row. **Attaching
  subscribers make a provisional credit reservation of the same size** (no USD reservation); a subscriber
  who cannot reserve is refused `quota_exceeded` and not attached.
- **Payer transfer:** if the initiator detaches while subscribers remain, in one transaction the initiator's
  reservations are released and the earliest-attached remaining subscriber's provisional credit reservation
  becomes the settling one; the USD reservation is re-attributed to that account — if its `account_month` cap
  cannot cover it, the run **continues** (never aborted in flight), the overage is charged against the global
  pool and an `admin_audit` row records it.
- **Settlement / release** at terminal status, one transaction: (1) settled minutes per the rules below; (2)
  consume allocations lot by lot in allocation order; (3) release the remainder to the same lots unless the lot
  has expired — plan-lot remainder is then forfeited, pack/admin remainder returned; (4) events. Rules:
  `complete`, `degraded` → 100 %; `partial` → 100 % if ≥ 90 % of primary resolved, else 50 %;
  `provider_unavailable`, `budget_exhausted`, `source_changed`, `failed`, `cancelled` → 0 %; non-paying
  subscribers' provisional reservations are released in full at the run's terminal status; when the last
  subscriber detaches, the cancel token fires, in-flight dispatched attempts resolve and settle as spent, and
  the run ends `cancelled`. Released-but-spent runs still count toward `usd_cap_account_month_e2`.
- **USD:** `usd_reservations(id, run_id, account, usd_e6_reserved, usd_e6_remaining, pricing_version)` and
  `usd_events` (reserve / admit / refund_unit / settle / release / reattribute); period remaining = cap −
  Σ(reserved-not-yet-settled + settled).
- `cache_hits(user, day, count)` enforces `cache_hits_per_day`.
- Packs bought via Stripe (M2 7a-ii) are lots with `source_ref = order_id`; `refunds(id, order_id,
  stripe_refund_id, amount_e2, minutes_reversed, grant_id, dispute_id, at)`; a refund/dispute reverses
  `min(remaining, refunded_minutes)`; **a dispute later resolved in our favour re-grants the reversed minutes as
  a new lot** with the original expiry class.

### 3.6 Sign-in, payments, beta (decided)

Email + password (argon2-cffi); server-side sessions; CSRF + Origin. `BILLING_BACKEND = off | local | stripe`
(`local`, `stripe` in M2). **M1 accounts are admin-created and pre-verified** (no self-signup; invites in M2
7c); password reset via admin-issued single-use links. **"M1 build complete" (6b) ≠ beta live:** the private
beta is activated only after recorded owner sign-off of L1, L2, L3, L5 in `docs/LAUNCH.md`; L4 belongs to the
payment launch (M2). Beta configuration: `BILLING_BACKEND=off`, admin Deep lots, SoundCloud/Mixcloud links
only, `IDEA_SHARING=off`, `IDEA_PUBLIC_CATALOGUE=off`, Panako not in the image.

---

## 4. Hosted architecture

### 4.1 Showstopper checks

| Check | Finding | Consequence |
|---|---|---|
| Unofficial Shazam from a server | ~20 req/min per IP shared by all users → ~40–65 mixes/day per IP; failures past the throttle (E-S1); ASN blocks; no official API | **D1 accepted.** Per-account weekly cap; `provider_attempt_events` per egress; §2.3.5 breaker; kill-switch; extra egress IPs as config rows; `trust_env=False` on Shazam and AudD clients; never proxied |
| yt-dlp from a server | SoundCloud/Mixcloud direct; YouTube needs residential egress | **D4/D7:** YouTube paid-only (M2 7c); proxy passed explicitly to yt-dlp for YouTube jobs only; S2 runs before 7c |
| AudD terms | **Read 2026-09-10:** paying customers may use, display and cache Results in their own products; attribution optional; no standalone Results resale; no competing recognition service; users must be bound by equally protective terms; AudD may terminate without notice; terms-version endpoint to poll | licensing cleared; commercial questions (rate, limits, 429 billing) by email; ToS flow-down under L2; daily `GET https://api.audd.io/terms/version` check in the 6b runbook |
| Disk | 330–370 MB/hour; 10–25 MB durable | retention for every terminal state (§4.6) |
| RAM | novelty ~0.7 GB/hour | guarded off (0b-ii); 1.5 GB/job; length caps |

### 4.2 Shape

```
Internet ─:443─▶ Caddy (TLS, gzip, 250 MB cap, headers) ─▶ uvicorn · idea_web (FastAPI) [starts only with a valid /data/hosted-ready.json]
                                                            │ pages · auth/sessions · CSRF/Origin · compatible-result lookup · submit pre-check
                                                            │ POST /analyse → jobs row (state=intake) · status polling · /healthz
                                                            │ result routes (authz via library_items, traversal-safe) over present/bundles/<bundle_id>/
                                                            ▼
                                                       app.db (SQLite WAL; Litestream) · online-backup snapshots under the artefact lock
                                                            ▲ lease / heartbeat / checkpoints / progress / subscribers / reservations
                                                       idea_web.jobs.worker (separate process; N=1)
                                                            │ intake: alias → fetch → media_key/duration/hints → analysis_key → serve|attach|reserve+run (one txn)
                                                            │ id_detector.service.run(RunRequest) ← pipeline public API
                                                            │ retention GC → versioned trash (purged after 7 d); shares the artefact lock with backup
                                                            ▼
                                                       work/<source>/<media>/ (content-addressed; durable subset snapshotted)
```

Rules: web never runs a pipeline; worker never serves HTTP; workers never receive user paths (`PlatformUrl |
UploadId | LocalPath(local only)`); `src/id_detector/` changes after Phase 3 limited to `service.py`,
`recipes.py`, `serve` entry point, `PROJECT_ROOT`.

### 4.3 Web layer and service API

FastAPI + Jinja2 + uvicorn (S4 ADR: Codex recommends, owner approves before 4a-ii). `src/idea_web` packaged.
`idea serve` = same app in local mode; `present/server.py` retired after parity. Render policy
`emit_local_audio` false when hosted. Polling 2.5 s. `/healthz` exists from 0a-i on the current server and is
ported.

`id_detector.service.run(RunRequest(run_id, analysis_key, target: PlatformUrl | UploadId | LocalPath,
recipe, accept_degraded, hints_snapshot_policy, manual_tracklist: bytes | None, checkpoint_store,
attempt_journal, usd_admitter, progress, cancel_token)) → RunResult(run_id, status, reason, achieved,
bundle_id, usd_e6_reserved, usd_e6_spent, attempts)`. `run_id` caller-supplied; `checkpoint_store` persists
phase completion (`ingest, decode, windows, primary, hints, fuse1, secondary, fuse2, present`); checkpoints are
written **after** the phase's artefacts are durable (§3.4 invariant).

### 4.4 Accounts, sessions, abuse

argon2-cffi; server-side sessions (`__Host-idea_session; HttpOnly; Secure; SameSite=Lax`; `sha256(token)`;
rotation on login/plan change; 30 d idle / 90 d absolute); single-use hashed tokens (admin-issued in M1);
CSRF synchroniser + Origin; per-IP limits (login 10/15 min, reset 3/h, analyse 5/h; IPv6 /64; trusted proxy
config); CSP/security headers; export routes 10/min.

### 4.5 Data model (SQLite WAL; numbered SQL migrations with `down` scripts; **M1 tables only** — invites,
sharing, orders, invoices, refunds, `processed_stripe_events` and Stripe entitlement fields arrive with their M2 cycles)

`users` (id, email, pw_hash, verified_at, created_ip, disabled_at, deleted_at) · `sessions` · `email_tokens`
(hash, purpose, expires, used_at, issued_by) · `media` (media_key, duration_ms, first_seen) · `source_aliases`
(§3.4) · `analysis_runs` (run_id, analysis_key, media_key, requested_recipe_id, algorithm_version,
adapter_versions, achieved JSON, status, reason, tenant_scope, payer_user, payer_reservation_id, attempts,
checkpoints JSON, usd_e6_reserved, usd_e6_spent, pricing_version, started_at, finished_at) · `result_bundles`
(bundle_id, run_id, presentation_version, path, manifest_sha256, created_at) · `run_subscribers` (run_id, user,
reservation_id, alias_id, attached_at, detached_at) · `library_items` (id, user, run_id, alias_id,
retention_class ∈ {free30, pack90, pro∞}, added_at, deleted_at) · `jobs` (id, run_id, target JSON, recipe_id,
state ∈ {intake, waiting, analysis, …terminal}, lease_owner, lease_until, heartbeat_at, attempt, max_attempts
= 3, dead_letter_reason, cancel_requested, progress JSON, log_path) · `credit_grants`, `credit_allocations`,
`credit_events`, `usd_reservations`, `usd_events`, `cache_hits` (§3.5) · **`provider_attempt_events`**
(`event_id` INTEGER PRIMARY KEY, attempt_id, seq, run_id, provider, egress_id, query_id, parent_attempt_id,
state, outcome, http_status, unit_usd_e6, at) — append-only · `entitlements` (user, plan, status, source =
admin, period_end) · `admin_audit` (actor, action, target, at).

### 4.6 Jobs, retention, backups

Worker: lease + heartbeat 10 s; states `intake → waiting|analysis → terminal`; checkpoints after durable
artefacts; `attempt ≤ 3` then dead-letter (treated as `failed` after 24 h); cancel/drain per §3.5.
**Retention** (`idea gc --policy local|hosted`): `complete|degraded|partial` → delete `windows/**` now, PCM
after 48 h, original after 7 d (hosted; local keeps it), sidecars rewritten; `failed|cancelled|
provider_unavailable|budget_exhausted|source_changed|quota_exceeded|dead_letter` → delete `windows/**` + PCM
now, original after 24 h, media dir after 7 d if no bundle references it; upload staging (M2) expires after
24 h; jobs with a stale lease and no status → `failed` after `max_attempts`; history expiry removes
`library_items`; a bundle/run is deleted only when no library item, share or publication references it.
**GC never deletes in place: it moves directories to `work/.trash/<YYYY-MM-DD>/` and purges after 7 days.**
**Backups** (`idea backup`, nightly): (1) **take the artefact lock** (shared with GC and bundle commits; GC
skips a cycle rather than wait); (2) `sqlite3.Connection.backup()` → `snapshot/app.db`; (3) enumerate every
`bundle_id`/`run_id` referenced by the copied DB; (4) hard-link (same filesystem) those directories plus
`recognise/`, `hints/`, `ingest/source.json` into `snapshot/artefacts/` — hard-linking is O(entries), so the
lock is held for seconds, not the copy duration; (5) release the lock; (6) upload the snapshot; `snapshot.json`
records DB hash + listed artefacts + manifest hashes. Restore = DB → artefacts → `idea verify-artefacts`
(bundle manifests, fuse-run manifests, and the existing completion sidecars for `recognise/`/`hints/`). Server:
Hetzner CX/CPX (~€16/month); image `python:3.12-slim` + uv + ffmpeg + Deno + `yt-dlp[default,curl-cffi]`, no
JDK, no dev tooling; ffmpeg/ffprobe under `timeout` + memory limits.

### 4.7 Billing (M2)

`BillingProvider` — `create_checkout_url`, `portal_url`, `handle_webhook`, `current_entitlement`. `off` (M1):
admin lots. `local` (7a-i): `/dev/billing/*` simulate pages; monthly-lot scheduler `idea billing grant-monthly`
(idempotent by `(subscription_id, YYYY-MM)`, anniversary day clamped). `stripe` (7a-ii): Checkout subscription
(monthly/annual) and payment (packs); Portal; webhooks `checkout.session.completed`,
`checkout.session.async_payment_{succeeded,failed}`, `customer.subscription.{created,updated,deleted}`,
`invoice.paid`, `invoice.payment_failed`, `charge.refunded`, `charge.dispute.{created,closed}`; raw-body
signature; `event_id` dedupe; re-fetch on event; grant on `active|trialing`, revoke on `canceled|unpaid`,
7-day grace on `past_due`; Stripe Tax; GBP VAT-inclusive; tables `orders`, `invoices`, `order_credit_links`,
`refunds`, `processed_stripe_events`; `docs/billing-testing.md` with the exact CLI/test-clock sequence and
expected rows; live needs `sk_live_` and `IDEA_BILLING_LIVE=1`.

### 4.8 Hosted boundary

Allow-listed platform hosts (M1: SoundCloud, Mixcloud) or upload ids (M2); SSRF guard (private/reserved ranges,
DNS rebinding, redirect re-validation); result/export routes authorise via `library_items` with traversal/
symlink checks; originals never served; no rescan route; results private; one privacy sentence everywhere.

---

## 5. Phases — M1 (33 cycles) then M2 (6 cycles)

Every cycle: Codex builds → `uv run pytest -q` · `uv run ruff check .` · `uv run ruff format --check .` ·
`uv run python scripts/audit_fixtures.py` green → Codex reviews the diff → fix → one commit. Test selectors
are exact. Owner-only checks are `live`-marked or `.ps1`. Gates depend only on deliverables of the same or
earlier cycles.

**Shared test infrastructure (0a-i):**
- `scripts/make_audio_fixtures.py` → committed `tests/fixtures/audio/tone-60s.wav` (60 s, 16 kHz mono s16,
  three tones with 2 s crossfades → 7 frozen windows); `tone-3600s.wav` generated on demand.
- **Fakes matching production signatures (E-S5):** `tests/fakes/providers.py` — `FakeAudD` with
  `async recognize_clip(self, path: Path, on_attempt) -> dict`; `FakeShazamHTTP(HTTPClientInterface)`. Scripts
  `tests/fakes/scripts/*.json`: `{"audd": {"default": "match", "windows": {"3": "no_match", "5":
  "timeout_post"}}, "shazam": {...}}`, outcomes `match | no_match | http_401 | http_403 | quota_error |
  http_429 | http_503 | http_500 | timeout_pre | timeout_post | malformed`; `match` payloads carry
  title/artist per tone and a `timecode`.
- **Injection:** `_analyse(..., paid_scan_adapters=..., shazam_http_client=...)`; `idea analyse
  --fake-providers audd,shazam` (hidden) reads `IDEA_FAKE_SCRIPT`, refused unless `IDEA_TEST_MODE=1`.
- `scripts/assert_journal.py --work-root <dir> --expect k=v…`.
- **`GET /healthz`** (200, `{"ok": true}`) added to the current `present/server.py` in 0a-i and ported in 4a-ii.
- `scripts/smoke_serve.ps1` / `.sh`: start `uv run idea serve --no-open --port 8791` in the background, poll
  `GET /healthz` (≤ 20 s), assert 200 and that `GET /` contains `Drop a mix`, stop the process; non-zero exit
  on any failure.
  **PowerShell host:** gates written as `pwsh scripts/<x>.ps1` are equivalently satisfied by
  `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/<x>.ps1` (Windows PowerShell 5.1, the host
  installed on the owner's machine); scripts must run under both.

### Phase S — spikes and owner gates (parallel; not build cycles)
- **S1 (owner):** AudD production terms + per-clip rate + whether throttled/refused requests bill +
  reconciliation → L1.
- **S2 (owner runs; script from 0a-iii; runs before M2 7c):** `scripts/spike_ingest_vps.sh <sc-url> <mc-url>
  <yt-url> [proxy]` → `docs/spikes/ingest-vps.md`.
- **S3 (owner runs; script from 0a-iii):** `scripts/spike_shazam_vps.py --minutes 60 --ceiling 45` daily × 5
  → `docs/spikes/shazam-vps.md` → `shazam_daily_budget_per_egress`.
- **S4 (Codex recommends, owner approves):** `docs/adr/0001-web-framework.md` before 4a-ii.

### 0a-i — Crash, cache states, `/healthz`, fixtures, fakes, injection, journal assertions
E-C1, E-C4, E-H2, E-H3 (status), E-S4 (relocation).
- Bind `matches`; free failure count only when the free branch ran; `PaidScanResult` → `paid_clip.py`;
  cache states (§2.3.3); `_refuse_with` accumulates; `provider_unavailable` (3) when the paid engine yields
  no resolved attempt (without `--allow-degrade` yet); `/healthz`; fixtures, fakes, injection,
  `assert_journal.py`, `smoke_serve.*`.
- **Gate:** `uv run pytest tests/test_phase0a_crash_cache.py -q` (paid success; no-match; all `http_401` →
  exit 3, nothing cached, zero units billed; error bodies never cached; counts accumulate);
  `pwsh scripts/smoke_serve.ps1`.

### 0a-ii — Recipes, pricing, reservation, admission, settlement
E-H1, E-S3.
- `recipes.py` (§2.3.1, `targeting:0`) + `--recipe free|deep`; `pricing.toml` (§3.3) + loader; reservation/
  admission/settlement (§2.3.2) with an in-process `usd_admitter`; journal fields `usd_e6_reserved`,
  `usd_e6_spent`, `usd_e2_reserved`, `usd_e2_spent`, `requested_recipe_id`, `algorithm_version`.
- **Gate:** `uv run pytest tests/test_phase0a_money.py -q`: reservation arithmetic on 7 windows (36 750 / 4 ¢);
  reservation > cap → exit 4 with zero attempts; `max_usd_e2=0` refuses; admission stops dispatch at the
  reservation (429-storm script) → `partial/reservation_exhausted`; zero-cost outcomes refund their unit.

### 0a-iv — Status matrix, `--allow-degrade`, provisional secondary, Phase-0 Deep gate
- §2.3.5 matrix incl. `auth_error`/`quota_error`; `--allow-degrade` recipe substitution; the provisional
  `targeting:0` secondary scheduler over Shazam (uses the existing injected client); `status`, `reason`,
  `achieved` in the journal and `tracklist.json`.
- **Gate (PowerShell):**
  ```powershell
  $env:IDEA_TEST_MODE = "1"; $env:IDEA_FAKE_SCRIPT = "tests/fakes/scripts/gate0a-deep.json"
  $w = Join-Path $env:TEMP ("idea-gate0a-" + [guid]::NewGuid().ToString("N"))   # fresh root each run: Windows cannot delete the long-path cache files
  uv run idea analyse tests/fixtures/audio/tone-60s.wav --recipe deep --fake-providers audd,shazam --work-root $w
  uv run python scripts/assert_journal.py --work-root $w --expect status=complete algorithm_version=targeting:1,fusion:2 usd_e6_reserved=36750 usd_e6_spent=35000 usd_e2_spent=4
  ```
  plus `uv run pytest tests/test_phase0a_status.py -q` asserting every matrix row (`http_401` first →
  exit 3; `quota_error` after two matches → `partial/provider_unavailable_midrun`; `--allow-degrade` →
  `degraded`, `achieved=free`, zero further AudD attempts; secondary < 80 % → `degraded`).

### 0a-iii — Security and dead paths (local)
E-H8, E-H9, E-M9, E-L4, U-F5 (loopback).
- Drop `upload_consent`; `run_paid_scanners` call site removed; `--engine acrcloud` refused; `tl1001`
  default-disabled; **`local_index_label` wired from the web runner (D3)**; `_windows_in_spans` skips
  transformed windows; loopback CSRF (`GET /csrf`, form field or `X-CSRF-Token`, `Origin`/`Host` ∈
  {`127.0.0.1:<port>`, `localhost:<port>`}); S2/S3 scripts written.
- **Gate:** `uv run pytest tests/test_phase0a_security.py -q`.

### 0b-i — AudD adapter v2: anchor, concurrency, retries
E-C3 (anchor), E-H5.
- Anchor validity rule; `simultaneous_source="audd"`; concurrency 4 + token bucket; retry policy incl.
  `auth_error`/`quota_error` classification; `trust_env=False`.
- **Gate:** `uv run pytest tests/test_phase0b_audd.py -q` (`http_429`×2 then `match` → 3 attempts, 1 unit
  billed; `timeout_pre` retried; negative/absent `timecode` → `anchor=None`; `http_403` → `auth_error`, cost 0).

### 0b-iii — Attempt journal and cancellation
E-H4.
- Durable `prepared/dispatched/resolved` with `dispatched` before network I/O; progress per clip; cancel token.
- **Gate:** `uv run pytest tests/test_phase0b_attempts.py -q` (cancel after 3 clips with 200 ms fake latency →
  ≤ 4 `dispatched`; `timeout_post` → ambiguous, spent, not retried; resume with `dispatched`-unresolved →
  ambiguous; `prepared`-only → re-issued).

### 0b-ii — Effective config, novelty, throttle handling, golden
E-H6, E-M2, E-M10, E-S1.
- Config carry-over (both call sites); `idea config show [--profile X]`; novelty guarded + hoisted; Shazam
  decode errors → limiter penalty + failures; `free` → `partial` when resolved < 80 %; true scanned window set.
- **Golden:** `scripts/make_golden.py` → `tests/golden/local-free/tracklist.json`; semantic comparison
  ignoring `run_id`, timestamps, `generated_by`, `requested_recipe_id`, `algorithm_version`, `analysis_key`,
  `presentation_version`.
- **Gate:** `uv run pytest tests/test_phase0b_config.py tests/test_golden_local_free.py -q`.

### 1a-i — Immutable bundles, manifests, refresh
- `bundle_id`, bundle + fuse-run manifests, publication invariant, `present/current` (local), `refresh.py` →
  new bundle.
- **Gate:** `uv run pytest tests/test_phase1a_bundles.py -q` (refresh creates a second bundle; `current`
  follows the newest complete run; manifests hash every file; a run's DB/pointer reference is written only
  after its files exist).

### 1a-iii — Audio route, cached-open without the original, Windows gate
- Page audio via `/media/<media_key>/audio` (local); `_load_cached` via manifest + `work/index.json`.
- **Gate:** `uv run pytest tests/test_phase1a_cached_open.py -q` (cached-open with the original deleted);
  owner `scripts/gate_local_mode.ps1` (runs `idea.cmd`, opens a cached mix, asserts the probe page logs
  `audio-ok`).

### 1a-ii — Analysis keys, compatibility, Deep-on-Free, source_changed
- `analysis_key`, `serves()` (§3.4), `serve_free_from_deep`, `accept_degraded`, Deep-on-Free reuse,
  `source_changed`; web runner uses `--recipe`.
- **Gate:** `uv run pytest tests/test_phase1a_compat.py -q` (free→deep reserves primary only and FakeShazam
  gets 0 requests; **stored d=1 serves a d=2 request; stored d=2 does not serve a d=1 request**; free served by
  deep only when the flag is on; different `algorithm_version` or `adapter_versions` never served;
  degraded/partial never served hosted; private scope never served across users; altered local-file source →
  `source_changed`, exit 5).

### 1b-i — Secondary targeting v2 (`targeting:1`)
- **Gate:** `uv run pytest tests/test_phase1b_targeting.py -q` on `tests/fixtures/deep/`: `overlap-allowed`
  (both densities: Shazam requests > 0, ≥ 1 window coincides with an AudD window), `hint-only-first`,
  `overflow` (30 candidates, C=12, R=3 → 9 first-round picks), `largest-remainder-ties`,
  `replacement-after-duplicate`, `reserve-confirmation` (two windows ≥ 30 s apart), `reserve-exhausted`
  (third discovery listed uncorroborated), `duration-scaling` (120-min → C=240), `secondary-80pct` → `degraded`;
  `algorithm_version` bumped and old results not served (test).

### 1b-ii — Corroboration and crowd rows
E-C2, E-C3 (trial source), E-M1, E-M3, E-M4, E-M6, E-L5, U-F13.
- **Gate:** `uv run pytest tests/test_phase1b_fusion.py -q` on `c3-scenario` (7 + 7 → keeps `likely` under the
  unchanged rule, corroborated with ≥ 6 s overlap), `single-coincidence` (no bypass), `two-separated`
  (bypass), `crowd-contradiction`; golden green.

### 1b-iii — Per-process breaker and corpus scorer
- Breaker (§2.3.5 a/b/c with the sample minimum, per process); `scripts/score_corpus.py --run-list <json>
  --out <json>` — run list `{"recipe": "deep", "runs": [{"mix_id", "truth", "episodes", "min_track_ms":
  30000}]}`; pre-filters episodes with `present.exports.hidden_reason`; calls `idea benchmark score --truth
  --episodes --out` per mix; **aggregation = pooled counts across mixes** (precision numerators/denominators
  summed over predictions, recall over truth rows); **outputs e4 integers** `likely_precision_e4` (from
  `empirical_tier_precision_e4["likely"]`), `listed_precision_e4` (`selective_precision_e4` post-filter),
  `work_recall_e4` (`overall.identification_work.recall_e4`).
- **Gate:** `uv run pytest tests/test_phase1b_breaker_scorer.py -q` (rate-open needs ≥ 20 samples; cooldown
  30 min; daily budget → open until UTC midnight; latch after 3; scorer on `tests/fixtures/corpus-mini/`
  reproduces `expected.json` exactly).

### 2b — Retention and sidecar pruning
E-M8, E-S2.
- `pruned_upstream`; verifier; re-derivation chain; retention per §4.6 incl. `.trash/`; `idea gc`;
  `--keep-intermediates`; manifest sizes.
- **Gate:** `uv run pytest tests/test_phase2b_retention.py -q` (`tone-3600s.wav` → manifest total ≤ 25 MB after
  `gc --policy hosted`; Deep upgrade after pruning re-derives windows; after PCM expiry re-fetch →
  `source_changed` when altered; every terminal state's rule; trash purge after 7 d); profiles + golden green.

### 3a-i — Canonical projection
- **Gate:** `uv run pytest tests/test_projection.py -q` on `tests/fixtures/present/{garage,boiler,crowd}.json`;
  `PAGE_VERSION` bump.

### 3a-ii — Honesty, accessibility, mobile
- **Gate:** `uv run pytest tests/test_phase3a_honesty.py -q`; `uv run python scripts/check_page_js.py`;
  screenshots and the axe report (`scripts/screenshot_pages.ps1`, vendored `axe-core@4.10.2`) are **non-blocking
  evidence**, committed when the owner runs them.

### 4a-i — Service API and packaging
- **Gate:** `uv run pytest tests/test_service_api.py -q` (resume from checkpoint `primary` → 0 AudD requests);
  `uv build` then `uv run python -c "import zipfile,glob;z=zipfile.ZipFile(glob.glob('dist/*.whl')[0]);
  assert any(n.startswith('idea_web/') for n in z.namelist())"`.

### 4a-ii — FastAPI parity (loopback)
- **Gate:** `uv run pytest tests/idea_web/test_parity.py -q`; `pwsh scripts/smoke_serve.ps1`; owner
  `scripts/gate_local_mode.ps1`.

### 4a-iii — Server-side UI fixes, static assets, headers
- **Gate:** `uv run pytest tests/idea_web/test_headers_forms.py -q`.

### 4b-i — Durable queue, intake, worker, checkpoints
- **Gate:** `uv run pytest tests/idea_web/test_worker.py -q` (kill after `primary` → resume at `hints` with 0
  AudD requests; dead letter after 3; intake → analysis transition; checkpoints only after durable artefacts).

### 4b-iv — Coalescing, subscribers, cancel/drain
- **Gate:** `uv run pytest tests/idea_web/test_coalescing.py -q` (two users, same key → one run, two
  subscribers, both reserved; initiator detaches → payer transfer to the earliest subscriber incl. USD
  re-attribution and the cap-overage audit path; last detach → `cancelled` with in-flight attempts settled;
  private scope never coalesces).

### 4b-ii — Progress, operations, shared breaker
- **Gate:** `uv run pytest tests/idea_web/test_ops.py -q`.

### 4b-iii — Backups and restore
- **Gate:** `uv run pytest tests/idea_web/test_backup.py -q` (backup taken while a bundle commit and a GC run
  are in progress → every listed artefact exists and verifies; GC skipped its cycle; restore drill from
  `tests/fixtures/snapshot/` passes).

### 4c-i — Users, sessions, passwords, CSRF
- **Gate:** `uv run pytest tests/idea_web/test_auth.py -q`.

### 4c-ii — Admin-created accounts, admin reset
- **Gate:** `uv run pytest tests/idea_web/test_accounts.py -q` (admin-created account pre-verified; admin
  reset link works once; XSS via titles/comments escaped; no self-signup route).

### 4d-i — Tenancy schema, lots, worker-side reservations
- **Gate:** `uv run pytest tests/idea_web/test_credits.py -q` (20 concurrent 60-min intakes vs 150 min →
  exactly 2 reserved, the rest `quota_exceeded` with bytes kept; soonest-expiring lot first; settlement per
  status; expired plan-lot remainder forfeited, pack/admin remainder returned).

### 4d-ii — Compatible-result serving, submission order, Deep-on-Free delta, caps, sources
- **Gate:** `uv run pytest tests/idea_web/test_cache_serving.py -q` (zero-balance user still receives a
  compatible cache hit; free served by deep only with the flag; d=1 serves d=2, not vice versa; YouTube link
  refused on free; 151-min mix → `too_long` after intake with minutes released; Deep-on-Free FakeShazam gets 0
  requests; `cache_hits_per_day` enforced).

### 4d-iii — Library, usage meter, admin essentials
- **Gate:** `uv run pytest tests/idea_web/test_library_admin.py -q`.

### 4d-iv — USD ceilings
- **Gate:** `uv run pytest tests/idea_web/test_usd_caps.py -q` (account-month and global-day caps refuse
  reservations; re-attribution overage audited).

### 6a-i — Ingest policy and SSRF
- **Gate:** `uv run pytest tests/idea_web/test_ingest_policy.py -q`.

### 6a-iii — Container
- Dockerfile (`ARG BUILD_ID` → `/app/BUILD_ID` + label), `compose.yml` (web, worker, Caddy) and
  `compose.gate.yml` (single `gate` service, `IDEA_SERVICE=gate`, loopback, no Caddy), secrets file, migration
  `down` test.
- **Gate:** `docker compose -f compose.gate.yml run --build gate` runs the M1 suite inside the image;
  `docker compose up` **refuses** to start web without `/data/hosted-ready.json` (test).

### 6a-iv — Hosted-ready gate, backups schedule
- `scripts/gate_hosted_ready.sh` (inside the `gate` service): the 4a–6a selectors, an end-to-end analysis of
  the authorised source named in `IDEA_GATE_SOURCE_URL` (owner-provided, documented in `docs/LAUNCH.md`), a
  5-hour synthetic refusal, restart-during-job recovery, the restore drill; on success writes
  `/data/hosted-ready.json` `{build_id, gate_suite_sha, passed_at}`. Litestream + nightly `idea backup`.
- **Gate:** the script exits 0; `docker compose up` then starts web; deleting the file or rebuilding with a
  new `BUILD_ID` makes it refuse (test).

### 6b — M1 build-complete checklist
- Policy pages; attribution slot; `/admin` (queue, spend, breaker, disk, grants, disable/delete, audit);
  runbooks; canary; `docs/LAUNCH.md` with the L1/L2/L3/L5 sign-off table; `scripts/gate_beta_config.py`.
- **Gate:** `uv run python scripts/gate_beta_config.py`; `docs/LAUNCH.md` complete except the owner sign-offs.
  **Beta activation is the owner's recorded sign-off, not this gate.**

### M2 (after the beta; before any public launch)
- **7a-i — Billing `local` + monthly-lot scheduler** (gate `tests/idea_web/test_billing_local.py`).
- **7a-ii — Stripe sandbox** (tables, webhooks, portal, refunds/disputes incl. restoration; gate
  `uv run pytest -m live tests/test_stripe_sandbox.py` with `stripe listen`; no real charge).
- **7b — Sharing and publication** (`/s/<token>`, OG, explicit publish, takedown/DMCA; tables added here).
- **7c — Invites/self-signup, uploads, YouTube egress, email provider, signup controls** (S2 before this).
- **7d — Visual polish** (U-F16/17/21–28/35).
- **7e — Job-complete email, extra admin UI, pack sizes/annual edge cases.**
- **7f — Physical deletions + startup cleanup** (`scan.py` path, ACRCloud, `tl1001`; lazy imports + `dev`
  extra (E-L1); tag `pre-v2-removals`).

---

## 6. Owner decisions, launch gates, risks

### 6.1 Owner decisions (2026-09-09) — constraints

| # | Decision | Trade-off / mitigation |
|---|---|---|
| **D1** | Hosted free tier runs on unofficial Shazam (round-1 P0 #2 declined). | ToS exposure accepted; capacity ≈ 40–65 mixes/day per egress; §2.3.5 breaker; kill-switch |
| **D2** | Deep = AudD sweep first, Shazam sampled second. | $0.80–2.00/hour; allowances after L1 |
| **D3** | Panako kept; web wiring fixed. | not in the hosted image |
| **D4** | YouTube kept when hosted. | paid tier only (D7), M2; residential egress; S2 |
| **D5** | Subscription + non-expiring packs both built. | M2; beta with billing off |
| **D6** | Consumer product as acquisition layer; pro path kept open. | corpus funding later |
| **D7** | Free = SoundCloud + Mixcloud links only; YouTube and uploads are paid features. | £0 per free mix |
| **D8** | Breaker trips automatically on throttling and latches after 3 trips/day; manual kill-switch. | — |

### 6.2 Open questions (defaults set)

1. Free 150 min/week; caps 150/240 min — config. 2. Density 2 as Deep default after L1/L3? 3. Desktop build —
recorded option, not adopted.

### 6.3 Launch gates

| | Gate | Owner |
|---|---|---|
| **L1** | **Licensing cleared 2026-09-10** — the AudD API Terms (text of 2026-07-07, recorded verbatim in `docs/legal/audd-terms-2026-07-07.md`, assessed in `docs/legal/audd-terms-assessment.md`) permit use and display of Results within our own products, permit caching, and require no attribution from paying customers. **Remaining, commercial only:** per-clip subscription rate, documented rate limit/concurrency, whether 429s bill, reconciliation of ambiguous requests — one email to api@audd.io; they set the Pro allowance in `pricing.toml`, not launch. Restrictions to honour: no standalone Results data product; no competing recognition service (keep the L3 accuracy report about IDea's tiers, not a provider benchmark); flow-down of AudD's use restrictions to our users (L2). | Nathan (email) |
| **L2** | Legal review of ToS/privacy/DMCA and ingestion posture | Nathan + solicitor |
| **L3** | **Release gate:** ≥ 5 owner-verified mixes, ≥ 3 DJs, ≥ 2 platforms, ≥ 4 h, two-pass truth → `data/corpus/release-1/`; `uv run idea analyse <url> --recipe free|deep` per mix; `uv run python scripts/score_corpus.py --run-list data/corpus/release-1/runs-<recipe>.json --out docs/accuracy/release-1-<recipe>.json`; thresholds `likely_precision_e4 ≥ 9000`, `listed_precision_e4 ≥ 8000`, `work_recall_e4 ≥ 7500` (`deep`) / `≥ 7000` (`free`); also decides `serve_free_from_deep`. | Nathan (+ 1b-iii tooling) |
| **L4** | Stripe business country, KYC, live keys, VAT — **payment launch (M2), not beta** | Nathan |
| **L5** | Domain, email provider (M2 for delivery; not needed for admin-created beta accounts), restore drill on the real host | Nathan + 4b-iii/6a-iv |
| **L6** | Beta ≥ 2 weeks with billing off before M2 7a-ii is enabled | — |

**Private-beta activation = recorded sign-off of L1, L2, L3, L5 in `docs/LAUNCH.md`.**

### 6.4 Risks

| # | Risk | L / I | Mitigation |
|---|---|---|---|
| R1 | Shazam blocks the server IP | medium / high | breaker + latch; kill-switch; Deep unaffected |
| R2 | YouTube blocks datacenter fetches | high / medium | paid-only (M2); residential egress; S2 |
| R3 | Only walk-up AudD rate | medium / medium | density 2; §3.3 table; USD ceilings |
| R4 | Weak willingness to pay; Beatport | high / medium | tiny fixed costs; beta first; pro hedge |
| R5 | Frozen-profile byte test | certain / low | recipes are data; golden semantic |
| R6 | Cycle too big | medium / low | 39 cycles ≤ ~1 day each; split at > 1,500 changed lines |
| R7 | Disk fills | certain w/o 2b / high | retention for all terminal states; trash purge; manifest gate; alarm |
| R8 | Paid run fails after spend | medium / medium | admission + attempt states; settlement rules |
| R9 | Free-tier abuse | medium / medium | lots; admin accounts (M1); per-IP limits; budgets; cache-hit ledger |
| R10 | AudD terms unknown | certain / high | L1 blocks hosted use |
| R11 | Throttled Shazam thin runs | certain today / high | E-S1; `partial`; breaker |
| R12 | Free overwrites Deep | certain today / medium | bundles + `serves()` |
| R13 | Restart double-bills AudD | medium / medium | `dispatched` before I/O; bounded by in-flight concurrency; L1 reconciliation |
| R14 | Inconsistent backups | medium / medium | lock-before-snapshot + hard-link + trash + verify |
| R15 | Mutable source URL | medium / low | alias revalidation; `source_changed` |

---

## Appendix A — register

| Item | Where |
|---|---|
| E-C1, E-C4, E-H2, E-H3, E-S4, E-S5 (fakes), `/healthz` | 0a-i |
| E-H1, E-S3, money | 0a-ii |
| status matrix, `--allow-degrade`, `targeting:0` | 0a-iv |
| E-H8, E-H9, E-M9, E-L4, U-F5 (loopback), `tl1001` | 0a-iii |
| E-C3 anchor, E-H5 | 0b-i |
| E-H4, attempt journal | 0b-iii |
| E-H6, E-M2, E-M10, E-S1 | 0b-ii |
| bundles, manifests, publication invariant | 1a-i |
| audio route, E-M7, Windows gate | 1a-iii |
| `analysis_key`, `serves()`, Deep-on-Free, `source_changed`, E-M6 | 1a-ii |
| targeting (E-C2 fix), `targeting:1` | 1b-i |
| E-C3 trial source, E-M1, E-M3, E-M4, E-L5, U-F13, E-S6 | 1b-ii |
| breaker, L3 scorer | 1b-iii |
| E-M8, E-S2, trash | 2b |
| U-F2/F3/F14 | 3a-i |
| U-F4, F7, F8, F10, F11, F12, F19, F20, F31 | 3a-ii |
| E-L2, service API, packaging | 4a-i |
| parity, U-F6 | 4a-ii |
| U-F1, F15, F18, F29, F30, F34 | 4a-iii |
| E-H7 queue, intake, R13 | 4b-i |
| coalescing, payer transfer | 4b-iv |
| U-F9, U-F33, shared breaker | 4b-ii |
| R14 | 4b-iii |
| auth | 4c-i / 4c-ii |
| lots, reservations | 4d-i |
| serving, submission order, caps, D7 | 4d-ii |
| library, admin | 4d-iii |
| USD ceilings | 4d-iv |
| SSRF, egress, aliases | 6a-i |
| container, build id | 6a-iii |
| hosted-ready, backups schedule | 6a-iv |
| policy, launch sign-off table | 6b |
| billing local + scheduler, Stripe, sharing (U-F39), invites/uploads/YouTube/email/signup controls, polish, email/admin extras, deletions + E-L1 | M2 7a–7f |
| **Round-5 required changes** | 1 → §2.3.2–2.3.3 (`auth_error`, `quota_error`) · 2 → `/healthz` in 0a-i · 3 → `targeting:0` provisional scheduler in 0a-iv, `algorithm_version` gating · 4 → 1a-ii/4d-ii gates corrected · 5 → §3.5 provisional subscriber reservations + payer transfer · 6 → `BUILD_ID`, `IDEA_SERVICE=gate`, `compose.gate.yml`, `--build` · 7 → §4.6 lock-before-snapshot + hard-link + trash + publication invariant · 8 → `pricing.toml` 90/55 · 9 → 1b-iii pooled e4 aggregation, real scorer paths · 10 → §2.3.5 breaker outcomes/denominator/min sample · 11 → `serve_free_from_deep` in schema, `accept_degraded` in `RunRequest`, exact version equality · 12 → `event_id`/`seq`, dispute restoration · 13 → splits (0a-iv, 0b-iii, 1a-iii, 4b-iv) + "split at > 1,500 lines" rule · 14 → §3.4 submission order · 15 → §3.6/6b "build complete ≠ beta" · 16 → scheduler → 7a-i, invites → 7c, Stripe schema → 7a-ii, screenshots non-blocking |
| Round-1 P0 #2 | declined — D1 |

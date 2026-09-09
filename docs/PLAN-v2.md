# IDea v2 — from a local tool to a product people pay for

*Plan rev 5 · 2026-09-09 · from four independent adversarial reviews (engines/fusion, UI/product, market,
hosting/payments) against `main` @ `27c36fd`; revised after Codex plan-review rounds 1–4
(`docs/reviews/plan-v2-review-round-{1..4}.md`) and the owner's decisions (§6.1). Appendix A maps every finding
and every round-4 required change to where it is resolved. This document is the **build contract** for the
cycles in §5; a cycle may specify its own internals at build time as long as nothing here is contradicted.*

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

**Shape:** spikes/owner gates (S) in parallel; **M1 = 29 build cycles** to an invite-only private beta (free tier
+ admin-granted Deep, billing off, SoundCloud/Mixcloud links only); **M2 = 6 cycles** after the beta (local
billing simulation, Stripe sandbox, uploads/YouTube egress, sharing, signup controls, polish, deletions). Every
cycle ≤ ~1 day, one commit, a named fixture and an exact command.

---

## 1. What was reviewed and how

| Review | Scope | Method |
|---|---|---|
| **E — engines** (`docs/reviews/v2-review-engines.md`) | `cli._analyse`, `paid_clip`, `scan_targeting`, `fuse/*`, `providers/*`, jobs, disk, tests | code + `dis` + `du` + timing |
| **U — UI** (`docs/reviews/v2-review-ui.md`; `docs/screenshots/v2-before/`) | every screen | live server, screenshots, `node --check`, data audit |
| **M — market** (`docs/research/05-market-2026-09.md`) | 21 competitors, metering, engine terms, demand | web research, cited |
| **H — hosting** (`docs/reviews/v2-review-hosting.md`) | yt-dlp/Shazam from a server, AudD, Stripe, framework, sizing | web research + code |
| **Codex rounds 1–4** | this plan | independent code verification |

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
| **E-S5** (new) | Production signatures the fakes must match: `AudDAdapter.recognize_clip(path: Path, on_attempt) -> dict` (`providers/audd.py:412`); Shazam is driven through `HTTPClientInterface` / `InjectedHTTPClient` (`shazam.py:146`). Scorer fields available for L3: `empirical_tier_precision_e4[tier]`, `selective_precision_e4`, `work_recall_e4` (`benchmark/scorer.py:1003-1043,1122-1130`). | code |

### 2.2 The web layer cannot be hosted as-is (verified)

One in-memory worker thread (`webapp/jobs.py`); restart loses jobs. No users/sessions/CSRF — any website the
owner visits can `POST /analyse` to `127.0.0.1:8765` (U-F5); `validate_target` accepts `file://` and any path
(`webapp/jobs.py:59-84`). Loopback-only by design; HTTP/1.0; 178 KB pages (U-F29). `ProcessLock` raises rather
than coalesces (E-H7). Pages emit `<audio src="../ingest/original.*">` (`present/page.py:1168-1172`; U-F6).
Stale-page GET rewrites `index.html` but not exports (U-F14/F3). `fuse/episodes.json` and `present/tracklist.*`
are mutable singletons. `_load_cached` hashes the retained original (`ingest.py:143-164`). The wheel packages
only `src/id_detector` (`pyproject.toml:35-36`).

### 2.3 Engine sequencing — Deep scan v2

**Owner decision D2:** AudD sweeps first, Shazam second.

#### 2.3.1 Recipes (frozen data in `src/id_detector/recipes.py`; `recipe_id = sha256(canonical JSON of every field below)`)

| Field | `free` | `deep` |
|---|---|---|
| `primary_engine` / `primary_density` | shazam / 1 | audd / 1 (config `deep.primary_density=2` = even-indexed frozen windows) |
| `secondary_engine` | — | shazam |
| `secondary_clips_per_minute` | — | 2 → `C = ⌈duration_min × 2⌉` |
| `secondary_reserve_fraction` | — | 0.25 → `R = ⌊0.25 × C⌋` |
| `secondary_priority` | — | `hint_only → listed_not_confident → suppressed_challengeable → blank` |
| `eligibility_min_intersection_ms` | — | 4000 |
| `suppressed_min_votes` | — | 2 |
| `reserve_search_ms` / `reserve_min_separation_ms` | — | 45000 / 30000 |
| `overlap_min_ms` / `separation_min_ms` | — | 6000 / 60000 |
| `primary_achieved_fraction` / `secondary_achieved_fraction` | 0.80 / — | 0.95 / 0.80 |
| `anchor_max_ms` / `anchor_slack_ms` | — | 86 400 000 / 12 000 |
| `audd_concurrency` | — | 4 |
| `retry_policy` | shazam: existing limiter | audd: `connect_error`, `http_429`, `http_503` only; ≤ 3; backoff 1/2/4 s |
| `max_usd_e2` (hard per-run cap) | **0 = paid calls forbidden** | **900** |
| `adapter_versions` | shazam:1 | shazam:1, audd_clip:2 |
| `algorithm_version` | `fusion:1` | `targeting:1,fusion:1` (bumped whenever any step-4/5 rule changes) |
| `requires` | `shazam_sweep` | `audd_sweep`, `shazam_secondary` |

`presentation_version` is not in the recipe. Frozen profiles are untouched; their `budget.max_usd_e2` is never
read by the clip path (E-S3). `max_paid_clips` is retired.

#### 2.3.2 Money: reservation, admission, settlement

- Price: `audd_usd_e6_per_request` from `pricing.toml` (§3.3), recorded on the run. **Outcome costs:** `match`,
  `no_match`, `timeout_post`, `http_5xx` (≠ 503), `malformed` → 1 unit; `connect_error`, `timeout_pre`,
  `http_429`, `http_503` → 0 (assumption: AudD does not bill throttled/unavailable responses; L1 confirms;
  config `bill_on_throttle=false`).
- **Reservation** (before any network I/O): `planned = ⌈windows / density⌉`;
  `usd_e6_reserved = planned × price_e6 × 1.05` (headroom for retries); `usd_e2_reserved = ⌈usd_e6_reserved / 10⁴⌉`;
  refused with `budget_exhausted` if `usd_e2_reserved > min(recipe.max_usd_e2, AppConfig.max_usd_e2 or ∞,
  account_month_remaining, global_day_remaining)` (the last two hosted-only, §3.3).
- **Admission (the hard cap):** immediately before each potentially billable dispatch (any AudD attempt),
  atomically decrement the run's `usd_e6_remaining` by one unit; if it would go negative, **do not dispatch**:
  the primary stops, the run ends `partial` with `reason=reservation_exhausted`. Zero-cost outcomes refund
  their unit on resolution. Retries re-admit. Total billed ≤ reservation by construction.
- **Settlement** at terminal status: `usd_e2_spent = ⌈Σ unit costs / 10⁴⌉`; the remainder is released.

#### 2.3.3 Attempt state machine (every provider request)

Durable rows (`recognise/attempts.jsonl` locally; `provider_attempt_events` hosted), append-only:
`prepared(attempt_id, query_id = clip cache key, ordinal, parent_attempt_id, unit_usd_e6)` → **`dispatched`
(written before entering network I/O)** → `resolved(outcome ∈ {match, no_match, http_429, http_503, http_5xx,
malformed, connect_error, timeout_pre, timeout_post})`; `ambiguous` is the derived class {`timeout_post`,
`http_5xx`, `malformed`} — spent, never auto-retried. On resume: `prepared` without `dispatched` → re-issue;
`dispatched` without `resolved` → treated as `ambiguous` (spent). Response cache receives only `match|no_match`;
`--refresh-states no_match,http_429` (default) never re-bills `match`.

#### 2.3.4 Steps (deep)

1. **Intake and resolve** — worker resolves the alias, fetches (or takes the upload), computes `media_key`,
   duration, `hints_snapshot_id`; builds `analysis_key = sha256(media_key, recipe_id, source_kind ∈ {platform,
   upload, local}, tenant_scope ∈ {public, user:<id>}, hints_snapshot_id, manual_tracklist_sha256 | "",
   panako_index_id | "")` — `tenant_scope = user:<id>` whenever `source_kind = upload`, a manual tracklist is
   present, or a private index is used; then one transaction: find a compatible stored result (§3.4) or a
   non-terminal run to attach to, else reserve credits + USD and start.
2. **Primary sweep (AudD)** — admission per dispatch; each observation gets `simultaneous_source="audd"` and an
   anchor from `timecode` iff numeric, `0 ≤ timecode_ms ≤ anchor_max_ms`, and `≤ duration + anchor_slack_ms`
   when the match carries a duration; else `anchor=None` (still a vote). `audd_sweep` achieved iff
   resolved `match|no_match` ≥ 95 % of `planned`.
3. **Hints → first fuse.**
4. **Secondary (Shazam)** — candidates in priority order with spans; eligible windows = frozen windows whose
   intersection with the span ≥ `eligibility_min_intersection_ms`. **Shazam windows may and should coincide
   with AudD windows — the only exclusion is a Shazam window already picked in this run** (round-4 P0 #1).
   Allocation with `A = C − R`: (i) one window per candidate in priority order, ties by longer span then earlier
   start, until `A` is exhausted (lower-priority candidates beyond that get none); (ii) remaining `A − k` by
   largest-remainder proportional to span duration, ties by earlier start; (iii) within a span rank by
   intersection desc, RMS energy desc, start asc; a duplicate pick is replaced by the next-ranked window in the
   same span; a span with no eligible windows returns its quota to (ii). Reserve `R`: a new identity found in a
   blank at `[m₀, m₁]` queues the two eligible windows with starts in `[m₀ − 45 s, m₁ + 45 s]` (excluding the
   probe) that are farthest apart and ≥ 30 s apart (one if only one exists); unused `R` is distributed by (ii)
   at the end. `shazam_secondary` achieved iff resolved ≥ 80 % of allocated.
5. **Re-fuse once.** Corroboration = **selected votes** for the same normalised work with supports overlapping
   ≥ `overlap_min_ms`, from different families — `catalogue` {audd, acrcloud}, `shazam`, `local_index`
   {panako}; crowd hints stay hint corroboration. One cross-family agreement → "confirmed twice" shown;
   bypassing suppression/the 30 s floor needs two agreements ≥ `separation_min_ms` apart **or** the existing
   `likely` rule (T_ind ≥ 4, on-air ≥ 30 s).

#### 2.3.5 Status matrix (one table; 0a/0b tests assert exactly this)

| Situation | `free` | `deep` |
|---|---|---|
| First AudD attempt(s) resolve `http_401/403`, quota error body, or `connect_error` after retries, before any successful/ambiguous attempt | — | **`provider_unavailable`**, exit 3, nothing spent, no result stored. Local CLI `--allow-degrade` **restarts the request as the `free` recipe before any paid work** (`requested=deep`, `achieved=free`, status `degraded`, exit 0) — a recipe substitution, never a mid-run swap |
| Primary achieved fraction not met (`free`: Shazam resolved < 80 %, e.g. throttled; `deep`: AudD resolved < 95 %, incl. `reservation_exhausted`) | **`partial`**, exit 0 | **`partial`**, exit 0 |
| Primary met, `shazam_secondary` < 80 % (breaker open / throttled) | — | **`degraded`**, exit 0 |
| All requirements met | `complete` | `complete` |
| Reservation > effective cap | — | `budget_exhausted`, exit 4, nothing spent |
| Re-fetched bytes ≠ `media_key` | `source_changed`, 5 | `source_changed`, 5 |
| Exception / cancel | `failed` 1 / `cancelled` 130 | same |

Results with status `complete|degraded|partial` are **shown** to the requester (with a banner for the latter
two); only `complete` (and `degraded` when the request sets `accept_degraded`, local only) is **served** to
later requests (§3.4). `partial` results keep their bundle for the requester's history and are GC-eligible like
any other bundle (§4.6).

**Breaker (Shazam):** three independent mechanisms — (a) rolling 5-minute failure rate > 30 % → open for
30 min; (b) `shazam_daily_budget_per_egress` exhausted → open until 00:00 UTC; (c) **latch:** three (a)-opens
in one UTC day → off until an admin re-enables. Open → new free jobs wait (`waiting`), a running deep secondary
is skipped → `degraded`, a running free primary continues (its failures make it `partial`). Per-process in
1b-iii; service-wide over `provider_attempt_events` from 4b-ii. Manual `IDEA_ENGINE_SHAZAM=off`. Never a
mid-run engine swap; never proxied. A "sparse AudD" free tier is **not** a contingency the system chooses —
it is a post-L1, operator-only configuration change.

#### 2.3.6 Cost per 60-minute mix (400 windows; $5/1,000 walk-up · $2/1,000 subscription)

| Recipe | AudD req | Shazam req | AudD $ | Wall-clock (after 0b) |
|---|---|---|---|---|
| `free` | 0 | 400 | $0 | 9–60 min |
| `deep` d=1 | 400 | ≤ 120 | $2.00 · $0.80 | ~4 + 3–10 min |
| `deep` d=2 | 200 | ≤ 120 | $1.00 · $0.40 | ~2 + 3–10 min |

ACRCloud removed (same family as AudD). Panako kept (D3) as `local_index`; `panako_index_id` is in `analysis_key`.

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
(`IDEA_MODE=hosted`): **startup requires `/data/hosted-ready.json` whose `image_digest` equals
`IDEA_IMAGE_DIGEST`** (set by compose from the image label); the file is written only by the gate container
(6a-iv) — regardless of bind address. The special value `IDEA_HOSTED_READY=gate` allows a loopback-only start
inside the gate container itself.

### 3.2 Scans

| | Free scan (`free`) | Deep scan (`deep`) |
|---|---|---|
| Engines (D1) | Shazam sweep + crowd hints | AudD sweep → hints → Shazam sampled (overlapping) → re-fuse |
| User sees | badges, "from comments", run status | + "confirmed twice", tighter boundaries, rescued short tracks when doubly confirmed |
| API cost / hour | $0 (Shazam capacity) | $0.80–2.00 (d=1) |
| Shazam throttled | `partial` with failed-window count; breaker pauses new free scans | `degraded` (AudD-only) |

### 3.3 Tiers, caps, prices

Unit = **minutes of new audio**, rounded up. Free lots reset weekly (Monday 00:00 UTC). Pro lots are created
monthly on the subscription anniversary (day clamped to month length) by `idea billing grant-monthly` (daily,
idempotent by `(subscription_id, YYYY-MM)`); unused Pro minutes expire when the next lot is created; packs never
expire; allocation order = soonest-expiring lot first.

**Single pricing authority: `pricing.toml`** (repo root; created in 0a-ii; `pricing_version` field). Fields:
`audd_usd_e6_per_request = 5000`, `bill_on_throttle = false`, `free.minutes_per_week = 150`,
`free.max_mix_minutes = 150`, `free.sources = ["soundcloud","mixcloud"]`, `pro.price_gbp_month = 9`,
`pro.price_gbp_year = 90`, `pro.deep_minutes_per_month = 110` (**provisional walk-up d=1 row until L1**),
`pro.max_mix_minutes = 240`, `pro.sources = ["soundcloud","mixcloud","youtube","upload"]`,
`pack.small = {gbp = 6, minutes = 70}`, `usd_cap_account_month_e2 = 2000`, `usd_cap_global_day_e2 = 5000`,
`cache_hits_per_day = 20`, `hints_max_age_days = 7`, `alias_revalidate_days = 7`. `AppConfig` loads
`audd_usd_e6_per_request` and `max_usd_e2` (optional; absent = no operator cap) from it; the recipe's
`max_usd_e2` is never raised by pricing. Every reservation records `pricing_version`.

| | **Free** | **Pro** | **Packs** |
|---|---|---|---|
| Price (GBP incl. VAT) | £0, no card | £9 / month; £90 / year | from £6 |
| Allowance | 150 min / week, `free` recipe | Deep minutes / month per `pricing.toml` | Deep minutes, never expire |
| Sources | SoundCloud, Mixcloud links (D7) | + YouTube links, uploads (M2) | as Pro |
| Mix length cap | 150 min | 240 min | 240 min |
| History | 30 days | unlimited | 90 days |
| Compatible cached result | free, uncounted, `cache_hits_per_day` | same; Deep-on-Free pays the AudD sweep | same |

**COGS per Deep hour** = AudD 400 req × 1.05 + YouTube proxy share (50 % × $0.30 = $0.15) + compute/storage/
bandwidth/backup $0.10 + **3 % of gross allowance for refunded failures/outages** ($0.35 on £9). £9 → $11.50
gross − VAT 20 % − Stripe (1.5 % + 20p + 0.7 %) ≈ **$8.90 net**; £6 → ≈ $5.75 net; target ≥ 50 % margin →
spendable ≈ $4.10 (Pro) / $2.60 (pack):

| AudD rate | Deep $/hour (incl. allowance) | Pro £9 → minutes | Pack £6 → minutes |
|---|---|---|---|
| walk-up, d=1 | $2.35 + $0.35 = $2.70 | **≈ 90** (provisional row rounds to 110 only if L1 confirms no attribution/branding cost; else set 90) | ≈ 55 |
| walk-up, d=2 | $1.30 + $0.35 = $1.65 | ≈ 150 | ≈ 95 |
| subscription, d=1 | $1.09 + $0.35 = $1.44 | ≈ 170 | ≈ 110 |
| subscription, d=2 | $0.67 + $0.35 = $1.02 | ≈ 240 | ≈ 150 |

**No price is published and no third-party Deep scan runs before L1** (beta included).

### 3.4 Results, compatibility, coalescing

- **Result bundle** = `present/bundles/<bundle_id>/` (`bundle_id = sha256(run_id, presentation_version)`): page,
  exports, `manifest.json` (files + hashes + sizes, `run_id`, `analysis_key`, `requested_recipe_id`, `achieved`,
  `status`, `presentation_version`, `pricing_version`, `usd_e2_spent`); fuse artefacts in `fuse/runs/<run_id>/`
  with their own `manifest.json`. Bundles are immutable; refresh writes a new bundle for the same run. Local
  mode keeps `present/current` → the newest `complete` run's latest bundle; hosted mode has no global pointer.
- **`serves(stored, request)`** (`compat_version = 1`):

| Request | Served by a stored result iff |
|---|---|
| `free` | stored `free complete` with equal `analysis_key`; **or** stored `deep complete` (any density) with the same non-recipe key inputs — **only when `pricing.toml` `serve_free_from_deep = true`, which is set after L3 demonstrates Deep ≥ Free on the release corpus** (default false) |
| `deep` d=2 | stored `deep complete` d=2 or d=1, same non-recipe key inputs |
| `deep` d=1 | stored `deep complete` d=1, same inputs |
| any | equal `algorithm_version` and adapter versions ≥ requested; `degraded` only with `accept_degraded` (local); `partial` never; `tenant_scope` must match (private results are never served across users) |

  **Deep-on-Free:** a `deep` request finding only a `free complete` result (same inputs, same scope) reuses its
  Shazam observations as secondary evidence, runs the AudD sweep, re-fuses; reservation = primary only.
- **Coalescing key = `analysis_key`** (includes scope); a request with a non-terminal run attaches as a
  subscriber. Provider raw responses (content-addressed by clip) are shared across all users; everything
  derived from private inputs is scoped.
- **Presentation inputs** (title, uploader, embed) come from the alias the requester used
  (`library_items.alias_id`), not from the analysis.
- **Sidecars after pruning** (E-S2): `pruned_upstream` entries; verifier accepts them when the artefact hash
  matches; consumers re-derive (windows ← PCM ← original ← re-fetch); **re-fetched bytes must hash to
  `media_key` or the run ends `source_changed`**.
- **Source aliases:** `(url, canonical_url, media_key, valid_from, valid_to, last_verified)`; revalidated after
  `alias_revalidate_days` via `yt-dlp --skip-download` metadata; mismatch closes the row and the fetch creates a
  new media.

### 3.5 Credits: lots, allocations, settlement

- `credit_grants(id, account, kind ∈ {plan, pack, admin}, minutes_total, minutes_remaining, expires_at,
  source_ref)`; `credit_allocations(reservation_id, grant_id, minutes)`; `credit_events` (append-only audit).
  Admin comps are `kind = admin` lots — there is no separate grants table.
- **Reservation** happens in the worker after intake (§2.3.4 step 1) in one `BEGIN IMMEDIATE` transaction with
  the balance check, allocations from soonest-expiring lots, the USD reservation, and the run row. Submit-time
  pre-check: refuse a submission when the account has 0 minutes remaining (cheap; avoids pointless downloads).
- **Settlement / release** in one transaction at terminal status, in this order: (1) compute settled minutes
  (below); (2) consume allocations lot by lot in allocation order; (3) release the remainder to the same lots
  **unless the lot has expired, in which case plan-lot remainder is forfeited and pack/admin remainder is
  returned**; (4) write events. Rules: `complete`, `degraded` → 100 %; `partial` → 100 % if ≥ 90 % of primary
  resolved, else 50 %; `provider_unavailable`, `budget_exhausted`, `source_changed`, `failed`, `cancelled`
  → 0 %; coalesced subscribers pay 0; **if the initiator detaches while others remain, its reservation is
  released and the earliest-attached remaining subscriber's reservation becomes the settling one**; when the
  last subscriber detaches, the cancel token fires, in-flight dispatched attempts resolve and are settled as
  spent, and the run ends `cancelled`. Released-but-spent runs still count toward `usd_cap_account_month_e2`.
- **USD:** `usd_reservations(id, run_id, account, usd_e6_reserved, usd_e6_remaining, pricing_version)` and
  `usd_events` (reserve / admit / refund_unit / settle / release); `account_month` and `global_day` remaining =
  cap − Σ(reserved-not-yet-settled + settled) in the period.
- **`cache_hits(user, day, count)`** enforces `cache_hits_per_day`.
- Packs bought via Stripe are lots with `source_ref = order_id`; `refunds(id, order_id, stripe_refund_id,
  amount_e2, minutes_reversed, grant_id, dispute_id, at)`; a refund/dispute reverses `min(remaining,
  refunded_minutes)` from that lot (M2 7a-ii).

### 3.6 Sign-in, payments, beta (decided)

Email + password (argon2-cffi); server-side sessions; CSRF + Origin. `BILLING_BACKEND = off | local | stripe`
(`local` and `stripe` in M2). **Beta (end of M1):** `BILLING_BACKEND=off`; accounts **created by the admin,
pre-verified**, invite codes for any self-signup; password reset performed by the admin (email provider in M2);
free tier + admin-granted Deep lots; SoundCloud/Mixcloud links only; `IDEA_SHARING=off`,
`IDEA_PUBLIC_CATALOGUE=off`; Panako not in the image.

---

## 4. Hosted architecture

### 4.1 Showstopper checks

| Check | Finding | Consequence |
|---|---|---|
| Unofficial Shazam from a server | ~20 req/min per IP shared by all users → ~40–65 mixes/day per IP; failures past the throttle (E-S1); ASN blocks; no official API | **D1 accepted.** Per-account weekly cap; `provider_attempt_events` per egress; §2.3.5 breaker (rate / daily budget / latch); kill-switch; extra egress IPs as config rows; `trust_env=False` on Shazam and AudD clients; never proxied |
| yt-dlp from a server | SoundCloud/Mixcloud direct; YouTube needs residential egress | **D4/D7:** YouTube paid-only (M2); proxy passed explicitly to yt-dlp for YouTube jobs only; S2 before M2 7c |
| AudD terms | unreadable production terms; trial = evaluation-only + attribution | **L1 blocks any hosted third-party AudD use, beta included** |
| Disk | 330–370 MB/hour; 10–25 MB durable | retention for every terminal state (§4.6) |
| RAM | novelty ~0.7 GB/hour | guarded off (0b-ii); 1.5 GB/job; length caps |

### 4.2 Shape

```
Internet ─:443─▶ Caddy (TLS, gzip, 250 MB cap, headers) ─▶ uvicorn · idea_web (FastAPI) [starts only with a valid /data/hosted-ready.json]
                                                            │ pages · auth/sessions · CSRF/Origin · submit pre-check
                                                            │ POST /analyse → jobs row (state=intake) · status polling
                                                            │ result routes (authz via library_items, traversal-safe) over present/bundles/<bundle_id>/
                                                            ▼
                                                       app.db (SQLite WAL; Litestream) · online-backup snapshots
                                                            ▲ lease / heartbeat / checkpoints / progress / subscribers / reservations
                                                       idea_web.jobs.worker (separate process; N=1)
                                                            │ intake: alias → fetch → media_key/duration/hints → analysis_key → reserve+coalesce (one txn)
                                                            │ id_detector.service.run(RunRequest) ← pipeline public API
                                                            │ retention GC (all terminal states; shares the artefact lock with backup)
                                                            ▼
                                                       work/<source>/<media>/ (content-addressed; durable subset snapshotted)
```

Rules: web never runs a pipeline; worker never serves HTTP; workers never receive user paths (`PlatformUrl |
UploadId | LocalPath(local only)`); `src/id_detector/` changes after Phase 3 limited to `service.py`,
`recipes.py`, `serve` entry point, `PROJECT_ROOT`.

### 4.3 Web layer and service API

FastAPI + Jinja2 + uvicorn (S4 ADR: Codex recommends, owner approves before 4a-ii). `src/idea_web` packaged.
`idea serve` = same app in local mode; `present/server.py` retired after parity. Render policy
`emit_local_audio` false when hosted. Polling 2.5 s.

`id_detector.service.run(RunRequest(run_id, analysis_key, target: PlatformUrl | UploadId | LocalPath,
recipe, hints_snapshot_policy, manual_tracklist: bytes | None, checkpoint_store, attempt_journal, usd_admitter,
progress, cancel_token)) → RunResult(run_id, status, reason, achieved, bundle_id, usd_e6_reserved,
usd_e6_spent, attempts)`. `run_id` caller-supplied; `checkpoint_store` persists phase completion (`ingest,
decode, windows, primary, hints, fuse1, secondary, fuse2, present`); `attempt_journal` and `usd_admitter`
implement §2.3.2–2.3.3. The manual tracklist crosses the boundary as validated bytes, never a path.

### 4.4 Accounts, sessions, abuse

argon2-cffi; server-side sessions (`__Host-idea_session; HttpOnly; Secure; SameSite=Lax`; `sha256(token)`;
rotation on login/plan change; 30 d idle / 90 d absolute); single-use hashed email tokens (M2 for email
delivery; admin-issued reset links in M1); CSRF synchroniser + Origin; per-IP limits (signup 3/day, login
10/15 min, reset 3/h, analyse 5/h; IPv6 /64; trusted proxy config); CSP/security headers; export routes 10/min.
M1 beta: admin-created accounts + invite codes.

### 4.5 Data model (SQLite WAL; numbered SQL migrations with `down` scripts; M1 tables only — sharing/orders/
invoices/refunds arrive with their M2 cycles)

`users` (id, email, pw_hash, verified_at, created_ip, invite_id, disabled_at, deleted_at) · `sessions` ·
`email_tokens` · `invites` · `media` (media_key, duration_ms, first_seen) · `source_aliases` (§3.4) ·
`analysis_runs` (run_id, analysis_key, media_key, requested_recipe_id, achieved JSON, status, reason,
tenant_scope, initiator_user, reservation_id, attempts, checkpoints JSON, usd_e6_reserved, usd_e6_spent,
pricing_version, started_at, finished_at) · `result_bundles` (bundle_id, run_id, presentation_version, path,
manifest_sha256, created_at) · `run_subscribers` (run_id, user, reservation_id, alias_id, attached_at,
detached_at) · `library_items` (id, user, run_id, alias_id, retention_class ∈ {free30, pack90, pro∞}, added_at,
deleted_at) · `jobs` (id, run_id, target JSON, recipe_id, state ∈ {intake, waiting, analysis, …terminal},
lease_owner, lease_until, heartbeat_at, attempt, max_attempts = 3, dead_letter_reason, cancel_requested,
progress JSON, log_path) · `credit_grants`, `credit_allocations`, `credit_events`, `usd_reservations`,
`usd_events`, `cache_hits` (§3.5) · **`provider_attempt_events`** (attempt_id, ordinal, run_id, provider,
egress_id, query_id, parent_attempt_id, state, outcome, http_status, unit_usd_e6, at) — append-only; budgets,
the rolling breaker and resume read it · `entitlements` (user, plan, status, source ∈ {admin, stripe},
anniversary_day, period_end, cancel flags, stripe ids nullable) · `processed_stripe_events` (M2) ·
`admin_audit` (actor, action, target, at).

### 4.6 Jobs, retention, backups

Worker: lease + heartbeat 10 s; states `intake → waiting|analysis → terminal`; checkpoints; `attempt ≤ 3`
then dead-letter (treated as `failed` after 24 h); idempotent bundle commits; cancel/drain per §3.5.
**Retention** (`idea gc --policy local|hosted`): `complete|degraded|partial` → delete `windows/**` now, PCM
after 48 h, original after 7 d (hosted; local keeps it), sidecars rewritten; `failed|cancelled|
provider_unavailable|budget_exhausted|source_changed|quota_exceeded|dead_letter` → delete `windows/**` + PCM
now, original after 24 h, media dir after 7 d if no bundle references it; upload staging expires after 24 h;
jobs with a stale lease and no status → `failed` after `max_attempts`; history expiry removes
`library_items`; a bundle/run is deleted only when no library item, share or publication references it.
**Backups** (`idea backup`, nightly): (1) `sqlite3.Connection.backup()` (online backup API — consistent
without fencing writers) → `snapshot/app.db`; (2) take the **artefact lock** (shared with GC and bundle
commits, ≤ 60 s) → list every `bundle_id`/`run_id` referenced by the copied DB → copy those directories plus
`recognise/`, `hints/`, `ingest/source.json` → release; (3) `snapshot.json` (DB hash + listed artefacts +
their manifest hashes). Restore = DB → listed artefacts → `idea verify-artefacts` (bundle manifests, fuse-run
manifests, and the existing completion sidecars for `recognise/`/`hints/`). Server: Hetzner CX/CPX (~€16/month);
image `python:3.12-slim` + uv + ffmpeg + Deno + `yt-dlp[default,curl-cffi]`, no JDK, no dev tooling;
ffmpeg/ffprobe under `timeout` + memory limits.

### 4.7 Billing (M2)

`BillingProvider` — `create_checkout_url`, `portal_url`, `handle_webhook`, `current_entitlement`. `off` (M1):
admin lots. `local` (7a-i): `/dev/billing/*` simulate pages writing the same rows Stripe would; banner; refuses
`ENV=production`. `stripe` (7a-ii): Checkout subscription (monthly/annual) and payment (packs); Portal; webhooks
`checkout.session.completed`, `checkout.session.async_payment_{succeeded,failed}`,
`customer.subscription.{created,updated,deleted}`, `invoice.paid`, `invoice.payment_failed`, `charge.refunded`,
`charge.dispute.created`; raw-body signature; `event_id` dedupe; re-fetch on event; grant on `active|trialing`,
revoke on `canceled|unpaid`, 7-day grace on `past_due`; Stripe Tax; GBP VAT-inclusive; tables `orders`,
`invoices`, `order_credit_links`, `refunds`; `docs/billing-testing.md` with the exact CLI/test-clock sequence
and expected rows; live needs `sk_live_` and `IDEA_BILLING_LIVE=1`.

### 4.8 Hosted boundary

Allow-listed platform hosts (M1: SoundCloud, Mixcloud) or upload ids (M2); SSRF guard (private/reserved
ranges, DNS rebinding, redirect re-validation); result/export routes authorise via `library_items` with
traversal/symlink checks; originals never served; no rescan route; results private; one privacy sentence
everywhere.

---

## 5. Phases — M1 (29 cycles) then M2 (6 cycles)

Every cycle: Codex builds → `uv run pytest -q` · `uv run ruff check .` · `uv run ruff format --check .` ·
`uv run python scripts/audit_fixtures.py` green → Codex reviews the diff → fix → one commit. Test selectors
are exact. Owner-only checks are `live`-marked or `.ps1`.

**Shared test infrastructure (0a-i):**
- `scripts/make_audio_fixtures.py` → committed `tests/fixtures/audio/tone-60s.wav` (60 s, 16 kHz mono s16,
  three tones with 2 s crossfades → 7 frozen windows); `tone-3600s.wav` generated on demand.
- **Fakes matching production signatures (E-S5):** `tests/fakes/providers.py` — `FakeAudD` with
  `async recognize_clip(self, path: Path, on_attempt) -> dict` (calls `on_attempt` like the real adapter);
  `FakeShazamHTTP(HTTPClientInterface)` returning scripted HTTP responses to the existing Shazam adapter. Scripts:
  `tests/fakes/scripts/*.json` = `{"audd": {"default": "match", "windows": {"3": "no_match", "5":
  "timeout_post"}}, "shazam": {...}}`, outcomes `match | no_match | http_401 | http_429 | http_503 | http_500 |
  timeout_pre | timeout_post | malformed`; `match` payloads carry title/artist per tone and a `timecode`.
- **Injection:** `_analyse(..., paid_scan_adapters=..., shazam_http_client=...)`; `idea analyse
  --fake-providers audd,shazam` (hidden) reads `IDEA_FAKE_SCRIPT`, refused unless `IDEA_TEST_MODE=1`.
- `scripts/assert_journal.py --work-root <dir> --expect k=v…` (newest `invocations.jsonl` entry; non-zero
  exit on mismatch).
- `scripts/smoke_serve.ps1` / `.sh`: start `uv run idea serve --no-open --port 8791` in the background,
  poll `GET /healthz` (≤ 20 s), assert 200 and that `GET /` contains `Drop a mix`, then stop the process;
  non-zero exit on any failure.

### Phase S — spikes and owner gates (parallel; not build cycles)
- **S1 (owner):** AudD production terms + per-clip rate + reconciliation → L1.
- **S2 (owner runs; script from 0a-iii):** `scripts/spike_ingest_vps.sh <sc-url> <mc-url> <yt-url> [proxy]`
  → `docs/spikes/ingest-vps.md`; pass = SC + MC direct; YT via proxy. Blocks M2 7c.
- **S3 (owner runs; script from 0a-iii):** `scripts/spike_shazam_vps.py --minutes 60 --ceiling 45` daily × 5
  → `docs/spikes/shazam-vps.md` → `shazam_daily_budget_per_egress`.
- **S4 (Codex recommends, owner approves):** `docs/adr/0001-web-framework.md` before 4a-ii.

### 0a-i — Crash, cache states, fixtures, fakes, injection, journal assertions
E-C1, E-C4, E-H2, E-H3 (status), E-S4 (relocation).
- Bind `matches`; free failure count only when the free branch ran; `PaidScanResult` → `paid_clip.py`;
  cache states (§2.3.3: only `match|no_match` written; read rejects others; `--refresh-states`); `_refuse_with`
  accumulates; `provider_unavailable` (3) per §2.3.5 (without `--allow-degrade` yet); fixtures, fakes,
  injection, `assert_journal.py`, `smoke_serve.*`.
- **Gate:** `uv run pytest tests/test_phase0a_crash_cache.py -q` (paid success; no-match; all `http_401` →
  exit 3, nothing cached; error bodies never cached; counts accumulate); `pwsh scripts/smoke_serve.ps1`.

### 0a-ii — Recipes, pricing, reservation, admission, statuses
E-H1, E-S3.
- `recipes.py` (§2.3.1) + `--recipe free|deep`; `pricing.toml` (§3.3) + loader; reservation/admission/
  settlement (§2.3.2) with a local in-process `usd_admitter`; journal fields `usd_e6_reserved`, `usd_e6_spent`,
  `usd_e2_*`, `status`, `reason`, `achieved`, `requested_recipe_id`; `budget_exhausted` (4); `partial` on
  `reservation_exhausted`; `--allow-degrade` = recipe substitution (§2.3.5).
- **Gate (PowerShell):**
  ```powershell
  $env:IDEA_TEST_MODE = "1"; $env:IDEA_FAKE_SCRIPT = "tests/fakes/scripts/gate0a-deep.json"
  $w = "$env:TEMP\idea-gate0a"; Remove-Item -Recurse -Force $w -ErrorAction SilentlyContinue
  uv run idea analyse tests/fixtures/audio/tone-60s.wav --recipe deep --fake-providers audd,shazam --work-root $w
  uv run python scripts/assert_journal.py --work-root $w --expect status=complete usd_e6_reserved=36750 usd_e6_spent=35000 usd_e2_spent=4
  ```
  (7 × 5000 × 1.05 = 36 750; 7 resolved × 5000 = 35 000 → 4 ¢.) Plus `uv run pytest
  tests/test_phase0a_money.py -q`: reservation > cap → exit 4 with zero attempts; `max_usd_e2=0` refuses;
  admission stops dispatch at the reservation (script with 429 storms) → `partial/reservation_exhausted`;
  `--allow-degrade` on all-`http_401` → `degraded`, `achieved=free`, zero AudD attempts after the first probe;
  the §2.3.5 matrix row by row.

### 0a-iii — Security and dead paths (local)
E-H8, E-H9, E-M9, E-L4, U-F5 (loopback).
- Drop `upload_consent`; `run_paid_scanners` call site removed; `--engine acrcloud` refused; `tl1001`
  default-disabled; **`local_index_label` wired from the web runner (D3)**; `_windows_in_spans` skips
  transformed windows; loopback CSRF (`GET /csrf` token, form field or `X-CSRF-Token`, plus `Origin`/`Host` ∈
  {`127.0.0.1:<port>`, `localhost:<port>`}); S2/S3 scripts written.
- **Gate:** `uv run pytest tests/test_phase0a_security.py -q`.

### 0b-i — AudD adapter v2: anchor, concurrency, attempt states, cancellation
E-C3 (anchor), E-H4, E-H5.
- Anchor validity rule; `simultaneous_source="audd"`; concurrency 4 + token bucket; retry policy; attempt
  journal with `dispatched` before network I/O; progress per clip; `trust_env=False`.
- **Gate:** `uv run pytest tests/test_phase0b_audd.py -q`: cancel after 3 clips (200 ms fake latency) → ≤ 4
  `dispatched`; `http_429`×2 then `match` → 3 attempt rows, 429s refund their units, one unit billed;
  `timeout_post` → ambiguous, spent, not retried; `timeout_pre` → retried; negative/absent `timecode` →
  `anchor=None`; resume with a `dispatched`-unresolved row → ambiguous.

### 0b-ii — Effective config, novelty, throttle handling, golden
E-H6, E-M2, E-M10, E-S1.
- Config carry-over (both call sites); `idea config show [--profile X]` marks profile-sourced values; novelty
  guarded + hoisted; Shazam decode errors → limiter penalty + failures; `free` → `partial` when resolved
  < 80 %; true scanned window set to the fuser.
- **Golden:** `scripts/make_golden.py` → `tests/golden/local-free/tracklist.json`; semantic comparison
  (`tests/test_golden_local_free.py`) ignoring `run_id`, timestamps, `generated_by`, `requested_recipe_id`,
  `analysis_key`, `presentation_version`.
- **Gate:** `uv run pytest tests/test_phase0b_config.py tests/test_golden_local_free.py -q`.

### 1a-i — Immutable bundles, manifests, refresh, audio route, cached-open
- `bundle_id`, bundle + fuse-run manifests, `present/current` (local), `refresh.py` → new bundle; page audio
  via `/media/<media_key>/audio` (local); `_load_cached` via manifest + `work/index.json` (no original needed).
- **Gate:** `uv run pytest tests/test_phase1a_bundles.py -q` (refresh creates a second bundle; `current`
  follows the newest complete run; cached-open with the original deleted); owner
  `scripts/gate_local_mode.ps1` (runs `idea.cmd`, opens a cached mix, asserts the probe page logs `audio-ok`).

### 1a-ii — Analysis keys, compatibility, Deep-on-Free, source_changed
- `analysis_key` (§2.3.4 step 1), `serves()` table (§3.4), `serve_free_from_deep` config, Deep-on-Free reuse,
  `source_changed`; web runner uses `--recipe`.
- **Gate:** `uv run pytest tests/test_phase1a_compat.py -q` (free→deep reserves primary only and FakeShazam
  gets 0 requests; d=1 serves d=2; d=2 does not serve d=1; free served by deep only when the flag is on;
  degraded/partial never served; private scope never served across users; altered local-file source →
  `source_changed`, exit 5).

### 1b-i — Secondary targeting v2
- §2.3.4 step 4 exactly.
- **Gate:** `uv run pytest tests/test_phase1b_targeting.py -q` on `tests/fixtures/deep/`: `overlap-allowed`
  (density 1 → Shazam requests > 0 and ≥ 1 window coincides with an AudD window; density 2 likewise),
  `hint-only-first`, `overflow` (30 candidates, C=12, R=3 → 9 first-round picks in priority order),
  `largest-remainder-ties`, `replacement-after-duplicate`, `reserve-confirmation`, `duration-scaling` (120-min
  → C=240), `secondary-80pct` → `degraded`.

### 1b-ii — Corroboration and crowd rows
E-C2, E-C3 (trial source), E-M1, E-M3, E-M4, E-M6, E-L5, U-F13.
- **Gate:** `uv run pytest tests/test_phase1b_fusion.py -q` on `c3-scenario` (7 + 7 → keeps `likely`,
  corroborated with ≥ 6 s overlap), `single-coincidence` (no bypass), `two-separated` (bypass),
  `crowd-contradiction`; golden green.

### 1b-iii — Per-process breaker and corpus scorer
- Breaker (§2.3.5 a/b/c, per process); `scripts/score_corpus.py --run-list <json> --out <json>` where the
  run list is `{"recipe": "deep", "runs": [{"mix_id": "...", "truth": "<path>", "episodes": "<path>",
  "min_track_ms": 30000}]}`; the script pre-filters episodes with `present.exports.hidden_reason` and calls the
  existing `idea benchmark score --truth --episodes --out` per mix, aggregating: `likely_precision =
  empirical_tier_precision_e4["likely"]`, `listed_precision = selective_precision_e4` (post-filter),
  `work_recall = work_recall_e4`, each as corpus-weighted means.
- **Gate:** `uv run pytest tests/test_phase1b_breaker_scorer.py -q` (rate-open cooldown 30 min; daily-budget
  open until UTC midnight; latch after 3 rate-opens; scorer on `tests/fixtures/corpus-mini/` run list produces
  the three aggregate fields).

### 2b — Retention and sidecar pruning
E-M8, E-S2.
- **Gate:** `uv run pytest tests/test_phase2b_retention.py -q` (`tone-3600s.wav` → after `gc --policy
  hosted` manifest total ≤ 25 MB; Deep upgrade after pruning re-derives windows from PCM; after PCM expiry
  re-fetch → `source_changed` when altered; every terminal state's cleanup rule); `tests/test_stage4d_profiles.py`
  + golden green.

### 3a-i — Canonical projection
- **Gate:** `uv run pytest tests/test_projection.py -q` on `tests/fixtures/present/{garage,boiler,crowd}.json`;
  `PAGE_VERSION` bump.

### 3a-ii — Honesty, accessibility, mobile
- **Gate:** `uv run pytest tests/test_phase3a_honesty.py -q` (banned strings; table semantics; acquire chips
  present ≤ 720 px via CSS assertions); `uv run python scripts/check_page_js.py`; owner
  `scripts/screenshot_pages.ps1` with vendored `axe-core@4.10.2` → `docs/screenshots/v2/axe.json`, 0 serious.

### 4a-i — Service API and packaging
- `id_detector/service.py` (§4.3) used by the CLI; `idea_web` added to the wheel; `PROJECT_ROOT` fix.
- **Gate:** `uv run pytest tests/test_service_api.py -q` (resume from checkpoint `primary` → 0 AudD requests);
  `uv build` then `uv run python -c "import zipfile,glob;z=zipfile.ZipFile(glob.glob('dist/*.whl')[0]);
  assert any(n.startswith('idea_web/') for n in z.namelist())"`.

### 4a-ii — FastAPI parity (loopback)
- **Gate:** `uv run pytest tests/idea_web/test_parity.py -q`; `pwsh scripts/smoke_serve.ps1`; owner
  `scripts/gate_local_mode.ps1`.

### 4a-iii — Server-side UI fixes, static assets, headers
U-F1, U-F15, U-F18, U-F29, U-F30, U-F34.
- **Gate:** `uv run pytest tests/idea_web/test_headers_forms.py -q` (CSP/security headers; bad-URL keeps
  fields; garage page ≤ 60 KB gzipped).

### 4b-i — Durable queue, intake, worker
- `jobs` with `intake → waiting|analysis`; worker; checkpoints; attempts; dead-letter; coalescing by
  `analysis_key` via `run_subscribers`; cancel/drain per §3.5; attempt events in the DB.
- **Gate:** `uv run pytest tests/idea_web/test_worker.py -q` (kill after `primary` → resume at `hints` with 0
  AudD requests; two users, same key → one run, two subscribers; initiator detaches → settling reservation
  transfers; last detach → `cancelled` with in-flight attempts settled; dead letter after 3; private scope never
  coalesces).

### 4b-ii — Progress, operations, shared breaker
- Progress rows; wall-clock progress (U-F9); structured logs + redaction; spend metrics; disk alarm;
  `/healthz`; shared breaker over `provider_attempt_events`.
- **Gate:** `uv run pytest tests/idea_web/test_ops.py -q` (rate-open → new free jobs `waiting`, running deep
  → `degraded`, running free continues; daily budget → open until UTC midnight; latch).

### 4b-iii — Backups and restore
- `idea backup` / `verify-artefacts` (§4.6); `scripts/restore_drill.sh`.
- **Gate:** `uv run pytest tests/idea_web/test_backup.py -q` (backup taken while a bundle commit and a GC run
  are in progress yields a snapshot whose listed artefacts all exist and verify; restore drill from
  `tests/fixtures/snapshot/` passes).

### 4c-i — Users, sessions, passwords, CSRF
- **Gate:** `uv run pytest tests/idea_web/test_auth.py -q` (fixation, rotation, expiry, CSRF/Origin, limits).

### 4c-ii — Admin-created accounts, invites, admin reset
- **Gate:** `uv run pytest tests/idea_web/test_accounts.py -q` (invite required; admin-created account is
  pre-verified; admin reset link works once; XSS via titles/comments escaped).

### 4d-i — Tenancy schema, lots, reservations (worker-side)
- §4.5 tables; §3.5 lots/allocations/events; reservation in the worker after intake; submit pre-check.
- **Gate:** `uv run pytest tests/idea_web/test_credits.py -q` (20 concurrent 60-min intakes vs 150 min →
  exactly 2 reserved, the rest `quota_exceeded` with bytes kept; soonest-expiring lot first; settlement per
  status; expired plan-lot remainder forfeited, pack remainder returned).

### 4d-ii — Compatible-result serving, Deep-on-Free delta, caps, sources
- **Gate:** `uv run pytest tests/idea_web/test_cache_serving.py -q` (free served by deep only with the flag;
  d=2 request not served by d=1's request… and vice-versa per table; YouTube link refused on free; 151-min mix
  → `too_long` after intake with minutes released; Deep-on-Free FakeShazam gets 0 requests;
  `cache_hits_per_day` enforced).

### 4d-iii — Library, usage meter, admin essentials
- **Gate:** `uv run pytest tests/idea_web/test_library_admin.py -q` (isolation; N+1th blocked with prompt while
  a compatible cache hit still opens; admin actions audited; `/admin` shows queue, spend, breaker, disk,
  grants, disable/delete).

### 4d-iv — USD ceilings, monthly lot scheduler
- **Gate:** `uv run pytest tests/idea_web/test_usd_scheduler.py -q` (account-month and global-day caps refuse
  reservations; scheduler idempotent across reruns and month-end anniversaries).

### 6a-i — Ingest policy and SSRF
- **Gate:** `uv run pytest tests/idea_web/test_ingest_policy.py -q` (private ranges, redirect-to-private, DNS
  rebinding stub; proxy env not inherited by Shazam/AudD clients; alias mismatch closes the row).

### 6a-iii — Container
- Dockerfile/compose (web, worker, Caddy), secrets file, migration `down` test.
- **Gate:** `docker compose -f compose.yml -f compose.gate.yml run gate` builds the image and runs the M1
  test suite inside it; `docker compose up` on a clean machine serves `/healthz` through Caddy with
  `IDEA_HOSTED_READY=gate`.

### 6a-iv — Hosted-ready gate, backups schedule
- `scripts/gate_hosted_ready.sh` runs (inside the gate container, `IDEA_HOSTED_READY=gate`, loopback) the
  4a–6a gate selectors plus an authorised SoundCloud end-to-end analysis, a 5-hour synthetic refusal, a
  restart-during-job recovery and the restore drill; on success writes `/data/hosted-ready.json`
  `{image_digest, gate_suite_sha, passed_at}`; production start validates it (§3.1). Litestream + nightly
  `idea backup` scheduled.
- **Gate:** the script exits 0 and the production compose starts; deleting the file makes it refuse to start
  (test).

### 6b — Private-beta checklist
- Policy pages; attribution slot; runbooks; canary; `docs/LAUNCH.md`; `scripts/gate_beta_config.py`.
- **Gate:** `uv run python scripts/gate_beta_config.py`; `docs/LAUNCH.md` complete except L1–L5.

### M2 (after the beta; before any public launch)
- **7a-i — Billing `local`** (simulate pages; gate `tests/idea_web/test_billing_local.py`).
- **7a-ii — Stripe sandbox** (`orders`, `invoices`, `order_credit_links`, `refunds`, webhooks, portal;
  gate: `uv run pytest -m live tests/test_stripe_sandbox.py` with `stripe listen` per `docs/billing-testing.md`;
  no real charge).
- **7b — Sharing and publication** (`/s/<token>`, OG, explicit publish, takedown/DMCA; tables added here).
- **7c — Uploads + YouTube egress + email provider + self-service signup controls** (6a-ii content; S2 result;
  disposable list; Turnstile; transactional email).
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
| **L1** | AudD production terms in writing (hosted/consumer use, caching, attribution, concurrency, per-clip rate, whether throttled responses bill, reconciliation of ambiguous requests). **Blocks any hosted third-party AudD use, beta included.** | Nathan |
| **L2** | Legal review of ToS/privacy/DMCA and ingestion posture | Nathan + solicitor |
| **L3** | **Release gate:** ≥ 5 owner-verified mixes, ≥ 3 DJs, ≥ 2 platforms, ≥ 4 h, two-pass truth → `data/corpus/release-1/`; `uv run idea analyse <url> --recipe free|deep` per mix; `uv run python scripts/score_corpus.py --run-list data/corpus/release-1/runs-<recipe>.json --out docs/accuracy/release-1-<recipe>.json`; thresholds `likely_precision ≥ 0.90`, `listed_precision ≥ 0.80`, `work_recall ≥ 0.75` (`deep`) / `≥ 0.70` (`free`); also sets `serve_free_from_deep`. | Nathan (+ 1b-iii tooling) |
| **L4** | Stripe business country, KYC, live keys, VAT | Nathan |
| **L5** | Domain, email provider, restore drill on the real host | Nathan + 4b-iii/6a-iv |
| **L6** | Beta ≥ 2 weeks invite-only with billing off before M2 7a-ii is enabled | — |

### 6.4 Risks

| # | Risk | L / I | Mitigation |
|---|---|---|---|
| R1 | Shazam blocks the server IP | medium / high | breaker + latch; kill-switch; Deep unaffected |
| R2 | YouTube blocks datacenter fetches | high / medium | paid-only (M2); residential egress; S2 |
| R3 | Only walk-up AudD rate | medium / medium | density 2; §3.3 table; USD ceilings |
| R4 | Weak willingness to pay; Beatport | high / medium | tiny fixed costs; beta first; pro hedge |
| R5 | Frozen-profile byte test | certain / low | recipes are data; golden semantic |
| R6 | Cycle too big | medium / low | 35 cycles ≤ ~1 day each |
| R7 | Disk fills | certain w/o 2b / high | retention for all terminal states; manifest gate; alarm |
| R8 | Paid run fails after spend | medium / medium | admission + attempt states; settlement rules |
| R9 | Free-tier abuse | medium / medium | lots; invites; per-IP limits; budgets; cache-hit ledger |
| R10 | AudD terms unknown | certain / high | L1 blocks hosted use |
| R11 | Throttled Shazam thin runs | certain today / high | E-S1; `partial`; breaker |
| R12 | Free overwrites Deep | certain today / medium | bundles + `serves()` |
| R13 | Restart double-bills AudD | medium / medium | `dispatched` before I/O; bounded by in-flight concurrency; L1 reconciliation |
| R14 | Inconsistent backups | medium / medium | online backup API + artefact lock + verify |
| R15 | Mutable source URL | medium / low | alias revalidation; `source_changed` |

---

## Appendix A — register

| Item | Where |
|---|---|
| E-C1, E-C4, E-H2, E-H3, E-S4, E-S5 (fakes) | 0a-i |
| E-H1, E-S3, money, statuses | 0a-ii |
| E-H8, E-H9, E-M9, E-L4, U-F5 (loopback), `tl1001` | 0a-iii |
| E-C3 anchor, E-H4, E-H5 | 0b-i |
| E-H6, E-M2, E-M10, E-S1 | 0b-ii |
| bundles, refresh, audio route, E-M7 | 1a-i |
| `analysis_key`, `serves()`, Deep-on-Free, `source_changed`, E-M6 | 1a-ii |
| targeting (E-C2 fix) | 1b-i |
| E-C3 trial source, E-M1, E-M3, E-M4, E-L5, U-F13 | 1b-ii |
| breaker, L3 scorer | 1b-iii |
| E-M8, E-S2 | 2b |
| U-F2/F3/F14 | 3a-i |
| U-F4, F7, F8, F10, F11, F12, F19, F20, F31 | 3a-ii |
| E-L2, service API, packaging | 4a-i |
| parity, U-F6 | 4a-ii |
| U-F1, F15, F18, F29, F30, F34 | 4a-iii |
| E-H7 queue, intake, R13 | 4b-i |
| U-F9, U-F33, shared breaker | 4b-ii |
| R14 | 4b-iii |
| auth | 4c-i / 4c-ii |
| lots, reservations | 4d-i |
| compatibility serving, caps, D7 | 4d-ii |
| library, admin | 4d-iii |
| USD ceilings, scheduler | 4d-iv |
| SSRF, egress, aliases | 6a-i |
| container | 6a-iii |
| hosted-ready, backups schedule | 6a-iv |
| policy, beta config | 6b |
| billing local/Stripe, sharing, uploads/YouTube/email/signup controls, polish, email/admin extras, deletions + E-L1 | M2 7a–7f |
| **Round-4 required changes** | 1 → §2.3.4 step 4 + 1b-i `overlap-allowed` · 2 → §2.3.5 matrix + 0a-ii tests · 3 → §2.3.2 admission + outcome costs + 0b-i test · 4 → §2.3.4 step 1, §3.5, 4b-i/4d-i · 5 → `analysis_key` scope, `serves()` scope rule · 6 → `smoke_serve.*`, §3.1 readiness file, 6a-iv · 7 → `algorithm_version`, `serve_free_from_deep` · 8 → §3.5 order/expiry/transfer/zero-subscriber · 9 → `provider_attempt_events`, admin lots, `refunds` (M2) · 10 → online backup API + artefact lock + manifests · 11 → §3.3 COGS + provisional row + caps + `cache_hits` · 12 → 35 cycles + exact assertions · 13 → L3 run-list + field mapping · 14 → §2.3.5 breaker mechanisms · 15 → E-S5 fakes + citations |
| Round-1 P0 #2 | declined — D1 |

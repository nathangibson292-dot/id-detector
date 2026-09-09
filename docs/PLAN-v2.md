# IDea v2 — from a local tool to a product people pay for

*Plan rev 4 · 2026-09-09 · from four independent adversarial reviews (engines/fusion, UI/product, market,
hosting/payments) against `main` @ `27c36fd`; revised after Codex plan-review rounds 1–3
(`docs/reviews/plan-v2-review-round-{1,2,3}.md`) and the owner's decisions (§6.1). Appendix A maps every
finding and every round-3 required change to where it is resolved.*

---

## 0. The verdicts, in plain English

| Question Nathan asked | Verdict |
|---|---|
| **Is the business model right** (free capped weekly, paid capped monthly)? | **Yes, with three changes.** (1) Meter **minutes of audio**. (2) The paid allowance is **Deep-scan minutes**; every new Pro mix is a Deep scan. (3) A mix already analysed is served free and uncounted when the stored result is *compatible* with the request (§3.4); a Deep request on a Free result pays for the missing AudD sweep only. Subscription and non-expiring packs both. §3. |
| **Could we charge for it?** | **Not today; yes after Phases 0–1.** The paid tier crashes after spending whenever AudD returns any parseable result (E-C1); with an exhausted key it silently runs the free engine and caches the error bodies (E-H3, E-C4); no spend cap (E-H1); and as built it throws away the benefit it buys (E-C2/E-C3). |
| **Is it the best implementation?** | **No.** Pipeline core sound and tested; paid sequencing broken in four specific ways (§2.3); web layer not hostable (§2.2). |
| **Is the engine order right?** | **Paid engine first, then Shazam — yes (D2), but not as built.** Shazam must sample the uncertain spans as well as the blanks; AudD must carry a validated time anchor and its own vote; spend must be reserved and capped; the run must not crash. §2.3. |
| **Does the UI work / show useless data?** | Keep the look. Fix the numbers and words: card vs page counts disagree, downloads can disagree with the page, the page advertises its own noise, two columns are constant across 305 tracks, buy links vanish on phones, a bad URL lands on raw JSON, the privacy footer is false. §2.5. |
| **What should be removed?** | Whole-file paid scan + consent gate, ACRCloud, 1001tracklists connector, rescan button/route, version/role page columns, hidden-matches reveal, M3U, duplicate `/new` form, dead POST params (disabled before the beta; physically deleted after — §5 M2). Quarantined: rescans, calibration ML, benchmark, truth. **Kept and fixed: Panako** (D3). §2.6. |
| **Does it fill a genuine gap?** | **Partly — and not where we assumed.** Consumer "paste a link" is a commodity (21 tools, ~$1/mix, falling traffic, weak willingness to pay). Nobody ships work-vs-recording confidence or an evidence trail — the pro/report buyer's needs (D6), which need certified tiers, which need the real-mix corpus. §2.7. |

**Shape:** spikes/owner gates (S) in parallel; **Milestone M1 = 26 build cycles** to an invite-only private
beta with billing off (Phases 0–6); **Milestone M2 = 5 cycles** after the beta (Stripe sandbox, sharing,
polish, deletions, signup controls). Every cycle ≤ ~1 day, one commit, a named fixture and an exact command.

---

## 1. What was reviewed and how

| Review | Scope | Method |
|---|---|---|
| **E — engines** (`docs/reviews/v2-review-engines.md`) | `cli._analyse`, `paid_clip`, `scan_targeting`, `fuse/*`, `providers/*`, jobs, disk, tests | code + `dis` + `du` + test timing |
| **U — UI** (`docs/reviews/v2-review-ui.md`; `docs/screenshots/v2-before/`) | every screen | live server, headless-Edge screenshots, `node --check`, data audit of 305 tracks |
| **M — market** (`docs/research/05-market-2026-09.md`) | 21 competitors, metering, engine terms, demand | web research, cited |
| **H — hosting** (`docs/reviews/v2-review-hosting.md`) | yt-dlp/Shazam from a server, AudD, Stripe, framework, sizing | web research + code |
| **Codex rounds 1–3** | this plan | independent code verification |

Offline suite: **581 passed in 44 s** (93 deselected `slow`/`live`); the full-pipeline modules never run by
default — which is how E-C1 shipped.

---

## 2. Findings that decide the plan

### 2.1 The paid tier does not work (Critical; re-verified by Codex in every round)

| ID | Finding | Evidence |
|---|---|---|
| **E-C1** | **Paid-first crashes after presentation whenever any parseable AudD observation exists** (incl. no-match): `matches`/`recognised` read at `cli.py:794-815`, bound only in the free branch (`:507-521`). Unavailable/all-error AudD falls back to free instead (`:499-506`, E-H3). Reached via the web runner (`runner.py:137-167`). | `dis` → `LOAD_FAST_CHECK` |
| **E-C2** | Paid-first sends the free engine only to the complement of non-suppressed AudD episodes (`scan_targeting.py:61-69,127-146`; `cli.py:659-682`); `_engine_corroborated` checks no temporal overlap (`episodes.py:148-156`). | code |
| **E-C3** | AudD clip observations: `anchor=None`, no own trial source (`audd.py:344-365`) → shared `primary` group (`alignment.py:137-150`); zero-skew AudD votes can win; anchorless winners make no alignment point (`:115-124`) → `likely` unreachable + spurious `hypothesis_rejected`. `timecode` is parsed into an anchor by the enterprise parser (`audd.py:242-250`) — **which accepts negative values** (round 3). | code |
| **E-C4** | AudD error bodies cached before parsing (`paid_clip.py:182-190`), trusted on read (`:175-181`). | code |
| **E-H1** | No spend cap or record; `usd_e2` journaled `0` (`cli.py:820/850/864`). | code |
| **E-H2** | Gap-fill counts overwritten (`cli.py:649-655,676`). | code |
| **E-H3** | Silent paid→free downgrade. | `cli.py:499-506` |
| **E-H4** | Paid pass uncancellable; ETA "1 s". | `paid_clip.py:161-197`, `jobs.py:211-221` |
| **E-H5** | Paid pass sequential/unretried; timeout → re-billed next run. | `paid_clip.py` |
| **E-H6** | Runtime settings dropped under a profile (`cli.py:985-1007`, `runner.py:48-72`); `config show` prints file config (`cli.py:303-318`). | code |
| **E-H8** | `upload_consent`/`build_index` accepted from POST bodies; `upload_consent=true` runs the whole-file scan too. | `server.py:1373-1406` |
| **E-H9** | Web "build index" never queried (`local_index_label` never passed). | `runner.py:130-172` |
| **E-M1** | Corroboration counts provider strings; `COMMERCIAL_PROVIDERS` is a discount set. | `episodes.py:91-108,148-156` |
| **E-M2** | Novelty (~0.7 GB RAM/hour) on every fuse, unused with rescans off. | `orchestrate.py:140` |
| **E-M3** | `select_gap_targets` uses any coverage. | `scan_targeting.py:60-68` |
| **E-M4** | Crowd rows hash-ordered; contradictions both listed. | `episodes.py:790-845` |
| **E-M8** | ~330–370 MB/hour on disk; 10–25 MB durable. | measured |
| **E-L1** | 2.1 s import tax. | measured |
| **E-S1** | Malformed Shazam bodies under throttle → `ShazamHTTPError` (`shazam.py:200-204`) → failure observations (`recognise.py:307-340,521-553`), poorly surfaced. | code |
| **E-S2** | Completion sidecars verify every upstream file exists and hashes (`io.py:148-176`); the gen-N fuse sidecar lists all window files + PCM (`episodes.py:1136-1151`). | code |
| **E-S3** | Both frozen profiles have `budget.max_usd_e2 = 0`; `profile_app_config` drops it. | `profiles/*.json` |
| **E-S4** (new) | `paid_clip.py` imports `PaidScanResult` from `scan.py` — the contract must move before `scan.py` is deleted. `httpx` clients inherit `HTTP(S)_PROXY` unless `trust_env=False`. | code |

### 2.2 The web layer cannot be hosted as-is (verified)

One in-memory worker thread; restart loses jobs. No users/sessions/CSRF — any website the owner visits can
`POST /analyse` to `127.0.0.1:8765` (U-F5); `validate_target` accepts `file://` and any path (`jobs.py:59-84`)
and the server serves it back. Loopback-only by design; HTTP/1.0; 178 KB pages (U-F29). `ProcessLock` raises
rather than coalesces (E-H7); two URLs converging on one media dir collide (`cli.py:412-422`). Pages emit
`<audio src="../ingest/original.*">` (`page.py:1168-1172`; U-F6). Stale-page GET rewrites `index.html` but
not exports (U-F14/F3). `fuse/episodes.json` and `present/tracklist.*` are mutable singletons — a later Free run
overwrites a Deep result. `_load_cached` hashes the retained original (`ingest.py:143-164`). The wheel packages
only `src/id_detector` (`pyproject.toml:35-36`).

### 2.3 Engine sequencing — Deep scan v2

**Owner decision D2:** AudD sweeps first (no per-IP throttle), Shazam second. Free Shazam is throttled per IP
(the owner's live 2-hour run: 789 windows at 12.5 req/min ≈ 63 min).

#### 2.3.1 Recipes (frozen data in `src/id_detector/recipes.py`; `recipe_id = sha256(canonical JSON)`)

| Field | `free` | `deep` |
|---|---|---|
| `primary_engine` | shazam | audd |
| `primary_density` | 1 | 1 (config `deep.primary_density=2` selects **even-indexed windows** of the frozen 12 s / 9 s set; density is part of `recipe_id`) |
| `secondary_engine` | — | shazam |
| `secondary_clips_per_minute` | — | 2 → cap `C = ⌈duration_min × 2⌉` |
| `secondary_reserve_fraction` | — | 0.25 → `R = ⌊0.25 × C⌋` |
| `secondary_priority` | — | `hint_only → listed_not_confident → suppressed_challengeable → blank` |
| `overlap_min_ms` / `separation_min_ms` | — | 6000 / 60000 |
| `audd_concurrency` | — | 4 |
| `retry_policy` | shazam: existing | audd: **pre-dispatch failures and HTTP 429/503 only**, up to 3, backoff 1/2/4 s |
| `max_usd_e2` (recipe hard cap per run) | **0 = paid calls forbidden** | **900** ($9.00 — covers 240 min at density 1: 1,600 clips × 0.5 ¢ × 1.05 = $8.40) |
| `adapter_versions` | shazam:1 | shazam:1, audd_clip:2 |
| `requires` | `shazam_sweep`, `hints` | `audd_sweep`, `hints`, `shazam_secondary` |

`presentation_version` is **not** in the recipe. Frozen profiles are untouched; their `budget.max_usd_e2`
documents the retired file-scanner budget and is never read by the clip path (E-S3). `max_paid_clips` is
retired: a Deep run is never truncated — if the reservation exceeds the effective cap it ends
`budget_exhausted` **before** any network call.

#### 2.3.2 Money: reservation and caps

- Price input: `AppConfig.audd_usd_e6_per_request` (default 5000 = $0.005), recorded per run.
- **Reserve only billable AudD attempts:** `planned = ⌈windows / density⌉`;
  `usd_e2_reserved = ⌈planned × 1.05 × price_e6 / 10⁴⌉` (5 % headroom for retries that may bill). Shazam
  probes reserve nothing.
- **Effective cap = min(recipe.max_usd_e2, AppConfig.max_usd_e2 if set, account_month_remaining,
  global_day_remaining)**; the last two exist only in hosted mode. Reservation > effective cap →
  `budget_exhausted`.
- Settlement = Σ resolved attempt costs (`usd_e6`, ceiling to cents once per run); the difference to the
  reservation is released.

#### 2.3.3 Attempt state machine (all providers; drives idempotency, spend and the breaker)

`prepared` (journal row written with `attempt_id`, before any I/O) → `dispatched` (request bytes sent) →
`resolved(match | no_match | http_429 | http_5xx | malformed | connect_error | timeout)`.
- Retryable: `connect_error` (never dispatched), `http_429`, `http_503`.
- **Spent and never auto-retried:** `timeout` after `dispatched`, `http_5xx` other than 503, `malformed`
  (recorded as `ambiguous`, `usd_e6 = price`).
- Resume after a crash: `prepared`-not-`dispatched` → re-issue; `dispatched`-unresolved → `ambiguous`.
  This is at-least-once with bounded double-billing (≤ in-flight concurrency), stated honestly; exactly-once
  needs a provider reconciliation API (L1 question).
- Cache: only `match`/`no_match` are written to the response cache (`recognise/invocations/<adapter>-v<N>/raw/`);
  `--refresh-states no_match,http_429` (default) never re-bills `match`.

#### 2.3.4 Steps

1. **Resolve the analysis** — `analysis_key = sha256(media_key, recipe_id, hints_snapshot_id,
   manual_tracklist_sha256 | "", panako_index_id | "")`; look up a compatible stored result (§3.4); reserve
   only the missing work (§2.3.2).
2. **Primary sweep (AudD)** at the density, `audd_concurrency` workers, token bucket, retry policy, progress
   per clip (cancellable). Each observation: `simultaneous_source="audd"`; **anchor from `timecode` only if
   present, numeric, `0 ≤ timecode_ms ≤ 24 h`, and (when the match carries a duration) `≤ duration + 12 s`;
   otherwise `anchor=None` and the observation still votes without alignment**. `audd_sweep` achieved iff
   resolved (`match|no_match`) ≥ 95 % of `planned`; else the run is `partial`.
3. **Hints → first fuse.**
4. **Secondary (Shazam) targets** — candidates in priority order, each with span `[s, e]`:
   `hint_only` rows; listed episodes with tier < likely and not corroborated; suppressed candidates with ≥ 2
   votes; blank spans ≥ 12 s. Eligible windows for a span = frozen windows whose intersection with `[s, e]`
   ≥ 4 s. Allocation with `A = C − R` windows: (i) one window per candidate in priority order, ties by longer
   span then earlier `s`, until `A` is exhausted — lower-priority candidates beyond that get none; (ii) the
   remaining `A − k` windows by largest-remainder proportional to span duration, ties by earlier `s`;
   (iii) within a span rank by intersection length desc, then RMS energy desc, then start asc; a pick
   overlapping an already-picked window (any engine, this run) is skipped and **the next-ranked window in the
   same span takes its place**; a span with no remaining eligible windows returns its quota to (ii).
   Reserve `R`: when a secondary probe returns an identity inside a blank at `[m₀, m₁]`, queue the two
   eligible windows with starts in `[m₀ − 45 s, m₁ + 45 s]`, excluding the probe, that are farthest apart and
   ≥ 30 s apart (if only one exists, one); when `R` is exhausted, stop confirming. Unused `R` at the end is
   distributed by (ii). `shazam_secondary` achieved iff resolved ≥ 80 % of allocated; else `degraded`.
5. **Re-fuse once.** Corroboration = **selected votes** for the same normalised work with supports overlapping
   ≥ `overlap_min_ms`, from different families — `catalogue` {audd, acrcloud}, `shazam`, `local_index`
   {panako}; crowd hints stay hint corroboration. One cross-family agreement → "confirmed twice" displayed;
   bypassing suppression/the 30 s floor needs two agreements ≥ `separation_min_ms` apart **or** the existing
   `likely` rule (T_ind ≥ 4, on-air ≥ 30 s).
6. **Status** (exit codes for the CLI; credit rules in §3.5):

| Status | Meaning | Exit | Stored as | Served to later requests |
|---|---|---|---|---|
| `complete` | all `requires` achieved | 0 | result | yes (§3.4) |
| `degraded` | a non-primary requirement missing (Shazam breaker open) | 0 | result | only with `accept_degraded` (CLI flag; hosted: never) |
| `partial` | primary < 95 % resolved | 0 | result | never |
| `provider_unavailable` | paid engine unusable (auth/quota/network before any spend) | 3 | none | — |
| `budget_exhausted` | reservation > effective cap | 4 | none | — |
| `source_changed` | re-fetched bytes ≠ `media_key` | 5 | none | — |
| `failed` / `cancelled` | exception / cancel | 1 / 130 | none | — |

**Breaker (Shazam):** per-process in M1 Phase 1b-ii (`shazam_daily_budget_per_egress`, rolling 5-minute
failure rate > 30 % → open); service-wide over `provider_attempts` from 4b-ii; opens → new free jobs wait,
running secondary skipped → `degraded`; closes after 30 min cool-down; **latches off after 3 opens in a UTC
day until an admin re-enables**; manual `IDEA_ENGINE_SHAZAM=off`. Never a mid-run engine swap; never proxied.

#### 2.3.5 Cost per 60-minute mix (400 windows; AudD $5/1,000 walk-up · $2/1,000 subscription)

| Recipe | AudD req | Shazam req | AudD $ | Wall-clock (after 0b) |
|---|---|---|---|---|
| `free` | 0 | 400 | $0 | 9–60 min |
| `deep` d=1 | 400 | ≤ 120 | $2.00 · $0.80 | ~4 + 3–10 min |
| `deep` d=2 | 200 | ≤ 120 | $1.00 · $0.40 | ~2 + 3–10 min |

ACRCloud removed (same family as AudD). Panako kept (D3) as `local_index`; its `panako_index_id` (index label +
content hash) is part of `analysis_key`.

### 2.4 Accuracy: where the real edge is

Shazam alone caps at ~27/30 on the owner's benchmark; the misses are in no commercial catalogue. Levers: crowd
IDs; a DJ's own indexed uploads. Cross-family agreement is the strongest precision signal. Riding along:
comment text rendered as a track (U-F13); contradictory crowd answers both listed (E-M4). **No accuracy number
is advertised before L3.**

### 2.5 UI: remove / change / add (review U) — unchanged from rev 3 except where noted

**Remove:** hidden-matches counter+toggle (U-F7); `Version`/`Role` page+Markdown columns (U-F8; kept in JSON;
shown only when verified/contested); rescan button/column/route (U-F11); `HINT` pill; `layer`/`outgoing`; `ID
gap` tile; "9h 03m listened"; M3U; duplicate `/new`; false footer (U-F12); `no_evidence`/"suppressed" strings;
`upload_consent` param.
**Change:** bad URL → inline error (U-F1); **one typed entry list** feeds page, Copy, CUE, Markdown, JSON, card
(U-F2/F3/F14); mobile keeps "Where to get it" (U-F4); `purchase_url` before search (U-F10); wall-clock
progress (U-F9); failure copy + credit statement (U-F15); one confidence word + glossary; legend 6 → 2
(U-F16/17); contrast + table semantics (U-F19/20); "Free scan"/"Deep scan" chip; three tiles (U-F21);
alternatives only for a different work (U-F25); lead-in behind details (U-F26); options summary (U-F28);
in-progress cards (U-F18); no auto-redirect (U-F30); crowd rows honest (U-F13); one privacy line; run status in
the header. **Local-mode audio URL becomes a server route (`/media/<media_key>/audio`), not `../ingest/…`,
because pages move into bundle directories (§3.4).**
**Add:** analysed date; per-user library with delete; "you can close this tab"; failed runs visible (U-F33);
share links (M2).
**Keep:** theme, logo, scanner strip, cards, `prefers-reduced-motion`, NOW pill, CUE/Markdown/JSON, paste box.

### 2.6 Remove / quarantine / keep

| Subsystem | Decision |
|---|---|
| Whole-file `scan.py` path, `run_paid_scanners`, consent gate, `upload_consent` | 0a-ii: unreachable + refused; **`PaidScanResult` moved to `paid_clip.py` in 0a-i**; M2 7c: delete (tag `pre-v2-removals`) |
| ACRCloud adapter; `--engine acrcloud` | 0a-ii: refused "not supported"; M2 7c: delete |
| `tl1001` connector | 2a: default-disabled; M2 7c: delete |
| **Panako** + web `build_index` | **keep; 0a-ii wires `local_index_label`**; not in the hosted image until needed |
| Rescans + transform grid | quarantine |
| `calibrate/`, `benchmark/`, `truth.py`, `local_fixture.py`, `pointer_import` | quarantine: lazy import + `dev` extra (2a) |
| `novelty.py` | guarded + hoisted (0b-ii) |
| `sc_comments`, `manual`, `mixesdb`, `yt_comments`, `mixcloud` | keep (L2); hard timeouts hosted |
| CUE / Markdown / JSON | keep; M3U removed |

### 2.7 Market (review M; unchanged from rev 3)

21 hosted competitors at $0.99–2.99/mix or €6–$11/month, traffic falling; set79/TrackRadar already read
comments and leave gaps blank; nobody ships work-vs-recording or an evidence trail; demand large (MixesDB
21.9 % of 366 K mixes untracklisted; #DJSET +39 % YoY), willingness to pay weak (Set2Tracks shut down: "people
love to find this for free but wouldn't pay"); Beatport Track ID (10 M users) bundles live ID. Lessons: minutes,
never cripple free results, non-expiring packs, refund on failure, cached catalogue as the SEO layer. AudD:
$5/1,000 walk-up, "$2/1,000" subscription (per-clip applicability unconfirmed — L1); trial licence =
evaluation-only + attribution; paid terms unreadable by fetchers. No legal server-side Shazam; no enforcement
found; competitors run it openly. Uniqueness → the pro/report buyer (D6); consumer angle → scene-specific.

---

## 3. Product definition v2

### 3.1 Two modes, one codebase

**Local mode** (`idea.cmd`): the browser workflow passes `scripts/gate_local_mode.ps1` at every boundary.
**Hosted mode** (`IDEA_MODE=hosted`): **the app refuses to start unless `IDEA_HOSTED_READY=1`**, which only
`scripts/gate_hosted_ready.sh` (6a-iii) writes after the 4a–6a gate suite passes inside the container —
regardless of bind address, so a reverse proxy cannot expose an unready app.

### 3.2 Scans

| | Free scan (`free`) | Deep scan (`deep`) |
|---|---|---|
| Engines (D1) | Shazam sweep + crowd hints | AudD sweep → hints → Shazam sampled → re-fuse |
| User sees | badges, "from comments", run status | + "confirmed twice", tighter boundaries, rescued short tracks when doubly confirmed |
| API cost / hour | $0 (Shazam capacity) | $0.80–2.00 (d=1) |
| Shazam throttled | `degraded`, failed-window count shown; breaker pauses new free scans | `degraded` (AudD-only) |

### 3.3 Tiers, caps, prices

Unit = **minutes of new audio**, rounded up. **Free allowance resets weekly** (Monday 00:00 UTC). **Pro
allowance is a monthly credit lot** (§3.5) created on the subscription's monthly anniversary (day-of-month
clamped to the month's length) by the idempotent scheduler `idea billing grant-monthly` (daily; key =
`(subscription_id, YYYY-MM)`) — annual plans thus receive 12 lots; unused Pro minutes expire when the next lot
is created; packs never expire; allocation order = soonest-expiring lot first (so plan before pack).

Configuration authority (single source): **`pricing.toml` in the repo root** (created in 0a-i), versioned by
`pricing_version`; it holds `audd_usd_e6_per_request`, tier allowances, prices, mix-length caps and
`free_sources`/`paid_sources`. `AppConfig.audd_usd_e6_per_request` is *loaded from it*; the recipe's
`max_usd_e2` is the per-run hard cap and pricing cannot raise it. Every reservation records `pricing_version`.

| | **Free** | **Pro** | **Packs** |
|---|---|---|---|
| Price (GBP incl. VAT) | £0, no card | £9 / month; £90 / year (12 monthly lots) | from £6 |
| Allowance | 150 min / week, `free` recipe, full results | **N Deep minutes / month** (N from the table) | Deep minutes, never expire |
| Sources | **SoundCloud, Mixcloud links** (owner decision, §6.1 D7) | + YouTube links, file uploads | as Pro |
| Mix length cap | 150 min | 240 min | 240 min |
| History | 30 days | unlimited | 90 days |
| Compatible cached result | free, uncounted, 20/day | same; Deep-on-Free pays the AudD sweep | same |

**N and pack sizes** — COGS per Deep hour = AudD (400 req) × 1.05 + YouTube proxy share (50 % of Pro mixes ×
$0.30) + compute/storage/bandwidth/backup $0.10 + **failure/outage allowance 3 % of gross**; £9 → $11.50
gross − VAT 20 % − Stripe (1.5 % + 20p + 0.7 %) ≈ **$8.90 net**; £6 → ≈ $5.75 net; target ≥ 50 % gross margin:

| AudD rate | Deep $/hour | Pro £9 → N | Pack £6 → min |
|---|---|---|---|
| walk-up, d=1 | $2.35 | ≈ 110 min | ≈ 70 min |
| walk-up, d=2 | $1.30 | ≈ 200 min | ≈ 130 min |
| subscription, d=1 | $1.09 | ≈ 245 min | ≈ 155 min |
| subscription, d=2 | $0.67 | ≈ 395 min | ≈ 255 min |

**No price is published and no third-party Deep scan runs before L1** (beta included). Free-tier cash cost is
the flat server plus allocated compute/bandwidth (≈ $0.03 per mix); Shazam is $0 but capacity-bounded.

### 3.4 Results, compatibility, coalescing

- **Result bundle** = `present/bundles/<bundle_id>/` (`bundle_id = sha256(run_id, presentation_version)`):
  page, exports, `manifest.json` (files, hashes, sizes, `run_id`, `analysis_key`, `requested_recipe_id`,
  `achieved`, `status`, `presentation_version`, `pricing_version`, `usd_e2_spent`); fuse artefacts in
  `fuse/runs/<run_id>/`. Bundles are immutable; a presentation refresh writes a new bundle for the same run.
  Local mode keeps `present/current` → the latest bundle of the **newest complete run** (single user); hosted
  mode has no global pointer (§4.5).
- **Compatibility relation `serves(stored, request)`** — explicit, versioned (`compat_version = 1`):

| Request | Served by a stored result iff |
|---|---|
| `free` | stored recipe ∈ {`free`, `deep`(any density)} ∧ status = `complete` ∧ stored adapter versions ≥ requested ∧ `analysis_key` inputs match (hints snapshot within `hints_max_age_days`, same manual tracklist hash, same panako index) |
| `deep` d=2 | stored recipe ∈ {`deep` d=2, `deep` d=1} ∧ status = `complete` ∧ same conditions |
| `deep` d=1 | stored recipe = `deep` d=1 ∧ status = `complete` ∧ same conditions |
| any | `degraded` only when the request sets `accept_degraded` (local CLI); `partial` never |

  **Deep-on-Free:** a `deep` request finding only a `free complete` result reuses its Shazam observations as
  the secondary evidence (a full sweep contains every window a sampled secondary could pick), runs the AudD
  sweep, re-fuses; reserve = primary only. **Free-on-Deep:** served as-is (a Deep result is strictly more
  evidence).
- **Coalescing key = `analysis_key`.** A request whose `analysis_key` has a non-terminal run **attaches** as a
  subscriber; results with a non-empty manual tracklist are `private_inputs = true` and are never served to or
  coalesced with other users. Provider raw responses (content-addressed by clip) are shared across all users.
- **Hints snapshot:** `hints_snapshot_id = sha256(hints.jsonl)`, refetched when older than `hints_max_age_days`
  (7) at request time.
- **Sidecars after pruning** (E-S2): retention moves deleted files' entries to `pruned_upstream` (same hashes);
  `verify_completion_sidecar` accepts pruned entries when the artefact hash matches; consumers needing a pruned
  file re-derive it (windows ← PCM ← original ← re-fetch); **a re-fetch must hash to `media_key` or the run
  ends `source_changed`**.
- **Source aliases:** `source_aliases(url, canonical_url, media_key, valid_from, valid_to, last_verified)`;
  an alias older than `alias_revalidate_days` (7) is revalidated at request time by `yt-dlp --skip-download`
  metadata (title, uploader id, duration ± 2 s); mismatch closes the row (`valid_to`) and the fetch creates a
  new media.

### 3.5 Credits: lots, allocations, settlement

- `credit_grants(id, account, kind ∈ {plan, pack, admin}, minutes_total, minutes_remaining, expires_at,
  source_ref)` — one row per lot.
- `credit_allocations(reservation_id, grant_id, minutes)` — a reservation allocates from the soonest-expiring
  lots first, inside one `BEGIN IMMEDIATE` transaction with the balance check and the job insert.
- `credit_events(id, account, kind ∈ {grant, reserve, settle, release, refund, expire, reverse}, minutes,
  reservation_id, grant_id, ref, created_at)` — append-only audit; balances are on `credit_grants`.
- **Settlement rules (minutes):** `complete` → 100 % of the reservation; `degraded` → 100 % (the paid sweep was
  delivered); `partial` → 100 % if ≥ 90 % of primary clips resolved, else 50 %, remainder released to the same
  lots; `provider_unavailable`, `budget_exhausted`, `source_changed`, `failed`, `cancelled` → 0 % (full release);
  **coalesced subscribers pay 0** (treated as cache hits; only the initiating reservation settles); a user who
  cancels their own subscription to a shared run releases their reservation — the run continues while other
  subscribers remain. Spent-but-released runs still count toward abuse ceilings (`usd_ledger` scope
  `account_month`).
- **USD:** `usd_reservations(id, run_id, account, usd_e2, pricing_version)` and `usd_events` (reserve/settle/
  release) at the run; `account_month` and `global_day` ceilings are computed as Σ over reservations + settled
  spend in the period (no duplicate rows). Attempt-level `usd_e6` lives on `provider_attempts`.
- Packs bought via Stripe are lots with `source_ref = order_id`; a refund/dispute reverses `min(remaining,
  refunded_minutes)` from that lot and records a `reverse` event (M2 5b).

### 3.6 Sign-in, payments, beta (decided)

Email + password (argon2-cffi); verification required; server-side sessions; CSRF + Origin. `BILLING_BACKEND=
off | local | stripe`; live needs `sk_live_` and `IDEA_BILLING_LIVE=1`. **Beta (M1 end):** `BILLING_BACKEND=off`
with audited admin grants; invite-only sign-up; one recipe per tier; `IDEA_SHARING=off`,
`IDEA_PUBLIC_CATALOGUE=off`; Panako not in the image. Stripe sandbox, sharing, self-service signup controls,
polish and deletions follow in M2 — built, per D5, before any real launch.

---

## 4. Hosted architecture

### 4.1 Showstopper checks

| Check | Finding | Consequence |
|---|---|---|
| Unofficial Shazam from a server | ~20 req/min per IP shared by all users → ~40–65 mixes/day per IP; failures past the throttle (E-S1); ASN blocks; no official server API | **D1 accepted.** Per-account weekly cap; `provider_attempts` per egress; daily budget + rolling failure-rate breaker with latch (§2.3.4); kill-switch; extra egress IPs as config rows; `trust_env=False` on the Shazam client (E-S4); never residential-proxied |
| yt-dlp from a server | SoundCloud/Mixcloud direct; YouTube needs residential egress (~65 MB/mix) | **D4/D7:** YouTube paid-only; the proxy is passed explicitly to yt-dlp for YouTube jobs only; S2 before 6a-i |
| AudD terms | production terms unreadable; trial = evaluation-only + attribution | **L1 blocks any hosted third-party AudD use, beta included** |
| Disk | 330–370 MB/hour; 10–25 MB durable | retention for every terminal state (§4.6) |
| RAM | novelty ~0.7 GB/hour | guarded off (0b-ii); 1.5 GB/job; length caps |

### 4.2 Shape

```
Internet ─:443─▶ Caddy (TLS, gzip, 250 MB cap, headers) ─▶ uvicorn · idea_web (FastAPI)   [starts only if IDEA_HOSTED_READY=1]
                                                            │ pages · auth/sessions · CSRF/Origin · credit + USD reservation
                                                            │ billing provider · POST /analyse → jobs row · status polling
                                                            │ result routes (authz, traversal-safe) over present/bundles/<bundle_id>/
                                                            ▼
                                                       app.db (SQLite WAL; Litestream) + fenced artefact snapshots
                                                            ▲ lease / heartbeat / checkpoints / progress / subscribers
                                                       idea_web.jobs.worker (separate process; N=1)
                                                            │ ingest policy (allow-list, SSRF guard, per-platform egress, upload ids)
                                                            │ id_detector.service.run(RunRequest) ← pipeline public API
                                                            │ retention GC (all terminal states)
                                                            ▼
                                                       work/<source>/<media>/ (content-addressed; durable subset snapshotted)
```

Rules: web never runs a pipeline; worker never serves HTTP; workers never receive user paths (`PlatformUrl |
UploadId | LocalPath(local only)`); billing = four methods; `src/id_detector/` changes after Phase 3 limited to
`service.py`, `recipes.py`, `serve` entry point, `PROJECT_ROOT`.

### 4.3 Web layer and service API

FastAPI + Jinja2 + uvicorn (S4 ADR: **Codex writes the ADR with a recommendation; the owner approves** before
4a-ii). `src/idea_web` packaged. `idea serve` = same app, local mode; `present/server.py` retired after parity.
Render policy `emit_local_audio` false when hosted. Polling 2.5 s.

`id_detector.service.run(RunRequest(run_id, analysis_key, target, recipe, hints_snapshot_policy,
manual_tracklist_path, checkpoint_store, attempt_journal, progress, cancel_token)) → RunResult(run_id, status,
achieved, bundle_id, usd_e2_reserved, usd_e2_spent, attempts)`. `run_id` is caller-supplied; `checkpoint_store`
persists phase completion (`ingest, decode, windows, primary, hints, fuse1, secondary, fuse2, present`) so a
resumed run skips completed phases; `attempt_journal` implements §2.3.3.

### 4.4 Accounts, sessions, abuse

argon2-cffi; server-side sessions (`__Host-idea_session; HttpOnly; Secure; SameSite=Lax`; `sha256(token)`;
rotation on login/plan change; 30 d idle / 90 d absolute); single-use hashed email tokens; verification
required; CSRF synchroniser + Origin; per-IP limits (signup 3/day, login 10/15 min, reset 3/h, analyse 5/h;
IPv6 /64; trusted proxy config); CSP/security headers; export routes 10/min. Email provider interface; `console`
backend refused when hosted. **M1 beta:** invite codes only; disposable-email list and Turnstile in M2.

### 4.5 Data model (SQLite WAL; numbered SQL migrations with `down` scripts)

`users` (id, email, pw_hash, verified_at, created_ip, invite_id, disabled_at, deleted_at) · `sessions` ·
`email_tokens` (hash, purpose, expires, used_at) · `invites` (code_hash, created_by, used_by, expires) ·
**`media`** (media_key, duration_ms, first_seen) · **`source_aliases`** (§3.4) · **`uploads`** (id, user,
staging_path, bytes, created_at, expires_at (24 h), media_key once hashed, deleted_at) ·
**`analysis_runs`** (run_id, analysis_key, media_key, requested_recipe_id, achieved JSON, status, private_inputs,
initiator_user, reservation_id, attempts, checkpoints JSON, usd_e2_reserved, usd_e2_spent, pricing_version,
started_at, finished_at) · **`result_bundles`** (bundle_id, run_id, presentation_version, path, manifest_sha256,
created_at) · **`run_subscribers`** (run_id, user, reservation_id, attached_at, detached_at) ·
**`library_items`** (id, user, run_id, retention_class ∈ {free30, pack90, pro∞}, added_at, deleted_at) — a
user sees only runs they initiated, subscribed to, or received as a compatible cache hit (a row is created at
that moment); the served bundle is the run's latest presentation bundle ·
**`publications`** / **`shares`** (bundle_id-scoped; M2) ·
`jobs` (id, run_id, target JSON, recipe_id, state, lease_owner, lease_until, heartbeat_at, attempt, max_attempts
= 3, dead_letter_reason, cancel_requested, progress JSON, log_path) ·
`credit_grants`, `credit_allocations`, `credit_events`, `usd_reservations`, `usd_events` (§3.5) ·
**`provider_attempts`** (attempt_id, run_id, provider, egress_id, state, http_status, outcome, usd_e6,
prepared_at, dispatched_at, resolved_at) — drives budgets, the rolling breaker (5-minute window query on
`resolved_at`), and reconciliation ·
`entitlements` (user, plan, status, anniversary_day, period_end, cancel flags, stripe ids) ·
`entitlement_grants` (admin comps) · **`orders`** (id, user, kind, stripe session/payment_intent, status) ·
**`invoices`** (stripe_invoice_id, subscription, period_start, period_end, status) · **`order_credit_links`**
(order_id | invoice_id, grant_id) · `processed_stripe_events` · `admin_audit`.

### 4.6 Jobs, retention, backups

Worker: lease + heartbeat 10 s, checkpoints, `attempt ≤ 3` then dead-letter (treated as `failed` for
cleanup/credits after 24 h), idempotent bundle commits, attempt journal (§2.3.3), cancel = subscriber detach,
drain = stop leasing. **Retention** (`idea gc`, policies `local|hosted`): on `complete|degraded|partial` —
delete `windows/**` now, PCM after 48 h, original after 7 d (hosted; local keeps it), rewriting sidecars; on
`failed|cancelled|provider_unavailable|budget_exhausted|source_changed|dead_letter` — delete `windows/**` + PCM
now, original after 24 h, the media dir after 7 d if no bundle exists; uploads staging expires after 24 h;
jobs whose lease is stale with no status → `failed` after `max_attempts`; history expiry removes
`library_items`; a bundle/run is deleted only when no library item, share or publication references it.
**Backups:** `idea backup` (nightly) = take the `backup` lock (pauses bundle commits ≤ 60 s) → `PRAGMA
wal_checkpoint(TRUNCATE)` → copy `app.db` → write `snapshot.json` listing every `bundle_id` and `run_id`
referenced by that DB copy → release the lock → copy those immutable directories + `recognise/`, `hints/`,
`ingest/source.json`; restore = DB → listed artefacts → `idea verify-artefacts` (manifest hashes). Server:
Hetzner CX/CPX (~€16/month); image `python:3.12-slim` + uv + ffmpeg + Deno + `yt-dlp[default,curl-cffi]`, no
JDK, no dev tooling; ffmpeg/ffprobe under `timeout` + memory limits; uploads streamed with byte/time/disk caps.

### 4.7 Billing

`BillingProvider` — `create_checkout_url(user, product, success, cancel)`, `portal_url(user, return)`,
`handle_webhook(raw_body, sig)`, `current_entitlement(user)`. `off`: admin grants. `local` (M1 5a): `/dev/billing/*`
simulate pages (success, decline, async-success, renewal, failed renewal, cancel, refund, dispute) writing the
same `entitlements`/`orders`/`invoices`/`credit_grants` rows; banner; refuses `ENV=production`. `stripe` (M2
5b): Checkout subscription (monthly/annual) and payment (packs); Portal; webhooks `checkout.session.completed`,
`checkout.session.async_payment_{succeeded,failed}`, `customer.subscription.{created,updated,deleted}`,
`invoice.paid` (creates the `invoices` row; the monthly lot is created by the scheduler), `invoice.payment_failed`,
`charge.refunded`, `charge.dispute.created` (reverse via `order_credit_links`); raw-body signature; `event_id`
dedupe; re-fetch on event; grant on `active|trialing`, revoke on `canceled|unpaid`, 7-day grace on `past_due`;
Stripe Tax; GBP VAT-inclusive. `docs/billing-testing.md` (5b) holds the exact CLI/test-clock sequence with
expected rows after each step.

### 4.8 Hosted boundary

Allow-listed platform hosts or upload ids; SSRF guard (private/reserved ranges, DNS rebinding, redirect
re-validation); untrusted filenames/MIME; result/export routes authorise by `library_items`/share/publication
with traversal/symlink checks; originals never served; no rescan route; results private; explicit publish (M2)
with takedown ownership; one privacy sentence everywhere.

---

## 5. Phases — M1 (26 cycles) then M2 (5 cycles)

Every cycle: Codex builds → `uv run pytest -q` · `uv run ruff check .` · `uv run ruff format --check .` ·
`uv run python scripts/audit_fixtures.py` green → Codex reviews the diff → fix → one commit. Test selectors
are exact (`uv run pytest tests/<file>.py -q`). Owner-only checks are `live`-marked or `.ps1`.

**Shared test infrastructure (built in 0a-i, reused everywhere):**
- `scripts/make_audio_fixtures.py` → committed `tests/fixtures/audio/tone-60s.wav` (60 s, 16 kHz mono s16,
  three tones with 2 s crossfades; 7 frozen windows) and on-demand `tone-3600s.wav`.
- **Fakes:** `tests/fakes/providers.py` with `FakeAudD` (implements `recognize_clip(bytes) -> dict` like the
  AudD clip adapter) and `FakeShazam` (implements the injected Shazam client's `recognize(bytes) -> dict`);
  both scripted by a JSON file: `{"audd": {"default": "match", "windows": {"3": "no_match", "5":
  "timeout_post"}}, "shazam": {...}}` with outcomes `match | no_match | http_429 | http_503 | http_500 |
  timeout_pre | timeout_post | malformed`; `match` payloads include a title/artist per tone and a plausible
  `timecode`. Committed scripts under `tests/fakes/scripts/`.
- **Injection:** `_analyse(..., paid_scan_adapters=..., shazam_client=...)` (new `shazam_client` threaded to
  `recognise_generation`); `idea analyse --fake-providers audd,shazam` (hidden) reads `IDEA_FAKE_SCRIPT` and is
  refused unless `IDEA_TEST_MODE=1`.
- `scripts/assert_journal.py --work-root <dir> --expect key=value…` reads the newest `invocations.jsonl`
  entry and exits non-zero on mismatch.

### Phase S — spikes and owner gates (parallel; owner tasks, not build cycles)
- **S1 (owner):** AudD production terms + per-clip rate → L1.
- **S2 (owner runs, Codex writes in 0a-ii):** `scripts/spike_ingest_vps.sh <soundcloud-url> <mixcloud-url>
  <youtube-url> [proxy-url]` — 3 runs per platform, direct and via proxy, `docs/spikes/ingest-vps.md` (platform,
  route, ok, bytes, seconds). Pass = SC + MC direct; YT via proxy. Blocks 6a-i.
- **S3 (owner runs, Codex writes in 0a-ii):** `scripts/spike_shazam_vps.py --minutes 60 --ceiling 45` using the
  fixture windows; records achieved req/min, first 429, decode errors; run daily × 5 → `docs/spikes/shazam-vps.md`
  → sets `shazam_daily_budget_per_egress`.
- **S4 (Codex writes, owner approves):** `docs/adr/0001-web-framework.md` before 4a-ii.

### 0a-i — Crash, cache states, reservation, recipes (minimal), fakes
E-C1, E-C4, E-H1, E-H2, E-H3, E-M5, E-S3, E-S4 (relocation).
- Bind `matches`; free failure count only when the free branch ran; `PaidScanResult` → `paid_clip.py`.
- `recipes.py` with the §2.3.1 table and `--recipe free|deep` on `analyse` (web runner in 1a); `pricing.toml`;
  `AppConfig.audd_usd_e6_per_request`, `max_usd_e2`; reservation/cap/settlement per §2.3.2 (local: recipe and
  AppConfig caps only); journal fields `usd_e2_reserved`, `usd_e2_spent`, `status`, `achieved`.
- Cache states per §2.3.3 (write only `match|no_match`; read rejects others; `--refresh-states`).
- Statuses `provider_unavailable`(3) / `budget_exhausted`(4) with `--allow-degrade` → `degraded`(0).
- Fixtures, fakes, injection, `assert_journal.py`.
- **Tests** `tests/test_phase0a_paid_path.py`: deep success; all-no-match; all `http_500` → exit 3;
  `--allow-degrade` → degraded; `max_usd_e2=0` refuses; reservation > cap → exit 4 with zero attempts; error
  bodies never cached; counts accumulate.
- **Gate (PowerShell):**
  ```powershell
  $env:IDEA_TEST_MODE = "1"; $env:IDEA_FAKE_SCRIPT = "tests/fakes/scripts/gate0a-deep.json"
  $w = "$env:TEMP\idea-gate0a"; Remove-Item -Recurse -Force $w -ErrorAction SilentlyContinue
  uv run idea analyse tests/fixtures/audio/tone-60s.wav --recipe deep --fake-providers audd,shazam --work-root $w
  uv run python scripts/assert_journal.py --work-root $w --expect status=complete usd_e2_reserved=4 usd_e2_spent=4
  ```
  (7 windows × 1.05 × 0.5 ¢ = 3.675 ¢ → reserved 4; 7 resolved × 0.5 ¢ = 3.5 ¢ → spent 4.)

### 0a-ii — Security and dead-path removal (local)
E-H8, E-H9, E-M9, E-L4, U-F5 (loopback).
- Drop `upload_consent`; `run_paid_scanners` call site removed; `--engine acrcloud` → "not supported";
  **`local_index_label` wired from the web runner (D3)**; `_windows_in_spans` skips transformed windows.
- Loopback CSRF: the server mints a per-process token exposed at `GET /csrf` (JSON) and embedded in forms;
  every `POST` must carry it (form field or `X-CSRF-Token`) **and** an `Origin`/`Host` of `127.0.0.1:<port>` or
  `localhost:<port>`; token lifetime = server lifetime.
- Spike scripts S2/S3 written (not run).
- **Gate:** `uv run pytest tests/test_phase0a_security.py -q` (CSRF/Origin reject, param removal, Panako label
  reaches `_analyse`, acrcloud refused).

### 0b-i — AudD adapter: anchor, concurrency, attempt states, cancellation
E-C3 (anchor), E-H4, E-H5.
- `audd_clip` adapter v2: anchor validity rule (§2.3.4 step 2), `simultaneous_source="audd"`; concurrency 4 +
  token bucket; retry policy; attempt journal `prepared/dispatched/resolved` (`recognise/attempts.jsonl`);
  progress per clip; `trust_env=False`.
- **Gate:** `uv run pytest tests/test_phase0b_audd.py -q`: cancel after 3 clips (fake 200 ms latency) → ≤ 4
  attempts; `http_429`×2 then match → 3 journal rows, 1 billed; `timeout_post` → ambiguous, spent, not retried;
  `timeout_pre` → retried; negative/absent `timecode` → `anchor=None`; resume with a `dispatched` row → ambiguous.

### 0b-ii — Effective config, novelty, degraded runs, golden
E-H6, E-M2, E-M10, E-S1.
- Config carry-over (both call sites); `idea config show [--profile X]` marks profile-sourced values; novelty
  guarded + hoisted; Shazam decode errors → limiter penalty + failures; `degraded` when failures > 20 %; true
  scanned window set to the fuser.
- **Golden:** `scripts/make_golden.py` → `tests/golden/local-free/tracklist.json` from `tone-60s.wav` +
  `tests/fakes/scripts/golden-free.json`; `tests/test_golden_local_free.py` compares entries minus provenance
  fields (`run_id`, timestamps, `generated_by`, `requested_recipe_id`, `analysis_key`, `presentation_version`).
- **Gate:** `uv run pytest tests/test_phase0b_config.py tests/test_golden_local_free.py -q`.

### 1a — Bundles, analysis keys, compatibility, local pointer
- `bundle_id`, bundle layout + manifest, `fuse/runs/<run_id>/`, `present/current` (local), `refresh.py` → new
  bundle, `analysis_key`, `serves()` table (`compat_version=1`), Deep-on-Free reuse, `source_changed`; web
  runner uses `--recipe`; page audio via `/media/<media_key>/audio` route (local); `_load_cached` via manifest.
- **Gate:** `uv run pytest tests/test_phase1a_bundles.py -q` (free→deep reserves primary only and FakeShazam
  gets 0 requests; deep d=1 serves d=2 and free requests; d=2 does not serve d=1; degraded/partial never served
  hosted; refresh creates a second bundle for the same run; `current` stays on the newest complete run) **and**
  `scripts/gate_local_mode.ps1` (owner: `idea.cmd` opens a cached mix from a bundle path and the audio route
  plays — asserted by the probe page writing `audio-ok` into the log).

### 1b-i — Secondary targeting v2
- §2.3.4 step 4 exactly (eligibility, allocation, ranking, replacement, reserve, achieved thresholds).
- **Gate:** `uv run pytest tests/test_phase1b_targeting.py -q` on `tests/fixtures/deep/` scripts:
  `hint-only-first`, `overflow` (30 candidates, C=12, R=3 → 9 first-round picks in priority order),
  `largest-remainder-ties`, `replacement-after-overlap`, `reserve-confirmation` (two windows ≥ 30 s apart),
  `duration-scaling` (120-min fake → C=240), `secondary-80pct` (→ degraded).

### 1b-ii — Corroboration, crowd rows, per-process breaker, corpus scorer
E-C2, E-C3 (trial source), E-M1, E-M3, E-M4, E-M6, E-L5, U-F13.
- Selected-vote overlap corroboration + families; display vs bypass thresholds; `discounted_providers` by
  family; crowd-row rules; per-process Shazam breaker + latch; `scripts/score_corpus.py --truth-dir <dir>
  --runs <run-list.json> --out <json>` wrapping the existing `idea benchmark score --truth --episodes --out` per
  mix and aggregating precision/recall per tier (the L3 command).
- **Gate:** `uv run pytest tests/test_phase1b_fusion.py -q` on `tests/fixtures/deep/c3-scenario.json` (7 + 7
  → keeps `likely`, corroborated), `single-coincidence` (no bypass), `two-separated` (bypass), `crowd-contradiction`;
  `tests/test_golden_local_free.py` still green; owner `live`: `uv run pytest -m live tests/test_live_deep_holly.py`.

### 2a — Startup and runtime cleanup
- Lazy imports; `dev` extra; `work/index.json`; `tl1001` default-disabled.
- **Gate:** `uv run python scripts/measure_startup.py` (3 cold runs of `python -X importtime -c "import
  id_detector.cli"`, prints median) < 1.5 s, value recorded in the commit; `tests/test_phase2a_cache.py`
  (cached-open with the original deleted).

### 2b — Retention and sidecar pruning
E-M8, E-S2.
- `pruned_upstream`; verifier; re-derivation chain with `source_changed`; retention for every terminal state
  (§4.6); `idea gc --policy local|hosted`; `--keep-intermediates`; manifest sizes.
- **Gate:** `uv run pytest tests/test_phase2b_retention.py -q` (generates `tone-3600s.wav`; after `gc
  --policy hosted` the manifest total ≤ 25 MB; Deep upgrade after pruning re-derives windows from PCM; after PCM
  expiry re-fetches from the local-file source and ends `source_changed` when the file is altered);
  `tests/test_stage4d_profiles.py`, golden green.

### 3a-i — Canonical projection and immutable presentation
- `present/projection.py`; page, Copy, CUE, Markdown, JSON, card from one typed list; exports inside bundles.
- **Gate:** `uv run pytest tests/test_projection.py -q` on `tests/fixtures/present/{garage,boiler,crowd}.json`
  (tracks and gaps compared separately across all outputs); `PAGE_VERSION` bump.

### 3a-ii — Honesty, accessibility, mobile
- §2.5 removals; version-column rule; crowd rows; contrast; table semantics; stacked cards; privacy line; run
  status; `scripts/check_page_js.py`.
- **Gate:** `uv run pytest tests/test_phase3a_honesty.py -q` (banned strings; no `role="button"` on `tr`;
  `scope="col"`; acquire chips present at 390 px in the rendered HTML via CSS class assertions);
  `uv run python scripts/check_page_js.py`; owner: `scripts/screenshot_pages.ps1` runs `axe-core@4.10.2`
  (pinned, vendored under `scripts/vendor/`) → `docs/screenshots/v2/axe.json` with 0 serious.

### 4a-i — Service API and packaging
- `id_detector/service.py` (§4.3) used by the CLI; `src/idea_web` added to the wheel; `PROJECT_ROOT` fix.
- **Gate:** `uv run pytest tests/test_service_api.py -q` (resume from checkpoint `primary` issues 0 AudD
  requests; `run_id` stable); `uv build` produces a wheel containing `idea_web/`.

### 4a-ii — FastAPI parity (loopback)
- Templates ported 1:1; routes parity; discriminated targets + SSRF-safe validation; `idea serve` → idea_web
  local mode; `present/server.py` retired.
- **Gate:** `uv run pytest tests/idea_web/test_parity.py -q` on the three present fixtures; `uv run idea serve
  --no-open --port 8791` smoke in CI; owner `scripts/gate_local_mode.ps1`.

### 4a-iii — Server-side UI fixes, static assets, headers
U-F1, U-F15, U-F18, U-F29, U-F30, U-F34.
- **Gate:** `uv run pytest tests/idea_web/test_headers_forms.py -q` (CSP + security headers; bad-URL keeps
  fields; garage page ≤ 60 KB gzipped).

### 4b-i — Durable queue and worker
- `jobs` + worker, checkpoints, attempts, dead-letter, coalescing via `run_subscribers` keyed by `analysis_key`,
  cancel/drain, attempt journal in the DB.
- **Gate:** `uv run pytest tests/idea_web/test_worker.py -q` (kill after `primary` → resume at `hints` with 0
  AudD requests; two users, same `analysis_key` → one run, two subscribers; one detaches → run continues; dead
  letter after 3; private-input runs never coalesce).

### 4b-ii — Progress, operations, shared breaker, backups
- Progress rows; wall-clock progress (U-F9) from per-phase medians; job-complete email hook; structured logs +
  redaction; spend metrics; disk alarm; `/healthz`; shared breaker over `provider_attempts`; `idea backup` /
  `verify-artefacts` (§4.6); `scripts/restore_drill.sh`.
- **Gate:** `uv run pytest tests/idea_web/test_ops.py -q` (breaker opens at budget/failure rate → new free jobs
  `waiting`, running job `degraded`; latch after 3 opens; backup during a bundle commit yields a consistent
  snapshot; restore drill from `tests/fixtures/snapshot/` passes `verify-artefacts`).

### 4c-i — Users, sessions, passwords, CSRF
- **Gate:** `uv run pytest tests/idea_web/test_auth.py -q` (fixation, rotation, expiry, CSRF/Origin, rate limits).

### 4c-ii — Invites, verification, reset, email interface
- **Gate:** `uv run pytest tests/idea_web/test_signup.py -q` (invite required; verify via console link in tests;
  hosted mode refuses console email; XSS via titles/comments escaped).

### 4d-i — Tenancy schema, credit lots, USD reservations
- §4.5 tables; §3.5 lots/allocations/events; `BEGIN IMMEDIATE` reservation; `idea billing grant-monthly`.
- **Gate:** `uv run pytest tests/idea_web/test_credits.py -q` (20 concurrent 60-min requests vs 150 min →
  exactly 2 accepted; soonest-expiring lot allocated first; settlement rules per status; refund to the same lots;
  monthly scheduler idempotent across reruns and month-end anniversaries).

### 4d-ii — Compatible-result serving, Deep-on-Free delta, caps
- `serves()` in the web path; cache-hit `library_items`; Deep-on-Free reserves primary only; length caps;
  source rules (`free_sources`).
- **Gate:** `uv run pytest tests/idea_web/test_cache_serving.py -q` (free request served by deep; d=2 not
  served by d=1's request; YouTube link refused on free; 151-min mix refused on free; Deep-on-Free FakeShazam
  gets 0 requests).

### 4d-iii — Library, usage meter, admin essentials
- Per-user library + delete; usage meter; capped-state prompt; `idea admin` (create-user, invite, grant,
  revoke, jobs, gc, disable-user, delete-user) with `admin_audit`.
- **Gate:** `uv run pytest tests/idea_web/test_library_admin.py -q` (isolation; N+1th blocked with prompt while
  a cache hit still opens; every admin action audited).

### 5a — Billing `off` / `local`
- **Gate:** `uv run pytest tests/idea_web/test_billing_local.py -q` (each simulate action → expected
  `entitlements`/`orders`/`invoices`/`credit_grants` rows; production refuses `local`).

### 6a-i — Ingest policy and SSRF
- `ingest_policy.py`; per-platform egress (proxy explicitly passed to yt-dlp for YouTube only); fetch-failure
  release; alias revalidation.
- **Gate:** `uv run pytest tests/idea_web/test_ingest_policy.py -q` (private ranges, redirect-to-private, DNS
  rebinding stub; proxy env not inherited by Shazam/AudD clients; alias mismatch closes the row); S2 recorded.

### 6a-ii — Uploads and media limits
- Streamed uploads, caps, ffprobe with limits, opaque ids, staging expiry.
- **Gate:** `uv run pytest tests/idea_web/test_uploads.py -q` (oversize, over-length, bad container refused;
  expiry GC).

### 6a-iii — Container, backups, hosted-ready
- Dockerfile/compose (web, worker, Caddy), Litestream, `idea backup` schedule, secrets file, migration `down`
  test, `scripts/gate_hosted_ready.sh`.
- **Gate:** clean-machine `docker compose up`; authorised SoundCloud source end-to-end inside the container;
  5-hour job refused; restart-during-job recovery; restore drill; the script writes `IDEA_HOSTED_READY=1`.

### 6b — Private-beta checklist
- Policy pages; attribution slot; `/admin` (queue, spend, breaker, disk, grants, disable/delete, audit);
  runbooks; canary; `docs/LAUNCH.md`; `scripts/gate_beta_config.py`.
- **Gate:** `uv run python scripts/gate_beta_config.py` passes; `docs/LAUNCH.md` complete except L1–L5.

### M2 (after the beta; still before any public launch)
- **7a — Stripe sandbox (was 5b):** §4.7; `docs/billing-testing.md`; gate `uv run pytest -m live
  tests/test_stripe_sandbox.py` with `stripe listen` (4242 → Pro lot; 0341 renewal → past_due → grace → revoke
  via test clock +1 month +1 h; cancel at period end; pack sync/async; refund → reverse; replay no-op; client
  price id ignored; live guard).
- **7b — Sharing and publication:** `/s/<token>`, OG, explicit publish, takedown/DMCA UI (behind flags).
- **7c — Self-service signup controls, annual edge cases, pack sizes:** disposable list, Turnstile, multiple
  packs.
- **7d — Visual polish (was 3b).**
- **7e — Physical deletions:** `scan.py` path, ACRCloud, `tl1001` (tag `pre-v2-removals` first).

---

## 6. Owner decisions, launch gates, risks

### 6.1 Owner decisions (2026-09-09) — constraints

| # | Decision | Trade-off / mitigation |
|---|---|---|
| **D1** | Hosted free tier runs on unofficial Shazam (round-1 P0 #2 declined). | ToS exposure accepted; capacity ≈ 40–65 mixes/day per egress; §2.3.4 breaker + latch; kill-switch; config fallback to sparse AudD |
| **D2** | Deep = AudD sweep first, Shazam sampled second. | $0.80–2.00/hour; allowances after L1 |
| **D3** | Panako kept; web wiring fixed. | not in the hosted image until needed |
| **D4** | YouTube kept when hosted. | paid tier only (D7); residential egress; S2 |
| **D5** | Subscription + non-expiring packs both built. | Stripe sandbox in M2; beta with billing off |
| **D6** | Consumer product as acquisition layer; pro path kept open in the data model. | corpus funding later |
| **D7** | **Free tier accepts SoundCloud + Mixcloud links only; YouTube links and uploads are paid features.** | £0 per free mix; none of the alternatives is legally cleaner |
| **D8** | Breaker trips automatically on throttling and latches after 3 trips/day; manual kill-switch for a hard block. | — |

### 6.2 Open questions (defaults set)

1. Free 150 min/week; caps 150/240 min — config. 2. Density 2 as Deep default after L1/L3? 3. Desktop build
("free = your own machine/IP") — recorded option, not adopted.

### 6.3 Launch gates

| | Gate | Owner |
|---|---|---|
| **L1** | AudD production terms in writing (hosted/consumer use, caching, attribution, concurrency, per-clip rate, reconciliation of ambiguous requests). **Blocks any hosted third-party AudD use, beta included.** | Nathan |
| **L2** | Legal review of ToS/privacy/DMCA and ingestion posture (scraped hint sources stay enabled pending it by D1/D4) | Nathan + solicitor |
| **L3** | **Release gate:** ≥ 5 owner-verified mixes, ≥ 3 DJs, ≥ 2 platforms, ≥ 4 h total, two-pass truth (`idea truth seed/verify/second-pass/resolve/freeze` → `data/corpus/release-1/`); `uv run idea analyse <url> --recipe free|deep` per mix; `uv run python scripts/score_corpus.py --truth-dir data/corpus/release-1/truth --runs data/corpus/release-1/runs-<recipe>.json --out docs/accuracy/release-1-<recipe>.json`; thresholds: LIKELY precision ≥ 0.90, listed precision ≥ 0.80, work recall ≥ 0.75 (`deep`) / ≥ 0.70 (`free`). No accuracy claim or "confirmed twice" marketing before it passes. | Nathan (+ tooling from 1b-ii) |
| **L4** | Stripe business country, KYC, live keys, VAT position | Nathan |
| **L5** | Domain, email provider, restore drill on the real host | Nathan + 6a-iii |
| **L6** | Beta ≥ 2 weeks invite-only with billing off before M2 7a is enabled | — |

### 6.4 Risks

| # | Risk | L / I | Mitigation |
|---|---|---|---|
| R1 | Shazam blocks the server IP | medium / high | breaker + latch; kill-switch; config flip; Deep unaffected |
| R2 | YouTube blocks datacenter fetches | high / medium | paid-only; residential egress; upload fallback; S2 |
| R3 | Only walk-up AudD rate | medium / medium | density 2; §3.3 table; USD ceilings |
| R4 | Weak willingness to pay; Beatport | high / medium | tiny fixed costs; beta first; pro hedge |
| R5 | Frozen-profile byte test | certain / low | recipes are data; golden semantic |
| R6 | Cycle too big | medium / low | 31 cycles, each ≤ ~1 day |
| R7 | Disk fills | certain w/o 2b / high | retention for all terminal states; manifest gate; alarm |
| R8 | Paid run fails after spend | medium / medium | attempt states; ambiguous counted; release rules |
| R9 | Free-tier abuse | medium / medium | lots + allocations; invites; per-IP limits; budgets |
| R10 | AudD terms unknown | certain / high | L1 blocks hosted use |
| R11 | Throttled Shazam thin runs | certain today / high | E-S1; `degraded`; breaker |
| R12 | Free overwrites Deep | certain today / medium | bundles + `serves()` (1a) |
| R13 | Restart double-bills AudD | medium / medium | attempt states; bounded by in-flight concurrency; L1 reconciliation question |
| R14 | Inconsistent backups | medium / medium | fenced snapshot + listed artefacts + verify |
| R15 | Mutable source URL serves stale bytes | medium / low | alias revalidation; `source_changed` |

---

## Appendix A — register

| Item | Where |
|---|---|
| E-C1, E-C4, E-H1, E-H2, E-H3, E-M5, E-S3, E-S4 | 0a-i |
| E-H8, E-H9, E-M9, E-L4, U-F5 (loopback) | 0a-ii |
| E-C3 anchor, E-H4, E-H5 | 0b-i |
| E-H6, E-M2, E-M10, E-S1 | 0b-ii |
| bundles, `analysis_key`, `serves()`, Deep-on-Free, `source_changed`, audio route | 1a |
| targeting | 1b-i |
| E-C2, E-C3 trial source, E-M1, E-M3, E-M4, E-M6, E-L5, U-F13, breaker, corpus scorer | 1b-ii |
| E-L1, E-M7 | 2a |
| E-M8, E-S2 | 2b |
| U-F2/F3/F14 | 3a-i |
| U-F4, F7, F8, F10, F11, F12, F19, F20, F31 | 3a-ii |
| E-L2, service API, packaging | 4a-i |
| parity, U-F6 render policy, targets | 4a-ii |
| U-F1, F15, F18, F29, F30, F34 | 4a-iii |
| E-H7 queue, R13 | 4b-i |
| U-F9, U-F33, shared breaker, R14 | 4b-ii |
| auth | 4c-i / 4c-ii |
| lots, USD, scheduler | 4d-i |
| compatibility serving, caps, D7 | 4d-ii |
| library, admin | 4d-iii |
| billing local | 5a |
| SSRF, egress, aliases | 6a-i |
| uploads | 6a-ii |
| container, backups, hosted-ready | 6a-iii |
| policy, runbooks, beta config | 6b |
| Stripe, sharing (U-F39), signup controls, polish (U-F16/17/21–28/35), deletions | M2 7a–7e |
| **Round-3 required changes** | 1 → 0a-i (recipes/CLI/gate/fakes) · 2 → §2.3.2 · 3 → §3.4 `serves()` · 4 → §2.3.4 step 1, §3.4 coalescing · 5 → §2.3.3 · 6 → §3.4 `bundle_id`, §4.5 `library_items` · 7 → §3.5 settlement · 8 → §3.5 lots/allocations · 9 → §4.5 `provider_attempts`, `invoices`, `order_credit_links`, scheduler · 10 → §4.6 · 11 → §2.3.4 · 12 → §5 (31 cycles, exact selectors) · 13 → L3 (`score_corpus.py`) · 14 → §3.1, §4.1 · 15 → §3.4 aliases/`source_changed` · 16 → §5 M1/M2 sequencing |
| Round-1 P0 #2 | declined — D1 |

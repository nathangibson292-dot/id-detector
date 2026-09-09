# IDea v2 — from a local tool to a product people pay for

*Plan rev 3 · 2026-09-09 · written from four independent adversarial reviews (engines/fusion, UI/product,
market, hosting/payments) against `main` @ `27c36fd`; revised after Codex plan-review rounds 1 and 2
(`docs/reviews/plan-v2-review-round-{1,2}.md`) and the owner's decisions of the same day (§6.1).
Appendix A maps every finding and every round-2 required change to the section or phase that resolves it.*

---

## 0. The verdicts, in plain English

| Question Nathan asked | Verdict |
|---|---|
| **Is the business model right** (free capped weekly, paid capped monthly)? | **Yes, with three changes.** (1) Meter **minutes of audio**, not mixes. (2) The paid allowance is **Deep-scan minutes**; on Pro every new mix is a Deep scan. (3) A mix already in the cache is served free and uncounted when the cached result *achieved* everything the request needs; a Deep request on a Free result pays for the missing AudD sweep. Both a subscription and non-expiring packs. §3. |
| **Could we charge for it?** | **Not today; yes after Phases 0–1.** The paid tier crashes after spending whenever AudD returns any parseable result (E-C1); with an exhausted key it silently runs the free engine and caches the error bodies (E-H3, E-C4); it has no spend cap (E-H1); and as built it throws away the benefit it is meant to buy (E-C2/E-C3). |
| **Is it the best implementation?** | **No.** The pipeline core is sound and well tested; the paid sequencing is broken in four specific ways (§2.3) and the web layer cannot be hosted as-is (§2.2). |
| **Is the engine order right?** | **Paid engine first, then Shazam — yes (owner decision D2), but not the way it is built.** After the AudD sweep, Shazam must sample the *uncertain* spans as well as the blanks; AudD must carry a time anchor and its own vote; spend must be capped; the run must not crash. §2.3. |
| **Does the UI work / show useless data?** | The look is good — keep it. The numbers and words are not: card vs page counts disagree, downloads can disagree with the page, the page advertises its own noise, two columns are constant across 305 tracks, buy links vanish on phones, a bad URL lands on raw JSON, the privacy footer is false. §2.5. |
| **What should be removed?** | Whole-file paid scan path + consent gate, ACRCloud, the 1001tracklists connector, rescan button/route, version/role columns (page), hidden-matches reveal, M3U export, the duplicate `/new` form, dead POST params. Quarantined: rescans, calibration ML, benchmark, truth. **Kept and fixed: Panako** (D3). §2.6. |
| **Does it fill a genuine gap?** | **Partly — and not where we assumed.** "Paste a link, get a tracklist" is a commodity (21 hosted tools, ~$1/mix, falling traffic, weak willingness to pay). Nobody ships "right track vs right *recording*" or a re-checkable evidence trail — which matter to royalty/clearance/label buyers, which needs certified tiers, which needs the real-mix corpus. §2.7, D6. |

**Shape:** spikes and owner gates (S) in parallel; Phases 0–3 make the *existing local tool* correct, bounded,
honest and cheap (24 build cycles in total, each ≤ ~1 day, each with a named fixture and command); Phases 4–6
add the web layer, durable jobs, accounts, ledgers, billing (test mode) and the hosting shape, ending in an
**invite-only private beta** with billing off (§3.6).

---

## 1. What was reviewed and how

| Review | Scope | Method |
|---|---|---|
| **E — engines & fusion** (`docs/reviews/v2-review-engines.md`) | `cli._analyse`, `paid_clip`, `scan_targeting`, `fuse/*`, `providers/*`, job runner, disk, tests | code reading + `dis` verification + `du` + test timing |
| **U — UI / product** (`docs/reviews/v2-review-ui.md`; `docs/screenshots/v2-before/`) | every screen | live server, headless-Edge screenshots, `node --check`, data audit of 305 tracks |
| **M — market** (`docs/research/05-market-2026-09.md`) | 21 hosted competitors, metering, engine pricing/terms, demand | web research, cited; Reddit/HN verified |
| **H — hosting & payments** (`docs/reviews/v2-review-hosting.md`) | yt-dlp/Shazam from a server, AudD terms, Stripe, framework, sizing, abuse | web research + code, cited |
| **Codex plan reviews** (`docs/reviews/plan-v2-review-round-{1,2}.md`) | rev 1, rev 2 | independent code verification |

Current offline suite: **581 passed in 44 s** (93 deselected `slow`/`live`); the two full-pipeline modules are
`slow` and never run by default — which is how E-C1 shipped.

---

## 2. Findings that decide the plan

### 2.1 The paid tier does not work (Critical, verified three times)

| ID | Finding | Evidence |
|---|---|---|
| **E-C1** | **The paid-first branch deterministically crashes after presentation whenever it receives any parseable AudD observation** (incl. a no-match): `matches`/`recognised` read at `cli.py:794-815`, bound only in the free branch (`cli.py:507-521`). If AudD is unavailable or every call errors, it falls back to free instead (`cli.py:499-506`) — see E-H3. Reached via the web runner (`runner.py:137-167`); CLI `--profile max_accuracy` alone is Shazam-only (E-M6). | `dis` → `LOAD_FAST_CHECK`; no test sets `primary_engine` |
| **E-C2** | **Paid-first eliminates the designed overlap between engines**: the free pass covers only the complement of non-suppressed AudD episodes (`scan_targeting.py:61-69,127-146`; `cli.py:659-682`); `_engine_corroborated` checks no temporal overlap (`episodes.py:148-156`), so any flag that fires is incidental. | code |
| **E-C3** | **AudD can downgrade tracks**: clip observations have `anchor=None`, no own trial source (`audd.py:344-365`) → shared `primary` group (`alignment.py:137-150`); when both name one candidate the zero-skew AudD vote can win and an anchorless winner makes no alignment point (`alignment.py:115-124`) → `likely` unreachable + spurious `hypothesis_rejected`. AudD's standard result carries `timecode`; the enterprise parser already builds an anchor from it (`audd.py:242-250`). | code |
| **E-C4** | **AudD error bodies cached indefinitely under the current key**: written before parsing (`paid_clip.py:182-190`), trusted on read (`:175-181`). | code |
| **E-H1** | No spend cap or record: `max_clips=len(windows)+1`; `usd_e2` journaled `0` (`cli.py:820/850/864`); no USD field in `AppConfig`. | code |
| **E-H2** | Gap-fill request counts overwritten with 0 (`cli.py:649-655,676`). | code |
| **E-H3** | Silent paid→free downgrade on missing/invalid key, exhausted quota, outage. | `cli.py:499-506` |
| **E-H4** | Paid pass uncancellable; ETA "1 s". | `paid_clip.py:161-197`, `jobs.py:211-221` |
| **E-H5** | Paid pass sequential, unrated, unretried; timeout → re-billed next run. | `paid_clip.py` |
| **E-H6** | `present_min_track_ms`, `shazam_requests_per_minute`, `recognise_concurrency`, `collapse`, `same_track_bridge_ms` dropped under a profile (`cli.py:985-1007`, `runner.py:48-72`); `config show` prints file config only (`cli.py:303-318`). | code |
| **E-H8** | `upload_consent` / `build_index` accepted from POST bodies; `upload_consent=true` runs the whole-file scan *in addition*. | `server.py:1373-1406`, `cli.py:527-547` |
| **E-H9** | Web "build index" builds a Panako index never queried (`local_index_label` never passed). | `runner.py:130-172`, `cli.py:636` |
| **E-M1** | Corroboration counts provider strings; `COMMERCIAL_PROVIDERS` is a discount set, not families. | `episodes.py:91-108,148-156` |
| **E-M2** | Spectral novelty (~0.7 GB RAM/hour) on every fuse, consumed by nothing with rescans off. | `orchestrate.py:140` |
| **E-M3** | `select_gap_targets` uses any coverage → one phantom blocks the free pass. | `scan_targeting.py:60-68` |
| **E-M4** | Crowd rows sorted by opaque hash; contradictory answers both listed. | `episodes.py:790-845` |
| **E-M8** | ~330–370 MB per hour of audio; 10–25 MB durable; nothing reclaims. | measured |
| **E-L1** | 2.1 s startup tax from eager `benchmark/`, `calibrate/`, `truth` imports. | measured |
| **E-S1** | Malformed Shazam bodies under throttle raise `ShazamHTTPError` (`shazam.py:200-204`) → **failure** observations (`recognise.py:307-340,521-553`), poorly surfaced → thin runs that don't say so. | code |
| **E-S2** (new) | **Completion sidecars verify that every upstream file still exists and hashes the same** (`io.py:148-176`); the gen-N fuse sidecar lists every window file, the PCM, identities and hints (`episodes.py:1136-1151`). Deleting `windows/**` or the PCM invalidates them. | code |
| **E-S3** (new) | Both frozen profiles have `budget.max_usd_e2 = 0` (`profiles/*.json`); `profile_app_config` drops it anyway (`profiles.py:741-753`). | code |

### 2.2 The web layer cannot be hosted as-is (verified)

One in-memory worker thread; restart loses every job. No users/sessions/CSRF — **any website the owner visits
can `POST /analyse` to `127.0.0.1:8765` and start a paid run** (U-F5); `validate_target` accepts `file://`
and any path (`jobs.py:59-84`) and the server serves it back. Loopback-only by design; HTTP/1.0; 178 KB pages
(U-F29). `ProcessLock` raises instead of coalescing (E-H7); two URLs converging on one media dir collide
(`cli.py:412-422`). The renderer emits `<audio src="../ingest/original.*">` whenever the original exists
(`page.py:1168-1172`) — hosted, that is redistribution (U-F6). A GET of a stale page rewrites `index.html`
without the exports (U-F14/F3). **Result artefacts are mutable singletons** (`fuse/episodes.json`,
`present/tracklist.*`): a later Free run overwrites a Deep result. `_load_cached` hashes the retained original
(`ingest.py:143-164`).

### 2.3 Engine sequencing — Deep scan v2

**Owner decision D2:** the paid engine sweeps first, Shazam second. Free Shazam is throttled per IP (the owner's
live 2-hour run: 789 windows at 12.5 req/min ≈ 63 min); AudD has no per-IP throttle. What was wrong with
paid-first as built: Shazam only on blanks (E-C2); AudD anchorless and voteless (E-C3); no cap (E-H1); crash
(E-C1); sequential loop (E-H5).

**Recipes.** Exactly two analysis recipes exist; both are frozen data (`src/id_detector/recipes.py`), hashed
to a `recipe_id` over the fields below. **No Shazam-first paid recipe exists** (round-2 P0 #3).

| Field | `free` | `deep` |
|---|---|---|
| `primary_engine` / `primary_density` | shazam / 1 (every window of the frozen 12 s / 9 s set) | audd / 1 (config may set 2 = every other window; not a separate product) |
| `secondary_engine` | none | shazam |
| `secondary_clips_per_minute` (cap = ⌈duration_min × rate⌉) | — | 2 |
| `secondary_reserve_fraction` (held for confirmations) | — | 0.25 |
| `secondary_priority` | — | `hint_only` → `listed_not_confident` → `suppressed_challengeable` → `blank` |
| `overlap_min_ms` (corroboration) | — | 6000 |
| `separation_min_ms` (two agreements count as separate) | — | 60000 |
| `audd_concurrency` / `audd_retries` | — | 4 / 3 (backoff 1 s, 2 s, 4 s on 429/5xx/timeout) |
| `max_usd_e2` (per run, hard) | **0 = no paid calls permitted** | 400 ($4.00) |
| `adapter_versions` | shazam:1 | shazam:1, audd_clip:2 (anchor-capable) |
| `required_capabilities` | {shazam_sweep, hints} | {audd_sweep, audd_anchor, hints, shazam_secondary} |

`presentation_version` is **not** part of the recipe (a theme change never forces re-analysis). The frozen
profiles are untouched; their `budget.max_usd_e2` is documentation of the retired file-scanner budget and is
never read by the clip path (E-S3).

**The deep recipe, step by step:**

1. **Reserve** — `usd_e2_reserved = ⌈(primary_clips + secondary_cap) × audd_usd_e6_per_request / 10⁴⌉` (config
   `audd_usd_e6_per_request`, default 5000 = $0.005), checked against `max_usd_e2`, then (hosted) the account
   and global USD ledgers, in one transaction, before any network call. A cached run reserves only the missing
   work.
2. **Primary sweep** — AudD over the mix at `primary_density`, `audd_concurrency` workers behind a token bucket,
   retries as above, progress per clip (cancellable). Each observation: anchor from `timecode`,
   `simultaneous_source="audd"`. Outcomes recorded per clip: `match | no_match | retryable_failure | ambiguous`
   (§3.4). **An ambiguous clip is counted as spent and is never retried automatically** (round-2 P0 #7); the run
   continues and finishes `partial` if ambiguous > 5 % of clips.
3. **Hints → first fuse.**
4. **Secondary (Shazam) targets** — cap `C = ⌈duration_min × 2⌉`, of which `⌊0.25·C⌋` is reserve.
   Candidates, in priority order, each with its span: `hint_only` rows; listed-but-not-confident episodes
   (tier < likely and not corroborated); suppressed candidates with ≥ 2 votes; blank spans ≥ 12 s.
   Allocation: (i) one window per candidate in priority order, ties by longer span then earlier start, until the
   non-reserve cap is exhausted — **if candidates exceed the cap, lower-priority candidates get none** (overflow
   rule); (ii) remaining non-reserve windows distributed to candidates proportionally to span duration (largest
   remainder), (iii) within a span, windows are ranked by intersection length with the span, then by RMS energy
   (highest first), then by start; overlapping picks are dropped. Reserve use: when a secondary probe returns a
   new identity inside a blank, two additional windows ≥ 30 s apart around it are queued from the reserve; unused
   reserve returns to (ii) at the end.
5. **Re-fuse once.** Corroboration = **selected votes** for the same normalised work whose supports overlap by
   ≥ `overlap_min_ms`, from different trust families — `catalogue` {audd, acrcloud}, `shazam`, `local_index`
   {panako}; crowd hints stay hint corroboration. One cross-family agreement → the row shows "confirmed twice";
   **bypassing suppression and the 30 s floor requires either two cross-family agreements ≥ `separation_min_ms`
   apart, or the existing `likely` rule (T_ind ≥ 4 non-overlapping supports and on-air ≥ 30 s).**
6. **Status** — every run records `requested_recipe_id`, `achieved_capabilities`, and a status:

| Status | Meaning | Exit code | Minutes/USD | Cache eligibility | Page |
|---|---|---|---|---|---|
| `complete` | all required capabilities achieved | 0 | settled | serves exact and dominated requests | normal |
| `degraded` | a required capability missing (e.g. Shazam circuit open → no secondary) | 0 | settled for work done; secondary minutes refunded (hosted) | serves only requests whose `required ⊆ achieved` | banner "Deep scan · degraded: … " |
| `partial` | > 5 % ambiguous or retryable failures on the primary | 0 | settled; ambiguous counted as spent | never dominates; exact re-request re-runs failed clips only | banner |
| `provider_unavailable` | paid engine could not be used at all (auth/quota/network) | 3 | fully refunded | not cached | error page |
| `budget_exhausted` | reservation would exceed a ceiling | 4 | nothing spent | not cached | error page |
| `failed` / `cancelled` | exception / user cancel | 1 / 130 | refunded; spent clips counted for abuse ceilings | not cached | error page |

**Dominance (cache rule):** a stored result R serves a request Q iff `Q.required_capabilities ⊆ R.achieved_capabilities`
and R.status ∈ {complete, degraded}. `free` is dominated by a complete `deep` (the Shazam sweep is a superset of
the sampled secondary). **A Deep request on a stored Free result reuses the cached Shazam evidence and runs
the AudD sweep only (reserve = primary clips)** (round-2 P0 #5). A stored result is never overwritten by a run
with fewer achieved capabilities; each run writes its own result bundle (§3.4).

**Circuit breaker:** per-process budget in Phase 1 (`shazam_daily_budget` per egress, `shazam_failure_rate`
threshold → secondary skipped, status `degraded`); the shared, service-wide breaker lives in the jobs DB from
Phase 4b-ii. Never a mid-run engine swap; never residential-proxied Shazam.

**Cost per 60-minute mix, 400 windows** (AudD $5/1,000 walk-up · $2/1,000 subscription):

| Recipe | AudD req | Shazam req | AudD $ | Wall-clock (after 0b) |
|---|---|---|---|---|
| `free` | 0 | 400 | $0 | 9–60 min (IP throttle) |
| `deep` (density 1) | 400 | ≤ 120 | $2.00 · $0.80 | ~4 min + 3–10 min |
| `deep` (density 2, config) | 200 | ≤ 120 | $1.00 · $0.40 | ~2 min + 3–10 min |

ACRCloud removed (same family as AudD, cannot corroborate it; ≈ same coverage live). Panako kept (D3) as the
`local_index` family; web wiring fixed.

### 2.4 Accuracy: where the real edge is

Shazam alone caps at ~27/30 on the owner's benchmark; the misses are in no commercial catalogue. Levers: crowd
IDs and a DJ's own indexed uploads. Cross-family agreement is the strongest precision signal. Bugs riding along:
comment text rendered as a track (U-F13), contradictory crowd answers both listed (E-M4). **No accuracy number is
advertised before L3.**

### 2.5 UI: remove / change / add (review U)

**Remove:** hidden-matches counter+toggle (U-F7); `Version`/`Role` columns from page + Markdown (U-F8; kept in
JSON; shown only when verified/contested); rescan button/column/`POST /rescan` (U-F11); `HINT` pill; `layer`/
`outgoing` tags; `ID gap` tile; "9h 03m listened"; M3U export; duplicate `/new` form; the "nothing leaves
127.0.0.1" footer (U-F12); `no_evidence` / "suppressed" / "buried…" strings; `upload_consent` param.

**Change:** bad URL → inline error, form preserved (U-F1); **one typed entry list (tracks, gaps) feeds page,
Copy, CUE, Markdown, JSON and the library card**; exports regenerated with the page as one immutable bundle
(U-F2/F3/F14); "Where to get it" kept on phones — stacked cards ≤ 720 px (U-F4); `purchase_url` before store
search, labelled fallbacks, column hidden when empty (U-F10); wall-clock progress (U-F9); failure copy: plain
cause + remedy, failed step marked, credit statement (U-F15); one confidence word per row + glossary; legend
6 → 2 (U-F16/F17); `--dim` ≈ `#8b8ba6`; `<tr>` without `role="button"`, `scope="col"` (U-F19/F20); "Free scan"
/ "Deep scan" chip; three tiles (U-F21); alternatives only for a different work (U-F25); lead-in behind details
(U-F26); options summary shows the pasted tracklist (U-F28); in-progress cards titled + friendly steps (U-F18);
no auto-redirect (U-F30); crowd rows without comment text or badge (U-F13); one privacy line; run status in the
header (§2.3 table).

**Add:** analysed date; share link `/s/<token>` (Phase 4d-ii; disabled in the beta); per-user library with
delete; "you can close this tab"; failed runs visible (U-F33).

**Keep:** club-mode theme, logo, scanner strip, mix cards, `prefers-reduced-motion`, NOW pill, CUE/Markdown/JSON,
the paste-a-tracklist box.

### 2.6 Remove / quarantine / keep (subsystems)

| Subsystem | Decision |
|---|---|
| Whole-file `scan.py` path, `run_paid_scanners`, consent gate, `upload_consent` | disable 0a → delete 2b (tag `pre-v2-removals`) |
| `providers/acrcloud.py` (+ tests, `_Bundle` branch); `--engine acrcloud` | 0a: flag refused with "ACRCloud is not supported"; 2b: delete |
| `hints/connectors/tl1001.py` | 2a default-disabled → 2b delete |
| **Panako** + web `build_index` | **keep; 0a wires `local_index_label`**; excluded from the hosted image until needed |
| Rescans + transform grid, `orchestrate` loop | quarantine (profiles certify `rescans=3`) |
| `calibrate/`, `benchmark/`, `truth.py`, `local_fixture.py`, `pointer_import` | quarantine: lazy import + `dev` extra (2a) |
| `novelty.py` | guarded on `max_generations > 0`, hoisted (0b) |
| `sc_comments`, `manual`, `mixesdb`, `yt_comments`, `mixcloud` hints | keep (L2 review item); hard timeouts when hosted |
| CUE / Markdown / JSON | keep; M3U removed |

### 2.7 Market (review M — unchanged from rev 2; summary)

21 hosted competitors, pay-per-mix $0.99–2.99, subscriptions €6–$11, traffic falling category-wide; set79 and
TrackRadar already read comments / leave gaps blank; **nobody** ships work-vs-recording or an evidence trail;
demand is large (MixesDB 21.9 % of 366 K mixes have no tracklist; SoundCloud #DJSET +39 % YoY) and the
complaints match our design (edits resolving to originals; pitch-shift), but willingness to pay is weak (a
371-upvote free tool shut down: "people love to find this for free but wouldn't pay"); Beatport Track ID (10 M
users) now bundles live ID. Metering lessons: minutes, never cripple the free result, non-expiring packs, refund
on failure, cached catalogue as the SEO/business layer. AudD: $5/1,000 walk-up, "$2/1,000" subscription
(enterprise docs; per-clip endpoint unconfirmed — L1); Test License = evaluation-only + attribution; paid terms
unreadable by fetchers. No legal server-side Shazam; no enforcement found; competitors run it openly.
Uniqueness: two-axis confidence, evidence trail, uncertified honesty → the pro/report buyer (D6). Consumer
angle: scene-specific (UK bass/garage; underground/unreleased).

---

## 3. Product definition v2

### 3.1 Two modes, one codebase

**Local mode** (`idea.cmd` → `idea serve`): the browser workflow — paste a link, watch progress, open the
result, play local audio, export — passes a Windows gate at every boundary. CLI changes are listed per phase.
**Hosted mode** (`IDEA_MODE=hosted`): accounts, ledgers, platform embed, no local paths; **a non-loopback bind
is refused unless `IDEA_HOSTED_READY=1`, which only the Phase 6a-ii gate script writes** (round-2 P1 #16).

### 3.2 Scans

| | Free scan (`free`) | Deep scan (`deep`) |
|---|---|---|
| Engines (local and hosted — D1) | Shazam sweep + crowd hints | AudD sweep → hints → Shazam sampled → re-fuse |
| User sees | LIKELY / POSSIBLE / UNCLEAR, "from comments", run status | + "confirmed twice", tighter boundaries, rescued short tracks when doubly confirmed |
| API cost / hour of audio | $0 (Shazam IP budget) | $0.80–2.00 (density 1) |
| Shazam throttled | finishes `degraded` with failed-window count; service breaker pauses new free scans | finishes `degraded` (AudD-only) |

### 3.3 Tiers, caps, prices

Unit = **minutes of new audio**, rounded up. **Free allowance resets weekly** (Monday 00:00 UTC). **Pro allowance
is credited monthly on the subscription's monthly anniversary regardless of billing interval** (annual = 12
monthly credits); unused Pro minutes expire at the next credit; packs never expire; consumption order = Pro
minutes → packs. All values are config (`docs/pricing.toml`, versioned; every reservation records the
`pricing_version` and `audd_usd_e6_per_request` it used).

| | **Free** | **Pro** | **Packs** |
|---|---|---|---|
| Price (GBP incl. VAT) | £0, no card | £9 / month; £90 / year | from £6 |
| Allowance | 150 min / week, `free` recipe, full results | **N Deep minutes / month** (N from the table below) | Deep minutes, never expire |
| Mix length cap | 150 min | 240 min (longer refused) | 240 min |
| History | 30 days (bundle GC by reference count) | unlimited | 90 days |
| Cached mixes | free, uncounted, 20/day | same; Deep-on-Free pays the AudD sweep | same |
| Failure | minutes refunded; spent clips count toward abuse ceilings | same | same |

**N and pack sizes** — COGS per Deep hour = AudD (400 req) + **5 % retry/ambiguous overhead** + YouTube proxy
share (assume 50 % of mixes × $0.30) + compute/storage $0.05; £9 → $11.50 gross, − VAT 20 % − Stripe (1.5 % +
20p + 0.7 % Billing) ≈ **$8.90 net**; £6 → ≈ $5.75 net; target ≥ 50 % gross margin:

| AudD rate | Deep $/hour | Pro £9 → N | Pack £6 → min |
|---|---|---|---|
| walk-up, density 1 | $2.30 | ≈ 115 min | ≈ 75 min |
| walk-up, density 2 | $1.25 | ≈ 210 min | ≈ 135 min |
| subscription, density 1 | $1.04 | ≈ 255 min | ≈ 165 min |
| subscription, density 2 | $0.62 | ≈ 430 min | ≈ 275 min |

**No price or allowance is published, and no third-party Deep scan runs, before L1** — the private beta runs
with billing off and admin grants (§3.6), and hosted AudD use in the beta is itself gated on L1 (round-2 P0 #6).
**Free-tier cash cost is £0 per mix** (free accepts SoundCloud/Mixcloud links only — Open Q 2 decided; YouTube
and uploads are paid features, so the residential proxy is paid from the customer's money); the flat server is
the only fixed cost; Shazam is $0 but capacity-bounded (§4.1). Kill-switch policy: the breaker trips
automatically on throttling (failure rate or budget), resumes after cool-down, and **latches off after three
trips in one day until an admin re-enables it**; `IDEA_ENGINE_SHAZAM=off` is the manual switch for a hard block.

### 3.4 Recipes, result bundles, ledgers

- **Result bundle** — every run writes `present/bundles/<run_id>/` (page, exports, `manifest.json` listing
  files, hashes, sizes, `requested_recipe_id`, `achieved_capabilities`, `status`, `presentation_version`,
  `pricing_version`, `usd_e2_spent`) and `fuse/runs/<run_id>/` (episodes, identities); `present/current`
  is an atomic pointer to the **dominant** bundle (most achieved capabilities, then newest). Bundles are
  immutable; refresh (presentation-version migration) writes a new bundle from the same fuse run and
  re-points. **Delivered in Phase 1a** (round-2 P0 #4).
- **Provider response cache** (`recognise/invocations/<adapter>-v<N>/raw/<key>.json`) states:
  `match | no_match` (cacheable, TTL per config) · `retryable_failure` (not cached) · `ambiguous` (recorded in
  `recognise/ambiguous.jsonl` with request id, never cached as evidence, counted as spend). The **attempt
  journal** (`recognise/attempts.jsonl`: key, outcome, http status, usd_e6) is separate from the cache.
  `--refresh` takes `--refresh-states no_match,retryable_failure` (default) and never re-bills `match`.
- **One credit ledger** (`credit_ledger`: id, account, kind ∈ {plan_credit, pack_purchase, admin_grant,
  reservation, settlement, refund, expiry, reversal}, minutes (signed), ref_type/ref_id, pricing_version,
  created_at) — balance = Σ; a reservation is inserted in the same `BEGIN IMMEDIATE` transaction as the balance
  check and the job insert; settlement/refund reference the reservation. **`usd_ledger`** has the same shape for
  provider spend at scopes `run | account_month | global_day`; a run's reservation must fit all three. The old
  `quota_usage` counters do not exist.
- **Sidecars after pruning** (E-S2): retention rewrites a sidecar's `upstream` entries for deleted files into
  `pruned_upstream` (same hashes); `verify_completion_sidecar` accepts a pruned entry when the artefact's own
  hash still matches; any consumer that needs a pruned file re-derives it (windows from PCM; PCM from the
  original; original by re-fetch) and fails with `source_unavailable` if it cannot.

### 3.5 Sign-in and payments (decided)

Email + password (argon2-cffi); verification required before any allowance; server-side sessions; CSRF +
Origin on every state-changing POST. `BILLING_BACKEND=off | local | stripe` (§4.7); live needs `sk_live_` **and**
`IDEA_BILLING_LIVE=1`.

### 3.6 The private beta (round-2 P2 #18, reconciled with D5)

Everything in Phases 4–6 is *built* (D5: subscription + packs, verified in test mode). The **beta configuration**
is narrower: `BILLING_BACKEND=off` with audited admin grants; **invite codes required to sign up**; one recipe
per tier (`free`, `deep` density per L1); publications and share links **disabled** (`IDEA_SHARING=off`,
`IDEA_PUBLIC_CATALOGUE=off`); Panako not in the image; Phase 3b polish not beta-blocking.

---

## 4. Hosted architecture

### 4.1 Showstopper checks

| Check | Finding | Consequence |
|---|---|---|
| Unofficial Shazam from a server | ~20 req/min per IP shared by all users → ~40–65 hour-long mixes/day per IP; past it, failures (E-S1); ASN blocks escalate; Apple's terms forbid automation; no official server API | **D1 accepted.** Mitigations: per-account weekly cap; **`provider_requests` ledger per egress IP per day** with `shazam_daily_budget_per_egress` (default 20,000 req ≈ 50 mixes); breaker opens at budget or when the rolling 5-minute failure rate > 30 % → new free jobs wait ("busy"), running jobs finish `degraded`; decode errors count as throttle; kill-switch `IDEA_ENGINE_SHAZAM=off`; extra egress IPs are config rows; never residential-proxied |
| yt-dlp from a server | SoundCloud/Mixcloud direct; YouTube needs residential egress (~65 MB/mix) + Deno + `curl-cffi` | **D4 kept**; per-platform egress; global proxy budget; upload fallback; refund on fetch failure; **S2 must pass before 6a-i** |
| AudD terms | pricing public; DJ sets a listed use case; production terms unreadable; trial = evaluation-only + attribution | **L1 blocks any hosted third-party AudD use, beta included** |
| Disk | 330–370 MB/hour; 10–25 MB durable | retention §4.6 incl. failed/cancelled jobs |
| RAM | novelty ~0.7 GB/hour | guarded off (0b); 1.5 GB/job; length caps |

### 4.2 Shape

```
Internet ─:443─▶ Caddy (TLS, gzip, 250 MB body cap, security headers) ─▶ uvicorn · idea_web (FastAPI)
                                                                          │ pages · auth/sessions · CSRF/Origin · ledger reservation
                                                                          │ billing provider · POST /analyse → jobs row · status polling
                                                                          │ result routes (authz, traversal-safe) over present/bundles/<run>/
                                                                          ▼
                                                                     app.db (SQLite WAL; Litestream) + artefact snapshot job
                                                                          ▲ lease / heartbeat / checkpoints / progress / subscribers
                                                                     idea_web.jobs.worker (separate process; N=1)
                                                                          │ ingest policy (allow-list, SSRF guard, egress per platform, upload ids)
                                                                          │ id_detector.service.run(RunRequest) ← pipeline public API
                                                                          │ retention GC (terminal states)
                                                                          ▼
                                                                     work/<source>/<media>/ (content-addressed; durable subset backed up)
```

Rules: the web process never runs a pipeline; the worker never serves HTTP; workers never receive user paths
(targets: `PlatformUrl | UploadId | LocalPath(local mode only)`); billing touches the app through four methods;
`src/id_detector/` changes after Phase 3 are limited to `service.py`, `recipes.py`, the `serve` entry point and
`PROJECT_ROOT`.

### 4.3 Web layer

FastAPI + Jinja2 + uvicorn (S4 ADR decides vs Django/allauth before 4a-i; if Django wins, 4c shrinks and
4a-i's port targets Django views — the rest of the plan is unchanged). `src/idea_web/` added to the wheel.
`idea serve` launches the same app in local mode; `present/server.py` retired after parity. Renderers unchanged
in role; CSS/JS to static files. Polling progress at 2.5 s. Render policy: `emit_local_audio` false in hosted mode.

**Service API (round-2 P1 #13):** `id_detector.service.run(RunRequest(run_id, target, recipe, checkpoint_store,
progress, cancel_token)) → RunResult(run_id, status, achieved_capabilities, bundle_path, usd_e2_spent,
requests)`; `run_id` is supplied by the caller (stable across restarts); `checkpoint_store` persists per-phase
completion (`ingest, decode, windows, primary, hints, fuse1, secondary, fuse2, present`) so a resumed run skips
completed phases and re-derives only what a pruned sidecar demands; idempotency = the same `run_id` never
re-issues a provider request whose attempt is journaled.

### 4.4 Accounts, sessions, abuse controls

argon2-cffi; server-side sessions (`__Host-idea_session; HttpOnly; Secure; SameSite=Lax`, `sha256(token)`
stored, rotate on login/plan change, 30 d idle / 90 d absolute); single-use hashed email tokens; verification
required; CSRF synchroniser tokens + Origin check; per-IP limits (signup 3/day, login 10/15 min, reset 3/h,
analyse 5/h) with IPv6 /64 normalisation and trusted-proxy config; disposable-email list (soft fail); optional
Turnstile; CSP/security headers; export routes 10/min. Email provider interface; `console` backend refused when
`IDEA_MODE=hosted`. Beta: invite codes.

### 4.5 Data model (SQLite WAL; numbered SQL migrations, each with a `down` script)

`users` (id, email, pw_hash, verified_at, created_ip, invite_code) · `sessions` · `email_tokens` (hash,
purpose, expires, used_at) · `invites` ·
**`media`** (media_key PK, duration_ms, first_seen) · **`source_aliases`** (url, canonical_url, media_key,
first_seen, last_verified, superseded_by) · **`uploads`** (id, user, tmp_path, bytes, expires_at, media_key
once hashed) ·
**`analysis_runs`** (run_id PK, media_key, requested_recipe_id, achieved_capabilities JSON, status, user_id,
reservation_id, attempts, checkpoints JSON, requests, usd_e2, pricing_version, started/finished) ·
**`result_bundles`** (run_id PK, path, manifest_sha256, presentation_version, dominant bool) ·
**`run_subscribers`** (run_id, user_id, reservation_id, cancelled_at) — coalescing: a request for
(media_key, recipe) with a non-terminal run **attaches** instead of starting a new run; one subscriber
cancelling detaches and settles its own reservation; the run stops only when no subscribers remain ·
**`library_items`** (user, media_key, added_at, deleted_at; the library shows the media's dominant bundle) ·
**`publications`** (media_key, bundle run_id, published_by, published_at, takedown_at) ·
**`shares`** (token, bundle run_id, created_by, revoked_at) ·
`jobs` (id, run_id, target JSON, recipe_id, state, lease_owner, lease_until, heartbeat_at, attempt,
max_attempts=3, dead_letter_reason, cancel_requested, progress JSON, log_path) ·
**`credit_ledger`**, **`usd_ledger`** (§3.4) · **`provider_requests`** (provider, egress_id, day, count) ·
`entitlements` (user, plan, status, monthly_credit_anchor, period_end, cancel flags, stripe_customer,
stripe_subscription) · `entitlement_grants` (user, plan, granted_by, expires_at, reason) ·
**`orders`** (id, user, kind ∈ {pack, subscription}, stripe_checkout_session, payment_intent, status ∈
{pending, paid, failed, refunded, disputed}, credit_ledger_ref) · `processed_stripe_events` (event_id PK) ·
`admin_audit` (actor, action, target, at).

### 4.6 Jobs, retention, backups

Worker: lease + heartbeat (10 s), phase checkpoints, `attempt ≤ 3` then dead-letter, idempotent artefact
commits (bundle dir + pointer swap), attempt journal before any provider request, cancel = subscriber detach,
drain = stop leasing + finish running. **Retention:** on `complete|degraded|partial` — delete `windows/**`
immediately, `decode/audio.pcm` after 48 h, `ingest/original.*` after 7 d (hosted; local keeps it), rewriting
sidecars per §3.4; on `failed|cancelled|provider_unavailable` — delete `windows/**` and PCM immediately, keep
`original` 24 h, remove the media dir after 7 d if no bundle exists; history expiry (30/90 d) removes
`library_items` and GC deletes a bundle only when no library item, share or publication references it. Backups:
Litestream for `app.db`; nightly `idea backup-artefacts` snapshots `present/bundles`, `fuse/runs`, `hints`,
`recognise`, `ingest/source.json` + manifests to object storage **after** a DB checkpoint, writing a
`snapshot.json` that pairs the DB LSN with the artefact set; restore = DB first, then artefacts, then
`idea verify-artefacts`. Server: Hetzner CX/CPX (~€16/month); image `python:3.12-slim` + uv + ffmpeg + Deno +
`yt-dlp[default,curl-cffi]`, no JDK, no dev tooling; ffmpeg/ffprobe under `timeout` + cgroup memory limits;
uploads streamed to `uploads/<id>/` with byte/time/disk caps.

### 4.7 Billing (verifiable with no charge)

`BillingProvider` — `create_checkout_url(user, product, success, cancel)`, `portal_url(user, return)`,
`handle_webhook(raw_body, sig)`, `current_entitlement(user)`. `off`: admin grants only. `local`: `/dev/billing/*`
simulate pages (success, decline, async-success, renewal, failed renewal, cancel, refund, dispute), banner,
refuses `ENV=production`. `stripe`: Checkout `mode=subscription` (monthly/annual prices) and `mode=payment`
(packs) with `client_reference_id` from the session and `subscription_data.metadata.app_user_id`; Portal;
webhooks `checkout.session.completed`, `checkout.session.async_payment_{succeeded,failed}`,
`customer.subscription.{created,updated,deleted}`, `invoice.paid` (monthly credit), `invoice.payment_failed`
(notify), `charge.refunded`, `charge.dispute.created` (reverse the order's credit-ledger entries; flag
entitlement); raw-body signature; `event_id` dedupe; re-fetch on event; grant on `active|trialing`, revoke on
`canceled|unpaid`, 7-day grace on `past_due`; Stripe Tax on, GBP VAT-inclusive prices. `docs/billing-testing.md`
holds the exact `stripe sandbox create` / `stripe listen` / test-clock command sequence with expected
`entitlements` and `orders` rows after each step (written in 5b, asserted by its gate).

### 4.8 Hosted boundary

Allow-listed platform hosts or opaque upload ids; SSRF guard (private/reserved ranges, DNS rebinding, redirect
re-validation); untrusted filenames/MIME; result/export routes authorise by ownership/share/publication with
traversal/symlink checks; originals never served; no rescan route; results private by default; explicit
publish with takedown ownership and a DMCA route; one privacy sentence everywhere.

---

## 5. Phases (24 cycles)

Every cycle: Codex builds → `uv run pytest -q` · `uv run ruff check .` · `uv run ruff format --check .` ·
`uv run python scripts/audit_fixtures.py` green → Codex reviews the diff → fix → one commit. Gates name a
committed fixture and an exact command; owner-only checks are `live`-marked or PowerShell scripts.
**Fixtures created in 0a and reused everywhere:** `scripts/make_audio_fixtures.py` (deterministic; committed
outputs `tests/fixtures/audio/tone-60s.wav` — 60 s, 16 kHz mono s16, three tone "tracks" with 2 s crossfades —
and `tone-3600s.wav` generated on demand, not committed), and **test-mode provider injection**: when
`IDEA_TEST_MODE=1`, `idea analyse --fake-providers audd,shazam` installs `tests/fakes/FakeAudD` /
`FakeShazam` (scripted per-window outcomes from a JSON script path in `IDEA_FAKE_SCRIPT`) into `_analyse`'s
`paid_scan_adapters` and the injected Shazam client; without `IDEA_TEST_MODE` the flag is refused.

### Phase S — spikes and owner gates (parallel)
- **S1 (owner):** AudD production terms + per-clip rate (browser-read `/terms`, or email api@audd.io). → L1.
- **S2 (owner runs; Codex writes):** `scripts/spike_ingest_vps.sh` — on a throwaway VPS, for one authorised
  URL per platform, fetch direct and via `HTTP_PROXY` (owner supplies a residential proxy trial), 3 runs each,
  writing `docs/spikes/ingest-vps.md` (platform, route, success, bytes, seconds). Pass = SoundCloud and Mixcloud
  succeed direct; YouTube succeeds via proxy. Blocks 6a-i.
- **S3 (owner runs; Codex writes):** `scripts/spike_shazam_vps.py` — reuses `calibrate-shazam` against the
  fixture windows from the VPS for 60 min at 45 req/min ceiling; records req/min achieved, first 429, decode
  errors; repeat daily for 5 days. Output `docs/spikes/shazam-vps.md`. Sets `shazam_daily_budget_per_egress`.
- **S4 (Codex):** `docs/adr/0001-web-framework.md` — FastAPI vs Django/allauth, criteria: auth/CSRF/sessions
  out of the box, ASGI for the async pipeline, template port cost, admin. Decided before 4a-i.

### Phase 0a — Emergency correctness, spend, security (local)
E-C1, E-C4, E-H1, E-H2, E-H3, E-H8, E-H9, E-M5, E-M9, E-S3, U-F5 (loopback CSRF/Origin), test-mode injection.
- Bind `matches`; free failure count only when the free branch ran.
- Cache states (§3.4): write only `match|no_match`; read rejects others; attempt journal; `--refresh-states`.
- `run_paid_clip_recognition` → `PaidPassResult(requests, usd_e2, status, ambiguous)`; `max_paid_clips`
  honoured; `AppConfig.max_usd_e2` (**0 = no paid calls**; profiles' value ignored, documented) and
  `audd_usd_e6_per_request`; reservation arithmetic (§2.3 step 1) with cent ceiling; `_refuse_with`
  accumulates.
- Unavailable/erroring paid engine → exit 3 `provider_unavailable`; `--allow-degrade` → finish `degraded`,
  exit 0, status in `tracklist.json` and journal.
- Drop `upload_consent`; remove `run_paid_scanners` call site; `--engine acrcloud` → error "not supported";
  **wire `local_index_label` from the web runner (D3)**.
- CSRF token (per-server-process secret, 24 h) + Origin/Host check on loopback `/analyse`, `/rescan`; JSON
  clients send `X-CSRF-Token`.
- `_windows_in_spans` skips transformed windows.
- Fixtures + fakes as above.
- **Tests (`tests/test_phase0a_*.py`):** deep with fake AudD: success; all-no-match; all-errors → exit 3;
  `--allow-degrade` → `degraded`; partial errors → `partial`; cap cached-vs-live; count accumulation; error
  bodies never cached; spend journaled and capped; `max_usd_e2=0` refuses paid calls; CSRF/Origin rejects.
- **Gate:** `IDEA_TEST_MODE=1 uv run idea analyse tests/fixtures/audio/tone-60s.wav --recipe deep
  --fake-providers audd,shazam --work-root %TEMP%\idea-gate0a` exits 0; the journal shows
  `usd_e2_reserved == 5` (7 primary windows + secondary cap ⌈1 × 2⌉ = 9 clips × 0.5 ¢ = 4.5 ¢ → ceiling 5)
  and `usd_e2_spent == 4` (7 clips × 0.5 ¢ = 3.5 ¢ → ceiling 4). The test derives both numbers from the §2.3
  formula and the window schedule, not from literals.

### Phase 0b — Adapter reliability, effective config, provenance, cancellation
E-H4, E-H5, E-H6, E-M2, E-M10, E-S1, E-C3 (anchor).
- AudD clip parser: anchor from `timecode` (share the enterprise parser's helper), `simultaneous_source="audd"`,
  `adapter_version=2`.
- Paid pass: progress per clip → cancellable; concurrency 4 + token bucket + retries (1/2/4 s); **ambiguous =
  counted as spent, journaled, never auto-retried**.
- Config carry-over under profiles (both call sites); `idea config show [--profile X]` prints effective values,
  noting which come from the profile.
- Novelty guarded + hoisted. Shazam decode errors → limiter penalty + failure count; run status `degraded` when
  failures > 20 % of windows. Fuser receives the true scanned window set.
- **Golden Local Free:** `tests/golden/local-free/` generated by `scripts/make_golden.py` from `tone-60s.wav`
  with FakeShazam script `tests/fakes/scripts/golden-free.json`; comparison is **semantic** — `tracklist.json`
  entries minus provenance fields (`run_id`, timestamps, `generated_by`, `requested_recipe_id`,
  `presentation_version`).
- **Gate:** cancellation mid-paid-pass test (fake AudD with 200 ms latency; cancel after 3 clips; ≤ 4 clips
  journaled); retry test (fake 429 twice then success → one journal row per attempt, one billed); effective-config
  test; golden committed and green.

### Phase 1a — Recipes and result bundles
- `recipes.py` (§2.3 table), `recipe_id`; `RunRequest/RunResult` shapes (§4.3) inside `_analyse` (service
  module lands in 4a-i); bundle layout, manifest, `present/current` pointer; `refresh.py` writes new bundles;
  dominance rule; `requested` vs `achieved`; the status table (§2.3) with exit codes; `--recipe free|deep`
  replaces `--profile max_accuracy` in the web runner (CLI keeps `--profile` for the frozen free/legacy profiles).
- **Gate:** tests: a `deep` run then a `free` run leaves `current` on the deep bundle; a `free` run then a
  `deep` request reserves only the primary clips and reuses cached Shazam evidence (fake providers; assert
  FakeShazam received 0 requests); degraded never dominates; golden Local Free still green (semantic).

### Phase 1b — Targeting v2, corroboration, crowd rows
E-C2, E-C3 (trial source), E-M1, E-M3, E-M4, E-M6, E-L5, U-F13.
- Targeting per §2.3 step 4 with the exact allocation/overflow/ranking rules; reserve mechanics.
- Corroboration per step 5 (selected votes, overlap ≥ 6 s, families, display vs bypass thresholds);
  `discounted_providers` attributes by family.
- Per-process Shazam budget + failure-rate breaker → `degraded`.
- Crowd rows: `Artist – Title` parse required; no comment text; contradictory answers → best-supported listed,
  other as alternative.
- **Fixtures:** `tests/fixtures/deep/c3-scenario.json` (recorded, anonymised: 7 Shazam windows + 7 AudD
  agreements) → keeps `likely`, gains corroboration; `single-coincidence.json` → no floor bypass;
  `hint-only-first.json` → hint_only targeted before blanks; `overflow.json` (30 candidates, cap 12) → priority
  order respected; `duration-scaling` (fake 120-min mix) → cap 240. Owner-only `live`:
  `tests/test_live_deep_holly.py` (cached raws) reports ≥ 12 cross-checked and no `likely` lost.
- **Gate:** all above green; `docs/recipes.md` with the cost formula per recipe.

### Phase 2a — Startup and runtime cleanup (non-destructive)
- Lazy imports; `dev` extra; `_load_cached` via manifest + `work/index.json` (url → media_key) — no original
  required; `tl1001` default-disabled.
- **Gate:** `uv run python scripts/measure_startup.py` (runs `python -X importtime -c "import id_detector.cli"`
  3× cold, prints median) < 1.5 s on the dev machine, recorded in the commit message; cached-open test with the
  original deleted.

### Phase 2b — Retention, sidecar pruning, deletions
E-M8, E-S2.
- `pruned_upstream` sidecar scheme + verifier change + re-derivation; retention per §4.6 for all terminal
  states; `--keep-intermediates`; `idea gc --policy local|hosted`; manifest sizes.
- Delete `scan.py` whole-file path, ACRCloud, `tl1001`, their tests; tag `pre-v2-removals`.
- **Gate:** on `tone-3600s.wav` (generated in the test), `idea gc --policy hosted` leaves ≤ 25 MB per manifest;
  a Deep upgrade after pruning re-derives windows from PCM (test) and, after PCM expiry, re-fetches or fails
  `source_unavailable` (test with a local-file source removed); frozen profiles re-derive byte-for-byte; golden
  green.

### Phase 3a-i — Canonical projection and immutable presentation
U-F2, U-F3, U-F14.
- `present/projection.py`: one typed list (`TrackEntry | GapEntry`) → page, Copy, CUE, Markdown, JSON, card
  summary; exports written into the bundle; `refresh.py` = new bundle.
- **Gate:** `tests/test_projection.py` on `tests/fixtures/present/{garage,boiler,crowd}.json` compares typed
  entries across all five outputs (tracks and gaps separately); `PAGE_VERSION` bump.

### Phase 3a-ii — Honesty, accessibility, mobile
U-F4, U-F7, U-F8, U-F10, U-F11, U-F12, U-F13 (render), U-F19, U-F20, U-F31, version-column rule, run status.
- **Gate:** banned-strings test (`unverified`, `no_evidence`, `episode`, `prediction interval`, `suppressed`,
  `window`) on rendered HTML for the three fixtures; `scripts/check_page_js.py` (extracts inline scripts →
  `node --check`); axe-core run via `scripts/screenshot_pages.ps1` (owner) with 0 serious violations recorded
  in `docs/screenshots/v2/axe.json`.

### Phase 3b — Visual polish (not beta-blocking)
U-F16, U-F17, U-F21–F28, U-F35. **Gate:** page tests green; `docs/screenshots/v2/` refreshed.

### Phase 4a-i — Service API, packaging, web parity (loopback only)
- `id_detector/service.py` (§4.3) used by the CLI; `src/idea_web` packaged; FastAPI + Jinja templates ported
  1:1; routes parity; discriminated targets + SSRF-safe validation; `idea serve` → idea_web local mode;
  `PROJECT_ROOT` fix; `present/server.py` retired.
- **Gate:** route parity tests (`tests/idea_web/test_parity.py`) against the three present fixtures;
  `scripts/gate_local_mode.ps1` (owner: runs `idea.cmd`, opens a cached mix, asserts audio plays via the
  probe page) documented and run once; `uv run idea serve --no-open --port 8791` smoke test in CI.

### Phase 4a-ii — Server-side UI fixes, static assets, headers
U-F1, U-F15, U-F18, U-F29, U-F30, U-F34; CSP/security headers; gzip; cache headers.
- **Gate:** header tests; bad-URL test renders the form with the error and preserved fields; page weight
  ≤ 60 KB gzipped for the garage fixture.

### Phase 4b-i — Durable queue and worker
- `jobs` table, worker process, lease/heartbeat/checkpoints/attempts/dead-letter, cancel/drain, coalescing via
  `run_subscribers`, attempt-journal idempotency.
- **Gate:** kill worker after checkpoint `primary` → restart → run resumes at `hints` with 0 new AudD requests
  (fake); two users request the same (media, recipe) → one run, two subscribers; one cancels → run continues,
  their reservation settled; dead-letter after 3 failures.

### Phase 4b-ii — Progress, minimum operations, shared breaker
- Progress rows; wall-clock progress (U-F9) from per-phase medians in `jobs`; job-complete email hook;
  structured logs with redaction; paid-spend metrics; disk alarm; `/healthz`; **shared Shazam breaker over
  `provider_requests`**; restore drill `scripts/restore_drill.sh` (db + artefacts from a fixture snapshot).
- **Gate:** breaker test (budget exhausted → new free jobs `waiting`, running job ends `degraded`); restore drill
  passes; failed runs visible in the library (U-F33).

### Phase 4c-i — Users, sessions, passwords, CSRF
- **Gate:** session fixation, rotation, expiry, CSRF/Origin, rate-limit tests.

### Phase 4c-ii — Email tokens, verification, reset, invites, abuse
- Email provider interface (console dev-only), verification required, reset, invite codes, disposable list,
  Turnstile hook; XSS tests for titles/comments in rendered pages.
- **Gate:** hosted mode refuses console email; e2e signup → verify → login (console link in test).

### Phase 4d-i — Tenancy data model and ledgers
- §4.5 tables; `credit_ledger`/`usd_ledger` with `BEGIN IMMEDIATE` reservation; dominance-aware cache serving;
  Deep-on-Free delta; refunds; abuse ceilings; length caps; `orders` skeleton.
- **Gate:** 20 concurrent submissions against a 150-minute balance → exactly ⌊150/60⌋ accepted (test); refund
  on failure; Deep-on-Free reserves primary clips only.

### Phase 4d-ii — Library, sharing, publication, admin, usage UI
- Per-user library + delete; usage meter; capped-state prompt; `/s/<token>` + OG (behind `IDEA_SHARING`);
  explicit publish (behind `IDEA_PUBLIC_CATALOGUE`); `idea admin` (create-user, grant, revoke, jobs, gc,
  disable-user, delete-user) with `admin_audit`.
- **Gate:** isolation test; share opens logged-out only when enabled; publication explicit; admin actions audited.

### Phase 5a — Billing `off` / `local`
- **Gate:** simulate success/decline/async/renewal/failed renewal/cancel/refund/dispute through the UI with zero
  network; each step's expected `entitlements`/`orders`/`credit_ledger` rows asserted.

### Phase 5b — Stripe sandbox
- **Gate:** `docs/billing-testing.md` sequence executed by `tests/test_stripe_sandbox.py` (`live`-marked,
  needs `stripe listen`): 4242 → Pro credited; 0341 → renewal fails → `past_due` → grace → revoke (test clock
  +1 month +1 h); cancel at period end; pack sync and async (`4000 0000 0000 3220`-style async card) → credits
  only on paid; refund → reversal rows; webhook replay no-op; client price id ignored; live-mode guard.

### Phase 6a-i — Ingest hardening, upload fallback
- `ingest_policy.py`; upload path; SSRF guard; fetch-failure refund; ffmpeg/ffprobe limits; proxy budget.
- **Gate:** SSRF tests (private ranges, redirect to private, DNS rebinding stub); oversize/over-length upload
  refused; S2 result recorded.

### Phase 6a-ii — Container, backups, hosted-ready gate
- Dockerfile/compose (web, worker, Caddy), Litestream, `idea backup-artefacts`, secrets file, migration
  `down` test, `scripts/gate_hosted_ready.sh` (runs the 4a–6a gate suite inside the container and writes
  `IDEA_HOSTED_READY=1` into the env file only on success).
- **Gate:** clean-machine `docker compose up`; authorised SoundCloud source end-to-end inside the container;
  5-hour job refused; restart-during-job recovery; restore drill from backup.

### Phase 6b — Private-beta checklist
- Policy pages; attribution slot; `/admin`; runbooks (provider outage, disk, restore, drain); canary;
  `docs/LAUNCH.md`.
- **Gate:** `docs/LAUNCH.md` complete except L1–L5; beta config (§3.6) verified by `scripts/gate_beta_config.py`.

---

## 6. Owner decisions, launch gates, risks

### 6.1 Owner decisions (2026-09-09) — constraints, not questions

| # | Decision | Trade-off accepted / mitigation |
|---|---|---|
| **D1** | Hosted free tier runs on unofficial Shazam. Round-1 P0 #2 declined. | ToS exposure accepted; capacity ≈ 40–65 mixes/day per egress IP; §4.1 mitigations; engine roles config |
| **D2** | Deep scan = AudD sweep first, Shazam sampled second. | $0.80–2.00/hour; allowances from §3.3 after L1 |
| **D3** | Panako kept; web wiring fixed. | not in the hosted image until needed |
| **D4** | YouTube kept when hosted. | residential egress; proxy budget; S2 first |
| **D5** | Subscription and non-expiring packs both built. | beta runs with billing off (§3.6) |
| **D6** | Consumer product as acquisition layer; pro/report path kept open in the data model. | corpus funding is a later call |

### 6.2 Open questions (defaults set)

1. Free allowance 150 min/week; free mix cap 150 min; paid 240 min — config.
2. YouTube on the free tier — **decided (owner, 2026-09-09): (a) free tier accepts SoundCloud and Mixcloud
   links only (£0 per mix); YouTube links and file uploads are paid-tier features** (the proxy cost is paid
   from the customer's money; uploads are for recordings the user owns). Config: `free_sources =
   [soundcloud, mixcloud]`, `paid_sources = [soundcloud, mixcloud, youtube, upload]`. Rationale: none of the
   options is legally cleaner than the others (all platform fetching is ToS-tolerated, not licensed), so the
   choice is made on cost.
2b. Recorded option, not adopted: a downloadable desktop build ("free = run it on your own machine, your own
   Shazam quota") would move the Shazam load to each user's IP — exactly how the owner's local tool works
   today — at the cost of packaging/updating a Windows/macOS installer with ffmpeg + yt-dlp.
3. Whether density 2 becomes the Deep default after L1/L3 measurements.

### 6.3 Launch gates

| | Gate | Owner |
|---|---|---|
| **L1** | AudD production terms in writing (consumer/hosted use, caching/retention, attribution, concurrency, per-clip rate). **Blocks any hosted third-party AudD use, including the private beta.** | Nathan |
| **L2** | Legal review of ToS/privacy/DMCA and the ingestion posture; the scraped hint sources stay enabled pending it by D1/D4 | Nathan + solicitor |
| **L3** | **Real-mix release gate:** ≥ 5 owner-verified mixes, ≥ 3 different DJs, ≥ 2 platforms, total ≥ 4 h, two-pass truth via `idea truth seed/verify/second-pass/resolve/freeze`; scored with `uv run idea benchmark score --corpus <frozen> --recipe free|deep` into `docs/accuracy-report.md`; **thresholds:** LIKELY precision ≥ 0.90, listed-track precision ≥ 0.80, work recall ≥ 0.75 (`deep`) / ≥ 0.70 (`free`). No accuracy claim or "confirmed twice" marketing before it passes. | Nathan (+ tooling exists) |
| **L4** | Stripe business country, KYC, live keys, VAT position | Nathan |
| **L5** | Domain, email provider, restore drill passed on the real host | Nathan + 6a-ii |
| **L6** | Beta ≥ 2 weeks, invite-only, billing off, before `stripe` | — |

### 6.4 Risks

| # | Risk | L / I | Mitigation |
|---|---|---|---|
| R1 | Shazam blocks the server IP | medium / high | breaker, kill-switch, config flip to sparse AudD; Deep unaffected |
| R2 | YouTube blocks datacenter fetches | high / medium | residential egress; upload fallback; refunds; S2 |
| R3 | Only walk-up AudD rate | medium / medium | density 2; §3.3 table; USD ledgers |
| R4 | Weak willingness to pay; Beatport bundles ID | high / medium | tiny fixed costs; beta first; pro path hedge |
| R5 | Frozen-profile byte test | certain / low | recipes are runtime data; golden semantic comparison |
| R6 | Cycle too big | medium / low | 24 cycles ≤ ~1 day each |
| R7 | Disk fills | certain w/o 2b / high | retention for all terminal states; manifest gate; alarm |
| R8 | Paid run fails after spend | medium / medium | attempt journal; ambiguous counted; refunds; no blind retry |
| R9 | Free-tier abuse | medium / medium | ledgers; invites (beta); per-IP limits; budgets |
| R10 | AudD terms unknown | certain / high | L1 blocks hosted use |
| R11 | Throttled Shazam thin runs | certain today / high | E-S1 fix; `degraded` status; breaker |
| R12 | Free overwrites Deep | certain today / medium | bundles + dominance (1a) |
| R13 | Restart re-bills AudD | medium / medium | attempt journal + checkpoints (4b-i) |
| R14 | Backups inconsistent (DB vs artefacts) | medium / medium | paired snapshot + restore order (4b-ii, 6a-ii) |

---

## Appendix A — register

| Finding / required change | Where resolved |
|---|---|
| E-C1, E-C4, E-H1, E-H2, E-H3, E-H8, E-H9, E-M5, E-M9, E-S3, U-F5 (loopback) | 0a |
| E-H4, E-H5, E-H6, E-M2, E-M10, E-S1, E-C3 anchor | 0b |
| recipes, bundles, dominance, status table, Deep-on-Free | 1a |
| E-C2, E-C3 trial source, E-M1, E-M3, E-M4, E-M6, E-L5, U-F13 (fusion) | 1b |
| E-L1, E-M7, E-L4 | 2a |
| E-M8, E-S2, deletions | 2b |
| U-F2/F3/F14 | 3a-i |
| U-F4, F7, F8, F10, F11, F12, F19, F20, F31, version rule | 3a-ii |
| U-F16, F17, F21–F28, F35 | 3b |
| E-L2, E-H7 (paths), U-F6 render policy | 4a-i |
| U-F1, F15, F18, F29, F30, F34 | 4a-ii |
| E-H7 (queue), R13 | 4b-i |
| U-F9, U-F33, shared breaker, R14 | 4b-ii |
| U-F5 (hosted), auth | 4c-i / 4c-ii |
| ledgers, tenancy, U-F39 (behind flag) | 4d-i / 4d-ii |
| billing | 5a / 5b |
| R2, SSRF, uploads, container, hosted-ready | 6a-i / 6a-ii |
| policy, runbooks, beta | 6b |
| **Round-2 required changes** | 1 → 0a fixtures/injection + cancellation in 0b · 2 → §2.3 step 1, 0a · 3 → §2.3 recipes (deep-economy removed) · 4 → 1a · 5 → §2.3 dominance · 6 → L1/§3.3 · 7 → §2.3 step 2, 0b · 8 → §4.6, 2b · 9 → §2.3 recipe table + step 4/5 · 10 → §2.3 (`presentation_version`), 0b golden · 11 → §3.4 ledger · 12 → §4.5 · 13 → §4.3 service API, §4.5 subscribers · 14 → §3.3 (monthly credit), §2.3 status table · 15 → §5 (24 cycles) · 16 → §3.1 `IDEA_HOSTED_READY` · 17 → L3 · 18 → §3.6 |
| Round-1 P0 #2 | declined — D1 |

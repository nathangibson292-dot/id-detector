# IDea v2 — from a local tool to a product people pay for

*Plan rev 1 · 2026-09-09 · written from four independent adversarial reviews (engines/fusion, UI/product,
market, hosting/payments) run against `main` @ `27c36fd`. Review sources are summarised in §1; the
findings register in Appendix A maps every finding to the phase that fixes it. Codex review rounds
of this plan are logged in `docs/reviews/plan-v2-review-round-N.md`.*

---

## 0. The verdicts, in plain English

| Question Nathan asked | Verdict |
|---|---|
| **Is the business model right** (free capped per week, paid capped per month)? | **Yes, with two changes.** Caps are the right shape because the paid engine costs real money per mix. But (1) the *paid* cap should be on **Deep scans** (the thing that costs us), not on mixes generally, and (2) a mix that is **already in the cache** (someone analysed that link before) should be free for everyone and not count — it costs us nothing and it is the product's best growth lever. Numbers in §3. |
| **Could we charge for it?** | **Yes — after Phase 0–1.** Today the paid tier **crashes 100 % of the time after spending the money** (E-C1), destroys the one benefit it is meant to buy (E-C2/E-C3), has **no spend cap** (E-H1) and silently downgrades to free when the key fails (E-H3). Nobody can be charged for that. Fixed, and re-sequenced (§2.3), a Deep scan costs us **~$0.40** instead of **~$2.00** and is *more* accurate than the current paid path. |
| **Is it the best implementation?** | **No.** The pipeline core (windows → engines → fusion → episodes) is sound and well tested. The *paid-first* sequencing is wrong on every axis (cost, accuracy, speed). The web layer (stdlib `http.server`, one worker thread, in-memory jobs, no users) cannot be hosted as-is. |
| **Is the engine order right?** | **No → free engine first, then a *sampled* paid cross-check** (~80 clips split between "confirm the uncertain tracks" and "probe the blanks"), never a paid sweep. Details and numbers in §2.3. |
| **Does the UI work / show useless data?** | The look is genuinely good — keep it. But it shows **contradictory numbers** (library card "55 tracks" vs page "10 tracks"; three different tracklists from Copy / CUE / page), **advertises its own noise** ("56 matches hidden · show" → *Cher — Believe*), ships two columns that are **100 % constant across 305 tracks**, hides the buy links on phones, and speaks internal jargon (episode, prediction interval, suppressed, `no_evidence`). Full remove/change list in §2.5. |
| **What should be removed?** | The whole-file paid scan path + its consent gate, the ACRCloud adapter, the 1001tracklists connector, the web "build index" plumbing, the rescan button/route, the version/role columns, the hidden-matches reveal, the M3U export, the duplicate `/new` form, dead POST parameters. Quarantine (keep, but off the hot path): rescans, calibration ML, benchmark, truth, Panako, novelty. §2.6. |
| **Does it fill a genuine gap?** | **Partly — and not where we assumed.** "Paste a link, get a timestamped tracklist" is a commodity: **20 hosted competitors**, pay-per-mix prices around **$1**, subscriptions €6–$11/month, and every measurable traffic curve is falling (set79 −16 %/month, TrackSniff −14 %, 1001Tracklists −17 %). Two things IDea treated as differentiators — reading SoundCloud comments and leaving gaps blank — are already shipped by set79 and TrackRadar. What **nobody** ships: separating "right track" from "right recording", and an immutable, re-checkable evidence trail per run. Those matter to a different buyer (royalty/clearance reporting, label promo tracking) who pays tens of pounds per report. §2.7 and Open Question 1. |

**Cost of the plan:** seven phases (§5). Phases 0–3 make the *existing* tool honest, cheap and sellable
(all local, no new infrastructure). Phases 4–6 add accounts, quotas, Stripe (test mode) and the
hosting shape. Each phase is built, reviewed and committed on its own so the work can stop cleanly
at any phase boundary.

---

## 1. What was reviewed and how

Four independent Opus reviews, all read-only against `main` @ `27c36fd` (reports kept in the
session scratchpad; the decisive findings are reproduced here with their evidence):

| Review | Scope | Method |
|---|---|---|
| **E — engines & fusion** | `cli._analyse` sequencing, `paid_clip`, `scan_targeting`, `fuse/*`, `providers/*`, job runner, disk, tests | Code reading + `dis` bytecode verification of the crash + `du` measurements + test-suite timing |
| **U — UI / product** | Every screen of the web app | Live server on a spare port, headless-Edge screenshots at 1280/640/390 px, `node --check` on inline JS, data audit across all 305 tracks in `work/` |
| **M — market** | Hosted competitors, caps/metering patterns, engine pricing/terms, demand signals | Web research, cited |
| **H — hosting & payments** | yt-dlp/Shazam from a server, AudD terms, Stripe test-mode flow, framework, Docker sizing, abuse controls | Web research + code reading, cited |

The three most consequential engine findings were **re-verified by the orchestrating session by
reading the code** (`cli.py:796-811`, `fuse/alignment.py:85-174`, `providers/audd.py:350-365`,
`fuse/episodes.py:148-156`, `cli.py:985-1012`) before this plan was written.

The current offline test suite: **581 passed in 44 s** (93 deselected: `slow` + `live`).

---

## 2. Findings that decide the plan

### 2.1 The paid tier does not work (Critical, verified)

| ID | Finding | Evidence |
|---|---|---|
| **E-C1** | **`max_accuracy` crashes with `UnboundLocalError` after the money is spent.** `matches` and `recognised` are read at `cli.py:806/811` but only bound inside `if not paid_first:` (`cli.py:507-513`). Web path: AudD sweeps all ~400 windows (~$2), page is written, then the summary line raises → job FAILED, result never handed to the user. | `dis` shows `LOAD_FAST_CHECK`; no test sets `primary_engine`; the only full-pipeline tests are `slow`-marked and deselected by default. |
| **E-C2** | **Paid-first makes corroboration impossible by construction.** The free engine runs only over `select_gap_targets` = spans with *no* AudD observation, so the two engines never overlap and `_engine_corroborated` (`episodes.py:148`) can never fire — yet that flag is what lifts `possible` → confident, immunises against suppression, and exempts genuine short tracks from the 30 s floor. | `cli.py:663-671`, `scan_targeting.py:120-137` |
| **E-C3** | **Adding AudD *downgrades* tracks.** AudD observations carry `anchor=None` (`audd.py:361`) and no `simultaneous_source`, so they share the "primary" trial group with Shazam (`alignment.py:143-146`); the tie-break `_native_skew_cost` is 0 for AudD (no skew fields) so **AudD wins every contested window**, Shazam's anchored vote is marked `hypothesis_rejected`, `has_global` becomes False, and a clean `likely` track drops to `possible` (and may fall under the 30 s floor). | `alignment.py:85-94, 155-160`, `episodes.py:499-505, 534-550` |
| **E-C4** | **AudD error bodies are cached forever.** `paid_clip.py:180` writes the raw response *before* parsing; AudD returns HTTP 200 `{"status":"error"}` for out-of-credits/auth failures; there is no state check on the read path. Top up the account, re-run → 200 cached errors, zero AudD evidence, only `--refresh` (re-bills everything) escapes. | `paid_clip.py:170-190` vs `recognise.py:189-205` ("errors are never cacheable") |
| **E-H1** | **No spend cap, no spend record.** Paid-first passes `max_clips=len(windows)+1`; `usd_e2` is journaled as `0` unconditionally (`cli.py:820/850/864`); `AppConfig` has no USD field, so the frozen profile's `budget.max_usd_e2` never reaches the clip path. A 3-hour set = $6, uncapped, unrecorded. | `cli.py:495`, `scan.py:145`, `profiles.py:741-753` |
| **E-H2** | Gap-fill Shazam request counts are overwritten with 0 by `_refuse_with` → per-mix unit economics cannot be measured from `invocations.jsonl`. | `cli.py:649-655, 676` |
| **E-H3** | **Silent downgrade paid → free.** Missing/invalid key, exhausted quota, network outage all flip `paid_first=False` with only a progress-log line; nothing in the page/exports/journal says which tier ran. | `cli.py:499-506`, `audd.py:73-77, 435-436` |
| **E-H4** | The paid pass is **uncancellable** (no `progress()` call inside the loop → `check_cancel` never runs) and reports `windows_total=1` so the ETA says "1 s" for the whole 10-minute phase. | `paid_clip.py:161-197`, `jobs.py:211-221` |
| **E-H5** | The paid pass is sequential, unrated, unretried; a timeout drops the window with no cache entry → re-billed next run. | `paid_clip.py` vs `recognise.py:274-290` |
| **E-H6** | **Documented config knobs are silently ignored whenever a profile is active**, incl. `present_min_track_ms` (the main precision dial), `shazam_requests_per_minute`, `recognise_concurrency`, `collapse`, `same_track_bridge_ms`; `config show` prints them as effective. | `cli.py:990-1010`, `webapp/runner.py:57-72`, `config_template.py` |
| **E-H8** | `upload_consent` and `build_index` are still accepted from the POST body after removal from the form; `upload_consent=true` triggers the **whole-file enterprise scan in addition to** the clip scan — a second unbudgeted charge from an untrusted field. | `server.py:1373-1406`, `cli.py:527-547` |
| **E-H9** | The web "build reference index" builds a Panako index that the web analyse **never queries** (`local_index_label` is never passed). | `runner.py:130-172`, `cli.py:636` |
| **E-M1** | `engine_corroborated` counts *distinct provider strings*; AudD + ACRCloud (≈ one catalogue twice) would earn the confident tier. | `episodes.py:148-156` vs `COMMERCIAL_PROVIDERS` at `:92-108` |
| **E-M2** | Spectral novelty (full log-mel pass over ~115 MB PCM) runs on **every** fuse (1–3× per run) and its output is consumed by nothing with rescans off. | `orchestrate.py:140`, `novelty.py:174-193` |
| **E-M3** | `select_gap_targets` uses *any* coverage, so a single AudD phantom (12 s) removes that span from the free pass. | `scan_targeting.py:60-68` |
| **E-M8** | **Disk: 332 MB per 60-min mix, 77 % disposable** (147 MB window WAVs + 110 MB PCM); the 5 MB of raw provider JSON is the actual cross-user cache asset. `work/` = 4.9 GB for 10 mixes. Nothing reclaims. | measured |
| **E-L1** | `cli.py` eagerly imports `benchmark/`, `calibrate/`, `truth` → **2.1 s startup tax** on every command (3.17 s vs 1.04 s). | measured |

### 2.2 The web layer cannot be hosted as-is (verified)

- One in-memory worker thread, one job at a time; restart loses every job (`webapp/jobs.py`).
- No users, no sessions, no CSRF: **today any website Nathan visits can `POST /analyse` to
  `127.0.0.1:8765` and start a paid run** (U-F5). `validate_target` accepts any local path and the
  server serves it back over `/jobs/<id>/audio` — arbitrary file read the moment the host is not
  loopback (E-H7).
- `make_server` refuses non-loopback binds by design; HTTP/1.0, no TLS, no gzip, no cache headers;
  result page is 178 KB uncompressed with byte-identical CSS/JS inlined on every page (U-F29).
- `ProcessLock` raises instead of queueing → two users analysing the *same* mix (the cheapest case)
  breaks (E-H7).
- The result page streams the full downloaded mix from disk (`<audio src="../ingest/original.m4a">`)
  — fine locally, **redistribution of someone else's set + ~100 MB egress per view** when hosted (U-F6).
- `ensure_fresh_page` rewrites `index.html` on GET but not the exports → page and downloads drift
  (U-F3, U-F14).

### 2.3 The engine order is wrong — and the right one is cheaper *and* more accurate

**Why paid-first fails on every axis** (E-C2, E-C3, E-H1, plus: the "AudD is 4–6× faster" premise is
false — `paid_clip.py` is a sequential loop, ~400 × ~1.5 s ≈ 10 min vs Shazam's 9–20 min):

| Mode (60-min mix, 400 windows) | Shazam req | AudD req | $/mix | Cold time | Recall | Precision |
|---|---|---|---|---|---|---|
| free | 400 | 0 | $0.00 | 9–20 min | ~27/30 | good (30 s on-air floor) |
| free + AudD on uncertain (cap 150) | 400 | ≤150 | ≤$0.75 | +~4 min | ~27–28/30 | **best** — corroboration lifts `possible`→confident |
| **paid-first (shipped)** | ~120 | **400 uncapped** | **$2.00** | 13–17 min | **0 — crashes (E-C1)**; ≤27/30 once fixed | **worse than free** (E-C2, E-C3) |

*(AudD latency ~1.5 s/clip is unmeasured; the uncertain-span fraction ~40 % is an estimate. Everything
else is derived from code constants.)*

**Decision — one symmetric shape; which engine plays which role is configuration, not code:**

1. The **primary** engine sweeps the whole mix (or the content-addressed cache serves it — a link
   that was analysed before costs nothing). Sweep *density* is a config value implemented by
   subsampling the frozen 12 s / 9 s window set (`_subsample_evenly` already exists), so the frozen
   profiles and per-window cache keys are untouched.
2. Hints (comments / pasted tracklist) → fuse.
3. The **secondary** engine runs a *sampled, capped* cross-check over **uncertain-listed spans ∪
   blank spans** — never gaps-only (that is E-C2): ~half inside listed-but-not-confident episodes
   (corroboration — lifts real tracks, exposes phantoms), ~half inside blanks (recall), evenly
   spread, ≤ `max_secondary_clips` (default 80, ceiling 120). One clip every ~45 s is enough: a DJ
   track plays 3–6 min = 20–40 windows, and paying for 40 confirmations of the same track is waste.
   A per-run **USD ceiling** applies to whichever engine is paid.
4. **Re-fuse once.** The secondary engine is a **separate trial source** (`simultaneous_source`)
   so both engines' votes survive per-trial selection (E-C3); corroboration reads the *evidence*
   bucket, not post-selection votes; the two providers must come from different families
   (`COMMERCIAL_PROVIDERS` vs Shazam/Panako/crowd) (E-M1).

| Mode | primary (sweep) | secondary (sampled) | AudD req | $/60-min mix (subscription – walk-up) | Notes |
|---|---|---|---|---|---|
| **Local Free** | Shazam, full (400) | — | 0 | $0 | as today |
| **Local Deep** | Shazam, full | AudD ≤ 80 | ≤ 80 | **$0.16 – 0.40** | replaces paid-first; +~2 min |
| **Hosted Free** | AudD, sparse (1 clip / 30 s ≈ 120) | — | ~120 | **$0.24 – 0.60** | licensed; boundaries ±30 s; single-clip phantoms still fall under the 30 s floor; validate on the benchmark mix |
| **Hosted Deep** | AudD, half density (every 2nd window ≈ 200) | Shazam ≤ 120, kill-switchable | ~200 | **$0.40 – 1.00** | Shazam load ÷3 vs a full sweep → ~200 mixes/day per egress IP instead of 65 |
| **Contingency** (Shazam off/blocked) | AudD | — | as above | as above | −10–25 % recall on underground tracks; crowd hints still apply; state shown on the page |

**Why the hosted roles flip:** the hosting review (§4.1) measured what a server can do with the
unofficial Shazam route — ~20 req/min *per IP, shared by every user* → **~65 hour-long mixes per day
for the whole service**, and past that limit the endpoint returns malformed JSON that the adapter
reads as "no match", so **accuracy degrades silently before throughput does**. A hosted product
therefore cannot sweep with Shazam; it can only afford Shazam as a bounded, kill-switchable secondary
(and must treat malformed bodies as a throttle signal, never as evidence).

**Result:** Local Deep at ~$0.40 instead of $2.00 (−80 %) with the precision mechanism *restored*;
hosted tiers fully licensed at $0.24–1.00 per hour of audio. Margin check: at these COGS a £7/month
plan is comfortable at the AudD subscription rate and survivable at walk-up; $2.00 uncapped never was
(one power user × 20 long sets = $120/month).

**Why not ACRCloud as the second paid engine:** live-tested ≈ AudD's coverage (recovered 1/8 of
AudD's misses), same underground blind spots, and it is the main way corroboration can be faked
(E-M1). Removed (§2.6).

**Why not Panako:** 0 tracks recovered on 2 real mixes; only helps when a DJ plays their *own*
indexed uploads. Quarantined to the CLI; web plumbing removed.

### 2.4 Accuracy: where the real edge is

On Nathan's 30-track benchmark Shazam alone caps at ~27/30; the 3–4 misses are tracks in **no
commercial catalogue** (AudD and ACRCloud miss them too). The only lever that recovers those is the
**SoundCloud comment answers** (`hint_only` "from comments" rows). Two engines agreeing is the
strongest *precision* signal. So the product story is honest: *"we find what the catalogues know,
we read what the crowd knows, and we tell you how sure we are"* — not "AI finds everything".

Two accuracy bugs to fix alongside: crowd rows can render raw comment text as a track with a
POSSIBLE badge (`I like the way you talk — Dave era`, U-F13), and contradictory crowd answers at the
same timestamp both get listed (E-M4).

### 2.5 UI: remove / change / add (from review U; screenshots verified)

**Remove (dead or harmful):** the "N matches (46 short, 10 suppressed) hidden · show" counter+toggle
(U-F7); `Version` and `Role` columns — `unverified` / `incoming|dominant` on 305/305 rows (U-F8);
the per-row `rescan` button, `ops` column and `POST /rescan` (U-F11); the `HINT` pill (loudest
element, least meaning); `layer`/`outgoing` role tags (1 in 305); the `ID gap` tile (0 or 1 on every
mix); the "9h 03m of music listened to" vanity stat; the M3U export (every entry `#EXTINF:-1` to the
same page URL — dead); the duplicate `/new` form (keep the route only as the "try again" landing);
the false footer "nothing leaves 127.0.0.1" (U-F12); the `no_evidence` token and "suppressed" /
"buried under a surer track" strings; the `build_index` + `upload_consent` POST params.

**Change:** bad URL → inline error with the form preserved, never a raw JSON page (U-F1); **one
source of truth for track counts and exports** — card count, page, Copy, CUE, MD, JSON all from the
same filtered list, exports regenerated whenever the page is (U-F2/F3/F14); keep the "Where to get
it" column on phones, stack rows as cards ≤ 720 px, thin the ruler labels (U-F4); prefer a known
`purchase_url` over a store *search*, label search fallbacks, hide the column when a mix has no
acquire data (U-F10); wall-clock-proportional progress (phase weights from measured medians, recent-
rate ETA, U-F9); failure copy with plain cause + remedy, failed step marked, explicit "no credit
used / 1 credit used" (U-F15); one confidence word per row + one-line glossary under the Tracklist
heading; legend cut from six terms to two (U-F16/F17); `--dim` → ≈`#8b8ba6` for AA contrast; drop
`role="button"` from `<tr>`, add `scope="col"` (U-F19/F20); `profile free` chip → "Free scan" /
"Deep scan"; three stat tiles only (tracks · length · identified %) with the confidence mix
captioned under the Tracklist heading (U-F21); "other versions" only when a genuinely different
track (U-F25); lead-in spinner behind a details (U-F26); options summary reflects the pasted
tracklist (U-F28); in-progress cards use the resolved title + friendly step names, running first
(U-F18); no 5-s auto-redirect (U-F30); crowd rows never render comment text and never carry a badge
(U-F13); one true privacy line everywhere.

**Add:** analysed date on cards and page header; share link (`/s/<token>`, read-only, OG tags) —
the only viral surface a tracklist product has (U-F39); per-user library with delete (hosted
phases); "you can close this tab" on the progress page; a visible record of failed runs (U-F33).

**Keep (it's good):** the club-mode theme, equaliser logo, scanner strip + flavour text, mix cards,
`prefers-reduced-motion`, NOW pill, CUE/Markdown/JSON exports, the paste-a-tracklist box (promote it).

### 2.6 Remove vs quarantine (subsystems)

| Subsystem | Decision | Why |
|---|---|---|
| Whole-file `scan.py` path, `run_paid_scanners` call site, `require_upload_permission` gate, `upload_consent` param | **Remove from the shipped path** | Superseded by the clip path; active liability (E-H8). Keep the gate function only if a genuine owner-upload feature ships later. |
| `providers/acrcloud.py` (840 lines) + `test_acrcloud_clip` + `_Bundle` branch | **Remove** (archive to a git tag) | Redundant with AudD; corroboration-gaming vector. |
| `hints/connectors/tl1001.py` | **Remove** (or default-disable) | 1001tracklists is JS-gated; emits only quarantined pointers nobody confirms. |
| Web `build_index` plumbing (`runner._run_build_index`, POST param) | **Remove** | Builds an index the web never queries (E-H9). |
| Rescans + transform grid, `rescan.py`, `orchestrate` loop | **Quarantine** (frozen legacy) | Profiles certify `rescans=3` from the synthetic corpus and must stay byte-identical; off by default; no new work. |
| `calibrate/`, `benchmark/`, `truth.py`, `local_fixture.py`, `pointer_import` | **Quarantine** — lazy import + a `dev` extra / separate entry point | Zero value on real mixes today; re-freezing profiles needs `benchmark/`. Removes the 2.1 s startup tax. |
| Panako (`providers/panako*.py`, `local_index.py`, `candidates.py`) | **Quarantine to CLI** | One real use case (a DJ analysing their own set). |
| `novelty.py` | **Guard** with `max_generations > 0`, hoist out of the re-fuse loop | E-M2. |
| `sc_comments`, `manual` tracklist, `mixesdb`, `yt_comments`, `mixcloud` hints | **Keep** (invest in `sc_comments`) | The differentiator. Hard-timeout `yt_comments` when hosted. |
| CUE / Markdown / JSON exports | **Keep** | Real DJ formats. M3U removed. |

### 2.7 Market: where IDea can and cannot win (review M — full report in `docs/research/05-market-2026-09.md`)

**The consumer segment is crowded, cheap and shrinking.** Verified September 2026 prices:

| Tool | Paid | Free | Metering | Engines |
|---|---|---|---|---|
| set79 | $10.90/mo unlimited; $8 / 3 sets | browse 70,000 cached sets, **0** new analyses | per set | fingerprinting + **reads SoundCloud comments**, leaves gaps blank |
| setlist.id | $6 / $15 / $25 per month (3 / 8 / 15 sets) | 2-set trial, card required | per mix (> 4 h = 2) | ACRCloud |
| ryser.id | €6 / 300 min · €12 / 800 min · €29 / 2,500 min + API; packs never expire | **120 min/month** | **1 credit = 1 minute** | two engines + tempo-warp rescue; **sells royalty reporting** to PRS/GEMA/SACEM |
| TrackSniff | $9 / $19 per month | 50 scans/month but **only the first 3 tracks shown** | per scan | five confidence tiers, BPM/key |
| TrackRadar | $4.99 / 10 credits, never expire | 3 analyses/month, first one without an account | per analysis / per track found | **AudD → Shazam → ACRCloud**, comments + description |
| djtracks.io / TracklistAI / IDThisMix | **$0.99 – $1.40 per mix** | first mix free, full result | per mix | ACRCloud / undisclosed |

Traffic: set79 ~80 K visits/month (−16 % MoM), trackid.net ~41 K, TrackSniff ~12 K (−14 %); 1001Tracklists −17 %.

**Demand, verified first-hand (Reddit/HN, Sept 2026):** the *want* is large and old — a 445-upvote r/DJs
thread ("so many bangers that Shazam couldn't pick up"), MixesDB's own stats (366,257 mix pages, **21.9 %
with no tracklist, 37.8 % incomplete**), SoundCloud reporting **#DJSET uploads +39 % YoY**. The loudest
technical complaints are exactly what IDea is built for: edits/flips/bootlegs resolving to the original
("Shazam only works for official remixes"), pitch-shifted tracks, and a user literally writing our brief
("output the timestamp… anticipating numerous identification errors… an approximate tracklist enables
quick validation"). But **willingness to pay is weak and partly hostile**: nobody in any thread named a
price they would pay; the free tool Set2Tracks (371 upvotes) shut down because *"this is the kind of tool
people love to find for free but wouldn't pay for"*; trackid.net going paid drew "nothing free now";
the Beatport Track ID thread called commercialising trainspotting "fucking wild". The free incumbent
(1001Tracklists) does **78.9 M visits/year** with no paid tier, and **Beatport Track ID** (May 2026,
10 M registered users, owns the buy links) now markets pitch-shift and edit/remix handling — our
differentiators — from inside the app DJs already use.

**What the market has learned (copy these):** meter **minutes of audio**, not mixes (the only unit that tracks the API bill; a 4-hour set otherwise costs us 4×); **never cripple the free result** (TrackSniff's "first 3 tracks" and VerifAI's "first 30 minutes" pay the full API cost and convert worst); **non-expiring credit packs** are mandatory (most demand is "one tracklist, once"); **refund on failure** is table stakes; the **cached public catalogue is the real business model** — set79's free tier is its 70,000 already-analysed sets, and 74.5 % of its desktop traffic is organic search into those pages.

**Unit costs (verified):** AudD counts 1 request per 12 s of audio; **$5/1,000 walk-up = $1.50 per hour** scanned densely; **"subscription options start at $2 per 1,000" = $0.60 per hour**. Sparse sampling (1 clip / 30 s) = $0.24–0.60 per hour. So a *fully licensed, paid-engine-only* IDea is affordable — a 400-minute monthly allowance costs ~$4 of AudD at the subscription rate. ACRCloud's pricing is not public and its standard terms (§13 internal use only, §14 non-compete) do not obviously permit consumer resale. **There is no legal server-side Shazam**: ShazamKit is on-device and its licence forbids both "creating another audio recognition service" from its data and any product "marketed for compliance purposes (e.g. music licensing and royalty auditing)". No enforcement against shazamio or any hosted tracklist service was found — but absence of enforcement is not a licence, and hosted competitors (TrackRadar, trackid.net) openly run the same unlicensed route.

**The honest strategic read.** IDea's engineering is *better* than the category (calibrated two-axis confidence, provenance, corroboration) but its consumer feature set is *behind* it (not hosted, no BPM/key, no share pages, no cached catalogue, no playlist push). Entering as competitor #21 at $1/mix with a thinner legal footing is not a business. The two things nobody else has — work-vs-version separation and audit-grade evidence — are what a **royalty / clearance / label-promo-tracking** buyer needs, and ShazamKit's terms mean nobody building on Apple can follow. That niche needs the *certified* tiers (the frozen real-mix corpus Nathan has not yet funded — standing decision 1 in `docs/STATUS.md`).

**What this plan does with that:** it does not pivot the build. Phases 0–6 are needed for *either* direction (a working paid path, honest UI, accounts, minute-based quotas, Stripe, hosting). It makes two things configurable rather than baked in — which engine powers the hosted free tier (§3.2) and whether the version axis is shown (§2.5, kept in the JSON/report path) — and it puts the positioning choice to Nathan as Open Question 1 with a recommendation.

---

## 3. Product definition v2

### 3.1 Two modes, one codebase

- **Local mode** (what Nathan runs today via `idea.cmd`): unchanged behaviour — loopback, no
  accounts, local file paths allowed, local audio player. Every phase must keep this working.
- **Hosted mode** (`IDEA_MODE=hosted`): accounts required, quotas enforced, local paths refused,
  platform embed instead of served audio, share links, Stripe. Same pipeline, same artefact tree.

### 3.2 Scans

| | Free scan | Deep scan |
|---|---|---|
| Engines (local) | Shazam full sweep + crowd hints | Free scan **+** ≤ 80 sampled AudD clips + re-fuse |
| Engines (hosted, default) | sparse AudD sweep (1 clip / 30 s) + crowd hints | half-density AudD sweep + ≤ 120 sampled Shazam clips (kill-switchable) + re-fuse |
| What the user sees | LIKELY / POSSIBLE / UNCLEAR badges, "from comments" rows | the same, plus **"confirmed twice"** tags where the engines agree; short real tracks rescued from the floor; tighter boundaries |
| COGS per hour of audio | local $0 · hosted $0.24–0.60 | local ~$0.40 · hosted $0.40–1.00 |
| Kill-switch | `IDEA_ENGINE_SHAZAM=off` pauses local free scans with an honest message; hosted free scans are unaffected | keeps working without Shazam (contingency, ~−10–25 % recall on underground tracks) |

Engine roles per tier are settings (`engine_primary`, `engine_secondary`, `primary_density`,
`max_secondary_clips`, `max_usd_e2`), so the hosted defaults above can be changed without a code change.

### 3.3 Tiers and caps

Metered in **minutes of new audio analysed** (1 credit = 1 minute). All numbers are config values, not code.

| | **Free** | **Pro** (subscription) | **Packs** (one-off) |
|---|---|---|---|
| Price | £0, no card | **£7 / month** (£59 / year) | **£6 / 300 min**, £20 / 1,200 min — never expire |
| Allowance | **120 new minutes / month** (≈ 1–2 sets; Nathan's "1 mix a week" ≈ 240 — a config number) | **400 Deep-scan minutes / month** + standard scans to a fair-use ceiling of 1,500 min | minutes, Deep scan |
| Scan type | Standard (Free scan) | **Deep scan** on every mix | Deep scan |
| Results | **Full tracklist, always** — never a preview | full + "confirmed twice" tags + exports | as Pro |
| History | 30 days | unlimited | 90 days |
| Queue | normal | priority | normal |
| Cached mixes | free, unlimited, don't count | free, unlimited, don't count | same |
| Failure | minutes refunded | minutes refunded | minutes refunded |

COGS check (60-min mix): Free on Shazam = $0 API; Free on sparse AudD = $0.24–0.60; Deep = ~$0.40 (Shazam
+ 80 clips) or ~$0.65–1.10 (AudD-only: sparse sweep + 80 targeted clips). At the AudD **subscription**
rate a fully-used £7 Pro month costs ~$3–4.50 of API → ≥ 50 % gross margin before the cache absorbs
repeats; at the walk-up rate the margin is thin, so **the AudD subscription rate must be agreed before a
price is published**. "Unlimited" is not offered until the public cached catalogue is live.

**Later tiers (not built in this plan):** Studio £18 / month (1,500 min + API) and a **Pro Report** tier
(£39 / month or £8 per certified report) gated on frozen-corpus certification — the only tier with no
direct competitor.

**Rules that make the caps fair and cheap to run:**
- A mix is one analysis of one link. **A link that is already in the cache is free for everyone and
  does not count** — the content-addressed cache means it costs nothing, and "re-running is free" is
  the best possible marketing line. (Deep-scan results are cached too; a cached Deep result is shown
  to a free user with the confirmed-twice tags — it's the upsell.)
- Quotas count **new analyses started**, per account, per ISO week (free) or calendar month (Pro).
  A failed run (download blocked etc.) refunds the count.
- Deep scans are the metered unit on Pro; standard scans on Pro have a generous fair-use ceiling
  because they still consume Shazam IP budget.
- Hard per-run USD ceiling regardless of tier; per-account monthly USD ceiling for Pro.

### 3.4 Sign-in and payments (decided)

Email + password (argon2), email verification deferred, sessions in signed cookies, CSRF on every
POST. Stripe **test mode only** for now: Checkout (subscription) → webhooks → Billing Portal; plus a
**billing-off mode** (`IDEA_BILLING=off`) with `idea admin grant <email> pro` so the whole site is
verifiable without a Stripe account or a card. Live keys are a config change, not a code change.

---

## 4. Hosted architecture (review H — full report in `docs/reviews/v2-review-hosting.md`)

### 4.1 Showstopper checks (done)

| Check | Finding | Consequence in this plan |
|---|---|---|
| Unofficial Shazam from a server | ~20 req/min **per IP**, shared by all users → ~65 hour-long mixes/day per IP; past the limit: malformed JSON parsed as "no match" (silent accuracy loss); datacenter ASNs escalate to blocks; Apple's terms forbid automated access; no server-side Shazam API exists at any price | never the hosted primary (§2.3); bounded secondary with global daily budget, circuit breaker, malformed-JSON = throttle; kill-switch; **no residential proxying of Shazam** |
| yt-dlp from a server | SoundCloud and Mixcloud fetch fine from a VPS; **YouTube** needs residential egress (~65 MB per mix → $0.15–0.50 at Webshare/IPRoyal rates) plus Deno + `curl-cffi`; the whole category runs "you assert the rights, we don't re-host, audio is discarded" | per-platform egress setting; upload fallback; refund on fetch failure; YouTube switchable off (Open Q 2b) |
| AudD terms | pricing public ($5/1,000 walk-up; "$2/1,000" subscription); explicitly lists DJ sets as a use case; **production ToS unreadable, trial licence requires attribution and forbids commercial use** | R10 — Nathan emails AudD; attribution slot in config |
| Disk | 330–370 MB per hour of audio, of which **10–25 MB is the durable result** | retention policy (§4.6) is mandatory |
| RAM | `novelty.py` materialises the whole mix as float64 (~0.7 GB per hour, 2.1 GB for 3 h); ffmpeg/windowing stream | Phase 2 turns novelty off when rescans are off; budget 1.5 GB per job; mix-length caps |

### 4.2 Shape

```
Internet ──:443──▶ Caddy (auto-TLS, gzip, 250 MB body cap) ──▶ uvicorn · idea_web (FastAPI)
                                                                 │ pages, auth/sessions, CSRF, quota,
                                                                 │ billing provider, POST /analyse → jobs row,
                                                                 │ GET /jobs/{id}/status (poll 2.5 s),
                                                                 │ serves work/**/present/* behind ownership/share checks
                                                                 ▼
                                                            app.db (SQLite, WAL; Litestream → object storage)
                                                                 ▲ lease / heartbeat / progress
                                                            idea_web.jobs.worker (separate process, N=1 to start)
                                                                 │ ingest policy (egress per platform / upload)
                                                                 │ id_detector.cli._analyse  ← UNCHANGED pipeline
                                                                 │ retention GC
                                                                 ▼
                                                            work/<source>/<media>/  (content-addressed cache, ~20 MB durable per mix)
```

Rules: the web process never runs a pipeline; the worker never serves HTTP; billing touches the app
through exactly four methods; `src/id_detector/` is changed only by Phases 0–3, never by the web work.

### 4.3 Web layer: FastAPI + Jinja2, one app for both modes

Migrate off `http.server` (it refuses non-loopback binds by design and has no sessions, CSRF,
multipart, streaming, or DI). `src/idea_web/` (settings · app · db + SQL migrations · auth/ · billing/ ·
quota · jobs/{store,routes,worker} · ingest_policy · retention · ratelimit · routes/ · templates/ ·
static/). **`idea serve` launches the same app in local mode** (`IDEA_MODE=local`: no accounts,
loopback bind, local file paths allowed, local audio served) so there is one web layer to maintain;
`present/server.py` is retired once parity tests pass. `present/page.py`, `theme.py`, `exports.py`
keep rendering artefacts; the CSS/JS they inline moves to static files served with cache headers
(U-F29). Progress stays polling (survives reloads/proxies); SSE is not needed.

### 4.4 Accounts, sessions, abuse controls

- Passwords: `argon2-cffi` defaults (exceed OWASP minimums), `check_needs_rehash` on login.
- Sessions: **server-side** table (a signed cookie goes stale the moment a webhook changes the plan);
  cookie `__Host-idea_session; HttpOnly; Secure; SameSite=Lax`; store `sha256(token)`; rotate on login
  and plan change; 30 d idle / 90 d absolute; "log out everywhere" = delete rows.
- CSRF: synchroniser token on every state-changing POST; `/webhooks/stripe` exempt (signature-authenticated).
- Email: provider interface (Postmark/Resend/SES later); until configured, `IDEA_EMAIL=console` prints
  verification/reset links to the log so the flow is testable with no external service. Email
  verification is required before any quota is granted.
- Abuse: disposable-email domain list at signup (soft fail); per-IP limits (signup 3/day, 10/week;
  analyse 5/hour incl. cached); per-account minute quota; mix-length caps (90 min free, 4 h paid,
  > 4 h counts double); optional Turnstile on signup/free submit; export endpoints rate-limited
  tighter than pages; `created_ip` stored for retro-bans.
- Cache hits: a link already in the library costs nothing and **does not count** against minutes,
  but is counted separately and rate-limited (20/day per account, 50/day per IP) so the library
  cannot be scraped for free. Byte-identical audio under a new URL still costs one fetch — counted
  against a bandwidth budget, not minutes.

### 4.5 Data model (SQLite, WAL, migrations in SQL files)

`users` · `sessions` · `email_tokens` · `mixes` (media_key, source_key, owner, title, duration_ms,
visibility public|private, share_token, created_at) · `jobs` (queue: status, lease, heartbeat,
progress JSON, log, user, mix, engines requested/ran, usd_e2) · `quota_usage` (user, period key
`YYYY-Www` / `YYYY-MM`, new_minutes, deep_minutes, cached_hits, fetch_bytes) · `subscriptions` (user,
plan, status, **source stripe|admin**, period_end, cancel flags, stripe customer/subscription ids) ·
`credit_packs` (user, minutes remaining, purchased_at) · `processed_stripe_events` (event_id PK) ·
`paid_spend` (job, provider, requests, usd_e2). One worker process to start (`N` configurable);
`recover_startup()` requeues jobs with stale heartbeats — the lease/heartbeat pattern is copied from
`src/id_detector/jobs.py`, which already implements it correctly.

### 4.6 Retention, sizing, cost

On job success: drop `windows/**` immediately; keep `decode/audio.pcm` 48 h; keep `ingest/original.*`
7 days (the local player uses it; hosted pages use the platform embed after that); keep `present/`,
`fuse/`, `hints/`, `recognise/`, `ingest/source.json` forever (~20 MB per mix — the cross-user cache
asset). `idea gc` runs this and a disk alarm. Server: one Hetzner CX/CPX-class VPS (~€16/month,
persistent disk; **not** Fly/Railway — ephemeral/slow disks, request timeouts on 30-minute jobs).
Image: `python:3.12-slim` + uv + ffmpeg + Deno + `yt-dlp[default,curl-cffi]`; **no JDK**;
`benchmark/`, `calibrate/`, `truth`, `local_fixture` excluded from the production image. Backups:
Litestream of `app.db` to object storage; `work/` is reproducible from source URLs. Rough monthly cost:
**$65–75 at 100 mixes, $370–550 at 1,000** — dominated by AudD and (if kept) YouTube proxy bandwidth,
not the server.

### 4.7 Billing (Stripe, test-mode only, verifiable with no charge)

`BillingProvider` protocol — `create_checkout_url(user, success, cancel)`, `portal_url(user, return)`,
`handle_webhook(raw_body, sig)`, `current_plan(user) → PlanState` — **no price id ever comes from the
client**. Two implementations behind `BILLING_BACKEND=none|stripe`:

- **LocalBilling** (`none`): `/dev/billing/*` pages with *Simulate successful payment / decline /
  renewal / failed renewal / cancel* buttons that write the same `subscriptions` rows Stripe would;
  persistent "Billing simulated" banner; refuses to boot when `ENV=production`. This makes the entire
  site (pricing, upgrade prompts, gates, account page) testable with zero network and no Stripe account.
- **StripeBilling**: Checkout Session `mode=subscription` (Pro) and `mode=payment` (packs), created
  server-side with `client_reference_id` from the session and `subscription_data.metadata.app_user_id`
  (session metadata does not propagate); Billing Portal for cancel/update; webhooks
  `checkout.session.completed`, `customer.subscription.{created,updated,deleted}`,
  `invoice.payment_failed` (notify, don't revoke — Smart Retries); signature verified over the **raw
  body**; `processed_stripe_events` dedupe by `event_id` (never by `created`); **re-fetch the
  subscription on every event** so stale events cannot clobber state; grant on `active|trialing`,
  revoke on `canceled|unpaid`, 7-day grace on `past_due`; unique constraints make the success page
  and the webhook safe to race. `stripe listen --forward-to` for local; test cards (4242 success,
  0341 fails at renewal, 3155 3-DS); test clocks for renewals; `stripe sandbox create` gives working
  test keys with no account (accept the `rkcs_test_` prefix). `idea admin grant <email> pro --days N`
  writes `source=admin` rows that Stripe events never revoke.

### 4.8 Hosted-mode rules (enforced by `IDEA_MODE=hosted`)

Refuse local file paths; never serve `ingest/original.*` (platform embed only); no rescan route;
result pages readable by the owner or via a share token; mixes analysed from public links are listed
in the public library (the set79 model — the acquisition and SEO layer), uploads are private; one
privacy sentence everywhere: *"We fetch the audio to analyse it and delete it within 7 days; only
short clips go to the recognition engines; the tracklist is kept."*; ToS/privacy/DMCA pages drafted
for a solicitor's review (Open Q 5); "Powered by AudD" attribution slot pending R10.

---

## 5. Phases

Every phase: Codex (gpt-5.6-sol, xhigh) builds from this plan → `uv run pytest -q`, `uv run ruff
check .`, `uv run ruff format --check .`, `uv run python scripts/audit_fixtures.py` all green → Codex
reviews the diff → fixes → one commit on `main` per phase. Local mode must keep working at every
boundary (`uv run idea serve --no-open` + a cached mix opens and plays).

### Phase 0 — Make the paid path correct, capped and measurable
**Scope:** E-C1, E-C4, E-H1, E-H2, E-H3, E-H4, E-H5, E-H6, E-M5, E-M9, E-M10, E-H8, E-H9.
- Bind `matches` from `gen0_observations`; report free-engine failures only when the free branch ran.
- Cache AudD raw responses only after a successful parse; the read path rejects non-success bodies.
- `run_paid_clip_recognition` returns `requests`, `usd_e2`, `status ∈ {ran, unavailable(reason), no_matches}`; honour `max_paid_clips` in every mode; add `max_usd_e2` to `AppConfig` (profile budget flows through); journal the real spend; `_refuse_with` accumulates instead of overwriting counts.
- Paid pass: progress callback every clip (cancellable), bounded concurrency (3) + token bucket + the same retry/backoff Shazam has; ambiguous outcomes are *not* re-billed blindly (record an `ambiguous` sidecar; retry once next run).
- Tier that actually ran is written into `present/tracklist.json` (`engines_run`, `paid_status`) and shown in the page header; an unavailable paid engine **fails the job** with a plain message instead of degrading silently (local CLI keeps a `--allow-degrade` flag).
- Carry `present_min_track_ms`, `collapse`, `same_track_bridge_ms`, `shazam_requests_per_minute`, `recognise_concurrency` through the profile `replace(...)` in both `cli.analyse` and `webapp/runner._resolve_settings`; `config show` reports the effective values.
- Drop `upload_consent`/`build_index` from the POST handler and the web runner; remove the `run_paid_scanners` call site from `_analyse`.
- **Tests:** one fast `_analyse` integration test per mode (free / deep / degraded) with a fake AudD adapter through the existing `paid_scan_adapters` injection point and a tiny synthetic WAV; a test that an AudD error body is never cached; a test that spend is journaled and capped; a config carry-over test.
- **Gate:** the deep-mode integration test passes end-to-end and reports `usd_e2 > 0`.

### Phase 1 — Re-sequence: Deep scan = sampled corroborate-and-fill
**Scope:** E-C2, E-C3, E-M1, E-M3, E-M4, §2.3.
- Remove `primary_engine="audd"` paid-first; implement `select_deep_targets(episodes, duration)` → two target sets (uncertain-listed, blank) sampled evenly to ≤ `max_paid_clips`; coverage uses the on-air floor.
- AudD observations get `native["simultaneous_source"]="audd"`; `_engine_corroborated` reads the evidence bucket and requires a provider outside `COMMERCIAL_PROVIDERS`; `discounted_providers` attribution fixed (E-L5).
- Contingency order when the Shazam limiter reports sustained degradation; recorded in the journal + page.
- Crowd rows: require a plausible `Artist – Title` parse and no raw comment text; de-duplicate contradictory answers at one timestamp (keep the best-supported, list the other as an alternative).
- Web: `max_accuracy` → `deep`; the form's second card reads "Deep scan".
- **Tests:** corroboration survives per-trial selection (a regression test built from the C3 scenario: 7 Shazam windows + 7 AudD agreements keep `likely` and gain `engine_corroborated`); paid targets never exceed the cap; phantom-only AudD coverage does not block the free pass.
- **Gate:** on the cached Holly Olivia March mix (`--max-generations 0`, AudD from cache) the tracklist has ≥ the current 12 cross-checked tracks and no `likely` row lost its tier vs the free run.

### Phase 2 — Remove the dead weight, reclaim disk and startup
**Scope:** §2.6, E-M2, E-M8, E-L1, E-L4, E-M7.
- Delete `scan.py` whole-file path + ACRCloud + `tl1001` + web build-index plumbing + their tests; archive tag `pre-v2-removals` first.
- Lazy-import `benchmark/`, `calibrate/`, `truth` behind their commands; `dev` extra in `pyproject`.
- Guard novelty on `max_generations > 0`; hoist out of the re-fuse loop.
- After a successful fuse delete `windows/*.wav` and `decode/` PCM (re-derivable from the retained original); a `--keep-intermediates` flag for benchmarking; `idea gc` command with a retention policy (original audio after N days in hosted mode).
- `_load_cached` index (small JSON/SQLite map by canonical URL).
- **Gate:** `import id_detector.cli` < 1.5 s; a fresh analyse leaves < 90 MB on disk for a 60-min mix; suite still green; the frozen profiles still re-derive byte-for-byte.

### Phase 3 — UI honesty and cleanup (result page, exports, theme)
**Scope:** everything in §2.5 that lives in `present/page.py`, `present/exports.py`, `present/theme.py`
and the library card summary — the parts that survive the Phase 4a port. (Form / progress / home-copy
fixes that live in `present/server.py` — U-F1, U-F9, U-F15, U-F18, U-F30, U-F34 — are done once, in
4a, rather than twice.) Bump `PAGE_VERSION`; regenerate exports alongside the page in one atomic step
(U-F3/F14); update the pinned test hooks in `tests/test_stage7_page.py` deliberately (each change
named in the commit).
- **Gate:** headless-Edge screenshots of home / options / progress / result / narrow committed under `docs/screenshots/v2/`; `node --check` clean; no `unverified`, `no_evidence`, `episode`, `prediction interval`, `suppressed`, `window` strings in rendered user-facing HTML; card count == page count == CUE row count for every mix in `work/`.

### Phase 4 — Web layer port, durable jobs, accounts, quotas (two build cycles)

**4a — FastAPI port + durable job queue (no visual change).** `src/idea_web/` app factory, settings,
db + migrations; Jinja2 templates ported from `present/server.py` + `theme.py`; routes: home/library,
`/new` (try-again landing only), `POST /analyse`, `/jobs/{id}`, `/jobs/{id}/status`, result pages
served from `present/` files; `idea serve` launches this app in local mode; `jobs/store.py` +
`jobs/worker.py` (lease / heartbeat / `recover_startup`, progress rows, persisted log); poll at 2.5 s;
static CSS/JS with gzip + cache headers (U-F29). The server-side UI fixes deferred from Phase 3 ride
along: U-F1 (inline URL error, form preserved), U-F9 (wall-clock progress from measured phase
medians + recent-rate ETA), U-F15 (plain failure cause + remedy, failed step marked, credit line),
U-F18, U-F30, U-F34. `present/server.py` and its tests are retired once the parity tests pass.
**Gate:** local parity — `uv run idea serve --no-open` lists the library, an analyse of a cached mix
runs through the worker and opens; killing the worker mid-job and restarting requeues it; route +
queue tests; `docs/screenshots/v2/` refreshed.

**4b — Accounts, ownership, quotas, sharing.** `users`/`sessions`/`email_tokens`; signup / login /
logout / verify / forgot / reset with the console email backend; CSRF; per-IP rate limits;
disposable-email list; mix ownership + per-user library + delete; public library of cached mixes;
share tokens `/s/<token>` + OG tags; `quota_usage` minute accounting (new vs cached vs deep), refund
on failure, mix-length caps; usage meter ("2 h 10 m of 2 h used · resets 1 Oct") and capped-state
upgrade prompt with the URL preserved; hosted-mode rules (§4.8) behind `IDEA_MODE=hosted`;
`idea admin` (create-user, grant, revoke, jobs, gc).
**Gate:** two users cannot see each other's private mixes; the N+1th new analysis is blocked with the
upgrade prompt while a cached link still opens; CSRF and session-rotation tests; an end-to-end test
signup → verify (console link) → analyse cached mix → share link opens logged-out.

### Phase 5 — Billing: LocalBilling first, then Stripe in test mode

`BillingProvider` + `PlanState`; **LocalBilling** with `/dev/billing/*` simulate pages, banner and
production refusal; pricing page; plan gates in the UI; then **StripeBilling** exactly as §4.7
(Checkout subscription + one-time packs, Portal, raw-body webhook verification, `event_id` dedupe,
re-fetch-on-event, grant/revoke/grace rules); `stripe listen` dev loop documented; admin grants with
`source=admin`.
**Gate:** (1) with `BILLING_BACKEND=none` and no network: free → simulate payment → Pro gates open →
simulate failed renewal → grace → revoke, all via the UI; (2) with `stripe sandbox create` keys +
`stripe listen`: 4242 upgrade opens Pro, 0341 renewal fails → past_due → grace → revoke, cancel at
period end, pack purchase adds minutes — **no real charge anywhere**; (3) replaying a webhook is a
no-op (idempotency test); (4) a client-supplied price id is ignored (test).

### Phase 6 — Hosting shape (two build cycles)

**6a — Ingest hardening, container, retention.** `ingest_policy.py` (per-platform egress; proxy only
for YouTube jobs; `IDEA_YOUTUBE=off`); upload fallback (multipart, 250 MB cap, ffprobe validation);
minutes refunded on fetch failure; `yt-dlp[default,curl-cffi]` + Deno in the image; `retention.py`
GC + disk alarm; Dockerfile + compose (web, worker, Caddy) + Litestream; secrets via env file;
`PROJECT_ROOT` no longer `cwd()` (E-L2).
**Gate:** `docker compose up` on a clean machine serves the site through Caddy; a SoundCloud link
analyses end-to-end from inside the container; a 5-hour synthetic job is refused per cap; GC leaves
≤ 25 MB durable per mix; restart during a job recovers it.

**6b — Observability, policy, launch checklist.** Structured logs with secret redaction; `/admin`
(queue depth, quota usage, paid spend, disk); attribution slot; ToS / privacy / DMCA / "how it works
and what it can't do" pages (drafts for the solicitor); 3-platform canary job; deployment doc; the
hosted engine defaults from §2.3 set in config.
**Gate:** the launch checklist in `docs/LAUNCH.md` is complete except the items only Nathan can do
(AudD terms, Stripe live keys, legal review, domain).

---

## 6. Risks and open questions for Nathan

### 6.1 Open questions (each has a default so the build is not blocked)

1. **Positioning — consumer tool, or sell upward?** The market evidence (§2.7) says the consumer
   segment is commoditised, shrinking, newly contested by Beatport, and culturally resistant to paying;
   the only tier with real margin and no competitor is *certified* reporting for royalty / clearance /
   label-promo buyers — which needs the real-mix certification corpus (standing decision 1 in
   `docs/STATUS.md`). **Default in this plan:** build the consumer product as the acquisition and
   cached-catalogue layer with modest revenue expectations, and keep the upward path open at zero extra
   cost (provenance, version axis and evidence bundle stay in the data model and the JSON export;
   nothing report-specific is built). Nathan decides whether to fund the corpus later.
2. **Which engine powers the *hosted* free tier?** (a) unofficial Shazam behind a kill-switch — $0 API,
   the legal exposure the commercial checklist says to remove, **~65 mixes/day for the whole service
   per egress IP**, and silent accuracy degradation past the throttle (§2.3); or (b) sparse licensed
   AudD (1 clip / 30 s ≈ $0.24–0.60 per hour) — legal, parallelisable, ~10–25 % less recall on
   underground tracks (which the crowd hints partly recover). **Default: (b).** Engine roles per tier
   are config values; local mode stays Shazam; both reviews that looked at this independently
   recommend (b) for anything that takes money.
2b. **Keep YouTube as a source when hosted?** YouTube blocks datacenter IPs; it needs residential
   egress (~$0.15–0.50 per mix) and the most fragile part of the stack (PO tokens, SABR). SoundCloud
   and Mixcloud work direct from a VPS. set79 is SoundCloud-only. **Default:** YouTube stays
   supported behind a per-platform egress setting and can be switched off (`IDEA_YOUTUBE=off`);
   spike it on a throwaway VPS before Phase 6 commits to a proxy vendor.
3. **Free allowance:** 120 new minutes/month (≈ ryser.id, the most generous competitor) vs Nathan's
   "1 mix a week" (≈ 240). Config value; default 120.
4. **AudD subscription rate.** The £7 tier only carries margin at the "$2 per 1,000" committed rate.
   Nathan must ask AudD for that rate before a price is published.
5. **Legal review** (Nathan owns; the plan only drafts pages): terms of service + privacy policy + DMCA
   contact; the ingestion posture (every competitor: "you assert the rights, we don't re-host, audio is
   discarded"); SoundCloud comment scraping (api-v2 — on the v1 commercial checklist); UK personal-data
   handling (email + password + payment status only; Stripe holds card data).
6. **Version axis on the page:** hidden by default (100 % `unverified` in single-engine runs), kept in
   JSON and shown only when a run actually verified a recording. Confirm.

### 6.2 Risks

| # | Risk | Likelihood / impact | Mitigation in this plan |
|---|---|---|---|
| R1 | Shazam endpoint changes or blocks the server IP | medium / high (free tier stops) | provider kill-switch; engine-per-tier config; Deep scan keeps working AudD-first (contingency order); honest "free scans paused" state |
| R2 | YouTube blocks yt-dlp from datacenter IPs; SoundCloud/Mixcloud work direct | high / medium (YouTube jobs fail) | per-platform egress (residential proxy only for YouTube jobs, ~$0.15–0.50/mix) or `IDEA_YOUTUBE=off`; link-first + **upload fallback**; minutes refunded on fetch failure; spike on a throwaway VPS before Phase 6 |
| R10 | AudD production terms unread (the *trial* licence forbids commercial use and requires a "Powered by AudD" logo); subscription rate for the clip endpoint unconfirmed | certain / medium | Nathan emails api@audd.io before launch (attribution, caching/resale of results, concurrency, rate); an attribution slot is a config value in the page footer |
| R11 | Hosted Shazam: ~65 mixes/day per IP for everyone, malformed JSON past the throttle read as "no match" | certain if used as primary / high | Shazam is never the hosted primary; as secondary it is capped per mix, has a global daily budget + circuit breaker, and malformed bodies are treated as throttle signals; never routed through residential proxies (that is throttle evasion) |
| R3 | AudD only at walk-up rate ($5/1,000) | medium / medium (margin thin) | sampled clips (≤ 80/mix) keep Deep ≈ $0.40; per-run and per-account USD ceilings; price not published until the rate is known |
| R4 | Willingness to pay is weak; Beatport bundles Track ID for free | high / medium | keep fixed costs tiny (one small VPS); free tier is the SEO catalogue; don't build Studio/API until Pro has paying users; the upward (report) path is the hedge |
| R5 | The frozen profiles' byte-for-byte test (`test_committed_profiles_rederive_byte_for_byte`) blocks engine changes | certain / low | Deep scan is a **runtime mode layered on the free profile** (targets + paid pass + re-fuse), never a new frozen profile; `max_accuracy-v1.json` stays untouched and is simply no longer selected by the web app |
| R6 | A phase is too big for one Codex build + review cycle | medium / low | each phase has a named gate; Phase 3 and Phase 6 are the candidates to split (UI: data-honesty first, then cosmetics; hosting: persistence + workers first, then Docker/Caddy) |
| R7 | Disk fills (4.9 GB / 10 mixes today) | certain without Phase 2 / high | Phase 2 reclaims 77 %; `idea gc` + retention policy; hosted mode drops the original after N days |
| R8 | A user's paid run fails mid-way after AudD spend | medium / medium | minutes refunded on failure (table stakes in the category); spend journaled per run; ambiguous outcomes retried once, not re-billed blindly |
| R9 | Free-tier abuse (many accounts, long mixes) | medium / low–medium | minute-based metering; per-IP signup + analysis rate limits; disposable-email blocklist; cached mixes don't cost anything; per-run USD ceiling |

---

## Appendix A — findings register

| ID | Severity | One line | Phase |
|---|---|---|---|
| E-C1 | Critical | paid-first crashes after spend | 0 |
| E-C2 | Critical | paid-first makes corroboration impossible | 1 |
| E-C3 | Critical | AudD wins selection, no anchor → tiers downgrade | 1 |
| E-C4 | Critical | AudD error bodies cached forever | 0 |
| E-H1 | High | no spend cap / no spend record | 0 |
| E-H2 | High | gap-fill request counts zeroed | 0 |
| E-H3 | High | silent paid→free downgrade | 0 |
| E-H4 | High | paid pass uncancellable, ETA "1 s" | 0 |
| E-H5 | High | paid pass sequential/unretried, re-bills on timeout | 0 |
| E-H6 | High | config knobs dropped under a profile | 0 |
| E-H7 | High | hosted blockers: single worker, in-memory jobs, local-path input, non-queuing lock | 4, 6 |
| E-H8 | High | `upload_consent` POST triggers whole-file scan | 0 |
| E-H9 | High | web build-index never queried | 0/2 |
| E-M1 | Medium | corroboration counts two commercial catalogues | 1 |
| E-M2 | Medium | novelty computed for nothing | 2 |
| E-M3 | Medium | AudD phantom blocks free gap-fill | 1 |
| E-M4 | Medium | crowd rows order-sensitive / duplicated | 1 |
| E-M5 | Medium | `--refresh` re-bills silently | 0 |
| E-M6 | Medium | CLI `--profile max_accuracy` == free | 1 |
| E-M7 | Medium | `_load_cached` O(all mixes) + full hash | 2 |
| E-M8 | Medium | 77 % of disk is disposable | 2 |
| E-M9 | Medium | `_windows_in_spans` ignores transforms | 0 |
| E-M10 | Medium | fuser told whole mix was Shazam-scanned | 1 |
| E-L1 | Low | 2.1 s import tax | 2 |
| E-L2 | Low | `PROJECT_ROOT = cwd` at import | 6 |
| E-L4 | Low | `--engine acrcloud` silently whole-file | 2 (removed) |
| E-L5 | Low | `discounted_providers` alphabetical attribution | 1 |
| U-F1 | Critical | bad URL → raw JSON page | 3 |
| U-F2 | Critical | card vs page track counts differ | 3 |
| U-F3 | Critical | Copy / CUE / page = three tracklists; M3U dead | 3 |
| U-F4 | Critical | buy links hidden on phones | 3 |
| U-F5 | Critical | no users, no CSRF, global library | 4 |
| U-F6 | Critical | full mix audio served (hosted) | 6 |
| U-F7 | Critical | "N matches hidden · show" advertises noise | 3 |
| U-F8 | Critical | version/role columns 100 % constant | 3 |
| U-F9 | High | progress is step-index maths | 3 |
| U-F10 | High | acquire column empty / search-as-store | 3 |
| U-F11 | High | dead rescan button → CLI instruction | 3 |
| U-F12 | High | false privacy footer | 3 |
| U-F13 | High | comment text rendered as a track | 1 |
| U-F14 | High | write-on-GET page refresh, exports not refreshed | 3 |
| U-F15 | High | failure copy: raw yt-dlp, no failed step, no credit statement | 3 |
| U-F16/17 | High | seven badge systems, statistics legend | 3 |
| U-F18 | High | in-progress cards: raw URL + raw phase key | 3 |
| U-F19/20 | High | contrast fails AA; `<tr role=button>` | 3 |
| U-F21–F35 | Medium | tiles, chips, player, library, alts, lead-in, options summary, HTTP/1.0 + no gzip, auto-redirect, tokens, tail gap, failed runs invisible, duplicate form, copy | 3 (F29 → 6) |
| U-F39 | Low | no share link / OG tags | 4 |
| U-F41 | Low | no BPM/key/artwork | later (Pro feature candidate) |

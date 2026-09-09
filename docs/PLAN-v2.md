# IDea v2 — from a local tool to a product people pay for

*Plan rev 2 · 2026-09-09 · written from four independent adversarial reviews (engines/fusion, UI/product,
market, hosting/payments) against `main` @ `27c36fd`, revised after Codex plan-review round 1
(`docs/reviews/plan-v2-review-round-1.md`) and the owner's decisions of the same day (§6.1). Review
sources are summarised in §1; Appendix A maps every finding to the phase that fixes it.*

---

## 0. The verdicts, in plain English

| Question Nathan asked | Verdict |
|---|---|
| **Is the business model right** (free capped weekly, paid capped monthly)? | **Yes, with three changes.** (1) Meter **minutes of audio**, not mixes — a 4-hour set otherwise costs us 4×. (2) The *paid* allowance is **Deep-scan minutes** (the thing that costs us money); on Pro every new mix is a Deep scan — there is no separate "standard" allowance. (3) A mix that is already in the cache is served free and does not count — but only when the cached result is at least as good as what was asked for (a Deep request on a Standard result pays the difference). Both a subscription and non-expiring packs are offered. Numbers in §3. |
| **Could we charge for it?** | **Not today; yes after Phases 0–1.** The paid tier **crashes after spending the money** whenever AudD returns any parseable result (E-C1); when the key is out of credit it *silently* runs the free engine instead and caches the error bodies (E-H3, E-C4); it has no spend cap (E-H1); and as built it throws away the benefit it is meant to buy (E-C2/E-C3). Fixed, capped and re-sequenced, a Deep scan is fast, bounded and *more* accurate than today. |
| **Is it the best implementation?** | **No.** The pipeline core (windows → engines → fusion → episodes) is sound and well tested. The paid sequencing is broken in four specific ways (§2.3), and the web layer (stdlib `http.server`, one worker thread, in-memory jobs, no users) cannot be hosted as-is (§2.2). |
| **Is the engine order right?** | **Paid engine first, then Shazam — yes (owner decision D2, for speed), but not the way it is built.** After the AudD sweep, Shazam must check the *uncertain* spans as well as the blanks (sampled, capped), AudD must carry a time anchor and its own vote so it cannot *downgrade* Shazam's results, spend must be capped, and the run must not crash. §2.3. |
| **Does the UI work / show useless data?** | The look is good — keep it. But the library card and the result page disagree on track counts, the downloads can disagree with the page, the page advertises its own noise ("56 matches hidden · show" → *Cher — Believe*), two columns are constant across all 305 tracks, buy links vanish on phones, a bad URL lands on raw JSON, and the footer's privacy claim is false. §2.5. |
| **What should be removed?** | Whole-file paid scan path + consent gate, the ACRCloud adapter, the 1001tracklists connector, the rescan button/route, version/role columns, the hidden-matches reveal, the M3U export, the duplicate `/new` form, dead POST parameters. Quarantined (kept, off the hot path): rescans, calibration ML, benchmark, truth. **Kept and fixed: Panako** (D3) — the web "build index" button today builds an index the analysis never queries. §2.6. |
| **Does it fill a genuine gap?** | **Partly — and not where we assumed.** "Paste a link, get a tracklist" is a commodity (20+ hosted tools, ~$1/mix, falling traffic, weak willingness to pay). Two things nobody ships: "right track vs right *recording*", and a re-checkable evidence trail. Those matter to royalty/clearance/label buyers, which needs certified tiers, which needs the real-mix corpus. §2.7, D1. |

**Shape of the plan:** a spike/owner-gate phase (S) that can run in parallel; Phases 0–3 make the
*existing* tool correct, bounded, honest and cheap to run (all local); Phases 4–6 add the web layer,
durable jobs, accounts, quotas, billing (test mode) and the hosting shape, ending in a **private
beta**. Fifteen build cycles; each built, reviewed and committed on its own.

---

## 1. What was reviewed and how

| Review | Scope | Method |
|---|---|---|
| **E — engines & fusion** (`docs/reviews/v2-review-engines.md`) | `cli._analyse` sequencing, `paid_clip`, `scan_targeting`, `fuse/*`, `providers/*`, job runner, disk, tests | code reading + `dis` bytecode verification of the crash + `du` measurements + test timing |
| **U — UI / product** (`docs/reviews/v2-review-ui.md`, screenshots in `docs/screenshots/v2-before/`) | every screen | live server on a spare port, headless-Edge screenshots at 1280/640/390 px, `node --check`, data audit across all 305 tracks in `work/` |
| **M — market** (`docs/research/05-market-2026-09.md`) | 21 hosted competitors, caps/metering, engine pricing/terms, demand (Reddit/HN verified) | web research, cited |
| **H — hosting & payments** (`docs/reviews/v2-review-hosting.md`) | yt-dlp/Shazam from a server, AudD terms, Stripe test-mode flow, framework, sizing, abuse | web research + code reading, cited |
| **Codex plan review round 1** (`docs/reviews/plan-v2-review-round-1.md`) | this plan, rev 1 | independent code verification of every cited finding |

The decisive engine findings were re-verified by the orchestrating session and again by Codex.
Current offline suite: **581 passed in 44 s** (93 deselected: `slow` + `live`). The two full-pipeline
test modules are `slow`-marked and never run by default — which is how E-C1 shipped.

---

## 2. Findings that decide the plan

### 2.1 The paid tier does not work (Critical, verified twice)

| ID | Finding (wording as qualified by round 1) | Evidence |
|---|---|---|
| **E-C1** | **The paid-first branch deterministically crashes after presentation whenever it receives any parseable AudD observation** (including a successful no-match). `matches`/`recognised` are read at `cli.py:794-815` but bound only in the free branch (`cli.py:507-521`). When AudD is unavailable or every call errors, the code instead falls back to free (`cli.py:499-506`) — no crash, but see E-H3. The web runner is the path that reaches this (`webapp/runner.py:137-167`); CLI `--profile max_accuracy` alone is still Shazam-only (E-M6). | `dis` → `LOAD_FAST_CHECK`; no test sets `primary_engine` |
| **E-C2** | **Paid-first eliminates the designed overlap between engines.** The free pass runs only over the complement of every non-suppressed AudD episode (`scan_targeting.py:61-69,127-146`, called at `cli.py:659-682`), so corroboration is normally absent and any flag that does fire (via 3 s padding / start-time window selection) is incidental. `_engine_corroborated` does not check temporal overlap at all (`episodes.py:148-156`). | code |
| **E-C3** | **Adding AudD can downgrade tracks.** AudD clip observations have `anchor=None` and no separate source (`audd.py:344-365`), so both engines share the `primary` trial group (`alignment.py:137-150`); when both name the same candidate the tie-break favours AudD's zero skew cost, and an anchorless winner cannot create an alignment point (`alignment.py:115-124`) → `has_global` False → `likely` unreachable, a spurious `hypothesis_rejected` flag, and possible hiding under the 30 s floor. Not *every* contested window, but common. **AudD's standard result carries a `timecode` that the enterprise parser already turns into an anchor (`audd.py:242-250`); the clip parser must too.** | code |
| **E-C4** | **AudD error bodies are cached indefinitely under the current cache key.** `paid_clip.py:182-190` writes the raw response before `:191-202` rejects `status != success`; any cached dict is trusted on read (`:175-181`). Top up the account → the mix keeps reading errors until `--refresh` (which re-bills every clip). | code |
| **E-H1** | **No spend cap, no spend record.** Paid-first passes `max_clips=len(windows)+1`; `usd_e2` journaled as `0` unconditionally (`cli.py:820/850/864`); no USD field in `AppConfig`, so the profile's `budget.max_usd_e2` never reaches the clip path. | code |
| **E-H2** | Gap-fill request counts overwritten with 0 by `_refuse_with` (`cli.py:649-655,676`). | code |
| **E-H3** | **Silent downgrade paid → free** on missing/invalid key, exhausted quota, network outage — only a progress-log line records it. | `cli.py:499-506`, `audd.py:73-77,435-436` |
| **E-H4** | Paid pass uncancellable (no `progress()` in the loop) and reports `windows_total=1` → ETA "1 s". | `paid_clip.py:161-197`, `jobs.py:211-221` |
| **E-H5** | Paid pass sequential, unrated, unretried; a timeout drops the window with no cache entry → re-billed next run. | `paid_clip.py` vs `recognise.py:274-290` |
| **E-H6** | **Config knobs silently ignored whenever a profile is active** — `present_min_track_ms`, `shazam_requests_per_minute`, `recognise_concurrency`, `collapse`, `same_track_bridge_ms` (real `AppConfig` fields, `providers/base.py:90-115`) are not carried by `cli.py:985-1007` / `runner.py:48-72`; `config show` prints the file config, not the effective one (`cli.py:303-318`); the profile's own budget/RPM values are not propagated either. | code |
| **E-H8** | `upload_consent` / `build_index` still accepted from the POST body after removal from the form; `upload_consent=true` triggers the whole-file enterprise scan **in addition to** the clip scan. | `server.py:1373-1406`, `cli.py:527-547` |
| **E-H9** | Web "build reference index" builds a Panako index the analysis never queries (`local_index_label` never passed). | `runner.py:130-172`, `cli.py:636` |
| **E-M1** | `engine_corroborated` counts distinct provider strings; `COMMERCIAL_PROVIDERS` is a *discount* set, not an independence model. | `episodes.py:91-108,148-156` |
| **E-M2** | Spectral novelty (full log-mel pass, ~0.7 GB RAM per hour of audio) runs on every fuse, consumed by nothing with rescans off. | `orchestrate.py:140`, `novelty.py` |
| **E-M3** | `select_gap_targets` uses *any* coverage → one AudD phantom removes ~12 s from the free pass. | `scan_targeting.py:60-68` |
| **E-M4** | Crowd-ID rows order-sensitive (sorted by opaque hash) and contradictory answers at one timestamp both listed. | `episodes.py:790-845` |
| **E-M8** | **Disk: ~330–370 MB per hour of audio, 10–25 MB of it durable.** `work/` = 4.9 GB / 11 mixes. Nothing reclaims. | measured |
| **E-L1** | Eager imports of `benchmark/`, `calibrate/`, `truth` → 2.1 s startup tax on every command. | measured |
| **E-S1** (new) | Under sustained throttle Shazam returns malformed bodies; the adapter raises `ShazamHTTPError` (`shazam.py:200-204`) and recognition records a **failure** observation (`recognise.py:307-340,521-553`) — not a no-match. Failures are poorly surfaced, so a throttled run ends with many failed windows and a thin tracklist without saying so. | code (round-1 correction) |

### 2.2 The web layer cannot be hosted as-is (verified)

- One in-memory worker thread; a restart loses every job (`webapp/jobs.py`).
- No users, sessions or CSRF: **today any website Nathan visits can `POST /analyse` to `127.0.0.1:8765` and start a paid run** (U-F5). `validate_target` accepts `file://` and any existing path (`jobs.py:59-84`) and the server serves it back over `/jobs/<id>/audio`.
- `make_server` refuses non-loopback binds by design; HTTP/1.0, no TLS/gzip/cache headers; 178 KB pages with inlined CSS/JS (U-F29).
- `ProcessLock` raises instead of coalescing → two users analysing the same mix breaks (E-H7); two URLs converging on one media directory after download collide (`cli.py:412-422`).
- The renderer inserts `<audio src="../ingest/original.*">` whenever the original exists (`page.py:1168-1172`) — hosted, that is redistribution of someone's set + ~100 MB egress per view (U-F6).
- A GET of a stale result page rewrites `index.html` without updating the exports (U-F14/F3): downloadable exports can disagree with the page; Copy follows the visible rows.
- **Result artefacts are mutable singletons** (`fuse/episodes.json`, `present/tracklist.*`): a later Standard run overwrites a Deep result (round 1).
- `_load_cached` requires the retained original and hashes it (`ingest.py:143-164`): "a cached link costs nothing" is only true while the original is kept.

### 2.3 Engine sequencing — Deep scan v2

**Owner decision D2:** the paid engine sweeps first (no per-IP throttle; ~4–8 min for a 2-hour mix once
parallelised) and Shazam runs second. Free Shazam is throttled per IP — the owner's live 2-hour run today:
789 windows at 12.5 req/min ≈ 63 min — and nothing in our control makes it faster.

**What was wrong with paid-first as built** (and stays fixed in v2): Shazam was sent only to the blanks
(E-C2); AudD had no anchor and no vote of its own (E-C3); no cap (E-H1); the crash (E-C1); a sequential
paid loop (E-H5).

**Deep scan v2 — the recipe** (all values are fields of an immutable *analysis recipe*, §3.4):

1. **Resolve the recipe** — engine roles, sweep density, secondary cap rule, parser/adapter versions,
   presentation settings → `recipe_id` (hash). Serve from cache only an exact or capability-superset
   result; a lesser recipe never overwrites a greater one.
2. **Primary sweep: AudD over the whole mix** at `primary_density` (default full: the frozen 12 s / 9 s
   window set; half density = every other window). Bounded concurrency (3–4), token bucket, retry/backoff,
   progress every clip (cancellable), per-run USD reservation. AudD observations carry an **anchor parsed
   from `timecode`** and `simultaneous_source="audd"` (own trial group).
3. **Hints** (comments / pasted tracklist) → **first fuse**.
4. **Secondary: Shazam, sampled and capped, over targets built in priority order:**
   (a) `hint_only` rows (a crowd ID with no audio match is a first-class verification target — today it is
   wrongly treated as confident, `scan_targeting.py:20-31`); (b) listed-but-not-confident episodes;
   (c) suppressed candidates worth challenging; (d) genuine blank duration. At least one representative,
   high-audio-quality window per episode; the remainder distributed by target *duration*, unused allocation
   flowing to the other category; windows chosen by **intersection with the target**, not start time
   (`paid_clip.py:101-117` misses spans < 12 s). **The cap scales with duration** — `secondary_clips_per_minute`
   (default 2 → 120 for 60 min, 240 for 2 h) — bounded by the USD reservation. **20–30 % of the cap is
   reserved for adaptive confirmation:** when a probe finds a new identity in a blank, query two more
   non-overlapping windows around it.
5. **Re-fuse once.** Corroboration = **selected votes** (each provider survives selection thanks to its own
   trial group) for the same normalised work with **temporally overlapping support** from **different trust
   families** — `catalogue-commercial` {audd, acrcloud}, `shazam`, `local-index` (Panako), `crowd` (hints
   remain hint corroboration, not an "engine"). One cross-family agreement is *displayed* ("confirmed
   twice"); bypassing suppression and the 30 s floor requires **two separated agreements or sufficient
   same-engine on-air support**.
6. **Run status is recorded and shown:** `complete | degraded | budget_exhausted | provider_unavailable |
   partial`. A run never silently changes the purchased recipe; if Shazam degrades mid-run the job finishes
   as explicitly `degraded` (AudD-only) — a service-wide circuit breaker with a request budget, never a
   mid-run semantic swap, never residential-proxied Shazam.

**Cost/speed per 60-minute mix, 400 windows** (AudD $5/1,000 walk-up · $2/1,000 subscription; Shazam free
but IP-budgeted; times assume Phase 0b concurrency):

| Recipe | AudD req | Shazam req | AudD $ | Wall-clock |
|---|---|---|---|---|
| **Local Free** (Shazam sweep) | 0 | 400 | $0 | 9–60 min (IP throttle) |
| **Deep** (default: AudD full sweep → Shazam ≤ 120 sampled) | 400 | ≤ 120 | **$2.00 · $0.80** | ~4 min + 3–10 min |
| Deep, half density | 200 | ≤ 120 | $1.00 · $0.40 | ~2 min + 3–10 min |
| Deep economy (Shazam sweep → AudD ≤ 80 sampled) | ≤ 80 | 400 | $0.40 · $0.16 | 9–60 min + ~1 min |
| Hosted Free (owner decision D1: Shazam sweep, capped) | 0 | 400 | $0 | shares the service's Shazam IP budget (§4.1) |

**Why not ACRCloud:** live-tested ≈ AudD's coverage; same family, so it cannot corroborate AudD. Removed.
**Panako:** kept (D3); it is the `local-index` family and the only lever for a DJ's own unreleased uploads;
web wiring fixed so the built index is queried.

### 2.4 Accuracy: where the real edge is

On the owner's 30-track benchmark Shazam alone caps at ~27/30; the misses are tracks in **no commercial
catalogue** (AudD/ACRCloud miss them too). The levers for those are crowd IDs from comments and a DJ's own
indexed uploads. Two cross-family engines agreeing is the strongest *precision* signal. Two accuracy bugs
ride along: comment text rendered as a track with a POSSIBLE badge (U-F13) and contradictory crowd answers
both listed (E-M4). **No accuracy number is advertised until the real-mix release gate (§6.3 L3) exists.**

### 2.5 UI: remove / change / add (review U; screenshots verified)

**Remove:** the "N matches hidden · show" counter+toggle (U-F7); `Version`/`Role` columns from the page and
Markdown — `unverified` / `incoming|dominant` on 305/305 rows (U-F8; **kept in JSON and shown only when a
row is verified or contested** — the axis is central to the evidence model and the future report tier);
the per-row `rescan` button, `ops` column and `POST /rescan` (U-F11); the `HINT` pill; `layer`/`outgoing`
tags; the `ID gap` tile; "9h 03m of music listened to"; the M3U export (dead as written); the duplicate
`/new` form (route kept as the try-again landing); the false "nothing leaves 127.0.0.1" footer (U-F12);
`no_evidence` / "suppressed" / "buried under a surer track" strings; the `upload_consent` POST param.

**Change:** bad URL → inline error with the form preserved, never raw JSON (U-F1); **one typed entry list
(tracks vs gaps) is the single source for the page, Copy, CUE, Markdown, JSON and the library card**, and the
exports are regenerated with the page as one atomic, versioned bundle (U-F2/F3/F14); keep "Where to get it"
on phones — stacked cards ≤ 720 px, thinner ruler (U-F4); prefer a known `purchase_url` over a store
*search*, label search fallbacks, hide the column when empty (U-F10); wall-clock-proportional progress
(U-F9); failure copy with plain cause + remedy, failed step marked, explicit credit statement (U-F15); one
confidence word per row + a one-line glossary; legend six terms → two (U-F16/F17); `--dim` → ≈ `#8b8ba6`;
`<tr>` loses `role="button"`, headers get `scope="col"` (U-F19/F20); `profile free` chip → "Free scan" /
"Deep scan"; three stat tiles (tracks · length · identified %) with the confidence mix captioned under the
Tracklist heading (U-F21); "other versions" only for a genuinely different track (U-F25); lead-in spinner
behind a details (U-F26); options summary reflects the pasted tracklist (U-F28); in-progress cards use the
resolved title + friendly step names, running first (U-F18); no 5 s auto-redirect (U-F30); crowd rows never
render comment text and never carry a badge (U-F13); one true privacy line everywhere; run status
("Deep scan · complete" / "degraded — Shazam unavailable") in the header.

**Add:** analysed date on cards and page; share link (`/s/<token>`, unlisted, read-only, OG tags — U-F39);
per-user library with delete (Phase 4d); "you can close this tab"; a visible record of failed runs (U-F33).

**Keep:** the club-mode theme, equaliser logo, scanner strip + flavour text, mix cards, `prefers-reduced-motion`,
NOW pill, CUE/Markdown/JSON exports, the paste-a-tracklist box (promoted).

### 2.6 Remove / quarantine / keep (subsystems)

| Subsystem | Decision | Why |
|---|---|---|
| Whole-file `scan.py` path, `run_paid_scanners` call site, consent gate, `upload_consent` param | **Disable in 0a, delete in 2b** (archive tag first) | superseded by the clip path; active liability (E-H8) |
| `providers/acrcloud.py` + tests + `_Bundle` branch | **Disable in 2a, delete in 2b** | same family as AudD; redundant; corroboration-gaming vector |
| `hints/connectors/tl1001.py` | **Default-disable 2a, delete 2b** | JS-gated site; emits only quarantined pointers |
| **Panako** (`providers/panako*.py`, `local_index.py`, `candidates.py`) + web `build_index` | **Keep; fix the wiring (0a)** | D3; the `local-index` family; excluded from the hosted image until needed (JDK) |
| Rescans + transform grid, `rescan.py`, `orchestrate` loop | **Quarantine** (frozen legacy) | profiles certify `rescans=3` and must re-derive byte-for-byte; off by default |
| `calibrate/`, `benchmark/`, `truth.py`, `local_fixture.py`, `pointer_import` | **Quarantine** — lazy import + `dev` extra | zero value on real mixes today; `benchmark/` needed to re-freeze; removes the 2.1 s tax |
| `novelty.py` | **Guard** on `max_generations > 0`, hoist out of the re-fuse loop | E-M2; also the RAM hog (§4.1) |
| `sc_comments`, `manual` tracklist, `mixesdb`, `yt_comments`, `mixcloud` hints | **Keep** (invest in `sc_comments`) | the differentiator; hard timeouts when hosted; commercial-review item L2 |
| CUE / Markdown / JSON exports | **Keep**; M3U removed | real DJ formats |

### 2.7 Market: where IDea can and cannot win (review M)

**The consumer segment is crowded, cheap and shrinking.** Verified September 2026:

| Tool | Paid | Free | Metering | Engines |
|---|---|---|---|---|
| set79 | $10.90/mo unlimited; $8 / 3 sets | browse 70,000 cached sets, **0** new analyses | per set | fingerprinting + **reads SoundCloud comments**, leaves gaps blank |
| setlist.id | $6 / $15 / $25 per month (3 / 8 / 15 sets) | 2-set trial, card required | per mix (> 4 h = 2) | ACRCloud |
| ryser.id | €6 / 300 min · €12 / 800 min · €29 / 2,500 min + API; packs never expire | **120 min/month** | **1 credit = 1 minute** | two engines + rescue passes; **sells royalty reporting** |
| TrackSniff | $9 / $19 per month | 2 scans/day, **first 3 tracks shown** | per scan | five confidence tiers, BPM/key |
| TrackRadar | $4.99 / 10 credits, never expire | 3 analyses/month, first without an account | per analysis / per track | **AudD → Shazam → ACRCloud**, comments |
| djtracks.io / TracklistAI / IDThisMix / AudioScout | **$0.99 – $2.99 per mix** | first mix free, full result | per mix | ACRCloud / undisclosed |

Traffic: set79 ~80 K visits/month (−16 % MoM), trackid.net ~41 K, TrackSniff ~12 K (−14 %); 1001Tracklists
(free, human-curated, **78.9 M visits/year**) −17 %.

**Demand, verified first-hand:** the want is large and old (445-upvote r/DJs thread "so many bangers that
Shazam couldn't pick up"; MixesDB: 366,257 mix pages, **21.9 % with no tracklist, 37.8 % incomplete**;
SoundCloud #DJSET uploads +39 % YoY). The loudest technical complaints are what IDea is built for —
edits/flips/bootlegs resolving to the original, pitch-shifted tracks — and one user literally wrote our brief
("output the timestamp… anticipating numerous identification errors… an approximate tracklist enables quick
validation"). But **willingness to pay is weak and partly hostile**: nobody named a price; the free tool
Set2Tracks (371 upvotes) shut down because *"this is the kind of tool people love to find for free but
wouldn't pay for"*; trackid.net going paid drew "nothing free now"; Beatport's thread called commercialising
trainspotting "fucking wild". **Beatport Track ID** (May 2026, 10 M users, owns the buy links) now markets
pitch-shift and edit/remix handling from inside the app DJs already use.

**What the market has learned (copy these):** meter minutes; never cripple the free result; non-expiring
packs; refund on failure; the cached public catalogue is the real business model (74.5 % of set79's desktop
traffic is organic search into it).

**Unit costs (verified):** AudD counts 1 request per 12 s; $5/1,000 walk-up; "subscription options start at
$2 per 1,000" (enterprise docs; whether that rate applies to the per-clip endpoint is unconfirmed — L1).
AudD's **Test License** (the owner's current trial key) is evaluation-only and requires a "Powered by AudD
Music" logo; the paid Developer terms page is a JavaScript shell no fetcher can read. ACRCloud's standard
terms (§13 internal use, §14 non-compete) don't obviously permit consumer resale. **No legal server-side
Shazam exists**; ShazamKit's licence forbids building another recognition service and "compliance
purposes (e.g. royalty auditing)". No enforcement against shazamio or any hosted tracklist service was found;
hosted competitors run the same unlicensed route openly.

**Uniqueness, honestly:** not the core; yes for the two-axis confidence (work vs recording), the evidence
trail, and refusing to print an uncertified accuracy number. Those matter to royalty/clearance/label buyers
(ryser.id sells that with no published accuracy). The realistic consumer angle is *scene-specific* (UK bass /
garage; underground/unreleased is the one need every tool admits it can't serve; crowd IDs + a DJ's own dubs
via Panako is the closest anyone gets).

---

## 3. Product definition v2

### 3.1 Two modes, one codebase

- **Local mode** (`idea.cmd` → `idea serve`): the owner's current browser workflow keeps working at every
  phase boundary: paste a link, watch progress, open the result, play the local audio, export. Changes to the
  CLI surface are listed explicitly per phase (M3U export removed, whole-file scan removed, `max_accuracy`
  profile replaced by the Deep recipe, rescans quarantined). Local mode is exercised by a Windows gate that
  runs `idea.cmd` itself.
- **Hosted mode** (`IDEA_MODE=hosted`): accounts required, quotas/ledgers enforced, local paths refused,
  platform embed instead of served audio, share links, billing. Same pipeline, same artefact tree.

### 3.2 Scans

| | Free scan | Deep scan |
|---|---|---|
| Engines (local and hosted — D1) | Shazam full sweep + crowd hints | AudD full sweep → hints → Shazam sampled (≤ 2/min) → re-fuse (§2.3) |
| What the user sees | LIKELY / POSSIBLE / UNCLEAR, "from comments" rows, run status | the same plus **"confirmed twice"** where families agree; tighter boundaries (AudD anchors); short real tracks rescued when doubly confirmed |
| API cost per hour of audio | $0 (Shazam IP budget, §4.1) | $0.80 – 2.00 (rate-dependent; half density halves it) |
| If Shazam is throttled/blocked | run finishes `degraded` with the failed-window count shown; service circuit breaker pauses new free scans ("busy — try later") | finishes `degraded` (AudD-only), still complete |

### 3.3 Tiers, caps, prices

Metered in **minutes of new audio analysed** (1 credit = 1 minute), rounded up to the minute. **Free resets
weekly** (Monday 00:00 UTC; matches "one mix a week"); **Pro resets on the Stripe billing period**; packs
never expire; consumption order = plan minutes, then packs. Every allowance and price is a config value.

| | **Free** | **Pro** (subscription) | **Packs** (one-off) |
|---|---|---|---|
| Price (GBP, VAT-inclusive) | £0, no card | **£9 / month** (annual = 10 months) | from **£6** |
| Allowance | **150 new minutes / week** (≈ one set), Free scan, full results | **N Deep minutes / month** — N set from the AudD rate actually obtained (table below) | Deep minutes, never expire |
| Every new mix on the tier is | a Free scan | a Deep scan | a Deep scan |
| Mix length | ≤ 150 min per mix | ≤ 240 min per mix (longer refused with a message) | ≤ 240 min |
| History | 30 days | unlimited | 90 days |
| Cached mixes | served free, uncounted, rate-limited (20/day) | same; a Deep request on a Standard result pays only the secondary pass | same |
| Failure | minutes refunded (failed spend still counts toward abuse ceilings) | same | same |
| Queue | normal | priority | normal |

**What N and the pack sizes can be** (Deep = 400 AudD req/hour + YouTube proxy ≈ $0.30 where used + compute/
storage ≈ $0.05; Stripe ≈ 5 %; VAT 20 % → £9 nets ≈ $9.10, £6 nets ≈ $6.05; target ≥ 50 % gross margin):

| AudD rate obtained | Deep cost / hour | Pro £9 → N | Pack £6 → minutes |
|---|---|---|---|
| Walk-up $5/1,000, full density | ~$2.35 | **≈ 120 min** (~1–2 sets) | ≈ 75 min |
| Walk-up, half density | ~$1.35 | ≈ 200 min | ≈ 130 min |
| Subscription $2/1,000, full density | ~$1.15 | ≈ 240 min | ≈ 155 min |
| Subscription, half density | ~$0.75 | **≈ 360 min** (~5 sets) | ≈ 240 min |

The market sells at ~$1/mix because it samples sparsely or leads with a free engine; a full-density licensed
sweep cannot match that price at walk-up rates. **No price or allowance is published until L1 (AudD rate and
terms) is settled** — the site launches as a private beta with admin-granted plans.

**Free-tier cost is capacity, not dollars:** Shazam is $0 but one egress IP sustains ~40–65 hour-long mixes
per day for the whole service (§4.1). Free is therefore bounded by a **global daily Shazam budget per IP**
with a circuit breaker, plus the per-account weekly allowance. YouTube proxy bandwidth (~$0.15–0.50 per
YouTube mix) is the only cash cost of a free scan; it sits under a global monthly proxy budget.

### 3.4 Recipes, results, ledgers (the round-1 P0s)

- **Analysis recipe** — immutable, hashed: engine roles, sweep density, secondary cap rule, adapter/parser
  versions, presentation settings. Every result records its `recipe_id`. Cache serves exact or
  capability-superset results; a lesser recipe never overwrites a greater one; a Deep upgrade of a cached
  Standard result runs only the secondary pass and charges the delta. Result bundles are versioned
  directories with an atomic manifest pointer, never in-place file replacement.
- **Quota ledger** — append-only rows: reservation (before enqueue, atomic with a balance check),
  consumption, refund, adjustment; source = plan | pack | admin. Concurrent submissions cannot both pass a
  balance check. Same shape for **USD ledger** (per-run, per-account monthly, and global provider ceilings
  across all workers) and **credit ledger** (packs: purchase, consumption, chargeback reversal, liability).
- **Typed provider cache states** — `match | no_match | retryable_failure | ambiguous`, with adapter/schema
  version in the key; `--refresh` is selective (by state), never "re-bill everything".

### 3.5 Sign-in and payments (decided)

Email + password (argon2-cffi); **email verification required before any allowance**; server-side sessions;
CSRF on every state-changing POST. Billing backends `BILLING_BACKEND=off | local | stripe`: `off` = admin
grants only (the private beta); `local` = simulated checkout/portal pages, no network; `stripe` = sandbox or
live, live additionally requiring `sk_live_` keys **and** `IDEA_BILLING_LIVE=1`. Everything is verifiable
with no real charge (§4.7).

---

## 4. Hosted architecture (review H + round 1)

### 4.1 Showstopper checks

| Check | Finding | Consequence |
|---|---|---|
| Unofficial Shazam from a server | ~20 req/min **per IP**, shared by every user → ~40–65 hour-long mixes/day per IP; past the throttle the adapter records *failures* (E-S1) and a run ends thin; datacenter ASNs escalate to blocks; Apple's terms forbid automated access; no official server API at any price | **D1: the owner keeps Shazam for the free tier with the risk accepted.** Mitigations: per-account weekly cap; global daily request budget per egress IP; circuit breaker → "free scans are busy" rather than a degraded result; decode errors treated as throttle signals; provider kill-switch; extra datacenter egress IPs (~$1–2/month each) as capacity; config fallback to sparse AudD; **never residential-proxied** |
| yt-dlp from a server | SoundCloud/Mixcloud fetch fine from a VPS; **YouTube** needs residential egress (~65 MB/mix → $0.15–0.50) plus Deno + `curl-cffi`; the whole category runs "you assert the rights, we don't re-host, audio is discarded" | **D4: YouTube kept.** Per-platform egress; global proxy budget; upload fallback; refund on fetch failure; spike S2 before Phase 6a |
| AudD terms | pricing public; DJ sets an explicit use case; production terms unreadable; trial licence requires attribution | **L1** (owner) before any price is published; attribution slot in config |
| Disk | 330–370 MB/hour, 10–25 MB durable | retention at terminal success only (§4.6) |
| RAM | `novelty.py` ~0.7 GB per hour of audio | guarded off (Phase 0b); 1.5 GB budget per job; length caps |

### 4.2 Shape

```
Internet ─:443─▶ Caddy (TLS, gzip, 250 MB body cap, security headers) ─▶ uvicorn · idea_web (FastAPI)
                                                                          │ pages, auth/sessions, CSRF/Origin, quota+USD reservation,
                                                                          │ billing provider, POST /analyse → jobs row, status polling,
                                                                          │ result routes (authz + traversal-safe) over work/**/present/<bundle>/
                                                                          ▼
                                                                     app.db (SQLite WAL; Litestream → object storage)
                                                                          ▲ lease / heartbeat / checkpoints / progress
                                                                     idea_web.jobs.worker (separate process; N=1 to start)
                                                                          │ ingest policy (platform allow-list, SSRF guard, egress per platform, upload ids)
                                                                          │ id_detector.service.analyse(recipe) ← the pipeline (public API)
                                                                          │ retention GC after terminal success
                                                                          ▼
                                                                     work/<source>/<media>/  (content-addressed; durable subset backed up with the DB)
```

Rules: the web process never runs a pipeline; the worker never serves HTTP; workers never receive
user-controlled filesystem paths (targets are discriminated: platform URL | opaque upload id); billing
touches the app through four methods. `src/id_detector/` changes after Phase 3 are limited to the public
service API, the `serve` entry point and `PROJECT_ROOT`.

### 4.3 Web layer

FastAPI + Jinja2 + uvicorn (an ADR in spike S4 records why not Django/allauth — it is a close call; if the
ADR flips, Phase 4c shrinks). `src/idea_web/` is added to the wheel (`pyproject` packages). `idea serve`
launches the same app in local mode; `present/server.py` is retired after parity. `present/page.py`,
`theme.py`, `exports.py` keep rendering artefacts; CSS/JS move to static files with cache headers. Progress
stays polling (2.5 s). Hosted render policy: the audio element is never emitted; local render policy keeps it.

### 4.4 Accounts, sessions, abuse controls

argon2-cffi; server-side sessions (`__Host-idea_session; HttpOnly; Secure; SameSite=Lax`, `sha256(token)`
stored, rotation on login and plan change, 30 d idle / 90 d absolute); single-use hashed email tokens;
verification **required**; CSRF synchroniser tokens + Origin check on every state-changing POST
(`/webhooks/stripe` exempt — signature-authenticated); login/reset/signup/analyse per-IP rate limits with
IPv6 normalisation and trusted-proxy config; disposable-email list (soft fail); optional Turnstile;
security headers/CSP; export endpoints rate-limited tighter than pages. Email provider interface
(Postmark/Resend/SES); `console` backend allowed only when `IDEA_MODE=local` or in tests — **hosted mode
refuses to start with console email**.

### 4.5 Data model (SQLite WAL; SQL migrations with rollback scripts)

`users` · `sessions` · `email_tokens` · **`media`** (media_key, duration_ms, first_seen) ·
**`source_aliases`** (url/canonical_url → media) · **`analysis_runs`** (media, recipe_id, status, attempts,
checkpoints, requests, usd_e2, started/finished) · **`result_bundles`** (run → versioned bundle path,
manifest hash, presentation version) · **`library_items`** (user ↔ media, added_at, deleted_at) ·
**`publications`** (media → public catalogue entry, published_by, takedown state) · **`shares`** (token ↔
media/bundle, revoked) · `jobs` (queue: target, recipe, user, lease, heartbeat, attempt, dead-letter state,
cancel flag, progress JSON, log) · **`quota_ledger`**, **`usd_ledger`**, **`credit_ledger`** (append-only,
§3.4) · `entitlements` (user, plan, status, period_end, cancel flags, stripe ids) · `entitlement_grants`
(admin comps — separate from Stripe-managed rows) · `processed_stripe_events` (event_id PK) ·
`paid_spend` (run, provider, requests, usd_e2). One globally cached mix can sit in many users' libraries;
publication is explicit, never automatic.

### 4.6 Jobs, retention, backups, sizing

Worker: lease with heartbeat, **phase checkpoints + attempt counter** (max attempts → dead-letter), idempotent
artefact commits, durable request/ambiguous-response records so a crash between network acceptance and cache
commit does not re-bill, cancel/drain semantics, **duplicate-work coalescing** (same media → attach to the
running job). Retention **only after terminal success** (final re-fuse + presentation + job commit): drop
`windows/**`; keep `decode/audio.pcm` 48 h; keep `ingest/original.*` 7 days in hosted mode (local mode keeps
it — the owner's player uses it); keep `present/<bundle>`, `fuse/`, `hints/`, `recognise/`, `ingest/source.json`
+ manifest. **`_load_cached` is changed to validate from the manifest and recorded media key, not by hashing
the original.** Backups: Litestream for `app.db` **and** the durable artefact subset (rclone to object
storage); a restore drill covers both. Server: Hetzner CX/CPX-class VPS (~€16/month); image
`python:3.12-slim` + uv + ffmpeg + Deno + `yt-dlp[default,curl-cffi]`, no JDK, dev tooling excluded;
ffmpeg/ffprobe run with CPU/memory/time limits; uploads stream to per-job temp dirs with byte/time/disk
caps. Rough cost: $65–75/month at 100 mixes, $370–550 at 1,000 (AudD + proxy dominate).

### 4.7 Billing (test mode; verifiable with no charge)

`BillingProvider` — `create_checkout_url(user, kind, success, cancel)`, `portal_url(user, return)`,
`handle_webhook(raw_body, sig)`, `current_entitlement(user)` — **no price id from the client**.
`LocalBilling` (`local`): `/dev/billing/*` simulate pages (success / decline / renewal / failed renewal /
cancel / refund), banner, refuses to boot in production. `StripeBilling`: Checkout `mode=subscription`
(Pro, monthly + annual prices) and `mode=payment` (packs) created server-side with `client_reference_id`
from the session and `subscription_data.metadata.app_user_id`; Billing Portal; webhooks
`checkout.session.completed`, `checkout.session.async_payment_succeeded|failed` (packs granted only on
confirmed paid status), `customer.subscription.{created,updated,deleted}`, `invoice.payment_failed`
(notify; don't revoke), `charge.refunded` / `charge.dispute.created` (reverse pack credits; flag
entitlement); raw-body signature verification; `event_id` dedupe; re-fetch on every event; grant on
`active|trialing`, revoke on `canceled|unpaid`, 7-day grace on `past_due`; Stripe Tax enabled, prices
VAT-inclusive GBP. Dev loop: `stripe sandbox create` keys (accept `rkcs_test_`), `stripe listen
--forward-to localhost:8000/webhooks/stripe`, test cards 4242 / 0341 / 3155, test clocks with the exact
command sequence and expected state transitions committed in `docs/billing-testing.md`.

### 4.8 Hosted-mode boundary

Targets are platform URLs from an allow-list (SoundCloud, YouTube, Mixcloud) or opaque upload ids — never
paths; every fetch is guarded against SSRF (private/reserved addresses, DNS rebinding, unsafe redirects);
uploads never trust filenames/MIME; result and export routes authorise by ownership/share/publication and
are traversal- and symlink-safe; originals are never served; `/rescan` does not exist; results are **private
by default** with explicit publish; the public catalogue has takedown ownership, corrections and a DMCA
route; one privacy sentence everywhere: *"We fetch the audio to analyse it and delete it within 7 days;
only short clips go to the recognition engines; the tracklist is kept."*

---

## 5. Phases

Every cycle: Codex (gpt-5.6-sol, xhigh) builds from this plan → `uv run pytest -q` · `uv run ruff check .`
· `uv run ruff format --check .` · `uv run python scripts/audit_fixtures.py` green → Codex reviews the diff →
fixes → one commit on `main`. Gates use **committed fixtures and exact commands**; owner-only live checks are
`live`-marked and named separately. The local browser workflow must pass its gate at every boundary.

### Phase S — spikes and owner gates (parallel; no product code)
- **S1 (owner):** AudD production terms + the rate for the per-clip endpoint (browser-read `/terms`, or email
  api@audd.io). Blocks publishing prices (L1), not the build.
- **S2 (owner + script):** YouTube + SoundCloud + Mixcloud fetch from a throwaway VPS, direct and via one
  residential proxy — `scripts/spike_ingest_vps.sh` with a written result in `docs/spikes/ingest-vps.md`.
  Blocks Phase 6a.
- **S3 (owner + script):** Shazam throttle behaviour from the same VPS using the existing calibration probe —
  req/min ceiling, behaviour past it, block escalation over a week. Sizes the free-tier budget (§4.1). Blocks
  Phase 4d's budget numbers, not its code.
- **S4:** ADR — FastAPI vs Django/allauth, one page, decided before Phase 4a.

### Phase 0a — Emergency correctness, spend, security (local)
E-C1, E-C4, E-H1, E-H2, E-H3, E-H8, E-H9, E-M5, E-M9, U-F5 (loopback CSRF/Origin).
- Bind `matches` from `gen0_observations`; free-engine failure count only when the free branch ran.
- Typed cache states (§3.4) — cache written only after a successful parse; read path rejects non-success;
  selective `--refresh` by state; adapter version in the key.
- `run_paid_clip_recognition` returns `requests`, `usd_e2`, `status`; `max_paid_clips` honoured in every mode;
  `max_usd_e2` in `AppConfig` (profile budget flows through); real spend journaled; `_refuse_with`
  accumulates counts.
- Unavailable/erroring paid engine **fails the job** with a plain reason (CLI `--allow-degrade` keeps the
  old behaviour, explicitly).
- Drop `upload_consent` from the POST handler; remove the `run_paid_scanners` call site (scan path
  unreachable); **wire `local_index_label` through the web runner so "build index" is queried (D3)**.
- CSRF token + Origin check on the loopback `/analyse` and `/rescan` POSTs.
- `_windows_in_spans` skips transformed windows (E-M9).
- **Tests:** fast `_analyse` integration tests with a fake AudD adapter (`paid_scan_adapters`) and a tiny
  synthetic WAV for: paid success; paid *no-match*; all-errors fallback (must fail, and must degrade with the
  flag); partial errors; cancellation mid-paid-pass; cached-vs-live cap; count accumulation; error bodies
  never cached; spend journaled and capped; the two full-pipeline `slow` modules gain a fast smoke variant.
- **Gate:** all new tests green; `uv run idea analyse <fixture.wav> --engine audd` (fake adapter via env)
  exits 0 and journals `usd_e2 > 0`.

### Phase 0b — Adapter reliability, effective config, provenance
E-H4, E-H5, E-H6, E-M2, E-M10, E-S1, E-C3 (anchor part).
- AudD clip parser: **anchor from `timecode`** (reuse the enterprise parser's logic), `simultaneous_source="audd"`.
- Paid pass: progress per clip (cancellable), concurrency 3–4 + token bucket + retry/backoff; ambiguous
  outcomes recorded in a sidecar and retried at most once, never blindly re-billed.
- Config carry-over under profiles in both `cli.analyse` and `runner._resolve_settings`; `config show`
  prints the *effective* config (given a profile).
- Novelty guarded on `max_generations > 0` and hoisted out of the re-fuse loop.
- Shazam adapter: decode errors count as throttle signals for the limiter; runs record failed-window count
  and a `degraded` status; page header shows run status and engines that ran.
- Fuser told the true scanned window set in paid-first (E-M10).
- **Gate:** integration tests for cancellation timing, retry-without-rebill, effective-config; a
  golden **Local Free pipeline output fixture** is committed now (`tests/golden/local-free/`) — Phase 1 must
  reproduce it byte-for-byte for the free recipe.

### Phase 1 — Deep scan v2 (§2.3) + recipes
E-C2, E-C3, E-M1, E-M3, E-M4, E-M6, U-F13.
- `AnalysisRecipe` (immutable, hashed) + `recipe_id` in every result and manifest; capability-superset
  cache rule; lesser-never-overwrites-greater; Deep upgrade runs only the secondary pass.
- Targeting v2 (priority order, per-episode representative, duration-proportional allocation, intersection
  window selection, duration-scaled cap, 20–30 % confirmation reserve).
- Corroboration on selected votes with temporal overlap and trust families; display vs bypass thresholds.
- Run status semantics + circuit breaker (no mid-run swap).
- Crowd rows: plausible `Artist – Title` only, no comment text, contradictory answers → one listed + alternative.
- Web: `max_accuracy` → the `deep` recipe; CLI `--recipe deep|deep-half|deep-economy|free`.
- **Tests:** committed **recorded-response regression fixture** (anonymised per `scripts/audit_fixtures.py`)
  built from the C3 scenario: 7 Shazam windows + 7 AudD agreements keep `likely` and gain
  `engine_corroborated`; a single cross-family coincidence does not bypass the floor; hint_only rows are
  targeted first; cap scales with duration; golden Local Free output unchanged. Owner-only `live` smoke:
  `uv run pytest -m live tests/test_live_deep_holly.py` (cached AudD raws) reports ≥ 12 cross-checked and no
  `likely` lost vs free.
- **Gate:** the above green; `docs/recipes.md` documents each recipe's cost formula.

### Phase 2a — Startup and runtime cleanup (non-destructive)
E-L1, E-M7, E-L4.
- Lazy imports of `benchmark/`, `calibrate/`, `truth`; `dev` extra.
- `_load_cached`: manifest-based validation (no original required) + a URL→media index.
- ACRCloud and `tl1001` default-disabled; `--engine acrcloud` refused with a message.
- **Gate:** `uv run python -X importtime -c "import id_detector.cli"` total < 1.5 s on the committed
  benchmark script `scripts/measure_startup.py` (cold, 3 runs, median); cached-open works with the original
  deleted (test).

### Phase 2b — Retention and deletions
E-M8, §2.6 deletions.
- Retention at terminal success only; `--keep-intermediates`; `idea gc` with the hosted/local policies;
  artefact manifest listing durable files + sizes.
- Delete `scan.py` whole-file path, ACRCloud, `tl1001`, their tests; archive tag `pre-v2-removals`.
- **Gate:** on the committed 60-minute synthetic fixture, `idea gc --policy hosted` leaves ≤ 25 MB per the
  manifest (test); frozen profiles still re-derive byte-for-byte; golden Local Free output unchanged.

### Phase 3a — Canonical projection, honesty, accessibility (result page + exports)
U-F2, U-F3, U-F7, U-F8, U-F10, U-F11, U-F12, U-F14, U-F19, U-F20, U-F31, U-F4, version-column rule.
- One typed entry list → page, Copy, CUE, Markdown, JSON, library card; **typed comparison test** across all
  projections on committed `tests/fixtures/present/*` (tracks vs gaps compared separately — CUE emits gaps as
  `TRACK` rows).
- Atomic versioned bundle + manifest pointer (`refresh.py` migrates a stale page to a new bundle, never
  rewrites in place).
- Dead columns/controls/counter/rescan/M3U/HINT pill/layer/ID-gap tile removed; version column conditional.
- Crowd rows honest; contrast + table semantics; stacked cards ≤ 720 px keeping acquire chips; one privacy
  line; run status in header.
- **Gate:** projection test green; `node --check` on extracted scripts via `scripts/check_page_js.py`; no
  banned strings (`unverified`, `no_evidence`, `episode`, `prediction interval`, `suppressed`, `window`) in
  rendered user-facing HTML (test); owner-only screenshot script `scripts/screenshot_pages.ps1` documented.

### Phase 3b — Visual polish
U-F16, U-F17, U-F21–F28, U-F35, library date, glossary, tiles, chips, lead-in, alts, player styling.
- **Gate:** `PAGE_VERSION` bumped; page tests green; screenshots refreshed under `docs/screenshots/v2/`.

### Phase 4a — Web parity port + public pipeline API
- `id_detector.service` (`analyse(target, recipe, progress) → RunResult`, `acquire(...)`) used by CLI and web.
- `src/idea_web/` (packaged), FastAPI + Jinja2, templates ported 1:1; routes parity; static assets + gzip +
  cache + security headers/CSP + Origin checks; discriminated targets + SSRF-safe URL validation; `idea
  serve` → idea_web local mode; `PROJECT_ROOT` fixed (E-L2); server-side UI fixes U-F1, U-F15, U-F18, U-F30,
  U-F34; `present/server.py` retired.
- **Gate:** route parity tests; **Windows gate** `scripts/gate_local_mode.ps1` runs `idea.cmd`, opens a cached
  mix, plays audio (owner-run, documented); `uv run idea serve --no-open` smoke test in CI.

### Phase 4b — Durable queue, worker, minimum operations
- `jobs` table + worker process per §4.6 (checkpoints, attempts, dead-letter, coalescing, cancel/drain,
  idempotent commits, durable ambiguous records); progress rows; wall-clock progress (U-F9); job-complete
  email hook; structured logs with redaction; paid-spend metrics; disk alarm; `/healthz`; restore drill
  script (db + durable artefacts).
- **Gate:** kill the worker mid-job → restart → job resumes from checkpoint without re-billing (test with the
  fake adapter); two submissions of the same media coalesce; restore drill passes on a fixture.

### Phase 4c — Auth and security
- Users/sessions/email tokens; signup/login/logout/verify/forgot/reset; verification required; email
  provider interface (console only in local/tests); CSRF; rate limits; disposable-email list; Turnstile
  optional; security tests: session fixation, XSS via titles/comments in rendered pages, CSRF, rate limits.
- **Gate:** security tests green; hosted mode refuses console email.

### Phase 4d — Tenancy, cache ownership, quotas, sharing
- Data model §4.5; ledgers §3.4 with atomic reservation before enqueue; capability-superset cache; Deep
  delta charge; refunds on failure with abuse ceilings; length caps; usage meter + capped-state prompt;
  private-by-default + explicit publish; share tokens + OG; per-user library + delete; `idea admin`.
- **Gate:** two users cannot see each other's private mixes; concurrent submissions cannot exceed balance
  (test); N+1th new analysis blocked with prompt while a cached link opens; Standard-then-Deep charges only
  the delta; share link opens logged-out; publication requires the explicit action.

### Phase 5a — Billing `off` / `local`
- `BillingProvider`, entitlements from ledgers, `entitlement_grants` for admin comps, LocalBilling simulate
  pages, pricing page (numbers from config; hidden until L1), plan gates.
- **Gate:** full upgrade/renewal/failure/cancel/refund flow through the UI with zero network.

### Phase 5b — Stripe (sandbox)
- StripeBilling per §4.7; `docs/billing-testing.md` with the exact `stripe` CLI + test-clock sequence and
  expected transitions; live-mode guard.
- **Gate:** with `stripe sandbox create` keys + `stripe listen`: 4242 → Pro; 0341 → renewal fails → past_due
  → grace → revoke; cancel at period end; pack purchase (sync and async) adds credits; refund reverses;
  webhook replay is a no-op; client-supplied price id ignored — **no real charge anywhere**.

### Phase 6a — Ingest hardening, container, backups
- `ingest_policy.py` (allow-list, per-platform egress, proxy only for YouTube, global proxy budget); upload
  fallback (streamed, capped, ffprobe with limits, opaque ids); fetch-failure refund; Docker/compose
  (web, worker, Caddy) + Litestream + artefact backup; secrets via env file; migration rollback test.
- **Gate:** `docker compose up` on a clean machine serves through Caddy; an **authorised** SoundCloud test
  source analyses end-to-end inside the container; a 5-hour job is refused; restart during a job recovers;
  restore drill from backups passes.

### Phase 6b — Private-beta launch checklist
- Policy pages (ToS modelled on the category's posture, privacy, DMCA/takedown, "how it works and what it
  can't do"); attribution slot; `/admin` (queue, quotas, spend, disk, audit log); runbooks (provider outage,
  disk, restore, job drain); provider circuit-breaker test; 3-platform canary; `docs/LAUNCH.md`.
- **Gate:** `docs/LAUNCH.md` complete except the owner-only launch gates (§6.3); private beta runs with
  `BILLING_BACKEND=off` and admin grants.

---

## 6. Owner decisions, launch gates, risks

### 6.1 Owner decisions recorded 2026-09-09 (these are constraints on the plan, not open questions)

| # | Decision | Accepted trade-off / mitigation |
|---|---|---|
| **D1** | **The hosted free tier runs on unofficial Shazam** ("there must be a way to use Shazam for free"). Round 1's P0 #2 (remove Shazam from hosted defaults) is **declined by the owner**. | Legal/ToS exposure accepted by the owner; capacity ≈ 40–65 mixes/day per egress IP; mitigations in §4.1; engine roles remain config so the default can flip to sparse AudD without code |
| **D2** | **Deep scan = AudD sweep first, Shazam second** (speed). | $0.80–2.00 per hour of audio; cost table §3.3 decides allowances after L1 |
| **D3** | **Panako stays** and the web wiring is fixed. | JDK not in the hosted image until needed |
| **D4** | **YouTube stays** as a hosted source. | residential egress for YouTube jobs (~$0.15–0.50/mix) under a global budget; S2 spike first |
| **D5** | **Both a subscription and non-expiring packs.** | one billing code path; sizes from config |
| **D6** | Positioning: consumer product as the free/cached-catalogue acquisition layer; the pro/report path kept open at zero extra cost (provenance, version axis, evidence bundle stay in the data model). | funding the certification corpus is a later owner call |

### 6.2 Open questions (defaults set)

1. Free allowance: 150 min/week (≈ "one mix a week") — config.
2. Free mix-length cap 150 min, paid 240 min — config.
3. Whether standard scans exist on Pro at all (default: no — every Pro mix is Deep).

### 6.3 Launch gates (block a public/paid launch, not the build)

| | Gate | Owner |
|---|---|---|
| **L1** | AudD production terms in writing: consumer resale, caching/retention of results, attribution, concurrency, and the rate for the per-clip endpoint | Nathan |
| **L2** | Legal review of ToS/privacy/DMCA and the ingestion posture (yt-dlp vs YouTube ToS, SoundCloud stream-ripping ban, SoundCloud comment scraping, Mixcloud GraphQL) — `docs/PLAN.md` commercial checklist is the agenda; **the plan keeps these sources enabled pending that review, by owner decision D1/D4** | Nathan + solicitor |
| **L3** | **Real-mix release gate:** ≥ 5 owner-verified mixes (two-pass truth via `idea truth`), recall/precision per recipe reported in `docs/accuracy-report.md`; no accuracy number or "confirmed twice" marketing before this | Nathan (+ tooling exists) |
| **L4** | Stripe: business origin country (irreversible), KYC, live keys, VAT position (Stripe Tax on) | Nathan |
| **L5** | Domain, transactional email provider, backups verified by a restore drill | Nathan + Phase 6 |
| **L6** | Private beta ≥ 2 weeks on admin grants before `BILLING_BACKEND=stripe` | — |

### 6.4 Risks

| # | Risk | L / I | Mitigation |
|---|---|---|---|
| R1 | Shazam changes the endpoint or blocks the server IP | medium / high (free tier stops) | kill-switch; circuit breaker; config flip to sparse AudD; Deep is AudD-first so paying users are unaffected |
| R2 | YouTube blocks datacenter fetches | high / medium | per-platform residential egress; upload fallback; refund on fetch failure; S2 spike |
| R3 | Only the walk-up AudD rate is available | medium / medium | half-density recipe; allowances from the §3.3 table; per-run/account/global USD ledgers |
| R4 | Willingness to pay is weak; Beatport bundles Track ID free | high / medium | tiny fixed costs; free library as SEO; private beta before any spend on marketing; pro path as hedge |
| R5 | Frozen-profile byte test blocks engine changes | certain / low | Deep is a runtime recipe; profiles untouched; golden Local Free output pins behaviour |
| R6 | A phase is too big for one build+review cycle | medium / low | fifteen cycles with named gates; split further if a cycle exceeds ~1 day |
| R7 | Disk fills | certain without 2b / high | retention at terminal success; manifest-measured gate; disk alarm |
| R8 | A paid run fails mid-way after spend | medium / medium | refunds; durable request records; no blind re-bill; failed spend counted for abuse |
| R9 | Free-tier abuse | medium / medium | ledgers with reservation; per-IP limits; verification; caps; global Shazam/proxy budgets |
| R10 | AudD production terms (attribution/resale) unknown | certain / medium | L1; attribution slot in config |
| R11 | Throttled Shazam ends runs thin without saying so | certain today / high | E-S1 fix: failures counted, `degraded` status shown, circuit breaker |
| R12 | A Standard run overwrites a Deep result; cache serves the wrong recipe | certain today / medium | recipes + result bundles + superset rule (Phase 1) |

---

## Appendix A — findings register

| ID | Severity | One line | Phase |
|---|---|---|---|
| E-C1 | Critical | paid-first crashes after any parseable AudD result | 0a |
| E-C2 | Critical | paid-first removes designed engine overlap | 1 |
| E-C3 | Critical | AudD no anchor / shared trial group → downgrades | 0b (anchor) + 1 (trial source) |
| E-C4 | Critical | AudD error bodies cached indefinitely | 0a |
| E-H1 | High | no spend cap / record | 0a |
| E-H2 | High | gap-fill counts zeroed | 0a |
| E-H3 | High | silent paid→free downgrade | 0a |
| E-H4 | High | paid pass uncancellable, ETA 1 s | 0b |
| E-H5 | High | paid pass sequential/unretried, re-bills | 0b |
| E-H6 | High | config dropped under a profile | 0b |
| E-H7 | High | hosted blockers | 4a–4d, 6a |
| E-H8 | High | `upload_consent` POST → whole-file scan | 0a |
| E-H9 | High | web build-index never queried | 0a (wired) |
| E-M1 | Medium | corroboration ignores families/overlap | 1 |
| E-M2 | Medium | novelty for nothing (+ RAM) | 0b |
| E-M3 | Medium | phantom blocks free pass | 1 |
| E-M4 | Medium | crowd rows order/duplicates | 1 |
| E-M5 | Medium | `--refresh` re-bills silently | 0a |
| E-M6 | Medium | CLI `max_accuracy` == free | 1 |
| E-M7 | Medium | `_load_cached` O(n) + needs original | 2a |
| E-M8 | Medium | 77 % of disk disposable | 2b |
| E-M9 | Medium | `_windows_in_spans` ignores transforms | 0a |
| E-M10 | Medium | fuser told whole mix was Shazam-scanned | 0b |
| E-S1 | High | throttled Shazam → silent thin runs | 0b |
| E-L1 | Low | import tax | 2a |
| E-L2 | Low | `PROJECT_ROOT = cwd` | 4a |
| E-L4 | Low | `--engine acrcloud` silently whole-file | 2a |
| E-L5 | Low | discount attribution alphabetical | 1 |
| U-F1 | Critical | bad URL → raw JSON | 4a |
| U-F2/F3/F14 | Critical | counts/exports/page disagree; write-on-GET | 3a |
| U-F4 | Critical | buy links hidden on phones | 3a |
| U-F5 | Critical | no users/CSRF; global library | 0a (CSRF) + 4c/4d |
| U-F6 | Critical | original audio served (hosted) | 4a render policy |
| U-F7/F8 | Critical | noise reveal; dead columns | 3a |
| U-F9 | High | progress arithmetic | 4b |
| U-F10 | High | acquire column empty / search-as-store | 3a |
| U-F11/F12/F13 | High | rescan button; false footer; comment text as track | 3a / 3a / 1 |
| U-F15/F18/F30/F34 | High/Med | failure copy; in-progress cards; auto-redirect; duplicate form | 4a |
| U-F16/F17/F21–F28/F35 | Med | badges, legend, tiles, chips, player, library, alts, lead-in, options summary, copy | 3b |
| U-F19/F20/F31 | High/Med | contrast; table semantics; tokens | 3a |
| U-F29 | Med | HTTP/1.0, no gzip | 4a |
| U-F33 | Med | failed runs invisible | 4b |
| U-F39 | Low | share link / OG | 4d |
| U-F41 | Low | BPM/key/artwork | later (Pro candidate) |
| Round-1 P0 #1 | — | AudD terms before prices | L1 |
| Round-1 P0 #2 | — | Shazam out of hosted defaults | **declined — D1** |
| Round-1 P0 #3–#9, P1 #10–#16, P2 #17–#18 | — | folded into §2.3, §3.3–3.5, §4, §5 | as marked |

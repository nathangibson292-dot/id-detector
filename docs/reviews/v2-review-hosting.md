# IDea as a hosted product — feasibility review

**Date:** 2026-09-09 · **Scope:** read-only research on `C:/Users/natha/Documents/Music/id-detector` + web research
**Status of claims:** every external claim carries a URL. Things I could not verify are tagged **[UNVERIFIED]**.
**Not legal advice.** The ToS sections are plain-English readings, not counsel.

---

## Executive summary (10 bullets)

1. **The web app is not the hard part. The free tier is.** Migrating the browser layer to a real
   framework, adding accounts, quotas and Stripe is ordinary work (~115–175 h). The thing that
   decides whether this is a business is: *what powers a free analysis on a server you own?*
2. **Shazam-on-a-server is the showstopper, and it fails on capacity before it fails on law.**
   The unofficial endpoint throttles at roughly **20 requests/minute per IP**. That limit is global
   to your server, not per user. A 60-minute mix is ~400 windows ≈ 22 minutes of wall clock, so
   **one server IP can do about 65 hour-long mixes a day, total, for everybody**. Past the wall it
   does not return a clean 429 — it starts returning undecodable JSON, which your pipeline will read
   as "no match". Accuracy degrades silently before throughput does.
3. **A paid, marketed product whose free tier runs on Apple's undocumented endpoint is the
   highest-exposure shape available.** Apple's Media Services terms ban automated access and
   non-Apple clients; ShazamKit's licence bans using the data to build another recognition service.
   No enforcement against a Shazam-API project was found — but "no evidence found" is not "safe",
   and being attributable removes the anonymity that is doing most of the work today.
4. **Recommended fix: don't ship a Shazam-powered free tier.** Free = browse the shared library of
   already-analysed mixes + one welcome analysis. Every *new* analysis is AudD-backed
   (**$0.60–$1.50 per 60-min mix** on their enterprise endpoint, which bills 1 request per 12 s of
   audio and names DJ sets as a use case). That kills the capacity wall and the legal exposure in one
   move, and it is exactly what every hosted competitor already does.
5. **yt-dlp from a server is survivable, not a showstopper.** SoundCloud and Mixcloud are fine
   direct from a VPS (SoundCloud's own limit is ~600 req/10 min per IP and yt-dlp self-heals the
   client_id; Mixcloud needs `curl-cffi` impersonation). **YouTube is the problem** and needs a
   residential egress path — budget **$0.15–$0.50 per mix** in proxy bandwidth, only on YouTube jobs.
6. **Two direct competitors already do server-side link fetch openly and commercially.**
   [setlist.id](https://setlist.id/) states in its own ToS that it extracts audio from submitted
   URLs, is ACRCloud-backed, and charges $1.67–$2.00 per set. [set79](https://set79.com/) is
   SoundCloud-only — which sidesteps YouTube entirely. TrackSniff does **link + upload fallback**.
   Copy setlist.id's policy of refunding the credit when a fetch fails; it converts your worst
   failure mode into a non-event.
7. **Measured disk: ~330–370 MB per hour of audio, of which only ~10–25 MB is the durable result.**
   Your `work/` tree is 4.9 GB across 11 mixes. A retention policy that drops
   `ingest/original.*`, `decode/audio.pcm` and `windows/` after a successful run reclaims **~95%**.
   Without it, 1,000 mixes/month fills a 160 GB disk in about two weeks.
8. **Measured RAM: the constraint is `novelty.py`, not ffmpeg.** `_read_pcm` materialises the whole
   mix as float64 — 460 MB per hour of audio, ~690 MB peak with the transient int16 copies. A
   60-min mix peaks near 0.7 GB; a 3-hour mix near 2.1 GB. Size for **~1.5 GB per concurrent job**
   and cap mix length.
9. **Migrate the web layer to FastAPI/Starlette + uvicorn, and move jobs out of the web process.**
   Today `webapp/jobs.py` holds every job in a dict in the server process with a single worker
   thread — a restart loses everything, and 10–40 minute jobs must survive restarts. The good news:
   `src/id_detector/jobs.py` already contains a proven SQLite queue with leases, heartbeats and
   `recover_startup()` requeueing. Copy that pattern up a level for the app queue.
10. **Stripe test-mode-first is genuinely easy, and you can now build it before the account exists.**
    `stripe sandbox create` provisions working test keys with **no Stripe account at all** (7-day
    expiry, claimable). Combine that with a `BILLING_BACKEND=none|stripe` flag and a `LocalBilling`
    implementation, and the whole site is testable from day one with zero real money.

---

## Showstopper risks

### R1 — Shazam from one datacenter IP (severity: **blocking as designed**)

Two independent failures, either of which sinks a free tier.

**(a) Capacity.** The best-attested per-IP figure is **~20 requests/minute, then HTTP 429** —
[Pyzam author, Hacker News, Apr 2024](https://news.ycombinator.com/item?id=40143255). Your
`TokenBucket` default of 18 req/min (`src/id_detector/shazam.py:59`) sits right on it, which
suggests it was tuned empirically and is correct. But that ceiling is **per IP, shared by all your
users**, and `recognise.py:64` sets `DEFAULT_CONCURRENCY = 1`. At 400 windows for a 60-minute mix:

| | value |
|---|---|
| windows per 60-min mix (12 s / 9 s hop) | 400 |
| wall clock at 18 req/min | ~22 min |
| hour-long mixes per day, **whole server** | **~65** |
| hour-long mixes per month | ~2,000 — at 100% duty cycle, no bursts, no headroom |

**(b) Degradation, not failure.** From
[ShazamIO issue #120](https://github.com/shazamio/ShazamIO/issues/120): under sustained pressure
"I start getting '429' errors, but in reattempts, I start getting mostly **'Failed to decode json'**
errors" — even with 12 retries and 205 s backoff. SongRec shows the same signature
([#164](https://github.com/marin-m/SongRec/issues/164)). Your adapter treats 429/503 as a throttle
(`shazam.py:183`) but a malformed body will surface as a non-match, so **your accuracy silently
drops before your throughput does**. That is the worst possible failure mode for a product whose
selling point is honesty.

**(c) Datacenter IPs are a worse profile than a laptop.** No API key is involved, so throttling is
purely IP reputation. A single VPS doing thousands of 12 s fingerprint POSTs a day from a known
cloud ASN looks nothing like a phone. One SongRec user reports blocks escalating "from occasional to
permanent over 2-3 weeks" while the phone app kept working
([#222](https://github.com/marin-m/SongRec/issues/222)) — one data point, but the right shape.

**(d) Legal exposure.** [Apple Media Services T&Cs, eff. 15 Sep 2025](https://www.apple.com/legal/internet-services/itunes/us/terms.html)
prohibit "any software, device, automated process… to scrape, copy, or perform measurement,
analysis, or monitoring of, any portion of the Content or Services" and say "You may access our
Services **only using Apple's software**". ShazamKit's licence separately forbids using the data
"for the purpose of improving or creating another audio recognition service"
([clause text via Law Insider](https://www.lawinsider.com/clause/shazamkit) — **[UNVERIFIED]**
against Apple's live PDF). There is **no official server-side Shazam API**: ShazamKit is an
on-device SDK for Apple platforms + Android, enabled as an App ID capability
([developer.apple.com/shazamkit](https://developer.apple.com/shazamkit/),
[enabling it](https://developer.apple.com/help/account/services/shazamkit/)). Every RapidAPI/Zyla
"Shazam API" is an unofficial reseller — [Zyla](https://zylalabs.com/api-marketplace/music/shazam+api/2219)
charges ~£11/1,000 requests for the same endpoint, ~3× AudD, with none of the contractual cover.
On the law: [hiQ v. LinkedIn](https://www.jenner.com/en/news-insights/publications/client-alert-data-scraping-in-hiq-v-linkedin-the-ninth-circuit-reaffirms-narrow-interpretation-of-cfaa)
and [Meta v. Bright Data](https://www.fbm.com/publications/major-decision-affects-law-of-scraping-and-online-data-collection-meta-platforms-v-bright-data/)
both say anonymous logged-out access to a public endpoint is a contract question, not a computer
crime — and shazamio sends no key and no account, which is the winning fact pattern. What changes
when you charge money is that you become discoverable, attributable, and describable as "another
audio recognition service". Realistic enforcement ladder: silent throttle → ASN block → endpoint
change → cease-and-desist. A lawsuit is unlikely; the first three are near-certain at scale.

**Recommended mitigation (in order of preference):**

1. **Ship no Shazam-powered free analysis.** Free tier = the shared library of already-analysed
   mixes (which costs you nothing — see §7) + **one welcome analysis** on signup, AudD-backed at
   ~$1.50 customer-acquisition cost. Paid tier = N analyses/month, AudD-led, Shazam used only as a
   *gap filler inside a paid job* at a fraction of the request volume. This is what
   `runner.py` already does in `max_accuracy` (`primary_engine="audd"`), so the code path exists.
2. If a free Shazam allowance is non-negotiable: hard cap it at **1 mix/week/account** *and* a
   global daily server budget (e.g. 20 free mixes/day), with a circuit breaker that halts and says
   "recognition is temporarily unavailable" rather than shipping a thin tracklist. Add explicit
   detection of the malformed-JSON degradation in `shazam.py` and treat it as a throttle signal,
   never as a no-match. Accept and document the exposure.
3. **Do not route Shazam through rotating residential proxies.** It converts "we used a public
   endpoint" into "we evaded a throttle", which is the exact practice named in
   [Reddit v. Perplexity/Oxylabs/SerpApi (SDNY, Oct 2025)](https://searchengineland.com/reddit-sues-perplexity-serpapi-scraping-google-search-data-463681),
   brought under DMCA §1201.

### R2 — yt-dlp from a datacenter IP (severity: **manageable**)

Verdict: **not a showstopper**, but YouTube needs a residential egress path and everything needs a
fallback. See §1 for the full evidence. The mitigation is a three-parter:

- **Link-first with an upload fallback** (TrackSniff's shape). The upload path never breaks and is
  the honest answer for private, geo-blocked, exclusive or DRM'd content.
- **Per-platform egress routing.** SoundCloud and Mixcloud go direct from the VPS; only YouTube goes
  through a residential proxy. This cuts the proxy bill to a fraction of the naive estimate.
- **Refund the credit on fetch failure** (setlist.id's policy). Costs nothing, removes the support
  burden.

### R3 — AudD's production Terms of Service are unread (severity: **must resolve before launch**)

`https://audd.io/terms/` would not render for the researcher on four attempts. What *is* readable is
the [Test License Agreement](https://audd.io/legal/tla/), which is evaluation-only, forbids
"operation of its business… or providing ongoing services to others", and mandates a
**"Powered by AudD Music"** logo. The paid Developer licence presumably differs — AudD publishes a
public rate card and sells on-premise, so a paid product is clearly their business model — but
**whether production attribution is required, and whether caching/reselling results is restricted,
is unverified and materially affects your UI**. Email api@audd.io and get it in writing, along with
a concurrency number.

---

## 1. yt-dlp from a server

### What the repo does today

`src/id_detector/ingest.py:196` shells out to `sys.executable -m yt_dlp` with
`-f ba --write-info-json --no-write-comments --no-playlist -o <tmp>/asset.%(ext)s`, a 7200 s
timeout, **no proxy, no cookies, no PO-token provider, no player-client override**. That is the
right minimal invocation for a laptop and the wrong one for a server. `-f ba` (best audio) is
already correct and is worth ~20× in bandwidth versus pulling video.

### YouTube (2026)

- **Bot walls are real from DC IPs.** yt-dlp's [FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ)
  treats 429/403 as IP-reputation blocks and recommends `--cookies`, `--proxy`, `--source-address`.
  The [PO Token Guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide) says without a token
  "requests… may return HTTP Error 403, or result in your account or IP address being blocked."
  The best real-world write-up is a
  [dev.to post by Maxim Osovsky](https://dev.to/osovsky/i-was-building-a-cloud-video-service-youtube-turned-me-into-an-ip-trafficker-1l9o):
  seven approaches tested, only residential-proxy + yt-dlp worked, and "YouTube allows or blocks
  based on IP reputation **before** even checking cookies." Quantified DC-vs-residential failure
  rates are **[UNVERIFIED]** — the widely-cited "9% vs <1%" comes from
  [issue #16072](https://github.com/yt-dlp/yt-dlp/issues/16072), which maintainers closed with an
  `ai-policy-violation` label; the "20–40% vs 85–95%" figures appear only in proxy-vendor marketing.
  **You must A/B this yourself.**
- **PO tokens.** Bound to session (Visitor ID / Session ID) or video ID, lifetime possibly as short
  as 12 h. The claim that a token must be minted from the *same IP* as the download is
  **[UNVERIFIED]** — it is not in yt-dlp's docs or the bgutil README. Mint through the same exit
  anyway; it is free insurance.
  [`bgutil-ytdlp-pot-provider`](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) (by a
  yt-dlp maintainer) runs as an **HTTP server on port 4416, needs Node ≥20 or Deno ≥2, has prebuilt
  Docker images, and needs no browser** — so it is fully server-deployable. Its own README is
  honest: "does not guarantee bypassing 403 errors or bot checks, but it *may* help."
- **Which clients still work unauthenticated:** `web_safari` (HLS m3u8, no GVS PO token today),
  `android_vr`, `web_embedded`, `visionos`, `tv` (DRM'd without cookies). This moves every few
  releases — **2026.08.19 removed `android_vr` from the defaults and added `visionos`**
  ([releases](https://github.com/yt-dlp/yt-dlp/releases)). Pin a version, rebuild weekly, and run a
  canary.
- **Also needed: a JS runtime.** yt-dlp solves YouTube JS challenges with an external runtime —
  **Deno recommended and default** ([EJS wiki](https://github.com/yt-dlp/yt-dlp/wiki/EJS)).
  PyInstaller builds bundle it; **`pip install yt-dlp` does not**, so your Docker image must add
  Deno explicitly. Your current `pyproject.toml` has `yt-dlp>=2026.8.19` from PyPI — so this applies.
- **Cookies: don't.** yt-dlp's [Extractors wiki](https://github.com/yt-dlp/yt-dlp/wiki/Extractors)
  says outright "By using your account with yt-dlp, you run the risk of it being banned (temporarily
  or permanently)". A single shared account across all users' jobs, rotating and expiring, on a paid
  service, is the clearest possible ToS violation — and per the dev.to report, IP reputation is
  checked first anyway.
- **SABR** is a second, separate breakage vector. The SABR downloader
  ([PR #13515](https://github.com/yt-dlp/yt-dlp/pull/13515)) is still **open**. When web formats get
  forced to SABR, the practical fallback is a low-quality progressive format (itag 18, 360p + AAC) —
  which is **perfectly adequate for fingerprinting**. Worth encoding as an explicit fallback in your
  format string rather than failing the job.
- **ToS:** [YouTube ToS, eff. 17 Mar 2025](https://www.youtube.com/t/terms) prohibits downloading
  any part of the Service, automated access, and non-personal/non-commercial use. Server-side
  downloading for a paid product violates all three. Enforcement reality: no case found of Google or
  a label suing a *track-identification* service; the litigation (RIAA v. youtube-dl 2020;
  [Yout v. RIAA](https://torrentfreak.com/suno-udio-wade-into-youtube-ripper-circumvention-lawsuit-appeal-251008/),
  still on appeal at the 2nd Circuit in 2026) targets rippers that hand the audio to the user. You
  don't. Your realistic exposure is a takedown demand or an IP ban, with no contractual recourse.

### SoundCloud (much easier)

From the extractor source
([soundcloud.py](https://raw.githubusercontent.com/yt-dlp/yt-dlp/master/yt_dlp/extractor/soundcloud.py)):

- **No hardcoded client_id.** `_update_client_id()` scrapes `soundcloud.com`'s JS bundles in reverse
  for `client_id: "…"`, caches it, and on a 401/403 **clears and re-fetches automatically, once**.
  This is why SoundCloud rarely "breaks" for yt-dlp users.
- **Rate limit, verbatim from the source:** *"You have reached the API rate limit, which is ~600
  requests per 10 minutes."* That is ~1 req/s per IP — enormously generous for your workload
  (a handful of metadata calls plus one stream fetch per mix).
- **No evidence of YouTube-style IP-reputation blocking.** A recent 404 bug
  ([#16603, Apr 2026, fixed within days](https://github.com/yt-dlp/yt-dlp/issues/16603)) was
  reproducible from the same IP that worked in-browser — a site change, not an IP block.
- Gated content is handled explicitly: geo-block → `raise_geo_restricted`; private tracks via the
  `secret_token` param (`?s-xxxxx` links work); encrypted `ctr-`/`cbc-` protocols → `report_drm`;
  previews get `preference: -10` so you can detect and reject a 30-second snippet.
- **The official API is now open but paywalled and useless to you.**
  [Registration is open again](https://developers.soundcloud.com/docs/api/register-app) but "You
  need a SoundCloud Artist Pro subscription to register API applications."
  [Rate limits](https://developers.soundcloud.com/docs/api/rate-limits): 15,000 playable-stream
  requests per 24 h per client_id. And the
  [API Terms](https://developers.soundcloud.com/docs/api/terms-of-use) kill it: "Your app must not
  include file-save functionality, or otherwise designed to cache, download or persistently store
  any User Content" and no "stream ripping, stream capture or similar." A transient-buffer-and-delete
  argument exists but "designed to cache" is broad. Lawyer question.

### Mixcloud (technically easiest, legally most explicit)

From [mixcloud.py](https://raw.githubusercontent.com/yt-dlp/yt-dlp/master/yt_dlp/extractor/mixcloud.py):

- Uses the GraphQL endpoint **with `impersonate=True`** — your image needs
  `yt-dlp[default,curl-cffi]` or Mixcloud simply fails.
- **Not DRM.** Stream URLs are base64 + XOR with a hardcoded key. Three format paths (HLS, DASH,
  progressive HTTP); HTTP chunks capped at 5 MB "to avoid throttling."
- **Real per-IP limit:** the extractor raises *"You have reached your play limit for this track"* —
  you will hit this on a shared server, and cheap datacenter proxies are a legitimate fix for it.
- Long-standing 403/Cloudflare issue [#7444](https://github.com/yt-dlp/yt-dlp/issues/7444) was
  **closed Feb 2026**; the extractor is in good shape.
- [Mixcloud T&Cs](https://www.mixcloud.com/terms/) grant a "non-commercial" listen-only licence and
  ban downloading, circumventing security features, and "any robot, spider, scraper." The XOR key is
  literally `IFYOUWANTTHEARTISTSTOGETPAIDDONOTDOWNLOADFROMMIXCLOUD`.

### How hosted competitors get the audio

| Service | Mechanism | Notes |
|---|---|---|
| **setlist.id** | **Server-side link fetch only.** No upload path. | ACRCloud-backed. "+100s more" sites = a yt-dlp fingerprint. $6–$25/mo, $1.67–$2.00/set. **Refunds the credit if identification fails.** |
| **set79** | **Server-side fetch, SoundCloud only.** | Sidesteps YouTube entirely. $8 for 3, or $10.90/mo. 70k-tracklist free library; "you only pay to analyze a set that isn't in the library yet." |
| **TrackSniff** | **Link *or* upload.** | Free 50 recognitions/mo, 90-min cap, 100 MB upload cap; Pro $9/mo. Five confidence tiers — the same vocabulary as IDea. |
| **trackid.net** | Server-side fetch, **gated behind premium**. | Throttles egress cost and abuse surface by making link submission a paid feature. £20/yr. |
| **Aha-Music** | Record / upload / paste URL / browser extension. | ACRCloud front-end; the URL fetch is their own layer. |
| **MixSleuth** | **Uploads only, as a stated legal position.** | And charges **$4.99–$29.99 per *year***. |
| **MixesDB / 1001tracklists** | Crowdsourced. No audio at all. | |

**The pricing collapse is the signal**: the upload-only competitor charges per *year* what the
link-fetch competitors charge per *month*. **Link ingestion is the product.** Uploads are the
fallback that makes it reliable.

`setlist.id`'s [ToS](https://setlist.id/terms) is the template to copy: the user warrants the right
to analyse the URL; "We do not host, store, or distribute the underlying audio content"; results are
"for personal, non-commercial use."

### Practical options

| Option | Pros | Cons | Cost |
|---|---|---|---|
| **A. Links + upload fallback (recommended)** | Link path is the product; upload never breaks and is the honest answer for private/geo/DRM content; credit-refund-on-failure removes support load | Two ingest paths; needs resumable multipart for 200 MB files and a size cap | ~0 |
| **B. Residential proxy for YouTube only** | The only thing that reliably works from a server | Vendor KYC; ethical sourcing diligence; the Reddit v. Oxylabs precedent | see below |
| C. Datacenter proxies | Cheap; genuinely useful for Mixcloud's per-IP play limit and spreading SoundCloud's 600/10min | Same blocklisted ASNs YouTube scores down — useless for YouTube | ~$0.02–$1.80/IP/mo |
| D. Browser-side download | Solves IP reputation permanently, zero bandwidth cost | CORS blocks page-side fetch → needs a Chrome extension, which Google reviews and removes; tab capture is **realtime** (2 h mix = 2 h) vs competitors' 5–10 min | — |
| E. Self-hosted residential exit (home box / Tailscale) | Real residential IP, zero bandwidth cost, no vendor KYC | ISP AUPs forbid it ([Comcast](https://www.xfinity.com/corporate/customers/policies/highspeedinternetaup)); single point of failure; funnelling every customer through one IP rebuilds the exact pattern YouTube flags | €0 |

**Bandwidth per mix with `-f ba`:** YouTube itag 140/251 ≈ **57–72 MB/hour**; SoundCloud Opus
≈ 29 MB/h; Mixcloud 29–57 MB/h. A 60-min mix is **~65 MB**. (The scary "$2.40 per download" figures
in blogs are for video+audio at full quality — ~20× your usage. That difference is why a track-ID
product is viable on residential proxies where a video service is not.)

**Residential proxy pricing, verified 2026, cost per 65 MB mix:**

| Provider | $/GB | Per mix | Note |
|---|---|---|---|
| [Webshare](https://www.webshare.io/pricing) | $3.50 (1 GB) → **$2.25 (100 GB)** → $1.40 (3 TB) | **$0.15** | Cheapest credible; 10 free proxies, no card |
| [Decodo](https://decodo.com/proxies/residential-proxies) (ex-Smartproxy) | $4.00 PAYG → **$2.75 (100 GB)** | **$0.18** | 3-day trial + 100 MB free |
| [SOAX](https://soax.com/pricing) | $3.00 Builder → $0.50 enterprise | $0.20 | **$200/mo hard minimum** |
| [Bright Data](https://brightdata.com/pricing/proxy-network) | $4.00 promo / $8.00 list | $0.26–$0.52 | **KYC-gated since 7 Jul 2026** — corporate email required, Gmail ineligible, and access is scoped to an approved use case ([policy](https://docs.brightdata.com/proxy-networks/residential/network-access)). They may say no. |
| [Oxylabs](https://oxylabs.io/products/residential-proxy-pool) | $6 → $2.50 | $0.26–$0.39 | Named defendant in Reddit v. Perplexity |
| [IPRoyal](https://iproyal.com/residential-proxies/) | $7 (1 GB) → $1.75 (10 TB) | $0.34–$0.46 | **Traffic never expires** — best for lumpy early volume |

**Recommendation:** build egress as a pluggable per-platform setting (`HTTP_PROXY` env var applied
only to YouTube jobs). Start on a home exit node for development and the first customers; swap in
Webshare or IPRoyal the day it matters, without touching application code.

---

## 2. Shazam from a server

Covered under **R1** above. The additional facts worth recording:

- **shazamio is alive and maintained.** `shazamio` 0.8.1 (Jun 2025), repo pushed **8 Sep 2026**;
  `shazamio-core` 1.2.1 (**5 Sep 2026**). Your `pyproject.toml` pins `shazamio-core==1.1.2` and
  `shazamio==0.8.1` — the core is behind. Note that **`shazamio-core` never touches the network**;
  it is Rust fingerprinting only. All the risk lives in `shazamio`.
- **Proxy support in shazamio is broken.** [Issue #139 (open, Apr 2025)](https://github.com/shazamio/ShazamIO/issues/139):
  HTTP proxies throw TLS-in-TLS warnings under asyncio; SOCKS5 unsupported. So even the mitigation
  you'd want is not off-the-shelf.
- **What comparable projects do.** [Tracklistify](https://github.com/betmoar/tracklistify)'s
  defaults are a useful sanity check: **Shazam 25 req/min, concurrency 1**; ACRCloud **300 req/min,
  concurrency 10**. Its architecture assumes Shazam *will* fail and falls back to a paid engine.
  It also runs a per-provider circuit breaker (5 consecutive failures → open, 60 s reset) and a
  30-day result cache — both of which you already have equivalents for.
- **MixesDB's community wiki is blunt**
  ([Help:Tracklist Generation Tools](https://www.mixesdb.com/w/Help:Tracklist_Generation_Tools)):
  "abusing or spamming these unofficial endpoints could result in rate limits — or even a permanent
  ban."
- **No credible competitor runs a *paid* product on the free Shazam endpoint.** TrackSniff's free
  tier shows only the first 3 tracks; Scanamix gives one free mix; trackid.net gates link submission
  behind premium. Those free-tier shapes are what a business that pays per request downstream looks
  like. An unlimited free tier is not a feature they declined to build — it is one the unit
  economics forbid.

**Plain-English ToS reading (not legal advice):** today, on a laptop, an anonymous unauthenticated
client hitting a public endpoint sits on the winning side of hiQ/Bright Data — you never agreed to
anything. Turning it into the engine of a marketed, paid product changes three things: it becomes
discoverable, it becomes attributable to a company, and it becomes describable as "another audio
recognition service" — the exact phrase ShazamKit's licence prohibits. The likely outcome is not a
lawsuit. It is that your free tier gets throttled into uselessness at exactly the moment it starts
working, and that you have no recourse and no notice when it happens.

---

## 3. AudD / ACRCloud for a hosted product

### AudD — the recommended paid path

Pricing from [audd.io](https://audd.io/) (note: `/pricing/` 404s; it lives on the homepage) and
[docs.audd.io/enterprise](https://docs.audd.io/enterprise/):

| Item | Price |
|---|---|
| Free trial | **First 300 requests free**, no card |
| Pay-as-you-go | **$2–$5 per 1,000 requests** |
| 100k/mo | $450 ($4.50/1k) · 200k → $800 · 500k → $1,800 ($3.60/1k) |
| Streams | $45/stream/mo (AudD DB) or $25 (own catalog) |
| **Minimum monthly plan** | **None** — genuinely as-you-go |

**The enterprise endpoint is built for exactly this workload.** It accepts hours-long audio, names
**"DJ sets"** as a use case, and **bills 1 request per 12 seconds of audio** with server-side
chunking. So:

| Path | Requests/60-min mix | Cost |
|---|---|---|
| **AudD enterprise @ $2/1k** | **300** | **$0.60** |
| AudD enterprise @ $5/1k | 300 | **$1.50** |
| AudD standard, your own 500 clips @ $5/1k | 500 | $2.50 |
| ACRCloud @ ~$4.4/1k **[UNVERIFIED]** | 500 | ~$2.20 + mandatory logo |
| Zyla resold "Shazam" | 500 | ~$7 |

Note this is **cheaper than your current clip-by-clip AudD path** because AudD does the chunking and
you stop paying for your own 12/9 overlap. Worth a code change: `providers/audd.py` currently drives
the clip API; the enterprise long-file endpoint would cut request count ~40% on a 9 s hop.

**Rate limits / concurrency:** no published req/s cap. Parallelism is explicitly supported via an
async WebSocket endpoint (`wss://api.audd.io/ws/`) — "send multiple requests… without waiting for the
server's responses". Get a number in writing before sizing a job queue around it.

**ToS:** see **R3** — unresolved and blocking.

### ACRCloud — skip it

- [Pricing](https://www.acrcloud.com/pricing/) publishes **no numbers**; it is console-login-gated.
  Free trial is **14 days**, not request-boxed — worse for bursty testing than AudD's 300 requests.
- [Terms, eff. 15 Sep 2022](https://www.acrcloud.com/terms/) grant a perpetual royalty-free right to
  the metadata but restrict it to **"internal business purposes"**, which sits awkwardly against a
  public consumer product; and the **Brand Exposure programme requires displaying ACRCloud branding
  in all communication**. AudD's equivalent requirement is confirmed only for the *test* licence.
- Your own live test already found it recovered only 1/8 of AudD's misses. The commercial evidence
  agrees with the recall evidence: **a second paid engine is not worth it.**

### Everything else, one line each

| Option | Verdict for DJ-mix ID |
|---|---|
| **AcoustID + Chromaprint** | Wrong tool — matches *near-identical files*, not a track playing inside a mix. But it is the cheapest legal API by an order of magnitude (€50/mo for 1M searches, [acoustid.biz](https://acoustid.biz/)) if you ever need exact-recording lookup. |
| **Panako / Olaf** | Confirms your field finding: only matches what you indexed. Academic evaluation found both fail at short queries ([PMC10028751](https://pmc.ncbi.nlm.nih.gov/articles/PMC10028751/)). **Drop the JDK.** |
| **Dejavu** | Best open-source accuracy in that comparison, but same "bring your own catalogue" problem, and unmaintained upstream. |
| **Emysound** | Self-hostable .NET fingerprinting; Community Edition non-commercial only; Enterprise price on request. Engine only. |
| **Gracenote** | No self-serve, no public rate card, enterprise contract only. Effectively closed. |
| **ShazamKit custom catalog** | On-device only, and its licence bans building another recognition service. Useless server-side. |
| **Human tracklists (your paste-a-tracklist box)** | Validated by every comparison read: catches "edits, VIPs, and unreleased tracks that algorithms miss". Keep and promote it. |

---

## 4. Stripe, test-mode-first

Everything here is from Stripe's docs unless tagged.

### Versions

| Thing | Value |
|---|---|
| `stripe` PyPI | **15.6.1** (2026-09-01) — [pypi.org/project/stripe](https://pypi.org/project/stripe/) |
| Current API version | **`2026-08-26.dahlia`** — [api/versioning](https://docs.stripe.com/api/versioning) |
| Stripe CLI | v1.43.3+ |

### The minimal robust integration

**Checkout Session, `mode: "subscription"`**, created server-side only:

```python
client = stripe.StripeClient(os.environ["STRIPE_SECRET_KEY"], max_network_retries=2)
session = client.v1.checkout.sessions.create(params={
    "line_items": [{"price": SERVER_SIDE_PRICE_ID, "quantity": 1}],
    "mode": "subscription",
    "success_url": DOMAIN + "/billing/done?session_id={CHECKOUT_SESSION_ID}",
    "cancel_url": DOMAIN + "/pricing",
    "client_reference_id": str(user.id),        # from the SESSION, never a form field
    "customer": user.stripe_customer_id or None,
    "customer_email": user.email if not user.stripe_customer_id else None,
    "subscription_data": {"metadata": {"app_user_id": str(user.id)}},
    "allow_promotion_codes": True,
})
```

Note `subscription_data.metadata` — Session metadata does **not** propagate to the Subscription, so
without it your `customer.subscription.*` events won't carry your user id.

**Webhooks.** Stripe's hard rule: *"Automatic fulfillment with webhooks is required if you sell
subscriptions"* ([fulfillment](https://docs.stripe.com/checkout/fulfillment.md)). Its closest thing
to a canonical statement, from [billing/subscriptions/webhooks](https://docs.stripe.com/billing/subscriptions/webhooks.md):
*"use the `customer.subscription` events to track subscription events."*

| Event | Action |
|---|---|
| `checkout.session.completed` | Bind `client_reference_id` → `session.customer`. **Your only reliable moment to learn which of your users a new `cus_…` belongs to.** |
| `customer.subscription.created` | Upsert |
| `customer.subscription.updated` | **The workhorse** — renewal, plan change, trial→active, cancel-at-period-end |
| `customer.subscription.deleted` | Revoke |
| `invoice.payment_failed` | Notify. **Do not revoke here** — Smart Retries may recover; the status transition arrives via `subscription.updated` |
| `invoice.paid` | Optional; receipts |

Grant access when `status in {"active","trialing"}`. Revoke on `unpaid` and `canceled`. Decide the
`past_due` grace policy explicitly. Two gotchas: `active` does **not** mean all invoices are paid,
and Checkout-created subscriptions start `incomplete` with 23 hours to pay.
Cancel-at-period-end: check **both** `cancel_at is not None` (flexible billing mode) **and**
`cancel_at_period_end` (classic mode).

**Signature verification.** `stripe.Webhook.construct_event(payload, sig_header, secret)` with the
**raw body bytes** — in FastAPI that means `await request.body()`, never `await request.json()` and
never a Pydantic-typed body param on that route, because the signature is HMAC over
`f"{timestamp}.{raw_body}"` and any re-serialisation breaks it. Default tolerance 5 minutes; never
set it to 0; keep the server NTP-synced. Exempt the route from CSRF. Return 2xx fast — Checkout waits
up to 10 s for your webhook before redirecting the customer.

**Idempotency and ordering — one correction to the brief.** Stripe explicitly says **do not use
`created` to order or dedupe**: *"distinct events can share a timestamp. Don't use `created` to
determine event order or whether you've already processed an event. Track event IDs instead."*
So:
- `processed_stripe_events(event_id TEXT PRIMARY KEY, received_at)`, insert-or-ignore, skip on conflict.
- On any `customer.subscription.*`, **re-fetch** `client.v1.subscriptions.retrieve(sub_id)` and write
  that fresh state. A stale event then cannot clobber newer state.
- Make fulfilment safe under **concurrent** calls (DB unique constraint, not an in-process lock) —
  the landing page and the webhook genuinely race, and locally they race *harder* (see below).
- Live mode retries for **3 days**; **sandboxes retry only 3 times over a few hours** — don't
  misread test behaviour.
- Outbound: pass `Idempotency-Key` on POSTs; keys are pruned after ~24 h.

**Never trust from the client.** Stripe's own words:
*"Recalculate amounts on your server. Don't trust client-provided prices or totals."*
For one tier, the browser sends **nothing but intent** — `price_id` is a server constant,
`quantity` is hardcoded `1`, `client_reference_id` comes from the server session. On
`checkout.session.completed`, verify the price matches your expected `price_id` and reject if not.

**Billing Portal** for cancel/update: `client.v1.billing_portal.sessions.create({customer, return_url})`,
create on click (sessions expire after 5 min unused), authenticate the user yourself first, cannot be
iframed. **Portal configurations are per-mode** — you must configure it separately in live mode.

### Verifying everything with no real charge

- **Test keys** `sk_test_`/`pk_test_`; *"Live payments cannot be processed with test API keys."*
- **Cards** ([docs.stripe.com/testing#cards](https://docs.stripe.com/testing.md#cards)):
  `4242 4242 4242 4242` success · `4000 0000 0000 0002` decline ·
  `4000 0000 0000 9995` insufficient funds · `4000 0025 0000 3155` 3DS ·
  **`4000 0000 0000 0341`** attaches fine but **fails on charge** — the card for testing a failed
  renewal. (Use a short trial so the first attempt is deferred, otherwise the sub is created
  `incomplete` rather than going active-then-failing.)
- **Stripe CLI:** `stripe listen --forward-to localhost:8000/webhooks/stripe`. The signing secret it
  prints is **stable across restarts**, so it can live in `.env.local`. No Dashboard endpoint needed,
  no tunnel, no HTTPS. `stripe trigger checkout.session.completed` etc. — but heed the caveat:
  *"the event your webhook receives contains fake data that doesn't correlate to subscription
  information."* Use `trigger` to prove your route/signature/dedupe wiring; use a real test checkout
  to prove business logic.
- **Test clocks** (now branded "Simulations", sandbox-only): create clock → create customer *on* the
  clock with a default PM → create subscription → `POST /v1/test_helpers/test_clocks/{id}/advance`.
  Monthly renewal = advance a month, then **one more hour** (draft invoices take ~1 h to finalize).
  Limits: 3 customers/clock, advance ≤2 billing intervals at a time, auto-delete after 30 days, and
  **list APIs hide clock-generated objects unless you query for them** — if your admin page looks
  empty during a simulation, that's why.
  Whether clocks work with **Checkout** is **[UNVERIFIED]** — the mechanism requires creating the
  clock-bound customer first and passing `customer=cus_…` into the Session, since Checkout otherwise
  creates a fresh (clock-free) Customer. Confirm empirically. Portal + clocks: **[UNVERIFIED]** but
  should work.
- **Local race:** *"If you're using the Stripe CLI for local testing, Checkout redirects to the
  `success_url` immediately"* — skipping the 10 s webhook wait. Locally your landing page beats the
  webhook, which makes the concurrency bug reproducible. Good.

### Before a Stripe account exists at all

Two answers, use both.

**(a) `stripe sandbox create`** ([docs](https://docs.stripe.com/cli/sandbox)) — genuinely new and
directly on point: *"If your CLI isn't logged into Stripe, this command provisions a new sandbox with
working test API keys, **without requiring an account**."* Returns a secret key, publishable key and
a `claim_url`; **expires in 7 days**; `stripe sandbox claim` converts it to a real account.
**Gotcha: the key prefix is `rkcs_test_…`, not `sk_test_…`** — if your config validates
`startswith("sk_")` it will reject it. Accept `sk_test_`, `rk_test_` and `rkcs_test_`.

**(b) A `LocalBilling` provider** — the thing that actually makes the site testable forever, in CI,
with no network. Keep the boundary narrow:

```python
Plan = Literal["free", "pro"]

@dataclass(frozen=True)
class PlanState:
    plan: Plan
    status: str                       # active | trialing | past_due | canceled | none
    current_period_end: int | None = None
    cancel_at_period_end: bool = False
    managed_externally: bool = True    # False => admin-granted; hide "Manage billing"

class BillingProvider(Protocol):
    def create_checkout_url(self, user_id: str, email: str,
                            success_url: str, cancel_url: str) -> str: ...
    def portal_url(self, user_id: str, return_url: str) -> str: ...
    def handle_webhook(self, raw_body: bytes, sig_header: str) -> None: ...
    def current_plan(self, user_id: str) -> PlanState: ...
```

Note there is **no `price_id` parameter** — the provider owns it. That is the "never trust the
client" rule enforced by the type signature.

`LocalBilling.create_checkout_url` returns `/dev/billing/checkout?...`, a local page with **Simulate
successful payment** / **Simulate decline** buttons that write the same `subscriptions` row Stripe
would and redirect to `success_url` — so your success page, session handling and post-upgrade UI all
get exercised. `LocalBilling.portal_url` gives **Cancel at period end / Cancel now / Simulate
renewal / Simulate failed renewal**. Both providers back onto **one table with one schema**, so
`current_plan()` is provider-agnostic and every gate in the app is identical in both modes.

Guardrails: **refuse to start** if `BILLING_BACKEND=none` and `ENV=production`; mount `/dev/billing/*`
only when the local provider is active; show a persistent "Billing simulated" banner; keep an
`idea admin grant <user> pro --days 30` path in *both* modes with a `source` column
(`"stripe" | "admin"`) so `customer.subscription.deleted` never revokes a comp.

### Practical notes

- **Fees:** [stripe.com/gb/pricing](https://stripe.com/pricing) — UK cards **1.5% + 20p**, EEA 2.5%,
  international 3.15%, +2% conversion. **Stripe Billing adds 0.7% of billing volume.** On a £10/mo
  sub that's roughly **£0.47, ~4.7%**. US **2.9% + $0.30** is **[UNVERIFIED]** (geolocated page).
- **EU VAT:** a digital subscription is taxable **in the customer's country**. EU-based sellers get a
  €10,000/yr cross-border threshold, then Union OSS (one registration, one return). **Non-EU sellers
  have no threshold** — registration may be needed from the first EU sale, via Non-Union OSS. Stripe
  Tax (`automatic_tax: {enabled: true}`) calculates and monitors thresholds but **does not register
  or file for you**. Get an accountant's five minutes.
- **Going live** needs KYC (business info, website, support contact, bank account) but **not
  necessarily a registered company** — sole traders can activate in most countries **[UNVERIFIED]**.
  One irreversible choice: **the business origin country cannot be changed after activation.**

---

## 5. Framework + process model

### What exists today

| Fact | Where |
|---|---|
| `ThreadingHTTPServer`, **refuses to bind anything but loopback** | `present/server.py:1447` |
| Handler is one `BaseHTTPRequestHandler` subclass with hand-rolled routing, `parse_qs` form parsing, manual Range support | `present/server.py:1234-1410` |
| Jobs live **in a dict in the web process**, one worker thread, `max_recent=50`, pruned | `webapp/jobs.py:283`, `:361` |
| A restart loses every job; there is no persistence | `webapp/jobs.py` docstring: "state lives in memory" |
| Progress = browser polls `/jobs/<id>/status` every 1.5 s | `present/server.py` `_JOB_JS` |
| The pipeline is already asyncio; the runner does `asyncio.run(cli._analyse(...))` | `webapp/runner.py` |
| **A production-grade SQLite queue already exists** — leases, heartbeats, WAL, `recover_startup()` requeues dead-leased jobs | `src/id_detector/jobs.py:177-300` |
| 54 test files, offline-by-default (`-m "not slow and not live"`) | `tests/`, `pyproject.toml` |

### Recommendation: **(b) migrate the web layer to FastAPI/Starlette + uvicorn** — with one addition

Keep `src/id_detector/` **completely untouched**. Build a new `src/idea_web/` package. Reuse
`webapp/runner.py` as-is (it is a clean `JobContext → pipeline` adapter). Replace
`webapp/jobs.py`'s in-memory manager with a SQLite-backed queue **in a separate worker process**.

**Why not (a) keep http.server:**
- No sessions, no cookies helper, no CSRF, no multipart (which the upload fallback requires), no
  streaming responses, no dependency injection for auth — you would write all of it by hand, badly.
- Thread-per-request with no limits, no timeouts and no body-size guard is not an internet-facing
  posture. The current design is *deliberately* loopback-only and correctly refuses otherwise.
- The pipeline is already async; ASGI is a natural fit and lets Stripe's `_async` methods work
  properly.
- Effort saved by keeping it is roughly a week; effort spent hand-rolling the missing pieces is more.

**Why not (c) something else:** Django gives you auth/sessions/CSRF/admin free, which is genuinely
tempting — but it drags in an ORM and a project layout that fights a package whose whole identity is
"artefact contracts and content addressing". Flask is fine but WSGI-only. FastAPI is the smallest
step from where you are that gets you everything you need. **This is a close call; if you'd rather
not write auth at all, Django + `django-allauth` is a defensible alternative and would cut Phase 2
roughly in half.**

**The non-negotiable part: jobs move out of the web process.** A 10–40 minute job that must survive
a deploy, an OOM kill and a page reload cannot live in a dict in the uvicorn process.

```
Web process (uvicorn)          Worker process(es)
  - render pages                 - lease next queued job (SQLite, WAL)
  - auth / sessions              - heartbeat every 10 s
  - quota check                  - run id_detector pipeline (asyncio.run)
  - INSERT job row  ------------>- write progress rows
  - read progress rows <---------- - release / fail / requeue on restart
  - Stripe webhooks
```

One worker to start (the Shazam limiter and AudD concurrency both argue against parallelism until
measured). `recover_startup()`'s logic — requeue anything leased with a stale heartbeat — is already
written in `jobs.py` and is exactly what makes a restart safe.

**Progress: keep polling.** The 1.5 s `fetch('/jobs/<id>/status')` loop already survives page
reloads, works through any proxy, needs no sticky sessions, and is trivially cacheable. SSE is nicer
but adds a long-lived connection per viewer for a job that ticks every few seconds. Slow the poll to
2.5 s and keep it. Revisit SSE only if you add live multi-user views.

### Module layout

```
src/id_detector/                  # UNCHANGED — the pipeline, contracts, artefacts
src/idea_web/
  __init__.py
  settings.py          # pydantic-settings; env only; refuses to boot on bad combos
  app.py               # FastAPI factory: middleware, routers, exception handlers
  db.py                # connection factory, WAL pragmas, migration runner
  migrations/
    001_init.sql  002_billing.sql  ...
  auth/
    passwords.py       # argon2-cffi hash/verify + check_needs_rehash
    sessions.py        # server-side session table + cookie read/write
    csrf.py            # synchroniser token, exempt list
    routes.py          # /signup /login /logout /verify /forgot /reset
    deps.py            # current_user / require_user / require_plan dependencies
  billing/
    base.py            # BillingProvider protocol, PlanState  (see §4)
    local.py           # LocalBilling  (no network)
    stripe_impl.py     # StripeBilling
    routes.py          # /billing/checkout /billing/portal /webhooks/stripe /dev/billing/*
  quota.py             # weekly counters, dedupe-aware accounting
  jobs/
    store.py           # SQLite queue: submit / lease / heartbeat / progress / release
    routes.py          # POST /analyse, GET /jobs/{id}, GET /jobs/{id}/status, POST cancel
    worker.py          # `python -m idea_web.jobs.worker` — the separate process
  ingest_policy.py     # per-platform egress: proxy only for youtube; upload fallback
  retention.py         # GC: drop windows/, pcm, original after success + N days
  ratelimit.py         # per-IP token buckets (signup, login, analyse)
  routes/
    pages.py  library.py  results.py  account.py  admin.py
  templates/           # Jinja2 — port present/theme.py's CSS wholesale
  static/
```

`present/page.py`, `present/theme.py` and `present/exports.py` keep doing exactly what they do —
rendering artefacts to a `present/index.html`. The web app serves those files (through an auth +
ownership check) rather than re-implementing them.

### Sessions: **server-side, not signed cookies**

A signed cookie carrying `{user_id, plan}` goes stale the instant a Stripe webhook changes the plan,
and cannot be revoked. Use a `sessions` table:

- Cookie name `__Host-idea_session`; attributes `HttpOnly; Secure; SameSite=Lax; Path=/`.
- Value = `secrets.token_urlsafe(32)`. **Store only `sha256(token)` in the DB** so a DB leak is not
  a session leak.
- Rotate the session id on login and on plan change; delete on logout; "log out everywhere" is one
  `DELETE FROM sessions WHERE user_id = ?`.
- Sliding expiry: 30 days idle, 90 days absolute.

### Passwords: **argon2-cffi**

`argon2-cffi`'s `PasswordHasher()` defaults (t=3, m=64 MiB, p=4) already exceed
[OWASP's Argon2id minimum](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
of m=19 MiB, t=2, p=1. Use `ph.verify()` in a try/except and `ph.check_needs_rehash()` to upgrade on
login. Prefer it over bcrypt: memory-hardness against GPU/ASIC, no 72-byte truncation. The one thing
to watch is that 64 MiB × concurrent logins is real RAM — at your scale, irrelevant; add a per-IP
login rate limit anyway.

### CSRF

`SameSite=Lax` blocks cross-site POSTs from a plain form and covers most of it. Add a synchroniser
token on every state-changing POST (`/analyse`, `/billing/checkout`, `/account/*`, `/logout`): a
random value in the session, echoed in a hidden field, compared with `secrets.compare_digest`.
**Exempt `/webhooks/stripe`** — it is authenticated by signature, not by cookie.

### SQLite schema

```sql
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;

CREATE TABLE users (
  id                 TEXT PRIMARY KEY,              -- uuid4 hex
  email              TEXT NOT NULL UNIQUE,          -- store casefolded
  password_hash      TEXT NOT NULL,                 -- argon2id
  email_verified_at  TEXT,                          -- NULL until verified
  created_at         TEXT NOT NULL,
  created_ip         TEXT,
  disabled_at        TEXT,
  is_admin           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX users_created_ip ON users(created_ip, created_at);

CREATE TABLE sessions (
  token_sha256  TEXT PRIMARY KEY,
  user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at    TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  expires_at    TEXT NOT NULL,
  user_agent    TEXT,
  ip            TEXT
);
CREATE INDEX sessions_user ON sessions(user_id);

CREATE TABLE email_tokens (                          -- verification + password reset
  token_sha256 TEXT PRIMARY KEY,
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  purpose      TEXT NOT NULL CHECK (purpose IN ('verify','reset')),
  expires_at   TEXT NOT NULL,
  used_at      TEXT
);

-- One row per user. Written by the Stripe webhook OR by an admin grant.
CREATE TABLE subscriptions (
  user_id               TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  plan                  TEXT NOT NULL DEFAULT 'free',   -- free | pro
  status                TEXT NOT NULL DEFAULT 'none',   -- none|trialing|active|past_due|unpaid|canceled
  source                TEXT NOT NULL DEFAULT 'none',   -- none | stripe | admin
  stripe_customer_id    TEXT UNIQUE,
  stripe_subscription_id TEXT UNIQUE,
  price_id              TEXT,
  current_period_end    TEXT,
  cancel_at             TEXT,
  cancel_at_period_end  INTEGER NOT NULL DEFAULT 0,
  updated_at            TEXT NOT NULL
);
CREATE INDEX subs_customer ON subscriptions(stripe_customer_id);

CREATE TABLE processed_stripe_events (               -- webhook idempotency
  event_id    TEXT PRIMARY KEY,
  type        TEXT NOT NULL,
  received_at TEXT NOT NULL
);

-- Quota: one row per (user, ISO week). ISO week as 'YYYY-Www', e.g. '2026-W37'.
CREATE TABLE quota_usage (
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  iso_week     TEXT NOT NULL,
  analyses_new INTEGER NOT NULL DEFAULT 0,   -- counted against quota
  analyses_cached INTEGER NOT NULL DEFAULT 0,-- NOT counted; rate-limited separately
  fetch_bytes  INTEGER NOT NULL DEFAULT 0,   -- egress budget (proxy cost control)
  paid_cents   INTEGER NOT NULL DEFAULT 0,   -- paid-engine spend this week
  PRIMARY KEY (user_id, iso_week)
);

-- The durable job queue (pattern lifted from src/id_detector/jobs.py).
CREATE TABLE jobs (
  id               TEXT PRIMARY KEY,
  user_id          TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  target           TEXT NOT NULL,               -- URL or uploaded-file id
  display          TEXT NOT NULL,               -- redacted, safe to render
  platform         TEXT,                        -- youtube|soundcloud|mixcloud|upload
  profile          TEXT NOT NULL,               -- free | max_accuracy
  known_tracklist  TEXT,
  state            TEXT NOT NULL,               -- queued|leased|running|succeeded|failed|cancelled
  phase            TEXT NOT NULL DEFAULT 'queued',
  windows_done     INTEGER NOT NULL DEFAULT 0,
  windows_total    INTEGER NOT NULL DEFAULT 0,
  message          TEXT NOT NULL DEFAULT '',
  error            TEXT,
  source_key       TEXT,                        -- filled after ingest
  media_key        TEXT,                        -- sha256 of the audio: the dedupe key
  result_path      TEXT,                        -- work/<source_key>/<media_key>/present/index.html
  was_cache_hit    INTEGER NOT NULL DEFAULT 0,
  paid_cents       INTEGER NOT NULL DEFAULT 0,
  lease_owner      TEXT,
  lease_expires_at TEXT,
  heartbeat_at     TEXT,
  created_at       TEXT NOT NULL,
  started_at       TEXT,
  finished_at      TEXT
);
CREATE INDEX jobs_queue  ON jobs(state, created_at);
CREATE INDEX jobs_user   ON jobs(user_id, created_at DESC);
CREATE INDEX jobs_media  ON jobs(media_key);

CREATE TABLE job_log (                              -- the ring buffer, persisted
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  seq    INTEGER NOT NULL,
  at     TEXT NOT NULL,
  line   TEXT NOT NULL,
  PRIMARY KEY (job_id, seq)
);

-- Library: one row per analysed mix, independent of who analysed it.
CREATE TABLE mixes (
  media_key    TEXT PRIMARY KEY,
  source_key   TEXT NOT NULL,
  canonical_url TEXT,
  platform     TEXT,
  title        TEXT,
  duration_ms  INTEGER,
  track_count  INTEGER,
  analysed_at  TEXT NOT NULL,
  last_viewed_at TEXT,
  artefacts_pruned_at TEXT     -- when windows/pcm/original were GC'd
);

CREATE TABLE rate_limits (                          -- per-IP buckets, also survives restart
  bucket     TEXT NOT NULL,     -- 'signup' | 'login' | 'analyse'
  key        TEXT NOT NULL,     -- ip or user_id
  window_start TEXT NOT NULL,
  count      INTEGER NOT NULL,
  PRIMARY KEY (bucket, key, window_start)
);
```

Two DBs, deliberately: `app.db` (above, backed up continuously) and the existing per-mix
`work/**/jobs.sqlite` (the pipeline's own provider-invocation ledger — leave it alone).

**Testability:** every one of these tables is exercisable offline. Keep the existing
`-m "not slow and not live"` default; add `tests/web/` with a `LocalBilling` fixture and an
in-memory-ish `app.db` in a tmpdir. No network, no keys, no flakiness — and those are the tests that
actually matter, because they cover "does a free user get blocked."

---

## 6. Deployment sizing

### Measured footprint (from `work/`, 11 mixes, 4.9 GB total, mean 445 MB/mix)

| Artefact | 38-min mix, gen0 only | 46-min mix, 3 gens | **Per hour of audio** |
|---|---|---|---|
| `ingest/original.*` | 36 MB | 55 MB | **56–75 MB** (source bitrate) |
| `decode/audio.pcm` | 71 MB | 85 MB | **115 MB** — fixed: 16 kHz mono s16le = 32 kB/s |
| `windows/gen0` (12 s WAV every 9 s) | 94 MB | 113 MB | **154 MB** — fixed: 384,044 B per 9 s |
| `windows/gen1+gen2` (rescans) | 0 | 307 MB | rescans only — **off by default** |
| `recognise/invocations` | 3 MB | 17 MB | 5–22 MB |
| `fuse` + `hints` + `present` + `jobs.sqlite` | 2 MB | 3 MB | ~3 MB |
| **Total** | **203 MB** | **577 MB** | **~330–370 MB/h with rescans off** |

Largest observed mix: 81 min, **750 MB** (with rescans).

**The durable result is only `present/` + `fuse/` + `hints/` + `recognise/` + `ingest/source.json` —
about 10–25 MB.** Everything else is reproducible from the source URL. Confirmed by
`ingest._load_cached`, which re-derives from `source.json` and verifies the media-key hash.

### Retention policy (mandatory, not optional)

```
On job success:
  keep   present/ fuse/ hints/ recognise/ ingest/source.json ingest/source.done.json
  drop   windows/**            (154 MB/h — regenerable from PCM)
  keep   decode/audio.pcm      for 48 h   (115 MB/h — lets a rescan/refresh skip re-download)
  keep   ingest/original.*     for 7 days (56–75 MB/h — powers the result page's <audio> player)
  then   drop both
```

The `<audio>` player on the result page (`present/server.py:_resolve_served_audio`) reads
`ingest/original.*`, so dropping it degrades that feature — after 7 days, fall back to the platform
embed (`plan_embed_from_url` already exists) or re-fetch on demand. Note the SoundCloud API terms
argument in §1 gets *easier* the shorter you keep the audio: deleting it promptly and saying so, as
setlist.id does, is both the honest and the defensible position.

**Result:** ~20 MB durable per mix. 1,000 mixes/month = **20 GB/month durable growth**, plus
~370 MB × jobs-in-flight transient. Without retention: 370 GB/month — a 160 GB disk dies in twelve
days.

### RAM per concurrent job

The driver is **not** ffmpeg (which streams to disk) and **not** windowing (`windows.py:230` does
`seek` + `read` of 384 kB at a time). It is `novelty.py:_read_pcm`, which materialises the entire
mix as float64:

| | per hour of audio | 60-min mix | 3-hour mix |
|---|---|---|---|
| float64 array (16000 × 8 B) | 460 MB | 0.46 GB | 1.38 GB |
| transient int16 concat + block list | 230 MB | 0.23 GB | 0.69 GB |
| **peak** | **~690 MB** | **~0.7 GB** | **~2.1 GB** |

Plus a Python + numpy + pydantic process at ~250–400 MB RSS, and ffmpeg at ~50–100 MB. So:

- **Budget ~1.5 GB per concurrent job** for mixes up to 90 minutes.
- **Cap mix length** (competitors do: TrackSniff 90 min free; setlist.id counts >4 h as 2 sets).
  A 4-hour mix would peak near 3 GB and is the OOM you will actually hit.
- **A cheap win:** streaming the flux computation instead of concatenating (the batching at
  `novelty.py:94` already processes 2,048 frames at a time — only `_read_pcm` needs changing) would
  cut peak RAM by ~5×. Roughly a 2-hour change, worth doing before sizing a server.

CPU is not the constraint: ffmpeg decoding a 60-min mix to 16 kHz mono is seconds, windowing is I/O,
and recognition is network-bound behind an 18 req/min limiter.

### Container

One image: `python:3.12-slim` + `uv` + **ffmpeg** + **Deno** (yt-dlp EJS — the PyPI install does not
bundle it) + `yt-dlp[default,curl-cffi]` (Mixcloud needs impersonation). Optionally the
`bgutil-ytdlp-pot-provider` server as a sidecar.

**Drop the JDK/Panako.** Confirmed by the repo itself: `README.md` lists Panako as *excluded from
v1*; `runner.py:_run_build_index` is best-effort and swallows every failure; and your own field test
found it only recovers the DJ's own indexed tracks. Removing it saves ~200–400 MB of image and one
whole runtime to patch.

Also drop from the production image: `benchmark/`, `calibrate/`, `truth/`, `local_fixture.py` — they
are development tooling, not runtime. (They're ~5,000 lines of the 33,700.)

### Provider and size

[Hetzner pricing, Sep 2026](https://costgoat.com/pricing/hetzner) (note the **June 2026 repricing**
raised CPX/CCX by 113–175% while the Intel-shared CX line stayed cheap —
[Hetzner price adjustment](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/)):

| Plan | vCPU / RAM / disk | Traffic | €/mo | Fit |
|---|---|---|---|---|
| **CX33** | 4 / 8 GB / 80 GB | 20 TB | **€8.49** | Start here — 1 worker, 100 mixes/mo |
| **CX43** | 8 / 16 GB / 160 GB | 20 TB | **€15.99** | **Recommended** — 2 workers, retention headroom |
| CX53 | 16 / 32 GB / 320 GB | 20 TB | €29.49 | Only if you need 4+ concurrent jobs |
| CAX21 (ARM) | 4 / 8 GB / 80 GB | 20 TB | €10.49 | Viable; check `shazamio-core` has an aarch64 wheel first |

20 TB of included traffic is far more than you will use (1,000 mixes × 65 MB ≈ 65 GB).

**Avoid Fly and Railway for this workload:**
- Fly Machine root filesystems are **ephemeral and capped at 2000 IOPS / 8 MiB/s regardless of
  machine type** ([Fly volumes docs](https://fly.io/docs/volumes/overview/)) — you are writing 400
  WAV files per mix. Fly's own
  [long-running-tasks blueprint](https://fly.io/docs/blueprints/long-running-tasks/) says checkpoint
  anything over 5 minutes. Volumes are per-machine, not shared.
- Railway caps HTTP requests at 15 minutes and sleeps a service after 10 minutes with no outbound
  requests ([docs](https://docs.railway.com/guides/cron-workers-queues)). Background workers are
  fine, but the ephemeral-disk story is wrong for a 5–50 GB artefact cache.
- DigitalOcean works and is simpler than AWS but is roughly 2× Hetzner for the same specs.

**A plain VPS with a real disk is the right answer**, and it also keeps the option of a home exit
node for YouTube egress.

### Reverse proxy, TLS, backups, secrets

- **Caddy** in front, one `Caddyfile`, automatic Let's Encrypt. Set
  `request_body { max_size 250MB }` for the upload path and a generous
  `transport http { read_timeout 300s }`. Uvicorn binds `127.0.0.1` only.
- **Backups:** [Litestream](https://litestream.io/) v0.5 continuously replicates `app.db` to S3-
  compatible storage (Cloudflare R2 or Backblaze B2 — pennies at this size) with sub-second lag and
  point-in-time recovery. Do **not** back up `work/` — it is a regenerable cache. Do back up any
  uploaded originals if you keep them, or don't keep them.
- **Secrets:** environment only, exactly as the repo already does
  (`README.md`: "Secrets never go in the config file... the logger redacts them"). On the server,
  a root-owned `/etc/idea/env` with `0600` loaded by systemd `EnvironmentFile=`. Keep
  `io.redact_text` in the logging path — it is already applied to every job log line
  (`webapp/jobs.py:252`) and that is a genuinely good habit to carry forward.
- **Deploy:** `docker compose up -d` with two services from one image (`web` and `worker`) plus
  Caddy. Rolling restart is safe because leases expire and `recover_startup()` requeues.

### Rough monthly cost

**100 mixes/month** (say 60 YouTube, 20 paid-tier):

| Item | $/mo |
|---|---|
| Hetzner CX33 | ~$10 |
| Domain + R2 backups | ~$2 |
| Residential proxy (60 × 65 MB = 3.9 GB @ ~$3/GB) | ~$12–20 |
| AudD (20 mixes × $1.50) | $30 |
| Stripe fees (say 20 subs × £10) | ~$10 |
| **Total** | **~$65–75** |

**1,000 mixes/month** (600 YouTube, 200 paid-tier):

| Item | $/mo |
|---|---|
| Hetzner CX43 | ~$19 |
| Backups + domain | ~$3 |
| Residential proxy (600 × 65 MB = 39 GB @ ~$2.75/GB) | ~$110 |
| AudD (200 × $1.50, or $0.60 at volume) | $120–300 |
| Stripe fees (200 subs × £10 @ ~4.7%) | ~$120 |
| **Total** | **~$370–550** |

**The number that is missing from that table is the free tier.** If 800 of those 1,000 mixes are
free-tier Shazam jobs, they cost $0 in cash and **impossible** in throughput: 800/month is 27/day
against a ~65/day per-IP ceiling, at 100% duty cycle, with no bursts. That is the wall — reached
long before the money runs out.

---

## 7. Free-tier abuse controls

| Control | Recommendation | Why |
|---|---|---|
| **Email verification** | **Yes.** No quota granted until verified. | Cheapest gate there is, and you need the token flow for password reset anyway. |
| **Disposable-email blocking** | Yes — [disposable-email-domains](https://github.com/disposable-email-domains/disposable-email-domains) list, refreshed weekly, checked at signup. | ~90% effective for near-zero effort. Fail *soft* (ask for another address), never silently. |
| **Per-IP signup limit** | 3 accounts/day, 10/week per IP; store `created_ip` on `users` (schema above) so you can retro-ban a cluster. | The dominant free-tier attack is one person, many accounts. |
| **Per-IP analysis limit** | 5 submissions/hour regardless of account, incl. cached ones. | Stops a single script from hammering the queue. |
| **Per-account weekly quota** | **1 new analysis/week free** (if you keep a free analysis at all — see R1). ISO week key `YYYY-Www` in `quota_usage`. | Matches the market: TrackSniff 50 recognitions/mo but only first 3 tracks; Scanamix one free mix; setlist.id 10-day trial capped at 2 sets. |
| **CAPTCHA** | [Cloudflare Turnstile](https://prosopo.io/tools/cloudflare-turnstile-pricing/) on signup and on free-tier submit. Free tier is **unlimited requests, unlimited sitekeys**, no card. | The only free-forever option at this scale; [hCaptcha](https://www.hcaptcha.com/pricing) free tier is 10k–100k/mo, then $99/mo. |
| **Email deliverability** | Postmark / Resend / SES. Never self-host SMTP on the VPS. | A blocked verification email is a dead signup. |
| **Job length cap** | 90 min free / 4 h paid; >4 h counts as 2. | Directly bounds the RAM peak (§6) and the AudD bill. Same policy setlist.id uses. |

### The dedupe argument — should a cached result count against quota?

**Recommendation: a cache hit does not count against the analysis quota, but is counted separately
and rate-limited.** The reasoning, grounded in how the code actually works:

1. **URL-level dedupe happens before any download.** `ingest._load_cached` (called first thing in
   `ingest()`) walks `work/*/*/ingest/source.json` and matches on `input_url` **or**
   `canonical_url`, verifying the completion sidecar and the media-key hash before returning. So a
   repeat submission of an already-analysed link costs **zero bandwidth, zero proxy spend, zero
   recognition requests, and a few milliseconds**. Charging a weekly quota for it would be charging
   for nothing.
2. **Byte-level dedupe happens *after* download.** `media_key = sha256(bytes)`, so the same audio
   posted under a different URL still costs you one fetch (≈65 MB and one proxy call) before the
   hash matches. **That one should count — against a bandwidth budget, not the analysis quota.**
   The `fetch_bytes` column in `quota_usage` exists for exactly this.
3. **The risk of free cache hits is that you become a free scraping API for your own library.**
   Mitigate with a separate, higher limit — e.g. 20 cached results/day per account, 50/day per IP —
   and rate-limit the export endpoints (CUE/M3U/JSON) more tightly than the HTML page.
4. **The market has already validated this.** set79's copy is literally *"You only pay to analyze a
   set that isn't in the library yet"*, and their 70,000-tracklist free library is their main
   acquisition channel. Your `_discover_sets` + library page is the same asset, already built.

This also reframes the free tier in a way that dodges R1 entirely: **free = unlimited browsing of
everything anyone has ever analysed**, which is genuinely valuable, costs you nothing, grows with
every paid job, and is the single best reason for someone to create an account. Paid = the right to
add something new to it.

### What competitors do

- **setlist.id** — 10-day trial, 2 sets, **card required**; refunds the credit if identification fails.
- **set79** — free public library of 70k tracklists; you pay only for new analyses; 7-day trial.
- **TrackSniff** — 50 recognitions/mo free, **first 3 tracks only**, 90-min and 100 MB caps.
- **trackid.net** — link submission is premium-only; £20/year.
- **Scanamix** — one free mix up to 3 hours, then from $3.19/mo.

Nobody offers an unlimited free tier. Every one of them either caps the *result* (TrackSniff's
3 tracks) or caps the *count* (everyone else). The consistent shape is: **the library is free, the
analysis is not.**

---

## Recommended architecture

```
                        Internet
                            │  :443
                    ┌───────▼────────┐
                    │  Caddy         │  auto-TLS, 250 MB body cap, gzip
                    └───────┬────────┘
                            │ 127.0.0.1:8000
    ┌───────────────────────▼─────────────────────────┐
    │  uvicorn  ·  idea_web.app  (FastAPI)            │
    │                                                  │
    │  auth/       argon2-cffi, server-side sessions   │
    │  quota.py    weekly counters, dedupe-aware       │
    │  billing/    BillingProvider → Local | Stripe    │
    │  jobs/routes POST /analyse → INSERT jobs row     │
    │              GET /jobs/{id}/status → poll (2.5s) │
    │  routes/     library, results, account, admin    │
    │  static file serve of work/**/present/*  (authz) │
    └──────────┬──────────────────────┬────────────────┘
               │                      │
        ┌──────▼──────┐        ┌──────▼─────────────────────────┐
        │  app.db     │◄──────►│  idea_web.jobs.worker (proc)   │
        │  SQLite WAL │  lease │   lease → heartbeat → run      │
        │             │  +     │   ├─ ingest_policy: youtube?   │
        │  Litestream │  progr.│   │    → HTTP_PROXY residential │
        │   → R2      │        │   │  soundcloud/mixcloud → direct│
        └─────────────┘        │   │  upload → local file        │
                               │   ├─ id_detector.cli._analyse   │
                               │   │   (UNCHANGED pipeline)      │
                               │   │    ingest → decode → windows│
                               │   │    → recognise → hints      │
                               │   │    → fuse → enrich → present│
                               │   └─ retention.py GC            │
                               └──────┬─────────────────────────┘
                                      │
                              ┌───────▼────────┐
                              │  work/         │  content-addressed cache
                              │   <src>/<media>│  ~20 MB durable per mix
                              └────────────────┘
                                      ▲
                     ┌────────────────┴──────────────────┐
                     │ external: AudD (paid), Shazam     │
                     │ (paid-job gap-fill only),         │
                     │ SoundCloud comments (hints),      │
                     │ Stripe (webhooks in)              │
                     └───────────────────────────────────┘
```

**Boundaries that matter:**
- `src/id_detector/` is never modified by the web work. The web layer talks to it only through
  `cli._analyse` / `cli._acquire` (as `webapp/runner.py` already does) and by reading artefacts.
- The web process never runs a pipeline. It only writes a `jobs` row and reads progress.
- The worker never serves HTTP. It only leases, heartbeats and writes.
- Billing touches the app in exactly four methods (`BillingProvider`), so the whole site is testable
  with `BILLING_BACKEND=none`.

---

## Phased build list

Hours are for one competent developer who knows this codebase. They exclude the owner's decisions,
legal review, and design polish.

| # | Phase | What | Hours |
|---|---|---|---|
| **0** | **Decide the free tier** | Owner decision (see R1). Everything downstream depends on it. Also: email api@audd.io for the production ToS + a concurrency number (R3). | 0 dev |
| **1** | **Web layer → FastAPI** | New `src/idea_web/`; port `present/server.py`'s routes and `theme.py`'s CSS into Jinja2 templates; keep the artefact-serving path; Caddy in front. No behaviour change. | **20–30** |
| **2** | **Auth** | users/sessions/email_tokens tables; argon2-cffi; signup/login/logout/verify/reset; CSRF; per-IP rate limits; Turnstile; disposable-email list; transactional email provider. | **14–20** |
| **3** | **Durable job queue + worker process** | `jobs/store.py` copying the lease/heartbeat/`recover_startup` pattern from `id_detector/jobs.py`; `jobs/worker.py`; persist the log ring buffer; wire progress polling to DB rows. **This is what makes restarts safe.** | **16–24** |
| **4** | **Quotas + library dedupe** | `quota_usage`; ISO-week counters; cache-hit accounting (§7); the `mixes` table and a public library page; export rate limits. | **10–14** |
| **5a** | **Billing — LocalBilling first** | `BillingProvider` protocol; `LocalBilling`; `/dev/billing/*` simulate pages; admin grant/revoke CLI; plan gates throughout the UI; tests. **Ship and use this before any Stripe account exists.** | **8–12** |
| **5b** | **Billing — Stripe** | `StripeBilling`; Checkout + Portal; webhook route with raw-body verification, `event_id` dedupe, re-fetch-on-event; `stripe listen` loop; test-clock renewal/failure/cancel walkthrough; go-live checklist. | **10–16** |
| **6** | **Ingest hardening** | Per-platform egress routing (`ingest_policy.py`); `--proxy` plumbed into the yt-dlp call; Deno + curl-cffi in the image; optional bgutil POT sidecar; **upload fallback** (multipart, 250 MB cap, ffprobe validation); credit-refund-on-failure; format fallback for SABR; a 3-platform canary job. | **14–20** |
| **7** | **Deployment** | Dockerfile (drop JDK, drop benchmark/calibrate/truth); compose with web+worker+Caddy; systemd; Litestream → R2; secrets via EnvironmentFile; deploy script. | **10–16** |
| **8** | **Retention + observability** | `retention.py` GC job (§6); disk-usage alarm; structured logs with `redact_text`; a `/admin` page showing queue depth, quota usage, paid spend, disk. **Optional but recommended: stream `novelty._read_pcm` to cut peak RAM ~5×** (~2 h). | **10–14** |
| **9** | **Legal + policy pages** | ToS modelled on setlist.id's (user warrants the right to analyse; no audio retained or redistributed; results for personal use); privacy policy; UK/EU data handling; the honest "how it works / what it can't do" page. **Then a real solicitor's review** — the `docs/PLAN.md` commercial-release checklist is already the right agenda. | **6–10** + external |

**Total: ~118–176 hours of build.** Roughly 3–4½ focused weeks.

**Sequencing note:** phases 1–3 are the foundation and should be done in order. Phase 5a can happen
any time after 2 and unblocks all UI work on plans. Phase 6 is the one with genuine unknown-unknowns
(YouTube from a server) — **spike it early**, in an afternoon, on a cheap VPS with a throwaway
Hetzner box and one real YouTube link, before committing to the rest.

---

## Open questions for the owner

1. **The free tier** — library-only + one welcome analysis (recommended), or a small Shazam allowance
   with eyes open? Nothing else can be sized until this is answered.
2. **AudD production ToS** — attribution required in the UI? caching results permitted? concurrency
   ceiling? (email api@audd.io)
3. **Business origin country for Stripe** — irreversible after activation, and it sets both the fee
   schedule and the VAT position.
4. **YouTube support at all?** set79 is SoundCloud-only and appears to be doing fine. Dropping
   YouTube removes the entire residential-proxy line item, the PO-token machinery, and the highest-
   churn part of the stack. Worth pricing as an explicit option.
5. **Mix-length cap** for free and paid tiers (drives RAM sizing and the AudD bill).

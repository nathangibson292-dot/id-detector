## A. Factual claims

The central diagnosis is directionally strong, but several absolute claims are inaccurate.

| Claim | Finding |
|---|---|
| **E-C1 — paid-first crash** | **Confirmed, but not every `max_accuracy` run.** The paid branch leaves `matches` and `recognised` unbound, while output reads them at `src/id_detector/cli.py:794-815`; they are only assigned in the free branch at `src/id_detector/cli.py:507-521`. The crash occurs whenever paid-first produces at least one parseable AudD observation, including a successful no-match. If AudD is unavailable or every call errors, the code falls back to free at `cli.py:499-506` and does not hit this bug. CLI `max_accuracy` is also still Shazam-only unless AudD is explicitly enabled, whereas the web runner adds AudD at `src/id_detector/webapp/runner.py:137-167`. The plan should say: **“The paid-first branch deterministically crashes after presentation whenever it receives any parseable AudD observation.”** The quoted ~$2 applies only to an uncached 60-minute, 400-request run at $5/1,000. |
| **E-C2 — corroboration impossible** | **Core argument confirmed; “impossible” is too absolute.** The intended free pass is restricted to the complement of every non-suppressed AudD episode, not literally every AudD observation: `src/id_detector/scan_targeting.py:61-69,127-146`; the call is at `src/id_detector/cli.py:659-682`. Therefore there is no deliberate same-window cross-check. However, targets are padded by 3 seconds and `_windows_in_spans` selects a full 12-second window by start time, allowing boundary overlap: `scan_targeting.py:72-99`, `cli.py:355-369`. `_engine_corroborated` does not verify temporal overlap at all: `src/id_detector/fuse/episodes.py:148-156`. Replace “can never fire” with **“eliminates designed overlap, so corroboration is normally absent and any remaining flag can be incidental and unreliable.”** |
| **E-C3 — AudD always wins selection/no anchor** | **The downgrade mechanism is real; “wins every contested window” is false.** Standard AudD clip observations have `anchor=None` and no separate source: `src/id_detector/providers/audd.py:344-365`. Both engines then enter the default `primary` group at `src/id_detector/fuse/alignment.py:137-150`. AudD’s missing skew data evaluates to zero, but selection first chooses the majority candidate; disagreements tie-break by candidate ID, and equal skew costs tie-break by observation ID: `alignment.py:85-94,155-169`. Thus AudD can displace Shazam, particularly when both name the same candidate and Shazam has nonzero skew, but not invariably. An anchorless winner cannot create an alignment point: `alignment.py:115-124`. The proposed `simultaneous_source` fix prevents the collision, but the plan also needs to parse and validate AudD’s documented standard-result `timecode` into an anchor; the enterprise parser already does this at `audd.py:242-250`. [AudD API documentation](https://docs.audd.io/) |
| **E-C4 — error caching** | **Confirmed.** Any cached dictionary is trusted at `src/id_detector/paid_clip.py:175-181`; a live response is written at `paid_clip.py:182-190`; only afterwards does parsing reject `status != success` at `paid_clip.py:191-202` and `src/id_detector/providers/audd.py:313-314`. The bad entry remains indefinitely for that cache key unless refreshed, deleted, or invalidated by a config-version change. “Forever” should be phrased as **“indefinitely under the current cache key.”** |
| **E-H6 — config drop** | **Confirmed.** `profile_app_config` supplies only upload, transform, schedule and rescan fields: `src/id_detector/profiles.py:738-753`. The subsequent replacements omit `present_min_track_ms`, `collapse`, `same_track_bridge_ms`, Shazam RPM and concurrency in both `src/id_detector/cli.py:985-1007` and `src/id_detector/webapp/runner.py:48-72`, despite those being real `AppConfig` fields at `src/id_detector/providers/base.py:90-115`. `config show` displays the file configuration alone, not the profile-resolved runtime configuration: `cli.py:303-318`. The frozen profile’s own provider budget/RPM values are also not propagated. |
| **U-F3 — “three tracklists”** | **The stale-export bug is confirmed; the number three is rhetorical rather than invariant.** Refresh regenerates only `index.html`: `src/id_detector/present/refresh.py:50-77`. Exports are separate fixed files generated at `src/id_detector/present/exports.py:350-444`. Copy serializes the currently visible DOM and changes when short rows are toggled: `src/id_detector/present/page.py:1037-1047`. In the checked DJ Three artefact, JSON/CUE contain 55 entries, the HTML contains 50 track rows with 40 hidden, and Copy initially follows the same visible projection as the page. The plan should say **“multiple stale and UI-state-dependent projections; by default the page and Copy agree, while downloadable exports can disagree.”** |
| **U-F14 — write on GET** | **Confirmed, conditionally.** Serving an old result page calls `ensure_fresh_page` during `GET`: `src/id_detector/present/server.py:1235-1268`. It writes only when the page version is stale, and failures are swallowed: `src/id_detector/present/refresh.py:80-94`. Say **“a GET of a stale result can mutate `index.html` without updating the bundle.”** |

Additional factual corrections:

- The claim that malformed Shazam HTTP-200 bodies are parsed as “no match” is **wrong**. JSON parsing failure raises `ShazamHTTPError`: `src/id_detector/shazam.py:200-204`; recognition records a permanent failure/error observation: `src/id_detector/recognise.py:307-340,521-553`. Failures may be poorly surfaced, but they are not no-matches. Correct `docs/PLAN-v2.md:129-134,320-324,557-560,587`.
- “Hosted tiers fully licensed” is unsupported. The plan itself says AudD production resale, caching and attribution terms remain unresolved: `docs/PLAN-v2.md:324,586`; the hosting review calls this launch-blocking at `docs/reviews/v2-review-hosting.md:143-152`.
- “A cached link costs nothing” is not generally true. Current URL cache validation requires the retained original and hashes it: `src/id_detector/ingest.py:143-164`. Deleting originals after seven days, as proposed at `docs/PLAN-v2.md:394-401`, disables this path. A standard cached result also does not make a requested Deep upgrade free.
- “`work/` is reproducible from source URLs” is unsafe. URLs disappear, become private, mutate, or become blocked; only `app.db` is backed up under `docs/PLAN-v2.md:394-403`.
- “Local mode unchanged” conflicts with removal of M3U, web index-building, whole-file scanning and paid-first `max_accuracy`, plus quarantine of rescans: `docs/PLAN-v2.md:195-208,251-256`.

## B. Deep-scan sequencing

The high-level local sequence—free primary sweep, hints, fuse, targeted paid checks, one final re-fuse—is correct. The proposed selection and promotion rules are not safe enough.

### Recall and budget problems

- Eighty clips is an absolute count even though billing is minute-based. It averages one clip per 45 seconds for a 60-minute mix, but one per three minutes for a four-hour mix.
- The fixed 40/40 split ignores the actual sizes of the two regions. A five-minute uncertain-listed region can be oversampled while fifty-five minutes of blanks receive only forty probes.
- “Tracks play 3–6 minutes” excludes rapid cuts, layered records, mashups, intros, radio edits and transitions—the exact material likely to be missing from a catalogue.
- Targeting windows whose **start** lies in a span misses short spans and can choose clips dominated by adjacent audio: `src/id_detector/paid_clip.py:101-117`. An 8-second target can contain no eligible start even though a 12-second window intersects it.
- A newly found AudD-only track in a blank gets only 12 seconds of proved on-air time. It remains below the 30-second presentation floor unless it becomes `likely`, receives a hint, or gets cross-engine corroboration: `src/id_detector/present/exports.py:116-141`. Recovering blank tracks therefore needs multiple non-overlapping confirmations, not just broad probing.

### Precision problems

- `hint_only` is currently deemed confident and skipped by paid targeting: `src/id_detector/scan_targeting.py:20-31`; yet it explicitly has no audio match: `src/id_detector/fuse/episodes.py:775-840`. These rows should be first-class verification targets.
- One cross-provider coincidence currently exempts a row from both suppression and the on-air floor: `episodes.py:712-772`, `exports.py:126-141`. A single shared-catalogue mistake should not gain both privileges.
- Changing corroboration to read the entire evidence bucket is dangerous and unnecessary. Rejected observations can be reassigned to the selected trial owner, and unanchored evidence can be attached to the nearest occurrence: `episodes.py:259-340`. That can associate two providers with the same repeated track occurrence even when their supports did not overlap.
- Once each provider has its own `simultaneous_source`, both selected observations already survive. Corroboration should remain based on **selected votes**, requiring the same normalized work, overlapping support, and different trust families.
- `COMMERCIAL_PROVIDERS` is a discount set, not a complete independence model: `episodes.py:91-108`. Define explicit families such as AudD/ACR-derived catalogue, Shazam, user-owned local index and crowd. Crowd should remain hint corroboration, not be called an engine.
- AudD’s missing anchor means the plan’s “tighter boundaries” promise is presently false: `audd.py:361`, `alignment.py:115-124`.

### Frozen profiles and cache

Keeping Deep as a runtime recipe can preserve committed profile bytes. But the existing byte test only proves that the profile JSON regenerates identically; it says nothing about free-run outputs: `tests/test_stage4d_profiles.py:38-51`. Add a Local Free golden pipeline result before changing selection/fusion behavior.

Cache entries also need typed states—success-match, success-no-match, retryable failure, ambiguous—and an adapter/schema version. `--refresh` must be selective; rebilling every clip to escape one corrupt entry is not acceptable.

### Better concrete sequence

1. Resolve an immutable analysis recipe containing engine roles, density, cap, parser versions and presentation settings; return an exact/capability-superset cached result only.
2. Run Local Free exactly as today. Hosted mode uses only a provider whose production contract explicitly permits this use.
3. Run hints and the first fuse.
4. Build targets in priority order: `hint_only`; listed-but-not-confident; suppressed candidates worth challenging; then genuine blank duration.
5. Allocate at least one representative, high-audio-quality window per episode; distribute the remainder by target duration, with unused allocation automatically flowing to the other category.
6. Reserve 20–30% of the cap for adaptive confirmation: when a probe finds a new blank identity, query two additional non-overlapping windows around it. One agreement may be displayed, but require two separated cross-provider agreements—or sufficient same-engine on-air support—before bypassing suppression/floor.
7. Select windows by intersection/usable-audio score, not start time alone.
8. Re-fuse once after all secondary probes and confirmations.
9. Record `complete`, `degraded`, `budget_exhausted`, `provider_unavailable` or `partial`; never silently change the purchased recipe.
10. Scale the cap with duration and money, e.g. clips-per-audio-minute bounded by a per-run USD reservation, rather than 80/120 for every length.

The contingency order should be an atomic, service-wide circuit breaker and request budget. If Shazam degrades, finish as explicitly AudD-only/degraded; do not silently swap semantics mid-run, and do not route Shazam through residential proxies.

## C. Business model and COGS

Minute-based metering, full free results, packs, bounded spend and failure refunds are good principles. The published allowances and arithmetic are not commercially safe.

Using the plan’s $2–$5 per 1,000 AudD requests (`docs/PLAN-v2.md:241`; `docs/reviews/v2-review-hosting.md:365-375`):

| Request shape per 60 minutes | Requests | Cost at $2/1k | Cost at $5/1k |
|---|---:|---:|---:|
| Current dense 12 s / 9 s windows | 400 | $0.80 | $2.00 |
| Sparse 1 / 30 s | 120 | $0.24 | $0.60 |
| Half-density | 200 | $0.40 | $1.00 |
| Sparse + 80 targeted | 200 | $0.40 | $1.00 |
| AudD enterprise 1 / 12 s | 300 | $0.60 | $1.50 |

The 400 count follows `DEFAULT_WINDOW_MS=12,000` and `DEFAULT_HOP_MS=9,000` at `src/id_detector/providers/base.py:14-18`, including the tail rule at `src/id_detector/windows.py:125-138`. AudD publishes the same $5 pay-as-you-go/$2-starting-rate inputs and 12-second enterprise cadence: [pricing](https://audd.io/), [enterprise documentation](https://docs.audd.io/enterprise/).

Consequences:

- `docs/PLAN-v2.md:286-288` says sparse 120 + targeted 80 costs $0.65–$1.10. It is 200 requests, hence **$0.40–$1.00**, before infrastructure.
- Four hundred Deep minutes are 6.67 hours: **$2.67–$6.67** for the proposed 200-request/hour hosted Deep recipe.
- Pro additionally advertises 1,500 standard minutes. If additive, those 25 hours cost **$6–$15**. Full use therefore costs **$8.67–$21.67 in AudD alone**, before VAT, Stripe, proxy bandwidth, storage and compute. A £7 plan cannot have the claimed ≥50% gross margin.
- If Pro users cannot consume 1,500 standard minutes in addition to 400 Deep minutes, the plan must remove the entitlement; the current text contradicts “Deep on every mix”: `docs/PLAN-v2.md:277-284,301-305`.
- Free 120 minutes costs **$0.48–$1.20 per active account/month**. That is supportable only with strong signup controls and a modeled conversion rate. The market review itself recommended 60 minutes, not 120: `docs/research/05-market-2026-09.md:674-686`.
- A 300-minute pack costs $2–$5 in API; £6 is thin at the high rate. A 1,200-minute pack costs $8–$20; £20 is already underwater at walk-up pricing before other costs.
- Local Deep’s AudD cost is $0.16–$0.40 for 80 clips, not generically “~$0.40”; hosted Deep is $0.40–$1.00.

Abuse and accounting risks:

- A user can publish or circulate one paid Deep URL so every free account receives Deep results. That may be intended marketing, but it materially weakens the upsell.
- A cached Standard result upgraded to Deep still incurs cost. Cache eligibility must compare recipes/capabilities and charge only the delta.
- Current final artefacts are mutable singleton paths—`fuse/episodes.json` and `present/tracklist.*`: `src/id_detector/fuse/episodes.py:1135-1151`, `src/id_detector/present/exports.py:375-442`. A later Standard run can overwrite a Deep result.
- New URLs for identical audio still incur download/proxy cost before byte-level dedupe; mutable URLs can return stale cached material.
- Failed, cancelled and deliberately malformed jobs can consume provider requests and then refund all minutes. Failed spend must still count against separate per-account/IP/global abuse ceilings.
- Concurrent submissions can all pass a balance check unless minutes and USD are atomically reserved before queue insertion.
- “Cached free” plus a public catalogue creates scraping and export abuse; account/IP limits alone are weak against IPv6/VPN rotation.
- Non-expiring packs need an immutable credit ledger, consumption order, chargeback reversal and liability accounting—not merely `minutes_remaining`.
- The length rules contradict themselves: 4 hours is described as a hard paid cap while `>4 h` “counts double” at `docs/PLAN-v2.md:371-374`.

The recommendation to use AudD rather than unofficial Shazam for hosted Free is directionally correct, but it must be described as **“contract-pending licensed-provider mode”**, and its quality must be measured on real mixes before pricing.

## D. Phase plan

The universal commands are syntactically reasonable, but the default pytest configuration excludes slow/live coverage: `pyproject.toml:38-43`. Several other gates depend on uncommitted local `work/` data or unspecified hardware.

| Phase | Assessment |
|---|---|
| **0** | Too large for one cycle. It combines the crash, cache protocol, retries, concurrency, cancellation, spend accounting, effective configuration, UI provenance and removal of two subsystems. E-M9 and E-M10 are listed but have no corresponding implementation/test. “Retry ambiguous once next run” can still rebill an accepted request. Add cases for successful no-match, all failures/fallback, partial errors, cancellation, cached-vs-live cap, global-transform targeting, count accumulation and profile-effective config. Patch the current loopback paid POST with CSRF/Origin checking—or disable paid web submission—here, not Phase 4. Split into **0a emergency correctness/spend/security** and **0b adapter reliability/config/provenance**. |
| **1** | Correct strategic point, but not buildable safely as written: missing AudD anchor, unsafe evidence-bucket rule, unscaled 80-clip allocation, `hint_only` exclusion, and no precise floor/suppression semantics. The Holly gate relies on local cached AudD data, has no committed baseline or command, and cannot run in CI. Add a committed recorded-response regression fixture plus a separately named owner-only Holly smoke test. This should be combined with the removal of paid-first from Phase 0 to avoid building capped paid-first only to delete it. |
| **2** | “After a successful fuse delete windows” is dangerous: the first fuse occurs before Deep targeting at `src/id_detector/cli.py:606-761`. Cleanup must occur only after the final secondary pass, re-fuse, presentation and successful job commit. Deleting the original later breaks `_load_cached`. Timing `<1.5 s`, disk `<90 MB`, and “fresh 60-min mix” lack a fixed fixture, cold/warm definition and measurement command. The profile-byte gate tests profile JSON, not Local Free output. Split runtime startup cleanup from destructive subsystem deletion/retention. |
| **3** | Too broad: data consistency, terminology, accessibility, responsive design, theme and screenshots. `card count == page count == CUE row count` is underspecified and can be wrong because CUE emits ID gaps as `TRACK` rows: `src/id_detector/present/exports.py:462-476`. Compare typed track/gap entries across JSON, Markdown, CUE, page and Copy instead. “Atomic bundle” needs immutable versioned directories plus an atomic manifest/pointer, not several independently replaced files. Headless Edge and Node commands/environment must be specified. Split **3a canonical projection/accessibility** from **3b visual polish**. |
| **4a** | Too large: framework port, templates, public pipeline boundary, migrations, persistent queue, recovery, progress, asset extraction and numerous UI bugs. New `src/idea_web` will not enter the wheel because packaging currently includes only `src/id_detector`: `pyproject.toml:35-36`. `idea serve` and `PROJECT_ROOT` also require changes to `src/id_detector`, contradicting the rule that it is never changed after Phase 3: `docs/PLAN-v2.md:347-359`, `src/id_detector/cli.py:91-99`. Split **web parity/public pipeline API** from **durable queue/idempotent recovery**. |
| **4b** | Too large: complete auth, verification/reset, CSRF, tenancy, global cache, quota accounting, sharing, public SEO, deletion and admin. `mixes(owner, visibility, share_token)` cannot represent one globally cached mix attached privately to multiple users. Use separate content/mix, URL alias, user-library, analysis-run/result-variant and share tables. Console email is suitable for tests, not hosted verification. Split auth/security, tenancy/cache/quota, then sharing/public/admin. |
| **5** | LocalBilling followed by Stripe is good ordering, but these are two cycles. `IDEA_BILLING=off` versus `BILLING_BACKEND=none`, production refusal and admin-only grants are contradictory. Add annual-price parity, pack-payment status, asynchronous payment events, refund/dispute/chargeback handling, tax/VAT decisions and an immutable entitlement ledger. Give exact Stripe CLI/test-clock commands and expected state transitions. |
| **6a** | YouTube egress, AudD contract shape and ingestion policy are commercial prerequisites, not final-phase discoveries. Security scope omits SSRF, redirects/private IPs, streaming size limits, temporary-disk quotas, malicious-media resource limits and opaque upload IDs. The SoundCloud gate must use an authorized test source. “GC ≤25 MB” needs an artefact manifest and fixture. Split the external feasibility spikes before Phase 1, then container/ingestion hardening, then retention/restore. |
| **6b** | Observability and policy arrive too late. Minimum paid-spend metrics, alarms, audit logs and operational health are required when the durable worker is introduced. Legal/provider approval must precede commercial launch and should not be excluded from a supposedly complete launch gate. Add backup restore, migration rollback, job-drain, provider-circuit-breaker and incident-runbook tests. |

The local-mode gate must exercise the owner’s actual `idea.cmd` path on Windows, not only `uv run idea serve --no-open`. Define whether “survives” means the default browser workflow only or full CLI/API backward compatibility; the current “unchanged” promise is false.

## E. Hosting and architecture

### Framework, process and storage

FastAPI/Jinja2 is plausible, and moving jobs to a separate persistent worker is mandatory. The hosting review itself calls Django/allauth a close alternative that could halve the custom auth work: `docs/reviews/v2-review-hosting.md:600-623`. Given how much of the product is now auth, billing, admin and migrations, require a short ADR/spike before committing.

SQLite WAL plus one worker is reasonable at initial scale, but copying the current store is not plug-and-play. `AsyncJobStore` owns an exclusive cross-process lock and a single writer task: `src/id_detector/jobs.py:40-90,177-205`. The web and worker need ordinary transactional connections, atomic leasing and busy-timeout handling, not that process-wide ownership model.

Persisted job rows survive page reloads. Surviving process death safely additionally requires:

- phase checkpoints and an attempt counter;
- idempotent artefact commits;
- durable request/ambiguous-response records;
- atomic quota and USD reservations;
- stale-lease recovery with maximum attempts/dead-letter state;
- cancellation/drain semantics;
- duplicate-work coalescing.

Otherwise restart recovery is merely at-least-once and can rebill AudD after a crash between network acceptance and cache commit.

Backing up only SQLite is insufficient when results, evidence and the public catalogue live in `work/`. Either back up immutable result/evidence objects or formally make them disposable and remove claims of permanent cached results. A restore drill must cover both database and artefacts.

### Auth, sessions and quotas

The §4 server-side session design is sound: hashed random token, `__Host-` cookie, rotation and expiry at `docs/PLAN-v2.md:361-370`. It contradicts §3’s signed-cookie and deferred-verification design at `docs/PLAN-v2.md:307-312`. Choose the §4 version.

CSRF synchronizer tokens are appropriate. Add login/reset rate limits, Origin checking, CSP/security headers, trusted-proxy configuration, IPv6 normalization and single-use hashed email tokens. Email verification cannot depend on console links in production.

Quotas need a ledger/reservation model, not mutable period counters. Define duration rounding, period boundary timezone, subscription-versus-pack consumption order, concurrent submissions, cancellations and partial failures. Free is monthly in the tier table but ISO-weekly in cache rules/data-model fields: `docs/PLAN-v2.md:277-284,301-305,382-387`.

### Stripe

The no-real-charge test strategy is valid in principle: Stripe sandboxes/test keys cannot create real charges, and test clocks can advance subscriptions. [Stripe sandbox CLI](https://docs.stripe.com/cli/sandbox), [billing testing](https://docs.stripe.com/billing/testing?locale=en-GB), [test keys](https://docs.stripe.com/keys?locale=en-GB).

The raw-body signature, server-owned price IDs, event-ID dedupe and subscription re-fetch design are good: `docs/PLAN-v2.md:405-426`. Missing details include:

- granting packs only after confirmed paid status;
- `checkout.session.async_payment_succeeded/failed`;
- refunds, disputes and chargebacks;
- tax/VAT and invoice behavior;
- test-clock timing for the 0341 renewal scenario;
- separate admin grants rather than a single subscription row whose source is either Stripe or admin.

Define three explicit modes: `off` for a production private beta/admin grants, `local` for simulated development billing and `stripe` for sandbox/live configuration. Live mode must additionally require live-key prefixes and an explicit launch flag.

### Hosted input/output boundary

“Reject local paths and never serve originals” is necessary but incomplete.

- The current validator accepts `file://` and any existing path: `src/id_detector/webapp/jobs.py:59-84`. Hosted jobs should carry discriminated platform URLs or opaque upload IDs; workers should never receive user-controlled filesystem paths.
- Allow only supported platform hosts and protect every fetch against SSRF, private/reserved addresses, DNS rebinding and unsafe redirects.
- Stream uploads into per-job temporary directories with hard byte/time/disk limits; do not trust filenames or MIME types; constrain ffmpeg/ffprobe CPU, memory, duration and subprocess time.
- The renderer automatically inserts `../<original path>` whenever an original exists: `src/id_detector/present/page.py:1168-1172`. Add an explicit hosted render policy so the audio element is never emitted.
- Authorize every result/export route and prevent traversal/symlink escapes; denying only `ingest/original.*` is insufficient.
- Public-link submissions should not become public automatically. Default to private and require an explicit publication grant; public catalogue entries need correction/takedown ownership.
- The present media/content locks raise on duplicate work rather than coalescing it: `src/id_detector/cli.py:412-422`. Two URLs can converge on one media directory after download and collide.

## F. Missing or not operationalized

- **Immutable analysis recipes and result variants:** Standard, Deep, degraded, parser version and presentation version need stable identities rather than one mutable result.
- **A real-mix release corpus:** current tiers remain provisional and paid engines unevaluated: `docs/STATUS.md:20-34`. No consumer accuracy promise should launch without a labeled, representative multi-mix gate.
- **Provider contract gate:** written permission for consumer resale, cache retention, attribution, concurrency and raw-response retention must precede implementation decisions.
- **Commercial integration policy:** the old checklist requires disabling unofficial Shazam and several scraped hint sources until reviewed: `docs/PLAN.md:203-204`; v2 keeps them at `docs/PLAN-v2.md:203-208`.
- **Transactional email:** verification/reset plus job-complete/failure notifications; users cannot monitor a 10–40-minute tab reliably.
- **Data protection operations:** controller basis, processor agreements, subprocessor list, international transfers, DSAR/export, erasure, breach process, backup deletion and retention for logs/IPs/tokens.
- **UK consumer/tax treatment:** VAT-inclusive prices, subscription renewal notices, cooling-off/immediate digital-service consent, refund policy and non-expiring-credit liability.
- **Public-content governance:** DMCA/takedown, repeat-infringer policy, corrections, claimant verification and what account deletion does to shared/public cache entries.
- **Artifact/database migrations:** forward migration, rollback, partial-deploy compatibility and page/export schema migration.
- **Operational readiness:** readiness/liveness, alert thresholds, restore drills, disk exhaustion behavior, provider outage runbooks, job draining and admin audit logs.
- **Security testing:** SSRF, traversal, malicious media, stored XSS from titles/comments, session fixation, quota races, webhook races, load/soak and dependency/container scanning.
- **Global provider controls:** durable request/dollar ceilings across all accounts/workers, not just per-run configuration.
- **Version-status product decision:** keep the work/version distinction in data because it is central to the evidence model and future report tier (`README.md:70-99`, `docs/PLAN.md:213`). Hide the column when uniformly unverified; display it only when verified or contested.

## G. What I would cut from the first sellable version

Ship a narrow private beta:

- SoundCloud links plus explicitly authorized uploads only; cut YouTube proxying, Mixcloud and scraped comments until terms and VPS behavior are settled.
- AudD-only hosted recognition under a written production agreement; keep Shazam+AudD Deep local-only until a licensed independent hosted provider exists.
- Private results, history, exports and completion email; cut the public SEO catalogue, automatic publication, share pages and OG work until takedown/privacy/data ownership are designed.
- Start with one non-expiring pack and a small free welcome allowance; defer subscriptions, annual billing, multiple pack sizes, priority promises and the contradictory 1,500-minute Pro allowance until real usage establishes COGS.
- Keep correctness, mobile accessibility, canonical exports and honest uncertainty; defer broad theme/copy polish.
- Defer destructive legacy-code removal until the hosted hot path and compatibility contract are stable. Disable risky routes without immediately deleting research tooling.
- Do not cut auth, verified email, CSRF, quota/spend reservations, retention, backups, basic admin, legal review or the real-mix quality gate.

## Required changes

1. **P0** — Resolve AudD production resale/caching/attribution terms and permitted ingestion sources before fixing prices or declaring any hosted tier licensed.
2. **P0** — Remove unofficial Shazam from every commercial hosted default until written permission exists, or replace it with a licensed independent provider and revise the Deep promise.
3. **P0** — Rewrite Deep corroboration to use temporally overlapping selected votes for the same work across explicit trust families, and parse/validate AudD anchors.
4. **P0** — Replace the static half/half sampler with duration-scaled, episode-aware targeting that prioritizes `hint_only` rows and reserves clips for multiple non-overlapping confirmations.
5. **P0** — Recalculate Free, Pro and pack pricing from all additive allowances, VAT/fees/proxy/compute, then remove the 1,500-minute entitlement or raise the £7 price.
6. **P0** — Introduce immutable analysis recipes/result variants plus atomic quota, credit and USD reservation ledgers before implementing cross-user free caching.
7. **P0** — Move CSRF/Origin protection, verified production email, SSRF controls and untrusted-media limits ahead of any internet-facing or paid-provider endpoint.
8. **P0** — Move cleanup to terminal successful completion, reconcile original deletion with cache behavior, and back up durable result/evidence artefacts with the database.
9. **P0** — Add a representative labeled real-mix release gate for each hosted recipe before advertising accuracy, “confirmed twice,” or tighter boundaries.
10. **P1** — Correct E-C1, E-C2, E-C3, U-F3 and malformed-Shazam wording to the qualified findings in section A.
11. **P1** — Reconcile monthly versus weekly quota periods, signed versus server-side sessions, deferred versus required verification, length caps and billing backend names.
12. **P1** — Split Phases 0, 3, 4a and 4b into the smaller cycles identified above and move legal/egress feasibility spikes before engine/product commitments.
13. **P1** — Replace local `work/` and hardware-dependent gates with committed fixtures, exact commands, typed assertions and separately labeled owner-only live checks.
14. **P1** — Add `idea_web` packaging, a public pipeline service API, phase-aware idempotency and a real Windows `idea.cmd` compatibility gate.
15. **P1** — Expand Stripe tests to asynchronous pack payments, refunds/disputes, test-clock renewal timing, tax decisions and separate admin-grant entitlements.
16. **P1** — Define multi-user cache ownership with separate content, URL alias, user-library, analysis-run/result-variant and share/publication records.
17. **P2** — Keep version provenance in data while conditionally hiding the uniformly unverified UI column.
18. **P2** — Reduce the first sellable release to the private, licensed, pack-first scope in section G.

VERDICT: CHANGES_REQUESTED
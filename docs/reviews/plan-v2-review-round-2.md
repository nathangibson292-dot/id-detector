## A. Round-1 required changes

| # | Status | Rev 2 coverage and remaining gap |
|---:|---|---|
| 1 | **Partially addressed** | §§3.3, 4.1, 6.3 L1 defer published pricing pending AudD terms, but L1 does not block hosted private-beta Deep use, and §3.3 still fixes £9/£6 prices before the rate and permitted use are known. |
| 2 | **Addressed by owner override** | D1 and §§3.2/4.1 explicitly retain unofficial Shazam and document the accepted risk; the mitigations are directionally adequate, but the persistent per-egress request budget and service-wide circuit breaker are not operationally specified. |
| 3 | **Partially addressed** | §§2.3.2/2.3.5 and Phase 1 add AudD anchors, separate trial sources, selected votes, trust families and temporal overlap; exact overlap, separation, and “sufficient same-engine support” thresholds remain undefined. |
| 4 | **Partially addressed** | §2.3.4 and Phase 1 add episode-aware priority, duration scaling and confirmation reserve; `20–30%`, “high-audio-quality”, overflow priority, and behavior when episodes outnumber available clips are unspecified. |
| 5 | **Partially addressed** | §3.3 removes the 1,500-minute entitlement, raises Pro to £9 and gives plausible conditional arithmetic; annual allowance resets, retry/failure COGS, free-tier cash costs and pack sizing remain incomplete. |
| 6 | **Partially addressed** | §§3.4, 4.5 and Phase 4d introduce recipes, variants and ledgers, but Phase 1 cannot enforce immutable variants before Phase 3a creates versioned bundles, and ledger authority/schema and recipe dominance remain undefined. |
| 7 | **Partially addressed** | Loopback CSRF moves to 0a; hosted SSRF to 4a, auth/email to 4c, and media limits to 6a. The plan never explicitly prevents non-loopback/internet exposure until all those controls are present. |
| 8 | **Partially addressed** | §§4.6 and Phases 2a/2b move success cleanup after final commit, replace original-dependent cache validation and back up durable artifacts; failed/cancelled cleanup, invalidated completion sidecars and consistent DB/artifact backup are unresolved. |
| 9 | **Partially addressed** | §6.3 L3 adds five owner-verified mixes and per-recipe reporting, but “representative” is undefined and no minimum precision/recall acceptance threshold makes it an actual release gate. |
| 10 | **Addressed** | §§2.1–2.2 use the qualified E-C1/E-C2/E-C3/U-F3 wording and correctly describe malformed Shazam bodies as failures. |
| 11 | **Partially addressed** | §§3.3–3.5 reconcile weekly Free periods, server-side sessions, required verification, length caps and billing backend names; annual subscriptions still conflict with “reset on the Stripe billing period.” |
| 12 | **Partially addressed** | S, 0a/0b, 2a/2b, 3a/3b and 4a–4d improve sequencing, but Phases 1, 3a, 4a, 4b, 4c, 4d and 6a remain larger than one credible build-and-review cycle. |
| 13 | **Partially addressed** | §5 promises committed fixtures and exact commands, but several gates still contain placeholders, nonexistent scripts, unspecified fixtures, owner hardware, or prose-only procedures. |
| 14 | **Partially addressed** | §§4.3 and 5/4a–4b name `idea_web` packaging, `id_detector.service`, checkpoints and a Windows `idea.cmd` gate; the service API lacks run/idempotency/checkpoint context needed for restart-safe resumption. |
| 15 | **Partially addressed** | §§4.7 and 5a/5b cover asynchronous packs, refunds, disputes, tax, annual products and separate grants; the phase gate omits dispute/tax assertions and defers the exact test-clock sequence to a future document. |
| 16 | **Partially addressed** | §4.5 separates media, aliases, runs, bundles, libraries, publications and shares; several links remain ambiguous or incorrectly media-scoped, and mutable URLs are not versioned. |
| 17 | **Addressed** | §§2.5, 4.5 and Phase 3a preserve version provenance while making the UI column conditional. |
| 18 | **Not addressed** | The endpoint is called a private beta, but the plan still builds subscriptions, Stripe, public publication/catalogue, sharing, broad signup, three Deep variants and visual polish before that beta; it is neither pack-first nor materially reduced. |

## B. New defects introduced by rev 2

### Deep scan v2 (§2.3)

- `Deep economy` is Shazam-first (§2.3 cost table; Phase 1 CLI), directly contradicting D2 and §§0/3.2, which define every Deep scan as AudD-first.
- Recipe-defining values are ranges or adjectives: concurrency `3–4`, confirmation reserve `20–30%`, “temporally overlapping”, “two separated”, “sufficient support”, “worth challenging” and “high-audio-quality”. Two builders can produce different `recipe_id` semantics.
- “At least one window per episode” is impossible when eligible episodes exceed the 70–80% non-reserved cap; no deterministic overflow or tie-break rule is given.
- The status set conflates result completeness and job outcome. `provider_unavailable`, `budget_exhausted`, `partial` and `degraded` have no defined exit code, refund, cache eligibility or presentation behavior.
- A degraded AudD-only result still records the requested `recipe_id`; without an achieved-capabilities field, exact/superset cache lookup can later serve it as complete Deep.
- Phase 1 requires byte-identical Local Free output while also adding `recipe_id` to every result/manifest and changing crowd-row fusion. A Phase 0b golden cannot remain byte-identical under those changes.

### Pricing (§3.3)

- “A Deep request on a Standard result runs/pays only the secondary pass” is wrong in §§0, 3.3 and 3.4. Standard already has the Shazam sweep; under D2 the missing paid work is the full AudD primary sweep.
- “Pro resets on the Stripe billing period” gives annual subscribers one allowance per year, while the table promises `N Deep minutes/month`.
- “Free-tier cost is capacity, not dollars” contradicts the next sentences and the plan’s own proxy, compute and storage costs.
- The margin table has no allowance for paid retries, ambiguous accepted requests, failed-run COGS, refunds or fixed payment fees; those are material for small £6 purchases.
- `N`, pack sizes and the AudD request price are runtime configuration, but no versioned commercial-price/cost input is attached to reservations or result cost records.

### Recipes and ledgers (§3.4)

- Phase 1 promises lesser-never-overwrites-greater, but immutable versioned presentation bundles do not arrive until Phase 3a. Current `fuse/episodes.json` and `present/tracklist.*` are mutable singletons.
- “Capability superset” has no partial-order definition. Free full-Shazam and Deep sampled-Shazam-plus-AudD are not observation-set supersets of each other.
- Presentation settings are both part of `recipe_id` and separately tracked by `presentation_version` in §4.5. A theme/projection change should not force a paid reanalysis.
- `quota_ledger` includes `source=pack`, while a separate `credit_ledger` also consumes packs; the authoritative balance and atomic cross-ledger reservation are unspecified.
- Typed provider cache states conflict with “error bodies never cached.” Response cache, attempt journal and ambiguous-submission record need separate state machines.
- The Phase 0b “retry-without-rebill” gate is not implementable for an ambiguous AudD clip request unless AudD supplies an idempotency/reconciliation contract. Recording a sidecar cannot prove the request was not billed.

### Data model (§4.5)

- `analysis_runs` lacks the initiating user/reservation and requested-versus-achieved capabilities.
- `library_items` identifies only media, while multiple recipe/result bundles may exist; the library cannot deterministically choose what the user owns or sees.
- `publications` is media-scoped and `shares` is “media/bundle”; both must point to an immutable bundle or explicit latest-version policy.
- A canonical URL can later yield different bytes, so `source_aliases(url → media)` needs temporal/version semantics rather than an assumed permanent mapping.
- No upload table represents opaque upload IDs, ownership, expiry and temporary paths required by §4.8.
- No provider-request/egress ledger represents D1’s global daily Shazam budget per IP.
- No order/payment record maps an asynchronous pack Checkout, PaymentIntent/Charge, refund or dispute to the precise credit-ledger entries it must reverse.
- `analysis_runs`, `paid_spend`, `usd_ledger` and provider request records overlap without a stated source of truth.

### Jobs, retention and backups (§4.6)

- “Same media → attach to the running job” is incorrect for incompatible recipes and impossible before a new alias has been ingested and hashed.
- Coalesced work needs subscriber/attachment rows: one user cancelling must not cancel another user’s underlying run, and quota reservations need deterministic release/settlement.
- `id_detector.service.analyse(target, recipe, progress)` lacks a stable run ID, checkpoint input and idempotency context, so Phase 4b cannot resume the current `_analyse`, which creates a fresh random run ID.
- Cleanup only after terminal success leaves failed and cancelled jobs’ 330–370 MB/hour work trees indefinitely, creating a disk-exhaustion path.
- Deleting `windows/**` invalidates existing fuse/window completion sidecars because they verify the continued existence and hash of their upstream window files (`io.py:148-176`, `episodes.py:1135-1151`).
- After PCM/original expiry, a Standard-to-Deep upgrade cannot perform missing AudD work without re-fetching and rebuilding windows; unavailable or mutable source URLs are not handled.
- Tier history retention is not implemented: §3.3 says 30/90 days or unlimited, while §4.6 keeps every durable bundle indefinitely without reference-counted GC for libraries, shares and publications.
- Litestream plus separate rclone copies do not by themselves produce a consistent DB-to-artifact snapshot or define restore ordering.

### Gates and phase size

- Phase 0a’s CLI gate uses `<fixture.wav>`, but no committed audio fixture exists and `paid_scan_adapters` is injectable only into `_analyse`, not through an environment variable or `idea analyse`. It would also run live Shazam.
- Phase 0a requires cancellation during the paid pass, but paid-loop progress/cancellation is explicitly implemented in Phase 0b.
- “Profile budget flows through” is unsafe as written: both frozen profiles currently contain `max_usd_e2=0`. Zero-as-cap disables paid calls; zero-as-unlimited fails the spend requirement.
- Disabling `run_paid_scanners` in 0a leaves `--engine acrcloud` accepted but ineffective until Phase 2a refuses it.
- Phase 2a names both a direct `-X importtime` command and a different future benchmark script; it does not say which produces the three-run cold median.
- The 60-minute GC fixture, presentation fixtures, page-JS script, startup script, VPS script, screenshot script and Holly test do not currently exist or have precise generation commands.
- Phases 1, 3a and 4a–4d each combine multiple independent schemas/algorithms/UI surfaces and remain too large; “split if over one day” transfers planning decisions back to the builder.
- The persistent service-wide Shazam breaker is assigned to local Phase 1 even though the shared database/worker architecture needed to implement it does not exist until Phase 4.

## C. Can a builder start Phase S/0a without questions?

**No.** A builder would have to guess:

- Which S tasks Codex owns, and the credentials, VPS, residential proxy and authorised source URLs for S2/S3.
- The exact S2/S3 commands, run duration, output schema, throttle stopping rules and acceptable results.
- Who decides S4 and what criteria choose FastAPI versus Django/allauth.
- The offline fixture and mechanism for injecting fake AudD and Shazam into the Phase 0a CLI gate.
- Whether `max_usd_e2=0` means no spend or unlimited spend, its default, precedence and how frozen profiles remain untouched.
- The AudD clip price input and how half-cent requests are rounded for reservation, journal and cap enforcement.
- `--allow-degrade` syntax, exit codes, result statuses and which paid failures qualify.
- Cache-state files, TTLs, transitions and whether failures/ambiguous responses retain raw bodies.
- Whether to move the cancellation test to 0b or pull cancellation implementation into 0a.
- CSRF token/session lifetime and behavior for JSON clients on the current sessionless loopback server.
- How `config show --profile` is invoked and whether file or frozen-profile RPM/budget values win.
- What `--engine acrcloud` should do immediately after the whole-file call site is disabled.

## D. Cut or defer for the first private beta

Keep one AudD-first Deep recipe plus Hosted Free per D1; defer `deep-half`/`deep-economy` product variants until L1 and real-mix measurements choose one density.

Defer:

- Stripe, annual billing and customer purchases; ship `BILLING_BACKEND=off` with audited admin grants, while preserving D5 for the later commercial release.
- Public catalogue/publications, SEO pages, share links and OG metadata.
- Phase 3b visual polish and refreshed marketing screenshots.
- Self-service signup, disposable-email screening and Turnstile; use invite-only, verified accounts with reset support.
- Public admin UI beyond queue, spend, disk, user disable and deletion essentials.
- Hosted Panako/JDK and user-facing “build index”; retain and fix the local wiring as D3 requires.
- Multiple pack sizes, annual-price presentation and subscription portals until actual payments are enabled.
- Report-tier presentation; retain provenance/version fields as D6 requires.

Do not defer spend caps, cache correctness, canonical bundles, durable jobs, auth/CSRF/SSRF/media limits, retention, backups, provider permission for hosted AudD, or the real-mix quality gate.

## Required changes

1. **P0** — Replace Phase 0a’s placeholder CLI gate with a named committed fixture and exact offline test command using explicit adapter injection, and move its cancellation assertion to 0b or implement cancellation in 0a.
2. **P0** — Define the AudD clip price, cent-rounding/reservation algorithm, cap precedence and explicit meaning of `max_usd_e2=0`, accounting for the frozen profiles’ current zero values.
3. **P0** — Remove or rename the Shazam-first `deep-economy` recipe so every recipe marketed or exposed as Deep remains AudD-first under D2.
4. **P0** — Move immutable result bundles into Phase 1 and define requested versus achieved capabilities, cache dominance and degraded-result eligibility before enabling recipe caching.
5. **P0** — Correct Standard-to-Deep upgrade semantics: reuse cached Shazam evidence, run the missing AudD sweep, and reserve/charge the actual incremental paid work.
6. **P0** — Make L1 block any third-party hosted AudD use, including private beta, unless that beta is explicitly within written evaluation permission and attribution terms.
7. **P0** — For ambiguous AudD clip outcomes, prohibit automatic retry unless L1 confirms a provider idempotency/reconciliation mechanism; otherwise count possible spend and finish partial.
8. **P0** — Define bounded cleanup for failed/cancelled jobs and replace or preserve completion-sidecar dependencies before deleting `windows/**`.
9. **P1** — Replace all Deep algorithm ranges and adjectives with recipe fields, exact thresholds, deterministic tie-breaks and a cap-overflow rule.
10. **P1** — Separate analysis-recipe identity from presentation version and make the Local Free golden comparison semantic or exclude explicitly versioned provenance fields.
11. **P1** — Specify ledger schemas and one atomic transaction for plan/pack/admin allocation, reservation, settlement, refund, expiry and payment reversal.
12. **P1** — Add immutable bundle references, upload/payment/provider-request records and requested/achieved capability fields to §4.5, with versioned URL aliases.
13. **P1** — Define recipe-aware job coalescing, subscriber cancellation/quota settlement and a resumable service API carrying stable run and idempotency identifiers.
14. **P1** — Fix annual allowance resets, include retry/failure/free-tier costs in COGS, and define status-to-refund/cache/exit-code behavior.
15. **P1** — Split Phases 1, 3a, 4a–4d and 6a into executable cycles and give every gate a named fixture, exact command and numeric assertion.
16. **P1** — Keep all web phases loopback-only until auth, SSRF, media limits and transactional global Shazam budgets/circuit breaking have passed their gates.
17. **P1** — Give L3 a representative-mix definition, exact scoring command and minimum per-recipe precision/recall thresholds.
18. **P2** — Reduce private beta to invite-only admin grants, one Deep recipe and private results; defer Stripe, public catalogue/sharing and visual polish.

VERDICT: CHANGES_REQUESTED
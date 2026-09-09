## A. Round-2 required changes

| # | Status | Rev-3 coverage and remaining gap |
|---:|---|---|
| 1 | **Partially addressed** | §5 prelude and Phases 0a/0b name fixtures, fakes, and move cancellation to 0b; the 0a command still cannot run because `--recipe` arrives in 1a, its environment syntax mixes POSIX and `cmd.exe`, and `IDEA_FAKE_SCRIPT` plus the journal assertion command are missing. |
| 2 | **Partially addressed** | §2.3 step 1 and 0a define the price, cent ceiling, zero semantics, and frozen-profile treatment; the formula wrongly reserves AudD dollars for free Shazam probes, and precedence between recipe, operator, account, and global caps remains incomplete. |
| 3 | **Addressed** | §2.3 and §3.2 define only `free` and AudD-first `deep`; `deep-economy` is gone. |
| 4 | **Partially addressed** | §2.3, §3.4, and 1a move immutable bundles and achieved capabilities into Phase 1; the capability relation is internally false, dominance is query-relative rather than one global pointer, and the proposed bundle key cannot support presentation variants. |
| 5 | **Addressed** | §2.3 dominance and the 1a gate explicitly reuse cached Shazam evidence, run only the AudD sweep, and reserve primary work; execution still depends on repairing the capability model in #4. |
| 6 | **Addressed** | §3.3, §4.1, and L1 explicitly block all hosted third-party AudD use, including private beta, pending written production terms. |
| 7 | **Partially addressed** | §2.3 step 2 and 0b say ambiguous outcomes are spent and never automatically retried; the recipe simultaneously mandates retries on “timeout,” which is itself an ambiguous post-dispatch outcome unless further classified. |
| 8 | **Partially addressed** | §3.4, §4.6, and 2b add bounded failed/cancelled cleanup and a pruned-sidecar scheme; `budget_exhausted`, dead-letter, abandoned upload, and `source_unavailable` cleanup are omitted, and the consumer/re-derivation protocol is underspecified. |
| 9 | **Partially addressed** | §2.3 gives fixed rates, thresholds, priorities, and an overflow rule; largest-remainder ties, reserve-window search bounds/refill, density parity, anchor validation, and several eligibility/span definitions remain non-deterministic. |
| 10 | **Addressed** | §2.3, §3.4, 0b, and 1a separate presentation version from recipe identity and make the Local Free golden semantic. |
| 11 | **Partially addressed** | §3.4 and 4d-i replace counters with ledgers and `BEGIN IMMEDIATE`; the schema cannot allocate reservations across expiring plan lots versus non-expiring packs or reverse already-consumed grants deterministically. |
| 12 | **Partially addressed** | §4.5 adds uploads, orders, provider requests, bundle references, and requested/achieved fields; bundle version identity, library ownership, source-alias history, provider attempt events, and payment/invoice relationships remain inadequate. |
| 13 | **Partially addressed** | §4.3, §4.5, and 4b-i add stable run IDs, checkpoints, subscribers, and recipe-aware coalescing; the key ignores hint/index context, cancellation settlement is undefined, and “journaled before request” is not safe idempotency. |
| 14 | **Partially addressed** | §3.3 fixes monthly annual-plan credits and adds a 5% overhead; no monthly-credit scheduler exists for annual Stripe subscriptions, free/failure COGS remains incomplete, and degraded/partial credit refunds have no numeric rule. |
| 15 | **Partially addressed** | §5 splits several phases and names more fixtures; many gates remain prose rather than exact commands, several referenced scripts do not yet have specified interfaces, and 0a, 0b, 1b, 4a-i, 4d-i, 4d-ii, 5a, and 6a-i remain multi-cycle builds. |
| 16 | **Partially addressed** | §3.1 and Phase 6a-ii introduce `IDEA_HOSTED_READY`; checking only a non-loopback bind does not prevent Caddy from exposing a loopback-bound app, and the proposed provider table cannot implement the required rolling breaker. |
| 17 | **Partially addressed** | L3 now defines a corpus and numeric thresholds; its command is not executable—current `benchmark score` accepts `--truth`, `--episodes`, and `--out`, not `--corpus` or `--recipe`, and `<frozen>` remains a placeholder. |
| 18 | **Partially addressed** | §3.6 configures invite-only admin grants, one Deep recipe, private results, and no polish requirement; it still requires Stripe, sharing/publication, and broad account features to be built before the beta rather than deferring them. |

## B. New and remaining defects

The cited baseline defects were reverified against the current code. The paid branch still has conditionally bound summary variables in `cli.py:499-521,794-815`; paid targeting is gap-only; AudD clips remain anchorless; error bodies are cached before parsing; the web runner omits `local_index_label`; paths and `file://` are accepted; pages emit the original audio; exports and refresh overwrite singleton artefacts; profiles drop runtime settings; the wheel packages only `src/id_detector`; and default pytest excludes the full-pipeline modules.

### §2.3 — Deep scan v2

- The reservation formula charges `(primary_clips + secondary_cap)` at the AudD price even though the secondary engine is Shazam. A 60-minute run reserves $2.60 for $2.00 of possible AudD work.

- This makes the advertised 240-minute Deep limit unusable at defaults. Density 1 reserves roughly $10.40 and density 2 roughly $6.40, both exceeding the recipe’s $4 hard cap; even density 2’s actual AudD cost is only $4.

- `free` requires `shazam_sweep`, while a normal Deep result achieves `shazam_secondary`, not `shazam_sweep`. Therefore the declared subset test does **not** let Deep serve Free. The sentence claiming this works because the full sweep is a superset proves the implication in the opposite direction.

- Density 1 and density 2 have identical capability sets. Under the stated rule, a cheaper half-density result can satisfy a later full-density request despite having a different `recipe_id` and fewer observations.

- A single `present/current` chosen by capability count cannot represent dominance. Free full-Shazam and degraded AudD-only results are incomparable, and a partial result is not excluded from §3.4’s “most capabilities, then newest” selection.

- Retries on timeout conflict with “ambiguous is never retried.” A read timeout after upload may have been billed; it cannot safely enter the same state as a pre-connect failure.

- Missing primary windows up to 5%, or Shazam failures up to 20%, appear to produce `complete` while still claiming full sweep capabilities. The plan does not define whether capabilities mean adapter support, attempted coverage, or successful coverage.

- `max_paid_clips` is “honoured” in 0a while Deep requires a full AudD sweep. The current default is 150 versus about 400 windows/hour; truncation status and achieved capabilities are undefined.

- Confirmation allocation still lacks deterministic largest-remainder ties, a bounded definition of “around” a match, refill after overlapping picks are dropped, and behavior when two reserve confirmations 30 seconds apart do not exist.

- “Anchor from `timecode`” does not specify rejection of absent, negative, malformed, or implausible values. The existing helper accepts negative numeric timecodes and current clip observations have `anchor=None`.

### §3.3 — pricing

- `docs/pricing.toml`, `AppConfig.audd_usd_e6_per_request`, and the recipe’s `max_usd_e2` create three configuration authorities, but no phase establishes their precedence or even creates `docs/pricing.toml`.

- Annual subscriptions receive only one annual `invoice.paid`; §4.7 cannot implement twelve monthly credits without a separate idempotent monthly scheduler and end-of-month anniversary rules.

- The 5% request overhead does not model fully refunded failed runs, provider outages after spend, or retry rates separately. “Free-tier cash cost is £0 per mix” also omits allocated compute, bandwidth, backup, and capacity cost.

- “Secondary minutes refunded” is not meaningful with one audio-minute reservation. No fraction or conversion from a 2-probe/minute Shazam phase to customer minutes is defined.

### §§3.4–4.5 — recipes, bundles, ledgers, and tenancy

- `present/bundles/<run_id>/` plus `result_bundles(run_id PK)` permits only one presentation per analysis run, contradicting refresh creating a new immutable presentation bundle from the same fuse run. A distinct `bundle_id` is required.

- Cache and coalescing identity omit analysis inputs. The same media and recipe can have different pasted tracklists, platform comments, source aliases, or Panako index versions. Coalescing those runs can leak one user’s hints and return a different analysis than requested.

- Panako is retained under D3 but neither its index ID/version nor its catalogue snapshot appears in `recipe_id`, `analysis_runs`, or the coalescing key.

- `library_items(user, media_key)` displays the globally dominant bundle. A user can therefore receive another user’s Deep result or private-hint result, and the row cannot encode whether its retention is 30 days, 90 days, or unlimited.

- `source_aliases(..., superseded_by)` lacks a version key, validity interval, and revalidation rule. A mutable platform URL can continue serving old bytes indefinitely.

- A signed aggregate credit balance cannot enforce “plan before pack,” expire only unused plan minutes, or refund the precise lots consumed. Reservations need allocation rows against individual grants.

- `usd_ledger` is described as “the same shape” at three scopes without defining whether one reservation creates three rows, which row is authoritative, or how actual microdollar attempts settle a cent-rounded reservation.

- `provider_requests(provider, egress_id, day, count)` cannot calculate a rolling five-minute failure rate, associate attempts with runs, distinguish decode/429/success outcomes, or reconcile an ambiguous paid request.

- `orders.credit_ledger_ref` is singular and there is no invoice/payment/refund allocation model. It cannot represent twelve subscription credits, partial refunds, multiple reversal rows, or dispute resolution.

### §4.6 — jobs, retention, and backups

- Writing an attempt journal before the network call does not create exactly-once behavior. A crash before send causes the resumed job to skip necessary work; a crash after provider acceptance cannot prove whether retrying will rebill it.

- `(media_key, recipe)` is not a sufficient coalescing key, and the plan never says whether an attached subscriber is charged, refunded as a cache hit, or allocated a share of the underlying run. “Settled” on cancellation also conflicts with the status table’s “refunded.”

- Cleanup omits `budget_exhausted`, `source_unavailable`, dead-letter jobs, expired upload staging areas, and jobs that die before a pipeline status is committed.

- The backup text does not define a write fence or immutable artefact list tied transactionally to the recorded database point. A generic SQLite checkpoint plus later file copy is not by itself a restorable DB/artefact snapshot.

- Re-fetching after PCM expiry must require the fetched bytes to hash to the original `media_key`; a changed source must not silently continue an old run.

### Gates and phase execution

- The plan says 24 cycles but contains 23 `### Phase` headings; Phase S itself contains four independently gated activities.

- The Phase 0a command is invalid in both PowerShell and `cmd.exe`, does not set the required fake script, invokes the not-yet-implemented `--recipe`, and gives no exact command that verifies the journal.

- Current `_analyse` injects only paid adapters. Fake Shazam requires a new injection boundary, but neither its interface nor the scripted fixture outcomes are specified.

- Moving pages into `present/bundles/<run_id>/` breaks current assumptions in `webapp/runner.py`, `present/server.py`, `present/refresh.py`, and the page’s `../ingest/original.*` audio URL. Phase 1a has no local-browser compatibility gate for this migration.

- Phase 2b deletes `scan.py`, but current `paid_clip.py` imports `PaidScanResult` from it. The shared result contract must be relocated before deletion.

- L3’s scoring command does not exist. The currently implemented `benchmark score` interface cannot run the stated per-recipe gate.

- Most later gates say “tests green,” “header tests,” or “restore drill passes” without an exact test selector, fixture-generation command, or assertion schema. Axe also has no pinned dependency or offline invocation.

- The `IDEA_HOSTED_READY` bind check is bypassed by the documented architecture: uvicorn remains on loopback while Caddy makes it public. Hosted-mode startup itself must require the readiness state.

- D1’s residual risk remains accepted, but its mitigation is incomplete: the request schema cannot drive the rolling breaker, and D4’s residential proxy must be scoped only to yt-dlp. Current `httpx` clients can inherit process proxy environment variables unless explicitly prevented.

## C. Can a builder start Phase S/0a without questions?

**No.** A builder can begin isolated bug fixes, but cannot complete the Phase 0a contract without guessing:

- Whether `recipes.py` and `--recipe` move into 0a or the gate should use the legacy profile/engine interface.

- The exact FakeAudD/FakeShazam script, expected per-window outcomes, Shazam injection API, and valid Windows command.

- Whether USD reservation covers only billable AudD calls, potential retries, or Shazam probes too.

- Which cap wins among recipe, `AppConfig`, pricing version, account-month, and global-day limits.

- Whether the existing 150-clip limit truncates Deep, is removed, or changes its result status.

- Which timeout stages are safely retryable and which become spent ambiguous attempts.

- The exact definitions of `complete`, `partial`, and achieved sweep capabilities below the 5%/20% thresholds.

- How a sessionless JSON client obtains and refreshes the loopback CSRF token and which Origin/Host combinations are accepted.

- Which command asserts the reserved/spent journal values and where that journal is located under the temporary work root.

- Whether S4’s ADR author makes the framework decision or merely recommends one for owner approval.

## D. Cut or defer for the first private beta

Keep the correctness work, D1 breaker and budgets, L1, private-result authorization, durable jobs, auth/CSRF, SSRF/media limits, ledgers, bounded cleanup, backups, and L3.

Defer without reversing D1–D6:

- Phase 5b Stripe integration until after the two-week billing-off beta; retain both subscription and pack domain models and local simulations under D5.

- Share links, publication/catalogue tables and routes, OG metadata, and DMCA UI until a later public-result milestone.

- Disposable-email automation, Turnstile, and general self-service signup; beta needs invite creation, verification, login/reset, disable, and deletion only.

- Phase 3b visual polish and refreshed marketing screenshots.

- Nonessential admin screens; keep queue, spend, breaker, disk, grants, user disable/delete, and audit log.

- Annual-plan edge cases and multiple pack sizes until payments are enabled; preserve their schema direction.

- Physical deletion of ACRCloud, the whole-file scanner, and 1001Tracklists until the new service API and compatibility suite are stable; disable them first and retain the recovery tag.

## Required changes

1. **P0** — Move minimal recipe/CLI support into 0a and provide one valid PowerShell gate that sets a named fake script, runs offline, and invokes an exact automated journal assertion.
2. **P0** — Reserve USD only for possible billable AudD attempts, define `min(recipe, operator, account, global)` cap precedence, and make the hard cap consistent with every advertised mix length/density.
3. **P0** — Replace raw capability-subset/cache-count dominance with an explicit versioned recipe-compatibility relation covering density, sweep quality, adapter versions, and full-Shazam-to-secondary reuse.
4. **P0** — Key runs and coalescing by an analysis-input fingerprint including source/hint snapshot, manual tracklist hash, and Panako index version, while sharing only content-safe provider observations.
5. **P0** — Treat every post-dispatch timeout as spent and non-retryable unless L1 supplies provider idempotency/reconciliation; add durable prepared/dispatched/resolved attempt states.
6. **P0** — Give each immutable bundle its own ID and bind library ownership to an authorized bundle/result variant rather than exposing a global media-level dominant bundle.
7. **P1** — Define numeric credit settlement/refund rules for complete, degraded, partial, failed, cancelled, and coalesced-subscriber outcomes.
8. **P1** — Add grant-allocation rows so reservations, expiry, settlement, refund, and reversal operate against exact plan/pack/admin credit lots atomically.
9. **P1** — Add provider-attempt events, invoices/payment allocations, recurring-credit scheduler rules, and complete refund/dispute relationships to §4.5.
10. **P1** — Specify cleanup for every terminal/dead-letter/abandoned state and a fenced database-to-immutable-artefact snapshot and restore protocol.
11. **P1** — Finish the Deep algorithm with anchor validity bounds, density parity, largest-remainder ties, reserve search/refill rules, and exact capability thresholds for incomplete sweeps.
12. **P1** — Split the remaining multi-surface phases and give every cycle a committed fixture, exact command, numeric assertions, and a Phase-1 Windows browser/audio compatibility gate.
13. **P1** — Replace L3’s nonexistent command with the implemented benchmark interface or schedule and gate the exact CLI extension, using a concrete frozen corpus path.
14. **P1** — Require hosted readiness whenever `IDEA_MODE=hosted` regardless of bind address, store timestamped breaker outcomes, and explicitly disable proxy-environment inheritance for Shazam.
15. **P1** — Define versioned source-alias revalidation and require byte-hash equality before resuming work after an original/PCM re-fetch.
16. **P2** — Remove Stripe, sharing/publication, general signup controls, visual polish, and destructive legacy deletion from the private-beta critical path while preserving D5/D6 for later milestones.

VERDICT: CHANGES_REQUESTED
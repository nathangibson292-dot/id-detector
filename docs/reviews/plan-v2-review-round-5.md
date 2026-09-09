## A. Round-4 required changes

| # | Status | Rev-5 coverage and remaining gap |
|---:|---|---|
| 1 | **Addressed** | §2.3.4 step 4 permits AudD/Shazam overlap, and 1b-i tests both densities plus an actual coincident window. |
| 2 | **Addressed** | §2.3.5 now has one status matrix covering HTTP 500, throttled Free, `partial`, `provider_unavailable`, and pre-spend `--allow-degrade`; 0a-ii asserts it. |
| 3 | **Partially addressed** | §2.3.2–2.3.3 add per-dispatch admission, refunds and a hard reservation, but `http_401/403` and quota errors used by the status gate have no legal attempt outcome or cost. |
| 4 | **Addressed** | §2.3.4 step 1, §3.5 and 4b-i/4d-i correctly make submission an intake job and perform resolve/coalesce/reserve/run atomically in the worker. |
| 5 | **Addressed** | §2.3.4 and §3.4 key analysis on source kind, hint/manual/index snapshots and tenant scope; private inputs cannot cross users, while clip responses remain content-addressed. |
| 6 | **Partially addressed** | §5 defines start/probe/stop smoke scripts and §3.1/6a-iv a persisted readiness file, but `/healthz` arrives too late and the compose readiness bootstrap remains contradictory. |
| 7 | **Partially addressed** | §2.3.1 adds algorithm/version identity and §3.4 gates Deep-to-Free serving on L3, but 4d-ii contradicts the density table and `serve_free_from_deep` is absent from §3.3’s authoritative fields. |
| 8 | **Partially addressed** | §3.5 defines expiry, settlement and zero-subscriber cancellation, but an attaching subscriber receives no reservation that can become “the settling one” after initiator detachment. |
| 9 | **Partially addressed** | §2.3.3/§4.5 add query identity, lineage, unit price and append-only attempts and remove duplicate admin grants; events lack their own ordering key and dispute restoration remains unspecified. |
| 10 | **Partially addressed** | §4.6 uses SQLite online backup, manifests and a GC lock, but takes the artefact lock only after the DB snapshot, leaving a deterministic snapshot-versus-GC deletion race. |
| 11 | **Partially addressed** | §3.3 now names caps, pricing fields and `cache_hits`, but `pricing.toml`’s 110/70-minute allowances contradict its own provisional 90/55-minute walk-up margin row. |
| 12 | **Partially addressed** | §5 expands the plan to 29 M1 and 6 M2 cycles with named selectors, but several cycles remain multi-day and multiple gates depend on deliverables scheduled later. |
| 13 | **Partially addressed** | 1b-iii defines a run-list and field mapping, but `work_recall_e4` is not a field emitted by `idea benchmark score`, and corpus weights/output units remain undefined. |
| 14 | **Addressed** | §2.3.5 separates rolling cooldown, UTC daily reset and three-trip latch, with sparse AudD explicitly post-L1 and operator-only. |
| 15 | **Partially addressed** | §5’s fake interfaces now match the actual async AudD and Shazam boundaries and the stale CLI citations are fixed; E-S5’s scorer-field citation is still false. |

## B. New and remaining defects in rev 5

### §2.3 — Deep scan v2

- **The attempt state machine cannot represent its own status gate.** §2.3.3 permits only `match`, `no_match`, 429, 503, generic 5xx, malformed, connect and timeout outcomes. Section 2.3.5 and the Phase-0 fixtures require `http_401/403` and quota-error results. Their billing/refund treatment is consequently undefined.

- **Phase 0a-ii cannot honestly produce its required `complete` Deep run.** A complete Deep recipe requires `shazam_secondary`; deterministic secondary targeting is not implemented until 1b-i. The current code only performs gap-fill targeting at `cli.py:659-682`, so accepting that as the recipe capability would store results produced by the wrong algorithm.

- `suppressed_min_votes` is frozen into the recipe but never connected to the definition of `suppressed_challengeable`. Adaptive-reserve exhaustion when several blank probes discover identities is also unspecified.

- Step 5 calls `T_ind ≥ 4, on-air ≥ 30 s` the “existing likely rule,” but current fusion uses `T_ind ≥ 4`, span ≥40 s, no competition and global alignment at `fuse/episodes.py:496-504`. A builder must not silently replace that rule.

### §3.3 — pricing

- The authoritative values say `pro.deep_minutes_per_month = 110` and `pack.small.minutes = 70`; the COGS table says the current walk-up/density-1 margin supports approximately 90 and 55 respectively. The prose makes 110 conditional on L1 while instructing Phase 0a-ii to write it before L1.

- The “3% of gross” reserve is treated as $0.35 per audio hour, although it is a fixed allowance-level reserve—and a £6 pack’s 3% is not $0.35. The displayed arithmetic is conservative in some rows but is not a reproducible pricing formula.

### §§3.4–4.5 — compatibility, ledgers and schema

- **Density compatibility has mutually exclusive gates.** The §3.4 table and 1a-ii correctly say stored d=1 serves a d=2 request, but 4d-ii says a d=2 request is not served by d=1 “and vice-versa.”

- **Subscriber transfer remains impossible.** Step 1 attaches to an existing run without reserving credits/USD, while §3.5 releases the initiator’s reservation and promotes the earliest subscriber’s reservation. That subscriber has none. The 4b-i transfer assertion therefore has no implementable state transition.

- `provider_attempt_events` is append-only but has no `event_id` or monotonic sequence; multiple states share one `attempt_id`, so durable ordering and idempotent replay are under-specified.

- `serve_free_from_deep` is referenced as a `pricing.toml` field but is omitted from the authoritative field list. `accept_degraded` is required by `serves()` but absent from `RunRequest`.

- “Adapter versions ≥ requested” assumes arbitrary parser versions are monotonic capability upgrades. Exact equality or an explicit compatibility relation is needed.

- The zero-balance submit pre-check can prevent an uncounted compatible cache hit unless an alias-level compatibility lookup happens before rejection. That ordering is not stated.

### §4.6 — jobs, retention and backups

- **The backup ordering is unsafe.** It snapshots SQLite, then acquires the artefact lock. Between those steps GC can delete a bundle still referenced by the copied DB, after removing its reference from the live DB. The promised concurrent backup/GC gate can therefore fail legitimately.

- Bundle publication, checkpoint commits and evidence writes lack a required ordering of “durable files and hashes first, DB reference/checkpoint second.” Without that invariant, online DB backup can capture references to incomplete artefacts.

- Copying all referenced evidence while holding a lock “≤60 s” is not credible beyond a very small beta. A versioned snapshot/trash scheme will eventually be needed even after correctness is fixed.

### Gates and phase sizing

- **The first smoke gate is unrunnable at its phase.** Shared infrastructure and 0a-i require `GET /healthz == 200`, but the current server has no route and the plan schedules `/healthz` only in 4b-ii.

- **Hosted readiness still has two contradictions.** Docker Compose cannot automatically populate `IDEA_IMAGE_DIGEST` “from the image label,” and 6a-iii exposes a compose stack through Caddy using the gate-only bypass that §3.1 restricts to a loopback gate container.

- `docker compose … run gate` omits `--build`, so it may test a stale existing image rather than the current source.

- The hosted-ready live SoundCloud analysis has no named authorised URL/config input, making the release gate environment-dependent without a specified interface.

- The scorer wrapper maps `work_recall` to nonexistent `work_recall_e4`; the actual current path is `overall.identification_work.recall_e4`. “Corpus-weighted mean” does not say whether precision is weighted by predictions, recall by truth rows, or each mix equally; output values are compared as decimals while source metrics are e4 integers.

- Breaker “failure rate >30%” does not define qualifying outcomes, denominator or minimum sample size. As written, the first malformed response opens it at 100%.

- Clearly oversized cycles remain: 0a-ii, 0b-i, 1a-i, 2b, 4a-i, 4a-ii, 4b-i, 4c-i and 4d-i. FastAPI parity and durable queue/coalescing are especially unlikely to fit one build-review day.

- Section 6b can pass with L1–L5 incomplete while the document elsewhere says L1 blocks the beta. It should distinguish “M1 build complete” from “private beta authorised”; L1/L2/L3/L5 are manual beta launch conditions, while L4 belongs to payment launch.

## C. Can Codex start Phase S/0a-i without questions?

**No.** The isolated crash and cache fix can begin, but completing the first boundary requires guessing:

- Whether `/healthz` must be pulled into 0a-i or removed from its smoke gate.

- Whether minimal secondary targeting moves into 0a-ii, or Deep remains non-complete until 1b-i.

- What attempt outcome and cost represent AudD authentication and quota responses.

- Whether `--refresh-states http_429` refers to an attempt record, given that HTTP 429 bodies are forbidden from the response cache.

- Which of the contradictory d=1/d=2 compatibility assertions is authoritative.

- How an unreserved coalesced subscriber becomes liable when the initiating subscriber detaches.

- How compose obtains a trustworthy image identity and when the gate-only startup bypass may be used.

- Which lock/commit order makes DB, bundle, checkpoint and GC state one restorable snapshot.

- The exact scorer paths, weighting denominators and output units used by L3.

- Which provisional allowance is authoritative: 110/70 from `pricing.toml` or 90/55 from the COGS table.

- Which Shazam outcomes count toward the rolling breaker and how much traffic is required before it opens.

- Whether 6b means “build complete” or permission to begin the beta despite unresolved manual launch gates.

## D. Cut or defer for the first private beta

The plan already correctly defers Stripe, sharing/publication, uploads/YouTube, email delivery, broad signup controls, visual polish and physical legacy deletion.

Further reductions:

- Move the monthly subscription-lot scheduler from 4d-iv to M2; M1 billing is off and uses admin lots only. Keep account/global USD ceilings in M1.

- Remove `processed_stripe_events` and Stripe-specific entitlement fields from the M1 migration; introduce them with 7a-ii.

- Defer invite-code self-signup. Use only admin-created, pre-verified accounts during the first private beta; retain login, session security and admin reset.

- Move S2’s YouTube/proxy execution to immediately before 7c. Its script may be prepared earlier, but it does not block M1.

- Keep automated accessibility assertions, but make screenshot curation and the committed axe report non-blocking beta evidence.

- Defer broad plan/pack expiry permutations beyond the invariants needed for admin lots; complete them before billing is enabled.

## Required changes

1. **P0** — Add explicit `auth_error` and `quota_error` attempt outcomes, with cost/refund/retry rules, and use them consistently in §2.3.3, the status matrix and fake scripts.

2. **P0** — Add `/healthz` to 0a-i or change that phase’s smoke endpoint so its gate only depends on work delivered by 0a-i.

3. **P0** — Either deliver the minimal recipe-correct Shazam secondary scheduler in 0a-ii or forbid `complete` Deep results until 1b-i and change the Phase-0 gate accordingly.

4. **P0** — Correct 4d-ii to assert the §3.4 density relation: stored d=1 serves d=2, while stored d=2 does not serve d=1.

5. **P0** — Define attach-time provisional allocations and an atomic payer-transfer rule, including successor credit/USD-cap failure, so initiator detachment has an executable transition.

6. **P0** — Replace implicit image-digest discovery with a named build ID baked into the image and readiness file, and restrict the gate bypass to the gate service before starting the Caddy production stack.

7. **P0** — Acquire the artefact lock before the SQLite snapshot and hold it through reference enumeration/copy, or use versioned trash, while requiring artefact publication before DB references/checkpoints.

8. **P1** — Make `pricing.toml` and the COGS calculation agree on one pre-L1 allowance—90 Pro minutes and 55 pack minutes under the current stated walk-up assumptions.

9. **P1** — Map L3 to `overall.identification_work.recall_e4`, specify numerator-weighted aggregation and e4-versus-decimal output units, and fixture those exact calculations.

10. **P1** — Define breaker failure outcomes, denominator and minimum sample count while preserving the separate cooldown, UTC reset and latch rules.

11. **P1** — Add `serve_free_from_deep` to the authoritative pricing schema, carry `accept_degraded` in the local request contract, and replace numeric adapter-version comparison with explicit compatibility.

12. **P1** — Give `provider_attempt_events` an append-only event identity/order and specify refund/dispute restoration before the Stripe cycle begins.

13. **P1** — Split 0a-ii, 0b-i, 1a-i, 2b, 4a-i/ii, 4b-i, 4c-i and 4d-i so each cycle owns one coherent state transition and a gate using only prior/current deliverables.

14. **P1** — Make zero-balance submission perform an alias-compatible cache lookup before refusal, or define a cache-only intake that cannot fetch or start paid work.

15. **P1** — State that 6b marks M1 build completion only and that private-beta activation separately requires recorded L1/L2/L3/L5 sign-off.

16. **P2** — Move the monthly lot scheduler, invite self-signup, Stripe-only schema and non-blocking screenshot artefacts out of the first private-beta critical path.

VERDICT: CHANGES_REQUESTED
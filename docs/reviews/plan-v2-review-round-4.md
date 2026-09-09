## A. Round-3 required changes

| # | Status | Rev-4 coverage and remaining gap |
|---:|---|---|
| 1 | **Addressed** | §5 shared infrastructure and 0a-i now provide recipe support, named fakes, valid PowerShell syntax, an offline command, and an automated journal assertion. |
| 2 | **Partially addressed** | §§2.3.1–2.3.2 reserve only AudD work and define `min(recipe, operator, account, global)`; retry billing can still exceed the 5% reservation and hard cap. |
| 3 | **Partially addressed** | §3.4 adds an explicit versioned density/status/adapter compatibility table and Deep-on-Free reuse; its claim that Deep is strictly more evidence than Free is false because Deep has at most 120 Shazam probes versus Free’s 400. |
| 4 | **Partially addressed** | §2.3.4 step 1 and §3.4 add media, hints, manual-tracklist and Panako fingerprints; source/platform snapshot and private-upload/cache scope remain absent. |
| 5 | **Addressed** | §2.3.3 and 0b-i distinguish prepared/dispatched/resolved, make post-dispatch timeouts spent and non-retryable, and acknowledge bounded crash ambiguity. |
| 6 | **Addressed** | §3.4 gives every presentation bundle a distinct ID; §4.5 authorizes hosted access through a run-scoped library item rather than a global media pointer. |
| 7 | **Addressed** | §3.5 assigns numeric settlement to every status and charges coalesced subscribers zero; initiator cancellation remains a separate lifecycle defect below. |
| 8 | **Partially addressed** | §3.5 adds grant lots and allocations, but atomicity is stated only for reservation, and releasing after expiry or reversing consumed grants is unresolved. |
| 9 | **Partially addressed** | §§3.3, 4.5 and 4.7 add the scheduler, invoices and allocation links; `provider_attempts` is mutable rather than event-based, while refund/dispute and charge relationships remain incomplete. |
| 10 | **Partially addressed** | §§4.6 and 2b cover every named terminal/dead-letter/upload state; the proposed DB/artifact backup is not actually fenced against DB writers, checkpoints or GC. |
| 11 | **Partially addressed** | §2.3.4 supplies anchor bounds, density selection, thresholds, tie-breaks and reserve bounds; its cross-engine overlap exclusion makes the secondary algorithm nonfunctional. |
| 12 | **Partially addressed** | §5 now has 26 M1 cycles and many exact selectors, including a Phase-1 Windows/audio gate; several cycles remain multi-day and several sub-gates are blocking, prose-only or lack assertions. |
| 13 | **Partially addressed** | 1b-ii and L3 schedule an exact `score_corpus.py` wrapper and concrete corpus path; the run-list schema and derivation of “listed precision” from the current scorer are unspecified and untested. |
| 14 | **Partially addressed** | §§3.1, 4.1 and 4b-ii require hosted readiness regardless of bind, timestamp attempts and disable proxy inheritance; the hosted-ready gate cannot bootstrap or persist the flag as written. |
| 15 | **Addressed** | §3.4 adds alias validity intervals/revalidation and requires re-fetched bytes to equal the original byte-hash `media_key`; 2b gates the changed-source case. |
| 16 | **Addressed** | §3.6 and M1/M2 move Stripe, sharing, signup controls, polish and destructive deletion beyond the private-beta critical path. |

## B. New and remaining defects

Code re-verification confirms the cited baseline: the paid branch still reads conditionally bound `matches`/`recognised`; AudD clips remain anchorless; paid errors are cached before parsing; the web queue remains in-memory; exports and refresh use mutable singleton paths; ingestion’s `media_key` is a byte hash; and the wheel packages only `src/id_detector`.

### §2.3 — Deep scan v2

- **The secondary pass selects zero windows.** Step 4 rejects a pick overlapping any already-picked window “any engine, this run.” At density 1 every Shazam window is exactly an AudD window; at density 2 each intervening 12-second window overlaps an AudD window by three seconds. This also makes the required ≥6-second cross-family corroboration impossible.

- **Status rules conflict.** §2.3.3 makes `http_500` spent/ambiguous and §2.3.4 makes a sub-95% primary sweep `partial`; 0a-i instead requires all `http_500` to return `provider_unavailable`, then permits `--allow-degrade`. Likewise Free Shazam is both the primary requirement and described as `degraded`, not `partial`, when throttled.

- **The reservation is not a hard spend cap.** The 5% headroom is statistical, while the recipe permits up to three retries that “may bill.” Neither admission before each dispatch nor worst-case retry reservation is specified. The 0b test simultaneously declares two 429s plus a match to be three attempts but only one billed attempt.

- `resolved(...)` omits the later-used `ambiguous` outcome, and no exact cost is assigned to 429/503. A builder cannot derive settlement or retry admission consistently.

- Marking `dispatched` only after bytes are sent leaves a crash window in which a billed call remains `prepared` and is reissued. To preserve the stated semantics, the durable transition must occur conservatively before entering network I/O.

- Deep cannot presently be called “strictly more evidence” than Free. AudD plus ≤2 Shazam probes/minute is not a superset of a 6.7-probe/minute full Shazam sweep. L3 might justify semantic compatibility, but the plan currently asserts structural dominance.

- Most outcome-affecting constants in step 4—95%/80% thresholds, four-second eligibility, vote threshold, reserve bounds and anchor limits—are absent from the recipe identity. Changing them could reuse results produced by a different algorithm under the same `recipe_id`.

### §3.3 — pricing

- The COGS table omits its stated “3% of gross” failure/outage allowance. For example, `$2.35/hour` is exactly AudD headroom + proxy share + compute; the additional allowance is not included, so the displayed 110-minute allowance misses the stated 50% target.

- “N from the table” leaves four possible Pro allowances. Phase 0a-i creates the authoritative `pricing.toml`, but neither L1 nor a pre-L1 provisional row tells the builder which value to write.

- Account-month and global-day USD ceilings have no configured values or named configuration fields, and their reservation transaction is not specified.

- The advertised 20 compatible-cache-hits/day has no usage record, enforcement phase or gate.

### §§3.4–4.5 — recipes, ledgers and data model

- **Submission order is impossible as written.** §3.5 atomically reserves minutes with the job insert, but the web process initially knows neither byte-hash `media_key`, trusted duration, planned windows, hints snapshot nor final `analysis_key`. Those are produced only after the worker ingests the target, while §4.2 says the web process never runs the pipeline.

- The fingerprint lacks a source snapshot and visibility scope. Equal bytes from two platforms can coalesce despite different title/embed/acquisition metadata; uploaded/private media can cross-user coalesce unless it also contains a manual tracklist.

- Coalesced accounting has an orphan-payer case: only the initiating reservation settles, but any subscriber may detach and release “their” reservation. If the initiator detaches while another subscriber remains, the run continues with no defined reservation to settle.

- A reservation released after its plan lot expires may resurrect expired minutes because §3.5 says to release to the same lots. The expiry/release ordering needs a deterministic rule.

- `provider_attempts` lacks `query_id`/clip cache key, retry lineage, ordinal and recorded unit price. Consequently it cannot identify which prepared request to resume or reconcile.

- `entitlement_grants` duplicates the `credit_grants(kind=admin)` authority. Refund/dispute storage also lacks charge/refund identifiers, amounts and dispute-resolution restoration.

- `RunRequest.manual_tracklist_path` crosses the service boundary as a filesystem path despite the stated opaque-input boundary. Hosted mode needs an internal artifact/blob ID or validated content.

### §4.6 — jobs, retention and backups

- `PRAGMA wal_checkpoint(TRUNCATE)` followed by copying `app.db` does not fence other database writers or automatic checkpoints. Pausing only bundle commits is insufficient for a consistent SQLite file copy.

- The lock does not explicitly fence GC. A referenced immutable directory may be deleted after `snapshot.json` is written but before it is copied.

- Bundle manifests do not establish hashes for every listed fuse run, `recognise/`, `hints/` and `ingest/source.json`; therefore `verify-artefacts` cannot verify the full promised snapshot.

- “Cancel = subscriber detach” never defines the zero-subscriber transition, when the worker cancellation token fires, or how an in-flight paid request settles.

- Partial results are never served but retain durable provider artifacts without a bounded run/media deletion rule.

### Gates, phases and owner-risk mitigations

- `uv run idea serve --no-open --port 8791` is a blocking process, not an executable CI smoke gate; no background start, readiness request, assertion or termination is given.

- The 6a-iii gate is circular: hosted web refuses to start without `IDEA_HOSTED_READY=1`, yet the end-to-end gate must start it before the script supposedly writes that value. A child shell also cannot persist an environment variable into its parent unless it writes a named environment/config artifact.

- The fake contract says `FakeAudD.recognize_clip(bytes)`, while the actual adapter is `recognize_clip(path, on_attempt)`. A deliberate common protocol or adapter refactor must be stated.

- 0a-i, 1a, 1b-ii, 4a-i, 4a-ii, 4b-ii, 4d-i and 6a-iii remain larger than a credible one-day build-and-review cycle.

- L3 does not define `runs-<recipe>.json`, and the existing scorer exposes overall work precision and empirical tier precision—not a defined presentation-filtered “listed precision.”

- D1 is not re-raised. Its mitigations are much stronger, but daily-budget exhaustion must wait until UTC reset rather than close after the generic 30-minute cooldown. The “sparse AudD” fallback also conflicts with the frozen Free recipe, no-mid-run-swap rule and L1 unless defined as a post-L1 operator-only contingency.

- Citation drift: §2.1 E-H1’s current successful/cancelled/failed zero-cost writes are at `cli.py:821,833,845`, not `:820/:850/:864`; unqualified `jobs.py` references should distinguish `webapp/jobs.py` from `id_detector/jobs.py`.

## C. Can a builder start Phase S/0a-i without questions?

**No.** Isolated crash/cache fixes can start, but completing 0a-i requires guessing:

- Whether all post-dispatch HTTP 500s produce `partial`, `provider_unavailable`, or a Free fallback.

- Which exact failures `--allow-degrade` covers without violating the no-mid-run-swap rule.

- Whether retryable 429/503 attempts cost zero, consume reserved headroom, or are blocked once the reservation is exhausted.

- Whether `dispatched` is durably recorded before network entry or after observable byte transmission.

- Whether the fake protocols replace the current adapter signatures or require wrappers.

- How a Deep run can be `complete` during 0a-i before secondary targeting and its achieved threshold arrive in 1b-i.

- Which provisional Pro allowance and pack size belong in the authoritative `pricing.toml`.

- Whether `AppConfig.max_usd_e2` is nullable, where it is loaded from, and how absence differs from explicit zero.

- Which algorithm constants are part of canonical recipe JSON rather than unversioned code constants.

## D. Cut or defer for the first private beta

Keep engine correctness, L1/L3, authenticated private results, CSRF, durable jobs, credits/USD caps, D1’s breaker, SSRF for enabled sources, retention and restorable backups.

Further reductions consistent with D1–D8:

- Defer Phase 2a startup/import optimization; it does not block a private beta.

- Defer `BILLING_BACKEND=local`, recurring subscription grants, orders and invoices alongside Stripe; beta needs only audited admin credit lots and `BILLING_BACKEND=off`.

- Defer uploads and YouTube from the beta, limiting initial invites to SoundCloud/Mixcloud. Preserve D4/D7 for the later paid launch.

- Defer job-complete email and wall-clock progress modelling; retain durable polling, plain failure messages and accurate run status.

- Defer publication/share tables themselves to the M2 migration rather than carrying unused M1 schema.

- Reduce `/admin` to queue, spend, breaker, disk, grants, disable/delete and audit; defer broader operational UI.

- Use admin-created, pre-verified beta accounts and manual password reset if transactional email would otherwise hold up the beta.

## Required changes

1. **P0** — Allow Shazam windows to overlap AudD windows; deduplicate only redundant same-engine probes and gate both densities on nonzero Shazam requests plus an actual cross-family corroboration.

2. **P0** — Publish one per-recipe status matrix and make 0a/0b tests agree on HTTP 500, throttled Free primary, `partial`, `provider_unavailable`, and `--allow-degrade`.

3. **P0** — Make every potentially billable dispatch atomically consume retry reservation before network I/O, define cost by outcome, and prohibit dispatch once the hard USD cap would be exceeded.

4. **P0** — Replace submit-time analysis reservation with an intake job followed by worker ingest/snapshot and one atomic resolve–coalesce–credit/USD-reserve–run transaction.

5. **P0** — Add source snapshot and visibility/tenant scope to compatibility and coalescing; never cross-user serve or coalesce private uploads, manual inputs or private indexes while retaining content-safe clip-response reuse.

6. **P0** — Replace the blocking 4a-ii smoke command and circular 6a-iii readiness prose with exact start/probe/assert/stop commands and a named persisted readiness artifact produced by a pre-start gate container.

7. **P1** — Put every analysis-affecting targeting/status/fusion value under `recipe_id` or an explicit analysis-algorithm version, and condition Deep-to-Free serving on demonstrated semantic compatibility.

8. **P1** — Define atomic settlement/release/expiry order, expired-lot behavior, coalesced initiator transfer, and the zero-subscriber cancellation transition.

9. **P1** — Add query identity, retry lineage, unit price and append-only transitions to provider attempts; remove the duplicate admin-grant authority and normalize payment/refund/dispute allocations.

10. **P1** — Use SQLite’s online-backup mechanism or fence every writer/checkpointer, make GC share the artifact lock, and hash every snapshotted run/evidence artifact.

11. **P1** — Correct the COGS arithmetic, choose an explicit provisional `pricing.toml` row, and define configured account/global USD caps plus the 20/day cache-hit ledger.

12. **P1** — Split the remaining multi-surface cycles and give wheel inspection, scorer, server smoke, backup/GC concurrency and hosted readiness exact automated assertions.

13. **P1** — Define and fixture the L3 run-list schema and map each threshold to an exact current scorer field or a specified presentation-projection calculation.

14. **P1** — Separate failure-rate cooldown, UTC daily-budget reset and three-trip latch semantics, and remove or phase the sparse-AudD contingency behind L1.

15. **P2** — Align fake interfaces with production adapters and correct the stale/ambiguous source citations.

VERDICT: CHANGES_REQUESTED
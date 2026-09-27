Two realistic P1 blockers remain: browser approvals are not consumed atomically, and paid fusion can rename a Free-listed row.

## A Free path unchanged

- Pass. `FREE_RECIPE` remains unchanged at `src/id_detector/recipes.py:100-124`, including recipe ID `c5a3cc574d99ffa92e40c82cd4f8c8d13a94f53943bf9086bd564af39f3f63db`.
- Free retains the same compatibility/breaker order (`pipeline.py:853-857`), window schedule (`:886-910`), recognition arguments (`:975-996`), fusion (`:1127-1185`), and publication (`:1639-1663`). `running_free=True` is neutral for Free because the previous expression was already true.
- Stored-Free reuse is Deep-only (`pipeline.py:923-930`; `compat.py:227-236`), and all paid planning remains behind `elif deep` at `pipeline.py:1350`.
- The refactored Shazam evidence write at `pipeline.py:1245-1252` performs the same write that existed before.
- `tests/golden/local-free/tracklist.json` is byte-for-byte unchanged: both base and worktree have Git blob `316b6171c42f0772a9bce3066cf716491f22d181`. No Free request or user-visible output change was found.

## B Approval gates (CLI and browser)

- CLI gate is correctly after the free plan and before reservation: exact plan output and targeted count are at `cli.py:590-618`, invocation at `pipeline.py:1388-1402`, and reservation begins at `:1417`. `--yes`, zero-cost cached plans, default-No, and non-interactive refusal remain correct.
- Browser execution is bounded by both approved clip count and price at `webapp/runner.py:291-312`; a changed or larger plan stops with another offer.
- Approve/Skip require POST plus a synchronizer token (`application.py:713-742`). Foreign Origin/non-loopback Host are rejected (`http.py:241-270,332-348`); GET cannot reach the mutation route. Closing the page performs no POST. Hosted paid work is refused before intake or reservation (`jobs/worker.py:2661-2665`).
- **Blocker:** durable local approval is not atomic. `LocalJobs.approve_offer()` reads the open offer, commits the authorized follow-up job, and only afterwards conditionally consumes the offer (`jobs/local.py:377-425`). A concurrent Approve/Skip, database failure, or process crash can leave an authorized job queued while the offer remains reusable; the losing path cancels only after enqueue (`:425-427`) and may race a worker claim. Queue coalescing mitigates the usual case but is not an approval fence; after an ambiguous/cancelled first run, retrying the still-open offer can create a fresh run and re-buy clips.

## C Money authority and cache reuse

- Apart from the approval boundary above, the `bb57b70` authority remains intact: the SQLite claim/lease/run/cancel fence, durable reservation-before-dispatch, ambiguous-as-spent folding, and settlement code were not substantively changed.
- Reservation and dispatch ordering remains at `pipeline.py:1417-1481`; recovered reservations win over recomputation.
- Cached paid matches and no-matches use the unchanged key (`additive.py:83-104`) and are excluded from the quoted price. The live sweep forces `refresh_states=frozenset()` at `pipeline.py:1463-1465`; only explicit `--refresh` re-buys them.
- `providers/audd.py` and `windows.py` are unchanged. Endpoint, multipart fields, clip bytes/schedule/sample rate, and `CLIP_CONFIG_VERSION = "audd-main-v1"` (`paid_clip.py:83`) remain unchanged.

## D Fusion invariant and offline guard

- **Blocker:** `_same_work()` permits provider-node overlap to establish identity (`additive.py:266-274`), but `_preserves()` never checks the rendered label (`:277-305`). The confirming row then replaces the Free row (`:378-384`).
- A direct offline reproduction changed a listed Free label from `Zulu — Free` to `Alpha — Paid` while returning `MergeReport(confirmed=1)`. The existing tests use matching labels, so they do not exercise this ordinary provider-metadata disagreement. This violates the explicit “never rename” invariant.
- The offline guard is effective for these providers: it clears the AudD token, disables Shazam, and intercepts DNS plus `connect`, `connect_ex`, and `create_connection` (`scripts/offline_guard.py:34-85`). Both provider clients use ordinary HTTP/TCP, so attempted external requests are stopped rather than merely logged.

## E Legacy results, measurement and scope

- Legacy handling passes: the new Deep recipe identity is `additive:1,fusion:4`; a `targeting:1` primary is rejected without sending (`pipeline.py:911-919`), and old/pre-bundle Deep results remain outside Free-evidence reuse and refusion.
- The measurement is credible for the stated corpus: it re-fuses proven Free evidence, selects policies with production code, reads AudD by the production cache key, and scores with the project scorer. Pooled Free and every paid policy are 56/107; “AudD adds nothing on these mixes” is a fair, corpus-limited conclusion.
- Independent collection found exactly `2100/2197` tests with 97 deselected, matching the reported `2098 passed + 2 skipped`. Four pure additive-invariant tests also passed offline; no provider was called.
- Diff/status contain no changes under `work/`, `data/corpus/`, `profiles/`, the second-session files, or the named playlist paths. The builder’s before/after metadata comparison reports the external deepscan tree unchanged; it is outside this worktree, so Git cannot independently attest it.
- No credential, token, paid answer, absolute owner path, or other secret was found in new public files.
- The claim that no assertion was weakened is not exact: `tests/test_refusion.py:353,392` changed `first_audd.calls > 30` to `> 0`.

## Required fixes

### Realistic

- **P1:** Make offer consumption and authorized follow-up creation one SQLite transaction with a unique, durable approval identity. Add concurrent Approve/Approve, Approve/Skip, and crash-after-enqueue/retry tests proving one maximum paid run.
- **P1:** Enforce presentation-label preservation when a paid row confirms a listed Free row. Test differing Shazam/AudD artist-title metadata, not merely identical labels.

### Adversarial

- None beyond the realistic concurrency failure above; GET, CSRF, Host/Origin, cap, and hosted-refusal defenses are present.

## Follow-ups, not blockers

- **P2:** Replace the weakened `first_audd.calls > 0` assertions with exact expected gap-target/cache counts.
- **P2:** Make the golden test compare produced raw bytes; the checked-in golden is exact, but the current assertion is semantic JSON equality.
- **P2:** In the partially cached/no-credential path, avoid creating an internal reservation for unsendable clips so behavior matches the “nothing reserved” wording.
- **P2:** Reset or measure a delta of the offline guard’s global `BLOCKED` list when multiple measurements run in one process.

VERDICT: FIX_FIRST

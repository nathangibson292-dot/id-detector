## A — display channel

`display_only_labels` is populated only from verified `id_unknown` hints ([identity.py:1031](src/id_detector/fuse/identity.py:1031)). The normal identity path skips those hints before creating nodes, assertions or mappings ([identity.py:1180](src/id_detector/fuse/identity.py:1180)). The collision test also proves zero specificity, candidate/work mapping, episode backing, or identity node ([test_free_accuracy.py:774](tests/test_free_accuracy.py:774)).

However, the channel is not display-only in practice. `_candidate_label` consumes it ([exports.py:143](src/id_detector/present/exports.py:143)), and grouping uses that result as its title/work identity key ([grouping.py:310](src/id_detector/present/grouping.py:310)). A read-only reproduction with independent rows `artist - a` and `artist - b` plus `Artist - ID (A / B)` produced two candidates/two rows before the fix, but two candidates/one collapsed row after it.

The scorer also turns `_candidate_label` into `ListedWork` and feeds it to `match_works` ([score_corpus.py:1006](scripts/score_corpus.py:1006)). Thus placeholder wording can affect matches and counts.

## B — schema compatibility

The field defaults to an empty list and is omitted when empty ([contracts.py:392](src/id_detector/contracts.py:392)); the generated schema keeps it optional and matches the model.

Read-only comparison of the owner’s 16 old-format identity files found all 16 validate, with identical pre-fix/fixed canonical aggregates. Thirteen saved results rendered byte-identically; one pre-existing graph/episode mismatch failed identically in both versions.

Bundles and frozen runs remain byte-bound: manifests verify original file hashes ([bundles.py:53](src/id_detector/present/bundles.py:53)), `serves()` does not inspect identities ([compat.py:163](src/id_detector/compat.py:163)), and re-fusion verifies stored identity bytes without reserializing them ([refusion.py:282](src/id_detector/refusion.py:282)). All 14 owner-cache input checks had identical outcomes before/after: 13 accepted and the same pre-existing one refused.

An old-format fixture is exercised at [test_free_accuracy.py:649](tests/test_free_accuracy.py:649). No schema-version bump is needed. No additional fusion bump is needed because `fusion:5` belongs to the same still-uncommitted accuracy build ([recipes.py:24](src/id_detector/recipes.py:24)).

## C — tests and measurement

The collision, original fuller-label, unchanged MixesDB, and placeholder checks passed: 9 tests, zero network attempts. Running the new collision test against the pre-fix source failed at the fuller-row assertion, as required.

The stored combined score confirms 164/218 recall, 167/198 work precision, `likely` 63/63, and 217 rows. Its `rows.json` is byte-identical to the accuracy reference; the builder’s guarded log records zero network attempts ([build report:1236](docs/reviews/build-local-free-accuracy.md:1236)).

The Local Free golden is unchanged at SHA-256 `78FF49D38F53E323DE0CC63542BFE133C121610BAFAA8057FAD2001C31973931`. The existing accuracy assertion was replaced by the stronger requirement that the placeholder have no node, not loosened.

The isolated fix changes only the contract/schema, fusion producer, presentation consumer, collision tests, and required report. No status-listed file exists only here; no secret or unsuitable public content was added.

## Required fixes

### Realistic

- **P1:** Split identity/grouping/scoring labels from final display labels. Apply the placeholder wording only after grouping and scoring. Add a regression proving adjacent independent `a` and `b` rows remain two rows and are scored under their independent identities.

### Adversarial

- None.

## Follow-ups, not blockers

- The one saved owner result that already fails rendering/re-fusion should be repaired separately; this fix does not change its outcome.

VERDICT: FIX_FIRST

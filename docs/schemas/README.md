# Stage 0 contract conventions

All JSON records use schema version `1.0.0`, name their writer in `generated_by`, require nullable
fields to be present, store time in integer milliseconds, and reject floating-point values at any
depth. Confidence, rate, agreement, coverage, and probability-like quantities are integer
ten-thousandths; money uses integer cents where a contract names an `e2` field. Provider-native
decimal values must be normalised to named fixed-point integer fields before entering an artefact.

Canonical JSON is UTF-8 with keys sorted and no insignificant whitespace. Record ordering is
`(start_ms, id)` where `start_ms` exists and `id` otherwise. A natural key is encoded as a compact
canonical JSON array before being passed to `make_id(media_key, record_type, natural_key)`.

## Natural keys

| Record | Natural key components, in order |
|---|---|
| source | `canonical_url` |
| pcm | `media_key` |
| window | `generation`, `start_ms`, `support_ms[1]`, transform type, `rate_e4`, `semitones` |
| query | `provider`, `capability`, canonical target, provider-config filename, `scan_policy` |
| observation | `query_id`, `mix_span_ms`, canonical raw-label hash, provider-native index |
| hint | `connector`, connector source-record id |
| identity node | the node id, exactly `ns:value` |
| identity assertion | ordered `a`, `b`, relation, `source.record_id` |
| identity work | normalised `artist|title` |
| identity candidate | sorted member-node ids |
| episode | `candidate_id`, `occurrence_index`, first evidence-support start |
| gap | `start_ms`, `end_ms` |
| rescan request | `generation`, trigger, `start_ms`, `end_ms`, canonical policy |
| ground truth | `set_id` |
| benchmark report | `corpus_version`, profile, `config_hash` |
| invocation journal entry | invocation id supplied by the orchestrator |
| raw-index entry | `cache_key` |
| provider config | provider, immutable version filename |
| profile | profile name, immutable version filename |

`durations`, the identities aggregate, and the episodes aggregate have no standalone id: their
identity is their immutable stage path and parent generation. Identity works and candidates retain
their explicit deterministic ids inside the aggregate.

The provider-independent fields that are needed only to form a key (`source_record_id`,
`raw_label_hash`, `native_index`, and normalised identity text) are adapter inputs; they are not
duplicated in the resulting record when the plan does not include them.

## Completion sidecars

For an artefact named `X.ext`, the sibling is `X.done.json`. It contains only `schema_version`, the
artefact `sha256`, and an `upstream` object mapping logical input paths to their current SHA-256.
Writers close a temporary file in the destination directory before atomically replacing the final
path, which is safe on Windows.

## Stage 0 implementation decisions

- Query targets are a union of two closed objects: exactly `{window_id}` for clip/local-index
  queries, or exactly `{asset, asset_sha256}` for file-scanner queries.
- Scanner observations use the plan's capability-neutral logical trial id; callers derive it from
  provider and chunk index.
- Confidence fields whose plan names do not carry an `e4` suffix retain their plan names but their
  schemas constrain them to integer ten-thousandths.
- The SQLite `max_usd`, `reserved_usd`, `used_usd`, and `actual_usd` columns store integer cents;
  this is the smallest conventional fixed-point unit available without changing the plan's exact
  column names.
- Invocation and raw-index schemas enumerate the minimum fields implied by immutable artefact and
  provenance rules because revision 5 does not provide field-level definitions for those records.

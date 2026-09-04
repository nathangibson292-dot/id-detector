# Corpus layout

Each version lives at data/corpus/<corpus_version>/:

- <set_id>/ground_truth.json contains the Stage 2a truth contract.
- corpus-version.json inventories hashes. A frozen manifest is produced by truth freeze;
  an unverified seed inventory is produced by truth manifest-draft and says frozen: false.
- baseline-<profile>.json is an aggregate benchmark report when a verified stratum exists.
- media/ may contain local source files but is ignored; data/local/media/ is preferred.

dev-1 is an unverified draft corpus, not benchmark truth. Its one-set comparison report is
explicitly marked unverified_seed_comparison: true. controlled-synth-1 is exact generated truth
and supplies the Stage 2b baseline.

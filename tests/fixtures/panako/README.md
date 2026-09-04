# Panako output fixtures (authored, synthetic)

Authored, shape-accurate samples of Panako 2.1 (OLAF strategy) CLI output. They are NOT recordings
of a specific machine's run: reference/query paths, resource ids, offsets, scores and time/frequency
factors are invented to exercise `id_detector.providers.panako.parse_store_output`,
`parse_query_output` and `normalise_matches` deterministically and network-free. No audio is
committed. Resource ids are short synthetic integers; no real URLs, handles, or platform IDs appear.

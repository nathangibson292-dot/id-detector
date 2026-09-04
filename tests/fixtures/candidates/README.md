# Candidate-discovery fixtures (authored, synthetic)

These files are **authored, synthetic** shape-accurate samples of
`yt-dlp --flat-playlist --dump-single-json` output. They are NOT recordings of live service
calls: uploader names, titles and track paths are invented, and per the committed-corpus policy
they carry no real URLs, handles, or platform IDs (track URLs are reduced to scheme-less
placeholders). They exercise `id_detector.candidates.parse_flat_playlist` deterministically and
network-free.

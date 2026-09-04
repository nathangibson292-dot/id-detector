# id-detector

Evidence-first track identification for DJ sets. Stage 4a adds non-blocking text-hint connectors,
strict tracklist/comment parsing, provenance-aware work-tier evidence, and hint-driven rescan
requests to the Stage 3 recognition foundation.

Recognition evidence is immutable per invocation under
`work/<source_key>/<media_key>/recognise/invocations/<invocation_key>/`. The active packaged
Shazam measurement is `shazam-v3.json`; `--refresh` creates a new evidence namespace and never
replaces an earlier raw response.

## Quick start

```powershell
uv sync --dev
uv run id-detector doctor
uv run id-detector calibrate-shazam --track <released-file-or-url> --positions 10,40,70,100,140
uv run id-detector analyse <mix-url> --raw
uv run id-detector analyse <mix-url> --tracklist <tracklist.txt>
uv run id-detector hints <mix-url>
uv run id-detector hints <mix-url> --confirm-mirror <ALLOWLISTED_MIRROR_URL>
uv run id-detector benchmark hints --corpus dev-2 --out data/local/benchmark/dev-2/hints-gate.json
uv run id-detector show <source-key>
uv run id-detector benchmark run --corpus controlled-synth-1 --profile free --out data/corpus/controlled-synth-1/baseline-free.json
uv run id-detector benchmark shortlist --corpus controlled-synth-1 --out data/corpus/controlled-synth-1/shortlist.json
uv run id-detector benchmark render --sources <local-audio-dir> --out <truth-json-dir> `
  --audio-out <local-audio-output-dir> --seed 7
uv run id-detector benchmark score --truth <truth-dir> --episodes <predictions.json> --out <report.json>
uv run id-detector truth seed --help
uv run id-detector truth verify --help
uv run id-detector truth second-pass --help
uv run id-detector truth resolve --help
uv run id-detector truth freeze --help
uv run id-detector truth manifest-draft --help
uv run pytest -q
uv run ruff check .
uv run python scripts/derive_fixtures.py
uv run python scripts/audit_fixtures.py
```

JSON Schemas are checked in under `docs/schemas/`. Regenerate them after a contract change with:

```powershell
uv run python scripts/export_schemas.py
```

Live tests are excluded by default. Run them explicitly with `uv run pytest -q -m live`.

Third-party uploads are denied by default. To evaluate AudD or ACRCloud, copy
`id-detector.example.toml` to the ignored local file `id-detector.toml`, set
`allow_third_party_upload = true`, provide only the environment variables listed in
`.env.example`, and pass `--i-own-this-audio-or-have-permission` to `benchmark shortlist`.

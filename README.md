# id-detector

Evidence-first track identification for DJ sets. Stage 1 provides Windows-safe ingest, full PCM
decode, deterministic generation-0 windows, durable Shazam jobs, measured anchors, caching, and
raw observations.

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
uv run id-detector show <source-key>
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

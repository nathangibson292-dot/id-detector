# id-detector

Evidence-first track identification for DJ sets. Stage 0 establishes the executable data
contracts and validates that the local machine can generate Shazam signatures without a network
request.

## Quick start

```powershell
uv sync --dev
uv run id-detector doctor
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


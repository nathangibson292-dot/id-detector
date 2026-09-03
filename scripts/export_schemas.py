"""Regenerate the checked-in JSON Schemas from the Pydantic contracts."""

from pathlib import Path

from id_detector.contracts import SCHEMA_MODELS, schema_for
from id_detector.io import atomic_write_json


def main() -> None:
    output = Path(__file__).resolve().parents[1] / "docs" / "schemas"
    output.mkdir(parents=True, exist_ok=True)
    for name, model in sorted(SCHEMA_MODELS.items()):
        atomic_write_json(output / f"{name}.schema.json", schema_for(model))
    print(f"wrote {len(SCHEMA_MODELS)} schemas to {output}")


if __name__ == "__main__":
    main()

"""Manual UTF-8 tracklist import."""

from __future__ import annotations

from pathlib import Path

from id_detector.hints.connectors.base import ConnectorError, ConnectorOutput
from id_detector.hints.parse import HintInput
from id_detector.io import native_path

MAX_BYTES = 2 * 1024 * 1024


def load(path: Path) -> ConnectorOutput:
    try:
        with open(native_path(path.resolve()), "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError as exc:
        raise ConnectorError(f"cannot read manual tracklist: {type(exc).__name__}") from exc
    truncated = len(data) > MAX_BYTES
    try:
        text = data[:MAX_BYTES].decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ConnectorError("manual tracklist is not valid UTF-8") from exc
    return ConnectorOutput(
        inputs=(
            HintInput(
                connector="manual_tracklist",
                source_record_id="manual-tracklist",
                text=text,
                author_pseudo_id="manual",
                truncated=truncated,
                structured_tracklist=True,
            ),
        ),
        items_fetched=len([line for line in text.splitlines() if line.strip()]),
        truncated=truncated,
        tracklist_blocks=1,
    )

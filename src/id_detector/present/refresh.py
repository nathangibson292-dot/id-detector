"""Regenerate a stale result page from its saved artefacts.

The result page is a static file written at analyse time, so a page rendered by an older build
keeps the older look until the mix is re-analysed.  Every page carries a
``<meta name="id-detector-page" content="N">`` stamp; the server calls :func:`ensure_fresh_page`
before serving ``present/index.html`` and, when the stamp is older than the current
:data:`~id_detector.present.page.PAGE_VERSION`, re-renders the page from ``ingest/source.json``,
``fuse/…``, ``decode/pcm.json`` and (if present) ``enrich/acquire.json``.

No provider call, no network, no change to any analysis artefact — only ``present/index.html``
(and its completion sidecar) is rewritten, and any failure leaves the existing page untouched.
"""

from __future__ import annotations

import re
from pathlib import Path

from id_detector.contracts import AcquireFile, PcmRecord, SourceRecord
from id_detector.enrich.run import final_identities_path, load_analysis
from id_detector.io import native_path, path_is_file, read_text
from id_detector.present.page import PAGE_VERSION, generate_page
from id_detector.providers.base import AppConfig

_STAMP = re.compile(r'<meta name="id-detector-page" content="(\d+)"')


def page_version(index_html: Path) -> int:
    """The page-version stamp of a written page (``0`` for a page from before stamps existed)."""

    try:
        with open(native_path(index_html), "rb") as handle:
            head = handle.read(4096).decode("utf-8", "replace")
    except OSError:
        return 0
    match = _STAMP.search(head)
    return int(match.group(1)) if match else 0


def _load_config() -> AppConfig:
    """The owner's non-secret preferences (lead-in, collapse), mirroring the ``analyse`` command."""

    path = Path("id-detector.toml")
    try:
        return AppConfig.load(path) if path.is_file() else AppConfig()
    except (ValueError, OSError):
        return AppConfig()


def regenerate_page(media_dir: Path, *, config: AppConfig | None = None) -> Path:
    """Re-render ``present/index.html`` for an analysed mix from its artefacts on disk."""

    app_config = config if config is not None else _load_config()
    source = SourceRecord.model_validate_json(read_text(media_dir / "ingest" / "source.json"))
    episodes, identities = load_analysis(media_dir)
    pcm = PcmRecord.model_validate_json(read_text(media_dir / "decode" / "pcm.json"))
    acquire: AcquireFile | None = None
    acquire_path: Path | None = media_dir / "enrich" / "acquire.json"
    if acquire_path is not None and path_is_file(acquire_path):
        acquire = AcquireFile.model_validate_json(read_text(acquire_path))
    else:
        acquire_path = None
    return generate_page(
        media_dir=media_dir,
        source=source,
        episodes=episodes,
        identities=identities,
        duration_ms=pcm.pcm.duration_ms,
        episodes_path=media_dir / "fuse" / "episodes.json",
        identities_path=final_identities_path(media_dir),
        acquire=acquire,
        acquire_path=acquire_path,
        lead_in_ms=app_config.lead_in_ms,
        collapse=app_config.collapse,
        same_track_bridge_ms=app_config.same_track_bridge_ms,
        min_track_ms=getattr(app_config, "present_min_track_ms", 0),
    )


def ensure_fresh_page(media_dir: Path, *, config: AppConfig | None = None) -> bool:
    """Regenerate the page if its stamp is older than this build; ``True`` when it was rewritten.

    Best-effort by design: a missing artefact or any render error leaves the existing page in
    place (the server then serves what is there) and returns ``False``.
    """

    index_html = media_dir / "present" / "index.html"
    if not path_is_file(index_html) or page_version(index_html) >= PAGE_VERSION:
        return False
    try:
        regenerate_page(media_dir, config=config)
    except Exception:  # noqa: BLE001 - never let a refresh break serving the existing page
        return False
    return True

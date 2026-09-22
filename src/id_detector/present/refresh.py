"""Refresh into a new immutable bundle; legacy files are always left untouched."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from id_detector.io import native_path, path_is_file
from id_detector.jobs import JobStoreLocked, ProcessLock
from id_detector.present.bundles import load_run_snapshot, publish_snapshot, result_dir
from id_detector.present.page import PAGE_VERSION
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

    path = Path("idea.toml")
    try:
        return AppConfig.load(path) if path.is_file() else AppConfig()
    except (ValueError, OSError):
        return AppConfig()


def regenerate_page(media_dir: Path, *, config: AppConfig | None = None) -> Path:
    """Re-render the selected run from one snapshot of its own inputs, into a new bundle."""

    media_dir = Path(native_path(media_dir))
    app_config = config if config is not None else _load_config()
    snapshot = load_run_snapshot(media_dir)
    return publish_snapshot(snapshot, media_dir=media_dir, config=app_config) / "index.html"


def refresh_page(
    media_dir: Path, *, config: AppConfig | None = None
) -> Literal["fresh", "refreshed", "busy", "failed"]:
    """Re-render a stale page UNDER THE MEDIA LOCK — publication is never unlocked.

    ``busy`` means a run (or another ID'er process) holds the lock: nothing was published, and
    the caller must not publish either; that run, or the next upkeep pass, brings the page up.
    """

    media_dir = Path(native_path(media_dir))
    index_html = result_dir(media_dir) / "index.html"
    if not path_is_file(index_html) or page_version(index_html) >= PAGE_VERSION:
        return "fresh"
    lock = ProcessLock(media_dir / ".media.lock")
    try:
        lock.acquire()
    except JobStoreLocked:
        return "busy"
    try:
        index_html = result_dir(media_dir) / "index.html"  # re-read under the lock
        if not path_is_file(index_html) or page_version(index_html) >= PAGE_VERSION:
            return "fresh"
        try:
            regenerated = regenerate_page(media_dir, config=config)
        except Exception:  # noqa: BLE001 - never let a refresh break serving the existing page
            return "failed"
        return "refreshed" if regenerated != index_html else "fresh"
    finally:
        lock.release()


def ensure_fresh_page(media_dir: Path, *, config: AppConfig | None = None) -> bool:
    """Best-effort refresh; a missing input or a busy media leaves the existing result available."""

    return refresh_page(media_dir, config=config) == "refreshed"

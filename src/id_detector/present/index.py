"""Rebuildable local media index; paths are always relative to the work root."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from id_detector.contracts import SourceRecord
from id_detector.io import atomic_write_json, fsync_directory, native_path, read_text
from id_detector.present.bundles import read_bundle_manifest, result_dir, run_metadata

#: Last good map per work root, kept only when ``index.json`` could not be replaced (a read-only or
#: locked work root).  Without it every cached open — including one audio range request — would
#: rebuild and re-hash the whole library again.
_MEMORY: dict[str, dict[str, Any]] = {}


def _tree_stamp(root: Path) -> list[list[Any]]:
    paths = set(root.glob("*/*/ingest/source.json"))
    paths.update(root.glob("*/*/present/current"))
    paths.update(root.glob("*/*/present/index.html"))
    paths.update(root.glob("*/*/present/bundles/*/manifest.json"))
    return [
        [p.relative_to(root).as_posix(), p.stat().st_mtime_ns, p.stat().st_size]
        for p in sorted(paths)
        if p.resolve().is_relative_to(root.resolve())
    ]


def _build_index_document(work_root: Path) -> dict[str, Any]:
    root = Path(native_path(work_root))
    entries = {}
    for source_path in sorted(root.glob("*/*/ingest/source.json")):
        if not source_path.resolve().is_relative_to(root):
            continue
        media_dir = source_path.parents[1]
        directory = result_dir(media_dir)
        if not (directory / "index.html").is_file():
            continue
        manifest = read_bundle_manifest(directory)
        try:
            source = SourceRecord.model_validate_json(
                read_text(directory / "source.json" if manifest else source_path)
            )
        except (OSError, ValueError):
            continue
        if source.media_key != media_dir.name or source.source_key != media_dir.parent.name:
            continue
        metadata = manifest or run_metadata(media_dir)
        alias = {
            "source_key": source.source_key,
            "media_dir": media_dir.relative_to(root).as_posix(),
            "input_url": source.input_url,
            "canonical_url": source.canonical_url,
        }
        entry = entries.get(source.media_key)
        if entry is None:
            # Identical bytes reached under two URLs share a media key but not a directory; every
            # alias is kept, or the second import silently unlinks the first one's URL forever.
            entries[source.media_key] = {
                **alias,
                "newest_complete_run": (
                    metadata["run_id"] if metadata.get("status") == "complete" else None
                ),
                "bundle": directory.name if manifest else None,
                "aliases": [alias],
            }
        else:
            entry["aliases"].append(alias)
    return {"media": entries, "tree_stamp": _tree_stamp(root)}


def rebuild_index(work_root: Path) -> dict[str, Any]:
    root = Path(native_path(work_root))
    document = _build_index_document(root)
    try:
        atomic_write_json(root / "index.json", document)
        fsync_directory(root)
        _MEMORY.pop(str(root), None)
    except OSError:
        # The derived index is optional: keep the rebuilt map in memory so a work root that cannot
        # be written is slow once, not once per lookup.
        _MEMORY[str(root)] = document
    return document


def load_index_read_only(work_root: Path) -> dict[str, Any]:
    """Load or rebuild the media map without ever writing beneath ``work_root``.

    Owner tooling uses this path when ``work/`` is an input rather than application state.  A
    current on-disk index is still preferred, but a missing or stale index is rebuilt in memory.
    """

    root = Path(native_path(work_root))
    stamp = _tree_stamp(root)
    try:
        on_disk = json.loads(read_text(root / "index.json"))
    except (OSError, ValueError):
        on_disk = None
    for document in (on_disk, _MEMORY.get(str(root))):
        if document is not None and _usable(root, document, stamp):
            return document
    return _build_index_document(root)


def media_dir_for_key_read_only(work_root: Path, media_key: str) -> Path | None:
    """Resolve one indexed media key without refreshing or otherwise mutating ``work_root``."""

    root = Path(native_path(work_root))
    entry = load_index_read_only(root)["media"].get(media_key)
    if not isinstance(entry, dict) or not isinstance(entry.get("media_dir"), str):
        return None
    candidate = (root / entry["media_dir"]).resolve()
    if candidate == root or not candidate.is_relative_to(root) or candidate.name != media_key:
        return None
    return candidate


def _usable(root: Path, document: Any, stamp: list[list[Any]]) -> bool:
    """The stamp already covers every source record, pointer, legacy page and bundle manifest, so
    entries are only re-checked structurally: verifying each bundle's hashes again would make every
    cached open (including one audio range request) cost a re-hash of the whole library."""

    try:
        if document["tree_stamp"] != stamp:
            return False
        for key, item in document["media"].items():
            for alias in item.get("aliases") or [item]:
                directory = (root / alias["media_dir"]).resolve()
                if (
                    not directory.is_relative_to(root)
                    or directory.name != key
                    or directory.parent.name != alias["source_key"]
                ):
                    return False
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    return True


def load_index(work_root: Path) -> dict[str, Any]:
    root = Path(native_path(work_root))
    stamp = _tree_stamp(root)
    try:
        on_disk = json.loads(read_text(root / "index.json"))
    except (OSError, ValueError):
        on_disk = None
    for document in (on_disk, _MEMORY.get(str(root))):
        if document is not None and _usable(root, document, stamp):
            return document
    return rebuild_index(root)


def cached_media_dir(work_root: Path, target: str) -> Path | None:
    from id_detector.ingest import canonicalize_url

    root = Path(native_path(work_root))
    canonical = canonicalize_url(target)[0]
    try:
        # A target that is not expressible as a local path (a bracketed-host URL, an unknown
        # ``~user``, an invalid name) is simply not a local file; it must never raise here.
        local_uri = Path(target).expanduser().resolve().as_uri()
    except (OSError, ValueError, RuntimeError):
        local_uri = None
    wanted = {target, canonical, local_uri} - {None}
    document = load_index(root)
    for attempt in range(2):
        for key, entry in document["media"].items():
            if target == key:
                return root / entry["media_dir"]
            for alias in entry.get("aliases") or [entry]:
                if wanted.intersection({alias["input_url"], alias["canonical_url"]}):
                    return root / alias["media_dir"]
        if attempt == 0:
            document = rebuild_index(root)
    return None

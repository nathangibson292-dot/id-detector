"""Immutable local result publication and legacy presentation-path resolution."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from id_detector.contracts import (
    AcquireFile,
    EpisodesFile,
    IdentitiesRecord,
    PcmRecord,
    SourceRecord,
)
from id_detector.io import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    fsync_directory,
    native_path,
    path_is_file,
    read_bytes,
    read_text,
    sha256_file,
)
from id_detector.present import page
from id_detector.present.exports import export_tracklist
from id_detector.providers.base import AppConfig

_PUBLICATION_LOCK = threading.RLock()
_KEY = re.compile(r"[a-f0-9]{64}")
#: Plan §2.3.5: these are *shown* to the requester (with a banner); only ``complete`` is ever
#: *served* to a later request, which is why ``present/current`` never names one of them.
_SHOWN = ("degraded", "partial")
#: A bundle manifest that cannot answer "which run, which revision, which frozen inputs" is damaged
#: however well its files hash; selecting or pointing at one would crash or mislabel the library.
_BUNDLE_FIELDS = ("run_id", "presentation_version", "status", "fuse_run")
_BUNDLE_FILES = ("index.html", "tracklist.json", "source.json")


def bundle_id(run_id: str, presentation_version: int) -> str:
    """SHA-256 of canonical UTF-8 JSON [run_id, integer presentation_version], without newline."""

    return sha256(canonical_json_bytes([run_id, presentation_version])).hexdigest()


def read_manifest(directory: Path) -> dict[str, Any] | None:
    """Only sealed, intact directories are eligible for publication or cached serving."""

    directory = Path(native_path(directory))
    try:
        manifest = json.loads(read_text(directory / "manifest.json"))
        files = manifest["files"]
        if not isinstance(files, dict) or not files:
            return None
        for name, item in files.items():
            path = (directory / name).resolve()
            if not path.is_relative_to(directory.resolve()):
                return None
            if path.stat().st_size != item["size"] or sha256_file(path) != item["sha256"]:
                return None
        return manifest
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def read_bundle_manifest(directory: Path) -> dict[str, Any] | None:
    """``read_manifest`` plus the structure every consumer of a *bundle* manifest indexes.

    A JSON-valid but damaged manifest must be skipped, never crash the caller, so identity
    (``sha256(run_id, presentation_version)`` == the directory name), the run reference and the
    presentation files are all checked here rather than at each use site.
    """

    manifest = read_manifest(directory)
    if manifest is None or any(manifest.get(field) is None for field in _BUNDLE_FIELDS):
        return None
    if not isinstance(manifest["run_id"], str) or not isinstance(manifest["status"], str):
        return None
    if isinstance(manifest["presentation_version"], bool) or not isinstance(
        manifest["presentation_version"], int
    ):
        return None
    if bundle_id(manifest["run_id"], manifest["presentation_version"]) != Path(directory).name:
        return None
    if any(name not in manifest["files"] for name in _BUNDLE_FILES):
        return None
    return manifest


def _frozen_run(media_dir: Path, manifest: dict[str, Any]) -> Path:
    """The bundle's frozen fuse run, verified and proven to stay inside ``fuse/runs/``."""

    fuse_run = (media_dir / manifest["fuse_run"]).resolve()
    if not fuse_run.is_relative_to((media_dir / "fuse" / "runs").resolve()):
        raise ValueError("unsafe fuse run path")
    if read_manifest(fuse_run) is None:
        raise ValueError("missing or damaged frozen run")
    return fuse_run


def _seal(directory: Path, metadata: dict[str, Any]) -> None:
    files = {
        path.relative_to(directory).as_posix(): {
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    }
    atomic_write_json(directory / "manifest.json", {**metadata, "files": files})
    for path in sorted((p for p in directory.rglob("*") if p.is_dir()), reverse=True):
        fsync_directory(path)
    fsync_directory(directory)
    fsync_directory(directory.parent)
    fsync_directory(directory.parent.parent)
    fsync_directory(directory.parent.parent.parent)


def result_dir(media_dir: Path) -> Path:
    """Resolve the local current bundle, with a read-only fallback to pre-bundle results."""

    media_dir = Path(native_path(media_dir))
    present = media_dir / "present"
    try:
        key = read_text(present / "current").strip()
        if _KEY.fullmatch(key):
            directory = present / "bundles" / key
            manifest = read_bundle_manifest(directory)
            if manifest and manifest["status"] == "complete":
                return directory
    except OSError:
        pass
    complete: list[tuple[tuple[str, str, int], Path]] = []
    shown: list[tuple[tuple[str, str, int], Path]] = []
    for directory in (present / "bundles").glob("*"):
        manifest = read_bundle_manifest(directory)
        if not manifest:
            continue
        if manifest["status"] == "complete":
            complete.append((_order(manifest), directory))
        elif manifest["status"] in _SHOWN:
            shown.append((_order(manifest), directory))
    if complete:
        return max(complete)[1]
    if shown:
        # A degraded/partial run is the requester's only result: it must still open from the
        # library, the CLI and the local URL, even though no pointer may name it (plan §2.3.5).
        return max(shown)[1]
    return present


def shown_result_dir(media_dir: Path) -> Path:
    """The initiating local job can also show its partial/degraded result."""

    media_dir = Path(native_path(media_dir))
    try:
        entry = json.loads(read_text(media_dir / "invocations.jsonl").splitlines()[-1])
        key = entry.get("bundle_id") or ""
        if _KEY.fullmatch(key):
            directory = media_dir / "present" / "bundles" / key
            manifest = read_bundle_manifest(directory)
            if manifest:
                current_dir = result_dir(media_dir)
                current = read_bundle_manifest(current_dir)
                if current and current["run_id"] == manifest["run_id"]:
                    return current_dir
                return directory
    except (OSError, ValueError, IndexError):
        pass
    return result_dir(media_dir)


def _order(manifest: dict[str, Any]) -> tuple[str, str, int]:
    return (
        manifest.get("started_at") or "",
        manifest["run_id"],
        manifest["presentation_version"],
    )


def _publish_current(media_dir: Path, directory: Path, metadata: dict[str, Any]) -> None:
    if metadata["status"] != "complete":
        return
    current = read_bundle_manifest(result_dir(media_dir))
    if current is not None and current["status"] != "complete":
        current = None  # a shown-only fallback result never holds back the complete-run pointer
    if current is None or _order(metadata) >= _order(current):
        atomic_write_bytes(media_dir / "present" / "current", directory.name.encode("ascii"))
        fsync_directory(media_dir / "present")


def run_metadata(media_dir: Path) -> dict[str, Any]:
    current = read_bundle_manifest(result_dir(media_dir))
    if current:
        return current
    return _legacy_metadata(media_dir)


def _legacy_metadata(media_dir: Path) -> dict[str, Any]:
    """The run identity of a pre-bundle result, from its journal or its deterministic stand-in."""

    try:
        for line in reversed(read_text(media_dir / "invocations.jsonl").splitlines()):
            entry = json.loads(line)
            if entry.get("status") in {"complete", "degraded", "partial"}:
                return {**entry, "run_id": entry["invocation_id"]}
    except (OSError, ValueError, KeyError):
        pass
    return {"run_id": "legacy-" + media_dir.name, "status": "complete"}


def publish_result(
    *,
    media_dir: Path,
    source: SourceRecord,
    episodes: EpisodesFile,
    identities: IdentitiesRecord,
    duration_ms: int,
    metadata: dict[str, Any],
    config: AppConfig,
    acquire: AcquireFile | None = None,
    local: bool = True,
    presentation_version: int | None = None,
) -> Path:
    """Seal artefacts before publishing any pointer; repeated identities reuse immutable bytes.

    Flat fuse outputs remain the newest-run working copies for existing benchmark/scorer readers.
    A manifest is the seal: an interrupted, unsealed directory is never referenced or overwritten.
    """

    media_dir = Path(native_path(media_dir))
    version = page.PAGE_VERSION if presentation_version is None else presentation_version
    version = max(version, metadata.get("presentation_version", 0))
    run_id = metadata["run_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("unsafe run_id")
    # PAGE_VERSION stamps the HTML build; presentation_version also advances for changed inputs
    # (preferences/acquisition). sha256(run_id, presentation_version) remains the sole identity.
    render_key = sha256(
        canonical_json_bytes(
            {
                "page_version": page.PAGE_VERSION,
                "source": source,
                "episodes": episodes,
                "identities": identities,
                "duration_ms": duration_ms,
                "acquire": acquire,
                "lead_in_ms": config.lead_in_ms,
                "collapse": config.collapse,
                "same_track_bridge_ms": config.same_track_bridge_ms,
                "min_track_ms": config.present_min_track_ms,
                "status": metadata["status"],
                "reason": metadata.get("reason"),
                "achieved": metadata.get("achieved"),
            }
        )
    ).hexdigest()
    with _PUBLICATION_LOCK:
        bundles = media_dir / "present" / "bundles"
        # Reuse only identical presentation inputs; a changed render gets the next free revision.
        # An unsealed or damaged directory is never a bundle and is never rewritten either: the
        # revision advances past it, so one interrupted publication cannot brick this run forever.
        while True:
            directory = bundles / bundle_id(run_id, version)
            manifest = read_bundle_manifest(directory)
            if manifest is not None and manifest.get("render_key") == render_key:
                break
            if manifest is None and not directory.exists():
                break
            version += 1
        if manifest is None:
            fuse_run = media_dir / "fuse" / "runs" / run_id
            if read_manifest(fuse_run) is None:
                if path_is_file(fuse_run / "manifest.json"):
                    # It was sealed once, so a published bundle may already name it: its frozen
                    # inputs must never be re-derived from today's mutable fuse tree.
                    raise ValueError("damaged frozen run; refusing to reseal")
                # No manifest at all means it was never sealed and nothing can reference it, so an
                # interrupted freeze of this same run is finished in place rather than refused.
                fuse_run.mkdir(parents=True, exist_ok=True)
                for path in sorted((media_dir / "fuse").rglob("*")):
                    relative = path.relative_to(media_dir / "fuse")
                    if relative.parts[0] != "runs" and path.is_file():
                        atomic_write_bytes(fuse_run / relative, read_bytes(path))
                if not (fuse_run / "episodes.json").is_file():
                    atomic_write_json(fuse_run / "episodes.json", episodes)
                atomic_write_json(fuse_run / "presentation-identities.json", identities)
                _seal(fuse_run, {"run_id": run_id})
            directory.mkdir(parents=True, exist_ok=False)
            atomic_write_json(directory / "source.json", source)
            acquire_path = None
            if acquire is not None:
                acquire_path = directory / "acquire.json"
                atomic_write_json(acquire_path, acquire)
            common = dict(
                media_dir=media_dir,
                output_dir=directory,
                episodes=episodes,
                identities=identities,
                duration_ms=duration_ms,
                episodes_path=fuse_run / "episodes.json",
                identities_path=fuse_run / "presentation-identities.json",
                acquire=acquire,
                acquire_path=acquire_path,
                collapse=config.collapse,
                same_track_bridge_ms=config.same_track_bridge_ms,
                min_track_ms=config.present_min_track_ms,
            )
            export_tracklist(
                **common,
                media_key=source.media_key,
                title=source.title,
                media_target=source.canonical_url,
                status=metadata["status"],
                reason=metadata.get("reason"),
                achieved=metadata.get("achieved"),
            )
            if metadata.get("analysis_key") is not None:
                tracklist_path = directory / "tracklist.json"
                tracklist = json.loads(read_text(tracklist_path))
                tracklist["analysis_key"] = metadata["analysis_key"]
                atomic_write_json(tracklist_path, tracklist)
            page.generate_page(**common, source=source, lead_in_ms=config.lead_in_ms)
            manifest = {
                "run_id": run_id,
                "analysis_key": metadata.get("analysis_key"),
                "compatibility": metadata.get("compatibility"),
                "requested_recipe_id": metadata.get("requested_recipe_id"),
                "achieved": metadata.get("achieved"),
                "status": metadata["status"],
                "reason": metadata.get("reason"),
                "presentation_version": version,
                "render_key": render_key,
                "pricing_version": metadata.get("pricing_version"),
                "usd_e2_spent": metadata.get("usd_e2_spent", 0),
                "started_at": metadata.get("started_at", ""),
                "media_key": source.media_key,
                "source_key": source.source_key,
                "duration_ms": duration_ms,
                "fuse_run": fuse_run.relative_to(media_dir).as_posix(),
            }
            _seal(directory, manifest)
        else:
            # Reuse: the pointer is about to name this bundle, so prove its frozen run is intact
            # first — the publication invariant covers every artefact a reference reaches.
            _frozen_run(media_dir, manifest)
        if local:
            _publish_current(media_dir, directory, manifest)
            from id_detector.present.index import rebuild_index

            rebuild_index(media_dir.parents[1])
    return directory


@dataclass(frozen=True)
class RunSnapshot:
    """One run's presentation inputs, resolved together so a render can never mix two runs."""

    directory: Path
    manifest: dict[str, Any] | None
    metadata: dict[str, Any]
    source: SourceRecord
    episodes: EpisodesFile
    identities: IdentitiesRecord
    duration_ms: int
    acquire: AcquireFile | None


def load_run_snapshot(media_dir: Path, *, directory: Path | None = None) -> RunSnapshot:
    """The selected result *and* the exact inputs that produced it, resolved under one lock.

    Resolving the run and its artefacts separately is how an older ``complete`` run's identity ends
    up stamped on a newer ``partial`` run's rows: the mutable ``fuse/`` tree always belongs to the
    newest run, while ``result_dir`` deliberately prefers the newest *complete* one.  Every caller
    that re-publishes an existing run (refresh, acquisition) takes both from here instead.

    ``directory`` names one specific bundle: the compatibility lookup (§3.4) may deliberately select
    an older run than ``present/current`` names, and re-publishing it must not silently swap in
    whatever the newest complete run happens to be.
    """

    from id_detector.enrich.run import load_analysis

    media_dir = Path(native_path(media_dir))
    with _PUBLICATION_LOCK:
        selected = directory is not None
        directory = result_dir(media_dir) if directory is None else Path(native_path(directory))
        manifest = read_bundle_manifest(directory)
        if selected and manifest is None:
            raise ValueError("selected bundle is missing or damaged")
        if manifest is not None:
            source = SourceRecord.model_validate_json(read_text(directory / "source.json"))
            fuse_run = _frozen_run(media_dir, manifest)
            episodes = EpisodesFile.model_validate_json(read_text(fuse_run / "episodes.json"))
            identities = IdentitiesRecord.model_validate_json(
                read_text(fuse_run / "presentation-identities.json")
            )
            duration_ms = manifest["duration_ms"]
            metadata: dict[str, Any] = manifest
            acquire_path = directory / "acquire.json"
        else:
            source = SourceRecord.model_validate_json(read_text(media_dir / "ingest/source.json"))
            episodes, identities = load_analysis(media_dir)
            duration_ms = PcmRecord.model_validate_json(
                read_text(media_dir / "decode/pcm.json")
            ).pcm.duration_ms
            metadata = _legacy_metadata(media_dir)
            acquire_path = media_dir / "enrich/acquire.json"
        acquire = (
            AcquireFile.model_validate_json(read_text(acquire_path))
            if path_is_file(acquire_path)
            else None
        )
        return RunSnapshot(
            directory=directory,
            manifest=manifest,
            metadata=metadata,
            source=source,
            episodes=episodes,
            identities=identities,
            duration_ms=duration_ms,
            acquire=acquire,
        )


def publish_snapshot(
    snapshot: RunSnapshot,
    *,
    media_dir: Path,
    config: AppConfig,
    acquire: AcquireFile | None = None,
) -> Path:
    """Publish a revision of the snapshot's own run; ``acquire`` replaces its acquisition data."""

    return publish_result(
        media_dir=media_dir,
        source=snapshot.source,
        episodes=snapshot.episodes,
        identities=snapshot.identities,
        duration_ms=snapshot.duration_ms,
        metadata=snapshot.metadata,
        config=config,
        acquire=snapshot.acquire if acquire is None else acquire,
    )

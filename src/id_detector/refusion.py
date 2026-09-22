"""Re-fuse a stored result under the current fusion version — offline, from its recorded evidence.

A fusion change (``fusion:N`` in a recipe's ``algorithm_version``) makes every stored result stale:
its tracklist was decided by the old rules.  Recognition is not stale — the observations a run
recorded are what the engines said about that audio, whatever fusion later makes of them — so a
stale result is brought up to date by running TODAY's fusion over the run's OWN recorded
observations, windows and hints, and publishing the outcome as a new immutable bundle.

What this never does: contact a provider, fetch anything, run a hint connector, read or write a
recognition cache, reserve or spend money, or touch the mutable ``fuse/`` working tree, the legacy
``present/`` files or the bundle it supersedes.  The old bundle and its frozen run stay exactly as
they were; the new bundle simply sorts after it (``run_id`` ``refuse<version>-…``), so
``present/current`` and every "newest complete result" lookup move on and nothing is orphaned or
rewritten.

What it REFUSES to do, each with a plain reason (:class:`NotRebuildable`):

* anything whose recorded evidence cannot be PROVEN: every observation file and the hints file
  must be present and equal to the well-formed SHA-256 the stored fusion recorded for it.  A window
  file may be absent — retention collects ``windows/`` at once, and the answered observations
  carry the same supports — but a window file that is present must match as well;
* a paid (Deep) result.  Deep's second-opinion windows were CHOSEN by the old fusion's first pass,
  so its evidence is not what today's fusion would have gathered; calling a re-fusion of it
  "current" would be a claim nobody checked.  It is left exactly as it is;
* a result scored by a calibration model the caller does not hold.

Two callers, neither of them a web request: the server's background upkeep after start-up (every
stored result, the owner's pre-bundle mixes included) and ``run_analysis`` when the one thing
keeping a stored result from answering a request is its fusion version.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from id_detector.contracts import (
    EpisodesFile,
    HintRecord,
    IdentitiesRecord,
    ObservationRecord,
    PcmRecord,
    WindowRecord,
)
from id_detector.fuse.episodes import (
    CORROBORATION_OVERLAP_MIN_MS,
    CORROBORATION_SEPARATION_MIN_MS,
    build_episodes,
)
from id_detector.fuse.identity import build_identity_graph
from id_detector.io import native_path, path_is_file, read_bytes, read_text
from id_detector.jobs import JobStoreLocked, ProcessLock
from id_detector.orchestrate import scanned_windows
from id_detector.present.bundles import (
    RunSnapshot,
    freeze_refused_run,
    load_run_snapshot,
    publish_result,
    read_manifest,
)
from id_detector.providers.base import AppConfig
from id_detector.recipes import FUSION_VERSION, RECIPES, fusion_component

_GENERATION = re.compile(r"fuse/episodes\.gen(\d+)\.json")
_SHA256 = re.compile(r"[a-f0-9]{64}")


class NotRebuildable(Exception):
    """This stored result cannot be re-fused faithfully; ``str(exc)`` says why, in plain words."""


@dataclass(frozen=True)
class Refusion:
    """What happened to one stored result.

    ``current`` — nothing to do; ``refused`` — ``bundle`` is the newly published result;
    ``busy`` — a run holds the media lock, try again later (NOTHING may be published meanwhile);
    ``unrebuildable`` — ``why`` says what could not be proven; the result stays as it is.
    """

    state: Literal["current", "refused", "busy", "unrebuildable"]
    bundle: Path | None = None
    why: str | None = None
    may_refresh_page: bool = True
    recipe: Literal["free", "deep", "unknown"] = "unknown"


@dataclass(frozen=True)
class _Scanned:
    """What fusion reads of a window — its support — when the window records were collected."""

    support_ms: tuple[int, int]


@dataclass(frozen=True)
class FusionInputs:
    generation: int
    duration_ms: int
    observations: tuple[ObservationRecord, ...]
    windows: tuple[Any, ...]
    hints: tuple[HintRecord, ...]
    files: dict[str, str]  # media-relative path -> the proven sha256 of the bytes that were fused


def stored_fusion_version(stored: dict[str, Any] | None) -> int:
    """The fusion version a stored result was decided under; ``0`` when it carries no stamp (a
    pre-bundle result, or a bundle re-published from one)."""

    if stored is None:
        return 0
    refusion = stored.get("refusion")
    if isinstance(refusion, dict) and isinstance(refusion.get("fusion_version"), int):
        return int(refusion["fusion_version"])
    compatibility = stored.get("compatibility")
    version = compatibility.get("algorithm_version") if isinstance(compatibility, dict) else None
    return fusion_component(version if isinstance(version, str) else None) or 0


def is_deep(manifest: dict[str, Any] | None, metadata: dict[str, Any] | None = None) -> bool:
    """Whether the stored result's own provenance says it was paid.

    Legacy results have no manifest.  Their validated invocation metadata is therefore required;
    absence of a bundle must never be interpreted as evidence that a result was Free.
    """

    return "deep" in _stored_recipe_names(manifest, metadata)


def _stored_recipe_names(
    manifest: dict[str, Any] | None, metadata: dict[str, Any] | None
) -> set[str]:
    names: set[str] = set()
    for stored in (manifest, metadata):
        if not stored:
            continue
        compatibility = stored.get("compatibility") or {}
        for name in (stored.get("achieved"), compatibility.get("recipe_name")):
            if name in {"free", "deep"}:
                names.add(str(name))
    return names


def stored_recipe(
    manifest: dict[str, Any] | None, metadata: dict[str, Any] | None = None
) -> Literal["free", "deep", "unknown"]:
    """The recipe the stored result proves, with conflicts and omissions left unknown."""

    names = _stored_recipe_names(manifest, metadata)
    if len(names) != 1:
        return "unknown"
    return "deep" if "deep" in names else "free"


def _legacy_metadata_problem(metadata: dict[str, Any] | None) -> str | None:
    from id_detector.present.bundles import LEGACY_METADATA_ERROR

    problem = (metadata or {}).get(LEGACY_METADATA_ERROR)
    return str(problem) if problem else None


def refused_run_id(source_run_id: str) -> str:
    """Deterministic, so an interrupted re-fusion resumes into the same frozen run; and ordered
    (``refuseNNNN-…`` after any hex or ``legacy-`` id, ordered by fusion version), because run id is
    the tie-break between two results of one analysis time."""

    digest = sha256(f"{source_run_id}|fusion:{FUSION_VERSION}".encode()).hexdigest()[:32]
    return f"refuse{FUSION_VERSION:04d}-{digest}"


def _source_fuse_dir(snapshot: RunSnapshot, media_dir: Path) -> Path:
    """The original run whose proved inputs a chain of offline re-fusions still carries.

    A re-fused run intentionally freezes only its new fusion output; its ``source_bundle`` points
    back to the immutable run with the completion sidecars.  A legacy source has no bundle, so its
    byte-preserved flat ``fuse/`` tree is the source.  Following the chain lets a later fusion bump
    remain offline without pretending the smaller re-fused run contains inputs it does not.
    """

    from id_detector.present.bundles import read_bundle_manifest

    manifest = snapshot.manifest
    seen: set[str] = set()
    while manifest is not None and isinstance(manifest.get("refusion"), dict):
        source_bundle = manifest["refusion"].get("source_bundle")
        if source_bundle is None:
            return media_dir / "fuse"
        if not isinstance(source_bundle, str) or not _SHA256.fullmatch(source_bundle):
            raise NotRebuildable("the stored re-fusion has an invalid source bundle reference")
        if source_bundle in seen:
            raise NotRebuildable("the stored re-fusion source bundle chain contains a cycle")
        seen.add(source_bundle)
        manifest = read_bundle_manifest(media_dir / "present" / "bundles" / source_bundle)
        if manifest is None:
            raise NotRebuildable("the stored re-fusion's source bundle is missing or damaged")
    if manifest is None:
        return media_dir / "fuse"
    fuse_run = manifest.get("fuse_run")
    if not isinstance(fuse_run, str):
        raise NotRebuildable("the stored source bundle does not name its frozen fusion run")
    directory = (media_dir / fuse_run).resolve()
    allowed = (media_dir / "fuse" / "runs").resolve()
    if not directory.is_relative_to(allowed) or directory.parent != allowed:
        raise NotRebuildable("the stored source bundle names an unsafe fusion run")
    return directory


def _records(payload: bytes, model: Any) -> tuple[Any, ...]:
    return tuple(
        model.model_validate_json(line)
        for line in payload.decode("utf-8").splitlines()
        if line.strip()
    )


def _sidecar(path: Path) -> tuple[str, dict[str, Any]]:
    payload = json.loads(read_text(path))
    digest = payload["sha256"]
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError("completion sidecar without a valid artifact checksum")
    upstream = payload["upstream"]
    if not isinstance(upstream, dict):
        raise ValueError("completion sidecar without an upstream map")
    return digest, upstream


def _recorded_digest(recorded: object, *, prunable: bool) -> str | None:
    """The well-formed digest a sidecar recorded, or ``None``.  Retention rewrites the entry of a
    file it collected as ``{"pruned_upstream": <digest>}``; only a window file may be one."""

    if prunable and isinstance(recorded, dict) and set(recorded) == {"pruned_upstream"}:
        recorded = recorded["pruned_upstream"]
    if isinstance(recorded, str) and _SHA256.fullmatch(recorded):
        return recorded
    return None


def load_fusion_inputs(media_dir: Path, fuse_dir: Path) -> FusionInputs:
    """The evidence the stored fusion in ``fuse_dir`` names, PROVEN and read back.

    Each file is read once; the digest is taken of, and the records are parsed from, those same
    bytes.  Raises :class:`NotRebuildable` for anything that cannot be proven.
    """

    try:
        final_digest, final = _sidecar(fuse_dir / "episodes.done.json")
        references = [
            (int(match.group(1)), key) for key in final if (match := _GENERATION.fullmatch(key))
        ]
        if len(references) != 1 or len(final) != 1:
            raise ValueError("final episode sidecar must name exactly one generation")
        generation, generation_key = references[0]
        generation_digest, upstream = _sidecar(fuse_dir / f"episodes.gen{generation}.done.json")
        recorded_generation_digest = _recorded_digest(final[generation_key], prunable=False)
        if generation_digest != recorded_generation_digest:
            raise NotRebuildable(
                "its stored records disagree with each other: the final generation reference "
                "disagrees with its generation sidecar"
            )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise NotRebuildable(
            "the stored result does not record which recognition files it was built from"
        ) from exc
    root = media_dir.resolve()
    files: dict[str, str] = {}

    def _path(key: str) -> Path:
        relative = Path(key)
        if relative.parts and relative.parts[0] == "fuse":
            path = (fuse_dir / Path(*relative.parts[1:])).resolve()
            allowed = fuse_dir.resolve()
        else:
            path = (media_dir / relative).resolve()
            allowed = root
        if not path.is_relative_to(allowed):
            raise NotRebuildable(f"{key} is outside this mix's folder")
        return path

    def _proven(
        key: str, *, recorded: dict[str, Any] = upstream, prunable: bool = False
    ) -> bytes | None:
        expected = _recorded_digest(recorded.get(key), prunable=prunable)
        if expected is None:
            raise NotRebuildable(f"no valid recorded checksum for {key}")
        path = _path(key)
        if not path_is_file(path):
            if prunable:
                return None  # collected by retention: acceptable, and rebuilt by the caller
            raise NotRebuildable(f"recorded recognition file is missing: {key}")
        try:
            payload = read_bytes(path)
        except OSError as exc:
            raise NotRebuildable(f"{key} could not be read") from exc
        if sha256(payload).hexdigest() != expected:
            raise NotRebuildable(f"{key} has changed since the result was made")
        files[key] = expected
        return payload

    try:
        # Prove the final stored episode file and the exact generation it selects before looking at
        # any recognition evidence.  The generation reference is itself an upstream checksum, not
        # merely a filename from which we infer the highest number.
        final_payload = read_bytes(fuse_dir / "episodes.json")
        if sha256(final_payload).hexdigest() != final_digest:
            raise NotRebuildable("the stored final episode file has changed since it was made")
        files["fuse/episodes.json"] = final_digest
        stored_episodes = EpisodesFile.model_validate_json(final_payload)
        generation_payload = _proven(generation_key, recorded=final)
        generation_episodes = EpisodesFile.model_validate_json(generation_payload or b"")
        if stored_episodes.generation != generation or generation_episodes.generation != generation:
            raise NotRebuildable("the stored episode generation does not match its final reference")
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise NotRebuildable("the stored final episode provenance could not be proven") from exc

    # Every recorded generation input is part of the provenance chain.  Windows alone may have
    # been explicitly pruned by retention; all other inputs, including identities and PCM, must
    # still exist byte-for-byte.
    proven = {key: _proven(key, prunable=key.startswith("windows/")) for key in sorted(upstream)}

    observation_keys = sorted(key for key in upstream if key.startswith("recognise/"))
    if not observation_keys:
        raise NotRebuildable("the stored result names no recognition files")
    window_keys = sorted(key for key in upstream if key.startswith("windows/"))
    identity_key = f"fuse/identities.gen{generation}.json"
    missing_categories = []
    if not window_keys:
        missing_categories.append("window inputs")
    if "decode/pcm.json" not in upstream:
        missing_categories.append("the PCM record")
    if identity_key not in upstream:
        missing_categories.append("the generation identity record")
    if missing_categories:
        raise NotRebuildable("the generation sidecar omits " + ", ".join(missing_categories))
    try:
        observations: list[ObservationRecord] = []
        for key in observation_keys:
            observations.extend(_records(proven[key] or b"", ObservationRecord))
        windows: list[WindowRecord] = []
        windows_complete = True
        for key in window_keys:
            payload = proven[key]
            if payload is None:
                windows_complete = False
                continue
            windows.extend(_records(payload, WindowRecord))
        hints: tuple[HintRecord, ...] = ()
        if "hints/hints.jsonl" in upstream:
            hints = _records(proven["hints/hints.jsonl"] or b"", HintRecord)
        pcm = PcmRecord.model_validate_json(proven["decode/pcm.json"] or b"")
    except (ValueError, UnicodeDecodeError) as exc:
        raise NotRebuildable("a recorded recognition file could not be parsed") from exc
    scanned: tuple[Any, ...]
    if windows_complete and windows:
        # Exactly the pipeline's rule: only a window some engine answered for counts as scanned.
        scanned = tuple(scanned_windows(windows, observations))
    else:
        scanned = tuple(
            _Scanned(support)
            for support in sorted(
                {tuple(item.support_ms) for item in observations if item.status != "error"}
            )
        )
    return FusionInputs(generation, pcm.pcm.duration_ms, tuple(observations), scanned, hints, files)


def _thresholds(
    manifest: dict[str, Any] | None, metadata: dict[str, Any] | None = None
) -> tuple[int, int]:
    compatibility = (manifest or metadata or {}).get("compatibility") or {}
    recipe = RECIPES.get(compatibility.get("recipe_name"))
    overlap = recipe.overlap_min_ms if recipe is not None else None
    separation = recipe.separation_min_ms if recipe is not None else None
    return (
        CORROBORATION_OVERLAP_MIN_MS if overlap is None else overlap,
        CORROBORATION_SEPARATION_MIN_MS if separation is None else separation,
    )


def _restamped(
    manifest: dict[str, Any] | None, metadata: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """The stored compatibility stamp with today's algorithm version.

    Where the result is its own recipe's complete result the recipe id inside the analysis key is
    moved on as well, so the re-fused bundle answers tomorrow's identical request outright; any
    other result keeps its key and is simply brought up to date again, at no cost, when asked for.
    """

    from id_detector.compat import AnalysisInputs, digest
    from id_detector.recipes import get_recipe

    stored_metadata = manifest or metadata or {}
    compatibility = stored_metadata.get("compatibility")
    if not isinstance(compatibility, dict):
        return None
    recipe = RECIPES.get(compatibility.get("recipe_name"))
    if recipe is None:
        return None
    stamp = {**compatibility, "algorithm_version": recipe.algorithm_version}
    inputs = compatibility.get("analysis_inputs")
    own_complete = (
        stored_metadata.get("status") == "complete"
        and stored_metadata.get("achieved") == recipe.name
        and compatibility.get("analysis_key") == stored_metadata.get("analysis_key")
    )
    if own_complete and isinstance(inputs, dict):
        try:
            density = int(compatibility.get("primary_density") or 1)
            current = get_recipe(recipe.name, primary_density=density)
            moved = AnalysisInputs(**{**inputs, "recipe_id": current.recipe_id})
        except (TypeError, ValueError):
            return stamp
        if digest(inputs) == compatibility.get("analysis_key"):  # a supplied key is left alone
            stamp.update(analysis_inputs=vars(moved), analysis_key=moved.analysis_key)
    return stamp


def refuse_snapshot(
    snapshot: RunSnapshot,
    *,
    media_dir: Path,
    config: AppConfig,
    calibrator: object | None = None,
    compatibility: dict[str, Any] | None = None,
    local: bool = True,
) -> Path:
    """Re-fuse ONE stored result and publish it; raises :class:`NotRebuildable` otherwise.

    The caller holds the media lock.  ``compatibility`` is the requesting run's own stamp (the
    pipeline knows the request it is answering); the upkeep pass has none and the stored stamp is
    carried forward instead.
    """

    media_dir = Path(native_path(media_dir))
    manifest = snapshot.manifest
    if is_deep(manifest, snapshot.metadata):
        raise NotRebuildable(
            "it is a paid (Deep) result, and the windows its second opinion checked were chosen "
            "by the older fusion rules, so it cannot simply be rebuilt"
        )
    source_run_id = str(snapshot.metadata["run_id"])
    fuse_dir = _source_fuse_dir(snapshot, media_dir)
    run_id = refused_run_id(source_run_id)
    frozen = media_dir / "fuse" / "runs" / run_id
    if read_manifest(frozen) is not None:
        # An earlier pass froze this very re-fusion: every bundle of it must show those bytes.
        episodes = EpisodesFile.model_validate_json(read_text(frozen / "episodes.json"))
        identities = IdentitiesRecord.model_validate_json(
            read_text(frozen / "presentation-identities.json")
        )
    else:
        if calibrator is None and any(
            item.score_kind == "calibrated" for item in snapshot.episodes.episodes
        ):
            raise NotRebuildable("its scores came from a calibration profile that is not loaded")
        inputs = load_fusion_inputs(media_dir, fuse_dir)
        if inputs.generation != snapshot.episodes.generation:
            raise NotRebuildable(
                "the selected generation does not equal the stored episode generation"
            )
        if inputs.duration_ms != snapshot.duration_ms:
            raise NotRebuildable("the recorded PCM duration does not equal the stored result")
        overlap_min_ms, separation_min_ms = _thresholds(manifest, snapshot.metadata)
        identity = build_identity_graph(
            snapshot.source.media_key, inputs.observations, hints=inputs.hints
        )
        episodes, _requests = build_episodes(
            media_key=snapshot.source.media_key,
            duration_ms=snapshot.duration_ms,
            observations=inputs.observations,
            windows=inputs.windows,
            identity=identity,
            hints=inputs.hints,
            generation=inputs.generation,
            config=config,
            calibrator=calibrator,
            overlap_min_ms=overlap_min_ms,
            separation_min_ms=separation_min_ms,
        )
        identities = identity.record
        carried = {}
        free_evidence = fuse_dir / "shazam-observations.json"
        if path_is_file(free_evidence):
            # What lets a later Deep request reuse this Free result's sweep, not re-run it.
            carried["shazam-observations.json"] = read_bytes(free_evidence)
        freeze_refused_run(
            media_dir,
            run_id,
            episodes=episodes,
            identities=identities,
            provenance={
                "source_run_id": source_run_id,
                "source_bundle": snapshot.directory.name if manifest is not None else None,
                "source_fusion_version": stored_fusion_version(manifest or snapshot.metadata),
                "fusion_version": FUSION_VERSION,
                "inputs": inputs.files,
            },
            carried=carried,
        )
    stamp = compatibility if compatibility is not None else _restamped(manifest, snapshot.metadata)
    metadata = {
        **snapshot.metadata,
        "run_id": run_id,
        "compatibility": stamp,
        "analysis_key": (stamp or {}).get("analysis_key", snapshot.metadata.get("analysis_key")),
        "refusion": {
            "source_run_id": source_run_id,
            "source_bundle": snapshot.directory.name if manifest is not None else None,
            "fusion_version": FUSION_VERSION,
        },
    }
    metadata.pop("presentation_version", None)  # a new run starts at today's page version
    metadata.pop("bundle_id", None)
    return publish_result(
        media_dir=media_dir,
        source=snapshot.source,
        episodes=episodes,
        identities=identities,
        duration_ms=snapshot.duration_ms,
        metadata=metadata,
        config=config,
        acquire=snapshot.acquire,
        local=local,
    )


def fusion_is_stale(media_dir: Path) -> bool:
    """Whether the CURRENT result of ``media_dir`` was decided by an older fusion version (cheap:
    one manifest read, nothing published)."""

    from id_detector.present.bundles import read_bundle_manifest, result_dir

    return stored_fusion_version(read_bundle_manifest(result_dir(media_dir))) < FUSION_VERSION


def refuse_stale_result(media_dir: Path, *, config: AppConfig | None = None) -> Refusion:
    """Upkeep half: bring the CURRENT result of ``media_dir`` up to today's fusion version."""

    from id_detector.present.refresh import _load_config

    media_dir = Path(native_path(media_dir))
    lock = ProcessLock(media_dir / ".media.lock")
    try:
        lock.acquire()
    except JobStoreLocked:
        return Refusion("busy")
    try:
        from id_detector.present.bundles import (
            legacy_result_metadata,
            read_bundle_manifest,
            result_dir,
        )

        selected = result_dir(media_dir)
        manifest = read_bundle_manifest(selected)
        legacy_layout = manifest is None or str(manifest.get("run_id") or "").startswith("legacy-")
        legacy_metadata = legacy_result_metadata(media_dir) if legacy_layout else None
        stored = manifest or legacy_metadata
        if stored_fusion_version(stored) >= FUSION_VERSION:
            return Refusion("current")
        metadata_problem = _legacy_metadata_problem(legacy_metadata)
        recipe = stored_recipe(manifest, legacy_metadata)
        if metadata_problem is not None:
            return Refusion(
                "unrebuildable",
                why=(
                    f"{metadata_problem}; without trustworthy legacy metadata it is not safe to "
                    "assume the stored result was Free"
                ),
                may_refresh_page=False,
                recipe="unknown",
            )
        if is_deep(manifest, legacy_metadata):
            return Refusion(
                "unrebuildable",
                why=(
                    "it is a paid (Deep) result, and the windows its second opinion checked were "
                    "chosen by the older fusion rules, so it cannot simply be rebuilt"
                ),
                may_refresh_page=False,
                recipe=recipe,
            )
        snapshot = load_run_snapshot(media_dir)
        if stored_fusion_version(snapshot.manifest or snapshot.metadata) >= FUSION_VERSION:
            return Refusion("current")
        if snapshot.metadata.get("status", "complete") not in {"complete", "degraded", "partial"}:
            return Refusion("unrebuildable", why="it is not a finished result", recipe=recipe)
        try:
            bundle = refuse_snapshot(
                snapshot,
                media_dir=media_dir,
                config=config if config is not None else _load_config(),
            )
        except NotRebuildable as exc:
            return Refusion("unrebuildable", why=str(exc), recipe=recipe)
        return Refusion("refused", bundle=bundle)
    finally:
        lock.release()

"""Analysis identities and the versioned, local/hosted result-serving contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from id_detector.io import native_path, read_text, sha256_file
from id_detector.pricing import load_pricing
from id_detector.recipes import RECIPES, Recipe, fusion_component, without_fusion

#: Launch-controlled, so it comes from the single pricing authority (plan §3.3) and never from the
#: owner's config: bumping it in ``pricing.toml`` retires every stored result at once.
COMPAT_VERSION = load_pricing().compat_version
LOCAL_OWNER_SCOPE = "user:local"


def digest(value: object) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class AnalysisInputs:
    media_key: str
    recipe_id: str
    source_kind: Literal["platform", "upload", "local"]
    tenant_scope: str
    hints_snapshot_id: str
    manual_tracklist_sha256: str = ""
    panako_index_id: str = ""

    def __post_init__(self) -> None:
        if self.source_kind not in {"platform", "upload", "local"}:
            raise ValueError("invalid source kind")
        if self.tenant_scope != "public" and not self.tenant_scope.startswith("user:"):
            raise ValueError("invalid tenant scope")
        if (
            self.source_kind == "upload" or self.manual_tracklist_sha256 or self.panako_index_id
        ) and self.tenant_scope == "public":
            raise ValueError("private inputs require an owner scope")

    @property
    def analysis_key(self) -> str:
        return digest(vars(self))

    @property
    def non_recipe_key(self) -> str:
        return digest({key: value for key, value in vars(self).items() if key != "recipe_id"})


@dataclass(frozen=True)
class RunRequest:
    inputs: AnalysisInputs
    recipe: Recipe
    accept_degraded: bool = False
    local: bool = True

    def metadata(self, achieved: Recipe | None = None) -> dict[str, Any]:
        """The identity and version evidence stamped on this run's result.

        ``achieved`` is the recipe that actually produced the result (a degrade substitutes one):
        a result is stamped with the engines and algorithms it really ran, never with a
        recipe's components it never dispatched, so a bump confined to one recipe cannot
        invalidate the other recipe's stored results.
        """

        recipe = achieved or self.recipe
        return {
            "compat_version": COMPAT_VERSION,
            "analysis_key": self.inputs.analysis_key,
            "analysis_inputs": vars(self.inputs),
            "non_recipe_key": self.inputs.non_recipe_key,
            "tenant_scope": self.inputs.tenant_scope,
            "algorithm_version": recipe.algorithm_version,
            "adapter_versions": dict(recipe.adapter_versions),
            "recipe_name": recipe.name,
            "primary_density": recipe.primary_density,
        }


def versions_current(stored: dict[str, Any]) -> bool:
    """Plan §3.4 "equal ``algorithm_version`` and equal ``adapter_versions``", per stored result.

    The comparison is against the *stored* result's own recipe as this build ships it, not against
    the requester's recipe: a Free result is evidence produced by the Free recipe whether a Free or
    a Deep request reads it, so a Deep-only bump (``targeting``, the AudD adapter) must not retire
    Free results, and any bump to a recipe retires that recipe's results for every request.
    """

    recipe = RECIPES.get(stored.get("recipe_name"))  # type: ignore[arg-type]
    return (
        recipe is not None
        and stored.get("algorithm_version") == recipe.algorithm_version
        and stored.get("adapter_versions") == dict(recipe.adapter_versions)
    )


def fusion_stale_only(stored: dict[str, Any]) -> bool:
    """Whether the ONE thing out of date about a stored result is its fusion version.

    Same recipe, same adapters, the same non-fusion algorithm components (``targeting:1``) and an
    OLDER ``fusion:N``: the recognition evidence is exactly what this build would gather, only the
    rules that read it have moved.  Such a result is stale for serving — :func:`versions_current`
    still says no — but fully reusable: :mod:`id_detector.refusion` re-fuses it offline.
    """

    recipe = RECIPES.get(stored.get("recipe_name"))  # type: ignore[arg-type]
    if recipe is None or stored.get("adapter_versions") != dict(recipe.adapter_versions):
        return False
    version = stored.get("algorithm_version")
    if not isinstance(version, str) or without_fusion(version) != without_fusion(
        recipe.algorithm_version
    ):
        return False
    had, has = fusion_component(version), fusion_component(recipe.algorithm_version)
    return had is not None and has is not None and had < has


def serves_once_refused(
    stored: dict[str, Any], request: RunRequest, *, serve_free_from_deep: bool = False
) -> bool:
    """:func:`serves`, read as if the stored result had already been re-fused.

    The recipe id inside ``analysis_key`` hashes the algorithm version, so a Free result's stored
    key can never equal today's; everything else in that key is ``non_recipe_key``, which
    :func:`same_inputs` still compares, and the recipe is compared by name.
    """

    if not fusion_stale_only(stored):
        return False
    recipe = RECIPES[stored["recipe_name"]]
    lifted = {**stored, "algorithm_version": recipe.algorithm_version}
    if (stored.get("achieved") or stored.get("recipe_name")) == "free":
        lifted["analysis_key"] = request.inputs.analysis_key
    return serves(lifted, request, serve_free_from_deep=serve_free_from_deep)


def same_inputs(stored: dict[str, Any], request: RunRequest) -> bool:
    return (
        stored.get("compat_version") == COMPAT_VERSION
        and stored.get("non_recipe_key") == request.inputs.non_recipe_key
        and stored.get("tenant_scope") == request.inputs.tenant_scope
        and versions_current(stored)
    )


def same_evidence(stored: dict[str, Any], request: RunRequest) -> bool:
    """:func:`same_inputs` for RECOGNITION reuse: a result whose only staleness is its fusion
    version recorded exactly the observations this build would gather, so a Deep request may
    still reuse a ``fusion:2`` Free sweep rather than re-running it."""

    if fusion_stale_only(stored):
        recipe = RECIPES[stored["recipe_name"]]
        stored = {**stored, "algorithm_version": recipe.algorithm_version}
    return same_inputs(stored, request)


def serves(
    stored: dict[str, Any], request: RunRequest, *, serve_free_from_deep: bool = False
) -> bool:
    if not same_inputs(stored, request):
        return False
    if stored.get("status") != "complete" and not (
        stored.get("status") == "degraded" and request.local and request.accept_degraded
    ):
        return False
    name = stored.get("achieved") or stored.get("recipe_name")
    if request.recipe.name == "free":
        return (name == "free" and stored.get("analysis_key") == request.inputs.analysis_key) or (
            name == "deep" and serve_free_from_deep
        )
    return name == "deep" and stored.get("primary_density") in (
        {1, 2} if request.recipe.primary_density == 2 else {1}
    )


def _alias_dirs(media_dir: Path) -> list[Path]:
    """Every directory holding results for these exact bytes, this one first.

    Identical bytes reached under two URLs share a ``media_key`` and therefore an ``analysis_key``,
    but ingest files them under ``work/<source_key>/<media_key>`` — two directories (§3.4 source
    aliases).  Searching only the requester's directory would let the second alias re-run, and
    re-bill, an analysis that is already stored.
    """

    media_key = media_dir.name
    work_root = media_dir.parents[1] if len(media_dir.parents) > 1 else media_dir
    siblings = {
        path
        for path in Path(native_path(work_root)).glob(f"*/{media_key}")
        if path.name == media_key and path.is_dir()
    }
    return [media_dir, *sorted(siblings - {media_dir})]


def find_result(
    media_dir: Path,
    request: RunRequest,
    *,
    serve_free_from_deep: bool = False,
    free_evidence: bool = False,
) -> Path | None:
    from id_detector.present.bundles import read_bundle_manifest, read_manifest

    media_dir = Path(native_path(media_dir))
    # Frozen Free evidence is re-fused in place, and fusion records every observation file relative
    # to *this* media directory, so evidence reuse stays directory-local; only serving — where the
    # stored bundle is handed over whole — is alias-wide.
    roots = [media_dir] if free_evidence else _alias_dirs(media_dir)
    candidates = []
    for root in roots:
        for directory in Path(native_path(root / "present/bundles")).glob("*"):
            manifest = read_bundle_manifest(directory)
            if not manifest or manifest.get("media_key") != media_dir.name:
                continue
            stored = {
                **(manifest.get("compatibility") or {}),
                "status": manifest["status"],
                "achieved": manifest.get("achieved"),
            }
            eligible = serves(stored, request, serve_free_from_deep=serve_free_from_deep)
            if free_evidence:
                frozen = (root / manifest["fuse_run"]).resolve()
                eligible = (
                    request.recipe.name == "deep"
                    and stored["status"] == "complete"
                    and stored["achieved"] == "free"
                    and same_evidence(stored, request)
                    and frozen.is_relative_to((root / "fuse/runs").resolve())
                    and read_manifest(frozen) is not None
                    and (frozen / "shazam-observations.json").is_file()
                )
            if eligible:
                candidates.append(
                    (manifest.get("started_at", ""), manifest["presentation_version"], directory)
                )
    return max(candidates)[2] if candidates else None


def find_stale_result(
    media_dir: Path,
    *,
    recipe: Recipe,
    source_kind: str,
    tenant_scope: str,
    manual_tracklist_sha256: str,
    panako_index_id: str,
    with_hints: bool,
    accept_degraded: bool = False,
) -> tuple[Path, RunRequest] | None:
    """The stored bundle that is only a FUSION version behind this request — found OFFLINE.

    The ordinary lookup needs the request's hints snapshot, and producing one runs the hint
    connectors.  This one is asked BEFORE any connector runs: the request is read with each
    stored result's OWN hints snapshot (a request for no hints only matches a result made with
    none), so the question is "was this mix already analysed this way?", answered from disk.

    Only the NEWEST matching result counts, stale or current.  When that is a current one the
    answer is ``None`` and the caller carries on exactly as before (hints, then the ordinary
    lookup) — so a mix that has been re-fused, or analysed again since the bump, is never
    dragged back to the stale bundle still lying beside it.
    """

    from id_detector.present.bundles import (
        LEGACY_METADATA_ERROR,
        legacy_result_metadata,
        read_bundle_manifest,
    )

    media_dir = Path(native_path(media_dir))
    no_hints_id = hints_snapshot(())
    found: list[tuple[tuple[str, str, int], bool, Path, RunRequest]] = []

    def consider(
        *,
        root: Path,
        directory: Path,
        compatibility: dict[str, Any],
        status: object,
        achieved: object,
        started_at: object,
        run_id: object,
        presentation_version: object,
    ) -> None:
        inputs = compatibility.get("analysis_inputs")
        if not isinstance(inputs, dict) or not isinstance(inputs.get("hints_snapshot_id"), str):
            return
        if not with_hints and inputs["hints_snapshot_id"] != no_hints_id:
            return
        try:
            request = RunRequest(
                AnalysisInputs(
                    media_dir.name,
                    recipe.recipe_id,
                    source_kind,  # type: ignore[arg-type]
                    tenant_scope,
                    inputs["hints_snapshot_id"],
                    manual_tracklist_sha256,
                    panako_index_id,
                ),
                recipe,
                accept_degraded=accept_degraded,
            )
        except (KeyError, TypeError, ValueError):
            return
        stored = {**compatibility, "status": status, "achieved": achieved}
        stale = serves_once_refused(stored, request)
        if stale or serves(stored, request):
            order = (
                str(started_at or ""),
                str(run_id or ""),
                int(presentation_version or 0),
            )
            found.append((order, stale, directory, request))

    for root in _alias_dirs(media_dir):
        for directory in Path(native_path(root / "present/bundles")).glob("*"):
            manifest = read_bundle_manifest(directory)
            if not manifest or manifest.get("media_key") != media_dir.name:
                continue
            compatibility = manifest.get("compatibility") or {}
            if not isinstance(compatibility, dict):
                continue
            consider(
                root=root,
                directory=directory,
                compatibility=compatibility,
                status=manifest["status"],
                achieved=manifest.get("achieved"),
                started_at=manifest.get("started_at"),
                run_id=manifest["run_id"],
                presentation_version=manifest["presentation_version"],
            )
        # A page-only refresh or an unrelated bundle does not erase the flat result's provenance.
        # Legacy discovery is based on the flat presentation itself, not on bundle absence.
        metadata = legacy_result_metadata(root)
        if metadata is not None:
            # A pre-bundle result lives directly in ``present/``.  Its manifest is absent by
            # definition, so its own validated invocation is the sole authority for recipe and
            # inputs; missing metadata is not interpreted as Free.
            compatibility = metadata.get("compatibility")
            blocks_deep = metadata.get("achieved") == "deep" or LEGACY_METADATA_ERROR in metadata
            if recipe.name == "deep" and blocks_deep:
                # A legacy Deep result always requires explicit refresh before another Deep run.
                # Even a newer pre-bundle journal may carry compatibility inputs that differ from
                # today's optional inputs; that difference is not permission to spend again.
                try:
                    request = RunRequest(
                        AnalysisInputs(
                            media_dir.name,
                            recipe.recipe_id,
                            source_kind,  # type: ignore[arg-type]
                            tenant_scope,
                            no_hints_id,
                            manual_tracklist_sha256,
                            panako_index_id,
                        ),
                        recipe,
                        accept_degraded=accept_degraded,
                    )
                except ValueError:
                    continue
                order = (
                    str(metadata.get("started_at") or ""),
                    str(metadata.get("run_id") or ""),
                    0,
                )
                found.append((order, True, root / "present", request))
            elif isinstance(compatibility, dict):
                consider(
                    root=root,
                    directory=root / "present",
                    compatibility=compatibility,
                    status=metadata.get("status"),
                    achieved=metadata.get("achieved"),
                    started_at=metadata.get("started_at"),
                    run_id=metadata.get("run_id"),
                    presentation_version=0,
                )
    if not found:
        return None
    _, stale, directory, request = max(found, key=lambda item: item[0])
    return (directory, request) if stale else None


def hints_snapshot(hints: object) -> str:
    """Hash sorted complete HintRecords used by fusion; connector timing/status is excluded."""
    records = [hint.model_dump(mode="json") for hint in hints]
    return digest(sorted(records, key=digest))


def index_identity(root: Path, label: str | None) -> str:
    if label is None:
        return ""
    manifest = Path(native_path(root / label / "index.json"))
    return digest({"label": label, "manifest": sha256_file(manifest) if manifest.is_file() else ""})


def load_free_observations(bundle: Path) -> tuple[tuple[object, ...], Path]:
    from id_detector.contracts import ObservationRecord
    from id_detector.present.bundles import read_bundle_manifest

    manifest = read_bundle_manifest(bundle)
    path = bundle.parents[2] / manifest["fuse_run"] / "shazam-observations.json"
    return tuple(
        ObservationRecord.model_validate(item) for item in json.loads(read_text(path))
    ), path

"""Public, host-neutral entry point for one analysis run (plan §4.3).

This module is the seam. It owns:

* the request and result types §4.3 fixes verbatim;
* target validation — a hosted caller may name a platform URL or an opaque upload id, never a
  path, so the refusal happens here, before anything resolves or ingests a file;
* checkpointing — nine phases, each written only after that phase's artefacts are durable;
* recovery — the money, the resolved paid work and the completed phases a crashed or cancelled
  run of the same ``run_id`` already produced;
* outcome reporting — one :class:`RunResult` built from what the run did, and one status → exit
  code mapping (:func:`exit_code`) for every caller.

``cli.analyse`` and ``webapp.runner`` are thin callers of :func:`run`; the pipeline it drives is
:mod:`id_detector.pipeline`. Neither caller reads a journal, maps a status or runs a phase itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from id_detector.attempts import AttemptJournal, load_attempt_ledger
from id_detector.io import atomic_write_bytes, atomic_write_json, path_is_file, read_text
from id_detector.money import (
    BILLABLE_OUTCOMES,
    UsdAdmitter,
    UsdReservation,
    UsdSettlement,
    ceil_e2,
)
from id_detector.paid_clip import CancelToken
from id_detector.recipes import Recipe

CheckpointPhase = Literal[
    "ingest",
    "decode",
    "windows",
    "primary",
    "hints",
    "fuse1",
    "secondary",
    "fuse2",
    "present",
]
CHECKPOINT_PHASES: tuple[CheckpointPhase, ...] = (
    "ingest",
    "decode",
    "windows",
    "primary",
    "hints",
    "fuse1",
    "secondary",
    "fuse2",
    "present",
)

#: Plan §2.3.5's status matrix as the process exit code every caller reports. This is the ONLY
#: place a status becomes an exit code: the CLI and the web runner both read it, so a new status
#: can never mean two different things to two callers (the 3a-i duplicated-path defect).
STATUS_EXIT_CODES: dict[str, int] = {
    "complete": 0,
    "degraded": 0,
    "partial": 0,
    "provider_unavailable": 3,
    "budget_exhausted": 4,
    "source_changed": 5,
    "waiting": 6,
    "cancelled": 130,
    "failed": 1,
}

#: URL schemes a hosted caller may submit. A filesystem path, a UNC share and ``file:`` are not
#: audio sources a worker is ever allowed to reach (plan §4.2: workers never receive user paths).
ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
#: An opaque upload id is a hex/urlsafe token the host minted; it is never a path fragment.
_UPLOAD_ID_ALPHABET = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
_UPLOAD_ID_MAX = 128


class TargetRefused(ValueError):
    """A request named a target this host is not allowed to run (§4.2)."""


@dataclass(frozen=True)
class PlatformUrl:
    url: str


@dataclass(frozen=True)
class UploadId:
    upload_id: str


@dataclass(frozen=True)
class LocalPath:
    path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))


def validate_platform_url(url: str) -> str:
    """Return ``url`` if it is a real remote platform URL; refuse anything path-shaped.

    A hosted store must never be handed a filesystem path dressed as a platform URL: the ingest
    layer treats an existing path as local audio, so an unvalidated ``PlatformUrl`` would read
    server files (and, worse, journal them as a public ``platform`` analysis). Everything below is
    checked before any resolution or ingestion happens.
    """

    if not isinstance(url, str) or not url.strip():
        raise TargetRefused("platform URL is empty")
    candidate = url.strip()
    if candidate != url:
        raise TargetRefused("platform URL has surrounding whitespace")
    if "\n" in candidate or "\r" in candidate or "\x00" in candidate:
        raise TargetRefused("platform URL contains a control character")
    if candidate.startswith("\\\\") or candidate.startswith("//"):
        raise TargetRefused("platform URL is a UNC path")
    parts = urlsplit(candidate)
    scheme = parts.scheme.casefold()
    if scheme not in ALLOWED_URL_SCHEMES:
        raise TargetRefused(f"platform URL scheme is not allowed: {scheme or '(none)'}")
    if not parts.hostname:
        raise TargetRefused("platform URL has no host")
    if parts.username or parts.password or "@" in parts.netloc:
        raise TargetRefused("credential-bearing URLs are not accepted")
    # A Windows drive letter parses as a one-character scheme, so this only fires for an
    # ``http://c:/...`` style host; a plain ``C:\\mix.mp3`` is already refused by the scheme check.
    if len(parts.hostname) == 1 and parts.hostname.isalpha():
        raise TargetRefused("platform URL host is a drive letter")
    if Path(candidate).exists():
        raise TargetRefused("platform URL resolves to an existing filesystem path")
    return candidate


def validate_upload_id(upload_id: str) -> str:
    """Return ``upload_id`` if it is an opaque token, never a path fragment."""

    if not isinstance(upload_id, str) or not upload_id:
        raise TargetRefused("upload id is empty")
    if len(upload_id) > _UPLOAD_ID_MAX:
        raise TargetRefused("upload id is too long")
    if any(character not in _UPLOAD_ID_ALPHABET for character in upload_id):
        raise TargetRefused("upload id is not an opaque token")
    return upload_id


class CheckpointStore(Protocol):
    """Durable phase-completion boundary supplied by the caller."""

    mode: Literal["local", "hosted"]
    work_root: Path

    def completed_phases(self, run_id: str) -> frozenset[CheckpointPhase]: ...

    def write(
        self,
        run_id: str,
        phase: CheckpointPhase,
        *,
        artefacts: tuple[Path, ...],
        state: dict[str, Any] | None = None,
    ) -> None: ...

    def state(self, run_id: str, phase: CheckpointPhase) -> dict[str, Any]:
        """The recovery state written with ``phase``; ``{}`` when there is none.

        Part of the protocol, not an optional extra: recovery cannot restore a reservation, an
        achieved-recipe substitution or an artefact reference it is unable to read back.
        """
        ...


class Progress(Protocol):
    def __call__(self, phase: str, done: int, total: int, message: str) -> None: ...


@dataclass(frozen=True)
class PipelineOptions:
    """Local adapter settings; not part of the host-neutral request contract."""

    project_root: Path = field(default_factory=Path.cwd)
    print_raw: bool = False
    refresh: bool = False
    refresh_states: frozenset[str] = frozenset({"no_match"})
    max_requests: int = 2_000
    no_hints: bool = False
    confirmed_mirrors: tuple[str, ...] = ()
    app_config: object | None = None
    max_generations: int = 0
    novelty: bool = True
    calibrator: object | None = None
    enabled_engines: tuple[str, ...] = ()
    cli_confirmation: bool | None = None
    primary_engine: str | None = None
    allow_degrade: bool = False
    tenant_scope: str | None = None
    paid_scan_adapters: object | None = None
    shazam_http_client: object | None = None
    shazam_breaker: object | None = None
    local_index_label: str | None = None
    index_root: Path = Path("data/local/panako-db")
    panako_tool_dir: Path = Path("data/local/panako")
    paid_sleep: object | None = None
    keep_intermediates: bool = False


class LocalCheckpointStore:
    """Atomic JSON checkpoints for local mode; SQLite persistence arrives in phase 4b-i."""

    mode: Literal["local", "hosted"]

    def __init__(
        self,
        work_root: Path,
        *,
        mode: Literal["local", "hosted"] = "local",
        options: PipelineOptions | None = None,
    ) -> None:
        self.work_root = Path(work_root)
        self.mode = mode
        self.options = options or PipelineOptions()

    def _path(self, run_id: str) -> Path:
        safe = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        if not run_id or any(character not in safe for character in run_id):
            raise ValueError("unsafe run_id")
        return self.work_root / ".checkpoints" / f"{run_id}.json"

    def _document(self, run_id: str) -> dict[str, Any]:
        try:
            value = json.loads(read_text(self._path(run_id)))
        except (OSError, ValueError):
            return {"run_id": run_id, "phases": {}}
        phases = value.get("phases")
        if value.get("run_id") != run_id or not isinstance(phases, dict):
            return {"run_id": run_id, "phases": {}}
        return value

    def completed_phases(self, run_id: str) -> frozenset[CheckpointPhase]:
        known = set(CHECKPOINT_PHASES)
        return frozenset(self._document(run_id)["phases"].keys() & known)  # type: ignore[return-value]

    def write(
        self,
        run_id: str,
        phase: CheckpointPhase,
        *,
        artefacts: tuple[Path, ...],
        state: dict[str, Any] | None = None,
    ) -> None:
        if phase not in CHECKPOINT_PHASES:
            raise ValueError(f"unknown checkpoint phase: {phase}")
        missing = [str(path) for path in artefacts if not path_is_file(Path(path))]
        if missing:
            raise ValueError(f"checkpoint artefacts are not durable: {', '.join(missing)}")
        document = self._document(run_id)
        document["phases"][phase] = {
            "artefacts": [str(Path(path).resolve()) for path in artefacts],
            "state": state or {},
        }
        atomic_write_json(self._path(run_id), document)

    def state(self, run_id: str, phase: CheckpointPhase) -> dict[str, Any]:
        value = self._document(run_id)["phases"].get(phase, {})
        state = value.get("state", {}) if isinstance(value, dict) else {}
        return state if isinstance(state, dict) else {}

    def resolve_upload(self, upload_id: str) -> Path:
        candidate = (self.work_root / ".uploads" / validate_upload_id(upload_id)).resolve()
        if not candidate.is_relative_to((self.work_root / ".uploads").resolve()):
            raise TargetRefused("unsafe upload id")
        if not candidate.is_file():
            raise TargetRefused(f"unknown upload id: {upload_id}")
        return candidate


@dataclass(frozen=True)
class RunRequest:
    run_id: str
    analysis_key: str
    target: PlatformUrl | UploadId | LocalPath
    recipe: Recipe
    accept_degraded: bool
    hints_snapshot_policy: str
    manual_tracklist: bytes | None
    checkpoint_store: CheckpointStore
    attempt_journal: AttemptJournal | None
    usd_admitter: UsdAdmitter | None
    progress: Progress | None
    cancel_token: CancelToken | None


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: str
    reason: str | None
    achieved: str | None
    bundle_id: str | None
    usd_e6_reserved: int
    usd_e6_spent: int
    attempts: int


#: The inverse of :data:`STATUS_EXIT_CODES`, used for ONE thing: a pipeline that returned an exit
#: code without recording an outcome (only a test double does that -- every terminal path of the
#: real pipeline records one). Reporting "failed" for such a run would be a lie; guessing a second
#: mapping elsewhere would be the 3a-i defect again, so the inverse lives beside the forward map.
_STATUS_FOR_EXIT: dict[int, str] = {
    0: "complete",
    1: "failed",
    3: "provider_unavailable",
    4: "budget_exhausted",
    5: "source_changed",
    6: "waiting",
    130: "cancelled",
}


def exit_code_for(result: RunResult) -> int:
    """Plan §2.3.5's exit code for a finished run — the one mapping every caller uses."""

    return STATUS_EXIT_CODES.get(result.status, 1)


@dataclass(frozen=True)
class PaidRecovery:
    """What a previous pass of the SAME run already did at the paid provider (plan §2.3.3).

    ``billed_units`` is money this run has already spent: every attempt it resolved to a billable
    outcome, plus every attempt it dispatched and never resolved (ambiguous — the provider may
    well have billed it). ``resolved_query_ids`` must not be dispatched again; re-querying them
    would pay twice for one clip, which is exactly what a cached ``no_match`` did on resume before.
    """

    billed_units: int = 0
    resolved_query_ids: frozenset[str] = frozenset()
    ambiguous_query_ids: frozenset[str] = frozenset()

    @property
    def any(self) -> bool:
        return bool(self.billed_units or self.resolved_query_ids or self.ambiguous_query_ids)


def recover_paid_attempts(
    journal_path: Path, *, run_id: str, provider: str = "audd"
) -> PaidRecovery:
    """Fold the attempt journal into what ``run_id`` already spent and resolved.

    ``journal_path`` is the journal the sweep WRITES to — the caller's injected journal when there
    is one. Reading a different file (the per-media default) would make a hosted journal's
    unresolved dispatches invisible on recovery, and a recovered run would re-bill them.
    """

    del provider  # one journal per provider today; the ledger does not record it per attempt
    ledger = load_attempt_ledger(Path(journal_path))
    billed = 0
    resolved: set[str] = set()
    ambiguous: set[str] = set()
    for attempt in ledger.attempts:
        if attempt.run_id != run_id:
            continue
        if attempt.outcome is not None:
            resolved.add(attempt.query_id)
            if attempt.outcome in BILLABLE_OUTCOMES:
                billed += 1
        elif attempt.dispatched:
            ambiguous.add(attempt.query_id)
            billed += 1
    return PaidRecovery(
        billed_units=billed,
        resolved_query_ids=frozenset(resolved),
        ambiguous_query_ids=frozenset(ambiguous - resolved),
    )


def restored_settlement(units: int, unit_usd_e6: int) -> UsdSettlement:
    """A settlement that reports money a previous pass of this run already spent, with no
    reservation of its own — what a refusal (a lowered cap, an exceeded reservation) must journal
    so the run's existing spend is never erased."""

    spent = max(0, units) * unit_usd_e6
    return UsdSettlement(
        usd_e6_reserved=0,
        usd_e6_spent=spent,
        usd_e2_reserved=0,
        usd_e2_spent=ceil_e2(spent),
        usd_e6_released=0,
    )


def charge_restored_units(admitter: UsdAdmitter, units: int) -> bool:
    """Charge ``units`` already-billed requests to ``admitter`` before it admits anything new.

    The run resumes against its ORIGINAL cap: pre-charging means the remaining reservation is what
    is genuinely left, and the settlement carries cumulative spend. ``False`` means the reservation
    cannot even cover what this run already spent, so no further paid request may be made.
    """

    from id_detector.money import ReservationExhausted

    for _ in range(max(0, units)):
        try:
            admitter.admit()
        except ReservationExhausted:
            return False
        admitter.resolve("no_match")
    return True


def admitter_from_state(state: dict[str, Any], *, unit_usd_e6: int) -> UsdAdmitter | None:
    """Rebuild the primary's admitter from its durable checkpoint.

    A real :class:`UsdAdmitter` pre-charged to the checkpointed spend, not a stand-in: the run's
    remaining reservation, its cap and its settlement are then the same objects the first pass had,
    whatever the configured cap says today.
    """

    reserved = int(state.get("usd_e6_reserved", 0))
    spent = int(state.get("usd_e6_spent", 0))
    if not reserved and not spent:
        return None
    unit = int(state.get("unit_usd_e6") or unit_usd_e6) or 1
    admitter = UsdAdmitter(
        UsdReservation(
            planned=int(state.get("planned", 0)),
            unit_usd_e6=unit,
            usd_e6_reserved=reserved,
            usd_e2_reserved=int(state.get("usd_e2_reserved", ceil_e2(reserved))),
            effective_cap_e2=int(state.get("effective_cap_e2", ceil_e2(reserved))),
        )
    )
    charge_restored_units(admitter, spent // unit)
    return admitter


def _target_value(request: RunRequest) -> tuple[str, str]:
    """Resolve the request's target to a path/URL the pipeline may open, refusing the rest.

    Validation happens HERE, at the boundary, before any path resolution or ingestion: the
    pipeline's ingest step treats an existing path as local audio, so a target that reaches it is
    already trusted.
    """

    target = request.target
    store = request.checkpoint_store
    if isinstance(target, LocalPath):
        if store.mode != "local":
            raise TargetRefused("LocalPath is accepted only in local mode")
        return str(target.path), "local"
    if isinstance(target, PlatformUrl):
        return validate_platform_url(target.url), "platform"
    if isinstance(target, UploadId):
        upload_id = validate_upload_id(target.upload_id)
        resolver = getattr(store, "resolve_upload", None)
        if not callable(resolver):
            raise TargetRefused("checkpoint store cannot resolve UploadId targets")
        return str(resolver(upload_id)), "upload"
    raise TypeError("target must be PlatformUrl, UploadId, or LocalPath")


def _manual_tracklist(store: CheckpointStore, body: bytes | None) -> Path | None:
    if body is None:
        return None
    digest = sha256(body).hexdigest()
    path = store.work_root / ".tracklists" / f"{digest}.txt"
    if not path.is_file():
        atomic_write_bytes(path, body)
    return path


def run(request: RunRequest) -> RunResult:
    """Run exactly one analysis and report what it did."""

    from id_detector import pipeline

    target, source_kind = _target_value(request)
    store = request.checkpoint_store
    options = getattr(store, "options", PipelineOptions())
    tracklist = _manual_tracklist(store, request.manual_tracklist)
    completed = store.completed_phases(request.run_id)
    # ``state`` is part of the CheckpointStore protocol, not an optional extra: recovery cannot
    # restore a reservation, a recipe substitution or an artefact reference it cannot read back.
    checkpoint_state: dict[str, dict[str, Any]] = {
        phase: store.state(request.run_id, phase) for phase in completed
    }
    app_config = options.app_config
    unit_usd_e6 = int(getattr(app_config, "audd_usd_e6_per_request", 0) or 5_000)
    resumed_admitter = request.usd_admitter
    if resumed_admitter is None and "primary" in completed:
        resumed_admitter = admitter_from_state(
            checkpoint_state.get("primary", {}), unit_usd_e6=unit_usd_e6
        )
    result_paths: list[Path] = []
    outcome = pipeline.PipelineOutcome()

    def checkpoint(
        phase: str,
        artefacts: tuple[Path, ...],
        state: dict[str, Any] | None = None,
    ) -> None:
        store.write(request.run_id, phase, artefacts=artefacts, state=state)  # type: ignore[arg-type]

    with contextlib.suppress(asyncio.CancelledError):
        analysis_kwargs: dict[str, Any] = {
            "work_root": store.work_root,
            "print_raw": options.print_raw,
            "refresh": options.refresh,
            "refresh_states": options.refresh_states,
            "max_requests": options.max_requests,
            "tracklist": tracklist,
            "no_hints": options.no_hints or request.hints_snapshot_policy == "disabled",
            "confirmed_mirrors": options.confirmed_mirrors,
            "app_config": options.app_config,
            "max_generations": options.max_generations,
            "novelty": options.novelty,
            "calibrator": options.calibrator,
            "enabled_engines": options.enabled_engines,
            "recipe": request.recipe,
            "allow_degrade": options.allow_degrade,
            "accept_degraded": request.accept_degraded,
            "source_kind": source_kind,
            "tenant_scope": options.tenant_scope,
            "result_paths": result_paths,
            "paid_scan_adapters": options.paid_scan_adapters,
            "shazam_http_client": options.shazam_http_client,
            "shazam_breaker": options.shazam_breaker,
            "local_index_label": options.local_index_label,
            "index_root": options.index_root,
            "panako_tool_dir": options.panako_tool_dir,
            "progress": request.progress,
            "cancel_token": request.cancel_token,
            "paid_sleep": options.paid_sleep,
            "keep_intermediates": options.keep_intermediates,
            "supplied_run_id": request.run_id,
            "supplied_analysis_key": request.analysis_key,
            "checkpoint": checkpoint,
            "completed_phases": completed,
            "checkpoint_state": checkpoint_state,
            "attempt_journal": request.attempt_journal,
            "injected_usd_admitter": resumed_admitter,
            "project_root": options.project_root,
            "outcome": outcome,
        }
        if options.cli_confirmation is not None:
            analysis_kwargs["cli_confirmation"] = options.cli_confirmation
        if options.primary_engine is not None:
            analysis_kwargs["primary_engine"] = options.primary_engine
        returned = asyncio.run(pipeline.run_analysis(target, **analysis_kwargs))
        if not outcome.recorded:
            outcome.exit_code = int(returned)
            outcome.status = _STATUS_FOR_EXIT.get(int(returned), "failed")
    bundle = (
        outcome.bundle
        if outcome.bundle is not None
        else (result_paths[-1] if result_paths else None)
    )
    return RunResult(
        run_id=request.run_id,
        status=outcome.status,
        reason=outcome.reason,
        achieved=outcome.achieved,
        bundle_id=bundle.name if bundle is not None else None,
        usd_e6_reserved=outcome.usd_e6_reserved,
        usd_e6_spent=outcome.usd_e6_spent,
        attempts=outcome.attempts,
    )

"""The host-neutral analysis pipeline behind :func:`id_detector.service.run`.

Plan §4.2 puts one pipeline behind one seam: ``service.run`` owns execution and outcome
reporting, and this module is the implementation it owns.  Nothing here knows about Typer
options, HTTP, jobs or a user's filesystem — the caller resolves its target to a path, supplies
the checkpoint store, the attempt journal and the admitter, and reads the outcome back from
:class:`PipelineOutcome`.  ``cli.analyse`` and ``webapp.runner`` are thin callers of the seam;
neither maps a status to an exit code itself (:func:`id_detector.service.exit_code` is the one
place that does).

The pipeline lived in ``cli.py`` until cycle 4a-i; it moved here so that a second caller can
never drift from the first (the duplicated-path defect of 3a-i) and so that plan §4.2's
post-Phase-3 restriction on ``src/id_detector/`` holds: ``cli.py`` carries no pipeline.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

import typer

from id_detector.attempts import AttemptJournal, attempts_path
from id_detector.compat import (
    LOCAL_OWNER_SCOPE,
    AnalysisInputs,
    RunRequest,
    find_result,
    find_stale_result,
    hints_snapshot,
    index_identity,
    load_free_observations,
)
from id_detector.contracts import ObservationRecord, WindowRecord
from id_detector.decode import decode, load_decode
from id_detector.fuse.episodes import (
    CORROBORATION_OVERLAP_MIN_MS,
    CORROBORATION_SEPARATION_MIN_MS,
)
from id_detector.hints.pipeline import run_hints
from id_detector.ingest import SourceChanged, _load_cached, ingest
from id_detector.io import (
    atomic_write_json,
    native_path,
    path_is_file,
    read_text,
    sha256_file,
)
from id_detector.jobs import JobStoreLocked, ProcessLock
from id_detector.journal import InvocationTimer, append_invocation, has_invocation
from id_detector.local_index import run_local_index_recognition
from id_detector.money import (
    UNREACHABLE_OUTCOMES,
    BudgetExhausted,
    UsdAdmitter,
    UsdSettlement,
    reserve_usd,
)
from id_detector.orchestrate import (
    compute_novelty_change_points,
    run_generation_loop,
    scanned_windows,
)
from id_detector.paid_clip import (
    PAID_CLIP_ENGINES,
    CancelToken,
    PaidScanResult,
    SleepFn,
    run_paid_clip_recognition,
)
from id_detector.providers.audd import DEFAULT_ANCHOR_MAX_MS, DEFAULT_ANCHOR_SLACK_MS
from id_detector.providers.base import AppConfig
from id_detector.recipes import Recipe, get_recipe
from id_detector.recognise import _write_jsonl, recognise_generation, restore_run_answers
from id_detector.rescan import DEFAULT_MAX_GENERATIONS
from id_detector.run_ledger import (
    RecoveredMoney,
    ReservationRecord,
    new_run_id,
    recovered_money,
    restore_admitter,
)
from id_detector.scan import PAID_FILE_SCANNERS
from id_detector.scan_targeting import select_scan_targets
from id_detector.secondary_targeting import (
    SecondaryPick,
    allocate_secondary_windows,
    distribute_secondary_windows,
    energy_reader,
    frozen_windows,
    listed_text_keys,
    new_identity_discoveries,
    secondary_capacity,
    secondary_reserve,
    select_secondary_candidates,
    serve_confirmations,
)
from id_detector.shazam import HTTPClientInterface
from id_detector.shazam_breaker import ShazamBreaker, shazam_off
from id_detector.windows import (
    TransformGrid,
    WindowSchedule,
    WindowsResult,
    generate_windows_async,
)

#: The repository the pipeline reads its frozen, committed inputs from (profiles, calibration,
#: the fake-provider module).  ``cli`` re-exports it; every caller may override it per run.
PROJECT_ROOT = Path.cwd()
#: Every engine that can cost money; the Free recipe strips them all (its cap is $0).
_PAID_ENGINES = frozenset(PAID_FILE_SCANNERS) | frozenset(PAID_CLIP_ENGINES)


#: A pipeline progress hook: ``(phase, done, total, message)``.  ``phase`` is one of ``ingest``,
#: ``decode``, ``windows``, ``recognise``, ``hints``, ``fuse``, ``enrich`` or ``present``.  It is
#: optional everywhere (default ``None``) so the CLI path stays byte-for-byte unchanged; only the
#: web app supplies one.  A progress hook may raise ``asyncio.CancelledError`` to abort a run.
ProgressFn = Callable[[str, int, int, str], None]


def _report(progress: ProgressFn | None, phase: str, done: int, total: int, message: str) -> None:
    if progress is not None:
        progress(phase, done, total, message)


def _windows_in_spans(windows: WindowsResult, spans: tuple[tuple[int, int], ...]) -> WindowsResult:
    """A WindowsResult holding only the frozen (generation-0, untransformed) windows whose start
    falls in one of ``spans``.

    A ``rescan_only`` transform policy never puts a transformed window in generation 0, but a
    ``global`` policy does — and every sibling of a window shares its start, so keeping them would
    multiply a span's request count by the size of the transform grid (E-M9).
    """

    keep = tuple(
        window
        for window in windows.records
        if window.generation == 0
        and window.transform.type == "none"
        and any(lo <= window.support_ms[0] < hi for lo, hi in spans)
    )
    return WindowsResult(records=keep, record_path=windows.record_path, cached=windows.cached)


def _settle_money(usd_admitter: UsdAdmitter | None) -> UsdSettlement:
    """Terminal USD figures; a run that never reserved (Free, or refused) settles at zero."""

    if usd_admitter is None:
        return UsdSettlement(
            usd_e6_reserved=0,
            usd_e6_spent=0,
            usd_e2_reserved=0,
            usd_e2_spent=0,
            usd_e6_released=0,
        )
    return usd_admitter.settle()


def _money_journal_fields(
    settlement: UsdSettlement,
    recipe: Recipe,
    app_config: AppConfig,
    achieved: Recipe | None = None,
) -> dict[str, int | str]:
    """``requested_recipe_id`` names what was asked for; ``algorithm_version`` describes the result
    that was actually produced (the achieved recipe, when a degrade substituted one)."""

    return {
        "usd_e6_reserved": settlement.usd_e6_reserved,
        "usd_e6_spent": settlement.usd_e6_spent,
        "usd_e2_reserved": settlement.usd_e2_reserved,
        "usd_e2_spent": settlement.usd_e2_spent,
        "requested_recipe_id": recipe.recipe_id,
        "algorithm_version": (achieved or recipe).algorithm_version,
        "pricing_version": app_config.pricing_version,
        "audd_usd_e6_per_request": app_config.audd_usd_e6_per_request,
    }


def _refuse_stored(
    stale: Path,
    *,
    request: RunRequest,
    analysis_key: str,
    config: AppConfig,
    calibrator: object | None,
    local: bool,
    own_media_dir: Path,
) -> Path:
    """Re-fuse the stored bundle ``stale`` under today's fusion version, stamped as the answer to
    ``request``.  Offline by construction (:mod:`id_detector.refusion`).  Raises
    :class:`~id_detector.refusion.NotRebuildable`, with the reason in plain words, when it cannot
    be done faithfully — the caller then STOPS; it never analyses afresh on its own initiative."""

    from id_detector.present.bundles import (
        LEGACY_METADATA_ERROR,
        legacy_result_metadata,
        load_run_snapshot,
        read_bundle_manifest,
    )
    from id_detector.refusion import NotRebuildable, is_deep, refuse_snapshot

    stale_manifest = read_bundle_manifest(stale)
    legacy = stale_manifest is None
    stored_dir = stale.parent if legacy else stale.parents[2]
    lock: ProcessLock | None = None
    try:
        if stored_dir.resolve() != own_media_dir.resolve():
            # A source alias of the same bytes: its evidence lives in ITS directory.
            stored_lock = ProcessLock(stored_dir / ".media.lock")
            held_lock = ProcessLock(own_media_dir / ".media.lock")
            # Source aliases have different directories but the same media key, so ProcessLock
            # deliberately maps both paths to one canonical work-root lock.  The pipeline already
            # owns that lock for ``own_media_dir``; trying to acquire it again would reject this
            # legitimate in-process re-fusion as busy forever.
            if stored_lock.path != held_lock.path:
                lock = stored_lock
                lock.acquire()
        stored_metadata = legacy_result_metadata(stored_dir) if legacy else stale_manifest
        metadata_problem = (stored_metadata or {}).get(LEGACY_METADATA_ERROR)
        if metadata_problem:
            raise NotRebuildable(
                f"{metadata_problem}; without trustworthy legacy metadata it is not safe to "
                "assume the stored result was Free"
            )
        if is_deep(stale_manifest, stored_metadata):
            # Refuse from the result's own provenance before touching evidence. Retention may
            # legitimately have removed PCM/windows, but that is never permission for a new paid
            # sweep.
            raise NotRebuildable(
                "it is a paid (Deep) result, and the windows its second opinion checked were "
                "chosen by the older fusion rules, so it cannot simply be rebuilt"
            )
        snapshot = load_run_snapshot(stored_dir, directory=None if legacy else stale)
        stored = (snapshot.manifest or snapshot.metadata).get("compatibility") or {}
        achieved = get_recipe(
            str(
                (snapshot.manifest or snapshot.metadata).get("achieved")
                or stored.get("recipe_name")
            ),
            primary_density=int(stored.get("primary_density") or 1),
        )
        return refuse_snapshot(
            snapshot,
            media_dir=stored_dir,
            config=config,
            calibrator=calibrator,
            compatibility={**request.metadata(achieved), "analysis_key": analysis_key},
            local=local,
        )
    except JobStoreLocked as exc:
        raise NotRebuildable("another analysis of the same audio is running right now") from exc
    except (OSError, ValueError, KeyError) as exc:
        raise NotRebuildable(f"the stored result could not be read ({exc})") from exc
    finally:
        if lock is not None:
            lock.release()


#: What a run reports when a stored result exists, is only out of date, and could not be rebuilt.
STALE_RESULT_REASON = (
    "stale_result: this mix already has a result, made by older track-matching rules, and it "
    "could not be rebuilt offline because {why}. Nothing was spent and nothing was sent to any "
    "recognition service; the stored result is still shown as it was. To analyse the mix again "
    "from scratch - a paid recipe will reserve and spend again - run it again with --refresh."
)


def _achieved(resolved: int, planned: int, fraction: float) -> bool:
    """``resolved >= fraction x planned`` in exact integer arithmetic; an empty plan is achieved."""

    return resolved * 100 >= round(fraction * 100) * planned


def _run_status(
    *,
    primary_resolved: int,
    primary_planned: int,
    primary_fraction: float,
    secondary_resolved: int,
    secondary_allocated: int,
    secondary_fraction: float | None,
    provider_stopped: str | None,
    reservation_exhausted: bool,
) -> tuple[str, str | None]:
    """Plan §2.3.5: the terminal status and reason of a run that produced a result.

    ``partial`` (the primary requirement was not met) outranks ``degraded`` (only the secondary
    fell short); a terminal-provider stop after at least one resolved attempt is ``partial`` too.
    """

    if provider_stopped is not None:
        return "partial", "provider_unavailable_midrun"
    if reservation_exhausted:
        return "partial", "reservation_exhausted"
    if not _achieved(primary_resolved, primary_planned, primary_fraction):
        return "partial", "primary_not_achieved"
    if secondary_fraction is not None and not _achieved(
        secondary_resolved, secondary_allocated, secondary_fraction
    ):
        return "degraded", "secondary_not_achieved"
    return "complete", None


@dataclass
class PipelineOutcome:
    """What one run actually did - the service's single source of truth for its ``RunResult``.

    Every terminal path in :func:`run_analysis` fills this in before it returns (or re-raises),
    so no caller has to re-read the per-media journal and guess which entry belongs to this
    run.  That guess is what let a cache hit inherit an earlier run's spend before 4a-i (U-F15).
    """

    exit_code: int = 1
    status: str = "failed"
    reason: str | None = None
    achieved: str | None = None
    bundle: Path | None = None
    usd_e6_reserved: int = 0
    usd_e6_spent: int = 0
    usd_e2_spent: int = 0
    attempts: int = 0
    served_compatible: bool = False
    timings: dict[str, int] = field(default_factory=dict)
    #: ``True`` once a terminal path has filled this in. The real pipeline always records; a test
    #: double that only returns an exit code does not, and the service says so instead of guessing.
    recorded: bool = False


def _checkpointed_path(media_dir: Path, state: Mapping[str, object], key: str) -> Path:
    """A checkpointed artefact reference resolved back to an absolute path.

    References are stored relative to the media directory so a work root that moved (or a hosted
    worker that mounts it elsewhere) still resolves them; an absolute one is honoured as written.
    """

    path = Path(str(state.get(key, "")))
    return path if path.is_absolute() else media_dir / path


def _checkpointed_paths(media_dir: Path, values: object) -> tuple[Path, ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    resolved = []
    for value in values:
        path = Path(str(value))
        resolved.append(path if path.is_absolute() else media_dir / path)
    return tuple(resolved)


def _reference(media_dir: Path, path: Path) -> str:
    """``path`` as the stable, media-relative reference a checkpoint stores."""

    try:
        return Path(path).relative_to(media_dir).as_posix()
    except ValueError:
        return Path(path).as_posix()


def _read_observations(*paths: Path) -> tuple[ObservationRecord, ...]:
    """Every observation in ``paths``, in file order; a missing file contributes nothing."""

    records: list[ObservationRecord] = []
    for path in paths:
        if not path_is_file(path):
            continue
        records.extend(
            ObservationRecord.model_validate_json(line)
            for line in read_text(path).splitlines()
            if line.strip()
        )
    return tuple(records)


def _primary_state(
    *,
    media_dir: Path,
    observation_path: Path | None,
    paid_first: bool,
    achieved_recipe: Recipe,
    degrade_reason: str | None,
    primary_planned: int,
    primary_resolved: int,
    primary_clip: PaidScanResult,
    free_failures: int,
    counts: Mapping[str, int],
    usd_admitter: UsdAdmitter | None,
) -> dict[str, object]:
    """Everything a resume needs about the completed primary.

    Besides the evidence and the counts this records the SUBSTITUTION (``achieved`` / ``paid_first``
    / ``degrade_reason``) and the whole reservation, so a resumed run cannot take the paid branch
    for Free evidence, and cannot lose or re-derive the money against a cap that has since changed.
    """

    reservation = usd_admitter.reservation if usd_admitter is not None else None
    return {
        "observation_path": (
            _reference(media_dir, observation_path) if observation_path is not None else ""
        ),
        "paid_first": paid_first,
        "achieved": achieved_recipe.name,
        "degrade_reason": degrade_reason,
        "primary_planned": primary_planned,
        "resolved": primary_resolved,
        "requests": int(counts.get("requests", 0)),
        "physical_attempts": int(counts.get("physical_attempts", 0)),
        "failures": primary_clip.failures if paid_first else free_failures,
        "cache_hits": primary_clip.cache_hits if paid_first else int(counts.get("cache_hits", 0)),
        "billable_units": primary_clip.billable_units,
        "reservation_exhausted": primary_clip.reservation_exhausted,
        "provider_stopped": primary_clip.provider_stopped,
        "attempts": primary_clip.attempts,
        "planned": reservation.planned if reservation is not None else 0,
        "unit_usd_e6": reservation.unit_usd_e6 if reservation is not None else 0,
        "effective_cap_e2": reservation.effective_cap_e2 if reservation is not None else 0,
        "usd_e6_reserved": reservation.usd_e6_reserved if reservation is not None else 0,
        "usd_e2_reserved": reservation.usd_e2_reserved if reservation is not None else 0,
        "usd_e6_spent": usd_admitter.usd_e6_spent if usd_admitter is not None else 0,
    }


def _secondary_state(
    *,
    media_dir: Path,
    secondary_scan: PaidScanResult,
    index_scan: PaidScanResult,
    secondary_allocated: int,
    secondary_resolved: int,
    secondary_blocked: str | None,
    supplemental_requests: int,
    supplemental_physical_attempts: int,
    counts: Mapping[str, int],
) -> dict[str, object]:
    """Everything a resume needs about the completed second opinion."""

    return {
        "secondary_paths": [
            _reference(media_dir, path) for path in secondary_scan.observation_paths if path
        ],
        "index_paths": [
            _reference(media_dir, path) for path in index_scan.observation_paths if path
        ],
        "allocated": secondary_allocated,
        "resolved": secondary_resolved,
        "blocked": secondary_blocked,
        "supplemental_requests": supplemental_requests,
        "supplemental_physical_attempts": supplemental_physical_attempts,
        "counts": {
            key: value
            for key, value in counts.items()
            if key.startswith("secondary") or key == "local_index_matches"
        },
    }


async def run_analysis(
    url: str,
    *,
    work_root: Path,
    print_raw: bool,
    refresh: bool,
    refresh_states: frozenset[str] = frozenset({"no_match"}),
    max_requests: int,
    tracklist: Path | None,
    no_hints: bool,
    confirmed_mirrors: tuple[str, ...] = (),
    app_config: AppConfig | None = None,
    max_generations: int = DEFAULT_MAX_GENERATIONS,
    novelty: bool = True,
    calibrator: object | None = None,
    enabled_engines: tuple[str, ...] = (),
    cli_confirmation: bool = False,
    primary_engine: str = "shazam",
    recipe: Recipe | None = None,
    allow_degrade: bool = False,
    accept_degraded: bool = False,
    source_kind: str | None = None,
    tenant_scope: str | None = None,
    result_paths: list[Path] | None = None,
    paid_scan_adapters: Mapping[str, object] | None = None,
    shazam_http_client: HTTPClientInterface | None = None,
    shazam_breaker: ShazamBreaker | None = None,
    local_index_label: str | None = None,
    index_root: Path = Path("data/local/panako-db"),
    panako_tool_dir: Path = Path("data/local/panako"),
    progress: ProgressFn | None = None,
    cancel_token: CancelToken | None = None,
    paid_sleep: SleepFn | None = None,
    keep_intermediates: bool = False,
    supplied_run_id: str | None = None,
    supplied_analysis_key: str = "",
    checkpoint: Callable[[str, tuple[Path, ...], dict[str, object] | None], None] | None = None,
    completed_phases: frozenset[str] = frozenset(),
    checkpoint_state: Mapping[str, dict[str, object]] | None = None,
    attempt_journal: AttemptJournal | None = None,
    injected_usd_admitter: UsdAdmitter | None = None,
    project_root: Path | None = None,
    outcome: PipelineOutcome | None = None,
    presentation_local: bool = True,
    dispatch_admission: Callable[[], None] | None = None,
    settlement_writer: Callable[[Path, object], object] | None = None,
) -> int:
    """Run one analysis and return its exit code (plan §2.3.5).

    ``cancel_token`` is polled by the paid sweep before every dispatch (the web app passes its
    job's cancel event); when it fires the run ends ``cancelled`` once the clips in flight have
    resolved.  ``paid_sleep`` injects the AudD retry-backoff sleeper (tests pass a no-op).
    """

    app_config = app_config or AppConfig()
    pipeline_project_root = project_root or PROJECT_ROOT
    requested_recipe = recipe or get_recipe(
        "deep" if primary_engine == "audd" else "free",
        primary_density=app_config.deep_primary_density,
    )
    if requested_recipe.name == "free":
        # The Free recipe's zero-dollar cap is structural: legacy --engine/profile state cannot
        # smuggle a paid provider into this run.
        enabled_engines = tuple(engine for engine in enabled_engines if engine not in _PAID_ENGINES)
    elif "audd" not in enabled_engines:
        enabled_engines = (*enabled_engines, "audd")
    shazam_breaker = shazam_breaker or ShazamBreaker(app_config.shazam_breaker)
    if not shazam_off():
        shazam_breaker.configure(app_config.shazam_breaker)
    if not supplied_run_id and (dispatch_admission is not None or settlement_writer is not None):
        # A job-driven run never mints a fallback id: a crash would resume a DIFFERENT run.
        raise ValueError("a job-driven analysis must carry its durable run id")
    run_id = supplied_run_id or new_run_id()
    timer = InvocationTimer(run_id, ["analyse", url], keep_intermediates=keep_intermediates)
    media_dir: Path | None = None
    ffmpeg_version: str | None = None
    source_ids: list[str] = []
    source_lock: ProcessLock | None = None
    media_lock: ProcessLock | None = None
    usd_admitter = injected_usd_admitter
    request = None
    counts = {
        "requests": 0,
        "physical_attempts": 0,
        "matches": 0,
        "failures": 0,
        "cache_hits": 0,
    }
    report = outcome if outcome is not None else PipelineOutcome()
    #: What every durable record says THIS run already reserved and spent: an earlier pass's
    #: attempt ledger, its reservation record and its primary checkpoint. Filled in as soon as the
    #: media directory is known; every settlement below is merged with it, so no terminal path —
    #: a cancel, a refusal, a failure, a served compatible result — can report less (§2.3.2).
    recovered = RecoveredMoney()
    journal: AttemptJournal | None = attempt_journal

    def _settle() -> UsdSettlement:
        return recovered.merged(_settle_money(usd_admitter))

    def _append_settlement(path: Path, entry: object) -> None:
        # SQLite is the single authority: a job-driven run's settlement is a row written under
        # its claim fence in one transaction, and invocations.jsonl is projected from it after
        # commit. A worker whose claim was reclaimed (or lease lost) writes NOTHING.
        if settlement_writer is not None:
            settlement_writer(path, entry)
            return
        append_invocation(path, entry)  # type: ignore[arg-type]

    def _finish(
        *,
        exit_code: int,
        status: str,
        reason: str | None = None,
        achieved: str | None = None,
        bundle: Path | None = None,
        settlement: UsdSettlement | None = None,
        served_compatible: bool = False,
    ) -> int:
        """Freeze this run's outcome for the service, then hand back the exit code.

        The status/reason pair, the achieved recipe and the settled money are exactly the ones
        the journal entry beside this call carries, so the caller's ``RunResult`` and the
        durable §2.3.5 record can never disagree.
        """

        report.recorded = True
        report.exit_code = exit_code
        report.status = status
        report.reason = reason
        report.achieved = achieved
        report.bundle = bundle
        report.served_compatible = served_compatible
        report.attempts = int(counts.get("paid_attempts", 0) or 0)
        if settlement is not None:
            report.usd_e6_reserved = settlement.usd_e6_reserved
            report.usd_e6_spent = settlement.usd_e6_spent
            report.usd_e2_spent = settlement.usd_e2_spent
        report.timings = dict(timer.timings)
        return exit_code

    def _checkpoint(
        phase: str, artefacts: tuple[Path, ...], state: dict[str, object] | None = None
    ) -> None:
        if checkpoint is not None and phase not in completed_phases:
            checkpoint(phase, artefacts, state)

    def refuse_free() -> bool:
        """Refuse a Free request that would start new Shazam work while the breaker is open.

        Called only where a new analysis is about to begin — never before the compatible-result
        lookup (§3.4), which serves a stored result without a single Shazam request.  A Deep
        request refused at its ``--allow-degrade`` restart has already reserved USD, so the
        journal settles that reservation rather than reporting a run that never reserved.
        """

        reason = "shazam_manual_off" if shazam_off() else shazam_breaker.reason()
        if reason is None:
            return False
        settlement = _settle()
        entry = timer.entry(
            status="waiting",
            reason=reason,
            exit_code=6,
            counts=counts,
            costs={"usd_e2": settlement.usd_e2_spent},
            source_ids=source_ids,
            ffmpeg_version=ffmpeg_version,
            **_money_journal_fields(settlement, requested_recipe, app_config),
        )
        _append_settlement((media_dir or work_root) / "invocations.jsonl", entry)
        _finish(exit_code=6, status="waiting", reason=reason, settlement=settlement)
        typer.echo(
            f"waiting ({reason}): not queued locally; retry after re-enable or recovery", err=True
        )
        return True

    try:
        lock_key = sha256(url.encode("utf-8")).hexdigest()
        source_lock = ProcessLock(work_root.resolve() / ".locks" / f"{lock_key}.lock")
        source_lock.acquire()
        _report(progress, "ingest", 0, 1, "resolving source")
        timer.start_stage("ingest_ms")
        retained = _load_cached(work_root, url)
        ingested = retained or await ingest(url, work_root)
        timer.finish_stage("ingest_ms")
        media_dir = ingested.media_dir
        acquired_media_lock = ProcessLock(media_dir / ".media.lock")
        acquired_media_lock.acquire()
        media_lock = acquired_media_lock
        source_ids = [f"source:{ingested.record.source_key}"]
        # Plan §2.3.2/§2.3.3: recover THIS run's money before anything can return early. The
        # journal the sweep writes to is the one recovery reads (the caller's, when injected).
        if journal is None:
            journal = AttemptJournal(
                attempts_path(media_dir),
                run_id=run_id,
                provider="audd",
                unit_usd_e6=app_config.audd_usd_e6_per_request,
            )
        if dispatch_admission is not None and getattr(journal, "admission", None) is None:
            journal.admission = dispatch_admission  # the queue's ONE admission check
        durable_reservation = journal.durable_reservation()
        primary_floor = (checkpoint_state or {}).get("primary", {})
        recovered = recovered_money(
            journal.run_ledger(),
            durable_reservation,
            RecoveredMoney(
                int(primary_floor.get("usd_e6_reserved", 0) or 0),
                int(primary_floor.get("usd_e6_spent", 0) or 0),
                int(primary_floor.get("attempts", 0) or 0),
            ),
        )
        _report(progress, "ingest", 1, 1, ingested.record.title or "source ready")
        # The phase's durable artefact is the immutable source record.  The fetched original is
        # checkpointed only when it is still there: retention deliberately prunes it while the
        # retained RESULT stays servable (``ingest._load_cached`` requires no original), and a
        # checkpoint that insisted on it would fail a post-retention cache hit before the
        # compatibility lookup below ever ran.
        ingest_artefacts: tuple[Path, ...] = (ingested.source_path,)
        if path_is_file(ingested.original_path):
            ingest_artefacts = (*ingest_artefacts, ingested.original_path)
        _checkpoint("ingest", ingest_artefacts)

        kind = source_kind or ("local" if ingested.record.platform == "file" else "platform")
        manual_hash = sha256_file(tracklist) if tracklist is not None else ""
        panako_id = index_identity(index_root, local_index_label)
        scope = tenant_scope or (
            LOCAL_OWNER_SCOPE if kind == "upload" or manual_hash or panako_id else "public"
        )
        # A fusion bump retires every stored result for SERVING, never its recognition.  Asked
        # BEFORE any hint connector runs (so this path is offline end to end) and before the
        # breaker check, the reservation and every dispatch: is the newest stored answer to this
        # request only a fusion version behind?  Then it is re-fused from its own recorded
        # observations and served like any stored result — or, when that cannot be done
        # faithfully, the run STOPS with the reason.  It never goes on to a fresh analysis by
        # itself: for a paid recipe that would silently pay for the same sweep twice.  Only an
        # explicit ``--refresh`` analyses again.
        stale_found = (
            None
            if refresh
            else find_stale_result(
                media_dir,
                recipe=requested_recipe,
                source_kind=kind,
                tenant_scope=scope,
                manual_tracklist_sha256=manual_hash,
                panako_index_id=panako_id,
                with_hints=not no_hints,
                accept_degraded=accept_degraded,
            )
        )
        from id_detector.present.bundles import read_bundle_manifest

        retained_manifest = read_bundle_manifest(ingested.source_path.parent) if retained else None
        decoded = None
        duration_ms = int(retained_manifest["duration_ms"]) if retained_manifest is not None else 0
        if stale_found is None:
            if "decode" in completed_phases:
                decoded = load_decode(media_dir)
                ffmpeg_version = decoded.record.decoder.ffmpeg_version
                duration_ms = decoded.record.pcm.duration_ms
            elif retained_manifest is None:
                _report(progress, "decode", 0, 1, "decoding audio")
                timer.start_stage("decode_ms")
                decoded = await decode(ingested)
                timer.finish_stage("decode_ms")
                ffmpeg_version = decoded.record.decoder.ffmpeg_version
                duration_ms = decoded.record.pcm.duration_ms
                _report(progress, "decode", 1, 1, "audio decoded")
        hint_result = None
        if not no_hints and stale_found is None:
            _report(progress, "hints", 0, 1, "reading tracklist hints")
            timer.start_stage("hints_ms")
            hint_result = await run_hints(
                source=ingested.record,
                duration_ms=duration_ms,
                media_dir=media_dir,
                source_path=ingested.source_path,
                project_root=pipeline_project_root,
                manual_tracklist=tracklist,
                confirmed_mirrors=confirmed_mirrors,
                refresh=refresh,
                disabled_connectors=app_config.disabled_hint_connectors,
            )
            timer.finish_stage("hints_ms")
            counts["hints"] = len(hint_result.hints)
            _report(progress, "hints", 1, 1, f"{len(hint_result.hints)} hints")

        request = (
            stale_found[1]
            if stale_found is not None
            else RunRequest(
                AnalysisInputs(
                    ingested.record.media_key,
                    requested_recipe.recipe_id,
                    kind,
                    scope,
                    hints_snapshot(hint_result.hints if hint_result else ()),
                    manual_hash,
                    panako_id,
                ),
                requested_recipe,
                accept_degraded=accept_degraded,
            )
        )
        effective_analysis_key = supplied_analysis_key or request.inputs.analysis_key
        compatibility = request.metadata()
        compatibility["analysis_key"] = effective_analysis_key
        timer.analysis_key = effective_analysis_key
        timer.compatibility = compatibility
        if stale_found is not None:
            from id_detector.refusion import NotRebuildable

            try:
                compatible = _refuse_stored(
                    stale_found[0],
                    request=request,
                    analysis_key=effective_analysis_key,
                    config=app_config,
                    calibrator=calibrator,
                    local=presentation_local,
                    own_media_dir=media_dir,
                )
            except NotRebuildable as exc:
                stale_reason = STALE_RESULT_REASON.format(why=str(exc))
                stale_settlement = _settle()
                entry = timer.entry(
                    status="failed",
                    reason=stale_reason,
                    exit_code=1,
                    counts=counts,
                    costs={"usd_e2": stale_settlement.usd_e2_spent},
                    source_ids=source_ids,
                    ffmpeg_version=ffmpeg_version,
                    **_money_journal_fields(stale_settlement, requested_recipe, app_config),
                )
                _append_settlement(media_dir / "invocations.jsonl", entry)
                typer.echo(stale_reason, err=True)
                return _finish(
                    exit_code=1, status="failed", reason=stale_reason, settlement=stale_settlement
                )
        else:
            compatible = (
                None
                if refresh
                else find_result(
                    media_dir, request, serve_free_from_deep=app_config.serve_free_from_deep
                )
            )
        if compatible is not None:
            # §3.4 compatibility serving: the stored result answers this request without a
            # single provider call, so the served bundle's own status is reported and THIS
            # run's spend is zero.  Copying the stored money across would invent a charge.
            served = read_bundle_manifest(compatible) or {}
            if result_paths is not None:
                result_paths.append(compatible)
            typer.echo(f"cached; tracklist={compatible / 'tracklist.json'}")
            served_status = str(served.get("status") or "complete")
            served_reason = str(served["reason"]) if served.get("reason") is not None else None
            served_achieved = (
                str(served["achieved"]) if served.get("achieved") is not None else None
            )
            served_settlement = _settle()
            if (
                settlement_writer is not None
                or recovered.any
                or has_invocation(media_dir / "invocations.jsonl", run_id)
            ):
                # A job-owned run ALWAYS settles (its SQLite row), even a zero-money cache hit.
                # Any earlier settlement of THIS run is replaced, even at zero money (a run
                # cancelled before reserving that is later served must not stay `cancelled`).
                # A resumed run that already reserved or spent (e.g. killed after its bundle was
                # published and before its settlement): the run settles exactly once, with the
                # money it really spent — never the zero a fresh cache hit reports.
                served_entry = timer.entry(
                    status=served_status,
                    reason=served_reason,
                    achieved=served_achieved,
                    exit_code=0,
                    counts=counts,
                    costs={"usd_e2": served_settlement.usd_e2_spent},
                    source_ids=source_ids,
                    ffmpeg_version=ffmpeg_version,
                    **_money_journal_fields(served_settlement, requested_recipe, app_config),
                )
                _append_settlement(
                    media_dir / "invocations.jsonl",
                    served_entry.model_copy(update={"bundle_id": compatible.name}),
                )
            return _finish(
                exit_code=0,
                status=served_status,
                reason=served_reason,
                achieved=served_achieved,
                bundle=compatible,
                settlement=served_settlement,
                served_compatible=True,
            )
        # Submission order (§3.4): the compatible result above is served even while the breaker is
        # open — it needs no Shazam request.  Only a request that would start a new analysis waits.
        if requested_recipe.name == "free" and refuse_free():
            return 6
        free_bundle = None if refresh else find_result(media_dir, request, free_evidence=True)
        reused_observations, reused_path = (
            load_free_observations(free_bundle) if free_bundle else ((), None)
        )

        if free_bundle is not None:
            max_generations = 0  # Reused Free evidence replaces all new Shazam allocation.
        if decoded is None:
            ingested = await ingest(url, work_root)
            _report(progress, "decode", 0, 1, "decoding audio")
            timer.start_stage("decode_ms")
            decoded = await decode(ingested)
            timer.finish_stage("decode_ms")
            ffmpeg_version = decoded.record.decoder.ffmpeg_version
            # The retained manifest's duration seeded the identity above; from here the plan's
            # window, reservation and secondary arithmetic runs on the decoded PCM's own figure.
            duration_ms = decoded.record.pcm.duration_ms
            _report(progress, "decode", 1, 1, "audio decoded")
        _checkpoint("decode", (decoded.record_path, decoded.pcm_path))

        _report(progress, "windows", 0, 1, "cutting windows")
        timer.start_stage("windows_ms")
        if "windows" in completed_phases:
            windows_path = media_dir / "windows/windows.gen0.jsonl"
            windows = WindowsResult(
                records=tuple(
                    WindowRecord.model_validate_json(line)
                    for line in read_text(windows_path).splitlines()
                    if line.strip()
                ),
                record_path=windows_path,
                cached=True,
            )
        else:
            windows = await generate_windows_async(
                decoded,
                media_dir,
                schedule=WindowSchedule(
                    window_ms=app_config.window_ms,
                    hop_ms=app_config.hop_ms,
                    phase_ms=app_config.phase_ms,
                ),
                transform_policy=app_config.transforms_policy,
                transform_grid=TransformGrid(
                    rates_e4=app_config.transform_rates_e4,
                    semitones=app_config.transform_semitones,
                ),
            )
        timer.finish_stage("windows_ms")
        _report(progress, "windows", 1, 1, f"{len(windows.records)} windows")
        _checkpoint(
            "windows",
            (windows.record_path, *(media_dir / item.wav_path for item in windows.records)),
        )

        # Plan §2.3.3: what a previous pass of THIS run already dispatched and paid for.  The
        # authority is the journal the sweep writes to (the caller's, when one was injected):
        # recovering it before any new paid request means resuming cannot re-bill resolved work
        # and cannot omit earlier spend from this run's settlement.
        restored_over_cap = False

        # The frozen generation-0 window set is what every recipe plans against (§2.3.1): the
        # Deep reservation, both primaries' achieved fractions and the secondary's eligibility.
        frozen_count = len(frozen_windows(windows.records))
        primary_planned = (
            frozen_count + requested_recipe.primary_density - 1
        ) // requested_recipe.primary_density
        if requested_recipe.name == "deep":
            planned = primary_planned
            counts["paid_planned"] = planned
            if usd_admitter is not None and durable_reservation is None:
                # The caller's admitter (or one restored from a primary checkpoint written before
                # reservation records existed) carries the run's original reservation.
                durable_reservation = ReservationRecord.from_reservation(
                    run_id, usd_admitter.reservation
                )
            if durable_reservation is None and journal.dispatch_without_reservation():
                # Fail closed: the SQLite authority proves this run already dispatched, yet holds
                # no reservation. Recomputing one from today's price or cap could authorise spend
                # the run never reserved, so no further paid request is made at all.
                missing_settlement = _settle()
                entry = timer.entry(
                    status="failed",
                    reason="reservation_missing",
                    exit_code=1,
                    counts=counts,
                    costs={"usd_e2": missing_settlement.usd_e2_spent},
                    source_ids=source_ids,
                    ffmpeg_version=ffmpeg_version,
                    **_money_journal_fields(missing_settlement, requested_recipe, app_config),
                )
                _report(progress, "recognise", 0, planned, "refused: no durable reservation")
                _append_settlement(media_dir / "invocations.jsonl", entry)
                return _finish(
                    exit_code=1,
                    status="failed",
                    reason="reservation_missing",
                    settlement=missing_settlement,
                )
            try:
                if durable_reservation is not None:
                    # Plan §2.3.2 on resume: the reservation this run made before its first
                    # dispatch is restored verbatim — never recomputed from today's price or cap.
                    reservation = durable_reservation.reservation()
                else:
                    reservation = reserve_usd(
                        planned=planned,
                        unit_usd_e6=app_config.audd_usd_e6_per_request,
                        recipe_max_usd_e2=requested_recipe.max_usd_e2,
                        configured_max_usd_e2=app_config.max_usd_e2,
                    )
            except BudgetExhausted as exc:
                # A refusal must still report what this run already spent — the exact µUSD its own
                # attempt events recorded, never units × today's price.
                refused_settlement = _settle()
                entry = timer.entry(
                    status="budget_exhausted",
                    reason="reservation_exceeds_cap",
                    exit_code=4,
                    counts=counts,
                    costs={"usd_e2": refused_settlement.usd_e2_spent},
                    source_ids=source_ids,
                    ffmpeg_version=ffmpeg_version,
                    **_money_journal_fields(refused_settlement, requested_recipe, app_config),
                )
                _report(progress, "recognise", 0, planned, str(exc))
                _append_settlement(media_dir / "invocations.jsonl", entry)
                return _finish(
                    exit_code=4,
                    status="budget_exhausted",
                    reason="reservation_exceeds_cap",
                    settlement=refused_settlement,
                )
            # Durable BEFORE any dispatch (§2.3.2): a crash from here on resumes against exactly
            # this reservation, and an existing record for the run always wins.
            durable_reservation = journal.record_reservation(reservation)
            journal.unit_usd_e6 = durable_reservation.unit_usd_e6
            # Charge what this run already spent — the ledger's exact µUSD — against the ORIGINAL
            # reservation before anything new is admitted, an injected admitter included. A
            # reservation that cannot even cover it means no further paid request may be made.
            usd_admitter, covered = restore_admitter(
                durable_reservation.reservation(), recovered.usd_e6_spent, usd_admitter
            )
            restored_over_cap = not covered

        timer.start_stage("recognise_ms")
        # The latest per-window tick, so a log line in the same phase repeats the real
        # done/total instead of resetting the web app's ETA to "1 window" (review H4).
        recognise_progress = [0, 1]

        def _on_recognise_window(done: int, total: int) -> None:
            recognise_progress[:] = [done, total]
            _report(progress, "recognise", done, total, "recognising windows")

        def _recognise_log(message: str) -> None:
            _report(progress, "recognise", recognise_progress[0], recognise_progress[1], message)

        # A same-run resume never re-sends a Shazam answer THIS run already received: its cached
        # no_match is this run's own evidence, not a stale entry to refresh (4a-i retro P1).
        # Evidence of an earlier pass of THIS run, not merely any checkpoint: queue intake writes
        # `ingest`/`decode` before a run's first pass, and a genuinely fresh run must still refresh
        # another run's cached no_match as configured (second-model review P1).
        same_run_evidence = (
            recovered.any
            or journal.run_ledger().any
            or bool(set(completed_phases) - {"ingest", "decode"})
            or os.path.isdir(
                native_path(
                    media_dir
                    / "recognise"
                    / "invocations"
                    / sha256(run_id.encode("utf-8")).hexdigest()[:20]
                )
            )
        )
        recognise_refresh_states = frozenset() if same_run_evidence else refresh_states
        # ONE Shazam attempt identity for this run: the journal's own events (deterministic ids,
        # the clip query id), projected into SQLite by the hosted journal. Recognise consumes the
        # whole fold -- answered, refused and unresolved -- so no lost store can cause a resend.
        shazam_journal = journal.for_provider("shazam")

        async def recognise_windows(
            *, windows: object, generation: int, run_label: str | None = None
        ) -> object:
            # ``run_label`` keys a further Shazam pass of the same generation (the secondary's
            # confirmations) to its own invocation directory: the recognition artefacts are
            # immutable per invocation and generation, so a second pass over new windows must
            # not try to rewrite the first pass's files.
            return await recognise_generation(
                media_key=ingested.record.media_key,
                media_dir=media_dir,
                windows=windows,  # type: ignore[arg-type]
                project_root=pipeline_project_root,
                run_id=run_id if run_label is None else f"{run_id}:{run_label}",
                generation=generation,
                refresh=refresh,
                max_requests=max_requests,
                requests_per_minute=app_config.shazam_requests_per_minute,
                concurrency=app_config.recognise_concurrency,
                positive_max_age_seconds=app_config.cache_positive_max_age_seconds,
                no_match_max_age_seconds=app_config.cache_no_match_max_age_seconds,
                on_window=_on_recognise_window if progress is not None else None,
                http_client=shazam_http_client,
                process_breaker=shazam_breaker,
                running_free=achieved_recipe.name == "free",
                refresh_states=recognise_refresh_states,
                attempt_journal=shazam_journal,
            )

        # Which engine identifies the WHOLE mix first (generation 0).  Free = the rate-limited free
        # engine over every frozen window.  Deep = the paid engine, which is ~4-6x faster and has
        # no rate limit, so it does the bulk and the free engine runs only as the bounded second
        # opinion of plan §2.3.4 step 4.
        paid_first = requested_recipe.primary_engine == "audd" and "audd" in enabled_engines
        achieved_recipe = requested_recipe
        degrade_reason: str | None = None
        primary_state = (checkpoint_state or {}).get("primary", {})
        if "primary" in completed_phases:
            # Plan §2.3.1: the achieved recipe is part of the primary's durable state.  Without
            # restoring the substitution a degraded Free pass would resume down the paid branch
            # and load Free observations as AudD primary evidence.
            restored_achieved = str(primary_state.get("achieved") or achieved_recipe.name)
            if restored_achieved != achieved_recipe.name:
                achieved_recipe = get_recipe(restored_achieved)
            if primary_state.get("degrade_reason"):
                degrade_reason = str(primary_state["degrade_reason"])
            paid_first = bool(primary_state.get("paid_first", paid_first))
            if not paid_first:
                enabled_engines = tuple(
                    engine for engine in enabled_engines if engine not in _PAID_ENGINES
                )
            primary_planned = int(primary_state.get("primary_planned", primary_planned))
        primary_clip = PaidScanResult()
        primary_resolved = 0
        gen0_observations: tuple[object, ...] = ()
        gen0_observations_path: Path | None = None
        gen0_requests = 0
        gen0_physical = 0
        free_failures = 0
        if "primary" in completed_phases:
            # A completed primary is restored from its immutable observation file and the counts
            # checkpointed beside it -- for BOTH primaries.  The Free primary used to re-run every
            # Shazam window here, which is the whole cost of the phase; and a restart with the
            # breaker open would then refuse and degrade a run whose evidence is already on disk.
            observation_path = _checkpointed_path(media_dir, primary_state, "observation_path")
            gen0_observations = _read_observations(observation_path)
            gen0_observations_path = observation_path
            primary_resolved = int(primary_state.get("resolved", len(gen0_observations)))
            counts["matches"] = sum(item.status == "match" for item in gen0_observations)
            if paid_first:
                primary_clip = PaidScanResult(
                    observations=gen0_observations,
                    observation_paths=(observation_path,),
                    engines_run=("audd",),
                    resolved=primary_resolved,
                    failures=int(primary_state.get("failures", 0)),
                    cache_hits=int(primary_state.get("cache_hits", len(gen0_observations))),
                    billable_units=int(primary_state.get("billable_units", 0)),
                    reservation_exhausted=bool(primary_state.get("reservation_exhausted", False)),
                    provider_stopped=(
                        str(primary_state["provider_stopped"])
                        if primary_state.get("provider_stopped") is not None
                        else None
                    ),
                    attempts=int(primary_state.get("attempts", 0)),
                )
                counts.update(
                    {
                        "paid_requests": 0,
                        "paid_attempts": primary_clip.attempts,
                        "paid_resolved": primary_clip.resolved,
                        "paid_failures": primary_clip.failures,
                        "paid_cache_hits": primary_clip.cache_hits,
                        "paid_billable_units": primary_clip.billable_units,
                        "paid_resumed_ambiguous": 0,
                        "paid_resumed_reissued": 0,
                    }
                )
            else:
                gen0_requests = int(primary_state.get("requests", 0))
                gen0_physical = int(primary_state.get("physical_attempts", 0))
                free_failures = int(primary_state.get("failures", 0))
                counts.update(
                    {
                        "requests": gen0_requests,
                        "physical_attempts": gen0_physical,
                        "failures": free_failures,
                        "cache_hits": int(primary_state.get("cache_hits", 0)),
                    }
                )
                # Reused Free evidence replaces all new Shazam allocation; a restored one likewise.
                max_generations = 0
        elif paid_first:
            if restored_over_cap:
                # The recovered spend already fills the reservation. The sweep still runs: it
                # reuses every clip this run resolved (so the primary has its evidence file) and
                # its exhausted admitter refuses any new dispatch, ending `partial`. Skipping it
                # left no observation file and fusion crashed.
                _recognise_log("audd primary resumed with its reservation already spent")
            audd_retry = requested_recipe.retry_policy.get("audd")
            primary_clip = await run_paid_clip_recognition(
                media_key=ingested.record.media_key,
                media_dir=media_dir,
                windows=windows,
                targets=((0, duration_ms),),
                run_id=run_id,
                app_config=app_config,
                enabled_engines=enabled_engines,
                cli_confirmation=cli_confirmation,
                refresh=refresh,
                refresh_states=refresh_states,
                max_clips=len(windows.records) + 1,  # the whole mix, no per-mix cap
                primary_density=requested_recipe.primary_density,
                usd_admitter=usd_admitter,
                adapters=paid_scan_adapters,
                log=_recognise_log,
                # Plan §2.3.1: the recipe fixes the concurrency, the bounded retry policy and
                # the anchor validity bounds; the token-bucket ceiling is a config knob.
                concurrency=requested_recipe.audd_concurrency or 1,
                retry_policy=audd_retry,
                anchor_max_ms=requested_recipe.anchor_max_ms or DEFAULT_ANCHOR_MAX_MS,
                anchor_slack_ms=requested_recipe.anchor_slack_ms or DEFAULT_ANCHOR_SLACK_MS,
                cancel_token=cancel_token,
                on_window=_on_recognise_window if progress is not None else None,
                sleep=paid_sleep,
                attempt_journal=journal,
            )
            counts.update(
                {
                    "paid_requests": primary_clip.requests,
                    "paid_attempts": primary_clip.attempts,
                    "paid_resolved": primary_clip.resolved,
                    "paid_failures": primary_clip.failures,
                    "paid_cache_hits": primary_clip.cache_hits,
                    "paid_billable_units": primary_clip.billable_units,
                    "paid_resumed_ambiguous": primary_clip.resumed_ambiguous,
                    "paid_resumed_reissued": primary_clip.resumed_reissued,
                }
            )
            if primary_clip.cancelled:
                # The cancel token fired (or the progress hook raised) inside the sweep; the
                # clips in flight resolved first, so the journal below carries their spend.
                raise asyncio.CancelledError("paid sweep cancelled")
            if primary_clip.resolved == 0 and (
                not primary_clip.ran or primary_clip.provider_stopped or primary_clip.unreachable
            ):
                # Plan §2.3.5 row 1: the provider refused the credential, ran out of quota, was
                # never configured or could not be reached before a single resolved attempt.  A
                # Deep request never silently turns into a Free one: it stops here (exit 3) unless
                # the local caller passed --allow-degrade, which restarts it as the Free recipe
                # before any paid work — requested stays deep, achieved becomes free.
                reason = primary_clip.provider_stopped or next(
                    (item for item in primary_clip.outcomes if item in UNREACHABLE_OUTCOMES),
                    "not_configured",
                )
                # "Before any paid work" is literal: an ambiguous-but-billable outcome
                # (http_5xx / malformed / timeout_post) charges a unit without resolving
                # anything, so once one has been billed the request may no longer be restarted
                # as the Free recipe — that would report `degraded` (settled at 100 %, servable
                # with accept_degraded) on top of money already spent.  Stop with the true spend.
                # Cumulative, not this pass's: a resumed run whose earlier pass was billed may
                # not restart as Free either (4a-i retro P1).
                already_billed = bool(primary_clip.billable_units) or (
                    usd_admitter is not None and usd_admitter.usd_e6_spent > 0
                )
                if allow_degrade and already_billed:
                    _report(
                        progress,
                        "recognise",
                        0,
                        1,
                        f"--allow-degrade not applied: {primary_clip.billable_units} paid "
                        f"request(s) were already billed before {reason}",
                    )
                if not allow_degrade or already_billed:
                    settlement = _settle()
                    entry = timer.entry(
                        status="provider_unavailable",
                        reason=reason,
                        exit_code=3,
                        counts=counts,
                        costs={"usd_e2": settlement.usd_e2_spent},
                        source_ids=source_ids,
                        ffmpeg_version=ffmpeg_version,
                        **_money_journal_fields(settlement, requested_recipe, app_config),
                    )
                    _append_settlement(media_dir / "invocations.jsonl", entry)
                    return _finish(
                        exit_code=3,
                        status="provider_unavailable",
                        reason=reason,
                        settlement=settlement,
                    )
                _report(
                    progress,
                    "recognise",
                    0,
                    1,
                    f"paid engine unavailable ({reason}); restarting as the free recipe",
                )
                achieved_recipe = get_recipe("free")
                if refuse_free():
                    return 6
                degrade_reason = "provider_unavailable"
                paid_first = False
                primary_clip = PaidScanResult()
                primary_planned = frozen_count
                enabled_engines = tuple(
                    engine for engine in enabled_engines if engine not in _PAID_ENGINES
                )
            else:
                gen0_observations = primary_clip.observations
                gen0_observations_path = primary_clip.observation_paths[0]
                primary_resolved = primary_clip.resolved
                counts["matches"] = sum(
                    item.status == "match" for item in primary_clip.observations
                )
        if not paid_first and "primary" not in completed_phases:
            recognised = await recognise_windows(windows=windows, generation=0)
            gen0_observations = recognised.observations
            gen0_observations_path = recognised.observations_path
            gen0_requests = recognised.requests
            gen0_physical = recognised.physical_attempts
            free_failures = recognised.failures
            counts.update(
                {
                    "requests": recognised.requests,
                    "physical_attempts": recognised.physical_attempts,
                    "matches": sum(item.status == "match" for item in recognised.observations),
                    "failures": recognised.failures,
                    "cache_hits": recognised.cache_hits,
                }
            )
            # A frozen window is resolved by a match or a no-match; an error observation is not.
            primary_resolved = sum(
                item.generation == 0 and item.transform.type == "none" and item.status != "error"
                for item in recognised.observations
            )
            # Past its 429s Shazam's throttle looks like empty or non-JSON bodies; each leaves a
            # planned window with no answer, and too few answers end the run `partial` (review
            # S1).  The count is over the planned frozen windows the status rule itself uses --
            # `recognised.failures` also counts transform siblings, so under
            # `[transforms] policy = "global"` it can exceed the window count and read as
            # nonsense ("15 of 7 windows").  The verdict is this run's, not a general rule.
            unanswered = primary_planned - primary_resolved
            if unanswered > 0:
                needed = round(achieved_recipe.primary_achieved_fraction * 100)
                verdict = (
                    f"still at or above the {needed} % this recipe needs"
                    if _achieved(
                        primary_resolved,
                        primary_planned,
                        achieved_recipe.primary_achieved_fraction,
                    )
                    else f"below the {needed} % this recipe needs, so the run ends partial"
                )
                _recognise_log(
                    f"{unanswered} of {primary_planned} windows got no usable answer from "
                    f"Shazam (throttled or malformed reply): {primary_resolved} of "
                    f"{primary_planned} resolved, {verdict}"
                )
        if "primary" not in completed_phases:
            _checkpoint(
                "primary",
                (gen0_observations_path,) if gen0_observations_path is not None else (),
                _primary_state(
                    media_dir=media_dir,
                    observation_path=gen0_observations_path,
                    paid_first=paid_first,
                    achieved_recipe=achieved_recipe,
                    degrade_reason=degrade_reason,
                    primary_planned=primary_planned,
                    primary_resolved=primary_resolved,
                    primary_clip=primary_clip,
                    free_failures=free_failures,
                    counts=counts,
                    usd_admitter=usd_admitter,
                ),
            )
        _checkpoint(
            "hints",
            (hint_result.hints_path,) if hint_result is not None else (),
        )
        matches = [item for item in gen0_observations if item.status == "match"]
        timer.finish_stage("recognise_ms")

        _report(progress, "fuse", 0, 1, "fusing episodes")
        timer.start_stage("fuse_ms")
        # Novelty change points only ever feed rescan triggers, so they are computed here once
        # for every fuse of this run, and not at all with rescans off — the full log-mel pass
        # over the PCM (~0.7 GB per hour of mix) was paid on every re-fuse for nothing before
        # (review M2).  ``max_generations`` is the config-capped value the caller resolved.
        # Computed BEFORE ``_fuse`` closes over it: the closure reads the name at call time, so
        # defining it afterwards would make any future reordering a NameError at run time.
        novelty_points = compute_novelty_change_points(
            decoded, enabled=novelty and max_generations > 0
        )

        async def _fuse(
            extra_observations: tuple[object, ...],
            extra_observation_paths: tuple[Path, ...],
        ) -> object:
            return await run_generation_loop(
                media_key=ingested.record.media_key,
                media_dir=media_dir,
                decoded=decoded,
                windows=windows,
                observations=gen0_observations,
                observations_path=gen0_observations_path,
                recognise=recognise_windows,
                app_config=app_config,
                extra_observations=extra_observations,
                extra_observation_paths=extra_observation_paths,
                hints=hint_result.hints if hint_result is not None else (),
                hints_path=hint_result.hints_path if hint_result is not None else None,
                max_generations=max_generations,
                request_budget=max_requests,
                novelty_enabled=novelty,
                novelty_change_points_ms=novelty_points,
                # Only the windows an engine actually answered for count as scanned (review
                # M10): under Deep the AudD sweep may skip every other window (density 2) and
                # the Shazam secondary probes a handful, so the coverage figures and the gap
                # evidence must not describe a free-engine pass over the whole mix that never
                # happened.
                scanned_windows=scanned_windows(
                    windows.records, (*gen0_observations, *extra_observations)
                ),
                gen0_requests=gen0_requests,
                gen0_physical_attempts=gen0_physical,
                calibrator=calibrator,
                # The recipe's corroboration thresholds (plan §2.3.4 step 5); the Free recipe
                # defines neither (``None``) and takes the fuser's defaults, which are the Deep
                # values.  Only ``None`` falls back — a recipe that deliberately sets ``0`` means
                # "no floor", not "use the default".
                overlap_min_ms=(
                    CORROBORATION_OVERLAP_MIN_MS
                    if requested_recipe.overlap_min_ms is None
                    else requested_recipe.overlap_min_ms
                ),
                separation_min_ms=(
                    CORROBORATION_SEPARATION_MIN_MS
                    if requested_recipe.separation_min_ms is None
                    else requested_recipe.separation_min_ms
                ),
            )

        orchestrated = await _fuse((), ())
        fused = orchestrated.fusion
        counts.update(
            {
                "requests": orchestrated.requests,
                "physical_attempts": orchestrated.physical_attempts,
                "generations": orchestrated.final_generation + 1,
                "novelty_change_points": len(orchestrated.novelty_change_points_ms),
            }
        )
        timer.finish_stage("fuse_ms")
        _report(progress, "fuse", 1, 1, f"{len(fused.episodes.episodes)} episodes")
        _checkpoint("fuse1", (fused.final_path, fused.identities_path))

        # Phase 2 — the second opinion.  The first fuse just said where the primary is weak; each
        # lever below sends only window clips (the same ~12 s clips the engines already see — no
        # whole-file upload, no consent gate) and re-fuses, so agreement lifts confidence,
        # disagreement lets a phantom be demoted, and a primary-blind track is recovered:
        #   • Deep: the Shazam secondary — the ``targeting:1`` scheduler spends ``C − R`` clips
        #     over the hint-only, not-confident, challengeable-suppressed and blank spans, keeps
        #     the reserve ``R`` to confirm a new identity a blank probe turns up, and hands any
        #     unused reserve back to the spans (plan §2.3.4 step 4);
        #   • a local Panako index — the DJ's OWN unreleased uploads, in no public catalogue —
        #     over whatever is still uncertain afterwards.
        # The paid clip lever no longer runs here: the Deep primary already swept the whole mix
        # and the Free recipe's cap forbids every paid call.
        index_scan = PaidScanResult()
        secondary_scan = PaidScanResult()
        secondary_blocked: str | None = None
        secondary_allocated = 0
        secondary_resolved = 0
        supplemental_requests = 0
        supplemental_physical_attempts = 0
        want_index = local_index_label is not None
        resume_secondary = "secondary" in completed_phases
        secondary_state = (checkpoint_state or {}).get("secondary", {})

        async def _refuse_with(*scans: PaidScanResult) -> None:
            nonlocal fused, orchestrated
            _report(progress, "fuse", 0, 1, "re-fusing with cross-check evidence")
            timer.start_stage("refuse_ms")
            extra_obs = tuple(o for s in scans for o in s.observations)
            extra_paths = tuple(p for s in scans for p in s.observation_paths)
            orchestrated = await _fuse(extra_obs, extra_paths)
            fused = orchestrated.fusion
            counts.update(
                {
                    # Re-fusion recomputes the generation-loop totals from generation zero. Keep
                    # the separately-run secondary work instead of overwriting it with zero.
                    "requests": orchestrated.requests + supplemental_requests,
                    "physical_attempts": (
                        orchestrated.physical_attempts + supplemental_physical_attempts
                    ),
                    "generations": orchestrated.final_generation + 1,
                }
            )
            timer.finish_stage("refuse_ms")
            _report(progress, "fuse", 1, 1, f"{len(fused.episodes.episodes)} episodes")

        if resume_secondary:
            # The second opinion is already durable.  Re-probing it would spend the free
            # engine's allocation twice, and a restart while the breaker is open would discard
            # a COMPLETED secondary's evidence and report `degraded` for a run that is not.
            secondary_paths = _checkpointed_paths(
                media_dir, secondary_state.get("secondary_paths", ())
            )
            index_paths = _checkpointed_paths(media_dir, secondary_state.get("index_paths", ()))
            secondary_scan = PaidScanResult(
                observations=_read_observations(*secondary_paths),
                observation_paths=secondary_paths,
            )
            index_scan = PaidScanResult(
                observations=_read_observations(*index_paths),
                observation_paths=index_paths,
            )
            secondary_allocated = int(secondary_state.get("allocated", 0))
            secondary_resolved = int(secondary_state.get("resolved", 0))
            secondary_blocked = (
                str(secondary_state["blocked"]) if secondary_state.get("blocked") else None
            )
            supplemental_requests = int(secondary_state.get("supplemental_requests", 0))
            supplemental_physical_attempts = int(
                secondary_state.get("supplemental_physical_attempts", 0)
            )
            restored_counts = secondary_state.get("counts")
            if isinstance(restored_counts, dict):
                counts.update({str(key): int(value) for key, value in restored_counts.items()})
            if secondary_scan.observations or index_scan.observations:
                await _refuse_with(secondary_scan, index_scan)
        if not resume_secondary and paid_first and free_bundle is not None:
            secondary_scan = PaidScanResult(
                observations=reused_observations, observation_paths=(reused_path,)
            )
            counts["secondary_reused"] = len(reused_observations)
            counts["secondary_allocated"] = 0
            await _refuse_with(secondary_scan)

        if not resume_secondary and paid_first and free_bundle is None:
            secondary_blocked = "shazam_manual_off" if shazam_off() else shazam_breaker.reason()
            if secondary_blocked is not None and completed_phases:
                # The breaker is open on a same-run resume whose secondary never checkpointed.
                # What this run's probes already received is evidence: restore it from the run's
                # own raw answers BEFORE the refusal can discard it. No request is sent.
                restored = restore_run_answers(
                    media_key=ingested.record.media_key,
                    media_dir=media_dir,
                    windows=WindowsResult(
                        records=tuple(frozen_windows(windows.records)),
                        record_path=windows.record_path,
                        cached=windows.cached,
                    ),
                    project_root=pipeline_project_root,
                    run_labels=(run_id, f"{run_id}:secondary-2"),
                )
                if restored:
                    restored_key = sha256(f"{run_id}:secondary-restored".encode()).hexdigest()[:20]
                    restored_path = (
                        media_dir
                        / "recognise"
                        / "invocations"
                        / restored_key
                        / "observations.gen0.jsonl"
                    )
                    _write_jsonl(restored_path, list(restored))
                    secondary_scan = PaidScanResult(
                        observations=restored, observation_paths=(restored_path,)
                    )
                    secondary_resolved = len(restored)
                    secondary_allocated = len(restored)
                    counts["secondary_restored"] = len(restored)
                    counts["secondary_resolved"] = len(restored)
                    await _refuse_with(secondary_scan)
        if (
            not resume_secondary
            and paid_first
            and free_bundle is None
            and secondary_blocked is None
        ):
            hint_ids = (
                frozenset(hint.id for hint in hint_result.hints)
                if hint_result is not None
                else frozenset()
            )
            min_intersection_ms = requested_recipe.eligibility_min_intersection_ms or 0
            candidates = select_secondary_candidates(
                fused.episodes,
                duration_ms=duration_ms,
                suppressed_min_votes=requested_recipe.suppressed_min_votes or 0,
                min_intersection_ms=min_intersection_ms,
                hint_ids=hint_ids,
            )
            capacity = secondary_capacity(
                duration_ms, requested_recipe.secondary_clips_per_minute or 0
            )
            reserve = secondary_reserve(
                capacity, requested_recipe.secondary_reserve_fraction or 0.0
            )
            energy = energy_reader(media_dir)
            picks: list[SecondaryPick] = list(
                allocate_secondary_windows(
                    windows.records,
                    candidates,
                    allocation=capacity - reserve,
                    min_intersection_ms=min_intersection_ms,
                    energy=energy,
                )
            )
            picked_ids = {pick.window.id for pick in picks}
            counts["secondary_capacity"] = capacity
            counts["secondary_reserve"] = reserve
            counts["secondary_allocated"] = len(picks)
            secondary_passes: list[object] = []

            async def _probe(batch: Sequence[SecondaryPick], label: str, run_label: str | None):
                _report(progress, "scan", 0, len(batch), label)
                result = await recognise_windows(
                    windows=WindowsResult(
                        records=[pick.window for pick in batch],
                        record_path=windows.record_path,
                        cached=windows.cached,
                    ),
                    generation=0,
                    run_label=run_label,
                )
                nonlocal secondary_blocked
                secondary_blocked = secondary_blocked or result.blocked_reason
                secondary_passes.append(result)
                return result

            if picks:
                timer.start_stage("secondary_ms")
                first_pass = await _probe(picks, "second opinion (free engine)", None)
                # The reserve: a blank probe that names a track no listed episode carries gets
                # two confirmation clips around it (first-come until R is gone); whatever is
                # left of R goes back to the spans proportionally.  One further Shazam pass.
                discoveries = new_identity_discoveries(
                    first_pass.observations,
                    picks,
                    listed_text_keys(fused.episodes, fused.identities.record),
                )
                served, unused_reserve = serve_confirmations(
                    windows.records,
                    [
                        (item.observation.support_ms, item.pick.window.id, item.pick.candidate.span)
                        for item in discoveries
                    ],
                    reserve=reserve,
                    picked=picked_ids,
                    search_ms=requested_recipe.reserve_search_ms or 0,
                    min_separation_ms=requested_recipe.reserve_min_separation_ms or 0,
                    min_intersection_ms=min_intersection_ms,
                )
                follow_up: list[SecondaryPick] = [
                    SecondaryPick(window, item.pick.candidate, "confirmation")
                    for item, chosen in zip(discoveries, served, strict=True)
                    for window in chosen
                ]
                picked_ids.update(pick.window.id for pick in follow_up)
                follow_up.extend(
                    distribute_secondary_windows(
                        windows.records,
                        candidates,
                        quota=unused_reserve,
                        min_intersection_ms=min_intersection_ms,
                        energy=energy,
                        picked=picked_ids,
                    )
                )
                counts["secondary_discoveries"] = len(discoveries)
                counts["secondary_confirmed"] = sum(1 for chosen in served if chosen)
                counts["secondary_uncorroborated"] = sum(1 for chosen in served if not chosen)
                counts["secondary_confirmation_clips"] = sum(len(chosen) for chosen in served)
                if discoveries:
                    _recognise_log(
                        f"{len(discoveries)} new track(s) found in blank stretches; "
                        f"{counts['secondary_confirmed']} confirmed from the reserve, "
                        f"{counts['secondary_uncorroborated']} listed uncorroborated"
                    )
                if follow_up:
                    picks.extend(follow_up)
                    counts["secondary_allocated"] = len(picks)
                    await _probe(follow_up, "confirming new finds (free engine)", "secondary-2")
                timer.finish_stage("secondary_ms")
                secondary_allocated = len(picks)
                secondary_failures = sum(item.failures for item in secondary_passes)
                secondary_resolved = secondary_allocated - secondary_failures
                counts["secondary_resolved"] = secondary_resolved
                if secondary_failures:
                    _recognise_log(
                        f"{secondary_failures} of {secondary_allocated} second-opinion windows "
                        "got no usable answer from Shazam (throttled or malformed reply)"
                    )
                secondary_observations = tuple(
                    observation for item in secondary_passes for observation in item.observations
                )
                counts["secondary_matches"] = sum(
                    item.status == "match" for item in secondary_observations
                )
                supplemental_requests += sum(item.requests for item in secondary_passes)
                supplemental_physical_attempts += sum(
                    item.physical_attempts for item in secondary_passes
                )
                counts["requests"] = orchestrated.requests + supplemental_requests
                counts["physical_attempts"] = (
                    orchestrated.physical_attempts + supplemental_physical_attempts
                )
                counts["failures"] = free_failures + secondary_failures
                counts["cache_hits"] += sum(item.cache_hits for item in secondary_passes)
                if secondary_observations:
                    secondary_scan = PaidScanResult(
                        observations=secondary_observations,
                        observation_paths=tuple(
                            item.observations_path for item in secondary_passes
                        ),
                    )
                    await _refuse_with(secondary_scan)  # re-fuse once (§2.3.4 step 5)
        if want_index and not resume_secondary:
            # Panako can still recover the DJ's own unreleased edits in the still-uncertain spans.
            idx_targets = select_scan_targets(fused.episodes.episodes, duration_ms)
            if idx_targets:
                _report(progress, "scan", 0, 1, "querying the local reference index")
                timer.start_stage("index_scan_ms")
                index_scan = await run_local_index_recognition(
                    media_key=ingested.record.media_key,
                    media_dir=media_dir,
                    windows=windows,
                    targets=idx_targets,
                    duration_ms=duration_ms,
                    run_id=run_id,
                    index_label=local_index_label,
                    index_root=index_root,
                    tool_dir=panako_tool_dir,
                    log=lambda message: _report(progress, "scan", 0, 1, message),
                )
                timer.finish_stage("index_scan_ms")
                for name, reason in index_scan.skipped:
                    _report(progress, "scan", 1, 1, f"{name} skipped: {reason}")
                counts["local_index_matches"] = sum(
                    item.status == "match" for item in index_scan.observations
                )
                if any(item.status == "match" for item in index_scan.observations):
                    await _refuse_with(secondary_scan, index_scan)

        secondary_artefacts = tuple(
            path for path in secondary_scan.observation_paths if path is not None
        ) + tuple(path for path in index_scan.observation_paths if path is not None)
        _checkpoint(
            "secondary",
            secondary_artefacts,
            _secondary_state(
                media_dir=media_dir,
                secondary_scan=secondary_scan,
                index_scan=index_scan,
                secondary_allocated=secondary_allocated,
                secondary_resolved=secondary_resolved,
                secondary_blocked=secondary_blocked,
                supplemental_requests=supplemental_requests,
                supplemental_physical_attempts=supplemental_physical_attempts,
                counts=counts,
            ),
        )
        _checkpoint("fuse2", (fused.final_path, fused.identities_path))

        status, reason = _run_status(
            primary_resolved=primary_resolved,
            primary_planned=primary_planned,
            primary_fraction=achieved_recipe.primary_achieved_fraction,
            secondary_resolved=secondary_resolved,
            secondary_allocated=secondary_allocated,
            secondary_fraction=achieved_recipe.secondary_achieved_fraction,
            provider_stopped=primary_clip.provider_stopped,
            reservation_exhausted=primary_clip.reservation_exhausted,
        )
        if secondary_blocked is not None and status in {"complete", "degraded"}:
            status, reason = "degraded", secondary_blocked
        if degrade_reason is not None and status == "complete":
            status, reason = "degraded", degrade_reason
        _report(progress, "present", 0, 1, "writing result page")
        timer.start_stage("export_ms")
        from id_detector.present.bundles import publish_result

        settlement = _settle()
        if achieved_recipe.name == "free":
            shazam_observations = [
                json.loads(line)
                for generation in orchestrated.generations
                for line in read_text(generation.observations_path).splitlines()
                if line.strip()
            ]
            atomic_write_json(media_dir / "fuse/shazam-observations.json", shazam_observations)
        bundle = publish_result(
            media_dir=media_dir,
            source=ingested.record,
            episodes=fused.episodes,
            identities=fused.identities.record,
            duration_ms=decoded.record.pcm.duration_ms,
            metadata={
                "analysis_key": effective_analysis_key,
                "compatibility": {
                    **request.metadata(achieved_recipe),
                    "analysis_key": effective_analysis_key,
                },
                "run_id": timer.run_id,
                "started_at": timer.started_at,
                "status": status,
                "reason": reason,
                "achieved": achieved_recipe.name,
                **_money_journal_fields(settlement, requested_recipe, app_config, achieved_recipe),
            },
            config=app_config,
            local=presentation_local,
        )
        timer.finish_stage("export_ms")
        _report(progress, "present", 1, 1, "result page ready")
        _checkpoint("present", (bundle / "manifest.json",))
        if print_raw:
            output = [
                {
                    "mix_time_ms": observation.mix_span_ms[0],
                    "mix_span_ms": list(observation.mix_span_ms),
                    "raw_label": observation.raw_label.model_dump(mode="json"),
                    "provider_ids": observation.provider_ids,
                    "matches": observation.native.get("matches", []),
                    "anchor": observation.anchor.model_dump(mode="json")
                    if observation.anchor
                    else None,
                }
                for observation in matches
            ]
            typer.echo(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
        else:
            outcome = status if reason is None else f"{status} ({reason})"
            typer.echo(
                f"{outcome}; {len(matches)} matches; {counts['failures']} failures; "
                f"{counts['physical_attempts']} physical attempts; "
                f"{orchestrated.final_generation + 1} generations "
                f"(stop={orchestrated.stop_reason}); "
                f"{len(fused.episodes.episodes)} episodes; tracklist={bundle / 'tracklist.json'}"
            )
        entry = timer.entry(
            status=status,
            reason=reason,
            achieved=achieved_recipe.name,
            exit_code=0,
            counts=counts,
            costs={"usd_e2": settlement.usd_e2_spent},
            source_ids=source_ids,
            ffmpeg_version=ffmpeg_version,
            **_money_journal_fields(settlement, requested_recipe, app_config, achieved_recipe),
        )
        entry = entry.model_copy(
            update={
                "bundle_id": bundle.name,
                "fuse_run": f"fuse/runs/{timer.run_id}",
                "analysis_key": effective_analysis_key,
                "compatibility": {
                    **request.metadata(achieved_recipe),
                    "analysis_key": effective_analysis_key,
                },
            }
        )
        _append_settlement(media_dir / "invocations.jsonl", entry)
        if result_paths is not None:
            result_paths.append(bundle)
        return _finish(
            exit_code=0,
            status=status,
            reason=reason,
            achieved=achieved_recipe.name,
            bundle=bundle,
            settlement=settlement,
        )
    except SourceChanged as exc:
        # Source identity is proven before any window is cut, so nothing can be reserved here yet;
        # settling the admitter regardless keeps the journal honest if that order ever moves.
        settlement = _settle()
        entry = timer.entry(
            status="source_changed",
            reason="media_key_mismatch",
            exit_code=5,
            counts=counts,
            costs={"usd_e2": settlement.usd_e2_spent},
            source_ids=source_ids,
            ffmpeg_version=ffmpeg_version,
            **_money_journal_fields(settlement, requested_recipe, app_config),
        )
        _append_settlement(exc.media_dir / "invocations.jsonl", entry)
        typer.echo("source_changed: source bytes no longer match the stored media", err=True)
        return _finish(
            exit_code=5,
            status="source_changed",
            reason="media_key_mismatch",
            settlement=settlement,
        )
    except asyncio.CancelledError:
        _finish(exit_code=130, status="cancelled", settlement=_settle())
        if media_dir is not None:
            settlement = _settle()
            entry = timer.entry(
                status="cancelled",
                reason=None,
                exit_code=130,
                counts=counts,
                costs={"usd_e2": settlement.usd_e2_spent},
                source_ids=source_ids,
                ffmpeg_version=ffmpeg_version,
                **_money_journal_fields(settlement, requested_recipe, app_config),
            )
            _append_settlement(media_dir / "invocations.jsonl", entry)
        raise
    except Exception:
        _finish(exit_code=1, status="failed", settlement=_settle())
        if media_dir is not None:
            settlement = _settle()
            entry = timer.entry(
                status="failed",
                reason=None,
                exit_code=1,
                counts=counts,
                costs={"usd_e2": settlement.usd_e2_spent},
                source_ids=source_ids,
                ffmpeg_version=ffmpeg_version,
                **_money_journal_fields(settlement, requested_recipe, app_config),
            )
            _append_settlement(media_dir / "invocations.jsonl", entry)
        raise
    finally:
        if media_lock is not None:
            media_lock.release()
        if source_lock is not None:
            source_lock.release()

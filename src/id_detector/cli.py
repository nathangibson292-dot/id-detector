"""Command-line entry point."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tomllib
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Annotated

import typer

from id_detector.benchmark.ablations import engine_status_rows, run_ablations
from id_detector.benchmark.controlled import render_controlled
from id_detector.benchmark.corpus import run_corpus
from id_detector.benchmark.hints import run_hint_gate
from id_detector.benchmark.scorer import score_corpus
from id_detector.benchmark.shortlist import run_shortlist
from id_detector.benchmark.transforms_schedule import run_transform_schedule_benchmark
from id_detector.calibrate.certify import CorpusNotFrozen, DuplicateTestVersion, run_certify
from id_detector.calibrate.model import load_calibration
from id_detector.calibrate.validate import run_calibration_validation
from id_detector.calibration import calibrate_shazam
from id_detector.config_template import CONFIG_TEMPLATE, render_effective_config
from id_detector.contracts import SourceRecord
from id_detector.decode import decode
from id_detector.doctor import run_doctor
from id_detector.enrich.benchmark import build_link_sample, score_link_sample
from id_detector.enrich.run import enrich_media_dir
from id_detector.fuse.episodes import (  # noqa: F401  (fuse_generation_zero: public re-export)
    CORROBORATION_OVERLAP_MIN_MS,
    CORROBORATION_SEPARATION_MIN_MS,
    fuse_generation_zero,
)
from id_detector.hints.pipeline import run_hints
from id_detector.ingest import _load_cached, ingest
from id_detector.io import read_text, redact_text
from id_detector.jobs import AsyncJobStore, ProcessLock
from id_detector.journal import InvocationTimer, append_invocation
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
from id_detector.present import export_tracklist, generate_page
from id_detector.present.server import consume_rescan_queue, read_rescan_queue
from id_detector.process import run_process
from id_detector.profiles import (
    UnknownProfile,
    effective_app_config,
    freeze_profiles,
    load_profile,
    profile_fixed_fields,
)
from id_detector.providers.audd import DEFAULT_ANCHOR_MAX_MS, DEFAULT_ANCHOR_SLACK_MS
from id_detector.providers.base import AppConfig
from id_detector.recipes import Recipe, get_recipe
from id_detector.recognise import recognise_generation
from id_detector.rescan import DEFAULT_MAX_GENERATIONS
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
from id_detector.truth import (
    freeze_truth,
    resolve_truth,
    second_pass_truth,
    seed_truth,
    verify_truth,
    write_draft_manifest,
)
from id_detector.windows import (
    TransformGrid,
    WindowSchedule,
    WindowsResult,
    generate_windows_async,
)

#: A pipeline progress hook: ``(phase, done, total, message)``.  ``phase`` is one of ``ingest``,
#: ``decode``, ``windows``, ``recognise``, ``hints``, ``fuse``, ``enrich`` or ``present``.  It is
#: optional everywhere (default ``None``) so the CLI path stays byte-for-byte unchanged; only the
#: web app supplies one.  A progress hook may raise ``asyncio.CancelledError`` to abort a run.
ProgressFn = Callable[[str, int, int, str], None]


def _report(progress: ProgressFn | None, phase: str, done: int, total: int, message: str) -> None:
    if progress is not None:
        progress(phase, done, total, message)


app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
benchmark_app = typer.Typer(no_args_is_help=True)
truth_app = typer.Typer(no_args_is_help=True)
config_app = typer.Typer(no_args_is_help=True, help="Show or create the idea.toml config.")
app.add_typer(benchmark_app, name="benchmark")
app.add_typer(truth_app, name="truth")
app.add_typer(config_app, name="config")
PROJECT_ROOT = Path.cwd()
DEFAULT_WORK_ROOT = Path("work")
#: Every engine that can cost money; the Free recipe strips them all (its cap is $0).
_PAID_ENGINES = frozenset(PAID_FILE_SCANNERS) | frozenset(PAID_CLIP_ENGINES)


def _load_dotenv(root: Path) -> None:
    """Load ``KEY=VALUE`` lines from ``<root>/.env`` into the environment, never overriding a real
    variable already set in the shell.

    Secrets (``AUDD_API_TOKEN`` etc.) are read only from the environment; this makes the documented
    ``.env`` file work without adding a dependency.  A missing file and malformed lines are ignored.
    """

    try:
        text = (root / ".env").read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


@app.callback()
def main() -> None:
    """Evidence-first DJ-set identification."""

    _load_dotenv(PROJECT_ROOT)
    # Redirected Windows consoles commonly default to cp1252. Provider labels are Unicode and
    # must never make a completed analysis fail during its final presentation step.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


@app.command()
def doctor() -> None:
    """Check the runtime and offline signature-generation path."""
    raise typer.Exit(run_doctor())


@app.command("panako-setup")
def panako_setup_command(
    tool_dir: Path = typer.Option(  # noqa: B008
        Path("data/local/panako"), "--tool-dir", help="Where the pinned Panako jar + config live."
    ),
    index_root: Path = typer.Option(  # noqa: B008
        Path("data/local/panako-db"), "--index-root", help="Git-ignored root for index stores."
    ),
    offline: bool = typer.Option(
        False, "--offline", help="Verify and configure an already-present jar; never download."
    ),
) -> None:
    """Download+verify the pinned Panako jar, write its config, and confirm it starts."""

    from id_detector.providers.panako_setup import (
        PanakoSetupError,
        manual_instructions,
        run_setup,
    )

    try:
        result = asyncio.run(
            run_setup(tool_dir=tool_dir, index_root=index_root, allow_download=not offline)
        )
    except PanakoSetupError as exc:
        typer.echo(redact_text(str(exc)), err=True)
        typer.echo(manual_instructions(tool_dir), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"Panako jar: {result.jar} (sha256 {result.sha256})")
    typer.echo(f"config:     {result.config}")
    typer.echo(f"index root: {result.index_root}")
    typer.echo(f"JDK:        {result.java if result.java else 'not found — Panako cannot run'}")
    typer.echo(f"Panako starts: {result.help_first_line}")


@app.command("build-index")
def build_index_command(
    set_url: str | None = typer.Argument(  # noqa: B008
        None, help="Public set URL whose uploader's own uploads seed the candidate pool."
    ),
    uploader_url: str | None = typer.Option(  # noqa: B008
        None, "--uploader-url", help="Uploader uploads URL to list directly (skip set lookup)."
    ),
    extra_artist: list[str] | None = typer.Option(  # noqa: B008
        None, "--extra-artist", help="Search SoundCloud for this artist; repeatable."
    ),
    extra_url: list[str] | None = typer.Option(  # noqa: B008
        None, "--extra-url", help="Index this exact track URL (user-supplied); repeatable."
    ),
    file: list[Path] | None = typer.Option(  # noqa: B008
        None, "--file", help="Index this local audio file directly; repeatable."
    ),
    from_hints: bool = typer.Option(
        False, "--from-hints", help="Add artists parsed from --hints to the search set."
    ),
    hints: Path | None = typer.Option(  # noqa: B008
        None, "--hints", help="hints.jsonl artefact to read artist names from (with --from-hints)."
    ),
    index: bool = typer.Option(
        False, "--index", help="Confirm: download and fingerprint the discovered candidates."
    ),
    index_label: str = typer.Option(  # noqa: B008
        "default", "--index-label", help="Names the index; its id enters the local-index cache key."
    ),
    tool_dir: Path = typer.Option(  # noqa: B008
        Path("data/local/panako"), "--tool-dir"
    ),
    index_root: Path = typer.Option(  # noqa: B008
        Path("data/local/panako-db"), "--index-root"
    ),
) -> None:
    """Discover candidate reference tracks (emits links) and, only on --index, fingerprint them.

    Discovery never auto-rips: the default prints a candidate list. Audio is downloaded and
    indexed only for candidates you confirm with --index, or that you supply explicitly via
    --extra-url / --file. Downloaded audio is deleted after fingerprinting; only the DB is kept.
    """

    from id_detector.candidates import (
        Candidate,
        artists_from_hints,
        build_manifest,
        deduplicate_candidates,
        discover_candidates,
        format_candidate_list,
        index_candidates,
        write_manifest,
    )
    from id_detector.providers.panako import PanakoIndexPaths, PanakoProvider, PanakoRuntime
    from id_detector.providers.panako_setup import jar_path

    artists = list(extra_artist or [])
    if from_hints and hints is not None:
        artists.extend(artists_from_hints(hints))

    candidates = asyncio.run(
        discover_candidates(
            set_url=set_url,
            uploader_url=uploader_url,
            artists=artists,
            extra_urls=list(extra_url or []),
        )
    )
    typer.echo(format_candidate_list(candidates))

    explicit_files = list(file or [])
    if not index and not explicit_files:
        return  # links only; the owner must confirm before anything is downloaded or read

    try:
        runtime = PanakoRuntime.resolve(jar=jar_path(tool_dir))
    except Exception as exc:  # ProviderUnavailable and friends: a usage error, not a traceback
        typer.echo(redact_text(str(exc)), err=True)
        raise typer.Exit(1) from None

    index_dir = index_root / index_label
    provider = PanakoProvider(runtime=runtime, paths=PanakoIndexPaths(root=index_dir))
    to_index: list[Candidate] = list(candidates) if index else []
    for local in explicit_files:
        to_index.append(
            Candidate(
                url=local.resolve().as_uri(),
                title=local.stem,
                uploader=None,
                source="local_file",
            )
        )
    to_index = deduplicate_candidates(to_index)

    async def _run() -> list[object]:
        from id_detector.candidates import download_audio

        async def _local_or_download(candidate: Candidate, dest: Path) -> Path:
            if candidate.source == "local_file":
                import shutil

                source = Path(candidate.url.removeprefix("file:///"))
                if not source.is_file():  # file URIs on POSIX begin with a single slash
                    source = Path(candidate.url.removeprefix("file://"))
                target = dest / source.name
                shutil.copyfile(source, target)
                return target
            return await download_audio(candidate, dest)

        return await index_candidates(
            provider,
            to_index,
            download_dir=index_dir / "downloads",
            downloader=_local_or_download,
        )

    resources = asyncio.run(_run())
    manifest = build_manifest(index_label=index_label, resources=resources)  # type: ignore[arg-type]
    write_manifest(PanakoIndexPaths(root=index_dir).manifest_path, manifest)
    typer.echo(
        f"indexed {len(resources)} track(s) into {index_dir} "
        f"(index_id {manifest['index_id']}, index_version {manifest['index_version']})"
    )


@config_app.command("show")
def config_show(
    config: Path = typer.Option(  # noqa: B008
        Path("idea.toml"),
        "--config",
        help="TOML config to resolve (missing file is fine: built-in defaults are shown).",
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help=(
            "Show what a run under this frozen profile ('free' or 'max_accuracy') actually "
            "uses, marking the lines the profile fixes. Without it, the file's default_profile "
            "applies when set."
        ),
    ),
) -> None:
    """Print the effective, resolved configuration (file + profile + defaults). No secrets."""

    file_config = _load_app_config(config)
    selected = profile if profile is not None else file_config.default_profile
    if selected is None:
        source = (
            f"{config} + defaults"
            if config.is_file()
            else f"built-in defaults ({config} not found)"
        )
        typer.echo(f"# source: {source}")
        typer.echo(render_effective_config(file_config), nl=False)
        return
    frozen = _load_profile_or_exit(selected)
    chosen_by = "--profile" if profile is not None else f"default_profile in {config}"
    file_part = (
        f"{config} + defaults" if config.is_file() else f"built-in defaults ({config} not found)"
    )
    effective = effective_app_config(file_config, frozen)
    engines = ", ".join(frozen.enabled_engines) or "none"
    trailer = [
        f'# Profile "{frozen.name}" also fixes what no config line controls:',
        f"#   engines = {engines}  (--recipe deep adds the paid AudD sweep)",
        f"#   novelty change points = {'on' if frozen.novelty_enabled else 'off'}  "
        "(rescan triggers; computed only when max_generations > 0)",
        f"#   hints = {'on' if frozen.hints_enabled else 'off'}",
    ]
    typer.echo(f'# source: {file_part} + profile "{frozen.name}" (chosen by {chosen_by})')
    typer.echo(
        f'# Lines marked "fixed by profile" come from the frozen profile "{frozen.name}"; the '
        "file's value is ignored for those."
    )
    typer.echo("# Every other line is exactly what a run under this profile uses.")
    typer.echo(
        render_effective_config(
            effective,
            fixed_by=profile_fixed_fields(
                file_config,
                frozen,
                # Match the header: with no config file on disk the ceiling that caps the
                # profile's rescans is the built-in default, not "this file".
                capped_by="this file" if config.is_file() else "the built-in default",
            ),
            trailer=trailer,
        ),
        nl=False,
    )


@config_app.command("init")
def config_init(
    path: Path = typer.Option(  # noqa: B008
        Path("idea.toml"), "--path", help="Where to write the documented template."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file."),
) -> None:
    """Write the documented idea.toml template (never contains secrets)."""

    if path.exists() and not force:
        typer.echo(f"{path} already exists; pass --force to overwrite", err=True)
        raise typer.Exit(1)
    _validate_config_or_exit()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8", newline="\n")
    typer.echo(f"wrote {path}; edit it and keep it un-committed (it is git-ignored).")


def _validate_config_or_exit() -> None:
    """Fail fast if the packaged template ever stops parsing (guards against edit drift)."""

    import tempfile

    with tempfile.NamedTemporaryFile(
        "w", suffix=".toml", delete=False, encoding="utf-8", newline="\n"
    ) as handle:
        handle.write(CONFIG_TEMPLATE)
        temp_path = Path(handle.name)
    try:
        AppConfig.load(temp_path)
    finally:
        temp_path.unlink(missing_ok=True)


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


def _tracklist_run_fields(media_dir: Path) -> dict[str, str | None]:
    """The run outcome a previous export recorded, so a re-export (``acquire``) carries it on."""

    try:
        document = json.loads(read_text(media_dir / "present" / "tracklist.json"))
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict):
        return {}
    return {key: document.get(key) for key in ("status", "reason", "achieved") if key in document}


async def _analyse(
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
    paid_scan_adapters: Mapping[str, object] | None = None,
    shazam_http_client: HTTPClientInterface | None = None,
    local_index_label: str | None = None,
    index_root: Path = Path("data/local/panako-db"),
    panako_tool_dir: Path = Path("data/local/panako"),
    progress: ProgressFn | None = None,
    cancel_token: CancelToken | None = None,
    paid_sleep: SleepFn | None = None,
) -> int:
    """Run one analysis and return its exit code (plan §2.3.5).

    ``cancel_token`` is polled by the paid sweep before every dispatch (the web app passes its
    job's cancel event); when it fires the run ends ``cancelled`` once the clips in flight have
    resolved.  ``paid_sleep`` injects the AudD retry-backoff sleeper (tests pass a no-op).
    """

    app_config = app_config or AppConfig()
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
    run_id = uuid.uuid4().hex
    timer = InvocationTimer(run_id, ["analyse", url])
    media_dir: Path | None = None
    ffmpeg_version: str | None = None
    source_ids: list[str] = []
    source_lock: ProcessLock | None = None
    media_lock: ProcessLock | None = None
    usd_admitter: UsdAdmitter | None = None
    counts = {
        "requests": 0,
        "physical_attempts": 0,
        "matches": 0,
        "failures": 0,
        "cache_hits": 0,
    }
    try:
        lock_key = sha256(url.encode("utf-8")).hexdigest()
        source_lock = ProcessLock(work_root.resolve() / ".locks" / f"{lock_key}.lock")
        source_lock.acquire()
        _report(progress, "ingest", 0, 1, "resolving source")
        timer.start_stage("ingest_ms")
        ingested = await ingest(url, work_root)
        timer.finish_stage("ingest_ms")
        media_dir = ingested.media_dir
        acquired_media_lock = ProcessLock(media_dir / ".media.lock")
        acquired_media_lock.acquire()
        media_lock = acquired_media_lock
        source_ids = [f"source:{ingested.record.source_key}"]
        _report(progress, "ingest", 1, 1, ingested.record.title or "source ready")

        _report(progress, "decode", 0, 1, "decoding audio")
        timer.start_stage("decode_ms")
        decoded = await decode(ingested)
        timer.finish_stage("decode_ms")
        ffmpeg_version = decoded.record.decoder.ffmpeg_version
        duration_ms = decoded.record.pcm.duration_ms
        _report(progress, "decode", 1, 1, "audio decoded")

        _report(progress, "windows", 0, 1, "cutting windows")
        timer.start_stage("windows_ms")
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

        # The frozen generation-0 window set is what every recipe plans against (§2.3.1): the
        # Deep reservation, both primaries' achieved fractions and the secondary's eligibility.
        frozen_count = len(frozen_windows(windows.records))
        primary_planned = (
            frozen_count + requested_recipe.primary_density - 1
        ) // requested_recipe.primary_density
        if requested_recipe.name == "deep":
            planned = primary_planned
            counts["paid_planned"] = planned
            try:
                reservation = reserve_usd(
                    planned=planned,
                    unit_usd_e6=app_config.audd_usd_e6_per_request,
                    recipe_max_usd_e2=requested_recipe.max_usd_e2,
                    configured_max_usd_e2=app_config.max_usd_e2,
                )
            except BudgetExhausted as exc:
                entry = timer.entry(
                    status="budget_exhausted",
                    reason="reservation_exceeds_cap",
                    exit_code=4,
                    counts=counts,
                    costs={"usd_e2": 0},
                    source_ids=source_ids,
                    ffmpeg_version=ffmpeg_version,
                    **_money_journal_fields(_settle_money(None), requested_recipe, app_config),
                )
                _report(progress, "recognise", 0, planned, str(exc))
                append_invocation(media_dir / "invocations.jsonl", entry)
                return 4
            usd_admitter = UsdAdmitter(reservation)

        timer.start_stage("recognise_ms")
        # The latest per-window tick, so a log line in the same phase repeats the real
        # done/total instead of resetting the web app's ETA to "1 window" (review H4).
        recognise_progress = [0, 1]

        def _on_recognise_window(done: int, total: int) -> None:
            recognise_progress[:] = [done, total]
            _report(progress, "recognise", done, total, "recognising windows")

        def _recognise_log(message: str) -> None:
            _report(progress, "recognise", recognise_progress[0], recognise_progress[1], message)

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
                project_root=PROJECT_ROOT,
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
                refresh_states=refresh_states,
            )

        # Which engine identifies the WHOLE mix first (generation 0).  Free = the rate-limited free
        # engine over every frozen window.  Deep = the paid engine, which is ~4-6x faster and has
        # no rate limit, so it does the bulk and the free engine runs only as the bounded second
        # opinion of plan §2.3.4 step 4.
        paid_first = requested_recipe.primary_engine == "audd" and "audd" in enabled_engines
        achieved_recipe = requested_recipe
        degrade_reason: str | None = None
        primary_clip = PaidScanResult()
        primary_resolved = 0
        gen0_observations: tuple[object, ...] = ()
        gen0_observations_path: Path | None = None
        gen0_requests = 0
        gen0_physical = 0
        free_failures = 0
        if paid_first:
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
                if allow_degrade and primary_clip.billable_units:
                    _report(
                        progress,
                        "recognise",
                        0,
                        1,
                        f"--allow-degrade not applied: {primary_clip.billable_units} paid "
                        f"request(s) were already billed before {reason}",
                    )
                if not allow_degrade or primary_clip.billable_units:
                    settlement = _settle_money(usd_admitter)
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
                    append_invocation(media_dir / "invocations.jsonl", entry)
                    return 3
                _report(
                    progress,
                    "recognise",
                    0,
                    1,
                    f"paid engine unavailable ({reason}); restarting as the free recipe",
                )
                achieved_recipe = get_recipe("free")
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
        if not paid_first:
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
        matches = [item for item in gen0_observations if item.status == "match"]
        timer.finish_stage("recognise_ms")

        hint_result = None
        if not no_hints:
            _report(progress, "hints", 0, 1, "reading tracklist hints")
            timer.start_stage("hints_ms")
            hint_result = await run_hints(
                source=ingested.record,
                duration_ms=duration_ms,
                media_dir=media_dir,
                source_path=ingested.source_path,
                project_root=PROJECT_ROOT,
                manual_tracklist=tracklist,
                confirmed_mirrors=confirmed_mirrors,
                refresh=refresh,
                disabled_connectors=app_config.disabled_hint_connectors,
            )
            timer.finish_stage("hints_ms")
            counts["hints"] = len(hint_result.hints)
            _report(progress, "hints", 1, 1, f"{len(hint_result.hints)} hints")

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
        secondary_allocated = 0
        secondary_resolved = 0
        supplemental_requests = 0
        supplemental_physical_attempts = 0
        want_index = local_index_label is not None

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

        if paid_first:
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
        if want_index:
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
        if degrade_reason is not None and status == "complete":
            status, reason = "degraded", degrade_reason
        _report(progress, "present", 0, 1, "writing result page")
        timer.start_stage("export_ms")
        exported = export_tracklist(
            media_dir=media_dir,
            media_key=ingested.record.media_key,
            duration_ms=decoded.record.pcm.duration_ms,
            episodes=fused.episodes,
            identities=fused.identities.record,
            episodes_path=fused.final_path,
            identities_path=fused.identities_path,
            title=ingested.record.title,
            media_target=ingested.record.canonical_url,
            collapse=app_config.collapse,
            same_track_bridge_ms=app_config.same_track_bridge_ms,
            min_track_ms=app_config.present_min_track_ms,
            status=status,
            reason=reason,
            achieved=achieved_recipe.name,
        )
        generate_page(
            media_dir=media_dir,
            source=ingested.record,
            episodes=fused.episodes,
            identities=fused.identities.record,
            duration_ms=decoded.record.pcm.duration_ms,
            episodes_path=fused.final_path,
            identities_path=fused.identities_path,
            lead_in_ms=app_config.lead_in_ms,
            collapse=app_config.collapse,
            same_track_bridge_ms=app_config.same_track_bridge_ms,
            min_track_ms=app_config.present_min_track_ms,
        )
        timer.finish_stage("export_ms")
        _report(progress, "present", 1, 1, "result page ready")
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
                f"{len(fused.episodes.episodes)} episodes; tracklist={exported.json_path}"
            )
        settlement = _settle_money(usd_admitter)
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
        append_invocation(media_dir / "invocations.jsonl", entry)
        return 0
    except asyncio.CancelledError:
        if media_dir is not None:
            settlement = _settle_money(usd_admitter)
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
            append_invocation(media_dir / "invocations.jsonl", entry)
        raise
    except Exception:
        if media_dir is not None:
            settlement = _settle_money(usd_admitter)
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
            append_invocation(media_dir / "invocations.jsonl", entry)
        raise
    finally:
        if media_lock is not None:
            media_lock.release()
        if source_lock is not None:
            source_lock.release()


def _load_app_config(path: Path) -> AppConfig:
    """Load the non-secret TOML config, reporting a bad file as a usage error, not a traceback."""

    try:
        return AppConfig.load(path)
    except (ValueError, OSError, tomllib.TOMLDecodeError) as exc:
        typer.echo(f"invalid config {path}: {redact_text(str(exc))}", err=True)
        raise typer.Exit(2) from None


def _load_profile_or_exit(name: str):
    """Resolve a frozen profile by name; a non-frozen or unknown name is a usage error."""

    try:
        return load_profile(PROJECT_ROOT, name)
    except UnknownProfile as exc:
        typer.echo(redact_text(str(exc)), err=True)
        raise typer.Exit(2) from None


@app.command()
def analyse(
    url: str = typer.Argument(..., help="Public mix URL (or a local media file)."),
    raw: bool = typer.Option(False, "--raw", help="Print raw match tuples with mix times."),
    refresh: bool = typer.Option(False, "--refresh", help="Bypass positive/no-match TTLs."),
    refresh_states: str = typer.Option(
        "no_match",
        "--refresh-states",
        help="Comma-separated cached provider states to re-query (match and/or no_match).",
    ),
    work_root: Path = typer.Option(DEFAULT_WORK_ROOT, "--work-root"),  # noqa: B008
    max_requests: int = typer.Option(
        -1,
        "--max-requests",
        min=-1,
        help="Per-run Shazam request ceiling; -1 uses max_requests from config (default 2000).",
    ),
    tracklist: Path | None = typer.Option(  # noqa: B008
        None, "--tracklist", help="Manual UTF-8 tracklist."
    ),
    no_hints: bool = typer.Option(False, "--no-hints", help="Disable all hint connectors."),
    config: Path = typer.Option(  # noqa: B008
        Path("idea.toml"), "--config", help="Non-secret schedule/transform TOML config."
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help=(
            "Select a frozen profile ('free' or 'max_accuracy'). It fixes the engines, "
            "transform/schedule/rescan geometry and the hint/novelty toggles; a name that is not "
            "a frozen artefact is rejected. Overrides --config's schedule/transform tables."
        ),
    ),
    recipe: str | None = typer.Option(
        None,
        "--recipe",
        help=(
            "Select the scan recipe ('free' or 'deep'). Defaults to free; only the legacy "
            "'--profile max_accuracy --engine audd' pair still defaults to deep. A profile alone "
            "never selects paid work: '--profile max_accuracy' without --recipe is the free "
            "recipe."
        ),
    ),
    allow_degrade: bool = typer.Option(
        False,
        "--allow-degrade",
        help=(
            "If the paid engine is unavailable before any paid work has been done (refused "
            "credential, exhausted quota, unreachable), restart this request as the free recipe "
            "and report it as degraded instead of stopping with exit code 3. Local only."
        ),
    ),
    confirm_mirror: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--confirm-mirror",
        help="Manually confirm and import an allow-listed mirror URL; repeatable.",
    ),
    max_generations: int = typer.Option(
        -1,
        "--max-generations",
        min=-1,
        help=(
            "Rescan generations after generation 0; 0 disables rescans. "
            "-1 uses the profile or [rescan].max_generations from the config."
        ),
    ),
    novelty: bool = typer.Option(
        True,
        "--novelty/--no-novelty",
        help="Compute local spectral-novelty change points as rescan triggers.",
    ),
    collapse: bool | None = typer.Option(
        None,
        "--collapse/--no-collapse",
        help=(
            "Collapse a contiguous run of competing near-duplicate matches of the same underlying "
            "track into one row with 'could also be' alternatives (default: present.collapse, on)."
        ),
    ),
    i_own_this_audio_or_have_permission: bool = typer.Option(
        False,
        "--i-own-this-audio-or-have-permission",
        # Retired with the whole-file upload scan (0a-iii): a clip to a paid recogniser needs no
        # ownership.  Still accepted so old command lines keep working, but it changes nothing.
        hidden=True,
    ),
    engine: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--engine",
        help=(
            "Add a paid clip engine ('audd') for this run; repeatable. Only --recipe deep can "
            "start paid work, and Deep already uses AudD, so this is a legacy no-op kept for "
            "old command lines. 'acrcloud' is refused: ACRCloud was retired (same catalogue "
            "family as AudD)."
        ),
    ),
    fake_providers: str | None = typer.Option(
        None,
        "--fake-providers",
        hidden=True,
    ),
    local_index: str | None = typer.Option(  # noqa: B008
        None,
        "--local-index",
        help=(
            "Query a local Panako reference index (by its --index-label from `build-index`) over "
            "the still-uncertain spans, to recover the DJ's own unreleased tracks that no public "
            "catalogue holds. Free and self-hosted; needs a built index and a JDK, else it skips."
        ),
    ),
    index_root: Path = typer.Option(  # noqa: B008
        Path("data/local/panako-db"), "--index-root", help="Root holding built local indexes."
    ),
    panako_tool_dir: Path = typer.Option(  # noqa: B008
        Path("data/local/panako"), "--panako-tool-dir", help="Directory holding the Panako jar."
    ),
) -> None:
    """Run the full multi-generation pipeline and export a flattened tracklist."""
    calibrator = None
    enabled_engines: tuple[str, ...] = ()
    paid_scan_adapters: Mapping[str, object] | None = None
    shazam_http_client: HTTPClientInterface | None = None
    # The file config is always the source of non-schedule preferences (lead-in, budget, cache TTLs,
    # per-connector hint switches).  A --profile (or the file's default_profile) is the authority on
    # engines and the transform/schedule/rescan geometry, so it overrides those tables while the
    # file still supplies the preferences above.
    file_config = _load_app_config(config)
    if not file_config.hints_enabled:
        no_hints = True
    selected_profile = profile if profile is not None else file_config.default_profile
    if selected_profile is not None:
        frozen = _load_profile_or_exit(selected_profile)
        # One resolver shared with the web runner and `config show`: the profile fixes the
        # transform/schedule/rescan geometry, every other file preference is carried (review H6),
        # and the config's rescan ceiling caps the profile's rescan generations (default 0 =
        # rescans off; set [rescan] max_generations or --max-generations to opt in).
        loaded_config = effective_app_config(file_config, frozen)
        # A frozen profile is the authority on its feature toggles and its engine set (only
        # max_accuracy lists paid file_scanner engines).
        enabled_engines = tuple(frozen.enabled_engines)
        novelty = frozen.novelty_enabled
        no_hints = no_hints or not frozen.hints_enabled
        # Use calibrated scores/tiers only if a frozen calibration artefact exists for the profile;
        # otherwise the pipeline stays heuristic (current behaviour). No real-mix calibration model
        # is committed, so this is heuristic by default until an owner-verified corpus fits one.
        calibrator = load_calibration(PROJECT_ROOT, frozen.name)
    else:
        loaded_config = file_config
    # An explicit --engine adds a paid scanner on top of whatever the profile fixes (deduped, order
    # preserved).  It still runs only with credentials + consent, so this is a convenience opt-in,
    # not a bypass of the safety gates.
    if engine:
        requested = [name.strip().lower() for name in engine if name.strip()]
        if "acrcloud" in requested:
            typer.echo(
                "--engine acrcloud is refused: ACRCloud was retired in v2 (it is the same "
                "catalogue family as AudD and recovered almost nothing AudD missed)",
                err=True,
            )
            raise typer.Exit(2)
        unknown = [name for name in requested if name not in PAID_CLIP_ENGINES]
        if unknown:
            typer.echo(f"unknown --engine: {', '.join(unknown)} (choose audd)", err=True)
            raise typer.Exit(2)
        enabled_engines = tuple(dict.fromkeys([*enabled_engines, *requested]))
    if i_own_this_audio_or_have_permission:
        typer.echo(
            "--i-own-this-audio-or-have-permission has no effect: the whole-file upload scan "
            "was removed; paid clips need no ownership",
            err=True,
        )
    if fake_providers is not None:
        if os.environ.get("IDEA_TEST_MODE") != "1":
            typer.echo("--fake-providers is available only when IDEA_TEST_MODE=1", err=True)
            raise typer.Exit(2)
        names = tuple(
            dict.fromkeys(
                name.strip().casefold() for name in fake_providers.split(",") if name.strip()
            )
        )
        unknown = [name for name in names if name not in {"audd", "shazam"}]
        if not names or unknown:
            detail = f": {', '.join(unknown)}" if unknown else ""
            typer.echo(f"unknown fake provider{detail} (choose audd and/or shazam)", err=True)
            raise typer.Exit(2)
        script_value = os.environ.get("IDEA_FAKE_SCRIPT", "").strip()
        if not script_value:
            typer.echo("IDEA_FAKE_SCRIPT must name a fake-provider script", err=True)
            raise typer.Exit(2)
        try:
            import runpy

            fake_module = runpy.run_path(str(PROJECT_ROOT / "tests" / "fakes" / "providers.py"))
            load_fake_providers = fake_module["load_fake_providers"]
            fake_audd, fake_shazam = load_fake_providers(Path(script_value), names)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            typer.echo(f"invalid IDEA_FAKE_SCRIPT: {redact_text(str(exc))}", err=True)
            raise typer.Exit(2) from None
        if fake_audd is not None:
            paid_scan_adapters = {"audd": fake_audd}
            enabled_engines = tuple(dict.fromkeys([*enabled_engines, "audd"]))
        shazam_http_client = fake_shazam
    if collapse is not None:
        loaded_config = replace(loaded_config, collapse=collapse)
    selected_refresh_states = frozenset(
        state.strip().casefold() for state in refresh_states.split(",") if state.strip()
    )
    invalid_refresh_states = selected_refresh_states - {"match", "no_match"}
    if invalid_refresh_states:
        typer.echo(
            f"unknown --refresh-states: {', '.join(sorted(invalid_refresh_states))}", err=True
        )
        raise typer.Exit(2)
    if max_requests < 0:
        max_requests = loaded_config.max_requests
    if no_hints and (tracklist is not None or confirm_mirror):
        typer.echo("--tracklist/--confirm-mirror cannot be combined with --no-hints", err=True)
        raise typer.Exit(2)
    # Recipe selection is the only thing that can start paid work. Without an explicit --recipe the
    # legacy invocation keeps exactly the spend it had before recipes existed: max_accuracy went
    # paid-first only when a paid clip engine was ALSO enabled (--engine audd). A bare
    # `--profile max_accuracy` (or `default_profile = "max_accuracy"`) therefore never begins
    # billing AudD on its own — `--recipe deep` is the explicit opt-in.
    legacy_paid_first = selected_profile == "max_accuracy" and bool(
        set(enabled_engines) & set(PAID_CLIP_ENGINES)
    )
    try:
        requested_recipe = get_recipe(
            recipe or ("deep" if legacy_paid_first else "free"),
            primary_density=loaded_config.deep_primary_density,
        )
    except ValueError as exc:
        typer.echo(redact_text(str(exc)), err=True)
        raise typer.Exit(2) from None
    if engine and requested_recipe.name == "free":
        typer.echo(
            "--engine is ignored by the free recipe (max_usd_e2 = 0); "
            "pass --recipe deep to run a paid scan",
            err=True,
        )
    try:
        exit_code = asyncio.run(
            _analyse(
                url,
                work_root=work_root,
                print_raw=raw,
                refresh=refresh,
                refresh_states=selected_refresh_states,
                max_requests=max_requests,
                tracklist=tracklist,
                no_hints=no_hints,
                confirmed_mirrors=tuple(confirm_mirror or ()),
                app_config=loaded_config,
                max_generations=(
                    max_generations
                    if max_generations >= 0
                    else loaded_config.rescan_max_generations
                ),
                novelty=novelty,
                calibrator=calibrator,
                enabled_engines=enabled_engines,
                cli_confirmation=i_own_this_audio_or_have_permission,
                primary_engine=requested_recipe.primary_engine,
                recipe=requested_recipe,
                allow_degrade=allow_degrade,
                paid_scan_adapters=paid_scan_adapters,
                shazam_http_client=shazam_http_client,
                local_index_label=local_index,
                index_root=index_root,
                panako_tool_dir=panako_tool_dir,
            )
        )
    except KeyboardInterrupt:
        typer.echo("cancelled; safe job states were restored", err=True)
        raise typer.Exit(130) from None
    raise typer.Exit(exit_code)


async def _acquire(
    url: str,
    *,
    work_root: Path,
    refresh: bool,
    enable_soundcloud: bool,
    progress: ProgressFn | None = None,
) -> int:
    from id_detector.contracts import PcmRecord
    from id_detector.enrich.run import final_identities_path, load_analysis

    work_root = work_root.resolve()
    cached = _load_cached(work_root, url)
    if cached is None:
        typer.echo(
            f"no cached analysis for {redact_text(url)} under {work_root}; run `analyse` first",
            err=True,
        )
        return 2
    media_dir = cached.media_dir
    if not (media_dir / "fuse" / "episodes.json").is_file():
        typer.echo(
            f"analysis at {media_dir} has no fuse/episodes.json; run `analyse` first", err=True
        )
        return 2

    _report(progress, "enrich", 0, 1, "resolving acquire links")
    cache_root = PROJECT_ROOT / "data" / "local" / "enrich"
    result = await enrich_media_dir(
        source=cached.record,
        media_dir=media_dir,
        cache_root=cache_root,
        refresh=refresh,
        enable_soundcloud=enable_soundcloud,
    )
    _report(progress, "enrich", 1, 1, "acquire links resolved")
    _report(progress, "present", 0, 1, "updating result page")
    episodes, identities = load_analysis(media_dir)
    acquire_config = _load_app_config(Path("idea.toml"))
    duration_ms = PcmRecord.model_validate_json(
        read_text(media_dir / "decode" / "pcm.json")
    ).pcm.duration_ms
    export_tracklist(
        media_dir=media_dir,
        media_key=cached.record.media_key,
        duration_ms=duration_ms,
        episodes=episodes,
        identities=identities,
        episodes_path=media_dir / "fuse" / "episodes.json",
        identities_path=final_identities_path(media_dir),
        acquire=result.record,
        acquire_path=result.path,
        title=cached.record.title,
        media_target=cached.record.canonical_url,
        collapse=acquire_config.collapse,
        same_track_bridge_ms=acquire_config.same_track_bridge_ms,
        min_track_ms=acquire_config.present_min_track_ms,
        **_tracklist_run_fields(media_dir),
    )
    generate_page(
        media_dir=media_dir,
        source=cached.record,
        episodes=episodes,
        identities=identities,
        duration_ms=duration_ms,
        episodes_path=media_dir / "fuse" / "episodes.json",
        identities_path=final_identities_path(media_dir),
        acquire=result.record,
        acquire_path=result.path,
        lead_in_ms=acquire_config.lead_in_ms,
        collapse=acquire_config.collapse,
        same_track_bridge_ms=acquire_config.same_track_bridge_ms,
        min_track_ms=acquire_config.present_min_track_ms,
    )
    _report(progress, "present", 1, 1, "result page updated")
    typer.echo(
        f"acquire: {result.counts['episodes']} identified episodes; "
        f"{result.counts['direct_links_total']} direct links "
        f"{result.counts['direct_links_by_source']}; "
        f"free_dl={result.counts['free_download_flags']}; "
        f"gate={result.counts['gate_links']}; buy={result.counts['buy_links']}; "
        f"search_only={result.counts['search_only_rows']}; out={result.path}"
    )
    return 0


@app.command()
def acquire(
    url: str = typer.Argument(
        ..., help="A URL (or local file) already analysed under --work-root."
    ),
    work_root: Path = typer.Option(DEFAULT_WORK_ROOT, "--work-root"),  # noqa: B008
    refresh: bool = typer.Option(False, "--refresh", help="Bypass the local enrichment cache."),
    soundcloud: bool = typer.Option(
        True,
        "--soundcloud/--no-soundcloud",
        help="Resolve SoundCloud acquisition flags (api-v2, zero-auth). Never automates gates.",
    ),
) -> None:
    """Attach non-authoritative acquisition links to an existing analysis (writes acquire.json)."""

    exit_code = asyncio.run(
        _acquire(
            url,
            work_root=work_root,
            refresh=refresh,
            enable_soundcloud=soundcloud,
        )
    )
    raise typer.Exit(exit_code)


@app.command()
def serve(
    work_root: Path = typer.Option(DEFAULT_WORK_ROOT, "--work-root"),  # noqa: B008
    port: int = typer.Option(8765, "--port", min=0, max=65535),
    host: str = typer.Option(
        "127.0.0.1", "--host", help="Loopback only; a routable interface is refused."
    ),
    analyse: bool = typer.Option(
        True,
        "--analyse/--no-analyse",
        help="Enable the browser analyse form and job runner (default). --no-analyse is read-only.",
    ),
    open_browser: bool = typer.Option(
        True, "--open/--no-open", help="Open the home page in the default browser (default)."
    ),
    config: Path = typer.Option(  # noqa: B008
        Path("idea.toml"), "--config", help="Non-secret schedule/transform TOML config."
    ),
) -> None:
    """Serve analysed sets on 127.0.0.1; by default also run analyses started from the browser."""

    import contextlib
    import threading
    import webbrowser

    from id_detector.present.server import make_server
    from id_detector.webapp.jobs import JobManager
    from id_detector.webapp.runner import make_pipeline_runner

    def _open() -> None:
        with contextlib.suppress(Exception):
            webbrowser.open(url)

    manager: JobManager | None = None
    if analyse:
        runner = make_pipeline_runner(work_root, project_root=PROJECT_ROOT, config_path=config)
        manager = JobManager(work_root, runner)
    try:
        server = make_server(work_root, host=host, port=port, job_manager=manager)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    url = f"http://{bound_host}:{bound_port}"
    mode = "analyse + read-only" if analyse else "read-only"
    typer.echo(f"serving {work_root} at {url} ({mode}; Ctrl-C to stop)")
    if open_browser:
        # Open after the server is listening; a browser failure must never stop the server.
        threading.Timer(0.4, _open).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        typer.echo("stopping", err=True)
    finally:
        server.shutdown()
        server.server_close()
        if manager is not None:
            manager.shutdown()


@app.command()
def rescan(
    url: str = typer.Argument(
        ..., help="A URL (or local file) already analysed under --work-root."
    ),
    work_root: Path = typer.Option(DEFAULT_WORK_ROOT, "--work-root"),  # noqa: B008
    config: Path = typer.Option(  # noqa: B008
        Path("idea.toml"), "--config", help="Non-secret schedule/transform TOML config."
    ),
    max_generations: int = typer.Option(
        1, "--max-generations", min=1, help="Rescan generations to run when consuming the queue."
    ),
    refresh: bool = typer.Option(False, "--refresh", help="Bypass positive/no-match TTLs."),
    no_hints: bool = typer.Option(False, "--no-hints", help="Disable all hint connectors."),
    novelty: bool = typer.Option(True, "--novelty/--no-novelty"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Only report queued requests; do not run a generation."
    ),
) -> None:
    """Consume present/rescan_queue.jsonl for a set and run another analysis generation."""

    work_root = work_root.resolve()
    cached = _load_cached(work_root, url)
    if cached is None:
        typer.echo(f"no cached analysis for {redact_text(url)} under {work_root}", err=True)
        raise typer.Exit(2)
    pending = read_rescan_queue(cached.media_dir)
    if not pending:
        typer.echo("no queued rescans")
        raise typer.Exit(0)
    if dry_run:
        typer.echo(
            f"{len(pending)} queued rescan request(s): "
            + ", ".join(f"{item.trigger}[{item.start_ms}-{item.end_ms}]" for item in pending)
        )
        raise typer.Exit(0)
    consumed = consume_rescan_queue(cached.media_dir)
    typer.echo(f"consuming {len(consumed)} queued rescan request(s); running a new generation")
    loaded_config = _load_app_config(config)
    try:
        exit_code = asyncio.run(
            _analyse(
                url,
                work_root=work_root,
                print_raw=False,
                refresh=refresh,
                max_requests=2_000,
                tracklist=None,
                no_hints=no_hints,
                app_config=loaded_config,
                max_generations=max_generations,
                novelty=novelty,
            )
        )
    except KeyboardInterrupt:
        typer.echo("cancelled; safe job states were restored", err=True)
        raise typer.Exit(130) from None
    raise typer.Exit(exit_code)


async def _original_duration_ms(path: Path) -> int:
    result = await run_process(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path.resolve()),
        ],
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError(redact_text(result.stderr or "ffprobe failed"))
    lines = result.stdout.strip().splitlines()
    if not lines:
        raise RuntimeError("ffprobe returned no duration")
    value = lines[0]
    return int((Decimal(value) * 1_000).to_integral_value())


async def _hints(
    url: str,
    *,
    tracklist: Path | None,
    confirmed_mirrors: tuple[str, ...],
    refresh: bool,
    work_root: Path,
) -> dict[str, object]:
    source_lock: ProcessLock | None = None
    media_lock: ProcessLock | None = None
    try:
        lock_key = sha256(url.encode("utf-8")).hexdigest()
        source_lock = ProcessLock(work_root.resolve() / ".locks" / f"{lock_key}.lock")
        source_lock.acquire()
        ingested = await ingest(url, work_root)
        media_lock = ProcessLock(ingested.media_dir / ".media.lock")
        media_lock.acquire()
        pcm_path = ingested.media_dir / "decode" / "pcm.json"
        if pcm_path.is_file():
            from id_detector.contracts import PcmRecord

            pcm = PcmRecord.model_validate_json(read_text(pcm_path))
            duration_ms = pcm.pcm.duration_ms
        else:
            duration_ms = await _original_duration_ms(ingested.original_path)
        result = await run_hints(
            source=ingested.record,
            duration_ms=duration_ms,
            media_dir=ingested.media_dir,
            source_path=ingested.source_path,
            project_root=PROJECT_ROOT,
            manual_tracklist=tracklist,
            confirmed_mirrors=confirmed_mirrors,
            refresh=refresh,
        )
        by_connector: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for hint in result.hints:
            by_connector[hint.connector] = by_connector.get(hint.connector, 0) + 1
            by_kind[hint.kind] = by_kind.get(hint.kind, 0) + 1
        block_lines: dict[tuple[str, bool], list[object]] = {}
        for hint in result.hints:
            if hint.kind != "tracklist_line":
                continue
            authority = bool(
                hint.author.is_uploader or hint.is_pinned or hint.connector in {"mixesdb", "1001tl"}
            )
            block_lines.setdefault((hint.connector, authority), []).append(hint)
        top_blocks = [
            {
                "connector": connector,
                "authority": int(authority),
                "line_count": len(lines),
                "sample_lines": [
                    {
                        "position_range_ms": list(hint.position_range_ms)
                        if hint.position_range_ms
                        else None,
                        "artist": hint.artist,
                        "title": hint.title,
                    }
                    for hint in lines[:5]
                ],
            }
            for (connector, authority), lines in sorted(
                block_lines.items(), key=lambda item: (-int(item[0][1]), -len(item[1]), item[0][0])
            )[:10]
        ]
        return {
            "counts_by_connector": dict(sorted(by_connector.items())),
            "counts_by_kind": dict(sorted(by_kind.items())),
            "tracklist_blocks": result.tracklist_blocks,
            "top_tracklist_blocks": top_blocks,
            "quarantined_mirrors": list(result.quarantined_mirrors),
            "hints_path": str(result.hints_path),
            "connector_status_path": str(result.status_path),
        }
    finally:
        if media_lock is not None:
            media_lock.release()
        if source_lock is not None:
            source_lock.release()


@app.command("hints")
def hints_command(
    url: str = typer.Argument(..., help="Public mix URL (or a local media file)."),
    tracklist: Path | None = typer.Option(  # noqa: B008
        None, "--tracklist", help="Manual UTF-8 tracklist."
    ),
    confirm_mirror: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--confirm-mirror",
        help="Manually confirm and import an allow-listed mirror URL; repeatable.",
    ),
    refresh: bool = typer.Option(False, "--refresh", help="Refresh connector caches."),
    work_root: Path = typer.Option(DEFAULT_WORK_ROOT, "--work-root"),  # noqa: B008
) -> None:
    """Fetch and parse hints without decoding, recognition, fusion, or export."""

    try:
        summary = asyncio.run(
            _hints(
                url,
                tracklist=tracklist,
                confirmed_mirrors=tuple(confirm_mirror or ()),
                refresh=refresh,
                work_root=work_root,
            )
        )
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        typer.echo(redact_text(str(exc)), err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(summary, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


def _parse_position(value: str) -> int:
    parts = value.strip().split(":")
    if not parts or any(not part.isdigit() for part in parts) or len(parts) > 3:
        raise ValueError(f"invalid position: {value}")
    numbers = [int(part) for part in parts]
    if len(numbers) == 1:
        seconds = numbers[0]
    elif len(numbers) == 2:
        seconds = numbers[0] * 60 + numbers[1]
    else:
        seconds = numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    return seconds * 1000


@app.command("calibrate-shazam")
def calibrate_shazam_command(
    track: str = typer.Option(..., "--track", help="Released local track or public URL."),
    positions: str = typer.Option(
        ..., "--positions", help="Comma-separated seconds or MM:SS positions (at least five)."
    ),
) -> None:
    """Run the live insertion suite and write a new immutable Shazam config."""
    try:
        parsed = [_parse_position(value) for value in positions.split(",") if value.strip()]
        result = asyncio.run(
            calibrate_shazam(track=track, positions_ms=parsed, project_root=PROJECT_ROOT)
        )
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo(
        json.dumps(
            {
                "provider_config": str(result.config_path),
                "adapter_bias_ms": result.adapter_bias_ms,
                "adapter_bias_uncertainty_ms": result.adapter_bias_uncertainty_ms,
                "L_min_ms": result.l_min_ms,
                "cases": result.cases,
                "successes": result.successes,
                "physical_attempts": result.physical_attempts,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


@app.command()
def show(
    source_key: str,
    work_root: Path = typer.Option(DEFAULT_WORK_ROOT, "--work-root"),  # noqa: B008
) -> None:
    """Show cached source metadata and Stage 1 artifact paths."""
    source_root = work_root.resolve() / source_key
    candidates = sorted(source_root.glob("*/ingest/source.json"))
    if not candidates:
        typer.echo(f"source key not found: {source_key}", err=True)
        raise typer.Exit(1)
    source_path = candidates[-1]
    source = SourceRecord.model_validate_json(source_path.read_text(encoding="utf-8"))
    media_dir = source_path.parents[1]
    recognition_invocations = sorted(
        path.parent for path in media_dir.glob("recognise/invocations/*/observations.gen0.jsonl")
    )
    payload = {
        "source": source.model_dump(mode="json"),
        "media_dir": str(media_dir),
        "artifacts": {
            name: str(media_dir / relative)
            for name, relative in {
                "pcm": "decode/pcm.json",
                "windows": "windows/windows.gen0.jsonl",
                "journal": "invocations.jsonl",
                "jobs": "jobs.sqlite",
            }.items()
            if (media_dir / relative).exists()
        },
        "recognition_invocations": [str(path) for path in recognition_invocations],
    }
    typer.echo(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


async def _retry(job_id: str, work_root: Path) -> Path | None:
    for database in sorted(work_root.resolve().glob("*/*/jobs.sqlite")):
        async with AsyncJobStore(database) as store:
            job = await store.get_job(job_id)
            if job is not None:
                await store.acknowledge_retry(job_id)
                return database
    return None


@app.command()
def retry(
    job_id: str,
    acknowledge_billing: bool = typer.Option(False, "--acknowledge-billing"),
    work_root: Path = typer.Option(DEFAULT_WORK_ROOT, "--work-root"),  # noqa: B008
) -> None:
    """Manually release one outcome-unknown job for a possible billed resubmission."""
    if not acknowledge_billing:
        typer.echo("--acknowledge-billing is required", err=True)
        raise typer.Exit(2)
    database = asyncio.run(_retry(job_id, work_root))
    if database is None:
        typer.echo(f"outcome-unknown job not found: {job_id}", err=True)
        raise typer.Exit(1)
    typer.echo(f"job {job_id} returned to pending in {database}")


@benchmark_app.command("score")
def benchmark_score(
    truth: Annotated[Path, typer.Option("--truth", help="Truth directory or ground_truth.json.")],
    episodes: Annotated[
        Path, typer.Option("--episodes", help="Identity-labelled prediction JSON.")
    ],
    out: Annotated[Path, typer.Option("--out", help="Output benchmark report JSON.")],
) -> None:
    """Score predictions using support-time occurrence association."""
    try:
        report = score_corpus(truth, episodes, out_path=out)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo(
        f"scored {len(report.sets)} sets; work precision="
        f"{report.overall.identification_work.precision_e4}/10000; report={out}"
    )


@benchmark_app.command("render")
def benchmark_render(
    sources: Annotated[Path, typer.Option("--sources", help="Directory of legally held audio.")],
    out: Annotated[Path, typer.Option("--out", help="Controlled corpus output directory.")],
    seed: Annotated[int, typer.Option("--seed", min=0)],
    audio_out: Annotated[
        Path | None,
        typer.Option("--audio-out", help="Local-only rendered audio directory."),
    ] = None,
    cases: Annotated[
        str,
        typer.Option("--cases", help="Case set: base (Stage 2a) or events (Stage 4c replicates)."),
    ] = "base",
    corpus_version: Annotated[
        str | None,
        typer.Option("--corpus-version", help="Write this corpus_version into every truth file."),
    ] = None,
) -> None:
    """Render the deterministic controlled-transform slice through FFmpeg."""
    try:
        result = asyncio.run(
            render_controlled(
                sources,
                out,
                seed=seed,
                audio_dir=audio_out,
                case_set=cases,
                corpus_version=corpus_version,
            )
        )
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except (ValueError, RuntimeError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo(
        f"rendered {result.set_count} sets and {result.boundary_count} boundaries; "
        f"manifest={result.manifest_path}"
    )


@benchmark_app.command("run")
def benchmark_run(
    corpus: Annotated[str, typer.Option("--corpus", help="Corpus version under data/corpus.")],
    profile: Annotated[str, typer.Option("--profile")] = "free",
    out: Annotated[Path, typer.Option("--out", help="Output benchmark report JSON.")] = Path(
        "benchmark-report.json"
    ),
    baseline: Annotated[
        str | None,
        typer.Option("--baseline", help="Baseline corpus name or report path."),
    ] = None,
    set_id: Annotated[
        str | None,
        typer.Option("--set-id", help="Run one corpus set (useful for live smoke tests)."),
    ] = None,
    work_root: Annotated[Path, typer.Option("--work-root")] = DEFAULT_WORK_ROOT,
    max_requests: Annotated[int, typer.Option("--max-requests", min=1)] = 2_000,
    include_hints: Annotated[
        bool, typer.Option("--hints/--no-hints", help="Include Stage 4a hint evidence.")
    ] = False,
) -> None:
    """Analyse every selected corpus set, score it, and compare a named baseline."""

    try:
        result = asyncio.run(
            run_corpus(
                corpus_version=corpus,
                profile=profile,
                out_path=out,
                project_root=PROJECT_ROOT,
                work_root=work_root,
                baseline=baseline,
                set_id=set_id,
                max_requests=max_requests,
                include_hints=include_hints,
            )
        )
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo(
        f"scored {len(result.report.sets)} sets; "
        f"unverified_seed_comparison={str(result.report.unverified_seed_comparison).lower()}; "
        f"report={out}"
    )


@benchmark_app.command("transforms-schedule")
def benchmark_transforms_schedule(
    corpus: Annotated[str, typer.Option("--corpus", help="Frozen controlled corpus version.")],
    out: Annotated[Path, typer.Option("--out", help="Stage 4b decision report JSON.")],
    work_root: Annotated[Path, typer.Option("--work-root")] = Path(
        "data/local/work-transforms-schedule"
    ),
) -> None:
    """Compare every Stage 4b schedule with transforms off and global."""

    try:
        result = run_transform_schedule_benchmark(
            corpus_version=corpus,
            out_path=out,
            project_root=PROJECT_ROOT,
            work_root=work_root,
        )
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        typer.echo(redact_text(str(exc)), err=True)
        raise typer.Exit(1) from None
    schedule = result.selected_schedule
    typer.echo(
        f"benchmarked 18 schedules with off/global policies; rescan-policy="
        f"{schedule.window_ms}/{schedule.hop_ms}/{schedule.phase_ms}; report={result.path}"
    )


@benchmark_app.command("ablations")
def benchmark_ablations(
    corpus: Annotated[str, typer.Option("--corpus", help="Frozen controlled corpus version.")],
    out: Annotated[Path, typer.Option("--out", help="Stage 4c ablation and gate report JSON.")],
    work_root: Annotated[Path, typer.Option("--work-root")] = Path("data/local/work-ablations"),
) -> None:
    """Run the Stage 4c per-engine and per-feature ablations and evaluate its acceptance gates."""

    try:
        result = run_ablations(
            corpus_version=corpus,
            out_path=out,
            project_root=PROJECT_ROOT,
            work_root=work_root,
            engine_statuses=engine_status_rows(PROJECT_ROOT),
        )
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        typer.echo(redact_text(str(exc)), err=True)
        raise typer.Exit(1) from None
    gates = "; ".join(
        f"{gate['name']}={str(bool(gate['pass'])).lower()}" for gate in result.payload["gates"]
    )
    typer.echo(
        f"ablated {len(result.payload['arms'])} arms on {corpus} "
        f"({result.payload['n_sets']} sets, {result.payload['n_boundaries']} boundaries); "
        f"{gates}; report={out}"
    )


@benchmark_app.command("freeze-profiles")
def benchmark_freeze_profiles(
    ablations: Annotated[Path, typer.Option("--ablations", help="Stage 4c ablation report JSON.")],
    shortlist: Annotated[Path, typer.Option("--shortlist", help="Stage 3 shortlist report JSON.")],
    out: Annotated[
        Path, typer.Option("--out", help="Directory to write frozen profiles into.")
    ] = Path("profiles"),
) -> None:
    """Derive the frozen `free` and `max_accuracy` profiles mechanically from the two reports."""

    try:
        result = freeze_profiles(
            ablations_path=ablations,
            shortlist_path=shortlist,
            out_dir=out,
        )
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        typer.echo(redact_text(str(exc)), err=True)
        raise typer.Exit(1) from None
    for name in sorted(result.profiles):
        profile = result.profiles[name]
        typer.echo(
            f"froze {profile.version}: engines={','.join(profile.enabled_engines)} "
            f"transforms={profile.transforms_policy} rescans={str(profile.rescan.enabled).lower()} "
            f"novelty={str(profile.novelty_enabled).lower()} "
            f"hints={str(profile.hints_enabled).lower()}(certified=false); "
            f"-> {result.written[name]}"
        )


@benchmark_app.command("certify")
def benchmark_certify(
    corpus: Annotated[
        str, typer.Option("--corpus", help="Frozen corpus version under data/corpus.")
    ],
    profile: Annotated[str, typer.Option("--profile", help="Frozen profile name.")],
    test_version: Annotated[
        str, typer.Option("--test-version", help="Immutable test-set version identifier.")
    ],
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Certification report JSON (default under data/local)."),
    ] = None,
    work_root: Annotated[Path, typer.Option("--work-root")] = Path("data/local/work-certify"),
) -> None:
    """Evaluate one frozen test version once and write the pre-registered certification report."""

    try:
        result = asyncio.run(
            run_certify(
                corpus_version=corpus,
                profile=profile,
                test_version=test_version,
                project_root=PROJECT_ROOT,
                work_root=work_root,
                out_path=out,
            )
        )
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except (CorpusNotFrozen, DuplicateTestVersion) as exc:
        typer.echo(redact_text(str(exc)), err=True)
        raise typer.Exit(2) from None
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        typer.echo(redact_text(str(exc)), err=True)
        raise typer.Exit(1) from None
    typer.echo(
        f"certified corpus={corpus} profile={profile} test_version={test_version}; "
        f"certified_triples={result.n_certified}; n_test_predictions={result.n_test_predictions}; "
        f"report={result.report_path}"
    )


@benchmark_app.command("calibration-validate")
def benchmark_calibration_validate(
    corpus: Annotated[str, typer.Option("--corpus", help="Frozen controlled corpus version.")],
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Validation report JSON (default under data/corpus)."),
    ] = None,
    work_root: Annotated[Path, typer.Option("--work-root")] = Path(
        "data/local/work-calibration-validate"
    ),
    seed: Annotated[int, typer.Option("--seed", min=0)] = 20_260_904,
) -> None:
    """Fit and validate the calibration machinery on a controlled corpus (not certification)."""

    try:
        result = asyncio.run(
            run_calibration_validation(
                corpus_version=corpus,
                project_root=PROJECT_ROOT,
                work_root=work_root,
                split_seed=seed,
                out_path=out,
            )
        )
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        typer.echo(redact_text(str(exc)), err=True)
        raise typer.Exit(1) from None
    certified = sum(entry.status == "certified" for entry in result.record.certification)
    typer.echo(
        f"validated calibration machinery on {corpus} "
        f"({result.n_calibration_sets} calibration sets, {result.n_test_sets} test sets); "
        f"certified_triples={certified} (controlled -- not real-mix certification); "
        f"model={result.model_path}; report={result.validation_path}"
    )


@benchmark_app.command("shortlist")
def benchmark_shortlist(
    corpus: Annotated[str, typer.Option("--corpus", help="Controlled corpus version.")],
    out: Annotated[Path, typer.Option("--out", help="Shortlist report JSON.")],
    config: Annotated[
        Path, typer.Option("--config", help="Non-secret TOML config with the upload gate.")
    ] = Path("idea.toml"),
    i_own_this_audio_or_have_permission: Annotated[
        bool,
        typer.Option(
            "--i-own-this-audio-or-have-permission",
            help="Per-run confirmation required before any paid-provider upload.",
        ),
    ] = False,
    work_root: Annotated[Path, typer.Option("--work-root")] = Path("data/local/work-shortlist"),
    max_requests: Annotated[int, typer.Option("--max-requests", min=1)] = 2_000,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Bypass positive/no-match scanner cache TTLs.")
    ] = False,
) -> None:
    """Run every available engine independently and write the Stage-3 shortlist."""

    try:
        result = asyncio.run(
            run_shortlist(
                corpus_version=corpus,
                out_path=out,
                project_root=PROJECT_ROOT,
                work_root=work_root,
                app_config=_load_app_config(config),
                cli_confirmation=i_own_this_audio_or_have_permission,
                max_requests=max_requests,
                refresh=refresh,
            )
        )
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        typer.echo(redact_text(str(exc)), err=True)
        raise typer.Exit(1) from None
    statuses = ", ".join(f"{engine.provider}={engine.status}" for engine in result.report.engines)
    typer.echo(
        f"shortlisted {len(result.report.engines)} engines on "
        f"{result.report.corpus_version}; {statuses}; report={out}"
    )


@benchmark_app.command("hints")
def benchmark_hints(
    corpus: Annotated[str, typer.Option("--corpus", help="Frozen held-out corpus version.")],
    out: Annotated[Path, typer.Option("--out", help="Stage 4a gate report JSON.")],
    work_root: Annotated[Path, typer.Option("--work-root")] = Path("data/local/work-hints-gate"),
    max_requests: Annotated[int, typer.Option("--max-requests", min=1)] = 2_000,
) -> None:
    """Run the formal fused-vs-audio-only Stage 4a held-out gate."""

    try:
        result = asyncio.run(
            run_hint_gate(
                corpus_version=corpus,
                out_path=out,
                project_root=PROJECT_ROOT,
                work_root=work_root,
                max_requests=max_requests,
            )
        )
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        typer.echo(redact_text(str(exc)), err=True)
        raise typer.Exit(1) from None
    typer.echo(
        f"Stage 4a gate pass={str(result.passed).lower()}; "
        f"coverage_delta_e4={result.coverage_delta_e4}; "
        f"coverage_cluster_lower_e4={result.coverage_cluster_lower_e4}; report={out}"
    )


def _collect_acquire_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(root.rglob("acquire.json"))


@benchmark_app.command("links")
def benchmark_links(
    episodes: Annotated[
        Path,
        typer.Option(
            "--episodes",
            help="An acquire.json file, or a directory searched for **/enrich/acquire.json.",
        ),
    ],
    out: Annotated[Path, typer.Option("--out", help="Marking-sheet JSON to write.")],
    sample: Annotated[int, typer.Option("--sample", min=1, help="Stratified sample size.")] = 60,
) -> None:
    """Draw a stratified (version-ambiguity) sample of direct links for a human to mark."""

    from id_detector.contracts import AcquireFile
    from id_detector.io import atomic_write_json

    paths = _collect_acquire_files(episodes)
    if not paths:
        typer.echo(f"no acquire.json found under {episodes}", err=True)
        raise typer.Exit(2)
    records = [AcquireFile.model_validate_json(read_text(path)) for path in paths]
    sheet = build_link_sample(records, sample_size=sample)
    atomic_write_json(out, sheet)
    typer.echo(
        f"link sample: {len(sheet['links'])} of {sheet['total_direct_links']} direct links "
        f"across {len(paths)} analyses; strata={sheet['strata_sampled']}; "
        f"gate pending owner marking; out={out}"
    )


@benchmark_app.command("links-score")
def benchmark_links_score(
    marked: Annotated[Path, typer.Option("--marked", help="A human-marked link sample JSON.")],
    out: Annotated[Path | None, typer.Option("--out", help="Optional score JSON to write.")] = None,
) -> None:
    """Score a marked link sample: precision and a one-sided 95% Clopper-Pearson lower bound."""

    from id_detector.io import atomic_write_json

    sheet = json.loads(read_text(marked))
    score = score_link_sample(sheet)
    if out is not None:
        atomic_write_json(out, score)
    typer.echo(
        f"marked={score['marked_links']} correct={score['correct']} "
        f"incorrect={score['incorrect']} precision_e4={score['precision_e4']} "
        f"one_sided_95_lower_e4={score['one_sided_95_lower_e4']} "
        f"gate_pass={str(score['gate']['pass']).lower()}"
    )


@truth_app.command("seed")
def truth_seed(
    out: Annotated[Path, typer.Option("--out")],
    set_id: Annotated[str, typer.Option("--set-id")],
    duration_ms: Annotated[int, typer.Option("--duration-ms", min=1)],
    media_key: Annotated[str, typer.Option("--media-key")],
    hints: Annotated[Path | None, typer.Option("--hints")] = None,
    tracklist: Annotated[Path | None, typer.Option("--tracklist")] = None,
    overlays: Annotated[
        Path | None,
        typer.Option(
            "--overlays",
            help=(
                "Tracks blended in over a tracklist row ('w/' lines, 'H:MM:SS - Artist - Title "
                "(w/ overlay)'): each becomes a layered episode from its time to the end of the "
                "row it overlays, linked both ways. Needs a timestamped --tracklist."
            ),
        ),
    ] = None,
    split: Annotated[str, typer.Option("--split")] = "dev-1",
    stratum: Annotated[str, typer.Option("--stratum")] = "catalogue-covered",
    corpus_version: Annotated[str, typer.Option("--corpus-version")] = "draft",
    platform: Annotated[str, typer.Option("--platform")] = "local",
    selection_basis: Annotated[
        str, typer.Option("--selection-basis")
    ] = "manual seed assembled before scoring",
    source_url: Annotated[str | None, typer.Option("--source-url")] = None,
    uploader: Annotated[str | None, typer.Option("--uploader")] = None,
    event: Annotated[str | None, typer.Option("--event")] = None,
) -> None:
    """Seed draft truth from hints and/or a manual tracklist."""
    try:
        truth = seed_truth(
            out_path=out,
            set_id=set_id,
            duration_ms=duration_ms,
            media_key=media_key,
            hints=hints,
            tracklist=tracklist,
            overlays=overlays,
            split=split,
            stratum=stratum,
            corpus_version=corpus_version,
            platform=platform,
            selection_basis=selection_basis,
            source_url=source_url,
            uploader=uploader,
            event=event,
            project_root=PROJECT_ROOT,
        )
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"seeded {len(truth.episodes)} draft episodes in {out}")


@truth_app.command("verify")
def truth_verify(
    truth: Annotated[Path, typer.Option("--truth")],
    annotator_ref: Annotated[str, typer.Option("--annotator-ref")],
    audio: Annotated[Path | None, typer.Option("--audio")] = None,
    annotation: Annotated[
        Path | None,
        typer.Option("--annotation", help="Complete independently authored ground-truth JSON."),
    ] = None,
) -> None:
    """Run the first-pass terminal annotation loop (commands only; no GUI launch)."""
    try:
        updated = verify_truth(
            truth, annotator_ref=annotator_ref, audio=audio, annotation_path=annotation
        )
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"saved {len(updated.episodes)} episodes to {truth}")


@truth_app.command("second-pass")
def truth_second_pass(
    truth: Annotated[Path, typer.Option("--truth")],
    annotator_ref: Annotated[str, typer.Option("--annotator-ref")],
    audio: Annotated[Path | None, typer.Option("--audio")] = None,
    annotation: Annotated[
        Path | None,
        typer.Option("--annotation", help="Complete independently authored ground-truth JSON."),
    ] = None,
) -> None:
    """Store a distinct second annotation without revealing the first-pass decisions."""
    try:
        updated = second_pass_truth(
            truth, annotator_ref=annotator_ref, audio=audio, annotation_path=annotation
        )
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"saved second pass for {len(updated.episodes)} episodes to {truth}")


@truth_app.command("resolve")
def truth_resolve(
    truth: Annotated[Path, typer.Option("--truth")],
    resolver_ref: Annotated[str, typer.Option("--resolver-ref")],
    annotation: Annotated[
        Path,
        typer.Option("--annotation", help="Third annotator's complete resolved ground-truth JSON."),
    ],
) -> None:
    """Resolve differing first/second passes with a distinct third annotation."""
    try:
        updated = resolve_truth(truth, resolver_ref=resolver_ref, annotation_path=annotation)
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"saved third-annotator resolution for {len(updated.episodes)} episodes to {truth}")


@truth_app.command("freeze")
def truth_freeze(
    truth: Annotated[Path, typer.Option("--truth", help="Truth corpus directory.")],
    corpus_version: Annotated[str, typer.Option("--corpus-version")],
    out: Annotated[Path, typer.Option("--out", help="Corpus-version manifest JSON.")],
) -> None:
    """Validate complete verification and hash a frozen corpus manifest."""
    try:
        manifest = freeze_truth(truth, corpus_version=corpus_version, out_path=out)
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"froze {len(manifest['sets'])} sets as {corpus_version}; manifest={out}")


@truth_app.command("manifest-draft")
def truth_manifest_draft(
    truth: Annotated[Path, typer.Option("--truth", help="Draft truth corpus directory.")],
    corpus_version: Annotated[str, typer.Option("--corpus-version")],
    out: Annotated[Path, typer.Option("--out", help="Draft inventory JSON.")],
) -> None:
    """Inventory unverified seeds without claiming that they are frozen truth."""

    try:
        manifest = write_draft_manifest(truth, corpus_version=corpus_version, out_path=out)
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo(
        f"recorded {len(manifest['sets'])} unverified draft sets; frozen=false; manifest={out}"
    )


if __name__ == "__main__":
    app()

"""Command-line entry point."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tomllib
import uuid
from collections.abc import Callable, Mapping
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
from id_detector.fuse.episodes import fuse_generation_zero  # noqa: F401  (public re-export)
from id_detector.hints.pipeline import run_hints
from id_detector.ingest import _load_cached, ingest
from id_detector.io import read_text, redact_text
from id_detector.jobs import AsyncJobStore, ProcessLock
from id_detector.journal import InvocationTimer, append_invocation
from id_detector.local_index import run_local_index_recognition
from id_detector.orchestrate import run_generation_loop
from id_detector.paid_clip import (
    DEFAULT_MAX_CLIPS,
    PAID_CLIP_ENGINES,
    run_paid_clip_recognition,
)
from id_detector.present import export_tracklist, generate_page
from id_detector.present.server import consume_rescan_queue, read_rescan_queue
from id_detector.process import run_process
from id_detector.profiles import (
    UnknownProfile,
    freeze_profiles,
    load_profile,
    profile_app_config,
)
from id_detector.providers.base import AppConfig
from id_detector.recognise import recognise_generation
from id_detector.rescan import DEFAULT_MAX_GENERATIONS
from id_detector.scan import PAID_FILE_SCANNERS, PaidScanResult, run_paid_scanners
from id_detector.scan_targeting import select_gap_targets, select_scan_targets
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
) -> None:
    """Print the effective, resolved configuration (file + defaults). No secrets are shown."""

    loaded = _load_app_config(config)
    source = (
        f"{config} + defaults" if config.is_file() else f"built-in defaults ({config} not found)"
    )
    typer.echo(f"# source: {source}")
    typer.echo(render_effective_config(loaded), nl=False)


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


def _windows_in_spans(
    windows: WindowsResult, spans: tuple[tuple[int, int], ...]
) -> WindowsResult:
    """A WindowsResult holding only the generation-0 windows whose start falls in one of ``spans``.

    Used by the paid-first path to run the free engine over just the gap spans the paid engine left
    blank, instead of the whole mix.
    """

    keep = tuple(
        window
        for window in windows.records
        if any(lo <= window.support_ms[0] < hi for lo, hi in spans)
    )
    return WindowsResult(records=keep, record_path=windows.record_path, cached=windows.cached)


async def _analyse(
    url: str,
    *,
    work_root: Path,
    print_raw: bool,
    refresh: bool,
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
    paid_scan_adapters: Mapping[str, object] | None = None,
    max_paid_clips: int = DEFAULT_MAX_CLIPS,
    local_index_label: str | None = None,
    index_root: Path = Path("data/local/panako-db"),
    panako_tool_dir: Path = Path("data/local/panako"),
    progress: ProgressFn | None = None,
) -> int:
    app_config = app_config or AppConfig()
    run_id = uuid.uuid4().hex
    timer = InvocationTimer(run_id, ["analyse", url])
    media_dir: Path | None = None
    ffmpeg_version: str | None = None
    source_ids: list[str] = []
    source_lock: ProcessLock | None = None
    media_lock: ProcessLock | None = None
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

        timer.start_stage("recognise_ms")

        def _on_recognise_window(done: int, total: int) -> None:
            _report(progress, "recognise", done, total, "recognising windows")

        async def recognise_windows(*, windows: object, generation: int) -> object:
            return await recognise_generation(
                media_key=ingested.record.media_key,
                media_dir=media_dir,
                windows=windows,  # type: ignore[arg-type]
                project_root=PROJECT_ROOT,
                run_id=run_id,
                generation=generation,
                refresh=refresh,
                max_requests=max_requests,
                requests_per_minute=app_config.shazam_requests_per_minute,
                concurrency=app_config.recognise_concurrency,
                positive_max_age_seconds=app_config.cache_positive_max_age_seconds,
                no_match_max_age_seconds=app_config.cache_no_match_max_age_seconds,
                on_window=_on_recognise_window if progress is not None else None,
            )

        # Which engine identifies the WHOLE mix first (generation 0).  Free = the rate-limited free
        # engine.  Paid-first (max_accuracy) = the paid engine, which is ~4-6x faster and has no
        # rate limit, so it does the bulk and the free engine only fills the spans it left blank.
        paid_first = primary_engine == "audd" and "audd" in enabled_engines
        primary_clip = PaidScanResult()
        gen0_observations: tuple[object, ...] = ()
        gen0_observations_path: Path | None = None
        gen0_requests = 0
        gen0_physical = 0
        if paid_first:
            _report(progress, "recognise", 0, 1, "identifying the whole mix (paid)")
            primary_clip = await run_paid_clip_recognition(
                media_key=ingested.record.media_key,
                media_dir=media_dir,
                windows=windows,
                targets=((0, decoded.record.pcm.duration_ms),),
                run_id=run_id,
                app_config=app_config,
                enabled_engines=enabled_engines,
                cli_confirmation=cli_confirmation,
                refresh=refresh,
                max_clips=len(windows.records) + 1,  # the whole mix, no per-mix cap
                adapters=paid_scan_adapters,
                log=lambda message: _report(progress, "recognise", 0, 1, message),
            )
            if primary_clip.observations:
                gen0_observations = primary_clip.observations
                gen0_observations_path = primary_clip.observation_paths[0]
                counts["matches"] = sum(
                    item.status == "match" for item in primary_clip.observations
                )
            else:  # no key / unavailable — fall back to the free engine as primary
                paid_first = False
        if not paid_first:
            recognised = await recognise_windows(windows=windows, generation=0)
            gen0_observations = recognised.observations
            gen0_observations_path = recognised.observations_path
            gen0_requests = recognised.requests
            gen0_physical = recognised.physical_attempts
            matches = [item for item in recognised.observations if item.status == "match"]
            counts.update(
                {
                    "requests": recognised.requests,
                    "physical_attempts": recognised.physical_attempts,
                    "matches": len(matches),
                    "failures": recognised.failures,
                    "cache_hits": recognised.cache_hits,
                }
            )
        timer.finish_stage("recognise_ms")

        # Paid whole-file scanners (AudD/ACRCloud) run once here when the active profile enables
        # them AND credentials + upload consent are present; otherwise this is a no-op and the free
        # path is untouched.  Their observations join generation 0 as a static evidence set.
        # In paid-first mode the paid engine already scanned the whole mix as generation 0, so the
        # (consent-gated) whole-file scan would be redundant — skip it.
        paid_scan = PaidScanResult()
        if enabled_engines and not paid_first:
            _report(progress, "scan", 0, 1, "cross-checking with paid engines")
            timer.start_stage("scan_ms")
            paid_scan = await run_paid_scanners(
                media_key=ingested.record.media_key,
                media_dir=media_dir,
                asset_path=ingested.original_path,
                asset_sha256=ingested.record.original.sha256,
                asset_kind="original",
                duration_ms=decoded.record.pcm.duration_ms,
                source_path=ingested.source_path,
                app_config=app_config,
                enabled_engines=enabled_engines,
                cli_confirmation=cli_confirmation,
                refresh=refresh,
                adapters=paid_scan_adapters,
                log=lambda message: _report(progress, "scan", 0, 1, message),
            )
            timer.finish_stage("scan_ms")
            counts["paid_matches"] = sum(
                item.status == "match" for item in paid_scan.observations
            )
            for provider, reason in paid_scan.skipped:
                _report(progress, "scan", 1, 1, f"{provider} skipped: {reason}")
            summary = (
                f"{len(paid_scan.engines_run)} engine(s), "
                f"{len(paid_scan.observations)} observations"
                if paid_scan.ran
                else "no paid engine ran"
            )
            _report(progress, "scan", 1, 1, summary)

        hint_result = None
        if not no_hints:
            _report(progress, "hints", 0, 1, "reading tracklist hints")
            timer.start_stage("hints_ms")
            hint_result = await run_hints(
                source=ingested.record,
                duration_ms=decoded.record.pcm.duration_ms,
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
                gen0_requests=gen0_requests,
                gen0_physical_attempts=gen0_physical,
                calibrator=calibrator,
            )

        _report(progress, "fuse", 0, 1, "fusing episodes")
        timer.start_stage("fuse_ms")
        orchestrated = await _fuse(paid_scan.observations, paid_scan.observation_paths)
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

        # Phase 2 — cross-check the still-uncertain spans with the levers that see what Shazam
        # can't.  The free fuse just told us which spans remain uncertain; each lever sends only
        # those window clips (the same ~12 s clips Shazam saw — no whole-file upload, no consent
        # gate) and re-fuses, so agreement lifts confidence, disagreement lets a Shazam phantom be
        # demoted, and a Shazam-blind track is recovered:
        #   • a local Panako index — the DJ's OWN unreleased uploads, in no public catalogue;
        #   • the paid engine's CLIP path (AudD) — catalogued-but-Shazam-blind tracks.
        # FREE-FIRST: Panako runs and re-fuses BEFORE AudD, so whatever it confidently identifies
        # shrinks the uncertain region — we never pay AudD to check a span the free index nailed.
        # A no-op unless a lever is enabled and uncertain spans remain.
        clip_scan = PaidScanResult()
        index_scan = PaidScanResult()
        # In paid-first mode the paid engine already covered the whole mix, so the paid CLIP lever
        # is not a phase-2 step here; the free engine's gap-fill is.
        want_clips = bool(set(enabled_engines) & set(PAID_CLIP_ENGINES)) and not paid_first
        want_index = local_index_label is not None

        async def _refuse_with(*scans: PaidScanResult) -> None:
            nonlocal fused, orchestrated
            _report(progress, "fuse", 0, 1, "re-fusing with cross-check evidence")
            timer.start_stage("refuse_ms")
            extra_obs = (*paid_scan.observations, *(o for s in scans for o in s.observations))
            extra_paths = (
                *paid_scan.observation_paths,
                *(p for s in scans for p in s.observation_paths),
            )
            orchestrated = await _fuse(extra_obs, extra_paths)
            fused = orchestrated.fusion
            counts.update(
                {
                    "requests": orchestrated.requests,
                    "physical_attempts": orchestrated.physical_attempts,
                    "generations": orchestrated.final_generation + 1,
                }
            )
            timer.finish_stage("refuse_ms")
            _report(progress, "fuse", 1, 1, f"{len(fused.episodes.episodes)} episodes")

        if paid_first:
            # The paid engine identified the whole mix; the free engine now fills only the spans
            # it left blank (typically the underground tracks no commercial catalogue holds), so it
            # runs over a handful of windows instead of all of them — the point of paid-first.
            gaps = select_gap_targets(fused.episodes.episodes, decoded.record.pcm.duration_ms)
            gap_windows = _windows_in_spans(windows, gaps)
            gap_scan = PaidScanResult()
            if gap_windows.records:
                _report(
                    progress, "scan", 0, len(gap_windows.records), "filling the gaps (free engine)"
                )
                timer.start_stage("gapfill_ms")
                gap_rec = await recognise_windows(windows=gap_windows, generation=0)
                timer.finish_stage("gapfill_ms")
                counts["gap_matches"] = sum(
                    item.status == "match" for item in gap_rec.observations
                )
                counts["requests"] = counts.get("requests", 0) + gap_rec.requests
                if any(item.status == "match" for item in gap_rec.observations):
                    gap_scan = PaidScanResult(
                        observations=tuple(gap_rec.observations),
                        observation_paths=(gap_rec.observations_path,),
                    )
                    await _refuse_with(gap_scan)
            # Panako can still recover the DJ's own unreleased edits in the still-uncertain spans.
            if want_index:
                idx_targets = select_scan_targets(
                    fused.episodes.episodes, decoded.record.pcm.duration_ms
                )
                if idx_targets:
                    index_scan = await run_local_index_recognition(
                        media_key=ingested.record.media_key,
                        media_dir=media_dir,
                        windows=windows,
                        targets=idx_targets,
                        duration_ms=decoded.record.pcm.duration_ms,
                        run_id=run_id,
                        index_label=local_index_label,
                        index_root=index_root,
                        tool_dir=panako_tool_dir,
                        log=lambda message: _report(progress, "scan", 0, 1, message),
                    )
                    counts["local_index_matches"] = sum(
                        item.status == "match" for item in index_scan.observations
                    )
                    if any(item.status == "match" for item in index_scan.observations):
                        await _refuse_with(gap_scan, index_scan)
        elif want_clips or want_index:
            targets = select_scan_targets(fused.episodes.episodes, decoded.record.pcm.duration_ms)
            # Free lever first.
            if targets and want_index:
                _report(progress, "scan", 0, 1, "querying the local reference index")
                timer.start_stage("index_scan_ms")
                index_scan = await run_local_index_recognition(
                    media_key=ingested.record.media_key,
                    media_dir=media_dir,
                    windows=windows,
                    targets=targets,
                    duration_ms=decoded.record.pcm.duration_ms,
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
                    # Re-fuse so Panako's confident IDs recompute (and shrink) the uncertain region.
                    await _refuse_with(index_scan)
                    targets = select_scan_targets(
                        fused.episodes.episodes, decoded.record.pcm.duration_ms
                    )
            # Paid lever next, only on what is still uncertain after the free lever.
            if targets and want_clips:
                _report(progress, "scan", 0, 1, "clip-checking uncertain spans with paid engine")
                timer.start_stage("clip_scan_ms")
                clip_scan = await run_paid_clip_recognition(
                    media_key=ingested.record.media_key,
                    media_dir=media_dir,
                    windows=windows,
                    targets=targets,
                    run_id=run_id,
                    app_config=app_config,
                    enabled_engines=enabled_engines,
                    cli_confirmation=cli_confirmation,
                    refresh=refresh,
                    max_clips=max_paid_clips,
                    adapters=paid_scan_adapters,
                    log=lambda message: _report(progress, "scan", 0, 1, message),
                )
                timer.finish_stage("clip_scan_ms")
                for name, reason in clip_scan.skipped:
                    _report(progress, "scan", 1, 1, f"{name} skipped: {reason}")
                counts["clip_matches"] = sum(
                    item.status == "match" for item in clip_scan.observations
                )
                if any(item.status == "match" for item in clip_scan.observations):
                    await _refuse_with(index_scan, clip_scan)

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
            typer.echo(
                f"{len(matches)} matches; {recognised.failures} failures; "
                f"{orchestrated.physical_attempts} physical attempts; "
                f"{orchestrated.final_generation + 1} generations "
                f"(stop={orchestrated.stop_reason}); "
                f"{len(fused.episodes.episodes)} episodes; tracklist={exported.json_path}"
            )
        entry = timer.entry(
            status="succeeded",
            exit_code=0,
            counts=counts,
            costs={"usd_e2": 0},
            source_ids=source_ids,
            ffmpeg_version=ffmpeg_version,
        )
        append_invocation(media_dir / "invocations.jsonl", entry)
        return 0
    except asyncio.CancelledError:
        if media_dir is not None:
            entry = timer.entry(
                status="cancelled",
                exit_code=130,
                counts=counts,
                costs={"usd_e2": 0},
                source_ids=source_ids,
                ffmpeg_version=ffmpeg_version,
            )
            append_invocation(media_dir / "invocations.jsonl", entry)
        raise
    except Exception:
        if media_dir is not None:
            entry = timer.entry(
                status="failed",
                exit_code=1,
                counts=counts,
                costs={"usd_e2": 0},
                source_ids=source_ids,
                ffmpeg_version=ffmpeg_version,
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
        help=(
            "Per-run consent to upload this audio to a paid engine (AudD/ACRCloud). Required, "
            "together with allow_third_party_upload = true in config, before a profile's paid "
            "file_scanner engines run; otherwise they are skipped and only the free engine is used."
        ),
    ),
    engine: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--engine",
        help=(
            "Add a paid file_scanner engine ('audd' or 'acrcloud') for this run, on top of the "
            "profile's engines; repeatable.  The engine still runs only with its credentials set "
            "and upload consent given.  Use this to try a paid cross-check without re-freezing a "
            "profile (a frozen profile lists paid engines only once benchmarked with credentials)."
        ),
    ),
    max_paid_clips: int = typer.Option(
        DEFAULT_MAX_CLIPS,
        "--max-paid-clips",
        min=1,
        help=(
            "Per-mix budget of clips sent to a paid engine, spread evenly across the uncertain "
            f"spans (default {DEFAULT_MAX_CLIPS} ≈ $0.75/mix at AudD's $5/1000). Raise to trade "
            "cost for reach; only matters when a paid --engine is active."
        ),
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
        loaded_config = replace(
            profile_app_config(frozen),
            allow_third_party_upload=file_config.allow_third_party_upload,
            default_profile=file_config.default_profile,
            max_requests=file_config.max_requests,
            lead_in_ms=file_config.lead_in_ms,
            cache_positive_max_age_days=file_config.cache_positive_max_age_days,
            cache_no_match_max_age_days=file_config.cache_no_match_max_age_days,
            hints_enabled=file_config.hints_enabled,
            disabled_hint_connectors=file_config.disabled_hint_connectors,
            # The config's rescan ceiling caps the profile's rescan generations.  The default
            # ceiling is 0 (rescans off) because on real mixes they cost hours and add only
            # phantoms; set [rescan] max_generations in config (or --max-generations) to opt in.
            rescan_max_generations=min(
                frozen.rescan.max_generations, file_config.rescan_max_generations
            ),
        )
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
        unknown = [name for name in requested if name not in PAID_FILE_SCANNERS]
        if unknown:
            typer.echo(
                f"unknown --engine: {', '.join(unknown)} (choose audd or acrcloud)", err=True
            )
            raise typer.Exit(2)
        enabled_engines = tuple(dict.fromkeys([*enabled_engines, *requested]))
    if collapse is not None:
        loaded_config = replace(loaded_config, collapse=collapse)
    if max_requests < 0:
        max_requests = loaded_config.max_requests
    if no_hints and (tracklist is not None or confirm_mirror):
        typer.echo("--tracklist/--confirm-mirror cannot be combined with --no-hints", err=True)
        raise typer.Exit(2)
    try:
        exit_code = asyncio.run(
            _analyse(
                url,
                work_root=work_root,
                print_raw=raw,
                refresh=refresh,
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
                # max_accuracy is now paid-first: the paid engine identifies the whole mix and the
                # free engine only fills the gaps. Falls back to free-primary if audd isn't enabled.
                primary_engine="audd" if selected_profile == "max_accuracy" else "shazam",
                max_paid_clips=max_paid_clips,
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

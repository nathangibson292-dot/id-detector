"""Command-line entry point."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tomllib
from collections.abc import Mapping
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
from id_detector.calibrate.certify import (
    CertificationDisabled,
    CorpusNotFrozen,
    CorpusNotIndependent,
    DuplicateTestVersion,
    run_certify,
)
from id_detector.calibrate.model import load_calibration
from id_detector.calibrate.validate import run_calibration_validation
from id_detector.calibration import calibrate_shazam
from id_detector.config_template import CONFIG_TEMPLATE, render_effective_config
from id_detector.contracts import SourceRecord
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
from id_detector.io import atomic_write_json, read_text, redact_text
from id_detector.jobs import AsyncJobStore, ProcessLock
from id_detector.paid_clip import (
    PAID_CLIP_ENGINES,
)
from id_detector.pipeline import (  # noqa: F401  (the seam's helpers are re-exported here)
    PROJECT_ROOT,
    ProgressFn,
    _achieved,
    _money_journal_fields,
    _report,
    _run_status,
    _settle_money,
    _windows_in_spans,
)
from id_detector.pipeline import run_analysis as _analyse
from id_detector.present.server import consume_rescan_queue, read_rescan_queue
from id_detector.process import run_process
from id_detector.profiles import (
    UnknownProfile,
    effective_app_config,
    freeze_profiles,
    load_profile,
    profile_fixed_fields,
)
from id_detector.providers.base import AppConfig
from id_detector.recipes import get_recipe
from id_detector.retention import collect
from id_detector.shazam import HTTPClientInterface
from id_detector.truth import (
    CERTIFICATION_DISABLED,
    CERTIFICATION_DISABLED_NEXT_STEP,
    FREEZE_MANIFEST_NAME,
    certification_enabled,
    freeze_truth,
    refuse_generated_output,
    resolve_truth,
    second_pass_truth,
    seed_truth,
    verify_truth,
    write_draft_manifest,
)

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
benchmark_app = typer.Typer(no_args_is_help=True)
truth_app = typer.Typer(no_args_is_help=True)
config_app = typer.Typer(no_args_is_help=True, help="Show or create the idea.toml config.")
app.add_typer(benchmark_app, name="benchmark")
app.add_typer(truth_app, name="truth")
app.add_typer(config_app, name="config")
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


def _snapshot_command(arguments: list[str]) -> None:
    """Run the existing snapshot entry point behind an ``idea`` command name."""

    from idea_web.backup import BackupRefused
    from idea_web.backup import main as snapshot_main

    try:
        status = snapshot_main(arguments)
    except (BackupRefused, OSError, ValueError, json.JSONDecodeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    raise typer.Exit(status)


@app.command("backup")
def backup_command(
    into: Annotated[
        Path,
        typer.Option(
            "--into",
            help=(
                "New empty destination for the sealed snapshot "
                "(outside work, database and corpus trees)."
            ),
        ),
    ],
    work_root: Annotated[
        Path,
        typer.Option("--work-root", help="Work tree to read; it is never changed by the backup."),
    ] = DEFAULT_WORK_ROOT,
    database: Annotated[
        Path | None,
        typer.Option("--database", help="Queue database; defaults to <work-root>/.idea/app.db."),
    ] = None,
    hint_cache: Annotated[
        Path | None,
        typer.Option("--hint-cache", help="Read-only connector cache used to seal hint evidence."),
    ] = None,
) -> None:
    """Create a sealed database/result snapshot without copying audio or touching any corpus."""

    arguments = ["backup", "--work-root", str(work_root), "--into", str(into)]
    if database is not None:
        arguments.extend(("--database", str(database)))
    if hint_cache is not None:
        arguments.extend(("--hint-cache", str(hint_cache)))
    _snapshot_command(arguments)


@app.command("restore")
def restore_command(
    snapshot: Annotated[
        Path,
        typer.Option("--snapshot", help="Sealed snapshot directory to verify and restore."),
    ],
    work_root: Annotated[
        Path,
        typer.Option("--work-root", help="Work tree to restore; ID'er must be stopped first."),
    ] = DEFAULT_WORK_ROOT,
    database: Annotated[
        Path | None,
        typer.Option("--database", help="Database inside the work tree; defaults to .idea/app.db."),
    ] = None,
) -> None:
    """Restore the database and saved-result artefacts, not audio or any truth corpus."""

    arguments = ["restore", "--snapshot", str(snapshot), "--work-root", str(work_root)]
    if database is not None:
        arguments.extend(("--database", str(database)))
    _snapshot_command(arguments)


@app.command("verify-artefacts")
def verify_artefacts_command(
    snapshot: Annotated[
        Path,
        typer.Option("--snapshot", help="Sealed snapshot directory to check without changing it."),
    ],
) -> None:
    """Verify hashes, manifests and sidecars without changing work, data or the snapshot."""

    _snapshot_command(["verify", "--snapshot", str(snapshot)])


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

    _refuse_output_or_exit(path)  # before anything, --force included
    if path.exists() and not force:
        typer.echo(f"{path} already exists; pass --force to overwrite", err=True)
        raise typer.Exit(1)
    _validate_config_or_exit()
    path.parent.mkdir(parents=True, exist_ok=True)
    _refuse_output_or_exit(path)  # revalidated immediately before the write
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8", newline="\n")
    typer.echo(f"wrote {path}; edit it and keep it un-committed (it is git-ignored).")


def _refuse_output_or_exit(path: Path) -> None:
    """``truth.refuse_generated_output`` for a CLI destination, reported as a usage error."""

    try:
        refuse_generated_output(path)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


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
def gc(
    policy: str = typer.Option("local", "--policy", help="Retention policy: local or hosted."),
    work_root: Path = typer.Option(DEFAULT_WORK_ROOT, "--work-root"),  # noqa: B008
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Move expired artefacts to dated trash and purge old trash (default: dry run).",
    ),
) -> None:
    """Apply retention explicitly; without --apply, print a dry run and change nothing."""

    if policy not in {"local", "hosted"}:
        typer.echo("--policy must be local or hosted", err=True)
        raise typer.Exit(2)
    result = collect(work_root, policy=policy, apply=apply)  # type: ignore[arg-type]
    prefix = "" if apply else "would "
    for action in result.actions:
        verb = {"move": "move", "purge": "purge", "skip": "skip"}[action.operation]
        typer.echo(f"{prefix}{verb} {action.path}: {action.reason}")
    typer.echo(f"gc {'applied' if apply else 'dry run'}: {len(result.actions)} action(s)")


@app.command()
def cost(
    target: str = typer.Argument(..., help="Cached source URL or media key."),
    minutes: float | None = typer.Option(None, "--minutes", min=0.0001),
    work_root: Path = typer.Option(DEFAULT_WORK_ROOT, "--work-root"),  # noqa: B008
    config: Path = typer.Option(Path("idea.toml"), "--config"),  # noqa: B008
    profile: str | None = typer.Option(None, "--profile"),
    density: int | None = typer.Option(None, "--density", min=1, max=2),
) -> None:
    """Estimate Deep spending without downloading, decoding or changing the work tree."""
    from id_detector.cost import cached_mix, estimate

    loaded = _load_app_config(config)
    selected = profile if profile is not None else loaded.default_profile
    if selected is not None:
        loaded = effective_app_config(loaded, _load_profile_or_exit(selected))
    if density is not None:
        loaded = replace(loaded, deep_primary_density=density)
    cached = cached_mix(work_root, target)
    duration = cached.duration_ms if cached is not None else None
    if minutes is not None:
        import math

        if not math.isfinite(minutes):
            raise typer.BadParameter("minutes must be finite")
        duration = round(minutes * 60_000)
    if duration is None or duration <= 0:
        typer.echo("Length unknown. Supply --minutes to estimate; nothing was fetched or spent.")
        raise typer.Exit(2)
    typer.echo(
        estimate(duration, loaded).render(cached.scanned if cached else "No cached mix found.")
    )


def _confirm_paid(duration: int, windows: int, config: AppConfig, history: str, yes: bool) -> bool:
    from id_detector.cost import estimate

    typer.echo(estimate(duration, config, windows=windows).render(history))
    if yes:
        return True
    if not sys.stdin.isatty():
        typer.echo(
            "cancelled: paid confirmation requires an interactive terminal; "
            "use --yes for a scripted run.",
            err=True,
        )
        return False
    try:
        accepted = typer.confirm("Spend AudD credit on this Deep scan?", default=False)
    except (EOFError, typer.Abort):
        accepted = False
    if not accepted:
        typer.echo("cancelled: paid scan not confirmed; nothing reserved or spent.", err=True)
    return accepted


@app.command()
def analyse(
    url: str = typer.Argument(..., help="Public mix URL (or a local media file)."),
    raw: bool = typer.Option(False, "--raw", help="Print raw match tuples with mix times."),
    yes: bool = typer.Option(False, "--yes", help="Confirm paid spending without a prompt."),
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
    accept_degraded: bool = typer.Option(
        False, "--accept-degraded", help="Allow a compatible degraded result in local mode."
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
    keep_intermediates: bool = typer.Option(
        False,
        "--keep-intermediates",
        help="Pin this media's generated windows and decoded PCM against retention GC.",
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
        from id_detector.service import (
            LocalCheckpointStore,
            LocalPath,
            PipelineOptions,
            PlatformUrl,
            exit_code_for,
            run,
        )
        from id_detector.service import (
            RunRequest as ServiceRunRequest,
        )

        target = LocalPath(Path(url)) if Path(url).is_file() else PlatformUrl(url)
        from id_detector.run_ledger import new_run_id

        run_id = new_run_id()
        from id_detector.cost import cached_mix

        cached = cached_mix(work_root, url) if requested_recipe.name == "deep" else None
        paid_confirm = None
        if requested_recipe.name == "deep":

            def paid_confirm(duration: int, windows: int) -> bool:
                return _confirm_paid(
                    duration,
                    windows,
                    loaded_config,
                    cached.scanned if cached else "No stored scan found.",
                    yes,
                )

        store = LocalCheckpointStore(
            work_root,
            options=PipelineOptions(
                project_root=PROJECT_ROOT,
                print_raw=raw,
                refresh=refresh,
                refresh_states=selected_refresh_states,
                max_requests=max_requests,
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
                allow_degrade=allow_degrade,
                paid_scan_adapters=paid_scan_adapters,
                shazam_http_client=shazam_http_client,
                local_index_label=local_index,
                index_root=index_root,
                panako_tool_dir=panako_tool_dir,
                keep_intermediates=keep_intermediates,
                cli_paid_confirm=paid_confirm,
            ),
        )
        result = run(
            ServiceRunRequest(
                run_id=run_id,
                analysis_key="",
                target=target,
                recipe=requested_recipe,
                accept_degraded=accept_degraded,
                hints_snapshot_policy="disabled" if no_hints else "reuse",
                manual_tracklist=tracklist.read_bytes() if tracklist is not None else None,
                checkpoint_store=store,
                attempt_journal=None,
                usd_admitter=None,
                progress=None,
                cancel_token=None,
            )
        )
        # One mapping, in the service (§2.3.5): this command renders a RunResult, it does not
        # decide what a status means.
        exit_code = exit_code_for(result)
        if result.status == "failed" and result.reason:
            typer.echo(f"failed: {result.reason}", err=True)
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
    bundle: Path | None = None,
    result_paths: list[Path] | None = None,
) -> int:
    from id_detector.present.bundles import load_run_snapshot, publish_snapshot

    work_root = work_root.resolve()
    # ``bundle`` is the run the compatibility lookup selected (§3.4).  Without it acquisition would
    # reopen ``present/current`` — the newest complete run — and hand the caller a result it was
    # never served: a Deep bundle for a Free request with ``serve_free_from_deep`` off.
    media_dir: Path | None = bundle.parents[2] if bundle is not None else None
    if media_dir is None:
        cached = _load_cached(work_root, url)
        if cached is None:
            typer.echo(
                f"no cached analysis for {redact_text(url)} under {work_root}; run `analyse` first",
                err=True,
            )
            return 2
        media_dir = cached.media_dir
    try:
        # One snapshot for both halves: the links below and the rows the new bundle renders must
        # come from the same run, and that run owns the identity the bundle is published under.
        snapshot = load_run_snapshot(media_dir, directory=bundle)
    except (OSError, ValueError, KeyError) as error:
        typer.echo(
            f"analysis at {media_dir} cannot be opened ({error}); run `analyse` first", err=True
        )
        return 2

    _report(progress, "enrich", 0, 1, "resolving acquire links")
    cache_root = PROJECT_ROOT / "data" / "local" / "enrich"
    result = await enrich_media_dir(
        source=snapshot.source,
        media_dir=media_dir,
        cache_root=cache_root,
        refresh=refresh,
        enable_soundcloud=enable_soundcloud,
        episodes=snapshot.episodes,
        identities=snapshot.identities,
    )
    _report(progress, "enrich", 1, 1, "acquire links resolved")
    _report(progress, "present", 0, 1, "updating result page")
    acquire_config = _load_app_config(Path("idea.toml"))
    published = publish_snapshot(
        snapshot, media_dir=media_dir, config=acquire_config, acquire=result.record
    )
    if result_paths is not None:
        result_paths.append(published)
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

    from idea_web.server import make_server, require_loopback

    def _open() -> None:
        with contextlib.suppress(Exception):
            webbrowser.open(url)

    try:
        require_loopback(host)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    # The web process never runs a pipeline (plan §4.2): it enqueues into the durable local queue,
    # and a worker process that this command starts, restarts and stops runs the analyses.
    jobs = supervisor = None
    if analyse:
        from idea_web.jobs.local import LocalJobs, LocalWorkerSupervisor, MigrationRefused

        try:
            jobs = LocalJobs(work_root)
        except MigrationRefused as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(2) from None
        supervisor = LocalWorkerSupervisor(work_root, config_path=config, jobs=jobs)

    def _show_upkeep(report: object) -> None:
        for line in report.owner_status_lines():  # type: ignore[attr-defined]
            typer.echo(line, err=True)

    server = make_server(
        work_root,
        host=host,
        port=port,
        jobs=jobs,
        on_upkeep_finished=_show_upkeep,
    )
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    url = f"http://{bound_host}:{bound_port}"
    mode = "analyse + read-only" if analyse else "read-only"
    try:
        if supervisor is not None:
            supervisor.start()
        typer.echo(f"serving {work_root} at {url} ({mode}; Ctrl-C to stop)")
        if open_browser:
            # Open after the server is listening; a browser failure must never stop the server.
            threading.Timer(0.4, _open).start()
        server.serve_forever()
    except KeyboardInterrupt:
        typer.echo("stopping", err=True)
    finally:
        server.shutdown()
        server.server_close()
        if supervisor is not None:
            supervisor.stop()


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
    if not certification_enabled():
        typer.echo(f"certification: {CERTIFICATION_DISABLED}. {CERTIFICATION_DISABLED_NEXT_STEP}")


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
                work_root=DEFAULT_WORK_ROOT,
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
    except CertificationDisabled as exc:
        # The exact disabled message, then what to do about it.
        typer.echo(f"{exc}. {CERTIFICATION_DISABLED_NEXT_STEP}", err=True)
        raise typer.Exit(2) from None
    except (
        CorpusNotFrozen,
        CorpusNotIndependent,
        DuplicateTestVersion,
    ) as exc:
        typer.echo(redact_text(str(exc)), err=True)
        raise typer.Exit(2) from None
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        typer.echo(redact_text(str(exc)), err=True)
        raise typer.Exit(1) from None
    # "certified" only when something was: a run in which no triple certifies was evaluated.
    verb = "certified" if result.n_certified else "evaluated"
    typer.echo(
        f"{verb} corpus={corpus} profile={profile} test_version={test_version}; "
        f"certified_triples={result.n_certified}; n_test_predictions={result.n_test_predictions}; "
        f"report={result.report_path}"
    )


@benchmark_app.command("calibration-validate")
def benchmark_calibration_validate(
    corpus: Annotated[str, typer.Option("--corpus", help="Frozen controlled corpus version.")],
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Validation report JSON (default data/local/calibration/<corpus>).",
        ),
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

    _refuse_output_or_exit(out)  # before anything is read

    from id_detector.contracts import AcquireFile

    paths = _collect_acquire_files(episodes)
    if not paths:
        typer.echo(f"no acquire.json found under {episodes}", err=True)
        raise typer.Exit(2)
    records = [AcquireFile.model_validate_json(read_text(path)) for path in paths]
    sheet = build_link_sample(records, sample_size=sample)
    _refuse_output_or_exit(out)  # revalidated immediately before the write
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

    if out is not None:
        _refuse_output_or_exit(out)  # before anything is read

    sheet = json.loads(read_text(marked))
    score = score_link_sample(sheet)
    if out is not None:
        _refuse_output_or_exit(out)  # revalidated immediately before the write
        atomic_write_json(out, score)
    gate = score["gate"]
    # While certification is disabled the gate is not judged: print its status, never a pass.
    verdict = (
        f"gate_pass={str(gate['pass']).lower()}"
        if gate["pass"] is not None
        else f"gate_status={gate['status']}"
    )
    typer.echo(
        f"marked={score['marked_links']} correct={score['correct']} "
        f"incorrect={score['incorrect']} precision_e4={score['precision_e4']} "
        f"one_sided_95_lower_e4={score['one_sided_95_lower_e4']} {verdict}"
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
            work_root=DEFAULT_WORK_ROOT,
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
            truth,
            annotator_ref=annotator_ref,
            audio=audio,
            annotation_path=annotation,
            work_root=DEFAULT_WORK_ROOT,
        )
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"saved {len(updated.episodes)} episodes to {truth}")


@truth_app.command("review")
def truth_review(
    set_id: Annotated[str, typer.Option("--set", help="The ground-truth record's set_id.")],
    corpus: Annotated[Path, typer.Option("--corpus", help="Corpus directory to search.")] = Path(
        "data/corpus/release-1"
    ),
    work_root: Annotated[Path, typer.Option("--work-root")] = DEFAULT_WORK_ROOT,
    port: Annotated[int, typer.Option("--port", min=0, max=65535)] = 8797,
    open_browser: Annotated[
        bool,
        typer.Option("--open/--no-open", help="Open the review page in the default browser."),
    ] = True,
) -> None:
    """Review and first-pass verify one truth set in a loopback-only browser screen."""

    import contextlib
    import threading
    import webbrowser

    from id_detector.truth_review import TruthReviewSession, find_truth_path
    from idea_web.truth_review import make_truth_review_server

    try:
        # The work tree is regenerable cache that idea gc prunes: a truth record resolving
        # inside it would be the one corpus loss nothing can undo, so --corpus is checked, not
        # trusted.
        truth_path = find_truth_path(corpus, set_id, work_root=work_root)
        session = TruthReviewSession(truth_path, work_root=work_root)
        server = make_truth_review_server(session, port=port)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    url = f"http://{bound_host}:{bound_port}"
    audio = "audio ready" if session.audio_path is not None else "audio unavailable; editing works"
    typer.echo(f"reviewing {set_id} at {url} ({audio}; Ctrl-C to stop)")
    if open_browser:

        def _open() -> None:
            with contextlib.suppress(Exception):
                webbrowser.open(url)

        threading.Timer(0.4, _open).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        typer.echo("stopping", err=True)
    finally:
        server.shutdown()
        server.server_close()
        server.server_close()


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
            truth,
            annotator_ref=annotator_ref,
            audio=audio,
            annotation_path=annotation,
            work_root=DEFAULT_WORK_ROOT,
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
        updated = resolve_truth(
            truth,
            resolver_ref=resolver_ref,
            annotation_path=annotation,
            work_root=DEFAULT_WORK_ROOT,
        )
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"saved third-annotator resolution for {len(updated.episodes)} episodes to {truth}")


@truth_app.command("freeze")
def truth_freeze(
    truth: Annotated[
        Path,
        typer.Option(
            "--truth", help="The <corpus> directory; sets must be <corpus>/<set>/ground_truth.json."
        ),
    ],
    corpus_version: Annotated[str, typer.Option("--corpus-version")],
    out: Annotated[
        Path,
        typer.Option(
            "--out",
            help=(
                "Must be <corpus>/corpus-version.json, directly in the corpus directory being "
                "frozen: the only place scoring, certification and review look for it."
            ),
        ),
    ],
) -> None:
    """Validate complete verification and hash a frozen corpus manifest."""
    try:
        manifest = freeze_truth(
            truth, corpus_version=corpus_version, out_path=out, work_root=DEFAULT_WORK_ROOT
        )
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    exposed = manifest["exposed_sets"]
    typer.echo(f"froze {len(manifest['sets'])} sets as {corpus_version}; manifest={out}")
    if exposed:
        typer.echo(
            f"NOT CERTIFIABLE: {', '.join(exposed)} was reviewed with IDea's predictions visible; "
            "the manifest records and hashes that evidence, so those sets can be scored but never "
            "back an L3 or certification claim"
        )


@truth_app.command("manifest-draft")
def truth_manifest_draft(
    truth: Annotated[
        Path,
        typer.Option(
            "--truth", help="The <corpus> directory; the inventory is written to its own root."
        ),
    ],
    corpus_version: Annotated[str, typer.Option("--corpus-version")],
) -> None:
    """Inventory unverified seeds without claiming that they are frozen truth.

    The inventory is always written to ``<corpus>/corpus-version.json``; there is no ``--out``, so a
    draft can never target another corpus's manifest.
    """

    try:
        manifest = write_draft_manifest(
            truth, corpus_version=corpus_version, work_root=DEFAULT_WORK_ROOT
        )
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo(
        f"recorded {len(manifest['sets'])} unverified draft sets; frozen=false; "
        f"manifest={truth / FREEZE_MANIFEST_NAME}"
    )


if __name__ == "__main__":
    app()

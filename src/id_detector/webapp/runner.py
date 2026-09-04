"""The real pipeline runner behind the web app's job manager.

It runs exactly what the ``analyse`` (and optionally ``acquire`` / ``build-index``) CLI commands
run — reusing ``id_detector.cli._analyse`` / ``._acquire`` so the artefact contracts and
``work/<keys>/…`` layout are identical — translating the pipeline's progress hook into
:class:`~id_detector.webapp.jobs.JobContext` updates.  The job manager keeps this deliberately
pluggable: tests inject a fake runner instead and never touch the network.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path

from id_detector.providers.base import AppConfig
from id_detector.webapp.jobs import JobContext


@dataclass(frozen=True)
class _RunSettings:
    config: AppConfig
    calibrator: object | None
    novelty: bool
    no_hints: bool
    max_requests: int
    max_generations: int


def _resolve_settings(project_root: Path, config_path: Path, profile: str | None) -> _RunSettings:
    """Mirror the ``analyse`` command's config+profile precedence (file prefs, profile geometry).

    Kept in lock-step with ``id_detector.cli.analyse``: the file config supplies preferences
    (budget, lead-in, cache TTLs, hint switches); a frozen profile is the authority on engines,
    transform/schedule/rescan geometry and the novelty/hints toggles, and supplies a calibrator when
    a frozen artefact exists.
    """

    from id_detector.calibrate.model import load_calibration
    from id_detector.profiles import UnknownProfile, load_profile, profile_app_config

    file_config = AppConfig.load(config_path) if config_path.is_file() else AppConfig()
    no_hints = not file_config.hints_enabled
    selected = profile if profile is not None else file_config.default_profile
    if selected is not None:
        try:
            frozen = load_profile(project_root, selected)
        except UnknownProfile:
            frozen = None
        if frozen is not None:
            loaded = replace(
                profile_app_config(frozen),
                allow_third_party_upload=file_config.allow_third_party_upload,
                default_profile=file_config.default_profile,
                max_requests=file_config.max_requests,
                lead_in_ms=file_config.lead_in_ms,
                cache_positive_max_age_days=file_config.cache_positive_max_age_days,
                cache_no_match_max_age_days=file_config.cache_no_match_max_age_days,
                hints_enabled=file_config.hints_enabled,
                disabled_hint_connectors=file_config.disabled_hint_connectors,
            )
            return _RunSettings(
                config=loaded,
                calibrator=load_calibration(project_root, frozen.name),
                novelty=frozen.novelty_enabled,
                no_hints=no_hints or not frozen.hints_enabled,
                max_requests=loaded.max_requests,
                max_generations=loaded.rescan_max_generations,
            )
    return _RunSettings(
        config=file_config,
        calibrator=None,
        novelty=True,
        no_hints=no_hints,
        max_requests=file_config.max_requests,
        max_generations=file_config.rescan_max_generations,
    )


def make_pipeline_runner(
    work_root: Path,
    *,
    project_root: Path | None = None,
    config_path: Path | None = None,
):
    """Build a runner that executes the real analyse pipeline for a submitted job."""

    from id_detector import cli

    root = Path(work_root)
    project = project_root if project_root is not None else cli.PROJECT_ROOT
    config_file = config_path if config_path is not None else Path("id-detector.toml")

    def runner(ctx: JobContext) -> None:
        target = ctx.target

        if ctx.build_index:
            ctx.progress("build_index", 0, 1, "building reference index")
            _run_build_index(ctx, target, project_root=project)
            ctx.check_cancel()

        settings = _resolve_settings(project, config_file, ctx.profile)

        def progress(phase: str, done: int, total: int, message: str = "") -> None:
            ctx.progress(phase, done, total, message)

        asyncio.run(
            cli._analyse(
                target,
                work_root=root,
                print_raw=False,
                refresh=False,
                max_requests=settings.max_requests,
                tracklist=None,
                no_hints=settings.no_hints,
                app_config=settings.config,
                max_generations=settings.max_generations,
                novelty=settings.novelty,
                calibrator=settings.calibrator,
                progress=progress,
            )
        )

        if ctx.acquire:
            ctx.check_cancel()
            asyncio.run(
                cli._acquire(
                    target,
                    work_root=root,
                    refresh=False,
                    enable_soundcloud=True,
                    progress=progress,
                )
            )

        cached = cli._load_cached(root.resolve(), target)
        if cached is not None:
            index = cached.media_dir / "present" / "index.html"
            if index.is_file():
                ctx.set_result(index)

    return runner


def _run_build_index(ctx: JobContext, target: str, *, project_root: Path) -> None:
    """Best-effort reference-index build (discover uploader uploads, fingerprint them).

    This mirrors ``id-detector build-index <set-url> --index``.  It needs a JDK/Panako runtime and
    network access, so any failure (no runtime, discovery/download error) is logged and the job
    continues to the audio analysis rather than failing — the reference index only *augments* it.
    """

    try:
        from id_detector.candidates import (
            deduplicate_candidates,
            discover_candidates,
            index_candidates,
        )
        from id_detector.providers.panako import (
            PanakoIndexPaths,
            PanakoProvider,
            PanakoRuntime,
        )
        from id_detector.providers.panako_setup import jar_path

        tool_dir = Path("data/local/panako")
        index_dir = Path("data/local/panako-db") / "default"
        candidates = asyncio.run(discover_candidates(set_url=target, artists=[], extra_urls=[]))
        candidates = deduplicate_candidates(list(candidates))
        ctx.log(f"reference candidates discovered: {len(candidates)}")
        if not candidates:
            return
        ctx.check_cancel()
        runtime = PanakoRuntime.resolve(jar=jar_path(tool_dir))
        provider = PanakoProvider(runtime=runtime, paths=PanakoIndexPaths(root=index_dir))
        resources = asyncio.run(
            index_candidates(provider, candidates, download_dir=index_dir / "downloads")
        )
        ctx.log(f"reference index: fingerprinted {len(resources)} track(s)")
    except Exception as exc:  # noqa: BLE001 - the index is optional; never fail the whole job
        from id_detector.io import redact_text

        ctx.log(f"reference index skipped: {redact_text(str(exc))[:200]}")

"""The real pipeline runner behind the web app's job manager.

It runs exactly what the ``analyse`` (and optionally ``acquire`` / ``build-index``) CLI commands
run — reusing ``id_detector.cli._analyse`` / ``._acquire`` so the artefact contracts and
``work/<keys>/…`` layout are identical — translating the pipeline's progress hook into
:class:`~id_detector.webapp.jobs.JobContext` updates.  The job manager keeps this deliberately
pluggable: tests inject a fake runner instead and never touch the network.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from id_detector.providers.base import AppConfig
from id_detector.shazam_breaker import ShazamBreaker
from id_detector.webapp.jobs import STAGE_LABELS, JobContext, JobWaiting

#: Where the browser's "build index" step fingerprints the uploader's tracks, and the label the
#: analysis then queries (D3: Panako kept; the index is built AND used).  Mirrors ``idea
#: build-index`` / ``idea analyse --local-index default`` on their default roots.
WEB_INDEX_LABEL = "default"
WEB_INDEX_ROOT = Path("data/local/panako-db")
WEB_PANAKO_TOOL_DIR = Path("data/local/panako")
#: ``_analyse`` refused to start new Shazam work: the job waits instead of failing.
WAITING_EXIT = 6
#: What each non-zero ``_analyse`` exit code means to a browser job (plan §2.3.5).
_EXIT_STATUS = {
    1: "failed",
    3: "paid provider unavailable",
    4: "budget exhausted",
    5: "source changed",
    # U-F31: this string reaches the progress page as the job's live message, so it names no
    # engine.  The engine-level detail stays in the journal and in ``JobWaiting``'s docstring.
    WAITING_EXIT: "waiting: not queued locally; the free recognition service is paused",
}


def _this_runs_entry(journal: Path, started_at: float | None) -> dict | None:
    """The journal entry belonging to the run that started at ``started_at``, newest first.

    A mix analysed twice has two entries; attributing an *earlier* run's spend to this attempt is
    exactly the kind of made-up money figure U-F15 is about, so an entry that predates this job is
    not this job's.
    """

    import json
    from datetime import datetime

    try:
        lines = [line for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
            stamp = str(entry.get("started_at") or "")
            moment = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError, AttributeError):
            continue
        # One second of slack: the job clock and the pipeline clock are the same wall clock, but the
        # journal stamp is second-resolution on some platforms.
        if started_at is None or moment >= started_at - 1.0:
            return entry
    return None


def _record_outcome(ctx: JobContext, work_root: Path, target: str) -> None:
    """Read this run's terminal journal entry back onto the job (U-F15).

    The journal is the frozen §2.3.5 record — status, reason and the money that was *settled* —
    written on every terminal path that reached a media directory.  No media directory means the
    run stopped before a single window could be reserved, so nothing was spent: that is knowledge,
    not a guess, and only an unreadable journal leaves the cost genuinely unknown.
    """

    from id_detector import cli

    # Bookkeeping must never mask the job's own outcome: this runs on the way out of a failure or a
    # cancellation, so anything it raises would replace the real exception with an AttributeError.
    record = getattr(ctx, "set_outcome", None)
    if record is None:  # a duck-typed context that does not carry the outcome contract
        return
    try:
        cached = cli._load_cached(Path(work_root).resolve(), target)
        if cached is None:
            record(usd_e2_spent=0, spend_known=True)
            return
        journal = Path(cached.media_dir) / "invocations.jsonl"
        entry = _this_runs_entry(journal, getattr(ctx, "started_at", None))
        if entry is None:
            # Either nothing was journalled for this run, or only *earlier* runs are in the journal:
            # in both cases this attempt never reached the point where money is admitted.
            record(usd_e2_spent=0, spend_known=True)
            return
        timings = entry.get("timings") or {}
        stage = next((label for key, label in reversed(STAGE_LABELS) if key in timings), None)
        spent = entry.get("usd_e2_spent")
        record(
            run_status=str(entry.get("status")) if entry.get("status") else None,
            run_reason=str(entry.get("reason")) if entry.get("reason") else None,
            last_stage=stage,
            usd_e2_spent=int(spent) if isinstance(spent, int) else None,
            spend_known=isinstance(spent, int),
        )
    except Exception:  # noqa: BLE001 - an unreadable record is "cost unknown", never a new failure
        with suppress(Exception):
            record(spend_known=False)


@dataclass(frozen=True)
class _RunSettings:
    config: AppConfig
    calibrator: object | None
    novelty: bool
    no_hints: bool
    max_requests: int
    max_generations: int
    #: Engines the frozen profile fixes; only ``max_accuracy`` lists paid file_scanners.
    enabled_engines: tuple[str, ...] = ()


def _resolve_settings(project_root: Path, config_path: Path, profile: str | None) -> _RunSettings:
    """Mirror the ``analyse`` command's config+profile precedence (file prefs, profile geometry).

    Shares ``profiles.effective_app_config`` with ``id_detector.cli.analyse`` so the two cannot
    drift: the file config supplies every preference (budget, lead-in, cache TTLs, hint switches,
    ``[recognise]``, ``[deep]``, ``[present]``); a frozen profile is the authority on engines,
    transform/schedule/rescan geometry and the novelty/hints toggles, and supplies a calibrator
    when a frozen artefact exists.
    """

    from id_detector.calibrate.model import load_calibration
    from id_detector.profiles import UnknownProfile, effective_app_config, load_profile

    file_config = AppConfig.load(config_path)
    no_hints = not file_config.hints_enabled
    selected = profile if profile is not None else file_config.default_profile
    if selected is not None:
        try:
            frozen = load_profile(project_root, selected)
        except UnknownProfile:
            frozen = None
        if frozen is not None:
            # The same resolver `idea analyse` and `config show` use (review H6): the profile
            # fixes the geometry, every file preference is carried, and the config rescan ceiling
            # caps the profile (default 0 = rescans off; raise [rescan] max_generations to opt in).
            loaded = effective_app_config(file_config, frozen)
            return _RunSettings(
                config=loaded,
                calibrator=load_calibration(project_root, frozen.name),
                novelty=frozen.novelty_enabled,
                no_hints=no_hints or not frozen.hints_enabled,
                max_requests=loaded.max_requests,
                max_generations=loaded.rescan_max_generations,
                enabled_engines=tuple(frozen.enabled_engines),
            )
    return _RunSettings(
        config=file_config,
        calibrator=None,
        novelty=True,
        no_hints=no_hints,
        max_requests=file_config.max_requests,
        max_generations=file_config.rescan_max_generations,
    )


def _materialise_tracklist(work_root: Path, pasted: str | None) -> Path | None:
    """Write a pasted known tracklist to a stable UTF-8 file for the manual-tracklist hint path.

    The file is content-addressed under ``<work_root>/.tracklists`` so repeated submissions of the
    same text reuse one file, and blank/whitespace-only input means "audio only" (no file).
    """

    if pasted is None:
        return None
    text = pasted.strip()
    if not text:
        return None
    body = text.encode("utf-8")
    digest = sha256(body).hexdigest()[:16]
    path = Path(work_root) / ".tracklists" / f"{digest}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        path.write_bytes(body)
    return path


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
    config_file = config_path if config_path is not None else Path("idea.toml")

    shazam_breaker = ShazamBreaker(AppConfig.load(config_file).shazam_breaker)

    def _run_job(ctx: JobContext) -> None:
        target = ctx.target

        if ctx.build_index:
            ctx.progress("build_index", 0, 1, "building reference index")
            _run_build_index(ctx, target, project_root=project)
            ctx.check_cancel()

        settings = _resolve_settings(project, config_file, ctx.profile)
        tracklist_path = _materialise_tracklist(root, ctx.known_tracklist)

        def progress(phase: str, done: int, total: int, message: str = "") -> None:
            ctx.progress(phase, done, total, message)

        from id_detector.recipes import get_recipe

        selected_recipe = get_recipe(
            "deep" if ctx.profile == "max_accuracy" else "free",
            primary_density=settings.config.deep_primary_density
            if ctx.profile == "max_accuracy"
            else 1,
        )
        result_paths: list[Path] = []
        exit_code = asyncio.run(
            cli._analyse(
                target,
                work_root=root,
                print_raw=False,
                refresh=False,
                max_requests=settings.max_requests,
                tracklist=tracklist_path,
                no_hints=settings.no_hints,
                app_config=settings.config,
                max_generations=settings.max_generations,
                novelty=settings.novelty,
                calibrator=settings.calibrator,
                enabled_engines=settings.enabled_engines,
                # The recipe owns engine selection and the shared compatible-result lookup.
                recipe=selected_recipe,
                shazam_breaker=shazam_breaker,
                result_paths=result_paths,
                # The index this job just built (or one an earlier job built) is queried over the
                # still-uncertain spans; without a label the build was paid for and never used.
                local_index_label=WEB_INDEX_LABEL if ctx.build_index else None,
                index_root=WEB_INDEX_ROOT,
                panako_tool_dir=WEB_PANAKO_TOOL_DIR,
                progress=progress,
                # The paid sweep polls this before each dispatch so a cancel stops new AudD
                # requests while the clips in flight resolve (their spend is journalled).
                cancel_token=ctx.cancel_token,
            )
        )
        if exit_code == WAITING_EXIT:
            # Plan §2.3.5: the breaker (or the kill-switch) refused a new Free request. The job
            # waits — it did not fail — so the manager records ``waiting``, not ``failed``.
            raise JobWaiting(_EXIT_STATUS[WAITING_EXIT])
        if exit_code != 0:
            meaning = _EXIT_STATUS.get(exit_code, "error")
            raise RuntimeError(f"analysis failed: {meaning} (exit code {exit_code})")

        selected = result_paths[-1] if result_paths else None
        if ctx.acquire:
            ctx.check_cancel()
            asyncio.run(
                cli._acquire(
                    target,
                    work_root=root,
                    refresh=False,
                    enable_soundcloud=True,
                    progress=progress,
                    # Acquisition re-publishes a revision of the run the pipeline selected, so the
                    # job still delivers that run and not whichever bundle is newest.
                    bundle=selected,
                    result_paths=result_paths,
                )
            )
            selected = result_paths[-1] if result_paths else None

        if selected is not None and (selected / "index.html").is_file():
            ctx.set_result(selected / "index.html")
            return
        cached = cli._load_cached(root.resolve(), target)
        if cached is not None:
            from id_detector.present.bundles import shown_result_dir

            index = shown_result_dir(cached.media_dir) / "index.html"
            if index.is_file():
                ctx.set_result(index)

    def runner(ctx: JobContext) -> None:
        """Run the job, and whatever happens leave its frozen outcome on the job (U-F15).

        Failure and cancellation both unwind through here, so the terminal status, reason, last
        completed stage and settled spend are read back from the journal *once* — the UI then states
        the cost instead of hedging about it.
        """

        try:
            _run_job(ctx)
        except BaseException:
            _record_outcome(ctx, root, ctx.target)
            raise
        _record_outcome(ctx, root, ctx.target)

    return runner


def _run_build_index(ctx: JobContext, target: str, *, project_root: Path) -> None:
    """Best-effort reference-index build (discover uploader uploads, fingerprint them).

    This mirrors ``idea build-index <set-url> --index``.  It needs a JDK/Panako runtime and
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

        tool_dir = WEB_PANAKO_TOOL_DIR
        index_dir = WEB_INDEX_ROOT / WEB_INDEX_LABEL
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

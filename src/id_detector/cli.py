"""Command-line entry point."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from hashlib import sha256
from pathlib import Path
from typing import Annotated

import typer

from id_detector.benchmark.controlled import render_controlled
from id_detector.benchmark.corpus import run_corpus
from id_detector.benchmark.scorer import score_corpus
from id_detector.benchmark.shortlist import run_shortlist
from id_detector.calibration import calibrate_shazam
from id_detector.contracts import SourceRecord
from id_detector.decode import decode
from id_detector.doctor import run_doctor
from id_detector.fuse.episodes import fuse_generation_zero
from id_detector.ingest import ingest
from id_detector.io import redact_text
from id_detector.jobs import AsyncJobStore, ProcessLock
from id_detector.journal import InvocationTimer, append_invocation
from id_detector.present import export_tracklist
from id_detector.providers.base import AppConfig
from id_detector.recognise import recognise_generation_zero
from id_detector.truth import (
    freeze_truth,
    resolve_truth,
    second_pass_truth,
    seed_truth,
    verify_truth,
    write_draft_manifest,
)
from id_detector.windows import generate_windows

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
benchmark_app = typer.Typer(no_args_is_help=True)
truth_app = typer.Typer(no_args_is_help=True)
app.add_typer(benchmark_app, name="benchmark")
app.add_typer(truth_app, name="truth")
PROJECT_ROOT = Path.cwd()
DEFAULT_WORK_ROOT = Path("work")


@app.callback()
def main() -> None:
    """Evidence-first DJ-set identification."""

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


async def _analyse(
    url: str,
    *,
    work_root: Path,
    print_raw: bool,
    refresh: bool,
    max_requests: int,
) -> int:
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
        timer.start_stage("ingest_ms")
        ingested = await ingest(url, work_root)
        timer.finish_stage("ingest_ms")
        media_dir = ingested.media_dir
        acquired_media_lock = ProcessLock(media_dir / ".media.lock")
        acquired_media_lock.acquire()
        media_lock = acquired_media_lock
        source_ids = [f"source:{ingested.record.source_key}"]

        timer.start_stage("decode_ms")
        decoded = await decode(ingested)
        timer.finish_stage("decode_ms")
        ffmpeg_version = decoded.record.decoder.ffmpeg_version

        timer.start_stage("windows_ms")
        windows = generate_windows(decoded, media_dir)
        timer.finish_stage("windows_ms")

        timer.start_stage("recognise_ms")
        recognised = await recognise_generation_zero(
            media_key=ingested.record.media_key,
            media_dir=media_dir,
            windows=windows,
            project_root=PROJECT_ROOT,
            run_id=run_id,
            refresh=refresh,
            max_requests=max_requests,
        )
        timer.finish_stage("recognise_ms")
        matches = sorted(
            (item for item in recognised.observations if item.status == "match"),
            key=lambda item: (item.mix_span_ms[0], item.id),
        )
        counts.update(
            {
                "requests": recognised.requests,
                "physical_attempts": recognised.physical_attempts,
                "matches": len(matches),
                "failures": recognised.failures,
                "cache_hits": recognised.cache_hits,
            }
        )
        timer.start_stage("fuse_ms")
        fused = fuse_generation_zero(
            media_key=ingested.record.media_key,
            media_dir=media_dir,
            duration_ms=decoded.record.pcm.duration_ms,
            observations=recognised.observations,
            observations_path=recognised.observations_path,
            windows=windows.records,
            windows_path=windows.record_path,
            pcm_path=decoded.record_path,
        )
        timer.finish_stage("fuse_ms")
        timer.start_stage("export_ms")
        exported = export_tracklist(
            media_dir=media_dir,
            media_key=ingested.record.media_key,
            duration_ms=decoded.record.pcm.duration_ms,
            episodes=fused.episodes,
            identities=fused.identities.record,
            episodes_path=fused.final_path,
            identities_path=fused.identities_path,
        )
        timer.finish_stage("export_ms")
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
                f"{recognised.physical_attempts} physical attempts; "
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


@app.command()
def analyse(
    url: str = typer.Argument(..., help="Public mix URL (or a local media file)."),
    raw: bool = typer.Option(False, "--raw", help="Print raw match tuples with mix times."),
    refresh: bool = typer.Option(False, "--refresh", help="Bypass positive/no-match TTLs."),
    work_root: Path = typer.Option(DEFAULT_WORK_ROOT, "--work-root"),  # noqa: B008
    max_requests: int = typer.Option(2_000, "--max-requests", min=1),
) -> None:
    """Run the full generation-zero pipeline and export a flattened tracklist."""
    try:
        exit_code = asyncio.run(
            _analyse(
                url,
                work_root=work_root,
                print_raw=raw,
                refresh=refresh,
                max_requests=max_requests,
            )
        )
    except KeyboardInterrupt:
        typer.echo("cancelled; safe job states were restored", err=True)
        raise typer.Exit(130) from None
    raise typer.Exit(exit_code)


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
) -> None:
    """Render the deterministic controlled-transform slice through FFmpeg."""
    try:
        result = asyncio.run(render_controlled(sources, out, seed=seed, audio_dir=audio_out))
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


@benchmark_app.command("shortlist")
def benchmark_shortlist(
    corpus: Annotated[str, typer.Option("--corpus", help="Controlled corpus version.")],
    out: Annotated[Path, typer.Option("--out", help="Shortlist report JSON.")],
    config: Annotated[
        Path, typer.Option("--config", help="Non-secret TOML config with the upload gate.")
    ] = Path("id-detector.toml"),
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
                app_config=AppConfig.load(config),
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

"""Corpus layout, end-to-end benchmark execution, and baseline regression comparison."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from id_detector.benchmark.scorer import (
    PredictionDocument,
    ScoringConfigSnapshot,
    paired_non_inferiority,
    score_corpus,
    truth_is_frozen_verified,
)
from id_detector.contracts import (
    BenchmarkCost,
    BenchmarkReportRecord,
    GroundTruthRecord,
    IdentitiesRecord,
)
from id_detector.decode import decode
from id_detector.fuse.alignment import (
    CONTINUATION_GAP_MS,
    DRIFT_DELTA_E4,
    HYPOTHESIS_AGREEMENT_E4,
    MAX_RATE_E4,
    MIN_RATE_E4,
    REPLAY_GAP_MS,
    RESIDUAL_GATE_MS,
)
from id_detector.fuse.episodes import FusionResult, fuse_generation_zero
from id_detector.ingest import ingest
from id_detector.io import atomic_write_json, canonical_json_bytes, read_text
from id_detector.local_fixture import (
    PROVIDER_CONFIG_VERSION as LOCAL_FIXTURE_CONFIG_VERSION,
)
from id_detector.local_fixture import build_recorded_response_map, recognise_controlled_fixture
from id_detector.present import export_tracklist
from id_detector.process import run_process
from id_detector.recognise import load_provider_config
from id_detector.semantics import RECORDING_NAMESPACES
from id_detector.windows import HOP_MS, WINDOW_MS, generate_windows

SOURCE_DURATION_TOLERANCE_MS = 500


def _validate_source_media(truth: GroundTruthRecord, *, media_key: str, duration_ms: int) -> None:
    if media_key != truth.source.media_key:
        raise ValueError(f"source SHA-256 differs from truth media_key for {truth.set_id}")
    if abs(duration_ms - truth.source.duration_ms) > SOURCE_DURATION_TOLERANCE_MS:
        raise ValueError(f"decoded duration differs from truth for {truth.set_id}")


@dataclass(frozen=True)
class CorpusRunResult:
    report: BenchmarkReportRecord
    predictions_path: Path


def _truth_files(corpus_dir: Path, set_id: str | None) -> list[Path]:
    paths = sorted(corpus_dir.rglob("ground_truth.json"))
    if set_id is not None:
        paths = [
            path
            for path in paths
            if GroundTruthRecord.model_validate_json(read_text(path)).set_id == set_id
        ]
    if not paths:
        suffix = f" for set {set_id}" if set_id else ""
        raise ValueError(f"corpus contains no ground truth{suffix}: {corpus_dir}")
    return paths


def _source_links(project_root: Path) -> dict[str, str]:
    path = project_root / "data" / "local" / "source_links.json"
    return json.loads(read_text(path)) if path.is_file() else {}


def _local_media(project_root: Path, corpus_dir: Path, set_id: str) -> Path | None:
    candidates: list[Path] = []
    for directory in (
        project_root / "data" / "local" / "media",
        corpus_dir / "media",
    ):
        if directory.is_dir():
            candidates.extend(path for path in directory.glob(f"{set_id}.*") if path.is_file())
            nested = directory / set_id
            if nested.is_dir():
                candidates.extend(path for path in nested.iterdir() if path.is_file())
    return sorted(candidates)[0] if candidates else None


def _controlled_audio(project_root: Path, set_id: str) -> Path | None:
    candidates = sorted(
        (project_root / "data" / "local" / "controlled").glob(f"*/{set_id}/mix.wav")
    )
    return candidates[0] if candidates else None


def _cached_link_source(work_root: Path, media_key: str, source_link: str | None) -> str | None:
    if source_link is None or not work_root.is_dir():
        return None
    for source_dir in work_root.iterdir():
        if (source_dir / media_key / "ingest" / "source.json").is_file():
            return source_link
    return None


def _source_offset(project_root: Path, set_id: str) -> int:
    for path in sorted(
        (project_root / "data" / "fixtures" / "controlled").rglob("render_manifest.json")
    ):
        payload = json.loads(read_text(path))
        for item in payload.get("sets", []):
            if item.get("set_id") == set_id:
                return int(item.get("source_offset_ms", 0))
    return 0


def _load_fusion(media_dir: Path) -> tuple[IdentitiesRecord, Any]:
    identities = IdentitiesRecord.model_validate_json(
        read_text(media_dir / "fuse" / "identities.gen0.json")
    )
    from id_detector.contracts import EpisodesFile

    episodes = EpisodesFile.model_validate_json(read_text(media_dir / "fuse" / "episodes.json"))
    return identities, episodes


def _label_for_candidate(identities: IdentitiesRecord, candidate_id: str) -> tuple[str, str]:
    candidate = next(item for item in identities.candidates if item.canonical_id == candidate_id)
    work = next(item for item in identities.works if item.work_id == candidate.work_id)
    nodes = [node for node in identities.nodes if node.id in work.member_nodes]
    text_labels = [node.label for node in nodes if node.ns == "text"]
    label = min(
        text_labels or [node.label for node in nodes], default="Unknown artist - Unknown title"
    )
    if " - " not in label:
        return "Unknown artist", label
    artist, title = label.split(" - ", 1)
    return artist, title


def _prediction_set(set_id: str, fusion: FusionResult | None, media_dir: Path) -> dict[str, Any]:
    if fusion is None:
        identities, episodes = _load_fusion(media_dir)
    else:
        identities, episodes = fusion.identities.record, fusion.episodes
    candidates = {item.canonical_id: item for item in identities.candidates}
    scored = []
    for episode in episodes.episodes:
        artist, title = _label_for_candidate(identities, episode.candidate_id)
        version_ids: dict[str, str] = {}
        for node in candidates[episode.candidate_id].member_nodes:
            namespace, value = node.split(":", 1)
            if namespace in RECORDING_NAMESPACES:
                version_ids.setdefault(namespace, value)
        scored.append(
            {
                "work": {"artist": artist, "title": title},
                "version": {"qualifier": None, "ids": version_ids},
                "candidate_id": episode.candidate_id,
                "evidence_support_ms": episode.evidence_support_ms,
                "start_no_later_than_ms": episode.start_no_later_than_ms,
                "end_no_earlier_than_ms": episode.end_no_earlier_than_ms,
                "start_pi": episode.start_pi,
                "end_pi": episode.end_pi,
                "best_start_ms": episode.best_start_ms,
                "best_end_ms": episode.best_end_ms,
                "role_segments": episode.role_segments,
                "occurrence_index": episode.occurrence_index,
                "claim": episode.claim,
                "scores": episode.scores,
                "tiers": episode.tiers,
                "alignment_events": episode.alignment_events,
            }
        )
    return {
        "set_id": set_id,
        "identities": identities,
        "episodes": scored,
    }


async def _run_controlled(
    truth: GroundTruthRecord,
    audio: Path,
    *,
    project_root: Path,
    work_root: Path,
) -> tuple[Path, FusionResult, BenchmarkCost]:
    ingested = await ingest(str(audio), work_root)
    decoded = await decode(ingested)
    _validate_source_media(
        truth,
        media_key=ingested.record.media_key,
        duration_ms=decoded.record.pcm.duration_ms,
    )
    windows = generate_windows(decoded, ingested.media_dir)
    recorded_responses = build_recorded_response_map(
        truth=truth,
        windows=windows,
        source_offset_ms=_source_offset(project_root, truth.set_id),
    )
    recognised = recognise_controlled_fixture(
        media_key=ingested.record.media_key,
        media_dir=ingested.media_dir,
        windows=windows,
        recorded_responses=recorded_responses,
    )
    fused = fuse_generation_zero(
        media_key=ingested.record.media_key,
        media_dir=ingested.media_dir,
        duration_ms=decoded.record.pcm.duration_ms,
        observations=recognised.observations,
        observations_path=recognised.observations_path,
        windows=windows.records,
        windows_path=windows.record_path,
        pcm_path=decoded.record_path,
    )
    export_tracklist(
        media_dir=ingested.media_dir,
        media_key=ingested.record.media_key,
        duration_ms=decoded.record.pcm.duration_ms,
        episodes=fused.episodes,
        identities=fused.identities.record,
        episodes_path=fused.final_path,
        identities_path=fused.identities_path,
    )
    return (
        ingested.media_dir,
        fused,
        BenchmarkCost(requests=0, physical_attempts=0, billable_seconds=0, usd_e2=0, wall_ms=0),
    )


async def _run_real(
    source: str,
    *,
    truth: GroundTruthRecord,
    work_root: Path,
    max_requests: int,
    include_hints: bool,
) -> tuple[Path, None, BenchmarkCost]:
    ingested = await ingest(source, work_root)
    decoded = await decode(ingested)
    _validate_source_media(
        truth,
        media_key=ingested.record.media_key,
        duration_ms=decoded.record.pcm.duration_ms,
    )
    command = [
        sys.executable,
        "-m",
        "id_detector.cli",
        "analyse",
        source,
        "--work-root",
        str(work_root),
        "--max-requests",
        str(max_requests),
    ]
    if not include_hints:
        command.append("--no-hints")
    result = await run_process(
        command,
        timeout=28_800,
    )
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    entries = [
        json.loads(line)
        for line in read_text(ingested.media_dir / "invocations.jsonl").splitlines()
        if line.strip()
    ]
    last = entries[-1]
    return (
        ingested.media_dir,
        None,
        BenchmarkCost(
            requests=max(0, int(last.get("counts", {}).get("requests", 0))),
            physical_attempts=max(0, int(last.get("counts", {}).get("physical_attempts", 0))),
            billable_seconds=0,
            usd_e2=max(0, int(last.get("costs", {}).get("usd_e2", 0))),
            wall_ms=max(0, int(last.get("duration_ms") or 0)),
        ),
    )


def _regression(
    report: BenchmarkReportRecord,
    baseline_path: Path,
    *,
    seed: int,
) -> dict[str, Any]:
    baseline = BenchmarkReportRecord.model_validate_json(read_text(baseline_path))
    if report.corpus_version != baseline.corpus_version:
        raise ValueError("regression reports must use the same corpus_version")
    if report.profile != baseline.profile:
        raise ValueError("regression reports must use the same profile")
    if report.unverified_seed_comparison != baseline.unverified_seed_comparison:
        raise ValueError("regression reports must have the same verification status")
    current_sets = {item.set_id: item for item in report.sets}
    baseline_sets = {item.set_id: item for item in baseline.sets}
    if set(current_sets) != set(baseline_sets):
        raise ValueError("regression reports must have identical set populations")
    if any(
        (current_sets[set_id].stratum, current_sets[set_id].split)
        != (baseline_sets[set_id].stratum, baseline_sets[set_id].split)
        for set_id in current_sets
    ):
        raise ValueError("regression reports have incompatible set metadata")

    def pairs(
        which: str, metric: str
    ) -> tuple[dict[str, tuple[int, int]], dict[str, tuple[int, int]]]:
        old: dict[str, tuple[int, int]] = {}
        new: dict[str, tuple[int, int]] = {}
        for set_id in sorted(current_sets):
            old_value = getattr(getattr(baseline_sets[set_id].metrics, which), metric)
            new_value = getattr(getattr(current_sets[set_id].metrics, which), metric)
            old[set_id], new[set_id] = (old_value, 10_000), (new_value, 10_000)
        return old, new

    deltas: dict[str, int] = {
        "work_precision_e4": report.overall.identification_work.precision_e4
        - baseline.overall.identification_work.precision_e4,
        "work_recall_e4": report.overall.identification_work.recall_e4
        - baseline.overall.identification_work.recall_e4,
        "segment_precision_e4": report.overall.segment_micro.precision_e4
        - baseline.overall.segment_micro.precision_e4,
        "segment_recall_e4": report.overall.segment_micro.recall_e4
        - baseline.overall.segment_micro.recall_e4,
    }
    gates = []
    for index, (name, which, metric) in enumerate(
        (
            ("work_precision", "identification_work", "precision_e4"),
            ("work_recall", "identification_work", "recall_e4"),
            ("segment_precision", "segment_micro", "precision_e4"),
            ("segment_recall", "segment_micro", "recall_e4"),
        )
    ):
        old, new = pairs(which, metric)
        result = paired_non_inferiority(old, new, seed=seed + index)
        deltas[f"{name}_paired_delta_e4"] = int(result["delta_e4"])
        deltas[f"{name}_paired_lower_e4"] = int(result["lower_bound_e4"])
        gates.append({"name": f"{name}_noninferior_1pp", "pass": bool(result["pass"])})
    return {
        "baseline_report_ref": baseline_path.name,
        "deltas": deltas,
        "gates": gates,
    }


def _scoring_config(
    truths: list[GroundTruthRecord],
    *,
    profile: str,
    max_requests: int,
    project_root: Path,
    include_hints: bool = False,
) -> ScoringConfigSnapshot:
    controlled_run = any("controlled" in truth.stratum.casefold() for truth in truths)
    real_run = any("controlled" not in truth.stratum.casefold() for truth in truths)
    providers: list[dict[str, Any]] = []
    if controlled_run:
        providers.append(
            {
                "provider": "local_fixture",
                "provider_config_version": LOCAL_FIXTURE_CONFIG_VERSION,
                "scan_policy": "content-hash-recorded-response",
            }
        )
    if real_run:
        provider_config, provider_config_name = load_provider_config(project_root)
        providers.append(
            {
                "provider": "shazam",
                "provider_config_version": provider_config_name,
                "provider_config_sha256": sha256(canonical_json_bytes(provider_config)).hexdigest(),
                "scan_policy": "default",
            }
        )
    run_config = {
        "set_ids": sorted(truth.set_id for truth in truths),
        "providers": providers,
        "window_schedule": {
            "window_ms": WINDOW_MS,
            "hop_ms": HOP_MS,
            "phase_ms": 0,
            "end_anchored_tail": True,
            "short_input": True,
            "transforms": [{"type": "none", "rate_e4": 10_000, "semitones": 0}],
        },
        "fuser": {
            "policy_version": "baseline-fuser-v1",
            "generation": 0,
            "residual_gate_ms": RESIDUAL_GATE_MS,
            "min_rate_e4": MIN_RATE_E4,
            "max_rate_e4": MAX_RATE_E4,
            "hypothesis_agreement_e4": HYPOTHESIS_AGREEMENT_E4,
            "drift_delta_e4": DRIFT_DELTA_E4,
            "continuation_gap_ms": CONTINUATION_GAP_MS,
            "replay_gap_ms": REPLAY_GAP_MS,
            "badge_rule": "rev5.1",
            **(
                {"hint_policy": "stage4a-one-work-trial-no-version-question-rescans"}
                if include_hints
                else {}
            ),
        },
        "budget": {"max_requests_per_set": max_requests if real_run else 0},
        "source_validation": {
            "sha256": True,
            "duration_tolerance_ms": SOURCE_DURATION_TOLERANCE_MS,
        },
    }
    return ScoringConfigSnapshot(
        schema_version="1.0.0",
        config_version="baseline-fuser-v1",
        profile=profile,
        bootstrap_seed=20_260_904,
        certification_targets=[],
        run_config=run_config,
    )


async def run_corpus(
    *,
    corpus_version: str,
    profile: str,
    out_path: Path,
    project_root: Path,
    work_root: Path,
    baseline: str | None = None,
    set_id: str | None = None,
    max_requests: int = 2_000,
    include_hints: bool = False,
) -> CorpusRunResult:
    if profile != "free":
        raise ValueError("Stage 2b corpus runs support only the free profile")
    corpus_dir = project_root / "data" / "corpus" / corpus_version
    truths_with_paths = [
        (GroundTruthRecord.model_validate_json(read_text(path)), path)
        for path in _truth_files(corpus_dir, set_id)
    ]
    if any(truth.corpus_version != corpus_version for truth, _ in truths_with_paths):
        raise ValueError("truth corpus_version differs from requested corpus")
    links = _source_links(project_root)
    prediction_sets = []
    costs: list[BenchmarkCost] = []
    for truth, _ in truths_with_paths:
        audio = _local_media(project_root, corpus_dir, truth.set_id)
        controlled = "controlled" in truth.stratum.casefold()
        if controlled:
            audio = audio or _controlled_audio(project_root, truth.set_id)
            if audio is None:
                raise ValueError(f"local controlled audio is missing for {truth.set_id}")
            media_dir, fusion, cost = await _run_controlled(
                truth, audio, project_root=project_root, work_root=work_root
            )
        else:
            source_link = links.get(truth.source.url_ref)
            source = _cached_link_source(work_root, truth.source.media_key, source_link)
            source = source or (str(audio) if audio is not None else source_link)
            if source is None:
                raise ValueError(
                    f"no local media or source link for {truth.set_id} ({truth.source.url_ref})"
                )
            media_dir, fusion, cost = await _run_real(
                source,
                truth=truth,
                work_root=work_root,
                max_requests=max_requests,
                include_hints=include_hints,
            )
        prediction_sets.append(_prediction_set(truth.set_id, fusion, media_dir))
        costs.append(cost)
    config = _scoring_config(
        [truth for truth, _ in truths_with_paths],
        profile=profile,
        max_requests=max_requests,
        project_root=project_root,
        include_hints=include_hints,
    )
    config_hash = sha256(canonical_json_bytes(config)).hexdigest()
    unverified = not truth_is_frozen_verified(corpus_dir, [truth for truth, _ in truths_with_paths])
    prediction_document = PredictionDocument(
        corpus_version=corpus_version,
        profile=profile,
        config_hash=config_hash,
        config_snapshot=config,
        sets=sorted(prediction_sets, key=lambda item: item["set_id"]),
        engines=[],
        cost=BenchmarkCost(
            requests=sum(item.requests for item in costs),
            physical_attempts=sum(item.physical_attempts for item in costs),
            billable_seconds=sum(item.billable_seconds for item in costs),
            usd_e2=sum(item.usd_e2 for item in costs),
            wall_ms=sum(item.wall_ms for item in costs),
        ),
        unverified_seed_comparison=unverified,
    )
    predictions_path = (
        project_root
        / "data"
        / "local"
        / "benchmark"
        / corpus_version
        / profile
        / (f"predictions-{set_id}.json" if set_id else "predictions.json")
    )
    atomic_write_json(predictions_path, prediction_document)
    truth_path = truths_with_paths[0][1] if len(truths_with_paths) == 1 else corpus_dir
    report = score_corpus(truth_path, predictions_path)
    if baseline:
        baseline_path = Path(baseline)
        if not baseline_path.is_file():
            baseline_path = project_root / "data" / "corpus" / baseline / f"baseline-{profile}.json"
        if not baseline_path.is_file():
            raise ValueError(f"named baseline report not found: {baseline}")
        report = BenchmarkReportRecord.model_validate(
            {
                **report.model_dump(mode="json"),
                "regression": _regression(report, baseline_path, seed=config.bootstrap_seed),
            }
        )
    atomic_write_json(out_path, report)
    return CorpusRunResult(report=report, predictions_path=predictions_path)

from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np

from id_detector.benchmark.scorer import ScoredEpisode
from id_detector.contracts import GroundTruthRecord
from id_detector.decode import decode
from id_detector.fuse.episodes import fuse_generation_zero
from id_detector.ingest import ingest
from id_detector.io import read_bytes
from id_detector.local_fixture import build_recorded_response_map, recognise_controlled_fixture
from id_detector.present import export_tracklist, flatten_tracklist
from id_detector.windows import generate_windows


def _noise_wav(path: Path, duration_ms: int, *, seed: int = 22) -> None:
    rng = np.random.default_rng(seed)
    samples = rng.integers(-12_000, 12_001, duration_ms * 16, dtype=np.int16)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(samples.astype("<i2").tobytes())


def _truth(media_key: str, duration_ms: int) -> GroundTruthRecord:
    return GroundTruthRecord.model_validate(
        {
            "schema_version": "1.0.0",
            "generated_by": "id-detector/0.1.0",
            "set_id": "controlled-pipeline-vector",
            "source": {
                "url_ref": "controlled-source",
                "media_key": media_key,
                "duration_ms": duration_ms,
                "platform": "file",
                "uploader_ref": "generator",
                "event_ref": None,
                "date": None,
            },
            "stratum": "controlled",
            "split": "controlled",
            "corpus_version": "controlled-vector",
            "selection_basis": "authored test vector",
            "episodes": [
                {
                    "work": {"artist": "Synthetic Artist", "title": "Synthetic Work"},
                    "version": {"qualifier": "Fixture", "ids": {"mb_recording": "fixture-r1"}},
                    "version_verified": True,
                    "verified_against": "source_recording",
                    "start_ms_range": [10_000, 10_000],
                    "end_ms_range": [50_000, 50_000],
                    "audible_rule": "authored test vector",
                    "role_segments": [{"from_ms": 10_000, "to_ms": 50_000, "role": "dominant"}],
                    "overlaps_with": [],
                    "occurrence_index": 0,
                    "in_reference_pool": True,
                    "annotator_ref": "generator",
                    "second_pass_ref": None,
                    "disagreement_resolution": None,
                    "note": None,
                    "draft": False,
                }
            ],
            "regions": [],
        }
    )


def test_partition_and_byte_determinism_on_full_local_pipeline_sync(tmp_path: Path) -> None:
    import asyncio

    async def exercise() -> tuple[bytes, object, object, Path]:
        source = tmp_path / "source.wav"
        _noise_wav(source, 75_000)
        ingested = await ingest(str(source), tmp_path / "work")
        decoded = await decode(ingested)
        windows = generate_windows(decoded, ingested.media_dir)
        truth = _truth(ingested.record.media_key, decoded.record.pcm.duration_ms)
        recorded_responses = build_recorded_response_map(
            truth=truth, windows=windows, source_offset_ms=20_000
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
        exported = export_tracklist(
            media_dir=ingested.media_dir,
            media_key=ingested.record.media_key,
            duration_ms=decoded.record.pcm.duration_ms,
            episodes=fused.episodes,
            identities=fused.identities.record,
            episodes_path=fused.final_path,
            identities_path=fused.identities_path,
        )
        first = read_bytes(fused.final_path)
        second_fusion = fuse_generation_zero(
            media_key=ingested.record.media_key,
            media_dir=ingested.media_dir,
            duration_ms=decoded.record.pcm.duration_ms,
            observations=recognised.observations,
            observations_path=recognised.observations_path,
            windows=windows.records,
            windows_path=windows.record_path,
            pcm_path=decoded.record_path,
        )
        assert read_bytes(second_fusion.final_path) == first
        return first, fused, exported, ingested.media_dir

    payload, fused, exported, _ = asyncio.run(exercise())
    parsed = json.loads(payload)
    assert (
        sum(
            parsed["durations"][name]
            for name in (
                "evidence_supported_ms",
                "predicted_episode_ms",
                "unresolved_boundary_ms",
                "unclear_ms",
                "no_evidence_ms",
                "unscanned_ms",
            )
        )
        == 75_000
    )
    assert fused.episodes.durations.predicted_episode_ms == 0
    assert fused.episodes.durations.evidence_supported_ms > 0
    assert all(entry.status == "provisional" for entry in fused.episodes.certification.per)
    assert exported.entries
    assert all(
        entry["version_status"] == "unverified"
        for entry in exported.entries
        if entry["kind"] == "track"
    )
    markdown = exported.markdown_path.read_text(encoding="utf-8")
    assert "| Time | Badge | Version | Role | Track |" in markdown
    assert "| POSSIBLE | UNVERIFIED |" in markdown


def test_recorded_responses_reject_unrelated_window_content(tmp_path: Path) -> None:
    import asyncio

    async def exercise() -> None:
        expected = tmp_path / "expected.wav"
        unrelated = tmp_path / "unrelated.wav"
        _noise_wav(expected, 75_000, seed=22)
        _noise_wav(unrelated, 75_000, seed=23)

        expected_ingest = await ingest(str(expected), tmp_path / "expected-work")
        expected_decode = await decode(expected_ingest)
        expected_windows = generate_windows(expected_decode, expected_ingest.media_dir)
        truth = _truth(expected_ingest.record.media_key, expected_decode.record.pcm.duration_ms)
        recorded = build_recorded_response_map(
            truth=truth, windows=expected_windows, source_offset_ms=20_000
        )

        unrelated_ingest = await ingest(str(unrelated), tmp_path / "unrelated-work")
        unrelated_decode = await decode(unrelated_ingest)
        unrelated_windows = generate_windows(unrelated_decode, unrelated_ingest.media_dir)
        recognised = recognise_controlled_fixture(
            media_key=unrelated_ingest.record.media_key,
            media_dir=unrelated_ingest.media_dir,
            windows=unrelated_windows,
            recorded_responses=recorded,
        )
        assert recognised.observations
        assert all(item.status == "no_match" for item in recognised.observations)

    asyncio.run(exercise())


def test_export_flattening_uses_incoming_best_start_and_id_gap() -> None:
    from id_detector.contracts import EpisodesFile, IdentitiesRecord, RoleSegment

    root = Path(__file__).parent / "golden"
    episodes = EpisodesFile.model_validate_json(
        (root / "episodes.json").read_text(encoding="utf-8")
    )
    identities = IdentitiesRecord.model_validate_json(
        (root / "identities.json").read_text(encoding="utf-8")
    )
    incoming = episodes.episodes[0].model_copy(
        update={
            "role_segments": [
                RoleSegment(from_ms=18_000, to_ms=40_000, role="incoming"),
                RoleSegment(from_ms=40_000, to_ms=219_000, role="dominant"),
            ]
        }
    )
    episodes = episodes.model_copy(update={"episodes": [incoming]})
    flattened = flatten_tracklist(episodes, identities)
    track = next(item for item in flattened if item["kind"] == "track")
    gap = next(item for item in flattened if item["kind"] == "id")
    assert track["start_ms"] == incoming.best_start_ms
    assert track["primary_role"] == "incoming"
    assert track["version_status"] == incoming.version_status
    assert gap["label"] == "ID"


def test_scorer_accepts_crossed_one_sided_proofs() -> None:
    episode = ScoredEpisode.model_validate(
        {
            "work": {"artist": "Artist", "title": "Title"},
            "version": {"qualifier": None, "ids": {}},
            "candidate_id": "1" * 40,
            "evidence_support_ms": [[0, 12_000]],
            "start_no_later_than_ms": 12_000,
            "end_no_earlier_than_ms": 0,
            "start_pi": None,
            "end_pi": None,
            "best_start_ms": 12_000,
            "best_end_ms": 0,
            "role_segments": [{"from_ms": 0, "to_ms": 12_000, "role": "dominant"}],
            "occurrence_index": 0,
            "claim": "performed",
            "scores": {"work": 2_500, "version": 0, "boundary": 0},
            "tiers": {"work": "unclear", "version": "unclear", "boundary": "unclear"},
            "alignment_events": [],
        }
    )
    assert episode.best_start_ms > episode.best_end_ms

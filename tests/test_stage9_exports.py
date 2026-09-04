"""Stage 9 export polish: CUE REM overlap/layer lines and the VLC-seeking M3U."""

from __future__ import annotations

import json
from pathlib import Path

from id_detector.contracts import EpisodesFile, IdentitiesRecord
from id_detector.present.exports import (
    export_tracklist,
    flatten_tracklist,
    render_cue,
    render_m3u,
)

GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "golden"
CANDIDATE_ID = "46b3308f39ae9f94358ee570f343e48993b360a3"


def _track(start_ms: int, **extra: object) -> dict:
    entry = {
        "kind": "track",
        "start_ms": start_ms,
        "episode_id": f"{start_ms:040d}",
        "candidate_id": CANDIDATE_ID,
        "artist": "Artist",
        "title": "Title",
        "occurrence_index": 0,
        "primary_role": "dominant",
        "overlap_labels": [],
        "has_layer": False,
        "badge": "likely",
        "version_status": "unverified",
        "hint_supported": False,
        "n_rejected_hypotheses": 0,
        "tiers": {"work": "likely", "version": "unclear", "boundary": "possible"},
        "acquire": None,
    }
    entry.update(extra)
    return entry


def test_cue_notes_overlaps_and_layers_on_rem_lines() -> None:
    entries = (
        _track(0, overlap_labels=["Other - Track"]),
        _track(60_000, overlap_labels=["First - One"], has_layer=True),
    )
    cue = render_cue(entries, title="Set")
    assert 'REM OVERLAP "Other - Track"' in cue
    assert 'REM LAYER "First - One"' in cue
    # REM lines sit inside their TRACK, before that track's INDEX.
    layer_pos = cue.index("REM LAYER")
    index_after = cue.index("INDEX 01", layer_pos)
    assert layer_pos < index_after


def test_cue_without_overlaps_has_no_rem_lines() -> None:
    cue = render_cue((_track(0), _track(60_000)), title="Set")
    assert "REM" not in cue


def test_m3u_seeks_each_entry_with_extvlcopt_start_time() -> None:
    entries = (
        _track(0, artist="A", title="One"),
        _track(90_500, artist="B", title="Two"),
        {
            "kind": "id",
            "start_ms": 120_000,
            "end_ms": 180_000,
            "label": "ID",
            "reason": "no_evidence",
        },
    )
    m3u = render_m3u(entries, media_target="https://example.invalid/set")
    lines = m3u.splitlines()
    assert lines[0] == "#EXTM3U"
    assert "#EXTVLCOPT:start-time=0" in lines
    assert "#EXTVLCOPT:start-time=90" in lines  # 90_500 ms -> 90 s
    assert "#EXTVLCOPT:start-time=120" in lines
    assert lines.count("https://example.invalid/set") == 3
    assert "#EXTINF:-1,A - One" in lines
    assert any(line.startswith("#EXTINF:-1,ID - no evidence through") for line in lines)


def _episode(idx: int, start_ms: int, end_ms: int, overlaps: list[str], role: str) -> dict:
    return {
        "schema_version": "1.0.0",
        "generated_by": "id-detector/0.1.0",
        "id": f"{idx:040d}",
        "candidate_id": CANDIDATE_ID,
        "alternatives": [],
        "claim": "performed",
        "start_no_later_than_ms": start_ms,
        "end_no_earlier_than_ms": end_ms,
        "evidence_support_ms": [[start_ms, end_ms]],
        "start_no_earlier_than_ms": None,
        "end_no_later_than_ms": None,
        "start_pi": None,
        "end_pi": None,
        "best_start_ms": start_ms,
        "best_end_ms": end_ms,
        "role_segments": [{"from_ms": start_ms, "to_ms": end_ms, "role": role}],
        "occurrence_index": idx,
        "overlaps": overlaps,
        "alignment_segments": [],
        "alignment_events": [],
        "has_global_alignment": True,
        "scores": {"work": 7800, "version": 5000, "boundary": 6000},
        "score_kind": "heuristic",
        "tiers": {"work": "likely", "version": "unclear", "boundary": "possible"},
        "badge": "likely",
        "version_status": "unverified",
        "evidence": ["a" * 40],
        "rejected_evidence": [],
        "flags": [],
        "rescan_state": "not_requested",
    }


def _overlapping_episodes_file() -> EpisodesFile:
    document = {
        "schema_version": "1.0.0",
        "generated_by": "id-detector/0.1.0",
        "generation": 0,
        "episodes": [
            _episode(1, 0, 90_000, overlaps=[f"{2:040d}"], role="layer"),
            _episode(2, 60_000, 150_000, overlaps=[f"{1:040d}"], role="layer"),
        ],
        "gaps": [],
        "durations": {
            "schema_version": "1.0.0",
            "generated_by": "id-detector/0.1.0",
            "evidence_supported_ms": 150_000,
            "predicted_episode_ms": 0,
            "unresolved_boundary_ms": 0,
            "unclear_ms": 0,
            "no_evidence_ms": 0,
            "unscanned_ms": 0,
        },
        "certification": {
            "profile": "free",
            "per": [
                {
                    "dimension": "work",
                    "tier": "possible",
                    "status": "provisional",
                    "n_test_predictions": 0,
                    "lower_bound_e4": 0,
                    "test_version": "not-run",
                }
            ],
        },
    }
    return EpisodesFile.model_validate(document)


def _identities() -> IdentitiesRecord:
    return IdentitiesRecord.model_validate_json((GOLDEN / "identities.json").read_text("utf-8"))


def _seed_upstream(tmp_path: Path, episodes: EpisodesFile, identities: IdentitiesRecord) -> None:
    """Write the fuse artefacts export_tracklist hashes into its completion sidecars."""

    fuse = tmp_path / "fuse"
    fuse.mkdir(parents=True, exist_ok=True)
    (fuse / "episodes.json").write_text(episodes.model_dump_json(), encoding="utf-8")
    (fuse / "identities.gen0.json").write_text(identities.model_dump_json(), encoding="utf-8")


def test_flatten_populates_overlap_labels_and_export_writes_cue_and_m3u(tmp_path: Path) -> None:
    episodes = _overlapping_episodes_file()
    identities = _identities()
    _seed_upstream(tmp_path, episodes, identities)
    entries = flatten_tracklist(episodes, identities)
    tracks = [entry for entry in entries if entry["kind"] == "track"]
    assert len(tracks) == 2
    # Each overlapping episode names the other on its overlap_labels, and both are layer role.
    assert all(track["overlap_labels"] for track in tracks)
    assert all(track["has_layer"] for track in tracks)

    result = export_tracklist(
        media_dir=tmp_path,
        media_key="0" * 64,
        duration_ms=150_000,
        episodes=episodes,
        identities=identities,
        episodes_path=tmp_path / "fuse" / "episodes.json",
        identities_path=tmp_path / "fuse" / "identities.gen0.json",
        title="Overlap Set",
        media_target="https://example.invalid/set",
    )
    cue = (tmp_path / "present" / "tracklist.cue").read_text(encoding="utf-8")
    m3u = (tmp_path / "present" / "tracklist.m3u").read_text(encoding="utf-8")
    assert "REM LAYER" in cue
    assert "#EXTVLCOPT:start-time=" in m3u
    assert "https://example.invalid/set" in m3u
    assert result.m3u_path == tmp_path / "present" / "tracklist.m3u"
    assert (tmp_path / "present" / "tracklist.cue").is_file()
    assert (tmp_path / "present" / "tracklist.m3u").is_file()
    # A completion sidecar is written for the export set (shared X.done.json name).
    assert (tmp_path / "present" / "tracklist.done.json").is_file()


def test_flattened_json_shape_unchanged_for_existing_consumers(tmp_path: Path) -> None:
    """New fields are additive: existing keys stay present so Stage 2b/6 consumers keep working."""

    episodes = _overlapping_episodes_file()
    identities = _identities()
    _seed_upstream(tmp_path, episodes, identities)
    export_tracklist(
        media_dir=tmp_path,
        media_key="0" * 64,
        duration_ms=150_000,
        episodes=episodes,
        identities=identities,
        episodes_path=tmp_path / "fuse" / "episodes.json",
        identities_path=tmp_path / "fuse" / "identities.gen0.json",
    )
    payload = json.loads((tmp_path / "present" / "tracklist.json").read_text(encoding="utf-8"))
    entry = payload["entries"][0]
    for key in ("kind", "start_ms", "badge", "version_status", "primary_role"):
        assert key in entry

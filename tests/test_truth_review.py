"""Owner truth-review tests, isolated from the real corpus and work tree."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
import threading
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import id_detector.truth as truth_module
import id_detector.truth_review as review_module
from id_detector.contracts import GroundTruthRecord, SourceRecord, derive_source_key
from id_detector.io import read_text, sha256_file
from id_detector.truth import _annotation_path
from id_detector.truth_review import (
    _SAVE_LOCK_TIMEOUT,
    TruthReviewSession,
    exposure_path,
    find_truth_path,
    preview_bulk_offset,
    reconciled_role_segments,
    reviewed_record,
    serve_truth_review_in_background,
)
from scripts import audit_fixtures
from scripts.check_page_js import _inline_scripts
from scripts.make_audio_fixtures import generate

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "truth-review"
TIMEOUT = httpx.Timeout(5.0)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    target = tmp_path / "corpus"
    shutil.copytree(FIXTURE, target)
    return target


def _truth(corpus: Path) -> tuple[Path, GroundTruthRecord]:
    path = corpus / "fixture-set" / "ground_truth.json"
    return path, GroundTruthRecord.model_validate_json(read_text(path))


def _seed_work(root: Path, truth: GroundTruthRecord) -> Path:
    canonical = "file:///synthetic/truth-review-tone.wav"
    source_key = derive_source_key(canonical)
    media_dir = root / source_key / truth.source.media_key
    ingest = media_dir / "ingest"
    ingest.mkdir(parents=True)
    audio = ingest / "tone.wav"
    generate(audio, 3)
    source = SourceRecord.model_validate(
        {
            "schema_version": "1.0.0",
            "generated_by": "id-detector/0.1.0",
            "source_key": source_key,
            "media_key": truth.source.media_key,
            "input_url": canonical,
            "canonical_url": canonical,
            "platform": "file",
            "platform_id": None,
            "uploader_id": None,
            "uploader_name": None,
            "title": "Synthetic truth review tone",
            "upload_date": None,
            "original": {
                "path": "ingest/tone.wav",
                "sha256": sha256_file(audio),
                "container": "wav",
                "codec": "pcm_s16le",
                "bitrate": None,
                "ytdlp_format_id": None,
            },
            "metadata": {"description": None, "chapters": [], "comment_count": None},
            "config_snapshot": {},
        }
    )
    (ingest / "source.json").write_text(source.model_dump_json(), encoding="utf-8")
    present = media_dir / "present"
    present.mkdir()
    (present / "index.html").write_text("fixture", encoding="utf-8")
    return media_dir


def _rows(truth: GroundTruthRecord, *, offset_ms: int = 0) -> list[dict[str, object]]:
    rows = []
    for index, episode in enumerate(truth.episodes):
        rows.append(
            {
                "index": index,
                "artist": episode.work.artist,
                "title": episode.work.title,
                "start_ms_range": [value + offset_ms for value in episode.start_ms_range],
                "end_ms_range": [
                    min(truth.source.duration_ms, value + offset_ms)
                    for value in episode.end_ms_range
                ],
                "confirmed": True,
            }
        )
    return rows


def _post(
    running: object,
    route: str,
    *,
    token: str | None,
    body: object | None = None,
    origin: str | None = None,
) -> httpx.Response:
    base = running.base_url  # type: ignore[attr-defined]
    headers = {}
    if token is not None:
        headers["X-CSRF-Token"] = token
    if origin is not None:
        headers["Origin"] = origin
    return httpx.post(base + route, json=body, headers=headers, timeout=TIMEOUT)


def test_draft_page_renders_keyboard_workflow_and_range_audio(corpus: Path, tmp_path: Path) -> None:
    truth_path, truth = _truth(corpus)
    work = tmp_path / "work"
    _seed_work(work, truth)
    session = TruthReviewSession(truth_path, work_root=work)
    session.suggested_offset_ms = 48_000
    # Resolving review media is strictly read-only; it rebuilt the absent index only in memory.
    assert not (work / "index.json").exists()
    original_truth = truth_path.read_bytes()
    running = serve_truth_review_in_background(session)
    try:
        page = httpx.get(running.base_url + "/", timeout=TIMEOUT)
        assert page.status_code == 200
        assert "fixture-truth-review" in page.text
        assert page.text.count('class="truth-row"') == 3
        assert "needs-listening" in page.text
        assert "Predictions are hidden so truth stays independent" in page.text
        assert "Use scorer suggestion +48 s" in page.text
        assert 'id="offset" type="number" step="0.1" value="0"' in page.text
        assert "accept our answer" not in page.text.casefold()
        for key in (
            "Space",
            "J / L",
            "Shift+J / Shift+L",
            "Enter",
            "E",
            "T",
            "N / P",
            "S",
            "?",
        ):
            assert key in page.text
        # Pin the generated dispatch, not just its help copy.  The behavioural Node test below
        # is what proves the loop works; these keep the page from silently losing a binding.
        for script_fragment in (
            "if(e.code==='Space')",
            "k==='j'&&audio",
            "e.shiftKey?30:5",
            "k==='l'&&audio",
            "e.key==='Enter')confirmRow()",
            "k==='e'){edit()",
            "k==='t')stamp()",
            "k==='n')select(current+1)",
            "k==='p')select(current-1)",
            "k==='s'){save()",
            "help.classList.remove('open')",
            "audio.currentTime=rows[current].start_ms_range[0]/1000",
            "document.getElementById('undo-offset').onclick=undo",
        ):
            assert script_fragment in page.text
        assert review_module._SUGGESTED_OFFSETS_MS == {
            "release1-mall-grab-boiler-room-melbourne-22": 48_000,
            "release1-dj-heartstring-youtube-set": 51_000,
        }
        audio = httpx.get(
            running.base_url + f"/media/{truth.source.media_key}/audio",
            headers={"Range": "bytes=0-31"},
            timeout=TIMEOUT,
        )
        assert audio.status_code == 206
        assert len(audio.content) == 32
        assert audio.headers["accept-ranges"] == "bytes"
        assert truth_path.read_bytes() == original_truth  # no GET mutation
    finally:
        running.shutdown()


def test_missing_audio_keeps_review_available(corpus: Path, tmp_path: Path) -> None:
    truth_path, truth = _truth(corpus)
    session = TruthReviewSession(truth_path, work_root=tmp_path / "empty-work")
    running = serve_truth_review_in_background(session)
    try:
        page = httpx.get(running.base_url + "/", timeout=TIMEOUT)
        assert page.status_code == 200
        assert "Audio is not in the local media index" in page.text
        assert "playback and playhead stamping do not" in page.text
        audio = httpx.get(
            running.base_url + f"/media/{truth.source.media_key}/audio", timeout=TIMEOUT
        )
        assert audio.status_code == 404
    finally:
        running.shutdown()


def test_confirm_and_save_uses_verify_contract_and_preserves_other_fields(
    corpus: Path, tmp_path: Path
) -> None:
    truth_path, before = _truth(corpus)
    session = TruthReviewSession(
        truth_path,
        work_root=tmp_path / "work",
        clock=lambda: datetime(2026, 9, 14, 12, 30, tzinfo=UTC),
    )
    rows = _rows(before)
    rows[1]["artist"] = "Corrected Fixture Artist"
    rows[1]["title"] = "Corrected Middle Tone"
    saved = session.save({"rows": rows})

    reread = GroundTruthRecord.model_validate_json(read_text(truth_path))
    assert reread == saved
    assert all(not episode.draft for episode in saved.episodes)
    assert all(episode.verified_against == "audio" for episode in saved.episodes)
    assert all(episode.annotator_ref == "owner" for episode in saved.episodes)
    assert saved.episodes[1].work.artist == "Corrected Fixture Artist"
    for old, new in zip(before.episodes, saved.episodes, strict=True):
        assert new.occurrence_index == old.occurrence_index
        assert new.overlaps_with == old.overlaps_with
        assert new.version == old.version
        assert new.in_reference_pool == old.in_reference_pool
        assert new.note == old.note
        assert new.second_pass_ref == old.second_pass_ref
        assert new.disagreement_resolution == old.disagreement_resolution
        # The owner's hand-made role annotation survives confirmation untouched.
        assert new.role_segments == old.role_segments
    annotation = json.loads(read_text(_annotation_path(truth_path, "first")))
    assert annotation["mode"] == "independent"
    assert annotation["review_provenance"] == {
        "predictions_visible_during_review": False,
        "reviewed_at_utc": "2026-09-14T12:30:00Z",
        "tool": "idea truth review",
        "version": "0.1.0",
    }


def test_bulk_offset_preview_is_pure_shifts_uniformly_and_clamps(
    corpus: Path,
) -> None:
    truth_path, truth = _truth(corpus)
    before = truth_path.read_bytes()
    shifted, clamped, collapsed = preview_bulk_offset(truth, 5_000)
    assert collapsed == []
    assert [episode.start_ms_range[0] for episode in shifted.episodes] == [5_000, 25_000, 45_000]
    assert [episode.end_ms_range[1] for episode in shifted.episodes] == [25_000, 45_000, 60_000]
    assert clamped > 0
    assert truth_path.read_bytes() == before

    negative, negative_clamps, negative_collapsed = preview_bulk_offset(truth, -5_000)
    assert negative_collapsed == []
    assert [episode.start_ms_range[0] for episode in negative.episodes] == [0, 15_000, 35_000]
    assert negative_clamps > 0
    assert truth_path.read_bytes() == before


def test_offset_is_not_written_until_explicit_save(corpus: Path, tmp_path: Path) -> None:
    truth_path, truth = _truth(corpus)
    original = truth_path.read_bytes()
    preview, _, _ = preview_bulk_offset(truth, 4_000)
    assert truth_path.read_bytes() == original
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    session.save({"rows": _rows(preview)})
    saved = GroundTruthRecord.model_validate_json(read_text(truth_path))
    assert [episode.start_ms_range[0] for episode in saved.episodes] == [4_000, 24_000, 44_000]


def test_predictions_are_not_in_html_and_reveal_is_recorded(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path, truth = _truth(corpus)
    session = TruthReviewSession(
        truth_path,
        work_root=tmp_path / "work",
        clock=lambda: datetime(2026, 9, 14, 13, 0, tzinfo=UTC),
    )
    asked: list[object] = []

    def _stub(media_dir: object) -> list[dict[str, object]]:
        asked.append(media_dir)
        return [
            {
                "artist": "Secret Engine Artist",
                "title": "Secret Engine Guess",
                "start_ms": 1234,
                "end_ms": 5678,
                "tier": "likely",
            }
        ]

    monkeypatch.setattr(review_module, "_predictions", _stub)
    running = serve_truth_review_in_background(session)
    try:
        page = httpx.get(running.base_url + "/", timeout=TIMEOUT)
        # Inspect the served body, not the styling: no prediction payload, label or endpoint
        # result may be present before the owner asks for one.
        assert "Secret Engine Artist" not in page.text
        assert "Secret Engine Guess" not in page.text
        assert "1234" not in page.text and "5678" not in page.text
        # Rendering the page never even asks the engine for an answer.
        assert asked == []
        token = httpx.get(running.base_url + "/csrf", timeout=TIMEOUT).json()["token"]
        reveal = _post(running, "/predictions", token=token)
        assert reveal.status_code == 200
        assert len(asked) == 1
        assert reveal.json()["predictions"][0]["artist"] == "Secret Engine Artist"
        save = _post(running, "/save", token=token, body={"rows": _rows(truth)})
        assert save.status_code == 200, save.text
    finally:
        running.shutdown()
    annotation = json.loads(read_text(_annotation_path(truth_path, "first")))
    assert annotation["review_provenance"]["predictions_visible_during_review"] is True
    saved = GroundTruthRecord.model_validate_json(read_text(truth_path))
    assert all(episode.work.artist != "Secret Engine Artist" for episode in saved.episodes)
    # The durable taint is visible and remains true if the set is reopened.
    reopened = TruthReviewSession(truth_path, work_root=tmp_path / "work-two")
    assert reopened.predictions_visible is True


def test_mutations_require_csrf_and_same_origin(corpus: Path, tmp_path: Path) -> None:
    truth_path, truth = _truth(corpus)
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    running = serve_truth_review_in_background(session)
    try:
        token = httpx.get(running.base_url + "/csrf", timeout=TIMEOUT).json()["token"]
        missing = _post(running, "/save", token=None, body={"rows": _rows(truth)})
        assert missing.status_code == 403
        foreign = _post(
            running,
            "/save",
            token=token,
            body={"rows": _rows(truth)},
            origin="https://foreign.invalid",
        )
        assert foreign.status_code == 403
        # A rebound Host cannot even collect the token, let alone spend it.
        rebound = httpx.get(
            running.base_url + "/csrf",
            headers={"Host": "attacker.invalid"},
            timeout=TIMEOUT,
        )
        assert rebound.status_code == 403
        rebound_save = httpx.post(
            running.base_url + "/save",
            json={"rows": _rows(truth)},
            headers={"Host": "attacker.invalid", "X-CSRF-Token": token},
            timeout=TIMEOUT,
        )
        assert rebound_save.status_code == 403
        assert all(
            episode.draft
            for episode in GroundTruthRecord.model_validate_json(read_text(truth_path)).episodes
        )
    finally:
        running.shutdown()


def test_set_and_audio_traversal_are_refused(corpus: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="set id"):
        find_truth_path(corpus, "../fixture-set")
    truth_path, truth = _truth(corpus)
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    running = serve_truth_review_in_background(session)
    try:
        traversal = httpx.get(
            running.base_url + "/media/%2e%2e/%2e%2e/pyproject.toml", timeout=TIMEOUT
        )
        assert traversal.status_code == 404
        wrong_key = httpx.get(running.base_url + "/media/" + "b" * 64 + "/audio", timeout=TIMEOUT)
        assert wrong_key.status_code == 404
    finally:
        running.shutdown()


def test_injected_atomic_replace_failure_leaves_original(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path, truth = _truth(corpus)
    original = truth_path.read_bytes()
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    real_write = truth_module.atomic_write_json

    def fail_truth(path: Path, value: object) -> None:
        if path.resolve() == truth_path.resolve():
            raise OSError("injected replace failure")
        real_write(path, value)

    monkeypatch.setattr(truth_module, "atomic_write_json", fail_truth)
    with pytest.raises(OSError, match="injected"):
        session.save({"rows": _rows(truth)})
    assert truth_path.read_bytes() == original
    assert not _annotation_path(truth_path, "first").exists()


def test_fixture_audit_passes_after_review_save(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path, truth = _truth(corpus)
    _reveal_stub(monkeypatch)
    session = TruthReviewSession(
        truth_path,
        work_root=tmp_path / "work",
        clock=lambda: datetime(2026, 9, 14, 14, 0, tzinfo=UTC),
    )
    # Reveal first, so the exposure sidecar this tool now writes is audited too: no URL, handle
    # or long numeric id may enter any file that lands in a corpus directory.
    session.reveal_predictions()
    session.save({"rows": _rows(truth)})
    assert exposure_path(truth_path).exists()
    monkeypatch.setattr(audit_fixtures, "ROOT", tmp_path)
    monkeypatch.setattr(audit_fixtures, "SCAN_ROOTS", (corpus,))
    monkeypatch.setattr(audit_fixtures, "RAW_ROOT", tmp_path / "absent-raw")
    assert audit_fixtures.audit() == []


# --------------------------------------------------------------------------------------------
# P0-1 — confirmation must carry the owner's hand-made role annotation, never invent one.
# --------------------------------------------------------------------------------------------


def test_confirmation_preserves_hand_made_role_segments(corpus: Path, tmp_path: Path) -> None:
    """The real corpus is entirely ``uncertain`` with nine ``layer`` spans; none of it is ours.

    Rewriting a row's roles to a single ``dominant`` span both deleted hand-transcribed work and
    asserted a confidence the owner never expressed.
    """

    truth_path, before = _truth(corpus)
    payload = json.loads(read_text(truth_path))
    payload["episodes"][0]["role_segments"] = [
        {"from_ms": 0, "to_ms": 9_000, "role": "incoming"},
        {"from_ms": 9_000, "to_ms": 20_000, "role": "layer"},
    ]
    truth_path.write_text(json.dumps(payload), encoding="utf-8")
    truth_path, before = _truth(corpus)

    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    saved = session.save({"rows": _rows(before)})

    assert [(s.from_ms, s.to_ms, s.role) for s in saved.episodes[0].role_segments] == [
        (0, 9_000, "incoming"),
        (9_000, 20_000, "layer"),
    ]
    assert [s.role for s in saved.episodes[1].role_segments] == ["uncertain"]
    assert not any(
        segment.role == "dominant"
        for episode in saved.episodes
        for segment in episode.role_segments
    )


def test_offset_translates_role_segments_and_never_invents_dominant(
    corpus: Path, tmp_path: Path
) -> None:
    truth_path, before = _truth(corpus)
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    saved = session.save({"rows": _rows(before, offset_ms=4_000)})

    # A uniform shift moves the annotation with the episode: same roles, same structure.
    assert [(s.from_ms, s.to_ms, s.role) for s in saved.episodes[1].role_segments] == [
        (24_000, 44_000, "uncertain")
    ]
    assert [s.role for s in saved.episodes[2].role_segments] == ["uncertain"]


def test_non_uniform_timing_edit_clips_roles_without_relabelling(corpus: Path) -> None:
    truth_path, before = _truth(corpus)
    episode = before.episodes[1]
    # Trim the row's start forward by 5 s while leaving its end alone: not a translation.
    clipped = reconciled_role_segments(episode, (25_000, 25_000), episode.end_ms_range)
    assert [(s.from_ms, s.to_ms, s.role) for s in clipped] == [(25_000, 40_000, "uncertain")]

    dropped = reconciled_role_segments(episode, (39_000, 39_000), (40_000, 40_000))
    assert [s.role for s in dropped] == ["uncertain"]
    assert all(segment.role != "dominant" for segment in dropped)

    record = reviewed_record(
        before,
        [
            {**row, "start_ms_range": [25_000, 25_000]} if row["index"] == 1 else row
            for row in _rows(before)
        ],
        annotator_ref="owner",
    )
    assert [s.role for s in record.episodes[1].role_segments] == ["uncertain"]


# --------------------------------------------------------------------------------------------
# P0-2 — the independence flag must be durable from the instant predictions are revealed.
# --------------------------------------------------------------------------------------------


def _reveal_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        review_module,
        "_predictions",
        lambda media_dir: [
            {
                "artist": "Secret Engine Artist",
                "title": "Secret Engine Guess",
                "start_ms": 1234,
                "end_ms": 5678,
                "tier": "likely",
            }
        ],
    )


def test_reveal_is_durable_across_a_restart_before_any_save(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reveal, kill the server, reopen, save — the record must still admit the exposure."""

    truth_path, truth = _truth(corpus)
    _reveal_stub(monkeypatch)
    first = TruthReviewSession(
        truth_path,
        work_root=tmp_path / "work",
        clock=lambda: datetime(2026, 9, 14, 9, 0, tzinfo=UTC),
    )
    first.reveal_predictions()
    assert exposure_path(truth_path).exists()
    del first  # the whole process goes away; nothing else was written

    reopened = TruthReviewSession(
        truth_path,
        work_root=tmp_path / "work",
        clock=lambda: datetime(2026, 9, 14, 9, 5, tzinfo=UTC),
    )
    assert reopened.predictions_visible is True
    reopened.save({"rows": _rows(truth)})
    annotation = json.loads(read_text(_annotation_path(truth_path, "first")))
    assert annotation["review_provenance"]["predictions_visible_during_review"] is True


def test_reveal_survives_a_failed_save(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path, truth = _truth(corpus)
    _reveal_stub(monkeypatch)
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    session.reveal_predictions()

    real_write = truth_module.atomic_write_json

    def fail_truth(path: Path, value: object) -> None:
        if path.resolve() == truth_path.resolve():
            raise OSError("injected replace failure")
        real_write(path, value)

    monkeypatch.setattr(truth_module, "atomic_write_json", fail_truth)
    with pytest.raises(OSError, match="injected"):
        session.save({"rows": _rows(truth)})
    monkeypatch.undo()
    _reveal_stub(monkeypatch)

    assert TruthReviewSession(truth_path, work_root=tmp_path / "work").predictions_visible is True


def test_predictions_are_withheld_when_the_exposure_cannot_be_recorded(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag may be pessimistic, never optimistic: no durable record, no predictions."""

    truth_path, _ = _truth(corpus)
    _reveal_stub(monkeypatch)
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")

    def fail_exposure(path: Path, value: object) -> None:
        raise OSError("injected exposure write failure")

    monkeypatch.setattr(review_module, "atomic_write_json", fail_exposure)
    with pytest.raises(OSError, match="injected exposure"):
        session.reveal_predictions()
    assert session.predictions_visible is False
    assert not exposure_path(truth_path).exists()


def test_reveal_route_records_before_it_answers(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path, _ = _truth(corpus)
    seen: list[bool] = []
    monkeypatch.setattr(
        review_module,
        "_predictions",
        lambda media_dir: (seen.append(exposure_path(truth_path).exists()), [])[1],
    )
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    running = serve_truth_review_in_background(session)
    try:
        token = httpx.get(running.base_url + "/csrf", timeout=TIMEOUT).json()["token"]
        assert _post(running, "/predictions", token=token).status_code == 200
    finally:
        running.shutdown()
    # The flag was already on disk by the time any prediction was assembled.
    assert seen == [True]


# --------------------------------------------------------------------------------------------
# P1-4 — a preview that cannot be saved has to say so, per row, before it is applied.
# --------------------------------------------------------------------------------------------


def test_collapsing_negative_offset_is_named_per_row_not_discovered_at_save(
    corpus: Path, tmp_path: Path
) -> None:
    truth_path, truth = _truth(corpus)
    shifted, _, collapsed = preview_bulk_offset(truth, -60_000)
    assert collapsed == [1, 2, 3]

    partial, _, partial_collapsed = preview_bulk_offset(truth, -20_000)
    assert partial_collapsed == [1]

    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    with pytest.raises(ValueError, match="row 1 needs a positive audible span"):
        session.save({"rows": _rows(partial)})
    assert all(
        episode.draft
        for episode in GroundTruthRecord.model_validate_json(read_text(truth_path)).episodes
    )


# --------------------------------------------------------------------------------------------
# P1-5 — two review processes must not be able to overwrite each other, or each other's rollback.
# --------------------------------------------------------------------------------------------


def test_second_process_cannot_commit_inside_the_first_writers_window(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth_path, truth = _truth(corpus)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import sys, time
                from pathlib import Path
                sys.path.insert(0, sys.argv[2])
                from id_detector.truth import truth_write_lock
                with truth_write_lock(Path(sys.argv[1])):
                    print("held", flush=True)
                    time.sleep(30)
                """
            ),
            str(truth_path),
            str(ROOT / "src"),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
        monkeypatch.setattr(review_module, "_SAVE_LOCK_TIMEOUT", 1.0)
        assert _SAVE_LOCK_TIMEOUT > 1.0  # the shipped default is patient, the test is not
        with pytest.raises(ValueError, match="another truth writer holds this set"):
            session.save({"rows": _rows(truth)})
        assert all(
            episode.draft
            for episode in GroundTruthRecord.model_validate_json(read_text(truth_path)).episodes
        )
    finally:
        holder.kill()
        holder.wait(timeout=30)


def test_concurrent_sessions_cannot_clobber_each_others_annotation(
    corpus: Path, tmp_path: Path
) -> None:
    truth_path, truth = _truth(corpus)
    first = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    second = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    first.save({"rows": _rows(truth)})
    winner = read_text(_annotation_path(truth_path, "first"))

    # The loser re-checks under the same lock the winner committed under, so it refuses rather
    # than replacing the winner's truth and rolling its annotation back over the winner's.
    with pytest.raises(ValueError, match="changed on disk"):
        second.save({"rows": _rows(truth)})
    assert read_text(_annotation_path(truth_path, "first")) == winner


def test_write_lock_is_reentrant_within_one_process(corpus: Path, tmp_path: Path) -> None:
    truth_path, truth = _truth(corpus)
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    # save() takes the lock and verify_truth takes it again beneath: a non-reentrant lock would
    # deadlock here rather than fail a test.
    done = threading.Event()

    def run() -> None:
        session.save({"rows": _rows(truth)})
        done.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert done.wait(30), "truth_write_lock deadlocked on re-entry"


# --------------------------------------------------------------------------------------------
# P1-6 — nothing beneath the work tree is a corpus record, by any spelling.
# --------------------------------------------------------------------------------------------


def test_corpus_beneath_the_work_root_is_refused(tmp_path: Path) -> None:
    work = tmp_path / "work"
    inside = work / "corpus"
    inside.mkdir(parents=True)
    shutil.copytree(FIXTURE / "fixture-set", inside / "fixture-set")
    with pytest.raises(ValueError, match="beneath the work tree"):
        find_truth_path(inside, "fixture-truth-review", work_root=work)
    with pytest.raises(ValueError, match="beneath the work tree"):
        TruthReviewSession(inside / "fixture-set" / "ground_truth.json", work_root=work)


def test_relative_and_aliased_work_paths_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    inside = work / "corpus"
    inside.mkdir(parents=True)
    shutil.copytree(FIXTURE / "fixture-set", inside / "fixture-set")
    monkeypatch.chdir(tmp_path)
    # A relative --work-root and an upward-traversing --corpus still resolve to the same tree.
    with pytest.raises(ValueError, match="beneath the work tree"):
        find_truth_path(Path("work/../work/corpus"), "fixture-truth-review", work_root=Path("work"))


def test_symlinked_record_into_the_work_root_is_refused(tmp_path: Path) -> None:
    """A corpus entry that is really a link into ``work/`` is refused, not silently written."""

    work = tmp_path / "work"
    (work / "hidden").mkdir(parents=True)
    shutil.copytree(FIXTURE / "fixture-set", work / "hidden" / "fixture-set")
    corpus = tmp_path / "corpus"
    (corpus / "fixture-set").mkdir(parents=True)
    link = corpus / "fixture-set" / "ground_truth.json"
    try:
        link.symlink_to(work / "hidden" / "fixture-set" / "ground_truth.json")
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without developer mode
        pytest.skip("this platform/account cannot create symlinks")
    with pytest.raises(ValueError, match="beneath the work tree"):
        find_truth_path(corpus, "fixture-truth-review", work_root=work)
    with pytest.raises(ValueError, match="beneath the work tree"):
        TruthReviewSession(link, work_root=work)


def test_symlinked_directory_into_the_work_root_is_never_traversed(tmp_path: Path) -> None:
    work = tmp_path / "work"
    (work / "hidden").mkdir(parents=True)
    shutil.copytree(FIXTURE / "fixture-set", work / "hidden" / "fixture-set")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    try:
        (corpus / "fixture-set").symlink_to(
            work / "hidden" / "fixture-set", target_is_directory=True
        )
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without developer mode
        pytest.skip("this platform/account cannot create directory symlinks")
    # rglob does not recurse into symlinked directories, so the record is simply never a
    # candidate -- unreachable is as good as refused, and the direct path is refused anyway.
    with pytest.raises(ValueError, match="set not found in corpus"):
        find_truth_path(corpus, "fixture-truth-review", work_root=work)
    with pytest.raises(ValueError, match="beneath the work tree"):
        TruthReviewSession(corpus / "fixture-set" / "ground_truth.json", work_root=work)


def test_save_touches_only_the_reviewed_sets_files(corpus: Path, tmp_path: Path) -> None:
    truth_path, truth = _truth(corpus)
    other = corpus / "other-set"
    other.mkdir()
    payload = json.loads(read_text(truth_path))
    payload["set_id"] = "fixture-other-set"
    (other / "ground_truth.json").write_text(json.dumps(payload), encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    (work / "sentinel.txt").write_text("untouched", encoding="utf-8")

    def snapshot(root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    before_other = snapshot(other)
    before_work = snapshot(work)
    TruthReviewSession(truth_path, work_root=work).save({"rows": _rows(truth)})

    assert snapshot(other) == before_other
    assert snapshot(work) == before_work
    written = set(snapshot(corpus / "fixture-set"))
    assert written == {"ground_truth.json", "annotation-first.json"}


# --------------------------------------------------------------------------------------------
# P1-3, P1-4 and P1-7 — driven through the page's own JavaScript, not a re-implementation.
# --------------------------------------------------------------------------------------------

_DOM_STUB = r"""
class ClassList{
 constructor(el){this.el=el;}
 add(...c){c.forEach(x=>this.el.classes.add(x));}
 remove(...c){c.forEach(x=>this.el.classes.delete(x));}
 contains(c){return this.el.classes.has(c);}
 toggle(c,on){if(on===undefined)on=!this.el.classes.has(c);
  if(on)this.el.classes.add(c);else this.el.classes.delete(c);return on;}
}
function matchSel(el,sel){return String(sel).split(',').map(s=>s.trim()).some(s=>
 s.startsWith('.')?s.slice(1).split('.').every(c=>c&&el.classes.has(c)):el.tag===s);}
class El{
 constructor(tag,classes,attrs){attrs=attrs||{};this.tag=tag;this.classes=new Set(classes||[]);
  this.children=[];this.dataset=attrs.dataset||{};this.id=attrs.id||'';
  this.value=attrs.value===undefined?'':attrs.value;this._text='';this._html='';
  this.listeners={};this.parent=null;this.classList=new ClassList(this);this.checked=false;}
 get className(){return [...this.classes].join(' ');}
 set className(v){this.classes=new Set(String(v).split(/\s+/).filter(Boolean));}
 get textContent(){return this._text;}
 set textContent(v){this._text=String(v);}
 get innerHTML(){return this._html||this._text;}
 set innerHTML(v){this._html=String(v);}
 appendChild(c){c.parent=this;this.children.push(c);return c;}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);}
 addEventListener(t,f){(this.listeners[t]=this.listeners[t]||[]).push(f);}
 fire(t,ev){(this.listeners[t]||[]).forEach(f=>f(ev||{target:this}));}
 focus(){document.activeElement=this;}
 blur(){if(document.activeElement===this)document.activeElement=document.body;}
 select(){}
 matches(sel){return matchSel(this,sel);}
 descendants(){let out=[];
  for(const c of this.children){out.push(c);out=out.concat(c.descendants());}
  return out;}
 querySelectorAll(sel){return this.descendants().filter(e=>matchSel(e,sel));}
 querySelector(sel){return this.querySelectorAll(sel)[0]||null;}
}
const document={activeElement:null,body:null,_byId:{},listeners:{},
 getElementById(id){return this._byId[id]||null;},
 createElement(tag){return new El(tag);},
 addEventListener(t,f){(this.listeners[t]=this.listeners[t]||[]).push(f);},
 querySelectorAll(sel){return this.body.querySelectorAll(sel);},
 key(ev){(this.listeners.keydown||[]).forEach(f=>f(ev));}};
const window={addEventListener(){}};
globalThis.fetch=async()=>({ok:true,json:async()=>({predictions:[],saved:true})});
function mk(tag,classes,attrs){const e=new El(tag,classes,attrs);
 if(e.id)document._byId[e.id]=e;return e;}
const docBody=mk('body');document.body=docBody;document.activeElement=docBody;
for(const id of ['playhead','claimed','message','help','offset','offset-minus','offset-plus',
                 'apply-offset','undo-offset','show-guesses','save'])docBody.appendChild(mk('div',[],{id:id}));
document._byId['offset'].value='0';
const audioEl=mk('audio',[],{id:'audio'});
audioEl.currentTime=0;audioEl.paused=true;audioEl.duration=60;
audioEl.play=function(){this.paused=false;};audioEl.pause=function(){this.paused=true;};
docBody.appendChild(audioEl);
const tbody=mk('tbody',[],{id:'rows'});docBody.appendChild(tbody);
for(let i=0;i<ROW_COUNT;i++){const tr=mk('tr',['truth-row']);
 tr.appendChild(mk('td',['num']));tr.appendChild(mk('td',['when','start']));
 tr.appendChild(mk('input',['edit','artist'],{dataset:{field:'artist'}}));
 tr.appendChild(mk('input',['edit','title'],{dataset:{field:'title'}}));
 tr.appendChild(mk('span',['state']));tr.appendChild(mk('td',['when','end']));
 tbody.appendChild(tr);}
function press(key,extra){
 const code=key===' '?'Space':'Key'+key.toUpperCase();
 const ev=Object.assign({key:key,code:code,shiftKey:false,ctrlKey:false,
  target:document.activeElement,preventDefault(){}},extra||{});
 document.key(ev);}
function typeInto(el,text){el.value=text;el.fire('input',{target:el});}
function setOffset(v){const el=document.getElementById('offset');
 el.value=String(v);el.fire('input',{target:el});}
"""


def _page_script(corpus: Path, tmp_path: Path) -> tuple[str, int]:
    truth_path, truth = _truth(corpus)
    session = TruthReviewSession(truth_path, work_root=tmp_path / "work")
    page = review_module._page(session, "test-token").decode("utf-8")
    scripts = _inline_scripts(page)
    assert len(scripts) >= 1
    return scripts[-1], len(truth.episodes)


def _run_page_js(corpus: Path, tmp_path: Path, driver: str) -> dict:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - the repo's JS gate skips the same way
        pytest.skip("node is not installed")
    script, row_count = _page_script(corpus, tmp_path)
    harness = tmp_path / "harness.mjs"
    harness.write_text(
        "\n".join(
            [
                f"const ROW_COUNT={row_count};",
                _DOM_STUB,
                script,
                driver,
                "console.log(JSON.stringify(RESULT));",
            ]
        ),
        encoding="utf-8",
    )
    finished = subprocess.run(
        [node, str(harness)], capture_output=True, text=True, check=False, timeout=120
    )
    assert finished.returncode == 0, finished.stderr
    return json.loads(finished.stdout.strip().splitlines()[-1])


def test_shipped_js_offset_undo_keeps_unrelated_edits(corpus: Path, tmp_path: Path) -> None:
    """P1-3: undo owns only the two time fields the offset wrote."""

    result = _run_page_js(
        corpus,
        tmp_path,
        """
        const artistInput=document.querySelectorAll('.truth-row')[1].querySelector('.artist');
        setOffset(5); applyOffset();
        const afterApply=rows[1].start_ms_range[0];
        typeInto(artistInput,'Corrected During Review');
        undo();
        const RESULT={afterApply:afterApply,
          artist:rows[1].artist, inputValue:artistInput.value,
          start:rows[1].start_ms_range[0], undoDepth:offsetUndo.length};
        """,
    )
    assert result["afterApply"] == 25_000
    assert result["start"] == 20_000  # the offset is gone
    assert result["artist"] == "Corrected During Review"  # the correction is not
    assert result["inputValue"] == "Corrected During Review"
    assert result["undoDepth"] == 0


def test_shipped_js_editing_during_a_preview_is_not_discarded(corpus: Path, tmp_path: Path) -> None:
    result = _run_page_js(
        corpus,
        tmp_path,
        """
        const titleInput=document.querySelectorAll('.truth-row')[0].querySelector('.title');
        setOffset(3);
        typeInto(titleInput,'Edited Mid Preview');
        setOffset(6);
        const RESULT={title:rows[0].title,start:rows[0].start_ms_range[0]};
        """,
    )
    assert result["title"] == "Edited Mid Preview"
    assert result["start"] == 6_000


def test_shipped_js_blocks_a_collapsed_preview_with_a_row_reason(
    corpus: Path, tmp_path: Path
) -> None:
    """P1-4: the boundary case is refused at preview time and named row by row."""

    result = _run_page_js(
        corpus,
        tmp_path,
        """
        setOffset(-60);
        const previewMessage=document.getElementById('message').textContent;
        applyOffset();
        const RESULT={previewMessage:previewMessage,
          applyMessage:document.getElementById('message').textContent,
          undoDepth:offsetUndo.length, stillPreviewing:offsetBase!==null,
          collapsedClass:document.querySelectorAll('.truth-row')[0].classList.contains('collapsed')};
        """,
    )
    assert "row 1" in result["previewMessage"] and "row 3" in result["previewMessage"]
    assert "audible span" in result["previewMessage"]
    assert "row 1" in result["applyMessage"]
    assert result["undoDepth"] == 0
    assert result["stillPreviewing"] is True
    assert result["collapsedClass"] is True


def test_shipped_js_keyboard_loop_edits_confirms_and_navigates(
    corpus: Path, tmp_path: Path
) -> None:
    """P1-7: E -> type -> Enter must hand focus back, so N/P/S are keys again, not text."""

    result = _run_page_js(
        corpus,
        tmp_path,
        """
        press('e');
        const focusedAfterEdit=document.activeElement.classes.has('artist');
        typeInto(document.activeElement,'Typed By Owner');
        press('Enter');
        const focusedAfterEnter=document.activeElement===document.body;
        press('n');
        const afterN=current;
        press('p');
        const RESULT={focusedAfterEdit:focusedAfterEdit, artist:rows[0].artist,
          confirmed:rows[0].confirmed, afterN:afterN, afterP:current,
          focusedAfterEnter:focusedAfterEnter,
          editingClass:document.querySelectorAll('.truth-row')[0].classList.contains('editing')};
        """,
    )
    assert result["focusedAfterEdit"] is True
    assert result["artist"] == "Typed By Owner"
    assert result["confirmed"] is True
    assert result["focusedAfterEnter"] is True
    assert result["editingClass"] is False
    assert result["afterN"] == 2  # Enter advanced to row 2, N to row 3
    assert result["afterP"] == 1


def test_shipped_js_escape_discards_the_edit_and_leaves_the_field(
    corpus: Path, tmp_path: Path
) -> None:
    result = _run_page_js(
        corpus,
        tmp_path,
        """
        const before=rows[0].artist;
        press('e');
        typeInto(document.activeElement,'Mistyped');
        press('Escape');
        const RESULT={before:before, after:rows[0].artist, current:current,
          confirmed:rows[0].confirmed===true,
          blurred:document.activeElement===document.body,
          inputValue:document.querySelectorAll('.truth-row')[0].querySelector('.artist').value};
        """,
    )
    assert result["after"] == result["before"]
    assert result["inputValue"] == result["before"]
    assert result["blurred"] is True
    assert result["confirmed"] is False
    assert result["current"] == 0


def test_shipped_js_never_writes_a_guess_into_a_truth_row(corpus: Path, tmp_path: Path) -> None:
    """No one-click path accepts our answer: reveal only appends separate .guess rows."""

    result = _run_page_js(
        corpus,
        tmp_path,
        """
        const guess={artist:'Engine Artist',title:'Engine Title',
          start_ms:1000,end_ms:2000,tier:'likely'};
        globalThis.fetch=async()=>({ok:true,json:async()=>({predictions:[guess]})});
        const box=document.getElementById('show-guesses');box.checked=true;
        await reveal();
        const guesses=document.querySelectorAll('.guess');
        const RESULT={guessRows:guesses.length,
          truthRows:document.querySelectorAll('.truth-row').length,
          artists:rows.map(r=>r.artist),
          guessHasClickHandler:guesses.map(g=>Object.keys(g.listeners).length)};
        """,
    )
    assert result["guessRows"] == 1
    assert result["truthRows"] == 3
    assert not any("Engine" in artist for artist in result["artists"])
    assert result["guessHasClickHandler"] == [0]

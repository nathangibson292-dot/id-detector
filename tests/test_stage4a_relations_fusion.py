from __future__ import annotations

import json
from pathlib import Path

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    ObservationRecord,
    RawLabel,
    SourceRecord,
    compose_natural_key,
    make_id,
)
from id_detector.fuse.episodes import build_episodes
from id_detector.fuse.identity import build_identity_graph
from id_detector.hints.connectors.base import ConnectorOutput, MirrorCandidate
from id_detector.hints.mirrors import MirrorMetadata, mirror_is_verified
from id_detector.hints.parse import HintInput, parse_hint_inputs
from id_detector.hints.pipeline import _release_mirror_quarantine
from id_detector.hints.relations import apply_relations

MEDIA_KEY = "4" * 64
DURATION_MS = 300_000


def _observation(provider: str) -> ObservationRecord:
    label = RawLabel(artist="Artist", title="Title", album=None, label=None, release_date=None)
    query_id = ("1" if provider == "shazam" else "2") * 40
    natural = {
        "query_id": query_id,
        "mix_span_ms": [0, 30_000],
        "raw_label_hash": __import__("hashlib")
        .sha256(json.dumps(label.model_dump(mode="json"), sort_keys=True).encode())
        .hexdigest(),
        "native_index": 0,
    }
    return ObservationRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id(MEDIA_KEY, "observation", compose_natural_key("observation", natural)),
        generation=0,
        query_id=query_id,
        provider=provider,
        capability="clip_recognizer",
        status="match",
        is_final=True,
        mix_span_ms=(0, 30_000),
        support_ms=(0, 30_000),
        transform={"type": "none", "rate_e4": 10_000, "semitones": 0},
        logical_trial_id="3" * 40,
        raw_label=label,
        provider_ids={"shazam": "recording-a", "isrc": "recording-b"},
        native={"matches": [{"offset_ms": 0, "frequencyskew_e6": 0, "timeskew_e6": 0}]},
        anchor={
            "mix_anchor_ms": 0,
            "ref_anchor_ms": 0,
            "uncertainty_ms": 0,
            "reliable": True,
            "method": "fixture",
            "bias_applied_ms": 0,
        },
        score_raw=None,
        quality=None,
        raw_response_ref="fixture-ref",
        source_ids=[f"provider:{provider}"],
    )


def test_relations_corrections_and_copy_provenance_are_deterministic() -> None:
    inputs = [
        HintInput(
            connector="sc_comments",
            source_record_id="question",
            text="track id?",
            position_ms=100_000,
            position_kind="comment_timestamp",
            author_pseudo_id="asker",
            author_permalink="asker-link",
        ),
        HintInput(
            connector="sc_comments",
            source_record_id="answer",
            text="@asker-link: Artist - Title",
            position_ms=120_000,
            position_kind="comment_timestamp",
            author_pseudo_id="answerer",
        ),
        HintInput(
            connector="yt_comments",
            source_record_id="parent",
            text="02:00 Artist - Title",
            author_pseudo_id="yt-author",
        ),
        HintInput(
            connector="yt_comments",
            source_record_id="correction",
            text="actually Artist - Correct Title",
            parent_source_id="parent",
            author_pseudo_id="yt-corrector",
        ),
        HintInput(
            connector="mixesdb",
            source_record_id="copy",
            text="02:00 Artist - Title",
            author_pseudo_id="mixesdb",
        ),
    ]
    parsed = parse_hint_inputs(MEDIA_KEY, DURATION_MS, inputs)
    left = apply_relations(MEDIA_KEY, DURATION_MS, parsed, inputs)
    right = apply_relations(MEDIA_KEY, DURATION_MS, list(reversed(parsed)), inputs)
    assert [item.model_dump_json() for item in left] == [item.model_dump_json() for item in right]
    reply = next(item for item in left if item.raw_text.startswith("@asker-link"))
    assert reply.relations[0].type == "replies_to"
    assert reply.relations[0].confidence < 10_000
    correction = next(item for item in left if item.kind == "correction")
    assert any(relation.type == "corrects" for relation in correction.relations)
    assert correction.position_range_ms == (115_000, 125_000)
    copied = [item for item in left if item.title == "Title"]
    assert len({item.provenance_group for item in copied}) == 1


def test_conflicting_corrections_remain_distinct_and_target_the_same_parent() -> None:
    inputs = [
        HintInput(
            connector="yt_comments",
            source_record_id="parent",
            text="02:00 Artist - Original Title",
            author_pseudo_id="parent-author",
        ),
        HintInput(
            connector="yt_comments",
            source_record_id="correction-a",
            parent_source_id="parent",
            text="actually Artist - Alternate A",
            author_pseudo_id="corrector-a",
        ),
        HintInput(
            connector="yt_comments",
            source_record_id="correction-b",
            parent_source_id="parent",
            text="actually Artist - Alternate B",
            author_pseudo_id="corrector-b",
        ),
    ]
    hints = apply_relations(
        MEDIA_KEY,
        DURATION_MS,
        parse_hint_inputs(MEDIA_KEY, DURATION_MS, inputs),
        inputs,
    )
    parent = next(hint for hint in hints if hint.title == "Original Title")
    corrections = [hint for hint in hints if hint.kind == "correction"]
    assert {hint.title for hint in corrections} == {"Alternate A", "Alternate B"}
    assert all(
        any(
            relation.type == "corrects" and relation.hint_id == parent.id
            for relation in hint.relations
        )
        for hint in corrections
    )
    assert len({hint.provenance_group for hint in corrections}) == 2


def test_hint_vote_raises_work_once_never_version_and_questions_only_rescan() -> None:
    observations = [_observation("shazam"), _observation("audd")]
    tracklist_input = HintInput(
        connector="mixesdb",
        source_record_id="line",
        text="Artist - Title",
        position_ms=0,
        position_end_ms=30_000,
        position_kind="section",
        author_pseudo_id="mixesdb",
        structured_tracklist=True,
    )
    tracklist = parse_hint_inputs(MEDIA_KEY, DURATION_MS, [tracklist_input])
    audio_identity = build_identity_graph(MEDIA_KEY, observations)
    audio, _ = build_episodes(
        media_key=MEDIA_KEY,
        duration_ms=DURATION_MS,
        observations=observations,
        windows=[],
        identity=audio_identity,
    )
    fused_identity = build_identity_graph(MEDIA_KEY, observations, hints=tracklist)
    fused, _ = build_episodes(
        media_key=MEDIA_KEY,
        duration_ms=DURATION_MS,
        observations=observations,
        windows=[],
        identity=fused_identity,
        hints=tracklist,
    )
    assert audio.episodes[0].tiers.work == "unclear"
    assert fused.episodes[0].tiers.work == "possible"
    assert fused.episodes[0].tiers.version == audio.episodes[0].tiers.version == "unclear"
    assert "hint_supported" in fused.episodes[0].flags

    question_inputs = [
        HintInput(
            connector="sc_comments",
            source_record_id=f"q-{index}",
            text="track id?",
            position_ms=value,
            position_kind="comment_timestamp",
            author_pseudo_id=f"asker-{index}",
        )
        for index, value in enumerate((100_000, 130_000, 160_000))
    ]
    questions = parse_hint_inputs(MEDIA_KEY, DURATION_MS, question_inputs)
    question_identity = build_identity_graph(MEDIA_KEY, observations, hints=questions)
    question_episodes, requests = build_episodes(
        media_key=MEDIA_KEY,
        duration_ms=DURATION_MS,
        observations=observations,
        windows=[],
        identity=question_identity,
        hints=questions,
    )
    assert question_episodes.episodes[0].tiers == audio.episodes[0].tiers
    assert any(request.trigger == "question_cluster" for request in requests)


def test_quarantined_hints_never_vote_and_mirror_release_requires_all_conditions() -> None:
    observations = [_observation("shazam"), _observation("audd")]
    quarantined_input = HintInput(
        connector="1001tl",
        source_record_id="line",
        text="Artist - Title",
        position_ms=0,
        position_end_ms=30_000,
        position_kind="section",
        author_pseudo_id="1001tl",
        mirror_of="source-ref",
        mirror_status="quarantined",
        structured_tracklist=True,
    )
    hints = parse_hint_inputs(MEDIA_KEY, DURATION_MS, [quarantined_input])
    identity = build_identity_graph(MEDIA_KEY, observations, hints=hints)
    episodes, _ = build_episodes(
        media_key=MEDIA_KEY,
        duration_ms=DURATION_MS,
        observations=observations,
        windows=[],
        identity=identity,
        hints=hints,
    )
    assert episodes.episodes[0].tiers.work == "unclear"

    source_payload = json.loads(Path("tests/golden/source.json").read_text(encoding="utf-8"))
    source_payload.update(
        {
            "platform_id": "same-platform-ref",
            "uploader_id": "same-uploader-ref",
            "upload_date": "20260904",
        }
    )
    source = SourceRecord.model_validate(source_payload)
    verified_hints = [hint.model_copy(update={"mirror_status": "verified"}) for hint in hints]
    source_hints = [
        hint.model_copy(
            update={
                "id": (str(index + 5) * 40)[:40],
                "provenance_group": (str(index + 1) * 40)[:40],
                "artist": f"Artist {index}",
                "title": f"Title {index}",
                "position_range_ms": (index * 60_000, index * 60_000 + 30_000),
            }
        )
        for index, hint in enumerate(verified_hints * 2)
    ]
    mirror_hints = [
        hint.model_copy(
            update={
                "id": (str(index + 7) * 40)[:40],
                "provenance_group": (str(index + 3) * 40)[:40],
                "artist": f"Artist {index}",
                "title": f"Title {index}",
                "position_range_ms": (index * 60_000, index * 60_000 + 30_000),
            }
        )
        for index, hint in enumerate(verified_hints * 2)
    ]
    metadata = MirrorMetadata("same-platform-ref", None, None, DURATION_MS)
    assert mirror_is_verified(
        source,
        source_duration_ms=DURATION_MS,
        mirror=metadata,
        source_hints=source_hints,
        mirror_hints=mirror_hints,
    )
    duplicate_mirror_hints = [
        mirror_hints[0],
        mirror_hints[0].model_copy(update={"id": "9" * 40}),
    ]
    assert not mirror_is_verified(
        source,
        source_duration_ms=DURATION_MS,
        mirror=metadata,
        source_hints=source_hints,
        mirror_hints=duplicate_mirror_hints,
    )
    assert not mirror_is_verified(
        source,
        source_duration_ms=DURATION_MS,
        mirror=MirrorMetadata("same-platform-ref", None, None, DURATION_MS + 7_000),
        source_hints=source_hints,
        mirror_hints=mirror_hints,
    )


def test_pipeline_releases_mirrors_after_provenance_or_manual_confirmation() -> None:
    source_payload = json.loads(Path("tests/golden/source.json").read_text(encoding="utf-8"))
    source_payload.update({"platform_id": "matching-platform-id"})
    source = SourceRecord.model_validate(source_payload)
    source_inputs = [
        HintInput(
            connector="yt_chapters",
            source_record_id=f"source-{index}",
            text=f"Artist {index} - Title {index}",
            position_ms=index * 60_000,
            position_end_ms=index * 60_000 + 30_000,
            position_kind="chapter",
            author_pseudo_id="uploader",
            is_uploader=True,
            structured_tracklist=True,
        )
        for index in range(2)
    ]
    mirror_input = HintInput(
        connector="1001tl",
        source_record_id="imported-tracklist-distinct",
        text="00:00 Artist 0 - Title 0\n01:00 Artist 1 - Title 1",
        author_pseudo_id="1001tl",
        mirror_of=source.canonical_url,
        mirror_status="quarantined",
        structured_tracklist=True,
    )
    all_inputs = [*source_inputs, mirror_input]
    hints = apply_relations(
        source.media_key,
        DURATION_MS,
        parse_hint_inputs(source.media_key, DURATION_MS, all_inputs),
        all_inputs,
    )
    candidate = MirrorCandidate(
        requested_url="https://1001.tl/mirror",
        final_url="https://1001.tl/mirror",
        platform_id="matching-platform-id",
        duration_ms=DURATION_MS,
        source_record_ids=(mirror_input.source_record_id,),
    )
    released, releases, confirmations = _release_mirror_quarantine(
        source=source,
        duration_ms=DURATION_MS,
        hints=hints,
        outputs=[ConnectorOutput(inputs=(mirror_input,), mirror_candidate=candidate)],
        confirmed_mirrors=(),
    )
    mirror_hints = [hint for hint in released if hint.mirror_of is not None]
    assert len(mirror_hints) == 2
    assert all(hint.mirror_status == "verified" for hint in mirror_hints)
    assert releases == [
        {
            "url": "https://1001.tl/mirror",
            "method": "agreement",
            "hints_released": 2,
        }
    ]
    assert confirmations == []

    no_metadata = candidate.__class__(
        requested_url="https://1001.tl/manual",
        final_url="https://1001.tl/manual",
        source_record_ids=(mirror_input.source_record_id,),
    )
    manual, releases, confirmations = _release_mirror_quarantine(
        source=source,
        duration_ms=DURATION_MS,
        hints=hints,
        outputs=[ConnectorOutput(inputs=(mirror_input,), mirror_candidate=no_metadata)],
        confirmed_mirrors=("https://1001.tl/manual",),
    )
    assert all(hint.mirror_status == "verified" for hint in manual if hint.mirror_of is not None)
    assert releases[0]["method"] == "manual"
    assert confirmations == [{"url": "https://1001.tl/manual", "matched_import": True}]

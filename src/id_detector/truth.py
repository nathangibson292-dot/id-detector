"""Draft, independent annotation, resolution, and corpus-freeze tooling for benchmark truth."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any

from id_detector.contracts import GroundTruthRecord, TruthRoleSegment
from id_detector.io import (
    atomic_write_json,
    canonical_json_bytes,
    path_is_file,
    read_text,
    sha256_file,
)

Input = Callable[[str], str]
Output = Callable[[str], None]
_TRACKLIST = re.compile(
    r"^\s*(?:(?P<time>\d+(?::\d{1,2}){1,2})\s+)?(?P<artist>.+?)\s+-\s+(?P<title>.+?)\s*$"
)


def _parse_time(value: str) -> int:
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        if seconds > 59:
            raise ValueError(f"invalid timestamp: {value}")
        return (minutes * 60 + seconds) * 1000
    if len(parts) == 3:
        hours, minutes, seconds = parts
        if minutes > 59 or seconds > 59:
            raise ValueError(f"invalid timestamp: {value}")
        return (hours * 3600 + minutes * 60 + seconds) * 1000
    raise ValueError(f"invalid timestamp: {value}")


def _read_json_or_jsonl(path: Path) -> list[dict[str, Any]]:
    text = read_text(path)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("episodes", "hints", "tracks"):
            if isinstance(value.get(key), list):
                return value[key]
        return [value]
    raise ValueError(f"unsupported hints shape in {path}")


def _seed_entries(hints: Path | None, tracklist: Path | None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if hints is not None:
        for item in _read_json_or_jsonl(hints):
            artist = item.get("artist") or item.get("work", {}).get("artist")
            title = item.get("title") or item.get("work", {}).get("title")
            if not artist or not title:
                continue
            position = item.get("position_range_ms") or item.get("start_ms_range")
            entries.append(
                {
                    "artist": str(artist),
                    "title": str(title),
                    "version_qualifier": item.get("version_qualifier"),
                    "position": list(position) if position is not None else None,
                }
            )
    if tracklist is not None:
        for line_number, line in enumerate(read_text(tracklist).splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = _TRACKLIST.match(line)
            if not match:
                raise ValueError(f"invalid manual tracklist line {line_number}: {line}")
            at_ms = _parse_time(match.group("time")) if match.group("time") else None
            entries.append(
                {
                    "artist": match.group("artist"),
                    "title": match.group("title"),
                    "version_qualifier": None,
                    "position": [at_ms, at_ms] if at_ms is not None else None,
                }
            )
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int | None]] = set()
    for item in entries:
        start = item["position"][0] if item["position"] else None
        key = (item["artist"].casefold(), item["title"].casefold(), start)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _write_source_links(project_root: Path, links: dict[str, str]) -> None:
    if not links:
        return
    path = project_root / "data" / "local" / "source_links.json"
    existing: dict[str, str] = {}
    if path.exists():
        existing = json.loads(read_text(path))
    existing.update(links)
    atomic_write_json(path, existing)


def seed_truth(
    *,
    out_path: Path,
    set_id: str,
    duration_ms: int,
    media_key: str,
    hints: Path | None = None,
    tracklist: Path | None = None,
    split: str = "dev-1",
    stratum: str = "catalogue-covered",
    corpus_version: str = "draft",
    platform: str = "local",
    selection_basis: str = "manual seed assembled before scoring",
    source_url: str | None = None,
    uploader: str | None = None,
    event: str | None = None,
    project_root: Path | None = None,
) -> GroundTruthRecord:
    entries = _seed_entries(hints, tracklist)
    if not entries:
        raise ValueError("seed needs at least one usable hint or manual tracklist entry")
    if duration_ms <= 0:
        raise ValueError("duration_ms must be positive")
    has_timed = any(item["position"] is not None for item in entries)
    if has_timed and any(item["position"] is None for item in entries):
        raise ValueError("mixed timed and untimed seeds require an explicit cue for every entry")
    entries.sort(key=lambda item: item["position"][0] if item["position"] else duration_ms + 1)
    positions: list[list[int]] = []
    for index, item in enumerate(entries):
        if item["position"] is not None:
            position = [
                max(0, int(item["position"][0])),
                min(duration_ms, int(item["position"][1])),
            ]
        else:
            point = index * duration_ms // len(entries)
            position = [point, point]
        positions.append(position)
    episodes: list[dict[str, Any]] = []
    occurrences: dict[tuple[str, str], int] = {}
    for index, (item, start_range) in enumerate(zip(entries, positions, strict=True)):
        end_range = (
            positions[index + 1] if index + 1 < len(positions) else [duration_ms, duration_ms]
        )
        work_identity = (item["artist"].casefold(), item["title"].casefold())
        occurrence_index = occurrences.get(work_identity, 0)
        occurrences[work_identity] = occurrence_index + 1
        episodes.append(
            {
                "work": {"artist": item["artist"], "title": item["title"]},
                "version": {"qualifier": item["version_qualifier"], "ids": {}},
                "version_verified": False,
                "verified_against": None,
                "start_ms_range": start_range,
                "end_ms_range": end_range,
                "audible_rule": "manual annotation required",
                "role_segments": [
                    {"from_ms": start_range[0], "to_ms": end_range[1], "role": "uncertain"}
                ],
                "overlaps_with": [],
                "occurrence_index": occurrence_index,
                "in_reference_pool": False,
                "annotator_ref": None,
                "second_pass_ref": None,
                "disagreement_resolution": None,
                "note": None,
                "draft": True,
            }
        )
    source_ref = f"source-{set_id}"
    uploader_ref = f"uploader-{set_id}"
    event_ref = f"event-{set_id}" if event else None
    truth = GroundTruthRecord(
        schema_version="1.0.0",
        generated_by="id-detector/0.1.0",
        set_id=set_id,
        source={
            "url_ref": source_ref,
            "media_key": media_key,
            "duration_ms": duration_ms,
            "platform": platform,
            "uploader_ref": uploader_ref,
            "event_ref": event_ref,
            "date": None,
        },
        stratum=stratum,
        split=split,
        corpus_version=corpus_version,
        selection_basis=selection_basis,
        episodes=episodes,
        regions=[],
    )
    atomic_write_json(out_path, truth)
    links = {
        key: value
        for key, value in (
            (source_ref, source_url),
            (uploader_ref, uploader),
            (event_ref, event),
        )
        if key is not None and value is not None
    }
    _write_source_links(project_root or Path.cwd(), links)
    return truth


def _parse_range(value: str, current: tuple[int, int] | None = None) -> list[int]:
    if not value.strip() and current is not None:
        return list(current)
    parts = [part.strip() for part in value.replace("-", ",").split(",")]
    if len(parts) != 2 or any(not part.isdigit() for part in parts):
        raise ValueError("range must be START_MS,END_MS")
    result = [int(parts[0]), int(parts[1])]
    if result[1] < result[0]:
        raise ValueError("range end must not precede start")
    return result


def _ffplay_command(audio: Path | None, start_ms: int | None, duration_ms: int | None) -> str:
    args = ["ffplay", "-nodisp", "-autoexit"]
    if start_ms is not None:
        args.extend(("-ss", f"{start_ms / 1000:g}"))
    if duration_ms is not None:
        args.extend(("-t", f"{duration_ms / 1000:g}"))
    args.append(str(audio) if audio is not None else "MIX_AUDIO_FILE")
    return subprocess.list2cmdline(args)


def _annotation_path(truth_path: Path, pass_name: str) -> Path:
    return truth_path.with_name(f"annotation-{pass_name}.json")


def _episode_content(episode: Any) -> dict[str, Any]:
    payload = episode.model_dump(mode="json") if hasattr(episode, "model_dump") else dict(episode)
    for key in ("annotator_ref", "second_pass_ref", "disagreement_resolution", "draft"):
        payload.pop(key, None)
    return payload


def _annotation_content(truth: GroundTruthRecord) -> dict[str, Any]:
    return {
        "episodes": [_episode_content(episode) for episode in truth.episodes],
        "regions": [region.model_dump(mode="json") for region in truth.regions],
    }


def _write_annotation_pass(
    truth_path: Path,
    pass_name: str,
    *,
    set_id: str,
    annotator_ref: str,
    mode: str,
    content: dict[str, Any],
) -> None:
    digest = sha256(canonical_json_bytes(content)).hexdigest()
    atomic_write_json(
        _annotation_path(truth_path, pass_name),
        {
            "schema_version": "1.0.0",
            "generated_by": "id-detector/0.1.0",
            "set_id": set_id,
            "pass": pass_name,
            "annotator_ref": annotator_ref,
            "mode": mode,
            "content_sha256": digest,
            **content,
        },
    )


def _read_annotation_pass(truth_path: Path, pass_name: str) -> dict[str, Any]:
    path = _annotation_path(truth_path, pass_name)
    if not path_is_file(path):
        raise ValueError(f"missing {pass_name} annotation pass: {path.name}")
    payload = json.loads(read_text(path))
    content = {"episodes": payload.get("episodes"), "regions": payload.get("regions")}
    digest = sha256(canonical_json_bytes(content)).hexdigest()
    if payload.get("pass") != pass_name or payload.get("content_sha256") != digest:
        raise ValueError(f"invalid {pass_name} annotation pass")
    return payload


def _load_independent_annotation(annotation_path: Path, base: GroundTruthRecord) -> dict[str, Any]:
    annotation = GroundTruthRecord.model_validate_json(read_text(annotation_path))
    if annotation.set_id != base.set_id:
        raise ValueError("annotation set_id differs from the seeded truth")
    if annotation.source.media_key != base.source.media_key:
        raise ValueError("annotation media_key differs from the seeded truth")
    if annotation.source.duration_ms != base.source.duration_ms:
        raise ValueError("annotation duration differs from the seeded truth")
    for index, episode in enumerate(annotation.episodes):
        if episode.draft or episode.verified_against is None:
            raise ValueError(f"annotation episode {index} is not a completed work annotation")
    return _annotation_content(annotation)


def _truth_with_content(
    base: GroundTruthRecord,
    content: dict[str, Any],
    *,
    first_ref: str,
    second_ref: str | None,
    resolution: str | None,
) -> GroundTruthRecord:
    episodes = [
        {
            **episode,
            "annotator_ref": first_ref,
            "second_pass_ref": second_ref,
            "disagreement_resolution": resolution,
            "draft": False,
        }
        for episode in content["episodes"]
    ]
    return GroundTruthRecord.model_validate(
        {
            **base.model_dump(mode="json"),
            "episodes": episodes,
            "regions": content["regions"],
        }
    )


def verify_truth(
    truth_path: Path,
    *,
    annotator_ref: str,
    audio: Path | None = None,
    annotation_path: Path | None = None,
    input_fn: Input = input,
    output_fn: Output = print,
) -> GroundTruthRecord:
    truth = GroundTruthRecord.model_validate_json(read_text(truth_path))
    if annotation_path is not None:
        content = _load_independent_annotation(annotation_path, truth)
        updated = _truth_with_content(
            truth,
            content,
            first_ref=annotator_ref,
            second_ref=None,
            resolution=None,
        )
        atomic_write_json(truth_path, updated)
        _write_annotation_pass(
            truth_path,
            "first",
            set_id=truth.set_id,
            annotator_ref=annotator_ref,
            mode="independent",
            content=content,
        )
        return updated
    retained: list[Any] = []
    for index, episode in enumerate(truth.episodes):
        if not episode.draft:
            retained.append(episode)
            continue
        region_start = max(0, episode.start_ms_range[0] - 10_000)
        region_end = min(truth.source.duration_ms, episode.end_ms_range[1] + 10_000)
        output_fn(
            f"episode {index + 1}/{len(truth.episodes)}: "
            f"{episode.work.artist} - {episode.work.title}; "
            f"mix {region_start}..{region_end} ms"
        )
        output_fn(_ffplay_command(audio, region_start, region_end - region_start))
        decision = input_fn("decision [accept/reject/skip]: ").strip().casefold()
        if decision == "skip":
            retained.append(episode)
            continue
        if decision == "reject":
            continue
        if decision != "accept":
            raise ValueError(f"unknown decision: {decision}")
        start_range = _parse_range(
            input_fn("start range START_MS,END_MS (blank keeps draft): "), episode.start_ms_range
        )
        end_range = _parse_range(
            input_fn("end range START_MS,END_MS (blank keeps draft): "), episode.end_ms_range
        )
        verified_against = input_fn(
            "verified against [audio/source_recording/authoritative_metadata]: "
        ).strip()
        if verified_against not in {"audio", "source_recording", "authoritative_metadata"}:
            raise ValueError("invalid verified_against decision")
        version_verified = input_fn("exact version verified [y/n]: ").strip().casefold() in {
            "y",
            "yes",
        }
        roles = [
            TruthRoleSegment(
                from_ms=start_range[0],
                to_ms=end_range[1],
                role="dominant",
            )
        ]
        retained.append(
            episode.model_copy(
                update={
                    "start_ms_range": tuple(start_range),
                    "end_ms_range": tuple(end_range),
                    "verified_against": verified_against,
                    "version_verified": version_verified,
                    "annotator_ref": annotator_ref,
                    "role_segments": roles,
                    "draft": False,
                }
            )
        )
    updated = truth.model_copy(update={"episodes": retained})
    updated = GroundTruthRecord.model_validate(updated.model_dump(mode="json"))
    atomic_write_json(truth_path, updated)
    _write_annotation_pass(
        truth_path,
        "first",
        set_id=truth.set_id,
        annotator_ref=annotator_ref,
        mode="seed-review",
        content=_annotation_content(updated),
    )
    return updated


def second_pass_truth(
    truth_path: Path,
    *,
    annotator_ref: str,
    audio: Path | None = None,
    annotation_path: Path | None = None,
    input_fn: Input = input,
    output_fn: Output = print,
) -> GroundTruthRecord:
    truth = GroundTruthRecord.model_validate_json(read_text(truth_path))
    first = _read_annotation_pass(truth_path, "first")
    first_ref = str(first["annotator_ref"])
    if annotator_ref == first_ref:
        raise ValueError("second-pass annotator must differ from the first-pass annotator")
    if annotation_path is not None:
        second_content = _load_independent_annotation(annotation_path, truth)
        mode = "independent"
    else:
        if truth.split == "test":
            raise ValueError("test truth requires an independent second-pass annotation file")
        guided_episodes: list[Any] = []
        for index, episode in enumerate(truth.episodes):
            if episode.draft:
                raise ValueError("second pass cannot annotate a draft episode")
            output_fn(
                f"blind episode {index + 1}/{len(truth.episodes)}: "
                f"{episode.work.artist} - {episode.work.title}; first-pass boundaries hidden"
            )
            output_fn(_ffplay_command(audio, None, None))
            start_range = _parse_range(input_fn("blind start range START_MS,END_MS: "))
            end_range = _parse_range(input_fn("blind end range START_MS,END_MS: "))
            verified_against = input_fn(
                "blind verified against [audio/source_recording/authoritative_metadata]: "
            ).strip()
            if verified_against not in {"audio", "source_recording", "authoritative_metadata"}:
                raise ValueError("invalid verified_against decision")
            version_verified = input_fn(
                "blind exact version verified [y/n]: "
            ).strip().casefold() in {"y", "yes"}
            guided_episodes.append(
                episode.model_copy(
                    update={
                        "start_ms_range": tuple(start_range),
                        "end_ms_range": tuple(end_range),
                        "verified_against": verified_against,
                        "version_verified": version_verified,
                        "role_segments": [
                            TruthRoleSegment(
                                from_ms=start_range[0],
                                to_ms=end_range[1],
                                role="dominant",
                            )
                        ],
                    }
                )
            )
        guided = truth.model_copy(update={"episodes": guided_episodes})
        second_content = _annotation_content(
            GroundTruthRecord.model_validate(guided.model_dump(mode="json"))
        )
        mode = "guided"
    _write_annotation_pass(
        truth_path,
        "second",
        set_id=truth.set_id,
        annotator_ref=annotator_ref,
        mode=mode,
        content=second_content,
    )
    first_content = {"episodes": first["episodes"], "regions": first["regions"]}
    agrees = canonical_json_bytes(first_content) == canonical_json_bytes(second_content)
    resolution = "agreed" if agrees else "unresolved:third-annotator-required"
    updated = _truth_with_content(
        truth,
        first_content,
        first_ref=first_ref,
        second_ref=annotator_ref,
        resolution=resolution,
    )
    atomic_write_json(truth_path, updated)
    return updated


def resolve_truth(
    truth_path: Path,
    *,
    resolver_ref: str,
    annotation_path: Path,
) -> GroundTruthRecord:
    truth = GroundTruthRecord.model_validate_json(read_text(truth_path))
    first = _read_annotation_pass(truth_path, "first")
    second = _read_annotation_pass(truth_path, "second")
    annotators = {str(first["annotator_ref"]), str(second["annotator_ref"])}
    if len(annotators) != 2:
        raise ValueError("first and second annotation passes must use distinct annotators")
    if resolver_ref in annotators:
        raise ValueError("resolver must be a third, distinct annotator")
    first_content = {"episodes": first["episodes"], "regions": first["regions"]}
    second_content = {"episodes": second["episodes"], "regions": second["regions"]}
    if canonical_json_bytes(first_content) == canonical_json_bytes(second_content):
        raise ValueError("matching passes do not need third-annotator resolution")
    resolved_content = _load_independent_annotation(annotation_path, truth)
    _write_annotation_pass(
        truth_path,
        "resolution",
        set_id=truth.set_id,
        annotator_ref=resolver_ref,
        mode="independent",
        content=resolved_content,
    )
    updated = _truth_with_content(
        truth,
        resolved_content,
        first_ref=str(first["annotator_ref"]),
        second_ref=str(second["annotator_ref"]),
        resolution=f"resolved-by:{resolver_ref}",
    )
    atomic_write_json(truth_path, updated)
    return updated


def freeze_truth(truth_dir: Path, *, corpus_version: str, out_path: Path) -> dict[str, Any]:
    candidates = (
        [truth_dir] if truth_dir.is_file() else sorted(truth_dir.rglob("ground_truth.json"))
    )
    if not candidates:
        raise ValueError("freeze found no ground_truth.json files")
    truths: list[tuple[Path, GroundTruthRecord]] = []
    errors: list[str] = []
    for path in candidates:
        truth = GroundTruthRecord.model_validate_json(read_text(path))
        for index, episode in enumerate(truth.episodes):
            prefix = f"{truth.set_id} episode {index}"
            if episode.draft:
                errors.append(f"{prefix} is still draft")
            if episode.verified_against is None or episode.annotator_ref is None:
                errors.append(f"{prefix} has no completed first-pass verification")
            if truth.split == "test" and episode.second_pass_ref is None:
                errors.append(f"{prefix} lacks the required blind second pass")
            if episode.disagreement_resolution and episode.disagreement_resolution.startswith(
                "unresolved"
            ):
                errors.append(f"{prefix} has an unresolved disagreement")
        if truth.split == "test":
            try:
                first = _read_annotation_pass(path, "first")
                second = _read_annotation_pass(path, "second")
                first_ref = str(first["annotator_ref"])
                second_ref = str(second["annotator_ref"])
                if first.get("set_id") != truth.set_id or second.get("set_id") != truth.set_id:
                    errors.append(f"{truth.set_id} annotation pass set_id differs")
                if first_ref == second_ref:
                    errors.append(f"{truth.set_id} annotation passes are not independent")
                if second.get("mode") != "independent":
                    errors.append(f"{truth.set_id} second pass was not independently authored")
                first_content = {"episodes": first["episodes"], "regions": first["regions"]}
                second_content = {"episodes": second["episodes"], "regions": second["regions"]}
                disagree = canonical_json_bytes(first_content) != canonical_json_bytes(
                    second_content
                )
                expected = first_content
                if disagree:
                    resolution = _read_annotation_pass(path, "resolution")
                    resolver_ref = str(resolution["annotator_ref"])
                    if resolution.get("set_id") != truth.set_id:
                        errors.append(f"{truth.set_id} resolution set_id differs")
                    if resolver_ref in {first_ref, second_ref}:
                        errors.append(f"{truth.set_id} resolver is not a distinct third annotator")
                    expected = {
                        "episodes": resolution["episodes"],
                        "regions": resolution["regions"],
                    }
                    if any(
                        episode.disagreement_resolution != f"resolved-by:{resolver_ref}"
                        for episode in truth.episodes
                    ):
                        errors.append(f"{truth.set_id} does not record third-annotator resolution")
                elif any(episode.disagreement_resolution != "agreed" for episode in truth.episodes):
                    errors.append(f"{truth.set_id} does not record pass agreement")
                if any(
                    episode.annotator_ref != first_ref or episode.second_pass_ref != second_ref
                    for episode in truth.episodes
                ):
                    errors.append(f"{truth.set_id} final truth has inconsistent annotator refs")
                if canonical_json_bytes(_annotation_content(truth)) != canonical_json_bytes(
                    expected
                ):
                    errors.append(f"{truth.set_id} frozen truth differs from its annotation record")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(f"{truth.set_id} invalid independent annotation record: {exc}")
        truths.append((path, truth))
    if errors:
        raise ValueError("cannot freeze:\n" + "\n".join(errors))
    if len({truth.set_id for _, truth in truths}) != len(truths):
        raise ValueError("cannot freeze duplicate set_id values")
    for path, truth in truths:
        frozen = truth.model_copy(update={"corpus_version": corpus_version})
        atomic_write_json(path, GroundTruthRecord.model_validate(frozen.model_dump(mode="json")))
    base = truth_dir.resolve() if truth_dir.is_dir() else truth_dir.resolve().parent
    manifest = {
        "schema_version": "1.0.0",
        "generated_by": "id-detector/0.1.0",
        "corpus_version": corpus_version,
        "frozen": True,
        "sets": [
            {
                "set_id": truth.set_id,
                "path": path.resolve().relative_to(base).as_posix(),
                "sha256": sha256_file(path),
                "annotation_passes": {
                    name: (
                        sha256_file(_annotation_path(path, name))
                        if path_is_file(_annotation_path(path, name))
                        else None
                    )
                    for name in ("first", "second", "resolution")
                },
            }
            for path, truth in sorted(truths, key=lambda item: item[1].set_id)
        ],
    }
    atomic_write_json(out_path, manifest)
    return manifest


def write_draft_manifest(truth_dir: Path, *, corpus_version: str, out_path: Path) -> dict[str, Any]:
    """Inventory draft truth without implying that human verification has happened."""

    candidates = sorted(truth_dir.rglob("ground_truth.json"))
    if not candidates:
        raise ValueError("draft manifest found no ground_truth.json files")
    base = truth_dir.resolve()
    entries: list[dict[str, Any]] = []
    for path in candidates:
        truth = GroundTruthRecord.model_validate_json(read_text(path))
        if truth.corpus_version != corpus_version:
            raise ValueError(f"{truth.set_id} corpus_version differs from {corpus_version}")
        entries.append(
            {
                "set_id": truth.set_id,
                "path": path.resolve().relative_to(base).as_posix(),
                "sha256": sha256_file(path),
                "episode_count": len(truth.episodes),
                "all_episodes_draft": all(episode.draft for episode in truth.episodes),
            }
        )
    manifest = {
        "schema_version": "1.0.0",
        "generated_by": "id-detector/0.1.0",
        "corpus_version": corpus_version,
        "frozen": False,
        "verification_status": "unverified_seed_drafts_not_truth",
        "warning": "Do not use these seeds as verified benchmark truth.",
        "sets": sorted(entries, key=lambda item: item["set_id"]),
    }
    atomic_write_json(out_path, manifest)
    return manifest

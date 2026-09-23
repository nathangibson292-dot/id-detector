"""Measure stored results versus current offline fusion without changing cached mixes.

Use --work-root and optionally --run-list (score_corpus format) for measured truth.
All derived files live in an OS temporary directory. Unprovable inputs are reported
as skipped, never silently replaced by a fresh recognition or hint fetch.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from id_detector.fuse.episodes import _eligible_tracklist_hint, build_episodes
from id_detector.fuse.identity import build_identity_graph
from id_detector.io import atomic_write_json
from id_detector.present.bundles import load_run_snapshot
from id_detector.present.exports import flatten_tracklist
from id_detector.providers.base import AppConfig
from id_detector.refusion import NotRebuildable, _source_fuse_dir, _thresholds, load_fusion_inputs

try:
    from scripts.score_corpus import RunList, assert_identities_cover, load_run_list, score_run_list
except ModuleNotFoundError:
    from score_corpus import RunList, assert_identities_cover, load_run_list, score_run_list


def counts(episodes, identities, hints, duration_ms: int, config: AppConfig) -> tuple[int, ...]:
    attached = {e for episode in episodes.episodes for e in episode.evidence}
    hint_ids = {hint.id for hint in hints}
    eligible = {hint.id for hint in hints if _eligible_tracklist_hint(hint)}
    listed = len(
        flatten_tracklist(
            episodes, identities, min_track_ms=config.present_min_track_ms, collapse=config.collapse
        )
    )
    return (
        len(attached & eligible),
        len(attached & hint_ids),
        sum("hint_supported" in e.flags for e in episodes.episodes),
        listed,
    )


def measure(work_root: Path, run_list: Path | None, config: AppConfig) -> str:
    baseline = load_run_list(run_list) if run_list else None
    lines = [
        "Offline measurement; cached files are read-only.",
        "Media | Eligible hints | All attached hints | Supported episodes | Listed rows",
        "--- | --- | --- | --- | ---",
    ]
    before_runs, after_runs = [], []
    totals = [[0] * 4, [0] * 4]
    seen = set()
    with tempfile.TemporaryDirectory(prefix="idea-refusion-measure-") as temporary:
        scratch = Path(temporary)
        for source in sorted(work_root.glob("*/*/ingest/source.json")):
            media = source.parents[1]
            try:
                snapshot = load_run_snapshot(media)
                if snapshot.source.media_key in seen:
                    continue
                assert_identities_cover(
                    snapshot.episodes, snapshot.identities, snapshot.directory, media.name[:12]
                )
                inputs = load_fusion_inputs(media, _source_fuse_dir(snapshot, media))
                if any(e.score_kind == "calibrated" for e in snapshot.episodes.episodes):
                    raise NotRebuildable("calibrated result needs its original calibrator")
                if (
                    inputs.duration_ms != snapshot.duration_ms
                    or inputs.generation != snapshot.episodes.generation
                ):
                    raise NotRebuildable("stored result and proven input geometry differ")
                identity = build_identity_graph(
                    snapshot.source.media_key, inputs.observations, hints=inputs.hints
                )
                overlap, separation = _thresholds(snapshot.manifest, snapshot.metadata)
                after, _ = build_episodes(
                    media_key=snapshot.source.media_key,
                    duration_ms=inputs.duration_ms,
                    observations=inputs.observations,
                    windows=inputs.windows,
                    identity=identity,
                    hints=inputs.hints,
                    generation=inputs.generation,
                    config=config,
                    overlap_min_ms=overlap,
                    separation_min_ms=separation,
                )
                values = [
                    counts(
                        snapshot.episodes,
                        snapshot.identities,
                        inputs.hints,
                        inputs.duration_ms,
                        config,
                    ),
                    counts(after, identity.record, inputs.hints, inputs.duration_ms, config),
                ]
                seen.add(snapshot.source.media_key)
                for total, value in zip(totals, values, strict=True):
                    for i, count in enumerate(value):
                        total[i] += count
                lines.append(
                    media.name[:12]
                    + " | "
                    + " | ".join(f"{a} -> {b}" for a, b in zip(*values, strict=True))
                )
                if baseline is None:
                    continue
                for entry in baseline.runs:
                    from id_detector.contracts import GroundTruthRecord
                    from id_detector.io import read_text

                    truth = GroundTruthRecord.model_validate_json(read_text(entry.truth))
                    if truth.source.media_key != snapshot.source.media_key:
                        continue
                    for label, episodes, identities, runs in (
                        ("before", snapshot.episodes, snapshot.identities, before_runs),
                        ("after", after, identity.record, after_runs),
                    ):
                        folder = scratch / label / entry.mix_id
                        atomic_write_json(folder / "episodes.json", episodes)
                        atomic_write_json(folder / "identities.json", identities)
                        runs.append(
                            entry.model_copy(
                                update={
                                    "episodes": folder / "episodes.json",
                                    "identities": folder / "identities.json",
                                    "media_key": snapshot.source.media_key,
                                }
                            )
                        )
            except (OSError, ValueError, KeyError, StopIteration, NotRebuildable) as exc:
                lines.append(
                    f"{media.name[:12]}: SKIPPED: {str(exc) or 'incomplete identity graph'}"
                )
        lines.append("Total | " + " | ".join(f"{a} -> {b}" for a, b in zip(*totals, strict=True)))
        if before_runs:
            for label, runs in (("Before", before_runs), ("After", after_runs)):
                document = score_run_list(
                    RunList(recipe=baseline.recipe, runs=runs),
                    run_list_dir=scratch,
                    artefact_dir=scratch / f"score-{label}",
                    out_dir=scratch,
                )
                lines.append(
                    f"{label}: {len(runs)} truth mixes, {document['match_mode']} matching; "
                    f"recall {document['work_recall_e4'] / 100:.1f}%; "
                    f"precision {document['work_precision_e4'] / 100:.1f}%; "
                    f"likely precision {document['likely_precision_e4'] / 100:.1f}%."
                )
        lines.append(
            "Deep re-fusion measures its stored evidence only; "
            "it does not simulate a new secondary selection. Development scores, not certification."
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--run-list", type=Path)
    parser.add_argument("--config", type=Path, default=Path("idea.toml"))
    args = parser.parse_args(argv)
    print(measure(args.work_root, args.run_list, AppConfig.load(args.config)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

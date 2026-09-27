"""Measure "paid adds to free" policies offline, from stored Free evidence and CACHED paid answers.

Nothing here can spend: every paid answer is read from an existing clip cache (keyed by clip
content, exactly as the live sweep keys it), and a window without a cached answer is COUNTED as a
request the policy would have cost, never sent.  The process runs with an empty ``AUDD_API_TOKEN``,
``IDEA_ENGINE_SHAZAM=off`` and a socket guard that raises on any attempted connection.

Inputs are read-only: ``--work-root`` holds the stored Free results (their proven fusion inputs are
read with :func:`id_detector.refusion.load_fusion_inputs`), ``--paid-root`` holds the paid clip
cache (``recognise/invocations/live-audd-clip-v1/raw``).  All derived files go to a temporary
directory that is deleted afterwards.

Policies (``id_detector.additive``):

* ``everywhere`` -- paid on every frozen window, added to the full free evidence;
* ``gaps`` -- paid only where the free result lists nothing at all;
* ``gaps_and_unsure`` -- paid on the gaps plus every span the free result is not confident about.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from id_detector.additive import (
    POLICIES,
    additive_merge,
    fuse_offline,
    paid_observations_from_cache,
    paid_windows,
    policy_targets,
)
from id_detector.contracts import GroundTruthRecord, WindowRecord
from id_detector.io import atomic_write_json, read_text
from id_detector.present.bundles import legacy_result_metadata, load_run_snapshot
from id_detector.providers.base import AppConfig
from id_detector.refusion import _source_fuse_dir, load_fusion_inputs

try:
    from scripts.offline_guard import no_network
    from scripts.score_corpus import RunEntry, score_mix
except ModuleNotFoundError:
    from offline_guard import no_network
    from score_corpus import RunEntry, score_mix


@dataclass
class Scored:
    recall: tuple[int, int]
    precision: tuple[int, int]
    likely: tuple[int, int]
    named: dict[int, list[str]]  # truth index -> episode ids naming it
    names: dict[int, str]
    spans: dict[str, tuple[int, int]]  # episode id -> best span
    truth_spans: dict[int, tuple[int, int]]
    #: listed episode id -> (label, tier, truth index or None, flags)
    listed: dict[str, tuple[str, str, int | None, list[str]]]


def _score(entry: RunEntry, episodes: Any, identities: Any, scratch: Path, label: str) -> Scored:
    folder = scratch / label / entry.mix_id
    atomic_write_json(folder / "episodes.json", episodes)
    atomic_write_json(folder / "identities.json", identities)
    scored = score_mix(
        entry.model_copy(
            update={
                "episodes": folder / "episodes.json",
                "identities": folder / "identities.json",
            }
        ),
        recipe="free",
        artefact_dir=scratch / f"score-{label}",
        match="work",
    )
    document = json.loads(read_text(scored.work_match_path))
    flags = {item.id: list(item.flags) for item in episodes.episodes}
    counts = scored.work_match.counts
    truth = scored.truth
    first_index: dict[tuple[str, str], int] = {}
    for row in document["truth"]:
        first_index.setdefault((row["artist"], row["title"]), row["index"])
    named: dict[int, list[str]] = {}
    for row in document["truth"]:
        if row["named_by"]:
            key = first_index[(row["artist"], row["title"])]
            named.setdefault(key, []).extend(row["named_by"])
    return Scored(
        recall=(counts.truth_matched, counts.truth),
        precision=(counts.rows_correct, counts.rows),
        likely=(counts.likely_correct, counts.likely),
        named=named,
        names={row["index"]: f"{row['artist']} - {row['title']}" for row in document["truth"]},
        spans={item.id: (item.best_start_ms, item.best_end_ms) for item in episodes.episodes},
        truth_spans={
            index: (episode.start_ms_range[0], episode.end_ms_range[1])
            for index, episode in enumerate(truth.episodes)
        },
        listed={
            row["episode_id"]: (
                f"{row['artist']} - {row['title']}",
                row["tier"],
                row["truth_index"],
                flags.get(row["episode_id"], []),
            )
            for row in document["predictions"]
        },
    )


def _pct(pair: tuple[int, int]) -> str:
    numerator, denominator = pair
    if not denominator:
        return "n/a"
    return f"{numerator}/{denominator} ({100 * numerator / denominator:.1f}%)"


def _timing(scored: Scored, index: int, tolerance_ms: int) -> str:
    lo, hi = scored.truth_spans[index]
    verdicts = []
    for episode_id in scored.named.get(index, []):
        start, end = scored.spans[episode_id]
        inside = start <= hi + tolerance_ms and end >= lo - tolerance_ms
        verdicts.append(
            f"{'IN' if inside else 'OUT'} {start // 1000}-{end // 1000}s "
            f"vs truth {lo // 1000}-{hi // 1000}s"
        )
    return "; ".join(verdicts) or "unnamed"


def _windows(media_dir: Path) -> tuple[WindowRecord, ...]:
    path = media_dir / "windows" / "windows.gen0.jsonl"
    return tuple(
        WindowRecord.model_validate_json(line)
        for line in read_text(path).splitlines()
        if line.strip()
    )


def measure(
    *,
    work_root: Path,
    paid_root: Path,
    truths: list[tuple[str, Path]],
    config: AppConfig,
    density: int,
    unit_usd_e6: int,
    tolerance_ms: int,
    merge: bool,
    no_hints: bool = False,
) -> str:
    lines: list[str] = [
        "Offline measurement: stored Free evidence + CACHED paid answers; nothing sent.",
        f"Fusion merge rule: {'additive (free rows kept)' if merge else 'plain one-pass fusion'}.",
        "Tracklist/crowd hints: "
        + ("IGNORED (audio evidence only)." if no_hints else "the stored run's own hints."),
        "",
        "Mix | Policy | Work recall | Work precision | Likely precision | Paid clips | Cached | "
        "Would cost",
        "--- | --- | --- | --- | --- | --- | --- | ---",
    ]
    details: list[str] = []
    pooled: dict[str, list[int]] = {}
    with no_network() as refused, tempfile.TemporaryDirectory(prefix="idea-additive-") as temporary:
        scratch = Path(temporary)
        for mix_id, truth_path in truths:
            truth = GroundTruthRecord.model_validate_json(read_text(truth_path))
            media_key = truth.source.media_key
            matches = sorted(work_root.glob(f"*/{media_key}/ingest/source.json"))
            paid_matches = sorted(paid_root.glob(f"*/{media_key}/ingest/source.json"))
            if not matches or not paid_matches:
                lines.append(f"{mix_id}: SKIPPED: stored Free result or paid cache not found")
                continue
            media_dir = matches[0].parents[1]
            paid_dir = paid_matches[0].parents[1]
            snapshot = load_run_snapshot(media_dir)
            metadata = snapshot.manifest or legacy_result_metadata(media_dir) or {}
            stored_recipe = metadata.get("achieved") or (
                (metadata.get("compatibility") or {}).get("recipe_name")
            )
            if stored_recipe != "free":
                lines.append(f"{mix_id}: SKIPPED: stored result is not Free ({stored_recipe})")
                continue
            inputs = load_fusion_inputs(media_dir, _source_fuse_dir(snapshot, media_dir))
            if no_hints:
                from dataclasses import replace

                inputs = replace(inputs, hints=())
            windows = _windows(paid_dir)
            entry = RunEntry(
                mix_id=mix_id, truth=truth_path, episodes=truth_path, media_key=media_key
            )
            free = fuse_offline(
                media_key=media_key,
                duration_ms=inputs.duration_ms,
                observations=inputs.observations,
                windows=windows,
                hints=inputs.hints,
                config=config,
            )
            stored_free = _score(entry, snapshot.episodes, snapshot.identities, scratch, "stored")
            free_score = _score(entry, free.episodes, free.identities, scratch, "free")
            rows = [("Free (stored)", stored_free, 0, 0, 0), ("Free (today)", free_score, 0, 0, 0)]
            details.append(
                f"{mix_id}: today's fusion of the SAME stored Free evidence vs the stored "
                "(older-fusion) Free result:"
            )
            for index in sorted(set(free_score.named) - set(stored_free.named)):
                naming = free_score.named[index]
                how = sorted({flag for item in naming for flag in free_score.listed[item][3]})
                details.append(
                    f"  + {free_score.names[index]} [{_timing(free_score, index, tolerance_ms)}]"
                    f" flags={','.join(how) or 'none'}"
                )
            for index in sorted(set(stored_free.named) - set(free_score.named)):
                details.append(f"  - {stored_free.names[index]}")
            for policy in POLICIES:
                targets = policy_targets(policy, free.episodes.episodes, inputs.duration_ms)
                chosen = paid_windows(windows, targets, density=density)
                paid = paid_observations_from_cache(
                    media_key=media_key, media_dir=paid_dir, windows=chosen
                )
                combined = fuse_offline(
                    media_key=media_key,
                    duration_ms=inputs.duration_ms,
                    observations=(*inputs.observations, *paid.observations),
                    windows=windows,
                    hints=inputs.hints,
                    config=config,
                )
                fused = combined
                if merge:
                    fused, report = additive_merge(
                        free=free,
                        combined=combined,
                        paid_observation_ids=[item.id for item in paid.observations],
                        hint_ids=[hint.id for hint in inputs.hints],
                        min_track_ms=config.present_min_track_ms,
                    )
                    details.append(
                        f"{mix_id} / {policy}: merge added {report.added} row(s), paid confirmed "
                        f"{report.confirmed}, {report.kept_free} paid row(s) dropped to keep "
                        "free rows."
                    )
                scored = _score(entry, fused.episodes, fused.identities, scratch, policy)
                rows.append((policy, scored, len(chosen), paid.cached, paid.missing))
                gained = sorted(set(scored.named) - set(free_score.named))
                lost = sorted(set(free_score.named) - set(scored.named))
                details.append(
                    f"{mix_id} / {policy}: {len(chosen)} paid clips over "
                    f"{sum(e - s for s, e in targets) // 1000}s of target spans "
                    f"({len(targets)} spans); {paid.matches} paid matches."
                )
                for index in gained:
                    details.append(
                        f"  + gained: {scored.names[index]} "
                        f"[{_timing(scored, index, tolerance_ms)}]"
                    )
                for index in lost:
                    details.append(
                        f"  - LOST: {free_score.names[index]} "
                        f"[free had {_timing(free_score, index, tolerance_ms)}]"
                    )
                if not gained and not lost:
                    details.append("  (no change in named truth tracks)")
                for episode_id, (label, tier, index, _flags) in scored.listed.items():
                    if episode_id in free_score.listed:
                        continue
                    verdict = "correct" if index is not None else "not in truth"
                    details.append(f"  listed row added: {label} ({tier}; {verdict})")
            for label, scored, clips, cached, missing in rows:
                lines.append(
                    f"{mix_id} | {label} | {_pct(scored.recall)} | {_pct(scored.precision)} | "
                    f"{_pct(scored.likely)} | {clips} | {cached} | {missing}"
                )
                bucket = pooled.setdefault(label, [0] * 9)
                for position, value in enumerate(
                    (*scored.recall, *scored.precision, *scored.likely, clips, cached, missing)
                ):
                    bucket[position] += value
    for label, bucket in pooled.items():
        lines.append(
            f"Pooled | {label} | {_pct((bucket[0], bucket[1]))} | {_pct((bucket[2], bucket[3]))} | "
            f"{_pct((bucket[4], bucket[5]))} | {bucket[6]} | {bucket[7]} | {bucket[8]}"
        )
    lines.append("")
    for label, bucket in pooled.items():
        if bucket[6]:
            lines.append(
                f"{label}: {bucket[6]} paid clips = ${bucket[6] * unit_usd_e6 / 1e6:.2f} at list "
                f"price if none were cached; new requests this measurement: 0 "
                f"({bucket[8]} window(s) had no cached answer and were counted, not sent)."
            )
    lines.append(f"Network attempts blocked by the guard: {len(refused)}.")
    lines.append("")
    lines.extend(details)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--paid-root", type=Path, required=True)
    parser.add_argument(
        "--truth",
        action="append",
        required=True,
        help="mix_id=path to the mix's truth record; repeatable",
    )
    parser.add_argument("--config", type=Path, default=Path("idea.toml"))
    parser.add_argument("--density", type=int, choices=(1, 2), default=1)
    parser.add_argument("--tolerance-s", type=int, default=30)
    parser.add_argument(
        "--plain-fusion",
        action="store_true",
        help="Fuse free+paid in one plain pass instead of the additive merge (the old behaviour).",
    )
    parser.add_argument(
        "--no-hints",
        action="store_true",
        help="Fuse the audio evidence only: what paid adds to a mix with no tracklist.",
    )
    args = parser.parse_args(argv)
    config = AppConfig.load(args.config) if args.config.is_file() else AppConfig()
    truths = []
    for item in args.truth:
        mix_id, _, path = item.partition("=")
        truths.append((mix_id, Path(path)))
    print(
        measure(
            work_root=args.work_root,
            paid_root=args.paid_root,
            truths=truths,
            config=config,
            density=args.density,
            unit_usd_e6=config.audd_usd_e6_per_request,
            tolerance_ms=args.tolerance_s * 1000,
            merge=not args.plain_fusion,
            no_hints=args.no_hints,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

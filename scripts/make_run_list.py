"""Build a ``score_corpus.py`` run list: every truth set, its cached mix, its CURRENT result.

Usage::

    uv run python scripts/make_run_list.py --corpus data/corpus/release-1 --work-root work \\
        --out C:/somewhere/runs-free.json [--recipe free]

Why this exists: a Free-versus-Deep comparison once scored each mix's ``fuse/episodes.json`` —
which offline re-fusion never rewrites, so it was still the ``fusion:1`` result of weeks before —
against a ``fusion:4`` run, and credited the paid engine with the free side's own improvements.
This script never points at that working file when a published result exists:

* **Which mix.** Each truth set (``<corpus>/<set>/ground_truth.json``) is matched to its media by
  the media key the truth carries (``work/<source_key>/<media_key>/``); failing that, by EXACT
  decoded duration (``decode/pcm.json``), and only when exactly one cached mix has it.  Anything
  else is an error that names the set, never a guess.
* **Which result.** The mix's CURRENT published result — the bundle ``present/current`` names (or
  the newest complete one, exactly as the library and page resolve it) and that bundle's frozen
  fusion run (``fuse/runs/<run>/episodes.json`` + ``presentation-identities.json``).  Only a
  pre-bundle mix, which has nothing else, falls back to its flat ``fuse/episodes.json``.
* **Which rules.** Every entry records the ``fusion_version`` its result was decided by (the
  bundle manifest's, or the legacy journal's), and ``score_corpus.py`` refuses to pool runs of two
  different versions — re-fuse first.

Everything is read-only: the work root and the corpus are only read, and the run list may not be
written inside either.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from id_detector.benchmark.scorer import load_truth_files
from id_detector.contracts import PcmRecord
from id_detector.io import atomic_write_json, read_text
from id_detector.present.bundles import read_bundle_manifest, result_dir
from id_detector.present.index import media_map_read_only
from id_detector.truth import refuse_generated_output

try:
    from scripts.score_corpus import DEFAULT_MIN_TRACK_MS, _fusion_of, run_fusion_version
except ModuleNotFoundError:  # run as a plain script from scripts/
    from score_corpus import DEFAULT_MIN_TRACK_MS, _fusion_of, run_fusion_version


@dataclass(frozen=True)
class Resolved:
    """One truth set and the current result of its mix."""

    mix_id: str
    truth: Path
    media_dir: Path
    media_key: str
    matched_by: str  # "media key" or "duration"
    episodes: Path
    identities: Path | None
    kind: str  # "bundle" or "pre-bundle"
    fusion_version: int | None


def _duration(media_dir: Path) -> int | None:
    try:
        record = PcmRecord.model_validate_json(read_text(media_dir / "decode/pcm.json"))
        return record.pcm.duration_ms
    except (OSError, ValueError):
        return None


def current_result(media_dir: Path) -> tuple[Path, Path | None, str, int | None]:
    """``(episodes, identities, kind, fusion_version)`` of the mix's CURRENT published result."""

    directory = result_dir(media_dir)
    manifest = read_bundle_manifest(directory)
    if manifest is not None:
        fuse_run = (media_dir / manifest["fuse_run"]).resolve()
        if not fuse_run.is_relative_to((media_dir / "fuse" / "runs").resolve()):
            raise ValueError(f"{media_dir.name[:12]}: its bundle names an unsafe fusion run")
        version = _fusion_of(manifest)
        if version is None:
            version = run_fusion_version(fuse_run / "episodes.json")
        return (
            fuse_run / "episodes.json",
            fuse_run / "presentation-identities.json",
            "bundle",
            version,
        )
    episodes = media_dir / "fuse" / "episodes.json"
    if not episodes.is_file():
        raise ValueError(f"{media_dir.name[:12]}: no published result to score")
    return episodes, None, "pre-bundle", run_fusion_version(episodes)


def resolve(corpus: Path, work_root: Path) -> list[Resolved]:
    """Every truth set of ``corpus``, matched to its mix under ``work_root``, and that mix's
    current result."""

    media, by_key = media_map_read_only(work_root)
    resolved: list[Resolved] = []
    # Through the corpus gateway (read-only): the corpus's own vetted truth records, never a walk.
    for loaded in sorted(load_truth_files(corpus), key=lambda item: item.path):
        truth, truth_path = loaded.record, loaded.path
        mix_id = truth_path.parent.name
        media_dir = by_key.get(truth.source.media_key)
        matched_by = "media key"
        if media_dir is None:
            same = [path for path in media if _duration(path) == truth.source.duration_ms]
            if len(same) != 1:
                raise ValueError(
                    f"{mix_id}: no cached mix has media key {truth.source.media_key[:12]}…, and "
                    f"{len(same)} have its exact decoded duration {truth.source.duration_ms} ms; "
                    "cannot tell which mix it is"
                )
            media_dir, matched_by = same[0], "duration"
        episodes, identities, kind, version = current_result(media_dir)
        source = json.loads(read_text(media_dir / "ingest/source.json"))
        resolved.append(
            Resolved(
                mix_id=mix_id,
                truth=truth_path.resolve(),
                media_dir=media_dir,
                media_key=str(source.get("media_key") or media_dir.name),
                matched_by=matched_by,
                episodes=episodes.resolve(),
                identities=None if identities is None else identities.resolve(),
                kind=kind,
                fusion_version=version,
            )
        )
    if not resolved:
        raise ValueError(f"no truth set under {corpus}")
    return resolved


def run_list(resolved: list[Resolved], recipe: str) -> dict[str, object]:
    runs = []
    for item in resolved:
        entry: dict[str, object] = {
            "mix_id": item.mix_id,
            "truth": item.truth.as_posix(),
            "episodes": item.episodes.as_posix(),
            "media_key": item.media_key,
            "min_track_ms": DEFAULT_MIN_TRACK_MS,
        }
        if item.identities is not None:
            entry["identities"] = item.identities.as_posix()
        if item.fusion_version is not None:
            entry["fusion_version"] = item.fusion_version
        runs.append(entry)
    return {"recipe": recipe, "runs": runs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--corpus", type=Path, required=True, help="e.g. data/corpus/release-1")
    parser.add_argument("--work-root", type=Path, required=True, help="the cached mixes (work/)")
    parser.add_argument("--out", type=Path, required=True, help="the run list to write")
    parser.add_argument("--recipe", choices=("free", "deep"), default="free")
    args = parser.parse_args(argv)
    try:
        refuse_generated_output(args.out, work_root=args.work_root)
        resolved = resolve(args.corpus, args.work_root)
        document = run_list(resolved, args.recipe)
        refuse_generated_output(args.out, work_root=args.work_root)
        atomic_write_json(args.out, document)
    except (OSError, ValueError) as exc:
        print(f"make_run_list: {exc}", file=sys.stderr)
        return 1
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    for item in resolved:
        version = "unknown" if item.fusion_version is None else f"fusion:{item.fusion_version}"
        print(
            f"{item.mix_id}: media {item.media_key[:12]} (by {item.matched_by}); "
            f"{item.kind} result, {version}; {item.episodes}"
        )
    versions = {item.fusion_version for item in resolved}
    if len(versions) > 1 or None in versions:
        print(
            "WARNING — these results were NOT all decided by one known fusion version; "
            "score_corpus.py will refuse to pool them. Open the library (start-up re-fuses stored "
            "Free results offline) or re-fuse with scripts/measure_refusion.py first."
        )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

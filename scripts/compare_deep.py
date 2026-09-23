"""Budgeted, resumable Free/Deep experiment over a Free score_corpus run list.

Default: read-only dry run. --spend requires --scan-root outside the source work tree
and repository. Cached media are copied there; originals and truth are never mutated.
The isolated scan root is the experiment identity: keep it to resume. Jobs, attempts,
reservations and settlements use the existing SQLite worker, with its usual fences.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from id_detector.contracts import GroundTruthRecord
from id_detector.cost import cached_mix, estimate
from id_detector.io import native_path, read_text
from id_detector.jobs import ProcessLock
from id_detector.money import ceil_e2
from id_detector.present.bundles import load_run_snapshot
from id_detector.profiles import effective_app_config, load_profile
from id_detector.providers.base import AppConfig
from id_detector.recipes import get_recipe
from id_detector.service import LocalPath, PipelineOptions, PlatformUrl
from idea_web.database import Database
from idea_web.jobs.worker import JobQueue, Worker

try:
    from scripts.score_corpus import RunList, load_run_list, score_run_list
except ModuleNotFoundError:
    from score_corpus import RunList, load_run_list, score_run_list

ROOT = Path(__file__).resolve().parents[1]


def dollars(value: str) -> int:
    amount = Decimal(value)
    if not amount.is_finite() or amount < 0 or amount * 100 != int(amount * 100):
        raise argparse.ArgumentTypeError(
            "budget must be nonnegative dollars with at most two decimals"
        )
    return int(amount * 1_000_000)


def comparison(free: RunList, deep: RunList) -> str:
    """The existing scorer owns matching, pooling, and the presentation floor."""
    with tempfile.TemporaryDirectory(prefix="idea-compare-score-") as temporary:
        base = Path(temporary)
        docs = [
            score_run_list(
                runs, run_list_dir=base, artefact_dir=base / label, out_dir=base, match="work"
            )
            for label, runs in (("free", free), ("deep", deep))
        ]
        lines = [
            "Development comparison; work-only matching, not certification.",
            "Mix | Recipe | Work recall | Work precision | Likely precision",
            "--- | --- | --- | --- | ---",
        ]
        for index, mix in enumerate(docs[0]["mixes"]):
            for label, doc in zip(("Free", "Deep"), docs, strict=True):
                row = doc["mixes"][index]
                if row["mix_id"] != mix["mix_id"]:
                    raise ValueError("comparison mixes must have the same order")
                lines.append(_score_line(row["mix_id"], label, row["work_only"]))
        for label, doc in zip(("Free", "Deep"), docs, strict=True):
            lines.append(_score_line("Pooled", label, doc["work_only"]))
        lines.append("Deep found that Free missed (truth-matched names):")
        for before, after in zip(docs[0]["mixes"], docs[1]["mixes"], strict=True):
            first = json.loads(read_text(base / before["work_match"]))
            second = json.loads(read_text(base / after["work_match"]))
            added = [
                f"{row['artist']} - {row['title']}"
                for old, row in zip(first["truth"], second["truth"], strict=True)
                if row["named_by"] and not old["named_by"]
            ]
            lines.append(f"{after['mix_id']}: " + ("; ".join(added) or "none"))
        return "\n".join(lines)


def _score_line(mix: str, recipe: str, values: dict) -> str:
    numbers = [
        values[name] for name in ("work_recall_e4", "work_precision_e4", "likely_precision_e4")
    ]
    return f"{mix} | {recipe} | " + " | ".join(
        "n/a" if value is None else f"{value / 100:.1f}%" for value in numbers
    )


def _existing(database: Path) -> tuple[dict[str, str], dict[str, int]]:
    if not database.exists():
        return {}, {}
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        jobs = dict(connection.execute("SELECT id, state FROM jobs"))
        # Existing original reservations remain the conservative liability on resume, even
        # when today's price is lower. The worker never reprices them.
        reserved = dict(connection.execute("SELECT job_id, usd_e6_reserved FROM run_reservations"))
        return jobs, reserved


def execute(args, *, options: PipelineOptions | None = None, worker_type=Worker) -> int:
    source_root = args.work_root.resolve()
    scan_root = args.scan_root.resolve()
    if (
        scan_root.is_relative_to(source_root)
        or source_root.is_relative_to(scan_root)
        or scan_root.is_relative_to(ROOT)
    ):
        raise ValueError("--scan-root must be outside the repository and separate from --work-root")
    config = AppConfig.load(args.config)
    if config.default_profile:
        config = effective_app_config(config, load_profile(ROOT, config.default_profile))
    config = replace(config, deep_primary_density=args.density)
    recipe = get_recipe("deep", primary_density=args.density)
    baseline = load_run_list(args.run_list)
    if baseline.recipe != "free":
        raise ValueError("run list must name the stored Free baseline")
    entries = [
        item.resolved(args.run_list.resolve().parent)
        for item in baseline.runs
        if not args.mix or item.mix_id in args.mix
    ]
    if not entries or (args.mix and set(args.mix) != {item.mix_id for item in entries}):
        raise ValueError("unknown or empty mix selection")
    database_path = scan_root / ".idea/app.db"
    jobs, prior = _existing(database_path)
    plans = []
    media_seen = set()
    for entry in entries:
        truth = GroundTruthRecord.model_validate_json(read_text(entry.truth))
        cached = cached_mix(source_root, truth.source.media_key)
        if cached is None or cached.duration_ms is None:
            raise ValueError(f"{entry.mix_id}: cached length unknown; no scan started")
        if cached.source.media_key in media_seen:
            raise ValueError("the same media cannot be charged twice in one experiment")
        media_seen.add(cached.source.media_key)
        preview = estimate(cached.duration_ms, config)
        identifier = (
            "compare-"
            + sha256((cached.source.media_key + recipe.recipe_id).encode()).hexdigest()[:32]
        )
        print(
            f"{entry.mix_id}: {preview.clips} clips, estimate ${preview.estimate_e6 / 1e6:.2f}; "
            f"reservation ${ceil_e2(preview.reservation_e6) / 100:.2f}; "
            f"{jobs.get(identifier, 'not started')}"
        )
        plans.append((entry, cached, preview, identifier))
    if set(jobs) - {plan[3] for plan in plans}:
        raise ValueError(
            "scan root belongs to a different mix selection or recipe; "
            "retain its original selection to resume"
        )
    total = sum(plan[2].estimate_e6 for plan in plans)
    # Budget the full original plan including completed work; retries never acquire a new budget.
    bound = sum(
        max(prior.get(identifier, 0), ceil_e2(preview.reservation_e6) * 10_000)
        for _, _, preview, identifier in plans
    )
    print(
        f"Total estimate: ${total / 1e6:.2f}; conservative budget required: ${bound / 1e6:.2f}; "
        f"hard budget: ${args.budget / 1e6:.2f}."
    )
    if total > args.budget or bound > args.budget:
        print(
            "REFUSED: estimate or reservation headroom exceeds the total budget. Nothing started."
        )
        return 4
    if any(ceil_e2(p.reservation_e6) > p.cap_e2 for _, _, p, _ in plans):
        print("REFUSED: a mix exceeds its recipe/configured cap. Nothing started.")
        return 4
    # Prove that the stored baseline can be scored before paying for its counterpart.
    # All scorer artefacts are temporary; the baseline and hand-written truth stay read-only.
    with tempfile.TemporaryDirectory(prefix="idea-baseline-check-") as temporary:
        scratch = Path(temporary)
        score_run_list(
            RunList(recipe="free", runs=entries),
            run_list_dir=args.run_list.parent,
            artefact_dir=scratch,
            out_dir=scratch,
            match="work",
        )
    if not args.spend:
        print("DRY RUN: nothing copied, queued, reserved or spent. Use --spend deliberately.")
        return 0
    database = Database(database_path)
    database.migrate()
    supervisor = ProcessLock(database.supervisor_lock_path)
    supervisor.acquire()
    completed_free, completed_deep = [], []
    try:
        # Re-read after acquiring the single worker lock. A process that ran between the
        # preview and this claim may have reserved at a different price.
        jobs, prior = _existing(database_path)
        bound = sum(
            max(prior.get(identifier, 0), ceil_e2(preview.reservation_e6) * 10_000)
            for _, _, preview, identifier in plans
        )
        if set(jobs) - {plan[3] for plan in plans} or bound > args.budget:
            print("REFUSED: durable reservations or selection changed; budget recheck failed.")
            return 4
        queue = JobQueue(database, local_mode=True)
        for entry, cached, preview, identifier in plans:
            destination = scan_root / cached.directory.relative_to(source_root)
            if identifier not in jobs:
                # Copy into a temporary sibling then rename: a crash must not leave a half copy
                # that a subsequent invocation mistakes for a usable cached mix.
                if not destination.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(
                        dir=native_path(destination.parent)
                    ) as temporary:
                        staged = Path(temporary) / "media"
                        shutil.copytree(native_path(cached.directory), native_path(staged))
                        staged.rename(native_path(destination))
                url = cached.source.input_url
                target = LocalPath(Path(url)) if Path(url).is_file() else PlatformUrl(url)
                queue.enqueue(target, recipe, job_id=identifier)
            state = queue.get(identifier).state
            if state in {"intake", "analysis", "waiting"}:
                # Every mix has a fixed maximum allocation; their sum was checked above.
                capped = replace(
                    config, max_usd_e2=min(preview.cap_e2, ceil_e2(preview.reservation_e6))
                )
                settings = replace(
                    options or PipelineOptions(project_root=ROOT),
                    app_config=capped,
                    enabled_engines=("audd",),
                    max_generations=0,
                )
                worker_type(database, scan_root, local_mode=True, options=settings).run_once()
            job = queue.get(identifier)
            print(f"{entry.mix_id}: {job.state}")
            with database.read() as connection:
                from idea_web.jobs.worker import fold_run_money

                spend = sum(
                    fold_run_money(connection, row[0]).usd_e6_spent
                    for row in connection.execute("SELECT run_id FROM analysis_runs").fetchall()
                )
            print(f"Experiment spent so far (including ambiguous requests): ${spend / 1e6:.6f}.")
            # The sum of per-mix durable reservations is already bounded by the budget, so this can
            # only fire if that invariant broke. Stop before starting another mix if it ever does.
            if spend > args.budget:
                print(
                    f"STOPPED: recorded spend ${spend / 1e6:.6f} exceeds the hard budget "
                    f"${args.budget / 1e6:.2f}. No further mix will be started."
                )
                break
            if job.state in {"intake", "analysis", "waiting"}:
                print(
                    "Interrupted or waiting: resume with the same scan root and selection; "
                    "no new job will be created."
                )
                break
            try:
                if job.result_bundle_id is None:
                    raise ValueError("job has no result bundle")
                snapshot = load_run_snapshot(
                    destination, directory=destination / "present/bundles" / job.result_bundle_id
                )
                if snapshot.metadata.get("achieved") != "deep":
                    raise ValueError("no completed Deep result")
                fuse = destination / snapshot.manifest["fuse_run"]
                completed_free.append(entry)
                completed_deep.append(
                    entry.model_copy(
                        update={
                            "episodes": fuse / "episodes.json",
                            "identities": fuse / "presentation-identities.json",
                        }
                    )
                )
            except (OSError, ValueError, TypeError, KeyError):
                print(
                    f"{entry.mix_id}: no usable Deep result; not included in paired scores; "
                    "never automatically re-paid."
                )
        if completed_deep:
            print(
                comparison(
                    RunList(recipe="free", runs=completed_free),
                    RunList(recipe="deep", runs=completed_deep),
                )
            )
        print(
            "Not compared: "
            + (", ".join(e.mix_id for e in entries if e not in completed_free) or "none")
        )
    finally:
        supervisor.release()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-list", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--scan-root", type=Path, required=True)
    parser.add_argument("--mix", action="append", default=[])
    parser.add_argument("--budget", type=dollars, required=True)
    parser.add_argument("--density", type=int, choices=(1, 2), default=1)
    parser.add_argument("--config", type=Path, default=Path("idea.toml"))
    parser.add_argument("--spend", action="store_true")
    parser.add_argument("--fake-providers", choices=("audd,shazam",))
    args = parser.parse_args(argv)
    options = None
    try:
        if args.fake_providers:
            if os.environ.get("IDEA_TEST_MODE") != "1":
                raise ValueError("fake providers require IDEA_TEST_MODE=1")
            import runpy

            fakes = runpy.run_path(str(ROOT / "tests/fakes/providers.py"))
            audd, shazam = fakes["load_fake_providers"](
                Path(os.environ["IDEA_FAKE_SCRIPT"]), ("audd", "shazam")
            )
            options = PipelineOptions(
                project_root=ROOT,
                paid_scan_adapters={"audd": audd},
                shazam_http_client=shazam,
                paid_sleep=fakes["no_backoff"],
                no_hints=True,
            )
        elif os.environ.get("IDEA_TEST_MODE") == "1" and args.spend:
            raise ValueError("test mode spending requires --fake-providers audd,shazam")
        elif args.spend:
            from id_detector.cli import _load_dotenv

            _load_dotenv(ROOT)
        return execute(args, options=options)
    except (OSError, ValueError, KeyError) as exc:
        print(f"REFUSED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

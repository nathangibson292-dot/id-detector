"""Budgeted, resumable Free/Deep experiment over a Free score_corpus run list.

Default: read-only dry run. --spend requires --scan-root outside the source work tree
and repository. Cached media are copied there; originals and truth are never mutated.
The isolated scan root is the experiment identity: keep it to resume. Jobs, attempts,
reservations and settlements use the existing SQLite worker, with its usual fences.

Deep is free-first and additive: each mix's stored Free result answers the free pass, and
the paid engine checks only the gaps that result leaves. ``--paid-answers-from`` copies the
paid answers an earlier experiment already bought into the new scan root (the source is only
read), so re-running costs nothing for every clip already answered. ``--offline`` runs with a
guard that refuses every network attempt, an empty AUDD_API_TOKEN and the free engine off: a
clip with no stored answer is counted as "would cost one request" and never sent.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from id_detector.contracts import GroundTruthRecord
from id_detector.cost import cached_mix, estimate
from id_detector.io import atomic_write_json, native_path, read_text
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
    from scripts.offline_guard import no_network
    from scripts.score_corpus import RunList, load_run_list, score_run_list
except ModuleNotFoundError:
    from offline_guard import no_network
    from score_corpus import RunList, load_run_list, score_run_list

ROOT = Path(__file__).resolve().parents[1]
#: The paid clip cache inside a media directory (content-addressed; see ``paid_clip``).
PAID_CACHE = Path("recognise") / "invocations" / "live-audd-clip-v1" / "raw"
#: Seconds between attempts to move a freshly copied mix into place. On Windows a scanner (most
#: likely antivirus) briefly locks a fresh ~500 MB copy and the rename fails with WinError 5.
STAGE_BACKOFF_SECONDS = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0)


class StagingFailed(RuntimeError):
    """A mix could not be moved into the scan root; nothing was queued, reserved or spent."""


def dollars(value: str) -> int:
    amount = Decimal(value)
    if not amount.is_finite() or amount < 0 or amount * 100 != int(amount * 100):
        raise argparse.ArgumentTypeError(
            "budget must be nonnegative dollars with at most two decimals"
        )
    return int(amount * 1_000_000)


def stage_media(
    source: Path,
    destination: Path,
    *,
    paid_answers: Path | None = None,
    backoff: tuple[float, ...] = STAGE_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    rename: Callable[[str, str], None] = os.rename,
) -> int:
    """Copy one cached mix into the scan root through a temporary sibling, atomically.

    The copy is renamed into place only when complete, retried with a short bounded backoff while
    the fresh copy is locked, and on persistent failure the temporary copy is removed and
    :class:`StagingFailed` says so: a crash or a lock never leaves a half copy that a later run
    mistakes for a usable cached mix.  ``paid_answers`` is another scan root's copy of the same
    mix: its stored paid answers are copied in (only read there).  Returns how many were.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(dir=native_path(destination.parent), prefix=".staging-"))
    copied = 0
    try:
        staged = temporary / "media"
        shutil.copytree(native_path(source), native_path(staged))
        if paid_answers is not None and (paid_answers / PAID_CACHE).is_dir():
            target = staged / PAID_CACHE
            target.mkdir(parents=True, exist_ok=True)
            for answer in sorted((paid_answers / PAID_CACHE).glob("*.json")):
                if not (target / answer.name).exists():
                    shutil.copy2(native_path(answer), native_path(target / answer.name))
                    copied += 1
        for attempt in range(len(backoff) + 1):
            try:
                rename(native_path(staged), native_path(destination))
                return copied
            except PermissionError as exc:
                if attempt == len(backoff):
                    raise StagingFailed(
                        f"could not move the copied mix into {destination} after "
                        f"{len(backoff) + 1} attempts ({exc}); the partial copy was removed and "
                        "nothing was queued, reserved or spent. Close anything holding the "
                        "folder open (a virus scan of the fresh copy is the usual cause) and run "
                        "the same command again."
                    ) from exc
                sleep(backoff[attempt])
    finally:
        shutil.rmtree(native_path(temporary), ignore_errors=True)
    return copied


def comparison(free: RunList, deep: RunList, today: RunList | None = None) -> str:
    """The existing scorer owns matching, pooling, and the presentation floor."""
    columns = [("Free (stored)", free), ("Deep", deep)]
    if today is not None:
        columns.insert(1, ("Free today", today))
    with tempfile.TemporaryDirectory(prefix="idea-compare-score-") as temporary:
        base = Path(temporary)
        docs = [
            score_run_list(
                runs, run_list_dir=base, artefact_dir=base / label, out_dir=base, match="work"
            )
            for label, runs in (
                ("".join(c if c.isalnum() else "-" for c in label).lower(), runs)
                for label, runs in columns
            )
        ]
        labels = [label for label, _ in columns]
        lines = [
            "Development comparison; work-only matching, not certification.",
            "Mix | Recipe | Work recall | Work precision | Likely precision",
            "--- | --- | --- | --- | ---",
        ]
        for index, mix in enumerate(docs[0]["mixes"]):
            for label, doc in zip(labels, docs, strict=True):
                row = doc["mixes"][index]
                if row["mix_id"] != mix["mix_id"]:
                    raise ValueError("comparison mixes must have the same order")
                lines.append(_score_line(row["mix_id"], label, row["work_only"]))
        for label, doc in zip(labels, docs, strict=True):
            lines.append(_score_line("Pooled", label, doc["work_only"]))

        def named(doc: dict, mix: dict) -> dict[tuple[str, str], bool]:
            match = json.loads(read_text(base / mix["work_match"]))
            return {(row["artist"], row["title"]): bool(row["named_by"]) for row in match["truth"]}

        lines.append("Deep found that Free missed (truth-matched names):")
        for before, after in zip(docs[0]["mixes"], docs[-1]["mixes"], strict=True):
            first, second = named(docs[0], before), named(docs[-1], after)
            added = [f"{a} - {t}" for (a, t), hit in second.items() if hit and not first[(a, t)]]
            lines.append(f"{after['mix_id']}: " + ("; ".join(added) or "none"))
        reference = docs[1] if today is not None else docs[0]
        lines.append(
            "Free-found tracks Deep lost (the additive invariant; must be none), against "
            + ("today's Free:" if today is not None else "Free:")
        )
        for before, after in zip(reference["mixes"], docs[-1]["mixes"], strict=True):
            first, second = named(reference, before), named(docs[-1], after)
            lost = [f"{a} - {t}" for (a, t), hit in first.items() if hit and not second[(a, t)]]
            lines.append(f"{after['mix_id']}: " + ("; ".join(lost) or "none"))
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


def _identifier(media_key: str, recipe_id: str) -> str:
    return "compare-" + sha256((media_key + recipe_id).encode()).hexdigest()[:32]


def _selection_refusal(jobs: dict[str, str], baseline: RunList, source_root: Path, recipe) -> str:
    """Name exactly what the scan root was started with, so the owner can extend it safely."""

    started = []
    known = set()
    for item in baseline.runs:
        try:
            truth = GroundTruthRecord.model_validate_json(read_text(item.truth))
        except (OSError, ValueError):
            continue
        identifier = _identifier(truth.source.media_key, recipe.recipe_id)
        if identifier in jobs:
            started.append(item.mix_id)
            known.add(identifier)
    other = len(set(jobs) - known)
    parts = []
    if started:
        parts.append(
            "it was started with "
            + ", ".join(started)
            + "; name those mixes too ("
            + " ".join(f"--mix {name}" for name in started)
            + ") to resume or extend it"
        )
    if other:
        parts.append(
            f"{other} of its job(s) belong to a different recipe or run list; use a new "
            "--scan-root for this recipe"
        )
    return (
        "scan root belongs to a different mix selection or recipe: "
        + ("; ".join(parts) or "it holds jobs this run list does not name")
        + ". It is refused so that no mix can be charged twice."
    )


def _today_free(
    entry,
    source_root: Path,
    media_key: str,
    config: AppConfig,
    scratch: Path,
    *,
    no_hints: bool = False,
):
    """Today's fusion of the stored Free evidence (what the Free recipe shows now), or None."""

    from id_detector.additive import fuse_offline, proven_free_results
    from id_detector.contracts import WindowRecord

    cached = cached_mix(source_root, media_key)
    if cached is None or cached.duration_ms is None:
        return None
    path = cached.directory / "windows" / "windows.gen0.jsonl"
    if not path.is_file():
        return None
    windows = tuple(
        WindowRecord.model_validate_json(line) for line in read_text(path).splitlines() if line
    )
    for observations, hints, _source in proven_free_results(cached.directory, windows):
        fused = fuse_offline(
            media_key=media_key,
            duration_ms=cached.duration_ms,
            observations=observations,
            windows=windows,
            hints=() if no_hints else hints,
            config=config,
        )
        folder = scratch / "free-today" / entry.mix_id
        atomic_write_json(folder / "episodes.json", fused.episodes)
        atomic_write_json(folder / "identities.json", fused.identities)
        return entry.model_copy(
            update={
                "episodes": folder / "episodes.json",
                "identities": folder / "identities.json",
                "media_key": media_key,
            }
        )
    return None


def _run_counts(media_dir: Path, run_id: str | None) -> dict[str, int]:
    """The Deep run's own journal counts (paid clips planned, cached, sent)."""

    if not run_id:
        return {}
    try:
        lines = read_text(media_dir / "invocations.jsonl").splitlines()
    except OSError:
        return {}
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("invocation_id") == run_id:
            return {key: int(value) for key, value in (entry.get("counts") or {}).items()}
    return {}


def execute(args, *, options: PipelineOptions | None = None, worker_type=Worker) -> int:
    source_root = args.work_root.resolve()
    scan_root = args.scan_root.resolve()
    offline = bool(getattr(args, "offline", False))
    paid_answers_root = getattr(args, "paid_answers_from", None)
    paid_answers_root = paid_answers_root.resolve() if paid_answers_root is not None else None
    if (
        scan_root.is_relative_to(source_root)
        or source_root.is_relative_to(scan_root)
        or scan_root.is_relative_to(ROOT)
    ):
        raise ValueError("--scan-root must be outside the repository and separate from --work-root")
    if paid_answers_root is not None and (
        scan_root.is_relative_to(paid_answers_root) or paid_answers_root.is_relative_to(scan_root)
    ):
        raise ValueError("--paid-answers-from must be a different folder from --scan-root")
    config = AppConfig.load(args.config)
    if config.default_profile:
        config = effective_app_config(config, load_profile(ROOT, config.default_profile))
    config = replace(config, deep_primary_density=args.density)
    recipe = get_recipe("deep", primary_density=args.density)
    baseline = load_run_list(args.run_list)
    if baseline.recipe != "free":
        raise ValueError("run list must name the stored Free baseline")
    resolved = RunList(
        recipe="free",
        runs=[item.resolved(args.run_list.resolve().parent) for item in baseline.runs],
    )
    entries = [item for item in resolved.runs if not args.mix or item.mix_id in args.mix]
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
        identifier = _identifier(cached.source.media_key, recipe.recipe_id)
        print(
            f"{entry.mix_id}: at most {preview.clips} clips (the whole mix; Deep pays only for "
            f"the free result's gaps), ceiling ${preview.estimate_e6 / 1e6:.2f}; "
            f"reservation ceiling ${ceil_e2(preview.reservation_e6) / 100:.2f}; "
            f"{jobs.get(identifier, 'not started')}"
        )
        plans.append((entry, cached, preview, identifier))
    if set(jobs) - {plan[3] for plan in plans}:
        raise ValueError(_selection_refusal(jobs, resolved, source_root, recipe))
    total = sum(plan[2].estimate_e6 for plan in plans)
    # Budget the full original plan including completed work; retries never acquire a new budget.
    bound = sum(
        max(prior.get(identifier, 0), ceil_e2(preview.reservation_e6) * 10_000)
        for _, _, preview, identifier in plans
    )
    print(
        f"Total ceiling: ${total / 1e6:.2f}; conservative budget required: ${bound / 1e6:.2f}; "
        f"hard budget: ${args.budget / 1e6:.2f}."
        + (" Offline: nothing can be sent, so nothing can be spent." if offline else "")
    )
    if not offline and (total > args.budget or bound > args.budget):
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
    if not args.spend and not offline:
        print("DRY RUN: nothing copied, queued, reserved or spent. Use --spend deliberately.")
        return 0
    guard = no_network() if offline else nullcontext([])
    with guard as blocked:
        code = _run(
            args,
            options=options,
            worker_type=worker_type,
            source_root=source_root,
            scan_root=scan_root,
            database_path=database_path,
            plans=plans,
            config=config,
            paid_answers_root=paid_answers_root,
        )
        if offline:
            attempts = len(blocked)
            print(
                f"Network attempts refused by the offline guard: {attempts}"
                + (" (none: no request left this machine)." if attempts == 0 else ".")
            )
    return code


def _run(
    args,
    *,
    options,
    worker_type,
    source_root: Path,
    scan_root: Path,
    database_path: Path,
    plans,
    config: AppConfig,
    paid_answers_root: Path | None,
) -> int:
    database = Database(database_path)
    database.migrate()
    supervisor = ProcessLock(database.supervisor_lock_path)
    supervisor.acquire()
    completed_free, completed_deep, completed_today = [], [], []
    offline = bool(getattr(args, "offline", False))
    # Offline, no tracklist or comment connector can be reached: both sides are compared on the
    # audio evidence alone rather than on hints one side could fetch and the other could not.
    no_hints = offline or bool(getattr(args, "no_hints", False))
    if no_hints:
        print(
            "Hints: none on either side (audio evidence only)"
            + (" -- offline, no connector can be reached." if offline else ".")
        )
    try:
        # Re-read after acquiring the single worker lock. A process that ran between the
        # preview and this claim may have reserved at a different price.
        jobs, prior = _existing(database_path)
        bound = sum(
            max(prior.get(identifier, 0), ceil_e2(preview.reservation_e6) * 10_000)
            for _, _, preview, identifier in plans
        )
        if set(jobs) - {plan[3] for plan in plans} or (not offline and bound > args.budget):
            print("REFUSED: durable reservations or selection changed; budget recheck failed.")
            return 4
        queue = JobQueue(database, local_mode=True)
        for entry, cached, preview, identifier in plans:
            destination = scan_root / cached.directory.relative_to(source_root)
            if identifier not in jobs:
                if not destination.exists():
                    answers = (
                        paid_answers_root / cached.directory.relative_to(source_root)
                        if paid_answers_root is not None
                        else None
                    )
                    try:
                        imported = stage_media(cached.directory, destination, paid_answers=answers)
                    except StagingFailed as exc:
                        print(f"{entry.mix_id}: STAGING FAILED: {exc}")
                        print("Stopped before this mix: nothing further was queued or spent.")
                        return 5
                    if answers is not None:
                        print(
                            f"{entry.mix_id}: {imported} stored paid answer(s) copied from "
                            f"{answers} (read only)."
                        )
                url = cached.source.input_url
                target = LocalPath(Path(url)) if Path(url).is_file() else PlatformUrl(url)
                queue.enqueue(
                    target, get_recipe("deep", primary_density=args.density), job_id=identifier
                )
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
                if no_hints:
                    settings = replace(settings, no_hints=True)
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
            if not offline and spend > args.budget:
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
                counts = _run_counts(destination, job.run_id)
                if counts:
                    print(
                        f"{entry.mix_id}: paid step {counts.get('paid_targets', 0)} clip(s) "
                        f"over {counts.get('paid_target_spans', 0)} gap(s); "
                        f"{counts.get('paid_cache_hits', 0)} answered from stored answers; "
                        f"{counts.get('paid_requests', 0)} new request(s) sent; "
                        f"{counts.get('paid_planned', 0) - counts.get('paid_requests', 0)} "
                        "without a stored answer counted as would-cost, not sent; "
                        f"free requests sent {counts.get('requests', 0)}."
                    )
                completed_free.append(entry)
                completed_deep.append(
                    entry.model_copy(
                        update={
                            "episodes": fuse / "episodes.json",
                            "identities": fuse / "presentation-identities.json",
                        }
                    )
                )
                completed_today.append((entry, cached))
            except (OSError, ValueError, TypeError, KeyError):
                print(
                    f"{entry.mix_id}: no usable Deep result; not included in paired scores; "
                    "never automatically re-paid."
                )
        if completed_deep:
            with tempfile.TemporaryDirectory(prefix="idea-free-today-") as temporary:
                today = [
                    _today_free(
                        entry,
                        source_root,
                        cached.source.media_key,
                        config,
                        Path(temporary),
                        no_hints=no_hints,
                    )
                    for entry, cached in completed_today
                ]
                print(
                    comparison(
                        RunList(recipe="free", runs=completed_free),
                        RunList(recipe="deep", runs=completed_deep),
                        RunList(recipe="free", runs=today) if all(today) else None,
                    )
                )
        print(
            "Not compared: "
            + (", ".join(p[0].mix_id for p in plans if p[0] not in completed_free) or "none")
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
    parser.add_argument("--budget", type=dollars, default=None)
    parser.add_argument("--density", type=int, choices=(1, 2), default=1)
    parser.add_argument("--config", type=Path, default=Path("idea.toml"))
    parser.add_argument("--spend", action="store_true")
    parser.add_argument(
        "--paid-answers-from",
        type=Path,
        default=None,
        help="An earlier scan root whose stored paid answers are copied in (only read there).",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help=(
            "Refuse every network attempt, empty AUDD_API_TOKEN, free engine off: runs from "
            "stored answers only and can spend nothing."
        ),
    )
    parser.add_argument(
        "--no-hints",
        action="store_true",
        help="Compare on audio evidence only (implied by --offline).",
    )
    parser.add_argument("--fake-providers", choices=("audd,shazam",))
    args = parser.parse_args(argv)
    if args.budget is None:
        if not args.offline:
            parser.error("--budget is required unless --offline")
        args.budget = 0
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
        elif args.spend and not args.offline:
            from id_detector.cli import _load_dotenv

            _load_dotenv(ROOT)
        return execute(args, options=options)
    except (OSError, ValueError, KeyError) as exc:
        print(f"REFUSED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

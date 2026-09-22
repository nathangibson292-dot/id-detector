"""A fusion bump against the SQLite money authority (bb57b70), through the REAL local worker.

A paid (Deep) result that is only a fusion version behind is never re-fused (its second-opinion
windows were chosen by the older fusion) and — the point of this file — is never silently paid
for again either: the job STOPS, with the reason in plain words, no reservation row, no dispatch
row of any engine, no provider request, and exactly one zero-money settlement row for the run.
That holds whether the recorded evidence is intact, missing or changed.
"""

from __future__ import annotations

import json
import shutil
import socket
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from id_detector.compat import AnalysisInputs
from id_detector.compat import RunRequest as CompatibilityRequest
from id_detector.io import native_path, read_text
from id_detector.present.bundles import read_bundle_manifest, result_dir
from id_detector.recipes import FREE_RECIPE
from id_detector.retention import collect
from id_detector.service import PlatformUrl
from idea_web.jobs.local import LocalJobs, LocalWorker, local_database
from idea_web.jobs.worker import Worker
from tests.fakes.providers import FakeAudD, FakeShazamHTTP
from tests.idea_web.local_runner_fakes import fake_pipeline_runner
from tests.idea_web.test_followup_review_fixes import _invocations
from tests.idea_web.test_followup_round2 import _env
from tests.idea_web.test_followup_round6 import _count, _entry_of, _paid_dispatches, _settlement
from tests.idea_web.test_worker import AUDIO, SCRIPT


def _stamp_as_fusion_2(bundle: Path) -> None:
    """What a bundle published before the bump says about itself.  The manifest seals the
    bundle's FILES (it does not hash itself), so this is the stored result an older build left."""

    path = Path(native_path(bundle / "manifest.json"))
    manifest = json.loads(read_text(path))
    version = manifest["compatibility"]["algorithm_version"]
    assert version.endswith("fusion:3")
    manifest["compatibility"]["algorithm_version"] = version.replace("fusion:3", "fusion:2")
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert read_bundle_manifest(bundle) is not None


def _run_one(
    tmp_path: Path, config: Path, audd: FakeAudD, shazam: FakeShazamHTTP | None = None
) -> None:
    LocalWorker(
        local_database(tmp_path),
        tmp_path,
        fake_pipeline_runner(tmp_path, config, audd=audd, shazam=shazam),
        flush_seconds=0.05,
    ).run_once()


def test_real_worker_re_fuses_a_stale_result_across_source_aliases_without_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _env(tmp_path, monkeypatch)
    jobs = LocalJobs(tmp_path)
    first_audd, first_shazam = FakeAudD(SCRIPT), FakeShazamHTTP(SCRIPT)
    first = jobs.submit(str(AUDIO), "free")
    _run_one(tmp_path, config, first_audd, first_shazam)
    assert jobs.get(first).status == "succeeded" and first_shazam.requests > 0
    (media,) = [path.parent for path in Path(native_path(tmp_path)).glob("*/*/present")]
    old_bundle = result_dir(media)
    _stamp_as_fusion_2(old_bundle)
    sibling = tmp_path / "same-bytes-different-source.wav"
    shutil.copy2(AUDIO, sibling)
    dispatches = _count(jobs.database, "run_dispatches")
    reservations = _count(jobs.database, "run_reservations")

    blocked_audd, blocked_shazam = FakeAudD(SCRIPT), FakeShazamHTTP(SCRIPT)
    second = jobs.submit(str(sibling), "free")
    _run_one(tmp_path, config, blocked_audd, blocked_shazam)

    assert jobs.get(second).status == "succeeded", jobs.get(second).error
    assert blocked_audd.calls == 0 and blocked_shazam.requests == 0
    assert _count(jobs.database, "run_dispatches") == dispatches
    assert _count(jobs.database, "run_reservations") == reservations
    published = result_dir(media)
    assert published != old_bundle
    assert read_bundle_manifest(published)["refusion"]["source_bundle"] == old_bundle.name


def _make_pre_bundle(media: Path) -> None:
    bundle = result_dir(media)
    for name in ("index.html", "tracklist.json", "source.json"):
        shutil.copy2(bundle / name, media / "present" / name)
    lines = [json.loads(line) for line in read_text(media / "invocations.jsonl").splitlines()]
    completed = next(line for line in reversed(lines) if line["status"] == "complete")
    completed["algorithm_version"] = completed["algorithm_version"].replace("fusion:3", "fusion:2")
    completed.update(analysis_key=None, compatibility=None, bundle_id=None, fuse_run=None)
    (media / "invocations.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    shutil.rmtree(media / "present/bundles")
    shutil.rmtree(media / "fuse/runs")
    (media / "present/current").unlink()
    assert result_dir(media) == media / "present"


@pytest.mark.parametrize(
    ("legacy", "evidence"),
    [
        (False, "intact"),
        (False, "missing"),
        (False, "mismatched"),
        (True, "intact"),
        (True, "missing"),
        (True, "mismatched"),
        (True, "retention-pruned"),
    ],
)
def test_a_stale_deep_result_stops_the_job_without_a_reservation_or_a_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool, evidence: str
) -> None:
    config = _env(tmp_path, monkeypatch)
    jobs = LocalJobs(tmp_path)
    paid = FakeAudD(SCRIPT)
    first = jobs.submit(str(AUDIO), "max_accuracy")
    if legacy and evidence == "intact":
        from id_detector.present import page as page_module

        monkeypatch.setattr(page_module, "PAGE_VERSION", 24)
    _run_one(tmp_path, config, paid)
    if legacy and evidence == "intact":
        monkeypatch.setattr(page_module, "PAGE_VERSION", 25)
    assert jobs.get(first).status == "succeeded", jobs.get(first).error
    first_run = jobs.queue.get(first).run_id
    assert paid.calls > 0 and _paid_dispatches(jobs.database, first_run) == paid.calls
    (media,) = [path.parent for path in Path(native_path(tmp_path)).glob("*/*/present")]
    old_bundle = result_dir(media)
    assert read_bundle_manifest(old_bundle)["achieved"] == "deep"
    _stamp_as_fusion_2(old_bundle)
    if legacy:
        _make_pre_bundle(media)
    observations = sorted((media / "recognise").rglob("observations.gen0.jsonl"))
    if evidence == "missing":
        observations[0].unlink()
    elif evidence == "mismatched":
        observations[0].write_bytes(observations[0].read_bytes() + b"\n")
    elif evidence == "retention-pruned":
        collected = collect(
            tmp_path, policy="local", apply=True, now=datetime(2100, 1, 1, tzinfo=UTC)
        )
        assert any(
            action.operation == "move" and action.path.name == "windows"
            for action in collected.actions
        )
        assert not (media / "windows").exists() and not (media / "decode/audio.pcm").exists()
    shown = result_dir(media)
    preserved = {
        path.relative_to(media).as_posix(): path.read_bytes()
        for path in media.rglob("*")
        if path.is_file()
    }
    dispatches = _count(jobs.database, "run_dispatches")
    reservations = _count(jobs.database, "run_reservations")
    if legacy and evidence == "intact":
        from idea_web.server import Upkeep

        report = Upkeep(tmp_path).run_all()
        assert len(report.skipped) == 1 and not report.updated and not report.refreshed
        assert result_dir(media) == shown

    again = FakeAudD(SCRIPT)
    second = jobs.submit(str(AUDIO), "max_accuracy")
    _run_one(tmp_path, config, again)
    job = jobs.get(second)
    run_id = jobs.queue.get(second).run_id
    # It stopped, and says why in words an owner can act on ...
    assert job.status == "failed"
    assert "stale_result" in job.error and "--refresh" in job.error
    assert "Nothing was spent" in job.error and "paid (Deep) result" in job.error
    # ... having asked no provider and the money authority for nothing ...
    assert again.calls == 0
    assert not any("recognis" in line for line in job.log), list(job.log)
    assert _count(jobs.database, "run_dispatches") == dispatches
    assert _count(jobs.database, "run_reservations") == reservations
    assert _count(jobs.database, "run_dispatches", run_id) == 0
    assert _count(jobs.database, "run_reservations", run_id) == 0
    # ... and still settles exactly once, at zero.
    row = _settlement(jobs.database, run_id)
    assert row is not None and row["status"] == "failed"
    assert row["usd_e6_spent"] == 0 and row["usd_e6_reserved"] == 0
    (projected,) = _invocations(tmp_path, run_id)
    assert _entry_of(row) == projected
    # The stored result is still the one shown, and the first run's money is untouched.
    assert result_dir(media) == shown
    assert {
        path.relative_to(media).as_posix(): path.read_bytes()
        for path in media.rglob("*")
        if path.is_file() and path.name != "invocations.jsonl"
    } == {key: value for key, value in preserved.items() if not key.endswith("invocations.jsonl")}
    assert _settlement(jobs.database, first_run)["usd_e6_spent"] == paid.calls * 5_000


def _damage_legacy_metadata(media: Path, case: str) -> None:
    path = media / "invocations.jsonl"
    if case == "missing":
        path.unlink()
    elif case == "malformed":
        path.write_text("{not-json}\n", encoding="utf-8")
    elif case == "truncated":
        path.write_bytes(path.read_bytes()[:-11])
    else:
        rows = [json.loads(line) for line in read_text(path).splitlines()]
        completed = next(row for row in rows if row["status"] == "complete")
        rows.append({**completed, "achieved": "free"})
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


@pytest.mark.parametrize("case", ["missing", "malformed", "truncated", "ambiguous"])
def test_unreadable_legacy_metadata_stops_the_real_worker_before_money(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    config = _env(tmp_path, monkeypatch)
    jobs = LocalJobs(tmp_path)
    paid = FakeAudD(SCRIPT)
    first = jobs.submit(str(AUDIO), "max_accuracy")
    _run_one(tmp_path, config, paid)
    assert jobs.get(first).status == "succeeded"
    (media,) = [path.parent for path in Path(native_path(tmp_path)).glob("*/*/present")]
    _stamp_as_fusion_2(result_dir(media))
    _make_pre_bundle(media)
    _damage_legacy_metadata(media, case)
    preserved = {
        path.relative_to(media).as_posix(): path.read_bytes()
        for path in media.rglob("*")
        if path.is_file() and path.name != "invocations.jsonl"
    }
    dispatches = _count(jobs.database, "run_dispatches")
    reservations = _count(jobs.database, "run_reservations")

    blocked = FakeAudD(SCRIPT)
    second = jobs.submit(str(AUDIO), "max_accuracy")
    _run_one(tmp_path, config, blocked)
    job = jobs.get(second)
    run_id = jobs.queue.get(second).run_id

    assert job.status == "failed" and "trustworthy legacy metadata" in job.error
    assert "--refresh" in job.error and "Nothing was spent" in job.error
    assert blocked.calls == 0
    assert _count(jobs.database, "run_dispatches") == dispatches
    assert _count(jobs.database, "run_reservations") == reservations
    assert _count(jobs.database, "run_dispatches", run_id) == 0
    assert _count(jobs.database, "run_reservations", run_id) == 0
    settlement = _settlement(jobs.database, run_id)
    assert settlement["usd_e6_spent"] == 0 and settlement["usd_e6_reserved"] == 0
    assert {
        path.relative_to(media).as_posix(): path.read_bytes()
        for path in media.rglob("*")
        if path.is_file() and path.name != "invocations.jsonl"
    } == preserved


def test_hosted_stale_free_lookup_precedes_every_hint_connector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hosted intake reuses the stored hint identity without constructing any network client."""

    from idea_web.jobs import worker as worker_module

    database = local_database(tmp_path)
    work = tmp_path / "w"
    media = work / "s" / ("b" * 64)
    source = media / "ingest/source.json"
    original = media / "ingest/original.bin"
    bundle = media / "present/bundles/stale"
    source.parent.mkdir(parents=True)
    bundle.mkdir(parents=True)
    source.write_text("{}", encoding="utf-8")
    original.write_bytes(b"cached")
    inputs = AnalysisInputs(
        media.name,
        FREE_RECIPE.recipe_id,
        "platform",
        "public",
        "stored-hints-snapshot",
    )
    stale = (bundle, CompatibilityRequest(inputs, FREE_RECIPE))
    cached = SimpleNamespace(
        media_dir=media,
        source_path=source,
        original_path=original,
        record=SimpleNamespace(media_key=media.name),
    )
    monkeypatch.setattr(worker_module, "_load_cached", lambda *_args: cached)
    monkeypatch.setattr(worker_module, "find_stale_result", lambda *_args, **_kwargs: stale)
    monkeypatch.setattr(
        worker_module,
        "read_bundle_manifest",
        lambda path: {"duration_ms": 60_000} if Path(path) in {bundle, source.parent} else None,
    )

    async def no_connectors(**_kwargs):
        pytest.fail("hosted stale lookup ran a hint connector")

    def no_client(*_args, **_kwargs):
        pytest.fail("hosted stale lookup constructed an HTTP client")

    sockets: list[object] = []
    real_connect = socket.socket.connect

    def no_socket(self, address, *args, **kwargs):  # noqa: ANN001
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return real_connect(self, address, *args, **kwargs)
        sockets.append(address)
        pytest.fail("hosted stale lookup opened a network socket")

    monkeypatch.setattr(worker_module, "run_hints", no_connectors)
    monkeypatch.setattr("httpx.AsyncClient", no_client)
    monkeypatch.setattr(socket.socket, "connect", no_socket)
    monkeypatch.setattr(socket.socket, "connect_ex", no_socket)

    worker = Worker(database, work)
    job_id = worker.queue.enqueue(PlatformUrl("https://soundcloud.com/example/stale"), FREE_RECIPE)
    job = worker.queue.claim("hosted-stale", lease_seconds=600)
    assert job is not None and job.id == job_id
    intake = worker._prepare_intake(job, worker._store(job))
    assert intake.inputs == inputs and intake.duration_ms == 60_000 and sockets == []

"""Upkeep is OFF the readiness path, and a backup carries what upkeep published.

* ``idea.cmd`` gives the server 30 seconds to answer ``/healthz``.  Bringing fourteen stored mixes
  up to date took 21-35 s when it ran before the server served anything; with more mixes it only
  grows.  The server now spends a small, fixed budget on it and finishes the rest beside itself.
* A re-fusion publishes a bundle and moves ``present/current`` without any database row, so a
  backup that reads bundles only from the database carried a pointer to nothing.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import httpx
import pytest

from id_detector.io import read_text, sha256_file
from id_detector.present.bundles import bundle_id, read_bundle_manifest, result_dir
from id_detector.refusion import Refusion
from idea_web import server as server_module
from idea_web.backup import backup, restore, verify
from idea_web.database import LOCAL_DATABASE_DIR
from idea_web.server import make_server, run_in_background
from tests.idea_web.test_backup import (
    CREATED,
    DATABASE_NAME,
    MEDIA,
    RUN,
    SOURCE,
    _seal_directory,
    _work_root,
)

SLOW_SECONDS = 0.6
MIXES = 24  # 24 x 0.6 s = 14 s of re-fusion: far beyond the budget, as the owner's library is


def _stale_library(root: Path) -> list[Path]:
    media = []
    for index in range(MIXES):
        present = root / f"{index:064x}" / f"{index + 1000:064x}" / "present"
        present.mkdir(parents=True)
        media.append(present.parent)
    return media


def test_the_server_answers_healthz_long_before_a_slow_upkeep_pass_is_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "work"
    media = _stale_library(root)
    broken = media[5].name
    seen: list[str] = []
    scanned_on: list[str] = []
    health_answered = threading.Event()

    real_scan = server_module.Upkeep._media_directories

    def recorded_scan(upkeep):
        scanned_on.append(threading.current_thread().name)
        return real_scan(upkeep)

    def slow_refuse(media_dir: Path, *, config=None) -> Refusion:
        seen.append(Path(media_dir).name)
        # Moving upkeep back onto the serving thread makes this wait prevent uvicorn from
        # starting and the health assertion below time out.  On the background thread, health
        # answers first and releases the deliberately slow pass.
        assert health_answered.wait(timeout=10), "upkeep blocked HTTP readiness"
        time.sleep(SLOW_SECONDS)
        if Path(media_dir).name == broken:
            raise RuntimeError("this mix is damaged")
        return Refusion("refused", bundle=Path(media_dir) / "present")

    monkeypatch.setattr("id_detector.refusion.refuse_stale_result", slow_refuse)
    monkeypatch.setattr(server_module.Upkeep, "_media_directories", recorded_scan)
    started = time.monotonic()
    running = run_in_background(make_server(root, port=0))
    try:
        upkeep = running.server.upkeep
        deadline = started + 20
        answered = None
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{running.base_url}/healthz", timeout=1).status_code == 200:
                    answered = time.monotonic() - started
                    health_answered.set()
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        assert answered is not None, "the server never answered /healthz"
        # Ready before the first deliberately blocked re-fusion, not after the full pass.
        assert answered < SLOW_SECONDS + 3.0 < MIXES * SLOW_SECONDS
        assert scanned_on == ["idea-upkeep"]  # discovery itself was off the serving thread
        assert not upkeep.report.finished.is_set() and len(seen) < MIXES  # still working, beside us
        assert httpx.get(f"{running.base_url}/", timeout=5).status_code == 200  # and serving pages
        assert upkeep.report.finished.wait(timeout=60)
        # One mix failing stops neither the rest nor the server, and it is reported by name.
        assert len(seen) == MIXES and len(upkeep.report.updated) == MIXES - 1
        ((name, error),) = upkeep.report.failed
        assert name.endswith(broken[:12]) and "this mix is damaged" in error
        assert httpx.get(f"{running.base_url}/healthz", timeout=5).status_code == 200
    finally:
        health_answered.set()
        running.shutdown()


def test_the_bounded_pass_stops_at_its_budget_and_the_background_finishes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "work"
    _stale_library(root)
    threads: list[str] = []

    def quick(media_dir: Path, *, config=None) -> Refusion:
        threads.append(threading.current_thread().name)
        time.sleep(0.05)
        return Refusion("current")

    monkeypatch.setattr("id_detector.refusion.refuse_stale_result", quick)
    upkeep = server_module.Upkeep(root, budget_seconds=0.2)
    upkeep.before_serving()
    done_in_time = len(threads)
    assert 0 < done_in_time < MIXES and not upkeep.report.finished.is_set()
    upkeep.continue_in_background()
    assert upkeep.report.finished.wait(timeout=30)
    assert len(threads) == MIXES and set(threads[done_in_time:]) == {"idea-upkeep"}


def _re_fused_by_upkeep(work_root: Path) -> tuple[Path, Path]:
    """What an offline re-fusion leaves: a sealed run and bundle, the pointer moved — and NO
    ``analysis_runs`` / ``result_bundles`` row."""

    media = work_root / SOURCE / MEDIA
    run_id = "refuse0004-" + "a" * 32
    fuse_run = media / "fuse" / "runs" / run_id
    fuse_run.mkdir(parents=True)
    (fuse_run / "episodes.json").write_bytes(b'{"episodes": ["re-fused"]}')
    _seal_directory(fuse_run, {"run_id": run_id})
    bundle = media / "present" / "bundles" / bundle_id(run_id, 25)
    bundle.mkdir(parents=True)
    for name in ("index.html", "tracklist.json", "source.json"):
        (bundle / name).write_bytes(b'{"fusion": 4}')
    _seal_directory(
        bundle,
        {
            "run_id": run_id,
            "presentation_version": 25,
            "status": "complete",
            "fuse_run": f"fuse/runs/{run_id}",
            "duration_ms": 1000,
            "refusion": {"source_run_id": RUN, "source_bundle": None, "fusion_version": 4},
        },
    )
    (media / "present" / "current").write_bytes(bundle.name.encode("ascii"))
    assert result_dir(media).name == bundle.name
    return bundle, fuse_run


def test_a_re_fused_result_survives_backup_and_restore(tmp_path: Path) -> None:
    work_root, database, original = _work_root(tmp_path)
    bundle, fuse_run = _re_fused_by_upkeep(work_root)
    with database.write() as connection:  # simulate startup publication with no database author
        connection.execute("DELETE FROM result_bundles")
        connection.execute("DELETE FROM analysis_runs")
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM result_bundles").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0

    snapshot = tmp_path / "snapshot"
    document = backup(database, work_root, snapshot, now=CREATED)
    assert document["skipped"] == [] and verify(snapshot) == []
    listed = {entry["path"] for entry in document["artefacts"]}
    relative = f"{SOURCE}/{MEDIA}"
    assert f"{relative}/present/current" in listed
    assert f"{relative}/present/bundles/{bundle.name}/manifest.json" in listed
    assert f"{relative}/fuse/runs/{fuse_run.name}/episodes.json" in listed
    assert f"{relative}/present/bundles/{original.name}/manifest.json" not in listed

    restored = tmp_path / "restored"
    restore(snapshot, restored)
    media = restored / SOURCE / MEDIA
    current = result_dir(media)
    assert current.name == bundle.name and read_text(media / "present/current") == bundle.name
    manifest = read_bundle_manifest(current)
    assert manifest is not None and manifest["refusion"]["fusion_version"] == 4
    assert sha256_file(media / manifest["fuse_run"] / "episodes.json") == sha256_file(
        fuse_run / "episodes.json"
    )
    connection = sqlite3.connect(restored / LOCAL_DATABASE_DIR / DATABASE_NAME)
    try:
        assert connection.execute("SELECT COUNT(*) FROM result_bundles").fetchone()[0] == 0
    finally:
        connection.close()

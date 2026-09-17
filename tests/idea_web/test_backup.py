"""Cycle 4b-iii: backups and the restore drill (plan §4.6; risk R14).

Offline and deterministic. The drill's inputs are the committed ``tests/fixtures/snapshot/``: its
artefacts as bytes, and its queue database as SQL (so every committed byte is readable). The
snapshot itself is sealed here by the production ``describe``/``seal``, so what the drill restores
is a real snapshot and not a hand-written stand-in.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from id_detector import retention
from id_detector.io import (
    SCHEMA_VERSION as SIDECAR_SCHEMA_VERSION,
)
from id_detector.io import (
    canonical_json_bytes,
    read_text,
    sha256_file,
    write_completion_sidecar,
)
from id_detector.jobs import ProcessLock
from id_detector.present.bundles import bundle_id
from idea_web import backup as backup_module
from idea_web import durable
from idea_web.backup import (
    ARTEFACTS_DIR,
    DATABASE_NAME,
    JOURNAL_NAME,
    SNAPSHOT_NAME,
    STAGING_DIR,
    BackupRefused,
    backup,
    contained,
    describe,
    recover_interrupted,
    restore,
    safe_entry_path,
    seal,
    verify,
    verify_artefacts,
    verify_snapshot,
)
from idea_web.database import LOCAL_DATABASE_DIR, SUPERVISOR_LOCK_NAME, Database
from idea_web.jobs import local as local_module
from idea_web.jobs.local import LocalJobs, LocalWorkerSupervisor, MigrationRefused, local_database

FAKES = "tests.idea_web.local_runner_fakes"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "snapshot"
SOURCE = "fixture-source"
MEDIA = "fixture-media"
RUN = next((FIXTURE / "artefacts" / SOURCE / MEDIA / "fuse" / "runs").iterdir()).name
FIXTURE_WORK_ROOT = "/idea-fixture-work"
CREATED = 1_760_000_000.0


def _materialise_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(read_text(FIXTURE / "app.sql"))
    finally:
        connection.close()


def _snapshot(tmp_path: Path, name: str = "snapshot") -> Path:
    destination = tmp_path / name
    shutil.copytree(FIXTURE / ARTEFACTS_DIR, destination / ARTEFACTS_DIR)
    _materialise_database(destination / DATABASE_NAME)
    seal(
        destination,
        created_at=CREATED,
        work_root=Path(FIXTURE_WORK_ROOT),
        **describe(destination),
    )
    return destination


def _work_root(tmp_path: Path) -> tuple[Path, Database, Path]:
    work_root = tmp_path / "work"
    shutil.copytree(FIXTURE / ARTEFACTS_DIR, work_root)
    database_path = work_root / LOCAL_DATABASE_DIR / "app.db"
    _materialise_database(database_path)
    bundle = next((work_root / SOURCE / MEDIA / "present" / "bundles").iterdir())
    database = Database(database_path)
    with database.write() as connection:
        connection.execute("UPDATE result_bundles SET path=?", (str(bundle.resolve()),))
    return work_root, database, bundle


def _seal_directory(directory: Path, metadata: dict) -> None:
    files = {
        path.relative_to(directory).as_posix(): {
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    }
    (directory / "manifest.json").write_bytes(canonical_json_bytes({**metadata, "files": files}))


def _add_medium(
    work_root: Path,
    database: Database,
    *,
    media_key: str,
    run_id: str,
    status: str = "complete",
    on_disk: bool = True,
) -> Path:
    """A second published medium. ``on_disk=False`` writes only the rows — no directory at all."""

    media = work_root / SOURCE / media_key
    identifier = bundle_id(run_id, 1)
    bundle = media / "present" / "bundles" / identifier
    if on_disk:
        (media / "ingest").mkdir(parents=True)
        (media / "ingest" / "source.json").write_bytes(b'{"media": "second"}')
        (media / "recognise").mkdir()
        (media / "recognise" / "attempts.jsonl").write_bytes(b'{"event": "prepared"}\n')
        fuse_run = media / "fuse" / "runs" / run_id
        fuse_run.mkdir(parents=True)
        (fuse_run / "episodes.json").write_bytes(b'{"episodes": []}')
        _seal_directory(fuse_run, {"run_id": run_id})
        bundle.mkdir(parents=True)
        for name in ("index.html", "tracklist.json", "source.json"):
            (bundle / name).write_bytes(b"{}")
        _seal_directory(
            bundle,
            {
                "run_id": run_id,
                "presentation_version": 1,
                "status": "complete",
                "fuse_run": f"fuse/runs/{run_id}",
                "duration_ms": 1000,
            },
        )
    with database.write() as connection:
        connection.execute(
            "INSERT INTO media(media_key, duration_ms, first_seen) VALUES (?, ?, ?)",
            (media_key, 1000, 1000.0),
        )
        connection.execute(
            "INSERT INTO analysis_runs(run_id, analysis_key, media_key, requested_recipe_id, "
            "algorithm_version, adapter_versions, achieved, status, tenant_scope, attempts, "
            "checkpoints, usd_e6_reserved, usd_e6_spent, started_at, finished_at, "
            "analysis_inputs, non_recipe_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'public', "
            "0, '{}', 0, 0, ?, ?, ?, ?)",
            (
                run_id,
                "d" * 64,
                media_key,
                "e" * 64,
                "1",
                "{}",
                "free",
                status,
                1000.0,
                1000.0,
                "{}",
                "f" * 64,
            ),
        )
        if status == "complete" and on_disk:
            connection.execute(
                "INSERT INTO result_bundles(bundle_id, run_id, presentation_version, path, "
                "manifest_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    run_id,
                    1,
                    str(bundle.resolve()),
                    sha256_file(bundle / "manifest.json"),
                    1000.0,
                ),
            )
    return media


def _insert_terminal_run(database: Database, *, run_id: str, media_key: str) -> None:
    with database.write() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO media(media_key, duration_ms, first_seen) VALUES (?, ?, ?)",
            (media_key, 1000, 1000.0),
        )
        connection.execute(
            "INSERT INTO analysis_runs(run_id, analysis_key, media_key, requested_recipe_id, "
            "algorithm_version, adapter_versions, achieved, status, tenant_scope, attempts, "
            "checkpoints, usd_e6_reserved, usd_e6_spent, started_at, finished_at, "
            "analysis_inputs, non_recipe_key) VALUES (?, ?, ?, ?, ?, ?, ?, 'complete', 'public', "
            "0, '{}', 0, 0, ?, ?, ?, ?)",
            (
                run_id,
                "d" * 64,
                media_key,
                "e" * 64,
                "1",
                "{}",
                "free",
                1000.0,
                1000.0,
                "{}",
                "f" * 64,
            ),
        )


def _abandon(
    work_root: Path,
    *,
    targets: list[tuple[str, bool]],
    kept: dict[str, bytes],
    name: str = "abandoned",
    database: str | None = None,
    database_existed: bool = True,
) -> Path:
    """A recovery tree exactly as an interrupted publication leaves one."""

    staging = work_root / LOCAL_DATABASE_DIR / f"{STAGING_DIR}-{name}"
    staging.mkdir(parents=True, exist_ok=True)
    for relative, data in kept.items():
        path = staging / backup_module.REPLACED_DIR / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (staging / JOURNAL_NAME).write_bytes(
        json.dumps(
            {
                "state": "publishing",
                "database": database or f"{LOCAL_DATABASE_DIR}/{DATABASE_NAME}",
                "database_existed": database_existed,
                "targets": [{"path": path, "existed": existed} for path, existed in targets],
            }
        ).encode("utf-8")
    )
    return staging


def _staging_trees(work_root: Path) -> list[Path]:
    return list((work_root / LOCAL_DATABASE_DIR).glob(f"{STAGING_DIR}-*"))


def _junction(link: Path, target: Path) -> bool:
    """A real Windows junction, or False when this platform cannot make one."""

    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True
    )
    return made.returncode == 0 and link.exists()


class _Holder:
    """Holds one medium's artefact lock for the ``with`` block, in its own thread."""

    def __init__(self, media: Path, *, on_hold=None) -> None:
        self.media = media
        self.on_hold = on_hold
        self.holding = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._hold, name="scan", daemon=True)

    def _hold(self) -> None:
        lock = ProcessLock(self.media / ".media.lock")
        lock.acquire()
        try:
            self.holding.set()
            if self.on_hold is not None:
                self.on_hold()
            self.release.wait(timeout=30)
        finally:
            lock.release()

    def __enter__(self) -> _Holder:
        self.thread.start()
        assert self.holding.wait(timeout=10)
        return self

    def __exit__(self, *exc: object) -> None:
        self.release.set()
        self.thread.join(timeout=30)


# --------------------------------------------------------------------------------------------------
# The corpus is untouchable
# --------------------------------------------------------------------------------------------------
def test_no_backup_destination_anywhere_beneath_a_corpus_is_accepted(tmp_path: Path) -> None:
    """The frozen `io` backstop looks two directories up; a corpus needs the whole walk."""

    work_root, database, _bundle = _work_root(tmp_path)
    corpus = tmp_path / "data" / "corpus" / "dev-1"
    corpus.mkdir(parents=True)
    (corpus / "corpus-version.json").write_bytes(b'{"version": 1}')
    nested = corpus / "backups" / "nightly" / "snapshot"
    marker = corpus / "corpus-version.json"
    before = marker.read_bytes()
    for destination in (corpus, corpus / "backups", nested):
        with pytest.raises(BackupRefused, match="corpus"):
            backup(database, work_root, destination)
        # Nothing of a snapshot was made, and no staging tree was left beside it.
        assert not (destination / SNAPSHOT_NAME).exists()
        assert not list(destination.parent.glob(f".{destination.name}.staging-*"))
    assert marker.read_bytes() == before
    # The conventional home is protected even before any corpus file exists in it.
    with pytest.raises(BackupRefused, match="corpus"):
        backup(database, work_root, tmp_path / "data" / "corpus" / "fresh" / "snapshot")


def test_a_restore_that_would_publish_inside_a_corpus_is_refused_before_anything_moves(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    work_root = tmp_path / "data" / "corpus" / "release-1" / "work"
    work_root.mkdir(parents=True)
    (work_root.parent / "corpus-version.json").write_bytes(b'{"version": 1}')
    keepsake = work_root.parent / "ground_truth.json"
    keepsake.write_bytes(b'{"tracks": ["hand verified"]}')
    with pytest.raises(BackupRefused, match="corpus"):
        restore(snapshot, work_root)
    assert keepsake.read_bytes() == b'{"tracks": ["hand verified"]}'
    assert not (work_root / SOURCE).exists()


# --------------------------------------------------------------------------------------------------
# A backup owns its destination
# --------------------------------------------------------------------------------------------------
def test_a_backup_destination_may_never_overlap_the_work_tree_or_its_database(
    tmp_path: Path,
) -> None:
    work_root, database, _bundle = _work_root(tmp_path)
    for destination in (
        work_root,
        work_root / LOCAL_DATABASE_DIR,
        work_root / "inside",
        work_root.parent,
    ):
        with pytest.raises(BackupRefused, match="may not overlap"):
            backup(database, work_root, destination)
    assert database.path.is_file() and database.version() >= 4


def test_a_backup_refuses_to_reuse_a_sealed_destination(tmp_path: Path) -> None:
    work_root, database, _bundle = _work_root(tmp_path)
    destination = tmp_path / "snapshot"
    backup(database, work_root, destination, now=CREATED)
    before = (destination / SNAPSHOT_NAME).read_bytes()
    with pytest.raises(BackupRefused, match="already holds a sealed snapshot"):
        backup(database, work_root, destination)
    assert (destination / SNAPSHOT_NAME).read_bytes() == before
    assert verify(destination) == []


def test_a_crash_before_publication_leaves_the_destination_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_root, database, _bundle = _work_root(tmp_path)
    destination = tmp_path / "snapshot"

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("the disk went away while sealing")

    monkeypatch.setattr(backup_module, "_seal_into", boom)
    with pytest.raises(OSError, match="went away"):
        backup(database, work_root, destination)
    monkeypatch.undo()
    assert not destination.exists()
    assert not list(tmp_path.glob(".snapshot.staging-*"))


def test_a_link_or_junction_is_refused_as_a_root_not_merely_beneath_one(tmp_path: Path) -> None:
    """A junction *root* relocates every path built from it, and `is_symlink()` cannot see one."""

    work_root, database, _bundle = _work_root(tmp_path)
    alias = tmp_path / "work-alias"
    if not _junction(alias, work_root):  # pragma: no cover - POSIX or no privilege
        pytest.skip("this platform cannot create a junction")
    assert not alias.is_symlink()  # exactly why is_symlink() is not the rule

    with pytest.raises(BackupRefused, match="link or junction"):
        backup(database, alias, tmp_path / "snapshot-a")
    destination_alias = tmp_path / "destination-alias"
    outside = tmp_path / "outside"
    outside.mkdir()
    if _junction(destination_alias, outside):
        with pytest.raises(BackupRefused, match="link or junction"):
            backup(database, work_root, destination_alias)
    snapshot = _snapshot(tmp_path)
    with pytest.raises(BackupRefused, match="link or junction"):
        restore(snapshot, alias)


# --------------------------------------------------------------------------------------------------
# Completeness, classification and coexistence
# --------------------------------------------------------------------------------------------------
def test_a_backup_lists_every_published_artefact_and_each_one_verifies(tmp_path: Path) -> None:
    work_root, database, bundle = _work_root(tmp_path)
    destination = tmp_path / "snapshot"
    document = backup(database, work_root, destination, now=CREATED)

    prefix = f"{SOURCE}/{MEDIA}"
    listed = {entry["path"] for entry in document["artefacts"]}
    for expected in (
        f"{prefix}/ingest/source.json",
        f"{prefix}/recognise/shazam.jsonl",
        f"{prefix}/hints/hints.json",
        f"{prefix}/fuse/runs/{RUN}/manifest.json",
        f"{prefix}/present/bundles/{bundle.name}/manifest.json",
    ):
        assert expected in listed, expected
    assert document["skipped"] == []
    assert document["work_root"] == str(work_root)
    assert verify(destination) == []


def test_the_reported_counts_are_what_really_happened(tmp_path: Path) -> None:
    """``linked`` counts hard links that succeeded, not entries eligible for one."""

    work_root, database, bundle = _work_root(tmp_path)
    destination = tmp_path / "snapshot"
    document = backup(database, work_root, destination, now=CREATED)
    root = destination / ARTEFACTS_DIR / SOURCE / MEDIA
    staged_bundle = root / "present" / "bundles" / bundle.name
    linked = [path for path in staged_bundle.rglob("*") if path.is_file()]
    assert document["linked"] == len(linked)
    assert all(path.stat().st_nlink > 1 for path in linked)
    # A frozen fuse run is keyed by a caller-supplied run id, not a content address: copied.
    for copied in (
        root / "fuse" / "runs" / RUN / "manifest.json",
        root / "recognise" / "shazam.jsonl",
        root / "ingest" / "source.json",
    ):
        assert copied.stat().st_nlink == 1, copied
    assert document["copied"] == len(document["artefacts"]) - document["linked"]


def test_a_sealed_snapshot_is_not_changed_by_a_later_scan(tmp_path: Path) -> None:
    work_root, database, _bundle = _work_root(tmp_path)
    destination = tmp_path / "snapshot"
    backup(database, work_root, destination, now=CREATED)
    journal = work_root / SOURCE / MEDIA / "recognise" / "shazam.jsonl"
    kept = destination / ARTEFACTS_DIR / SOURCE / MEDIA / "recognise" / "shazam.jsonl"
    before = kept.read_bytes()
    with open(journal, "ab") as handle:
        handle.write(b'{"window": 99, "outcome": "match"}\n')
    assert kept.read_bytes() == before
    assert verify(destination) == []


def test_a_non_terminal_run_is_always_skipped_even_when_nothing_holds_its_lock(
    tmp_path: Path,
) -> None:
    work_root, database, _bundle = _work_root(tmp_path)
    _add_medium(work_root, database, media_key="live-media", run_id="live-run", status="analysis")
    document = backup(database, work_root, tmp_path / "snapshot", now=CREATED)
    reasons = [item["reason"] for item in document["skipped"]]
    assert any("still in flight" in reason for reason in reasons)
    assert verify(tmp_path / "snapshot") == []


def test_a_terminal_run_with_no_media_directory_at_all_is_recorded_as_skipped(
    tmp_path: Path,
) -> None:
    """Silently omitting it would let the snapshot claim a completeness it does not have."""

    work_root, database, _bundle = _work_root(tmp_path)
    _add_medium(
        work_root, database, media_key="vanished-media", run_id="vanished-run", on_disk=False
    )
    document = backup(database, work_root, tmp_path / "snapshot", now=CREATED)
    vanished = [item for item in document["skipped"] if item["runs"] == ["vanished-run"]]
    assert vanished and "no media directory" in vanished[0]["reason"]
    assert vanished[0]["media"] == "vanished-media"
    assert verify(tmp_path / "snapshot") == []


def test_a_backup_never_waits_for_a_busy_medium(tmp_path: Path) -> None:
    work_root, database, bundle = _work_root(tmp_path)
    media = [bundle.parents[2]]
    for index in range(3):
        media.append(
            _add_medium(work_root, database, media_key=f"busy-{index}", run_id=f"busy-run-{index}")
        )
    holders = [_Holder(path) for path in media]
    for holder in holders:
        holder.__enter__()
    try:
        started = time.monotonic()
        document = backup(database, work_root, tmp_path / "snapshot", now=CREATED)
        elapsed = time.monotonic() - started
    finally:
        for holder in holders:
            holder.__exit__()
    assert elapsed < 2.0, elapsed
    assert len([item for item in document["skipped"] if "media is active" in item["reason"]]) == 4
    assert verify(tmp_path / "snapshot") == []


def test_a_bundle_commit_in_progress_makes_gc_and_backup_give_way_and_both_see_it(
    tmp_path: Path,
) -> None:
    """The overlap, made deterministic: a production ``publish_result`` runs while its medium's
    artefact lock is held, exactly as the pipeline holds it, and GC and a backup run inside that
    same window. Both give way on that medium, the backup SAYS it did, and once the commit has
    finished the next backup carries the bundle it produced."""

    from tests.test_phase1a_bundles import publish, seed

    work_root, database, _bundle = _work_root(tmp_path)
    committing = Path(str(seed(work_root)))  # a real 64-hex media tree GC actually walks
    media_key = committing.name
    _insert_terminal_run(database, run_id="run-concurrent", media_key=media_key)
    published: dict[str, Path] = {}

    def commit() -> None:
        published["bundle"] = Path(str(publish(committing, "run-concurrent")))

    with _Holder(committing, on_hold=commit):
        started = time.monotonic()
        gc = retention.collect(work_root, policy="local", apply=True)
        during = backup(database, work_root, tmp_path / "during", now=CREATED)
        elapsed = time.monotonic() - started
    assert elapsed < 5.0, elapsed

    assert any(
        action.operation == "skip"
        and action.reason == "media is active"
        and Path(str(action.path)).name == media_key
        for action in gc.actions
    ), gc.actions
    seen = [item for item in during["skipped"] if item["media"].endswith(media_key)]
    assert seen and "media is active" in seen[0]["reason"]
    assert seen[0]["runs"] == ["run-concurrent"]
    assert not any(media_key in entry["path"] for entry in during["artefacts"])
    assert verify(tmp_path / "during") == []

    bundle = published["bundle"]
    manifest = json.loads(read_text(bundle / "manifest.json"))
    with database.write() as connection:
        connection.execute(
            "INSERT INTO result_bundles(bundle_id, run_id, presentation_version, path, "
            "manifest_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                bundle.name,
                "run-concurrent",
                manifest["presentation_version"],
                str(bundle),
                sha256_file(bundle / "manifest.json"),
                1000.0,
            ),
        )
    after = backup(database, work_root, tmp_path / "after", now=CREATED)
    assert any(
        entry["path"].endswith(f"{bundle.name}/manifest.json") for entry in after["artefacts"]
    )
    assert verify(tmp_path / "after") == []


def test_a_backup_refuses_rather_than_seal_an_incomplete_snapshot(tmp_path: Path) -> None:
    work_root, database, bundle = _work_root(tmp_path)
    shutil.rmtree(bundle)
    with pytest.raises(BackupRefused, match="incomplete"):
        backup(database, work_root, tmp_path / "snapshot")


def test_a_backup_refuses_a_bundle_that_lives_outside_the_work_root(tmp_path: Path) -> None:
    work_root, database, _bundle = _work_root(tmp_path)
    with database.write() as connection:
        connection.execute("UPDATE result_bundles SET path=?", (str(tmp_path / "elsewhere"),))
    with pytest.raises(BackupRefused, match="outside the work root"):
        backup(database, work_root, tmp_path / "snapshot")


def test_a_backup_refuses_a_published_bundle_whose_manifest_does_not_describe_it(
    tmp_path: Path,
) -> None:
    work_root, database, bundle = _work_root(tmp_path)
    (bundle / "tracklist.json").write_bytes(b'{"tracks": ["not what the manifest sealed"]}')
    with pytest.raises(BackupRefused, match="does not verify"):
        backup(database, work_root, tmp_path / "snapshot")


# --------------------------------------------------------------------------------------------------
# The restore drill (plan §4.6; launch gate L5)
# --------------------------------------------------------------------------------------------------
def test_the_restore_drill_runs_from_the_committed_fixture(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    assert verify(snapshot) == []

    work_root = tmp_path / "restored"
    report = restore(snapshot, work_root)
    assert report["artefacts"] == len(describe(snapshot)["entries"])
    assert report["created_at"] == CREATED and report["skipped"] == []

    bundle = next((work_root / SOURCE / MEDIA / "present" / "bundles").iterdir())
    assert (bundle / "manifest.json").is_file()
    assert (work_root / LOCAL_DATABASE_DIR / DATABASE_NAME).is_file()
    assert verify_artefacts(work_root, backup_module.read_snapshot(snapshot)) == []
    assert not list((work_root / LOCAL_DATABASE_DIR).glob(f"{STAGING_DIR}-*"))

    connection = sqlite3.connect(work_root / LOCAL_DATABASE_DIR / DATABASE_NAME)
    try:
        connection.row_factory = sqlite3.Row
        run = connection.execute("SELECT run_id, status FROM analysis_runs").fetchone()
        stored = connection.execute("SELECT path FROM result_bundles").fetchone()
    finally:
        connection.close()
    assert run["run_id"] == RUN and run["status"] == "complete"
    assert report["rerooted"] == 1
    assert Path(stored["path"]).is_relative_to(work_root.resolve())
    assert Path(stored["path"]).is_dir()


def test_a_snapshot_restores_into_a_different_work_root_and_backs_up_again(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    restore(snapshot, elsewhere)
    again = backup(
        Database(elsewhere / LOCAL_DATABASE_DIR / DATABASE_NAME),
        elsewhere,
        tmp_path / "second",
        now=CREATED,
    )
    assert again["skipped"] == []
    assert verify(tmp_path / "second") == []
    moved = next((elsewhere / SOURCE / MEDIA / "present" / "bundles").iterdir()).name
    listed = {entry["path"] for entry in again["artefacts"]}
    assert f"{SOURCE}/{MEDIA}/present/bundles/{moved}/manifest.json" in listed


def test_restore_replaces_stale_media_anywhere_in_the_work_root(tmp_path: Path) -> None:
    """The whole locked root is inspected: a medium the snapshot never mentions must not survive
    a restore. One it mentions only under ``skipped`` is owner data the snapshot never held, so it
    is preserved (the coordinator's decision after round 5; this test used to demand removal)."""

    snapshot = _snapshot(tmp_path)
    document = json.loads((snapshot / SNAPSHOT_NAME).read_text(encoding="utf-8"))
    document["skipped"] = [
        {"media": f"{SOURCE}/skipped-media", "reason": "media is active", "bundles": [], "runs": []}
    ]
    (snapshot / SNAPSHOT_NAME).write_bytes(json.dumps(document).encode("utf-8"))

    work_root = tmp_path / "restored"
    absent = work_root / SOURCE / "newer-media" / "present" / "bundles" / ("b" * 64)
    absent.mkdir(parents=True)
    (absent / "index.html").write_bytes(b"<p>a newer mix</p>")
    skipped = work_root / SOURCE / "skipped-media" / "present" / "bundles" / ("c" * 64)
    skipped.mkdir(parents=True)
    (skipped / "index.html").write_bytes(b"<p>only ever skipped</p>")

    report = restore(snapshot, work_root)
    assert report["removed"] >= 1
    assert not (absent / "index.html").exists()
    assert (skipped / "index.html").read_bytes() == b"<p>only ever skipped</p>"
    assert report["preserved"] == {
        f"{SOURCE}/skipped-media": "preserved: not in this snapshot (media is active)"
    }
    assert (work_root / SOURCE / MEDIA / "ingest" / "source.json").is_file()


def test_restore_refuses_a_database_outside_the_locked_work_root(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    work_root = tmp_path / "restored"
    outsider = tmp_path / "other" / LOCAL_DATABASE_DIR / DATABASE_NAME
    outsider.parent.mkdir(parents=True)
    outsider.write_bytes(b"somebody else's live database")
    with pytest.raises(BackupRefused, match="outside"):
        restore(snapshot, work_root, database_path=outsider)
    assert outsider.read_bytes() == b"somebody else's live database"


def test_restore_refuses_a_semantically_broken_snapshot_and_keeps_the_previous_tree(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    bundle = next((snapshot / ARTEFACTS_DIR / SOURCE / MEDIA / "present" / "bundles").iterdir())
    (bundle / "tracklist.json").write_bytes(b'{"tracks": ["not what the manifest sealed"]}')
    seal(snapshot, created_at=CREATED, work_root=Path(FIXTURE_WORK_ROOT), **describe(snapshot))
    assert verify_snapshot(snapshot) == []
    assert any("does not verify" in problem for problem in verify(snapshot))

    work_root = tmp_path / "restored"
    keepsake = work_root / SOURCE / MEDIA / "ingest" / "source.json"
    keepsake.parent.mkdir(parents=True)
    keepsake.write_bytes(b'{"mine": true}')
    with pytest.raises(BackupRefused, match="do not verify"):
        restore(snapshot, work_root)
    assert keepsake.read_bytes() == b'{"mine": true}'
    assert not (work_root / SOURCE / MEDIA / "present").exists()


# --------------------------------------------------------------------------------------------------
# Abrupt death, and recovery
# --------------------------------------------------------------------------------------------------
_CHILD = """
import sys, time
from pathlib import Path
sys.path.insert(0, {repo!r})
from idea_web import backup as backup_module

snapshot, work_root, sentinel = (Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
real = backup_module._rename

def interrupted(source, destination, guard):
    real(source, destination, guard)
    if backup_module.REPLACED_DIR in Path(destination).parts:
        # An original has just been displaced, after several publications that had no
        # predecessor. Die here: no except, no finally, no cleanup of any kind.
        sentinel.write_bytes(b"publishing")
        while True:
            time.sleep(0.05)

backup_module._rename = interrupted
backup_module.restore(snapshot, work_root)
"""


def test_a_restore_killed_mid_publication_is_recovered_when_idea_serve_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real abrupt death — the process is killed between renames, so no ``except`` and no
    ``finally`` ever run — then ID'er is started the normal way. Recovery must happen before the
    database is opened, must put the displaced original back, and must REMOVE the publications
    that had no predecessor."""

    snapshot = _snapshot(tmp_path)
    work_root = tmp_path / "restored"
    original = work_root / SOURCE / MEDIA / "ingest" / "source.json"
    original.parent.mkdir(parents=True)
    original.write_bytes(b'{"original": true}')

    script = tmp_path / "child.py"
    repo = str(Path(__file__).resolve().parents[2] / "src")
    script.write_text(_CHILD.format(repo=repo), encoding="utf-8")
    sentinel = tmp_path / "publishing.flag"
    child = subprocess.Popen(
        [sys.executable, str(script), str(snapshot), str(work_root), str(sentinel)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 60
        while not sentinel.exists() and time.monotonic() < deadline:
            if child.poll() is not None:
                _out, err = child.communicate()
                raise AssertionError(f"child exited early: {err.decode('utf-8', 'replace')}")
            time.sleep(0.05)
        assert sentinel.exists(), "the child never reached publication"
        child.kill()
    finally:
        child.wait(timeout=30)

    trees = _staging_trees(work_root)
    assert trees and (trees[0] / JOURNAL_NAME).is_file()
    fresh = work_root / SOURCE / MEDIA / "fuse" / "runs" / RUN / "manifest.json"
    assert fresh.is_file()  # a publication with no predecessor was already in place
    assert not original.exists()  # and the original had been moved aside

    # Start ID'er exactly as `idea serve` does: open the queue, then start supervising.
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    jobs = LocalJobs(work_root)
    # Recovery already ran, before the database was opened or migrated.
    assert original.read_bytes() == b'{"original": true}'
    assert not fresh.exists()
    # Every publication that had no predecessor is gone; only the owner's original remains.
    assert sorted(path for path in (work_root / SOURCE).rglob("*") if path.is_file()) == [original]
    assert not _staging_trees(work_root)
    supervisor = LocalWorkerSupervisor(
        work_root,
        config_path=work_root / "idea.toml",
        jobs=jobs,
        runner_spec=f"{FAKES}:slow_runner",
    )
    assert supervisor.start() is True
    supervisor.stop()

    report = restore(snapshot, work_root)  # and the tree is usable again
    assert report["artefacts"]
    assert verify_artefacts(work_root, backup_module.read_snapshot(snapshot)) == []


def test_a_server_start_and_a_new_restore_each_consume_an_interrupted_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot(tmp_path)
    work_root = tmp_path / "restored"
    displaced = work_root / SOURCE / MEDIA / "ingest" / "source.json"
    orphan = work_root / SOURCE / MEDIA / "hints" / "orphan.json"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b'{"published": "with nothing before it"}')
    journal_targets = [
        (f"{SOURCE}/{MEDIA}/ingest/source.json", True),
        (f"{SOURCE}/{MEDIA}/hints/orphan.json", False),
    ]
    kept = {f"{SOURCE}/{MEDIA}/ingest/source.json": b'{"displaced": true}'}
    _abandon(work_root, targets=journal_targets, kept=kept)

    # The supervisor's own pass: a database opened some other way, then `start()`.
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    database = Database(work_root / LOCAL_DATABASE_DIR / DATABASE_NAME)
    database.migrate()
    supervisor = LocalWorkerSupervisor(
        work_root,
        config_path=work_root / "idea.toml",
        jobs=LocalJobs(work_root, database=database),
        runner_spec=f"{FAKES}:slow_runner",
    )
    assert supervisor.start() is True
    supervisor.stop()
    assert displaced.read_bytes() == b'{"displaced": true}'
    assert not orphan.exists()
    assert not _staging_trees(work_root)

    # And a restore consumes one before it begins.
    orphan.write_bytes(b"{}")
    _abandon(work_root, targets=journal_targets, kept=kept, name="again")
    report = restore(snapshot, work_root)
    assert report["recovered"] == 1
    assert not _staging_trees(work_root)
    assert not orphan.exists()
    assert displaced.is_file()


def test_a_tampered_journal_cannot_make_recovery_write_outside_the_work_root(
    tmp_path: Path,
) -> None:
    """Every journal target is confined BEFORE any parent directory is created."""

    work_root = tmp_path / "restored"
    (work_root / LOCAL_DATABASE_DIR).mkdir(parents=True)
    _abandon(work_root, targets=[("../outside/escaped.json", False)], kept={})
    with pytest.raises(BackupRefused, match="unsafe"):
        recover_interrupted(work_root)
    assert not (tmp_path / "outside").exists()
    shutil.rmtree(_staging_trees(work_root)[0])

    _abandon(work_root, targets=[], kept={}, database="../elsewhere/app.db")
    with pytest.raises(BackupRefused, match="unsafe"):
        recover_interrupted(work_root)
    assert not (tmp_path / "elsewhere").exists()
    shutil.rmtree(_staging_trees(work_root)[0])

    _abandon(work_root, targets=[], kept={f"{backup_module._REPLACED_DATABASE}/evil.txt": b"x"})
    with pytest.raises(BackupRefused, match="unexpected file"):
        recover_interrupted(work_root)
    assert not (work_root / LOCAL_DATABASE_DIR / "evil.txt").exists()


def test_restore_replaces_a_live_database_without_leaving_its_wal_behind(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    work_root = tmp_path / "restored"
    database_path = work_root / LOCAL_DATABASE_DIR / DATABASE_NAME
    database_path.parent.mkdir(parents=True)
    database_path.write_bytes(b"an older database")
    for suffix in ("-wal", "-shm"):
        Path(str(database_path) + suffix).write_bytes(b"stale")
    restore(snapshot, work_root)
    assert database_path.is_file()
    for suffix in ("-wal", "-shm"):
        assert not Path(str(database_path) + suffix).exists(), suffix


def test_restore_refuses_while_a_serving_idea_holds_the_supervisor_lock(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    work_root = tmp_path / "restored"
    (work_root / LOCAL_DATABASE_DIR).mkdir(parents=True)
    lock = ProcessLock(work_root / LOCAL_DATABASE_DIR / SUPERVISOR_LOCK_NAME)
    lock.acquire()
    try:
        with pytest.raises(BackupRefused, match="still running"):
            restore(snapshot, work_root)
    finally:
        lock.release()
    assert not (work_root / SOURCE).exists()
    restore(snapshot, work_root)
    assert (work_root / SOURCE / MEDIA / "ingest" / "source.json").is_file()


def test_restore_refuses_a_snapshot_whose_hashes_do_not_agree(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    target = snapshot / ARTEFACTS_DIR / SOURCE / MEDIA / "hints" / "hints.json"
    target.write_bytes(b'{"hints": ["tampered"], "sources": []}')
    assert any("hash differs" in problem for problem in verify_snapshot(snapshot))
    with pytest.raises(BackupRefused, match="does not verify"):
        restore(snapshot, tmp_path / "restored")


# --------------------------------------------------------------------------------------------------
# Paths and sidecars
# --------------------------------------------------------------------------------------------------
def test_no_snapshot_entry_can_escape_the_tree(tmp_path: Path) -> None:
    for hostile in (
        "../escape.json",
        "/etc/passwd",
        "C:/Windows/system32/x.dll",
        "a/../../b.json",
        "sub\\escape.json",
        "",
        " leading.json",
    ):
        with pytest.raises(BackupRefused, match="unsafe snapshot entry path"):
            safe_entry_path(hostile)
    assert safe_entry_path("a/b/c.json") == "a/b/c.json"
    assert contained(tmp_path, "a/b.json") == (tmp_path / "a" / "b.json").resolve()

    snapshot = _snapshot(tmp_path)
    document = json.loads((snapshot / SNAPSHOT_NAME).read_text(encoding="utf-8"))
    document["artefacts"].append(
        {"path": "../escaped.json", "kind": "source", "sha256": "0" * 64, "size": 0}
    )
    (snapshot / SNAPSHOT_NAME).write_bytes(json.dumps(document).encode("utf-8"))
    with pytest.raises(BackupRefused, match="unsafe snapshot entry path"):
        restore(snapshot, tmp_path / "restored")
    assert not (tmp_path / "escaped.json").exists()


def test_a_sidecar_is_judged_by_the_frozen_verifiers_rules(tmp_path: Path) -> None:
    """A missing ordinary upstream is a failure; only an explicit ``pruned_upstream`` marker
    licenses absence, and the schema version is checked."""

    media_dir = tmp_path / "src" / "med"
    media = media_dir / "hints"
    media.mkdir(parents=True)
    artefact = media / "hints.json"
    artefact.write_bytes(b'{"hints": []}\n')
    (media_dir / "recognise").mkdir()
    pcm = media_dir / "recognise" / "shazam.jsonl"
    pcm.write_bytes(b'{"window": 0}\n')
    # Logical upstream keys are media-relative, exactly as the pipeline and retention write them.
    logical = "recognise/shazam.jsonl"
    write_completion_sidecar(artefact, {logical: pcm})
    relative = "src/med/hints/hints.done.json"
    assert backup_module._verify_sidecar(tmp_path, relative) == []
    sidecar = media / "hints.done.json"
    original = sidecar.read_bytes()

    # An upstream no snapshot ever carries (decoded audio) may be absent, but never wrong.
    payload = json.loads(original)
    payload["upstream"] = {"decode/audio.pcm": "b" * 64}
    sidecar.write_bytes(json.dumps(payload).encode("utf-8"))
    assert backup_module._verify_sidecar(tmp_path, relative) == []
    (media_dir / "decode").mkdir()
    (media_dir / "decode" / "audio.pcm").write_bytes(b"\x00")
    assert any(
        "upstream differs" in problem
        for problem in backup_module._verify_sidecar(tmp_path, relative)
    )
    payload["upstream"] = {"../outside/x": "b" * 64}
    sidecar.write_bytes(json.dumps(payload).encode("utf-8"))
    assert any(
        "unsafe upstream" in problem
        for problem in backup_module._verify_sidecar(tmp_path, relative)
    )
    sidecar.write_bytes(original)

    # Retention prunes upstreams on purpose and records that; absence alone does not.
    pcm.unlink()
    problems = backup_module._verify_sidecar(tmp_path, relative)
    assert any("upstream is missing" in problem for problem in problems)

    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    # Exactly the frozen rule: a lowercase 64-hex digest, nothing that merely has the length.
    for malformed in ("G" * 64, "A" * 64, "a" * 63):
        payload["upstream"][logical] = {"pruned_upstream": malformed}
        sidecar.write_bytes(json.dumps(payload).encode("utf-8"))
        assert any(
            "invalid pruned upstream" in problem
            for problem in backup_module._verify_sidecar(tmp_path, relative)
        ), malformed

    payload["upstream"][logical] = {"pruned_upstream": "a" * 64}
    sidecar.write_bytes(json.dumps(payload).encode("utf-8"))
    assert backup_module._verify_sidecar(tmp_path, relative) == []

    payload["schema_version"] = "something-else"
    sidecar.write_bytes(json.dumps(payload).encode("utf-8"))
    assert any(
        "schema_version" in problem for problem in backup_module._verify_sidecar(tmp_path, relative)
    )
    assert SIDECAR_SCHEMA_VERSION  # the frozen constant this matches


def test_verify_artefacts_catches_damage_a_file_listing_would_miss(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    work_root = tmp_path / "restored"
    restore(snapshot, work_root)
    document = backup_module.read_snapshot(snapshot)
    assert verify_artefacts(work_root, document) == []
    bundle = next((work_root / SOURCE / MEDIA / "present" / "bundles").iterdir())
    (bundle / "tracklist.json").write_bytes(b'{"tracks": ["not what the manifest sealed"]}')
    assert any(
        "manifest does not verify" in problem for problem in verify_artefacts(work_root, document)
    )


def test_the_entry_point_verifies_everything_and_exits_nonzero_on_damage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = _snapshot(tmp_path)
    assert backup_module.main(["verify", "--snapshot", str(snapshot)]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "errors": []}

    bundle = next((snapshot / ARTEFACTS_DIR / SOURCE / MEDIA / "present" / "bundles").iterdir())
    (bundle / "tracklist.json").write_bytes(b'{"tracks": ["not what the manifest sealed"]}')
    seal(snapshot, created_at=CREATED, work_root=Path(FIXTURE_WORK_ROOT), **describe(snapshot))
    assert verify_snapshot(snapshot) == []
    assert backup_module.main(["verify", "--snapshot", str(snapshot)]) == 1
    reported = json.loads(capsys.readouterr().out)
    assert reported["ok"] is False


def test_a_snapshot_that_lists_one_path_twice_is_refused(tmp_path: Path) -> None:
    """Two bytes for one target would let the second silently replace the first."""

    snapshot = _snapshot(tmp_path)
    document = json.loads((snapshot / SNAPSHOT_NAME).read_text(encoding="utf-8"))
    document["artefacts"].append(dict(document["artefacts"][0]))
    (snapshot / SNAPSHOT_NAME).write_bytes(json.dumps(document).encode("utf-8"))
    assert any("twice" in problem for problem in verify_snapshot(snapshot))
    with pytest.raises(BackupRefused, match="twice"):
        restore(snapshot, tmp_path / "restored")
    assert not (tmp_path / "restored" / SOURCE).exists()


# --------------------------------------------------------------------------------------------------
# A stable capture: nothing dropped silently, everything flushed
# --------------------------------------------------------------------------------------------------
def _journal_of(work_root: Path) -> Path:
    return work_root / SOURCE / MEDIA / "recognise" / "shazam.jsonl"


def test_a_member_that_changes_after_the_lock_is_released_is_captured_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan can append to a journal the moment the lock is released. Each copy is checked
    against the identity recorded under the lock; a change means the medium is taken again."""

    work_root, database, _bundle = _work_root(tmp_path)
    journal = _journal_of(work_root)
    real = backup_module._copy_view
    calls = {"n": 0}

    def appended_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            with open(journal, "ab") as handle:
                handle.write(b'{"window": 7, "outcome": "match"}\n')
        return real(*args, **kwargs)

    monkeypatch.setattr(backup_module, "_copy_view", appended_once)
    destination = tmp_path / "snapshot"
    document = backup(database, work_root, destination, now=CREATED)
    assert calls["n"] == 2
    entry = next(item for item in document["artefacts"] if item["path"].endswith("shazam.jsonl"))
    assert entry["sha256"] == sha256_file(journal)  # the bytes that are really there now
    assert verify_snapshot(destination) == []


def test_a_member_that_keeps_changing_refuses_the_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_root, database, _bundle = _work_root(tmp_path)
    journal = _journal_of(work_root)
    real = backup_module._copy_view

    def always(*args, **kwargs):
        with open(journal, "ab") as handle:
            handle.write(b'{"window": 8}\n')
        return real(*args, **kwargs)

    monkeypatch.setattr(backup_module, "_copy_view", always)
    destination = tmp_path / "snapshot"
    with pytest.raises(BackupRefused, match="changed while it was being captured"):
        backup(database, work_root, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".snapshot.staging-*"))


def test_a_member_that_vanishes_and_comes_back_is_captured_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's scenario: a silent omission would verify, and a later restore would then
    delete the owner's surviving copy. A vanished member triggers a fresh capture instead."""

    work_root, database, _bundle = _work_root(tmp_path)
    journal = _journal_of(work_root)
    saved = journal.read_bytes()
    real_copy = backup_module._copy_view
    real_discard = backup_module._discard
    calls = {"n": 0}

    def vanish_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            journal.unlink()  # moved aside after it was listed
        return real_copy(*args, **kwargs)

    def discard_then_it_returns(paths):
        real_discard(paths)
        journal.write_bytes(saved)  # ... and back before the medium is taken again

    monkeypatch.setattr(backup_module, "_copy_view", vanish_once)
    monkeypatch.setattr(backup_module, "_discard", discard_then_it_returns)
    destination = tmp_path / "snapshot"
    document = backup(database, work_root, destination, now=CREATED)
    assert calls["n"] == 2
    assert any(item["path"].endswith("recognise/shazam.jsonl") for item in document["artefacts"])
    assert verify(destination) == []


def test_a_copy_that_fails_refuses_the_backup_rather_than_skipping_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_root, database, _bundle = _work_root(tmp_path)
    real = shutil.copy2

    def locked(source, destination, *args, **kwargs):
        if str(source).endswith("shazam.jsonl"):
            raise PermissionError("the file is locked by another program")
        return real(source, destination, *args, **kwargs)

    monkeypatch.setattr(backup_module.shutil, "copy2", locked)
    with pytest.raises(BackupRefused, match="could not capture"):
        backup(database, work_root, tmp_path / "snapshot")
    monkeypatch.undo()
    assert not (tmp_path / "snapshot").exists()


def test_every_copied_file_and_every_created_directory_is_flushed_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not only the leaf directories: every directory the backup created, and every copied file.
    (The rename itself is not the boundary; the bytes and entries beneath it have to be durable.)"""

    work_root, database, _bundle = _work_root(tmp_path)
    flushed_dirs: list[str] = []
    flushed_files: list[str] = []
    real_dir, real_file = backup_module.flush_directory, backup_module.fsync_file

    def plain(path: object) -> str:
        return str(path).removeprefix("\\\\?\\").replace("\\", "/")

    def record_dir(path):
        flushed_dirs.append(plain(path))
        real_dir(path)

    def record_file(path):
        flushed_files.append(plain(path))
        real_file(path)

    monkeypatch.setattr(backup_module, "flush_directory", record_dir)
    monkeypatch.setattr(backup_module, "fsync_file", record_file)
    destination = tmp_path / "snapshot"
    backup(database, work_root, destination, now=CREATED)

    def inside_staging(paths: list[str]) -> set[str]:
        found: set[str] = set()
        for text in paths:
            if ".snapshot.staging-" in text:
                tail = text.split(".snapshot.staging-", 1)[1]
                found.add(tail.split("/", 1)[1] if "/" in tail else "")
        return found

    directories = {
        path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_dir()
    } | {""}
    missing = directories - inside_staging(flushed_dirs)
    assert not missing, missing
    copied = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file() and path.stat().st_nlink == 1 and path.name != SNAPSHOT_NAME
    }
    unflushed = copied - inside_staging(flushed_files)
    assert not unflushed, unflushed


def test_a_backup_carries_the_flat_present_files_that_a_restore_owns(tmp_path: Path) -> None:
    work_root, database, bundle = _work_root(tmp_path)
    pointer = work_root / SOURCE / MEDIA / "present" / "current"
    pointer.write_bytes(bundle.name.encode("ascii"))
    destination = tmp_path / "snapshot"
    document = backup(database, work_root, destination, now=CREATED)
    listed = {item["path"]: item for item in document["artefacts"]}
    assert listed[f"{SOURCE}/{MEDIA}/present/current"]["kind"] == "present"
    elsewhere = tmp_path / "elsewhere"
    restore(destination, elsewhere)
    assert (elsewhere / SOURCE / MEDIA / "present" / "current").read_bytes() == pointer.read_bytes()


def test_restore_replaces_the_whole_owned_footprint_not_only_bundles(tmp_path: Path) -> None:
    """A medium absent from the snapshot keeps no ingest record, no current pointer and no legacy
    flat result — while the owner's original audio, which is never snapshotted, is left alone."""

    snapshot = _snapshot(tmp_path)
    work_root = tmp_path / "restored"
    newer = work_root / SOURCE / "newer-media"
    (newer / "ingest").mkdir(parents=True)
    (newer / "ingest" / "source.json").write_bytes(b'{"newer": true}')
    (newer / "ingest" / "original.m4a").write_bytes(b"the owner's audio")
    (newer / "present").mkdir()
    (newer / "present" / "current").write_bytes(b"b" * 64)
    (newer / "present" / "index.html").write_bytes(b"<p>a legacy flat result</p>")
    restored_present = work_root / SOURCE / MEDIA / "present"
    restored_present.mkdir(parents=True)
    (restored_present / "current").write_bytes(b"c" * 64)

    report = restore(snapshot, work_root)
    assert report["removed"] >= 4
    for gone in (
        newer / "ingest" / "source.json",
        newer / "present" / "current",
        newer / "present" / "index.html",
        restored_present / "current",
    ):
        assert not gone.exists(), gone
    assert (newer / "ingest" / "original.m4a").read_bytes() == b"the owner's audio"


def test_a_member_that_vanishes_and_stays_gone_leaves_its_whole_medium_out_with_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 5: the retry sees a smaller medium. Without membership carried across attempts, the
    snapshot would seal the medium minus its journal, verify, and a restore would then delete the
    owner's surviving copy. The whole medium is left out instead, and the snapshot says why."""

    work_root, database, _bundle = _work_root(tmp_path)
    journal = _journal_of(work_root)
    real_copy = backup_module._copy_view
    calls = {"n": 0}

    def vanish_for_good(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            journal.unlink()  # moved aside after it was listed, and never coming back
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(backup_module, "_copy_view", vanish_for_good)
    destination = tmp_path / "snapshot"
    document = backup(database, work_root, destination, now=CREATED)
    assert not journal.exists()
    assert calls["n"] == 1  # the second view was refused before anything was copied
    reasons = [item for item in document["skipped"] if item["media"] == f"{SOURCE}/{MEDIA}"]
    assert reasons and "vanished" in reasons[0]["reason"], document["skipped"]
    assert "recognise/shazam.jsonl" in reasons[0]["reason"]
    assert not [
        item for item in document["artefacts"] if item["path"].startswith(f"{SOURCE}/{MEDIA}/")
    ]
    assert verify(destination) == []


# --------------------------------------------------------------------------------------------------
# Round 5: no server while a restore is live
# --------------------------------------------------------------------------------------------------
_PAUSED_CHILD = """
import sys, time
from pathlib import Path
sys.path.insert(0, {repo!r})
from idea_web import backup as backup_module

snapshot, work_root, paused, go = (Path(value) for value in sys.argv[1:5])
real = backup_module._rename

def barrier(source, destination, guard):
    real(source, destination, guard)
    if backup_module.REPLACED_DIR not in Path(destination).parts and not paused.exists():
        paused.write_bytes(b"published one file")  # mid-publication, locks held
        while not go.exists():
            time.sleep(0.05)

backup_module._rename = barrier
backup_module.restore(snapshot, work_root)
print("restored")
"""


def _tree_state(root: Path) -> dict[str, tuple[int, int]]:
    return {
        path.relative_to(root).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_no_server_can_open_or_serve_a_work_root_while_a_restore_is_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real restore in a child process, paused by a barrier after its first publication.
    `idea serve`'s own entry points — `local_database()`, `LocalJobs(work_root)`, and the
    supervisor's `start()` — must all refuse, and write nothing at all."""

    snapshot = _snapshot(tmp_path)
    work_root = tmp_path / "restored"
    script = tmp_path / "paused_child.py"
    repo = str(Path(__file__).resolve().parents[2] / "src")
    script.write_text(_PAUSED_CHILD.format(repo=repo), encoding="utf-8")
    paused, go = tmp_path / "paused.flag", tmp_path / "go.flag"
    child = subprocess.Popen(
        [sys.executable, str(script), str(snapshot), str(work_root), str(paused), str(go)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    try:
        deadline = time.monotonic() + 60
        while not paused.exists() and time.monotonic() < deadline:
            if child.poll() is not None:
                _out, err = child.communicate()
                raise AssertionError(f"child exited early: {err.decode('utf-8', 'replace')}")
            time.sleep(0.05)
        assert paused.exists(), "the restore never reached publication"
        assert _staging_trees(work_root), "the restore's journal is its durable marker"
        before = _tree_state(work_root)
        database_path = work_root / LOCAL_DATABASE_DIR / DATABASE_NAME
        assert not database_path.exists()

        refusal = "a restore is in progress; wait for it to finish"
        with pytest.raises(MigrationRefused, match=refusal):
            local_database(work_root)
        with pytest.raises(MigrationRefused, match=refusal):
            LocalJobs(work_root)
        elsewhere = Database(tmp_path / "elsewhere" / DATABASE_NAME)
        elsewhere.migrate()
        supervisor = LocalWorkerSupervisor(
            work_root,
            config_path=work_root / "idea.toml",
            jobs=LocalJobs(work_root, database=elsewhere),
            runner_spec=f"{FAKES}:slow_runner",
        )
        with pytest.raises(MigrationRefused, match=refusal):
            supervisor.start()

        assert not database_path.exists()  # nothing was opened, created or migrated
        assert _tree_state(work_root) == before  # and nothing at all was written
        assert child.poll() is None  # the restore is still alive and owns the root
    finally:
        go.write_bytes(b"go")
        out, err = child.communicate(timeout=120)
    assert child.returncode == 0, err.decode("utf-8", "replace")
    assert b"restored" in out
    assert verify_artefacts(work_root, backup_module.read_snapshot(snapshot)) == []
    local_database(work_root)  # once the restore is over, the root opens normally


# --------------------------------------------------------------------------------------------------
# Round 5: durable directory boundaries
# --------------------------------------------------------------------------------------------------
def _plain_text(path: object) -> str:
    return str(path).removeprefix("\\\\?\\").replace("\\", "/").rstrip("/").casefold()


def test_a_snapshot_is_published_by_a_write_through_move_and_a_flushed_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_root, database, _bundle = _work_root(tmp_path)
    events: list[tuple[str, str]] = []
    real_move, real_flush = durable.move_directory, durable.flush_directory

    def moved(source, destination):
        events.append(("move", _plain_text(destination)))
        real_move(source, destination)

    def flushed(path):
        events.append(("flush", _plain_text(path)))
        real_flush(path)

    def plain_rename(*args, **kwargs):
        raise AssertionError("a plain rename published the snapshot")

    monkeypatch.setattr(backup_module, "move_directory", moved)
    monkeypatch.setattr(durable, "flush_directory", flushed)
    monkeypatch.setattr(backup_module, "flush_directory", flushed)
    if sys.platform == "win32":  # io.durable_replace never renames through os on Windows
        monkeypatch.setattr(backup_module.os, "replace", plain_rename)
        monkeypatch.setattr(backup_module.os, "rename", plain_rename)
    destination = tmp_path / "snapshot"
    backup(database, work_root, destination, now=CREATED)
    monkeypatch.undo()

    target = _plain_text(destination.resolve())
    assert ("move", target) in events, events
    after = events[events.index(("move", target)) + 1 :]
    assert ("flush", _plain_text(destination.resolve().parent)) in after
    assert verify(destination) == []


def test_every_recovery_deletion_is_flushed_and_the_journal_is_closed_before_it_goes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_root = tmp_path / "restored"
    media = work_root / SOURCE / MEDIA
    fresh = [media / "hints" / "orphan.json", media / "recognise" / "fresh.jsonl"]
    for path in fresh:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{}")
    database_path = work_root / LOCAL_DATABASE_DIR / DATABASE_NAME
    database_path.parent.mkdir(parents=True)
    database_path.write_bytes(b"published by the dead restore")
    _abandon(
        work_root,
        targets=[(path.relative_to(work_root).as_posix(), False) for path in fresh],
        kept={},
        database_existed=False,
    )

    events: list[tuple[str, str]] = []
    real_remove, real_flush = backup_module.os.remove, durable.flush_directory
    real_rmtree = backup_module.shutil.rmtree
    states: list[str] = []

    def removed(path, *args, **kwargs):
        events.append(("remove", _plain_text(path)))
        real_remove(path, *args, **kwargs)

    def flushed(path):
        events.append(("flush", _plain_text(path)))
        real_flush(path)

    def rmtree(path, *args, **kwargs):
        journal = Path(str(path)) / JOURNAL_NAME
        states.append(json.loads(journal.read_text(encoding="utf-8"))["state"])
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(backup_module.os, "remove", removed)
    monkeypatch.setattr(durable, "flush_directory", flushed)
    monkeypatch.setattr(backup_module, "flush_directory", flushed)
    monkeypatch.setattr(backup_module.shutil, "rmtree", rmtree)
    recover_interrupted(work_root)
    monkeypatch.undo()

    for path in [*fresh, database_path]:
        assert not path.exists(), path
        index = events.index(("remove", _plain_text(path.resolve())))
        assert events[index + 1] == ("flush", _plain_text(path.resolve().parent)), events
    assert states == ["rolled-back"]
    assert not _staging_trees(work_root)


# --------------------------------------------------------------------------------------------------
# Round 5: the sidecar exception is exact, with production sidecar shapes
# --------------------------------------------------------------------------------------------------
def _hint_sidecars(media: Path, cache: Path, *, job: str = "job-1") -> tuple[Path, str]:
    """`hints/pipeline.py`'s shape: ``ingest/source.json`` plus ``local-cache/<c>/<job>/…``."""

    result = cache / "web" / "source-key" / job / "result.json"
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_bytes(b'{"items": ["a hint"]}')
    hints_path = media / "hints" / "hints.jsonl"
    status_path = media / "hints" / "status.json"
    hints_path.parent.mkdir(parents=True, exist_ok=True)
    # The fixture's older `hints.json` shares the sidecar name; production writes only `.jsonl`.
    (media / "hints" / "hints.json").unlink(missing_ok=True)
    hints_path.write_bytes(b'{"hint": 1}\n')
    status_path.write_bytes(b'{"connectors": []}')
    logical = f"local-cache/web/{job}/result.json"
    upstream = {"ingest/source.json": media / "ingest" / "source.json", logical: result}
    write_completion_sidecar(hints_path, upstream)
    write_completion_sidecar(status_path, upstream)
    return result, logical


def test_hint_cache_evidence_is_sealed_into_the_snapshot_and_its_absence_fails(
    tmp_path: Path,
) -> None:
    work_root, database, _bundle = _work_root(tmp_path)
    media = work_root / SOURCE / MEDIA
    cache = tmp_path / "project" / "data" / "local" / "hints"
    result, logical = _hint_sidecars(media, cache)
    relative = f"{SOURCE}/{MEDIA}/hints/hints.done.json"

    # In the live tree the evidence is not beside the medium: the missing upstream fails.
    problems = backup_module._verify_sidecar(work_root, relative)
    assert any("upstream is missing" in problem and logical in problem for problem in problems)

    # A backup seals the evidence at exactly its media-relative key, so the snapshot verifies.
    destination = tmp_path / "snapshot"
    document = backup(database, work_root, destination, now=CREATED, hint_cache=cache)
    listed = {item["path"]: item for item in document["artefacts"]}
    sealed = listed[f"{SOURCE}/{MEDIA}/{logical}"]
    assert sealed["kind"] == "evidence" and sealed["sha256"] == sha256_file(result)
    assert verify(destination) == []
    elsewhere = tmp_path / "elsewhere"
    restore(destination, elsewhere)
    assert verify_artefacts(elsewhere, backup_module.read_snapshot(destination)) == []

    # Evidence removed from the sealed snapshot is a failure, not an allowed absence.
    (destination / ARTEFACTS_DIR / SOURCE / MEDIA / logical).unlink()
    assert any(logical in problem for problem in verify(destination))

    # Evidence that cannot be found (or whose bytes differ) leaves the medium out, with a reason.
    result.write_bytes(b'{"items": ["changed since"]}')
    document = backup(database, work_root, tmp_path / "second", now=CREATED, hint_cache=cache)
    reasons = [item["reason"] for item in document["skipped"] if item["media"].endswith(MEDIA)]
    assert any("hint evidence" in reason and logical in reason for reason in reasons), reasons
    assert verify(tmp_path / "second") == []


def test_only_decoded_audio_windows_and_original_media_may_be_missing(tmp_path: Path) -> None:
    """`recognise.py`'s queries sidecar: a windows record (may be absent) and
    ``provider_configs/<name>``, whose bytes sit beside the invocation and must match."""

    media = tmp_path / SOURCE / MEDIA
    invocation = media / "recognise" / "invocations" / "0123456789abcdef0123"
    invocation.mkdir(parents=True)
    config = invocation / "shazam-v3.json"
    config.write_bytes(b'{"version": 3}')
    windows_record = media / "windows" / "windows.gen0.jsonl"
    windows_record.parent.mkdir(parents=True)
    windows_record.write_bytes(b'{"window": 0}\n')
    queries = invocation / "queries.gen0.jsonl"
    queries.write_bytes(b'{"query": 0}\n')
    write_completion_sidecar(
        queries,
        {
            "windows/windows.gen0.jsonl": windows_record,
            "provider_configs/shazam-v3.json": config,
        },
    )
    sidecar = invocation / "queries.gen0.done.json"
    relative = sidecar.relative_to(tmp_path).as_posix()
    check = backup_module._verify_sidecar
    assert check(tmp_path, relative) == []

    windows_record.unlink()  # analysis windows are never snapshotted
    assert check(tmp_path, relative) == []

    config.write_bytes(b'{"version": "tampered"}')
    assert any("upstream differs" in problem for problem in check(tmp_path, relative))
    config.unlink()
    assert any(
        "upstream is missing" in problem and "provider_configs" in problem
        for problem in check(tmp_path, relative)
    )
    config.write_bytes(b'{"version": 3}')
    assert check(tmp_path, relative) == []

    original = json.loads(sidecar.read_text(encoding="utf-8"))

    def judged(upstream: dict) -> list[str]:
        sidecar.write_bytes(json.dumps({**original, "upstream": upstream}).encode("utf-8"))
        return check(tmp_path, relative)

    digest = "a" * 64
    # Exactly the allowed spellings may be absent ...
    for allowed in (
        "decode/audio.pcm",
        "decode/pcm.json",
        "windows/windows.gen2.jsonl",
        "windows/gen1/0000001000-000012000-plain.wav",
        "ingest/original.m4a",
    ):
        assert judged({allowed: digest}) == [], allowed
    # ... and nothing that merely resembles them, nor any other pipeline output.
    for required in (
        "decode/audio.pcm.bak",
        "windows/extra/record.jsonl",
        "ingest/source.json",
        "fuse/episodes.json",
        "enrich/acquire.json",
        "local-cache/web/job-9/result.json",
        "provider_configs/missing.json",
        "somewhere/else.json",
    ):
        problems = judged({required: digest})
        assert any("upstream is missing" in problem for problem in problems), required
    # A malformed digest fails even where absence would be allowed.
    for malformed in ("A" * 64, "g" * 64, "a" * 63, 7, None, {"pruned": digest}):
        problems = judged({"windows/windows.gen0.jsonl": malformed})
        assert any("invalid" in problem for problem in problems), malformed
    # A pruned marker is still satisfied by absence.
    assert judged({"fuse/episodes.json": {"pruned_upstream": digest}}) == []


def test_sidecars_inside_sealed_directories_are_proven_by_their_manifest(tmp_path: Path) -> None:
    """The frozen retention's rule: a frozen run's copied sidecars keep their recorded upstream
    hashes as evidence, even after the live files they name have moved on."""

    relative = f"{SOURCE}/{MEDIA}/fuse/runs/{RUN}/episodes.gen0.done.json"
    sidecar = tmp_path / relative
    sidecar.parent.mkdir(parents=True)
    sidecar.write_bytes(b'{"schema_version": "x", "upstream": {"hints/hints.jsonl": "b"}}')
    assert backup_module._verify_sidecar(tmp_path, relative) == []
    live = f"{SOURCE}/{MEDIA}/fuse/episodes.gen0.done.json"
    (tmp_path / live).write_bytes(sidecar.read_bytes())
    assert backup_module._verify_sidecar(tmp_path, live)


# --------------------------------------------------------------------------------------------------
# A restore never destroys what the snapshot knew about but did not hold
# --------------------------------------------------------------------------------------------------
def _bytes_under(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_a_restore_preserves_skipped_media_restores_captured_ones_and_removes_the_rest(
    tmp_path: Path,
) -> None:
    work_root, database, _bundle = _work_root(tmp_path)
    busy = _add_medium(work_root, database, media_key="busy-media", run_id="busy-run")
    captured = work_root / SOURCE / MEDIA
    journal = _journal_of(work_root)
    snapshotted_journal = journal.read_bytes()

    destination = tmp_path / "snapshot"
    with _Holder(busy):
        document = backup(database, work_root, destination, now=CREATED)
    busy_relative = f"{SOURCE}/busy-media"
    assert busy_relative in {item["media"] for item in document["skipped"]}
    assert not [p for p in document["artefacts"] if p["path"].startswith(f"{busy_relative}/")]

    # After the backup: the busy medium moves on, the captured one changes, a new one appears.
    (busy / "recognise" / "attempts.jsonl").write_bytes(b'{"event": "settled"}\n')
    (busy / "hints").mkdir()
    (busy / "hints" / "hints.jsonl").write_bytes(b'{"hint": "newer"}\n')
    (busy / "present" / "current").write_bytes(b"c" * 64)
    journal.write_bytes(b'{"window": 99}\n')
    unmentioned = work_root / SOURCE / "unmentioned-media"
    (unmentioned / "ingest").mkdir(parents=True)
    (unmentioned / "ingest" / "source.json").write_bytes(b'{"after": "the backup"}')
    before = _bytes_under(busy)

    report = restore(destination, work_root)

    assert _bytes_under(busy) == before  # byte-identical: nothing removed, nothing replaced
    assert journal.read_bytes() == snapshotted_journal  # the captured medium is restored
    assert (captured / "ingest" / "source.json").is_file()
    assert not (unmentioned / "ingest" / "source.json").exists()  # post-backup state removed
    assert f"{SOURCE}/unmentioned-media/ingest/source.json" in report["removed_paths"]
    assert not [path for path in report["removed_paths"] if path.startswith(f"{busy_relative}/")]
    note = report["preserved"][busy_relative]
    assert note.startswith("preserved: not in this snapshot (") and "media is active" in note
    assert f"{SOURCE}/{MEDIA}" not in report["preserved"]


# --------------------------------------------------------------------------------------------------
# Round 6: startup holds restore exclusion until migration has returned
# --------------------------------------------------------------------------------------------------
_WAITING_CHILD = """
import sys, time
from pathlib import Path
sys.path.insert(0, {repo!r})
from idea_web import backup as backup_module

snapshot, work_root, refused, migrated, verdict = (Path(value) for value in sys.argv[1:6])
real = backup_module._rename
first = []

def watched(source, destination, guard):
    if not first:
        first.append(True)
        # The restore owns the root and is about to publish: startup must be finished by now.
        verdict.write_text("after" if migrated.exists() else "OVERLAP", encoding="utf-8")
    real(source, destination, guard)

backup_module._rename = watched
deadline = time.monotonic() + 90
while True:
    try:
        backup_module.restore(snapshot, work_root)
        break
    except backup_module.BackupRefused as exc:
        if "starting up" not in str(exc) or time.monotonic() > deadline:
            raise
        refused.write_text(str(exc), encoding="utf-8")
        time.sleep(0.05)
print("restored")
"""


def test_a_restore_cannot_start_while_idea_serve_is_opening_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inverse race: startup has passed its restore check and is about to create, open and
    migrate the database. A restore started in exactly that window is refused until migration
    has returned, so SQLite is never opened by both."""

    snapshot = _snapshot(tmp_path)
    work_root = tmp_path / "restored"
    (work_root / LOCAL_DATABASE_DIR).mkdir(parents=True)
    refused, migrated, verdict = (
        tmp_path / "refused.flag",
        tmp_path / "migrated.flag",
        tmp_path / "verdict.txt",
    )
    script = tmp_path / "waiting_child.py"
    repo = str(Path(__file__).resolve().parents[2] / "src")
    script.write_text(_WAITING_CHILD.format(repo=repo), encoding="utf-8")
    started: list[subprocess.Popen] = []

    class HeldAtTheBarrier(Database):
        def migrate(self, target=None):
            # Inside startup's window, before SQLite is touched: start a restore and watch it
            # be turned away.
            child = subprocess.Popen(
                [sys.executable, str(script), str(snapshot), str(work_root)]
                + [str(refused), str(migrated), str(verdict)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            started.append(child)
            deadline = time.monotonic() + 60
            while not refused.exists() and time.monotonic() < deadline:
                if child.poll() is not None:
                    _out, err = child.communicate()
                    raise AssertionError(f"restore was not refused: {err.decode()}")
                time.sleep(0.05)
            assert refused.exists(), "the restore was never refused"
            assert not verdict.exists() and not _staging_trees(work_root)
            assert not Path(self.path).exists()  # nothing has opened SQLite yet
            result = super().migrate(target)  # now the database is created and opened
            migrated.write_bytes(b"migration returned")
            return result

    monkeypatch.setattr(local_module, "Database", HeldAtTheBarrier)
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    try:
        database = local_database(work_root)
        assert Path(database.path).is_file()
    finally:
        for child in started:
            out, err = child.communicate(timeout=120)
    assert started, "the barrier never ran"
    assert started[0].returncode == 0, err.decode("utf-8", "replace")
    assert b"restored" in out
    assert verdict.read_text(encoding="utf-8") == "after"


# --------------------------------------------------------------------------------------------------
# Round 6: a skipped run inside a captured medium keeps its own subtree
# --------------------------------------------------------------------------------------------------
def test_a_restore_preserves_a_skipped_run_inside_a_captured_medium(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_root, database, _bundle = _work_root(tmp_path)
    media = work_root / SOURCE / MEDIA
    in_flight = "in-flight-run"
    with database.write() as connection:
        connection.execute(
            "INSERT INTO analysis_runs(run_id, analysis_key, media_key, requested_recipe_id, "
            "algorithm_version, adapter_versions, achieved, status, tenant_scope, attempts, "
            "checkpoints, usd_e6_reserved, usd_e6_spent, started_at, finished_at, "
            "analysis_inputs, non_recipe_key) VALUES (?, ?, ?, ?, '1', '{}', 'free', 'running', "
            "'public', 0, '{}', 0, 0, ?, NULL, '{}', ?)",
            (in_flight, "d" * 64, MEDIA, "e" * 64, 1000.0, "f" * 64),
        )
    run_tree = media / "fuse" / "runs" / in_flight
    run_tree.mkdir(parents=True)
    (run_tree / "episodes.json").write_bytes(b'{"episodes": ["frozen so far"]}')
    _seal_directory(run_tree, {"run_id": in_flight})
    owned_bundle = media / "present" / "bundles" / bundle_id(in_flight, 1)
    owned_bundle.mkdir(parents=True)
    (owned_bundle / "index.html").write_bytes(b"<p>being committed</p>")
    _seal_directory(owned_bundle, {"run_id": in_flight, "presentation_version": 1})

    destination = tmp_path / "snapshot"
    document = backup(database, work_root, destination, now=CREATED)
    notes = [item for item in document["skipped"] if in_flight in item["runs"]]
    assert notes and "in flight" in notes[0]["reason"]
    listed = [item["path"] for item in document["artefacts"]]
    assert any(path.startswith(f"{SOURCE}/{MEDIA}/") for path in listed)  # medium captured
    assert not [path for path in listed if in_flight in path or owned_bundle.name in path]

    # The run moves on after the backup; the captured parts change too.
    (run_tree / "episodes.json").write_bytes(b'{"episodes": ["finished later"]}')
    (run_tree / "presentation-identities.json").write_bytes(b"{}")
    (owned_bundle / "tracklist.json").write_bytes(b'{"tracks": []}')
    journal = _journal_of(work_root)
    snapshotted = journal.read_bytes()
    journal.write_bytes(b'{"window": 42}\n')
    before = {"run": _bytes_under(run_tree), "bundle": _bytes_under(owned_bundle)}

    journals: list[dict] = []
    real_write = backup_module._write_json

    def recorded(path, document, guard=None):
        if document.get("state") == "publishing":
            journals.append(document)
        real_write(path, document, guard)

    monkeypatch.setattr(backup_module, "_write_json", recorded)
    report = restore(destination, work_root)
    monkeypatch.undo()

    assert {"run": _bytes_under(run_tree), "bundle": _bytes_under(owned_bundle)} == before
    assert journal.read_bytes() == snapshotted  # the captured parts are restored
    run_key = f"{SOURCE}/{MEDIA}/fuse/runs/{in_flight}"
    bundle_key = f"{SOURCE}/{MEDIA}/present/bundles/{owned_bundle.name}"
    for key in (run_key, bundle_key):
        assert report["preserved"][key].startswith("preserved: not in this snapshot ("), key
        assert journals and journals[0]["preserved"][key] == report["preserved"][key]
    assert f"run {in_flight}" in report["preserved"][run_key]
    assert f"{SOURCE}/{MEDIA}" not in report["preserved"]
    assert not [path for path in report["removed_paths"] if in_flight in path]

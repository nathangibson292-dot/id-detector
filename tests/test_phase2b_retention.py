"""Cycle 2b: recoverable retention, pruned sidecars and on-demand re-derivation."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from typer.testing import CliRunner

from id_detector import cli, pipeline, retention
from id_detector.io import (
    atomic_write_json,
    native_path,
    read_bytes,
    read_text,
    verify_completion_sidecar,
    write_completion_sidecar,
)
from id_detector.jobs import JobStoreLocked
from id_detector.present.bundles import read_bundle_manifest, read_manifest
from id_detector.retention import FAILED_STATUSES, SUCCESS_STATUSES, collect
from scripts.make_audio_fixtures import FIXTURE_ROOT, generate
from tests.test_phase1a_bundles import publish, seed
from tests.test_phase1a_compat import run

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


def _seed_media(root: Path, status: str, *, age: timedelta, keep: bool = False) -> Path:
    media_key = sha256(f"{status}-{age}-{keep}".encode()).hexdigest()
    media = root / sha256(("source-" + media_key).encode()).hexdigest() / media_key
    (media / "ingest").mkdir(parents=True)
    (media / "decode").mkdir()
    (media / "windows").mkdir()
    original = media / "ingest/original.wav"
    original.write_bytes(b"original")
    source = media / "ingest/source.json"
    atomic_write_json(
        source,
        {"media_key": media_key, "original": {"path": "ingest/original.wav"}},
    )
    write_completion_sidecar(source, {"ingest/original.wav": original})
    pcm = media / "decode/audio.pcm"
    pcm.write_bytes(b"pcm")
    record = media / "decode/pcm.json"
    atomic_write_json(record, {"pcm": True})
    write_completion_sidecar(
        record,
        {"ingest/source.json": source, "ingest/original.wav": original},
    )
    windows = media / "windows/windows.gen0.jsonl"
    windows.write_text("{}\n", encoding="utf-8")
    write_completion_sidecar(
        windows,
        {"decode/pcm.json": record, "decode/audio.pcm": pcm},
    )
    downstream = media / "recognise/observations.jsonl"
    downstream.parent.mkdir()
    downstream.write_text("{}\n", encoding="utf-8")
    write_completion_sidecar(downstream, {"windows/windows.gen0.jsonl": windows})
    finished = NOW - age
    atomic_write_json(
        media / "entry.json",
        {"fixture": True},
    )
    journal = {
        "invocation_id": "run",
        "status": status,
        "started_at": (finished - timedelta(minutes=1)).isoformat(),
        "finished_at": finished.isoformat(),
        "keep_intermediates": keep,
    }
    (media / "invocations.jsonl").write_text(json.dumps(journal) + "\n", encoding="utf-8")
    return media


@pytest.mark.parametrize("status", sorted(SUCCESS_STATUSES))
def test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d(
    tmp_path: Path, status: str
) -> None:
    fresh = _seed_media(tmp_path / "fresh", status, age=timedelta(hours=1))
    collect(tmp_path / "fresh", policy="hosted", apply=True, now=NOW)
    assert not (fresh / "windows").exists()
    assert (fresh / "decode/audio.pcm").is_file()
    assert (fresh / "ingest/original.wav").is_file()

    pcm_old = _seed_media(tmp_path / "pcm-old", status, age=timedelta(days=3))
    collect(tmp_path / "pcm-old", policy="hosted", apply=True, now=NOW)
    assert not (pcm_old / "decode/audio.pcm").exists()
    assert (pcm_old / "ingest/original.wav").is_file()

    hosted = _seed_media(tmp_path / "hosted", status, age=timedelta(days=8))
    collect(tmp_path / "hosted", policy="hosted", apply=True, now=NOW)
    assert not (hosted / "ingest/original.wav").exists()
    assert (hosted / "retention-manifest.json").is_file()

    local = _seed_media(tmp_path / "local", status, age=timedelta(days=8))
    collect(tmp_path / "local", policy="local", apply=True, now=NOW)
    assert (local / "ingest/original.wav").is_file()


@pytest.mark.parametrize("status", sorted(FAILED_STATUSES))
def test_failure_statuses_prune_windows_pcm_now_and_original_at_24h(
    tmp_path: Path, status: str
) -> None:
    fresh = _seed_media(tmp_path / "fresh", status, age=timedelta(hours=1))
    collect(tmp_path / "fresh", policy="local", apply=True, now=NOW)
    assert not (fresh / "windows").exists()
    assert not (fresh / "decode/audio.pcm").exists()
    assert (fresh / "ingest/original.wav").is_file()

    old = _seed_media(tmp_path / "old", status, age=timedelta(days=2))
    collect(tmp_path / "old", policy="local", apply=True, now=NOW)
    assert old.is_dir()
    assert not (old / "ingest/original.wav").exists()


def test_unreferenced_failed_media_moves_whole_directory_after_7d(tmp_path: Path) -> None:
    work = tmp_path / "work"
    media = _seed_media(work, "failed", age=timedelta(days=8))
    collect(work, policy="local", apply=True, now=NOW)
    assert not media.exists()
    assert (work / ".trash/2026-09-11" / media.relative_to(work)).is_dir()


def test_dry_run_changes_nothing_and_cli_requires_apply(tmp_path: Path) -> None:
    work = tmp_path / "work"
    media = _seed_media(work, "complete", age=timedelta(hours=1))
    before = {
        path.relative_to(work): read_bytes(path) for path in work.rglob("*") if path.is_file()
    }
    result = CliRunner().invoke(cli.app, ["gc", "--policy", "local", "--work-root", str(work)])
    assert result.exit_code == 0 and "gc dry run" in result.output and "would move" in result.output
    after = {path.relative_to(work): read_bytes(path) for path in work.rglob("*") if path.is_file()}
    assert after == before
    result = CliRunner().invoke(
        cli.app, ["gc", "--policy", "local", "--work-root", str(work), "--apply"]
    )
    assert result.exit_code == 0 and "gc applied" in result.output
    assert not (media / "windows").exists()


def test_pruned_upstream_is_verified_and_replaced_after_rederivation(tmp_path: Path) -> None:
    work = tmp_path / "work"
    media = _seed_media(work, "complete", age=timedelta(hours=1))
    artifact = media / "recognise/observations.jsonl"
    upstream = {"windows/windows.gen0.jsonl": media / "windows/windows.gen0.jsonl"}
    original_hash = sha256(read_bytes(upstream["windows/windows.gen0.jsonl"])).hexdigest()
    assert verify_completion_sidecar(artifact, upstream).valid
    collect(work, policy="local", apply=True, now=NOW)
    payload = json.loads(read_text(media / "recognise/observations.done.json"))
    pruned = payload["upstream"]["windows/windows.gen0.jsonl"]
    assert pruned == {"pruned_upstream": original_hash}
    assert verify_completion_sidecar(artifact, upstream).valid
    artifact.write_bytes(b"changed")
    assert not verify_completion_sidecar(artifact, upstream).valid


def test_keep_intermediates_preserves_windows_and_pcm(tmp_path: Path) -> None:
    work = tmp_path / "work"
    code, _, _, paths = run(work, keep_intermediates=True)
    assert code == 0
    media = paths[0].parents[2]
    entry = json.loads(read_text(media / "invocations.jsonl").splitlines()[-1])
    assert entry["keep_intermediates"] is True
    collect(work, policy="local", apply=True, now=datetime.now(UTC) + timedelta(seconds=1))
    assert (media / "windows/windows.gen0.jsonl").is_file()
    assert (media / "decode/audio.pcm").is_file()


def test_nonterminal_latest_run_prevents_pruning(tmp_path: Path) -> None:
    work = tmp_path / "work"
    media = _seed_media(work, "complete", age=timedelta(days=8))
    with (media / "invocations.jsonl").open("a", encoding="utf-8") as journal:
        journal.write(json.dumps({"status": "running", "started_at": NOW.isoformat()}) + "\n")
    assert collect(work, policy="hosted", apply=True, now=NOW).actions == ()
    assert (media / "windows/windows.gen0.jsonl").is_file()
    assert (media / "decode/audio.pcm").is_file()
    assert (media / "ingest/original.wav").is_file()


def test_referenced_bundle_prevents_whole_media_collection(tmp_path: Path) -> None:
    work = tmp_path / "work"
    media = seed(work)
    bundle = publish(media)
    finished = (NOW - timedelta(days=8)).isoformat()
    (media / "invocations.jsonl").write_text(
        json.dumps({"status": "failed", "finished_at": finished}) + "\n", encoding="utf-8"
    )
    (media / "windows").mkdir(exist_ok=True)
    (media / "windows/temp.wav").write_bytes(b"window")
    collect(work, policy="local", apply=True, now=NOW)
    assert media.is_dir() and bundle.is_dir()


def test_trash_purge_never_removes_anything_younger_than_7_days(tmp_path: Path) -> None:
    work = tmp_path / "work"
    # A dated directory collects that whole UTC day, so 2026-09-04 can still hold a file moved at
    # 23:59 that day - only 6.5 days old at NOW. Only 2026-09-03 is provably expired.
    old = work / ".trash/2026-09-03/old"
    boundary = work / ".trash/2026-09-04/boundary"
    young = work / ".trash/2026-09-05/young"
    for directory in (old, boundary, young):
        directory.mkdir(parents=True)
        (directory / "file").write_bytes(b"x")
    collect(work, policy="local", apply=True, now=NOW)
    assert not old.parent.exists()
    assert boundary.is_dir()
    assert young.is_dir()


def test_deep_upgrade_rederives_identical_frozen_windows(tmp_path: Path) -> None:
    work = tmp_path / "work"
    code, _, _, free = run(work)
    assert code == 0
    media = free[0].parents[2]
    window_record = media / "windows/windows.gen0.jsonl"
    before = read_bytes(window_record)
    collect(work, policy="local", apply=True, now=datetime.now(UTC) + timedelta(seconds=1))
    assert not window_record.exists()
    free_manifest = read_bundle_manifest(free[0])
    assert free_manifest is not None
    assert read_manifest(media / free_manifest["fuse_run"]) is not None
    code, audd, shazam, deep = run(work, "deep")
    assert code == 0 and deep != free and audd.calls == 7 and shazam.requests == 0
    assert read_bytes(window_record) == before


def test_refetch_after_pcm_expiry_reuses_source_changed_exit_5(tmp_path: Path, monkeypatch) -> None:
    import importlib

    ingestion = importlib.import_module("id_detector.ingest")
    work = tmp_path / "work"
    _, _, _, paths = run(work)
    retained = cli._load_cached(work, str(Path("tests/fixtures/audio/tone-60s.wav")))
    media = paths[0].parents[2]
    entries = [json.loads(line) for line in read_text(media / "invocations.jsonl").splitlines()]
    entries[-1]["finished_at"] = (NOW - timedelta(days=8)).isoformat()
    (media / "invocations.jsonl").write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8"
    )
    collect(work, policy="hosted", apply=True, now=NOW)
    assert not (media / "decode/audio.pcm").exists() and not retained.original_path.exists()
    monkeypatch.setattr(cli, "_load_cached", lambda *args: retained)
    monkeypatch.setattr(pipeline, "_load_cached", lambda *args: retained)
    monkeypatch.setattr(ingestion, "_load_cached", lambda *args: retained)
    monkeypatch.setattr(ingestion, "_load_ingest_cached", lambda *args: None)

    async def fake_download(command, **kwargs):
        output = Path(command[command.index("-o") + 1]).parent
        (output / "asset.wav").write_bytes(b"different refetched bytes")
        (output / "asset.info.json").write_text('{"extractor":"fixture"}', encoding="utf-8")

    monkeypatch.setattr(ingestion, "run_process", fake_download)
    code, audd, shazam, results = run(
        work, "deep", audio="https://example.invalid/changed", refresh=True
    )
    assert code == 5 and audd.calls == shazam.requests == 0 and not results
    assert json.loads(read_text(media / "invocations.jsonl").splitlines()[-1])["status"] == (
        "source_changed"
    )


def test_hour_fixture_hosted_manifest_is_at_most_25mb(tmp_path: Path) -> None:
    fixture = FIXTURE_ROOT / "tone-3600s.wav"
    if not fixture.is_file():
        generate(fixture, 3_600)
    work = tmp_path / "work"
    media = _seed_media(work, "complete", age=timedelta(days=8))
    original = media / "ingest/original.wav"
    original.unlink()
    os.link(fixture, original)
    pcm = media / "decode/audio.pcm"
    with pcm.open("wb") as handle:
        handle.truncate(fixture.stat().st_size)
    collect(work, policy="hosted", apply=True, now=NOW)
    manifest = json.loads(read_text(media / "retention-manifest.json"))
    assert manifest["total_size"] == sum(item["size"] for item in manifest["files"].values())
    assert manifest["total_size"] <= 25 * 1024 * 1024


def _journal(media: Path, *entries: dict[str, object]) -> None:
    """Replace the media's journal with *entries*, oldest first."""

    (media / "invocations.jsonl").write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8"
    )


def _entry(status: str, *, age: timedelta, keep: bool = False) -> dict[str, object]:
    finished = NOW - age
    return {
        "invocation_id": f"{status}-{age}",
        "status": status,
        "started_at": (finished - timedelta(minutes=1)).isoformat(),
        "finished_at": finished.isoformat(),
        "keep_intermediates": keep,
    }


def test_failed_run_keeps_what_an_earlier_success_owns(tmp_path: Path) -> None:
    """Regression (P0): media artefacts are shared, so one run's rule cannot delete another's.

    Plan section 4.6 keeps a local ``complete`` original indefinitely and its PCM for 48 h.
    Reading only the newest journal entry let a failed refresh two days later move both.
    """

    work = tmp_path / "work"
    media = _seed_media(work, "complete", age=timedelta(hours=1))
    _journal(
        media,
        _entry("complete", age=timedelta(hours=1)),
        _entry("failed", age=timedelta(days=2)),
    )
    collect(work, policy="local", apply=True, now=NOW)
    assert (media / "ingest/original.wav").is_file()
    assert (media / "decode/audio.pcm").is_file(), "the success still owns its PCM for 48 h"

    hosted = _seed_media(tmp_path / "h", "complete", age=timedelta(days=30))
    _journal(
        hosted,
        _entry("complete", age=timedelta(days=30)),
        _entry("failed", age=timedelta(days=2)),
    )
    collect(tmp_path / "h", policy="hosted", apply=True, now=NOW)
    assert not (hosted / "ingest/original.wav").exists(), "hosted still expires at 7 d"


def test_media_dir_survives_a_failure_after_a_success(tmp_path: Path) -> None:
    """Regression (P0): the 7-day whole-directory rule is for media that never succeeded."""

    work = tmp_path / "work"
    media = _seed_media(work, "failed", age=timedelta(days=8))
    _journal(
        media,
        _entry("complete", age=timedelta(days=40)),
        _entry("failed", age=timedelta(days=8)),
    )
    collect(work, policy="local", apply=True, now=NOW)
    assert media.is_dir() and (media / "invocations.jsonl").is_file()
    assert (media / "ingest/original.wav").is_file()


def test_keep_intermediates_pin_survives_a_later_default_run(tmp_path: Path) -> None:
    """Regression (P0): an explicit keep is durable; a later default run cannot undo it."""

    work = tmp_path / "work"
    media = _seed_media(work, "complete", age=timedelta(days=8))
    _journal(
        media,
        _entry("complete", age=timedelta(days=8), keep=True),
        _entry("complete", age=timedelta(days=8)),
    )
    collect(work, policy="local", apply=True, now=NOW)
    assert (media / "windows/windows.gen0.jsonl").is_file()
    assert (media / "decode/audio.pcm").is_file()


@pytest.mark.parametrize("damage", ["bundle", "index"])
def test_unreadable_reference_blocks_collection(tmp_path: Path, damage: str) -> None:
    """Regression (P0/P1): ambiguity is a reference; only proven-unreferenced media is collected."""

    work = tmp_path / "work"
    media = _seed_media(work, "failed", age=timedelta(days=8))
    if damage == "bundle":
        bundle = media / "present/bundles" / ("c" * 64)
        os.makedirs(native_path(bundle), exist_ok=True)
        Path(native_path(bundle / "manifest.json")).write_text("{", encoding="utf-8")
    else:
        (work / "index.json").write_text("{not json", encoding="utf-8")
    collect(work, policy="local", apply=True, now=NOW)
    assert media.is_dir(), "a bundle or index we cannot read must not license a delete"


def test_state_is_read_under_the_media_lock(tmp_path: Path, monkeypatch) -> None:
    """Regression (P0/P1): a publication that lands during the scan must not be collected.

    The pipeline holds ``.media.lock`` while it ingests, journals and publishes, so re-reading
    the journal and the references after acquiring it closes the plan-to-move window.
    """

    work = tmp_path / "work"
    media = _seed_media(work, "failed", age=timedelta(days=8))

    class PublishingLock(retention.ProcessLock):
        def acquire(self) -> None:
            super().acquire()
            if media.is_dir() and not (media / "present/current").exists():
                (media / "present").mkdir(exist_ok=True)
                (media / "present/current").write_text("a" * 64, encoding="utf-8")
                _journal(media, _entry("complete", age=timedelta(minutes=1)))

    monkeypatch.setattr(retention, "ProcessLock", PublishingLock)
    collect(work, policy="local", apply=True, now=NOW)
    assert media.is_dir(), "the run that finished during the scan owns this media now"
    assert (media / "ingest/original.wav").is_file()
    assert (media / "decode/audio.pcm").is_file()


def test_manifest_is_written_under_the_media_lock(tmp_path: Path, monkeypatch) -> None:
    """Regression (P1): the inventory must not race the writer it is inventorying."""

    work = tmp_path / "work"
    media = _seed_media(work, "complete", age=timedelta(hours=1))
    held: list[bool] = []
    real_manifest = retention._write_manifest

    def spy(media_dir: Path, **kwargs) -> None:
        probe = retention.ProcessLock(media_dir / ".media.lock")
        try:
            probe.acquire()
            probe.release()
            held.append(False)
        except JobStoreLocked:
            held.append(True)
        real_manifest(media_dir, **kwargs)

    monkeypatch.setattr(retention, "_write_manifest", spy)
    collect(work, policy="local", apply=True, now=NOW)
    assert held == [True]
    assert (media / "retention-manifest.json").is_file()


def _link(link: Path, target: Path) -> bool:
    """Best-effort directory link; junctions need no privilege, symlinks usually do."""

    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                check=False,
            )
            return completed.returncode == 0 and link.exists()
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        return False
    return link.exists()


def test_reparse_point_cannot_move_files_outside_work(tmp_path: Path) -> None:
    """Regression (P0): GC only ever touches paths that really live under the work root."""

    outside = tmp_path / "outside"
    external = _seed_media(outside, "failed", age=timedelta(days=8))
    work = tmp_path / "work"
    work.mkdir()
    if not _link(work / external.parent.name, external.parent):
        pytest.skip("this platform will not create a directory link without extra privilege")
    collect(work, policy="local", apply=True, now=NOW)
    assert external.is_dir()
    assert (external / "ingest/original.wav").is_file()
    assert (external / "decode/audio.pcm").is_file()
    assert (external / "windows/windows.gen0.jsonl").is_file()
    assert not (work / ".trash").exists()


def test_reparse_point_trash_is_never_purged(tmp_path: Path) -> None:
    """Regression (P0): ``rmtree`` follows a junction, so a faked ``.trash`` is refused."""

    outside = tmp_path / "outside"
    (outside / "2026-09-01").mkdir(parents=True)
    (outside / "2026-09-01/precious").write_bytes(b"not ours to delete")
    work = tmp_path / "work"
    work.mkdir()
    media = _seed_media(work, "failed", age=timedelta(days=8))
    if not _link(work / ".trash", outside):
        pytest.skip("this platform will not create a directory link without extra privilege")
    result = collect(work, policy="local", apply=True, now=NOW)
    assert (outside / "2026-09-01/precious").is_file()
    assert media.is_dir() and (media / "ingest/original.wav").is_file()
    assert any(action.operation == "skip" for action in result.actions)


def test_pruned_marker_rejects_a_divergent_upstream(tmp_path: Path) -> None:
    """Regression (P1): the marker records what the artefact was derived from, and stays checked."""

    work = tmp_path / "work"
    media = _seed_media(work, "complete", age=timedelta(hours=1))
    artifact = media / "recognise/observations.jsonl"
    window = media / "windows/windows.gen0.jsonl"
    before = read_bytes(window)
    upstream = {"windows/windows.gen0.jsonl": window}
    collect(work, policy="local", apply=True, now=NOW)
    assert not window.exists() and verify_completion_sidecar(artifact, upstream).valid

    window.parent.mkdir(exist_ok=True)
    window.write_bytes(b"re-derived differently\n")
    assert not verify_completion_sidecar(artifact, upstream).valid

    window.write_bytes(before)
    assert verify_completion_sidecar(artifact, upstream).valid
    write_completion_sidecar(artifact, upstream)
    payload = json.loads(read_text(media / "recognise/observations.done.json"))
    assert payload["upstream"]["windows/windows.gen0.jsonl"] == sha256(before).hexdigest()


def test_every_pruned_artefact_is_recoverable(tmp_path: Path) -> None:
    """Regression (P1): nothing is deleted in place; the dated trash holds an intact copy."""

    work = tmp_path / "w"
    media = _seed_media(work, "failed", age=timedelta(days=2))
    pruned = {
        "ingest/original.wav": read_bytes(media / "ingest/original.wav"),
        "decode/audio.pcm": read_bytes(media / "decode/audio.pcm"),
        "windows/windows.gen0.jsonl": read_bytes(media / "windows/windows.gen0.jsonl"),
    }
    collect(work, policy="local", apply=True, now=NOW)
    trashed = work / ".trash" / NOW.date().isoformat() / media.parent.name / media.name
    for relative, content in pruned.items():
        assert not Path(native_path(media / relative)).exists()
        assert read_bytes(trashed / relative) == content

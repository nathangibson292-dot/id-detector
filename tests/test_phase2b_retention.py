"""Cycle 2b: recoverable retention, pruned sidecars and on-demand re-derivation."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from typer.testing import CliRunner

from id_detector import cli, pipeline, retention
from id_detector.io import (
    atomic_write_bytes,
    atomic_write_json,
    completion_sidecar_path,
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


def _write(path: Path, content: bytes | str) -> None:
    """Seed one fixture file through the long-path-safe atomic writer.

    A work tree nests two 64-hex directories, and pytest's numbered temp root gains a character
    whenever its counter passes a power of ten, so plain ``pathlib`` writes (and ``exists()``
    answers) break here once a fixture path passes MAX_PATH. Real work trees reach the same
    lengths, so the fixtures must not be shortened to dodge it.
    """

    atomic_write_bytes(path, content.encode("utf-8") if isinstance(content, str) else content)


def _exists(path: Path) -> bool:
    return os.path.exists(native_path(path))


def _is_file(path: Path) -> bool:
    return os.path.isfile(native_path(path))


def _is_dir(path: Path) -> bool:
    return os.path.isdir(native_path(path))


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under *root* with its bytes, walked in extended-path form so none is missed."""

    base = native_path(root)
    found: dict[str, bytes] = {}
    for directory, _, names in os.walk(base):
        for name in names:
            full = os.path.join(directory, name)
            with open(full, "rb") as handle:
                found[Path(os.path.relpath(full, base)).as_posix()] = handle.read()
    return found


def _seed_media(root: Path, status: str, *, age: timedelta, keep: bool = False) -> Path:
    media_key = sha256(f"{status}-{age}-{keep}".encode()).hexdigest()
    media = root / sha256(("source-" + media_key).encode()).hexdigest() / media_key
    original = media / "ingest/original.wav"
    _write(original, b"original")
    source = media / "ingest/source.json"
    atomic_write_json(
        source,
        {"media_key": media_key, "original": {"path": "ingest/original.wav"}},
    )
    write_completion_sidecar(source, {"ingest/original.wav": original})
    pcm = media / "decode/audio.pcm"
    _write(pcm, b"pcm")
    record = media / "decode/pcm.json"
    atomic_write_json(record, {"pcm": True})
    write_completion_sidecar(
        record,
        {"ingest/source.json": source, "ingest/original.wav": original},
    )
    windows = media / "windows/windows.gen0.jsonl"
    _write(windows, "{}\n")
    write_completion_sidecar(
        windows,
        {"decode/pcm.json": record, "decode/audio.pcm": pcm},
    )
    downstream = media / "recognise/observations.jsonl"
    _write(downstream, "{}\n")
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
    _write(media / "invocations.jsonl", json.dumps(journal) + "\n")
    return media


@pytest.mark.parametrize("status", sorted(SUCCESS_STATUSES))
def test_success_statuses_prune_windows_now_pcm_at_48h_and_hosted_original_at_7d(
    tmp_path: Path, status: str
) -> None:
    fresh = _seed_media(tmp_path / "fresh", status, age=timedelta(hours=1))
    collect(tmp_path / "fresh", policy="hosted", apply=True, now=NOW)
    assert not _exists(fresh / "windows")
    assert _is_file(fresh / "decode/audio.pcm")
    assert _is_file(fresh / "ingest/original.wav")

    pcm_old = _seed_media(tmp_path / "pcm-old", status, age=timedelta(days=3))
    collect(tmp_path / "pcm-old", policy="hosted", apply=True, now=NOW)
    assert not _exists(pcm_old / "decode/audio.pcm")
    assert _is_file(pcm_old / "ingest/original.wav")

    hosted = _seed_media(tmp_path / "hosted", status, age=timedelta(days=8))
    collect(tmp_path / "hosted", policy="hosted", apply=True, now=NOW)
    assert not _exists(hosted / "ingest/original.wav")
    assert _is_file(hosted / "retention-manifest.json")

    local = _seed_media(tmp_path / "local", status, age=timedelta(days=8))
    collect(tmp_path / "local", policy="local", apply=True, now=NOW)
    assert _is_file(local / "ingest/original.wav")


@pytest.mark.parametrize("status", sorted(FAILED_STATUSES))
def test_failure_statuses_prune_windows_pcm_now_and_original_at_24h(
    tmp_path: Path, status: str
) -> None:
    fresh = _seed_media(tmp_path / "fresh", status, age=timedelta(hours=1))
    collect(tmp_path / "fresh", policy="local", apply=True, now=NOW)
    assert not _exists(fresh / "windows")
    assert not _exists(fresh / "decode/audio.pcm")
    assert _is_file(fresh / "ingest/original.wav")

    old = _seed_media(tmp_path / "old", status, age=timedelta(days=2))
    collect(tmp_path / "old", policy="local", apply=True, now=NOW)
    assert _is_dir(old)
    assert not _exists(old / "ingest/original.wav")


def test_unreferenced_failed_media_moves_whole_directory_after_7d(tmp_path: Path) -> None:
    work = tmp_path / "work"
    media = _seed_media(work, "failed", age=timedelta(days=8))
    collect(work, policy="local", apply=True, now=NOW)
    assert not _exists(media)
    assert _is_dir(work / ".trash/2026-09-11" / media.relative_to(work))


def test_dry_run_changes_nothing_and_cli_requires_apply(tmp_path: Path) -> None:
    work = tmp_path / "work"
    media = _seed_media(work, "complete", age=timedelta(hours=1))
    before = _tree(work)
    result = CliRunner().invoke(cli.app, ["gc", "--policy", "local", "--work-root", str(work)])
    assert result.exit_code == 0 and "gc dry run" in result.output and "would move" in result.output
    assert _tree(work) == before
    result = CliRunner().invoke(
        cli.app, ["gc", "--policy", "local", "--work-root", str(work), "--apply"]
    )
    assert result.exit_code == 0 and "gc applied" in result.output
    assert not _exists(media / "windows")


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
    _write(artifact, b"changed")
    assert not verify_completion_sidecar(artifact, upstream).valid


def test_keep_intermediates_preserves_windows_and_pcm(tmp_path: Path) -> None:
    work = tmp_path / "work"
    code, _, _, paths = run(work, keep_intermediates=True)
    assert code == 0
    media = paths[0].parents[2]
    entry = json.loads(read_text(media / "invocations.jsonl").splitlines()[-1])
    assert entry["keep_intermediates"] is True
    collect(work, policy="local", apply=True, now=datetime.now(UTC) + timedelta(seconds=1))
    assert _is_file(media / "windows/windows.gen0.jsonl")
    assert _is_file(media / "decode/audio.pcm")


def test_nonterminal_latest_run_prevents_pruning(tmp_path: Path) -> None:
    work = tmp_path / "work"
    media = _seed_media(work, "complete", age=timedelta(days=8))
    running = json.dumps({"status": "running", "started_at": NOW.isoformat()}) + "\n"
    _write(media / "invocations.jsonl", read_text(media / "invocations.jsonl") + running)
    assert collect(work, policy="hosted", apply=True, now=NOW).actions == ()
    assert _is_file(media / "windows/windows.gen0.jsonl")
    assert _is_file(media / "decode/audio.pcm")
    assert _is_file(media / "ingest/original.wav")


def test_referenced_bundle_prevents_whole_media_collection(tmp_path: Path) -> None:
    work = tmp_path / "work"
    media = seed(work)
    bundle = publish(media)
    finished = (NOW - timedelta(days=8)).isoformat()
    _write(
        media / "invocations.jsonl",
        json.dumps({"status": "failed", "finished_at": finished}) + "\n",
    )
    _write(media / "windows/temp.wav", b"window")
    collect(work, policy="local", apply=True, now=NOW)
    assert _is_dir(media) and _is_dir(bundle)


def test_trash_purge_never_removes_anything_younger_than_7_days(tmp_path: Path) -> None:
    work = tmp_path / "work"
    # A dated directory collects that whole UTC day, so 2026-09-04 can still hold a file moved at
    # 23:59 that day - only 6.5 days old at NOW. Only 2026-09-03 is provably expired.
    old = work / ".trash/2026-09-03/old"
    boundary = work / ".trash/2026-09-04/boundary"
    young = work / ".trash/2026-09-05/young"
    for directory in (old, boundary, young):
        _write(directory / "file", b"x")
    collect(work, policy="local", apply=True, now=NOW)
    assert not _exists(old.parent)
    assert _is_dir(boundary)
    assert _is_dir(young)


def test_deep_upgrade_rederives_identical_frozen_windows(tmp_path: Path) -> None:
    work = tmp_path / "work"
    code, _, _, free = run(work)
    assert code == 0
    media = free[0].parents[2]
    window_record = media / "windows/windows.gen0.jsonl"
    before = read_bytes(window_record)
    collect(work, policy="local", apply=True, now=datetime.now(UTC) + timedelta(seconds=1))
    assert not _exists(window_record)
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
    _write(media / "invocations.jsonl", "\n".join(json.dumps(entry) for entry in entries) + "\n")
    collect(work, policy="hosted", apply=True, now=NOW)
    assert not _exists(media / "decode/audio.pcm") and not _exists(retained.original_path)
    monkeypatch.setattr(cli, "_load_cached", lambda *args: retained)
    monkeypatch.setattr(pipeline, "_load_cached", lambda *args: retained)
    monkeypatch.setattr(ingestion, "_load_cached", lambda *args: retained)
    monkeypatch.setattr(ingestion, "_load_ingest_cached", lambda *args: None)

    async def fake_download(command, **kwargs):
        output = Path(command[command.index("-o") + 1]).parent
        _write(output / "asset.wav", b"different refetched bytes")
        _write(output / "asset.info.json", '{"extractor":"fixture"}')

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
    os.unlink(native_path(original))
    os.link(native_path(fixture), native_path(original))
    pcm = media / "decode/audio.pcm"
    with open(native_path(pcm), "wb") as handle:
        handle.truncate(fixture.stat().st_size)
    collect(work, policy="hosted", apply=True, now=NOW)
    manifest = json.loads(read_text(media / "retention-manifest.json"))
    assert manifest["total_size"] == sum(item["size"] for item in manifest["files"].values())
    assert manifest["total_size"] <= 25 * 1024 * 1024


def _journal(media: Path, *entries: dict[str, object]) -> None:
    """Replace the media's journal with *entries*, oldest first."""

    _write(media / "invocations.jsonl", "".join(json.dumps(entry) + "\n" for entry in entries))


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
    assert _is_file(media / "ingest/original.wav")
    assert _is_file(media / "decode/audio.pcm"), "the success still owns its PCM for 48 h"

    hosted = _seed_media(tmp_path / "h", "complete", age=timedelta(days=30))
    _journal(
        hosted,
        _entry("complete", age=timedelta(days=30)),
        _entry("failed", age=timedelta(days=2)),
    )
    collect(tmp_path / "h", policy="hosted", apply=True, now=NOW)
    assert not _exists(hosted / "ingest/original.wav"), "hosted still expires at 7 d"


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
    assert _is_dir(media) and _is_file(media / "invocations.jsonl")
    assert _is_file(media / "ingest/original.wav")


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
    assert _is_file(media / "windows/windows.gen0.jsonl")
    assert _is_file(media / "decode/audio.pcm")


@pytest.mark.parametrize("damage", ["bundle", "index"])
def test_unreadable_reference_blocks_collection(tmp_path: Path, damage: str) -> None:
    """Regression (P0/P1): ambiguity is a reference; only proven-unreferenced media is collected."""

    work = tmp_path / "work"
    media = _seed_media(work, "failed", age=timedelta(days=8))
    if damage == "bundle":
        _write(media / "present/bundles" / ("c" * 64) / "manifest.json", "{")
    else:
        _write(work / "index.json", "{not json")
    collect(work, policy="local", apply=True, now=NOW)
    assert _is_dir(media), "a bundle or index we cannot read must not license a delete"


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
            if _is_dir(media) and not _exists(media / "present/current"):
                _write(media / "present/current", "a" * 64)
                _journal(media, _entry("complete", age=timedelta(minutes=1)))

    monkeypatch.setattr(retention, "ProcessLock", PublishingLock)
    collect(work, policy="local", apply=True, now=NOW)
    assert _is_dir(media), "the run that finished during the scan owns this media now"
    assert _is_file(media / "ingest/original.wav")
    assert _is_file(media / "decode/audio.pcm")


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
    assert _is_file(media / "retention-manifest.json")


def _link(link: Path, target: Path) -> bool:
    """Best-effort directory link; junctions need no privilege, symlinks usually do."""

    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                check=False,
            )
            return completed.returncode == 0 and _exists(link)
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        return False
    return _exists(link)


def test_reparse_point_cannot_move_files_outside_work(tmp_path: Path) -> None:
    """Regression (P0): GC only ever touches paths that really live under the work root."""

    outside = tmp_path / "outside"
    external = _seed_media(outside, "failed", age=timedelta(days=8))
    work = tmp_path / "work"
    work.mkdir()
    if not _link(work / external.parent.name, external.parent):
        pytest.skip("this platform will not create a directory link without extra privilege")
    collect(work, policy="local", apply=True, now=NOW)
    assert _is_dir(external)
    assert _is_file(external / "ingest/original.wav")
    assert _is_file(external / "decode/audio.pcm")
    assert _is_file(external / "windows/windows.gen0.jsonl")
    assert not _exists(work / ".trash")


def test_reparse_point_trash_is_never_purged(tmp_path: Path) -> None:
    """Regression (P0): ``rmtree`` follows a junction, so a faked ``.trash`` is refused."""

    outside = tmp_path / "outside"
    _write(outside / "2026-09-01/precious", b"not ours to delete")
    work = tmp_path / "work"
    work.mkdir()
    media = _seed_media(work, "failed", age=timedelta(days=8))
    if not _link(work / ".trash", outside):
        pytest.skip("this platform will not create a directory link without extra privilege")
    result = collect(work, policy="local", apply=True, now=NOW)
    assert _is_file(outside / "2026-09-01/precious")
    assert _is_dir(media) and _is_file(media / "ingest/original.wav")
    assert any(action.operation == "skip" for action in result.actions)


@pytest.mark.parametrize("state", ["linked", "dangling", "file"])
def test_invalid_trash_dry_run_plans_exactly_what_apply_does(tmp_path: Path, state: str) -> None:
    """Regression (P1): with an unusable ``.trash`` the preview promised moves apply never makes.

    ``idea gc`` without ``--apply`` is the owner's safe preview, so it must list exactly the
    actions ``--apply`` then returns - here, one refusal and nothing else.
    """

    work = tmp_path / "w"
    media = _seed_media(work, "failed", age=timedelta(days=2))
    outside = tmp_path / "outside"
    outside.mkdir()
    trash = work / ".trash"
    if state == "file":
        _write(trash, b"not a directory")
    else:
        if not _junction(trash, outside):
            pytest.skip("this platform will not create a directory link without extra privilege")
        if state == "dangling":
            outside.rmdir()
    before = _tree(work)
    planned = collect(work, policy="local", apply=False, now=NOW)
    assert _tree(work) == before
    applied = collect(work, policy="local", apply=True, now=NOW)
    assert applied.actions == planned.actions
    assert [(action.operation, action.path) for action in planned.actions] == [("skip", trash)]
    assert _tree(work) == before
    assert read_bytes(media / "ingest/original.wav") == b"original"


def test_pruned_marker_rejects_a_divergent_upstream(tmp_path: Path) -> None:
    """Regression (P1): the marker records what the artefact was derived from, and stays checked."""

    work = tmp_path / "work"
    media = _seed_media(work, "complete", age=timedelta(hours=1))
    artifact = media / "recognise/observations.jsonl"
    window = media / "windows/windows.gen0.jsonl"
    before = read_bytes(window)
    upstream = {"windows/windows.gen0.jsonl": window}
    collect(work, policy="local", apply=True, now=NOW)
    assert not _exists(window) and verify_completion_sidecar(artifact, upstream).valid

    _write(window, b"re-derived differently\n")
    assert not verify_completion_sidecar(artifact, upstream).valid

    _write(window, before)
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
        assert not _exists(media / relative)
        assert read_bytes(trashed / relative) == content


# --- Real-length work trees -------------------------------------------------------------------
#
# The owner's work root is ``C:\Users\natha\Documents\Music\id-detector\work`` (43 characters),
# so a media directory is about 175 characters and its deeper artefacts - a recognise
# invocation's ``raw/<64-hex>.json`` (~290), a bundle's ``index.html`` (~265) - pass MAX_PATH.
# Without LongPathsEnabled every plain ``pathlib``/``shutil`` call on those paths misreports or
# fails, so these tests build trees of that shape through the extended-path helpers only.

_INVOCATION = "b2de34cd16ecfdbee2e5"


def _padded_work_root(tmp_path: Path, media_length: int) -> Path:
    """A work root whose ``<source>/<media>`` directories are at least *media_length* long."""

    want = media_length - 130  # "\\<64-hex source>\\<64-hex media>"
    pad = want - len(str(tmp_path)) - 1
    return tmp_path / ("p" * pad) if pad > 0 else tmp_path / "w"


def _seed_invocation(media: Path) -> dict[str, Path]:
    """Add a recognise invocation with a sealed artefact and a raw response, as a real run does."""

    invocation = media / "recognise/invocations" / _INVOCATION
    observations = invocation / "observations.gen0.jsonl"
    _write(observations, "{}\n")
    write_completion_sidecar(
        observations, {"windows/windows.gen0.jsonl": media / "windows/windows.gen0.jsonl"}
    )
    raw = invocation / "raw" / f"{'f' * 64}.json"
    _write(raw, '{"raw":true}')
    return {"observations": observations, "raw": raw}


def _real_length(tmp_path: Path, status: str, *, age: timedelta) -> tuple[Path, Path, dict]:
    work = _padded_work_root(tmp_path, 205)
    media = _seed_media(work, status, age=age)
    deep = _seed_invocation(media)
    if os.name == "nt":
        assert len(str(media)) < 248, "the media directory itself stays a legacy-length path"
        assert len(str(completion_sidecar_path(deep["observations"]))) > 260
        assert len(str(deep["raw"])) > 260
    return work, media, deep


def test_long_paths_manifest_inventories_every_file(tmp_path: Path) -> None:
    work, media, deep = _real_length(tmp_path, "complete", age=timedelta(hours=1))
    collect(work, policy="local", apply=True, now=NOW)
    manifest = json.loads(read_text(media / "retention-manifest.json"))
    on_disk = {
        relative: {"size": len(content)}
        for relative, content in _tree(media).items()
        if relative != "retention-manifest.json"
    }
    assert deep["raw"].relative_to(media).as_posix() in on_disk
    assert manifest["files"] == on_disk
    assert manifest["total_size"] == sum(item["size"] for item in on_disk.values())


def test_long_paths_sidecar_is_sealed_before_its_upstream_moves(tmp_path: Path) -> None:
    work, media, deep = _real_length(tmp_path, "complete", age=timedelta(hours=1))
    window = media / "windows/windows.gen0.jsonl"
    window_hash = sha256(read_bytes(window)).hexdigest()
    collect(work, policy="local", apply=True, now=NOW)
    assert not _exists(window)
    payload = json.loads(read_text(completion_sidecar_path(deep["observations"])))
    assert payload["upstream"]["windows/windows.gen0.jsonl"] == {"pruned_upstream": window_hash}
    assert verify_completion_sidecar(
        deep["observations"], {"windows/windows.gen0.jsonl": window}
    ).valid


def test_long_paths_expired_media_is_trashed_then_purged(tmp_path: Path) -> None:
    work, media, deep = _real_length(tmp_path, "failed", age=timedelta(days=8))
    raw = read_bytes(deep["raw"])
    collect(work, policy="local", apply=True, now=NOW)
    assert not _exists(media)
    day = work / ".trash" / NOW.date().isoformat()
    assert read_bytes(day / deep["raw"].relative_to(work)) == raw

    result = collect(work, policy="local", apply=True, now=NOW + timedelta(days=9))
    assert [action.path for action in result.actions if action.operation == "purge"] == [day]
    assert not _exists(day)
    assert _is_dir(work / ".trash")


def test_long_paths_published_bundle_blocks_collection(tmp_path: Path) -> None:
    work, media, deep = _real_length(tmp_path, "failed", age=timedelta(days=8))
    index = media / "present/bundles" / ("d" * 64) / "index.html"
    _write(index, "<!doctype html>")
    result = collect(work, policy="local", apply=True, now=NOW)
    assert media not in [action.path for action in result.actions]
    assert _is_file(index) and _is_file(deep["raw"])


def test_long_paths_dry_run_plans_exactly_what_apply_does(tmp_path: Path) -> None:
    work, media, deep = _real_length(tmp_path, "complete", age=timedelta(days=8))
    stale = work / ".trash/2026-09-01" / media.relative_to(work) / deep["raw"].relative_to(media)
    _write(stale, b"expired")
    before = _tree(work)
    planned = collect(work, policy="hosted", apply=False, now=NOW)
    assert _tree(work) == before
    assert {action.operation for action in planned.actions} == {"move", "purge"}

    applied = collect(work, policy="hosted", apply=True, now=NOW)
    assert applied.actions == planned.actions
    day = work / ".trash" / NOW.date().isoformat()
    for action in applied.actions:
        assert not _exists(action.path), action
        if action.operation == "move":
            assert _exists(day / action.path.relative_to(work)), action


def test_media_directory_beyond_max_path_is_still_collected(tmp_path: Path) -> None:
    """A longer work root pushes the media directory itself past MAX_PATH."""

    work = _padded_work_root(tmp_path, 275)
    media = _seed_media(work, "complete", age=timedelta(hours=1))
    assert len(str(media)) > 260
    planned = collect(work, policy="local", apply=False, now=NOW)
    assert [(action.operation, action.path) for action in planned.actions] == [
        ("move", media / "windows")
    ]
    applied = collect(work, policy="local", apply=True, now=NOW)
    assert applied.actions == planned.actions
    assert not _exists(media / "windows")
    assert _is_file(media / "decode/audio.pcm") and _is_file(media / "ingest/original.wav")
    assert _is_file(media / "retention-manifest.json")


def test_long_paths_bundles_directory_past_max_path_blocks_collection(tmp_path: Path) -> None:
    """The listed ``present/bundles`` directory itself - not only a child - is past MAX_PATH."""

    work = _padded_work_root(tmp_path, 246)
    media = _seed_media(work, "failed", age=timedelta(days=8))
    bundles = media / "present/bundles"
    _write(bundles / ("d" * 64) / "index.html", "<!doctype html>")
    if os.name == "nt":
        assert len(str(media)) < 260 < len(str(bundles))
    result = collect(work, policy="local", apply=True, now=NOW)
    assert media not in [action.path for action in result.actions]
    assert _is_dir(media) and _is_dir(bundles)


def test_long_paths_original_past_max_path_follows_its_policy(tmp_path: Path) -> None:
    hosted_work = _padded_work_root(tmp_path / "h", 246)
    hosted = _seed_media(hosted_work, "complete", age=timedelta(days=8))
    original = hosted / "ingest/original.wav"
    if os.name == "nt":
        assert len(str(original)) > 260
    collect(hosted_work, policy="hosted", apply=True, now=NOW)
    assert not _exists(original)
    trashed = hosted_work / ".trash" / NOW.date().isoformat() / original.relative_to(hosted_work)
    assert read_bytes(trashed) == b"original"

    local_work = _padded_work_root(tmp_path / "l", 246)
    local = _seed_media(local_work, "complete", age=timedelta(days=8))
    collect(local_work, policy="local", apply=True, now=NOW)
    assert read_bytes(local / "ingest/original.wav") == b"original"


def test_long_paths_trash_move_operands_past_max_path(tmp_path: Path) -> None:
    """Both rename operands are past MAX_PATH, and an earlier trashed copy is never overwritten."""

    work = _padded_work_root(tmp_path, 246)
    media = _seed_media(work, "failed", age=timedelta(hours=1))
    pcm = media / "decode/audio.pcm"
    destination = work / ".trash" / NOW.date().isoformat() / pcm.relative_to(work)
    earlier = b"trashed by an earlier pass today"
    _write(destination, earlier)
    if os.name == "nt":
        assert len(str(pcm)) > 260 and len(str(destination)) > 260
    collect(work, policy="local", apply=True, now=NOW)
    assert not _exists(pcm)
    assert read_bytes(destination) == earlier, "a recoverable copy must never be overwritten"
    assert read_bytes(destination.with_name("audio.pcm.1")) == b"pcm"


# --- Links, junctions and reparse points at mutation time -------------------------------------
#
# ``idea gc --apply`` renames, creates, rewrites and deletes. Each regression below plants a link
# where GC mutates and proves that whatever the link names stays byte-identical.


def _ext(path: Path) -> str:
    """The unresolved extended spelling, so a link is inspected rather than followed."""

    absolute = os.path.abspath(path)
    if os.name != "nt" or absolute.startswith("\\\\"):
        return absolute
    return "\\\\?\\" + absolute


def _lexists(path: Path) -> bool:
    return os.path.lexists(_ext(path))


def _junction(link: Path, target: Path) -> bool:
    """A directory junction (no privilege needed), even where ``mklink`` hits its length limit."""

    if os.name == "nt":
        import _winapi

        try:
            _winapi.CreateJunction(str(target), _ext(link))
        except OSError:
            return _link(link, target)
        return _lexists(link)
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        return False
    return _lexists(link)


def _file_link(link: Path, target: Path) -> bool:
    """A file symlink; Windows needs Developer Mode or the privilege, so callers skip without."""

    try:
        os.symlink(str(target), _ext(link))
    except (OSError, NotImplementedError):
        return False
    return _lexists(link)


@contextlib.contextmanager
def _unlistable(directory: Path) -> Iterator[None]:
    """Deny listing *directory* for the current user, restoring access afterwards."""

    if os.name == "nt":
        user = os.environ.get("USERNAME", "")
        denied = subprocess.run(
            ["icacls", str(directory), "/deny", f"{user}:(RD)"], capture_output=True, check=False
        )
        try:
            if denied.returncode != 0:
                pytest.skip("icacls could not deny listing")
            try:
                os.listdir(_ext(directory))
                pytest.skip("listing is not deniable for this account")
            except PermissionError:
                pass
            yield
        finally:
            subprocess.run(
                ["icacls", str(directory), "/remove:d", user], capture_output=True, check=False
            )
        return
    mode = os.stat(directory).st_mode
    os.chmod(directory, 0)
    try:
        try:
            os.listdir(directory)
            pytest.skip("listing is not deniable for this account")
        except PermissionError:
            pass
        yield
    finally:
        os.chmod(directory, mode)


def test_linked_windows_directory_is_refused_not_its_target_moved(tmp_path: Path) -> None:
    """Regression (P0): the rename subject was resolved, so a junction's target was moved."""

    work = tmp_path / "w"
    media = _seed_media(work, "complete", age=timedelta(hours=1))
    kept = media / "kept-windows"
    _write(kept / "windows.gen0.jsonl", "{}\n")
    shutil.rmtree(_ext(media / "windows"))
    if not _junction(media / "windows", kept):
        pytest.skip("this platform will not create a directory link without extra privilege")
    before = _tree(kept)
    result = collect(work, policy="local", apply=True, now=NOW)
    assert _tree(kept) == before and before
    assert _lexists(media / "windows")
    assert ("skip", media / "windows") in [(a.operation, a.path) for a in result.actions]
    assert not _lexists(work / ".trash" / NOW.date().isoformat() / media.relative_to(work))


def test_linked_pcm_file_is_refused_not_its_target_moved(tmp_path: Path) -> None:
    work = tmp_path / "w"
    media = _seed_media(work, "failed", age=timedelta(hours=1))
    elsewhere = media / "decode/elsewhere.pcm"
    _write(elsewhere, b"not the pcm")
    pcm = media / "decode/audio.pcm"
    os.unlink(_ext(pcm))
    if not _file_link(pcm, elsewhere):
        pytest.skip("this platform will not create a file symlink without extra privilege")
    result = collect(work, policy="local", apply=True, now=NOW)
    assert read_bytes(elsewhere) == b"not the pcm"
    assert _lexists(pcm)
    assert ("skip", pcm) in [(action.operation, action.path) for action in result.actions]


def test_dangling_link_at_trash_destination_is_never_followed(tmp_path: Path) -> None:
    """Regression (P0): a dangling link read as a free name, and the rename followed it outside."""

    work = tmp_path / "w"
    media = _seed_media(work, "complete", age=timedelta(hours=1))
    window = read_bytes(media / "windows/windows.gen0.jsonl")
    outside = tmp_path / "outside"
    landing = outside / "landing"
    landing.mkdir(parents=True)
    slot = work / ".trash" / NOW.date().isoformat() / media.relative_to(work)
    os.makedirs(_ext(slot))
    if not _junction(slot / "windows", landing):
        pytest.skip("this platform will not create a directory link without extra privilege")
    landing.rmdir()
    collect(work, policy="local", apply=True, now=NOW)
    assert os.listdir(outside) == []
    assert not _exists(media / "windows")
    assert read_bytes(slot / "windows.1/windows.gen0.jsonl") == window


def test_linked_trash_ancestor_creates_nothing_outside(tmp_path: Path) -> None:
    """Regression (P0): ``makedirs`` ran before containment was checked, through a dated link."""

    work = tmp_path / "w"
    media = _seed_media(work, "complete", age=timedelta(hours=1))
    outside = tmp_path / "outside"
    outside.mkdir()
    os.makedirs(_ext(work / ".trash"))
    if not _junction(work / ".trash" / NOW.date().isoformat(), outside):
        pytest.skip("this platform will not create a directory link without extra privilege")
    planned = collect(work, policy="local", apply=False, now=NOW)
    result = collect(work, policy="local", apply=True, now=NOW)
    assert os.listdir(outside) == []
    assert _is_file(media / "windows/windows.gen0.jsonl")
    assert ("skip", media / "windows") in [(a.operation, a.path) for a in result.actions]
    assert result.actions == planned.actions


def test_linked_sidecar_is_never_written_through(tmp_path: Path) -> None:
    """Regression (P0): a ``*.done.json`` file symlink was rewritten, overwriting its target."""

    work = tmp_path / "w"
    media = _seed_media(work, "complete", age=timedelta(hours=1))
    sidecar = media / "recognise/observations.done.json"
    victim = tmp_path / "outside/victim.done.json"
    _write(victim, read_bytes(sidecar))
    os.unlink(_ext(sidecar))
    if not _file_link(sidecar, victim):
        pytest.skip("this platform will not create a file symlink without extra privilege")
    before = read_bytes(victim)
    collect(work, policy="local", apply=True, now=NOW)
    assert read_bytes(victim) == before
    assert _lexists(sidecar)


def test_linked_manifest_is_never_written_through(tmp_path: Path) -> None:
    """Regression (P0): the retention manifest was written through a planted file symlink."""

    work = tmp_path / "w"
    media = _seed_media(work, "complete", age=timedelta(hours=1))
    victim = tmp_path / "outside/victim.json"
    _write(victim, b"the owner's file")
    manifest = media / "retention-manifest.json"
    if not _file_link(manifest, victim):
        pytest.skip("this platform will not create a file symlink without extra privilege")
    planned = collect(work, policy="local", apply=False, now=NOW)
    result = collect(work, policy="local", apply=True, now=NOW)
    assert read_bytes(victim) == b"the owner's file"
    assert not _exists(media / "windows"), "the move itself is still recoverable and done"
    assert ("skip", manifest) in [(action.operation, action.path) for action in result.actions]
    assert result.actions == planned.actions, "the preview must announce the manifest refusal"


def test_trash_swapped_for_a_link_after_planning_is_not_purged(tmp_path: Path, monkeypatch) -> None:
    """Regression (P0): purge containment was checked only when planned, not when executed."""

    work = tmp_path / "w"
    _write(work / ".trash/2026-09-01/old/file", b"expired")
    outside = tmp_path / "outside"
    _write(outside / "2026-09-01/precious", b"not ours to delete")
    real_media_directories = retention._media_directories
    swapped: list[bool] = []

    def swap_then_scan(root: Path) -> list[Path]:
        os.replace(_ext(work / ".trash"), _ext(tmp_path / "moved-trash"))
        swapped.append(_junction(work / ".trash", outside))
        return real_media_directories(root)

    monkeypatch.setattr(retention, "_media_directories", swap_then_scan)
    result = collect(work, policy="local", apply=True, now=NOW)
    if not swapped[0]:
        pytest.skip("this platform will not create a directory link without extra privilege")
    assert read_bytes(outside / "2026-09-01/precious") == b"not ours to delete"
    assert [(action.operation, action.path) for action in result.actions] == [
        ("skip", work / ".trash/2026-09-01")
    ]


def test_unlistable_subtree_fails_closed_for_its_media(tmp_path: Path) -> None:
    """Regression (P1): an unreadable directory was skipped, so its sidecars were never sealed."""

    work = tmp_path / "w"
    media = _seed_media(work, "failed", age=timedelta(hours=1))
    with _unlistable(media / "recognise"):
        planned = collect(work, policy="local", apply=False, now=NOW)
        applied = collect(work, policy="local", apply=True, now=NOW)
    for result in (planned, applied):
        assert not [action for action in result.actions if action.operation == "move"]
        assert ("skip", media) in [(action.operation, action.path) for action in result.actions]
    assert _is_file(media / "windows/windows.gen0.jsonl")
    assert _is_file(media / "decode/audio.pcm")
    assert not _lexists(media / "retention-manifest.json")
    assert not _lexists(work / ".trash")


_ACQUIRE = """
import sys
from pathlib import Path
from id_detector.jobs import JobStoreLocked, ProcessLock
lock = ProcessLock(Path(sys.argv[1]))
try:
    lock.acquire()
except JobStoreLocked:
    print("locked")
else:
    print("acquired")
    lock.release()
"""


def test_media_lock_is_one_lock_for_two_spellings_past_max_path(tmp_path: Path) -> None:
    """GC and a pipeline that reach one long media directory by different paths share one lock."""

    from id_detector.jobs import ProcessLock

    real_parent = tmp_path / ("r" * 60)
    media = real_parent / ("s" * 64) / ("m" * 64) / ("e" * 40)
    _write(media / "entry.json", b"{}")
    holder = tmp_path / ("a" * 60)
    holder.mkdir()
    if not _junction(holder / "j", real_parent):
        pytest.skip("this platform will not create a directory link without extra privilege")
    real_lock = media / ".media.lock"
    alias_lock = holder / "j" / media.relative_to(real_parent) / ".media.lock"
    # The literal Win32 extended spelling ``\\?\C:\...`` that the I/O helpers pass around.
    extended_lock = _ext(real_lock)
    if os.name == "nt":
        assert extended_lock.startswith("\\\\?\\")
        assert len(str(real_lock)) > 260 and len(str(alias_lock)) > 260
    spellings = [str(real_lock), str(alias_lock), extended_lock]
    keys = {ProcessLock(Path(spelling)).path for spelling in spellings}
    assert keys == {ProcessLock(real_lock).path}

    def other_process(spelling: str) -> str:
        completed = subprocess.run(
            [sys.executable, "-c", _ACQUIRE, spelling],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        return completed.stdout.strip() or completed.stderr

    for held_spelling in spellings:
        held = ProcessLock(Path(held_spelling))
        held.acquire()
        try:
            for other in spellings:
                assert other_process(other) == "locked", (held_spelling, other)
        finally:
            held.release()
    for other in spellings:
        assert other_process(other) == "acquired", other

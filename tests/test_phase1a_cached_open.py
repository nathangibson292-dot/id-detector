"""Phase 1a-iii: retained results, rebuildable index, local audio and Windows paths."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from id_detector.cli import _load_cached
from id_detector.io import read_bytes, read_text
from id_detector.present.bundles import result_dir
from id_detector.present.index import load_index
from id_detector.present.server import serve_in_background
from tests.test_phase1a_bundles import publish, seed
from tests.test_stage7_page import _source


def test_cached_open_without_original_or_pcm_uses_manifest(tmp_path: Path) -> None:
    media = seed(tmp_path)
    directory = publish(media)
    source = _source("soundcloud")
    original = media / source.original.path
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"cached audio")
    original.unlink()
    assert not (media / "decode/pcm.json").exists()
    cached = _load_cached(tmp_path, source.input_url)
    assert cached is not None
    assert cached.record == source
    assert not cached.original_path.exists()
    assert cached.source_path == directory / "source.json"
    with_server = serve_in_background(tmp_path, port=0)
    try:
        response = httpx.get(
            f"{with_server.base_url}/{source.source_key}/{source.media_key}/present/index.html"
        )
        assert response.status_code == 200
        assert f"/media/{source.media_key}/audio" in response.text
        assert (
            httpx.get(f"{with_server.base_url}/media/{source.media_key}/audio").status_code == 404
        )
    finally:
        with_server.shutdown()


@pytest.mark.parametrize("damage", ["deleted", "invalid-json", "stale", "missing-entry"])
def test_index_rebuilds_when_missing_invalid_or_stale(tmp_path: Path, damage: str) -> None:
    media = seed(tmp_path)
    publish(media)
    index = tmp_path / "index.json"
    if damage == "deleted":
        index.unlink()
    elif damage == "invalid-json":
        index.write_bytes(b"{")
    elif damage == "missing-entry":
        stale = json.loads(read_text(index))
        stale["media"] = {}
        index.write_text(json.dumps(stale), encoding="utf-8")
    else:
        publish(media, "new-run", metadata={"started_at": "2026-09-12T00:00:00Z"})
        stale = json.loads(read_text(index))
        stale["tree_stamp"] = []
        index.write_text(json.dumps(stale), encoding="utf-8")
    cached = _load_cached(tmp_path, _source("soundcloud").media_key)
    assert cached is not None
    entry = json.loads(read_text(index))["media"][cached.record.media_key]
    assert entry["bundle"] == result_dir(media).name
    assert entry["source_key"] == cached.record.source_key


def test_legacy_library_and_cli_open_without_rewriting_files(tmp_path: Path) -> None:
    media = seed(tmp_path)
    source = _source("soundcloud")
    before = {p: read_bytes(p) for p in media.rglob("*") if p.is_file()}
    assert _load_cached(tmp_path, source.input_url) is not None
    assert load_index(tmp_path)["media"][source.media_key]["bundle"] is None
    server = serve_in_background(tmp_path, port=0)
    try:
        assert source.title in httpx.get(server.base_url).text
        response = httpx.get(
            f"{server.base_url}/{source.source_key}/{source.media_key}/present/index.html"
        )
        assert response.status_code == 200
    finally:
        server.shutdown()
    assert {p: read_bytes(p) for p in media.rglob("*") if p.is_file()} == before


def test_audio_bytes_ranges_head_and_traversal(tmp_path: Path) -> None:
    media = seed(tmp_path)
    publish(media)
    source = _source("soundcloud")
    (media / source.original.path).parent.mkdir(parents=True, exist_ok=True)
    (media / source.original.path).write_bytes(b"0123456789")
    server = serve_in_background(tmp_path, port=0)
    route = f"{server.base_url}/media/{source.media_key}/audio"
    try:
        assert httpx.get(route).content == b"0123456789"
        response = httpx.get(route, headers={"Range": "bytes=2-5"})
        assert response.status_code == 206 and response.content == b"2345"
        assert response.headers["content-range"] == "bytes 2-5/10"
        assert httpx.get(route, headers={"Range": "bytes=-3"}).content == b"789"
        assert httpx.get(route, headers={"Range": "bytes=8-"}).content == b"89"
        assert httpx.get(route, headers={"Range": "bytes=20-"}).status_code == 416
        response = httpx.head(route)
        assert response.status_code == 200 and response.content == b""
        assert response.headers["content-length"] == "10"
        for key in ("f" * 64, "%2e%2e", "..%5c..", "C:%5cWindows", "%2fetc%2fpasswd"):
            assert httpx.get(f"{server.base_url}/media/{key}/audio").status_code == 404
    finally:
        server.shutdown()


def test_audio_rejects_source_path_escape(tmp_path: Path) -> None:
    media = seed(tmp_path)
    source_path = media / "ingest/source.json"
    source = json.loads(read_text(source_path))
    source["original"]["path"] = "../../outside.wav"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    (tmp_path / "outside.wav").write_bytes(b"private")
    server = serve_in_background(tmp_path, port=0)
    try:
        route = f"{server.base_url}/media/{source['media_key']}/audio"
        assert httpx.get(route).status_code == 404
    finally:
        server.shutdown()


def test_deleted_local_input_opens_by_path_and_rebuilds_missing_current(tmp_path: Path) -> None:
    from id_detector.io import native_path
    from scripts.make_golden import run_local_free

    tracklist = run_local_free(tmp_path / "work")
    media = tracklist.parents[3]
    source_path = tracklist.parent / "source.json"
    source = json.loads(read_text(source_path))
    # The cached-open fixture names a vanished local input; no bytes need to be hashed to find it.
    missing_input = tmp_path / "deleted.wav"
    source["input_url"] = str(missing_input)
    source["canonical_url"] = missing_input.resolve().as_uri()
    from id_detector.contracts import derive_source_key
    from id_detector.io import atomic_write_json

    source["source_key"] = derive_source_key(source["canonical_url"])
    legacy = Path(native_path(tmp_path / "legacy" / source["source_key"] / source["media_key"]))
    atomic_write_json(legacy / "ingest/source.json", source)
    (legacy / "present").mkdir()
    (legacy / "present/index.html").write_bytes(b"legacy")
    assert _load_cached(tmp_path / "legacy", str(missing_input)) is not None
    (media / "present/current").unlink()
    (tmp_path / "work/index.json").unlink()
    assert _load_cached(tmp_path / "work", media.name) is not None
    assert result_dir(media) == tracklist.parent


def test_web_runner_publishes_bundle_result_url(tmp_path: Path, monkeypatch) -> None:
    from id_detector import cli
    from id_detector.webapp.runner import make_pipeline_runner
    from scripts.make_golden import AUDIO, run_local_free

    tracklist = run_local_free(tmp_path / "work")

    async def analysed(*args, **kwargs):
        return 0

    monkeypatch.setattr(cli, "_analyse", analysed)

    class Context:
        target = str(AUDIO)
        build_index = False
        profile = None
        known_tracklist = None
        acquire = False
        cancel_token = None
        result = None

        def progress(self, *args):
            pass

        def set_result(self, path):
            self.result = path

    context = Context()
    runner = make_pipeline_runner(tmp_path / "work", config_path=tmp_path / "missing.toml")
    runner(context)
    assert context.result == tracklist.parent / "index.html"


def test_index_write_failure_does_not_prevent_cached_open(tmp_path: Path, monkeypatch) -> None:
    from id_detector.present import index

    media = seed(tmp_path)
    publish(media)
    (tmp_path / "index.json").unlink()

    def read_only(*args):
        raise PermissionError("index is read-only")

    monkeypatch.setattr(index, "atomic_write_json", read_only)
    assert _load_cached(tmp_path, media.name) is not None
    assert not (tmp_path / "index.json").exists()


def test_non_local_targets_never_raise_from_the_cached_lookup(tmp_path: Path) -> None:
    """Anything the web form accepts reaches ``_load_cached``; a non-path target is a miss."""

    media = seed(tmp_path)
    publish(media)
    for target in ("http://[::1]/mix", "~nosuchuser/mix.mp3", chr(0) + "bad", "", "C:"):
        assert _load_cached(tmp_path, target) is None


def test_warm_index_does_not_rehash_every_bundle_on_a_cached_open(
    tmp_path: Path, monkeypatch
) -> None:
    """One audio range request must not cost a full re-hash of the whole library."""

    from id_detector.present import index as index_module

    media = seed(tmp_path)
    publish(media)
    load_index(tmp_path)  # warm: the stamp now matches the tree
    verified: list[object] = []
    for name in ("result_dir", "read_bundle_manifest"):
        monkeypatch.setattr(index_module, name, lambda *a, **k: verified.append(a))
    assert load_index(tmp_path)["media"]
    assert verified == []


def test_job_audio_is_only_served_for_a_verified_original(tmp_path: Path) -> None:
    from id_detector.present.server import _resolve_job_audio

    media = seed(tmp_path)
    publish(media)
    source = _source("soundcloud")
    original = media / source.original.path
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"not the fetched bytes")

    class _Job:
        target = source.input_url
        audio_path = None
        status = "running"
        phase = "recognise"
        phase_done = 1
        phase_total = 1

    assert _resolve_job_audio(_Job(), tmp_path) is None


def test_every_source_alias_for_the_same_audio_stays_findable(tmp_path: Path) -> None:
    """Two URLs that yield identical bytes share a media key but not a directory."""

    from tests.test_stage7_server import _seed_work_root

    seed(tmp_path)
    soundcloud, other = _source("soundcloud"), _source("mixcloud")
    assert soundcloud.media_key == other.media_key
    assert soundcloud.source_key != other.source_key
    _seed_work_root(tmp_path, other)
    # No original was ever written, so only the index can resolve either alias.
    first, second = (
        _load_cached(tmp_path, soundcloud.input_url),
        _load_cached(tmp_path, other.input_url),
    )
    assert first is not None and second is not None
    assert first.media_dir.parent.name == soundcloud.source_key
    assert second.media_dir.parent.name == other.source_key
    aliases = load_index(tmp_path)["media"][soundcloud.media_key]["aliases"]
    assert {alias["source_key"] for alias in aliases} == {
        soundcloud.source_key,
        other.source_key,
    }


def test_repeated_cached_open_on_an_unwritable_work_root_rebuilds_once(
    tmp_path: Path, monkeypatch
) -> None:
    """A work root that cannot be written is slow once, not once per audio range request."""

    from id_detector.present import index as index_module

    media = seed(tmp_path)
    publish(media)
    (tmp_path / "index.json").unlink()

    def read_only(*args: object) -> None:
        raise PermissionError("index is read-only")

    rebuilds: list[object] = []
    real = index_module.rebuild_index
    monkeypatch.setattr(index_module, "atomic_write_json", read_only)
    monkeypatch.setattr(
        index_module, "rebuild_index", lambda root: (rebuilds.append(root), real(root))[1]
    )
    for _ in range(3):
        assert _load_cached(tmp_path, media.name) is not None
    assert len(rebuilds) == 1
    assert not (tmp_path / "index.json").exists()

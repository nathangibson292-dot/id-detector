"""Stage 7 — local server (loopback-only, read-only) and the rescan queue.

These tests are deterministic and use only the loopback interface; they never touch an external
network or provider.  The server always runs on a background thread with a bounded lifetime and is
torn down (with a join timeout) so no test can leak a hung process.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from id_detector.contracts import SourceRecord
from id_detector.io import path_is_file
from id_detector.present.page import generate_page
from id_detector.present.server import (
    append_rescan_request,
    build_rescan_request,
    consume_rescan_queue,
    make_server,
    read_rescan_queue,
    serve_in_background,
)
from tests.test_stage7_page import _episodes_file, _identities, _source

TIMEOUT = httpx.Timeout(5.0)


def _seed_work_root(root: Path, source: SourceRecord) -> Path:
    media_dir = root / source.source_key / source.media_key
    (media_dir / "ingest").mkdir(parents=True)
    (media_dir / "fuse").mkdir(parents=True)
    (media_dir / "ingest" / "source.json").write_bytes(source.model_dump_json().encode("utf-8"))
    episodes = _episodes_file()
    identities = _identities()
    episodes_path = media_dir / "fuse" / "episodes.json"
    identities_path = media_dir / "fuse" / "identities.gen0.json"
    episodes_path.write_bytes(episodes.model_dump_json().encode("utf-8"))
    identities_path.write_bytes(identities.model_dump_json().encode("utf-8"))
    generate_page(
        media_dir=media_dir,
        source=source,
        episodes=episodes,
        identities=identities,
        duration_ms=3_600_000,
        episodes_path=episodes_path,
        identities_path=identities_path,
        # This server fixture pins one served row per episode; collapse is covered in test_collapse.
        collapse=False,
    )
    return media_dir


def test_server_only_binds_loopback(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback"):
        make_server(tmp_path, host="0.0.0.0", port=0)


def test_server_serves_index_page_and_pages_and_blocks_traversal(tmp_path: Path) -> None:
    source = _source("soundcloud")
    _seed_work_root(tmp_path, source)
    running = serve_in_background(tmp_path, port=0)
    try:
        base = running.base_url
        index = httpx.get(base + "/", timeout=TIMEOUT)
        assert index.status_code == 200
        assert "Fixture Live Set" in index.text
        assert "Analysed sets" in index.text

        page_url = f"{base}/{source.source_key}/{source.media_key}/present/index.html"
        page = httpx.get(page_url, timeout=TIMEOUT)
        assert page.status_code == 200
        # The live page renders every episode row and every gap row.
        assert page.text.count('<tr class="track"') == len(_episodes_file().episodes)
        assert page.text.count('<tr class="gap"') == len(_episodes_file().gaps)

        # Only present/ files with known suffixes are served; source.json (outside present/) is not.
        leak = httpx.get(
            f"{base}/{source.source_key}/{source.media_key}/ingest/source.json", timeout=TIMEOUT
        )
        assert leak.status_code == 404
        traversal = httpx.get(base + "/../../pyproject.toml", timeout=TIMEOUT)
        assert traversal.status_code == 404
    finally:
        running.shutdown()
    assert not running.thread.is_alive()


def test_rescan_endpoint_appends_to_queue_without_provider_calls(tmp_path: Path) -> None:
    source = _source("soundcloud")
    media_dir = _seed_work_root(tmp_path, source)
    running = serve_in_background(tmp_path, port=0)
    try:
        body = {
            "media_key": source.media_key,
            "trigger": "gap",
            "start_ms": 150_000,
            "end_ms": 600_000,
        }
        first = httpx.post(running.base_url + "/rescan", json=body, timeout=TIMEOUT)
        assert first.status_code == 200
        assert first.json()["queued"] is True

        queued = read_rescan_queue(media_dir)
        assert len(queued) == 1
        assert queued[0].trigger == "gap"
        assert queued[0].start_ms == 150_000
        assert queued[0].end_ms == 600_000

        # Re-posting the identical request is de-duplicated by id.
        again = httpx.post(running.base_url + "/rescan", json=body, timeout=TIMEOUT)
        assert again.status_code == 200
        assert len(read_rescan_queue(media_dir)) == 1

        # A well-formed request for an unknown media_key is rejected.
        bad = httpx.post(
            running.base_url + "/rescan",
            json={**body, "media_key": "f" * 64},
            timeout=TIMEOUT,
        )
        assert bad.status_code == 404
        # A malformed media_key is a 400.
        malformed = httpx.post(
            running.base_url + "/rescan", json={**body, "media_key": "nope"}, timeout=TIMEOUT
        )
        assert malformed.status_code == 400
    finally:
        running.shutdown()


def test_rescan_queue_roundtrip_and_consume(tmp_path: Path) -> None:
    source = _source("mixcloud")
    media_dir = tmp_path / "wr" / source.source_key / source.media_key
    (media_dir / "fuse").mkdir(parents=True)
    request = build_rescan_request(
        source=source, media_dir=media_dir, trigger="edge", start_ms=30_000, end_ms=42_000
    )
    append_rescan_request(media_dir, request)
    assert len(read_rescan_queue(media_dir)) == 1

    consumed = consume_rescan_queue(media_dir)
    assert len(consumed) == 1
    # The queue is emptied and archived; a second consume yields nothing.
    assert read_rescan_queue(media_dir) == []
    assert path_is_file(media_dir / "present" / "rescan_queue.consumed.jsonl")
    assert consume_rescan_queue(media_dir) == []


def test_build_rescan_request_rejects_unknown_trigger(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="trigger"):
        build_rescan_request(
            source=_source("soundcloud"),
            media_dir=tmp_path,
            trigger="not_a_trigger",
            start_ms=0,
            end_ms=1_000,
        )


def test_build_rescan_request_hashes_episodes_when_present(tmp_path: Path) -> None:
    source = _source("soundcloud")
    media_dir = tmp_path / source.source_key / source.media_key
    (media_dir / "fuse").mkdir(parents=True)
    episodes = _episodes_file()
    (media_dir / "fuse" / "episodes.json").write_bytes(episodes.model_dump_json().encode("utf-8"))
    request = build_rescan_request(
        source=source, media_dir=media_dir, trigger="gap", start_ms=10_000, end_ms=20_000
    )
    assert "fuse/episodes.json" in request.input_hashes
    assert request.generation == episodes.generation
    # The record is schema-valid and its id is deterministic.
    assert json.loads(request.model_dump_json())["trigger"] == "gap"


def test_stale_result_page_is_regenerated_on_open(tmp_path: Path) -> None:
    """A page written by an older build (no version stamp) is re-rendered from the artefacts the
    first time the server serves it — no re-analysis, and the artefacts themselves are untouched."""

    from id_detector.present.page import PAGE_VERSION
    from id_detector.present.refresh import ensure_fresh_page, page_version

    source = _source("soundcloud")
    media_dir = _seed_work_root(tmp_path, source)
    (media_dir / "decode").mkdir()
    golden_pcm = Path(__file__).resolve().parents[1] / "tests" / "golden" / "pcm.json"
    (media_dir / "decode" / "pcm.json").write_bytes(golden_pcm.read_bytes())
    index_html = media_dir / "present" / "index.html"
    episodes_before = (media_dir / "fuse" / "episodes.json").read_bytes()

    # A fresh page is left alone.
    assert page_version(index_html) == PAGE_VERSION
    assert ensure_fresh_page(media_dir) is False

    # An old page (pre-stamp) is rewritten when served.
    index_html.write_bytes(b"<!doctype html><title>old page</title><p>Fixture Live Set</p>")
    assert page_version(index_html) == 0
    running = serve_in_background(tmp_path, port=0)
    try:
        page_url = f"{running.base_url}/{source.source_key}/{source.media_key}/present/index.html"
        page = httpx.get(page_url, timeout=TIMEOUT)
        assert page.status_code == 200
        assert f'<meta name="id-detector-page" content="{PAGE_VERSION}">' in page.text
        assert '<tr class="track"' in page.text
    finally:
        running.shutdown()
    assert page_version(index_html) == PAGE_VERSION
    assert (media_dir / "fuse" / "episodes.json").read_bytes() == episodes_before

    # A page that cannot be regenerated (artefact missing) is served as-is, never an error.
    index_html.write_bytes(b"<!doctype html><title>old page</title>")
    (media_dir / "decode" / "pcm.json").unlink()
    assert ensure_fresh_page(media_dir) is False
    assert index_html.read_bytes().startswith(b"<!doctype html><title>old page</title>")

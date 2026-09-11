"""Phase 1a-i: immutable bundles, durable publication and frozen refresh inputs."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from id_detector.io import canonical_json_bytes, native_path, read_bytes, read_text
from id_detector.present import bundles, page, refresh
from id_detector.providers.base import AppConfig
from tests.test_stage7_page import _episodes_file, _identities, _source
from tests.test_stage7_server import _seed_work_root


def seed(root: Path) -> Path:
    return Path(native_path(_seed_work_root(root, _source("soundcloud"))))


def publish(media_dir: Path, run: str = "run-1", **kwargs: object) -> Path:
    metadata = {
        "run_id": run,
        "started_at": "2026-09-11T00:00:00Z",
        "status": "complete",
        "requested_recipe_id": "a" * 64,
        "achieved": "free",
        "pricing_version": "v1",
        "usd_e2_spent": 0,
    }
    metadata.update(kwargs.pop("metadata", {}))
    return bundles.publish_result(
        media_dir=media_dir,
        source=_source("soundcloud"),
        episodes=_episodes_file(),
        identities=_identities(),
        duration_ms=3_600_000,
        metadata=metadata,
        config=AppConfig(),
        **kwargs,
    )


def test_bundle_identity_and_manifests_cover_every_file(tmp_path: Path) -> None:
    media = seed(tmp_path)
    directory = publish(media)
    assert directory.name == sha256(canonical_json_bytes(["run-1", page.PAGE_VERSION])).hexdigest()
    for home in (directory, media / "fuse/runs/run-1"):
        manifest = json.loads(read_text(home / "manifest.json"))
        assert set(manifest["files"]) == {
            p.relative_to(home).as_posix()
            for p in home.rglob("*")
            if p.is_file() and p.name != "manifest.json"
        }
        for name, item in manifest["files"].items():
            raw = read_bytes(home / name)
            assert item == {"sha256": sha256(raw).hexdigest(), "size": len(raw)}
    manifest = bundles.read_manifest(directory)
    assert manifest["analysis_key"] is None
    assert manifest["requested_recipe_id"] == "a" * 64
    assert manifest["achieved"] == "free"
    assert manifest["pricing_version"] == "v1"
    assert manifest["usd_e2_spent"] == 0
    assert read_bytes(media / "fuse/episodes.json") == read_bytes(
        media / "fuse/runs/run-1/episodes.json"
    )


def test_refresh_creates_second_bundle_and_reuses_same_version(tmp_path: Path, monkeypatch) -> None:
    media = seed(tmp_path)
    first = publish(media)
    before = {p.name: read_bytes(p) for p in first.iterdir()}
    # Refresh must use the run snapshot, even after the mutable newest-run files change/disappear.
    (media / "fuse/episodes.json").unlink()
    monkeypatch.setattr(page, "PAGE_VERSION", page.PAGE_VERSION + 1)
    second = refresh.regenerate_page(media)
    assert second.parent != first
    assert len(list((media / "present/bundles").iterdir())) == 2
    assert read_text(media / "present/current") == second.parent.name
    assert bundles.read_manifest(second.parent)["run_id"] == "run-1"
    assert {p.name: read_bytes(p) for p in first.iterdir()} == before
    assert refresh.regenerate_page(media) == second


def test_current_tracks_newest_complete_not_latest_partial_or_old_refresh(tmp_path: Path) -> None:
    media = seed(tmp_path)
    publish(media)
    newest = publish(media, "run-2", metadata={"started_at": "2026-09-12T00:00:00Z"})
    publish(media, "run-3", metadata={"status": "partial", "started_at": "2026-09-13T00:00:00Z"})
    publish(media, presentation_version=page.PAGE_VERSION + 1)
    assert read_text(media / "present/current") == newest.name
    assert bundles.result_dir(media) == newest


def test_failure_after_files_before_pointer_leaves_no_dangling_reference(
    tmp_path: Path, monkeypatch
) -> None:
    media = seed(tmp_path)
    first = publish(media)
    flushes = []
    real_flush = bundles.fsync_directory

    def flushed(path):
        flushes.append(path)
        real_flush(path)

    def crash(media_dir, directory, metadata):
        assert bundles.read_manifest(directory)
        assert bundles.read_manifest(media_dir / "fuse/runs/run-2")
        assert directory in flushes and directory.parent in flushes
        raise RuntimeError("crash before pointer")

    monkeypatch.setattr(bundles, "fsync_directory", flushed)
    monkeypatch.setattr(bundles, "_publish_current", crash)
    with pytest.raises(RuntimeError, match="before pointer"):
        publish(media, "run-2")
    assert read_text(media / "present/current") == first.name
    assert not (media / "invocations.jsonl").exists()


def test_pipeline_journal_names_only_durable_files(tmp_path: Path, monkeypatch) -> None:
    from id_detector import cli
    from scripts.make_golden import run_local_free

    original_append = cli.append_invocation
    observed = []

    def append(path, entry):
        if entry.bundle_id:
            directory = path.parent / "present/bundles" / entry.bundle_id
            assert bundles.read_manifest(directory)
            assert bundles.read_manifest(path.parent / entry.fuse_run)
            observed.append(entry.bundle_id)
        original_append(path, entry)

    monkeypatch.setattr(cli, "append_invocation", append)
    run_local_free(tmp_path / "work")
    assert len(observed) == 1


def test_hosted_shared_publication_needs_no_pointer_and_preserves_analysis_key(
    tmp_path: Path,
) -> None:
    media = seed(tmp_path)
    directory = publish(media, local=False, metadata={"analysis_key": "journal-value"})
    assert bundles.read_manifest(directory)["analysis_key"] == "journal-value"
    assert not (media / "present/current").exists()
    assert not (tmp_path / "index.json").exists()


def test_damaged_or_unsealed_bundle_is_never_overwritten_and_publication_recovers(
    tmp_path: Path,
) -> None:
    media = seed(tmp_path)
    directory = publish(media)
    (directory / "index.html").write_bytes(b"damaged")
    # An unsealed/damaged directory is not a bundle: never rewritten, never a permanent block.
    recovered = publish(media)
    assert recovered != directory
    assert (directory / "index.html").read_bytes() == b"damaged"
    assert bundles.read_manifest(recovered)["run_id"] == "run-1"
    assert bundles.result_dir(media) == recovered
    assert read_text(media / "present/current") == recovered.name


def test_interrupted_fuse_freeze_is_completed_rather_than_refused(tmp_path: Path) -> None:
    media = seed(tmp_path)
    # A crash between mkdir and the seal leaves an unreferenced, unsealed frozen-run directory.
    (media / "fuse/runs/run-1").mkdir(parents=True)
    (media / "fuse/runs/run-1/episodes.json").write_bytes(b"half-written")
    directory = publish(media)
    assert bundles.read_manifest(media / "fuse/runs/run-1")
    assert read_bytes(media / "fuse/runs/run-1/episodes.json") == read_bytes(
        media / "fuse/episodes.json"
    )
    assert bundles.read_manifest(directory)["fuse_run"] == "fuse/runs/run-1"


def test_degraded_result_is_shown_but_never_becomes_the_served_pointer(tmp_path: Path) -> None:
    """Plan §2.3.5: ``complete|degraded|partial`` are shown; only ``complete`` may be served."""

    media = seed(tmp_path)
    for stale in (media / "present").glob("*"):
        if stale.is_file():
            stale.unlink()  # a post-bundle run writes no flat present/ files at all
    degraded = publish(media, "run-d", metadata={"status": "degraded", "reason": "secondary"})
    assert not (media / "present/current").exists()
    assert bundles.result_dir(media) == degraded
    assert (degraded / "index.html").is_file() and (degraded / "tracklist.json").is_file()
    partial = publish(media, "run-p", metadata={"status": "partial", "started_at": "2026-09-12"})
    assert bundles.result_dir(media) == partial
    assert not (media / "present/current").exists()
    complete = publish(media, "run-c", metadata={"started_at": "2026-09-10T00:00:00Z"})
    assert read_text(media / "present/current") == complete.name
    assert bundles.result_dir(media) == complete


def test_degraded_result_opens_from_the_library_the_url_and_the_cli(tmp_path: Path) -> None:
    import httpx

    from id_detector.cli import _load_cached
    from id_detector.present.index import load_index
    from id_detector.present.server import _discover_sets, serve_in_background
    from tests.test_stage7_page import _source

    media = seed(tmp_path)
    for stale in (media / "present").glob("*"):
        if stale.is_file():
            stale.unlink()
    source = _source("soundcloud")
    publish(media, "run-d", metadata={"status": "degraded", "reason": "secondary"})
    assert len(_discover_sets(tmp_path)) == 1
    entry = load_index(tmp_path)["media"][source.media_key]
    assert entry["newest_complete_run"] is None  # shown, never recorded as a servable run
    assert _load_cached(tmp_path, source.input_url) is not None
    server = serve_in_background(tmp_path, port=0)
    try:
        assert source.title in httpx.get(server.base_url).text
        base = f"{server.base_url}/{source.source_key}/{source.media_key}/present"
        assert httpx.get(f"{base}/index.html").status_code == 200
        assert httpx.get(f"{base}/tracklist.json").status_code == 200
    finally:
        server.shutdown()


def test_legacy_journal_parses_without_bundle_references() -> None:
    from id_detector.contracts import InvocationJournalEntry

    path = Path(__file__).parent / "golden/invocation_journal_entry.json"
    legacy = json.loads(read_text(path))
    legacy.pop("bundle_id")
    legacy.pop("fuse_run")
    parsed = InvocationJournalEntry.model_validate(legacy)
    assert parsed.bundle_id is None and parsed.fuse_run is None


def test_pipeline_failure_before_pointer_has_no_bundle_journal_reference(
    tmp_path: Path, monkeypatch
) -> None:
    from scripts.make_golden import run_local_free

    def crash(*args):
        raise RuntimeError("before pointer")

    monkeypatch.setattr(bundles, "_publish_current", crash)
    with pytest.raises(RuntimeError, match="before pointer"):
        run_local_free(tmp_path / "work")
    (journal,) = (tmp_path / "work").glob("*/*/invocations.jsonl")
    entry = json.loads(read_text(journal).splitlines()[-1])
    assert entry["status"] == "failed"
    assert entry["bundle_id"] is None and entry["fuse_run"] is None
    assert not (journal.parent / "present/current").exists()


def test_changed_preferences_create_revision_without_rewriting_original(tmp_path: Path) -> None:
    media = seed(tmp_path)
    first = publish(media)
    before = read_bytes(first / "index.html")
    second = refresh.regenerate_page(media, config=AppConfig(lead_in_ms=9_000))
    assert second.parent != first
    assert bundles.read_manifest(second.parent)["presentation_version"] > page.PAGE_VERSION
    assert read_bytes(first / "index.html") == before
    assert refresh.page_version(second) == page.PAGE_VERSION
    assert refresh.regenerate_page(media, config=AppConfig(lead_in_ms=9_000)) == second
    third = refresh.regenerate_page(media, config=AppConfig())
    assert third.parent not in {first, second.parent}
    assert bundles.result_dir(media) == third.parent


def test_acquisition_creates_a_new_bundle_and_job_follows_it(tmp_path: Path) -> None:
    from id_detector.contracts import AcquireFile
    from id_detector.io import atomic_write_bytes

    media = seed(tmp_path)
    first = publish(media)
    acquire = AcquireFile.model_validate_json(
        read_text(Path(__file__).parent / "golden/acquire.json")
    )
    second = publish(media, acquire=acquire)
    assert second != first
    assert "acquire.json" in bundles.read_manifest(second)["files"]
    assert "acquire.json" not in bundles.read_manifest(first)["files"]
    atomic_write_bytes(
        media / "invocations.jsonl", canonical_json_bytes({"bundle_id": first.name}) + b"\n"
    )
    assert bundles.shown_result_dir(media) == second


def _other_episodes():
    """An episodes file whose rows are visibly different from the shared fixture's."""

    episodes = _episodes_file()
    trimmed = episodes.model_copy(update={"episodes": episodes.episodes[:1]})
    assert len(trimmed.episodes) != len(episodes.episodes)
    return trimmed


def test_acquire_never_publishes_a_newer_partial_run_under_an_older_complete_one(
    tmp_path: Path, monkeypatch
) -> None:
    """complete C, then partial P, then `idea acquire`: C must never gain P's rows."""

    from id_detector.contracts import AcquireFile
    from id_detector.io import atomic_write_json

    media = seed(tmp_path)
    complete = publish(media, "run-c", metadata={"started_at": "2026-09-11T00:00:00Z"})
    # A later partial run leaves its own rows in the mutable flat fuse tree, as the pipeline does.
    partial_rows = _other_episodes()
    atomic_write_json(media / "fuse/episodes.json", partial_rows)
    bundles.publish_result(
        media_dir=media,
        source=_source("soundcloud"),
        episodes=partial_rows,
        identities=_identities(),
        duration_ms=3_600_000,
        metadata={"run_id": "run-p", "started_at": "2026-09-12T00:00:00Z", "status": "partial"},
        config=AppConfig(),
    )
    acquire = AcquireFile.model_validate_json(
        read_text(Path(__file__).parent / "golden/acquire.json")
    )
    snapshot = bundles.load_run_snapshot(media)
    assert snapshot.metadata["run_id"] == "run-c"
    assert snapshot.episodes == _episodes_file()  # C's frozen rows, not the flat partial ones
    published = bundles.publish_snapshot(
        snapshot, media_dir=media, config=AppConfig(), acquire=acquire
    )
    manifest = bundles.read_bundle_manifest(published)
    assert manifest["run_id"] == "run-c" and manifest["status"] == "complete"
    assert read_bytes(published / "tracklist.json") != read_bytes(
        bundles.result_dir(media) / "tracklist.json"
    ) or published == bundles.result_dir(media)
    rendered = json.loads(read_text(published / "tracklist.json"))
    frozen = json.loads(read_text(media / "fuse/runs/run-c/episodes.json"))
    assert len(frozen["episodes"]) == len(_episodes_file().episodes)
    assert rendered["entries"]
    # The pointer still names a complete run's own revision, never the partial run's rows.
    assert read_text(media / "present/current") == published.name
    assert bundles.read_bundle_manifest(bundles.result_dir(media))["run_id"] == "run-c"
    assert read_bytes(media / "fuse/episodes.json") == canonical_json_bytes(partial_rows)
    assert complete != published


def test_refresh_renders_the_selected_run_not_the_newest_mutable_fuse(tmp_path: Path) -> None:
    """Refresh has the same split-resolution risk as acquisition, and must resolve once too."""

    from id_detector.io import atomic_write_json

    media = seed(tmp_path)
    publish(media, "run-c", metadata={"started_at": "2026-09-11T00:00:00Z"})
    partial_rows = _other_episodes()
    atomic_write_json(media / "fuse/episodes.json", partial_rows)
    bundles.publish_result(
        media_dir=media,
        source=_source("soundcloud"),
        episodes=partial_rows,
        identities=_identities(),
        duration_ms=3_600_000,
        metadata={"run_id": "run-p", "started_at": "2026-09-12T00:00:00Z", "status": "partial"},
        config=AppConfig(),
    )
    regenerated = refresh.regenerate_page(media, config=AppConfig(lead_in_ms=7_000))
    manifest = bundles.read_bundle_manifest(regenerated.parent)
    assert manifest["run_id"] == "run-c" and manifest["status"] == "complete"
    rows = json.loads(read_text(regenerated.parent / "tracklist.json"))
    frozen = json.loads(read_text(media / "fuse/runs/run-c/episodes.json"))
    assert len(frozen["episodes"]) == len(_episodes_file().episodes)
    assert len(rows["entries"]) == len(
        json.loads(read_text(bundles.result_dir(media) / "tracklist.json"))["entries"]
    )
    assert read_text(media / "present/current") == regenerated.parent.name


def test_a_run_published_between_snapshot_and_publish_keeps_the_newest_pointer(
    tmp_path: Path,
) -> None:
    """A concurrent publication may win the pointer; it may never change what we render."""

    media = seed(tmp_path)
    publish(media, "run-c", metadata={"started_at": "2026-09-11T00:00:00Z"})
    snapshot = bundles.load_run_snapshot(media)
    assert snapshot.metadata["run_id"] == "run-c"
    newest = publish(media, "run-n", metadata={"started_at": "2026-09-14T00:00:00Z"})
    published = bundles.publish_snapshot(
        snapshot, media_dir=media, config=AppConfig(lead_in_ms=7_000)
    )
    assert bundles.read_bundle_manifest(published)["run_id"] == "run-c"
    # The revision of the older run never demotes the newer complete run's pointer.
    assert read_text(media / "present/current") == newest.name
    assert bundles.result_dir(media) == newest


def test_a_previously_sealed_but_damaged_frozen_run_is_never_resealed(tmp_path: Path) -> None:
    media = seed(tmp_path)
    publish(media, "run-1")
    frozen = media / "fuse/runs/run-1/episodes.json"
    before = read_bytes(frozen)
    frozen.write_bytes(b"damaged")
    # The mutable tree has moved on; re-deriving the frozen inputs from it would silently rewrite
    # the snapshot that the already-published bundle refreshes from.
    (media / "fuse/episodes.json").write_bytes(b'{"schema_version":"1.0.0"}')
    with pytest.raises(ValueError, match="refusing to reseal"):
        publish(media, "run-1", presentation_version=page.PAGE_VERSION + 5)
    assert read_bytes(frozen) == b"damaged"
    assert read_bytes(media / "fuse/runs/run-1/manifest.json")
    assert read_bytes(frozen) != before


def test_a_damaged_manifest_is_skipped_instead_of_breaking_the_library(tmp_path: Path) -> None:
    from id_detector.io import atomic_write_json

    media = seed(tmp_path)
    good = publish(media, "run-1")
    later = publish(media, "run-2", metadata={"started_at": "2026-09-13T00:00:00Z"})
    manifest = json.loads(read_text(later / "manifest.json"))
    for damage in ("run_id", "presentation_version", "fuse_run", "status"):
        broken = {key: value for key, value in manifest.items() if key != damage}
        atomic_write_json(later / "manifest.json", broken)
        assert bundles.read_bundle_manifest(later) is None
        assert bundles.result_dir(media) == good  # no crash, and the intact bundle still wins
    # A manifest whose identity does not match its own directory is damaged too.
    atomic_write_json(later / "manifest.json", {**manifest, "presentation_version": 999})
    assert bundles.read_bundle_manifest(later) is None
    assert bundles.result_dir(media) == good
    assert bundles.run_metadata(media)["run_id"] == "run-1"


def test_a_reused_bundle_is_not_pointed_at_when_its_frozen_run_is_damaged(tmp_path: Path) -> None:
    media = seed(tmp_path)
    directory = publish(media)
    (media / "present/current").unlink()
    (media / "fuse/runs/run-1/episodes.json").write_bytes(b"damaged")
    with pytest.raises(ValueError, match="damaged frozen run"):
        publish(media)
    assert not (media / "present/current").exists()
    assert bundles.read_bundle_manifest(directory)  # the bundle itself is untouched


def test_idea_acquire_enriches_and_renders_the_same_run(tmp_path: Path, monkeypatch) -> None:
    """The whole `idea acquire` path, with the provider call replaced by a recorder."""

    import asyncio

    from id_detector import cli
    from id_detector.contracts import AcquireFile
    from id_detector.enrich.run import AcquireResult
    from id_detector.io import atomic_write_json

    media = seed(tmp_path)
    publish(media, "run-c", metadata={"started_at": "2026-09-11T00:00:00Z"})
    partial_rows = _other_episodes()
    atomic_write_json(media / "fuse/episodes.json", partial_rows)
    bundles.publish_result(
        media_dir=media,
        source=_source("soundcloud"),
        episodes=partial_rows,
        identities=_identities(),
        duration_ms=3_600_000,
        metadata={"run_id": "run-p", "started_at": "2026-09-12T00:00:00Z", "status": "partial"},
        config=AppConfig(),
    )
    acquire = AcquireFile.model_validate_json(
        read_text(Path(__file__).parent / "golden/acquire.json")
    )
    seen: dict[str, object] = {}

    async def recorded(**kwargs: object) -> AcquireResult:
        seen.update(kwargs)
        path = media / "enrich/acquire.json"
        atomic_write_json(path, acquire)
        return AcquireResult(
            record=acquire,
            path=path,
            counts=dict.fromkeys(
                (
                    "episodes",
                    "direct_links_total",
                    "direct_links_by_source",
                    "free_download_flags",
                    "gate_links",
                    "buy_links",
                    "search_only_rows",
                ),
                0,
            ),
        )

    monkeypatch.setattr(cli, "enrich_media_dir", recorded)
    code = asyncio.run(
        cli._acquire(
            _source("soundcloud").input_url,
            work_root=tmp_path,
            refresh=False,
            enable_soundcloud=False,
        )
    )
    assert code == 0
    # Enrichment saw the selected run's rows, not the newer partial run's mutable ones.
    assert seen["episodes"] == _episodes_file()
    assert seen["identities"] == _identities()
    selected = bundles.result_dir(media)
    manifest = bundles.read_bundle_manifest(selected)
    assert manifest["run_id"] == "run-c" and manifest["status"] == "complete"
    assert "acquire.json" in manifest["files"]
    assert len(json.loads(read_text(media / "fuse/runs/run-c/episodes.json"))["episodes"]) == len(
        _episodes_file().episodes
    )
    assert read_text(media / "present/current") == selected.name

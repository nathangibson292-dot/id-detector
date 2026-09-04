"""Stage 8 — candidate reference-pool discovery (network-free parsing + manifest).

Live discovery drives yt-dlp; these default tests only parse a recorded flat-playlist listing and
exercise the manifest/formatting helpers.  The "never auto-rips" contract is enforced at the CLI
(links printed unless --index) and by the deletion guarantee tested in ``test_stage8_panako``.
"""

from __future__ import annotations

import json
from pathlib import Path

from id_detector.candidates import (
    Candidate,
    IndexedResource,
    artists_from_hints,
    build_manifest,
    candidate_from_entry,
    deduplicate_candidates,
    derive_index_id,
    derive_index_version,
    format_candidate_list,
    parse_flat_playlist,
    uploader_uploads_url,
)
from id_detector.providers.panako import PANAKO_JAR_SHA256

FIXTURES = Path(__file__).parent / "fixtures" / "candidates"


def test_parse_uploader_uploads_listing() -> None:
    text = (FIXTURES / "uploader-uploads.json").read_text(encoding="utf-8")
    candidates = parse_flat_playlist(text, source="uploader_uploads")
    assert [c.title for c in candidates] == [
        "Untitled Edit 01",
        "Warehouse Dub",
        "ID - Forthcoming",
    ]
    assert all(c.source == "uploader_uploads" for c in candidates)
    assert candidates[0].url == "example-uploader/untitled-edit-01"
    assert candidates[0].uploader == "Example Uploader"


def test_parse_artist_search_listing() -> None:
    text = (FIXTURES / "artist-search.json").read_text(encoding="utf-8")
    candidates = parse_flat_playlist(text, source="artist_search:Example Artist")
    assert len(candidates) == 2
    assert candidates[1].url == "example-artist/rework"


def test_candidate_from_entry_skips_entries_without_a_url() -> None:
    assert candidate_from_entry({"title": "no url"}, source="x") is None
    got = candidate_from_entry({"webpage_url": "a/b", "title": "T"}, source="x")
    assert got is not None and got.url == "a/b"


def test_deduplicate_preserves_first_seen_order() -> None:
    candidates = [
        Candidate(url="a", title="A", uploader=None, source="uploader_uploads"),
        Candidate(url="b", title="B", uploader=None, source="artist_search:x"),
        Candidate(url="a", title="A dup", uploader=None, source="explicit_url"),
    ]
    deduped = deduplicate_candidates(candidates)
    assert [c.url for c in deduped] == ["a", "b"]
    assert deduped[0].source == "uploader_uploads"


def test_uploader_uploads_url_derives_tracks_page() -> None:
    listing = json.dumps({"uploader_url": "https://example.test/artist", "entries": []})
    assert uploader_uploads_url(listing) == "https://example.test/artist/tracks"
    assert uploader_uploads_url(json.dumps({"entries": []})) is None


def test_artists_from_hints_collects_distinct_names(tmp_path: Path) -> None:
    hints = tmp_path / "hints.jsonl"
    hints.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {"artist": "Example Artist", "title": "One"},
                {"artist": "Example Artist", "title": "Two"},  # duplicate collapses
                {"artist": None, "title": "unknown"},
                {"artist": "Another Act", "title": "Three"},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    assert artists_from_hints(hints) == ["Example Artist", "Another Act"]
    assert artists_from_hints(tmp_path / "missing.jsonl") == []


def test_manifest_records_index_identity_for_the_cache_key() -> None:
    resources = [
        IndexedResource("700123", 2920, "Untitled Edit 01", "Example Uploader", "u/a", "uploads"),
        IndexedResource("700124", 2333, "Warehouse Dub", "Example Uploader", "u/b", "uploads"),
    ]
    manifest = build_manifest(index_label="set-xyz", resources=resources)
    assert manifest["index_id"] == derive_index_id("set-xyz")
    assert manifest["index_version"] == derive_index_version(resources)
    assert manifest["panako_jar_sha256"] == PANAKO_JAR_SHA256
    assert len(manifest["resources"]) == 2
    # index_version is content-addressed: it changes when the pool changes.
    assert derive_index_version(resources) != derive_index_version(resources[:1])


def test_format_candidate_list_is_links_only_and_advises_confirmation() -> None:
    candidates = [Candidate(url="u/a", title="A", uploader="U", source="uploader_uploads")]
    text = format_candidate_list(candidates)
    assert "no audio downloaded" in text
    assert "--index" in text
    assert format_candidate_list([]) == "no candidates discovered"

"""Stage 6 — enrichment ("where to get it").

Recorded catalogue responses are embedded as Python constants (not committed JSON fixtures) so the
fixture audit's URL check — which does *not* allow-list arbitrary lookup fixtures — never sees them,
and so the default test run performs no network at all (every request is served by an httpx mock
transport and a no-op sleeper).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from id_detector.contracts import (
    AcquireFile,
    EpisodesFile,
    IdentitiesRecord,
    SourceRecord,
    derive_source_key,
)
from id_detector.enrich import benchmark as link_bench
from id_detector.enrich.feedback import augmented_version_support, enriched_version_status
from id_detector.enrich.http import EnrichHttp, EnrichHttpError
from id_detector.enrich.links import classify_soundcloud, is_gate_host, search_links
from id_detector.enrich.lookups import (
    fetch_deezer,
    fetch_discogs,
    parse_deezer,
    parse_discogs,
    parse_itunes,
    parse_musicbrainz,
)
from id_detector.enrich.match import (
    Candidate,
    match_confidence_e4,
    parse_title,
    strong_agreement,
)
from id_detector.enrich.run import build_acquire, candidate_artist_title
from id_detector.enrich.soundcloud import (
    best_soundcloud_flags,
    build_token_context,
    fetch_soundcloud_flags,
    flags_from_track,
    parse_search_tracks,
)
from id_detector.present import export_tracklist

MEDIA_KEY = "a" * 64

# --- recorded catalogue responses (documented API shapes) --------------------------------------

DEEZER = {
    "data": [
        {
            "id": 3135556,
            "title": "Signal Path",
            "duration": 214,
            "link": "https://www.deezer.com/track/3135556",
            "preview": "https://cdns-preview.dzcdn.net/x.mp3",
            "artist": {"name": "Example Artist"},
            "album": {"title": "Signal Path EP"},
            "type": "track",
        }
    ]
}
ITUNES = {
    "resultCount": 1,
    "results": [
        {
            "trackId": 1440905255,
            "trackName": "Signal Path",
            "artistName": "Example Artist",
            "collectionName": "Signal Path EP",
            "trackViewUrl": "https://music.apple.com/us/album/signal-path/1/1440905255",
            "previewUrl": "https://audio-preview.itunes.apple.com/x.m4a",
            "trackTimeMillis": 214000,
        }
    ],
}
MUSICBRAINZ = {
    "recordings": [
        {
            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "title": "Signal Path",
            "length": 214000,
            "artist-credit": [{"name": "Example Artist"}],
            "isrcs": ["GBXYZ2400001"],
        }
    ]
}
DISCOGS = {
    "results": [
        {
            "title": "Example Artist - Signal Path",
            "uri": "/release/12345-Example-Artist-Signal-Path",
            "id": 12345,
        }
    ]
}
# A remix result for a query whose episode title has no qualifier: must NOT become a direct link.
DEEZER_REMIX = {
    "data": [
        {
            "id": 999,
            "title": "Signal Path (Nightowl Remix)",
            "duration": 300,
            "link": "https://www.deezer.com/track/999",
            "artist": {"name": "Example Artist"},
            "album": {"title": "Remixes"},
            "type": "track",
        }
    ]
}
SC_HOMEPAGE = (
    '<html><script crossorigin src="https://a-v2.sndcdn.com/assets/50-abc.js"></script></html>'
)
SC_ASSET = 'window.__sc_hydration=[];var o={client_id:"abcdef0123456789ABCDEF0123456789"};'
SC_SEARCH_FREE = {
    "collection": [
        {
            "id": 5001,
            "title": "Signal Path",
            "duration": 214000,
            "downloadable": True,
            "has_downloads_left": True,
            "purchase_url": None,
            "purchase_title": None,
            "license": "cc-by",
            "permalink_url": "https://soundcloud.com/example-artist/signal-path",
            "user": {"username": "Example Artist"},
        }
    ]
}
SC_SEARCH_GATE = {
    "collection": [
        {
            "id": 5002,
            "title": "Signal Path",
            "duration": 214000,
            "downloadable": False,
            "has_downloads_left": False,
            "purchase_url": "https://hypeddit.com/example/signalpath",
            "purchase_title": "Free Download",
            "license": "all-rights-reserved",
            "permalink_url": "https://soundcloud.com/example-artist/signal-path",
            "user": {"username": "Example Artist"},
        }
    ]
}


def _handler(routes: dict[str, object], *, fail: str | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        key = f"{request.url.host}{request.url.path}"
        if fail == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if fail == "status":
            return httpx.Response(500, text="boom")
        for route, payload in routes.items():
            if key.endswith(route):
                if isinstance(payload, str):
                    return httpx.Response(200, text=payload)
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handle)


def _client(routes, *, fail=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_handler(routes, fail=fail))


def _http(routes, tmp_path: Path, *, fail=None, clock=None) -> EnrichHttp:
    async def _sleep(_seconds: float) -> None:
        return None

    return EnrichHttp(
        client=_client(routes, fail=fail),
        cache_root=tmp_path / "enrich",
        sleeper=_sleep,
        clock=clock or (lambda: 0.0),
    )


# --- lookups: parsers + fetchers + failure paths -----------------------------------------------


def test_lookup_parsers_normalise_recording_ids() -> None:
    deezer = parse_deezer(DEEZER)
    assert deezer[0].recording_ids == {"deezer": "3135556"}
    assert deezer[0].duration_ms == 214_000
    apple = parse_itunes(ITUNES)
    assert apple[0].recording_ids == {"apple": "1440905255"}
    assert apple[0].url.startswith("https://music.apple.com/")
    mb = parse_musicbrainz(MUSICBRAINZ)
    assert mb[0].recording_ids == {
        "mb_recording": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "isrc": "GBXYZ2400001",
    }
    assert mb[0].isrcs == ("GBXYZ2400001",)
    discogs = parse_discogs(DISCOGS)
    assert discogs[0].url == "https://www.discogs.com/release/12345-Example-Artist-Signal-Path"
    assert discogs[0].recording_ids == {}
    assert parse_deezer({"error": {}}) == [] and parse_itunes([]) == []


def test_fetch_uses_cache_and_respects_rate_limit(tmp_path: Path) -> None:
    slept: list[float] = []
    ticks = iter([0.0, 0.0, 0.0, 0.01])  # two same-source requests inside the min interval

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    http = EnrichHttp(
        client=_client({"api.deezer.com/search": DEEZER}),
        cache_root=tmp_path / "enrich",
        sleeper=_sleep,
        clock=lambda: next(ticks),
    )

    async def scenario() -> tuple[int, int, int]:
        first = await fetch_deezer(http, "Example Artist", "Signal Path")
        second = await fetch_deezer(http, "Example Artist", "Signal Path")  # cache hit
        third = await fetch_deezer(http, "Example Artist", "Other Track")  # new key -> rate wait
        await http.client.aclose()
        return len(first), len(second), len(third)

    assert asyncio.run(scenario()) == (1, 1, 1)
    assert http.cache_hits == 1 and http.request_count == 2
    assert slept and slept[0] > 0  # the second live request waited for the deezer interval


def test_http_error_and_timeout_are_bounded(tmp_path: Path) -> None:
    async def scenario(fail: str) -> tuple[bool, list[Candidate]]:
        http = _http({"api.deezer.com/search": DEEZER}, tmp_path / fail, fail=fail)
        raised = False
        try:
            await http.get_json("deezer", "https://api.deezer.com/search", params={"q": "x"})
        except EnrichHttpError:
            raised = True
        found = await fetch_deezer(http, "A", "B")  # never raises
        await http.client.aclose()
        return raised, found

    for mode in ("status", "timeout"):
        raised, found = asyncio.run(scenario(mode))
        assert raised is True and found == []


def test_discogs_requires_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCOGS_TOKEN", raising=False)
    http = _http({"api.discogs.com/database/search": DISCOGS}, tmp_path)

    async def scenario() -> list[Candidate]:
        result = await fetch_discogs(http, "Example Artist", "Signal Path")
        await http.client.aclose()
        return result

    assert asyncio.run(scenario()) == []  # no token -> no network, no candidates


# --- scoring + direct-link policy vectors ------------------------------------------------------


def test_parse_title_separates_version_qualifier() -> None:
    assert parse_title("Signal Path") == (("signal", "path"), frozenset())
    base, version = parse_title("Signal Path (Nightowl Remix)")
    assert base == ("signal", "path") and "remix" in version
    base, version = parse_title("Track - Radio Edit")
    assert base == ("track",) and {"radio", "edit"} <= set(version)


def test_remix_vs_original_is_not_a_direct_link_on_title_only_agreement() -> None:
    # Episode title has no qualifier; catalogue returns a remix -> strong agreement must be False.
    original = Candidate("deezer", "u", "Example Artist", "Signal Path")
    remix = Candidate("deezer", "u", "Example Artist", "Signal Path (Nightowl Remix)")
    assert strong_agreement("Example Artist", "Signal Path", *(original.artist, original.title))
    assert not strong_agreement("Example Artist", "Signal Path", remix.artist, remix.title)
    # And the remix still scores below the original.
    assert match_confidence_e4("Example Artist", "Signal Path", remix) < match_confidence_e4(
        "Example Artist", "Signal Path", original
    )


def test_duration_agreement_uses_three_second_window() -> None:
    close = Candidate("deezer", "u", "A", "T", duration_ms=214_000)
    far = Candidate("deezer", "u", "A", "T", duration_ms=260_000)
    high = match_confidence_e4("A", "T", close, reference_duration_ms=216_000)
    low = match_confidence_e4("A", "T", far, reference_duration_ms=216_000)
    assert high > low


# --- SoundCloud classification + flags ---------------------------------------------------------


def test_soundcloud_classification_and_gate_hosts() -> None:
    assert classify_soundcloud(downloadable=True, has_downloads_left=True, purchase_url=None) == (
        "free_download_native"
    )
    assert (
        classify_soundcloud(
            downloadable=False, has_downloads_left=False, purchase_url="https://hypeddit.com/x"
        )
        == "gate_link"
    )
    assert (
        classify_soundcloud(
            downloadable=False, has_downloads_left=None, purchase_url="https://www.beatport.com/x"
        )
        == "buy_link"
    )
    assert classify_soundcloud(downloadable=False, has_downloads_left=None, purchase_url=None) == (
        "none"
    )
    assert is_gate_host("https://go.hypeddit.com/abc") and not is_gate_host(
        "https://www.beatport.com/x"
    )


def test_best_soundcloud_flags_picks_the_agreeing_track() -> None:
    tracks = parse_search_tracks(SC_SEARCH_GATE)
    flags = best_soundcloud_flags(tracks, artist="Example Artist", title="Signal Path")
    assert flags is not None and flags.classification == "gate_link"
    assert flags.purchase_url == "https://hypeddit.com/example/signalpath"
    free = flags_from_track(SC_SEARCH_FREE["collection"][0], 9100)
    assert free.classification == "free_download_native" and free.match_confidence_e4 == 9100


def test_soundcloud_lookup_discovers_client_id_and_reads_flags(tmp_path: Path) -> None:
    routes = {
        "soundcloud.com/": SC_HOMEPAGE,
        "a-v2.sndcdn.com/assets/50-abc.js": SC_ASSET,
        "api-v2.soundcloud.com/search/tracks": SC_SEARCH_FREE,
    }
    sc_client = _client(routes)
    http = _http(routes, tmp_path)

    async def scenario():
        context = build_token_context(_source("soundcloud"), tmp_path / "enrich", sc_client)
        flags = await fetch_soundcloud_flags(
            http, token_context=context, artist="Example Artist", title="Signal Path"
        )
        await sc_client.aclose()
        await http.client.aclose()
        return flags

    flags = asyncio.run(scenario())
    assert flags is not None and flags.classification == "free_download_native"
    # The public client token is cached locally, never in the returned artefact.
    assert (tmp_path / "enrich" / "sc_comments" / "client-token.json").is_file()


# --- search links ------------------------------------------------------------------------------


def test_search_links_are_deterministic_and_no_network() -> None:
    links = search_links("Example Artist", "Signal Path (Club Mix)")
    sources = [link["source"] for link in links]
    assert sources == ["bandcamp", "beatport", "traxsource"]
    assert links[0]["url"].startswith("https://bandcamp.com/search?q=")
    assert "item_type=t" in links[0]["url"]
    assert search_links("A", "B") == search_links("A", "B")


# --- identity feedback (non-authoritative version corroboration) --------------------------------


def _identities(member_nodes: list[str], *, assertions=None) -> IdentitiesRecord:
    payload = json.loads(Path("tests/golden/identities.json").read_text(encoding="utf-8"))
    candidate = payload["candidates"][0]
    candidate["member_nodes"] = member_nodes
    payload["nodes"] = [
        {
            "schema_version": "1.0.0",
            "generated_by": "id-detector/0.1.0",
            "id": node,
            "ns": node.split(":", 1)[0],
            "label": "Example Artist - Signal Path",
        }
        for node in member_nodes
    ]
    payload["assertions"] = assertions or []
    return IdentitiesRecord.model_validate(payload)


def test_single_enrich_source_does_not_corroborate_version() -> None:
    identities = _identities(["shazam:track-local-a"])  # one recording node, no base assertions
    candidate_id = identities.candidates[0].canonical_id
    support = augmented_version_support(
        identities, {candidate_id: [("deezer", "deezer", "3135556")]}
    )
    assert support[candidate_id] is False
    assert enriched_version_status("unverified", contested=False, supported=False) == "unverified"


def test_two_independent_sources_corroborate_version() -> None:
    # Base graph already asserts (shazam, deezer) once; enrichment supplies the 2nd independent
    # source for the same pair, so the corroboration rule unions them into a 2-recording candidate.
    base_assertion = {
        "schema_version": "1.0.0",
        "generated_by": "id-detector/0.1.0",
        "id": "c" * 40,
        "a": "deezer:3135556",
        "b": "shazam:track-local-a",
        "relation": "same_recording",
        "source": {"kind": "provider_observation", "record_id": "obs-1"},
        "independent_of": "provider:shazam",
        "confidence": 9000,
    }
    identities = _identities(["shazam:track-local-a"], assertions=[base_assertion])
    candidate_id = identities.candidates[0].canonical_id
    support = augmented_version_support(
        identities, {candidate_id: [("deezer", "deezer", "3135556")]}
    )
    assert support[candidate_id] is True
    assert enriched_version_status("unverified", contested=False, supported=True) == "verified"


def test_contested_never_becomes_verified() -> None:
    assert enriched_version_status("unverified", contested=True, supported=True) == "contested"


# --- build_acquire end to end ------------------------------------------------------------------


def _source(platform: str = "soundcloud") -> SourceRecord:
    payload = json.loads(Path("tests/golden/source.json").read_text(encoding="utf-8"))
    payload.update(
        {
            "media_key": MEDIA_KEY,
            "platform": platform,
            "canonical_url": "https://soundcloud.com/example/mix",
            "input_url": "https://soundcloud.com/example/mix",
            "source_key": derive_source_key("https://soundcloud.com/example/mix"),
        }
    )
    return SourceRecord.model_validate(payload)


def _episodes(candidate_id: str, *, badge: str = "possible") -> EpisodesFile:
    payload = json.loads(Path("tests/golden/episodes.json").read_text(encoding="utf-8"))
    episode = payload["episodes"][0]
    episode["candidate_id"] = candidate_id
    episode["badge"] = badge
    episode["version_status"] = "unverified"
    payload["episodes"] = [episode]
    payload["gaps"] = []
    return EpisodesFile.model_validate(payload)


def _seed_fuse_artefacts(
    media_dir: Path, episodes: EpisodesFile, identities: IdentitiesRecord
) -> None:
    """Write the fuse artefacts build_acquire records as provenance upstreams.

    In the real pipeline ``fuse`` writes these before enrich runs; the in-memory tests
    must materialise them so the acquire completion sidecar can hash its upstream.
    """

    fuse_dir = media_dir / "fuse"
    fuse_dir.mkdir(parents=True, exist_ok=True)
    (fuse_dir / "episodes.json").write_bytes(json.dumps(episodes.model_dump(mode="json")).encode())
    (fuse_dir / "identities.gen0.json").write_bytes(
        json.dumps(identities.model_dump(mode="json")).encode()
    )


def test_build_acquire_direct_links_search_and_determinism(tmp_path: Path) -> None:
    identities = _identities(["shazam:track-local-a"])
    candidate_id = identities.candidates[0].canonical_id
    episodes = _episodes(candidate_id)
    _seed_fuse_artefacts(tmp_path / "media", episodes, identities)
    routes = {
        "api.deezer.com/search": DEEZER,
        "itunes.apple.com/search": ITUNES,
        "musicbrainz.org/ws/2/recording": MUSICBRAINZ,
    }

    async def run_once() -> AcquireFile:
        http = _http(routes, tmp_path)
        result = await build_acquire(
            source=_source(),
            media_dir=tmp_path / "media",
            episodes=episodes,
            identities=identities,
            http=http,
            sc_client=None,
            enable_soundcloud=False,
        )
        await http.client.aclose()
        return result.record

    record = asyncio.run(run_once())
    assert len(record.episodes) == 1
    episode = record.episodes[0]
    direct_sources = {link.source for link in episode.direct}
    assert {"deezer", "apple", "musicbrainz"} <= direct_sources  # all strongly agree
    assert all(link.corroborates_version is False for link in episode.direct)  # single-engine
    assert episode.version_status == "unverified"
    assert [link.source for link in episode.search] == ["bandcamp", "beatport", "traxsource"]

    first = (tmp_path / "media" / "enrich" / "acquire.json").read_bytes()
    second_record = asyncio.run(run_once())
    assert second_record.model_dump(mode="json") == record.model_dump(mode="json")
    assert (tmp_path / "media" / "enrich" / "acquire.json").read_bytes() == first  # deterministic


def test_build_acquire_search_only_when_no_strong_match(tmp_path: Path) -> None:
    identities = _identities(["shazam:track-local-a"])
    candidate_id = identities.candidates[0].canonical_id
    episodes = _episodes(candidate_id)
    _seed_fuse_artefacts(tmp_path / "media", episodes, identities)

    async def scenario() -> AcquireFile:
        http = _http({"api.deezer.com/search": DEEZER_REMIX}, tmp_path)
        result = await build_acquire(
            source=_source(),
            media_dir=tmp_path / "media",
            episodes=episodes,
            identities=identities,
            http=http,
            sc_client=None,
            enable_soundcloud=False,
        )
        await http.client.aclose()
        return result.record

    record = asyncio.run(scenario())
    episode = record.episodes[0]
    assert episode.direct == []  # remix result is not a direct link on a no-qualifier episode
    # the remix result surfaces as a labelled deezer search link instead
    assert any(link.source == "deezer" for link in episode.search)


def test_candidate_label_prefers_provider_nodes() -> None:
    identities = _identities(["shazam:track-local-a"])
    artist, title = candidate_artist_title(identities, identities.candidates[0].canonical_id)
    assert (artist, title) == ("Example Artist", "Signal Path")


# --- exports with acquire columns --------------------------------------------------------------


def test_export_tracklist_gains_acquire_columns(tmp_path: Path) -> None:
    identities = _identities(["shazam:track-local-a"])
    candidate_id = identities.candidates[0].canonical_id
    episodes = _episodes(candidate_id)
    media_dir = tmp_path / "media"
    (media_dir / "fuse").mkdir(parents=True)
    episodes_path = media_dir / "fuse" / "episodes.json"
    identities_path = media_dir / "fuse" / "identities.gen0.json"
    episodes_path.write_bytes(json.dumps(episodes.model_dump(mode="json")).encode())
    identities_path.write_bytes(json.dumps(identities.model_dump(mode="json")).encode())

    async def scenario():
        http = _http({"api.deezer.com/search": DEEZER, "itunes.apple.com/search": ITUNES}, tmp_path)
        result = await build_acquire(
            source=_source(),
            media_dir=media_dir,
            episodes=episodes,
            identities=identities,
            http=http,
            sc_client=None,
            enable_soundcloud=False,
        )
        await http.client.aclose()
        return result

    result = asyncio.run(scenario())
    export = export_tracklist(
        media_dir=media_dir,
        media_key=MEDIA_KEY,
        duration_ms=600_000,
        episodes=episodes,
        identities=identities,
        episodes_path=episodes_path,
        identities_path=identities_path,
        acquire=result.record,
        acquire_path=result.path,
    )
    markdown = export.markdown_path.read_text(encoding="utf-8")
    assert "Free DL | Gate | Buy | Search" in markdown
    track_entries = [entry for entry in export.entries if entry["kind"] == "track"]
    assert track_entries[0]["acquire"] is not None
    assert track_entries[0]["acquire"]["buy"] is True  # apple purchase link present
    assert track_entries[0]["acquire"]["search"] is True


# --- link-correctness benchmark ----------------------------------------------------------------


def test_link_benchmark_sample_is_stratified_and_scored() -> None:
    acquire = AcquireFile.model_validate_json(
        Path("tests/golden/acquire.json").read_text(encoding="utf-8")
    )
    sheet = link_bench.build_link_sample([acquire], sample_size=60)
    assert sheet["total_direct_links"] == 2  # golden has two direct links (episode 1)
    assert set(sheet["strata_sampled"]) == set(link_bench.STRATA)
    assert all(row["mark"] is None for row in sheet["links"])

    # Mark them and score: 2/2 correct is still short of the >=60-link gate.
    for row in sheet["links"]:
        row["mark"] = "correct"
    score = link_bench.score_link_sample(sheet)
    assert score["correct"] == 2 and score["precision_e4"] == 10_000
    assert score["gate"]["pass"] is False  # fewer than 60 marked links

    # One wrong mark lowers precision and the one-sided lower bound.
    sheet["links"][0]["mark"] = "incorrect"
    lowered = link_bench.score_link_sample(sheet)
    assert lowered["precision_e4"] == 5_000
    assert lowered["one_sided_95_lower_e4"] < score["one_sided_95_lower_e4"]


# --- audit allow-rule scoping ------------------------------------------------------------------


def _audit_module():
    spec = importlib.util.spec_from_file_location(
        "audit_fixtures", Path("scripts/audit_fixtures.py")
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_audit_allow_rule_is_scoped_to_acquisition_artifacts() -> None:
    audit = _audit_module()
    assert audit._acquisition_artifact(Path("work/x/y/enrich/acquire.json"))
    assert audit._acquisition_artifact(Path("work/x/y/present/tracklist.json"))
    assert audit._acquisition_artifact(Path("work/x/y/present/tracklist.md"))
    assert audit._acquisition_artifact(Path("tests/golden/acquire.json"))
    # Not scoped: a lookup fixture or any other JSON keeps the full URL check.
    assert not audit._acquisition_artifact(Path("tests/fixtures/enrich/deezer.json"))
    assert not audit._acquisition_artifact(Path("data/corpus/dev-1/x/ground_truth.json"))


def test_audit_catalogue_hosts_and_handles() -> None:
    audit = _audit_module()
    catalogue = (
        "https://www.deezer.com/track/1 https://music.apple.com/x "
        "https://hypeddit.com/y https://soundcloud.com/a/b"
    )
    assert audit._non_catalogue_urls(catalogue) == []
    assert audit._non_catalogue_urls("see https://evil.example.com/x") == [
        "https://evil.example.com/x"
    ]
    # The handle/username check is never relaxed, on acquisition paths or anywhere.
    assert audit._HANDLE.search("credit @some_dj here") is not None

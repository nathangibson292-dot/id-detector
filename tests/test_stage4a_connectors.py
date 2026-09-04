from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from id_detector.contracts import SourceRecord, derive_source_key
from id_detector.hints.connectors import pointer as pointer_connector
from id_detector.hints.connectors import soundcloud as soundcloud_connector
from id_detector.hints.connectors import youtube as youtube_connector
from id_detector.hints.connectors.base import (
    CircuitBreaker,
    ConnectorContext,
    ConnectorError,
    RetryableConnectorError,
    write_raw_json,
)
from id_detector.hints.connectors.mixcloud import parse_sections
from id_detector.hints.connectors.mixesdb import parse_wikitext, revision_content
from id_detector.hints.connectors.pointer import (
    MAX_BYTES,
    fetch_limited,
    parse_pointer_html,
    validate_pointer_url,
)
from id_detector.hints.connectors.soundcloud import parse_comments_page
from id_detector.hints.connectors.tl1001 import parse_search
from id_detector.hints.connectors.youtube import (
    COMMENT_ARGS,
    MAX_COMMENTS,
    MAX_PARENT_THREADS,
    MAX_REPLIES,
    MAX_REPLIES_PER_THREAD,
    chapters,
    parse_comments,
)
from id_detector.hints.parse import HintInput, parse_hint_inputs
from id_detector.jobs import AsyncJobStore, ConnectorJob
from id_detector.process import ProcessResult

FIXTURES = Path("tests/fixtures/hints")
MEDIA_KEY = "a" * 64


def _source(platform: str = "youtube") -> SourceRecord:
    payload = json.loads(Path("tests/golden/source.json").read_text(encoding="utf-8"))
    payload.update(
        {
            "media_key": MEDIA_KEY,
            "platform": platform,
            "canonical_url": "source-ref:fixture",
            "input_url": "source-ref:fixture",
            "source_key": derive_source_key("source-ref:fixture"),
            "metadata": {
                "description": "00:00 Artist One - Title One\n05:00 Artist Two - Title Two",
                "chapters": [
                    {"title": "Artist One - Title One", "start_time_ms": 0, "end_time_ms": 300_000},
                    {
                        "title": "Artist Two - Title Two",
                        "start_time_ms": 300_000,
                        "end_time_ms": 600_000,
                    },
                ],
                "comment_count": 2,
            },
        }
    )
    return SourceRecord.model_validate(payload)


def _job(connector: str = "pointer_import") -> ConnectorJob:
    return ConnectorJob(
        id="b" * 40,
        media_key=MEDIA_KEY,
        connector=connector,
        target_url="target-ref",
        cursor=None,
        page=0,
        page_cap=1,
        item_cap=5_000,
        items_fetched=0,
        state="leased",
        lease_owner="owner",
        lease_expires_at=None,
        heartbeat_at=None,
        attempts=1,
        next_retry_at=None,
        result_path=None,
        truncated=0,
        error=None,
        created_at="2026-09-04T00:00:00Z",
        updated_at="2026-09-04T00:00:00Z",
    )


def test_authored_connector_response_parsers(tmp_path: Path) -> None:
    sc = json.loads((FIXTURES / "soundcloud-comments-authored.json").read_text(encoding="utf-8"))
    sc["collection"][0]["user_id"] = 11
    sc["collection"][1]["user_id"] = 22
    sc_inputs = parse_comments_page(sc, media_key=MEDIA_KEY, uploader_user_id="22")
    assert len(sc_inputs) == 2
    assert sc_inputs[0].position_ms == 120_000
    assert sc_inputs[1].is_uploader
    assert sc_inputs[1].is_verified and sc_inputs[1].follower_count == 900

    yt = json.loads((FIXTURES / "youtube-comments-authored.json").read_text(encoding="utf-8"))
    yt_inputs = parse_comments(yt, media_key=MEDIA_KEY)
    assert len(yt_inputs) == 2
    assert yt_inputs[0].is_pinned and yt_inputs[0].is_uploader
    assert yt_inputs[1].parent_source_id == "root-a"
    assert COMMENT_ARGS == "youtube:comment_sort=top;max_comments=4200,200,4000,20,1"

    mix_payload = json.loads((FIXTURES / "mixesdb-authored.json").read_text(encoding="utf-8"))
    content = revision_content(mix_payload, "17")
    assert content is not None
    list_payload = json.loads(json.dumps(mix_payload))
    listed_page = next(iter(list_payload["query"]["pages"].values()))
    listed_page["pageid"] = 17
    list_payload["query"]["pages"] = [listed_page]
    assert revision_content(list_payload, "17") == content
    mix = parse_wikitext(content, page_id="17")
    assert mix.items_fetched == 3 and mix.tracklist_blocks == 1

    cloud = json.loads((FIXTURES / "mixcloud-graphql-authored.json").read_text(encoding="utf-8"))
    sections = parse_sections(cloud, duration_ms=600_000)
    assert [item.position_ms for item in sections.inputs] == [0, 300_000]
    assert all(item.is_uploader for item in sections.inputs)

    search_payload = json.loads(
        (FIXTURES / "tl1001-search-authored.json").read_text(encoding="utf-8")
    )
    search = parse_search(search_payload)
    assert search.items_fetched == 1 and search.inputs[0].mirror_status == "quarantined"

    pointer_html = (FIXTURES / "pointer-1001-authored.html").read_text(encoding="utf-8")
    imported = parse_pointer_html(
        pointer_html,
        final_url="https://1001.tl/fixture",
        mirror_of="source-ref:fixture",
        truncated=False,
    )
    assert imported.inputs[0].connector == "1001tl"
    assert imported.inputs[0].text.splitlines() == [
        "00:00 Artist One - Title One",
        "05:00 Artist Two - Title Two",
    ]
    assert imported.inputs[0].source_record_id.startswith("imported-tracklist-")

    source = _source()
    context = ConnectorContext(
        source=source,
        duration_ms=600_000,
        media_dir=tmp_path,
        cache_root=tmp_path,
        store=AsyncJobStore(tmp_path / "unused.sqlite"),
        job=_job("yt_chapters"),
        owner="owner",
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
        breaker=CircuitBreaker(),
    )
    try:
        chapter_output = chapters(context)
    finally:
        asyncio.run(context.http.aclose())
    assert len(chapter_output.inputs) == 2
    assert chapter_output.inputs[0].position_end_ms == 300_000


@pytest.mark.parametrize(
    "url",
    [
        "http://1001.tl/fixture",
        "https://example.invalid/fixture",
        "https://user@1001.tl/fixture",
        "https://1001.tl:444/fixture",
        "https://1001.tl/fixture?token=secret",
        "https://1001.tl/fixture?signature=secret",
        "https://1001.tl/fixture?credential=secret",
        "https://1001.tl/fixture?api-key=secret",
    ],
)
def test_pointer_allowlist_rejects_every_out_of_policy_shape(url: str) -> None:
    with pytest.raises(ConnectorError):
        validate_pointer_url(url)


def test_pointer_revalidates_redirects_and_records_size_truncation(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if len(calls) == 1:
                return httpx.Response(302, headers={"location": "https://www.mixesdb.com/page"})
            return httpx.Response(200, content=b"x" * (MAX_BYTES + 17))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            context = ConnectorContext(
                source=_source(),
                duration_ms=600_000,
                media_dir=tmp_path,
                cache_root=tmp_path,
                store=AsyncJobStore(tmp_path / "unused.sqlite"),
                job=_job(),
                owner="owner",
                http=client,
                breaker=CircuitBreaker(),
            )
            final, body, truncated, redirects = await fetch_limited(
                context, "https://1001.tl/fixture"
            )
            assert final == "https://www.mixesdb.com/page"
            assert len(body.encode()) == MAX_BYTES
            assert truncated and redirects == 1

        def disallowed(_: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://example.invalid/page"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(disallowed)) as client:
            context = ConnectorContext(
                source=_source(),
                duration_ms=600_000,
                media_dir=tmp_path,
                cache_root=tmp_path,
                store=AsyncJobStore(tmp_path / "unused-2.sqlite"),
                job=_job(),
                owner="owner",
                http=client,
                breaker=CircuitBreaker(),
            )
            with pytest.raises(ConnectorError, match="allow-listed"):
                await fetch_limited(context, "https://1001.tl/fixture")

    asyncio.run(scenario())


def test_connector_job_cursor_caps_and_terminal_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with AsyncJobStore(tmp_path / "jobs.sqlite") as store:
            job = await store.ensure_connector_job(
                MEDIA_KEY, "sc_comments", "target-ref", page_cap=25, item_cap=5_000
            )
            leased = await store.lease_connector(job.id, "owner")
            assert leased is not None
            checkpoint = await store.checkpoint_connector(
                job.id,
                "owner",
                cursor="next-page-ref",
                page=1,
                items_fetched=200,
                result_path="result-ref",
                truncated=False,
            )
            assert checkpoint.cursor == "next-page-ref" and checkpoint.items_fetched == 200
            final = await store.finish_connector(
                job.id, "owner", "succeeded", result_path="result-ref"
            )
            assert final.state == "succeeded"

    asyncio.run(scenario())


def test_youtube_enforces_200_parents_and_20_replies_per_thread() -> None:
    comments: list[dict[str, object]] = []
    for parent_index in range(MAX_PARENT_THREADS + 5):
        parent_id = f"parent-{parent_index}"
        comments.append({"id": parent_id, "parent": "root", "text": f"parent {parent_index}"})
        comments.extend(
            {
                "id": f"reply-{parent_index}-{reply_index}",
                "parent": parent_id,
                "text": f"reply {reply_index}",
            }
            for reply_index in range(MAX_REPLIES_PER_THREAD + 5)
        )
    parsed = parse_comments({"comments": comments}, media_key=MEDIA_KEY)
    parents = [item for item in parsed if item.parent_source_id is None]
    replies = [item for item in parsed if item.parent_source_id is not None]
    assert len(parsed) == MAX_COMMENTS
    assert len(parents) == MAX_PARENT_THREADS
    assert len(replies) == MAX_REPLIES
    assert (
        max(
            sum(item.parent_source_id == parent.source_record_id for item in replies)
            for parent in parents
        )
        == MAX_REPLIES_PER_THREAD
    )
    assert all(int(item.parent_source_id.split("-")[1]) < 200 for item in replies)


def test_youtube_rejects_nonzero_extractor_exit_even_with_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failed_process(*_args: object, **_kwargs: object) -> ProcessResult:
        return ProcessResult(("yt-dlp",), 2, '{"comments":[]}', "failed")

    monkeypatch.setattr(youtube_connector, "run_process", failed_process)

    async def scenario() -> None:
        async with httpx.AsyncClient() as client:
            context = ConnectorContext(
                source=_source(),
                duration_ms=600_000,
                media_dir=tmp_path,
                cache_root=tmp_path,
                store=AsyncJobStore(tmp_path / "unused-youtube.sqlite"),
                job=_job("yt_comments"),
                owner="owner",
                http=client,
                breaker=CircuitBreaker(),
            )
            with pytest.raises(ConnectorError, match="exited 2"):
                await youtube_connector.fetch_comments(context)

    asyncio.run(scenario())


def test_pointer_pages_have_unique_natural_keys() -> None:
    html = (FIXTURES / "pointer-1001-authored.html").read_text(encoding="utf-8")
    first = parse_pointer_html(
        html,
        final_url="https://1001.tl/first",
        mirror_of="source-ref:fixture",
        truncated=False,
    )
    second = parse_pointer_html(
        html,
        final_url="https://1001.tl/second",
        mirror_of="source-ref:fixture",
        truncated=False,
    )
    hints = parse_hint_inputs(MEDIA_KEY, 600_000, [*first.inputs, *second.inputs])
    assert len(hints) == 4
    assert len({hint.id for hint in hints}) == 4
    assert first.inputs[0].source_record_id != second.inputs[0].source_record_id


@pytest.mark.parametrize("query_key", ["token", "signature", "credential", "api-key"])
def test_credential_pointer_text_never_reaches_hint_artifacts(query_key: str) -> None:
    url = f"https://1001.tl/fixture?{query_key}=super-secret-value"
    inputs = [
        HintInput(
            connector="yt_description",
            source_record_id="description",
            text=(f"00:00 Artist One - Title One {url}\n05:00 Artist Two - Title Two"),
            structured_tracklist=True,
        )
    ]
    hints = parse_hint_inputs(MEDIA_KEY, 600_000, inputs)
    assert hints
    assert all("super-secret-value" not in hint.model_dump_json() for hint in hints)


def test_pointer_extracts_release_metadata() -> None:
    html = (
        '<meta name="platform-id" content="platform-17">'
        '<meta name="uploader-id" content="uploader-4">'
        '<meta name="upload-date" content="20260904">'
        '<meta itemprop="duration" content="PT10M">'
        + (FIXTURES / "pointer-1001-authored.html").read_text(encoding="utf-8")
    )
    output = parse_pointer_html(
        html,
        final_url="https://1001.tl/metadata",
        mirror_of="source-ref:fixture",
        truncated=False,
    )
    assert output.mirror_candidate is not None
    assert output.mirror_candidate.platform_id == "platform-17"
    assert output.mirror_candidate.uploader_id == "uploader-4"
    assert output.mirror_candidate.upload_date == "20260904"
    assert output.mirror_candidate.duration_ms == 600_000


def test_mixesdb_requires_the_exact_requested_page() -> None:
    content = "== Tracklist ==\n# [00] Artist - Title"
    keyed = {"query": {"pages": {"99": {"revisions": [{"*": content}]}}}}
    listed = {
        "query": {"pages": [{"pageid": 99, "revisions": [{"slots": {"main": {"*": content}}}]}]}
    }
    assert revision_content(keyed, "17") is None
    assert revision_content(listed, "17") is None
    keyed["query"]["pages"]["17"] = {"revisions": [{"*": "requested"}]}
    assert revision_content(keyed, "17") == "requested"


def test_soundcloud_refresh_ignores_stale_pages_beyond_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def client_id(*_args: object, **_kwargs: object) -> str:
        return "a" * 32

    monkeypatch.setattr(soundcloud_connector, "discover_client_id", client_id)

    async def scenario() -> None:
        async with AsyncJobStore(tmp_path / "jobs.sqlite") as store:
            job = await store.ensure_connector_job(
                MEDIA_KEY, "sc_comments", "target-ref", page_cap=25, item_cap=5_000
            )
            leased = await store.lease_connector(job.id, "owner")
            assert leased is not None

            def handler(request: httpx.Request) -> httpx.Response:
                assert parse_qs(request.url.query.decode())["client_id"] == ["a" * 32]
                return httpx.Response(
                    200,
                    json={
                        "collection": [{"id": "fresh", "body": "track id?", "timestamp": 1_000}],
                        "next_href": None,
                    },
                )

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                context = ConnectorContext(
                    source=_source("soundcloud"),
                    duration_ms=600_000,
                    media_dir=tmp_path,
                    cache_root=tmp_path / "cache",
                    store=store,
                    job=leased,
                    owner="owner",
                    http=client,
                    breaker=CircuitBreaker(),
                )
                write_raw_json(
                    context.raw_path("page-001.json"),
                    {"collection": [{"id": "stale", "body": "Artist - Stale"}]},
                )
                output = await soundcloud_connector.fetch_comments(
                    context, {"id": "track", "user_id": 1}
                )
                assert [item.source_record_id for item in output.inputs] == ["fresh"]
                await store.finish_connector(job.id, "owner", "succeeded", result_path="result-ref")

    asyncio.run(scenario())


def test_pointer_wall_clock_timeout_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.05)
            yield b"content"

    monkeypatch.setattr(pointer_connector, "TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=SlowStream()))
        ) as client:
            context = ConnectorContext(
                source=_source(),
                duration_ms=600_000,
                media_dir=tmp_path,
                cache_root=tmp_path,
                store=AsyncJobStore(tmp_path / "unused-timeout.sqlite"),
                job=_job(),
                owner="owner",
                http=client,
                breaker=CircuitBreaker(),
            )
            with pytest.raises(RetryableConnectorError, match="wall-clock"):
                await fetch_limited(context, "https://1001.tl/fixture")

    asyncio.run(scenario())

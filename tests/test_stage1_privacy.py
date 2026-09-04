from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from id_detector.ingest import ingest
from id_detector.journal import InvocationTimer, append_invocation


@pytest.mark.parametrize(
    "url",
    [
        "https://example.invalid/audio?api_key=query-secret",
        "https://url-user:url-password@example.invalid/audio",
        "https://example.invalid/audio?X-Amz-Signature=signed-secret",
        "https://example.invalid/audio?client_id=oauth-client-secret",
    ],
)
def test_ingest_rejects_credential_bearing_urls_before_writing(tmp_path: Path, url: str) -> None:
    work_root = tmp_path / "work"
    with pytest.raises(ValueError, match="credential-bearing"):
        asyncio.run(ingest(url, work_root))
    assert not work_root.exists()


def test_invocation_journal_redacts_url_query_and_userinfo(tmp_path: Path) -> None:
    timer = InvocationTimer(
        "privacy-test",
        [
            "analyse",
            "https://journal-user:journal-password@example.invalid/audio?api_key=journal-secret",
        ],
    )
    entry = timer.entry(
        status="failed",
        exit_code=1,
        counts={},
        costs={},
        source_ids=[],
        ffmpeg_version=None,
    )
    journal = tmp_path / "invocations.jsonl"
    append_invocation(journal, entry)
    persisted = journal.read_text(encoding="utf-8")
    for secret in ("journal-user", "journal-password", "journal-secret"):
        assert secret not in persisted
    assert "api_key" in persisted
    assert "REDACTED" in persisted

"""Phase 0a-i gate: paid-path crash/cache/status fixes and shared offline infrastructure."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import wave
from hashlib import sha256
from pathlib import Path
from urllib.request import urlopen

from typer.testing import CliRunner

from id_detector.cli import _analyse
from id_detector.io import native_path
from id_detector.providers import audd as audd_module
from id_detector.providers.audd import AudDAdapter, AudDCredentials
from id_detector.providers.base import AppConfig
from id_detector.shazam import CircuitBreaker, InjectedHTTPClient, TokenBucket
from id_detector.windows import generation_zero_schedule
from scripts.make_audio_fixtures import generate
from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff

ROOT = Path(__file__).resolve().parents[1]
AUDIO = ROOT / "tests" / "fixtures" / "audio" / "tone-60s.wav"
SCRIPTS = ROOT / "tests" / "fakes" / "scripts"


def _entry(work_root: Path) -> tuple[Path, dict[str, object]]:
    (path,) = work_root.rglob("invocations.jsonl")
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return path.parent, entries[-1]


def _raw_payloads(raw_dir: Path) -> list[dict[str, object]]:
    directory = native_path(raw_dir)
    if not os.path.isdir(directory):
        return []
    payloads: list[dict[str, object]] = []
    for name in os.listdir(directory):
        if name.endswith(".json"):
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                payloads.append(json.load(handle))
    return payloads


def _run_analysis(
    tmp_path: Path,
    script_name: str,
    *,
    max_requests: int = 100,
    progress_messages: list[str] | None = None,
) -> tuple[int, FakeAudD, FakeShazamHTTP]:
    script = SCRIPTS / script_name
    audd = FakeAudD(script)
    shazam = FakeShazamHTTP(script)
    result = asyncio.run(
        _analyse(
            str(AUDIO),
            work_root=tmp_path / "work",
            print_raw=False,
            refresh=False,
            max_requests=max_requests,
            tracklist=None,
            no_hints=True,
            app_config=AppConfig(
                transforms_policy="off",
                recognise_concurrency=1,
                shazam_requests_per_minute=1_000_000,
                audd_requests_per_minute=1_000_000,
            ),
            max_generations=0,
            novelty=False,
            enabled_engines=("audd",),
            primary_engine="audd",
            paid_scan_adapters={"audd": audd},
            shazam_http_client=shazam,
            paid_sleep=no_backoff,
            progress=(
                (lambda _phase, _done, _total, message: progress_messages.append(message))
                if progress_messages is not None
                else None
            ),
        )
    )
    return result, audd, shazam


def test_committed_audio_fixture_is_reproducible_and_forms_seven_windows(tmp_path: Path) -> None:
    with wave.open(str(AUDIO), "rb") as fixture:
        assert fixture.getnchannels() == 1
        assert fixture.getsampwidth() == 2
        assert fixture.getframerate() == 16_000
        assert fixture.getnframes() == 60 * 16_000
    assert len(generation_zero_schedule(60_000)) == 7

    regenerated = tmp_path / "tone-60s.wav"
    generate(regenerated, 60)
    assert sha256(regenerated.read_bytes()).digest() == sha256(AUDIO.read_bytes()).digest()


def test_fixture_audit_accepts_committed_audio_and_screenshots() -> None:
    from scripts.audit_fixtures import audit

    assert audit() == []


def test_fixture_audit_checks_binary_filenames_before_skipping_contents(
    tmp_path: Path, monkeypatch
) -> None:
    import scripts.audit_fixtures as fixture_audit

    fixtures = tmp_path / "tests" / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "1234567890.wav").write_bytes(b"\x00\xfffixture")
    monkeypatch.setattr(fixture_audit, "ROOT", tmp_path)
    monkeypatch.setattr(fixture_audit, "RAW_ROOT", tmp_path / "missing-raw")
    monkeypatch.setattr(fixture_audit, "SCAN_ROOTS", (fixtures,))

    assert fixture_audit.audit() == [
        "tests\\fixtures\\1234567890.wav: filename contains a numeric platform ID"
        if os.name == "nt"
        else "tests/fixtures/1234567890.wav: filename contains a numeric platform ID"
    ]


def test_paid_primary_success_completes_without_the_branch_local_crash(tmp_path: Path) -> None:
    exit_code, audd, _shazam = _run_analysis(tmp_path, "gate0a-deep.json")

    media_dir, entry = _entry(tmp_path / "work")
    assert exit_code == 0
    assert entry["status"] == "complete"
    assert entry["counts"]["paid_resolved"] == 7  # type: ignore[index]
    assert entry["counts"]["paid_billable_units"] == 7  # type: ignore[index]
    assert audd.calls == audd.billed_units == 7
    assert (media_dir / "present" / "index.html").is_file()


def test_paid_no_match_is_resolved_cached_and_gap_counts_accumulate(tmp_path: Path) -> None:
    exit_code, audd, shazam = _run_analysis(tmp_path, "all-no-match.json")

    media_dir, entry = _entry(tmp_path / "work")
    raw_dir = media_dir / "recognise" / "invocations" / "live-audd-clip-v1" / "raw"
    cached = _raw_payloads(raw_dir)
    assert exit_code == 0
    assert len(cached) == 7
    assert all(item == {"status": "success", "result": None} for item in cached)
    assert entry["counts"]["paid_resolved"] == 7  # type: ignore[index]
    # Seven AudD no-matches leave the whole mix blank, so the targeting:0 secondary sends the
    # capacity of C = ceil(1 min x 2) = 2 Shazam clips to it (plan §2.3.4 step 4).
    assert entry["counts"]["requests"] == shazam.requests == 2  # type: ignore[index]
    assert entry["counts"]["physical_attempts"] == 2  # type: ignore[index]
    assert entry["counts"]["secondary_allocated"] == 2  # type: ignore[index]
    assert audd.billed_units == 7

    # The user-facing default refreshes cached no-match states, but preserves Shazam matches.
    second_code, second_audd, second_shazam = _run_analysis(tmp_path, "all-no-match.json")
    _media_dir, second_entry = _entry(tmp_path / "work")
    assert second_code == 0
    assert second_audd.calls == 7
    assert second_entry["counts"]["paid_cache_hits"] == 0  # type: ignore[index]
    assert second_shazam.requests == 0


def test_shazam_no_match_refresh_gets_a_fresh_allowance_after_exhausting_budget(
    tmp_path: Path,
) -> None:
    async def analyse_with(client: FakeShazamHTTP, run_id: str) -> int:
        del run_id
        return await _analyse(
            str(AUDIO),
            work_root=tmp_path / "work",
            print_raw=False,
            refresh=False,
            max_requests=7,
            tracklist=None,
            no_hints=True,
            app_config=AppConfig(
                transforms_policy="off",
                recognise_concurrency=1,
                shazam_requests_per_minute=1_000_000,
            ),
            max_generations=0,
            novelty=False,
            enabled_engines=(),
            primary_engine="shazam",
            shazam_http_client=client,
        )

    script = {"shazam": {"default": "no_match", "windows": {}}}
    first = FakeShazamHTTP(script)
    second = FakeShazamHTTP(script)
    assert asyncio.run(analyse_with(first, "first")) == 0
    assert first.requests == 7
    assert asyncio.run(analyse_with(second, "refresh")) == 0
    assert second.requests == 7


def test_identity_less_audd_result_is_malformed_and_never_cached(tmp_path: Path) -> None:
    exit_code, audd, shazam = _run_analysis(tmp_path, "all-malformed.json")

    media_dir, entry = _entry(tmp_path / "work")
    raw_dir = media_dir / "recognise" / "invocations" / "live-audd-clip-v1" / "raw"
    # Plan §2.3.5: ``malformed`` is billable and ambiguous, not a terminal-provider outcome, so a
    # sweep of them is a primary that resolved 0 of 7 (< 95 %): ``partial``, exit 0, spent.
    assert exit_code == 0
    assert entry["status"] == "partial"
    assert entry["reason"] == "primary_not_achieved"
    assert entry["counts"]["paid_resolved"] == 0  # type: ignore[index]
    assert entry["counts"]["paid_billable_units"] == 7  # type: ignore[index]
    assert entry["usd_e6_spent"] == 35_000
    assert audd.calls == audd.billed_units == 7
    assert shazam.requests == 2  # the secondary probes the blank mix, nothing more
    assert not _raw_payloads(raw_dir)


def test_all_http_401_is_provider_unavailable_unspent_and_never_cached(tmp_path: Path) -> None:
    progress_messages: list[str] = []
    exit_code, audd, shazam = _run_analysis(
        tmp_path, "all-http-401.json", progress_messages=progress_messages
    )

    media_dir, entry = _entry(tmp_path / "work")
    raw_dir = media_dir / "recognise" / "invocations" / "live-audd-clip-v1" / "raw"
    assert exit_code == 3
    assert entry["status"] == "provider_unavailable"
    assert entry["reason"] == "auth_error"
    assert entry["achieved"] is None
    assert entry["exit_code"] == 3
    assert entry["costs"] == {"usd_e2": 0}
    assert entry["counts"]["paid_billable_units"] == 0  # type: ignore[index]
    assert entry["counts"]["paid_resolved"] == 0  # type: ignore[index]
    # A terminal-provider outcome stops the primary at once (§2.3.3): one dispatch, six not sent.
    assert audd.calls == 1 and audd.billed_units == 0
    assert shazam.requests == 0
    assert not _raw_payloads(raw_dir)
    assert not (media_dir / "present" / "index.html").exists()
    assert any("(1 requests, 0 cached, 6 not sent)" in message for message in progress_messages)
    assert all("billable" not in message for message in progress_messages)


def test_real_web_runner_fails_exit_3_without_attaching_stale_result(
    tmp_path: Path, monkeypatch
) -> None:
    from id_detector import cli
    from id_detector.webapp.jobs import JobManager
    from id_detector.webapp.runner import make_pipeline_runner

    stale_media = tmp_path / "stale-media"
    stale_index = stale_media / "present" / "index.html"
    stale_index.parent.mkdir(parents=True)
    stale_index.write_text("stale result", encoding="utf-8")
    cache_loads = 0

    async def unavailable_analyse(_target: str, **_kwargs: object) -> int:
        return 3

    def stale_cached(_work_root: Path, _target: str):
        nonlocal cache_loads
        cache_loads += 1
        return type("Cached", (), {"media_dir": stale_media})()

    monkeypatch.setattr(cli, "_analyse", unavailable_analyse)
    monkeypatch.setattr(cli, "_load_cached", stale_cached)
    runner = make_pipeline_runner(
        tmp_path,
        project_root=ROOT,
        config_path=tmp_path / "missing-idea.toml",
    )
    manager = JobManager(tmp_path, runner)
    try:
        job_id = manager.submit(str(AUDIO), "max_accuracy")
        deadline = time.monotonic() + 5
        while manager.get(job_id).status not in {"failed", "succeeded"}:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        job = manager.get(job_id)
        assert job.status == "failed"
        assert job.error and "exit code 3" in job.error
        assert job.result_path is None
        assert cache_loads == 0
        assert stale_index.read_text(encoding="utf-8") == "stale result"
    finally:
        manager.shutdown()


def test_paid_result_contract_was_relocated_without_breaking_compatibility() -> None:
    from id_detector.paid_clip import PaidScanResult as Relocated
    from id_detector.scan import PaidScanResult as CompatibleImport

    assert CompatibleImport is Relocated


def test_fake_provider_cli_is_hidden_guarded_and_injects_both_boundaries(
    tmp_path: Path, monkeypatch
) -> None:
    from id_detector import cli

    runner = CliRunner()
    monkeypatch.delenv("IDEA_TEST_MODE", raising=False)
    refused = runner.invoke(
        cli.app,
        ["analyse", str(AUDIO), "--fake-providers", "audd,shazam"],
    )
    assert refused.exit_code == 2
    assert "IDEA_TEST_MODE=1" in refused.output
    assert "fake-providers" not in runner.invoke(cli.app, ["analyse", "--help"]).output

    captured: dict[str, object] = {}

    async def fake_analyse(_url: str, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_analyse", fake_analyse)
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    monkeypatch.setenv("IDEA_FAKE_SCRIPT", str(SCRIPTS / "gate0a-deep.json"))
    accepted = runner.invoke(
        cli.app,
        [
            "analyse",
            str(AUDIO),
            "--fake-providers",
            "audd,shazam",
            "--work-root",
            str(tmp_path / "work"),
        ],
    )
    assert accepted.exit_code == 0
    assert type(captured["paid_scan_adapters"]["audd"]).__name__ == "FakeAudD"  # type: ignore[index]
    assert type(captured["shazam_http_client"]).__name__ == "FakeShazamHTTP"


def test_provider_http_clients_ignore_proxy_environment(tmp_path: Path, monkeypatch) -> None:
    options: list[dict[str, object]] = []

    class Response:
        status_code = 200
        headers: dict[str, str] = {}
        text = "{}"

        @staticmethod
        def json() -> dict[str, object]:
            return {"status": "success", "result": None}

    class ShazamResponse(Response):
        # The Shazam client (0b-ii) accepts only a recognition body: one with a ``matches`` list.
        @staticmethod
        def json() -> dict[str, object]:
            return {"matches": []}

    class Client:
        def __init__(self, **kwargs: object) -> None:
            options.append(kwargs)

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, *args: object, **kwargs: object) -> Response:
            return Response()

        async def request(self, *args: object, **kwargs: object) -> Response:
            return ShazamResponse()

    monkeypatch.setattr(audd_module.httpx, "AsyncClient", Client)
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"fixture")

    async def scenario() -> None:
        adapter = AudDAdapter(AudDCredentials("fixture"), AppConfig(), False)
        assert await adapter.recognize_clip(clip, lambda: asyncio.sleep(0)) == {
            "status": "success",
            "result": None,
        }
        client = InjectedHTTPClient(
            on_attempt=lambda: asyncio.sleep(0),
            limiter=TokenBucket(rate_per_minute=60),
            breaker=CircuitBreaker(),
        )
        assert await client.request("GET", "https://fixture.invalid") == {"matches": []}

    asyncio.run(scenario())
    assert len(options) == 2
    assert all(option["trust_env"] is False for option in options)


def test_healthz_and_journal_assertion_helpers(tmp_path: Path) -> None:
    from id_detector.present.server import serve_in_background

    server = serve_in_background(tmp_path / "served")
    try:
        with urlopen(f"{server.base_url}/healthz", timeout=5) as response:
            assert response.status == 200
            assert json.load(response) == {"ok": True}
    finally:
        server.shutdown()

    journal = tmp_path / "journal-work" / "source" / "media" / "invocations.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        json.dumps(
            {
                "started_at": "2026-09-09T00:00:00Z",
                "finished_at": "2026-09-09T00:00:01Z",
                "status": "provider_unavailable",
                "counts": {"paid_billable_units": 0},
                "costs": {"usd_e2": 0},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    command = [
        sys.executable,
        str(ROOT / "scripts" / "assert_journal.py"),
        "--work-root",
        str(tmp_path / "journal-work"),
        "--expect",
        "status=provider_unavailable",
        "paid_billable_units=0",
        "costs.usd_e2=0",
    ]
    passed = subprocess.run(command, check=False, capture_output=True, text=True)
    assert passed.returncode == 0
    assert "journal assertion passed" in passed.stdout

    failed = subprocess.run(
        [*command[:-1], "costs.usd_e2=1"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert failed.returncode == 1
    assert "expected 1, got 0" in failed.stdout

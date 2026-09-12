"""Phase 0a-iii gate: dead paid paths unreachable or refused, ``upload_consent`` gone, ``tl1001``
default-disabled, the web index wired into the analysis, ``_windows_in_spans`` frozen-only, loopback
CSRF, and the owner-run spike scripts present and guarded.  Everything is offline."""

from __future__ import annotations

import asyncio
import json
import os
import py_compile
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from id_detector import cli
from id_detector import scan as scan_module
from id_detector.contracts import SourceRecord, Transform, WindowRecord, derive_source_key
from id_detector.hints.pipeline import run_hints
from id_detector.io import atomic_write_json
from id_detector.present.server import _home_html, _job_page_html, _new_html, serve_in_background
from id_detector.providers.base import DEFAULT_DISABLED_HINT_CONNECTORS, AppConfig
from id_detector.recipes import DEEP_RECIPE, FREE_RECIPE, Recipe
from id_detector.webapp import runner as runner_module
from id_detector.webapp.jobs import Job, JobContext, JobManager
from id_detector.webapp.runner import (
    WEB_INDEX_LABEL,
    WEB_INDEX_ROOT,
    WEB_PANAKO_TOOL_DIR,
    make_pipeline_runner,
)
from id_detector.windows import WindowsResult
from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff

ROOT = Path(__file__).resolve().parents[1]
AUDIO = ROOT / "tests" / "fixtures" / "audio" / "tone-60s.wav"
SCRIPTS = ROOT / "tests" / "fakes" / "scripts"
GOLDEN = ROOT / "tests" / "golden"
TIMEOUT = httpx.Timeout(5.0)
CLEAN_URL = "https://soundcloud.com/example/live-mix"


def _wait(predicate, timeout: float = 5.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _gated_runner(started: threading.Event, gate: threading.Event):
    def runner(ctx: JobContext) -> None:
        started.set()
        gate.wait(timeout=5)

    return runner


def _analyse(tmp_path: Path, *, recipe: Recipe, **overrides: object):
    script = SCRIPTS / "gate0a-deep.json"
    audd = FakeAudD(script)
    kwargs: dict[str, object] = {
        "work_root": tmp_path / "work",
        "print_raw": False,
        "refresh": False,
        "max_requests": 100,
        "tracklist": None,
        "no_hints": True,
        "app_config": AppConfig(
            transforms_policy="off",
            recognise_concurrency=1,
            shazam_requests_per_minute=1_000_000,
            audd_requests_per_minute=1_000_000,
            allow_third_party_upload=True,
        ),
        "max_generations": 0,
        "novelty": False,
        "enabled_engines": ("shazam", "audd", "acrcloud"),
        "cli_confirmation": True,
        "primary_engine": recipe.primary_engine,
        "recipe": recipe,
        "paid_scan_adapters": {"audd": audd},
        "shazam_http_client": FakeShazamHTTP(script),
        "paid_sleep": no_backoff,
    }
    kwargs.update(overrides)
    code = asyncio.run(cli._analyse(str(AUDIO), **kwargs))  # type: ignore[arg-type]
    (path,) = (tmp_path / "work").rglob("invocations.jsonl")
    entry = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    return code, audd, entry


# --------------------------------------------------------------------------------------------------
# Loopback CSRF (U-F5)
# --------------------------------------------------------------------------------------------------
def test_post_analyse_requires_the_token_and_a_loopback_origin(tmp_path: Path) -> None:
    started, gate = threading.Event(), threading.Event()
    manager = JobManager(tmp_path, _gated_runner(started, gate))
    running = serve_in_background(tmp_path, port=0, job_manager=manager)
    base = running.base_url
    body = {"url": CLEAN_URL, "profile": "free"}
    try:
        # No token: refused before any job exists.
        bare = httpx.post(base + "/analyse", json=body, timeout=TIMEOUT)
        assert bare.status_code == 403 and "CSRF" in bare.json()["error"]
        assert manager.recent() == []
        token = httpx.get(base + "/csrf", timeout=TIMEOUT).json()["token"]
        assert len(token) >= 32
        # A foreign Origin (any other website the owner has open) is refused even with the token.
        foreign = httpx.post(
            base + "/analyse",
            json=body,
            headers={"X-CSRF-Token": token, "Origin": "https://evil.example"},
            timeout=TIMEOUT,
        )
        assert foreign.status_code == 403 and "origin" in foreign.json()["error"]
        # A non-loopback Host (DNS rebinding) is refused.
        rebound = httpx.post(
            base + "/analyse",
            json=body,
            headers={"X-CSRF-Token": token, "Host": "attacker.example:80"},
            timeout=TIMEOUT,
        )
        assert rebound.status_code == 403 and "host" in rebound.json()["error"]
        # A wrong token is refused.
        wrong = httpx.post(
            base + "/analyse", json=body, headers={"X-CSRF-Token": "x" * 43}, timeout=TIMEOUT
        )
        assert wrong.status_code == 403
        assert manager.recent() == []
        # The token in the header with a loopback Origin creates the job (JSON API).
        created = httpx.post(
            base + "/analyse",
            json=body,
            headers={"X-CSRF-Token": token, "Origin": f"http://127.0.0.1:{running.port}"},
            timeout=TIMEOUT,
        )
        assert created.status_code == 200
        first_id = created.json()["id"]
        assert manager.get(first_id) is not None
        # The hidden form field works for the browser form (any loopback authority).
        form = httpx.post(
            base + "/analyse",
            data={**body, "csrf_token": token},
            headers={"Origin": f"http://localhost:{running.port}"},
            timeout=TIMEOUT,
            follow_redirects=False,
        )
        assert form.status_code == 303
        # Cancel needs the token too.
        assert started.wait(timeout=5)
        refused = httpx.post(f"{base}/jobs/{first_id}/cancel", timeout=TIMEOUT)
        assert refused.status_code == 403
        cancelled = httpx.post(
            f"{base}/jobs/{first_id}/cancel", headers={"X-CSRF-Token": token}, timeout=TIMEOUT
        )
        assert cancelled.status_code == 200 and cancelled.json()["cancelled"] is True
    finally:
        gate.set()
        running.shutdown()
        manager.shutdown()


def test_unknown_post_is_origin_gated_even_on_the_read_only_server(tmp_path: Path) -> None:
    """The loopback gate runs before routing, so it protects routes that do not even exist.

    ``/rescan`` is the case that matters: §2.5 removed the route, and the removal must not be what
    is doing the protecting — a foreign ``Origin`` is still refused with 403 *before* dispatch, and
    only a same-origin request gets as far as the 404 that says the route is gone.
    """

    running = serve_in_background(tmp_path, port=0)
    try:
        foreign = httpx.post(
            running.base_url + "/rescan",
            json={"media_key": "f" * 64},
            headers={"Origin": "https://evil.example"},
            timeout=TIMEOUT,
        )
        assert foreign.status_code == 403
        assert "cross-site request refused" in foreign.json()["error"]
        # Without a foreign Origin the gate passes and routing answers: the route is gone (§2.5).
        local = httpx.post(running.base_url + "/rescan", content=b"", timeout=TIMEOUT)
        assert local.status_code == 404
        # The read-only server has no analyse route, token or not.
        token = httpx.get(running.base_url + "/csrf", timeout=TIMEOUT).json()["token"]
        absent = httpx.post(
            running.base_url + "/analyse",
            json={"url": CLEAN_URL},
            headers={"X-CSRF-Token": token},
            timeout=TIMEOUT,
        )
        assert absent.status_code == 404
    finally:
        running.shutdown()


def test_every_refused_post_delivers_its_answer_instead_of_resetting_the_connection(
    tmp_path: Path,
) -> None:
    """A POST answered without reading its body must drain it first.

    Closing the socket with request bytes still in flight makes Windows abort the connection, so
    the client sees a ``ReadError`` rather than the 403/404/400 — intermittently, which is how it
    showed up as a flaky suite.  Every refusal path is exercised repeatedly with a real body.
    """

    started, gate = threading.Event(), threading.Event()
    manager = JobManager(tmp_path, _gated_runner(started, gate))
    app = serve_in_background(tmp_path, port=0, job_manager=manager)
    read_only = serve_in_background(tmp_path, port=0)
    payload = {"url": CLEAN_URL, "profile": "free", "padding": "p" * 4_000}
    big = json.dumps({"url": CLEAN_URL, "padding": "p" * 20_000})
    try:
        for _ in range(25):
            # No job manager: /analyse is a 404 that never reads the body.
            assert (
                httpx.post(
                    read_only.base_url + "/analyse", json=payload, timeout=TIMEOUT
                ).status_code
                == 404
            )
            # Over the 8 KiB ceiling: refused before the body is read.
            assert (
                httpx.post(
                    app.base_url + "/analyse",
                    content=big,
                    headers={"Content-Type": "application/json"},
                    timeout=TIMEOUT,
                ).status_code
                == 400
            )
            # Cross-site: refused before routing.
            assert (
                httpx.post(
                    app.base_url + "/analyse",
                    json=payload,
                    headers={"Origin": "https://evil.example"},
                    timeout=TIMEOUT,
                ).status_code
                == 403
            )
            # Token-less cancel, with a body the handler never reads.
            assert (
                httpx.post(
                    f"{app.base_url}/jobs/{'a' * 32}/cancel", json=payload, timeout=TIMEOUT
                ).status_code
                == 403
            )
            # The removed /rescan route, with a body the handler never reads (§2.5).
            assert (
                httpx.post(read_only.base_url + "/rescan", content=big, timeout=TIMEOUT).status_code
                == 404
            )
        assert manager.recent() == []
    finally:
        gate.set()
        read_only.shutdown()
        app.shutdown()
        manager.shutdown()


def test_served_pages_carry_the_csrf_token() -> None:
    home = _home_html([], [], "tok-home").decode("utf-8")
    assert 'name="csrf_token" value="tok-home"' in home
    fresh = _new_html("", "tok-new").decode("utf-8")
    assert 'name="csrf_token" value="tok-new"' in fresh
    job = Job(
        id="a" * 32,
        target=CLEAN_URL,
        display=CLEAN_URL,
        profile="free",
        acquire=True,
        build_index=False,
    )
    page = _job_page_html(job, "tok-job").decode("utf-8")
    assert 'var CSRF_TOKEN="tok-job";' in page
    assert "'X-CSRF-Token': CSRF_TOKEN" in page


def test_live_home_page_embeds_the_same_token_the_csrf_route_serves(tmp_path: Path) -> None:
    manager = JobManager(tmp_path, lambda _ctx: None)
    running = serve_in_background(tmp_path, port=0, job_manager=manager)
    try:
        token = httpx.get(running.base_url + "/csrf", timeout=TIMEOUT).json()["token"]
        home = httpx.get(running.base_url + "/", timeout=TIMEOUT).text
        assert f'name="csrf_token" value="{token}"' in home
        new = httpx.get(running.base_url + "/new", follow_redirects=True, timeout=TIMEOUT)
        assert new.url.path == "/"
        assert f'name="csrf_token" value="{token}"' in new.text
    finally:
        running.shutdown()
        manager.shutdown()


# --------------------------------------------------------------------------------------------------
# E-H8 — upload_consent dropped; the whole-file scanner is unreachable
# --------------------------------------------------------------------------------------------------
def test_upload_consent_is_not_read_from_any_body_and_no_longer_exists(tmp_path: Path) -> None:
    started, gate = threading.Event(), threading.Event()
    manager = JobManager(tmp_path, _gated_runner(started, gate))
    running = serve_in_background(tmp_path, port=0, job_manager=manager)
    try:
        token = httpx.get(running.base_url + "/csrf", timeout=TIMEOUT).json()["token"]
        created = httpx.post(
            running.base_url + "/analyse",
            json={"url": CLEAN_URL, "profile": "max_accuracy", "upload_consent": True},
            headers={"X-CSRF-Token": token},
            timeout=TIMEOUT,
        )
        assert created.status_code == 200
        job = manager.get(created.json()["id"])
        assert job is not None and not hasattr(job, "upload_consent")
        assert "upload_consent" not in job.status_dict()
    finally:
        gate.set()
        running.shutdown()
        manager.shutdown()
    with pytest.raises(TypeError):
        JobManager(tmp_path, lambda _ctx: None).submit(CLEAN_URL, upload_consent=True)  # type: ignore[call-arg]
    assert not hasattr(JobContext, "upload_consent")


def test_whole_file_scanner_call_site_is_gone_from_analyse(tmp_path: Path, monkeypatch) -> None:
    assert not hasattr(cli, "run_paid_scanners")

    async def forbidden(*_args: object, **_kwargs: object):
        raise AssertionError("the whole-file scanner ran")

    monkeypatch.setattr(scan_module, "run_paid_scanners", forbidden)
    # Consent open, both paid families enabled: the Free recipe still makes no paid call at all.
    code, audd, entry = _analyse(tmp_path / "free", recipe=FREE_RECIPE)
    assert code == 0 and audd.calls == 0
    assert "scan_ms" not in entry["timings"] and "paid_matches" not in entry["counts"]
    assert entry["usd_e6_spent"] == 0
    # The Deep recipe pays only for its clip primary — never a second, whole-file charge.
    code, audd, entry = _analyse(tmp_path / "deep", recipe=DEEP_RECIPE)
    assert code == 0 and audd.calls == 7
    assert "scan_ms" not in entry["timings"] and "paid_matches" not in entry["counts"]
    assert entry["usd_e6_spent"] == 35_000


def test_engine_acrcloud_is_refused_and_the_consent_flag_is_inert(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_analyse(_url: str, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_analyse", fake_analyse)
    runner = CliRunner()
    refused = runner.invoke(cli.app, ["analyse", "http://example/set", "--engine", "acrcloud"])
    assert refused.exit_code == 2
    assert "acrcloud is refused" in refused.output
    assert not captured
    unknown = runner.invoke(cli.app, ["analyse", "http://example/set", "--engine", "other"])
    assert unknown.exit_code == 2 and "choose audd" in unknown.output
    accepted = runner.invoke(
        cli.app, ["analyse", "http://example/set", "--recipe", "deep", "--engine", "audd"]
    )
    assert accepted.exit_code == 0, accepted.output
    assert "audd" in captured["enabled_engines"]  # type: ignore[operator]
    consent = runner.invoke(
        cli.app, ["analyse", "http://example/set", "--i-own-this-audio-or-have-permission"]
    )
    assert consent.exit_code == 0, consent.output
    assert "has no effect" in consent.output
    assert "--i-own-this-audio" not in runner.invoke(cli.app, ["analyse", "--help"]).output


# --------------------------------------------------------------------------------------------------
# tl1001 default-disabled, and honoured by the pipeline
# --------------------------------------------------------------------------------------------------
def test_tl1001_is_default_disabled_and_needs_an_explicit_true(tmp_path: Path) -> None:
    assert AppConfig().disabled_hint_connectors == DEFAULT_DISABLED_HINT_CONNECTORS
    assert AppConfig().disabled_hint_connectors == frozenset({"tl1001"})
    empty = tmp_path / "empty.toml"
    empty.write_text("[hints]\n", encoding="utf-8")
    assert AppConfig.load(empty).disabled_hint_connectors == frozenset({"tl1001"})
    opted_in = tmp_path / "in.toml"
    opted_in.write_text("[hints]\ntl1001 = true\n", encoding="utf-8")
    assert AppConfig.load(opted_in).disabled_hint_connectors == frozenset()
    assert AppConfig.load(tmp_path / "absent.toml").disabled_hint_connectors == frozenset(
        {"tl1001"}
    )


@pytest.mark.parametrize("disabled", [frozenset({"tl1001"}), frozenset()])
def test_hint_pipeline_maps_the_tl1001_switch_onto_the_search_connector(
    tmp_path: Path, disabled: frozenset[str]
) -> None:
    # The title search runs only for platform sources; the golden source is a local file.
    url = "https://soundcloud.com/fixture-dj/fixture-mix"
    source = SourceRecord.model_validate_json(
        (GOLDEN / "source.json").read_text(encoding="utf-8")
    ).model_copy(
        update={
            "platform": "soundcloud",
            "input_url": url,
            "canonical_url": url,
            "source_key": derive_source_key(url),
        }
    )
    media_dir = tmp_path / "work" / source.source_key / source.media_key
    source_path = media_dir / "ingest" / "source.json"
    atomic_write_json(source_path, source)
    hosts: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(200, json={})

    async def scenario() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as client:
            result = await run_hints(
                source=source,
                duration_ms=600_000,
                media_dir=media_dir,
                source_path=source_path,
                project_root=tmp_path,
                http=client,
                disabled_connectors=disabled,
            )
        return json.loads(result.status_path.read_text(encoding="utf-8"))

    status = asyncio.run(scenario())
    by_name = {item["connector"]: item for item in status["connectors"]}
    searched = any("1001tracklists" in host for host in hosts)
    if disabled:
        assert by_name["tl1001_search"]["state"] == "disabled"
        assert not searched
    else:
        assert by_name["tl1001_search"]["state"] != "disabled"
        assert searched


@pytest.mark.parametrize("disabled", [frozenset({"mixcloud"}), frozenset()])
def test_hint_pipeline_maps_the_mixcloud_switch_onto_both_mixcloud_connectors(
    tmp_path: Path, disabled: frozenset[str]
) -> None:
    """``[hints] mixcloud = false`` is a documented switch, so it must actually reach the
    connectors — the pipeline calls them ``mixcloud_graphql`` and ``mixcloud_description``."""

    url = "https://www.mixcloud.com/fixture-dj/fixture-mix/"
    source = SourceRecord.model_validate_json(
        (GOLDEN / "source.json").read_text(encoding="utf-8")
    ).model_copy(
        update={
            "platform": "mixcloud",
            "input_url": url,
            "canonical_url": url,
            "source_key": derive_source_key(url),
        }
    )
    media_dir = tmp_path / "work" / source.source_key / source.media_key
    source_path = media_dir / "ingest" / "source.json"
    atomic_write_json(source_path, source)
    hosts: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(200, json={})

    async def scenario() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as client:
            result = await run_hints(
                source=source,
                duration_ms=600_000,
                media_dir=media_dir,
                source_path=source_path,
                project_root=tmp_path,
                http=client,
                disabled_connectors=disabled,
            )
        return json.loads(result.status_path.read_text(encoding="utf-8"))

    status = asyncio.run(scenario())
    by_name = {item["connector"]: item for item in status["connectors"]}
    states = {by_name[name]["state"] for name in ("mixcloud_graphql", "mixcloud_description")}
    contacted = any("mixcloud" in host for host in hosts)
    if disabled:
        assert states == {"disabled"}
        assert not contacted
    else:
        assert "disabled" not in states
        assert contacted


# --------------------------------------------------------------------------------------------------
# E-M9 — _windows_in_spans keeps only frozen windows
# --------------------------------------------------------------------------------------------------
def test_windows_in_spans_skips_transformed_and_rescan_windows() -> None:
    from id_detector.cli import _windows_in_spans

    frozen = WindowRecord.model_validate(
        json.loads((GOLDEN / "window.json").read_text(encoding="utf-8"))
    )
    transformed = frozen.model_copy(
        update={
            "id": "1" * 40,
            "transform": Transform(type="resample", rate_e4=9_600, semitones=0),
        }
    )
    rescan = frozen.model_copy(update={"id": "2" * 40, "generation": 1})
    windows = WindowsResult(
        records=(transformed, rescan, frozen), record_path=Path("w"), cached=True
    )
    lo = frozen.support_ms[0]
    assert _windows_in_spans(windows, ((lo, lo + 1),)).records == (frozen,)


# --------------------------------------------------------------------------------------------------
# E-H9 / D3 — the web runner queries the index it builds
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("build_index", [True, False])
def test_web_runner_wires_the_built_index_into_the_analysis(
    tmp_path: Path, monkeypatch, build_index: bool
) -> None:
    captured: dict[str, object] = {}
    built: list[str] = []

    async def capture_analyse(_target: str, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_analyse", capture_analyse)
    monkeypatch.setattr(cli, "_load_cached", lambda _root, _target: None)
    monkeypatch.setattr(
        runner_module,
        "_run_build_index",
        lambda ctx, target, *, project_root: built.append(target),
    )
    manager = JobManager(
        tmp_path,
        make_pipeline_runner(
            tmp_path, project_root=ROOT, config_path=tmp_path / "missing-idea.toml"
        ),
    )
    try:
        job_id = manager.submit(str(AUDIO), "free", build_index=build_index)
        assert _wait(lambda: manager.get(job_id).status in {"succeeded", "failed"})
        assert manager.get(job_id).status == "succeeded"
    finally:
        manager.shutdown()
    assert built == ([str(AUDIO)] if build_index else [])
    assert captured["local_index_label"] == (WEB_INDEX_LABEL if build_index else None)
    assert captured["index_root"] == WEB_INDEX_ROOT == Path("data/local/panako-db")
    assert captured["panako_tool_dir"] == WEB_PANAKO_TOOL_DIR == Path("data/local/panako")
    assert "cli_confirmation" not in captured


# --------------------------------------------------------------------------------------------------
# S2 / S3 owner-run spike scripts: present, valid, and refusing to run in a test context
# --------------------------------------------------------------------------------------------------
def test_spike_scripts_exist_compile_and_refuse_test_contexts(tmp_path: Path) -> None:
    shell = ROOT / "scripts" / "spike_ingest_vps.sh"
    python = ROOT / "scripts" / "spike_shazam_vps.py"
    text = shell.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env sh")
    assert "docs/spikes/ingest-vps.md" in text
    # Both spike scripts refuse the same two test contexts.
    assert "IDEA_TEST_MODE" in text and "PYTEST_CURRENT_TEST" in text
    py_compile.compile(str(python), doraise=True)
    env = {**os.environ, "IDEA_TEST_MODE": "1"}
    refused = subprocess.run(
        [sys.executable, str(python), "--minutes", "1", "--ceiling", "1", "--out", str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert refused.returncode == 2 and "refusing" in refused.stderr
    assert not any(tmp_path.iterdir())

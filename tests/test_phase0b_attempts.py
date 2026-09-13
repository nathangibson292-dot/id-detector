"""Phase 0b-iii gate: the durable attempt journal (``prepared`` → ``dispatched`` before network
I/O → ``resolved``), per-clip progress, the cancel token, and the resume rule for what an earlier
run left behind.  Everything is offline over the 60 s fixture (seven frozen windows)."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import wave
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

import pytest

from id_detector import cli, pipeline
from id_detector.attempts import (
    AttemptJournal,
    AttemptLedger,
    attempt_id_for,
    attempts_path,
    load_attempt_ledger,
)
from id_detector.contracts import ProviderAttemptEvent, WindowRecord, clip_cache_key
from id_detector.io import native_path
from id_detector.paid_clip import CLIP_CONFIG_VERSION, run_paid_clip_recognition
from id_detector.providers.base import AppConfig
from id_detector.recipes import DEEP_RECIPE
from id_detector.webapp.jobs import JobContext, JobManager
from id_detector.webapp.runner import make_pipeline_runner
from id_detector.windows import WindowsResult
from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff

ROOT = Path(__file__).resolve().parents[1]
AUDIO = ROOT / "tests" / "fixtures" / "audio" / "tone-60s.wav"
SCRIPTS = ROOT / "tests" / "fakes" / "scripts"
GOLDEN = ROOT / "tests" / "golden"
CONFIG = AppConfig(
    transforms_policy="off",
    recognise_concurrency=1,
    shazam_requests_per_minute=1_000_000,
    audd_requests_per_minute=1_000_000,
)


def _entry(work_root: Path) -> tuple[Path, dict[str, object]]:
    (path,) = work_root.rglob("invocations.jsonl")
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return path.parent, entries[-1]


def _events(media_dir: Path) -> list[dict[str, object]]:
    path = attempts_path(media_dir)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _count(work_root: Path, event: str) -> int:
    paths = list(work_root.rglob("attempts.jsonl"))
    if not paths:
        return 0
    (path,) = paths
    return sum(
        json.loads(line)["event"] == event for line in path.read_text(encoding="utf-8").splitlines()
    )


def _raw_cached(media_dir: Path) -> int:
    """Cached AudD bodies; listed through ``native_path`` — these paths exceed Windows MAX_PATH."""

    directory = native_path(media_dir / "recognise" / "invocations" / "live-audd-clip-v1" / "raw")
    if not os.path.isdir(directory):
        return 0
    return sum(name.endswith(".json") for name in os.listdir(directory))


def _run(
    tmp_path: Path,
    script_name: str,
    *,
    audd: object | None = None,
    cancel_token: threading.Event | None = None,
    progress: Callable[[str, int, int, str], None] | None = None,
) -> int:
    script = SCRIPTS / script_name
    return asyncio.run(
        cli._analyse(
            str(AUDIO),
            work_root=tmp_path / "work",
            print_raw=False,
            refresh=False,
            max_requests=100,
            tracklist=None,
            no_hints=True,
            app_config=CONFIG,
            max_generations=0,
            novelty=False,
            recipe=DEEP_RECIPE,
            paid_scan_adapters={"audd": audd if audd is not None else FakeAudD(script)},
            shazam_http_client=FakeShazamHTTP(script),
            paid_sleep=no_backoff,
            progress=progress,
            cancel_token=cancel_token,
        )
    )


class _AfterDispatch:
    """Wraps FakeAudD; calls ``hook(n)`` the instant the n-th attempt is journalled dispatched —
    after ``on_attempt`` returned and before the fake produces its outcome (the network step)."""

    def __init__(self, inner: FakeAudD, hook: Callable[[int], None]) -> None:
        self.inner = inner
        self.hook = hook
        self.dispatched = 0

    async def recognize_clip(self, path: Path, on_attempt):
        async def hooked() -> None:
            await on_attempt()
            self.dispatched += 1
            self.hook(self.dispatched)

        return await self.inner.recognize_clip(path, hooked)


def _golden_window() -> WindowRecord:
    return WindowRecord.model_validate(
        json.loads((GOLDEN / "window.json").read_text(encoding="utf-8"))
    )


def _write_clip(media_dir: Path, window: WindowRecord) -> None:
    path = media_dir / window.wav_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x00\x00" * 16_000)


def _sweep(media_dir: Path, window: WindowRecord, adapter: object, run_id: str):
    windows = WindowsResult(records=(window,), record_path=media_dir / "w.jsonl", cached=True)
    lo = window.support_ms[0]
    return asyncio.run(
        run_paid_clip_recognition(
            media_key="a" * 64,
            media_dir=media_dir,
            windows=windows,
            targets=((lo, lo + 1000),),
            run_id=run_id,
            app_config=AppConfig(audd_requests_per_minute=1_000_000),
            enabled_engines=("audd",),
            cli_confirmation=False,
            adapters={"audd": adapter},
        )
    )


def _wait(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------------------------------
# prepared -> dispatched (before network I/O) -> resolved
# --------------------------------------------------------------------------------------------------
def test_every_attempt_is_prepared_dispatched_before_network_io_then_resolved(
    tmp_path: Path,
) -> None:
    seen_at_dispatch: list[int] = []

    def check(n: int) -> None:
        # The request is about to enter network I/O: its `dispatched` line is already on disk.
        assert _count(tmp_path / "work", "dispatched") == n
        assert _count(tmp_path / "work", "resolved") == n - 1
        seen_at_dispatch.append(n)

    audd = _AfterDispatch(FakeAudD(SCRIPTS / "gate0a-deep.json"), check)
    assert _run(tmp_path, "gate0a-deep.json", audd=audd) == 0
    media_dir, entry = _entry(tmp_path / "work")
    assert seen_at_dispatch == list(range(1, 8))
    events = _events(media_dir)
    assert len(events) == 21
    by_attempt: dict[str, list[dict[str, object]]] = defaultdict(list)
    for event in events:
        ProviderAttemptEvent.model_validate(event)  # every line is a valid contract record
        by_attempt[str(event["attempt_id"])].append(event)
    assert len(by_attempt) == 7
    identity_fields = (
        "query_id",
        "window_id",
        "run_id",
        "provider",
        "ordinal",
        "parent_attempt_id",
        "unit_usd_e6",
    )
    for attempt_id, chain in by_attempt.items():
        assert [event["event"] for event in chain] == ["prepared", "dispatched", "resolved"]
        assert [event["seq"] for event in chain] == [0, 1, 2]
        assert chain[0]["at"] <= chain[1]["at"] <= chain[2]["at"]
        identity = {field: chain[0][field] for field in identity_fields}
        assert all(
            {field: event[field] for field in identity_fields} == identity for event in chain
        )
        assert identity["run_id"] == entry["invocation_id"] and identity["provider"] == "audd"
        assert identity["ordinal"] == 0 and identity["parent_attempt_id"] is None
        assert identity["unit_usd_e6"] == 5_000
        assert attempt_id == attempt_id_for(str(identity["run_id"]), str(identity["query_id"]), 0)
        assert [event["outcome"] for event in chain[:2]] == [None, None]
        assert chain[2]["outcome"] in {"match", "no_match"}
    assert sorted(chain[2]["outcome"] for chain in by_attempt.values()) == ["match"] * 6 + [
        "no_match"
    ]


def test_timeout_post_is_ambiguous_spent_and_never_retried(tmp_path: Path) -> None:
    audd = FakeAudD(SCRIPTS / "timeout-post-once.json")
    assert _run(tmp_path, "timeout-post-once.json", audd=audd) == 0
    media_dir, entry = _entry(tmp_path / "work")
    assert (entry["status"], entry["reason"]) == ("partial", "primary_not_achieved")
    # One attempt per window — the lost response is not retried — and it is billed.
    assert audd.calls == 7 and audd.billed_units == 7
    counts = entry["counts"]
    assert counts["paid_attempts"] == 7 and counts["paid_resolved"] == 6  # type: ignore[index]
    assert entry["usd_e6_spent"] == 35_000
    ledger = load_attempt_ledger(attempts_path(media_dir))
    ambiguous = [attempt for attempt in ledger.attempts if attempt.outcome == "timeout_post"]
    assert len(ambiguous) == 1 and ambiguous[0].ordinal == 0
    assert all(attempt.ordinal == 0 for attempt in ledger.attempts)
    assert ledger.with_classification("resolved") == ledger.attempts  # nothing left dangling
    assert _raw_cached(media_dir) == 6  # the lost response is never cached


# --------------------------------------------------------------------------------------------------
# Cancel token
# --------------------------------------------------------------------------------------------------
def test_cancel_after_three_dispatched_clips_leaves_at_most_four_dispatched(tmp_path: Path) -> None:
    token = threading.Event()
    inner = FakeAudD(SCRIPTS / "latency-match.json")  # 200 ms per clip, four in flight
    audd = _AfterDispatch(inner, lambda n: token.set() if n == 3 else None)
    with pytest.raises(asyncio.CancelledError):
        _run(tmp_path, "latency-match.json", audd=audd, cancel_token=token)
    media_dir, entry = _entry(tmp_path / "work")
    assert (entry["status"], entry["exit_code"], entry["achieved"]) == ("cancelled", 130, None)
    events = _events(media_dir)
    dispatched = [event for event in events if event["event"] == "dispatched"]
    resolved = [event for event in events if event["event"] == "resolved"]
    assert 3 <= len(dispatched) <= 4
    # Nothing was dispatched after the token fired, and the clips in flight resolved.
    assert len(resolved) == len(dispatched) == inner.calls
    assert {event["outcome"] for event in resolved} == {"match"}
    assert entry["counts"]["paid_attempts"] == len(dispatched)  # type: ignore[index]
    assert entry["usd_e6_spent"] == 5_000 * len(dispatched)
    assert entry["usd_e6_reserved"] == 36_750
    assert _raw_cached(media_dir) == len(dispatched)  # their answers are kept for the next run
    assert not (media_dir / "present").exists()


def test_a_cancel_token_already_set_dispatches_nothing_and_ends_cancelled(tmp_path: Path) -> None:
    token = threading.Event()
    token.set()
    audd = FakeAudD(SCRIPTS / "gate0a-deep.json")
    with pytest.raises(asyncio.CancelledError):
        _run(tmp_path, "gate0a-deep.json", audd=audd, cancel_token=token)
    media_dir, entry = _entry(tmp_path / "work")
    assert (entry["status"], entry["exit_code"]) == ("cancelled", 130)
    assert audd.calls == 0 and entry["counts"]["paid_attempts"] == 0  # type: ignore[index]
    assert entry["usd_e6_reserved"] == 36_750 and entry["usd_e6_spent"] == 0
    assert not attempts_path(media_dir).exists()


def test_a_raising_progress_hook_stops_dispatch_and_lets_in_flight_clips_resolve(
    tmp_path: Path,
) -> None:
    inner = FakeAudD(SCRIPTS / "latency-match.json", latency_s=0.05)
    at_raise: dict[str, int] = {}

    def progress(phase: str, done: int, total: int, message: str) -> None:
        # The web app's hook raises from a tick once the user clicked cancel.
        if phase == "recognise" and done == 3 and "dispatched" not in at_raise:
            at_raise["dispatched"] = _count(tmp_path / "work", "dispatched")
            raise asyncio.CancelledError("browser cancel")

    with pytest.raises(asyncio.CancelledError):
        _run(tmp_path, "latency-match.json", audd=inner, progress=progress)
    media_dir, entry = _entry(tmp_path / "work")
    assert (entry["status"], entry["exit_code"]) == ("cancelled", 130)
    events = _events(media_dir)
    dispatched = sum(event["event"] == "dispatched" for event in events)
    resolved = sum(event["event"] == "resolved" for event in events)
    assert at_raise["dispatched"] >= 4  # several clips were in flight when the hook raised
    assert dispatched == at_raise["dispatched"]  # nothing dispatched after it
    assert resolved == dispatched == inner.calls < 7  # in flight resolved; the rest never sent
    assert entry["usd_e6_spent"] == 5_000 * dispatched


# --------------------------------------------------------------------------------------------------
# Resume (plan §2.3.3): dispatched-unresolved -> ambiguous; prepared-only -> re-issued
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("dispatched", "classification", "field"),
    [
        (True, "ambiguous", "resumed_ambiguous"),
        (False, "reissue", "resumed_reissued"),
    ],
)
def test_resume_classifies_an_earlier_runs_unresolved_attempt(
    tmp_path: Path, dispatched: bool, classification: str, field: str
) -> None:
    window = _golden_window()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    _write_clip(media_dir, window)
    query_id = clip_cache_key(window.wav_sha256, "audd", CLIP_CONFIG_VERSION)
    old = AttemptJournal(
        attempts_path(media_dir), run_id="0" * 32, provider="audd", unit_usd_e6=5_000
    )
    old_id = old.prepare(query_id=query_id, window_id=window.id, ordinal=0, parent_attempt_id=None)
    if dispatched:
        old.dispatched(old_id)
    before = load_attempt_ledger(attempts_path(media_dir))
    stale = before.dangling(query_id)
    assert stale is not None and stale.classification == classification
    assert stale.state == ("dispatched" if dispatched else "prepared")

    adapter = FakeAudD({"audd": {"default": "match"}})
    result = _sweep(media_dir, window, adapter, "1" * 32)
    assert getattr(result, field) == 1
    assert result.resumed_ambiguous + result.resumed_reissued == 1
    assert adapter.calls == 1 and result.resolved == 1 and result.attempts == 1
    after = load_attempt_ledger(attempts_path(media_dir))
    assert after.dangling(query_id) is None
    fresh = after.latest(query_id)
    assert fresh is not None and fresh.attempt_id != old_id
    assert fresh.parent_attempt_id == old_id and fresh.ordinal == 0
    assert fresh.outcome == "match" and fresh.run_id == "1" * 32
    # The earlier attempt keeps its classification as history; the new one resolves the query.
    assert after.with_classification(classification) == (stale,)


def test_a_resolved_earlier_attempt_is_not_resumed(tmp_path: Path) -> None:
    window = _golden_window()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    _write_clip(media_dir, window)
    query_id = clip_cache_key(window.wav_sha256, "audd", CLIP_CONFIG_VERSION)
    old = AttemptJournal(
        attempts_path(media_dir), run_id="0" * 32, provider="audd", unit_usd_e6=5_000
    )
    old_id = old.prepare(query_id=query_id, window_id=window.id, ordinal=0, parent_attempt_id=None)
    old.dispatched(old_id)
    old.resolved(old_id, "http_429")  # zero-cost, simply re-run next time (never cached)
    result = _sweep(media_dir, window, FakeAudD({"audd": {"default": "match"}}), "1" * 32)
    assert result.resumed_ambiguous == result.resumed_reissued == 0 and result.resolved == 1
    fresh = load_attempt_ledger(attempts_path(media_dir)).latest(query_id)
    assert fresh is not None and fresh.parent_attempt_id is None and fresh.outcome == "match"


def test_resume_through_the_pipeline_after_a_crash_mid_request(tmp_path: Path) -> None:
    def crash(n: int) -> None:
        if n == 2:
            raise RuntimeError("simulated process crash after dispatch")

    first = _AfterDispatch(FakeAudD(SCRIPTS / "gate0a-deep.json"), crash)
    with pytest.raises(RuntimeError):
        _run(tmp_path, "gate0a-deep.json", audd=first)
    media_dir, entry = _entry(tmp_path / "work")
    assert (entry["status"], entry["exit_code"]) == ("failed", 1)
    ledger = load_attempt_ledger(attempts_path(media_dir))
    (torn,) = ledger.with_classification("ambiguous")
    assert torn.dispatched and torn.outcome is None
    assert ledger.with_classification("reissue") == ()
    # The crashed unit settled as spent (§2.3.3): at least the match before it and itself.
    assert entry["usd_e6_spent"] >= 10_000

    second = FakeAudD(SCRIPTS / "gate0a-deep.json")
    assert _run(tmp_path, "gate0a-deep.json", audd=second) == 0
    media_dir_again, entry = _entry(tmp_path / "work")
    assert media_dir_again == media_dir and entry["status"] == "complete"
    counts = entry["counts"]
    assert counts["paid_resumed_ambiguous"] == 1  # type: ignore[index]
    assert counts["paid_resumed_reissued"] == 0  # type: ignore[index]
    assert counts["paid_requests"] + counts["paid_cache_hits"] == 7  # type: ignore[index]
    assert counts["paid_requests"] >= 1  # type: ignore[index]
    after = load_attempt_ledger(attempts_path(media_dir))
    (resumed,) = [a for a in after.attempts if a.parent_attempt_id == torn.attempt_id]
    assert resumed.outcome == "match" and resumed.run_id == entry["invocation_id"]
    assert after.dangling(torn.query_id) is None


def test_a_prepared_but_refused_admission_is_re_issued_not_ambiguous(tmp_path: Path) -> None:
    from id_detector.money import UsdAdmitter, reserve_usd

    window = _golden_window()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    _write_clip(media_dir, window)
    windows = WindowsResult(records=(window,), record_path=media_dir / "w.jsonl", cached=True)
    lo = window.support_ms[0]
    admitter = UsdAdmitter(
        reserve_usd(planned=1, unit_usd_e6=5_000, recipe_max_usd_e2=900, configured_max_usd_e2=None)
    )
    admitter.admit()  # the whole reservation is already out: the dispatch must be refused
    adapter = FakeAudD({"audd": {"default": "match"}})
    result = asyncio.run(
        run_paid_clip_recognition(
            media_key="a" * 64,
            media_dir=media_dir,
            windows=windows,
            targets=((lo, lo + 1000),),
            run_id="2" * 32,
            app_config=AppConfig(audd_requests_per_minute=1_000_000),
            enabled_engines=("audd",),
            cli_confirmation=False,
            usd_admitter=admitter,
            adapters={"audd": adapter},
        )
    )
    assert result.reservation_exhausted and adapter.calls == 0 and result.attempts == 0
    ledger = load_attempt_ledger(attempts_path(media_dir))
    (attempt,) = ledger.attempts
    assert attempt.state == "prepared" and attempt.classification == "reissue"


def test_an_admitted_dispatch_without_an_outcome_is_settled_as_ambiguous(tmp_path: Path) -> None:
    """An admitted unit is always resolved: never left outstanding, never re-billed on resume."""

    from id_detector.money import UsdAdmitter, reserve_usd

    class _AdmitsThenFails:
        """An adapter that admits the unit (writing `dispatched`) and then fails outright."""

        def __init__(self) -> None:
            self.calls = 0

        async def recognize_clip(self, path: Path, on_attempt):
            self.calls += 1
            await on_attempt()
            raise FileNotFoundError(path)

    window = _golden_window()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    _write_clip(media_dir, window)
    windows = WindowsResult(records=(window,), record_path=media_dir / "w.jsonl", cached=True)
    lo = window.support_ms[0]
    admitter = UsdAdmitter(
        reserve_usd(planned=2, unit_usd_e6=5_000, recipe_max_usd_e2=900, configured_max_usd_e2=None)
    )
    adapter = _AdmitsThenFails()
    result = asyncio.run(
        run_paid_clip_recognition(
            media_key="a" * 64,
            media_dir=media_dir,
            windows=windows,
            targets=((lo, lo + 1000),),
            run_id="3" * 32,
            app_config=AppConfig(audd_requests_per_minute=1_000_000),
            enabled_engines=("audd",),
            cli_confirmation=False,
            usd_admitter=admitter,
            adapters={"audd": adapter},
        )
    )
    assert adapter.calls == 1 and result.attempts == 1 and result.requests == 1
    assert result.outcomes == ("timeout_post",) and result.billable_units == 1
    # Resolved, not merely outstanding: the unit is spent before settlement, and the rest of the
    # reservation is still admissible.
    assert admitter.usd_e6_spent == 5_000
    assert admitter.usd_e6_remaining == 5_500
    ledger = load_attempt_ledger(attempts_path(media_dir))
    (attempt,) = ledger.attempts
    assert attempt.outcome == "timeout_post" and attempt.classification == "resolved"
    query_id = clip_cache_key(window.wav_sha256, "audd", CLIP_CONFIG_VERSION)
    assert ledger.dangling(query_id) is None  # a later run never re-bills it as ambiguous


def test_an_adapter_that_admits_twice_is_refused(tmp_path: Path) -> None:
    """Two `on_attempt` calls would be two `dispatched` lines and two units for one request."""

    class _DoubleAdmit:
        async def recognize_clip(self, path: Path, on_attempt):
            await on_attempt()
            await on_attempt()
            return {"status": "success", "result": None}

    window = _golden_window()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    _write_clip(media_dir, window)
    with pytest.raises(RuntimeError, match="twice"):
        _sweep(media_dir, window, _DoubleAdmit(), "4" * 32)
    events = [event for event in _events(media_dir) if event["event"] == "dispatched"]
    assert len(events) == 1


# --------------------------------------------------------------------------------------------------
# Progress per clip, the web app's token, the ledger reader and the contract
# --------------------------------------------------------------------------------------------------
def test_progress_is_reported_per_clip_with_the_real_total(tmp_path: Path) -> None:
    ticks: list[tuple[int, int, str]] = []

    def progress(phase: str, done: int, total: int, message: str) -> None:
        if phase == "recognise":
            ticks.append((done, total, message))

    assert _run(tmp_path, "gate0a-deep.json", progress=progress) == 0
    # The paid primary's ticks end at its summary line; the Shazam secondary ticks its own
    # (done, 2) afterwards through the same hook.
    summary = next(
        i for i, (_d, _t, message) in enumerate(ticks) if message.startswith("audd clips:")
    )
    primary = ticks[: summary + 1]
    windows = [
        (done, total) for done, total, message in primary if message == "recognising windows"
    ]
    assert windows[0] == (0, 7) and windows[-1] == (7, 7)
    assert [done for done, _ in windows] == list(range(8))
    # Log lines inside the phase repeat the current done/total instead of resetting the web
    # app's ETA to "1 window" (review H4); only the opening line precedes the first tick.
    assert primary[summary][:2] == (7, 7)
    assert all(total == 7 for _done, total, message in primary if "identifying" not in message)


def test_web_runner_passes_the_jobs_cancel_event_as_the_cancel_token(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    async def capture_analyse(_target: str, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(pipeline, "run_analysis", capture_analyse)
    monkeypatch.setattr(cli, "_load_cached", lambda _root, _target: None)
    monkeypatch.setattr(pipeline, "_load_cached", lambda _root, _target: None)
    manager = JobManager(
        tmp_path,
        make_pipeline_runner(tmp_path, project_root=ROOT, config_path=tmp_path / "missing.toml"),
    )
    try:
        job_id = manager.submit(str(AUDIO), "max_accuracy")
        assert _wait(lambda: manager.get(job_id).status in {"succeeded", "failed"})
        job = manager.get(job_id)
        assert job.status == "succeeded"
        assert captured["cancel_token"] is job.cancel_event
        assert JobContext(manager, job).cancel_token is job.cancel_event
        assert not job.cancel_event.is_set()
    finally:
        manager.shutdown()


def test_ledger_skips_a_torn_trailing_line_and_folds_events(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    journal = AttemptJournal(path, run_id="r" * 32, provider="audd", unit_usd_e6=5_000)
    first = journal.prepare(
        query_id="a" * 64, window_id="1" * 40, ordinal=0, parent_attempt_id=None
    )
    journal.dispatched(first)
    journal.resolved(first, "http_429")
    second = journal.prepare(
        query_id="a" * 64, window_id="1" * 40, ordinal=1, parent_attempt_id=first
    )
    journal.dispatched(second)
    with open(path, "ab") as handle:  # a crash mid-write leaves a torn last line
        handle.write(b'{"event": "resolved", "seq": 2, "attempt_id": "')
    ledger = load_attempt_ledger(path)
    assert ledger.skipped_lines == 1
    assert [attempt.state for attempt in ledger.attempts] == ["resolved", "dispatched"]
    assert ledger.attempts[0].outcome == "http_429" and ledger.attempts[0].dispatched
    assert ledger.attempts[1].parent_attempt_id == first
    latest = ledger.latest("a" * 64)
    assert latest is not None and latest.attempt_id == second
    dangling = ledger.dangling("a" * 64)
    assert dangling is not None and dangling.classification == "ambiguous"
    assert ledger.latest("b" * 64) is None
    for line in path.read_text(encoding="utf-8").splitlines()[:-1]:
        ProviderAttemptEvent.model_validate(json.loads(line))
    assert load_attempt_ledger(tmp_path / "missing.jsonl") == AttemptLedger()


def test_attempt_event_contract_ties_outcome_and_seq_to_the_event_kind() -> None:
    base = json.loads((GOLDEN / "provider_attempt_event.json").read_text(encoding="utf-8"))
    ProviderAttemptEvent.model_validate(base)
    for bad in (
        {**base, "outcome": None},  # resolved without an outcome
        {**base, "event": "dispatched", "seq": 1},  # an outcome on a non-resolved event
        {**base, "seq": 0},  # seq disagrees with the kind
        {**base, "outcome": "lost"},  # outside the frozen vocabulary
        {**base, "unit_usd_e6": 5000.0},  # floats are forbidden in artefacts
    ):
        with pytest.raises(ValueError):
            ProviderAttemptEvent.model_validate(bad)

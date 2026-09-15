"""Follow-up cycle: money and resume safety at the service seam (retro-review 4a-i).

Every test here was written to FAIL at ``ec32f97`` before any production change, reproducing one
retro-review finding by behaviour rather than by inspection.  ``_HardKill`` is a ``BaseException``
so neither the pipeline's ``except Exception`` nor its cancellation handler runs: it stands in for a
process that dies (power cut, OOM kill) with a ``dispatched`` line on disk and nothing after it.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from id_detector.io import native_path
from id_detector.money import UsdAdmitter, reserve_usd
from id_detector.providers.base import AppConfig
from id_detector.recipes import DEEP_RECIPE, FREE_RECIPE
from id_detector.service import (
    CHECKPOINT_PHASES,
    LocalCheckpointStore,
    PipelineOptions,
    TargetRefused,
    exit_code_for,
    run,
    validate_platform_url,
)
from id_detector.shazam_breaker import BreakerConfig, ShazamBreaker
from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff
from tests.test_service_api import (
    AUDIO,
    DEEP_CLIPS,
    ROOT,
    SCRIPT,
    UNIT_USD_E6,
    _entries,
    _request,
)

#: 105 % of seven clips at the fixture price: the reservation the first pass durably made.
ORIGINAL_RESERVED_E6 = (DEEP_CLIPS * UNIT_USD_E6 * 105 + 99) // 100


class _HardKill(BaseException):
    """The process died here: no ``except Exception`` handler, no settlement, no journal line."""


class _KillAfterDispatch:
    """An AudD adapter whose ``kill_on``-th request is dispatched and then the process dies."""

    def __init__(self, inner: FakeAudD, kill_on: int) -> None:
        self.inner = inner
        self.kill_on = kill_on
        self.calls = 0

    async def recognize_clip(self, path: Path, on_attempt):
        self.calls += 1
        if self.calls == self.kill_on:
            await on_attempt()  # `dispatched` is durable: the provider may have billed it
            raise _HardKill("killed after dispatch, before resolution")
        return await self.inner.recognize_clip(path, on_attempt)


class _KillBeforeDispatch:
    """An AudD adapter whose ``kill_on``-th request dies before it is ever admitted."""

    def __init__(self, inner: FakeAudD, kill_on: int) -> None:
        self.inner = inner
        self.kill_on = kill_on
        self.calls = 0

    async def recognize_clip(self, path: Path, on_attempt):
        self.calls += 1
        if self.calls == self.kill_on:
            raise _HardKill("killed before dispatch")
        return await self.inner.recognize_clip(path, on_attempt)


class _KillShazamOn(FakeShazamHTTP):
    """A Shazam HTTP boundary that kills the process on its ``kill_on``-th request."""

    def __init__(self, script, kill_on: int) -> None:
        super().__init__(script)
        self.kill_on = kill_on
        self.seen = 0

    async def request(self, method: str, url: str, *args: object, **kwargs):
        self.seen += 1
        if self.seen == self.kill_on:
            raise _HardKill("killed mid secondary")
        return await super().request(method, url, *args, **kwargs)


def _options(
    *,
    audd=None,
    shazam=None,
    recipe=DEEP_RECIPE,
    refresh_states: frozenset[str] = frozenset({"no_match"}),
    app_config: AppConfig | None = None,
    allow_degrade: bool = False,
    paid_sleep=no_backoff,
    shazam_breaker: ShazamBreaker | None = None,
) -> PipelineOptions:
    return PipelineOptions(
        project_root=ROOT,
        refresh_states=refresh_states,
        max_requests=100,
        no_hints=True,
        app_config=app_config or AppConfig(),
        max_generations=0,
        enabled_engines=("audd",) if recipe.name == "deep" else (),
        paid_scan_adapters={"audd": audd} if audd is not None else None,
        shazam_http_client=shazam,
        shazam_breaker=shazam_breaker,
        allow_degrade=allow_degrade,
        paid_sleep=paid_sleep,
    )


def _local(work: Path, **kwargs) -> LocalCheckpointStore:
    return LocalCheckpointStore(work, options=_options(**kwargs))


def _run_events(work: Path, run_id: str) -> list[dict]:
    events: list[dict] = []
    for root, _dirs, files in os.walk(native_path(work)):
        if "attempts.jsonl" in files:
            with open(os.path.join(root, "attempts.jsonl"), encoding="utf-8") as handle:
                events.extend(json.loads(line) for line in handle if line.strip())
    return [event for event in events if event["run_id"] == run_id]


def _dispatched_ids(events: list[dict]) -> set[str]:
    return {event["attempt_id"] for event in events if event["event"] == "dispatched"}


def _unresolved(events: list[dict]) -> set[str]:
    resolved = {event["attempt_id"] for event in events if event["event"] == "resolved"}
    return _dispatched_ids(events) - resolved


# ------------------------------------------------------------------------ 4a-i B / P0-1 (+4b-i P0)


def test_a_same_run_ambiguous_attempt_is_spent_and_never_sent_again(tmp_path: Path) -> None:
    """Dispatched-but-unresolved is ambiguous: counted as spent and never automatically re-sent.

    No later attempt of the same run reuses its ordinal or attempt id, or names itself as parent.
    """

    work = tmp_path / "work"
    run_id = "ambiguous-same-run"
    killer = _KillAfterDispatch(FakeAudD(SCRIPT), kill_on=3)
    with pytest.raises(_HardKill):
        run(_request(_local(work, audd=killer), run_id=run_id))
    first = _run_events(work, run_id)
    dispatched_first = _dispatched_ids(first)
    assert _unresolved(first), "the kill must leave an ambiguous attempt behind"

    first_attempts = {event["attempt_id"] for event in first}
    first_queries = {
        event["query_id"] for event in first if event["attempt_id"] in dispatched_first
    }
    ambiguous_queries = {
        event["query_id"] for event in first if event["attempt_id"] in _unresolved(first)
    }

    resumed = FakeAudD(SCRIPT)
    second = run(_request(_local(work, audd=resumed), run_id=run_id))
    # The ambiguous clip has no answer, so `partial` (primary_not_achieved) is honest. What must
    # not happen is the old failure: re-sending the ambiguous clip exhausted the reservation and
    # left a never-dispatched clip unsent — a bare call count could not tell those apart.
    assert second.status in {"complete", "degraded", "partial"}, second
    assert second.reason != "reservation_exhausted", second
    assert resumed.calls == DEEP_CLIPS - len(dispatched_first)
    events = _run_events(work, run_id)
    resent = {
        event["query_id"]
        for event in events
        if event["event"] == "dispatched" and event["attempt_id"] not in first_attempts
    }
    # Exact identities: none of the first pass's clips — above all the ambiguous one — went again,
    # and every clip the first pass never dispatched was sent exactly once.
    assert not resent & ambiguous_queries
    assert not resent & first_queries
    assert resent | first_queries == {event["query_id"] for event in events}
    assert len(resent | first_queries) == DEEP_CLIPS
    prepared = [event["attempt_id"] for event in events if event["event"] == "prepared"]
    assert len(prepared) == len(set(prepared)), "an attempt id was reused inside one run"
    assert all(event["parent_attempt_id"] != event["attempt_id"] for event in events)
    assert second.usd_e6_spent == DEEP_CLIPS * UNIT_USD_E6


# ------------------------------------------------------------------------------ 4a-i B / P0-2


def _crash_mid_primary(work: Path, run_id: str) -> set[str]:
    killer = _KillBeforeDispatch(FakeAudD(SCRIPT), kill_on=5)
    with pytest.raises(_HardKill):
        run(_request(_local(work, audd=killer), run_id=run_id))
    return _dispatched_ids(_run_events(work, run_id))


def test_resume_restores_the_original_reservation_and_price_not_todays(tmp_path: Path) -> None:
    work = tmp_path / "work"
    run_id = "price-changed"
    dispatched = _crash_mid_primary(work, run_id)
    assert 0 < len(dispatched) < DEEP_CLIPS

    cheaper = AppConfig(audd_usd_e6_per_request=4_000)
    second = run(_request(_local(work, audd=FakeAudD(SCRIPT), app_config=cheaper), run_id=run_id))
    assert second.usd_e6_reserved == ORIGINAL_RESERVED_E6
    assert second.usd_e6_spent == DEEP_CLIPS * UNIT_USD_E6
    assert _entries(work, run_id)[-1]["usd_e6_reserved"] == ORIGINAL_RESERVED_E6


def test_a_lowered_cap_does_not_recompute_a_durable_reservation(tmp_path: Path) -> None:
    work = tmp_path / "work"
    run_id = "cap-lowered"
    _crash_mid_primary(work, run_id)
    second = run(
        _request(
            _local(work, audd=FakeAudD(SCRIPT), app_config=AppConfig(max_usd_e2=1)), run_id=run_id
        )
    )
    assert second.status != "budget_exhausted", second
    assert second.usd_e6_reserved == ORIGINAL_RESERVED_E6
    assert second.usd_e6_spent == DEEP_CLIPS * UNIT_USD_E6


def test_an_injected_admitter_on_resume_is_charged_with_the_recovered_spend(tmp_path: Path) -> None:
    work = tmp_path / "work"
    run_id = "injected-admitter"
    _crash_mid_primary(work, run_id)
    admitter = UsdAdmitter(
        reserve_usd(
            planned=DEEP_CLIPS,
            unit_usd_e6=UNIT_USD_E6,
            recipe_max_usd_e2=DEEP_RECIPE.max_usd_e2,
            configured_max_usd_e2=None,
        )
    )
    request = replace(
        _request(_local(work, audd=FakeAudD(SCRIPT)), run_id=run_id), usd_admitter=admitter
    )
    second = run(request)
    assert second.usd_e6_spent == DEEP_CLIPS * UNIT_USD_E6
    assert admitter.usd_e6_spent == DEEP_CLIPS * UNIT_USD_E6


# ------------------------------------------------------------------------------ 4a-i B / P0-3


class _DiesAtPhase(LocalCheckpointStore):
    def __init__(self, work_root: Path, *, options: PipelineOptions, phase: str, error) -> None:
        super().__init__(work_root, options=options)
        self.phase = phase
        self.error = error

    def write(self, run_id, phase, *, artefacts, state=None) -> None:
        if phase == self.phase:
            raise self.error
        super().write(run_id, phase, artefacts=artefacts, state=state)


def test_a_crash_after_bundle_publication_recovers_the_runs_money_when_served(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    run_id = "published-then-killed"
    store = _DiesAtPhase(
        work,
        options=_options(audd=FakeAudD(SCRIPT)),
        phase="present",
        error=_HardKill("killed after publish_result"),
    )
    with pytest.raises(_HardKill):
        run(_request(store, run_id=run_id))
    assert _entries(work, run_id) == []  # died before its settlement was journalled

    audd = FakeAudD(SCRIPT)
    served = run(_request(_local(work, audd=audd), run_id=run_id))
    assert audd.calls == 0
    assert served.bundle_id is not None
    assert served.usd_e6_spent == DEEP_CLIPS * UNIT_USD_E6
    assert served.usd_e6_reserved == ORIGINAL_RESERVED_E6
    entries = _entries(work, run_id)
    assert len(entries) == 1 and entries[0]["usd_e6_spent"] == DEEP_CLIPS * UNIT_USD_E6


# ------------------------------------------------------------------------------ 4a-i B / P1-4


def test_cancel_then_resume_journals_exactly_one_monotonic_settlement(tmp_path: Path) -> None:
    work = tmp_path / "work"
    run_id = "one-settlement"
    token = threading.Event()
    audd = FakeAudD(SCRIPT, latency_s=0.02)

    def progress(phase: str, done: int, _total: int, _message: str) -> None:
        if phase == "recognise" and done >= 1:
            token.set()

    cancelled = run(
        _request(_local(work, audd=audd), run_id=run_id, cancel_token=token, progress=progress)
    )
    assert cancelled.status == "cancelled"
    second = run(_request(_local(work, audd=FakeAudD(SCRIPT)), run_id=run_id))
    entries = _entries(work, run_id)
    assert len(entries) == 1, [entry["status"] for entry in entries]
    assert entries[0]["status"] == second.status
    assert entries[0]["usd_e6_spent"] == second.usd_e6_spent == DEEP_CLIPS * UNIT_USD_E6
    assert entries[0]["usd_e6_reserved"] >= cancelled.usd_e6_reserved


# ------------------------------------------------------------------------------ 4a-i B / P1-5


def test_a_real_failure_after_paid_work_returns_a_failed_run_result(tmp_path: Path) -> None:
    work = tmp_path / "work"
    store = _DiesAtPhase(
        work,
        options=_options(audd=FakeAudD(SCRIPT)),
        phase="fuse1",
        error=RuntimeError("checkpoint store is broken"),
    )
    result = run(_request(store, run_id="fails-after-paid"))
    assert result.status == "failed"
    assert exit_code_for(result) == 1
    assert result.usd_e6_spent == DEEP_CLIPS * UNIT_USD_E6
    assert result.usd_e6_reserved == ORIGINAL_RESERVED_E6


# ------------------------------------------------------------------------------ 4a-i B / P1-6


def test_every_c0_control_and_del_is_refused_in_a_platform_url() -> None:
    controls = [chr(code) for code in range(0x20)] + ["\x7f"]
    for character in controls:
        for template in ("https://example.com/a{}b", "https://exa{}mple.com/set"):
            with pytest.raises(TargetRefused):
                validate_platform_url(template.format(character))
    # The parser differential that makes this matter: urlsplit silently drops tab and newline,
    # so a URL with an embedded control would name a different host than the bytes submitted.
    assert urlsplit("https://exa\tmple.com/set").hostname == "example.com"


# ------------------------------------------------------------------------------ 4a-i A / P1-1


def test_a_retryable_zero_cost_outcome_is_retried_after_a_crash(tmp_path: Path) -> None:
    work = tmp_path / "work"
    run_id = "retry-after-crash"
    script = {"audd": {"default": "match", "windows": {"1": ["http_429", "match"]}}}

    async def die_in_backoff(_seconds: float) -> None:
        raise _HardKill("killed during retry backoff")

    with pytest.raises(_HardKill):
        run(_request(_local(work, audd=FakeAudD(script), paid_sleep=die_in_backoff), run_id=run_id))

    resumed = FakeAudD(script)
    second = run(_request(_local(work, audd=resumed), run_id=run_id))
    assert any(attempt["window"] == 1 for attempt in resumed.attempts), resumed.attempts
    assert second.status != "partial", (second.status, second.reason)


# ------------------------------------------------------------------------------ 4a-i A / P1-2


def test_allow_degrade_is_refused_once_the_run_has_cumulative_spend(tmp_path: Path) -> None:
    work = tmp_path / "work"
    run_id = "degrade-after-spend"
    billed = {"audd": {"default": "http_500"}}
    killer = _KillBeforeDispatch(FakeAudD(billed), kill_on=2)
    with pytest.raises(_HardKill):
        run(
            _request(
                _local(work, audd=killer, shazam=FakeShazamHTTP(SCRIPT), allow_degrade=True),
                run_id=run_id,
            )
        )
    already = len(_dispatched_ids(_run_events(work, run_id)))
    assert already >= 1

    unavailable = ROOT / "tests/fakes/scripts/all-http-401.json"
    second = run(
        _request(
            _local(
                work,
                audd=FakeAudD(unavailable),
                shazam=FakeShazamHTTP(SCRIPT),
                allow_degrade=True,
            ),
            run_id=run_id,
        )
    )
    assert second.status == "provider_unavailable", (second.status, second.achieved)
    assert second.achieved != "free"
    assert second.usd_e6_spent == already * UNIT_USD_E6


# ------------------------------------------------------------------------------ 4a-i A / P1-3


_NO_MATCH_SHAZAM = {
    "audd": json.loads(SCRIPT.read_text(encoding="utf-8"))["audd"],
    "shazam": {"default": "no_match"},
}


def _secondary_killed(work: Path, run_id: str) -> set[int]:
    killer = _KillShazamOn(_NO_MATCH_SHAZAM, kill_on=2)
    store = _local(work, audd=FakeAudD(_NO_MATCH_SHAZAM), shazam=killer)
    with pytest.raises(_HardKill):
        run(_request(store, run_id=run_id))
    assert "secondary" not in store.completed_phases(run_id)
    return {attempt["window"] for attempt in killer.attempts}


def test_a_partial_secondary_is_not_re_queried_on_a_same_run_resume(tmp_path: Path) -> None:
    work = tmp_path / "work"
    run_id = "partial-secondary-closed"
    answered = _secondary_killed(work, run_id)
    assert answered, "the first probe answered at least one window before the kill"

    resumed = FakeShazamHTTP(_NO_MATCH_SHAZAM)
    run(_request(_local(work, audd=FakeAudD(_NO_MATCH_SHAZAM), shazam=resumed), run_id=run_id))
    resent = answered & {attempt["window"] for attempt in resumed.attempts}
    assert not resent, f"same-run no_match answers were sent again: {sorted(resent)}"


def test_a_partial_secondary_is_restored_even_when_the_breaker_is_open(tmp_path: Path) -> None:
    work = tmp_path / "work"
    run_id = "partial-secondary-open"
    answered = _secondary_killed(work, run_id)
    assert answered

    config = BreakerConfig(shazam_daily_budget_per_egress=1)
    breaker = ShazamBreaker(config)
    breaker.dispatch(running_free=False)
    assert breaker.reason() is not None
    resumed = FakeShazamHTTP(_NO_MATCH_SHAZAM)
    store = _local(
        work,
        audd=FakeAudD(_NO_MATCH_SHAZAM),
        shazam=resumed,
        app_config=AppConfig(shazam_breaker=config),
        shazam_breaker=breaker,
    )
    run(_request(store, run_id=run_id))
    assert resumed.requests == 0
    assert int(store.state(run_id, "secondary").get("resolved", 0)) >= len(answered)


# ------------------------------------------------------------------------------ 4a-i A / P1-4


def test_a_local_checkpoint_whose_artefact_is_gone_or_changed_is_not_complete(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    run_id = "revalidated"
    store = _local(work, recipe=FREE_RECIPE, shazam=FakeShazamHTTP(SCRIPT))
    result = run(_request(store, run_id=run_id, recipe=FREE_RECIPE))
    assert result.status == "complete"
    assert store.completed_phases(run_id) == frozenset(CHECKPOINT_PHASES)

    document = json.loads(store._path(run_id).read_text(encoding="utf-8"))
    decode_artefacts = [Path(item) for item in document["phases"]["decode"]["artefacts"]]
    pcm = next(path for path in decode_artefacts if path.suffix == ".pcm")
    os.unlink(native_path(pcm))
    windows_path = next(
        Path(item) for item in document["phases"]["windows"]["artefacts"] if item.endswith(".jsonl")
    )
    with open(native_path(windows_path), "ab") as handle:
        handle.write(b"\n")
    completed = store.completed_phases(run_id)
    assert "decode" not in completed
    assert "windows" not in completed
    assert "ingest" in completed


def test_decoder_output_is_published_with_the_write_through_move() -> None:
    """4b-i P1: plain ``os.replace`` has no write-through guarantee on Windows."""

    source = (ROOT / "src/id_detector/decode.py").read_text(encoding="utf-8")
    assert "os.replace(" not in source


def test_the_fixture_audio_exists() -> None:
    assert AUDIO.is_file()


# =============================================== second-model review (sol xhigh) regressions


def test_an_unhashed_legacy_checkpoint_is_rebuilt_not_trusted(tmp_path: Path) -> None:
    """Review P1-5: a pre-fix checkpoint (no recorded hashes) proves existence only."""

    work = tmp_path / "work"
    store = LocalCheckpointStore(work)
    artefact = tmp_path / "audio.pcm"
    artefact.write_bytes(b"\x00\x01" * 32)
    path = store._path("legacy-run")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": "legacy-run",
                "phases": {"decode": {"artefacts": [str(artefact)], "state": {}}},
            }
        ),
        encoding="utf-8",
    )
    assert "decode" not in store.completed_phases("legacy-run")


def test_an_injected_admitter_must_hold_the_durable_reservation(tmp_path: Path) -> None:
    """Review P1-6: same unit price, smaller reservation — refused, never silently used."""

    work = tmp_path / "work"
    run_id = "injected-mismatch"
    _crash_mid_primary(work, run_id)
    smaller = UsdAdmitter(
        reserve_usd(
            planned=3,
            unit_usd_e6=UNIT_USD_E6,
            recipe_max_usd_e2=DEEP_RECIPE.max_usd_e2,
            configured_max_usd_e2=None,
        )
    )
    audd = FakeAudD(SCRIPT)
    result = run(replace(_request(_local(work, audd=audd), run_id=run_id), usd_admitter=smaller))
    assert result.status == "failed", result
    assert "reservation" in (result.reason or "")
    assert audd.calls == 0


def test_a_zero_money_compatible_resume_replaces_the_runs_earlier_settlement(
    tmp_path: Path,
) -> None:
    """Review P1-8: a run cancelled before reserving, served later, settles as what it became."""

    import asyncio

    work = tmp_path / "work"
    run_id = "cancelled-then-served"

    def cancel_at_decode(phase: str, done: int, _total: int, _message: str) -> None:
        if phase == "decode" and done == 0:
            raise asyncio.CancelledError("cancelled before any money")

    cancelled = run(
        _request(
            _local(work, recipe=FREE_RECIPE, shazam=FakeShazamHTTP(SCRIPT)),
            run_id=run_id,
            recipe=FREE_RECIPE,
            progress=cancel_at_decode,
        )
    )
    assert cancelled.status == "cancelled"
    assert [entry["status"] for entry in _entries(work, run_id)] == ["cancelled"]
    other = run(
        _request(
            _local(work, recipe=FREE_RECIPE, shazam=FakeShazamHTTP(SCRIPT)),
            run_id="publishes-the-bundle",
            recipe=FREE_RECIPE,
        )
    )
    assert other.status == "complete"
    served = run(
        _request(
            _local(work, recipe=FREE_RECIPE, shazam=FakeShazamHTTP(SCRIPT)),
            run_id=run_id,
            recipe=FREE_RECIPE,
        )
    )
    assert served.status == "complete" and served.bundle_id == other.bundle_id
    assert [entry["status"] for entry in _entries(work, run_id)] == ["complete"]


def test_a_conflicting_duplicate_ordinal_is_refused_by_the_shared_fold() -> None:
    """Review P1-7: every immutable field of a duplicate event is verified, ordinal included."""

    from id_detector.run_ledger import AttemptEvent, LedgerConflict, fold_run_ledger

    base = AttemptEvent(
        attempt_id="a" * 64,
        event="prepared",
        run_id="r",
        provider="audd",
        query_id="q" * 64,
        parent_attempt_id=None,
        unit_usd_e6=UNIT_USD_E6,
        outcome=None,
        ordinal=0,
    )
    with pytest.raises(LedgerConflict):
        fold_run_ledger("r", [base, replace(base, ordinal=1)])


def test_a_fresh_run_with_intake_checkpoints_still_refreshes_cached_no_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review P1-4, a regression of the first fix pass.

    Queue intake writes ``ingest`` and ``decode`` before a run's first pipeline pass; that is not
    evidence of an earlier pass of the same run.
    """

    work = tmp_path / "work"
    no_match = {"shazam": {"default": "no_match"}}
    first = _local(work, recipe=FREE_RECIPE, shazam=FakeShazamHTTP(no_match))
    assert run(_request(first, run_id="caches-no-match", recipe=FREE_RECIPE)).status

    monkeypatch.setattr("id_detector.pipeline.find_result", lambda *args, **kwargs: None)
    document = json.loads(first._path("caches-no-match").read_text(encoding="utf-8"))
    intake_only = {
        "run_id": "fresh-hosted-run",
        "phases": {phase: document["phases"][phase] for phase in ("ingest", "decode")},
    }
    fresh_shazam = FakeShazamHTTP(no_match)
    fresh = _local(work, recipe=FREE_RECIPE, shazam=fresh_shazam)
    fresh._path("fresh-hosted-run").write_text(json.dumps(intake_only), encoding="utf-8")
    assert {"ingest", "decode"} <= fresh.completed_phases("fresh-hosted-run")
    run(_request(fresh, run_id="fresh-hosted-run", recipe=FREE_RECIPE))
    assert fresh_shazam.requests > 0, "a fresh run reused another run's cached no_match"


class _KillShazamAfterSend(FakeShazamHTTP):
    """Dies inside the ``kill_on``-th request: dispatch accounted, no answer received."""

    def __init__(self, script, kill_on: int) -> None:
        super().__init__(script)
        self.kill_on = kill_on
        self.seen = 0
        self.killed_window: int | None = None

    async def request(self, method: str, url: str, *args: object, **kwargs):
        self.seen += 1
        if self.seen == self.kill_on:
            self.killed_window = self.script.index_for_path(self._path.get())
            raise _HardKill("killed with a Shazam request in flight")
        return await super().request(method, url, *args, **kwargs)


def test_an_unresolved_shazam_dispatch_is_never_resent_on_a_same_run_resume(
    tmp_path: Path,
) -> None:
    """Review P1-3: a Shazam request sent and never answered is not automatically sent again."""

    work = tmp_path / "work"
    run_id = "shazam-ambiguous"
    killer = _KillShazamAfterSend(SCRIPT, kill_on=3)
    with pytest.raises(_HardKill):
        run(
            _request(
                _local(work, recipe=FREE_RECIPE, shazam=killer), run_id=run_id, recipe=FREE_RECIPE
            )
        )
    assert killer.killed_window is not None
    # Round-2 review P1-3: lose the per-media Shazam job store (its `submission_started` row is
    # what protected the query before). Only the durable attempt ledger can now prevent a resend.
    for root, _dirs, files in os.walk(native_path(work)):
        for name in files:
            if name.startswith("jobs.sqlite"):
                os.unlink(os.path.join(root, name))
    resumed = FakeShazamHTTP(SCRIPT)
    run(
        _request(
            _local(work, recipe=FREE_RECIPE, shazam=resumed), run_id=run_id, recipe=FREE_RECIPE
        )
    )
    windows = [attempt["window"] for attempt in resumed.attempts]
    assert killer.killed_window not in windows, windows


def test_a_resume_whose_durable_reservation_is_already_spent_ends_partial_not_failed(
    tmp_path: Path,
) -> None:
    """Round-2 review P1-6: the reservation matches its durable record and is fully spent.

    The resume must still run the sweep (reusing every clip this run resolved, admitting nothing)
    and end ``partial`` / ``reservation_exhausted`` — skipping it left no primary observation file
    and fusion crashed.
    """

    from id_detector.run_ledger import ReservationRecord

    work = tmp_path / "work"
    run_id = "reservation-filled"
    dispatched = _crash_mid_primary(work, run_id)
    spent = len(dispatched) * UNIT_USD_E6
    sidecar = next(
        Path(root) / f"{run_id}.json"
        for root, _dirs, files in os.walk(native_path(work))
        if f"{run_id}.json" in files and os.path.basename(root) == "reservations"
    )
    planned = len(dispatched) - 1
    reserved_e6 = (planned * UNIT_USD_E6 * 105 + 99) // 100
    assert reserved_e6 < spent
    filled = ReservationRecord(
        run_id=run_id,
        planned=planned,
        unit_usd_e6=UNIT_USD_E6,
        usd_e6_reserved=reserved_e6,
        usd_e2_reserved=(reserved_e6 + 9_999) // 10_000,
        effective_cap_e2=DEEP_RECIPE.max_usd_e2,
    )
    with open(native_path(sidecar), "w", encoding="utf-8") as handle:
        handle.write(json.dumps(filled.document()))

    audd = FakeAudD(SCRIPT)
    result = run(_request(_local(work, audd=audd), run_id=run_id))
    assert (result.status, result.reason) == ("partial", "reservation_exhausted"), result
    assert audd.calls == 0
    assert result.usd_e6_spent == spent

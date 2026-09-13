from __future__ import annotations

import json
import os
import threading
from dataclasses import fields
from pathlib import Path

import pytest
from typer.testing import CliRunner

from id_detector import cli
from id_detector.attempts import AttemptJournal
from id_detector.io import native_path, path_is_file
from id_detector.providers.base import AppConfig
from id_detector.recipes import DEEP_RECIPE, FREE_RECIPE
from id_detector.service import (
    CHECKPOINT_PHASES,
    STATUS_EXIT_CODES,
    LocalCheckpointStore,
    LocalPath,
    PipelineOptions,
    PlatformUrl,
    RunRequest,
    RunResult,
    TargetRefused,
    UploadId,
    exit_code_for,
    recover_paid_attempts,
    run,
    validate_platform_url,
)
from id_detector.shazam_breaker import BreakerConfig, ShazamBreaker
from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff

ROOT = Path(__file__).resolve().parents[1]
AUDIO = ROOT / "tests/fixtures/audio/tone-60s.wav"
SCRIPT = ROOT / "tests/fakes/scripts/gate0a-deep.json"


def _store(
    work: Path,
    *,
    recipe=DEEP_RECIPE,
    audd: FakeAudD | None = None,
    shazam: FakeShazamHTTP | None = None,
    refresh: bool = False,
    refresh_states: frozenset[str] = frozenset(),
    app_config: AppConfig | None = None,
    shazam_breaker: ShazamBreaker | None = None,
    allow_degrade: bool = False,
    mode: str = "local",
) -> LocalCheckpointStore:
    return LocalCheckpointStore(
        work,
        mode=mode,  # type: ignore[arg-type]
        options=PipelineOptions(
            project_root=ROOT,
            refresh=refresh,
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
            paid_sleep=no_backoff,
        ),
    )


def _request(
    store: LocalCheckpointStore,
    *,
    run_id: str,
    recipe=DEEP_RECIPE,
    cancel_token=None,
    progress=None,
    attempt_journal: AttemptJournal | None = None,
    target=None,
) -> RunRequest:
    return RunRequest(
        run_id=run_id,
        analysis_key="",
        target=LocalPath(AUDIO) if target is None else target,
        recipe=recipe,
        accept_degraded=False,
        hints_snapshot_policy="disabled",
        manual_tracklist=None,
        checkpoint_store=store,
        attempt_journal=attempt_journal,
        usd_admitter=None,
        progress=progress,
        cancel_token=cancel_token,
    )


def _entries(work: Path, run_id: str) -> list[dict]:
    """Every journal entry this run wrote, oldest first (a resumed run writes more than one)."""

    found: list[dict] = []
    for path in sorted(work.rglob("invocations.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if value["invocation_id"] == run_id:
                found.append(value)
    return found


def _entry(work: Path, run_id: str) -> dict:
    found = _entries(work, run_id)
    if not found:
        raise AssertionError(f"run {run_id} was not journalled")
    return found[0]


UNIT_USD_E6 = AppConfig().audd_usd_e6_per_request
#: The Deep recipe's frozen clip count over the 60 s fixture (seven generation-zero windows).
DEEP_CLIPS = 7


class _Boom(RuntimeError):
    """A crash — a power cut, an OOM kill — in the middle of the paid primary."""


def _crash_after(clips: int):
    """A progress hook that dies once ``clips`` paid clips have resolved."""

    def progress(phase: str, done: int, _total: int, _message: str) -> None:
        if phase == "recognise" and done >= clips:
            raise _Boom("the worker died mid-primary")

    return progress


def _crash_at_phase(target_phase: str):
    """A progress hook that dies as ``target_phase`` starts (its predecessors are checkpointed)."""

    def progress(phase: str, done: int, _total: int, _message: str) -> None:
        if phase == target_phase and done == 0:
            raise _Boom(f"the worker died entering {target_phase}")

    return progress


def _trim_to(store: LocalCheckpointStore, run_id: str, last_phase: str) -> None:
    path = store._path(run_id)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["phases"] = {
        phase: value
        for phase, value in document["phases"].items()
        if CHECKPOINT_PHASES.index(phase) <= CHECKPOINT_PHASES.index(last_phase)
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def test_public_request_and_result_have_the_plan_contract() -> None:
    assert [item.name for item in fields(RunRequest)] == [
        "run_id",
        "analysis_key",
        "target",
        "recipe",
        "accept_degraded",
        "hints_snapshot_policy",
        "manual_tracklist",
        "checkpoint_store",
        "attempt_journal",
        "usd_admitter",
        "progress",
        "cancel_token",
    ]
    assert [item.name for item in fields(RunResult)] == [
        "run_id",
        "status",
        "reason",
        "achieved",
        "bundle_id",
        "usd_e6_reserved",
        "usd_e6_spent",
        "attempts",
    ]


def test_full_local_service_run_matches_the_cli_bundle_and_honours_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    shazam = FakeShazamHTTP(SCRIPT)
    supplied = "caller-supplied-run"
    result = run(
        _request(
            _store(work, recipe=FREE_RECIPE, shazam=shazam), run_id=supplied, recipe=FREE_RECIPE
        )
    )
    assert result.status == "complete"
    assert result.run_id == supplied
    assert _entry(work, supplied)["bundle_id"] == result.bundle_id

    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    monkeypatch.setenv("IDEA_FAKE_SCRIPT", str(SCRIPT))
    invoked = CliRunner().invoke(
        cli.app,
        [
            "analyse",
            str(AUDIO),
            "--work-root",
            str(work),
            "--recipe",
            "free",
            "--no-hints",
            "--fake-providers",
            "audd,shazam",
        ],
    )
    assert invoked.exit_code == 0, invoked.output
    assert result.bundle_id in invoked.output


def test_hosted_caller_cannot_pass_a_local_path(tmp_path: Path) -> None:
    store = LocalCheckpointStore(tmp_path, mode="hosted")
    with pytest.raises(ValueError, match="only in local mode"):
        run(_request(store, run_id="hosted-local-refused", recipe=FREE_RECIPE))


class RecordingStore(LocalCheckpointStore):
    def __init__(self, work_root: Path, *, options: PipelineOptions) -> None:
        super().__init__(work_root, options=options)
        self.writes: list[tuple[str, tuple[Path, ...]]] = []

    def write(self, run_id, phase, *, artefacts, state=None) -> None:
        assert all(path_is_file(path) for path in artefacts)
        self.writes.append((phase, artefacts))
        super().write(run_id, phase, artefacts=artefacts, state=state)


def test_checkpoints_follow_the_exact_order_after_their_artefacts_exist(tmp_path: Path) -> None:
    base = _store(tmp_path / "work", recipe=FREE_RECIPE, shazam=FakeShazamHTTP(SCRIPT))
    store = RecordingStore(base.work_root, options=base.options)
    result = run(_request(store, run_id="checkpoint-order", recipe=FREE_RECIPE))
    assert result.status == "complete"
    assert tuple(phase for phase, _ in store.writes) == CHECKPOINT_PHASES
    assert store.completed_phases(result.run_id) == frozenset(CHECKPOINT_PHASES)


def test_resume_from_primary_issues_zero_audd_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    first_audd = FakeAudD(SCRIPT)
    run_id = "resume-primary"
    store = _store(work, audd=first_audd)
    first = run(_request(store, run_id=run_id))
    assert first.status in {"complete", "degraded"}
    assert first_audd.calls == 7

    checkpoint = store._path(run_id)
    document = json.loads(checkpoint.read_text(encoding="utf-8"))
    document["phases"] = {
        phase: value
        for phase, value in document["phases"].items()
        if CHECKPOINT_PHASES.index(phase) <= CHECKPOINT_PHASES.index("primary")
    }
    checkpoint.write_text(json.dumps(document), encoding="utf-8")

    resumed_audd = FakeAudD(SCRIPT)
    resumed = _store(work, audd=resumed_audd)
    monkeypatch.setattr("id_detector.pipeline.find_result", lambda *args, **kwargs: None)
    second = run(_request(resumed, run_id=run_id))
    assert second.status in {"complete", "degraded"}
    assert resumed_audd.calls == 0


def test_cancellation_mid_primary_leaves_a_resumable_checkpoint(tmp_path: Path) -> None:
    work = tmp_path / "work"
    token = threading.Event()
    audd = FakeAudD(SCRIPT, latency_s=0.02)

    def progress(phase: str, done: int, _total: int, _message: str) -> None:
        if phase == "recognise" and done >= 1:
            token.set()

    store = _store(work, audd=audd)
    result = run(
        _request(
            store,
            run_id="cancel-mid-primary",
            cancel_token=token,
            progress=progress,
        )
    )
    assert result.status == "cancelled"
    completed = store.completed_phases(result.run_id)
    assert {"ingest", "decode", "windows"} <= completed
    assert "primary" not in completed
    assert 0 < audd.calls < 7


def test_result_surfaces_the_journal_status_reason_achievement_and_spend(tmp_path: Path) -> None:
    work = tmp_path / "work"
    result = run(_request(_store(work, audd=FakeAudD(SCRIPT)), run_id="result-journal"))
    entry = _entry(work, result.run_id)
    assert result.status == entry["status"]
    assert result.reason == entry["reason"]
    assert result.achieved == entry["achieved"]
    assert result.usd_e6_reserved == entry["usd_e6_reserved"]
    assert result.usd_e6_spent == entry["usd_e6_spent"]
    assert result.attempts == entry["counts"]["paid_attempts"]


# --------------------------------------------------------------------------- P0-2: the boundary


def test_a_platform_url_can_never_name_a_filesystem_path(tmp_path: Path) -> None:
    """Plan §4.2: workers never receive user paths.

    ``ingest`` treats an existing path as local audio, so an unvalidated ``PlatformUrl`` would let a
    hosted caller read server files — and journal them as a *public* ``platform`` analysis. The
    refusal is at the seam, before any path resolution or ingestion.
    """

    store = _store(tmp_path / "work", recipe=FREE_RECIPE, mode="hosted")
    for value in (
        str(AUDIO),
        AUDIO.as_uri(),
        "file:///C:/mixes/set.mp3",
        "\\\\server\\share\\mix.mp3",
        "//server/share/mix.mp3",
        "C:/mixes/set.mp3",
        "/etc/passwd",
        "../../etc/passwd",
        "ftp://example.com/set",
        "https://user:secret@example.com/set",
        "https:///no-host",
        "",
        "  ",
    ):
        with pytest.raises(TargetRefused):
            run(
                _request(store, run_id="refuse-path", recipe=FREE_RECIPE, target=PlatformUrl(value))
            )
    assert validate_platform_url("https://soundcloud.com/dj/a-set") == (
        "https://soundcloud.com/dj/a-set"
    )


def test_an_upload_id_must_be_an_opaque_token(tmp_path: Path) -> None:
    work = tmp_path / "work"
    store = _store(work, recipe=FREE_RECIPE, mode="hosted")
    for value in ("../../etc/passwd", "a/b", "..", "", "C:\\mix.mp3", "x" * 200, "id with space"):
        with pytest.raises(TargetRefused):
            run(_request(store, run_id="refuse-upload", recipe=FREE_RECIPE, target=UploadId(value)))

    uploads = work / ".uploads"
    uploads.mkdir(parents=True)
    (uploads / "abc123DEF").write_bytes(b"audio")
    assert store.resolve_upload("abc123DEF") == (uploads / "abc123DEF").resolve()
    with pytest.raises(TargetRefused):
        store.resolve_upload("never-minted")


# ------------------------------------------------------- P0-1: one seam, one outcome, one mapping


def test_the_service_is_the_only_place_a_status_becomes_an_exit_code() -> None:
    """Statuses map to exit codes once, and the pipeline lives behind the seam, not in the CLI."""

    for status, code in STATUS_EXIT_CODES.items():
        result = RunResult(
            run_id="x",
            status=status,
            reason=None,
            achieved=None,
            bundle_id=None,
            usd_e6_reserved=0,
            usd_e6_spent=0,
            attempts=0,
        )
        assert exit_code_for(result) == code
    unknown = RunResult(
        run_id="x",
        status="something_new",
        reason=None,
        achieved=None,
        bundle_id=None,
        usd_e6_reserved=0,
        usd_e6_spent=0,
        attempts=0,
    )
    assert exit_code_for(unknown) == 1

    cli_source = (ROOT / "src/id_detector/cli.py").read_text(encoding="utf-8")
    runner_source = (ROOT / "src/id_detector/webapp/runner.py").read_text(encoding="utf-8")
    pipeline_source = (ROOT / "src/id_detector/pipeline.py").read_text(encoding="utf-8")
    for source in (cli_source, runner_source):
        assert "exit_code_for(" in source
        # No second status table, and no pipeline-internal machinery, in either caller.
        assert "budget_exhausted" not in source
        assert "run_paid_clip_recognition" not in source
        assert "recognise_generation" not in source
        assert "run_generation_loop" not in source
    assert "async def _analyse" not in cli_source
    assert "async def run_analysis" in pipeline_source


def test_the_web_runner_reports_the_services_spend_not_a_journal_lookup(tmp_path: Path) -> None:
    """U-F15: the job's money comes from the run's own RunResult."""

    from id_detector.webapp import runner as runner_module

    recorded: dict[str, object] = {}

    class Ctx:
        def set_outcome(self, **kwargs: object) -> None:
            recorded.update(kwargs)

    outcome = runner_module.RunOutcome(
        run_id="b" * 32, status="partial", reason="primary_not_achieved", usd_e2_spent=4
    )
    runner_module._record_outcome(Ctx(), tmp_path, str(AUDIO), outcome)
    assert recorded["run_status"] == "partial"
    assert recorded["run_reason"] == "primary_not_achieved"
    assert recorded["usd_e2_spent"] == 4
    assert recorded["spend_known"] is True

    recorded.clear()
    runner_module._record_outcome(Ctx(), tmp_path, str(AUDIO), None)
    assert recorded == {"spend_known": False}


def test_a_served_compatible_result_reports_this_runs_zero_spend(tmp_path: Path) -> None:
    """§3.4 compatibility serving: the stored result is reported, its money is not re-charged."""

    work = tmp_path / "work"
    first = run(_request(_store(work, audd=FakeAudD(SCRIPT)), run_id="compat-first"))
    assert first.usd_e6_spent > 0
    audd = FakeAudD(SCRIPT)
    served = run(_request(_store(work, audd=audd), run_id="compat-served"))
    assert served.bundle_id == first.bundle_id
    assert served.status == first.status
    assert served.usd_e6_spent == 0
    assert served.usd_e6_reserved == 0
    assert audd.calls == 0
    assert _entries(work, "compat-served") == []


# --------------------------------------------------- P0-3 / P0-4 / P1-9: crash recovery and money


def _crashed_deep_run(
    work: Path, run_id: str, *, journal: AttemptJournal | None = None
) -> tuple[FakeAudD, LocalCheckpointStore]:
    """Run a Deep primary for real and kill the worker part-way through it."""

    audd = FakeAudD(SCRIPT)
    store = _store(work, audd=audd, refresh_states=frozenset({"no_match"}))
    with pytest.raises(_Boom):
        run(_request(store, run_id=run_id, progress=_crash_after(4), attempt_journal=journal))
    assert 0 < audd.calls < DEEP_CLIPS, audd.calls
    assert "primary" not in store.completed_phases(run_id)
    return audd, store


def test_a_crashed_primary_resumes_without_re_billing_and_keeps_its_spend(tmp_path: Path) -> None:
    """Plan §2.3.3: resuming the same run pays only for the clips it never resolved.

    The default ``refresh_states`` re-queries a cached ``no_match``, so before this fix a resumed
    primary paid AudD a second time for clips the SAME run had already settled — and settled only
    the second pass's spend, losing the first.
    """

    work = tmp_path / "work"
    run_id = "crash-primary"
    first, _ = _crashed_deep_run(work, run_id)
    ledger_path = next(iter(work.rglob("recognise/attempts.jsonl")))
    recovery = recover_paid_attempts(ledger_path, run_id=run_id)
    assert recovery.billed_units == first.calls
    assert len(recovery.resolved_query_ids) == first.calls

    resumed_audd = FakeAudD(SCRIPT)
    resumed = _store(work, audd=resumed_audd, refresh_states=frozenset({"no_match"}))
    second = run(_request(resumed, run_id=run_id))
    assert second.status in {"complete", "degraded", "partial"}, second
    # Zero AudD calls for the recovered clips — asserted on the adapter, not on a log line.
    assert resumed_audd.calls == DEEP_CLIPS - first.calls
    # Cumulative spend: the whole mix was paid for exactly once across the two passes.
    assert second.usd_e6_spent == DEEP_CLIPS * UNIT_USD_E6
    assert _entries(work, run_id)[-1]["usd_e6_spent"] == DEEP_CLIPS * UNIT_USD_E6


def test_recovery_reads_the_injected_attempt_journal(tmp_path: Path) -> None:
    """The journal the sweep WRITES to is the journal recovery READS.

    A hosted caller injects its own journal; reading the per-media default instead left that
    journal's unresolved dispatches invisible, so the next pass re-billed them.
    """

    work = tmp_path / "work"
    run_id = "injected-journal"
    hosted = tmp_path / "hosted" / "attempts.jsonl"
    first, _ = _crashed_deep_run(
        work,
        run_id,
        journal=AttemptJournal(hosted, run_id=run_id, provider="audd", unit_usd_e6=UNIT_USD_E6),
    )
    assert hosted.is_file()
    assert not list(work.rglob("recognise/attempts.jsonl"))
    assert recover_paid_attempts(hosted, run_id=run_id).billed_units == first.calls

    resumed_audd = FakeAudD(SCRIPT)
    resumed = _store(work, audd=resumed_audd, refresh_states=frozenset({"no_match"}))
    second = run(
        _request(
            resumed,
            run_id=run_id,
            attempt_journal=AttemptJournal(
                hosted, run_id=run_id, provider="audd", unit_usd_e6=UNIT_USD_E6
            ),
        )
    )
    assert second.status in {"complete", "degraded", "partial"}
    assert resumed_audd.calls == DEEP_CLIPS - first.calls
    assert second.usd_e6_spent == DEEP_CLIPS * UNIT_USD_E6


def test_a_lowered_cap_refuses_the_resume_without_erasing_its_spend(tmp_path: Path) -> None:
    """Plan §2.3.2: a refusal journals the money this run already spent, never zero."""

    work = tmp_path / "work"
    run_id = "lowered-cap"
    first, _ = _crashed_deep_run(work, run_id)
    audd = FakeAudD(SCRIPT)
    resumed = _store(
        work,
        audd=audd,
        refresh_states=frozenset({"no_match"}),
        app_config=AppConfig(max_usd_e2=1),
    )
    refused = run(_request(resumed, run_id=run_id))
    assert refused.status == "budget_exhausted"
    assert refused.reason == "reservation_exceeds_cap"
    assert audd.calls == 0
    assert refused.usd_e6_spent == first.calls * UNIT_USD_E6
    assert _entries(work, run_id)[-1]["usd_e6_spent"] == first.calls * UNIT_USD_E6


# ------------------------------------------------------------ P1-5: a cancelled primary resumes


def test_a_cancelled_primary_resumes_instead_of_colliding_with_its_own_artefacts(
    tmp_path: Path,
) -> None:
    """A resumed pass writes beside the cancelled one; the immutable file is never rewritten.

    The resumed run legitimately holds MORE observations than the pass it resumes, so reusing that
    pass's invocation directory raised ``FileExistsError`` — after every provider call had been paid
    for.
    """

    work = tmp_path / "work"
    run_id = "cancel-then-resume"
    token = threading.Event()
    audd = FakeAudD(SCRIPT, latency_s=0.02)

    def progress(phase: str, done: int, _total: int, _message: str) -> None:
        if phase == "recognise" and done >= 1:
            token.set()

    store = _store(work, audd=audd, refresh_states=frozenset({"no_match"}))
    cancelled = run(_request(store, run_id=run_id, cancel_token=token, progress=progress))
    assert cancelled.status == "cancelled"
    assert 0 < audd.calls < DEEP_CLIPS

    resumed_audd = FakeAudD(SCRIPT)
    resumed = _store(work, audd=resumed_audd, refresh_states=frozenset({"no_match"}))
    second = run(_request(resumed, run_id=run_id))
    assert second.status in {"complete", "degraded", "partial"}, second
    assert resumed_audd.calls == DEEP_CLIPS - audd.calls
    assert second.usd_e6_spent == DEEP_CLIPS * UNIT_USD_E6
    # os.walk over the long-path form: the resumed invocation directory sits deeper than
    # Windows' 260-character limit, where pathlib's own globbing quietly finds nothing.
    directories = sorted(
        name
        for _root, names, _files in os.walk(native_path(work))
        for name in names
        if name.startswith("live-audd-clip-") and name != "live-audd-clip-v1"
    )
    assert len(directories) == 2, directories
    assert directories[1].endswith("-r1")


# ---------------------------------------------------------- P1-6: retention pruned the original


def test_a_retained_result_is_served_after_retention_pruned_the_original(tmp_path: Path) -> None:
    """The ingest checkpoint names the durable source record, not the prunable audio.

    ``ingest._load_cached`` deliberately opens a retained result without the fetched original; a
    checkpoint that insisted on the original failed a post-retention cache hit before the
    compatibility lookup could serve it.
    """

    work = tmp_path / "work"
    first = run(
        _request(
            _store(work, recipe=FREE_RECIPE, shazam=FakeShazamHTTP(SCRIPT)),
            run_id="pruned-first",
            recipe=FREE_RECIPE,
        )
    )
    assert first.status == "complete"
    pruned = [
        path
        for path in work.rglob("ingest/*")
        if path.is_file() and path.name != "source.json" and not path.name.endswith(".done.json")
    ]
    assert pruned, sorted(str(path) for path in work.rglob("ingest/*"))
    for path in pruned:
        path.unlink()

    shazam = FakeShazamHTTP(SCRIPT)
    served = run(
        _request(
            _store(work, recipe=FREE_RECIPE, shazam=shazam),
            run_id="pruned-second",
            recipe=FREE_RECIPE,
        )
    )
    assert served.bundle_id == first.bundle_id
    assert shazam.requests == 0


# ------------------------------------------- P1-7: completed downstream phases are not re-run


def test_a_resumed_free_run_re_recognises_nothing(tmp_path: Path) -> None:
    """A completed Free primary is restored, not re-paid for in Shazam requests."""

    work = tmp_path / "work"
    run_id = "resume-free-primary"
    first_shazam = FakeShazamHTTP(SCRIPT)
    store = _store(work, recipe=FREE_RECIPE, shazam=first_shazam)
    with pytest.raises(_Boom):
        run(_request(store, run_id=run_id, recipe=FREE_RECIPE, progress=_crash_at_phase("fuse")))
    assert first_shazam.requests > 0
    completed = store.completed_phases(run_id)
    assert {"primary", "hints"} <= completed and "fuse1" not in completed

    second_shazam = FakeShazamHTTP(SCRIPT)
    second = run(
        _request(
            _store(work, recipe=FREE_RECIPE, shazam=second_shazam),
            run_id=run_id,
            recipe=FREE_RECIPE,
        )
    )
    assert second.status in {"complete", "degraded", "partial"}, second
    assert second_shazam.requests == 0


def test_a_completed_secondary_is_restored_and_an_open_breaker_cannot_degrade_it(
    tmp_path: Path,
) -> None:
    """The second opinion is evidence, not something to re-probe — or to throw away."""

    work = tmp_path / "work"
    run_id = "resume-secondary"
    audd = FakeAudD(SCRIPT)
    shazam = FakeShazamHTTP(SCRIPT)
    store = _store(work, audd=audd, shazam=shazam)
    with pytest.raises(_Boom):
        run(_request(store, run_id=run_id, progress=_crash_at_phase("present")))
    completed = store.completed_phases(run_id)
    assert {"secondary", "fuse2"} <= completed and "present" not in completed
    first_secondary_requests = shazam.requests

    # The breaker is open on the resume: a run whose secondary is already durable must neither
    # re-probe it nor report `degraded` for evidence it already holds.
    config = BreakerConfig(shazam_daily_budget_per_egress=1)
    breaker = ShazamBreaker(config)
    breaker.dispatch(running_free=False)
    assert breaker.reason() is not None

    resumed_shazam = FakeShazamHTTP(SCRIPT)
    resumed_audd = FakeAudD(SCRIPT)
    second = run(
        _request(
            _store(
                work,
                audd=resumed_audd,
                shazam=resumed_shazam,
                app_config=AppConfig(shazam_breaker=config),
                shazam_breaker=breaker,
            ),
            run_id=run_id,
        )
    )
    assert first_secondary_requests > 0
    assert resumed_shazam.requests == 0
    assert resumed_audd.calls == 0
    assert second.status != "degraded", (second.status, second.reason)
    assert second.reason != "shazam_breaker:b_daily_budget"


# ------------------------------------------------- P1-8: a degraded run resumes as what it became


def test_a_degraded_free_primary_resumes_as_free_and_never_as_deep(tmp_path: Path) -> None:
    """``--allow-degrade`` substituted the Free recipe; the checkpoint remembers that.

    Without the substitution in the primary's state, recovery took the paid branch and loaded the
    Free observations as AudD primary evidence.
    """

    work = tmp_path / "work"
    run_id = "degraded-resume"
    unavailable = ROOT / "tests/fakes/scripts/all-http-401.json"
    audd = FakeAudD(unavailable)
    shazam = FakeShazamHTTP(SCRIPT)
    store = _store(work, audd=audd, shazam=shazam, allow_degrade=True)
    with pytest.raises(_Boom):
        run(_request(store, run_id=run_id, progress=_crash_at_phase("fuse")))
    state = store.state(run_id, "primary")
    assert state["achieved"] == "free"
    assert state["paid_first"] is False
    assert state["degrade_reason"] == "provider_unavailable"

    resumed_audd = FakeAudD(unavailable)
    resumed_shazam = FakeShazamHTTP(SCRIPT)
    second = run(
        _request(
            _store(work, audd=resumed_audd, shazam=resumed_shazam, allow_degrade=True),
            run_id=run_id,
        )
    )
    assert second.achieved == "free"
    assert resumed_audd.calls == 0
    assert resumed_shazam.requests == 0
    assert second.usd_e6_spent == 0

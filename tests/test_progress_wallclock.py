"""The analysing page's percentage is proportional to wall-clock time (the owner's rule).

"If it'll take 10 min, every 10 % is about 1 min; if 20 min, every 10 % is about 2 min."

Every test here drives the *shipped* state machine — a real ``Job`` ticked through a real
``JobContext.progress`` — on an injected clock, and reads the bar exactly as the status route does
(``Job.progress_percent``), at the page's own 2.5 s poll cadence.  Nothing sleeps and nothing reads
the real clock, so each run is deterministic.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from id_detector.present.server import _JOB_JS, _PROGRESS_JS, _job_page_html
from id_detector.webapp.jobs import RECENT_RATE_SECONDS, Job, JobContext, JobManager

POLL_SECONDS = 2.5
#: How far the displayed percent may sit from elapsed/total once a rate has been measured.
TOLERANCE_POINTS = 3.0


class _Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@dataclass
class _Poll:
    at: float  # seconds since the job started
    percent: int
    eta_seconds: int
    estimating: bool
    rate_per_minute: float


@dataclass
class _Run:
    """One simulated analysis: what the page would have shown at every poll."""

    polls: list[_Poll] = field(default_factory=list)  # every poll while the job was running
    total_seconds: float = 0.0
    recognise_from: float = 0.0
    finished_percent: int = -1  # what the first poll after success reported

    def truth(self, poll: _Poll) -> float:
        return 100.0 * poll.at / self.total_seconds

    def after(self, seconds: float) -> list[_Poll]:
        return [poll for poll in self.polls if poll.at >= seconds]


def _simulate(
    tmp_path: Path,
    *,
    windows_total: int,
    cached: int = 0,
    rate_at: object = 45.0,
    before: tuple[float, float, float] = (20.0, 5.0, 2.0),
    after: tuple[float, float, float] = (2.0, 5.0, 1.0),
    first_poll: float = 0.0,
) -> _Run:
    """Run one job through every phase and poll its bar every :data:`POLL_SECONDS`.

    ``cached`` windows complete in the recognise pass's first instant, as a cache resume's do.
    ``rate_at`` is windows/min, or a function of seconds-into-recognise returning windows/min.
    ``first_poll`` is when the page first asks: nothing has evaluated the bar before then.
    """

    clock = _Clock()
    started = clock.now
    job = Job(
        id="a" * 32,
        target="https://soundcloud.com/example/mix",
        display="https://soundcloud.com/example/mix",
        profile="free",
        acquire=False,
        build_index=False,
        status="running",
        phase="starting",
        started_at=started,
        phase_started_at=started,
    )
    context = JobContext(JobManager(tmp_path, lambda ctx: None), job, clock=clock)
    run = _Run()
    next_poll = [started + first_poll]

    def advance(seconds: float) -> None:
        """Move the clock on, polling the bar at the page's cadence along the way."""

        target = clock.now + seconds
        while next_poll[0] <= target:
            clock.now = next_poll[0]
            run.polls.append(
                _Poll(
                    at=clock.now - started,
                    percent=job.progress_percent(clock.now),
                    eta_seconds=job.eta_seconds(clock.now),
                    estimating=job.estimating(clock.now),
                    rate_per_minute=job.rate_per_minute(clock.now),
                )
            )
            next_poll[0] += POLL_SECONDS
        clock.now = target

    for phase, seconds in zip(("ingest", "decode", "windows"), before, strict=True):
        context.progress(phase, 0, 1, "")
        advance(seconds)
        context.progress(phase, 1, 1, "")

    run.recognise_from = clock.now - started
    recognise_started = clock.now
    done = cached
    context.progress("recognise", done, windows_total, "recognising windows")
    while done < windows_total:
        into = clock.now - recognise_started
        per_minute = rate_at(into) if callable(rate_at) else rate_at
        advance(60.0 / float(per_minute))
        done += 1
        context.progress("recognise", done, windows_total, "recognising windows")

    for phase, seconds in zip(("hints", "fuse", "present"), after, strict=True):
        context.progress(phase, 0, 1, "")
        advance(seconds)
        context.progress(phase, 1, 1, "")
    run.total_seconds = clock.now - started
    # The terminal hand-off, as ``JobManager._execute`` does it: the job succeeds and the very
    # next poll reads it.
    job.status = "succeeded"
    job.phase = "done"
    job.finished_at = clock.now
    run.finished_percent = job.progress_percent(clock.now)
    assert job.eta_seconds(clock.now) == 0 and job.estimating(clock.now) is False
    return run


def _never_backwards(run: _Run) -> None:
    percents = [poll.percent for poll in run.polls]
    assert percents == sorted(percents), "the bar moved backwards"
    assert max(percents) <= 99  # it never claims to be done before it is
    assert run.finished_percent == 100  # ...and says so the moment it is


def _largest_step(run: _Run) -> int:
    """The biggest move between two consecutive polls of the RUNNING job (0 for a single poll)."""

    percents = [poll.percent for poll in run.polls]
    return max((b - a for a, b in zip(percents, percents[1:], strict=False)), default=0)


def test_at_a_steady_rate_every_tenth_of_the_bar_is_a_tenth_of_the_time(tmp_path: Path) -> None:
    """The owner's rule, asserted directly — on a ~10 minute run and again on a ~20 minute one."""

    for windows_total in (450, 900):  # 45 windows/min: 10 and 20 minutes of listening
        run = _simulate(tmp_path, windows_total=windows_total)
        assert run.total_seconds == pytest.approx(windows_total / 45 * 60 + 35, abs=1.0)
        _never_backwards(run)
        # Once the rate has been measured (a minute in), the bar IS elapsed/total.
        settled = run.after(run.recognise_from + 60.0)
        assert len(settled) > 200
        for poll in settled:
            assert abs(poll.percent - run.truth(poll)) <= TOLERANCE_POINTS, poll
        # ...so each 10 % arrives a tenth of the total time after the last one.
        for tenth in range(2, 10):
            reached = next(poll.at for poll in run.polls if poll.percent >= tenth * 10)
            assert reached == pytest.approx(run.total_seconds * tenth / 10, rel=0.06)
        # It neither leaps nor crawls: no poll moves the bar by more than a couple of points.
        assert _largest_step(run) <= 2
        # The time left is the same estimate, so it is right too.
        for poll in settled:
            assert poll.eta_seconds == pytest.approx(run.total_seconds - poll.at, abs=30.0), poll


def test_a_cache_resume_burst_is_not_counted_as_work_or_as_speed(tmp_path: Path) -> None:
    """A resumed run whose first 60 % came back instantly from cache does not show 60 %.

    All of the *waiting* is still ahead, so the bar starts near zero and then tracks the time the
    remaining 40 % really takes; and the burst does not inflate the rate the estimate uses.
    """

    run = _simulate(tmp_path, windows_total=450, cached=270, before=(0.4, 0.3, 0.1))
    _never_backwards(run)
    assert run.total_seconds == pytest.approx(180 / 45 * 60 + 8.8, abs=1.0)
    first = run.after(run.recognise_from)[0]
    assert first.percent <= 5, first  # 270 of 450 windows are done, and the bar does not say 60
    listening_ends = run.recognise_from + 180 / 45 * 60
    for poll in run.after(run.recognise_from + 60.0):
        # The rate is the live one (45/min), not 270 windows-in-an-instant folded into an average.
        if poll.at < listening_ends:
            assert poll.rate_per_minute == pytest.approx(45.0, rel=0.1), poll
        assert abs(poll.percent - run.truth(poll)) <= TOLERANCE_POINTS, poll
    assert _largest_step(run) <= 4  # a four-minute run: a poll is ~1 % of it, times the catch-up


def test_a_mid_run_rate_drop_holds_the_bar_until_time_catches_up(tmp_path: Path) -> None:
    """Shazam throttles five minutes in: 45 windows/min becomes 20.  The estimate lengthens, the
    bar holds where it was instead of stepping down, and then it tracks the *new* total."""

    drop_at = 300.0
    run = _simulate(
        tmp_path,
        windows_total=900,
        rate_at=lambda into: 45.0 if into < drop_at else 20.0,
    )
    _never_backwards(run)
    dropped = run.recognise_from + drop_at
    before = [poll for poll in run.polls if dropped - 30.0 <= poll.at < dropped][-1]
    # Just before the drop the run looked like a ~20 minute one and the bar said so.
    assert before.percent == pytest.approx(100.0 * before.at / (900 / 45 * 60 + 62), abs=3.0)
    # Within about one sliding window the estimate has followed the limiter down...
    adapted = run.after(dropped + RECENT_RATE_SECONDS + 15.0)
    for poll in adapted:
        assert poll.rate_per_minute == pytest.approx(20.0, rel=0.12), poll
    assert adapted[0].eta_seconds > before.eta_seconds + 600  # the honest, much longer time left
    # ...the bar is held (never lowered) while the truth is below it...
    held = [poll for poll in adapted if run.truth(poll) < before.percent - TOLERANCE_POINTS]
    assert held and {poll.percent for poll in held} <= {before.percent, before.percent + 1}
    # ...and once time has caught up it is wall-clock proportional again, to the real total.
    caught_up = [poll for poll in adapted if run.truth(poll) >= before.percent + 2]
    assert len(caught_up) > 200
    for poll in caught_up:
        assert abs(poll.percent - run.truth(poll)) <= TOLERANCE_POINTS, poll


def test_a_stall_never_moves_the_bar_backwards_and_lengthens_the_time_left(tmp_path: Path) -> None:
    """No window completes for two minutes: the recent rate decays, the time left grows, and the
    displayed value holds."""

    run = _simulate(
        tmp_path,
        windows_total=450,
        # One window takes two minutes (a rate of 0.5/min) a little over three minutes in.
        rate_at=lambda into: 0.5 if 200.0 <= into < 201.0 else 45.0,
    )
    _never_backwards(run)
    # The last quick window lands just after 200 s; the next one takes two minutes.
    stall_from = run.recognise_from + 60.0 / 45 * 151
    stall_ends = stall_from + 120.0
    stalled = [poll for poll in run.polls if stall_from + 5.0 <= poll.at <= stall_ends - 5.0]
    etas = [poll.eta_seconds for poll in stalled]
    assert etas == sorted(etas) and etas[-1] > etas[0] + 300  # it admits the delay as it grows
    assert max(poll.percent for poll in stalled) - min(poll.percent for poll in stalled) <= 1

    # Then the work resumes at 45/min, and "recent" has to mean recent: the rate is back within
    # ONE sliding window of the recovery, not averaged with the stall for minutes afterwards.
    def rate(seconds_after: float) -> float:
        return next(p.rate_per_minute for p in run.polls if p.at >= stall_ends + seconds_after)

    assert rate(RECENT_RATE_SECONDS / 2) > 18.0  # half a window in: already about half-way back
    assert rate(RECENT_RATE_SECONDS + 3.0) == pytest.approx(45.0, rel=0.08)
    recovered = [poll for poll in run.polls if poll.at >= stall_ends + RECENT_RATE_SECONDS + 3.0]
    listening_ends = run.recognise_from + 60.0 / 45 * 449 + 120.0
    for poll in recovered:
        if poll.at < listening_ends:
            assert poll.rate_per_minute == pytest.approx(45.0, rel=0.08), poll
        # The bar, held through the stall, is not pinned while dozens of windows complete: it
        # walks back to the truth at the catch-up pace (under a minute for the ~9 points it fell
        # behind) and is wall-clock proportional again from there.
        if poll.at >= stall_ends + RECENT_RATE_SECONDS + 60.0:
            assert abs(poll.percent - run.truth(poll)) <= TOLERANCE_POINTS, poll
    assert recovered[40].percent >= recovered[0].percent + 10  # 100 s later: visibly moving


def test_before_there_is_rate_data_the_page_estimates_instead_of_racing_ahead(
    tmp_path: Path,
) -> None:
    run = _simulate(tmp_path, windows_total=450)
    early = [poll for poll in run.polls if poll.at < run.recognise_from + 10.0]
    assert early and all(poll.estimating for poll in early)
    # The early number is the cautious prior's: never ahead of where the run really is.
    for poll in early:
        assert poll.percent <= run.truth(poll) + 1.0, poll
    # A measured rate arrives within half a minute of listening, and the guess label goes away —
    # by climbing to the measured value over a few polls, not by jumping to it.
    measured = run.after(run.recognise_from + 30.0)
    assert not any(poll.estimating for poll in measured)
    assert _largest_step(run) <= 2
    # Two or three quick ticks are not a rate: a burst of 3 windows in 2 s stays "estimating".
    clock = _Clock()
    job = Job("b" * 32, "t", "t", "free", False, False, status="running", phase="windows")
    context = JobContext(JobManager(tmp_path, lambda ctx: None), job, clock=clock)
    for done in range(4):
        context.progress("recognise", done, 400, "")
        clock.now += 0.7
    assert job.recent_rate_per_minute(clock.now) is None and job.estimating(clock.now)
    assert job.rate_per_minute(clock.now) == 18.0  # the cautious prior, not 250 windows/min


def test_the_sample_list_stays_bounded_and_restarts_with_each_pass(tmp_path: Path) -> None:
    clock = _Clock()
    job = Job("c" * 32, "t", "t", "free", False, False, status="running", phase="windows")
    context = JobContext(JobManager(tmp_path, lambda ctx: None), job, clock=clock)
    for done in range(2_000):  # ten ticks a second for 200 s
        context.progress("recognise", done, 5_000, "")
        clock.now += 0.1
    assert len(job.rate_samples) <= RECENT_RATE_SECONDS + 3  # ~one per second of the window
    assert job.rate_samples[-1][0] - job.rate_samples[0][0] <= RECENT_RATE_SECONDS + 2
    assert job.rate_per_minute(clock.now) == pytest.approx(600.0, rel=0.02)
    json.dumps(job.rate_samples)  # it rides in the published snapshot, so it must be JSON
    # A new pass (the paid-engine restart reports 0 of 1, then a new total) starts afresh: the old
    # pass's samples must not be differenced against the new pass's count.
    context.progress("recognise", 0, 1, "restarting as the free recipe")
    assert job.rate_samples == [[clock.now, 0.0]]
    context.progress("recognise", 120, 300, "")  # a cache burst opens the new pass
    assert job.rate_samples == [[clock.now, 120.0]]
    assert job.recent_rate_per_minute(clock.now) is None


#: No poll of a running job may move the bar further than this, however its estimate collapses:
#: 2.5 points a second over a 2.5 s poll is 6.25, and the page shows whole points.
MAX_POLL_STEP = 7


def test_a_collapsed_estimate_cannot_make_the_bar_leap(tmp_path: Path) -> None:
    """Fully cached, very short and below-the-rate-threshold runs, each through to the hand-off.

    When listening finishes instantly the expected total shrinks to seconds, and a climb limit
    expressed only as a multiple of "the natural slope" would then allow any jump (0 to 66 % in
    one poll).  The running bar is bounded in points per second instead; the job being *finished*
    is the one step that is not smoothed, because done is done.
    """

    cases: dict[str, dict[str, object]] = {
        # All 450 windows back from cache at once; the closing steps take a few polls.
        "fully cached": {"windows_total": 450, "cached": 450, "before": (0.4, 0.3, 0.1)},
        # The same, and so quick that the whole job fits between two polls.
        "cached, over before the next poll": {
            "windows_total": 450,
            "cached": 450,
            "before": (0.4, 0.3, 0.1),
            "after": (0.3, 0.5, 0.2),
        },
        # Half a minute of listening: a rate is measured only just before it ends.
        "very short": {"windows_total": 20, "before": (1.0, 0.5, 0.2)},
        # Eight windows (under 15 s): the rate is never trusted, so listening ends while the
        # estimate still rests on the 18/min prior and the remaining time collapses at once.
        "below the rate threshold": {"windows_total": 8, "before": (1.0, 0.5, 0.2)},
        # A resume with a handful of windows left, the same way.
        "resume, a few windows left": {
            "windows_total": 450,
            "cached": 444,
            "before": (0.4, 0.3, 0.1),
        },
    }
    runs = {name: _simulate(tmp_path, **arguments) for name, arguments in cases.items()}  # type: ignore[arg-type]
    for name, run in runs.items():
        _never_backwards(run)
        assert _largest_step(run) <= MAX_POLL_STEP, (name, [poll.percent for poll in run.polls])
        assert run.polls[0].percent == 0, name
        assert run.finished_percent == 100, name
    # The first poll is not exempt.  A page that first asks 2.5 s into a fully cached run is the
    # first thing to evaluate the bar, with listening already over: it is limited from the moment
    # the job started, not handed the collapsed estimate (66 %) because it has no history.
    late = _simulate(tmp_path, **cases["fully cached"], first_poll=2.5)  # type: ignore[arg-type]
    assert late.polls[0].at == 2.5 and late.polls[0].percent <= MAX_POLL_STEP, late.polls[0]
    assert _largest_step(late) <= MAX_POLL_STEP
    sub_threshold = runs["below the rate threshold"]
    listening = [poll for poll in sub_threshold.polls if 2.0 <= poll.at <= 12.0]
    assert listening and all(poll.estimating for poll in listening)
    # One poll, then finished: the bar never got the chance to move, and is not made to pretend.
    assert [poll.percent for poll in runs["cached, over before the next poll"].polls] == [0]

    # The hand-off itself: on success the page replaces the whole scan panel (the percentage
    # lives inside it) with the result, so the last running number is never animated to 100.
    assert "document.getElementById('scan').style.display = 'none';" in _JOB_JS
    page = _job_page_html(
        Job("d" * 32, "https://soundcloud.com/example/mix", "x", "free", False, False)
    ).decode("utf-8")
    scan = page[page.index('<section class="scan" id="scan"') :]
    assert 'id="pct"' in scan[: scan.index("</section>")]


def test_the_page_words_the_time_left_plainly() -> None:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - node is present in the gate environment
        pytest.skip("node is not installed")
    jobs = [
        {"status": "running", "eta_seconds": 600, "eta_estimating": True},
        {"status": "running", "eta_seconds": 600, "eta_estimating": False},
        {"status": "running", "eta_seconds": 1_290, "eta_estimating": False},
        {"status": "running", "eta_seconds": 4_500, "eta_estimating": False},
        {"status": "running", "eta_seconds": 30, "eta_estimating": False},
        {"status": "queued", "eta_seconds": 0, "eta_estimating": True},
        {"status": "succeeded", "terminal": True, "eta_seconds": 0, "eta_estimating": False},
    ]
    script = _PROGRESS_JS + f"console.log(JSON.stringify({json.dumps(jobs)}.map(etaWords)));"
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, check=True, encoding="utf-8"
    )
    assert json.loads(result.stdout) == [
        "Estimating…",
        "about 10 min",
        "about 22 min",
        "about 1 hr 15 min",
        "under a minute",
        "…",
        "—",
    ]

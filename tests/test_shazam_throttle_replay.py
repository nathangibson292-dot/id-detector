"""Shazam 429 handling, proven on a simulated clock against the owner's real throttle pattern.

On 2026-10-21 the owner's 808-window scan sent 1518 Shazam requests: 811 were HTTP 429s and 101
windows were given up after six tries each.  ``tests/fixtures/shazam/throttle-replay-2026-10-21
.json`` keeps exactly what Shazam did to each of those requests (when it was sent, and whether it
was throttled or answered).  The fake Shazam below replays that pattern: a request sent at time
``t`` into the sweep gets what the owner's request sent most recently before ``t`` got (the 96-min
log repeats after it ends, the way its ~30-minute block cycle did).  A second fake models the
cause instead -- an hourly-style sliding quota of answered requests -- so the proof does not hinge
on one reading of the log.

The sweep under test is the REAL request path: the real ``TokenBucket`` (rate, AIMD and pause), the
real ``InjectedHTTPClient`` (Retry-After parsing, penalties) over an ``httpx.MockTransport``, and
the real per-window decision ``next_attempt_after_failure`` the recognise job runner uses, plus
the end-of-sweep retry pass's rule (one more ask per errored window, at most 10 %).  "Old" is the
same code with the pause off -- byte-for-byte the previous behaviour.  No network, no real time.
"""

from __future__ import annotations

import asyncio
import bisect
import json
import selectors
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from id_detector.recognise import (
    MAX_RETRIES,
    RETRY_PASS_FRACTION_E2,
    RETRY_PASS_MIN,
    SHAZAM_THROTTLE_GIVE_UP_S,
    SHAZAM_THROTTLE_PAUSE_MAX_S,
    SHAZAM_THROTTLE_PAUSE_S,
    SHAZAM_THROTTLE_STREAK,
    next_attempt_after_failure,
)
from id_detector.shazam import CircuitBreaker, InjectedHTTPClient, ShazamHTTPError, TokenBucket

FIXTURE = Path(__file__).parent / "fixtures" / "shazam" / "throttle-replay-2026-10-21.json"
#: Today's defaults (``[recognise]`` requests_per_minute / concurrency).
RATE_PER_MINUTE = 45
CONCURRENCY = 3
#: Median latencies measured in the same log.
ANSWER_LATENCY_S = 0.6
THROTTLE_LATENCY_S = 0.3


class _AdvancingSelector(selectors.SelectSelector):
    """Never sleeps: when the loop would wait for its next timer, jump the clock exactly there."""

    def __init__(self, loop: SimulatedClockLoop) -> None:
        super().__init__()
        self._loop = loop

    def select(self, timeout: float | None = None) -> list[Any]:
        events = super().select(0)
        if events or timeout == 0:
            return events
        if timeout is None:
            raise RuntimeError("simulated sweep deadlocked: nothing scheduled")
        self._loop.advance_to_next_timer(timeout)
        return []


class SimulatedClockLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        self._now = 0.0
        super().__init__(_AdvancingSelector(self))
        # A timer is due once the clock has reached it (asyncio needs a resolution above 0), and
        # never "close enough" early: on a clock that only moves when asked, that would spin.
        self._clock_resolution = 1e-9

    def time(self) -> float:
        return self._now

    def call_later(self, delay: float, callback: Any, *args: Any, context: Any = None) -> Any:
        # A positive sleep must move the clock: one too small to change a float would fire at
        # once on an unmoved clock and re-schedule itself forever.
        if delay > 0:
            delay = max(delay, 1e-6)
        return super().call_later(delay, callback, *args, context=context)

    def advance_to_next_timer(self, timeout: float) -> None:
        scheduled = [handle.when() for handle in self._scheduled if not handle.cancelled()]
        target = min(scheduled) if scheduled else self._now + timeout
        self._now = max(self._now, target)


def _simulate(coro_factory: Any) -> Any:
    loop = SimulatedClockLoop()
    try:
        return loop.run_until_complete(coro_factory(loop))
    finally:
        loop.close()


@dataclass
class ReplayShazam:
    """What Shazam did to the owner's request sent most recently before ``t`` (cyclic)."""

    times_s: list[float]
    throttled_flags: list[bool]
    period_s: float

    @classmethod
    def from_fixture(cls) -> ReplayShazam:
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        times = [ms / 1000 for ms in data["dispatched_ms"]]
        flags = [char == "T" for char in data["outcomes"]]
        return cls(times, flags, period_s=times[-1] + 1.0)

    def throttled(self, now: float, sent: int) -> bool:
        del sent
        into = now % self.period_s
        index = max(0, bisect.bisect_right(self.times_s, into) - 1)
        return self.throttled_flags[index]

    def answered(self, now: float) -> None:
        del now


@dataclass
class QuotaShazam:
    """A model of the cause: at most ``limit`` answers in any ``window_s``; 429s are free."""

    limit: int = 200
    window_s: float = 1_800.0
    burst_per_minute: int = 24
    answers: deque[float] = field(default_factory=deque)
    recent: deque[float] = field(default_factory=deque)

    def throttled(self, now: float, sent: int) -> bool:
        del sent
        while self.answers and self.answers[0] <= now - self.window_s:
            self.answers.popleft()
        while self.recent and self.recent[0] <= now - 60.0:
            self.recent.popleft()
        return len(self.answers) >= self.limit or len(self.recent) >= self.burst_per_minute

    def answered(self, now: float) -> None:
        self.answers.append(now)
        self.recent.append(now)


@dataclass
class SweepResult:
    wall_s: float
    requests: int
    throttled: int
    answered: int
    unanswered: int
    pauses: int = 0
    answered_at: list[float] = field(default_factory=list)

    def time_to_answer(self, count: int) -> float:
        """When the ``count``-th window was answered (``inf`` if it never was)."""

        return self.answered_at[count - 1] if 0 < count <= len(self.answered_at) else float("inf")


def _limiter(loop: asyncio.AbstractEventLoop, *, pausing: bool) -> TokenBucket:
    if not pausing:  # exactly what recognise_generation built before this change
        return TokenBucket(rate_per_minute=RATE_PER_MINUTE, capacity=CONCURRENCY, clock=loop.time)
    return TokenBucket(
        rate_per_minute=RATE_PER_MINUTE,
        capacity=CONCURRENCY,
        throttle_pause_s=SHAZAM_THROTTLE_PAUSE_S,
        throttle_pause_max_s=SHAZAM_THROTTLE_PAUSE_MAX_S,
        throttle_streak=SHAZAM_THROTTLE_STREAK,
        give_up_after_s=SHAZAM_THROTTLE_GIVE_UP_S,
        clock=loop.time,
    )


async def _sweep(
    loop: asyncio.AbstractEventLoop,
    server: Any,
    *,
    windows: int,
    pausing: bool,
    retry_after: float | None = None,
) -> SweepResult:
    limiter = _limiter(loop, pausing=pausing)
    counts = {"requests": 0, "throttled": 0}
    sent_at: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        now = loop.time()
        counts["requests"] += 1
        sent_at.append(now)
        if server.throttled(now, counts["requests"]):
            counts["throttled"] += 1
            await asyncio.sleep(THROTTLE_LATENCY_S)
            headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
            return httpx.Response(429, text="slow down", headers=headers)
        server.answered(now)
        await asyncio.sleep(ANSWER_LATENCY_S)
        return httpx.Response(200, json={"matches": [], "tagid": "x"})

    async def no_op() -> None:
        return None

    client = InjectedHTTPClient(
        on_attempt=no_op,
        limiter=limiter,
        breaker=CircuitBreaker(),
        transport=httpx.MockTransport(handler),
        url_override="http://shazam.invalid/recognise",
    )
    answered: set[int] = set()
    answered_at: list[float] = []
    errored: list[int] = []
    queue = deque(range(windows))

    async def ask(window: int, max_retries: int) -> bool:
        retry_index = throttles = 0
        while True:
            try:
                await client.request("POST", "http://shazam.invalid/recognise")
                answered.add(window)
                answered_at.append(loop.time())
                return True
            except ShazamHTTPError as exc:
                step = next_attempt_after_failure(
                    exc,
                    limiter,
                    retry_index=retry_index,
                    throttles=throttles,
                    max_retries=max_retries,
                )
                if step is None:
                    return False
                kind, delay = step
                if kind == "throttle":
                    throttles += 1
                else:
                    retry_index += 1
                if delay > 0:
                    await asyncio.sleep(delay)

    async def worker() -> None:
        while queue:
            window = queue.popleft()
            if not await ask(window, MAX_RETRIES):
                errored.append(window)

    await asyncio.gather(*(worker() for _ in range(CONCURRENCY)))
    # The end-of-sweep retry pass: one more ask per errored window, at most 10 % of the mix.
    cap = max(RETRY_PASS_MIN, -(-windows * RETRY_PASS_FRACTION_E2 // 100))
    for window in sorted(errored)[:cap]:
        await ask(window, 0)
    return SweepResult(
        wall_s=loop.time(),
        requests=counts["requests"],
        throttled=counts["throttled"],
        answered=len(answered),
        unanswered=windows - len(answered),
        pauses=limiter.pauses,
        answered_at=answered_at,
    )


def _windows() -> int:
    return int(json.loads(FIXTURE.read_text(encoding="utf-8"))["windows"])


def run_both(server_factory: Any, *, windows: int | None = None) -> tuple[SweepResult, SweepResult]:
    count = windows or _windows()
    old = _simulate(lambda loop: _sweep(loop, server_factory(), windows=count, pausing=False))
    new = _simulate(lambda loop: _sweep(loop, server_factory(), windows=count, pausing=True))
    return old, new


def test_the_replayed_2026_10_21_throttle_wastes_far_fewer_requests_and_answers_every_window() -> (
    None
):
    old, new = run_both(ReplayShazam.from_fixture)
    windows = _windows()
    # The old path reproduces the owner's run (1518 requests, 811 429s, 101 windows lost).
    assert old.throttled > 600 and old.unanswered > 25, old
    # The new path answers EVERY window and sends at most a fifth of the old 429s...
    assert new.unanswered == 0 and new.answered == windows, new
    assert new.throttled * 5 <= old.throttled, (old, new)
    assert new.requests * 10 <= old.requests * 7, (old, new)
    # ...and reaches the old path's final coverage no later than the old path did (+5 %).
    assert new.time_to_answer(old.answered) <= old.wall_s * 1.05, (old, new)


def test_against_a_sliding_quota_the_new_path_answers_everything_and_wastes_little() -> None:
    """The cause, modelled: 200 answers per 30 minutes (429s free).  No client can beat the
    quota, so the gain is coverage and waste, at the same pace."""

    old, new = run_both(QuotaShazam)
    assert old.unanswered > 0 and old.throttled > 300, old
    assert new.unanswered == 0, new
    assert new.throttled * 5 <= old.throttled, (old, new)
    assert new.requests * 10 <= old.requests * 7, (old, new)
    assert new.time_to_answer(old.answered) <= old.wall_s * 1.05, (old, new)


def test_retry_after_is_honoured_by_the_whole_pool() -> None:
    """A 429 with ``Retry-After: 120`` stops EVERY worker for two minutes, not just one."""

    sent_after_throttle: list[float] = []

    @dataclass
    class OnceThrottled:
        first: float | None = None

        def throttled(self, now: float, sent: int) -> bool:
            if sent == 4:
                self.first = now
                return True
            if self.first is not None:
                sent_after_throttle.append(now - self.first)
            return False

        def answered(self, now: float) -> None:
            del now

    result = _simulate(
        lambda loop: _sweep(loop, OnceThrottled(), windows=12, pausing=True, retry_after=120.0)
    )
    assert result.unanswered == 0 and result.throttled == 1, result
    # Requests already in flight may land, but nothing new is SENT inside the 120 s.
    late = [delay for delay in sent_after_throttle if delay > 2.0]
    assert late and min(late) >= 120.0, sent_after_throttle


def test_a_pool_that_never_hears_back_gives_up_without_sending_more() -> None:
    """Shazam blocked for good: after the give-up period every remaining window fails at once
    and nothing more is sent (the old path sent six 429s per window, all of them)."""

    @dataclass
    class AlwaysThrottled:
        def throttled(self, now: float, sent: int) -> bool:
            return True

        def answered(self, now: float) -> None:
            del now

    old = _simulate(lambda loop: _sweep(loop, AlwaysThrottled(), windows=200, pausing=False))
    new = _simulate(lambda loop: _sweep(loop, AlwaysThrottled(), windows=200, pausing=True))
    assert old.requests >= 200 * (MAX_RETRIES + 1)
    # One lone probe a minute or so for the give-up hour, then nothing: ~70 requests, not 1200.
    assert new.unanswered == 200 and new.requests < 100, new
    assert new.wall_s <= SHAZAM_THROTTLE_GIVE_UP_S + 3 * SHAZAM_THROTTLE_PAUSE_MAX_S, new


def test_the_pause_is_off_for_other_callers_of_the_token_bucket() -> None:
    """The paid AudD sweep and direct callers keep the plain AIMD bucket: no pause, no probe."""

    bucket = TokenBucket(rate_per_minute=60, capacity=2)
    bucket.penalize(retry_after=600.0)
    assert not bucket.pauses_on_throttle and bucket.pauses == 0
    assert not bucket.gave_up()

    async def admitted_at_once() -> bool:
        await asyncio.wait_for(bucket.acquire(), timeout=1.0)
        return True

    assert asyncio.run(admitted_at_once())


def test_a_throttled_probe_doubles_the_pause_and_an_answer_ends_it() -> None:
    now = [0.0]
    bucket = TokenBucket(
        rate_per_minute=45,
        capacity=3,
        throttle_pause_s=10.0,
        throttle_pause_max_s=60.0,
        throttle_streak=2,
        clock=lambda: now[0],
    )
    bucket.penalize()  # an isolated throttle only cuts the rate
    assert bucket.pauses == 0 and bucket.rate_per_second == 0.375
    bucket.recover()
    bucket.penalize()
    now[0] = 0.4
    bucket.penalize()  # two in a row with no answer between: the pool pauses
    assert bucket.pauses == 1
    first = bucket.paused_until - now[0]
    now[0] = 1.0
    bucket.penalize()  # an in-flight sibling: the pause is not lengthened
    assert bucket.pauses == 1 and bucket.paused_until - 0.4 == first
    pauses = [first]
    for _ in range(6):
        now[0] = bucket.paused_until + 0.5  # the lone probe sent after the pause: throttled
        bucket.penalize()
        pauses.append(bucket.paused_until - now[0])
    nominal = [10, 20, 40, 60, 60, 60, 60]
    assert all(0.75 * n <= p <= 1.25 * n for p, n in zip(pauses, nominal, strict=True)), pauses
    assert len({round(p, 3) for p in pauses[3:]}) == 4  # jittered: never in step with a period
    bucket.recover()
    assert bucket.throttle_level == 0 and not bucket.gave_up()
    now[0] = bucket.paused_until + 1.0
    bucket.penalize()
    assert bucket.paused_until < now[0]  # a fresh isolated throttle does not pause again


def test_only_one_probe_is_in_flight_while_paused() -> None:
    async def scenario(loop: asyncio.AbstractEventLoop) -> list[float]:
        bucket = TokenBucket(
            rate_per_minute=6_000, capacity=3, throttle_pause_s=10.0, clock=loop.time
        )
        bucket.penalize()
        admitted: list[float] = []

        async def take() -> None:
            await bucket.acquire()
            admitted.append(loop.time())

        tasks = [asyncio.ensure_future(take()) for _ in range(3)]
        await asyncio.sleep(30.0)
        assert len(admitted) == 1  # the probe; the others wait for its outcome
        bucket.settle()
        bucket.recover()  # the probe was answered: the pool resumes
        await asyncio.gather(*tasks)
        return admitted

    admitted = _simulate(scenario)
    assert len(admitted) == 3 and admitted[0] >= 7.5 and admitted[1] >= 30.0, admitted


def test_the_real_recognise_path_waits_out_a_block_and_answers_every_window(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """End to end through ``recognise_generation`` with its OWN default limiter: Shazam refuses
    the first 25 requests (a block, like 2026-10-21's), then answers.  Every window ends answered,
    after pauses, with no window's retries used up.  The old path spent six tries per window and
    one end-of-sweep retry inside the block and left every window an error."""

    from id_detector import recognise as recognise_module
    from id_detector.recognise import load_provider_config, recognise_generation_zero
    from id_detector.shazam import ShazamAdapter
    from id_detector.windows import generate_windows_async
    from tests.test_stage1_windows import _decoded

    match = json.loads((FIXTURE.parent / "response-match.json").read_text(encoding="utf-8"))
    calls: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        calls.append(1)
        if len(calls) <= 25:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json=match)

    built: list[ShazamAdapter] = []

    class _Offline(ShazamAdapter):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.transport = httpx.MockTransport(handler)  # never the network
            built.append(self)

    monkeypatch.setattr(recognise_module, "ShazamAdapter", _Offline)
    monkeypatch.setattr(recognise_module, "SHAZAM_THROTTLE_PAUSE_S", 0.01)
    monkeypatch.setattr(recognise_module, "SHAZAM_THROTTLE_PAUSE_MAX_S", 0.02)
    monkeypatch.setattr(recognise_module, "retry_delay", lambda *_: 0)

    async def scenario() -> Any:
        decoded, _, media_dir = _decoded(tmp_path, 30_000)
        windows = await generate_windows_async(decoded, media_dir)
        load_provider_config(tmp_path)
        return windows, await recognise_generation_zero(
            media_key="a" * 64,
            media_dir=media_dir,
            windows=windows,
            project_root=tmp_path,
            run_id="blocked-then-answered",
            max_requests=100,
            requests_per_minute=600_000,
        )

    windows, result = asyncio.run(scenario())
    assert built and built[0].limiter.pauses_on_throttle  # the default path pauses on 429s
    assert built[0].limiter.pauses >= 4
    assert result.failures == 0, result
    assert len(calls) == len(windows.records) + 25
    assert all(item.status == "match" for item in result.observations)

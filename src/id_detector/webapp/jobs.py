"""In-process background-thread job manager for the browser-driven web app.

The present server is a :class:`http.server.ThreadingHTTPServer`, so this manager runs analyse
jobs on a **single** dedicated worker thread (one job at a time — a queue — to respect the Shazam
rate limit).  Every job records live progress, a bounded ring-buffer log, a terminal status and the
path of the finished ``present/index.html``; state lives in memory keyed by job id so a page reload
just re-reads it.  Jobs are cancellable, and :meth:`JobManager.shutdown` joins the worker so tearing
the server down never leaks a hung thread.

Privacy: the manager only ever stores what it is given, and it runs the submitted target and every
log line through :func:`id_detector.io.redact_text`.  No provider secrets, usernames or comment text
enter job state — the runner emits only coarse phase messages.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from id_detector.io import redact_text, url_has_credentials
from id_detector.run_ledger import new_run_id

#: Ring-buffer size for a job's human-readable log tail.
LOG_RING = 200
#: Cold-start prior for the progress page's estimate, used only until a *recent* rate has been
#: observed.  It is deliberately the slow end of what the adaptive limiter does (it starts near
#: 45 windows/min and drops toward 20/min when Shazam throttles), so the early bar under-promises
#: instead of racing ahead on a guess.
SHAZAM_RATE_PER_MINUTE = 18
#: The bar's rate is **recent**: windows completed over roughly the last this-many seconds, never
#: the run average.  Two reasons.  A cache resume completes many windows in one instant, which would
#: inflate an average for the rest of the run; and the limiter is adaptive, so the rate that
#: predicts the *remaining* time is the one being achieved now, not the one achieved earlier.
RECENT_RATE_SECONDS = 45.0
#: A recent rate is trusted once it spans this much listening time with at least this many windows
#: — or once so many windows have completed that the speed is beyond doubt however short the span
#: (a warm run that finishes its recognise pass in seconds).  Before that the page says it is still
#: estimating rather than showing a number built on two or three ticks.
_RATE_MIN_WINDOWS = 3
_RATE_MIN_SECONDS = 15.0
_RATE_SURE_WINDOWS = 30
#: Window ticks closer together than this are folded into one sample, which bounds the sample list
#: (and the snapshot that carries it) to about one entry per second of the sliding window.
_SAMPLE_SPACING_SECONDS = 1.0
#: The displayed bar may climb at most this many times faster than its natural slope (100 % over
#: the expected total time).  When an estimate improves — the early prior gives way to a measured
#: rate, or Shazam stops throttling — the bar catches up over a few seconds instead of leaping.
_MAX_CATCH_UP = 3.0
#: ...and that natural slope is never taken to be steeper than a job of this many seconds.  When an
#: estimate *collapses* — every window came back from cache, or listening ended while the estimate
#: still rested on the prior — the expected total can shrink to a few seconds, and "three times the
#: natural slope" of a three-second job is any jump at all.  With the floor, no poll of a running
#: job moves the bar by more than ``3 × 100 / 120 = 2.5`` points per second (about six points per
#: 2.5 s poll), however short the job turns out to be.  A job that finishes while the bar is still
#: behind hands over to the finished state directly: done is done, and the page swaps the
#: percentage for the result at that moment rather than animating a number that is already known.
_SMOOTHING_FLOOR_SECONDS = 120.0
#: Expected wall-clock **seconds** for each phase of a cold 60-minute run — the denominator behind
#: the progress bar (U-F9: "10 % of the bar ≈ a tenth of the expected wall time").  Recognise
#: dominates by an order of magnitude and is replaced by the *observed*-rate estimate the moment
#: one exists, so these constants only matter before a phase has been measured.  A phase that does
#: not run in this job (no reference index, no acquisition) is dropped from the denominator, and a
#: phase that has already finished contributes the time it really took — so a cached ingest or a
#: warm recognise pass shrinks the total instead of holding a tenth of the bar hostage.
PHASE_EXPECTED_SECONDS: dict[str, float] = {
    "build_index": 120.0,
    "ingest": 60.0,
    "decode": 30.0,
    "windows": 15.0,
    "recognise": 1_200.0,
    "hints": 10.0,
    "fuse": 20.0,
    "enrich": 15.0,
    "present": 5.0,
}
#: Pipeline order of the phases above (dict order is the pipeline order).
PHASE_SEQUENCE: tuple[str, ...] = tuple(PHASE_EXPECTED_SECONDS)
#: ``InvocationTimer`` stage keys in pipeline order, and the plain step each one finished.  Lets a
#: failed run say how far it actually got without naming an internal phase key (U-F15/F31).  Shared
#: by the live progress page and the durable failure card, so both tell the same story.
STAGE_LABELS: tuple[tuple[str, str], ...] = (
    ("ingest_ms", "fetching the mix"),
    ("decode_ms", "preparing the audio"),
    ("windows_ms", "preparing the scan"),
    ("recognise_ms", "listening to the set"),
    ("hints_ms", "reading tracklist clues"),
    ("fuse_ms", "lining up the tracks"),
    ("refuse_ms", "lining up the tracks"),
    ("secondary_ms", "double-checking the matches"),
    ("index_scan_ms", "checking the reference index"),
    ("export_ms", "writing the page"),
)
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
#: Plan §2.3.5: the Shazam breaker is open, so this request never started analysing.  It is not a
#: failure — nothing went wrong with the mix — and local mode has no queue to retry it from, so the
#: job stops here and the page says so.  Polling treats it as terminal.
WAITING = "waiting"
TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED, WAITING})


class JobWaiting(RuntimeError):
    """The runner refused this job because Shazam is paused (breaker open or kill-switch off).

    Distinct from every other runner error: the job becomes :data:`WAITING`, never ``failed``.
    """


class JobCancelled(asyncio.CancelledError):
    """Raised inside the pipeline (from a progress tick) to abort a running job.

    It subclasses :class:`asyncio.CancelledError` so ``_analyse``'s cancellation handler records a
    clean ``cancelled`` invocation and unwinds its locks/DB owner before the worker sees it.
    """


class TargetValidationError(ValueError):
    """The submitted analyse target is neither an http(s) URL nor an existing local file."""


def validate_target(raw: str) -> str:
    """Validate and normalise a submitted analyse target (server-side).

    Accepts an ``http``/``https`` mix URL (rejecting credential-bearing ones) or a local file the
    owner passes (a ``file://`` URI or an existing path).  Everything else — ``javascript:``,
    ``data:``, ``ftp:``, an unknown scheme, a non-existent path — is refused.
    """

    text = (raw or "").strip()
    if not text:
        raise TargetValidationError("a mix URL or local file is required")
    parts = urlsplit(text)
    scheme = parts.scheme.casefold()
    if scheme in {"http", "https"}:
        if not parts.netloc:
            raise TargetValidationError("the URL is missing a host")
        if url_has_credentials(text):
            raise TargetValidationError("credential-bearing URLs are not accepted")
        return text
    if scheme == "file":
        return text
    # Anything else must be a local file the owner passes.  A Windows path like ``C:\mix.wav``
    # parses with a single-letter scheme, so accept any non-web target that names an existing file.
    if Path(text).is_file():
        return text
    raise TargetValidationError("only an http(s) URL or a local file is accepted")


@dataclass
class Job:
    """One submitted analysis, its live progress, and its result — all in memory."""

    id: str
    target: str
    display: str
    profile: str | None
    acquire: bool
    build_index: bool
    #: An optional tracklist the user pasted (e.g. from 1001tracklists / a YouTube description) to
    #: seed positioned hints — the same corroboration/recovery path as the CLI's ``--tracklist``.
    known_tracklist: str | None = None
    #: The ONE service ``run_id`` this job analyses under, minted when the job is created and
    #: reused by every execution of it — a restart after the worker process died resumes the same
    #: run (its checkpoints, attempt ledger, reservation and settlement) instead of starting a new
    #: one that would re-bill the clips the first pass already paid for.
    run_id: str | None = None
    status: str = QUEUED
    phase: str = QUEUED
    phase_done: int = 0
    phase_total: int = 0
    windows_done: int = 0
    windows_total: int = 0
    message: str = ""
    error: str | None = None
    result_path: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    recognise_started_at: float | None = None
    resolved_title: str | None = None
    failed_phase: str | None = None
    #: Measured wall-clock seconds per phase, accumulated as each phase is left (a phase the
    #: pipeline re-enters — a second ``fuse`` for the cross-check re-fuse — accumulates).  This is
    #: what makes the bar wall-clock rather than step-index arithmetic: finished phases contribute
    #: what they really cost, not what they were budgeted.
    phase_seconds: dict[str, float] = field(default_factory=dict)
    #: Working seconds spent by EARLIER attempts of this job (a durable retry).  Kept apart from
    #: :attr:`phase_seconds` on purpose: that mapping also says which phases THIS attempt has
    #: finished, and an attempt that died mid-recognition has not finished recognising — counting
    #: its time as a completed phase is what would put a retry's bar at 97 % during intake.
    carried_seconds: float = 0.0
    #: When the phase named by :attr:`phase` began, so time *inside* it is measured too.
    phase_started_at: float | None = None
    #: Highest percentage this job has ever reported: the bar must never move backwards, even if a
    #: growing ETA would otherwise pull the computed value down.
    progress_max: int = 0
    #: The same high-water mark unrounded, and when it was last evaluated.  The bar is rate-limited
    #: (:data:`_MAX_CATCH_UP`), and on a long run one poll moves it by a fraction of a point, so the
    #: fraction has to survive between evaluations — and between the worker process that publishes
    #: this snapshot and the web process that re-evaluates it at request time.
    progress_value: float = 0.0
    progress_at: float | None = None
    #: ``[time, windows_done]`` samples of the current recognise pass, pruned to the sliding window
    #: (plus one older anchor).  The first sample of a pass already contains every window the cache
    #: completed instantly, so rate differences never count that burst as observed speed.  Rebound,
    #: never mutated in place, for the same cross-thread reason as :attr:`phase_seconds`.
    rate_samples: list[list[float]] = field(default_factory=list)
    #: The frozen §2.3.5 outcome of the run behind a terminal job, read back from its journal:
    #: the status (``failed``/``provider_unavailable``/``budget_exhausted``/``source_changed``/
    #: ``cancelled``), the machine reason, the last pipeline stage that completed, and the money
    #: that was really settled.  ``spend_known`` is False only when the journal could not be read,
    #: which is the one case the UI is allowed to hedge about cost.
    run_status: str | None = None
    run_reason: str | None = None
    last_stage: str | None = None
    usd_e2_spent: int | None = None
    spend_known: bool = False
    #: The canonical projection's own figures for the finished result — the count the tracklist
    #: actually lists.  Never derived from a log line (U-F2/F3: one filtered entry list).
    tracks_found: int | None = None
    crowd_tracks: int = 0
    #: The fetched mix on disk (``ingest/original.*``) once ingest has completed and the
    #: server has resolved it — what the analysing page plays while the engines run.
    audio_path: str | None = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_RING))
    cancel_event: threading.Event = field(default_factory=threading.Event)
    #: Queue-aware guards the supervised local worker attaches for ONE execution: the dispatch
    #: admission check and the settlement fence. Runtime objects, never snapshotted.
    dispatch_admission: Any = field(default=None, repr=False, compare=False)
    settlement_writer: Any = field(default=None, repr=False, compare=False)

    def recent_rate_per_minute(self, now: float | None = None) -> float | None:
        """Windows per minute over the sliding window, or ``None`` before there is enough data.

        Measured from the oldest kept sample towards *now*, not just to the last tick: while
        Shazam stalls and no window completes, the span keeps growing and the rate decays, so the
        estimate lengthens honestly instead of freezing at the last good speed.
        """

        samples = self.rate_samples
        if len(samples) < 2:
            return None
        first, last = samples[0], samples[-1]
        if self.finished_at is not None:
            end = self.finished_at
        else:
            end = time.time() if now is None else now
        ticked = last[0] - first[0]
        windows = last[1] - first[1]
        if ticked <= 0 or windows < _RATE_MIN_WINDOWS:
            return None
        # Between two ticks the next window is simply on its way, so one average gap of silence
        # is not evidence of slowing; only silence beyond that stretches the span.
        span = max(ticked, end - first[0] - ticked / windows)
        if span < _RATE_MIN_SECONDS and windows < _RATE_SURE_WINDOWS:
            return None
        return windows * 60.0 / span

    def rate_per_minute(self, now: float | None = None) -> float:
        """The rate the estimate uses: the recent observed one, else the cautious prior."""

        recent = self.recent_rate_per_minute(now)
        return float(SHAZAM_RATE_PER_MINUTE) if recent is None else recent

    def _recognise_rest_seconds(self, now: float | None = None) -> float:
        """Seconds of listening still to come: the windows left at the recent rate."""

        remaining = max(0, self.windows_total - self.windows_done)
        if not remaining or self.status in TERMINAL_STATES:
            return 0.0
        return remaining / self.rate_per_minute(now) * 60.0

    def estimating(self, now: float | None = None) -> bool:
        """True while the time estimate still rests on the prior instead of a measured rate.

        The page says "Estimating…" rather than putting words to a guess.  Once listening has
        been measured — or is over, leaving only the short closing steps — the estimate is real.
        """

        if self.status in TERMINAL_STATES:
            return False
        if self.status != RUNNING:
            return True
        if "recognise" in self.phase_seconds and self.phase != "recognise":
            return False
        if self.phase == "recognise" and 0 < self.windows_total <= self.windows_done:
            return False
        return self.recent_rate_per_minute(now) is None

    def eta_seconds(self, now: float | None = None) -> int:
        """Expected seconds until the whole job finishes — the same estimate the bar divides by."""

        if self.status != RUNNING:
            return 0
        now = time.time() if now is None else now
        started = self.phase_started_at
        current = max(0.0, now - started) if started is not None else 0.0
        return int(round(self._remaining_seconds(current, now)))

    # -- wall-clock progress (U-F9) -------------------------------------------------------------
    def _phase_runs(self, phase: str) -> bool:
        """Whether this job runs ``phase`` at all — a skipped phase is not part of the total."""

        if phase == "build_index":
            return self.build_index
        if phase == "enrich":
            return self.acquire
        return True

    def _speed_factor(self) -> float:
        """How much faster (or slower) this job is running than the cold-run constants predict.

        A run served from cache finishes ingest/decode/windows in a fraction of a cold run's time,
        and the phases *after* recognise are cached in the same way — so leaving them at their full
        cold expectation is what pins a warm run's bar near zero until it abruptly finishes.  The
        ratio of measured to predicted time over the phases already done is the best available
        estimate for the ones still to come; it is clamped so one freak phase cannot dominate.
        """

        measured = spent = 0.0
        for phase, seconds in self.phase_seconds.items():
            budget = PHASE_EXPECTED_SECONDS.get(phase)
            if budget and phase != "recognise":
                measured += seconds
                spent += budget
        if spent <= 0:
            return 1.0
        return min(4.0, max(0.05, measured / spent))

    def _expected_seconds(self, phase: str, now: float | None = None) -> float:
        """Expected wall-clock seconds for one phase of *this* job.

        A phase already measured contributes what it really took (a cached ingest costs what the
        cache cost, not a cold download).  Recognise prefers the observed window rate, which is the
        only estimate that survives a warm cache, a changed rate limit or added concurrency; every
        other unmeasured phase is scaled by how fast this run has actually been going.
        """

        measured = self.phase_seconds.get(phase)
        if measured is not None:
            return measured
        if phase == "recognise":
            if self.windows_total > 0:
                # Not entered yet in this attempt.  A fresh job has every window ahead of it; a
                # retry knows how many the earlier attempt finished, and those come back from
                # cache in an instant, so only the rest is waiting.
                left = max(0, self.windows_total - self.windows_done)
                return left / self.rate_per_minute(now) * 60.0
            return PHASE_EXPECTED_SECONDS["recognise"]
        return PHASE_EXPECTED_SECONDS.get(phase, 0.0) * self._speed_factor()

    def _remaining_seconds(self, current_elapsed: float, now: float) -> float:
        """Expected seconds still to come: the rest of this phase plus every phase after it."""

        phases = [phase for phase in PHASE_SEQUENCE if self._phase_runs(phase)]
        if self.phase in phases:
            index = phases.index(self.phase)
            if self.phase == "recognise":
                rest = self._recognise_rest_seconds(now)
            else:
                rest = max(0.0, self._expected_seconds(self.phase, now) - current_elapsed)
        else:
            # "starting", or an auxiliary phase the tracker does not list (the local-index scan):
            # place it after the last phase that has already been measured.
            measured = [phases.index(name) for name in phases if name in self.phase_seconds]
            index, rest = (max(measured) if measured else -1), 0.0
        return rest + sum(self._expected_seconds(phase, now) for phase in phases[index + 1 :])

    def progress_percent(self, now: float | None = None) -> int:
        """Percent of the job's expected **wall-clock** time that has elapsed (U-F9).

        ``elapsed / (elapsed + remaining)``: if the job will take ten minutes, every 10 % is about
        a minute.  ``elapsed`` is real time only, so windows a cache resume completed in an instant
        add nothing to it; ``remaining`` is the windows left at the **recent** rate plus the short
        closing steps.  Three rules keep the number steady as well as honest: it never moves
        backwards (when the rate drops and the estimate lengthens, the bar holds and time catches
        up), it never climbs faster than :data:`_MAX_CATCH_UP` times its natural slope (so a better
        estimate is approached over seconds, not leapt to), and it stays below 100 until the job
        has actually succeeded.
        """

        if self.status == SUCCEEDED:
            self.progress_max = 100
            return 100
        if self.status == QUEUED:
            return self.progress_max
        now = time.time() if now is None else now
        started = self.phase_started_at
        current = max(0.0, now - started) if started is not None else 0.0
        elapsed = self.carried_seconds + sum(self.phase_seconds.values()) + current
        if self.status in TERMINAL_STATES:
            return self.progress_max  # failed/cancelled/waiting: freeze where it stopped
        total = elapsed + self._remaining_seconds(current, now)
        value = elapsed * 100.0 / total if total > 0 else 0.0
        floor = max(self.progress_value, float(self.progress_max))
        # Rate-limit the climb from the last evaluation — or, the first time, from the moment the
        # job started, so a first poll cannot leap either.  Only a job with neither (one rebuilt
        # from a document older than these fields) takes the computed value as it is.
        since = self.progress_at if self.progress_at is not None else self.started_at
        if since is not None:
            waited = max(0.0, now - since)
            pace = max(total, _SMOOTHING_FLOOR_SECONDS)
            value = min(value, floor + _MAX_CATCH_UP * 100.0 * waited / pace)
        self.progress_value = max(floor, min(99.0, max(0.0, value)))
        self.progress_at = now
        self.progress_max = max(self.progress_max, int(self.progress_value))
        return self.progress_max

    def status_dict(self) -> dict[str, Any]:
        """A JSON-safe snapshot for ``GET /jobs/<id>/status`` — never contains a secret."""

        return {
            "id": self.id,
            "display": self.display,
            "profile": self.profile,
            "acquire": self.acquire,
            "build_index": self.build_index,
            "status": self.status,
            "phase": self.phase,
            "phase_done": self.phase_done,
            "phase_total": self.phase_total,
            "windows_done": self.windows_done,
            "windows_total": self.windows_total,
            # The whole job's remaining time (the bar's own denominator), and whether it is still
            # a guess: the page words a guess as "Estimating…", never as a number.
            "eta_seconds": self.eta_seconds(),
            "eta_estimating": self.estimating(),
            "rate_per_minute": round(self.rate_per_minute(), 1),
            "message": self.message,
            "error": self.error,
            "result_url": ("/" + self.result_path) if self.result_path else None,
            "audio_url": f"/jobs/{self.id}/audio" if self.audio_path else None,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "recognise_started_at": self.recognise_started_at,
            "finished_at": self.finished_at,
            "title": self.resolved_title,
            "failed_phase": self.failed_phase,
            # Wall-clock progress is computed here, once, so the progress page and the home cards
            # cannot drift into two different bars (and so it is testable without a browser).
            "progress_pct": self.progress_percent(),
            # The §2.3.5 outcome and the money that was really settled, so the failure UI can state
            # the cost instead of hedging (U-F15).
            "run_status": self.run_status,
            "run_reason": self.run_reason,
            "last_stage": self.last_stage,
            "usd_e2_spent": self.usd_e2_spent,
            "spend_known": self.spend_known,
            # The canonical projection's count for the finished result.
            "tracks_found": self.tracks_found,
            "crowd_tracks": self.crowd_tracks,
            "terminal": self.status in TERMINAL_STATES,
            "log": list(self.log),
        }


def _strip_extended_prefix(path: Path) -> Path:
    r"""Drop the Windows extended-length (``\\?\``) prefix ``Path.resolve()`` adds to deep paths.

    Deeply-nested ``work/<keys>/…`` result paths exceed 260 chars, so ``resolve()`` returns the
    ``\\?\`` form on one side but not the short work-root — which made ``relative_to`` raise and
    silently lose the result link.  Stripping it from both sides fixes that; a no-op elsewhere.
    """

    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + text[len("\\\\?\\UNC\\") :])
    if text.startswith("\\\\?\\"):
        return Path(text[len("\\\\?\\") :])
    return path


class JobContext:
    """The handle a runner uses to report progress, log, check cancellation, and set the result."""

    def __init__(
        self, manager: JobManager, job: Job, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._manager = manager
        self._job = job
        self._clock = clock
        self._last_phase: str | None = None
        self._last_logged: tuple[str, str] | None = None

    @property
    def target(self) -> str:
        return self._job.target

    @property
    def profile(self) -> str | None:
        return self._job.profile

    @property
    def acquire(self) -> bool:
        return self._job.acquire

    @property
    def build_index(self) -> bool:
        return self._job.build_index

    @property
    def known_tracklist(self) -> str | None:
        return self._job.known_tracklist

    @property
    def run_id(self) -> str | None:
        """The job's durable service run id (``None`` only for a job built without one)."""

        return self._job.run_id

    @property
    def dispatch_admission(self) -> Any:
        return self._job.dispatch_admission

    @property
    def settlement_writer(self) -> Any:
        return self._job.settlement_writer

    @property
    def work_root(self) -> Path:
        return self._manager.work_root

    @property
    def started_at(self) -> float | None:
        """When this attempt began — used to tell this run's journal entry from an earlier one."""

        return self._job.started_at

    @property
    def cancel_token(self) -> threading.Event:
        """The job's cancel flag, polled by the paid sweep before every dispatch (0b-iii).

        A progress tick raises to cancel; the token lets a sweep with several clips in flight
        stop *dispatching* while those clips resolve, so their spend is recorded, not lost.
        """

        return self._job.cancel_event

    def check_cancel(self) -> None:
        if self._job.cancel_event.is_set():
            raise JobCancelled(self._job.id)

    def progress(self, phase: str, done: int, total: int, message: str = "") -> None:
        """Record a phase tick.  Every tick is also a cancellation point."""

        self.check_cancel()
        safe = redact_text(message) if message else ""
        with self._manager.lock:
            now = self._clock()
            entered = phase != self._job.phase
            if entered:
                # Leaving a phase freezes its *measured* duration, which is what the wall-clock bar
                # divides by from then on (U-F9).
                if self._job.phase_started_at is not None and self._job.phase:
                    spent = max(0.0, now - self._job.phase_started_at)
                    previous = self._job.phase
                    # Rebind a NEW dict rather than mutating in place: the status route
                    # reads ``phase_seconds`` from another thread without this lock, and a
                    # reader summing a dict that grows underneath it raises.  Rebinding
                    # means a reader sees the old mapping or the new one, never one in flux.
                    self._job.phase_seconds = {
                        **self._job.phase_seconds,
                        previous: self._job.phase_seconds.get(previous, 0.0) + spent,
                    }
                self._job.phase_started_at = now
            self._job.phase = phase
            self._job.phase_done = done
            self._job.phase_total = total
            self._job.message = safe
            if phase == "recognise":
                # A new pass — the phase was just entered, the total changed, or the count went
                # back — starts its samples afresh, so its first sample is its own baseline: the
                # windows a cache resume completed instantly are in it, not measured as speed.
                fresh = entered or total != self._job.windows_total or done < self._job.windows_done
                self._job.rate_samples = _with_sample(
                    [] if fresh else self._job.rate_samples, now, done
                )
                self._job.windows_done = done
                self._job.windows_total = total
                if self._job.recognise_started_at is None:
                    self._job.recognise_started_at = now
            if phase == "ingest" and total > 0 and done >= total and safe:
                self._job.resolved_title = safe
            # Log a phase when it starts and again when it completes with a new message, so the
            # outcome of each phase ("ingest: <set title>", "windows: 212 windows",
            # "fuse: 41 episodes") reaches the progress page — not just "started".
            completed = total > 0 and done >= total and bool(safe)
            if (phase != self._last_phase or completed) and (phase, safe) != self._last_logged:
                self._job.log.append(f"{_stamp()} {phase}: {safe or 'started'}")
                self._last_logged = (phase, safe)
        self._last_phase = phase

    def log(self, message: str) -> None:
        with self._manager.lock:
            self._job.log.append(f"{_stamp()} {redact_text(message)}")

    def set_result(self, index_html: Path) -> None:
        try:
            relative = (
                _strip_extended_prefix(index_html.resolve())
                .relative_to(_strip_extended_prefix(self.work_root.resolve()))
                .as_posix()
            )
        except ValueError:
            return
        # The count the finished page shows comes from the run's own canonical projection, read
        # back from the bundle it just published — never from a log line (U-F2/F3).
        summary = None
        try:
            from id_detector.present.exports import read_projected_summary

            summary = read_projected_summary(index_html.parent / "tracklist.json")
        except (ImportError, OSError, ValueError):  # pragma: no cover - defensive
            summary = None
        with self._manager.lock:
            self._job.result_path = relative
            if summary is not None:
                self._job.tracks_found = summary.tracks
                self._job.crowd_tracks = summary.crowd

    def set_outcome(
        self,
        *,
        run_status: str | None = None,
        run_reason: str | None = None,
        last_stage: str | None = None,
        usd_e2_spent: int | None = None,
        spend_known: bool = False,
    ) -> None:
        """Record a terminal run's frozen §2.3.5 outcome and its settled spend (U-F15).

        Without this the UI can only guess from the *requested* profile, which is how a run that
        spent nothing ends up hedging that paid checks "may still count".
        """

        with self._manager.lock:
            self._job.run_status = run_status
            self._job.run_reason = run_reason
            self._job.last_stage = last_stage
            self._job.usd_e2_spent = usd_e2_spent
            self._job.spend_known = spend_known


def _with_sample(samples: list[list[float]], now: float, done: int) -> list[list[float]]:
    """A NEW sample list with ``[now, done]`` added, folded and pruned to the sliding window.

    Ticks less than :data:`_SAMPLE_SPACING_SECONDS` apart replace the newest sample instead of
    growing the list (the first sample, the pass's baseline, is never replaced).  Samples older
    than the window are dropped and replaced by one interpolated sample at the window's far edge,
    so the measured span is the full window — and never more than the window.
    """

    kept = [list(sample) for sample in samples]
    if len(kept) >= 2 and kept[-1][0] - kept[-2][0] < _SAMPLE_SPACING_SECONDS:
        kept.pop()
    kept.append([float(now), float(done)])
    horizon = now - RECENT_RATE_SECONDS
    older = [sample for sample in kept if sample[0] < horizon]
    inside = [sample for sample in kept if sample[0] >= horizon]
    if not older:
        return inside
    # The far edge is a sample *at* the horizon, interpolated between the last sample before it
    # and the first one after.  Keeping the old sample itself would let the window stretch to
    # however long ago that was: after a two-minute stall, work resuming at full speed would be
    # averaged with the stall for another whole window and the rate would read a fraction of the
    # truth.  Interpolated, the window is never wider than RECENT_RATE_SECONDS at a tick, so the
    # rate is back to the live one exactly one window after the work resumes.
    before, after = older[-1], inside[0]
    share = (horizon - before[0]) / (after[0] - before[0])
    return [[horizon, before[1] + (after[1] - before[1]) * share], *inside]


#: A runner takes a :class:`JobContext` and runs the work, raising to fail (or ``JobCancelled``).
Runner = Callable[[JobContext], None]


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


class JobManager:
    """A single-worker, one-at-a-time queue of analyse jobs kept entirely in memory."""

    def __init__(self, work_root: Path, runner: Runner, *, max_recent: int = 50) -> None:
        self.work_root = Path(work_root)
        self._runner = runner
        self._max_recent = max_recent
        self.lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._queue: deque[str] = deque()
        self._worker: threading.Thread | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()

    # -- submission / inspection ---------------------------------------------------------------
    def submit(
        self,
        target: str,
        profile: str | None = None,
        *,
        acquire: bool = False,
        build_index: bool = False,
        known_tracklist: str | None = None,
    ) -> str:
        validated = validate_target(target)
        job = Job(
            id=uuid.uuid4().hex,
            target=validated,
            display=redact_text(validated),
            profile=profile,
            acquire=acquire,
            build_index=build_index,
            known_tracklist=known_tracklist,
            run_id=new_run_id(),
        )
        with self.lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._queue.append(job.id)
            self._prune_locked()
            self._ensure_worker_locked()
        self._wake.set()
        return job.id

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 25) -> list[Job]:
        with self.lock:
            ids = list(reversed(self._order))[:limit]
            return [self._jobs[job_id] for job_id in ids if job_id in self._jobs]

    def cancel(self, job_id: str) -> bool:
        with self.lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in TERMINAL_STATES:
                return False
            job.cancel_event.set()
            if job.status == QUEUED:
                # A queued job never reaches the runner: mark it terminal now for a snappy UI. The
                # worker loop skips any cancelled id it pops.
                job.status = CANCELLED
                job.phase = CANCELLED
                job.finished_at = time.time()
                job.log.append(f"{_stamp()} cancelled before it started")
        self._wake.set()
        return True

    def dismiss(self, job_id: str) -> bool:
        """Remove one terminal local-user job card; running work is never affected."""

        with self.lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in TERMINAL_STATES:
                return False
            self._jobs.pop(job_id, None)
            if job_id in self._order:
                self._order.remove(job_id)
            return True

    # -- lifecycle -----------------------------------------------------------------------------
    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the worker and join it — cancelling any in-flight job so teardown never hangs."""

        self._stop.set()
        with self.lock:
            for job in self._jobs.values():
                if job.status in {QUEUED, RUNNING}:
                    job.cancel_event.set()
            worker = self._worker
        self._wake.set()
        if worker is not None:
            worker.join(timeout=timeout)

    # -- internals -----------------------------------------------------------------------------
    def _prune_locked(self) -> None:
        while len(self._order) > self._max_recent:
            oldest = self._order[0]
            job = self._jobs.get(oldest)
            if job is not None and job.status not in TERMINAL_STATES:
                break  # never drop a queued/running job
            self._order.pop(0)
            self._jobs.pop(oldest, None)

    def _ensure_worker_locked(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._stop.clear()
            self._worker = threading.Thread(target=self._run_loop, name="webapp-jobs", daemon=True)
            self._worker.start()

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            job_id = self._next_job()
            if job_id is None:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            self._execute(job_id)

    def _next_job(self) -> str | None:
        with self.lock:
            while self._queue:
                candidate = self._queue.popleft()
                job = self._jobs.get(candidate)
                if job is None or job.cancel_event.is_set():
                    continue
                return candidate
        return None

    def _execute(self, job_id: str) -> None:
        with self.lock:
            job = self._jobs[job_id]
            job.status = RUNNING
            job.started_at = time.time()
            job.phase = "starting"
            job.phase_started_at = job.started_at
            job.log.append(f"{_stamp()} started")
        ctx = JobContext(self, job)
        outcome = SUCCEEDED
        error: str | None = None
        try:
            self._runner(ctx)
        except asyncio.CancelledError:
            outcome = CANCELLED
        except JobWaiting as exc:
            outcome = WAITING
            error = redact_text(str(exc))[:500] or WAITING
        except Exception as exc:  # noqa: BLE001 - record on the job, never crash the worker
            outcome = FAILED
            error = redact_text(str(exc))[:500] or exc.__class__.__name__
        with self.lock:
            job.finished_at = time.time()
            if outcome == CANCELLED:
                job.status = CANCELLED
                job.phase = CANCELLED
                job.log.append(f"{_stamp()} cancelled")
            elif outcome == WAITING:
                # Not an error: no result, no spend, nothing to retry from locally.  The reason
                # lives in ``message`` so the page can explain the pause without a failure banner.
                job.status = WAITING
                job.phase = WAITING
                job.message = error or WAITING
                job.log.append(f"{_stamp()} waiting: {error}")
            elif outcome == FAILED:
                job.failed_phase = job.phase
                job.status = FAILED
                job.phase = FAILED
                job.error = error
                job.log.append(f"{_stamp()} failed: {error}")
            else:
                job.status = SUCCEEDED
                job.phase = "done"
                job.log.append(f"{_stamp()} done")

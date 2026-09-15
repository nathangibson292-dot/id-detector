"""AudD clip recognition — the Deep recipe's primary sweep.

Recipe-primary deep scans sweep the frozen generation-0 windows at the recipe density and send
each clip to a paid recogniser's clip endpoint — the same ~12 s clips Shazam already sees, so
there is no whole-file upload and no third-party-upload consent gate.  The resulting
``clip_recognizer`` observations are generation 0 for the fuser; the Shazam secondary
(:mod:`id_detector.secondary_targeting`) then re-fuses alongside them: agreeing lifts a track's
confidence, disagreeing lets a phantom be demoted, and a catalogue-blind track is recovered.

The sweep (plan §2.3.1–2.3.3) runs ``audd_concurrency`` clips at once behind a token bucket,
retries only the recipe's zero-cost transient outcomes with its backoff, stops at once on a
terminal-provider outcome, and records every request in the durable attempt journal
(:mod:`id_detector.attempts`): ``prepared`` → ``dispatched`` (on disk before network I/O) →
``resolved``.  A cancel token — or a progress hook that raises — stops new dispatches while the
clips already in flight are allowed to resolve, so their spend is never lost.

Only validated match/no-match responses are cached by clip cache-key across runs. Matches are reused
by default, while cached no-matches are re-queried by default and ``refresh_states`` controls that
selection. Dollar admission bounds live calls before dispatch; only the live call needs a
credential.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from id_detector.attempts import (
    AttemptJournal,
    DispatchRefused,
    attempts_path,
    load_attempt_ledger,
)
from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    ObservationRecord,
    QueryRecord,
    WindowQueryTarget,
    WindowRecord,
    clip_cache_key,
    compose_natural_key,
    make_id,
    sort_records,
)
from id_detector.io import atomic_write_json, path_is_file, read_text
from id_detector.money import (
    BILLABLE_OUTCOMES,
    TERMINAL_PROVIDER_OUTCOMES,
    UNREACHABLE_OUTCOMES,
    ReservationExhausted,
    UsdAdmitter,
)
from id_detector.providers.audd import (
    DEFAULT_ANCHOR_MAX_MS,
    DEFAULT_ANCHOR_SLACK_MS,
    AudDAdapter,
    AudDCredentials,
    clip_response_to_observation,
    clip_result_has_identity,
)
from id_detector.providers.base import (
    AmbiguousProviderOutcome,
    AppConfig,
    ProviderProtocolError,
    ProviderUnavailable,
)
from id_detector.recipes import RetryPolicy
from id_detector.recognise import _write_jsonl
from id_detector.scan_targeting import Span
from id_detector.shazam import TokenBucket, canonicalize_provider_json
from id_detector.windows import WindowsResult

#: Paid clip-recognition engines (only AudD for now; ACRCloud is whole-file-only in this codebase).
PAID_CLIP_ENGINES: tuple[str, ...] = ("audd",)
CLIP_CONFIG_VERSION = "audd-main-v1"
#: Legacy selection ceiling kept as the parameter default; it is not the Deep recipe's dollar cap
#: (the primary passes the whole window count), and the supplemental call site that used it was
#: removed with the provisional secondary scheduler (0a-iv).
DEFAULT_MAX_CLIPS = 150
#: Throttle outcomes that also slow the token bucket (the AIMD cut), besides being retried.
THROTTLE_OUTCOMES = frozenset({"http_429", "http_503"})

LogFn = Callable[[str], None]
WindowProgressFn = Callable[[int, int], None]
SleepFn = Callable[[float], Awaitable[None]]


class CancelToken(Protocol):
    """Anything with ``is_set()`` — a ``threading.Event`` from the web app, or a test flag."""

    def is_set(self) -> bool: ...


@dataclass(frozen=True)
class PaidScanResult:
    """A paid stage's fusion contribution and request/cache audit."""

    observations: tuple[ObservationRecord, ...] = ()
    observation_paths: tuple[Path, ...] = ()
    engines_run: tuple[str, ...] = ()
    #: ``(provider, reason)`` for every requested engine that did not run.
    skipped: tuple[tuple[str, str], ...] = ()
    usd_e2: int = 0
    #: Windows sent to the provider at least once (cache hits excluded).
    requests: int = 0
    resolved: int = 0
    failures: int = 0
    cache_hits: int = 0
    billable_units: int = 0
    reservation_exhausted: bool = False
    #: Every live dispatch's frozen money outcome, in resolution order (cache hits excluded).
    outcomes: tuple[str, ...] = ()
    #: The terminal-provider outcome (``auth_error`` / ``quota_error``) that stopped the sweep.
    provider_stopped: str | None = None
    #: Live dispatches including retries; ``attempts - requests`` is the retry count.
    attempts: int = 0
    #: The cancel token fired or the progress hook raised: nothing further was dispatched and the
    #: clips already in flight were allowed to resolve.  The caller ends the run ``cancelled``.
    cancelled: bool = False
    #: Attempts an earlier run left unresolved that this sweep re-ran (plan §2.3.3 resume rule):
    #: ``dispatched`` without ``resolved`` is ambiguous (counted as spent by that run);
    #: ``prepared`` without ``dispatched`` was never sent and is simply re-issued.
    resumed_ambiguous: int = 0
    resumed_reissued: int = 0
    #: Clips THIS run had already resolved before it was interrupted: recovered from the
    #: attempt journal and never dispatched again, so a resume cannot pay twice for one clip.
    recovered_resolved: int = 0
    #: Clips THIS run dispatched and never saw resolved: ambiguous, counted as spent, never re-sent.
    recovered_ambiguous: int = 0

    @property
    def ran(self) -> bool:
        return bool(self.engines_run)

    @property
    def retries(self) -> int:
        return self.attempts - self.requests

    @property
    def unreachable(self) -> int:
        """Dispatches the provider could not be reached for (``connect_error``/``timeout_pre``)."""

        return sum(outcome in UNREACHABLE_OUTCOMES for outcome in self.outcomes)


def _cache_state(response: Mapping[str, Any]) -> str | None:
    """Return the only two states allowed in the raw response cache."""

    if response.get("status") != "success":
        return None
    result = response.get("result")
    if result is None:
        return "no_match"
    if isinstance(result, Mapping) and clip_result_has_identity(result):
        return "match"
    return None


#: AudD's own error codes inside an HTTP 200 body: 900 = wrong token, 901 = the token's request
#: limit is reached (https://docs.audd.io/#common-errors).  Both are terminal for the sweep.
_AUDD_AUTH_CODES = frozenset({401, 403, 900})
_AUDD_QUOTA_CODES = frozenset({402, 901})


def _error_body_outcome(response: Mapping[str, Any]) -> str:
    """Classify an AudD error body into the frozen money outcomes."""

    error = response.get("error")
    if isinstance(error, Mapping):
        code = error.get("error_code", error.get("code"))
        message = " ".join(str(value) for value in error.values())
    else:
        code = response.get("status_code")
        message = str(error or response.get("message") or "")
    try:
        status_code = int(code)
    except (TypeError, ValueError):
        status_code = 0
    # An explicit code always outranks the message words below: AudD words its throttle bodies
    # "…limit reached", and reading that as a terminal `quota_error` would stop the whole primary
    # on an outcome the recipe retries (plan §2.3.1/§2.3.3).  The words are only a fallback for a
    # body that carries no code we know.
    if status_code in _AUDD_AUTH_CODES:
        return "auth_error"
    if status_code in _AUDD_QUOTA_CODES:
        return "quota_error"
    if status_code == 429:
        return "http_429"
    if status_code == 503:
        return "http_503"
    if 500 <= status_code <= 599:
        return "http_5xx"
    lowered = message.casefold()
    if "auth" in lowered:
        return "auth_error"
    if any(word in lowered for word in ("quota", "credit", "limit reached")):
        return "quota_error"
    return "malformed"


#: The status code in ``ProviderProtocolError("AudD HTTP <code>")``.  Parsed rather than substring-
#: matched: a bare ``"503" in message`` would refund a billable unit for any message that merely
#: contains those digits (a byte count, a clip offset, a future error code).
_HTTP_STATUS = re.compile(r"\bhttp (\d{3})\b")


def _protocol_outcome(error: ProviderProtocolError) -> str:
    """Classify an AudD transport-level protocol error into the frozen money outcomes."""

    message = str(error).casefold()
    found = _HTTP_STATUS.search(message)
    status_code = int(found.group(1)) if found is not None else 0
    # Same precedence as `_error_body_outcome`: the status code decides, the words only fill in
    # for a transport error that carries none.
    if status_code in {401, 403}:
        return "auth_error"
    if status_code == 402:
        return "quota_error"
    if status_code == 429:
        return "http_429"
    if status_code == 503:
        return "http_503"
    if 500 <= status_code <= 599:
        return "http_5xx"
    if "auth" in message:
        return "auth_error"
    if any(word in message for word in ("quota", "credit")):
        return "quota_error"
    return "malformed"


def _unavailable_outcome(error: ProviderUnavailable) -> str:
    """``timeout_pre`` or ``connect_error`` — both pre-receipt and free, retried apart."""

    lowered = str(error).casefold()
    timed_out = any(word in lowered for word in ("timeout", "timed out"))
    return "timeout_pre" if timed_out else "connect_error"


def _subsample_evenly(items: list[WindowRecord], budget: int) -> list[WindowRecord]:
    """At most ``budget`` items, spread uniformly across ``items`` (not just the first ``budget``).

    Taking the first N would leave a long mix's later half unchecked; even spacing keeps whole-mix
    coverage while bounding spend.
    """

    if budget <= 0:
        return []
    if len(items) <= budget:
        return items
    step = len(items) / budget
    return [items[int(index * step)] for index in range(budget)]


def _clip_query(media_key: str, window: WindowRecord) -> QueryRecord:
    target = WindowQueryTarget(window_id=window.id)
    natural = {
        "provider": "audd",
        "capability": "clip_recognizer",
        "target": target.model_dump(mode="json"),
        "provider_config_version": CLIP_CONFIG_VERSION,
        "scan_policy": "single-window-main-endpoint",
    }
    return QueryRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id(media_key, "query", compose_natural_key("query", natural)),
        generation=0,
        provider="audd",
        capability="clip_recognizer",
        target=target,
        provider_config_version=CLIP_CONFIG_VERSION,
        scan_policy="single-window-main-endpoint",
        cache_key=clip_cache_key(window.wav_sha256, "audd", CLIP_CONFIG_VERSION),
    )


def _windows_in_targets(windows: WindowsResult, targets: tuple[Span, ...]) -> list[WindowRecord]:
    """The untransformed generation-0 window clips whose start falls in an uncertain target span."""

    selected: list[WindowRecord] = []
    seen: set[str] = set()
    for window in windows.records:
        if window.transform.type != "none":  # only the untransformed clip, never a rescan variant
            continue
        start = window.support_ms[0]
        if not any(lo <= start < hi for lo, hi in targets):
            continue
        if window.wav_sha256 in seen:  # identical clip content -> recognise once
            continue
        seen.add(window.wav_sha256)
        selected.append(window)
    selected.sort(key=lambda w: w.support_ms[0])
    return selected


def _invocation_dir(media_dir: Path, run_id: str) -> Path:
    """This pass's immutable output directory for ``run_id``.

    Recognition artefacts are immutable per invocation (``recognise._write_jsonl`` refuses to
    replace one), and a resumed run legitimately produces a LARGER observation set than the pass it
    resumes: reusing the first pass's directory made every resume of a cancelled primary die of
    ``FileExistsError`` — after paying for its provider calls. Each pass writes beside the last.
    """

    base = media_dir / "recognise" / "invocations" / f"live-audd-clip-{run_id[:12]}"
    if not path_is_file(base / "observations.gen0.jsonl"):
        return base
    ordinal = 1
    while path_is_file(Path(f"{base}-r{ordinal}") / "observations.gen0.jsonl"):
        ordinal += 1
    return Path(f"{base}-r{ordinal}")


def _read_cached(raw_path: Path, refresh_states: frozenset[str]) -> dict[str, Any] | None:
    """A cached match/no-match body unless its state is one the caller wants re-queried."""

    if not path_is_file(raw_path):
        return None
    try:
        cached = json.loads(read_text(raw_path))
    except (ValueError, OSError):
        return None
    state = _cache_state(cached) if isinstance(cached, dict) else None
    if state is None or state in refresh_states:
        return None
    return cached


@dataclass
class _Sweep:
    """Mutable state shared by the worker tasks; only ever touched between awaits."""

    total: int
    observations: list[ObservationRecord] = field(default_factory=list)
    #: One query per window the sweep touched, keyed by window id: the workers interleave, so the
    #: artefact is emitted in window order rather than in whichever order they happened to finish.
    queries: dict[str, QueryRecord] = field(default_factory=dict)
    outcomes: list[str] = field(default_factory=list)
    requests: int = 0
    attempts: int = 0
    cache_hits: int = 0
    billable_units: int = 0
    done: int = 0
    reservation_exhausted: bool = False
    provider_stopped: str | None = None
    resumed_ambiguous: int = 0
    resumed_reissued: int = 0
    recovered_resolved: int = 0
    recovered_ambiguous: int = 0
    #: No further dispatch (terminal outcome, exhausted reservation, or a cancel).
    halted: bool = False
    cancelled: bool = False

    def stop(self) -> None:
        self.halted = True

    def cancel(self) -> None:
        self.halted = True
        self.cancelled = True


async def run_paid_clip_recognition(
    *,
    media_key: str,
    media_dir: Path,
    windows: WindowsResult,
    targets: tuple[Span, ...],
    run_id: str,
    app_config: AppConfig,
    enabled_engines: tuple[str, ...] | list[str],
    cli_confirmation: bool,
    refresh: bool = False,
    refresh_states: frozenset[str] = frozenset({"no_match"}),
    max_clips: int = DEFAULT_MAX_CLIPS,
    primary_density: int = 1,
    usd_admitter: UsdAdmitter | None = None,
    adapters: Mapping[str, Any] | None = None,
    log: LogFn | None = None,
    concurrency: int = 1,
    retry_policy: RetryPolicy | None = None,
    anchor_max_ms: int = DEFAULT_ANCHOR_MAX_MS,
    anchor_slack_ms: int = DEFAULT_ANCHOR_SLACK_MS,
    cancel_token: CancelToken | None = None,
    on_window: WindowProgressFn | None = None,
    sleep: SleepFn | None = None,
    attempt_journal: AttemptJournal | None = None,
) -> PaidScanResult:
    """Recognise the uncertain-region window clips with the paid engine and return observations.

    Gated on the engine being enabled and its credentials being present — NOT on upload consent
    (a clip is the same shape as the free Shazam clip).  ``adapters`` injects a fake adapter for
    tests; ``sleep`` injects the retry backoff sleeper.  ``concurrency``, ``retry_policy`` and the
    anchor bounds come from the recipe; ``on_window`` is ticked once per finished clip and, like
    ``log``, may raise ``asyncio.CancelledError`` to cancel the sweep.  Never raises for an
    unavailable engine; it is skipped and recorded.
    """

    emit_raw: LogFn = log or (lambda _message: None)
    if "audd" not in enabled_engines or not targets:
        return PaidScanResult()
    override = adapters.get("audd") if adapters else None
    try:
        adapter = (
            override
            if override is not None
            else AudDAdapter(AudDCredentials.from_env(), app_config, cli_confirmation)
        )
    except ProviderUnavailable as exc:
        emit_raw(f"paid clip engine audd skipped: {exc}")
        return PaidScanResult(skipped=((("audd"), str(exc)),))

    if primary_density <= 0:
        raise ValueError("primary_density must be positive")
    if concurrency <= 0:
        raise ValueError("concurrency must be positive")
    eligible = _windows_in_targets(windows, tuple(targets))
    if usd_admitter is not None:
        # Recipe reservations are calculated from the frozen generation-zero window set. Keep
        # dispatch selection on that identical set even if a legacy global transform policy is on.
        eligible = [
            window
            for window in eligible
            if window.generation == 0 and window.transform.type == "none"
        ]
    selected = _subsample_evenly(eligible[::primary_density], max_clips)
    if not selected:
        return PaidScanResult()

    policy = retry_policy if retry_policy is not None and retry_policy.mode == "bounded" else None
    retryable = frozenset(policy.retryable_outcomes) if policy is not None else frozenset()
    max_retries = policy.max_retries if policy is not None else 0
    backoff = tuple(policy.backoff_seconds) if policy is not None else ()
    pause: SleepFn = sleep or asyncio.sleep

    cache_dir = media_dir / "recognise" / "invocations" / "live-audd-clip-v1" / "raw"
    invocation_dir = _invocation_dir(media_dir, run_id)
    journal = attempt_journal or AttemptJournal(
        attempts_path(media_dir),
        run_id=run_id,
        provider="audd",
        unit_usd_e6=app_config.audd_usd_e6_per_request,
    )
    if usd_admitter is not None:
        # One price per request: the ledger records exactly what the admitter charges, which is
        # the run's durable reservation price — never whatever the configuration says today.
        journal.unit_usd_e6 = usd_admitter.reservation.unit_usd_e6
    # Plan §2.3.3: the journal the sweep WRITES to is the one recovery must READ. Reading the
    # per-media default while dispatch wrote to an injected (hosted) journal made that journal's
    # unresolved dispatches invisible on resume, so the next run re-billed them.
    ledger = load_attempt_ledger(journal.path)  # every run: an earlier run's dangling parent
    # THIS run's verified fold (the JSONL plus, for a hosted journal, its SQLite projection): the
    # shared resume rule of :mod:`id_detector.run_ledger` decides each query below.
    run_ledger = journal.run_ledger()
    limiter = TokenBucket(rate_per_minute=app_config.audd_requests_per_minute, capacity=concurrency)
    sweep = _Sweep(total=len(selected))
    queue: deque[WindowRecord] = deque(selected)

    def _guard(call: Callable[[], None]) -> None:
        """Run a progress/log callback; a ``CancelledError`` it raises is a cancel request.

        The web app's progress hook raises to cancel a job.  Raising it straight out of a worker
        would tear down the sibling requests in flight — requests the provider may already have
        billed — so the sweep records the request, stops dispatching and lets them resolve.
        """

        try:
            call()
        except asyncio.CancelledError:
            sweep.cancel()

    def emit(message: str) -> None:
        _guard(lambda: emit_raw(message))

    def _tick() -> None:
        if on_window is not None:
            _guard(lambda: on_window(sweep.done, sweep.total))

    def _halt_requested() -> bool:
        if cancel_token is not None and cancel_token.is_set():
            sweep.cancel()
        return sweep.halted

    async def _attempt(
        wav: Path, attempt_id: str, start_s: int
    ) -> tuple[str | None, dict[str, Any] | None, bool]:
        """One dispatch: ``(outcome, body, admitted)``.

        ``outcome`` is ``None`` when the adapter failed without reaching the provider at all; the
        caller settles that as ambiguous whenever the unit was already admitted, so an admitted
        unit is never left outstanding.
        """

        admitted = False

        async def _admit() -> None:
            nonlocal admitted
            if admitted:
                # One dispatch is one attempt: a second callback would journal a second
                # `dispatched` for this attempt id and admit a second unit for one request.
                raise RuntimeError("AudD adapter invoked its on_attempt callback twice")
            if usd_admitter is not None:
                usd_admitter.admit()
            # Plan §2.3.3: `dispatched` is durable before the request enters network I/O, so a
            # crash from here on leaves an attempt the resume rule treats as ambiguous (spent).
            # The job queue's admission check runs immediately before that write: a cancel (or a
            # lost lease) committed after the last halt check still stops THIS request.
            try:
                journal.dispatched(attempt_id)
            except DispatchRefused:
                if usd_admitter is not None:
                    usd_admitter.resolve("timeout_pre")  # admitted, never sent: refunded
                raise
            admitted = True
            sweep.attempts += 1

        try:
            response = await adapter.recognize_clip(wav, on_attempt=_admit)
        except ReservationExhausted:
            raise
        except DispatchRefused:
            # Nothing was sent; the attempt stays `prepared`. The job was cancelled or lost its
            # claim, so the sweep stops dispatching and the run ends `cancelled`.
            sweep.cancel()
            emit("audd primary stopped: the job queue refused the dispatch")
            return None, None, False
        except ProviderUnavailable as exc:
            emit(f"audd clip error @{start_s}s: {type(exc).__name__}")
            return _unavailable_outcome(exc), None, admitted
        except AmbiguousProviderOutcome as exc:
            emit(f"audd clip error @{start_s}s: {type(exc).__name__}")
            return "timeout_post", None, admitted
        except ProviderProtocolError as exc:
            emit(f"audd clip error @{start_s}s: {type(exc).__name__}")
            return _protocol_outcome(exc), None, admitted
        except FileNotFoundError as exc:
            emit(f"audd clip error @{start_s}s: {type(exc).__name__}")
            return None, None, admitted
        except asyncio.CancelledError:
            # Real task cancellation (Ctrl-C) mid-request: the journal keeps `dispatched` without
            # `resolved` — ambiguous on resume — and the admitted unit settles as spent.
            if admitted and usd_admitter is not None:
                usd_admitter.resolve("timeout_post")
            raise
        except Exception:
            if admitted and usd_admitter is not None:
                usd_admitter.resolve("malformed")
            raise
        if not admitted:
            raise RuntimeError("AudD adapter returned without invoking its on_attempt callback")
        state = _cache_state(response)
        if state is None:
            return _error_body_outcome(response), None, admitted
        return state, response, admitted

    async def _process(window: WindowRecord) -> None:
        query = _clip_query(media_key, window)
        sweep.queries[window.id] = query
        raw_path = cache_dir / f"{query.cache_key}.json"
        raw_ref = raw_path.relative_to(media_dir).as_posix()
        start_s = window.support_ms[0] // 1000
        # Plan §2.3.3, decided once for both layers by `run_ledger.RunLedger.resume`: what an
        # earlier pass of THIS run already did to this clip.
        resume = run_ledger.resume(query.cache_key)
        if resume.action in {"settled", "ambiguous"}:
            # `settled`: resolved billable-ambiguous (timeout_post/http_5xx/malformed) or terminal
            # (auth/quota) — spent or refused, never automatically retried. `ambiguous`: dispatched
            # and never resolved — the provider may have billed it, so it is spent and NOT re-sent.
            if resume.action == "ambiguous":
                sweep.recovered_ambiguous += 1
            else:
                sweep.recovered_resolved += 1
            sweep.done += 1
            _tick()
            return
        if resume.action == "reuse":
            # Resolved match/no_match: never sent again, whatever ``refresh`` or ``refresh_states``
            # say — re-querying a cached ``no_match`` is how a resume used to re-bill settled work.
            response = _read_cached(raw_path, frozenset())
            sweep.recovered_resolved += 1
            if response is None:
                sweep.done += 1
                _tick()
                return
        elif resume.action == "fresh" and not refresh:
            response = _read_cached(raw_path, refresh_states)
        else:
            response = None  # `retry` / `reissue` (zero-cost or never sent) and ``--refresh``
        was_cached = response is not None
        if was_cached:
            sweep.cache_hits += 1
        else:
            wav = media_dir / window.wav_path
            if resume.action == "fresh":
                # An EARLIER RUN's unresolved attempt on this clip becomes this run's parent —
                # ambiguous if it was dispatched (that run charged it), a re-issue if only prepared.
                dangling = ledger.dangling(query.cache_key)
                parent: str | None = dangling.attempt_id if dangling is not None else None
            else:
                # This run's own zero-cost or never-sent attempt: retried under a fresh ordinal,
                # so the new attempt id cannot collide with the old one and never parents itself.
                dangling = None
                parent = resume.parent_attempt_id
            ordinal = resume.next_ordinal
            first_ordinal = ordinal
            sent = False
            while True:
                await limiter.acquire()
                if _halt_requested():
                    break
                attempt_id = journal.prepare(
                    query_id=query.cache_key,
                    window_id=window.id,
                    ordinal=ordinal,
                    parent_attempt_id=parent,
                )
                try:
                    outcome, response, admitted = await _attempt(wav, attempt_id, start_s)
                except ReservationExhausted:
                    sweep.reservation_exhausted = True
                    sweep.stop()
                    emit("audd primary stopped: USD reservation exhausted")
                    break
                if not admitted:
                    break  # never sent: the `prepared` line is re-issued by a later run
                if outcome is None:
                    # The adapter admitted the unit — its `dispatched` line is already on disk —
                    # and then failed without a provider outcome.  Settle it as ambiguous (spent,
                    # never retried) rather than breaking: an unresolved admission would hold a
                    # unit of the reservation for the rest of the run and leave a dangling
                    # `dispatched` that the next run re-bills.
                    outcome = "timeout_post"
                if not sent:
                    sent = True
                    sweep.requests += 1
                    if dangling is not None and dangling.classification == "ambiguous":
                        sweep.resumed_ambiguous += 1
                    elif dangling is not None or resume.action == "reissue":
                        sweep.resumed_reissued += 1
                journal.resolved(attempt_id, outcome)  # type: ignore[arg-type]
                if usd_admitter is not None:
                    usd_admitter.resolve(outcome)  # type: ignore[arg-type]
                sweep.outcomes.append(outcome)
                if outcome in BILLABLE_OUTCOMES:
                    sweep.billable_units += 1
                if response is not None:
                    limiter.recover()
                    break
                if outcome in TERMINAL_PROVIDER_OUTCOMES:
                    # Plan §2.3.3: a refused credential or an exhausted quota will not change for
                    # the next window, so the primary stops at once rather than burning the sweep.
                    sweep.provider_stopped = outcome
                    sweep.stop()
                    emit(f"audd primary stopped: {outcome}")
                    break
                if outcome in THROTTLE_OUTCOMES:
                    limiter.penalize()
                retries_used = ordinal - first_ordinal
                if outcome not in retryable or retries_used >= max_retries or _halt_requested():
                    break
                delay = backoff[min(retries_used, len(backoff) - 1)] if backoff else 0
                emit(
                    f"audd clip retry {retries_used + 1}/{max_retries} @{start_s}s "
                    f"after {outcome} in {delay}s"
                )
                await pause(delay)
                parent = attempt_id
                ordinal += 1
            if not sent:
                return
        if response is not None:
            try:
                observation = clip_response_to_observation(
                    response,
                    query=query,
                    window=window,
                    media_key=media_key,
                    raw_response_ref=raw_ref,
                    anchor_max_ms=anchor_max_ms,
                    anchor_slack_ms=anchor_slack_ms,
                )
            except ProviderProtocolError as exc:
                # Only a cached body can fail here: a live body was classified before it was
                # accepted as match/no-match.  The cache entry is stale; the window is unresolved.
                emit(f"audd clip parse error @{start_s}s: {exc}")
            else:
                sweep.observations.append(observation)
                if not was_cached:
                    # Parsing established a match/no-match state. Provider errors and malformed
                    # successes never reach this content-addressed cache write.
                    atomic_write_json(raw_path, canonicalize_provider_json(response))
        sweep.done += 1
        _tick()

    async def _worker() -> None:
        while queue and not _halt_requested():
            await _process(queue.popleft())

    emit(f"identifying the whole mix (paid): {len(selected)} clips")
    _tick()
    workers = [asyncio.create_task(_worker()) for _ in range(min(concurrency, len(selected)))]
    try:
        await asyncio.gather(*workers)
    except BaseException:
        # Ctrl-C (CancelledError) or an unexpected worker error: cancel the siblings, let them
        # unwind, then re-raise.  Their in-flight requests stay `dispatched` in the journal.
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise

    observations_out = tuple(sort_records(sweep.observations))
    observation_path = invocation_dir / "observations.gen0.jsonl"
    _write_jsonl(observation_path, list(observations_out))
    _write_jsonl(
        invocation_dir / "queries.gen0.jsonl",
        [sweep.queries[window.id] for window in selected if window.id in sweep.queries],
    )
    matched = sum(item.status == "match" for item in observations_out)
    not_sent = len(selected) - sweep.requests - sweep.cache_hits
    retries = sweep.attempts - sweep.requests
    summary = (
        f"audd clips: {matched} match(es) across {len(observations_out)} windows "
        f"({sweep.requests} requests, {sweep.cache_hits} cached, {not_sent} not sent)"
    )
    if retries:
        summary += f"; {retries} retries"
    if sweep.resumed_ambiguous or sweep.resumed_reissued:
        summary += (
            f"; resumed {sweep.resumed_ambiguous} ambiguous, "
            f"{sweep.resumed_reissued} re-issued attempt(s) from an earlier run"
        )
    if sweep.recovered_resolved:
        summary += (
            f"; {sweep.recovered_resolved} clip(s) this run had already resolved were recovered, "
            "not re-sent"
        )
    if sweep.recovered_ambiguous:
        summary += (
            f"; {sweep.recovered_ambiguous} ambiguous clip(s) this run had already dispatched were "
            "counted as spent, not re-sent"
        )
    if sweep.cancelled:
        summary += "; cancelled"
    emit(summary)
    return PaidScanResult(
        observations=observations_out,
        observation_paths=(observation_path,),
        engines_run=("audd",),
        requests=sweep.requests,
        resolved=len(observations_out),
        # Windows never reached (a stopped or cancelled sweep) are not failures.
        failures=sweep.requests + sweep.cache_hits - len(observations_out),
        cache_hits=sweep.cache_hits,
        billable_units=sweep.billable_units,
        reservation_exhausted=sweep.reservation_exhausted,
        outcomes=tuple(sweep.outcomes),
        provider_stopped=sweep.provider_stopped,
        attempts=sweep.attempts,
        cancelled=sweep.cancelled,
        resumed_ambiguous=sweep.resumed_ambiguous,
        resumed_reissued=sweep.resumed_reissued,
        recovered_resolved=sweep.recovered_resolved,
        recovered_ambiguous=sweep.recovered_ambiguous,
    )

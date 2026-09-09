"""AudD clip recognition — the Deep recipe's primary sweep.

Recipe-primary deep scans sweep the frozen generation-0 windows at the recipe density and send
each clip to a paid recogniser's clip endpoint — the same ~12 s clips Shazam already sees, so
there is no whole-file upload and no third-party-upload consent gate.  The resulting
``clip_recognizer`` observations are generation 0 for the fuser; the Shazam secondary
(:mod:`id_detector.secondary_targeting`) then re-fuses alongside them: agreeing lifts a track's
confidence, disagreeing lets a phantom be demoted, and a catalogue-blind track is recovered.

Only validated match/no-match responses are cached by clip cache-key across runs. Matches are reused
by default, while cached no-matches are re-queried by default and ``refresh_states`` controls that
selection. Dollar admission bounds live calls before dispatch; only the live call needs a
credential.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    TERMINAL_PROVIDER_OUTCOMES,
    UNREACHABLE_OUTCOMES,
    ReservationExhausted,
    UsdAdmitter,
)
from id_detector.providers.audd import (
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
from id_detector.recognise import _write_jsonl
from id_detector.scan_targeting import Span
from id_detector.shazam import canonicalize_provider_json
from id_detector.windows import WindowsResult

#: Paid clip-recognition engines (only AudD for now; ACRCloud is whole-file-only in this codebase).
PAID_CLIP_ENGINES: tuple[str, ...] = ("audd",)
CLIP_CONFIG_VERSION = "audd-main-v1"
#: Legacy selection ceiling kept as the parameter default; it is not the Deep recipe's dollar cap
#: (the primary passes the whole window count), and the supplemental call site that used it was
#: removed with the provisional secondary scheduler (0a-iv).
DEFAULT_MAX_CLIPS = 150

LogFn = Callable[[str], None]


@dataclass(frozen=True)
class PaidScanResult:
    """A paid stage's fusion contribution and request/cache audit."""

    observations: tuple[ObservationRecord, ...] = ()
    observation_paths: tuple[Path, ...] = ()
    engines_run: tuple[str, ...] = ()
    #: ``(provider, reason)`` for every requested engine that did not run.
    skipped: tuple[tuple[str, str], ...] = ()
    usd_e2: int = 0
    requests: int = 0
    resolved: int = 0
    failures: int = 0
    cache_hits: int = 0
    billable_units: int = 0
    reservation_exhausted: bool = False
    #: Every live dispatch's frozen money outcome, in dispatch order (cache hits excluded).
    outcomes: tuple[str, ...] = ()
    #: The terminal-provider outcome (``auth_error`` / ``quota_error``) that stopped the sweep.
    provider_stopped: str | None = None

    @property
    def ran(self) -> bool:
        return bool(self.engines_run)

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
    lowered = message.casefold()
    if status_code in {401, 403} or "auth" in lowered:
        return "auth_error"
    if status_code == 402 or any(word in lowered for word in ("quota", "credit")):
        return "quota_error"
    if status_code == 429:
        return "http_429"
    if status_code == 503:
        return "http_503"
    if 500 <= status_code <= 599:
        return "http_5xx"
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
    if status_code in {401, 403} or "auth" in message:
        return "auth_error"
    if status_code == 402 or any(word in message for word in ("quota", "credit")):
        return "quota_error"
    if status_code == 429:
        return "http_429"
    if status_code == 503:
        return "http_503"
    if 500 <= status_code <= 599:
        return "http_5xx"
    return "malformed"


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
) -> PaidScanResult:
    """Recognise the uncertain-region window clips with the paid engine and return observations.

    Gated on the engine being enabled and its credentials being present — NOT on upload consent
    (a clip is the same shape as the free Shazam clip).  ``adapters`` injects a fake adapter for
    tests.  Never raises for an unavailable engine; it is skipped and recorded.
    """

    emit: LogFn = log or (lambda _message: None)
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
        emit(f"paid clip engine audd skipped: {exc}")
        return PaidScanResult(skipped=((("audd"), str(exc)),))

    if primary_density <= 0:
        raise ValueError("primary_density must be positive")
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

    cache_dir = media_dir / "recognise" / "invocations" / "live-audd-clip-v1" / "raw"
    invocation_dir = media_dir / "recognise" / "invocations" / f"live-audd-clip-{run_id[:12]}"
    observations: list[ObservationRecord] = []
    queries: list[QueryRecord] = []
    requests = 0
    cache_hits = 0
    billable_units = 0
    reservation_exhausted = False
    outcomes: list[str] = []
    provider_stopped: str | None = None

    for window in selected:
        query = _clip_query(media_key, window)
        queries.append(query)
        raw_path = cache_dir / f"{query.cache_key}.json"
        raw_ref = raw_path.relative_to(media_dir).as_posix()
        response: dict[str, Any] | None = None
        was_cached = False
        if not refresh and path_is_file(raw_path):
            try:
                cached = json.loads(read_text(raw_path))
                state = _cache_state(cached) if isinstance(cached, dict) else None
                if state is not None and state not in refresh_states:
                    response = cached
                    was_cached = True
                    cache_hits += 1
            except (ValueError, OSError):
                response = None
        if response is None:
            wav = media_dir / window.wav_path
            requests += 1
            admitted = False

            async def _admit() -> None:
                nonlocal admitted
                if usd_admitter is not None:
                    usd_admitter.admit()
                admitted = True

            try:
                response = await adapter.recognize_clip(wav, on_attempt=_admit)
            except ReservationExhausted:
                requests -= 1
                reservation_exhausted = True
                emit("audd primary stopped: USD reservation exhausted")
                break
            except ProviderUnavailable as exc:
                lowered = str(exc).casefold()
                timed_out = any(word in lowered for word in ("timeout", "timed out"))
                outcome = "timeout_pre" if timed_out else "connect_error"
                if admitted and usd_admitter is not None:
                    usd_admitter.resolve(outcome)  # type: ignore[arg-type]
                outcomes.append(outcome)
                emit(f"audd clip error @{window.support_ms[0] // 1000}s: {type(exc).__name__}")
                continue
            except AmbiguousProviderOutcome as exc:
                if admitted and usd_admitter is not None:
                    usd_admitter.resolve("timeout_post")
                outcomes.append("timeout_post")
                billable_units += 1
                emit(f"audd clip error @{window.support_ms[0] // 1000}s: {type(exc).__name__}")
                continue
            except (ProviderProtocolError, FileNotFoundError) as exc:
                outcome = _protocol_outcome(exc) if isinstance(exc, ProviderProtocolError) else None
                if admitted and usd_admitter is not None and outcome is not None:
                    usd_admitter.resolve(outcome)  # type: ignore[arg-type]
                if outcome is not None:
                    outcomes.append(outcome)
                if outcome in {"http_5xx", "malformed"}:
                    billable_units += 1
                emit(f"audd clip error @{window.support_ms[0] // 1000}s: {type(exc).__name__}")
                if outcome in TERMINAL_PROVIDER_OUTCOMES:
                    # Plan §2.3.3: a refused credential or an exhausted quota will not change for
                    # the next window, so the primary stops at once rather than burning the sweep.
                    provider_stopped = outcome
                    emit(f"audd primary stopped: {outcome}")
                    break
                continue
            except asyncio.CancelledError:
                if admitted and usd_admitter is not None:
                    usd_admitter.resolve("timeout_post")
                raise
            except Exception:
                if admitted and usd_admitter is not None:
                    usd_admitter.resolve("malformed")
                raise
            if usd_admitter is not None and not admitted:
                raise RuntimeError("AudD adapter returned without invoking its on_attempt callback")
        try:
            observation = clip_response_to_observation(
                response,
                query=query,
                window=window,
                media_key=media_key,
                raw_response_ref=raw_ref,
            )
        except ProviderProtocolError as exc:
            outcome = _error_body_outcome(response) if response is not None else "malformed"
            if not was_cached and usd_admitter is not None:
                usd_admitter.resolve(outcome)  # type: ignore[arg-type]
            if not was_cached:
                outcomes.append(outcome)
            if outcome in {"http_5xx", "malformed"}:
                billable_units += 1
            emit(f"audd clip parse error @{window.support_ms[0] // 1000}s: {exc}")
            if outcome in TERMINAL_PROVIDER_OUTCOMES:
                provider_stopped = outcome
                emit(f"audd primary stopped: {outcome}")
                break
            continue
        except asyncio.CancelledError:
            if not was_cached and usd_admitter is not None:
                usd_admitter.resolve("timeout_post")
            raise
        except Exception:
            if not was_cached and usd_admitter is not None:
                usd_admitter.resolve("malformed")
            raise
        observations.append(observation)
        if not was_cached:
            # Parsing established a match/no-match state. Provider errors and malformed successes
            # never reach this content-addressed cache write.
            atomic_write_json(raw_path, canonicalize_provider_json(response))
            state = _cache_state(response)
            if usd_admitter is not None:
                usd_admitter.resolve(state)  # type: ignore[arg-type]
            outcomes.append(str(state))
            billable_units += 1

    observations_out = tuple(sort_records(observations))
    observation_path = invocation_dir / "observations.gen0.jsonl"
    _write_jsonl(observation_path, list(observations_out))
    _write_jsonl(invocation_dir / "queries.gen0.jsonl", queries)
    matched = sum(item.status == "match" for item in observations_out)
    not_sent = len(selected) - requests - cache_hits
    emit(
        f"audd clips: {matched} match(es) across {len(observations_out)} windows "
        f"({requests} requests, {cache_hits} cached, {not_sent} not sent)"
    )
    return PaidScanResult(
        observations=observations_out,
        observation_paths=(observation_path,),
        engines_run=("audd",),
        requests=requests,
        resolved=len(observations_out),
        # Windows never reached (an exhausted reservation stops the sweep) are not failures.
        failures=requests + cache_hits - len(observations_out),
        cache_hits=cache_hits,
        billable_units=billable_units,
        reservation_exhausted=reservation_exhausted,
        outcomes=tuple(outcomes),
        provider_stopped=provider_stopped,
    )

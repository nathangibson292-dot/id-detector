"""The paid CLIP-recognition stage (the corrected, gate-free design).

The free (Shazam) pass runs and fuses first.  This stage then sends ONLY the window clips that fall
in the still-uncertain regions (see :func:`id_detector.scan_targeting.select_scan_targets`) to a
paid recogniser's clip endpoint — the same ~12 s clips Shazam already sees, so there is no
whole-file upload and no third-party-upload consent gate.  The resulting ``clip_recognizer``
observations join the fuser and re-fuse alongside Shazam's: agreeing lifts a track's confidence,
disagreeing lets a Shazam phantom be demoted, and a Shazam-blind (but catalogued) track is
recovered.

Only validated match/no-match responses are cached by clip cache-key across runs. Matches are reused
by default, while cached no-matches are re-queried by default and ``refresh_states`` controls that
selection; a per-run request cap bounds the spend. Only the live HTTP call needs a credential.
"""

from __future__ import annotations

import json
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
#: Default per-mix clip budget (cost guard).  At AudD's $5/1000, 150 clips ≈ $0.75/mix, and the
#: 300-request free trial covers ~2 mixes.  A track spans minutes, so an evenly-spread 150 still
#: samples every uncertain track many times; raise it with --max-paid-clips to trade cost for reach.
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

    @property
    def ran(self) -> bool:
        return bool(self.engines_run)


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


def _error_body_is_free(response: Mapping[str, Any]) -> bool:
    """Identify provider refusals that the frozen billing rules price at zero units."""

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
    return status_code in {401, 402, 403, 429, 503} or any(
        word in lowered for word in ("quota", "credit", "auth")
    )


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

    selected = _subsample_evenly(_windows_in_targets(windows, tuple(targets)), max_clips)
    if not selected:
        return PaidScanResult()

    cache_dir = media_dir / "recognise" / "invocations" / "live-audd-clip-v1" / "raw"
    invocation_dir = media_dir / "recognise" / "invocations" / f"live-audd-clip-{run_id[:12]}"
    observations: list[ObservationRecord] = []
    queries: list[QueryRecord] = []
    requests = 0
    cache_hits = 0
    billable_units = 0

    async def _noop() -> None:
        return None

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
            try:
                response = await adapter.recognize_clip(wav, on_attempt=_noop)
            except ProviderUnavailable as exc:
                emit(f"audd clip error @{window.support_ms[0] // 1000}s: {type(exc).__name__}")
                continue
            except AmbiguousProviderOutcome as exc:
                billable_units += 1
                emit(f"audd clip error @{window.support_ms[0] // 1000}s: {type(exc).__name__}")
                continue
            except (ProviderProtocolError, FileNotFoundError) as exc:
                message = str(exc).casefold()
                if not any(code in message for code in ("401", "402", "403", "429", "503")):
                    billable_units += 1
                emit(f"audd clip error @{window.support_ms[0] // 1000}s: {type(exc).__name__}")
                continue
        try:
            observation = clip_response_to_observation(
                response,
                query=query,
                window=window,
                media_key=media_key,
                raw_response_ref=raw_ref,
            )
        except ProviderProtocolError as exc:
            if response is not None and not _error_body_is_free(response):
                billable_units += 1
            emit(f"audd clip parse error @{window.support_ms[0] // 1000}s: {exc}")
            continue
        observations.append(observation)
        if not was_cached:
            # Parsing established a match/no-match state. Provider errors and malformed successes
            # never reach this content-addressed cache write.
            atomic_write_json(raw_path, canonicalize_provider_json(response))
            billable_units += 1

    observations_out = tuple(sort_records(observations))
    observation_path = invocation_dir / "observations.gen0.jsonl"
    _write_jsonl(observation_path, list(observations_out))
    _write_jsonl(invocation_dir / "queries.gen0.jsonl", queries)
    matched = sum(item.status == "match" for item in observations_out)
    emit(
        f"audd clips: {matched} match(es) across {len(observations_out)} uncertain windows "
        f"({requests} requests, {len(selected) - requests} cached)"
    )
    return PaidScanResult(
        observations=observations_out,
        observation_paths=(observation_path,),
        engines_run=("audd",),
        requests=requests,
        resolved=len(observations_out),
        failures=len(selected) - len(observations_out),
        cache_hits=cache_hits,
        billable_units=billable_units,
    )

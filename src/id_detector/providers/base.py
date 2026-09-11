"""Small shared contracts for scanner providers."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from id_detector.pricing import load_pricing

TransformPolicy = Literal["off", "rescan_only", "global"]

DEFAULT_TRANSFORM_RATES_E4 = (9_200, 9_600, 10_400, 10_800)
DEFAULT_TRANSFORM_SEMITONES = (-2, -1, 1, 2)
DEFAULT_WINDOW_MS = 12_000
# rev 5.2: the generation-0 default stays 12 s / 9 s.  The Stage 4b benchmark's denser 12 s / 5 s
# schedule is recorded below as the *rescan* policy, because the provisional tier thresholds were
# calibrated against 9 s hops and a denser hop inflates T without adding independent evidence.
DEFAULT_HOP_MS = 9_000
DEFAULT_PHASE_MS = 0
DEFAULT_RESCAN_WINDOW_MS = 12_000
DEFAULT_RESCAN_HOP_MS = 5_000
DEFAULT_RESCAN_PHASE_MS = 0
#: Plan rev 5.2: the generation loop stops after this many rescan generations.  This is the value
#: the frozen profiles certify (rescans ON) from the synthetic corpus, and it stays fixed so those
#: profiles remain byte-for-byte reproducible.
DEFAULT_MAX_GENERATIONS = 3
#: Live default for real analyses (CLI/web): rescans OFF.  On four real owner mixes the transform
#: rescans recovered ZERO tracks gen0 missed while injecting famous-track phantoms at 5-65x the
#: request cost (a 46-min mix took ~3h and drained the 2000-request budget).  So live runs default
#: to 0; a config ``[rescan] max_generations`` or ``--max-generations N`` opts back in.
LIVE_DEFAULT_MAX_GENERATIONS = 0
#: Seek lead-in applied by the web page and exports (jump this many ms before the proved start).
DEFAULT_LEAD_IN_MS = 5_000
#: Default same-exact-track display bridge: two appearances of one track (equal work key) up to this
#: far apart, with no different confident track between them, stack into one collapsed row (~3 min).
DEFAULT_SAME_TRACK_BRIDGE_MS = 180_000
#: Minimum proved on-air duration for a low/medium-confidence track to be LISTED in the tracklist.
#: A real track in a DJ set plays for minutes; most false positives are a single ~12 s window.
#: Empirically (Nathan's benchmark set) a 30 s floor cuts false positives ~5x while keeping recall.
#: A ``likely``/``verified`` badge or a corroborating text hint bypasses this floor.  0 disables it.
DEFAULT_PRESENT_MIN_TRACK_MS = 30_000
#: Default per-run Shazam request budget (a hard ceiling on billable/physical attempts).
DEFAULT_MAX_REQUESTS = 2_000
#: Recognition pacing (runtime performance knobs; they change speed only, never results, which are
#: content-addressed).  ``requests_per_minute`` is the *ceiling* admission rate — the adaptive
#: limiter starts here and backs off automatically when Shazam's free endpoint returns 429s, so a
#: higher ceiling is safe.  ``concurrency`` is how many recognitions may be in flight at once.
#: Historic behaviour was 18/min, strictly serial; these defaults are a safe ~2-3x speed-up.
DEFAULT_SHAZAM_REQUESTS_PER_MINUTE = 45
DEFAULT_RECOGNISE_CONCURRENCY = 3
#: Ceiling of the Deep primary's AudD token bucket (plan §2.3.1: concurrency 4 + token bucket; the
#: concurrency itself is recipe data).  AudD has no published per-token rate for the clip
#: endpoint, so this is a courtesy ceiling that the AIMD bucket lowers on a 429/503; 400 windows
#: at 120/min is the ~4 min primary of plan §2.3.6.  Speed only, never results.
DEFAULT_AUDD_REQUESTS_PER_MINUTE = 120
#: Cache TTLs (plan): a positive match is trusted for 180 days, a ``no_match`` for 30 days.
DEFAULT_CACHE_POSITIVE_MAX_AGE_DAYS = 180
DEFAULT_CACHE_NO_MATCH_MAX_AGE_DAYS = 30
#: Every hint connector a ``[hints]`` table may switch on or off by name.
HINT_CONNECTORS: tuple[str, ...] = (
    "sc_comments",
    "mixesdb",
    "yt_comments",
    "mixcloud",
    "tl1001",
    "pointer_import",
)
#: Connectors that stay off unless the ``[hints]`` table names them ``true``.  ``tl1001`` is
#: default-disabled (plan 0a-iii): 1001tracklists is JS-gated, so its title search is dead weight
#: and one more third party contacted per run.
DEFAULT_DISABLED_HINT_CONNECTORS: frozenset[str] = frozenset({"tl1001"})


class ProviderUnavailable(RuntimeError):
    """Raised when a provider cannot run in the current environment."""


class ProviderProtocolError(RuntimeError):
    """Raised for a known provider rejection or malformed response."""


class AmbiguousProviderOutcome(RuntimeError):
    """The request may have reached the provider, but no response was received."""


class UploadPermissionError(PermissionError):
    """Raised before I/O when both upload consent gates are not open."""


@dataclass(frozen=True)
class ProviderCapability:
    provider: str
    capability: str
    available: bool
    detail: str


@dataclass(frozen=True)
class AppConfig:
    """Validated non-secret application configuration."""

    allow_third_party_upload: bool = False
    transforms_policy: TransformPolicy = "rescan_only"
    transform_rates_e4: tuple[int, ...] = DEFAULT_TRANSFORM_RATES_E4
    transform_semitones: tuple[int, ...] = DEFAULT_TRANSFORM_SEMITONES
    window_ms: int = DEFAULT_WINDOW_MS
    hop_ms: int = DEFAULT_HOP_MS
    phase_ms: int = DEFAULT_PHASE_MS
    rescan_window_ms: int = DEFAULT_RESCAN_WINDOW_MS
    rescan_hop_ms: int = DEFAULT_RESCAN_HOP_MS
    rescan_phase_ms: int = DEFAULT_RESCAN_PHASE_MS
    rescan_max_generations: int = LIVE_DEFAULT_MAX_GENERATIONS
    default_profile: str | None = None
    max_requests: int = DEFAULT_MAX_REQUESTS
    pricing_version: str = "v1"
    audd_usd_e6_per_request: int = 5_000
    bill_on_throttle: bool = False
    max_usd_e2: int | None = None
    serve_free_from_deep: bool = False
    deep_primary_density: int = 1
    audd_requests_per_minute: int = DEFAULT_AUDD_REQUESTS_PER_MINUTE
    shazam_requests_per_minute: int = DEFAULT_SHAZAM_REQUESTS_PER_MINUTE
    recognise_concurrency: int = DEFAULT_RECOGNISE_CONCURRENCY
    lead_in_ms: int = DEFAULT_LEAD_IN_MS
    collapse: bool = True
    same_track_bridge_ms: int = DEFAULT_SAME_TRACK_BRIDGE_MS
    present_min_track_ms: int = DEFAULT_PRESENT_MIN_TRACK_MS
    cache_positive_max_age_days: int = DEFAULT_CACHE_POSITIVE_MAX_AGE_DAYS
    cache_no_match_max_age_days: int = DEFAULT_CACHE_NO_MATCH_MAX_AGE_DAYS
    hints_enabled: bool = True
    disabled_hint_connectors: frozenset[str] = DEFAULT_DISABLED_HINT_CONNECTORS

    @property
    def cache_positive_max_age_seconds(self) -> int:
        return self.cache_positive_max_age_days * 24 * 60 * 60

    @property
    def cache_no_match_max_age_seconds(self) -> int:
        return self.cache_no_match_max_age_days * 24 * 60 * 60

    @classmethod
    def load(cls, path: Path | None, *, pricing_path: Path | None = None) -> AppConfig:
        """Parse the owner's config; money fields always come from the pricing authority.

        ``pricing_path`` defaults to the repository ``pricing.toml`` resolved from the package, so
        prices never depend on the working directory; tests pass an explicit file.
        """

        pricing = load_pricing(pricing_path)
        if path is None or not path.is_file():
            return cls(
                pricing_version=pricing.pricing_version,
                audd_usd_e6_per_request=pricing.audd_usd_e6_per_request,
                bill_on_throttle=pricing.bill_on_throttle,
                max_usd_e2=pricing.max_usd_e2,
                serve_free_from_deep=pricing.serve_free_from_deep,
            )
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
        value = payload.get("allow_third_party_upload", False)
        if not isinstance(value, bool):
            raise ValueError("allow_third_party_upload must be true or false")
        transforms = payload.get("transforms", {})
        schedule = payload.get("schedule", {})
        rescan = payload.get("rescan", {})
        cache = payload.get("cache", {})
        hints = payload.get("hints", {})
        present = payload.get("present", {})
        recognise = payload.get("recognise", {})
        deep = payload.get("deep", {})
        if not isinstance(transforms, dict):
            raise ValueError("transforms must be a TOML table")
        if not isinstance(recognise, dict):
            raise ValueError("recognise must be a TOML table")
        if not isinstance(schedule, dict):
            raise ValueError("schedule must be a TOML table")
        if not isinstance(rescan, dict):
            raise ValueError("rescan must be a TOML table")
        if not isinstance(cache, dict):
            raise ValueError("cache must be a TOML table")
        if not isinstance(hints, dict):
            raise ValueError("hints must be a TOML table")
        if not isinstance(present, dict):
            raise ValueError("present must be a TOML table")
        if not isinstance(deep, dict):
            raise ValueError("deep must be a TOML table")
        unknown_deep = sorted(set(deep) - {"primary_density", "audd_requests_per_minute"})
        if unknown_deep:
            raise ValueError(f"unknown deep setting: {', '.join(unknown_deep)}")
        if "serve_free_from_deep" in cache:
            # Launch-controlled (L3), so it is set in the pricing authority only: an owner config
            # able to override it could serve Deep results to Free requests while pricing.toml
            # still says no (plan §3.3, §3.4).
            raise ValueError("serve_free_from_deep is set in pricing.toml, not cache")
        deep_primary_density = deep.get("primary_density", 1)
        if isinstance(deep_primary_density, bool) or deep_primary_density not in {1, 2}:
            raise ValueError("deep.primary_density must be 1 or 2")
        audd_requests_per_minute = _positive_integer(
            deep.get("audd_requests_per_minute", DEFAULT_AUDD_REQUESTS_PER_MINUTE),
            "deep",
            "audd_requests_per_minute",
        )
        policy = transforms.get("policy", "rescan_only")
        if policy not in {"off", "rescan_only", "global"}:
            raise ValueError("transforms.policy must be off, rescan_only, or global")
        rates = _integer_tuple(
            transforms.get("rate_e4", list(DEFAULT_TRANSFORM_RATES_E4)),
            name="transforms.rate_e4",
        )
        semitones = _integer_tuple(
            transforms.get("semitones", list(DEFAULT_TRANSFORM_SEMITONES)),
            name="transforms.semitones",
        )
        if any(rate <= 0 for rate in rates):
            raise ValueError("transforms.rate_e4 values must be positive")
        if any(semitone == 0 for semitone in semitones):
            raise ValueError("transforms.semitones must not contain zero")
        window_ms, hop_ms, phase_ms = _schedule_table(
            schedule,
            "schedule",
            defaults=(DEFAULT_WINDOW_MS, DEFAULT_HOP_MS, DEFAULT_PHASE_MS),
        )
        rescan_window_ms, rescan_hop_ms, rescan_phase_ms = _schedule_table(
            rescan,
            "rescan",
            defaults=(
                DEFAULT_RESCAN_WINDOW_MS,
                DEFAULT_RESCAN_HOP_MS,
                DEFAULT_RESCAN_PHASE_MS,
            ),
        )
        max_generations = rescan.get("max_generations", LIVE_DEFAULT_MAX_GENERATIONS)
        if isinstance(max_generations, bool) or not isinstance(max_generations, int):
            raise ValueError("rescan.max_generations must be a non-negative integer")
        if max_generations < 0:
            raise ValueError("rescan.max_generations must be a non-negative integer")
        default_profile = payload.get("default_profile")
        if default_profile is not None and (
            not isinstance(default_profile, str) or not default_profile.strip()
        ):
            raise ValueError("default_profile must be a non-empty string or absent")
        max_requests = _positive_integer(
            payload.get("max_requests", DEFAULT_MAX_REQUESTS), "config", "max_requests"
        )
        requests_per_minute = _positive_integer(
            recognise.get("requests_per_minute", DEFAULT_SHAZAM_REQUESTS_PER_MINUTE),
            "recognise",
            "requests_per_minute",
        )
        recognise_concurrency = _positive_integer(
            recognise.get("concurrency", DEFAULT_RECOGNISE_CONCURRENCY),
            "recognise",
            "concurrency",
        )
        lead_in_ms = payload.get("lead_in_ms", DEFAULT_LEAD_IN_MS)
        if isinstance(lead_in_ms, bool) or not isinstance(lead_in_ms, int) or lead_in_ms < 0:
            raise ValueError("lead_in_ms must be a non-negative integer")
        collapse = present.get("collapse", True)
        if not isinstance(collapse, bool):
            raise ValueError("present.collapse must be true or false")
        same_track_bridge_ms = present.get("same_track_bridge_ms", DEFAULT_SAME_TRACK_BRIDGE_MS)
        if (
            isinstance(same_track_bridge_ms, bool)
            or not isinstance(same_track_bridge_ms, int)
            or same_track_bridge_ms < 0
        ):
            raise ValueError("present.same_track_bridge_ms must be a non-negative integer")
        present_min_track_ms = present.get("min_track_ms", DEFAULT_PRESENT_MIN_TRACK_MS)
        if (
            isinstance(present_min_track_ms, bool)
            or not isinstance(present_min_track_ms, int)
            or present_min_track_ms < 0
        ):
            raise ValueError("present.min_track_ms must be a non-negative integer")
        positive_days = _positive_integer(
            cache.get("positive_max_age_days", DEFAULT_CACHE_POSITIVE_MAX_AGE_DAYS),
            "cache",
            "positive_max_age_days",
        )
        no_match_days = _positive_integer(
            cache.get("no_match_max_age_days", DEFAULT_CACHE_NO_MATCH_MAX_AGE_DAYS),
            "cache",
            "no_match_max_age_days",
        )
        hints_enabled, disabled_connectors = _hints_table(hints)
        return cls(
            allow_third_party_upload=value,
            transforms_policy=policy,
            transform_rates_e4=rates,
            transform_semitones=semitones,
            window_ms=window_ms,
            hop_ms=hop_ms,
            phase_ms=phase_ms,
            rescan_window_ms=rescan_window_ms,
            rescan_hop_ms=rescan_hop_ms,
            rescan_phase_ms=rescan_phase_ms,
            rescan_max_generations=max_generations,
            default_profile=default_profile,
            max_requests=max_requests,
            pricing_version=pricing.pricing_version,
            audd_usd_e6_per_request=pricing.audd_usd_e6_per_request,
            bill_on_throttle=pricing.bill_on_throttle,
            max_usd_e2=pricing.max_usd_e2,
            serve_free_from_deep=pricing.serve_free_from_deep,
            deep_primary_density=deep_primary_density,
            audd_requests_per_minute=audd_requests_per_minute,
            shazam_requests_per_minute=requests_per_minute,
            recognise_concurrency=recognise_concurrency,
            lead_in_ms=lead_in_ms,
            collapse=collapse,
            same_track_bridge_ms=same_track_bridge_ms,
            present_min_track_ms=present_min_track_ms,
            cache_positive_max_age_days=positive_days,
            cache_no_match_max_age_days=no_match_days,
            hints_enabled=hints_enabled,
            disabled_hint_connectors=disabled_connectors,
        )


def _integer_tuple(value: object, *, name: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a non-empty integer array")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise ValueError(f"{name} must be a non-empty integer array")
    result = tuple(value)
    if len(result) != len(set(result)):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _hints_table(table: dict[str, object]) -> tuple[bool, frozenset[str]]:
    """Read the optional ``[hints]`` table: a global ``enabled`` plus per-connector switches.

    ``[hints] enabled = false`` turns every connector off (equivalent to ``--no-hints``); naming a
    connector with ``false`` turns just that one off, and a default-disabled connector needs an
    explicit ``true``.  Unknown keys are rejected so a typo in a connector name can never silently
    leave a connector running.
    """

    enabled = table.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("hints.enabled must be true or false")
    disabled: set[str] = set(DEFAULT_DISABLED_HINT_CONNECTORS)
    for key, switch in table.items():
        if key == "enabled":
            continue
        if key not in HINT_CONNECTORS:
            known = ", ".join(HINT_CONNECTORS)
            raise ValueError(f"unknown hints connector {key!r}; known: {known}")
        if not isinstance(switch, bool):
            raise ValueError(f"hints.{key} must be true or false")
        if switch:
            disabled.discard(key)
        else:
            disabled.add(key)
    return enabled, frozenset(disabled)


def _positive_integer(value: object, table: str, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{table}.{name} must be a positive integer")
    return value


def _schedule_table(
    table: dict[str, object], name: str, *, defaults: tuple[int, int, int]
) -> tuple[int, int, int]:
    """Validate one ``window_ms``/``hop_ms``/``phase_ms`` TOML table."""

    default_window, default_hop, default_phase = defaults
    window_ms = _positive_integer(table.get("window_ms", default_window), name, "window_ms")
    hop_ms = _positive_integer(table.get("hop_ms", default_hop), name, "hop_ms")
    phase_ms = table.get("phase_ms", default_phase)
    if isinstance(phase_ms, bool) or not isinstance(phase_ms, int) or phase_ms < 0:
        raise ValueError(f"{name}.phase_ms must be a non-negative integer")
    if phase_ms >= hop_ms:
        raise ValueError(f"{name}.phase_ms must be smaller than {name}.hop_ms")
    if window_ms > 12_000:
        raise ValueError(f"{name}.window_ms must not exceed 12000")
    return window_ms, hop_ms, phase_ms


def require_upload_permission(config: AppConfig, cli_confirmation: bool) -> None:
    """Enforce the plan's independent config and per-invocation consent gates."""

    missing: list[str] = []
    if not config.allow_third_party_upload:
        missing.append("allow_third_party_upload = true in config")
    if not cli_confirmation:
        missing.append("--i-own-this-audio-or-have-permission")
    if missing:
        message = "third-party upload refused; required: " + " and ".join(missing)
        raise UploadPermissionError(message)

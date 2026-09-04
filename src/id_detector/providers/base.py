"""Small shared contracts for scanner providers."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
#: Plan rev 5.2: the generation loop stops after this many rescan generations.
DEFAULT_MAX_GENERATIONS = 3
#: Seek lead-in applied by the web page and exports (jump this many ms before the proved start).
DEFAULT_LEAD_IN_MS = 5_000
#: Default per-run Shazam request budget (a hard ceiling on billable/physical attempts).
DEFAULT_MAX_REQUESTS = 2_000
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
    rescan_max_generations: int = DEFAULT_MAX_GENERATIONS
    default_profile: str | None = None
    max_requests: int = DEFAULT_MAX_REQUESTS
    lead_in_ms: int = DEFAULT_LEAD_IN_MS
    collapse: bool = True
    cache_positive_max_age_days: int = DEFAULT_CACHE_POSITIVE_MAX_AGE_DAYS
    cache_no_match_max_age_days: int = DEFAULT_CACHE_NO_MATCH_MAX_AGE_DAYS
    hints_enabled: bool = True
    disabled_hint_connectors: frozenset[str] = frozenset()

    @property
    def cache_positive_max_age_seconds(self) -> int:
        return self.cache_positive_max_age_days * 24 * 60 * 60

    @property
    def cache_no_match_max_age_seconds(self) -> int:
        return self.cache_no_match_max_age_days * 24 * 60 * 60

    @classmethod
    def load(cls, path: Path | None) -> AppConfig:
        if path is None or not path.is_file():
            return cls()
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
        if not isinstance(transforms, dict):
            raise ValueError("transforms must be a TOML table")
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
        max_generations = rescan.get("max_generations", DEFAULT_MAX_GENERATIONS)
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
        lead_in_ms = payload.get("lead_in_ms", DEFAULT_LEAD_IN_MS)
        if isinstance(lead_in_ms, bool) or not isinstance(lead_in_ms, int) or lead_in_ms < 0:
            raise ValueError("lead_in_ms must be a non-negative integer")
        collapse = present.get("collapse", True)
        if not isinstance(collapse, bool):
            raise ValueError("present.collapse must be true or false")
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
            lead_in_ms=lead_in_ms,
            collapse=collapse,
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
    connector with ``false`` turns just that one off.  Unknown keys are rejected so a typo in a
    connector name can never silently leave a connector running.
    """

    enabled = table.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("hints.enabled must be true or false")
    disabled: set[str] = set()
    for key, switch in table.items():
        if key == "enabled":
            continue
        if key not in HINT_CONNECTORS:
            known = ", ".join(HINT_CONNECTORS)
            raise ValueError(f"unknown hints connector {key!r}; known: {known}")
        if not isinstance(switch, bool):
            raise ValueError(f"hints.{key} must be true or false")
        if not switch:
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

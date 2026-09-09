"""Strict loader for the repository's single pricing authority."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _resolve_pricing_path(module_path: Path) -> Path:
    """The pricing authority for this installation: checked-out source first, then the wheel copy.

    Resolution is relative to this module, never to the process working directory, so the price a
    run is charged cannot change with the directory the command was started from. A built wheel
    force-includes the repository file as ``id_detector/resources/pricing.toml`` — a build-time
    copy, so there is no second committed file to drift from the authority — exactly how
    :mod:`id_detector.profiles` resolves the frozen profiles.
    """

    module = module_path.resolve()
    repository = module.parents[2] / "pricing.toml"
    if repository.is_file():
        return repository
    return module.parent / "resources" / "pricing.toml"


#: The pricing authority this process prices runs from.
DEFAULT_PRICING_PATH = _resolve_pricing_path(Path(__file__))


@dataclass(frozen=True)
class FreePricing:
    minutes_per_week: int
    max_mix_minutes: int
    sources: tuple[str, ...]


@dataclass(frozen=True)
class ProPricing:
    price_gbp_month: int
    price_gbp_year: int
    deep_minutes_per_month: int
    max_mix_minutes: int
    sources: tuple[str, ...]


@dataclass(frozen=True)
class PackProductPricing:
    gbp: int
    minutes: int


@dataclass(frozen=True)
class PackPricing:
    small: PackProductPricing


@dataclass(frozen=True)
class PricingConfig:
    pricing_version: str
    audd_usd_e6_per_request: int
    bill_on_throttle: bool
    usd_cap_account_month_e2: int
    usd_cap_global_day_e2: int
    cache_hits_per_day: int
    hints_max_age_days: int
    alias_revalidate_days: int
    serve_free_from_deep: bool
    compat_version: int
    free: FreePricing
    pro: ProPricing
    pack: PackPricing
    max_usd_e2: int | None = None

    @classmethod
    def load(cls, path: Path) -> PricingConfig:
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
        _reject_unknown(
            payload,
            {
                "pricing_version",
                "audd_usd_e6_per_request",
                "bill_on_throttle",
                "max_usd_e2",
                "usd_cap_account_month_e2",
                "usd_cap_global_day_e2",
                "cache_hits_per_day",
                "hints_max_age_days",
                "alias_revalidate_days",
                "serve_free_from_deep",
                "compat_version",
                "free",
                "pro",
                "pack",
            },
            "pricing",
        )
        free = _table(payload, "free")
        pro = _table(payload, "pro")
        pack = _table(payload, "pack")
        small = _table(pack, "small", prefix="pack")
        _reject_unknown(free, {"minutes_per_week", "max_mix_minutes", "sources"}, "free")
        _reject_unknown(
            pro,
            {
                "price_gbp_month",
                "price_gbp_year",
                "deep_minutes_per_month",
                "max_mix_minutes",
                "sources",
            },
            "pro",
        )
        _reject_unknown(pack, {"small"}, "pack")
        _reject_unknown(small, {"gbp", "minutes"}, "pack.small")
        version = payload.get("pricing_version")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("pricing_version must be a non-empty string")
        bill_on_throttle = payload.get("bill_on_throttle")
        serve_free_from_deep = payload.get("serve_free_from_deep")
        if not isinstance(bill_on_throttle, bool):
            raise ValueError("bill_on_throttle must be true or false")
        if not isinstance(serve_free_from_deep, bool):
            raise ValueError("serve_free_from_deep must be true or false")
        max_usd_e2 = payload.get("max_usd_e2")
        if max_usd_e2 is not None:
            max_usd_e2 = _integer(max_usd_e2, "max_usd_e2", minimum=0)
        return cls(
            pricing_version=version,
            audd_usd_e6_per_request=_integer(
                payload.get("audd_usd_e6_per_request"),
                "audd_usd_e6_per_request",
                minimum=1,
            ),
            bill_on_throttle=bill_on_throttle,
            max_usd_e2=max_usd_e2,
            usd_cap_account_month_e2=_integer(
                payload.get("usd_cap_account_month_e2"),
                "usd_cap_account_month_e2",
                minimum=0,
            ),
            usd_cap_global_day_e2=_integer(
                payload.get("usd_cap_global_day_e2"),
                "usd_cap_global_day_e2",
                minimum=0,
            ),
            cache_hits_per_day=_integer(
                payload.get("cache_hits_per_day"), "cache_hits_per_day", minimum=0
            ),
            hints_max_age_days=_integer(
                payload.get("hints_max_age_days"), "hints_max_age_days", minimum=0
            ),
            alias_revalidate_days=_integer(
                payload.get("alias_revalidate_days"), "alias_revalidate_days", minimum=0
            ),
            serve_free_from_deep=serve_free_from_deep,
            compat_version=_integer(payload.get("compat_version"), "compat_version", minimum=1),
            free=FreePricing(
                minutes_per_week=_integer(
                    free.get("minutes_per_week"), "free.minutes_per_week", minimum=0
                ),
                max_mix_minutes=_integer(
                    free.get("max_mix_minutes"), "free.max_mix_minutes", minimum=1
                ),
                sources=_sources(free.get("sources"), "free.sources"),
            ),
            pro=ProPricing(
                price_gbp_month=_integer(
                    pro.get("price_gbp_month"), "pro.price_gbp_month", minimum=0
                ),
                price_gbp_year=_integer(pro.get("price_gbp_year"), "pro.price_gbp_year", minimum=0),
                deep_minutes_per_month=_integer(
                    pro.get("deep_minutes_per_month"),
                    "pro.deep_minutes_per_month",
                    minimum=0,
                ),
                max_mix_minutes=_integer(
                    pro.get("max_mix_minutes"), "pro.max_mix_minutes", minimum=1
                ),
                sources=_sources(pro.get("sources"), "pro.sources"),
            ),
            pack=PackPricing(
                small=PackProductPricing(
                    gbp=_integer(small.get("gbp"), "pack.small.gbp", minimum=0),
                    minutes=_integer(small.get("minutes"), "pack.small.minutes", minimum=1),
                )
            ),
        )


def _reject_unknown(payload: dict[str, Any], expected: set[str], name: str) -> None:
    unknown = sorted(set(payload) - expected)
    if unknown:
        raise ValueError(f"unknown {name} field: {', '.join(unknown)}")


def _table(payload: dict[str, Any], name: str, *, prefix: str = "") -> dict[str, Any]:
    value = payload.get(name)
    dotted = f"{prefix}.{name}" if prefix else name
    if not isinstance(value, dict):
        raise ValueError(f"{dotted} must be a TOML table")
    return value


def _integer(value: object, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "positive" if minimum == 1 else "non-negative"
        raise ValueError(f"{name} must be a {qualifier} integer")
    return value


def _sources(value: object, name: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise ValueError(f"{name} must be a non-empty array of strings")
    return tuple(value)


def load_pricing(path: Path | None = None) -> PricingConfig:
    """Load the pricing authority, defaulting to the repository copy beside this package."""

    return PricingConfig.load(DEFAULT_PRICING_PATH if path is None else path)

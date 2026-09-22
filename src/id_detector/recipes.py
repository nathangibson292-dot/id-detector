"""Frozen scan recipes and their content-derived identities."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, replace
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Literal

RecipeName = Literal["free", "deep"]

#: The fusion rules a result was decided by.  Bump it whenever fusion (``fuse/``) or the
#: presentation floor lists different rows from the same evidence: every stored result then stops
#: being SERVED as it is, and is re-fused offline from its own recorded observations instead
#: (:mod:`id_detector.refusion`) — recognition is never repeated and nothing is re-spent.
#: 3 = hint corroboration by reach and field-level labels; smeared ``likely`` rows are scatter and
#: bury only under proved audio; edge answers; short rows by an artist solidly in the mix.
#: 4 = an unambiguous untimed hint may support a recognised work without changing its timing.
FUSION_VERSION = 4
_FUSION = f"fusion:{FUSION_VERSION}"


def fusion_component(algorithm_version: str | None) -> int | None:
    """The ``N`` of the ``fusion:N`` component of an ``algorithm_version``, if it has one."""

    for part in (algorithm_version or "").split(","):
        name, _, number = part.strip().partition(":")
        if name == "fusion" and number.isdigit():
            return int(number)
    return None


def without_fusion(algorithm_version: str | None) -> tuple[str, ...]:
    """Every other component ("targeting:1"): what must still match for a re-fusion to do."""

    parts = [part.strip() for part in (algorithm_version or "").split(",") if part.strip()]
    return tuple(part for part in parts if part.partition(":")[0] != "fusion")


@dataclass(frozen=True)
class RetryPolicy:
    mode: Literal["existing_limiter", "bounded"]
    retryable_outcomes: tuple[str, ...] = ()
    max_retries: int = 0
    backoff_seconds: tuple[int, ...] = ()


@dataclass(frozen=True)
class Recipe:
    """Every result-affecting scan choice that identifies a recipe."""

    name: RecipeName
    primary_engine: Literal["shazam", "audd"]
    primary_density: int
    secondary_engine: Literal["shazam"] | None
    secondary_clips_per_minute: int | None
    secondary_reserve_fraction: float | None
    secondary_priority: tuple[str, ...]
    eligibility_min_intersection_ms: int | None
    suppressed_min_votes: int | None
    reserve_search_ms: int | None
    reserve_min_separation_ms: int | None
    overlap_min_ms: int | None
    separation_min_ms: int | None
    primary_achieved_fraction: float
    secondary_achieved_fraction: float | None
    anchor_max_ms: int | None
    anchor_slack_ms: int | None
    audd_concurrency: int | None
    retry_policy: Mapping[str, RetryPolicy]
    max_usd_e2: int
    adapter_versions: Mapping[str, int]
    algorithm_version: str
    requires: tuple[str, ...]

    def canonical_payload(self) -> dict[str, Any]:
        payload = {field.name: getattr(self, field.name) for field in fields(self)}
        payload["retry_policy"] = {
            provider: asdict(policy) for provider, policy in sorted(self.retry_policy.items())
        }
        payload["adapter_versions"] = dict(sorted(self.adapter_versions.items()))
        return payload

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @property
    def recipe_id(self) -> str:
        return sha256(self.canonical_json.encode("utf-8")).hexdigest()


FREE_RECIPE = Recipe(
    name="free",
    primary_engine="shazam",
    primary_density=1,
    secondary_engine=None,
    secondary_clips_per_minute=None,
    secondary_reserve_fraction=None,
    secondary_priority=(),
    eligibility_min_intersection_ms=None,
    suppressed_min_votes=None,
    reserve_search_ms=None,
    reserve_min_separation_ms=None,
    overlap_min_ms=None,
    separation_min_ms=None,
    primary_achieved_fraction=0.80,
    secondary_achieved_fraction=None,
    anchor_max_ms=None,
    anchor_slack_ms=None,
    audd_concurrency=None,
    retry_policy=MappingProxyType({"shazam": RetryPolicy(mode="existing_limiter")}),
    max_usd_e2=0,
    adapter_versions=MappingProxyType({"shazam": 1}),
    algorithm_version=_FUSION,
    requires=("shazam_sweep",),
)

DEEP_RECIPE = Recipe(
    name="deep",
    primary_engine="audd",
    primary_density=1,
    secondary_engine="shazam",
    secondary_clips_per_minute=2,
    secondary_reserve_fraction=0.25,
    secondary_priority=(
        "hint_only",
        "listed_not_confident",
        "suppressed_challengeable",
        "blank",
    ),
    eligibility_min_intersection_ms=4_000,
    suppressed_min_votes=2,
    reserve_search_ms=45_000,
    reserve_min_separation_ms=30_000,
    overlap_min_ms=6_000,
    separation_min_ms=60_000,
    primary_achieved_fraction=0.95,
    secondary_achieved_fraction=0.80,
    anchor_max_ms=86_400_000,
    anchor_slack_ms=12_000,
    audd_concurrency=4,
    retry_policy=MappingProxyType(
        {
            "audd": RetryPolicy(
                mode="bounded",
                retryable_outcomes=(
                    "connect_error",
                    "timeout_pre",
                    "http_429",
                    "http_503",
                ),
                max_retries=3,
                backoff_seconds=(1, 2, 4),
            )
        }
    ),
    max_usd_e2=900,
    adapter_versions=MappingProxyType({"audd_clip": 2, "shazam": 1}),
    algorithm_version=f"targeting:1,{_FUSION}",
    requires=("audd_sweep", "shazam_secondary"),
)

FREE = FREE_RECIPE
DEEP = DEEP_RECIPE
RECIPES: Mapping[RecipeName, Recipe] = MappingProxyType({"free": FREE_RECIPE, "deep": DEEP_RECIPE})


def get_recipe(name: str, *, primary_density: int = 1) -> Recipe:
    """Return a frozen recipe, deriving the supported Deep density variant when requested."""

    normalised = name.strip().casefold()
    if normalised not in RECIPES:
        raise ValueError(f"unknown recipe {name!r} (choose free or deep)")
    recipe = RECIPES[normalised]  # type: ignore[index]
    if recipe.name == "free":
        if primary_density != 1:
            raise ValueError("the free recipe has primary_density=1")
        return recipe
    if primary_density not in {1, 2}:
        raise ValueError("deep.primary_density must be 1 or 2")
    return recipe if primary_density == 1 else replace(recipe, primary_density=primary_density)

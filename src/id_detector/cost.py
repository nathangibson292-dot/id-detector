"""Read-only Deep cost previews. No ingest, providers, cache repair or money writes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from id_detector.contracts import PcmRecord, SourceRecord
from id_detector.ingest import canonicalize_url
from id_detector.io import read_text
from id_detector.money import ceil_e2
from id_detector.present.bundles import result_dir, run_metadata
from id_detector.providers.base import AppConfig
from id_detector.recipes import get_recipe
from id_detector.windows import WindowSchedule, schedule_windows


@dataclass(frozen=True)
class CachedMix:
    directory: Path
    source: SourceRecord
    duration_ms: int | None
    scanned: str


def cached_mix(root: Path, target: str) -> CachedMix | None:
    """Read source and decode records even when no result/index/original remains."""
    canonical = canonicalize_url(target)[0]
    for path in sorted(root.glob("*/*/ingest/source.json")):
        if not path.resolve().is_relative_to(root.resolve()):
            continue
        try:
            source = SourceRecord.model_validate_json(read_text(path))
            if target != source.media_key and not {target, canonical}.intersection(
                {source.input_url, source.canonical_url}
            ):
                continue
            directory = path.parents[1]
            duration = None
            try:
                pcm = PcmRecord.model_validate_json(read_text(directory / "decode/pcm.json"))
                if pcm.media_key == source.media_key:
                    duration = pcm.pcm.duration_ms
            except (OSError, ValueError):
                pass
            metadata = run_metadata(directory)
            scanned = "No stored scan found."
            if (result_dir(directory) / "tracklist.json").is_file():
                recipe = (
                    metadata.get("achieved")
                    or metadata.get("achieved_recipe")
                    or metadata.get("requested_recipe")
                )
                compatibility = metadata.get("compatibility") or {}
                recipe = recipe or compatibility.get("recipe_name") or "unknown recipe"
                scanned = f"Stored scan: {recipe} ({metadata.get('status', 'unknown status')})."
            return CachedMix(directory, source, duration, scanned)
        except (OSError, ValueError):
            continue
    return None


@dataclass(frozen=True)
class CostEstimate:
    duration_ms: int
    windows: int
    density: int
    unit_usd_e6: int
    ceiling_e2: int
    configured_cap_e2: int | None

    @property
    def clips(self) -> int:
        return (self.windows + self.density - 1) // self.density

    @property
    def estimate_e6(self) -> int:
        return self.clips * self.unit_usd_e6

    @property
    def reservation_e6(self) -> int:
        return (self.estimate_e6 * 105 + 99) // 100

    @property
    def cap_e2(self) -> int:
        return (
            min(self.ceiling_e2, self.configured_cap_e2 or self.ceiling_e2)
            if self.configured_cap_e2 != 0
            else 0
        )

    def render(self, scanned: str = "Scan history unavailable.") -> str:
        seconds = self.duration_ms // 1000
        length = f"{seconds // 3600:d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
        cap = "none" if self.configured_cap_e2 is None else f"${self.configured_cap_e2 / 100:.2f}"
        alternative = ((self.windows + 1) // 2) * self.unit_usd_e6
        lines = [
            f"Mix length: {length} ({self.duration_ms / 60000:.2f} minutes).",
            f"Deep at density {self.density}: approximately {self.clips} paid clips, "
            f"estimated ${ceil_e2(self.estimate_e6) / 100:.2f}.",
            f"Density 2: approximately {(self.windows + 1) // 2} paid clips, "
            f"estimated ${ceil_e2(alternative) / 100:.2f}.",
            f"Recipe ceiling: ${self.ceiling_e2 / 100:.2f}; configured per-run cap: {cap}; "
            f"effective cap: ${self.cap_e2 / 100:.2f}.",
            f"Reservation including 5% headroom: ${ceil_e2(self.reservation_e6) / 100:.2f}.",
            scanned,
            "Estimate only, not a promise: cached answers, duplicate clips and provider "
            "outcomes can change the spend.",
        ]
        if ceil_e2(self.reservation_e6) > self.cap_e2:
            lines.append("This plan exceeds the cap and will be refused before paid dispatch.")
        return "\n".join(lines)


def estimate(duration_ms: int, config: AppConfig, *, windows: int | None = None) -> CostEstimate:
    recipe = get_recipe("deep", primary_density=config.deep_primary_density)
    count = (
        windows
        if windows is not None
        else len(
            schedule_windows(
                duration_ms, WindowSchedule(config.window_ms, config.hop_ms, config.phase_ms)
            )
        )
    )
    return CostEstimate(
        duration_ms,
        count,
        recipe.primary_density,
        config.audd_usd_e6_per_request,
        recipe.max_usd_e2,
        config.max_usd_e2,
    )

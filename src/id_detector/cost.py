"""Read-only Deep cost previews. No ingest, providers, cache repair or money writes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from id_detector.additive import PaidPlan
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
            "Deep runs the free pass first and then pays only for the stretches the free result "
            "leaves blank, so the real figure is known only after the free pass. The whole mix is "
            "the ceiling:",
            f"Ceiling at density {self.density}: {self.clips} paid clips, "
            f"${ceil_e2(self.estimate_e6) / 100:.2f}.",
            f"Ceiling at density 2: {(self.windows + 1) // 2} paid clips, "
            f"${ceil_e2(alternative) / 100:.2f}.",
            f"Recipe ceiling: ${self.ceiling_e2 / 100:.2f}; configured per-run cap: {cap}; "
            f"effective cap: ${self.cap_e2 / 100:.2f}.",
            f"Ceiling reservation including 5% headroom: "
            f"${ceil_e2(self.reservation_e6) / 100:.2f}.",
            scanned,
            "Stored paid answers are never bought again, so a mix Deep-scanned before costs less.",
        ]
        if ceil_e2(self.reservation_e6) > self.cap_e2:
            lines.append("This plan exceeds the cap and will be refused before paid dispatch.")
        return "\n".join(lines)


def _clock(ms: int) -> str:
    seconds = max(0, ms) // 1000
    return f"{seconds // 3600:d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def render_plan(plan: PaidPlan, config: AppConfig, scanned: str = "") -> str:
    """The EXACT paid step of a Deep run, known once the free pass has run."""

    recipe = get_recipe("deep", primary_density=config.deep_primary_density)
    configured = config.max_usd_e2
    effective = min(recipe.max_usd_e2, configured) if configured is not None else recipe.max_usd_e2
    cap = "none" if configured is None else f"${configured / 100:.2f}"
    lines = [
        f"Deep would check {plan.spans} gaps for about ${ceil_e2(plan.estimate_e6) / 100:.2f}.",
        f"The free pass lists nothing for {_clock(plan.target_ms)} of this "
        f"{_clock(plan.duration_ms)} mix; Deep checks only those gaps: {plan.clips} paid clips "
        f"at density {plan.density}, {plan.cached} already answered (free), "
        f"{plan.to_send} to send.",
        f"Exact price: {plan.to_send} x ${plan.unit_usd_e6 / 1e6:.4f} = "
        f"${ceil_e2(plan.estimate_e6) / 100:.2f}; reservation including 5% headroom: "
        f"${ceil_e2(plan.reservation_e6) / 100:.2f}.",
        f"Recipe ceiling: ${recipe.max_usd_e2 / 100:.2f}; configured per-run cap: {cap}; "
        f"effective cap: ${effective / 100:.2f}.",
    ]
    if scanned:
        lines.append(scanned)
    if ceil_e2(plan.reservation_e6) > effective:
        lines.append("This plan exceeds the cap and will be refused before paid dispatch.")
    return "\n".join(lines)


def free_scanned_plan(mix: CachedMix, config: AppConfig) -> PaidPlan | None:
    """The exact Deep paid plan of a mix already Free-scanned, computed offline; else ``None``.

    Today's fusion of the stored Free result's own proven evidence, the Deep policy's targets over
    it, and the paid answers already stored for those clips.  Reads files only.
    """

    from id_detector.additive import fuse_offline, plan_paid_step, proven_free_results
    from id_detector.contracts import WindowRecord
    from id_detector.io import path_is_file
    from id_detector.recipes import paid_policy

    if mix.duration_ms is None:
        return None
    windows_path = mix.directory / "windows" / "windows.gen0.jsonl"
    if not path_is_file(windows_path):
        return None
    try:
        windows = tuple(
            WindowRecord.model_validate_json(line)
            for line in read_text(windows_path).splitlines()
            if line.strip()
        )
    except (OSError, ValueError):
        return None
    recipe = get_recipe("deep", primary_density=config.deep_primary_density)
    for observations, hints, _source in proven_free_results(mix.directory, windows):
        free = fuse_offline(
            media_key=mix.source.media_key,
            duration_ms=mix.duration_ms,
            observations=observations,
            windows=windows,
            hints=hints,
            config=config,
        )
        plan, _targets = plan_paid_step(
            policy=paid_policy(recipe) or "gaps",
            free_episodes=free.episodes.episodes,
            windows=windows,
            duration_ms=mix.duration_ms,
            density=recipe.primary_density,
            unit_usd_e6=config.audd_usd_e6_per_request,
            media_dir=mix.directory,
        )
        return plan
    return None


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

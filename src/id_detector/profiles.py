"""Frozen recognition profiles (Stage 4d).

A *profile* is an immutable, versioned artefact (``profiles/<name>-v<K>.json``) that fixes which
engines and which free features the pipeline turns on.  The two profiles named by the plan are
``free`` and ``max_accuracy``.  Both are derived **mechanically** from two committed reports:

* the Stage 4c ablation report (``data/corpus/controlled-synth-1/ablations.json``), which measures,
  on the controlled oracle, the effect of each free feature with paired one-sided 95% cluster
  lower bounds; and
* the Stage 3 shortlist (``data/corpus/controlled-synth-1/shortlist.json``), which records which
  engines are actually available and what the paid engines would cost.

Every number a profile states about the world is read from one of those two reports and recorded
in a :class:`~id_detector.contracts.ProfileEvidence` entry, so a profile can be re-derived
byte-for-byte from the evidence.  Concrete geometry (the 12 s / 9 s generation-0 schedule, the
12 s / 5 s rescan policy, ``max_generations = 3``) and the budget ceilings are configuration the
plan fixes, not claims about the world, so they carry plan citations in ``notes`` rather than
report evidence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    ProfileBudget,
    ProfileCostReport,
    ProfileEngine,
    ProfileEvidence,
    ProfileFeature,
    ProfilePaidEstimate,
    ProfileProvenance,
    ProfileRecord,
    ProfileRescan,
    ProfileSchedule,
    compose_natural_key,
    make_id,
)
from id_detector.io import (
    atomic_write_json,
    canonical_json_bytes,
    read_text,
    sha256_file,
    write_completion_sidecar,
)
from id_detector.providers.base import (
    DEFAULT_HOP_MS,
    DEFAULT_MAX_GENERATIONS,
    DEFAULT_PHASE_MS,
    DEFAULT_RESCAN_HOP_MS,
    DEFAULT_RESCAN_PHASE_MS,
    DEFAULT_RESCAN_WINDOW_MS,
    DEFAULT_WINDOW_MS,
    AppConfig,
)

PROFILE_NAMES = ("free", "max_accuracy")

# Budget constants the plan fixes (not evidence): the free-profile Shazam rate ceiling from the
# plan's throttling risk row ("<= 18 req/min"), and the per-media request ceiling from the
# ``analyse`` default.  Recorded in the profile's ``notes`` as plan provenance.
SHAZAM_REQUESTS_PER_MINUTE = 18
MAX_REQUESTS_PER_MEDIA = 2_000

_VERSION_RE = re.compile(r"-v(\d+)$")


# --------------------------------------------------------------------------------------------------
# Evidence-cited report reading
# --------------------------------------------------------------------------------------------------
def _dig(report: Any, path: str) -> Any:
    """Return the value at a dotted ``path`` (integer components index into lists)."""

    current = report
    for part in path.split("."):
        current = current[int(part)] if part.lstrip("-").isdigit() else current[part]
    return current


@dataclass
class _Citer:
    """Read a report field and append the exact value read as a :class:`ProfileEvidence`."""

    report_key: str
    report: dict[str, Any]

    def cite(self, path: str, sink: list[ProfileEvidence]) -> Any:
        value = _dig(self.report, path)
        sink.append(ProfileEvidence(report=self.report_key, field=path, value=value))
        return value


def _improves_at_non_inferior_precision(comp: dict[str, Any]) -> bool:
    """True iff a comparison has a positive lower bound on any gain and non-inferior precision.

    "Non-inferior precision" is the plan's paired one-sided cluster test with a 1 pp margin,
    stored per metric as ``pass`` in the ablation report.
    """

    gained = any(
        comp[metric]["lower_bound_e4"] > 0
        for metric in ("best_start_p90", "work_recall", "segment_recall")
    )
    non_inferior = bool(comp["work_precision"]["pass"]) and bool(comp["segment_precision"]["pass"])
    return gained and non_inferior


# --------------------------------------------------------------------------------------------------
# Engine rows
# --------------------------------------------------------------------------------------------------
def _shortlist_engine(shortlist: dict[str, Any], provider: str) -> tuple[int, dict[str, Any]]:
    for index, engine in enumerate(shortlist["engines"]):
        if engine["provider"] == provider:
            return index, engine
    raise KeyError(f"shortlist has no engine {provider}")


def _engine_rows(
    *,
    name: str,
    shortlist: dict[str, Any],
    shortlist_citer: _Citer,
    panako_index: PanakoProfileInput | None = None,
) -> list[ProfileEngine]:
    free_only = name == "free"
    rows: list[ProfileEngine] = []

    shazam_index, _ = _shortlist_engine(shortlist, "shazam")
    shazam_evidence: list[ProfileEvidence] = []
    shazam_status = shortlist_citer.cite(f"engines.{shazam_index}.status", shazam_evidence)
    shortlist_citer.cite(f"engines.{shazam_index}.cost.physical_attempts", shazam_evidence)
    shortlist_citer.cite(f"engines.{shazam_index}.match_count", shazam_evidence)
    rows.append(
        ProfileEngine(
            provider="shazam",
            capability="clip_recognizer",
            cost_class="free",
            enabled=True,
            eligible_when_available=True,
            eligibility_condition="",
            status=str(shazam_status),
            reason=(
                "The only free clip recognizer and the only available production engine "
                "(Stage 3 shortlist ran it live over the whole corpus). It carries every "
                "profile. Its controlled-stratum accuracy is not measured -- the synthetic "
                "corpus is not in any commercial catalogue -- so the feature toggles, not the "
                "engine, are what the ablations certify."
            ),
            evidence=shazam_evidence,
        )
    )

    for provider in ("audd", "acrcloud"):
        index, engine = _shortlist_engine(shortlist, provider)
        evidence: list[ProfileEvidence] = []
        status = shortlist_citer.cite(f"engines.{index}.status", evidence)
        shortlist_citer.cite(f"engines.{index}.expected_trial_cost_usd_e2", evidence)
        rows.append(
            ProfileEngine(
                provider=provider,
                capability="file_scanner",
                cost_class="paid",
                enabled=False,
                eligible_when_available=not free_only,
                eligibility_condition=(
                    ""
                    if free_only
                    else f"set the {provider.upper()} credentials (Stage 3 procedure) and open "
                    "both third-party-upload consent gates; paid file_scanner"
                ),
                status=str(status),
                reason=(
                    "Paid file scanner. The free profile enables only free engines (plan "
                    "principle 8), so it is out of scope here; it becomes eligible in "
                    "max_accuracy once credentials exist."
                    if free_only
                    else "Paid file scanner, not evaluated because no credentials are set. "
                    "Eligible when available: it would run over the whole set, unsuppressed, "
                    "once credentials and the upload consent gates are present."
                ),
                evidence=evidence,
            )
        )

    panako_shortlist_index, _ = _shortlist_engine(shortlist, "panako")
    panako_evidence: list[ProfileEvidence] = []
    panako_status = shortlist_citer.cite(
        f"engines.{panako_shortlist_index}.status", panako_evidence
    )
    # rev 5.2 / Stage 8: Panako becomes an *enabled* independent engine in max_accuracy only when a
    # JDK and a built reference pool are present (a v2 profile). The free profile and the v1
    # max_accuracy derivation are unaffected: with ``panako_index=None`` this row is byte-for-byte
    # the Stage 4d row.
    # The reference pool is a local operator artefact, not a field of either committed report, so
    # its identity is recorded in ``status``/``reason`` rather than as report-cited evidence (the
    # ``evidence`` list stays confined to the shortlist status, keeping the profile re-derivable).
    panako_available = panako_index is not None and name == "max_accuracy"
    rows.append(
        ProfileEngine(
            provider="panako",
            capability="local_index_query",
            cost_class="self_hosted_free",
            enabled=panako_available,
            eligible_when_available=True,
            eligibility_condition=(
                ""
                if panako_available
                else "install a JDK >= 11 and build a reference pool (plan Stage 8); free, "
                "self-hosted"
            ),
            status=(
                f"enabled ({panako_index.resource_count} reference tracks, JDK "
                f"{panako_index.jdk_version})"
                if panako_available
                else str(panako_status)
            ),
            reason=(
                "Free, self-hosted reference-pool matcher, enabled in max_accuracy because a JDK "
                f"({panako_index.jdk_version}) and a built reference pool (index_id "
                f"{panako_index.index_id}, {panako_index.resource_count} tracks) are present. It "
                "runs over the whole set as an independent source, unsuppressed, exactly like the "
                "file-scanner path."
                if panako_available
                else "Free, self-hosted reference-pool matcher, excluded from v1 because no JDK "
                "is installed. Eligible when available in either profile once a JDK exists; until "
                "then reference-pool recognition is excluded."
            ),
            evidence=panako_evidence,
        )
    )
    return rows


# --------------------------------------------------------------------------------------------------
# Feature rows (mechanical rules over the ablation comparisons)
# --------------------------------------------------------------------------------------------------
def _feature_rows(ablations: dict[str, Any], citer: _Citer) -> tuple[list[ProfileFeature], str]:
    comparisons = ablations["comparisons"]
    features: list[ProfileFeature] = []

    # -- rescans -----------------------------------------------------------------------------------
    rescans_ev: list[ProfileEvidence] = []
    citer.cite("comparisons.rescans_on_minus_off.best_start_p90.lower_bound_e4", rescans_ev)
    citer.cite("comparisons.rescans_on_minus_off.work_recall.lower_bound_e4", rescans_ev)
    citer.cite("comparisons.rescans_on_minus_off.segment_recall.lower_bound_e4", rescans_ev)
    citer.cite("comparisons.rescans_on_minus_off.work_precision.pass", rescans_ev)
    citer.cite("comparisons.rescans_on_minus_off.segment_precision.pass", rescans_ev)
    rescans_on = _improves_at_non_inferior_precision(comparisons["rescans_on_minus_off"])
    features.append(
        ProfileFeature(
            name="rescans",
            enabled=rescans_on,
            certified=rescans_on,
            setting={"enabled": rescans_on},
            decision=(
                "Enabled: rescans are the only thing that moves the proved start bound "
                "(best_start p90 -55.55% relative, lower bound +5555 e4) and lift work/segment "
                "recall (+470/+374 e4 lower bound) with precision non-inferior."
                if rescans_on
                else "Left off: no positive lower bound at non-inferior precision."
            ),
            evidence=rescans_ev,
        )
    )

    # -- novelty triggers (a rescan sub-feature) ---------------------------------------------------
    novelty_ev: list[ProfileEvidence] = []
    citer.cite("comparisons.novelty_on_minus_off.work_recall.lower_bound_e4", novelty_ev)
    citer.cite("comparisons.novelty_on_minus_off.segment_recall.lower_bound_e4", novelty_ev)
    citer.cite("comparisons.novelty_on_minus_off.work_precision.pass", novelty_ev)
    citer.cite("comparisons.novelty_on_minus_off.segment_precision.pass", novelty_ev)
    novelty_on = _improves_at_non_inferior_precision(comparisons["novelty_on_minus_off"])
    features.append(
        ProfileFeature(
            name="novelty",
            enabled=novelty_on,
            certified=novelty_on,
            setting={"enabled": novelty_on},
            decision=(
                "Enabled: novelty change points are the only trigger that reaches a rate- or "
                "pitch-shifted track with no generation-0 evidence, lifting work/segment recall "
                "(+473/+377 e4 lower bound) with precision non-inferior."
                if novelty_on
                else "Left off: no positive lower bound at non-inferior precision."
            ),
            evidence=novelty_ev,
        )
    )

    # -- transforms --------------------------------------------------------------------------------
    transforms_ev: list[ProfileEvidence] = []
    citer.cite(
        "comparisons.transforms_rescan_only_minus_off.work_recall.lower_bound_e4", transforms_ev
    )
    citer.cite(
        "comparisons.transforms_rescan_only_minus_off.segment_recall.lower_bound_e4", transforms_ev
    )
    citer.cite("comparisons.transforms_rescan_only_minus_off.segment_precision.pass", transforms_ev)
    citer.cite(
        "comparisons.transforms_global_minus_rescan_only.segment_recall.lower_bound_e4",
        transforms_ev,
    )
    citer.cite(
        "comparisons.transforms_global_minus_rescan_only.segment_precision.pass", transforms_ev
    )
    rescan_only_beats_off = _improves_at_non_inferior_precision(
        comparisons["transforms_rescan_only_minus_off"]
    )
    global_beats_rescan_only = _improves_at_non_inferior_precision(
        comparisons["transforms_global_minus_rescan_only"]
    )
    if global_beats_rescan_only:
        transforms_policy = "global"
    elif rescan_only_beats_off:
        transforms_policy = "rescan_only"
    else:
        transforms_policy = "off"
    features.append(
        ProfileFeature(
            name="transforms",
            enabled=transforms_policy != "off",
            certified=transforms_policy != "off",
            setting={"policy": transforms_policy},
            decision=(
                "Enabled at rescan_only: applying the transform grid on rescans lifts work/segment "
                "recall (+470/+371 e4 lower bound) over off with precision non-inferior. Not "
                "escalated to global: global buys no recall over rescan_only (0 e4 lower bound) "
                "and fails segment-precision non-inferiority."
                if transforms_policy == "rescan_only"
                else f"Frozen at policy={transforms_policy}."
            ),
            evidence=transforms_ev,
        )
    )

    # -- schedule ----------------------------------------------------------------------------------
    # rev 5.2 fixes the generation-0 schedule at 12 s / 9 s: only the active measured L_min gates
    # coverage-completeness and a denser hop inflates T_ind against tier thresholds calibrated at
    # 9 s hops. A schedule challenger is adopted for generation 0 only if it improves a metric at
    # non-inferior precision *and* keeps the hop >= 9000 ms. Both challengers use a 5000 ms hop, so
    # neither is adopted; the default is kept and the observed 8/5 precision gain is deferred to a
    # re-calibrated v2.
    schedule_ev: list[ProfileEvidence] = []
    hop_12_5 = citer.cite("arms.schedule_12_5_0.hop_ms", schedule_ev)
    citer.cite("comparisons.schedule_12_5_minus_12_9.segment_precision.pass", schedule_ev)
    citer.cite("comparisons.schedule_12_5_minus_12_9.segment_precision.lower_bound_e4", schedule_ev)
    hop_8_5 = citer.cite("arms.schedule_8_5_0.hop_ms", schedule_ev)
    citer.cite("comparisons.schedule_8_5_minus_12_9.segment_precision.pass", schedule_ev)
    citer.cite("comparisons.schedule_8_5_minus_12_9.segment_precision.lower_bound_e4", schedule_ev)

    def _adopt(comp_key: str, hop_ms: int) -> bool:
        return (
            _improves_at_non_inferior_precision(comparisons[comp_key]) and hop_ms >= DEFAULT_HOP_MS
        )

    adopt_12_5 = _adopt("schedule_12_5_minus_12_9", int(hop_12_5))
    adopt_8_5 = _adopt("schedule_8_5_minus_12_9", int(hop_8_5))
    schedule_setting = {
        "window_ms": DEFAULT_WINDOW_MS,
        "hop_ms": DEFAULT_HOP_MS,
        "phase_ms": DEFAULT_PHASE_MS,
    }
    if adopt_8_5 and hop_8_5 >= DEFAULT_HOP_MS:
        schedule_setting = {"window_ms": 8_000, "hop_ms": int(hop_8_5), "phase_ms": 0}
    elif adopt_12_5 and hop_12_5 >= DEFAULT_HOP_MS:
        schedule_setting = {"window_ms": DEFAULT_WINDOW_MS, "hop_ms": int(hop_12_5), "phase_ms": 0}
    features.append(
        ProfileFeature(
            name="schedule",
            enabled=True,
            certified=True,
            setting=schedule_setting,
            decision=(
                "Frozen at the plan default 12 s / 9 s / phase 0 (rev 5.2). The 12/5 challenger "
                "fails segment-precision non-inferiority (lower bound -76 e4). The 8/5 challenger "
                "does improve segment precision (+294 e4 lower bound) but uses a 5000 ms hop; "
                "adopting a hop below 9000 ms would inflate T_ind against tier thresholds "
                "calibrated at 9 s hops (rev 5.2), so it is deferred to a re-calibrated v2."
            ),
            evidence=schedule_ev,
        )
    )

    # -- hints -------------------------------------------------------------------------------------
    hints_ev: list[ProfileEvidence] = []
    hint_feature = citer.cite("not_evaluable.0.feature", hints_ev)
    hint_reason = citer.cite("not_evaluable.0.reason", hints_ev)
    hints_gate_status = f"not_evaluable ({hint_feature}): {hint_reason}"
    features.append(
        ProfileFeature(
            name="hints",
            enabled=True,
            certified=False,
            setting={"always_on": True, "gate": "stage-4a"},
            decision=(
                "Enabled but uncertified: the plan keeps hints always-on and non-blocking, and "
                "they never raise the version tier, so they cannot lower precision by "
                "construction. Their accuracy benefit is not certified -- the controlled stratum "
                "carries no hint evidence and the held-out dev-2 corpus does not exist, so the "
                "Stage 4a gate remains the authority and stays blocked. Shown as provisional."
            ),
            evidence=hints_ev,
        )
    )
    return features, hints_gate_status


# --------------------------------------------------------------------------------------------------
# Cost report
# --------------------------------------------------------------------------------------------------
def _cost_report(
    *,
    name: str,
    ablations: dict[str, Any],
    ablations_citer: _Citer,
    shortlist: dict[str, Any],
    shortlist_citer: _Citer,
) -> ProfileCostReport:
    evidence: list[ProfileEvidence] = []
    n_sets = ablations_citer.cite("n_sets", evidence)
    windows_on = ablations_citer.cite("arms.rescans_on.windows", evidence)
    windows_off = ablations_citer.cite("arms.rescans_off.windows", evidence)

    paid: list[ProfilePaidEstimate] = []
    if name == "max_accuracy":
        for provider in ("audd", "acrcloud"):
            index, engine = _shortlist_engine(shortlist, provider)
            paid_ev: list[ProfileEvidence] = []
            status = shortlist_citer.cite(f"engines.{index}.status", paid_ev)
            cost = shortlist_citer.cite(f"engines.{index}.expected_trial_cost_usd_e2", paid_ev)
            paid.append(
                ProfilePaidEstimate(
                    provider=provider,
                    status=str(status),
                    expected_trial_cost_usd_e2=int(cost),
                    corpus_version=str(shortlist["corpus_version"]),
                    evidence=paid_ev,
                )
            )
    return ProfileCostReport(
        enabled_engines_usd_e2=0,
        ablation_corpus_sets=int(n_sets),
        windows_rescans_on=int(windows_on),
        windows_rescans_off=int(windows_off),
        paid_when_enabled=paid,
        evidence=evidence,
    )


# --------------------------------------------------------------------------------------------------
# Profile derivation
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ReportRef:
    name: str
    ref: str
    corpus_version: str


@dataclass(frozen=True)
class PanakoProfileInput:
    """The JDK + built reference pool that enables Panako in a ``max_accuracy`` v2 profile."""

    index_id: str
    index_version: str
    resource_count: int
    panako_version: str
    jdk_version: str


def derive_profile(
    *,
    name: str,
    version_number: int,
    ablations: dict[str, Any],
    shortlist: dict[str, Any],
    ablations_ref: ReportRef,
    shortlist_ref: ReportRef,
    panako_index: PanakoProfileInput | None = None,
) -> ProfileRecord:
    """Derive one frozen profile mechanically from the two reports.

    ``panako_index`` (a JDK + built reference pool) enables Panako as an independent engine in the
    ``max_accuracy`` profile only; the ``free`` profile and the Stage 4d v1 derivation are
    unaffected, so passing ``None`` reproduces the committed profiles byte-for-byte.
    """

    if name not in PROFILE_NAMES:
        raise ValueError(f"unknown profile {name!r}; expected one of {PROFILE_NAMES}")
    if version_number < 1:
        raise ValueError("profile version numbers start at 1")

    ablations_citer = _Citer("ablations", ablations)
    shortlist_citer = _Citer("shortlist", shortlist)

    engines = _engine_rows(
        name=name,
        shortlist=shortlist,
        shortlist_citer=shortlist_citer,
        panako_index=panako_index,
    )
    features, hints_gate_status = _feature_rows(ablations, ablations_citer)
    feature_by_name = {feature.name: feature for feature in features}
    transforms_policy = feature_by_name["transforms"].setting["policy"]
    schedule = feature_by_name["schedule"].setting
    rescans_on = feature_by_name["rescans"].enabled
    novelty_on = feature_by_name["novelty"].enabled
    hints_on = feature_by_name["hints"].enabled

    cost_report = _cost_report(
        name=name,
        ablations=ablations,
        ablations_citer=ablations_citer,
        shortlist=shortlist,
        shortlist_citer=shortlist_citer,
    )

    provenance = ProfileProvenance(
        ablations_report=ablations_ref.name,
        ablations_report_ref=ablations_ref.ref,
        ablations_corpus_version=ablations_ref.corpus_version,
        shortlist_report=shortlist_ref.name,
        shortlist_report_ref=shortlist_ref.ref,
        shortlist_corpus_version=shortlist_ref.corpus_version,
    )

    version = f"{name}-v{version_number}.json"
    notes = [
        "Frozen mechanically by `id-detector benchmark freeze-profiles`; do not hand-edit. Every "
        "number about the world is cited from the ablation or shortlist report; concrete geometry "
        "and budget ceilings are plan configuration cited below.",
        "Generation-0 schedule 12 s / 9 s / phase 0 and rescan policy 12 s / 5 s / phase 0 with "
        "max_generations 3 are the plan rev-5.2 defaults (DEFAULT_* in providers.base).",
        "Budget ceilings are plan configuration: shazam_requests_per_minute "
        f"{SHAZAM_REQUESTS_PER_MINUTE} from the plan throttling risk row (<= 18 req/min) and "
        f"max_requests_per_media {MAX_REQUESTS_PER_MEDIA} from the analyse default.",
        "local_fixture is excluded from every production profile: it is a controlled-corpus "
        "oracle, not a production recognizer.",
    ]
    if name == "free":
        notes.append(
            "engine_policy free_only: only free engines may ever be enabled here; paid scanners "
            "are out of scope and belong to max_accuracy."
        )
    elif panako_index is not None:
        notes.append(
            "engine_policy all_available: every available independent engine runs over the whole "
            f"set with no suppression. A JDK ({panako_index.jdk_version}) and a built reference "
            f"pool (index_id {panako_index.index_id}, index_version {panako_index.index_version}, "
            f"{panako_index.resource_count} tracks) are present, so Panako is enabled as an "
            "independent local_index_query source alongside Shazam; the paid scanners remain "
            "eligible-when-available with their cost."
        )
    else:
        notes.append(
            "engine_policy all_available: every available independent engine runs over the whole "
            "set with no suppression. In v1 only Shazam is available (no credentials, no JDK), so "
            "the enabled set and the operational pipeline are identical to the free profile; the "
            "paid scanners and Panako are recorded as eligible-when-available with their cost."
        )

    natural_key = compose_natural_key("profile", {"name": name, "version": version})
    return ProfileRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id(ablations_ref.ref, "profile", natural_key),
        name=name,
        version=version,
        frozen=True,
        engine_policy="free_only" if name == "free" else "all_available",
        enabled_engines=[engine.provider for engine in engines if engine.enabled],
        engines=engines,
        transforms_policy=transforms_policy,
        transform_rates_e4=list(AppConfig().transform_rates_e4),
        transform_semitones=list(AppConfig().transform_semitones),
        schedule=ProfileSchedule(
            window_ms=schedule["window_ms"],
            hop_ms=schedule["hop_ms"],
            phase_ms=schedule["phase_ms"],
        ),
        rescan=ProfileRescan(
            enabled=rescans_on,
            window_ms=DEFAULT_RESCAN_WINDOW_MS,
            hop_ms=DEFAULT_RESCAN_HOP_MS,
            phase_ms=DEFAULT_RESCAN_PHASE_MS,
            max_generations=DEFAULT_MAX_GENERATIONS if rescans_on else 0,
        ),
        novelty_enabled=novelty_on,
        hints_enabled=hints_on,
        hints_gate_status=hints_gate_status,
        features=features,
        budget=ProfileBudget(
            max_requests_per_media=MAX_REQUESTS_PER_MEDIA,
            max_usd_e2=0,
            allow_third_party_upload=False,
            shazam_requests_per_minute=SHAZAM_REQUESTS_PER_MINUTE,
        ),
        cost_report=cost_report,
        frozen_from=provenance,
        notes=notes,
    )


def _report_ref(path: Path) -> ReportRef:
    payload = json.loads(read_text(path))
    corpus_version = str(payload.get("corpus_version", ""))
    return ReportRef(name=path.name, ref=sha256_file(path), corpus_version=corpus_version)


def _next_version_number(out_dir: Path, name: str) -> int:
    highest = 0
    if out_dir.is_dir():
        for path in out_dir.glob(f"{name}-v*.json"):
            match = _VERSION_RE.search(path.stem)
            if match:
                highest = max(highest, int(match.group(1)))
    return highest + 1


@dataclass(frozen=True)
class FreezeResult:
    profiles: dict[str, ProfileRecord]
    written: dict[str, Path]


def freeze_profiles(
    *,
    ablations_path: Path,
    shortlist_path: Path,
    out_dir: Path,
) -> FreezeResult:
    """Derive and write ``free`` and ``max_accuracy`` profiles from the two reports."""

    ablations = json.loads(read_text(ablations_path))
    shortlist = json.loads(read_text(shortlist_path))
    ablations_ref = _report_ref(ablations_path)
    shortlist_ref = _report_ref(shortlist_path)

    profiles: dict[str, ProfileRecord] = {}
    written: dict[str, Path] = {}
    for name in PROFILE_NAMES:
        version_number = _next_version_number(out_dir, name)
        profile = derive_profile(
            name=name,
            version_number=version_number,
            ablations=ablations,
            shortlist=shortlist,
            ablations_ref=ablations_ref,
            shortlist_ref=shortlist_ref,
        )
        destination = out_dir / profile.version
        atomic_write_json(destination, profile)
        write_completion_sidecar(destination, {})
        profiles[name] = profile
        written[name] = destination
    return FreezeResult(profiles=profiles, written=written)


def derive_max_accuracy_v2(
    *,
    ablations_path: Path,
    shortlist_path: Path,
    panako_index: PanakoProfileInput,
) -> ProfileRecord:
    """Derive the ``max_accuracy`` v2 profile that enables Panako, from the two committed reports.

    Stage 8: with a JDK and a built reference pool present, Panako joins the enabled independent
    engines.  This does not touch the ``free`` or v1 ``max_accuracy`` artefacts; the operator
    freezes it explicitly by writing the returned record to ``profiles/max_accuracy-v2.json``.
    """

    ablations = json.loads(read_text(ablations_path))
    shortlist = json.loads(read_text(shortlist_path))
    return derive_profile(
        name="max_accuracy",
        version_number=2,
        ablations=ablations,
        shortlist=shortlist,
        ablations_ref=_report_ref(ablations_path),
        shortlist_ref=_report_ref(shortlist_path),
        panako_index=panako_index,
    )


# --------------------------------------------------------------------------------------------------
# Loading a frozen profile for `analyse --profile`
# --------------------------------------------------------------------------------------------------
class UnknownProfile(ValueError):
    """Raised when a requested profile is not a frozen artefact."""


def _profile_texts(project_root: Path, name: str) -> dict[int, str]:
    """Return ``{version_number: json_text}`` for a profile from disk then packaged resources."""

    found: dict[int, str] = {}
    directory = project_root / "profiles"
    if directory.is_dir():
        for path in directory.glob(f"{name}-v*.json"):
            match = _VERSION_RE.search(path.stem)
            if match:
                found[int(match.group(1))] = read_text(path)
    packaged = files("id_detector.resources.profiles")
    for item in packaged.iterdir():
        if not item.name.endswith(".json") or item.name.endswith(".done.json"):
            continue
        stem = item.name.removesuffix(".json")
        if not stem.startswith(f"{name}-v"):
            continue
        match = _VERSION_RE.search(stem)
        if match:
            found.setdefault(int(match.group(1)), item.read_text(encoding="utf-8"))
    return found


def load_profile(project_root: Path, name: str) -> ProfileRecord:
    """Load the highest-versioned frozen profile ``name``; reject anything not frozen."""

    if name not in PROFILE_NAMES:
        raise UnknownProfile(
            f"unknown profile {name!r}; frozen profiles are: {', '.join(PROFILE_NAMES)}"
        )
    texts = _profile_texts(project_root, name)
    if not texts:
        raise UnknownProfile(f"no frozen artefact found for profile {name!r}")
    profile = ProfileRecord.model_validate_json(texts[max(texts)])
    if not profile.frozen:
        raise UnknownProfile(f"profile {name!r} is not frozen")
    return profile


def profile_app_config(profile: ProfileRecord) -> AppConfig:
    """Map a frozen profile onto the runtime :class:`AppConfig` used by the pipeline."""

    return AppConfig(
        allow_third_party_upload=profile.budget.allow_third_party_upload,
        transforms_policy=profile.transforms_policy,
        transform_rates_e4=tuple(profile.transform_rates_e4),
        transform_semitones=tuple(profile.transform_semitones),
        window_ms=profile.schedule.window_ms,
        hop_ms=profile.schedule.hop_ms,
        phase_ms=profile.schedule.phase_ms,
        rescan_window_ms=profile.rescan.window_ms,
        rescan_hop_ms=profile.rescan.hop_ms,
        rescan_phase_ms=profile.rescan.phase_ms,
        rescan_max_generations=profile.rescan.max_generations,
    )


def profile_canonical_bytes(profile: ProfileRecord) -> bytes:
    return canonical_json_bytes(profile)

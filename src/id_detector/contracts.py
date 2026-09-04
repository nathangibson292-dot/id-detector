"""Versioned Stage 0 data contracts.

Confidence-like values use integer ten-thousandths (0..10000). This keeps the plan's field
names while satisfying its no-floats rule. Nullable fields are intentionally required so writers
must emit an explicit JSON ``null`` rather than silently omitting unknown information.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha1, sha256
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0.0"
GENERATED_BY = "id-detector/0.1.0"
SENSITIVE_FIELD_NAMES = frozenset(
    {
        "client_id",
        "oauth_token",
        "api_key",
        "api_token",
        "access_key",
        "access_secret",
        "client_secret",
        "authorization",
        "cookie",
    }
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Sha1 = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
NonNegativeInt = Annotated[int, Field(ge=0)]
ConfidenceE4 = Annotated[int, Field(ge=0, le=10_000)]
MoneyE2 = Annotated[int, Field(ge=0)]
# JSON arrays arrive as Python lists. Only the container conversion is relaxed; strict mode still
# rejects strings and booleans for either integer item.
SpanMs = Annotated[tuple[NonNegativeInt, NonNegativeInt], Field(strict=False)]

# Recursive JSON that deliberately has no ``float`` branch. This also makes exported schemas
# reject floating-point numbers inside provider-native/config/metrics payloads.
type JsonValue = dict[str, JsonValue] | list[JsonValue] | str | int | bool | None


def _round_fraction(numerator: int, denominator: int) -> int:
    quotient, remainder = divmod(numerator, denominator)
    return quotient + int(remainder * 2 >= denominator)


def _reject_floats(value: Any, path: str = "$") -> None:
    if isinstance(value, float):
        raise ValueError(f"floating-point value is forbidden at {path}")
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in SENSITIVE_FIELD_NAMES:
                raise ValueError(f"secret field is forbidden in artefacts at {path}.{key}")
            _reject_floats(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_floats(item, f"{path}[{index}]")


class ContractModel(BaseModel):
    """Closed model shared by all contract objects."""

    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def no_floats(cls, value: Any) -> Any:
        _reject_floats(value)
        return value


class Record(ContractModel):
    schema_version: Literal[SCHEMA_VERSION]
    generated_by: Annotated[str, Field(min_length=1)]


class OriginalAsset(ContractModel):
    path: str
    sha256: Sha256
    container: str
    codec: str
    bitrate: NonNegativeInt | None
    ytdlp_format_id: str | None


class SourceMetadata(ContractModel):
    description: str | None
    chapters: list[JsonValue]
    comment_count: NonNegativeInt | None


class SourceRecord(Record):
    source_key: Sha256
    media_key: Sha256
    input_url: str
    canonical_url: str
    platform: Literal["soundcloud", "youtube", "mixcloud", "file", "other"]
    platform_id: str | None
    uploader_id: str | None
    uploader_name: str | None
    title: str | None
    upload_date: str | None
    original: OriginalAsset
    metadata: SourceMetadata
    config_snapshot: dict[str, JsonValue]

    @model_validator(mode="after")
    def source_key_matches_canonical_url(self) -> SourceRecord:
        if self.source_key != derive_source_key(self.canonical_url):
            raise ValueError("source_key must be sha256(canonical_url)")
        return self


class PcmAsset(ContractModel):
    path: str
    sha256: Sha256
    sample_rate: Literal[16000]
    channels: Literal[1]
    sample_format: Literal["s16le"]
    duration_ms: NonNegativeInt
    ffprobe_duration_ms: NonNegativeInt


class DecoderInfo(ContractModel):
    ffmpeg_version: str
    filtergraph: str


class PcmRecord(Record):
    media_key: Sha256
    pcm: PcmAsset
    decoder: DecoderInfo


class Transform(ContractModel):
    type: Literal["none", "resample", "tempo", "pitch"]
    rate_e4: Annotated[int, Field(gt=0)]
    semitones: int

    @model_validator(mode="after")
    def type_parameters_agree(self) -> Transform:
        if self.type == "none" and (self.rate_e4 != 10_000 or self.semitones != 0):
            raise ValueError("none transform must have rate_e4=10000 and semitones=0")
        if self.type in {"resample", "tempo"} and self.semitones != 0:
            raise ValueError("rate transforms must have semitones=0")
        if self.type == "pitch":
            expected_rate = round(10_000 * (2 ** (self.semitones / 12)))
            if self.rate_e4 != expected_rate:
                raise ValueError("pitch rate_e4 must be derived from semitones")
        return self


class SampleMap(ContractModel):
    a_num: Annotated[int, Field(gt=0)]
    a_den: Annotated[int, Field(gt=0)]
    b_samples: int
    uncertainty_ms: NonNegativeInt


class WindowRecord(Record):
    id: Sha1
    generation: NonNegativeInt
    start_ms: NonNegativeInt
    support_ms: SpanMs
    output_ms: NonNegativeInt
    transform: Transform
    sample_map: SampleMap
    wav_path: str
    wav_sha256: Sha256
    logical_trial_id: Sha1
    reason: Literal["schedule", "tail", "rescan"]
    rescan_request_id: Sha1 | None

    @model_validator(mode="after")
    def transform_span_and_sample_map_agree(self) -> WindowRecord:
        if not 0 < self.output_ms <= 12_000:
            raise ValueError("output_ms must be in 1..12000")
        support_start, support_end = self.support_ms
        if support_start != self.start_ms or support_end <= support_start:
            raise ValueError("support_ms must be an ordered span beginning at start_ms")

        if self.transform.type in {"resample", "tempo"}:
            expected_span = _round_fraction(self.output_ms * 10_000, self.transform.rate_e4)
            expected_map = (10_000, self.transform.rate_e4, 0)
            expected_uncertainty = 0 if self.transform.type == "resample" else 100
        else:
            expected_span = self.output_ms
            expected_map = (1, 1, 0)
            expected_uncertainty = 0 if self.transform.type == "none" else 100

        if support_end - support_start != expected_span:
            raise ValueError("support_ms span does not agree with output_ms and transform")
        actual_map = (
            self.sample_map.a_num,
            self.sample_map.a_den,
            self.sample_map.b_samples,
        )
        if actual_map != expected_map:
            raise ValueError("sample_map does not agree with transform")
        if self.sample_map.uncertainty_ms != expected_uncertainty:
            raise ValueError("sample_map uncertainty does not agree with transform")
        if self.transform.type == "none" and self.logical_trial_id != self.id:
            raise ValueError("a none window must be its own logical trial")
        return self


class WindowQueryTarget(ContractModel):
    window_id: Sha1


class AssetQueryTarget(ContractModel):
    asset: Literal["original", "pcm"]
    asset_sha256: Sha256


type QueryTarget = WindowQueryTarget | AssetQueryTarget


class QueryRecord(Record):
    id: Sha1
    generation: NonNegativeInt
    provider: str
    capability: Literal["clip_recognizer", "file_scanner", "local_index_query"]
    target: QueryTarget
    provider_config_version: str
    scan_policy: str
    cache_key: Sha256

    @model_validator(mode="after")
    def capability_matches_target(self) -> QueryRecord:
        if self.capability == "file_scanner" and not isinstance(self.target, AssetQueryTarget):
            raise ValueError("file_scanner queries require an asset target")
        if self.capability != "file_scanner" and not isinstance(self.target, WindowQueryTarget):
            raise ValueError("clip and local-index queries require a window target")
        return self


class RawLabel(ContractModel):
    artist: str | None
    title: str | None
    album: str | None
    label: str | None
    release_date: str | None


class Anchor(ContractModel):
    mix_anchor_ms: NonNegativeInt
    ref_anchor_ms: int
    uncertainty_ms: NonNegativeInt
    reliable: bool
    method: str
    bias_applied_ms: int


class ObservationRecord(Record):
    id: Sha1
    generation: NonNegativeInt
    query_id: Sha1
    provider: str
    capability: Literal["clip_recognizer", "file_scanner", "local_index_query"]
    status: Literal["match", "no_match", "error"]
    is_final: bool
    mix_span_ms: SpanMs
    support_ms: SpanMs
    transform: Transform | None
    logical_trial_id: Sha1
    raw_label: RawLabel
    provider_ids: dict[str, JsonValue]
    native: dict[str, JsonValue]
    anchor: Anchor | None
    score_raw: int | None
    quality: int | None
    raw_response_ref: str
    source_ids: list[str]


class HintFlags(ContractModel):
    unreleased: bool
    id_unknown: bool
    mashup_with: bool
    edit: bool
    bootleg: bool


class HintAuthor(ContractModel):
    pseudo_id: str
    is_uploader: bool
    is_verified: bool
    follower_count: NonNegativeInt | None
    like_count: NonNegativeInt | None


class HintRelation(ContractModel):
    type: Literal["replies_to", "corrects", "copies"]
    hint_id: Sha1
    confidence: ConfidenceE4


class HintRecord(Record):
    id: Sha1
    connector: str
    kind: Literal["tracklist_line", "answer", "correction", "question", "pointer", "keyword"]
    raw_text: str
    artist: str | None
    title: str | None
    version_qualifier: str | None
    label: str | None
    flags: HintFlags
    position_range_ms: SpanMs | None
    position_kind: Literal[
        "cue_hms", "cue_minute", "comment_timestamp", "chapter", "section", "none"
    ]
    author: HintAuthor
    is_pinned: bool
    parse_confidence: ConfidenceE4
    identity_specificity: ConfidenceE4
    temporal_precision_ms: NonNegativeInt | None
    relations: list[HintRelation]
    provenance_group: str
    mirror_of: str | None
    mirror_status: Literal["verified", "quarantined"]
    truncated: bool


IdentityNamespace = Literal[
    "isrc",
    "mb_recording",
    "mb_work",
    "mb_release",
    "shazam",
    "deezer",
    "apple",
    "spotify",
    "acr",
    "audd",
    "beatport",
    "soundcloud",
    "text",
]


class IdentityNode(Record):
    id: str
    ns: IdentityNamespace
    label: str


class AssertionSource(ContractModel):
    kind: str
    record_id: str


class IdentityAssertion(Record):
    id: Sha1
    a: str
    b: str
    relation: Literal[
        "same_recording",
        "same_work",
        "same_release",
        "edit_of",
        "sampled_from",
        "mashup_of",
        "component_of",
        "conflicts",
    ]
    source: AssertionSource
    independent_of: str
    confidence: ConfidenceE4


class IdentityWork(Record):
    work_id: Sha1
    member_nodes: list[str]


class IdentityCandidate(Record):
    canonical_id: Sha1
    work_id: Sha1
    member_nodes: list[str]
    alternatives: list[Sha1]
    contested: bool
    conflicts: list[str]


class IdentitiesRecord(Record):
    nodes: list[IdentityNode]
    assertions: list[IdentityAssertion]
    works: list[IdentityWork]
    candidates: list[IdentityCandidate]


class PredictionInterval(ContractModel):
    lo: NonNegativeInt
    hi: NonNegativeInt
    coverage_target: ConfidenceE4
    method: str
    calibrated: bool

    @model_validator(mode="after")
    def _ordered(self) -> PredictionInterval:
        if self.hi < self.lo:
            raise ValueError("prediction interval must be ordered")
        return self


class RoleSegment(ContractModel):
    from_ms: NonNegativeInt
    to_ms: NonNegativeInt
    role: Literal["incoming", "dominant", "outgoing", "layer", "component", "uncertain"]

    @model_validator(mode="after")
    def _ordered(self) -> RoleSegment:
        if self.to_ms <= self.from_ms:
            raise ValueError("role segment must have positive duration")
        return self


class AlignmentSegment(ContractModel):
    mix_from_ms: NonNegativeInt
    mix_to_ms: NonNegativeInt
    rate_e4: Annotated[int, Field(gt=0)]
    intercept_ms: int
    residual_ms: NonNegativeInt
    n_obs: NonNegativeInt


class AlignmentEvent(ContractModel):
    at_ms: NonNegativeInt
    # rev 5.2 / Stage 4c adds ``replay``: the plan's precedence decides it and its metrics are
    # required per type, but the episode contract otherwise gave the decision nowhere to be dated.
    type: Literal["jump", "loop", "reset", "drift", "replay"]


class EpisodeScores(ContractModel):
    work: ConfidenceE4
    version: ConfidenceE4
    boundary: ConfidenceE4


class EpisodeTiers(ContractModel):
    work: Literal["unclear", "possible", "likely", "verified"]
    version: Literal["unclear", "possible", "likely", "verified"]
    boundary: Literal["unclear", "possible", "likely", "verified"]


class EpisodeRecord(Record):
    id: Sha1
    candidate_id: Sha1
    alternatives: list[Sha1]
    claim: Literal["performed", "component_evidence"]
    start_no_later_than_ms: NonNegativeInt
    end_no_earlier_than_ms: NonNegativeInt
    evidence_support_ms: list[SpanMs]
    start_no_earlier_than_ms: NonNegativeInt | None
    end_no_later_than_ms: NonNegativeInt | None
    start_pi: PredictionInterval | None
    end_pi: PredictionInterval | None
    best_start_ms: NonNegativeInt
    best_end_ms: NonNegativeInt
    role_segments: list[RoleSegment]
    occurrence_index: NonNegativeInt
    overlaps: list[Sha1]
    alignment_segments: list[AlignmentSegment]
    alignment_events: list[AlignmentEvent]
    has_global_alignment: bool
    scores: EpisodeScores
    score_kind: Literal["heuristic", "calibrated"]
    tiers: EpisodeTiers
    badge: Literal["unclear", "possible", "likely", "verified"]
    version_status: Literal["verified", "unverified", "contested"]
    evidence: list[str]
    rejected_evidence: list[str]
    flags: list[str]
    rescan_state: str


class GapEvidence(ContractModel):
    n_windows: NonNegativeInt
    n_no_match: NonNegativeInt
    n_error: NonNegativeInt
    n_unclear_candidates: NonNegativeInt
    n_hint_events: NonNegativeInt
    n_novelty_events: NonNegativeInt


class GapRecord(Record):
    id: Sha1
    start_ms: NonNegativeInt
    end_ms: NonNegativeInt
    bounded_by: list[Sha1]
    evidence: GapEvidence
    reason: Literal["no_evidence", "unclear_only"]
    truncated: bool
    best_unclear_candidate: Sha1 | None


class DurationsRecord(Record):
    evidence_supported_ms: NonNegativeInt
    predicted_episode_ms: NonNegativeInt
    unresolved_boundary_ms: NonNegativeInt
    unclear_ms: NonNegativeInt
    no_evidence_ms: NonNegativeInt
    unscanned_ms: NonNegativeInt


class CertificationEntry(ContractModel):
    dimension: Literal["work", "version", "start", "end", "boundary"]
    tier: Literal["possible", "likely", "verified"]
    status: Literal["certified", "provisional"]
    n_test_predictions: NonNegativeInt
    lower_bound_e4: ConfidenceE4
    test_version: str


class CertificationBlock(ContractModel):
    profile: str
    per: list[CertificationEntry]


class EpisodesFile(Record):
    generation: NonNegativeInt
    episodes: list[EpisodeRecord]
    gaps: list[GapRecord]
    durations: DurationsRecord
    certification: CertificationBlock


class RescanPolicy(ContractModel):
    window_ms: NonNegativeInt
    hop_ms: NonNegativeInt
    phase_ms: NonNegativeInt
    transforms: list[Transform]


class RescanRequestRecord(Record):
    id: Sha1
    generation: NonNegativeInt
    trigger: Literal[
        "gap", "contested", "edge", "long_episode", "novelty", "hint_cluster", "question_cluster"
    ]
    start_ms: NonNegativeInt
    end_ms: NonNegativeInt
    policy: RescanPolicy
    priority: int
    input_hashes: dict[str, Sha256]


class TruthSource(ContractModel):
    url_ref: str
    media_key: Sha256
    duration_ms: NonNegativeInt
    platform: str
    uploader_ref: str
    event_ref: str | None
    date: str | None


class TruthWork(ContractModel):
    artist: str
    title: str


class TruthVersion(ContractModel):
    qualifier: str | None
    ids: dict[str, str]


class TruthRoleSegment(ContractModel):
    from_ms: NonNegativeInt
    to_ms: NonNegativeInt
    role: Literal["incoming", "dominant", "outgoing", "layer", "component", "uncertain"]

    @model_validator(mode="after")
    def _ordered(self) -> TruthRoleSegment:
        if self.to_ms <= self.from_ms:
            raise ValueError("truth role segment must have positive duration")
        return self


class TruthEpisode(ContractModel):
    work: TruthWork
    version: TruthVersion
    version_verified: bool
    verified_against: Literal["audio", "source_recording", "authoritative_metadata"] | None
    start_ms_range: SpanMs
    end_ms_range: SpanMs
    audible_rule: str
    role_segments: list[TruthRoleSegment]
    overlaps_with: list[NonNegativeInt]
    occurrence_index: NonNegativeInt
    in_reference_pool: bool
    annotator_ref: str | None
    second_pass_ref: str | None
    disagreement_resolution: str | None
    note: str | None
    draft: bool

    @model_validator(mode="after")
    def _verification_state(self) -> TruthEpisode:
        if self.draft and (
            self.verified_against is not None
            or self.version_verified
            or self.annotator_ref is not None
            or self.second_pass_ref is not None
        ):
            raise ValueError("draft truth must not claim verification")
        if self.version_verified and self.verified_against not in {
            "source_recording",
            "authoritative_metadata",
        }:
            raise ValueError("exact version truth needs source-recording or authoritative evidence")
        return self


class TruthRegion(ContractModel):
    start_ms: NonNegativeInt
    end_ms: NonNegativeInt
    type: Literal["silence_or_speech", "out_of_pool", "unresolved"]

    @model_validator(mode="after")
    def _ordered(self) -> TruthRegion:
        if self.end_ms <= self.start_ms:
            raise ValueError("truth region must have positive duration")
        return self


class TruthEvent(ContractModel):
    """Explicit event truth (rev 5.2 / Stage 4c).

    ``at_ms`` is the exact mix time of the rendered or annotated discontinuity.  ``episode_index``
    names the truth episode the event belongs to (``null`` for a set-level event).  Stage 2a
    encoded these in the free-text ``note`` field; the scorer now reads this contract instead.
    """

    type: Literal["jump", "loop", "reset", "drift", "replay"]
    at_ms: NonNegativeInt
    episode_index: NonNegativeInt | None
    note: str | None


class GroundTruthRecord(Record):
    set_id: str
    source: TruthSource
    stratum: str
    split: Literal["dev-1", "dev-2", "calibration", "test", "controlled"]
    corpus_version: str
    selection_basis: str
    episodes: list[TruthEpisode]
    events: list[TruthEvent]
    regions: list[TruthRegion]

    @model_validator(mode="after")
    def _timeline_is_coherent(self) -> GroundTruthRecord:
        duration_ms = self.source.duration_ms
        if duration_ms <= 0:
            raise ValueError("truth source duration_ms must be positive")

        occurrence_keys: set[tuple[str, str, int]] = set()
        for index, episode in enumerate(self.episodes):
            start_lo, start_hi = episode.start_ms_range
            end_lo, end_hi = episode.end_ms_range
            if start_hi < start_lo:
                raise ValueError(f"episode {index} start_ms_range must be ordered")
            if end_hi < end_lo:
                raise ValueError(f"episode {index} end_ms_range must be ordered")
            if start_hi > end_lo:
                raise ValueError(f"episode {index} start range must not cross its end range")
            if end_hi > duration_ms:
                raise ValueError(f"episode {index} exceeds source duration_ms")
            for role in episode.role_segments:
                if role.from_ms < start_lo or role.to_ms > end_hi:
                    raise ValueError(f"episode {index} role segment lies outside its audible span")
            if len(set(episode.overlaps_with)) != len(episode.overlaps_with):
                raise ValueError(f"episode {index} has duplicate overlap indexes")
            for other in episode.overlaps_with:
                if other == index or other >= len(self.episodes):
                    raise ValueError(f"episode {index} has an invalid overlap index")
            occurrence_key = (
                episode.work.artist.casefold().strip(),
                episode.work.title.casefold().strip(),
                episode.occurrence_index,
            )
            if occurrence_key in occurrence_keys:
                raise ValueError("occurrence_index must be unique within a work")
            occurrence_keys.add(occurrence_key)

        for index, episode in enumerate(self.episodes):
            for other in episode.overlaps_with:
                if index not in self.episodes[other].overlaps_with:
                    raise ValueError(f"episode overlap {index}<->{other} must be symmetric")
                other_episode = self.episodes[other]
                if min(episode.end_ms_range[1], other_episode.end_ms_range[1]) <= max(
                    episode.start_ms_range[0], other_episode.start_ms_range[0]
                ):
                    raise ValueError(
                        f"episode overlap {index}<->{other} has no audible intersection"
                    )

        event_keys: set[tuple[str, int, int | None]] = set()
        for index, event in enumerate(self.events):
            if event.at_ms > duration_ms:
                raise ValueError(f"truth event {index} exceeds source duration_ms")
            if event.episode_index is not None and event.episode_index >= len(self.episodes):
                raise ValueError(f"truth event {index} has an invalid episode_index")
            key = (event.type, event.at_ms, event.episode_index)
            if key in event_keys:
                raise ValueError("truth events must be unique by type, at_ms and episode_index")
            event_keys.add(key)

        ordered_regions = sorted(
            self.regions, key=lambda item: (item.start_ms, item.end_ms, item.type)
        )
        for index, region in enumerate(ordered_regions):
            if region.end_ms > duration_ms:
                raise ValueError(f"truth region {index} exceeds source duration_ms")
            if index and region.start_ms < ordered_regions[index - 1].end_ms:
                raise ValueError("truth regions must not overlap")
        return self


class PrecisionRecallF1(ContractModel):
    precision_e4: ConfidenceE4
    recall_e4: ConfidenceE4
    f1_e4: ConfidenceE4


class PrecisionRecall(ContractModel):
    precision_e4: ConfidenceE4
    recall_e4: ConfidenceE4


class EventMetric(PrecisionRecall):
    n: NonNegativeInt


class PerformedComponentConfusion(ContractModel):
    performed_as_performed: NonNegativeInt
    performed_as_component: NonNegativeInt
    component_as_performed: NonNegativeInt
    component_as_component: NonNegativeInt


class BenchmarkMetrics(ContractModel):
    identification_work: PrecisionRecallF1
    identification_version: PrecisionRecallF1
    occurrence: PrecisionRecallF1
    segment_micro: PrecisionRecall
    segment_macro_by_set: PrecisionRecall
    selective_precision_e4: ConfidenceE4
    selective_recall_e4: ConfidenceE4
    selective_coverage_e4: ConfidenceE4
    empirical_tier_precision_e4: dict[str, ConfidenceE4]
    empirical_tier_lower_bound_e4: dict[str, ConfidenceE4]
    calibration_error_e4: NonNegativeInt
    false_discovery_rate_e4: ConfidenceE4
    start_median_absolute_error_ms: NonNegativeInt
    start_p90_error_ms: NonNegativeInt
    start_within_5s_e4: ConfidenceE4
    start_within_10s_e4: ConfidenceE4
    start_within_30s_e4: ConfidenceE4
    start_bound_violation_e4: ConfidenceE4
    start_bound_n: NonNegativeInt
    start_interval_coverage_e4: ConfidenceE4
    start_interval_median_width_ms: NonNegativeInt
    start_interval_p90_width_ms: NonNegativeInt
    start_interval_winkler_score: NonNegativeInt
    end_median_absolute_error_ms: NonNegativeInt
    end_p90_error_ms: NonNegativeInt
    end_within_5s_e4: ConfidenceE4
    end_within_10s_e4: ConfidenceE4
    end_within_30s_e4: ConfidenceE4
    end_bound_violation_e4: ConfidenceE4
    end_bound_n: NonNegativeInt
    end_interval_coverage_e4: ConfidenceE4
    end_interval_median_width_ms: NonNegativeInt
    end_interval_p90_width_ms: NonNegativeInt
    end_interval_winkler_score: NonNegativeInt
    boundary_interval_coverage_e4: ConfidenceE4
    boundary_interval_median_width_ms: NonNegativeInt
    boundary_interval_p90_width_ms: NonNegativeInt
    boundary_winkler_score: NonNegativeInt
    episode_iou_e4: ConfidenceE4
    repeated_occurrence_recall_e4: ConfidenceE4
    overlap_recall_e4: ConfidenceE4
    event_jump: EventMetric
    event_loop: EventMetric
    event_reset: EventMetric
    event_drift: EventMetric
    # rev 5.2 / Stage 4c. Defaulted so reports written before replay was scored stay readable;
    # every report produced by the current scorer sets it explicitly.
    event_replay: EventMetric = EventMetric(precision_e4=0, recall_e4=0, n=0)
    performed_component_confusion: PerformedComponentConfusion
    dominant_layer: PrecisionRecall
    secondary_layer: PrecisionRecall
    unknown_region: PrecisionRecall
    physical_attempts: NonNegativeInt


class BenchmarkSet(ContractModel):
    set_id: str
    stratum: str
    split: str
    metrics: BenchmarkMetrics


class BenchmarkStratum(ContractModel):
    stratum: str
    metrics: BenchmarkMetrics
    ci: dict[str, JsonValue]


class BenchmarkEngine(ContractModel):
    provider: str
    oracle_coverage: ConfidenceE4
    pairwise_agreement: ConfidenceE4
    ablation_delta: int


class BenchmarkCost(ContractModel):
    requests: NonNegativeInt
    physical_attempts: NonNegativeInt
    billable_seconds: NonNegativeInt
    usd_e2: MoneyE2
    wall_ms: NonNegativeInt


class BenchmarkCertification(ContractModel):
    dimension: str
    tier: str
    n: NonNegativeInt
    errors: NonNegativeInt
    lower_bound_e4: ConfidenceE4
    cluster_lower_bound_e4: ConfidenceE4
    n_sets: NonNegativeInt
    target_e4: ConfidenceE4 | None
    registration_version: str | None
    status: Literal["certified", "provisional"]


class RegressionGate(ContractModel):
    name: str
    pass_: bool = Field(alias="pass")


class BenchmarkRegression(ContractModel):
    baseline_report_ref: str | None
    deltas: dict[str, int]
    gates: list[RegressionGate]


class BenchmarkReportRecord(Record):
    corpus_version: str
    profile: str
    config_hash: Sha256
    sets: list[BenchmarkSet]
    strata: list[BenchmarkStratum]
    overall: BenchmarkMetrics
    engines: list[BenchmarkEngine]
    cost: BenchmarkCost
    certification: list[BenchmarkCertification]
    regression: BenchmarkRegression
    unverified_seed_comparison: bool


class ShortlistPairwiseAgreement(ContractModel):
    provider_a: str
    provider_b: str
    n_sets: NonNegativeInt
    agreement_e4: ConfidenceE4


class ShortlistEngine(ContractModel):
    provider: str
    capability: Literal["clip_recognizer", "file_scanner", "local_index_query"]
    provider_config_version: str
    status: str
    set_count: NonNegativeInt
    observation_count: NonNegativeInt
    match_count: NonNegativeInt
    oracle_coverage_e4: ConfidenceE4
    metrics: BenchmarkMetrics | None
    cost: BenchmarkCost
    expected_trial_cost_usd_e2: MoneyE2


class ShortlistReportRecord(Record):
    corpus_version: str
    engines: list[ShortlistEngine]
    pairwise_agreement: list[ShortlistPairwiseAgreement]
    union_coverage_e4: ConfidenceE4
    oracle_coverage_e4: ConfidenceE4
    cost: BenchmarkCost
    reference_pool_status: Literal["excluded_from_v1_pending_owner_jdk_decision"]
    notes: list[str]


class InvocationJournalEntry(Record):
    invocation_id: str
    command: list[str]
    started_at: str
    finished_at: str | None
    status: Literal["running", "succeeded", "failed", "cancelled"]
    exit_code: int | None
    duration_ms: NonNegativeInt | None
    tool_versions: dict[str, str]
    timings: dict[str, NonNegativeInt]
    counts: dict[str, int]
    costs: dict[str, int]
    source_ids: list[str]


class RawIndexEntry(Record):
    id: Sha1
    cache_key: Sha256
    query_id: Sha1
    path: str
    sha256: Sha256
    status: Literal["match", "no_match", "error"]
    source_ids: list[str]


class ProviderConfigRecord(Record):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"measured": {"const": True}},
                        "required": ["measured"],
                    },
                    "then": {
                        "properties": {
                            "adapter_bias_ms": {"type": "integer"},
                            "adapter_bias_uncertainty_ms": {"type": "integer"},
                            "L_min_ms": {"type": "object", "minProperties": 1},
                            "source_ids": {"minItems": 1},
                        }
                    },
                    "else": {
                        "properties": {
                            "adapter_bias_ms": {"type": "null"},
                            "adapter_bias_uncertainty_ms": {"type": "null"},
                            "L_min_ms": {"type": "null"},
                        }
                    },
                }
            ]
        },
    )

    id: Sha1
    provider: str
    version: str
    capability: Literal["clip_recognizer", "file_scanner", "local_index_query"]
    measured: bool
    config: dict[str, JsonValue]
    adapter_bias_ms: int | None
    adapter_bias_uncertainty_ms: NonNegativeInt | None
    l_min_ms: dict[str, NonNegativeInt] | None = Field(alias="L_min_ms")
    source_ids: list[str]

    @model_validator(mode="after")
    def measurement_fields_agree(self) -> ProviderConfigRecord:
        fields = (
            self.adapter_bias_ms,
            self.adapter_bias_uncertainty_ms,
            self.l_min_ms,
        )
        if self.measured:
            if any(value is None for value in fields):
                raise ValueError("measured provider config requires all measurement outputs")
            if not self.l_min_ms:
                raise ValueError("measured provider config requires non-empty L_min_ms")
            if not self.source_ids:
                raise ValueError("measured provider config requires evidence source_ids")
        elif any(value is not None for value in fields):
            raise ValueError("unmeasured provider config must not claim measurement outputs")
        return self


class ProfileEvidence(ContractModel):
    """One report field cited by a profile decision, copied verbatim for traceability.

    Every number a frozen profile states about the world comes from exactly one field of the
    Stage 4c ablation report or the Stage 3 shortlist, named here as a dotted path with the
    value read from it.  The freeze command reads these values from the reports rather than
    hard-coding them, so the profile can be re-derived byte-for-byte from the evidence.
    """

    report: Literal["ablations", "shortlist"]
    field: Annotated[str, Field(min_length=1)]
    value: JsonValue


class ProfileEngine(ContractModel):
    provider: str
    capability: Literal["clip_recognizer", "file_scanner", "local_index_query"]
    cost_class: Literal["free", "paid", "self_hosted_free"]
    enabled: bool
    eligible_when_available: bool
    eligibility_condition: str
    status: str
    reason: str
    evidence: list[ProfileEvidence]


class ProfileFeature(ContractModel):
    name: Literal["rescans", "novelty", "transforms", "schedule", "hints"]
    enabled: bool
    certified: bool
    setting: dict[str, JsonValue]
    decision: str
    evidence: list[ProfileEvidence]


class ProfileSchedule(ContractModel):
    window_ms: NonNegativeInt
    hop_ms: NonNegativeInt
    phase_ms: NonNegativeInt


class ProfileRescan(ContractModel):
    enabled: bool
    window_ms: NonNegativeInt
    hop_ms: NonNegativeInt
    phase_ms: NonNegativeInt
    max_generations: NonNegativeInt


class ProfileBudget(ContractModel):
    max_requests_per_media: NonNegativeInt
    max_usd_e2: MoneyE2
    allow_third_party_upload: bool
    shazam_requests_per_minute: NonNegativeInt


class ProfilePaidEstimate(ContractModel):
    provider: str
    status: str
    expected_trial_cost_usd_e2: MoneyE2
    corpus_version: str
    evidence: list[ProfileEvidence]


class ProfileCostReport(ContractModel):
    enabled_engines_usd_e2: MoneyE2
    ablation_corpus_sets: NonNegativeInt
    windows_rescans_on: NonNegativeInt
    windows_rescans_off: NonNegativeInt
    paid_when_enabled: list[ProfilePaidEstimate]
    evidence: list[ProfileEvidence]


class ProfileProvenance(ContractModel):
    ablations_report: str
    ablations_report_ref: Sha256
    ablations_corpus_version: str
    shortlist_report: str
    shortlist_report_ref: Sha256
    shortlist_corpus_version: str


class ProfileRecord(Record):
    """A frozen, immutable, versioned recognition profile derived mechanically from evidence."""

    id: Sha1
    name: Literal["free", "max_accuracy"]
    version: str
    frozen: bool
    engine_policy: Literal["free_only", "all_available"]
    enabled_engines: list[str]
    engines: list[ProfileEngine]
    transforms_policy: Literal["off", "rescan_only", "global"]
    transform_rates_e4: list[int]
    transform_semitones: list[int]
    schedule: ProfileSchedule
    rescan: ProfileRescan
    novelty_enabled: bool
    hints_enabled: bool
    hints_gate_status: str
    features: list[ProfileFeature]
    budget: ProfileBudget
    cost_report: ProfileCostReport
    frozen_from: ProfileProvenance
    notes: list[str]


# --------------------------------------------------------------------------------------------------
# Stage 5 — calibration & test
# --------------------------------------------------------------------------------------------------
TierLabel = Literal["unclear", "possible", "likely", "verified"]
CalibrationDimensionName = Literal["work", "version", "boundary"]
CalibrationSideName = Literal["start", "end"]


class CalibrationFeatures(Record):
    """Deterministic, integer/fixed-point features of one episode used by the calibrator.

    Every value is an integer, a boolean, or a fixed enum so a fitted calibrator re-derives
    byte-for-byte from the evidence.  ``median_score_raw`` is nullable because most clip
    recognizers (Shazam, the controlled oracle) never emit a per-window score.
    """

    episode_id: Sha1
    candidate_id: Sha1
    # Evidence strength (the plan's ``T`` and ``S``).
    t_ind_e4: NonNegativeInt
    n_logical_trials: NonNegativeInt
    n_selected_observations: NonNegativeInt
    span_ms: NonNegativeInt
    support_total_ms: NonNegativeInt
    # Alignment residuals and segment count.
    n_alignment_segments: NonNegativeInt
    max_residual_ms: NonNegativeInt
    n_alignment_events: NonNegativeInt
    has_global_alignment: bool
    # Engine agreement discounted by the correlation prior.
    n_providers: NonNegativeInt
    engine_agreement_e4: ConfidenceE4
    # Transform consistency and provider score.
    transform_consistency_e4: ConfidenceE4
    n_score_raw: NonNegativeInt
    median_score_raw: int | None
    # One vote per ``provenance_group`` from supporting hints.
    n_provenance_groups: NonNegativeInt
    hint_vote_e4: ConfidenceE4
    # Contradictions, identity conflicts and version agreement.
    competing: bool
    n_competing_candidates: NonNegativeInt
    identity_conflicts: NonNegativeInt
    contested: bool
    recording_supported: bool
    version_ids_count: NonNegativeInt
    claim: Literal["performed", "component_evidence"]
    heuristic_work_tier: TierLabel
    heuristic_version_tier: TierLabel
    heuristic_boundary_tier: TierLabel


class CalibrationBin(ContractModel):
    """One step of a monotone (pool-adjacent-violators) isotonic step function.

    For an ordering index ``x`` the calibrated precision is the ``calibrated_e4`` of the last bin
    whose ``index_ge <= x`` (``0`` below the first bin).  Values are non-decreasing by construction.
    """

    index_ge: int
    calibrated_e4: ConfidenceE4
    n: NonNegativeInt


class CalibrationTierThreshold(ContractModel):
    tier: Literal["possible", "likely", "verified"]
    target_e4: ConfidenceE4
    # ``null`` when no ordering index on the calibration split reaches the target precision.
    min_index: int | None
    achieved_precision_e4: ConfidenceE4 | None


class CalibrationDimensionModel(ContractModel):
    dimension: CalibrationDimensionName
    index_formula: str
    isotonic: list[CalibrationBin]
    tier_thresholds: list[CalibrationTierThreshold]
    n: NonNegativeInt
    n_positive: NonNegativeInt


class CalibrationIntervalModel(ContractModel):
    """Empirical prediction interval for a proved bound, learned on the calibration split.

    The offsets are quantiles of ``(true boundary - proved bound)`` in milliseconds; applying them
    to a new episode's proved bound gives ``[proved + q_lo_ms, proved + q_hi_ms]``.
    """

    side: CalibrationSideName
    q_lo_ms: int
    q_hi_ms: int
    coverage_target_e4: ConfidenceE4
    achieved_coverage_e4: ConfidenceE4
    method: str
    n: NonNegativeInt


class CalibrationCertEntry(ContractModel):
    dimension: Literal["work", "version", "start", "end", "boundary"]
    tier: Literal["possible", "likely", "verified"]
    status: Literal["certified", "provisional"]
    n_test_predictions: NonNegativeInt
    lower_bound_e4: ConfidenceE4
    test_version: str


class CalibrationProvenance(ContractModel):
    corpus_version: str
    population: str
    split_seed: NonNegativeInt
    calibration_set_ids: list[str]
    method: str


class CalibrationModelRecord(Record):
    """A frozen, immutable, versioned score/tier/PI calibrator for one ``(profile, dimension)`` set.

    ``population`` names what the calibrator was fit on; a calibrator fit on the controlled corpus
    carries the controlled label and is machinery validation only, never real-mix certification.
    """

    id: Sha1
    profile: str
    version: str
    frozen: bool
    corpus_version: str
    config_hash: Sha256
    population: str
    method: str
    n_calibration_sets: NonNegativeInt
    n_calibration_predictions: NonNegativeInt
    feature_names: list[str]
    dimensions: list[CalibrationDimensionModel]
    intervals: list[CalibrationIntervalModel]
    certification: list[CalibrationCertEntry]
    frozen_from: CalibrationProvenance
    notes: list[str]


class CalibrationValidationSet(ContractModel):
    set_id: str
    split: str
    n_episodes: NonNegativeInt


class CalibrationValidationTier(ContractModel):
    dimension: Literal["work", "version", "start", "end", "boundary"]
    tier: Literal["possible", "likely", "verified"]
    n: NonNegativeInt
    correct: NonNegativeInt
    precision_e4: ConfidenceE4
    cp_lower_e4: ConfidenceE4
    cluster_lower_e4: ConfidenceE4


class CalibrationValidationInterval(ContractModel):
    side: Literal["start", "end", "boundary"]
    coverage_e4: ConfidenceE4
    median_width_ms: NonNegativeInt
    p90_width_ms: NonNegativeInt
    winkler_score: NonNegativeInt


class CalibrationValidationRecord(Record):
    """Machinery-validation report for the calibration code path on a controlled corpus.

    ``population`` is fixed to the controlled label: this proves the code path end-to-end, it is not
    a real-mix certification and certifies no tier.
    """

    corpus_version: str
    profile: str
    config_hash: Sha256
    population: str
    calibration_model_ref: Sha256
    split_seed: NonNegativeInt
    calibration_sets: list[CalibrationValidationSet]
    test_sets: list[CalibrationValidationSet]
    n_calibration_predictions: NonNegativeInt
    n_test_predictions: NonNegativeInt
    tiers: list[CalibrationValidationTier]
    intervals: list[CalibrationValidationInterval]
    certification: list[CalibrationCertEntry]
    notes: list[str]


SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "source": SourceRecord,
    "pcm": PcmRecord,
    "window": WindowRecord,
    "query": QueryRecord,
    "observation": ObservationRecord,
    "hint": HintRecord,
    "identity_node": IdentityNode,
    "identity_assertion": IdentityAssertion,
    "identity_work": IdentityWork,
    "identity_candidate": IdentityCandidate,
    "identities": IdentitiesRecord,
    "episode": EpisodeRecord,
    "gap": GapRecord,
    "durations": DurationsRecord,
    "rescan_request": RescanRequestRecord,
    "episodes": EpisodesFile,
    "ground_truth": GroundTruthRecord,
    "benchmark_report": BenchmarkReportRecord,
    "shortlist_report": ShortlistReportRecord,
    "invocation_journal_entry": InvocationJournalEntry,
    "raw_index_entry": RawIndexEntry,
    "provider_config": ProviderConfigRecord,
    "profile": ProfileRecord,
    "calibration_features": CalibrationFeatures,
    "calibration_model": CalibrationModelRecord,
    "calibration_validation": CalibrationValidationRecord,
}


# Dotted names denote a nested field. List-valued components are sorted before canonical encoding.
NATURAL_KEY_FIELDS: dict[str, tuple[str, ...]] = {
    "source": ("canonical_url",),
    "pcm": ("media_key",),
    "window": (
        "generation",
        "start_ms",
        "support_ms.1",
        "transform.type",
        "transform.rate_e4",
        "transform.semitones",
    ),
    "query": ("provider", "capability", "target", "provider_config_version", "scan_policy"),
    # ``transform`` (rev 5.2) separates sibling hypotheses of one window: byte-identical sibling
    # WAVs share a cache key, so ``query_id`` fans out, and a ``pitch`` sibling has exactly the
    # same ``mix_span_ms`` as its ``none`` parent.  Scanner observations carry ``transform: null``.
    "observation": ("query_id", "mix_span_ms", "raw_label_hash", "native_index", "transform"),
    "hint": ("connector", "source_record_id"),
    "identity_node": ("id",),  # id is exactly ``ns:value``.
    "identity_assertion": ("a", "b", "relation", "source.record_id"),
    "identity_work": ("normalised_artist_title",),
    "identity_candidate": ("member_nodes",),
    "episode": ("candidate_id", "occurrence_index", "first_support_start_ms"),
    "gap": ("start_ms", "end_ms"),
    "rescan_request": ("generation", "trigger", "start_ms", "end_ms", "policy"),
    "ground_truth": ("set_id",),
    "benchmark_report": ("corpus_version", "profile", "config_hash"),
    "invocation_journal_entry": ("invocation_id",),
    "raw_index_entry": ("cache_key",),
    "provider_config": ("provider", "version"),
    "profile": ("name", "version"),
    "calibration_features": ("episode_id",),
    "calibration_model": ("profile", "version"),
    "calibration_validation": ("corpus_version", "profile", "config_hash"),
}


def make_id(media_key: str, record_type: str, natural_key: str) -> str:
    """Return ``sha1(media_key || record_type || natural_key)`` exactly as revision 5 specifies."""

    payload = f"{media_key}{record_type}{natural_key}".encode()
    return sha1(payload, usedforsecurity=False).hexdigest()


def compose_natural_key(record_type: str, values: Mapping[str, Any] | BaseModel) -> str:
    """Compose the declared natural key as an unambiguous canonical JSON array."""

    if record_type not in NATURAL_KEY_FIELDS:
        raise KeyError(f"no natural key is declared for {record_type}")
    data = (
        values.model_dump(mode="json", by_alias=True) if isinstance(values, BaseModel) else values
    )

    def extract(path: str) -> Any:
        current: Any = data
        for component in path.split("."):
            current = current[int(component)] if component.isdigit() else current[component]
        if path == "member_nodes":
            return sorted(current)
        return current

    parts = [extract(path) for path in NATURAL_KEY_FIELDS[record_type]]
    _reject_floats(parts)
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sort_records(
    records: list[Mapping[str, Any] | BaseModel],
) -> list[Mapping[str, Any] | BaseModel]:
    """Apply the plan's stable record ordering convention."""

    def key(record: Mapping[str, Any] | BaseModel) -> tuple[int, int, str]:
        value = record.model_dump(mode="json") if isinstance(record, BaseModel) else record
        if "start_ms" in value:
            return (0, int(value["start_ms"]), str(value.get("id", "")))
        return (1, 0, str(value.get("id", "")))

    return sorted(records, key=key)


def derive_source_key(canonical_url: str) -> str:
    """Derive a source key from the already-canonical URL."""

    return sha256(canonical_url.encode("utf-8")).hexdigest()


def derive_media_key(data: bytes) -> str:
    """Derive a media key from the exact original bytes."""

    return sha256(data).hexdigest()


def derive_media_key_from_path(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_parts(*parts: str) -> str:
    return sha256("".join(parts).encode("utf-8")).hexdigest()


def clip_cache_key(wav_sha256: str, provider: str, provider_config_version: str) -> str:
    return _hash_parts(wav_sha256, provider, provider_config_version)


def file_scan_cache_key(
    asset_kind: str,
    asset_sha256: str,
    provider: str,
    provider_config_version: str,
    scan_policy: str,
) -> str:
    return _hash_parts(asset_kind, asset_sha256, provider, provider_config_version, scan_policy)


def local_index_cache_key(wav_sha256: str, index_id: str, index_version: str) -> str:
    return _hash_parts(wav_sha256, index_id, index_version)


def schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """Export a validation schema with stable aliases and a contract identifier."""

    return model.model_json_schema(by_alias=True, union_format="any_of")

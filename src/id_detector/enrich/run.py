"""Assemble ``enrich/acquire.json`` for an existing analysis (the ``acquire`` command's core).

This orchestrates the four catalogue lookups plus the SoundCloud acquisition-flag lookup for every
*identified* episode (badge ≥ possible), applies the direct-link policy, folds any strongly-agreeing
recording ids back through the non-authoritative version-corroboration path, and writes the
deterministic ``acquire.json`` artefact and its completion sidecar.  It never mutates the immutable
``fuse/`` artefacts.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import httpx

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    AcquireDirectLink,
    AcquireEpisode,
    AcquireFile,
    AcquireSearchLink,
    AcquireSoundcloud,
    EpisodesFile,
    IdentitiesRecord,
    SourceRecord,
)
from id_detector.enrich.feedback import augmented_version_support, enriched_version_status
from id_detector.enrich.http import EnrichHttp, build_async_client
from id_detector.enrich.links import search_links
from id_detector.enrich.lookups import (
    fetch_deezer,
    fetch_discogs,
    fetch_itunes,
    fetch_musicbrainz,
)
from id_detector.enrich.match import (
    Candidate,
    match_confidence_e4,
    strong_agreement,
)
from id_detector.enrich.soundcloud import (
    SoundcloudFlags,
    build_token_context,
    fetch_soundcloud_flags,
)
from id_detector.io import atomic_write_json, read_text, write_completion_sidecar
from id_detector.semantics import RECORDING_NAMESPACES

IDENTIFIED_BADGES = {"possible", "likely", "verified"}
DIRECT_KIND = {
    "deezer": "stream",
    "apple": "purchase",
    "musicbrainz": "catalogue",
    "discogs": "catalogue",
}
# A lookup result that is not direct-eligible still becomes a labelled *search* result link when it
# scores at least this well, so a near miss is offered without ever implying it is exact.
SEARCH_RESULT_MIN_E4 = 5_000
_PAREN = re.compile(r"[\(\[]([^)\]]*)[\)\]]")


@dataclass(frozen=True)
class AcquireResult:
    record: AcquireFile
    path: Path
    counts: dict[str, object]


def final_identities_path(media_dir: Path) -> Path:
    generations = sorted(
        (int(match.group(1)), path)
        for path in (media_dir / "fuse").glob("identities.gen*.json")
        if (match := re.search(r"identities\.gen(\d+)\.json$", path.name))
    )
    if not generations:
        raise FileNotFoundError(f"no fuse/identities.gen*.json under {media_dir}")
    return generations[-1][1]


def load_analysis(media_dir: Path) -> tuple[EpisodesFile, IdentitiesRecord]:
    episodes = EpisodesFile.model_validate_json(read_text(media_dir / "fuse" / "episodes.json"))
    identities = IdentitiesRecord.model_validate_json(read_text(final_identities_path(media_dir)))
    return episodes, identities


def candidate_artist_title(identities: IdentitiesRecord, candidate_id: str) -> tuple[str, str]:
    candidate = next(
        (item for item in identities.candidates if item.canonical_id == candidate_id), None
    )
    if candidate is None:
        return "Unknown artist", "Unknown title"
    provider_labels = [
        node.label
        for node in identities.nodes
        if node.id in candidate.member_nodes and node.ns != "text"
    ]
    labels = provider_labels
    if not labels:
        work = next((w for w in identities.works if w.work_id == candidate.work_id), None)
        if work is not None:
            labels = [node.label for node in identities.nodes if node.id in work.member_nodes]
    label = min(labels) if labels else "Unknown artist - Unknown title"
    if " - " not in label:
        return "Unknown artist", label
    artist, title = label.split(" - ", 1)
    return artist, title


def _version_qualifier(title: str) -> str | None:
    match = _PAREN.search(title)
    if match and match.group(1).strip():
        return match.group(1).strip()
    return None


def _best(candidates: list[Candidate], artist: str, title: str, ref: int | None):
    best: tuple[int, Candidate] | None = None
    for candidate in candidates:
        score = match_confidence_e4(artist, title, candidate, reference_duration_ms=ref)
        if best is None or score > best[0]:
            best = (score, candidate)
    return best


@dataclass
class _EpisodePlan:
    episode_id: str
    candidate_id: str
    artist: str
    title: str
    version_qualifier: str | None
    original_version_status: str
    contested: bool
    member_nodes: frozenset[str]
    direct: list[dict[str, object]]
    search: list[dict[str, object]]
    soundcloud: SoundcloudFlags | None
    recording_ids: list[tuple[str, str, str]]


def _classify_source(plan: _EpisodePlan, source: str, best: tuple[int, Candidate] | None) -> None:
    if best is None:
        return
    score, candidate = best
    shared_id = any(
        f"{ns}:{value}" in plan.member_nodes for ns, value in candidate.recording_ids.items()
    )
    strong = strong_agreement(plan.artist, plan.title, candidate.artist, candidate.title)
    if shared_id or strong:
        plan.direct.append(
            {
                "source": source,
                "url": candidate.url,
                "kind": DIRECT_KIND[source],
                "match_confidence": score,
                "candidate": candidate,
            }
        )
        if strong:
            for ns, value in candidate.recording_ids.items():
                if ns in RECORDING_NAMESPACES:
                    plan.recording_ids.append((source, ns, value))
    elif score >= SEARCH_RESULT_MIN_E4:
        plan.search.append({"source": source, "url": candidate.url})


async def build_acquire(
    *,
    source: SourceRecord,
    media_dir: Path,
    episodes: EpisodesFile,
    identities: IdentitiesRecord,
    http: EnrichHttp,
    sc_client: httpx.AsyncClient | None,
    enable_soundcloud: bool,
) -> AcquireResult:
    candidates_by_id = {item.canonical_id: item for item in identities.candidates}
    token_context = (
        build_token_context(source, http.cache_root, sc_client)
        if enable_soundcloud and sc_client is not None
        else None
    )

    plans: list[_EpisodePlan] = []
    for episode in episodes.episodes:
        if episode.badge not in IDENTIFIED_BADGES:
            continue
        artist, title = candidate_artist_title(identities, episode.candidate_id)
        candidate = candidates_by_id.get(episode.candidate_id)
        plan = _EpisodePlan(
            episode_id=episode.id,
            candidate_id=episode.candidate_id,
            artist=artist,
            title=title,
            version_qualifier=_version_qualifier(title),
            original_version_status=episode.version_status,
            contested=bool(candidate.contested) if candidate else False,
            member_nodes=frozenset(candidate.member_nodes if candidate else ()),
            direct=[],
            search=list(search_links(artist, title)),
            soundcloud=None,
            recording_ids=[],
        )
        reference = None  # a mix span is not a track duration; see stage report / plan gate.
        for source_name, fetcher in (
            ("deezer", fetch_deezer),
            ("apple", fetch_itunes),
            ("musicbrainz", fetch_musicbrainz),
            ("discogs", fetch_discogs),
        ):
            found = await fetcher(http, artist, title)
            _classify_source(plan, source_name, _best(found, artist, title, reference))
        if token_context is not None:
            plan.soundcloud = await fetch_soundcloud_flags(
                http,
                token_context=token_context,
                artist=artist,
                title=title,
                reference_duration_ms=reference,
            )
        plans.append(plan)

    enrich_ids = {plan.candidate_id: plan.recording_ids for plan in plans if plan.recording_ids}
    supported = augmented_version_support(identities, enrich_ids)

    acquire_episodes: list[AcquireEpisode] = []
    for plan in plans:
        is_supported = supported.get(plan.candidate_id, False)
        version_status = enriched_version_status(
            plan.original_version_status, contested=plan.contested, supported=is_supported
        )
        direct = [
            AcquireDirectLink(
                source=item["source"],
                url=str(item["url"]),
                kind=item["kind"],
                match_confidence=int(item["match_confidence"]),
                corroborates_version=bool(item["candidate"].recording_ids) and is_supported,
            )
            for item in sorted(plan.direct, key=lambda d: (d["source"], d["url"]))
        ]
        search = [
            AcquireSearchLink(source=item["source"], url=str(item["url"]))
            for item in sorted(plan.search, key=lambda d: (d["source"], d["url"]))
        ]
        soundcloud = (
            AcquireSoundcloud(
                classification=plan.soundcloud.classification,
                downloadable=plan.soundcloud.downloadable,
                has_downloads_left=plan.soundcloud.has_downloads_left,
                purchase_url=plan.soundcloud.purchase_url,
                purchase_title=plan.soundcloud.purchase_title,
                license=plan.soundcloud.license,
                permalink_url=plan.soundcloud.permalink_url,
                match_confidence=plan.soundcloud.match_confidence_e4,
            )
            if plan.soundcloud is not None
            else None
        )
        acquire_episodes.append(
            AcquireEpisode(
                episode_id=plan.episode_id,
                candidate_id=plan.candidate_id,
                artist=plan.artist,
                title=plan.title,
                version_qualifier=plan.version_qualifier,
                version_status=version_status,
                direct=direct,
                search=search,
                soundcloud=soundcloud,
            )
        )

    acquire_episodes.sort(key=lambda item: item.episode_id)
    record = AcquireFile(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        media_key=source.media_key,
        generation=episodes.generation,
        episodes=acquire_episodes,
    )
    path = media_dir / "enrich" / "acquire.json"
    atomic_write_json(path, record)
    write_completion_sidecar(path, {"fuse/episodes.json": media_dir / "fuse" / "episodes.json"})
    return AcquireResult(record=record, path=path, counts=_summarise(acquire_episodes))


def _summarise(episodes: list[AcquireEpisode]) -> dict[str, object]:
    direct_by_source: Counter[str] = Counter()
    free_downloads = gate_links = buy_links = search_only = 0
    for episode in episodes:
        for link in episode.direct:
            direct_by_source[link.source] += 1
        classification = episode.soundcloud.classification if episode.soundcloud else "none"
        free_downloads += classification == "free_download_native"
        gate_links += classification == "gate_link"
        buy_links += classification == "buy_link"
        if not episode.direct:
            search_only += 1
    return {
        "episodes": len(episodes),
        "direct_links_by_source": dict(sorted(direct_by_source.items())),
        "direct_links_total": sum(direct_by_source.values()),
        "free_download_flags": free_downloads,
        "gate_links": gate_links,
        "buy_links": buy_links,
        "search_only_rows": search_only,
    }


async def enrich_media_dir(
    *,
    source: SourceRecord,
    media_dir: Path,
    cache_root: Path,
    refresh: bool = False,
    enable_soundcloud: bool = True,
) -> AcquireResult:
    """Run enrichment end-to-end for a media dir, owning the HTTP clients."""

    episodes, identities = load_analysis(media_dir)
    client = build_async_client()
    sc_client = build_async_client() if enable_soundcloud else None
    http = EnrichHttp(client=client, cache_root=cache_root, refresh=refresh)
    try:
        return await build_acquire(
            source=source,
            media_dir=media_dir,
            episodes=episodes,
            identities=identities,
            http=http,
            sc_client=sc_client,
            enable_soundcloud=enable_soundcloud,
        )
    finally:
        await client.aclose()
        if sc_client is not None:
            await sc_client.aclose()

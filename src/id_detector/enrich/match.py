"""Version-aware normalisation, agreement, and candidate scoring for enrichment.

Enrichment is *non-authoritative*: it never rewrites an episode's identity.  These helpers only
decide whether a catalogue candidate agrees strongly enough with an already-identified episode to
show a *direct item link* (the plan's exact-ID / strong artist·title·version rule) versus a plain,
labelled *search link*.  All arithmetic is done in Python floats internally; only the final
``match_confidence`` is emitted as an integer ten-thousandth (0..10000), so no float reaches an
artefact.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Words that mark a *version* of a work (remix / edit / dub / …).  A parenthetical group or a
# trailing " - …" segment containing one of these is treated as the version qualifier, never as part
# of the base title, so "Poison" vs "That Girl Is Poison (Original Mix)" never match on title alone.
VERSION_WORDS = frozenset(
    {
        "remix",
        "rmx",
        "edit",
        "mix",
        "mixed",
        "dub",
        "vip",
        "instrumental",
        "acapella",
        "version",
        "rework",
        "refix",
        "flip",
        "bootleg",
        "extended",
        "radio",
        "club",
        "original",
        "alternate",
        "edition",
        "intro",
        "outro",
        "short",
        "live",
        "remaster",
        "remastered",
        "rerub",
        "rub",
    }
)

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_PAREN = re.compile(r"[\(\[\{]([^)\]}]*)[\)\]}]")
_DASH_SPLIT = re.compile(r"\s[-–—~]\s")

MATCH_STRONG_E4 = 8_500  # score at or above which a match is "strong" independent of exact tokens


def _fold(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold().strip())


def tokens(value: str | None) -> tuple[str, ...]:
    return tuple(_TOKEN.findall(_fold(value)))


def parse_title(title: str | None) -> tuple[tuple[str, ...], frozenset[str]]:
    """Split a title into base tokens and a version-qualifier token set.

    Parenthetical/bracketed groups always become version material; a trailing dash is version
    material only when it contains a :data:`VERSION_WORDS` keyword (so "Artist - Title" stays a base
    title but "Title - Radio Edit" does not).
    """

    folded = _fold(title)
    version_fragments = list(_PAREN.findall(folded))
    base = _PAREN.sub(" ", folded)
    parts = _DASH_SPLIT.split(base)
    if len(parts) > 1:
        tail = parts[-1]
        if any(word in _TOKEN.findall(tail) for word in VERSION_WORDS):
            version_fragments.append(tail)
            base = " - ".join(parts[:-1])
    base_tokens = tuple(_TOKEN.findall(base))
    version_tokens = frozenset(
        token for fragment in version_fragments for token in _TOKEN.findall(fragment)
    )
    return base_tokens, version_tokens


@dataclass(frozen=True)
class Candidate:
    """A catalogue lookup result, normalised into the shape the policy needs."""

    source: str
    url: str
    artist: str
    title: str
    album: str | None = None
    duration_ms: int | None = None
    recording_ids: dict[str, str] = field(default_factory=dict)
    isrcs: tuple[str, ...] = ()


def _jaccard(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    a, b = set(left), set(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def artist_agrees(episode_artist: str, candidate_artist: str) -> bool:
    left, right = tokens(episode_artist), tokens(candidate_artist)
    return bool(left) and left == right


def title_agrees(episode_title: str, candidate_title: str) -> bool:
    base_a, ver_a = parse_title(episode_title)
    base_b, ver_b = parse_title(candidate_title)
    return bool(base_a) and base_a == base_b and ver_a == ver_b


def strong_agreement(
    episode_artist: str, episode_title: str, candidate_artist: str, candidate_title: str
) -> bool:
    """The plan's direct-link gate: normalised equality of artist *and* title incl. version."""

    return artist_agrees(episode_artist, candidate_artist) and title_agrees(
        episode_title, candidate_title
    )


def duration_agreement(reference_ms: int | None, candidate_ms: int | None) -> float | None:
    """±3 s is full agreement; decays to zero by 15 s. ``None`` when no reference is available."""

    if not reference_ms or not candidate_ms:
        return None
    diff = abs(reference_ms - candidate_ms)
    if diff <= 3_000:
        return 1.0
    return max(0.0, 1.0 - (diff - 3_000) / 12_000)


def match_confidence_e4(
    episode_artist: str,
    episode_title: str,
    candidate: Candidate,
    *,
    reference_duration_ms: int | None = None,
) -> int:
    """Score normalised artist/title/version agreement plus optional duration agreement."""

    artist = (
        1.0
        if artist_agrees(episode_artist, candidate.artist)
        else _jaccard(tokens(episode_artist), tokens(candidate.artist))
    )
    base_a, ver_a = parse_title(episode_title)
    base_b, ver_b = parse_title(candidate.title)
    base = 1.0 if base_a and base_a == base_b else _jaccard(base_a, base_b)
    if ver_a == ver_b:
        version = 1.0
    elif not ver_a or not ver_b:
        version = 0.4
    else:
        version = _jaccard(tuple(ver_a), tuple(ver_b))

    duration = duration_agreement(reference_duration_ms, candidate.duration_ms)
    if duration is None:
        total = artist * 0.39 + base * 0.39 + version * 0.22
    else:
        total = artist * 0.32 + base * 0.32 + version * 0.18 + duration * 0.18
    return max(0, min(10_000, round(total * 10_000)))

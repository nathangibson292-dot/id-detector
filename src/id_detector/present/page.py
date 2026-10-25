"""Stage 7 static page generator.

Writes one self-contained ``present/index.html`` per run: an embedded platform player, an
evidence-first timeline, and a clickable tracklist whose rows seek the player to
``best_start_ms - lead_in``.  The only external resources are the chosen platform's own player
script/iframe; everything else (CSS, JS, data) is inlined so the page works offline.

Privacy: the page never contains usernames or comment text.  Track labels come from catalogue
identity nodes, hints contribute only a boolean marker, and the only free text rendered is the set
title from ``source.json`` (the same field the read-only server index shows).
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from id_detector.contracts import (
    AcquireFile,
    EpisodeRecord,
    EpisodesFile,
    IdentitiesRecord,
    SourceRecord,
)
from id_detector.fuse.identity import audio_work_key
from id_detector.io import atomic_write_bytes, write_completion_sidecar
from id_detector.playlists import PLAYLIST_CSS, PLAYLIST_JS, row_actions_html
from id_detector.present.exports import (
    CanonicalProjection,
    _format_time,
    build_projection,
    comment_credit_text,
)
from id_detector.present.theme import (
    LIGHT_CSS,
    PLATFORM_NAMES,
    THEME_JS,
    head_html,
    topbar_html,
)

#: Default seek lead-in.  The page exposes a control to change it live; this is the seed value and
#: the value the seek-correctness test pins.
DEFAULT_LEAD_IN_MS = 5_000

#: The plan's unresolved-boundary fallback: a censored side is drawn outward at most this far.
UNRESOLVED_CAP_MS = 120_000

#: Bump when the page's look or behaviour changes: ``present.refresh.ensure_fresh_page`` re-renders
#: any written page whose ``<meta name="id-detector-page">`` stamp is older, so already-analysed
#: mixes pick up the new page the next time they are opened (no re-analysis).
PAGE_VERSION = 29


# --------------------------------------------------------------------------------------------------
# Shared seek arithmetic.  These two functions are the single source of truth; ``render_page`` emits
# the byte-identical JavaScript below, and the Stage 7 seek test imports them to prove the page's UI
# arithmetic lands within 1 s of ``best_start_ms - lead_in``.  This is UI arithmetic only and is
# entirely separate from any measured boundary error.
# --------------------------------------------------------------------------------------------------
def seek_target_ms(best_start_ms: int, lead_in_ms: int) -> int:
    """The canonical (millisecond) seek target, clamped so the player never seeks before 0."""

    return max(0, best_start_ms - lead_in_ms)


def seek_argument(platform: str, best_start_ms: int, lead_in_ms: int) -> int:
    """The value handed to the active player's seek API, in that player's native unit.

    SoundCloud's ``seekTo`` takes milliseconds; the YouTube IFrame ``seekTo`` and the Mixcloud
    widget ``seek`` take (whole) seconds.  Flooring to whole seconds is the only rounding, and it
    can move the realised target at most 999 ms — inside the 1 s acceptance tolerance.
    """

    target = seek_target_ms(best_start_ms, lead_in_ms)
    if platform == "soundcloud":
        return target
    return target // 1000


_SEEK_JS = """
    // Shared seek arithmetic — kept byte-for-byte in step with id_detector.present.page.
    function seekTargetMs(bestStartMs, leadInMs) {
      return Math.max(0, bestStartMs - leadInMs);
    }
    function seekArgument(bestStartMs, leadInMs) {
      var target = seekTargetMs(bestStartMs, leadInMs);
      if (PLATFORM === 'soundcloud') { return target; }
      return Math.floor(target / 1000);
    }
"""


# --------------------------------------------------------------------------------------------------
# Shared playhead arithmetic.  Like ``seek_target_ms``/``seek_argument`` above, ``playhead_x`` is
# the single source of truth for placing the moving playhead on the timeline; ``render_page`` emits
# byte-identical ``playheadX`` JavaScript and the Stage 11 playhead test imports this function to
# prove the position→pixel mapping clamps at both ends and lands the midpoint.
# --------------------------------------------------------------------------------------------------
def playhead_x(position_ms: int, duration_ms: int, width_px: float) -> float:
    """Pixel x-offset of the timeline playhead for ``position_ms``.

    The fraction ``position_ms / duration_ms`` is clamped to ``[0, 1]`` and scaled by the timeline's
    measured pixel width, so a position at or before 0 pins the head to the left edge and one at or
    past the duration pins it to the right.  A non-positive duration or width collapses to 0 — there
    is nothing to place the head against.
    """

    if duration_ms <= 0 or width_px <= 0:
        return 0.0
    fraction = position_ms / duration_ms
    fraction = max(0.0, min(1.0, fraction))
    return fraction * width_px


_PLAYHEAD_JS = """
    // Shared playhead arithmetic — kept byte-for-byte in step with id_detector.present.page.
    function playheadX(positionMs, durationMs, widthPx) {
      if (durationMs <= 0 || widthPx <= 0) { return 0.0; }
      var fraction = positionMs / durationMs;
      fraction = Math.max(0, Math.min(1, fraction));
      return fraction * widthPx;
    }
    function formatTime(ms) {
      var total = Math.floor(ms / 1000);
      var hours = Math.floor(total / 3600);
      var minutes = Math.floor((total % 3600) / 60);
      var seconds = total % 60;
      var ss = (seconds < 10 ? '0' : '') + seconds;
      if (hours > 0) {
        var mm = (minutes < 10 ? '0' : '') + minutes;
        return hours + ':' + mm + ':' + ss;
      }
      return minutes + ':' + ss;
    }
"""


@dataclass(frozen=True)
class EmbedPlan:
    """How (or whether) to embed the run's platform player."""

    kind: str  # "soundcloud" | "youtube" | "mixcloud" | "link"
    identifier: str  # permalink / video id / feed path — depends on kind
    link_url: str  # always a plain fallback link to the set
    reason: str  # why a link fallback was chosen (for the report / title), or ""


def _embeddable(source: SourceRecord) -> tuple[bool, str]:
    """Return ``(embeddable, reason)`` honouring SoundCloud ``embeddable_by`` and a generic flag."""

    snapshot = source.config_snapshot or {}
    if snapshot.get("embed_disabled") is True:
        return False, "embedding disabled by config"
    embeddable_by = snapshot.get("embeddable_by")
    # SoundCloud info.json exposes ``embeddable_by`` ∈ {"all", "me", "none"}; only "all" permits a
    # third-party embed.  When the field is absent we assume the public default (embeddable).
    if isinstance(embeddable_by, str) and embeddable_by.casefold() not in {"all", ""}:
        return False, f"embeddable_by={embeddable_by}"
    return True, ""


def plan_embed(source: SourceRecord) -> EmbedPlan:
    """Choose a single-embed plan (never a catalogue of embeds — one per page, per the ToU)."""

    link_url = source.canonical_url
    embeddable, reason = _embeddable(source)
    if not embeddable:
        return EmbedPlan("link", "", link_url, reason)
    if source.platform == "soundcloud":
        return EmbedPlan("soundcloud", source.canonical_url, link_url, "")
    if source.platform == "youtube" and source.platform_id:
        return EmbedPlan("youtube", source.platform_id, link_url, "")
    if source.platform == "mixcloud":
        feed = urlsplit(source.canonical_url).path or "/"
        return EmbedPlan("mixcloud", feed, link_url, "")
    return EmbedPlan("link", "", link_url, "platform not embeddable")


def _youtube_id(parts: object) -> str:
    """The video id from a parsed YouTube URL: ``watch?v=``, ``youtu.be/<id>`` or ``embed/<id>``."""

    host = parts.netloc.casefold()  # type: ignore[attr-defined]
    path = parts.path  # type: ignore[attr-defined]
    if "youtu.be" in host:
        segments = [segment for segment in path.split("/") if segment]
        return segments[0] if segments else ""
    query = parse_qs(parts.query)  # type: ignore[attr-defined]
    if query.get("v"):
        return query["v"][0]
    segments = [segment for segment in path.split("/") if segment]
    for marker in ("embed", "shorts", "live"):
        if marker in segments:
            index = segments.index(marker)
            if index + 1 < len(segments):
                return segments[index + 1]
    return ""


def plan_embed_from_url(url: str) -> EmbedPlan:
    """Plan a player embed from a submitted URL alone — no ``SourceRecord`` needed.

    The analysing (progress) page has only the target URL (ingest has not written ``source.json``
    yet), so this mirrors :func:`plan_embed`'s per-platform choices but derives platform and
    identifier from the URL: SoundCloud embeds by the URL itself, YouTube by its video id, Mixcloud
    by its feed path.  A local file or an unrecognised non-web target becomes a plain ``link`` (no
    embed).
    """

    text = (url or "").strip()
    parts = urlsplit(text)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        return EmbedPlan("link", "", "", "local or non-web target")
    host = parts.netloc.casefold()
    if "soundcloud.com" in host:
        return EmbedPlan("soundcloud", text, text, "")
    if "youtube.com" in host or "youtu.be" in host:
        video_id = _youtube_id(parts)
        return (
            EmbedPlan("youtube", video_id, text, "")
            if video_id
            else EmbedPlan("link", "", text, "no video id")
        )
    if "mixcloud.com" in host:
        return EmbedPlan("mixcloud", parts.path or "/", text, "")
    return EmbedPlan("link", "", text, "platform not embeddable")


def _pct(value_ms: int, duration_ms: int) -> float:
    if duration_ms <= 0:
        return 0.0
    return max(0.0, min(100.0, value_ms * 100.0 / duration_ms))


def _band(from_ms: int, to_ms: int, duration_ms: int) -> tuple[float, float]:
    left = _pct(from_ms, duration_ms)
    right = _pct(to_ms, duration_ms)
    return left, max(0.0, right - left)


def _evidence_boundaries(episodes: list[EpisodeRecord]) -> list[int]:
    points: set[int] = set()
    for episode in episodes:
        for span in episode.evidence_support_ms:
            points.add(span[0])
            points.add(span[1])
    return sorted(points)


def _unresolved_zones(
    episode: EpisodeRecord, boundaries: list[int], duration_ms: int
) -> list[tuple[int, int]]:
    """The plan's censored-side unresolved zones: outward from a proved bound to the next evidence
    or ``UNRESOLVED_CAP_MS``, whichever is first — and only where the side is genuinely unknown
    (no audited/held-reference censoring and no calibrated prediction interval)."""

    zones: list[tuple[int, int]] = []
    if episode.start_no_earlier_than_ms is None and episode.start_pi is None:
        anchor = episode.start_no_later_than_ms
        floor = anchor - UNRESOLVED_CAP_MS
        prior = [b for b in boundaries if b < anchor]
        if prior:
            floor = max(floor, prior[-1])
        floor = max(0, floor)
        if anchor > floor:
            zones.append((floor, anchor))
    if episode.end_no_later_than_ms is None and episode.end_pi is None:
        anchor = episode.end_no_earlier_than_ms
        ceil = anchor + UNRESOLVED_CAP_MS
        later = [b for b in boundaries if b > anchor]
        if later:
            ceil = min(ceil, later[0])
        ceil = min(duration_ms, ceil)
        if ceil > anchor:
            zones.append((anchor, ceil))
    return zones


def _timeline_lane(
    episode: EpisodeRecord,
    label: str,
    boundaries: list[int],
    duration_ms: int,
) -> dict[str, Any]:
    extent_left, extent_width = _band(episode.best_start_ms, episode.best_end_ms, duration_ms)
    solids = [_band(span[0], span[1], duration_ms) for span in episode.evidence_support_ms]
    predictions: list[tuple[float, float]] = []
    for interval in (episode.start_pi, episode.end_pi):
        if interval is not None:
            predictions.append(_band(interval.lo, interval.hi, duration_ms))
    unresolved = [
        _band(zone[0], zone[1], duration_ms)
        for zone in _unresolved_zones(episode, boundaries, duration_ms)
    ]
    return {
        "episode_id": episode.id,
        "label": label,
        "badge": episode.badge,
        "extent": {"left": extent_left, "width": extent_width},
        "solids": [{"left": left, "width": width} for left, width in solids],
        "predictions": [{"left": left, "width": width} for left, width in predictions],
        "unresolved": [{"left": left, "width": width} for left, width in unresolved],
    }


def _gap_marker(gap: Any, duration_ms: int) -> dict[str, Any]:
    left, width = _band(gap.start_ms, gap.end_ms, duration_ms)
    return {
        "gap_id": gap.id,
        "left": left,
        "width": width,
        "start_ms": gap.start_ms,
        "end_ms": gap.end_ms,
        "n_windows": gap.evidence.n_windows,
        "n_no_match": gap.evidence.n_no_match,
        "n_error": gap.evidence.n_error,
        "reason": gap.reason,
    }


# --------------------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------------------
def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _safe_href(value: object) -> str | None:
    """An escaped ``href`` for a third-party link, or ``None`` unless it is a plain web URL.

    Buy, download, stream and search links arrive verbatim from SoundCloud, stores and lookups, and
    an uploader writes their own purchase link.  Escaping stops attribute breakout but not a
    ``javascript:`` URL, which would run in this page's origin when clicked.
    """

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        return None
    return _esc(value.strip())


def _acquire_links_html(acquire: dict[str, Any] | None) -> str:
    if not acquire:
        return '<span class="acq none">—</span>'
    chips: list[str] = []
    soundcloud = acquire.get("soundcloud") or {}
    permalink = _safe_href(soundcloud.get("permalink_url"))
    purchase = _safe_href(soundcloud.get("purchase_url"))
    if acquire.get("free_download") and permalink:
        chips.append(
            f'<a class="acq free" target="_blank" rel="noopener" '
            f'href="{permalink}">SoundCloud · Free</a>'
        )
    if acquire.get("gate") and (permalink or purchase):
        # Land on the SoundCloud track page (where the Free Download button lives) rather than
        # bouncing straight to the off-SoundCloud gate host, which reads like a broken link.
        chips.append(
            f'<a class="acq gate" target="_blank" rel="noopener" '
            f'href="{permalink or purchase}">SoundCloud · Gate</a>'
        )
    # A known product URL is more useful than a store search or the SoundCloud landing page.
    buy_url = purchase if acquire.get("buy") else None
    buy_label = "SoundCloud · Buy"
    for link in acquire.get("direct") or ():
        if link.get("kind") == "purchase" and _safe_href(link.get("url")):
            buy_url = _safe_href(link.get("url"))
            buy_label = f"{link.get('source', 'link')} · Buy"
            break
    if acquire.get("buy") and buy_url:
        chips.append(
            f'<a class="acq buy" target="_blank" rel="noopener" '
            f'href="{buy_url}">{_esc(buy_label)}</a>'
        )
    # "Where to get it" means where to hear or buy the track — so keep streaming links (Deezer) but
    # drop pure-database entries (MusicBrainz) that a normal user can't get the track from.
    for link in acquire.get("direct") or ():
        href = _safe_href(link.get("url"))
        if link.get("kind") == "stream" and href:
            source = link.get("source", "link")
            chips.append(
                f'<a class="acq direct" target="_blank" rel="noopener" '
                f'href="{href}">{_esc(source)}</a>'
            )
    # Search fallbacks are the least specific, so cap them (2) to keep the row scannable — for this
    # music Bandcamp/Beatport are the useful stores, ordered as the enrichment ranked them.
    for link in (acquire.get("search_links") or ())[:2]:
        source = link.get("source", "search")
        href = _safe_href(link.get("url"))
        if not href:
            continue
        chips.append(
            f'<a class="acq search" target="_blank" rel="noopener" href="{href}">'
            f"Search {_esc(source)}</a>"
        )
    return "".join(chips) if chips else '<span class="acq none">—</span>'


def _alternatives_html(entry: dict[str, Any]) -> str:
    """A native ``<details>`` disclosure listing the collapsed "could also be" alternatives.

    Everything is inline — the list is already in the page, so expanding it makes no request.
    """

    from id_detector.present.grouping import work_key

    primary_work = work_key(str(entry.get("artist", "")), str(entry.get("title", "")))
    alternatives = tuple(
        alt
        for alt in (entry.get("alternatives") or ())
        if work_key(str(alt.get("artist", "")), str(alt.get("title", ""))) != primary_work
    )
    count = len(alternatives)
    if not count:
        return ""
    items = "".join(
        f'<li><span class="alt-badge badge-{_esc(alt["badge"])}">'
        f"{_esc(str(alt['badge']).upper())}</span> {_esc(alt['track'])}</li>"
        for alt in alternatives
    )
    plural = "s" if count != 1 else ""
    # A crowd row's alternatives are the other answers given at the same moment, not versions.
    summary = (
        f"▸ {count} other answer{plural} in the comments"
        if entry.get("hint_only")
        else f"▸ {count} other track{plural}"
    )
    return (
        f'<details class="alts"><summary>{summary}</summary>'
        f'<ul class="altlist">{items}</ul></details>'
    )


def _tags_html(entry: dict[str, Any]) -> str:
    """At most one plain-language qualifier beside the row's confidence word."""

    version = str(entry["version_status"])
    if entry.get("hint_only"):
        return ""
    if entry.get("engine_corroborated"):
        # U-F31: the tooltip explains the *claim*, not which services made it.  Naming an engine
        # tells a user nothing they can act on and leaks how the product is put together.
        tag = (
            '<span class="hint engine" title="two independent checks agreed on this track at the '
            'same moment">confirmed twice</span>'
        )
    elif version in {"verified", "contested"}:
        tag = f'<span class="tag ver-{_esc(version)}">{_esc(version)}</span>'
    else:
        tag = ""
    if entry.get("hint_supported") and not entry.get("hint_only"):
        tag += (
            '<span class="hint comment" title="a listener named this track in the comments">'
            "confirmed in comments</span>"
        )
    return f'<span class="tags">{tag}</span>' if tag else ""


#: The small play glyph a row's time button shows on hover (decorative; the button is labelled).
_PLAY_ICON = (
    '<svg class="pi" viewBox="0 0 10 10" aria-hidden="true"><path d="M2 1.2v7.6L8.6 5z" '
    'fill="currentColor"/></svg>'
)


def _track_row_html(
    entry: dict[str, Any],
    platform: str | None = None,
    index: int = 0,
    *,
    hidden: str | None = None,
    show_acquire: bool = True,
) -> str:
    badge = "comments" if entry.get("hint_only") else _esc(entry["badge"])
    badge_label = "from comments" if entry.get("hint_only") else badge
    label = f"{_esc(entry['artist'])} — {_esc(entry['title'])}"
    acquire = _acquire_links_html(entry.get("acquire"))
    best_start = int(entry["start_ms"])
    alternatives = _alternatives_html(entry)
    row_class = "track short" if hidden else "track"
    crowd_attr = ""
    if entry.get("hint_only"):
        row_class += " crowd"
        crowd_attr = 'data-crowd="1" '
    acquire_cell = (
        f'<td class="acquire" data-label="Where to get it">{acquire}</td>' if show_acquire else ""
    )
    return (
        f'<tr class="{row_class}" data-episode-id="{_esc(entry["episode_id"])}" '
        f'{crowd_attr}style="--i:{index}" '
        f'id="{_esc(entry["episode_id"])}" '
        f'data-best-start-ms="{best_start}">'
        f'<td class="time" data-label="Time"><button type="button" class="seek" '
        f'aria-label="Play from {_esc(_format_time(best_start))} — {label}">'
        '<span class="eqi" aria-hidden="true"><i></i><i></i><i></i></span>'
        f"{_PLAY_ICON}{_esc(_format_time(best_start))}</button></td>"
        f'<td class="badge badge-{badge}" data-label="Confidence"><span class="pill">'
        f"{badge_label}</span></td>"
        f'<td class="label" data-label="Track"><div class="trk">'
        f'<span class="ar">{_esc(entry["artist"])}</span>'
        f'<span class="sep">—</span><span class="tt">{_esc(entry["title"])}</span>'
        f"{_tags_html(entry)}{alternatives}</div></td>"
        f"{acquire_cell}"
        f'<td class="ops" data-label="Save">{row_actions_html(entry) if not hidden else ""}</td>'
        "</tr>"
    )


def _gap_row_html(entry: dict[str, Any], *, show_acquire: bool = True) -> str:
    start = int(entry["start_ms"])
    end = int(entry["end_ms"])
    span = f"{_format_time(start)}–{_format_time(end)}"
    return (
        f'<tr class="gap" data-gap-id="{_esc(entry["gap_id"])}">'
        f'<td class="time" data-label="Time">{_esc(_format_time(start))}</td>'
        f'<td class="badge badge-gap" data-label="Confidence"><span class="pill">ID</span></td>'
        f'<td class="label" data-label="Track"><span class="tt">Couldn\'t identify</span> '
        f"{_esc(span)}</td>"
        + (
            '<td class="acquire" data-label="Where to get it"><span class="acq none">—</span></td>'
            if show_acquire
            else ""
        )
        + '<td class="ops" data-label="Save"></td>'
        "</tr>"
    )


#: A timeline segment is labelled in place only when it is at least this wide (percent of the set);
#: a narrower one would show a few clipped letters, so it relies on its tooltip instead.
_SEGMENT_LABEL_MIN_PCT = 4.5
_BADGE_WORDS = {"verified": "Verified", "likely": "Likely", "possible": "Possible"}


def _segments(track_entries: list[dict[str, Any]], duration_ms: int) -> list[dict[str, Any]]:
    """One timeline block per shown track: from its start to its end, cut at the next track.

    The blocks are the mix at a glance — each identified track as a coloured stretch, so the gaps
    between them (music nothing identified) stay visibly empty rather than being painted over.
    """

    ordered = sorted(
        track_entries, key=lambda entry: (int(entry["start_ms"]), str(entry["episode_id"]))
    )
    segments: list[dict[str, Any]] = []
    for index, entry in enumerate(ordered):
        start = int(entry["start_ms"])
        end = max(start, int(entry["end_ms"]))
        following = [int(other["start_ms"]) for other in ordered[index + 1 :]]
        later = [value for value in following if value > start]
        if later:
            end = min(end, later[0])
        left, width = _band(start, end, duration_ms)
        crowd = bool(entry.get("hint_only"))
        badge = "comments" if crowd else str(entry["badge"])
        word = "From comments" if crowd else _BADGE_WORDS.get(badge, "Unclear")
        segments.append(
            {
                "id": str(entry["episode_id"]),
                "left": left,
                "width": width,
                "badge": str(entry["badge"]),
                "crowd": crowd,
                "artist": str(entry["artist"]),
                "title": str(entry["title"]),
                "word": word,
                "span": f"{_format_time(start)} – {_format_time(end)}",
                "start": _format_time(start),
            }
        )
    return segments


def _segments_html(segments: list[dict[str, Any]]) -> str:
    """The keyboard-reachable track blocks: one tab stop, arrows walk them, Enter plays one."""

    parts = ['<div class="tl-segs" role="group" aria-label="Tracks on the timeline">']
    for index, seg in enumerate(segments):
        label = (
            f"<span>{_esc(seg['title'])}</span>" if seg["width"] >= _SEGMENT_LABEL_MIN_PCT else ""
        )
        crowd = ' data-crowd="1"' if seg["crowd"] else ""
        parts.append(
            f'<button type="button" class="tl-seg" data-seg="{_esc(seg["id"])}" '
            f'data-badge="{_esc(seg["badge"])}"{crowd} tabindex="{0 if index == 0 else -1}" '
            f'style="left:{seg["left"]:.3f}%;width:{seg["width"]:.3f}%" '
            f'data-title="{_esc(seg["title"])}" data-artist="{_esc(seg["artist"])}" '
            f'data-word="{_esc(seg["word"])}" data-span="{_esc(seg["span"])}" '
            f'aria-label="{_esc(seg["start"])} — {_esc(seg["artist"])} — {_esc(seg["title"])}, '
            f'{_esc(seg["word"].lower())}">{label}</button>'
        )
    parts.append("</div>")
    return "".join(parts)


def _timeline_html(
    lanes: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    segments: list[dict[str, Any]] | None = None,
) -> str:
    parts: list[str] = ['<div class="timeline">']
    for gap in gaps:
        # U-F31: "windows", "no-match" and "error" are engine bookkeeping.  What a listener needs
        # from a striped span is which part of the set it covers.
        title = (
            "nothing identified here — "
            f"{_format_time(gap['start_ms'])} to {_format_time(gap['end_ms'])}"
        )
        parts.append(
            f'<div class="tl-gap" style="left:{gap["left"]:.3f}%;width:{gap["width"]:.3f}%" '
            f'title="{_esc(title)}" aria-hidden="true"></div>'
        )
    for lane in lanes:
        ext = lane["extent"]
        # ``data-badge`` colours the lane by confidence, so the strip reads as a confidence map.
        flags = ""
        if lane.get("short"):
            flags += ' data-short="1"'
        if lane.get("crowd"):
            flags += ' data-crowd="1"'
        if lane.get("comment_confirmed"):
            flags += ' data-comment-confirmed="1"'
        title = str(lane["label"])
        if lane.get("comment_confirmed"):
            title += " — confirmed in comments"
        parts.append(
            f'<div class="tl-lane" data-episode-id="{_esc(lane["episode_id"])}" '
            f'data-badge="{_esc(lane["badge"])}"{flags} '
            f'title="{_esc(title)}" aria-hidden="true">'
        )
        if lane.get("comment_confirmed"):
            parts.append(
                f'<span class="tl-comment" style="left:{lane["comment_left"]:.3f}%">'
                "comment ✓</span>"
            )
        parts.append(
            f'<div class="tl-extent" '
            f'style="left:{ext["left"]:.3f}%;width:{ext["width"]:.3f}%"></div>'
        )
        for zone in lane["unresolved"]:
            parts.append(
                f'<div class="tl-unresolved" '
                f'style="left:{zone["left"]:.3f}%;width:{zone["width"]:.3f}%"></div>'
            )
        for pred in lane["predictions"]:
            parts.append(
                f'<div class="tl-pi" style="left:{pred["left"]:.3f}%;width:{pred["width"]:.3f}%">'
                "</div>"
            )
        for solid in lane["solids"]:
            parts.append(
                f'<div class="tl-solid" data-label="{_esc(lane["label"])}" '
                f'style="left:{solid["left"]:.3f}%;width:{solid["width"]:.3f}%"></div>'
            )
        parts.append("</div>")
    # Live playhead: an absolutely-positioned vertical line that tracks the embedded player.  It
    # starts hidden and is revealed by the first position event; ``pointer-events:none`` lets clicks
    # fall through to the timeline's click-to-seek handler.
    parts.append(_segments_html(segments or []))
    parts.append('<div class="tl-hover" id="tl-hover" aria-hidden="true"></div>')
    parts.append(
        '<div class="playhead" id="playhead" hidden aria-hidden="true">'
        '<span class="playhead-time" id="playhead-time"></span></div>'
    )
    parts.append("</div>")
    return "".join(parts)


_RULER_STEPS_MS = (60_000, 120_000, 300_000, 600_000, 900_000, 1_800_000, 3_600_000)


def _ruler_html(duration_ms: int) -> str:
    """Time ticks under the timeline: the coarsest step that still gives at most 13 labels."""

    if duration_ms <= 0:
        return '<div class="ruler" aria-hidden="true"></div>'
    step = next((s for s in _RULER_STEPS_MS if duration_ms / s <= 13), _RULER_STEPS_MS[-1])
    ticks: list[str] = []
    t = 0
    while t <= duration_ms:
        left = t * 100.0 / duration_ms
        if left <= 98.5:  # a label flush against the right edge would clip
            ticks.append(f'<span style="left:{left:.3f}%">{_esc(_format_time(t))}</span>')
        t += step
    return f'<div class="ruler" aria-hidden="true">{"".join(ticks)}</div>'


_BADGE_ORDER = ("verified", "likely", "possible", "unclear")


def _human_duration(milliseconds: int) -> str:
    minutes = max(0, int(milliseconds)) // 60_000
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} m" if hours else f"{minutes} min"


#: What each frozen §2.3.5 `degraded`/`partial` outcome really means, in plain words.  Keyed by
#: ``status:reason`` first, then by ``status`` alone.  A run is `degraded` for several unrelated
#: reasons — the commonest by far being "Deep ran fine, the free cross-check just did not cover
#: enough of the set" (``reason=secondary_not_achieved``, ``achieved=deep``) — so a single sentence
#: claiming Deep was unavailable and Free was used instead is wrong on most degraded runs.
_STATUS_COPY: dict[str, str] = {
    "degraded:secondary_not_achieved": (
        "The Deep scan ran, but the free second opinion only reached part of the set, so fewer "
        "tracks are double-checked than usual."
    ),
    "degraded:shazam_manual_off": (
        "The Deep scan ran, but the free second opinion was switched off, so no track here is "
        "double-checked."
    ),
    "degraded:provider_unavailable": (
        "The paid engine was unavailable, so this set was scanned with the free engine instead."
    ),
    "degraded": (
        "The Deep scan ran, but the free second opinion was paused, so fewer tracks are "
        "double-checked than usual."
    ),
    "partial:primary_not_achieved": (
        "The scan could not cover all of the set, so some music here was never checked."
    ),
    "partial:provider_unavailable_midrun": (
        "A recognition service stopped part-way through, so the later part of the set was never "
        "checked."
    ),
    "partial:reservation_exhausted": (
        "The paid-scan limit was reached part-way through, so the rest of the set was never "
        "checked."
    ),
    "partial": "Some of the set was never checked, so tracks may be missing from this tracklist.",
}


def _status_banner_html(status: str | None, reason: str | None, achieved: str | None) -> str:
    """The run-status banner, rendered from the run's own frozen status, reason and recipe.

    Shown for `degraded` and `partial` (plan §2.3.5: those are shown with a banner); `complete`
    needs none.  The heading names what the user got — a Deep scan that lost its cross-check is
    still a Deep scan — instead of asserting a fallback that mostly did not happen.
    """

    if status not in {"degraded", "partial"}:
        return ""
    copy = _STATUS_COPY.get(f"{status}:{reason}") or _STATUS_COPY[status]
    if status == "degraded" and achieved == "free":
        heading = "Free scan result."
    elif achieved == "deep":
        heading = "Deep scan, with gaps." if status == "partial" else "Deep scan, less confirmed."
    else:
        heading = "Partial result." if status == "partial" else "Degraded result."
    return f'<div class="run-banner" role="status"><b>{_esc(heading)}</b> {_esc(copy)}</div>'


def _stats_html(projection: CanonicalProjection, duration_ms: int) -> str:
    """The three useful hero tiles: tracks, duration and identified share.

    "Identified" is honest coverage — listening time backed by proved evidence (plus any
    calibrated predicted episode time) over the set's length — not a guess at how many tracks
    were in the set.  Every tile is read off the one canonical projection, coverage included:
    a tile must never describe rows the tracklist beneath it does not list.
    """

    entries = projection.shown_entries
    tracks = [entry for entry in entries if entry["kind"] == "track"]
    n = len(tracks)
    comment_named = {
        audio_work_key(entry.get("artist"), entry.get("title"))
        for entry in tracks
        if entry.get("hint_supported") or entry.get("hint_only")
    }
    comment_only = {
        audio_work_key(entry.get("artist"), entry.get("title"))
        for entry in tracks
        if entry.get("hint_only")
    }
    credit = comment_credit_text(len(comment_named), len(comment_only))
    crowd_note = f" · {_esc(credit)}" if credit else ""
    covered = projection.covered_ms
    pct = int(round(max(0.0, min(100.0, covered * 100.0 / duration_ms)))) if duration_ms else 0
    return (
        '<div class="stats">'
        f'<div class="stat"><div><span class="big">{n}</span>'
        f"<small>track{'s' if n != 1 else ''} found{crowd_note}</small></div></div>"
        f'<div class="stat"><div><span class="big">{_esc(_human_duration(duration_ms))}</span>'
        "<small>set length</small></div></div>"
        f'<div class="stat"><div class="ring" style="--p:{pct}">'
        '<svg viewBox="0 0 36 36" aria-hidden="true"><defs><linearGradient id="ringgrad" '
        'x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ff3d8a"></stop>'
        '<stop offset=".55" stop-color="#8b5cf6"></stop><stop offset="1" stop-color="#22d3ee">'
        "</stop></linearGradient></defs>"
        '<circle class="bg" cx="18" cy="18" r="15.5" pathLength="100"></circle>'
        '<circle class="fg" cx="18" cy="18" r="15.5" pathLength="100"></circle></svg>'
        f"<b>{pct}%</b></div><div><b>of the set identified</b></div></div>"
        "</div>"
    )


def _confidence_html(projection: CanonicalProjection) -> str:
    tracks = [entry for entry in projection.shown_entries if entry["kind"] == "track"]
    counts = dict.fromkeys(_BADGE_ORDER, 0)
    for entry in tracks:
        if not entry.get("hint_only"):
            key = str(entry["badge"])
            counts[key] = counts.get(key, 0) + 1
    total = sum(counts.values())
    bars = "".join(
        f'<i class="c-{key}" style="width:{counts[key] * 100.0 / total:.2f}%"></i>'
        for key in _BADGE_ORDER
        if total and counts[key]
    )
    summary = ", ".join(f"{counts[key]} {key}" for key in _BADGE_ORDER if counts[key])
    return (
        '<div class="confidence-summary"><b>Confidence mix</b>'
        f'<div class="conf-bar" role="img" aria-label="{_esc(summary or "No matched tracks")}">'
        f"{bars}</div><span>{_esc(summary or 'No matched tracks')}</span></div>"
    )


def _embed_html(embed: EmbedPlan, audio_src: str | None = None) -> str:
    href = _safe_href(embed.link_url)
    link = (
        f'<a class="setlink" target="_blank" rel="noopener" href="{href}">Open the set ↗</a>'
        if href
        else ""
    )
    if audio_src:
        # Play the fetched original locally: robust (works even when the platform video is removed
        # or embedding is disabled) and reliably seekable, unlike a third-party iframe.  The link to
        # the original set stays as a courtesy.
        return (
            f'<audio id="localplayer" class="localplayer" controls preload="metadata" '
            f'src="{_esc(audio_src)}"></audio>'
            f'<div class="fallback">{link}</div>'
        )
    if embed.kind == "soundcloud":
        src = (
            "https://w.soundcloud.com/player/?url="
            + quote(embed.identifier, safe="")
            + "&show_comments=false&auto_play=false&hide_related=true&color=%23ff3d8a"
        )
        return (
            f'<iframe id="sc-player" title="SoundCloud player" width="100%" height="166" '
            f'scrolling="no" frameborder="no" allow="autoplay" src="{_esc(src)}"></iframe>'
            f'<div class="fallback">{link}</div>'
            '<script src="https://w.soundcloud.com/player/api.js"></script>'
        )
    if embed.kind == "youtube":
        return (
            '<div id="yt-player" class="yt"></div>'
            f'<div class="fallback">{link}</div>'
            '<script src="https://www.youtube.com/iframe_api"></script>'
        )
    if embed.kind == "mixcloud":
        src = "https://player-widget.mixcloud.com/widget/iframe/?feed=" + quote(
            embed.identifier, safe=""
        )
        return (
            f'<iframe id="mc-player" title="Mixcloud player" width="100%" height="120" '
            f'frameborder="0" allow="autoplay" src="{_esc(src)}"></iframe>'
            f'<div class="fallback">{link}</div>'
            '<script src="https://widget.mixcloud.com/media/js/widgetApi.js"></script>'
        )
    reason = f" ({_esc(embed.reason)})" if embed.reason else ""
    return f'<div class="fallback noembed">Player embed unavailable{reason}. {link}</div>'


# The page-specific styles; the shared tokens, top bar, buttons and chips come from ``theme``.
_CSS = (
    """
:root{--pi:#a78bfa;--unresolved:#fbbf24;--extent:#9a9ab0}
/* hero */
.hero{padding:var(--s7) 0 var(--s5);animation:rise var(--t3) var(--ease) both}
.meta{display:flex;gap:var(--s2);flex-wrap:wrap;align-items:center;margin-bottom:var(--s4)}
h1{font:780 clamp(30px,4.4vw,50px)/1.05 var(--display);letter-spacing:-.035em;
margin:0 0 var(--s6);max-width:24ch;overflow-wrap:anywhere}
.run-banner{display:flex;gap:var(--s2);flex-wrap:wrap;align-items:baseline;
border:1px solid color-mix(in srgb,var(--possible) 45%,transparent);border-radius:var(--r2);
padding:12px 16px;margin:0 0 var(--s4);color:var(--fg);
background:color-mix(in srgb,var(--possible) 9%,var(--card))}
.run-banner b{color:var(--possible)}
.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));background:var(--card);
border:1px solid var(--line);border-radius:var(--r3);box-shadow:var(--sh2);overflow:hidden}
.stat{display:flex;align-items:center;gap:var(--s4);padding:var(--s5) var(--s6);min-height:98px;
position:relative}
.stat+.stat{border-left:1px solid var(--line)}
.stat .big{font:780 38px/1 var(--display);letter-spacing:-.04em;font-variant-numeric:tabular-nums}
.stat small{display:block;color:var(--muted);font-size:12.5px;margin-top:7px}
.stat>div>b{font:600 14px/1.3 var(--text);color:var(--fg)}
.ring{position:relative;width:60px;height:60px;flex:none}
.ring svg{width:60px;height:60px;transform:rotate(-90deg)}
.ring circle{fill:none;stroke-width:3.2}
.ring .bg{stroke:var(--track)}
.ring .fg{stroke:url(#ringgrad);stroke-linecap:round;stroke-dasharray:var(--p) 100;
animation:ring 1.1s var(--ease) both}
@keyframes ring{from{stroke-dasharray:0 100}}
.ring b{position:absolute;inset:0;display:grid;place-items:center;font:700 14px/1 var(--display);
font-variant-numeric:tabular-nums}
/* the deck: transport, timeline, ruler, legend */
.deck{margin:var(--s5) 0 0;background:var(--card);border:1px solid var(--line);
border-radius:var(--r4);box-shadow:var(--sh2);padding:var(--s4) var(--s5) var(--s3);
position:relative}
@media (min-width:900px) and (min-height:720px){.deck{position:sticky;top:72px;z-index:30;
box-shadow:var(--sh3)}}
.transport{display:flex;align-items:center;gap:var(--s3);margin-bottom:var(--s4);min-height:48px}
.tp-btn{flex:none;display:inline-grid;place-items:center;width:38px;height:38px;border-radius:50%;
border:1px solid var(--line2);background:var(--hover);color:var(--fg);cursor:pointer;padding:0;
transition:background-color var(--t1) var(--ease),transform var(--t1) var(--ease),
border-color var(--t1) var(--ease)}
.tp-btn:hover{background:var(--hover2);border-color:var(--line3)}
.tp-btn:active{transform:scale(.96)}
.tp-btn svg{width:16px;height:16px}
.tp-play{width:48px;height:48px;border:0;background:var(--grad);color:var(--on-accent);
box-shadow:0 10px 26px -12px #8b5cf6f2,0 0 0 1px #ffffff26 inset}
.tp-play:hover{filter:brightness(1.08);background:var(--grad)}
.tp-play svg{width:18px;height:18px}
.tp-play .i-pause,.is-playing .tp-play .i-play{display:none}
.is-playing .tp-play .i-pause{display:block}
.tp-time{flex:none;font:600 13px/1 var(--mono);font-variant-numeric:tabular-nums;
color:var(--dim);margin-left:var(--s1)}
.tp-time b{color:var(--fg);font-weight:600}
.tp-now{flex:1;min-width:0;display:flex;align-items:center;gap:10px;padding-left:var(--s3);
border-left:1px solid var(--line);margin-left:var(--s2);min-height:34px}
.tp-now .k{flex:none;font:700 10px/1 var(--display);letter-spacing:.14em;color:var(--pink);
padding:5px 7px;border:1px solid color-mix(in srgb,var(--pink) 50%,transparent);border-radius:6px;
background:color-mix(in srgb,var(--pink) 9%,transparent)}
.tp-now .l{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
.tp-now .l small{color:var(--muted);font-weight:500;margin-left:6px}
.tp-now.idle .k{color:var(--dim);border-color:var(--line2);background:none}
.tp-now.idle .l{color:var(--dim);font-weight:500}
.controls{flex:none;font-size:12.5px;color:var(--muted);position:relative}
.controls>summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;gap:6px;
height:34px;padding:0 12px;border-radius:10px;border:1px solid var(--line2);
transition:background-color var(--t1) var(--ease),color var(--t1) var(--ease)}
.controls>summary::-webkit-details-marker{display:none}
.controls>summary:hover,.controls[open]>summary{color:var(--fg);background:var(--hover)}
.controls>summary svg{width:15px;height:15px}
.controls>div{position:absolute;right:0;top:calc(100% + 8px);z-index:35;display:flex;gap:8px;
align-items:center;white-space:nowrap;background:var(--raise);border:1px solid var(--line2);
border-radius:var(--r2);padding:12px 14px;box-shadow:var(--sh3);animation:fade var(--t2) both}
.controls input{width:64px;height:34px;padding:0 8px;border:1px solid var(--line2);
border-radius:8px;background:var(--hover);color:var(--fg);font-variant-numeric:tabular-nums;
text-align:center}
.controls input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.player{margin:0 0 var(--s3)}
.player.enhanced .localplayer{display:none}
.localplayer{width:100%;display:block;height:40px;border-radius:var(--r2)}
.player iframe{display:block;border-radius:var(--r2);border:0}
.yt{aspect-ratio:16/9;width:100%;border-radius:var(--r2);overflow:hidden;background:#000}
.yt iframe,iframe#yt-player{width:100%;height:100%;aspect-ratio:16/9}
.fallback{margin-top:var(--s2);font-size:13px;display:flex;align-items:center;gap:10px;
flex-wrap:wrap}
.player.enhanced .fallback{display:none}
.noembed{padding:var(--s3) 2px;color:var(--muted)}
.setlink{display:inline-flex;align-items:center;gap:6px;color:var(--muted);height:28px;
border:1px solid var(--line2);border-radius:var(--pill);padding:0 12px;font-size:12px;
font-weight:600;background:transparent;transition:color var(--t1),background-color var(--t1)}
.setlink:hover{text-decoration:none;color:var(--fg);background:var(--hover)}
.tl-wrap{position:relative}
.timeline{position:relative;height:82px;background:var(--sunken);border:1px solid var(--line);
border-radius:var(--r2);overflow:hidden;cursor:pointer;
background-image:repeating-linear-gradient(90deg,transparent 0 calc(10% - 1px),
var(--line) calc(10% - 1px) 10%)}
.tl-gap{position:absolute;top:0;height:100%;pointer-events:none;
background:repeating-linear-gradient(135deg,color-mix(in srgb,var(--gap) 26%,transparent) 0 5px,
transparent 5px 10px)}
.tl-segs{position:absolute;left:0;right:0;top:18px;height:40px}
.tl-seg{position:absolute;top:0;height:100%;margin:0;padding:0 6px 0 9px;border:0;
border-radius:7px;display:flex;align-items:center;min-width:3px;cursor:pointer;color:var(--fg);
font:600 11.5px/1.2 var(--text);text-align:left;overflow:hidden;white-space:nowrap;
--sc:var(--unclear);background:color-mix(in srgb,var(--sc) 26%,var(--sunken));
box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--sc) 50%,transparent);
transition:background-color var(--t1) var(--ease),box-shadow var(--t2) var(--ease)}
.tl-seg::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--sc)}
.tl-seg span{display:block;overflow:hidden;text-overflow:ellipsis;pointer-events:none}
.tl-seg[data-badge=verified]{--sc:var(--verified)}.tl-seg[data-badge=likely]{--sc:var(--likely)}
.tl-seg[data-badge=possible]{--sc:var(--possible)}.tl-seg[data-badge=unclear]{--sc:var(--unclear)}
.tl-seg[data-crowd="1"]{--sc:var(--accent);background:transparent;
box-shadow:none;border:1.5px dashed color-mix(in srgb,var(--accent) 75%,transparent)}
.tl-seg[data-crowd="1"]::before{display:none}
.tl-seg:hover,.tl-seg.hover,.tl-seg:focus-visible{z-index:3;
background:color-mix(in srgb,var(--sc) 42%,var(--sunken))}
.tl-seg:focus-visible{outline:2px solid var(--fg);outline-offset:1px}
.tl-seg.current{z-index:2;background:color-mix(in srgb,var(--sc) 52%,var(--sunken));
box-shadow:inset 0 0 0 1.5px var(--sc),0 0 22px -6px var(--sc)}
.tl-lane{position:absolute;top:0;height:100%;left:0;width:100%;pointer-events:none;
--lc:var(--unclear)}
.tl-lane[data-badge="verified"]{--lc:var(--verified)}
.tl-lane[data-badge="likely"]{--lc:var(--likely)}
.tl-lane[data-badge="possible"]{--lc:var(--possible)}
.tl-lane[data-badge="unclear"]{--lc:var(--unclear)}
.tl-extent{display:none}
.tl-solid,.tl-pi,.tl-unresolved{position:absolute;top:auto;bottom:9px;height:6px;border-radius:3px}
.tl-solid{background:var(--lc);opacity:.85}
.tl-pi{background:var(--pi);opacity:.45}
.tl-unresolved{opacity:.3;background:repeating-linear-gradient(45deg,var(--unresolved),
var(--unresolved) 2px,transparent 2px,transparent 5px)}
.tl-lane.current .tl-solid{opacity:1;box-shadow:0 0 10px var(--lc)}
.tl-lane.hover .tl-solid{opacity:1;filter:brightness(1.25)}
.tl-lane[data-crowd="1"] .tl-solid{background:transparent;border:1px dashed var(--lc);opacity:.8}
.tl-comment{position:absolute;top:3px;transform:translateX(1px);z-index:3;white-space:nowrap;
font:700 8.5px/1 var(--display);letter-spacing:.06em;text-transform:uppercase;
color:var(--accent);border:1px solid var(--accent-line);border-radius:4px;padding:2px 4px;
background:var(--sunken)}
.tl-hover{position:absolute;top:0;bottom:0;left:0;width:1px;background:var(--line3);z-index:4;
pointer-events:none;opacity:0;transition:opacity var(--t1)}
.timeline:hover .tl-hover{opacity:1}
.playhead{position:absolute;top:0;left:0;height:100%;width:2px;margin-left:-1px;
background:var(--fg);z-index:5;pointer-events:none;
box-shadow:0 0 0 1px #00000040,0 0 14px var(--pink)}
.playhead[hidden]{display:none}
.playhead-time{position:absolute;top:2px;left:5px;font:700 10px/1.5 var(--mono);
font-variant-numeric:tabular-nums;background:var(--pink);color:var(--on-accent);
border-radius:4px;padding:0 5px;white-space:nowrap}
.tl-tip{position:absolute;left:0;bottom:calc(100% + 10px);z-index:45;transform:translateX(-50%);
min-width:180px;max-width:min(380px,92vw);background:var(--raise);color:var(--fg);
border:1px solid var(--line2);border-radius:var(--r2);padding:10px 12px;box-shadow:var(--sh3);
font-size:12.5px;line-height:1.4;pointer-events:none;opacity:0;
transition:opacity var(--t1) var(--ease)}
.tl-tip.show{opacity:1}
.tl-tip b{display:block;font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tl-tip span{display:block;color:var(--muted);overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}
.tl-tip em{display:flex;gap:8px;align-items:center;margin-top:6px;font-style:normal;
font:600 11px/1 var(--mono);color:var(--dim);font-variant-numeric:tabular-nums}
.tl-tip em i{font:700 9.5px/1 var(--display);letter-spacing:.08em;text-transform:uppercase;
font-style:normal;color:var(--bc);padding:3px 6px;border-radius:5px;
border:1px solid color-mix(in srgb,var(--bc) 40%,transparent)}
.ruler{position:relative;height:20px;margin:6px 2px 0;font:10.5px/1 var(--mono);color:var(--dim);
font-variant-numeric:tabular-nums}
.ruler span{position:absolute;top:4px;transform:translateX(-50%)}
.ruler span:first-child{transform:none}
.ruler span::before{content:"";position:absolute;left:50%;top:-5px;width:1px;height:3px;
background:var(--line3)}
.legend{display:flex;gap:6px 14px;flex-wrap:wrap;align-items:center;color:var(--muted);
font-size:11.5px;margin-top:6px;padding-top:10px;border-top:1px solid var(--line)}
.legend>span{display:inline-flex;align-items:center;gap:6px}
.legend>span::before{content:"";display:inline-block;width:14px;height:8px;border-radius:2px}
.lg-solid::before{background:linear-gradient(90deg,var(--likely),var(--possible),var(--unclear))}
.lg-gap::before{background:repeating-linear-gradient(135deg,var(--gap) 0 2px,transparent 2px 5px)}
.legend>.key::before{background:var(--kc);width:8px;height:8px;border-radius:50%}
.key.k-verified{--kc:var(--verified)}.key.k-likely{--kc:var(--likely)}
.key.k-possible{--kc:var(--possible)}.key.k-unclear{--kc:var(--unclear)}
.key.k-crowd::before{background:transparent;border:1.5px dashed var(--accent)}
.legend .setlink{margin-left:auto}
/* tracklist header */
.list-head{display:flex;align-items:center;gap:var(--s3) var(--s5);margin:var(--s8) 0 var(--s3);
flex-wrap:wrap}
.list-head h2{font:760 26px/1 var(--display);letter-spacing:-.03em;margin:0}
.list-head .count{font:600 13px/1 var(--mono);color:var(--dim);margin-left:-8px}
.hint-k{display:inline-flex;align-items:center;gap:6px;color:var(--dim);font-size:12px}
.hint-k kbd+kbd{margin-left:-2px}
.hint-k .dot{width:3px;height:3px;border-radius:50%;background:var(--line3);margin:0 4px}
.exports{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-left:auto}
.exports .lbl{font:700 10.5px/1 var(--display);color:var(--dim);text-transform:uppercase;
letter-spacing:.14em;margin-right:4px}
.xbtn{display:inline-flex;align-items:center;gap:6px;height:32px;font:600 12.5px/1 var(--text);
color:var(--muted);border:1px solid var(--line);border-radius:9px;padding:0 11px;
background:transparent;cursor:pointer;text-decoration:none;
transition:color var(--t1) var(--ease),border-color var(--t1) var(--ease),
background-color var(--t1) var(--ease)}
.xbtn:hover{color:var(--fg);border-color:var(--line2);background:var(--hover);
text-decoration:none}
.xbtn.copy{color:var(--fg);border-color:var(--line2);background:var(--hover)}
.xbtn svg{width:14px;height:14px}
.list-info{display:flex;align-items:center;gap:var(--s3) var(--s6);flex-wrap:wrap;
margin:0 0 var(--s4);color:var(--muted);font-size:12.5px}
.glossary{color:var(--muted);font-size:12.5px;margin:0;flex:1 1 360px;max-width:72ch}
.glossary b{color:var(--fg);font-weight:600}
.confidence-summary{display:flex;gap:10px;align-items:center;color:var(--muted);font-size:12px;
margin:0;flex:0 1 auto}
.confidence-summary b{color:var(--fg);font-weight:600;white-space:nowrap}
.conf-bar{display:flex;height:8px;width:180px;border-radius:5px;overflow:hidden;
background:var(--track);gap:2px}
.conf-bar i{display:block;height:100%;min-width:0}
.c-verified{background:var(--verified)}.c-likely{background:var(--likely)}
.c-possible{background:var(--possible)}.c-unclear{background:var(--unclear)}
/* tracklist */
.tablewrap{border:1px solid var(--line);border-radius:var(--r3);background:var(--card);
box-shadow:var(--sh2);overflow:hidden}
table{width:100%;border-collapse:separate;border-spacing:0}
caption{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);
white-space:nowrap}
th{font:700 10.5px/1 var(--display);letter-spacing:.14em;text-transform:uppercase;
color:var(--dim);white-space:nowrap;padding:14px 14px 12px;text-align:left;
border-bottom:1px solid var(--line);background:var(--hover)}
td{padding:12px 14px;border-bottom:1px solid var(--line);vertical-align:middle}
tbody tr:last-child td{border-bottom:none}
tr.track{cursor:pointer;scroll-margin:240px 0 120px}
tr.track td{transition:background-color var(--t1) var(--ease)}
tr.track:hover td,tr.track:focus-within td{background:var(--hover)}
tr.track.current td{background:color-mix(in srgb,var(--pink) 8%,transparent)}
tr.track.current td:first-child{box-shadow:inset 3px 0 0 var(--pink)}
.time{width:1%;white-space:nowrap}
.seek{display:inline-flex;align-items:center;gap:8px;border:0;background:transparent;
color:var(--muted);padding:7px 9px;margin:-7px -9px;cursor:pointer;border-radius:8px;
font:600 13px/1 var(--mono);font-variant-numeric:tabular-nums;
transition:color var(--t1) var(--ease),background-color var(--t1) var(--ease)}
.seek:hover{color:var(--fg);background:var(--hover2)}
.seek .pi{width:10px;height:10px;display:none;color:var(--fg)}
tr.track:hover .seek .pi,.seek:focus-visible .pi{display:block}
tr.track.current .seek{color:var(--pink)}
tr.track.current .seek .pi{display:none}
.time .eqi{display:none}
tr.track.current .time .eqi{display:inline-flex}
.eqi{align-items:flex-end;gap:1.5px;height:10px;width:10px}
.eqi i{display:block;width:2px;height:3px;background:var(--pink);border-radius:1px;
animation:eq .8s ease-in-out infinite}
.eqi i:nth-child(2){animation-delay:.2s}.eqi i:nth-child(3){animation-delay:.4s}
.badge{width:1%;white-space:nowrap}
.pill{display:inline-flex;align-items:center;gap:6px;height:22px;
font:700 10px/1 var(--display);letter-spacing:.09em;text-transform:uppercase;padding:0 8px;
border-radius:6px;color:var(--bc);background:color-mix(in srgb,var(--bc) 12%,transparent);
border:1px solid color-mix(in srgb,var(--bc) 30%,transparent)}
.pill::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--bc)}
.badge-verified{--bc:var(--verified)}.badge-likely{--bc:var(--likely)}
.badge-possible{--bc:var(--possible)}.badge-unclear{--bc:var(--unclear)}
.badge-gap{--bc:var(--gap)}.badge-comments{--bc:var(--accent)}
.label{min-width:0}
.trk{display:flex;flex-wrap:wrap;align-items:center;column-gap:8px;row-gap:3px;min-width:0}
.tt{order:1;flex-basis:100%;font-weight:620;color:var(--fg);font-size:14.5px;letter-spacing:-.005em}
.ar{order:2;color:var(--muted);font-size:13px}
.sep{display:none}
.tags{order:3;display:inline-flex;gap:6px;flex-wrap:wrap}
.alts{order:4;flex-basis:100%}
tr.track.current .tt{color:var(--fg)}
.tag{display:inline-flex;align-items:center;height:19px;font:600 10.5px/1 var(--text);
color:var(--dim);border:1px solid var(--line2);border-radius:5px;padding:0 6px}
.tag.ver-verified{color:var(--verified);border-color:color-mix(in srgb,var(--verified) 40%,
transparent)}
.tag.ver-contested{color:var(--gap);border-color:color-mix(in srgb,var(--gap) 40%,transparent)}
.hint{display:inline-flex;align-items:center;height:19px;font:700 9.5px/1 var(--display);
border-radius:5px;padding:0 6px;letter-spacing:.06em;text-transform:uppercase}
.hint.crowd{background:none;border:1px dashed var(--accent);color:var(--accent)}
.hint.engine{background:none;border:1px solid color-mix(in srgb,var(--verified) 55%,transparent);
color:var(--verified)}
.hint.comment{background:none;border:1px solid var(--accent-line);color:var(--accent)}
.alts{margin-top:3px;font-size:12px}
.alts>summary{cursor:pointer;color:var(--muted);list-style:none;display:inline-flex;
align-items:center;gap:5px;height:24px;border:1px solid var(--line);border-radius:6px;
padding:0 8px;font-size:11.5px;font-weight:600;transition:color var(--t1),border-color var(--t1)}
.alts>summary::-webkit-details-marker{display:none}
.alts>summary:hover,.alts[open]>summary{color:var(--fg);border-color:var(--line2)}
.altlist{margin:8px 0 2px;padding:0;list-style:none;color:var(--muted)}
.altlist li{margin:5px 0;display:flex;align-items:center;gap:8px}
.alt-badge{font:700 9px/1 var(--display);letter-spacing:.1em;padding:3px 5px;border-radius:4px;
color:var(--bc);border:1px solid color-mix(in srgb,var(--bc) 35%,transparent)}
.acquire{white-space:nowrap;width:1%}
.acq{display:inline-flex;align-items:center;height:26px;margin:2px 4px 2px 0;
font:600 11.5px/1 var(--text);border:1px solid var(--line2);border-radius:7px;padding:0 9px;
color:var(--fg);background:transparent;transition:background-color var(--t1),border-color var(--t1)}
.acq:hover{text-decoration:none;background:var(--hover2);border-color:var(--line3)}
.acq.free{border-color:color-mix(in srgb,var(--verified) 50%,transparent);color:var(--verified)}
.acq.buy,.acq.gate{border-color:color-mix(in srgb,var(--possible) 50%,transparent);
color:var(--possible)}
.acq.none{border:none;color:var(--dim);background:none;padding-left:0}
.ops{width:1%;white-space:nowrap;text-align:right}
.ops .pl-actions{gap:4px}
.ops .pl-like,.ops .pl-add{display:inline-grid;place-items:center;width:30px;height:30px;
padding:0;border-radius:8px;border:1px solid var(--line2);background:transparent;
color:var(--muted);font-size:13px;cursor:pointer;
transition:color var(--t1),background-color var(--t1),border-color var(--t1)}
.ops .pl-like:hover,.ops .pl-add:hover{color:var(--fg);background:var(--hover2);
border-color:var(--accent-line)}
.ops .pl-like.on{color:var(--pink);border-color:color-mix(in srgb,var(--pink) 45%,transparent)}
@media (hover:hover) and (pointer:fine){tr.track .acq:not(.none){color:var(--muted);
border-color:var(--line)}
tr.track:hover .acq.free,tr.track:focus-within .acq.free{color:var(--verified);
border-color:color-mix(in srgb,var(--verified) 50%,transparent)}
tr.track:hover .acq.buy,tr.track:focus-within .acq.buy,tr.track:hover .acq.gate,
tr.track:focus-within .acq.gate{color:var(--possible);
border-color:color-mix(in srgb,var(--possible) 50%,transparent)}
tr.track:hover .acq:not(.free):not(.buy):not(.gate):not(.none),
tr.track:focus-within .acq:not(.free):not(.buy):not(.gate):not(.none){color:var(--fg);
border-color:var(--line2)}
tr.track .ops .pl-add,tr.track .ops .pl-like:not(.on){border-color:var(--line)}
tr.track:hover .ops .pl-add,tr.track:focus-within .ops .pl-add,tr.track:hover .ops .pl-like,
tr.track:focus-within .ops .pl-like{border-color:var(--line2);color:var(--fg)}}
tr.gap td{background:repeating-linear-gradient(135deg,color-mix(in srgb,var(--gap) 6%,
transparent) 0 8px,transparent 8px 16px)}
tr.gap .time{font:600 13px/1 var(--mono);color:var(--dim);font-variant-numeric:tabular-nums}
tr.gap .label{color:var(--muted);font-size:13px}
tr.gap .tt{color:var(--gap);font-weight:600;margin-right:6px}
tr.track.crowd td:first-child{box-shadow:inset 3px 0 0 transparent;
border-left:3px dashed color-mix(in srgb,var(--accent) 60%,transparent)}
.lg-crowd::before{background:transparent;border:1.5px dashed var(--accent);height:6px}
/* the NOW pill in the top bar (phones: the deck is not sticky there) */
.now{flex:1;min-width:0;display:flex;align-items:center;gap:10px;justify-content:center;
font-size:13px;cursor:pointer;border:0;background:none;color:var(--fg);padding:0}
.now[hidden]{display:none}
.now-k{font:700 10px/1 var(--display);letter-spacing:.14em;color:var(--pink);padding:5px 7px;
border:1px solid color-mix(in srgb,var(--pink) 50%,transparent);border-radius:6px;
background:color-mix(in srgb,var(--pink) 9%,transparent)}
.now-t{font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--muted)}
.now-l{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
@media (min-width:900px) and (min-height:720px){.now{visibility:hidden}}
@media (max-width:900px){.hint-k{display:none}}
@media (max-width:440px){.now-l{display:none}}
@media (max-width:720px){.hero{padding-top:var(--s5)}.stat .big{font-size:30px}
/* One tile per row on a phone, stated rather than left to auto-fit arithmetic: no rounding, zoom
   level or font-size setting can produce a second column that runs off the screen. */
.stats{grid-template-columns:1fr}.stat{min-height:64px;padding:var(--s3) var(--s5)}
.stat+.stat{border-left:0;border-top:1px solid var(--line)}
h1{font-size:30px;margin-bottom:var(--s5)}
.deck{padding:var(--s3) var(--s3) var(--s2);border-radius:var(--r3)}
.transport{gap:var(--s2);flex-wrap:wrap}.tp-now{order:5;flex-basis:100%;border-left:0;
margin-left:0;padding-left:0}.tp-time{margin-left:auto}
.timeline{height:70px}.tl-segs{top:14px;height:34px}.tl-seg span{display:none}
.ruler span:nth-child(even){display:none}
.legend .setlink{margin-left:0}
.list-head{margin-top:var(--s7)}.list-head h2{font-size:22px}
.exports .lbl{display:none}.exports{margin-left:0;width:100%}
.conf-bar{width:140px}
.tablewrap{overflow:visible;border:0;background:transparent;box-shadow:none}
table{display:block}thead{position:absolute;
width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;
border:0}tbody{display:grid;gap:10px}tr.track,tr.gap{display:grid;
grid-template-columns:auto 1fr auto;
gap:8px 12px;background:var(--card);border:1px solid var(--line);border-radius:var(--r3);
padding:12px 14px;box-shadow:var(--sh1)}
tr.track.current{border-color:color-mix(in srgb,var(--pink) 45%,transparent)}
tr.track.current td{background:none}tr.track.current td:first-child{box-shadow:none}
td{display:block;padding:0;border:0}tr.track:hover td{background:none}
.time{grid-column:1}.badge{grid-column:2;align-self:center}.ops{grid-column:3;
grid-row:1}.label{grid-column:1/-1;grid-row:2}.acquire{grid-column:1/-1;grid-row:3;
white-space:normal}
.seek{margin:0}
.acq{white-space:nowrap}
.acquire:has(>.acq.none:only-child){display:none}
/* Every control a thumb has to hit is at least 44x44 CSS px (WCAG 2.5.5 / U-F19-F20): the row's
   seek button, each acquisition chip, the playlist buttons the playlists feature injects, every
   export control, and each disclosure summary. ``inline-flex`` is what makes min-height apply to
   the inline elements (links, summaries) at all. */
.acq,.seek,.ops button,.ops a,.xbtn,.alts>summary,.controls>summary,.now,#leadin,.tp-btn{
min-height:44px;min-width:44px;display:inline-flex;align-items:center;justify-content:center}
.ops .pl-like,.ops .pl-add{width:44px;height:44px}
.alts>summary,.controls>summary{justify-content:flex-start}
.exports{gap:8px;flex-wrap:wrap}.xbtn{padding:0 14px}
.now{font-size:12px}.now-l{max-width:30vw}.confidence-summary{flex-wrap:wrap}
.confidence-summary span{display:block}}
.tl-hover::after{content:attr(data-time);position:absolute;bottom:3px;left:5px;
font:600 9.5px/1 var(--mono);color:var(--muted);font-variant-numeric:tabular-nums;
white-space:nowrap}
"""
    + LIGHT_CSS
)


# The page's own JavaScript: player bindings, playhead/current-row highlight, timeline and real seek
# buttons; on top of those sit the NOW pill, copy-tracklist and row↔lane hover.
_PAGE_JS = """
let CURRENT_POSITION_MS = null, PLAYER_DURATION_MS = 0;
// A row the user just clicked stays highlighted through its lead-in (the player is still in the
// previous track showing the mix-in) until the playhead actually reaches the clicked track's start.
let PINNED_ID = null, PINNED_UNTIL_MS = 0;
let localAudio = null;  // the fetched-original <audio>, when the page plays locally (the default)
let scWidget = null, ytPlayer = null, mcWidget = null, ytTimer = null, playTimer = null;
let LAST_CURRENT_ID = null, USER_SCROLLED_AT = 0;
const ROW_LABELS = {};
const REDUCED_MOTION = !!(window.matchMedia &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches);
function ready(fn){ if(document.readyState!=='loading'){fn();}
  else{document.addEventListener('DOMContentLoaded',fn);} }
function toast(msg){ const t=document.getElementById('toast'); if(!t) return;
  t.textContent=msg; t.classList.add('show'); clearTimeout(t._h);
  t._h=setTimeout(()=>t.classList.remove('show'),2200); }
// Playhead readout -----------------------------------------------------------
function playheadDuration(){
  return (CONFIG.durationMs > 0) ? CONFIG.durationMs : PLAYER_DURATION_MS;
}
function setPlayingState(on){
  const deck = document.getElementById('deck'); if(!deck) return;
  deck.classList.toggle('is-playing', !!on);
  const play = document.getElementById('tp-play');
  if(play) play.setAttribute('aria-label', on ? 'Pause' : 'Play');
}
function markPlaying(){
  document.body.classList.add('playing'); clearTimeout(playTimer);
  if(!localAudio) setPlayingState(true);
  playTimer = setTimeout(function(){ document.body.classList.remove('playing');
    if(!localAudio) setPlayingState(false); }, 1800);
}
function updatePlayhead(positionMs){
  if(typeof positionMs !== 'number' || isNaN(positionMs)) return;
  CURRENT_POSITION_MS = positionMs;
  const timeline = document.querySelector('.timeline');
  const head = document.getElementById('playhead');
  if(!timeline || !head) return;
  const x = playheadX(positionMs, playheadDuration(), timeline.clientWidth);
  head.style.left = x + 'px';
  head.hidden = false;
  const label = document.getElementById('playhead-time');
  if(label) label.textContent = formatTime(positionMs);
  const pos = document.getElementById('tp-pos');
  if(pos) pos.textContent = formatTime(positionMs);
  markPlaying();
  highlightCurrent(positionMs);
}
function highlightCurrent(positionMs){
  let currentId = null;
  for(let i=0;i<EPISODE_SPANS.length;i++){
    const span = EPISODE_SPANS[i];
    if(positionMs >= span.start && positionMs < span.end){ currentId = span.id; break; }
  }
  // Hold the just-clicked row as "current" while the player is still in its lead-in.
  if(PINNED_ID !== null){
    if(positionMs < PINNED_UNTIL_MS){ currentId = PINNED_ID; }
    else { PINNED_ID = null; }
  }
  document.querySelectorAll('tr.track.current, .tl-lane.current, .tl-seg.current').forEach(
    function(el){ el.classList.remove('current'); });
  if(currentId){
    document.querySelectorAll('[data-episode-id="'+currentId+'"]').forEach(function(el){
      if(el.classList.contains('track') || el.classList.contains('tl-lane')){
        el.classList.add('current'); }
    });
    const seg = document.querySelector('.tl-seg[data-seg="'+currentId+'"]');
    if(seg) seg.classList.add('current');
  }
  setNowPlaying(currentId, positionMs);
  if(currentId !== LAST_CURRENT_ID){
    const previous = LAST_CURRENT_ID; LAST_CURRENT_ID = currentId;
    if(currentId && previous !== null) followRow(currentId);
  }
}
// Gently bring the playing row into view -- only when it is off screen and the listener has not
// been scrolling around the list themselves in the last few seconds.
function followRow(id){
  if(Date.now() - USER_SCROLLED_AT < 6000) return;
  const row = document.getElementById(id); if(!row) return;
  const box = row.getBoundingClientRect();
  const deck = document.getElementById('deck');
  const top = (deck && getComputedStyle(deck).position === 'sticky') ?
    deck.getBoundingClientRect().bottom : 64;
  if(box.top >= top && box.bottom <= window.innerHeight) return;
  row.scrollIntoView({block:'center', behavior: REDUCED_MOTION ? 'auto' : 'smooth'});
}
function setNowPlaying(id, positionMs){
  const label = id ? (ROW_LABELS[id] || '') : '';
  const now = document.getElementById('now');
  if(now){
    if(!id){ now.hidden = true; }
    else {
      now.hidden = false;
      document.getElementById('now-time').textContent = formatTime(positionMs);
      document.getElementById('now-label').textContent = label;
    }
  }
  const deckNow = document.getElementById('tp-now');
  if(deckNow){
    deckNow.classList.toggle('idle', !id);
    const text = document.getElementById('tp-label');
    if(text) text.textContent = id ? label : 'Press play, or pick a track below';
  }
}
// Player bindings ------------------------------------------------------------
ready(function(){
  localAudio = document.getElementById('localplayer');
  if(localAudio){
    const player = localAudio.closest('.player');
    if(player) player.classList.add('enhanced');
    localAudio.addEventListener('timeupdate', function(){
      updatePlayhead(localAudio.currentTime * 1000); });
    localAudio.addEventListener('loadedmetadata', function(){
      if(localAudio.duration > 0) PLAYER_DURATION_MS = localAudio.duration * 1000; });
    localAudio.addEventListener('play', function(){ setPlayingState(true); });
    localAudio.addEventListener('pause', function(){ setPlayingState(false); });
    localAudio.addEventListener('ended', function(){ setPlayingState(false); });
    return;  // local audio is the player — the platform-widget bindings below are the fallback
  }
  try{
    if(CONFIG.embedKind==='soundcloud' && window.SC){
      scWidget = SC.Widget(document.getElementById('sc-player'));
      if(scWidget && scWidget.bind){
        scWidget.bind(SC.Widget.Events.READY, function(){
          scWidget.getDuration(function(d){
            if(typeof d === 'number' && d > 0) PLAYER_DURATION_MS = d; });
        });
        scWidget.bind(SC.Widget.Events.PLAY_PROGRESS, function(e){
          if(e && typeof e.currentPosition === 'number') updatePlayhead(e.currentPosition); });
        scWidget.bind(SC.Widget.Events.SEEK, function(e){
          if(e && typeof e.currentPosition === 'number') updatePlayhead(e.currentPosition); });
      }
    } else if(CONFIG.embedKind==='mixcloud' && window.Mixcloud){
      mcWidget = Mixcloud.PlayerWidget(document.getElementById('mc-player'));
      if(mcWidget && mcWidget.ready && mcWidget.events && mcWidget.events.progress){
        mcWidget.ready.then(function(){
          mcWidget.events.progress.on(function(position){
            if(typeof position === 'number') updatePlayhead(position * 1000); });
        });
      }
    }
  } catch(err){ /* embedding disabled or API missing — timeline still works, no playhead */ }
});
function onYouTubeIframeAPIReady(){
  if(CONFIG.embedKind!=='youtube') return;
  ytPlayer = new YT.Player('yt-player', {videoId: CONFIG.identifier,
    playerVars: {playsinline:1},
    events: {
      onReady: function(){
        if(ytPlayer.getDuration){
          const d = ytPlayer.getDuration(); if(d > 0) PLAYER_DURATION_MS = d * 1000; }
      },
      onStateChange: function(e){
        if(e.data === YT.PlayerState.PLAYING){ startYtPolling(); } else { stopYtPolling(); }
      }
    } });
}
function startYtPolling(){
  if(ytTimer) return;
  ytTimer = setInterval(function(){
    if(ytPlayer && ytPlayer.getCurrentTime) updatePlayhead(ytPlayer.getCurrentTime() * 1000);
  }, 250);
}
function stopYtPolling(){ if(ytTimer){ clearInterval(ytTimer); ytTimer = null; } }
function seekPlayerArg(arg){
  if(CONFIG.embedKind==='soundcloud' && scWidget){
    scWidget.seekTo(arg); scWidget.play(); return true; }
  if(CONFIG.embedKind==='youtube' && ytPlayer && ytPlayer.seekTo){
    ytPlayer.seekTo(arg, true); if(ytPlayer.playVideo) ytPlayer.playVideo(); return true; }
  if(CONFIG.embedKind==='mixcloud' && mcWidget){
    mcWidget.ready.then(function(){ mcWidget.seek(arg); mcWidget.play(); }); return true; }
  return false;
}
// Seek with the lead-in (a tracklist row) or exactly to a point (a timeline click); both reuse the
// shared seek arithmetic — a timeline click is a zero-lead-in seek to the clicked position.
function seekToMs(bestStartMs){
  if(localAudio){ localAudio.currentTime = seekTargetMs(bestStartMs, LEAD_IN_MS) / 1000;
    if(localAudio.play){ localAudio.play().catch(function(){}); } return true; }
  return seekPlayerArg(seekArgument(bestStartMs, LEAD_IN_MS)); }
function seekToPositionMs(positionMs){
  if(localAudio){ localAudio.currentTime = Math.max(0, positionMs) / 1000;
    if(localAudio.play){ localAudio.play().catch(function(){}); } return true; }
  return seekPlayerArg(seekArgument(positionMs, 0)); }
// Play/pause for whichever player the page has.
function togglePlay(){
  if(localAudio){
    if(localAudio.paused){ if(localAudio.play) localAudio.play().catch(function(){}); }
    else { localAudio.pause(); }
    return true;
  }
  if(scWidget && scWidget.toggle){ scWidget.toggle(); return true; }
  if(ytPlayer && ytPlayer.getPlayerState){
    if(ytPlayer.getPlayerState() === 1) ytPlayer.pauseVideo(); else ytPlayer.playVideo();
    return true;
  }
  if(mcWidget && mcWidget.togglePlay){ mcWidget.togglePlay(); return true; }
  toast('Player not ready — open the set link'); return false;
}
// Copy the tracklist as plain text (time, artist — title; ID for a gap) ------
function tracklistText(){
  return COPY_ENTRIES.map(function(entry){
    if(entry.kind === 'id'){ return entry.time + '  ID'; }
    return entry.time + '  ' + entry.label + (entry.crowd ? ' (from comments)' : '');
  }).join('\\n');
}
function copyTracklist(){
  const text = tracklistText(); const n = text ? text.split('\\n').length : 0;
  function ok(){ toast('Copied ' + n + ' lines to the clipboard'); }
  function fallback(){
    const ta = document.createElement('textarea'); ta.value = text; ta.style.position='fixed';
    ta.style.opacity='0'; document.body.appendChild(ta); ta.select();
    try{ document.execCommand('copy'); ok(); } catch(e){ toast('Copy failed'); }
    document.body.removeChild(ta);
  }
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(ok, fallback);
  } else { fallback(); }
}
// Tracklist + timeline interactions ------------------------------------------
ready(function(){
  const leadin = document.getElementById('leadin');
  leadin.addEventListener('change', function(){
    const v = parseFloat(leadin.value);
    LEAD_IN_MS = (isNaN(v) || v<0) ? 0 : Math.round(v * 1000);
  });
  const rows = Array.prototype.slice.call(document.querySelectorAll('tr.track'));
  const goTo = {};
  rows.forEach(function(row){
    const id = row.getAttribute('data-episode-id');
    const ar = row.querySelector('.ar'), tt = row.querySelector('.tt');
    ROW_LABELS[id] = (ar ? ar.textContent + ' — ' : '') + (tt ? tt.textContent : '');
    function go(){
      const ms = parseInt(row.getAttribute('data-best-start-ms'), 10) || 0;
      // Pin THIS row so it reads as playing from the click, even during its lead-in mix-in.
      PINNED_ID = id; PINNED_UNTIL_MS = ms; LAST_CURRENT_ID = id;
      highlightCurrent(seekTargetMs(ms, LEAD_IN_MS));
      if(!seekToMs(ms)){ toast('Player not ready — open the set link'); }
    }
    goTo[id] = go;
    const seek = row.querySelector('button.seek');
    if(seek) seek.addEventListener('click', go);
    // Preserve the original whole-row shortcut without hijacking links, playlist buttons,
    // disclosures, or form controls. The real time-cell button remains the keyboard-accessible
    // seek control; this listener is the larger mouse/touch target users expect.
    row.addEventListener('click', function(e){
      if(e.target.closest('a,button,input,select,textarea,summary,details')) return;
      go();
    });
    const lane = document.querySelector('.tl-lane[data-episode-id="'+id+'"]');
    const seg = document.querySelector('.tl-seg[data-seg="'+id+'"]');
    row.addEventListener('mouseenter', function(){
      if(lane) lane.classList.add('hover'); if(seg) seg.classList.add('hover'); });
    row.addEventListener('mouseleave', function(){
      if(lane) lane.classList.remove('hover'); if(seg) seg.classList.remove('hover'); });
  });
  // The timeline: click anywhere to jump exactly there; a track segment chosen from the keyboard
  // plays that track from its start (with the lead-in), like its row.
  const timeline = document.querySelector('.timeline');
  const tip = document.getElementById('tl-tip');
  const hover = document.getElementById('tl-hover');
  const segs = Array.prototype.slice.call(document.querySelectorAll('.tl-seg'));
  function fraction(e){
    const rect = timeline.getBoundingClientRect();
    if(rect.width <= 0) return null;
    return Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  }
  function showTip(seg, x){
    if(!tip || !seg) return;
    const wrap = timeline.parentNode.getBoundingClientRect();
    tip.innerHTML = '';
    const title = document.createElement('b'); title.textContent = seg.getAttribute('data-title');
    const artist = document.createElement('span');
    artist.textContent = seg.getAttribute('data-artist');
    const meta = document.createElement('em');
    const badge = document.createElement('i');
    badge.className = 'badge-' + seg.getAttribute('data-badge');
    badge.textContent = seg.getAttribute('data-word');
    meta.appendChild(badge);
    meta.appendChild(document.createTextNode(seg.getAttribute('data-span')));
    tip.appendChild(title); tip.appendChild(artist); tip.appendChild(meta);
    const half = Math.min(190, tip.offsetWidth / 2 || 110);
    tip.style.left = Math.max(half, Math.min(wrap.width - half, x)) + 'px';
    tip.classList.add('show');
  }
  function hideTip(){ if(tip) tip.classList.remove('show'); }
  if(timeline){
    timeline.addEventListener('click', function(e){
      const frac = fraction(e); if(frac === null) return;
      const ms = Math.round(frac * playheadDuration());
      PINNED_ID = null;
      updatePlayhead(ms);
      if(!seekToPositionMs(ms)){ toast('Player not ready — open the set link'); }
    });
    timeline.addEventListener('mousemove', function(e){
      const frac = fraction(e); if(frac === null) return;
      const rect = timeline.getBoundingClientRect();
      if(hover){ hover.style.transform = 'translateX(' + (frac * rect.width) + 'px)';
        hover.setAttribute('data-time', formatTime(frac * playheadDuration())); }
      const seg = e.target.closest ? e.target.closest('.tl-seg') : null;
      if(seg) showTip(seg, e.clientX - timeline.parentNode.getBoundingClientRect().left);
      else hideTip();
    });
    timeline.addEventListener('mouseleave', hideTip);
  }
  segs.forEach(function(seg, index){
    seg.addEventListener('click', function(e){
      if(e.detail !== 0) return;  // a pointer click falls through to the exact-position seek
      e.stopPropagation();
      const id = seg.getAttribute('data-seg');
      if(goTo[id]) goTo[id]();
    });
    seg.addEventListener('focus', function(){
      const rect = seg.getBoundingClientRect();
      const wrap = timeline.parentNode.getBoundingClientRect();
      showTip(seg, rect.left - wrap.left + rect.width / 2);
    });
    seg.addEventListener('blur', hideTip);
    // One tab stop for the whole strip; arrows walk the tracks (roving tabindex).
    seg.addEventListener('keydown', function(e){
      let next = null;
      if(e.key === 'ArrowRight' || e.key === 'ArrowDown') next = segs[index + 1];
      else if(e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = segs[index - 1];
      else if(e.key === 'Home') next = segs[0];
      else if(e.key === 'End') next = segs[segs.length - 1];
      if(!next) return;
      e.preventDefault(); e.stopPropagation();
      segs.forEach(function(s){ s.tabIndex = -1; });
      next.tabIndex = 0; next.focus();
    });
  });
  window.addEventListener('resize', function(){
    if(CURRENT_POSITION_MS !== null) updatePlayhead(CURRENT_POSITION_MS); });
  // Transport ---------------------------------------------------------------
  function step(delta){
    const ids = EPISODE_SPANS.map(function(s){ return s.id; });
    if(!ids.length) return;
    const at = ids.indexOf(LAST_CURRENT_ID);
    const target = at < 0 ? (delta > 0 ? 0 : ids.length - 1)
      : Math.max(0, Math.min(ids.length - 1, at + delta));
    if(goTo[ids[target]]) goTo[ids[target]]();
  }
  const play = document.getElementById('tp-play');
  if(play) play.addEventListener('click', togglePlay);
  const prev = document.getElementById('tp-prev');
  if(prev) prev.addEventListener('click', function(){ step(-1); });
  const next = document.getElementById('tp-next');
  if(next) next.addEventListener('click', function(){ step(1); });
  // Keyboard: Space plays/pauses, J/K (or arrows on a row) move between tracks, Enter plays the
  // focused one.  Never while typing, and never instead of a control's own key.
  function typing(el){
    if(!el) return false;
    const tag = el.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
  }
  function moveFocus(delta){
    const buttons = Array.prototype.slice.call(document.querySelectorAll('tr.track button.seek'));
    if(!buttons.length) return;
    let at = buttons.indexOf(document.activeElement);
    if(at < 0){
      const current = document.querySelector('tr.track.current button.seek');
      at = current ? buttons.indexOf(current) : (delta > 0 ? -1 : buttons.length);
    }
    const target = buttons[Math.max(0, Math.min(buttons.length - 1, at + delta))];
    target.focus({preventScroll:true});
    target.scrollIntoView({block:'nearest', behavior: REDUCED_MOTION ? 'auto' : 'smooth'});
  }
  document.addEventListener('keydown', function(e){
    if(e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || typing(e.target)) return;
    const onSeek = e.target.classList && e.target.classList.contains('seek');
    if(e.key === ' ' || e.code === 'Space'){
      const control = e.target.closest && e.target.closest('button,a,summary');
      if(control && !onSeek && !control.classList.contains('tl-seg')) return;
      e.preventDefault(); togglePlay(); return;
    }
    if(e.key === 'j' || e.key === 'J' || (onSeek && e.key === 'ArrowDown')){
      e.preventDefault(); moveFocus(1); return; }
    if(e.key === 'k' || e.key === 'K' || (onSeek && e.key === 'ArrowUp')){
      e.preventDefault(); moveFocus(-1); return; }
  });
  ['wheel', 'touchmove'].forEach(function(kind){
    window.addEventListener(kind, function(){ USER_SCROLLED_AT = Date.now(); }, {passive:true});
  });
  document.addEventListener('keydown', function(e){
    if(['PageUp', 'PageDown', 'Home', 'End'].indexOf(e.key) >= 0) USER_SCROLLED_AT = Date.now();
  });
  const copy = document.getElementById('copy');
  if(copy) copy.addEventListener('click', copyTracklist);
  const now = document.getElementById('now');
  if(now) now.addEventListener('click', function(){
    const row = document.querySelector('tr.track.current');
    if(row){ row.scrollIntoView({block:'center', behavior: REDUCED_MOTION ? 'auto' : 'smooth'}); }
  });
  setNowPlaying(null, 0);
});
"""


#: Transport and toolbar glyphs (decorative: every button that shows one is labelled).
_ICON_PLAY = (
    '<svg class="i-play" viewBox="0 0 16 16" aria-hidden="true">'
    '<path d="M4.5 2.6v10.8L13 8z" fill="currentColor"/></svg>'
)
_ICON_PAUSE = (
    '<svg class="i-pause" viewBox="0 0 16 16" aria-hidden="true"><rect x="3.5" y="2.5" width="3" '
    'height="11" rx="1" fill="currentColor"/><rect x="9.5" y="2.5" width="3" height="11" rx="1" '
    'fill="currentColor"/></svg>'
)
_ICON_PREV = (
    '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2.5" y="3" width="2" height="10" '
    'rx="1" fill="currentColor"/><path d="M13.5 3.2v9.6L5.8 8z" fill="currentColor"/></svg>'
)
_ICON_NEXT = (
    '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="11.5" y="3" width="2" height="10" '
    'rx="1" fill="currentColor"/><path d="M2.5 3.2v9.6L10.2 8z" fill="currentColor"/></svg>'
)
_ICON_GEAR = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" aria-hidden="true"><path d="M4 7h10M18 7h2M4 17h4M12 17h8"/>'
    '<circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/></svg>'
)
_ICON_COPY = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linejoin="round" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2.5"/>'
    '<path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></svg>'
)


def _set_link_html(embed: EmbedPlan) -> str:
    """A quiet link to the original set, for the deck's legend row."""

    href = _safe_href(embed.link_url)
    if not href:
        return ""
    return f'<a class="setlink" target="_blank" rel="noopener" href="{href}">Open the set ↗</a>'


def render_page(
    *,
    source: SourceRecord,
    episodes: EpisodesFile,
    identities: IdentitiesRecord,
    duration_ms: int,
    acquire: AcquireFile | None = None,
    lead_in_ms: int = DEFAULT_LEAD_IN_MS,
    collapse: bool = True,
    same_track_bridge_ms: int | None = None,
    min_track_ms: int = 0,
    projection: CanonicalProjection | None = None,
    status: str = "complete",
    reason: str | None = None,
    achieved: str | None = None,
    analysed_at: str | None = None,
) -> str:
    """Render the complete self-contained HTML page as a string.

    With ``collapse`` (the default), a contiguous run of competing near-duplicate matches of the
    same underlying track collapses to one display-track row (with a "▸ N other versions"
    disclosure); two appearances of the SAME exact track up to ``same_track_bridge_ms`` apart
    (``None`` → the grouping default) with no different confident track between them likewise stack
    into one row.  The timeline lane, the current-row highlight and the seek all use that display
    track's primary.  ``collapse=False`` restores the one-lane-per-episode view.

    ``min_track_ms`` (default ``0`` = off) removes track rows that played too briefly to be a real
    track (see :func:`~id_detector.present.exports.short_track`) from every user-facing surface;
    fusion-suppressed rows are removed the same way.  Their model data stays in the frozen run.
    """

    embed = plan_embed(source)
    # A stable local route survives bundle nesting and cached-open without the source file.
    original_path = getattr(getattr(source, "original", None), "path", None)
    audio_src = f"/media/{source.media_key}/audio" if original_path else None
    projection = projection or build_projection(
        episodes,
        identities,
        acquire,
        collapse=collapse,
        same_track_bridge_ms=same_track_bridge_ms,
        min_track_ms=min_track_ms,
    )
    entries = projection.shown_entries
    boundaries = _evidence_boundaries(list(episodes.episodes))

    # A display track is one collapsed row (primary + folded-in alternatives); ungrouped, it is one
    # episode.  Lanes, the highlight partition and the row all key off the primary's id so the
    # Stage 11 playhead lights the same row + lane as the tracklist.  They are read straight off
    # the canonical projection — re-grouping here is exactly how the page used to end up showing a
    # different set of tracks from its own exports.
    episode_by_id = {episode.id: episode for episode in episodes.episodes}
    track_entries = [entry for entry in entries if entry["kind"] == "track"]
    lane_episodes = [
        episode_by_id[entry["episode_id"]]
        for entry in track_entries
        if entry["episode_id"] in episode_by_id
    ]
    span_items = [
        (str(entry["episode_id"]), int(entry["start_ms"]), int(entry["end_ms"]))
        for entry in track_entries
    ]

    label_by_episode = {
        str(entry["episode_id"]): str(entry["display_label"])
        for entry in track_entries
        if entry["episode_id"] is not None
    }
    lanes = [
        _timeline_lane(
            episode,
            label_by_episode[episode.id],
            boundaries,
            duration_ms,
        )
        for episode in lane_episodes
    ]
    entry_by_episode = {str(entry["episode_id"]): entry for entry in track_entries}
    for lane in lanes:
        entry = entry_by_episode[lane["episode_id"]]
        lane["crowd"] = bool(entry.get("hint_only"))
        lane["comment_confirmed"] = bool(entry.get("hint_supported") and not entry.get("hint_only"))
        lane["comment_left"] = (
            lane["solids"][0]["left"] if lane["solids"] else lane["extent"]["left"]
        )
    shown_gap_ids = {
        entry.get("gap_id") for entry in projection.shown_entries if entry["kind"] == "id"
    }
    gap_markers = [
        _gap_marker(gap, duration_ms) for gap in episodes.gaps if gap.id in shown_gap_ids
    ]

    # Playhead → current-track partition: each display track owns time from its start up to the
    # next display track's start (the plan's ``[start, next start)`` interval); the last one owns
    # the tail of the set.  The page uses these spans to add ``.current`` to the row and timeline
    # lane whose interval contains the live player position.
    ordered = sorted(span_items, key=lambda item: (item[1], item[0]))
    episode_spans: list[dict[str, Any]] = []
    for index, (track_id, start, end_fallback) in enumerate(ordered):
        following = [other[1] for other in ordered[index + 1 :] if other[1] > start]
        end = following[0] if following else max(end_fallback, duration_ms)
        if end <= start:
            end = max(end_fallback, start + 1)
        episode_spans.append({"id": track_id, "start": start, "end": end})

    show_acquire = any(entry["kind"] == "track" and bool(entry.get("acquire")) for entry in entries)
    rows: list[str] = []
    for index, entry in enumerate(entries):
        if entry["kind"] == "track":
            rows.append(_track_row_html(entry, index=index, show_acquire=show_acquire))
        else:
            rows.append(_gap_row_html(entry, show_acquire=show_acquire))

    title = source.title or "DJ set"
    platform_name = PLATFORM_NAMES.get(source.platform, source.platform)
    config_js = json.dumps(
        {
            "platform": embed.kind if embed.kind != "link" else source.platform,
            "embedKind": embed.kind,
            "identifier": embed.identifier,
            "mediaKey": source.media_key,
            "durationMs": duration_ms,
            "leadInMs": lead_in_ms,
        }
    ).replace("</", "<\\/")
    episode_spans_js = json.dumps(episode_spans).replace("</", "<\\/")
    copy_entries_js = json.dumps(
        [
            {
                "kind": entry["kind"],
                "time": _format_time(entry["start_ms"]),
                "label": entry["display_label"],
                "crowd": bool(entry.get("hint_only")),
            }
            for entry in projection.shown_entries
        ]
    ).replace("</", "<\\/")
    lead_in_s = f"{lead_in_ms / 1000:g}"
    now_pill = (
        '<button type="button" class="now" id="now" hidden title="Jump to the current track">'
        '<span class="now-k">NOW</span><span class="now-t" id="now-time"></span>'
        '<span class="now-l" id="now-label"></span></button>'
    )
    # Rescans are off by default, so generation is almost always 0 — a meaningless chip. Show it
    # only when someone deliberately ran rescans (generation > 0).
    gen_chip = (
        f'<span class="chip">generation <b>{episodes.generation}</b></span>'
        if episodes.generation
        else ""
    )
    scan_name = (
        "Deep scan"
        if (achieved == "deep" or episodes.certification.profile == "max_accuracy")
        else "Free scan"
    )
    analysed = ""
    if analysed_at:
        try:
            moment = datetime.fromisoformat(analysed_at.replace("Z", "+00:00"))
            analysed = f'<span class="chip">analysed <b>{moment.day} {moment:%b %Y}</b></span>'
        except (ValueError, TypeError):
            analysed = ""
    status_banner = _status_banner_html(status, reason, achieved)

    track_count = len(track_entries)
    segments = _segments(track_entries, duration_ms)
    body = f"""{topbar_html(back=True, new=True, middle=now_pill)}
<main>
<header class="hero">
<div class="meta">
  <span class="chip plat-{_esc(source.platform)}"><span
class="pd"></span>{_esc(platform_name)}</span>
  <span class="chip" id="dur">length <b>{_esc(_format_time(duration_ms))}</b></span>
  <span class="chip"><b>{scan_name}</b></span>
  {analysed}
  {gen_chip}
</div>
<h1>{_esc(title)}</h1>
{status_banner}
{_stats_html(projection, duration_ms)}
</header>
<section class="deck" id="deck" aria-label="Player and timeline">
<div class="transport">
  <button type="button" class="tp-btn" id="tp-prev"
    aria-label="Previous track">{_ICON_PREV}</button>
  <button type="button" class="tp-btn tp-play" id="tp-play"
    aria-label="Play">{_ICON_PLAY}{_ICON_PAUSE}</button>
  <button type="button" class="tp-btn" id="tp-next" aria-label="Next track">{_ICON_NEXT}</button>
  <span class="tp-time"><b id="tp-pos">0:00</b> / {_esc(_format_time(duration_ms))}</span>
  <div class="tp-now idle" id="tp-now" aria-live="off"><span class="k">NOW</span><span
class="l" id="tp-label">Press play, or pick a track below</span></div>
  <details class="controls"><summary>{_ICON_GEAR}Playback settings</summary><div>
  <label for="leadin">Lead-in</label>
  <input id="leadin" type="number" min="0" step="1" value="{lead_in_s}"
    aria-label="Seek lead-in in seconds"><span>s before each track</span>
</div></details>
</div>
<div class="player">{_embed_html(embed, audio_src)}</div>
<div class="tl-wrap">
{_timeline_html(lanes, gap_markers, segments)}
<div class="tl-tip" id="tl-tip" aria-hidden="true"></div>
</div>
{_ruler_html(duration_ms)}
<div class="legend">
  <span class="lg-solid">colour = how sure we are</span>
  <span class="key k-likely">likely</span><span class="key k-possible">possible</span>
  <span class="key k-unclear">unclear</span><span class="key k-crowd">from comments</span>
  <span class="lg-gap">striped = nothing identified here</span>
  {_set_link_html(embed) if audio_src else ""}
</div>
</section>
<div class="list-head"><h2>Tracklist</h2><span class="count">{track_count}</span>
<span class="hint-k"><kbd>Space</kbd> play / pause<i class="dot"></i><kbd>J</kbd><kbd>K</kbd>
move<i class="dot"></i><kbd>Enter</kbd> play track</span>
<div class="exports">
  <span class="lbl">Export</span>
  <button type="button" class="xbtn copy" id="copy">{_ICON_COPY}Copy tracklist</button>
  <a class="xbtn" href="tracklist.cue" download>CUE sheet</a>
  <a class="xbtn" href="tracklist.md" download>Markdown</a>
  <a class="xbtn" href="tracklist.json" download>JSON</a>
</div></div>
<div class="list-info">
<p class="glossary"><b>Likely</b> means we are confident. <b>Possible</b> means check it.
<b>Unclear</b> means the match is weak. <b>ID</b> marks music we could not identify.</p>
{_confidence_html(projection)}
</div>
<div class="tablewrap"><table>
<caption>Tracklist for {_esc(title)}</caption>
<thead><tr><th scope="col">Time</th><th scope="col">Confidence</th>
<th scope="col">Track</th>{'<th scope="col">Where to get it</th>' if show_acquire else ""}
<th scope="col">Save</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table></div>
<footer><span>🔒 Audio stays local; only short recognition clips are sent to the selected
services.</span>
<span>ID&#39;er</span></footer>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
</main>
<script>{_PAGE_JS}</script>
<script>{PLAYLIST_JS}</script>"""
    # The page's constants, its shared arithmetic and the theme are one script in the head: the
    # theme is applied before first paint, and nothing in it touches the document body.
    head_script = f"""{THEME_JS}
const CONFIG = {config_js};
const PLATFORM = CONFIG.platform;
const EPISODE_SPANS = {episode_spans_js};
const COPY_ENTRIES = {copy_entries_js};
let LEAD_IN_MS = CONFIG.leadInMs;
{_SEEK_JS}
{_PLAYHEAD_JS}
"""
    stamp = f'<meta name="id-detector-page" content="{PAGE_VERSION}">'
    return (
        head_html(
            f"{title} — ID'er",
            _CSS + PLAYLIST_CSS,
            stamp,
            theme_script="none",
            inline_script=head_script,
        )
        + f"<body>{body}</body></html>\n"
    )


def generate_page(
    *,
    media_dir: Path,
    output_dir: Path | None = None,
    source: SourceRecord,
    episodes: EpisodesFile,
    identities: IdentitiesRecord,
    duration_ms: int,
    episodes_path: Path,
    identities_path: Path,
    acquire: AcquireFile | None = None,
    acquire_path: Path | None = None,
    lead_in_ms: int = DEFAULT_LEAD_IN_MS,
    collapse: bool = True,
    same_track_bridge_ms: int | None = None,
    min_track_ms: int = 0,
    projection: CanonicalProjection | None = None,
    status: str = "complete",
    reason: str | None = None,
    achieved: str | None = None,
    analysed_at: str | None = None,
) -> Path:
    """Render and atomically write ``present/index.html`` with a completion sidecar."""

    html_text = render_page(
        source=source,
        episodes=episodes,
        identities=identities,
        duration_ms=duration_ms,
        acquire=acquire,
        lead_in_ms=lead_in_ms,
        collapse=collapse,
        same_track_bridge_ms=same_track_bridge_ms,
        min_track_ms=min_track_ms,
        projection=projection,
        status=status,
        reason=reason,
        achieved=achieved,
        analysed_at=analysed_at,
    )
    index_path = (output_dir or media_dir / "present") / "index.html"
    atomic_write_bytes(index_path, html_text.encode("utf-8"))
    upstream = {
        episodes_path.relative_to(media_dir).as_posix(): episodes_path,
        identities_path.relative_to(media_dir).as_posix(): identities_path,
    }
    if acquire is not None and acquire_path is not None:
        upstream[acquire_path.relative_to(media_dir).as_posix()] = acquire_path
    write_completion_sidecar(index_path, upstream)
    return index_path

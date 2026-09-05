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
from id_detector.io import atomic_write_bytes, write_completion_sidecar
from id_detector.present.exports import (
    _candidate_label,
    _format_time,
    flatten_tracklist,
    hidden_reason,
)
from id_detector.present.theme import PLATFORM_NAMES, head_html, topbar_html

#: Default seek lead-in.  The page exposes a control to change it live; this is the seed value and
#: the value the seek-correctness test pins.
DEFAULT_LEAD_IN_MS = 5_000

#: The plan's unresolved-boundary fallback: a censored side is drawn outward at most this far.
UNRESOLVED_CAP_MS = 120_000

#: Bump when the page's look or behaviour changes: ``present.refresh.ensure_fresh_page`` re-renders
#: any written page whose ``<meta name="id-detector-page">`` stamp is older, so already-analysed
#: mixes pick up the new page the next time they are opened (no re-analysis).
PAGE_VERSION = 5


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


def _acquire_links_html(acquire: dict[str, Any] | None) -> str:
    if not acquire:
        return '<span class="acq none">—</span>'
    chips: list[str] = []
    soundcloud = acquire.get("soundcloud") or {}
    permalink = soundcloud.get("permalink_url")
    purchase = soundcloud.get("purchase_url")
    if acquire.get("free_download") and permalink:
        chips.append(f'<a class="acq free" rel="noopener" href="{_esc(permalink)}">Free DL</a>')
    if acquire.get("gate") and purchase:
        chips.append(f'<a class="acq gate" rel="noopener" href="{_esc(purchase)}">Gate</a>')
    buy_url = purchase if acquire.get("buy") else None
    for link in acquire.get("direct") or ():
        if link.get("kind") == "purchase":
            buy_url = link.get("url")
            break
    if acquire.get("buy") and buy_url:
        chips.append(f'<a class="acq buy" rel="noopener" href="{_esc(buy_url)}">Buy</a>')
    for link in acquire.get("direct") or ():
        if link.get("kind") in {"stream", "catalogue"}:
            source = link.get("source", "link")
            chips.append(
                f'<a class="acq direct" rel="noopener" href="{_esc(link.get("url"))}">'
                f"{_esc(source)}</a>"
            )
    for link in acquire.get("search_links") or ():
        source = link.get("source", "search")
        chips.append(
            f'<a class="acq search" rel="noopener" href="{_esc(link.get("url"))}">'
            f"{_esc(source)}</a>"
        )
    return "".join(chips) if chips else '<span class="acq none">—</span>'


def _alternatives_html(entry: dict[str, Any]) -> str:
    """A native ``<details>`` disclosure listing the collapsed "could also be" alternatives.

    Everything is inline — the list is already in the page, so expanding it makes no request.
    """

    alternatives = entry.get("alternatives") or ()
    count = entry.get("also_count") or 0
    if not count:
        return ""
    items = "".join(
        f'<li><span class="alt-badge badge-{_esc(alt["badge"])}">'
        f"{_esc(str(alt['badge']).upper())}</span> {_esc(alt['track'])}</li>"
        for alt in alternatives
    )
    plural = "s" if count != 1 else ""
    return (
        f'<details class="alts"><summary>▸ {count} other version{plural}</summary>'
        f'<ul class="altlist">{items}</ul></details>'
    )


def _tags_html(entry: dict[str, Any], hidden: str | None = None) -> str:
    """Small inline tags after the track name: an interesting role, a decided version, a hint.

    ``incoming``/``dominant`` are the normal case for a DJ mix, so only ``layer``/``outgoing``/
    ``uncertain`` earn a tag; likewise ``unverified`` is the default and stays silent.
    """

    tags: list[str] = []
    role = str(entry["primary_role"])
    if role in {"layer", "outgoing", "uncertain"}:
        tags.append(f'<span class="tag role-{_esc(role)}">{_esc(role)}</span>')
    version = str(entry["version_status"])
    if version in {"verified", "contested"}:
        tags.append(f'<span class="tag ver-{_esc(version)}">{_esc(version)}</span>')
    if entry["hint_supported"]:
        tags.append('<span class="hint" title="supported by a text hint">hint</span>')
    if hidden == "short":
        seconds = round(int(entry.get("on_air_ms") or 0) / 1000)
        tags.append(f'<span class="tag short-tag">short · {seconds}s</span>')
    elif hidden:
        label = _HIDDEN_LABELS.get(hidden, hidden)
        tags.append(f'<span class="tag short-tag">{_esc(label)}</span>')
    return f'<span class="tags">{"".join(tags)}</span>' if tags else ""


def _track_row_html(
    entry: dict[str, Any], platform: str, index: int = 0, *, hidden: str | None = None
) -> str:
    badge = _esc(entry["badge"])
    version_status = _esc(entry["version_status"])
    role = _esc(entry["primary_role"])
    label = f"{_esc(entry['artist'])} — {_esc(entry['title'])}"
    acquire = _acquire_links_html(entry.get("acquire"))
    best_start = int(entry["start_ms"])
    alternatives = _alternatives_html(entry)
    # ``short`` is the CSS hook for every hidden-by-default row, whatever the reason.
    row_class = "track short" if hidden else "track"
    # ``--i`` staggers the row entrance animation; the hidden ``ver``/``role`` cells keep the
    # export-identical columns available to CSS/tests while the visible row stays uncluttered.
    return (
        f'<tr class="{row_class}" data-episode-id="{_esc(entry["episode_id"])}" '
        f'style="--i:{index}" '
        f'data-best-start-ms="{best_start}" tabindex="0" role="button" '
        f'aria-label="Seek to {_esc(_format_time(best_start))} — {label}">'
        f'<td class="time"><span class="eqi"><i></i><i></i><i></i></span>'
        f"{_esc(_format_time(best_start))}</td>"
        f'<td class="badge badge-{badge}"><span class="pill">{badge}</span></td>'
        f'<td class="ver ver-{version_status}">{version_status}</td>'
        f'<td class="role">{role}</td>'
        f'<td class="label"><span class="ar">{_esc(entry["artist"])}</span>'
        f'<span class="sep">—</span><span class="tt">{_esc(entry["title"])}</span>'
        f"{_tags_html(entry, hidden)}{alternatives}</td>"
        f'<td class="acquire">{acquire}</td>'
        f'<td class="ops"><button type="button" class="rescan" '
        f'title="Ask for a rescan around here" data-trigger="edge" '
        f'data-start-ms="{best_start}" '
        f'data-end-ms="{int(entry.get("end_ms") or best_start)}">rescan</button></td>'
        "</tr>"
    )


def _gap_row_html(entry: dict[str, Any]) -> str:
    start = int(entry["start_ms"])
    end = int(entry["end_ms"])
    span = f"{_format_time(start)}–{_format_time(end)}"
    return (
        f'<tr class="gap" data-gap-id="{_esc(entry["gap_id"])}">'
        f'<td class="time">{_esc(_format_time(start))}</td>'
        f'<td class="badge badge-gap"><span class="pill">ID</span></td>'
        f'<td class="ver">—</td><td class="role">gap</td>'
        f'<td class="label"><span class="tt">ID</span><span class="sep">—</span>'
        f'no evidence for {_esc(span)} <span class="tags"><span class="tag">'
        f"{_esc(entry['reason'])}</span></span></td>"
        f'<td class="acquire"><span class="acq none">—</span></td>'
        f'<td class="ops"><button type="button" class="rescan" data-trigger="gap" '
        f'data-start-ms="{start}" data-end-ms="{end}">rescan</button></td>'
        "</tr>"
    )


def _timeline_html(lanes: list[dict[str, Any]], gaps: list[dict[str, Any]]) -> str:
    parts: list[str] = ['<div class="timeline" role="img" aria-label="Evidence timeline">']
    for gap in gaps:
        title = (
            f"gap {_format_time(gap['start_ms'])}–{_format_time(gap['end_ms'])}: "
            f"{gap['n_windows']} windows, {gap['n_no_match']} no-match, {gap['n_error']} error"
        )
        parts.append(
            f'<div class="tl-gap" style="left:{gap["left"]:.3f}%;width:{gap["width"]:.3f}%" '
            f'title="{_esc(title)}"></div>'
        )
    for lane in lanes:
        ext = lane["extent"]
        # ``data-badge`` colours the lane by confidence, so the strip reads as a confidence map.
        parts.append(
            f'<div class="tl-lane" data-episode-id="{_esc(lane["episode_id"])}" '
            f'data-badge="{_esc(lane["badge"])}"{' data-short="1"' if lane.get("short") else ""} '
            f'title="{_esc(lane["label"])}">'
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
                f'<div class="tl-solid" '
                f'style="left:{solid["left"]:.3f}%;width:{solid["width"]:.3f}%"></div>'
            )
        parts.append("</div>")
    # Live playhead: an absolutely-positioned vertical line that tracks the embedded player.  It
    # starts hidden and is revealed by the first position event; ``pointer-events:none`` lets clicks
    # fall through to the timeline's click-to-seek handler.
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

#: Friendly labels for fusion's ``suppressed`` reason tokens (an unknown token is shown as-is).
_HIDDEN_LABELS = {
    "buried": "buried under a surer track",
    "contradicted": "contradicted by comments",
    "scatter": "scattered detections",
}


def _stats_html(
    entries: tuple[dict[str, Any], ...], episodes: EpisodesFile, duration_ms: int
) -> str:
    """The hero stat tiles: tracks found, share of the set identified, confidence mix, ID gaps.

    "Identified" is honest coverage — listening time backed by proved evidence (plus any
    calibrated predicted episode time) over the set's length — not a guess at how many tracks
    were in the set.
    """

    tracks = [entry for entry in entries if entry["kind"] == "track"]
    counts = dict.fromkeys(_BADGE_ORDER, 0)
    for entry in tracks:
        key = str(entry["badge"])
        counts[key] = counts.get(key, 0) + 1
    n = len(tracks)
    covered = episodes.durations.evidence_supported_ms + episodes.durations.predicted_episode_ms
    pct = int(round(max(0.0, min(100.0, covered * 100.0 / duration_ms)))) if duration_ms else 0
    bars = "".join(
        f'<i class="c-{key}" style="width:{counts[key] * 100.0 / n:.2f}%"></i>'
        for key in _BADGE_ORDER
        if n and counts[key]
    )
    key_html = "".join(
        f'<span style="--k:var(--{key})">{counts[key]} {key}</span>'
        for key in _BADGE_ORDER
        if counts[key]
    )
    gaps = len(episodes.gaps)
    return (
        '<div class="stats">'
        f'<div class="stat"><div><span class="big">{n}</span>'
        f"<small>track{'s' if n != 1 else ''} found</small></div></div>"
        f'<div class="stat"><div class="ring" style="--p:{pct}">'
        '<svg viewBox="0 0 36 36" aria-hidden="true"><defs><linearGradient id="ringgrad" '
        'x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ff3d8a"></stop>'
        '<stop offset=".55" stop-color="#8b5cf6"></stop><stop offset="1" stop-color="#22d3ee">'
        "</stop></linearGradient></defs>"
        '<circle class="bg" cx="18" cy="18" r="15.5" pathLength="100"></circle>'
        '<circle class="fg" cx="18" cy="18" r="15.5" pathLength="100"></circle></svg>'
        f"<b>{pct}%</b></div><div><b>of the set identified</b>"
        "<small>listening time backed by evidence</small></div></div>"
        f'<div class="stat"><div class="conf"><div class="conf-bar">{bars}</div>'
        f'<div class="conf-key">{key_html}</div></div></div>'
        f'<div class="stat"><div><span class="big">{gaps}</span>'
        f"<small>ID gap{'s' if gaps != 1 else ''}</small></div></div>"
        "</div>"
    )


def _embed_html(embed: EmbedPlan) -> str:
    link = f'<a class="setlink" rel="noopener" href="{_esc(embed.link_url)}">Open the set ↗</a>'
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
_CSS = """
:root{--pi:#a78bfa;--unresolved:#fbbf24;--extent:#7d7d99}
/* hero */
.hero{padding:26px 0 18px}
.meta{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
h1{font:800 clamp(28px,4.2vw,44px)/1.08 var(--display);letter-spacing:-.03em;margin:0 0 20px;
max-width:24ch}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;
display:flex;align-items:center;gap:14px;min-height:84px;position:relative;overflow:hidden}
.stat::after{content:"";position:absolute;inset:0;pointer-events:none;
background:linear-gradient(180deg,#ffffff06,transparent)}
.stat .big{font:800 34px/1 var(--display);letter-spacing:-.03em;font-variant-numeric:tabular-nums}
.stat small{display:block;color:var(--muted);font-size:12px;margin-top:4px}
.ring{position:relative;width:56px;height:56px;flex:none}
.ring svg{width:56px;height:56px;transform:rotate(-90deg)}
.ring circle{fill:none;stroke-width:3.4}
.ring .bg{stroke:#ffffff14}
.ring .fg{stroke:url(#ringgrad);stroke-linecap:round;stroke-dasharray:var(--p) 100;
animation:ring 1.1s cubic-bezier(.2,.8,.2,1) both}
@keyframes ring{from{stroke-dasharray:0 100}}
.ring b{position:absolute;inset:0;display:grid;place-items:center;font:700 13px/1 var(--display);
font-variant-numeric:tabular-nums}
.conf{flex:1}
.conf-bar{display:flex;height:10px;border-radius:6px;overflow:hidden;background:#ffffff10;gap:2px}
.conf-bar i{display:block;height:100%;min-width:0}
.c-verified{background:var(--verified)}.c-likely{background:var(--likely)}
.c-possible{background:var(--possible)}.c-unclear{background:var(--unclear)}
.conf-key{display:flex;gap:10px;flex-wrap:wrap;margin-top:8px;font-size:11px;color:var(--muted);
font-variant-numeric:tabular-nums}
.conf-key span::before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;
margin-right:5px;background:var(--k)}
/* player + exports */
.player{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px;
margin:18px 0 14px;box-shadow:0 20px 50px -30px rgba(0,0,0,.9)}
.player iframe{display:block;border-radius:10px;border:0}
.yt{aspect-ratio:16/9;width:100%;border-radius:10px;overflow:hidden;background:#000}
.yt iframe,iframe#yt-player{width:100%;height:100%;aspect-ratio:16/9}
.fallback{margin-top:10px;font-size:13px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.noembed{padding:18px 6px;color:var(--muted)}
.setlink{display:inline-flex;align-items:center;gap:6px;color:var(--fg);
border:1px solid var(--line2);
border-radius:999px;padding:5px 12px;font-size:12px;font-weight:600;background:#ffffff08}
.setlink:hover{text-decoration:none;background:#ffffff12}
.exports{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:0 0 6px}
.exports .lbl{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.12em;
font-weight:700;margin-right:4px}
.xbtn{display:inline-flex;align-items:center;gap:6px;font:600 12px/1 var(--text);color:var(--muted);
border:1px solid var(--line);border-radius:8px;padding:7px 10px;background:transparent;
cursor:pointer;transition:color .12s,border-color .12s,background .12s}
.xbtn:hover{color:var(--fg);border-color:var(--line2);background:#ffffff08;text-decoration:none}
.xbtn.copy{color:var(--fg);border-color:var(--line2)}
/* timeline */
.tl-wrap{background:var(--card);border:1px solid var(--line);border-radius:16px;
padding:14px 16px 10px;margin:14px 0}
.tl-head{display:flex;align-items:center;gap:14px;margin-bottom:10px;flex-wrap:wrap}
.tl-head h2{font:700 12px/1 var(--display);letter-spacing:.14em;text-transform:uppercase;
color:var(--muted);margin:0;flex:1}
.controls{display:flex;gap:8px;align-items:center;font-size:12px;color:var(--muted)}
.controls input{width:64px;padding:5px 8px;border:1px solid var(--line2);border-radius:8px;
background:#ffffff08;color:var(--fg);font:inherit;font-variant-numeric:tabular-nums;
text-align:center}
.controls input:focus{outline:none;border-color:var(--accent);
box-shadow:0 0 0 3px rgba(167,139,250,.25)}
.timeline{position:relative;height:76px;background:#0c0c13;border:1px solid var(--line);
border-radius:10px;overflow:hidden;cursor:crosshair;
background-image:repeating-linear-gradient(90deg,transparent 0 calc(10% - 1px),
#ffffff0a calc(10% - 1px) 10%)}
.tl-lane{position:absolute;top:0;height:100%;left:0;width:100%;pointer-events:none;
--lc:var(--unclear)}
.tl-lane[data-badge="verified"]{--lc:var(--verified)}
.tl-lane[data-badge="likely"]{--lc:var(--likely)}
.tl-lane[data-badge="possible"]{--lc:var(--possible)}
.tl-lane[data-badge="unclear"]{--lc:var(--unclear)}
.tl-extent{position:absolute;top:22px;height:30px;background:var(--lc);opacity:.22;
border-radius:5px;transition:opacity .15s}
.tl-solid{position:absolute;top:24px;height:26px;background:var(--lc);border-radius:4px;
box-shadow:0 0 12px -3px var(--lc);transition:filter .15s}
.tl-pi{position:absolute;top:22px;height:30px;background:var(--pi);opacity:.4;border-radius:5px}
.tl-unresolved{position:absolute;top:22px;height:30px;border-radius:5px;opacity:.75;
background:repeating-linear-gradient(45deg,var(--unresolved),var(--unresolved) 3px,
transparent 3px,transparent 8px)}
.tl-gap{position:absolute;top:0;height:100%;pointer-events:none;
background:repeating-linear-gradient(135deg,rgba(251,113,133,.28) 0 6px,
rgba(251,113,133,.06) 6px 12px)}
.tl-lane.current .tl-extent,.tl-lane.hover .tl-extent{opacity:.5}
.tl-lane.current .tl-solid{box-shadow:0 0 0 2px #fff,0 0 18px var(--lc);z-index:2}
.tl-lane.hover .tl-solid{filter:brightness(1.35)}
.playhead{position:absolute;top:0;left:0;height:100%;width:2px;background:var(--pink);z-index:4;
pointer-events:none;box-shadow:0 0 10px var(--pink),0 0 2px #fff}
.playhead[hidden]{display:none}
.playhead-time{position:absolute;top:4px;left:5px;font:700 10px/1.4 var(--mono);
font-variant-numeric:tabular-nums;background:var(--pink);color:#fff;border-radius:4px;
padding:1px 5px;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.4)}
.ruler{position:relative;height:18px;margin:4px 2px 0;font:10px/1 var(--mono);color:var(--dim);
font-variant-numeric:tabular-nums}
.ruler span{position:absolute;top:2px;transform:translateX(-50%)}
.ruler span::before{content:"";position:absolute;left:50%;top:-6px;width:1px;height:4px;
background:var(--dim)}
.legend{display:flex;gap:6px 14px;flex-wrap:wrap;color:var(--muted);font-size:11px;margin-top:8px;
padding-top:8px;border-top:1px solid var(--line)}
.legend span{display:inline-flex;align-items:center;gap:6px}
.legend span::before{content:"";display:inline-block;width:14px;height:8px;border-radius:2px}
.lg-solid::before{background:linear-gradient(90deg,var(--verified),var(--likely),var(--possible),
var(--unclear))}
.lg-extent::before{background:var(--extent);opacity:.35}
.lg-pi::before{background:var(--pi);opacity:.6}
.lg-unresolved::before{background:repeating-linear-gradient(45deg,var(--unresolved),
var(--unresolved) 2px,transparent 2px,transparent 5px)}
.lg-gap::before{background:repeating-linear-gradient(135deg,rgba(251,113,133,.6) 0 3px,
transparent 3px 6px)}
/* tracklist */
.list-head{display:flex;align-items:baseline;gap:14px;margin:26px 0 10px;flex-wrap:wrap}
.list-head h2{font:800 22px/1 var(--display);letter-spacing:-.02em;margin:0}
.list-head .hint-k{color:var(--dim);font-size:12px;margin-left:auto}
.tablewrap{overflow-x:auto;border-radius:16px}
table{width:100%;border-collapse:separate;border-spacing:0;background:var(--card);
border:1px solid var(--line);border-radius:16px;overflow:hidden}
th{font:700 10px/1 var(--display);letter-spacing:.14em;text-transform:uppercase;color:var(--dim);
white-space:nowrap;padding:12px 12px 10px;text-align:left;border-bottom:1px solid var(--line);
background:#ffffff04}
td{padding:11px 12px;border-bottom:1px solid var(--line);vertical-align:middle}
tbody tr:last-child td{border-bottom:none}
tr.track{cursor:pointer;transition:background .12s;animation:rise .45s ease both;
animation-delay:calc(min(var(--i,0),40)*14ms)}
tr.track:hover td,tr.track:focus td{background:#ffffff07}tr.track:focus{outline:none}
tr.track:focus-visible td:first-child{box-shadow:inset 3px 0 0 var(--accent)}
tr.track.current td{background:linear-gradient(90deg,rgba(255,61,138,.16),
rgba(255,61,138,.03) 55%,transparent)}
tr.track.current td:first-child{box-shadow:inset 3px 0 0 var(--pink)}
.time{font:600 13px/1 var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap;
color:var(--muted);width:1%}
tr.track.current .time{color:var(--pink)}
.time .eqi{display:none;vertical-align:-2px;margin-right:6px}
tr.track.current .time .eqi{display:inline-flex}
.eqi{align-items:flex-end;gap:1.5px;height:10px;width:10px}
.eqi i{display:block;width:2px;height:3px;background:var(--pink);border-radius:1px;
animation:eq .8s ease-in-out infinite}
.eqi i:nth-child(2){animation-delay:.2s}.eqi i:nth-child(3){animation-delay:.4s}
.badge{width:1%;white-space:nowrap}
.pill{display:inline-block;font:700 10px/1 var(--display);letter-spacing:.1em;
text-transform:uppercase;padding:5px 8px;border-radius:6px;color:var(--bc);
background:color-mix(in srgb,var(--bc) 16%,transparent);
border:1px solid color-mix(in srgb,var(--bc) 35%,transparent)}
.badge-verified{--bc:var(--verified)}.badge-likely{--bc:var(--likely)}
.badge-possible{--bc:var(--possible)}.badge-unclear{--bc:var(--unclear)}.badge-gap{--bc:var(--gap)}
.ver,.role{display:none}
.label{min-width:0}
.ar{color:var(--muted)}.sep{color:var(--dim);margin:0 6px}.tt{font-weight:600;color:var(--fg)}
.tags{display:inline-flex;gap:6px;margin-left:10px;vertical-align:1px;flex-wrap:wrap}
.tag{font:600 10px/1 var(--text);color:var(--dim);border:1px solid var(--line);border-radius:5px;
padding:3px 6px;letter-spacing:.02em;text-transform:lowercase}
.tag.role-outgoing{color:var(--violet);border-color:rgba(139,92,246,.4)}
.tag.role-layer{color:var(--possible);border-color:rgba(251,191,36,.4)}
.tag.ver-verified{color:var(--verified);border-color:rgba(52,211,153,.4)}
.tag.ver-contested{color:var(--gap);border-color:rgba(251,113,133,.4)}
.hint{font:700 10px/1 var(--display);background:var(--grad);color:#fff;border-radius:5px;
padding:3px 6px;letter-spacing:.06em;text-transform:uppercase}
.alts{margin-top:5px;font-size:12px}
.alts>summary{cursor:pointer;color:var(--muted);list-style:none;display:inline-flex;
align-items:center;gap:5px;border:1px solid var(--line);border-radius:6px;padding:3px 8px;
font-size:11px;font-weight:600}
.alts>summary::-webkit-details-marker{display:none}
.alts>summary:hover,.alts[open]>summary{color:var(--fg);border-color:var(--line2)}
.altlist{margin:8px 0 2px;padding:0 0 0 2px;list-style:none;color:var(--muted)}
.altlist li{margin:4px 0;display:flex;align-items:center;gap:8px}
.alt-badge{font:700 9px/1 var(--display);letter-spacing:.1em;padding:3px 5px;border-radius:4px;
color:var(--bc);border:1px solid color-mix(in srgb,var(--bc) 35%,transparent)}
.acquire{white-space:nowrap;width:1%}
.acq{display:inline-flex;align-items:center;margin:0 4px 2px 0;font:600 11px/1 var(--text);
border:1px solid var(--line2);border-radius:6px;padding:5px 8px;color:var(--fg);
background:#ffffff06}
.acq:hover{text-decoration:none;background:#ffffff12}
.acq.free{border-color:rgba(52,211,153,.5);color:var(--verified)}
.acq.buy,.acq.gate{border-color:rgba(251,191,36,.5);color:var(--possible)}
.acq.none{border:none;color:var(--dim);background:none;padding-left:0}
.ops{width:1%;white-space:nowrap;text-align:right}
.rescan{font:600 11px/1 var(--text);background:transparent;border:1px solid transparent;
border-radius:7px;padding:6px 8px;color:var(--dim);cursor:pointer;opacity:0;
transition:opacity .12s,color .12s}
tr:hover .rescan,tr:focus-within .rescan{opacity:1}
.rescan:hover{color:var(--fg);border-color:var(--line2);background:#ffffff08}
tr.gap td{background:repeating-linear-gradient(135deg,rgba(251,113,133,.05) 0 8px,
transparent 8px 16px)}
tr.gap .label{color:var(--muted)}
tr.gap .tt{color:var(--gap)}
/* short matches (under the configured on-air floor): hidden until asked for */
tr.track.short{display:none}body.show-short tr.track.short{display:table-row}
body.show-short tr.track.short td{opacity:.72}
.tl-lane[data-short="1"]{display:none}body.show-short .tl-lane[data-short="1"]{display:block}
.tag.short-tag{color:var(--gap);border-color:rgba(251,113,133,.35)}
.short-note{color:var(--muted);font-size:12px;display:inline-flex;gap:5px;align-items:center}
.short-note b{color:var(--fg)}
.linkish{background:none;border:0;padding:0;color:var(--accent);cursor:pointer;font:inherit;
font-size:12px}
.linkish:hover{text-decoration:underline}
/* the NOW pill in the top bar */
.now{flex:1;min-width:0;display:flex;align-items:center;gap:10px;justify-content:center;
font-size:13px;cursor:pointer}
.now[hidden]{display:none}
.now-k{font:700 10px/1 var(--display);letter-spacing:.14em;color:var(--pink);padding:5px 7px;
border:1px solid rgba(255,61,138,.5);border-radius:6px;background:rgba(255,61,138,.08)}
.now-t{font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--muted)}
.now-l{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
@media (max-width:720px){.now{display:none}.hint-k{display:none}
th:nth-child(6),td.acquire{display:none}.stat .big{font-size:28px}.hero{padding-top:14px}
.exports .lbl{display:none}.tl-wrap{padding:12px}}
"""


# The page's own JavaScript.  Everything the tests pin (player bindings, playhead, current-row
# highlight, timeline click-to-seek, the ``closest('a,button,details,summary')`` guard) is here
# verbatim; on top of it: the NOW pill, copy-tracklist, arrow-key row navigation, row↔lane hover.
_PAGE_JS = """
let CURRENT_POSITION_MS = null, PLAYER_DURATION_MS = 0;
let scWidget = null, ytPlayer = null, mcWidget = null, ytTimer = null, playTimer = null;
const ROW_LABELS = {};
function ready(fn){ if(document.readyState!=='loading'){fn();}
  else{document.addEventListener('DOMContentLoaded',fn);} }
function toast(msg){ const t=document.getElementById('toast'); if(!t) return;
  t.textContent=msg; t.classList.add('show'); clearTimeout(t._h);
  t._h=setTimeout(()=>t.classList.remove('show'),2200); }
// Playhead readout -----------------------------------------------------------
function playheadDuration(){
  return (CONFIG.durationMs > 0) ? CONFIG.durationMs : PLAYER_DURATION_MS;
}
function markPlaying(){
  document.body.classList.add('playing'); clearTimeout(playTimer);
  playTimer = setTimeout(function(){ document.body.classList.remove('playing'); }, 1800);
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
  markPlaying();
  highlightCurrent(positionMs);
}
function highlightCurrent(positionMs){
  let currentId = null;
  for(let i=0;i<EPISODE_SPANS.length;i++){
    const span = EPISODE_SPANS[i];
    if(positionMs >= span.start && positionMs < span.end){ currentId = span.id; break; }
  }
  document.querySelectorAll('tr.track.current, .tl-lane.current').forEach(function(el){
    el.classList.remove('current'); });
  if(currentId){
    document.querySelectorAll('[data-episode-id="'+currentId+'"]').forEach(function(el){
      if(el.classList.contains('track') || el.classList.contains('tl-lane')){
        el.classList.add('current'); }
    });
  }
  setNowPlaying(currentId, positionMs);
}
function setNowPlaying(id, positionMs){
  const now = document.getElementById('now'); if(!now) return;
  if(!id){ now.hidden = true; return; }
  now.hidden = false;
  document.getElementById('now-time').textContent = formatTime(positionMs);
  document.getElementById('now-label').textContent = ROW_LABELS[id] || '';
}
// Player bindings ------------------------------------------------------------
ready(function(){
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
function seekToMs(bestStartMs){ return seekPlayerArg(seekArgument(bestStartMs, LEAD_IN_MS)); }
function seekToPositionMs(positionMs){ return seekPlayerArg(seekArgument(positionMs, 0)); }
// Copy the tracklist as plain text (time, artist — title; ID for a gap) ------
function tracklistText(){
  const lines = [];
  const showingShort = document.body.classList.contains('show-short');
  document.querySelectorAll('tr.track, tr.gap').forEach(function(row){
    if(row.classList.contains('short') && !showingShort) return;
    const t = row.querySelector('.time').textContent.trim();
    if(row.classList.contains('gap')){ lines.push(t + '  ID'); return; }
    lines.push(t + '  ' + (ROW_LABELS[row.getAttribute('data-episode-id')] || ''));
  });
  return lines.join('\\n');
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
  const timeline = document.querySelector('.timeline');
  if(timeline){
    timeline.addEventListener('click', function(e){
      const rect = timeline.getBoundingClientRect();
      if(rect.width <= 0) return;
      const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      const ms = Math.round(frac * playheadDuration());
      updatePlayhead(ms);
      if(!seekToPositionMs(ms)){ toast('Player not ready — open the set link'); }
    });
  }
  window.addEventListener('resize', function(){
    if(CURRENT_POSITION_MS !== null) updatePlayhead(CURRENT_POSITION_MS); });
  const rows = Array.prototype.slice.call(document.querySelectorAll('tr.track'));
  rows.forEach(function(row, index){
    const id = row.getAttribute('data-episode-id');
    const ar = row.querySelector('.ar'), tt = row.querySelector('.tt');
    ROW_LABELS[id] = (ar ? ar.textContent + ' — ' : '') + (tt ? tt.textContent : '');
    function go(){
      const ms = parseInt(row.getAttribute('data-best-start-ms'), 10) || 0;
      if(!seekToMs(ms)){ toast('Player not ready — open the set link'); }
    }
    row.addEventListener('click', function(e){
      if(e.target.closest('a,button,details,summary')) return; go(); });
    row.addEventListener('keydown', function(e){
      if(e.key==='Enter' || e.key===' '){ e.preventDefault(); go(); }
      else if(e.key==='ArrowDown' && rows[index+1]){ e.preventDefault(); rows[index+1].focus(); }
      else if(e.key==='ArrowUp' && rows[index-1]){ e.preventDefault(); rows[index-1].focus(); }
    });
    const lane = document.querySelector('.tl-lane[data-episode-id="'+id+'"]');
    if(lane){
      row.addEventListener('mouseenter', function(){ lane.classList.add('hover'); });
      row.addEventListener('mouseleave', function(){ lane.classList.remove('hover'); });
    }
  });
  document.querySelectorAll('button.rescan').forEach(function(btn){
    btn.addEventListener('click', function(e){
      e.stopPropagation();
      requestRescan(btn.getAttribute('data-trigger'),
        parseInt(btn.getAttribute('data-start-ms'),10)||0,
        parseInt(btn.getAttribute('data-end-ms'),10)||0);
    });
  });
  const copy = document.getElementById('copy');
  if(copy) copy.addEventListener('click', copyTracklist);
  const showShort = document.getElementById('show-short');
  if(showShort) showShort.addEventListener('click', function(){
    const on = document.body.classList.toggle('show-short');
    showShort.textContent = on ? 'hide' : 'show';
    if(CURRENT_POSITION_MS !== null) updatePlayhead(CURRENT_POSITION_MS);
  });
  const now = document.getElementById('now');
  if(now) now.addEventListener('click', function(){
    const row = document.querySelector('tr.track.current');
    if(row){ row.scrollIntoView({block:'center', behavior:'smooth'}); }
  });
});
function requestRescan(trigger, startMs, endMs){
  fetch('/rescan', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({media_key: CONFIG.mediaKey, trigger: trigger,
      start_ms: startMs, end_ms: endMs})})
    .then(function(r){ return r.ok ? r.json() : Promise.reject(r.status); })
    .then(function(){ toast('Rescan queued — run `id-detector rescan`'); })
    .catch(function(){ toast('Rescan needs the local server (id-detector serve)'); });
}
"""


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
) -> str:
    """Render the complete self-contained HTML page as a string.

    With ``collapse`` (the default), a contiguous run of competing near-duplicate matches of the
    same underlying track collapses to one display-track row (with a "▸ N other versions"
    disclosure); two appearances of the SAME exact track up to ``same_track_bridge_ms`` apart
    (``None`` → the grouping default) with no different confident track between them likewise stack
    into one row.  The timeline lane, the current-row highlight and the seek all use that display
    track's primary.  ``collapse=False`` restores the one-lane-per-episode view.

    ``min_track_ms`` (default ``0`` = off) marks track rows that played too briefly to be a real
    track (see :func:`~id_detector.present.exports.short_track`): they stay in the page but are
    hidden behind a "N matches hidden · show" toggle and are left out of the stats, the
    timeline and the playhead partition; so are rows fusion marked ``suppressed``.  The
    exports drop both outright.
    """

    embed = plan_embed(source)
    # Every row, including short/suppressed ones: the page hides them itself (see hidden_by_id).
    entries = flatten_tracklist(
        episodes,
        identities,
        acquire,
        collapse=collapse,
        same_track_bridge_ms=same_track_bridge_ms,
        include_hidden=True,
    )
    boundaries = _evidence_boundaries(list(episodes.episodes))
    hidden_by_id = {
        e["episode_id"]: reason
        for e in entries
        if e["kind"] == "track" and (reason := hidden_reason(e, min_track_ms)) is not None
    }
    short_ids = set(hidden_by_id)
    visible = tuple(e for e in entries if e.get("episode_id") not in short_ids)

    # A display track is one collapsed row (primary + folded-in alternatives); ungrouped, it is one
    # episode.  Lanes, the highlight partition and the row all key off the primary's id so the
    # Stage 11 playhead lights the same row + lane as the tracklist.
    if collapse:
        from id_detector.present.grouping import (
            DEFAULT_SAME_TRACK_BRIDGE_MS,
            group_display_tracks,
        )

        bridge_ms = (
            DEFAULT_SAME_TRACK_BRIDGE_MS if same_track_bridge_ms is None else same_track_bridge_ms
        )
        display_tracks = group_display_tracks(
            list(episodes.episodes), identities, duration_ms, same_track_bridge_ms=bridge_ms
        )
        lane_episodes = [track.primary for track in display_tracks]
        span_items = [(track.primary.id, track.start_ms, track.end_ms) for track in display_tracks]
    else:
        lane_episodes = list(episodes.episodes)
        span_items = [
            (episode.id, episode.best_start_ms, episode.best_end_ms)
            for episode in episodes.episodes
        ]

    lanes = [
        _timeline_lane(
            episode,
            " — ".join(_candidate_label(identities, episode.candidate_id)),
            boundaries,
            duration_ms,
        )
        for episode in lane_episodes
    ]
    for lane in lanes:
        lane["short"] = lane["episode_id"] in short_ids
    span_items = [item for item in span_items if item[0] not in short_ids]
    gap_markers = [_gap_marker(gap, duration_ms) for gap in episodes.gaps]

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

    rows: list[str] = []
    for index, entry in enumerate(entries):
        if entry["kind"] == "track":
            hidden = hidden_by_id.get(entry["episode_id"])
            rows.append(_track_row_html(entry, source.platform, index, hidden=hidden))
        else:
            rows.append(_gap_row_html(entry))

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
    )
    episode_spans_js = json.dumps(episode_spans)
    # The control shows whole seconds; the page converts back to milliseconds on change.
    lead_in_s = f"{lead_in_ms / 1000:g}"
    short_note = ""
    if short_ids:
        n = len(short_ids)
        n_short = sum(1 for reason in hidden_by_id.values() if reason == "short")
        seconds = f"{min_track_ms / 1000:g}"
        plural = "es" if n != 1 else ""
        if n_short == n:
            why = f"short match{plural} (under {seconds} s)"
        elif n_short == 0:
            why = f"suppressed match{plural}"
        else:
            why = f"matches ({n_short} short, {n - n_short} suppressed)"
        short_note = (
            f'<span class="short-note" id="short-note"><b>{n}</b> {why} hidden · '
            '<button type="button" class="linkish" id="show-short">show</button></span>'
        )
    now_pill = (
        '<div class="now" id="now" hidden title="Jump to the current track">'
        '<span class="now-k">NOW</span><span class="now-t" id="now-time"></span>'
        '<span class="now-l" id="now-label"></span></div>'
    )

    body = f"""{topbar_html(back=True, new=True, middle=now_pill)}
<main>
<header class="hero">
<div class="meta">
  <span class="chip plat-{_esc(source.platform)}"><span
class="pd"></span>{_esc(platform_name)}</span>
  <span class="chip" id="dur">length <b>{_esc(_format_time(duration_ms))}</b></span>
  <span class="chip">profile <b>{_esc(episodes.certification.profile)}</b></span>
  <span class="chip">generation <b>{episodes.generation}</b></span>
</div>
<h1>{_esc(title)}</h1>
{_stats_html(visible, episodes, duration_ms)}
</header>
<section class="player">{_embed_html(embed)}</section>
<div class="exports">
  <span class="lbl">Export</span>
  <button type="button" class="xbtn copy" id="copy">Copy tracklist</button>
  <a class="xbtn" href="tracklist.cue" download>CUE sheet</a>
  <a class="xbtn" href="tracklist.m3u" download>M3U playlist</a>
  <a class="xbtn" href="tracklist.md" download>Markdown</a>
  <a class="xbtn" href="tracklist.json" download>JSON</a>
</div>
<section class="tl-wrap">
<div class="tl-head"><h2>Timeline</h2>
<div class="controls">
  <label for="leadin">Lead-in</label>
  <input id="leadin" type="number" min="0" step="1" value="{lead_in_s}"
    aria-label="Seek lead-in in seconds"><span>s before each track</span>
</div></div>
{_timeline_html(lanes, gap_markers)}
{_ruler_html(duration_ms)}
<div class="legend">
  <span class="lg-solid">evidence (proved, coloured by confidence)</span>
  <span class="lg-extent">episode extent</span>
  <span class="lg-pi">prediction interval</span>
  <span class="lg-unresolved">unresolved boundary</span>
  <span class="lg-gap">ID gap</span>
</div>
</section>
<div class="list-head"><h2>Tracklist</h2>{short_note}
<span class="hint-k">click a row to jump there · <kbd>↑</kbd><kbd>↓</kbd> move · <kbd>↵</kbd>
play</span></div>
<div class="tablewrap"><table>
<thead><tr><th>Time</th><th>Confidence</th><th class="ver">Version</th><th class="role">Role</th>
<th>Track</th><th>Where to get it</th><th></th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table></div>
<footer><span>🔒 ran entirely on this machine — nothing leaves 127.0.0.1</span>
<span>id-detector</span></footer>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
</main>
<script>
const CONFIG = {config_js};
const PLATFORM = CONFIG.platform;
const EPISODE_SPANS = {episode_spans_js};
let LEAD_IN_MS = CONFIG.leadInMs;
{_SEEK_JS}
{_PLAYHEAD_JS}
</script>
<script>{_PAGE_JS}</script>"""
    stamp = f'<meta name="id-detector-page" content="{PAGE_VERSION}">'
    return head_html(f"{title} — id-detector", _CSS, stamp) + f"<body>{body}</body></html>\n"


def generate_page(
    *,
    media_dir: Path,
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
    )
    index_path = media_dir / "present" / "index.html"
    atomic_write_bytes(index_path, html_text.encode("utf-8"))
    upstream = {
        episodes_path.relative_to(media_dir).as_posix(): episodes_path,
        identities_path.relative_to(media_dir).as_posix(): identities_path,
    }
    if acquire is not None and acquire_path is not None:
        upstream[acquire_path.relative_to(media_dir).as_posix()] = acquire_path
    write_completion_sidecar(index_path, upstream)
    return index_path

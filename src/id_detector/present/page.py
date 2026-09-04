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
from urllib.parse import quote, urlsplit

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
)

#: Default seek lead-in.  The page exposes a control to change it live; this is the seed value and
#: the value the seek-correctness test pins.
DEFAULT_LEAD_IN_MS = 5_000

#: The plan's unresolved-boundary fallback: a censored side is drawn outward at most this far.
UNRESOLVED_CAP_MS = 120_000


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


def _track_row_html(entry: dict[str, Any], platform: str) -> str:
    badge = _esc(entry["badge"]).upper()
    version_status = _esc(entry["version_status"])
    role = _esc(entry["primary_role"])
    label = f"{_esc(entry['artist'])} — {_esc(entry['title'])}"
    hint = (
        ' <span class="hint" title="supported by a text hint">hint</span>'
        if entry["hint_supported"]
        else ""
    )
    acquire = _acquire_links_html(entry.get("acquire"))
    best_start = int(entry["start_ms"])
    alternatives = _alternatives_html(entry)
    return (
        f'<tr class="track" data-episode-id="{_esc(entry["episode_id"])}" '
        f'data-best-start-ms="{best_start}" tabindex="0" role="button" '
        f'aria-label="Seek to {_esc(_format_time(best_start))} — {label}">'
        f'<td class="time">{_esc(_format_time(best_start))}</td>'
        f'<td class="badge badge-{_esc(entry["badge"])}">{badge}</td>'
        f'<td class="ver ver-{version_status}">{version_status}</td>'
        f'<td class="role">{role}</td>'
        f'<td class="label">{label}{hint}{alternatives}</td>'
        f'<td class="acquire">{acquire}</td>'
        f'<td class="ops"><button type="button" class="rescan" '
        f'data-trigger="edge" data-start-ms="{best_start}" '
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
        f'<td class="badge badge-gap">ID</td>'
        f'<td class="ver">—</td><td class="role">gap</td>'
        f'<td class="label">ID — no evidence ({_esc(span)}, reason {_esc(entry["reason"])})</td>'
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
        parts.append(
            f'<div class="tl-lane" data-episode-id="{_esc(lane["episode_id"])}" '
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


def _embed_html(embed: EmbedPlan) -> str:
    link = f'<a class="setlink" rel="noopener" href="{_esc(embed.link_url)}">Open the set</a>'
    if embed.kind == "soundcloud":
        src = (
            "https://w.soundcloud.com/player/?url="
            + quote(embed.identifier, safe="")
            + "&show_comments=false&auto_play=false&hide_related=true"
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


_CSS = """
:root{color-scheme:light dark;--bg:#faf9f7;--fg:#1c1c1c;--muted:#666;--card:#fff;--line:#e3e1dd;
--solid:#1f7a4d;--extent:#cfe8db;--pi:#8a6bd6;--unresolved:#c9a227;--gap:#d98a8a;--accent:#2b6cb0;}
@media (prefers-color-scheme:dark){:root{--bg:#16171a;--fg:#e9e9ea;--muted:#a0a0a5;--card:#212228;
--line:#33343b;--extent:#274b3b;--solid:#3fbf7f;--pi:#a98bf0;--unresolved:#e0be4a;--gap:#c96a6a;
--accent:#6aa9e9;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1040px;margin:0 auto;padding:20px}
h1{font-size:20px;margin:0 0 2px}.sub{color:var(--muted);margin:0 0 16px;font-size:13px}
.player{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;
margin-bottom:16px}.yt{aspect-ratio:16/9;width:100%}.yt iframe{width:100%;height:100%}
.fallback{margin-top:8px;font-size:13px}.noembed{padding:12px}
.setlink{color:var(--accent)}
.controls{display:flex;gap:12px;align-items:center;margin:0 0 12px;flex-wrap:wrap}
.controls label{font-size:13px;color:var(--muted)}
.controls input{width:88px;padding:4px 6px;border:1px solid var(--line);border-radius:6px;
background:var(--card);color:var(--fg)}
.timeline{position:relative;height:46px;background:var(--card);border:1px solid var(--line);
border-radius:8px;overflow:hidden;margin-bottom:6px;cursor:pointer}
.tl-lane{position:absolute;top:0;height:100%;left:0;width:100%}
.tl-extent{position:absolute;top:16px;height:14px;background:var(--extent);border-radius:3px}
.tl-solid{position:absolute;top:16px;height:14px;background:var(--solid);border-radius:3px}
.tl-pi{position:absolute;top:16px;height:14px;background:var(--pi);opacity:.5;border-radius:3px}
.tl-unresolved{position:absolute;top:16px;height:14px;border-radius:3px;
background:repeating-linear-gradient(45deg,var(--unresolved),var(--unresolved) 4px,
transparent 4px,transparent 8px);opacity:.8}
.tl-gap{position:absolute;top:0;height:100%;background:var(--gap);opacity:.22}
.playhead{position:absolute;top:0;left:0;height:100%;width:2px;background:var(--accent);
z-index:4;pointer-events:none;box-shadow:0 0 4px var(--accent)}
.playhead[hidden]{display:none}
.playhead-time{position:absolute;top:1px;left:3px;font-size:10px;line-height:1.3;
font-variant-numeric:tabular-nums;background:var(--accent);color:#fff;border-radius:3px;
padding:0 3px;white-space:nowrap}
.tl-lane.current .tl-extent{box-shadow:0 0 0 2px var(--accent)}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin-bottom:16px}
.legend span::before{content:"";display:inline-block;width:12px;height:12px;border-radius:3px;
margin-right:5px;vertical-align:-2px}
.lg-solid::before{background:var(--solid)}.lg-extent::before{background:var(--extent)}
.lg-pi::before{background:var(--pi);opacity:.6}.lg-unresolved::before{
background:repeating-linear-gradient(45deg,var(--unresolved),var(--unresolved) 3px,
transparent 3px,transparent 6px)}.lg-gap::before{background:var(--gap);opacity:.4}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
border-radius:8px;overflow:hidden}
th,td{padding:7px 9px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
tr.track{cursor:pointer}tr.track:hover,tr.track:focus{background:rgba(43,108,176,.09);outline:none}
tr.track.current{background:rgba(43,108,176,.16);box-shadow:inset 3px 0 0 var(--accent)}
tr.gap{color:var(--muted)}
.time{font-variant-numeric:tabular-nums;white-space:nowrap}
.badge{font-weight:700;font-size:11px;white-space:nowrap}
.badge-verified{color:#1f7a4d}.badge-likely{color:#2b6cb0}.badge-possible{color:#b8860b}
.badge-unclear,.badge-gap{color:var(--muted)}
.ver{font-size:12px}.ver-verified{color:#1f7a4d}.ver-contested{color:#b23b3b}
.role{font-size:12px;color:var(--muted)}
.hint{font-size:10px;background:var(--accent);color:#fff;border-radius:4px;padding:1px 4px}
.alts{margin-top:3px;font-size:12px}
.alts>summary{cursor:pointer;color:var(--muted);list-style:none;display:inline-block}
.alts>summary::-webkit-details-marker{display:none}
.alts[open]>summary{color:var(--accent)}
.altlist{margin:4px 0 2px;padding-left:14px;color:var(--muted)}
.altlist li{margin:1px 0}
.alt-badge{font-weight:700;font-size:10px;margin-right:4px}
.acq{display:inline-block;margin:0 4px 2px 0;font-size:12px;text-decoration:none;
border:1px solid var(--line);border-radius:5px;padding:1px 6px;color:var(--accent)}
.acq.free{border-color:var(--solid);color:var(--solid)}.acq.buy,.acq.gate{border-color:#b8860b;
color:#b8860b}.acq.none{border:none;color:var(--muted)}
.rescan{font-size:11px;background:transparent;border:1px solid var(--line);border-radius:5px;
padding:2px 7px;color:var(--muted);cursor:pointer}.rescan:hover{color:var(--fg)}
.toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--card);
border:1px solid var(--line);border-radius:8px;padding:8px 14px;font-size:13px;opacity:0;
transition:opacity .2s;pointer-events:none}.toast.show{opacity:1}
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
) -> str:
    """Render the complete self-contained HTML page as a string.

    With ``collapse`` (the default), a contiguous run of competing near-duplicate matches of the
    same underlying track collapses to one display-track row (with a "▸ N other versions"
    disclosure); two appearances of the SAME exact track up to ``same_track_bridge_ms`` apart
    (``None`` → the grouping default) with no different confident track between them likewise stack
    into one row.  The timeline lane, the current-row highlight and the seek all use that display
    track's primary.  ``collapse=False`` restores the one-lane-per-episode view.
    """

    embed = plan_embed(source)
    entries = flatten_tracklist(
        episodes, identities, acquire, collapse=collapse, same_track_bridge_ms=same_track_bridge_ms
    )
    boundaries = _evidence_boundaries(list(episodes.episodes))

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
    for entry in entries:
        if entry["kind"] == "track":
            rows.append(_track_row_html(entry, source.platform))
        else:
            rows.append(_gap_row_html(entry))

    title = source.title or "DJ set"
    summary = (
        f"{len([e for e in entries if e['kind'] == 'track'])} tracks · "
        f"{len(gap_markers)} ID gaps · profile {episodes.certification.profile} · "
        f"generation {episodes.generation}"
    )

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

    body = f"""<main>
<h1>{_esc(title)}</h1>
<p class="sub">{_esc(summary)}</p>
<section class="player">{_embed_html(embed)}</section>
<div class="controls">
  <label for="leadin">Lead-in (ms)</label>
  <input id="leadin" type="number" min="0" step="500" value="{lead_in_ms}"
    aria-label="Seek lead-in in milliseconds">
  <span class="sub" id="dur">length {_esc(_format_time(duration_ms))}</span>
</div>
{_timeline_html(lanes, gap_markers)}
<div class="legend">
  <span class="lg-solid">evidence (proved)</span>
  <span class="lg-extent">episode extent</span>
  <span class="lg-pi">prediction interval</span>
  <span class="lg-unresolved">unresolved boundary</span>
  <span class="lg-gap">ID gap</span>
</div>
<table>
<thead><tr><th>Time</th><th>Badge</th><th>Version</th><th>Role</th><th>Track</th>
<th>Where to get it</th><th></th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
</main>
<script>
const CONFIG = {config_js};
const PLATFORM = CONFIG.platform;
const EPISODE_SPANS = {episode_spans_js};
let LEAD_IN_MS = CONFIG.leadInMs;
let CURRENT_POSITION_MS = null, PLAYER_DURATION_MS = 0;
{_SEEK_JS}
{_PLAYHEAD_JS}
let scWidget = null, ytPlayer = null, mcWidget = null, ytTimer = null;
function ready(fn){{ if(document.readyState!=='loading'){{fn();}}
  else{{document.addEventListener('DOMContentLoaded',fn);}} }}
function toast(msg){{ const t=document.getElementById('toast'); if(!t) return;
  t.textContent=msg; t.classList.add('show'); setTimeout(()=>t.classList.remove('show'),2200); }}
// Playhead readout -----------------------------------------------------------
function playheadDuration(){{
  return (CONFIG.durationMs > 0) ? CONFIG.durationMs : PLAYER_DURATION_MS;
}}
function updatePlayhead(positionMs){{
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
  highlightCurrent(positionMs);
}}
function highlightCurrent(positionMs){{
  let currentId = null;
  for(let i=0;i<EPISODE_SPANS.length;i++){{
    const span = EPISODE_SPANS[i];
    if(positionMs >= span.start && positionMs < span.end){{ currentId = span.id; break; }}
  }}
  document.querySelectorAll('tr.track.current, .tl-lane.current').forEach(function(el){{
    el.classList.remove('current'); }});
  if(currentId){{
    document.querySelectorAll('[data-episode-id="'+currentId+'"]').forEach(function(el){{
      if(el.classList.contains('track') || el.classList.contains('tl-lane')){{
        el.classList.add('current'); }}
    }});
  }}
}}
// Player bindings ------------------------------------------------------------
ready(function(){{
  try{{
    if(CONFIG.embedKind==='soundcloud' && window.SC){{
      scWidget = SC.Widget(document.getElementById('sc-player'));
      if(scWidget && scWidget.bind){{
        scWidget.bind(SC.Widget.Events.READY, function(){{
          scWidget.getDuration(function(d){{
            if(typeof d === 'number' && d > 0) PLAYER_DURATION_MS = d; }});
        }});
        scWidget.bind(SC.Widget.Events.PLAY_PROGRESS, function(e){{
          if(e && typeof e.currentPosition === 'number') updatePlayhead(e.currentPosition); }});
        scWidget.bind(SC.Widget.Events.SEEK, function(e){{
          if(e && typeof e.currentPosition === 'number') updatePlayhead(e.currentPosition); }});
      }}
    }} else if(CONFIG.embedKind==='mixcloud' && window.Mixcloud){{
      mcWidget = Mixcloud.PlayerWidget(document.getElementById('mc-player'));
      if(mcWidget && mcWidget.ready && mcWidget.events && mcWidget.events.progress){{
        mcWidget.ready.then(function(){{
          mcWidget.events.progress.on(function(position){{
            if(typeof position === 'number') updatePlayhead(position * 1000); }});
        }});
      }}
    }}
  }} catch(err){{ /* embedding disabled or API missing — timeline still works, no playhead */ }}
}});
function onYouTubeIframeAPIReady(){{
  if(CONFIG.embedKind!=='youtube') return;
  ytPlayer = new YT.Player('yt-player', {{videoId: CONFIG.identifier,
    playerVars: {{playsinline:1}},
    events: {{
      onReady: function(){{
        if(ytPlayer.getDuration){{
          const d = ytPlayer.getDuration(); if(d > 0) PLAYER_DURATION_MS = d * 1000; }}
      }},
      onStateChange: function(e){{
        if(e.data === YT.PlayerState.PLAYING){{ startYtPolling(); }} else {{ stopYtPolling(); }}
      }}
    }} }});
}}
function startYtPolling(){{
  if(ytTimer) return;
  ytTimer = setInterval(function(){{
    if(ytPlayer && ytPlayer.getCurrentTime) updatePlayhead(ytPlayer.getCurrentTime() * 1000);
  }}, 250);
}}
function stopYtPolling(){{ if(ytTimer){{ clearInterval(ytTimer); ytTimer = null; }} }}
function seekPlayerArg(arg){{
  if(CONFIG.embedKind==='soundcloud' && scWidget){{
    scWidget.seekTo(arg); scWidget.play(); return true; }}
  if(CONFIG.embedKind==='youtube' && ytPlayer && ytPlayer.seekTo){{
    ytPlayer.seekTo(arg, true); if(ytPlayer.playVideo) ytPlayer.playVideo(); return true; }}
  if(CONFIG.embedKind==='mixcloud' && mcWidget){{
    mcWidget.ready.then(function(){{ mcWidget.seek(arg); mcWidget.play(); }}); return true; }}
  return false;
}}
// Seek with the lead-in (a tracklist row) or exactly to a point (a timeline click); both reuse the
// shared seek arithmetic — a timeline click is a zero-lead-in seek to the clicked position.
function seekToMs(bestStartMs){{ return seekPlayerArg(seekArgument(bestStartMs, LEAD_IN_MS)); }}
function seekToPositionMs(positionMs){{ return seekPlayerArg(seekArgument(positionMs, 0)); }}
// Tracklist + timeline interactions ------------------------------------------
ready(function(){{
  const leadin = document.getElementById('leadin');
  leadin.addEventListener('change', function(){{
    const v = parseInt(leadin.value, 10);
    LEAD_IN_MS = (isNaN(v) || v<0) ? 0 : v;
  }});
  const timeline = document.querySelector('.timeline');
  if(timeline){{
    timeline.addEventListener('click', function(e){{
      const rect = timeline.getBoundingClientRect();
      if(rect.width <= 0) return;
      const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      const ms = Math.round(frac * playheadDuration());
      updatePlayhead(ms);
      if(!seekToPositionMs(ms)){{ toast('Player not ready — open the set link'); }}
    }});
  }}
  window.addEventListener('resize', function(){{
    if(CURRENT_POSITION_MS !== null) updatePlayhead(CURRENT_POSITION_MS); }});
  document.querySelectorAll('tr.track').forEach(function(row){{
    function go(){{
      const ms = parseInt(row.getAttribute('data-best-start-ms'), 10) || 0;
      if(!seekToMs(ms)){{ toast('Player not ready — open the set link'); }}
    }}
    row.addEventListener('click', function(e){{
      if(e.target.closest('a,button,details,summary')) return; go(); }});
    row.addEventListener('keydown', function(e){{
      if(e.key==='Enter' || e.key===' '){{ e.preventDefault(); go(); }} }});
  }});
  document.querySelectorAll('button.rescan').forEach(function(btn){{
    btn.addEventListener('click', function(e){{
      e.stopPropagation();
      requestRescan(btn.getAttribute('data-trigger'),
        parseInt(btn.getAttribute('data-start-ms'),10)||0,
        parseInt(btn.getAttribute('data-end-ms'),10)||0);
    }});
  }});
}});
function requestRescan(trigger, startMs, endMs){{
  fetch('/rescan', {{method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{media_key: CONFIG.mediaKey, trigger: trigger,
      start_ms: startMs, end_ms: endMs}})}})
    .then(function(r){{ return r.ok ? r.json() : Promise.reject(r.status); }})
    .then(function(){{ toast('Rescan queued — run `id-detector rescan`'); }})
    .catch(function(){{ toast('Rescan needs the local server (id-detector serve)'); }});
}}
</script>"""
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)} — id-detector</title>"
        f"<style>{_CSS}</style></head><body>{body}</body></html>\n"
    )


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

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
    return (
        f'<tr class="track" data-episode-id="{_esc(entry["episode_id"])}" '
        f'data-best-start-ms="{best_start}" tabindex="0" role="button" '
        f'aria-label="Seek to {_esc(_format_time(best_start))} — {label}">'
        f'<td class="time">{_esc(_format_time(best_start))}</td>'
        f'<td class="badge badge-{_esc(entry["badge"])}">{badge}</td>'
        f'<td class="ver ver-{version_status}">{version_status}</td>'
        f'<td class="role">{role}</td>'
        f'<td class="label">{label}{hint}</td>'
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
border-radius:8px;overflow:hidden;margin-bottom:6px}
.tl-lane{position:absolute;top:0;height:100%;left:0;width:100%}
.tl-extent{position:absolute;top:16px;height:14px;background:var(--extent);border-radius:3px}
.tl-solid{position:absolute;top:16px;height:14px;background:var(--solid);border-radius:3px}
.tl-pi{position:absolute;top:16px;height:14px;background:var(--pi);opacity:.5;border-radius:3px}
.tl-unresolved{position:absolute;top:16px;height:14px;border-radius:3px;
background:repeating-linear-gradient(45deg,var(--unresolved),var(--unresolved) 4px,
transparent 4px,transparent 8px);opacity:.8}
.tl-gap{position:absolute;top:0;height:100%;background:var(--gap);opacity:.22}
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
tr.gap{color:var(--muted)}
.time{font-variant-numeric:tabular-nums;white-space:nowrap}
.badge{font-weight:700;font-size:11px;white-space:nowrap}
.badge-verified{color:#1f7a4d}.badge-likely{color:#2b6cb0}.badge-possible{color:#b8860b}
.badge-unclear,.badge-gap{color:var(--muted)}
.ver{font-size:12px}.ver-verified{color:#1f7a4d}.ver-contested{color:#b23b3b}
.role{font-size:12px;color:var(--muted)}
.hint{font-size:10px;background:var(--accent);color:#fff;border-radius:4px;padding:1px 4px}
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
) -> str:
    """Render the complete self-contained HTML page as a string."""

    embed = plan_embed(source)
    entries = flatten_tracklist(episodes, identities, acquire)
    boundaries = _evidence_boundaries(list(episodes.episodes))
    lanes = [
        _timeline_lane(
            episode,
            " — ".join(_candidate_label(identities, episode.candidate_id)),
            boundaries,
            duration_ms,
        )
        for episode in episodes.episodes
    ]
    gap_markers = [_gap_marker(gap, duration_ms) for gap in episodes.gaps]

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
let LEAD_IN_MS = CONFIG.leadInMs;
{_SEEK_JS}
let scWidget = null, ytPlayer = null, mcWidget = null;
function ready(fn){{ if(document.readyState!=='loading'){{fn();}}
  else{{document.addEventListener('DOMContentLoaded',fn);}} }}
function toast(msg){{ const t=document.getElementById('toast'); if(!t) return;
  t.textContent=msg; t.classList.add('show'); setTimeout(()=>t.classList.remove('show'),2200); }}
// Player bindings ------------------------------------------------------------
ready(function(){{
  if(CONFIG.embedKind==='soundcloud' && window.SC){{
    scWidget = SC.Widget(document.getElementById('sc-player'));
  }} else if(CONFIG.embedKind==='mixcloud' && window.Mixcloud){{
    mcWidget = Mixcloud.PlayerWidget(document.getElementById('mc-player'));
  }}
}});
function onYouTubeIframeAPIReady(){{
  if(CONFIG.embedKind!=='youtube') return;
  ytPlayer = new YT.Player('yt-player', {{videoId: CONFIG.identifier,
    playerVars: {{playsinline:1}} }});
}}
function seekToMs(bestStartMs){{
  const arg = seekArgument(bestStartMs, LEAD_IN_MS);
  if(CONFIG.embedKind==='soundcloud' && scWidget){{
    scWidget.seekTo(arg); scWidget.play(); return true; }}
  if(CONFIG.embedKind==='youtube' && ytPlayer && ytPlayer.seekTo){{
    ytPlayer.seekTo(arg, true); if(ytPlayer.playVideo) ytPlayer.playVideo(); return true; }}
  if(CONFIG.embedKind==='mixcloud' && mcWidget){{
    mcWidget.ready.then(function(){{ mcWidget.seek(arg); mcWidget.play(); }}); return true; }}
  return false;
}}
// Tracklist interactions -----------------------------------------------------
ready(function(){{
  const leadin = document.getElementById('leadin');
  leadin.addEventListener('change', function(){{
    const v = parseInt(leadin.value, 10);
    LEAD_IN_MS = (isNaN(v) || v<0) ? 0 : v;
  }});
  document.querySelectorAll('tr.track').forEach(function(row){{
    function go(){{
      const ms = parseInt(row.getAttribute('data-best-start-ms'), 10) || 0;
      if(!seekToMs(ms)){{ toast('Player not ready — open the set link'); }}
    }}
    row.addEventListener('click', function(e){{ if(e.target.closest('a,button')) return; go(); }});
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
) -> Path:
    """Render and atomically write ``present/index.html`` with a completion sidecar."""

    html_text = render_page(
        source=source,
        episodes=episodes,
        identities=identities,
        duration_ms=duration_ms,
        acquire=acquire,
        lead_in_ms=lead_in_ms,
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

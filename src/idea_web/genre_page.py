"""The Genre page: find one song's specific genre, BPM and key (``/genre``).

The page is a server-rendered shell (form, notes, recent answers) plus the static script below,
which starts a check (``POST /genre/check``, CSRF-protected), polls its steps
(``GET /genre/check/<id>``) and draws the answer with DOM calls only (``textContent``), so nothing a
music site returns is ever parsed as HTML.  No inline script: the page's policy stays hash-only.
"""

from __future__ import annotations

import html
from importlib.resources import files
from typing import Any
from urllib.parse import quote

from id_detector.present.genre_css import GENRE_CSS

__all__ = ["GENRE_CSS", "GENRE_JS", "body_html", "home_entry_html"]


#: The page script, kept as a plain .js file beside this module (bundled into app.js).
GENRE_JS = chr(10) + files("idea_web").joinpath("genre.js").read_text("utf-8")

_SEARCH_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/>'
    '<path d="m16 16 4.5 4.5"/></svg>'
)
_ARROW = (
    '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8h9M8.5 4.5 12 8l-3.5 3.5" '
    'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
    'stroke-linejoin="round"/></svg>'
)
_STAGES = (("find", "Finding the track"), ("listen", "Listening"), ("lookup", "Looking up"),
           ("done", "Done"))  # fmt: skip


def notes_html(lookups: dict[str, bool], model_ready: bool) -> str:
    items = []
    if lookups.get("discogs") and lookups.get("lastfm"):
        items.append('<li class="on"><i></i>Discogs and Last.fm look-ups on</li>')
    elif lookups.get("discogs") or lookups.get("lastfm"):
        on, off_key = (
            ("Discogs", "LASTFM_API_KEY")
            if lookups.get("discogs")
            else ("Last.fm", "DISCOGS_API_TOKEN")
        )
        items.append(
            f'<li class="on"><i></i>{on} look-up on</li><li class="off"><i></i>'
            f"Add <code>{off_key}</code> to .env for the other</li>"
        )
    else:
        items.append(
            '<li class="off"><i></i>Look-ups off: answers come from listening alone. Add '
            "<code>DISCOGS_API_TOKEN</code> and <code>LASTFM_API_KEY</code> to the .env file to "
            "also check Discogs and Last.fm</li>"
        )
    if not model_ready:
        items.append("<li><i></i>The first check downloads the genre model (18 MB, one time)</li>")
    return '<ul class="g-notes">' + "".join(items) + "</ul>"


def _bpm_text(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else f"{number:.1f}"


def recent_html(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    links = []
    for item in items:
        bpm = item.get("bpm")
        bpm_text = f"<small>{html.escape(_bpm_text(bpm))} BPM</small>" if bpm else ""
        links.append(
            f'<li><a href="/genre?q={quote(str(item.get("input") or ""), safe="")}">'
            f"<span>{html.escape(str(item.get('title') or item.get('input') or ''))}</span>"
            f"<b>{html.escape(str(item.get('genre') or ''))}</b>{bpm_text}</a></li>"
        )
    return (
        '<section class="g-recent"><h2 class="sec">Recent checks</h2><ul>'
        + "".join(links)
        + "</ul></section>"
    )


def body_html(
    *,
    csrf_token: str,
    query: str,
    lookups: dict[str, bool],
    model_ready: bool,
    recent: list[dict[str, Any]],
) -> str:
    stages = "".join(
        f'<li class="stage" data-step="{key}"><i></i><span>{name}</span></li>'
        for key, name in _STAGES
    )
    return (
        '<main class="genre"><header class="hero-home compact">'
        '<p class="eyebrow">Genre finder · listens, then checks Discogs and Last.fm</p>'
        '<h1>What genre <span class="grad">is this track?</span></h1>'
        '<p class="lede">Paste a SoundCloud, YouTube or Spotify link, or type artist - title. '
        "ID&#39;er listens to three short bits of the song and names its specific style, with "
        "the BPM and key.</p>"
        f'<form class="genre-form" id="genre-form" action="/genre" method="get" '
        f'data-csrf="{html.escape(csrf_token)}" autocomplete="off" role="search">'
        f'<div class="urlbox"><span class="q-ic">{_SEARCH_ICON}</span>'
        '<label class="sr-only" for="gq">Song link, or artist and title</label>'
        f'<input id="gq" name="q" type="text" required spellcheck="false" maxlength="500" '
        f'value="{html.escape(query)}" '
        'placeholder="Artist - Title, or a SoundCloud / YouTube / Spotify link">'
        f'<button class="btn primary big" type="submit">Find genre{_ARROW}</button></div>'
        '<p class="form-error" id="genre-error" role="alert"></p></form>'
        f"{notes_html(lookups, model_ready)}</header>"
        '<section class="g-live" id="genre-live" hidden aria-live="polite" '
        'aria-label="Progress">'
        f'<ol class="stages">{stages}</ol>'
        '<div class="g-download" id="g-download" hidden>Downloading the genre model '
        '(one time)<div class="bar"><span></span></div><small></small></div>'
        '<p class="g-msg" id="g-msg"><b></b><span class="g-time" id="g-time"></span></p>'
        "</section>"
        '<section class="g-error" id="genre-fail" hidden role="alert"><b>Couldn&#39;t finish '
        "that check.</b><p></p></section>"
        '<section class="g-result" id="genre-result" hidden aria-live="polite"></section>'
        '<noscript><p class="lede">The genre finder needs JavaScript turned on.</p></noscript>'
        f"{recent_html(recent)}"
    )


def home_entry_html() -> str:
    """The home page's compact way in: a plain GET form to the Genre page."""

    return (
        '<form class="genre-mini" action="/genre" method="get" autocomplete="off" '
        'role="search"><label for="gm-q">What genre is a track?<small>Speed garage, '
        "bassline, DnB&hellip; plus BPM and key</small></label>"
        '<input id="gm-q" name="q" type="text" spellcheck="false" maxlength="500" '
        'placeholder="Artist - Title or a link">'
        '<button class="btn" type="submit">Find genre</button></form>'
    )

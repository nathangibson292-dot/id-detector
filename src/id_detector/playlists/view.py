"""Escaped HTML views of durable local snapshots."""

import html
from urllib.parse import quote, urlsplit

from . import store as operations

_CSS = """
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line2);padding:20px;border-radius:12px}
form{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}input{background:var(--card2);
color:var(--fg);border:1px solid var(--line2);border-radius:6px;padding:8px}
.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse}
td,th{text-align:left;padding:12px;border-bottom:1px solid var(--line2)}
.badge,.pill{font-size:11px}.pill{border:1px solid currentColor;border-radius:5px;padding:3px}
.badge-verified{color:var(--verified)}.badge-likely{color:var(--likely)}
.badge-possible{color:var(--possible)}.badge-unclear{color:var(--unclear)}
.acq{display:inline-block;padding:4px 7px;margin:3px;border:1px solid var(--line2);
border-radius:5px;font-size:12px}#message{color:var(--bad)}
"""

_VIEW_JS = """
document.addEventListener('DOMContentLoaded', () => {
  let token;
  document.querySelectorAll('form[data-action]').forEach(form => {
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const button = form.querySelector('button'); button.disabled = true;
      try{
        if(!token){
          const response = await fetch('/csrf');
          if(!response.ok) throw new Error('Could not load token');
          token = (await response.json()).token;
        }
        const response = await fetch('/playlists/' + form.dataset.action, {method:'POST',
          headers:{'X-CSRF-Token':token, 'Content-Type':'application/x-www-form-urlencoded'},
          body:new URLSearchParams(new FormData(form))});
        const result = await response.json();
        if(!response.ok || !result.ok) throw new Error(result.error || "Couldn't save");
        if(form.dataset.action === 'delete') location.assign('/playlists');
        else location.reload();
      }catch(error){ document.getElementById('message').textContent = error.message;
        button.disabled = false; }
    });
  });
});
"""


def _esc(value) -> str:
    return html.escape(str(value or ""))


def acquire_links_html(acquire: dict | None) -> str:
    if not isinstance(acquire, dict):
        return '<span class="acq none">—</span>'
    links = []

    def link(url, label, category):
        try:
            safe = isinstance(url, str) and urlsplit(url).scheme.lower() in {"http", "https"}
        except ValueError:
            safe = False
        if safe:
            links.append(
                f'<a class="acq {category}" target="_blank" rel="noopener" '
                f'href="{_esc(url)}">{_esc(label)}</a>'
            )

    sc = acquire.get("soundcloud") or {}
    permalink, purchase = sc.get("permalink_url"), sc.get("purchase_url")
    if acquire.get("free_download"):
        link(permalink, "SoundCloud · Free", "free")
    if acquire.get("gate"):
        link(permalink or purchase, "SoundCloud · Gate", "gate")
    buy_url, buy_label = permalink or purchase, "SoundCloud · Buy"
    for direct in acquire.get("direct") or []:
        if direct.get("kind") == "purchase":
            buy_url, buy_label = direct.get("url"), f"{direct.get('source', 'link')} · Buy"
            break
    if acquire.get("buy"):
        link(buy_url, buy_label, "buy")
    for direct in acquire.get("direct") or []:
        if direct.get("kind") == "stream":
            link(direct.get("url"), direct.get("source", "link"), "direct")
    for search in (acquire.get("search_links") or [])[:2]:
        link(search.get("url"), search.get("source", "search"), "search")
    return "".join(links) or '<span class="acq none">—</span>'


def _page(title: str, body: str) -> bytes:
    # present.__init__ imports page and server; defer theme loading until rendering.
    from id_detector.present.theme import head_html, topbar_html

    return (
        head_html(f"{title} — ID'er", _CSS)
        + "<body>"
        + topbar_html(back=True, new=True)
        + f"<main><h1>{_esc(title)}</h1>{body}"
        + '<p id="message" role="status"></p></main>'
        + f"<script>{_VIEW_JS}</script></body></html>"
    ).encode("utf-8")


def _form(action: str, label: str, fields: dict, *, name: str | None = None) -> str:
    inputs = "".join(
        f'<input type="hidden" name="{_esc(k)}" value="{_esc(v)}">' for k, v in fields.items()
    )
    if name is not None:
        inputs += (
            '<label>Playlist name <input name="name" required maxlength="80" '
            f'value="{_esc(name)}"></label>'
        )
    return (
        f'<form data-action="{action}">{inputs}'
        f'<button class="btn" type="submit">{label}</button></form>'
    )


def render_index(store: dict) -> bytes:
    playlists = operations.list_playlists(store)
    cards = "".join(
        f'<article class="card"><h2><a href="/playlists/p/{quote(p["id"], safe="")}">'
        f"{_esc(p['name'])}</a></h2><p>{p['count']} tracks</p>"
        + ('<span class="pill">Built-in</span>' if p["builtin"] else "")
        + "</article>"
        for p in playlists
    )
    empty = (
        "<p>No playlists yet — open a mix and tap ♥ on a track.</p>"
        if len(playlists) == 1 and not playlists[0]["count"]
        else ""
    )
    return _page(
        "Playlists",
        _form("create", "New playlist", {}, name="") + empty + f'<div class="cards">{cards}</div>',
    )


def render_playlist(store: dict, playlist_id: str) -> bytes | None:
    playlist = operations.get_playlist(store, playlist_id)
    if playlist is None:
        return None
    controls = ""
    if playlist_id != "likes":
        controls = _form("rename", "Rename", {"playlist_id": playlist_id}, name=playlist["name"])
        controls += _form("delete", "Delete playlist", {"playlist_id": playlist_id})
    rows = []
    for track in playlist["tracks"]:
        jump = ""
        if track.get("sources"):
            source = track["sources"][0]
            seconds = max(0, int(source.get("start_ms") or 0)) // 1000
            timestamp = f"{seconds // 60}:{seconds % 60:02d}"
            path = "/".join(
                quote(str(source.get(k) or ""), safe="") for k in ("source_key", "media_key")
            )
            fragment = quote(str(source.get("episode_id") or ""), safe="")
            jump = (
                f'<a href="/{path}/present/index.html#{fragment}">'
                f"{_esc(source.get('mix_title') or 'Original mix')} · {timestamp}</a>"
            )
        badge = track.get("badge") or "unclear"
        tier = badge if badge in {"verified", "likely", "possible", "unclear"} else "unclear"
        remove = _form(
            "remove", "Remove", {"playlist_id": playlist_id, "candidate_id": track["candidate_id"]}
        )
        rows.append(
            f"<tr><td>{_esc(track.get('artist'))} — {_esc(track.get('title'))}</td>"
            f'<td class="badge badge-{tier}"><span class="pill">{_esc(badge)}</span></td>'
            f"<td>{acquire_links_html(track.get('acquire'))}</td><td>{jump}</td>"
            f"<td>{remove}</td></tr>"
        )
    body = (
        '<div class="table-wrap"><table><thead><tr><th>Track</th><th>Confidence</th>'
        "<th>Where to get it</th><th>Jump back</th><th></th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
        if rows
        else "<p>No saved tracks yet.</p>"
    )
    return _page(playlist["name"], controls + body)


def render_not_found() -> bytes:
    return _page("Playlist not found", '<a href="/playlists">Back to playlists</a>')

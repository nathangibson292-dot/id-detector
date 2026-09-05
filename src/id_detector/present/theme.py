"""Shared look for every id-detector page: design tokens, the top bar, buttons, chips, toast.

Pure strings — no I/O — so both the static result page (offline, written to ``present/index.html``)
and the live server pages (home, new-mix, progress) share one visual system.  Dark by design: a
DJ tool lives in the dark, and one committed look is stronger than two half-committed ones.

Privacy: nothing here renders user data; the only text is brand chrome and navigation.
"""

from __future__ import annotations

import html

#: Browser-tab icon: four equaliser bars in the brand gradient (a data URI, so it works offline).
FAVICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E"
    "%3Cstop offset='0' stop-color='%23ff3d8a'/%3E%3Cstop offset='.55' stop-color='%238b5cf6'/%3E"
    "%3Cstop offset='1' stop-color='%2322d3ee'/%3E%3C/linearGradient%3E%3C/defs%3E"
    "%3Crect width='32' height='32' rx='8' fill='%230a0a0f'/%3E"
    "%3Crect x='6' y='14' width='4' height='10' rx='2' fill='url(%23g)'/%3E"
    "%3Crect x='12' y='7' width='4' height='17' rx='2' fill='url(%23g)'/%3E"
    "%3Crect x='18' y='11' width='4' height='13' rx='2' fill='url(%23g)'/%3E"
    "%3Crect x='24' y='16' width='4' height='8' rx='2' fill='url(%23g)'/%3E%3C/svg%3E"
)

PLATFORM_NAMES = {"soundcloud": "SoundCloud", "youtube": "YouTube", "mixcloud": "Mixcloud"}

#: Tokens plus the elements every page shares (top bar, buttons, chips, status pills, toast).
BASE_CSS = """
:root{color-scheme:dark;
--bg:#0a0a0f;--card:#13131b;--card2:#191924;--line:#ffffff12;--line2:#ffffff24;
--fg:#f3f3f8;--muted:#9494ab;--dim:#5f5f78;
--pink:#ff3d8a;--violet:#8b5cf6;--cyan:#22d3ee;--accent:#a78bfa;
--verified:#34d399;--likely:#38bdf8;--possible:#fbbf24;--unclear:#7d7d99;--gap:#fb7185;
--ok:#34d399;--bad:#fb7185;--warn:#fbbf24;
--grad:linear-gradient(135deg,#ff3d8a 0%,#8b5cf6 55%,#22d3ee 100%);
--mono:"Cascadia Mono",Consolas,ui-monospace,SFMono-Regular,Menlo,monospace;
--display:"Segoe UI Variable Display","Segoe UI",Inter,system-ui,sans-serif;
--text:"Segoe UI Variable Text","Segoe UI",Inter,system-ui,-apple-system,sans-serif}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 var(--text);
-webkit-font-smoothing:antialiased;min-height:100vh;position:relative;overflow-x:hidden}
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
background:radial-gradient(60% 45% at 8% -10%,rgba(139,92,246,.28),transparent 60%),
radial-gradient(45% 40% at 95% -5%,rgba(255,61,138,.18),transparent 60%),
radial-gradient(40% 30% at 50% 110%,rgba(34,211,238,.10),transparent 60%)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
button{font:inherit}
main{max-width:1120px;margin:0 auto;padding:12px 24px 64px;animation:rise .5s ease both}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.grad{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
/* top bar */
.topbar{position:sticky;top:0;z-index:20;backdrop-filter:blur(14px) saturate(140%);
-webkit-backdrop-filter:blur(14px) saturate(140%);background:rgba(10,10,15,.72);
border-bottom:1px solid var(--line)}
.topbar-in{max-width:1120px;margin:0 auto;padding:10px 24px;display:flex;align-items:center;
gap:16px;min-height:56px}
.brand{display:inline-flex;align-items:center;gap:10px;font:700 16px/1 var(--display);
letter-spacing:-.02em;color:var(--fg);white-space:nowrap}.brand:hover{text-decoration:none}
.brand .dot{background:var(--grad);-webkit-background-clip:text;background-clip:text;
color:transparent;font-weight:900}
.eq{display:inline-flex;align-items:flex-end;gap:2px;height:14px;width:16px}
.eq i{display:block;width:3px;height:4px;border-radius:2px;background:var(--grad);
transform-origin:bottom;transition:height .3s}
body.playing .eq i,.eq.live i{animation:eq .9s ease-in-out infinite}
.eq i:nth-child(2){animation-delay:.15s}.eq i:nth-child(3){animation-delay:.3s}
.eq i:nth-child(4){animation-delay:.45s}
@keyframes eq{0%,100%{height:4px}50%{height:14px}}
.nav{margin-left:auto;display:flex;gap:8px;align-items:center}
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 14px;border-radius:9px;
font:600 13px/1 var(--text);border:1px solid var(--line2);color:var(--fg);background:#ffffff0a;
cursor:pointer;white-space:nowrap;transition:transform .12s,background .12s,border-color .12s,
filter .12s}
.btn:hover{background:#ffffff14;text-decoration:none;transform:translateY(-1px)}
.btn.primary{border:none;background:var(--grad);color:#fff;
box-shadow:0 6px 20px -8px rgba(139,92,246,.9)}
.btn.primary:hover{filter:brightness(1.08)}
.btn.big{padding:13px 22px;font-size:15px;border-radius:12px}
.btn.danger{color:var(--bad);border-color:rgba(251,113,133,.4)}
.btn.danger:hover{background:rgba(251,113,133,.1)}
.btn:disabled{opacity:.5;cursor:default;transform:none}
/* chips + status */
.chip{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);
border:1px solid var(--line2);border-radius:999px;padding:4px 10px;background:#ffffff06;
font-variant-numeric:tabular-nums;white-space:nowrap}
.chip b{color:var(--fg);font-weight:600}
.pd{width:8px;height:8px;border-radius:50%;background:var(--dim);flex:none;display:inline-block}
.plat-soundcloud .pd{background:#ff5500}.plat-youtube .pd{background:#ff0033}
.plat-mixcloud .pd{background:#5000ff}.plat-file .pd{background:var(--cyan)}
.st{display:inline-flex;align-items:center;gap:6px;font:700 10px/1 var(--display);
letter-spacing:.12em;
text-transform:uppercase;padding:5px 8px;border-radius:6px;color:var(--sc);
background:color-mix(in srgb,var(--sc) 14%,transparent);
border:1px solid color-mix(in srgb,var(--sc) 35%,transparent);--sc:var(--muted)}
.st::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--sc)}
.st-running{--sc:var(--cyan)}.st-running::before{animation:blink 1s ease-in-out infinite}
.st-queued{--sc:var(--warn)}.st-succeeded{--sc:var(--ok)}.st-failed{--sc:var(--bad)}
.st-cancelled{--sc:var(--dim)}
@keyframes blink{50%{opacity:.25}}
kbd{font:600 10px/1 var(--mono);border:1px solid var(--line2);border-bottom-width:2px;
border-radius:5px;
padding:2px 5px;color:var(--muted);background:#ffffff08}
.toast{position:fixed;bottom:22px;left:50%;transform:translate(-50%,10px);background:#1d1d29;
border:1px solid var(--line2);border-radius:10px;padding:10px 16px;font-size:13px;opacity:0;
transition:opacity .2s,transform .2s;pointer-events:none;box-shadow:0 12px 40px -10px rgba(0,0,
0,.8);
z-index:30}
.toast.show{opacity:1;transform:translate(-50%,0)}
footer{margin-top:36px;color:var(--dim);font-size:12px;display:flex;gap:14px;flex-wrap:wrap;
align-items:center}
h2.sec{font:700 11px/1 var(--display);letter-spacing:.16em;text-transform:uppercase;
color:var(--dim);
margin:28px 0 12px}
@media (max-width:720px){main{padding:8px 14px 48px}.topbar-in{padding:8px 14px;gap:10px}
.btn.hide-sm{display:none}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""


def head_html(title: str, css: str, extra: str = "") -> str:
    """``<!doctype>`` + ``<head>``: viewport, favicon, any ``extra`` tags, and the CSS inlined."""

    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        f'<link rel="icon" href="{FAVICON}">{extra}'
        f"<style>{BASE_CSS}{css}</style></head>"
    )


def topbar_html(*, back: bool = False, new: bool = True, middle: str = "") -> str:
    """The sticky brand bar.  ``middle`` is an optional centre slot (the result page's NOW pill)."""

    nav = ""
    if back:
        nav += '<a class="btn hide-sm" href="/">Your mixes</a>'
    if new:
        nav += '<a class="btn primary" href="/new">+ New mix</a>'
    return (
        '<nav class="topbar"><div class="topbar-in">'
        '<a class="brand" href="/"><span class="eq"><i></i><i></i><i></i><i></i></span>'
        'id<span class="dot">·</span>detector</a>'
        f'{middle}<span class="nav">{nav}</span></div></nav>'
    )


def platform_chip(platform: str) -> str:
    """A small platform chip with its brand-coloured dot (``file`` for a local path)."""

    key = platform or "file"
    name = PLATFORM_NAMES.get(key, key)
    return (
        f'<span class="chip plat-{html.escape(key)}"><span class="pd"></span>'
        f"{html.escape(name)}</span>"
    )

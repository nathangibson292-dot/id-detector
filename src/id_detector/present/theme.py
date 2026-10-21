"""Shared look for every ID'er page: design tokens, the top bar, buttons, chips, toast.

Pure strings — no I/O — so both the static result page (offline, written to ``present/index.html``)
and the live server pages (home, progress, playlists) share one visual system.

The system, in one place:

* **Colour** — a dark studio palette (``:root``) and a matching light one.  The light palette is
  applied by ``prefers-color-scheme`` unless the owner picked a theme with the top-bar toggle, which
  sets ``data-theme`` on ``<html>`` (remembered in ``localStorage``; every access is wrapped).
  Text tokens clear WCAG AA on every surface in both themes (``test_phase3a_honesty`` checks).
* **Space** — a 4 px scale ``--s1`` … ``--s9``; **radii** ``--r1`` … ``--r4``; **shadows**
  ``--sh1`` … ``--sh3``; **motion** ``--t1`` (150 ms) … ``--t3`` (250 ms) on one easing curve, and
  every animation and transition is switched off under ``prefers-reduced-motion``.
* **Type** — local system fonts only (Inter if installed, else Segoe UI Variable, else the
  platform UI face): nothing is fetched, so every page works offline.  Times are tabular.

Privacy: nothing here renders user data; the only text is brand chrome and navigation.
"""

from __future__ import annotations

import hashlib
import html

#: Browser-tab icon, matching the top-bar logo: white equaliser bars rising left-to-right on the
#: brand-gradient tile (a static data URI, so it works offline).
FAVICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E"
    "%3Cstop offset='0' stop-color='%23ff3d8a'/%3E%3Cstop offset='.55' stop-color='%238b5cf6'/%3E"
    "%3Cstop offset='1' stop-color='%2322d3ee'/%3E%3C/linearGradient%3E%3C/defs%3E"
    "%3Crect width='32' height='32' rx='8' fill='url(%23g)'/%3E"
    "%3Crect x='6' y='16' width='4' height='8' rx='2' fill='%23fff'/%3E"
    "%3Crect x='12' y='12' width='4' height='12' rx='2' fill='%23fff'/%3E"
    "%3Crect x='18' y='8' width='4' height='16' rx='2' fill='%23fff'/%3E"
    "%3Crect x='24' y='4' width='4' height='20' rx='2' fill='%23fff'/%3E%3C/svg%3E"
)

PLATFORM_NAMES = {"soundcloud": "SoundCloud", "youtube": "YouTube", "mixcloud": "Mixcloud"}

#: Where the owner's theme choice is remembered (per browser; a convenience, never relied on).
THEME_STORAGE_KEY = "ider-theme"

#: Applies a remembered theme before first paint and wires the top-bar toggle.  Shipped inline in
#: the head of an offline result page, and as a tiny static script (:data:`THEME_SRC`) in the head
#: of every live page, whose policy forbids inline script.
THEME_JS = """
(function(){
  var KEY = '__KEY__', root = document.documentElement, stored = null;
  try { stored = window.localStorage.getItem(KEY); } catch (e) { stored = null; }
  if (stored === 'light' || stored === 'dark') root.setAttribute('data-theme', stored);
  function current(){
    var chosen = root.getAttribute('data-theme');
    if (chosen === 'light' || chosen === 'dark') return chosen;
    try {
      if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches)
        return 'light';
    } catch (e) {}
    return 'dark';
  }
  function paint(button){
    var mode = current();
    button.setAttribute('data-mode', mode);
    button.setAttribute('aria-label', mode === 'light' ? 'Switch to dark theme'
      : 'Switch to light theme');
    button.title = button.getAttribute('aria-label');
  }
  function wire(){
    var button = document.getElementById('theme-toggle'); if (!button) return;
    button.hidden = false; paint(button);
    button.addEventListener('click', function(){
      var next = current() === 'light' ? 'dark' : 'light';
      root.setAttribute('data-theme', next);
      try { window.localStorage.setItem(KEY, next); } catch (e) {}
      paint(button);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
  else wire();
})();
""".replace("__KEY__", THEME_STORAGE_KEY)
#: The versioned URL the live server serves :data:`THEME_JS` under (immutable, like app.*.js).
THEME_SRC = f"/static/theme.{hashlib.sha256(THEME_JS.encode('utf-8')).hexdigest()[:12]}.js"

#: The light palette.  It lives in each page sheet *after* that sheet's dark rules (the AA test
#: reads the dark tokens before ``@media (prefers-color-scheme:light)`` and the light ones after),
#: and applies either from the system preference (unless the owner chose dark) or from the toggle.
_LIGHT_TOKENS = (
    "color-scheme:light;"
    "--bg:#f6f6f9;--bg2:#ececf2;--card:#ffffff;--card2:#f1f1f6;--raise:#ffffff;"
    "--line:#14142414;--line2:#14142426;--line3:#14142440;"
    "--fg:#15151d;--muted:#4b4b5e;--dim:#5a5a6e;"
    "--pink:#be185d;--violet:#6d28d9;--cyan:#0e7490;--accent:#6d28d9;--on-accent:#ffffff;"
    "--verified:#047857;--likely:#0369a1;--possible:#92400e;--unclear:#52525b;--gap:#be123c;"
    "--ok:#047857;--bad:#be123c;--warn:#92400e;"
    "--hover:#1414240a;--hover2:#14142414;--sunken:#ececf3;--track:#1414241a;"
    "--glass:#f6f6f9d9;--accent-soft:#6d28d91a;--accent-line:#6d28d966;"
    "--glow1:#8b5cf61f;--glow2:#ff3d8a12;--glow3:#22d3ee10;"
    "--sh1:0 1px 2px #1414240f,0 1px 1px #1414240a;"
    "--sh2:0 10px 30px -12px #14142430,0 2px 6px -2px #1414241a;"
    "--sh3:0 24px 60px -24px #14142445;"
)
LIGHT_CSS = (
    "@media (prefers-color-scheme:light){:root:not([data-theme=dark]){"
    + _LIGHT_TOKENS
    + "}}:root[data-theme=light]{"
    + _LIGHT_TOKENS
    + "}"
)

#: Tokens plus the elements every page shares (top bar, buttons, chips, status pills, toast).
BASE_CSS = """
:root{color-scheme:dark;
--bg:#08080d;--bg2:#0d0d14;--card:#12121a;--card2:#1a1a25;--raise:#20202c;
--line:#ffffff0f;--line2:#ffffff1c;--line3:#ffffff30;
--fg:#f4f4f8;--muted:#a3a3b8;--dim:#8f8fa6;
--pink:#ff4d94;--violet:#8b5cf6;--cyan:#22d3ee;--accent:#b197fc;--on-accent:#0a0a0f;
--verified:#34d399;--likely:#4cc3fa;--possible:#fbbf24;--unclear:#a3a3bd;--gap:#fb7185;
--ok:#34d399;--bad:#fb7185;--warn:#fbbf24;
--hover:#ffffff0a;--hover2:#ffffff14;--sunken:#0b0b11;--track:#ffffff12;
--glass:#08080dc7;--accent-soft:#8b5cf624;--accent-line:#b197fc73;
--glow1:#8b5cf638;--glow2:#ff3d8a1f;--glow3:#22d3ee14;
--grad:linear-gradient(120deg,#ff4d94 0%,#8b5cf6 52%,#22d3ee 100%);
--sh1:0 1px 0 #ffffff08 inset,0 1px 2px #00000066;
--sh2:0 1px 0 #ffffff0a inset,0 18px 40px -22px #000000cc;
--sh3:0 30px 80px -30px #000000e6;
--s1:4px;--s2:8px;--s3:12px;--s4:16px;--s5:20px;--s6:24px;--s7:32px;--s8:48px;--s9:64px;
--r1:8px;--r2:12px;--r3:16px;--r4:22px;--pill:999px;
--t1:150ms;--t2:200ms;--t3:250ms;--ease:cubic-bezier(.2,.8,.2,1);
--mono:"Cascadia Mono","JetBrains Mono",Consolas,ui-monospace,SFMono-Regular,Menlo,monospace;
--display:Inter,"Segoe UI Variable Display","Segoe UI",system-ui,-apple-system,Roboto,sans-serif;
--text:Inter,"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,Roboto,sans-serif;
--wrap:1160px}
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%;background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 var(--text);
font-feature-settings:"cv11","ss01";-webkit-font-smoothing:antialiased;
-moz-osx-font-smoothing:grayscale;min-height:100vh;position:relative;overflow-x:hidden;
transition:background-color var(--t3) var(--ease),color var(--t3) var(--ease)}
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
background:radial-gradient(55% 42% at 6% -8%,var(--glow1),transparent 62%),
radial-gradient(40% 36% at 98% -6%,var(--glow2),transparent 60%),
radial-gradient(50% 30% at 50% 108%,var(--glow3),transparent 60%)}
::selection{background:var(--accent-soft);color:var(--fg)}
a{color:var(--accent);text-decoration:none;text-underline-offset:3px}
a:hover{text-decoration:underline}
button,input,select,textarea{font:inherit;color:inherit}
img,svg{display:block}
main{max-width:var(--wrap);margin:0 auto;padding:var(--s3) var(--s6) var(--s9)}
main>h1{font:750 clamp(28px,4vw,40px)/1.1 var(--display);letter-spacing:-.03em;
margin:var(--s7) 0 var(--s5)}
h1,h2,h3{text-wrap:balance}
@keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@keyframes fade{from{opacity:0}to{opacity:1}}
.grad{background:var(--grad);-webkit-background-clip:text;background-clip:text;
color:transparent}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
clip:rect(0,0,0,0);white-space:nowrap;border:0}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button:focus-visible,a:focus-visible,summary:focus-visible,input:focus-visible,
textarea:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
/* top bar */
.topbar{position:sticky;top:0;z-index:40;background:var(--glass);
backdrop-filter:blur(16px) saturate(160%);-webkit-backdrop-filter:blur(16px) saturate(160%);
border-bottom:1px solid var(--line)}
.topbar-in{max-width:var(--wrap);margin:0 auto;padding:0 var(--s6);display:flex;
align-items:center;gap:var(--s4);height:60px}
.brand{display:inline-flex;align-items:center;gap:10px;color:var(--fg);white-space:nowrap;
border-radius:var(--r1)}
.brand:hover{text-decoration:none}
.logo{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;
border-radius:9px;background:var(--grad);box-shadow:0 6px 18px -8px #8b5cf6e6,
0 0 0 1px #ffffff1f inset}
.logo .eq{height:14px;width:16px}.logo .eq i{background:#fff}
.wm{font:800 19px/1 var(--display);letter-spacing:-.035em;color:var(--fg)}
.wm span{font-weight:500;color:var(--muted)}
.eq{display:inline-flex;align-items:flex-end;gap:2px;height:14px;width:16px}
.eq i{display:block;width:3px;height:5px;border-radius:2px;background:var(--grad);
transform-origin:bottom}
body.analysing .logo .eq i,.eq.live i{animation:eq .9s ease-in-out infinite}
.eq i:nth-child(1){height:5px}
.eq i:nth-child(2){height:8px;animation-delay:.15s}
.eq i:nth-child(3){height:11px;animation-delay:.3s}
.eq i:nth-child(4){height:14px;animation-delay:.45s}
@keyframes eq{0%,100%{height:4px}50%{height:14px}}
.nav{margin-left:auto;display:flex;gap:var(--s2);align-items:center}
/* buttons */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;height:36px;
padding:0 14px;border-radius:10px;font:600 13px/1 var(--text);letter-spacing:-.005em;
border:1px solid var(--line2);color:var(--fg);background:var(--hover);cursor:pointer;
white-space:nowrap;text-decoration:none;
transition:background-color var(--t1) var(--ease),border-color var(--t1) var(--ease),
transform var(--t1) var(--ease),box-shadow var(--t2) var(--ease),filter var(--t1) var(--ease)}
.btn:hover{background:var(--hover2);border-color:var(--line3);text-decoration:none}
.btn:active{transform:translateY(1px)}
.btn.ghost{background:transparent;border-color:transparent;color:var(--muted)}
.btn.ghost:hover{color:var(--fg);background:var(--hover)}
.btn.primary{border:0;background:var(--grad);color:var(--on-accent);
box-shadow:0 8px 24px -10px #8b5cf6f2,0 0 0 1px #ffffff26 inset}
.btn.primary:hover{filter:brightness(1.07) saturate(1.05);
box-shadow:0 12px 30px -10px #8b5cf6f2,0 0 0 1px #ffffff33 inset}
.btn.big{height:48px;padding:0 24px;font-size:15px;border-radius:var(--r2)}
.btn.icon{width:36px;padding:0}
.btn.icon svg{width:18px;height:18px}
.btn.danger{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 40%,transparent)}
.btn.danger:hover{background:color-mix(in srgb,var(--bad) 10%,transparent)}
.btn:disabled{opacity:.5;cursor:default;transform:none}
.btn[hidden]{display:none}
.theme-toggle .sun{display:none}.theme-toggle[data-mode=light] .sun{display:block}
.theme-toggle[data-mode=light] .moon{display:none}
/* chips + status */
.chip{display:inline-flex;align-items:center;gap:6px;height:26px;font-size:12px;
color:var(--muted);border:1px solid var(--line2);border-radius:var(--pill);padding:0 10px;
background:var(--hover);font-variant-numeric:tabular-nums;white-space:nowrap}
.chip b{color:var(--fg);font-weight:600}
.pd{width:7px;height:7px;border-radius:50%;background:var(--dim);flex:none;display:inline-block}
.plat-soundcloud .pd{background:#ff5500}.plat-youtube .pd{background:#ff0033}
.plat-mixcloud .pd{background:#5000ff}.plat-file .pd{background:var(--cyan)}
.st{display:inline-flex;align-items:center;gap:6px;height:24px;
font:700 10.5px/1 var(--display);letter-spacing:.1em;text-transform:uppercase;padding:0 9px;
border-radius:var(--pill);color:color-mix(in srgb,var(--sc) 78%,var(--fg));
background:color-mix(in srgb,var(--sc) 10%,transparent);
border:1px solid color-mix(in srgb,var(--sc) 32%,transparent);--sc:var(--muted)}
.st::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--sc)}
.st-running{--sc:var(--cyan)}.st-running::before{animation:blink 1.2s ease-in-out infinite}
.st-queued{--sc:var(--warn)}.st-succeeded{--sc:var(--ok)}.st-failed{--sc:var(--bad)}
.st-cancelled{--sc:var(--dim)}.st-waiting{--sc:var(--warn)}
@keyframes blink{50%{opacity:.25}}
kbd{display:inline-block;min-width:20px;text-align:center;font:600 10.5px/1 var(--mono);
border:1px solid var(--line2);border-bottom-width:2px;border-radius:6px;padding:3px 5px;
color:var(--muted);background:var(--hover)}
.toast{position:fixed;bottom:24px;left:50%;transform:translate(-50%,12px);
background:var(--raise);color:var(--fg);border:1px solid var(--line2);border-radius:var(--r2);
padding:11px 16px;font-size:13px;font-weight:500;opacity:0;box-shadow:var(--sh3);
transition:opacity var(--t2) var(--ease),transform var(--t2) var(--ease);pointer-events:none;
z-index:60;max-width:calc(100vw - 32px)}
.toast.show{opacity:1;transform:translate(-50%,0)}
footer{margin-top:var(--s8);padding-top:var(--s5);border-top:1px solid var(--line);
color:var(--dim);font-size:12px;display:flex;gap:var(--s2) var(--s4);flex-wrap:wrap;
align-items:center;justify-content:space-between}
h2.sec{display:flex;align-items:center;gap:var(--s3);font:700 11px/1 var(--display);
letter-spacing:.16em;text-transform:uppercase;color:var(--dim);margin:var(--s8) 0 var(--s4)}
h2.sec::after{content:"";flex:1;height:1px;background:var(--line)}
@media (max-width:720px){main{padding:var(--s2) var(--s4) var(--s8)}
.topbar-in{padding:0 var(--s4);gap:var(--s2);height:56px}.btn.hide-sm{display:none}
.nav{gap:6px}.nav .btn{padding:0 11px}.nav .btn.icon{padding:0}}
@media (max-width:400px){.nav .btn.ghost.pl{display:none}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;
transition:none!important;scroll-behavior:auto!important}}
"""

_SUN = (
    '<svg class="sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4.2"/>'
    '<path d="M12 2.5v2.2M12 19.3v2.2M4.6 4.6l1.6 1.6M17.8 17.8l1.6 1.6M2.5 12h2.2M19.3 12h2.2'
    'M4.6 19.4l1.6-1.6M17.8 6.2l1.6-1.6"/></svg>'
)
_MOON = (
    '<svg class="moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linejoin="round" aria-hidden="true">'
    '<path d="M20.5 14.2A8.5 8.5 0 0 1 9.8 3.5a8.5 8.5 0 1 0 10.7 10.7z"/></svg>'
)
#: The theme toggle: hidden until the theme script runs, so a page without it shows no dead button.
THEME_TOGGLE = (
    '<button class="btn icon ghost theme-toggle" id="theme-toggle" type="button" hidden '
    f'aria-label="Switch theme">{_SUN}{_MOON}</button>'
)


def head_html(
    title: str,
    css: str,
    extra: str = "",
    *,
    theme_script: str = "src",
    inline_script: str = "",
) -> str:
    """``<!doctype>`` + ``<head>``: viewport, favicon, any ``extra`` tags, and the CSS inlined.

    The light palette is appended unless the page sheet already carries it.  ``theme_script`` says
    how the remembered theme is applied before first paint: ``"src"`` links the live server's
    static script, ``"none"`` leaves it to the page (the offline result page inlines it, as part
    of ``inline_script``, which is emitted after the stylesheet).
    """

    sheet = css if "@media (prefers-color-scheme:light)" in css else css + LIGHT_CSS
    script = f'<script src="{THEME_SRC}"></script>' if theme_script == "src" else ""
    if inline_script:
        script += f"<script>{inline_script}</script>"
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="dark light">'
        f"<title>{html.escape(title)}</title>"
        f'<link rel="icon" href="{FAVICON}">{extra}'
        f"<style>{BASE_CSS}{sheet}</style>{script}</head>"
    )


def topbar_html(*, back: bool = False, new: bool = True, middle: str = "") -> str:
    """The sticky brand bar.  ``middle`` is an optional centre slot (the result page's NOW pill)."""

    nav = '<a class="btn ghost pl" href="/playlists">Playlists</a>'
    if back:
        nav += '<a class="btn ghost hide-sm" href="/">Your mixes</a>'
    nav += THEME_TOGGLE
    if new:
        nav += '<a class="btn primary" href="/new">+ New mix</a>'
    return (
        '<nav class="topbar"><div class="topbar-in">'
        '<a class="brand" href="/" aria-label="ID\'er home">'
        '<span class="logo"><span class="eq"><i></i><i></i><i></i><i></i></span></span>'
        '<span class="wm"><b>ID</b><span>&#39;er</span></span></a>'
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

"""The Genre page's styles (``/genre`` and the home page's compact entry).

Part of the live pages' one stylesheet: :data:`id_detector.present.server._APP_CSS` includes it
before the light palette.  Tokens only, so it follows both themes.
"""

from __future__ import annotations

GENRE_CSS = """
.genre .hero-home{padding-bottom:var(--s5)}
.genre .hero-home h1{max-width:16ch}
.genre-form{max-width:760px}
.genre-form .urlbox .q-ic{display:inline-grid;place-items:center;width:36px;height:36px;
border-radius:10px;background:var(--hover);color:var(--muted);flex:none}
.genre-form .urlbox .q-ic svg{width:17px;height:17px}
.g-notes{display:flex;flex-wrap:wrap;gap:var(--s2);margin:var(--s4) 0 0;padding:0;list-style:none}
.g-notes li{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;color:var(--muted);
border:1px solid var(--line2);border-radius:var(--pill);padding:5px 11px;background:var(--hover)}
.g-notes li i{width:7px;height:7px;border-radius:50%;background:var(--dim);flex:none}
.g-notes li.on i{background:var(--ok)}.g-notes li.off i{background:var(--warn)}
.g-notes code{font:600 11.5px/1 var(--mono);color:var(--fg)}
.g-live{margin:var(--s6) 0 0;padding:var(--s5) var(--s6);background:var(--card);
border:1px solid var(--line2);border-radius:var(--r3);box-shadow:var(--sh2);
animation:rise var(--t3) var(--ease) both}
.g-live[hidden],.g-result[hidden],.g-download[hidden]{display:none}
.g-live .stages{margin:0}
.g-msg{margin:var(--s4) 0 0;color:var(--muted);font-size:13px;display:flex;gap:var(--s3);
justify-content:space-between;flex-wrap:wrap}
.g-msg b{color:var(--fg);font-weight:600}
.g-msg .g-time{font-variant-numeric:tabular-nums;color:var(--dim)}
.g-download{margin-top:var(--s4);font-size:13px;color:var(--muted)}
.g-download .bar{height:6px;border-radius:4px;background:var(--track);overflow:hidden;
margin-top:8px}
.g-download .bar span{display:block;height:100%;width:0;background:var(--grad);
transition:width var(--t2) var(--ease)}
.g-result{margin-top:var(--s6);animation:rise var(--t3) var(--ease) both}
.g-card{background:var(--card);border:1px solid var(--line2);border-radius:var(--r4);
box-shadow:var(--sh2);overflow:hidden}
.g-track{display:grid;grid-template-columns:88px 1fr;gap:var(--s5);align-items:center;
padding:var(--s5) var(--s6);border-bottom:1px solid var(--line)}
.g-art{width:88px;height:88px;border-radius:var(--r2);object-fit:cover;background:var(--grad);
box-shadow:var(--sh1)}
.g-art.ph{display:grid;place-items:center}
.g-art.ph .eq{height:28px;width:32px;gap:4px}.g-art.ph .eq i{width:5px;background:#fff}
.g-track h2{margin:4px 0 6px;font:750 20px/1.2 var(--display);letter-spacing:-.02em;
overflow-wrap:anywhere}
.g-track .eyebrow{margin:0}
.g-by{margin:0;color:var(--muted);font-size:13px;display:flex;flex-wrap:wrap;gap:6px 10px;
align-items:center}
.g-by a{font-weight:600}
.g-pick{grid-column:1/-1}
.g-pick summary{cursor:pointer;color:var(--muted);font-size:13px;font-weight:600;
display:inline-flex;align-items:center;gap:6px;border-radius:var(--r1);padding:2px 0}
.g-pick summary:hover{color:var(--fg)}
.g-pick summary::-webkit-details-marker{display:none}
.g-pick summary::before{content:"";width:6px;height:6px;border-right:2px solid currentColor;
border-bottom:2px solid currentColor;transform:rotate(-45deg);margin-right:4px;
transition:transform var(--t1) var(--ease)}
.g-pick[open] summary::before{transform:rotate(45deg)}
.g-pick summary{list-style:none}
.g-pick ul{list-style:none;margin:var(--s3) 0 0;padding:0;display:grid;gap:6px}
.g-pick button{width:100%;display:grid;grid-template-columns:1fr auto;gap:2px 12px;
text-align:left;padding:10px 12px;border-radius:var(--r2);border:1px solid var(--line2);
background:var(--hover);cursor:pointer;min-height:44px;color:var(--fg)}
.g-pick button:hover{border-color:var(--accent-line);background:var(--accent-soft)}
.g-pick button b{font-weight:600;overflow-wrap:anywhere}
.g-pick button small{color:var(--muted);grid-column:1}
.g-pick button span{grid-row:1/3;grid-column:2;align-self:center;color:var(--dim);
font-size:12px;font-variant-numeric:tabular-nums}
.g-pick button[aria-current=true]{border-color:var(--accent);background:var(--accent-soft)}
.g-body{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:0}
.g-answer{padding:var(--s6);border-right:1px solid var(--line)}
.g-kicker{display:flex;align-items:center;gap:var(--s2);flex-wrap:wrap;margin:0 0 var(--s3)}
.g-top{margin:0;font:800 clamp(34px,5vw,52px)/1 var(--display);letter-spacing:-.04em;
overflow-wrap:anywhere}
.g-top .grad{padding-right:.05em}
.g-top small{font:700 .42em/1 var(--display);color:var(--muted);letter-spacing:-.01em;
margin-left:.35em;font-variant-numeric:tabular-nums;white-space:nowrap}
.g-summary{margin:var(--s4) 0 var(--s5);color:var(--muted);font-size:14.5px;max-width:52ch}
.g-agree{--sc:var(--ok)}.g-partial{--sc:var(--likely)}.g-disagree{--sc:var(--warn)}
.g-heard-only{--sc:var(--muted)}
.g-bars{list-style:none;margin:0;padding:0;display:grid;gap:var(--s4)}
.g-bar-h{display:flex;align-items:baseline;justify-content:space-between;gap:var(--s3)}
.g-bar-h b{font:650 15px/1.2 var(--display);letter-spacing:-.01em}
.g-bar-h span{font:700 14px/1 var(--display);font-variant-numeric:tabular-nums}
.g-track-bar{height:8px;border-radius:5px;background:var(--track);margin:7px 0 7px;
overflow:hidden}
.g-track-bar i{display:block;height:100%;border-radius:5px;background:var(--grad)}
.g-bars li:not(:first-child) .g-track-bar i{background:var(--accent);opacity:.7}
.g-srcs{display:flex;gap:6px;flex-wrap:wrap}
.g-src{display:inline-flex;align-items:center;gap:5px;height:22px;padding:0 8px;
border-radius:var(--pill);font:600 11px/1 var(--text);color:var(--muted);
border:1px solid var(--line2)}
.g-src::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--dim)}
.g-src.heard::before{background:var(--cyan)}.g-src.discogs::before{background:var(--pink)}
.g-src.lastfm::before{background:var(--possible)}
.g-side{padding:var(--s6);display:flex;flex-direction:column;gap:var(--s4)}
.g-tile{border:1px solid var(--line2);border-radius:var(--r3);padding:var(--s4) var(--s5);
background:var(--card2)}
.g-tile .k{font:700 10.5px/1 var(--display);letter-spacing:.14em;text-transform:uppercase;
color:var(--dim);margin:0 0 8px}
.g-tile .v{font:800 34px/1 var(--display);letter-spacing:-.03em;margin:0;
font-variant-numeric:tabular-nums;display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.g-tile .v small{font:600 13px/1 var(--text);color:var(--muted);letter-spacing:0}
.g-cam{display:inline-flex;align-items:center;height:26px;padding:0 10px;border-radius:8px;
font:800 14px/1 var(--display);color:var(--on-accent);background:var(--grad);
align-self:center}
.g-tile .note{margin:8px 0 0;color:var(--muted);font-size:12.5px}
.g-ev{list-style:none;margin:0;padding:0;display:grid;gap:8px;font-size:13px}
.g-ev li{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;align-items:baseline}
.g-ev li b{font-weight:650}
.g-ev li span{color:var(--muted);overflow-wrap:anywhere}
.g-ev li.off span{color:var(--dim)}
.g-why{border-top:1px solid var(--line)}
.g-why>summary{cursor:pointer;padding:var(--s4) var(--s6);font-weight:600;color:var(--muted);
list-style:none;display:flex;justify-content:space-between;align-items:center;min-height:48px}
.g-why>summary::-webkit-details-marker{display:none}
.g-why>summary::after{content:"+";font:600 18px/1 var(--display);color:var(--dim)}
.g-why[open]>summary::after{content:"\\2212"}
.g-why>summary:hover{color:var(--fg);background:var(--hover)}
.g-why-in{padding:0 var(--s6) var(--s6);display:grid;grid-template-columns:repeat(3,minmax(0,1fr));
gap:var(--s5)}
.g-why h4{margin:0 0 var(--s3);font:700 11px/1 var(--display);letter-spacing:.14em;
text-transform:uppercase;color:var(--dim)}
.g-why p{margin:0 0 var(--s2);color:var(--muted);font-size:13px}
.g-why p b{color:var(--fg);font-weight:600}
.g-model{list-style:none;margin:0;padding:0;display:grid;gap:5px;font-size:12.5px}
.g-model li{display:grid;grid-template-columns:minmax(0,1fr) 72px 34px;gap:8px;align-items:center}
.g-model li span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.g-model .mini{height:5px;border-radius:3px;background:var(--track);overflow:hidden}
.g-model .mini i{display:block;height:100%;background:var(--cyan)}
.g-model li span:last-child{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.g-tags{display:flex;flex-wrap:wrap;gap:5px;margin:var(--s2) 0 var(--s3)}
.g-tags span{font-size:12px;padding:3px 8px;border-radius:7px;background:var(--hover2);
color:var(--fg)}
.g-rule{grid-column:1/-1;border-top:1px dashed var(--line2);padding-top:var(--s4)}
.g-rule code{font:600 12px/1.6 var(--mono);color:var(--fg);background:var(--hover);
padding:2px 6px;border-radius:6px}
.g-error{margin-top:var(--s6);padding:var(--s5) var(--s6);border-radius:var(--r3);
border:1px solid color-mix(in srgb,var(--bad) 40%,transparent);
background:color-mix(in srgb,var(--bad) 8%,transparent)}
.g-error[hidden]{display:none}
.g-error b{color:var(--bad)}
.g-error p{margin:6px 0 0;color:var(--muted)}
.g-recent ul{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:var(--s2)}
.g-recent a{display:inline-flex;align-items:center;gap:8px;min-height:40px;padding:6px 12px;
border-radius:var(--r2);border:1px solid var(--line2);background:var(--card);color:var(--fg);
font-size:13px;max-width:100%}
.g-recent a:hover{text-decoration:none;border-color:var(--accent-line);background:var(--hover)}
.g-recent a span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:28ch}
.g-recent a b{color:var(--accent);font-weight:650;white-space:nowrap}
.g-recent a small{color:var(--dim);font-variant-numeric:tabular-nums;white-space:nowrap}
/* home: the compact genre entry */
.genre-mini{display:flex;align-items:center;gap:var(--s3);flex-wrap:wrap;max-width:820px;
margin:var(--s5) 0 0;padding:10px 10px 10px 16px;border:1px solid var(--line2);
border-radius:var(--r3);background:var(--card2)}
.genre-mini label{font-weight:650;white-space:nowrap}
.genre-mini label small{display:block;font-weight:500;color:var(--muted);font-size:12px}
.genre-mini input{flex:1;min-width:180px;height:40px;padding:0 12px;border-radius:10px;
border:1px solid var(--line2);background:var(--card);color:var(--fg);font-size:14px}
.genre-mini input:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
@media (max-width:860px){.g-body{grid-template-columns:1fr}
.g-answer{border-right:0;border-bottom:1px solid var(--line)}
.g-side{flex-direction:row;flex-wrap:wrap}.g-side .g-tile{flex:1 1 140px}
.g-why-in{grid-template-columns:1fr}}
@media (max-width:720px){.genre-form .urlbox .q-ic{display:none}}
@media (max-width:520px){.g-track{grid-template-columns:64px 1fr;gap:var(--s4);
padding:var(--s4)}.g-art{width:64px;height:64px}
.g-answer,.g-side{padding:var(--s5) var(--s4)}.g-why>summary{padding:var(--s4)}
.g-why-in{padding:0 var(--s4) var(--s5)}.g-live{padding:var(--s4)}
.g-live .stage span{display:none}.g-live .stage.active span{display:inline}
.g-tile .v{font-size:28px}.genre-mini label{white-space:normal}
.genre-mini input{flex-basis:100%}.genre-mini .btn{width:100%}}
"""

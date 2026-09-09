"""Stage 7 local server and rescan queue.

A read-only, ``127.0.0.1``-only server over ``work/**/present/`` plus a single ``POST /rescan``
endpoint that only ever *appends a request to a queue file* — it makes no provider calls and writes
nothing else.  The queue (``present/rescan_queue.jsonl``) is later consumed by ``idea
rescan <url>`` to run another generation.

The index page lists analysed sets by their ``source.json`` title only — never a username or any
comment text.
"""

from __future__ import annotations

import html
import json
import re
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    EpisodesFile,
    RescanRequestRecord,
    SourceRecord,
    compose_natural_key,
    make_id,
)
from id_detector.ingest import _load_cached
from id_detector.io import (
    atomic_write_bytes,
    canonical_json_bytes,
    native_path,
    path_is_file,
    path_mtime,
    read_text,
    sha256_file,
)
from id_detector.present.exports import _format_time
from id_detector.present.page import EmbedPlan, plan_embed_from_url
from id_detector.present.refresh import ensure_fresh_page
from id_detector.present.theme import PLATFORM_NAMES, head_html, platform_chip, topbar_html
from id_detector.providers.base import AppConfig
from id_detector.rescan import policy_for_trigger, priority_for_trigger
from id_detector.webapp.jobs import TERMINAL_STATES, Job, JobManager, TargetValidationError

_SHA = re.compile(r"^[0-9a-f]{64}$")
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_PROFILES = ("free", "max_accuracy")
_MANUAL_TRIGGERS = {"gap", "edge", "contested", "long_episode", "novelty", "hint_cluster"}
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".cue": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}


# --------------------------------------------------------------------------------------------------
# Rescan queue (pure file operations — no network)
# --------------------------------------------------------------------------------------------------
def rescan_queue_path(media_dir: Path) -> Path:
    return media_dir / "present" / "rescan_queue.jsonl"


def build_rescan_request(
    *,
    source: SourceRecord,
    media_dir: Path,
    trigger: str,
    start_ms: int,
    end_ms: int,
    config: AppConfig | None = None,
) -> RescanRequestRecord:
    """Construct a schema-valid ``rescan_request`` for a manual page request."""

    if trigger not in _MANUAL_TRIGGERS:
        raise ValueError(f"unsupported manual rescan trigger: {trigger!r}")
    start = max(0, int(start_ms))
    end = max(start + 1, int(end_ms))
    policy = policy_for_trigger(trigger, config=config)
    generation = 0
    input_hashes: dict[str, str] = {}
    episodes_path = media_dir / "fuse" / "episodes.json"
    if path_is_file(episodes_path):
        try:
            generation = EpisodesFile.model_validate_json(read_text(episodes_path)).generation
        except (ValueError, OSError):
            generation = 0
        input_hashes["fuse/episodes.json"] = sha256_file(episodes_path)
    natural = {
        "generation": generation,
        "trigger": trigger,
        "start_ms": start,
        "end_ms": end,
        "policy": policy.model_dump(mode="json"),
    }
    natural_key = compose_natural_key("rescan_request", natural)
    return RescanRequestRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id(source.media_key, "rescan_request", natural_key),
        generation=generation,
        trigger=trigger,
        start_ms=start,
        end_ms=end,
        policy=policy,
        priority=priority_for_trigger(trigger),
        input_hashes=input_hashes,
    )


def append_rescan_request(media_dir: Path, request: RescanRequestRecord) -> Path:
    """Append one request as a canonical JSON line, skipping a duplicate id already queued."""

    path = rescan_queue_path(media_dir)
    existing = read_rescan_queue(media_dir)
    if any(item.id == request.id for item in existing):
        return path
    line = canonical_json_bytes(request) + b"\n"
    previous = b""
    if path_is_file(path):
        with open(native_path(path), "rb") as handle:
            previous = handle.read()
    atomic_write_bytes(path, previous + line)
    return path


def read_rescan_queue(media_dir: Path) -> list[RescanRequestRecord]:
    path = rescan_queue_path(media_dir)
    if not path_is_file(path):
        return []
    records: list[RescanRequestRecord] = []
    for line in read_text(path).splitlines():
        line = line.strip()
        if line:
            records.append(RescanRequestRecord.model_validate_json(line))
    return records


def consume_rescan_queue(media_dir: Path) -> list[RescanRequestRecord]:
    """Read the queue and move it aside to ``rescan_queue.consumed.jsonl`` (append-preserving)."""

    records = read_rescan_queue(media_dir)
    path = rescan_queue_path(media_dir)
    if not path_is_file(path):
        return records
    consumed = media_dir / "present" / "rescan_queue.consumed.jsonl"
    previous = b""
    if path_is_file(consumed):
        with open(native_path(consumed), "rb") as handle:
            previous = handle.read()
    with open(native_path(path), "rb") as handle:
        current = handle.read()
    atomic_write_bytes(consumed, previous + current)
    atomic_write_bytes(path, b"")
    return records


# --------------------------------------------------------------------------------------------------
# Read-only server
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class AnalysedSet:
    source_key: str
    media_key: str
    media_dir: Path
    title: str
    platform: str


def _discover_sets(work_root: Path) -> list[AnalysedSet]:
    dated: list[tuple[float, AnalysedSet]] = []
    if not work_root.is_dir():
        return []
    for source_json in work_root.glob("*/*/ingest/source.json"):
        index_html = source_json.parents[1] / "present" / "index.html"
        if not path_is_file(index_html):
            continue
        try:
            source = SourceRecord.model_validate_json(read_text(source_json))
        except (ValueError, OSError):
            continue
        # "Analysed at" = when the set was ingested (source.json is written once and never touched
        # on a re-render or an open, unlike present/index.html), so viewing a mix never reorders
        # the library.  Newest first.
        analysed_at = path_mtime(source_json)
        dated.append(
            (
                analysed_at,
                AnalysedSet(
                    source_key=source.source_key,
                    media_key=source.media_key,
                    media_dir=source_json.parents[1],
                    title=source.title or "(untitled set)",
                    platform=source.platform,
                ),
            )
        )
    dated.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in dated]


def _human_duration(milliseconds: int) -> str:
    """``1h 57m`` / ``58 min`` — the library's "music listened" figure."""

    minutes = max(0, int(milliseconds)) // 60_000
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes} min"


@dataclass(frozen=True)
class _SetSummary:
    """Per-mix figures for a library card, read from the flattened ``present/tracklist.json``."""

    tracks: int
    duration_ms: int
    badges: dict[str, int]


def _set_summary(item: AnalysedSet) -> _SetSummary | None:
    path = item.media_dir / "present" / "tracklist.json"
    if not path_is_file(path):
        return None
    try:
        document = json.loads(read_text(path))
        entries = [e for e in document.get("entries", ()) if e.get("kind") == "track"]
        badges: dict[str, int] = {}
        for entry in entries:
            badge = str(entry.get("badge", "unclear"))
            badges[badge] = badges.get(badge, 0) + 1
        return _SetSummary(len(entries), int(document.get("duration_ms") or 0), badges)
    except (ValueError, OSError, TypeError, AttributeError):
        return None


_BADGE_ORDER = ("verified", "likely", "possible", "unclear")


def _conf_mini_html(badges: dict[str, int]) -> str:
    total = sum(badges.get(key, 0) for key in _BADGE_ORDER)
    if not total:
        return ""
    bars = "".join(
        f'<i class="c-{key}" style="width:{badges[key] * 100.0 / total:.2f}%"></i>'
        for key in _BADGE_ORDER
        if badges.get(key)
    )
    title = " · ".join(f"{badges[key]} {key}" for key in _BADGE_ORDER if badges.get(key))
    return f'<span class="conf-mini" title="{html.escape(title)}">{bars}</span>'


def _index_html(sets: list[AnalysedSet]) -> bytes:
    """Read-only mode home: the mixes library with no analyse form (Stage 7 index)."""

    body = (
        topbar_html(back=False, new=False)
        + '<main class="home"><header class="hero-home compact"><h1>Analysed sets</h1>'
        '<p class="lede">Every mix analysed under this work root — open one to explore its '
        "tracklist. This server is read-only.</p></header>"
        + _library_stats_html(sets)
        + _mixes_block(sets, allow_new=False)
        + _footer_html()
        + "</main>"
    )
    return _page_shell("IDea — analysed sets", body)


# --------------------------------------------------------------------------------------------------
# Web-app pages (self-contained inline HTML/CSS/JS; no usernames or comment text)
# --------------------------------------------------------------------------------------------------
_APP_CSS = """
/* home hero + the drop-a-link form */
.hero-home{padding:44px 0 26px;max-width:760px}
.hero-home.compact{padding:26px 0 8px}
.eyebrow{font:700 11px/1 var(--display);letter-spacing:.16em;text-transform:uppercase;
color:var(--dim);margin:0 0 14px}
.hero-home h1{font:800 clamp(34px,5.2vw,58px)/1.02 var(--display);letter-spacing:-.035em;
margin:0 0 14px}
.lede{color:var(--muted);font-size:15px;max-width:60ch;margin:0 0 22px}
.dropform{margin:0}
.urlbox{display:flex;align-items:center;gap:8px;padding:6px 6px 6px 10px;background:var(--card);
border:1px solid var(--line2);border-radius:16px;
box-shadow:0 30px 60px -40px rgba(139,92,246,.6);transition:border-color .15s,box-shadow .15s}
.urlbox:focus-within{border-color:var(--accent);
box-shadow:0 0 0 4px rgba(167,139,250,.18),0 30px 60px -40px rgba(139,92,246,.8)}
.plat-ind{display:inline-flex;align-items:center;gap:7px;font:600 12px/1 var(--text);
color:var(--muted);padding:8px 10px;border-radius:10px;background:#ffffff08;white-space:nowrap;
transition:color .15s}
.plat-ind.on{color:var(--fg)}
.urlbox input{flex:1;min-width:0;padding:12px 8px;border:0;background:transparent;color:var(--fg);
font:15px/1.3 var(--text);outline:none}
.urlbox input::placeholder{color:var(--dim)}
.opts{margin-top:12px}
.opts>summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;gap:10px;
font:600 13px/1 var(--text);color:var(--muted);padding:8px 12px;border-radius:9px;
border:1px solid transparent}
.opts>summary::-webkit-details-marker{display:none}
.opts>summary::before{content:"▸";font-size:11px;transition:transform .15s}
.opts[open]>summary::before{transform:rotate(90deg)}
.opts>summary:hover,.opts[open]>summary{color:var(--fg);border-color:var(--line)}
.opt-sum{font-weight:500;color:var(--dim)}
.opt-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
.seg{grid-column:1/-1;display:grid;grid-template-columns:1fr 1fr;gap:8px}
.segopt{position:relative;cursor:pointer}
.segopt input{position:absolute;opacity:0;inset:0}
.segopt span{display:block;padding:12px 14px;border:1px solid var(--line);border-radius:12px;
background:var(--card);transition:border-color .12s,background .12s}
.segopt input:checked+span{border-color:var(--accent);
background:linear-gradient(135deg,rgba(139,92,246,.18),rgba(34,211,238,.08))}
.segopt input:focus-visible+span{box-shadow:0 0 0 3px rgba(167,139,250,.3)}
.segopt b{display:block;font-size:14px}.segopt small,.tog small,.tlbox-h small{display:block;
color:var(--muted);font-size:12px;margin-top:2px}
.tog{display:flex;align-items:flex-start;gap:12px;padding:12px 14px;border:1px solid var(--line);
border-radius:12px;background:var(--card);cursor:pointer}
.tog input{position:absolute;opacity:0;width:0;height:0}
.tog b{font-size:13px}
.sw{flex:none;width:34px;height:20px;border-radius:999px;background:#ffffff1a;position:relative;
margin-top:1px;transition:background .15s}
.sw::after{content:"";position:absolute;top:3px;left:3px;width:14px;height:14px;border-radius:50%;
background:#fff;transition:transform .15s}
.tog input:checked~.sw{background:var(--violet)}
.tog input:checked~.sw::after{transform:translateX(14px)}
.tog input:focus-visible~.sw{box-shadow:0 0 0 3px rgba(167,139,250,.3)}
.tlbox{grid-column:1/-1;padding:12px 14px;border:1px solid var(--line);border-radius:12px;
background:var(--card)}
.tlbox-h{display:block;margin-bottom:8px}.tlbox-h b{font-size:13px}
.tlbox textarea{display:block;width:100%;resize:vertical;min-height:66px;padding:10px 12px;
border:1px solid var(--line);border-radius:9px;background:#ffffff08;color:var(--fg);
font:12px/1.55 var(--mono);outline:none;transition:border-color .12s,box-shadow .12s}
.tlbox textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(167,139,250,.18)}
.tlbox textarea::placeholder{color:var(--dim)}
.opt-grid>.tog{grid-column:1/-1}
.adv{grid-column:1/-1;margin-top:2px}
.adv>summary{list-style:none;cursor:pointer;font-size:12px;color:var(--dim);padding:4px 2px;
display:inline-flex;align-items:center;gap:6px}
.adv>summary::-webkit-details-marker{display:none}
.adv>summary::before{content:"▸";font-size:10px;transition:transform .15s}
.adv[open]>summary::before{transform:rotate(90deg)}
.adv>summary:hover{color:var(--fg)}
.adv .tog,.adv .tlbox{margin-top:8px;grid-column:auto}
.consent{grid-column:1/-1;border-color:rgba(251,191,36,.38);background:rgba(251,191,36,.06)}
.consent[hidden]{display:none}
/* library */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;
margin:8px 0 4px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;
min-height:78px;display:flex;align-items:center}
.stat .big{font:800 30px/1 var(--display);letter-spacing:-.03em;font-variant-numeric:tabular-nums}
.stat small{display:block;color:var(--muted);font-size:12px;margin-top:4px}
.mixes{list-style:none;padding:0;margin:0;display:grid;
grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.mix{border:1px solid var(--line);border-radius:16px;background:var(--card);overflow:hidden;
transition:transform .15s,border-color .15s,box-shadow .15s;position:relative}
.mix::before{content:"";position:absolute;inset:0 0 auto 0;height:3px;background:var(--grad);
opacity:0;transition:opacity .15s}
.mix:hover{transform:translateY(-2px);border-color:var(--line2);
box-shadow:0 20px 40px -24px rgba(139,92,246,.7)}
.mix:hover::before{opacity:1}
.mix-link{display:block;padding:16px 16px 14px;color:inherit}
.mix-link:hover{text-decoration:none}
.mix-top{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:12px}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--muted);font-size:12px}
.mix-title{font:700 17px/1.25 var(--display);letter-spacing:-.01em;margin-bottom:10px;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.mix-meta{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:12px}
.mix-meta b{color:var(--fg)}
.conf-mini{display:flex;flex:1;height:6px;border-radius:4px;overflow:hidden;background:#ffffff10;
gap:1px}
.conf-mini i{display:block;height:100%}
.c-verified{background:var(--verified)}.c-likely{background:var(--likely)}
.c-possible{background:var(--possible)}.c-unclear{background:var(--unclear)}
.go{color:var(--dim);font-size:16px;transition:transform .15s,color .15s}
.mix:hover .go{color:var(--fg);transform:translateX(3px)}
.empty{border:1px dashed var(--line2);border-radius:16px;padding:40px 20px;text-align:center;
color:var(--muted)}
.empty b{display:block;color:var(--fg);font:700 18px/1.2 var(--display);margin-bottom:6px}
.acts{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:8px}
.act{border:1px solid var(--line);border-radius:14px;background:var(--card);overflow:hidden}
.act-link{display:block;padding:12px 16px;color:inherit}.act-link:hover{text-decoration:none}
.act-row{display:flex;align-items:center;gap:12px;min-width:0}
.act-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
font-weight:600;font-size:14px}
.act-phase{color:var(--muted);font-size:12px;margin-top:6px;
font-variant-numeric:tabular-nums}
.bar{height:6px;border-radius:4px;background:#ffffff10;overflow:hidden;margin-top:8px}
.bar>span{display:block;height:100%;background:var(--grad);width:0;transition:width .6s;
border-radius:4px}
.act[data-status="failed"] .bar>span{background:var(--bad)}
.act[data-status="cancelled"] .bar>span{background:var(--dim)}
/* how it works (new-mix page) */
.how{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:8px}
.how div{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}
.how b{display:block;font:700 14px/1.2 var(--display);margin:8px 0 4px}
.how small{color:var(--muted);font-size:12px}
.how .n{display:inline-grid;place-items:center;width:26px;height:26px;border-radius:8px;
background:var(--grad);color:#fff;font:800 12px/1 var(--display)}
/* progress page */
.job-head{display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap;padding:28px 0 18px}
.job-head .titles{flex:1;min-width:0}
.job-head h1{font:800 clamp(26px,3.6vw,38px)/1.1 var(--display);letter-spacing:-.03em;
margin:6px 0 6px;overflow-wrap:anywhere}
.job-url{color:var(--muted);font-size:13px;overflow-wrap:anywhere}
.job-actions{display:flex;align-items:center;gap:10px;padding-top:6px}
.job-player{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:14px;
margin:0 0 18px}
.job-player .jp-label{display:flex;align-items:center;gap:9px;font-size:12.5px;color:var(--muted);
margin:2px 2px 12px}
.job-player audio{display:block;width:100%;height:44px;border-radius:12px;outline:none}
.job-player audio::-webkit-media-controls-panel{background:#1a1a26}
.job-player .jp-yt{position:relative;width:100%;max-width:560px;aspect-ratio:16/9;
border-radius:12px;overflow:hidden}
.job-player .jp-yt iframe{position:absolute;inset:0;width:100%;height:100%;border-radius:0}
.jp-frame{position:relative}.jp-frame[hidden]{display:none}
.jp-frame.loading audio{opacity:0}
.jp-wait{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
gap:10px;color:var(--muted);font-size:13px;background:#0c0c13;border-radius:12px;
border:1px solid var(--line);animation:blink 1.6s ease-in-out infinite}
.jp-frame:not(.loading) .jp-wait{display:none}
.jp-fail{color:var(--muted);font-size:13px;padding:14px 16px;border:1px dashed var(--line2);
border-radius:12px}.jp-fail[hidden]{display:none}.jp-fail b{color:var(--fg)}
.scan{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px 22px 18px;
position:relative;overflow:hidden}
.scan::after{content:"";position:absolute;inset:0;pointer-events:none;
background:linear-gradient(180deg,#ffffff05,transparent)}
.scan-top{display:flex;align-items:flex-end;gap:22px;flex-wrap:wrap;margin-bottom:16px}
.pct{font:800 64px/1 var(--display);letter-spacing:-.04em;font-variant-numeric:tabular-nums;
background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;
min-width:3ch}
.pct small{font-size:26px;letter-spacing:0}
.phase-line{flex:1;min-width:220px}
.phase-name{font:700 18px/1.2 var(--display);letter-spacing:-.01em}
.phase-msg{color:var(--muted);font-size:13px;margin-top:3px;min-height:18px}
.tiles{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:12px}
.tiles b{display:block;color:var(--fg);font:700 18px/1.1 var(--display);
font-variant-numeric:tabular-nums}
.cells{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;gap:2px;height:46px;padding:3px;
background:#0c0c13;border:1px solid var(--line);border-radius:10px}
.cells i{display:block;border-radius:2px;background:#1a1a26;transition:background .35s,
box-shadow .35s}
.cells i.on{background:var(--c);box-shadow:0 0 8px -2px var(--c)}
.cells i.pop{animation:pop .5s ease-out}
.cells i.next{animation:next 1s ease-in-out infinite}
@keyframes pop{0%{background:#fff;box-shadow:0 0 14px #fff}}
@keyframes next{50%{background:#2a2a3c}}
.cells.idle{background-image:linear-gradient(90deg,transparent 0%,rgba(139,92,246,.18) 50%,
transparent 100%);background-size:40% 100%;background-repeat:no-repeat;
animation:shimmer 1.6s linear infinite}
@keyframes shimmer{from{background-position:-40% 0}to{background-position:140% 0}}
.flavour{margin:14px 2px 0;color:var(--muted);font-size:13px;min-height:20px;
transition:opacity .35s}
.flavour.fade{opacity:0}
.steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;margin:14px 0}
.step{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 12px 10px;
position:relative;transition:border-color .2s,opacity .2s;opacity:.55}
.step .k{display:flex;align-items:center;gap:8px;font:700 12px/1 var(--display);
letter-spacing:.02em}
.step .k i{display:inline-grid;place-items:center;width:18px;height:18px;border-radius:50%;
border:1.5px solid var(--dim);font:700 10px/1 var(--mono);color:var(--dim);font-style:normal}
.step small{display:block;color:var(--dim);font-size:11px;margin-top:5px}
.step.done{opacity:1}.step.done .k i{background:var(--ok);border-color:var(--ok);color:#0a0a0f}
.step.active{opacity:1;border-color:var(--accent);
background:linear-gradient(135deg,rgba(139,92,246,.16),rgba(34,211,238,.06))}
.step.active .k i{border-color:var(--accent);color:var(--accent);
animation:ringpulse 1.2s ease-out infinite}
@keyframes ringpulse{0%{box-shadow:0 0 0 0 rgba(167,139,250,.55)}
100%{box-shadow:0 0 0 8px rgba(167,139,250,0)}}
.outcome{display:none;border-radius:18px;padding:26px;border:1px solid var(--line);
background:var(--card);position:relative;overflow:hidden;margin:14px 0}
.outcome.show{display:block;animation:rise .5s ease both}
.outcome h2{font:800 30px/1.1 var(--display);letter-spacing:-.03em;margin:0 0 6px}
.outcome p{color:var(--muted);margin:0 0 16px}
.outcome.ok{border-color:rgba(52,211,153,.35);
background:linear-gradient(135deg,rgba(52,211,153,.10),var(--card) 60%)}
.outcome.bad{border-color:rgba(251,113,133,.4);
background:linear-gradient(135deg,rgba(251,113,133,.10),var(--card) 60%)}
.outcome .err{font-family:var(--mono);font-size:12px;color:var(--bad);background:#00000033;
border-radius:8px;padding:10px 12px;margin:0 0 16px;white-space:pre-wrap;overflow-wrap:anywhere}
.outcome .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.countdown{color:var(--dim);font-size:12px}
.confetti{position:absolute;inset:0;pointer-events:none;overflow:hidden}
.confetti i{position:absolute;top:-10px;left:var(--x);width:7px;height:11px;background:var(--c);
border-radius:2px;opacity:0;animation:fall var(--d) ease-in var(--delay) forwards}
@keyframes fall{0%{opacity:1;transform:translateY(0) rotate(0)}
100%{opacity:0;transform:translateY(340px) rotate(var(--r))}}
details.log{margin-top:16px}
details.log>summary{cursor:pointer;color:var(--dim);font-size:12px;list-style:none;
display:inline-flex;align-items:center;gap:6px}
details.log>summary::-webkit-details-marker{display:none}
details.log>summary::before{content:"▸";font-size:10px}
details.log[open]>summary::before{content:"▾"}
pre{white-space:pre-wrap;word-break:break-word;background:#0c0c13;border:1px solid var(--line);
border-radius:10px;padding:12px;font:12px/1.6 var(--mono);color:var(--muted);max-height:260px;
overflow:auto;margin:8px 0 0}
@media (max-width:720px){.hero-home{padding:24px 0 18px}.urlbox{flex-wrap:wrap;padding:8px}
.urlbox input{flex-basis:100%;order:-1;padding:10px 6px}.urlbox .btn{margin-left:auto}
.opt-grid,.seg{grid-template-columns:1fr}.pct{font-size:48px}.job-head{padding-top:16px}}
"""


_FORM_JS = """
(function(){
  var input = document.getElementById('url'); if(!input) return;
  var ind = document.getElementById('plat'), name = document.getElementById('plat-name');
  function detect(v){
    v = (v || '').trim().toLowerCase();
    if(!v) return ['', 'Paste a link'];
    if(v.indexOf('soundcloud.com') >= 0) return ['soundcloud', 'SoundCloud'];
    if(v.indexOf('youtube.com') >= 0 || v.indexOf('youtu.be') >= 0) return ['youtube', 'YouTube'];
    if(v.indexOf('mixcloud.com') >= 0) return ['mixcloud', 'Mixcloud'];
    if(/^https?:\\/\\//.test(v)) return ['', 'Web link'];
    return ['file', 'Local file'];
  }
  function update(){
    var d = detect(input.value);
    ind.className = 'plat-ind' + (d[0] ? ' on plat-' + d[0] : (input.value.trim() ? ' on' : ''));
    name.textContent = d[1];
  }
  input.addEventListener('input', update); update();
  var sum = document.getElementById('opt-sum');
  function summary(){
    if(!sum) return;
    var f = input.form, parts = [];
    var prof = f.querySelector('input[name=profile]:checked');
    parts.push(prof && prof.value === 'max_accuracy' ? 'Paid cross-check' : 'Free');
    if(f.querySelector('input[name=acquire]').checked) parts.push('with download links');
    if(f.querySelector('input[name=build_index]').checked) parts.push('reference index');
    sum.textContent = parts.join(' · ');
  }
  var consent = document.getElementById('consent-row');
  function gateConsent(){
    if(!consent) return;
    var prof = input.form.querySelector('input[name=profile]:checked');
    var max = prof && prof.value === 'max_accuracy';
    consent.hidden = !max;
    if(!max){
      var c = consent.querySelector('input[name=upload_consent]'); if(c) c.checked = false;
    }
  }
  Array.prototype.forEach.call(input.form.querySelectorAll(
  'input[type=radio],input[type=checkbox]'),
    function(el){ el.addEventListener('change', function(){ summary(); gateConsent(); }); });
  summary(); gateConsent();
})();
"""


_HOME_JS = """
(function(){
  var cards = document.querySelectorAll('.act[data-job]'); if(!cards.length) return;
  function fmtPhase(j){
    if(j.status === 'queued') return 'waiting in the queue';
    var t = j.phase; if(j.message) t += ' — ' + j.message;
    if(j.windows_total) t += '  ·  ' + j.windows_done + ' / ' + j.windows_total + ' windows';
    return t;
  }
  function poll(card){
    var id = card.getAttribute('data-job');
    fetch('/jobs/' + id + '/status').then(function(r){ return r.json(); }).then(function(j){
      var st = card.querySelector('.st'); st.textContent = j.status;
  st.className = 'st st-' + j.status;
      card.setAttribute('data-status', j.status);
      card.querySelector('.act-phase').textContent = fmtPhase(j);
      var total = j.windows_total || 0, done = j.windows_done || 0;
      var pct = total ? Math.round(done * 100 / total) : (j.terminal ? 100 : 4);
      card.querySelector('.bar>span').style.width = pct + '%';
      if(j.status === 'succeeded' && j.result_url){ window.location.reload(); return; }
      if(!j.terminal) setTimeout(function(){ poll(card); }, 2500);
    }).catch(function(){ setTimeout(function(){ poll(card); }, 5000); });
  }
  Array.prototype.forEach.call(cards, function(card){
    if(card.getAttribute('data-terminal') !== '1') poll(card); });
})();
"""


_JOB_JS = """
var STEP_INDEX = {}; STEPS.forEach(function(s, i){ STEP_INDEX[s[0]] = i; });
var FLAVOUR = {
  queued: ['Waiting in the queue — one analysis at a time keeps Shazam happy.'],
  starting: ['Warming up…'],
  build_index: ['Fingerprinting the uploader\\'s own tracks so unreleased ones match too.'],
  ingest: ['Fetching the mix from the platform — a long set can take a minute.',
    'Only the audio comes down; nothing about you goes up.'],
  decode: ['Decoding to raw audio so every window sounds the same to the engines.'],
  windows: ['Slicing the set into short, overlapping windows.'],
  recognise: ['Every window is one question: what\\'s playing right now?',
    'Shazam allows 18 asks a minute — we\\'re being polite, hence the wait.',
    'A window that matches nothing stays honest: it becomes an ID, never a guess.',
    'Overlapping windows are how a track start gets pinned — only as far as evidence proves.',
    'The same track heard across several windows gets stitched into one episode later.',
    'Nod along. The machine is listening so you don\\'t have to rewind.'],
  hints: ['Reading the comments for tracklist clues — as hints, never as proof.'],
  fuse: ['Stitching windows into track episodes with honest confidence tiers.',
    'Where a boundary is not proved, the page will say so rather than guess.'],
  enrich: ['Looking up where each track can be bought or downloaded.'],
  present: ['Writing your click-to-jump tracklist page.'],
  done: ['Done.'], failed: [''], cancelled: ['']
};
var cellCount = 0, lastDone = -1, flavourIx = 0, flavourPhase = '', redirectLeft = null;
var LAST = null, celebrated = false;
function fmt(s){ s = Math.max(0, Math.round(s)); var m = Math.floor(s / 60);
  return m ? (m + 'm ' + (s % 60 < 10 ? '0' : '') + (s % 60) + 's') : (s + 's'); }
function colour(t){
  function mix(a, b, u){ return a.map(function(v, i){ return Math.round(v + (b[i] - v) * u); }); }
  var P = [255, 61, 138], V = [139, 92, 246], C = [34, 211, 238];
  var rgb = t < 0.55 ? mix(P, V, t / 0.55) : mix(V, C, (t - 0.55) / 0.45);
  return 'rgb(' + rgb.join(',') + ')';
}
function buildCells(total){
  var box = document.getElementById('cells'); box.innerHTML = ''; box.classList.remove('idle');
  cellCount = Math.min(total, 240); lastDone = -1;
  for(var i = 0; i < cellCount; i++){
    var c = document.createElement('i'); c.style.setProperty('--c', colour(i / Math.max(1,
  cellCount - 1)));
    box.appendChild(c);
  }
}
function lightCells(done, total){
  var box = document.getElementById('cells'); if(!cellCount) return;
  var lit = Math.round(done * cellCount / Math.max(1, total));
  var cells = box.children;
  for(var i = 0; i < cellCount; i++){
    var on = i < lit; cells[i].classList.toggle('on', on);
    cells[i].classList.toggle('next', i === lit && done < total);
    if(on && i >= lastDone) cells[i].classList.add('pop');
  }
  lastDone = lit;
}
function overall(j){
  var ix = STEP_INDEX[j.phase]; var rec = STEP_INDEX['recognise'];
  if(j.status === 'succeeded') return 100;
  if(j.status === 'queued' || ix === undefined) return 0;
  if(ix < rec) return 2 + ix * 2;
  if(ix === rec){ var t = j.windows_total || 0; return 8 + (t ? Math.round(
  j.windows_done * 82 / t) : 0); }
  return 90 + Math.min(9, (ix - rec) * 3);
}
function setSteps(j){
  var ix = STEP_INDEX[j.phase]; var terminal = j.terminal;
  Array.prototype.forEach.call(document.querySelectorAll('.step'), function(el, i){
    el.classList.remove('done', 'active');
    if(j.status === 'succeeded' || (
  terminal === false && ix !== undefined && i < ix)) el.classList.add('done');
    else if(!terminal && ix === i) el.classList.add('active');
    else if(terminal && ix !== undefined && i < ix) el.classList.add('done');
  });
}
function titleFromLog(j){
  for(var i = 0; i < (j.log || []).length; i++){
    var m = /ingest: (.+)$/.exec(j.log[i]);
    var skip = {'resolving source': 1, 'started': 1, 'source ready': 1};
    if(m && !skip[m[1]]) return m[1];
  }
  return null;
}
function fromLog(j, re){ for(var i = 0; i < (j.log || []).length; i++){ var m = re.exec(
  j.log[i]); if(m) return m[1]; } return null; }
function rotateFlavour(){
  var j = LAST; if(!j) return;
  var pool = FLAVOUR[j.status === 'queued' ? 'queued' : j.phase] || [];
  if(!pool.length) return;
  if(flavourPhase !== j.phase){ flavourPhase = j.phase; flavourIx = 0; } else { flavourIx = (
  flavourIx + 1) % pool.length; }
  var el = document.getElementById('flavour'); el.classList.add('fade');
  setTimeout(function(){ el.textContent = pool[flavourIx]; el.classList.remove('fade'); }, 350);
}
function confetti(){
  var box = document.getElementById('confetti'); if(!box || celebrated) return; celebrated = true;
  var cols = ['#ff3d8a', '#8b5cf6', '#22d3ee', '#34d399', '#fbbf24', '#ffffff'];
  for(var i = 0; i < 70; i++){
    var p = document.createElement('i');
    p.style.setProperty('--x', (Math.random() * 100) + '%'); p.style.setProperty('--c',
  cols[i % cols.length]);
    p.style.setProperty('--d', (1.6 + Math.random() * 1.4) + 's'); p.style.setProperty(
  '--delay', (Math.random() * .6) + 's');
    p.style.setProperty('--r', (Math.random() * 720 - 360) + 'deg');
    box.appendChild(p);
  }
}
function showOutcome(j){
  var box = document.getElementById('outcome'); box.className = 'outcome show';
  document.getElementById('scan').style.display = 'none';
  var h = document.getElementById('o-title'), p = document.getElementById('o-sub'),
  row = document.getElementById('o-row');
  var err = document.getElementById('o-err'); err.style.display = 'none';
  if(j.status === 'succeeded'){
    box.classList.add('ok');
    var eps = fromLog(j, /fuse: (\\d+) episodes/), wins = fromLog(j, /windows: (\\d+) windows/);
    h.textContent = 'Tracklist ready';
    p.textContent = (eps ? eps + ' track episodes found' : 'Analysis finished') + (
  wins ? ' after listening to ' + wins + ' windows.' : '.');
    var open = '<a class="btn primary big" id="open" href="' + j.result_url +
      '">Open the tracklist →</a><span class="countdown" id="countdown"></span>';
    row.innerHTML = j.result_url ? open : '';
    confetti();
    if(j.result_url && redirectLeft === null){ redirectLeft = 5; countdown(j.result_url); }
  } else if(j.status === 'failed'){
    box.classList.add('bad'); h.textContent = 'That one didn\\'t work';
    p.textContent = 'The analysis stopped with an error. The log below has the details.';
    if(j.error){ err.textContent = j.error; err.style.display = 'block'; }
    row.innerHTML = '<a class="btn primary" href="/new?url=' + encodeURIComponent(
  DISPLAY) + '">Try again</a>' +
      '<a class="btn" href="/">Your mixes</a>';
  } else {
    h.textContent = 'Cancelled'; p.textContent = 'Stopped before it finished — nothing was saved.';
    row.innerHTML = '<a class="btn primary" href="/new?url=' + encodeURIComponent(
  DISPLAY) + '">Start again</a>' +
      '<a class="btn" href="/">Your mixes</a>';
  }
}
function countdown(url){
  var el = document.getElementById('countdown'); if(!el) return;
  if(redirectLeft <= 0){ window.location.href = url; return; }
  el.innerHTML = 'opening in ' + redirectLeft + 's · <a href="#" id="stay">stay here</a>';
  var stay = document.getElementById('stay');
  if(stay) stay.addEventListener('click', function(e){ e.preventDefault(); redirectLeft = -1;
  el.textContent = ''; });
  redirectLeft -= 1;
  setTimeout(function(){ if(redirectLeft >= 0) countdown(url); }, 1000);
}
function render(j){
  LAST = j;
  document.body.classList.toggle('analysing', !j.terminal);
  var st = document.getElementById('status'); st.textContent = j.status;
  st.className = 'st st-' + j.status;
  var t = titleFromLog(j); if(t){ document.getElementById('title').textContent = t;
  document.getElementById('url').style.display = 'block'; }
  var pct = overall(j);
  document.getElementById('pct').innerHTML = pct + '<small>%</small>';
  var stepIx = STEP_INDEX[j.phase];
  var label = stepIx !== undefined ? STEPS[stepIx][1]
    : (j.phase.charAt(0).toUpperCase() + j.phase.slice(1));
  if(j.status === 'queued') label = 'In the queue';
  document.getElementById('phase-name').textContent = label;
  var msg = j.message || (stepIx !== undefined ? STEPS[stepIx][2] : '');
  if(j.status === 'queued') msg = 'another analysis is running first';
  document.getElementById('phase-msg').textContent = msg;
  var total = j.windows_total || 0, done = j.windows_done || 0;
  if(total && total !== cellCount && (cellCount === 0 || Math.min(total,
  240) !== cellCount)) buildCells(total);
  if(total) lightCells(done, total); else document.getElementById('cells').classList.add('idle');
  document.getElementById('t-windows').textContent = total ? (done + ' / ' + total) : '—';
  document.getElementById('t-eta').textContent = (j.eta_seconds && !j.terminal) ? '~' + fmt(
  j.eta_seconds) : (j.terminal ? '—' : '…');
  document.getElementById('t-rate').textContent = (total && j.rate_per_minute) ?
    (Math.round(j.rate_per_minute * 10) / 10) + '/min' : '…';
  var started = j.started_at, finished = j.finished_at;
  var elapsed = started ? ((finished || Date.now() / 1000) - started) : 0;
  document.getElementById('t-elapsed').textContent = started ? fmt(elapsed) : '—';
  setSteps(j);
  document.title = (j.terminal ? (
  j.status === 'succeeded' ? 'Done' : j.status) : pct + '%') + ' · ' + (
  t || 'Analysing') + ' — IDea';
  document.getElementById('log').textContent = (j.log || []).join('\\n');
  document.getElementById('cancel').style.display = j.terminal ? 'none' : '';
  var eyebrow = {succeeded: 'Analysed', failed: 'Analysis failed', cancelled: 'Analysis cancelled'};
  document.getElementById('eyebrow').textContent = eyebrow[j.status] || 'Analysing';
  if(window.wireAudio) wireAudio(j);
  if(j.terminal) showOutcome(j); else if(flavourPhase !== j.phase) rotateFlavour();
}
function tick(){
  fetch('/jobs/' + JOB_ID + '/status').then(function(r){ return r.json(); }).then(function(j){
    render(j); if(!j.terminal) setTimeout(tick, 1500);
  }).catch(function(){ setTimeout(tick, 4000); });
}
setInterval(function(){ if(LAST && !LAST.terminal && LAST.started_at){
  document.getElementById('t-elapsed').textContent = fmt(Date.now() / 1000 - LAST.started_at);
  } }, 1000);
setInterval(rotateFlavour, 7000);
document.getElementById('cancel').addEventListener('click', function(){
  if(!confirm('Stop this analysis?')) return;
  fetch('/jobs/' + JOB_ID + '/cancel', {method: 'POST'}).then(tick);
});
tick();
"""


_PLAYER_JS = """
(function(){
  var section = document.getElementById('job-player'); if(!section) return;
  var frame = document.getElementById('jp-frame'), fail = document.getElementById('jp-fail');
  var audio = document.getElementById('jp-audio'), wired = false;
  audio.addEventListener('error', function(){ frame.hidden = true; fail.hidden = false; });
  audio.addEventListener('loadedmetadata', function(){ frame.classList.remove('loading'); });
  // Called from the status poll: the fetched original exists once ingest has completed.
  window.wireAudio = function(j){
    if(wired) return;
    if(j.audio_url){ wired = true; audio.src = j.audio_url; return; }
    if(j.terminal && j.status !== 'succeeded'){ section.hidden = true; }
  };
})();
"""


def _page_shell(title: str, body: str, script: str = "") -> bytes:
    tail = f"<script>{script}</script>" if script else ""
    return (head_html(title, _APP_CSS) + f"<body>{body}{tail}</body></html>\n").encode("utf-8")


def _footer_html() -> str:
    return (
        "<footer><span>🔒 runs on your machine — no account, only short clips go to the "
        "recognizers</span><span>IDea</span></footer>"
    )


def _form_html(prefill: str = "") -> str:
    """The drop-a-link form: a hero input with platform detection and a collapsible options tray."""

    return (
        '<form method="post" action="/analyse" class="dropform" autocomplete="off">'
        '<div class="urlbox">'
        '<span class="plat-ind" id="plat"><span class="pd"></span>'
        '<span id="plat-name">Paste a link</span></span>'
        '<input id="url" name="url" type="text" required autofocus spellcheck="false" '
        f'value="{html.escape(prefill)}" '
        'placeholder="https://soundcloud.com/… — or a local audio file path">'
        '<button class="btn primary big" type="submit">Analyse</button></div>'
        '<details class="opts"><summary><span>Options</span>'
        '<span class="opt-sum" id="opt-sum">Free · with download links</span></summary>'
        '<div class="opt-grid"><div class="seg" role="radiogroup" aria-label="Mode">'
        '<label class="segopt"><input type="radio" name="profile" value="free" checked>'
        "<span><b>Free</b><small>Shazam + crowd comments — no key needed</small></span></label>"
        '<label class="segopt"><input type="radio" name="profile" value="max_accuracy">'
        "<span><b>Paid cross-check</b><small>adds AudD to confirm uncertain tracks "
        "(~$0.75/mix)</small></span></label>"
        "</div>"
        # Download links are part of the normal result now (default on); a normal user always wants
        # them, so it is a prominent pre-ticked toggle rather than a buried opt-in.
        '<label class="tog"><input type="checkbox" name="acquire" value="1" checked>'
        '<span class="sw"></span>'
        "<span><b>Include buy / download links</b>"
        "<small>where to get each track — free download, buy, or gated — on the result page"
        "</small></span></label>"
        # Everything below is power-user territory: kept, but collapsed so the common path stays two
        # choices (Free/Paid + links).
        '<details class="adv"><summary>Advanced</summary>'
        '<label class="tog consent" id="consent-row" hidden>'
        '<input type="checkbox" name="upload_consent" value="1"><span class="sw"></span>'
        "<span><b>I own this audio (allow whole-file upload)</b>"
        "<small>Paid cross-check already works clip-by-clip without this. Tick only to also send "
        "the whole file to a paid engine — for a mix you own.</small></span></label>"
        '<label class="tog"><input type="checkbox" name="build_index" value="1">'
        '<span class="sw"></span><span><b>Build a reference index first</b>'
        "<small>fingerprints this uploader's own tracks — only helps if the DJ plays their own "
        "unreleased edits in the mix</small></span></label>"
        '<label class="tlbox"><span class="tlbox-h"><b>Know part of the tracklist?</b>'
        "<small>Paste anything you can already see — from 1001tracklists, a YouTube "
        "description, a comment — to guide the analysis. One track per line, ideally "
        '"12:34 Artist - Title". Leave blank to analyse from the audio only.</small></span>'
        '<textarea name="known_tracklist" rows="4" spellcheck="false" '
        'placeholder="12:34 Artist - Title&#10;19:20 Another Artist - Another Title">'
        "</textarea></label></details></div></details></form>"
    )


def _library_stats_html(sets: list[AnalysedSet]) -> str:
    """Three big numbers for the library: mixes, tracks identified, music listened to."""

    if not sets:
        return ""
    summaries = [_set_summary(item) for item in sets]
    tracks = sum(s.tracks for s in summaries if s)
    listened = sum(s.duration_ms for s in summaries if s)
    mixes = len(sets)
    return (
        '<div class="stats">'
        f'<div class="stat"><div><span class="big">{mixes}</span>'
        f"<small>mix{'es' if mixes != 1 else ''} analysed</small></div></div>"
        f'<div class="stat"><div><span class="big">{tracks}</span>'
        "<small>tracks identified</small></div></div>"
        f'<div class="stat"><div><span class="big">{html.escape(_human_duration(listened))}</span>'
        "<small>of music listened to</small></div></div>"
        "</div>"
    )


def _mix_card_html(item: AnalysedSet) -> str:
    href = f"/{html.escape(item.source_key)}/{html.escape(item.media_key)}/present/index.html"
    summary = _set_summary(item)
    duration = (
        f'<span class="mono">{html.escape(_format_time(summary.duration_ms))}</span>'
        if summary and summary.duration_ms
        else ""
    )
    meta = ""
    if summary:
        meta = (
            f'<div class="mix-meta"><span><b>{summary.tracks}</b> '
            f"track{'s' if summary.tracks != 1 else ''}</span>{_conf_mini_html(summary.badges)}"
            '<span class="go" aria-hidden="true">→</span></div>'
        )
    else:
        meta = '<div class="mix-meta"><span class="go" aria-hidden="true">→</span></div>'
    return (
        f'<li class="mix"><a class="mix-link" href="{href}">'
        f'<div class="mix-top">{platform_chip(item.platform)}{duration}</div>'
        f'<div class="mix-title">{html.escape(item.title)}</div>{meta}</a></li>'
    )


def _mixes_block(sets: list[AnalysedSet], *, allow_new: bool = True) -> str:
    """The library grid of analysed mixes, or a friendly empty state."""

    if sets:
        cards = "".join(_mix_card_html(item) for item in sets)
        return f'<ul class="mixes">{cards}</ul>'
    cta = (
        "<p>Paste a link above and find out what is in it.</p>"
        if allow_new
        else "<p>Analyse a set from the command line and it will appear here.</p>"
    )
    return f'<div class="empty"><b>No mixes yet</b>{cta}</div>'


def _activity_item_html(job: Job) -> str:
    status = html.escape(job.status)
    label = html.escape(job.display)
    phase = job.message and f"{job.phase} — {job.message}" or job.phase
    if job.status == "queued":
        phase = "waiting in the queue"
    total, done = job.windows_total, job.windows_done
    pct = round(done * 100 / total) if total else (100 if job.status == "succeeded" else 4)
    terminal = "1" if job.status in TERMINAL_STATES else "0"
    return (
        f'<li class="act" data-job="{html.escape(job.id)}" data-terminal="{terminal}" '
        f'data-status="{status}">'
        f'<a class="act-link" href="/jobs/{html.escape(job.id)}">'
        f'<div class="act-row"><span class="st st-{status}">{status}</span>'
        f'<span class="act-title">{label}</span></div>'
        f'<div class="act-phase">{html.escape(phase)}</div>'
        f'<div class="bar"><span style="width:{pct}%"></span></div></a></li>'
    )


def _home_html(sets: list[AnalysedSet], jobs: list[Job]) -> bytes:
    """The home: a drop-a-link hero, anything in flight, and the library of analysed mixes."""

    active = [job for job in jobs if job.status != "succeeded"]
    activity = ""
    if active:
        items = "".join(_activity_item_html(job) for job in active)
        activity = f'<h2 class="sec">In progress</h2><ul class="acts">{items}</ul>'
    body = (
        topbar_html(back=False, new=True) + '<main class="home"><header class="hero-home">'
        '<p class="eyebrow">DJ-set track identifier · runs on this machine</p>'
        '<h1>Drop a mix.<br><span class="grad">Get the tracklist.</span></h1>'
        '<p class="lede">Paste a SoundCloud, YouTube or Mixcloud link. It listens to the set in '
        "short windows, asks the recognition engines what is playing, and hands you a "
        "click-to-jump tracklist with honest confidence for every track.</p>"
        + _form_html()
        + "</header>"
        + activity
        + '<h2 class="sec">Your mixes</h2>'
        + _library_stats_html(sets)
        + _mixes_block(sets)
        + _footer_html()
        + "</main>"
    )
    return _page_shell("IDea — your mixes", body, _FORM_JS + _HOME_JS)


def _new_html(prefill: str = "") -> bytes:
    """The New-mix page: the analyse form on its own (also the "try again" landing)."""

    body = (
        topbar_html(back=True, new=False) + '<main class="home"><header class="hero-home">'
        '<p class="eyebrow">New mix</p>'
        '<h1>What is in <span class="grad">this one</span>?</h1>'
        '<p class="lede">Paste a mix link or a local audio file path, pick your options, and '
        "hit Analyse. You can leave the page while it runs — it keeps going on this machine.</p>"
        + _form_html(prefill)
        + "</header>"
        '<h2 class="sec">How it works</h2><div class="how">'
        '<div><span class="n">1</span><b>Fetch &amp; slice</b>'
        "<small>The audio is downloaded once and cut into short overlapping windows.</small></div>"
        '<div><span class="n">2</span><b>Listen</b>'
        "<small>Each window is sent to the recognition engines — politely, at their rate "
        "limit.</small></div>"
        '<div><span class="n">3</span><b>Stitch</b>'
        "<small>Matches are fused into track episodes with honest confidence tiers; unknown "
        "stretches stay marked ID.</small></div>"
        '<div><span class="n">4</span><b>Play</b>'
        "<small>You get a page where clicking any track jumps the player to that moment.</small>"
        "</div></div>" + _footer_html() + "</main>"
    )
    return _page_shell("IDea — new mix", body, _FORM_JS)


def _job_steps(job: Job) -> list[tuple[str, str, str]]:
    """The step tracker for this job — phase key, label, one-line sublabel — in pipeline order."""

    steps: list[tuple[str, str, str]] = []
    if job.build_index:
        steps.append(("build_index", "Index", "fingerprint the uploader's tracks"))
    steps += [
        ("ingest", "Fetch", "download the mix"),
        ("decode", "Decode", "to raw audio"),
        ("windows", "Slice", "into short windows"),
        ("recognise", "Listen", "ask the engines"),
        ("hints", "Hints", "read the comments"),
        ("fuse", "Stitch", "build track episodes"),
    ]
    if job.acquire:
        steps.append(("enrich", "Links", "where to get each track"))
    steps.append(("present", "Page", "write your tracklist"))
    return steps


#: Browser MIME types for the fetched original, by container (``OriginalAsset.container``) or
#: file suffix.  Anything else is served as ``application/octet-stream`` and the page's ``<audio>``
#: reports whether the browser can play it.
_AUDIO_TYPES = {
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "aac": "audio/aac",
    "webm": "audio/webm",
    "opus": "audio/ogg",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "flac": "audio/flac",
}
#: Phases during which the fetched original cannot exist yet (no point resolving it).
_PRE_INGEST_PHASES = frozenset({"queued", "starting", "build_index"})


def _audio_content_type(path: Path, container: str | None = None) -> str:
    key = (container or "").casefold() or path.suffix.lstrip(".").casefold()
    return _AUDIO_TYPES.get(
        key, _AUDIO_TYPES.get(path.suffix.lstrip(".").casefold(), "application/octet-stream")
    )


def _resolve_job_audio(job: Job, work_root: Path) -> Path | None:
    """The job's fetched original on disk, resolved (once) from the target after ingest.

    ``ingest._load_cached`` verifies the completion sidecar and the media-key hash, so a partial
    download is never served; the result is memoised on the job.
    """

    if job.audio_path:
        return Path(job.audio_path)
    if job.status not in TERMINAL_STATES:
        if job.phase in _PRE_INGEST_PHASES:
            return None
        # Still fetching: the ingest phase reports done == total once the file is written.
        if job.phase == "ingest" and job.phase_done < job.phase_total:
            return None
    try:
        cached = _load_cached(work_root, job.target)
    except (OSError, ValueError):
        return None
    if cached is None or not path_is_file(cached.original_path):
        return None
    job.audio_path = str(cached.original_path)
    return cached.original_path


def _job_player_html(plan: EmbedPlan, job: Job) -> str:
    """The listen-while-it-works player: the fetched mix itself, played from disk.

    An HTML5 ``<audio>`` over ``GET /jobs/<id>/audio`` (Range-capable, so seeking works) — no
    platform widget, so no third-party outage or bot challenge can break it or masquerade as a
    failed analysis.  Until ingest has written the file the card shows a "getting the audio" cover;
    the status poll hands the page ``audio_url`` as soon as it exists.  A local-file target gets
    the same player.
    """

    name = PLATFORM_NAMES.get(plan.kind)
    link = (
        f' <a rel="noopener" href="{html.escape(plan.link_url)}">Open on {html.escape(name)} ↗</a>'
        if name and plan.link_url
        else ""
    )
    return (
        '<section class="job-player" id="job-player">'
        '<div class="jp-label"><span class="eq live"><i></i><i></i><i></i><i></i></span>'
        "Listen while it works — scrub around to pass the time</div>"
        '<div class="jp-frame loading" id="jp-frame">'
        '<audio id="jp-audio" controls preload="metadata"></audio>'
        '<div class="jp-wait" id="jp-wait">Getting the audio…</div></div>'
        '<div class="jp-fail" id="jp-fail" hidden>'
        "Couldn't play the fetched audio in this browser. "
        f"<b>The analysis below is still running.</b>{link}</div></section>"
    )


def _job_page_html(job: Job) -> bytes:
    label = html.escape(job.display)
    plan = plan_embed_from_url(job.target)
    platform = plan.kind if plan.kind in ("soundcloud", "youtube", "mixcloud") else "file"
    steps = _job_steps(job)
    steps_html = "".join(
        f'<div class="step" data-step="{html.escape(key)}"><div class="k"><i>{n}</i>'
        f"{html.escape(name)}</div><small>{html.escape(sub)}</small></div>"
        for n, (key, name, sub) in enumerate(steps, start=1)
    )
    tiles = (
        '<div class="tiles"><span><b id="t-windows">—</b>windows</span>'
        '<span><b id="t-eta">…</b>time left</span><span><b id="t-elapsed">—</b>elapsed</span>'
        '<span><b id="t-rate">…</b>windows / min</span></div>'
    )
    body = (
        topbar_html(back=True, new=True) + '<main><header class="job-head"><div class="titles">'
        '<p class="eyebrow" id="eyebrow">Analysing</p>'
        f'<h1 id="title">{label}</h1><p class="job-url" id="url" style="display:none">{label}</p>'
        f"{platform_chip(platform)}</div>"
        '<div class="job-actions"><span class="st" id="status">…</span>'
        '<button class="btn danger" id="cancel" type="button">Cancel</button></div></header>'
        + _job_player_html(plan, job)
        + '<section class="scan" id="scan"><div class="scan-top">'
        '<div class="pct" id="pct">0<small>%</small></div>'
        '<div class="phase-line"><div class="phase-name" id="phase-name">Starting</div>'
        '<div class="phase-msg" id="phase-msg"></div></div>' + tiles + "</div>"
        '<div class="cells idle" id="cells" aria-hidden="true"></div>'
        '<p class="flavour" id="flavour"></p></section>'
        '<section class="outcome" id="outcome"><div class="confetti" id="confetti"></div>'
        '<h2 id="o-title"></h2><p id="o-sub"></p><pre class="err" id="o-err"></pre>'
        '<div class="row" id="o-row"></div></section>'
        f'<div class="steps">{steps_html}</div>'
        '<details class="log"><summary>Show the log</summary><pre id="log"></pre></details>'
        + _footer_html()
        + "</main>"
    )
    script = (
        f"var JOB_ID={json.dumps(job.id)};var DISPLAY={json.dumps(job.display)};"
        f"var STEPS={json.dumps(steps)};" + _JOB_JS + _PLAYER_JS
    )
    return _page_shell("Analysing — IDea", body, script)


class _Handler(BaseHTTPRequestHandler):
    server_version = "id-detector-present/1.0"
    work_root: Path
    config: AppConfig | None = None
    job_manager: JobManager | None = None
    analyse_enabled: bool = False

    def log_message(self, *args: object) -> None:  # noqa: D401 - silence default stderr logging
        return

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_file_range(self, path: Path, content_type: str) -> None:
        """Serve a file with HTTP Range support (206) — what makes <audio> seeking work."""

        size = path.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK
        header = self.headers.get("Range") or ""
        match = re.match(r"bytes=(\d*)-(\d*)$", header.strip())
        if match and size:
            first, last = match.group(1), match.group(2)
            if first:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
            elif last:  # a suffix range: the final N bytes
                start = max(0, size - int(last))
            if start > end or start >= size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(native_path(path), "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return  # the browser seeked away or closed the player
                remaining -= len(chunk)

    def _resolve_served_file(self, path: str) -> Path | None:
        """Map a URL path to a file strictly inside ``work_root`` and under a ``present/`` dir."""

        segments = [segment for segment in path.split("/") if segment not in ("", ".")]
        if any(segment == ".." for segment in segments):
            return None
        candidate = self.work_root
        for segment in segments:
            candidate = candidate / segment
        try:
            resolved = candidate.resolve()
            root = self.work_root.resolve()
        except OSError:
            return None
        if root != resolved and root not in resolved.parents:
            return None
        if "present" not in resolved.parts:
            return None
        if resolved.suffix.lower() not in _CONTENT_TYPES:
            return None
        return resolved if path_is_file(resolved) else None

    def _resolve_served_audio(self, path: str) -> Path | None:
        """Map a URL to an audio file strictly inside ``work_root`` (the result page's <audio> src).

        Mirrors :meth:`_resolve_served_file`'s traversal guard, but allows the fetched original
        (under ``ingest/``, not ``present/``) so a persistent result page can play + seek it.
        """

        segments = [segment for segment in path.split("/") if segment not in ("", ".")]
        if any(segment == ".." for segment in segments):
            return None
        candidate = self.work_root
        for segment in segments:
            candidate = candidate / segment
        try:
            resolved = candidate.resolve()
            root = self.work_root.resolve()
        except OSError:
            return None
        if root != resolved and root not in resolved.parents:
            return None
        if resolved.suffix.lstrip(".").casefold() not in _AUDIO_TYPES:
            return None
        return resolved if path_is_file(resolved) else None

    def _send_json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, _CONTENT_TYPES[".json"])

    def _app_active(self) -> bool:
        return self.analyse_enabled and self.job_manager is not None

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        route = self.path.split("?", 1)[0]
        if route in ("/", "/index.html"):
            if self._app_active():
                assert self.job_manager is not None
                body = _home_html(_discover_sets(self.work_root), self.job_manager.recent())
            else:
                body = _index_html(_discover_sets(self.work_root))
            self._send(HTTPStatus.OK, body, _CONTENT_TYPES[".html"])
            return
        if self._app_active() and route == "/new":
            query = parse_qs(urlsplit(self.path).query)
            prefill = (query.get("url") or [""])[0][:2048]
            self._send(HTTPStatus.OK, _new_html(prefill), _CONTENT_TYPES[".html"])
            return
        if self._app_active() and route.startswith("/jobs/"):
            self._handle_job_get(route)
            return
        served_audio = self._resolve_served_audio(route)
        if served_audio is not None:
            # The result page's <audio> points at the fetched original; serve it Range-capable so
            # scrubbing/seeking works (a plain send would force a full download and break seeking).
            self._send_file_range(served_audio, _audio_content_type(served_audio))
            return
        served = self._resolve_served_file(route)
        if served is None:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            return
        if served.name == "index.html" and served.parent.name == "present":
            # A page written by an older build is re-rendered from its artefacts on open.
            ensure_fresh_page(served.parent.parent, config=self.config)
        with open(native_path(served), "rb") as handle:
            body = handle.read()
        self._send(HTTPStatus.OK, body, _CONTENT_TYPES[served.suffix.lower()])

    def _handle_job_get(self, route: str) -> None:
        assert self.job_manager is not None
        segments = route.strip("/").split("/")
        if len(segments) == 2 and _JOB_ID.match(segments[1]):
            job = self.job_manager.get(segments[1])
            if job is None:
                self._send(HTTPStatus.NOT_FOUND, b"unknown job", "text/plain; charset=utf-8")
                return
            self._send(HTTPStatus.OK, _job_page_html(job), _CONTENT_TYPES[".html"])
            return
        if len(segments) == 3 and _JOB_ID.match(segments[1]) and segments[2] == "status":
            job = self.job_manager.get(segments[1])
            if job is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown job"})
                return
            _resolve_job_audio(job, self.work_root)
            self._send_json(HTTPStatus.OK, job.status_dict())
            return
        if len(segments) == 3 and _JOB_ID.match(segments[1]) and segments[2] == "audio":
            job = self.job_manager.get(segments[1])
            audio = _resolve_job_audio(job, self.work_root) if job is not None else None
            if audio is None:
                self._send(HTTPStatus.NOT_FOUND, b"no audio yet", "text/plain; charset=utf-8")
                return
            self._send_file_range(audio, _audio_content_type(audio))
            return
        self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0]
        if self._app_active() and route == "/analyse":
            self._handle_analyse()
            return
        if self._app_active() and route.startswith("/jobs/") and route.endswith("/cancel"):
            self._handle_job_cancel(route)
            return
        if route != "/rescan":
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 8192:
            self._send(HTTPStatus.BAD_REQUEST, b'{"error":"bad length"}', _CONTENT_TYPES[".json"])
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            media_key = str(payload["media_key"])
            trigger = str(payload["trigger"])
            start_ms = int(payload["start_ms"])
            end_ms = int(payload["end_ms"])
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            self._send(HTTPStatus.BAD_REQUEST, b'{"error":"bad request"}', _CONTENT_TYPES[".json"])
            return
        if not _SHA.match(media_key):
            body = b'{"error":"bad media_key"}'
            self._send(HTTPStatus.BAD_REQUEST, body, _CONTENT_TYPES[".json"])
            return
        target = next(
            (item for item in _discover_sets(self.work_root) if item.media_key == media_key), None
        )
        if target is None:
            self._send(HTTPStatus.NOT_FOUND, b'{"error":"unknown set"}', _CONTENT_TYPES[".json"])
            return
        try:
            source = SourceRecord.model_validate_json(
                read_text(target.media_dir / "ingest" / "source.json")
            )
            request = build_rescan_request(
                source=source,
                media_dir=target.media_dir,
                trigger=trigger,
                start_ms=start_ms,
                end_ms=end_ms,
                config=self.config,
            )
            append_rescan_request(target.media_dir, request)
        except (ValueError, OSError) as exc:
            body = json.dumps({"error": str(exc)[:120]}).encode("utf-8")
            self._send(HTTPStatus.BAD_REQUEST, body, _CONTENT_TYPES[".json"])
            return
        body = json.dumps({"queued": True, "id": request.id, "trigger": request.trigger}).encode()
        self._send(HTTPStatus.OK, body, _CONTENT_TYPES[".json"])

    def _read_body(self, limit: int = 8192) -> bytes | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > limit:
            return None
        return self.rfile.read(length) if length else b""

    def _handle_analyse(self) -> None:
        assert self.job_manager is not None
        raw = self._read_body()
        if raw is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad length"})
            return
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        wants_json = content_type == "application/json"
        try:
            if wants_json:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                url = str(payload.get("url", ""))
                profile = payload.get("profile")
                profile = str(profile) if profile is not None else None
                acquire = bool(payload.get("acquire"))
                build_index = bool(payload.get("build_index"))
                known_tracklist = payload.get("known_tracklist")
                upload_consent = bool(payload.get("upload_consent"))
            else:
                form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
                url = (form.get("url") or [""])[0]
                profile = (form.get("profile") or [None])[0]
                acquire = bool(form.get("acquire"))
                build_index = bool(form.get("build_index"))
                known_tracklist = (form.get("known_tracklist") or [""])[0]
                upload_consent = bool(form.get("upload_consent"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad request"})
            return
        if profile is not None and profile not in _PROFILES:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "unknown profile"})
            return
        # A pasted tracklist is an optional hint seed; blank/whitespace means "audio only".  Cap it
        # so an oversized paste can never balloon a job (a real tracklist is a few KB at most).
        if not isinstance(known_tracklist, str) or not known_tracklist.strip():
            known_tracklist = None
        elif len(known_tracklist) > 64_000:
            known_tracklist = known_tracklist[:64_000]
        try:
            job_id = self.job_manager.submit(
                url,
                profile,
                acquire=acquire,
                build_index=build_index,
                known_tracklist=known_tracklist,
                upload_consent=upload_consent,
            )
        except TargetValidationError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        location = f"/jobs/{job_id}"
        if wants_json:
            self._send_json(HTTPStatus.OK, {"id": job_id, "location": location})
            return
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_job_cancel(self, route: str) -> None:
        assert self.job_manager is not None
        segments = route.strip("/").split("/")
        if len(segments) != 3 or not _JOB_ID.match(segments[1]) or segments[2] != "cancel":
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            return
        cancelled = self.job_manager.cancel(segments[1])
        if self.job_manager.get(segments[1]) is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown job"})
            return
        self._send_json(HTTPStatus.OK, {"cancelled": cancelled})


def make_server(
    work_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    config: AppConfig | None = None,
    job_manager: JobManager | None = None,
) -> ThreadingHTTPServer:
    """Create a ``127.0.0.1``-bound threading server (never binds a routable interface).

    When ``job_manager`` is supplied the home page becomes the analyse form and the ``/analyse`` /
    ``/jobs/<id>`` routes are enabled; without it the server stays the read-only Stage 7 index.
    """

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("the present server only binds the loopback interface")

    handler = type(
        "BoundHandler",
        (_Handler,),
        {
            "work_root": work_root.resolve(),
            "config": config,
            "job_manager": job_manager,
            "analyse_enabled": job_manager is not None,
        },
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


@dataclass
class RunningServer:
    server: ThreadingHTTPServer
    thread: threading.Thread

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[0], self.server.server_address[1]
        return f"http://{host}:{port}"

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def serve_in_background(
    work_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    config: AppConfig | None = None,
    job_manager: JobManager | None = None,
) -> RunningServer:
    """Start the server on a background thread (port 0 picks a free port). For tests and the CLI."""

    server = make_server(work_root, host=host, port=port, config=config, job_manager=job_manager)
    thread = threading.Thread(target=server.serve_forever, name="present-server", daemon=True)
    thread.start()
    return RunningServer(server, thread)

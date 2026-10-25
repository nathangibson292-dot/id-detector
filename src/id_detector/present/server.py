"""Presentation fragments for the local web app, and the rescan-queue helpers.

The stdlib HTTP server that used to live here is retired (4a-ii): ``idea serve`` runs the one
FastAPI application in :mod:`idea_web`, which renders its pages from these fragments.

There is no ``POST /rescan``: §2.5 removed the rescan control **and** its route, because on real
mixes another generation buys zero recall, adds phantom rows and costs hours.  The rescan queue
helpers below remain for ``idea rescan``, which reads a queue a human wrote — nothing over HTTP can
fill it.

The library lists analysed sets by their ``source.json`` title only — never a username, never any
comment text, and never the submitted target (a private-share link is a secret).
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    EpisodesFile,
    RescanRequestRecord,
    SourceRecord,
    compose_natural_key,
    make_id,
)
from id_detector.ingest import _load_ingest_cached
from id_detector.io import (
    atomic_write_bytes,
    canonical_json_bytes,
    native_path,
    path_is_file,
    path_mtime,
    read_text,
    sha256_file,
)
from id_detector.present.bundles import result_dir, run_metadata
from id_detector.present.exports import (
    BADGE_ORDER,
    ProjectedSummary,
    _format_time,
    comment_credit_text,
    read_projected_summary,
)
from id_detector.present.genre_css import GENRE_CSS
from id_detector.present.page import EmbedPlan, plan_embed_from_url
from id_detector.present.refresh import ensure_fresh_page
from id_detector.present.theme import (
    LIGHT_CSS,
    PLATFORM_NAMES,
    head_html,
    platform_chip,
    topbar_html,
)
from id_detector.providers.base import AppConfig
from id_detector.rescan import policy_for_trigger, priority_for_trigger
from id_detector.webapp.jobs import (
    STAGE_LABELS,
    TERMINAL_STATES,
    Job,
)

_SHA = re.compile(r"^[0-9a-f]{64}$")
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
#: Loopback CSRF (U-F5): every POST must come from this server's own pages.  The per-server
#: token is served by ``GET /csrf`` and travels back as a form field or a request header.
_CSRF_HEADER = "X-CSRF-Token"
_CSRF_FIELD = "csrf_token"
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
    analysed_at: float = 0.0


@dataclass(frozen=True)
class FailedRun:
    """One durable terminal failure, with the cause and the money it really cost (U-F15/F33)."""

    source_key: str
    media_key: str
    media_dir: Path
    title: str
    platform: str
    analysed_at: float
    #: The frozen §2.3.5 terminal status and machine reason of the attempt.
    status: str = "failed"
    reason: str | None = None
    #: The last pipeline stage that completed, as a plain phrase ("listening to the set").
    last_stage: str | None = None
    #: Money settled for the attempt, in USD cents; ``None`` when the journal did not record it.
    usd_e2_spent: int | None = None
    #: Whether an earlier, still-openable result exists for the same mix.  When it does the attempt
    #: is a note beside that result, not a removable library object of its own — deleting it must
    #: never take the good result with it.
    has_result: bool = False


def _iso_epoch(value: object) -> float | None:
    """Parse one ISO-8601 journal/manifest timestamp to epoch seconds, or ``None``."""

    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _run_started_at(media_dir: Path) -> float | None:
    """When the run behind this mix's shown result started, from its frozen bundle manifest."""

    try:
        return _iso_epoch(run_metadata(media_dir).get("started_at"))
    except (OSError, ValueError, KeyError):  # pragma: no cover - defensive
        return None


def _discover_sets(work_root: Path) -> list[AnalysedSet]:
    dated: list[tuple[float, AnalysedSet]] = []
    if not work_root.is_dir():
        return []
    for source_json in work_root.glob("*/*/ingest/source.json"):
        index_html = result_dir(source_json.parents[1]) / "index.html"
        if not path_is_file(index_html):
            continue
        try:
            source = SourceRecord.model_validate_json(read_text(source_json))
        except (ValueError, OSError):
            continue
        # "Analysed at" = when the run that produced the SHOWN result started, taken from that
        # bundle's frozen manifest (U-F24/§2.5 "analysed date").  The ingest timestamp is only a
        # fallback for a pre-bundle result: it is when the audio was fetched, which for a mix
        # re-analysed later is not when it was analysed.  Both are frozen, so opening a mix never
        # rewrites its date and never reorders the library.  Newest first.
        analysed_at = _run_started_at(source_json.parents[1]) or path_mtime(source_json)
        dated.append(
            (
                analysed_at,
                AnalysedSet(
                    source_key=source.source_key,
                    media_key=source.media_key,
                    media_dir=source_json.parents[1],
                    title=source.title or "(untitled set)",
                    platform=source.platform,
                    analysed_at=analysed_at,
                ),
            )
        )
    dated.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in dated]


def _fresh_sets(work_root: Path, config: AppConfig) -> list[AnalysedSet]:
    """Refresh stale result bundles before their canonical summaries reach the library."""

    sets = _discover_sets(work_root)
    for item in sets:
        ensure_fresh_page(item.media_dir, config=config)
    return sets


#: Terminal §2.3.5 statuses that produced no result: each one is a failed attempt a user tried and
#: deserves to see a record of (U-F33).  ``cancelled`` is included — a run the user stopped after
#: paid work had begun still cost money, so hiding it hides the cost.
_TERMINAL_FAILURES = frozenset(
    {"failed", "provider_unavailable", "budget_exhausted", "source_changed", "cancelled"}
)


#: A failed attempt the owner removed from the library while an older result survives: the
#: attempt's card goes, the result and the journal stay.  The marker names the journal length it
#: hid, so a later attempt (a new journal entry) shows its own card again.
ATTEMPT_DISMISSED = "library-dismissed.json"


def _attempt_dismissed(media_dir: Path, entries: int) -> bool:
    marker = media_dir / ATTEMPT_DISMISSED
    if not path_is_file(marker):
        return False
    try:
        return json.loads(read_text(marker)).get("journal_entries") == entries
    except (OSError, ValueError, AttributeError):
        return False


def dismiss_failed_attempt(media_dir: Path) -> bool:
    """Hide this media directory's latest failed attempt from the library; never deletes."""

    journal = media_dir / "invocations.jsonl"
    try:
        entries = [line for line in read_text(journal).splitlines() if line.strip()]
    except OSError:
        return False
    if not entries:
        return False
    (media_dir / ATTEMPT_DISMISSED).write_text(
        json.dumps({"journal_entries": len(entries)}), encoding="utf-8"
    )
    return True


def _discover_failed_runs(work_root: Path) -> list[FailedRun]:
    """Find durable terminal attempts, whether or not an older result exists for the same mix.

    U-F33: skipping any media directory that holds *a* result made a failed Deep attempt on a mix
    with an older Free bundle completely invisible — the run the user actually just paid for. The
    latest journal entry decides: a terminal failure is shown, and when a result also exists the
    card says so and offers no delete of its own (removing it must not take the result with it).
    """

    failed: list[FailedRun] = []
    for journal in work_root.glob("*/*/invocations.jsonl"):
        media_dir = journal.parent
        try:
            entries = [json.loads(line) for line in read_text(journal).splitlines() if line.strip()]
            latest = entries[-1]
        except (OSError, ValueError, IndexError, TypeError):
            continue
        if latest.get("status") not in _TERMINAL_FAILURES:
            continue
        if _attempt_dismissed(media_dir, len(entries)):
            continue
        # The record's own keys are authoritative; the directory names are the fallback for a run
        # that failed before ``ingest/source.json`` was written, and must still be well-formed
        # because they are echoed back into a delete form.
        source_key, media_key = media_dir.parent.name, media_dir.name
        title, platform = "Unfinished mix", "file"
        source_path = media_dir / "ingest" / "source.json"
        identified = False
        if path_is_file(source_path):
            try:
                source = SourceRecord.model_validate_json(read_text(source_path))
                title = source.title or title
                platform = source.platform
                source_key, media_key = source.source_key, source.media_key
                identified = True
            except (OSError, ValueError):
                identified = False
        if not identified and (not _SHA.fullmatch(source_key) or not _SHA.fullmatch(media_key)):
            continue
        analysed_at = (
            _iso_epoch(latest.get("finished_at"))
            or _iso_epoch(latest.get("started_at"))
            or path_mtime(journal)
        )
        spent = latest.get("usd_e2_spent")
        timings = latest.get("timings") or {}
        stage = next(
            (label for key, label in reversed(STAGE_LABELS) if key in timings),
            None,
        )
        failed.append(
            FailedRun(
                source_key,
                media_key,
                media_dir,
                title,
                platform,
                analysed_at,
                status=str(latest.get("status") or "failed"),
                reason=str(latest["reason"]) if latest.get("reason") else None,
                last_stage=stage,
                usd_e2_spent=int(spent) if isinstance(spent, int) else None,
                has_result=path_is_file(result_dir(media_dir) / "index.html"),
            )
        )
    return sorted(failed, key=lambda item: item.analysed_at, reverse=True)


def _set_summary(item: AnalysedSet) -> ProjectedSummary | None:
    """This mix's figures, read straight off its published canonical projection.

    ``read_projected_summary`` is the one reader of a published ``tracklist.json``, shared with the
    completion screen, so the card and the result page cannot print two different track counts
    (U-F2) and a crowd row cannot move the card's confidence bar (U-F13).
    """

    return read_projected_summary(result_dir(item.media_dir) / "tracklist.json")


_BADGE_ORDER = BADGE_ORDER


def _conf_mini_html(summary: ProjectedSummary) -> str:
    """The card's confidence bar: audio-supported rows only, worded to the actual numbers.

    "Mostly likely" is only said when likely really is a majority; a plurality is described as the
    mix it is.  Crowd rows are named separately — they are comments, never audio evidence.
    """

    badges = summary.badges
    total = summary.audio_tracks
    comment_credit = comment_credit_text(summary.comment_named, summary.comment_only)
    crowd_note = (
        f'<span class="conf-text">{html.escape(comment_credit)}</span>' if comment_credit else ""
    )
    if not total:
        return crowd_note
    bars = "".join(
        f'<i class="c-{key}" style="width:{badges[key] * 100.0 / total:.2f}%"></i>'
        for key in _BADGE_ORDER
        if badges.get(key)
    )
    title = " · ".join(f"{badges[key]} {key}" for key in _BADGE_ORDER if badges.get(key))
    majority = summary.majority_badge()
    wording = f"mostly {majority[0]}" if majority else f"mixed — {title}"
    return (
        f'<span class="conf-mini" role="img" aria-label="{html.escape(title)}">{bars}</span>'
        f'<span class="conf-text">{html.escape(wording)}</span>{crowd_note}'
    )


def _hero_wave() -> str:
    """The home hero's backdrop: a wide, quiet equaliser of a mix's energy (decorative only).

    Heights follow a fixed curve rather than randomness, so the page is byte-stable between
    renders; each bar breathes on its own delay, and stands still under reduced motion.
    """

    import math

    stops = ((255, 77, 148), (139, 92, 246), (34, 211, 238))  # the brand gradient
    bars = []
    count = 56
    for index in range(count):
        energy = 0.5 + 0.32 * math.sin(index / 4.1) + 0.18 * math.sin(index / 1.7 + 1.3)
        height = max(10, min(100, round(energy * 100)))
        at = index / (count - 1) * 2
        low, high = stops[int(min(at, 1.999))], stops[int(min(at, 1.999)) + 1]
        share = at - int(min(at, 1.999))
        colour = "#" + "".join(
            f"{round(a + (b - a) * share):02x}" for a, b in zip(low, high, strict=True)
        )
        bars.append(f'<i style="--h:{height}%;--d:{(index * 137) % 1100}ms;--c:{colour}"></i>')
    return f'<div class="hero-wave" aria-hidden="true">{"".join(bars)}</div>'


HERO_WAVE = _hero_wave()


def _index_html(sets: list[AnalysedSet]) -> bytes:
    """Read-only mode home: the mixes library with no analyse form (Stage 7 index)."""

    body = (
        topbar_html(back=False, new=False)
        + '<main class="home"><header class="hero-home compact"><h1>Analysed sets</h1>'
        '<p class="lede">Every mix analysed under this work root — open one to explore its '
        "tracklist. This server is read-only.</p></header>"
        + '<section class="library">'
        + _library_stats_html(sets)
        + _mixes_block(sets, allow_new=False)
        + "</section>"
        + _footer_html()
        + "</main>"
    )
    return _page_shell("ID'er — analysed sets", body)


# --------------------------------------------------------------------------------------------------
# Web-app pages (self-contained inline HTML/CSS/JS; no usernames or comment text)
# --------------------------------------------------------------------------------------------------
_APP_CSS = (
    """
.eyebrow{display:inline-flex;align-items:center;gap:10px;font:700 11px/1 var(--display);
letter-spacing:.16em;text-transform:uppercase;color:var(--dim);margin:0 0 var(--s4)}
/* home hero + the drop-a-link form */
.hero-home{position:relative;padding:var(--s9) 0 var(--s7);max-width:820px;
animation:rise var(--t3) var(--ease) both}
.hero-home.compact{padding:var(--s7) 0 var(--s3)}
.hero-home h1{font:800 clamp(40px,6.4vw,76px)/.98 var(--display);letter-spacing:-.045em;
margin:0 0 var(--s5)}
.hero-home.compact h1{font-size:clamp(30px,4.4vw,44px)}
.hero-home .grad{padding-right:.04em}
.lede{color:var(--muted);font-size:16px;line-height:1.6;max-width:58ch;margin:0 0 var(--s7)}
.lede.reassurance{font-size:13px;color:var(--dim);margin:var(--s4) 0 0}
.hero-wave{position:absolute;z-index:-1;top:var(--s7);right:calc(-1 * min(340px,30vw));
width:min(620px,52vw);height:240px;display:flex;align-items:center;gap:4px;opacity:.9;
pointer-events:none;-webkit-mask-image:linear-gradient(90deg,transparent,#000 30%,#000 70%,
transparent);mask-image:linear-gradient(90deg,transparent,#000 30%,#000 70%,transparent)}
.hero-wave i{flex:1;height:var(--h);min-width:2px;border-radius:3px;background:var(--c);
opacity:.55;transform-origin:center;
animation:breathe 2.6s ease-in-out var(--d) infinite alternate}
@keyframes breathe{from{transform:scaleY(.55)}to{transform:scaleY(1)}}
.dropform{margin:0}
.urlbox{display:flex;align-items:center;gap:var(--s2);padding:7px 7px 7px 10px;
background:var(--card);border:1px solid var(--line2);border-radius:18px;
box-shadow:var(--sh2),0 40px 80px -50px #8b5cf6aa;
transition:border-color var(--t2) var(--ease),box-shadow var(--t2) var(--ease)}
.urlbox:hover{border-color:var(--line3)}
.urlbox:focus-within{border-color:var(--accent);
box-shadow:0 0 0 4px var(--accent-soft),var(--sh2),0 40px 80px -40px #8b5cf6cc}
.plat-ind{display:inline-flex;align-items:center;gap:7px;height:36px;
font:600 12px/1 var(--text);color:var(--muted);padding:0 11px;border-radius:10px;
background:var(--hover);white-space:nowrap;transition:color var(--t2) var(--ease)}
.plat-ind.on{color:var(--fg)}
.urlbox input{flex:1;min-width:0;height:48px;padding:0 8px;border:0;background:transparent;
color:var(--fg);font:16px/1.3 var(--text);outline:none}
.urlbox input::placeholder{color:var(--dim)}
.urlbox input:focus-visible{outline:none}
.urlbox .btn svg{width:16px;height:16px}
.form-error{color:var(--bad);font-weight:600;margin:var(--s3) 4px 0}.form-error:empty{display:none}
.modes{margin-top:var(--s4)}
.seg{display:grid;grid-template-columns:1fr 1fr;gap:var(--s3)}
.segopt{position:relative;cursor:pointer;display:block}
.segopt input{position:absolute;opacity:0;inset:0;margin:0;cursor:pointer}
.segopt span{display:block;height:100%;padding:14px 16px;border:1px solid var(--line);
border-radius:var(--r2);background:var(--card);
transition:border-color var(--t1) var(--ease),background-color var(--t1) var(--ease),
box-shadow var(--t2) var(--ease)}
.segopt:hover span{border-color:var(--line2)}
.segopt input:checked+span{border-color:var(--accent-line);
background:linear-gradient(135deg,var(--accent-soft),transparent 70%),var(--card);
box-shadow:0 0 0 1px var(--accent-line) inset}
.segopt input:focus-visible+span{outline:2px solid var(--accent);outline-offset:2px}
.segopt b{display:flex;align-items:center;gap:8px;font-size:14px}
.mode-dot{width:9px;height:9px;border-radius:50%;border:2px solid var(--line3);flex:none;
transition:background-color var(--t1),border-color var(--t1)}
.segopt input:checked+span .mode-dot{border-color:var(--accent);background:var(--accent);
box-shadow:0 0 0 3px var(--accent-soft)}
.segopt small,.tog small,.tlbox-h small{display:block;color:var(--muted);font-size:12.5px;
margin-top:4px;line-height:1.45}
.opts{margin-top:var(--s3)}
.opts>summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;gap:10px;
height:36px;font:600 13px/1 var(--text);color:var(--muted);padding:0 12px;border-radius:10px;
border:1px solid transparent;transition:color var(--t1),border-color var(--t1),
background-color var(--t1)}
.opts>summary::-webkit-details-marker{display:none}
.opts>summary::before{content:"";width:6px;height:6px;border-right:1.6px solid currentColor;
border-bottom:1.6px solid currentColor;transform:rotate(-45deg);transition:transform var(--t2)}
.opts[open]>summary::before{transform:rotate(45deg)}
.opts>summary:hover,.opts[open]>summary{color:var(--fg);background:var(--hover)}
.opt-sum{font-weight:500;color:var(--dim)}
.opt-grid{display:grid;grid-template-columns:1fr 1fr;gap:var(--s3);margin-top:var(--s3);
animation:rise var(--t2) var(--ease) both}
.tog{position:relative;display:flex;align-items:flex-start;gap:var(--s3);padding:14px 16px;
border:1px solid var(--line);border-radius:var(--r2);background:var(--card);cursor:pointer}
.tog input{position:absolute;opacity:0;width:0;height:0}
.tog b{font-size:13.5px}
.sw{flex:none;width:36px;height:22px;border-radius:999px;background:var(--line2);
position:relative;margin-top:1px;transition:background-color var(--t2) var(--ease)}
.sw::after{content:"";position:absolute;top:3px;left:3px;width:16px;height:16px;
border-radius:50%;background:#fff;box-shadow:0 1px 3px #0005;
transition:transform var(--t2) var(--ease)}
.tog input:checked~.sw{background:var(--violet)}
.tog input:checked~.sw::after{transform:translateX(14px)}
.tog input:focus-visible~.sw{outline:2px solid var(--accent);outline-offset:2px}
.tlbox{grid-column:1/-1;padding:14px 16px;border:1px solid var(--line);border-radius:var(--r2);
background:var(--card);display:block}
.tlbox-h{display:block;margin-bottom:var(--s2)}.tlbox-h b{font-size:13.5px}
.tlbox textarea{display:block;width:100%;resize:vertical;min-height:84px;padding:10px 12px;
border:1px solid var(--line);border-radius:10px;background:var(--sunken);color:var(--fg);
font:12.5px/1.6 var(--mono);outline:none;
transition:border-color var(--t1),box-shadow var(--t1)}
.tlbox textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.tlbox textarea::placeholder{color:var(--dim)}
.opt-grid>.tog{grid-column:1/-1}
.consent{grid-column:1/-1;border-color:color-mix(in srgb,var(--warn) 40%,transparent);
background:color-mix(in srgb,var(--warn) 6%,var(--card))}
.consent[hidden]{display:none}
/* start-up upkeep note */
#upkeep-status:empty{display:none}
.upkeep{margin:var(--s6) 0 0}
.upkeep-note{display:flex;align-items:center;gap:10px;color:var(--dim);font-size:13px;margin:0}
.upkeep-dot{width:8px;height:8px;border-radius:50%;background:var(--cyan);flex:none;
animation:blink 1.4s ease-in-out infinite}
.upkeep-dot.done{background:var(--ok);animation:none}
.upkeep-box{border:1px solid var(--line);border-radius:var(--r2);background:var(--card);
font-size:13px;color:var(--muted)}
.upkeep-box>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:10px;
padding:12px 16px;border-radius:var(--r2)}
.upkeep-box>summary::-webkit-details-marker{display:none}
.upkeep-box>summary b{flex:1;min-width:0;font-weight:550;color:var(--fg)}
.upkeep-more{color:var(--dim);font-size:12px;font-weight:600;white-space:nowrap}
.upkeep-box[open] .upkeep-more{color:var(--fg)}
.upkeep-list{margin:0;padding:0 16px 0 34px;display:grid;gap:8px;line-height:1.5}
.upkeep-foot{margin:12px 16px 14px 34px;color:var(--dim);font-size:12.5px}
/* activity: what is running now */
.acts{list-style:none;padding:0;margin:0;display:grid;gap:var(--s3)}
.act{border:1px solid var(--line);border-radius:var(--r3);background:var(--card);
overflow:hidden;box-shadow:var(--sh1);display:flex;align-items:center;
transition:border-color var(--t1) var(--ease)}
.act:hover{border-color:var(--line2)}
.act-link{flex:1;min-width:0;display:block;padding:14px 18px;color:inherit}
.act-link:hover{text-decoration:none}
.act-actions{padding:0 14px 0 0}
.act-row{display:flex;align-items:center;gap:var(--s3);min-width:0}
.act-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
font-weight:650;font-size:14.5px}
.act-phase{color:var(--muted);font-size:12.5px;margin-top:8px;font-variant-numeric:tabular-nums}
.bar{height:6px;border-radius:4px;background:var(--track);overflow:hidden;margin-top:10px}
.bar>span{display:block;height:100%;background:var(--grad);width:0;border-radius:4px;
transition:width .8s var(--ease)}
.act[data-status="failed"] .bar>span{background:var(--bad)}
.act[data-status="cancelled"] .bar>span{background:var(--dim)}
.act[data-status="waiting"] .bar>span{background:var(--warn)}
.act[data-status="running"] .bar>span{
background:linear-gradient(90deg,#ff4d94,#8b5cf6,#22d3ee,#8b5cf6,#ff4d94);
background-size:200% 100%;animation:flow 2.4s linear infinite}
.link-danger{border:0;background:transparent;color:var(--muted);cursor:pointer;height:32px;
padding:0 10px;border-radius:8px;font:600 12.5px/1 var(--text);
transition:color var(--t1),background-color var(--t1)}
.link-danger:hover{color:var(--bad);background:color-mix(in srgb,var(--bad) 10%,transparent)}
/* library */
.library{animation:fade var(--t3) var(--ease) both}
.lib-bar{display:flex;align-items:stretch;gap:var(--s4);flex-wrap:wrap;margin:0 0 var(--s5)}
.stats{display:flex;flex:1 1 auto;background:var(--card);border:1px solid var(--line);
border-radius:var(--r3);overflow:hidden;box-shadow:var(--sh1)}
.stat{flex:1;padding:14px 20px;min-width:0;display:flex;align-items:center}
.stat+.stat{border-left:1px solid var(--line)}
.stat .big{font:760 26px/1 var(--display);letter-spacing:-.035em;
font-variant-numeric:tabular-nums;white-space:nowrap}
.stat small{display:block;color:var(--muted);font-size:12px;margin-top:6px}
.lib-tools{display:flex;align-items:center;gap:var(--s2);flex:0 1 420px}
.lib-search{flex:1;display:flex;align-items:center;gap:8px;height:44px;padding:0 14px;
border:1px solid var(--line2);border-radius:var(--r2);background:var(--card);
color:var(--dim);transition:border-color var(--t1),box-shadow var(--t1)}
.lib-search:focus-within{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.lib-search svg{width:16px;height:16px;flex:none}
.lib-search input{flex:1;min-width:0;border:0;background:transparent;color:var(--fg);
height:100%;outline:none;font-size:14px}
.lib-search input::placeholder{color:var(--dim)}
.lib-filter{display:inline-flex;gap:2px;padding:3px;border:1px solid var(--line2);
border-radius:var(--r2);background:var(--card);height:44px}
.lib-filter button{border:0;background:transparent;color:var(--muted);border-radius:9px;
padding:0 12px;font:600 12.5px/1 var(--text);cursor:pointer;
transition:color var(--t1),background-color var(--t1)}
.lib-filter button:hover{color:var(--fg)}
.lib-filter button.on{background:var(--hover2);color:var(--fg)}
.lib-none{color:var(--muted);text-align:center;padding:var(--s7) 0}
.lib-none[hidden]{display:none}
.mixes{list-style:none;padding:0;margin:0;display:grid;
grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:var(--s4)}
.mix{position:relative;border:1px solid var(--line);border-radius:var(--r3);background:var(--card);
overflow:hidden;box-shadow:var(--sh1);display:flex;flex-direction:column;
transition:transform var(--t2) var(--ease),border-color var(--t2) var(--ease),
box-shadow var(--t2) var(--ease)}
.mix:hover{transform:translateY(-2px);border-color:var(--line2);box-shadow:var(--sh2),
0 24px 50px -30px #8b5cf699}
.mix[hidden]{display:none}
.mix-link{display:flex;flex-direction:column;flex:1;color:inherit;border-radius:var(--r3)}
.mix-link:hover{text-decoration:none}
.mix-link:focus-visible{outline-offset:-2px}
.mix-strip{position:relative;height:46px;background:var(--sunken);
border-bottom:1px solid var(--line);
background-image:repeating-linear-gradient(90deg,transparent 0 calc(10% - 1px),var(--line)
calc(10% - 1px) 10%)}
.mix-strip i{position:absolute;top:12px;bottom:12px;border-radius:3px;
background:color-mix(in srgb,var(--sc) 70%,transparent);--sc:var(--unclear);
box-shadow:inset 2px 0 0 var(--sc)}
.mix-strip .s-verified{--sc:var(--verified)}.mix-strip .s-likely{--sc:var(--likely)}
.mix-strip .s-possible{--sc:var(--possible)}.mix-strip .s-unclear{--sc:var(--unclear)}
.mix-strip .s-crowd{background:transparent;border:1px dashed var(--accent);box-shadow:none}
.mix-strip .s-gap{top:0;bottom:0;border-radius:0;box-shadow:none;
background:repeating-linear-gradient(135deg,color-mix(in srgb,var(--gap) 22%,transparent) 0 4px,
transparent 4px 8px)}
.mix:hover .mix-strip i:not(.s-gap){background:var(--sc)}
.mix-strip.empty::after{content:"";position:absolute;inset:20px 16px;border-radius:3px;
background:var(--track)}
.mix-body{padding:16px 18px 18px;display:flex;flex-direction:column;flex:1}
.mix-top{display:flex;align-items:center;gap:var(--s2);margin-bottom:var(--s3)}
.mix-top .mono{margin-left:auto}
.scan-badge{display:inline-flex;align-items:center;height:22px;padding:0 8px;border-radius:6px;
font:700 10px/1 var(--display);letter-spacing:.1em;text-transform:uppercase;color:var(--muted);
border:1px solid var(--line2)}
.scan-badge.deep{color:var(--accent);border-color:var(--accent-line);background:var(--accent-soft)}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--muted);font-size:12px}
.mix-title{font:700 17px/1.3 var(--display);letter-spacing:-.015em;margin-bottom:6px;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
overflow-wrap:anywhere}
.mix-meta{display:flex;align-items:center;gap:var(--s3);color:var(--muted);font-size:12.5px;
margin-top:auto;padding-top:var(--s3)}
.mix-meta>span:first-child{white-space:nowrap}
.mix-meta{flex-wrap:wrap;row-gap:6px}
.mix-meta .go{order:2}
.mix-meta .conf-text{order:3;flex-basis:100%;margin:0}
.mix-meta b{color:var(--fg)}
.conf-mini{display:flex;flex:1;height:6px;border-radius:4px;overflow:hidden;background:var(--track);
gap:2px;min-width:40px}
.conf-mini i{display:block;height:100%}
.conf-text{color:var(--muted);font-size:12px;margin-top:2px}
.mix-actions{position:absolute;top:8px;right:8px;opacity:0;transition:opacity var(--t1)}
.mix:hover .mix-actions,.mix:focus-within .mix-actions{opacity:1}
.mix-actions .link-danger{background:var(--raise);border:1px solid var(--line2);height:28px}
.c-verified{background:var(--verified)}.c-likely{background:var(--likely)}
.c-possible{background:var(--possible)}.c-unclear{background:var(--unclear)}
.go{color:var(--dim);font-size:16px;line-height:1;transition:transform var(--t2) var(--ease),
color var(--t2)}
.mix:hover .go{color:var(--fg);transform:translateX(3px)}
.mix.failed{border-color:color-mix(in srgb,var(--bad) 30%,var(--line))}
.mix.failed::before{content:"";height:3px;background:var(--bad);opacity:.8}
.mix.failed .mix-actions{position:static;opacity:1;padding:0 12px 12px;display:flex;
justify-content:flex-end}
.mix.failed .mix-actions .link-danger{background:transparent;border:0}
.mix.failed .cause{color:var(--fg);margin:2px 0 6px}
.mix.failed .mix-top{padding-right:40px}
.mix-menu{position:absolute;top:8px;right:8px;z-index:3}
.mix-menu>summary{list-style:none;width:34px;height:34px;display:grid;place-items:center;
border-radius:10px;border:1px solid var(--line2);color:var(--fg);cursor:pointer;font-size:20px;
line-height:1;background:color-mix(in srgb,var(--card) 82%,transparent);backdrop-filter:blur(8px);
transition:background var(--t1)}
.mix-menu>summary::-webkit-details-marker{display:none}
.mix-menu>summary:hover,.mix-menu[open]>summary{background:var(--raise)}
.mm-pop{position:absolute;right:0;top:40px;width:240px;background:var(--card);
border:1px solid var(--line2);border-radius:var(--r3);box-shadow:var(--sh2);padding:6px;
animation:rise var(--t2) var(--ease) both}
.mm-item{display:block;width:100%;border:0;background:transparent;color:var(--bad);
padding:10px 12px;border-radius:8px;font:inherit;font-size:14px;font-weight:600;cursor:pointer;
text-align:left}
.mm-item:hover,.mm-item:focus-visible{background:color-mix(in srgb,var(--bad) 12%,transparent)}
.mm-confirm{display:none;padding:8px 8px 4px;margin:0}
.mm-pop.asking .mm-confirm{display:block}.mm-pop.asking .mm-ask{display:none}
.mm-confirm p{margin:0 0 12px;font-size:13px;line-height:1.45;color:var(--muted)}
.mm-confirm p b{display:block;color:var(--fg);font-size:14px;margin-bottom:2px}
.mm-row{display:flex;gap:8px;justify-content:flex-end}
.mm-no,.mm-yes{min-height:36px;padding:0 14px;border-radius:9px;font:inherit;font-size:13px;
font-weight:650;cursor:pointer}
.mm-no{background:transparent;border:1px solid var(--line2);color:var(--fg)}
.mm-yes{background:var(--bad);border:1px solid var(--bad);color:#fff}
.mm-yes:hover{filter:brightness(1.08)}
.empty{border:1px dashed var(--line2);border-radius:var(--r3);padding:var(--s8) var(--s5);
text-align:center;color:var(--muted);background:linear-gradient(180deg,var(--hover),transparent)}
.empty b{display:block;color:var(--fg);font:700 19px/1.2 var(--display);margin:var(--s4) 0 6px}
.empty p{margin:0}
.empty-eq{display:inline-flex;align-items:flex-end;gap:4px;height:34px}
.empty-eq i{width:6px;border-radius:3px;background:var(--grad);height:40%;
animation:eq2 1.4s ease-in-out infinite}
.empty-eq i:nth-child(2){animation-delay:.2s}.empty-eq i:nth-child(3){animation-delay:.4s}
.empty-eq i:nth-child(4){animation-delay:.6s}.empty-eq i:nth-child(5){animation-delay:.8s}
@keyframes eq2{0%,100%{height:30%}50%{height:100%}}
/* progress page */
.job-head{display:flex;align-items:flex-start;gap:var(--s4);flex-wrap:wrap;
padding:var(--s7) 0 var(--s5)}
.job-head .titles{flex:1;min-width:0}
.job-head .eyebrow{margin-bottom:var(--s3)}
.job-head h1{font:780 clamp(28px,4vw,44px)/1.06 var(--display);letter-spacing:-.035em;
margin:0 0 var(--s3);overflow-wrap:anywhere}
.job-url{color:var(--muted);font-size:13px;overflow-wrap:anywhere}
.job-actions{display:flex;align-items:center;gap:var(--s3);padding-top:2px}
.scan{position:relative;background:var(--card);border:1px solid var(--line);
border-radius:var(--r4);padding:var(--s6) var(--s7) var(--s5);box-shadow:var(--sh2);
overflow:hidden;animation:rise var(--t3) var(--ease) both}
.scan::before{content:"";position:absolute;inset:-1px -1px auto;height:180px;pointer-events:none;
background:radial-gradient(60% 100% at 10% 0%,var(--glow1),transparent 70%)}
.stages{list-style:none;margin:0 0 var(--s6);padding:0;display:grid;
grid-template-columns:repeat(4,1fr);position:relative}
.stage{position:relative;display:flex;align-items:center;gap:10px;color:var(--dim);
font:650 13px/1 var(--display);letter-spacing:-.005em}
.stage:not(:last-child)::after{content:"";flex:1;height:2px;margin:0 12px 0 2px;border-radius:2px;
background:var(--track)}
.stage i{flex:none;width:22px;height:22px;border-radius:50%;border:2px solid var(--line3);
display:grid;place-items:center;transition:background-color var(--t2),border-color var(--t2)}
.stage.done{color:var(--fg)}
.stage.done i{border-color:var(--ok);background:var(--ok)}
.stage.done i::after{content:"";width:8px;height:4px;border-left:2px solid var(--on-accent);
border-bottom:2px solid var(--on-accent);transform:translateY(-1px) rotate(-45deg)}
.stage.done:not(:last-child)::after{background:linear-gradient(90deg,var(--ok),
color-mix(in srgb,var(--ok) 40%,transparent))}
.stage.active{color:var(--fg)}
.stage.active i{border-color:var(--accent);background:var(--accent-soft);
animation:ringpulse 1.6s ease-out infinite}
.stage.active i::after{content:"";width:8px;height:8px;border-radius:50%;background:var(--accent)}
.stage.active:not(:last-child)::after{background:linear-gradient(90deg,var(--accent),
var(--track) 70%)}
.stage.failed{color:var(--bad)}.stage.failed i{border-color:var(--bad)}
.stage.failed i::after{content:"!";font:800 11px/1 var(--display);color:var(--bad)}
.scan-top{position:relative;display:grid;grid-template-columns:auto minmax(0,1fr) auto;
align-items:end;gap:var(--s3) var(--s7);margin-bottom:var(--s5)}
.pct{font:800 76px/.86 var(--display);letter-spacing:-.05em;font-variant-numeric:tabular-nums;
background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;
min-width:3.2ch}
.pct small{font-size:30px;letter-spacing:0;margin-left:2px}
.phase-line{min-width:0;padding-bottom:4px}
.phase-name{font:720 22px/1.15 var(--display);letter-spacing:-.02em}
.phase-msg{color:var(--muted);font-size:13.5px;margin-top:6px;min-height:20px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tiles{display:flex;gap:var(--s7);color:var(--dim);font-size:12px;padding-bottom:4px}
.tiles span{white-space:nowrap}
.tiles b{display:block;color:var(--fg);font:700 19px/1.1 var(--display);margin-bottom:4px;
font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.pbar{position:relative;height:12px;border-radius:999px;background:var(--track);overflow:hidden;
box-shadow:inset 0 1px 2px #00000040}
.pbar>span{position:absolute;inset:0 auto 0 0;width:0;border-radius:999px;
background:linear-gradient(90deg,#ff4d94,#8b5cf6,#22d3ee,#8b5cf6,#ff4d94);
background-size:200% 100%;box-shadow:0 0 18px -2px #8b5cf6b3;
transition:width .9s var(--ease)}
.scan.running .pbar>span{animation:flow 2.4s linear infinite}
.scan.running .pbar>span::after{content:"";position:absolute;inset:0;border-radius:inherit;
background:linear-gradient(90deg,transparent,#ffffff59,transparent);background-size:40% 100%;
background-repeat:no-repeat;animation:sheen 2s ease-in-out infinite}
.scan.held .pbar>span{animation:none;filter:saturate(.6)}
.scan.held .pbar>span::after{display:none}
@keyframes flow{from{background-position:0 0}to{background-position:-200% 0}}
@keyframes sheen{from{background-position:-40% 0}to{background-position:140% 0}}
.cells-wrap{margin-top:var(--s5)}
.cells-k{display:flex;justify-content:space-between;align-items:center;
font:700 10.5px/1 var(--display);letter-spacing:.14em;text-transform:uppercase;color:var(--dim);
margin-bottom:var(--s2)}
.cells-k span+span{font:600 11.5px/1 var(--mono);letter-spacing:0;text-transform:none}
.cells{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;gap:2px;height:34px;padding:3px;
background:var(--sunken);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.cells i{display:block;border-radius:2px;background:var(--track);
transition:background-color .35s,box-shadow .35s}
.cells i.on{background:var(--c);box-shadow:0 0 8px -2px var(--c)}
.cells i.pop{animation:pop .5s ease-out}
.cells i.next{animation:next 1s ease-in-out infinite}
@keyframes pop{0%{background:#fff;box-shadow:0 0 14px #fff}}
@keyframes next{50%{background:var(--line3)}}
.cells.idle{background-image:linear-gradient(90deg,transparent 0%,var(--accent-soft) 50%,
transparent 100%);background-size:40% 100%;background-repeat:no-repeat;
animation:shimmer 1.6s linear infinite}
@keyframes shimmer{from{background-position:-40% 0}to{background-position:140% 0}}
.slow-note{display:flex;gap:6px;flex-wrap:wrap;align-items:baseline;margin:var(--s4) 0 0;
padding:10px 14px;border-radius:var(--r2);font-size:13px;color:var(--muted);
border:1px solid color-mix(in srgb,var(--warn) 35%,transparent);
background:color-mix(in srgb,var(--warn) 7%,transparent);animation:fade var(--t3) both}
.slow-note b{color:var(--warn);font-weight:650}
.slow-note[hidden]{display:none}
.flavour{margin:var(--s4) 0 0;color:var(--muted);font-size:13.5px;min-height:21px;
transition:opacity .35s}
.flavour.fade{opacity:0}
.scan .reassurance{margin:var(--s2) 0 0;color:var(--dim);font-size:12.5px}
@keyframes ringpulse{0%{box-shadow:0 0 0 0 var(--accent-line)}
100%{box-shadow:0 0 0 9px transparent}}
.job-player{background:var(--card);border:1px solid var(--line);border-radius:var(--r3);
padding:var(--s4) var(--s5);margin:var(--s4) 0 0;box-shadow:var(--sh1)}
.job-player .jp-label{display:flex;align-items:center;gap:10px;font-size:13px;color:var(--muted);
margin:0 2px var(--s3)}
.job-player audio{display:block;width:100%;height:44px;border-radius:var(--r2);outline:none}
.job-player .jp-yt{position:relative;width:100%;max-width:560px;aspect-ratio:16/9;
border-radius:var(--r2);overflow:hidden}
.job-player .jp-yt iframe{position:absolute;inset:0;width:100%;height:100%;border-radius:0}
.jp-frame{position:relative}.jp-frame[hidden]{display:none}
.jp-frame.loading audio{opacity:0}
.jp-wait{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
gap:10px;color:var(--muted);font-size:13px;background:var(--sunken);border-radius:var(--r2);
border:1px solid var(--line);animation:blink 1.6s ease-in-out infinite}
.jp-frame:not(.loading) .jp-wait{display:none}
.jp-fail{color:var(--muted);font-size:13px;padding:14px 16px;border:1px dashed var(--line2);
border-radius:var(--r2)}.jp-fail[hidden]{display:none}.jp-fail b{color:var(--fg)}
.outcome{display:none;border-radius:var(--r4);padding:var(--s7);border:1px solid var(--line);
background:var(--card);position:relative;overflow:hidden;box-shadow:var(--sh2)}
.outcome.show{display:block;animation:rise .45s var(--ease) both}
.outcome h2{font:800 34px/1.08 var(--display);letter-spacing:-.035em;margin:0 0 var(--s2)}
.outcome p{color:var(--muted);margin:0 0 var(--s5);font-size:15px;max-width:70ch}
.outcome.ok{border-color:color-mix(in srgb,var(--ok) 35%,transparent);
background:radial-gradient(70% 120% at 0% 0%,color-mix(in srgb,var(--ok) 14%,transparent),
transparent 60%),var(--card)}
.outcome.bad{border-color:color-mix(in srgb,var(--bad) 40%,transparent);
background:radial-gradient(70% 120% at 0% 0%,color-mix(in srgb,var(--bad) 13%,transparent),
transparent 60%),var(--card)}
.outcome .err{font-family:var(--mono);font-size:12px;color:var(--bad);background:var(--sunken);
border-radius:8px;padding:10px 12px;margin:0 0 var(--s4);white-space:pre-wrap;
overflow-wrap:anywhere}
.outcome .row{display:flex;gap:var(--s3);align-items:center;flex-wrap:wrap}
.countdown{color:var(--dim);font-size:12px}
.confetti{position:absolute;inset:0;pointer-events:none;overflow:hidden}
.confetti i{position:absolute;top:-10px;left:var(--x);width:7px;height:11px;background:var(--c);
border-radius:2px;opacity:0;animation:fall var(--d) ease-in var(--delay) forwards}
@keyframes fall{0%{opacity:1;transform:translateY(0) rotate(0)}
100%{opacity:0;transform:translateY(340px) rotate(var(--r))}}
details.log{margin-top:var(--s5);border:1px solid var(--line);border-radius:var(--r3);
background:var(--card)}
details.log>summary{cursor:pointer;color:var(--muted);font-size:13px;font-weight:600;
list-style:none;display:flex;align-items:center;gap:8px;padding:14px 18px;
border-radius:var(--r3);transition:color var(--t1)}
details.log>summary:hover{color:var(--fg)}
details.log>summary::-webkit-details-marker{display:none}
details.log>summary::before{content:"";width:6px;height:6px;border-right:1.6px solid currentColor;
border-bottom:1.6px solid currentColor;transform:rotate(-45deg);transition:transform var(--t2)}
details.log[open]>summary::before{transform:rotate(45deg)}
details.log>.steps,details.log>pre{margin:0 18px 18px}
.steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:var(--s2)}
.step{background:var(--sunken);border:1px solid var(--line);border-radius:var(--r2);
padding:12px 12px 10px;transition:border-color var(--t2),opacity var(--t2);opacity:.6}
.step .k{display:flex;align-items:center;gap:8px;font:700 12.5px/1 var(--display)}
.step .k i{display:inline-grid;place-items:center;width:18px;height:18px;border-radius:50%;
border:1.5px solid var(--dim);font:700 10px/1 var(--mono);color:var(--dim);font-style:normal}
.step small{display:block;color:var(--dim);font-size:11.5px;margin-top:6px}
.step.done{opacity:1}.step.done .k i{background:var(--ok);border-color:var(--ok);
color:var(--on-accent)}
.step.active{opacity:1;border-color:var(--accent-line);background:var(--accent-soft)}
.step.active .k i{border-color:var(--accent);color:var(--accent)}
.step.failed{opacity:1;border-color:var(--bad);background:color-mix(in srgb,var(--bad) 8%,
transparent)}
.step.failed .k i{border-color:var(--bad);color:var(--bad)}
pre{white-space:pre-wrap;word-break:break-word;background:var(--sunken);
border:1px solid var(--line);border-radius:10px;padding:12px;font:12px/1.6 var(--mono);
color:var(--muted);max-height:260px;overflow:auto;margin:8px 0 0}
pre:empty{display:none}
@media (max-width:900px){.scan-top{grid-template-columns:auto minmax(0,1fr)}
.tiles{grid-column:1/-1;gap:var(--s6)}.hero-wave{opacity:.3}}
@media (max-width:720px){.job-head .titles{flex-basis:100%}
.hero-home{padding:var(--s7) 0 var(--s5)}
.hero-home h1{font-size:40px}.lede{font-size:15px;margin-bottom:var(--s5)}
.hero-wave{right:-40px;width:80vw;height:160px;top:0;opacity:.22}
.urlbox{flex-wrap:wrap;padding:8px;border-radius:16px}
.urlbox input{flex-basis:100%;order:-1;padding:0 8px;height:44px}.urlbox .btn{margin-left:auto}
.opt-grid,.seg{grid-template-columns:1fr}
.lib-bar{flex-direction:column}.lib-tools{flex:1 1 auto}
.stats{flex-wrap:wrap}.stat{padding:12px 14px}.stat .big{font-size:22px}
.mixes{grid-template-columns:1fr}
.mix-actions{position:static;opacity:1;padding:0 10px 10px;display:flex;
justify-content:flex-end;margin-top:-6px}
.mix-actions .link-danger{background:transparent;border:0;min-height:44px}
.job-head{padding-top:var(--s5)}.scan{padding:var(--s5) var(--s4) var(--s4)}
.stages{margin-bottom:var(--s5)}.stage{font-size:11.5px;gap:6px;flex-direction:column;
align-items:flex-start}
.stage:not(:last-child)::after{position:absolute;left:28px;right:4px;top:10px;margin:0}
.pct{font-size:56px}.pct small{font-size:22px}.phase-name{font-size:18px}
.tiles{gap:var(--s5)}.tiles b{font-size:16px}.outcome{padding:var(--s5)}
.outcome h2{font-size:26px}
.btn,.link-danger,.opts>summary,.segopt span,.tog,.lib-filter button{min-height:44px}
.lib-filter{height:auto}
.tiles{grid-template-columns:1fr 1fr}}
"""
    + GENRE_CSS
    + LIGHT_CSS
)


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
    var mode = f.querySelector('input[name=profile]:checked');
    parts.push(mode && mode.value === 'max_accuracy' ? 'Deep scan' : 'Free scan');
    parts.push(f.querySelector('input[name=acquire]').checked ? 'links on' : 'links off');
    var known = f.querySelector('textarea[name=known_tracklist]');
    if(known && known.value.trim()) parts.push('tracklist added');
    sum.textContent = parts.join(' · ');
  }
  Array.prototype.forEach.call(input.form.querySelectorAll(
  'input[type=radio],input[type=checkbox]'),
    function(el){ el.addEventListener('change', summary); });
  var known = input.form.querySelector('textarea[name=known_tracklist]');
  if(known) known.addEventListener('input', summary);
  input.form.addEventListener('submit', function(event){
    var value = input.value.trim(), error = document.getElementById('form-error');
    if(value && !/^[a-z][a-z0-9+.-]*:\\/\\//i.test(value) &&
      /(^|\\.)((soundcloud|mixcloud)\\.com|youtu\\.be|youtube\\.com)(\\/|$)/i.test(value)){
      value = 'https://' + value; input.value = value;
    }
    if(!value){ event.preventDefault();
      error.textContent = 'Paste a mix link or choose a local file.';
      input.focus(); return; }
    if(/^(javascript|data|ftp):/i.test(value)){ event.preventDefault();
      error.textContent = 'Use a SoundCloud, Mixcloud or YouTube web link.'; input.focus(); }
  });
  summary();
})();
"""


#: Wall-clock progress (U-F9).  The arithmetic lives server-side in ``Job.progress_percent`` — over
#: *measured* phase durations and the **recent**-window-rate estimate — so a tenth of the bar really
#: is a tenth of the expected wall time, recognise dominates it, a cached or skipped phase cannot
#: claim time it never used, and the progress page and the home cards cannot drift into two bars.
#: All the browser does is hold the monotonic floor a dropped/reordered response could otherwise
#: breach, and put the server's time-left estimate into plain words: a guess is worded as
#: "Estimating…", never as a number, and a real estimate is rounded to the minute so it does not
#: flicker with every poll.
_PROGRESS_JS = """
function wallProgress(j, previous){
  if(j.status === 'succeeded') return 100;
  var value = (typeof j.progress_pct === 'number') ? j.progress_pct : 0;
  return Math.max(previous || 0, Math.max(0, Math.min(100, Math.round(value))));
}
function etaWords(j){
  if(j.terminal) return '—';
  if(j.status === 'queued') return '…';
  if(j.eta_estimating || typeof j.eta_seconds !== 'number') return 'Estimating…';
  if(j.eta_seconds < 50) return 'under a minute';
  var m = Math.round(j.eta_seconds / 60);
  if(m < 60) return 'about ' + m + ' min';
  var h = Math.floor(m / 60), r = m % 60;
  return 'about ' + h + ' hr' + (r ? ' ' + r + ' min' : '');
}
"""


_HOME_JS = """
(function(){
  var cards = document.querySelectorAll('.act[data-job]'); if(!cards.length) return;
  function fmtPhase(j){
    if(j.status === 'queued') return 'waiting in the queue';
    var labels = {starting:'Starting', ingest:'Fetching the mix', decode:'Preparing audio',
      windows:'Preparing the scan', recognise:'Listening', hints:'Checking tracklist clues',
      fuse:'Lining up the tracks', enrich:'Finding links', present:'Building the page',
      failed:'Failed', cancelled:'Cancelled', waiting:'Waiting'};
    return labels[j.phase] || 'Working';
  }
  function poll(card){
    var id = card.getAttribute('data-job');
    fetch('/jobs/' + id + '/status').then(function(r){ return r.json(); }).then(function(j){
      var st = card.querySelector('.st'); st.textContent = j.status;
  st.className = 'st st-' + j.status;
      card.setAttribute('data-status', j.status);
      card.querySelector('.act-phase').textContent = fmtPhase(j);
      if(j.title) card.querySelector('.act-title').textContent = j.title;
      var pct = wallProgress(j, parseInt(card.getAttribute('data-pct'), 10) || 0);
      card.setAttribute('data-pct', pct);
      card.querySelector('.bar>span').style.width = pct + '%';
      if(j.status === 'succeeded' && j.result_url){ window.location.reload(); return; }
      if(!j.terminal) setTimeout(function(){ poll(card); }, 2500);
    }).catch(function(){ setTimeout(function(){ poll(card); }, 5000); });
  }
  Array.prototype.forEach.call(cards, function(card){
    if(card.getAttribute('data-terminal') !== '1') poll(card); });
})();
"""


#: The library's search box and Free/Deep filter: they only show and hide the cards already on the
#: page (by their ``data-title`` / ``data-scan``), so nothing is fetched and nothing is stored.
_LIBRARY_JS = """
(function(){
  function shut(menu){
    menu.open = false;
    var pop = menu.querySelector('.mm-pop'); if(pop) pop.classList.remove('asking');
  }
  document.addEventListener('click', function(event){
    var t = event.target; if(!t || !t.closest) return;
    var ask = t.closest('.mm-ask');
    if(ask){
      var pop = ask.closest('.mm-pop'); pop.classList.add('asking');
      var no = pop.querySelector('.mm-no'); if(no) no.focus();
      return;
    }
    var cancel = t.closest('.mm-no');
    if(cancel){
      var menu = cancel.closest('details'); shut(menu);
      menu.querySelector('summary').focus(); return;
    }
    Array.prototype.forEach.call(document.querySelectorAll('details.mix-menu[open]'),
      function(menu){ if(!menu.contains(t)) shut(menu); });
  });
  document.addEventListener('keydown', function(event){
    if(event.key !== 'Escape') return;
    Array.prototype.forEach.call(document.querySelectorAll('details.mix-menu[open]'), shut);
  });
})();
(function(){
  var search = document.getElementById('lib-search');
  var cards = document.querySelectorAll('.mixes>.mix'); if(!cards.length) return;
  var filter = 'all', none = document.getElementById('lib-none');
  function apply(){
    var words = (search ? search.value : '').trim().toLowerCase().split(/\\s+/)
      .filter(function(w){ return w; });
    var shown = 0;
    Array.prototype.forEach.call(cards, function(card){
      var title = card.getAttribute('data-title') || '', scan = card.getAttribute('data-scan');
      var ok = (filter === 'all' || scan === filter) &&
        words.every(function(w){ return title.indexOf(w) >= 0; });
      card.hidden = !ok; if(ok) shown += 1;
    });
    if(none) none.hidden = shown > 0;
  }
  if(search) search.addEventListener('input', apply);
  Array.prototype.forEach.call(document.querySelectorAll('.lib-filter button'), function(button){
    button.addEventListener('click', function(){
      filter = button.getAttribute('data-filter') || 'all';
      Array.prototype.forEach.call(document.querySelectorAll('.lib-filter button'), function(b){
        var on = b === button; b.classList.toggle('on', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      apply();
    });
  });
})();
"""


_JOB_JS = """
var STEP_INDEX = {}; STEPS.forEach(function(s, i){ STEP_INDEX[s[0]] = i; });
var FLAVOUR = {
  queued: ['Waiting in the queue — one analysis runs at a time.'],
  starting: ['Warming up…'],
  build_index: ['Fingerprinting the uploader\\'s own tracks so unreleased ones match too.'],
  ingest: ['Fetching the mix from the platform — a long set can take a minute.',
    'Only the audio comes down; nothing about you goes up.'],
  decode: ['Preparing the audio for a consistent scan.'],
  windows: ['Dividing the set into short listening sections.'],
  recognise: ['Checking what is playing throughout the set.',
    'Working through the set clip by clip — a long mix takes a few minutes.',
    'A section that matches nothing stays honest: it becomes an ID, never a guess.',
    'Overlapping checks help pin each track start only as far as the evidence proves.',
    'Repeated matches are combined into one track later.',
    'Nod along. The machine is listening so you don\\'t have to rewind.'],
  hints: ['Reading the comments for tracklist clues — as hints, never as proof.'],
  fuse: ['Lining up matched tracks with honest confidence levels.',
    'Where a boundary is not proved, the page will say so rather than guess.'],
  enrich: ['Looking up where each track can be bought or downloaded.'],
  present: ['Writing your click-to-jump tracklist page.'],
  done: ['Done.'], failed: [''], cancelled: ['']
};
// OUTCOME_COPY is the server's own _OUTCOME_COPY table, so the live page and the durable failed
// card cannot explain one run two different ways.
function outcomeCopy(status, reason){
  return OUTCOME_COPY[status + ':' + reason] || OUTCOME_COPY[status] || OUTCOME_COPY.failed;
}
function costSentence(j){
  // A settled, non-zero spend is always stated, whatever the status (a refusal can arrive while
  // sibling requests already in flight are charged).
  if(j.spend_known && typeof j.usd_e2_spent === 'number' && j.usd_e2_spent > 0)
    return '$' + (j.usd_e2_spent / 100).toFixed(2) + ' of paid checks was spent before it stopped.';
  if(j.run_status === 'budget_exhausted' || j.run_status === 'provider_unavailable' ||
    j.status === 'waiting')
    return 'Nothing was spent — it stopped before any paid check ran.';
  if(j.spend_known && typeof j.usd_e2_spent === 'number')
    return j.usd_e2_spent > 0
      ? '$' + (j.usd_e2_spent / 100).toFixed(2) + ' of paid checks was spent before it stopped.'
      : 'Nothing was spent on this run.';
  if(j.profile !== 'max_accuracy') return 'This was a Free scan, so nothing was spent.';
  return "This run's cost record could not be read.";
}
var cellCount = 0, lastDone = -1, flavourIx = 0, flavourPhase = '';
// One cell per clip, but never more than fit the map at a few pixels each (a phone shows fewer).
function cellTarget(total){
  var box = document.getElementById('cells'), width = box ? box.clientWidth : 0;
  return Math.min(total, 240, Math.max(24, Math.floor((width || 960) / 4)));
}
var LAST = null, celebrated = false, PROGRESS_MAX = 0;
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
  cellCount = cellTarget(total); lastDone = -1;
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
// The four plain stages: every pipeline phase belongs to one of them.
var STAGE_OF = {queued: 0, starting: 0, build_index: 0, ingest: 0, decode: 0, windows: 0,
  recognise: 1, hints: 1, fuse: 2, enrich: 2, present: 2, done: 3};
function setStages(j){
  var phase = j.status === 'failed' ? (j.failed_phase || j.phase) : j.phase;
  var ix = j.status === 'succeeded' ? 4 : (STAGE_OF[phase] !== undefined ? STAGE_OF[phase] : 0);
  Array.prototype.forEach.call(document.querySelectorAll('.stage'), function(el, i){
    el.classList.remove('done', 'active', 'failed');
    el.removeAttribute('aria-current');
    if(i < ix) el.classList.add('done');
    else if(i === ix && j.status === 'failed') el.classList.add('failed');
    else if(i === ix && j.status === 'running'){
      el.classList.add('active'); el.setAttribute('aria-current', 'step'); }
  });
}
function setSteps(j){
  setStages(j);
  var phase = j.status === 'failed' ? j.failed_phase : j.phase;
  var ix = STEP_INDEX[phase]; var terminal = j.terminal;
  Array.prototype.forEach.call(document.querySelectorAll('.step'), function(el, i){
    el.classList.remove('done', 'active', 'failed');
    if(j.status === 'succeeded' || (
  terminal === false && ix !== undefined && i < ix)) el.classList.add('done');
    else if(!terminal && ix === i) el.classList.add('active');
    else if(terminal && ix !== undefined && i < ix) el.classList.add('done');
    else if(j.status === 'failed' && ix === i) el.classList.add('failed');
  });
}
function safeLog(j){
  var labels = {build_index:'Reference check', ingest:'Fetch', decode:'Prepare audio',
    windows:'Prepare scan', recognise:'Listen', hints:'Tracklist clues', fuse:'Line up tracks',
    enrich:'Find links', present:'Build page', failed:'Scan stopped', cancelled:'Scan stopped'};
  return (j.log || []).map(function(line){
    var stamp = line.slice(0, 8), match = / (\\w+):/.exec(line), key = match && match[1];
    return stamp + ' ' + (labels[key] || 'Working');
  }).join('\\n');
}
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
  var again = '<a class="btn primary" href="' + RETRY_HREF + '">Try again</a>' +
    '<a class="btn" href="/">Your mixes</a>';
  if(j.status === 'succeeded'){
    box.classList.add('ok');
    h.textContent = 'Tracklist ready';
    // The count comes from the run's own canonical projection, so it is the number of rows the
    // tracklist actually lists — never the raw fused-episode total (U-F2/F3).
    if(typeof j.tracks_found === 'number'){
      var word = j.tracks_found === 1 ? ' track' : ' tracks';
      p.textContent = j.tracks_found + word + ' in your tracklist' +
        (j.crowd_tracks ? ' (' + j.crowd_tracks + ' from the comments).' : '.');
    } else { p.textContent = 'Analysis finished.'; }
    var open = '<a class="btn primary big" id="open" href="' + j.result_url +
      '">Open the tracklist →</a>';
    row.innerHTML = j.result_url ? open : '';
    if(j.paid_offer){ showOffer(j, h, p, row); return; }
    // Deep ended on its Free result: paid recognition is not set up, or AudD refused the token.
    // Say so plainly; there is no Approve button, because there is nothing it could buy.
    if(j.paid_notice){
      h.textContent = j.paid_notice.reason === 'not_configured'
        ? 'Free result ready — paid recognition is not set up'
        : j.paid_notice.reason === 'not_configured_cached'
        ? 'Tracklist ready — paid recognition is not set up'
        : 'Free result ready — the paid check was refused';
      p.textContent = p.textContent + ' ' + j.paid_notice.words;
      return;
    }
    confetti();
  } else if(j.status === 'waiting'){
    var w = outcomeCopy('waiting', null);
    h.textContent = 'Waiting for the free service';
    p.textContent = w[0] + ' ' + costSentence(j) + ' ' + w[1];
    row.innerHTML = again;
  } else {
    var failed = j.status === 'failed';
    if(failed) box.classList.add('bad');
    h.textContent = failed ? 'That one didn\\'t work' : 'Cancelled';
    // Cause, remedy and cost all come from the run's frozen journal outcome, not from the profile
    // that was requested: a run that spent nothing says so, and one that spent money names it.
    var copy = outcomeCopy(j.run_status || j.status, j.run_reason || j.failed_phase);
    var reached = j.last_stage ? ' It got as far as ' + j.last_stage + '.' : '';
    p.textContent = copy[0] + reached + ' ' + costSentence(j) + ' ' + copy[1];
    row.innerHTML = again;
  }
}
// A Max-accuracy job stops after its free pass until the owner answers the exact Deep price.
// Approve starts the paid job (it reserves and dispatches only up to this price); Skip keeps the
// Free result and spends nothing; closing the tab spends nothing either.
function offerForm(action, label, cls){
  return '<form method="post" action="/jobs/' + JOB_ID + '/' + action +
    '" style="display:inline">' +
    '<input type="hidden" name="csrf_token" value="' + CSRF_TOKEN + '">' +
    '<button class="btn ' + cls + '" type="submit">' + label + '</button></form>';
}
function showOffer(j, h, p, row){
  var o = j.paid_offer, dollars = '$' + (o.usd_e2 / 100).toFixed(2);
  var free = o.cached ? ' ' + o.cached + ' of its ' + o.clips +
    ' clips already have a stored answer and cost nothing.' : '';
  var open = j.result_url ? '<a class="btn" href="' + j.result_url +
    '">Open the Free result</a>' : '';
  if(j.offer_decision === 'approved'){
    h.textContent = 'Deep approved';
    p.textContent = 'The paid check was approved and runs as its own job.';
    row.innerHTML = (j.follow_up_url ? '<a class="btn primary" href="' + j.follow_up_url +
      '">Follow the Deep job →</a>' : '') + open;
  } else if(j.offer_decision === 'skipped'){
    h.textContent = 'Free result kept';
    p.textContent = 'You skipped the paid check. Nothing was reserved or spent.';
    row.innerHTML = open;
  } else {
    h.textContent = 'Free pass done — Deep is waiting for your approval';
    p.textContent = 'Deep would check ' + o.gaps + ' gaps for about ' + dollars + '.' + free +
      ' Nothing has been reserved or spent. Approve to run the paid check, or Skip to keep the ' +
      'Free result. Closing this page spends nothing.';
    row.innerHTML = offerForm('approve', 'Approve ' + dollars, 'primary big') +
      offerForm('skip', 'Skip', '') + open;
  }
}
function render(j){
  LAST = j;
  document.body.classList.toggle('analysing', !j.terminal);
  var st = document.getElementById('status'); st.textContent = j.status;
  st.className = 'st st-' + j.status;
  var t = j.title || null; if(t) document.getElementById('title').textContent = t;
  PROGRESS_MAX = wallProgress(j, PROGRESS_MAX);
  var pct = PROGRESS_MAX;
  document.getElementById('pct').innerHTML = pct + '<small>%</small>';
  var fill = document.getElementById('pbar-fill'); if(fill) fill.style.width = pct + '%';
  // A phase past its expected time holds the bar and says so, calmly, instead of creeping on.
  var slow = !!j.overrun && !j.terminal;
  var scan = document.getElementById('scan');
  scan.classList.toggle('running', j.status === 'running' && !slow);
  scan.classList.toggle('held', slow);
  var note = document.getElementById('slow-note'); if(note) note.hidden = !slow;
  var stepIx = STEP_INDEX[j.phase];
  var label = stepIx !== undefined ? STEPS[stepIx][1]
    : (j.phase.charAt(0).toUpperCase() + j.phase.slice(1));
  if(j.status === 'queued') label = 'In the queue';
  document.getElementById('phase-name').textContent = label;
  var msg = j.message || (stepIx !== undefined ? STEPS[stepIx][2] : '');
  if(j.status === 'queued') msg = 'another analysis is running first';
  document.getElementById('phase-msg').textContent = msg;
  var total = j.windows_total || 0, done = j.windows_done || 0;
  if(total && total !== cellCount && (cellCount === 0 || cellTarget(total) !== cellCount))
    buildCells(total);
  if(total) lightCells(done, total); else document.getElementById('cells').classList.add('idle');
  var heard = document.getElementById('t-win');
  if(heard) heard.textContent = total ? done + ' / ' + total + ' clips heard' : '';
  document.getElementById('t-eta').textContent = etaWords(j);
  // Counted in the four stages shown above, not the eight pipeline phases underneath.
  var stageIx = j.status === 'succeeded' ? 3 : STAGE_OF[j.phase];
  document.getElementById('t-step').textContent = stageIx !== undefined ?
    (stageIx + 1) + ' of ' + document.querySelectorAll('.stage').length : '—';
  var started = j.started_at, finished = j.finished_at;
  var elapsed = started ? ((finished || Date.now() / 1000) - started) : 0;
  document.getElementById('t-elapsed').textContent = started ? fmt(elapsed) : '—';
  setSteps(j);
  document.title = (j.terminal ? (
  j.status === 'succeeded' ? 'Done' : j.status) : pct + '%') + ' · ' + (
  t || 'Analysing') + " — ID'er";
  document.getElementById('log').textContent = safeLog(j);
  document.getElementById('cancel').style.display = j.terminal ? 'none' : '';
  var eyebrow = {succeeded: 'Analysed', failed: 'Analysis failed', cancelled: 'Analysis cancelled',
  waiting: 'Not started'};
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
  fetch('/jobs/' + JOB_ID + '/cancel', {method: 'POST',
    headers: {'X-CSRF-Token': CSRF_TOKEN}}).then(tick);
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
        "<footer><span>🔒 Audio stays local; only short recognition clips are sent to the "
        "selected services.</span><span>ID&#39;er</span></footer>"
    )


@dataclass(frozen=True)
class _FormState:
    url: str = ""
    profile: str = "free"
    acquire: bool = True
    known_tracklist: str = ""
    error: str = ""


def _form_html(prefill: str = "", csrf_token: str = "", *, state: _FormState | None = None) -> str:
    """The drop-a-link form: a hero input with platform detection and a collapsible options tray."""

    current = state or _FormState(url=prefill)
    free_checked = " checked" if current.profile != "max_accuracy" else ""
    deep_checked = " checked" if current.profile == "max_accuracy" else ""
    acquire_checked = " checked" if current.acquire else ""
    return (
        '<form method="post" action="/analyse" class="dropform" autocomplete="off">'
        f'<input type="hidden" name="{_CSRF_FIELD}" value="{html.escape(csrf_token)}">'
        '<div class="urlbox">'
        '<span class="plat-ind" id="plat"><span class="pd"></span>'
        '<span id="plat-name">Paste a link</span></span>'
        '<label class="sr-only" for="url">Mix link or local audio file</label>'
        '<input id="url" name="url" type="text" required autofocus spellcheck="false" '
        f'value="{html.escape(current.url)}" '
        'placeholder="https://soundcloud.com/… — or a local audio file path">'
        '<button class="btn primary big" type="submit">Analyse<svg viewBox="0 0 16 16" '
        'aria-hidden="true"><path d="M3 8h9M8.5 4.5 12 8l-3.5 3.5" fill="none" '
        'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
        'stroke-linejoin="round"/></svg></button></div>'
        f'<p class="form-error" id="form-error" role="alert">{html.escape(current.error)}</p>'
        # Step 1 — the only choice most people make, always visible above the fold: Free or Paid.
        '<div class="seg modes" role="radiogroup" aria-label="Mode">'
        f'<label class="segopt"><input type="radio" name="profile" value="free"{free_checked}>'
        '<span><b><i class="mode-dot free"></i>Free scan</b><small>no key needed — identifies '
        "tracks and reads the crowd's comments</small></span></label>"
        f'<label class="segopt"><input type="radio" name="profile" '
        f'value="max_accuracy"{deep_checked}>'
        '<span><b><i class="mode-dot deep"></i>Deep scan</b><small>checks more of the set '
        "with your configured paid provider; provider charges can apply</small></span></label>"
        "</div>"
        # Step 2 — everything else, collapsed. Download links default on for both modes; the rest is
        # power-user territory.
        '<details class="opts"><summary><span>More options</span>'
        '<span class="opt-sum" id="opt-sum">Free scan · links on</span></summary>'
        '<div class="opt-grid">'
        f'<label class="tog"><input type="checkbox" name="acquire" value="1"{acquire_checked}>'
        '<span class="sw"></span>'
        "<span><b>Buy / download links</b>"
        "<small>on by default — where to get each track (free download, buy, or gated) on the "
        "result page</small></span></label>"
        '<label class="tlbox"><span class="tlbox-h"><b>Know part of the tracklist?</b>'
        "<small>Paste anything you can already see — from 1001tracklists, a YouTube "
        "description, a comment — to guide the analysis. One track per line, ideally "
        '"12:34 Artist - Title". Leave blank to analyse from the audio only.</small></span>'
        '<textarea name="known_tracklist" rows="4" spellcheck="false" '
        'placeholder="12:34 Artist - Title&#10;19:20 Another Artist - Another Title">'
        f"{html.escape(current.known_tracklist)}</textarea></label></div></details></form>"
    )


#: Plain cause + remedy for each frozen §2.3.5 terminal outcome (U-F15).  Keyed by the journalled
#: status, or ``status:reason`` when the reason changes the story.  The same table feeds the live
#: progress page (serialised into its script) and the durable failed-attempt card, so a user can
#: never read two different explanations of one run.
_OUTCOME_COPY: dict[str, tuple[str, str]] = {
    "failed": ("The scan stopped before it could finish.", "Try again in a few minutes."),
    "failed:ingest": (
        "The platform would not hand over the audio.",
        "Try again in a few minutes, or paste another link to the same set.",
    ),
    "failed:decode": (
        "The downloaded audio could not be read.",
        "Try again, or paste another link to the same set.",
    ),
    "provider_unavailable": (
        "A recognition service was unavailable, so the scan never started.",
        "Try again in a few minutes.",
    ),
    "budget_exhausted": (
        "The paid-scan limit reached its ceiling before any paid check ran.",
        "Raise the limit in your settings, or run a Free scan instead.",
    ),
    "source_changed": (
        "The mix changed on the platform while it was being fetched.",
        "Try it again.",
    ),
    "cancelled": ("You stopped this scan.", "Start it again whenever you like."),
    "waiting": (
        "The free recognition service is paused, so this scan did not start.",
        "Try again once it is back.",
    ),
}


def _outcome_copy(status: str | None, reason: str | None = None) -> tuple[str, str]:
    """The plain cause and remedy for one terminal outcome, most specific match first."""

    keys = [f"{status}:{reason}", str(status), "failed"]
    for key in keys:
        if key in _OUTCOME_COPY:
            return _OUTCOME_COPY[key]
    return _OUTCOME_COPY["failed"]


def _cost_sentence(
    *,
    usd_e2_spent: int | None,
    spend_known: bool,
    status: str | None,
    profile: str | None = None,
) -> str:
    """What this run really cost, stated exactly — never hedged (U-F15).

    ``budget_exhausted`` and ``provider_unavailable`` spend nothing by definition (plan §2.3.5), so
    they say so outright.  Otherwise the settled figure from the run's journal is quoted; a Free
    scan can never spend; and only an unreadable cost record is admitted as unknown.
    """

    if spend_known and usd_e2_spent is not None and usd_e2_spent > 0:
        # A settled, non-zero spend is always stated, whatever the status: a refusal can stop the
        # paid sweep while sibling requests already in flight are charged.
        return f"${usd_e2_spent / 100:.2f} of paid checks was spent before it stopped."
    if status in {"budget_exhausted", "provider_unavailable"}:
        return "Nothing was spent — it stopped before any paid check ran."
    if spend_known and usd_e2_spent is not None:
        if usd_e2_spent <= 0:
            return "Nothing was spent on this run."
        return f"${usd_e2_spent / 100:.2f} of paid checks was spent before it stopped."
    if profile is not None and profile != "max_accuracy":
        return "This was a Free scan, so nothing was spent."
    return "This run's cost record could not be read."


def _library_stats_html(sets: list[AnalysedSet]) -> str:
    """The library's totals (mixes, tracks identified, music listened to) and its search bar.

    The search and the Free/Deep filter run in the page (``_LIBRARY_JS``) over the cards' own
    ``data-`` attributes: no request, and nothing to show when there is only one mix.
    """

    if not sets:
        return ""
    summaries = [_set_summary(item) for item in sets]
    tracks = sum(s.tracks for s in summaries if s)
    mixes = len(sets)
    minutes = sum(s.duration_ms for s in summaries if s) // 60_000
    hours, rest = divmod(minutes, 60)
    listened = f"{hours} h {rest:02d} m" if hours else f"{rest} min"
    tools = ""
    if mixes > 1:
        tools = (
            '<div class="lib-tools" role="search">'
            '<label class="lib-search"><span class="sr-only">Search your mixes</span>'
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
            'stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/>'
            '<path d="m16 16 4 4"/></svg>'
            '<input type="search" id="lib-search" placeholder="Search mixes" autocomplete="off" '
            'spellcheck="false"></label>'
            '<div class="lib-filter" role="group" aria-label="Show">'
            '<button type="button" class="on" data-filter="all" aria-pressed="true">All</button>'
            '<button type="button" data-filter="free" aria-pressed="false">Free</button>'
            '<button type="button" data-filter="deep" aria-pressed="false">Deep</button></div>'
            "</div>"
        )
    return (
        '<div class="lib-bar"><div class="stats">'
        f'<div class="stat"><div><span class="big">{mixes}</span>'
        f"<small>mix{'es' if mixes != 1 else ''} analysed</small></div></div>"
        f'<div class="stat"><div><span class="big">{tracks}</span>'
        "<small>tracks identified</small></div></div>"
        f'<div class="stat"><div><span class="big">{html.escape(listened)}</span>'
        "<small>of music listened to</small></div></div>"
        f"</div>{tools}</div>"
        '<p class="lib-none" id="lib-none" hidden>No mix matches that search.</p>'
    )


def _set_strip_html(item: AnalysedSet) -> str:
    """The card's signature: the whole mix as a strip, each identified track a coloured block.

    Read from the same published ``tracklist.json`` as the card's figures, so the strip can only
    show the rows the result page lists.  Decorative (the card states its figures in words).
    """

    empty = '<div class="mix-strip empty" aria-hidden="true"></div>'
    try:
        document = json.loads(read_text(result_dir(item.media_dir) / "tracklist.json"))
        duration = int(document.get("duration_ms") or 0)
        entries = [entry for entry in document.get("entries", ()) if isinstance(entry, dict)]
    except (OSError, ValueError, TypeError, AttributeError):
        return empty
    if duration <= 0:
        return empty
    blocks: list[str] = []
    for entry in entries:
        try:
            start = max(0, int(entry.get("start_ms") or 0))
            end = max(start, int(entry.get("end_ms") or start))
        except (TypeError, ValueError):
            continue
        left = min(100.0, start * 100.0 / duration)
        width = max(0.35, min(100.0 - left, (end - start) * 100.0 / duration))
        if entry.get("kind") == "id":
            kind = "gap"
        elif entry.get("hint_only"):
            kind = "crowd"
        else:
            kind = str(entry.get("badge") or "unclear")
            if kind not in BADGE_ORDER:
                kind = "unclear"
        blocks.append(f'<i class="s-{kind}" style="left:{left:.2f}%;width:{width:.2f}%"></i>')
    return f'<div class="mix-strip" aria-hidden="true">{"".join(blocks)}</div>'


#: What the menu's confirm step says will happen, per kind of card.
_MENU_NOTE = {
    "mix": "It leaves your library; the files go to the trash folder.",
    "attempt": "Only this failed try goes; the earlier result stays.",
}


def _card_menu_html(
    title: str, action: str, fields: dict[str, str], csrf_token: str, *, what: str
) -> str:
    """The card's ⋯ menu: Delete, then an in-place Cancel / Delete before anything is posted."""

    hidden = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in {_CSRF_FIELD: csrf_token, **fields}.items()
    )
    return (
        '<details class="mix-menu">'
        f'<summary aria-label="Options for {html.escape(title, quote=True)}" title="Options">'
        '<span aria-hidden="true">⋯</span></summary><div class="mm-pop">'
        '<button type="button" class="mm-item mm-ask">Delete</button>'
        f'<form class="mm-confirm" method="post" action="{action}">{hidden}'
        f"<p><b>Delete this {what}?</b>{_MENU_NOTE[what]}</p>"
        '<div class="mm-row"><button type="button" class="mm-no">Cancel</button>'
        '<button type="submit" class="mm-yes">Delete</button></div></form></div></details>'
    )


def _mix_card_html(item: AnalysedSet, csrf_token: str = "") -> str:
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
            f"track{'s' if summary.tracks != 1 else ''}</span>{_conf_mini_html(summary)}"
            '<span class="go" aria-hidden="true">→</span></div>'
        )
    else:
        meta = '<div class="mix-meta"><span class="go" aria-hidden="true">→</span></div>'
    analysed = datetime.fromtimestamp(item.analysed_at).strftime("%d %b %Y").lstrip("0")
    deep = bool(summary and summary.achieved == "deep")
    scan = (
        '<span class="scan-badge deep">Deep</span>'
        if deep
        else '<span class="scan-badge">Free</span>'
    )
    keys = {"source_key": item.source_key, "media_key": item.media_key}
    remove = (
        _card_menu_html(item.title, "/library/remove", keys, csrf_token, what="mix")
        if csrf_token
        else ""
    )
    search = html.escape(item.title.casefold(), quote=True)
    return (
        f'<li class="mix" data-title="{search}" data-scan="{"deep" if deep else "free"}">'
        f'<a class="mix-link" href="{href}">{_set_strip_html(item)}<div class="mix-body">'
        f'<div class="mix-top">{platform_chip(item.platform)}{scan}{duration}</div>'
        f'<div class="mix-title">{html.escape(item.title)}</div>'
        f'<div class="conf-text">Analysed {html.escape(analysed)}</div>{meta}</div></a>'
        f"{remove}</li>"
    )


def _failed_card_html(item: FailedRun, csrf_token: str) -> str:
    """A durable failed attempt: when, why it stopped, how far it got and what it cost (U-F15/F33).

    A card for an attempt that sits behind an older result never posts to Remove: the two share
    one media directory, so deleting it would delete the result the user can still open.  Its
    Delete only hides the attempt (``/library/dismiss-attempt``).
    """

    analysed = datetime.fromtimestamp(item.analysed_at).strftime("%d %b %Y").lstrip("0")
    cause, remedy = _outcome_copy(item.status, item.reason)
    reached = f" It got as far as {item.last_stage}." if item.last_stage else ""
    cost = _cost_sentence(
        usd_e2_spent=item.usd_e2_spent,
        spend_known=item.usd_e2_spent is not None,
        status=item.status,
    )
    note = (
        " The result below is from an earlier scan of the same mix."
        if item.has_result
        else " No result was saved."
    )
    # With a result behind it, Delete only hides this attempt (its own route); without one the
    # whole media directory goes to the trash as before.
    keys = {"source_key": item.source_key, "media_key": item.media_key}
    remove = (
        _card_menu_html(item.title, "/library/dismiss-attempt", keys, csrf_token, what="attempt")
        if item.has_result
        else _card_menu_html(item.title, "/library/remove", keys, csrf_token, what="mix")
    )
    label = "cancelled" if item.status == "cancelled" else "failed"
    search = html.escape(item.title.casefold(), quote=True)
    return (
        f'<li class="mix failed" data-title="{search}" data-scan="failed">'
        f'<div class="mix-body"><div class="mix-top"><span class="st st-{label}">{label}</span>'
        f'<span class="mono">Tried {html.escape(analysed)}</span></div>'
        f'<div class="mix-title">{html.escape(item.title)}</div>'
        f'<div class="conf-text cause">{html.escape(cause + reached)}</div>'
        f'<div class="conf-text">{html.escape(cost)} {html.escape(remedy)}'
        f"{html.escape(note)}</div></div>{remove}</li>"
    )


def _mixes_block(
    sets: list[AnalysedSet],
    *,
    allow_new: bool = True,
    csrf_token: str = "",
    failed_runs: list[FailedRun] | None = None,
) -> str:
    """The library grid of analysed mixes, or a friendly empty state."""

    failed_runs = failed_runs or []
    if sets or failed_runs:
        cards = "".join(_failed_card_html(item, csrf_token) for item in failed_runs)
        cards += "".join(_mix_card_html(item, csrf_token) for item in sets)
        return f'<ul class="mixes">{cards}</ul>'
    cta = (
        "<p>Paste a link above and find out what is in it.</p>"
        if allow_new
        else "<p>Analyse a set from the command line and it will appear here.</p>"
    )
    return (
        '<div class="empty"><span class="empty-eq" aria-hidden="true"><i></i><i></i><i></i><i></i>'
        f"<i></i></span><b>No mixes yet</b>{cta}</div>"
    )


def _activity_item_html(job: Job, csrf_token: str = "") -> str:
    status = html.escape(job.status)
    label = html.escape(_job_title(job))
    phase_names = {
        "queued": "Waiting in the queue",
        "starting": "Starting",
        "ingest": "Fetching the mix",
        "decode": "Preparing audio",
        "windows": "Preparing the scan",
        "recognise": "Listening",
        "hints": "Checking tracklist clues",
        "fuse": "Lining up the tracks",
        "enrich": "Finding links",
        "present": "Building the page",
        "failed": "Failed",
        "cancelled": "Cancelled",
        "waiting": "Waiting",
    }
    phase = phase_names.get(job.phase, "Working")
    pct = 100 if job.status == "succeeded" else 0
    terminal = "1" if job.status in TERMINAL_STATES else "0"
    dismiss = ""
    if terminal == "1":
        dismiss = (
            '<form class="act-actions" method="post" action="/jobs/'
            f'{html.escape(job.id)}/dismiss"><input type="hidden" name="{_CSRF_FIELD}" '
            f'value="{html.escape(csrf_token)}"><button class="link-danger" '
            'type="submit">Dismiss</button></form>'
        )
    return (
        f'<li class="act" data-job="{html.escape(job.id)}" data-terminal="{terminal}" '
        f'data-status="{status}" data-pct="{pct}">'
        f'<a class="act-link" href="/jobs/{html.escape(job.id)}">'
        f'<div class="act-row"><span class="st st-{status}">{status}</span>'
        f'<span class="act-title">{label}</span></div>'
        f'<div class="act-phase">{html.escape(phase)}</div>'
        f'<div class="bar"><span style="width:{pct}%"></span></div></a>{dismiss}</li>'
    )


def _home_html(
    sets: list[AnalysedSet],
    jobs: list[Job],
    csrf_token: str = "",
    *,
    form_state: _FormState | None = None,
    failed_runs: list[FailedRun] | None = None,
) -> bytes:
    """The home: a drop-a-link hero, anything in flight, and the library of analysed mixes."""

    active = [job for job in jobs if job.status != "succeeded"]
    rank = {"running": 0, "queued": 1, "waiting": 2, "failed": 3, "cancelled": 4}
    active.sort(key=lambda job: (rank.get(job.status, 5), -job.created_at))
    activity = ""
    if active:
        items = "".join(_activity_item_html(job, csrf_token) for job in active)
        activity = f'<h2 class="sec">Recent analyses</h2><ul class="acts">{items}</ul>'
    body = (
        topbar_html(back=False, new=True)
        + '<main class="home"><header class="hero-home">'
        + HERO_WAVE
        + '<p class="eyebrow">DJ-set track identifier · runs on this machine</p>'
        '<h1>Drop a mix.<br><span class="grad">Get the tracklist.</span></h1>'
        '<p class="lede">Paste a SoundCloud, YouTube or Mixcloud link. It listens to the set in '
        "short windows, asks the recognition engines what is playing, and hands you a "
        "click-to-jump tracklist with honest confidence for every track.</p>"
        + _form_html(csrf_token=csrf_token, state=form_state)
        + '<p class="lede reassurance">You can close this tab while the scan runs.</p>'
        + "</header>"
        + activity
        + '<section class="library"><h2 class="sec">Your mixes</h2>'
        + _library_stats_html(sets)
        + _mixes_block(sets, csrf_token=csrf_token, failed_runs=failed_runs)
        + "</section>"
        + _footer_html()
        + "</main>"
    )
    script = _PROGRESS_JS + _FORM_JS + _HOME_JS + _LIBRARY_JS
    return _page_shell("ID'er — your mixes", body, script)


def _new_html(prefill: str = "", csrf_token: str = "") -> bytes:
    """Compatibility helper for tests/callers; ``/new`` redirects to this single home form."""

    return _home_html([], [], csrf_token, form_state=_FormState(url=prefill))


def _job_steps(job: Job) -> list[tuple[str, str, str]]:
    """The step tracker for this job — phase key, label, one-line sublabel — in pipeline order."""

    steps: list[tuple[str, str, str]] = []
    if job.build_index:
        steps.append(("build_index", "Index", "fingerprint the uploader's tracks"))
    steps += [
        ("ingest", "Fetch", "download the mix"),
        ("decode", "Decode", "to raw audio"),
        ("windows", "Prepare", "divide the set for listening"),
        ("recognise", "Listen", "ask the engines"),
        ("hints", "Hints", "read the comments"),
        ("fuse", "Line up", "put the tracks in order"),
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

    ``ingest._load_ingest_cached`` verifies the completion sidecar and the media-key hash, so a
    partial or altered download is never served; the result is memoised on the job.
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
        cached = _load_ingest_cached(work_root, job.target)
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


def _job_title(job: Job) -> str:
    """What to call this job on screen: its resolved set title, else a plain platform label.

    Never the submitted target (U-F31): a SoundCloud private-share link is a secret token, and a
    local path is the owner's filesystem — neither belongs in a heading, a document title or an
    embedded script constant.  The same fallback the home activity cards use.
    """

    plan = plan_embed_from_url(job.target)
    return job.resolved_title or f"{PLATFORM_NAMES.get(plan.kind, 'Local')} mix"


def _outcome_copy_js() -> dict[str, list[str]]:
    """:data:`_OUTCOME_COPY` in a JSON-safe shape for the progress page's script."""

    return {key: list(value) for key, value in _OUTCOME_COPY.items()}


#: The progress page's four plain stages; every pipeline phase belongs to one of them.
JOB_STAGES: tuple[tuple[str, str], ...] = (
    ("fetch", "Fetch"),
    ("listen", "Listen"),
    ("decide", "Decide"),
    ("done", "Done"),
)


def _scan_html() -> str:
    """The live progress card: stage stepper, the percentage and phase, the bar, the tiles, the
    listening map, and the calm "taking longer than usual" note (shown only on a real overrun)."""

    stages = "".join(
        f'<li class="stage" data-stage="{key}"><i aria-hidden="true"></i><span>{name}</span></li>'
        for key, name in JOB_STAGES
    )
    return (
        '<section class="scan" id="scan">'
        f'<ol class="stages" id="stages" aria-label="Stages">{stages}</ol>'
        '<div class="scan-top"><div class="pct" id="pct">0<small>%</small></div>'
        '<div class="phase-line" aria-live="polite"><div class="phase-name" id="phase-name">'
        'Starting</div><div class="phase-msg" id="phase-msg"></div></div>'
        '<div class="tiles"><span><b id="t-eta">…</b>time left</span>'
        '<span><b id="t-elapsed">—</b>elapsed</span>'
        '<span><b id="t-step">—</b>stage</span></div></div>'
        '<div class="pbar" id="pbar" aria-hidden="true"><span id="pbar-fill"></span></div>'
        '<div class="cells-wrap"><div class="cells-k"><span>Listening map</span>'
        '<span id="t-win"></span></div>'
        '<div class="cells idle" id="cells" aria-hidden="true"></div></div>'
        '<p class="slow-note" id="slow-note" role="status" hidden><b>Taking longer than '
        "usual.</b> <span>Nothing is wrong — the bar is holding until this step finishes."
        "</span></p>"
        '<p class="flavour" id="flavour"></p>'
        '<p class="reassurance">You can close this tab — the scan will keep running.</p>'
        "</section>"
    )


def _outcome_html() -> str:
    return (
        '<section class="outcome" id="outcome"><div class="confetti" id="confetti"></div>'
        '<h2 id="o-title"></h2><p id="o-sub"></p><pre class="err" id="o-err"></pre>'
        '<div class="row" id="o-row"></div></section>'
    )


def _steps_html(steps: list[tuple[str, str, str]]) -> str:
    """Every pipeline step, in order: the detail behind the four stages (inside the log panel)."""

    items = "".join(
        f'<div class="step" data-step="{html.escape(key)}"><div class="k"><i>{n}</i>'
        f"{html.escape(name)}</div><small>{html.escape(sub)}</small></div>"
        for n, (key, name, sub) in enumerate(steps, start=1)
    )
    return f'<div class="steps">{items}</div>'


def _job_page_html(job: Job, csrf_token: str = "") -> bytes:
    label = html.escape(_job_title(job))
    plan = plan_embed_from_url(job.target)
    platform = plan.kind if plan.kind in ("soundcloud", "youtube", "mixcloud") else "file"
    steps = _job_steps(job)
    body = (
        topbar_html(back=True, new=True) + '<main class="job"><header class="job-head">'
        '<div class="titles"><p class="eyebrow" id="eyebrow">Analysing</p>'
        f'<h1 id="title">{label}</h1>'
        f"{platform_chip(platform)}</div>"
        '<div class="job-actions"><span class="st" id="status">…</span>'
        '<button class="btn danger" id="cancel" type="button">Cancel</button></div></header>'
        + _scan_html()
        + _outcome_html()
        + _job_player_html(plan, job)
        + '<details class="log"><summary>Steps and log</summary>'
        + _steps_html(steps)
        + '<pre id="log"></pre></details>'
        + _footer_html()
        + "</main>"
    )
    script = (
        f"var JOB_ID={json.dumps(job.id)};"
        f"var RETRY_HREF={json.dumps(f'/?job={job.id}')};"
        f"var OUTCOME_COPY={json.dumps(_outcome_copy_js())};"
        f"var STEPS={json.dumps(steps)};var CSRF_TOKEN={json.dumps(csrf_token)};"
        + _PROGRESS_JS
        + _JOB_JS
        + _PLAYER_JS
    )
    return _page_shell("Analysing — ID'er", body, script)


# --------------------------------------------------------------------------------------------------
# Retired server entry points (4a-ii)
# --------------------------------------------------------------------------------------------------
# The stdlib ``ThreadingHTTPServer`` and its request handler are gone: ``idea serve``, ``idea truth
# review`` and every test reach the one FastAPI/uvicorn application in :mod:`idea_web`.  These names
# remain only because callers still import them from here, and they import the web layer lazily so
# the CLI, the pipeline and the analysis worker process never load an HTTP framework.


def make_server(
    work_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    config: AppConfig | None = None,
    job_manager: Any = None,
) -> Any:
    """Compatibility alias for :func:`idea_web.server.make_server`."""

    from idea_web.server import make_server as make_loopback_server

    return make_loopback_server(
        work_root, host=host, port=port, config=config, job_manager=job_manager
    )


def serve_in_background(
    work_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    config: AppConfig | None = None,
    job_manager: Any = None,
) -> Any:
    """Compatibility alias for :func:`idea_web.server.serve_in_background`."""

    from idea_web.server import serve_in_background as serve_loopback_in_background

    return serve_loopback_in_background(
        work_root, host=host, port=port, config=config, job_manager=job_manager
    )


def __getattr__(name: str) -> Any:
    if name == "RunningServer":
        from idea_web.server import RunningServer

        return RunningServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

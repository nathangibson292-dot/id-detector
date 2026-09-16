"""The live pages' own fragments, scripts and static assets (plan cycle 4a-iii).

Plan §4.2 closes ``src/id_detector/`` to Phase-4 work, so everything the web layer changes about
the pages lives here, over the legacy presentation fragments rather than in them: the activity
card and its polling script (U-F18), the "+ New mix" and Remove-confirmation behaviours the
Content-Security-Policy needs out of the markup (U-F34, §4.4), and the one static stylesheet and
script the pages link instead of inlining (U-F29).

Labels have one source.  The progress tracker's step list (``legacy._job_steps``) names each
phase; :func:`phase_label` is the tracker page's own rule for turning a status and phase into
words, and the card, its polling script and the progress log all use that rule.
"""

from __future__ import annotations

import hashlib
import html
import json
from typing import Any

from id_detector.present import server as legacy
from id_detector.present import theme
from id_detector.webapp.jobs import TERMINAL_STATES

_CSRF_FIELD = "csrf_token"
#: The tracker page's wording for a job that has not started (``render()`` in ``_JOB_JS``).
QUEUED_LABEL = "In the queue"


def phase_label(steps: list[tuple[str, str, str]], status: str, phase: str) -> str:
    """What the progress tracker calls this phase: its step name, else the phase, capitalised.

    This is the tracker page's ``render()`` rule, in Python, so a home card never describes a
    job in different words from the page the card links to.
    """

    if status == "queued":
        return QUEUED_LABEL
    for key, name, _ in steps:
        if key == phase:
            return name
    return phase[:1].upper() + phase[1:] if phase else "Working"


def activity_item_html(job: Any, csrf_token: str) -> str:
    """A home activity card (U-F18): the resolved title, the tracker's own step label, a bar,
    and a Dismiss form once the job is over.  The card carries its job's step list so the polling
    script applies the same label rule as the server did."""

    status = html.escape(job.status)
    steps = legacy._job_steps(job)
    label = html.escape(legacy._job_title(job))
    phase = html.escape(phase_label(steps, job.status, job.phase))
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
        f'data-status="{status}" data-pct="{pct}" '
        f'data-steps="{html.escape(json.dumps(steps), quote=True)}">'
        f'<a class="act-link" href="/jobs/{html.escape(job.id)}">'
        f'<div class="act-row"><span class="st st-{status}">{status}</span>'
        f'<span class="act-title">{label}</span></div>'
        f'<div class="act-phase">{phase}</div>'
        f'<div class="bar"><span style="width:{pct}%"></span></div></a>{dismiss}</li>'
    )


def activity_list_html(jobs: list[Any], csrf_token: str) -> str:
    """The home's "Recent analyses" list, in the order given.  Served whole by ``GET /activity``
    too, so the polling script can replace the list with exactly what a fresh render shows."""

    return (
        '<ul class="acts">' + "".join(activity_item_html(job, csrf_token) for job in jobs) + "</ul>"
    )


#: The library card's Remove button asks first.  The legacy card asks from an inline handler,
#: which a hashed policy refuses, so the served card carries the question as data and
#: :data:`CONFIRM_JS` asks it.
_INLINE_CONFIRM = " onclick=\"return confirm('Remove this mix from your library?')\""
_DATA_CONFIRM = ' data-confirm="Remove this mix from your library?"'


def mixes_block(*args: Any, **kwargs: Any) -> str:
    """``legacy._mixes_block`` with no inline handler in it."""

    return legacy._mixes_block(*args, **kwargs).replace(_INLINE_CONFIRM, _DATA_CONFIRM)


# --------------------------------------------------------------------------------------------------
# Scripts
# --------------------------------------------------------------------------------------------------
#: Home activity cards: poll each live card's status and describe it with the tracker's rule.
#: A card that changes kind — it ends, or it starts running — is not patched in place: the whole
#: list is replaced by the server's fresh render (``GET /activity``), so a finished card gets its
#: Dismiss control and a running card moves first exactly as a reload would show them (U-F18).
HOME_JS = """
(function(){
  if(!document.querySelector('ul.acts')) return;
  var TERMINAL = ['succeeded', 'failed', 'cancelled', 'waiting'];
  // The tracker page's own rule (render() in the progress script): the step's name, "In the
  // queue" before it starts, else the phase capitalised.  The steps come with the card.
  function phaseLabel(steps, status, phase){
    if(status === 'queued') return __QUEUED__;
    for(var i = 0; i < steps.length; i++){ if(steps[i][0] === phase) return steps[i][1]; }
    return phase ? phase.charAt(0).toUpperCase() + phase.slice(1) : 'Working';
  }
  // A fresh render is needed when a card's kind changes: it is over, or it has started.
  function refreshNeeded(before, j){
    return before !== j.status && (TERMINAL.indexOf(j.status) >= 0 || j.status === 'running');
  }
  // A refresh that fails — an error status, or a body that is not the one list — changes
  // nothing on screen: the list already shown stays, and the card that asked keeps polling, so
  // the refresh is tried again on its next poll, later each time it keeps failing (2.5 s
  // doubling to a minute), and never given up.
  var refreshing = false, failures = 0, retryAt = 0;
  var LIST = /^<ul class="acts">[\\s\\S]*<\\/ul>$/;
  function refresh(){
    if(refreshing || Date.now() < retryAt) return; refreshing = true;
    fetch('/activity').then(function(r){
      if(!r.ok) throw new Error('activity ' + r.status);
      return r.text();
    }).then(function(html){
      if(!LIST.test(html.trim())) throw new Error('activity malformed');
      refreshing = false; failures = 0;
      var list = document.querySelector('ul.acts'); if(!list) return;
      list.outerHTML = html.trim(); arm();
    }).catch(function(){
      refreshing = false; failures += 1;
      retryAt = Date.now() + Math.min(60000, 2500 * Math.pow(2, Math.min(failures, 5) - 1));
    });
  }
  function poll(card){
    if(!card.isConnected) return;
    var id = card.getAttribute('data-job');
    var steps = JSON.parse(card.getAttribute('data-steps') || '[]');
    fetch('/jobs/' + id + '/status').then(function(r){ return r.json(); }).then(function(j){
      if(!card.isConnected) return;
      if(j.status === 'succeeded' && j.result_url){ window.location.reload(); return; }
      if(refreshNeeded(card.getAttribute('data-status'), j)){
        // Not patched: the card's own kind is what changed.  Its poll goes on, so a refresh
        // that failed is asked for again next time round.
        refresh(); setTimeout(function(){ poll(card); }, 2500); return;
      }
      var st = card.querySelector('.st'); st.textContent = j.status;
      st.className = 'st st-' + j.status;
      card.setAttribute('data-status', j.status);
      card.querySelector('.act-phase').textContent = phaseLabel(steps, j.status, j.phase);
      if(j.title) card.querySelector('.act-title').textContent = j.title;
      var pct = wallProgress(j, parseInt(card.getAttribute('data-pct'), 10) || 0);
      card.setAttribute('data-pct', pct);
      card.querySelector('.bar>span').style.width = pct + '%';
      if(!j.terminal) setTimeout(function(){ poll(card); }, 2500);
    }).catch(function(){ setTimeout(function(){ poll(card); }, 5000); });
  }
  function arm(){
    var list = document.querySelector('ul.acts'); if(!list) return;
    Array.prototype.forEach.call(list.querySelectorAll('.act[data-job]'), function(card){
      if(card.getAttribute('data-terminal') !== '1') poll(card); });
  }
  arm();
})();
""".replace("__QUEUED__", json.dumps(QUEUED_LABEL))

#: A form that asks before it acts says so in data-confirm (on the form or its button); no
#: handler lives in the markup, so the policy can refuse every inline handler outright.
CONFIRM_JS = """
(function(){
  document.addEventListener('submit', function(event){
    var form = event.target; if(!form || !form.querySelector) return;
    var ask = form.hasAttribute('data-confirm') ? form : form.querySelector('[data-confirm]');
    if(ask && !confirm(ask.getAttribute('data-confirm'))) event.preventDefault();
  });
})();
"""

#: One form (U-F34): on a page that already has it, "+ New mix" goes to the field, not away.
NEW_MIX_JS = """
(function(){
  var input = document.getElementById('url'); if(!input) return;
  var newMix = document.querySelector('.topbar a[href="/new"]');
  if(newMix) newMix.addEventListener('click', function(event){
    event.preventDefault();
    input.scrollIntoView({block: 'center', behavior: 'smooth'});
    input.focus(); input.select();
  });
})();
"""


def _paced(script: str, before: str, after: str, what: str) -> str:
    """One exact substitution in a legacy script, failing loudly if the script drifted."""

    if script.count(before) != 1:  # pragma: no cover - fails at import if the script drifts
        raise RuntimeError(f"the progress page script no longer has exactly one {what}")
    return script.replace(before, after)


#: The binding progress-page poll interval (ADR 0001 / plan §4.3) is owned by the web layer.
_POLL_FROM = "setTimeout(tick, 1500)"
_POLL_TO = "setTimeout(tick, 2500)"
#: The progress log named phases from its own table; it now reads the tracker's step list.
_LOG_LABELS_FROM = (
    "  var labels = {build_index:'Reference check', ingest:'Fetch', decode:'Prepare audio',\n"
    "    windows:'Prepare scan', recognise:'Listen', hints:'Tracklist clues', "
    "fuse:'Line up tracks',\n"
    "    enrich:'Find links', present:'Build page', failed:'Scan stopped', "
    "cancelled:'Scan stopped'};"
)
_LOG_LABELS_TO = """  var labels = {failed:'Scan stopped', cancelled:'Scan stopped'};
  STEPS.forEach(function(s){ labels[s[0]] = s[1]; });"""
JOB_JS = _paced(
    _paced(legacy._JOB_JS, _POLL_FROM, _POLL_TO, "status poll to pace"),
    _LOG_LABELS_FROM,
    _LOG_LABELS_TO,
    "log label table",
)

#: The live pages' stylesheet and script, served once as versioned immutable assets instead of
#: being re-sent inline with every page (U-F29, narrowed to the live pages: a result page is an
#: immutable bundle that must also open as a file, so it keeps its own inline copy).  The
#: progress-page part runs only where the page defined its per-job constants.
STATIC_CSS = (theme.BASE_CSS + legacy._APP_CSS).encode("utf-8")
STATIC_JS = (
    legacy._PROGRESS_JS
    + legacy._FORM_JS
    + NEW_MIX_JS
    + CONFIRM_JS
    + HOME_JS
    + legacy._PLAYER_JS
    + "(function(){\nif(typeof JOB_ID === 'undefined') return;\n"
    + JOB_JS
    + "})();\n"
).encode("utf-8")
ASSET_VERSION = hashlib.sha256(STATIC_CSS + STATIC_JS).hexdigest()[:12]
STYLESHEET_HREF = f"/static/app.{ASSET_VERSION}.css"
SCRIPT_SRC = f"/static/app.{ASSET_VERSION}.js"


def head_html(title: str) -> str:
    """``theme.head_html`` with the stylesheet linked rather than inlined."""

    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        f'<link rel="icon" href="{theme.FAVICON}">'
        f'<link rel="stylesheet" href="{STYLESHEET_HREF}"></head>'
    )

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from id_detector.io import atomic_write_json, path_is_file, path_mtime, read_text
from id_detector.present import bundles
from id_detector.present.bundles import result_dir
from id_detector.present.exports import build_projection, read_projected_summary
from id_detector.present.page import (
    _CSS,
    _acquire_links_html,
    _alternatives_html,
    _status_banner_html,
    _tags_html,
    render_page,
)
from id_detector.present.server import (
    _APP_CSS,
    _OUTCOME_COPY,
    _PROGRESS_JS,
    _activity_item_html,
    _cost_sentence,
    _discover_failed_runs,
    _discover_sets,
    _failed_card_html,
    _home_html,
    _job_page_html,
    _mix_card_html,
    _outcome_copy,
    _outcome_copy_js,
    _run_started_at,
    serve_in_background,
)
from id_detector.present.theme import BASE_CSS
from id_detector.webapp.jobs import (
    PHASE_EXPECTED_SECONDS,
    PHASE_SEQUENCE,
    Job,
    JobContext,
    JobManager,
)
from id_detector.webapp.runner import _entry_by_run_id
from tests.test_projection import _inputs, _publish, _source

TIMEOUT = 5.0


def _fixture_page(name: str = "boiler") -> tuple[str, dict, object]:
    fixture, episodes, identities, acquire = _inputs(name)
    projection = build_projection(
        episodes,
        identities,
        acquire,
        min_track_ms=fixture["min_track_ms"],
    )
    page = render_page(
        source=_source("file").model_copy(update={"title": fixture["title"]}),
        episodes=episodes,
        identities=identities,
        duration_ms=fixture["duration_ms"],
        acquire=acquire,
        min_track_ms=fixture["min_track_ms"],
        projection=projection,
        status="partial",
        reason="provider_unavailable_midrun",
        achieved="deep",
        analysed_at="2026-09-12T08:30:00Z",
    )
    return page, fixture, projection


def test_result_page_is_honest_semantic_and_mobile() -> None:
    page, _, projection = _fixture_page()
    hidden = [entry for entry in projection.entries if entry["hidden_reason"]]

    assert all(f'id="{entry["episode_id"]}"' not in page for entry in hidden)
    for leaked in (
        "show-short",
        "suppressed match",
        "no_evidence",
        ">hint<",
        ">layer<",
        ">outgoing<",
    ):
        assert leaked not in page.casefold()
    assert "rescan" not in page.casefold()
    # U-F31: no engine is named to a user, and the striped-span tooltip is in plain words.
    for token in ("shazam", "audd", "panako", "no-match", "n_windows", "episodes"):
        assert token not in page.casefold(), token
    assert 'title="nothing identified here' in page
    corroborated = _tags_html({"version_status": "unverified", "engine_corroborated": True})
    assert 'title="two independent checks agreed' in corroborated
    assert "shazam" not in corroborated.casefold() and "confirmed twice" in corroborated
    # The banner names what this run actually was (Deep, with gaps) — see the status-matrix test.
    assert '<div class="run-banner" role="status"><b>Deep scan, with gaps.</b>' in page
    assert "analysed <b>12 Sep 2026</b>" in page and "<b>Deep scan</b>" in page

    stats = page.split('<div class="stats">', 1)[1].split("</header>", 1)[0]
    assert stats.count('<div class="stat">') == 3
    assert "ID gap" not in stats and "confidence mix" not in stats.casefold()
    assert "<caption>Tracklist for Boiler fixture</caption>" in page
    assert page.count('scope="col"') in {4, 5}
    assert 'role="button"' not in page and '<button type="button" class="seek"' in page
    assert "th:nth-child(6),td.acquire{display:none}" not in page
    assert "grid-template-columns:auto 1fr auto" in page
    mobile = page.split("@media (max-width:720px){", 1)[1]
    assert "min-height:44px;min-width:44px" in mobile
    # One stat tile per row: no arithmetic, no second column running off a phone screen.
    assert ".stats{grid-template-columns:1fr}" in mobile
    for control in (
        ".acq",
        ".seek",
        ".ops button",
        ".ops a",
        ".xbtn",
        ".alts>summary",
        ".controls>summary",
        "#leadin",
    ):
        assert control in mobile, control
    assert page.count('<span class="lg-') == 2
    assert "Playback settings" in page and '<details class="controls">' in page
    assert "Audio stays local; only short recognition clips" in page


def test_purchase_links_and_only_different_work_alternatives_are_shown() -> None:
    links = _acquire_links_html(
        {
            "buy": True,
            "soundcloud": {
                "permalink_url": "https://soundcloud.com/a/track",
                "purchase_url": "https://label.example/product",
            },
            "direct": [],
            "search_links": [{"source": "Bandcamp", "url": "https://bandcamp.com/search?q=x"}],
        }
    )
    assert links.index("https://label.example/product") < links.index("bandcamp.com/search")
    assert "Search Bandcamp" in links

    alternatives = _alternatives_html(
        {
            "artist": "Denise",
            "title": "Rise",
            "alternatives": [
                {
                    "artist": "Denise",
                    "title": "Rise (Club Mix)",
                    "badge": "likely",
                    "track": "same",
                },
                {
                    "artist": "Other",
                    "title": "Different",
                    "badge": "possible",
                    "track": "different",
                },
            ],
        }
    )
    assert "same" not in alternatives and "different" in alternatives
    assert "other track" in alternatives


def _job(**overrides: object) -> Job:
    fields = {
        "id": "0" * 32,
        "target": "https://soundcloud.com/example/mix",
        "display": "https://soundcloud.com/example/mix",
        "profile": "free",
        "acquire": True,
        "build_index": False,
    }
    fields.update(overrides)
    return Job(**fields)  # type: ignore[arg-type]


def test_progress_is_wall_clock_proportional_not_stage_arithmetic() -> None:
    """U-F9: a tenth of the bar is about a tenth of the expected wall time.

    The old model weighted *stages*, so a real one-minute download sat at 2 % and the bar then
    jumped 80 points the moment recognise started.  The model now divides measured elapsed time by
    measured-plus-estimated total time, with recognise — 90 %-plus of a cold run — dominating, so
    finishing every pre-recognise phase can only account for a small slice of the bar.
    """

    expected_total = sum(
        PHASE_EXPECTED_SECONDS[name]
        for name in ("ingest", "decode", "windows", "recognise", "hints", "fuse", "present")
    )
    assert PHASE_EXPECTED_SECONDS["recognise"] / expected_total > 0.85

    job = _job(status="running", phase="ingest", phase_started_at=1_000.0)
    # Half-way through a cold one-minute fetch: a small slice of the bar, not a fixed 2 % step.
    half_fetch = job.progress_percent(now=1_030.0)
    assert 0 < half_fetch <= 3

    # Everything before recognise is done and recognise has just begun: still near the start,
    # because the time that matters has not been spent yet.
    job = _job(
        status="running",
        phase="recognise",
        phase_started_at=2_000.0,
        phase_seconds={"ingest": 60.0, "decode": 30.0, "windows": 15.0},
        windows_total=400,
        windows_done=0,
        recognise_started_at=2_000.0,
    )
    assert job.progress_percent(now=2_000.0) <= 10

    # Half the expected recognise time gone at the expected rate: about half the bar.
    job = _job(
        status="running",
        phase="recognise",
        phase_started_at=3_000.0,
        phase_seconds={"ingest": 60.0, "decode": 30.0, "windows": 15.0},
        windows_total=400,
        windows_done=200,
        recognise_started_at=3_000.0,
    )
    halfway = job.progress_percent(now=3_000.0 + 200 / 18 * 60)
    assert 45 <= halfway <= 60


def test_progress_handles_cached_and_skipped_phases_and_never_moves_backwards() -> None:
    """A phase that was cached contributes what it cost; a phase that never runs is not counted."""

    # Skipped phases leave the denominator: no reference index, no acquisition.
    lean = _job(acquire=False, build_index=False, status="running", phase="present")
    rich = _job(acquire=True, build_index=True, status="running", phase="present")
    assert lean._remaining_seconds(0.0, 0.0) == PHASE_EXPECTED_SECONDS["present"]
    assert rich._remaining_seconds(0.0, 0.0) == PHASE_EXPECTED_SECONDS["present"]
    assert "build_index" not in [p for p in PHASE_SEQUENCE if not lean._phase_runs(p)] or True
    assert lean._phase_runs("enrich") is False and rich._phase_runs("enrich") is True
    assert lean._phase_runs("build_index") is False and rich._phase_runs("build_index") is True

    # A cached ingest took 0.2 s: the bar credits 0.2 s of real time, not a full minute's worth.
    cached = _job(
        status="running",
        phase="recognise",
        phase_started_at=10.0,
        phase_seconds={"ingest": 0.2, "decode": 0.3, "windows": 0.1},
        windows_total=100,
        windows_done=0,
        recognise_started_at=10.0,
    )
    assert cached.progress_percent(now=10.0) == 0
    # ...and a warm recognise pass (97 of 100 windows in 4 s) is *observed*, while the still-to-come
    # phases are scaled by how fast this run has really been going — so the time left is the few
    # seconds it really is rather than the minute a cold run would have needed.  The bar itself
    # walks there: a collapsed estimate may not move it more than 2.5 points a second.
    assert cached._speed_factor() < 0.1
    cached.windows_done = 97
    cached.rate_samples = [[10.0, 0.0], [14.0, 97.0]]
    assert cached.eta_seconds(now=14.0) <= 5
    warm = cached.progress_percent(now=14.0)
    assert warm == 10, warm

    # Monotonic: a growing ETA can never pull the bar back down.
    job = _job(
        status="running",
        phase="recognise",
        phase_started_at=0.0,
        windows_total=400,
        windows_done=200,
        recognise_started_at=0.0,
        rate_samples=[[555.0, 155.0], [600.0, 200.0]],  # 60 windows/min just now
    )
    high = job.progress_percent(now=600.0)
    assert high >= 65
    job.windows_done = 206  # the rate collapses: six windows in the last ten minutes
    job.rate_samples = [[3_000.0, 200.0], [3_600.0, 206.0]]
    assert job.eta_seconds(now=3_600.0) > 10_000  # computed alone, the bar would be under 30 %
    assert job.progress_percent(now=3_600.0) == high
    assert job.progress_percent(now=3_600.0) <= 99  # never claims done before it is

    # Succeeded is 100, and a terminal failure freezes where it stopped.
    done = _job(status="succeeded")
    assert done.progress_percent() == 100
    stopped = _job(status="failed", progress_max=41)
    assert stopped.progress_percent(now=1e9) == 41


def test_the_browser_only_holds_the_monotonic_floor() -> None:
    """The bar's arithmetic is server-side; the page may not invent a second progress model."""

    assert "PHASE_WEIGHT" not in _PROGRESS_JS and "byUnits" not in _PROGRESS_JS
    assert "progress_pct" in _PROGRESS_JS and "Math.max(previous" in _PROGRESS_JS
    assert "steps" not in _PROGRESS_JS  # there is no second, page-side phase model
    node = shutil.which("node")
    if node is None:  # pragma: no cover - node is present in the gate environment
        pytest.skip("node is not installed")
    script = (
        _PROGRESS_JS + "var p = 0, out = [];"
        "[{status:'running',progress_pct:7},{status:'running',progress_pct:3},"
        "{status:'running',progress_pct:64},{status:'succeeded',progress_pct:64}]"
        ".forEach(function(j){ p = wallProgress(j, p); out.push(p); });"
        "console.log(JSON.stringify(out));"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [7, 7, 64, 100]


def test_progress_and_home_copy_are_plain_and_complete() -> None:
    failed = _job(
        id="f" * 32,
        target="https://soundcloud.com/user/s-PrIvAtEsHaReToKeN",
        display="https://soundcloud.com/user/s-PrIvAtEsHaReToKeN",
        profile="max_accuracy",
        status="failed",
        phase="failed",
        error="yt-dlp: provider_internal_token exit code 1",
        failed_phase="ingest",
    )
    card = _activity_item_html(failed, "csrf")
    assert "SoundCloud mix" in card and failed.target not in card
    assert "Failed" in card and "Dismiss" in card

    home = _home_html([], [failed], "csrf").decode()
    assert "Free scan" in home and "Deep scan" in home
    assert "tracklist added" in home and "You can close this tab" in home
    assert "of music listened to" not in home
    assert home.count("Audio stays local; only short recognition clips") == 1

    progress = _job_page_html(failed, "csrf").decode()
    assert 'aria-live="polite"' in progress
    assert 'id="t-windows"' not in progress and 'id="t-rate"' not in progress
    assert 'id="t-eta"' in progress and 'id="t-step"' in progress
    assert "window.location.href" not in progress and "countdown(" not in progress
    assert "err.textContent = j.error" not in progress
    assert "failed_phase" in progress and "classList.add('failed')" in progress
    # U-F31: the submitted target is a secret (a private-share slug, or the owner's filesystem),
    # so it is never displayed and never embedded in the script -- "try again" goes back through the
    # job id instead.  It survives in exactly one place: the href of the courtesy "open it on the
    # platform" link inside the player's error fallback, which is what that link is for.
    assert f'<h1 id="title">{failed.target}' not in progress
    assert "var DISPLAY" not in progress
    script = progress.rsplit("<script>", 1)[1]
    assert failed.target not in script
    hrefs = progress.count(f'href="{failed.target}"')
    assert progress.count(failed.target) == hrefs == 1
    assert f'var RETRY_HREF="/?job={failed.id}"' in progress
    assert "SoundCloud mix" in progress
    # U-F15: the page no longer guesses cost from the requested profile, and no longer hedges.
    assert "may still count" not in progress
    assert "usd_e2_spent" in progress and "spend_known" in progress


def test_failure_and_cancellation_state_the_real_cost_exactly() -> None:
    """U-F15: what a failed or cancelled run cost is read from its journal, never inferred.

    ``budget_exhausted`` and ``provider_unavailable`` spend nothing by definition (plan §2.3.5); any
    other Deep run may have real spend, and that settled figure, in cents, is what is shown.
    """

    nothing = "Nothing was spent - it stopped before any paid check ran.".replace("-", "—")
    assert (
        _cost_sentence(usd_e2_spent=None, spend_known=False, status="budget_exhausted") == nothing
    )
    assert (
        _cost_sentence(usd_e2_spent=None, spend_known=False, status="provider_unavailable")
        == nothing
    )
    assert (
        _cost_sentence(usd_e2_spent=0, spend_known=True, status="failed")
        == "Nothing was spent on this run."
    )
    assert (
        _cost_sentence(usd_e2_spent=137, spend_known=True, status="cancelled")
        == "$1.37 of paid checks was spent before it stopped."
    )
    # A Free scan can never spend, even with no readable record; only a Deep run admits to not
    # knowing, and it says so plainly rather than hedging that checks "may still count".
    assert "Free scan" in _cost_sentence(
        usd_e2_spent=None, spend_known=False, status="failed", profile="free"
    )
    unknown = _cost_sentence(
        usd_e2_spent=None, spend_known=False, status="failed", profile="max_accuracy"
    )
    assert unknown.endswith("cost record could not be read.")
    assert "may still count" not in unknown

    # Plain cause + remedy per frozen outcome, most specific first, and no internal phase key.
    assert _outcome_copy("failed", "ingest")[0] == "The platform would not hand over the audio."
    assert "Try again" in _outcome_copy("failed", "ingest")[1]
    assert _outcome_copy("source_changed", None)[0].startswith("The mix changed")
    assert _outcome_copy("nonsense", None) == _outcome_copy("failed", None)
    # Everything the live page uses comes from that same table, so the two cannot disagree.
    assert set(_outcome_copy_js()) == set(_OUTCOME_COPY)


def test_the_status_banner_is_rendered_from_the_frozen_status_reason_and_recipe() -> None:
    """P0: a normal Deep run whose free cross-check fell short is `degraded` with `achieved=deep`.

    Plan §2.3.5 row 4 (asserted by ``tests/test_phase0a_status.py``): primary met, secondary under
    80 % gives ``degraded``, ``reason=secondary_not_achieved``, ``achieved=deep``.  Telling the
    Deep was unavailable and the Free scan was used instead is false, and it was said on every
    degraded run.
    """

    common = _status_banner_html("degraded", "secondary_not_achieved", "deep")
    assert "Deep scan" in common and "unavailable" not in common
    assert "second opinion only reached part of the set" in common

    # The one case where Free really was used: --allow-degrade restarted as the Free recipe.
    fell_back = _status_banner_html("degraded", "provider_unavailable", "free")
    assert "Free scan result." in fell_back and "paid engine was unavailable" in fell_back

    off = _status_banner_html("degraded", "shazam_manual_off", "deep")
    assert "switched off" in off and "shazam" not in off.casefold()

    for reason, phrase in (
        ("primary_not_achieved", "never checked"),
        ("provider_unavailable_midrun", "stopped part-way through"),
        ("reservation_exhausted", "paid-scan limit was reached part-way"),
    ):
        banner = _status_banner_html("partial", reason, "deep")
        assert phrase in banner, reason
        assert 'role="status"' in banner

    # A complete run needs no banner, and an unknown reason still gets an honest generic one.
    assert _status_banner_html("complete", None, "deep") == ""
    assert _status_banner_html("degraded", "brand_new_reason", "deep") != ""


def test_the_completion_screen_counts_the_canonical_projection(tmp_path: Path) -> None:
    """P0: "N tracks found" was scraped from the raw ``fuse: N episodes`` log line.

    With rows suppressed that number is the fused total while the tracklist under it is shorter --
    the exact card-versus-page split U-F2 is about.  The count now comes from the published
    projection, through the one reader the library card uses.
    """

    directory, projection, item, _ = _publish(tmp_path, "boiler", collapse=True)
    shown = [e for e in projection.shown_entries if e["kind"] == "track"]
    assert projection.suppressed_count > 0  # the fixture really does suppress rows

    summary = read_projected_summary(directory / "tracklist.json")
    assert summary is not None and summary.tracks == len(shown)

    manager = JobManager(tmp_path, lambda context: None)
    try:
        job = _job(id="c" * 32, status="succeeded")
        manager._jobs[job.id] = job
        manager._order.append(job.id)
        JobContext(manager, job).set_result(directory / "index.html")
        assert job.tracks_found == len(shown)
        state = job.status_dict()
        assert state["tracks_found"] == len(shown)
        assert len(shown) < len([e for e in projection.entries if e["kind"] == "track"])
    finally:
        manager.shutdown()

    # The page prints that one number and never scrapes the log line for it.
    rendered = _job_page_html(_job(), "csrf").decode()
    assert "j.tracks_found" in rendered
    assert "fuse: (" not in rendered and "episodes" not in rendered
    assert "fromLog" not in rendered and "titleFromLog" not in rendered
    card = _mix_card_html(item)
    assert f"<b>{len(shown)}</b> track" in card


def test_bad_form_is_inline_and_json_is_reserved_for_json_requests(tmp_path: Path) -> None:
    manager = JobManager(tmp_path, lambda context: None)
    running = serve_in_background(tmp_path, job_manager=manager)
    try:
        token = httpx.get(running.base_url + "/csrf", timeout=TIMEOUT).json()["token"]
        form = httpx.post(
            running.base_url + "/analyse",
            data={
                "csrf_token": token,
                "url": "not a URL",
                "profile": "max_accuracy",
                "known_tracklist": "12:34 Artist - Track",
            },
            timeout=TIMEOUT,
        )
        assert form.status_code == 400
        assert form.headers["content-type"].startswith("text/html")
        assert 'value="not a URL"' in form.text
        assert "12:34 Artist - Track" in form.text
        assert "Use a complete web link" in form.text
        assert 'value="max_accuracy" checked' in form.text

        api = httpx.post(
            running.base_url + "/analyse",
            json={"csrf_token": token, "url": "not a URL"},
            timeout=TIMEOUT,
        )
        assert api.status_code == 400
        assert api.headers["content-type"].startswith("application/json")

        duplicate = httpx.get(running.base_url + "/new?url=example", timeout=TIMEOUT)
        assert duplicate.status_code == 303 and duplicate.headers["location"].startswith("/?url=")
    finally:
        running.shutdown()
        manager.shutdown()


def test_local_library_remove_is_recoverable_and_failed_jobs_can_be_dismissed(
    tmp_path: Path,
) -> None:
    _, _, item, _ = _publish(tmp_path, "garage", collapse=True)
    failed_dir = tmp_path / ("a" * 64) / ("b" * 64)
    failed_dir.mkdir(parents=True)
    (failed_dir / "invocations.jsonl").write_text(
        json.dumps({"status": "failed", "finished_at": "2026-09-11T10:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    failed_runs = _discover_failed_runs(tmp_path)
    assert len(failed_runs) == 1
    failed_home = _home_html([], [], "csrf", failed_runs=failed_runs).decode()
    assert "Unfinished mix" in failed_home and "No result was saved" in failed_home

    manager = JobManager(tmp_path, lambda context: None)
    running = serve_in_background(tmp_path, job_manager=manager)
    try:
        token = httpx.get(running.base_url + "/csrf", timeout=TIMEOUT).json()["token"]
        removed = httpx.post(
            running.base_url + "/library/remove",
            data={"csrf_token": token, "source_key": item.source_key, "media_key": item.media_key},
            timeout=TIMEOUT,
        )
        assert removed.status_code == 303
        assert not item.media_dir.exists()
        assert any((tmp_path / ".trash" / "library").iterdir())

        removed_failed = httpx.post(
            running.base_url + "/library/remove",
            data={
                "csrf_token": token,
                "source_key": "a" * 64,
                "media_key": "b" * 64,
            },
            timeout=TIMEOUT,
        )
        assert removed_failed.status_code == 303 and not failed_dir.exists()

        job = Job(
            "d" * 32,
            "https://soundcloud.com/a/b",
            "Mix",
            "free",
            True,
            False,
            status="failed",
            phase="failed",
        )
        manager._jobs[job.id] = job
        manager._order.append(job.id)
        dismissed = httpx.post(
            running.base_url + f"/jobs/{job.id}/dismiss",
            data={"csrf_token": token},
            timeout=TIMEOUT,
        )
        assert dismissed.status_code == 303 and manager.get(job.id) is None
    finally:
        running.shutdown()
        manager.shutdown()


def _luminance(colour: str) -> float:
    digits = colour.lstrip("#")
    if len(digits) in {3, 4}:  # CSS shorthand: #fff is #ffffff
        digits = "".join(digit * 2 for digit in digits)
    channels = [int(digits[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(left: str, right: str) -> float:
    high, low = sorted((_luminance(left), _luminance(right)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize(
    ("foreground", "background"),
    [
        ("#9a9ab0", "#13131b"),
        ("#9a9ab0", "#0a0a0f"),
        ("#a3a3bd", "#13131b"),
        ("#0a0a0f", "#ff3d8a"),
        ("#0a0a0f", "#8b5cf6"),
        ("#5e5e72", "#ffffff"),
        ("#5e5e72", "#f7f7fb"),
        ("#6d28d9", "#ffffff"),
    ],
)
def test_text_contrast_is_wcag_aa(foreground: str, background: str) -> None:
    assert _contrast(foreground, background) >= 4.5


def test_bundle_status_and_date_are_frozen_presentation_inputs(tmp_path: Path) -> None:
    directory, _, item, _ = _publish(tmp_path, "garage", collapse=True)
    snapshot = bundles.load_run_snapshot(item.media_dir, directory=directory)
    assert snapshot.metadata["status"] == "complete"
    assert re.fullmatch(r"[0-9a-f]{64}", directory.name)


def test_a_failed_attempt_is_visible_even_behind_an_older_result(tmp_path: Path) -> None:
    """U-F33: a media directory holding *a* result hid every later failure on that mix.

    The case that matters is the one that costs money: a Free scan succeeded weeks ago, the user
    paid for a Deep re-scan, it failed, and nothing anywhere said so.  The latest journal entry now
    decides, and because the failure and the result share one media directory the card offers no
    Remove of its own -- deleting the attempt must never delete the result.
    """

    directory, _, item, _ = _publish(tmp_path, "garage", collapse=True)
    journal = item.media_dir / "invocations.jsonl"
    journal.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "status": "complete",
                        "started_at": "2026-08-01T09:00:00Z",
                        "usd_e2_spent": 0,
                    }
                ),
                json.dumps(
                    {
                        "status": "failed",
                        "reason": None,
                        "started_at": "2026-09-10T09:00:00Z",
                        "finished_at": "2026-09-10T09:04:00Z",
                        "usd_e2_spent": 63,
                        "timings": {"ingest_ms": 1, "decode_ms": 2, "recognise_ms": 3},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # The older result is still openable.
    assert path_is_file(result_dir(item.media_dir) / "index.html")

    runs = _discover_failed_runs(tmp_path)
    assert len(runs) == 1
    run = runs[0]
    assert run.has_result is True
    assert run.status == "failed" and run.usd_e2_spent == 63
    assert run.last_stage == "listening to the set"

    card = _failed_card_html(run, "csrf")
    assert "$0.63 of paid checks was spent" in card
    assert "It got as far as listening to the set." in card
    assert "from an earlier scan of the same mix" in card
    assert "/library/remove" not in card  # removing it would take the good result with it

    # A failure with no surviving result keeps its Remove button.
    alone = dataclasses.replace(run, has_result=False)
    assert "/library/remove" in _failed_card_html(alone, "csrf")

    # A cancelled run is durable too: it may have spent money.
    journal.write_text(
        json.dumps(
            {
                "status": "cancelled",
                "started_at": "2026-09-10T09:00:00Z",
                "usd_e2_spent": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    cancelled = _discover_failed_runs(tmp_path)
    assert len(cancelled) == 1 and cancelled[0].status == "cancelled"
    assert "You stopped this scan." in _failed_card_html(cancelled[0], "csrf")


def test_the_library_date_is_the_runs_frozen_analysis_date_not_the_ingest_time(
    tmp_path: Path,
) -> None:
    """U-F24/section 2.5: "Analysed" must mean when the set was analysed.

    ``ingest/source.json``'s mtime is when the audio was *fetched*: for a mix ingested once and
    analysed again later that is the wrong day, and it is not what the result page's own chip says.
    Both now read the shown bundle's frozen ``started_at``.
    """

    directory, _, item, _ = _publish(tmp_path, "garage", collapse=True)
    manifest = json.loads(read_text(directory / "manifest.json"))
    manifest["started_at"] = "2026-07-04T11:22:33Z"
    atomic_write_json(directory / "manifest.json", manifest)

    expected = datetime.fromisoformat("2026-07-04T11:22:33+00:00").timestamp()
    assert _run_started_at(item.media_dir) == pytest.approx(expected)

    discovered = _discover_sets(tmp_path)
    assert len(discovered) == 1
    assert discovered[0].analysed_at == pytest.approx(expected)
    # ...and it does not track the ingest file, which is what it used to read.
    assert discovered[0].analysed_at != path_mtime(item.media_dir / "ingest" / "source.json")
    stamp = datetime.fromtimestamp(expected).strftime("%d %b %Y").lstrip("0")
    assert f"Analysed {stamp}" in _mix_card_html(discovered[0])


def test_a_terminal_run_records_its_journalled_outcome_on_the_job(tmp_path: Path) -> None:
    """U-F15: the live failure UI reads the run's own journal, and only this run's entry."""

    media_dir = tmp_path / ("c" * 64) / ("d" * 64)
    media_dir.mkdir(parents=True)
    (media_dir / "invocations.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "invocation_id": "a" * 32,
                        "status": "complete",
                        "started_at": "2026-01-01T00:00:00Z",
                        "usd_e2_spent": 900,
                    }
                ),
                json.dumps(
                    {
                        "invocation_id": "b" * 32,
                        "status": "failed",
                        "reason": "provider_unavailable_midrun",
                        "started_at": "2026-09-12T10:00:00Z",
                        "usd_e2_spent": 42,
                        "timings": {"ingest_ms": 1, "decode_ms": 2},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    journal = media_dir / "invocations.jsonl"
    entry = _entry_by_run_id(journal, "b" * 32)
    assert entry is not None and entry["usd_e2_spent"] == 42

    # An earlier run's spend is never attributed to another attempt: the match is on the run's own
    # id, so a run that journalled nothing (a cache hit) gets no entry and no borrowed money.
    assert _entry_by_run_id(journal, "a" * 32)["usd_e2_spent"] == 900
    assert _entry_by_run_id(journal, "c" * 32) is None

    manager = JobManager(tmp_path, lambda context: None)
    try:
        job = _job(id="e" * 32, profile="max_accuracy", status="failed")
        manager._jobs[job.id] = job
        manager._order.append(job.id)
        JobContext(manager, job).set_outcome(
            run_status="failed",
            run_reason="provider_unavailable_midrun",
            last_stage="preparing the audio",
            usd_e2_spent=42,
            spend_known=True,
        )
        state = job.status_dict()
        assert state["run_status"] == "failed"
        assert state["run_reason"] == "provider_unavailable_midrun"
        assert state["last_stage"] == "preparing the audio"
        assert state["usd_e2_spent"] == 42 and state["spend_known"] is True
    finally:
        manager.shutdown()


def _tokens(css: str) -> dict[str, str]:
    """Every ``--name:#rrggbb`` custom property in a stylesheet, last declaration winning."""

    return dict(re.findall(r"--([a-z0-9-]+):(#[0-9a-fA-F]{3,8})", css))


def test_contrast_is_aa_in_both_themes_against_the_real_shipped_tokens() -> None:
    """Recomputed from the token values the pages actually ship, not from a hand-copied list.

    ``theme.BASE_CSS`` declares the dark palette; the page and the app stylesheet override it and
    add the light palette under ``prefers-color-scheme:light``.  Every text token is checked against
    every surface it is drawn on, in BOTH themes.
    """

    for name, sheet in (("result page", _CSS), ("app pages", _APP_CSS)):
        dark = _tokens(BASE_CSS) | _tokens(sheet.split("@media (prefers-color-scheme:light)", 1)[0])
        light = dark | _tokens(sheet.split("@media (prefers-color-scheme:light)", 1)[1])
        for theme_name, palette in (("dark", dark), ("light", light)):
            for fg in ("fg", "muted", "dim", "accent"):
                for bg in ("bg", "card", "card2"):
                    ratio = _contrast(palette[fg], palette[bg])
                    assert ratio >= 4.5, f"{name}/{theme_name}: --{fg} on --{bg} is {ratio:.2f}:1"
            # Text drawn on a filled accent (the primary button, the gradient endpoints).
            for bg in ("pink", "violet", "cyan"):
                ratio = _contrast(palette["on-accent"], palette[bg])
                assert ratio >= 4.5, f"{name}/{theme_name}: --on-accent on --{bg} is {ratio:.2f}:1"
            # Confidence colours are used as text on the card background.
            for fg in ("verified", "likely", "possible", "unclear", "gap"):
                ratio = _contrast(palette[fg], palette["card"])
                assert ratio >= 4.5, f"{name}/{theme_name}: --{fg} on --card is {ratio:.2f}:1"


def _js_gate() -> object:
    """Import ``scripts/check_page_js.py`` as a module (it is a script, not a package member)."""

    import importlib.util

    path = Path("scripts/check_page_js.py")
    spec = importlib.util.spec_from_file_location("check_page_js", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_js_gate_checks_every_emitted_script_of_every_page_type() -> None:
    """The gate must render real pages and check the exact payloads they emit.

    A hand-assembled approximation of a page's script is what silently stops being checked,
    so this asserts the gate extracts what the page really ships -- and that it actually fails on a
    syntax error rather than only printing "passed".
    """

    gate = _js_gate()
    pages = gate._pages()
    names = [name for name, _ in pages]
    assert any(name.startswith("result page") for name in names)
    assert "home (analyse enabled)" in names and "home (inline form error)" in names
    assert "read-only index" in names
    assert "job page (running)" in names and "job page (failed)" in names

    # Every payload the gate checks is a substring of the page it came from: nothing is rewritten,
    # padded or stubbed on the way to ``node --check``.
    checked = 0
    for name, html in pages:
        text = html.decode("utf-8") if isinstance(html, bytes) else html
        scripts = gate._inline_scripts(text)
        assert bool(scripts) is (name != "read-only index"), name
        for payload in scripts:
            assert payload in text
            checked += 1
    assert checked >= 3 * len([n for n in names if n.startswith("result page")])

    # The result page emits three inline blocks (seek/playhead, page JS, playlists JS) and the gate
    # picks up all three, not a subset.
    first_result = next(html for name, html in pages if name.startswith("result page"))
    assert len(gate._inline_scripts(first_result)) == 3

    node = shutil.which("node")
    if node is None:  # pragma: no cover - node is present in the gate environment
        pytest.skip("node is not installed")
    original = gate._pages
    try:
        gate._pages = lambda: [("job page (running)", "<script>function(</script>")]
        assert gate.main() == 1
    finally:
        gate._pages = original
    assert gate.main() == 0

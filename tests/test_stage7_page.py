"""Stage 7 — static page generator: seek correctness, HTML validity, CUE, embed policy."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from id_detector.contracts import (
    EpisodesFile,
    IdentitiesRecord,
    SourceRecord,
    derive_source_key,
)
from id_detector.present.exports import render_cue
from id_detector.present.page import (
    DEFAULT_LEAD_IN_MS,
    generate_page,
    plan_embed,
    plan_embed_from_url,
    playhead_x,
    render_page,
    seek_argument,
    seek_target_ms,
)
from scripts.audit_fixtures import _HANDLE, _ID_FIELD

GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "golden"
CANDIDATE_ID = "46b3308f39ae9f94358ee570f343e48993b360a3"
DURATION_MS = 3_600_000


def _episode(
    *,
    idx: int,
    best_start_ms: int,
    best_end_ms: int,
    role: str = "dominant",
    start_pi: dict | None = None,
    end_pi: dict | None = None,
    start_censored: int | None = None,
    flags: list[str] | None = None,
) -> dict:
    return {
        "schema_version": "1.0.0",
        "generated_by": "id-detector/0.1.0",
        "id": f"{idx:040d}",
        "candidate_id": CANDIDATE_ID,
        "alternatives": [],
        "claim": "performed",
        "start_no_later_than_ms": best_start_ms,
        "end_no_earlier_than_ms": best_end_ms,
        "evidence_support_ms": [[best_start_ms, min(best_start_ms + 12_000, best_end_ms)]],
        "start_no_earlier_than_ms": start_censored,
        "end_no_later_than_ms": None,
        "start_pi": start_pi,
        "end_pi": end_pi,
        "best_start_ms": best_start_ms,
        "best_end_ms": best_end_ms,
        "role_segments": [{"from_ms": best_start_ms, "to_ms": best_end_ms, "role": role}],
        "occurrence_index": idx,
        "overlaps": [],
        "alignment_segments": [],
        "alignment_events": [],
        "has_global_alignment": True,
        "scores": {"work": 7800, "version": 5000, "boundary": 6000},
        "score_kind": "heuristic",
        "tiers": {"work": "likely", "version": "unclear", "boundary": "possible"},
        "badge": "likely",
        "version_status": "unverified",
        "evidence": ["a" * 40],
        "rejected_evidence": [],
        "flags": flags or [],
        "rescan_state": "not_requested",
        "suppressed": None,
    }


def _episodes_file() -> EpisodesFile:
    episodes = [
        _episode(idx=1, best_start_ms=30_000, best_end_ms=150_000),
        # best_start below the lead-in exercises the clamp at 0.
        _episode(idx=2, best_start_ms=2_000, best_end_ms=90_000, role="incoming"),
        _episode(
            idx=3,
            best_start_ms=600_000,
            best_end_ms=720_000,
            start_pi={
                "lo": 590_000,
                "hi": 605_000,
                "coverage_target": 9000,
                "method": "test",
                "calibrated": True,
            },
            end_pi={
                "lo": 715_000,
                "hi": 730_000,
                "coverage_target": 9000,
                "method": "test",
                "calibrated": True,
            },
            flags=["hint_supported"],
        ),
        _episode(idx=4, best_start_ms=1_200_000, best_end_ms=1_260_000, start_censored=1_195_000),
    ]
    document = {
        "schema_version": "1.0.0",
        "generated_by": "id-detector/0.1.0",
        "generation": 0,
        "episodes": episodes,
        "gaps": [
            {
                "schema_version": "1.0.0",
                "generated_by": "id-detector/0.1.0",
                "id": "b" * 40,
                "start_ms": 150_000,
                "end_ms": 600_000,
                "bounded_by": [f"{1:040d}"],
                "evidence": {
                    "n_windows": 40,
                    "n_no_match": 38,
                    "n_error": 1,
                    "n_unclear_candidates": 0,
                    "n_hint_events": 0,
                    "n_novelty_events": 1,
                },
                "reason": "no_evidence",
                "truncated": False,
                "best_unclear_candidate": None,
            }
        ],
        "durations": {
            "schema_version": "1.0.0",
            "generated_by": "id-detector/0.1.0",
            "evidence_supported_ms": 48_000,
            "predicted_episode_ms": 0,
            "unresolved_boundary_ms": 120_000,
            "unclear_ms": 0,
            "no_evidence_ms": 450_000,
            "unscanned_ms": 0,
        },
        "certification": {
            "profile": "free",
            "per": [
                {
                    "dimension": "work",
                    "tier": "possible",
                    "status": "provisional",
                    "n_test_predictions": 0,
                    "lower_bound_e4": 0,
                    "test_version": "not-run",
                }
            ],
        },
    }
    return EpisodesFile.model_validate(document)


def _identities() -> IdentitiesRecord:
    return IdentitiesRecord.model_validate_json((GOLDEN / "identities.json").read_text("utf-8"))


def _source(platform: str = "soundcloud") -> SourceRecord:
    urls = {
        "soundcloud": "https://soundcloud.com/example/mix",
        "youtube": "https://www.youtube.com/watch?v=abcdEFGHijk",
        "mixcloud": "https://www.mixcloud.com/example/mix/",
    }
    url = urls.get(platform, "https://example.invalid/mix")
    base = json.loads((GOLDEN / "source.json").read_text("utf-8"))
    base.update(
        {
            "platform": platform,
            "canonical_url": url,
            "input_url": url,
            "source_key": derive_source_key(url),
            "platform_id": "abcdEFGHijk" if platform == "youtube" else "12345",
            "title": "Fixture Live Set",
        }
    )
    return SourceRecord.model_validate(base)


class _Validator(HTMLParser):
    """Assert the page is parseable and that tags nest without crossing."""

    _VOID = {"meta", "br", "img", "input", "hr", "link"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag not in self._VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._VOID:
            return
        if tag not in self.stack:
            self.errors.append(f"stray </{tag}>")
            return
        while self.stack and self.stack[-1] != tag:
            self.stack.pop()
        if self.stack:
            self.stack.pop()


def _config(page: str) -> dict:
    match = re.search(r"const CONFIG = (\{.*?\});", page)
    assert match, "page must embed a CONFIG object"
    return json.loads(match.group(1))


def _row_best_starts(page: str) -> list[int]:
    return [int(value) for value in re.findall(r'data-best-start-ms="(\d+)"', page)]


# -------------------------------------------------------------------------------------------------
def test_seek_python_matches_documented_formula() -> None:
    assert seek_target_ms(30_000, 5_000) == 25_000
    assert seek_target_ms(2_000, 5_000) == 0  # clamp
    assert seek_argument("soundcloud", 30_000, 5_000) == 25_000  # milliseconds
    assert seek_argument("youtube", 30_000, 5_000) == 25  # whole seconds
    assert seek_argument("mixcloud", 30_500, 5_000) == 25  # floors


@pytest.mark.parametrize("platform", ["soundcloud", "youtube", "mixcloud"])
def test_page_seek_lands_within_one_second_of_target(platform: str) -> None:
    """UI arithmetic only: the realised seek is within 1 s of ``best_start_ms - lead_in`` (clamped).

    This is separate from any measured boundary error — it proves the page's click-to-seek maths,
    not the accuracy of ``best_start_ms`` itself.
    """

    episodes = _episodes_file()
    page = render_page(
        source=_source(platform),
        episodes=episodes,
        identities=_identities(),
        duration_ms=DURATION_MS,
        collapse=False,
    )
    config = _config(page)
    lead_in = config["leadInMs"]
    assert lead_in == DEFAULT_LEAD_IN_MS
    embedded_platform = config["platform"]

    # Every episode's best_start_ms reaches the DOM as a data attribute.
    rendered = sorted(_row_best_starts(page))
    assert rendered == sorted(episode.best_start_ms for episode in episodes.episodes)

    for best_start in rendered:
        arg = seek_argument(embedded_platform, best_start, lead_in)
        realised_ms = arg if embedded_platform == "soundcloud" else arg * 1000
        expected = seek_target_ms(best_start, lead_in)
        assert abs(realised_ms - expected) <= 1000
    # The page carries the byte-identical JS formula, not a divergent copy.
    assert "Math.max(0, bestStartMs - leadInMs)" in page


def test_playhead_x_matches_documented_formula() -> None:
    """The shared position→pixel mapping clamps at both ends, lands the midpoint, and collapses when
    there is nothing to place the head against (matching the page's ``playheadX``)."""

    assert playhead_x(0, 100_000, 800) == 0.0  # left edge (== 0)
    assert playhead_x(100_000, 100_000, 800) == 800.0  # right edge (== duration)
    assert playhead_x(200_000, 100_000, 800) == 800.0  # clamp past the end
    assert playhead_x(-5_000, 100_000, 800) == 0.0  # clamp before the start
    assert playhead_x(50_000, 100_000, 800) == 400.0  # midpoint
    assert playhead_x(50_000, 0, 800) == 0.0  # no duration → collapse
    assert playhead_x(50_000, 100_000, 0) == 0.0  # no width → collapse


@pytest.mark.parametrize("platform", ["soundcloud", "youtube", "mixcloud"])
def test_playhead_markup_and_hooks_present(platform: str) -> None:
    """The live playhead element, its shared arithmetic, the per-platform position hooks, the
    current-track partition and timeline click-to-seek all reach the generated page."""

    episodes = _episodes_file()
    page = render_page(
        source=_source(platform),
        episodes=episodes,
        identities=_identities(),
        duration_ms=DURATION_MS,
        collapse=False,
    )
    # Playhead element (starts hidden) and its shared arithmetic + time label.
    assert '<div class="playhead" id="playhead" hidden' in page
    assert 'id="playhead-time"' in page
    assert "function playheadX" in page
    assert "function updatePlayhead" in page
    # Current-track partition: a span per episode, the highlight logic and its CSS.
    assert "const EPISODE_SPANS = [" in page
    for episode in episodes.episodes:
        assert f'"id": "{episode.id}"' in page
    assert "function highlightCurrent" in page
    assert "classList.add('current')" in page
    assert "tr.track.current" in page  # CSS row highlight
    assert ".tl-lane.current" in page  # CSS lane highlight
    # Per-platform position wiring is emitted for all three players in the one shared script.
    assert "SC.Widget.Events.PLAY_PROGRESS" in page  # SoundCloud
    assert "currentPosition" in page
    assert "getCurrentTime" in page and "setInterval" in page  # YouTube polling
    assert "mcWidget.events.progress" in page  # Mixcloud progress event
    # Timeline click-to-seek reuses the shared seek arithmetic (zero lead-in).
    assert "function seekToPositionMs" in page
    assert "getBoundingClientRect" in page


def test_page_is_parseable_and_lists_every_episode_id() -> None:
    episodes = _episodes_file()
    page = render_page(
        source=_source("soundcloud"),
        episodes=episodes,
        identities=_identities(),
        duration_ms=DURATION_MS,
        collapse=False,
    )
    validator = _Validator()
    validator.feed(page)
    assert validator.errors == []
    for episode in episodes.episodes:
        assert f'data-episode-id="{episode.id}"' in page
    for gap in episodes.gaps:
        assert f'data-gap-id="{gap.id}"' in page


def test_plan_embed_from_url_detects_platform_without_a_source() -> None:
    """The progress page plans an embed from the URL alone (no SourceRecord yet)."""

    sc = plan_embed_from_url("https://soundcloud.com/artist/live-mix")
    assert sc.kind == "soundcloud" and sc.identifier == "https://soundcloud.com/artist/live-mix"
    assert plan_embed_from_url("https://www.youtube.com/watch?v=abcdEFGHijk").identifier == (
        "abcdEFGHijk"
    )
    assert plan_embed_from_url("https://youtu.be/abcdEFGHijk").kind == "youtube"
    assert plan_embed_from_url("https://youtu.be/abcdEFGHijk").identifier == "abcdEFGHijk"
    assert plan_embed_from_url("https://www.mixcloud.com/artist/some-mix/").kind == "mixcloud"
    assert plan_embed_from_url("https://www.mixcloud.com/artist/some-mix/").identifier == (
        "/artist/some-mix/"
    )
    # A local file or unknown host is not embeddable — a plain link, no player.
    assert plan_embed_from_url(r"C:\mixes\set.wav").kind == "link"
    assert plan_embed_from_url("https://example.invalid/mix").kind == "link"


def test_result_page_has_library_and_new_mix_nav() -> None:
    """The result page can navigate back to the library and start a new mix (multi-mix flow)."""

    page = render_page(
        source=_source("soundcloud"),
        episodes=_episodes_file(),
        identities=_identities(),
        duration_ms=DURATION_MS,
    )
    assert '<nav class="topbar">' in page
    assert 'href="/"' in page  # ← Your mixes (back to the library home)
    assert 'href="/new"' in page  # + New mix
    validator = _Validator()
    validator.feed(page)
    assert validator.errors == []


def test_page_contains_no_usernames_or_comment_text() -> None:
    """Reuse the fixture-audit handle/identifier patterns (style block excluded — CSS ``@media``
    at-rules are not usernames)."""

    page = render_page(
        source=_source("soundcloud"),
        episodes=_episodes_file(),
        identities=_identities(),
        duration_ms=DURATION_MS,
    )
    without_style = re.sub(r"<style>.*?</style>", "", page, flags=re.DOTALL)
    assert _HANDLE.search(without_style) is None
    assert _ID_FIELD.search(page) is None


def test_prediction_interval_and_unresolved_zone_render() -> None:
    page = render_page(
        source=_source("soundcloud"),
        episodes=_episodes_file(),
        identities=_identities(),
        duration_ms=DURATION_MS,
    )
    assert "tl-pi" in page  # a calibrated PI is shaded
    assert "tl-unresolved" in page  # a censored/unknown side is hatched
    assert "tl-gap" in page  # the ID gap is marked
    assert "hint" in page  # the hint-supported row is flagged


def test_embed_plans_per_platform() -> None:
    assert plan_embed(_source("soundcloud")).kind == "soundcloud"
    assert plan_embed(_source("youtube")).kind == "youtube"
    assert plan_embed(_source("mixcloud")).kind == "mixcloud"


def test_embedding_disabled_falls_back_to_plain_link() -> None:
    source = _source("soundcloud")
    source = source.model_copy(
        update={"config_snapshot": {**source.config_snapshot, "embeddable_by": "me"}}
    )
    plan = plan_embed(source)
    assert plan.kind == "link"
    page = render_page(
        source=source, episodes=_episodes_file(), identities=_identities(), duration_ms=DURATION_MS
    )
    assert "Player embed unavailable" in page
    assert "w.soundcloud.com/player/api.js" not in page
    assert 'href="https://soundcloud.com/example/mix"' in page


def test_cue_export_flattens_with_role_precedence(tmp_path: Path) -> None:
    from id_detector.present.exports import flatten_tracklist

    episodes = _episodes_file()
    entries = flatten_tracklist(episodes, _identities(), collapse=False)
    cue = render_cue(entries, title="Fixture Live Set")
    assert cue.startswith('TITLE "Fixture Live Set"')
    assert 'FILE "audio" WAVE' in cue
    # One INDEX per flattened entry (tracks + ID gaps), monotonic and in MM:SS:FF.
    indices = re.findall(r"INDEX 01 (\d{2,}:\d{2}:\d{2})", cue)
    assert len(indices) == len(entries)
    # 30 s → 00:30:00 (75 frames/s); the incoming episode at 2 s starts at 00:02:00.
    assert "00:30:00" in cue
    assert "ID" in cue  # the gap flattens to an ID track


def test_generate_page_writes_index_and_sidecar(tmp_path: Path) -> None:
    media_dir = tmp_path / "work" / "sk" / "mk"
    fuse = media_dir / "fuse"
    fuse.mkdir(parents=True)
    episodes = _episodes_file()
    identities = _identities()
    episodes_path = fuse / "episodes.json"
    identities_path = fuse / "identities.gen0.json"
    episodes_path.write_bytes(episodes.model_dump_json().encode("utf-8"))
    identities_path.write_bytes(identities.model_dump_json().encode("utf-8"))
    index = generate_page(
        media_dir=media_dir,
        source=_source("youtube"),
        episodes=episodes,
        identities=identities,
        duration_ms=DURATION_MS,
        episodes_path=episodes_path,
        identities_path=identities_path,
    )
    assert index.is_file()
    assert index.name == "index.html"
    assert (media_dir / "present" / "index.done.json").is_file()
    assert "iframe_api" in index.read_text("utf-8")  # youtube embed


def test_page_carries_its_version_stamp() -> None:
    """Every written page is stamped so an older page can be recognised and regenerated on open."""

    from id_detector.present.page import PAGE_VERSION

    page = render_page(
        source=_source("soundcloud"),
        episodes=_episodes_file(),
        identities=_identities(),
        duration_ms=DURATION_MS,
    )
    assert f'<meta name="id-detector-page" content="{PAGE_VERSION}">' in page


def test_short_low_confidence_matches_drop_from_exports_and_hide_on_the_page() -> None:
    """On-air duration separates real tracks from false positives: with ``min_track_ms`` a brief
    low-confidence match is dropped from the exports and hidden (but kept, behind a toggle) on the
    page — unless it is likely/verified or hint-supported.  ``0`` (the default) changes nothing."""

    from id_detector.present.exports import flatten_tracklist, short_track

    document = _episodes_file().model_dump(mode="json")
    short_unclear = _episode(idx=5, best_start_ms=2_000_000, best_end_ms=2_012_000)
    short_unclear["badge"] = "unclear"
    short_likely = _episode(idx=6, best_start_ms=2_500_000, best_end_ms=2_512_000)  # likely
    short_hinted = _episode(
        idx=7, best_start_ms=3_000_000, best_end_ms=3_012_000, flags=["hint_supported"]
    )
    short_hinted["badge"] = "unclear"
    document["episodes"] += [short_unclear, short_likely, short_hinted]
    episodes = EpisodesFile.model_validate(document)
    identities = _identities()

    everything = flatten_tracklist(episodes, identities, collapse=False)
    kept = flatten_tracklist(episodes, identities, collapse=False, min_track_ms=30_000)
    dropped = {e.get("episode_id") for e in everything} - {e.get("episode_id") for e in kept}
    assert dropped == {short_unclear["id"]}
    assert [e for e in kept if e["kind"] == "id"] == [e for e in everything if e["kind"] == "id"]
    unclear_entry = next(e for e in everything if e.get("episode_id") == short_unclear["id"])
    assert unclear_entry["on_air_ms"] == 12_000 and short_track(unclear_entry, 30_000)
    assert not short_track(unclear_entry, 0)

    page = render_page(
        source=_source("soundcloud"),
        episodes=episodes,
        identities=identities,
        duration_ms=DURATION_MS,
        collapse=False,
        min_track_ms=30_000,
    )
    assert page.count('<tr class="track short"') == 1
    assert f'<tr class="track short" data-episode-id="{short_unclear["id"]}"' in page
    assert 'id="short-note"' in page and 'id="show-short"' in page
    assert f'data-episode-id="{short_unclear["id"]}" data-badge="unclear" data-short="1"' in page
    assert f'"id": "{short_unclear["id"]}"' not in page  # not in the playhead partition
    assert f'"id": "{short_likely["id"]}"' in page
    validator = _Validator()
    validator.feed(page)
    assert validator.errors == []

    plain = render_page(
        source=_source("soundcloud"),
        episodes=episodes,
        identities=identities,
        duration_ms=DURATION_MS,
        collapse=False,
    )
    assert 'class="track short"' not in plain and 'id="short-note"' not in plain


def test_hidden_reason_covers_suppressed_and_short_rows() -> None:
    """``hidden_reason`` is the single predicate the exports drop by and the page tucks away by:
    a fusion-side ``suppressed`` token wins, then the on-air floor; gaps are never hidden."""

    from id_detector.present.exports import _support_ms, hidden_reason

    base = {"kind": "track", "badge": "unclear", "hint_supported": False, "on_air_ms": 12_000}
    assert hidden_reason(base, 30_000) == "short"
    assert hidden_reason(base, 0) is None
    assert hidden_reason({**base, "suppressed": "buried"}, 0) == "buried"
    assert hidden_reason({**base, "badge": "likely"}, 30_000) is None
    assert hidden_reason({**base, "hint_supported": True}, 30_000) is None
    assert hidden_reason({"kind": "id", "suppressed": "buried"}, 30_000) is None
    # Summed support merges overlapping windows and ignores the unproven time between detections.
    assert _support_ms([[0, 12_000], [6_000, 18_000]]) == 18_000
    assert _support_ms([[0, 12_000], [60_000, 72_000]]) == 24_000
    assert _support_ms([]) == 0


def test_suppressed_episodes_drop_from_exports_and_tuck_on_the_page() -> None:
    """A fusion-side ``suppressed`` reason is honoured like a short match: gone from the exports,
    kept on the page behind the toggle with a friendly reason tag and counted in the note."""

    from id_detector.present.exports import flatten_tracklist

    document = _episodes_file().model_dump(mode="json")
    buried = _episode(idx=8, best_start_ms=2_000_000, best_end_ms=2_090_000)
    buried["badge"] = "possible"
    buried["suppressed"] = "buried"
    document["episodes"].append(buried)
    episodes = EpisodesFile.model_validate(document)
    identities = _identities()

    listed = flatten_tracklist(episodes, identities, collapse=False)
    assert buried["id"] not in {e.get("episode_id") for e in listed}  # even with the floor off
    everything = flatten_tracklist(episodes, identities, collapse=False, include_hidden=True)
    assert buried["id"] in {e.get("episode_id") for e in everything}

    page = render_page(
        source=_source("soundcloud"),
        episodes=episodes,
        identities=identities,
        duration_ms=DURATION_MS,
        collapse=False,
    )
    assert f'<tr class="track short" data-episode-id="{buried["id"]}"' in page
    assert "buried under a surer track" in page
    assert "<b>1</b> suppressed match hidden" in page
    assert f'"id": "{buried["id"]}"' not in page  # out of the playhead partition


def test_hint_only_crowd_ids_render_distinctly_and_are_never_proved_evidence() -> None:
    """A fusion ``hint_only`` episode (a comment answer no engine matched) is listed — the hint
    keeps it past the on-air floor — but the page marks it as a crowd ID everywhere: row, chip,
    lane, stat tile; the JSON entry carries ``hint_only``."""

    from id_detector.present.exports import flatten_tracklist

    document = _episodes_file().model_dump(mode="json")
    crowd = _episode(
        idx=9, best_start_ms=40_000, best_end_ms=50_000, flags=["hint_only", "hint_supported"]
    )
    crowd["badge"] = "possible"
    document["episodes"].append(crowd)
    episodes = EpisodesFile.model_validate(document)
    identities = _identities()

    entries = flatten_tracklist(episodes, identities, collapse=False, min_track_ms=30_000)
    entry = next(e for e in entries if e.get("episode_id") == crowd["id"])  # not hidden (hint)
    assert entry["hint_only"] is True and entry["on_air_ms"] == 10_000

    page = render_page(
        source=_source("soundcloud"),
        episodes=episodes,
        identities=identities,
        duration_ms=DURATION_MS,
        collapse=False,
        min_track_ms=30_000,
    )
    assert f'<tr class="track crowd" data-episode-id="{crowd["id"]}" data-crowd="1"' in page
    assert ">from comments</span>" in page
    assert f'data-episode-id="{crowd["id"]}" data-badge="possible" data-crowd="1"' in page
    assert "found · 1 from comments</small>" in page
    assert 'class="lg-crowd"' in page
    validator = _Validator()
    validator.feed(page)
    assert validator.errors == []


def test_hint_only_marker_reaches_the_markdown_export(tmp_path: Path) -> None:
    from id_detector.present.exports import export_tracklist

    document = _episodes_file().model_dump(mode="json")
    crowd = _episode(
        idx=9, best_start_ms=40_000, best_end_ms=50_000, flags=["hint_only", "hint_supported"]
    )
    document["episodes"].append(crowd)
    episodes = EpisodesFile.model_validate(document)
    media_dir = tmp_path / "media"
    (media_dir / "fuse").mkdir(parents=True)
    episodes_path = media_dir / "fuse" / "episodes.json"
    identities_path = media_dir / "fuse" / "identities.gen0.json"
    episodes_path.write_bytes(episodes.model_dump_json().encode("utf-8"))
    identities_path.write_bytes(_identities().model_dump_json().encode("utf-8"))
    result = export_tracklist(
        media_dir=media_dir,
        media_key="a" * 64,
        duration_ms=DURATION_MS,
        episodes=episodes,
        identities=_identities(),
        episodes_path=episodes_path,
        identities_path=identities_path,
        collapse=False,
    )
    assert "FROM COMMENTS" in result.markdown_path.read_text("utf-8")
    assert any(e.get("hint_only") for e in result.entries)

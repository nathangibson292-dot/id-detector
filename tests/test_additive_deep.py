"""Free-first, additive Deep: paid evidence only ADDS to the free result, and never without a price.

Every run is offline: fake providers, synthetic tones, temporary work roots.  Covered here:

* the additive invariant (:func:`id_detector.additive.additive_merge`) on hand-built records --
  paid evidence may confirm or add a row, never remove, demote or re-label one the free evidence
  lists on its own;
* the Deep pipeline end to end: the stored Free sweep is reused (no Shazam request), the paid
  engine checks only the free result's gaps, and a pre-bundle Free result is reused too;
* the paid sweep without a credential answers stored clips and sends nothing;
* the browser's price gate: a Max-accuracy job stops after its free pass with the exact offer,
  Approve starts the job that may spend it (and only up to it), Skip keeps the Free result;
* the comparison runner's staging retry, its selection refusal and the offline network guard.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
from pathlib import Path

import pytest

from id_detector import cli
from id_detector.additive import (
    Fused,
    additive_merge,
    find_free_evidence,
    listed_episode_ids,
    paid_windows,
    plan_paid_step,
    policy_targets,
)
from id_detector.contracts import EpisodesFile, IdentitiesRecord, WindowRecord
from id_detector.io import native_path, read_text
from id_detector.paid_clip import run_paid_clip_recognition
from id_detector.present.bundles import read_bundle_manifest, result_dir, shown_result_dir
from id_detector.providers.base import AppConfig
from id_detector.recipes import DEEP_RECIPE, FREE_RECIPE, Recipe
from id_detector.windows import WindowsResult
from scripts import compare_deep
from scripts.make_audio_fixtures import generate
from scripts.offline_guard import NetworkAttempt, no_network
from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff
from tests.test_cost_preview import cached  # noqa: F401  (the Free-scanned fixture mix)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden"

# --------------------------------------------------------------------------------------------------
# The additive invariant, on hand-built fusion records
# --------------------------------------------------------------------------------------------------
_BASE_EPISODE = json.loads((GOLDEN / "episode.json").read_text(encoding="utf-8"))
_BASE_IDENTITIES = json.loads((GOLDEN / "identities.json").read_text(encoding="utf-8"))
_BASE_EPISODES = json.loads((GOLDEN / "episodes.json").read_text(encoding="utf-8"))
_NODE = _BASE_IDENTITIES["nodes"][0]
_WORK = _BASE_IDENTITIES["works"][0]
_CANDIDATE = _BASE_IDENTITIES["candidates"][0]
FREE_OBS = "f" * 40
PAID_OBS = "a" * 40
HINT = "b" * 40


def _node(node_id: str, label: str) -> dict:
    return {**_NODE, "id": node_id, "ns": node_id.split(":", 1)[0], "label": label}


def _identities(*candidates: tuple[str, list[str], str, list[str]], labels: dict) -> dict:
    """``(candidate id, member nodes, work id, work member nodes)`` per candidate."""

    nodes = {node for _cid, members, _wid, work in candidates for node in (*members, *work)}
    return {
        **_BASE_IDENTITIES,
        "assertions": [],
        "nodes": [_node(node, labels[node]) for node in sorted(nodes)],
        "works": [
            {**_WORK, "work_id": work_id, "member_nodes": sorted(work)}
            for _cid, _members, work_id, work in {c[2]: c for c in candidates}.values()
        ],
        "candidates": [
            {**_CANDIDATE, "canonical_id": cid, "member_nodes": sorted(members), "work_id": wid}
            for cid, members, wid, _work in candidates
        ],
    }


def _episode(
    episode_id: str,
    candidate: str,
    span: tuple[int, int],
    evidence: list[str],
    *,
    tier: str = "possible",
    flags: list[str] | None = None,
    suppressed: str | None = None,
) -> dict:
    start, end = span
    return {
        **_BASE_EPISODE,
        "id": episode_id,
        "candidate_id": candidate,
        "evidence": evidence,
        "evidence_support_ms": [[start, start + 12_000], [end - 12_000, end]],
        "start_no_later_than_ms": start + 12_000,
        "end_no_earlier_than_ms": end - 12_000,
        "best_start_ms": start,
        "best_end_ms": end,
        "alignment_segments": [],
        "role_segments": [{"from_ms": start, "to_ms": end, "role": "dominant"}],
        "badge": tier,
        "tiers": {"boundary": tier, "version": tier, "work": tier},
        "flags": flags or [],
        "suppressed": suppressed,
    }


def _fused(episodes: list[dict], identities: dict) -> Fused:
    return Fused(
        EpisodesFile.model_validate({**_BASE_EPISODES, "episodes": episodes}),
        IdentitiesRecord.model_validate(identities),
    )


LABELS = {
    "shazam:juice-mixed": "Mall Grab & CRTB - Juice (Mixed)",
    "audd:juice": "Mall Grab - Juice",
    "audd:new-track": "Unreleased Artist - Gap Track",
    "text:mall grab & c.r.t.b|juice": "Mall Grab & C.R.T.B - Juice",
}


def _label(fused: Fused, episode_id: str) -> str:
    from id_detector.benchmark.corpus import _label_for_candidate

    episode = next(item for item in fused.episodes.episodes if item.id == episode_id)
    return " - ".join(_label_for_candidate(fused.identities, episode.candidate_id))


def _juice_case() -> tuple[Fused, Fused]:
    """The measured Mall Grab loss, reduced: the free row is named by a tracklist line; the paid
    answer for the same work elsewhere pulls that line to ITS work, so in a plain combined fusion
    the free row -- same episode, same evidence -- loses the name the tracklist gave it."""

    free = _fused(
        [
            _episode(
                "e" * 40, "c" * 40, (60_000, 180_000), [FREE_OBS, HINT], flags=["hint_supported"]
            )
        ],
        _identities(
            (
                "c" * 40,
                ["shazam:juice-mixed"],
                "1" * 40,
                ["shazam:juice-mixed", "text:mall grab & c.r.t.b|juice"],
            ),
            labels=LABELS,
        ),
    )
    combined = _fused(
        [
            _episode("e" * 40, "c" * 40, (60_000, 180_000), [FREE_OBS], flags=[]),
            _episode("d" * 40, "9" * 40, (400_000, 460_000), [PAID_OBS, HINT], tier="likely"),
        ],
        _identities(
            ("c" * 40, ["shazam:juice-mixed"], "2" * 40, ["shazam:juice-mixed"]),
            (
                "9" * 40,
                ["audd:juice"],
                "1" * 40,
                ["audd:juice", "text:mall grab & c.r.t.b|juice"],
            ),
            labels=LABELS,
        ),
    )
    return free, combined


def test_paid_evidence_never_strips_a_free_rows_tracklist_name_or_support() -> None:
    free, combined = _juice_case()
    assert _label(free, "e" * 40) == "Mall Grab & C.R.T.B - Juice"
    assert _label(combined, "e" * 40) == "Mall Grab & CRTB - Juice (Mixed)"  # the measured loss
    merged, report = additive_merge(
        free=free,
        combined=combined,
        paid_observation_ids=[PAID_OBS],
        hint_ids=[HINT],
        min_track_ms=0,
    )
    rows = {item.id: item for item in merged.episodes.episodes}
    # The free row is exactly the free fusion's, with its name and its hint support ...
    assert rows["e" * 40] == free.episodes.episodes[0]
    assert _label(merged, "e" * 40) == "Mall Grab & C.R.T.B - Juice"
    # ... and the paid row, a later play of the same work in a gap, is ADDED.
    assert "d" * 40 in rows and report.added == 1
    assert listed_episode_ids(free, 0) <= listed_episode_ids(merged, 0)


def test_a_paid_row_only_confirms_a_free_row_it_keeps_everything_of() -> None:
    labels = {"shazam:a": "Artist - Track", "audd:a": "Artist - Track", "text:artist|track": "x"}
    free = _fused(
        [_episode("e" * 40, "c" * 40, (0, 120_000), [FREE_OBS, HINT], flags=["hint_supported"])],
        _identities(
            ("c" * 40, ["shazam:a"], "1" * 40, ["shazam:a", "text:artist|track"]), labels=labels
        ),
    )

    def combined(tier: str, flags: list[str], evidence: list[str]) -> Fused:
        return _fused(
            [_episode("d" * 40, "9" * 40, (0, 130_000), evidence, tier=tier, flags=flags)],
            _identities(
                (
                    "9" * 40,
                    ["shazam:a", "audd:a"],
                    "1" * 40,
                    ["audd:a", "shazam:a", "text:artist|track"],
                ),
                labels=labels,
            ),
        )

    keeps = combined(
        "likely", ["hint_supported", "engine_corroborated"], [FREE_OBS, PAID_OBS, HINT]
    )
    merged, report = additive_merge(
        free=free, combined=keeps, paid_observation_ids=[PAID_OBS], hint_ids=[HINT], min_track_ms=0
    )
    assert [item.id for item in merged.episodes.episodes] == ["d" * 40] and report.confirmed == 1

    # A paid-touched row that would drop the tracklist support, or the tier, stands in for
    # nothing: the free row is kept exactly as free fusion made it and the paid row is dropped.
    for weaker in (
        combined("likely", ["engine_corroborated"], [FREE_OBS, PAID_OBS]),
        combined("unclear", ["hint_supported"], [FREE_OBS, PAID_OBS, HINT]),
    ):
        merged, report = additive_merge(
            free=free,
            combined=weaker,
            paid_observation_ids=[PAID_OBS],
            hint_ids=[HINT],
            min_track_ms=0,
        )
        assert merged.episodes.episodes == free.episodes.episodes
        assert report.kept_free == 1 and report.confirmed == report.added == 0


def test_a_free_row_hidden_on_free_evidence_alone_may_be_lifted_but_is_never_protected() -> None:
    labels = {"shazam:a": "Artist - Track", "audd:a": "Artist - Track"}
    identities = _identities(
        ("c" * 40, ["shazam:a", "audd:a"], "1" * 40, ["shazam:a", "audd:a"]), labels=labels
    )
    free = _fused(
        [_episode("e" * 40, "c" * 40, (0, 120_000), [FREE_OBS], suppressed="scatter")], identities
    )
    combined = _fused(
        [_episode("d" * 40, "c" * 40, (0, 120_000), [FREE_OBS, PAID_OBS], tier="likely")],
        identities,
    )
    merged, report = additive_merge(
        free=free, combined=combined, paid_observation_ids=[PAID_OBS], min_track_ms=0
    )
    assert "e" * 40 not in listed_episode_ids(free, 0)
    assert listed_episode_ids(merged, 0) == {"d" * 40} and report.confirmed == 1


def test_no_paid_evidence_leaves_the_free_result_untouched() -> None:
    free, combined = _juice_case()
    merged, report = additive_merge(
        free=free, combined=combined, paid_observation_ids=[], hint_ids=[HINT], min_track_ms=0
    )
    assert merged is free and report.added == report.confirmed == report.kept_free == 0


# --------------------------------------------------------------------------------------------------
# The Deep pipeline, end to end (offline)
# --------------------------------------------------------------------------------------------------
#: A three-minute sweep: the free engine hears track A over windows 0-9 (12-81 s) and nothing after;
#: the paid engine answers every clip it is sent, naming a track D in the second half.
SWEEP_S = 180
_A = {str(index): "A" for index in range(10)}
GAP_SCRIPT = {
    "shazam": {"default": "no_match", "windows": dict.fromkeys(_A, "match"), "labels": _A},
    "audd": {
        "default": "match",
        "labels": {str(index): "D" for index in range(10, 20)},
    },
}


@pytest.fixture(scope="module")
def sweep(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("additive-audio") / "sweep-180s.wav"
    generate(path, SWEEP_S)
    return path


def _analyse(
    work: Path,
    audio: Path,
    recipe: Recipe,
    *,
    script: dict = GAP_SCRIPT,
    confirm=None,
    tracklist: Path | None = None,
    paid: bool = True,
) -> tuple[int, FakeAudD, FakeShazamHTTP, dict[str, object], Path]:
    audd, shazam = FakeAudD(script), FakeShazamHTTP(script)
    code = asyncio.run(
        cli._analyse(
            str(audio),
            work_root=work,
            print_raw=False,
            refresh=False,
            max_requests=100,
            tracklist=tracklist,
            no_hints=tracklist is None,
            app_config=AppConfig(
                transforms_policy="off",
                recognise_concurrency=1,
                shazam_requests_per_minute=1_000_000,
                audd_requests_per_minute=1_000_000,
            ),
            max_generations=0,
            novelty=False,
            recipe=recipe,
            paid_scan_adapters={"audd": audd} if paid else None,
            shazam_http_client=shazam,
            paid_sleep=no_backoff,
            cli_paid_confirm=confirm,
        )
    )
    (journal,) = work.rglob("invocations.jsonl")
    entries = [json.loads(line) for line in read_text(journal).splitlines() if line]
    return code, audd, shazam, entries[-1], journal.parent


def _windows(media: Path) -> tuple[WindowRecord, ...]:
    path = media / "windows" / "windows.gen0.jsonl"
    return tuple(WindowRecord.model_validate_json(line) for line in read_text(path).splitlines())


def _listed_titles(media: Path) -> set[str]:
    tracklist = json.loads(read_text(shown_result_dir(media) / "tracklist.json"))
    return {entry["title"] for entry in tracklist["entries"] if entry["kind"] == "track"}


def test_deep_reuses_the_stored_free_sweep_and_pays_only_for_its_gaps(
    tmp_path: Path, sweep: Path
) -> None:
    code, _audd, free_shazam, _entry, media = _analyse(tmp_path / "work", sweep, FREE_RECIPE)
    assert code == 0 and free_shazam.requests == 20
    free_titles = _listed_titles(media)
    assert free_titles == {"Tone 440"}

    code, audd, shazam, entry, media = _analyse(tmp_path / "work", sweep, DEEP_RECIPE)
    assert code == 0 and entry["status"] == "complete" and entry["achieved"] == "deep"
    # The stored Free result answered the free pass outright: not one Shazam request.
    assert shazam.requests == 0 and entry["counts"]["free_reused"] == 20
    # AudD checked ONLY the gaps the free result leaves: the stretch before A's proved start and
    # everything after it -- never the windows A already covers.
    sent = {int(item["window"]) for item in audd.attempts}
    assert sent == {0, 1, *range(9, 20)}
    assert not sent & set(range(2, 9))
    assert entry["counts"]["paid_targets"] == audd.calls == len(sent)
    # The exact clips are the ones the plan names (what `idea cost` and the confirmation show).
    free_episodes = EpisodesFile.model_validate_json(
        read_text(media / "fuse" / "runs" / _free_run(media) / "episodes.json")
    ).episodes
    targets = policy_targets("gaps", free_episodes, SWEEP_S * 1000)
    assert len(paid_windows(_windows(media), targets)) == audd.calls
    # Additive: A is still listed, and D, which only the paid engine heard, is added.
    assert free_titles <= _listed_titles(media)
    assert "Tone 880" in _listed_titles(media)


def _free_run(media: Path) -> str:
    for directory in (media / "present" / "bundles").iterdir():
        manifest = read_bundle_manifest(directory)
        if manifest and manifest.get("achieved") == "free":
            return str(manifest["run_id"])
    raise AssertionError("no Free bundle")


def test_a_pre_bundle_free_result_is_reused_as_proven_free_evidence(
    tmp_path: Path, sweep: Path
) -> None:
    work = tmp_path / "work"
    code, _audd, _shazam, _entry, media = _analyse(work, sweep, FREE_RECIPE)
    assert code == 0
    # Turn it into the owner's pre-bundle layout: the flat result and its fuse tree only.
    bundle = result_dir(media)
    for name in ("index.html", "tracklist.json", "source.json"):
        shutil.copy2(native_path(bundle / name), native_path(media / "present" / name))
    lines = [json.loads(line) for line in read_text(media / "invocations.jsonl").splitlines()]
    completed = next(line for line in reversed(lines) if line["status"] == "complete")
    completed.update(analysis_key=None, compatibility=None, bundle_id=None, fuse_run=None)
    Path(native_path(media / "invocations.jsonl")).write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    shutil.rmtree(native_path(media / "present" / "bundles"))
    shutil.rmtree(native_path(media / "fuse" / "runs"))
    Path(native_path(media / "present" / "current")).unlink()
    assert result_dir(media).name == "present"
    found = find_free_evidence(media, object(), windows=_windows(media), local=True)
    assert found is not None and len(found.observations) == 20

    code, audd, shazam, entry, _media = _analyse(work, sweep, DEEP_RECIPE)
    assert code == 0 and shazam.requests == 0 and entry["counts"]["free_reused"] == 20
    assert audd.calls == 13


def test_a_rewritten_hint_file_does_not_stop_the_free_sweep_being_reused(
    tmp_path: Path, sweep: Path
) -> None:
    """Every run re-reads its hints and rewrites the mix's hint file; the reuse proof covers the
    recognition files only, so a changed tracklist never forces the free sweep to run again."""

    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    first.write_text("00:00 Fixture Artist A - Tone 440\n", encoding="utf-8")
    second.write_text("00:00 Fixture Artist A - Tone 440\n01:40 Somebody - Else\n", "utf-8")
    work = tmp_path / "work"
    code, _audd, shazam, _entry, _media = _analyse(work, sweep, FREE_RECIPE, tracklist=first)
    assert code == 0 and shazam.requests == 20
    code, _audd, shazam, entry, _media = _analyse(work, sweep, DEEP_RECIPE, tracklist=second)
    assert code == 0 and shazam.requests == 0 and entry["counts"]["free_reused"] == 20


def test_the_price_gate_is_asked_after_the_free_pass_with_the_exact_plan(
    tmp_path: Path, sweep: Path
) -> None:
    seen = []

    def decline(plan) -> bool:
        seen.append(plan)
        return False

    code, audd, shazam, entry, media = _analyse(
        tmp_path / "work", sweep, DEEP_RECIPE, confirm=decline
    )
    assert code == 130 and (entry["status"], entry["reason"]) == ("cancelled", "paid_not_confirmed")
    (plan,) = seen
    assert (plan.clips, plan.cached, plan.to_send, plan.spans) == (13, 0, 13, 2)
    assert plan.estimate_e6 == 13 * 5_000
    # The free pass ran (that is how the count is known); nothing was reserved or sent to AudD,
    # and the finished free pass is kept as the owner's Free result.
    assert shazam.requests == 20 and audd.calls == 0
    assert entry["usd_e6_reserved"] == entry["usd_e6_spent"] == 0
    kept = read_bundle_manifest(media / "present" / "bundles" / entry["bundle_id"])
    assert kept["achieved"] == "free" and kept["compatibility"]["recipe_name"] == "free"
    assert _listed_titles(media) == {"Tone 440"}


def test_a_stored_paid_answer_is_counted_free_and_never_bought_again(tmp_path: Path) -> None:
    """The paid cache is read whatever the free engine's refresh states say (a stored no-match
    included): the plan counts those clips as free, and the sweep sends none of them."""

    script = {"shazam": {"default": "no_match"}, "audd": {"default": "no_match"}}
    work = tmp_path / "work"
    audio = ROOT / "tests" / "fixtures" / "audio" / "tone-60s.wav"
    code, audd, _shazam, _entry, media = _analyse(work, audio, DEEP_RECIPE, script=script)
    assert code == 0 and audd.calls == 7
    windows = _windows(media)
    plan, _targets = plan_paid_step(
        policy="gaps",
        free_episodes=(),
        windows=windows,
        duration_ms=60_000,
        density=1,
        unit_usd_e6=5_000,
        media_dir=media,
    )
    assert (plan.clips, plan.cached, plan.to_send, plan.estimate_e6) == (7, 7, 0, 0)

    async def sweep_again(adapters):
        return await run_paid_clip_recognition(
            media_key=media.name,
            media_dir=media,
            windows=WindowsResult(records=windows, record_path=media / "w.jsonl", cached=True),
            targets=((0, 60_000),),
            run_id="again",
            app_config=AppConfig(),
            enabled_engines=("audd",),
            cli_confirmation=False,
            refresh_states=frozenset(),
            adapters=adapters,
        )

    again = FakeAudD(script)
    result = asyncio.run(sweep_again({"audd": again}))
    assert again.calls == 0 and result.cache_hits == 7 and result.requests == 0


def test_without_a_credential_stored_answers_are_read_and_nothing_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = {"shazam": {"default": "no_match"}, "audd": {"default": "match"}}
    audio = ROOT / "tests" / "fixtures" / "audio" / "tone-60s.wav"
    code, audd, _shazam, _entry, media = _analyse(
        tmp_path / "work", audio, DEEP_RECIPE, script=script
    )
    assert code == 0 and audd.calls == 7
    windows = _windows(media)
    raw = media / "recognise" / "invocations" / "live-audd-clip-v1" / "raw"
    answers = sorted(Path(native_path(raw)).glob("*.json"))
    answers[0].unlink()  # one clip never got a stored answer
    monkeypatch.delenv("AUDD_API_TOKEN", raising=False)

    result = asyncio.run(
        run_paid_clip_recognition(
            media_key=media.name,
            media_dir=media,
            windows=WindowsResult(records=windows, record_path=media / "w.jsonl", cached=True),
            targets=((0, 60_000),),
            run_id="no-credential",
            app_config=AppConfig(),
            enabled_engines=("audd",),
            cli_confirmation=False,
            refresh_states=frozenset(),
        )
    )
    assert result.requests == 0 and result.attempts == 0
    assert result.cache_hits == result.resolved == 6
    assert result.skipped and "1 clip(s) without a stored answer not sent" in result.skipped[0][1]


# --------------------------------------------------------------------------------------------------
# The browser's price gate (local mode)
# --------------------------------------------------------------------------------------------------
def _local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from idea_web.jobs.local import LocalJobs
    from tests.idea_web.test_followup_round2 import _env

    return _env(tmp_path, monkeypatch), LocalJobs(tmp_path)


def _run_one(tmp_path: Path, config: Path, audd: FakeAudD, shazam: FakeShazamHTTP) -> None:
    from idea_web.jobs.local import LocalWorker, local_database
    from tests.idea_web.local_runner_fakes import fake_pipeline_runner

    LocalWorker(
        local_database(tmp_path),
        tmp_path,
        fake_pipeline_runner(tmp_path, config, audd=audd, shazam=shazam),
        flush_seconds=0.05,
    ).run_once()


def _rows(jobs, table: str) -> int:
    with jobs.database.read() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_a_max_accuracy_job_stops_after_its_free_pass_with_the_exact_offer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.idea_web.test_worker import AUDIO, SCRIPT

    config, jobs = _local(tmp_path, monkeypatch)
    job_id = jobs.submit(str(AUDIO), "max_accuracy")
    audd, shazam = FakeAudD(SCRIPT), FakeShazamHTTP(SCRIPT)
    _run_one(tmp_path, config, audd, shazam)
    job = jobs.get(job_id)
    assert job.status == "succeeded", job.error
    # The free pass ran; the paid step waits for the owner: no reservation, no dispatch, no call.
    assert shazam.requests == 7 and audd.calls == 0
    assert _rows(jobs, "run_reservations") == 0
    assert job.paid_offer == {
        "gaps": 1,
        "clips": 7,
        "cached": 0,
        "to_send": 7,
        "usd_e6": 35_000,
        "usd_e2": 4,
    }
    status = job.status_dict()
    assert status["paid_offer"]["usd_e2"] == 4 and status["offer_decision"] is None
    assert status["result_url"]  # the Free result is open beside the offer
    with jobs.database.read() as connection:
        (entry,) = [
            json.loads(row[0]) for row in connection.execute("SELECT entry FROM run_settlements")
        ]
    assert (entry["status"], entry["reason"]) == ("cancelled", "paid_not_confirmed")
    assert entry["usd_e6_reserved"] == entry["usd_e6_spent"] == 0


def test_approve_runs_the_paid_check_within_the_price_and_skip_spends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.idea_web.test_worker import AUDIO, SCRIPT

    config, jobs = _local(tmp_path, monkeypatch)
    first = jobs.submit(str(AUDIO), "max_accuracy")
    _run_one(tmp_path, config, FakeAudD(SCRIPT), FakeShazamHTTP(SCRIPT))
    follow_up = jobs.approve_offer(first)
    assert follow_up is not None and follow_up != first
    assert jobs.approve_offer(first) is None and jobs.skip_offer(first) is False  # answered once
    decided = jobs.get(first)
    assert decided.offer_decision == "approved" and decided.follow_up_job == follow_up
    assert jobs.get(follow_up).approved_usd_e6 == 35_000
    assert jobs.get(follow_up).approved_clips == 7

    audd, shazam = FakeAudD(SCRIPT), FakeShazamHTTP(SCRIPT)
    _run_one(tmp_path, config, audd, shazam)
    job = jobs.get(follow_up)
    assert job.status == "succeeded", job.error
    # The Free pass just finished is reused; the approved clips are sent, under a reservation.
    assert shazam.requests == 0 and audd.calls == 7
    assert job.paid_offer is None
    assert _rows(jobs, "run_reservations") == 1

    # Skip, on another mix folder: the Free result stays, nothing is reserved or sent, and it
    # cannot then be approved.
    other = tmp_path / "other"
    other.mkdir()
    config, jobs = _local(other, monkeypatch)
    second = jobs.submit(str(AUDIO), "max_accuracy")
    audd = FakeAudD(SCRIPT)
    _run_one(other, config, audd, FakeShazamHTTP(SCRIPT))
    assert jobs.get(second).paid_offer is not None and audd.calls == 0
    assert jobs.skip_offer(second) is True
    assert jobs.get(second).offer_decision == "skipped"
    assert jobs.approve_offer(second) is None
    assert _rows(jobs, "run_reservations") == 0


def test_an_approval_below_the_new_price_stops_again_with_a_fresh_offer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.idea_web.test_worker import AUDIO, SCRIPT

    config, jobs = _local(tmp_path, monkeypatch)
    job_id = jobs.submit(str(AUDIO), "max_accuracy", approved_usd_e6=35_000, approved_clips=3)
    audd = FakeAudD(SCRIPT)
    _run_one(tmp_path, config, audd, FakeShazamHTTP(SCRIPT))
    job = jobs.get(job_id)
    assert audd.calls == 0 and _rows(jobs, "run_reservations") == 0
    assert job.paid_offer is not None and job.paid_offer["to_send"] == 7


def test_the_offer_routes_need_the_token_and_redirect_to_the_paid_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from idea_web.application import create_app
    from tests.idea_web.test_parity import request, token
    from tests.idea_web.test_worker import AUDIO, SCRIPT

    config, jobs = _local(tmp_path, monkeypatch)
    job_id = jobs.submit(str(AUDIO), "max_accuracy")
    _run_one(tmp_path, config, FakeAudD(SCRIPT), FakeShazamHTTP(SCRIPT))
    app = create_app(tmp_path, jobs=jobs)
    csrf = token(app)
    assert "Deep is waiting for your approval" in request(app, "GET", "/").text
    refused = request(app, "POST", f"/jobs/{job_id}/approve", data={})
    assert refused.status_code == 400 and jobs.get(job_id).offer_decision is None
    approved = request(app, "POST", f"/jobs/{job_id}/approve", data={"csrf_token": csrf})
    assert approved.status_code == 303
    follow_up = jobs.get(job_id).follow_up_job
    assert approved.headers["location"] == f"/jobs/{follow_up}"
    again = request(app, "POST", f"/jobs/{job_id}/skip", data={"csrf_token": csrf})
    assert again.status_code == 409  # already answered
    from idea_web.application import STATIC_JS

    script = STATIC_JS.decode("utf-8")
    assert "Deep would check" in script and "offerForm('approve'" in script
    assert "offerForm('skip'" in script and "Closing this page spends nothing." in script


# --------------------------------------------------------------------------------------------------
# The comparison runner
# --------------------------------------------------------------------------------------------------
def _media_tree(root: Path) -> Path:
    media = root / "source-key" / "media-key"
    (media / "decode").mkdir(parents=True)
    (media / "decode" / "audio.pcm").write_bytes(b"\0" * 1024)
    return media


def test_staging_retries_a_locked_rename_and_imports_stored_paid_answers(tmp_path: Path) -> None:
    source = _media_tree(tmp_path / "work")
    earlier = tmp_path / "earlier" / "source-key" / "media-key" / compare_deep.PAID_CACHE
    earlier.mkdir(parents=True)
    (earlier / "answer.json").write_text('{"status":"success","result":null}', encoding="utf-8")
    destination = tmp_path / "scan" / "source-key" / "media-key"
    failures = [PermissionError(5, "Access is denied"), PermissionError(5, "Access is denied")]
    slept: list[float] = []

    def rename(old: str, new: str) -> None:
        if failures:
            raise failures.pop(0)
        os.rename(old, new)

    imported = compare_deep.stage_media(
        source,
        destination,
        paid_answers=earlier.parents[3],
        backoff=(0.1, 0.2, 0.3),
        sleep=slept.append,
        rename=rename,
    )
    assert slept == [0.1, 0.2] and imported == 1
    assert (destination / "decode" / "audio.pcm").read_bytes() == b"\0" * 1024
    assert (destination / compare_deep.PAID_CACHE / "answer.json").is_file()
    assert (earlier / "answer.json").is_file()  # the source is only read
    assert not [path for path in destination.parent.iterdir() if path.name.startswith(".staging")]


def test_staging_that_stays_locked_leaves_no_half_copy_and_says_so(tmp_path: Path) -> None:
    source = _media_tree(tmp_path / "work")
    destination = tmp_path / "scan" / "source-key" / "media-key"

    def locked(_old: str, _new: str) -> None:
        raise PermissionError(5, "Access is denied")

    with pytest.raises(compare_deep.StagingFailed, match="nothing was queued, reserved or spent"):
        compare_deep.stage_media(
            source, destination, backoff=(0.0, 0.0), sleep=lambda _s: None, rename=locked
        )
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []  # the temporary copy was removed too


def test_a_different_selection_is_refused_naming_the_mixes_the_scan_root_started_with(
    tmp_path: Path,
) -> None:
    from scripts.score_corpus import RunEntry, RunList

    truth = json.loads(read_text(ROOT / "tests/fixtures/corpus-mini/mini-a/ground_truth.json"))
    truth["source"]["media_key"] = "c" * 64
    path = tmp_path / "truth" / "ground_truth.json"
    path.parent.mkdir()
    path.write_text(json.dumps(truth), encoding="utf-8")
    runs = RunList(recipe="free", runs=[RunEntry(mix_id="mall-grab", truth=path, episodes=path)])
    identifier = compare_deep._identifier("c" * 64, DEEP_RECIPE.recipe_id)
    message = compare_deep._selection_refusal(
        {identifier: "complete", "compare-other": "complete"}, runs, tmp_path, DEEP_RECIPE
    )
    assert "different mix selection" in message
    assert "it was started with mall-grab" in message and "--mix mall-grab" in message
    assert "1 of its job(s) belong to a different recipe" in message
    assert "no mix can be charged twice" in message


def test_the_offline_guard_refuses_the_network_and_keeps_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A placeholder, never a real credential: the guard must blank whatever the process holds.
    monkeypatch.setenv("AUDD_API_TOKEN", "placeholder-not-a-token")
    saved_token = os.environ.get("AUDD_API_TOKEN")
    with no_network() as blocked:
        before = len(blocked)
        assert os.environ["AUDD_API_TOKEN"] == "" and os.environ["IDEA_ENGINE_SHAZAM"] == "off"
        with pytest.raises(NetworkAttempt):
            socket.create_connection(("api.audd.io", 443), timeout=1)
        with pytest.raises(NetworkAttempt):
            socket.getaddrinfo("api.audd.io", 443)
        # asyncio's own self-pipe (a loopback socket pair on Windows) still works.
        asyncio.run(asyncio.sleep(0))
        assert len(blocked) - before == 2
    # Outside the block the process's own settings are back.
    assert os.environ.get("AUDD_API_TOKEN") == saved_token


def test_extending_an_experiment_is_refused_naming_the_mixes_to_add(
    cached,  # noqa: F811  (the fixture imported above)
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal protects the money (no mix charged twice) and now says exactly what to do."""

    from id_detector.cost import cached_mix
    from tests.test_cost_preview import experiment

    args = experiment(cached, tmp_path)
    args.spend = True
    started = compare_deep._identifier(cached_mix(*cached).source.media_key, DEEP_RECIPE.recipe_id)
    monkeypatch.setattr(
        compare_deep,
        "_existing",
        lambda _database: ({started: "complete", "compare-" + "0" * 32: "complete"}, {}),
    )
    with pytest.raises(ValueError) as refused:
        compare_deep.execute(args)
    message = str(refused.value)
    assert "it was started with tone" in message and "--mix tone" in message
    assert "1 of its job(s) belong to a different recipe" in message
    assert not args.scan_root.exists()


# --------------------------------------------------------------------------------------------------
# Fix pass 1: one offer authorises at most one paid run, ever (real concurrency, real SQLite)
# --------------------------------------------------------------------------------------------------
def _finished_offer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A Max-accuracy job stopped after its free pass with an open offer."""

    from tests.idea_web.test_worker import AUDIO, SCRIPT

    config, jobs = _local(tmp_path, monkeypatch)
    offer = jobs.submit(str(AUDIO), "max_accuracy")
    _run_one(tmp_path, config, FakeAudD(SCRIPT), FakeShazamHTTP(SCRIPT))
    assert jobs.get(offer).paid_offer is not None
    return config, jobs, offer


def _clone_offers(jobs, offer: str, count: int) -> list[str]:
    """``count`` more finished jobs holding the same open offer (cheap independent trials)."""

    import uuid

    clones = []
    with jobs.database.write() as connection:
        row = dict(connection.execute("SELECT * FROM jobs WHERE id=?", (offer,)).fetchone())
        for _ in range(count):
            clone = {**row, "id": uuid.uuid4().hex, "run_id": uuid.uuid4().hex}
            columns = ", ".join(clone)
            marks = ", ".join("?" for _ in clone)
            connection.execute(
                f"INSERT INTO jobs({columns}) VALUES ({marks})", tuple(clone.values())
            )
            clones.append(clone["id"])
    return clones


def _other_jobs(jobs, offers: list[str]) -> int:
    """Jobs that are not one of the offers: every one of them is a paid follow-up."""

    with jobs.database.read() as connection:
        rows = connection.execute("SELECT id FROM jobs").fetchall()
    return sum(row[0] not in offers for row in rows)


def _race(calls) -> list[object]:
    import threading

    barrier = threading.Barrier(len(calls))
    results: list[object] = [None] * len(calls)
    errors: list[BaseException] = []

    def run(index: int, call) -> None:
        barrier.wait()
        try:
            results[index] = call()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=item) for item in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not errors, errors
    return results


def test_concurrent_approvals_of_one_offer_create_one_paid_job_and_one_paid_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from idea_web.jobs.local import LocalJobs
    from tests.idea_web.test_worker import SCRIPT

    config, jobs, offer = _finished_offer(tmp_path, monkeypatch)
    offers = [offer, *_clone_offers(jobs, offer, 5)]
    # Every thread holds its OWN connection to the same database file: SQLite is the only fence.
    contenders = [LocalJobs(tmp_path) for _ in range(6)]
    for each in offers:
        results = _race(
            [lambda c=contender, o=each: c.approve_offer(o) for contender in contenders]
        )
        winners = [result for result in results if result is not None]
        assert len(winners) == 1, results
    assert _other_jobs(jobs, offers) == len(offers)  # one follow-up per offer, never two
    # ... and run the first offer's follow-up: exactly one paid run for that offer, nothing more.
    for other in offers[1:]:
        jobs.cancel(jobs.get(other).follow_up_job)
    audd = FakeAudD(SCRIPT)
    for _ in range(len(offers)):
        _run_one(tmp_path, config, audd, FakeShazamHTTP(SCRIPT))
    assert audd.calls == 7 and _rows(jobs, "run_reservations") == 1


def test_a_concurrent_approve_and_skip_never_leave_a_paid_job_behind_a_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from idea_web.jobs.local import LocalJobs

    _config, jobs, offer = _finished_offer(tmp_path, monkeypatch)
    offers = [offer, *_clone_offers(jobs, offer, 11)]
    approver, skipper = LocalJobs(tmp_path), LocalJobs(tmp_path)
    approved = 0
    for each in offers:
        follow_up, skipped = _race(
            [lambda o=each: approver.approve_offer(o), lambda o=each: skipper.skip_offer(o)]
        )
        decision = jobs.get(each).offer_decision
        assert (follow_up is not None) != bool(skipped)  # exactly one answer won
        assert decision == ("approved" if follow_up else "skipped")
        approved += follow_up is not None
    # A skipped offer has no paid job at all -- not even a cancelled one.
    assert _other_jobs(jobs, offers) == approved


def test_a_crash_after_the_paid_job_is_written_leaves_nothing_and_a_retry_authorises_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from tests.idea_web.test_worker import SCRIPT

    config, jobs, offer = _finished_offer(tmp_path, monkeypatch)
    # The process dies right after the follow-up job is written, before the offer is consumed.
    with jobs.database.write() as connection:
        connection.execute(
            "CREATE TRIGGER crash_after_enqueue AFTER UPDATE ON jobs "
            f"WHEN NEW.id = '{offer}' BEGIN SELECT RAISE(ABORT, 'simulated crash'); END"
        )
    with pytest.raises(sqlite3.DatabaseError, match="simulated crash"):
        jobs.approve_offer(offer)
    assert _other_jobs(jobs, [offer]) == 0 and jobs.get(offer).offer_decision is None
    with jobs.database.write() as connection:
        connection.execute("DROP TRIGGER crash_after_enqueue")
    follow_up = jobs.approve_offer(offer)
    assert follow_up is not None
    assert jobs.approve_offer(offer) is None  # consumed: a later retry authorises nothing
    assert _other_jobs(jobs, [offer]) == 1
    audd = FakeAudD(SCRIPT)
    _run_one(tmp_path, config, audd, FakeShazamHTTP(SCRIPT))
    _run_one(tmp_path, config, audd, FakeShazamHTTP(SCRIPT))
    assert audd.calls == 7 and _rows(jobs, "run_reservations") == 1


# --------------------------------------------------------------------------------------------------
# Fix pass 1: a paid row never renames a row the free result lists
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("paid_label", "what_differs"),
    [
        ("Alpha - Paid", "artist and title"),
        ("Alpha - Free", "artist"),
        ("Zulu - A Paid Edit", "title"),
    ],
)
def test_a_paid_engine_naming_the_same_recording_differently_never_renames_a_free_row(
    paid_label: str, what_differs: str
) -> None:
    """Shazam and AudD agree it is the same recording (one candidate holds both provider nodes)
    but disagree about its metadata.  The confirming paid row may not publish ITS label over the
    listed free row: the free row is kept exactly, under the free label."""

    labels = {"shazam:zulu": "Zulu - Free", "audd:alpha": paid_label}
    free = _fused(
        [_episode("e" * 40, "c" * 40, (0, 120_000), [FREE_OBS])],
        _identities(("c" * 40, ["shazam:zulu"], "1" * 40, ["shazam:zulu"]), labels=labels),
    )
    combined = _fused(
        [
            _episode(
                "d" * 40,
                "9" * 40,
                (0, 130_000),
                [FREE_OBS, PAID_OBS],
                tier="likely",
                flags=["engine_corroborated"],
            )
        ],
        _identities(
            ("9" * 40, ["audd:alpha", "shazam:zulu"], "1" * 40, ["audd:alpha", "shazam:zulu"]),
            labels=labels,
        ),
    )
    assert _label(combined, "d" * 40) != "Zulu - Free", what_differs  # the paid name would win
    merged, report = additive_merge(
        free=free, combined=combined, paid_observation_ids=[PAID_OBS], min_track_ms=0
    )
    assert merged.episodes.episodes == free.episodes.episodes
    assert _label(merged, "e" * 40) == "Zulu - Free"
    from id_detector.present.exports import flatten_tracklist

    shown = [
        (row["artist"], row["title"])
        for row in flatten_tracklist(merged.episodes, merged.identities, collapse=False)
        if row["kind"] == "track"
    ]
    assert shown == [("Zulu", "Free")]
    assert report.confirmed == 0 and report.kept_free == 1


def test_without_a_credential_a_partly_cached_deep_run_reserves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = {"shazam": {"default": "no_match"}, "audd": {"default": "match"}}
    audio = ROOT / "tests" / "fixtures" / "audio" / "tone-60s.wav"
    work = tmp_path / "work"
    code, audd, _shazam, _entry, media = _analyse(work, audio, DEEP_RECIPE, script=script)
    assert code == 0 and audd.calls == 7
    raw = media / "recognise" / "invocations" / "live-audd-clip-v1" / "raw"
    sorted(Path(native_path(raw)).glob("*.json"))[0].unlink()  # one clip has no stored answer
    monkeypatch.delenv("AUDD_API_TOKEN", raising=False)
    # A fresh Deep analysis (not the stored Deep result served again), with no credential.
    monkeypatch.setattr("id_detector.pipeline.find_result", lambda *args, **kwargs: None)
    code, _audd, _shazam, entry, _media = _analyse(
        work, audio, DEEP_RECIPE, script=script, paid=False
    )
    assert code == 0
    counts = entry["counts"]
    assert counts["paid_cache_hits"] == 6 and counts["paid_requests"] == 0
    # Nothing could be sent, so nothing was reserved -- not even internally.
    assert entry["usd_e6_reserved"] == entry["usd_e6_spent"] == 0


def test_each_offline_block_counts_only_its_own_refused_attempts() -> None:
    """Several measurements in one process: a later one never inherits an earlier one's count."""

    with no_network() as first, pytest.raises(NetworkAttempt):
        socket.getaddrinfo("api.audd.io", 443)
    assert len(first) == 1
    with no_network() as second:
        asyncio.run(asyncio.sleep(0))
    assert second == []

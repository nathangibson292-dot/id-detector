"""Plan 1b-iii (scorer half): ``scripts/score_corpus.py`` over ``tests/fixtures/corpus-mini/``.

The fixture is two hand-designed draft-truth mixes whose pooled numbers differ from the mean of
their per-mix numbers, so a macro average cannot pass by accident:

``mini-a`` (600 s; truth Alpha, Beta, Gamma in three equal slices)
  a1 Alpha   [10 s, 70 s]    likely   -> listed, correct
  a2 Beta    [210 s, 270 s]  likely   -> listed, correct
  a3 Delta   [300 s, 350 s]  possible -> listed (50 s clears the 30 s floor), wrong
  a4 Epsilon [420 s, 432 s]  possible -> hidden ``short`` (12 s), wrong
  a5 Gamma   [450 s, 462 s]  likely   -> listed (``likely`` keeps a short row), correct
  a6 Zeta    [500 s, 560 s]  likely, ``suppressed: buried`` -> hidden ``buried``, wrong
  a7 Eta     [560 s, 600 s]  unclear  -> listed (40 s), wrong, not a likely
  listed 5, correct 3 -> 6000; likely 3/3; works recalled 3/3

``mini-b`` (300 s; truth Theta, Iota, Mu)
  b1 Theta   [5 s, 65 s]     likely   -> listed, correct
  b2 Kappa   [100 s, 140 s]  likely   -> listed, wrong
  b3 Lambda  [200 s, 212 s]  possible -> hidden ``short``, wrong
  listed 2, correct 1 -> 5000; likely 1/2; works recalled 1/3

Pooled: listed 4/7 = 5714 (the mean would be 5500), likely 4/5 = 8000 (mean 7500), work recall
4/6 = 6667 (from 3/3 + 1/3).

Both truths still carry ``idea truth seed``'s placeholder equal-slice timings, exactly like the
rekordbox-playlist drafts under ``data/corpus/release-1/``, so the default run matches by WORK
(scorer part ii, time-agnostic); ``--match time`` runs the committed time path.  The fixture is
built so that both modes give the same numbers (every time association in it is also a work
match): 8000 / 5714 / 6667 either way, with ``listed_precision_e4`` null in work mode.
``expected.json`` is the script's exact output for the default run and ``expected-time.json`` for
``--match time``.
"""

from __future__ import annotations

import json
import os
import shutil
from hashlib import sha1, sha256
from pathlib import Path

import pytest

from id_detector.benchmark.corpus import _prediction_set, prediction_set_from_fusion
from id_detector.benchmark.scorer import (
    PredictionDocument,
    pooled_metrics,
    score_set,
    work_key,
)
from id_detector.contracts import EpisodesFile, GroundTruthRecord, IdentitiesRecord, TruthWork
from id_detector.fuse.identity import _word_sets_corroborate
from id_detector.io import canonical_json_bytes
from id_detector.truth import seed_truth
from scripts.score_corpus import (
    L3_THRESHOLDS,
    ListedWork,
    RunEntry,
    WorkCounts,
    identities_path,
    is_range_claim,
    listed_episodes,
    load_run_list,
    main,
    match_works,
    median_offset,
    mix_line,
    parity_identities,
    placeholder_rows,
    proved_bounds,
    run_media_key,
    score_mix,
    summary,
    truth_timing,
    work_identity,
    work_only_line,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "corpus-mini"
RUN_LIST = FIXTURE / "run-list.json"
EXPECTED = json.loads((FIXTURE / "expected.json").read_text("utf-8"))
EXPECTED_TIME = json.loads((FIXTURE / "expected-time.json").read_text("utf-8"))
HEADLINE = ("likely_precision_e4", "listed_precision_e4", "work_precision_e4", "work_recall_e4")
L3_KEYS = ("likely_precision_e4", "listed_precision_e4", "work_recall_e4")
WORK_KEYS = ("likely_precision_e4", "work_precision_e4", "work_recall_e4")


def _episode_id(key: str) -> str:
    return sha1(f"episode|{key}".encode()).hexdigest()


def _episodes(mix_id: str) -> EpisodesFile:
    path = FIXTURE / mix_id / "fuse" / "episodes.json"
    return EpisodesFile.model_validate_json(path.read_text("utf-8"))


def _truth(mix_id: str, root: Path = FIXTURE) -> GroundTruthRecord:
    path = root / mix_id / "ground_truth.json"
    return GroundTruthRecord.model_validate_json(path.read_text("utf-8"))


def _entry(mix_id: str, run_list: Path = RUN_LIST) -> RunEntry:
    return next(run for run in load_run_list(run_list).runs if run.mix_id == mix_id)


def _ratio_e4(numerator: int, denominator: int) -> int:
    return (numerator * 10_000 + denominator // 2) // denominator


def _run(tmp_path: Path, run_list: Path = RUN_LIST, *extra: str) -> tuple[int, dict | None]:
    out = tmp_path / "out.json"
    code = main(["--run-list", str(run_list), "--out", str(out), *extra])
    return code, (json.loads(out.read_text("utf-8")) if out.is_file() else None)


def _run_time(tmp_path: Path, run_list: Path = RUN_LIST, *extra: str) -> tuple[int, dict | None]:
    return _run(tmp_path, run_list, "--match", "time", *extra)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def _copy_fixture(tmp_path: Path) -> Path:
    """A private copy of the fixture with absolute paths in its run list."""

    root = tmp_path / "corpus-mini"
    shutil.copytree(FIXTURE, root)
    run_list = json.loads((root / "run-list.json").read_text("utf-8"))
    for run in run_list["runs"]:
        run["truth"] = str(root / run["truth"])
        run["episodes"] = str(root / run["episodes"])
    _write_json(root / "run-list.json", run_list)
    return root


def _mark_verified(truth_path: Path) -> None:
    truth = json.loads(truth_path.read_text("utf-8"))
    for episode in truth["episodes"]:
        episode.update(
            draft=False,
            annotator_ref="first-pass",
            second_pass_ref="second-pass",
            disagreement_resolution="agreed",
            verified_against="audio",
        )
    _write_json(truth_path, truth)


def _freeze(root: Path, set_ids: list[str]) -> None:
    _write_json(
        root / "corpus-version.json",
        {
            "schema_version": "1.0.0",
            "generated_by": "test",
            "corpus_version": "corpus-mini",
            "frozen": True,
            "sets": [
                {
                    "set_id": set_id,
                    "path": f"{set_id}/ground_truth.json",
                    "sha256": sha256(
                        (root / set_id / "ground_truth.json").read_bytes()
                    ).hexdigest(),
                }
                for set_id in set_ids
            ],
        },
    )


def _order_only_truth(truth: dict, works: list[tuple[str, str]]) -> dict:
    """Re-seed a truth's rows exactly as ``idea truth seed`` does from an order-only tracklist:
    row *i* of *n* starts at the point ``i * duration // n`` and is flagged for annotation."""

    duration = truth["source"]["duration_ms"]
    template = truth["episodes"][0]
    occurrences: dict[tuple[str, str], int] = {}
    episodes = []
    for index, (artist, title) in enumerate(works):
        start = index * duration // len(works)
        end = (index + 1) * duration // len(works)
        key = (artist.casefold(), title.casefold())
        occurrence = occurrences.get(key, 0)
        occurrences[key] = occurrence + 1
        episodes.append(
            {
                **template,
                "work": {"artist": artist, "title": title},
                "start_ms_range": [start, start],
                "end_ms_range": [end, end],
                "audible_rule": "manual annotation required",
                "role_segments": [{"from_ms": start, "to_ms": end, "role": "uncertain"}],
                "occurrence_index": occurrence,
            }
        )
    return {**truth, "episodes": episodes}


def _timed_truth(truth: dict, spans: list[tuple[int, int, int, int]]) -> dict:
    """Give each truth row real start / end ranges, as a verifier who listened would."""

    episodes = []
    for episode, (start_lo, start_hi, end_lo, end_hi) in zip(truth["episodes"], spans, strict=True):
        episodes.append(
            {
                **episode,
                "start_ms_range": [start_lo, start_hi],
                "end_ms_range": [end_lo, end_hi],
                "audible_rule": "first beat audible",
                "role_segments": [{"from_ms": start_lo, "to_ms": end_hi, "role": "uncertain"}],
            }
        )
    return {**truth, "episodes": episodes}


def _make_mini_b_timed(root: Path) -> None:
    truth_path = root / "mini-b" / "ground_truth.json"
    truth = json.loads(truth_path.read_text("utf-8"))
    _write_json(
        truth_path,
        _timed_truth(
            truth,
            [
                (0, 10_000, 60_000, 70_000),
                (100_000, 105_000, 190_000, 200_000),
                (200_000, 210_000, 290_000, 300_000),
            ],
        ),
    )


# --- the gate ---------------------------------------------------------------------------------


def test_corpus_mini_reproduces_expected_exactly(tmp_path: Path) -> None:
    """The default run: placeholder-timed truth is matched by work."""

    code, document = _run(tmp_path)
    assert code == 0
    assert document == EXPECTED
    assert document["match_requested"] == "auto"
    assert document["match_mode"] == "work"
    for mix in document["mixes"]:
        assert mix["match_mode"] == "work"
        # No time-based report exists in work mode; the assignment audit and the contract-checked
        # prediction document do.
        assert mix["report"] is None
        assert (tmp_path / mix["work_match"]).is_file()
        assert (tmp_path / mix["work_match"]).with_name("predictions.json").is_file()


def test_time_mode_numbers_on_corpus_mini_are_unchanged(tmp_path: Path) -> None:
    """Regression for part ii: the committed time path did not move by a single count."""

    code, document = _run_time(tmp_path)
    assert code == 0
    assert document == EXPECTED_TIME
    assert document["match_requested"] == "time"
    assert document["match_mode"] == "time"
    assert [document[key] for key in L3_KEYS] == [8_000, 5_714, 6_667]
    assert document["counts"]["likely"] == {"correct": 4, "predicted": 5}
    assert document["counts"]["listed"] == {"correct": 4, "predicted": 7, "truth": 6}
    assert document["counts"]["work"] == {"correct": 4, "predicted": 7, "truth": 6}
    assert document["l3"] == {
        "thresholds": L3_THRESHOLDS["deep"],
        "thresholds_met": False,
        "independent": True,
        "certifiable": False,
        "certification": "certification is disabled until the certification follow-up lands",
    }
    by_mix = {mix["mix_id"]: mix for mix in document["mixes"]}
    assert [by_mix["mini-a"][key] for key in L3_KEYS] == [10_000, 6_000, 10_000]
    assert [by_mix["mini-b"][key] for key in L3_KEYS] == [5_000, 5_000, 3_333]
    # The per-mix artefacts the plan's `idea benchmark score` step would have written.
    for mix in document["mixes"]:
        assert (tmp_path / mix["report"]).is_file()
        predictions_path = (tmp_path / mix["report"]).with_name("predictions.json")
        assert predictions_path.is_file()
        # Part iii, asserted explicitly: the fixture has no near-duplicate labels, so identity
        # parity granted nothing and the scorer saw the run's own identity graph, node for node.
        predictions = json.loads(predictions_path.read_text("utf-8"))
        assert predictions["config_snapshot"]["run_config"]["parity_keys"] == 0
        fixture_graph = json.loads(
            (FIXTURE / mix["mix_id"] / "fuse" / "identities.gen0.json").read_text("utf-8")
        )
        assert predictions["sets"][0]["identities"] == fixture_graph
        # Placeholder-timed rows carry no offset to measure.
        assert (mix["median_offset_ms"], mix["offset_pairs"]) == (None, 0)


@pytest.mark.parametrize("document", [EXPECTED, EXPECTED_TIME], ids=["work", "time"])
def test_headline_numbers_are_pooled_counts_not_a_mean_of_ratios(document: dict) -> None:
    counts = document["counts"]
    assert counts["likely"] == {"correct": 4, "predicted": 5}
    if document["match_mode"] == "time":
        assert counts["listed"] == {"correct": 4, "predicted": 7, "truth": 6}
        assert counts["work"] == {"correct": 4, "predicted": 7, "truth": 6}
        assert document["listed_precision_e4"] == _ratio_e4(4, 7) == 5_714
    else:
        assert counts["listed"] is None
        assert counts["work"] == {"correct": 4, "predicted": 7, "truth": 6, "truth_matched": 4}
        assert document["listed_precision_e4"] is None
    assert document["likely_precision_e4"] == _ratio_e4(4, 5) == 8_000
    assert document["work_precision_e4"] == _ratio_e4(4, 7) == 5_714
    assert document["work_recall_e4"] == _ratio_e4(4, 6) == 6_667
    by_mix = {mix["mix_id"]: mix for mix in document["mixes"]}
    assert [by_mix["mini-a"][key] for key in WORK_KEYS] == [10_000, 6_000, 10_000]
    assert [by_mix["mini-b"][key] for key in WORK_KEYS] == [5_000, 5_000, 3_333]
    # A macro average of the per-mix ratios would have said 7500 / 5500 / 6667 (recall by luck).
    assert document["work_precision_e4"] != (6_000 + 5_000) // 2
    assert document["likely_precision_e4"] != (10_000 + 5_000) // 2
    # Every per-mix count sums to the pooled count, in the mode's counts and in `work_only`.
    for metric in ("likely", "work"):
        for field, total in counts[metric].items():
            assert sum(mix["counts"][metric][field] for mix in document["mixes"]) == total
    for group, values in document["work_only"]["counts"].items():
        for field, total in values.items():
            assert sum(mix["work_only"]["counts"][group][field] for mix in document["mixes"]) == (
                total
            )


def test_headline_fields_come_from_the_scorer_report_by_name(tmp_path: Path) -> None:
    """The time numbers are read from the real benchmark report under the plan's field names."""

    code, document = _run_time(tmp_path)
    assert code == 0
    for mix in document["mixes"]:
        report = json.loads((tmp_path / mix["report"]).read_text("utf-8"))
        overall = report["overall"]
        assert mix["likely_precision_e4"] == overall["empirical_tier_precision_e4"]["likely"]
        assert mix["listed_precision_e4"] == overall["selective_precision_e4"]
        assert mix["work_precision_e4"] == overall["identification_work"]["precision_e4"]
        assert mix["work_recall_e4"] == overall["identification_work"]["recall_e4"]
        assert [item["set_id"] for item in report["sets"]] == [mix["set_id"]]
        assert report["profile"] == "deep"
        assert report["unverified_seed_comparison"] is True
        # Only the listed episodes reached the scorer.
        predictions = json.loads(
            (tmp_path / mix["report"]).with_name("predictions.json").read_text()
        )
        assert len(predictions["sets"][0]["episodes"]) == mix["episodes_listed"]
        run_config = predictions["config_snapshot"]["run_config"]
        assert run_config["hidden_by_reason"] == mix["hidden_by_reason"]
        assert run_config["timing"] == "order-only"
        assert run_config["match_mode"] == "time"


def test_pooled_metrics_sums_states_before_taking_ratios(tmp_path: Path) -> None:
    run_list = load_run_list(RUN_LIST)
    scored = [
        score_mix(entry, recipe="deep", artefact_dir=tmp_path, match="time")
        for entry in run_list.runs
    ]
    states = [mix.score.state for mix in scored]
    assert [mix.metrics.selective_precision_e4 for mix in scored] == [6_000, 5_000]
    pooled = pooled_metrics(states)
    assert pooled.selective_precision_e4 == 5_714
    assert pooled.empirical_tier_precision_e4["likely"] == 8_000
    assert pooled.identification_work.recall_e4 == 6_667
    # One set pooled is that set's own metrics (physical_attempts aside, which the pool sets).
    single = pooled_metrics([states[0]])
    assert single == scored[0].metrics.model_copy(update={"physical_attempts": 0})


# --- the presentation floor -------------------------------------------------------------------


def test_presentation_floor_drops_short_and_suppressed_rows_only() -> None:
    fuse = FIXTURE / "mini-a" / "fuse"
    episodes = EpisodesFile.model_validate_json((fuse / "episodes.json").read_text("utf-8"))
    identities = IdentitiesRecord.model_validate_json(
        (fuse / "identities.gen0.json").read_text("utf-8")
    )
    listed, hidden = listed_episodes(episodes, identities, 30_000)
    assert hidden == {"buried": 1, "short": 1}
    kept = {episode.id for episode in listed.episodes}
    assert _episode_id("a4") not in kept, "12 s possible row is short"
    assert _episode_id("a6") not in kept, "a suppressed row is hidden whatever its badge"
    assert _episode_id("a5") in kept, "a 12 s likely row stays listed"
    assert _episode_id("a3") in kept and _episode_id("a7") in kept
    assert listed.gaps == episodes.gaps and listed.durations == episodes.durations
    # Floor off: only fusion's own suppression hides anything.
    unfloored, hidden = listed_episodes(episodes, identities, 0)
    assert hidden == {"buried": 1}
    assert len(unfloored.episodes) == 6


def test_min_track_ms_zero_lists_the_short_rows_and_moves_the_precisions(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    run_list = json.loads((root / "run-list.json").read_text("utf-8"))
    for run in run_list["runs"]:
        run["min_track_ms"] = 0
    _write_json(root / "run-list.json", run_list)
    code, document = _run_time(tmp_path / "time", root / "run-list.json")
    assert code == 0
    assert document["counts"]["hidden_by_reason"] == {"buried": 1}
    assert document["counts"]["episodes"] == {"total": 10, "listed": 9, "hidden": 1}
    assert document["counts"]["listed"] == {"correct": 4, "predicted": 9, "truth": 6}
    assert document["listed_precision_e4"] == _ratio_e4(4, 9) == 4_444
    # The short rows were `possible`, so the likely tier and the recall are untouched.
    assert document["likely_precision_e4"] == 8_000
    assert document["work_recall_e4"] == 6_667
    # Matched by work, the two extra rows are two more distinct wrong works.
    code, document = _run(tmp_path / "work", root / "run-list.json")
    assert code == 0
    assert document["counts"]["work"] == {
        "correct": 4,
        "predicted": 9,
        "truth": 6,
        "truth_matched": 4,
    }
    assert document["work_precision_e4"] == 4_444
    assert document["likely_precision_e4"] == 8_000
    assert document["work_recall_e4"] == 6_667


def test_min_track_ms_defaults_to_the_thirty_second_floor(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    run_list = json.loads((root / "run-list.json").read_text("utf-8"))
    for run in run_list["runs"]:
        del run["min_track_ms"]
    _write_json(root / "run-list.json", run_list)
    loaded = load_run_list(root / "run-list.json")
    assert [run.min_track_ms for run in loaded.runs] == [30_000, 30_000]
    code, document = _run(tmp_path, root / "run-list.json")
    assert code == 0
    assert {key: document[key] for key in HEADLINE} == {key: EXPECTED[key] for key in HEADLINE}


# --- truth status -----------------------------------------------------------------------------


def test_draft_truth_is_scored_but_labelled_draft() -> None:
    for document in (EXPECTED, EXPECTED_TIME):
        assert document["truth_status"] == "draft"
        assert [mix["truth_status"] for mix in document["mixes"]] == ["draft", "draft"]
        assert document["l3"]["thresholds"] == L3_THRESHOLDS["deep"]
        assert document["l3"]["certifiable"] is False
    # The three thresholds only: L3's corpus shape (mixes, DJs, platforms, hours) is not judged
    # here, so `thresholds_met` never reads as "L3 passed"; matched by work it cannot be judged at
    # all, because the listed-precision bar needs timed truth.
    assert EXPECTED_TIME["l3"]["thresholds_met"] is False
    assert EXPECTED["l3"]["thresholds_met"] is None


def test_truth_status_unverified_then_verified_under_a_frozen_manifest(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    for set_id in ("mini-a", "mini-b"):
        _mark_verified(root / set_id / "ground_truth.json")
    code, document = _run_time(tmp_path / "unfrozen", root / "run-list.json")
    assert code == 0
    assert document["truth_status"] == "unverified"
    assert document["l3"]["certifiable"] is False
    # Same numbers either way: verification changes the label, never the score.
    assert {key: document[key] for key in HEADLINE} == {key: EXPECTED_TIME[key] for key in HEADLINE}

    _freeze(root, ["mini-a", "mini-b"])
    code, document = _run_time(tmp_path / "frozen", root / "run-list.json")
    assert code == 0
    assert document["truth_status"] == "verified"
    assert [mix["truth_status"] for mix in document["mixes"]] == ["verified", "verified"]
    # Round 8: frozen, verified, timed and independent would have been certifiable, but
    # certification is disabled until the certification follow-up lands.
    assert document["l3"]["certifiable"] is False
    assert (
        document["l3"]["certification"]
        == "certification is disabled until the certification follow-up lands"
    )
    report = json.loads((tmp_path / "frozen" / document["mixes"][0]["report"]).read_text("utf-8"))
    assert report["unverified_seed_comparison"] is False

    # Verified rows that kept the placeholder timings are still order-only, so the default run
    # matches by work — and a work-only score can never certify L3, verified or not.
    code, document = _run(tmp_path / "frozen-work", root / "run-list.json")
    assert code == 0
    assert document["truth_status"] == "verified"
    assert document["match_mode"] == "work"
    assert document["l3"] == {
        "thresholds": L3_THRESHOLDS["deep"],
        "thresholds_met": None,
        "independent": True,
        "certifiable": False,
        "certification": "certification is disabled until the certification follow-up lands",
    }

    # One draft mix drags the whole run back to draft.
    truth_b = json.loads((root / "mini-b" / "ground_truth.json").read_text("utf-8"))
    for episode in truth_b["episodes"]:
        episode.update(
            draft=True,
            annotator_ref=None,
            second_pass_ref=None,
            disagreement_resolution=None,
            verified_against=None,
        )
    _write_json(root / "mini-b" / "ground_truth.json", truth_b)
    code, document = _run_time(tmp_path / "mixed", root / "run-list.json")
    assert code == 0
    assert [mix["truth_status"] for mix in document["mixes"]] == ["verified", "draft"]
    assert document["truth_status"] == "draft"


# --- --print ----------------------------------------------------------------------------------


def test_print_mode_gives_one_plain_english_paragraph_then_one_line_per_mix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, document = _run_time(tmp_path, RUN_LIST, "--print")
    assert code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == summary(document, (tmp_path / "out.json").resolve())
    # Time mode also prints the work-only numbers as a secondary line, so the two kinds of run
    # compare; then one line per mix.
    assert lines[1] == work_only_line(document)
    assert lines[2:] == [mix_line(mix) for mix in document["mixes"]]
    for fragment in (
        "deep recipe",
        "2 mix(es) (mini-a, mini-b)",
        "DRAFT truth",
        # `--print` says which matching ran and why.
        "Matching is by TIME because --match time was given, although 2 of the 2 truth file(s) "
        "are order-only",
        "hid 1 as buried, 2 as short, leaving 7 listed",
        "4 of the 7 scored were tracks really played there (listed precision 57.1%)",
        "4 of the 5 it marked 'likely' or better were right (likely precision 80.0%)",
        "named 4 of the 6 distinct tracks actually played (work recall 66.7%)",
        "likely >= 90.0%, listed >= 80.0% and recall >= 75.0% for deep",
        "not met",
        "non-verified score cannot clear L3",
    ):
        assert fragment in lines[0], fragment
    assert lines[1].startswith(
        "Work-only (time-agnostic) numbers for comparison: it named 4 of the 6 distinct tracks "
        "actually played (work recall 66.7%); 4 of the 7 distinct tracks it listed were really "
        "played (work precision 57.1%)"
    )
    assert lines[2] == (
        "- mini-a [order-only; matched by time]: by time: likely 3/3 (100.0%), listed 3/5 (60.0%), "
        "recall 3/3 (100.0%); work-only: recall 3/3 (100.0%), precision 3/5 (60.0%), likely 3/3 "
        "(100.0%). Missed: nothing. Wrong: Mini Artist - Delta [possible]; Mini Artist - Eta "
        "[unclear]."
    )


def test_print_in_work_mode_explains_the_mode_and_lists_missed_and_wrong(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, document = _run(tmp_path, RUN_LIST, "--print")
    assert code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == summary(document, (tmp_path / "out.json").resolve())
    # The headline already is the work-only score: no secondary line, straight to the mixes.
    assert lines[1:] == [mix_line(mix) for mix in document["mixes"]]
    for fragment in (
        "Matching is WORK-ONLY (time-agnostic) because 2 of the 2 truth file(s) are order-only "
        "(a tracklist still carrying the seed's placeholder equal-slice timings)",
        "matched to the tracklist by normalised artist and title alone (mix suffixes, case, word "
        "order and featured artists ignored), never by when it played",
        "it named 4 of the 6 distinct tracks actually played (work recall 66.7%)",
        "4 of the 7 distinct tracks it listed were really played (work precision 57.1%)",
        "4 of the 5 it marked 'likely' or better were right (likely precision 80.0%)",
        "listed precision and every timing number need timed truth (reported as null)",
        "the L3 bar cannot be judged from a work-only score",
        "likely precision 80.0% < 90.0%; work recall 66.7% < 75.0%",
    ):
        assert fragment in lines[0], fragment
    assert "listed precision 57.1%" not in lines[0]
    assert lines[2] == (
        "- mini-b [order-only; matched by work]: work-only: recall 1/3 (33.3%), precision 1/2 "
        "(50.0%), likely 1/2 (50.0%). Missed: Mini Artist - Iota; Mini Artist - Mu. Wrong: "
        "Mini Artist - Kappa [likely]."
    )


def test_print_without_out_scores_into_a_scratch_directory(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--run-list", str(RUN_LIST), "--print"]) == 0
    text = capsys.readouterr().out
    assert "work precision 57.1%" in text
    assert "Full numbers" not in text
    assert not (FIXTURE / "out-mixes").exists()


def test_free_recipe_uses_its_own_recall_threshold_and_can_meet_the_bar() -> None:
    document = {
        "recipe": "free",
        "truth_status": "verified",
        "match_requested": "auto",
        "match_mode": "time",
        "likely_precision_e4": 9_500,
        "listed_precision_e4": 8_200,
        "work_precision_e4": 7_100,
        "work_recall_e4": 7_100,
        "l3": {"thresholds": L3_THRESHOLDS["free"]},
        "counts": {
            "mixes": 1,
            "episodes": {"total": 12, "listed": 12, "hidden": 0},
            "hidden_by_reason": {},
            "likely": {"correct": 19, "predicted": 20},
            "listed": {"correct": 41, "predicted": 50, "truth": 60},
            "work": {"correct": 71, "predicted": 100, "truth": 100},
        },
        "mixes": [{"mix_id": "only", "timing": "timed"}],
    }
    text = summary(document, None)
    assert "frozen, verified truth" in text
    assert "Matching is by TIME: every truth file has start times" in text
    assert "the presentation floor hid nothing" in text
    # Round 10: while certification is disabled no positive L3 claim is printed, however the
    # numbers look (the gate-open wording is tested in test_truth_corpus_followup_r10).
    assert "thresholds are met" not in text
    assert "recall >= 70.0% for free: no L3 threshold claim is made because" in text
    # Meeting the three numbers is not clearing L3: the corpus shape it also asks for is named.
    assert ">= 5 owner-verified mixes, >= 3 DJs, >= 2 platforms and >= 4 h of audio" in text
    assert "cannot clear" not in text


# --- work-only matching (part ii) -------------------------------------------------------------


def test_order_only_truth_is_detected_from_the_seeds_placeholders(tmp_path: Path) -> None:
    """The seed's fingerprint — every row ``manual annotation required`` at the equal-slice point
    ``i * duration // n`` — is what makes a truth order-only; a file with SOME real timings is
    ``partial`` and only a file with none left is ``timed``."""

    assert truth_timing(_truth("mini-a")) == "order-only"
    assert truth_timing(_truth("mini-b")) == "order-only"
    raw = json.loads((FIXTURE / "mini-a" / "ground_truth.json").read_text("utf-8"))
    # One row given a real start range by a verifier: partly timed, NOT timed — the other two rows
    # still hold nothing to score against.
    edited = json.loads(json.dumps(raw))
    edited["episodes"][1]["start_ms_range"] = [205_000, 215_000]
    edited["episodes"][1]["role_segments"][0]["from_ms"] = 205_000
    assert truth_timing(GroundTruthRecord.model_validate(edited)) == "partial"
    # A row annotated with a real audibility rule: partial, even at the placeholder point.
    edited = json.loads(json.dumps(raw))
    edited["episodes"][0]["audible_rule"] = "first beat audible"
    assert truth_timing(GroundTruthRecord.model_validate(edited)) == "partial"
    # Every row listened to: timed.
    every_row = _timed_truth(
        raw,
        [
            (0, 10_000, 60_000, 70_000),
            (205_000, 215_000, 260_000, 270_000),
            (450_000, 460_000, 590_000, 600_000),
        ],
    )
    assert truth_timing(GroundTruthRecord.model_validate(every_row)) == "timed"
    # Regression (review): a TIMED tracklist whose first track begins at 0:00 has row 0 sitting on
    # the first slice point by arithmetic, so the start alone cannot tell — the seeded Mall Grab set
    # is exactly this.  Both bounds are checked, so the file stays fully timed.
    from_zero = json.loads(json.dumps(every_row))
    from_zero["episodes"][0]["start_ms_range"] = [0, 0]
    from_zero["episodes"][0]["audible_rule"] = "manual annotation required"
    assert truth_timing(GroundTruthRecord.model_validate(from_zero)) == "timed"
    # Verification alone does not add timing: a verifier who kept the placeholders (the corpus
    # README's work-only option) leaves the truth order-only.
    root = _copy_fixture(tmp_path)
    _mark_verified(root / "mini-a" / "ground_truth.json")
    assert truth_timing(_truth("mini-a", root)) == "order-only"
    # Re-seeded from a different tracklist, the placeholders move with the row count.
    reseeded = _order_only_truth(raw, [("A", "One"), ("B", "Two"), ("C", "Three"), ("D", "Four")])
    assert [episode["start_ms_range"] for episode in reseeded["episodes"]] == [
        [0, 0],
        [150_000, 150_000],
        [300_000, 300_000],
        [450_000, 450_000],
    ]
    assert truth_timing(GroundTruthRecord.model_validate(reseeded)) == "order-only"


def test_a_half_verified_truth_is_matched_by_work_not_by_its_placeholder_slices(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression (review, P0): ``idea truth verify`` keeps the seed's ``audible_rule`` and, on a
    blank answer, the seed's range, while clearing ``draft`` — so an interrupted pass leaves a truth
    with SOME real start times and the rest on placeholders.  Time-matching such a file scores every
    untimed row as a miss: here mini-a's real work-only score is 3/4 recall and the time path sees
    0.  ``auto`` must therefore match it by work, and say so."""

    root = _copy_fixture(tmp_path)
    truth_path = root / "mini-a" / "ground_truth.json"
    reseeded = _order_only_truth(
        json.loads(truth_path.read_text("utf-8")),
        [
            ("Mini Artist feat. Guest", "GAMMA"),
            ("Mini Artist", "Alpha (Extended Mix)"),
            ("Mini Artist", "Omega"),
            ("mini artist", "Beta (Original Mix)"),
        ],
    )
    # The verifier listened to row 1 (Alpha, really 10 s - 70 s) and stopped there.
    reseeded["episodes"][0]["end_ms_range"] = [9_000, 9_000]
    reseeded["episodes"][0]["role_segments"] = [{"from_ms": 0, "to_ms": 9_000, "role": "uncertain"}]
    reseeded["episodes"][1]["start_ms_range"] = [9_000, 11_000]
    reseeded["episodes"][1]["end_ms_range"] = [69_000, 71_000]
    reseeded["episodes"][1]["audible_rule"] = "first beat audible"
    reseeded["episodes"][1]["role_segments"] = [
        {"from_ms": 9_000, "to_ms": 71_000, "role": "uncertain"}
    ]
    _write_json(truth_path, reseeded)
    assert truth_timing(_truth("mini-a", root)) == "partial"

    code, document = _run(tmp_path / "auto", root / "run-list.json", "--print")
    assert code == 0
    mini_a = next(mix for mix in document["mixes"] if mix["mix_id"] == "mini-a")
    assert mini_a["timing"] == "partial"
    assert mini_a["match_mode"] == "work"
    assert [mini_a[key] for key in HEADLINE] == [10_000, None, 6_000, 7_500]
    assert document["counts"]["timing"] == {"order-only": 1, "partial": 1}
    # The paragraph names the half-finished file rather than calling it timed.
    paragraph = capsys.readouterr().out.splitlines()[0]
    assert (
        "2 of the 2 truth file(s) still carry the seed's placeholder equal-slice timings on some "
        "or all rows (1 order-only, 1 only partly timed)" in paragraph
    )

    # The offset diagnostic reads only rows that no longer look like seed placeholders: Alpha (its
    # tracklist start is 9 s, the tool first heard it at 10 s) and row 0, whose end the verifier
    # trimmed (Gamma, heard at 450 s: +450 s); Beta's untouched placeholder row is left out.  The
    # lower median of {+1 s, +450 s} is +1 s.
    assert (mini_a["median_offset_ms"], mini_a["offset_pairs"]) == (1_000, 2)

    # What the old detector did: forced onto the time path, the untimed rows score as misses —
    # only Alpha, the one row with a real start, can be right (with identity parity, part iii, it
    # is: the strict key "mini artist|alpha extended mix" alone would have missed it too).
    code, document = _run_time(tmp_path / "time", root / "run-list.json", "--print")
    assert code == 0
    mini_a = next(mix for mix in document["mixes"] if mix["mix_id"] == "mini-a")
    assert [mini_a[key] for key in HEADLINE] == [3_333, 2_000, 2_000, 2_500]
    assert mini_a["counts"]["work"] == {"correct": 1, "predicted": 5, "truth": 4}
    assert mini_a["work_only"]["work_recall_e4"] == 7_500
    assert (
        "2 of the 2 truth file(s) still carry placeholder timings on some or all rows"
        in capsys.readouterr().out.splitlines()[0]
    )


def test_work_identity_is_fusions_normaliser_not_a_second_one() -> None:
    """Mix descriptors, case and punctuation fold exactly as ``hints.relations._normalise`` folds
    them; the word set is what ``fuse.identity`` compares order-independently."""

    assert work_identity("Bushbaby", "Whine Up (Extended Mix)") == work_identity(
        "bushbaby", "WHINE UP"
    )
    assert work_identity("Bakey", "Senses (Original Mix)")[0] == ("bakey", "senses")
    assert work_identity("Beau James", "4 Raws Edit (Edit)")[0] == ("beau james", "4 raws edit")
    key, words = work_identity("Old Sport & Loboski", "Tell Me Wagwan (ft. Flowdan)")
    assert key == ("old sport loboski", "tell me wagwan ft flowdan")
    # The key is fusion's; the word set drops the featuring MARKER only (part iii) — the featured
    # name stays, so "Flowdan" still has to be accounted for by the containment rule.
    assert words == {"old", "sport", "loboski", "tell", "me", "wagwan", "flowdan"}
    # A bare "(Extended)" is not a descriptor the shared normaliser strips: the scorer does not
    # strip it either (the word-set rule absorbs it as an extra word instead).
    assert work_identity("Bushbaby", "Whine Up (Extended)")[1] == {
        "bushbaby",
        "whine",
        "up",
        "extended",
    }
    # Swapped artist/title is the same word set.
    assert work_identity("Senses", "Bakey")[1] == work_identity("Bakey", "Senses")[1]


def test_match_works_ignores_time_mix_suffix_case_order_and_featured_artists() -> None:
    truth = [
        TruthWork(artist="Bushbaby", title="Whine Up (Extended Mix)"),
        TruthWork(artist="Old Sport & Loboski", title="Tell Me Wagwan (ft. Flowdan)"),
        TruthWork(artist="Bakey", title="Senses (Original Mix)"),
        TruthWork(artist="MPH", title="Raw (Extended Mix)"),
        TruthWork(artist="MPH", title="Raw Dub"),
        TruthWork(artist="Prozak", title="Clubgirls"),
        TruthWork(artist="Faster Horses", title="Get On Ya Knees"),
    ]
    listed = [
        # case + mix suffix: equal once normalised
        ListedWork("e1", "bushbaby", "WHINE UP", "likely", "w1"),
        # "&" vs "," and a "(ft. …)" clause: the listed word set is contained in the truth's
        ListedWork("e2", "Old Sport, Loboski", "Tell Me Wagwan", "likely", "w2"),
        # artist and title swapped: same word set
        ListedWork("e3", "Senses", "Bakey", "possible", "w3"),
        # two candidate rows: the exact normalised key ("Raw") beats the word-set one ("Raw Dub")
        ListedWork("e4", "MPH", "Raw", "likely", "w4"),
        # a single near-spelled long token (fusion's "clubgrls" / "clubgirls" rule)
        ListedWork("e5", "Prozak", "Clubgrls", "possible", "w5"),
        # wrong: shares the artist only
        ListedWork("e6", "MPH", "Fiesta", "likely", "w6"),
        # the same work listed a second time under another version label: one distinct work,
        # not a wrong row
        ListedWork("e7", "Bushbaby", "Whine Up (Original Mix)", "possible", "w1"),
    ]
    match = match_works(truth, listed)
    assert match.assignments == {0: 0, 1: 1, 2: 2, 3: 3, 4: 5, 6: 0}
    assert match.counts == WorkCounts(
        rows_correct=6,
        rows=7,
        likely_correct=3,
        likely=4,
        works_correct=5,
        works=6,
        truth_matched=5,
        truth=7,
    )
    assert match.counts.headline() == {
        "likely_precision_e4": 7_500,
        "work_precision_e4": 8_333,
        "work_recall_e4": 7_143,
    }
    assert match.unmatched_truth == [
        {"artist": "MPH", "title": "Raw Dub"},
        {"artist": "Faster Horses", "title": "Get On Ya Knees"},
    ]
    assert match.unmatched_predictions == [
        {"artist": "MPH", "title": "Fiesta", "tier": "likely", "rows": 1}
    ]


def test_match_works_counts_distinct_works_on_both_sides() -> None:
    # A track the DJ played twice is one truth work; naming it once recalls it.
    twice = [TruthWork(artist="X", title="Y"), TruthWork(artist="X", title="Y")]
    match = match_works(twice, [ListedWork("e1", "X", "Y", "likely", "w1")])
    assert match.counts.truth == 1 and match.counts.truth_matched == 1
    assert match.counts.headline()["work_recall_e4"] == 10_000
    # A track the tool listed twice (two rows of one identity work) is one listed work, and the
    # second row is not a wrong ID.
    once = [TruthWork(artist="X", title="Y")]
    match = match_works(
        once,
        [ListedWork("e1", "X", "Y", "likely", "w1"), ListedWork("e2", "X", "Y", "possible", "w1")],
    )
    assert match.assignments == {0: 0, 1: 0}
    assert match.counts == WorkCounts(2, 2, 1, 1, 1, 1, 1, 1)
    assert match.unmatched_predictions == []
    # Two wrong rows of one label collapse into one "wrong" entry carrying the higher tier.
    match = match_works(
        once,
        [ListedWork("e1", "A", "B", "possible", "w2"), ListedWork("e2", "A", "B", "likely", "w2")],
    )
    assert match.unmatched_predictions == [
        {"artist": "A", "title": "B", "tier": "likely", "rows": 2}
    ]
    assert match.counts.headline() == {
        "likely_precision_e4": 0,
        "work_precision_e4": 0,
        "work_recall_e4": 0,
    }
    # Nothing listed: zero, never a division error.
    assert match_works(once, []).counts.headline() == {
        "likely_precision_e4": 0,
        "work_precision_e4": 0,
        "work_recall_e4": 0,
    }


def test_labels_that_normalise_away_to_nothing_never_match() -> None:
    """Regression (review, P1): the exact-key branch compared normalised keys with no emptiness
    guard, so any two labels that fold to ``("", "")`` — an unnamed truth row against a prediction
    whose whole title was a mix descriptor — scored as a correct identification and inflated both
    precision and recall.  Fusion's own ``hints.relations._identity_key`` refuses an empty artist or
    title and its word-set rule needs two words, so the scorer must not be looser."""

    assert work_identity("???", "(Original Mix)") == (("", ""), frozenset())
    truth = [
        TruthWork(artist="???", title="(Original Mix)"),
        TruthWork(artist="Real", title="Song"),
    ]
    match = match_works(truth, [ListedWork("e1", "!!!", "(Extended Mix)", "likely", "w1")])
    assert match.assignments == {}
    assert match.counts == WorkCounts(0, 1, 0, 1, 0, 1, 0, 2)
    assert match.unmatched_predictions == [
        {"artist": "!!!", "title": "(Extended Mix)", "tier": "likely", "rows": 1}
    ]
    # An empty artist with a real multi-word title still matches, through the word-set rule.
    titled = [TruthWork(artist="", title="Long Season Intro Edit")]
    match = match_works(titled, [ListedWork("e1", "", "long season intro", "likely", "w1")])
    assert match.assignments == {0: 0}


def test_work_mode_fixture_titles_differ_only_by_suffix_case_and_feat(tmp_path: Path) -> None:
    """The cycle's fixture: ``mini-a``'s truth re-seeded as an order-only tracklist whose titles
    differ from the tool's labels only by mix suffix, case and a featured artist, listed in a
    different order (so a time association would be wrong even if the titles agreed), plus a track
    the tool never listed.  Exact counts."""

    root = _copy_fixture(tmp_path)
    truth_path = root / "mini-a" / "ground_truth.json"
    _write_json(
        truth_path,
        _order_only_truth(
            json.loads(truth_path.read_text("utf-8")),
            [
                ("Mini Artist feat. Guest", "GAMMA"),
                ("Mini Artist", "Alpha (Extended Mix)"),
                ("Mini Artist", "Omega"),
                ("mini artist", "Beta (Original Mix)"),
            ],
        ),
    )
    assert truth_timing(_truth("mini-a", root)) == "order-only"

    code, document = _run(tmp_path / "work", root / "run-list.json")
    assert code == 0
    assert document["match_mode"] == "work"
    mini_a = next(mix for mix in document["mixes"] if mix["mix_id"] == "mini-a")
    assert mini_a["counts"] == {
        "likely": {"correct": 3, "predicted": 3},
        "listed": None,
        "work": {"correct": 3, "predicted": 5, "truth": 4, "truth_matched": 3},
    }
    assert [mini_a[key] for key in HEADLINE] == [10_000, None, 6_000, 7_500]
    assert mini_a["unmatched_truth"] == [{"artist": "Mini Artist", "title": "Omega"}]
    assert mini_a["unmatched_predictions"] == [
        {"artist": "Mini Artist", "title": "Delta", "tier": "possible", "rows": 1},
        {"artist": "Mini Artist", "title": "Eta", "tier": "unclear", "rows": 1},
    ]
    # Pooled with mini-b (1/2 likely, 1 of 2 works, 1 of 3 truth works).
    assert document["counts"]["likely"] == {"correct": 4, "predicted": 5}
    assert document["counts"]["work"] == {
        "correct": 4,
        "predicted": 7,
        "truth": 7,
        "truth_matched": 4,
    }
    assert [document[key] for key in HEADLINE] == [8_000, None, 5_714, 5_714]
    # The assignment audit names each listed row's truth row.
    audit = json.loads((tmp_path / "work" / mini_a["work_match"]).read_text("utf-8"))
    by_episode = {item["episode_id"]: item["truth_index"] for item in audit["predictions"]}
    assert by_episode == {
        _episode_id("a1"): 1,
        _episode_id("a2"): 3,
        _episode_id("a3"): None,
        _episode_id("a5"): 0,
        _episode_id("a7"): None,
    }
    assert audit["truth"][2] == {
        "index": 2,
        "artist": "Mini Artist",
        "title": "Omega",
        "occurrence_index": 0,
        "named_by": [],
    }
    assert audit["counts"] == mini_a["work_only"]["counts"]

    # The time path cannot see any of them: the strict work key differs and the slices are in the
    # wrong places — which is why order-only truth is matched by work.
    code, document = _run_time(tmp_path / "time", root / "run-list.json")
    assert code == 0
    mini_a = next(mix for mix in document["mixes"] if mix["mix_id"] == "mini-a")
    assert mini_a["work_recall_e4"] == 0
    assert mini_a["listed_precision_e4"] == 0
    assert mini_a["work_only"]["work_recall_e4"] == 7_500


def test_a_timed_truth_is_matched_by_time_and_still_gets_work_only_numbers(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    _make_mini_b_timed(root)
    assert truth_timing(_truth("mini-b", root)) == "timed"
    run_list = json.loads((root / "run-list.json").read_text("utf-8"))
    run_list["runs"] = [run for run in run_list["runs"] if run["mix_id"] == "mini-b"]
    _write_json(root / "run-list.json", run_list)
    code, document = _run(tmp_path, root / "run-list.json")
    assert code == 0
    assert document["match_requested"] == "auto"
    assert document["match_mode"] == "time"
    assert document["counts"]["timing"] == {"timed": 1}
    assert [document[key] for key in HEADLINE] == [5_000, 5_000, 5_000, 3_333]
    assert document["l3"]["thresholds_met"] is False
    (mix,) = document["mixes"]
    assert mix["match_mode"] == "time"
    assert (tmp_path / mix["report"]).is_file()
    assert mix["work_only"] == {
        "likely_precision_e4": 5_000,
        "work_precision_e4": 5_000,
        "work_recall_e4": 3_333,
        "counts": {
            "rows": {"correct": 1, "predicted": 2},
            "likely": {"correct": 1, "predicted": 2},
            "works": {"correct": 1, "predicted": 2},
            "truth_works": {"matched": 1, "total": 3},
        },
    }
    assert mix["unmatched_truth"] == [
        {"artist": "Mini Artist", "title": "Iota"},
        {"artist": "Mini Artist", "title": "Mu"},
    ]


def test_a_mixed_run_list_pools_work_only_and_keeps_the_timed_mixs_time_numbers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One order-only mix and one timed mix: the pooled headline is the work-only score (the only
    one both have), the timed mix keeps its own time numbers on its row."""

    root = _copy_fixture(tmp_path)
    _make_mini_b_timed(root)
    code, document = _run(tmp_path / "auto", root / "run-list.json", "--print")
    assert code == 0
    assert document["match_mode"] == "work"
    assert document["counts"]["timing"] == {"order-only": 1, "timed": 1}
    assert [document[key] for key in HEADLINE] == [8_000, None, 5_714, 6_667]
    assert document["l3"]["thresholds_met"] is None
    by_mix = {mix["mix_id"]: mix for mix in document["mixes"]}
    assert by_mix["mini-a"]["match_mode"] == "work"
    assert by_mix["mini-a"]["report"] is None
    assert by_mix["mini-b"]["match_mode"] == "time"
    assert (tmp_path / "auto" / by_mix["mini-b"]["report"]).is_file()
    assert [by_mix["mini-b"][key] for key in HEADLINE] == [5_000, 5_000, 5_000, 3_333]
    assert by_mix["mini-b"]["counts"]["listed"] == {"correct": 1, "predicted": 2, "truth": 3}
    lines = capsys.readouterr().out.strip().splitlines()
    assert "because 1 of the 2 truth file(s) are order-only" in lines[0]
    assert lines[1].startswith("- mini-a [order-only; matched by work]: work-only:")
    assert lines[2].startswith("- mini-b [timed; matched by time]: by time: likely 1/2 (50.0%)")

    # Forced modes apply to every mix and say so.
    code, document = _run(tmp_path / "work", root / "run-list.json", "--match", "work", "--print")
    assert code == 0
    assert document["match_requested"] == "work"
    assert [mix["match_mode"] for mix in document["mixes"]] == ["work", "work"]
    assert "WORK-ONLY (time-agnostic) because --match work was given" in capsys.readouterr().out
    code, document = _run_time(tmp_path / "time", root / "run-list.json", "--print")
    assert code == 0
    assert [mix["match_mode"] for mix in document["mixes"]] == ["time", "time"]
    assert "although 1 of the 2 truth file(s) are order-only" in capsys.readouterr().out


def test_an_unknown_match_choice_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--run-list", str(RUN_LIST), "--print", "--match", "fuzzy"])
    assert raised.value.code == 2


# --- run list ---------------------------------------------------------------------------------


def test_relative_paths_resolve_against_the_run_list_not_the_cwd(tmp_path: Path) -> None:
    elsewhere = tmp_path / "lists"
    elsewhere.mkdir()
    run_list = json.loads(RUN_LIST.read_text("utf-8"))
    for run in run_list["runs"]:
        run["truth"] = os.path.relpath(FIXTURE / run["truth"], elsewhere)
        run["episodes"] = os.path.relpath(FIXTURE / run["episodes"], elsewhere)
    _write_json(elsewhere / "list.json", run_list)
    loaded = load_run_list(elsewhere / "list.json")
    assert [run.truth.resolve() for run in loaded.runs] == [
        (FIXTURE / "mini-a" / "ground_truth.json").resolve(),
        (FIXTURE / "mini-b" / "ground_truth.json").resolve(),
    ]
    assert Path.cwd() != elsewhere
    code, document = _run(tmp_path, elsewhere / "list.json")
    assert code == 0
    assert {key: document[key] for key in HEADLINE} == {key: EXPECTED[key] for key in HEADLINE}


def test_identities_come_from_the_runs_completion_sidecar_chain() -> None:
    """P0 regression: a NEWER generation beside the episodes is not this run's identity graph.

    ``mini-b``'s fuse directory carries a real run's sidecar chain (``episodes.done.json`` ->
    ``episodes.gen0.json`` -> ``identities.gen0.json``) plus a stale ``identities.gen2.json`` left
    by a second analysis of the same media, whose candidate ids are entirely different.  Taking the
    newest generation there crashed the scorer with ``StopIteration`` inside ``flatten_tracklist``.
    """

    fuse = FIXTURE / "mini-b" / "fuse"
    assert (fuse / "identities.gen2.json").is_file(), "the stale decoy must stay in the fixture"
    assert identities_path(_entry("mini-b"), _episodes("mini-b")) == fuse / "identities.gen0.json"


def test_a_stale_identity_graph_fails_loudly_naming_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _copy_fixture(tmp_path)
    run_list = json.loads((root / "run-list.json").read_text("utf-8"))
    run_list["runs"][1]["identities"] = str(root / "mini-b" / "fuse" / "identities.gen2.json")
    _write_json(root / "run-list.json", run_list)
    code, document = _run(tmp_path / "stale", root / "run-list.json")
    assert code == 1 and document is None
    error = capsys.readouterr().err
    assert "identities.gen2.json" in error
    assert "does not describe 3 candidate(s)" in error


def test_identities_fall_back_to_the_episodes_own_generation_then_the_newest(
    tmp_path: Path,
) -> None:
    fuse = tmp_path / "media" / "fuse"
    fuse.mkdir(parents=True)
    for name in ("episodes.json", "identities.gen0.json"):
        shutil.copyfile(FIXTURE / "mini-b" / "fuse" / name, fuse / name)
    episodes = _episodes("mini-b")
    entry = RunEntry(mix_id="m", truth=tmp_path / "t.json", episodes=fuse / "episodes.json")
    # No sidecars: pair by the generation the episodes themselves declare, not the newest.
    (fuse / "identities.gen2.json").write_text("{}", encoding="utf-8")
    assert episodes.generation == 0
    assert identities_path(entry, episodes) == fuse / "identities.gen0.json"
    # Only when that generation is absent does the newest stand in (numerically, not lexically).
    (fuse / "identities.gen0.json").unlink()
    (fuse / "identities.gen10.json").write_text("{}", encoding="utf-8")
    assert identities_path(entry, episodes) == fuse / "identities.gen10.json"
    explicit = entry.model_copy(update={"identities": tmp_path / "other.json"})
    assert identities_path(explicit, episodes) == tmp_path / "other.json"
    for name in ("identities.gen2.json", "identities.gen10.json"):
        (fuse / name).unlink()
    with pytest.raises(FileNotFoundError, match="identities.gen"):
        identities_path(entry, episodes)


# --- the media a run belongs to ----------------------------------------------------------------


def _work_layout(root: Path) -> Path:
    """Re-lay the copied fixture as real runs do: ``<media_key>/fuse/episodes.json``."""

    run_list = json.loads((root / "run-list.json").read_text("utf-8"))
    for run in run_list["runs"]:
        media_dir = root / "work" / run.pop("media_key")
        shutil.copytree(Path(run["episodes"]).parent, media_dir / "fuse")
        run["episodes"] = str(media_dir / "fuse" / "episodes.json")
    _write_json(root / "run-list.json", run_list)
    return root / "run-list.json"


def test_a_work_directory_states_its_own_media_key(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    run_list = _work_layout(root)
    entry = _entry("mini-b", run_list)
    assert entry.media_key is None
    assert (
        run_media_key(entry.episodes)
        == json.loads((root / "mini-b" / "ground_truth.json").read_text("utf-8"))["source"][
            "media_key"
        ]
    )
    code, document = _run(tmp_path / "work", run_list)
    assert code == 0
    assert {key: document[key] for key in HEADLINE} == {key: EXPECTED[key] for key in HEADLINE}


def test_episodes_from_another_mixs_run_fail_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A copy-pasted run-list row must not quietly publish one mix's score under another's truth."""

    root = _copy_fixture(tmp_path)
    run_list_path = _work_layout(root)
    run_list = json.loads(run_list_path.read_text("utf-8"))
    run_list["runs"][1]["episodes"] = run_list["runs"][0]["episodes"]
    _write_json(run_list_path, run_list)
    code, document = _run(tmp_path / "swapped", run_list_path)
    assert code == 1 and document is None
    assert "points at another mix's run" in capsys.readouterr().err


def test_an_unidentifiable_run_directory_fails_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _copy_fixture(tmp_path)
    run_list = json.loads((root / "run-list.json").read_text("utf-8"))
    del run_list["runs"][0]["media_key"]
    _write_json(root / "run-list.json", run_list)
    code, document = _run(tmp_path / "anonymous", root / "run-list.json")
    assert code == 1 and document is None
    assert "cannot tell which media" in capsys.readouterr().err


def test_an_ingest_source_record_outranks_the_directory_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _copy_fixture(tmp_path)
    run_list_path = _work_layout(root)
    entry = _entry("mini-a", run_list_path)
    _write_json(
        entry.episodes.parent.parent / "ingest" / "source.json",
        {"media_key": sha256(b"a different media").hexdigest()},
    )
    assert run_media_key(entry.episodes) == sha256(b"a different media").hexdigest()
    code, document = _run(tmp_path / "restated", run_list_path)
    assert code == 1 and document is None
    assert "points at another mix's run" in capsys.readouterr().err


def test_one_mix_may_appear_in_a_run_list_only_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pooling a mix twice double-weights it and inflates every headline number."""

    root = _copy_fixture(tmp_path)
    run_list = json.loads((root / "run-list.json").read_text("utf-8"))
    rerun = dict(run_list["runs"][0])
    rerun["mix_id"] = "mini-a-rerun"
    run_list["runs"].append(rerun)
    _write_json(root / "run-list.json", run_list)
    code, document = _run(tmp_path / "twice", root / "run-list.json")
    assert code == 1 and document is None
    assert "already scored as mini-a" in capsys.readouterr().err


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc.update(recipe="max_accuracy"),
        lambda doc: doc.update(runs=[]),
        lambda doc: doc["runs"].append(dict(doc["runs"][0])),
        lambda doc: doc["runs"][0].update(profile="deep"),
        lambda doc: doc["runs"][0].update(mix_id="../escape"),
        lambda doc: doc["runs"][0].update(min_track_ms=-1),
        lambda doc: doc.pop("recipe"),
    ],
    ids=[
        "unknown-recipe",
        "no-runs",
        "duplicate-mix-id",
        "unknown-key",
        "unsafe-mix-id",
        "negative-floor",
        "missing-recipe",
    ],
)
def test_invalid_run_lists_exit_2_without_scoring(tmp_path: Path, mutate) -> None:
    root = _copy_fixture(tmp_path)
    run_list = json.loads((root / "run-list.json").read_text("utf-8"))
    mutate(run_list)
    _write_json(root / "run-list.json", run_list)
    code, document = _run(tmp_path, root / "run-list.json")
    assert code == 2
    assert document is None
    assert not (tmp_path / "out-mixes").exists()


def test_scoring_failures_exit_1(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    run_list = json.loads((root / "run-list.json").read_text("utf-8"))
    run_list["runs"][1]["episodes"] = str(root / "mini-b" / "fuse" / "missing.json")
    _write_json(root / "run-list.json", run_list)
    code, document = _run(tmp_path / "missing", root / "run-list.json")
    assert code == 1 and document is None

    run_list = json.loads((root / "run-list.json").read_text("utf-8"))
    run_list["runs"][1]["episodes"] = str(root / "mini-b" / "fuse" / "episodes.json")
    run_list["runs"][1]["truth"] = str(root)  # a directory holding two sets
    _write_json(root / "run-list.json", run_list)
    code, document = _run(tmp_path / "two-sets", root / "run-list.json")
    assert code == 1 and document is None


def test_out_or_print_is_required() -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--run-list", str(RUN_LIST)])
    assert raised.value.code == 2


# --- the extracted prediction-set mapping -----------------------------------------------------


def test_prediction_set_from_fusion_is_the_corpus_mapping() -> None:
    media_dir = FIXTURE / "mini-a"
    fuse = media_dir / "fuse"
    episodes = EpisodesFile.model_validate_json((fuse / "episodes.json").read_text("utf-8"))
    identities = IdentitiesRecord.model_validate_json(
        (fuse / "identities.gen0.json").read_text("utf-8")
    )
    extracted = prediction_set_from_fusion("mini-a", identities, episodes)
    legacy = _prediction_set("mini-a", None, media_dir)
    assert canonical_json_bytes(extracted) == canonical_json_bytes(legacy)
    assert extracted["episodes"][0]["work"] == {"artist": "Mini Artist", "title": "Alpha"}
    assert extracted["episodes"][0]["version"] == {
        "qualifier": None,
        "ids": {"shazam": "mini-alpha"},
    }


# --- crowd rows: a position range, not engine-proved bounds ------------------------------------


def _crowd_episode(source: dict[str, object], start_ms: int, end_ms: int) -> dict[str, object]:
    """A ``hint_only`` row exactly as ``fuse/episodes.py`` writes one: no audio match, so the
    support IS the comment's position range and both bounds are that range's ends."""

    return {
        **source,
        "id": sha1(b"episode|crowd").hexdigest(),
        "claim": "component_evidence",
        "evidence_support_ms": [[start_ms, end_ms]],
        "start_no_later_than_ms": start_ms,
        "end_no_earlier_than_ms": end_ms,
        "best_start_ms": start_ms,
        "best_end_ms": end_ms,
        "start_pi": None,
        "end_pi": None,
        "role_segments": [{"from_ms": start_ms, "to_ms": end_ms, "role": "dominant"}],
        "flags": ["hint_only", "hint_supported"],
        "badge": "possible",
        "suppressed": None,
    }


def test_proved_bounds_swaps_a_range_claims_bounds_and_leaves_engine_rows_alone() -> None:
    engine = {
        "evidence_support_ms": [(10_000, 70_000)],
        "start_no_later_than_ms": 22_000,
        "end_no_earlier_than_ms": 58_000,
        "best_start_ms": 22_000,
        "best_end_ms": 58_000,
        "start_pi": None,
        "end_pi": None,
    }
    crowd = {
        **engine,
        "evidence_support_ms": [(250_000, 260_000)],
        "start_no_later_than_ms": 250_000,
        "end_no_earlier_than_ms": 260_000,
        "best_start_ms": 250_000,
        "best_end_ms": 260_000,
    }
    normalised, claims = proved_bounds([engine, crowd])
    assert claims == 1
    assert normalised[0] == engine
    # "played somewhere in [250 s, 260 s]" == started no later than 260 s, ended no earlier than
    # 250 s: the only reading the ScoredEpisode contract accepts.
    assert normalised[1]["start_no_later_than_ms"] == 260_000
    assert normalised[1]["end_no_earlier_than_ms"] == 250_000
    assert normalised[1]["best_start_ms"] == 260_000
    assert normalised[1]["best_end_ms"] == 250_000


def test_a_crowd_row_is_scored_instead_of_failing_the_prediction_contract(tmp_path: Path) -> None:
    """P0 regression: every real Free run carries crowd rows, and ``ScoredEpisode`` rejects their
    bounds outright, so all five release-1 mixes failed validation before this."""

    root = _copy_fixture(tmp_path)
    episodes_path = root / "mini-b" / "fuse" / "episodes.json"
    episodes = json.loads(episodes_path.read_text("utf-8"))
    episodes["episodes"].append(_crowd_episode(episodes["episodes"][-1], 250_000, 260_000))
    _write_json(episodes_path, episodes)
    code, document = _run_time(tmp_path / "crowd", root / "run-list.json")
    assert code == 0
    mini_b = next(mix for mix in document["mixes"] if mix["mix_id"] == "mini-b")
    assert mini_b["episodes_total"] == 4
    # The crowd row is listed (`hint_supported` keeps it) and scored as a prediction.
    assert mini_b["episodes_listed"] == 3
    assert mini_b["range_claims"] == 1
    assert document["counts"]["range_claims"] == 1
    assert mini_b["counts"]["listed"]["predicted"] == 3
    # The contract check runs in work mode too, and the crowd row is a listed row there as well.
    code, document = _run(tmp_path / "crowd-work", root / "run-list.json")
    assert code == 0
    mini_b = next(mix for mix in document["mixes"] if mix["mix_id"] == "mini-b")
    assert mini_b["range_claims"] == 1
    assert mini_b["work_only"]["counts"]["rows"]["predicted"] == 3


# --- part iii: timed-mode identity parity, overlay truth rows, the offset diagnostic -----------


def _relabel_text_node(fuse_dir: Path, old_id: str, new_id: str, new_label: str) -> None:
    """Rename one ``text:`` node of a fixture graph everywhere it is referenced."""

    path = fuse_dir / "identities.gen0.json"
    graph = json.loads(path.read_text("utf-8"))
    for node in graph["nodes"]:
        if node["id"] == old_id:
            node["id"], node["label"] = new_id, new_label
    for group in ("works", "candidates"):
        for item in graph[group]:
            item["member_nodes"] = [
                new_id if node == old_id else node for node in item["member_nodes"]
            ]
    for assertion in graph["assertions"]:
        for side in ("a", "b"):
            if assertion[side] == old_id:
                assertion[side] = new_id
    _write_json(path, graph)


def test_featuring_marker_spelling_never_separates_one_work() -> None:
    """The cycle's pair: "MPH ft. Cecelia - Rush" (truth) and "MPH - Rush (feat. Cecelia)" (engine)
    are one work.  Fusion's normaliser keeps the marker and its near-spelling tolerance needs four
    letters, so neither the key nor the word-set rule alone can say so; the scorer drops the
    marker (never the name) from the word set, in both modes."""

    truth_key, truth_words = work_identity("MPH ft. Cecelia", "Rush")
    listed_key, listed_words = work_identity("MPH", "Rush (feat. Cecelia)")
    assert truth_key == ("mph ft cecelia", "rush")
    assert listed_key == ("mph", "rush feat cecelia")
    assert not _word_sets_corroborate(
        frozenset(["mph", "ft", "cecelia", "rush"]), frozenset(["mph", "rush", "feat", "cecelia"])
    )
    assert truth_words == listed_words == {"mph", "cecelia", "rush"}
    assert work_identity("MPH", "Rush featuring Cecelia")[1] == truth_words
    # The name is what identifies the work: a different featured artist is a different word set.
    assert work_identity("MPH", "Rush (feat. Somebody Else)")[1] != truth_words

    truth = [
        TruthWork(artist="MPH ft. Cecelia", title="Rush"),
        TruthWork(artist="AC Slater & MPH ft. Eloise Keeble", title="Lights On"),
    ]
    listed = [
        ListedWork("e1", "MPH", "Rush (feat. Cecelia)", "likely", "w1"),
        ListedWork("e2", "MPH & AC Slater", "Lights On (feat. Eloise Keeble)", "possible", "w2"),
        ListedWork("e3", "MPH", "Rush (feat. Somebody Else)", "possible", "w3"),
    ]
    match = match_works(truth, listed)
    assert match.assignments == {0: 0, 1: 1}
    assert match.unmatched_predictions == [
        {"artist": "MPH", "title": "Rush (feat. Somebody Else)", "tier": "possible", "rows": 1}
    ]


def test_parity_identities_grants_each_truth_key_to_one_work_only() -> None:
    fuse = FIXTURE / "mini-b" / "fuse"
    identities = IdentitiesRecord.model_validate_json(
        (fuse / "identities.gen0.json").read_text("utf-8")
    )
    work_of = {node: work.work_id for work in identities.works for node in work.member_nodes}
    theta, kappa = work_of["text:mini artist|theta"], work_of["text:mini artist|kappa"]
    truth = _truth("mini-b").model_copy(
        update={
            "episodes": [
                _truth("mini-b")
                .episodes[0]
                .model_copy(
                    update={"work": TruthWork(artist="Mini Artist feat. Guest", title="Theta")}
                )
            ]
        }
    )
    # Two listed works both corroborate the one truth row (the work matcher is many-to-one); the
    # scorer resolves a truth to ONE work, so the key goes to the closest word set: the row whose
    # words equal the truth's beats the one missing "guest".
    listed = [
        ListedWork("e-theta", "Mini Artist", "Theta", "likely", theta),
        ListedWork("e-kappa", "Mini Artist Guest", "Theta", "possible", kappa),
    ]
    match = match_works([episode.work for episode in truth.episodes], listed)
    assert match.assignments == {0: 0, 1: 0}
    canonical, granted = parity_identities(identities, truth, listed, match)
    key = f"text:{work_key(truth.episodes[0].work)}"
    assert key == "text:mini artist feat guest|theta"
    assert granted == 1
    holders = [work.work_id for work in canonical.works if key in work.member_nodes]
    assert holders == [kappa]
    node = next(item for item in canonical.nodes if item.id == key)
    assert (node.ns, node.label) == ("text", "Mini Artist feat. Guest - Theta")
    # The run's own graph is not touched: a new record is returned.
    assert key not in {item.id for item in identities.nodes}
    assert len(canonical.nodes) == len(identities.nodes) + 1

    # A key some work already holds is left there (the scorer resolves the truth to it already),
    # and nothing is granted or copied.
    held = truth.model_copy(
        update={
            "episodes": [
                truth.episodes[0].model_copy(
                    update={"work": TruthWork(artist="Mini Artist", title="Theta")}
                )
            ]
        }
    )
    other = [ListedWork("e-kappa", "Mini Artist Theta", "Theta", "possible", kappa)]
    match = match_works([episode.work for episode in held.episodes], other)
    assert match.assignments == {0: 0}
    assert parity_identities(identities, held, other, match) == (identities, 0)
    # Strict equality already: nothing to grant either.
    same = [ListedWork("e-theta", "Mini Artist", "Theta", "likely", theta)]
    match = match_works([episode.work for episode in held.episodes], same)
    assert parity_identities(identities, held, same, match) == (identities, 0)


def test_timed_truth_with_a_near_duplicate_label_is_matched_by_time_only_through_parity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The cycle's fixture: ``mini-b`` really timed, its Theta row written "Mini Artist ft. Guest -
    THETA (Extended Mix)" in the truth and "Mini Artist - Theta (feat. Guest)" in the run's identity
    graph — one work, spelled two ways, at the same time.  The time path must score it as the work
    matcher does, and the original labels must survive in the missed / wrong lists."""

    root = _copy_fixture(tmp_path)
    _make_mini_b_timed(root)
    truth_path = root / "mini-b" / "ground_truth.json"
    truth = json.loads(truth_path.read_text("utf-8"))
    truth["episodes"][0]["work"] = {
        "artist": "Mini Artist ft. Guest",
        "title": "THETA (Extended Mix)",
    }
    _write_json(truth_path, truth)
    _relabel_text_node(
        root / "mini-b" / "fuse",
        "text:mini artist|theta",
        "text:mini artist|theta (feat. guest)",
        "Mini Artist - Theta (feat. Guest)",
    )
    run_list = json.loads((root / "run-list.json").read_text("utf-8"))
    run_list["runs"] = [run for run in run_list["runs"] if run["mix_id"] == "mini-b"]
    _write_json(root / "run-list.json", run_list)

    code, document = _run(tmp_path / "auto", root / "run-list.json", "--print")
    assert code == 0
    assert document["match_mode"] == "time"
    (mix,) = document["mixes"]
    # Exactly the numbers the untouched timed fixture scores: Theta right, Kappa wrong, Iota and Mu
    # missed — the spelling cost nothing.
    assert [mix[key] for key in HEADLINE] == [5_000, 5_000, 5_000, 3_333]
    assert mix["counts"]["listed"] == {"correct": 1, "predicted": 2, "truth": 3}
    assert mix["work_only"]["work_recall_e4"] == 3_333
    # The lists keep the labels as written on each side.
    assert mix["unmatched_truth"] == [
        {"artist": "Mini Artist", "title": "Iota"},
        {"artist": "Mini Artist", "title": "Mu"},
    ]
    assert mix["unmatched_predictions"] == [
        {"artist": "Mini Artist", "title": "Kappa", "tier": "likely", "rows": 1}
    ]
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert (
        "Missed: Mini Artist - Iota; Mini Artist - Mu. Wrong: Mini Artist - Kappa [likely]." in line
    )
    # What parity did: the truth row's own scorer key joined the Theta work as one more text node.
    predictions_path = (tmp_path / "auto" / mix["report"]).with_name("predictions.json")
    predictions = json.loads(predictions_path.read_text("utf-8"))
    assert predictions["config_snapshot"]["run_config"]["parity_keys"] == 1
    graph = predictions["sets"][0]["identities"]
    key = "text:mini artist ft guest|theta extended mix"
    (holder,) = [work for work in graph["works"] if key in work["member_nodes"]]
    assert "text:mini artist|theta (feat. guest)" in holder["member_nodes"]
    assert predictions["sets"][0]["episodes"][0]["work"] == {
        "artist": "Mini Artist",
        "title": "Theta (feat. Guest)",
    }
    # Without it the certified scorer — unchanged by this cycle — sees two different strings and
    # scores the same run 0 for 3: the reason the wrapper canonicalises.
    original = json.loads((root / "mini-b" / "fuse" / "identities.gen0.json").read_text("utf-8"))
    predictions["sets"][0]["identities"] = original
    document_without = PredictionDocument.model_validate(predictions)
    state = score_set(GroundTruthRecord.model_validate(truth), document_without.sets[0]).state
    assert (state.identification_work.correct, state.occurrence.correct) == (0, 0)
    assert state.tier_work["likely"] == (0, 2)


def test_median_offset_reads_engine_evidence_against_really_timed_rows_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _copy_fixture(tmp_path)
    _make_mini_b_timed(root)
    entry = _entry("mini-b", root / "run-list.json")
    episodes = _episodes("mini-b")
    truth = _truth("mini-b", root)
    assert placeholder_rows(truth) == frozenset()
    identities = IdentitiesRecord.model_validate_json(
        (root / "mini-b" / "fuse" / "identities.gen0.json").read_text("utf-8")
    )
    listed, _ = listed_episodes(episodes, identities, entry.min_track_ms)
    prediction_set = prediction_set_from_fusion("mini-b", identities, listed)
    listed_works = [
        ListedWork(
            episode.id,
            item["work"]["artist"],
            item["work"]["title"],
            item["tiers"].work,
            "w-" + episode.id,
        )
        for episode, item in zip(listed.episodes, prediction_set["episodes"], strict=True)
    ]
    match = match_works([episode.work for episode in truth.episodes], listed_works)
    # Theta: the tool first heard it at 5 s, the tracklist starts it at 0 s.  Kappa is unmatched.
    assert median_offset(listed, match, truth, range_claims=set()) == (5_000, 1)
    # A crowd row (a position range from the same comments) says nothing about the video, so it is
    # left out even when the work matcher pairs it.
    assert median_offset(listed, match, truth, range_claims={0}) == (None, 0)
    # A seed placeholder row is left out too.
    assert median_offset(listed, match, _truth("mini-b"), range_claims=set()) == (None, 0)

    run_list = json.loads((root / "run-list.json").read_text("utf-8"))
    run_list["runs"] = [run for run in run_list["runs"] if run["mix_id"] == "mini-b"]
    _write_json(root / "run-list.json", run_list)
    code, document = _run(tmp_path / "timed", root / "run-list.json", "--print")
    assert code == 0
    (mix,) = document["mixes"]
    assert (mix["median_offset_ms"], mix["offset_pairs"]) == (5_000, 1)
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert (
        "; median offset (tool start minus tracklist start) +5.0 s over 1 work-matched pair. "
        "Missed:" in line
    )
    assert line.startswith("- mini-b [timed; matched by time]: by time: likely 1/2 (50.0%)")
    # An order-only mix prints no offset at all; a timed mix with no usable pair says so.
    assert "median offset" not in mix_line({**EXPECTED["mixes"][0]})
    assert "median offset n/a" in mix_line({**mix, "median_offset_ms": None, "offset_pairs": 0})


def test_median_offset_is_the_lower_median_of_signed_offsets(tmp_path: Path) -> None:
    """Four timed rows on mini-a: offsets +10 s, +5 s, -10 s, +10 s -> the lower middle, +5 s."""

    root = _copy_fixture(tmp_path)
    truth_path = root / "mini-a" / "ground_truth.json"
    raw = json.loads(truth_path.read_text("utf-8"))
    four = _order_only_truth(
        raw,
        [
            ("Mini Artist", "Alpha"),
            ("Mini Artist", "Beta"),
            ("Mini Artist", "Delta"),
            ("Mini Artist", "Gamma"),
        ],
    )
    # a1 Alpha heard at 10 s, a2 Beta at 210 s, a3 Delta at 300 s, a5 Gamma at 450 s.
    timed = _timed_truth(
        four,
        [
            (0, 0, 190_000, 190_000),
            (205_000, 205_000, 280_000, 280_000),
            (290_000, 290_000, 440_000, 440_000),
            (460_000, 460_000, 600_000, 600_000),
        ],
    )
    _write_json(truth_path, timed)
    assert truth_timing(_truth("mini-a", root)) == "timed"
    run_list = json.loads((root / "run-list.json").read_text("utf-8"))
    run_list["runs"] = [run for run in run_list["runs"] if run["mix_id"] == "mini-a"]
    _write_json(root / "run-list.json", run_list)
    code, document = _run(tmp_path / "four", root / "run-list.json")
    assert code == 0
    (mix,) = document["mixes"]
    assert (mix["median_offset_ms"], mix["offset_pairs"]) == (5_000, 4)


def test_an_overlay_seeded_truth_scores_the_blended_track_by_time(tmp_path: Path) -> None:
    """``idea truth seed --overlays``: a second track blended in at a row's time becomes a layered
    truth episode from that time to the row's end, and a listed prediction of it is right by time
    (the MPH "w/" rows scored as WRONG predictions against a truth that could not hold them)."""

    root = _copy_fixture(tmp_path)
    media_key = _truth("mini-b").source.media_key
    tracklist = tmp_path / "tracklist.txt"
    tracklist.write_text(
        "0:00:00 - Mini Artist - Theta\n0:01:40 - Mini Artist - Iota\n0:03:20 - Mini Artist - Mu\n",
        encoding="utf-8",
    )
    overlays = tmp_path / "overlays.txt"
    overlays.write_text(
        "# blended in over the row playing at that time\n"
        "0:01:40 - Mini Artist - Kappa (w/ overlay)\n",
        encoding="utf-8",
    )
    truth_path = root / "mini-b" / "ground_truth.json"
    # Seeding never overwrites an existing truth set (round-3 R-P0-2); replacing this fixture copy's
    # record is deliberate here, so it is removed first, exactly as the refusal message advises.
    truth_path.unlink()
    seeded = seed_truth(
        out_path=truth_path,
        set_id="mini-b",
        duration_ms=300_000,
        media_key=media_key,
        tracklist=tracklist,
        overlays=overlays,
        split="test",
        corpus_version="corpus-mini",
        project_root=tmp_path,
    )
    assert [episode.work.title for episode in seeded.episodes] == ["Theta", "Iota", "Kappa", "Mu"]
    kappa = seeded.episodes[2]
    assert (tuple(kappa.start_ms_range), tuple(kappa.end_ms_range)) == (
        (100_000, 100_000),
        (200_000, 200_000),
    )
    assert [segment.role for segment in kappa.role_segments] == ["layer"]
    assert kappa.overlaps_with == [1] and seeded.episodes[1].overlaps_with == [2]
    assert truth_timing(seeded) == "timed"

    run_list = json.loads((root / "run-list.json").read_text("utf-8"))
    run_list["runs"] = [run for run in run_list["runs"] if run["mix_id"] == "mini-b"]
    _write_json(root / "run-list.json", run_list)
    code, document = _run(tmp_path / "overlay", root / "run-list.json")
    assert code == 0
    (mix,) = document["mixes"]
    assert mix["match_mode"] == "time"
    # Kappa [100 s, 140 s] now names the overlay row: both listed rows right, 2 of 4 works named.
    assert [mix[key] for key in HEADLINE] == [10_000, 10_000, 10_000, 5_000]
    assert mix["counts"]["listed"] == {"correct": 2, "predicted": 2, "truth": 4}
    assert mix["unmatched_predictions"] == []
    assert mix["unmatched_truth"] == [
        {"artist": "Mini Artist", "title": "Iota"},
        {"artist": "Mini Artist", "title": "Mu"},
    ]
    # Theta heard 5 s late, Kappa on time: the lower median of {0, +5 s}.
    assert (mix["median_offset_ms"], mix["offset_pairs"]) == (0, 2)
    report = json.loads((tmp_path / "overlay" / mix["report"]).read_text("utf-8"))
    assert report["overall"]["identification_work"]["recall_e4"] == 5_000


def test_is_range_claim_is_the_predicate_behind_proved_bounds() -> None:
    engine = {
        "evidence_support_ms": [(10_000, 70_000)],
        "start_no_later_than_ms": 22_000,
        "end_no_earlier_than_ms": 58_000,
        "start_pi": None,
        "end_pi": None,
    }
    crowd = {
        **engine,
        "evidence_support_ms": [(250_000, 260_000)],
        "start_no_later_than_ms": 250_000,
        "end_no_earlier_than_ms": 260_000,
    }
    assert not is_range_claim(engine)
    assert is_range_claim(crowd)

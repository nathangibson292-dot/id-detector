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
4/6 = 6667 (from 3/3 + 1/3).  ``expected.json`` is the script's exact output for the fixture.
"""

from __future__ import annotations

import json
import os
import shutil
from hashlib import sha1, sha256
from pathlib import Path

import pytest

from id_detector.benchmark.corpus import _prediction_set, prediction_set_from_fusion
from id_detector.benchmark.scorer import pooled_metrics
from id_detector.contracts import EpisodesFile, IdentitiesRecord
from id_detector.io import canonical_json_bytes
from scripts.score_corpus import (
    L3_THRESHOLDS,
    RunEntry,
    identities_path,
    listed_episodes,
    load_run_list,
    main,
    proved_bounds,
    run_media_key,
    score_mix,
    summary,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "corpus-mini"
RUN_LIST = FIXTURE / "run-list.json"
EXPECTED = json.loads((FIXTURE / "expected.json").read_text("utf-8"))
HEADLINE = ("likely_precision_e4", "listed_precision_e4", "work_recall_e4")


def _episode_id(key: str) -> str:
    return sha1(f"episode|{key}".encode()).hexdigest()


def _episodes(mix_id: str) -> EpisodesFile:
    path = FIXTURE / mix_id / "fuse" / "episodes.json"
    return EpisodesFile.model_validate_json(path.read_text("utf-8"))


def _entry(mix_id: str, run_list: Path = RUN_LIST) -> RunEntry:
    return next(run for run in load_run_list(run_list).runs if run.mix_id == mix_id)


def _ratio_e4(numerator: int, denominator: int) -> int:
    return (numerator * 10_000 + denominator // 2) // denominator


def _run(tmp_path: Path, run_list: Path = RUN_LIST, *extra: str) -> tuple[int, dict | None]:
    out = tmp_path / "out.json"
    code = main(["--run-list", str(run_list), "--out", str(out), *extra])
    return code, (json.loads(out.read_text("utf-8")) if out.is_file() else None)


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


# --- the gate ---------------------------------------------------------------------------------


def test_corpus_mini_reproduces_expected_exactly(tmp_path: Path) -> None:
    code, document = _run(tmp_path)
    assert code == 0
    assert document == EXPECTED
    # The per-mix artefacts the plan's `idea benchmark score` step would have written.
    for mix in document["mixes"]:
        assert (tmp_path / mix["report"]).is_file()
        assert (tmp_path / mix["report"]).with_name("predictions.json").is_file()


def test_headline_numbers_are_pooled_counts_not_a_mean_of_ratios() -> None:
    counts = EXPECTED["counts"]
    assert counts["likely"] == {"correct": 4, "predicted": 5}
    assert counts["listed"] == {"correct": 4, "predicted": 7, "truth": 6}
    assert counts["work"] == {"correct": 4, "predicted": 7, "truth": 6}
    assert EXPECTED["likely_precision_e4"] == _ratio_e4(4, 5) == 8_000
    assert EXPECTED["listed_precision_e4"] == _ratio_e4(4, 7) == 5_714
    assert EXPECTED["work_recall_e4"] == _ratio_e4(4, 6) == 6_667
    by_mix = {mix["mix_id"]: mix for mix in EXPECTED["mixes"]}
    assert [by_mix["mini-a"][key] for key in HEADLINE] == [10_000, 6_000, 10_000]
    assert [by_mix["mini-b"][key] for key in HEADLINE] == [5_000, 5_000, 3_333]
    # A macro average of the per-mix ratios would have said 7500 / 5500 / 6667 (recall by luck).
    assert EXPECTED["listed_precision_e4"] != (6_000 + 5_000) // 2
    assert EXPECTED["likely_precision_e4"] != (10_000 + 5_000) // 2
    # Every per-mix count sums to the pooled count.
    for metric in ("likely", "listed", "work"):
        for field, total in counts[metric].items():
            assert sum(mix["counts"][metric][field] for mix in EXPECTED["mixes"]) == total


def test_headline_fields_come_from_the_scorer_report_by_name(tmp_path: Path) -> None:
    """The three numbers are read from the real benchmark report under the plan's field names."""

    code, document = _run(tmp_path)
    assert code == 0
    for mix in document["mixes"]:
        report = json.loads((tmp_path / mix["report"]).read_text("utf-8"))
        overall = report["overall"]
        assert mix["likely_precision_e4"] == overall["empirical_tier_precision_e4"]["likely"]
        assert mix["listed_precision_e4"] == overall["selective_precision_e4"]
        assert mix["work_recall_e4"] == overall["identification_work"]["recall_e4"]
        assert [item["set_id"] for item in report["sets"]] == [mix["set_id"]]
        assert report["profile"] == "deep"
        assert report["unverified_seed_comparison"] is True
        # Only the listed episodes reached the scorer.
        predictions = json.loads(
            (tmp_path / mix["report"]).with_name("predictions.json").read_text()
        )
        assert len(predictions["sets"][0]["episodes"]) == mix["episodes_listed"]
        assert (
            predictions["config_snapshot"]["run_config"]["hidden_by_reason"]
            == (mix["hidden_by_reason"])
        )


def test_pooled_metrics_sums_states_before_taking_ratios(tmp_path: Path) -> None:
    run_list = load_run_list(RUN_LIST)
    scored = [score_mix(entry, recipe="deep", artefact_dir=tmp_path) for entry in run_list.runs]
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


def test_min_track_ms_zero_lists_the_short_rows_and_moves_listed_precision(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    run_list = json.loads((root / "run-list.json").read_text("utf-8"))
    for run in run_list["runs"]:
        run["min_track_ms"] = 0
    _write_json(root / "run-list.json", run_list)
    code, document = _run(tmp_path, root / "run-list.json")
    assert code == 0
    assert document["counts"]["hidden_by_reason"] == {"buried": 1}
    assert document["counts"]["episodes"] == {"total": 10, "listed": 9, "hidden": 1}
    assert document["counts"]["listed"] == {"correct": 4, "predicted": 9, "truth": 6}
    assert document["listed_precision_e4"] == _ratio_e4(4, 9) == 4_444
    # The short rows were `possible`, so the likely tier and the recall are untouched.
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
    assert EXPECTED["truth_status"] == "draft"
    assert [mix["truth_status"] for mix in EXPECTED["mixes"]] == ["draft", "draft"]
    assert EXPECTED["l3"] == {
        "thresholds": L3_THRESHOLDS["deep"],
        # The three thresholds only: L3's corpus shape (mixes, DJs, platforms, hours) is not judged
        # here, so `thresholds_met` never reads as "L3 passed".
        "thresholds_met": False,
        "certifiable": False,
    }


def test_truth_status_unverified_then_verified_under_a_frozen_manifest(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    for set_id in ("mini-a", "mini-b"):
        _mark_verified(root / set_id / "ground_truth.json")
    code, document = _run(tmp_path / "unfrozen", root / "run-list.json")
    assert code == 0
    assert document["truth_status"] == "unverified"
    assert document["l3"]["certifiable"] is False
    # Same numbers either way: verification changes the label, never the score.
    assert {key: document[key] for key in HEADLINE} == {key: EXPECTED[key] for key in HEADLINE}

    _freeze(root, ["mini-a", "mini-b"])
    code, document = _run(tmp_path / "frozen", root / "run-list.json")
    assert code == 0
    assert document["truth_status"] == "verified"
    assert [mix["truth_status"] for mix in document["mixes"]] == ["verified", "verified"]
    assert document["l3"]["certifiable"] is True
    report = json.loads((tmp_path / "frozen" / document["mixes"][0]["report"]).read_text("utf-8"))
    assert report["unverified_seed_comparison"] is False

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
    code, document = _run(tmp_path / "mixed", root / "run-list.json")
    assert code == 0
    assert [mix["truth_status"] for mix in document["mixes"]] == ["verified", "draft"]
    assert document["truth_status"] == "draft"


# --- --print ----------------------------------------------------------------------------------


def test_print_mode_gives_one_plain_english_paragraph(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, document = _run(tmp_path, RUN_LIST, "--print")
    assert code == 0
    text = capsys.readouterr().out.strip()
    assert text == summary(document, (tmp_path / "out.json").resolve())
    assert "\n" not in text
    for fragment in (
        "deep recipe",
        "2 mix(es) (mini-a, mini-b)",
        "DRAFT truth",
        "hid 1 as buried, 2 as short, leaving 7 listed",
        "4 of the 7 scored were tracks really played there (listed precision 57.1%)",
        "4 of the 5 it marked 'likely' or better were right (likely precision 80.0%)",
        "named 4 of the 6 distinct tracks actually played (work recall 66.7%)",
        "likely >= 90.0%, listed >= 80.0% and recall >= 75.0% for deep",
        "not met",
        "non-verified score cannot clear L3",
    ):
        assert fragment in text, fragment


def test_print_without_out_scores_into_a_scratch_directory(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--run-list", str(RUN_LIST), "--print"]) == 0
    text = capsys.readouterr().out
    assert "listed precision 57.1%" in text
    assert "Full numbers" not in text
    assert not (FIXTURE / "out-mixes").exists()


def test_free_recipe_uses_its_own_recall_threshold_and_can_meet_the_bar() -> None:
    document = {
        "recipe": "free",
        "truth_status": "verified",
        "likely_precision_e4": 9_500,
        "listed_precision_e4": 8_200,
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
        "mixes": [{"mix_id": "only"}],
    }
    text = summary(document, None)
    assert "frozen, verified truth" in text
    assert "the presentation floor hid nothing" in text
    assert "recall >= 70.0% for free: all three thresholds are met on these numbers" in text
    # Meeting the three numbers is not clearing L3: the corpus shape it also asks for is named.
    assert ">= 5 owner-verified mixes, >= 3 DJs, >= 2 platforms and >= 4 h of audio" in text
    assert "cannot clear" not in text


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
    code, document = _run(tmp_path / "crowd", root / "run-list.json")
    assert code == 0
    mini_b = next(mix for mix in document["mixes"] if mix["mix_id"] == "mini-b")
    assert mini_b["episodes_total"] == 4
    # The crowd row is listed (`hint_supported` keeps it) and scored as a prediction.
    assert mini_b["episodes_listed"] == 3
    assert mini_b["range_claims"] == 1
    assert document["counts"]["range_claims"] == 1
    assert mini_b["counts"]["listed"]["predicted"] == 3

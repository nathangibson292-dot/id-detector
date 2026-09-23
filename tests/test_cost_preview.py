"""Paid-preview regression tests: all audio/providers are local synthetic fixtures."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from id_detector import cli
from id_detector.cost import cached_mix, estimate
from id_detector.io import read_text
from id_detector.pricing import load_pricing
from id_detector.providers.base import AppConfig
from id_detector.recipes import FREE_RECIPE
from id_detector.service import PipelineOptions, run
from scripts import compare_deep, measure_refusion
from scripts.score_corpus import RunList, identities_path, load_run_list
from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff
from tests.test_followup_money_resume import _KillAfterDispatch
from tests.test_service_api import AUDIO, ROOT, SCRIPT, _request, _store


def stamps(root):
    return {
        str(p.relative_to(root)): (p.stat().st_mtime_ns, p.stat().st_size)
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.fixture
def cached(tmp_path, monkeypatch):
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    monkeypatch.setenv("IDEA_FAKE_SCRIPT", str(SCRIPT))
    work = tmp_path / "original"
    result = run(
        _request(
            _store(work, recipe=FREE_RECIPE, shazam=FakeShazamHTTP(SCRIPT)),
            run_id="baseline",
            recipe=FREE_RECIPE,
        )
    )
    assert result.status == "complete"
    source = next(work.glob("*/*/ingest/source.json"))
    key = json.loads(read_text(source))["media_key"]
    return work, key


def test_preview_uses_pricing_and_schedule_and_density():
    config = AppConfig.load(None)
    preview = estimate(3_600_000, config)
    assert preview.clips == 400
    assert preview.estimate_e6 == 400 * load_pricing().audd_usd_e6_per_request
    sparse = estimate(3_600_000, replace(config, deep_primary_density=2, max_usd_e2=0))
    assert sparse.clips == 200 and sparse.cap_e2 == 0
    assert "exceeds the cap" in sparse.render()


def test_a_part_cent_price_is_never_shown_lower_than_it_is():
    """A preview may overstate a half-cent, never understate what the owner would pay."""

    config = AppConfig.load(None)
    # 271 windows at 5,000 microUSD is $1.355 exactly: the midpoint that float rounding loses.
    preview = estimate(0, config, windows=271)
    assert preview.estimate_e6 == 1_355_000
    shown = preview.render()
    assert "estimated $1.36." in shown
    assert "$1.35." not in shown


def test_cost_cached_key_and_url_read_only(cached):
    work, key = cached
    before = stamps(work)
    mix = cached_mix(work, key)
    for target in (key, mix.source.input_url):
        result = CliRunner().invoke(cli.app, ["cost", target, "--work-root", str(work)])
        assert result.exit_code == 0, result.output
        assert "0:01:00" in result.output and "7 paid clips" in result.output
        assert "Stored scan: free" in result.output
        assert "Density 2: approximately 4" in result.output
    assert before == stamps(work)


def test_unknown_length_requires_minutes_without_creating_work(tmp_path):
    root = tmp_path / "absent"
    args = ["cost", "unknown-key", "--work-root", str(root)]
    missing = CliRunner().invoke(cli.app, args)
    assert missing.exit_code == 2 and "Length unknown" in missing.output
    supplied = CliRunner().invoke(cli.app, [*args, "--minutes", "60"])
    assert supplied.exit_code == 0 and "400 paid clips" in supplied.output
    assert not root.exists()


@pytest.mark.parametrize(
    "interactive,answer,yes,paid",
    [
        (False, "", False, False),
        (True, "n\n", False, False),
        (True, "y\n", False, True),
        (False, "", True, True),
    ],
)
def test_cli_confirmation_before_reservation(tmp_path, monkeypatch, interactive, answer, yes, paid):
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    monkeypatch.setenv("IDEA_FAKE_SCRIPT", str(SCRIPT))
    # CliRunner replaces stdin; pin just the prompt helper's interactivity read.
    original = cli._confirm_paid

    def confirm(*args):
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: interactive)
        return original(*args)

    monkeypatch.setattr(cli, "_confirm_paid", confirm)
    work = tmp_path / "scan"
    args = [
        "analyse",
        str(AUDIO),
        "--recipe",
        "deep",
        "--work-root",
        str(work),
        "--no-hints",
        "--fake-providers",
        "audd,shazam",
    ]
    result = CliRunner().invoke(cli.app, [*args, *(["--yes"] if yes else [])], input=answer)
    assert result.exit_code == (0 if paid else 130), (result.output, result.exception)
    assert "estimated $" in result.output
    if paid:
        assert list(work.rglob("invocations.jsonl"))
    else:
        assert "cancelled:" in result.output
        assert not list(work.rglob("invocations.jsonl"))
        assert not list(work.rglob("*reservations*"))
        assert not list(work.rglob("*attempts*.jsonl"))


def test_gate_is_cli_only_and_free_never_prompts(tmp_path, monkeypatch):
    seen = []
    from id_detector import pipeline

    async def capture(_url, **kwargs):
        seen.append(kwargs)
        return 0

    monkeypatch.setattr(pipeline, "run_analysis", capture)
    CliRunner().invoke(cli.app, ["analyse", str(AUDIO), "--recipe", "free"])
    assert "cli_paid_confirm" not in seen[-1]
    CliRunner().invoke(cli.app, ["analyse", str(AUDIO), "--recipe", "deep"])
    assert callable(seen[-1]["cli_paid_confirm"])
    store = _store(tmp_path, audd=FakeAudD(SCRIPT), shazam=FakeShazamHTTP(SCRIPT))
    run(_request(store, run_id="service"))
    assert "cli_paid_confirm" not in seen[-1]
    store.mode = "hosted"
    store.options = replace(store.options, cli_paid_confirm=lambda *_: pytest.fail("hosted prompt"))
    from id_detector.service import PlatformUrl

    run(_request(store, run_id="hosted", target=PlatformUrl("https://example.invalid/test")))
    assert "cli_paid_confirm" not in seen[-1]


def test_confirmation_cannot_hide_recovered_money(tmp_path):
    from id_detector.run_ledger import reservation_path
    from idea_web.jobs.worker import Worker
    from tests.idea_web.test_followup_queue_money import _crash_deep_job, _deep_options

    database, work, _queue, _job_id, dispatched = _crash_deep_job(tmp_path, "lost-reservation")
    with database.write() as connection:
        connection.execute("DELETE FROM run_reservations")
        connection.execute(
            "UPDATE analysis_runs SET checkpoints=json_remove(checkpoints, '$._reservation')"
        )
    reservation_path(work / ".attempts/lost-reservation.jsonl", "lost-reservation").unlink()
    prompts = []
    audd = FakeAudD(SCRIPT)
    options = _deep_options(audd, cli_paid_confirm=lambda *_: prompts.append("asked") or False)
    Worker(database, work, local_mode=True, options=options).run_once()
    assert prompts == []
    assert audd.calls == 0 and dispatched > 0
    with database.read() as connection:
        row = connection.execute("SELECT entry FROM run_settlements").fetchone()
    entry = json.loads(row[0])
    assert entry["status"] == "failed"
    assert entry["usd_e6_spent"] == dispatched * load_pricing().audd_usd_e6_per_request


def experiment(cached, tmp_path):
    work, key = cached
    truth = json.loads(read_text(ROOT / "tests/fixtures/corpus-mini/mini-a/ground_truth.json"))
    truth["source"]["media_key"] = key
    path = tmp_path / "truth" / "ground_truth.json"
    path.parent.mkdir()
    path.write_text(json.dumps(truth), encoding="utf-8")
    from id_detector.present.bundles import load_run_snapshot

    mix = cached_mix(work, key)
    snap = load_run_snapshot(mix.directory)
    fuse = mix.directory / snap.manifest["fuse_run"]
    run_list = tmp_path / "runs.json"
    run_list.write_text(
        json.dumps(
            {
                "recipe": "free",
                "runs": [
                    {
                        "mix_id": "tone",
                        "truth": str(path),
                        "episodes": str(fuse / "episodes.json"),
                        "identities": str(fuse / "presentation-identities.json"),
                        "media_key": key,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return SimpleNamespace(
        work_root=work,
        scan_root=tmp_path / "experiment",
        config=tmp_path / "none.toml",
        run_list=run_list,
        budget=100_000,
        density=1,
        mix=[],
        spend=False,
    )


def test_comparison_budget_refusal_and_default_dry_run(cached, tmp_path, capsys):
    args = experiment(cached, tmp_path)
    options = PipelineOptions(
        project_root=ROOT,
        no_hints=True,
        paid_scan_adapters={"audd": FakeAudD(SCRIPT)},
        shazam_http_client=FakeShazamHTTP(SCRIPT),
        paid_sleep=no_backoff,
    )
    before = stamps(args.work_root)
    args.budget = 1
    args.spend = True
    assert compare_deep.execute(args, options=options) == 4
    assert "REFUSED" in capsys.readouterr().out
    assert not args.scan_root.exists()
    args.budget = 100_000
    original = args.run_list.read_bytes()
    invalid = json.loads(original)
    invalid["runs"][0]["media_key"] = "a" * 64
    args.run_list.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(ValueError):
        compare_deep.execute(args, options=options)
    assert not args.scan_root.exists()
    args.run_list.write_bytes(original)
    args.spend = False
    assert (
        compare_deep.main(
            [
                "--run-list",
                str(args.run_list),
                "--work-root",
                str(args.work_root),
                "--scan-root",
                str(args.scan_root),
                "--budget",
                "0.10",
                "--fake-providers",
                "audd,shazam",
            ]
        )
        == 0
    )
    assert "DRY RUN" in capsys.readouterr().out
    assert not args.scan_root.exists()
    assert stamps(args.work_root) == before


@pytest.mark.parametrize("cancel", [False, True])
def test_comparison_resume_after_paid_interruption(cached, tmp_path, capsys, cancel):
    args = experiment(cached, tmp_path)
    args.spend = True
    before = stamps(args.work_root)
    killer = _KillAfterDispatch(FakeAudD(SCRIPT), kill_on=3)

    def options(audd):
        return PipelineOptions(
            project_root=ROOT,
            no_hints=True,
            paid_scan_adapters={"audd": audd},
            shazam_http_client=FakeShazamHTTP(SCRIPT),
            paid_sleep=no_backoff,
        )

    compare_deep.execute(args, options=options(killer))
    import sqlite3

    db = args.scan_root / ".idea/app.db"
    with sqlite3.connect(db) as connection:
        dispatched = connection.execute(
            "SELECT COUNT(*) FROM provider_attempt_events "
            "WHERE provider='audd' AND state='dispatched'"
        ).fetchone()[0]
    assert dispatched > 0
    resumed = FakeAudD(SCRIPT)
    args.budget = 1
    assert compare_deep.execute(args, options=options(resumed)) == 4
    assert resumed.calls == 0
    args.budget = 100_000
    if cancel:
        from idea_web.database import Database
        from idea_web.jobs.worker import JobQueue

        queue = JobQueue(Database(db), local_mode=True)
        with sqlite3.connect(db) as connection:
            job_id = connection.execute("SELECT id FROM jobs").fetchone()[0]
        queue.request_cancel(job_id)
    compare_deep.execute(args, options=options(resumed))
    assert resumed.calls == (0 if cancel else 7 - dispatched)
    output = capsys.readouterr().out
    assert ("no usable Deep result" if cancel else "Pooled | Deep") in output
    again = FakeAudD(SCRIPT)
    compare_deep.execute(args, options=options(again))
    assert again.calls == 0
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM run_settlements").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert before == stamps(args.work_root)


def test_comparison_table_and_named_gains(tmp_path):
    from id_detector.contracts import EpisodesFile

    deep = load_run_list(ROOT / "tests/fixtures/corpus-mini/run-list.json")
    runs = []
    for entry in deep.runs:
        episodes = EpisodesFile.model_validate_json(read_text(entry.episodes))
        identities = identities_path(entry, episodes)
        original = episodes.episodes
        episodes = episodes.model_copy(update={"episodes": original[1:]})
        path = tmp_path / entry.mix_id / "episodes.json"
        path.parent.mkdir()
        path.write_text(episodes.model_dump_json(), encoding="utf-8")
        runs.append(entry.model_copy(update={"episodes": path, "identities": identities}))
    output = compare_deep.comparison(RunList(recipe="free", runs=runs), deep)
    assert "Pooled | Deep | 66.7% | 57.1% | 80.0%" in output
    assert "Pooled | Free | 33.3% | 40.0% | 66.7%" in output
    assert "Mini Artist - Alpha" in output
    assert "Mini Artist - Theta" in output


def test_refusion_measurement_read_only(cached, tmp_path):
    work, _ = cached
    args = experiment(cached, tmp_path)
    before = stamps(work)
    output = measure_refusion.measure(work, args.run_list, AppConfig.load(None))
    assert "SKIPPED" not in output
    assert "Total |" in output and "Supported episodes" in output
    assert "->" in output
    assert "Before: 1 truth mixes" in output and "After: 1 truth mixes" in output
    assert before == stamps(work)

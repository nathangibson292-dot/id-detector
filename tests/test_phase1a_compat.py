"""Phase 1a-ii: offline compatibility, identity, delta work and source identity gate."""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from id_detector import cli, compat, pipeline
from id_detector.compat import (
    AnalysisInputs,
    RunRequest,
    digest,
    find_result,
    hints_snapshot,
    index_identity,
    serves,
)
from id_detector.config_template import CONFIG_TEMPLATE
from id_detector.contracts import GENERATED_BY, SCHEMA_VERSION, AcquireFile, HintRecord
from id_detector.ingest import SourceChanged
from id_detector.io import read_bytes, read_text
from id_detector.present.bundles import read_bundle_manifest, result_dir, shown_result_dir
from id_detector.pricing import load_pricing
from id_detector.providers.base import AppConfig
from id_detector.recipes import RECIPES, get_recipe
from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff
from tests.test_phase0a_status import AUDIO, SCRIPTS


def request(name="free", density=1, **kwargs):
    recipe = get_recipe(name, primary_density=density)
    inputs = AnalysisInputs("a" * 64, recipe.recipe_id, "local", "public", digest([]))
    return RunRequest(inputs, recipe, **kwargs)


def stored(req, status="complete"):
    return {**req.metadata(), "status": status, "achieved": req.recipe.name}


@pytest.mark.parametrize(
    "old,new,flag,expected",
    [
        (("free", 1), ("free", 1), False, True),
        (("free", 1), ("deep", 1), False, False),
        (("deep", 1), ("deep", 2), False, True),
        (("deep", 2), ("deep", 1), False, False),
        (("deep", 1), ("deep", 1), False, True),
        (("deep", 2), ("deep", 2), False, True),
        (("deep", 1), ("free", 1), False, False),
        (("deep", 2), ("free", 1), False, False),
        (("deep", 1), ("free", 1), True, True),
        (("deep", 2), ("free", 1), True, True),
    ],
)
def test_serves_table(old, new, flag, expected):
    assert serves(stored(request(*old)), request(*new), serve_free_from_deep=flag) == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("algorithm_version", "targeting:2,fusion:3"),
        ("adapter_versions", {"shazam": 2, "audd_clip": 2}),
        ("compat_version", 2),
        ("analysis_key", "b" * 64),
        ("tenant_scope", "user:someone"),
        ("non_recipe_key", "b" * 64),
    ],
)
def test_mismatches_never_serve(field, value):
    req = request()
    assert not serves({**stored(req), field: value}, req)


@pytest.mark.parametrize("status", ["partial", "degraded"])
@pytest.mark.parametrize("accept", [False, True])
def test_hosted_never_serves_partial_or_degraded(status, accept):
    req = request(local=False, accept_degraded=accept)
    assert not serves(stored(req, status), req)


def test_local_degraded_is_opt_in_and_partial_never_serves():
    req = request()
    assert not serves(stored(req, "degraded"), req)
    assert serves(stored(req, "degraded"), replace(req, accept_degraded=True))
    assert not serves(stored(req, "partial"), replace(req, accept_degraded=True))


def test_private_scopes_and_canonical_key_inputs():
    req = request()
    inputs = replace(req.inputs, source_kind="upload", tenant_scope="user:one")
    private = replace(req, inputs=inputs)
    assert serves(stored(private), private)
    assert not serves(
        stored(private), replace(private, inputs=replace(inputs, tenant_scope="user:two"))
    )
    assert inputs.analysis_key == digest(vars(inputs))
    for name in vars(inputs):
        value = (
            "local"
            if name == "source_kind"
            else "user:two"
            if name == "tenant_scope"
            else "changed"
        )
        altered = replace(inputs, **{name: value})
        assert altered.analysis_key != inputs.analysis_key
        assert (altered.non_recipe_key == inputs.non_recipe_key) == (name == "recipe_id")
    with pytest.raises(ValueError, match="owner scope"):
        replace(inputs, tenant_scope="public")


def run(work, recipe="free", density=1, *, audio=AUDIO, config=None, **kwargs):
    audd = FakeAudD(SCRIPTS / "phase1a-compat.json")
    shazam = FakeShazamHTTP(SCRIPTS / "phase1a-compat.json")
    paths = []
    code = asyncio.run(
        cli._analyse(
            str(audio),
            work_root=work,
            print_raw=False,
            refresh=kwargs.pop("refresh", False),
            max_requests=100,
            tracklist=kwargs.pop("tracklist", None),
            no_hints=kwargs.pop("no_hints", True),
            max_generations=kwargs.pop("max_generations", 0),
            novelty=False,
            recipe=get_recipe(recipe, primary_density=density),
            app_config=config
            or AppConfig(
                transforms_policy="off",
                recognise_concurrency=1,
                shazam_requests_per_minute=1_000_000,
                audd_requests_per_minute=1_000_000,
            ),
            paid_scan_adapters={"audd": audd},
            shazam_http_client=shazam,
            paid_sleep=no_backoff,
            result_paths=paths,
            **kwargs,
        )
    )
    return code, audd, shazam, paths


def test_free_to_deep_primary_only_and_frozen_secondary(tmp_path, monkeypatch):
    fused_inputs = []
    fuse = pipeline.run_generation_loop

    async def record_fuse(**kwargs):
        fused_inputs.append(kwargs)
        return await fuse(**kwargs)

    monkeypatch.setattr(pipeline, "run_generation_loop", record_fuse)
    work = tmp_path / "work"
    _, _, _, free_paths = run(work)
    free = free_paths[0]
    before = read_bytes(free / "manifest.json")
    code, audd, shazam, paths = run(work, "deep")
    assert code == 0 and audd.calls == 7 and shazam.requests == 0
    deep = paths[0]
    media = deep.parents[2]
    entries = [json.loads(line) for line in read_text(media / "invocations.jsonl").splitlines()]
    entry = entries[-1]
    # Primary only: seven AudD windows at 5,000 microdollars, reserved with the 5 % headroom,
    # every unit spent, the 1,750 remainder released, one terminal entry for this run only.
    assert entry["usd_e6_reserved"] == 36_750 and entry["usd_e6_spent"] == 7 * 5_000
    assert entry["usd_e6_reserved"] - entry["usd_e6_spent"] == 1_750
    assert entry["usd_e2_reserved"] == 4 and entry["usd_e2_spent"] == 4
    terminal = [item for item in entries if item["invocation_id"] == entry["invocation_id"]]
    assert len(terminal) == 1 and terminal[0]["status"] == "complete"
    assert entry["counts"]["secondary_allocated"] == 0
    assert entry["counts"]["secondary_reused"] == 7
    manifest = read_bundle_manifest(deep)
    assert manifest["status"] == "complete"
    assert entry["analysis_key"] == manifest["analysis_key"]
    assert json.loads(read_text(deep / "tracklist.json"))["analysis_key"] == entry["analysis_key"]
    assert read_bytes(free / "manifest.json") == before
    frozen = json.loads(read_text(media / manifest["fuse_run"] / "episodes.json"))
    assert frozen
    assert {item.provider for item in fused_inputs[-1]["observations"]} == {"audd"}
    assert {item.provider for item in fused_inputs[-1]["extra_observations"]} == {"shazam"}
    assert len(fused_inputs[-1]["extra_observations"]) == 7


def test_lookup_serves_dense_deep_without_providers_or_reservation(tmp_path):
    work = tmp_path / "work"
    _, _, _, paths = run(work, "deep")
    code, audd, shazam, cached = run(work, "deep", 2, config=AppConfig(max_usd_e2=0))
    assert code == 0 and audd.calls == shazam.requests == 0
    assert cached == paths
    manifest = read_bundle_manifest(paths[0])
    req = request("deep", 2)
    req = replace(
        req,
        inputs=AnalysisInputs(
            **{**manifest["compatibility"]["analysis_inputs"], "recipe_id": req.recipe.recipe_id}
        ),
    )
    assert find_result(paths[0].parents[2], req) == paths[0]


def test_lookup_sparse_does_not_serve_dense(tmp_path):
    work = tmp_path / "work"
    _, _, _, sparse = run(work, "deep", 2)
    code, _, _, dense = run(work, "deep")
    assert code == 0 and dense != sparse
    assert read_bundle_manifest(dense[0])["compatibility"]["primary_density"] == 1


def test_lookup_free_from_deep_flag(tmp_path):
    work = tmp_path / "work"
    _, _, _, deep = run(work, "deep")
    _, audd, shazam, cached = run(work, config=AppConfig(serve_free_from_deep=True))
    assert cached == deep and audd.calls == shazam.requests == 0
    _, _, _, free = run(work)
    assert free != deep and read_bundle_manifest(free[0])["achieved"] == "free"


def test_altered_local_source_exit_5_preserves_results(tmp_path, monkeypatch):
    audio = tmp_path / "mix.wav"
    shutil.copyfile(AUDIO, audio)
    work = tmp_path / "work"
    _, _, _, paths = run(work, audio=audio)
    directory = paths[0]
    preserved = {p: read_bytes(p) for p in directory.rglob("*") if p.is_file()}
    audio.write_bytes(b"different source bytes")
    with pytest.raises(SourceChanged):
        cli._load_cached(work, str(audio))
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    monkeypatch.setenv("IDEA_FAKE_SCRIPT", str(SCRIPTS / "phase1a-compat.json"))
    result = CliRunner().invoke(
        cli.app,
        [
            "analyse",
            str(audio),
            "--work-root",
            str(work),
            "--recipe",
            "free",
            "--no-hints",
            "--fake-providers",
            "audd,shazam",
        ],
    )
    assert result.exit_code == 5, result.output
    media = directory.parents[2]
    entry = json.loads(read_text(media / "invocations.jsonl").splitlines()[-1])
    assert entry["status"] == "source_changed" and entry["usd_e6_spent"] == 0
    assert entry["bundle_id"] is None
    assert all(read_bytes(p) == body for p, body in preserved.items())
    assert shown_result_dir(media) == directory


def test_hints_and_index_id_are_stable_and_content_sensitive(tmp_path):
    hint = HintRecord.model_validate_json(read_text(Path("tests/golden/hint.json")))
    assert hints_snapshot([hint]) == hints_snapshot([hint.model_copy()])
    assert hints_snapshot([]) != hints_snapshot([hint])
    altered = hint.model_copy(update={"id": "b" * 64})
    assert hints_snapshot([hint]) != hints_snapshot([altered])
    assert hints_snapshot([hint, altered]) == hints_snapshot([altered, hint])
    index = tmp_path / "private/index.json"
    index.parent.mkdir()
    index.write_text("{}")
    old = index_identity(tmp_path, "private")
    assert old == index_identity(tmp_path, "private")
    index.write_text('{"tracks": [1]}')
    assert old != index_identity(tmp_path, "private")


def _pricing(tmp_path, **overrides):
    lines = read_text(Path("pricing.toml")).splitlines()
    for key, value in overrides.items():
        lines = [f"{key} = {value}" if line.startswith(f"{key} ") else line for line in lines]
    path = tmp_path / "pricing.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_launch_flag_and_compat_version_come_from_pricing_only(tmp_path):
    idea = tmp_path / "idea.toml"
    idea.write_text("[cache]\npositive_max_age_days = 180\n")
    for value, expected in (("false", False), ("true", True)):
        authority = _pricing(tmp_path, serve_free_from_deep=value)
        assert AppConfig.load(idea, pricing_path=authority).serve_free_from_deep is expected
        assert AppConfig.load(None, pricing_path=authority).serve_free_from_deep is expected
    # The owner's config can neither set the launch flag nor shadow the authority.
    idea.write_text("[cache]\nserve_free_from_deep = true\n")
    with pytest.raises(ValueError, match="pricing.toml"):
        AppConfig.load(idea, pricing_path=_pricing(tmp_path, serve_free_from_deep="false"))
    assert "serve_free_from_deep" not in CONFIG_TEMPLATE
    assert load_pricing().compat_version == compat.COMPAT_VERSION


def test_compat_version_bump_retires_every_stored_result(monkeypatch):
    req = request()
    old = stored(req)
    monkeypatch.setattr(compat, "COMPAT_VERSION", compat.COMPAT_VERSION + 1)
    assert not serves(old, req, serve_free_from_deep=True)
    assert not serves(old, request("deep"), serve_free_from_deep=True)


def test_recipe_version_bumps_invalidate_serving(monkeypatch):
    for name in ("free", "deep"):
        req = request(name)
        old = stored(req)
        for changed in (
            replace(req.recipe, algorithm_version="fusion:99"),
            replace(req.recipe, adapter_versions={"shazam": 99}),
        ):
            monkeypatch.setattr(compat, "RECIPES", {**RECIPES, name: changed})
            new = RunRequest(replace(req.inputs, recipe_id=changed.recipe_id), changed)
            assert not serves(old, new, serve_free_from_deep=True)
            assert not serves(old, request(name), serve_free_from_deep=True)


def test_results_are_stamped_with_what_they_ran_and_deep_bumps_spare_free(monkeypatch):
    free_stamp = request("free").metadata()
    assert free_stamp["algorithm_version"] == "fusion:3"
    assert free_stamp["adapter_versions"] == {"shazam": 1}
    free_result, deep_result = stored(request("free")), stored(request("deep"))
    bumped = replace(get_recipe("deep"), algorithm_version="targeting:2,fusion:3")
    monkeypatch.setattr(compat, "RECIPES", {**RECIPES, "deep": bumped})
    # A Deep-only bump retires Deep results and leaves Free results -- and their reuse as Deep
    # evidence -- untouched: a Free result never ran the component that moved.
    assert not serves(deep_result, request("deep"))
    assert serves(free_result, request("free"))
    assert compat.same_inputs(free_result, request("deep"))


def test_cached_analysis_needs_no_original_decode_or_provider(tmp_path, monkeypatch):
    work = tmp_path / "work"
    _, _, _, paths = run(work)
    cached = cli._load_cached(work, str(AUDIO))
    cached.original_path.unlink()

    async def forbidden(*args, **kwargs):
        pytest.fail("compatible lookup must precede re-fetch/decode")

    monkeypatch.setattr(cli, "ingest", forbidden)
    monkeypatch.setattr(pipeline, "ingest", forbidden)
    monkeypatch.setattr(pipeline, "decode", forbidden)
    code, audd, shazam, reused = run(work)
    assert code == 0 and reused == paths and audd.calls == shazam.requests == 0


def test_used_hints_and_manual_scope_control_lookup(tmp_path, monkeypatch):
    from types import SimpleNamespace

    work = tmp_path / "work"
    tracklist = tmp_path / "tracks.txt"
    tracklist.write_text("00:00 Example - One")
    hint = HintRecord.model_validate_json(read_text(Path("tests/golden/hint.json")))
    used = [hint]

    async def fake_hints(**kwargs):
        path = kwargs["media_dir"] / "hints/used.jsonl"
        path.parent.mkdir(exist_ok=True)
        path.write_text("\n".join(item.model_dump_json() for item in used))
        return SimpleNamespace(hints=tuple(used), hints_path=path)

    monkeypatch.setattr(cli, "run_hints", fake_hints)
    monkeypatch.setattr(pipeline, "run_hints", fake_hints)
    _, _, _, first = run(work, tracklist=tracklist, no_hints=False)
    manifest = read_bundle_manifest(first[0])
    assert manifest["compatibility"]["tenant_scope"] == "user:local"
    _, _, shazam, repeated = run(work, tracklist=tracklist, no_hints=False)
    assert repeated == first and shazam.requests == 0
    used.clear()
    _, _, _, changed = run(work, tracklist=tracklist, no_hints=False)
    assert read_bundle_manifest(changed[0])["analysis_key"] != manifest["analysis_key"]
    tracklist.write_text("00:00 Example - Two")
    _, _, _, edited = run(work, tracklist=tracklist, no_hints=False)
    assert edited != changed


def test_upload_scope_does_not_reuse_other_owner(tmp_path):
    work = tmp_path / "work"
    _, _, _, first = run(work, source_kind="upload", tenant_scope="user:one")
    _, _, _, second = run(work, source_kind="upload", tenant_scope="user:two")
    assert first != second
    assert (
        read_bundle_manifest(first[0])["analysis_key"]
        != read_bundle_manifest(second[0])["analysis_key"]
    )


@pytest.mark.parametrize("profile,name", [(None, "free"), ("max_accuracy", "deep")])
def test_web_runner_passes_recipe_and_delivers_selected_bundle(
    tmp_path, monkeypatch, profile, name
):
    from types import SimpleNamespace

    from id_detector.webapp.runner import make_pipeline_runner

    work = tmp_path / "work"
    _, _, _, paths = run(work)
    selected = paths[0]
    calls = []

    async def fake_analyse(*args, **kwargs):
        calls.append(kwargs)
        kwargs["result_paths"].append(selected)
        return 0

    monkeypatch.setattr(pipeline, "run_analysis", fake_analyse)
    results = []
    outcomes = []
    context = SimpleNamespace(
        # A job-driven run always carries its durable run id; the runner fails closed without one.
        run_id=f"web-runner-{name}",
        target=str(AUDIO),
        build_index=False,
        profile=profile,
        known_tracklist=None,
        acquire=False,
        cancel_token=None,
        started_at=None,
        progress=lambda *args: None,
        set_result=results.append,
        set_outcome=lambda **fields: outcomes.append(fields),
    )
    make_pipeline_runner(work, config_path=tmp_path / "missing.toml")(context)
    assert calls[0]["recipe"].name == name
    assert "primary_engine" not in calls[0]
    assert results == [selected / "index.html"]
    # The run's frozen journal outcome reaches the job, so the UI never has to guess the cost.
    assert len(outcomes) == 1 and outcomes[0]["spend_known"] is True
    assert outcomes[0]["usd_e2_spent"] == 0


def test_refetched_different_bytes_exit_5_before_publication(tmp_path, monkeypatch):
    import importlib

    ingestion = importlib.import_module("id_detector.ingest")
    work = tmp_path / "work"
    _, _, _, paths = run(work)
    retained = cli._load_cached(work, str(AUDIO))
    before = read_bytes(paths[0] / "manifest.json")
    monkeypatch.setattr(cli, "_load_cached", lambda *args: retained)
    monkeypatch.setattr(pipeline, "_load_cached", lambda *args: retained)
    monkeypatch.setattr(ingestion, "_load_cached", lambda *args: retained)
    monkeypatch.setattr(ingestion, "_load_ingest_cached", lambda *args: None)

    async def fake_download(command, **kwargs):
        output = Path(command[command.index("-o") + 1]).parent
        (output / "asset.wav").write_bytes(b"refetched different bytes")
        (output / "asset.info.json").write_text('{"extractor": "other"}')

    monkeypatch.setattr(ingestion, "run_process", fake_download)
    code, audd, shazam, results = run(work, audio="https://example.invalid/fixture", refresh=True)
    assert code == 5 and audd.calls == shazam.requests == 0 and not results
    assert read_bytes(paths[0] / "manifest.json") == before
    entry = json.loads(read_text(retained.media_dir / "invocations.jsonl").splitlines()[-1])
    assert entry["status"] == "source_changed" and entry["exit_code"] == 5


def test_changed_retained_bytes_exit_5_when_reused_for_new_work(tmp_path):
    work = tmp_path / "work"
    _, _, _, paths = run(work)
    retained = cli._load_cached(work, str(AUDIO))
    retained.original_path.write_bytes(b"changed retained audio")
    code, audd, shazam, results = run(work, "deep")
    assert code == 5 and audd.calls == shazam.requests == 0 and not results
    assert read_bundle_manifest(paths[0])["status"] == "complete"
    entry = json.loads(read_text(retained.media_dir / "invocations.jsonl").splitlines()[-1])
    assert entry["status"] == "source_changed"


def test_second_alias_with_identical_bytes_pays_nothing(tmp_path):
    work = tmp_path / "work"
    first, second = tmp_path / "one/mix.wav", tmp_path / "two/mix.wav"
    for path in (first, second):
        path.parent.mkdir()
        shutil.copyfile(AUDIO, path)
    _, audd, _, deep = run(work, "deep", audio=first)
    assert audd.calls == 7
    # Identical bytes under a second URL land in a second directory but share the analysis key:
    # a zero cap proves nothing was reserved, and the stored bundle is served as it stands.
    code, audd, shazam, served = run(work, "deep", audio=second, config=AppConfig(max_usd_e2=0))
    assert code == 0 and audd.calls == 0 and shazam.requests == 0
    assert served == deep
    directories = sorted(p for p in work.glob("*/*") if p.is_dir())
    assert len(directories) == 2
    assert {p.name for p in directories} == {deep[0].parents[2].name}


def test_web_acquisition_republishes_the_selected_bundle(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from id_detector.webapp.runner import make_pipeline_runner

    work = tmp_path / "work"
    _, _, _, free_paths = run(work)
    _, _, _, deep_paths = run(work, "deep")
    selected, media = free_paths[0], free_paths[0].parents[2]
    # present/current names the newer Deep run, so acquisition must not silently follow it.
    assert result_dir(media) == deep_paths[0]

    async def fake_analyse(*args, **kwargs):
        kwargs["result_paths"].append(selected)
        return 0

    acquired = AcquireFile(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        media_key=read_bundle_manifest(selected)["media_key"],
        generation=0,
        episodes=[],
    )

    async def fake_enrich(**kwargs):
        return SimpleNamespace(
            record=acquired,
            path=media / "enrich/acquire.json",
            counts={
                "episodes": 0,
                "direct_links_total": 0,
                "direct_links_by_source": {},
                "free_download_flags": 0,
                "gate_links": 0,
                "buy_links": 0,
                "search_only_rows": 0,
            },
        )

    monkeypatch.setattr(pipeline, "run_analysis", fake_analyse)
    monkeypatch.setattr(cli, "enrich_media_dir", fake_enrich)
    results = []
    context = SimpleNamespace(
        run_id="web-acquisition-job",  # the job's durable run id (the runner fails closed without)
        target=str(AUDIO),
        build_index=False,
        profile=None,
        known_tracklist=None,
        acquire=True,
        cancel_token=None,
        check_cancel=lambda: None,
        started_at=None,
        progress=lambda *args: None,
        set_result=results.append,
        set_outcome=lambda **fields: None,
    )
    make_pipeline_runner(work, config_path=tmp_path / "missing.toml")(context)
    delivered = read_bundle_manifest(results[0].parent)
    # The acquisition revision belongs to the selected Free run, not to whichever run is newest,
    # and the job delivers that revision.
    assert delivered["run_id"] == read_bundle_manifest(selected)["run_id"]
    assert delivered["achieved"] == "free"
    assert (results[0].parent / "acquire.json").is_file()
    assert not (deep_paths[0] / "acquire.json").is_file()
    assert read_bundle_manifest(deep_paths[0])["run_id"] != delivered["run_id"]

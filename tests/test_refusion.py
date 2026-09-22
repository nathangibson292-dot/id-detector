"""Reaching existing mixes: a result stored under an older fusion version is RE-FUSED offline.

The scenario is the gapped-phantom shape of ``tests/test_accuracy_fixes.py`` played through the
real pipeline with the scripted fake engines: a "famous" label (tone A) heard for two runs of
windows with a 114 s hole between them, and two real tracks (tones B and C) inside that hole.
Under the ``fusion:2`` rules the phantom's hull buries both; under today's rules both are listed.

Every test starts from a result really produced and stamped under ``fusion:2`` (the old rules are
switched back on for that first run only) and then asks the NORMAL paths — ``analyse`` and the
server's start-up pass — to bring it up to date.  The fakes count every request they receive.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import wave
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from id_detector import cli, compat, pipeline
from id_detector.compat import (
    find_result,
    find_stale_result,
    fusion_stale_only,
    same_evidence,
    serves,
)
from id_detector.fuse import episodes as fusion_rules
from id_detector.io import native_path, read_text
from id_detector.present.bundles import read_bundle_manifest, result_dir
from id_detector.providers.base import AppConfig
from id_detector.recipes import DEEP_RECIPE, FREE_RECIPE, FUSION_VERSION, fusion_component
from id_detector.refusion import (
    NotRebuildable,
    load_fusion_inputs,
    refuse_stale_result,
    stored_fusion_version,
)
from id_detector.retention import collect
from idea_web.server import refresh_stale_pages
from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff
from tests.test_phase1a_compat import request, stored

OLD_FREE = replace(FREE_RECIPE, algorithm_version="fusion:2")
OLD_DEEP = replace(DEEP_RECIPE, algorithm_version="targeting:1,fusion:2")
PHANTOM, FIRST, SECOND = "Tone 440", "Tone 554", "Tone 659"
#: Window ordinals at the 9 s hop: the phantom 0-13 and 28-30 (a 114 s hole), the real tracks in it.
LABELS = {
    **{index: "A" for index in (*range(14), 28, 29, 30)},
    **{index: "B" for index in range(16, 21)},
    **{index: "C" for index in range(22, 27)},
}
SECTION = {
    "default": "no_match",
    "windows": {str(index): "match" for index in LABELS},
    "labels": {str(index): letter for index, letter in LABELS.items()},
}
SCRIPT = {"shazam": SECTION, "audd": SECTION}
CONFIG = AppConfig(
    transforms_policy="off",
    recognise_concurrency=1,
    shazam_requests_per_minute=1_000_000,
    audd_requests_per_minute=1_000_000,
)


def _audio(path: Path, seconds: int = 300) -> Path:
    """A slow sweep, so no two windows hold the same bytes (the recognition cache is keyed by
    window content, and identical windows would be answered once)."""

    rate = 8_000
    if seconds not in _FRAMES:  # synthesised once per session, not once per test
        _FRAMES[seconds] = b"".join(
            struct.pack("<h", int(9_000 * math.sin(2 * math.pi * (200 + n / rate) * n / rate)))
            for n in range(rate * seconds)
        )
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(_FRAMES[seconds])
    return path


_FRAMES: dict[int, bytes] = {}


def _analyse(work: Path, audio: Path, recipe, *, config: AppConfig = CONFIG, **kwargs):
    audd, shazam, paths = FakeAudD(SCRIPT), FakeShazamHTTP(SCRIPT), []
    code = asyncio.run(
        cli._analyse(
            str(audio),
            work_root=work,
            print_raw=False,
            refresh=kwargs.pop("refresh", False),
            max_requests=1_000,
            tracklist=None,
            no_hints=kwargs.pop("no_hints", True),
            max_generations=0,
            novelty=False,
            recipe=recipe,
            app_config=config,
            paid_scan_adapters={"audd": audd},
            shazam_http_client=shazam,
            paid_sleep=no_backoff,
            result_paths=paths,
        )
    )
    return code, audd, shazam, paths


def _stored_under_fusion_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recipe=OLD_FREE, **kwargs
):
    """A result really produced by the old rules and stamped ``fusion:2``."""

    work, audio = tmp_path / "work", _audio(tmp_path / "mix.wav")
    with monkeypatch.context() as old_rules:
        old_rules.setattr(fusion_rules, "LIKELY_CORE_RUN_MS", 0)
        old_rules.setattr(fusion_rules, "COVER_RUN_GAP_MS", 10**12)
        code, audd, shazam, paths = _analyse(work, audio, recipe, **kwargs)
    assert code == 0 and len(paths) == 1
    manifest = read_bundle_manifest(paths[0])
    assert fusion_component(manifest["compatibility"]["algorithm_version"]) == 2
    return work, audio, paths[0], (audd, shazam)


def _titles(bundle: Path) -> set[str]:
    entries = json.loads(read_text(bundle / "tracklist.json"))["entries"]
    return {entry["title"] for entry in entries if entry["kind"] == "track"}


def _tree(*roots: Path) -> dict[str, str]:
    return {
        path.as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for root in roots
        for path in sorted(root.rglob("*") if root.is_dir() else [root])
        if path.is_file() and "/runs/refuse" not in path.as_posix()  # the re-fusion's own run
    }


# --------------------------------------------------------------------------------------------------
# The compatibility contract
# --------------------------------------------------------------------------------------------------
def test_a_fusion_2_result_is_stale_for_serving_and_reusable_for_recognition(monkeypatch) -> None:
    assert FUSION_VERSION == 4
    assert FREE_RECIPE.algorithm_version == "fusion:4"
    assert DEEP_RECIPE.algorithm_version == "targeting:1,fusion:4"
    for name, old in (("free", OLD_FREE), ("deep", OLD_DEEP)):
        req = request(name)
        was = stored(compat.RunRequest(replace(req.inputs, recipe_id=old.recipe_id), old))
        assert not serves(was, req, serve_free_from_deep=True)  # never served as it is
        assert fusion_stale_only(was)
        assert compat.serves_once_refused(was, req)
        # ... and only a FUSION step behind: anything else that moved is a different analysis.
        for field, value in (
            ("algorithm_version", was["algorithm_version"].replace("fusion:2", "fusion:9")),
            ("algorithm_version", "targeting:0,fusion:2" if name == "deep" else "fusion:two"),
            ("adapter_versions", {"shazam": 99}),
            ("recipe_name", "unknown"),
        ):
            assert not fusion_stale_only({**was, field: value})
            assert not compat.serves_once_refused({**was, field: value}, req)
        assert not compat.serves_once_refused({**was, "non_recipe_key": "b" * 64}, req)
        assert not compat.serves_once_refused({**was, "status": "partial"}, req)
    # A current result is not "stale", and a Deep request may still reuse an old Free sweep.
    assert not fusion_stale_only(stored(request("free")))
    old_free = stored(compat.RunRequest(request("free").inputs, OLD_FREE))
    assert same_evidence(old_free, request("deep")) and not compat.same_inputs(
        old_free, request("deep")
    )


# --------------------------------------------------------------------------------------------------
# analyse: re-fused from the recorded observations, zero requests
# --------------------------------------------------------------------------------------------------
def test_analyse_re_fuses_a_fusion_2_result_without_a_single_provider_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, audio, old_bundle, (_, first_shazam) = _stored_under_fusion_2(tmp_path, monkeypatch)
    assert first_shazam.requests > 30  # the sweep that must never be repeated
    assert _titles(old_bundle) == {PHANTOM}  # the hull buried both real tracks
    media = old_bundle.parents[2]
    old_run = media / read_bundle_manifest(old_bundle)["fuse_run"]
    kept = _tree(old_bundle, old_run, media / "recognise", media / "windows", media / "fuse")

    async def forbidden(*args, **kwargs):
        pytest.fail("a re-fusion must not fetch or decode anything")

    monkeypatch.setattr(pipeline, "decode", forbidden)
    code, audd, shazam, paths = _analyse(work, audio, FREE_RECIPE)
    assert code == 0 and audd.calls == 0 and shazam.requests == 0
    (new_bundle,) = paths
    assert new_bundle != old_bundle
    assert _titles(new_bundle) == {PHANTOM, FIRST, SECOND}  # what the three fixes list
    manifest = read_bundle_manifest(new_bundle)
    assert manifest["compatibility"]["algorithm_version"] == "fusion:4"
    assert manifest["refusion"] == {
        "source_run_id": read_bundle_manifest(old_bundle)["run_id"],
        "source_bundle": old_bundle.name,
        "fusion_version": 4,
    }
    # Superseded, not corrupted or orphaned: every old byte is where it was (the recognition
    # evidence and the mutable fuse tree included) and the pointer has moved on.
    roots = (old_bundle, old_run, media / "recognise", media / "windows", media / "fuse")
    assert _tree(*roots) == kept
    assert result_dir(media) == new_bundle
    assert read_text(media / "present" / "current").strip() == new_bundle.name
    provenance = json.loads(read_text(media / manifest["fuse_run"] / "refusion.json"))
    assert provenance["source_fusion_version"] == 2 and provenance["fusion_version"] == 4
    assert any(key.startswith("recognise/") for key in provenance["inputs"])
    # The next identical request is an ordinary cache hit on the re-fused bundle — the stale
    # bundle still lying beside it is no longer what the offline lookup names.
    code, audd, shazam, again = _analyse(work, audio, FREE_RECIPE)
    assert code == 0 and again == [new_bundle] and audd.calls == 0 and shazam.requests == 0


def test_a_later_fusion_bump_follows_a_prior_refusion_to_its_original_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-fused run seals outputs, not a fake copy of its source run's completion sidecars."""

    work, audio, _old_bundle, _ = _stored_under_fusion_2(tmp_path, monkeypatch)
    code, _audd, _shazam, paths = _analyse(work, audio, FREE_RECIPE)
    assert code == 0
    (prior_refusion,) = paths
    prior_manifest = read_bundle_manifest(prior_refusion)
    assert prior_manifest is not None and prior_manifest["refusion"]["source_bundle"] is not None
    path = prior_refusion / "manifest.json"
    stored = json.loads(read_text(path))
    stored["compatibility"]["algorithm_version"] = "fusion:3"
    stored["refusion"]["fusion_version"] = 3
    path.write_text(json.dumps(stored), encoding="utf-8")

    async def forbidden(*args, **kwargs):
        pytest.fail("a later re-fusion must still be wholly offline")

    monkeypatch.setattr(pipeline, "decode", forbidden)
    code, audd, shazam, paths = _analyse(work, audio, FREE_RECIPE)
    assert code == 0 and audd.calls == 0 and shazam.requests == 0
    (new_bundle,) = paths
    manifest = read_bundle_manifest(new_bundle)
    assert new_bundle != prior_refusion
    assert manifest is not None and manifest["refusion"]["source_bundle"] == prior_refusion.name
    assert _titles(new_bundle) == {PHANTOM, FIRST, SECOND}


def test_pipeline_re_fuses_stale_result_found_through_a_sibling_source_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request owns the canonical media lock already; the stored alias must reuse it."""

    work, audio, old_bundle, _ = _stored_under_fusion_2(tmp_path, monkeypatch)
    sibling = tmp_path / "same-bytes-different-source.wav"
    shutil.copy2(audio, sibling)

    code, audd, shazam, paths = _analyse(work, sibling, FREE_RECIPE)

    assert code == 0 and audd.calls == 0 and shazam.requests == 0
    (published,) = paths
    assert published != old_bundle and published.parents[2] == old_bundle.parents[2]
    assert read_bundle_manifest(published)["refusion"]["source_bundle"] == old_bundle.name
    aliases = {path.parent for path in work.glob(f"*/{old_bundle.parents[2].name}/ingest")}
    assert len(aliases) == 2


def test_the_stale_lookup_and_the_re_fusion_never_run_a_hint_connector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review P1: a request WITH hints used to run the connectors before it looked for the stale
    result.  Every connector lives behind ``run_hints`` and an HTTP client; both are wired to
    fail, and so is every socket."""

    work, audio, old_bundle, _ = _stored_under_fusion_2(tmp_path, monkeypatch, no_hints=False)

    async def no_connectors(**kwargs):
        pytest.fail("a hint connector ran on the re-fusion path")

    def no_client(*args, **kwargs):
        pytest.fail("an HTTP client was built on the re-fusion path")

    monkeypatch.setattr(pipeline, "run_hints", no_connectors)
    monkeypatch.setattr("httpx.AsyncClient", no_client)
    sockets = _no_network(monkeypatch)
    code, audd, shazam, paths = _analyse(work, audio, FREE_RECIPE, no_hints=False)
    assert code == 0 and audd.calls == 0 and shazam.requests == 0 and sockets == []
    assert paths != [old_bundle] and _titles(paths[0]) == {PHANTOM, FIRST, SECOND}
    assert read_bundle_manifest(paths[0])["refusion"]["source_bundle"] == old_bundle.name


# --------------------------------------------------------------------------------------------------
# Never silently re-pay: a stale result that cannot be rebuilt STOPS the run
# --------------------------------------------------------------------------------------------------
def _damage(media: Path, how: str) -> None:
    observations = sorted((media / "recognise").rglob("observations.gen0.jsonl"))
    assert observations
    if how == "missing":
        observations[0].unlink()
    elif how == "mismatched":
        observations[0].write_bytes(observations[0].read_bytes() + b"\n")
    elif how == "retention-pruned":
        result = collect(
            media.parents[1], policy="local", apply=True, now=datetime(2100, 1, 1, tzinfo=UTC)
        )
        assert any(
            action.operation == "move" and action.path.name == "windows"
            for action in result.actions
        )
        assert not (media / "windows").exists() and not (media / "decode/audio.pcm").exists()


def _make_pre_bundle(media: Path) -> None:
    """Remove only bundle-era publication, leaving the real legacy mutable result and journal."""

    bundle = result_dir(media)
    for name in ("index.html", "tracklist.json", "source.json"):
        shutil.copy2(bundle / name, media / "present" / name)
    lines = [json.loads(line) for line in read_text(media / "invocations.jsonl").splitlines()]
    completed = next(line for line in reversed(lines) if line["status"] == "complete")
    completed.update(analysis_key=None, compatibility=None, bundle_id=None, fuse_run=None)
    (media / "invocations.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    shutil.rmtree(media / "present/bundles")
    shutil.rmtree(media / "fuse/runs")
    (media / "present/current").unlink()
    assert result_dir(media) == media / "present"


@pytest.mark.parametrize("how", ["missing", "mismatched", "intact"])
def test_a_stale_deep_result_never_becomes_a_new_paid_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], how: str
) -> None:
    """``intact`` too: a Deep result is never re-fused (its second-opinion windows were chosen by
    the old fusion), so the ONLY way to a new paid sweep is an explicit ``--refresh``."""

    work, audio, old_bundle, (first_audd, _) = _stored_under_fusion_2(
        tmp_path, monkeypatch, OLD_DEEP
    )
    assert first_audd.calls > 30  # paid clips, already paid for
    media = old_bundle.parents[2]
    _damage(media, how)
    attempts = {p: p.read_bytes() for p in media.rglob("attempts*.jsonl")}
    assert attempts
    published = _tree(media / "present", media / "fuse")
    lines = len(read_text(media / "invocations.jsonl").splitlines())

    async def forbidden(*args, **kwargs):
        pytest.fail("the run went on to a fresh analysis")

    monkeypatch.setattr(pipeline, "decode", forbidden)
    monkeypatch.setattr(pipeline, "reserve_usd", forbidden)
    capsys.readouterr()
    code, audd, shazam, paths = _analyse(work, audio, DEEP_RECIPE)
    assert code == 1 and paths == []
    assert audd.calls == 0 and shazam.requests == 0  # zero dispatches, zero provider requests
    assert {p: p.read_bytes() for p in media.rglob("attempts*.jsonl")} == attempts  # no reservation
    assert _tree(media / "present", media / "fuse") == published and result_dir(media) == old_bundle
    (entry,) = [
        json.loads(line) for line in read_text(media / "invocations.jsonl").splitlines()[lines:]
    ]
    assert entry["status"] == "failed" and entry["exit_code"] == 1
    assert entry["usd_e6_reserved"] == 0 and entry["usd_e6_spent"] == 0
    reason = entry["reason"]
    assert reason.startswith("stale_result: ") and "--refresh" in reason
    assert "Nothing was spent" in reason and "paid (Deep) result" in reason
    assert reason in capsys.readouterr().err.replace("\n", "")


@pytest.mark.parametrize("how", ["intact", "missing", "mismatched", "retention-pruned"])
def test_a_legacy_stale_deep_result_is_refused_before_any_paid_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], how: str
) -> None:
    """The owner's normal pre-bundle path is discovered from its own invocation metadata."""

    work, audio, old_bundle, (first_audd, _) = _stored_under_fusion_2(
        tmp_path, monkeypatch, OLD_DEEP
    )
    assert first_audd.calls > 30
    media = old_bundle.parents[2]
    _make_pre_bundle(media)
    _damage(media, how)
    before = _tree(media / "present", media / "fuse", media / "recognise", media / "windows")
    attempts = {path: path.read_bytes() for path in media.rglob("attempts*.jsonl")}
    lines = len(read_text(media / "invocations.jsonl").splitlines())
    upkeep = refuse_stale_result(media, config=CONFIG)
    assert upkeep.state == "unrebuildable" and "paid (Deep) result" in (upkeep.why or "")
    assert (
        _tree(media / "present", media / "fuse", media / "recognise", media / "windows") == before
    )

    async def forbidden(*args, **kwargs):
        pytest.fail("legacy Deep refusal reached decode or reservation")

    monkeypatch.setattr(pipeline, "decode", forbidden)
    monkeypatch.setattr(pipeline, "reserve_usd", forbidden)
    capsys.readouterr()
    code, audd, shazam, paths = _analyse(work, audio, DEEP_RECIPE, no_hints=False)

    assert code == 1 and paths == []
    assert audd.calls == 0 and shazam.requests == 0
    assert {path: path.read_bytes() for path in media.rglob("attempts*.jsonl")} == attempts
    assert (
        _tree(media / "present", media / "fuse", media / "recognise", media / "windows") == before
    )
    assert result_dir(media) == media / "present"
    (entry,) = [
        json.loads(line) for line in read_text(media / "invocations.jsonl").splitlines()[lines:]
    ]
    assert entry["status"] == "failed" and entry["usd_e6_reserved"] == 0
    assert entry["usd_e6_spent"] == 0 and entry["reason"].startswith("stale_result: ")
    assert "paid (Deep) result" in entry["reason"] and "--refresh" in entry["reason"]


@pytest.mark.parametrize(
    ("changed", "words"),
    [
        ("final-reference", "final generation reference"),
        ("generation", "generation"),
        ("pcm", "decode/pcm.json has changed"),
        ("identities", "identities.gen0.json has changed"),
    ],
)
def test_legacy_refusion_proves_the_complete_provenance_chain_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str, words: str
) -> None:
    work, _audio_path, old_bundle, _ = _stored_under_fusion_2(tmp_path, monkeypatch)
    media = old_bundle.parents[2]
    _make_pre_bundle(media)
    if changed == "final-reference":
        sidecar = media / "fuse/episodes.done.json"
        payload = json.loads(read_text(sidecar))
        key = next(iter(payload["upstream"]))
        payload["upstream"][key] = "0" * 64
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
    elif changed == "generation":
        episodes_path = media / "fuse/episodes.json"
        payload = json.loads(read_text(episodes_path))
        payload["generation"] += 1
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        episodes_path.write_bytes(encoded)
        sidecar = media / "fuse/episodes.done.json"
        done = json.loads(read_text(sidecar))
        done["sha256"] = hashlib.sha256(encoded).hexdigest()
        sidecar.write_text(json.dumps(done), encoding="utf-8")
    elif changed == "pcm":
        path = media / "decode/pcm.json"
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        path = media / "fuse/identities.gen0.json"
        path.write_bytes(path.read_bytes() + b"\n")
    before = _tree(media / "present", media / "fuse", media / "recognise", media / "windows")

    outcome = refuse_stale_result(media, config=CONFIG)

    assert outcome.state == "unrebuildable" and words in (outcome.why or "")
    assert (
        _tree(media / "present", media / "fuse", media / "recognise", media / "windows") == before
    )
    assert not (media / "present/bundles").exists()


@pytest.mark.parametrize(
    ("how", "words"),
    [("missing", "recorded recognition file is missing"), ("mismatched", "has changed since")],
)
def test_a_stale_free_result_with_unproven_evidence_stops_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str, words: str
) -> None:
    work, audio, old_bundle, _ = _stored_under_fusion_2(tmp_path, monkeypatch)
    media = old_bundle.parents[2]
    _damage(media, how)
    code, audd, shazam, paths = _analyse(work, audio, FREE_RECIPE)
    assert code == 1 and paths == [] and audd.calls == 0 and shazam.requests == 0
    entry = json.loads(read_text(media / "invocations.jsonl").splitlines()[-1])
    assert entry["status"] == "failed" and words in entry["reason"]
    assert result_dir(media) == old_bundle


def test_refresh_is_the_explicit_action_that_analyses_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, audio, old_bundle, _ = _stored_under_fusion_2(tmp_path, monkeypatch)
    _damage(old_bundle.parents[2], "mismatched")
    assert _analyse(work, audio, FREE_RECIPE)[0] == 1
    code, _audd, shazam, paths = _analyse(work, audio, FREE_RECIPE, refresh=True)
    assert code == 0 and shazam.requests > 30 and paths != [old_bundle]
    # ... and with a CURRENT result newer than the stale one, the stale one is out of the way.
    code, _audd, shazam, again = _analyse(work, audio, FREE_RECIPE)
    assert code == 0 and again == paths and shazam.requests == 0


# --------------------------------------------------------------------------------------------------
# Opening: the upkeep pass, never a GET
# --------------------------------------------------------------------------------------------------
def _no_network(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    attempts: list[object] = []
    real = socket.socket.connect

    def refuse(self, address, *args, **kwargs):  # noqa: ANN001
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return real(self, address, *args, **kwargs)  # asyncio's own self-pipe on Windows
        attempts.append(address)
        raise OSError("no network in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    return attempts


def _as_pre_bundle(media: Path) -> None:
    """The owner's mixes: analysed before bundles existed — flat fuse/ files, no stamp at all."""

    bundle = result_dir(media)
    for name in ("index.html", "tracklist.json", "source.json"):
        shutil.copy2(bundle / name, media / "present" / name)
    lines = [json.loads(line) for line in read_text(media / "invocations.jsonl").splitlines()]
    completed = next(line for line in reversed(lines) if line["status"] == "complete")
    completed.update(bundle_id=None, fuse_run=None)
    (media / "invocations.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    shutil.rmtree(native_path(media / "present" / "bundles"))
    shutil.rmtree(native_path(media / "fuse" / "runs"))
    (media / "present" / "current").unlink()
    assert stored_fusion_version(read_bundle_manifest(result_dir(media))) == 0


@pytest.mark.parametrize("layout", ["bundle", "pre-bundle"])
def test_the_upkeep_pass_re_fuses_stored_results_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    work, _audio_path, old_bundle, _ = _stored_under_fusion_2(tmp_path, monkeypatch)
    media = old_bundle.parents[2]
    if layout == "pre-bundle":
        _as_pre_bundle(media)
    flat = _tree(media / "fuse" / "episodes.json", media / "recognise")
    sockets = _no_network(monkeypatch)
    assert refresh_stale_pages(work, CONFIG) == 1
    new_bundle = result_dir(media)
    manifest = read_bundle_manifest(new_bundle)
    assert manifest is not None and manifest["refusion"]["fusion_version"] == 4
    assert new_bundle != old_bundle and _titles(new_bundle) == {PHANTOM, FIRST, SECOND}
    assert stored_fusion_version(manifest) == 4
    assert 'content="25"' in read_text(new_bundle / "index.html")[:4096]
    assert _tree(media / "fuse" / "episodes.json", media / "recognise") == flat
    # Idempotent: the next start-up has nothing left to do.
    assert refresh_stale_pages(work, CONFIG) == 0 and result_dir(media) == new_bundle
    assert sockets == []


def test_a_deep_result_is_left_as_it_is_by_the_upkeep_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _audio_path, old_bundle, _ = _stored_under_fusion_2(tmp_path, monkeypatch, OLD_DEEP)
    media = old_bundle.parents[2]
    outcome = refuse_stale_result(media, config=CONFIG)
    assert outcome.state == "unrebuildable" and "paid (Deep) result" in outcome.why
    assert outcome.recipe == "deep"
    assert result_dir(media) == old_bundle and not list((media / "fuse/runs").glob("refuse*"))


def test_owner_visible_upkeep_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from html import unescape

    from idea_web.application import create_app
    from idea_web.server import Upkeep, UpkeepReport
    from tests.idea_web.test_parity import request as get

    deep_root = tmp_path / "deep"
    deep_root.mkdir()
    deep_work, _audio_path, _deep_bundle, _ = _stored_under_fusion_2(
        deep_root, monkeypatch, OLD_DEEP
    )
    (deep_media,) = [path.parent for path in deep_work.glob("*/*/present")]
    deep_source_path = deep_media / "ingest/source.json"
    deep_source = json.loads(read_text(deep_source_path))
    deep_source["title"] = "Speed Garage & Bass Mix - Holly Olivia (March 26)"
    deep_source_path.write_text(json.dumps(deep_source), encoding="utf-8")
    deep_report = Upkeep(deep_work, CONFIG).run_all()
    assert len(deep_report.skipped) == 1
    assert deep_report.skipped[0][0] == "Speed Garage & Bass Mix - Holly Olivia (March 26)"
    assert "paid (Deep) result" in deep_report.skipped[0][1]
    assert deep_report.skipped[0][2] == "deep"

    benwal_root = tmp_path / "benwal"
    benwal_root.mkdir()
    benwal_work, _audio_path, _benwal_bundle, _ = _stored_under_fusion_2(benwal_root, monkeypatch)
    (benwal_media,) = [path.parent for path in benwal_work.glob("*/*/present")]
    _as_pre_bundle(benwal_media)
    benwal_source_path = benwal_media / "ingest/source.json"
    benwal_source = json.loads(read_text(benwal_source_path))
    benwal_source["title"] = "BENWAL"
    benwal_source_path.write_text(json.dumps(benwal_source), encoding="utf-8")
    final_sidecar = benwal_media / "fuse/episodes.done.json"
    final = json.loads(read_text(final_sidecar))
    generation_key = next(key for key in final["upstream"] if key.startswith("fuse/episodes.gen"))
    final["upstream"][generation_key] = "0" * 64
    final_sidecar.write_text(json.dumps(final), encoding="utf-8")
    benwal_report = Upkeep(benwal_work, CONFIG).run_all()
    assert len(benwal_report.skipped) == 1
    assert benwal_report.skipped[0][0] == "BENWAL"
    assert "stored records disagree with each other" in benwal_report.skipped[0][1]
    assert benwal_report.skipped[0][2] == "free"
    # Its safe page-only refresh creates a compatibility-less ``legacy-*`` bundle.  That wrapper
    # must not hide the validated Free journal on the next startup.
    benwal_again = Upkeep(benwal_work, CONFIG).run_all()
    assert len(benwal_again.skipped) == 1 and benwal_again.skipped[0][2] == "free"

    unknown_root = tmp_path / "unknown"
    unknown_root.mkdir()
    unknown_work, _audio_path, _unknown_bundle, _ = _stored_under_fusion_2(
        unknown_root, monkeypatch, OLD_DEEP
    )
    (unknown_media,) = [path.parent for path in unknown_work.glob("*/*/present")]
    _as_pre_bundle(unknown_media)
    _damage_legacy_metadata(unknown_media, "malformed")
    unknown_source_path = unknown_media / "ingest/source.json"
    unknown_source = json.loads(read_text(unknown_source_path))
    unknown_source["title"] = "Unknown recipe mix"
    unknown_source_path.write_text(json.dumps(unknown_source), encoding="utf-8")
    unknown_report = Upkeep(unknown_work, CONFIG).run_all()
    assert len(unknown_report.skipped) == 1
    assert unknown_report.skipped[0][2] == "unknown"

    report = UpkeepReport(
        updated=[f"updated {index}" for index in range(11)],
        refreshed=["BENWAL"],  # page-only work does not mean its inconsistent result improved
        skipped=[
            deep_report.skipped[0],
            ("Garage Mix - Dec 25", deep_report.skipped[0][1], "deep"),
            benwal_report.skipped[0],
            unknown_report.skipped[0],
        ],
    )
    report.finished.set()
    lines = report.owner_status_lines()
    assert lines == [
        "Saved-result upkeep complete: 11 mixes improved; 4 deliberately left as they are.",
        "Speed Garage & Bass Mix - Holly Olivia (March 26): The result was left as it is because "
        "it is a paid (Deep) result, and the windows its second opinion checked were chosen by "
        "the older fusion rules, so it cannot simply be rebuilt. Warning: re-running this one "
        "will spend real AudD credit.",
        "Garage Mix - Dec 25: The result was left as it is because it is a paid (Deep) result, "
        "and the windows its second opinion checked were chosen by the older fusion rules, so it "
        "cannot simply be rebuilt. Warning: re-running this one will spend real AudD credit.",
        "BENWAL: The result was left as it is because its stored records disagree with each "
        "other: the final generation reference disagrees with its generation sidecar. Re-running "
        "this one is free: it costs nothing, but it will take time.",
        "Unknown recipe mix: The result was left as it is because the legacy invocation journal "
        "is malformed or truncated; without trustworthy legacy metadata it is not safe to assume "
        "the stored result was Free. Warning: its recipe could not be determined, so ID'er must "
        "treat it as paid; re-running this one may spend real AudD credit.",
        "To analyse a skipped mix again, re-run it with --refresh; each line above says whether "
        "that re-run costs money.",
    ]

    app = create_app(tmp_path / "browser", upkeep_report=report)
    page = get(app, "GET", "/")
    status = get(app, "GET", "/upkeep/status")
    for words in lines:
        assert words in unescape(page.text) and words in unescape(status.text)
    assert 'id="upkeep-status"' in page.text and 'data-finished="1"' in status.text
    script = get(app, "GET", "/static/" + page.text.rsplit("/static/", 1)[1].split('"', 1)[0])
    assert b"/upkeep/status" in script.content

    from typer.testing import CliRunner

    class FinishedServer:
        server_address = ("127.0.0.1", 8765)

        def __init__(self, callback) -> None:
            self.callback = callback

        def serve_forever(self) -> None:
            self.callback(report)

        def shutdown(self) -> None:
            pass

        def server_close(self) -> None:
            pass

    def finished_server(*_args, on_upkeep_finished=None, **_kwargs):
        return FinishedServer(on_upkeep_finished)

    monkeypatch.setattr("idea_web.server.make_server", finished_server)
    command = CliRunner().invoke(
        cli.app,
        [
            "serve",
            "--work-root",
            str(tmp_path / "browser"),
            "--port",
            "0",
            "--no-analyse",
            "--no-open",
        ],
    )
    assert command.exit_code == 0, command.output
    for words in lines:
        assert words in command.output


def test_upkeep_cannot_replace_a_page_24_legacy_deep_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from id_detector.present import page as page_module
    from idea_web.server import Upkeep

    with monkeypatch.context() as old_page:
        old_page.setattr(page_module, "PAGE_VERSION", 24)
        work, audio, old_bundle, _ = _stored_under_fusion_2(tmp_path, old_page, OLD_DEEP)
    media = old_bundle.parents[2]
    _make_pre_bundle(media)
    _use_old_success_journal_schema(media)
    before = _tree(media / "present", media / "fuse")

    report = Upkeep(work, CONFIG).run_all()

    assert len(report.skipped) == 1 and not report.updated and not report.refreshed
    assert _tree(media / "present", media / "fuse") == before
    assert not (media / "present/current").exists()

    async def forbidden(*args, **kwargs):
        pytest.fail("the page-24 legacy Deep guard was bypassed")

    monkeypatch.setattr(pipeline, "decode", forbidden)
    monkeypatch.setattr(pipeline, "reserve_usd", forbidden)
    capsys.readouterr()
    code, audd, shazam, paths = _analyse(work, audio, DEEP_RECIPE, no_hints=False)
    assert code == 1 and paths == [] and audd.calls == 0 and shazam.requests == 0
    assert _tree(media / "present", media / "fuse") == before
    assert "--refresh" in capsys.readouterr().err

    # Even a caller that explicitly performs a page-only refresh cannot erase the validated flat
    # provenance: the compatibility-less bundle is unrelated to stale selection.
    from id_detector.present.refresh import refresh_page

    assert refresh_page(media, config=CONFIG) == "refreshed"
    page_bundle = result_dir(media)
    assert read_bundle_manifest(page_bundle)["compatibility"] is None
    guarded = _tree(media / "present", media / "fuse")
    code, audd, shazam, paths = _analyse(work, audio, DEEP_RECIPE, no_hints=False)
    assert code == 1 and paths == [] and audd.calls == 0 and shazam.requests == 0
    assert _tree(media / "present", media / "fuse") == guarded


def _damage_legacy_metadata(media: Path, case: str) -> None:
    path = media / "invocations.jsonl"
    if case == "missing":
        path.unlink()
    elif case == "malformed":
        path.write_text("{not-json}\n", encoding="utf-8")
    elif case == "truncated":
        path.write_bytes(path.read_bytes()[:-11])
    else:
        rows = [json.loads(line) for line in read_text(path).splitlines()]
        completed = next(row for row in rows if row["status"] == "complete")
        duplicate = {**completed, "achieved": "free"}
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in [*rows, duplicate]), encoding="utf-8"
        )


def _use_old_success_journal_schema(media: Path) -> None:
    path = media / "invocations.jsonl"
    completed = next(
        json.loads(line)
        for line in read_text(path).splitlines()
        if json.loads(line)["status"] == "complete"
    )
    fields = {
        "schema_version",
        "generated_by",
        "invocation_id",
        "command",
        "started_at",
        "finished_at",
        "status",
        "exit_code",
        "duration_ms",
        "tool_versions",
        "timings",
        "counts",
        "costs",
        "source_ids",
    }
    old = {key: value for key, value in completed.items() if key in fields}
    old["status"] = "succeeded"
    assert set(old) == fields
    path.write_text(json.dumps(old) + "\n", encoding="utf-8")


@pytest.mark.parametrize("case", ["missing", "malformed", "truncated", "ambiguous"])
def test_unreadable_legacy_metadata_blocks_direct_deep_and_upkeep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    from idea_web.server import Upkeep

    work, audio, old_bundle, _ = _stored_under_fusion_2(tmp_path, monkeypatch, OLD_DEEP)
    media = old_bundle.parents[2]
    _make_pre_bundle(media)
    _damage_legacy_metadata(media, case)
    before = _tree(media / "present", media / "fuse")
    report = Upkeep(work, CONFIG).run_all()
    assert len(report.skipped) == 1 and not report.updated and not report.refreshed
    assert _tree(media / "present", media / "fuse") == before

    async def forbidden(*args, **kwargs):
        pytest.fail("unreadable legacy metadata reached a paid action")

    monkeypatch.setattr(pipeline, "decode", forbidden)
    monkeypatch.setattr(pipeline, "reserve_usd", forbidden)
    capsys.readouterr()
    code, audd, shazam, paths = _analyse(work, audio, DEEP_RECIPE, no_hints=False)
    assert code == 1 and paths == [] and audd.calls == 0 and shazam.requests == 0
    assert _tree(media / "present", media / "fuse") == before
    error = capsys.readouterr().err
    assert "trustworthy legacy metadata" in error and "--refresh" in error


def test_a_get_never_re_fuses_or_publishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """4a-ii: nothing is published inside a request.  Without the upkeep pass the stale result
    is served exactly as it is."""

    from idea_web.application import create_app
    from tests.idea_web.test_parity import request as get

    work, _audio_path, old_bundle, _ = _stored_under_fusion_2(tmp_path, monkeypatch)
    media = old_bundle.parents[2]
    before = _tree(media / "present", media / "fuse")
    app = create_app(work)
    assert get(app, "GET", "/").status_code == 200
    page = get(app, "GET", f"/{media.parent.name}/{media.name}/present/index.html")
    assert page.status_code == 200
    assert _tree(media / "present", media / "fuse") == before and result_dir(media) == old_bundle


# --------------------------------------------------------------------------------------------------
# Input proof: every recorded digest present, well-formed and equal
# --------------------------------------------------------------------------------------------------
def _edit_sidecar(media: Path, edit) -> None:
    path = media / "fuse" / "episodes.gen0.done.json"
    document = json.loads(read_text(path))
    edit(document["upstream"])
    path.write_text(json.dumps(document), encoding="utf-8")


def _key(upstream: dict, prefix: str) -> str:
    return next(key for key in sorted(upstream) if key.startswith(prefix))


@pytest.mark.parametrize(
    ("case", "words"),
    [
        ("observation digest malformed", "no valid recorded checksum"),
        ("observation digest missing", "no valid recorded checksum"),
        ("observation marked pruned", "no valid recorded checksum"),
        ("observation changed", "has changed since"),
        ("window file changed", "has changed since"),
        ("window digest malformed", "no valid recorded checksum"),
    ],
)
def test_evidence_that_cannot_be_proven_is_never_re_fused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, words: str
) -> None:
    work, _audio_path, old_bundle, _ = _stored_under_fusion_2(tmp_path, monkeypatch)
    media = old_bundle.parents[2]
    _as_pre_bundle(media)  # flat sidecars: the layout an owner accident can actually reach
    if case == "observation digest malformed":
        _edit_sidecar(media, lambda up: up.update({_key(up, "recognise/"): "abc123"}))
    elif case == "observation digest missing":
        _edit_sidecar(media, lambda up: up.update({_key(up, "recognise/"): None}))
    elif case == "observation marked pruned":
        _edit_sidecar(
            media,
            lambda up: up.update(
                {_key(up, "recognise/"): {"pruned_upstream": up[_key(up, "recognise/")]}}
            ),
        )
    elif case == "observation changed":
        _damage(media, "mismatched")
    elif case == "window file changed":
        windows = media / "windows" / "windows.gen0.jsonl"
        windows.write_bytes(windows.read_bytes() + b"\n")
    elif case == "window digest malformed":
        _edit_sidecar(media, lambda up: up.update({_key(up, "windows/"): "not-a-digest"}))
    before = _tree(media / "present", media / "fuse")
    with pytest.raises(NotRebuildable, match=words):
        load_fusion_inputs(media, media / "fuse")
    outcome = refuse_stale_result(media, config=CONFIG)
    assert outcome.state == "unrebuildable" and words in outcome.why and outcome.bundle is None
    assert _tree(media / "present", media / "fuse") == before


@pytest.mark.parametrize(
    ("prefix", "words"),
    [
        ("windows/", "window inputs"),
        ("decode/pcm.json", "PCM record"),
        ("fuse/identities.gen", "identity record"),
    ],
)
def test_generation_sidecar_requires_each_input_category(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
    words: str,
) -> None:
    _work, _audio_path, old_bundle, _ = _stored_under_fusion_2(tmp_path, monkeypatch)
    media = old_bundle.parents[2]
    _as_pre_bundle(media)

    def remove(upstream: dict) -> None:
        for key in [key for key in upstream if key.startswith(prefix)]:
            upstream.pop(key)

    _edit_sidecar(media, remove)
    before = _tree(media / "present", media / "fuse")
    outcome = refuse_stale_result(media, config=CONFIG)
    assert outcome.state == "unrebuildable" and words in (outcome.why or "")
    assert _tree(media / "present", media / "fuse") == before


@pytest.mark.parametrize("marker", ["plain digest", "pruned marker"])
def test_collected_window_records_do_not_stop_a_re_fusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    """Retention removes ``windows/`` at once (and rewrites a flat sidecar's entry as pruned);
    an ABSENT window file is acceptable — the answered observations carry the same supports."""

    work, _audio_path, old_bundle, _ = _stored_under_fusion_2(tmp_path, monkeypatch)
    media = old_bundle.parents[2]
    if marker == "pruned marker":
        _as_pre_bundle(media)
        _edit_sidecar(
            media,
            lambda up: up.update(
                {_key(up, "windows/"): {"pruned_upstream": up[_key(up, "windows/")]}}
            ),
        )
    shutil.rmtree(media / "windows")
    outcome = refuse_stale_result(media, config=CONFIG)
    assert outcome.state == "refused" and _titles(outcome.bundle) == {PHANTOM, FIRST, SECOND}


def test_the_bytes_that_are_hashed_are_the_bytes_that_are_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each evidence file is read exactly once: there is no second open for a swap to land in."""

    from id_detector import refusion

    _work, _audio_path, old_bundle, _ = _stored_under_fusion_2(tmp_path, monkeypatch)
    media = old_bundle.parents[2]
    reads: list[str] = []
    real = refusion.read_bytes

    def counted(path):
        reads.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(refusion, "read_bytes", counted)
    monkeypatch.setattr(refusion, "read_text", lambda path: real(path).decode("utf-8"))
    inputs = load_fusion_inputs(media, media / read_bundle_manifest(old_bundle)["fuse_run"])
    assert sorted(reads) == sorted(Path(key).name for key in inputs.files)


# --------------------------------------------------------------------------------------------------
# Locking: busy is not "cannot be re-fused", and nothing is ever published unlocked
# --------------------------------------------------------------------------------------------------
_PROCESS_HARNESS = r"""
import os
import sys
import time
from pathlib import Path

from id_detector.jobs import JobStoreLocked, ProcessLock
from id_detector.providers.base import AppConfig
from id_detector.refusion import refuse_stale_result

mode = sys.argv[1]
media = Path(sys.argv[2])
ready = Path(sys.argv[3]) if sys.argv[3] != "-" else None
gate = Path(sys.argv[4]) if sys.argv[4] != "-" else None
if mode in {"refuse", "crash"}:
    if mode == "crash":
        import id_detector.refusion as module
        module.publish_result = lambda **_kwargs: os._exit(73)
    if ready is not None:
        ready.write_text("ready", encoding="utf-8")
    while gate is not None and not gate.exists():
        time.sleep(0.01)
    result = refuse_stale_result(media, config=AppConfig(transforms_policy="off"))
    print(result.state, flush=True)
elif mode == "hold":
    lock = ProcessLock(media / ".media.lock")
    try:
        lock.acquire()
    except JobStoreLocked:
        print("locked", flush=True)
        raise SystemExit(0)
    print("acquired", flush=True)
    ready.write_text("ready", encoding="utf-8")
    while not gate.exists():
        time.sleep(0.01)
    lock.release()
else:
    lock = ProcessLock(media / ".media.lock")
    try:
        lock.acquire()
    except JobStoreLocked:
        print("locked", flush=True)
    else:
        print("acquired", flush=True)
        lock.release()
"""


def _child(mode: str, media: Path, ready: Path | None = None, gate: Path | None = None):
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _PROCESS_HARNESS,
            mode,
            str(media),
            str(ready) if ready is not None else "-",
            str(gate) if gate is not None else "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "IDEA_TEST_MODE": "1"},
    )


def _await_files(*paths: Path) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not all(path.exists() for path in paths):
        time.sleep(0.02)
    assert all(path.exists() for path in paths)


def test_spawned_process_refusion_crash_and_alias_lock_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two processes race one stale result: at most one freezes/publishes, and the other is busy or
    # observes the completed publication.  Both outcomes are safe and deterministic on disk.
    compete = tmp_path / "compete"
    compete.mkdir()
    _work, _audio_path, old_bundle, _ = _stored_under_fusion_2(compete, monkeypatch)
    media = old_bundle.parents[2]
    ready1, ready2, gate = (tmp_path / name for name in ("ready1", "ready2", "go"))
    first = _child("refuse", media, ready1, gate)
    second = _child("refuse", media, ready2, gate)
    _await_files(ready1, ready2)
    gate.write_text("go", encoding="utf-8")
    one_out, one_err = first.communicate(timeout=120)
    two_out, two_err = second.communicate(timeout=120)
    assert first.returncode == second.returncode == 0, (one_err, two_err)
    assert {one_out.strip(), two_out.strip()} <= {"refused", "busy", "current"}
    assert "refused" in {one_out.strip(), two_out.strip()}
    assert read_bundle_manifest(result_dir(media))["refusion"]["fusion_version"] == 4
    assert len(list((media / "present/bundles").glob("*"))) == 2

    # The first process dies after the frozen run is sealed but before publish_result can create or
    # advance a bundle pointer.  A fresh process resumes the deterministic run and publishes it.
    crash_root = tmp_path / "crash"
    crash_root.mkdir()
    _work, _audio_path, crash_old, _ = _stored_under_fusion_2(crash_root, monkeypatch)
    crash_media = crash_old.parents[2]
    crashed = _child("crash", crash_media)
    _out, crash_err = crashed.communicate(timeout=120)
    assert crashed.returncode == 73, crash_err
    assert result_dir(crash_media) == crash_old
    (frozen,) = list((crash_media / "fuse/runs").glob("refuse*"))
    from id_detector.present.bundles import read_manifest

    assert read_manifest(frozen) is not None
    recovered = _child("refuse", crash_media)
    recovered_out, recovered_err = recovered.communicate(timeout=120)
    assert recovered.returncode == 0 and recovered_out.strip() == "refused", recovered_err
    assert result_dir(crash_media) != crash_old

    # Two source aliases of the same media key resolve to the same work-root lock.  The second
    # process cannot hold another media lock, so there is no dual-lock ordering to deadlock.
    lock_work = tmp_path / "lock-work"
    media_key = "a" * 64
    alias_a = lock_work / ("1" * 64) / media_key
    alias_b = lock_work / ("2" * 64) / media_key
    alias_a.mkdir(parents=True)
    alias_b.mkdir(parents=True)
    held_ready, release = tmp_path / "held", tmp_path / "release"
    holder = _child("hold", alias_a, held_ready, release)
    _await_files(held_ready)
    probe = _child("probe", alias_b)
    probe_out, probe_err = probe.communicate(timeout=120)
    assert probe.returncode == 0 and probe_out.strip() == "locked", probe_err
    release.write_text("release", encoding="utf-8")
    held_out, held_err = holder.communicate(timeout=120)
    assert holder.returncode == 0 and held_out.strip() == "acquired", held_err


def test_a_busy_media_is_left_alone_by_re_fusion_and_by_the_page_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from id_detector.jobs import ProcessLock
    from id_detector.present.refresh import ensure_fresh_page, refresh_page
    from idea_web.server import Upkeep

    work, _audio_path, old_bundle, _ = _stored_under_fusion_2(tmp_path, monkeypatch)
    media = old_bundle.parents[2]
    monkeypatch.setattr("id_detector.present.refresh.PAGE_VERSION", 10**6)  # the page IS stale
    before = _tree(media / "present", media / "fuse")
    held = ProcessLock(media / ".media.lock")
    held.acquire()
    try:
        assert refuse_stale_result(media, config=CONFIG).state == "busy"
        assert refresh_page(media, config=CONFIG) == "busy"
        assert ensure_fresh_page(media, config=CONFIG) is False
        report = Upkeep(work, CONFIG).run_all()
        assert len(report.busy) == 1 and not report.updated and not report.refreshed
        assert _tree(media / "present", media / "fuse") == before
    finally:
        held.release()
    assert Upkeep(work, CONFIG).run_all().updated  # and once the run is over, it is brought up


def test_the_offline_lookup_names_only_the_newest_stale_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _audio_path, old_bundle, _ = _stored_under_fusion_2(tmp_path, monkeypatch)
    media = old_bundle.parents[2]
    inputs = read_bundle_manifest(old_bundle)["compatibility"]["analysis_inputs"]
    asked = dict(
        recipe=FREE_RECIPE,
        source_kind=inputs["source_kind"],
        tenant_scope=inputs["tenant_scope"],
        manual_tracklist_sha256="",
        panako_index_id="",
    )
    req = compat.RunRequest(
        compat.AnalysisInputs(**{**inputs, "recipe_id": FREE_RECIPE.recipe_id}), FREE_RECIPE
    )
    assert find_result(media, req) is None  # never served as it is
    found = find_stale_result(media, with_hints=False, **asked)
    assert found is not None and found[0] == old_bundle and found[1] == req
    assert find_stale_result(media, with_hints=True, **asked) is not None
    # Different inputs are a different analysis, not a stale one.
    assert find_stale_result(media, with_hints=False, **{**asked, "recipe": DEEP_RECIPE}) is None
    other = {**asked, "manual_tracklist_sha256": "c" * 64, "tenant_scope": "user:local"}
    assert find_stale_result(media, with_hints=False, **other) is None
    # Once re-fused, the newest answer is current: the offline lookup steps aside.
    assert refuse_stale_result(media, config=CONFIG).state == "refused"
    assert find_stale_result(media, with_hints=False, **asked) is None
    assert find_result(media, req) == result_dir(media) != old_bundle

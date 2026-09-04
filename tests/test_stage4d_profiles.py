from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from id_detector import cli as cli_module
from id_detector.contracts import ProfileRecord
from id_detector.io import atomic_write_json, read_bytes, read_text
from id_detector.profiles import (
    PROFILE_NAMES,
    UnknownProfile,
    freeze_profiles,
    load_profile,
    profile_app_config,
)

ROOT = Path(__file__).resolve().parents[1]
ABLATIONS = ROOT / "data" / "corpus" / "controlled-synth-1" / "ablations.json"
SHORTLIST = ROOT / "data" / "corpus" / "controlled-synth-1" / "shortlist.json"
PROFILES = ROOT / "profiles"


def _committed(name: str) -> dict:
    return json.loads(read_text(PROFILES / f"{name}.json"))


def _dig(report: Any, path: str) -> Any:
    current = report
    for part in path.split("."):
        current = current[int(part)] if part.lstrip("-").isdigit() else current[part]
    return current


def test_committed_profiles_rederive_byte_for_byte(tmp_path: Path) -> None:
    """Freezing again from the same reports reproduces the committed artefacts exactly."""

    result = freeze_profiles(
        ablations_path=ABLATIONS,
        shortlist_path=SHORTLIST,
        out_dir=tmp_path,
    )
    assert set(result.profiles) == set(PROFILE_NAMES)
    for name in PROFILE_NAMES:
        version = result.profiles[name].version
        rederived = read_bytes(tmp_path / version)
        committed = read_bytes(PROFILES / version)
        assert rederived == committed, f"{version} is not byte-for-byte reproducible"


def test_every_cited_number_is_traceable_to_a_report_field() -> None:
    ablations = json.loads(read_text(ABLATIONS))
    shortlist = json.loads(read_text(SHORTLIST))
    reports = {"ablations": ablations, "shortlist": shortlist}

    def _walk(node: object) -> list[dict]:
        found: list[dict] = []
        if isinstance(node, dict):
            if node.keys() >= {"report", "field", "value"} and node["report"] in reports:
                found.append(node)
            for item in node.values():
                found.extend(_walk(item))
        elif isinstance(node, list):
            for item in node:
                found.extend(_walk(item))
        return found

    for name in PROFILE_NAMES:
        profile = _committed(f"{name}-v1")
        citations = _walk(profile)
        assert citations, f"{name} cites nothing"
        for citation in citations:
            actual = _dig(reports[citation["report"]], citation["field"])
            assert actual == citation["value"], (
                f"{name}: {citation['report']}.{citation['field']} is {actual!r}, "
                f"profile claims {citation['value']!r}"
            )


def test_only_shazam_is_enabled_and_paid_engines_are_recorded_not_enabled() -> None:
    for name in PROFILE_NAMES:
        profile = ProfileRecord.model_validate(_committed(f"{name}-v1"))
        assert profile.enabled_engines == ["shazam"]
        engines = {engine.provider: engine for engine in profile.engines}
        assert engines["shazam"].enabled is True and engines["shazam"].cost_class == "free"
        for provider in ("audd", "acrcloud", "panako"):
            assert engines[provider].enabled is False
        # Panako is free and self-hosted: eligible-when-available in both profiles.
        assert engines["panako"].eligible_when_available is True
        # Paid scanners: eligible only in the paid profile.
        if name == "free":
            assert engines["audd"].eligible_when_available is False
            assert engines["acrcloud"].eligible_when_available is False
            assert profile.engine_policy == "free_only"
            assert profile.cost_report.paid_when_enabled == []
        else:
            assert engines["audd"].eligible_when_available is True
            assert engines["acrcloud"].eligible_when_available is True
            assert profile.engine_policy == "all_available"
            paid = {row.provider: row for row in profile.cost_report.paid_when_enabled}
            assert set(paid) == {"audd", "acrcloud"}
            assert all(row.expected_trial_cost_usd_e2 == 29 for row in paid.values())


def test_feature_decisions_match_the_ablation_evidence() -> None:
    for name in PROFILE_NAMES:
        profile = ProfileRecord.model_validate(_committed(f"{name}-v1"))
        features = {feature.name: feature for feature in profile.features}
        assert profile.transforms_policy == "rescan_only"
        assert features["transforms"].setting["policy"] == "rescan_only"
        assert features["rescans"].enabled and features["rescans"].certified
        assert profile.rescan.enabled and profile.rescan.max_generations == 3
        assert profile.rescan.window_ms == 12_000 and profile.rescan.hop_ms == 5_000
        assert features["novelty"].enabled and profile.novelty_enabled
        # Generation-0 schedule stays at the plan default 12/9 (rev 5.2), not a 5 s challenger.
        assert profile.schedule.window_ms == 12_000
        assert profile.schedule.hop_ms == 9_000
        assert profile.schedule.phase_ms == 0
        # Hints are on by plan mandate but uncertified (Stage 4a gate blocked on dev-2).
        assert profile.hints_enabled is True
        assert features["hints"].enabled is True and features["hints"].certified is False
        assert "not_evaluable" in profile.hints_gate_status


def test_global_transforms_and_dense_gen0_hop_are_not_adopted() -> None:
    free = ProfileRecord.model_validate(_committed("free-v1"))
    # global was measured but is not selected (no recall gain, precision fails non-inferiority).
    assert free.transforms_policy != "global"
    # No frozen profile drops the generation-0 hop below the calibrated 9 s.
    assert free.schedule.hop_ms >= 9_000


def test_load_profile_accepts_frozen_and_rejects_unknown_or_nonfrozen(tmp_path: Path) -> None:
    profile = load_profile(ROOT, "free")
    assert profile.name == "free" and profile.frozen is True

    with pytest.raises(UnknownProfile):
        load_profile(ROOT, "aggressive")

    # A non-frozen artefact with a higher version wins the version race and is rejected.
    fake_root = tmp_path
    (fake_root / "profiles").mkdir()
    tampered = _committed("free-v1")
    tampered["version"] = "free-v9.json"
    tampered["frozen"] = False
    atomic_write_json(fake_root / "profiles" / "free-v9.json", tampered)
    with pytest.raises(UnknownProfile):
        load_profile(fake_root, "free")


def test_profile_app_config_maps_the_frozen_geometry() -> None:
    config = profile_app_config(load_profile(ROOT, "free"))
    assert config.transforms_policy == "rescan_only"
    assert config.window_ms == 12_000 and config.hop_ms == 9_000 and config.phase_ms == 0
    assert config.rescan_window_ms == 12_000 and config.rescan_hop_ms == 5_000
    assert config.rescan_max_generations == 3
    assert config.allow_third_party_upload is False
    assert config.transform_rates_e4 == (9_200, 9_600, 10_400, 10_800)


def test_analyse_rejects_a_profile_that_is_not_a_frozen_artefact() -> None:
    result = CliRunner().invoke(
        cli_module.app, ["analyse", "http://example/set", "--profile", "turbo"]
    )
    assert result.exit_code == 2
    assert "unknown profile" in result.output


def test_analyse_accepts_a_frozen_profile_and_derives_its_config(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_analyse(url: str, **kwargs: object) -> int:
        captured["url"] = url
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli_module, "_analyse", fake_analyse)
    result = CliRunner().invoke(
        cli_module.app, ["analyse", "http://example/set", "--profile", "max_accuracy"]
    )
    assert result.exit_code == 0, result.output
    config = captured["app_config"]
    assert config.transforms_policy == "rescan_only"
    assert config.hop_ms == 9_000 and config.rescan_hop_ms == 5_000
    assert captured["novelty"] is True
    assert captured["no_hints"] is False
    assert captured["max_generations"] == 3

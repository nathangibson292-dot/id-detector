from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from id_detector.cli import app
from id_detector.config_template import CONFIG_TEMPLATE, render_effective_config
from id_detector.providers.base import AppConfig

runner = CliRunner()

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_committed_example_config_matches_the_packaged_template() -> None:
    example = (PROJECT_ROOT / "id-detector.example.toml").read_text(encoding="utf-8")
    assert example == CONFIG_TEMPLATE


def test_packaged_template_parses_to_the_documented_defaults() -> None:
    config = _load_template(AppConfig)
    assert config.allow_third_party_upload is False
    assert config.default_profile is None
    assert config.max_requests == 2_000
    assert config.lead_in_ms == 5_000
    assert config.cache_positive_max_age_days == 180
    assert config.cache_no_match_max_age_days == 30
    assert config.hints_enabled is True
    assert config.disabled_hint_connectors == frozenset()
    # A template that parses to defaults must equal a fresh AppConfig on every runtime field.
    assert config == AppConfig()


def _load_template(_cls: type[AppConfig]) -> AppConfig:
    import tempfile

    with tempfile.NamedTemporaryFile(
        "w", suffix=".toml", delete=False, encoding="utf-8", newline="\n"
    ) as handle:
        handle.write(CONFIG_TEMPLATE)
        path = Path(handle.name)
    try:
        return AppConfig.load(path)
    finally:
        path.unlink(missing_ok=True)


def test_cache_and_budget_and_lead_in_are_honoured(tmp_path: Path) -> None:
    config = tmp_path / "id-detector.toml"
    config.write_text(
        "\n".join(
            [
                "max_requests = 42",
                "lead_in_ms = 1500",
                "default_profile = 'free'",
                "[cache]",
                "positive_max_age_days = 7",
                "no_match_max_age_days = 2",
            ]
        ),
        encoding="utf-8",
    )
    loaded = AppConfig.load(config)
    assert loaded.max_requests == 42
    assert loaded.lead_in_ms == 1_500
    assert loaded.default_profile == "free"
    assert loaded.cache_positive_max_age_seconds == 7 * 24 * 60 * 60
    assert loaded.cache_no_match_max_age_seconds == 2 * 24 * 60 * 60


def test_hints_table_toggles_individual_connectors(tmp_path: Path) -> None:
    config = tmp_path / "id-detector.toml"
    config.write_text("[hints]\nmixesdb = false\nyt_comments = false\n", encoding="utf-8")
    loaded = AppConfig.load(config)
    assert loaded.hints_enabled is True
    assert loaded.disabled_hint_connectors == frozenset({"mixesdb", "yt_comments"})


def test_hints_enabled_false_is_recorded(tmp_path: Path) -> None:
    config = tmp_path / "id-detector.toml"
    config.write_text("[hints]\nenabled = false\n", encoding="utf-8")
    assert AppConfig.load(config).hints_enabled is False


def test_unknown_hint_connector_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "id-detector.toml"
    config.write_text("[hints]\nnot_a_connector = false\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown hints connector"):
        AppConfig.load(config)


@pytest.mark.parametrize(
    "body",
    [
        "max_requests = 0",
        "max_requests = -1",
        "lead_in_ms = -1",
        "[cache]\npositive_max_age_days = 0",
        "default_profile = ''",
    ],
)
def test_invalid_values_are_rejected(tmp_path: Path, body: str) -> None:
    config = tmp_path / "id-detector.toml"
    config.write_text(body + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        AppConfig.load(config)


def test_render_effective_config_round_trips_through_the_loader(tmp_path: Path) -> None:
    original = AppConfig(
        max_requests=99,
        lead_in_ms=250,
        cache_positive_max_age_days=5,
        cache_no_match_max_age_days=1,
        disabled_hint_connectors=frozenset({"tl1001"}),
    )
    rendered = render_effective_config(original)
    config = tmp_path / "shown.toml"
    config.write_text(rendered, encoding="utf-8")
    reloaded = AppConfig.load(config)
    assert reloaded.max_requests == 99
    assert reloaded.lead_in_ms == 250
    assert reloaded.cache_positive_max_age_days == 5
    assert reloaded.disabled_hint_connectors == frozenset({"tl1001"})


def test_config_show_prints_defaults_when_no_file_present() -> None:
    result = runner.invoke(app, ["config", "show", "--config", "no-such-file.toml"])
    assert result.exit_code == 0
    assert "built-in defaults" in result.stdout
    assert "max_requests = 2000" in result.stdout
    # No credential env-var name or value must ever appear in the shown config.
    for secret in (
        "AUDD_API_TOKEN",
        "ACRCLOUD_ACCESS_SECRET",
        "ACRCLOUD_ACCESS_KEY",
        "SOUNDCLOUD_OAUTH_TOKEN",
        "SOUNDCLOUD_CLIENT_ID",
        "DISCOGS_API_TOKEN",
    ):
        assert secret not in result.stdout


def test_config_init_writes_template_and_refuses_to_clobber(tmp_path: Path) -> None:
    target = tmp_path / "id-detector.toml"
    first = runner.invoke(app, ["config", "init", "--path", str(target)])
    assert first.exit_code == 0
    assert target.read_text(encoding="utf-8") == CONFIG_TEMPLATE
    second = runner.invoke(app, ["config", "init", "--path", str(target)])
    assert second.exit_code == 1
    forced = runner.invoke(app, ["config", "init", "--path", str(target), "--force"])
    assert forced.exit_code == 0

"""Phase 0a-ii gate: recipes, pricing, and exact USD lifecycle controls."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from id_detector import cli
from id_detector.money import (
    BILLABLE_OUTCOMES,
    ZERO_COST_OUTCOMES,
    BudgetExhausted,
    ReservationExhausted,
    UsdAdmitter,
    reserve_usd,
)
from id_detector.paid_clip import _protocol_outcome
from id_detector.pricing import (
    DEFAULT_PRICING_PATH,
    PricingConfig,
    _resolve_pricing_path,
    load_pricing,
)
from id_detector.providers.audd import AudDAdapter, AudDCredentials
from id_detector.providers.base import (
    AmbiguousProviderOutcome,
    AppConfig,
    ProviderProtocolError,
    ProviderUnavailable,
)
from id_detector.recipes import DEEP_RECIPE, FREE_RECIPE, Recipe, get_recipe
from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff

ROOT = Path(__file__).resolve().parents[1]
AUDIO = ROOT / "tests" / "fixtures" / "audio" / "tone-60s.wav"
SCRIPTS = ROOT / "tests" / "fakes" / "scripts"


def _entry(work_root: Path) -> tuple[Path, dict[str, object]]:
    (path,) = work_root.rglob("invocations.jsonl")
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return path.parent, entries[-1]


def _run_deep(
    tmp_path: Path,
    script_name: str,
    *,
    app_config: AppConfig | None = None,
    recipe: Recipe = DEEP_RECIPE,
) -> tuple[int, FakeAudD, FakeShazamHTTP, dict[str, object], Path]:
    script = SCRIPTS / script_name
    audd = FakeAudD(script)
    shazam = FakeShazamHTTP(script)
    work_root = tmp_path / "work"
    code = asyncio.run(
        cli._analyse(
            str(AUDIO),
            work_root=work_root,
            print_raw=False,
            refresh=False,
            max_requests=100,
            tracklist=None,
            no_hints=True,
            app_config=app_config
            or AppConfig(
                transforms_policy="off",
                recognise_concurrency=1,
                shazam_requests_per_minute=1_000_000,
                audd_requests_per_minute=1_000_000,
            ),
            max_generations=0,
            novelty=False,
            recipe=recipe,
            paid_scan_adapters={"audd": audd},
            shazam_http_client=shazam,
            paid_sleep=no_backoff,
        )
    )
    media_dir, entry = _entry(work_root)
    return code, audd, shazam, entry, media_dir


def _run_free(tmp_path: Path) -> tuple[FakeAudD, FakeShazamHTTP, dict[str, object]]:
    script = SCRIPTS / "all-no-match.json"
    audd = FakeAudD(script)
    shazam = FakeShazamHTTP(script)
    work_root = tmp_path / "work"
    code = asyncio.run(
        cli._analyse(
            str(AUDIO),
            work_root=work_root,
            print_raw=False,
            refresh=False,
            max_requests=100,
            tracklist=None,
            no_hints=True,
            app_config=AppConfig(
                transforms_policy="off",
                recognise_concurrency=1,
                shazam_requests_per_minute=1_000_000,
                audd_requests_per_minute=1_000_000,
            ),
            max_generations=0,
            novelty=False,
            enabled_engines=("audd",),
            recipe=FREE_RECIPE,
            paid_scan_adapters={"audd": audd},
            shazam_http_client=shazam,
            paid_sleep=no_backoff,
        )
    )
    assert code == 0
    _media_dir, entry = _entry(work_root)
    return audd, shazam, entry


def test_frozen_recipes_contain_every_phase_zero_field_and_hash_canonical_json() -> None:
    assert FREE_RECIPE.primary_engine == "shazam"
    assert FREE_RECIPE.primary_density == 1
    assert FREE_RECIPE.primary_achieved_fraction == 0.80
    assert FREE_RECIPE.max_usd_e2 == 0
    assert dict(FREE_RECIPE.adapter_versions) == {"shazam": 1}
    assert FREE_RECIPE.algorithm_version == "fusion:2"
    assert FREE_RECIPE.requires == ("shazam_sweep",)

    assert DEEP_RECIPE.primary_engine == "audd"
    assert DEEP_RECIPE.secondary_engine == "shazam"
    assert DEEP_RECIPE.secondary_clips_per_minute == 2
    assert DEEP_RECIPE.secondary_reserve_fraction == 0.25
    assert DEEP_RECIPE.secondary_priority == (
        "hint_only",
        "listed_not_confident",
        "suppressed_challengeable",
        "blank",
    )
    assert DEEP_RECIPE.eligibility_min_intersection_ms == 4_000
    assert DEEP_RECIPE.suppressed_min_votes == 2
    assert DEEP_RECIPE.reserve_search_ms == 45_000
    assert DEEP_RECIPE.reserve_min_separation_ms == 30_000
    assert DEEP_RECIPE.overlap_min_ms == 6_000
    assert DEEP_RECIPE.separation_min_ms == 60_000
    assert DEEP_RECIPE.primary_achieved_fraction == 0.95
    assert DEEP_RECIPE.secondary_achieved_fraction == 0.80
    assert DEEP_RECIPE.anchor_max_ms == 86_400_000
    assert DEEP_RECIPE.anchor_slack_ms == 12_000
    assert DEEP_RECIPE.audd_concurrency == 4
    assert DEEP_RECIPE.max_usd_e2 == 900
    assert dict(DEEP_RECIPE.adapter_versions) == {"audd_clip": 2, "shazam": 1}
    assert DEEP_RECIPE.algorithm_version == "targeting:1,fusion:2"
    assert DEEP_RECIPE.requires == ("audd_sweep", "shazam_secondary")
    audd_retry = DEEP_RECIPE.retry_policy["audd"]
    assert audd_retry.retryable_outcomes == (
        "connect_error",
        "timeout_pre",
        "http_429",
        "http_503",
    )
    assert audd_retry.max_retries == 3 and audd_retry.backoff_seconds == (1, 2, 4)

    assert DEEP_RECIPE.recipe_id == sha256(DEEP_RECIPE.canonical_json.encode("utf-8")).hexdigest()
    assert replace(DEEP_RECIPE, overlap_min_ms=6_001).recipe_id != DEEP_RECIPE.recipe_id
    assert get_recipe("deep", primary_density=2).recipe_id != DEEP_RECIPE.recipe_id
    assert get_recipe("deep", primary_density=2).primary_density == 2
    with pytest.raises(TypeError):
        DEEP_RECIPE.adapter_versions["shazam"] = 2  # type: ignore[index]


def test_pricing_authority_and_app_config_loader_match_the_plan(tmp_path: Path) -> None:
    pricing = PricingConfig.load(ROOT / "pricing.toml")
    assert pricing.pricing_version == "v1"
    assert pricing.audd_usd_e6_per_request == 5_000
    assert pricing.bill_on_throttle is False
    assert pricing.free.minutes_per_week == pricing.free.max_mix_minutes == 150
    assert pricing.free.sources == ("soundcloud", "mixcloud")
    assert pricing.pro.price_gbp_month == 9 and pricing.pro.price_gbp_year == 90
    assert pricing.pro.deep_minutes_per_month == 90
    assert pricing.pro.max_mix_minutes == 240
    assert pricing.pro.sources == ("soundcloud", "mixcloud", "youtube", "upload")
    assert pricing.pack.small.gbp == 6 and pricing.pack.small.minutes == 55
    assert pricing.usd_cap_account_month_e2 == 2_000
    assert pricing.usd_cap_global_day_e2 == 5_000
    assert pricing.cache_hits_per_day == 20
    assert pricing.hints_max_age_days == pricing.alias_revalidate_days == 7
    assert pricing.serve_free_from_deep is False and pricing.compat_version == 1

    capped_pricing = tmp_path / "pricing.toml"
    capped_pricing.write_text(
        (ROOT / "pricing.toml")
        .read_text(encoding="utf-8")
        .replace("bill_on_throttle = false", "bill_on_throttle = false\nmax_usd_e2 = 0"),
        encoding="utf-8",
    )
    idea_config = tmp_path / "idea.toml"
    idea_config.write_text("[deep]\nprimary_density = 2\n", encoding="utf-8")
    loaded = AppConfig.load(idea_config, pricing_path=capped_pricing)
    assert loaded.pricing_version == "v1"
    assert loaded.audd_usd_e6_per_request == 5_000
    assert loaded.bill_on_throttle is False
    assert loaded.max_usd_e2 == 0
    assert loaded.deep_primary_density == 2
    with pytest.raises(FileNotFoundError):
        AppConfig.load(idea_config, pricing_path=tmp_path / "missing-pricing.toml")


def test_seven_window_reservation_is_36750_e6_and_four_cents() -> None:
    reservation = reserve_usd(
        planned=7,
        unit_usd_e6=5_000,
        recipe_max_usd_e2=900,
        configured_max_usd_e2=None,
    )
    assert reservation.usd_e6_reserved == 36_750
    assert reservation.usd_e2_reserved == 4
    assert reservation.effective_cap_e2 == 900

    with pytest.raises(BudgetExhausted):
        reserve_usd(
            planned=7,
            unit_usd_e6=5_000,
            recipe_max_usd_e2=3,
            configured_max_usd_e2=None,
        )


def test_admission_is_a_hard_cap_and_zero_cost_429_storm_refunds_units() -> None:
    assert {
        "match",
        "no_match",
        "timeout_post",
        "http_5xx",
        "malformed",
    } == BILLABLE_OUTCOMES
    assert {
        "connect_error",
        "timeout_pre",
        "http_429",
        "http_503",
        "auth_error",
        "quota_error",
    } == ZERO_COST_OUTCOMES

    async def storm() -> tuple[UsdAdmitter, FakeAudD, list[str]]:
        admitter = UsdAdmitter(
            reserve_usd(
                planned=7,
                unit_usd_e6=5_000,
                recipe_max_usd_e2=900,
                configured_max_usd_e2=None,
            )
        )
        fake = FakeAudD(SCRIPTS / "all-http-429.json")
        release = asyncio.Event()

        async def dispatch(index: int) -> str:
            async def admit_and_hold() -> None:
                admitter.admit()
                await release.wait()

            try:
                await fake.recognize_clip(Path(f"window-{index}.wav"), admit_and_hold)
            except ReservationExhausted:
                return "denied"
            except ProviderProtocolError:
                admitter.resolve("http_429")
                return "refunded"
            raise AssertionError("the 429 script returned without an error")

        tasks = [asyncio.create_task(dispatch(index)) for index in range(8)]
        await asyncio.sleep(0)
        assert fake.calls == 0
        assert admitter.admitted_units == 7
        release.set()
        return admitter, fake, list(await asyncio.gather(*tasks))

    admitter, fake, outcomes = asyncio.run(storm())
    assert outcomes.count("refunded") == 7
    assert outcomes.count("denied") == 1
    assert fake.calls == 7
    assert admitter.usd_e6_spent == 0
    assert admitter.usd_e6_remaining == 36_750
    assert admitter.refunded_units == 7
    settlement = admitter.settle()
    assert settlement.usd_e6_spent == settlement.usd_e2_spent == 0
    assert settlement.usd_e6_released == 36_750


def test_deep_success_records_price_recipe_reservation_and_settlement(tmp_path: Path) -> None:
    code, audd, _shazam, entry, _media_dir = _run_deep(tmp_path, "gate0a-deep.json")
    assert code == 0 and audd.calls == 7 and audd.billed_units == 7
    assert entry["usd_e6_reserved"] == 36_750
    assert entry["usd_e2_reserved"] == 4
    assert entry["usd_e6_spent"] == 35_000
    assert entry["usd_e2_spent"] == 4
    assert entry["costs"] == {"usd_e2": 4}
    assert entry["requested_recipe_id"] == DEEP_RECIPE.recipe_id
    assert entry["algorithm_version"] == "targeting:1,fusion:2"
    assert entry["pricing_version"] == "v1"
    assert entry["audd_usd_e6_per_request"] == 5_000


def test_deep_density_two_reserves_and_dispatches_even_frozen_windows(tmp_path: Path) -> None:
    recipe = get_recipe("deep", primary_density=2)
    code, audd, _shazam, entry, _media_dir = _run_deep(
        tmp_path,
        "gate0a-deep.json",
        recipe=recipe,
    )
    assert code == 0 and audd.calls == 4
    assert [path.name for path in audd.paths] == [
        "0000000000-none.wav",
        "0000018000-none.wav",
        "0000036000-none.wav",
        "0000048000-none.wav",
    ]
    assert entry["usd_e6_reserved"] == 21_000
    assert entry["usd_e6_spent"] == 20_000
    assert entry["requested_recipe_id"] == recipe.recipe_id


def test_free_recipe_forbids_paid_calls_and_journals_zero_money(tmp_path: Path) -> None:
    audd, shazam, entry = _run_free(tmp_path)
    assert audd.calls == 0
    assert shazam.requests == 7
    assert entry["requested_recipe_id"] == FREE_RECIPE.recipe_id
    assert entry["algorithm_version"] == "fusion:2"
    assert entry["usd_e6_reserved"] == entry["usd_e6_spent"] == 0
    assert entry["usd_e2_reserved"] == entry["usd_e2_spent"] == 0


@pytest.mark.parametrize("cap", [0, 3])
def test_reservation_over_effective_cap_exits_four_without_provider_attempts(
    tmp_path: Path, cap: int
) -> None:
    config = AppConfig(
        transforms_policy="off",
        recognise_concurrency=1,
        shazam_requests_per_minute=1_000_000,
        max_usd_e2=cap,
    )
    code, audd, shazam, entry, media_dir = _run_deep(
        tmp_path, "gate0a-deep.json", app_config=config
    )
    assert code == 4
    assert entry["status"] == "budget_exhausted"
    assert entry["reason"] == "reservation_exceeds_cap"
    assert entry["usd_e6_reserved"] == entry["usd_e6_spent"] == 0
    assert entry["usd_e2_reserved"] == entry["usd_e2_spent"] == 0
    assert audd.calls == shazam.requests == 0
    assert not (media_dir / "present" / "index.html").exists()


def test_paid_outcome_costs_refund_zero_cost_units_and_bill_ambiguous(tmp_path: Path) -> None:
    code, audd, _shazam, entry, _media_dir = _run_deep(tmp_path, "money-refunds.json")
    # 429, 503 and timeout_pre refund (and are retried until the 401 halts the sweep — how many
    # retries land before that depends on the four workers' interleaving); timeout_post and
    # no_match bill; the 401 on the sixth window is terminal-provider (cost 0) and stops the
    # sweep, so the seventh is never dispatched.
    assert code == 0
    assert entry["counts"]["paid_requests"] == 6  # type: ignore[index]
    assert entry["counts"]["paid_attempts"] == audd.calls >= 6  # type: ignore[index]
    assert audd.billed_units == 2
    assert entry["status"] == "partial"
    assert entry["reason"] == "provider_unavailable_midrun"
    assert entry["usd_e6_reserved"] == 36_750
    assert entry["usd_e6_spent"] == 10_000
    assert entry["usd_e2_spent"] == 1
    assert entry["counts"]["paid_billable_units"] == 2  # type: ignore[index]


@pytest.mark.parametrize(
    ("transport_error", "expected", "wording"),
    [
        (httpx.ConnectError, ProviderUnavailable, "connection failed"),
        (httpx.ConnectTimeout, ProviderUnavailable, "timeout"),
        (httpx.ReadTimeout, AmbiguousProviderOutcome, "lost"),
    ],
)
def test_audd_transport_distinguishes_free_pre_dispatch_from_billable_lost_response(
    transport_error: type[httpx.TransportError],
    expected: type[Exception],
    wording: str,
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise transport_error("scripted transport failure", request=request)

    adapter = AudDAdapter(
        AudDCredentials("fixture"),
        AppConfig(),
        False,
        transport=httpx.MockTransport(fail),
    )

    async def invoke() -> None:
        async def admitted() -> None:
            return None

        await adapter.recognize_clip(AUDIO, on_attempt=admitted)

    with pytest.raises(expected) as raised:
        asyncio.run(invoke())
    # `timeout_pre` and `connect_error` are both free, but must stay distinguishable for the
    # attempt journal and 0b-i's retry policy.
    assert wording in str(raised.value).casefold()


def test_primary_stops_and_journals_partial_when_admission_cannot_dispatch(
    tmp_path: Path, monkeypatch
) -> None:
    real_reserve = reserve_usd

    def undersized_reservation(**kwargs: object):
        return real_reserve(
            planned=1,
            unit_usd_e6=int(kwargs["unit_usd_e6"]),
            recipe_max_usd_e2=int(kwargs["recipe_max_usd_e2"]),
            configured_max_usd_e2=None,
        )

    monkeypatch.setattr(cli, "reserve_usd", undersized_reservation)
    code, audd, shazam, entry, _media_dir = _run_deep(tmp_path, "gate0a-deep.json")
    assert code == 0
    assert audd.calls == 1
    assert shazam.requests == 2  # the Deep secondary still probes the blank remainder
    assert entry["status"] == "partial"
    assert entry["reason"] == "reservation_exhausted"
    assert entry["usd_e6_reserved"] == 5_250
    assert entry["usd_e6_spent"] == 5_000
    assert entry["counts"]["paid_requests"] == 1  # type: ignore[index]
    assert entry["counts"]["paid_failures"] == 0  # type: ignore[index]


def test_recipe_cli_selects_deep_and_retires_max_paid_clips(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_analyse(_url: str, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_analyse", fake_analyse)
    runner = CliRunner()
    selected = runner.invoke(cli.app, ["analyse", "http://example/set", "--recipe", "deep"])
    assert selected.exit_code == 0, selected.output
    assert captured["recipe"] == DEEP_RECIPE
    assert captured["primary_engine"] == "audd"

    free_selected = runner.invoke(cli.app, ["analyse", "http://example/set", "--recipe", "free"])
    assert free_selected.exit_code == 0, free_selected.output
    assert captured["recipe"] == FREE_RECIPE
    assert captured["primary_engine"] == "shazam"

    invalid = runner.invoke(cli.app, ["analyse", "http://example/set", "--recipe", "turbo"])
    assert invalid.exit_code == 2
    assert "choose free or deep" in invalid.output
    retired = runner.invoke(cli.app, ["analyse", "http://example/set", "--max-paid-clips", "10"])
    assert retired.exit_code == 2


def test_pricing_authority_is_found_from_any_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert DEFAULT_PRICING_PATH == ROOT / "pricing.toml"
    monkeypatch.chdir(tmp_path)
    assert load_pricing() == PricingConfig.load(ROOT / "pricing.toml")
    defaults = AppConfig.load(None)
    assert defaults.audd_usd_e6_per_request == 5_000
    assert defaults.pricing_version == "v1"
    assert defaults.max_usd_e2 is None
    assert defaults.deep_primary_density == 1


def test_pricing_loader_refuses_unknown_or_malformed_fields(tmp_path: Path) -> None:
    authority = (ROOT / "pricing.toml").read_text(encoding="utf-8")
    unknown = tmp_path / "unknown.toml"
    unknown.write_text(f"audd_usd_e6_per_clip = 5000\n{authority}", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown pricing field: audd_usd_e6_per_clip"):
        PricingConfig.load(unknown)

    malformed = tmp_path / "malformed.toml"
    malformed.write_text(
        authority.replace("audd_usd_e6_per_request = 5000", 'audd_usd_e6_per_request = "5000"'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="audd_usd_e6_per_request must be a positive integer"):
        PricingConfig.load(malformed)

    incomplete = tmp_path / "incomplete.toml"
    incomplete.write_text(authority.replace("deep_minutes_per_month = 90\n", ""), encoding="utf-8")
    with pytest.raises(ValueError, match="pro.deep_minutes_per_month"):
        PricingConfig.load(incomplete)


def test_throttled_run_refunds_every_unit_and_bills_nothing(tmp_path: Path) -> None:
    """A 429 storm resolves nothing and bills nothing: plan §2.3.5 makes it a ``partial`` Deep run
    (AudD 0 of 7 < 95 %), not a silent Free one."""

    code, audd, shazam, entry, media_dir = _run_deep(tmp_path, "all-http-429.json")
    assert code == 0
    assert entry["status"] == "partial"
    assert entry["reason"] == "primary_not_achieved"
    assert entry["achieved"] == "deep"
    assert entry["algorithm_version"] == "targeting:1,fusion:2"
    # Every planned dispatch and each of its three bounded retries was admitted; none was refused.
    assert audd.calls == 28
    assert audd.billed_units == 0
    assert shazam.requests == 2  # only the Deep recipe's own bounded secondary, never a free sweep
    assert entry["usd_e6_reserved"] == 36_750
    assert entry["usd_e2_reserved"] == 4
    assert entry["usd_e6_spent"] == entry["usd_e2_spent"] == 0
    assert entry["costs"] == {"usd_e2": 0}
    assert entry["counts"]["paid_billable_units"] == 0  # type: ignore[index]
    raw = media_dir / "recognise" / "invocations" / "live-audd-clip-v1" / "raw"
    assert not list(raw.glob("*.json")) if raw.is_dir() else True


def test_pricing_authority_falls_back_to_the_packaged_copy_without_a_checkout(
    tmp_path: Path,
) -> None:
    """An installed wheel has no repository root; it must still find the pricing authority."""

    checkout = tmp_path / "repo" / "src" / "id_detector"
    checkout.mkdir(parents=True)
    repository_authority = tmp_path / "repo" / "pricing.toml"
    repository_authority.write_text("pricing_version = 'v1'\n", encoding="utf-8")
    assert _resolve_pricing_path(checkout / "pricing.py") == repository_authority.resolve()

    installed = tmp_path / "site-packages" / "id_detector"
    (installed / "resources").mkdir(parents=True)
    assert (
        _resolve_pricing_path(installed / "pricing.py")
        == (installed / "resources" / "pricing.toml").resolve()
    )


def test_legacy_max_accuracy_profile_never_starts_paid_work_without_an_explicit_recipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare `--profile max_accuracy` spent nothing before recipes; it must still spend nothing."""

    captured: dict[str, object] = {}

    async def fake_analyse(_url: str, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_analyse", fake_analyse)
    runner = CliRunner()
    bare = runner.invoke(cli.app, ["analyse", "http://example/set", "--profile", "max_accuracy"])
    assert bare.exit_code == 0, bare.output
    assert captured["recipe"] == FREE_RECIPE
    assert captured["primary_engine"] == "shazam"

    # The pre-v2 paid invocation (profile + an explicit paid engine) keeps its paid-first behaviour.
    opted_in = runner.invoke(
        cli.app,
        ["analyse", "http://example/set", "--profile", "max_accuracy", "--engine", "audd"],
    )
    assert opted_in.exit_code == 0, opted_in.output
    assert captured["recipe"] == DEEP_RECIPE

    # --engine on a free-recipe run is dropped, and says so rather than pretending it ran.
    dropped = runner.invoke(cli.app, ["analyse", "http://example/set", "--engine", "audd"])
    assert dropped.exit_code == 0, dropped.output
    assert captured["recipe"] == FREE_RECIPE
    assert "--engine is ignored by the free recipe" in dropped.output


def test_free_recipe_reaches_no_paid_call_path_even_with_consent_and_engines_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`max_usd_e2 = 0` forbids the clip path AND the residual whole-file scanner path."""

    def forbidden(*_args: object, **_kwargs: object):
        raise AssertionError("the free recipe reached a paid engine")

    assert not hasattr(cli, "run_paid_scanners")  # the whole-file call site is gone (0a-iii)
    monkeypatch.setattr(cli, "run_paid_clip_recognition", forbidden)
    script = SCRIPTS / "all-no-match.json"
    audd = FakeAudD(script)
    work_root = tmp_path / "work"
    code = asyncio.run(
        cli._analyse(
            str(AUDIO),
            work_root=work_root,
            print_raw=False,
            refresh=False,
            max_requests=100,
            tracklist=None,
            no_hints=True,
            app_config=AppConfig(
                transforms_policy="off",
                recognise_concurrency=1,
                shazam_requests_per_minute=1_000_000,
                allow_third_party_upload=True,
            ),
            max_generations=0,
            novelty=False,
            # Consent open, both paid engine families enabled, and the paid-first primary asked for.
            enabled_engines=("shazam", "audd", "acrcloud"),
            cli_confirmation=True,
            primary_engine="audd",
            recipe=FREE_RECIPE,
            paid_scan_adapters={"audd": audd},
            shazam_http_client=FakeShazamHTTP(script),
        )
    )
    assert code == 0
    assert audd.calls == 0
    _media_dir, entry = _entry(work_root)
    assert entry["usd_e6_reserved"] == entry["usd_e6_spent"] == 0
    assert entry["costs"] == {"usd_e2": 0}


def test_frozen_profile_usd_budget_never_caps_a_deep_run(tmp_path: Path) -> None:
    """E-S3: both frozen profiles carry `budget.max_usd_e2 = 0`; the clip path never reads it."""

    from id_detector.profiles import load_profile, profile_app_config

    frozen = load_profile(cli.PROJECT_ROOT, "max_accuracy")
    assert frozen.budget.max_usd_e2 == 0
    config = profile_app_config(frozen)
    assert config.max_usd_e2 is None
    code, audd, _shazam, entry, _media_dir = _run_deep(
        tmp_path,
        "gate0a-deep.json",
        app_config=replace(
            config,
            transforms_policy="off",
            recognise_concurrency=1,
            shazam_requests_per_minute=1_000_000,
            rescan_max_generations=0,
        ),
    )
    assert code == 0 and audd.calls == 7
    assert entry["status"] != "budget_exhausted"
    assert entry["usd_e6_spent"] == 35_000


def test_protocol_outcome_parses_the_status_code_instead_of_substring_matching() -> None:
    assert _protocol_outcome(ProviderProtocolError("AudD HTTP 429")) == "http_429"
    assert _protocol_outcome(ProviderProtocolError("AudD HTTP 503")) == "http_503"
    assert _protocol_outcome(ProviderProtocolError("AudD HTTP 500")) == "http_5xx"
    assert _protocol_outcome(ProviderProtocolError("AudD HTTP 401")) == "auth_error"
    assert _protocol_outcome(ProviderProtocolError("AudD HTTP 402")) == "quota_error"
    assert _protocol_outcome(ProviderProtocolError("AudD returned a non-JSON response")) == (
        "malformed"
    )
    # A billable outcome is never refunded because unrelated digits happen to spell a free code.
    assert _protocol_outcome(ProviderProtocolError("AudD response root is not an object")) == (
        "malformed"
    )
    assert _protocol_outcome(ProviderProtocolError("AudD clip 5031-byte body was truncated")) == (
        "malformed"
    )


def test_settlement_charges_dispatched_units_that_never_resolved_and_is_idempotent() -> None:
    admitter = UsdAdmitter(
        reserve_usd(
            planned=3,
            unit_usd_e6=5_000,
            recipe_max_usd_e2=900,
            configured_max_usd_e2=None,
        )
    )
    admitter.admit()
    admitter.admit()
    admitter.resolve("match")
    # The second unit was dispatched and never resolved: ambiguous, so it settles as spent.
    settlement = admitter.settle()
    assert settlement.usd_e6_spent == 10_000
    assert settlement.usd_e2_spent == 1
    assert settlement.usd_e6_reserved == 15_750
    assert settlement.usd_e6_released == 5_750
    assert admitter.settle() == settlement
    with pytest.raises(RuntimeError):
        admitter.admit()

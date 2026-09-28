"""The last rough edges of the local product, each pinned by an offline test.

* a Shazam window that came back as a provider error is asked once more at the end of the sweep,
  inside the same rate limit, breaker, request budget and attempt journal;
* MixesDB's wiki emphasis quotes are stripped and a quoted "ID" stays a placeholder;
* with no AudD token, Deep / "Max accuracy" / ``idea cost`` say plainly that paid recognition is
  not set up and the Free result is the final one; a token AudD refuses stops the paid sweep as a
  definite, no-charge refusal;
* the Local Free golden is compared against the bytes actually produced;
* a separate process killed mid-approval never authorises more than one paid run.

Every provider here is a scripted fake and ``AUDD_API_TOKEN`` is empty: nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from id_detector import cli
from id_detector.attempts import load_attempt_ledger
from id_detector.io import read_text
from id_detector.providers.base import AppConfig
from id_detector.recipes import FREE_RECIPE, Recipe
from id_detector.shazam_breaker import ShazamBreaker
from tests.fakes.providers import FakeAudD, FakeShazamHTTP, no_backoff
from tests.test_cost_preview import cached  # noqa: F401  (the Free-scanned fixture mix)

ROOT = Path(__file__).resolve().parents[1]
AUDIO = ROOT / "tests" / "fixtures" / "audio" / "tone-60s.wav"


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDD_API_TOKEN", "")
    monkeypatch.delenv("IDEA_ENGINE_SHAZAM", raising=False)


def _analyse(
    work: Path,
    recipe: Recipe,
    script: dict,
    *,
    max_requests: int = 100,
    breaker: ShazamBreaker | None = None,
    paid: bool = True,
    confirm=None,
) -> tuple[int, FakeAudD, FakeShazamHTTP, dict, Path]:
    audd, shazam = FakeAudD(script), FakeShazamHTTP(script)
    code = asyncio.run(
        cli._analyse(
            str(AUDIO),
            work_root=work,
            print_raw=False,
            refresh=False,
            max_requests=max_requests,
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
            recipe=recipe,
            paid_scan_adapters={"audd": audd} if paid else None,
            shazam_http_client=shazam,
            shazam_breaker=breaker,
            paid_sleep=no_backoff,
            cli_paid_confirm=confirm,
        )
    )
    (journal,) = work.rglob("invocations.jsonl")
    entries = [json.loads(line) for line in read_text(journal).splitlines() if line]
    return code, audd, shazam, entries[-1], journal.parent


def _shazam_ledger(media: Path):
    (path,) = (media / "recognise").glob("*shazam*attempts.jsonl")
    return load_attempt_ledger(path)


# --------------------------------------------------------------------------------------------------
# 1. A window that got a provider error is asked once more, at the end of the sweep
# --------------------------------------------------------------------------------------------------
#: Window 1 errors then answers; window 4 errors twice; window 5's credential is refused.
RETRY_SCRIPT = {
    "shazam": {
        "default": "match",
        "windows": {"1": ["malformed", "match"], "4": ["malformed", "malformed"], "5": "http_401"},
    }
}


def test_an_errored_window_is_retried_once_at_the_end_of_the_sweep(tmp_path: Path) -> None:
    code, _audd, shazam, entry, media = _analyse(tmp_path / "w", FREE_RECIPE, RETRY_SCRIPT)
    assert code == 0, entry
    # Seven windows in the sweep, then ONE more request for each of windows 1 and 4 -- never for
    # the refused credential (window 5), never twice for the window whose retry also failed.
    assert shazam.requests == 9
    assert [item["window"] for item in shazam.attempts[7:]] == [1, 4]
    assert [item["outcome"] for item in shazam.attempts[7:]] == ["match", "malformed"]
    # The retry is in the attempt journal like any other attempt: a new ordinal, parented on the
    # failure it retries, resolved with its own outcome.
    ledger = _shazam_ledger(media)
    retried = [attempt for attempt in ledger.attempts if attempt.parent_attempt_id is not None]
    assert len(retried) == 2
    by_id = {attempt.attempt_id: attempt for attempt in ledger.attempts}
    assert sorted(
        (by_id[attempt.parent_attempt_id].outcome, attempt.outcome) for attempt in retried
    ) == [("malformed", "malformed"), ("malformed", "match")]
    assert all(attempt.ordinal == 1 for attempt in retried)
    # The window whose retry answered is evidence now; the other two stay errors.
    assert entry["counts"]["failures"] == 2


def test_the_end_of_sweep_retry_never_goes_past_the_request_budget(tmp_path: Path) -> None:
    # A ceiling of exactly the sweep's seven requests: the retry is never allowed a request.
    code, _audd, shazam, entry, _media = _analyse(
        tmp_path / "w", FREE_RECIPE, RETRY_SCRIPT, max_requests=7
    )
    assert code == 0, entry
    assert shazam.requests == 7
    assert entry["counts"]["failures"] == 3


class _TripsOnFirstFailure(ShazamBreaker):
    """A breaker that opens as soon as one failure resolves (the free primary still runs on)."""

    tripped = False

    def reason(self) -> str | None:
        return "shazam_rate_open" if self.tripped else super().reason()

    def resolved(self, outcome: str) -> None:
        super().resolved(outcome)
        if outcome not in {"match", "no_match"}:
            self.tripped = True


def test_the_end_of_sweep_retry_never_goes_around_an_open_breaker(tmp_path: Path) -> None:
    code, _audd, shazam, entry, _media = _analyse(
        tmp_path / "w", FREE_RECIPE, RETRY_SCRIPT, breaker=_TripsOnFirstFailure()
    )
    assert code == 0, entry
    assert shazam.requests == 7  # the running free sweep finished; no retry past the breaker


# --------------------------------------------------------------------------------------------------
# 2. MixesDB "ID" rows lose their wiki quote marks and stay placeholders
# --------------------------------------------------------------------------------------------------
#: Real-shaped MixesDB wikitext: lines copied from the owner's own mixes' MixesDB pages (MixesDB
#: sets an unidentified entry in wiki italics, ''...''), plus one bold and one double-quoted row.
MIXESDB_WIKITEXT = """{{Player|url=https://soundcloud.com/example/set}}
== Tracklist ==
# [0:40:00] DJ HEARTSTRING & Southstar - Don't Stop [Teenage Dreams]
# [0:43:40] ''DJ HEARTSTRING - ID''
# [0:47:30] jozif - Lady B's Lullaby [Culprit - CP 022]
# [1:04:30] ''Sambaboys - ?''
# [1:06:00] Fred Again.. - Angie (I've Been Lost) [Atlantic (Warner Music)]''
# [1:10:00] '''Benwal - ID (Benwal Remix)'''
# [1:12:00] "Unknown Artist - ID"
# [1:14:00] BURNR - Rockin' Steady [Rapid Trax]
# [1:16:00] ''ID - ID''
# [1:18:00] ?
== Comments ==
"""


def _mixesdb_hints():
    from id_detector.hints.connectors.mixesdb import parse_wikitext
    from id_detector.hints.parse import parse_hint_inputs

    output = parse_wikitext(MIXESDB_WIKITEXT, page_id="42")
    return parse_hint_inputs("a" * 64, 6_000_000, list(output.inputs))


def test_mixesdb_wiki_quotes_are_stripped_and_apostrophes_in_names_are_kept() -> None:
    rows = [(hint.artist, hint.title) for hint in _mixesdb_hints()]
    assert rows == [
        ("DJ HEARTSTRING & Southstar", "Don't Stop"),
        ("DJ HEARTSTRING", "ID"),
        ("jozif", "Lady B's Lullaby"),
        ("Sambaboys", "?"),
        ("Fred Again..", "Angie (I've Been Lost)"),
        ("Benwal", "ID"),
        ("Unknown Artist", "ID"),
        ("BURNR", "Rockin' Steady"),
        ("ID", "ID"),
        (None, "?"),
    ]
    for hint in _mixesdb_hints():
        assert "''" not in hint.raw_text  # wiki markup never reaches the stored source line
        for text in (hint.artist, hint.title):
            assert text is None or ("''" not in text and '"' not in text)


def test_a_quoted_id_stays_a_placeholder_and_never_becomes_a_track_called_id() -> None:
    from id_detector.hints.relations import _identity_key

    hints = _mixesdb_hints()
    placeholders = {(hint.artist, hint.title) for hint in hints if hint.flags.id_unknown}
    assert placeholders == {
        ("DJ HEARTSTRING", "ID"),
        ("Sambaboys", "?"),
        ("Benwal", "ID"),
        ("Unknown Artist", "ID"),
        ("ID", "ID"),
        (None, "?"),
    }
    for hint in hints:
        if hint.flags.id_unknown:
            assert hint.identity_specificity == 0 and _identity_key(hint) is None
        else:  # a named track keeps its identity
            assert hint.identity_specificity == 10_000 and _identity_key(hint) is not None


def test_wiki_emphasis_follows_mediawiki_and_only_wrapping_quotes_are_removed() -> None:
    from id_detector.hints.connectors.mixesdb import strip_wiki_emphasis
    from id_detector.hints.parse import _unquote

    assert strip_wiki_emphasis("''A - B''") == "A - B"
    assert strip_wiki_emphasis("'''''A - B'''''") == "A - B"
    assert strip_wiki_emphasis("Rock ''''n'''' Roll") == "Rock 'n' Roll"  # four = ' + bold
    assert strip_wiki_emphasis("Don't Stop") == "Don't Stop"
    assert _unquote('"ID"') == "ID"
    assert _unquote("\u201cID\u201d") == "ID"
    assert _unquote("'ID'") == "ID"
    assert _unquote("'Round Midnight") == "'Round Midnight"
    assert _unquote("Rockin'") == "Rockin'"
    assert _unquote("Rock 'n' Roll") == "Rock 'n' Roll"
    assert _unquote('"') == '"' and _unquote('""') == '""'


# --------------------------------------------------------------------------------------------------
# 3. Paid recognition not set up, or a token AudD no longer accepts
# --------------------------------------------------------------------------------------------------
DEEP_SCRIPT = json.loads((ROOT / "tests/fakes/scripts/gate0a-deep.json").read_text("utf-8"))


def _cli_deep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fakes: str, script: dict):
    from typer.testing import CliRunner

    path = tmp_path / "script.json"
    path.write_text(json.dumps(script), encoding="utf-8")
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    monkeypatch.setenv("IDEA_FAKE_SCRIPT", str(path))
    work = tmp_path / "scan"
    result = CliRunner().invoke(
        cli.app,
        [
            "analyse",
            str(AUDIO),
            "--recipe",
            "deep",
            "--work-root",
            str(work),
            "--config",
            str(tmp_path / "idea.toml"),
            "--no-hints",
            "--yes",
            "--fake-providers",
            fakes,
        ],
    )
    (journal,) = work.rglob("invocations.jsonl")
    entry = json.loads(read_text(journal).splitlines()[-1])
    return result, entry, work


def _audd_events(work: Path) -> list[dict]:
    return [
        event
        for path in work.rglob("*attempts*.jsonl")
        for event in (json.loads(line) for line in read_text(path).splitlines() if line)
        if event.get("provider") == "audd"
    ]


def test_deep_without_a_token_says_plainly_that_the_free_result_is_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, entry, work = _cli_deep(tmp_path, monkeypatch, "shazam", DEEP_SCRIPT)
    assert result.exit_code == 3, (result.output, result.exception)
    assert (entry["status"], entry["reason"]) == ("provider_unavailable", "not_configured")
    assert "Paid recognition is not set up (no AudD token is configured)" in result.output
    assert "the Free result is the final one" in result.output
    assert "Your Free result is saved" in result.output
    assert "not_configured" not in result.output  # no internal code in the owner's words
    assert "Spend about" not in result.output  # never asked to approve what cannot be bought
    assert entry["usd_e6_reserved"] == entry["usd_e6_spent"] == 0
    assert not _audd_events(work)


def test_idea_cost_says_when_paid_recognition_is_not_set_up(
    cached,  # noqa: F811  (the Free-scanned fixture mix)
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    work, key = cached
    args = [
        "cost",
        key,
        "--work-root",
        str(work),
        "--config",
        str(work / "idea.toml"),
    ]
    monkeypatch.setenv("AUDD_API_TOKEN", "")
    output = CliRunner().invoke(cli.app, args).output
    assert "Paid recognition is not set up (no AudD token is configured)" in output
    assert "the Free result is the final one" in output
    # A placeholder credential (never sent anywhere: `idea cost` makes no request at all).
    monkeypatch.setenv("AUDD_API_TOKEN", "placeholder-not-a-token")
    output = CliRunner().invoke(cli.app, args).output
    assert "not set up" not in output and "Deep would check" in output


class _RefusingAudD:
    """AudD answering every clip with one of its documented account/token error bodies."""

    def __init__(self, code: int) -> None:
        self.code = code
        self.calls = 0

    async def recognize_clip(self, path: Path, on_attempt) -> dict:
        await on_attempt()
        self.calls += 1
        return {
            "status": "error",
            "error": {"error_code": self.code, "error_message": "fixture refusal"},
            "request_params": {},
        }


def _deep_run(work: Path, audd) -> tuple[int, dict]:
    from id_detector.recipes import DEEP_RECIPE

    exit_code = asyncio.run(
        cli._analyse(
            str(AUDIO),
            work_root=work,
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
            recipe=DEEP_RECIPE,
            paid_scan_adapters={"audd": audd},
            shazam_http_client=FakeShazamHTTP(DEEP_SCRIPT),
            paid_sleep=no_backoff,
            cli_paid_confirm=lambda _plan: True,
        )
    )
    (journal,) = work.rglob("invocations.jsonl")
    return exit_code, json.loads(read_text(journal).splitlines()[-1])


@pytest.mark.parametrize(
    ("code", "reason", "journalled", "billed"),
    [
        # The classification this code base already had (AudD's documented 900 / 901): zero cost.
        (900, "auth_error", "auth_error", False),
        (901, "quota_error", "quota_error", False),
        # Codes whose billing AudD does not document: counted as spent, never retried, sweep stops.
        (902, "quota_error", "malformed", True),
        (903, "auth_error", "malformed", True),
        (904, "auth_error", "malformed", True),
        (905, "auth_error", "malformed", True),
        (19, "blocked", "malformed", True),
        (31337, "blocked", "malformed", True),
        (611, "rate_limited", "malformed", True),
    ],
)
def test_a_token_audd_refuses_stops_the_paid_sweep_at_once(
    tmp_path: Path, code: int, reason: str, journalled: str, billed: bool
) -> None:
    from id_detector.paid_clip import paid_stop_words
    from id_detector.recipes import DEEP_RECIPE

    refusing = _RefusingAudD(code)
    exit_code, entry = _deep_run(tmp_path / "w", refusing)
    work = tmp_path / "w"
    # The sweep stops at once -- never retried clip after clip (the gap has seven clips; at most
    # the recipe's in-flight clips are ever sent).
    assert exit_code == 3
    assert (entry["status"], entry["reason"]) == ("provider_unavailable", reason)
    assert 1 <= refusing.calls <= (DEEP_RECIPE.audd_concurrency or 1) < 7
    assert entry["counts"]["paid_refusal_code"] == code
    events = _audd_events(work)
    resolved = [event for event in events if event["event"] == "resolved"]
    dispatched = [event for event in events if event["event"] == "dispatched"]
    assert len(resolved) == len(dispatched) == refusing.calls  # one attempt each, none retried
    assert {event["outcome"] for event in resolved} == {journalled}
    # A refusal whose billing AudD does not document is counted as spent: one unit per request.
    assert entry["usd_e6_spent"] == (5_000 * refusing.calls if billed else 0)
    words = paid_stop_words(reason, code, entry["usd_e6_spent"])
    assert words is not None and f"AudD error {code}" in words
    assert "Free result is the final one" in words
    if billed:
        assert "of AudD credit was counted as spent" in words and "Nothing was spent" not in words
    else:
        assert "Nothing was spent." in words


class _RefusalBesideAChargedSibling:
    """Two requests genuinely in flight at once: the first is still waiting for its answer when
    the second is refused (900, zero cost); the first then loses its answer (``timeout_post``, which
    is charged). Every later request is refused too."""

    def __init__(self) -> None:
        self.calls = 0
        self.first_in_flight = asyncio.Event()
        self.refused = asyncio.Event()
        self.max_in_flight = 0
        self._in_flight = 0

    async def recognize_clip(self, path: Path, on_attempt) -> dict:
        from id_detector.providers.base import AmbiguousProviderOutcome

        await on_attempt()
        self.calls += 1
        number = self.calls
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            if number == 1:
                self.first_in_flight.set()
                await asyncio.wait_for(self.refused.wait(), 30)
                raise AmbiguousProviderOutcome("AudD clip response was lost")
            await asyncio.wait_for(self.first_in_flight.wait(), 30)
            self.refused.set()
            await asyncio.sleep(0)
            return {"status": "error", "error": {"error_code": 900, "error_message": "refused"}}
        finally:
            self._in_flight -= 1


def test_a_refusal_beside_a_charged_in_flight_request_states_the_real_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import runpy

    from typer.testing import CliRunner

    mixed = _RefusalBesideAChargedSibling()
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(DEEP_SCRIPT), encoding="utf-8")
    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    monkeypatch.setenv("IDEA_FAKE_SCRIPT", str(script_path))
    # The CLI's own fake-provider loader, handing it this adapter instead of the scripted one.
    monkeypatch.setattr(
        runpy,
        "run_path",
        lambda _path: {
            "load_fake_providers": lambda _script, _names: (mixed, FakeShazamHTTP(DEEP_SCRIPT))
        },
    )
    work = tmp_path / "scan"
    result = CliRunner().invoke(
        cli.app,
        [
            "analyse",
            str(AUDIO),
            "--recipe",
            "deep",
            "--work-root",
            str(work),
            "--config",
            str(tmp_path / "idea.toml"),
            "--no-hints",
            "--yes",
            "--fake-providers",
            "audd,shazam",
        ],
    )
    (journal,) = work.rglob("invocations.jsonl")
    entry = json.loads(read_text(journal).splitlines()[-1])
    assert result.exit_code == 3, (result.output, result.exception)
    assert mixed.max_in_flight >= 2  # genuinely concurrent
    assert (entry["status"], entry["reason"]) == ("provider_unavailable", "auth_error")
    # The in-flight sibling's lost answer is charged; the refusal is not.
    outcomes = sorted(e["outcome"] for e in _audd_events(work) if e["event"] == "resolved")
    assert outcomes.count("timeout_post") == 1 and set(outcomes) == {"auth_error", "timeout_post"}
    assert entry["usd_e6_spent"] == 5_000
    # The owner is shown the real, non-zero amount -- never "nothing was spent".
    assert "$0.01 of AudD credit was counted as spent" in result.output
    assert "othing was spent" not in result.output


def test_the_cli_names_audds_own_refusal_in_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = {**DEEP_SCRIPT, "audd": {"default": "quota_error", "windows": {}}}
    result, entry, _work = _cli_deep(tmp_path, monkeypatch, "audd,shazam", script)
    assert result.exit_code == 3, (result.output, result.exception)
    assert (entry["status"], entry["reason"]) == ("provider_unavailable", "quota_error")
    assert "AudD says the account has no credit left (AudD error 402)" in result.output
    assert "Nothing was spent." in result.output
    assert "quota_error" not in result.output
    assert entry["usd_e6_spent"] == 0


@pytest.mark.parametrize(
    ("audd_outcome", "reason", "heading_words"),
    [
        (None, "not_configured", "Paid recognition is not set up"),
        ("quota_error", "quota_error", "AudD says the account has no credit left"),
    ],
)
def test_max_accuracy_shows_the_free_result_with_plain_words_and_no_approve_button(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    audd_outcome: str | None,
    reason: str,
    heading_words: str,
) -> None:
    from id_detector.webapp.runner import make_pipeline_runner
    from idea_web.jobs.local import LocalWorker, local_database
    from tests.test_additive_deep import _local
    from tests.test_additive_deep import _rows as rows

    script = dict(DEEP_SCRIPT)
    if audd_outcome is not None:
        script["audd"] = {"default": audd_outcome, "windows": {}}
    config, jobs = _local(tmp_path, monkeypatch)
    monkeypatch.setenv("AUDD_API_TOKEN", "")
    job_id = jobs.submit(str(AUDIO), "max_accuracy")
    audd = FakeAudD(script) if audd_outcome is not None else None

    def run_once() -> None:
        LocalWorker(
            local_database(tmp_path),
            tmp_path,
            make_pipeline_runner(
                tmp_path,
                project_root=ROOT,
                config_path=config,
                paid_scan_adapters={"audd": audd} if audd is not None else None,
                shazam_http_client=FakeShazamHTTP(script),
                paid_sleep=no_backoff,
            ),
            flush_seconds=0.05,
        ).run_once()

    run_once()
    if audd is not None:
        # A configured token: the price is offered first, and the approved job meets the refusal.
        assert jobs.get(job_id).paid_offer is not None
        job_id = jobs.approve_offer(job_id)
        assert job_id is not None
        run_once()
    job = jobs.get(job_id)
    # The Free result is shown, with the reason in words -- not a failure, not an offer.
    assert job.status == "succeeded", job.error
    assert job.paid_offer is None
    assert job.paid_notice is not None and job.paid_notice["reason"] == reason
    assert heading_words in job.paid_notice["words"]
    assert "Free result is the final one" in job.paid_notice["words"]
    status = job.status_dict()
    assert status["paid_notice"] == job.paid_notice and status["result_url"]
    assert rows(jobs, "run_reservations") == 0 or audd_outcome is not None
    if audd is not None:
        assert audd.calls <= 4  # the refusal stopped the sweep at once


# --------------------------------------------------------------------------------------------------
# 5. A separate process killed mid-approval never authorises a second paid run
# --------------------------------------------------------------------------------------------------
_APPROVE_THEN_HANG = """
import sys, time
from pathlib import Path
from idea_web.jobs.local import LocalJobs

work_root, offer, signal = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
jobs = LocalJobs(work_root)
insert = jobs.queue.insert_job

def insert_then_hang(*args, **kwargs):
    result = insert(*args, **kwargs)
    signal.write_text("paid job written; offer not consumed yet", encoding="utf-8")
    time.sleep(600)  # killed here, from outside, by the test
    return result

jobs.queue.insert_job = insert_then_hang
jobs.approve_offer(offer)
raise SystemExit("the approval finished; the test should have killed this process first")
"""


def test_killing_a_separate_process_mid_approval_then_retrying_pays_at_most_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import subprocess
    import sys
    import time

    from tests.idea_web.test_worker import SCRIPT
    from tests.test_additive_deep import _finished_offer, _other_jobs, _rows, _run_one

    config, jobs, offer = _finished_offer(tmp_path, monkeypatch)
    signal = tmp_path / "approval-signal.txt"
    child = subprocess.Popen(
        [sys.executable, "-c", _APPROVE_THEN_HANG, str(tmp_path), offer, str(signal)],
        cwd=ROOT,
        env=dict(os.environ, AUDD_API_TOKEN=""),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 120
        while not signal.exists():
            assert child.poll() is None, child.communicate()[1][-3000:]
            assert time.monotonic() < deadline, "the approving process never wrote the paid job"
            time.sleep(0.05)
    finally:
        # The process dies between writing the paid job and consuming the offer.
        child.kill()
        child.communicate(timeout=60)
    assert child.returncode != 0
    # Nothing it wrote survives: no paid job, and the offer is still open.
    assert _other_jobs(jobs, [offer]) == 0 and jobs.get(offer).offer_decision is None
    # The owner retries: exactly one paid job, and a further retry authorises nothing.
    follow_up = jobs.approve_offer(offer)
    assert follow_up is not None
    assert jobs.approve_offer(offer) is None
    assert _other_jobs(jobs, [offer]) == 1
    audd = FakeAudD(SCRIPT)
    _run_one(tmp_path, config, audd, FakeShazamHTTP(SCRIPT))
    _run_one(tmp_path, config, audd, FakeShazamHTTP(SCRIPT))
    # At most one paid run: seven clips bought once, one reservation.
    assert audd.calls == 7 and _rows(jobs, "run_reservations") == 1


# --------------------------------------------------------------------------------------------------
# 4. Found in the browser run: a restarted job's "elapsed" tile dropped back to a few seconds
# --------------------------------------------------------------------------------------------------
def test_a_restarted_job_keeps_its_first_start_time_so_elapsed_carries_on(tmp_path: Path) -> None:
    import time

    from id_detector.webapp.jobs import Job, JobManager

    seen: list[float | None] = []
    manager = JobManager(tmp_path, lambda ctx: seen.append(ctx.started_at))
    first_start = time.time() - 134.0  # the attempt the worker restart interrupted began 2m14s ago
    job = Job(
        id="restarted",
        target=str(AUDIO),
        display="mix",
        profile="free",
        acquire=False,
        build_index=False,
        started_at=first_start,
        carried_seconds=100.0,
        progress_max=40,
        progress_value=40.0,
    )
    with manager.lock:
        manager._jobs[job.id] = job
    manager._execute(job.id)
    assert seen == [first_start]
    assert job.started_at == first_start
    assert job.status_dict()["started_at"] == first_start  # the page's elapsed = now - this
    # A job that never ran before still gets its start time when it starts.
    fresh = Job(
        id="fresh",
        target=str(AUDIO),
        display="mix",
        profile="free",
        acquire=False,
        build_index=False,
    )
    with manager.lock:
        manager._jobs[fresh.id] = fresh
    before = time.time()
    manager._execute(fresh.id)
    assert fresh.started_at is not None and fresh.started_at >= before


def test_serve_refuses_a_substitute_runner_outside_test_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    monkeypatch.delenv("IDEA_TEST_MODE", raising=False)

    def no_server(*_args, **_kwargs):
        raise AssertionError("a server was about to start with a substitute runner")

    monkeypatch.setattr("idea_web.server.make_server", no_server)
    result = CliRunner().invoke(
        cli.app,
        [
            "serve",
            "--port",
            "0",
            "--no-open",
            "--work-root",
            str(tmp_path),
            "--config",
            str(tmp_path / "idea.toml"),
            "--runner",
            "x:y",
        ],
    )
    assert result.exit_code == 2
    assert "--runner is available only when IDEA_TEST_MODE=1" in result.output


# --------------------------------------------------------------------------------------------------
# Fix pass 1
# --------------------------------------------------------------------------------------------------
def test_the_page_states_a_settled_spend_even_when_the_run_stopped_unavailable() -> None:
    from id_detector.present.server import _cost_sentence

    assert _cost_sentence(
        usd_e2_spent=1, spend_known=True, status="provider_unavailable"
    ).startswith("$0.01 of paid checks was spent")
    assert _cost_sentence(usd_e2_spent=0, spend_known=True, status="provider_unavailable") == (
        "Nothing was spent — it stopped before any paid check ran."
    )


def test_max_accuracy_states_the_real_spend_of_a_refusal_beside_a_charged_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from id_detector.webapp.runner import make_pipeline_runner
    from idea_web.jobs.local import LocalWorker, local_database
    from tests.test_additive_deep import _local

    config, jobs = _local(tmp_path, monkeypatch)
    monkeypatch.setenv("AUDD_API_TOKEN", "")
    mixed = _RefusalBesideAChargedSibling()

    def run_once() -> None:
        LocalWorker(
            local_database(tmp_path),
            tmp_path,
            make_pipeline_runner(
                tmp_path,
                project_root=ROOT,
                config_path=config,
                paid_scan_adapters={"audd": mixed},
                shazam_http_client=FakeShazamHTTP(DEEP_SCRIPT),
                paid_sleep=no_backoff,
            ),
            flush_seconds=0.05,
        ).run_once()

    offer = jobs.submit(str(AUDIO), "max_accuracy")
    run_once()
    follow_up = jobs.approve_offer(offer)
    assert follow_up is not None
    run_once()
    job = jobs.get(follow_up)
    assert mixed.max_in_flight >= 2
    assert job.status == "succeeded", job.error
    assert job.paid_notice is not None and job.paid_notice["reason"] == "auth_error"
    assert "$0.01 of AudD credit was counted as spent" in job.paid_notice["words"]
    assert "othing was spent" not in job.paid_notice["words"]


def test_the_worker_loads_only_the_built_in_offline_runners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from idea_web.jobs.local import TEST_RUNNERS, _load_runner, runner_spec_refusal

    monkeypatch.setenv("IDEA_TEST_MODE", "1")
    monkeypatch.setenv("AUDD_API_TOKEN", "")
    for spec in ("os:system", "tests.idea_web.local_runner_fakes:priced", "x:y"):
        assert runner_spec_refusal(spec) is not None
        with pytest.raises(SystemExit, match="only the built-in offline runners"):
            _load_runner(tmp_path, tmp_path / "idea.toml", spec)
    for spec in sorted(TEST_RUNNERS):
        assert runner_spec_refusal(spec) is None
    assert callable(
        _load_runner(
            tmp_path, tmp_path / "idea.toml", "tests.idea_web.local_runner_fakes:slow_runner"
        )
    )
    # Never beside a paid credential (a placeholder: nothing is sent anywhere).
    monkeypatch.setenv("AUDD_API_TOKEN", "placeholder-not-a-token")
    assert "AUDD_API_TOKEN" in (
        runner_spec_refusal("tests.idea_web.local_runner_fakes:slow_runner") or ""
    )
    monkeypatch.delenv("IDEA_TEST_MODE")
    assert "IDEA_TEST_MODE" in (runner_spec_refusal(sorted(TEST_RUNNERS)[0]) or "")


def test_serve_refuses_an_arbitrary_runner_even_in_test_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    monkeypatch.setenv("IDEA_TEST_MODE", "1")

    def no_server(*_args, **_kwargs):
        raise AssertionError("a server was about to start with an arbitrary runner")

    monkeypatch.setattr("idea_web.server.make_server", no_server)
    result = CliRunner().invoke(
        cli.app,
        [
            "serve",
            "--port",
            "0",
            "--no-open",
            "--work-root",
            str(tmp_path),
            "--config",
            str(tmp_path / "idea.toml"),
            "--runner",
            "os:system",
        ],
    )
    assert result.exit_code == 2
    assert "only the built-in offline runners" in result.output


def test_the_retry_pass_is_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import id_detector.recognise as recognise

    monkeypatch.setattr(recognise, "RETRY_PASS_MIN", 2)
    script = {"shazam": {"default": "malformed", "windows": {"0": "match", "6": "match"}}}
    code, _audd, shazam, entry, _media = _analyse(tmp_path / "w", FREE_RECIPE, script)
    assert code == 0, entry
    # Five errored windows, but the pass retries at most max(2, 10 % of 7 windows) = 2.
    assert shazam.requests == 7 + 2
    assert [item["window"] for item in shazam.attempts[7:]] == [1, 2]


def test_the_retry_pass_stops_when_shazam_cannot_be_reached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("id_detector.recognise.retry_delay", lambda *_: 0)
    script = {"shazam": {"default": "timeout_pre", "windows": {}}}
    code, _audd, shazam, _entry, _media = _analyse(tmp_path / "w", FREE_RECIPE, script)
    # The sweep: 7 windows x (1 + 5 in-job retries) = 42 requests that never reached Shazam.
    # The end-of-sweep pass stops after 3 unreachable retries in a row instead of asking all 7.
    assert shazam.requests == 42 + 3
    del code


def test_no_token_deep_with_some_stored_paid_answers_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from id_detector.io import native_path

    result, entry, work = _cli_deep(tmp_path, monkeypatch, "audd,shazam", DEEP_SCRIPT)
    assert result.exit_code == 0, result.output
    (journal,) = work.rglob("invocations.jsonl")
    raw = journal.parent / "recognise" / "invocations" / "live-audd-clip-v1" / "raw"
    sorted(Path(native_path(raw)).glob("*.json"))[0].unlink()  # one clip has no stored answer
    monkeypatch.setattr("id_detector.pipeline.find_result", lambda *args, **kwargs: None)
    from typer.testing import CliRunner

    again = CliRunner().invoke(
        cli.app,
        [
            "analyse",
            str(AUDIO),
            "--recipe",
            "deep",
            "--work-root",
            str(work),
            "--config",
            str(tmp_path / "idea.toml"),
            "--no-hints",
            "--yes",
            "--fake-providers",
            "shazam",
        ],
    )
    assert again.exit_code == 0, (again.output, again.exception)
    assert "Paid recognition is not set up (no AudD token is configured)" in again.output
    assert "1 clip(s) Deep would check were not sent" in again.output
    assert "Nothing was spent." in again.output


def test_max_accuracy_without_a_token_but_with_stored_answers_shows_the_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from id_detector.io import native_path
    from id_detector.recipes import DEEP_RECIPE
    from id_detector.webapp.runner import make_pipeline_runner
    from idea_web.jobs.local import LocalWorker, local_database
    from tests.test_additive_deep import _local

    config, jobs = _local(tmp_path, monkeypatch)
    monkeypatch.setenv("AUDD_API_TOKEN", "")
    # Stored paid answers for this mix, then one of them lost.
    code, audd, _shazam, _entry, media = _analyse(
        tmp_path, DEEP_RECIPE, DEEP_SCRIPT, confirm=lambda _plan: True
    )
    assert code == 0 and audd.calls == 7
    raw = media / "recognise" / "invocations" / "live-audd-clip-v1" / "raw"
    sorted(Path(native_path(raw)).glob("*.json"))[0].unlink()
    monkeypatch.setattr("id_detector.pipeline.find_result", lambda *args, **kwargs: None)
    job_id = jobs.submit(str(AUDIO), "max_accuracy")
    LocalWorker(
        local_database(tmp_path),
        tmp_path,
        make_pipeline_runner(
            tmp_path,
            project_root=ROOT,
            config_path=config,
            paid_scan_adapters=None,
            shazam_http_client=FakeShazamHTTP(DEEP_SCRIPT),
            paid_sleep=no_backoff,
        ),
        flush_seconds=0.05,
    ).run_once()
    job = jobs.get(job_id)
    assert job.status == "succeeded", job.error
    assert job.paid_offer is None
    assert job.paid_notice is not None and job.paid_notice["reason"] == "not_configured_cached"
    assert "1 clip(s) Deep would check were not sent" in job.paid_notice["words"]


def test_idea_cost_with_an_unknown_length_still_says_paid_is_not_set_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    monkeypatch.setenv("AUDD_API_TOKEN", "")
    result = CliRunner().invoke(
        cli.app,
        [
            "cost",
            "unknown-mix",
            "--work-root",
            str(tmp_path),
            "--config",
            str(tmp_path / "idea.toml"),
        ],
    )
    assert result.exit_code == 2
    assert "Length unknown" in result.output
    assert "Paid recognition is not set up (no AudD token is configured)" in result.output

"""Cycle 4c-i: users, sessions, passwords, CSRF (plan §3.6, §4.4) — and local mode left alone.

Offline and in-process: the hosted app runs over ``httpx.ASGITransport`` against a temporary
SQLite database, with a settable clock where time matters. Races use separate SQLite handles on
separate threads, released together by a barrier.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path

import httpx
import pytest
import uvicorn

from id_detector.jobs import JobStoreLocked, ProcessLock
from id_detector.recipes import FREE_RECIPE
from id_detector.service import PlatformUrl
from idea_web import admin_cli, auth, pages
from idea_web.application import JobQueueAdapter, create_app
from idea_web.auth import (
    ARGON2_HASH_LEN,
    ARGON2_MEMORY_COST_KIB,
    ARGON2_PARALLELISM,
    ARGON2_SALT_LEN,
    ARGON2_TIME_COST,
    PRESESSION_COOKIE,
    SESSION_ABSOLUTE_SECONDS,
    SESSION_COOKIE,
    SESSION_IDLE_SECONDS,
    AccountStore,
    HostedSettings,
    client_address,
    rate_bucket,
    session_csrf,
    token_hash,
)
from idea_web.database import Database, MigrationRefused, hosted_database
from idea_web.hosted import LOGIN_FAILED, REDACTED_LINK
from idea_web.jobs.local import LocalJobs
from idea_web.jobs.worker import JobQueue, UnsupervisedWorker, Worker
from idea_web.server import serve_in_background
from tests.idea_web.hosted_helpers import (
    ORIGIN,
    PASSWORD,
    Browser,
    Clock,
    RecordingJobs,
    csrf_in,
    database_text,
    hosted_settings,
    make_account,
)
from tests.idea_web.test_parity import request, token

MIX = "https://soundcloud.com/example/live-mix"
DAY = 24 * 3600.0


def _hosted(tmp_path: Path, **kwargs):
    settings = hosted_settings(tmp_path, **kwargs)
    jobs = RecordingJobs()
    app = create_app(tmp_path / "work", local=False, jobs=jobs, hosted=settings)
    return settings, jobs, app


def _cookies(response: httpx.Response) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for header in response.headers.get_list("set-cookie"):
        parsed = SimpleCookie()
        parsed.load(header)
        for name, morsel in parsed.items():
            attributes = {key: str(value) for key, value in morsel.items() if value}
            attributes["value"] = morsel.value
            attributes["raw"] = header
            found[name] = attributes
    return found


def _sessions(database: Database, user_id: str) -> list[sqlite3.Row]:
    with database.read() as connection:
        return connection.execute(
            "SELECT * FROM sessions WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()


# ------------------------------------------------------------------------------------ passwords
def test_passwords_are_argon2id_with_the_stated_parameters_and_only_the_hash_is_kept(
    tmp_path: Path,
) -> None:
    assert (ARGON2_TIME_COST, ARGON2_MEMORY_COST_KIB, ARGON2_PARALLELISM) == (3, 65536, 4)
    assert (ARGON2_HASH_LEN, ARGON2_SALT_LEN) == (32, 16)
    settings = hosted_settings(tmp_path)
    account = make_account(settings, "Listener@Example.com")
    assert account.email == "listener@example.com"  # stored normalised
    with settings.database.read() as connection:
        stored = connection.execute(
            "SELECT pw_hash FROM users WHERE id = ?", (account.id,)
        ).fetchone()[0]
    assert stored.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert PASSWORD not in database_text(settings.database)
    store = AccountStore(settings.database)
    assert store.verify_login("LISTENER@example.com ", PASSWORD) is not None
    assert store.verify_login("listener@example.com", PASSWORD + "x") is None

    # A hash made with weaker parameters is upgraded at the next successful sign-in.
    weak = auth.PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
    with settings.database.write() as connection:
        connection.execute(
            "UPDATE users SET pw_hash = ? WHERE id = ?", (weak.hash(PASSWORD), account.id)
        )
    assert store.verify_login("listener@example.com", PASSWORD) is not None
    with settings.database.read() as connection:
        upgraded = connection.execute(
            "SELECT pw_hash FROM users WHERE id = ?", (account.id,)
        ).fetchone()[0]
    assert upgraded.startswith("$argon2id$v=19$m=65536,t=3,p=4$")


# ------------------------------------------------------------------------------------- sessions
def test_sign_in_sets_a_host_only_httponly_secure_lax_cookie_and_stores_only_its_hash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    settings, _, app = _hosted(tmp_path)
    account = make_account(settings, "listener@example.com")
    browser = Browser(app)
    page = browser.get("/login")
    presession = _cookies(page)[PRESESSION_COOKIE]
    assert presession["raw"].startswith(f"{PRESESSION_COOKIE}=")
    assert {"httponly", "secure"} <= set(presession) and presession["samesite"] == "Strict"
    signed_in = browser.sign_in("listener@example.com")
    assert signed_in.status_code == 303 and signed_in.headers["location"] == "/"
    cookie = _cookies(signed_in)[SESSION_COOKIE]
    assert cookie["path"] == "/" and "domain" not in cookie
    assert cookie["httponly"] and cookie["secure"] and cookie["samesite"] == "Lax"
    assert 0 < int(cookie["max-age"]) <= SESSION_ABSOLUTE_SECONDS
    raw = cookie["value"]
    assert len(raw) == 43
    rows = _sessions(settings.database, account.id)
    assert [row["token_hash"] for row in rows] == [hashlib.sha256(raw.encode()).hexdigest()]
    text = database_text(settings.database)
    assert raw not in text and session_csrf(raw) not in text and PASSWORD not in text
    assert raw not in caplog.text and PASSWORD not in caplog.text
    # The pre-sign-in cookie is spent.
    assert _cookies(signed_in)[PRESESSION_COOKIE]["max-age"] == "0"


def test_every_hosted_page_needs_a_live_session(tmp_path: Path) -> None:
    settings, jobs, app = _hosted(tmp_path)
    make_account(settings, "listener@example.com")
    job_id = jobs.submit(MIX, "free")
    anonymous = Browser(app)
    for path in ("/", "/index.html", "/new", f"/jobs/{job_id}", "/playlists", "/anything"):
        response = anonymous.get(path)
        assert response.status_code == 303 and response.headers["location"] == "/login", path
    for path in ("/csrf", "/activity", f"/jobs/{job_id}/status", "/playlists/state"):
        response = anonymous.get(path)
        assert response.status_code == 401 and response.json() == {"error": "sign in required"}
    assert anonymous.post(f"/jobs/{job_id}/cancel").status_code == 401
    assert anonymous.post("/playlists/like", data={"csrf_token": "x"}).status_code == 401
    assert anonymous.post("/analyse", data={"url": MIX}).status_code == 303
    assert anonymous.post("/analyse", json={"url": MIX}).status_code == 401
    assert jobs.submitted == [{"id": job_id, "target": MIX, "user_id": None}]
    assert anonymous.get("/healthz").json() == {"ok": True}
    assert anonymous.get("/login").status_code == 200

    anonymous.sign_in("listener@example.com")
    home = anonymous.get("/")
    assert home.status_code == 200 and "Drop a mix" in home.text
    assert "Signed in as <b>listener@example.com</b>" in home.text
    assert f'name="csrf_token" value="{anonymous.csrf()}"' in home.text
    assert anonymous.get(f"/jobs/{job_id}/status").status_code == 200
    # A signed-in browser that opens the sign-in page goes home instead.
    assert anonymous.get("/login").headers["location"] == "/"


def test_login_failure_never_reveals_whether_the_account_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, _, app = _hosted(tmp_path)
    make_account(settings, "known@example.com")
    make_account(settings, "disabled@example.com")
    AccountStore(settings.database).create_account("nopassword@example.com", actor="test")
    with settings.database.write() as connection:
        connection.execute("UPDATE users SET disabled_at = 1 WHERE email = 'disabled@example.com'")

    verifies: list[str] = []
    real = auth.PASSWORD_HASHER

    class CountingHasher:
        def verify(self, stored: str, password: str) -> bool:
            verifies.append(stored)
            return real.verify(stored, password)

        def __getattr__(self, name: str):
            return getattr(real, name)

    monkeypatch.setattr(auth, "PASSWORD_HASHER", CountingHasher())
    cases = {
        "unknown": ("nobody@example.com", PASSWORD),
        "wrong password": ("known@example.com", "not the password at all"),
        "no password yet": ("nopassword@example.com", PASSWORD),
        "disabled": ("disabled@example.com", PASSWORD),
        "malformed address": ("not an address", PASSWORD),
    }
    answers = {}
    for name, (email, password) in cases.items():
        browser = Browser(app)
        before = len(verifies)
        response = browser.sign_in(email, password)
        # Every path does exactly one argon2 verification: a missing account is not faster.
        assert len(verifies) == before + 1, name
        assert SESSION_COOKIE not in _cookies(response), name
        body = response.text.replace(email, "EMAIL").replace(csrf_in(response.text), "TOKEN")
        answers[name] = (response.status_code, body)
    assert len(set(answers.values())) == 1, answers
    status, body = answers["unknown"]
    assert status == 401 and LOGIN_FAILED in body

    # Wall-clock: the unknown address costs the same order of work as a wrong password.
    monkeypatch.setattr(auth, "PASSWORD_HASHER", real)
    store = AccountStore(settings.database)

    def fastest(email: str, password: str) -> float:
        best = float("inf")
        for _ in range(3):
            started = time.perf_counter()
            assert store.verify_login(email, password) is None
            best = min(best, time.perf_counter() - started)
        return best

    assert fastest("nobody@example.com", PASSWORD) > 0.3 * fastest(
        "known@example.com", "not the password at all"
    )


def test_sign_in_rotates_the_session_and_the_old_token_stops_working(tmp_path: Path) -> None:
    settings, _, app = _hosted(tmp_path)
    account = make_account(settings, "listener@example.com")
    browser = Browser(app)
    browser.sign_in("listener@example.com")
    first = browser.session_token
    # A copy of the first token, held elsewhere (a fixated or stolen cookie).
    copy = Browser(app)
    copy.cookies.set(SESSION_COOKIE, first, domain="idea.test")
    assert copy.get("/").status_code == 200

    # Signing in again from the browser that holds it (the form comes from a fresh tab, since a
    # signed-in browser is sent home from /login) replaces that session.
    fresh = Browser(app)
    form = fresh.get("/login")
    browser.cookies.set(PRESESSION_COOKIE, fresh.cookies[PRESESSION_COOKIE], domain="idea.test")
    fields = {"email": "listener@example.com", "password": PASSWORD}
    again = browser.post("/login", data={**fields, "csrf_token": csrf_in(form.text)})
    assert again.status_code == 303
    second = browser.session_token
    assert second and second != first
    rows = _sessions(settings.database, account.id)
    assert [row["revoked_reason"] for row in rows] == ["replaced", None]
    refused = copy.get("/")
    assert refused.status_code == 303 and refused.headers["location"] == "/login"
    # The dead cookie is not cleared: its holder was handed the replacement by the sign-in, and
    # an answer for the old cookie arriving after that one must not delete the new cookie.
    assert "set-cookie" not in refused.headers
    assert browser.get("/").status_code == 200


def test_logout_revokes_the_session_on_the_server(tmp_path: Path) -> None:
    settings, _, app = _hosted(tmp_path)
    account = make_account(settings, "listener@example.com")
    browser = Browser(app)
    browser.sign_in("listener@example.com")
    token_value = browser.session_token
    csrf = browser.csrf()
    assert browser.post("/logout", data={}).status_code == 403
    assert browser.post("/logout", data={"csrf_token": "x" * 64}).status_code == 403
    assert browser.get("/").status_code == 200  # a refused logout changes nothing

    out = browser.post("/logout", data={"csrf_token": csrf})
    assert out.status_code == 303 and out.headers["location"] == "/login?signed_out=1"
    assert _cookies(out)[SESSION_COOKIE]["max-age"] == "0"
    assert "You have signed out." in browser.get("/login?signed_out=1").text
    (row,) = _sessions(settings.database, account.id)
    assert row["revoked_reason"] == "logout" and row["revoked_at"] is not None
    # The browser dropped the cookie, but the server is what ended the session: a copy is dead.
    replay = Browser(app)
    replay.cookies.set(SESSION_COOKIE, token_value, domain="idea.test")
    assert replay.get("/").status_code == 303
    assert replay.get("/csrf").status_code == 401
    # A revoked session can never be reinstated, whatever writes the row.
    with (
        pytest.raises(sqlite3.DatabaseError, match="cannot be reinstated"),
        settings.database.write() as connection,
    ):
        connection.execute("UPDATE sessions SET revoked_at = NULL, revoked_reason = NULL")


def test_idle_and_absolute_expiry_are_enforced_from_durable_timestamps(tmp_path: Path) -> None:
    clock = Clock()
    settings, _, app = _hosted(tmp_path, clock=clock)
    account = make_account(settings, "listener@example.com")

    # Idle: 30 days without a request ends the session.
    idle = Browser(app)
    idle.sign_in("listener@example.com")
    clock.advance(SESSION_IDLE_SECONDS - 60)
    assert idle.get("/").status_code == 200  # used just in time: last_seen_at moves
    clock.advance(SESSION_IDLE_SECONDS - 60)
    assert idle.get("/").status_code == 200
    clock.advance(SESSION_IDLE_SECONDS + 1)
    assert idle.get("/").status_code == 303

    # Absolute: 90 days after sign-in the session ends however busy it is — including in a new
    # process that reads the same database (the timestamps are rows, not memory).
    started = clock.now
    busy = Browser(app)
    busy.sign_in("listener@example.com")
    restarted = create_app(tmp_path / "work", local=False, jobs=RecordingJobs(), hosted=settings)
    later = Browser(restarted)
    later.cookies = busy.cookies
    while clock.now + 20 * DAY < started + SESSION_ABSOLUTE_SECONDS:
        clock.advance(20 * DAY)
        assert later.get("/").status_code == 200
    clock.now = started + SESSION_ABSOLUTE_SECONDS - 1
    assert later.get("/").status_code == 200
    clock.now = started + SESSION_ABSOLUTE_SECONDS
    assert later.get("/").status_code == 303
    rows = _sessions(settings.database, account.id)
    assert rows[-1]["expires_at"] == started + SESSION_ABSOLUTE_SECONDS
    assert rows[-1]["last_seen_at"] < rows[-1]["expires_at"]


def test_a_plan_change_rotates_every_live_session_of_that_account(tmp_path: Path) -> None:
    clock = Clock()
    settings, _, app = _hosted(tmp_path, clock=clock)
    listener = make_account(settings, "listener@example.com")
    other = make_account(settings, "other@example.com")
    phone, laptop, bystander = Browser(app), Browser(app), Browser(app)
    phone.sign_in("listener@example.com")
    laptop.sign_in("listener@example.com")
    bystander.sign_in("other@example.com")
    old_phone, old_laptop, old_other = (
        phone.session_token,
        laptop.session_token,
        bystander.session_token,
    )
    expires = {
        row["token_hash"]: row["expires_at"] for row in _sessions(settings.database, listener.id)
    }
    old_csrf = phone.csrf()
    clock.advance(60)

    store = AccountStore(settings.database, clock=clock)
    assert store.set_plan(listener.id, "pro", actor="test-admin") is True
    assert store.set_plan(listener.id, "pro", actor="test-admin") is False  # no change, no rotation

    # The next request made with the old session is still that user's, and carries a new cookie;
    # a state-changing request signed with the old page's token is honoured once, as it was sent.
    answer = phone.post("/analyse", data={"url": MIX, "csrf_token": old_csrf})
    assert answer.status_code == 303
    new_phone = _cookies(answer)[SESSION_COOKIE]["value"]
    assert new_phone != old_phone and phone.session_token == new_phone
    assert phone.csrf() == session_csrf(new_phone) != old_csrf
    assert laptop.get("/").status_code == 200 and laptop.session_token != old_laptop
    assert bystander.get("/").status_code == 200 and bystander.session_token == old_other

    rows = _sessions(settings.database, listener.id)
    by_hash = {row["token_hash"]: row for row in rows}
    for old, new in ((old_phone, new_phone), (old_laptop, laptop.session_token)):
        before, after = by_hash[token_hash(old)], by_hash[token_hash(new)]
        assert before["revoked_reason"] == "rotated" and before["replaced_by"] == after["id"]
        # Rotation never extends the absolute lifetime.
        assert after["expires_at"] == expires[token_hash(old)]
    assert store.get(listener.id).plan == "pro" and store.get(other.id).plan == "free"
    # The old cookies are dead everywhere.
    for stale in (old_phone, old_laptop):
        copy = Browser(app)
        copy.cookies.set(SESSION_COOKIE, stale, domain="idea.test")
        assert copy.get("/").status_code == 303


def test_two_requests_racing_on_one_rotating_session_rotate_it_once(tmp_path: Path) -> None:
    settings = hosted_settings(tmp_path)
    account = make_account(settings, "listener@example.com")
    store = AccountStore(settings.database)
    issued = store.start_session(account.id, credential=account.credential)
    assert issued is not None
    store.set_plan(account.id, "pro", actor="test-admin")
    for _ in range(4):
        barrier = threading.Barrier(2)
        results: list[object] = []

        def use(barrier: threading.Barrier = barrier, results: list[object] = results) -> None:
            own = AccountStore(Database(settings.database.path))  # a separate handle
            barrier.wait(timeout=30)
            results.append(own.authenticate(issued.token))

        threads = [threading.Thread(target=use) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        rotated = [result for result in results if result is not None]
        assert len(results) == 2 and len(rotated) <= 1
        if rotated:
            break
    assert len(rotated) == 1 and rotated[0].new_token is not None
    rows = _sessions(settings.database, account.id)
    assert len(rows) == 2
    assert rows[0]["revoked_reason"] == "rotated" and rows[1]["revoked_at"] is None
    assert store.authenticate(issued.token) is None
    assert store.authenticate(rotated[0].new_token) is not None


def _supersede(
    path: str,
    browser: Browser,
    settings: HostedSettings,
    app,
    email: str,
    barrier: threading.Barrier | None = None,
) -> httpx.Response:
    """Hand ``browser`` a replacement cookie by ``path`` (a plan-change rotation, a sign-in, or
    a set-password link) and return the answer that sets it. With ``barrier``, the final request
    is released together with whoever else waits on it."""

    store = AccountStore(settings.database, clock=settings.clock)
    account = store.find(email)
    assert account is not None
    if path == "plan":
        store.set_plan(account.id, "pro" if account.plan == "free" else "free", actor="test-admin")
        method, url, data = "GET", "/", None
    elif path == "login":
        fresh = Browser(app)  # the form comes from a fresh tab: a signed-in browser is sent home
        form = fresh.get("/login")
        browser.cookies.set(PRESESSION_COOKIE, fresh.cookies[PRESESSION_COOKIE], domain="idea.test")
        method, url = "POST", "/login"
        data = {"email": email, "password": PASSWORD, "csrf_token": csrf_in(form.text)}
    else:
        link = store.issue_reset(account.id, actor="test-admin")
        page = browser.get(f"/reset/{link.token}")
        method, url = "POST", f"/reset/{link.token}"
        data = {"csrf_token": csrf_in(page.text), "password": PASSWORD, "confirm": PASSWORD}
    if barrier is not None:
        barrier.wait(timeout=30)
    return browser.request(method, url, data=data)


def _jar_after(*answers: httpx.Response) -> str | None:
    jar = httpx.Cookies()
    for answer in answers:
        jar.extract_cookies(answer)
    return jar.get(SESSION_COOKIE)


@pytest.mark.parametrize("path", ["plan", "login", "reset"])
def test_a_stale_answer_never_clears_a_freshly_issued_cookie(tmp_path: Path, path: str) -> None:
    """Every path that hands a browser a replacement cookie — a rotation on plan change, a
    sign-in, a set-password link — can have a request with the old cookie in flight at the same
    time. Whichever answer the browser applies last, it must keep the replacement: so an answer
    for a superseded cookie sets no cookie at all."""

    settings, _, app = _hosted(tmp_path)
    make_account(settings, "listener@example.com")
    email = "listener@example.com"
    browser = Browser(app)
    browser.sign_in(email)

    # Ordered first: the superseding request is answered, then a request that was already in
    # flight with the old cookie.
    old = browser.session_token
    stale = Browser(app)
    stale.cookies.set(SESSION_COOKIE, old, domain="idea.test")
    winner = _supersede(path, browser, settings, app, email)
    new = _cookies(winner)[SESSION_COOKIE]["value"]
    assert new != old and int(_cookies(winner)[SESSION_COOKIE]["max-age"]) > 0
    loser = stale.get("/")
    assert loser.status_code == 303 and "set-cookie" not in loser.headers
    assert _jar_after(winner, loser) == new and _jar_after(loser, winner) == new
    assert browser.get("/").status_code == 200

    # Then released together, twice: the superseding request and a request with the cookie it
    # supersedes. Exactly one answer carries a cookie, and it is never a clearing one.
    for _ in range(2):
        current = browser.session_token
        assert current is not None
        barrier = threading.Barrier(2)
        answers: dict[str, httpx.Response] = {}

        def supersede(
            barrier: threading.Barrier = barrier, answers: dict[str, httpx.Response] = answers
        ) -> None:
            answers["winner"] = _supersede(path, browser, settings, app, email, barrier)

        def use_old(
            current: str = current,
            barrier: threading.Barrier = barrier,
            answers: dict[str, httpx.Response] = answers,
        ) -> None:
            copy = Browser(app)
            copy.cookies.set(SESSION_COOKIE, current, domain="idea.test")
            barrier.wait(timeout=30)
            answers["stale"] = copy.get("/")

        threads = [threading.Thread(target=supersede), threading.Thread(target=use_old)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        with_cookie = [a for a in answers.values() if SESSION_COOKIE in _cookies(a)]
        assert len(with_cookie) == 1, {k: v.status_code for k, v in answers.items()}
        replacement = _cookies(with_cookie[0])[SESSION_COOKIE]["value"]
        assert (
            replacement != current and int(_cookies(with_cookie[0])[SESSION_COOKIE]["max-age"]) > 0
        )
        (other,) = [a for a in answers.values() if a is not with_cookie[0]]
        assert other.status_code in (200, 303) and SESSION_COOKIE not in _cookies(other)
        assert _jar_after(with_cookie[0], other) == replacement
        assert _jar_after(other, with_cookie[0]) == replacement
        browser.cookies.set(SESSION_COOKIE, replacement, domain="idea.test")
        assert browser.get("/").status_code == 200

    # A cookie that died any other way is still cleared: signed out, or reset from elsewhere.
    out = Browser(app)
    out.sign_in(email)
    ended = out.session_token
    out.post("/logout", data={"csrf_token": out.csrf()})
    replay = Browser(app)
    replay.cookies.set(SESSION_COOKIE, ended, domain="idea.test")
    assert _cookies(replay.get("/"))[SESSION_COOKIE]["max-age"] == "0"
    phone = Browser(app, address="192.0.2.9")
    phone.sign_in(email)
    _supersede("reset", Browser(app, address="192.0.2.10"), settings, app, email)
    assert _cookies(phone.get("/"))[SESSION_COOKIE]["max-age"] == "0"


def test_a_reset_revokes_and_replaces_in_one_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request with the old cookie is forced into the moment right after the first committed
    transaction that shows the old session revoked, and answered before the reset request goes
    on. Because using the link, revoking the old sessions and issuing the replacement (stamped
    ``replaced_by``) are ONE transaction, that request already sees a superseded cookie and
    clears nothing — so applying the replacement answer first and the stale answer last keeps
    the newer cookie. Split the reset into two transactions and this fails."""

    settings, _, app = _hosted(tmp_path)
    make_account(settings, "listener@example.com")
    email = "listener@example.com"
    browser = Browser(app)
    browser.sign_in(email)
    old = browser.session_token
    assert old is not None
    store = AccountStore(settings.database, clock=settings.clock)
    account = store.find(email)
    assert account is not None
    link = store.issue_reset(account.id, actor="test-admin")
    page = browser.get(f"/reset/{link.token}")
    form = {"csrf_token": csrf_in(page.text), "password": PASSWORD, "confirm": PASSWORD}

    release, answered = threading.Event(), threading.Event()
    fired: list[bool] = []
    real_write, real_read = settings.database.write, settings.database.read

    @contextlib.contextmanager
    def write_then_yield_to_the_stale_request():
        with real_write() as connection:
            yield connection
        # Committed. The first commit that leaves the old session revoked hands the turn to the
        # request with the old cookie, and waits until that request has been answered.
        if fired:
            return
        with real_read() as connection:
            row = connection.execute(
                "SELECT revoked_at FROM sessions WHERE token_hash = ?", (token_hash(old),)
            ).fetchone()
        if row is not None and row["revoked_at"] is not None:
            fired.append(True)
            release.set()
            assert answered.wait(60), "the stale request was never answered"

    monkeypatch.setattr(settings.database, "write", write_then_yield_to_the_stale_request)
    answers: dict[str, httpx.Response] = {}

    def reset() -> None:
        answers["reset"] = browser.post(f"/reset/{link.token}", data=form)

    def stale() -> None:
        copy = Browser(app)
        copy.cookies.set(SESSION_COOKIE, old, domain="idea.test")
        assert release.wait(60), "the reset never revoked the old session"
        try:
            answers["stale"] = copy.get("/")
        finally:
            answered.set()

    threads = [threading.Thread(target=reset), threading.Thread(target=stale)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    assert fired and set(answers) == {"reset", "stale"}
    replacement = answers["reset"]
    assert replacement.status_code == 303, replacement.text
    new = _cookies(replacement)[SESSION_COOKIE]["value"]
    assert new != old and int(_cookies(replacement)[SESSION_COOKIE]["max-age"]) > 0
    stale_answer = answers["stale"]
    assert stale_answer.status_code == 303 and "set-cookie" not in stale_answer.headers
    # The replacement answer applied BEFORE the stale one: the newer cookie survives.
    assert _jar_after(replacement, stale_answer) == new
    assert _jar_after(stale_answer, replacement) == new
    browser.cookies.set(SESSION_COOKIE, new, domain="idea.test")
    assert browser.get("/").status_code == 200
    rows = {row["token_hash"]: row for row in _sessions(settings.database, account.id)}
    assert rows[token_hash(old)]["revoked_reason"] == "password"
    assert rows[token_hash(old)]["replaced_by"] == rows[token_hash(new)]["id"]


# ---------------------------------------------------------------------------------------- CSRF
def test_every_state_changing_route_needs_the_sessions_synchroniser_token(tmp_path: Path) -> None:
    settings, jobs, app = _hosted(tmp_path)
    make_account(settings, "admin@example.com", role="admin")
    make_account(settings, "listener@example.com")
    admin, listener = Browser(app), Browser(app)
    admin.sign_in("admin@example.com")
    listener.sign_in("listener@example.com")
    job_id = jobs.submit(MIX, "free")
    foreign = listener.csrf()  # a real token, for another session
    mine = admin.csrf()

    routes = [
        ("/analyse", {"url": MIX, "profile": "free"}),
        (f"/jobs/{job_id}/dismiss", {}),
        ("/logout", {}),
        ("/admin/users", {"email": "new@example.com", "role": "user"}),
        ("/library/remove", {"source_key": "a" * 64, "media_key": "b" * 64}),
    ]
    for route, fields in routes:
        for presented in (None, "", foreign, mine[:-1] + "0"):
            data = dict(fields)
            if presented is not None:
                data["csrf_token"] = presented
            response = admin.post(route, data=data)
            assert response.status_code in (400, 403), (route, presented, response.status_code)
    for presented in (None, foreign):
        headers = {"X-CSRF-Token": presented} if presented else {}
        assert admin.post(f"/jobs/{job_id}/cancel", headers=headers).status_code == 403
        assert admin.post("/analyse", json={"url": MIX}, headers=headers).status_code == 403
    # Nothing above changed anything.
    assert len(jobs.submitted) == 1 and job_id in jobs.jobs
    assert AccountStore(settings.database).find("new@example.com") is None
    assert admin.get("/").status_code == 200

    # With the right token each route works.
    assert admin.post(f"/jobs/{job_id}/cancel", headers={"X-CSRF-Token": mine}).json() == {
        "cancelled": True
    }
    created = admin.post("/analyse", json={"url": MIX}, headers={"X-CSRF-Token": mine})
    assert created.status_code == 200
    assert admin.post(f"/jobs/{job_id}/dismiss", data={"csrf_token": mine}).status_code == 303
    made = admin.post(
        "/admin/users", data={"csrf_token": mine, "email": "new@example.com", "role": "user"}
    )
    assert made.status_code == 200 and "/reset/" in made.text
    assert admin.post("/logout", data={"csrf_token": mine}).status_code == 303


def test_the_sign_in_and_password_forms_need_their_pre_session_token(tmp_path: Path) -> None:
    settings, _, app = _hosted(tmp_path)
    make_account(settings, "listener@example.com")
    browser = Browser(app)
    page = browser.get("/login")
    form_token = csrf_in(page.text)
    fields = {"email": "listener@example.com", "password": PASSWORD}

    assert browser.post("/login", data=fields).status_code == 403  # no token
    other = Browser(app)
    other_token = csrf_in(other.get("/login").text)
    refused = browser.post("/login", data={**fields, "csrf_token": other_token})
    assert refused.status_code == 403 and "This page expired" in refused.text
    no_cookie = Browser(app)  # the right token without the cookie it is bound to
    assert no_cookie.post("/login", data={**fields, "csrf_token": form_token}).status_code == 403
    # A restarted server has a new signing key: a form shown before it simply expires.
    restarted = Browser(create_app(tmp_path / "w2", local=False, hosted=settings))
    restarted.cookies = browser.cookies
    assert restarted.post("/login", data={**fields, "csrf_token": form_token}).status_code == 403
    assert browser.session_token is None
    assert browser.post("/login", data={**fields, "csrf_token": form_token}).status_code == 303

    reset = Browser(app)
    link = "/reset/" + "A" * 43
    page = reset.get(link)
    assert page.status_code == 200
    body = {"password": "a new long password", "confirm": "a new long password"}
    assert reset.post(link, data=body).status_code == 403
    assert reset.post(link, data={**body, "csrf_token": other_token}).status_code == 403


# ------------------------------------------------------------------------------ Host and Origin
def test_hosted_requests_must_name_the_public_host_and_posts_its_origin(tmp_path: Path) -> None:
    settings, jobs, app = _hosted(tmp_path)
    make_account(settings, "listener@example.com")
    browser = Browser(app)
    browser.sign_in("listener@example.com")
    csrf = browser.csrf()
    fields = {"url": MIX, "csrf_token": csrf}
    for origin in (
        None,
        "https://evil.example",
        "http://idea.test",
        "https://idea.test:8443",
        "null",
    ):
        response = browser.post("/analyse", data=fields, origin=origin)
        assert response.status_code == 403 and "origin" in response.json()["error"], origin
    for host in ("evil.example", "127.0.0.1", "idea.test.evil.example"):
        response = browser.get("/", headers={"host": host})
        assert response.status_code == 403 and "host" in response.json()["error"], host
        assert browser.post("/analyse", data=fields, headers={"host": host}).status_code == 403
    # Refusals carry the defensive headers; the health probe answers on any host.
    refused = browser.get("/login", headers={"host": "evil.example"})
    assert refused.headers["x-frame-options"] == "SAMEORIGIN"
    assert browser.get("/healthz", headers={"host": "127.0.0.1:8000"}).status_code == 200
    assert jobs.submitted == []
    assert browser.post("/analyse", data=fields).status_code == 303
    assert len(jobs.submitted) == 1
    # A large refused body is drained, not left on the socket.
    big = browser.post("/analyse", content=b"x" * 200_000, origin="https://evil.example")
    assert big.status_code == 403

    with pytest.raises(ValueError):
        HostedSettings(database=settings.database, public_origin="http://idea.test")
    with pytest.raises(ValueError):
        HostedSettings(database=settings.database, public_origin="https://idea.test/path")


def test_hosted_pages_keep_the_per_document_policy_and_same_origin_framing(
    tmp_path: Path,
) -> None:
    from tests.idea_web.test_headers_forms import _assert_csp_names_exactly_the_inline_scripts

    settings, jobs, app = _hosted(tmp_path)
    make_account(settings, "admin@example.com", role="admin")
    browser = Browser(app)
    login = browser.get("/login")
    browser.sign_in("admin@example.com")
    job_id = jobs.submit(MIX, "free")
    pages = {
        "login": login,
        "reset": Browser(app).get("/reset/" + "B" * 43),
        "home": browser.get("/"),
        "job": browser.get(f"/jobs/{job_id}"),
        "admin": browser.get("/admin"),
    }
    for name, response in pages.items():
        assert response.status_code == 200, name
        assert response.headers["cache-control"] == "no-store", name
        assert response.headers["x-frame-options"] == "SAMEORIGIN", name
        csp = response.headers["content-security-policy"]
        assert "frame-src 'self'" in csp and "frame-ancestors 'self'" in csp, name
        _assert_csp_names_exactly_the_inline_scripts(response, name)
    assert pages["job"].text.count("<script>") == 1  # the per-job constants, hashed


# ---------------------------------------------------------------------------------- rate limits
def test_login_is_limited_per_ip_with_ipv6_bucketed_by_64(tmp_path: Path) -> None:
    clock = Clock()
    settings, _, app = _hosted(tmp_path, clock=clock)
    make_account(settings, "listener@example.com")
    first = Browser(app, address="2001:db8:1:2::10")
    for _ in range(10):
        assert first.sign_in("listener@example.com", "wrong password!").status_code == 401
    limited = first.sign_in("listener@example.com")  # even the right password now waits
    assert limited.status_code == 429 and SESSION_COOKIE not in _cookies(limited)
    assert int(limited.headers["retry-after"]) == 15 * 60
    assert "Too many sign-in attempts" in limited.text
    # Another address in the same /64 shares the bucket; the next /64 does not.
    neighbour = Browser(app, address="2001:db8:1:2:ffff:ffff:ffff:ffff")
    assert neighbour.sign_in("listener@example.com").status_code == 429
    elsewhere = Browser(app, address="2001:db8:1:3::10")
    assert elsewhere.sign_in("listener@example.com").status_code == 303
    ipv4 = Browser(app, address="203.0.113.9")
    assert ipv4.sign_in("listener@example.com").status_code == 303
    # The window slides: after 15 minutes the first attempt has aged out.
    clock.advance(15 * 60)
    assert first.sign_in("listener@example.com").status_code == 303


def test_reset_and_analyse_are_limited_per_ip(tmp_path: Path) -> None:
    clock = Clock()
    settings, jobs, app = _hosted(tmp_path, clock=clock)
    make_account(settings, "listener@example.com")
    browser = Browser(app, address="198.51.100.20")
    link = "/reset/" + "C" * 43
    page = browser.get(link)
    body = {
        "csrf_token": csrf_in(page.text),
        "password": "a new long password",
        "confirm": "a new long password",
    }
    # A mistyped confirmation never reaches the link, so it is not counted.
    mismatch = browser.post(link, data={**body, "confirm": "something else entirely"})
    assert mismatch.status_code == 400 and "do not match" in mismatch.text
    for _ in range(3):
        assert browser.post(link, data=body).status_code == 400
    limited = browser.post(link, data=body)
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) == 3600

    browser.sign_in("listener@example.com")
    csrf = browser.csrf()
    for index in range(5):
        response = browser.post("/analyse", data={"url": f"{MIX}-{index}", "csrf_token": csrf})
        assert response.status_code == 303
    refused = browser.post("/analyse", data={"url": f"{MIX}-6", "csrf_token": csrf})
    assert refused.status_code == 429 and "Too many analyses" in refused.text
    assert f"{MIX}-6" in refused.text  # the form keeps what was typed
    assert (
        browser.post("/analyse", json={"url": MIX}, headers={"X-CSRF-Token": csrf}).status_code
        == 429
    )
    # A refused form (bad token) is not counted and nothing is queued.
    assert len(jobs.submitted) == 5
    clock.advance(3600)
    assert browser.post("/analyse", data={"url": MIX, "csrf_token": csrf}).status_code == 303


def test_exports_are_limited_to_ten_per_minute_per_ip(tmp_path: Path) -> None:
    from tests.test_phase1a_bundles import publish, seed
    from tests.test_stage7_page import _source

    clock = Clock()
    publish(seed(tmp_path / "work"))
    settings, _, app = _hosted(tmp_path, clock=clock)
    make_account(settings, "listener@example.com")
    source = _source("soundcloud")
    prefix = f"/{source.source_key}/{source.media_key}/present"
    exports = [prefix + "/tracklist.cue", prefix + "/tracklist.md", prefix + "/tracklist.json"]
    # An export needs a session first; a request sent to sign in is not an admitted export.
    assert Browser(app, address="198.51.100.40").get(exports[0]).status_code == 303

    browser = Browser(app, address="198.51.100.41")
    browser.sign_in("listener@example.com")
    for index in range(10):
        assert browser.get(exports[index % 3]).status_code == 200, index
    refused = browser.get(exports[0])
    assert refused.status_code == 429 and int(refused.headers["retry-after"]) == 60
    assert "Too many exports" in refused.text
    assert browser.get(exports[1]).status_code == 429
    # The result page and the live pages are not exports, and stay available.
    assert browser.get(prefix + "/index.html").status_code == 200
    assert browser.get("/").status_code == 200
    # Another address has its own bucket, and the window slides.
    other = Browser(app, address="198.51.100.42")
    other.sign_in("listener@example.com")
    assert other.get(exports[2]).status_code == 200
    clock.advance(60)
    assert browser.get(exports[0]).status_code == 200
    assert "export" in auth.RATE_LIMITS and auth.RATE_LIMITS["export"] == (10, 60.0)


def test_forwarded_for_is_believed_only_from_a_trusted_proxy(tmp_path: Path) -> None:
    proxies = HostedSettings.parse_proxies(["127.0.0.1/32", "::1"])
    assert client_address("203.0.113.5", ["198.51.100.1"], proxies) == "203.0.113.5"
    assert client_address("127.0.0.1", ["198.51.100.1"], proxies) == "198.51.100.1"
    assert client_address("127.0.0.1", ["10.9.9.9, 198.51.100.1, 127.0.0.1"], proxies) == (
        "198.51.100.1"
    )
    assert client_address("127.0.0.1", ["not-an-ip, 198.51.100.1"], proxies) == "198.51.100.1"
    assert client_address("127.0.0.1", ["198.51.100.1, garbage"], proxies) == "127.0.0.1"
    assert client_address("127.0.0.1", [], proxies) == "127.0.0.1"
    assert client_address("::ffff:203.0.113.5", [], ()) == "203.0.113.5"
    assert rate_bucket("203.0.113.5") == "v4:203.0.113.5"
    assert rate_bucket("2001:db8::1") == rate_bucket("2001:db8::ffff") == "v6:2001:db8::/64"
    assert rate_bucket("[2001:db8:0:1::1]") == "v6:2001:db8:0:1::/64"
    assert rate_bucket("testclient") == "other:testclient"

    clock = Clock()
    settings, _, app = _hosted(tmp_path, clock=clock, trusted_proxies=proxies)
    make_account(settings, "listener@example.com")
    # Behind the trusted proxy each client has its own bucket...
    for client in range(3):
        browser = Browser(app, address="127.0.0.1")
        for _ in range(10):
            page = browser.get("/login")
            response = browser.post(
                "/login",
                data={"csrf_token": csrf_in(page.text), "email": "x@example.com", "password": "p"},
                headers={"x-forwarded-for": f"198.51.100.{client}"},
            )
            assert response.status_code == 401
    # ...while a direct client cannot choose its bucket with the header.
    direct = Browser(app, address="203.0.113.77")
    for attempt in range(11):
        page = direct.get("/login")
        response = direct.post(
            "/login",
            data={"csrf_token": csrf_in(page.text), "email": "x@example.com", "password": "p"},
            headers={"x-forwarded-for": f"192.0.2.{attempt}"},
        )
    assert response.status_code == 429


# ------------------------------------------------------------------------ jobs carry the account
def test_a_hosted_submission_records_the_signed_in_account_as_the_jobs_user(
    tmp_path: Path,
) -> None:
    settings, jobs, app = _hosted(tmp_path)
    account = make_account(settings, "listener@example.com")
    browser = Browser(app)
    browser.sign_in("listener@example.com")
    assert (
        browser.post("/analyse", data={"url": MIX, "csrf_token": browser.csrf()}).status_code == 303
    )
    assert jobs.submitted[-1]["user_id"] == account.id

    # The durable queue stores that id in jobs.user_id, next to rows written before accounts
    # existed (an opaque id with no account behind it), which keep working unchanged.
    queue = JobQueue(settings.database)
    real = queue.enqueue(PlatformUrl(MIX), FREE_RECIPE, user_id=account.id)
    legacy = queue.enqueue(PlatformUrl(MIX + "-2"), FREE_RECIPE, user_id="alice")
    with settings.database.read() as connection:
        rows = dict(
            connection.execute(
                "SELECT j.id, u.email FROM jobs j LEFT JOIN users u ON u.id = j.user_id"
            ).fetchall()
        )
    assert rows == {real: "listener@example.com", legacy: None}


def test_a_hosted_submission_goes_through_the_durable_queue_as_the_account(
    tmp_path: Path,
) -> None:
    """The production adapter, not the recording double: ``/analyse`` enqueues in the durable
    queue with ``jobs.user_id`` set, and the page, status, cancel and dismiss read that row."""

    settings = hosted_settings(tmp_path)
    work = tmp_path / "work"
    jobs = LocalJobs(work, database=settings.database, local_mode=False)
    app = create_app(work, local=False, jobs=jobs, hosted=settings)
    account = make_account(settings, "listener@example.com")
    browser = Browser(app)
    browser.sign_in("listener@example.com")
    csrf = browser.csrf()

    submitted = browser.post("/analyse", data={"url": MIX, "csrf_token": csrf})
    assert submitted.status_code == 303
    job_id = submitted.headers["location"].rsplit("/", 1)[1]
    api = browser.post("/analyse", json={"url": MIX + "-2"}, headers={"X-CSRF-Token": csrf})
    assert api.status_code == 200
    with settings.database.read() as connection:
        rows = {
            row["id"]: (row["user_id"], row["state"])
            for row in connection.execute("SELECT id, user_id, state FROM jobs")
        }
    assert rows == {job_id: (account.id, "intake"), api.json()["id"]: (account.id, "intake")}
    assert browser.get(f"/jobs/{job_id}").status_code == 200
    status = browser.get(f"/jobs/{job_id}/status").json()
    assert status["status"] == "queued" and status["audio_url"] is None
    assert "Recent analyses" in browser.get("/").text
    assert browser.post(f"/jobs/{job_id}/cancel", headers={"X-CSRF-Token": csrf}).json() == {
        "cancelled": True
    }
    assert browser.get(f"/jobs/{job_id}/status").json()["status"] == "cancelled"
    assert browser.post(f"/jobs/{job_id}/dismiss", data={"csrf_token": csrf}).status_code == 303
    assert jobs.get(job_id) is None
    # A hosted queue is not in local mode: a file on the server is refused as a target.
    audio = tmp_path / "mix.wav"
    audio.write_bytes(b"RIFF")
    refused = browser.post("/analyse", data={"url": str(audio), "csrf_token": csrf})
    assert refused.status_code == 400 and "complete web link" in refused.text
    with settings.database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    # The adapter protocol and the real adapter agree on the account keyword.
    for signature in (inspect.signature(JobQueueAdapter.submit), inspect.signature(jobs.submit)):
        assert "user_id" in signature.parameters


def test_hosted_migrations_take_the_worker_supervisor_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from id_detector.jobs import _ACTIVE_LOCKS

    folder = tmp_path / "data"
    path = folder / "app.db"
    lock_path = folder / "worker-supervisor.lock"
    arguments = ["--database", str(path), "--origin", ORIGIN, "--email", "owner@example.com"]
    held = ProcessLock(lock_path)
    held.acquire()  # what a live worker holds
    try:
        with pytest.raises(MigrationRefused, match="another ID'er is still running"):
            hosted_database(path)
        assert admin_cli.main(["create", *arguments, "--admin"]) == 2
        assert "another ID'er is still running" in capsys.readouterr().err
        supervised = Database(path, supervisor_lock=lock_path)
        with pytest.raises(MigrationRefused):
            create_app(
                tmp_path / "work",
                local=False,
                hosted=HostedSettings(database=supervised, public_origin=ORIGIN),
            )
        assert supervised.version() == 0  # nothing was applied
    finally:
        held.release()
    database = hosted_database(path)
    assert database.version() == 6
    assert database.supervisor_lock_path == lock_path.resolve()
    assert admin_cli.main(["create", *arguments, "--admin"]) == 0
    # A work root's own queue database is supervised by that root's lock, as ``idea serve`` is.
    rooted = hosted_database(tmp_path / "root" / ".idea" / "app.db")
    assert rooted.supervisor_lock_path == (tmp_path / "root" / ".idea" / lock_path.name).resolve()
    # A database with no supervisor lock cannot start a hosted server at all.
    loose = Database(tmp_path / "loose" / "app.db")
    loose.migrate()
    with pytest.raises(ValueError, match="supervised database"):
        create_app(
            tmp_path / "work",
            local=False,
            hosted=HostedSettings(database=loose, public_origin=ORIGIN),
        )

    # A hosted worker never runs unsupervised: started with NO lock argument, it takes the lock
    # its database names and holds it for its whole run, so no migration can run under it and a
    # second worker on the same lock is refused; over a database that names no lock it refuses
    # to start at all. (A fresh, empty database: its 0006 can then go down once the worker has
    # stopped, which proves the refusal was the lock's.)
    with pytest.raises(UnsupervisedWorker, match="hosted_database"):
        Worker(loose, tmp_path / "work").run_forever(stop=threading.Event())
    live = hosted_database(tmp_path / "live" / "app.db")
    live_lock = live.supervisor_lock_path
    assert live_lock == (tmp_path / "live" / lock_path.name).resolve()
    # An explicit lock argument must be the database's own: a different file would let this
    # worker and a migrator lock different things, so it is refused before anything is claimed.
    # (The stop event is already set: a worker that wrongly accepted the override would return
    # at once rather than run for ever, and the ``raises`` below would fail.)
    already_stopped = threading.Event()
    already_stopped.set()
    with pytest.raises(UnsupervisedWorker, match="different lock"):
        Worker(live, tmp_path / "work").run_forever(
            stop=already_stopped, supervisor_lock=tmp_path / "elsewhere.lock"
        )
    with pytest.raises(UnsupervisedWorker, match="hosted_database"):
        Worker(loose, tmp_path / "work").run_forever(
            stop=already_stopped, supervisor_lock=tmp_path / "elsewhere.lock"
        )
    assert not (tmp_path / "elsewhere.lock").exists()
    same = Worker(live, tmp_path / "work")
    same.run_forever(stop=already_stopped, supervisor_lock=live_lock)  # the same path is fine
    stop = threading.Event()
    worker = Worker(live, tmp_path / "work")
    thread = threading.Thread(
        target=worker.run_forever, kwargs={"stop": stop, "poll_seconds": 0.05}, daemon=True
    )
    thread.start()
    try:
        key = str(ProcessLock(live_lock).path).casefold() if os.name == "nt" else str(live_lock)
        deadline = time.monotonic() + 30
        while key not in _ACTIVE_LOCKS and time.monotonic() < deadline:
            time.sleep(0.02)
        assert key in _ACTIVE_LOCKS, "the worker never took its supervisor lock"
        with pytest.raises(MigrationRefused, match="another ID'er is still running"):
            live.migrate(5)
        with pytest.raises(JobStoreLocked):
            Worker(live, tmp_path / "work").run_forever(stop=stop)
        assert live.version() == 6
    finally:
        stop.set()
        thread.join(30)
    assert key not in _ACTIVE_LOCKS  # released with the run
    assert live.migrate(5) == 5 and live.migrate() == 6


def test_local_assets_and_start_up_imports_are_untouched_by_accounts(tmp_path: Path) -> None:
    from id_detector.present import server as legacy
    from id_detector.present import theme

    # The local asset set is exactly what it was before accounts existed.
    unchanged = (theme.BASE_CSS + legacy._APP_CSS).encode("utf-8")
    assert unchanged == pages.STATIC_CSS
    assert pages.ACCOUNT_CSS.encode("utf-8") not in pages.STATIC_CSS
    hosted_only = pages.ACCOUNT_CSS.encode("utf-8")
    assert hosted_only == pages.HOSTED_CSS
    assert pages.HOSTED_STYLESHEET_HREF.startswith("/static/hosted.")
    local = create_app(tmp_path / "local", jobs=LocalJobs(tmp_path / "local"))
    home = request(local, "GET", "/")
    assert pages.STYLESHEET_HREF in home.text and "/static/hosted." not in home.text
    assert request(local, "GET", pages.HOSTED_STYLESHEET_HREF).status_code == 404
    # Hosted pages link the hosted-only stylesheet, and only a hosted server serves it.
    settings, _, app = _hosted(tmp_path)
    make_account(settings, "listener@example.com")
    browser = Browser(app)
    assert pages.HOSTED_STYLESHEET_HREF in browser.get("/login").text
    browser.sign_in("listener@example.com")
    assert pages.HOSTED_STYLESHEET_HREF in browser.get("/").text
    served = browser.get(pages.HOSTED_STYLESHEET_HREF)
    assert served.status_code == 200 and served.content == pages.HOSTED_CSS
    assert served.headers["cache-control"].startswith("public, max-age=")

    # Importing what ``idea serve`` imports loads neither the account code nor argon2.
    probe = (
        "import sys, idea_web.application, idea_web.server, idea_web.jobs.local; "
        "print([m for m in ('argon2', 'idea_web.auth', 'idea_web.hosted') if m in sys.modules])"
    )
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
    )
    assert result.stdout.strip() == "[]", result.stdout


def test_hosted_access_logs_never_carry_a_setup_or_reset_token(tmp_path: Path) -> None:
    """A real uvicorn server with access logging ON: the request target is logged, the token
    in it is not (hosted mode installs a redacting filter on uvicorn's loggers)."""

    settings, _, app = _hosted(tmp_path)
    listener = make_account(settings, "listener@example.com")
    link = AccountStore(settings.database).issue_reset(listener.id, actor="test-admin")
    lines: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    capture = Capture()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="info", access_log=True, lifespan="off"
        )
    )
    loggers = [logging.getLogger(name) for name in ("uvicorn.access", "uvicorn.error")]
    for logger in loggers:
        logger.addHandler(capture)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0) as client:
            shown = client.get(f"/reset/{link.token}", headers={"host": "idea.test"})
            probed = client.get("/healthz")
    finally:
        server.should_exit = True
        thread.join(30)
        for logger in loggers:
            logger.removeHandler(capture)
    assert shown.status_code == 200 and "Choose a password" in shown.text
    assert probed.status_code == 200
    logged = [line for line in lines if "/reset/" in line]
    assert logged and all(REDACTED_LINK in line for line in logged), lines
    assert all(link.token not in line for line in lines)
    assert any("/healthz" in line for line in lines)  # the log itself is on


# ----------------------------------------------------------------------------------- migration
def test_migration_0006_goes_down_only_while_its_tables_are_empty(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    assert database.migrate() == 6
    with database.write() as connection:
        connection.execute(
            "INSERT INTO rate_limit_hits(action, bucket, at) VALUES ('login', 'v4:1.2.3.4', 1)"
        )
    assert database.migrate(5) == 5  # limit counters are disposable
    with database.read() as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert not names & {"users", "sessions", "email_tokens", "entitlements", "rate_limit_hits"}
    assert {"run_dispatches", "run_settlements", "run_reservations", "admin_audit"} <= names
    assert database.migrate() == 6

    settings = HostedSettings(database=database, public_origin=ORIGIN)
    make_account(settings, "listener@example.com")
    with pytest.raises(sqlite3.DatabaseError, match="account tables hold rows"):
        database.migrate(5)
    assert database.version() == 6
    assert AccountStore(database).find("listener@example.com") is not None


# ---------------------------------------------------------------------------------- local mode
def test_local_mode_stays_sign_in_free_and_unchanged(tmp_path: Path) -> None:
    jobs = LocalJobs(tmp_path)
    app = create_app(tmp_path, jobs=jobs)
    csrf = token(app)
    assert csrf == app.state.csrf_token  # the one process-wide token, as before

    responses = [
        request(app, "GET", "/"),
        request(app, "GET", "/csrf"),
        request(app, "GET", "/healthz"),
        request(app, "GET", "/activity"),
        request(app, "POST", "/analyse", data={"url": MIX, "csrf_token": csrf}),
        request(app, "POST", "/analyse", json={"url": MIX}, headers={"X-CSRF-Token": csrf}),
    ]
    home, _, _, _, form, api = responses
    assert home.status_code == 200 and "Drop a mix" in home.text
    assert "Sign out" not in home.text and "Sign in" not in home.text
    assert form.status_code == 303 and form.headers["location"].startswith("/jobs/")
    assert api.status_code == 200
    job_page = request(app, "GET", form.headers["location"])
    assert job_page.status_code == 200 and "Sign out" not in job_page.text
    responses.append(job_page)
    # No account routes, no redirect to a sign-in page, and never a cookie.
    for path in ("/login", "/logout", "/admin", "/admin/users", "/reset/" + "A" * 43, "/signup"):
        for method in ("GET", "POST"):
            answer = request(app, method, path, data={"csrf_token": csrf})
            assert answer.status_code == 404, (method, path)
            responses.append(answer)
    for response in responses:
        assert "set-cookie" not in response.headers, response.request.url
        assert response.status_code != 401
        assert response.headers.get("location", "").startswith("/login") is False
    # The loopback gate is the local gate: a request with no Origin is fine, a foreign one is not.
    assert (
        request(app, "POST", "/analyse", data={"url": MIX, "csrf_token": csrf}).status_code == 303
    )
    foreign = request(
        app, "POST", "/analyse", data={"url": MIX}, headers={"Origin": "https://idea.test"}
    )
    assert foreign.status_code == 403
    # The local queue database has the account tables (one schema), and they stay empty.
    with jobs.database.read() as connection:
        for table in ("users", "sessions", "email_tokens", "entitlements", "rate_limit_hits"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        users = {row[0] for row in connection.execute("SELECT user_id FROM jobs")}
    assert users == {None}

    # Accounts cannot be switched on in local mode, and hosted mode cannot run without them.
    settings = hosted_settings(tmp_path / "accounts")
    with pytest.raises(ValueError, match="sign-in free"):
        create_app(tmp_path, jobs=jobs, hosted=settings)
    with pytest.raises(ValueError, match="HostedSettings"):
        create_app(tmp_path, local=False, jobs=jobs)

    # The real loopback server `idea serve` runs: plain HTTP, no cookie, no sign-in.
    running = serve_in_background(tmp_path, port=0, jobs=jobs)
    try:
        client = httpx.Client(base_url=running.base_url, timeout=httpx.Timeout(10.0))
        with client:
            page = client.get("/")
            live_token = client.get("/csrf").json()["token"]
            submitted = client.post("/analyse", data={"url": MIX, "csrf_token": live_token})
            admin = client.get("/admin")
    finally:
        running.shutdown()
    assert page.status_code == 200 and "Drop a mix" in page.text
    assert submitted.status_code == 303 and admin.status_code == 404
    assert not client.cookies and all(
        "set-cookie" not in answer.headers for answer in (page, submitted, admin)
    )

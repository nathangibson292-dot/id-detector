"""Cycle 4c-ii: admin-created accounts and admin-issued reset links (plan §3.6, §4.4, §4.5).

The gate: an admin-created account is pre-verified; an admin reset link works exactly once (also
under a real race, across threads and across processes); XSS through titles or comments is
escaped; there is no self-signup route.
"""

from __future__ import annotations

import multiprocessing
import re
import sqlite3
import threading
import unicodedata
from pathlib import Path

import pytest
from markupsafe import escape

from idea_web import admin_cli
from idea_web.application import create_app
from idea_web.auth import (
    PASSWORD_HASHER,
    PASSWORD_MAX_LENGTH,
    RESET_LINK_SECONDS,
    SESSION_COOKIE,
    SETUP_LINK_SECONDS,
    AccountError,
    AccountStore,
    normalise_password,
    token_hash,
)
from idea_web.database import Database
from idea_web.hosted import LINK_FAILED as LINK_FAILED_TEXT
from idea_web.hosted import LOGIN_FAILED
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
from tests.idea_web.test_headers_forms import _assert_csp_names_exactly_the_inline_scripts, _markup
from tests.idea_web.test_parity import request

MIX = "https://soundcloud.com/example/live-mix"
NEW_PASSWORD = "a brand new long passphrase"
_LINK = re.compile(re.escape(ORIGIN) + r"(/reset/[A-Za-z0-9_-]{43})")
#: The refusal as it appears in the page (Jinja escapes the apostrophe).
LINK_FAILED = str(escape(LINK_FAILED_TEXT))
XSS = '<script>alert("x")</script><img src=x onerror=alert(1)>'


def _hosted(tmp_path: Path, **kwargs):
    settings = hosted_settings(tmp_path, **kwargs)
    jobs = RecordingJobs()
    app = create_app(tmp_path / "work", local=False, jobs=jobs, hosted=settings)
    return settings, jobs, app


def _admin(app, settings) -> Browser:
    make_account(settings, "admin@example.com", role="admin")
    browser = Browser(app)
    assert browser.sign_in("admin@example.com").status_code == 303
    return browser


def _link(html: str) -> str:
    match = _LINK.search(html)
    assert match is not None, "no single-use link on the page"
    return match.group(1)


def _set_password(app, link: str, password: str = NEW_PASSWORD, *, address: str = "192.0.2.50"):
    browser = Browser(app, address=address)
    page = browser.get(link)
    assert page.status_code == 200
    response = browser.post(
        link, data={"csrf_token": csrf_in(page.text), "password": password, "confirm": password}
    )
    return browser, response


def _user_row(settings, email: str) -> sqlite3.Row:
    with settings.database.read() as connection:
        return connection.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


# ---------------------------------------------------------------------- admin-created accounts
def test_an_admin_created_account_is_pre_verified_and_gets_a_single_use_setup_link(
    tmp_path: Path,
) -> None:
    clock = Clock()
    settings, _, app = _hosted(tmp_path, clock=clock)
    admin = _admin(app, settings)
    page = admin.get("/admin")
    created = admin.post(
        "/admin/users",
        data={
            "csrf_token": csrf_in(page.text),
            "email": " New.Listener@Example.com ",
            "role": "user",
            "comment": "beta wave 1",
        },
    )
    assert created.status_code == 200
    assert "A set-password link" in created.text and "new.listener@example.com" in created.text
    link = _link(created.text)

    row = _user_row(settings, "new.listener@example.com")
    assert row["verified_at"] == clock.now  # pre-verified at creation (plan §3.6)
    assert row["pw_hash"] is None and row["role"] == "user"
    admin_id = _user_row(settings, "admin@example.com")["id"]
    assert row["created_by"] == admin_id and row["created_ip"] == "198.51.100.7"
    raw = link.rsplit("/", 1)[1]
    assert raw not in database_text(settings.database)  # only its hash is kept
    with settings.database.read() as connection:
        token = connection.execute(
            "SELECT * FROM email_tokens WHERE user_id = ?", (row["id"],)
        ).fetchone()
        audit = connection.execute(
            "SELECT actor, action, target, detail FROM admin_audit WHERE target = ?", (row["id"],)
        ).fetchall()
    assert token["token_hash"] == token_hash(raw) and token["purpose"] == "setup"
    assert token["expires_at"] == clock.now + SETUP_LINK_SECONDS and token["issued_by"] == admin_id
    assert [(entry["actor"], entry["action"]) for entry in audit] == [(admin_id, "account.create")]
    assert '"comment": "beta wave 1"' in audit[0]["detail"]
    # The link is shown once: the admin page does not show it again.
    assert raw not in admin.get("/admin").text

    # No password yet, so no sign-in (and the same answer as any other failure).
    listener = Browser(app, address="192.0.2.60")
    failed = listener.sign_in("new.listener@example.com", PASSWORD)
    assert failed.status_code == 401 and LOGIN_FAILED in failed.text
    # The owner uses the link: the password is set and the account is signed in, verified as
    # created (no second verification step exists).
    browser, used = _set_password(app, link)
    assert used.status_code == 303 and used.headers["location"] == "/"
    assert "Signed in as <b>new.listener@example.com</b>" in browser.get("/").text
    assert _user_row(settings, "new.listener@example.com")["verified_at"] == row["verified_at"]
    assert listener.sign_in("new.listener@example.com", NEW_PASSWORD).status_code == 303
    store = AccountStore(settings.database)
    account = store.find("new.listener@example.com")
    assert account is not None and account.plan == "free" and not account.is_admin

    # A second account with the same address is refused, and an invalid address never stored.
    again = admin.post(
        "/admin/users",
        data={"csrf_token": admin.csrf(), "email": "NEW.listener@example.com", "role": "user"},
    )
    assert again.status_code == 400 and "already exists" in again.text
    bad = admin.post(
        "/admin/users", data={"csrf_token": admin.csrf(), "email": "not-an-email", "role": "user"}
    )
    assert bad.status_code == 400 and "valid email" in bad.text
    odd = admin.post(
        "/admin/users", data={"csrf_token": admin.csrf(), "email": "x@example.com", "role": "root"}
    )
    assert odd.status_code == 400 and store.find("x@example.com") is None


def test_the_operator_cli_creates_the_first_admin_pre_verified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "data" / "app.db"
    arguments = ["--database", str(database), "--origin", ORIGIN]
    assert admin_cli.main(["create", *arguments, "--email", "Owner@Example.com", "--admin"]) == 0
    printed = capsys.readouterr().out
    link = _link(printed)
    assert "owner@example.com (admin)" in printed
    store = AccountStore(Database(database))
    owner = store.find("owner@example.com")
    assert owner is not None and owner.is_admin and owner.verified_at is not None
    assert not owner.has_password
    assert link.rsplit("/", 1)[1] not in database_text(Database(database))

    assert admin_cli.main(["create", *arguments, "--email", "owner@example.com"]) == 2
    assert "already exists" in capsys.readouterr().err
    assert admin_cli.main(["reset", *arguments, "--email", "nobody@example.com"]) == 2
    assert admin_cli.main(["reset", *arguments, "--email", "owner@example.com"]) == 0
    newer = _link(capsys.readouterr().out)
    assert newer != link

    from idea_web.auth import HostedSettings
    from idea_web.database import hosted_database

    settings = HostedSettings(database=hosted_database(database), public_origin=ORIGIN)
    app = create_app(tmp_path / "work", local=False, hosted=settings)
    # Issuing the second link revoked the first.
    _, stale = _set_password(app, link)
    assert stale.status_code == 400 and LINK_FAILED in stale.text
    _, used = _set_password(app, newer)
    assert used.status_code == 303
    admin = Browser(app)
    assert admin.sign_in("owner@example.com", NEW_PASSWORD).status_code == 303
    assert admin.get("/admin").status_code == 200


# ------------------------------------------------------------------------- admin reset links
def test_an_admin_reset_link_works_exactly_once(tmp_path: Path) -> None:
    clock = Clock()
    settings, _, app = _hosted(tmp_path, clock=clock)
    admin = _admin(app, settings)
    listener = make_account(settings, "listener@example.com")
    phone, laptop = Browser(app, address="192.0.2.1"), Browser(app, address="192.0.2.2")
    phone.sign_in("listener@example.com")
    laptop.sign_in("listener@example.com")

    issued = admin.post(
        f"/admin/users/{listener.id}/reset",
        data={"csrf_token": admin.csrf(), "comment": "asked by email"},
    )
    assert issued.status_code == 200 and "A password reset link" in issued.text
    link = _link(issued.text)
    with settings.database.read() as connection:
        row = connection.execute(
            "SELECT * FROM email_tokens WHERE token_hash = ?", (token_hash(link.rsplit("/")[-1]),)
        ).fetchone()
    assert row["purpose"] == "reset" and row["expires_at"] == clock.now + RESET_LINK_SECONDS

    # Viewing the form is harmless and does not use the link.
    assert Browser(app).get(link).status_code == 200
    browser, used = _set_password(app, link)
    assert used.status_code == 303
    assert "Signed in as" in browser.get("/").text
    # Every session that existed before the reset has ended.
    assert phone.get("/").status_code == 303 and laptop.get("/csrf").status_code == 401
    # The old password is gone, the new one works.
    assert Browser(app).sign_in("listener@example.com", PASSWORD).status_code == 401
    assert Browser(app).sign_in("listener@example.com", NEW_PASSWORD).status_code == 303

    # The link works once.
    _, second = _set_password(app, link, "yet another long passphrase", address="192.0.2.51")
    assert second.status_code == 400 and LINK_FAILED in second.text
    assert (
        Browser(app).sign_in("listener@example.com", "yet another long passphrase").status_code
        == 401
    )
    with settings.database.read() as connection:
        used_row = connection.execute(
            "SELECT * FROM email_tokens WHERE id = ?", (row["id"],)
        ).fetchone()
    assert used_row["used_at"] is not None
    # ...whatever writes the row.
    with (
        pytest.raises(sqlite3.DatabaseError, match="used or revoked link"),
        settings.database.write() as connection,
    ):
        connection.execute("UPDATE email_tokens SET used_at = NULL WHERE id = ?", (row["id"],))


def test_a_reset_link_expires_and_a_newer_link_replaces_the_older_one(tmp_path: Path) -> None:
    clock = Clock()
    settings, _, app = _hosted(tmp_path, clock=clock)
    listener = make_account(settings, "listener@example.com")
    store = AccountStore(settings.database, clock=clock)

    expiring = store.issue_reset(listener.id, actor="test-admin")
    clock.advance(RESET_LINK_SECONDS)
    _, late = _set_password(app, f"/reset/{expiring.token}")
    assert late.status_code == 400 and LINK_FAILED in late.text

    older = store.issue_reset(listener.id, actor="test-admin")
    newer = store.issue_reset(listener.id, actor="test-admin")
    _, refused = _set_password(app, f"/reset/{older.token}", address="192.0.2.70")
    assert refused.status_code == 400
    _, accepted = _set_password(app, f"/reset/{newer.token}", address="192.0.2.71")
    assert accepted.status_code == 303
    # A disabled account's link does nothing.
    stale = store.issue_reset(listener.id, actor="test-admin")
    with settings.database.write() as connection:
        connection.execute("UPDATE users SET disabled_at = 1 WHERE id = ?", (listener.id,))
    assert store.redeem_link(stale.token, "a different long passphrase") is None
    with pytest.raises(AccountError):
        store.issue_reset("u" + "0" * 32, actor="test-admin")
    # Malformed links are not even looked up.
    assert Browser(app).get("/reset/short").status_code == 404
    assert store.redeem_link("../../etc", NEW_PASSWORD) is None


def _redeem_in_process(path: str, token: str, password: str, barrier, results) -> None:
    store = AccountStore(Database(Path(path)))
    barrier.wait(timeout=60)
    account = store.redeem_link(token, password)
    results.put((password, account is not None))


def _winner(settings, email: str, candidates: list[str]) -> list[str]:
    stored = _user_row(settings, email)["pw_hash"]
    winners = []
    for candidate in candidates:
        try:
            PASSWORD_HASHER.verify(stored, candidate)
            winners.append(candidate)
        except Exception:  # noqa: BLE001 - a mismatch is the expected outcome for the loser
            pass
    return winners


def test_two_simultaneous_uses_of_one_reset_link_set_exactly_one_password(tmp_path: Path) -> None:
    settings = hosted_settings(tmp_path)
    listener = make_account(settings, "listener@example.com")
    store = AccountStore(settings.database)
    candidates = ["first racing passphrase", "second racing passphrase"]

    # Threads, each with its own SQLite handle, released together.
    for _ in range(3):
        link = store.issue_reset(listener.id, actor="test-admin")
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, bool]] = []

        def use(
            password: str,
            link=link,
            barrier: threading.Barrier = barrier,
            outcomes: list[tuple[str, bool]] = outcomes,
        ) -> None:
            own = AccountStore(Database(settings.database.path))
            barrier.wait(timeout=30)
            outcomes.append((password, own.redeem_link(link.token, password) is not None))

        threads = [threading.Thread(target=use, args=(password,)) for password in candidates]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        winners = [password for password, ok in outcomes if ok]
        assert len(outcomes) == 2 and len(winners) == 1, outcomes
        assert _winner(settings, "listener@example.com", candidates) == winners

    # Two processes, released together.
    context = multiprocessing.get_context("spawn")
    link = store.issue_reset(listener.id, actor="test-admin")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_redeem_in_process,
            args=(str(settings.database.path), link.token, password, barrier, results),
        )
        for password in candidates
    ]
    for process in processes:
        process.start()
    try:
        outcomes = [results.get(timeout=120) for _ in processes]
    finally:
        for process in processes:
            process.join(60)
    winners = [password for password, ok in outcomes if ok]
    assert all(process.exitcode == 0 for process in processes)
    assert len(winners) == 1, outcomes
    assert _winner(settings, "listener@example.com", candidates) == winners
    with settings.database.read() as connection:
        used = connection.execute(
            "SELECT COUNT(*) FROM email_tokens WHERE user_id = ? AND used_at IS NOT NULL",
            (listener.id,),
        ).fetchone()[0]
    assert used == 5  # the setup link and four resets: one use each, never two


def test_a_sign_in_that_verified_the_old_password_loses_to_a_reset_that_completes_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Barrier-ordered: verify the old password, hold before the session is inserted, complete
    a reset, release. The held sign-in must fail, and no session of its may exist afterwards."""

    settings, _, app = _hosted(tmp_path)
    listener = make_account(settings, "listener@example.com")
    store = AccountStore(settings.database, clock=settings.clock)
    link = store.issue_reset(listener.id, actor="test-admin")
    auth = app.state.web.hosted
    verified, released = threading.Event(), threading.Event()
    real = auth.store.verify_login

    def held(email: str, password: str):
        account = real(email, password)
        verified.set()  # the old password has been checked...
        assert released.wait(60)  # ...and this request now holds, before its session exists
        return account

    monkeypatch.setattr(auth.store, "verify_login", held)
    attacker = Browser(app, address="203.0.113.5")
    answers: list = []
    thread = threading.Thread(
        target=lambda: answers.append(attacker.sign_in("listener@example.com", PASSWORD))
    )
    thread.start()
    assert verified.wait(60)
    monkeypatch.setattr(auth.store, "verify_login", real)
    # The owner's reset completes while that sign-in is held: the password changes and every
    # session and link of the account ends.
    owner, reset = _set_password(app, f"/reset/{link.token}")
    assert reset.status_code == 303 and "Signed in as" in owner.get("/").text
    released.set()
    thread.join(60)
    (answer,) = answers
    assert answer.status_code == 401 and LOGIN_FAILED in answer.text
    assert SESSION_COOKIE not in "".join(answer.headers.get_list("set-cookie"))
    assert attacker.session_token is None and attacker.get("/").status_code == 303
    with settings.database.read() as connection:
        live = connection.execute(
            "SELECT created_ip FROM sessions WHERE user_id = ? AND revoked_at IS NULL",
            (listener.id,),
        ).fetchall()
    assert [row["created_ip"] for row in live] == ["192.0.2.50"]  # the owner's, from the reset
    assert Browser(app).sign_in("listener@example.com", PASSWORD).status_code == 401
    assert Browser(app).sign_in("listener@example.com", NEW_PASSWORD).status_code == 303
    # The rule itself: a session is inserted only against the credential version that was
    # verified, inside the insert's own transaction.
    assert store.start_session(listener.id, credential=listener.credential) is None
    current = store.find("listener@example.com")
    assert current is not None and current.credential == listener.credential + 1
    assert store.start_session(listener.id, credential=current.credential) is not None


def test_unicode_passwords_are_bounded_after_normalisation_and_never_truncated(
    tmp_path: Path,
) -> None:
    settings, _, app = _hosted(tmp_path)
    store = AccountStore(settings.database)
    account, link = store.create_account("unicode@example.com", actor="test-admin")
    # 1,100 typed code points (e + a combining acute, 550 times) compose to 550 characters: more
    # code points than the bound allows, well inside it once normalised. (Twice that would be
    # refused earlier by the 8 KiB request-body limit, not by any password rule.)
    combining = "é" * 550
    assert len(combining) == 1100 > PASSWORD_MAX_LENGTH
    assert len(normalise_password(combining)) == 550
    _, chosen = _set_password(app, f"/reset/{link.token}", combining)
    assert chosen.status_code == 303, chosen.text
    # The same password signs in, typed either way; nothing was truncated on either side.
    assert Browser(app).sign_in("unicode@example.com", combining).status_code == 303
    assert Browser(app).sign_in("unicode@example.com", "é" * 550).status_code == 303
    assert Browser(app).sign_in("unicode@example.com", combining[:1024]).status_code == 401
    assert store.verify_login("unicode@example.com", combining) is not None
    assert store.verify_login("unicode@example.com", combining[:1024]) is None
    # A password whose NORMALISED form is too long is refused the same way when chosen and when
    # tried: 1,000 typed code points, 100 of them a letter whose canonical decomposition is two
    # code points (a composition exclusion, so NFC keeps them apart) — 1,100 characters.
    expanding = "a" * 900 + "क़" * 100
    assert len(expanding) <= PASSWORD_MAX_LENGTH < len(normalise_password(expanding))
    again = store.issue_reset(account.id, actor="test-admin")
    _, refused = _set_password(app, f"/reset/{again.token}", expanding, address="192.0.2.52")
    assert refused.status_code == 400 and f"at most {PASSWORD_MAX_LENGTH}" in refused.text
    assert store.verify_login("unicode@example.com", expanding) is None
    assert Browser(app).sign_in("unicode@example.com", combining).status_code == 303


def test_compatibility_distinct_passwords_are_distinct_credentials(tmp_path: Path) -> None:
    """NFC, not NFKC: a ligature, a circled digit, a Roman numeral and a full-width letter are
    NOT the ASCII they resemble, so choosing one string never admits its look-alike; canonically
    equivalent spellings of ONE string are still that one password, chosen or typed."""

    settings, _, app = _hosted(tmp_path)
    store = AccountStore(settings.database)
    chosen = "ligature ﬀ circled ① roman Ⅳ wide ａ passphrase"
    lookalike = "ligature ff circled 1 roman IV wide a passphrase"
    assert unicodedata.normalize("NFKC", chosen) == unicodedata.normalize("NFKC", lookalike)
    assert normalise_password(chosen) != normalise_password(lookalike)
    account, link = store.create_account("exact@example.com", actor="test-admin")
    _, used = _set_password(app, f"/reset/{link.token}", chosen)
    assert used.status_code == 303
    assert Browser(app).sign_in("exact@example.com", chosen).status_code == 303
    assert Browser(app).sign_in("exact@example.com", lookalike).status_code == 401
    assert store.verify_login("exact@example.com", lookalike) is None
    # And the other way round.
    again = store.issue_reset(account.id, actor="test-admin")
    _, used = _set_password(app, f"/reset/{again.token}", lookalike, address="192.0.2.53")
    assert used.status_code == 303
    assert Browser(app).sign_in("exact@example.com", lookalike).status_code == 303
    assert Browser(app).sign_in("exact@example.com", chosen).status_code == 401

    # Canonical equivalence still holds across set-then-sign-in, and at the confirmation field:
    # a precomposed é and an Angstrom sign, typed as e + combining acute and Å.
    canonical = "café Å passphrase!!"
    typed = "café Å passphrase!!"
    assert canonical != typed and normalise_password(canonical) == normalise_password(typed)
    third = store.issue_reset(account.id, actor="test-admin")
    browser = Browser(app, address="192.0.2.54")
    page = browser.get(f"/reset/{third.token}")
    confirmed = browser.post(
        f"/reset/{third.token}",
        data={"csrf_token": csrf_in(page.text), "password": canonical, "confirm": typed},
    )
    assert confirmed.status_code == 303
    assert Browser(app).sign_in("exact@example.com", typed).status_code == 303
    assert Browser(app).sign_in("exact@example.com", canonical).status_code == 303
    assert Browser(app).sign_in("exact@example.com", lookalike).status_code == 401


# ----------------------------------------------------------------------------------- escaping
def test_xss_through_titles_and_comments_is_escaped(tmp_path: Path) -> None:
    settings, jobs, app = _hosted(tmp_path)
    admin = _admin(app, settings)
    target = make_account(settings, "listener@example.com")

    # A comment is stored as typed and shown as text on the admin page.
    admin.post(f"/admin/users/{target.id}/reset", data={"csrf_token": admin.csrf(), "comment": XSS})
    admin.post(
        f"/admin/users/{target.id}/plan",
        data={"csrf_token": admin.csrf(), "plan": "pro", "comment": XSS},
    )
    admin.post(
        "/admin/users",
        data={"csrf_token": admin.csrf(), "email": "third@example.com", "comment": XSS},
    )
    # A mix title comes from the platform, so it is untrusted too.
    job_id = jobs.submit(MIX, "free")
    jobs.jobs[job_id].resolved_title = XSS
    jobs.jobs[job_id].status = "failed"

    pages = {
        "admin": admin.get("/admin"),
        "home": admin.get("/"),
        "job": admin.get(f"/jobs/{job_id}"),
        "activity": admin.get("/activity"),
        # Error paths echo what was typed.
        "admin error": admin.post(
            "/admin/users", data={"csrf_token": admin.csrf(), "email": XSS, "role": "user"}
        ),
        "analyse error": admin.post(
            "/analyse", data={"csrf_token": admin.csrf(), "url": XSS, "known_tracklist": XSS}
        ),
        "login error": Browser(app).sign_in(XSS, "whatever it is"),
        "password error": _set_password(app, "/reset/" + "D" * 43, "short")[1],
    }
    for name, response in pages.items():
        text = response.text
        assert "<script>alert" not in text and "<img src=x" not in text, name
        parsed = _markup(text)
        assert parsed.handlers == [], name
        if name != "activity":  # a fragment, not a document
            _assert_csp_names_exactly_the_inline_scripts(response, name)
    assert pages["admin"].text.count("&lt;script&gt;alert(") >= 3  # three audited comments
    assert "&lt;script&gt;alert(" in pages["home"].text
    assert "&lt;script&gt;alert(" in pages["job"].text
    assert "&lt;script&gt;alert(" in pages["activity"].text
    assert "&lt;script&gt;alert(" in pages["analyse error"].text
    assert "&lt;script&gt;alert(" in pages["login error"].text
    assert pages["password error"].status_code == 400
    # The job title never reaches the page's one inline script unescaped either.
    assert XSS not in pages["job"].text


# --------------------------------------------------------------------------------- no sign-up
def test_there_is_no_self_signup_route(tmp_path: Path) -> None:
    settings, _, app = _hosted(tmp_path)
    make_account(settings, "listener@example.com")
    count = len(AccountStore(settings.database).accounts())
    fields = {"email": "stranger@example.com", "password": PASSWORD, "confirm": PASSWORD}

    anonymous = Browser(app, address="203.0.113.99")
    login = anonymous.get("/login")
    assert "there is no sign-up" in login.text and 'action="/signup"' not in login.text
    fields["csrf_token"] = csrf_in(login.text)
    candidates = [
        "/signup",
        "/sign-up",
        "/register",
        "/join",
        "/invite",
        "/accounts",
        "/accounts/new",
        "/users",
        "/users/new",
        "/api/users",
        "/admin/users",
        "/login/new",
    ]
    for path in candidates:
        for method in ("GET", "POST"):
            response = anonymous.request(method, path, data=fields)
            # Anonymous: every one of them is "sign in first" (never a form, never an account).
            assert response.status_code in (303, 401, 404), (method, path)
            assert "stranger" not in response.text

    signed_in = Browser(app, address="203.0.113.98")
    signed_in.sign_in("listener@example.com")
    fields["csrf_token"] = signed_in.csrf()
    for path in candidates:
        for method in ("GET", "POST"):
            response = signed_in.request(method, path, data=fields)
            assert response.status_code == 404, (method, path)
    # A non-admin cannot reach the admin surface at all.
    assert signed_in.get("/admin").status_code == 404
    listener = AccountStore(settings.database).find("listener@example.com")
    assert (
        signed_in.post(
            f"/admin/users/{listener.id}/plan", data={**fields, "plan": "pro"}
        ).status_code
        == 404
    )

    store = AccountStore(settings.database)
    assert len(store.accounts()) == count and store.find("stranger@example.com") is None
    assert store.get(listener.id).plan == "free"
    assert "stranger" not in database_text(settings.database)

    # The application registers only its catch-all routes: no framework-generated extras.
    paths = sorted({getattr(route, "path", "") for route in app.routes})
    assert paths == ["/{rest:path}"]


def test_admin_actions_need_an_admin_session_are_audited_and_absent_locally(
    tmp_path: Path,
) -> None:
    settings, _, app = _hosted(tmp_path)
    admin = _admin(app, settings)
    listener = make_account(settings, "listener@example.com")
    page = admin.get("/admin")
    assert page.status_code == 200 and "listener@example.com" in page.text
    changed = admin.post(
        f"/admin/users/{listener.id}/plan",
        data={"csrf_token": admin.csrf(), "plan": "pro", "comment": "comp"},
    )
    assert changed.status_code == 200 and "is now on the pro plan" in changed.text
    same = admin.post(
        f"/admin/users/{listener.id}/plan", data={"csrf_token": admin.csrf(), "plan": "pro"}
    )
    assert "was already on the pro plan" in same.text
    bad = admin.post(
        f"/admin/users/{listener.id}/plan", data={"csrf_token": admin.csrf(), "plan": "gold"}
    )
    assert bad.status_code == 400 and "valid plan" in bad.text
    unknown = admin.post(
        f"/admin/users/u{'0' * 32}/reset", data={"csrf_token": admin.csrf(), "comment": ""}
    )
    assert unknown.status_code == 404
    with settings.database.read() as connection:
        actions = [
            row[0]
            for row in connection.execute(
                "SELECT action FROM admin_audit WHERE target = ? ORDER BY id", (listener.id,)
            )
        ]
        plans = connection.execute(
            "SELECT plan, status FROM entitlements WHERE user_id = ? ORDER BY id", (listener.id,)
        ).fetchall()
    assert actions == ["account.create", "account.plan"]  # an unchanged plan is not an action
    assert [tuple(row) for row in plans] == [("pro", "active")]
    # The same entitlement rules hold in the database: one active plan per account.
    with pytest.raises(sqlite3.IntegrityError), settings.database.write() as connection:
        connection.execute(
            "INSERT INTO entitlements(user_id, plan, status, set_by, created_at) "
            "VALUES (?, 'free', 'active', 'x', 1)",
            (listener.id,),
        )

    # An admin whose account is demoted loses the surface on the next request.
    with settings.database.write() as connection:
        connection.execute("UPDATE users SET role = 'user' WHERE email = 'admin@example.com'")
    assert admin.get("/admin").status_code == 404

    # Local mode has no admin surface at all.
    local = create_app(tmp_path / "local")
    for method in ("GET", "POST"):
        for path in ("/admin", "/admin/users", f"/admin/users/{listener.id}/reset"):
            assert request(local, method, path).status_code == 404

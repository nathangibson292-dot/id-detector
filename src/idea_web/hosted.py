"""Hosted mode's front door: sign-in, sign-out, set-password links and the admin page (4c-i, 4c-ii).

None of these routes exists in local mode: :func:`idea_web.application.create_app` only builds a
:class:`HostedAuth` when it is given :class:`~idea_web.auth.HostedSettings`, and refuses those
settings in local mode. There is no sign-up route of any kind (plan §3.6: M1 accounts are created by
an admin, pre-verified, and receive a single-use set-password link).

Every page here is a Jinja template with autoescaping on, is ``no-store``, and goes through
``html_response`` (the per-document CSP; none of these pages has an inline script).
"""

from __future__ import annotations

import logging
import math
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs

from jinja2 import Environment
from starlette.requests import Request
from starlette.responses import Response

from idea_web import pages
from idea_web.auth import (
    PASSWORD_MIN_LENGTH,
    PLANS,
    PRESESSION_COOKIE,
    SESSION_COOKIE,
    AccountError,
    AccountStore,
    HostedSettings,
    IssuedLink,
    RateLimiter,
    SignedIn,
    client_address,
    expired_cookie,
    normalise_password,
    password_problem,
    presession_cookie,
    presession_csrf,
    rate_bucket,
    session_cookie,
    tokens_equal,
    well_formed_token,
)
from idea_web.http import TEXT, bytes_response, html_response, json_response, not_found, redirect

_CSRF_FIELD = "csrf_token"
_RESET_ROUTE = re.compile(r"/reset/([A-Za-z0-9_-]{43})")
_ADMIN_ROUTE = re.compile(r"/admin/users/(u[0-9a-f]{32})/(reset|plan)")
LOGIN_FAILED = "That email and password did not match an account."
PAGE_EXPIRED = "This page expired. Reload it and try again."
LINK_FAILED = "This link has expired or has already been used. Ask the ID'er team for a new one."
EXPORTS_LIMITED = b"Too many exports from your network. Try again in a minute."

# ---------------------------------------------------------------------------------- log redaction
#: A single-use link's token travels in the request target (``/reset/<token>``), so any access
#: log that names the target would store the bearer secret. Hosted mode installs this filter on
#: uvicorn's loggers (:func:`redact_secret_paths`): it rewrites the token out of every record's
#: message and arguments before any handler formats them.
_SECRET_PATH = re.compile(r"(/reset/)[A-Za-z0-9_-]{43}")
REDACTED_LINK = "/reset/[redacted]"
ACCESS_LOGGERS = ("uvicorn.access", "uvicorn.error", "uvicorn")


def redact_secret_path(text: str) -> str:
    return _SECRET_PATH.sub(REDACTED_LINK, text)


class SecretPathFilter(logging.Filter):
    """Redacts single-use link tokens from a log record, whatever formats it afterwards."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secret_path(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_secret_path(value) if isinstance(value, str) else value
                for value in record.args
            )
        elif isinstance(record.args, Mapping):
            record.args = {
                key: redact_secret_path(value) if isinstance(value, str) else value
                for key, value in record.args.items()
            }
        return True


def redact_secret_paths() -> None:
    """Install :class:`SecretPathFilter` on uvicorn's loggers (once per process)."""

    for name in ACCESS_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(existing, SecretPathFilter) for existing in logger.filters):
            logger.addFilter(SecretPathFilter())


def _form(raw: bytes | None) -> dict[str, str] | None:
    if raw is None:
        return None
    try:
        parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    except (UnicodeDecodeError, ValueError):
        return None
    return {key: values[0] for key, values in parsed.items()}


def _minutes(seconds: float) -> int:
    return max(1, math.ceil(seconds / 60))


def _when(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%d %H:%M")


def with_cookie(response: Response, cookie: str) -> Response:
    response.headers.append("Set-Cookie", cookie)
    return response


def _too_many(response: Response, retry_after: float) -> Response:
    response.headers["Retry-After"] = str(math.ceil(retry_after))
    return response


@dataclass
class HostedAuth:
    settings: HostedSettings
    templates: Environment
    store: AccountStore = field(init=False)
    limiter: RateLimiter = field(init=False)
    #: Signs the pre-sign-in forms' tokens; a restart simply expires forms shown before it.
    presession_key: bytes = field(default_factory=lambda: secrets.token_bytes(32))

    def __post_init__(self) -> None:
        self.store = AccountStore(self.settings.database, clock=self.settings.clock)
        self.limiter = RateLimiter(self.settings.database, clock=self.settings.clock)

    # --------------------------------------------------------------------------- plumbing
    def client(self, request: Request) -> str:
        peer = request.client.host if request.client is not None else None
        return client_address(
            peer, request.headers.getlist("x-forwarded-for"), self.settings.trusted_proxies
        )

    def bucket(self, request: Request) -> str:
        return rate_bucket(self.client(request))

    def admit(self, action: str, request: Request) -> float | None:
        return self.limiter.admit(action, self.bucket(request))

    def session(self, request: Request) -> SignedIn | None:
        cookie = request.cookies.get(SESSION_COOKIE)
        return self.store.authenticate(cookie) if cookie else None

    def finish(self, request: Request, response: Response, session: SignedIn | None) -> Response:
        """Carry a rotated session's new cookie, or clear a cookie that no longer works.

        A cookie whose session was SUPERSEDED is never cleared: the request that superseded it
        (a rotation on plan change, a sign-in, or a set-password link) handed this same browser
        the replacement, and two answers in flight at once arrive in either order — a clearing
        answer arriving last would sign the browser out of the session it was just given. Every
        other dead cookie (logged out, expired, revoked by a reset made elsewhere) is cleared.
        """

        if session is not None and session.new_token is not None:
            lifetime = int(session.expires_at - self.settings.clock())
            with_cookie(response, session_cookie(session.new_token, lifetime))
        elif session is None:
            presented = request.cookies.get(SESSION_COOKIE)
            if presented and not self.store.superseded(presented):
                with_cookie(response, expired_cookie(SESSION_COOKIE))
        return response

    def refuse_export(self, wait: float) -> Response:
        """The export limit's answer (plan §4.4: 10 per minute per address)."""

        return _too_many(bytes_response(HTTPStatus.TOO_MANY_REQUESTS, EXPORTS_LIMITED, TEXT), wait)

    def _page(self, name: str, status: int, **context: Any) -> Response:
        body = self.templates.get_template(name).render(**context).encode("utf-8")
        return html_response(status, body)

    def _presession(self, request: Request) -> tuple[str, str | None]:
        """The pre-sign-in cookie token (existing or new) and the cookie to set, if new."""

        existing = request.cookies.get(PRESESSION_COOKIE)
        if well_formed_token(existing):
            assert existing is not None
            return existing, None
        token = secrets.token_urlsafe(32)
        return token, presession_cookie(token)

    def _presession_ok(self, request: Request, presented: object) -> bool:
        cookie = request.cookies.get(PRESESSION_COOKIE)
        if not well_formed_token(cookie):
            return False
        assert cookie is not None
        return tokens_equal(presented, presession_csrf(self.presession_key, cookie))

    # ----------------------------------------------------------------------------- sign in
    def login_page(
        self,
        request: Request,
        *,
        status: int = HTTPStatus.OK,
        email: str = "",
        error: str = "",
        notice: str = "",
    ) -> Response:
        token, cookie = self._presession(request)
        response = self._page(
            "login.html",
            status,
            head=pages.head_html("Sign in — ID'er", hosted=True),
            topbar=pages.brand_bar_html(),
            csrf=presession_csrf(self.presession_key, token),
            email=email,
            error=error,
            notice=notice,
        )
        return with_cookie(response, cookie) if cookie else response

    def login_get(self, request: Request, session: SignedIn | None) -> Response:
        if session is not None:
            return redirect("/")
        notice = ""
        if parse_qs(request.url.query).get("signed_out"):
            notice = "You have signed out."
        return self.login_page(request, notice=notice)

    def login_post(self, request: Request, raw: bytes | None) -> Response:
        form = _form(raw)
        if form is None:
            return self.login_page(
                request, status=HTTPStatus.BAD_REQUEST, error="The form could not be read."
            )
        email = form.get("email", "")[:254]
        if not self._presession_ok(request, form.get(_CSRF_FIELD)):
            return self.login_page(
                request, status=HTTPStatus.FORBIDDEN, email=email, error=PAGE_EXPIRED
            )
        # The limit applies before any lookup, so it says nothing about whether an account exists.
        wait = self.admit("login", request)
        if wait is not None:
            return _too_many(
                self.login_page(
                    request,
                    status=HTTPStatus.TOO_MANY_REQUESTS,
                    email=email,
                    error=(
                        "Too many sign-in attempts from your network. "
                        f"Try again in {_minutes(wait)} minutes."
                    ),
                ),
                wait,
            )
        account = self.store.verify_login(email, form.get("password", ""))
        issued = None
        if account is not None:
            # Issued only if the password verified is still the account's: a reset that completed
            # in between leaves this request with no session and the same failure as a wrong
            # password.
            issued = self.store.start_session(
                account.id,
                credential=account.credential,
                ip=self.client(request),
                replacing=request.cookies.get(SESSION_COOKIE),
            )
        if issued is None:
            return self.login_page(
                request, status=HTTPStatus.UNAUTHORIZED, email=email, error=LOGIN_FAILED
            )
        response = redirect("/")
        lifetime = int(issued.expires_at - self.settings.clock())
        with_cookie(response, session_cookie(issued.token, lifetime))
        return with_cookie(response, expired_cookie(PRESESSION_COOKIE))

    def logout_post(self, request: Request, raw: bytes | None, session: SignedIn) -> Response:
        form = _form(raw) or {}
        presented = request.headers.get("X-CSRF-Token") or form.get(_CSRF_FIELD)
        if not tokens_equal(presented, session.request_csrf):
            return json_response(HTTPStatus.FORBIDDEN, {"error": "CSRF token was invalid"})
        self.store.revoke_session(session.token)
        if session.new_token is not None:
            self.store.revoke_session(session.new_token)
        response = redirect("/login?signed_out=1")
        return with_cookie(response, expired_cookie(SESSION_COOKIE))

    # ------------------------------------------------------------------ set-password links
    def _password_page(
        self, request: Request, token: str, *, status: int = HTTPStatus.OK, error: str = ""
    ) -> Response:
        presession, cookie = self._presession(request)
        response = self._page(
            "set_password.html",
            status,
            head=pages.head_html("Choose a password — ID'er", hosted=True),
            topbar=pages.brand_bar_html(),
            csrf=presession_csrf(self.presession_key, presession),
            action=f"/reset/{token}",
            min_length=PASSWORD_MIN_LENGTH,
            error=error,
        )
        return with_cookie(response, cookie) if cookie else response

    def password_get(self, request: Request, route: str) -> Response:
        # The page does not look the link up: showing the form reveals nothing and writes nothing.
        match = _RESET_ROUTE.fullmatch(route)
        if match is None:
            return not_found()
        return self._password_page(request, match.group(1))

    def password_post(self, request: Request, route: str, raw: bytes | None) -> Response:
        match = _RESET_ROUTE.fullmatch(route)
        if match is None:
            return not_found()
        token = match.group(1)
        form = _form(raw)
        if form is None:
            return self._password_page(
                request, token, status=HTTPStatus.BAD_REQUEST, error="The form could not be read."
            )
        if not self._presession_ok(request, form.get(_CSRF_FIELD)):
            return self._password_page(
                request, token, status=HTTPStatus.FORBIDDEN, error=PAGE_EXPIRED
            )
        password = normalise_password(form.get("password", ""))
        problem = password_problem(password, normalise_password(form.get("confirm", "")))
        if problem is not None:
            return self._password_page(request, token, status=HTTPStatus.BAD_REQUEST, error=problem)
        wait = self.admit("reset", request)
        if wait is not None:
            return _too_many(
                self._password_page(
                    request,
                    token,
                    status=HTTPStatus.TOO_MANY_REQUESTS,
                    error=(
                        "Too many attempts from your network. "
                        f"Try again in {_minutes(wait)} minutes."
                    ),
                ),
                wait,
            )
        # One transaction: the link is used, the password set, every old session and link
        # revoked, and the owner's new session issued (superseding the cookie this browser sent).
        result = self.store.redeem_and_start_session(
            token,
            password,
            ip=self.client(request),
            replacing=request.cookies.get(SESSION_COOKIE),
        )
        if result is None:
            return self._password_page(
                request, token, status=HTTPStatus.BAD_REQUEST, error=LINK_FAILED
            )
        _account, issued = result
        response = redirect("/")
        lifetime = int(issued.expires_at - self.settings.clock())
        with_cookie(response, session_cookie(issued.token, lifetime))
        return with_cookie(response, expired_cookie(PRESESSION_COOKIE))

    # -------------------------------------------------------------------------------- admin
    def link_url(self, link: IssuedLink) -> str:
        return f"{self.settings.public_origin}/reset/{link.token}"

    def admin_page(
        self,
        session: SignedIn,
        *,
        status: int = HTTPStatus.OK,
        error: str = "",
        notice: str = "",
        email: str = "",
        link: IssuedLink | None = None,
        link_email: str = "",
    ) -> Response:
        audit = [
            {
                "when": _when(entry["at"]),
                "actor": entry["actor"],
                "action": entry["action"],
                "target": entry["target"],
                "comment": str(entry["detail"].get("comment", "")),
            }
            for entry in self.store.audit()
        ]
        account = session.account
        return self._page(
            "admin.html",
            status,
            head=pages.head_html("Accounts — ID'er admin", hosted=True),
            topbar=pages.brand_bar_html(),
            strip=pages.account_strip_html(account.email, session.csrf, admin=True),
            csrf=session.csrf,
            accounts=self.store.accounts(),
            plans=PLANS,
            audit=audit,
            error=error,
            notice=notice,
            email=email,
            link=self.link_url(link) if link is not None else "",
            link_label=(
                "A set-password link"
                if link is not None and link.purpose == "setup"
                else "A password reset link"
            ),
            link_email=link_email,
            link_expires=_when(link.expires_at) + " UTC" if link is not None else "",
        )

    def admin_post(
        self, request: Request, route: str, raw: bytes | None, session: SignedIn
    ) -> Response:
        form = _form(raw)
        if form is None:
            return self.admin_page(
                session, status=HTTPStatus.BAD_REQUEST, error="The form could not be read."
            )
        if not tokens_equal(form.get(_CSRF_FIELD), session.request_csrf):
            return self.admin_page(session, status=HTTPStatus.FORBIDDEN, error=PAGE_EXPIRED)
        actor = session.account.id
        comment = form.get("comment", "")
        try:
            if route == "/admin/users":
                account, link = self.store.create_account(
                    form.get("email", ""),
                    actor=actor,
                    role=form.get("role", "user"),
                    created_ip=self.client(request),
                    comment=comment,
                )
                return self.admin_page(session, link=link, link_email=account.email)
            match = _ADMIN_ROUTE.fullmatch(route)
            if match is None:
                return not_found()
            target = self.store.get(match.group(1))
            if target is None:
                return not_found()
            if match.group(2) == "reset":
                link = self.store.issue_reset(target.id, actor=actor, comment=comment)
                return self.admin_page(session, link=link, link_email=target.email)
            plan = form.get("plan", "")
            changed = self.store.set_plan(target.id, plan, actor=actor, comment=comment)
            notice = (
                f"{target.email} is now on the {plan} plan."
                if changed
                else f"{target.email} was already on the {plan} plan."
            )
            return self.admin_page(session, notice=notice)
        except AccountError as exc:
            return self.admin_page(
                session,
                status=HTTPStatus.BAD_REQUEST,
                error=str(exc),
                email=form.get("email", "")[:254],
            )


def unauthenticated(request: Request, *, wants_json: bool) -> Response:
    """No live session: scripts get a 401, people get the sign-in page."""

    del request
    if wants_json:
        return json_response(HTTPStatus.UNAUTHORIZED, {"error": "sign in required"})
    return redirect("/login")

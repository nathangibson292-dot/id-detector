"""A cookie-keeping browser over the hosted ASGI app, and account fixtures (cycles 4c-i, 4c-ii).

Everything runs in-process over ``httpx.ASGITransport``: no port is opened and no provider is
called. The browser talks to ``https://idea.test`` so that ``Secure`` cookies are kept and sent
exactly as a real browser would, and it sends the public ``Origin`` on every POST unless told not
to.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from id_detector.webapp.jobs import Job, TargetValidationError
from idea_web.auth import SESSION_COOKIE, Account, AccountStore, HostedSettings
from idea_web.database import Database, hosted_database

ORIGIN = "https://idea.test"
PASSWORD = "correct horse battery staple"
_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')
_ABSENT = object()


class Clock:
    """A settable clock shared by the app and the test."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def hosted_settings(
    tmp_path: Path, *, clock: Callable[[], float] | None = None, **kwargs: Any
) -> HostedSettings:
    # Opened as a hosted operator would: migrated under the worker supervisor lock next to it.
    database = hosted_database(tmp_path / "hosted-data" / "app.db")
    if clock is not None:
        kwargs["clock"] = clock
    return HostedSettings(database=database, public_origin=ORIGIN, **kwargs)


def make_account(
    settings: HostedSettings,
    email: str,
    *,
    role: str = "user",
    password: str = PASSWORD,
) -> Account:
    """An admin-created account whose owner has already used the set-password link."""

    store = AccountStore(settings.database, clock=settings.clock)
    account, link = store.create_account(email, actor="test-admin", role=role)
    redeemed = store.redeem_link(link.token, password)
    assert redeemed is not None and redeemed.id == account.id
    return redeemed


class RecordingJobs:
    """A queue adapter that records what hosted mode submits (no worker, no provider)."""

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.submitted: list[dict[str, Any]] = []

    def submit(
        self,
        target: str,
        profile: str | None = None,
        *,
        acquire: bool = False,
        build_index: bool = False,
        known_tracklist: str | None = None,
        user_id: str | None = None,
    ) -> str:
        if "://" not in target:
            raise TargetValidationError("not a web link")
        job = Job(uuid.uuid4().hex, target, target, profile, acquire, build_index, known_tracklist)
        self.jobs[job.id] = job
        self.submitted.append({"id": job.id, "target": target, "user_id": user_id})
        return job.id

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def recent(self, limit: int = 25) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda job: -job.created_at)[:limit]

    def cancel(self, job_id: str) -> bool:
        return job_id in self.jobs

    def dismiss(self, job_id: str) -> bool:
        return self.jobs.pop(job_id, None) is not None


def database_text(database: Database) -> str:
    """Every value in every table, as one string (for "this secret is not stored" checks)."""

    parts: list[str] = []
    with database.read() as connection:
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        for table in tables:
            for row in connection.execute(f'SELECT * FROM "{table}"'):
                parts.extend(str(value) for value in tuple(row))
    return "\n".join(parts)


def csrf_in(html: str) -> str:
    match = _CSRF.search(html)
    assert match is not None, "the page carries no csrf_token field"
    return match.group(1)


class Browser:
    """One browser: its own cookie jar and its own client address."""

    def __init__(self, app: Any, *, address: str = "198.51.100.7") -> None:
        self.app = app
        self.address = address
        self.cookies = httpx.Cookies()

    def request(
        self,
        method: str,
        path: str,
        *,
        origin: object = _ABSENT,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        sent = dict(headers or {})
        if method == "POST" and origin is _ABSENT:
            sent["origin"] = ORIGIN
        elif isinstance(origin, str):
            sent["origin"] = origin

        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app, client=(self.address, 50000))
            async with httpx.AsyncClient(
                transport=transport, base_url=ORIGIN, cookies=self.cookies
            ) as client:
                response = await client.request(method, path, headers=sent, **kwargs)
                self.cookies = httpx.Cookies(client.cookies)
                return response

        return asyncio.run(send())

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    @property
    def session_token(self) -> str | None:
        return self.cookies.get(SESSION_COOKIE)

    def sign_in(self, email: str, password: str = PASSWORD) -> httpx.Response:
        page = self.get("/login")
        assert page.status_code == 200, page.text
        return self.post(
            "/login",
            data={"csrf_token": csrf_in(page.text), "email": email, "password": password},
        )

    def csrf(self) -> str:
        response = self.get("/csrf")
        assert response.status_code == 200, response.text
        return str(response.json()["token"])

"""Accounts, sessions, single-use links, CSRF and per-IP limits for HOSTED mode (4c-i, 4c-ii).

Plan §3.6 and §4.4 are the contract. Local mode (``idea serve`` / ``idea.cmd`` on plain loopback
HTTP) never touches anything here: a ``Secure`` cookie is not even sent over ``http://``, so the
owner's tool stays sign-in free and keeps its one process-wide CSRF token.

What is stored, and what is not:

- a password only as an argon2id hash (:data:`PASSWORD_HASHER`, parameters below);
- a session only as ``sha256(token)``; the token itself lives in the browser's
  ``__Host-idea_session`` cookie (``HttpOnly; Secure; SameSite=Lax``);
- a single-use link only as ``sha256(token)``; the raw link is shown once, to the admin who
  issued it, and is never logged.

A session's CSRF token is derived from its raw cookie token (HMAC), so it is never stored either,
and the database alone is not enough to forge a request. Forms shown before sign-in (the sign-in
and set-password forms) use a short-lived pre-session cookie signed with a per-process key.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from idea_web.database import Database

# --------------------------------------------------------------------------------------- passwords
#: argon2id with RFC 9106's second recommended profile: 3 passes over 64 MiB, 4 lanes, a 16-byte
#: salt and a 32-byte tag. ``check_needs_rehash`` upgrades a stored hash at the next sign-in if
#: these ever change.
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST_KIB = 64 * 1024
ARGON2_PARALLELISM = 4
ARGON2_HASH_LEN = 32
ARGON2_SALT_LEN = 16
PASSWORD_HASHER = PasswordHasher(
    time_cost=ARGON2_TIME_COST,
    memory_cost=ARGON2_MEMORY_COST_KIB,
    parallelism=ARGON2_PARALLELISM,
    hash_len=ARGON2_HASH_LEN,
    salt_len=ARGON2_SALT_LEN,
    type=Type.ID,
)
PASSWORD_MIN_LENGTH = 12
#: A bound on the work one request can ask argon2 to do (the body limit is 8 KiB anyway).
PASSWORD_MAX_LENGTH = 1024

# ---------------------------------------------------------------------------------------- sessions
SESSION_COOKIE = "__Host-idea_session"
PRESESSION_COOKIE = "__Host-idea_presession"
SESSION_IDLE_SECONDS = 30 * 24 * 3600
SESSION_ABSOLUTE_SECONDS = 90 * 24 * 3600
#: ``last_seen_at`` is rewritten at most this often, so polling does not write on every request.
SESSION_TOUCH_SECONDS = 300
PRESESSION_SECONDS = 2 * 3600
#: Single-use links: a new account's set-password link, and an admin-issued reset link.
SETUP_LINK_SECONDS = 7 * 24 * 3600
RESET_LINK_SECONDS = 24 * 3600
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_CSRF_CONTEXT = b"idea-csrf-v1"

# ------------------------------------------------------------------------------------- rate limits
#: Per-IP limits (plan §4.4): attempts admitted per window, in seconds.
RATE_LIMITS: Mapping[str, tuple[int, float]] = {
    "login": (10, 15 * 60.0),
    "reset": (3, 3600.0),
    "analyse": (5, 3600.0),
    "export": (10, 60.0),
}

PLANS = ("free", "pro")
ROLES = ("user", "admin")
_EMAIL = re.compile(r"[^@\s<>\"'`,;:()\[\]\\]+@[^@\s<>\"'`,;:()\[\]\\]+\.[^@\s<>\"'`,;:()\[\]\\]+")
EMAIL_MAX_LENGTH = 254
COMMENT_MAX_LENGTH = 500


class AccountError(ValueError):
    """An admin action that cannot be carried out; the message is safe to show to the admin."""


def normalise_email(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def valid_email(value: str) -> bool:
    return len(value) <= EMAIL_MAX_LENGTH and _EMAIL.fullmatch(value) is not None


def normalise_password(value: str) -> str:
    """NIST SP 800-63B: Unicode passwords are compared after normalisation — NFC, and only NFC.

    NFC makes canonically equivalent spellings one password (``e`` + combining acute is ``é``)
    without inventing aliases: NFKC would also fold compatibility characters (``ﬀ`` to ``ff``,
    ``①`` to ``1``, full-width letters to ASCII), so a password chosen with one of them would
    admit a genuinely different string. Every length rule is applied to THIS form and never to
    the typed one, and nothing is ever truncated: 1,100 typed code points that compose to 550
    characters are a 550-character password, the same one at sign-in as when it was chosen.
    """

    return unicodedata.normalize("NFC", value)


def password_problem(password: str, confirm: str) -> str | None:
    """Why a new password (already normalised) is refused, in words for the person choosing it."""

    if password != confirm:
        return "The two passwords do not match."
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Use at least {PASSWORD_MIN_LENGTH} characters."
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"Use at most {PASSWORD_MAX_LENGTH} characters."
    return None


def hash_password(password: str) -> str:
    return PASSWORD_HASHER.hash(normalise_password(password))


_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    """A real hash with the live parameters, verified when no account (or no password) exists,
    so a missing account costs the same argon2 work as a wrong password."""

    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = PASSWORD_HASHER.hash(secrets.token_urlsafe(24))
    return _DUMMY_HASH


def _verify(stored: str | None, password: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(stored or _dummy_hash(), normalise_password(password)) and (
            stored is not None
        )
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def new_token() -> str:
    """A 256-bit URL-safe token (43 characters)."""

    return secrets.token_urlsafe(32)


def well_formed_token(value: object) -> bool:
    return isinstance(value, str) and _TOKEN.fullmatch(value) is not None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def session_csrf(session_token: str) -> str:
    """The synchroniser token of one session: derivable only from the raw cookie token."""

    digest = hmac.new(session_token.encode("ascii"), _CSRF_CONTEXT, hashlib.sha256).digest()
    return digest.hex()


def presession_csrf(key: bytes, presession_token: str) -> str:
    """The synchroniser token of a form shown before sign-in, bound to its pre-session cookie."""

    message = b"idea-presession-v1:" + presession_token.encode("ascii")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def tokens_equal(presented: object, expected: str) -> bool:
    if not isinstance(presented, str) or not presented or not expected:
        return False
    return secrets.compare_digest(presented.strip().encode("utf-8"), expected.encode("utf-8"))


def session_cookie(token: str, max_age: int) -> str:
    return (
        f"{SESSION_COOKIE}={token}; Path=/; Max-Age={max(0, max_age)}; HttpOnly; Secure; "
        "SameSite=Lax"
    )


def presession_cookie(token: str) -> str:
    return (
        f"{PRESESSION_COOKIE}={token}; Path=/; Max-Age={PRESESSION_SECONDS}; HttpOnly; Secure; "
        "SameSite=Strict"
    )


def expired_cookie(name: str) -> str:
    return f"{name}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"


# --------------------------------------------------------------------------------- hosted settings
@dataclass(frozen=True)
class HostedSettings:
    """What hosted mode needs beyond local mode: the account database and the public origin.

    ``public_origin`` is the one origin browsers use (``https://host[:port]``); every request must
    name its host, and every POST must carry exactly this ``Origin``. ``trusted_proxies`` lists
    the networks (Caddy's address) whose ``X-Forwarded-For`` is believed; nothing else is.
    """

    database: Database
    public_origin: str
    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    clock: Callable[[], float] = field(default=time.time)

    def __post_init__(self) -> None:
        parts = urlsplit(self.public_origin)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
            or parts.username is not None
        ):
            raise ValueError("public_origin must be https://host[:port] with nothing after it")
        # A browser's Origin never names the default port, so neither does ours.
        netloc = parts.hostname.casefold()
        if ":" in netloc:
            netloc = f"[{netloc}]"
        if parts.port not in (None, 443):
            netloc = f"{netloc}:{parts.port}"
        object.__setattr__(self, "public_origin", f"https://{netloc}")

    @property
    def host(self) -> str:
        return urlsplit(self.public_origin).netloc

    @staticmethod
    def parse_proxies(
        values: Iterable[str],
    ) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        return tuple(ipaddress.ip_network(value.strip(), strict=False) for value in values if value)


# --------------------------------------------------------------------------------- client buckets
def _address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    text = value.strip()
    if text.startswith("[") and "]" in text:
        text = text[1 : text.index("]")]
    try:
        address = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def client_address(
    peer: str | None,
    forwarded_for: Sequence[str],
    trusted_proxies: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str:
    """The address a limit applies to.

    ``X-Forwarded-For`` is read only when the connection itself comes from a trusted proxy, and
    then from the right: the first hop that is not a trusted proxy is the client. A client cannot
    choose its bucket by sending the header itself.
    """

    direct = _address(peer or "")
    if direct is None:
        return peer or "unknown"

    def trusted(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return any(address in network for network in trusted_proxies)

    if not trusted(direct):
        return str(direct)
    hops = [hop for header in forwarded_for for hop in header.split(",") if hop.strip()]
    for hop in reversed(hops):
        address = _address(hop)
        if address is None:
            break  # a malformed hop: stop believing the chain here
        if not trusted(address):
            return str(address)
    return str(direct)


def rate_bucket(address: str) -> str:
    """IPv4 per address; IPv6 per /64, the smallest block one subscriber normally holds."""

    parsed = _address(address)
    if parsed is None:
        return f"other:{address[:64]}"
    if isinstance(parsed, ipaddress.IPv4Address):
        return f"v4:{parsed}"
    return f"v6:{ipaddress.IPv6Network(f'{parsed}/64', strict=False)}"


class RateLimiter:
    """Durable per-bucket sliding windows; admission is one ``BEGIN IMMEDIATE`` transaction."""

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], float] = time.time,
        limits: Mapping[str, tuple[int, float]] = RATE_LIMITS,
    ) -> None:
        self.database = database
        self.clock = clock
        self.limits = dict(limits)

    def admit(self, action: str, bucket: str) -> float | None:
        """``None`` when the attempt is admitted (and counted), else seconds until it would be."""

        limit, window = self.limits[action]
        now = self.clock()
        with self.database.write() as connection:
            # Aged-out attempts are dropped for every bucket, so idle buckets do not pile up.
            connection.execute(
                "DELETE FROM rate_limit_hits WHERE action = ? AND at <= ?",
                (action, now - window),
            )
            rows = connection.execute(
                "SELECT at FROM rate_limit_hits WHERE action = ? AND bucket = ? ORDER BY at",
                (action, bucket),
            ).fetchall()
            if len(rows) >= limit:
                return max(1.0, float(rows[len(rows) - limit]["at"]) + window - now)
            connection.execute(
                "INSERT INTO rate_limit_hits(action, bucket, at) VALUES (?, ?, ?)",
                (action, bucket, now),
            )
        return None


# -------------------------------------------------------------------------------------- accounts
@dataclass(frozen=True)
class Account:
    id: str
    email: str
    role: str
    verified_at: float | None
    has_password: bool
    created_at: float
    disabled_at: float | None
    plan: str = "free"
    #: ``users.pw_version`` as read: the credential a sign-in verified, which a session is issued
    #: against (:meth:`AccountStore.start_session`).
    credential: int = 0

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass(frozen=True)
class IssuedLink:
    token: str
    purpose: str
    expires_at: float


@dataclass(frozen=True)
class IssuedSession:
    token: str
    expires_at: float


@dataclass(frozen=True)
class SignedIn:
    """One request's authenticated session.

    ``request_csrf`` is what this request's own form or header must carry (the session it
    presented); ``csrf`` is what pages rendered now must carry. They differ only when the request
    rotated the session, in which case ``new_token`` must be set as the cookie.
    """

    account: Account
    token: str
    expires_at: float
    request_csrf: str
    new_token: str | None = None

    @property
    def csrf(self) -> str:
        return session_csrf(self.new_token or self.token)


_ACCOUNT_COLUMNS = (
    "u.id, u.email, u.role, u.verified_at, u.pw_hash IS NOT NULL AS has_password, u.created_at, "
    "u.disabled_at, u.pw_version AS credential, COALESCE((SELECT plan FROM entitlements e "
    "WHERE e.user_id = u.id AND e.status = 'active'), 'free') AS plan"
)


def _account(row: Any) -> Account:
    return Account(
        id=str(row["id"]),
        email=str(row["email"]),
        role=str(row["role"]),
        verified_at=row["verified_at"],
        has_password=bool(row["has_password"]),
        created_at=float(row["created_at"]),
        disabled_at=row["disabled_at"],
        plan=str(row["plan"]),
        credential=int(row["credential"]),
    )


def _clip(comment: str) -> str:
    return comment.strip()[:COMMENT_MAX_LENGTH]


class AccountStore:
    """Every account, session and link operation, each in one SQLite transaction."""

    def __init__(self, database: Database, *, clock: Callable[[], float] = time.time) -> None:
        self.database = database
        self.clock = clock

    # ------------------------------------------------------------------------------ reading
    def get(self, user_id: str) -> Account | None:
        with self.database.read() as connection:
            row = connection.execute(
                f"SELECT {_ACCOUNT_COLUMNS} FROM users u WHERE u.id = ? AND u.deleted_at IS NULL",
                (user_id,),
            ).fetchone()
        return _account(row) if row is not None else None

    def find(self, email: str) -> Account | None:
        with self.database.read() as connection:
            row = connection.execute(
                f"SELECT {_ACCOUNT_COLUMNS} FROM users u "
                "WHERE u.email = ? AND u.deleted_at IS NULL",
                (normalise_email(email),),
            ).fetchone()
        return _account(row) if row is not None else None

    def accounts(self) -> list[Account]:
        with self.database.read() as connection:
            rows = connection.execute(
                f"SELECT {_ACCOUNT_COLUMNS} FROM users u WHERE u.deleted_at IS NULL "
                "ORDER BY u.created_at, u.email"
            ).fetchall()
        return [_account(row) for row in rows]

    def audit(self, limit: int = 25) -> list[dict[str, Any]]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT actor, action, target, detail, at FROM admin_audit "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        entries = []
        for row in rows:
            try:
                detail = json.loads(row["detail"])
            except ValueError:
                detail = {}
            entries.append(
                {
                    "actor": row["actor"],
                    "action": row["action"],
                    "target": row["target"],
                    "detail": detail if isinstance(detail, dict) else {},
                    "at": row["at"],
                }
            )
        return entries

    # ------------------------------------------------------------------------- admin actions
    def _audit(
        self, connection: Any, actor: str, action: str, target: str, detail: Mapping[str, Any]
    ) -> None:
        connection.execute(
            "INSERT INTO admin_audit(actor, action, target, detail, at) VALUES (?, ?, ?, ?, ?)",
            (actor, action, target, json.dumps(dict(detail), sort_keys=True), self.clock()),
        )

    def _issue(self, connection: Any, user_id: str, purpose: str, actor: str) -> IssuedLink:
        now = self.clock()
        lifetime = SETUP_LINK_SECONDS if purpose == "setup" else RESET_LINK_SECONDS
        token = new_token()
        # Only one link is ever outstanding: an older one stops working now.
        connection.execute(
            "UPDATE email_tokens SET revoked_at = ? "
            "WHERE user_id = ? AND used_at IS NULL AND revoked_at IS NULL",
            (now, user_id),
        )
        connection.execute(
            "INSERT INTO email_tokens(token_hash, user_id, purpose, issued_by, created_at, "
            "expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (token_hash(token), user_id, purpose, actor, now, now + lifetime),
        )
        return IssuedLink(token, purpose, now + lifetime)

    def create_account(
        self,
        email: str,
        *,
        actor: str,
        role: str = "user",
        created_ip: str | None = None,
        comment: str = "",
    ) -> tuple[Account, IssuedLink]:
        """An admin-created, pre-verified account with no password yet, and its setup link."""

        address = normalise_email(email)
        if not valid_email(address):
            raise AccountError("Enter a valid email address.")
        if role not in ROLES:
            raise AccountError("Choose a valid role.")
        now = self.clock()
        user_id = "u" + uuid.uuid4().hex
        with self.database.write() as connection:
            if connection.execute("SELECT 1 FROM users WHERE email = ?", (address,)).fetchone():
                raise AccountError("An account with that email address already exists.")
            connection.execute(
                "INSERT INTO users(id, email, pw_hash, role, verified_at, created_ip, created_by, "
                "created_at) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)",
                (user_id, address, role, now, created_ip, actor, now),
            )
            link = self._issue(connection, user_id, "setup", actor)
            self._audit(
                connection,
                actor,
                "account.create",
                user_id,
                {"email": address, "role": role, "comment": _clip(comment)},
            )
        account = self.get(user_id)
        assert account is not None
        return account, link

    def issue_reset(self, user_id: str, *, actor: str, comment: str = "") -> IssuedLink:
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT pw_hash IS NULL AS unset FROM users WHERE id = ? AND deleted_at IS NULL",
                (user_id,),
            ).fetchone()
            if row is None:
                raise AccountError("That account does not exist.")
            purpose = "setup" if row["unset"] else "reset"
            link = self._issue(connection, user_id, purpose, actor)
            self._audit(
                connection, actor, f"account.{purpose}_link", user_id, {"comment": _clip(comment)}
            )
        return link

    def set_plan(self, user_id: str, plan: str, *, actor: str, comment: str = "") -> bool:
        """Change the account's plan; every live session of the account rotates on its next use.

        Returns ``False`` when the plan was already ``plan`` (nothing changes, nothing rotates).
        """

        if plan not in PLANS:
            raise AccountError("Choose a valid plan.")
        now = self.clock()
        with self.database.write() as connection:
            if not connection.execute(
                "SELECT 1 FROM users WHERE id = ? AND deleted_at IS NULL", (user_id,)
            ).fetchone():
                raise AccountError("That account does not exist.")
            current = connection.execute(
                "SELECT plan FROM entitlements WHERE user_id = ? AND status = 'active'",
                (user_id,),
            ).fetchone()
            if (current["plan"] if current else "free") == plan:
                return False
            connection.execute(
                "UPDATE entitlements SET status = 'ended', ended_at = ? "
                "WHERE user_id = ? AND status = 'active'",
                (now, user_id),
            )
            connection.execute(
                "INSERT INTO entitlements(user_id, plan, status, source, set_by, created_at) "
                "VALUES (?, ?, 'active', 'admin', ?, ?)",
                (user_id, plan, actor, now),
            )
            connection.execute(
                "UPDATE sessions SET rotate_required = 1 WHERE user_id = ? AND revoked_at IS NULL",
                (user_id,),
            )
            self._audit(
                connection,
                actor,
                "account.plan",
                user_id,
                {
                    "plan": plan,
                    "from": current["plan"] if current else "free",
                    "comment": _clip(comment),
                },
            )
        return True

    # ----------------------------------------------------------------------------- sign-in
    def verify_login(self, email: str, password: str) -> Account | None:
        """The account these credentials open, or ``None`` — the same answer and about the same
        work whether the address is unknown, has no password yet, is disabled, or the password
        is wrong."""

        address = normalise_email(email)
        # The bound applies to the normalised password, the form that was hashed, and nothing is
        # truncated: a typed string that composes to an acceptable length IS the owner's password.
        normalised = normalise_password(password)
        row = None
        if valid_email(address) and len(normalised) <= PASSWORD_MAX_LENGTH:
            with self.database.read() as connection:
                row = connection.execute(
                    f"SELECT {_ACCOUNT_COLUMNS}, u.pw_hash FROM users u "
                    "WHERE u.email = ? AND u.deleted_at IS NULL",
                    (address,),
                ).fetchone()
        stored = row["pw_hash"] if row is not None else None
        ok = _verify(stored, normalised)
        if row is None or not ok or row["disabled_at"] is not None or row["verified_at"] is None:
            return None
        if PASSWORD_HASHER.check_needs_rehash(stored):
            with self.database.write() as connection:
                connection.execute(
                    "UPDATE users SET pw_hash = ? WHERE id = ? AND pw_hash = ?",
                    (hash_password(normalised), row["id"], stored),
                )
        return _account(row)

    def start_session(
        self,
        user_id: str,
        *,
        credential: int,
        ip: str | None = None,
        replacing: str | None = None,
    ) -> IssuedSession | None:
        """A brand-new session for the credential just verified, or ``None`` if it has changed.

        The insert is conditional on ``users.pw_version`` still being ``credential`` — the version
        :meth:`verify_login` (or :meth:`redeem_link`) saw — inside the one write transaction. A
        sign-in that verified a password a reset has since replaced therefore issues nothing: the
        reset's revocation of every session is final, and a stale verification loses rather than
        wins. Rotation on sign-in: the session the browser held is revoked in the same transaction
        and marked ``replaced_by`` the new one — so a stale answer for that cookie never clears
        the replacement (:meth:`superseded`), whether the old session was still live (sign-in)
        or already revoked by the reset that led here (set-password).
        """

        now = self.clock()
        with self.database.write() as connection:
            return self._issue_session(connection, user_id, credential, ip, replacing, now)

    @staticmethod
    def _issue_session(
        connection: Any,
        user_id: str,
        credential: int,
        ip: str | None,
        replacing: str | None,
        now: float,
    ) -> IssuedSession | None:
        """The conditional insert and the ``replaced_by`` stamp, inside the caller's transaction
        (a sign-in's own, or the one transaction a reset is)."""

        token = new_token()
        cursor = connection.execute(
            "INSERT INTO sessions(token_hash, user_id, created_at, last_seen_at, expires_at, "
            "created_ip) SELECT ?, id, ?, ?, ?, ? FROM users WHERE id = ? AND pw_version = ? "
            "AND pw_hash IS NOT NULL AND deleted_at IS NULL AND disabled_at IS NULL",
            (
                token_hash(token),
                now,
                now,
                now + SESSION_ABSOLUTE_SECONDS,
                ip,
                user_id,
                credential,
            ),
        )
        if cursor.rowcount != 1:
            return None
        if well_formed_token(replacing):
            assert replacing is not None
            connection.execute(
                "UPDATE sessions SET revoked_at = ?, revoked_reason = 'replaced' "
                "WHERE token_hash = ? AND revoked_at IS NULL",
                (now, token_hash(replacing)),
            )
            connection.execute(
                "UPDATE sessions SET replaced_by = ? WHERE token_hash = ? AND replaced_by IS NULL",
                (cursor.lastrowid, token_hash(replacing)),
            )
        return IssuedSession(token, now + SESSION_ABSOLUTE_SECONDS)

    def authenticate(self, token: object) -> SignedIn | None:
        """The session this cookie token names, if it is live; rotates it when a plan changed.

        Live means: not revoked, used within the last 30 days (``last_seen_at``), less than 90
        days old (``expires_at``), and its account neither disabled nor deleted. All three
        timestamps are durable rows, so a restart changes nothing.
        """

        if not well_formed_token(token):
            return None
        assert isinstance(token, str)
        hashed = token_hash(token)
        now = self.clock()
        query = (
            f"SELECT s.id AS session_id, s.last_seen_at, s.expires_at, s.rotate_required, "
            f"{_ACCOUNT_COLUMNS} FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.revoked_at IS NULL AND u.deleted_at IS NULL "
            "AND u.disabled_at IS NULL AND s.expires_at > ? AND s.last_seen_at > ?"
        )
        with self.database.read() as connection:
            row = connection.execute(query, (hashed, now, now - SESSION_IDLE_SECONDS)).fetchone()
        if row is None:
            return None
        expires_at = float(row["expires_at"])
        if not row["rotate_required"] and now - float(row["last_seen_at"]) < SESSION_TOUCH_SECONDS:
            return SignedIn(_account(row), token, expires_at, session_csrf(token))
        with self.database.write() as connection:
            row = connection.execute(query, (hashed, now, now - SESSION_IDLE_SECONDS)).fetchone()
            if row is None:
                return None
            if not row["rotate_required"]:
                connection.execute(
                    "UPDATE sessions SET last_seen_at = ? WHERE id = ? AND revoked_at IS NULL",
                    (now, row["session_id"]),
                )
                return SignedIn(_account(row), token, expires_at, session_csrf(token))
            # Rotation: the old token stops working in the same transaction the new one starts.
            replacement = new_token()
            revoked = connection.execute(
                "UPDATE sessions SET revoked_at = ?, revoked_reason = 'rotated' "
                "WHERE id = ? AND revoked_at IS NULL",
                (now, row["session_id"]),
            ).rowcount
            if revoked != 1:
                return None
            cursor = connection.execute(
                "INSERT INTO sessions(token_hash, user_id, created_at, last_seen_at, expires_at, "
                "created_ip) SELECT ?, user_id, ?, ?, expires_at, created_ip FROM sessions "
                "WHERE id = ?",
                (token_hash(replacement), now, now, row["session_id"]),
            )
            connection.execute(
                "UPDATE sessions SET replaced_by = ? WHERE id = ?",
                (cursor.lastrowid, row["session_id"]),
            )
        return SignedIn(_account(row), token, expires_at, session_csrf(token), replacement)

    def superseded(self, token: object) -> bool:
        """Whether the browser holding this cookie was handed its replacement — by a rotation
        (:meth:`authenticate`), a sign-in or a set-password link (:meth:`start_session`). A
        session ended any other way (logout, expiry, a reset made from another browser, an
        admin's revocation) has no ``replaced_by``."""

        if not well_formed_token(token):
            return False
        assert isinstance(token, str)
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT 1 FROM sessions WHERE token_hash = ? AND replaced_by IS NOT NULL",
                (token_hash(token),),
            ).fetchone()
        return row is not None

    def revoke_session(self, token: object, reason: str = "logout") -> bool:
        if not well_formed_token(token):
            return False
        assert isinstance(token, str)
        with self.database.write() as connection:
            changed = connection.execute(
                "UPDATE sessions SET revoked_at = ?, revoked_reason = ? "
                "WHERE token_hash = ? AND revoked_at IS NULL",
                (self.clock(), reason, token_hash(token)),
            ).rowcount
        return changed == 1

    def redeem_link(self, token: object, password: str) -> Account | None:
        """Use a set-password or reset link once: set the password, end every session and link.

        The argon2 work happens before the transaction; inside it, the link is claimed with a
        compare-and-set, so of two concurrent uses exactly one sets a password. Setting the
        password bumps ``users.pw_version``, and the account returned carries THAT version (read
        inside the same transaction), so the session started for the owner afterwards is issued
        against this reset and no other. Every other session of the account is revoked here, and
        every outstanding link too.
        """

        if not well_formed_token(token):
            return None
        assert isinstance(token, str)
        new_hash = hash_password(password)
        with self.database.write() as connection:
            return self._redeem(connection, token, new_hash, self.clock())

    def redeem_and_start_session(
        self,
        token: object,
        password: str,
        *,
        ip: str | None = None,
        replacing: str | None = None,
    ) -> tuple[Account, IssuedSession] | None:
        """:meth:`redeem_link` and the owner's new session as ONE transaction.

        Claiming the link, setting the password, revoking every session and link of the
        account, inserting the replacement session and stamping ``replaced_by`` on the cookie
        the browser sent all commit together: there is no moment at which the old session is
        revoked but not yet superseded, so a request racing in with the old cookie can never
        find a state in which :meth:`superseded` is false and clear the cookie this reset issues.
        """

        if not well_formed_token(token):
            return None
        assert isinstance(token, str)
        new_hash = hash_password(password)
        now = self.clock()
        with self.database.write() as connection:
            account = self._redeem(connection, token, new_hash, now)
            if account is None:
                return None
            issued = self._issue_session(
                connection, account.id, account.credential, ip, replacing, now
            )
        if issued is None:
            return None
        return account, issued

    @staticmethod
    def _redeem(connection: Any, token: str, new_hash: str, now: float) -> Account | None:
        row = connection.execute(
            "SELECT t.id, t.user_id FROM email_tokens t JOIN users u ON u.id = t.user_id "
            "WHERE t.token_hash = ? AND t.used_at IS NULL AND t.revoked_at IS NULL "
            "AND t.expires_at > ? AND u.deleted_at IS NULL AND u.disabled_at IS NULL",
            (token_hash(token), now),
        ).fetchone()
        if row is None:
            return None
        claimed = connection.execute(
            "UPDATE email_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL "
            "AND revoked_at IS NULL",
            (now, row["id"]),
        ).rowcount
        if claimed != 1:
            return None
        user_id = row["user_id"]
        connection.execute(
            "UPDATE users SET pw_hash = ?, pw_version = pw_version + 1, "
            "password_changed_at = ?, verified_at = COALESCE(verified_at, ?) WHERE id = ?",
            (new_hash, now, now, user_id),
        )
        connection.execute(
            "UPDATE sessions SET revoked_at = ?, revoked_reason = 'password' "
            "WHERE user_id = ? AND revoked_at IS NULL",
            (now, user_id),
        )
        connection.execute(
            "UPDATE email_tokens SET revoked_at = ? "
            "WHERE user_id = ? AND used_at IS NULL AND revoked_at IS NULL",
            (now, user_id),
        )
        account = connection.execute(
            f"SELECT {_ACCOUNT_COLUMNS} FROM users u WHERE u.id = ?", (user_id,)
        ).fetchone()
        return _account(account)

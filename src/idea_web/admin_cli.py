"""Server-side account commands for a hosted operator (cycle 4c-ii).

The first admin cannot be created from the web (there is no sign-up route), so the operator runs::

    python -m idea_web.admin_cli create --database /data/app.db --origin https://idea.example \\
        --email owner@example.com --admin

It prints the new account's single-use set-password link once, to the terminal that ran it; the
link is not stored (only its hash is) and is not logged. ``reset`` prints a new link for an
existing account and revokes any older one. Both actions are written to ``admin_audit``.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import UTC, datetime
from pathlib import Path

from idea_web.auth import AccountError, AccountStore, HostedSettings
from idea_web.database import MigrationRefused, hosted_database


def _actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:  # noqa: BLE001 - no user name is not a reason to refuse
        return "cli"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m idea_web.admin_cli")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("create", "create a pre-verified account and print its set-password link"),
        ("reset", "print a new single-use password link for an existing account"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("--database", required=True, type=Path)
        command.add_argument("--origin", required=True, help="the public https:// origin")
        command.add_argument("--email", required=True)
        command.add_argument("--comment", default="")
        if name == "create":
            command.add_argument("--admin", action="store_true")
    args = parser.parse_args(argv)

    try:
        # Migrated under the worker supervisor lock: refused, not applied, while a worker is live.
        database = hosted_database(args.database)
    except MigrationRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    settings = HostedSettings(database=database, public_origin=args.origin)
    store = AccountStore(database)
    try:
        if args.command == "create":
            account, link = store.create_account(
                args.email,
                actor=_actor(),
                role="admin" if args.admin else "user",
                comment=args.comment,
            )
        else:
            found = store.find(args.email)
            if found is None:
                raise AccountError("That account does not exist.")
            account = found
            link = store.issue_reset(account.id, actor=_actor(), comment=args.comment)
    except AccountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    expires = datetime.fromtimestamp(link.expires_at, UTC).strftime("%Y-%m-%d %H:%M UTC")
    print(f"account: {account.email} ({account.role})")
    print(f"single-use link (expires {expires}; shown once, not stored):")
    print(f"{settings.public_origin}/reset/{link.token}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

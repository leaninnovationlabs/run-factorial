"""Operator commands that run beside the database, never on the web (tech-stack decisions 80
and 91).

    uv run python -m orchestrator.admin grant-owner <email>
    uv run python -m orchestrator.admin verify-email <email>

`grant-owner` gives `default` an owner. An empty install needs none of this: its first account
becomes the owner. An install with data and no owner — one that signed people in with Google
before accounts existed — runs it once: inside the running container (`docker compose exec`,
or ECS Exec on AWS), or anywhere else where `FACTORIAL_DATABASE_URL` reaches Postgres.

An email that has an account becomes the owner at once. One that has none gets an owner's invite,
printed as a link: the person signs up from it, with a password or SSO, and accepting it makes
them the owner. The link works once, for that email, for seven days, and is not shown again.

`verify-email` marks an account's email verified, for a person whose verification email does
not arrive while mail is configured (design.md §7.19 item 17). The operator vouches for the
mailbox: run it only for a person whose address you know is theirs.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from orchestrator.access.accounts import vouch_for_email
from orchestrator.api.services import membership
from orchestrator.api.services.errors import ServiceError
from orchestrator.db.session import dispose_engine, session_scope
from orchestrator.settings import APP_PREFIX, get_settings

USAGE = "usage: python -m orchestrator.admin grant-owner|verify-email <email>"


async def grant_owner(email: str) -> str | None:
    try:
        async with session_scope() as session:
            return await membership.grant_owner(session, email, now=datetime.now(UTC))
    finally:
        await dispose_engine()


async def verify_email(email: str) -> bool:
    try:
        async with session_scope() as session:
            return await vouch_for_email(session, email, now=datetime.now(UTC))
    finally:
        await dispose_engine()


def invite_link(token: str, dashboard_url: str) -> str:
    """The invite as the dashboard opens it: on the deployment's own URL when it names one."""
    return f"{dashboard_url}/invite/{token}" if dashboard_url else f"{APP_PREFIX}/invite/{token}"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("grant-owner", "verify-email"):
        print(USAGE, file=sys.stderr)
        return 2
    email = argv[1]
    if argv[0] == "verify-email":
        if not asyncio.run(verify_email(email)):
            print(f"verify-email: no account has the email {email}", file=sys.stderr)
            return 1
        print(f"{email} is verified")
        return 0
    try:
        token = asyncio.run(grant_owner(email))
    except ServiceError as exc:
        print(f"grant-owner: {exc}", file=sys.stderr)
        return 1
    if token is None:
        print(f"{email} is an owner of default")
    else:
        print(
            f"{email} has no account yet. Send them this link: they sign up from it and become "
            f"an owner of default when they accept it. It works once and lasts seven days.\n"
            f"{invite_link(token, get_settings().dashboard_url)}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    sys.exit(main(sys.argv[1:]))

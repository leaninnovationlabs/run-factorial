"""Accounts: create one, sign in with a password or by SSO, set a password (design.md §7.19).

An account is its lowercase email. A password proves nothing about the mailbox, so only an SSO
sign-in sets `email_verified_at`, and that sign-in takes back an account whose email was
unverified: the person who set its password may not own the address, so the password and every
session minted before go with it (`session_ended`). A session also ends when the account's
`session_epoch` moves past the one it was minted under: signing out everywhere and setting a
password move it, and revoke the MCP tokens the account's sessions minted (`end_sessions`).

Each function changes rows and flushes; the caller commits.

Every hash and every verify runs on a worker thread, a few at a time: argon2 costs tens of
milliseconds of CPU and 64 MiB each, and the API's one event loop serves every tenant. A caller
past the queue gets `HasherBusy` at once rather than a place in an unbounded line.

Failed tries are counted per account AND client (`api.ratelimit.client_key`: an IPv6 client's
/64), in process memory (`_Lockouts`).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import AppUser, McpMintedVia, McpToken
from orchestrator.lru import BoundedLRU

__all__ = [
    "LOCKOUT",
    "MAX_FAILED_SIGN_INS",
    "PASSWORD_MAX",
    "PASSWORD_MIN",
    "AccountExists",
    "AccountLocked",
    "HasherBusy",
    "InvalidAccountInput",
    "SignInRefused",
    "create_account",
    "end_sessions",
    "normalize_email",
    "password_sign_in",
    "session_ended",
    "set_password",
    "sso_sign_in",
    "user_by_email",
    "verified_since",
]

PASSWORD_MIN: Final = 12
#: argon2 hashes the whole input, so a cap keeps one request from costing a long hash.
PASSWORD_MAX: Final = 256
NAME_MAX: Final = 100
EMAIL_MAX: Final = 320
MAX_FAILED_SIGN_INS: Final = 10
LOCKOUT: Final = timedelta(minutes=15)

#: One `@`, something on each side, a dot in the domain, no whitespace. Deliverability is the
#: email verification's job, later; this only refuses what cannot be an address.
_EMAIL: Final = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

_hasher: Final = PasswordHasher()

#: Hashes at once: one vCPU, and 64 MiB each.
HASH_WORKERS: Final = 2
#: Hashes that may wait for a worker. Past it a request is refused, never queued.
HASH_QUEUE: Final = 8


class InvalidAccountInput(ValueError):
    pass


class AccountExists(Exception):
    pass


class SignInRefused(Exception):
    """Wrong email or wrong password: one answer for both."""


class HasherBusy(Exception):
    """Every hashing worker is busy and the queue is full."""


class AccountLocked(Exception):
    def __init__(self, until: datetime) -> None:
        super().__init__(f"locked until {until.isoformat()}")
        self.until = until


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """Verified against when there is no hash to check, so an unknown email costs the same time
    as a wrong password and the timing does not tell them apart."""
    return _hasher.hash("an account that does not exist")


def normalize_email(raw: str) -> str:
    email = raw.strip().lower()
    if len(email) > EMAIL_MAX or not _EMAIL.fullmatch(email):
        raise InvalidAccountInput("This is not an email address.")
    return email


def _name(raw: str) -> str:
    name = raw.strip()
    if not 0 < len(name) <= NAME_MAX:
        raise InvalidAccountInput(f"A name is 1 to {NAME_MAX} characters.")
    return name


def _password(raw: str) -> str:
    if not PASSWORD_MIN <= len(raw) <= PASSWORD_MAX:
        raise InvalidAccountInput(f"A password is {PASSWORD_MIN} to {PASSWORD_MAX} characters.")
    return raw


class _HashGate:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._workers = asyncio.Semaphore(HASH_WORKERS)
        self._admitted = 0

    async def run[T](self, work: Callable[[], T]) -> T:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # A semaphore belongs to one loop; a test suite runs several.
            self._loop, self._workers, self._admitted = loop, asyncio.Semaphore(HASH_WORKERS), 0
        if self._admitted >= HASH_WORKERS + HASH_QUEUE:
            raise HasherBusy
        self._admitted += 1
        try:
            async with self._workers:
                return await asyncio.to_thread(work)
        finally:
            self._admitted -= 1


_gate: Final = _HashGate()


async def _hash(password: str) -> str:
    return await _gate.run(lambda: _hasher.hash(password))


def _verify(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _dummy_hash(), password) and bool(password_hash)
    except (VerificationError, InvalidHashError):
        return False


async def _matches(password_hash: str | None, password: str) -> bool:
    return await _gate.run(lambda: _verify(password_hash, password))


class _Lockouts:
    """Failed password tries per `(account, client address)`.

    Keyed on the address too, so a stranger's wrong guesses lock out only the stranger: keyed on
    the account alone, ten requests from anyone locked its owner out for `LOCKOUT`. In process
    memory, because the API is one process; a restart forgets the count, which the per-address
    request limit already bounds.
    """

    def __init__(self) -> None:
        #: key → (failures since the last lock, locked until).
        self._tries: BoundedLRU[tuple[str, str], tuple[int, datetime | None]] = BoundedLRU()

    def locked_until(self, key: tuple[str, str], now: datetime) -> datetime | None:
        until = self._tries.get(key, (0, None))[1]
        return until if until is not None and until > now else None

    def fail(self, key: tuple[str, str], now: datetime) -> None:
        failures = self._tries.get(key, (0, None))[0] + 1
        until = None
        if failures >= MAX_FAILED_SIGN_INS:
            failures, until = 0, now + LOCKOUT
        self._tries.put(key, (failures, until))

    def clear(self, key: tuple[str, str]) -> None:
        self._tries.pop(key, (0, None))


_lockouts: Final = _Lockouts()


async def user_by_email(session: AsyncSession, email: str) -> AppUser | None:
    return await session.scalar(select(AppUser).where(AppUser.email == email.lower()))


async def create_account(
    session: AsyncSession, *, email: str, name: str, password: str, now: datetime
) -> AppUser:
    values = {
        "email": normalize_email(email),
        "name": _name(name),
        "password_hash": await _hash(_password(password)),
        "last_sign_in_at": now,
    }
    user_id = await session.scalar(
        insert(AppUser).values(values).on_conflict_do_nothing().returning(AppUser.id)
    )
    if user_id is None:
        raise AccountExists(values["email"])
    user = await session.get(AppUser, user_id)
    assert user is not None
    return user


async def password_sign_in(
    session: AsyncSession, *, email: str, password: str, now: datetime, client: str
) -> AppUser:
    """`client` is the request's `client_key`: the lockout is per account and client."""
    try:
        address = normalize_email(email)
    except InvalidAccountInput:
        await _matches(None, password)
        raise SignInRefused from None
    # No row lock: it would be held across the hash gate, so guesses at one email would pin
    # pooled connections on it and queue its owner. The only write is a rehash, and losing one
    # to a concurrent sign-in costs nothing.
    user = await session.scalar(select(AppUser).where(AppUser.email == address))
    if user is None:
        await _matches(None, password)
        raise SignInRefused
    await _check_password(user, password, now=now, client=client)
    assert user.password_hash is not None
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = await _hash(password)
    user.last_sign_in_at = now
    await session.flush()
    return user


async def sso_sign_in(session: AsyncSession, *, email: str, now: datetime) -> AppUser:
    """The identity provider verified `email`. The first sign-in creates the account; a later one
    verifies an unverified account and takes its password away."""
    address = normalize_email(email)
    await session.execute(
        insert(AppUser)
        .values(
            email=address,
            name=address.split("@", 1)[0],
            email_verified_at=now,
            last_sign_in_at=now,
        )
        .on_conflict_do_nothing()
    )
    user = await session.scalar(
        select(AppUser)
        .where(AppUser.email == address)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert user is not None
    if user.email_verified_at is None:
        user.email_verified_at = now
        user.password_hash = None
    user.last_sign_in_at = now
    await session.flush()
    return user


async def session_ended(session: AsyncSession, *, email: str, verified: bool, epoch: int) -> bool:
    """Whether a signed session no longer stands: its account is gone, its epoch is not the
    account's, or it was minted while the account was unverified and an SSO sign-in has since
    verified it. A cookie names only the email, so without the last the stranger who registered
    the address would keep acting as its owner."""
    row = (
        await session.execute(
            select(AppUser.session_epoch, AppUser.email_verified_at).where(
                AppUser.email == email.lower()
            )
        )
    ).first()
    if row is None:
        return True
    current, verified_at = row
    return current != epoch or (not verified and verified_at is not None)


async def end_sessions(session: AsyncSession, user: AppUser) -> None:
    """End every session the account holds, and revoke each MCP token one of them minted: a
    year-long token from a stolen session would otherwise outlive the sign-out meant to end it.
    The caller mints the current browser a new session."""
    await session.execute(
        update(McpToken)
        .where(
            func.lower(McpToken.created_by) == user.email,
            McpToken.minted_via == McpMintedVia.SESSION,
            McpToken.revoked_at.is_(None),
        )
        .values(revoked_at=func.now(), revoked_by=user.email)
        .execution_options(synchronize_session=False)
    )
    epoch = await session.scalar(
        update(AppUser)
        .where(AppUser.id == user.id)
        .values(session_epoch=AppUser.session_epoch + 1)
        .returning(AppUser.session_epoch)
        .execution_options(synchronize_session=False)
    )
    assert epoch is not None
    user.session_epoch = epoch
    await session.flush()


async def verified_since(session: AsyncSession, *, email: str, moment: datetime) -> bool:
    """Whether an SSO sign-in verified the account after `moment`: a credential minted before
    then came from a session `session_ended` refuses, so it ends with it."""
    return bool(
        await session.scalar(
            select(
                exists().where(AppUser.email == email.lower(), AppUser.email_verified_at > moment)
            )
        )
    )


async def _check_password(user: AppUser, password: str, *, now: datetime, client: str) -> None:
    """The lockout and the failure count, shared by every place a password is tried, so a
    stolen session cannot guess the current password where sign-in would lock it out."""
    key = (user.email, client)
    if (until := _lockouts.locked_until(key, now)) is not None:
        raise AccountLocked(until)
    if not await _matches(user.password_hash, password):
        _lockouts.fail(key, now)
        raise SignInRefused
    _lockouts.clear(key)


async def set_password(
    session: AsyncSession,
    user: AppUser,
    *,
    current: str | None,
    password: str,
    now: datetime,
    client: str,
) -> None:
    """An account that has a password must name it; one that has none (SSO only, or one whose
    password an SSO sign-in removed) sets one on the strength of a fresh sign-in, which the route
    checks. Every other session of the account ends."""
    if user.password_hash is not None:
        await _check_password(user, current or "", now=now, client=client)
    user.password_hash = await _hash(_password(password))
    await end_sessions(session, user)

"""The links that prove a mailbox: `verify` after a password signup, `reset` for a forgotten
password (design.md §7.19 items 16 and 17).

A token is 32 random bytes; its row keeps the SHA-256. It works once, until it expires, and only
while the address it went to is still its account's. Issuing one replaces the account's live
token of the same purpose, and is refused within `RESEND_GAP` of the last or past `DAILY_SENDS`
in a day: open signup lets anyone make the deployment write to any address.

A `verify` token is redeemed by a session of its own account, which the route passes as
`email`: the owner of a mailbox who opens a link they never asked for must not verify the
account a stranger set the password of. A `reset` token needs no session, sets the password
and ends every session and session-minted MCP token (`accounts.reset_password`).

Each function changes rows and flushes; the caller commits, and sends only after it has.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.access.accounts import check_password_rules, hash_password, reset_password
from orchestrator.db.models import AppUser, EmailVerification, VerificationPurpose
from orchestrator.mail import Message
from orchestrator.mail.layout import Email, render

__all__ = [
    "DAILY_SENDS",
    "RESEND_GAP",
    "TTL",
    "InvalidToken",
    "TooSoon",
    "has_live",
    "issue",
    "redeem_reset",
    "redeem_verify",
    "reset_message",
    "verify_message",
]

TTL: Final = {
    VerificationPurpose.VERIFY: timedelta(days=1),
    VerificationPurpose.RESET: timedelta(hours=1),
}
RESEND_GAP: Final = timedelta(minutes=1)
DAILY_SENDS: Final = 5
_DAY: Final = timedelta(days=1)


class InvalidToken(Exception):
    """Unknown, used, replaced, expired, of another purpose, or for another account: one answer
    for all, so a guess learns nothing."""


class TooSoon(Exception):
    def __init__(self, retry_after: timedelta) -> None:
        super().__init__(f"retry after {retry_after}")
        self.retry_after = retry_after


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def has_live(
    session: AsyncSession, user: AppUser, purpose: VerificationPurpose, *, now: datetime
) -> bool:
    found = await session.scalar(
        select(EmailVerification.id).where(
            EmailVerification.user_id == user.id,
            EmailVerification.purpose == purpose,
            EmailVerification.email == user.email,
            EmailVerification.consumed_at.is_(None),
            EmailVerification.expires_at > now,
        )
    )
    return found is not None


async def issue(
    session: AsyncSession, user: AppUser, purpose: VerificationPurpose, *, now: datetime
) -> str:
    """A new token for `user`, replacing its live one of `purpose`. The account's row is locked
    so two requests at once cannot both pass the limits."""
    await session.execute(select(AppUser.id).where(AppUser.id == user.id).with_for_update())
    await session.execute(
        delete(EmailVerification).where(
            EmailVerification.user_id == user.id, EmailVerification.created_at <= now - _DAY
        )
    )
    recent = (
        await session.execute(
            select(func.count(), func.max(EmailVerification.created_at)).where(
                EmailVerification.user_id == user.id, EmailVerification.purpose == purpose
            )
        )
    ).one()
    count, last = recent
    if last is not None and now - last < RESEND_GAP:
        raise TooSoon(RESEND_GAP - (now - last))
    if count >= DAILY_SENDS:
        oldest = await session.scalar(
            select(func.min(EmailVerification.created_at)).where(
                EmailVerification.user_id == user.id, EmailVerification.purpose == purpose
            )
        )
        assert oldest is not None
        raise TooSoon(oldest + _DAY - now)
    await session.execute(
        update(EmailVerification)
        .where(
            EmailVerification.user_id == user.id,
            EmailVerification.purpose == purpose,
            EmailVerification.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    token = secrets.token_urlsafe(32)
    session.add(
        EmailVerification(
            user_id=user.id,
            email=user.email,
            purpose=purpose,
            token_hash=_digest(token),
            created_at=now,
            expires_at=now + TTL[purpose],
        )
    )
    await session.flush()
    return token


async def _consume(
    session: AsyncSession, token: str, purpose: VerificationPurpose, *, now: datetime
) -> AppUser:
    """Spend the token, once: the UPDATE matches only a live row, so of two redemptions at once
    one finds nothing. The account row is locked first, as `issue` locks it, so a redemption
    and a resend at once wait for each other instead of deadlocking."""
    owner = await session.scalar(
        select(EmailVerification.user_id).where(EmailVerification.token_hash == _digest(token))
    )
    if owner is None:
        raise InvalidToken
    user = await session.get(AppUser, owner, populate_existing=True, with_for_update=True)
    if user is None:
        raise InvalidToken
    user_id = await session.scalar(
        update(EmailVerification)
        .where(
            EmailVerification.token_hash == _digest(token),
            EmailVerification.purpose == purpose,
            EmailVerification.consumed_at.is_(None),
            EmailVerification.expires_at > now,
            EmailVerification.email == AppUser.email,
            EmailVerification.user_id == AppUser.id,
        )
        .values(consumed_at=now)
        .returning(EmailVerification.user_id)
    )
    if user_id is None:
        raise InvalidToken
    return user


async def _live_owner(
    session: AsyncSession, token: str, purpose: VerificationPurpose, *, now: datetime
) -> str:
    email = await session.scalar(
        select(AppUser.email)
        .join(EmailVerification, EmailVerification.user_id == AppUser.id)
        .where(
            EmailVerification.token_hash == _digest(token),
            EmailVerification.purpose == purpose,
            EmailVerification.consumed_at.is_(None),
            EmailVerification.expires_at > now,
            EmailVerification.email == AppUser.email,
        )
    )
    if email is None:
        raise InvalidToken
    return email


async def redeem_verify(session: AsyncSession, token: str, *, email: str, now: datetime) -> AppUser:
    """Verify the account signed in as `email`, with a token sent to it."""
    if await _live_owner(session, token, VerificationPurpose.VERIFY, now=now) != email.lower():
        raise InvalidToken
    user = await _consume(session, token, VerificationPurpose.VERIFY, now=now)
    if user.email_verified_at is None:
        user.email_verified_at = now
    await session.flush()
    return user


async def redeem_reset(
    session: AsyncSession, token: str, *, password: str, now: datetime
) -> AppUser:
    """Set a new password for the token's account. The token is checked before the password is
    hashed, and spent after, so a wrong token costs no hash and a hash holds no lock."""
    check_password_rules(password)
    await _live_owner(session, token, VerificationPurpose.RESET, now=now)
    password_hash = await hash_password(password)
    user = await _consume(session, token, VerificationPurpose.RESET, now=now)
    await reset_password(session, user, password_hash=password_hash, now=now)
    return user


def verify_message(to: str, link: str, *, dashboard: str) -> Message:
    note = (
        "If you did not create a Factorial account, ignore this email. Nobody can use this "
        "address in Factorial until it is verified."
    )
    return Message(
        to=to,
        subject="Verify your email for Factorial",
        text=(
            "Open this link to verify your email. It works for a day, and only while you are "
            f"signed in to the account you created:\n\n{link}\n\n{note}"
        ),
        html=render(
            Email(
                preview="Open the link to verify your email. It works for a day.",
                heading="Verify your email",
                lead="One step before you reach Factorial.",
                paragraphs=(
                    "Open the link in the browser where you created your account. It works for a "
                    "day, and only while you are signed in to that account.",
                ),
                button="Verify email",
                url=link,
                note=note,
                mark_url=_mark(dashboard),
                home_url=dashboard,
            )
        ),
    )


def reset_message(to: str, link: str, *, dashboard: str) -> Message:
    note = "If you did not ask for this, ignore this email: your password stays as it is."
    return Message(
        to=to,
        subject="Reset your Factorial password",
        text=(
            "Open this link to set a new password. It works once, for an hour:\n\n"
            f"{link}\n\n"
            f"Setting a new password signs you out everywhere else. {note}"
        ),
        html=render(
            Email(
                preview="Open the link to set a new password. It works once, for an hour.",
                heading="Reset your password",
                lead="A new password signs you out everywhere else.",
                paragraphs=("Open the link to set a new password. It works once, for an hour.",),
                button="Set a new password",
                url=link,
                note=note,
                mark_url=_mark(dashboard),
                home_url=dashboard,
            )
        ),
    )


def _mark(dashboard: str) -> str:
    """Served from `ui/public`, so each deployment's email loads the mark from itself."""
    return f"{dashboard}/email-mark.png" if dashboard else ""

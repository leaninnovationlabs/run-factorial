"""Organizations a person creates, their members and their invites (design.md §7.19), and who
an account joins on its own: the first owner of an empty install, and an SSO sign-in in the
allowed domains of a one-organization install.

The rules no role grants live here, whatever permission the route checked:
- nobody grants a role that holds a permission they do not hold;
- a member who cannot assign roles removes only members whose role holds strictly less;
- the last owner cannot leave, be removed or be demoted;
- whoever was removed or left is never joined again by the allowed-domain SSO sign-in, only by
  an invite they accept (`organization_removal`).
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import delete, exists, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.access import MEMBER, OWNER, Permission
from orchestrator.access.accounts import InvalidAccountInput, normalize_email, user_by_email
from orchestrator.access.decision import OPERATOR_PRINCIPAL, Caller
from orchestrator.access.permissions import granted
from orchestrator.api.services.errors import Conflict, Forbidden, Invalid, NotFound
from orchestrator.api.services.organizations import DEFAULT_ORGANIZATION
from orchestrator.db.models import (
    AppUser,
    McpToken,
    Organization,
    OrganizationCredential,
    OrganizationInvite,
    OrganizationMember,
    OrganizationRemoval,
    Project,
    RemovalReason,
    Role,
    RolePermission,
    Runtime,
    Secret,
)

__all__ = [
    "INVITE_LIFETIME",
    "RESERVED_SLUGS",
    "AcceptedInvite",
    "InvitePreview",
    "MemberRow",
    "NewInvite",
    "accept_invite",
    "built_in_roles",
    "change_role",
    "claim_empty_install",
    "create_invite",
    "create_organization",
    "default_has_owner",
    "grant_owner",
    "has_pending_invite",
    "install_is_empty",
    "invite_admits",
    "join_default",
    "list_invites",
    "list_members",
    "member_of_default",
    "organizations_of",
    "preview_invite",
    "remove_member",
    "revoke_invite",
]

INVITE_LIFETIME: Final = timedelta(days=7)
_SLUG: Final = re.compile(r"[a-z0-9][a-z0-9-]{1,38}[a-z0-9]")
#: Words the dashboard uses as paths, and the seeded organization's.
RESERVED_SLUGS: Final = frozenset(
    {"api", "auth", "default", "dev", "invite", "join", "onboarding", "orgs", "sign-in", "signup"}
)


def _by(caller: Caller) -> str:
    return caller.email or OPERATOR_PRINCIPAL


async def _person(session: AsyncSession, caller: Caller) -> AppUser:
    user = None if caller.email is None else await user_by_email(session, caller.email)
    if user is None:
        raise Forbidden("This needs a signed-in account, not the API token.")
    return user


async def built_in_roles(session: AsyncSession) -> dict[str, tuple[Role, frozenset[Permission]]]:
    rows = (
        await session.execute(
            select(Role, RolePermission.permission_key)
            .outerjoin(RolePermission, RolePermission.role_id == Role.id)
            .where(Role.built_in)
            .order_by(Role.id)
        )
    ).all()
    roles: dict[str, tuple[Role, list[str]]] = {}
    for role, key in rows:
        keys = roles.setdefault(role.key, (role, []))[1]
        if key is not None:
            keys.append(key)
    return {key: (role, granted(keys)) for key, (role, keys) in roles.items()}


async def _role(session: AsyncSession, key: str) -> tuple[Role, frozenset[Permission]]:
    roles = await built_in_roles(session)
    if key not in roles:
        raise Invalid(f"Role {key!r} not found.", candidates=sorted(roles))
    return roles[key]


def _may_grant(held: frozenset[Permission], granted: frozenset[Permission]) -> None:
    if not granted <= held:
        raise Forbidden("You can't grant a role that holds a permission you don't hold.")


async def organizations_of(
    session: AsyncSession, caller: Caller
) -> list[tuple[Organization, str | None]]:
    """Each organization the caller reaches, with their role in it: every organization and no role
    for the operator."""
    if caller.email is None:
        rows = await session.scalars(select(Organization).order_by(Organization.id))
        return [(organization, None) for organization in rows]
    found = await session.execute(
        select(Organization, Role.key)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .join(Role, Role.id == OrganizationMember.role_id)
        .join(AppUser, AppUser.id == OrganizationMember.user_id)
        .where(AppUser.email == caller.email)
        .order_by(Organization.id)
    )
    return [(organization, role) for organization, role in found.all()]


#: Rows only an operator makes. Any one means someone has set the install up, so its first
#: signup is not its owner: a stranger who arrives first must not take the runtimes and
#: credentials already stored, or the owner's invite `grant-owner` printed. A revision seeds
#: settings rows, so those cannot say it.
_SET_UP: Final = (
    OrganizationMember,
    OrganizationInvite,
    Project,
    Runtime,
    Secret,
    OrganizationCredential,
    McpToken,
)


async def default_has_owner(session: AsyncSession) -> bool:
    return bool(
        await session.scalar(
            select(
                exists().where(
                    Organization.slug == DEFAULT_ORGANIZATION,
                    OrganizationMember.organization_id == Organization.id,
                    OrganizationMember.role_id == Role.id,
                    Role.key == OWNER,
                    Role.built_in,
                )
            )
        )
    )


async def install_is_empty(session: AsyncSession, *, setup_token: bool) -> bool:
    """Whether an account may still become the first owner of `default`. Without a setup token
    (`FACTORIAL_AUTH__BOOTSTRAP_TOKEN`), while nothing an operator makes exists. With one, while
    `default` has no owner: only its holder can claim, so nothing a stranger made first, such as
    an organization of their own under multi-org, may shut the holder out. One query: the
    unauthenticated `/auth/providers` asks it."""
    if setup_token:
        return not await default_has_owner(session)
    return not await session.scalar(
        select(or_(*(exists().select_from(model) for model in _SET_UP)))
    )


async def _default_id(session: AsyncSession, *, lock: bool = False) -> int:
    query = select(Organization.id).where(Organization.slug == DEFAULT_ORGANIZATION)
    found = await session.scalar(query.with_for_update() if lock else query)
    if found is None:
        raise NotFound(f"Organization {DEFAULT_ORGANIZATION!r} not found.")
    return found


async def claim_empty_install(session: AsyncSession, user: AppUser, *, setup_token: bool) -> bool:
    """Make `user` the owner of `default` when the install is empty (`install_is_empty`). The
    `default` row is locked before the check, so of two accounts created at once only the first
    becomes owner."""
    organization_id = await _default_id(session, lock=True)
    if not await install_is_empty(session, setup_token=setup_token):
        return False
    owner, _ = await _role(session, OWNER)
    session.add(
        OrganizationMember(
            organization_id=organization_id, user_id=user.id, role_id=owner.id, added_by=user.email
        )
    )
    await session.flush()
    return True


async def join_default(session: AsyncSession, user: AppUser) -> None:
    """An SSO sign-in in the allowed domains of a one-organization install joins `default` as a
    member. A member already there keeps their role, and whoever was removed or left stays out
    until they accept an invite."""
    organization_id = await _default_id(session)
    member, _ = await _role(session, MEMBER)
    # One statement, so the removal it checks for is read in the same snapshot as the insert.
    await session.execute(
        insert(OrganizationMember)
        .from_select(
            ["organization_id", "user_id", "role_id", "added_by"],
            select(
                literal(organization_id), literal(user.id), literal(member.id), literal(user.email)
            ).where(
                ~exists().where(
                    OrganizationRemoval.organization_id == organization_id,
                    OrganizationRemoval.email == user.email,
                )
            ),
        )
        .on_conflict_do_nothing()
    )


async def _readmitted(session: AsyncSession, organization_id: int, email: str) -> None:
    await session.execute(
        delete(OrganizationRemoval).where(
            OrganizationRemoval.organization_id == organization_id,
            OrganizationRemoval.email == email,
        )
    )


async def member_of_default(session: AsyncSession, email: str) -> bool:
    found = await session.scalar(
        select(OrganizationMember.user_id)
        .join(AppUser, AppUser.id == OrganizationMember.user_id)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .where(Organization.slug == DEFAULT_ORGANIZATION, AppUser.email == email.lower())
    )
    return found is not None


async def has_pending_invite(session: AsyncSession, email: str, *, now: datetime) -> bool:
    found = await session.scalar(
        select(OrganizationInvite.id).where(
            OrganizationInvite.email == email.lower(),
            OrganizationInvite.accepted_at.is_(None),
            OrganizationInvite.revoked_at.is_(None),
            OrganizationInvite.expires_at > now,
        )
    )
    return found is not None


#: Who `orchestrator.admin` records as having made a membership or an invite.
ADMIN_PRINCIPAL: Final = "orchestrator.admin"


async def grant_owner(session: AsyncSession, email: str, *, now: datetime) -> str | None:
    """`orchestrator.admin grant-owner`: an install with data and no owner gets one. An account
    becomes the owner of `default` at once, and the invite token is None. An email with no account
    gets an owner's invite instead, and its token: the person signs up from the link, with a
    password or SSO, and accepting it makes them owner. Signup accepts an email with a pending
    invite in every shape, so this works where no SSO is configured."""
    try:
        address = normalize_email(email)
    except InvalidAccountInput as exc:
        raise Invalid(str(exc)) from None
    organization_id = await _default_id(session)
    owner, _ = await _role(session, OWNER)
    user = await user_by_email(session, address)
    if user is None:
        _, token = await _issue_invite(
            session, organization_id, address, owner, by=ADMIN_PRINCIPAL, now=now
        )
        return token
    await _readmitted(session, organization_id, address)
    await session.execute(
        insert(OrganizationMember)
        .values(
            organization_id=organization_id,
            user_id=user.id,
            role_id=owner.id,
            added_by=ADMIN_PRINCIPAL,
        )
        .on_conflict_do_update(
            index_elements=[OrganizationMember.organization_id, OrganizationMember.user_id],
            set_={"role_id": owner.id},
        )
    )
    await session.flush()
    return None


async def create_organization(
    session: AsyncSession, caller: Caller, *, name: str, slug: str, setup_token: bool
) -> Organization:
    """`setup_token`: while one is set and `default` has no owner, nobody creates an organization,
    so the install waits for the person who runs it."""
    user = await _person(session, caller)
    if setup_token and await install_is_empty(session, setup_token=True):
        raise Forbidden(
            "This install has no owner yet. Organizations open once its owner signs up."
        )
    name = name.strip()
    if not 0 < len(name) <= 200:
        raise Invalid("A name is 1 to 200 characters.")
    if not _SLUG.fullmatch(slug):
        raise Invalid(
            "A slug is 3 to 40 lowercase letters, digits and dashes, starting and ending with a "
            "letter or digit."
        )
    if slug in RESERVED_SLUGS:
        raise Invalid(f"{slug!r} is reserved.")
    organization_id = await session.scalar(
        insert(Organization)
        .values(slug=slug, name=name, updated_by=user.email)
        .on_conflict_do_nothing()
        .returning(Organization.id)
    )
    if organization_id is None:
        raise Conflict(f"The slug {slug!r} is taken.")
    owner, _ = await _role(session, OWNER)
    session.add(
        OrganizationMember(
            organization_id=organization_id,
            user_id=user.id,
            role_id=owner.id,
            added_by=user.email,
        )
    )
    await session.flush()
    organization = await session.get(Organization, organization_id)
    assert organization is not None
    return organization


@dataclass(frozen=True, slots=True)
class MemberRow:
    user_id: int
    email: str
    name: str
    role: str
    verified: bool
    added_by: str
    created_at: datetime


async def list_members(session: AsyncSession, organization_id: int) -> list[MemberRow]:
    rows = await session.execute(
        select(OrganizationMember, AppUser, Role.key)
        .join(AppUser, AppUser.id == OrganizationMember.user_id)
        .join(Role, Role.id == OrganizationMember.role_id)
        .where(OrganizationMember.organization_id == organization_id)
        .order_by(AppUser.email)
    )
    return [
        MemberRow(
            user_id=user.id,
            email=user.email,
            name=user.name,
            role=role,
            verified=user.email_verified_at is not None,
            added_by=member.added_by,
            created_at=member.created_at,
        )
        for member, user, role in rows.all()
    ]


async def _member(
    session: AsyncSession, organization_id: int, user_id: int
) -> tuple[OrganizationMember, str]:
    found = (
        await session.execute(
            select(OrganizationMember, Role.key)
            .join(Role, Role.id == OrganizationMember.role_id)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == user_id,
            )
            .with_for_update(of=OrganizationMember)
        )
    ).one_or_none()
    if found is None:
        raise NotFound(f"Member {user_id} not found.")
    return found[0], found[1]


async def _keep_an_owner(session: AsyncSession, organization_id: int, role: str) -> None:
    if role != OWNER:
        return
    owner, _ = await _role(session, OWNER)
    # Locked, so two owners leaving at once cannot both see the other still there.
    owners = await session.scalars(
        select(OrganizationMember.user_id)
        .where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.role_id == owner.id,
        )
        .with_for_update()
    )
    if len(owners.all()) <= 1:
        raise Conflict(
            "An organization keeps at least one owner.", hint="Make someone else an owner first."
        )


async def change_role(
    session: AsyncSession,
    caller: Caller,
    held: frozenset[Permission],
    *,
    organization_id: int,
    user_id: int,
    role: str,
    now: datetime,
) -> None:
    member, current = await _member(session, organization_id, user_id)
    target, granted = await _role(session, role)
    _may_grant(held, granted)
    if current == role:
        return
    await _keep_an_owner(session, organization_id, current)
    member.role_id = target.id
    member.added_by = _by(caller)
    _, before = await _role(session, current)
    if not before <= granted:
        person = await session.get(AppUser, user_id)
        assert person is not None, "a membership row holds its account"
        await _revoke_issued(session, organization_id, person.email, by=_by(caller), now=now)
    await session.flush()


async def remove_member(
    session: AsyncSession,
    caller: Caller,
    held: frozenset[Permission],
    *,
    organization_id: int,
    user_id: int,
    now: datetime,
) -> None:
    """The membership row is deleted, the person's pending invites to the organization, and the
    ones they sent, are revoked, and the removal is recorded, so only a new invite lets them back
    in."""
    member, role = await _member(session, organization_id, user_id)
    person = await session.get(AppUser, user_id)
    assert person is not None, "a membership row holds its account"
    leaving = person.email == caller.email
    if not leaving:
        if Permission.MEMBERS_REMOVE not in held:
            raise Forbidden(f"This needs the {Permission.MEMBERS_REMOVE} permission.")
        _, theirs = await _role(session, role)
        if Permission.MEMBERS_ASSIGN_ROLES not in held and not theirs < held:
            raise Forbidden("You can remove only members whose role holds less than yours.")
    await _keep_an_owner(session, organization_id, role)
    await session.delete(member)
    by = _by(caller)
    await _revoke_pending(session, organization_id, person.email, by=by, now=now)
    await _revoke_issued(session, organization_id, person.email, by=by, now=now)
    reason = RemovalReason.LEFT if leaving else RemovalReason.REMOVED
    await session.execute(
        insert(OrganizationRemoval)
        .values(
            organization_id=organization_id,
            email=person.email,
            reason=reason,
            removed_by=by,
            removed_at=now,
        )
        .on_conflict_do_update(
            index_elements=[OrganizationRemoval.organization_id, OrganizationRemoval.email],
            set_={"reason": reason, "removed_by": by, "removed_at": now},
        )
    )
    await session.flush()


async def _revoke_pending(
    session: AsyncSession, organization_id: int, address: str, *, by: str, now: datetime
) -> None:
    await session.execute(
        update(OrganizationInvite)
        .where(
            OrganizationInvite.organization_id == organization_id,
            OrganizationInvite.email == address,
            OrganizationInvite.accepted_at.is_(None),
            OrganizationInvite.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoked_by=by)
    )


async def _revoke_issued(
    session: AsyncSession, organization_id: int, creator: str, *, by: str, now: datetime
) -> None:
    """The pending invites `creator` sent. Accepting one never asks whether its sender still may
    invite, so a removal or a demotion ends their authority here."""
    await session.execute(
        update(OrganizationInvite)
        .where(
            OrganizationInvite.organization_id == organization_id,
            OrganizationInvite.created_by == creator,
            OrganizationInvite.accepted_at.is_(None),
            OrganizationInvite.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoked_by=by)
    )


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


@dataclass(frozen=True, slots=True)
class NewInvite:
    invite: OrganizationInvite
    role: str
    #: Shown once, in the answer that creates the invite; only its digest is stored.
    token: str


async def create_invite(
    session: AsyncSession,
    caller: Caller,
    held: frozenset[Permission],
    *,
    organization_id: int,
    email: str,
    role: str,
    now: datetime,
) -> NewInvite:
    try:
        address = normalize_email(email)
    except InvalidAccountInput as exc:
        raise Invalid(str(exc)) from None
    target, granted = await _role(session, role)
    _may_grant(held, granted)
    already = await session.scalar(
        select(OrganizationMember.user_id)
        .join(AppUser, AppUser.id == OrganizationMember.user_id)
        .where(OrganizationMember.organization_id == organization_id, AppUser.email == address)
    )
    if already is not None:
        raise Conflict(f"{address} is already a member.")
    invite, token = await _issue_invite(
        session, organization_id, address, target, by=_by(caller), now=now
    )
    return NewInvite(invite=invite, role=role, token=token)


async def _issue_invite(
    session: AsyncSession,
    organization_id: int,
    address: str,
    role: Role,
    *,
    by: str,
    now: datetime,
) -> tuple[OrganizationInvite, str]:
    # One pending invite per address: a new one replaces the last.
    await _revoke_pending(session, organization_id, address, by=by, now=now)
    token = secrets.token_urlsafe(32)
    invite = OrganizationInvite(
        organization_id=organization_id,
        email=address,
        role_id=role.id,
        token_hash=_digest(token),
        created_by=by,
        created_at=now,
        expires_at=now + INVITE_LIFETIME,
    )
    session.add(invite)
    await session.flush()
    return invite, token


async def list_invites(
    session: AsyncSession, organization_id: int, *, now: datetime
) -> list[tuple[OrganizationInvite, str]]:
    rows = await session.execute(
        select(OrganizationInvite, Role.key)
        .join(Role, Role.id == OrganizationInvite.role_id)
        .where(
            OrganizationInvite.organization_id == organization_id,
            OrganizationInvite.accepted_at.is_(None),
            OrganizationInvite.revoked_at.is_(None),
            OrganizationInvite.expires_at > now,
        )
        .order_by(OrganizationInvite.id.desc())
    )
    return [(invite, role) for invite, role in rows.all()]


async def revoke_invite(
    session: AsyncSession, caller: Caller, *, organization_id: int, invite_id: int, now: datetime
) -> None:
    revoked = await session.scalar(
        update(OrganizationInvite)
        .where(
            OrganizationInvite.id == invite_id,
            OrganizationInvite.organization_id == organization_id,
            OrganizationInvite.accepted_at.is_(None),
            OrganizationInvite.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoked_by=_by(caller))
        .returning(OrganizationInvite.id)
    )
    if revoked is None:
        raise NotFound(f"Pending invite {invite_id} not found.")


@dataclass(frozen=True, slots=True)
class InvitePreview:
    organization: Organization
    role: str
    created_by: str
    expires_at: datetime
    #: Whether the session's email is the one the invite is for. The address itself is never
    #: shown: the link may have been forwarded.
    for_you: bool


async def _invite(
    session: AsyncSession, token: str, *, now: datetime, lock: bool = False
) -> tuple[OrganizationInvite, Organization, str]:
    query = (
        select(OrganizationInvite, Organization, Role.key)
        .join(Organization, Organization.id == OrganizationInvite.organization_id)
        .join(Role, Role.id == OrganizationInvite.role_id)
        .where(OrganizationInvite.token_hash == _digest(token))
    )
    if lock:
        query = query.with_for_update(of=OrganizationInvite)
    found = (await session.execute(query)).one_or_none()
    if found is None:
        raise NotFound("This invite link is not valid.")
    invite, organization, role = found
    if invite.revoked_at is not None:
        raise Conflict("This invite was revoked.", hint="Ask for a new one.")
    if invite.accepted_at is not None:
        raise Conflict("This invite was already used.")
    if invite.expires_at <= now:
        raise Conflict("This invite has expired.", hint="Ask for a new one.")
    return invite, organization, role


async def invite_admits(session: AsyncSession, token: str, email: str, *, now: datetime) -> bool:
    """Whether `token` is a live invite for `email`. A signup by password must show one when the
    install admits no one else: knowing the invited address is not enough, only the link is."""
    try:
        invite, _, _ = await _invite(session, token, now=now)
    except (NotFound, Conflict):
        return False
    return invite.email == email.lower()


async def preview_invite(
    session: AsyncSession, caller: Caller, token: str, *, now: datetime
) -> InvitePreview:
    user = await _person(session, caller)
    invite, organization, role = await _invite(session, token, now=now)
    return InvitePreview(
        organization=organization,
        role=role,
        created_by=invite.created_by,
        expires_at=invite.expires_at,
        for_you=invite.email == user.email,
    )


@dataclass(frozen=True, slots=True)
class AcceptedInvite:
    organization: Organization
    role: str


async def accept_invite(
    session: AsyncSession, caller: Caller, token: str, *, now: datetime
) -> AcceptedInvite:
    user = await _person(session, caller)
    invite, organization, role = await _invite(session, token, now=now, lock=True)
    if invite.email != user.email:
        raise Forbidden(
            "This invite is for another email address.",
            hint="Sign in with the address the invite was sent to.",
        )
    added = await session.scalar(
        insert(OrganizationMember)
        .values(
            organization_id=organization.id,
            user_id=user.id,
            role_id=invite.role_id,
            added_by=invite.created_by,
        )
        .on_conflict_do_nothing()
        .returning(OrganizationMember.user_id)
    )
    if added is None:
        # An SSO sign-in can join the person as a member before they open an invite to more.
        member, current = await _member(session, organization.id, user.id)
        roles = await built_in_roles(session)
        if current not in roles or not roles[current][1] < roles[role][1]:
            raise Conflict(f"You are already a member of {organization.name}.")
        member.role_id = invite.role_id
        member.added_by = invite.created_by
    await _readmitted(session, organization.id, user.email)
    invite.accepted_at = now
    invite.accepted_by = user.id
    await session.flush()
    return AcceptedInvite(organization=organization, role=role)

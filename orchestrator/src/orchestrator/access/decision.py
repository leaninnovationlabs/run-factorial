"""The one access decision: may this caller do this in this organization (design.md §7.19).

The API's guard, the MCP server and the assistant call `authorize`; none decides on its own.
The operator — the shared API token — holds every permission everywhere. A person holds what
their membership's role holds, whatever the multi-org flag says: the flag decides only who may
sign up and create organizations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.access.permissions import Permission, granted
from orchestrator.db.models import (
    AppUser,
    ChatConversation,
    ChatItem,
    ChatProposal,
    ChatTurn,
    McpToken,
    Organization,
    OrganizationMember,
    Project,
    Role,
    RolePermission,
    Runtime,
    StepRun,
    Ticket,
)

__all__ = [
    "ALL_PERMISSIONS",
    "OPERATOR",
    "OPERATOR_PRINCIPAL",
    "OWNER_PARAMS",
    "Caller",
    "Forbidden",
    "Membership",
    "NotMember",
    "UnknownOwner",
    "allowed",
    "allowed_on_run",
    "authorize",
    "caller_named",
    "membership",
    "organization_of",
]

ALL_PERMISSIONS: Final = frozenset(Permission)


@dataclass(frozen=True, slots=True)
class Caller:
    """A person by their session's email, or the operator, who has none."""

    email: str | None

    @property
    def operator(self) -> bool:
        return self.email is None


OPERATOR: Final = Caller(None)

#: What a row records for the operator (`api.auth.BEARER_PRINCIPAL`, spelled out: access sits
#: below the API).
OPERATOR_PRINCIPAL: Final = "api-token"


def caller_named(principal: str) -> Caller:
    """The caller a recorded principal — a conversation's owner, a token's creator — names."""
    return OPERATOR if principal == OPERATOR_PRINCIPAL else Caller(principal.lower())


@dataclass(frozen=True, slots=True)
class Membership:
    role: str
    permissions: frozenset[Permission]


class NotMember(Exception):
    """Answered as 404, never 403: a slug must not tell a stranger the organization exists."""


class Forbidden(Exception):
    def __init__(self, permission: Permission) -> None:
        super().__init__(f"This needs the {permission} permission.")
        self.permission = permission


async def membership(
    session: AsyncSession, *, organization_id: int, email: str
) -> Membership | None:
    rows = (
        await session.execute(
            select(Role.key, RolePermission.permission_key)
            .join(OrganizationMember, OrganizationMember.role_id == Role.id)
            .join(AppUser, AppUser.id == OrganizationMember.user_id)
            .outerjoin(RolePermission, RolePermission.role_id == Role.id)
            .where(
                OrganizationMember.organization_id == organization_id,
                AppUser.email == email.lower(),
            )
        )
    ).all()
    if not rows:
        return None
    return Membership(role=rows[0][0], permissions=granted(key for _, key in rows if key))


async def authorize(
    session: AsyncSession,
    caller: Caller,
    organization_id: int,
    permission: Permission,
) -> frozenset[Permission]:
    """What the caller holds in the organization, once it is known to include `permission`."""
    if caller.operator:
        return ALL_PERMISSIONS
    assert caller.email is not None
    member = await membership(session, organization_id=organization_id, email=caller.email)
    if member is None:
        raise NotMember
    if permission not in member.permissions:
        raise Forbidden(permission)
    return member.permissions


async def allowed(
    session: AsyncSession, caller: Caller, organization_id: int, permission: Permission
) -> bool:
    """`authorize` for a caller that is not answering a request: a token, a turn."""
    try:
        await authorize(session, caller, organization_id, permission)
    except (NotMember, Forbidden):
        return False
    return True


async def allowed_on_run(
    session: AsyncSession, caller: Caller, step_run_id: int, permission: Permission
) -> bool:
    """`allowed` in the organization of a run, for a way in outside the API's guard: a page
    link, a terminal. A run that does not exist is allowed here, for the caller to answer as it
    answers any missing run."""
    try:
        organization_id = await organization_of(session, {"step_run_id": str(step_run_id)})
    except UnknownOwner:
        return True
    return await allowed(session, caller, organization_id, permission)


#: How each path parameter names a row, and how that row reaches its organization. The guard
#: takes the first one a route's path holds.
_OWNERS: Final = (
    ("org", Organization.slug, select(Organization.id)),
    ("project_id", Project.id, select(Project.organization_id)),
    (
        "ticket_id",
        Ticket.id,
        select(Project.organization_id).join(Ticket, Ticket.project_id == Project.id),
    ),
    (
        "step_run_id",
        StepRun.id,
        select(Project.organization_id)
        .join(Ticket, Ticket.project_id == Project.id)
        .join(StepRun, StepRun.ticket_id == Ticket.id),
    ),
    ("runtime_id", Runtime.id, select(Runtime.organization_id)),
    ("conversation_id", ChatConversation.id, select(ChatConversation.organization_id)),
    (
        "proposal_id",
        ChatProposal.id,
        select(ChatConversation.organization_id).join(
            ChatProposal, ChatProposal.conversation_id == ChatConversation.id
        ),
    ),
    (
        "turn_id",
        ChatTurn.id,
        select(ChatConversation.organization_id).join(
            ChatTurn, ChatTurn.conversation_id == ChatConversation.id
        ),
    ),
    (
        "item_id",
        ChatItem.id,
        select(ChatConversation.organization_id).join(
            ChatItem, ChatItem.conversation_id == ChatConversation.id
        ),
    ),
    ("token_id", McpToken.id, select(McpToken.organization_id)),
)

#: The path parameters `organization_of` understands, for the test that every guarded route
#: holds one.
OWNER_PARAMS: Final = tuple(name for name, _, _ in _OWNERS)


class UnknownOwner(LookupError):
    """The path names no row, or names none of `OWNER_PARAMS`."""


async def organization_of(session: AsyncSession, path_params: dict[str, str]) -> int:
    for name, column, query in _OWNERS:
        if name not in path_params:
            continue
        raw = path_params[name]
        value: str | int = raw
        if name != "org":
            try:
                value = int(raw)
            except ValueError:
                raise UnknownOwner(name) from None
        found = await session.scalar(query.where(column == value))
        if found is None:
            raise UnknownOwner(name)
        return found
    raise UnknownOwner("no organization in the path")

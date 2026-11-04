"""The closed permission catalogue: `module:action` keys, as the predicate library is closed
(design.md §6). A `permission` row may only hold one of these, and a test fails when the rows and
this enum differ, so a new key is a code change and a revision together.

Which role holds which key is data: revision 0035 seeds the built-in roles' grants and only a
revision changes them. The keys of those roles are code, because rules no role grants — the
last owner stays — name one."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final

__all__ = [
    "ADMIN",
    "BUILT_IN_ROLES",
    "GATED_READ_TOOLS",
    "MEMBER",
    "OWNER",
    "WRITE_TOOLS",
    "Permission",
    "granted",
]


class Permission(StrEnum):
    ORG_READ = "org:read"
    ORG_MANAGE = "org:manage"
    MEMBERS_INVITE = "members:invite"
    MEMBERS_REMOVE = "members:remove"
    MEMBERS_ASSIGN_ROLES = "members:assign_roles"
    PROJECTS_MANAGE = "projects:manage"
    PROJECTS_READ = "projects:read"
    TICKETS_OPERATE = "tickets:operate"
    TICKETS_TERMINAL = "tickets:terminal"
    RUNTIMES_MANAGE = "runtimes:manage"
    ASSISTANT_USE = "assistant:use"
    ASSISTANT_CONFIGURE = "assistant:configure"
    MCP_TOKENS_MINT = "mcp_tokens:mint"
    MCP_TOKENS_MANAGE = "mcp_tokens:manage"


def granted(keys: Iterable[str]) -> frozenset[Permission]:
    """The permissions a role's stored keys grant. A key the code no longer has grants nothing."""
    known = {permission.value for permission in Permission}
    return frozenset(Permission(key) for key in keys if key in known)


OWNER: Final = "owner"
ADMIN: Final = "admin"
MEMBER: Final = "member"
BUILT_IN_ROLES: Final = (OWNER, ADMIN, MEMBER)

#: What each write tool needs, whether an MCP client calls it or a person approves the
#: assistant's proposal to. A read tool needs `ORG_READ`, which every token's holder must have,
#: unless `GATED_READ_TOOLS` names it.
WRITE_TOOLS: Final[Mapping[str, Permission]] = MappingProxyType(
    {
        "factorial_ticket_exit": Permission.TICKETS_OPERATE,
        "factorial_ticket_reenter": Permission.TICKETS_OPERATE,
        "factorial_ticket_restart": Permission.TICKETS_OPERATE,
        "factorial_ticket_interrupt": Permission.TICKETS_OPERATE,
        "factorial_ticket_resume": Permission.TICKETS_OPERATE,
        "factorial_ticket_unblock": Permission.TICKETS_OPERATE,
        "factorial_ticket_retry": Permission.TICKETS_OPERATE,
        "factorial_ticket_requeue": Permission.TICKETS_OPERATE,
        "factorial_ticket_create": Permission.TICKETS_OPERATE,
        "factorial_config_commit": Permission.PROJECTS_MANAGE,
    }
)

#: Tools that change nothing but read what only a settings page shows: a project's config,
#: which the REST routes give `PROJECTS_READ`. Opening the editor drafts a change only a
#: person who may commit it should start, so it needs `PROJECTS_MANAGE`.
GATED_READ_TOOLS: Final[Mapping[str, Permission]] = MappingProxyType(
    {
        "factorial_config_read": Permission.PROJECTS_READ,
        "factorial_config_validate": Permission.PROJECTS_READ,
        "factorial_editor_open": Permission.PROJECTS_MANAGE,
    }
)

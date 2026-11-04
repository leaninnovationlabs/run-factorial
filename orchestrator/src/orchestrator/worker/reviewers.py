"""Who may decide a gate — the one rule behind a gate's approvals, its change requests, and the
review text its rework receives (design.md §7.12), so the three cannot disagree.

Bots never decide on their own standing. Agents act as the App and hold `pull_requests: write`,
so they can approve a pull request a person opened; GitHub stops an account approving only its
own. The App's review therefore never counts, even when named, and another bot counts only when
`approvers` names it — never through an empty list or through permission.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from orchestrator.config import ApproverPermission, Gate, expand_principals
from orchestrator.github.state import RepoPermission, permission_meets

__all__ = [
    "GateReviewers",
    "Ignored",
    "PermissionOf",
    "Qualification",
    "Weighed",
    "gate_reviewers",
    "qualify",
]

#: `login -> the role GitHub reports`, asked only for a reviewer the rule cannot settle by name.
PermissionOf = Callable[[str], Awaitable[RepoPermission]]


@dataclass(frozen=True, slots=True)
class Qualification:
    """Why one reviewer counted, kept on the gate's event because a role can change later."""

    basis: Literal["named", "permission", "open"]
    #: The role GitHub reported, for `permission` only.
    permission: RepoPermission | None = None


@dataclass(frozen=True, slots=True)
class Ignored:
    """Why one reviewer's review decided nothing."""

    reason: Literal["app", "bot", "not_named", "below_permission"]
    #: The role GitHub reported, for `below_permission` only.
    permission: RepoPermission | None = None


@dataclass(frozen=True, slots=True)
class Weighed:
    """Every login `qualify` was given, split. Decisions read `counted` only."""

    counted: dict[str, Qualification] = field(default_factory=dict)
    ignored: dict[str, Ignored] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GateReviewers:
    """One gate's decision rule, resolved for this tick. Logins are case-folded."""

    named: frozenset[str]
    bot_login: str
    floor: ApproverPermission | None = None

    @property
    def open(self) -> bool:
        """Any person counts only when nobody is named AND no role is. Never `not named`
        alone: under a floor that would admit every read-only reviewer."""
        return not self.named and self.floor is None


def gate_reviewers(
    gate: Gate,
    roles: Mapping[str, Sequence[str]],
    values: Mapping[str, str],
    *,
    bot_login: str,
) -> GateReviewers:
    named = expand_principals(gate.approvers, roles, values)
    return GateReviewers(
        named=frozenset(login.casefold() for login in named),
        bot_login=bot_login.casefold(),
        floor=gate.approver_permission,
    )


async def qualify(
    reviewers: GateReviewers,
    logins: Iterable[str],
    *,
    bots: Collection[str],
    permission_of: PermissionOf,
) -> Weighed:
    """Each login in `logins`, in order: whether it may decide the gate, and why. Only a login
    that is neither named nor a bot, on a gate with a floor, costs a lookup."""
    bot_logins = {login.casefold() for login in bots}
    weighed = Weighed()
    for login in dict.fromkeys(logins):
        folded = login.casefold()
        if reviewers.bot_login and folded == reviewers.bot_login:
            weighed.ignored[login] = Ignored("app")
        elif folded in reviewers.named:
            weighed.counted[login] = Qualification("named")
        elif folded in bot_logins:
            weighed.ignored[login] = Ignored("bot")
        elif reviewers.open:
            weighed.counted[login] = Qualification("open")
        elif reviewers.floor is None:
            weighed.ignored[login] = Ignored("not_named")
        else:
            held = await permission_of(login)
            if permission_meets(held, reviewers.floor):
                weighed.counted[login] = Qualification("permission", held)
            else:
                weighed.ignored[login] = Ignored("below_permission", held)
    return weighed

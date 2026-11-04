"""The operator's per-agent permission ceiling — the most a repo's agent manifest may request
beyond the shared default GitHub App token profile (decision S7).

`agent.yaml`'s `permissions:` block lives in the repo agents open PRs against, so — as with MCP
servers (`mcp_policy.py`) — it is a request, never a grant; the operator-owned ceiling in
`project.settings` is the boundary. Absence on either side falls back to the default profile
(`github/client.py`'s `APP_PERMISSIONS`): a manifest with no `permissions:` key requests it, not
nothing, and a project with no `permission_ceiling` permits requests only up to it.

`workflows` is never grantable at any level, and this module need not check: `Permissions` and
`PermissionCeiling` are both closed, named-field models with `extra="forbid"`, so naming it fails
to parse first (design.md §7.2 — CI runs agent-authored code with repo secrets before review).

Pure functions, like `mcp_policy`: `factorial.registry` raises `PermissionNotAllowed`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from orchestrator.config import Permissions
from orchestrator.github.client import APP_PERMISSIONS, PERMISSION_LEVEL_RANK

__all__ = [
    "CEILING_KEY",
    "PERMISSION_SCOPES",
    "PermissionCeiling",
    "PermissionCeilingInvalid",
    "check_permissions",
    "parse_ceiling",
    "requested_permissions",
]

#: The `project.settings` key (data-model.md, "Project settings"). Absent means "the default
#: profile is the ceiling" — see the module docstring; never "anything goes".
CEILING_KEY: Final = "permission_ceiling"

PermissionLevel = Literal["read", "write"]

#: The closed set of GitHub App permission scope names this factory grants at all. Mirrored as
#: named fields on `PermissionCeiling` below and on `config.models.Permissions`; `workflows` is
#: absent from both, enforced by each pydantic model (design.md §7.2).
PERMISSION_SCOPES: Final[tuple[str, ...]] = (
    "contents",
    "issues",
    "pull_requests",
    "metadata",
    "checks",
    "actions",
)

#: Shared with `client.check_granted`, which applies the ordering differently (a `write` ceiling
#: covers a `read` request here; a `write` grant does not satisfy a `read` request there). They
#: must still agree on the ordering, which one table guarantees.
_LEVEL_RANK: Final[Mapping[str, int]] = PERMISSION_LEVEL_RANK

#: One dict, two fallbacks — what an absent manifest `permissions:` key requests and what an
#: absent project `permission_ceiling` permits — so the two cannot drift. The cast is unchecked
#: (`dict()` erases the TypedDict's value types), so a future non-read/write level would not fail
#: typecheck; `test_the_closed_set_matches_the_default_profiles_keys` and `check_permissions`'
#: total `_LEVEL_RANK.get` lookups guard that instead.
_DEFAULT_PROFILE: Final[Mapping[str, PermissionLevel]] = cast(
    "Mapping[str, PermissionLevel]", dict(APP_PERMISSIONS)
)


class PermissionCeilingInvalid(ValueError):
    """`project.settings.permission_ceiling` is not a well-formed scope -> read|write mapping."""


class PermissionCeiling(BaseModel):
    """The operator's ceiling: the most any agent manifest in this project may request.

    Named fields plus `extra="forbid"`, as in `config.models.Permissions`, are what make
    `workflows` structurally undeclarable here (design.md §7.2).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contents: PermissionLevel | None = None
    issues: PermissionLevel | None = None
    pull_requests: PermissionLevel | None = None
    metadata: PermissionLevel | None = None
    checks: PermissionLevel | None = None
    actions: PermissionLevel | None = None


def parse_ceiling(settings: Mapping[str, Any]) -> PermissionCeiling | None:
    """Read `permission_ceiling` out of a project's settings bag.

    A missing key returns `None`, which `check_permissions` reads as "the default profile is the
    ceiling". A malformed value raises rather than degrading to "no ceiling": an operator typo
    must never read as "anything goes".
    """
    raw = settings.get(CEILING_KEY)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise PermissionCeilingInvalid(
            f"project.settings.{CEILING_KEY} must be a mapping of scope -> read|write, got "
            f"{type(raw).__name__}"
        )
    try:
        return PermissionCeiling.model_validate(raw)
    except ValidationError as exc:
        raise PermissionCeilingInvalid(
            f"project.settings.{CEILING_KEY} is invalid:\n{exc}"
        ) from exc


def requested_permissions(requested: Permissions | None) -> Mapping[str, str]:
    """The scope → level map a manifest actually asks for; absence means the default profile.

    Both the ceiling check below and `worker.agent_steps._build_run_spec` (which mints the token)
    resolve a manifest through this one function, so the request validated against the ceiling
    and the request sent to GitHub are the same map by construction.
    """
    return _DEFAULT_PROFILE if requested is None else _as_mapping(requested)


def check_permissions(
    requested: Permissions | None, ceiling: PermissionCeiling | None
) -> list[str]:
    """Every reason a manifest's requested permissions may not be granted. Empty == permitted.

    All violations are collected rather than raising on the first, matching `check_allowed`.
    `write` covers a request of `read` for the same scope; `read` never covers `write`.
    """
    request_map = requested_permissions(requested)
    ceiling_map = _DEFAULT_PROFILE if ceiling is None else _as_mapping(ceiling)

    problems: list[str] = []
    for scope, level in sorted(request_map.items()):
        allowed = ceiling_map.get(scope)
        if allowed is None:
            permitted = ", ".join(f"{k}:{v}" for k, v in sorted(ceiling_map.items())) or "(none)"
            problems.append(
                f"requests {scope}:{level}, which is outside the permission ceiling; "
                f"permitted: {permitted}"
            )
            continue
        # Total lookups, failing closed: a request at an unrecognized level outranks everything
        # (refused), a ceiling at an unrecognized level grants nothing. Such a level can only
        # arrive via `_DEFAULT_PROFILE`'s unchecked cast above.
        if _LEVEL_RANK.get(level, 3) > _LEVEL_RANK.get(allowed, 0):
            problems.append(
                f"requests {scope}:{level} but the permission ceiling grants only {scope}:{allowed}"
            )
    return problems


def _as_mapping(model: BaseModel) -> dict[str, str]:
    return {key: value for key, value in model.model_dump().items() if value is not None}

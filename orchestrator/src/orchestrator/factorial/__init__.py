"""ADLC resolution: a project's `.factorial/` folder, read out of its repo at a pinned commit sha.

`orchestrator.config` parses text, `orchestrator.github.contents` fetches it; this package joins
the two to the registry rows naming the repo path and commit. It hands back content, never
paths, so a pinned sha pins behaviour.

Imports `config`, `github`, `db` and `events` only — never `worker`, `api` or `runtime`.
Mapping a `ResolvedAgent` onto the runtime contract is the worker's job.
"""

from __future__ import annotations

from orchestrator.factorial.mcp_policy import (
    ALLOWLIST_KEY,
    McpAllowEntry,
    McpAllowlistInvalid,
    check_allowed,
    parse_allowlist,
)
from orchestrator.factorial.permission_policy import (
    CEILING_KEY,
    PERMISSION_SCOPES,
    PermissionCeiling,
    PermissionCeilingInvalid,
    check_permissions,
    parse_ceiling,
    requested_permissions,
)
from orchestrator.factorial.process_policy import (
    DEFAULT_KEY,
    DefaultProcessInvalid,
    parse_default_process,
)
from orchestrator.factorial.registry import (
    AgentInactive,
    AgentNotRegistered,
    AgentPin,
    AgentPins,
    AgentResolutionError,
    ConfigCommitRefused,
    DefaultProcessMoved,
    McpNotAllowed,
    PermissionNotAllowed,
    ProcessRegistration,
    ProcessSyncResult,
    ProcessVersionExists,
    adopters,
    register_process_versions,
    resolve_agent,
    upsert_agent_rows,
)
from orchestrator.factorial.source import (
    CONFIG_BRANCH,
    CONFIG_DIRS,
    DEFAULT_CONFIG_DIR,
    RepoConfigSource,
)
from orchestrator.factorial.types import ResolvedAgent, ResolvedMcpServer, ResolvedSkill

__all__ = [
    "ALLOWLIST_KEY",
    "CEILING_KEY",
    "CONFIG_BRANCH",
    "CONFIG_DIRS",
    "DEFAULT_CONFIG_DIR",
    "DEFAULT_KEY",
    "PERMISSION_SCOPES",
    "AgentInactive",
    "AgentNotRegistered",
    "AgentPin",
    "AgentPins",
    "AgentResolutionError",
    "ConfigCommitRefused",
    "DefaultProcessInvalid",
    "DefaultProcessMoved",
    "McpAllowEntry",
    "McpAllowlistInvalid",
    "McpNotAllowed",
    "PermissionCeiling",
    "PermissionCeilingInvalid",
    "PermissionNotAllowed",
    "ProcessRegistration",
    "ProcessSyncResult",
    "ProcessVersionExists",
    "RepoConfigSource",
    "ResolvedAgent",
    "ResolvedMcpServer",
    "ResolvedSkill",
    "adopters",
    "check_allowed",
    "check_permissions",
    "parse_allowlist",
    "parse_ceiling",
    "parse_default_process",
    "register_process_versions",
    "requested_permissions",
    "resolve_agent",
    "upsert_agent_rows",
]

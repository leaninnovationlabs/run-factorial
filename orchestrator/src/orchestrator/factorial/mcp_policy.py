"""The operator's MCP allowlist — what a repo's `.factorial/` may ask for (design.md §7.5).

`.factorial/` lives in the repo agents open PRs against, so it is a request, not a trust
boundary: an
agent can propose adding a server to its own future runs. Pinning bounds the blast radius but
decides no policy. The boundary is the per-project allowlist in `project.settings`, which only an
operator can write and no agent can reach. Moving definitions into operator-owned storage later
would change where an `McpAllowEntry` comes from, not what it means.

Pure functions: no session, no ORM, no exception hierarchy. `factorial.registry` raises
`McpNotAllowed`, since that layer owns "this agent cannot run".
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from fnmatch import fnmatch
from typing import Any, Final, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from orchestrator.factorial.types import ResolvedMcpServer

__all__ = [
    "ALLOWLIST_KEY",
    "McpAllowEntry",
    "McpAllowlistInvalid",
    "check_allowed",
    "own_endpoints",
    "parse_allowlist",
]

#: The `project.settings` key (data-model.md, "Project settings"). Absent or empty means no MCP
#: server may run — the only safe reading of silence.
ALLOWLIST_KEY: Final = "mcp_allowlist"

_LDH_LABEL: Final = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE)

#: An entry that names no tool patterns permits all of them. The decision that matters is which
#: server; narrowing tools is optional tightening, available on both sides.
_ALL_TOOLS: Final[tuple[str, ...]] = ("*",)


class McpAllowlistInvalid(ValueError):
    """`project.settings.mcp_allowlist` is not a list of well-formed entries."""


class McpAllowEntry(BaseModel):
    """One permitted server, as an operator writes it into `project.settings`.

    Every field is matched exactly against the repo's manifest, so the repo cannot vary what
    runs: a matching name with a different URL, or the same command with different args, is a
    different server.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    transport: Literal["http", "sse", "stdio"]
    url: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    #: `fnmatch` patterns. Empty means every tool this server exposes.
    tools: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("a stdio entry needs `command`")
            if self.url is not None:
                raise ValueError("a stdio entry has no `url`")
        else:
            if not self.url:
                raise ValueError(f"a {self.transport} entry needs `url`")
            if self.command is not None or self.args:
                raise ValueError(f"a {self.transport} entry has no `command`/`args`")
        return self

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        return self.tools or _ALL_TOOLS


def parse_allowlist(settings: Mapping[str, Any]) -> tuple[McpAllowEntry, ...]:
    """Read `mcp_allowlist` out of a project's settings bag.

    A missing key is an empty allowlist, not an error: unset means none, never all.
    """
    raw = settings.get(ALLOWLIST_KEY)
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise McpAllowlistInvalid(
            f"project.settings.{ALLOWLIST_KEY} must be a list of entries, got {type(raw).__name__}"
        )
    entries: list[McpAllowEntry] = []
    for index, item in enumerate(raw):
        try:
            entries.append(McpAllowEntry.model_validate(item))
        except ValidationError as exc:
            raise McpAllowlistInvalid(
                f"project.settings.{ALLOWLIST_KEY}[{index}] is invalid:\n{exc}"
            ) from exc
    return tuple(entries)


def check_allowed(
    servers: Sequence[ResolvedMcpServer],
    allowlist: Sequence[McpAllowEntry],
    *,
    own_endpoints: frozenset[tuple[str, int]] = frozenset(),
) -> list[str]:
    """Every reason these servers may not run, as operator-readable lines. Empty == permitted.

    All violations are collected rather than raising on the first, so an operator fixing an
    allowlist gets the whole list in one pass. `own_endpoints` name factorial's own MCP server,
    which no allowlist entry can grant to an agent (design.md §7.5 item 9).
    """
    by_name = {entry.name: entry for entry in allowlist}
    problems: list[str] = []
    for server in servers:
        own_url = _own_server_url(server, own_endpoints)
        if own_url is not None:
            problems.append(
                f"MCP server {server.name!r} points at {own_url!r}, Factorial's own MCP server. "
                "An agent never gets Factorial's controls, even if the server is allowlisted"
            )
            continue
        entry = by_name.get(server.name)
        if entry is None:
            permitted = ", ".join(sorted(by_name)) or "(none)"
            problems.append(
                f"MCP server {server.name!r} is not in this project's {ALLOWLIST_KEY}; "
                f"permitted: {permitted}"
            )
            continue
        problems.extend(_entry_violations(server, entry))
    return problems


def _entry_violations(server: ResolvedMcpServer, entry: McpAllowEntry) -> list[str]:
    manifest = server.manifest
    problems: list[str] = []
    if manifest.transport != entry.transport:
        problems.append(
            f"MCP server {server.name!r} declares transport {manifest.transport!r} but the "
            f"allowlist permits {entry.transport!r}"
        )
        return problems  # the shape checks below would compare unrelated fields

    # `entry.url` is non-empty for http/sse — McpAllowEntry's validator guarantees it.
    if manifest.http is not None and not _same_url(manifest.http.url, entry.url or ""):
        problems.append(
            f"MCP server {server.name!r} points at {manifest.http.url!r}; the allowlist permits "
            f"{entry.url!r}"
        )
    if manifest.stdio is not None:
        if manifest.stdio.command != entry.command:
            problems.append(
                f"MCP server {server.name!r} runs command {manifest.stdio.command!r}; the "
                f"allowlist permits {entry.command!r}"
            )
        if manifest.stdio.args != entry.args:
            problems.append(
                f"MCP server {server.name!r} runs args {list(manifest.stdio.args)}; the allowlist "
                f"permits {list(entry.args)}. The args must match exactly, because a stdio server "
                "is arbitrary code that runs beside a live GH_TOKEN"
            )

    requested = manifest.tools.allow or _ALL_TOOLS
    for pattern in requested:
        if not any(_covers(allowed, pattern) for allowed in entry.allowed_tools):
            problems.append(
                f"MCP server {server.name!r} requests tool(s) matching {pattern!r}; the allowlist "
                f"permits {', '.join(entry.allowed_tools)}"
            )
    return problems


def _same_url(declared: str, allowed: str) -> bool:
    """Exact match, modulo a trailing slash and the case of scheme+host.

    Not a host- or prefix-match: `https://mcp.example.com/team-a/mcp` and `…/team-b/mcp` are
    different servers with different data behind them.
    """
    return _normalize_url(declared) == _normalize_url(allowed)


def _normalize_url(url: str) -> str:
    trimmed = url.strip().rstrip("/")
    scheme, sep, rest = trimmed.partition("://")
    if not sep:
        return trimmed
    host, slash, path = rest.partition("/")
    return f"{scheme.lower()}://{host.lower()}{slash}{path}"


def own_endpoints(*urls: str) -> frozenset[tuple[str, int]]:
    """The (host, port) pairs of these URLs, with the scheme's default port filled in."""
    return frozenset(endpoint for url in urls if (endpoint := _endpoint(url)) is not None)


def _endpoint(url: str) -> tuple[str, int] | None:
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    # A trailing dot is the same DNS name, so it must not slip past the comparison.
    host = (parts.hostname or "").rstrip(".")
    if not host:
        return None
    return host, port or (443 if parts.scheme.lower() == "https" else 80)


def _own_server_url(server: ResolvedMcpServer, own: frozenset[tuple[str, int]]) -> str | None:
    """The server's URL when it reaches, or may reach, one of factorial's own endpoints.

    Host and port, not `_same_url`: a path or case variant of the factory's URL reaches the same
    server, and an exact match would let any of them through. The port is part of the match so an
    agent's server on another port of the same host (a local dev setup) is not mistaken for it.
    """
    http = server.manifest.http
    if http is None or not own:
        return None
    if _ambiguous_authority(http.url) or _endpoint(http.url) in own:
        return http.url
    return None


def _ambiguous_authority(url: str) -> bool:
    """True when Python and the agent's client (WHATWG URL) could read a different host.

    Percent escapes, backslashes and non-ASCII characters in the authority, or a host that is not
    a plain hostname or IP literal, all parse differently across the two, so a spelling of the
    factory's host can pass the comparison and still reach it. Such a URL is refused outright.
    """
    url = url.strip()
    authority = url.partition("://")[2]
    authority = authority.translate({ord(c): "/" for c in "?#"}).partition("/")[0]
    if "\\" in url or not url.isascii() or "%" in authority:
        return True
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return True
    host = parts.hostname or ""
    if not host:
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    labels = host.rstrip(".").split(".")
    # WHATWG reads a host whose last label is numeric as an IPv4 address in any radix.
    if labels[-1].isdigit() or labels[-1].startswith("0x"):
        return True
    return not all(_LDH_LABEL.fullmatch(label) for label in labels)


def _covers(allowed: str, requested: str) -> bool:
    """Does an allowlist pattern cover a requested one?

    Covered when it is the same pattern, or when the allowed pattern (as a glob) matches it as a
    literal string: `search_*` covers `search_issues` and itself, and nothing covers `*` except
    `*`. Erring this way refuses a legitimate config, which an operator fixes in one line; the
    other direction grants a tool nobody approved.
    """
    return allowed == requested or fnmatch(requested, allowed)

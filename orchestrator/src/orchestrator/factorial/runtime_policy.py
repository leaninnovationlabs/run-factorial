"""Which runtimes a project may name, and which hosts its E2B runs may reach (decision 25).

The repo's `process.yaml` selects a runtime; operator state in `project.settings` permits it,
the trust direction of the MCP allowlist (design.md §7.5). A runtime whose isolation is `e2b`
sends the repo's code to a third party, so it runs only when the project names it. Host runtimes
are not gated here, so permitting one E2B runtime never refuses a project's host lanes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Final

__all__ = [
    "EGRESS_KEY",
    "RUNTIME_KEY",
    "SettingsInvalid",
    "egress_allowlist",
    "egress_covers",
    "egress_hosts",
    "runtime_permitted",
    "validate_settings",
]

RUNTIME_KEY: Final = "runtime_allowlist"
EGRESS_KEY: Final = "egress_allowlist"

#: A host or a `*.` wildcard of one, as E2B's egress rules take them. Never `*`, an address or a
#: CIDR: each opens a sandbox to far more than the one service an operator meant.
_HOST: Final = re.compile(r"(\*\.)?([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]([a-z0-9-]*[a-z0-9])?")


class SettingsInvalid(ValueError):
    """A `project.settings` list is not the shape it must be."""


def _strings(settings: Mapping[str, object], key: str) -> list[str] | None:
    value = settings.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SettingsInvalid(f"project.settings.{key} must be a list of strings")
    return list(value)


def runtime_permitted(settings: Mapping[str, object], slug: str, isolation: str) -> bool:
    """A runtime on the host always is; an E2B runtime only when the project lists it."""
    if isolation == "local_cli":
        return True  # never refused over a list that gates E2B runtimes only
    allowed = _strings(settings, RUNTIME_KEY)
    return allowed is not None and slug in allowed


def egress_hosts(hosts: Iterable[str], *, source: str) -> tuple[str, ...]:
    """`hosts` lowercased, or `SettingsInvalid` naming each entry that is not a host. Every
    operator list of egress hosts passes through here: the project's and the deployment's."""
    lowered = tuple(host.strip().lower() for host in hosts)
    bad = [host for host in lowered if not _HOST.fullmatch(host)]
    if bad:
        raise SettingsInvalid(f"{source} holds entries that are not hosts: {', '.join(bad)}")
    return lowered


def egress_allowlist(settings: Mapping[str, object]) -> tuple[str, ...]:
    """The extra hosts this project's E2B runs may reach — package registries, usually."""
    hosts = _strings(settings, EGRESS_KEY) or []
    return egress_hosts(hosts, source=f"project.settings.{EGRESS_KEY}")


def egress_covers(allowed: Iterable[str], host: str) -> bool:
    """Whether a run may reach `host`: listed exactly, or under a `*.` entry."""
    host = host.lower()
    return any(
        host == entry or (entry.startswith("*.") and host.endswith(entry[1:])) for entry in allowed
    )


def validate_settings(settings: Mapping[str, object]) -> None:
    """Refuse a settings write these lists would refuse at every spawn, or that names a GitLab
    host or CA file the GitLab clients refuse."""
    # Deferred: `orchestrator.gitlab` imports this package through its config reader.
    from orchestrator.gitlab.hosts import HostRefused, check_project_gitlab

    _strings(settings, RUNTIME_KEY)
    egress_allowlist(settings)
    try:
        check_project_gitlab(settings)
    except HostRefused as exc:
        raise SettingsInvalid(str(exc)) from exc

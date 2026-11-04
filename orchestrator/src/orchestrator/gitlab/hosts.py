"""Which GitLab host a project may name, and when it may use the deployment's GitLab credentials.

`settings.gitlab.base_url` is project state that any organization owner writes, and the API and
the worker send that project's token and the assistant's reads to it. The operator's own
`FACTORIAL_GITLAB__BASE_URL` origin is always allowed. Any other host must lead nowhere inside
the deployment: never loopback, link-local, unspecified or multicast. Under multi-org, where a
stranger owns an organization, it must also use https and never be a private, shared or reserved
address, and a project's CA is pasted PEM, never a host path. A single-organization deployment's
owners are the operator's own people, so there a project may keep plain http and a CA path, as
an on-premises GitLab behind a private CA needs. The check runs when the setting is written and
again each time a client is built, from a short-lived cache of lookups. The worker applies the
same address rules to each URL it fetches an issue's image from, every redirect included
(`check_fetch_url`): an issue's author names those. A name that answers differently between that
check and the connection (DNS rebinding) is an accepted residual.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from collections.abc import Mapping
from typing import Final
from urllib.parse import urlsplit

from orchestrator.backoff import ConnectionBackoff
from orchestrator.lru import BoundedLRU
from orchestrator.settings import get_settings, normalized_origin

__all__ = [
    "HostLookupFailed",
    "HostRefused",
    "check_base_url",
    "check_fetch_url",
    "check_project_gitlab",
    "checked_base_url",
    "uses_deployment_credentials",
]

_HOSTNAME: Final = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")
_LOOKUP_TIMEOUT: Final = 2.0
_LOOKUP_TTL: Final = 120.0
_LOOKUP_CACHE_SIZE: Final = 256
#: The resolver's answers that mean the name has no address, as against a resolver that failed.
_NO_SUCH_NAME: Final = frozenset(
    code for code in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", None)) if code is not None
)

type Address = ipaddress.IPv4Address | ipaddress.IPv6Address

#: (host, port, multi_org) -> (expires at, the refusal or None).
_lookups: BoundedLRU[tuple[str, int, bool], tuple[float, str | None]] = BoundedLRU(
    _LOOKUP_CACHE_SIZE
)


class HostRefused(ValueError):
    """A GitLab URL a project may not name."""


class HostLookupFailed(ConnectionBackoff):
    """The resolver did not answer for a project's GitLab host, so it is not checked yet and no
    client is built. The next pass asks again."""


def _deployment(operator_base: str | None, multi_org: bool | None) -> tuple[str, bool]:
    if operator_base is not None and multi_org is not None:
        return operator_base, multi_org
    settings = get_settings()
    return (
        settings.gitlab.base_url if operator_base is None else operator_base,
        settings.multi_org.enabled if multi_org is None else multi_org,
    )


def _is_operator(url: str, operator_base: str) -> bool:
    own = normalized_origin(operator_base) if operator_base.strip() else None
    return own is not None and normalized_origin(url) == own


def _refused(address: Address, *, multi_org: bool) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
    ):
        return True
    return multi_org and not address.is_global


def _literal(host: str) -> Address | None:
    """The address `host` spells, in any form a resolver reads as one (`127.1`, `2130706433`)."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError:
        return None


def _shape(url: str, *, multi_org: bool) -> tuple[str, int, bool]:
    """`(host, port, whether the host is an address)` of a URL that is not the operator's, or
    `HostRefused`."""
    if normalized_origin(url) is None:
        raise HostRefused(
            f"{url!r} is not an http or https URL with an ASCII host and no user name or password."
        )
    parts = urlsplit(url)
    https = parts.scheme.lower() == "https"
    if multi_org and not https:
        raise HostRefused("A GitLab URL other than the deployment's own must use https.")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise HostRefused("A GitLab URL may not carry a query or a fragment.")
    host = parts.hostname or ""
    address = _literal(host)
    if address is not None:
        if _refused(address, multi_org=multi_org):
            raise HostRefused(f"{host} is not an address a GitLab URL may name.")
    elif not _HOSTNAME.fullmatch(host):
        raise HostRefused(f"{host!r} is not a host name.")
    return host, parts.port or (443 if https else 80), address is not None


def checked_base_url(
    raw: str, *, operator_base: str | None = None, multi_org: bool | None = None
) -> str:
    """`raw` stripped, or `HostRefused`. No DNS: `check_base_url` adds that half."""
    url = raw.strip()
    operator, strict = _deployment(operator_base, multi_org)
    if not _is_operator(url, operator):
        _shape(url, multi_org=strict)
    return url


async def _lookup_refusal(host: str, port: int, *, multi_org: bool) -> str | None:
    try:
        async with asyncio.timeout(_LOOKUP_TIMEOUT):
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
    except TimeoutError:
        raise HostLookupFailed(f"{host} did not resolve in time.") from None
    except socket.gaierror as exc:
        if exc.errno not in _NO_SUCH_NAME:
            raise HostLookupFailed(f"{host} could not be resolved: {exc}") from exc
        # A name that does not resolve passes: no connection can reach it either.
        return None
    except UnicodeError:
        return None
    for info in infos:
        address = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        if _refused(address, multi_org=multi_org):
            return f"{host} resolves to {address}, which a GitLab URL may not reach."
    return None


async def check_base_url(
    raw: str, *, operator_base: str | None = None, multi_org: bool | None = None
) -> str:
    """`checked_base_url`, then refuse a host name that resolves to an address it refuses as a
    literal. A lookup over two seconds, or a resolver failure, raises `HostLookupFailed` and is
    not kept; any other answer is kept for two minutes."""
    url = raw.strip()
    operator, strict = _deployment(operator_base, multi_org)
    if _is_operator(url, operator):
        return url
    host, port, literal = _shape(url, multi_org=strict)
    if not literal:
        await _check_name(host, port, multi_org=strict)
    return url


async def _check_name(host: str, port: int, *, multi_org: bool) -> None:
    key = (host, port, multi_org)
    now = time.monotonic()
    expires, refusal = _lookups.get(key, (now, None))
    if expires <= now:
        refusal = await _lookup_refusal(host, port, multi_org=multi_org)
        _lookups.put(key, (now + _LOOKUP_TTL, refusal))
    if refusal is not None:
        raise HostRefused(refusal)


async def check_fetch_url(
    url: str, *, operator_base: str | None = None, multi_org: bool | None = None
) -> None:
    """Refuse (`HostRefused`) an http or https URL whose host is, or resolves to, an address a
    GitLab URL may not name; `HostLookupFailed` as `check_base_url`. The operator's own origin
    passes. Only the address is judged: an image may live at any path, query or scheme."""
    operator, strict = _deployment(operator_base, multi_org)
    if _is_operator(url, operator):
        return
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        raise HostRefused(f"{url!r} has no valid port.") from None
    https = parts.scheme.lower() == "https"
    if not https and parts.scheme.lower() != "http":
        raise HostRefused(f"{url!r} is not an http or https URL.")
    host = parts.hostname or ""
    address = _literal(host)
    if address is not None:
        if _refused(address, multi_org=strict):
            raise HostRefused(f"{host} is not an address the worker may fetch from.")
        return
    if not _HOSTNAME.fullmatch(host):
        raise HostRefused(f"{host!r} is not a host name.")
    await _check_name(host, port or (443 if https else 80), multi_org=strict)


def check_project_gitlab(
    settings: Mapping[str, object],
    *,
    operator_base: str | None = None,
    multi_org: bool | None = None,
) -> str | None:
    """Refuse a `settings.gitlab` the clients would refuse: its base URL's shape, and, under
    multi-org, a `ca_bundle` that names a file: there a host path would let a stranger have the
    server read a file of their choosing, so a project pastes its PEM instead. Returns the
    project's own base URL, if it names one."""
    bag = settings.get("gitlab")
    if bag is None:
        return None
    if not isinstance(bag, Mapping):
        raise HostRefused("project.settings.gitlab must be an object.")
    ca = bag.get("ca_bundle")
    if ca is not None and not isinstance(ca, str):
        raise HostRefused("project.settings.gitlab.ca_bundle must be a string.")
    if (
        isinstance(ca, str)
        and ca.strip()
        and not ca.strip().startswith("-----BEGIN")
        and _deployment(operator_base, multi_org)[1]
    ):
        raise HostRefused(
            "project.settings.gitlab.ca_bundle takes PEM text, not a file path. Store the PEM "
            "as the project secret GITLAB_CA_BUNDLE."
        )
    raw = bag.get("base_url")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if not isinstance(raw, str):
        raise HostRefused("project.settings.gitlab.base_url must be a string.")
    return checked_base_url(raw, operator_base=operator_base, multi_org=multi_org)


def uses_deployment_credentials(
    effective_base: str, *, operator_base: str, multi_org: bool
) -> bool:
    """Whether a project on `effective_base` may fall back to the deployment's GitLab token and
    webhook secret: only on the operator's own host, and never when organizations are open to
    anyone, where each project brings its own."""
    return not multi_org and _is_operator(effective_base, operator_base)

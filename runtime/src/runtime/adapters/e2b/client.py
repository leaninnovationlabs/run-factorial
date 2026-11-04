"""The `SandboxClient` seam: every E2B call the adapter makes goes through here (tech-stack.md
decision 9). The template builders import the SDK too, to build what a sandbox boots from.
The terminal gateway gets the narrower `TerminalSandboxClient`, which cannot change a sandbox's
lifecycle (decision 25).

The adapter depends on these Protocols, so tests run against a fake with no network. Commands are
strings because the SDK's are: the adapter only ever passes constant scripts, and every value
reaches them through `envs` or a file.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Protocol
from urllib.parse import quote

from runtime.contract import AdapterUnavailable, SpawnError, SpawnReason, SubstrateUnavailable

if TYPE_CHECKING:
    import httpx
    from e2b import AsyncSandbox
    from e2b.api.client.models import SandboxDetail
    from e2b.sandbox_async.commands.command_handle import AsyncCommandHandle

__all__ = [
    "CommandOutput",
    "E2bSandboxClient",
    "E2bTerminalClient",
    "NetworkPolicy",
    "Sandbox",
    "SandboxClient",
    "SandboxGone",
    "SandboxInfo",
    "SandboxOptions",
    "TemplateBuild",
    "TerminalPty",
    "TerminalSandbox",
    "TerminalSandboxClient",
    "record_session_verdict",
    "session_verdicts",
    "substrate_failure",
    "template_builds",
    "verdict_tag",
    "verified_builds",
]

log = logging.getLogger(__name__)


class SandboxGone(Exception):
    """The sandbox no longer exists: killed, timed out, or never created."""


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    """Egress: `allow` hosts only, and headers the proxy sets on requests to a host.

    `inject` values are credentials. They live in E2B's control plane while the sandbox runs and
    never inside it (decision 25).
    """

    allow: tuple[str, ...]
    inject: Mapping[str, Mapping[str, str]] = field(default_factory=dict, repr=False)
    #: Inbound traffic to every listening port, by URL. Set only at creation by E2B, so it is
    #: decided at spawn: on only for a run kept for a preview (decision 54).
    public: bool = False

    def as_sdk(self, *, create: bool) -> dict[str, Any]:
        network: dict[str, Any] = {
            "allow_out": list(self.allow),
            "deny_out": ["0.0.0.0/0"],
            "rules": {
                host: [{"transform": {"headers": dict(headers)}}]
                for host, headers in self.inject.items()
            },
        }
        if create:
            network["allow_public_traffic"] = self.public
        return network


@dataclass(frozen=True, slots=True)
class SandboxOptions:
    """Everything a sandbox is created with, built in one place."""

    template: str
    timeout_seconds: int
    metadata: Mapping[str, str]
    network: NetworkPolicy


@dataclass(frozen=True, slots=True)
class SandboxInfo:
    sandbox_id: str
    state: str  # "running" | "paused"
    metadata: Mapping[str, str]
    started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CommandOutput:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class Sandbox(Protocol):
    @property
    def sandbox_id(self) -> str: ...

    async def run(
        self,
        command: str,
        *,
        user: str = "root",
        envs: Mapping[str, str] | None = None,
        timeout_seconds: float = 120,
    ) -> CommandOutput:
        """Run to completion. A non-zero exit is returned, never raised."""
        ...

    async def start(
        self, command: str, *, user: str = "root", envs: Mapping[str, str] | None = None
    ) -> int:
        """Start in the background with no time limit and return its pid."""
        ...

    async def pids(self) -> frozenset[int]:
        """The pids of the background commands still running."""
        ...

    async def write_many(self, files: Mapping[str, bytes], *, user: str = "root") -> None:
        """Several files, as one user, in one round trip."""
        ...

    async def update_network(self, network: NetworkPolicy) -> None:
        """Replace the egress rules whole. On a handle, so it can never wake a paused sandbox."""
        ...

    def host(self, port: int) -> str:
        """The hostname E2B serves `port` on. Reachable only for a sandbox created public."""
        ...


class SandboxClient(Protocol):
    async def create(self, options: SandboxOptions) -> Sandbox: ...

    async def connect(self, sandbox_id: str, *, timeout_seconds: int) -> Sandbox:
        """Attach to a sandbox, resuming it when paused, with a fresh expiry."""
        ...

    async def info(self, sandbox_id: str) -> SandboxInfo | None:
        """The sandbox's state without waking it; `None` when it no longer exists."""
        ...

    async def pause(self, sandbox_id: str) -> None:
        """Pause keeping only the filesystem: no process and nothing in memory survives."""
        ...

    async def kill(self, sandbox_id: str) -> None:
        """Destroy it. Idempotent."""
        ...

    async def list(self, metadata: Mapping[str, str]) -> Sequence[SandboxInfo]: ...

    async def check(self) -> None:
        """Prove the key and reachability, or raise `AdapterUnavailable`."""
        ...

    async def session_verified(self, pin: str) -> bool:
        """Whether the build `pin` names passed the session smoke. Raises `SpawnError`
        (`CREDENTIAL`) when E2B refuses the key, `AdapterUnavailable` when it does not answer."""
        ...


class TerminalPty(Protocol):
    """A pty in a sandbox."""

    async def send(self, data: bytes) -> None: ...

    async def resize(self, cols: int, rows: int) -> None: ...

    async def kill(self) -> None:
        """Idempotent: a pty already gone is not an error."""
        ...

    async def wait(self) -> int:
        """Its process's exit status. Raises when the stream from the sandbox broke first."""
        ...


class TerminalSandbox(Protocol):
    async def pty_create(
        self,
        *,
        cols: int,
        rows: int,
        on_data: Callable[[bytes], None],
        user: str,
        timeout_seconds: float,
        envs: Mapping[str, str],
    ) -> TerminalPty:
        """A login bash on a new pty; E2B runs nothing else there. With `timeout_seconds=0`
        nothing but `kill()` ends it."""
        ...

    async def run(
        self, command: str, *, user: str, envs: Mapping[str, str], timeout_seconds: float
    ) -> CommandOutput:
        """Run to completion. A non-zero exit is returned, never raised."""
        ...


class TerminalSandboxClient(Protocol):
    """What the terminal gateway may do to a sandbox: no create, pause, kill, list or network
    call, and no way to wake a paused one. Raises `SandboxGone` or `SubstrateUnavailable`."""

    async def info(self, sandbox_id: str) -> SandboxInfo | None: ...

    async def connect_if_running(self, sandbox_id: str) -> TerminalSandbox | None:
        """A handle on a running sandbox, or `None`. Never the connect API, which resumes a
        paused sandbox and sets its expiry: a pty on a sandbox paused since fails instead."""
        ...


# --------------------------------------------------------------------------- the real client


def _classify(exc: BaseException) -> SpawnReason:
    """An E2B error as the contract's spawn vocabulary."""
    from e2b import exceptions

    if isinstance(exc, exceptions.AuthenticationException):
        return SpawnReason.CREDENTIAL
    if isinstance(exc, exceptions.NotFoundException | exceptions.InvalidArgumentException):
        return SpawnReason.CONFIG  # an unknown template or a bad option: only an operator fixes it
    return SpawnReason.INFRASTRUCTURE  # rate limits, sandbox limits, 5xx, timeouts


def substrate_failure(exc: BaseException) -> bool:
    """E2B or the network to it failed, not the caller: the SDK's service errors and transport
    errors. Anything else is a bug here and must surface as one."""
    import httpx
    from e2b import exceptions

    if _classify(exc) is not SpawnReason.INFRASTRUCTURE:
        return False
    return isinstance(
        exc,
        exceptions.SandboxException | exceptions.ServiceBusyException | httpx.TransportError,
    )


def _info(raw: Any) -> SandboxInfo:
    state = getattr(raw, "state", "running")
    return SandboxInfo(
        sandbox_id=raw.sandbox_id,
        state=str(getattr(state, "value", state)),
        metadata=dict(getattr(raw, "metadata", None) or {}),
        started_at=getattr(raw, "started_at", None),
    )


async def _get_info(sandbox_id: str, opts: Mapping[str, Any]) -> SandboxInfo | None:
    from e2b import AsyncSandbox, exceptions

    try:
        return _info(await AsyncSandbox.get_info(sandbox_id, **opts))
    except exceptions.NotFoundException:
        return None


class _E2bSandbox:
    def __init__(self, sandbox: AsyncSandbox) -> None:
        self._sandbox = sandbox

    @property
    def sandbox_id(self) -> str:
        return self._sandbox.sandbox_id

    async def run(
        self,
        command: str,
        *,
        user: str = "root",
        envs: Mapping[str, str] | None = None,
        timeout_seconds: float = 120,
    ) -> CommandOutput:
        from e2b import CommandExitException

        try:
            result = await self._sandbox.commands.run(
                command, user=user, envs=dict(envs or {}), timeout=timeout_seconds
            )
        except CommandExitException as exc:
            return CommandOutput(exc.exit_code, exc.stdout, exc.stderr)
        return CommandOutput(result.exit_code, result.stdout, result.stderr)

    async def start(
        self, command: str, *, user: str = "root", envs: Mapping[str, str] | None = None
    ) -> int:
        handle = await self._sandbox.commands.run(
            command, user=user, envs=dict(envs or {}), background=True, timeout=0
        )
        # The command outlives this handle; what it does is read from the files it writes.
        await handle.disconnect()
        return handle.pid

    async def pids(self) -> frozenset[int]:
        return frozenset(process.pid for process in await self._sandbox.commands.list())

    async def write_many(self, files: Mapping[str, bytes], *, user: str = "root") -> None:
        from e2b.sandbox.filesystem.filesystem import WriteEntry

        entries = [WriteEntry(path=path, data=data) for path, data in files.items()]
        await self._sandbox.files.write_files(entries, user=user)

    async def update_network(self, network: NetworkPolicy) -> None:
        await self._sandbox.update_network(network.as_sdk(create=False))  # type: ignore[arg-type]

    def host(self, port: int) -> str:
        return self._sandbox.get_host(port)


class E2bSandboxClient:
    """`SandboxClient` over the E2B SDK. `api_key` never enters a sandbox."""

    def __init__(self, api_key: str, *, request_timeout: float = 60) -> None:
        self._opts: dict[str, Any] = {"api_key": api_key, "request_timeout": request_timeout}

    async def create(self, options: SandboxOptions) -> Sandbox:
        from e2b import AsyncSandbox

        try:
            sandbox = await AsyncSandbox.create(
                template=options.template,
                timeout=options.timeout_seconds,
                metadata=dict(options.metadata),
                network=options.network.as_sdk(create=True),  # type: ignore[arg-type]
                lifecycle={"on_timeout": "kill", "auto_resume": False},
                **self._opts,
            )
        except Exception as exc:
            reason = _classify(exc)
            if reason is SpawnReason.INFRASTRUCTURE:
                raise SubstrateUnavailable(f"E2B could not create a sandbox: {exc}") from exc
            raise SpawnError(f"creating an E2B sandbox failed: {exc}", reason=reason) from exc
        return _E2bSandbox(sandbox)

    async def connect(self, sandbox_id: str, *, timeout_seconds: int) -> Sandbox:
        from e2b import AsyncSandbox, exceptions

        try:
            sandbox = await AsyncSandbox.connect(sandbox_id, timeout=timeout_seconds, **self._opts)
        except exceptions.NotFoundException as exc:
            raise SandboxGone(sandbox_id) from exc
        return _E2bSandbox(sandbox)

    async def info(self, sandbox_id: str) -> SandboxInfo | None:
        return await _get_info(sandbox_id, self._opts)

    async def pause(self, sandbox_id: str) -> None:
        from e2b import AsyncSandbox

        await AsyncSandbox.pause(sandbox_id, keep_memory=False, **self._opts)  # type: ignore[call-arg]

    async def kill(self, sandbox_id: str) -> None:
        from e2b import AsyncSandbox

        await AsyncSandbox.kill(sandbox_id, **self._opts)  # returns False when already gone

    async def list(self, metadata: Mapping[str, str]) -> Sequence[SandboxInfo]:
        from e2b import AsyncSandbox, SandboxQuery

        found: list[SandboxInfo] = []
        paginator = AsyncSandbox.list(query=SandboxQuery(metadata=dict(metadata)), **self._opts)
        while paginator.has_next:
            found += [_info(item) for item in await paginator.next_items()]
        return found

    async def check(self) -> None:
        from e2b import AsyncSandbox

        try:
            await AsyncSandbox.list(limit=1, **self._opts).next_items()
        except Exception as exc:
            detail = f"E2B is not reachable with the configured key: {exc}"
            raise AdapterUnavailable(detail) from exc

    async def session_verified(self, pin: str) -> bool:
        import httpx

        template, _, build_id = pin.rpartition(":")
        try:
            return build_id in await session_verdicts(self._opts["api_key"], template)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise SpawnError(
                    "E2B refused this runtime's key when asked for its build's session "
                    "verdict: store the team's E2B_API_KEY again on the runtime page",
                    reason=SpawnReason.CREDENTIAL,
                ) from exc
            raise AdapterUnavailable(
                f"E2B did not give build {pin}'s session verdict: HTTP "
                f"{exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterUnavailable(
                f"E2B did not give build {pin}'s session verdict: {type(exc).__name__}"
            ) from exc


@contextlib.contextmanager
def _terminal_errors(sandbox_id: str) -> Iterator[None]:
    from e2b import exceptions

    try:
        yield
    except exceptions.NotFoundException as exc:
        raise SandboxGone(sandbox_id) from exc
    except Exception as exc:
        # The SDK's transport raises the builtin ConnectionError when envd cannot be reached,
        # as for a sandbox paused since it was read.
        if substrate_failure(exc) or isinstance(exc, ConnectionError):
            raise SubstrateUnavailable(
                f"E2B did not answer for sandbox {sandbox_id}: {exc}"
            ) from exc
        raise


class _E2bPty:
    def __init__(self, sandbox: AsyncSandbox, handle: AsyncCommandHandle) -> None:
        self._sandbox = sandbox
        self._handle = handle

    async def send(self, data: bytes) -> None:
        with _terminal_errors(self._sandbox.sandbox_id):
            await self._sandbox.pty.send_stdin(self._handle.pid, data)

    async def resize(self, cols: int, rows: int) -> None:
        from e2b import PtySize

        with _terminal_errors(self._sandbox.sandbox_id):
            await self._sandbox.pty.resize(self._handle.pid, PtySize(rows=rows, cols=cols))

    async def kill(self) -> None:
        with _terminal_errors(self._sandbox.sandbox_id):
            await self._handle.kill()  # False when already gone

    async def wait(self) -> int:
        from e2b import CommandExitException

        try:
            return (await self._handle.wait()).exit_code
        except CommandExitException as exc:
            return exc.exit_code


class _E2bTerminalSandbox:
    def __init__(self, sandbox: AsyncSandbox) -> None:
        self._sandbox = sandbox

    async def pty_create(
        self,
        *,
        cols: int,
        rows: int,
        on_data: Callable[[bytes], None],
        user: str,
        timeout_seconds: float,
        envs: Mapping[str, str],
    ) -> TerminalPty:
        from e2b import PtySize

        with _terminal_errors(self._sandbox.sandbox_id):
            handle = await self._sandbox.pty.create(
                PtySize(rows=rows, cols=cols),
                on_data,
                user=user,
                envs=dict(envs),
                timeout=timeout_seconds,
            )
        return _E2bPty(self._sandbox, handle)

    async def run(
        self, command: str, *, user: str, envs: Mapping[str, str], timeout_seconds: float
    ) -> CommandOutput:
        from e2b import CommandExitException

        with _terminal_errors(self._sandbox.sandbox_id):
            try:
                result = await self._sandbox.commands.run(
                    command, user=user, envs=dict(envs), timeout=timeout_seconds
                )
            except CommandExitException as exc:
                return CommandOutput(exc.exit_code, exc.stdout, exc.stderr)
        return CommandOutput(result.exit_code, result.stdout, result.stderr)


class E2bTerminalClient:
    """`TerminalSandboxClient` over the E2B SDK, for the terminal gateway. It reads a sandbox with
    `GET /sandboxes/{id}` and builds the envd handle from that, as the SDK builds one from a
    connect: a connect resumes a paused sandbox, which E2B then kills with the workspace at the
    timeout the connect set, and a sandbox is created with `on_timeout: kill`."""

    def __init__(
        self,
        api_key: str,
        *,
        request_timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._opts: dict[str, Any] = {"api_key": api_key, "request_timeout": request_timeout}
        #: Tests replace the REST transport; envd calls never go through it.
        self._transport = transport

    async def info(self, sandbox_id: str) -> SandboxInfo | None:
        detail = await self._detail(sandbox_id)
        return None if detail is None else _info(detail)

    async def connect_if_running(self, sandbox_id: str) -> TerminalSandbox | None:
        from e2b import AsyncSandbox, ConnectionConfig
        from packaging.version import Version

        detail = await self._detail(sandbox_id)
        if detail is None or str(getattr(detail.state, "value", detail.state)) != "running":
            return None
        token = detail.envd_access_token if isinstance(detail.envd_access_token, str) else None
        headers = {
            "E2b-Sandbox-Id": detail.sandbox_id,
            "E2b-Sandbox-Port": str(ConnectionConfig.envd_port),
            **({"X-Access-Token": token} if token is not None else {}),
        }
        sandbox = AsyncSandbox(
            sandbox_id=detail.sandbox_id,
            sandbox_domain=detail.domain if isinstance(detail.domain, str) else None,
            envd_version=Version(detail.envd_version),
            envd_access_token=token,
            traffic_access_token=None,
            connection_config=ConnectionConfig(extra_sandbox_headers=headers, **self._opts),
        )
        return _E2bTerminalSandbox(sandbox)

    async def _detail(self, sandbox_id: str) -> SandboxDetail | None:
        from e2b import ConnectionConfig
        from e2b.api import AsyncApiClient, handle_api_exception
        from e2b.api.client.api.sandboxes import get_sandboxes_sandbox_id
        from e2b.api.client.models import Error
        from e2b.api.client_async import get_api_client
        from e2b.exceptions import SandboxException

        config = ConnectionConfig(**self._opts)
        client = (
            get_api_client(config)
            if self._transport is None
            else AsyncApiClient(config, transport=self._transport)
        )
        with _terminal_errors(sandbox_id):
            res = await get_sandboxes_sandbox_id.asyncio_detailed(sandbox_id, client=client)
            if res.status_code == 404:
                return None
            if res.status_code >= 300:
                raise handle_api_exception(res)
            if res.parsed is None or isinstance(res.parsed, Error):
                raise SandboxException(f"E2B answered GET /sandboxes/{sandbox_id} with no detail")
            return res.parsed


@dataclass(frozen=True)
class TemplateBuild:
    """One finished build of a template in an E2B team: what a runtime of that team may boot."""

    template: str
    build_id: str
    created_at: datetime
    cpu_count: int
    memory_mb: int
    #: Whether it passed the session smoke: its `verdict_tag` names it.
    verified: bool = False

    @property
    def pin(self) -> str:
        return f"{self.template}:{self.build_id}"


#: Seconds for each E2B call the listing makes.
_LISTING_TIMEOUT: Final = 20.0


def verdict_tag(build_id: str) -> str:
    """The E2B tag that records a build passed the session smoke. One per build, so a later
    build of the same template never takes an earlier one's verdict away."""
    return f"interactive-ok-{build_id}"


def verified_builds(tags: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    """The build ids a template's tags verify: a verdict counts only on the build it names."""
    verified: set[str] = set()
    for tag in tags:
        build_id = str(tag.get("buildID", ""))
        if tag.get("tag") == verdict_tag(build_id):
            verified.add(build_id)
    return frozenset(verified)


@contextlib.asynccontextmanager
async def _e2b_api(api_key: str) -> AsyncIterator[httpx.AsyncClient]:
    import httpx
    from e2b import ConnectionConfig

    base = ConnectionConfig(api_key=api_key).api_url
    async with httpx.AsyncClient(
        base_url=base, headers={"X-API-Key": api_key}, timeout=_LISTING_TIMEOUT
    ) as http:
        yield http


async def _verdicts(http: httpx.AsyncClient, template: str) -> frozenset[str]:
    response = await http.get(f"/templates/{quote(template, safe='')}/tags")
    if response.status_code == 404:
        return frozenset()  # the template is gone, so none of its builds can boot
    return verified_builds(response.raise_for_status().json())


def _http_failure(exc: httpx.HTTPError) -> str:
    """The failure without its text, which can carry the request's headers."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


async def session_verdicts(api_key: str, template: str) -> frozenset[str]:
    """The ids of `template`'s builds that passed the session smoke."""
    async with _e2b_api(api_key) as http:
        return await _verdicts(http, template)


async def record_session_verdict(api_key: str, pin: str) -> None:
    """Tag the build `pin` names as having passed the session smoke."""
    from e2b import AsyncTemplate

    build_id = pin.rpartition(":")[2]
    info = await AsyncTemplate.assign_tags(pin, [verdict_tag(build_id)], api_key=api_key)
    if info.build_id != build_id:
        raise RuntimeError(f"E2B tagged build {info.build_id}, not {build_id}")


async def template_builds(api_key: str) -> list[TemplateBuild]:
    """Every ready build of every template in the key's E2B team, newest first (the most recent
    100 per template), each with its session verdict. Over E2B's REST API: the SDK builds
    templates but does not list them."""
    import httpx

    async with _e2b_api(api_key) as http:
        templates = (await http.get("/templates")).raise_for_status().json()
        # Bounded so a burst cannot draw E2B's rate limit, which would quietly read builds
        # unverified.
        sem = asyncio.Semaphore(8)

        async def _tags(name: str, template_id: str) -> frozenset[str]:
            try:
                return await _verdicts(http, template_id)
            except httpx.HTTPError as exc:
                # The listing still offers the builds; only the gate must read tags strictly.
                log.warning(
                    "reading template %s's tags failed (%s), so its builds read unverified",
                    name,
                    _http_failure(exc),
                )
                return frozenset()

        async def _builds(template: dict[str, Any]) -> list[TemplateBuild]:
            # `names` holds `<team>/<alias>` once a team has a namespace; a pin names the alias.
            names = [name.rsplit("/", 1)[-1] for name in template.get("names") or []]
            name = next(iter(names or template.get("aliases") or []), "")
            if not name:
                return []
            async with sem, asyncio.TaskGroup() as pair:
                detail = pair.create_task(
                    http.get(f"/templates/{template['templateID']}", params={"limit": 100})
                )
                tags = pair.create_task(_tags(name, template["templateID"]))
            verified = tags.result()
            return [
                TemplateBuild(
                    template=name,
                    build_id=str(build["buildID"]),
                    created_at=datetime.fromisoformat(build["createdAt"]),
                    cpu_count=int(build["cpuCount"]),
                    memory_mb=int(build["memoryMB"]),
                    verified=str(build["buildID"]) in verified,
                )
                for build in detail.result().raise_for_status().json().get("builds", [])
                if build.get("status") == "ready"
            ]

        # A task group, so one failed template cancels the others' requests before the client
        # closes.
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(_builds(template)) for template in templates]
        except* Exception as failed:
            raise _first_leaf(failed) from None
    found = [build for task in tasks for build in task.result()]
    return sorted(found, key=lambda build: build.created_at, reverse=True)


def _first_leaf(group: BaseExceptionGroup[Exception]) -> Exception:
    """The first plain exception in a task group's failure, so a caller sees what failed."""
    failure: BaseException = group
    while isinstance(failure, BaseExceptionGroup):
        failure = failure.exceptions[0]
    assert isinstance(failure, Exception)
    return failure

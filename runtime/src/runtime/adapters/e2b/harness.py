"""The harness seam: what differs per agent CLI inside a sandbox.

A harness does no I/O. It turns a `RunSpec` into files, argv, env and egress for the CLI's TUI
(decision 56), and its session log into facts through the same translator and `StreamReader` its
host adapter uses. The sandbox work — create, clone, launch, poll, pause, kill — is the
adapter's, and the same for every CLI.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

from runtime.adapters.e2b.layout import AGENT_USER, ATTACHMENTS_DIR, CONFIG_DIR
from runtime.contract import (
    McpServerSpec,
    McpTransport,
    MissingAttachment,
    RunAttachment,
    RunSpec,
    RunUsage,
    SpawnError,
    SpawnReason,
)

if TYPE_CHECKING:
    from runtime.adapters._cli.session_reader import LogSignals, SessionTranscript
    from runtime.adapters._cli.session_signals import CostState


@dataclass(frozen=True, slots=True)
class Launch:
    """One start of the agent: its argv and environment, and the files it reads.

    `files` maps an absolute sandbox path to (content, owner, mode). `inject` is the header set
    the egress proxy adds per host: the credentials the sandbox never holds.
    """

    argv: tuple[str, ...]
    env: Mapping[str, str] = field(repr=False)
    files: Mapping[str, tuple[bytes, str, int]] = field(default_factory=dict, repr=False)
    inject: Mapping[str, Mapping[str, str]] = field(default_factory=dict, repr=False)


class Reader(Protocol):
    """What the adapter reads off a harness's `StreamReader`."""

    session_id: str | None

    def feed(self, line: str) -> bool: ...

    def over_cap(self, cap: int | None) -> bool: ...

    @property
    def turns(self) -> int: ...

    @property
    def live_usage(self) -> RunUsage | None: ...


@dataclass(frozen=True, slots=True)
class Outcome:
    """A finished segment's verdict and figures, as its stream reported them."""

    error: str | None
    summary: str
    turns: int
    cost_usd: Decimal | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    output_tokens: int | None = None
    stream_warnings: tuple[str, ...] = ()


class Harness(Protocol):
    """One agent CLI, whose TUI runs in the sandbox's tmux (decision 56)."""

    @property
    def name(self) -> str:
        """The CLI's name, for messages."""
        ...

    @property
    def label(self) -> str:
        """The CLI as a person names it, for a run's failure reason."""
        ...

    @property
    def pinned_version(self) -> str:
        """The version the host parsers were last hand-checked against. A template installs the
        CLI's latest at build time instead, and its builder's smoke checks the parse."""
        ...

    @property
    def egress_hosts(self) -> tuple[str, ...]:
        """Hosts the CLI must reach: its vendor API."""
        ...

    @property
    def credential_paths(self) -> tuple[str, ...]:
        """Files that hold a credential and must not survive a pause."""
        ...

    def validate(self, spec: RunSpec, auth_mode: str) -> None:
        """Refuse what the CLI cannot run, before anything is created."""
        ...

    def reader(self, prior_turns: int) -> Reader:
        """Counts the translated stream's turns and tokens, from the turns before this segment."""
        ...

    @property
    def transcript_version(self) -> int:
        """Its translator's version: a restart replays only through the one that wrote."""
        ...

    @property
    def log_signals(self) -> LogSignals:
        """What its session log says to `SessionSignals` when a hook did not fire."""
        ...

    @property
    def turnless_warnings(self) -> frozenset[str]:
        """Its reader's words for a segment with no turn in it."""
        ...

    @property
    def chooses_session_id(self) -> bool:
        """Whether a spawn names the session, so its log's path is known before the CLI runs."""
        ...

    @property
    def finish_script(self) -> str:
        """The script that quits its TUI, or hangs it up (`layout.finish_sh`)."""
        ...

    @property
    def turnless_hook(self) -> bool:
        """Whether a launch with no turn fires a hook once it reaches its prompt. Codex fires
        none until a turn starts, so its consent is the word that it waits there."""
        ...

    @property
    def sign_in(self) -> tuple[str, ...]:
        """What a launch shows instead of its session when the CLI refused the login it holds."""
        ...

    @property
    def consent_script(self) -> str | None:
        """What every launch runs once the TUI is up, before anything else reaches it: the
        screen the CLI asks on first, taken. None when it asks nothing."""
        ...

    @property
    def launch_log(self) -> str | None:
        """The TUI's own log, the agent's file, whose end a launch that does not reach its
        prompt carries in its error. None when it keeps none."""
        ...

    def session_flags(self, spec: RunSpec) -> tuple[str, ...]:
        """The options every launch of the session repeats, which a resume needs again."""
        ...

    def session_spawn(
        self, spec: RunSpec, secrets: Mapping[str, str], auth_mode: str, session_id: str
    ) -> Launch:
        """The first launch: `files` carries the turn file, and the seeds are added once the
        checkout's real path is known (`session_seeds`)."""
        ...

    def session_seeds(
        self,
        workspace_real: str,
        config_real: str,
        auth_mode: str,
        mcp: tuple[McpServerSpec, ...],
        system: str | None,
    ) -> dict[str, tuple[bytes, str, int]]:
        """The CLI's config documents, written at every launch, naming the checkout and the config
        dir by their resolved paths. `system` is the system prompt, for a CLI that takes it as
        config: a spawn and an adoption pass it, a resume None."""
        ...

    def session_adopt(
        self,
        spec: RunSpec,
        secrets: Mapping[str, str],
        auth_mode: str,
        session_id: str,
        segment: int,
    ) -> Launch:
        """A rework's launch of an earlier run's session: the spec's files as the first launch
        writes them, its prompt as the segment's turn file, and the CLI resumed on the session."""
        ...

    def session_resume(
        self,
        flags: tuple[str, ...],
        session_id: str,
        secrets: Mapping[str, str],
        auth_mode: str,
        mcp: tuple[McpServerSpec, ...],
        turn: str | None,
        segment: int,
        attachments: tuple[RunAttachment, ...],
        missing: tuple[MissingAttachment, ...],
    ) -> Launch:
        """A later launch of the same session, every header rendered again from fresh secrets.
        With a turn, `files` carries it as the segment's turn file, which the prompt points at
        with its images; with none, the CLI comes back at its prompt with nothing typed."""
        ...

    def session_view(
        self,
        flags: tuple[str, ...],
        session_id: str,
        secrets: Mapping[str, str],
        auth_mode: str,
        *,
        workspace_real: str,
        config_real: str,
    ) -> Launch:
        """The session's CLI resumed at its prompt for a person to read (decision 57): the model
        credential alone, no MCP server and no turn, its hooks appending nowhere (`VIEW_SIGNALS`),
        and its config documents in `files`. `SpawnError` when `secrets` lacks the credential."""
        ...

    def session_log(self, session_id: str) -> str | None:
        """Where the CLI writes the session's log, in the sandbox; None when the CLI names it
        only once it runs, so a spawn finds it and the run's state records it."""
        ...

    def session_found(self, log: str, first_line: str) -> str | None:
        """The session of a log a spawn found (`ROLLOUT_FIND`), when its name and its first line
        agree on it; None when they do not. Asked only of a CLI that names its own session."""
        ...

    def transcript(
        self, session_id: str, *, turn: bool, cost_base: CostState | None, skip: int
    ) -> SessionTranscript:
        """The log's translator into the stream `reader` reads, for one segment."""
        ...

    def session_outcome(
        self, reader: Reader, exit_code: int | None, stderr: str, *, summary: str | None
    ) -> Outcome:
        """A segment's figures, and the error its stream reports. Its exit and its turns'
        failures are the session's to judge. `summary` is the last `Stop` hook's closing words,
        for a segment whose stream carries none."""
        ...

    def excused(self, transcript: SessionTranscript) -> frozenset[str]:
        """Its reader's warnings that a segment's log explains, read off the segment's
        translator once the segment ended."""
        ...


def mcp_headers_by_host(
    servers: Iterable[McpServerSpec], secrets: Mapping[str, str]
) -> dict[str, dict[str, str]]:
    """Each HTTP server's rendered headers, keyed by the host the proxy sets them for.

    Two servers on one host with different headers cannot both be served by a per-host rule, so
    that is refused rather than letting one server's credential reach the other.
    """
    by_host: dict[str, dict[str, str]] = {}
    for server in servers:
        headers = dict(server.rendered_headers(secrets))
        if not headers:
            continue
        host = urlsplit(server.url or "").hostname or ""
        if host in by_host and by_host[host] != headers:
            raise SpawnError(
                f"two MCP servers on {host} carry different headers; the sandbox's egress proxy "
                "sets headers per host, so it cannot keep them apart",
                reason=SpawnReason.CONFIG,
            )
        by_host[host] = headers
    return by_host


def require_secret(spec: RunSpec, name: str) -> None:
    if not spec.secrets.get(name):
        raise _no_secret(name)


def require_value(secrets: Mapping[str, str], name: str) -> None:
    if not secrets.get(name):
        raise _no_secret(name)


def _no_secret(name: str) -> SpawnError:
    return SpawnError(
        f"No {name}. Store it on the Runtime page, for the runtime this process names.",
        reason=SpawnReason.CREDENTIAL,
    )


def attachment_paths(attachments: Sequence[RunAttachment]) -> dict[str, str]:
    return {item.name: f"{ATTACHMENTS_DIR}/{item.filename}" for item in attachments}


def attachment_files(attachments: Sequence[RunAttachment]) -> dict[str, tuple[bytes, str, int]]:
    """Each image where the agent can read or copy it."""
    return {
        f"{ATTACHMENTS_DIR}/{item.filename}": (item.content, AGENT_USER, 0o600)
        for item in attachments
    }


def skill_files(spec: RunSpec) -> dict[str, tuple[bytes, str, int]]:
    """Each skill as the CLI reads it from its config dir, the agent's own."""
    return {
        f"{CONFIG_DIR}/skills/{skill.name}/SKILL.md": (skill.content.encode(), AGENT_USER, 0o600)
        for skill in spec.skills
    }


def reject_stdio(spec: RunSpec) -> None:
    stdio = sorted(s.name for s in spec.mcp if s.transport is McpTransport.STDIO)
    if stdio:
        # Decision 25: a sandbox would have to fetch their packages at run time.
        raise SpawnError(
            f"The stdio MCP server(s) {', '.join(stdio)} can't run in a cloud sandbox. Run "
            "this agent on a host runtime, or move the server(s) to `http`.",
            reason=SpawnReason.CONFIG,
        )

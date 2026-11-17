"""The E2B substrate: one sandbox per run, any agent CLI through a `Harness` (decision 25), its
TUI in a root tmux server, supervised by polling its hook signals and its session log (decision
56).

The sandbox is the run's source of truth, and the worker keeps mirrors under `<state_root>/runs/
<run_id>/` beside `state.json`. Each poll is one `SESSION_POLL`, reading both files from the
offsets in `state.json`, and only whole lines: a line still being written is read again, whole,
next poll, so the same loop serves a fresh spawn and a worker that restarted mid-run. Each file
is mirrored byte for byte (`signals.jsonl`, `session.jsonl`). The signals feed `SessionSignals`,
whose facts are the run's RUNNING or IDLE; the log goes through the harness's translator into the
translated mirror (`stdout.jsonl`), which `logs()` serves, so the API never needs the E2B key,
and which the harness's reader counts. Both files are the agent's to write, so they are data:
nothing in them is executed, and only what the translators extract reaches the mirror or the
state.

The adapter never times out an idle session: the worker owns its active time and its rollover.
The one backstop is E2B's cap. A minute before it the session is hung up and the run parked
INTERRUPTED, so E2B's own timeout never destroys the workspace.

`finish` ends a session that met its postconditions: the CLI's quit at a turn's end, a hangup on
a question, where typing would answer it. `interrupt` and `cancel` hang it up. `resume` launches
the CLI again on the session in the same sandbox: a new segment of the same session, translated
from where the log stands, its cost counted from the running total.

Codex takes no session id: a spawn waits for the rollout it begins (`ROLLOUT_FIND`) and records
its thread and path. Every codex launch first asks for the folder (`CONSENT`), and fires no hook
until a turn starts, so a resume with no turn is known to idle at its prompt from its consent.

A rework's or a retry's spawn with `resume_from` takes the earlier run's kept sandbox over
(`_adopt`): for a rework the checkout moves to the pull request's head, its uncommitted work
stashed; for a retry it stays as the failed attempt left it, still on its branch, since a run's
end leaves its own sandbox's HEAD where it was. The session goes on under the new run, whose
transcript, turns and cost start where the earlier run's ended. A checkout on no branch is never
continued (`NoBranchHeld`). A ready preview serves on as the new run's.

A run is terminal when its agent stops and the mirror is complete. Every run's sandbox is parked
(`_park`), or scrubbed and left awake to a preview still serving in it (`_end_run`), before its
status is written, whatever the status, so INTERRUPTED always means resumable and a kept sandbox
never holds a credential (decision 54). A sandbox is killed only when its park fails; the reaper
deletes the rest once the orchestrator no longer keeps them, and parks again a kept one it finds
running with nothing awake in it.

A kept sandbox wakes for a preview, a session view or a resume, and may hold more than one at
once: every credential rule is written with the preview's uid contained by the firewall, so a
view or a live session can run beside an awake preview. A run's or a view's end leaves the
sandbox to whatever still runs in it; only a renewal (a rollover, E2B's cap, or a resume, rework
or view the preview's wake left too little of the hour for, `_renew_for`) parks it under an awake
preview, which the worker then starts again. A rework that takes the sandbox over takes a ready
preview with it (`_adopt`). A public sandbox admits only its preview's ports (design.md
§7.10). A run's end and a preview's start, end or park each decide the sandbox's fate under the
run's one lock (`_fate`), so none of them parks or scrubs a sandbox another has just parked.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import shutil
import uuid
from collections.abc import AsyncIterator, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, Final
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from runtime.adapters._cli import mirror
from runtime.adapters._cli.environment import (
    GITHUB_KEY,
    GLAB_QUIET_ENV,
    agent_git_identity,
    config_env,
    config_gone,
    git_env,
    pinned_config_sha,
    refuse_attachments,
    refuse_blind,
    view_git_env,
)
from runtime.adapters._cli.mirror import MAX_LINE, READ_LIMIT, TRANSLATOR_CHANGED
from runtime.adapters._cli.paths import path_slug, utc_now
from runtime.adapters._cli.probes import ENV_BOT_LOGIN, discover_artifacts, expectation_verdict
from runtime.adapters._cli.process import Cli
from runtime.adapters._cli.rework import retry_header, rework_header
from runtime.adapters._cli.session_reader import (
    SessionReader,
    SessionTranscript,
    TornPoll,
    login_refused,
    turn_cap_reason,
)
from runtime.adapters._cli.session_signals import CostState, SessionSignals
from runtime.adapters._cli.session_state import drop_retired
from runtime.adapters.claude_cli.stream import add, merge_warnings
from runtime.adapters.e2b.client import (
    CommandOutput,
    E2bSandboxClient,
    NetworkPolicy,
    Sandbox,
    SandboxClient,
    SandboxGone,
    SandboxInfo,
    SandboxOptions,
    substrate_failure,
)
from runtime.adapters.e2b.harness import Harness, Launch, Outcome, Reader
from runtime.adapters.e2b.layout import (
    AGENT_HOME,
    AGENT_USER,
    ARGV,
    CAPTURE,
    CONFIG_DIR,
    CONSENT_NOT_ASKED,
    CONSENT_NOT_TAKEN,
    END_SHELL,
    ENV,
    EXIT_CODE,
    FIREWALL,
    INSPECT,
    INSPECT_SESSION,
    INSPECT_STATUS,
    INSPECT_STOP,
    MODES,
    PREVIEW,
    PREVIEW_ENDED,
    PREVIEW_ENV,
    PREVIEW_HOME,
    PREVIEW_SERVICES,
    PREVIEW_SETUP,
    PREVIEW_SETUP_ENV,
    PREVIEW_STATUS,
    PREVIEW_STOP,
    PREVIEW_USER,
    REALPATHS,
    REWORK,
    REWORK_BRANCH_GONE,
    REWORK_NO_BRANCH,
    REWORK_NOT_THE_BRANCH,
    ROLLOUT_FIND,
    SCRIPTS,
    SCRUB,
    SESSION,
    SESSION_POLL,
    SETUP,
    SETUP_CONFIG_FETCH_FAILED,
    STDERR,
    STOP,
    TMUX_CONF,
    TMUX_SESSION,
    TMUX_SOCKET,
    VIEW_ARGV,
    VIEW_ENV,
    VIEW_STDERR,
    SessionPoll,
    read_session_poll,
    service_log,
    turn_file,
)
from runtime.contract import (
    PREVIEW_CONTINUED,
    PREVIEW_IDLE,
    PREVIEW_RENEWAL,
    AdapterUnavailable,
    AgentRuntimeError,
    Artifact,
    ExpectedArtifact,
    IdleReason,
    InspectLoginRefused,
    InspectNoSession,
    InspectPhase,
    InspectState,
    InspectUnavailable,
    McpServerSpec,
    MissingAttachment,
    NoBranchHeld,
    ParkReason,
    PreviewPhase,
    PreviewServiceSpec,
    PreviewSpec,
    PreviewState,
    PreviewUnavailable,
    PreviewUrl,
    ResumeFrom,
    ResumeMode,
    RunAttachment,
    RunHandle,
    RunResult,
    RunSpec,
    RunStatus,
    RunUsage,
    SecretRoute,
    SessionState,
    SessionUnavailable,
    SpawnError,
    SpawnReason,
    SubstrateUnavailable,
    TerminalTarget,
    UnknownRunError,
)
from runtime.terminal import TMUX_LOCKDOWN

log = logging.getLogger(__name__)

ENV_GH_TOKEN: Final = "GH_TOKEN"
ENV_GIT_HOST: Final = "GIT_HTTP_HOST"
#: Handed to `gh` so it believes it is logged in; the proxy sets the real header.
PLACEHOLDER_GH_TOKEN: Final = "ghs_injected_at_egress"
#: The same for `glab` on a GitLab host.
PLACEHOLDER_GITLAB_TOKEN: Final = "glpat-injected-at-egress"
#: What a routed project secret's variable holds; the proxy sets the real header.
PLACEHOLDER_ROUTED: Final = "factorial-injected-at-egress"
GITHUB_HOSTS: Final = (
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "*.githubusercontent.com",
)

#: The Hobby plan's session cap; a pause and resume starts a new one.
MAX_SESSION: Final = timedelta(hours=1)
#: Left of the session for the stop, the final read and the probes before E2B kills the sandbox.
SESSION_MARGIN: Final = timedelta(minutes=5)
#: Less of the window than this left, and a session resumed, a rework's included, or a view opened
#: in a sandbox something keeps awake would barely start before it ended, so the sandbox is
#: renewed first (`_renew_for`).
#: More, and they take what is left rather than stop the preview: the worker rolls a session over
#: five minutes before its cap, though not one resumed that close to it, and a view ends with the
#: window, which the preview's next read renews.
RENEW_FLOOR: Final = timedelta(minutes=15)
POLL_INTERVAL: Final = 2.0
#: Why the session cap's backstop parked a run the worker did not roll over.
SESSION_CAP_REASON: Final = (
    "Paused at the sandbox's time limit before its token could be renewed. Its sandbox is kept "
    "for a resume."
)
#: The raw mirrors, beside the translated one.
SESSION_MIRROR: Final = "session.jsonl"
SIGNALS_MIRROR: Final = "signals.jsonl"
#: How long before E2B's cap the adapter parks a session the worker did not roll over.
CAP_BACKSTOP: Final = timedelta(seconds=60)
#: Reads after the CLI is gone, at most: an agent process can keep appending to its files.
FINAL_READS: Final = 8
#: finish.sh's worst case: the `/exit`, its wait, the hangup's grace and STOP's.
FINISH_TIMEOUT: Final = 90
#: How long rework.sh's fetch, stash and reset may take.
REWORK_TIMEOUT: Final = 300
#: rework.sh's report: `<branch> <head>`, then `stash <sha>` when it stashed, then `lost <sha>`
#: when the old HEAD held commits the PR head does not.
_REWORKED: Final = re.compile(
    r"(\S+) ([0-9a-f]{40,64})\n(?:stash ([0-9a-f]{40,64})\n)?(?:lost ([0-9a-f]{40,64})\n)?"
)
#: Its report for a retry, which moves nothing: `<branch> <head>`.
_RETRIED: Final = re.compile(r"(\S+) ([0-9a-f]{40,64})\n")
#: How long a spawn waits for a CLI that names its own session to begin its log, and how often
#: it looks.
SESSION_ID_WAIT: Final = 30.0
SESSION_ID_POLL: Final = 0.25
#: The consent script's worst case: its two waits and the tmux calls between them.
CONSENT_TIMEOUT: Final = 60
#: How many characters of the end of the TUI's own log a launch's error carries.
LAUNCH_LOG_TAIL: Final = 1000
#: The CLIs' first-run screens, which the seeds answer: any of them on screen means one is wrong.
#: Not codex's folder consent ("Open restricted"), which every launch puts up and `CONSENT` takes.
DIALOGS: Final = re.compile(
    r"trust this folder|Do you want to use this API key|Bypass Permissions mode|"
    r"Choose the text style|Select login method|Please run /login|security notes|"
    r"Hooks need review|Sign in with ChatGPT|Provide your own API key|Update available|"
    r"Codex just got an upgrade|no complete local package",
    re.IGNORECASE,
)
#: A sandbox younger than this may belong to a spawn whose run id is not recorded yet.
REAP_GRACE: Final = timedelta(minutes=15)
#: Consecutive failed polls before the supervisor asks whether the sandbox still exists.
POLL_FAILURES_BEFORE_CHECK: Final = 5
#: How long a stop waits for the supervisor to finalise the run and park or kill its sandbox.
STOP_WAIT: Final = 120.0
#: Ready checks a READY preview may miss in a row, one per poll, before it counts as failed.
PREVIEW_MISSES_BEFORE_FAILED: Final = 3
#: Past its limit by this much, a view the worker has not stopped is parked by its own status
#: read, still well before E2B's timeout would kill the sandbox with the workspace.
INSPECT_LIMIT_SLACK: Final = timedelta(minutes=1)
#: The timeout a stray is connected with, long enough for its park.
STRAY_PARK_SECONDS: Final = 300
SUBSTRATE: Final = "e2b"

_REPO: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+")
#: A ref git would read as an option, or one carrying whitespace or control characters, is never
#: a branch or a sha anyone meant.
_BAD_REF: Final = re.compile(r"^-|[\s\x00-\x1f\x7f]")
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f-\x9f]")
#: Environment the sandbox's own processes need that `env -i` in the runner would otherwise drop:
#: the path and E2B's proxy and CA settings. Read from the template at setup, before the agent
#: exists, so the agent cannot have shaped it.
_SANDBOX_ENV: Final = frozenset(
    {
        "PATH",
        "NODE_EXTRA_CA_CERTS",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "GIT_SSL_CAINFO",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)

STATE_FILE: Final = "state.json"
MIRROR_FILE: Final = "stdout.jsonl"

#: Every harness's credential files: the reaper's adapter parks the sandboxes of every kind.
_CREDENTIAL_PATHS: set[str] = set()
#: Sandboxes an adapter in this process woke and has not parked again, and a lock per sandbox.
#: The reaper's adapters are other instances than the ones serving runs, in the same worker
#: (decision 40), so they meet here: a stray is parked only under its lock, and only when no
#: adapter holds it.
_HELD: set[str] = set()
_LOCKS: dict[str, asyncio.Lock] = {}


def _lock(sandbox_id: str) -> asyncio.Lock:
    return _LOCKS.setdefault(sandbox_id, asyncio.Lock())


def _release(sandbox_id: str) -> None:
    """Nothing holds the sandbox any more: its hold goes, and its lock unless one is taken."""
    _HELD.discard(sandbox_id)
    lock = _LOCKS.get(sandbox_id)
    if lock is not None and not lock.locked():
        del _LOCKS[sandbox_id]


class _Preview(BaseModel):
    """A kept run's preview, on the worker's disk beside the run. It never holds a secret."""

    model_config = ConfigDict(extra="forbid")

    token: str
    #: The start script's, None until it runs: the record is written first, with the ports the
    #: firewall contains, so a worker that dies in between leaves no preview it does not know of.
    pid: int | None = None
    phase: PreviewPhase
    services: tuple[PreviewServiceSpec, ...]
    started_at: datetime
    #: When it closes for idle: `stop_after` from the start, until the worker gives a
    #: `keep_until`.
    deadline: datetime
    #: The end the preview last asked E2B's timeout for, at least `SESSION_MARGIN` past its
    #: deadline, so that E2B's own timeout never kills the sandbox and the kept workspace with it.
    #: A later deadline asks again; the cap's park comes first.
    timeout_until: datetime | None = None
    #: When setup was first seen done: each service's ready timeout counts from here.
    serving_since: datetime | None = None
    urls: tuple[PreviewUrl, ...] = ()
    detail: str | None = None
    head_sha: str | None = None
    uncommitted: bool = False
    #: Ready checks missed in a row since the preview became READY.
    misses: int = 0

    def state(self) -> PreviewState:
        return PreviewState(
            phase=self.phase,
            token=self.token,
            urls=self.urls,
            detail=self.detail,
            head_sha=self.head_sha,
            uncommitted=self.uncommitted,
        )


class _Inspect(BaseModel):
    """A kept session's view, on the worker's disk beside the run. It never holds a secret."""

    model_config = ConfigDict(extra="forbid")

    token: str
    #: The start script's, None until it runs: the record is written first, so a worker that
    #: dies in between still ends the view before a resume writes a credential.
    pid: int | None = None
    phase: InspectPhase
    started_at: datetime
    #: `limit` from the start: E2B's timeout falls `SESSION_MARGIN` after it.
    deadline: datetime
    clients: int = 0
    #: The later of tmux's last activity and the last status read that found a client attached.
    last_activity: datetime | None = None
    detail: str | None = None
    #: The session log's size before the view's launch, which its end cuts the log back to;
    #: None when there was no log.
    log_size: int | None = None

    def state(self, sandbox_id: str) -> InspectState:
        if self.phase is not InspectPhase.OPEN:
            return InspectState(
                phase=self.phase,
                token=self.token,
                last_activity=self.last_activity,
                started_at=self.started_at,
                detail=self.detail,
            )
        target = TerminalTarget(
            substrate=SUBSTRATE, socket=TMUX_SOCKET, session=INSPECT_SESSION, sandbox_id=sandbox_id
        )
        return InspectState(
            phase=self.phase,
            token=self.token,
            target=target,
            clients=self.clients,
            last_activity=self.last_activity,
            started_at=self.started_at,
        )


_RETIRED_KEYS: Final = frozenset({"interactive", "timeout_seconds", "session_deadline"})


class _RunState(BaseModel):
    """The run's whole life, on the worker's disk. It never holds a secret."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    sandbox_id: str
    status: RunStatus
    agent: str
    ticket: str
    repo: str
    branch: str
    auth_mode: str
    #: When the current segment's agent started, and E2B's session cap on it, from the sandbox's
    #: creation or its last wake. The worker, not the adapter, holds the session to the step's
    #: timeout.
    started_at: datetime
    deadline: datetime
    pid: int | None = None
    session_id: str | None = None
    max_turns: int | None = None
    #: Turns of the segments before this one; the segment's own are added at its end.
    turns: int = 0
    #: Bytes of the translated mirror, and where the current segment's lines begin; the session
    #: log has offsets of its own.
    offset: int = 0
    segment_start: int = 0
    exit_code: int | None = None
    summary: str = ""
    failure_reason: str | None = None
    cost_usd: Decimal | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    output_tokens: int | None = None
    artifacts: tuple[Artifact, ...] = ()
    stream_warnings: tuple[str, ...] = ()
    expected: tuple[ExpectedArtifact, ...] = ()
    #: What a resume repeats: the session's flags (no secret is ever an argument), the declared
    #: MCP servers (header templates, never values), the egress hosts, and which project
    #: secrets ride the proxy or the env file (names, never values).
    spawn_argv: tuple[str, ...] = ()
    mcp: tuple[McpServerSpec, ...] = ()
    allow: tuple[str, ...] = ()
    secret_routes: tuple[SecretRoute, ...] = ()
    secret_env: tuple[str, ...] = ()
    agent_env: dict[str, str] = {}
    #: The forge login the run was last handed (`ENV_BOT_LOGIN`), which its commits name and
    #: its comment probe fences on when a resume's secrets leave it out.
    bot_login: str = ""
    #: Created with public inbound traffic, for a preview (decision 54).
    public: bool = False
    #: The preview's declared ports, which the inbound rule admits (design.md §7.10).
    public_ports: tuple[int, ...] = ()
    #: Its sandbox is parked holding the workspace, for a preview or a resume to wake.
    kept: bool = False
    #: When the sandbox last woke from a pause, or was created: E2B's session cap counts from it,
    #: so it must pause again within `max_session` (a renewal). A park clears it; the reaper's park
    #: is never written back, so a paused sandbox can still show an old one, which a wake from
    #: that pause disregards (`_waking`).
    awake_since: datetime | None = None
    preview: _Preview | None = None
    inspect: _Inspect | None = None
    #: A stop someone asked for, written before STOP is sent: a worker that restarts in between
    #: must not read the agent's exit as a failure.
    stopping: RunStatus | None = None

    #: Launches of the session's CLI in its sandbox so far, a continued run's included:
    #: `turns/<n>.md` is the next one's turn file, so no launch overwrites an earlier one's.
    segments: int = 0
    #: Whether the current launch carried the orchestrator's turn.
    segment_turn: bool = False
    #: Where this run's slice of the sandbox's signals file starts, and how far it is read.
    signals_start: int = 0
    signals_offset: int = 0
    #: Where this run's slice of the session log starts (a rework continues another run's log),
    #: how far it is read, and where the current segment's lines begin.
    log_offset: int = 0
    log_read: int = 0
    segment_log_start: int = 0
    #: The session's running cost at the segment's start, and the latest seen.
    cost_base: CostState | None = None
    cost_seen: CostState | None = None
    #: The run's tokens at the segment's start, and so far, counted off its log as it runs.
    usage_base: RunUsage | None = None
    live_usage: RunUsage | None = None
    #: Where the session stands, from its hooks; persisted whole, so a restart replays none.
    signals: SessionSignals = Field(default_factory=SessionSignals)
    #: Turns across every segment, as `session_state` reports them.
    session_turns: int = 0
    #: A `finish` was sent: the session's end is a success.
    finishing: bool = False
    #: Why an interrupt parks it, and whether the session cap's backstop did, not the worker.
    park_reason: ParkReason | None = None
    parked_at_cap: bool = False
    #: The run that created the kept sandbox this one continues in, which the sandbox's metadata
    #: names, and the run that took this one's session and sandbox over.
    workspace_of: str | None = None
    continued_by: str | None = None
    #: The translator that wrote the mirror: a restart replays only through the same one.
    transcript_version: int | None = None
    #: The session log's sandbox path, for a CLI that names it only once it runs; otherwise
    #: the harness derives it from the session id.
    session_log: str | None = None
    #: The checkout's resolved path, which the CLI's trust seed names, and the config dir's.
    workspace_real: str | None = None
    config_real: str | None = None

    @property
    def began_session(self) -> bool:
        return self.session_id is not None and self.workspace_real is not None

    @model_validator(mode="before")
    @classmethod
    def _split_retain(cls, data: Any) -> Any:
        # A state file from before `public` and `kept`, read once after the deploy. `retain`
        # meant both, but could stay set after a failed park or a sandbox that timed out gone, so
        # `kept` is a best guess: a wrong one ends in a refusal once the sandbox is found gone.
        if not isinstance(data, dict) or "retain" not in data:
            return data
        data = dict(data)
        retain, status = data.pop("retain"), data.get("status")
        data.setdefault("public", retain)
        ended = status in ("succeeded", "failed", "timeout")
        data.setdefault("kept", status == "interrupted" or (retain and ended))
        return data

    @model_validator(mode="before")
    @classmethod
    def _drop_retired(cls, data: Any) -> Any:
        return drop_retired(data, _RETIRED_KEYS)


def _write_state(run_dir: Path, state: _RunState) -> None:
    target = run_dir / STATE_FILE
    tmp = target.with_suffix(".tmp")
    tmp.write_text(state.model_dump_json(indent=2), "utf-8")
    os.replace(tmp, target)


def _read_state(run_dir: Path, run_id: str) -> _RunState:
    try:
        return _RunState.model_validate_json((run_dir / STATE_FILE).read_text("utf-8"))
    except FileNotFoundError as exc:
        raise UnknownRunError(f"no E2B run {run_id} under {run_dir}") from exc


def _github_inject(token: str) -> dict[str, dict[str, str]]:
    """The run's GitHub token as the headers the proxy sets: git over HTTPS takes Basic auth,
    the REST API a token. Neither reaches the sandbox."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "github.com": {"Authorization": f"Basic {basic}"},
        "api.github.com": {"Authorization": f"token {token}"},
    }


def _on_gitlab(git_host: str) -> bool:
    return git_host not in ("", "github.com")


def _gitlab_inject(host: str, token: str) -> dict[str, dict[str, str]]:
    """A project token stays behind the proxy, for both git HTTPS and GitLab's API."""
    basic = base64.b64encode(f"oauth2:{token}".encode()).decode()
    return {
        host: {
            "Authorization": f"Basic {basic}",
            "PRIVATE-TOKEN": token,
        }
    }


def _check_routes(routes: Sequence[SecretRoute], allow: Sequence[str]) -> None:
    """A route's host must be one the sandbox may reach, or the proxy never sets its header.
    The match is `runtime_policy.egress_covers`'s, which the API checks the same route with."""
    for route in routes:
        host = route.host.lower()
        if not any(
            host == entry or (entry.startswith("*.") and host.endswith(entry[1:]))
            for entry in allow
        ):
            raise SpawnError(
                f"secret {route.name!r} is routed to {route.host}, which this run may not reach: "
                "add the host to the project's egress hosts",
                reason=SpawnReason.CONFIG,
            )


def _with_routes(
    inject: Mapping[str, Mapping[str, str]],
    routes: Sequence[SecretRoute],
    values: Mapping[str, str],
) -> dict[str, Mapping[str, str]]:
    """`inject` plus each routed secret's header. A route never replaces a header factorial
    sets itself (the model key, the run's git token, an MCP server's)."""
    merged = {host: dict(headers) for host, headers in inject.items()}
    for route in routes:
        value = values.get(route.name)
        if not value:
            raise SpawnError(
                f"secret {route.name!r} is routed to {route.host} but has no stored value",
                reason=SpawnReason.CONFIG,
            )
        headers = merged.setdefault(route.host, {})
        if any(name.lower() == route.header.lower() for name in headers):
            raise SpawnError(
                f"secret {route.name!r} would replace the {route.header} header factorial sets "
                f"on {route.host}",
                reason=SpawnReason.CONFIG,
            )
        headers[route.header] = value
    return dict(merged)


def _refuse_shadowing(names: Iterable[str], env: Mapping[str, str]) -> None:
    """A project secret never replaces a variable the sandbox runs on (`HOME`, the proxy's)."""
    taken = sorted(set(names) & set(env))
    if taken:
        raise SpawnError(
            f"project secret(s) {', '.join(taken)} would replace a variable the sandbox sets",
            reason=SpawnReason.CONFIG,
        )


def _exposed(names: Sequence[str], values: Mapping[str, str]) -> dict[str, str]:
    """The exposed secrets' values. One with none is refused, as `_with_routes` refuses a route."""
    missing = sorted(name for name in names if not values.get(name))
    if missing:
        raise SpawnError(
            f"secret(s) {', '.join(missing)} are exposed to the agent but have no stored value",
            reason=SpawnReason.CONFIG,
        )
    return {name: values[name] for name in names}


async def _checked(sandbox: Sandbox, command: str, what: str, **options: object) -> CommandOutput:
    """`command`'s output, or `SpawnError` naming `what` failed and its stderr."""
    output = await sandbox.run(command, **options)  # type: ignore[arg-type]
    if not output.ok:
        raise SpawnError(
            f"{what} failed in the sandbox (exit {output.exit_code}): "
            f"{output.stderr.strip()[-400:]}",
            reason=SpawnReason.INFRASTRUCTURE,
        )
    return output


def _nul(items: Sequence[str]) -> bytes:
    return b"".join(item.encode() + b"\0" for item in items)


def _validate_workspace(spec: RunSpec) -> None:
    if not _REPO.fullmatch(spec.workspace.repo):
        raise SpawnError(
            f"workspace.repo {spec.workspace.repo!r} is not owner/name",
            reason=SpawnReason.WORKSPACE,
        )
    if _BAD_REF.search(spec.workspace.base):
        raise SpawnError(
            f"workspace.base {spec.workspace.base!r} is not a ref git can fetch",
            reason=SpawnReason.WORKSPACE,
        )


class E2bAdapter:
    """One sandbox per run, its agent a session of the harness's TUI. Subclasses name the harness
    and the registry kind."""

    harness: Harness
    kind: ClassVar[str]

    supports_callbacks: ClassVar[bool] = False
    supports_mcp: ClassVar[bool] = True
    #: `assert_isolated` walks a host path, and the lane is dormant (design.md §7.6).
    supports_blind_workspace: ClassVar[bool] = False

    #: Written under the agent's config dir and attached by the harness (an `@` path or --image).
    supports_attachments: ClassVar[bool] = True
    #: The plan's limit counts running sandboxes only; the orchestrator bounds concurrency itself.
    max_concurrent_runs: ClassVar[int | None] = None
    pinned_version: ClassVar[str]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        harness = cls.__dict__.get("harness")
        if harness is not None:
            _CREDENTIAL_PATHS.update(harness.credential_paths)

    def __init__(
        self,
        *,
        api_key: SecretStr | str | None = None,
        template: str = "",
        state_root: Path | str | None = None,
        deployment: str = "",
        auth_mode: str = "api_key",
        egress: Sequence[str] = (),
        client: SandboxClient | None = None,
        cli: Cli | None = None,
        now: Callable[[], datetime] = utc_now,
        poll_interval: float = POLL_INTERVAL,
        max_session: timedelta = MAX_SESSION,
    ) -> None:
        self._key: str | None = None
        self._client = client
        self._template = template
        if client is None:
            self.use_workspace(api_key=api_key, template=template)
        self._root = Path(state_root).expanduser().resolve() if state_root is not None else None
        self._deployment = deployment
        self._auth_mode = auth_mode
        self._egress = tuple(egress)
        # Host-side probes only: `git ls-remote` and `gh`, never the agent's binary.
        self._cli = cli if cli is not None else Cli(agent_bin="git")
        self._now = now
        self._poll = poll_interval
        self._max_session = max_session
        self._supervisors: dict[str, asyncio.Task[None]] = {}
        self._sandboxes: dict[str, Sandbox] = {}
        #: The state each live supervisor writes, so a stop records its intent in the same object.
        self._states: dict[str, _RunState] = {}
        #: Per run, the lock `_fate` returns.
        self._fates: dict[str, asyncio.Lock] = {}
        #: The pins whose build passed the session smoke, as E2B said once.
        self._verified_builds: set[str] = set()

    def use_workspace(
        self, *, api_key: SecretStr | str | None, template: str | None = None
    ) -> None:
        """The E2B team this adapter's runtime spawns into, exactly as the row now names it: no
        key or no template leaves a spawn refused, never on the last ones seen. A key moves
        only when no run of the runtime is live (the API refuses the change otherwise), and a
        template only ever matters to a sandbox being created."""
        key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if key != self._key:
            self._key, self._client = key, E2bSandboxClient(key) if key else None
        self._template = template or ""

    # -- contract ------------------------------------------------------------------------

    async def spawn(self, run_spec: RunSpec) -> RunHandle:
        root = self._require_root()
        if self._client is None:
            raise SpawnError(
                "No E2B_API_KEY. Store the E2B team's key on the Runtime page.",
                reason=SpawnReason.CONFIG,
            )
        client = self._client
        if not self._template:
            raise SpawnError(
                f"This runtime names no E2B template. Choose the {self.harness.label} build it "
                "starts from on the Runtime page.\n\n`python -m orchestrator.e2b_template` "
                "prints the build.",
                reason=SpawnReason.CONFIG,
            )
        refuse_blind(type(self).__name__, run_spec)
        if not self.supports_attachments:
            refuse_attachments(type(self).__name__, run_spec.attachments)
        harness = self.harness
        harness.validate(run_spec, self._auth_mode)
        _validate_workspace(run_spec)
        if run_spec.resume_from is not None:
            return await self._adopt(run_spec, run_spec.resume_from)
        secrets = run_spec.resolved_secrets()  # the one sanctioned unwrap (contract.py)
        session_id = str(uuid.uuid4())
        launch = harness.session_spawn(run_spec, secrets, self._auth_mode, session_id)
        token = secrets.get(ENV_GH_TOKEN, "") if run_spec.workspace.credential == "minted" else ""
        git_host = secrets.get(ENV_GIT_HOST, "")
        allow = self._allow(run_spec, git_host=git_host)
        _check_routes(run_spec.secret_routes, allow)
        exposed = _exposed(run_spec.secret_env, secrets)
        policy = NetworkPolicy(
            allow=allow,
            inject=self._inject(
                launch, token, git_host=git_host, routes=run_spec.secret_routes, values=secrets
            ),
            public=run_spec.public,
        )

        run_id = _new_run_id(run_spec)
        created = self._now()
        # A session waits on humans, so only E2B's own cap bounds its sandbox.
        cap = created + self._max_session
        sandbox = await client.create(
            SandboxOptions(
                template=self._template,
                timeout_seconds=int(self._max_session.total_seconds()),
                metadata=self._metadata(run_id, run_spec),
                network=policy,
            )
        )
        run_dir = root / "runs" / run_id
        # Everything up to the state write sits in one try: an agent that started with no state
        # on disk is a run nobody could find, so any failure here kills its sandbox.
        try:
            sandbox_env = await self._prepare(sandbox, run_spec)
            # The create wrote the credential rules, before anything of the preview's exists.
            await self._firewall(sandbox, run_spec.public_ports, contain=True)
            # Read before the agent exists, so it names the checkout the CLI will trust.
            workspace_real, config_real = await _realpaths(sandbox)
            seeds = harness.session_seeds(
                workspace_real,
                config_real,
                self._auth_mode,
                run_spec.mcp,
                run_spec.prompts.system,
            )
            launch = replace(launch, files={**launch.files, **seeds})
            bot_login = secrets.get(ENV_BOT_LOGIN, "")
            agent_env = self._agent_env(
                launch,
                sandbox_env,
                token,
                run_spec.secret_routes,
                bot_login=bot_login,
                git_host=git_host,
            )
            agent_env |= config_env(pinned_config_sha(run_spec.workspace))
            await self._write_launch(sandbox, launch, agent_env, secrets, exposed=exposed)
            await asyncio.to_thread(run_dir.mkdir, parents=True, exist_ok=True)
            pid = await sandbox.start(SESSION)
            await self._take_consent(sandbox)
            session_log = None
            if not harness.chooses_session_id:
                session_id, session_log = await self._find_session(sandbox)
            state = _RunState(
                run_id=run_id,
                sandbox_id=sandbox.sandbox_id,
                status=RunStatus.RUNNING,
                agent=run_spec.agent,
                ticket=run_spec.ticket,
                repo=run_spec.workspace.repo,
                branch=run_spec.workspace.branch,
                auth_mode=self._auth_mode,
                started_at=self._now(),
                deadline=cap,
                pid=pid,
                session_id=session_id,
                max_turns=run_spec.max_turns,
                expected=run_spec.expected_artifacts,
                spawn_argv=harness.session_flags(run_spec),
                mcp=run_spec.mcp,
                allow=allow,
                secret_routes=run_spec.secret_routes,
                secret_env=run_spec.secret_env,
                agent_env=agent_env,
                bot_login=bot_login,
                public=run_spec.public,
                public_ports=run_spec.public_ports,
                awake_since=created,
                session_log=session_log,
                workspace_real=workspace_real,
                config_real=config_real,
            )
            _begin_segment(state, cap, turn=True, version=harness.transcript_version)
            _write_state(run_dir, state)
        except BaseException as exc:
            await self._kill(sandbox.sandbox_id, run_id)
            shutil.rmtree(run_dir, ignore_errors=True)
            if isinstance(exc, Exception) and substrate_failure(exc):
                detail = f"E2B failed while preparing the sandbox: {exc}"
                raise SubstrateUnavailable(detail) from exc
            raise

        self._supervise(run_dir, state, sandbox, git_env(secrets) if token else None)
        return RunHandle(run_id=run_id, status=RunStatus.RUNNING)

    async def status(self, run_id: str) -> RunStatus:
        run_dir = self._run_dir(run_id)
        state = _read_state(run_dir, run_id)
        if state.status.is_terminal:
            return state.status
        supervisor = self._supervisors.get(run_id)
        if supervisor is not None and not supervisor.done():
            return state.status
        # Nobody is watching this run: the worker restarted. Look without waking anything.
        client = self._require_client()
        try:
            info = await client.info(state.sandbox_id)
        except Exception as exc:
            log.warning("E2B unreachable while checking run %s: %s", run_id, exc)
            return state.status
        if info is None:
            return self._gone(run_dir, state, "found gone after a worker restart").status
        try:
            sandbox = await client.connect(state.sandbox_id, timeout_seconds=self._remaining(state))
        except SandboxGone:
            return self._gone(run_dir, state, "found gone after a worker restart").status
        except Exception as exc:
            log.warning("E2B unreachable while reattaching run %s: %s", run_id, exc)
            return state.status
        # No token: this process never had the run's secrets, so the orchestrator's
        # postconditions are the acceptance gate for it (decision 25).
        self._supervise(run_dir, state, sandbox, None)
        return state.status

    async def result(self, run_id: str) -> RunResult:
        state = _read_state(self._run_dir(run_id), run_id)
        if not state.status.is_terminal:
            raise AgentRuntimeError(f"run {run_id} has not reached a terminal state yet")
        return RunResult(
            run_id=run_id,
            outcome=state.status,
            summary=state.summary,
            artifacts=state.artifacts,
            cost_usd=state.cost_usd,
            input_tokens=state.input_tokens,
            cached_input_tokens=state.cached_input_tokens,
            cache_write_input_tokens=state.cache_write_input_tokens,
            output_tokens=state.output_tokens,
            failure_reason=state.failure_reason,
            stream_warnings=state.stream_warnings,
            session_id=state.session_id,
            park_reason=state.park_reason if state.status is RunStatus.INTERRUPTED else None,
        )

    async def logs(self, run_id: str) -> AsyncIterator[str]:
        """The mirror, one JSON object per line. Never the sandbox: the API holds no E2B key."""
        path = self._run_dir(run_id) / MIRROR_FILE
        if not path.exists():
            if not (path.parent / STATE_FILE).exists():
                raise UnknownRunError(f"no E2B run {run_id}")
            return
        for line in path.read_text("utf-8", errors="replace").splitlines():
            yield line

    async def cancel(self, run_id: str) -> None:
        await self._stop_run(run_id, interrupt=False)

    async def interrupt(self, run_id: str, reason: ParkReason = ParkReason.USER) -> None:
        await self._stop_run(run_id, interrupt=True, reason=reason)

    async def finish(self, run_id: str) -> None:
        """The CLI's quit at its plain prompt, a hangup on a question tool's dialog. Nothing while
        a turn runs, even after a finish that failed: a human just started it. Sent again by a
        call while the session still idles, which is the worker's one re-send. Raises only when
        it could not be sent; one that does not settle within `STOP_WAIT` leaves the session
        IDLE, for that re-send. Nothing on an interrupted run: its sandbox holds no branch
        another run needs."""
        run_dir = self._run_dir(run_id)
        state = _read_state(run_dir, run_id)
        if state.status.is_terminal:
            return
        attached = self._attached(run_id)
        live = attached[2] if attached is not None else state
        if live.stopping is not None:
            return  # a stop asked for first decides the outcome
        reason = live.signals.idle_reason
        if reason is None:
            return
        mode = "exit" if live.signals.at_plain_prompt else "hangup"
        # Before the send: the session can be gone, and finalised, before FINISH returns.
        live.finishing = True
        _write_state(run_dir, live)
        if (attached := await self._attach(run_id)) is None:
            return
        supervisor, sandbox, _ = attached
        try:
            await _checked(
                sandbox,
                self.harness.finish_script,
                "finishing the session",
                envs={"MODE": mode},
                timeout_seconds=FINISH_TIMEOUT,
            )
        except Exception as exc:
            if await _landed(run_dir, live, sandbox, supervisor):
                return
            raise AdapterUnavailable(f"cannot finish run {run_id}: {exc}") from exc
        try:
            await asyncio.wait_for(asyncio.shield(supervisor), timeout=STOP_WAIT)
        except TimeoutError:
            log.error("run %s did not finalise within %.0fs of its finish", run_id, STOP_WAIT)

    async def resume(
        self,
        run_id: str,
        turn: str | None,
        secrets: Mapping[str, SecretStr],
        attachments: tuple[RunAttachment, ...] = (),
        missing_attachments: tuple[MissingAttachment, ...] = (),
    ) -> RunHandle:
        """A session comes back in its kept sandbox as a new segment of the same session, at
        its prompt when `turn` is None."""
        if not self.supports_attachments:
            refuse_attachments(type(self).__name__, attachments)
        run_dir = self._run_dir(run_id)
        state = _read_state(run_dir, run_id)
        if state.status is not RunStatus.INTERRUPTED:
            raise UnknownRunError(
                f"This run isn't paused, so there is no session to resume.\n\nRun {run_id} is "
                f"{state.status}."
            )
        if state.continued_by is not None:
            raise UnknownRunError(
                "This run's session was already continued by a later run.\n\nRun "
                f"{state.continued_by} continued it."
            )
        if not state.began_session:
            raise UnknownRunError("This run had no terminal session to resume.")
        assert state.session_id is not None and state.workspace_real is not None
        if turn is None and attachments:
            raise AgentRuntimeError(
                f"run {run_id} resumes without a turn, so nothing would point at its images"
            )
        info = await self._require_kept(state)
        harness = self.harness
        values = {key: value.get_secret_value() for key, value in secrets.items()}
        launch = harness.session_resume(
            state.spawn_argv,
            state.session_id,
            values,
            state.auth_mode,
            state.mcp,
            turn,
            state.segments,
            attachments,
            missing_attachments,
        )
        # A run spawned before the config dir's path was recorded is claude's, which reads none.
        config_real = state.config_real or CONFIG_DIR
        seeds = harness.session_seeds(
            state.workspace_real, config_real, state.auth_mode, state.mcp, None
        )
        launch = replace(launch, files={**launch.files, **seeds})
        # The spawn's rule: a run that was never handed a GitHub token is not handed one now.
        token = values.get(ENV_GH_TOKEN, "") if ENV_GH_TOKEN in state.agent_env else ""
        bot_login = values.get(ENV_BOT_LOGIN) or state.bot_login
        agent_env = state.agent_env | (agent_git_identity(bot_login) if bot_login else {})
        info = await self._renew_for(run_dir, state, info, RENEW_FLOOR)
        sandbox = await self._wake(state, self._max_session, info)
        # E2B counts its cap from the last wake, which a preview keeping the sandbox awake made.
        assert state.awake_since is not None
        cap = state.awake_since + self._max_session
        try:
            await self._install_scripts(sandbox)
            await self._quiet(sandbox, state)
            policy = NetworkPolicy(
                allow=state.allow,
                inject=self._inject(
                    launch,
                    token,
                    git_host=values.get(ENV_GIT_HOST, ""),
                    routes=state.secret_routes,
                    values=values,
                ),
            )
            await self._arm(sandbox, _public_ports(state), policy)
            await self._write_launch(
                sandbox,
                launch,
                {**agent_env, **launch.env},
                values,
                exposed=_exposed(state.secret_env, values),
            )
            pid = await sandbox.start(SESSION)
            await self._take_consent(sandbox)
        except Exception as exc:
            raise await self._resume_failed(run_dir, state, sandbox, exc) from exc

        state.status = RunStatus.RUNNING
        state.stopping = state.park_reason = None
        state.parked_at_cap = state.finishing = state.kept = False
        state.agent_env, state.bot_login = agent_env, bot_login
        state.pid = pid
        state.exit_code = None
        state.failure_reason = None
        state.started_at = self._now()
        _begin_segment(state, cap, turn=turn is not None, version=harness.transcript_version)
        if turn is None and not harness.turnless_hook:
            # The launch's consent saw it reach its prompt, the only word that it waits there.
            state.signals.note_at_prompt(self._now())
        _write_state(run_dir, state)
        self._supervise(run_dir, state, sandbox, git_env(values) if token else None)
        return RunHandle(run_id=run_id, status=RunStatus.RUNNING)

    async def _adopt(self, spec: RunSpec, resume_from: ResumeFrom) -> RunHandle:
        """A rework or a retry continues `resume_from`'s session in that run's kept sandbox
        (decision 56): the checkout moved to the pull request's head for a rework, left as it
        was for a retry (`resume_from.mode`), the session resumed with the step's prompt as its
        turn, under a new run that begins where the earlier one's log ended, and that takes over
        a ready preview unless the sandbox is renewed first (`_renew_for`). `SessionUnavailable`,
        with nothing launched and the sandbox parked again, when there is no such session to
        continue; a substrate failure is never that, since a retry may find it."""
        harness = self.harness
        prev_dir, prev = self._adoptable(spec, resume_from)
        while prev.continued_by is not None and self._exists(prev.continued_by):
            # A rework names the step's latest attempt, so a continuer it does not name is one
            # whose spawn the orchestrator never recorded: a lost tick left it running in the
            # workspace, credentialled and unwatched. It is stopped, and its session continued.
            ghost = prev.continued_by
            await self.cancel(ghost)
            ghosted = ResumeFrom(run_id=ghost, session_id=resume_from.session_id)
            prev_dir, prev = self._adoptable(spec, ghosted)
        try:
            info = await self._require_kept(prev)
        except UnknownRunError as exc:
            raise SessionUnavailable(str(exc)) from exc
        run_id = _new_run_id(spec)
        session_id = resume_from.session_id
        secrets = spec.resolved_secrets()  # the one sanctioned unwrap (contract.py)
        launch = harness.session_adopt(spec, secrets, self._auth_mode, session_id, prev.segments)
        token = secrets.get(ENV_GH_TOKEN, "") if spec.workspace.credential == "minted" else ""
        git_host = secrets.get(ENV_GIT_HOST, "")
        allow = self._allow(spec, git_host=git_host)
        _check_routes(spec.secret_routes, allow)
        # Before the wake: a resume or a preview of the earlier run is refused from here on.
        prev.continued_by = run_id
        _write_state(prev_dir, prev)
        try:
            info = await self._renew_for(prev_dir, prev, info, RENEW_FLOOR)
            sandbox = await self._wake(prev, self._max_session, info)
        except BaseException as exc:
            prev.continued_by = None
            _write_state(prev_dir, prev)
            if isinstance(exc, UnknownRunError):
                raise SessionUnavailable(str(exc)) from exc
            raise
        # E2B counts its cap from the last wake, which a preview keeping the sandbox awake made.
        assert prev.awake_since is not None
        cap = prev.awake_since + self._max_session
        run_dir = self._require_root() / "runs" / run_id
        # A sandbox kept before its ports were recorded takes its preview's, or the rework's, the
        # same step's.
        public_ports = _public_ports(prev) or spec.public_ports
        try:
            await self._install_scripts(sandbox)
            # No session view may run once the proxy holds the new run's credentials, nor a CLI a
            # dead worker launched: rework.sh's fetch needs them. A ready preview serves on,
            # contained (`_arm`), and moves to the new run.
            await self._quiet(sandbox, prev)
            await _checked(sandbox, SCRUB, "stopping what the sandbox runs", timeout_seconds=60)
            preview = prev.preview
            # A sandbox found paused woke with nothing serving, whatever a park that died before
            # its record left there.
            ready = (
                info.state == "running"
                and preview is not None
                and preview.phase is PreviewPhase.READY
            )
            carried = preview if ready else None
            if carried is None and _preview_awake(prev):
                # One still starting is not carried: it is stopped for its renewal, as a setup an
                # agent's stop would cut is.
                await _renew_preview(sandbox, prev)
            inject = self._inject(
                launch, token, git_host=git_host, routes=spec.secret_routes, values=secrets
            )
            policy = NetworkPolicy(allow=allow, inject=inject)
            await self._arm(sandbox, public_ports, policy)
            session_log = _session_log(prev, harness)
            if session_log is None:
                raise _NoSession(f"run {prev.run_id} recorded no session log")
            if (await _sizes(sandbox, session_log)).log_size is None:
                raise _NoSession(f"run {prev.run_id}'s session log is gone from its sandbox")
            sandbox_env = await self._template_env(sandbox)
            workspace_real, config_real = await _realpaths(sandbox)
            branch_glob = spec.workspace.branch.replace("{slug}", "*")
            header = await _rework(sandbox, branch_glob, run_id, mode=resume_from.mode)
            # Measured after the rework: the agent's git can still append to the session log,
            # which is the agent's own file, and those lines are not this run's.
            sizes = await _sizes(sandbox, session_log)
            if sizes.log_size is None:
                raise _NoSession(f"run {prev.run_id}'s session log is gone from its sandbox")
            turn = turn_file(prev.segments)
            text, owner, mode = launch.files[turn]
            seeds = harness.session_seeds(
                workspace_real, config_real, self._auth_mode, spec.mcp, spec.prompts.system
            )
            files = {**launch.files, turn: (f"{header}\n\n".encode() + text, owner, mode)}
            launch = replace(launch, files={**files, **seeds})
            bot_login = secrets.get(ENV_BOT_LOGIN) or prev.bot_login
            agent_env = self._agent_env(
                launch,
                sandbox_env,
                token,
                spec.secret_routes,
                bot_login=bot_login,
                git_host=git_host,
            )
            agent_env |= config_env(pinned_config_sha(spec.workspace))
            await self._write_launch(
                sandbox, launch, agent_env, secrets, exposed=_exposed(spec.secret_env, secrets)
            )
            await asyncio.to_thread(run_dir.mkdir, parents=True, exist_ok=True)
            pid = await sandbox.start(SESSION)
            await self._take_consent(sandbox)
            state = _RunState(
                run_id=run_id,
                sandbox_id=prev.sandbox_id,
                status=RunStatus.RUNNING,
                agent=spec.agent,
                ticket=spec.ticket,
                repo=spec.workspace.repo,
                branch=spec.workspace.branch,
                auth_mode=self._auth_mode,
                started_at=self._now(),
                deadline=cap,
                pid=pid,
                session_id=session_id,
                max_turns=spec.max_turns,
                expected=spec.expected_artifacts,
                spawn_argv=harness.session_flags(spec),
                mcp=spec.mcp,
                allow=allow,
                secret_routes=spec.secret_routes,
                secret_env=spec.secret_env,
                agent_env=agent_env,
                bot_login=bot_login,
                public=prev.public,
                public_ports=public_ports,
                awake_since=prev.awake_since,
                preview=carried,
                workspace_real=workspace_real,
                config_real=config_real,
                workspace_of=prev.workspace_of or prev.run_id,
                segments=prev.segments,
                signals_start=sizes.signals_size,
                signals_offset=sizes.signals_size,
                log_offset=sizes.log_size,
                log_read=sizes.log_size,
                session_log=prev.session_log,
                cost_seen=prev.cost_seen,
            )
            _begin_segment(state, cap, turn=True, version=harness.transcript_version)
            _write_state(run_dir, state)
        except BaseException as exc:
            refused = await self._unadopt(sandbox, prev_dir, prev, run_dir, exc)
            if refused is None:
                raise
            raise refused from exc
        self._supervise(run_dir, state, sandbox, git_env(secrets) if token else None)
        prev.kept = False
        if carried is not None:
            # Only once the new run holds it: an adoption undone before this parks the preview,
            # and `_park` records it for its renewal.
            moved = {"phase": PreviewPhase.STOPPED, "detail": PREVIEW_CONTINUED, "urls": ()}
            prev.preview = carried.model_copy(update=moved)
        try:
            _write_state(prev_dir, prev)
        except OSError:
            # Its `continued_by` already refuses a preview or a resume of it, and leaves the
            # sandbox to the new run however its preview is stopped (`_settle`).
            log.exception("run %s: recording that run %s took it over failed", prev.run_id, run_id)
        return RunHandle(run_id=run_id, status=RunStatus.RUNNING)

    def _adoptable(self, spec: RunSpec, resume_from: ResumeFrom) -> tuple[Path, _RunState]:
        """The earlier run, read from disk, or `SessionUnavailable` saying why it cannot be
        continued. Its sandbox is not looked at."""
        run_id = resume_from.run_id
        try:
            prev_dir = self._run_dir(run_id)
            prev = _read_state(prev_dir, run_id)
        except UnknownRunError as exc:
            raise SessionUnavailable(f"there is no run {run_id} to continue: {exc}") from exc
        workspace = spec.workspace
        why = None
        if (prev.repo, prev.ticket, prev.agent) != (workspace.repo, spec.ticket, spec.agent):
            why = f"is {prev.agent} on {prev.repo} for {prev.ticket}, not this run's"
        elif not prev.status.is_terminal:
            why = f"is {prev.status}, not ended"
        elif not prev.began_session or prev.session_id != resume_from.session_id:
            why = f"has no session {resume_from.session_id}"
        elif prev.continued_by is not None and self._exists(prev.continued_by):
            pass  # its continuer holds the sandbox now; `_adopt` takes the session from it
        elif not prev.kept:
            why = "kept no sandbox"
        elif prev.public != spec.public:
            # Inbound traffic is set at the sandbox's creation only.
            made = "public" if prev.public else "private"
            why = f"has a {made} sandbox, and this run's preview needs the other"
        if why is not None:
            raise SessionUnavailable(f"run {run_id} {why}, so its session cannot be continued")
        return prev_dir, prev

    def _exists(self, run_id: str) -> bool:
        try:
            return (self._run_dir(run_id) / STATE_FILE).exists()
        except UnknownRunError:
            return False

    async def _unadopt(
        self,
        sandbox: Sandbox,
        prev_dir: Path,
        prev: _RunState,
        run_dir: Path,
        exc: BaseException,
    ) -> AgentRuntimeError | None:
        """Undo an adoption that failed before its run existed: the sandbox parked again without
        the new credentials, and the earlier run continuable as it was, unless the park killed
        it. Returns what the spawn raises instead of `exc`, or None to raise `exc` itself."""
        parked = await self._park(sandbox, prev)
        prev.continued_by = None
        prev.kept = parked is None
        _write_state(prev_dir, prev)
        shutil.rmtree(run_dir, ignore_errors=True)
        kept = "" if parked is None else f"; {parked}"
        if isinstance(exc, _NoBranch):
            return NoBranchHeld(f"{exc}{kept}")
        if isinstance(exc, _NoSession):
            return SessionUnavailable(f"{exc}{kept}")
        if isinstance(exc, SpawnError) or not isinstance(exc, Exception):
            return None
        why = f"continuing run {prev.run_id}'s session failed: {exc}{kept}"
        if substrate_failure(exc):
            return SubstrateUnavailable(why)
        return AdapterUnavailable(why)

    async def session_state(self, run_id: str) -> SessionState:
        """From the state file alone: it never wakes a sandbox, nor waits on one."""
        state = _read_state(self._run_dir(run_id), run_id)
        signals = state.signals
        idle = state.status is RunStatus.IDLE and signals.idle_since is not None
        asking = idle and signals.idle_reason is IdleReason.QUESTION
        failed = idle and signals.idle_reason is IdleReason.TURN_ERROR
        return SessionState(
            idle_reason=signals.idle_reason if idle else None,
            idle_since=signals.idle_since if idle else None,
            question=signals.question if asking else None,
            question_expires_at=signals.question_expires_at if asking else None,
            turn_error=signals.turn_error if failed else None,
            turns=state.session_turns,
            session_deadline=state.deadline,
            log_offset=state.log_offset,
            last_input_at=signals.last_input_at,
            session_id=state.session_id,
            usage=state.live_usage,
        )

    async def terminal_target(self, run_id: str) -> TerminalTarget:
        state = _read_state(self._run_dir(run_id), run_id)
        return TerminalTarget(
            substrate=SUBSTRATE,
            socket=TMUX_SOCKET,
            session=TMUX_SESSION,
            sandbox_id=state.sandbox_id,
        )

    async def spawn_refusal(self) -> str | None:
        """None once the build this adapter boots passed the session smoke: a verdict E2B
        holds as a tag on the build (decision 25). A passed build is remembered, since its
        verdict never changes; a missing one is asked again, since it can be recorded later.
        No key or no template is left to `spawn`, which refuses either."""
        pin = self._template
        if self._client is None or not pin or pin in self._verified_builds:
            return None
        try:
            verified = await self._client.session_verified(pin)
        except SpawnError as exc:
            return str(exc)
        if not verified:
            return (
                f"build {pin} has not passed the session smoke, so its CLI's session log is "
                "unproven: run `uv run python -m orchestrator.e2b_template --interactive-smoke "
                f"{pin}` with the team's E2B_API_KEY and the model key the smoke bills, or "
                "choose a build the Runtime page marks verified"
            )
        self._verified_builds.add(pin)
        return None

    async def _require_kept(self, state: _RunState) -> SandboxInfo:
        """An interrupted run's sandbox still exists, looked at without waking it: E2B's word on
        it, which says whether something keeps it awake."""
        run_id = state.run_id
        try:
            info = await self._require_client().info(state.sandbox_id)
        except Exception as exc:
            detail = f"cannot reach E2B to resume run {run_id}: {exc}"
            if substrate_failure(exc):
                raise SubstrateUnavailable(detail) from exc
            raise AdapterUnavailable(detail) from exc
        if info is None:
            raise UnknownRunError(
                "This run's sandbox is gone, so there is nothing to resume in.\n\nSandbox "
                f"{state.sandbox_id} no longer exists."
            )
        return info

    async def _renew_for(
        self, run_dir: Path, state: _RunState, info: SandboxInfo, needed: timedelta
    ) -> SandboxInfo:
        """Before a resume, a rework or a view wakes a sandbox something else keeps awake: E2B
        counts its hour from that wake, so a window with less left than `needed` (at most a whole
        one), or one whose wake was never recorded, is renewed first. `_park` stops an awake
        preview for the worker to start again, and the wake that follows opens a new window.
        Returns `info` as that wake will find the sandbox."""
        if info.state != "running":
            return info
        needed = min(needed, self._max_session - SESSION_MARGIN)
        if state.awake_since is not None and self._window_end(state) - self._now() >= needed:
            return info
        sandbox, _ = await self._awake_sandbox(state)
        if sandbox is not None:
            parked = await self._park(sandbox, state)
            if parked is not None:
                state.kept = False
                _write_state(run_dir, state)
                raise UnknownRunError(f"renewing run {state.run_id}'s sandbox failed: {parked}")
            _write_state(run_dir, state)
        return replace(info, state="paused")

    async def _wake(self, state: _RunState, session: timedelta, info: SandboxInfo) -> Sandbox:
        """An interrupted run's sandbox, as `info` found it, awake for `session` to resume in.
        A preview may keep it awake already, in the window its own wake opened."""
        run_id = state.run_id
        woke = self._now()
        _waking(state, info)
        try:
            sandbox = await self._hold(
                state.sandbox_id, timeout_seconds=int(session.total_seconds())
            )
        except SandboxGone as exc:
            raise UnknownRunError(f"run {run_id}'s sandbox is gone") from exc
        except Exception as exc:
            detail = f"cannot reach the sandbox of run {run_id}: {exc}"
            if substrate_failure(exc):
                raise SubstrateUnavailable(detail) from exc
            raise AdapterUnavailable(detail) from exc
        state.awake_since = state.awake_since or woke
        return sandbox

    async def _hold(self, sandbox_id: str, *, timeout_seconds: int) -> Sandbox:
        """Connect, waking a paused sandbox, and hold it against the reaper's stray park until
        `_park` or `_kill` lets it go."""
        async with _lock(sandbox_id):
            held = sandbox_id in _HELD
            _HELD.add(sandbox_id)
            try:
                return await self._require_client().connect(
                    sandbox_id, timeout_seconds=timeout_seconds
                )
            except BaseException:
                if not held:
                    _HELD.discard(sandbox_id)
                raise

    async def _quiet(self, sandbox: Sandbox, state: _RunState) -> None:
        """End any open view before a resume writes its credentials. A serving preview keeps
        serving: `_arm` contains it before the rules go in (design.md §7.10). One still in its
        setup is stopped for its renewal first (`_renew_in_setup`)."""
        await _renew_in_setup(sandbox, state)
        await self._end_inspect(sandbox, state)

    async def _end_inspect(self, sandbox: Sandbox, state: _RunState) -> None:
        """End any session view in the sandbox before a resume writes its credentials, and stop
        whatever runs as the agent's uid. Always, never on the state file's word: a sandbox an
        older adapter woke may hold a shell it never recorded. A recorded view's end follows, so
        what its CLI appended to the session log is not read as the resume's."""
        await _checked(sandbox, END_SHELL, "ending the session view", timeout_seconds=60)
        inspect = state.inspect
        if inspect is not None and inspect.phase in _INSPECT_AWAKE:
            await self._close_view(sandbox, state)
            stopped = {"phase": InspectPhase.STOPPED, "detail": "the agent resumed", "clients": 0}
            state.inspect = inspect.model_copy(update=stopped)

    async def _resume_failed(
        self, run_dir: Path, state: _RunState, sandbox: Sandbox, exc: Exception
    ) -> AdapterUnavailable:
        # `_wake` woke it with fresh credentials to come; put it back to sleep without them.
        parked = await self._park(sandbox, state)
        if parked is not None:
            state.kept = False
            _write_state(run_dir, state)
        kept = "" if parked is None else f"; {parked}"
        return AdapterUnavailable(f"resuming run {state.run_id} failed: {exc}{kept}")

    async def reap(self, keep: Collection[str], *, retain: timedelta | None) -> tuple[str, ...]:
        """Kill this deployment's sandboxes in this adapter's E2B team whose run `keep` does not
        name, and delete the mirrors of terminal runs older than `retain` (decision 25). Without
        a key it only prunes: the mirrors are the deployment's, whatever team ran them, so one
        reaper per pass prunes them and the others pass `retain=None`.

        Fenced by the deployment id in each sandbox's metadata, so a laptop and the deployment
        sharing a key never reap each other's; a deployment with no id reaps nothing."""
        if not self._deployment:
            return ()
        wanted, now, reaped = set(keep), self._now(), []
        listed = (
            await self._client.list({"factorial_deployment": self._deployment})
            if self._client
            else ()
        )
        for info in listed:
            run_id = info.metadata.get("factorial_run", "")
            if run_id in wanted and info.state == "running":
                try:
                    await self._park_stray(run_id, info.sandbox_id)
                except Exception as exc:
                    log.warning("re-parking run %s's sandbox failed: %s", run_id, exc)
            if run_id in wanted or info.started_at is None or now - info.started_at < REAP_GRACE:
                continue
            try:
                await self._require_client().kill(info.sandbox_id)
            except Exception as exc:
                log.warning("reaping sandbox %s of run %s failed: %s", info.sandbox_id, run_id, exc)
                continue
            reaped.append(run_id)
        if retain is not None:
            await asyncio.to_thread(self._prune, wanted, now - retain)
        return tuple(reaped)

    async def _park_stray(self, run_id: str, sandbox_id: str) -> None:
        """Pause a kept sandbox found running with nothing awake in it: a viewer's connect that
        raced a park woke it, or a worker died between a wake and its record. At its timeout
        E2B kills it, and the kept workspace with it."""
        if self._root is None or sandbox_id in _HELD:
            return
        try:
            run_dir = self._run_dir(run_id)
            state = _read_state(run_dir, run_id)
        except (AgentRuntimeError, OSError, ValueError):
            return
        if (
            state.sandbox_id != sandbox_id
            or not state.status.is_terminal
            or not state.kept
            or _preview_awake(state)
            or _inspect_awake(state)
        ):
            return
        lock = _lock(sandbox_id)
        if lock.locked():
            return
        try:
            async with lock:
                if sandbox_id in _HELD:
                    return
                # Again under the lock: a wake that finished meanwhile recorded why it woke.
                state = _read_state(run_dir, run_id)
                if _preview_awake(state) or _inspect_awake(state) or not state.status.is_terminal:
                    return
                client = self._require_client()
                info = await client.info(sandbox_id)
                if info is None or info.state != "running":
                    return  # `connect` would wake it
                try:
                    sandbox = await client.connect(sandbox_id, timeout_seconds=STRAY_PARK_SECONDS)
                except SandboxGone:
                    return
                log.warning("run %s's kept sandbox ran with nothing awake; parking it", run_id)
                if await self._park(sandbox, state) is not None:
                    state.kept = False
                    _write_state(run_dir, state)
        finally:
            if sandbox_id not in _HELD:
                _release(sandbox_id)

    def _prune(self, wanted: set[str], before: datetime) -> None:
        runs = self._require_root() / "runs"
        for run_dir in runs.iterdir() if runs.is_dir() else ():
            try:
                state = _read_state(run_dir, run_dir.name)
                written = (run_dir / STATE_FILE).stat().st_mtime
                stale = datetime.fromtimestamp(written, before.tzinfo)
                # An interrupted run is kept by being wanted, which only the orchestrator knows.
                if state.run_id not in wanted and state.status.is_terminal and stale < before:
                    shutil.rmtree(run_dir)
            except (OSError, UnknownRunError, ValueError) as exc:
                log.warning("pruning %s skipped it: %s", run_dir, exc)

    # -- previews (decision 54) -------------------------------------------------------------

    async def preview_start(
        self, run_id: str, spec: PreviewSpec, env: Mapping[str, SecretStr], *, token: str
    ) -> PreviewState:
        run_dir = self._run_dir(run_id)
        state = self._live_state(run_dir, run_id)
        current = state.preview
        if current is not None and current.token == token and current.phase in _AWAKE:
            return current.state()
        # A live run that ends meanwhile decides its sandbox before this start or once the start
        # is on record, never between the firewall's load and the record.
        async with self._fate(run_id):
            return await self._start_preview(run_dir, state, spec, env, token)

    async def _start_preview(
        self,
        run_dir: Path,
        state: _RunState,
        spec: PreviewSpec,
        env: Mapping[str, SecretStr],
        token: str,
    ) -> PreviewState:
        """`preview_start` under the run's lock: a run that ended while it waited is read as
        ended."""
        run_id = state.run_id
        _require_servable(state)
        current = state.preview
        if current is not None and current.phase in _AWAKE:
            # Another start's services still run: two supervisors would fight over the ports.
            awake, _ = await self._awake_sandbox(state)
            if awake is not None:
                await self._install_scripts(awake)
                await _stop_services(awake, current)
                if state.status.is_terminal and not _inspect_awake(state):
                    # A setup it still runs is the agent's uid's, and nothing else of the
                    # agent's runs here. Beside a live run or a view only the setup's
                    # supervisor goes, and a step it had begun ends by itself: the orchestrator
                    # never replaces a preview still starting, so none is begun there.
                    await _checked(awake, STOP, "stopping the preview's setup", timeout_seconds=60)
            replaced = {"phase": PreviewPhase.STOPPED, "detail": "replaced by a new start"}
            state.preview = current.model_copy(update={**replaced, "urls": ()})
            _write_state(run_dir, state)
        client = self._require_client()
        try:
            info = await client.info(state.sandbox_id)
        except Exception as exc:
            raise AdapterUnavailable(f"cannot reach E2B for run {run_id}'s preview: {exc}") from exc
        if info is None:
            raise PreviewUnavailable(f"run {run_id}'s sandbox is gone, so nothing can serve it")
        woke = self._now()
        _waking(state, info)
        session = self._wake_seconds(state, woke + spec.stop_after)
        try:
            sandbox = await self._hold(state.sandbox_id, timeout_seconds=session)
        except SandboxGone as exc:
            raise PreviewUnavailable(f"run {run_id}'s sandbox is gone") from exc
        state.awake_since = state.awake_since or woke
        recorded = False
        try:
            await self._install_scripts(sandbox)
            # Beside a live run or an open view, their credential rules stand and the preview is
            # contained; alone, the parked policy holds no credential (design.md §7.10).
            beside = not state.status.is_terminal or _inspect_awake(state)
            if not beside:
                await sandbox.update_network(NetworkPolicy(allow=state.allow))
            # A run kept before its ports were recorded takes them from its preview.
            ports = state.public_ports or tuple(service.port for service in spec.services)
            state.public_ports = ports
            await self._firewall(sandbox, ports, contain=beside)
            await self._write_files(sandbox, _preview_files(state, spec, env))
            now = self._now()
            deadline = now + spec.stop_after
            state.preview = _Preview(
                token=token,
                phase=PreviewPhase.STARTING,
                services=spec.services,
                started_at=now,
                deadline=deadline,
                timeout_until=max(deadline + SESSION_MARGIN, woke + timedelta(seconds=session)),
            )
            _write_state(run_dir, state)
            recorded = True
            state.preview.pid = await sandbox.start(PREVIEW)
        except Exception as exc:
            why = f"starting run {run_id}'s preview failed: {exc}"
            if recorded:
                # On record already, so it ends as any preview does.
                phase = PreviewPhase.FAILED
                failed = await self._close_preview(run_dir, state, sandbox, phase, why)
                raise PreviewUnavailable(failed.detail or why) from exc
            parked = await self._settle(sandbox, state)
            if parked is not None:
                state.kept = False
            _write_state(run_dir, state)
            kept = "" if parked is None else f"; {parked}"
            raise PreviewUnavailable(f"{why}{kept}") from exc
        self._sandboxes[run_id] = sandbox
        _write_state(run_dir, state)
        return state.preview.state()

    async def preview_status(
        self, run_id: str, *, keep_until: datetime | None = None
    ) -> PreviewState:
        run_dir = self._run_dir(run_id)
        state = self._live_state(run_dir, run_id)
        preview = state.preview
        if preview is None:
            return PreviewState(phase=PreviewPhase.STOPPED)
        if preview.phase not in _AWAKE:
            return preview.state()
        if keep_until is not None and keep_until != preview.deadline:
            preview.deadline = keep_until
            _write_state(run_dir, state)
        sandbox, why = await self._awake_sandbox(state)
        if sandbox is None:
            return await self._end_preview(run_dir, state, None, PreviewPhase.STOPPED, why)
        now = self._now()
        if now >= preview.deadline:
            return await self._end_preview(
                run_dir, state, sandbox, PreviewPhase.STOPPED, PREVIEW_IDLE
            )
        # A live run keeps the sandbox awake and decides it at its end; a run a rework continued
        # has no say in it (`_settle`).
        holds = state.status.is_terminal and state.continued_by is None
        if holds and not _inspect_awake(state) and now >= self._window_end(state):
            # E2B's cap: only a pause opens a new window. The worker starts the preview again. An
            # open view's end, at its limit within the window, leaves the pause to the next read.
            async with self._fate(run_id):
                parked = await self._park(sandbox, state)
            if parked is not None:
                state.kept = False
            _write_state(run_dir, state)
            assert state.preview is not None
            return state.preview.state()
        end = preview.deadline + SESSION_MARGIN
        extend = holds and (preview.timeout_until is None or end > preview.timeout_until)
        try:
            report = await _checked(sandbox, PREVIEW_STATUS, "reading the preview")
            alive = preview.pid in await sandbox.pids()
            if extend:
                # After the read: `connect` would wake a sandbox something else paused.
                seconds = self._wake_seconds(state, preview.deadline)
                await self._hold(state.sandbox_id, timeout_seconds=seconds)
                # What it asked for, though the cap or a whole second cut the timeout short: only a
                # later deadline asks again.
                preview.timeout_until = max(end, now + timedelta(seconds=seconds))
                _write_state(run_dir, state)
        except Exception:
            # A cached handle outlives a sandbox E2B's console or its timeout destroyed.
            self._sandboxes.pop(run_id, None)
            if await self._require_client().info(state.sandbox_id) is not None:
                raise
            state.kept = False
            why = "its sandbox is gone"
            return await self._end_preview(run_dir, state, None, PreviewPhase.FAILED, why)
        lines = report.stdout.splitlines()
        first = lines[0] if lines else ""
        if first == "setup failed":
            detail = "setup failed:\n" + "\n".join(lines[1:])
            return await self._end_preview(run_dir, state, sandbox, PreviewPhase.FAILED, detail)
        if first == "setup running":
            if alive:
                return preview.state()
            left = (await sandbox.run(PREVIEW_ENDED)).stdout.strip()
            why = "the preview's start script ended before setup finished"
            detail = f"{why}:\n{left}" if left else why
            return await self._end_preview(run_dir, state, sandbox, PreviewPhase.FAILED, detail)
        held = [service for service in preview.services if f"held {service.name}" in lines]
        if held:
            # What answers on that port is another uid's, so it is never published as the
            # preview's.
            service = held[0]
            why = (
                f"port {service.port} is held by another user, so service {service.name} cannot "
                "serve it"
            )
            return await self._end_preview(run_dir, state, sandbox, PreviewPhase.FAILED, why)
        serving_since = preview.serving_since = preview.serving_since or now
        ready = {line.removeprefix("ready ") for line in lines if line.startswith("ready ")}
        head = next((line.removeprefix("head ") for line in lines if line.startswith("head ")), "")
        preview.head_sha = head.strip() or None
        preview.uncommitted = "dirty" in lines
        waiting = [service for service in preview.services if service.name not in ready]
        if not waiting:
            preview.misses = 0
            preview.phase = PreviewPhase.READY
            preview.urls = tuple(
                PreviewUrl(service=service.name, url=f"https://{sandbox.host(service.port)}")
                for service in preview.services
            )
            _write_state(run_dir, state)
            return preview.state()
        if preview.phase is PreviewPhase.READY:
            preview.misses += 1
            if alive and preview.misses < PREVIEW_MISSES_BEFORE_FAILED:
                # A server busy compiling misses a 2s probe; the reviewer's frame stays up.
                _write_state(run_dir, state)
                return preview.state()
            names = ", ".join(service.name for service in waiting)
            why = f"service(s) {names} stopped answering"
            return await self._end_preview(run_dir, state, sandbox, PreviewPhase.FAILED, why)
        late = next((s for s in waiting if now >= serving_since + s.ready_timeout), None)
        if late is not None or not alive:
            service = late or waiting[0]
            tail = await sandbox.run('tail -c 2000 "$F"', envs={"F": service_log(service.name)})
            why = (
                f"service {service.name} did not answer {service.ready_path} on port "
                f"{service.port} within {service.ready_timeout}"
                if late is not None
                else "every service exited"
            )
            detail = f"{why}:\n{tail.stdout}"
            return await self._end_preview(run_dir, state, sandbox, PreviewPhase.FAILED, detail)
        _write_state(run_dir, state)
        return preview.state()

    async def preview_stop(self, run_id: str) -> None:
        run_dir = self._run_dir(run_id)
        state = self._live_state(run_dir, run_id)
        if not _preview_awake(state):
            return
        sandbox, _ = await self._awake_sandbox(state)
        await self._end_preview(run_dir, state, sandbox, PreviewPhase.STOPPED, "stopped")

    async def _awake_sandbox(self, state: _RunState) -> tuple[Sandbox | None, str]:
        """The sandbox an awake preview or view runs in, or None and why. Looks with `info`
        first: `connect` would wake a sandbox something else parked."""
        cached = self._sandboxes.get(state.run_id)
        if cached is not None:
            return cached, ""
        client = self._require_client()
        info = await client.info(state.sandbox_id)
        if info is None:
            state.kept = False
            return None, "its sandbox is gone"
        if info.state != "running":
            return None, "its sandbox was paused"
        try:
            sandbox = await self._hold(
                state.sandbox_id, timeout_seconds=self._wake_seconds(state, self._now())
            )
        except SandboxGone:
            state.kept = False
            return None, "its sandbox is gone"
        self._sandboxes[state.run_id] = sandbox
        return sandbox, ""

    async def _end_preview(
        self,
        run_dir: Path,
        state: _RunState,
        sandbox: Sandbox | None,
        phase: PreviewPhase,
        detail: str,
    ) -> PreviewState:
        """`_close_preview` under the run's lock. A preview something else ended while this
        waited, the run's end parking the sandbox for its renewal, stays as that end left it."""
        async with self._fate(state.run_id):
            assert state.preview is not None
            if not _preview_awake(state):
                return state.preview.state()
            return await self._close_preview(run_dir, state, sandbox, phase, detail)

    async def _close_preview(
        self,
        run_dir: Path,
        state: _RunState,
        sandbox: Sandbox | None,
        phase: PreviewPhase,
        detail: str,
    ) -> PreviewState:
        """Record the end, then stop the services and leave the sandbox to whatever still runs in
        it, parking it when nothing does (`_settle`). A park that fails kills it, and then nothing
        is kept. Its caller holds the run's lock."""
        assert state.preview is not None
        preview = state.preview
        state.preview = preview.model_copy(update={"phase": phase, "detail": detail, "urls": ()})
        parked = None
        if sandbox is not None:
            try:
                await _stop_services(sandbox, preview)
            except Exception as exc:
                # What the stop left is contained beside a live run or a view, whose end parks
                # it, and ended by the park otherwise.
                log.warning("stopping run %s's preview failed: %s", state.run_id, exc)
            parked = await self._settle(sandbox, state)
        if parked is not None:
            state.kept = False
            state.preview = state.preview.model_copy(update={"detail": f"{detail}\n{parked}"})
        _write_state(run_dir, state)
        return state.preview.state()

    def _window_end(self, state: _RunState) -> datetime:
        """When E2B's session cap ends the sandbox's current wake, less the park's margin."""
        return (state.awake_since or self._now()) + self._max_session - SESSION_MARGIN

    def _wake_seconds(self, state: _RunState, until: datetime) -> int:
        """E2B's timeout for a sandbox awake until `until`, or until the later deadline of a
        preview, a view or a live run already in it: a connect must never cut another short.
        Never past the window its last wake opened."""
        deadlines = [until]
        if _preview_awake(state):
            assert state.preview is not None
            deadlines.append(state.preview.deadline)
        if _inspect_awake(state):
            assert state.inspect is not None
            deadlines.append(state.inspect.deadline)
        if not state.status.is_terminal:
            deadlines.append(state.deadline)
        now = self._now()
        left = (max(deadlines) + SESSION_MARGIN - now).total_seconds()
        window = (self._window_end(state) + SESSION_MARGIN - now).total_seconds()
        return min(max(int(left), 60), max(int(window), 60), int(self._max_session.total_seconds()))

    async def _firewall(self, sandbox: Sandbox, ports: Sequence[int], *, contain: bool) -> None:
        """Load the sandbox's firewall (`FIREWALL`): inbound only to `ports` and envd's, and with
        `contain` the preview's uid kept to loopback. A run with no public ports has none."""
        if not ports:
            return
        envs = {"MODE": "contain" if contain else "open", "PORTS": ",".join(map(str, ports))}
        await _checked(sandbox, FIREWALL, "loading the firewall", envs=envs, timeout_seconds=60)

    async def _arm(self, sandbox: Sandbox, ports: Sequence[int], policy: NetworkPolicy) -> None:
        """Write credential rules with the preview contained first: no preview process runs a
        moment beside them uncontained, and a containment that fails to load writes none
        (design.md §7.10)."""
        await self._firewall(sandbox, ports, contain=True)
        await sandbox.update_network(policy)

    async def _settle(self, sandbox: Sandbox, state: _RunState) -> str | None:
        """After a preview or a view ends in a woken sandbox: leave it to whatever still runs
        there, with no credential rule it no longer needs, or park it when nothing does. A live
        run's supervisor holds it, credentials and all; an open view keeps its model key with the
        preview contained; a preview alone gets the parked policy and its egress back. A run a
        rework continued has no say: the sandbox is the rework's. Why, when that fails and the
        sandbox is killed.

        A preview's start or end calls it under the run's lock (`_fate`), and the run's end
        decides under that lock too: what the state says now is what that end left, and a sandbox
        it parked, or killed, is no longer running and is left as it is."""
        if state.continued_by is not None:
            return None
        if not state.status.is_terminal or _inspect_awake(state):
            return None
        info = await self._require_client().info(state.sandbox_id)
        if info is None:
            state.kept = False
            return None
        if info.state != "running":
            return None
        if _preview_awake(state):
            try:
                # A view's close may have failed, leaving its CLI and the credential files.
                scrub = f"{SCRUB} {self._credential_files()}"
                await _checked(sandbox, scrub, "the scrub", timeout_seconds=60)
                await sandbox.update_network(NetworkPolicy(allow=state.allow))
                await self._firewall(sandbox, _public_ports(state), contain=False)
            except Exception as exc:
                log.error("leaving run %s's preview alone failed: %s", state.run_id, exc)
                await self._kill(state.sandbox_id, state.run_id)
                return f"the sandbox could not be kept, so it was killed: {exc}"
            return None
        return await self._park(sandbox, state)

    def _credential_files(self) -> str:
        """What a scrub that keeps the sandbox awake deletes, quoted: every harness's credential
        files, the run's and a view's env files, never the preview's, whose services still read
        it."""
        paths = sorted({*self.harness.credential_paths, *_CREDENTIAL_PATHS, VIEW_ENV, ENV})
        return " ".join(f"'{path}'" for path in paths)

    async def _end_run(self, sandbox: Sandbox, state: _RunState) -> str | None:
        """A run's end. A rollover or E2B's cap must pause (a renewal: the preview stops, and the
        worker starts it again), and so does a run with no serving preview beside it: `_park`. A
        run that ends beside a serving preview otherwise ends as a park would but for the pause:
        the agent's processes and credential files go, the header rules drop, and the sandbox
        stays awake for the preview, its egress back (design.md §7.10). Its caller holds the run's
        lock (`_fate`) through the status write, so no preview's start or end lands in between."""
        rollover = state.stopping is RunStatus.INTERRUPTED and (
            state.park_reason is ParkReason.ROLLOVER or state.parked_at_cap
        )
        renewing = rollover or self._now() >= self._window_end(state)
        # The scrub would cut a setup still running as the agent's uid: that preview is parked
        # for its renewal too.
        if renewing or not _serving(state):
            return await self._park(sandbox, state)
        try:
            scrub = f"{SCRUB} {self._credential_files()}"
            await _checked(sandbox, scrub, "the scrub", timeout_seconds=60)
            await sandbox.update_network(NetworkPolicy(allow=state.allow))
            await self._firewall(sandbox, _public_ports(state), contain=False)
        except Exception as exc:
            log.error(
                "ending run %s beside its preview failed, so its sandbox is killed: %s",
                state.run_id,
                exc,
            )
            await self._kill(state.sandbox_id, state.run_id)
            return f"the sandbox could not be kept, so it was killed: {exc}"
        return None

    def _fate(self, run_id: str) -> asyncio.Lock:
        """The lock over what becomes of a run's sandbox: parked, left awake to its preview, or
        killed. The run's end, and a preview's start, end and park, each take it once, around
        their decision and the sandbox commands that carry it out, never around a wait for the
        agent or a setup."""
        return self._fates.setdefault(run_id, asyncio.Lock())

    # -- session views (decision 57) ---------------------------------------------------------

    async def inspect_start(
        self,
        run_id: str,
        *,
        token: str,
        limit: timedelta,
        credentials: Mapping[str, SecretStr],
    ) -> InspectState:
        run_dir = self._run_dir(run_id)
        state = _read_state(run_dir, run_id)
        _require_inspectable(state)
        current = state.inspect
        if current is not None and current.token == token and current.phase in _INSPECT_AWAKE:
            return current.state(state.sandbox_id)
        harness = self.harness
        assert state.session_id is not None and state.workspace_real is not None
        # No GitHub credential reaches a view, from the credentials handed in or the run's env.
        values = {
            key: value.get_secret_value()
            for key, value in credentials.items()
            if not GITHUB_KEY.search(key)
        }
        try:
            launch = harness.session_view(
                state.spawn_argv,
                state.session_id,
                values,
                state.auth_mode,
                workspace_real=state.workspace_real,
                config_real=state.config_real or CONFIG_DIR,
            )
        except SpawnError as exc:
            raise InspectUnavailable(str(exc)) from exc
        env = view_git_env(
            {
                key: value
                for key, value in {**state.agent_env, **launch.env}.items()
                if not GITHUB_KEY.search(key)
            }
        )
        # E2B's session cap holds the sandbox's whole wake, the park's margin included.
        limit = min(limit, self._max_session - SESSION_MARGIN)
        client = self._require_client()
        try:
            info = await client.info(state.sandbox_id)
        except Exception as exc:
            why = f"cannot reach E2B for run {run_id}'s session view: {exc}"
            raise AdapterUnavailable(why) from exc
        if info is None:
            state.kept = False
            _write_state(run_dir, state)
            raise UnknownRunError(f"run {run_id}'s sandbox is gone, so its session cannot open")
        if current is not None and current.phase in _INSPECT_AWAKE:
            # One view per run: a new start replaces the one another request opened.
            awake, _ = await self._awake_sandbox(state)
            if awake is not None:
                await self._close_view(awake, state)
            replaced = {"phase": InspectPhase.STOPPED, "detail": "replaced by another view"}
            state.inspect = current.model_copy(update={**replaced, "clients": 0})
        info = await self._renew_for(run_dir, state, info, RENEW_FLOOR)
        now = self._now()
        _waking(state, info)
        try:
            sandbox = await self._hold(
                state.sandbox_id, timeout_seconds=self._wake_seconds(state, now + limit)
            )
        except SandboxGone as exc:
            state.kept = False
            _write_state(run_dir, state)
            raise UnknownRunError(f"run {run_id}'s sandbox is gone") from exc
        state.awake_since = state.awake_since or now
        # A sandbox its preview keeps awake ends with the window that preview's wake opened.
        deadline = min(now + limit, self._window_end(state))
        try:
            await self._install_scripts(sandbox)
            session_log = _session_log(state, harness)
            size = None if session_log is None else (await _sizes(sandbox, session_log)).log_size
            state.inspect = _Inspect(
                token=token,
                phase=InspectPhase.STARTING,
                started_at=now,
                deadline=deadline,
                last_activity=now,
                log_size=size,
            )
            # Before the launch: the view's end cuts the log back to this, whoever ends it.
            _write_state(run_dir, state)
            policy = NetworkPolicy(allow=state.allow, inject=launch.inject)
            await self._arm(sandbox, _public_ports(state), policy)
            await self._write_launch(
                sandbox, launch, env, values, argv_path=VIEW_ARGV, env_path=VIEW_ENV
            )
            state.inspect.pid = await sandbox.start(INSPECT)
            await self._take_consent(sandbox, session=INSPECT_SESSION, stderr=VIEW_STDERR)
        except Exception as exc:
            log.warning("starting run %s's session view failed: %s", run_id, exc)
            why = str(exc) if isinstance(exc, SpawnError) else "the session view did not start"
            await self._unstarted(run_dir, state, sandbox, why)
            if isinstance(exc, SpawnError) and exc.reason is SpawnReason.CREDENTIAL:
                raise InspectLoginRefused() from exc
            raise InspectUnavailable(f"opening run {run_id}'s session failed: {why}") from exc
        self._sandboxes[run_id] = sandbox
        _write_state(run_dir, state)
        return await self._read_inspect(run_dir, state, sandbox)

    async def _unstarted(self, run_dir: Path, state: _RunState, sandbox: Sandbox, why: str) -> None:
        """A start that failed: the view ended as any view ends once its record exists, and the
        sandbox parked again."""
        inspect = state.inspect
        if inspect is not None and inspect.phase is InspectPhase.STARTING:
            try:
                await self._end_view(run_dir, state, sandbox, InspectPhase.FAILED, why)
                return
            except Exception as exc:
                log.warning("ending run %s's unstarted view failed: %s", state.run_id, exc)
                failed = {"phase": InspectPhase.FAILED, "detail": why}
                state.inspect = inspect.model_copy(update=failed)
        if await self._settle(sandbox, state) is not None:
            state.kept = False
        _write_state(run_dir, state)

    async def inspect_status(self, run_id: str) -> InspectState:
        run_dir = self._run_dir(run_id)
        state = _read_state(run_dir, run_id)
        inspect = state.inspect
        if inspect is None:
            return InspectState(phase=InspectPhase.STOPPED)
        if inspect.phase not in _INSPECT_AWAKE:
            return inspect.state(state.sandbox_id)
        sandbox, why = await self._awake_sandbox(state)
        if sandbox is None:
            return await self._end_view(run_dir, state, None, InspectPhase.STOPPED, why)
        if self._now() >= inspect.deadline + INSPECT_LIMIT_SLACK:
            why = "stopped at its limit"
            return await self._end_view(run_dir, state, sandbox, InspectPhase.STOPPED, why)
        return await self._read_inspect(run_dir, state, sandbox)

    async def inspect_stop(self, run_id: str) -> None:
        run_dir = self._run_dir(run_id)
        state = _read_state(run_dir, run_id)
        if not _inspect_awake(state):
            return
        sandbox, why = await self._awake_sandbox(state)
        await self._end_view(run_dir, state, sandbox, InspectPhase.STOPPED, why or "stopped")

    async def _read_inspect(
        self, run_dir: Path, state: _RunState, sandbox: Sandbox
    ) -> InspectState:
        """Where an awake view stands, from one read of the root tmux server."""
        inspect = state.inspect
        assert inspect is not None
        try:
            report = await _checked(sandbox, INSPECT_STATUS, "reading the session view")
            alive = inspect.pid in await sandbox.pids()
        except Exception:
            # A cached handle outlives a sandbox E2B's console or its timeout destroyed.
            self._sandboxes.pop(state.run_id, None)
            if await self._require_client().info(state.sandbox_id) is not None:
                raise
            state.kept = False
            why = "its sandbox is gone"
            return await self._end_view(run_dir, state, None, InspectPhase.STOPPED, why)
        shown = _view_report(report.stdout)
        if shown is None:
            if inspect.phase is InspectPhase.STARTING and alive:
                return inspect.state(state.sandbox_id)
            opened = inspect.phase is InspectPhase.OPEN
            phase = InspectPhase.STOPPED if opened else InspectPhase.FAILED
            why = "the session view exited" if opened else "the session view exited as it started"
            return await self._end_view(run_dir, state, sandbox, phase, why)
        clients, activity = shown
        now = self._now()
        seen = [at for at in (inspect.last_activity, min(activity, now)) if at is not None]
        inspect.last_activity = max([*seen, *([now] if clients else [])])
        inspect.clients = clients
        inspect.phase = InspectPhase.OPEN
        _write_state(run_dir, state)
        return inspect.state(state.sandbox_id)

    async def _end_view(
        self,
        run_dir: Path,
        state: _RunState,
        sandbox: Sandbox | None,
        phase: InspectPhase,
        detail: str,
    ) -> InspectState:
        """Record the view's end, then end it and leave the sandbox to whatever still runs in it,
        parking it when nothing does (`_settle`). A park that fails kills it."""
        assert state.inspect is not None
        ended = {"phase": phase, "detail": detail, "clients": 0}
        state.inspect = state.inspect.model_copy(update=ended)
        parked = None
        if sandbox is not None:
            await self._close_view(sandbox, state)
            parked = await self._settle(sandbox, state)
        if parked is not None:
            state.kept = False
            state.inspect = state.inspect.model_copy(update={"detail": f"{detail}\n{parked}"})
        _write_state(run_dir, state)
        return state.inspect.state(state.sandbox_id)

    async def _close_view(self, sandbox: Sandbox, state: _RunState) -> None:
        """`INSPECT_STOP`: the view's CLI hung up and gone, then the session log cut back to the
        size its record holds, and the view's env file and the credential files deleted. A
        preview still in its setup is stopped for its renewal first (`_renew_in_setup`)."""
        await _renew_in_setup(sandbox, state)
        inspect = state.inspect
        size = None if inspect is None else inspect.log_size
        log_path = None if size is None else _session_log(state, self.harness)
        paths = " ".join(f"'{path}'" for path in (*self.harness.credential_paths, VIEW_ENV))
        await _checked(
            sandbox,
            f"{INSPECT_STOP} {paths}",
            "ending the session view",
            envs={"LOG": log_path or "", "SIZE": str(size or 0)},
            timeout_seconds=60,
        )

    # -- sandbox work ----------------------------------------------------------------------

    def _allow(self, spec: RunSpec, *, git_host: str) -> tuple[str, ...]:
        mcp_hosts = [urlsplit(server.url or "").hostname or "" for server in spec.mcp]
        hosts = [
            *GITHUB_HOSTS,
            git_host,
            *self.harness.egress_hosts,
            *mcp_hosts,
            *spec.egress,
            *self._egress,
        ]
        return tuple(dict.fromkeys(host for host in hosts if host))

    def _inject(
        self,
        launch: Launch,
        token: str,
        *,
        git_host: str,
        routes: Sequence[SecretRoute] = (),
        values: Mapping[str, str] | None = None,
    ) -> dict[str, Mapping[str, str]]:
        credential = (
            _gitlab_inject(git_host, token) if _on_gitlab(git_host) else _github_inject(token)
        )
        inject = {**launch.inject, **(credential if token else {})}
        return _with_routes(inject, routes, values or {})

    def _metadata(self, run_id: str, spec: RunSpec) -> dict[str, str]:
        # The reaper's key: a sandbox belongs to one deployment and one run.
        return {
            "factorial_run": run_id,
            "factorial_deployment": self._deployment,
            "factorial_kind": self.kind,
            "ticket": spec.ticket[:64],
            "agent": spec.agent[:64],
            **({"factorial_org": spec.organization[:64]} if spec.organization else {}),
        }

    async def _take_consent(
        self, sandbox: Sandbox, *, session: str = TMUX_SESSION, stderr: str = STDERR
    ) -> None:
        """Takes the screen the CLI asks on first, at every launch of its TUI, in the tmux
        `session` it runs in, its stderr appended to `stderr`; `SpawnError`, the TUI hung up,
        when the launch did not reach its prompt."""
        harness = self.harness
        if harness.consent_script is None:
            return
        name = harness.name
        taken = await sandbox.run(
            harness.consent_script,
            envs={"TMUX_TARGET": session},
            timeout_seconds=CONSENT_TIMEOUT,
        )
        if taken.ok:
            return
        logged = await _launch_log(sandbox, harness)
        if taken.exit_code == CONSENT_NOT_ASKED:
            why = f"`{name}` put up no consent screen at its launch"
        elif taken.exit_code == CONSENT_NOT_TAKEN:
            why = f"`{name}` did not reach its prompt once its consent screen was taken"
        else:
            # The TUI is gone, or tmux would not answer: what the CLI said is on its stderr.
            said = (await sandbox.run(f"tail -c 400 {stderr}")).stdout.strip()
            raise SpawnError(
                f"`{name}` did not reach its consent screen (exit {taken.exit_code}): "
                f"{taken.stderr.strip()[-400:]} {said}".strip()
                + logged,
                reason=SpawnReason.INFRASTRUCTURE,
            )
        raise await self._unlaunched(sandbox, why + logged, taken.stdout)

    async def _find_session(self, sandbox: Sandbox) -> tuple[str, str]:
        """The session a CLI that names its own began at this launch, and its log: the one
        `ROLLOUT_FIND` lists, once its first line is whole and names the session its name does.
        Otherwise, or when none comes within `SESSION_ID_WAIT`, `SpawnError`, the TUI hung up."""
        harness = self.harness
        name = harness.name
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SESSION_ID_WAIT
        while True:
            found = await _checked(sandbox, ROLLOUT_FIND, "finding the session log")
            logs = found.stdout.splitlines()
            why = None
            if len(logs) > 1:
                why = f"`{name}` began {len(logs)} session logs at its launch, not one"
            elif logs:
                head = await _log_head(sandbox, logs[0])
                line, whole, _ = head.partition(b"\n")
                if whole:
                    session = harness.session_found(logs[0], line.decode("utf-8", "replace"))
                    if session is not None:
                        return session, logs[0]
                    why = (
                        f"the session log `{name}` began, {logs[0][:200]!r}, does not name in "
                        "its first line the session its name does"
                    )
                elif len(head) >= READ_LIMIT:
                    why = f"`{name}`'s session log begins with a line over {READ_LIMIT} bytes"
            if why is not None:
                raise await self._unlaunched(sandbox, why)
            if loop.time() >= deadline:
                screen = (await sandbox.run(CAPTURE)).stdout
                within = f"within {SESSION_ID_WAIT:.0f}s of its launch"
                why = (
                    f"`{name}`'s session log's first line was not whole {within}"
                    if logs
                    else f"`{name}` began no session log {within}"
                )
                raise await self._unlaunched(sandbox, why, screen)
            await asyncio.sleep(SESSION_ID_POLL)

    async def _unlaunched(
        self, sandbox: Sandbox, why: str, screen: str | None = None
    ) -> SpawnError:
        """A launch that did not reach its prompt, or began a session it cannot be held to, hung
        up. With its `screen`, the error names what that shows: a dialog the seeds did not
        answer."""
        harness = self.harness
        try:
            await _checked(
                sandbox,
                harness.finish_script,
                "hanging the session up",
                envs={"MODE": "hangup"},
                timeout_seconds=60,
            )
        except Exception as exc:
            log.warning("hanging up a launch that did not reach its prompt failed: %s", exc)
        if screen is None:
            return SpawnError(why, reason=SpawnReason.INFRASTRUCTURE)
        why = f"{why}; its screen shows {_screen_line(screen)}"
        if any(screen_text in screen for screen_text in harness.sign_in):
            return SpawnError(
                f"{login_refused(harness.label)}\n\n{why}.", reason=SpawnReason.CREDENTIAL
            )
        return SpawnError(why, reason=SpawnReason.INFRASTRUCTURE)

    async def _install_scripts(self, sandbox: Sandbox) -> None:
        """This adapter's constant scripts, written again on every use of a sandbox, so a
        sandbox made by an older adapter still runs today's."""
        scripts = {path: text.encode() for path, text in SCRIPTS.items()}
        await sandbox.write_many({**scripts, TMUX_CONF: TMUX_LOCKDOWN.encode()})
        await _checked(
            sandbox,
            f"chmod 755 {' '.join(SCRIPTS)} && chmod 644 {TMUX_CONF}",
            "installing the scripts",
        )

    async def _prepare(self, sandbox: Sandbox, spec: RunSpec) -> dict[str, str]:
        """Install the scripts, ensure the agent's uid and check out `base`. Returns the parts of
        the template's own environment the agent needs (`_SANDBOX_ENV`)."""
        await self._install_scripts(sandbox)
        setup = await sandbox.run(
            SETUP,
            envs={
                "REPO": spec.workspace.repo,
                "BASE": spec.workspace.base,
                **({"CONFIG_SHA": sha} if (sha := pinned_config_sha(spec.workspace)) else {}),
            },
            timeout_seconds=600,
        )
        detail = setup.stderr.strip()[-400:]
        if setup.exit_code == SETUP_CONFIG_FETCH_FAILED:
            if any(s in setup.stderr for s in ("not our ref", "couldn't find remote ref")):
                raise SpawnError(
                    f"{config_gone(spec.workspace.config_sha)}\n\n{detail}",
                    reason=SpawnReason.WORKSPACE,
                )
            raise SpawnError(
                f"the pinned config commit {spec.workspace.config_sha} could not be fetched into "
                f"the sandbox: {detail}",
                reason=SpawnReason.INFRASTRUCTURE,
            )
        if not setup.ok:
            # Only a ref git proved absent is the spec's fault; a DNS, egress or GitHub failure
            # is the next tick's to retry.
            if "couldn't find remote ref" in setup.stderr:
                raise SpawnError(
                    f"The base branch {spec.workspace.base} doesn't exist. Push it, or fix "
                    f"workspace.base in the process file.\n\n{detail}",
                    reason=SpawnReason.WORKSPACE,
                )
            raise SpawnError(
                f"checking out {spec.workspace.repo}@{spec.workspace.base} in the sandbox failed: "
                f"{detail}",
                reason=SpawnReason.INFRASTRUCTURE,
            )
        return await self._template_env(sandbox)

    async def _template_env(self, sandbox: Sandbox) -> dict[str, str]:
        """The parts of the template's own environment the agent needs (`_SANDBOX_ENV`)."""
        printed = await _checked(sandbox, "env -0", "reading the template's environment")
        found: dict[str, str] = {}
        for entry in printed.stdout.split("\0"):
            key, sep, value = entry.partition("=")
            if sep and key in _SANDBOX_ENV:
                found[key] = value
        return found

    def _agent_env(
        self,
        launch: Launch,
        sandbox_env: Mapping[str, str],
        token: str,
        routes: Sequence[SecretRoute] = (),
        *,
        bot_login: str,
        git_host: str,
    ) -> dict[str, str]:
        env = {
            **sandbox_env,
            "HOME": AGENT_HOME,
            "USER": AGENT_USER,
            "LANG": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0",
            **agent_git_identity(bot_login),
            **launch.env,
        }
        if _on_gitlab(git_host):
            env |= GLAB_QUIET_ENV
        if token:
            env[ENV_GH_TOKEN] = PLACEHOLDER_GH_TOKEN
            if _on_gitlab(git_host):
                env |= {"GITLAB_TOKEN": PLACEHOLDER_GITLAB_TOKEN, "GITLAB_HOST": git_host}
        _refuse_shadowing((route.name for route in routes), env)
        env |= dict.fromkeys((route.name for route in routes), PLACEHOLDER_ROUTED)
        return env

    async def _write_files(
        self, sandbox: Sandbox, files: Mapping[str, tuple[bytes, str, int]]
    ) -> None:
        # One write per owner and one chmod for all: each call is a round trip to the sandbox.
        by_owner: dict[str, dict[str, bytes]] = {}
        for path, (content, owner, _) in files.items():
            by_owner.setdefault(owner, {})[path] = content
        modes = [part for path, (_, _, mode) in files.items() for part in (f"{mode:o}", path)]
        by_owner.setdefault("root", {})[MODES] = _nul(modes)
        for owner, batch in by_owner.items():
            await sandbox.write_many(batch, user=owner)
        await _checked(sandbox, f"xargs -0 -n 2 chmod < {MODES}", "setting the files' modes")

    async def _write_launch(
        self,
        sandbox: Sandbox,
        launch: Launch,
        env: Mapping[str, str],
        secrets: Mapping[str, str],
        *,
        argv_path: str = ARGV,
        env_path: str = ENV,
        exposed: Mapping[str, str] | None = None,
    ) -> None:
        # The environment is the one place a harness could leak a value into the sandbox's
        # process table and the run state; credentials go by proxy or file, never here.
        # `exposed` is the operator's exception: secrets the agent may read, written to the env
        # file alone, never into `env`, which the run state keeps. The bot login and the git host
        # travel with the secrets but are no credentials: the commit identity and glab's
        # `GITLAB_HOST` are made of them.
        credentials = [
            value for key, value in secrets.items() if key not in (ENV_BOT_LOGIN, ENV_GIT_HOST)
        ]
        leaked = sorted(
            key
            for key, value in env.items()
            if any(len(secret) >= 8 and secret in value for secret in credentials)
        )
        if leaked:
            raise SpawnError(
                f"the agent environment would carry a secret in {', '.join(leaked)}",
                reason=SpawnReason.CONFIG,
            )
        _refuse_shadowing(exposed or {}, env)
        await self._write_files(
            sandbox,
            {
                **launch.files,
                argv_path: (_nul(launch.argv), "root", 0o600),
                env_path: (
                    _nul([f"{key}={value}" for key, value in {**env, **(exposed or {})}.items()]),
                    "root",
                    0o600,
                ),
            },
        )

    # -- supervision -----------------------------------------------------------------------

    def _supervise(
        self,
        run_dir: Path,
        state: _RunState,
        sandbox: Sandbox,
        probe_env: Mapping[str, str] | None,
    ) -> None:
        self._sandboxes[state.run_id] = sandbox
        supervisor = self._supervisor(run_dir, state, sandbox, probe_env)
        self._states[state.run_id] = state
        task = asyncio.create_task(supervisor.run(), name=f"{self.kind}-{state.run_id}")
        task.add_done_callback(_report_crash)
        self._supervisors[state.run_id] = task

    def _supervisor(
        self,
        run_dir: Path,
        state: _RunState,
        sandbox: Sandbox,
        probe_env: Mapping[str, str] | None,
    ) -> _Supervisor:
        assert state.session_id is not None
        # The state is written after the mirrors, so a mirror can hold a read the state lost.
        mirror.truncate(run_dir / MIRROR_FILE, state.offset)
        mirror.truncate(run_dir / SESSION_MIRROR, state.log_read - state.log_offset)
        mirror.truncate(run_dir / SIGNALS_MIRROR, state.signals_offset - state.signals_start)
        reader, replayed = self._reader(run_dir, state)
        harness = self.harness
        if state.transcript_version == harness.transcript_version:
            # The translator holds a message whose lines are still arriving: rebuilt from the
            # segment's raw lines, keeping only what the mirror does not hold yet.
            transcript = harness.transcript(
                state.session_id,
                turn=state.segment_turn,
                cost_base=state.cost_base,
                skip=replayed,
            )
            pending = _replay(run_dir, state, transcript)
        else:
            transcript = harness.transcript(
                state.session_id,
                turn=state.segment_turn and replayed == 0,
                cost_base=state.cost_base,
                skip=min(replayed, 1),
            )
            pending = []
            state.transcript_version = harness.transcript_version
            state.stream_warnings = merge_warnings(state.stream_warnings, (TRANSLATOR_CHANGED,))
        supervisor = _Supervisor(self, run_dir, state, sandbox, reader, probe_env, transcript)
        supervisor._emit(pending)
        return supervisor

    def _reader(self, run_dir: Path, state: _RunState) -> tuple[Reader, int]:
        """The segment's reader, and how many mirrored lines it was rebuilt from."""
        reader = self.harness.reader(state.turns)
        translated = run_dir / MIRROR_FILE
        replayed = 0
        if translated.exists() and state.offset > state.segment_start:
            # A worker restart: the segment's lines so far rebuild the reader, never re-count.
            with translated.open("rb") as handle:
                handle.seek(state.segment_start)
                for raw in handle.read(state.offset - state.segment_start).splitlines():
                    reader.feed(raw.decode("utf-8", errors="replace"))
                    replayed += 1
        return reader, replayed

    async def drain(self) -> None:
        """Wait for every live supervisor. Tests and smokes only."""
        pending = [task for task in self._supervisors.values() if not task.done()]
        if pending:
            await asyncio.gather(*pending)

    async def _stop_run(
        self, run_id: str, *, interrupt: bool, reason: ParkReason = ParkReason.USER
    ) -> None:
        run_dir = self._run_dir(run_id)
        state = _read_state(run_dir, run_id)
        if state.status.is_terminal:
            return  # idempotent
        attached = self._attached(run_id)
        live = attached[2] if attached is not None else state
        if live.stopping is not RunStatus.INTERRUPTED:
            # An interrupt already asked for keeps the sandbox, whatever stop follows it.
            live.stopping = RunStatus.INTERRUPTED if interrupt else RunStatus.CANCELLED
            if interrupt:
                live.park_reason = reason
        _write_state(run_dir, live)
        # Only a supervisor ends a segment with the result line its transcript owes.
        if (attached := await self._attach(run_id)) is None:
            return
        supervisor, sandbox, _ = attached
        # Hung up rather than stopped: the CLI writes its cost on the way out.
        try:
            await _checked(
                sandbox,
                self.harness.finish_script,
                "stopping the agent",
                envs={"MODE": "hangup"},
                timeout_seconds=60,
            )
        except Exception as exc:
            if await _landed(run_dir, live, sandbox, supervisor):
                return
            raise AdapterUnavailable(f"cannot stop run {run_id}: {exc}") from exc
        # Returned only once the supervisor has finalised and the sandbox is parked or killed, so
        # a resume issued right after never races the scrub.
        try:
            await asyncio.wait_for(asyncio.shield(supervisor), timeout=STOP_WAIT)
        except TimeoutError as exc:
            log.error("run %s did not finalise within %.0fs of its stop", run_id, STOP_WAIT)
            raise AdapterUnavailable(
                f"run {run_id} did not finalise within {STOP_WAIT:.0f}s of its stop; the "
                "stop is retried"
            ) from exc

    def _attached(self, run_id: str) -> tuple[asyncio.Task[None], Sandbox, _RunState] | None:
        """The run's live supervisor, its sandbox and the state it writes, when one runs."""
        supervisor, sandbox = self._supervisors.get(run_id), self._sandboxes.get(run_id)
        state = self._states.get(run_id)
        if supervisor is None or supervisor.done() or sandbox is None or state is None:
            return None
        return supervisor, sandbox, state

    def _live_state(self, run_dir: Path, run_id: str) -> _RunState:
        """The run's state: the object its live supervisor writes, when one runs here, so a
        preview beside the run and the supervisor never write each other's record away; else the
        file."""
        supervisor, state = self._supervisors.get(run_id), self._states.get(run_id)
        if supervisor is not None and not supervisor.done() and state is not None:
            return state
        return _read_state(run_dir, run_id)

    async def _attach(self, run_id: str) -> tuple[asyncio.Task[None], Sandbox, _RunState] | None:
        """`_attached`, reattaching a supervisor first after a worker restart. None when the run
        turned out to have ended, its sandbox gone."""
        if (attached := self._attached(run_id)) is not None:
            return attached
        status = await self.status(run_id)
        if (attached := self._attached(run_id)) is not None:
            return attached
        if status.is_terminal:
            return None
        raise AdapterUnavailable(f"cannot reach the sandbox of run {run_id}")

    async def _park(self, sandbox: Sandbox, state: _RunState) -> str | None:
        """Keep a run's sandbox: stop everything of the agent's, delete its credential files and
        the preview's env, drop the injected headers and pause. A step that fails kills the
        sandbox instead, and why is returned: a sandbox is never left paused holding a
        credential, and a run whose sandbox is gone must not claim it can be resumed."""
        renewing = _preview_awake(state)
        if renewing:
            # Nothing parks a sandbox under an awake preview but a renewal or a failure: the
            # worker starts the preview again (`worker.previews`).
            assert state.preview is not None
            renewal = {"phase": PreviewPhase.STOPPED, "detail": PREVIEW_RENEWAL, "urls": ()}
            state.preview = state.preview.model_copy(update=renewal)
        self._sandboxes.pop(state.run_id, None)
        # Every harness's, since the reaper parks a stray of any kind; the preview's env file,
        # whose values are preview-only, and still secrets, with setup's beside it; and a view's,
        # as a view's end does.
        scrubbed = (
            *sorted({*self.harness.credential_paths, *_CREDENTIAL_PATHS}),
            PREVIEW_ENV,
            PREVIEW_SETUP_ENV,
            VIEW_ENV,
            ENV,
        )
        paths = " ".join(f"'{path}'" for path in scrubbed)
        try:
            await _checked(sandbox, f"{SCRUB} {paths}".strip(), "the scrub", timeout_seconds=60)
            await sandbox.update_network(NetworkPolicy(allow=state.allow))
            await self._require_client().pause(state.sandbox_id)
        except Exception as exc:
            log.error("parking run %s failed, so its sandbox is killed: %s", state.run_id, exc)
            await self._kill(state.sandbox_id, state.run_id)
            why = f"the sandbox could not be kept, so it was killed: {exc}"
            if renewing:
                # No longer a renewal: nothing is left to start the preview again in.
                assert state.preview is not None
                failed = {"detail": f"{PREVIEW_RENEWAL}\n{why}"}
                state.preview = state.preview.model_copy(update=failed)
            return why
        state.awake_since = None
        _release(state.sandbox_id)
        return None

    async def _kill(self, sandbox_id: str, run_id: str) -> None:
        self._sandboxes.pop(run_id, None)
        _release(sandbox_id)
        try:
            await self._require_client().kill(sandbox_id)
        except Exception as exc:
            # The reaper finds it by its metadata once the run is no longer wanted.
            log.warning("killing sandbox %s of run %s failed: %s", sandbox_id, run_id, exc)

    def _gone(self, run_dir: Path, state: _RunState, seen: str) -> _RunState:
        """The sandbox no longer exists. Past its deadline E2B's own timeout fired; before it,
        something else destroyed it, and the run cannot have finished."""
        detail = f"\n\nSandbox {state.sandbox_id}: {seen}."
        if self._now() >= state.deadline:
            return _finalise(
                run_dir,
                state,
                RunStatus.TIMEOUT,
                failure_reason=f"The sandbox reached its time limit and was shut down.{detail}",
            )
        return _finalise(
            run_dir,
            state,
            RunStatus.FAILED,
            failure_reason=f"The sandbox stopped before the agent finished.{detail}",
        )

    def _remaining(self, state: _RunState) -> int:
        # No margin: the session's own backstop parks it before its cap. The floor can ask for up
        # to a minute past the cap on a last-minute reattach, which is what lets that park finish.
        return max(int((state.deadline - self._now()).total_seconds()), 60)

    def _require_root(self) -> Path:
        if self._root is None:
            raise AdapterUnavailable(
                f"{self.kind} requires FACTORIAL_RUNTIME__E2B__STATE_ROOT: the mirror of every "
                "run's stream lives there, and the API reads it"
            )
        return self._root

    def _require_client(self) -> SandboxClient:
        if self._client is None:
            raise AdapterUnavailable(
                f"{self.kind} holds no E2B key: store the runtime's E2B_API_KEY"
            )
        return self._client

    def _run_dir(self, run_id: str) -> Path:
        root = self._require_root()
        run_dir = (root / "runs" / run_id).resolve()
        if run_dir.parent != (root / "runs").resolve():
            raise UnknownRunError(f"run id {run_id!r} escapes the state root")
        return run_dir


def _new_run_id(spec: RunSpec) -> str:
    return f"{path_slug(spec.ticket)}-{path_slug(spec.agent)}-{uuid.uuid4().hex[:8]}"


async def _realpaths(sandbox: Sandbox) -> tuple[str, str]:
    """The checkout's and the config dir's resolved paths, read in one round trip."""
    real = await _checked(sandbox, REALPATHS, "resolving the checkout and the config dir")
    paths = real.stdout.split("\n")
    if len(paths) != 3 or paths[2] or not all(paths[:2]):
        raise AgentRuntimeError(f"`{REALPATHS}` printed {real.stdout[:200]!r}, not two paths")
    return paths[0], paths[1]


def _poll_envs(log: str, *, log_from: int = 0, sig_from: int = 0, limit: int = 0) -> dict[str, str]:
    return {"LOG": log, "LOG_FROM": str(log_from), "SIG_FROM": str(sig_from), "LIMIT": str(limit)}


async def _sizes(sandbox: Sandbox, log: str) -> SessionPoll:
    """The session log's size, None when it is absent, and the signals file's; no content."""
    output = await _checked(
        sandbox,
        SESSION_POLL,
        "reading the session's files",
        envs=_poll_envs(log),
    )
    return read_session_poll(output.stdout)


async def _log_head(sandbox: Sandbox, path: str) -> bytes:
    """A session log's first `READ_LIMIT` bytes, read as the agent."""
    output = await _checked(
        sandbox,
        SESSION_POLL,
        "reading the session log",
        envs=_poll_envs(path, limit=READ_LIMIT),
    )
    return read_session_poll(output.stdout).log


async def _launch_log(sandbox: Sandbox, harness: Harness) -> str:
    """`; its own log ends <quoted>`, the end of the TUI's `launch_log`, for a failed launch's
    error; nothing when it keeps none, or the log is absent or unreadable. The log is the
    agent's: read as the agent, its control characters blanked, and only ever quoted."""
    if harness.launch_log is None:
        return ""
    try:
        sizes = await _checked(
            sandbox, SESSION_POLL, "sizing the TUI's log", envs=_poll_envs(harness.launch_log)
        )
        polled = read_session_poll(sizes.stdout)
        if not polled.log_size:
            return ""
        # Bytes enough for the characters kept, at UTF-8's widest.
        start = max(polled.log_size - 4 * LAUNCH_LOG_TAIL, 0)
        envs = _poll_envs(
            harness.launch_log,
            log_from=start,
            sig_from=polled.signals_size,
            limit=polled.log_size - start,
        )
        tail = await _checked(sandbox, SESSION_POLL, "reading the TUI's log", envs=envs)
        text = read_session_poll(tail.stdout).log.decode("utf-8", errors="replace")
    except Exception as exc:
        log.warning("reading the TUI's log after a failed launch failed: %s", exc)
        return ""
    lines = (_CONTROL.sub(" ", line).strip() for line in text.splitlines())
    shown = " | ".join(line for line in lines if line)[-LAUNCH_LOG_TAIL:]
    return f"; its own log ends {shown!r}" if shown else ""


def _screen_line(screen: str) -> str:
    """The first dialog line on a TUI's screen, or else its first line of text, quoted, without
    the box its frame draws: a codex screen begins with its banner."""
    lines = [
        line.strip(" \t\u2500\u2502\u256d\u256e\u256f\u2570")
        for line in screen.replace("\xa0", " ").splitlines()
    ]
    texts = [text for text in lines if any(char.isalnum() for char in text)]
    shown = next((text for text in texts if DIALOGS.search(text)), texts[0] if texts else None)
    return "nothing" if shown is None else repr(shown[:200])


async def _landed(
    run_dir: Path, state: _RunState, sandbox: Sandbox, supervisor: asyncio.Task[None]
) -> bool:
    """Whether a session's FINISH that failed ended it anyway. finish.sh's tail runs after the
    CLI left, so the supervisor can park the sandbox under it, which the substrate reports as
    the command failing, or never answers. A session still running was not ended: that fails
    at once. Otherwise the run must be finalised, which a supervisor that died never does."""
    try:
        if state.pid in await sandbox.pids():
            return False
    except Exception:
        pass  # a parked sandbox answers nothing: the supervisor says
    await asyncio.wait({supervisor}, timeout=STOP_WAIT)
    return _read_state(run_dir, state.run_id).status.is_terminal


def _begin_segment(state: _RunState, cap: datetime, *, turn: bool, version: int) -> None:
    """A launch of the session's CLI: its translator, at the harness's `version`, starts where
    the mirrors and the log stand, and its cost from the running total."""
    state.deadline = cap
    state.segments += 1
    state.segment_turn = turn
    state.segment_start = state.offset
    state.segment_log_start = state.log_read
    state.cost_base = state.cost_seen
    state.usage_base = state.live_usage
    state.transcript_version = version
    state.signals.start_segment(turn=turn)


def _requested(state: _RunState) -> tuple[RunStatus, str]:
    """The outcome and the reason a stop someone asked for ends the run with."""
    if state.stopping is not RunStatus.INTERRUPTED:
        return RunStatus.CANCELLED, "cancelled by the operator"
    if state.parked_at_cap:
        return RunStatus.INTERRUPTED, SESSION_CAP_REASON
    return RunStatus.INTERRUPTED, _PARKED[state.park_reason or ParkReason.USER]


_PARKED: Final = {
    ParkReason.USER: "Interrupted by a person. Its sandbox is kept for a resume.",
    ParkReason.IDLE: "Paused while it waits for a person. Its sandbox is kept for a resume.",
    ParkReason.ROLLOVER: "Paused to renew its token. Its sandbox is kept for a resume.",
    ParkReason.CEILING: "Paused at its session time limit. Its sandbox is kept for a resume.",
    ParkReason.EXITED: "Quit from its terminal. Its sandbox is kept for a resume.",
}


_AWAKE: Final = (PreviewPhase.STARTING, PreviewPhase.READY)
_INSPECT_AWAKE: Final = (InspectPhase.STARTING, InspectPhase.OPEN)


def _preview_awake(state: _RunState) -> bool:
    return state.preview is not None and state.preview.phase in _AWAKE


def _inspect_awake(state: _RunState) -> bool:
    return state.inspect is not None and state.inspect.phase in _INSPECT_AWAKE


def _serving(state: _RunState) -> bool:
    """An awake preview past its setup: a status read saw the setup done."""
    preview = state.preview
    return _preview_awake(state) and preview is not None and preview.serving_since is not None


async def _renew_in_setup(sandbox: Sandbox, state: _RunState) -> None:
    """Before a stop of the agent's uid: a preview still in its setup runs that setup as the
    agent's uid, so the stop would cut it and the preview would read as failed. It is stopped
    first and recorded for its renewal, and the worker starts it again; setup re-runs, which a
    setup must be safe to retry for."""
    if _preview_awake(state) and not _serving(state):
        await _renew_preview(sandbox, state)


async def _renew_preview(sandbox: Sandbox, state: _RunState) -> None:
    """Stop an awake preview and record it for its renewal: the worker starts it again."""
    preview = state.preview
    assert preview is not None
    await _stop_services(sandbox, preview)
    renewal = {"phase": PreviewPhase.STOPPED, "detail": PREVIEW_RENEWAL, "urls": ()}
    state.preview = preview.model_copy(update=renewal)


def _public_ports(state: _RunState) -> tuple[int, ...]:
    """The ports the firewall admits and keeps the preview to: the run's, or for a run kept before
    they were recorded, those of the preview an older adapter started in it."""
    if state.public_ports or state.preview is None:
        return state.public_ports
    return tuple(service.port for service in state.preview.services)


def _waking(state: _RunState, info: SandboxInfo) -> None:
    """Before a connect: a sandbox `info` found paused wakes into a new window of E2B's cap, so the
    wake the state last recorded is over. A running one keeps the window its last wake opened."""
    if info.state != "running":
        state.awake_since = None


async def _stop_services(sandbox: Sandbox, preview: _Preview) -> None:
    """End a preview's processes without parking: its root supervisor first, so that it cannot
    start a service after they are gone, then everything of the preview's uid. A setup step it
    was running is the agent's uid's, which this leaves alone, as it does the agent's and an open
    session's processes (design.md §7.10)."""
    if preview.pid is not None:
        await sandbox.run('kill "$P" 2>/dev/null || true', envs={"P": str(preview.pid)})
    await _checked(sandbox, PREVIEW_STOP, "stopping the preview", timeout_seconds=60)


def _require_inspectable(state: _RunState) -> None:
    run_id = state.run_id
    if not state.status.is_terminal:
        raise InspectUnavailable(
            f"run {run_id}'s agent is still running; its session opens once it stops"
        )
    if state.continued_by is not None:
        raise InspectUnavailable(
            f"run {state.continued_by} continued in run {run_id}'s workspace; open that run's "
            "session instead"
        )
    if not state.began_session:
        raise InspectNoSession()
    if not state.kept:
        raise UnknownRunError(f"run {run_id}'s sandbox was not kept, so its session cannot open")


def _session_log(state: _RunState, harness: Harness) -> str | None:
    """The session log's sandbox path: as the spawn found it, or as the harness derives it."""
    assert state.session_id is not None
    return state.session_log or harness.session_log(state.session_id)


def _view_report(stdout: str) -> tuple[int, datetime] | None:
    """`INSPECT_STATUS`'s line: the clients attached and the session's last activity, or None
    when the session is gone. Anything else is not that script's, and raises ValueError."""
    words = stdout.split()
    if words == ["closed"]:
        return None
    if len(words) != 4 or words[0] != "open" or not all(w.isdigit() for w in words[1:]):
        raise ValueError(f"the session view's status read printed {stdout[:80]!r}")
    return int(words[1]), datetime.fromtimestamp(int(words[2]), UTC)


def _require_servable(state: _RunState) -> None:
    if state.continued_by is not None:
        raise PreviewUnavailable(
            f"run {state.run_id}'s workspace was taken over by its rework, run "
            f"{state.continued_by}: preview that run instead"
        )
    if not state.public:
        raise PreviewUnavailable(
            f"run {state.run_id} was not kept for a preview: its step declared no preview "
            "services when it was spawned"
        )
    if state.status.is_terminal and not state.kept:
        raise PreviewUnavailable(
            f"run {state.run_id} was not kept for a preview: its sandbox could not be kept"
        )


def _preview_files(
    state: _RunState, spec: PreviewSpec, env: Mapping[str, SecretStr]
) -> dict[str, tuple[bytes, str, int]]:
    """The preview's inputs as root's files. `env` values are preview-only by rule: they reach
    agent-written code, and only the services' uid, never setup's (design.md §7.10)."""
    sandbox_env = {key: value for key, value in state.agent_env.items() if key in _SANDBOX_ENV}
    setup = {**sandbox_env, "HOME": AGENT_HOME, "USER": AGENT_USER, "LANG": "C.UTF-8"}
    services_env = {
        **sandbox_env,
        "HOME": PREVIEW_HOME,
        "USER": PREVIEW_USER,
        "LANG": "C.UTF-8",
        **{key: value.get_secret_value() for key, value in env.items()},
    }
    services = [
        part
        for service in spec.services
        for part in (service.name, str(service.port), service.ready_path, service.command)
    ]
    return {
        PREVIEW_SETUP_ENV: (_nul([f"{k}={v}" for k, v in setup.items()]), "root", 0o600),
        PREVIEW_ENV: (_nul([f"{k}={v}" for k, v in services_env.items()]), "root", 0o600),
        PREVIEW_SETUP: (_nul(spec.setup), "root", 0o600),
        PREVIEW_SERVICES: (_nul(services), "root", 0o600),
    }


def _report_crash(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        # The run stays non-terminal on disk, so the next `status()` supervises it again.
        log.error("supervising %s crashed", task.get_name(), exc_info=exc)


def _finalise(
    run_dir: Path,
    state: _RunState,
    outcome: RunStatus,
    *,
    failure_reason: str | None = None,
    **fields: object,
) -> _RunState:
    # In place: a preview beside the run may hold this object across the run's end, and must
    # not write back the status it read.
    update = {"status": outcome, "failure_reason": failure_reason, **fields}
    for name, value in update.items():
        setattr(state, name, value)
    _write_state(run_dir, state)
    return state


class _Supervisor(SessionReader[_RunState, Reader]):
    """Mirrors and translates one segment of a session, sets RUNNING or IDLE from its signals,
    enforces the turn cap and the cap's backstop, and finalises it once the CLI is gone."""

    _signals_mirror = SIGNALS_MIRROR
    _session_mirror = SESSION_MIRROR
    _translated_mirror = MIRROR_FILE

    def __init__(
        self,
        adapter: E2bAdapter,
        run_dir: Path,
        state: _RunState,
        sandbox: Sandbox,
        reader: Reader,
        probe_env: Mapping[str, str] | None,
        transcript: SessionTranscript,
    ) -> None:
        assert state.session_id is not None
        harness = adapter.harness
        self._adapter = adapter
        self._run_dir = run_dir
        self._state = state
        self._sandbox = sandbox
        self._reader = reader
        self._probe_env = probe_env
        self._limit = self._read_limit = READ_LIMIT
        self._max_line = MAX_LINE
        self._override = None
        self._transcript = transcript
        self._log_signals = harness.log_signals
        self._turnless = harness.turnless_warnings
        self._parked_reasons = _PARKED
        if not harness.chooses_session_id:
            # A CLI that names its own session keeps it for the session's life; a hook naming
            # another is another thread's, such as a sub-agent's.
            self._hook_session = state.session_id
        log = _session_log(state, harness)
        if log is None:
            raise AgentRuntimeError(
                f"run {state.run_id} recorded no session log, so its session cannot be read"
            )
        self._log = log
        self._cli_label = harness.label

    async def run(self) -> None:
        failures = 0
        exit_code: int | None = None
        stderr = ""
        while True:
            try:
                more = await self._poll()
                if self._override is None:
                    await self._enforce()
                if more:
                    continue
                if self._state.pid not in await self._sandbox.pids():
                    # The CLI is gone, so a line it left unterminated is as whole as it gets.
                    for _ in range(FINAL_READS):
                        if not await self._poll(last=True):
                            break
                    exit_code, stderr = await self._exit()
                    break
                failures = 0
            except TornPoll:
                raise
            except Exception as exc:
                failures += 1
                log.warning("polling session %s failed: %s", self._state.run_id, exc)
                if failures >= POLL_FAILURES_BEFORE_CHECK and await self._vanished():
                    return
            await asyncio.sleep(self._adapter._poll)
        await self._finish(exit_code, stderr)

    async def _read(self) -> SessionPoll:
        state = self._state
        output = await self._sandbox.run(
            SESSION_POLL,
            envs=_poll_envs(
                self._log,
                log_from=state.log_read,
                sig_from=state.signals_offset,
                limit=self._limit,
            ),
        )
        if not output.ok:
            detail = output.stderr.strip()[-400:]
            raise AgentRuntimeError(f"the session poll failed (exit {output.exit_code}): {detail}")
        return read_session_poll(output.stdout)

    def _observed(self) -> datetime:
        return self._adapter._now()

    def _save(self) -> None:
        _write_state(self._run_dir, self._state)

    async def _enforce(self) -> None:
        state = self._state
        cap = state.max_turns
        if self._reader.over_cap(cap):
            await self._stop(RunStatus.FAILED, turn_cap_reason(cap))
        elif self._adapter._now() >= state.deadline - CAP_BACKSTOP:
            if state.stopping is None:
                # Persisted first: the hangup may fail, or the worker restart before it lands.
                state.stopping, state.park_reason = RunStatus.INTERRUPTED, ParkReason.ROLLOVER
                state.parked_at_cap = True
                _write_state(self._run_dir, state)
            # Sent every poll until it lands, by this worker or the next: at the cap E2B kills
            # the sandbox and the workspace with it.
            await self._stop(*_requested(state))

    async def _stop(self, outcome: RunStatus, reason: str) -> None:
        # A hangup rather than STOP: the CLI writes its cost on the way out.
        await _checked(
            self._sandbox,
            self._adapter.harness.finish_script,
            "hanging the session up",
            envs={"MODE": "hangup"},
            timeout_seconds=60,
        )
        self._override = (outcome, reason)

    async def _vanished(self) -> bool:
        try:
            info = await self._adapter._require_client().info(self._state.sandbox_id)
        except Exception as exc:
            log.warning("E2B unreachable while checking run %s: %s", self._state.run_id, exc)
            return False
        if info is not None:
            return False
        self._adapter._gone(self._run_dir, self._state, "vanished while supervised")
        return True

    async def _exit(self) -> tuple[int | None, str]:
        """The runner's exit code and the stderr tail. `None` when the runner itself died before
        writing one; an E2B failure raises, and the poll reads it again."""
        printed = await self._sandbox.run(f"cat {EXIT_CODE}")
        stderr = (await self._sandbox.run(f"tail -c 4000 {STDERR}")).stdout
        if not printed.ok:
            return None, stderr
        try:
            return int(printed.stdout.strip()), stderr
        except ValueError:
            log.warning("run %s wrote an unreadable exit code", self._state.run_id)
            return None, stderr

    async def _finish(self, exit_code: int | None, stderr: str) -> None:
        self._end_segment()
        adapter, state = self._adapter, self._state
        outcome = self._outcome(exit_code, stderr)
        status, reason = self._verdict(outcome, exit_code)

        artifacts: tuple[Artifact, ...] = ()
        if self._probe_env is not None:
            artifacts = await discover_artifacts(
                adapter._cli, state, self._probe_env, git_at=()
            )
            if status is RunStatus.SUCCEEDED and state.expected:
                status, reason = expectation_verdict(
                    state, status, reason, artifacts, env=self._probe_env
                )

        # Parked, or left to its preview, before the status is written, so INTERRUPTED always
        # means resumable.
        async with adapter._fate(state.run_id):
            parked = await adapter._end_run(self._sandbox, state)
            state.kept = parked is None
            if parked is not None and status is RunStatus.INTERRUPTED:
                # It was killed. Any other outcome stands: keeping the workspace is not part of it.
                if state.park_reason is ParkReason.EXITED:
                    # A quit parks only so a person can resume it; with nothing to resume, it is
                    # a clean end.
                    status, reason = RunStatus.SUCCEEDED, None
                    state.park_reason = None
                else:
                    status, reason = RunStatus.FAILED, parked
            _finalise(
                self._run_dir,
                state,
                status,
                failure_reason=reason,
                exit_code=exit_code,
                turns=outcome.turns,
                summary=outcome.summary,
                cost_usd=add(state.cost_usd, outcome.cost_usd),
                input_tokens=add(state.input_tokens, outcome.input_tokens),
                cached_input_tokens=add(state.cached_input_tokens, outcome.cached_input_tokens),
                cache_write_input_tokens=add(
                    state.cache_write_input_tokens, outcome.cache_write_input_tokens
                ),
                output_tokens=add(state.output_tokens, outcome.output_tokens),
                artifacts=artifacts,
                stream_warnings=merge_warnings(
                    state.stream_warnings, self._warnings(outcome, status, exit_code)
                ),
            )
        adapter._states.pop(state.run_id, None)

    def _outcome(self, exit_code: int | None, stderr: str) -> Outcome:
        summary = self._state.signals.summary
        harness = self._adapter.harness
        return harness.session_outcome(self._reader, exit_code, stderr, summary=summary)

    def _verdict(self, outcome: Outcome, exit_code: int | None) -> tuple[RunStatus, str | None]:
        if self._state.stopping is not None:
            return _requested(self._state)
        if self._override is not None:
            return self._override
        return self._session_verdict(outcome.error, exit_code)

    def _warnings(
        self, outcome: Outcome, status: RunStatus, exit_code: int | None
    ) -> tuple[str, ...]:
        excused = self._adapter.harness.excused(self._transcript)
        stream = tuple(warning for warning in outcome.stream_warnings if warning not in excused)
        return self._session_warnings(status, exit_code, stream=stream, error=outcome.error)


class _NoSession(AgentRuntimeError):
    """Adoption found no session to continue, after waking the sandbox: it is parked again, and
    the spawn raises `SessionUnavailable`."""


class _NoBranch(_NoSession):
    """The kept checkout is on no branch, so the spawn raises `NoBranchHeld`."""


async def _rework(sandbox: Sandbox, branch_glob: str, run_id: str, *, mode: ResumeMode) -> str:
    """Readies the kept checkout for the continued session: a rework moves it to the pull
    request's head, a retry leaves it as it is. Returns the turn's header saying so."""
    envs = {"BRANCH_GLOB": branch_glob, "RUN": run_id}
    if mode == "retry":
        envs["MODE"] = mode
    output = await sandbox.run(REWORK, envs=envs, timeout_seconds=REWORK_TIMEOUT)
    detail = output.stderr.strip()[-400:]
    if output.exit_code == REWORK_NO_BRANCH:
        raise _NoBranch(f"the kept checkout is on no branch: {detail}")
    if output.exit_code in (REWORK_NOT_THE_BRANCH, REWORK_BRANCH_GONE):
        raise _NoSession(f"the kept checkout cannot take the rework: {detail}")
    if not output.ok:
        raise AgentRuntimeError(
            f"resetting the kept checkout failed (exit {output.exit_code}): {detail}"
        )
    moved = (_RETRIED if mode == "retry" else _REWORKED).fullmatch(output.stdout)
    if moved is None:
        # The checkout was moved, but what it now holds cannot be told to the agent.
        raise _NoSession(f"rework.sh printed {output.stdout[:200]!r}, not its report")
    if mode == "retry":
        return retry_header(*moved.groups())
    branch, head, stash, lost = moved.groups()
    return rework_header(branch, head, stash, lost)


def _replay(run_dir: Path, state: _RunState, transcript: SessionTranscript) -> list[str]:
    block = mirror.read_slice(
        run_dir / SESSION_MIRROR,
        state.segment_log_start - state.log_offset,
        state.log_read - state.log_offset,
    )
    return [out for line in mirror.lines(block) for out in transcript.feed(line)]

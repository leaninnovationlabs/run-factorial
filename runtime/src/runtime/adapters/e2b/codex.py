"""Codex inside an E2B sandbox: the same CLI surface and stream reader as `codex_cli`.

An API key never enters the sandbox: `auth.json` holds a placeholder and the egress proxy sets
the real `Authorization` header, as it does for each MCP server's bearer token. A subscription
login is a refreshable token file with nothing to inject, so it is written into the sandbox and
deleted before every pause (decision 25).

The microVM is the boundary here, so codex runs `danger-full-access` (decision 34): its own
sandbox needs kernel features the guest need not offer, and it exists to protect a host this
substrate does not share.

A run (decision 56) runs the native binary the template links (`CODEX_NATIVE`) as the TUI, with
the login file and a `config.toml` holding the checkout seeded untrusted, the TUI's keys, the
hooks that run `SIGNAL`, and the system prompt as `developer_instructions`. That file is the
agent's to write, so every launch renders it again.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Final

from runtime.adapters._cli.environment import attachment_lines, refuse_unverifiable_expectations
from runtime.adapters._cli.session_signals import json_object
from runtime.adapters.codex_cli.auth import AUTH_MODE_SUBSCRIPTION, SECRET_AUTH_JSON
from runtime.adapters.codex_cli.cli import (
    AUTH_FILE,
    CONFIG_APPROVAL_POLICY,
    CONFIG_FILE,
    CONFIG_PROJECTS_TABLE,
    CONFIG_SANDBOX_MODE,
    CONFIG_TRUST_LEVEL,
    ENV_API_KEY,
    ENV_CODEX_HOME,
    ENV_TERM,
    LOG_DIRNAME,
    META_ID,
    PINNED_VERSION,
    RECORD_SESSION_META,
    ROLLOUT_PAYLOAD,
    ROLLOUT_TYPE,
    SIGN_IN_SCREENS,
    TUI_LOG_FILE,
    VALUE_APPROVAL_NEVER,
    VALUE_SANDBOX_FULL,
    VALUE_TERM,
    VALUE_TRUST_UNTRUSTED,
)
from runtime.adapters.codex_cli.spec import (
    _reject_model_alias,
    _toml_string,
    mcp_tables,
)
from runtime.adapters.codex_cli.stream import NO_TURN, NO_USAGE, StreamReader
from runtime.adapters.codex_cli.transcript import (
    TRANSCRIPT_VERSION,
    RolloutTranscript,
    codex_log_signals,
)
from runtime.adapters.codex_cli.tui import (
    pointer,
    resume_argv,
    signal_command,
    spawn_argv,
    thread_of,
    tui_config,
    tui_flags,
    turn_text,
)
from runtime.adapters.e2b.harness import (
    Launch,
    Outcome,
    Reader,
    attachment_files,
    attachment_paths,
    mcp_headers_by_host,
    reject_stdio,
    require_secret,
    require_value,
    skill_files,
)
from runtime.adapters.e2b.layout import (
    AGENT_USER,
    CODEX_FINISH,
    CODEX_NATIVE,
    CONFIG_DIR,
    CONSENT,
    ROLLOUT_DIR,
    SIGNAL,
    SIGNALS,
    VIEW_SIGNALS,
    WORKSPACE,
    turn_file,
)
from runtime.contract import McpServerSpec, MissingAttachment, RunAttachment, RunSpec

if TYPE_CHECKING:
    from runtime.adapters._cli.session_reader import LogSignals, SessionTranscript
    from runtime.adapters._cli.session_signals import CostState

API_HOST: Final = "api.openai.com"
#: A ChatGPT login talks to its backend and refreshes itself against the auth server.
SUBSCRIPTION_HOSTS: Final = ("chatgpt.com", "auth.openai.com")
PLACEHOLDER: Final = "sk-proj-injected-at-egress"

AUTH_PATH: Final = f"{CONFIG_DIR}/{AUTH_FILE}"
CONFIG_PATH: Final = f"{CONFIG_DIR}/{CONFIG_FILE}"
#: Every hook's command. The paths are constants, so its trusted hash is one too.
SIGNAL_COMMAND: Final = signal_command(SIGNALS, SIGNAL)


def _config(
    mcp: tuple[McpServerSpec, ...], workspace: str = WORKSPACE
) -> tuple[str, dict[str, str]]:
    """`config.toml` and the env its bearer variables need, rendered with placeholders: every
    header value is set by the proxy instead. `workspace` is the checkout's path as codex will
    key it."""
    lines = [
        f"{CONFIG_SANDBOX_MODE} = {_toml_string(VALUE_SANDBOX_FULL)}",
        f"{CONFIG_APPROVAL_POLICY} = {_toml_string(VALUE_APPROVAL_NEVER)}",
        "",
        # The checkout's `.codex/` would otherwise be trusted and loaded (`codex_cli/cli.py`).
        f"[{CONFIG_PROJECTS_TABLE}.{_toml_string(workspace)}]",
        f"{CONFIG_TRUST_LEVEL} = {_toml_string(VALUE_TRUST_UNTRUSTED)}",
        "",
    ]
    tables, env = mcp_tables(mcp, _Placeholders())
    lines += tables
    return "\n".join(lines), env


def _session_config(
    mcp: tuple[McpServerSpec, ...],
    workspace_real: str,
    config_real: str,
    system: str | None,
    *,
    hook: str = SIGNAL_COMMAND,
    ask_tool: bool = False,
) -> str:
    """A launch's `config.toml`: `_config`'s, with the checkout seeded untrusted under its
    resolved path, and the TUI's keys and hooks, each running `hook`."""
    base, _ = _config(mcp, workspace_real)
    return tui_config(
        base,
        codex_home_real=config_real,
        signal_command=hook,
        developer_instructions=system,
        ask_tool=ask_tool,
    )


class _Placeholders(dict[str, str]):
    """Answers every `{secrets.KEY}` with the placeholder, so nothing real is rendered."""

    def get(self, key: str, default: object = None) -> str:  # type: ignore[override]
        return PLACEHOLDER

    def __getitem__(self, key: str) -> str:
        return PLACEHOLDER

    def __contains__(self, key: object) -> bool:
        return True


class CodexHarness:
    name: str = "codex"
    label: str = "Codex CLI"
    pinned_version: str = PINNED_VERSION
    egress_hosts: tuple[str, ...] = (API_HOST, *SUBSCRIPTION_HOSTS)
    credential_paths: tuple[str, ...] = (AUTH_PATH,)
    transcript_version: int = TRANSCRIPT_VERSION
    turnless_warnings: frozenset[str] = frozenset({NO_TURN, NO_USAGE})
    chooses_session_id: bool = False
    finish_script: str = CODEX_FINISH
    turnless_hook: bool = False
    consent_script: str | None = CONSENT
    sign_in: tuple[str, ...] = SIGN_IN_SCREENS
    launch_log: str | None = f"{CONFIG_DIR}/{LOG_DIRNAME}/{TUI_LOG_FILE}"

    def __init__(self, *, ask_tool: bool = False) -> None:
        self._ask_tool = ask_tool

    @property
    def log_signals(self) -> LogSignals:
        return functools.partial(codex_log_signals, ask_tool=self._ask_tool)

    def validate(self, spec: RunSpec, auth_mode: str) -> None:
        _reject_model_alias(spec.model)
        refuse_unverifiable_expectations("CodexE2bAdapter", spec)
        reject_stdio(spec)
        require_secret(
            spec, SECRET_AUTH_JSON if auth_mode == AUTH_MODE_SUBSCRIPTION else ENV_API_KEY
        )
        _config(spec.mcp)  # an unmappable server header refuses here, before a sandbox exists

    def _auth(
        self, secrets: Mapping[str, str], auth_mode: str
    ) -> tuple[bytes, dict[str, dict[str, str]]]:
        if auth_mode == AUTH_MODE_SUBSCRIPTION:
            return secrets[SECRET_AUTH_JSON].encode(), {}
        document = json.dumps({"auth_mode": "apikey", ENV_API_KEY: PLACEHOLDER}).encode()
        return document, {API_HOST: {"Authorization": f"Bearer {secrets[ENV_API_KEY]}"}}

    def session_flags(self, spec: RunSpec) -> tuple[str, ...]:
        return tui_flags(spec.model, worktree=WORKSPACE, sandbox_mode=VALUE_SANDBOX_FULL)

    def session_spawn(
        self, spec: RunSpec, secrets: Mapping[str, str], auth_mode: str, session_id: str
    ) -> Launch:
        """`session_id` is unused: codex names its thread itself, once it runs."""
        turn = turn_file(0)
        launch, images = self._spec_launch(spec, secrets, auth_mode, turn)
        argv = spawn_argv(self.session_flags(spec), images, pointer(turn), binary=CODEX_NATIVE)
        return replace(launch, argv=argv)

    def session_adopt(
        self,
        spec: RunSpec,
        secrets: Mapping[str, str],
        auth_mode: str,
        session_id: str,
        segment: int,
    ) -> Launch:
        turn = turn_file(segment)
        launch, images = self._spec_launch(spec, secrets, auth_mode, turn)
        argv = resume_argv(
            self.session_flags(spec), session_id, images, pointer(turn), binary=CODEX_NATIVE
        )
        return replace(launch, argv=argv)

    def _spec_launch(
        self, spec: RunSpec, secrets: Mapping[str, str], auth_mode: str, turn: str
    ) -> tuple[Launch, tuple[str, ...]]:
        """A launch carrying `spec` whole, its prompt in `turn`; no argv. And the images."""
        paths = attachment_paths(spec.attachments)
        files = {
            turn: (turn_text(spec, paths).encode(), AGENT_USER, 0o600),
            **skill_files(spec),
            **attachment_files(spec.attachments),
        }
        return self._tui_launch(secrets, auth_mode, spec.mcp, files), tuple(paths.values())

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
        paths = attachment_paths(attachments)
        files = attachment_files(attachments)
        prompt = None
        if turn is not None:
            path = turn_file(segment)
            text = "\n".join([turn, *attachment_lines(attachments, missing, paths)])
            files[path] = (text.encode(), AGENT_USER, 0o600)
            prompt = pointer(path)
        launch = self._tui_launch(secrets, auth_mode, mcp, files)
        argv = resume_argv(flags, session_id, tuple(paths.values()), prompt, binary=CODEX_NATIVE)
        return replace(launch, argv=argv)

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
        require_value(
            secrets, SECRET_AUTH_JSON if auth_mode == AUTH_MODE_SUBSCRIPTION else ENV_API_KEY
        )
        launch = self._tui_launch(secrets, auth_mode, (), {})
        hook = signal_command(VIEW_SIGNALS, SIGNAL)
        config = _session_config((), workspace_real, config_real, None, hook=hook)
        return replace(
            launch,
            argv=resume_argv(flags, session_id, (), None, binary=CODEX_NATIVE),
            files={**launch.files, CONFIG_PATH: (config.encode(), AGENT_USER, 0o600)},
        )

    def _tui_launch(
        self,
        secrets: Mapping[str, str],
        auth_mode: str,
        mcp: tuple[McpServerSpec, ...],
        files: dict[str, tuple[bytes, str, int]],
    ) -> Launch:
        """`files` with the login, the env and the egress headers; no argv. The config waits for
        the checkout's real path (`session_seeds`)."""
        _, env = _config(mcp)
        auth, inject = self._auth(secrets, auth_mode)
        files[AUTH_PATH] = (auth, AGENT_USER, 0o600)
        return Launch(
            argv=(),
            env={ENV_CODEX_HOME: CONFIG_DIR, ENV_TERM: VALUE_TERM, **env},
            files=files,
            inject={**mcp_headers_by_host(mcp, secrets), **inject},
        )

    def session_seeds(
        self,
        workspace_real: str,
        config_real: str,
        auth_mode: str,
        mcp: tuple[McpServerSpec, ...],
        system: str | None,
    ) -> dict[str, tuple[bytes, str, int]]:
        config = _session_config(mcp, workspace_real, config_real, system, ask_tool=self._ask_tool)
        return {CONFIG_PATH: (config.encode(), AGENT_USER, 0o600)}

    def session_log(self, session_id: str) -> str | None:
        return None

    def session_found(self, log: str, first_line: str) -> str | None:
        thread = thread_of(log)
        if thread is None or not log.startswith(f"{ROLLOUT_DIR}/"):
            return None
        record = json_object(first_line) or {}
        payload = record.get(ROLLOUT_PAYLOAD)
        if record.get(ROLLOUT_TYPE) != RECORD_SESSION_META or not isinstance(payload, Mapping):
            return None
        return thread if payload.get(META_ID) == thread else None

    def transcript(
        self, session_id: str, *, turn: bool, cost_base: CostState | None, skip: int
    ) -> RolloutTranscript:
        # Codex reports no cost, so a segment has no running total to start from.
        return RolloutTranscript(session_id, turn=turn, skip=skip)

    def excused(self, transcript: SessionTranscript) -> frozenset[str]:
        assert isinstance(transcript, RolloutTranscript)
        return frozenset() if transcript.usage_due else frozenset({NO_USAGE})

    def reader(self, prior_turns: int) -> Reader:
        return StreamReader(prior_turns=prior_turns)

    def session_outcome(
        self, reader: Reader, exit_code: int | None, stderr: str, *, summary: str | None
    ) -> Outcome:
        # The translated stream has no closing line to judge: a segment with no turn, or none
        # that ended normally, carries no final message, which is not a failure in a session.
        assert isinstance(reader, StreamReader)
        return Outcome(
            error=None,
            summary=reader.summary or summary or "",
            turns=reader.turns,
            input_tokens=reader.input_tokens,
            cached_input_tokens=reader.cached_input_tokens,
            cache_write_input_tokens=reader.cache_write_input_tokens,
            output_tokens=reader.output_tokens,
            stream_warnings=reader.missing(),
        )

"""Claude Code inside an E2B sandbox: the same CLI surface and stream reader as `claude_cli`.

No credential is written into the sandbox. The model key or subscription token, and every MCP
server's headers, are set by the egress proxy on the way out, and the CLI sees placeholders
(decision 25). So there is nothing to scrub before a pause.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Final

from runtime.adapters._claude.flags import ENV_NONESSENTIAL as ENV_NONESSENTIAL
from runtime.adapters._cli.environment import attachment_lines, refuse_unverifiable_expectations
from runtime.adapters._cli.session_reader import LogSignals, SessionTranscript
from runtime.adapters._cli.session_signals import CostState
from runtime.adapters.claude_cli.auth import AUTH_MODE_SUBSCRIPTION, SECRET_OAUTH_TOKEN
from runtime.adapters.claude_cli.cli import (
    ENV_API_KEY,
    MCP_KEY_TYPE,
    MCP_KEY_URL,
    MCP_ROOT_KEY,
    PINNED_VERSION,
)
from runtime.adapters.claude_cli.spec import (
    _reject_model_alias,
    build_prompt,
)
from runtime.adapters.claude_cli.stream import NO_TURN, StreamReader
from runtime.adapters.claude_cli.transcript import (
    TRANSCRIPT_VERSION,
    Transcript,
    claude_log_signals,
)
from runtime.adapters.claude_cli.tui import (
    pointer,
    resume_argv,
    seed_files,
    spawn_argv,
    tui_env,
    tui_flags,
    view_flags,
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
    CONFIG_DIR,
    FINISH,
    SIGNAL,
    SIGNALS,
    VIEW_SIGNALS,
    session_log,
    turn_file,
)
from runtime.contract import McpServerSpec, MissingAttachment, RunAttachment, RunSpec

API_HOST: Final = "api.anthropic.com"
#: What the CLI is handed instead of a credential. Shaped like the real thing because the CLI
#: reads the prefix; the proxy overwrites the header it becomes.
PLACEHOLDER_API_KEY: Final = "sk-ant-api03-injected-at-egress"
PLACEHOLDER_OAUTH_TOKEN: Final = "sk-ant-oat01-injected-at-egress"

SYSTEM_PROMPT_PATH: Final = f"{CONFIG_DIR}/system-prompt.md"
MCP_CONFIG_PATH: Final = f"{CONFIG_DIR}/mcp.json"


class ClaudeHarness:
    name: str = "claude"
    label: str = "Claude CLI"
    pinned_version: str = PINNED_VERSION
    egress_hosts: tuple[str, ...] = (API_HOST,)
    credential_paths: tuple[str, ...] = ()
    transcript_version: int = TRANSCRIPT_VERSION
    turnless_warnings: frozenset[str] = frozenset({NO_TURN})
    chooses_session_id: bool = True
    finish_script: str = FINISH
    turnless_hook: bool = True
    consent_script: str | None = None
    sign_in: tuple[str, ...] = ()
    launch_log: str | None = None

    @property
    def log_signals(self) -> LogSignals:
        return claude_log_signals

    def validate(self, spec: RunSpec, auth_mode: str) -> None:
        _reject_model_alias(spec.model)
        refuse_unverifiable_expectations("ClaudeE2bAdapter", spec)
        reject_stdio(spec)
        require_secret(
            spec, SECRET_OAUTH_TOKEN if auth_mode == AUTH_MODE_SUBSCRIPTION else ENV_API_KEY
        )

    def _placeholder(self, auth_mode: str) -> str:
        if auth_mode == AUTH_MODE_SUBSCRIPTION:
            return PLACEHOLDER_OAUTH_TOKEN
        return PLACEHOLDER_API_KEY

    def _inject(self, secrets: Mapping[str, str], auth_mode: str) -> dict[str, dict[str, str]]:
        if auth_mode == AUTH_MODE_SUBSCRIPTION:
            return {API_HOST: {"Authorization": f"Bearer {secrets[SECRET_OAUTH_TOKEN]}"}}
        return {API_HOST: {"x-api-key": secrets[ENV_API_KEY]}}

    def _mcp_document(self, spec: RunSpec) -> bytes:
        servers = {s.name: {MCP_KEY_TYPE: s.transport.value, MCP_KEY_URL: s.url} for s in spec.mcp}
        return json.dumps({MCP_ROOT_KEY: servers}, indent=2).encode()

    def session_flags(self, spec: RunSpec) -> tuple[str, ...]:
        return tui_flags(spec, system_prompt_path=SYSTEM_PROMPT_PATH, mcp_path=MCP_CONFIG_PATH)

    def session_spawn(
        self, spec: RunSpec, secrets: Mapping[str, str], auth_mode: str, session_id: str
    ) -> Launch:
        launch, prompt = self._spec_launch(spec, secrets, auth_mode, turn_file(0))
        return replace(launch, argv=spawn_argv(self.session_flags(spec), session_id, prompt))

    def session_adopt(
        self,
        spec: RunSpec,
        secrets: Mapping[str, str],
        auth_mode: str,
        session_id: str,
        segment: int,
    ) -> Launch:
        launch, prompt = self._spec_launch(spec, secrets, auth_mode, turn_file(segment))
        return replace(launch, argv=resume_argv(self.session_flags(spec), session_id, prompt))

    def _spec_launch(
        self, spec: RunSpec, secrets: Mapping[str, str], auth_mode: str, turn: str
    ) -> tuple[Launch, str]:
        """A launch carrying `spec` whole, its prompt in `turn`, and the pointer to it; no argv."""
        paths = attachment_paths(spec.attachments)
        files = {
            SYSTEM_PROMPT_PATH: (spec.prompts.system.encode(), AGENT_USER, 0o600),
            turn: (build_prompt(spec, paths).encode(), AGENT_USER, 0o600),
            **skill_files(spec),
            **attachment_files(spec.attachments),
        }
        inject = self._inject(secrets, auth_mode)
        if spec.mcp:
            files[MCP_CONFIG_PATH] = (self._mcp_document(spec), AGENT_USER, 0o600)
            inject.update(mcp_headers_by_host(spec.mcp, secrets))
        launch = Launch(argv=(), env=self._tui_env(auth_mode), files=files, inject=inject)
        return launch, pointer(turn, tuple(paths.values()))

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
            prompt = pointer(path, tuple(paths.values()))
        return Launch(
            argv=resume_argv(flags, session_id, prompt),
            env=self._tui_env(auth_mode),
            files=files,
            inject={**mcp_headers_by_host(mcp, secrets), **self._inject(secrets, auth_mode)},
        )

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
            secrets, SECRET_OAUTH_TOKEN if auth_mode == AUTH_MODE_SUBSCRIPTION else ENV_API_KEY
        )
        return Launch(
            argv=resume_argv(view_flags(flags), session_id, None),
            env=self._tui_env(auth_mode, signals=VIEW_SIGNALS),
            files=self.session_seeds(workspace_real, config_real, auth_mode, (), None),
            inject=self._inject(secrets, auth_mode),
        )

    def _tui_env(self, auth_mode: str, *, signals: str = SIGNALS) -> dict[str, str]:
        env = tui_env(
            config_dir=CONFIG_DIR,
            signals_path=signals,
            auth_mode=auth_mode,
            credential=self._placeholder(auth_mode),
        )
        return {**env, ENV_NONESSENTIAL: "1"}

    def session_seeds(
        self,
        workspace_real: str,
        config_real: str,
        auth_mode: str,
        mcp: tuple[McpServerSpec, ...],
        system: str | None,
    ) -> dict[str, tuple[bytes, str, int]]:
        # The same placeholder `tui_env` hands the CLI: the approved key is its tail.
        seeds = seed_files(
            config_dir=CONFIG_DIR,
            workspace_real=workspace_real,
            signal_script=SIGNAL,
            auth_mode=auth_mode,
            credential=self._placeholder(auth_mode),
        )
        return {path: (content, AGENT_USER, 0o600) for path, content in seeds.items()}

    def session_log(self, session_id: str) -> str:
        return session_log(session_id)

    def session_found(self, log: str, first_line: str) -> str | None:
        return None  # claude takes the session id it is given

    def transcript(
        self, session_id: str, *, turn: bool, cost_base: CostState | None, skip: int
    ) -> Transcript:
        return Transcript(session_id, turn=turn, cost_base=cost_base, skip=skip)

    def excused(self, transcript: SessionTranscript) -> frozenset[str]:
        return frozenset()

    def reader(self, prior_turns: int) -> Reader:
        return StreamReader(prior_turns=prior_turns)

    def session_outcome(
        self, reader: Reader, exit_code: int | None, stderr: str, *, summary: str | None
    ) -> Outcome:
        # The translator's `result` line already carries the hook's words and the turn's error.
        assert isinstance(reader, StreamReader)
        if reader.result is not None:
            error = reader.error()
        else:
            # No terminal event: the CLI died before reporting, and stderr is the only diagnosis.
            ended = "no exit code" if exit_code is None else f"exit {exit_code}"
            error = f"The {self.label} stopped before it reported a result ({ended})."
            if tail := stderr.strip()[:400]:
                error += f"\n\n{tail}"
        return Outcome(
            error=error,
            summary=reader.summary,
            turns=reader.final_turns,
            cost_usd=reader.cost_usd,
            input_tokens=reader.input_tokens,
            cached_input_tokens=reader.cached_input_tokens,
            cache_write_input_tokens=reader.cache_write_input_tokens,
            output_tokens=reader.output_tokens,
            stream_warnings=reader.missing(),
        )

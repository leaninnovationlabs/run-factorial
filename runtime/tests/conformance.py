"""The conformance battery every `runtime.contract.RuntimeAdapter` implementation must pass.

Four adapters run it today (`codex_cli`, `claude_cli`, and both on E2B, `claude_e2b` and
`codex_e2b`); `claude_managed` or an HTTP adapter would join the same way (tech-stack.md §5).
This module is how "did the adapter honor what the spec declared" stays mechanically testable
across all of them, instead of re-derived by eye in each adapter's own test file.

**Extending this for a new adapter is exactly one addition.** Write one `_Case`-producing
function for it — its own fake substrate, built the same way that adapter's own unit tests
already build one (see `_claude_cli_case` below for the shape: a fake CLI seam, a runnable
spec, and the handful of adapter-specific hooks `AdapterCase` needs) — and add one entry to
`_CASE_FACTORIES`. Every test function below is written against the `AdapterCase` protocol only,
never against a concrete adapter class, so nothing else in this file changes.

The host cases run the fake TUIs in a real tmux under a short root, over a fake git seam. The E2B
cases fake the sandbox (`fake_e2b`).

Declared artifact expectations (`RunSpec.expected_artifacts`) are batteried the same
way: an adapter must either HONOUR an expectation — report the artifact it found, and fail a run
whose declared artifact is absent, naming which — or REFUSE the spawn. Silently ignoring one is
the failure the field exists to prevent, and it is the only outcome this battery does not allow.

Each capability-gated assertion (MCP rejection, attachments) is
written to `skip` rather than pass vacuously when the adapter under test does not have the
capability that would make the assertion meaningful — so a battery run always shows exactly what
was and was not exercised, and adding a case whose adapter *does* have the capability activates
the check with no change to the test itself. Every adapter refuses a blind workspace, and
`test_cli_blind_clone.py` runs the clone and its gates directly.

**Every refusal states WHY** (`SpawnReason`). A refusal that only says "no" leaves the
orchestrator with one option — retry — which is right for a busy substrate and catastrophic for a
wrong manifest: it spins at attempt 1 forever with nothing in the event log. So the battery does
not merely assert that a bad spec raises `SpawnError`; it asserts the reason it raises with, which
is what the caller actually branches on. `AdapterCase.refusals` is where an adapter lists the
scenarios only it can arrange (its own credential, its own workspace mechanics); the
capability-gated tests above assert their reason inline, since every adapter that has the
refusal at all must classify it the same way.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from fnmatch import fnmatch
from pathlib import Path
from typing import Protocol, cast

import pytest
import pytest_asyncio
from fake_e2b import FakeSandboxClient, Script, TuiScript, codex_turn
from host_session_support import short_root
from pydantic import SecretStr

from runtime.adapters._cli.environment import ENV_GH_TOKEN
from runtime.adapters._cli.process import CommandResult
from runtime.adapters.claude_cli.adapter import ClaudeCliAdapter
from runtime.adapters.claude_cli.cli import CLAUDE_BIN, ENV_API_KEY, PINNED_VERSION, _Cli
from runtime.adapters.codex_cli import cli as codex_cli_module
from runtime.adapters.codex_cli.adapter import (
    CodexCliAdapter,
)
from runtime.adapters.e2b import ClaudeE2bAdapter, CodexE2bAdapter, E2bAdapter
from runtime.adapters.e2b.client import CommandOutput
from runtime.adapters.e2b.layout import SESSION, SESSION_POLL
from runtime.contract import (
    AgentPrompts,
    AgentRuntimeError,
    ArtifactKind,
    ExpectedArtifact,
    IdleReason,
    InspectHost,
    InspectPhase,
    InspectUnavailable,
    McpServerSpec,
    McpTransport,
    ParkReason,
    PreviewHost,
    RunAttachment,
    RunResult,
    RunSpec,
    RunStatus,
    RuntimeAdapter,
    SpawnError,
    SpawnReason,
    TerminalTarget,
    UnknownRunError,
    Workspace,
)

pytestmark = pytest.mark.asyncio

#: A run id no case ever produces — deliberately never spawned by any case fixture.
UNKNOWN_RUN_ID = "conformance-battery-no-such-run"

#: The head glob every case's fake substrate DOES deliver under, and one no case ever delivers.
#: The unmet one is deliberately distinctive: the battery asserts the failure reason names it,
#: which is the difference between "the run failed" and "the run failed for THIS reason".
MET_GLOB = "conformance/*"
UNMET_GLOB = "conformance-never-delivered/*"

#: Every fake's `gh pr list`: this run's PR, and an earlier attempt's closed and merged ones
#: under the same glob. Only the closed one is history rather than delivery.
_PULLS = tuple(
    {"number": n, "url": f"https://example.test/pr/{n}", "headRefName": head, "state": state}
    for n, head, state in (
        (1, "conformance/x", "OPEN"),
        (2, "conformance/x-2", "CLOSED"),
        (3, "conformance/x-3", "MERGED"),
    )
)


def _gh_pr_list(args: Sequence[str]) -> str:
    """`_PULLS` as gh returns them: only the fields `--json` asked for."""
    fields = args[args.index("--json") + 1].split(",") if "--json" in args else []
    return json.dumps([{k: v for k, v in pr.items() if k in fields} for pr in _PULLS])

#: `spawn_running()`'s return shape: a fresh adapter, the run id of a run that will never reach
#: a terminal state on its own, and an async cleanup callable the caller always awaits.
_SpawnRunning = Callable[[], Awaitable[tuple[RuntimeAdapter, str, Callable[[], Awaitable[None]]]]]


@dataclass(frozen=True)
class Refusal:
    """One spawn an adapter must refuse, and the `SpawnReason` it must name doing it.

    A callable rather than a bare `RunSpec` because the interesting refusals are not all
    spec-shaped: claude_cli's workspace refusal needs a fake `git` that cannot resolve the
    base ref. Each case arranges its own; the battery only checks the verdict.
    """

    #: Names the scenario in the assertion message — this is what a failure report reads like.
    id: str
    #: Must raise `SpawnError`. Anything else (including returning) fails the battery.
    spawn: Callable[[], Awaitable[object]]
    reason: SpawnReason


@dataclass
class InteractiveArm:
    """The session half of the battery, expressed against the contract only."""

    #: Spawn an interactive run on a FRESH adapter whose session ends its first turn and then
    #: waits at its prompt, never ending on its own, and whose `resume` without a turn brings it
    #: back to that prompt; return `(adapter, run_id, cleanup)`. `cleanup` must leave nothing
    #: pending, and is always awaited.
    spawn_waiting: _SpawnRunning
    #: Let the substrate carry the session as far as it will go without a human.
    settle: Callable[[], Awaitable[None]]


@dataclass
class InspectArm:
    """What an `InspectHost` contributes: a view of a kept run's session (decision 57)."""

    #: A FRESH adapter whose one run has ended and kept its workspace, its session's where the
    #: substrate opens one; returns `(adapter, run_id, cleanup)`. `cleanup` must leave no view or
    #: server behind, and is always awaited.
    kept_run: _SpawnRunning
    #: What the worker hands `inspect_start`.
    credentials: Mapping[str, SecretStr]


@dataclass
class AdapterCase:
    """Everything one adapter contributes to the battery, expressed against the contract only.

    A field is `None` exactly when the corresponding assertion has nothing to enforce for this
    adapter (its capability ClassVar already grants what the assertion would otherwise refuse) —
    see the module docstring.
    """

    id: str
    #: Wired to a fake substrate able to run `spec` to a terminal state end to end.
    adapter: RuntimeAdapter
    #: A spec `adapter` can actually run to SUCCEEDED. Carries the values in `secret_values`, so
    #: the redaction test has something real to look for.
    spec: RunSpec
    secret_values: tuple[str, ...]
    #: Whatever this substrate needs, after `adapter.spawn(spec)` returns that run id, to carry
    #: the run to a terminal state before `status`/`result`/`logs` are read: a session finished
    #: at its prompt, a background supervisor drained.
    settle: Callable[[str], Awaitable[None]]
    #: `spec`, but with `workspace.isolation="blind"`, which every adapter must refuse.
    blind_spec: RunSpec
    #: `spec`, but with a non-empty `mcp` tuple — populated only when `adapter.supports_mcp` is
    #: False; the case that must refuse it.
    mcp_spec: RunSpec | None
    #: `spec`, but with `public=True` — populated only when the adapter is not a
    #: `PreviewHost`; the case that must refuse it.
    public_spec: RunSpec | None
    #: Build a *fresh* adapter of the same kind, wired so a spawned run announces its session and
    #: never reaches a terminal state, and return `(adapter, run_id, cleanup)`. `cleanup` must
    #: leave nothing pending (cancel the run and let any background task finish) — always
    #: awaited by the caller.
    spawn_running: _SpawnRunning
    #: How many agents this case's adapters, `spawn_running`'s included, have launched so far —
    #: how the battery proves a refusal fired before any agent existed.
    launches: Callable[[], int]
    #: Run one spec carrying a declared expectation to a terminal state on a FRESH adapter of
    #: this kind, and return its result. `met=True` builds a substrate that delivers what was
    #: declared, `met=False` one that does not (declaring `UNMET_GLOB`, which the reason must
    #: name). `None` only for an adapter that refuses expectations outright.
    run_expectation: Callable[[bool], Awaitable[RunResult]] | None
    #: `spec`, but declaring an expectation kind this adapter cannot verify — the case that must
    #: refuse it at spawn. `None` when the adapter can verify every kind.
    unsupported_expectation_spec: RunSpec | None
    #: Refusals only this adapter can arrange, each with the reason it must name.
    #: Every shipped adapter contributes at least one per PERMANENT reason it can produce; an
    #: empty tuple would be a case that proves nothing about classification.
    refusals: tuple[Refusal, ...]
    interactive_arm: InteractiveArm
    #: Populated exactly when `adapter` is an `InspectHost`.
    inspect_arm: InspectArm | None


def _blind(spec: RunSpec) -> RunSpec:
    workspace = spec.workspace.model_copy(
        update={"isolation": "blind", "hidden": ("acceptance/1/**",)}
    )
    return spec.model_copy(update={"workspace": workspace})


def _short_root() -> Path:
    """A `local_root` a tmux socket fits under: pytest's tmp_path on macOS is too long."""
    return Path(os.path.realpath(tempfile.mkdtemp(prefix="f243c-", dir="/tmp")))


def _kill_servers(root: Path) -> None:
    for socket in (root / "t").glob("*"):
        subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True)


class _Draining(Protocol):
    async def drain(self) -> None: ...


def _kill_every_server(root: Path) -> None:
    """The servers of every adapter a case built under `root`, each at `root/<name>`."""
    for local_root in root.iterdir():
        _kill_servers(local_root)


def _forget_root(root: Path) -> None:
    _kill_servers(root)
    shutil.rmtree(root, ignore_errors=True)


def _fake_tui(
    path: Path,
    fake_name: str = "fake_claude_tui.py",
    *,
    modes: str = "",
    launches: Path | None = None,
) -> str:
    """A fake TUI behind an absolute path, as `shutil.which` resolves the real CLI, scripted by
    `modes` (the fake's `FAKE_TUI`). Each start appends a line to `launches`."""
    fake = Path(__file__).with_name(fake_name)
    counted = f"echo launch >> '{launches}'\n" if launches is not None else ""
    run = f'exec "{sys.executable}" "{fake}" "$@"'
    path.write_text(f"#!/bin/sh\n{counted}export FAKE_TUI='{modes}'\n{run}\n")
    path.chmod(0o755)
    return str(path)


def _launched(launches: Path) -> int:
    return len(launches.read_text().splitlines()) if launches.exists() else 0


async def _settle_session(adapter: ClaudeCliAdapter | CodexCliAdapter, run_id: str) -> None:
    """A host session's first turn answered, then finished at its prompt."""
    for _ in range(400):
        if await adapter.status(run_id) is RunStatus.IDLE:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError(f"run {run_id} never idled")
    await adapter.finish(run_id)
    await adapter.drain()


def _host_inspect_arm(
    build: Callable[[Path, str], ClaudeCliAdapter | CodexCliAdapter],
    spec: RunSpec,
    fake_name: str,
) -> InspectArm:
    """A session the fake TUI ran in a real tmux, finished: only a session has a view."""

    async def kept_run() -> tuple[RuntimeAdapter, str, Callable[[], Awaitable[None]]]:
        if shutil.which("tmux") is None:
            pytest.skip("needs tmux")
        root = await asyncio.to_thread(_short_root)
        binary = await asyncio.to_thread(_fake_tui, root / "fake-cli", fake_name)
        adapter = build(root, binary)
        handle = await adapter.spawn(spec)
        for _ in range(400):
            if await adapter.status(handle.run_id) is RunStatus.IDLE:
                break
            await asyncio.sleep(0.05)
        await adapter.finish(handle.run_id)

        async def cleanup() -> None:
            await asyncio.to_thread(_kill_servers, root)
            await adapter.drain()
            await asyncio.to_thread(_forget_root, root)

        return adapter, handle.run_id, cleanup

    return InspectArm(kept_run=kept_run, credentials=spec.secrets)


# --------------------------------------------------------------------------- claude_cli case

_GH_TOKEN_MARKER = "ghs_conformanceBatteryMustNeverLeak"
_API_KEY_MARKER = "sk-ant-conformanceBatteryMustNeverLeak"


def _write_git_pointer(worktree: str, clone: str) -> None:
    """As git does: a worktree's `.git` is a FILE naming its administrative directory in the
    shared clone — what the codex adapter removes after `worktree add`, so the fake must lay
    it down for that path to run."""
    admin = Path(clone, ".git", "worktrees", "worktree")
    admin.mkdir(parents=True, exist_ok=True)
    (admin / "commondir").write_text("../..\n")
    Path(worktree, ".git").write_text(f"gitdir: {admin}\n")


class _FakeClaudeCli(_Cli):
    """claude_cli's CLI seam with the fake TUI as its binary: no real `claude`, `git` or `gh`.

    `delivers` is what the substrate pretends the run pushed: empty by default, so the cases that
    declare no expectation behave exactly as they did before expectations existed.
    """

    def __init__(self, *, delivers: str = "", claude_bin: str = CLAUDE_BIN) -> None:
        super().__init__(claude_bin=claude_bin)
        self._delivers = delivers

    async def run(
        self,
        program: str,
        *args: str,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        inherit_host_env: bool = True,
    ) -> CommandResult:
        argv = (program, *args)
        if "--version" in args:
            return CommandResult(
                argv=argv, returncode=0, stdout=f"{PINNED_VERSION} (Claude Code)\n", stderr=""
            )
        if "worktree" in args and "add" in args:
            _mkdir(args[-2])
            _write_git_pointer(args[-2], args[args.index("-C") + 1])
        if self._delivers and "ls-remote" in args and fnmatch(self._delivers, args[-1]):
            stdout = f"deadbeef\trefs/heads/{self._delivers}\n"
            return CommandResult(argv=argv, returncode=0, stdout=stdout, stderr="")
        if self._delivers and program == "gh" and "pr" in args and "list" in args:
            return CommandResult(argv=argv, returncode=0, stdout=_gh_pr_list(args), stderr="")
        return CommandResult(argv=argv, returncode=0, stdout="", stderr="")


class _NoSuchBaseCli(_FakeClaudeCli):
    """A substrate whose clone holds no such ref: every `rev-parse --verify` fails.

    The bootstrap doc's own claude_cli wedge (`docs/bootstrap.md`, "Do not put `{slug}` in
    `workspace.base`"), reproduced at the seam that decides it.
    """

    async def run(
        self,
        program: str,
        *args: str,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        inherit_host_env: bool = True,
    ) -> CommandResult:
        if "rev-parse" in args:
            return CommandResult(
                argv=(program, *args),
                returncode=128,
                stdout="",
                stderr="fatal: Needed a single revision",
            )
        return await super().run(program, *args, env=env, cwd=cwd)


def _mkdir(path: str) -> None:
    """Blocking filesystem work, kept out of the async bodies (ruff ASYNC240)."""
    Path(path).mkdir(parents=True, exist_ok=True)


def _claude_cli_spec(**overrides: object) -> RunSpec:
    defaults: dict[str, object] = {
        "agent": "conformance-agent",
        "ticket": "CONFORMANCE-1",
        "workspace": Workspace(repo="acme/conformance", base="main", branch="conformance/{slug}"),
        "prompts": AgentPrompts(system="You are a conformance fixture."),
        "secrets": {
            ENV_GH_TOKEN: SecretStr(_GH_TOKEN_MARKER),
            ENV_API_KEY: SecretStr(_API_KEY_MARKER),
        },
        "model": "claude-haiku-4-5",
    }
    return RunSpec.model_validate(defaults | overrides)


def _claude_cli_case(tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> AdapterCase:
    launches = root / "launches"
    claude = _fake_tui(root / "fake-claude", launches=launches)

    def host(
        name: str,
        cli: type[_FakeClaudeCli] = _FakeClaudeCli,
        *,
        binary: str = claude,
        delivers: str = "",
    ) -> ClaudeCliAdapter:
        seam = cli(delivers=delivers, claude_bin=binary)
        return ClaudeCliAdapter(local_root=root / name, cli=seam, poll=0.05)

    adapter = host("happy")
    spec = _claude_cli_spec()

    mcp_spec = (
        None
        if adapter.supports_mcp
        else spec.model_copy(
            update={
                "mcp": (
                    McpServerSpec(
                        name="product-docs",
                        transport=McpTransport.HTTP,
                        url="https://mcp.example.test/product-docs/mcp",
                    ),
                )
            }
        )
    )

    async def settle(run_id: str) -> None:
        await _settle_session(adapter, run_id)

    async def spawn_running() -> tuple[RuntimeAdapter, str, Callable[[], Awaitable[None]]]:
        slow = _fake_tui(root / "fake-claude-slow", modes="slow", launches=launches)
        hang_adapter = host("running", binary=slow)
        handle = await hang_adapter.spawn(spec)

        async def cleanup() -> None:
            await hang_adapter.cancel(handle.run_id)
            await hang_adapter.drain()

        return hang_adapter, handle.run_id, cleanup

    waiting_runs: list[tuple[ClaudeCliAdapter, str]] = []

    async def spawn_waiting() -> tuple[RuntimeAdapter, str, Callable[[], Awaitable[None]]]:
        """The TUI is `fake_claude_tui.py` in a real tmux; git is the fake's."""
        if shutil.which("tmux") is None:
            pytest.skip("needs tmux")
        root = await asyncio.to_thread(_short_root)
        claude = await asyncio.to_thread(_fake_tui, root / "fake-claude")
        waiting = ClaudeCliAdapter(
            local_root=root,
            cli=_FakeClaudeCli(claude_bin=claude),
            poll=0.05,
        )
        handle = await waiting.spawn(spec)
        waiting_runs.append((waiting, handle.run_id))

        async def cleanup() -> None:
            await asyncio.to_thread(_kill_servers, root)
            await waiting.drain()
            await asyncio.to_thread(_forget_root, root)

        return waiting, handle.run_id, cleanup

    async def settle_session() -> None:
        waiting, run_id = waiting_runs[-1]
        for _ in range(400):
            if await waiting.status(run_id) is not RunStatus.RUNNING:
                return
            await asyncio.sleep(0.05)

    async def run_expectation(met: bool) -> RunResult:
        expectation_adapter = host(f"expectation-{met}", delivers="conformance/x")
        handle = await expectation_adapter.spawn(
            _claude_cli_spec(
                expected_artifacts=(
                    ExpectedArtifact(
                        kind=ArtifactKind.PR,
                        selector={"head_glob": MET_GLOB if met else UNMET_GLOB},
                    ),
                )
            )
        )
        await _settle_session(expectation_adapter, handle.run_id)
        return await expectation_adapter.result(handle.run_id)

    # The api key is read from the process environment as a fallback, so a developer who exports
    # one would otherwise make the CREDENTIAL refusal vanish silently. Clear it for the case.
    monkeypatch.delenv(ENV_API_KEY, raising=False)

    async def spawn_without_api_key() -> object:
        return await adapter.spawn(
            _claude_cli_spec(secrets={ENV_GH_TOKEN: SecretStr(_GH_TOKEN_MARKER)})
        )

    async def spawn_with_a_missing_base() -> object:
        return await host("no-such-base", _NoSuchBaseCli).spawn(_claude_cli_spec())

    async def spawn_escaping_the_root() -> object:
        return await adapter.spawn(
            _claude_cli_spec(
                workspace=spec.workspace.model_copy(update={"repo": "../../escaped/repo"})
            )
        )

    return AdapterCase(
        id="claude_cli",
        adapter=adapter,
        spec=spec,
        secret_values=(_GH_TOKEN_MARKER, _API_KEY_MARKER),
        settle=settle,
        blind_spec=_blind(spec),
        mcp_spec=mcp_spec,
        public_spec=spec.model_copy(update={"public": True}),
        spawn_running=spawn_running,
        launches=lambda: _launched(launches),
        run_expectation=run_expectation,
        # A worktree this adapter can read is not evidence anything was delivered, so COMMIT is
        # refused rather than answered with a guess.
        unsupported_expectation_spec=spec.model_copy(
            update={"expected_artifacts": (ExpectedArtifact(kind=ArtifactKind.COMMIT),)}
        ),
        refusals=(
            Refusal(
                id="model-size-alias",
                spawn=lambda: adapter.spawn(_claude_cli_spec(model="large")),
                reason=SpawnReason.CONFIG,
            ),
            Refusal(
                id="no-anthropic-api-key",
                spawn=spawn_without_api_key,
                reason=SpawnReason.CREDENTIAL,
            ),
            # A former silent-retry cause, at the seam that produces it.
            Refusal(
                id="workspace-base-resolves-to-nothing",
                spawn=spawn_with_a_missing_base,
                reason=SpawnReason.WORKSPACE,
            ),
            Refusal(
                id="repo-escapes-local-root",
                spawn=spawn_escaping_the_root,
                reason=SpawnReason.WORKSPACE,
            ),
        ),
        interactive_arm=InteractiveArm(spawn_waiting=spawn_waiting, settle=settle_session),
        inspect_arm=_host_inspect_arm(
            lambda root, claude: ClaudeCliAdapter(
                local_root=root, cli=_FakeClaudeCli(claude_bin=claude), poll=0.05
            ),
            spec,
            "fake_claude_tui.py",
        ),
    )


# --------------------------------------------------------------------------- codex_cli case

_CODEX_GH_TOKEN = "ghs_codexConformanceMustNeverLeak"
_CODEX_API_KEY = "sk-openai-codexConformanceMustNeverLeak"


class _FakeCodexCli(codex_cli_module._Cli):
    """codex_cli's CLI seam with the fake TUI as its binary: no real `codex`, `git` or `gh`.
    `delivers` is as `_FakeClaudeCli`'s."""

    def __init__(
        self,
        *,
        delivers: str = "",
        codex_bin: str = codex_cli_module.CODEX_BIN,
    ) -> None:
        super().__init__(codex_bin=codex_bin)
        self._delivers = delivers

    async def run(
        self,
        program: str,
        *args: str,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        inherit_host_env: bool = True,
    ) -> CommandResult:
        argv = (program, *args)
        if "--version" in args:
            return CommandResult(
                argv=argv,
                returncode=0,
                stdout=f"codex-cli {codex_cli_module.PINNED_VERSION}\n",
                stderr="",
            )
        if "worktree" in args and "add" in args:
            _mkdir(args[-2])
            _write_git_pointer(args[-2], args[args.index("-C") + 1])
        if self._delivers and "ls-remote" in args and fnmatch(self._delivers, args[-1]):
            stdout = f"deadbeef\trefs/heads/{self._delivers}\n"
            return CommandResult(argv=argv, returncode=0, stdout=stdout, stderr="")
        if self._delivers and program == "gh" and "pr" in args and "list" in args:
            return CommandResult(
                argv=argv, returncode=0, stdout=_gh_pr_list(args), stderr=""
            )
        return CommandResult(argv=argv, returncode=0, stdout="", stderr="")


class _NoSuchBaseCodexCli(_FakeCodexCli):
    """A substrate whose clone holds no such ref: every `rev-parse --verify` fails."""

    async def run(
        self,
        program: str,
        *args: str,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        inherit_host_env: bool = True,
    ) -> CommandResult:
        if "rev-parse" in args:
            return CommandResult(
                argv=(program, *args),
                returncode=128,
                stdout="",
                stderr="fatal: Needed a single revision",
            )
        return await super().run(program, *args, env=env, cwd=cwd)


def _codex_cli_spec(**overrides: object) -> RunSpec:
    defaults: dict[str, object] = {
        "agent": "conformance-agent",
        "ticket": "CONFORMANCE-1",
        "workspace": Workspace(repo="acme/conformance", base="main", branch="conformance/{slug}"),
        "prompts": AgentPrompts(system="You are a conformance fixture."),
        "secrets": {
            ENV_GH_TOKEN: SecretStr(_CODEX_GH_TOKEN),
            codex_cli_module.ENV_API_KEY: SecretStr(_CODEX_API_KEY),
        },
        "model": "gpt-5.1-codex",
    }
    return RunSpec.model_validate(defaults | overrides)


def _codex_cli_case(tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> AdapterCase:
    launches = root / "launches"
    codex = _fake_tui(root / "fake-codex", "fake_codex_tui.py", launches=launches)

    def host(
        name: str,
        cli: type[_FakeCodexCli] = _FakeCodexCli,
        *,
        binary: str = codex,
        delivers: str = "",
    ) -> CodexCliAdapter:
        seam = cli(delivers=delivers, codex_bin=binary)
        return CodexCliAdapter(local_root=root / name, cli=seam, poll=0.05)

    adapter = host("happy")
    spec = _codex_cli_spec()

    async def settle(run_id: str) -> None:
        await _settle_session(adapter, run_id)

    async def spawn_running() -> tuple[RuntimeAdapter, str, Callable[[], Awaitable[None]]]:
        slow = _fake_tui(
            root / "fake-codex-slow", "fake_codex_tui.py", modes="slow", launches=launches
        )
        hang_adapter = host("running", binary=slow)
        handle = await hang_adapter.spawn(spec)

        async def cleanup() -> None:
            await hang_adapter.cancel(handle.run_id)
            await hang_adapter.drain()

        return hang_adapter, handle.run_id, cleanup

    waiting_runs: list[tuple[CodexCliAdapter, str]] = []

    async def spawn_waiting() -> tuple[RuntimeAdapter, str, Callable[[], Awaitable[None]]]:
        """The TUI is `fake_codex_tui.py` in a real tmux; git is the fake's."""
        if shutil.which("tmux") is None:
            pytest.skip("needs tmux")
        root = await asyncio.to_thread(_short_root)
        codex = await asyncio.to_thread(_fake_tui, root / "fake-codex", "fake_codex_tui.py")
        waiting = CodexCliAdapter(
            local_root=root,
            cli=_FakeCodexCli(codex_bin=codex),
            poll=0.05,
        )
        handle = await waiting.spawn(spec)
        waiting_runs.append((waiting, handle.run_id))

        async def cleanup() -> None:
            await asyncio.to_thread(_kill_servers, root)
            await waiting.drain()
            await asyncio.to_thread(_forget_root, root)

        return waiting, handle.run_id, cleanup

    async def settle_session() -> None:
        waiting, run_id = waiting_runs[-1]
        for _ in range(400):
            if await waiting.status(run_id) is not RunStatus.RUNNING:
                return
            await asyncio.sleep(0.05)

    async def run_expectation(met: bool) -> RunResult:
        expectation_adapter = host(f"expectation-{met}", delivers="conformance/x")
        handle = await expectation_adapter.spawn(
            _codex_cli_spec(
                expected_artifacts=(
                    ExpectedArtifact(
                        kind=ArtifactKind.PR,
                        selector={"head_glob": MET_GLOB if met else UNMET_GLOB},
                    ),
                )
            )
        )
        await _settle_session(expectation_adapter, handle.run_id)
        return await expectation_adapter.result(handle.run_id)

    monkeypatch.delenv(codex_cli_module.ENV_API_KEY, raising=False)

    async def spawn_without_api_key() -> object:
        return await adapter.spawn(
            _codex_cli_spec(secrets={ENV_GH_TOKEN: SecretStr(_CODEX_GH_TOKEN)})
        )

    async def spawn_with_a_missing_base() -> object:
        return await host("no-such-base", _NoSuchBaseCodexCli).spawn(_codex_cli_spec())

    async def spawn_escaping_the_root() -> object:
        return await adapter.spawn(
            _codex_cli_spec(
                workspace=spec.workspace.model_copy(update={"repo": "../../escaped/repo"})
            )
        )

    return AdapterCase(
        id="codex_cli",
        adapter=adapter,
        spec=spec,
        secret_values=(_CODEX_GH_TOKEN, _CODEX_API_KEY),
        settle=settle,
        blind_spec=_blind(spec),
        mcp_spec=None,
        public_spec=spec.model_copy(update={"public": True}),
        spawn_running=spawn_running,
        launches=lambda: _launched(launches),
        run_expectation=run_expectation,
        # A worktree this adapter can read is not evidence anything was delivered, so COMMIT is
        # refused rather than answered with a guess.
        unsupported_expectation_spec=spec.model_copy(
            update={"expected_artifacts": (ExpectedArtifact(kind=ArtifactKind.COMMIT),)}
        ),
        refusals=(
            Refusal(
                id="model-size-alias",
                spawn=lambda: adapter.spawn(_codex_cli_spec(model="large")),
                reason=SpawnReason.CONFIG,
            ),
            Refusal(
                id="no-openai-api-key",
                spawn=spawn_without_api_key,
                reason=SpawnReason.CREDENTIAL,
            ),
            Refusal(
                id="workspace-base-resolves-to-nothing",
                spawn=spawn_with_a_missing_base,
                reason=SpawnReason.WORKSPACE,
            ),
            Refusal(
                id="repo-escapes-local-root",
                spawn=spawn_escaping_the_root,
                reason=SpawnReason.WORKSPACE,
            ),
        ),
        interactive_arm=InteractiveArm(spawn_waiting=spawn_waiting, settle=settle_session),
        inspect_arm=_host_inspect_arm(
            lambda root, codex: CodexCliAdapter(
                local_root=root, cli=_FakeCodexCli(codex_bin=codex), poll=0.05
            ),
            spec,
            "fake_codex_tui.py",
        ),
    )


# --------------------------------------------------------------------------- claude_e2b case


def _e2b_case(
    tmp_path: Path,
    *,
    case_id: str,
    adapter_cls: type[E2bAdapter],
    make_spec: Callable[..., RunSpec],
    key_name: str,
    secret_values: tuple[str, str],
    sessions: tuple[TuiScript, TuiScript],
    running: TuiScript,
) -> AdapterCase:
    """A harness in a fake sandbox (`fake_e2b`). Probes run host-side, through
    the same fake `git`/`gh` seam the claude_cli case uses. `sessions` is a spawn whose first
    turn ends at its prompt, and its resume with no turn; `running` is a spawn whose first turn
    never ends."""

    clients: list[FakeSandboxClient] = []

    def e2b(
        name: str,
        script: Script,
        *,
        delivers: str = "",
        client: FakeSandboxClient | None = None,
    ) -> E2bAdapter:
        clients.append(client or FakeSandboxClient(scripts=[script]))
        return adapter_cls(
            template="factorial-agent:conformance",
            state_root=tmp_path / name,
            client=clients[-1],
            cli=_FakeClaudeCli(delivers=delivers),
            poll_interval=0,
        )

    spawned, resumed = sessions
    adapter = e2b("happy", Script(tui=spawned))
    spec = make_spec()

    async def finished(e2b_adapter: E2bAdapter, run_id: str) -> None:
        for _ in range(10_000):
            if await e2b_adapter.status(run_id) is RunStatus.IDLE:
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError(f"run {run_id} never idled")
        await e2b_adapter.finish(run_id)
        await e2b_adapter.drain()

    async def settle(run_id: str) -> None:
        await finished(adapter, run_id)

    async def spawn_running() -> tuple[RuntimeAdapter, str, Callable[[], Awaitable[None]]]:
        hang_adapter = e2b("running", Script(tui=running))
        handle = await hang_adapter.spawn(spec)

        async def cleanup() -> None:
            await hang_adapter.cancel(handle.run_id)
            await hang_adapter.drain()

        return hang_adapter, handle.run_id, cleanup

    async def spawn_waiting() -> tuple[RuntimeAdapter, str, Callable[[], Awaitable[None]]]:
        waiting = e2b("interactive", Script(tui=spawned))
        clients[-1].scripts.append(Script(tui=resumed))
        handle = await waiting.spawn(spec)

        async def cleanup() -> None:
            await waiting.cancel(handle.run_id)
            await waiting.drain()

        return waiting, handle.run_id, cleanup

    async def kept_run() -> tuple[RuntimeAdapter, str, Callable[[], Awaitable[None]]]:
        """A session that finished: only a session has a view. The view's CLI resumes as the
        session's turnless resume does."""
        client = FakeSandboxClient(scripts=[Script(tui=spawned)], view=resumed)
        kept = e2b("inspect", Script(), client=client)
        handle = await kept.spawn(spec)
        await finished(kept, handle.run_id)

        async def cleanup() -> None:
            await kept.drain()

        return kept, handle.run_id, cleanup

    async def settle_session() -> None:
        # Two polls: the first releases the turn's end, the second follows its state write.
        polls = len(clients[-1].commands_by_name(SESSION_POLL))
        for _ in range(10_000):
            if len(clients[-1].commands_by_name(SESSION_POLL)) >= polls + 2:
                return
            await asyncio.sleep(0)
        raise AssertionError("the session supervisor never polled twice")

    async def run_expectation(met: bool) -> RunResult:
        expecting = e2b(f"expectation-{met}", Script(tui=spawned), delivers="conformance/x")
        handle = await expecting.spawn(
            make_spec(
                expected_artifacts=(
                    ExpectedArtifact(
                        kind=ArtifactKind.PR,
                        selector={"head_glob": MET_GLOB if met else UNMET_GLOB},
                    ),
                )
            )
        )
        await finished(expecting, handle.run_id)
        return await expecting.result(handle.run_id)

    async def spawn_with_a_missing_base() -> object:
        client = FakeSandboxClient(
            setup_result=CommandOutput(128, "", "fatal: couldn't find remote ref spec/x")
        )
        return await e2b("no-such-base", Script(), client=client).spawn(make_spec())

    stdio = McpServerSpec(name="repo-index", transport=McpTransport.STDIO, command="npx")
    github_only = {ENV_GH_TOKEN: spec.secrets[ENV_GH_TOKEN]}
    return AdapterCase(
        id=case_id,
        adapter=adapter,
        spec=spec,
        secret_values=secret_values,
        settle=settle,
        blind_spec=_blind(spec),
        mcp_spec=None,
        public_spec=None,
        spawn_running=spawn_running,
        launches=lambda: sum(
            command.command == SESSION
            for client in clients
            for sandbox in client.sandboxes.values()
            for command in sandbox.commands
        ),
        run_expectation=run_expectation,
        unsupported_expectation_spec=spec.model_copy(
            update={"expected_artifacts": (ExpectedArtifact(kind=ArtifactKind.COMMIT),)}
        ),
        refusals=(
            Refusal(
                id="model-size-alias",
                spawn=lambda: adapter.spawn(make_spec(model="large")),
                reason=SpawnReason.CONFIG,
            ),
            Refusal(
                id="stdio-mcp-server",
                spawn=lambda: adapter.spawn(make_spec(mcp=(stdio,))),
                reason=SpawnReason.CONFIG,
            ),
            Refusal(
                id=f"no-{key_name.lower().replace('_', '-')}",
                spawn=lambda: adapter.spawn(make_spec(secrets=github_only)),
                reason=SpawnReason.CREDENTIAL,
            ),
            Refusal(
                id="workspace-base-resolves-to-nothing",
                spawn=spawn_with_a_missing_base,
                reason=SpawnReason.WORKSPACE,
            ),
        ),
        interactive_arm=InteractiveArm(spawn_waiting=spawn_waiting, settle=settle_session),
        inspect_arm=InspectArm(kept_run=kept_run, credentials=spec.secrets),
    )


def _hook(event: str, **fields: str) -> str:
    body = {"hook_event_name": event, **fields}
    return json.dumps({"at": "2026-01-01T00:00:00Z", "hook": body})


def _claude_e2b_case(tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> AdapterCase:
    return _e2b_case(
        tmp_path,
        case_id="claude_e2b",
        adapter_cls=ClaudeE2bAdapter,
        make_spec=_claude_cli_spec,
        key_name=ENV_API_KEY,
        secret_values=(_GH_TOKEN_MARKER, _API_KEY_MARKER),
        sessions=(
            TuiScript(
                steps=[
                    (
                        [
                            _hook("SessionStart", source="startup"),
                            _hook("UserPromptSubmit"),
                            _hook("Stop"),
                        ],
                        (),
                    )
                ]
            ),
            TuiScript(steps=[([_hook("SessionStart", source="resume")], ())]),
        ),
        running=TuiScript(),
    )


def _codex_e2b_case(tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> AdapterCase:
    return _e2b_case(
        tmp_path,
        case_id="codex_e2b",
        adapter_cls=CodexE2bAdapter,
        make_spec=_codex_cli_spec,
        key_name=codex_cli_module.ENV_API_KEY,
        secret_values=(_CODEX_GH_TOKEN, _CODEX_API_KEY),
        # Codex asks for the folder at every launch, and a resume with no turn fires no hook.
        sessions=(TuiScript(steps=[codex_turn(meta=True)], consent=True), TuiScript(consent=True)),
        # The spawn waits for the rollout its first turn begins.
        running=TuiScript(steps=[_turn_begun()], consent=True),
    )


def _turn_begun() -> tuple[list[str], list[str]]:
    """Codex's first turn cut after its prompt: the rollout begun, no Stop."""
    hooks, log = codex_turn(meta=True)
    return hooks[:2], log[:3]


_CASE_FACTORIES: dict[str, Callable[[Path, Path, pytest.MonkeyPatch], AdapterCase]] = {
    "codex_cli": _codex_cli_case,
    "claude_cli": _claude_cli_case,
    "claude_e2b": _claude_e2b_case,
    "codex_e2b": _codex_e2b_case,
}


@pytest_asyncio.fixture(params=sorted(_CASE_FACTORIES), loop_scope="function")
async def case(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AdapterCase]:
    roots = short_root("f243c-")
    root = next(roots)
    built = _CASE_FACTORIES[request.param](tmp_path, root, monkeypatch)
    try:
        yield built
    finally:
        await asyncio.to_thread(_kill_every_server, root)
        await cast("_Draining", built.adapter).drain()
        next(roots, None)


# --------------------------------------------------------------------------- mcp containment


async def test_spawn_rejects_mcp_when_unsupported(case: AdapterCase) -> None:
    if case.mcp_spec is None:
        pytest.skip(f"{case.id}: supports_mcp is True, nothing to enforce here")
    with pytest.raises(SpawnError) as excinfo:
        await case.adapter.spawn(case.mcp_spec)
    # CONFIG, on every substrate: a manifest declaring servers against an adapter that cannot
    # wire them is as deterministic as a typo, and a retry re-reads the same two facts.
    assert excinfo.value.reason is SpawnReason.CONFIG
    assert excinfo.value.reason.is_permanent


# --------------------------------------------------------------------------- previews


async def test_spawn_rejects_public_unless_a_preview_host(case: AdapterCase) -> None:
    host = isinstance(case.adapter, PreviewHost)
    assert (case.public_spec is None) is host, (
        f"{case.id}: public_spec and PreviewHost disagree — a case must arrange the refusal "
        "exactly when the adapter cannot serve a preview"
    )
    if case.public_spec is None:
        pytest.skip(f"{case.id}: a PreviewHost serves previews; its own tests cover it")
    with pytest.raises(SpawnError, match="public") as excinfo:
        await case.adapter.spawn(case.public_spec)
    assert excinfo.value.reason is SpawnReason.CONFIG


# --------------------------------------------------------------------------- interactive sessions

async def test_a_session_idles_and_reports_where_it_stands(case: AdapterCase) -> None:
    """At its prompt a session is IDLE, never done: the worker reads why from `session_state`
    and decides from the postconditions whether to finish it or wait for a human."""
    arm = case.interactive_arm
    adapter, run_id, cleanup = await arm.spawn_waiting()
    try:
        fresh = await adapter.session_state(run_id)
        assert fresh.log_offset == 0 and fresh.session_id, "answered as soon as spawn returns"

        await arm.settle()
        assert await adapter.status(run_id) is RunStatus.IDLE
        state = await adapter.session_state(run_id)
        assert state.idle_reason is IdleReason.TURN_END and state.idle_since is not None
        assert state.session_id == fresh.session_id
        target = await adapter.terminal_target(run_id)
        assert TerminalTarget.model_validate(target.model_dump()) == target
    finally:
        await cleanup()


async def test_a_session_parks_and_resumes_without_a_turn(case: AdapterCase) -> None:
    """A park keeps the session; `resume` with no turn brings the same session back to its
    prompt, where a `finish` ends it as the success the worker asked for."""
    arm = case.interactive_arm
    adapter, run_id, cleanup = await arm.spawn_waiting()
    try:
        await arm.settle()
        session_id = (await adapter.session_state(run_id)).session_id

        await adapter.interrupt(run_id, ParkReason.ROLLOVER)
        assert await adapter.status(run_id) is RunStatus.INTERRUPTED
        handle = await adapter.resume(run_id, None, case.spec.secrets)
        assert handle.run_id == run_id and not handle.status.is_terminal

        await arm.settle()
        assert await adapter.status(run_id) is RunStatus.IDLE
        state = await adapter.session_state(run_id)
        assert (state.idle_reason, state.session_id) == (IdleReason.TURN_END, session_id)

        await adapter.finish(run_id)
        assert await adapter.status(run_id) is RunStatus.SUCCEEDED
        await adapter.finish(run_id)  # idempotent
        assert (await adapter.result(run_id)).session_id == session_id
    finally:
        await cleanup()


async def test_a_finish_on_a_parked_session_keeps_its_outcome(case: AdapterCase) -> None:
    """A park accepted without a resume is finished, which ends the park and nothing else."""
    arm = case.interactive_arm
    adapter, run_id, cleanup = await arm.spawn_waiting()
    try:
        await arm.settle()
        await adapter.interrupt(run_id, ParkReason.IDLE)
        parked = await adapter.result(run_id)

        await adapter.finish(run_id)
        await adapter.finish(run_id)  # idempotent

        assert await adapter.status(run_id) is RunStatus.INTERRUPTED
        assert await adapter.result(run_id) == parked
    finally:
        await cleanup()


# --------------------------------------------------------------------------- inspect views


def _inspect_arm_or_skip(case: AdapterCase) -> InspectArm:
    host = isinstance(case.adapter, InspectHost)
    assert (case.inspect_arm is None) is not host, (
        f"{case.id}: inspect_arm and InspectHost disagree — an adapter with the verbs must prove "
        "them here"
    )
    if case.inspect_arm is None:
        pytest.skip(f"{case.id}: not an InspectHost")
    return case.inspect_arm


async def test_an_inspect_host_opens_a_view_of_a_kept_run_once_per_token(
    case: AdapterCase,
) -> None:
    """The worker answers one request with one start, which a second start with its token
    repeats nothing of, and attaches the gateway to the target it is handed."""
    arm = _inspect_arm_or_skip(case)
    adapter, run_id, cleanup = await arm.kept_run()
    try:
        assert isinstance(adapter, InspectHost)
        never = await adapter.inspect_status(run_id)
        assert (never.phase, never.token, never.target) == (InspectPhase.STOPPED, None, None)

        opened = await adapter.inspect_start(
            run_id, token="t1", limit=timedelta(minutes=55), credentials=arm.credentials
        )
        assert opened.phase is InspectPhase.OPEN and opened.token == "t1"
        assert opened.target is not None and opened.started_at is not None
        assert TerminalTarget.model_validate(opened.target.model_dump()) == opened.target
        again = await adapter.inspect_start(
            run_id, token="t1", limit=timedelta(minutes=55), credentials=arm.credentials
        )
        assert (again.token, again.started_at) == ("t1", opened.started_at)
        status = await adapter.inspect_status(run_id)
        assert (status.phase, status.clients) == (InspectPhase.OPEN, 0)

        await adapter.inspect_stop(run_id)
        await adapter.inspect_stop(run_id)  # idempotent
        stopped = await adapter.inspect_status(run_id)
        assert (stopped.phase, stopped.target) == (InspectPhase.STOPPED, None)
    finally:
        await cleanup()


async def test_an_inspect_host_refuses_a_view_while_the_agent_runs(case: AdapterCase) -> None:
    arm = _inspect_arm_or_skip(case)
    adapter, run_id, cleanup = await case.spawn_running()
    try:
        assert isinstance(adapter, InspectHost)
        with pytest.raises(InspectUnavailable, match="still running"):
            await adapter.inspect_start(
                run_id, token="t1", limit=timedelta(minutes=55), credentials=arm.credentials
            )
    finally:
        await cleanup()


# --------------------------------------------------------------------------- blind isolation


async def test_every_adapter_refuses_a_blind_workspace(case: AdapterCase) -> None:
    assert type(case.adapter).supports_blind_workspace is False
    with pytest.raises(SpawnError, match="blind") as excinfo:
        await case.adapter.spawn(case.blind_spec)
    assert excinfo.value.reason is SpawnReason.CONFIG
    assert case.launches() == 0


# --------------------------------------------------------------------------- refusal reasons


async def test_every_refusal_names_the_reason_it_refused_for(case: AdapterCase) -> None:
    """A refusal that only says "no" is read as retryable, because that is the only
    thing a caller can safely do with an unclassified `SpawnError` — which is how a wrong
    manifest, an unset App credential and a nonexistent base ref each spun at attempt 1 forever
    with nothing in the event log.

    Every scenario is attempted and every verdict collected before asserting, so one adapter
    misclassifying one site does not hide the other three.
    """
    assert case.refusals, f"{case.id}: contributes no classified refusal — see AdapterCase"
    wrong: list[str] = []
    for refusal in case.refusals:
        try:
            await refusal.spawn()
        except SpawnError as exc:
            if exc.reason is not refusal.reason:
                wrong.append(
                    f"{refusal.id}: expected {refusal.reason.value}, got {exc.reason.value}"
                )
        except Exception as exc:  # the point of this arm: it must be a SpawnError
            wrong.append(f"{refusal.id}: raised {type(exc).__name__}, not SpawnError ({exc})")
        else:
            wrong.append(f"{refusal.id}: did not refuse at all")
    assert not wrong, f"{case.id}: " + "; ".join(wrong)


async def test_a_permanent_refusal_is_never_reported_as_retryable(case: AdapterCase) -> None:
    """The half the orchestrator actually branches on (`worker.failures._permanent_spawn`). Asserted
    separately from the reason itself so a future vocabulary change that moved a member across
    the permanent/retryable line could not slip through on the strength of the name alone."""
    for refusal in case.refusals:
        assert refusal.reason.is_permanent, (
            f"{case.id}/{refusal.id}: every refusal a case declares is a config, credential or "
            "workspace fault — an INFRASTRUCTURE refusal belongs in the adapter's own tests, "
            "since the battery's contract here is about what must ESCALATE"
        )


# ------------------------------------------------------------ declared artifact expectations


async def test_an_unverifiable_expectation_is_refused_at_spawn(case: AdapterCase) -> None:
    """Honour or refuse — the one thing an adapter may not do with a declared expectation is
    ignore it, because then it reports a run delivered on evidence nobody looked for."""
    if case.unsupported_expectation_spec is None:
        pytest.skip(f"{case.id}: verifies every expectation kind, nothing to refuse")
    with pytest.raises(SpawnError) as excinfo:
        await case.adapter.spawn(case.unsupported_expectation_spec)
    assert excinfo.value.reason is SpawnReason.CONFIG


async def test_a_met_expectation_is_reported_on_the_result(case: AdapterCase) -> None:
    if case.run_expectation is None:
        pytest.skip(f"{case.id}: refuses declared expectations outright")
    result = await case.run_expectation(True)
    assert result.outcome is RunStatus.SUCCEEDED, result.failure_reason
    assert any(artifact.kind is ArtifactKind.PR for artifact in result.artifacts)


async def test_a_closed_unmerged_pull_request_is_not_delivery(case: AdapterCase) -> None:
    """`--state all` keeps a merged spec PR visible, and every abandoned attempt's PR with it."""
    if case.run_expectation is None:
        pytest.skip(f"{case.id}: refuses declared expectations outright")
    result = await case.run_expectation(True)
    assert result.outcome is RunStatus.SUCCEEDED, result.failure_reason
    found = {a.locator["number"] for a in result.artifacts if a.kind is ArtifactKind.PR}
    assert found == {"1", "3"}


async def test_an_unmet_expectation_fails_the_run_and_names_it(case: AdapterCase) -> None:
    """The gap declared expectations close, asserted identically on every substrate: the agent
    said done, the declared evidence is absent, and the reason says WHICH expectation went
    unanswered."""
    if case.run_expectation is None:
        pytest.skip(f"{case.id}: refuses declared expectations outright")
    result = await case.run_expectation(False)
    assert result.outcome is RunStatus.FAILED
    assert result.failure_reason is not None
    assert UNMET_GLOB in result.failure_reason


async def test_a_spec_declaring_nothing_still_reaches_a_terminal_state(case: AdapterCase) -> None:
    """The default that keeps every caller predating expectations valid: none declared, and the
    adapter's own probe decides, exactly as before."""
    assert case.spec.expected_artifacts == ()
    handle = await case.adapter.spawn(case.spec)
    await case.settle(handle.run_id)
    assert await case.adapter.status(handle.run_id) is RunStatus.SUCCEEDED


_ATTACHMENT_CONTENT = b"\x89PNG\r\n\x1a\n" + b"conformance-image"
_ATTACHMENT = RunAttachment(
    name="image-1",
    media_type="image/png",
    content=_ATTACHMENT_CONTENT,
    source="ticket_body",
    origin="https://github.com/user-attachments/assets/00000000-0000-4000-8000-000000000001",
    sha256=hashlib.sha256(_ATTACHMENT_CONTENT).hexdigest(),
)


async def test_attachments_are_honoured_or_refused(case: AdapterCase) -> None:
    """Honour or refuse, never drop: an agent told about an image it never got fails as if it
    were a bad agent."""
    spec = case.spec.model_copy(update={"attachments": (_ATTACHMENT,)})
    if not type(case.adapter).supports_attachments:
        with pytest.raises(SpawnError) as excinfo:
            await case.adapter.spawn(spec)
        assert excinfo.value.reason is SpawnReason.CONFIG
        return
    handle = await case.adapter.spawn(spec)
    await case.settle(handle.run_id)
    assert await case.adapter.status(handle.run_id) is RunStatus.SUCCEEDED


# --------------------------------------------------------------------------- unknown run id


async def test_status_of_an_unknown_run_raises(case: AdapterCase) -> None:
    with pytest.raises(UnknownRunError):
        await case.adapter.status(UNKNOWN_RUN_ID)


async def test_result_of_an_unknown_run_raises(case: AdapterCase) -> None:
    with pytest.raises(UnknownRunError):
        await case.adapter.result(UNKNOWN_RUN_ID)


async def test_logs_of_an_unknown_run_raises(case: AdapterCase) -> None:
    with pytest.raises(UnknownRunError):
        async for _ in case.adapter.logs(UNKNOWN_RUN_ID):
            pass


async def test_cancel_of_an_unknown_run_raises(case: AdapterCase) -> None:
    with pytest.raises(UnknownRunError):
        await case.adapter.cancel(UNKNOWN_RUN_ID)


# --------------------------------------------------------------------------- non-terminal result


async def test_result_on_a_non_terminal_run(case: AdapterCase) -> None:
    """Encodes claude_cli's EXISTING behaviour — read, don't invent: `result()` on a run that
    has not reached a terminal state raises `AgentRuntimeError`. Not a block, not a more specific
    subclass; exactly what `ClaudeCliAdapter.result` already does."""
    adapter, run_id, cleanup = await case.spawn_running()
    try:
        with pytest.raises(AgentRuntimeError):
            await adapter.result(run_id)
    finally:
        await cleanup()


# --------------------------------------------------------------------------- secrets in logs


async def test_secrets_never_appear_in_logs(case: AdapterCase) -> None:
    handle = await case.adapter.spawn(case.spec)
    await case.settle(handle.run_id)
    assert await case.adapter.status(handle.run_id) is RunStatus.SUCCEEDED

    lines = [line async for line in case.adapter.logs(handle.run_id)]
    joined = "\n".join(lines)
    for secret in case.secret_values:
        assert secret not in joined, secret



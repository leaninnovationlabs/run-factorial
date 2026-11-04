"""Helpers the host session tests share: a short root, a local bare origin, git that reaches it
in place of GitHub through either CLI's seam, and waits on a session's state. Imported by name,
which `pythonpath` in the pytest config allows: test modules do not import each other under
`--import-mode=importlib`.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable, Iterator, Mapping
from pathlib import Path

import pytest

from runtime.adapters._cli import environment, tmux
from runtime.adapters._cli.process import Cli, CommandResult
from runtime.adapters._cli.session_state import HostRunState, read_state, write_state
from runtime.adapters._cli.tmux import TmuxServer
from runtime.adapters.claude_cli.adapter import ClaudeCliAdapter
from runtime.adapters.claude_cli.cli import ENV_CONFIG_DIR, _Cli
from runtime.adapters.codex_cli.adapter import CodexCliAdapter
from runtime.adapters.codex_cli.cli import _Cli as CodexCli
from runtime.contract import RunStatus


@pytest.fixture(autouse=True)
def fake_tui_settings_reach_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse in every module that imports it. The fake TUIs are scripted by `FAKE_TUI*`
    variables the tests export, which a host session's allowlist would drop."""
    monkeypatch.setattr(environment, "_SESSION_KEPT_PREFIXES", ("LC_", "FAKE_TUI"))


REPO = "acme/widgets"
URL = f"https://github.com/{REPO}.git"
FAKE_TUI = Path(__file__).with_name("fake_claude_tui.py")
#: What an operator's shell may export that no host session may carry: credentials, the paths to
#: them, and what points git, gh or a shell at the operator's own config.
OPERATOR_ENV = {
    "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
    "GIT_ASKPASS": "/Applications/Code.app/askpass.sh",
    "VSCODE_GIT_IPC_HANDLE": "/tmp/vscode-git.sock",
    "GIT_SSH_COMMAND": "ssh -i /Users/operator/.ssh/id",
    "GIT_CONFIG_GLOBAL": "/Users/operator/.gitconfig",
    "XDG_CONFIG_HOME": "/Users/operator/.config",
    "ZDOTDIR": "/Users/operator/.zsh",
    "AWS_SECRET_ACCESS_KEY": "made-up",
    "NPM_TOKEN": "made-up",
    "KUBECONFIG": "/Users/operator/.kube/config",
    "ANTHROPIC_AUTH_TOKEN": "made-up",
    "CLAUDE_CODE_MESSAGING_TOKEN": "made-up",
    "SOME_WORKER_SETTING": "made-up",
}
#: What a host session keeps of the worker's environment, a sample.
SESSION_ENV = {
    "SHELL": "/bin/zsh",
    "USER": "operator",
    "TZ": "UTC",
    "HTTPS_PROXY": "http://proxy:3128",
    "SSL_CERT_FILE": "/etc/ssl/cert.pem",
    "LC_ALL": "C.UTF-8",
}

#: The toolchain and cache roots a host session gets back from the worker.
TOOL_ROOTS = (
    "RUSTUP_HOME",
    "PYENV_ROOT",
    "UV_PYTHON_INSTALL_DIR",
    "UV_CACHE_DIR",
    "npm_config_cache",
    "GOMODCACHE",
)


def plant_operator_toolchains(home: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """An operator HOME with every root at its default but `.pyenv`, the worker's own
    UV_CACHE_DIR, and the credential holders beside them; returns the roots a session gets."""
    for default in (".rustup", ".local/share/uv/python", ".cache/uv", ".npm", "go/pkg/mod"):
        (home / default).mkdir(parents=True)
    (home / ".cargo").mkdir()
    (home / ".cargo" / "credentials.toml").write_text('[registry]\ntoken = "made-up"\n')
    (home / ".npmrc").write_text("//registry.npmjs.org/:_authToken=made-up\n")
    for key in TOOL_ROOTS:
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        "HOME": str(home),
        "UV_CACHE_DIR": "/srv/uv-cache",
        "CARGO_HOME": str(home / ".cargo"),
        "NPM_CONFIG_USERCONFIG": str(home / ".npmrc"),
        "GOPATH": str(home / "go"),
    }.items():
        monkeypatch.setenv(key, value)
    return {
        "RUSTUP_HOME": str(home / ".rustup"),
        "UV_PYTHON_INSTALL_DIR": str(home / ".local/share/uv/python"),
        "UV_CACHE_DIR": "/srv/uv-cache",
        "npm_config_cache": str(home / ".npm"),
        "GOMODCACHE": str(home / "go/pkg/mod"),
    }


def short_root(prefix: str) -> Iterator[Path]:
    """A root short enough for a socket, which pytest's tmp_path on macOS is not; every tmux
    server under it is ended after."""
    path = Path(os.path.realpath(tempfile.mkdtemp(prefix=prefix, dir="/tmp")))
    yield path
    for socket in (path / tmux.SOCKETS_DIRNAME).glob("*"):
        subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True)
    shutil.rmtree(path, ignore_errors=True)


def via_origin(env: Mapping[str, str] | None, origin: Path) -> dict[str, str]:
    """`env` with GitHub's URL for the repo sent to `origin`, over the file transport host git
    refuses unless its caller asks (runtime/hostgit.py). The clone keeps GitHub's URL as its
    `origin`, as a real one does."""
    merged = dict(env or {})
    count = int(merged.get("GIT_CONFIG_COUNT", "0") or "0")
    entries = ((f"url.file://{origin}.insteadOf", URL), ("protocol.file.allow", "always"))
    for i, (key, value) in enumerate(entries, start=count):
        merged[f"GIT_CONFIG_KEY_{i}"] = key
        merged[f"GIT_CONFIG_VALUE_{i}"] = value
    merged["GIT_CONFIG_COUNT"] = str(count + len(entries))
    return merged


class _ViaOrigin(Cli):
    """Real git, with the repo's GitHub URL pointing at a local bare origin, and no `gh`.
    `garbled` makes `rev-parse` of a fetched head print what is not a sha. `commands` is every
    argv `run` was handed."""

    _origin: Path
    commands: list[tuple[str, ...]]
    garbled = False

    async def run(
        self,
        program: str,
        *args: str,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        inherit_host_env: bool = True,
    ) -> CommandResult:
        self.commands.append((program, *args))
        if program == "gh":
            return CommandResult((program, *args), 0, "[]", "")
        if self.garbled and "FETCH_HEAD^{commit}" in args:
            return CommandResult((program, *args), 0, "hook output\n0123\n", "")
        return await super().run(
            program,
            *args,
            env=via_origin(env, self._origin) if program == self.git_bin else env,
            cwd=cwd,
            inherit_host_env=inherit_host_env,
        )


class OriginCli(_ViaOrigin, _Cli):
    def __init__(self, claude: Path, origin: Path) -> None:
        _Cli.__init__(self, claude_bin=str(claude))
        self._origin = origin
        self.commands = []


class CodexOriginCli(_ViaOrigin, CodexCli):
    def __init__(self, codex: Path, origin: Path) -> None:
        CodexCli.__init__(self, codex_bin=str(codex))
        self._origin = origin
        self.commands = []


def leave_job(config_dir: str) -> subprocess.Popen[bytes]:
    """A job in a session of its own holding `config_dir` as its CLAUDE_CONFIG_DIR, as a command
    the agent backgrounds through claude's Bash tool is."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={"PATH": "/usr/bin:/bin", ENV_CONFIG_DIR: config_dir},
        start_new_session=True,
    )


#: The origin `bare_origin` made last, which `git` sends GitHub's URL to.
_origins: list[Path] = []


def to_origin() -> tuple[str, ...]:
    """Options for git a test or an agent runs in a checkout whose `origin` is GitHub's URL."""
    if not _origins:
        return ()
    return ("-c", f"url.file://{_origins[-1]}.insteadOf={URL}", "-c", "protocol.file.allow=always")


def git(*args: str, cwd: Path) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *to_origin(), *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def bare_origin(folder: Path) -> Path:
    """A bare repo whose `main` holds one commit."""
    bare = folder / "origin.git"
    git("init", "--bare", "-b", "main", str(bare), cwd=folder)
    work = folder / "seed"
    git("clone", str(bare), str(work), cwd=folder)
    (work / "README.md").write_text("widgets\n")
    git("add", "-A", cwd=work)
    git("commit", "-m", "start", cwd=work)
    git("push", "origin", "HEAD:main", cwd=work)
    _origins.append(bare)
    return bare


def run_dir(root: Path, run_id: str) -> Path:
    return root / "runs" / run_id


def state_of(root: Path, run_id: str) -> HostRunState:
    return read_state(run_dir(root, run_id), run_id)


def edit_state(root: Path, run_id: str, **fields: object) -> None:
    write_state(run_dir(root, run_id), state_of(root, run_id).model_copy(update=fields))


def server_of(root: Path, run_id: str) -> TmuxServer:
    binary = shutil.which("tmux")
    assert binary is not None
    socket = tmux.socket_path(root, run_id, "s", AssertionError)
    return TmuxServer(binary, socket, root / tmux.SESSION_CONF_FILE, root, "run")


async def until(check: Callable[[], Awaitable[bool]], what: str, within: float = 20) -> None:
    for _ in range(round(within / 0.05)):
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"never: {what}")


async def reaches(
    adapter: ClaudeCliAdapter | CodexCliAdapter, run_id: str, status: RunStatus
) -> None:
    async def there() -> bool:
        return await adapter.status(run_id) is status

    await until(there, f"run {run_id} {status}")

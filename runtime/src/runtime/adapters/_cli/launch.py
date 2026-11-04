"""How a host session's pane becomes the CLI: `launch.sh` reads the run's environment from a
0600 file, deletes it, and execs the CLI with that environment, so no secret is on argv or in
the tmux server's own environment. The CLI is the pane's process, so `#{pane_pid}` is its pid,
and the session conf keeps the dead pane, so its exit status can be read.
"""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from runtime.adapters._cli.paths import write_new

__all__ = [
    "BIN_DIRNAME",
    "ENV_FILE",
    "ENV_SIGNALS",
    "LAUNCH_FILE",
    "LAUNCH_SH",
    "SIGNAL_SCRIPT",
    "SIGNAL_SH",
    "VIEW_ENV_FILE",
    "launch_argv",
    "launch_prefix",
    "render_env_file",
    "write_env_file",
    "write_launch_files",
]

log = logging.getLogger(__name__)

BIN_DIRNAME: Final = "bin"
LAUNCH_FILE: Final = "launch.sh"
ENV_FILE: Final = "launch.env"
#: In the config home, apart from the session's own: a view and a resume never read each other's.
VIEW_ENV_FILE: Final = "view.env"

#: No trap: a hangup must reach the CLI. `$1`, never a named variable, which a key in the file
#: could overwrite. `/bin/rm`, since the file's PATH is the host's.
LAUNCH_SH: Final = """#!/bin/sh
set -u
. "$1"
/bin/rm -f "$1" || exit
shift
exec "$@"
"""

#: Where `SIGNAL_SH` appends, passed in the agent's environment.
ENV_SIGNALS: Final = "FACTORIAL_SIGNALS"

#: Beside `launch.sh`, the script every hook of every CLI runs. Each line it appends is
#: `{"at": <UTC, seconds>, "hook": <the hook's stdin JSON>}`; the format string works with GNU
#: and BSD `date` alike.
SIGNAL_SCRIPT: Final = "signal.sh"
SIGNAL_SH: Final = f"""#!/bin/sh
# A TUI's hook command for every event. Runs as the agent's user, with the event's JSON on
# stdin, and appends one line to ${ENV_SIGNALS}. Prints nothing, because a hook's stdout can
# reach the model's context; always exits 0, because exit 2 blocks the tool call or prompt.
exec >/dev/null 2>&1
body=$(tr -d '\\n')
printf '{{"at":"%s","hook":%s}}\\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${{body:-null}}" \\
  >>"${ENV_SIGNALS}"
exit 0
"""

_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: Names bash (`/bin/sh` on macOS) refuses to export, rewrites, or evaluates as arithmetic on
#: assignment, which runs a command substitution in an `OPTIND` value.
_SHELL_SPECIAL: Final = frozenset(
    {
        "BASHOPTS",
        "BASHPID",
        "BASH_ARGC",
        "BASH_ARGV",
        "BASH_ARGV0",
        "BASH_COMMAND",
        "BASH_LINENO",
        "BASH_SOURCE",
        "BASH_SUBSHELL",
        "BASH_VERSINFO",
        "DIRSTACK",
        "EPOCHREALTIME",
        "EPOCHSECONDS",
        "EUID",
        "FUNCNAME",
        "GROUPS",
        "HISTCMD",
        "LINENO",
        "OPTIND",
        "PIPESTATUS",
        "PPID",
        "RANDOM",
        "SECONDS",
        "SHELLOPTS",
        "SRANDOM",
        "UID",
    }
)


def _exportable(key: str, value: str) -> bool:
    return _NAME.fullmatch(key) is not None and key not in _SHELL_SPECIAL and "\0" not in value


def render_env_file(env: Mapping[str, str]) -> tuple[str, int]:
    """The env file's text, and how many keys it dropped because sh cannot export them."""
    kept = [
        f"export {key}={shlex.quote(value)}\n"
        for key, value in env.items()
        if _exportable(key, value)
    ]
    return "".join(kept), len(env) - len(kept)


def write_env_file(path: Path, env: Mapping[str, str]) -> int:
    """Write the env file at 0600 and return how many keys it dropped; only the count is
    logged, since a key's name can say what its value is."""
    text, dropped = render_env_file(env)
    write_new(path, text.encode("utf-8", "surrogateescape"), 0o600)
    if dropped:
        log.warning("dropped %d environment variable(s) sh cannot export", dropped)
    return dropped


def write_launch_files(root: Path, scripts: Mapping[str, str] | None = None) -> Path:
    """`<root>/bin/launch.sh` and the caller's own scripts beside it, 0700, rewritten at every
    launch so an agent's edit does not last. Returns the bin dir."""
    folder = root / BIN_DIRNAME
    folder.mkdir(mode=0o700, exist_ok=True)
    folder.chmod(0o700)
    for name, text in {LAUNCH_FILE: LAUNCH_SH, **(scripts or {})}.items():
        write_new(folder / name, text.encode(), 0o700)
    return folder


def launch_argv(root: Path, env_file: Path, argv: Sequence[str]) -> list[str]:
    """The pane's command. `env -i`: the pane otherwise inherits the tmux server's environment
    and tmux's own `TMUX`/`TMUX_PANE`, which the CLI must not see. Every path is absolute:
    the pane starts in the worktree, and `env -i` leaves no PATH to search."""
    if not env_file.is_absolute() or not argv or not argv[0].startswith("/"):
        raise ValueError("a launch needs an absolute env file and an absolute program")
    return [*launch_prefix(root, env_file), *argv]


def launch_prefix(root: Path, env_file: Path) -> list[str]:
    """Every pane command `launch_argv` builds for this env file begins with these words."""
    return ["/usr/bin/env", "-i", "/bin/sh", str(root / BIN_DIRNAME / LAUNCH_FILE), str(env_file)]

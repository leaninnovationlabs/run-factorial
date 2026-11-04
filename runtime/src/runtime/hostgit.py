"""Host-side git against a repository an agent can write (design.md §7.13).

Every `.git` in the run area is agent input: codex's sandbox leaves each clone's `.git` writable,
because a commit writes there (tech-stack.md decision 34), and claude_cli's agent runs
unsandboxed. The host's git runs outside any sandbox, as the worker, and git runs whatever a
repository's config names: a credential helper, an fsmonitor, a hook, a filter driver. Two
layers, because neither covers the other:

- `hardened_env` switches off, at command scope, everything that has one switch. Command scope
  is applied after every config file git reads, so a planted key cannot win against it.
- `repository_violation` reads the config as data and refuses what has no single switch: a
  driver or a config-defined hook (`hook.<name>.*`, newer git) under any name, an include, an
  `http.*` or `url.*` key, a foreign remote URL, a worktree whose `commondir` leads to config
  it never read (which `pinned_common_dir` also overrides at command scope).

Residual: the check and the git call that follows are two moments, and an agent running in
another run can write between them. Only the second layer has that window, and what it covers
grows with each git release that adds a config key naming a program.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Final

__all__ = [
    "NO_TOKEN",
    "ORCHESTRATOR_PREFIX",
    "ORIGIN_FETCH",
    "github_url",
    "hardened_env",
    "host_environment",
    "pinned_common_dir",
    "read_regular",
    "repository_violation",
]

#: The orchestrator's own settings. None is an agent's business, and some are keys to everything —
#: the master key, the API token — so an agent inherits the host's environment without them.
ORCHESTRATOR_PREFIX: Final = "FACTORIAL_"

_OTHER_PROTOCOLS: Final = ("ext", "fd", "file", "ssh", "git", "http", "ftp", "ftps")

#: Applied in this order, before any entry the caller brings: git applies GIT_CONFIG_* entries
#: in order, so an empty `credential.helper` empties the list read from files and the caller's
#: own helper (`git_env`) still lands after it.
_OVERRIDES: Final = (
    ("credential.helper", ""),
    ("core.fsmonitor", "false"),
    ("core.hooksPath", "/dev/null"),
    ("core.pager", "cat"),
    # https only: ssh, ext::, file:// and git:// would each let a remote URL run or read
    # something on the host. Each by name too: a repository's `protocol.<name>.allow` beats
    # `protocol.allow` from any scope. A caller that needs another (a test's file:// origin)
    # appends it.
    ("protocol.allow", "never"),
    *((f"protocol.{name}.allow", "never") for name in _OTHER_PROTOCOLS),
    ("protocol.https.allow", "always"),
)

_ENV: Final = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    # Set and empty, git asks no program at all — not `core.askPass`, not SSH_ASKPASS.
    "GIT_ASKPASS": "",
}

#: The environment for a host git that never leaves the machine (rev-parse, worktree add):
#: a call that a planted program might win has no token to hand it.
NO_TOKEN: Final = {"GH_TOKEN": "", "GITHUB_TOKEN": ""}

ORIGIN_FETCH: Final = "+refs/heads/*:refs/remotes/origin/*"

#: What `git clone` writes, what an agent's ordinary git, gh and hook managers add (`push -u`,
#: an identity, gh's default-repo and merge-base markers, husky's `core.hooksPath`), and keys
#: `_OVERRIDES` pins whatever the file says. None of the rest names a program for host git,
#: whose one commit, a rework's stash, git never signs.
_ALLOWED_KEYS: Final = frozenset(
    {
        "core.hookspath",
        "core.fsmonitor",
        "core.pager",
        "credential.helper",
        "core.autocrlf",
        "core.eol",
        "commit.gpgsign",
        "core.repositoryformatversion",
        "core.filemode",
        "core.bare",
        "core.logallrefupdates",
        "core.ignorecase",
        "core.precomposeunicode",
        "core.symlinks",
        "extensions.worktreeconfig",
        "remote.origin.url",
        "remote.origin.fetch",
        "remote.origin.tagopt",
        "remote.origin.gh-resolved",
        "user.name",
        "user.email",
        "pull.rebase",
        "pull.ff",
        "push.default",
        "push.autosetupremote",
    }
)
_ALLOWED_PATTERNS: Final = (
    re.compile(r"branch\..+\.(remote|merge|rebase|gh-merge-base)"),
    # The empty `credential.helper` override clears URL-scoped helpers as well.
    re.compile(r"credential\..+\.helper"),
    re.compile(r"advice\.[^.]+"),
)
#: The clone's refspec, or a blind clone's single-branch one.
_ORIGIN_FETCH_SHAPE: Final = re.compile(r"\+refs/heads/([^:]+):refs/remotes/origin/\1")


def pinned_common_dir(clone: Path) -> dict[str, str]:
    """For host git that names a linked worktree's administrative directory with `--git-dir`:
    git takes the common directory, and with it the config, from that directory's `commondir`,
    which the agent can rewrite after the check. The variable wins over the file."""
    return {"GIT_COMMON_DIR": str(clone / ".git")}


def host_environment() -> dict[str, str]:
    """The worker's environment as a child may inherit it."""
    return {k: v for k, v in os.environ.items() if not k.startswith(ORCHESTRATOR_PREFIX)}


def github_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


_REMOTE_HOST: Final = re.compile(r"^[A-Za-z0-9.-]+(?::\d+)?$")


def remote_url(repo: str, host: str = "") -> str:
    """Where a run clones `repo`. No host, or `github.com`, is `github_url`."""
    if host and host != "github.com" and _REMOTE_HOST.fullmatch(host):
        return f"https://{host}/{repo}.git"
    return github_url(repo)


def hardened_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """`env` for a host-side git, with the overrides ahead of its own GIT_CONFIG_* entries."""
    merged = dict(env or {})
    count = int(merged.pop("GIT_CONFIG_COUNT", "0") or "0")
    theirs = [
        (merged.pop(f"GIT_CONFIG_KEY_{i}"), merged.pop(f"GIT_CONFIG_VALUE_{i}"))
        for i in range(count)
    ]
    entries = [*_OVERRIDES, *theirs]
    merged |= _ENV
    merged["GIT_CONFIG_COUNT"] = str(len(entries))
    for i, (key, value) in enumerate(entries):
        merged[f"GIT_CONFIG_KEY_{i}"] = key
        merged[f"GIT_CONFIG_VALUE_{i}"] = value
    return merged


def repository_violation(repo: Path, url: str) -> str | None:
    """Why host git must not touch the repository at `repo` (whose git directory is
    `repo/.git`), or None. Nothing at `repo` is None: there is nothing for git to read.

    The answer names a key or a file, never a value: a value is agent-written text.
    """
    if not os.path.lexists(repo):
        return None
    git_dir = repo / ".git"
    if not _is_real_dir(git_dir):
        return ".git is not a directory"
    # git reads the config of whatever `commondir` names, so a main repository has none.
    if os.path.lexists(git_dir / "commondir"):
        return ".git/commondir exists"
    configs = [git_dir / "config", git_dir / "config.worktree"]
    worktrees = git_dir / "worktrees"
    if _is_real_dir(worktrees):
        for entry in worktrees.iterdir():
            name = entry.relative_to(repo)
            if (problem := _worktree_violation(entry, name, git_dir)) is not None:
                return problem
            # Every sibling run's too: host git reads one whenever it addresses that worktree.
            configs.append(entry / "config.worktree")
    for config in configs:
        if (problem := _config_violation(config, config.relative_to(repo), url)) is not None:
            return problem
    attributes = git_dir / "info" / "attributes"
    if os.path.lexists(attributes) and (
        not _is_regular(attributes) or attributes.stat().st_size > 0
    ):
        return ".git/info/attributes is not empty"
    # A second object store, anywhere: what host git checks out need not be what it fetched.
    alternates = git_dir / "objects" / "info" / "alternates"
    if os.path.lexists(alternates) and (
        not _is_regular(alternates) or alternates.stat().st_size > 0
    ):
        return ".git/objects/info/alternates is not empty"
    # Legacy remote definitions, read for a remote the config does not define.
    for legacy in ("remotes", "branches"):
        folder = git_dir / legacy
        if os.path.lexists(folder) and (not _is_real_dir(folder) or any(folder.iterdir())):
            return f".git/{legacy} is not empty"
    return None


def _worktree_violation(entry: Path, name: Path, git_dir: Path) -> str | None:
    """git addressing a linked worktree takes its common directory, and so its config, from
    `commondir`: one that leads anywhere but this `.git` would put config no check read in
    front of host git. None is missing only while `worktree add` is still writing the entry."""
    if not _is_real_dir(entry):
        return f"{name} is not a directory"
    try:
        common = read_regular(entry / "commondir").strip()
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return f"{name}/commondir is not a regular file"
    if (entry / common).resolve() != git_dir.resolve():
        return f"{name}/commondir does not lead to .git"
    return None


def _config_violation(config: Path, name: Path, url: str) -> str | None:
    if not os.path.lexists(config):
        return None
    if not _is_regular(config):
        return f"{name} is not a regular file"
    # git's own parser, never ours: a hand parse that split a key differently would be a bypass.
    # `--no-includes` lists an include as a key instead of following it; cwd `/` keeps the
    # repository being judged out of the parse.
    try:
        result = subprocess.run(
            ["git", "config", "--file", str(config), "--no-includes", "--list", "-z"],
            capture_output=True,
            text=True,
            check=False,
            cwd="/",
            env={**host_environment(), **hardened_env(NO_TOKEN)},
            # A file swapped for a FIFO after the lstat above would block the read for ever.
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return f"{name} could not be read"
    if result.returncode != 0:
        return f"{name} does not parse"
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        key, _, value = entry.partition("\n")
        if key not in _ALLOWED_KEYS and not any(p.fullmatch(key) for p in _ALLOWED_PATTERNS):
            return f"{name}: {key}"
        if key == "remote.origin.url" and value != url:
            return f"{name}: remote.origin.url is not {url}"
        if key == "remote.origin.fetch" and not _ORIGIN_FETCH_SHAPE.fullmatch(value):
            return f"{name}: remote.origin.fetch is not an origin refspec"
    return None


def read_regular(path: Path) -> str:
    """A small file an agent may have written, read without blocking on a FIFO or following a
    final symlink. ValueError or OSError when it is anything but a regular file."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError(f"{path.name} is not a regular file")
        return handle.read(4096).decode("utf-8", "replace")


def _is_real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _is_regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False

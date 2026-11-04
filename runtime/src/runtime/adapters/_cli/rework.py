"""What a continued session's kept checkout gets before its turn (decision 56), and the turn's
header that tells the agent what was done. A rework moves the checkout to its pull request's
head; a retry checks the branch the failed attempt held out again at its local tip, and moves
nothing else. E2B does both with rework.sh; a host does them to the worktree under `local_root`
with `rework_checkout` and `retry_checkout`, that script's twins.
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

from runtime.adapters._cli.process import Cli, CommandResult
from runtime.adapters._cli.tmux import host_path
from runtime.contract import AdapterUnavailable, AgentRuntimeError
from runtime.hostgit import pinned_common_dir, remote_url, repository_violation

__all__ = [
    "REWORK_HEADER",
    "REWORK_LOST",
    "REWORK_STASHED",
    "ReworkRefused",
    "plain_branch",
    "retry_checkout",
    "retry_header",
    "rework_checkout",
    "rework_header",
]

#: What a rework's turn opens with, before the step's prompt.
REWORK_HEADER: Final = (
    "Changes were requested on your pull request. The branch `{branch}` was reset to the pull "
    "request's head, `{head}`."
)
REWORK_STASHED: Final = (
    "Uncommitted changes in the checkout were stashed first, as `{stash}`; `git stash list` "
    "shows them."
)
REWORK_LOST: Final = (
    "Commits the checkout held that are not on the pull request's branch were at `{lost}`."
)
#: What a retry's turn opens with, before the step's prompt.
_RETRY_HEADER: Final = (
    "Your previous attempt at this step was not accepted; the inputs below say why. The "
    "checkout is on `{branch}` at `{head}`, as that attempt left it, uncommitted changes "
    "included."
)

_STASHER: Final = ("-c", "user.name=factorial", "-c", "user.email=factorial@localhost")
_SHA: Final = re.compile(r"[0-9a-f]{40,64}")
_BRANCH: Final = re.compile(r"[A-Za-z0-9._/-]+")
_REFUSED_CHECKOUT: Final = (
    "is already used by worktree",
    "is already checked out at",
    "is not a valid branch name",
)

_Git = Callable[..., Awaitable[CommandResult]]


class ReworkRefused(AgentRuntimeError):
    """The kept checkout cannot take the rework: its branch is not the step's, is gone from the
    remote, or is checked out elsewhere. The session cannot be continued."""


def plain_branch(name: str) -> bool:
    """A branch name git accepts and that is nothing else: `fnmatch`'s `*` matches a `:`, which
    would make the fetch's refspec write a ref of the agent's choosing, and a leading `-` an
    option."""
    return (
        _BRANCH.fullmatch(name) is not None
        and not any(part in name for part in ("..", "//", "/."))
        and not name.startswith(("-", ".", "/"))
        and not name.endswith((".lock", ".", "/"))
    )


def rework_header(branch: str, head: str, stash: str | None, lost: str | None) -> str:
    parts = [REWORK_HEADER.format(branch=branch, head=head)]
    if stash:
        parts.append(REWORK_STASHED.format(stash=stash))
    if lost:
        parts.append(REWORK_LOST.format(lost=lost))
    return " ".join(parts)


def retry_header(branch: str, head: str) -> str:
    return _RETRY_HEADER.format(branch=branch, head=head)


async def rework_checkout(
    cli: Cli,
    git_at: Sequence[str],
    *,
    clone: Path,
    repo: str,
    branch: str,
    glob: str,
    run_id: str,
    env: Mapping[str, str],
    home: Path,
) -> str:
    """Check `branch` out again at its pull request's head, its uncommitted work stashed, not
    discarded, and return the turn's header saying so. `git_at` names the worktree and its
    administrative directory in `clone`, the shared clone of `repo`. `env` authenticates the
    fetch alone and is never written; with `home`, which holds no git config, it is the fetch's
    whole environment. Every git here is host git on a repository the agent can write
    (design.md §7.13): `Cli.git` hardens it, and the clone is checked before the fetch and again
    before the stash and the checkout, which run what the clone's config names.

    The branch was read from the checkout before its park detached HEAD; the agent named it, so
    it is matched against `glob` and passed as one argument, never run. What git prints is never
    read into the header: only the shas `rev-parse` gives, each checked to be one."""
    git, refuse_changed, url = _guarded(
        cli, git_at, clone=clone, repo=repo, branch=branch, glob=glob, env=env, home=home
    )
    await refuse_changed()
    # The URL, never `origin`, which the clone's config names. The destination keeps the
    # tracking branch a fetch by name would have updated.
    fetched = await git(
        "fetch",
        "--quiet",
        "--no-tags",
        url,
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        token=True,
    )
    if not fetched.ok:
        detail = fetched.stderr.strip()[-400:]
        if "couldn't find remote ref" in fetched.stderr:
            raise ReworkRefused(f"the branch {branch!r} is gone from the remote: {detail}")
        raise AdapterUnavailable(f"fetching {branch!r} for the rework failed: {detail}")
    head = await _sha(git, "FETCH_HEAD^{commit}")
    old = await _sha(git, "HEAD")
    lost = None if (await git("merge-base", "--is-ancestor", old, head)).ok else old
    before = await _stash(git)
    await refuse_changed()
    stashed = await git(
        *_STASHER,
        "stash",
        "push",
        "--quiet",
        "--include-untracked",
        "-m",
        f"factorial: before the rework by run {run_id}",
    )
    if not stashed.ok:
        raise AgentRuntimeError(f"stashing the checkout failed: {stashed.stderr.strip()[-400:]}")
    after = await _stash(git)
    await refuse_changed()
    moved = await git("checkout", "--quiet", "-B", branch, head)
    if not moved.ok:
        raise _checkout_failed(moved, branch)
    if await _sha(git, "HEAD") != head:
        raise ReworkRefused(f"the checkout is not at {head} after the rework")
    return rework_header(branch, head, after if after != before else None, lost)


async def retry_checkout(
    cli: Cli,
    git_at: Sequence[str],
    *,
    clone: Path,
    repo: str,
    branch: str,
    glob: str,
    env: Mapping[str, str],
    home: Path,
) -> str:
    """Check `branch` out again at its local tip, where the failed attempt left it, and return
    the turn's header saying so. Nothing is fetched, stashed or reset: the attempt's commits,
    pushed or not, and its uncommitted files are the work the retry continues. `env` is read
    for the clone's remote alone; no git here holds a token. The guards are
    `rework_checkout`'s: the branch is matched against `glob` and passed as one argument, and
    the clone is checked before the checkout runs what its config names."""
    git, refuse_changed, _ = _guarded(
        cli, git_at, clone=clone, repo=repo, branch=branch, glob=glob, env=env, home=home
    )
    await refuse_changed()
    tip = await _sha(git, f"refs/heads/{branch}")
    await refuse_changed()
    moved = await git("checkout", "--quiet", branch, "--")
    if not moved.ok:
        raise _checkout_failed(moved, branch)
    if await _sha(git, "HEAD") != tip:
        raise ReworkRefused(f"the checkout is not at {tip} after the retry's checkout")
    return retry_header(branch, tip)


def _guarded(
    cli: Cli,
    git_at: Sequence[str],
    *,
    clone: Path,
    repo: str,
    branch: str,
    glob: str,
    env: Mapping[str, str],
    home: Path,
) -> tuple[_Git, Callable[[], Awaitable[None]], str]:
    """Both checkouts' guards: `branch` refused unless it is plain and matches `glob`; a host
    git whose environment is `local` alone, with `env`'s token only on a `token=True` call; and
    a check that the clone is still the one `repo`'s URL names. Returns them and the URL."""
    if not plain_branch(branch) or not fnmatch.fnmatchcase(branch, glob):
        raise ReworkRefused(f"the checkout's branch {branch!r} is not a branch matching {glob!r}")
    url = remote_url(repo, env.get("GIT_HTTP_HOST", ""))
    local = {"PATH": host_path(), "HOME": str(home), "LC_ALL": "C", **pinned_common_dir(clone)}

    async def git(*args: str, token: bool = False) -> CommandResult:
        whole = {**local, **env} if token else local
        return await cli.git(*git_at, *args, env=whole, inherit_host_env=False)

    async def refuse_changed() -> None:
        if (violation := await asyncio.to_thread(repository_violation, clone, url)) is not None:
            raise ReworkRefused(f"the shared clone was refused ({violation})")

    return git, refuse_changed, url


def _checkout_failed(moved: CommandResult, branch: str) -> AgentRuntimeError:
    detail = moved.stderr.strip()[-400:]
    if any(refused in moved.stderr for refused in _REFUSED_CHECKOUT):
        return ReworkRefused(f"the branch {branch!r} cannot be checked out: {detail}")
    return AgentRuntimeError(f"checking {branch!r} out failed: {detail}")


async def _sha(git: _Git, rev: str) -> str:
    shown = await git("rev-parse", "--quiet", "--verify", rev)
    sha = shown.stdout.strip()
    if not shown.ok or _SHA.fullmatch(sha) is None:
        raise ReworkRefused(f"`git rev-parse {rev}` printed {shown.stdout[:80]!r}, not a sha")
    return sha


async def _stash(git: _Git) -> str | None:
    shown = await git("rev-parse", "--quiet", "--verify", "refs/stash")
    return shown.stdout.strip() if shown.ok else None

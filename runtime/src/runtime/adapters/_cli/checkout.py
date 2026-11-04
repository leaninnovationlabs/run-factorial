"""Workspace preparation, parking and removal: the shared clone, the per-run worktree, the blind
arm, and the reaper that removes a kept workspace once the orchestrator no longer wants it.

Functions over the `Cli` seam rather than methods of an adapter, so a test can call them
directly and each adapter states its vendor needs as arguments.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import shutil
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, NamedTuple

from runtime.adapters._cli.environment import (
    ENV_GH_TOKEN,
    blind_slug_from_base,
    build_hidden_globs,
    classify_probe_failure,
    config_gone,
    pinned_config_sha,
)
from runtime.adapters._cli.paths import (
    ATTACHMENTS_DIRNAME,
    CLONES_DIRNAME,
    CONFIG_DIRNAME,
    RUNS_DIRNAME,
    WORKTREE_DIRNAME,
    contained,
    delete_credentials,
    remove_attachments,
)
from runtime.contract import AgentRuntimeError, RunStatus, SpawnError, SpawnReason
from runtime.hostgit import (
    NO_TOKEN,
    ORIGIN_FETCH,
    pinned_common_dir,
    read_regular,
    remote_url,
    repository_violation,
)
from runtime.workspace import assert_isolated, assert_no_retained_credential

if TYPE_CHECKING:
    from runtime.adapters._cli.process import Cli, CommandResult
    from runtime.contract import RunSpec

__all__ = [
    "REAP_GRACE",
    "Checkout",
    "Parse",
    "Reapable",
    "detach_head",
    "end_park",
    "held_branch",
    "park_workspace",
    "prepare_blind_clone",
    "prepare_worktree",
    "probe_git_at",
    "reap_workspaces",
    "remove_workspace",
    "sweep_credentials",
    "worktree_git_at",
]

log = logging.getLogger(__name__)


class Checkout(NamedTuple):
    worktree: Path
    #: The worktree's administrative directory inside the shared clone, when its `.git` pointer
    #: file was removed (`drop_git_pointer=True`): git is then told through GIT_DIR instead —
    #: the agent's git by `worktree_env`, the host's by `--git-dir`. None when the pointer stays.
    git_dir: Path | None

def _drop_git_pointer(worktree: Path) -> Path:
    """Remove the worktree's `.git` pointer file and return the directory it named.

    For a sandbox that keeps the cwd's top-level `.git` read-only and follows a pointer FILE to
    protect its `gitdir:` target too. codex-cli 0.153.2 does, and on Linux bwrap re-binds that
    target read-only after the writable roots, so `git commit` died on `index.lock: Read-only
    file system` (observed on the VM, run 157-plan-agent; #168). With no file there is nothing
    to resolve, and the administrative directory stays inside the writable root it always was in.
    """
    pointer = worktree / ".git"
    try:
        text = pointer.read_text("utf-8")
    except OSError as exc:
        raise SpawnError(
            f"`worktree add` left no .git pointer at {pointer}: {exc}",
            reason=SpawnReason.INFRASTRUCTURE,
        ) from exc
    prefix, sep, target = text.strip().partition(":")
    if not sep or prefix.strip() != "gitdir":
        raise SpawnError(
            f"{pointer} is not a gitdir pointer: {text.strip()[:80]!r}",
            reason=SpawnReason.INFRASTRUCTURE,
        )
    git_dir = (worktree / target.strip()).resolve()
    pointer.unlink()
    return git_dir

async def _git_or_raise(
    cli: Cli,
    *args: str,
    env: Mapping[str, str],
) -> CommandResult:
    """Every failure here is INFRASTRUCTURE: the fetch, the clone and the reset never name
    `workspace.base`, and `worktree add` and the blind arm's clone run only after `_start_point`
    or `prepare_blind_clone`'s ls-remote probe has refused an unresolvable one as WORKSPACE."""
    result = await cli.git(*args, env=env)
    if not result.ok:
        # Redacted argv is not needed: no secret is ever a git argument (see `git_env`).
        raise SpawnError(
            f"`git {' '.join(args)}` exited {result.returncode}: {result.stderr.strip()[:400]}",
            reason=SpawnReason.INFRASTRUCTURE,
        )
    return result

async def _start_point(cli: Cli, clone: Path, base: str, env: Mapping[str, str]) -> str:
    """`base` may be a branch or a sha; `origin/<sha>` is not a ref, so try both.

    Refusing here, with both candidates proven missing, is what makes the WORKSPACE
    classification honest rather than a guess about why `worktree add` failed — a disk or lock
    fault there stays INFRASTRUCTURE, because this probe already ruled the ref out.
    """
    candidate = f"origin/{base}"
    probe = await cli.git(
        "-C", str(clone), "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}", env=env
    )
    if probe.ok:
        return candidate
    bare = await cli.git(
        "-C", str(clone), "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", env=env
    )
    if bare.ok:
        return base
    raise SpawnError(
        f"The base branch {base} doesn't exist. Push it, or fix workspace.base in the process "
        f"file.\n\nNeither {candidate} nor {base} is a commit after a fetch. An unresolved "
        "{slug} in workspace.base is the usual cause.",
        reason=SpawnReason.WORKSPACE,
    )


async def _fetch_config_commit(
    cli: Cli, clone: Path, url: str, sha: str, env: Mapping[str, str]
) -> None:
    """Make the pinned config commit readable in the shared clone: objects only, no ref and no
    FETCH_HEAD, so concurrent worktrees of the clone never clash. Usually a no-op — the prune
    fetch has already brought `origin/factorial`, which is never force-pushed."""

    async def present() -> bool:
        probe = await cli.git(
            "-C", str(clone), "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}",
            env=NO_TOKEN,
        )
        return probe.ok

    if await present():
        return
    await _refuse_changed(clone, url)
    fetched = await cli.git(
        "-C", str(clone), "fetch", "--quiet", "--no-write-fetch-head", url, sha, env=env
    )
    if not fetched.ok:
        stderr = fetched.stderr.strip()
        detail = stderr[-400:] or f"rc={fetched.returncode}"
        if any(s in stderr.casefold() for s in ("not our ref", "couldn't find remote ref")):
            raise SpawnError(f"{config_gone(sha)}\n\n{detail}", reason=SpawnReason.WORKSPACE)
        raise SpawnError(
            f"the pinned config commit {sha} could not be fetched into {clone}: {detail}",
            reason=SpawnReason.INFRASTRUCTURE,
        )
    if not await present():
        raise SpawnError(config_gone(sha), reason=SpawnReason.WORKSPACE)


async def prepare_worktree(
    cli: Cli,
    root: Path,
    run_dir: Path,
    spec: RunSpec,
    env: Mapping[str, str],
    *,
    drop_git_pointer: bool,
) -> Checkout:
    """One clone per repo, one worktree per run — cheap enough to be per-attempt, isolated so
    two tickets never fight over a checkout, disposable so a failed run leaves nothing to
    inherit: not its worktree, and not a branch matching the run's own branch template that
    origin no longer has (`_drop_stale_branches`).
    `drop_git_pointer` is the vendor's call: see `_drop_git_pointer`."""
    clone = contained(root, root / CLONES_DIRNAME / spec.workspace.repo, create=False)
    # The URL, never `origin`: where `origin` points is the clone's config, which is agent
    # input, and the token goes wherever it points.
    url = remote_url(spec.workspace.repo, env.get("GIT_HTTP_HOST", ""))
    aside: Path | None = None
    if (violation := await asyncio.to_thread(repository_violation, clone, url)) is not None:
        log.error("shared clone %s refused (%s); replacing it with a fresh clone", clone, violation)
        aside = await asyncio.to_thread(_move_aside, clone)
    try:
        if (clone / ".git").exists():
            # Checked again even right after a replacement: an agent that forced the
            # replacement can put a repository of its own at this path in the meantime.
            await _refuse_changed(clone, url)
            await _git_or_raise(
                cli, "-C", str(clone), "fetch", "--prune", url, ORIGIN_FETCH, env=env
            )
            await _drop_stale_branches(
                cli, clone, url, spec.workspace.branch.replace("{slug}", "*")
            )
        else:
            clone.parent.mkdir(parents=True, exist_ok=True)
            # `--no-checkout`: only refs are needed here, and a checkout at the end of a long
            # transfer would read config an agent wrote during it — and an agent can force
            # this clone at will, by writing a key the check refuses.
            await _git_or_raise(cli, "clone", "--no-checkout", url, str(clone), env=env)
    finally:
        if aside is not None:
            # Off the path between the check and the clone: deleting a whole clone takes long.
            await asyncio.to_thread(shutil.rmtree, aside, ignore_errors=True)

    if (config_sha := pinned_config_sha(spec.workspace)) is not None:
        await _fetch_config_commit(cli, clone, url, config_sha, env)
    start_point = await _start_point(cli, clone, spec.workspace.base, NO_TOKEN)
    worktree = run_dir / WORKTREE_DIRNAME
    # `worktree add` checks files out, which is when a filter driver runs.
    await _refuse_changed(clone, url)
    # Detached: the agent creates and names its own branch (the template carries `{slug}`),
    # and a worktree holding the base branch would stop a second run checking it out.
    await _git_or_raise(
        cli,
        "-C",
        str(clone),
        "worktree",
        "add",
        "--detach",
        str(worktree),
        start_point,
        env=NO_TOKEN,
    )
    if not drop_git_pointer:
        return Checkout(worktree, None)
    return Checkout(worktree, await asyncio.to_thread(_drop_git_pointer, worktree))


#: Where an operation that detaches HEAD keeps the branch it gives back when it ends.
_HOLDING_FILES: Final = ("rebase-merge/head-name", "rebase-apply/head-name", "BISECT_START")


async def _drop_stale_branches(cli: Cli, clone: Path, url: str, glob: str) -> None:
    """Delete every local branch matching the run's own branch template, `glob`, that the
    prune fetch left with no `origin` counterpart and that no worktree of the clone holds.
    Agents create their branches here and nothing else deletes one, so a branch deleted on
    GitHub — by a restart, say — would hand the next agent that picks its name the old
    commits. A branch outside the template is another ticket's, perhaps a live run's that is
    not checked out for the moment, and stays.

    Best effort, unlike `_git_or_raise`'s calls, which the run cannot start without: a branch
    left behind costs an agent that reuses its name a visible `already exists`, while a raise
    would fail every spawn on the repository for as long as, say, another run's git held a ref
    lock. Only the clone check raises, as it would at `worktree add` a moment later."""
    env = {**NO_TOKEN, **pinned_common_dir(clone)}
    stale = await _stale_branches(cli, clone, env, glob)
    if not stale:
        return
    # A ref update runs the reference-transaction hooks a config can define.
    await _refuse_changed(clone, url)
    dropped = 0
    for ref, sha in stale:
        try:
            # The listed sha, and `--no-deref`: the branch as listed, never one moved since
            # or the target of one made symbolic since.
            deleted = await cli.git(
                "-C", str(clone), "update-ref", "--no-deref", "-d", ref, sha, env=env
            )
        except (AgentRuntimeError, OSError) as exc:
            log.warning("shared clone %s: %s not deleted: %s", clone, ref, exc)
            continue
        if deleted.ok:
            dropped += 1
        else:
            log.warning(
                "shared clone %s: %s not deleted: %s", clone, ref, deleted.stderr.strip()[:400]
            )
    if dropped:
        log.info("shared clone %s: deleted %d branch(es) origin no longer has", clone, dropped)


async def _stale_branches(
    cli: Cli, clone: Path, env: Mapping[str, str], glob: str
) -> list[tuple[str, str]]:
    """Each local branch matching `glob` with no `origin` counterpart that no worktree holds,
    with the sha it was listed at. None, logged, when git cannot say."""
    try:
        listed = await cli.git(
            "-C",
            str(clone),
            "for-each-ref",
            "--format=%(refname)%00%(objectname)%00%(symref)",
            "refs/heads/",
            "refs/remotes/origin/",
            env=env,
        )
        trees = await cli.git("-C", str(clone), "worktree", "list", "--porcelain", "-z", env=env)
        held = await asyncio.to_thread(_held_mid_operation, clone)
    except (AgentRuntimeError, OSError) as exc:
        log.warning("shared clone %s: stale branches not deleted: %s", clone, exc)
        return []
    if (failed := next((r for r in (listed, trees) if not r.ok), None)) is not None:
        log.warning(
            "shared clone %s: stale branches not deleted: `git %s` exited %d: %s",
            clone,
            " ".join(failed.argv[1:]),
            failed.returncode,
            failed.stderr.strip()[:400],
        )
        return []
    held |= {
        field.removeprefix("branch ")
        for field in trees.stdout.split("\0")
        if field.startswith("branch ")
    }
    branches: dict[str, str] = {}
    remote: set[str] = set()
    for line in listed.stdout.splitlines():
        ref, sha, symref = line.split("\0")
        if ref.startswith("refs/remotes/origin/"):
            remote.add(ref.removeprefix("refs/remotes/origin/"))
        elif not symref:
            branches[ref.removeprefix("refs/heads/")] = sha
    return [
        (f"refs/heads/{name}", sha)
        for name, sha in branches.items()
        if name not in remote and f"refs/heads/{name}" not in held and _in_template(name, glob)
    ]


def _in_template(name: str, glob: str) -> bool:
    """`glob` is the run's branch template with `{slug}` as `*`; with no `*` left, the slug was
    resolved and the template names one branch."""
    return fnmatch.fnmatchcase(name, glob) if "*" in glob else name == glob


def _held_mid_operation(clone: Path) -> set[str]:
    """The branches a rebase or a bisect in progress holds, in any worktree of the clone: both
    detach HEAD, so `worktree list` reports none, and git moves the branch when they end. Read
    as data an agent may have written, which can only keep a branch, never delete one."""
    git_dir = clone / ".git"
    entries = git_dir / "worktrees"
    admins = [git_dir]
    if entries.is_dir() and not entries.is_symlink():
        admins += [e for e in entries.iterdir() if e.is_dir() and not e.is_symlink()]
    held: set[str] = set()
    for admin in admins:
        for name in _HOLDING_FILES:
            try:
                text = read_regular(admin / name).strip()
            except (OSError, ValueError):
                continue
            held.add(text if text.startswith("refs/") else f"refs/heads/{text}")
    return held

async def _refuse_changed(repository: Path, url: str) -> None:
    """The check again, right before a git call that checks files out, carries the token or
    updates a ref: a check one network wait old is one an agent has had all that time to
    outrun. A repository that turned is the next spawn's to replace."""
    if (violation := await asyncio.to_thread(repository_violation, repository, url)) is not None:
        raise SpawnError(
            f"{repository} changed under the checkout ({violation})",
            reason=SpawnReason.INFRASTRUCTURE,
        )


async def prepare_blind_clone(
    cli: Cli,
    run_dir: Path,
    spec: RunSpec,
    env: Mapping[str, str],
    secrets: Mapping[str, str],
) -> Path:
    """A wiped, fresh, single-branch clone — never a worktree of the shared clone.

    The shared clone holds every branch and tag and a worktree of it reaches them all, so the
    blind shape starts from nothing: `--single-branch --branch spec/<slug> --no-tags` brings in
    exactly one branch's history, so no other branch is reachable from the workspace. The
    `runtime.workspace` gates below then prove it held rather than provide it.
    Scoped to the checkout, never the host: this substrate has no host containment.

    The clone may use the minted token — through `git_env`'s env-var helper, which never
    touches disk — but the workspace must not retain one, which is what
    `assert_no_retained_credential` verifies whatever the clone authenticated with.
    """
    slug = blind_slug_from_base(spec.workspace.base)
    hidden = build_hidden_globs(spec.workspace.hidden, slug)
    url = remote_url(spec.workspace.repo, env.get("GIT_HTTP_HOST", ""))

    # GIT_TERMINAL_PROMPT=0 unconditionally — `git_env` only sets it when a token exists, and
    # a token-less probe against a private repo would otherwise hang on a username prompt
    # forever instead of failing into the classification below.
    git_env = {**env, "GIT_TERMINAL_PROMPT": "0"}

    # Prove the spec branch exists on the remote BEFORE cloning — `_start_point`'s honesty
    # rule: a branch absent now is absent on the next tick too (WORKSPACE), while a clone that
    # fails after this probe passed is host or network trouble (INFRASTRUCTURE).
    probe = await cli.git("ls-remote", "--heads", url, spec.workspace.base, env=git_env)
    if not probe.ok:
        raise classify_probe_failure(
            spec.workspace.repo, probe, has_token=bool(env.get(ENV_GH_TOKEN))
        )
    wanted = f"refs/heads/{spec.workspace.base}"
    if not any(line.split("\t")[-1] == wanted for line in probe.stdout.splitlines()):
        raise SpawnError(
            f"the blind run's spec branch {spec.workspace.base!r} does not exist on "
            f"{spec.workspace.repo}. A blind run clones only that branch, so there is nothing "
            "to check out. Push the spec branch, or fix `workspace.base`",
            # WORKSPACE: the ref will not appear on its own; a retry reads the same remote.
            reason=SpawnReason.WORKSPACE,
        )

    worktree = run_dir / WORKTREE_DIRNAME
    # The wipe: nothing from any earlier attempt survives into a blind checkout, however the
    # directory came to exist.
    await asyncio.to_thread(shutil.rmtree, worktree, ignore_errors=True)
    await _git_or_raise(
        cli,
        "clone",
        "--no-checkout",  # see `prepare_worktree`; the checkout follows, after the check
        "--single-branch",
        "--no-tags",
        # Defence in depth: a local clone (git's optimization for path-shaped sources)
        # hardlinks the entire object store, so every hidden object would arrive despite
        # `--single-branch`, as dangling objects `git show` still reads. An https URL never
        # takes that path today; this keeps it so against a future `url.insteadOf` rewrite.
        "--no-local",
        "--branch",
        spec.workspace.base,
        url,
        str(worktree),
        env=git_env,
    )
    await _refuse_changed(worktree, url)
    await _git_or_raise(cli, "-C", str(worktree), "reset", "--quiet", "--hard", env=NO_TOKEN)
    # Host-side, before any agent process exists against `worktree` — the placement is the
    # security property (runtime/workspace.py). No opt-out, and `IsolationError` propagates
    # untouched: a blind checkout that is not blind is a containment failure, not a retry.
    await asyncio.to_thread(assert_isolated, worktree, hidden)
    await asyncio.to_thread(assert_no_retained_credential, worktree, secrets)
    return worktree


async def park_workspace(
    cli: Cli,
    root: Path,
    run_dir: Path,
    *,
    credentials: Sequence[str],
    interrupted: bool,
    repo: str,
    worktree: Path | None,
    workspace_dir: Path | None = None,
) -> tuple[str, ...]:
    """Keep a finished run's workspace without its credentials (decision 54), and return why
    HEAD could not be detached, for the run's stream warnings. The credentials are in
    `workspace_dir`'s config home when the run continues another run's workspace.

    Every end deletes the credential files; a resume writes them again. An interrupted run keeps
    its images and its branch checked out, for the session it resumes. Any other end deletes its
    images and detaches HEAD: the agent checked its branch out in this worktree, and git refuses
    that branch to any other worktree while this one holds it, so the next attempt could not
    check it out. `worktree` is None for a blind clone, which shares no branch with anything.
    """
    await asyncio.to_thread(delete_credentials, root, workspace_dir or run_dir, credentials)
    if interrupted:
        return ()
    return await end_park(cli, root, run_dir, repo=repo, worktree=worktree)


async def end_park(
    cli: Cli, root: Path, run_dir: Path, *, repo: str, worktree: Path | None
) -> tuple[str, ...]:
    """What every end but an interrupt drops beyond the credentials: the images and the branch.
    An interrupted run accepted without a resume gets the same."""
    await asyncio.to_thread(remove_attachments, run_dir)
    if worktree is None:
        return ()
    why = await detach_head(cli, root, repo, worktree)
    if why is None:
        return ()
    log.warning("run %s: HEAD was not detached: %s", run_dir.name, why)
    return (f"HEAD was not detached, so the branch may still be held: {why}",)


async def detach_head(cli: Cli, root: Path, repo: str, worktree: Path) -> str | None:
    """Release the branch the worktree holds; returns why it could not."""
    try:
        clone = contained(root, root / CLONES_DIRNAME / repo, create=False)
    except SpawnError as exc:
        return str(exc)
    admin = await asyncio.to_thread(_admin_dir_of, clone, worktree)
    if admin is None:
        return "the clone registers no administrative directory for this worktree"
    if (violation := await _clone_refused(clone, repo)) is not None:
        return violation
    # Named outright: `-C <worktree>` would walk up past a `.git` the agent removed, into
    # whatever repository holds the run area.
    git_at = ("--git-dir", str(admin), "--work-tree", str(worktree))
    env = {**NO_TOKEN, **pinned_common_dir(clone)}
    # A run that ended mid-bisect or mid-rebase holds its branch through that state, not HEAD.
    # `bisect reset` would check the branch out again, so its start file goes by hand.
    await asyncio.to_thread(_unlink_within, clone, admin / "BISECT_START")
    try:
        await cli.git(*git_at, "rebase", "--quit", env=env)
        # `update-ref`, not `checkout --detach`: it never reads the index, so the index.lock a
        # killed agent can leave behind does not stop it.
        detached = await cli.git(*git_at, "update-ref", "--no-deref", "HEAD", "HEAD", env=env)
    except (AgentRuntimeError, OSError) as exc:
        return str(exc)
    return None if detached.ok else detached.stderr.strip()[:400]


async def held_branch(cli: Cli, root: Path, repo: str, worktree: Path) -> str | None:
    """The branch the worktree has checked out, read before a park detaches HEAD, so a rework
    can check it out again. None when HEAD is detached or git cannot say."""
    try:
        git_at = await asyncio.to_thread(worktree_git_at, root, repo, worktree)
        if git_at is None:
            return None
        clone = root / CLONES_DIRNAME / repo
        if (violation := await _clone_refused(clone, repo)) is not None:
            log.warning("not reading the branch %s holds: %s", worktree, violation)
            return None
        env = {**NO_TOKEN, **pinned_common_dir(clone)}
        shown = await cli.git(*git_at, "symbolic-ref", "--quiet", "HEAD", env=env)
    except (AgentRuntimeError, OSError):
        return None
    ref = shown.stdout.strip()
    return ref.removeprefix("refs/heads/") if shown.ok and ref.startswith("refs/heads/") else None


async def _clone_refused(clone: Path, repo: str, host: str = "") -> str | None:
    """Why host git must not touch a kept worktree of `clone`: a config-defined hook runs even
    on git that never leaves the machine (design.md §7.13)."""
    violation = await asyncio.to_thread(repository_violation, clone, remote_url(repo, host))
    return None if violation is None else f"the shared clone was refused ({violation})"


def worktree_git_at(root: Path, repo: str, worktree: Path) -> tuple[str, ...] | None:
    """git's options naming the worktree and its administrative directory outright, or None
    when the clone registers none for it: `-C <worktree>` would walk up past a `.git` the agent
    removed, into whatever repository holds the run area."""
    try:
        clone = contained(root, root / CLONES_DIRNAME / repo, create=False)
    except SpawnError:
        return None
    admin = _admin_dir_of(clone, worktree)
    return None if admin is None else ("--git-dir", str(admin), "--work-tree", str(worktree))


def _admin_dir_of(clone: Path, worktree: Path) -> Path | None:
    """The clone's administrative directory for `worktree`, found by what git recorded in the
    clone rather than by the worktree's pointer or a state file, both of which the agent can
    write. git reuses a freed name (`worktree`, `worktree1`, ...), so a name proves nothing."""
    entries = clone / ".git" / "worktrees"
    # The clone's `.git` is the codex sandbox's writable root: a link there could name another
    # clone's entries, whose directory would become the next launch's writable root.
    if entries.is_symlink() or not entries.is_dir():
        return None
    within, target = entries.resolve(), (worktree / ".git").resolve()
    if within != clone.resolve() / ".git" / "worktrees":
        return None
    found: list[Path] = []
    for admin in sorted(entries.iterdir()):
        # What is returned gets rmtree'd: never a link, never anything outside the entries.
        if admin.is_symlink() or admin.resolve().parent != within:
            continue
        try:
            named = read_regular(admin / "gitdir").strip()
        except (OSError, ValueError):
            continue
        if named and (admin / named).resolve() == target:
            found.append(admin.resolve())
    return found[0] if len(found) == 1 else None


def _within_entries(clone: Path, admin: Path) -> bool:
    """`admin` is still one of the clone's entries, checked again right before it is deleted
    from: the agent can swap any directory on the way for a link after `_admin_dir_of` looked.
    `clone` comes resolved from `contained`, so a link anywhere along it shows here."""
    entries = clone / ".git" / "worktrees"
    return entries.resolve() == entries and admin.resolve().parent == entries


def _unlink_within(clone: Path, path: Path) -> None:
    if _within_entries(clone, path.parent):
        path.unlink(missing_ok=True)


async def remove_workspace(root: Path, *, run_dir: Path, repo: str, blind: bool) -> None:
    """Remove a run's checkout. Best effort: a leftover directory costs disk, not correctness.

    No git at all. `worktree remove` deletes the path a worktree's administrative `gitdir` file
    names, a file any agent in the run area can rewrite; `worktree prune` takes every kept
    worktree without its `.git` pointer for orphaned, and would delete theirs too. So the
    tree goes by hand, and its administrative directory after it, found in the clone. The
    tree is the run's own `run_dir/worktree`, never the path its state file records: that
    file sits in the run area too."""
    tree = run_dir / WORKTREE_DIRNAME
    if blind:
        # A standalone clone, not a worktree: nothing in the shared clone references it.
        await asyncio.to_thread(_rmtree, tree)
        return
    clone = contained(root, root / CLONES_DIRNAME / repo, create=False)
    admin = await asyncio.to_thread(_admin_dir_of, clone, tree)
    await asyncio.to_thread(_rmtree, tree)
    if admin is not None and await asyncio.to_thread(_within_entries, clone, admin):
        await asyncio.to_thread(_rmtree, admin)

def _rmtree(path: Path) -> None:
    """Remove a tree the agent may have made read-only, or replaced with a symlink, which is
    unlinked rather than followed. What still resists is logged once."""
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return
    if not path.exists():
        return
    failed: list[str] = []
    retried: set[str] = set()

    def retry(func: Callable[..., object], name: str, exc: BaseException) -> None:
        if name in retried:
            failed.append(f"{name}: {exc}")
            return
        retried.add(name)
        try:
            os.chmod(os.path.dirname(name), 0o700)
            if os.path.isdir(name) and not os.path.islink(name):
                os.chmod(name, 0o700)
                if func not in (os.rmdir, os.unlink, os.remove):
                    # A directory it could not open or list (`os.open`, `os.scandir`): its
                    # contents were never walked, so the walk starts again inside it.
                    shutil.rmtree(name, onexc=retry)
                    return
            func(name)
        except Exception as again:
            failed.append(f"{name}: {again}")

    shutil.rmtree(path, onexc=retry)
    if failed:
        log.warning("removing %s left %d path(s), the first %s", path, len(failed), failed[0])


#: A terminal run younger than this may not be recorded by the orchestrator yet.
REAP_GRACE: Final = timedelta(minutes=15)
#: A run directory with no state that young may be a spawn still checking out a large clone.
FAILED_SPAWN_GRACE: Final = timedelta(hours=1)


class Reapable(NamedTuple):
    """What the reaper and the credential sweep read from one adapter's state file."""

    run_id: str
    status: RunStatus
    repo: str
    worktree: str
    blind: bool
    credentials: tuple[str, ...]
    #: Where the credentials are when the run continues another run's workspace.
    workspace_dir: Path | None = None


#: An adapter's reader of one run directory's state text: it raises ValueError on a state that
#: is not its own.
Parse = Callable[[Path, str], Reapable]


async def reap_workspaces(
    cli: Cli,
    root: Path,
    keep: Collection[str],
    *,
    now: datetime,
    state_file: str,
    parse: Parse,
    session_files: Sequence[str] = (),
) -> tuple[str, ...]:
    """Remove the worktree, config home and images of every terminal run under `root` that
    `keep` does not name (decision 54), and return their ids, with the `session_files` beside a
    run: the raw copies of what its session wrote. `state.json` and the stream stay, so the
    run's activity still reads. A run directory whose state `parse` cannot read belongs
    to another adapter sharing the root, and is skipped; one with no state at all is a spawn
    that failed, and loses its config home and images once past the grace."""
    reaped: list[str] = []
    for run_dir, run, written in await asyncio.to_thread(_terminal_runs, root, state_file, parse):
        if run.run_id in keep or now - written < REAP_GRACE:
            continue
        try:
            if await _reap_one(cli, root, run_dir, run, written, state_file, parse, session_files):
                reaped.append(run.run_id)
        except Exception:
            # One run's tree must never stall the pass for every other run.
            log.exception("reaping run %s failed", run.run_id)
    for run_dir in await asyncio.to_thread(_stateless_runs, root, state_file, now):
        try:
            await asyncio.to_thread(_rmtree, run_dir / CONFIG_DIRNAME)
            await asyncio.to_thread(_rmtree, run_dir / ATTACHMENTS_DIRNAME)
        except Exception:
            log.exception("removing the config home of %s failed", run_dir.name)
        else:
            log.info("removed the config home of %s, a run that never started", run_dir.name)
    return tuple(reaped)


async def _reap_one(
    cli: Cli,
    root: Path,
    run_dir: Path,
    run: Reapable,
    written: datetime,
    state_file: str,
    parse: Parse,
    session_files: Sequence[str],
) -> bool:
    worktree = run_dir / WORKTREE_DIRNAME
    config, attachments = run_dir / CONFIG_DIRNAME, run_dir / ATTACHMENTS_DIRNAME
    present = await asyncio.to_thread(_present, worktree, config, attachments)
    # A rework's run works in another run's workspace (decision 56), so its raw session copies
    # can be all its own directory holds.
    raw = [run_dir / name for name in session_files]
    if not any(present) and not any(await asyncio.to_thread(_present, *raw)):
        return False
    if not await asyncio.to_thread(_unchanged, run_dir / state_file, parse, written):
        return False  # resumed, or rewritten, since the listing
    if present[0]:
        if worktree.is_symlink() or Path(run.worktree) != worktree:
            log.warning("run %s names a worktree the reaper does not own", run.run_id)
            return False
        try:
            await remove_workspace(root, run_dir=run_dir, repo=run.repo, blind=run.blind)
        except SpawnError as exc:
            log.warning("run %s: %s; skipped", run.run_id, exc)
            return False
    await asyncio.to_thread(_rmtree, config)
    await asyncio.to_thread(_rmtree, attachments)
    for path in raw:
        await asyncio.to_thread(path.unlink, missing_ok=True)
    return not await asyncio.to_thread(worktree.exists)


def sweep_credentials(
    root: Path,
    *,
    state_file: str,
    parse: Parse,
    before_delete: Callable[[Path], None] | None = None,
    stateless_credentials: Sequence[str] = (),
) -> int:
    """Delete the credential files of every terminal run, interrupted runs included, and return
    how many went: those of runs that ended before every end deleted them. A run directory with
    no state at all is a spawn that died before its first state write, and loses its
    `stateless_credentials`, whatever its age. `before_delete` first ends what of the run can
    still read them. Run once at worker startup, when no resume or spawn can be writing them: a
    deploy restarts the one worker (decision 40), so no other worker is running against the same
    root."""
    deleted = 0
    for run_dir, run, _ in _terminal_runs(root, state_file, parse):
        _end_before_delete(before_delete, run_dir)
        deleted += delete_credentials(root, run.workspace_dir or run_dir, run.credentials)
    for run_dir in _run_dirs(root):
        if not any(_present(run_dir / state_file)):
            _end_before_delete(before_delete, run_dir)
            deleted += delete_credentials(root, run_dir, stateless_credentials)
    return deleted


def _end_before_delete(before_delete: Callable[[Path], None] | None, run_dir: Path) -> None:
    if before_delete is None:
        return
    try:
        before_delete(run_dir)
    except Exception:
        log.exception("ending what run %s left running failed", run_dir.name)


def _present(*paths: Path) -> list[bool]:
    return [path.exists() or path.is_symlink() for path in paths]


def _unchanged(path: Path, parse: Parse, written: datetime) -> bool:
    try:
        run = parse(path.parent, path.read_text("utf-8"))
        again = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except (OSError, ValueError):
        return False
    return run.status.is_terminal and again == written


def _run_dirs(root: Path) -> list[Path]:
    runs = root / RUNS_DIRNAME
    if not runs.is_dir():
        return []
    return [d for d in sorted(runs.iterdir()) if d.is_dir() and not d.is_symlink()]


def _terminal_runs(
    root: Path, state_file: str, parse: Parse
) -> list[tuple[Path, Reapable, datetime]]:
    found: list[tuple[Path, Reapable, datetime]] = []
    for run_dir in _run_dirs(root):
        path = run_dir / state_file
        try:
            run = parse(run_dir, path.read_text("utf-8"))
            written = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except (OSError, ValueError):
            continue
        if run.run_id == run_dir.name and run.status.is_terminal:
            found.append((run_dir, run, written))
    return found


def _stateless_runs(root: Path, state_file: str, now: datetime) -> list[Path]:
    found: list[Path] = []
    for run_dir in _run_dirs(root):
        config, attachments = run_dir / CONFIG_DIRNAME, run_dir / ATTACHMENTS_DIRNAME
        if (run_dir / state_file).exists() or not any(_present(config, attachments)):
            continue
        try:
            stamps = [p.lstat().st_mtime for p in (run_dir, config, attachments) if _present(p)[0]]
        except OSError:
            continue
        if now - datetime.fromtimestamp(max(stamps), UTC) >= FAILED_SPAWN_GRACE:
            found.append(run_dir)
    return found


def probe_git_at(
    root: Path, repo: str, worktree: str, git_dir: str | None, *, blind: bool, host: str = ""
) -> tuple[tuple[str, ...], dict[str, str]]:
    """The `--git-dir` host probes read a run's HEAD through, with the environment that pins
    its common directory, or `()` when the repository it leads to is not one host git may
    touch — the probes then ask GitHub alone, as on E2B.

    Every path here is agent input: `worktree` and `git_dir` come back from the run's state
    file, and the worktree's `.git` pointer and its `commondir` are files in the run area. Each
    must lead back into this repo's own clone before host git follows it."""
    try:
        tree = contained(root / RUNS_DIRNAME, Path(worktree), create=False)
        if blind:
            repository, admin = tree, tree / ".git"
        else:
            repository = root / CLONES_DIRNAME / repo
            admin = _linked_git_dir(repository, tree, git_dir)
        violation = repository_violation(repository, remote_url(repo, host))
        if violation is None:
            return ("--git-dir", str(admin)), pinned_common_dir(repository)
    except (AgentRuntimeError, OSError, ValueError) as exc:
        violation = str(exc)
    log.error("host probes skip %s's local repository: %s", worktree, violation)
    return (), {}


def _linked_git_dir(clone: Path, tree: Path, git_dir: str | None) -> Path:
    if git_dir is None:
        prefix, sep, target = read_regular(tree / ".git").strip().partition(":")
        if not sep or prefix.strip() != "gitdir":
            raise ValueError("the worktree's .git is not a gitdir pointer")
        git_dir = str(tree / target.strip())
    admin = Path(git_dir).resolve()
    common = (clone / ".git").resolve()
    if admin.parent != common / "worktrees":
        raise ValueError("the administrative directory is not in the clone's .git/worktrees")
    if (admin / read_regular(admin / "commondir").strip()).resolve() != common:
        raise ValueError("the administrative directory's commondir is not the clone's .git")
    return admin


def _move_aside(clone: Path) -> Path | None:
    """Where the refused clone now is, for the caller to delete; None when it was already gone.
    The rename takes it out of every later lookup in one step, so nothing finds it
    half-deleted. Runs still working in it lose their repository, which is right — their own
    git would read the same poisoned config."""
    aside = clone.with_name(f".{clone.name}.discarded-{uuid.uuid4().hex[:8]}")
    try:
        clone.rename(aside)
    except FileNotFoundError:
        return None  # a concurrent spawn moved it first
    except OSError as exc:
        raise SpawnError(
            f"cannot move the refused clone {clone} aside: {exc}",
            reason=SpawnReason.INFRASTRUCTURE,
        ) from exc
    return aside

"""`assert_isolated` (+ `assert_no_retained_credential`) — the shared blindness gates every
adapter must call.

Placement is the security property, not the check itself: these run host-side, after an adapter
has prepared a `Workspace(isolation="blind")` checkout and before any agent process exists against
it — never from inside the workspace. A probe run inside proves nothing, because the agent can
delete the tag or ref the probe looks for and still pass it (issue #19).

Glob convention (`Workspace.hidden`, contract.py): an entry starting with `refs/` is a ref glob,
fnmatch'd against every line of `git for-each-ref --format=%(refname)` (local branches,
remote-tracking branches, tags). Every other entry is a path glob, used two ways: check 2
fnmatches it against working-tree paths (`git ls-files` unioned with an on-disk walk, so an
untracked leak counts too); check 4 hands it to git's own pathspec handling in `git log --all
--oneline -- <glob>`, so a bare directory name matches everything under it, no wildcard required.

The five checks, run in this order, each independent and each raising `IsolationError` on its own:

1. Zero tags anywhere in the repo — `--no-tags` clones must carry none.
2. No working-tree path matches a `hidden` path-glob.
3. No local or remote ref matches a `hidden` ref-glob.
4. No commit reachable from ANY ref touches a path matching a `hidden` path-glob — every ref, not
   just HEAD, so a leak that arrived on a side branch and was later removed is still caught.
5. Object containment: every object in the store is reachable from some ref, and no
   `objects/info/alternates` file exists. Checks 1-4 are ref-scoped, and refs are not the floor of
   what a repo can read: a fetch followed by deleting the fetched ref leaves its objects readable
   by plain `git show <sha>`, and an alternates file makes a foreign object store readable the
   same way. A dangling object in a blind checkout is a leak candidate, never garbage.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from runtime.contract import IsolationError
from runtime.hostgit import NO_TOKEN, hardened_env, host_environment

#: How many offending items an IsolationError message shows before truncating — short enough to
#: survive the log truncation this project applies downstream.
_EVIDENCE_LIMIT = 5


def assert_isolated(path: Path, hidden: tuple[str, ...]) -> None:
    """Raise `IsolationError` the instant the git repo at `path` is provably not blind.

    `hidden` is `Workspace.hidden` (glob convention above); empty leaves only checks 1 and 5 with
    anything to look for. Returning None means these five checks found no evidence of a leak —
    clone shape (`--single-branch --no-tags --no-local`) is what provides blindness, this only
    verifies it held.
    """
    path_globs = tuple(entry for entry in hidden if not entry.startswith("refs/"))
    ref_globs = tuple(entry for entry in hidden if entry.startswith("refs/"))

    _assert_no_tags(path)
    _assert_no_hidden_working_tree_paths(path, path_globs)
    _assert_no_hidden_refs(path, ref_globs)
    _assert_no_reachable_commits_touching_hidden_paths(path, path_globs)
    _assert_object_containment(path)


def assert_no_retained_credential(path: Path, secrets: Mapping[str, str]) -> None:
    """Raise `IsolationError` if the git repo at `path` retained any credential material.

    `assert_isolated`'s companion, for a blind checkout whose clone was allowed to authenticate:
    `credential="none"` (contract.Workspace) is a property of the workspace, not of the clone — a
    token may fetch the bytes, but nothing the agent reads out of the checkout may carry one. Same
    placement rule as `assert_isolated`, and the same split: clone hygiene (an env-var credential
    helper that never touches disk) provides the property; this only proves it held.

    Three checks, each raising on its own:

    1. No `credential.*` or `http.*.extraheader` key in the repo's own `--local` config — the two
       shapes git uses to persist authentication. Read with `--list -z` (NUL-separated entries,
       key split from value at the first newline) so a crafted multi-line value cannot smear
       across a line-oriented parse and hide the entry after it. Out of scope, with reasons:
       `core.askpass` and `include.path` point at files outside the workspace, which this
       no-containment substrate lets the agent read with or without the key; `url.*.insteadOf`
       redirects traffic but carries no credential, and a redirect only pays off with one.
    2. No secret value appears anywhere in that config. The error names the offending key only —
       a gate that echoed the value it exists to contain would be its own leak.
    3. No remote URL carries userinfo (`https://x-access-token:ghs_…@github.com/…` is a stored
       credential in all but name). An scp-style `git@github.com:owner/repo` remote trips this
       too: a documented false positive that fails closed, and no blind clone this gate guards
       writes such a remote.
    """
    entries = [
        entry for entry in _git(path, "config", "--local", "--list", "-z").split("\0") if entry
    ]
    for entry in entries:
        key = entry.partition("\n")[0]
        lowered = key.casefold()
        if lowered.startswith("credential.") or (
            lowered.startswith("http.") and lowered.endswith(".extraheader")
        ):
            raise IsolationError(
                f"credential check (git config) failed: {key} is set in the blind workspace's "
                "own config"
            )
    for name, value in secrets.items():
        if value and any(value in entry for entry in entries):
            raise IsolationError(
                f"credential check (git config) failed: the value of secret {name!r} appears in "
                "the blind workspace's own config"
            )
    for remote in _lines(_git(path, "remote")):
        url = _git(path, "remote", "get-url", remote).strip()
        host = url.split("://", 1)[-1].split("/", 1)[0]
        if "@" in host:
            raise IsolationError(
                f"credential check (remote url) failed: remote {remote!r} embeds userinfo in its "
                "url"
            )


def _assert_no_tags(path: Path) -> None:
    tags = _lines(_git(path, "tag", "--list"))
    if tags:
        raise IsolationError(
            f"blindness check 1 (tags) failed: repo carries tags: {_evidence(tags)}"
        )


def _assert_no_hidden_working_tree_paths(path: Path, path_globs: tuple[str, ...]) -> None:
    if not path_globs:
        return
    candidates = _worktree_paths(path)
    matches = sorted(c for c in candidates if _matches_any(c, path_globs))
    if matches:
        raise IsolationError(
            "blindness check 2 (working-tree paths) failed: "
            f"hidden path present in checkout: {_evidence(matches)}"
        )


def _assert_no_hidden_refs(path: Path, ref_globs: tuple[str, ...]) -> None:
    if not ref_globs:
        return
    refs = _lines(_git(path, "for-each-ref", "--format=%(refname)"))
    matches = [ref for ref in refs if _matches_any(ref, ref_globs)]
    if matches:
        raise IsolationError(
            f"blindness check 3 (refs) failed: hidden ref present: {_evidence(matches)}"
        )


def _assert_no_reachable_commits_touching_hidden_paths(
    path: Path, path_globs: tuple[str, ...]
) -> None:
    if not path_globs:
        return
    commits = _lines(_git(path, "log", "--all", "--oneline", "--", *path_globs))
    if commits:
        raise IsolationError(
            "blindness check 4 (reachable commits) failed: a commit reachable from some ref "
            f"touches a hidden path: {_evidence(commits)}"
        )


def _assert_object_containment(path: Path) -> None:
    """Check 5 (module docstring): the object store holds nothing a ref cannot reach, and borrows
    no other store — `git show <sha>` reads any object in it, ref or no ref."""
    alternates_raw = _git(path, "rev-parse", "--git-path", "objects/info/alternates").strip()
    alternates = Path(alternates_raw)
    if not alternates.is_absolute():
        alternates = path / alternates
    if alternates.exists():
        raise IsolationError(
            "blindness check 5 (object containment) failed: objects/info/alternates exists, so "
            "the checkout can read a foreign object store"
        )
    # First token per line is the object id for both commands; sets, not counts, so an exotic
    # listing quirk can never cancel out a real dangling object.
    reachable = {line.split()[0] for line in _lines(_git(path, "rev-list", "--objects", "--all"))}
    stored = {
        line.split()[0]
        for line in _lines(_git(path, "cat-file", "--batch-all-objects", "--batch-check"))
    }
    dangling = sorted(stored - reachable)
    if dangling:
        raise IsolationError(
            f"blindness check 5 (object containment) failed: {len(dangling)} object(s) in the "
            "store are reachable from no ref. `git show` can still read them. A fetch followed "
            f"by deleting the fetched ref leaves this state: {_evidence(dangling)}"
        )


def _worktree_paths(path: Path) -> set[str]:
    """Every path git tracks, unioned with an on-disk walk — a leak does not have to be `git
    add`ed to be a leak."""
    tracked = _lines(_git(path, "ls-files"))
    walked: list[str] = []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), path)
            walked.append(rel.replace(os.sep, "/"))
    return {p for p in (*tracked, *walked) if p}


def _matches_any(candidate: str, globs: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(candidate, glob) for glob in globs)


def _git(path: Path, *args: str) -> str:
    """Run one git query for a gate. A git that cannot answer raises `IsolationError`, never a
    raw `CalledProcessError`: a gate that cannot look must refuse, and callers only handle the
    containment error."""
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**host_environment(), **hardened_env(NO_TOKEN)},
    )
    if result.returncode != 0:
        raise IsolationError(
            f"blindness gate cannot judge {path}: `git {' '.join(args)}` exited "
            f"{result.returncode}: {result.stderr.strip()[-300:]}"
        )
    return result.stdout


def _lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line]


def _evidence(items: list[str], limit: int = _EVIDENCE_LIMIT) -> str:
    shown = ", ".join(items[:limit])
    if len(items) > limit:
        shown += f", and {len(items) - limit} more ({len(items)} total)"
    return shown

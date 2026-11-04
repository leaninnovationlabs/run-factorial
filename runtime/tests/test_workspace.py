"""assert_isolated tests — real throwaway git repos, no git config mutation (env-var
identity only)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from runtime.contract import IsolationError
from runtime.workspace import assert_isolated, assert_no_retained_credential

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(
    repo: Path, *args: str, check: bool = True, input: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        check=check,
        input=input,
    )


def _repo(tmp_path: Path) -> Path:
    d = tmp_path / "co"
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "commit", "-q", "--allow-empty", "-m", "x")
    return d


def _current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


# ------------------------------------------------------------------ clean repo


def test_clean_repo_passes(tmp_path: Path) -> None:
    assert_isolated(_repo(tmp_path), hidden=("acceptance/42-x/**", "refs/*/tests/42-x"))


def test_clean_repo_passes_with_no_hidden_globs_at_all(tmp_path: Path) -> None:
    assert_isolated(_repo(tmp_path), hidden=())


# ------------------------------------------------------------------ check 1: tags


def test_a_tag_fails_it(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "tag", "contract/42-x")
    with pytest.raises(IsolationError, match="check 1"):
        assert_isolated(repo, hidden=())


# ------------------------------------------------------------------ check 2: working-tree paths


def test_a_hidden_path_in_the_working_tree_fails_it(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "acceptance" / "42-x").mkdir(parents=True)
    (repo / "acceptance" / "42-x" / "t.py").write_text("x")
    with pytest.raises(IsolationError, match="check 2"):
        assert_isolated(repo, hidden=("acceptance/42-x/**",))


def test_an_untracked_hidden_path_fails_it_too(tmp_path: Path) -> None:
    """A leak does not need `git add` to count. This working-tree file is deliberately never
    staged."""
    repo = _repo(tmp_path)
    (repo / "acceptance" / "42-x").mkdir(parents=True)
    (repo / "acceptance" / "42-x" / "t.py").write_text("x")
    assert _git(repo, "status", "--porcelain").stdout.strip() != ""
    with pytest.raises(IsolationError, match="check 2"):
        assert_isolated(repo, hidden=("acceptance/42-x/**",))


# ------------------------------------------------------------------ check 3: refs


def test_a_hidden_remote_style_ref_fails_it(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/remotes/origin/tests/42-x", head)
    with pytest.raises(IsolationError, match="check 3"):
        assert_isolated(repo, hidden=("refs/*/tests/42-x",))


def test_a_hidden_local_branch_ref_fails_it(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/heads/tests/42-x", head)
    with pytest.raises(IsolationError, match="check 3"):
        assert_isolated(repo, hidden=("refs/*/tests/42-x",))


# ------------------------------------------------------------------ check 4: reachable commits


def test_a_reachable_commit_touching_a_hidden_path_fails_it_even_off_head(
    tmp_path: Path,
) -> None:
    """The issue-#19 class: a commit that touched a hidden path on a SIDE branch is a leak even
    once a later commit removes the file again and HEAD moves back to base. The side branch's
    ref is deliberately kept (never deleted) — a deleted branch can leave its commits
    unreachable, which would prove nothing about check 4; keeping the ref is what makes the
    commit still reachable via `git log --all`."""
    repo = _repo(tmp_path)
    base = _current_branch(repo)
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "acceptance" / "42-x").mkdir(parents=True)
    (repo / "acceptance" / "42-x" / "t.py").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "leak")
    _git(repo, "rm", "-q", "-r", "acceptance/42-x")
    _git(repo, "commit", "-q", "-m", "remove")
    _git(repo, "checkout", "-q", base)
    assert not (repo / "acceptance" / "42-x").exists()
    # "side" ref is still present — the branch was never deleted.
    assert _git(repo, "show-ref", "--verify", "--quiet", "refs/heads/side", check=False)
    with pytest.raises(IsolationError, match="check 4"):
        assert_isolated(repo, hidden=("acceptance/42-x",))


# ------------------------------------------------------------------ check 5: object containment


def test_a_fetched_then_deleted_ref_still_fails_object_containment(tmp_path: Path) -> None:
    """`git fetch` a hidden branch, delete the fetched ref, and every object it brought stays in
    the store — readable via `git show` — with checks 1-4 all green, because all four are
    ref-scoped. Check 5 is what refuses this shape, and its message names the dangling objects."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "spec/42-x")
    (origin / "SPEC.md").write_text("the spec\n")
    _git(origin, "add", "SPEC.md")
    _git(origin, "commit", "-q", "-m", "spec")
    _git(origin, "checkout", "-q", "-b", "tests/42-x")
    (origin / "acceptance" / "42-x").mkdir(parents=True)
    (origin / "acceptance" / "42-x" / "t.py").write_text("def test(): ...\n")
    _git(origin, "add", "acceptance")
    _git(origin, "commit", "-q", "-m", "acceptance material")
    _git(origin, "checkout", "-q", "spec/42-x")

    clone = tmp_path / "clone"
    subprocess.run(
        [
            "git", "clone", "-q", "--single-branch", "--no-tags", "--no-local",
            "--branch", "spec/42-x", f"file://{origin}", str(clone),
        ],
        env=_GIT_ENV,
        capture_output=True,
        check=True,
    )
    hidden = ("acceptance/42-x", "acceptance/42-x/**", "refs/*/tests/42-x")
    assert_isolated(clone, hidden)  # the clean single-branch clone passes

    _git(clone, "fetch", "-q", "origin", "+refs/heads/tests/42-x:refs/heads/smuggled")
    _git(clone, "update-ref", "-d", "refs/heads/smuggled")
    with pytest.raises(IsolationError, match="check 5") as excinfo:
        assert_isolated(clone, hidden)
    assert "object(s) in the store are reachable from no ref" in str(excinfo.value)


def test_a_dangling_object_alone_fails_object_containment(tmp_path: Path) -> None:
    """Minimal shape: one blob written straight into the store, no ref anywhere near it."""
    repo = _repo(tmp_path)
    _git(repo, "hash-object", "-w", "--stdin", input="leaked bytes\n")
    with pytest.raises(IsolationError, match="check 5"):
        assert_isolated(repo, hidden=())


def test_an_alternates_file_fails_object_containment(tmp_path: Path) -> None:
    """An alternates file makes a FOREIGN object store readable from this checkout — refused
    outright, before any object counting."""
    repo = _repo(tmp_path)
    alternates = repo / ".git" / "objects" / "info" / "alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text("/somewhere/else/objects\n")
    with pytest.raises(IsolationError, match="alternates"):
        assert_isolated(repo, hidden=())


# ------------------------------------------------------------------ the gate can always look


def test_a_directory_that_is_not_a_repo_is_an_isolation_error(tmp_path: Path) -> None:
    """A gate that cannot look must refuse — as the containment error, so no caller reads a
    broken checkout as a retryable substrate fault."""
    not_a_repo = tmp_path / "empty"
    not_a_repo.mkdir()
    with pytest.raises(IsolationError, match="cannot judge"):
        assert_isolated(not_a_repo, hidden=())


# ------------------------------------------------------------------ retained credentials


def test_a_clean_transport_clone_retains_no_credential(tmp_path: Path) -> None:
    origin = _repo(tmp_path)
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--no-local", f"file://{origin}", str(clone)],
        env=_GIT_ENV,
        capture_output=True,
        check=True,
    )
    assert_no_retained_credential(clone, {"GH_TOKEN": "ghs_used_by_the_clone_only"})


def test_an_injected_credential_helper_fails_the_credential_check(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "config", "--local", "credential.helper", "store")
    with pytest.raises(IsolationError, match="credential check"):
        assert_no_retained_credential(repo, {})


def test_an_http_extraheader_fails_the_credential_check(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(
        repo,
        "config", "--local", "http.https://github.com/.extraheader", "AUTHORIZATION: basic abc",
    )
    with pytest.raises(IsolationError, match="credential check"):
        assert_no_retained_credential(repo, {})


def test_a_secret_value_in_the_config_fails_and_names_the_key_not_the_value(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _git(repo, "config", "--local", "sf.leftover", "ghs_theActualTokenValue")
    with pytest.raises(IsolationError) as excinfo:
        assert_no_retained_credential(repo, {"GH_TOKEN": "ghs_theActualTokenValue"})
    message = str(excinfo.value)
    assert "GH_TOKEN" in message
    assert "ghs_theActualTokenValue" not in message  # the gate must not echo what it contains


def test_a_userinfo_remote_url_fails_the_credential_check(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://x-access-token:ghs_tok@github.com/a/b.git")
    with pytest.raises(IsolationError, match="remote url"):
        assert_no_retained_credential(repo, {})


# ------------------------------------------------------------------ evidence + independence


def test_evidence_is_truncated_to_a_few_items(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for i in range(8):
        _git(repo, "tag", f"contract/{i}")
    with pytest.raises(IsolationError) as excinfo:
        assert_isolated(repo, hidden=())
    message = str(excinfo.value)
    assert "8 total" in message
    # Truncated: not all 8 tag names appear verbatim in the message.
    assert not all(f"contract/{i}" in message for i in range(8))


def test_each_check_is_independent_of_the_others(tmp_path: Path) -> None:
    """A repo with only a stray tag and nothing else wrong still fails at check 1, proving check
    1 does not depend on `hidden` naming anything."""
    repo = _repo(tmp_path)
    _git(repo, "tag", "stray")
    with pytest.raises(IsolationError, match="check 1"):
        assert_isolated(repo, hidden=("nothing/matches/**", "refs/nothing/matches"))

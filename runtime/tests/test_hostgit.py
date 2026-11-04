"""Host git against a clone an agent can write (design.md §7.13) — real git, a `file://` origin,
and marker files that a planted program would create if host git ever ran it.

Every token here is fake and every git runs without system or global config, so no real
credential is reachable; assertions on git output are booleans, so none is ever printed."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from runtime.adapters._cli import checkout, rework
from runtime.adapters._cli.checkout import detach_head, prepare_worktree, worktree_git_at
from runtime.adapters._cli.environment import git_env, view_git_env
from runtime.adapters._cli.process import Cli, CommandResult
from runtime.adapters._cli.rework import ReworkRefused, rework_checkout
from runtime.contract import AgentPrompts, RunSpec, SpawnError, Workspace
from runtime.hostgit import github_url, hardened_env, remote_url
from runtime.workspace import assert_isolated

REPO = "acme/widgets"
URL = f"https://github.com/{REPO}.git"
TOKEN = "ghs_FAKE"
BRANCH = "spec/1-x"

_CLEAN = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_ASKPASS": "",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(*args: str) -> None:
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", *args],
        capture_output=True,
        check=True,
        env={**os.environ, **_CLEAN},
    )


def _origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    _git("init", "-q", "--bare", str(origin))
    _git("init", "-q", str(seed))
    (seed / "a.txt").write_text("hi\n")
    _git("-C", str(seed), "add", "a.txt")
    _git("-C", str(seed), "commit", "-qm", "init")
    _git("-C", str(seed), "push", "-q", str(origin), "HEAD:refs/heads/main")
    return origin


def _no_real_helper(clone: Path, env: Mapping[str, str]) -> None:
    """Before any credential command: git may name only a helper from the clone's own config or
    the env. Apple's git also reads Xcode's config, which names osxkeychain; a real store is never
    told."""
    listed = subprocess.run(
        [
            "git",
            "-C",
            str(clone),
            "config",
            "--show-origin",
            "--get-regexp",
            r"^credential\..*helper$",
        ],
        capture_output=True,
        text=True,
        env=dict(env),
    ).stdout
    for line in listed.splitlines():
        origin = line.split("\t", 1)[0]
        inside = origin.startswith("file:") and (clone / origin[5:]).resolve().is_relative_to(
            clone.resolve()
        )
        assert origin == "command line:" or inside, f"a real helper is reachable: {listed}"


def _shared_clone(root: Path, origin: Path) -> Path:
    """What `prepare_worktree` leaves at `repos/<repo>`: a clone whose origin is GitHub's URL."""
    clone = root / "repos" / REPO
    _git("clone", "-q", f"file://{origin}", str(clone))
    _git("-C", str(clone), "remote", "set-url", "origin", URL)
    return clone


def _plant(clone: Path, markers: Path) -> None:
    """Every planted program touches a marker named for itself."""

    def script(name: str) -> str:
        path = markers / f"{name}.sh"
        path.write_text(f"#!/bin/sh\ntouch {markers}/{name}\ncat\n")
        path.chmod(0o755)
        return str(path)

    _git("-C", str(clone), "config", "credential.helper", f"!{script('CREDENTIAL')}")
    _git("-C", str(clone), "config", "core.fsmonitor", script("FSMONITOR"))
    _git("-C", str(clone), "config", "core.askPass", script("ASKPASS"))
    _git("-C", str(clone), "config", "filter.evil.smudge", script("SMUDGE"))
    (clone / ".git" / "info" / "attributes").write_text("* filter=evil\n")
    hook = clone / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {markers}/HOOK\n")
    hook.chmod(0o755)


def _local_origin_env(origin: Path) -> dict[str, str]:
    """The adapter's own environment (the token helper), plus the test's way to GitHub: a URL
    rewrite and the file transport, which the hardening blocks unless its caller asks."""
    env = git_env({"GH_TOKEN": TOKEN})
    count = int(env["GIT_CONFIG_COUNT"])
    for i, (key, value) in enumerate(
        [(f"url.file://{origin}.insteadOf", URL), ("protocol.file.allow", "always")],
        start=count,
    ):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    env["GIT_CONFIG_COUNT"] = str(count + 2)
    return env


def _spec(branch: str = "spec/1-{slug}") -> RunSpec:
    return RunSpec(
        agent="plan-agent",
        ticket="GH-1",
        workspace=Workspace(repo=REPO, base="main", branch=branch),
        prompts=AgentPrompts(system="s", instructions="i"),
        model="gpt-5.1-codex",
        max_turns=1,
    )


async def test_host_git_runs_nothing_a_clone_plants_and_still_presents_the_token(
    tmp_path: Path,
) -> None:
    """The command-scope layer alone, on a clone the content check never saw — the window a
    concurrent agent can write in. A planted helper, fsmonitor, askpass or `.git/hooks` hook
    must not run on the host, and the adapter's token helper must still answer after the reset.
    A config-defined hook (`hook.<name>.*`) is not one of these: only the check refuses it."""
    markers = tmp_path / "markers"
    markers.mkdir()
    clone = _shared_clone(tmp_path / "root", _origin(tmp_path))
    _plant(clone, markers)
    (clone / ".git" / "info" / "attributes").unlink()  # no switch for a filter: the check's job
    env = _local_origin_env(tmp_path / "origin.git")

    def fill(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        full = {**os.environ, **_CLEAN, **hardened_env(env)}
        _no_real_helper(clone, full)
        return subprocess.run(
            ["git", "-C", str(clone), "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            text=True,
            env=full,
        )

    # Booleans, never the output: on a dev box an unhardened git answers with the operator's
    # own credential, and a failing assertion would print it.
    presented = f"password={TOKEN}" in fill(env).stdout
    assert presented, "the adapter's helper answers after the reset"
    refused = fill({}).returncode != 0
    assert refused, "no token, and nothing planted may supply one"
    cli = Cli(agent_bin="true")
    worktree = tmp_path / "root" / "runs" / "r1" / "worktree"
    for args in (
        ("fetch", "--prune", URL, "+refs/heads/*:refs/remotes/origin/*"),
        ("worktree", "add", "--detach", str(worktree), "origin/main"),
    ):
        result = await cli.git("-C", str(clone), *args, env=env)
        assert result.ok, result.stderr
    assert (await cli.git("-C", str(worktree), "status", env=env)).ok
    assert_isolated(clone, ("nothing/**",))

    assert sorted(p.name for p in markers.iterdir() if not p.suffix) == []


def test_an_open_sessions_git_runs_no_hook_or_fsmonitor_its_checkout_names(
    tmp_path: Path,
) -> None:
    """An open session (decision 57) runs the agent's CLI beside the model key, in a checkout whose
    config the agent wrote. Its env turns hooks and fsmonitor off above that config, after the
    `GIT_CONFIG_*` entries it already carries, which it keeps."""
    markers = tmp_path / "markers"
    markers.mkdir()
    clone = _shared_clone(tmp_path / "root", _origin(tmp_path))
    _plant(clone, markers)
    (clone / ".git" / "info" / "attributes").unlink()  # a filter still runs: design.md §7.10
    env = view_git_env(git_env({"GH_TOKEN": TOKEN}))

    for args in (("status",), ("checkout", "-q", "main")):
        subprocess.run(
            ["git", "-C", str(clone), *args],
            capture_output=True,
            check=True,
            env={**os.environ, **_CLEAN, **env},
        )

    assert sorted(p.name for p in markers.iterdir() if not p.suffix) == []
    assert env["GIT_CONFIG_COUNT"] == "4"
    assert env["GIT_CONFIG_KEY_1"] == "credential.https://github.com.helper"


def test_the_runs_helper_is_the_only_credential_helper_git_asks(tmp_path: Path) -> None:
    """macOS's system git and Homebrew's name `credential.helper=osxkeychain` in a config file,
    which git asks before a helper from the environment: the agent would push as the operator,
    and git would store each App token in the operator's keychain. Here the file is a scratch
    one holding a helper that would answer; no real store is ever reachable or told."""
    asked = tmp_path / "asked"
    helper = tmp_path / "helper"
    helper.write_text(
        f'#!/bin/sh\necho "$1" >> {asked}\necho username=operator\necho password=OPERATOR\n'
    )
    helper.chmod(0o755)
    scratch = tmp_path / "gitconfig"
    scratch.write_text(f"[credential]\n\thelper = {helper}\n")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        # Apple's git reads a config of its own beside the system one (Xcode's, naming
        # osxkeychain), which only this hides.
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": str(scratch),
        **git_env({"GH_TOKEN": TOKEN}),
    }

    def git_(*args: str, given: str | None = None) -> str:
        done = subprocess.run(
            ["git", *args], input=given, env=env, capture_output=True, text=True, check=True
        )
        return done.stdout

    listed = git_("config", "--show-origin", "--get-regexp", r"^credential\..*helper$")
    origins = {line.split("\t", 1)[0] for line in listed.splitlines()}
    assert origins == {f"file:{scratch}", "command line:"}, f"a real helper is reachable: {listed}"

    filled = git_("credential", "fill", given="protocol=https\nhost=github.com\n\n")

    assert f"password={TOKEN}" in filled
    assert not asked.exists(), f"git asked the file's helper: {asked.read_text()!r}"


async def test_a_poisoned_shared_clone_is_replaced_before_any_checkout(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A filter driver has no command-scope switch: any name works. The content check must see
    it before `worktree add` checks a file out, and the run must get a fresh clone."""
    markers = tmp_path / "markers"
    markers.mkdir()
    root = tmp_path / "root"
    origin = _origin(tmp_path)
    clone = _shared_clone(root, origin)
    _plant(clone, markers)
    spec = _spec()
    run_dir = root / "runs" / "r1"
    run_dir.mkdir(parents=True)

    with caplog.at_level(logging.ERROR):
        checkout = await prepare_worktree(
            Cli(agent_bin="true"),
            root.resolve(),
            run_dir,
            spec,
            _local_origin_env(origin),
            drop_git_pointer=False,
        )

    assert sorted(p.name for p in markers.iterdir() if not p.suffix) == []
    assert (checkout.worktree / "a.txt").read_text() == "hi\n"
    assert not (clone / ".git" / "info" / "attributes").exists(), "the clone was replaced"
    assert "core.askpass" in caplog.text
    assert str(markers) not in caplog.text, "the log names the key, never its value"


async def test_a_repository_cannot_turn_a_transport_back_on(tmp_path: Path) -> None:
    """`protocol.<name>.allow` in a repository's own config beats `protocol.allow=never` from
    any scope, and `ext::` runs a command with the token in its environment. Pinned per
    protocol, a rewrite of the URL onto it goes nowhere."""
    markers = tmp_path / "markers"
    markers.mkdir()
    clone = _shared_clone(tmp_path / "root", _origin(tmp_path))
    ext = markers / "ext.sh"
    ext.write_text(f"#!/bin/sh\ntouch {markers}/EXT\nexit 1\n")
    ext.chmod(0o755)
    _git("-C", str(clone), "config", f"url.ext::{ext} %S.insteadOf", URL)
    _git("-C", str(clone), "config", "protocol.ext.allow", "always")

    result = await Cli(agent_bin="true").git(
        "-C", str(clone), "fetch", URL, env=git_env({"GH_TOKEN": TOKEN})
    )

    refused = not result.ok
    assert refused
    assert sorted(p.name for p in markers.iterdir() if not p.suffix) == []


class _RecordingCli(Cli):
    def __init__(self) -> None:
        super().__init__(agent_bin="true")
        self.git_calls: list[tuple[str, ...]] = []

    async def git(
        self, *args: str, env: Mapping[str, str] | None = None, inherit_host_env: bool = True
    ) -> CommandResult:
        self.git_calls.append(args)
        return await super().git(*args, env=env, inherit_host_env=inherit_host_env)


async def test_a_repository_planted_while_the_clone_is_replaced_gets_no_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent that forces the replacement knows the path is about to be empty, and can put a
    repository of its own there — a proxy with TLS checks off, say — before the token-bearing
    fetch. That fetch must see the check again first."""
    root = tmp_path / "root"
    clone = _shared_clone(root, _origin(tmp_path))
    _git("-C", str(clone), "config", "filter.evil.smudge", "cat")
    move_aside = checkout._move_aside

    def move_aside_then_plant(path: Path) -> Path | None:
        aside = move_aside(path)
        _git("init", "-q", str(path))
        _git("-C", str(path), "config", "http.proxy", "http://127.0.0.1:9")
        _git("-C", str(path), "config", "http.sslVerify", "false")
        return aside

    monkeypatch.setattr(checkout, "_move_aside", move_aside_then_plant)
    cli = _RecordingCli()
    run_dir = root / "runs" / "r1"
    run_dir.mkdir(parents=True)

    with pytest.raises(SpawnError):
        await prepare_worktree(
            cli,
            root.resolve(),
            run_dir,
            _spec(),
            _local_origin_env(tmp_path / "origin.git"),
            drop_git_pointer=False,
        )

    assert not [c for c in cli.git_calls if "fetch" in c]


def _reworkable(tmp_path: Path) -> tuple[Path, str, tuple[str, ...]]:
    """A shared clone, a parked run's worktree of it, and its pull request's branch moved on by a
    review commit: what a rework takes (decision 56). Returns the clone, the review commit and
    the worktree's git options."""
    origin = _origin(tmp_path)
    seed = tmp_path / "seed"
    _git("-C", str(seed), "push", "-q", str(origin), f"HEAD:refs/heads/{BRANCH}")
    root = tmp_path / "root"
    clone = _shared_clone(root, origin)
    worktree = root / "runs" / "r1" / "worktree"
    _git("-C", str(clone), "worktree", "add", "-q", "--detach", str(worktree), f"origin/{BRANCH}")
    (seed / "b.txt").write_text("review\n")
    _git("-C", str(seed), "add", "b.txt")
    _git("-C", str(seed), "commit", "-qm", "review")
    _git("-C", str(seed), "push", "-q", str(origin), f"HEAD:refs/heads/{BRANCH}")
    head = _head(seed)
    git_at = worktree_git_at(root, REPO, worktree)
    assert git_at is not None
    return clone, head, git_at


def _head(repository: Path) -> str:
    shown = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_CLEAN},
    )
    return shown.stdout.strip()


def _approved(clone: Path, env: Mapping[str, str]) -> bool:
    """What git does with a credential a fetch used: every helper it names is told the token.
    Never the system or global config here, whatever `env` says: no real store is ever told."""
    full = {
        **env,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    _no_real_helper(clone, full)
    approve = subprocess.run(
        ["git", "-C", str(clone), "credential", "approve"],
        input=f"protocol=https\nhost=github.com\nusername=x\npassword={TOKEN}\n\n",
        capture_output=True,
        text=True,
        env=full,
    )
    return approve.returncode == 0


def _holds(clone: Path, sha: str) -> bool:
    shown = subprocess.run(
        ["git", "-C", str(clone), "cat-file", "-e", sha],
        capture_output=True,
        env={**os.environ, **_CLEAN},
    )
    return shown.returncode == 0


class _FetchEnvCli(Cli):
    """Keeps the environment the fetch ran with, the one call that carries the token."""

    def __init__(self) -> None:
        super().__init__(agent_bin="true")
        self.fetch_env: dict[str, str] = {}

    async def run(
        self,
        program: str,
        *args: str,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        inherit_host_env: bool = True,
    ) -> CommandResult:
        if "fetch" in args:
            self.fetch_env = dict(env or {})
        return await super().run(
            program,
            *args,
            env=env,
            cwd=cwd,
            inherit_host_env=inherit_host_env,
        )


async def _rework(cli: Cli, tmp_path: Path, clone: Path, git_at: tuple[str, ...]) -> str:
    (tmp_path / "home").mkdir(exist_ok=True)
    return await rework_checkout(
        cli,
        git_at,
        clone=clone,
        repo=REPO,
        branch=BRANCH,
        glob="spec/1-*",
        run_id="r2",
        env=_local_origin_env(tmp_path / "origin.git"),
        home=tmp_path / "home",
    )


async def test_a_rework_runs_nothing_the_clone_plants_and_hands_it_no_token(
    tmp_path: Path,
) -> None:
    """codex's sandbox lets the agent write the shared clone its kept worktree belongs to. The
    rework's fetch, stash and checkout must run no helper, fsmonitor or hook planted there, and
    a planted helper must never hear the token the fetch carries."""
    markers = tmp_path / "markers"
    markers.mkdir()
    clone, head, git_at = _reworkable(tmp_path)
    _plant(clone, markers)
    # What the check refuses is a case of its own; these are what it lets through.
    (clone / ".git" / "info" / "attributes").unlink()
    for key in ("filter.evil.smudge", "core.askPass"):
        _git("-C", str(clone), "config", "--unset", key)
    cli = _FetchEnvCli()

    header = await _rework(cli, tmp_path, clone, git_at)

    assert head in header
    assert _approved(clone, cli.fetch_env)
    assert sorted(p.name for p in markers.iterdir() if not p.suffix) == []


async def test_a_rework_refuses_a_clone_whose_origin_names_another_remote(
    tmp_path: Path,
) -> None:
    """Fetched by name, the pull request's head would come from wherever the clone's `origin`
    points, with the token: the agent's own remote and commit."""
    clone, _, git_at = _reworkable(tmp_path)
    elsewhere = tmp_path / "elsewhere.git"
    _git("clone", "-q", "--bare", f"file://{tmp_path / 'origin.git'}", str(elsewhere))
    seed = tmp_path / "seed"
    (seed / "c.txt").write_text("the agent's\n")
    _git("-C", str(seed), "add", "c.txt")
    _git("-C", str(seed), "commit", "-qm", "the agent's")
    _git("-C", str(seed), "push", "-q", "--force", str(elsewhere), f"HEAD:refs/heads/{BRANCH}")
    planted = _head(seed)
    _git("-C", str(clone), "remote", "set-url", "origin", f"file://{elsewhere}")

    with pytest.raises(ReworkRefused):
        await _rework(Cli(agent_bin="true"), tmp_path, clone, git_at)

    assert not _holds(clone, planted)


def _redirect_commondir(clone: Path, git_at: tuple[str, ...], markers: Path) -> None:
    """What a codex agent can do from its sandbox: copy the clone's `.git` into the run area,
    give the copy a config-defined hook, a filter and the attributes that select it, and point
    the worktree's `commondir` at the copy. The hook also says whether it saw the token."""
    copy = clone.parent.parent.parent / "runs" / "r1" / "common"
    shutil.copytree(clone / ".git", copy, symlinks=True)
    shutil.rmtree(copy / "worktrees")
    hook, smudge = markers / "hook.sh", markers / "filter.sh"
    hook.write_text(
        f'#!/bin/sh\ntouch {markers}/HOOK\n[ -n "$GH_TOKEN" ] && touch {markers}/TOKEN\nexit 0\n'
    )
    smudge.write_text(f"#!/bin/sh\ntouch {markers}/FILTER\ncat\n")
    for script in (hook, smudge):
        script.chmod(0o755)
    config = str(copy / "config")
    _git("config", "--file", config, "hook.planted.command", str(hook))
    _git("config", "--file", config, "hook.planted.event", "reference-transaction")
    _git("config", "--file", config, "--add", "hook.planted.event", "post-checkout")
    _git("config", "--file", config, "filter.planted.smudge", str(smudge))
    _git("config", "--file", config, "filter.planted.clean", str(smudge))
    (copy / "info").mkdir(exist_ok=True)
    (copy / "info" / "attributes").write_text("* filter=planted\n")
    (Path(git_at[1]) / "commondir").write_text(f"{copy}\n")


async def test_a_worktree_whose_commondir_leads_elsewhere_is_refused(tmp_path: Path) -> None:
    """git takes a linked worktree's common directory, and with it the config, from its
    `commondir`: one leading out of the clone puts config no check read in front of host git.
    The rework and the HEAD detach both refuse it, and nothing planted there runs."""
    markers = tmp_path / "markers"
    markers.mkdir()
    clone, _, git_at = _reworkable(tmp_path)
    _redirect_commondir(clone, git_at, markers)
    (Path(git_at[3]) / "dirty.txt").write_text("x\n")

    with pytest.raises(ReworkRefused):
        await _rework(Cli(agent_bin="true"), tmp_path, clone, git_at)
    why = await detach_head(Cli(agent_bin="true"), tmp_path / "root", REPO, Path(git_at[3]))

    assert why is not None and "commondir" in why
    assert sorted(p.name for p in markers.iterdir() if not p.suffix) == []


@pytest.mark.parametrize("call", ["rework", "detach"])
async def test_a_commondir_redirected_after_the_check_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    """The check and the git call are two moments, and the agent can write between them: the
    common directory is pinned at command scope, so host git never reads the redirect's config.
    The fetch that follows the check carries the token."""
    markers = tmp_path / "markers"
    markers.mkdir()
    clone, _, git_at = _reworkable(tmp_path)
    (Path(git_at[3]) / "dirty.txt").write_text("x\n")
    check = checkout.repository_violation
    redirected: list[bool] = []

    def check_then_redirect(repo: Path, url: str) -> str | None:
        violation = check(repo, url)
        if violation is None and not redirected:
            _redirect_commondir(clone, git_at, markers)
            redirected.append(True)
        return violation

    monkeypatch.setattr(rework, "repository_violation", check_then_redirect)
    monkeypatch.setattr(checkout, "repository_violation", check_then_redirect)

    if call == "rework":
        # The next check, before the stash, sees the redirect and stops the rework.
        with pytest.raises(ReworkRefused):
            await _rework(Cli(agent_bin="true"), tmp_path, clone, git_at)
    else:
        await detach_head(Cli(agent_bin="true"), tmp_path / "root", REPO, Path(git_at[3]))

    assert redirected
    assert sorted(p.name for p in markers.iterdir() if not p.suffix) == []


def test_a_gitlab_host_changes_the_clone_url_and_github_stays_the_default() -> None:
    assert remote_url(REPO) == github_url(REPO)
    assert remote_url(REPO, "github.com") == github_url(REPO)
    assert remote_url(REPO, "gitlab.com") == f"https://gitlab.com/{REPO}.git"
    github = git_env({"GH_TOKEN": TOKEN})
    gitlab = git_env({"GH_TOKEN": TOKEN, "GIT_HTTP_HOST": "gitlab.com"})
    assert github["GIT_CONFIG_COUNT"] == "2"
    assert github["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert github["GIT_CONFIG_VALUE_0"] == ""
    assert github["GIT_CONFIG_KEY_1"] == "credential.https://github.com.helper"
    assert "username=x-access-token" in github["GIT_CONFIG_VALUE_1"]
    assert "GITLAB_TOKEN" not in github
    assert "GITLAB_HOST" not in github
    assert "GIT_ASKPASS" not in github
    assert gitlab["GIT_CONFIG_COUNT"] == "2"
    assert gitlab["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert gitlab["GIT_CONFIG_VALUE_0"] == ""
    assert gitlab["GIT_CONFIG_KEY_1"] == "credential.https://gitlab.com.helper"
    assert "username=oauth2" in gitlab["GIT_CONFIG_VALUE_1"]
    assert gitlab["GITLAB_TOKEN"] == TOKEN
    assert gitlab["GITLAB_HOST"] == "gitlab.com"
    assert gitlab["GIT_ASKPASS"] == ""
    assert gitlab["GLAB_CHECK_UPDATE"] == "false"
    assert gitlab["GLAB_NO_PROMPT"] == "true"
    assert "GLAB_NO_PROMPT" not in github


def _branches(clone: Path) -> set[str]:
    listed = subprocess.run(
        ["git", "-C", str(clone), "for-each-ref", "--format=%(refname:strip=2)", "refs/heads/"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_CLEAN},
    )
    return set(listed.stdout.split())


def _after_a_restart(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A shared clone in which earlier runs left `spec/1-kept` and `spec/1-stale`, both pushed,
    and `spec/1-stale` since deleted on GitHub, as a restart does. Returns the root, resolved
    as the adapter holds it, the origin and the clone."""
    origin = _origin(tmp_path)
    seed = tmp_path / "seed"
    for branch in ("spec/1-kept", "spec/1-stale"):
        _git("-C", str(seed), "push", "-q", str(origin), f"HEAD:refs/heads/{branch}")
    root = (tmp_path / "root").resolve()
    clone = _shared_clone(root, origin)
    for branch in ("spec/1-kept", "spec/1-stale"):
        _git("-C", str(clone), "branch", "-q", branch, f"origin/{branch}")
    _git("-C", str(seed), "push", "-q", str(origin), "--delete", "spec/1-stale")
    (root / "runs" / "r9").mkdir(parents=True)
    return root, origin, clone


async def _spawn(
    root: Path, origin: Path, cli: Cli | None = None, *, branch: str = "spec/1-{slug}"
) -> checkout.Checkout:
    return await prepare_worktree(
        cli or Cli(agent_bin="true"),
        root,
        root / "runs" / "r9",
        _spec(branch),
        _local_origin_env(origin),
        drop_git_pointer=False,
    )


@pytest.mark.parametrize("template", ["spec/1-{slug}", "spec/1-stale"])
async def test_a_spawn_deletes_a_branch_origin_no_longer_has(tmp_path: Path, template: str) -> None:
    """Agents create their branches in the shared clone and nothing else deletes one: after a
    restart deletes a ticket's branch on GitHub, an agent picking the same name would check out
    the old commits. With `{slug}` resolved, the template names the branch outright."""
    root, origin, clone = _after_a_restart(tmp_path)

    await _spawn(root, origin, branch=template)

    assert "spec/1-stale" not in _branches(clone)


async def test_a_spawn_keeps_a_branch_origin_still_has(tmp_path: Path) -> None:
    root, origin, clone = _after_a_restart(tmp_path)

    await _spawn(root, origin)

    assert "spec/1-kept" in _branches(clone)


@pytest.mark.parametrize(
    ("template", "outside"), [("spec/1-{slug}", "spec/2-other"), ("spec/1-stale", "spec/1-other")]
)
async def test_a_spawn_keeps_a_stale_branch_outside_its_own_template(
    tmp_path: Path, template: str, outside: str
) -> None:
    """Another ticket's branch, never pushed, can be a live run's that it has stepped off for
    the moment, say to look at `origin/main`. Only a branch this run's agent could choose is
    one it could collide with."""
    root, origin, clone = _after_a_restart(tmp_path)
    _git("-C", str(clone), "branch", "-q", outside, "origin/main")

    await _spawn(root, origin, branch=template)

    assert outside in _branches(clone)


def _hold(clone: Path, worktree: Path, how: str) -> None:
    """Check `spec/1-held`, a branch origin never had, out in another worktree of the clone,
    and leave it held `how`."""
    _git(
        "-C", str(clone), "worktree", "add", "-q", "-b", "spec/1-held", str(worktree), "origin/main"
    )
    if how == "pointer dropped":
        (worktree / ".git").unlink()
        return
    if how == "checked out":
        return
    for name in ("b.txt", "c.txt"):
        (worktree / name).write_text(f"{name}\n")
        _git("-C", str(worktree), "add", name)
        _git("-C", str(worktree), "commit", "-qm", name)
    if how == "mid-bisect":
        _git("-C", str(worktree), "bisect", "start", "HEAD", "HEAD~2")
        return
    # Stops after its one pick, when `false` fails, with HEAD detached.
    subprocess.run(
        ["git", "-C", str(worktree), "rebase", "-q", "--exec", "false", "HEAD~1"],
        capture_output=True,
        env={**os.environ, **_CLEAN},
    )


@pytest.mark.parametrize("how", ["checked out", "pointer dropped", "mid-rebase", "mid-bisect"])
async def test_a_spawn_keeps_a_branch_another_worktree_holds(tmp_path: Path, how: str) -> None:
    """An interrupted run keeps its branch checked out for the session it resumes, codex's
    worktree without its `.git` pointer. A live run can be mid-rebase or mid-bisect, which
    detach HEAD and move the branch when they end."""
    root, origin, clone = _after_a_restart(tmp_path)
    _hold(clone, root / "runs" / "r1" / "worktree", how)

    await _spawn(root, origin)

    assert "spec/1-held" in _branches(clone)


async def test_a_branch_that_cannot_be_deleted_fails_neither_the_spawn_nor_the_rest(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Another run's git can hold a ref's lock. Deleting stale branches is hygiene the run
    does not need, so the run starts, and the other stale branches still go."""
    root, origin, clone = _after_a_restart(tmp_path)
    _git("-C", str(clone), "branch", "-q", "spec/1-locked", "origin/main")
    (clone / ".git" / "refs" / "heads" / "spec" / "1-locked.lock").touch()

    with caplog.at_level(logging.WARNING):
        made = await _spawn(root, origin)

    assert (made.worktree / "a.txt").read_text() == "hi\n"
    assert _branches(clone) & {"spec/1-locked", "spec/1-stale"} == {"spec/1-locked"}
    assert "refs/heads/spec/1-locked not deleted" in caplog.text


class _BranchListingFails(Cli):
    def __init__(self) -> None:
        super().__init__(agent_bin="true")

    async def git(
        self, *args: str, env: Mapping[str, str] | None = None, inherit_host_env: bool = True
    ) -> CommandResult:
        if "for-each-ref" in args:
            return CommandResult(("git", *args), 128, "", "fatal: bad object refs/heads/x")
        return await super().git(*args, env=env, inherit_host_env=inherit_host_env)


async def test_a_branch_listing_that_fails_does_not_fail_the_spawn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root, origin, clone = _after_a_restart(tmp_path)

    with caplog.at_level(logging.WARNING):
        made = await _spawn(root, origin, _BranchListingFails())

    assert (made.worktree / "a.txt").read_text() == "hi\n"
    assert "spec/1-stale" in _branches(clone)
    assert "stale branches not deleted" in caplog.text


async def test_a_config_commit_the_clone_lacks_is_fetched_by_sha(tmp_path: Path) -> None:
    """The pin can name a commit no branch the clone fetches holds. It arrives as objects only:
    a ref or FETCH_HEAD in the shared clone would clash between concurrent worktrees."""
    origin = _origin(tmp_path)
    seed = tmp_path / "seed"
    (seed / "pinned.txt").write_text("the config\n")
    _git("-C", str(seed), "add", "pinned.txt")
    _git("-C", str(seed), "commit", "-qm", "config")
    sha = _head(seed)
    _git("-C", str(seed), "push", "-q", str(origin), "HEAD:refs/pinned/config")
    root = (tmp_path / "root").resolve()
    (root / "runs" / "r1").mkdir(parents=True)
    spec = _spec()
    spec = spec.model_copy(
        update={"workspace": spec.workspace.model_copy(update={"config_sha": sha})}
    )

    await prepare_worktree(
        Cli(agent_bin="true"),
        root,
        root / "runs" / "r1",
        spec,
        _local_origin_env(origin),
        drop_git_pointer=False,
    )

    clone = root / "repos" / REPO

    def clone_git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(clone), *args],
            capture_output=True,
            text=True,
            env={**os.environ, **_CLEAN},
        )

    assert clone_git("cat-file", "-e", f"{sha}^{{commit}}").returncode == 0
    assert clone_git("for-each-ref", "--points-at", sha).stdout == ""
    assert not (clone / ".git" / "FETCH_HEAD").exists()

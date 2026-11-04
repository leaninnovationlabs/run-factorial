"""Git credentials and workspace environment for a CLI run, and the classification of a failed
remote probe. Each adapter keeps its own child environment: the config variable and the API key
differ by vendor."""

from __future__ import annotations

import os
import re
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Final

from runtime.adapters._cli.probes import PROBEABLE_EXPECTATIONS
from runtime.adapters._cli.process import CommandResult
from runtime.contract import (
    PAGE_MAX_BYTES,
    PATTERN_MAX_MATCHES,
    MissingAttachment,
    RunAttachment,
    RunSpec,
    SpawnError,
    SpawnReason,
    Workspace,
)

__all__ = [
    "CHILD_ENV_ALLOWED_PREFIX",
    "CHILD_ENV_ALLOWLIST",
    "ENV_CONFIG_SHA",
    "ENV_GH_TOKEN",
    "ENV_GIT_DIR",
    "ENV_GIT_WORK_TREE",
    "GITHUB_KEY",
    "GLAB_QUIET_ENV",
    "MODEL_SIZE_ALIASES",
    "PAGE_RULES",
    "agent_git_identity",
    "attachment_lines",
    "blind_slug_from_base",
    "build_hidden_globs",
    "classify_probe_failure",
    "config_env",
    "config_gone",
    "config_lines",
    "files_lines",
    "git_env",
    "host_session_environment",
    "pinned_config_sha",
    "refuse_attachments",
    "refuse_blind",
    "refuse_model_alias",
    "refuse_unverifiable_expectations",
    "view_git_env",
    "worktree_env",
]

#: What the orchestrator mints per run (design.md §7.1.3) and what `gh`/`git` authenticate with.
ENV_GH_TOKEN: Final = "GH_TOKEN"

#: The only `workspace.base` shape a blind run can have (see `blind_slug_from_base` for why there
#: is nothing honest to fall back to).
_BLIND_BASE: Final = re.compile(r"spec/(?P<slug>[A-Za-z0-9][A-Za-z0-9._/-]*)")

#: A secret key naming a GitHub credential: what a `credential="none"` run's agent environment
#: must not carry (each adapter's `_credential_free_environment`).
GITHUB_KEY: Final = re.compile(r"^GH_|GITHUB", re.IGNORECASE)

ENV_GIT_DIR: Final = "GIT_DIR"

ENV_CONFIG_SHA: Final = "FACTORIAL_CONFIG_SHA"

ENV_GIT_WORK_TREE: Final = "GIT_WORK_TREE"

#: What a host session keeps of the worker's environment: exact names, as a credential-less run's
#: allowlist is, since a denylist of the operator's credentials (an ssh-agent, an askpass, cloud
#: and npm keys, the other CLI's) is never complete. The adapter sets the rest a session needs.
_SESSION_KEPT: Final = frozenset(
    {
        "PATH",
        "LANG",
        "TERM",
        "COLORTERM",
        "TMPDIR",
        "SHELL",
        "USER",
        "LOGNAME",
        "TZ",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "GIT_SSL_CAINFO",
    }
)
_SESSION_KEPT_PREFIXES: Final = ("LC_",)

#: Toolchain and cache roots a session's private HOME would hide, by their defaults under the
#: worker's HOME on macOS and Linux. Each holds toolchains, packages or build output, never a
#: credential. CARGO_HOME is not here: it holds `credentials.toml`, cargo's registry tokens.
_SESSION_TOOL_ROOTS: Final[Mapping[str, str]] = {
    "RUSTUP_HOME": ".rustup",
    "PYENV_ROOT": ".pyenv",
    "UV_PYTHON_INSTALL_DIR": ".local/share/uv/python",
    "UV_CACHE_DIR": ".cache/uv",
    "npm_config_cache": ".npm",
    "GOMODCACHE": "go/pkg/mod",
}

#: The host variables a `credential="none"` child keeps — an allowlist, never a denylist: a
#: denylist leaks the next credential-shaped variable anyone exports, while an unlisted variable
#: here costs at worst a tool complaining. Each entry is read by something in the child:
#: - PATH — the CLI must find `git`, `gh`, `node` and its own helpers.
#: - HOME — `git`, `gh` and the CLI hard-fail or misbehave without one; a session replaces the
#:   value with its own.
#: - TMPDIR — temp-file creation; on macOS the default per-user dir only resolves through it.
#: - LANG, LC_* (prefix) — locale, so subprocess text decoding matches the host's.
#: - TERM — some CLIs probe it at startup.
CHILD_ENV_ALLOWLIST: Final = frozenset({"PATH", "HOME", "TMPDIR", "LANG", "TERM"})
CHILD_ENV_ALLOWED_PREFIX: Final = "LC_"

#: Size aliases resolve to names the API rejects and the agent dies on its first turn. The
#: manifest already refuses them; refusing here too means a hand-built `RunSpec` in a local test
#: cannot reintroduce the failure.
MODEL_SIZE_ALIASES: Final = frozenset({"small", "medium", "large", "extra-large", "xl"})


def worktree_env(worktree: Path, git_dir: Path) -> dict[str, str]:
    """How the agent's git finds a worktree that carries no `.git` pointer (`checkout.py`).
    Agent environment only: the host's git addresses the clone with `-C` and must not
    inherit these."""
    return {ENV_GIT_DIR: str(git_dir), ENV_GIT_WORK_TREE: str(worktree)}

def pinned_config_sha(workspace: Workspace) -> str | None:
    """The config commit a run can read, or None. A blind clone must reach nothing beyond
    `spec/<slug>`, so the pin is never fetched into one."""
    return workspace.config_sha if workspace.isolation == "shared" else None


def config_gone(sha: str | None) -> str:
    """A pinned config commit the remote does not hold: absent now, absent on every retry."""
    return (
        f"The process config this ticket was pinned to (commit {sha}) is no longer in the "
        "repository."
    )

def agent_git_identity(bot_login: str) -> dict[str, str]:
    """Who an agent's commits name, by env: a session's HOME holds no `.gitconfig`, and git on a
    host with no mail domain refuses to commit without one."""
    name = bot_login or "factorial-agent"
    email = f"{name}@users.noreply.github.com"
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }

def host_session_environment(carried: Collection[str] = ()) -> dict[str, str]:
    """What a host session takes of the worker's environment: `_SESSION_KEPT`, the locale, the
    names in `carried` that its CLI reads, and each `_SESSION_TOOL_ROOTS` root, as the worker
    sets it or else at its default under the worker's HOME when that directory exists."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _SESSION_KEPT or key in carried or key.startswith(_SESSION_KEPT_PREFIXES)
    }
    home = os.environ.get("HOME")
    for key, default in _SESSION_TOOL_ROOTS.items():
        if value := os.environ.get(key):
            env[key] = value
        elif home and (root := Path(home, default)).is_dir():
            env[key] = str(root)
    return env

def config_env(sha: str | None) -> dict[str, str]:
    return {ENV_CONFIG_SHA: sha} if sha else {}

def config_lines(workspace: Workspace) -> list[str]:
    """The `# Workspace` line that points the agent at its pinned config."""
    sha = pinned_config_sha(workspace)
    if sha is None:
        return []
    return [
        f"- config: `.factorial/` as this run's process was pinned, at commit {sha}. Read it "
        f"with `git show {sha}:.factorial/<path>` (or `${ENV_CONFIG_SHA}`). The copy in your "
        "checkout may be out of date"
    ]


#: What a shown file may hold, by its path's lower-cased extension: the API renders exactly these
#: (orchestrator/api/page_formats.py's `RENDERERS`, held to the same keys by a test there) and
#: serves any other file as plain text.
PAGE_RULES: Final[Mapping[str, str]] = {
    ".html": (
        f"One self-contained HTML file of at most {PAGE_MAX_BYTES} bytes. It is served with "
        "scripts and network turned off: no JavaScript, images and fonts only as `data:` URIs, "
        'diagrams and charts as inline SVG, and `target="_blank"` on every link.'
    ),
    ".md": (
        f"One Markdown file of at most {PAGE_MAX_BYTES} bytes. It is rendered as CommonMark with "
        "tables and strikethrough. Raw HTML is shown as text. Images load only as `data:` URIs. "
        "Nothing runs, so a Mermaid block shows as code: draw diagrams as SVG images or as text "
        "in a fenced block. Links open in a new tab."
    ),
}
_TEXT_RULE: Final = (
    f"One UTF-8 text file of at most {PAGE_MAX_BYTES} bytes. It is shown as plain text, exactly "
    "as written."
)


def files_lines(spec: RunSpec) -> list[str]:
    """The `# Files` section (tech-stack decision 71): the files the dashboard shows from the
    branch the run pushes, and the rules each is served under, once per type. Adapter translation
    of `RunSpec.shown_files`, as "branch to push" is. Nothing when the step shows no files. What
    to write in a file is the agent's instructions', never this section's. A path with `*` is a
    pattern, explained once; one whose extension is not fixed gets every rule."""
    if not spec.shown_files:
        return []
    intro = (
        "The dashboard shows these files beside this run, as the last commit on the branch you "
        "push has them. A file you do not commit there shows as missing."
    )
    if any("{slug}" in shown.path for shown in spec.shown_files):
        intro += " Replace `{slug}` in a path with your own branch's slug."
    if any("*" in shown.path for shown in spec.shown_files):
        intro += (
            " A path with `*` is a pattern: `*` stands for any characters inside one folder, "
            "`**` for any number of folders, and each file you commit that matches it shows on "
            f"its own, up to {PATTERN_MAX_MATCHES} for one pattern."
        )
    rules: dict[str, str] = {}
    for shown in spec.shown_files:
        suffix = PurePosixPath(shown.path).suffix.lower()
        if "*" in shown.path and (not suffix or "*" in suffix):
            for kind, rule in PAGE_RULES.items():
                rules.setdefault(f"`{kind}`", rule)
            rules.setdefault("any other file", _TEXT_RULE)
        elif suffix in PAGE_RULES:
            rules.setdefault(f"`{suffix}`", PAGE_RULES[suffix])
        else:
            rules.setdefault("any other file", _TEXT_RULE)
    return [
        "",
        "# Files",
        intro,
        *(
            f"- `{shown.path}` ({shown.label}, a pattern)"
            if "*" in shown.path
            else f"- `{shown.path}` ({shown.label})"
            for shown in spec.shown_files
        ),
        "",
        "How each is shown, by its extension:",
        *(f"- {kind}: {rule}" for kind, rule in rules.items()),
    ]


#: glab neither checks for updates nor prompts in a run.
GLAB_QUIET_ENV: Final = {"GLAB_CHECK_UPDATE": "false", "GLAB_NO_PROMPT": "true"}


def git_env(secrets: Mapping[str, str]) -> dict[str, str]:
    """Authenticate `git` and `gh` from the minted token, without writing it anywhere.

    The credential helper is passed through `GIT_CONFIG_*` environment variables rather than
    `git config`, so the token never lands in a `.git/config` on disk — and it reads the token
    from the environment at call time, so it never appears in a command line either. An empty
    `credential.helper` first empties the list git built from the files: macOS's system git
    and Homebrew's name `osxkeychain` there, which git would ask first, with the operator's
    login, and tell to store each token.
    """
    token = secrets.get(ENV_GH_TOKEN)
    if not token:
        return {}
    host = secrets.get("GIT_HTTP_HOST") or "github.com"
    if host == "github.com":
        return {
            ENV_GH_TOKEN: token,
            "GITHUB_TOKEN": token,  # `gh` accepts either; some subcommands only read this one
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": "credential.https://github.com.helper",
            "GIT_CONFIG_VALUE_1": (
                '!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'
            ),
            "GIT_TERMINAL_PROMPT": "0",  # fail instead of blocking forever on a missing credential
        }
    # GitLab (and any other non-GitHub host): oauth2, scoped helper, no osxkeychain prompt.
    helper = '!f() { echo username=oauth2; echo "password=$GH_TOKEN"; }; f'
    return {
        ENV_GH_TOKEN: token,
        "GITHUB_TOKEN": token,
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": f"credential.https://{host}.helper",
        "GIT_CONFIG_VALUE_1": helper,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GITLAB_TOKEN": token,
        "GITLAB_HOST": host,
        "GIT_HTTP_HOST": host,
        **GLAB_QUIET_ENV,
    }

#: What every git an open session runs (decision 57) takes as its own config: hooks and fsmonitor
#: are programs a checkout's config names, the agent wrote that config, and a view runs beside the
#: model key.
_VIEW_GIT_CONFIG: Final = (("core.fsmonitor", "false"), ("core.hooksPath", "/dev/null"))


def view_git_env(env: Mapping[str, str]) -> dict[str, str]:
    """`env` for an open session's launch: its `GIT_CONFIG_*` entries kept, and hooks and
    fsmonitor turned off after them. git applies those entries in order, the last one winning,
    and above every config file, so no checkout's config turns either back on."""
    merged = dict(env)
    count = int(merged.get("GIT_CONFIG_COUNT") or "0")
    for index, (key, value) in enumerate(_VIEW_GIT_CONFIG, start=count):
        merged[f"GIT_CONFIG_KEY_{index}"] = key
        merged[f"GIT_CONFIG_VALUE_{index}"] = value
    merged["GIT_CONFIG_COUNT"] = str(count + len(_VIEW_GIT_CONFIG))
    return merged



def blind_slug_from_base(base: str) -> str:
    """`spec/42-add-a-thing` -> `42-add-a-thing`, for a blind run only.

    A blind clone is `--single-branch --branch spec/<slug>`, so the slug is the part of the base
    after the prefix, and it is also what `build_hidden_globs` tells `assert_isolated` to look
    for. A base of another shape is refused
    rather than guessed at: a garbage slug produces globs that match nothing, and a blindness
    check looking for the wrong thing passes for the wrong reason.
    """
    match = _BLIND_BASE.fullmatch(base)
    if match is None:
        raise SpawnError(
            f"a blind run's workspace.base must be `spec/<slug>`, got {base!r}. A blind run "
            "clones only that branch and derives everything it hides from the slug, so it has "
            "no safe fallback",
            # WORKSPACE: `workspace.base` is wrong as written and stays wrong on every retry.
            reason=SpawnReason.WORKSPACE,
        )
    return match.group("slug")

#: git stderr shapes a failed `ls-remote` answers with, observed against real git + GitHub.
#: Sniffing vendor output is an adapter's job — only the orchestrator is forbidden to read adapter
#: prose (`SpawnReason`) — and these are narrow enough that a sha or branch name cannot trip them.
_GIT_AUTH_REFUSED_SHAPES: Final = (
    "could not read username",  # GIT_TERMINAL_PROMPT=0 + private repo + nothing to present
    "authentication failed",
    "returned error: 401",
    "returned error: 403",
)

_GIT_REPO_NOT_FOUND_SHAPE: Final = "repository not found"

def classify_probe_failure(repo: str, probe: CommandResult, *, has_token: bool) -> SpawnError:
    """Why `git ls-remote` could not read the repo, classified.

    A permanent-shaped stderr escalates (CREDENTIAL/WORKSPACE, evidence quoted) so a missing repo
    or a dead credential never spins at attempt 1 forever; everything unrecognized — DNS, connect
    timeouts, a GitHub 5xx — stays INFRASTRUCTURE. `Repository not found` is GitHub's answer both
    for a repo that does not exist and for a private one the caller cannot see, so which fault is
    actionable depends on whether a token was presented at all.
    """
    stderr = probe.stderr.strip()
    evidence = stderr[-300:] or f"rc={probe.returncode}"
    lowered = stderr.casefold()
    if any(shape in lowered for shape in _GIT_AUTH_REFUSED_SHAPES):
        remedy = (
            "fix or re-mint GH_TOKEN"
            if has_token
            else f"the spec carries no {ENV_GH_TOKEN}. Store one in Factorial and declare it "
            "in the manifest's `secrets:` block"
        )
        return SpawnError(
            f"cannot read {repo} for the blind clone: git was refused authentication "
            f"({evidence}); {remedy}",
            # CREDENTIAL: only a human storing or fixing a secret changes the next attempt.
            reason=SpawnReason.CREDENTIAL,
        )
    if _GIT_REPO_NOT_FOUND_SHAPE in lowered:
        if not has_token:
            return SpawnError(
                f"cannot read {repo} for the blind clone: GitHub answers `Repository not "
                f"found` for a private repo it cannot see, and the spec carries no "
                f"{ENV_GH_TOKEN} ({evidence}). Store one, or fix `workspace.repo` if the name is "
                "wrong",
                reason=SpawnReason.CREDENTIAL,
            )
        return SpawnError(
            f"{repo} is not visible to the presented credential ({evidence}). Fix "
            "`workspace.repo`, or widen the token's scope. A retry gets the same answer",
            reason=SpawnReason.WORKSPACE,
        )
    return SpawnError(
        f"`git ls-remote` against {repo} exited {probe.returncode}: {evidence}",
        reason=SpawnReason.INFRASTRUCTURE,
    )

def build_hidden_globs(hidden: tuple[str, ...], slug: str) -> tuple[str, ...]:
    """`Workspace.hidden` -> the globs `runtime.workspace.assert_isolated` checks.

    A declared path entry yields both the bare glob and its `/**` form — `acceptance/<slug>/**`
    alone leaves a hole, because fnmatch needs something after the slash, so a plain file at
    exactly `acceptance/<slug>` matches nothing and sails through check 2. `refs/` entries are ref
    globs, passed through untouched (contract.py's glob convention). An empty tuple falls back to
    issue #19's own shape, because a blind gate with nothing to look for is tags-only, i.e. fails
    open.
    """
    if not hidden:
        return (f"acceptance/{slug}", f"acceptance/{slug}/**", f"refs/*/tests/{slug}")
    out: list[str] = []
    for entry in hidden:
        rendered = entry.replace("{slug}", slug)
        out.append(rendered)
        if not rendered.startswith("refs/") and not rendered.endswith("*"):
            out.append(f"{rendered.rstrip('/')}/**")
    return tuple(dict.fromkeys(out))


def refuse_attachments(adapter: str, attachments: Sequence[RunAttachment]) -> None:
    """Honour or refuse, never drop — for an adapter whose `supports_attachments` is False."""
    if attachments:
        raise SpawnError(
            f"This runtime can't pass on images from the comments ({len(attachments)} "
            f"attached).\n\n{adapter} does not support attachments.",
            reason=SpawnReason.CONFIG,
        )


def refuse_blind(adapter: str, spec: RunSpec) -> None:
    """Refuse a blind workspace: every run is a session, and no session runs in one."""
    if spec.workspace.isolation == "blind":
        # The Complex lane is dormant (design.md §7.6).
        raise SpawnError(
            f"This runtime can't run a blind workspace: no runtime runs one today.\n\n{adapter} "
            "refused workspace isolation 'blind'.",
            reason=SpawnReason.CONFIG,
        )


def refuse_unverifiable_expectations(adapter: str, spec: RunSpec) -> None:
    """Refuse a declared expectation this adapter has no way to check.

    Honour or refuse, never ignore: an adapter that dropped an expectation would report a run as
    delivered on evidence it never looked for. BRANCH, PR and COMMENT are honoured (see
    `probes.discover_artifacts`); COMMIT and FILE are refused, naming the kind.
    """
    unverifiable = sorted(
        {
            item.kind.value
            for item in spec.expected_artifacts
            if item.kind not in PROBEABLE_EXPECTATIONS
        }
    )
    if unverifiable:
        raise SpawnError(
            f"expected artifact kind(s) {', '.join(unverifiable)} declared, which "
            f"{adapter} can't verify. It checks only "
            f"{', '.join(sorted(kind.value for kind in PROBEABLE_EXPECTATIONS))}, through `git "
            "ls-remote` and `gh`",
            # CONFIG: declared config this adapter cannot honour.
            reason=SpawnReason.CONFIG,
        )


def refuse_model_alias(model: str, example: str) -> None:
    if model.strip().lower() in MODEL_SIZE_ALIASES:
        raise SpawnError(
            f"The model {model!r} is a size alias, which the API rejects. Set model in the "
            f"agent's agent.yaml to a full model id, such as {example!r}.",
            # CONFIG: the manifest's `model:` is wrong; only an edit fixes it.
            reason=SpawnReason.CONFIG,
        )


def attachment_lines(
    attachments: Sequence[RunAttachment],
    missing: Sequence[MissingAttachment],
    paths: Mapping[str, str],
) -> list[str]:
    """The `# Attachments` section: where each delivered image was saved, and which embedded
    images the run does not get and why. Nothing when there is neither."""
    if not attachments and not missing:
        return []
    lines = ["", "# Attachments"]
    if attachments:
        lines.append(
            "Images embedded in the text above. Each one is attached to this message and saved "
            "at the path shown:"
        )
        lines += [
            f"- {item.name} ({item.source}, {item.origin}): {paths[item.name]}"
            for item in attachments
        ]
    if missing:
        if attachments:
            lines.append("")
        lines.append("Embedded in the text above but not delivered:")
        lines += [f"- {item.origin} ({item.source}): {item.reason}" for item in missing]
    return lines

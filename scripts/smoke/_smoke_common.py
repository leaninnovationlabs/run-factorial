"""What the host smokes share: the scratch layout and the one test seam.

Not a smoke itself — each smoke's module docstring stays the load-bearing account of what it
proves. Everything here is harness: nothing on GitHub is touched, and the only production code
path bent is `ScratchRedirect`'s `REDIRECT`, config entries on its `run`.
"""
import os
import re
import subprocess
import tempfile
from pathlib import Path

from pydantic import SecretStr

from runtime.adapters._cli.process import Cli, CommandResult

# Resolved, as the adapter resolves its root: on macOS `/tmp` is a link to `/private/tmp`.
ROOT = Path(
    os.environ.get("FACTORIAL_SMOKE_ROOT") or tempfile.mkdtemp(prefix="smoke-")
).resolve()
#: The faked remote. A real run pushes over https, which the sandbox allows as network; a push to
#: this file:// path is a filesystem write outside the agent's worktree and clone `.git`, so the
#: sandbox refuses it, and the smokes read the agent's commit from the clone or push for it.
SCRATCH = ROOT / "root" / "scratch-origin"
REPO_NAME = "SMOKE/scratch"
#: `api_key` (default) bills API credits from the key in `.env`; `subscription` bills the operator's
#: own subscription, seeded from their `codex login`. The two serve DIFFERENT models, so the
#: default model follows the mode -- a key's `gpt-5.3-codex` answers 400 on a subscription.
AUTH = os.environ.get("FACTORIAL_SMOKE_CODEX_AUTH", "api_key")
_DEFAULT_MODEL = "gpt-5.6-terra" if AUTH == "subscription" else "gpt-5.3-codex"
MODEL = os.environ.get("FACTORIAL_SMOKE_CODEX_MODEL", _DEFAULT_MODEL)


def sh(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def build_scratch():
    """A fresh file:// remote holding `main` and its README."""
    if SCRATCH.exists():
        subprocess.run(["rm", "-rf", str(SCRATCH)], check=True)
    SCRATCH.mkdir(parents=True)
    sh("git", "init", "-q", "-b", "main", str(SCRATCH))

    def g(*a: str) -> None:
        sh("git", "-C", str(SCRATCH), *a)

    (SCRATCH / "README.md").write_text("scratch\n")
    g("add", "README.md")
    g("-c", "user.name=smoke", "-c", "user.email=smoke@test", "commit", "-q", "-m", "init")


#: GitHub's URL for the scratch repo, sent to the scratch path over the file transport, which
#: host git refuses unless its caller asks (runtime/hostgit.py). The clone keeps GitHub's URL
#: as its `origin`, as a real one does, so every git that reaches it needs the same redirect.
REDIRECT = (
    ("url.file://" + str(SCRATCH) + ".insteadOf", f"https://github.com/{REPO_NAME}.git"),
    ("protocol.file.allow", "always"),
)
REDIRECT_ARGS = tuple(arg for key, value in REDIRECT for arg in ("-c", f"{key}={value}"))


def via_scratch(env):
    """`env` with REDIRECT appended after whatever config entries it already carries."""
    merged = dict(env or {})
    count = int(merged.get("GIT_CONFIG_COUNT", "0") or "0")
    for i, (key, value) in enumerate(REDIRECT, start=count):
        merged[f"GIT_CONFIG_KEY_{i}"] = key
        merged[f"GIT_CONFIG_VALUE_{i}"] = value
    merged["GIT_CONFIG_COUNT"] = str(count + len(REDIRECT))
    return merged


class ScratchRedirect(Cli):
    """The one seam: GitHub's URL for the scratch repo leads to the scratch path, for the host's
    git. Everything else real."""

    async def run(
        self, program, *args, env=None, cwd=None, inherit_host_env=True
    ) -> CommandResult:
        return await super().run(
            program,
            *args,
            env=via_scratch(env) if program == self.git_bin else env,
            cwd=cwd,
            inherit_host_env=inherit_host_env,
        )


def read_env_key(name: str) -> str:
    """A value from the repo-root `.env`, read without echoing."""
    m = re.search(rf"^{name}=(.*)$", Path(".env").read_text(), re.M)
    assert m, f"no {name} in .env"
    return m.group(1).strip()


def auth_secrets() -> dict[str, SecretStr]:
    """What a run authenticates with in the chosen mode: the key, or nothing at all."""
    if AUTH == "subscription":
        return {}
    return {"OPENAI_API_KEY": SecretStr(read_env_key("OPENAI_API_KEY"))}

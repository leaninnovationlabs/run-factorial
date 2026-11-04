"""The run area under `local_root`: its layout, the containment guard, and the file helpers
every CLI adapter writes with."""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from runtime.contract import RunAttachment, SpawnError, SpawnReason, UnknownRunError

log = logging.getLogger(__name__)

__all__ = [
    "ATTACHMENTS_DIRNAME",
    "CLONES_DIRNAME",
    "CONFIG_DIRNAME",
    "HOME_DIRNAME",
    "RUNS_DIRNAME",
    "WORKTREE_DIRNAME",
    "contained",
    "delete_credentials",
    "existing_run_dir",
    "is_run_id",
    "path_slug",
    "remove_attachments",
    "utc_now",
    "workspace_dir",
    "write_attachments",
    "write_new",
    "write_private",
]

#: Layout under `<local_root>/`: one clone per repo, one directory per run.
CLONES_DIRNAME: Final = "repos"

RUNS_DIRNAME: Final = "runs"

WORKTREE_DIRNAME: Final = "worktree"

#: A run's own config home under `<local_root>/runs/<run_id>/`: the vendor CLI is pointed at it,
#: so the operator's own settings are never consulted and nothing factory-shaped is written into
#: the worktree the agent can commit.
CONFIG_DIRNAME: Final = "config"

#: A host session's HOME, inside its config home: codex reads `$HOME/.agents/skills` whatever
#: `CODEX_HOME` says, and claude writes into its HOME, so the worker's would hand the operator's
#: skills to every agent and take each one's writes.
HOME_DIRNAME: Final = "home"

#: A run's images (`RunSpec.attachments`) under `<local_root>/runs/<run_id>/`: beside the
#: worktree, never in it, so an agent's `git add -A` cannot commit them.
ATTACHMENTS_DIRNAME: Final = "attachments"


_RUN_ID: Final = re.compile(r"(?:[^\W_]|-)+")


def utc_now() -> datetime:
    return datetime.now(UTC)

def write_private(path: Path, text: str) -> None:
    """Write a file only this user can read, without a window where it is world-readable."""
    write_new(path, text.encode("utf-8"), 0o600)


def write_new(path: Path, data: bytes, mode: int) -> None:
    """Replace `path` whole: a reader meanwhile sees the old file or the new one, never a missing
    or half-written one. The run area is the agent's to write, so a symlink left there is
    replaced, not followed."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def path_slug(value: str) -> str:
    # Lowered first: lowering can yield a non-alphanumeric (İ → i + U+0307) is_run_id refuses.
    return "".join(char if char.isalnum() else "-" for char in value.lower()).strip("-")[:40]

def contained(root: Path, candidate: Path, *, create: bool = True) -> Path:
    """Resolve `candidate` and refuse anything outside `root`.

    The one hard guarantee a CLI adapter offers. A ticket key or agent name that walked out of
    the root with `../` would put an agent's working directory somewhere nobody chose.
    """
    resolved = candidate.expanduser().resolve()
    if resolved != root and not resolved.is_relative_to(root):
        # WORKSPACE: the offending value is `workspace.repo` (or a ticket/agent name) as written
        # in config — it escapes the root on every retry, identically.
        raise SpawnError(
            f"{resolved} escapes local_root {root}; refusing to run an agent there",
            reason=SpawnReason.WORKSPACE,
        )
    if create:
        resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def existing_run_dir(root: Path, run_id: str) -> Path:
    run_dir = contained(root, root / RUNS_DIRNAME / run_id, create=False)
    if not run_dir.is_dir():
        raise UnknownRunError(f"no local run {run_id} under {root / RUNS_DIRNAME}")
    return run_dir


def is_run_id(name: str) -> bool:
    """Whether `name` could be a run's id, as `path_slug`s make them: one directory name, and
    one `resolve()` can take, which a NUL is not."""
    return _RUN_ID.fullmatch(name) is not None


def workspace_dir(root: Path, run_dir: Path, workspace_of: str | None) -> Path:
    """The directory holding a run's worktree and config home: its own, or, for a rework that
    continues another run's session, that of the run whose workspace it is. The state naming it
    is the agent's to write on a host, so a name that is not one run directory's is refused."""
    if workspace_of is None:
        return run_dir
    if not is_run_id(workspace_of):
        raise UnknownRunError(f"run {run_dir.name} names no run's workspace: {workspace_of!r}")
    return root / RUNS_DIRNAME / workspace_of


def write_attachments(run_dir: Path, attachments: Sequence[RunAttachment]) -> dict[str, str]:
    """Each image as a 0600 file under the run's attachments folder; name -> absolute path."""
    if not attachments:
        return {}
    folder = contained(run_dir, run_dir / ATTACHMENTS_DIRNAME)
    folder.chmod(0o700)
    paths: dict[str, str] = {}
    for item in attachments:
        path = contained(folder, folder / item.filename, create=False)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(item.content)
        paths[item.name] = str(path)
    return paths


def remove_attachments(run_dir: Path) -> None:
    shutil.rmtree(run_dir / ATTACHMENTS_DIRNAME, ignore_errors=True)


def delete_credentials(root: Path, run_dir: Path, names: Sequence[str]) -> int:
    """Delete the named credential files from the run's config home (decision 54), and return
    how many there were. The run area is writable by the agent, so a config home that resolves
    outside `root` is left alone, and a symlink is unlinked, never followed. A temporary file
    `write_new` left, killed before its rename, goes too: it holds the same secrets."""
    deleted = 0
    for name in names:
        try:
            config = contained(root, run_dir / CONFIG_DIRNAME, create=False)
            for partial in config.glob(f".{name}.*"):
                partial.unlink(missing_ok=True)
            (config / name).unlink()
            deleted += 1
        except FileNotFoundError:
            pass
        except (OSError, SpawnError) as exc:
            log.error("deleting %s of run %s failed: %s", name, run_dir.name, exc)
    return deleted

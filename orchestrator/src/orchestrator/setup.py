"""`factorial-setup`: write the settings file and the seccomp profile a first run needs.

    docker run --rm --platform linux/amd64 -v "${PWD}:/out" --entrypoint factorial-setup <image>

The same command works in bash, zsh and PowerShell, so no step of the docs depends on the host's
shell. The image makes the E2B deployment name itself (docker/rootfs/etc/factorial/
e2b-deployment.sh), so none is written here. The database URL's role, password and database are
the ones the compose `database` service initialises a new volume with (postgres-standalone).
"""

from __future__ import annotations

import base64
import errno
import os
import secrets
import shutil
import sys
from pathlib import Path
from typing import Final

__all__ = ["main", "write_setup"]

_SECCOMP: Final = Path("/etc/factorial/seccomp.json")
_OUT: Final = Path("/out")
_ENV_FILE: Final = "factorial.env"
_SECCOMP_FILE: Final = "seccomp.json"

_TEMPLATE: Final = """\
# The bearer token for the API, and the key the dashboard's session cookies are signed with.
FACTORIAL_API_TOKEN={api_token}

# Encrypts every secret Factorial stores. Keep this file: with a new key the stored secrets
# cannot be read again.
FACTORIAL_MASTER_KEY={master_key}

# Lets the dashboard open a terminal into a running agent session.
FACTORIAL_TERMINAL__ENABLED=true

# The database the compose `database` service runs, published on 127.0.0.1:5432.
FACTORIAL_DATABASE_URL=postgresql+asyncpg://factorial-db-user:{database_password}@database:5432/factorial-db
"""


def write_setup(out: Path, *, seccomp: Path = _SECCOMP) -> list[Path]:
    env_path = out / _ENV_FILE
    text = _TEMPLATE.format(
        api_token=secrets.token_urlsafe(32),
        master_key=base64.b64encode(secrets.token_bytes(32)).decode(),
        # URL-safe, so the URL carries it unescaped.
        database_password=secrets.token_urlsafe(24),
    )
    # O_EXCL: a second run, or a race, can never replace keys that already protect stored secrets.
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    seccomp_path = out / _SECCOMP_FILE
    made = [env_path] + ([] if seccomp_path.exists() else [seccomp_path])
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        shutil.copyfile(seccomp, seccomp_path)
        _hand_to_owner(out, [env_path, seccomp_path])
    except BaseException:
        # Keys nobody has seen yet must not block a rerun.
        for path in made:
            path.unlink(missing_ok=True)
        raise
    return [env_path, seccomp_path]


def _hand_to_owner(out: Path, paths: list[Path]) -> None:
    """Give the files to whoever owns the mounted folder. A host mount (Docker Desktop, Windows
    above all) may refuse, and then it owns the files already."""
    owner = out.stat()
    for path in paths:
        if (owner.st_uid, owner.st_gid) == (os.getuid(), os.getgid()):
            continue
        try:
            os.chown(path, owner.st_uid, owner.st_gid)
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EINVAL):
                raise


def main() -> None:
    try:
        written = write_setup(_OUT)
    except FileExistsError:
        print(
            f"{_ENV_FILE} already exists in this folder: Factorial keeps its keys there. "
            "Move it away to make a new one.",
            file=sys.stderr,
        )
        sys.exit(1)
    for path in written:
        print(f"wrote {path.name}")
    print("Next: docker compose up -d --wait")
    print(
        "Ran Factorial on this machine before? Its data volume needs the factorial.env it was "
        "made with. Use that file, or remove the volume with docker volume rm factorial-public "
        "to start over."
    )


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()

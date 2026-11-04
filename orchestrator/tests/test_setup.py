"""`factorial-setup` (docker/rootfs/usr/local/bin/factorial-setup): the first-run files."""

from __future__ import annotations

import errno
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from dotenv import dotenv_values

from orchestrator import container, setup
from orchestrator.settings import Settings
from tests.paths import REPO_ROOT


def _seccomp(tmp_path: Path) -> Path:
    profile = tmp_path / "profile.json"
    profile.write_text("{}")
    return profile


def test_setup_writes_keys_the_preflight_accepts(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    setup.write_setup(out, seccomp=_seccomp(tmp_path))
    values = dotenv_values(out / "factorial.env")
    assert set(values) == {
        "FACTORIAL_API_TOKEN",
        "FACTORIAL_MASTER_KEY",
        "FACTORIAL_TERMINAL__ENABLED",
        "FACTORIAL_DATABASE_URL",
    }
    found = container.problems(
        lambda: Settings(_env_file=out / "factorial.env"),  # pyright: ignore[reportCallIssue]
        environ={},
    )
    assert not [p for p in found if "API_TOKEN" in p or "MASTER_KEY" in p]
    assert (out / "seccomp.json").read_text() == "{}"


def test_setup_names_the_compose_database_with_a_password_of_its_own(tmp_path: Path) -> None:
    """postgres-standalone initialises a new volume with this role and password, so one the
    URL could not carry unescaped would leave the app unable to sign in."""
    out = tmp_path / "out"
    out.mkdir()
    setup.write_setup(out, seccomp=_seccomp(tmp_path))
    url = urlsplit(dotenv_values(out / "factorial.env")["FACTORIAL_DATABASE_URL"] or "")
    assert (url.scheme, url.username, url.hostname, url.port, url.path) == (
        "postgresql+asyncpg",
        "factorial-db-user",
        "database",
        5432,
        "/factorial-db",
    )
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", url.password or "")


def test_setup_writes_the_settings_file_closed(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    env_file, _ = setup.write_setup(out, seccomp=_seccomp(tmp_path))
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_setup_refuses_to_replace_an_existing_settings_file(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "factorial.env").write_text("FACTORIAL_MASTER_KEY=keep-me\n")
    with pytest.raises(FileExistsError):
        setup.write_setup(out, seccomp=_seccomp(tmp_path))
    assert (out / "factorial.env").read_text() == "FACTORIAL_MASTER_KEY=keep-me\n"
    assert not (out / "seccomp.json").exists()


def test_the_wrapper_is_executable() -> None:
    wrapper = REPO_ROOT / "docker" / "rootfs" / "usr" / "local" / "bin" / "factorial-setup"
    assert wrapper.stat().st_mode & 0o111


def test_a_failed_setup_leaves_no_keys_behind_and_a_rerun_succeeds(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(FileNotFoundError):
        setup.write_setup(out, seccomp=tmp_path / "missing.json")
    assert list(out.iterdir()) == []
    setup.write_setup(out, seccomp=_seccomp(tmp_path))
    assert (out / "factorial.env").exists() and (out / "seccomp.json").exists()


def test_a_mount_that_refuses_chown_still_gets_both_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(os, "getuid", lambda: out.stat().st_uid + 1)

    def refuse(*_: object) -> None:
        raise PermissionError(errno.EPERM, "refused")

    monkeypatch.setattr(os, "chown", refuse)
    setup.write_setup(out, seccomp=_seccomp(tmp_path))
    assert (out / "factorial.env").exists() and (out / "seccomp.json").exists()

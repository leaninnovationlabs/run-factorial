"""The local image (tech-stack decisions 52, 73): the CLI versions its adapters are pinned to,
the terminal service in its s6 tree, and how docker-compose.yml and scripts/run-local-container.sh
run it.

A host runtime refuses any CLI but the pin (`spawn_refusal`), so an image with a different CLI
would start and run no agent.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from orchestrator import container
from orchestrator.settings import ENV_PREFIX, Settings, _is_settings_name
from runtime.adapters.claude_cli.cli import PINNED_VERSION as CLAUDE_PINNED
from runtime.adapters.codex_cli.cli import PINNED_VERSION as CODEX_PINNED
from runtime.adapters.e2b.template import GLAB_VERSION
from tests.paths import REPO_ROOT

DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE = REPO_ROOT / "docker-compose.yml"
#: The plain `docker run` that mounts the optional files compose does not.
RUN_CONTAINER = REPO_ROOT / "scripts" / "run-local-container.sh"
S6 = REPO_ROOT / "docker" / "rootfs" / "etc" / "s6-overlay"
FACTORIAL_ETC = REPO_ROOT / "docker" / "rootfs" / "etc" / "factorial"
SECCOMP = REPO_ROOT / "docker" / "seccomp.json"
CLIS = REPO_ROOT / "docker" / "clis"
USERNS = {"clone", "mount", "pivot_root", "umount2", "unshare"}


def dockerfile_arg(name: str, dockerfile: str) -> str | None:
    match = re.search(rf"^ARG {name}=(\S+)$", dockerfile, re.MULTILINE)
    return match.group(1) if match else None


def test_the_image_installs_the_cli_versions_its_adapters_are_pinned_against() -> None:
    assert dockerfile_arg("CODEX_VERSION", DOCKERFILE.read_text()) == CODEX_PINNED
    package = "@anthropic-ai/claude-code"
    assert json.loads((CLIS / "package.json").read_text())["dependencies"] == {
        package: CLAUDE_PINNED
    }
    lock = json.loads((CLIS / "package-lock.json").read_text())["packages"]
    assert lock[f"node_modules/{package}"]["version"] == CLAUDE_PINNED


def test_the_image_installs_the_glab_the_e2b_template_installs() -> None:
    assert dockerfile_arg("GLAB_VERSION", DOCKERFILE.read_text()) == GLAB_VERSION


def test_every_build_input_is_pinned_to_its_bytes() -> None:
    """A tag can be pushed again, and a package fetched by version alone can change with the
    registry. Either would enter the published image with no change to this repository."""
    dockerfile = DOCKERFILE.read_text()
    stages = {m.group(1) for m in re.finditer(r"^FROM .* AS (\S+)$", dockerfile, re.MULTILINE)}
    for match in re.finditer(r"^FROM (\S+)", dockerfile, re.MULTILINE):
        image = match.group(1)
        if image.startswith("${"):
            image = dockerfile_arg(image[2:-1], dockerfile) or image
        assert image in stages or re.search(r"@sha256:[0-9a-f]{64}$", image), image
    installs = re.findall(r"\bnpm (?:install|i|ci)\b[^\n]*", dockerfile)
    assert installs and all("npm ci" in line or "--offline" in line for line in installs), installs
    lock = json.loads((CLIS / "package-lock.json").read_text())["packages"]
    assert all(entry.get("integrity", "").startswith("sha512-") for k, entry in lock.items() if k)


def _stages(dockerfile: str) -> list[list[str]]:
    """Each stage's instructions, a continued line joined into one."""
    stages: list[list[str]] = []
    for line in re.sub(r"\\\n", " ", dockerfile).splitlines():
        line = line.strip()
        if line.startswith("FROM "):
            stages.append([])
        if stages and line and not line.startswith("#"):
            stages[-1].append(line)
    return stages


def test_no_file_from_the_checkout_keeps_the_checkout_s_mode() -> None:
    """git writes a file with the cloner's umask. Under 077 an image built from that checkout
    holds files no service user can read, and the container stops at its startup check on a
    PermissionError that names no cause."""
    for stage in _stages(DOCKERFILE.read_text()):
        normalized = any(i.startswith("RUN ") and "chmod -R u=rwX,go=rX" in i for i in stage)
        for instruction in stage:
            if instruction.startswith("COPY ") and "--from=" not in instruction:
                assert normalized or "--chmod=" in instruction, instruction


def test_the_terminal_service_is_in_the_user_bundle_and_waits_for_migrate() -> None:
    """A service missing from the bundle never starts, and nothing reports it."""
    service = S6 / "s6-rc.d" / "terminal"
    assert (S6 / "user-bundles.d" / "user" / "contents.d" / "terminal").is_file()
    assert (service / "type").read_text().strip() == "longrun"
    assert (service / "dependencies.d" / "migrate").is_file()
    run = service / "run"
    assert os.access(run, os.X_OK)
    assert "python -m orchestrator.terminal" in run.read_text()


def _app() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text())["services"]["app"]


def test_a_stop_waits_for_the_gateway_to_close_its_clients() -> None:
    """The gateway's shutdown can take 45 s (an E2B pty kill among them). s6-rc waits for it, so
    Docker's stop timeout is the deadline, and its default 10 s kills the gateway first: a root
    tmux client then stays attached in the sandbox for good."""
    grace = re.fullmatch(r"(\d+)s", _app()["stop_grace_period"])
    assert grace is not None and int(grace.group(1)) >= 50
    stop = re.search(r"--stop-timeout (\d+)", RUN_CONTAINER.read_text())
    assert stop is not None and int(stop.group(1)) >= 50


def test_compose_hands_the_app_its_configuration_as_files() -> None:
    """Every agent run inherits the app's environment, and `.env` holds vendor keys init does
    not move, so `env_file:` or a secret in `environment:` reaches every agent, and nothing says
    so. A misspelt setting there is left at its default, also silently."""
    app = _app()
    vendor = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "E2B_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"}
    secret = {*container._secret_names(Settings), *vendor}  # pyright: ignore[reportPrivateUsage]
    assert "env_file" not in app
    for key in app.get("environment", {}):
        assert key.upper() not in secret and not key.startswith("AWS_"), key
        if not key.startswith(f"{ENV_PREFIX}IMAGE_"):
            assert _is_settings_name(key.removeprefix(ENV_PREFIX)), key
    assert {entry["target"] for entry in app["secrets"]} == {"factorial.env"}


def test_the_assistant_runs_as_its_own_user_reading_none_of_the_shared_secrets() -> None:
    """design.md §7.18: the process that runs the model must not start where `/app/.env` would be
    read; init must close the mounts to it, and its copy of `.env` for it must carry the database
    URL, which on RDS no other file it can read holds, and no GitHub setting."""
    service = S6 / "s6-rc.d" / "assistant"
    assert (S6 / "user-bundles.d" / "user" / "contents.d" / "assistant").is_file()
    assert (service / "type").read_text().strip() == "longrun"
    assert (service / "dependencies.d" / "migrate").is_file()
    run = service / "run"
    assert os.access(run, os.X_OK)
    script = run.read_text()
    assert "s6-setuidgid factorial-assistant python -m orchestrator.assistant" in script
    assert "cd /run/factorial-assistant\n" in script
    # A master key that reaches it anyway stops it at start (`drop_master_key`).
    assert "export FACTORIAL_ASSISTANT__OWN_USER=true\n" in script
    init = (S6 / "scripts" / "init").read_text()
    # Docker Desktop lets any user read a bind-mounted file whatever mode it shows.
    assert "chmod 0700 /run/secrets\n" in init
    [copy] = [
        line
        for line in init.splitlines()
        if "/run/factorial-assistant/.env" in line and "grep" in line
    ]
    assert "FACTORIAL_GITHUB__" not in copy


def test_the_assistant_s_env_holds_no_master_key(tmp_path: Path) -> None:
    """design.md §7.18: without the master key every stored secret is opaque to the assistant.
    The database URL stays: on RDS no other file the assistant can read holds it."""
    [copy] = [
        line
        for line in (S6 / "scripts" / "init").read_text().splitlines()
        if "/run/factorial-assistant/.env" in line and "grep" in line
    ]
    settings, env = tmp_path / "factorial.env", tmp_path / "assistant.env"
    settings.write_text(
        "FACTORIAL_API_TOKEN='token'\n"
        "FACTORIAL_MASTER_KEY='a2V5'\n"
        "export FACTORIAL_MASTER_KEY_ID='kid-1'\n"
        "FACTORIAL_DATABASE_URL='postgresql+asyncpg://u:p@db/f'\n"
        "FACTORIAL_ASSISTANT__CONCURRENCY='2'\n"
    )
    subprocess.run(
        [
            "sh",
            "-eu",
            "-c",
            copy.replace("/run/secrets/factorial.env", shlex.quote(str(settings))).replace(
                "/run/factorial-assistant/.env", shlex.quote(str(env))
            ),
        ],
        check=True,
        env={"PATH": os.environ["PATH"]},
    )
    assert env.read_text().splitlines() == [
        "FACTORIAL_API_TOKEN='token'",
        "FACTORIAL_DATABASE_URL='postgresql+asyncpg://u:p@db/f'",
        "FACTORIAL_ASSISTANT__CONCURRENCY='2'",
    ]


@pytest.mark.parametrize(
    ("url", "address"),
    [
        ("", ""),
        ("postgresql+asyncpg://factory:factory@localhost:5432/factory", ""),
        ("postgresql+asyncpg://factory:factory@127.0.0.1:5432/factory", ""),
        ("postgresql+asyncpg://factory:factory@[::1]:5432/factory", ""),
        ("postgresql+asyncpg://factory:pw@LOCALHOST:5432/factory", ""),
        ("postgresql+asyncpg://factory:pw@127.0.0.2/factory", ""),
        (
            "postgresql+asyncpg://factory:pw@factorial.abc123.us-east-1.rds.amazonaws.com/factory",
            "factorial.abc123.us-east-1.rds.amazonaws.com 5432",
        ),
        (
            "postgresql+asyncpg://factory:a%40b@db.abc123.us-east-1.rds.amazonaws.com/factory",
            "db.abc123.us-east-1.rds.amazonaws.com 5432",
        ),
        (
            "postgresql+asyncpg://factory:pw@host.docker.internal:5433/factory",
            "host.docker.internal 5433",
        ),
    ],
)
def test_only_a_url_naming_another_host_takes_the_image_off_its_own_postgres(
    url: str, address: str
) -> None:
    """Read as loopback, an RDS URL would start the image's own Postgres on EFS and migrate an
    empty database, which the app then runs on as if it were new."""
    shown = subprocess.run(
        [
            "sh",
            "-c",
            '. "$0" && factorial_database_address "$1"',
            FACTORIAL_ETC / "database.sh",
            url,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert shown.stdout.strip() == address


@pytest.mark.parametrize("mode", ["internal", "external"])
def test_env_sh_exports_the_loopback_database_and_localhost_cors_only_for_its_own_postgres(
    tmp_path: Path, mode: str
) -> None:
    """Exported, both outrank /app/.env. On RDS the CSRF guard, the terminal handshake and MCP
    token minting would then admit a localhost Origin, and none of them says so."""
    helper = "/etc/factorial/database.sh"
    text = (FACTORIAL_ETC / "env.sh").read_text()
    assert f". {helper}\n" in text
    env_sh = tmp_path / "env.sh"
    env_sh.write_text(text.replace(helper, str(FACTORIAL_ETC / "database.sh")))
    shown = subprocess.run(
        ["sh", "-c", '. "$0" && env', env_sh],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ["PATH"], "FACTORIAL_IMAGE_DATABASE": mode},
    )
    names = {line.partition("=")[0] for line in shown.stdout.splitlines()}
    own = mode == "internal"
    assert ("FACTORIAL_DATABASE_URL" in names) is own
    assert ("FACTORIAL_CORS_ORIGINS" in names) is own


COMPOSE_URL = "postgresql+asyncpg://factorial-db-user:pw_-1@database:5432/factorial-db"
FACTORY_ON_SERVICE = "postgresql+asyncpg://factory:factory@database:5432/factory"
FACTORY_BESIDE_APP = "postgresql+asyncpg://factory:factory@127.0.0.1:5432/factory"
UNSET = "<unset>"


@pytest.mark.parametrize(
    ("url_line", "database_host", "url"),
    [
        (COMPOSE_URL, "database", UNSET),
        ("postgresql+asyncpg://factorial-db-user:pw@database/factorial-db", "database", UNSET),
        ("", "database", FACTORY_ON_SERVICE),
        (
            "postgresql+asyncpg://factory:factory@localhost:5432/factory",
            "database",
            FACTORY_ON_SERVICE,
        ),
        (
            "postgresql+asyncpg://factorial-db-user:pw@database:5433/factorial-db",
            "database",
            FACTORY_ON_SERVICE,
        ),
        (COMPOSE_URL, None, FACTORY_BESIDE_APP),
        ("postgresql+asyncpg://someone:pw@127.0.0.1:5432/factory", None, FACTORY_BESIDE_APP),
    ],
)
def test_env_sh_leaves_only_the_compose_service_s_url_to_env(
    tmp_path: Path, url_line: str, database_host: str | None, url: str
) -> None:
    """Exported, the image's URL outranks `.env`: the compose `database` service makes its
    volume with the role and password factorial.env names, which the app could then not sign in
    as. Left to `.env`, that password never reaches the environment every agent run inherits. A
    `.env` written for `make dev` names localhost, a server the app container never runs, and the
    one container's Postgres is always `factory`'s."""
    env_file = tmp_path / ".env"
    line = f"FACTORIAL_DATABASE_URL={url_line}\n" if url_line else ""
    env_file.write_text(f"FACTORIAL_LOG_LEVEL=INFO\n{line}")
    env_sh = tmp_path / "env.sh"
    env_sh.write_text(
        (FACTORIAL_ETC / "env.sh")
        .read_text()
        .replace("/etc/factorial/database.sh", str(FACTORIAL_ETC / "database.sh"))
        .replace("/app/.env", str(env_file))
    )
    environ = {
        "PATH": os.environ["PATH"],
        "FACTORIAL_IMAGE_DATABASE": "internal",
        # A loopback URL in the environment leaves the image on its own database (init).
        "FACTORIAL_DATABASE_URL": "postgresql+asyncpg://stale:stale@127.0.0.1:5432/stale",
    }
    if database_host:
        environ["FACTORIAL_IMAGE_DATABASE_HOST"] = database_host
    shown = subprocess.run(
        ["sh", "-eu", "-c", f'. "$0" && printf %s "${{FACTORIAL_DATABASE_URL-{UNSET}}}"', env_sh],
        capture_output=True,
        text=True,
        check=True,
        env=environ,
    )
    assert shown.stdout == url


def _cluster_owner(tmp_path: Path, settings: Path | None) -> tuple[str, str, str]:
    pwfile, create = tmp_path / "pw", tmp_path / "create.sql"
    shown = subprocess.run(
        [
            "sh",
            "-eu",
            "-c",
            '. "$0" && factorial_cluster_owner "$@"',
            FACTORIAL_ETC / "postgres.sh",
            pwfile,
            create,
            *([settings] if settings else []),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return shown.stdout, pwfile.read_text(), create.read_text()


def test_a_new_cluster_is_made_with_the_role_and_database_the_settings_file_names(
    tmp_path: Path,
) -> None:
    """Made as `factory`, the cluster would refuse the URL factorial.env names, and the app
    would wait out db-ready's minute at every start."""
    settings = tmp_path / "factorial.env"
    settings.write_text(f"FACTORIAL_API_TOKEN=x\nFACTORIAL_DATABASE_URL='{COMPOSE_URL}'\n")
    assert _cluster_owner(tmp_path, settings) == (
        "factorial-db-user\n",
        "pw_-1",
        'CREATE DATABASE "factorial-db";\n',
    )


@pytest.mark.parametrize("line", [None, "FACTORIAL_API_TOKEN=x\n"])
def test_a_new_cluster_named_by_no_url_is_the_one_the_dev_stack_reaches(
    tmp_path: Path, line: str | None
) -> None:
    """`make up` and a plain `docker run` mount no URL, and env.sh then exports factory's."""
    settings = None
    if line is not None:
        settings = tmp_path / "factorial.env"
        settings.write_text(line)
    assert _cluster_owner(tmp_path, settings) == (
        "factory\n",
        "factory",
        'CREATE DATABASE "factory";\n',
    )


def test_only_the_compose_database_service_makes_its_cluster_from_the_settings_file() -> None:
    """The one-container image runs Postgres beside the app at 127.0.0.1, where env.sh exports
    factory's URL whatever the file names."""
    standalone = (FACTORIAL_ETC / "postgres-standalone").read_text()
    init_db = (S6 / "scripts" / "init-db").read_text()
    assert "factorial_initdb /run/secrets/factorial.env\n" in standalone
    assert re.search(r"^factorial_initdb$", init_db, re.MULTILINE)


@pytest.mark.parametrize(
    ("recorded", "user", "starts"),
    [
        (None, "factory", True),
        (None, "factorial-db-user", False),
        ("factorial-db-user", "factorial-db-user", True),
        ("factorial-db-user", "factory", False),
    ],
)
def test_a_volume_made_for_another_role_stops_the_start_and_names_both(
    tmp_path: Path, recorded: str | None, user: str, starts: bool
) -> None:
    """Started, the app fails its sign-in at every attempt, and db-ready and migrate report only
    that the password was refused."""
    role_file = tmp_path / "factorial-role"
    if recorded:
        role_file.write_text(f"{recorded}\n")
    shown = subprocess.run(
        [
            "sh",
            "-eu",
            "-c",
            '. "$0" && factorial_role_file="$1" && factorial_check_role "$2"',
            FACTORIAL_ETC / "postgres.sh",
            role_file,
            user,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (shown.returncode == 0) is starts
    if not starts:
        assert f"made for role {recorded or 'factory'}, but factorial.env names {user}." in (
            shown.stderr
        )


def test_a_url_initdb_cannot_read_leaves_no_password_file(tmp_path: Path) -> None:
    temp, bin_dir = tmp_path / "tmp", tmp_path / "bin"
    temp.mkdir()
    bin_dir.mkdir()
    # macOS's mktemp ignores TMPDIR.
    stub = bin_dir / "mktemp"
    stub.write_text(f'#!/bin/sh\nexec {shutil.which("mktemp")} "{temp}/file.XXXXXX"\n')
    stub.chmod(0o755)
    settings = tmp_path / "factorial.env"
    settings.write_text("FACTORIAL_DATABASE_URL=postgresql+asyncpg://database:5432/factorial-db\n")
    shown = subprocess.run(
        [
            "sh",
            "-eu",
            "-c",
            # Not after `&&`, which would suspend -eu inside the function, as no caller does.
            '. "$0"; factorial_initdb "$1"',
            FACTORIAL_ETC / "postgres.sh",
            settings,
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )
    assert shown.returncode != 0
    assert "names no user" in shown.stderr
    assert list(temp.iterdir()) == []


OWN_ORIGINS = ["http://localhost:8000", "http://127.0.0.1:8000"]


@pytest.mark.parametrize(
    ("line", "origins"),
    [
        ("", OWN_ORIGINS),
        (
            "FACTORIAL_CORS_ORIGINS='https://factorial.example.com'\n",
            ["https://factorial.example.com", *OWN_ORIGINS],
        ),
        (
            'export FACTORIAL_CORS_ORIGINS=["https://a.example.com","https://b.example.com"]\n',
            ["https://a.example.com", "https://b.example.com", *OWN_ORIGINS],
        ),
    ],
)
def test_env_sh_keeps_the_cors_origins_env_names(
    tmp_path: Path, line: str, origins: list[str]
) -> None:
    """Exported, the image's value outranks `.env`: a self-hoster's own address written there
    was dropped, and every origin-checked route then refused the dashboard, saying nothing."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"FACTORIAL_LOG_LEVEL=INFO\n{line}")
    env_sh = tmp_path / "env.sh"
    env_sh.write_text(
        (FACTORIAL_ETC / "env.sh")
        .read_text()
        .replace("/etc/factorial/database.sh", str(FACTORIAL_ETC / "database.sh"))
        .replace("/app/.env", str(env_file))
    )
    shown = subprocess.run(
        ["sh", "-eu", "-c", '. "$0" && printf %s "$FACTORIAL_CORS_ORIGINS"', env_sh],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ["PATH"], "FACTORIAL_IMAGE_DATABASE": "internal"},
    )
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]  (see test_settings.py)
        cors_origins=shown.stdout,
    )
    assert settings.cors_origins == origins


@pytest.mark.parametrize(
    ("environment", "expected"),
    [({}, "172.18.0.0/16"), ({"FORWARDED_ALLOW_IPS": "10.0.0.0/8"}, "10.0.0.0/8")],
)
def test_the_api_service_hands_uvicorn_the_settings_file_s_forwarded_allow_ips(
    tmp_path: Path, environment: dict[str, str], expected: str
) -> None:
    """uvicorn reads it from the environment alone: written in the mounted settings file, it
    reached nothing, and behind a proxy every client shared the proxy's address."""
    env_file = tmp_path / ".env"
    env_file.write_text("FACTORIAL_LOG_LEVEL=INFO\nFORWARDED_ALLOW_IPS=172.18.0.0/16\n")
    stub = tmp_path / "env.sh"
    stub.write_text("")
    run = tmp_path / "run"
    script, _uvicorn = (S6 / "s6-rc.d" / "api" / "run").read_text().split("\nexec ", 1)
    run.write_text(
        script.replace("/etc/factorial/env.sh", str(stub))
        .replace("/app/.env", str(env_file))
        .replace("cd /app", f"cd {tmp_path}")
        + '\nprintf %s "${FORWARDED_ALLOW_IPS:-}"\n'
    )
    shown = subprocess.run(
        ["sh", "-eu", str(run)],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ["PATH"], **environment},
    )
    assert shown.stdout == expected


def test_a_value_init_writes_into_env_reads_back_as_it_was_put(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ECS hands init each secret bare. Read back changed, by a `#`, a `$` or a space, the API
    token is shorter or the allowed domains differ, and nothing says so."""
    env_file = tmp_path / "factorial.env"
    target = "/run/secrets/factorial.env"
    [write] = [
        line.strip()
        for line in (S6 / "scripts" / "init").read_text().splitlines()
        if line.lstrip().startswith("printf") and f">> {target}" in line
    ]
    write = write.replace(target, shlex.quote(str(env_file)))
    token = "a #b c=d$x/e+f=="
    values = {
        "FACTORIAL_API_TOKEN": token,
        "FACTORIAL_AUTH__ALLOWED_DOMAINS": "Example.com, example.org",
    }
    for name, value in values.items():
        subprocess.run(
            ["sh", "-c", write],
            check=True,
            env={"PATH": os.environ["PATH"], "name": name, "value": value},
        )
    for name in values:
        monkeypatch.delenv(name, raising=False)
    settings = Settings(
        _env_file=env_file,  # pyright: ignore[reportCallIssue]  (see test_settings.py)
    )
    assert settings.api_token.get_secret_value() == token
    assert settings.auth.allowed_domains == ["example.com", "example.org"]


def _init_moves_the_settings(
    contenv: Path, secrets: Path, *, mounts: str = "", check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Runs init's step that turns FACTORIAL_* variables into the settings file, on `contenv`,
    writing under `secrets` rather than /run/secrets, with `mounts` as /proc/self/mountinfo."""
    lines = (S6 / "scripts" / "init").read_text().splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith("# Every service starts"))
    end = next(i for i, line in enumerate(lines) if line.startswith("# The task role's"))
    mountinfo = contenv.parent / "mountinfo"
    mountinfo.write_text(mounts.replace("/run/secrets", str(secrets)))
    step = (
        "".join(lines[start:end])
        .replace("/run/secrets", str(secrets))
        .replace("/proc/self/mountinfo", str(mountinfo))
    )
    return subprocess.run(
        ["sh", "-eu", "-c", f"newline='\n'\n{step}"],
        check=check,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "contenv": str(contenv)},
    )


@pytest.mark.parametrize(
    "retired",
    [{}, {"FACTORIAL_GITHUB__PRIVATE_KEY": "-----BEGIN KEY-----", "FACTORIAL_GITHUB__APP_ID": "1"}],
)
def test_init_reads_the_settings_from_the_environment_when_the_master_key_is_there(
    tmp_path: Path, retired: dict[str, str]
) -> None:
    """A deployment may still pass a retired App's FACTORIAL_GITHUB__* variables, its key among
    them. Those are left out of every file and taken out of the environment every agent run
    inherits."""
    contenv, secrets = tmp_path / "contenv", tmp_path / "secrets"
    contenv.mkdir()
    values = {
        "FACTORIAL_MASTER_KEY": "a2V5",
        "FACTORIAL_API_TOKEN": "token",
        "FACTORIAL_LOG_LEVEL": "INFO",
        **retired,
    }
    for name, value in values.items():
        (contenv / name).write_text(value)

    _init_moves_the_settings(contenv, secrets)

    assert sorted(path.name for path in contenv.iterdir()) == ["FACTORIAL_LOG_LEVEL"]
    assert sorted(path.name for path in secrets.iterdir()) == ["factorial.env"]
    assert (secrets / "factorial.env").read_text().splitlines() == [
        "FACTORIAL_API_TOKEN='token'",
        "FACTORIAL_MASTER_KEY='a2V5'",
    ]


def test_init_stops_at_a_master_key_passed_beside_the_mounted_settings(tmp_path: Path) -> None:
    """`docker run -e FACTORIAL_MASTER_KEY=...` beside the mount sent init to replace the mounted
    file, and the start stopped on "Device or resource busy", which names no cause. Each setting
    that came as a variable is named, and neither the file nor the environment is touched."""
    contenv, secrets = tmp_path / "contenv", tmp_path / "secrets"
    contenv.mkdir()
    secrets.mkdir()
    (secrets / "factorial.env").write_text("FACTORIAL_API_TOKEN='token'\n")
    passed = ("FACTORIAL_API_TOKEN", "FACTORIAL_LOG_LEVEL", "FACTORIAL_MASTER_KEY")
    for name in passed:
        (contenv / name).write_text("x")
    mounts = (
        "730 700 0:120 / / rw,relatime - overlay overlay rw\n"
        "741 730 254:1 /factorial.env /run/secrets/factorial.env ro,relatime - ext4 /dev/vda1 rw\n"
    )

    stopped = _init_moves_the_settings(contenv, secrets, mounts=mounts, check=False)

    assert stopped.returncode == 1
    assert "FACTORIAL_API_TOKEN FACTORIAL_MASTER_KEY," in stopped.stderr
    assert "FACTORIAL_LOG_LEVEL" not in stopped.stderr
    assert "-e or --env-file" in stopped.stderr
    assert sorted(path.name for path in contenv.iterdir()) == list(passed)
    assert (secrets / "factorial.env").read_text() == "FACTORIAL_API_TOKEN='token'\n"


def test_init_replaces_a_settings_file_an_earlier_start_wrote(tmp_path: Path) -> None:
    """A container that restarts keeps /run: the file there is one init wrote, not a mount, and
    the variables still move into it."""
    contenv, secrets = tmp_path / "contenv", tmp_path / "secrets"
    contenv.mkdir()
    secrets.mkdir()
    (secrets / "factorial.env").write_text("FACTORIAL_API_TOKEN='old'\n")
    for name, value in {"FACTORIAL_MASTER_KEY": "a2V5", "FACTORIAL_API_TOKEN": "new"}.items():
        (contenv / name).write_text(value)

    _init_moves_the_settings(contenv, secrets, mounts="730 700 0:120 / / rw - overlay overlay rw\n")

    assert (secrets / "factorial.env").read_text().splitlines() == [
        "FACTORIAL_API_TOKEN='new'",
        "FACTORIAL_MASTER_KEY='a2V5'",
    ]


def test_init_leaves_a_mounted_start_s_environment_but_the_retired_app(tmp_path: Path) -> None:
    contenv, secrets = tmp_path / "contenv", tmp_path / "secrets"
    contenv.mkdir()
    for name in ("FACTORIAL_LOG_LEVEL", "FACTORIAL_GITHUB__PRIVATE_KEY_PATH"):
        (contenv / name).write_text("x")

    _init_moves_the_settings(contenv, secrets)

    assert sorted(path.name for path in contenv.iterdir()) == ["FACTORIAL_LOG_LEVEL"]
    assert not secrets.exists()


def test_init_takes_the_task_roles_credentials_out_of_the_services_environment(
    tmp_path: Path,
) -> None:
    """Left in, every agent run inherits a way to fetch the task role's credentials."""
    [strip] = [
        line.strip()
        for line in (S6 / "scripts" / "init").read_text().splitlines()
        if line.lstrip().startswith("rm ") and "AWS_CONTAINER" in line
    ]
    names = [
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        "ECS_CONTAINER_METADATA_URI",
        "ECS_CONTAINER_METADATA_URI_V4",
        "ECS_AGENT_URI",
    ]
    for name in [*names, "AWS_REGION"]:
        (tmp_path / name).write_text("x")
    subprocess.run(
        ["sh", "-c", strip],
        check=True,
        env={"PATH": os.environ["PATH"], "contenv": str(tmp_path)},
    )
    assert sorted(path.name for path in tmp_path.iterdir()) == ["AWS_REGION"]


def _unconditional(rule: dict[str, Any]) -> bool:
    return rule["action"] == "SCMP_ACT_ALLOW" and not any(
        rule.get(key) for key in ("args", "includes", "excludes")
    )


def test_the_seccomp_profile_is_dockers_default_plus_user_namespaces() -> None:
    profile = json.loads(SECCOMP.read_text("utf-8"))
    rules = profile["syscalls"]
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    assert not [r for r in rules if r["names"] == ["clone"]]
    [clone3] = [r for r in rules if r["names"] == ["clone3"]]
    assert (clone3["action"], clone3["errnoRet"]) == ("SCMP_ACT_ERRNO", 38)
    *rest, added = [r for r in rules if _unconditional(r)]
    assert set(added["names"]) == USERNS
    assert not USERNS & {name for r in rest for name in r["names"]}


def test_compose_applies_the_seccomp_profile() -> None:
    assert _app()["security_opt"] == ["seccomp=./docker/seccomp.json"]


def test_the_plain_docker_run_applies_the_seccomp_profile() -> None:
    tokens = shlex.split(RUN_CONTAINER.read_text(), comments=True)
    at = tokens.index("--security-opt")
    assert tokens[at + 1] == "seccomp=${PWD}/docker/seccomp.json"


@pytest.mark.parametrize(
    ("environ", "runs_postgres"),
    [
        ({}, True),
        ({"FACTORIAL_IMAGE_DATABASE": "external"}, False),
        ({"FACTORIAL_IMAGE_DATABASE_HOST": "database"}, False),
    ],
)
def test_only_the_image_on_its_own_database_here_starts_postgres(
    environ: dict[str, str], runs_postgres: bool
) -> None:
    """On RDS a Postgres of the image's own would migrate an empty database the app then runs
    on. Under compose it would open the `database` service's cluster from a second container,
    which no lock across containers prevents."""
    shown = subprocess.run(
        ["sh", "-eu", "-c", '. "$0" && factorial_runs_postgres', FACTORIAL_ETC / "database.sh"],
        env={"PATH": os.environ["PATH"], **environ},
        check=False,
    )
    assert (shown.returncode == 0) is runs_postgres


def test_the_image_gives_codex_cli_its_run_area() -> None:
    env = (FACTORIAL_ETC / "env.sh").read_text()
    assert "export FACTORIAL_RUNTIME__CODEX_CLI__LOCAL_ROOT=/data/runs/codex_cli\n" in env
    [line] = [
        line
        for line in (S6 / "scripts" / "init").read_text().splitlines()
        if "/data/runs/claude_cli" in line
    ]
    assert "/data/runs/codex_cli" in line


def test_nothing_the_image_ships_links_to_the_private_repository() -> None:
    """The repository is private, so a link into it is a 404 to everyone who pulled the public
    image: in the dashboard, an API message, an MCP resource or a file the scaffold writes."""
    private = re.compile(r"github\.com/leaninnovationlabs/|leaninnovationlabs/factorial\b")
    shipped = [
        REPO_ROOT / "orchestrator" / "src",
        REPO_ROOT / "runtime" / "src",
        REPO_ROOT / "docker" / "rootfs",
        REPO_ROOT / "ui" / "src",
        REPO_ROOT / "ui" / "index.html",
    ]
    found = [
        str(path.relative_to(REPO_ROOT))
        for root in shipped
        for path in ([root] if root.is_file() else root.rglob("*"))
        if path.is_file()
        and ".test." not in path.name
        and private.search(path.read_text("utf-8", errors="ignore"))
    ]
    assert found == []


def _name_deployment(env_file: Path, state_file: Path) -> None:
    subprocess.run(
        ["sh", str(FACTORIAL_ETC / "e2b-deployment.sh"), str(env_file), str(state_file)],
        check=True,
    )


@pytest.mark.parametrize(
    "line",
    [
        "FACTORIAL_RUNTIME__E2B__DEPLOYMENT=a",
        'export FACTORIAL_RUNTIME__E2B__DEPLOYMENT="a"',
        "FACTORIAL_RUNTIME__E2B__DEPLOYMENT = a",
        "FACTORIAL_RUNTIME__E2B__DEPLOYMENT='a'",
        "factorial_runtime__e2b__deployment=a",
        "Factorial_Runtime__E2B__Deployment=a",
        'FACTORIAL_RUNTIME__E2B={"deployment":"a"}',
        'FACTORIAL_RUNTIME={"e2b":{"deployment":"a"}}',
    ],
)
def test_a_named_deployment_is_never_replaced(tmp_path: Path, line: str) -> None:
    """The reaper fences by this name: a generated one beside prod's would stop it fencing
    prod's live sandboxes."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"FACTORIAL_API_TOKEN=t\n{line}\n")
    before = env_file.read_bytes()
    state_file = tmp_path / "e2b-deployment"
    _name_deployment(env_file, state_file)
    assert env_file.read_bytes() == before
    assert not state_file.exists()


def test_an_unnamed_install_keeps_one_name_across_starts(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    state_file = tmp_path / "e2b-deployment"
    env_file.write_text("FACTORIAL_API_TOKEN=t")
    env_file.chmod(0o640)
    _name_deployment(env_file, state_file)
    first = env_file.read_text()
    assert env_file.stat().st_mode & 0o777 == 0o640
    env_file.write_text("FACTORIAL_API_TOKEN=t")
    _name_deployment(env_file, state_file)
    assert env_file.read_text() == first
    [name] = re.findall(r"^FACTORIAL_RUNTIME__E2B__DEPLOYMENT=(.+)$", first, re.MULTILINE)
    assert re.fullmatch(r"factorial-[0-9a-f]{8}", name)
    assert state_file.read_text().strip() == name
    assert state_file.stat().st_mode & 0o777 == 0o600


def test_init_names_no_deployment_on_an_external_database() -> None:
    """ECS names its own, in the file this call would append to."""
    init = (S6 / "scripts" / "init").read_text()
    call = "sh /etc/factorial/e2b-deployment.sh /app/.env /data/e2b-deployment"
    assert re.search(rf"if ! factorial_external_database; then\n\s*{re.escape(call)}\n\s*fi", init)
    assert init.index("install -o factorial -g factorial -m 0400 /run/secrets/factorial.env") < (
        init.index(call)
    )


def test_a_failing_interpreter_leaves_the_env_file_alone(tmp_path: Path) -> None:
    """An unreadable file must not read as "no name": a set name would be shadowed."""
    env_file = tmp_path / ".env"
    env_file.write_text("FACTORIAL_RUNTIME__E2B__DEPLOYMENT=a\n")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "python").write_text("#!/bin/sh\nexit 1\n")
    (fake / "python").chmod(0o755)
    run = subprocess.run(
        ["sh", str(FACTORIAL_ETC / "e2b-deployment.sh"), str(env_file), str(tmp_path / "state")],
        env={"PATH": f"{fake}:{os.environ['PATH']}"},
    )
    assert run.returncode != 0
    assert env_file.read_text() == "FACTORIAL_RUNTIME__E2B__DEPLOYMENT=a\n"
    assert not (tmp_path / "state").exists()


def test_the_migrate_script_checks_the_master_key_only_on_the_images_own_database() -> None:
    script = (S6 / "scripts" / "migrate").read_text()
    before, _, guarded = script.partition("if ! factorial_external_database; then")
    assert "keycheck" not in before
    assert "python -m orchestrator.container keycheck" in guarded.split("\nfi", 1)[0]
    assert script.index("alembic upgrade head") < script.index("keycheck")

import json
import os
import stat
import subprocess
import sys

from runtime.assistant.adapters.claude_sdk.launcher import (
    ENV_AUTH_MODE,
    ENV_REAL_CLI,
    child_environment,
)


def test_allowlist_keeps_what_the_cli_needs_and_nothing_else():
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "LANG": "C.UTF-8",
        "ANTHROPIC_API_KEY": "sk",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "ENABLE_TOOL_SEARCH": "false",
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
        "CLAUDE_CODE_SESSION_ACCESS_TOKEN": "session",
        "CLAUDE_CODE_SSE_PORT": "4242",
        "FACTORIAL_MASTER_KEY": "m",
        "FACTORIAL_API_TOKEN": "t",
        "DATABASE_URL": "pg://",
        "GH_TOKEN": "g",
        ENV_AUTH_MODE: "api_key",
        ENV_REAL_CLI: "/x/claude",
    }
    env = child_environment(parent)
    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "LANG": "C.UTF-8",
        "ANTHROPIC_API_KEY": "sk",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "ENABLE_TOOL_SEARCH": "false",
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
    }


def test_subscription_child_carries_no_api_key():
    env = child_environment(
        {
            "ANTHROPIC_API_KEY": "host",
            "CLAUDE_CODE_OAUTH_TOKEN": "o",
            ENV_AUTH_MODE: "subscription",
        }
    )
    assert env == {"CLAUDE_CODE_OAUTH_TOKEN": "o"}


def test_api_key_child_carries_no_oauth_token():
    env = child_environment({"ANTHROPIC_API_KEY": "sk", "CLAUDE_CODE_OAUTH_TOKEN": "host"})
    assert env == {"ANTHROPIC_API_KEY": "sk"}


def test_launcher_execs_the_real_cli_with_the_built_environment(tmp_path):
    dump = tmp_path / "env.json"
    fake = tmp_path / "claude"
    fake.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'env': dict(os.environ)}}, open({str(dump)!r}, 'w'))\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = {
        "PATH": os.environ["PATH"],
        "FACTORIAL_CANARY": "leak",
        "ANTHROPIC_API_KEY": "sk",
        ENV_AUTH_MODE: "api_key",
        ENV_REAL_CLI: str(fake),
    }
    subprocess.run(
        [sys.executable, "-m", "runtime.assistant.adapters.claude_sdk.launcher", "--print", "x"],
        env=env,
        check=True,
    )
    seen = json.loads(dump.read_text())
    assert seen["argv"] == ["--print", "x"]
    assert "FACTORIAL_CANARY" not in seen["env"] and seen["env"]["ANTHROPIC_API_KEY"] == "sk"

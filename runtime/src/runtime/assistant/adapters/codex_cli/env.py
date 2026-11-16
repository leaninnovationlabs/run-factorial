"""Environment the Codex child receives for one assistant turn."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from runtime.adapters.codex_cli.auth import (
    AUTH_MODE_API_KEY,
    AUTH_MODE_BEDROCK,
    AUTH_MODE_SUBSCRIPTION,
    ENV_API_KEY,
    ENV_BEDROCK_BEARER,
)
from runtime.adapters.codex_cli.cli import ENV_CODEX_HOME
from runtime.assistant.adapters.codex_cli.relay import LOOPBACK
from runtime.hostgit import hardened_env

__all__ = ["build_child_env"]

_LOCALE: Final = ("LANG", "LC_ALL", "LC_CTYPE", "TZ")
_TLS: Final = ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
_PROXY: Final = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
_NO_PROXY: Final = ("NO_PROXY", "no_proxy")
_AWS: Final = ("AWS_REGION", "AWS_DEFAULT_REGION")


def build_child_env(
    *,
    auth_mode: str,
    credential: str,
    config_dir: Path,
    bedrock_region: str | None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Allowlisted env for `codex exec`: no database URL, no App key, no FACTORIAL_* secrets."""
    allowed = {*_LOCALE, *_TLS, *_PROXY, *_AWS, "PATH", "HOME", "USER", "TMPDIR", "TEMP"}
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in allowed:
            env[key] = value
    env.update(hardened_env())
    env[ENV_CODEX_HOME] = str(config_dir)
    if auth_mode == AUTH_MODE_API_KEY:
        env[ENV_API_KEY] = credential
    elif auth_mode == AUTH_MODE_BEDROCK:
        env[ENV_BEDROCK_BEARER] = credential
        # Codex's model requests go to the turn's relay, and a proxy the host names for every
        # address cannot reach a loopback one.
        hosts = [host.strip() for key in _NO_PROXY for host in env.get(key, "").split(",")]
        direct = ",".join(dict.fromkeys(host for host in [*hosts, LOOPBACK] if host))
        env.update(dict.fromkeys(_NO_PROXY, direct))
        region = (bedrock_region or "").strip()
        if region:
            env["AWS_REGION"] = region
            env["AWS_DEFAULT_REGION"] = region
    elif auth_mode != AUTH_MODE_SUBSCRIPTION:
        raise ValueError(f"unsupported auth_mode {auth_mode!r}")
    if extra:
        for key, value in extra.items():
            if key.startswith("FACTORIAL_"):
                continue
            env[key] = value
    return env

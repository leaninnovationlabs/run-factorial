"""A project's own GitHub credentials: non-secret fields in `project.settings["github"]`, secrets
in the project's encrypted `secret` rows.

Unlike GitLab's `settings_for_project`, nothing falls back to a deployment-wide credential: a
project without a complete connection reads as None and its GitHub work waits.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Project
from orchestrator.secrets import store

__all__ = [
    "CONNECTION_SECRETS",
    "PRIVATE_KEY_SECRET",
    "TOKEN_SECRET",
    "WEBHOOK_SECRET",
    "GitHubConnection",
    "connection_for_project",
    "github_bag",
    "has_connection",
    "project_bot_login",
]

PRIVATE_KEY_SECRET: Final = "GITHUB_CONNECTION_PRIVATE_KEY"
WEBHOOK_SECRET: Final = "GITHUB_CONNECTION_WEBHOOK_SECRET"
TOKEN_SECRET: Final = "GITHUB_CONNECTION_TOKEN"
CONNECTION_SECRETS: Final = frozenset({PRIVATE_KEY_SECRET, WEBHOOK_SECRET, TOKEN_SECRET})


@dataclass(frozen=True)
class GitHubConnection:
    mode: Literal["app", "token"]
    app_id: str
    private_key: SecretStr | None
    token: SecretStr | None
    webhook_secret: SecretStr | None
    bot_login: str
    #: Owner login to installation id.
    installations: Mapping[str, int]

    @property
    def fingerprint(self) -> str:
        """Changes when any credential does, without holding one: a cache keyed on it drops a
        client built from the old key or token."""
        parts = [
            self.mode,
            self.app_id,
            self.private_key.get_secret_value() if self.private_key else "",
            self.token.get_secret_value() if self.token else "",
            ",".join(f"{owner}={n}" for owner, n in sorted(self.installations.items())),
        ]
        return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def github_bag(project: Project) -> dict[str, Any]:
    settings = project.settings if isinstance(project.settings, dict) else {}
    bag = settings.get("github")
    return bag if isinstance(bag, dict) else {}


def project_bot_login(project: Project) -> str:
    """The login the project's GitHub work acts as. Read from the project, never from a pooled
    client's `connection`: the pool shares one client across projects with different logins."""
    return str(github_bag(project).get("bot_login") or "")


def _installations(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(owner): n
        for owner, n in raw.items()
        if isinstance(n, int) and not isinstance(n, bool)
    }


async def has_connection(session: AsyncSession, project: Project) -> bool:
    """Whether `connection_for_project` finds the credential its mode needs, from the secrets'
    names alone, for a reader that holds no master key."""
    bag = github_bag(project)
    names = set(await store.list_names(session, project.id))
    match bag.get("mode"):
        case "app":
            return bool(str(bag.get("app_id") or "").strip()) and PRIVATE_KEY_SECRET in names
        case "token":
            return TOKEN_SECRET in names
        case _:
            return False


async def connection_for_project(
    session: AsyncSession, project: Project, *, with_webhook_secret: bool = False
) -> GitHubConnection | None:
    """The project's connection, its secrets read in one query. The webhook secret is read only
    `with_webhook_secret`, for the routes that verify a delivery; everyone else's is None."""
    bag = github_bag(project)
    mode = bag.get("mode")
    app_id = str(bag.get("app_id") or "").strip() if mode == "app" else ""
    if mode not in ("app", "token") or (mode == "app" and not app_id):
        return None
    credential = PRIVATE_KEY_SECRET if mode == "app" else TOKEN_SECRET
    names = {credential, WEBHOOK_SECRET} if with_webhook_secret else {credential}
    found = await store.get_many(session, project.id, names)
    secret = found.get(credential)
    if secret is None or not secret.get_secret_value():
        return None
    if mode == "app":
        key, token, installations = secret, None, _installations(bag.get("installations"))
    else:
        key, token, installations = None, secret, {}
    return GitHubConnection(
        mode=mode,
        app_id=app_id,
        private_key=key,
        token=token,
        webhook_secret=found.get(WEBHOOK_SECRET),
        bot_login=project_bot_login(project),
        installations=installations,
    )

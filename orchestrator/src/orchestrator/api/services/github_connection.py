"""Saving and reading a project's GitHub connection.

The one writer of `project.settings["github"]` and of the connection's stored secrets. A save
looks the credential up on GitHub first, so a connection that is stored always has its bot login
and the installation of every owner the project touches: neither is looked up during a tick.
It asks GitHub before it locks the project row, so a slow GitHub holds up no edit of the project,
and refuses when the project's repositories moved while it asked.
Importable without a request, for the script that moves a deployment's App onto its projects.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from urllib.parse import urlsplit

from githubkit.exception import RequestError, RequestFailed
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.schemas import GitHubConnectionIn, GitHubConnectionOut
from orchestrator.api.services.errors import (
    Conflict,
    Invalid,
    NotFound,
    UpstreamFailed,
    as_sentence,
)
from orchestrator.db.models import Project, TicketSource, TicketSourceKind
from orchestrator.github.client import AppCredentialError, GitHubClient, split_repo
from orchestrator.github.connection import (
    PRIVATE_KEY_SECRET,
    TOKEN_SECRET,
    WEBHOOK_SECRET,
    GitHubConnection,
    github_bag,
    project_bot_login,
)
from orchestrator.secrets import store
from orchestrator.settings import APP_PREFIX, is_loopback_host

__all__ = ["connection_out", "github_repos", "save_github_connection", "webhook_url"]

logger = logging.getLogger(__name__)


def _given(value: SecretStr | None) -> SecretStr | None:
    return value if value is not None and value.get_secret_value().strip() else None


def webhook_url(base: str, project: Project) -> str:
    bag = github_bag(project)
    mode = bag.get("mode")
    if mode == "app":
        tail = f"/webhooks/github/apps/{bag.get('app_id') or ''}"
    elif mode == "token":
        tail = f"/webhooks/github/projects/{project.id}"
    else:
        return ""
    return "" if is_loopback_host(urlsplit(base).hostname or "") else f"{base}{tail}"


async def connection_out(
    session: AsyncSession, project: Project, *, base_url: str = APP_PREFIX
) -> GitHubConnectionOut:
    bag = github_bag(project)
    mode = bag.get("mode")
    names = set(await store.list_names(session, project.id))
    installations = bag.get("installations")
    return GitHubConnectionOut(
        mode=mode if mode in ("app", "token") else None,
        app_id=str(bag.get("app_id") or ""),
        bot_login=project_bot_login(project),
        installations=dict(installations) if isinstance(installations, dict) else {},
        private_key_set=PRIVATE_KEY_SECRET in names,
        webhook_secret_set=WEBHOOK_SECRET in names,
        token_set=TOKEN_SECRET in names,
        webhook_url=webhook_url(base_url, project),
    )


async def github_repos(session: AsyncSession, project: Project) -> list[str]:
    """The repositories the project's GitHub work reaches: its own, then its GitHub intake repo
    when that is another. The source's columns, not its row: a row this session loaded earlier
    would answer as it was then."""
    repos = [project.github_repo]
    source = (
        await session.execute(
            select(TicketSource.kind, TicketSource.external_ref).where(
                TicketSource.project_id == project.id
            )
        )
    ).first()
    if (
        source is not None
        and source.kind == TicketSourceKind.GITHUB
        and source.external_ref
        and source.external_ref.lower() != project.github_repo.lower()
    ):
        repos.append(source.external_ref)
    return repos


async def _locked(session: AsyncSession, project_id: int) -> Project:
    """The project row, locked and re-read: an edit committed since this session read it shows.
    `FOR NO KEY UPDATE`, as every editor of the project but a PATCH takes it: a webhook's event
    insert, which names the project, need not wait for the save."""
    project = (
        await session.execute(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if project is None:
        raise NotFound("Project not found.")
    return project


async def save_github_connection(
    session: AsyncSession,
    project: Project,
    change: GitHubConnectionIn,
    *,
    client_for: Callable[[GitHubConnection], GitHubClient] = GitHubClient,
    by: str,
    base_url: str = APP_PREFIX,
) -> GitHubConnectionOut:
    """Takes the project row's lock itself, after GitHub has answered; a caller already holding
    it, in the same transaction, re-locks it at no cost."""
    bag = copy.deepcopy(github_bag(project))
    same_mode = bag.get("mode") == change.mode
    given_key, given_token = _given(change.private_key), _given(change.token)
    given_webhook = _given(change.webhook_secret)
    name = PRIVATE_KEY_SECRET if change.mode == "app" else TOKEN_SECRET
    given = given_key if change.mode == "app" else given_token
    secret = given or (await store.get_many(session, project.id, {name})).get(name)

    app_id = (change.app_id or "").strip() or (str(bag.get("app_id") or "") if same_mode else "")
    if change.mode == "app":
        if not app_id:
            raise Invalid("Enter the GitHub App's ID.")
        if secret is None or not secret.get_secret_value():
            raise Invalid("Enter the GitHub App's private key.")
        key, token = secret, None
    else:
        if secret is None or not secret.get_secret_value():
            raise Invalid("Enter a GitHub token.")
        key, token, app_id = None, secret, ""

    connection = GitHubConnection(
        mode=change.mode,
        app_id=app_id,
        private_key=key,
        token=token,
        webhook_secret=given_webhook,
        bot_login="",
        installations={},
    )
    client = client_for(connection)
    bot_login = ""
    installations: dict[str, int] = {}
    repos = await github_repos(session, project)
    try:
        if change.mode == "app":
            bot_login = await client.describe_app()
            for repo in repos:
                owner = split_repo(repo)[0]
                if owner not in installations:
                    installations[owner] = await client.installation_for(repo)
        else:
            login = await client.describe_token_owner()
            typed = (change.bot_login or "").strip()
            if typed and typed.lower() != login.lower():
                raise Invalid(
                    f"This token belongs to {login}. Leave Bot username blank or enter {login}."
                )
            bot_login = login
    except AppCredentialError as exc:
        raise Invalid(as_sentence(str(exc))) from exc
    except RequestFailed as exc:
        if exc.response.status_code in (401, 403):
            raise Invalid("GitHub refused this credential.") from exc
        raise UpstreamFailed(f"GitHub answered {exc.response.status_code}. Try again.") from exc
    except RequestError as exc:
        raise UpstreamFailed("GitHub could not be reached. Try again.") from exc
    if not bot_login:
        raise Invalid("GitHub did not say which login this credential acts as. Enter it.")

    # What was checked against GitHub must still be what is stored: another save in between
    # would otherwise leave one App's id beside another's key, or installations for old repos.
    project = await _locked(session, project.id)
    moved = [repo.lower() for repo in await github_repos(session, project)] != [
        repo.lower() for repo in repos
    ]
    rekeyed = False
    if given is None:
        rekeyed = (await store.get_many(session, project.id, {name})).get(name) != secret
    if moved or rekeyed or github_bag(project) != bag:
        raise Conflict("The project changed while saving. Save again.")
    if given is not None:
        await store.put(session, project.id, name, given.get_secret_value())
    if given_webhook is not None:
        await store.put(session, project.id, WEBHOOK_SECRET, given_webhook.get_secret_value())
    await store.delete(
        session, project.id, TOKEN_SECRET if change.mode == "app" else PRIVATE_KEY_SECRET
    )

    settings = dict(project.settings) if isinstance(project.settings, dict) else {}
    new_bag: dict[str, object] = {
        "mode": change.mode,
        "bot_login": bot_login,
        "installations": installations,
    }
    if change.mode == "app":
        new_bag["app_id"] = app_id
    settings["github"] = new_bag
    project.settings = settings
    await session.flush()
    logger.info("github connection saved project=%s mode=%s by=%s", project.id, change.mode, by)
    return await connection_out(session, project, base_url=base_url)

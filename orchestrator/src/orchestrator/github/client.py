"""githubkit wrapper: one client per GitHub connection (`connection.py`), and per-run tokens.

A connection is a project's own credential: an App (id + private key, one installation per
owner) or a stored token. Two credentials come out of an App connection and they are not the
same thing:

* the orchestrator's installation token, one per installation, cached and reused across ticks —
  it backs every observation (`observer.py`) and every projection (`projector.py`). The
  installation is the repo owner's: the connection's saved `installations`, else one looked up
  with the App JWT and remembered on the instance;
* an agent run's repo-scoped token, minted fresh per run with that agent's own permission request
  and never cached (design.md §7.1 items 3 and 5). GitHub caps installation tokens at one hour,
  and the worker renews a live session's token before then (rollover, decision 56), so a run
  must never inherit a token that is already half spent.

A token connection has neither: every call, and every run, uses the stored token, which never
expires on GitHub's clock (`RunToken.expires_at` is None).

This module is the one owner of the agent-token mint (decision S7). A runtime adapter never
mints the credential the agent runs with: that arrives through `RunSpec.secrets["GH_TOKEN"]`,
minted here from the ceiling-checked `AgentManifest.permissions`. `pool.py` keeps one client per
connection, so a rotated key or token gets a new client on its next use.

githubkit does JWT → installation token natively (tech-stack decision 6); it needs the `auth-app`
extra (PyJWT) at runtime.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Hashable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast
from urllib.parse import urlsplit

import httpx
from githubkit import AppAuthStrategy, GitHub, TokenAuthStrategy
from githubkit.exception import GitHubException, RateLimitExceeded, RequestError, RequestFailed
from githubkit.retry import RetryOption, RetryServerError
from githubkit_schemas.latest.types import AppPermissionsType
from pydantic import BaseModel, SecretStr

from orchestrator.backoff import ConnectionBackoff
from orchestrator.github.connection import GitHubConnection

#: GHES is out of scope; `api_url` exists so a test can point a client at a local server.
GITHUB_API_URL: Final = "https://api.github.com"

#: The per-run token's environment name: minted by the orchestrator, never stored, so never
#: expected in a secret store.
GH_TOKEN_KEY: Final = "GH_TOKEN"

__all__ = [
    "APP_PERMISSIONS",
    "GH_TOKEN_KEY",
    "GITHUB_API_URL",
    "MAX_TOKEN_REPOS",
    "PERMISSION_LEVEL_RANK",
    "READ_PERMISSIONS",
    "AppCredentialError",
    "AppNotInstalled",
    "ConnectionBackoff",
    "GitHubClient",
    "InstallationAccess",
    "RateLimited",
    "ReadToken",
    "RunToken",
    "TokenScopeNotGranted",
    "check_granted",
    "is_rate_limited",
    "refusal_reason",
    "split_repo",
]

logger = logging.getLogger(__name__)


# The orchestrator's own installation profile, and the default profile for an agent run: what a
# manifest with no `permissions:` key requests, and what an unset `permission_ceiling` permits
# (design.md §7.1 item 5; `factorial/permission_policy.py`). A manifest may request less — S7
# threads the request through `mint_run_token` as an argument.
#
# `workflows: write` is NEVER requested (design.md §7.2): agents push branches to the *same*
# repository and `ci_green` is a postcondition, so CI runs agent-authored code with repository
# secrets before any human sees it. Write access to `.github/workflows/` would turn an injected
# ticket body into arbitrary code execution in CI, upstream of the human gate every other
# guarantee rests on. Do not add it, and do not add deploy-class permissions either until the App
# Runtime phase (§7.1.4).
#
# `checks: read` is what makes `ci_green` observable at all: check-run reads ride the
# orchestrator's own installation token, and an installation access token carries only what the
# mint REQUESTS — granting `checks` in GitHub's UI does nothing on its own, so without this entry
# every check-run read 403s and `ci_green` can never hold. Keeping one shared profile (design.md
# §7.1) means the agents' default request widens with it, by one read-only scope; weigh that
# against forking a second profile before narrowing it. Requesting `read` against an installation
# granting `write` is correct, not a mismatch: GitHub caps the minted token at the request, and
# `check_granted` compares the reply against that request.
APP_PERMISSIONS: Final[AppPermissionsType] = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "metadata": "read",
    "checks": "read",
}

#: How the two permission levels order, in the one place both layers read it from:
#: `factorial.permission_policy` compares a *request* against an operator ceiling, `check_granted`
#: below compares the same request against what GitHub handed back. Two tables would be two
#: chances to disagree about where `write` sits relative to `read`.
PERMISSION_LEVEL_RANK: Final[Mapping[str, int]] = {"read": 1, "write": 2}

#: The one scope GitHub attaches to an installation token nobody asked for, and therefore the one
#: unrequested grant `check_granted` tolerates. A pair, not a bare name: `metadata: write` was
#: never implicit and is a real violation. Another implicit grant gets added here by hand, with a
#: design.md §7.1 item 6 note.
_IMPLICIT_GRANT: Final[tuple[str, str]] = ("metadata", "read")

#: Refresh this long before GitHub's one-hour expiry. Covers clock skew plus a slow tick.
TOKEN_SAFETY_MARGIN: Final = timedelta(minutes=5)

#: The assistant's token (design.md §7.18): every scope its tools read, none of them at `write`.
#: No chat tool writes to GitHub; a proposal's write runs in the API, on the App's own token.
READ_PERMISSIONS: Final[Mapping[str, str]] = {
    "contents": "read",
    "issues": "read",
    "pull_requests": "read",
    "checks": "read",
    "metadata": "read",
}
#: GitHub's cap on `repositories` in one mint.
MAX_TOKEN_REPOS: Final = 500


class AppCredentialError(RuntimeError):
    """The App credentials cannot mint a token, and no retry will change that.

    A present-but-wrong App credential passes `_require_app` (the connection is complete) and
    fails later, on the mint call, where githubkit reports every failure as one exception family —
    read as retryable, the orchestrator would spin at attempt 1 forever. Two faults, each with its
    own evidence; the exception class alone is never enough.

    1. The key will not sign. githubkit wraps every non-timeout exception out of `client.send`
       into `RequestError`, so a PyJWT failure and a dropped TCP connection arrive as the same
       class. The discriminator is `__cause__`: a garbage PEM yields a PyJWT `InvalidKeyError`
       cause, a network fault an `httpx.HTTPError` one.
    2. The key signs and GitHub rejects it. A valid PEM belonging to a different App gets a 401; a
       wrong installation id gets a 404. Both arrive as `RequestFailed` (a
       `RequestError` subclass) with `__cause__` None, so the evidence is the HTTP status on the
       mint route. Both are deterministic — the next tick signs the identical JWT and is refused
       identically.

    Deliberately NOT classified, because escalating it would misdirect the fix: 403 on that route
    (a suspended installation or an org policy — an operator fixes it in GitHub's UI, not in the
    connection), 5xx and timeouts (ordinary transient failure), and any failure on a route that
    is not the installation access-tokens endpoint.

    A `RuntimeError` subclass because `_PERMANENT_CONFIG_ERRORS` already carries bare
    `RuntimeError` for this family, so no failure boundary needs changing.
    """


class AppNotInstalled(AppCredentialError):
    """The App has no installation on `owner`."""

    def __init__(self, app_id: str, owner: str) -> None:
        super().__init__(f"the GitHub App {app_id} is not installed on {owner}")
        self.owner = owner


#: The module path every PyJWT exception lives under. Matched by module rather than by importing
#: `jwt`: PyJWT arrives as githubkit's `auth-app` extra, not as a dependency this layer declares,
#: and tech-stack.md forbids adopting one that is not named there. A cause that is not PyJWT's is
#: left alone rather than guessed at.
_PYJWT_MODULE_ROOT: Final = "jwt."


#: The routes whose 401 means the key is wrong: the mint, and the installation lookup that can
#: precede it. Checked rather than assumed: a status-only rule would misread a 401 the moment
#: another App-authenticated call was added inside the context manager.
_APP_JWT_ROUTES: Final = re.compile(
    r"/app/installations/\d+/access_tokens/?$|/repos/[^/]+/[^/]+/installation/?$"
)

#: Statuses on those routes that a retry cannot change. 401: the JWT signed but GitHub will not
#: accept it — the key belongs to another App, or the App ID is wrong. 404: there is no such
#: installation (the lookup's own 404 is named in `installation_for`). 403 and every 5xx are
#: excluded (see `AppCredentialError`).
_APP_CREDENTIAL_STATUSES: Final = {
    401: "GitHub refused the App JWT (401). The private key does not belong to the App ID of "
    "the project's GitHub connection, or that ID is wrong",
    404: "GitHub has no such installation (404). Save the project's GitHub connection again on "
    "Ticket source, so Factorial reads the App's installations",
}


@contextmanager
def _app_credential_faults_named() -> Iterator[None]:
    """Turn a deterministic App-credential fault into `AppCredentialError`.

    Wraps only the two `/app/...` calls that sign with the private key; everything else keeps the
    class githubkit raised and its retry posture.

    `RequestFailed` is handled FIRST because it is a `RequestError` subclass: ordered the other
    way, a rejected-JWT 401 would fall into the cause-based arm, find `__cause__` None, and be
    re-raised as retryable.
    """
    try:
        yield
    except RequestFailed as exc:
        detail = _APP_CREDENTIAL_STATUSES.get(exc.response.status_code)
        if detail is None or not _APP_JWT_ROUTES.search(urlsplit(str(exc.request.url)).path):
            raise
        raise AppCredentialError(
            f"{detail}. Retrying won't help, because GitHub refuses the same key and installation "
            "again"
        ) from exc
    except RequestError as exc:
        module = "" if exc.__cause__ is None else type(exc.__cause__).__module__
        if module != "jwt" and not module.startswith(_PYJWT_MODULE_ROOT):
            raise
        cause = exc.__cause__
        raise AppCredentialError(
            "the GitHub App private key is present but unusable: it could not sign the App JWT "
            f"({type(cause).__name__}: {cause}). Paste the App's private key again in the "
            "project's GitHub connection on Ticket source. Retrying won't help, because the same "
            "key fails the same way"
        ) from exc


class TokenScopeNotGranted(RuntimeError):
    """GitHub minted a token that is not the one the run asked for — failed, never degraded.

    Raised by `mint_run_token` when the reply's `permissions` block does not MATCH the request, in
    either direction (`check_granted`). Both are 201s carrying a perfectly usable token:

    * too narrow — an App missing a scope simply gets a token without it (documented behaviour),
      and the run dies deep inside a container on a 403 nobody can attribute;
    * too wide — the agent holds authority the project's ceiling never granted, and nothing
      downstream would notice. A ceiling that can be exceeded without stopping the run is not a
      ceiling, so this is a failure rather than a warning.
    """


def check_granted(requested: Mapping[str, str], granted: Mapping[str, str]) -> list[str]:
    """Every way the token GitHub minted differs from the one that was asked for. Empty == ok.

    Two-directional, unlike `permission_policy.check_permissions`, which treats a ceiling wider
    than the request as fine: a token wider than the request is what S7 exists to prevent. A
    project capped at `contents: read` must not end up holding a `contents: write` token because
    the App's installation happened to have one — nothing downstream would notice, and the ceiling
    would have bought nothing. So the granted set must EQUAL the requested set, and three kinds of
    violation are all collected and named:

    * a requested scope missing, or granted below the level asked for — the run cannot do its job;
    * a requested scope granted ABOVE the level asked for — the run can do more than the ceiling
      allowed;
    * a granted scope NAME nobody requested — same reason, and `metadata: write` counts.

    One carve-out: `metadata: read`, which GitHub attaches to every installation token whether or
    not it was asked for. Another implicit grant gets added here deliberately, with a note in
    design.md §7.1 item 6 — never by widening this to "extras are fine".

    Pure: the caller owns the exception and the logging, so the comparison is testable against a
    hand-written response with no App, no network and no key.
    """
    problems: list[str] = []
    for scope, level in sorted(requested.items()):
        actual = granted.get(scope)
        if actual is None:
            problems.append(f"{scope}: requested {level}, not granted at all")
            continue
        # Total lookups, failing closed in both directions: an unrecognized level ranks 0 when it
        # stands for what was granted (covers nothing) and 3 when it stands for what was requested
        # (nothing covers it), so an unknown level can never compare as an exact match.
        granted_rank = PERMISSION_LEVEL_RANK.get(actual, 0)
        wanted_rank = PERMISSION_LEVEL_RANK.get(level, 3)
        if granted_rank < wanted_rank:
            problems.append(f"{scope}: requested {level}, granted only {actual}")
        elif granted_rank > wanted_rank:
            problems.append(f"{scope}: requested {level}, granted {actual} (more than requested)")
    for scope, level in sorted(granted.items()):
        if scope in requested or (scope, level) == _IMPLICIT_GRANT:
            continue
        problems.append(f"{scope}: granted {level}, never requested")
    return problems


def split_repo(repo: str) -> tuple[str, str]:
    """`"owner/name"` → `("owner", "name")`. The repo string is `project.github_repo`."""
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise ValueError(f"repo must be 'owner/name', got {repo!r}")
    return owner, name


def refusal_reason(exc: RequestFailed) -> str:
    """GitHub's own words for a refusal, read out of the response body.

    `str(exc)` is httpx's generic "Request failed" and the repr names only the status, so every
    422 read identically and told the operator nothing about which call refused or why.
    """
    fallback = f"HTTP {exc.response.status_code}"
    try:
        body = exc.response.json()
    except Exception:  # a refusal with no JSON body still has to produce a message
        return fallback
    if not isinstance(body, dict):
        return fallback
    parts = [str(body.get("message") or fallback)]
    errors = body.get("errors")
    if isinstance(errors, list):
        parts += [
            str(detail)
            for item in errors
            if isinstance(item, dict) and (detail := item.get("message") or item.get("code"))
        ]
    return "; ".join(parts)


@dataclass(frozen=True, slots=True)
class RunToken:
    """A repo-scoped installation token for one agent run, or a token connection's stored token
    (`expires_at` None). `SecretStr` so it cannot be logged by accident. The worker reads none of
    `expires_at`; rollover runs from `minted_at`."""

    token: SecretStr
    expires_at: datetime | None
    repo: str


@dataclass(frozen=True, slots=True)
class ReadToken:
    """A read-only token over one project's repositories under one owner, for the assistant's
    process, which holds no App key (design.md §7.18)."""

    token: SecretStr
    expires_at: datetime
    repos: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InstallationAccess:
    """What the App's installation is, and whether one repo is inside it.

    The precondition under every other repo-side probe. When a repo sits outside the
    installation, `repo_labels`, `repo_branch_protection` and the manifest reads all fail the
    same anonymous way, and the configuration page can only say "not checked" five times. Read
    once, this says which of the two it is: the App is not installed at all, or it is installed
    and this repository is not among the ones it was given.

    `permissions` is the *granted* map, straight from GitHub — not what `APP_PERMISSIONS`
    requests. It is the only honest answer to "did my permission change actually take effect",
    because GitHub queues an added permission as a request the installer must accept, and until
    they do the App keeps running on the old set with no error anywhere.
    """

    account: str
    account_is_org: bool
    installation_id: str
    #: `"all"` or `"selected"` — GitHub's `repository_selection`.
    selection: str
    permissions: Mapping[str, str]
    repo_reachable: bool

    @property
    def settings_url(self) -> str:
        """The page where repository selection and pending permission requests are handled.

        Org and user installations live at different URLs and only the org form exposes
        "selected repositories" to an owner, so this follows the account type rather than
        assuming. Reachable by the account's owners only — which is why it is a link the page
        offers, not a step it claims anyone can take.
        """
        if self.account_is_org:
            return (
                f"https://github.com/organizations/{self.account}"
                f"/settings/installations/{self.installation_id}"
            )
        return f"https://github.com/settings/installations/{self.installation_id}"


#: githubkit's default waits on a stalled connection for ever.
GITHUB_TIMEOUT: Final = httpx.Timeout(30.0, connect=10.0)
#: The longest one call sleeps before it retries. githubkit's default sleeps until a rate limit
#: resets, up to an hour, inside a pass that every tenant shares.
MAX_RETRY_SLEEP: Final = timedelta(seconds=5)
#: 1 s then 4 s.
_RETRY_SERVER_ERROR: Final = RetryServerError(max_retry=2)


_APP_LIMIT: Final = ("app",)
_TOKEN_LIMIT: Final = ("token",)


class RateLimited(ConnectionBackoff):
    """This connection spent its GitHub rate limit, and nothing calls GitHub on it until `until`."""

    def __init__(self, until: datetime) -> None:
        super().__init__(f"GitHub rate limit reached; calls resume at {until.isoformat()}")
        self.until = until


def is_rate_limited(exc: BaseException) -> bool:
    return isinstance(exc, ConnectionBackoff | RateLimitExceeded)


class _PooledTransport(httpx.AsyncHTTPTransport):
    # githubkit opens and closes an httpx client per request outside `async with`, and closing a
    # client closes its transport; this one outlives them so its pool's connections are reused.
    async def aclose(self) -> None:
        return None


_PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")


def _pooled_transport() -> _PooledTransport | None:
    """The shared transport, unless the environment configures a proxy: a client handed a
    transport skips httpx's own proxy rules, and only httpx applies them exactly."""
    if any(os.environ.get(name) or os.environ.get(name.upper()) for name in _PROXY_VARS):
        return None
    return _PooledTransport()


class GitHubClient:
    """Owns one connection's credentials and hands out authenticated githubkit clients.

    One instance per connection is the intended shape (`pool.py`) — the token caches are per
    instance. `connection` None holds no credential, as the assistant's read client
    (`assistant/github_token.py`): every call that needs one refuses.
    """

    def __init__(
        self, connection: GitHubConnection | None, *, api_url: str = GITHUB_API_URL
    ) -> None:
        self.connection = connection
        self._api_url = api_url
        saved = connection.installations if connection else {}
        self._installations = {owner.lower(): n for owner, n in saved.items()}
        self._tokens: dict[int, tuple[str, datetime]] = {}
        #: Owner → (token, client): rebuilt when that owner's token changes.
        self._apis: dict[str, tuple[str, GitHub[TokenAuthStrategy]]] = {}
        self._transport = _pooled_transport()
        #: Process memory, per credential (`_backoff_key`): GitHub limits each installation, the
        #: App's own JWT and each stored token apart, so one spent limit never holds another.
        self._backoff: dict[Hashable, datetime] = {}

    # --- rate limits --------------------------------------------------------------------------

    def _backoff_key(self, repo: str) -> Hashable:
        """The limit a call on `repo` spends: the stored token's, or its owner's installation's."""
        if self.connection is not None and self.connection.mode == "token":
            return _TOKEN_LIMIT
        return ("owner", split_repo(repo)[0].lower())

    def backoff_until(self, repo: str) -> datetime | None:
        """When calls on `repo` resume, while its credential waits out a rate limit."""
        return self._waiting(self._backoff_key(repo))

    def _waiting(self, key: Hashable) -> datetime | None:
        until = self._backoff.get(key)
        return until if until is not None and until > datetime.now(UTC) else None

    def _check_backoff(self, key: Hashable) -> None:
        until = self._waiting(key)
        if until is not None:
            raise RateLimited(until)

    def _retry(self, key: Hashable) -> Callable[[GitHubException, int], RetryOption]:
        """githubkit's retry decision, bounded to `MAX_RETRY_SLEEP`. A longer rate limit is
        remembered under `key` instead, and every call on that credential refuses until it
        ends."""

        def decide(exc: GitHubException, retry_count: int) -> RetryOption:
            if isinstance(exc, RateLimitExceeded):
                if retry_count < 1 and exc.retry_after <= MAX_RETRY_SLEEP:
                    return RetryOption(True, exc.retry_after)
                until = datetime.now(UTC) + exc.retry_after
                current = self._backoff.get(key)
                if current is None or until > current:
                    self._backoff[key] = until
                logger.warning("GitHub rate limit reached on %s; it waits until %s", key, until)
                return RetryOption(False)
            return _RETRY_SERVER_ERROR(exc, retry_count)

        return decide

    def _github(
        self, auth: Any, *, backoff: Hashable, base_url: str | None = None, **extra: Any
    ) -> GitHub[Any]:
        return GitHub(
            auth,
            base_url=base_url or self._api_url,
            async_transport=self._transport,
            timeout=GITHUB_TIMEOUT,
            auto_retry=self._retry(backoff),
            **extra,
        )

    # --- auth ---------------------------------------------------------------------------------

    def _require_connection(self) -> GitHubConnection:
        if self.connection is None:
            raise RuntimeError(
                "this project has no GitHub connection: connect it on Ticket source"
            )
        return self.connection

    def _require_app(self) -> tuple[GitHubConnection, SecretStr]:
        connection = self._require_connection()
        if connection.mode != "app" or connection.private_key is None:
            raise RuntimeError("this GitHub connection is a token, not an App")
        return connection, connection.private_key

    def _stored_token(self) -> str | None:
        connection = self._require_connection()
        if connection.mode != "token":
            return None
        if connection.token is None:
            raise RuntimeError("this GitHub token connection holds no token")
        return connection.token.get_secret_value()

    def _app_api(self) -> GitHub[AppAuthStrategy]:
        """Authenticated as the App itself (JWT). Only `/app/...` routes and the per-repo
        installation lookup accept this."""
        connection, key = self._require_app()
        self._check_backoff(_APP_LIMIT)
        return self._github(
            AppAuthStrategy(app_id=connection.app_id, private_key=key.get_secret_value()),
            backoff=_APP_LIMIT,
        )

    async def installation_for(self, repo: str) -> int:
        """The App's installation on `repo`'s owner."""
        connection, _key = self._require_app()
        owner, name = split_repo(repo)
        known = self._installations.get(owner.lower())
        if known is not None:
            return known
        with _app_credential_faults_named():
            async with self._app_api() as app:
                try:
                    resp = await app.rest.apps.async_get_repo_installation(owner, name)
                except RequestFailed as exc:
                    if exc.response.status_code != 404:
                        raise
                    raise AppNotInstalled(connection.app_id, owner) from exc
        found = resp.parsed_data.id
        self._installations[owner.lower()] = found
        return found

    async def _installation_token(self, installation: int) -> str:
        """The orchestrator's own token on one installation, cached until `TOKEN_SAFETY_MARGIN`
        before expiry."""
        cached = self._tokens.get(installation)
        if cached is not None and datetime.now(UTC) < cached[1] - TOKEN_SAFETY_MARGIN:
            return cached[0]
        with _app_credential_faults_named():
            async with self._app_api() as app:
                resp = await app.rest.apps.async_create_installation_access_token(
                    installation,
                    permissions=APP_PERMISSIONS,
                )
        minted = resp.parsed_data
        self._tokens[installation] = (minted.token, _parse_expiry(minted.expires_at))
        return minted.token

    async def _token_for(self, repo: str) -> str:
        stored = self._stored_token()
        if stored is not None:
            return stored
        return await self._installation_token(await self.installation_for(repo))

    async def api(self, repo: str) -> GitHub[TokenAuthStrategy]:
        """A githubkit client that can act on `repo`: as its owner's installation, or as the
        stored token. Callers must not close it — its lifetime is the client's, not the
        caller's."""
        limit = self._backoff_key(repo)
        self._check_backoff(limit)
        token = await self._token_for(repo)
        owner = split_repo(repo)[0].lower()
        cached = self._apis.get(owner)
        if cached is None or cached[0] != token:
            cached = (token, self._github(token, backoff=limit))
            self._apis[owner] = cached
        return cached[1]

    async def describe_app(self) -> str:
        """The App's bot login, `{slug}[bot]`."""
        with _app_credential_faults_named():
            async with self._app_api() as app:
                data = (await app.rest.apps.async_get_authenticated()).parsed_data
        slug = getattr(data, "slug", None)
        if not slug:
            raise RuntimeError("GitHub answered GET /app with no App slug")
        return f"{slug}[bot]"

    async def describe_token_owner(self) -> str:
        """The login the stored token acts as."""
        token = self._stored_token()
        if token is None:
            raise RuntimeError("this GitHub connection is an App, not a token")
        async with self._github(token, backoff=_TOKEN_LIMIT) as api:
            return (await api.rest.users.async_get_authenticated()).parsed_data.login

    async def installation_access(self, repo: str) -> InstallationAccess:
        """Read the installation, then test whether `repo` is inside it.

        Two calls with two identities: the installation itself is App-JWT state (`GET
        /app/installations/{id}`), while reachability has to be asked as the installation, since
        that is the credential every other probe uses. Asking as the App would answer a question
        nobody has.

        A 403 or 404 on the repo read is the answer, not an error — that is precisely what an
        unselected repository looks like. Anything else propagates so the caller degrades to
        "not checked" rather than reporting a reachable repo as missing.
        """
        owner, name = split_repo(repo)
        installation = await self.installation_for(repo)
        with _app_credential_faults_named():
            async with self._app_api() as app:
                data = (await app.rest.apps.async_get_installation(installation)).parsed_data

        account = getattr(data, "account", None)
        granted = getattr(data, "permissions", None)
        api = await self.api(repo)
        try:
            await api.rest.repos.async_get(owner, name)
            reachable = True
        except RequestFailed as exc:
            if exc.response.status_code not in (403, 404):
                raise
            reachable = False

        return InstallationAccess(
            account=str(getattr(account, "login", "") or ""),
            account_is_org=getattr(account, "type", "") == "Organization",
            installation_id=str(installation),
            selection=str(getattr(data, "repository_selection", "") or ""),
            permissions={
                scope: level
                for scope, level in (
                    granted.model_dump(exclude_none=True) if granted is not None else {}
                ).items()
                if isinstance(level, str)
            },
            repo_reachable=reachable,
        )

    async def mint_run_token(self, repo: str, permissions: Mapping[str, str]) -> RunToken:
        """A token scoped to one repository and one agent's permissions (§7.1 items 3 and 5).

        `permissions` is the scope → `read`|`write` map this run asks for — the manifest's own
        `permissions:` block once `factorial.permission_policy` has checked it against the project's
        ceiling, or `APP_PERMISSIONS` when the manifest declares none. Never cached (module
        docstring). A manifest's `secrets:` block does not narrow this; scoping at mint time is
        the actual boundary (design.md §7.4).

        Raises `TokenScopeNotGranted` when GitHub's reply does not match the request in either
        direction (`check_granted`) — the orchestrator trusts token facts, never config intent
        (design.md §2.4).

        A token connection mints nothing: the run gets the stored token, scoped by whatever
        GitHub gave it when it was made.
        """
        stored = self._stored_token()
        if stored is not None:
            return RunToken(token=SecretStr(stored), expires_at=None, repo=repo)
        token, expires_at = await self._mint_scoped([repo], permissions)
        return RunToken(token=token, expires_at=expires_at, repo=repo)

    async def mint_read_token(self, repos: Sequence[str]) -> ReadToken:
        """A read-only token over `repos`, one installation's, for the assistant (§7.18). Never
        cached here: the assistant's process caches what the API hands it."""
        if not repos:
            raise ValueError("a read token names at least one repository")
        if len(repos) > MAX_TOKEN_REPOS:
            raise ValueError(
                f"a read token names at most {MAX_TOKEN_REPOS} repositories; got {len(repos)}"
            )
        owners = sorted({split_repo(repo)[0].lower() for repo in repos})
        if len(owners) != 1:
            raise ValueError(f"a read token covers one installation's account; got {owners}")
        token, expires_at = await self._mint_scoped(repos, READ_PERMISSIONS)
        return ReadToken(token=token, expires_at=expires_at, repos=tuple(repos))

    async def _mint_scoped(
        self, repos: Sequence[str], permissions: Mapping[str, str]
    ) -> tuple[SecretStr, datetime]:
        """A token on `repos`, all under one owner, with exactly `permissions`, or
        `TokenScopeNotGranted`."""
        installation = await self.installation_for(repos[0])
        target = ", ".join(repos)
        with _app_credential_faults_named():
            async with self._app_api() as app:
                resp = await app.rest.apps.async_create_installation_access_token(
                    installation,
                    repositories=[split_repo(repo)[1] for repo in repos],
                    # UNCHECKED at the type level: `AppPermissionsType` is a TypedDict of literal
                    # levels and a `Mapping[str, str]` cannot be proven to satisfy it. A scope
                    # name this cast smuggles through is dropped client-side — githubkit
                    # re-validates the body against `AppPermissions`, which ignores extra keys —
                    # so it never reaches GitHub, cannot come back in the reply, and
                    # `check_granted` fails the mint. Fail-closed, but it means the pinned
                    # `githubkit-schemas` version bounds the scopes this factory can request at
                    # all: a name added to `PERMISSION_SCOPES` ahead of a schemas bump widens no
                    # token, it just makes every mint that asks for it fail.
                    permissions=cast("AppPermissionsType", dict(permissions)),
                )
        minted = resp.parsed_data
        granted = _granted_permissions(minted.permissions)
        if violations := check_granted(permissions, granted):
            raise TokenScopeNotGranted(
                f"GitHub minted a token for {target} that does not match the request: "
                + "; ".join(violations)
                + f" (requested: {_format_scopes(permissions)};"
                + f" granted: {_format_scopes(granted)})"
            )
        logger.debug(
            "minted a token for %s: requested %s, granted %s",
            target,
            _format_scopes(permissions),
            _format_scopes(granted),
        )
        return SecretStr(minted.token), _parse_expiry(minted.expires_at)


def _granted_permissions(granted: Any) -> dict[str, str]:
    """The scope → level map out of an `InstallationToken.permissions` field.

    That field is typed `Missing[AppPermissions]`, and all three union members are reachable: the
    model itself (optional fields defaulting to `UNSET`), githubkit's `UNSET` sentinel when the
    reply carried no `permissions` key, and `None` when it carried `"permissions": null` —
    githubkit models every optional property as nullable, so a null is parsed, not rejected. The
    `isinstance(granted, BaseModel)` guard covers the last two, both yielding `{}`; inside the
    model, `isinstance(level, str)` separates a real level from the per-field `UNSET` enum member
    and from a scope-level null.

    An empty map is never a pass: `check_granted` then names every requested scope as ungranted
    and the mint fails — no token facts arrived, so nothing about the token was verified.
    """
    if not isinstance(granted, BaseModel):
        return {}
    return {scope: level for scope, level in granted.model_dump().items() if isinstance(level, str)}


def _format_scopes(scopes: Mapping[str, str]) -> str:
    return ", ".join(f"{scope}:{level}" for scope, level in sorted(scopes.items())) or "(none)"


def _parse_expiry(value: str) -> datetime:
    """GitHub returns `2026-08-19T12:00:00Z`; `fromisoformat` handles the `Z` on 3.11+."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

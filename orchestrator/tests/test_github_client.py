"""The token-fact assertion: what `mint_run_token` asks GitHub for, and what it does when the
reply grants less (design.md §7.1 items 5 and 6, decision S7).

No network and no App key. `check_granted` is pure and exercised directly; the mint itself is
driven through a fake `_app_api` that returns a real `InstallationToken` built from the JSON
shape GitHub sends, so the field the assertion reads is the field githubkit actually parses —
`InstallationToken.permissions`, typed `Missing[AppPermissions]`, `UNSET` when the reply carried
no permissions block at all.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar, cast

import httpx
import pytest
from githubkit import TokenAuthStrategy
from githubkit.exception import RateLimitExceeded, RequestFailed
from githubkit_schemas.latest.models import InstallationToken
from pydantic import SecretStr

from orchestrator.github import client as client_module
from orchestrator.github.client import (
    APP_PERMISSIONS,
    READ_PERMISSIONS,
    AppCredentialError,
    AppNotInstalled,
    GitHubClient,
    RateLimited,
    TokenScopeNotGranted,
    _app_credential_faults_named,
    check_granted,
    split_repo,
)
from orchestrator.github.connection import GitHubConnection

# --- check_granted, the pure comparison -------------------------------------------------------


def test_an_exactly_matching_grant_has_no_violations() -> None:
    request = {"contents": "write", "metadata": "read"}
    assert check_granted(request, dict(request)) == []


def test_an_empty_request_is_not_satisfied_by_a_scoped_grant() -> None:
    """A request for nothing must come back as nothing. A token carrying `contents: write` that
    nobody asked for is the over-grant case at its starkest."""
    (problem,) = check_granted({}, {"contents": "write"})
    assert "contents" in problem
    assert "never requested" in problem


def test_a_missing_scope_is_named() -> None:
    problems = check_granted({"contents": "write", "issues": "write"}, {"contents": "write"})
    assert len(problems) == 1
    assert "issues" in problems[0]
    assert "not granted" in problems[0]


def test_a_lowered_scope_is_named_with_both_levels() -> None:
    (problem,) = check_granted({"contents": "write"}, {"contents": "read"})
    assert "contents" in problem
    assert "requested write" in problem
    assert "granted only read" in problem


def test_a_scope_granted_above_the_request_is_a_violation() -> None:
    """The direction the ceiling exists for. A project capped at `contents: read`, a manifest that
    duly asks for `contents: read`, and an App installation that happens to hold write — without
    this the run silently gets the write token and the ceiling bought nothing."""
    (problem,) = check_granted({"contents": "read"}, {"contents": "write"})
    assert "contents" in problem
    assert "requested read" in problem
    assert "(more than requested)" in problem


def test_an_unrequested_scope_name_is_a_violation() -> None:
    """Not just the level: a scope the request never named at all is authority the ceiling never
    saw, whatever the level."""
    (problem,) = check_granted({"contents": "write"}, {"contents": "write", "issues": "write"})
    assert "issues" in problem
    assert "never requested" in problem


def test_implicit_metadata_read_is_the_one_tolerated_extra() -> None:
    """GitHub attaches `metadata: read` to every installation token whether or not it was asked
    for, so tolerating it is a fact about the API, not a relaxation of the rule."""
    assert check_granted({"contents": "write"}, {"contents": "write", "metadata": "read"}) == []


def test_unrequested_metadata_write_is_not_carved_out() -> None:
    """The carve-out is a (name, level) pair. `metadata: write` was never implicit."""
    (problem,) = check_granted({"contents": "write"}, {"contents": "write", "metadata": "write"})
    assert "metadata" in problem
    assert "never requested" in problem


def test_requested_metadata_read_still_has_to_be_granted() -> None:
    """The carve-out excuses an EXTRA `metadata: read`; it does not excuse a missing one."""
    (problem,) = check_granted({"metadata": "read"}, {})
    assert "metadata" in problem
    assert "not granted" in problem


def test_every_violation_is_collected_not_just_the_first() -> None:
    """An operator fixing an App installation wants the whole list, matching `check_allowed`.
    Sorted within each direction, so the message is stable across runs."""
    problems = check_granted(
        {"contents": "write", "issues": "write", "pull_requests": "write"},
        {"pull_requests": "read"},
    )
    assert len(problems) == 3
    assert [p.split(":")[0] for p in problems] == ["contents", "issues", "pull_requests"]


def test_violations_in_both_directions_are_collected_together() -> None:
    """One reply can be short on one scope and long on another; the operator sees both at once."""
    problems = check_granted(
        {"contents": "write", "issues": "read"},
        {"contents": "read", "issues": "read", "actions": "write"},
    )
    assert len(problems) == 2
    assert any("contents" in p and "granted only read" in p for p in problems)
    assert any("actions" in p and "never requested" in p for p in problems)


def test_an_unrecognised_granted_level_covers_nothing() -> None:
    """Fails closed: a level neither `read` nor `write` ranks below both rather than being
    trusted, so it can never compare as an exact match in either direction."""
    assert check_granted({"contents": "read"}, {"contents": "elevated"}) != []
    assert check_granted({"contents": "elevated"}, {"contents": "write"}) != []


# --- the default profile's contents -----------------------------------------------------------


def test_the_default_profile_can_read_check_runs() -> None:
    """A live outage, pinned so a "minimal scopes" cleanup cannot re-open it (2026-08-21).

    `ci_green` is a postcondition on every impl PR, and the orchestrator observes check runs with
    its OWN installation token — minted from this map. An installation access token carries only
    what the mint REQUESTS, so granting `checks` on the installation in GitHub's UI is not enough
    on its own: with the scope absent here, every check-run read 403s, `ci_green` never holds, and
    the tickets behind it wait forever with nothing failing.

    Pinned as `read`, deliberately: the orchestrator only ever observes CI, and requesting `read`
    against an installation that grants `write` is correct — GitHub caps the token at the request
    (see `APP_PERMISSIONS`).
    """
    assert APP_PERMISSIONS.get("checks") == "read"


def test_the_default_profile_never_requests_workflows() -> None:
    """The other direction, and the single most load-bearing rule in the repo (design.md §7.2):
    CI runs agent-authored code with repository secrets before any human sees it, so the App must
    not be able to write `.github/workflows/`. Stated here as well as in the scope vocabulary,
    because this map is what is actually sent."""
    assert "workflows" not in APP_PERMISSIONS


# --- mint_run_token ---------------------------------------------------------------------------


@dataclass
class _Response:
    parsed_data: InstallationToken


class _FakeAppsApi:
    """The one githubkit route `mint_run_token` calls. The reply is parsed by the REAL
    `InstallationToken` model, so the field the assertion reads is the field githubkit produces —
    a duck type would let a wrong field name pass."""

    def __init__(self, reply: dict[str, Any], calls: list[dict[str, Any]]) -> None:
        self._reply = reply
        self.calls = calls

    async def async_create_installation_access_token(
        self, installation_id: int, **kwargs: Any
    ) -> _Response:
        self.calls.append({"installation_id": installation_id, **kwargs})
        return _Response(InstallationToken.model_validate(self._reply))


class _FakeAppApi:
    """The `async with self._app_api() as app:` context, and nothing else."""

    def __init__(self, apps: _FakeAppsApi) -> None:
        self.rest = type("_Rest", (), {"apps": apps})()

    async def __aenter__(self) -> _FakeAppApi:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _client(
    monkeypatch: pytest.MonkeyPatch, reply: dict[str, Any]
) -> tuple[GitHubClient, list[dict[str, Any]]]:
    client = GitHubClient(_app_connection(installations={"acme": 2}))
    apps = _FakeAppsApi(reply, [])
    monkeypatch.setattr(client, "_app_api", lambda: _FakeAppApi(apps))
    return client, apps.calls


def _reply(permissions: Mapping[str, str] | None) -> dict[str, Any]:
    body: dict[str, Any] = {"token": "ghs_minted", "expires_at": "2026-08-20T12:00:00Z"}
    if permissions is not None:
        body["permissions"] = dict(permissions)
    return body


async def test_the_requested_permissions_reach_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not `APP_PERMISSIONS`: what the caller asked for, scoped to the one repository."""
    request = {"contents": "read", "metadata": "read"}
    client, calls = _client(monkeypatch, _reply(request))

    token = await client.mint_run_token("acme/widgets", request)

    assert token.repo == "acme/widgets"
    assert token.token.get_secret_value() == "ghs_minted"
    # BARE repository name, as the API requires — the owner comes from the installation.
    assert calls[0]["repositories"] == ["widgets"]
    assert calls[0]["permissions"] == request


async def test_the_implicit_metadata_grant_does_not_fail_the_mint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The only extra a real reply is expected to carry. A mint that refused it would refuse
    every mint."""
    client, _calls = _client(monkeypatch, _reply({"contents": "write", "metadata": "read"}))
    token = await client.mint_run_token("acme/widgets", {"contents": "write"})
    assert token.token.get_secret_value() == "ghs_minted"


async def test_a_grant_wider_than_the_request_fails_the_mint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The security direction. An App installation holding more than this agent's ceiling allows
    must not quietly hand that surplus to the run — both the over-level scope and the entirely
    unrequested one are named."""
    granted = {"contents": "write", "metadata": "read", "issues": "read"}
    client, _calls = _client(monkeypatch, _reply(granted))

    with pytest.raises(TokenScopeNotGranted) as exc:
        await client.mint_run_token("acme/widgets", {"contents": "read"})

    message = str(exc.value)
    assert "contents: requested read, granted write (more than requested)" in message
    assert "issues: granted read, never requested" in message
    # Both sides of the comparison, so the operator can see what the installation really holds.
    assert "requested: contents:read" in message
    assert "granted: contents:write, issues:read, metadata:read" in message


async def test_a_missing_scope_fails_the_mint_and_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bite: the App installation lacks `issues`, GitHub narrows silently and returns a
    perfectly usable token that cannot do the job. Config intent said otherwise; the token facts
    win (design.md §7.1 item 6)."""
    client, _calls = _client(monkeypatch, _reply({"contents": "write", "metadata": "read"}))

    # `APP_PERMISSIONS` is a TypedDict, so its values type as `object`; the request map is a
    # plain scope -> level mapping, and the widening happens here rather than in the signature.
    default_profile = {scope: str(level) for scope, level in APP_PERMISSIONS.items()}

    with pytest.raises(TokenScopeNotGranted) as exc:
        await client.mint_run_token("acme/widgets", default_profile)

    message = str(exc.value)
    assert "issues" in message
    assert "pull_requests" in message
    # The repo, and what WAS granted, so the operator can see the installation's real profile.
    assert "acme/widgets" in message
    assert "granted: contents:write, metadata:read" in message


async def test_a_lowered_scope_fails_the_mint(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _calls = _client(monkeypatch, _reply({"contents": "read", "metadata": "read"}))

    with pytest.raises(TokenScopeNotGranted) as exc:
        await client.mint_run_token("acme/widgets", {"contents": "write", "metadata": "read"})

    assert "contents: requested write, granted only read" in str(exc.value)


async def test_a_reply_with_no_permissions_block_fails_the_mint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`InstallationToken.permissions` is `Missing[...]` and absent means UNSET. No token facts
    arrived, so nothing was verified — that fails, rather than passing by default."""
    client, _calls = _client(monkeypatch, _reply(None))

    with pytest.raises(TokenScopeNotGranted) as exc:
        await client.mint_run_token("acme/widgets", {"contents": "write"})

    assert "granted: (none)" in str(exc.value)


async def test_a_reply_with_an_explicitly_null_permissions_block_fails_the_mint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third member of `Missing[AppPermissions]`. githubkit models every optional property as
    nullable (its issue #47), so `"permissions": null` parses to `None` rather than raising — and
    `None` is not a `BaseModel`, so it reads as no token facts at all, exactly like an absent
    block. A clean refusal, never an `AttributeError` deep in the mint."""
    body: dict[str, Any] = {
        "token": "ghs_minted",
        "expires_at": "2026-08-20T12:00:00Z",
        "permissions": None,
    }
    client, _calls = _client(monkeypatch, body)

    with pytest.raises(TokenScopeNotGranted) as exc:
        await client.mint_run_token("acme/widgets", {"contents": "write"})

    assert "granted: (none)" in str(exc.value)


async def test_a_scope_level_null_reads_as_not_granted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-field nullability, same cause: `{"contents": null}` parses to `None` on the field. The
    `isinstance(level, str)` filter drops it, so it is refused rather than being carried into the
    comparison as a level."""
    body: dict[str, Any] = {
        "token": "ghs_minted",
        "expires_at": "2026-08-20T12:00:00Z",
        "permissions": {"contents": None, "metadata": "read"},
    }
    client, _calls = _client(monkeypatch, body)

    with pytest.raises(TokenScopeNotGranted) as exc:
        await client.mint_run_token("acme/widgets", {"contents": "write"})

    assert "contents: requested write, not granted at all" in str(exc.value)


async def test_a_client_with_no_connection_refuses_before_any_request() -> None:
    client = GitHubClient(None)
    with pytest.raises(RuntimeError, match="no GitHub connection"):
        await client.mint_run_token("acme/widgets", {"contents": "write"})


def test_split_repo_rejects_a_bare_name() -> None:
    assert split_repo("acme/widgets") == ("acme", "widgets")
    with pytest.raises(ValueError, match="owner/name"):
        split_repo("widgets")


# --- the App key itself ------------------------------------------------------------------------
#
# These two run against the REAL githubkit auth path — no `_app_api` monkeypatch — because the
# whole finding is about what githubkit does to an exception on its way out. A duck-typed double
# would prove nothing: the classification reads `RequestError.__cause__`, a value only the real
# `githubkit.core` wrapping produces.

#: Unreachable, instantly. Chosen over a hostname so the negative control fails on `connect`
#: rather than on DNS, and so neither test can touch the network or hang.
_NOWHERE = "http://127.0.0.1:1"

#: Present, and garbage: NOT an absent credential (`_require_connection` catches that and it was
#: already PERMANENT), but one that passes every settings check and dies at signing time.
_MALFORMED_PEM = "-----BEGIN RSA PRIVATE KEY-----\nnot-a-key\n-----END RSA PRIVATE KEY-----\n"  # gitleaks:allow  # noqa: E501


def _well_formed_pem() -> str:
    """A real RSA key, generated per call — nothing key-shaped is checked into the repo."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _live_client(private_key: str, *, api_url: str = _NOWHERE) -> GitHubClient:
    return GitHubClient(
        _app_connection(private_key=SecretStr(private_key), installations={"acme": 2}),
        api_url=api_url,
    )


async def test_a_present_but_malformed_private_key_is_a_permanent_app_credential_error() -> None:
    """A silent-retry cause, closed.

    BEFORE: githubkit wrapped the PyJWT failure into `githubkit.exception.RequestError`, a plain
    `Exception` — so `_PERMANENT_CONFIG_ERRORS`'s bare `RuntimeError` member never saw it, the
    tick rolled back, no `step_run` row and no event survived, and the ticket retried as
    "attempt 1" forever with only a worker log line. AFTER: it is an `AppCredentialError`, which
    IS a `RuntimeError`, so the existing permanent-failure boundary escalates it on the first
    attempt (`test_reconcile.py::test_permanent_build_spec_failure_escalates_without_spinning`
    carries the escalation half).

    `RuntimeError` is the assertion that matters — that, and nothing about the class name, is
    what `_PERMANENT_CONFIG_ERRORS` matches on.
    """
    client = _live_client(_MALFORMED_PEM)

    with pytest.raises(AppCredentialError) as excinfo:
        await client.mint_run_token("acme/widgets", {"contents": "read"})

    assert isinstance(excinfo.value, RuntimeError)
    assert "private key is present but unusable" in str(excinfo.value)
    # Names the concrete PyJWT failure, so an operator is not left guessing which half is wrong.
    assert "InvalidKeyError" in str(excinfo.value)


class _CannedStatus(BaseHTTPRequestHandler):
    """A real HTTP server that answers every request with one canned status.

    Why a socket and not a double: the whole finding is about what githubkit BUILDS out of a
    response — `RequestFailed`, its `.response.status_code`, its `.request.url`, and its
    `__cause__` being None. A fake `_app_api` would let the classifier be tested against an
    exception this suite constructed itself, which proves the assertion and not the behaviour.
    """

    status: ClassVar[int] = 401
    body: ClassVar[bytes] = b'{"message": "A JSON web token could not be decoded"}'

    # `do_POST`/`do_GET` are BaseHTTPRequestHandler's own spellings, not ours.
    def do_POST(self) -> None:
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)

    do_GET = do_POST

    def log_message(self, format: str, *args: object) -> None:
        """Silence: the handler must not print to the test run's stderr."""


@contextmanager
def _github_answering(status: int) -> Iterator[str]:
    """Run `_CannedStatus` on a loopback port for the duration of the block, yielding its URL."""
    _CannedStatus.status = status
    server = HTTPServer(("127.0.0.1", 0), _CannedStatus)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


async def test_a_valid_key_for_the_wrong_app_is_a_permanent_app_credential_error() -> None:
    """The other half of a bad App key, and the likelier one.

    A malformed PEM never reaches the network. A perfectly well-formed PEM belonging to a
    DIFFERENT App does: it signs a valid JWT, GitHub returns 401 "A JSON web token could not be
    decoded", and githubkit raises `RequestFailed` — a `RequestError` SUBCLASS built at
    `githubkit/core.py:445` from the response, with `__cause__` **None**. The cause-based
    discriminator therefore could not see it, so it fell through as a plain `Exception` and the
    ticket retried a deterministic failure forever: the identical key signs an identical JWT on
    the next tick and is refused identically.

    Ordering is load-bearing here — `except RequestFailed` must precede `except RequestError`, or
    this lands in the cause arm, finds nothing, and is re-raised as retryable.
    """
    with _github_answering(401) as api_url:
        client = _live_client(_well_formed_pem(), api_url=api_url)

        with pytest.raises(AppCredentialError) as excinfo:
            await client.mint_run_token("acme/widgets", {"contents": "read"})

    assert isinstance(excinfo.value, RuntimeError)
    assert "App ID" in str(excinfo.value)


async def test_a_wrong_installation_id_is_a_permanent_app_credential_error() -> None:
    """The sibling deterministic fault: the key is right and the App is right, but
    saved installation id names an installation that is not there. GitHub answers 404
    on the same route, and no retry conjures the installation into existence."""
    with _github_answering(404) as api_url:
        client = _live_client(_well_formed_pem(), api_url=api_url)

        with pytest.raises(AppCredentialError) as excinfo:
            await client.mint_run_token("acme/widgets", {"contents": "read"})

    assert "no such installation" in str(excinfo.value)


async def test_a_forbidden_mint_stays_retryable_and_unclassified() -> None:
    """403 is deliberately NOT a credential fault. A suspended installation, or an org policy
    that currently blocks the App, is a human-side condition an operator resolves in GitHub's UI
    without touching the connection — escalating it as "your credentials are wrong" would send
    them editing settings that are correct. Left retryable, and left loud in the worker log."""
    with _github_answering(403) as api_url:
        client = _live_client(_well_formed_pem(), api_url=api_url)

        # Deliberately broad: the assertions below are about the classes it is NOT.
        with pytest.raises(Exception) as excinfo:
            await client.mint_run_token("acme/widgets", {"contents": "read"})

    assert not isinstance(excinfo.value, AppCredentialError)
    assert not isinstance(excinfo.value, RuntimeError)


async def test_a_server_error_on_the_mint_stays_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative control for the status rule: a 5xx is ordinary transient failure and must keep
    the posture it always had. Classifying it would block a live ticket on a condition that clears
    itself — strictly worse than the silent retry it replaced.

    The client agrees, and says so in code: it retries any `>= 500` twice on its own
    (`_RETRY_SERVER_ERROR`). That policy is corroboration, not something this test needs to
    observe — so it is switched off here, because paying its real 1+4s backoff would put 5
    seconds on every suite run to prove a classification that takes microseconds.
    """
    monkeypatch.setattr(client_module._RETRY_SERVER_ERROR, "max_retry", 0)
    with _github_answering(503) as api_url:
        client = _live_client(_well_formed_pem(), api_url=api_url)

        # Deliberately broad: the assertions below are about the classes it is NOT.
        with pytest.raises(Exception) as excinfo:
            await client.mint_run_token("acme/widgets", {"contents": "read"})

    assert not isinstance(excinfo.value, AppCredentialError)
    assert not isinstance(excinfo.value, RuntimeError)


async def test_a_401_off_the_mint_route_is_not_an_app_credential_error() -> None:
    """The route half of the rule, exercised as a live branch.

    Driven THROUGH the classifier on purpose. Today the context manager wraps only the two mint
    calls, so "a non-mint 401 is untouched" would hold vacuously — and would go on holding right
    up until someone wrapped a third App-authenticated call, at which point an unrelated 401 would
    silently start escalating as a credential fault. Here `GET /app` fails with the identical
    status inside the identical wrapper, and only the path differs, so what passes is the route
    check itself.
    """
    with _github_answering(401) as api_url:
        client = _live_client(_well_formed_pem(), api_url=api_url)

        with pytest.raises(RequestFailed) as excinfo, _app_credential_faults_named():
            # Reaching past the public surface deliberately: the wrapper exposes no verb for a
            # non-mint App route, and a non-mint App route is exactly what must fail here.
            async with client._app_api() as app:
                await app.rest.apps.async_get_authenticated()

    assert not isinstance(excinfo.value, AppCredentialError)
    assert excinfo.value.response.status_code == 401
    assert excinfo.value.request.url.path == "/app"  # not .../access_tokens


async def test_a_network_failure_with_a_good_key_stays_retryable() -> None:
    """The negative control, and the reason `_PERMANENT_CONFIG_ERRORS` is not widened by
    class: githubkit raises the SAME `RequestError` for a dropped connection. Escalating that as
    "misconfiguration" would block a live ticket on a transient blip — strictly worse than the
    silent retry it replaced. The cause is an `httpx` error here, not a PyJWT one, so it must
    pass straight through and keep the retry posture it has always had.
    """
    client = _live_client(_well_formed_pem())

    # Deliberately broad: the assertion below is about the class it is NOT.
    with pytest.raises(Exception) as excinfo:
        await client.mint_run_token("acme/widgets", {"contents": "read"})

    assert not isinstance(excinfo.value, AppCredentialError)
    assert not isinstance(excinfo.value, RuntimeError), (
        "a transient network fault must not land in _PERMANENT_CONFIG_ERRORS' RuntimeError arm"
    )


# --- mint_read_token ---------------------------------------------------------------------------


async def test_a_read_token_asks_for_reads_on_every_named_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, calls = _client(monkeypatch, _reply(READ_PERMISSIONS))

    token = await client.mint_read_token(["acme/widgets", "acme/intake"])

    assert token.repos == ("acme/widgets", "acme/intake")
    assert token.token.get_secret_value() == "ghs_minted"
    assert calls[0]["repositories"] == ["widgets", "intake"]
    assert calls[0]["permissions"] == dict(READ_PERMISSIONS)
    assert set(READ_PERMISSIONS.values()) == {"read"}


async def test_a_read_token_granted_a_write_fails_the_mint(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client(monkeypatch, _reply({**READ_PERMISSIONS, "contents": "write"}))
    with pytest.raises(TokenScopeNotGranted, match="acme/widgets"):
        await client.mint_read_token(["acme/widgets"])


@pytest.mark.parametrize(
    ("repos", "why"),
    [
        ([], "at least one"),
        (["acme/a", "other/b"], "one installation"),
        ([f"acme/r{n}" for n in range(501)], "at most 500"),
    ],
)
async def test_a_read_token_refuses_a_repo_list_github_would(
    monkeypatch: pytest.MonkeyPatch, repos: list[str], why: str
) -> None:
    client, calls = _client(monkeypatch, _reply(READ_PERMISSIONS))
    with pytest.raises(ValueError, match=why):
        await client.mint_read_token(repos)
    assert calls == []


async def test_a_run_token_still_names_its_one_repo_in_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client(monkeypatch, _reply({"contents": "write"}))
    with pytest.raises(TokenScopeNotGranted, match="for acme/widgets that does not match"):
        await client.mint_run_token("acme/widgets", {"contents": "read"})


# --- the connection: installations per owner, and token mode ------------------------------------


class _FakeOwnerAppsApi(_FakeAppsApi):
    """`_FakeAppsApi`, plus the per-repo installation lookup a connection falls back to."""

    def __init__(
        self, reply: dict[str, Any], calls: list[dict[str, Any]], installed: Mapping[str, int]
    ) -> None:
        super().__init__(reply, calls)
        self._installed = installed
        self.lookups: list[tuple[str, str]] = []

    async def async_get_repo_installation(self, owner: str, repo: str) -> Any:
        self.lookups.append((owner, repo))
        return type("_R", (), {"parsed_data": type("_I", (), {"id": self._installed[owner]})()})()


def _app_connection(**overrides: Any) -> GitHubConnection:
    fields: dict[str, Any] = {
        "mode": "app",
        "app_id": "1",
        "private_key": SecretStr("-----BEGIN PRIVATE KEY-----"),
        "token": None,
        "webhook_secret": None,
        "bot_login": "acme[bot]",
        "installations": {},
    }
    return GitHubConnection(**{**fields, **overrides})


async def test_the_installation_is_found_per_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """A saved owner mints on its own installation with no lookup; another owner is looked up
    once with the App JWT and then remembered."""
    client = GitHubClient(_app_connection(installations={"acme": 7}))
    apps = _FakeOwnerAppsApi(_reply({"contents": "read"}), [], {"other": 9})
    monkeypatch.setattr(client, "_app_api", lambda: _FakeAppApi(apps))

    await client.mint_run_token("acme/widgets", {"contents": "read"})
    await client.mint_run_token("other/thing", {"contents": "read"})
    await client.mint_run_token("other/more", {"contents": "read"})

    assert [call["installation_id"] for call in apps.calls] == [7, 9, 9]
    assert apps.lookups == [("other", "thing")]


async def test_an_owner_the_app_is_not_installed_on_names_it() -> None:
    with _github_answering(404) as api_url:
        client = GitHubClient(
            _app_connection(private_key=SecretStr(_well_formed_pem())), api_url=api_url
        )

        with pytest.raises(AppNotInstalled) as excinfo:
            await client.mint_run_token("other/thing", {"contents": "read"})

    assert str(excinfo.value) == "the GitHub App 1 is not installed on other"
    assert excinfo.value.owner == "other"


async def test_a_refused_jwt_on_the_installation_lookup_is_a_permanent_app_credential_error() -> (
    None
):
    with _github_answering(401) as api_url:
        client = GitHubClient(
            _app_connection(private_key=SecretStr(_well_formed_pem())), api_url=api_url
        )

        with pytest.raises(AppCredentialError, match="refused the App JWT"):
            await client.mint_run_token("other/thing", {"contents": "read"})


async def test_a_token_connection_hands_runs_the_stored_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No App, so nothing to mint and nothing to refresh: the run gets the stored token."""
    client = GitHubClient(
        _app_connection(mode="token", app_id="", private_key=None, token=SecretStr("ghp_stored"))
    )

    def no_app() -> Any:
        raise AssertionError("a token connection never signs an App JWT")

    monkeypatch.setattr(client, "_app_api", no_app)

    token = await client.mint_run_token("acme/widgets", {"contents": "write"})

    assert token.token.get_secret_value() == "ghp_stored"
    assert token.expires_at is None
    assert token.repo == "acme/widgets"
    api = await client.api("other/thing")
    assert cast("TokenAuthStrategy", api.auth).token == "ghp_stored"


async def test_a_scoped_mint_still_requires_granted_equals_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GitHubClient(_app_connection(installations={"acme": 7}))
    apps = _FakeOwnerAppsApi(_reply({"contents": "write", "issues": "read"}), [], {})
    monkeypatch.setattr(client, "_app_api", lambda: _FakeAppApi(apps))

    with pytest.raises(TokenScopeNotGranted) as exc:
        await client.mint_run_token("acme/widgets", {"contents": "read"})

    assert "contents: requested read, granted write (more than requested)" in str(exc.value)
    assert "issues: granted read, never requested" in str(exc.value)
    assert apps.calls[0]["installation_id"] == 7


class _SpentRateLimit(BaseHTTPRequestHandler):
    """GitHub's answer to a token whose hourly limit is spent: 403, nothing remaining, reset in
    an hour."""

    def do_GET(self) -> None:
        body = b'{"message": "API rate limit exceeded"}'
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("x-ratelimit-remaining", "0")
        self.send_header("x-ratelimit-reset", str(int(time.time()) + 3600))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Silence: the handler must not print to the test run's stderr."""


async def test_a_spent_rate_limit_is_waited_out_by_the_connection_not_the_call() -> None:
    """githubkit's default slept until the reset, up to an hour, inside the pass every tenant
    shares: one tenant's spent token held admission and every tick for all of them."""
    server = HTTPServer(("127.0.0.1", 0), _SpentRateLimit)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = GitHubClient(
            _app_connection(mode="token", app_id="", private_key=None, token=SecretStr("ghp_x")),
            api_url=f"http://127.0.0.1:{server.server_port}",
        )
        api = await client.api("acme/widgets")
        async with asyncio.timeout(10):
            with pytest.raises(RateLimitExceeded):
                await api.rest.repos.async_get("acme", "widgets")
    finally:
        server.shutdown()
        thread.join(timeout=5)

    until = client.backoff_until("acme/widgets")
    assert until is not None and until > datetime.now(UTC) + timedelta(minutes=55)
    with pytest.raises(RateLimited):
        await client.api("acme/widgets")


def _spent_for(owner: str) -> httpx.MockTransport:
    """GitHub, where `owner`'s installation has spent its hourly limit and every other has not."""

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(f"/repos/{owner}/"):
            return httpx.Response(
                403,
                json={"message": "API rate limit exceeded"},
                headers={
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": str(int(time.time()) + 3600),
                },
            )
        return httpx.Response(200, json={})

    return httpx.MockTransport(answer)


async def test_one_installation_s_spent_limit_never_holds_another_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An App connection mints for every owner it is installed on, and GitHub limits each
    installation apart: one backoff per client refused every owner's calls for the hour."""
    client = GitHubClient(_app_connection(installations={"spent": 1, "fresh": 2}))

    async def token(installation: int) -> str:
        return f"ghs_{installation}"

    monkeypatch.setattr(client, "_installation_token", token)
    client._transport = _spent_for("spent")  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]

    with pytest.raises(RateLimitExceeded):
        await (await client.api("spent/app")).rest.repos.async_get("spent", "app")

    with pytest.raises(RateLimited):
        await client.api("spent/other")
    assert client.backoff_until("fresh/app") is None
    await (await client.api("fresh/app")).rest.repos.async_get("fresh", "app")

"""The credential probe: each runtime type's cheapest authenticated call, answered with a fixed
sentence and never the credential or the vendor's own words. No network: `httpx.MockTransport`."""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from orchestrator.api.runtimes_probe import probe, probe_e2b
from runtime.contract import AdapterUnavailable

#: A shape no vendor issues, so a leak of it in a detail is this test's and nobody's key.
FAKE = "sk-fake-probe-0000-not-a-key"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _recording(status: int, seen: list[httpx.Request], body: str = "{}") -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, text=body)

    return _client(handler)


@pytest.mark.parametrize(
    ("runtime_type", "name", "url", "headers"),
    [
        (
            "claude_api",
            "ANTHROPIC_API_KEY",
            "https://api.anthropic.com/v1/models",
            {"x-api-key": FAKE, "anthropic-version": "2023-06-01"},
        ),
        (
            "claude_subscription",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "https://api.anthropic.com/v1/models",
            {
                "authorization": f"Bearer {FAKE}",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "oauth-2025-04-20",
            },
        ),
        (
            "codex_api",
            "OPENAI_API_KEY",
            "https://api.openai.com/v1/models",
            {"authorization": f"Bearer {FAKE}"},
        ),
    ],
)
async def test_an_accepted_credential_is_ok_after_one_get_to_the_vendors_model_list(
    runtime_type: str, name: str, url: str, headers: dict[str, str]
) -> None:
    seen: list[httpx.Request] = []
    async with _recording(200, seen) as client:
        result = await probe(runtime_type, {name: SecretStr(FAKE)}, client=client)

    assert result.ok is True
    assert [(r.method, str(r.url)) for r in seen] == [("GET", url)]
    assert {key: seen[0].headers.get(key) for key in headers} == headers
    assert FAKE not in result.detail


@pytest.mark.parametrize(
    ("runtime_type", "name"),
    [
        ("claude_api", "ANTHROPIC_API_KEY"),
        ("claude_subscription", "CLAUDE_CODE_OAUTH_TOKEN"),
        ("codex_api", "OPENAI_API_KEY"),
    ],
)
async def test_a_refused_credential_is_not_ok_and_the_detail_names_the_401(
    runtime_type: str, name: str
) -> None:
    """The vendor's body can echo a key prefix, so it is never what the detail says."""
    seen: list[httpx.Request] = []
    echo = json.dumps({"error": {"message": f"Incorrect API key provided: {FAKE[:12]}****"}})
    async with _recording(401, seen, body=echo) as client:
        result = await probe(runtime_type, {name: SecretStr(FAKE)}, client=client)

    assert result.ok is False
    assert "401" in result.detail
    assert FAKE[:12] not in result.detail
    assert "Incorrect" not in result.detail


@pytest.mark.parametrize(("status", "fragment"), [(403, "403"), (429, "429"), (503, "503")])
async def test_other_answers_are_not_ok_and_name_their_status(status: int, fragment: str) -> None:
    async with _recording(status, []) as client:
        result = await probe("claude_api", {"ANTHROPIC_API_KEY": SecretStr(FAKE)}, client=client)
    assert result.ok is False
    assert fragment in result.detail


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectTimeout(f"timed out sending {FAKE}"),
        httpx.ConnectError(f"refused {FAKE}"),
        # What h11 raises for a key pasted with a newline: its text carries the header value.
        httpx.LocalProtocolError(f"Illegal header value b'{FAKE}\\n'"),
        ValueError(f"bad header {FAKE}"),
    ],
)
async def test_a_call_that_never_answers_says_so_without_the_errors_text(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    async with _client(handler) as client:
        result = await probe("codex_api", {"OPENAI_API_KEY": SecretStr(FAKE)}, client=client)
    assert result.ok is False
    assert FAKE not in result.detail
    assert FAKE not in caplog.text


async def test_a_key_no_header_can_carry_is_named_as_unsendable_not_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.LocalProtocolError(f"Illegal header value b'{FAKE}\\n'")

    async with _client(handler) as client:
        result = await probe("claude_api", {"ANTHROPIC_API_KEY": SecretStr(FAKE)}, client=client)
    assert (result.ok, result.detail) == (
        False,
        "The stored credential could not be sent. Check it for stray characters.",
    )


async def test_a_missing_credential_is_not_ok_and_makes_no_call() -> None:
    seen: list[httpx.Request] = []
    async with _recording(200, seen) as client:
        result = await probe("claude_api", {}, client=client)
    assert result.ok is False
    assert "ANTHROPIC_API_KEY" in result.detail
    assert seen == []


def _login(exp: int | None, *, access: str | None = None) -> SecretStr:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    token = access if access is not None else f"eyJhbGciOiJub25lIn0.{payload}.sig-{FAKE}"
    return SecretStr(json.dumps({"tokens": {"access_token": token, "refresh_token": FAKE}}))


def _no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"the codex subscription check called {request.url}")


async def test_a_codex_subscription_unexpired_is_ok_without_a_network_call() -> None:
    async with _client(_no_network) as client:
        result = await probe(
            "codex_subscription",
            {"CODEX_AUTH_JSON": _login(int(time.time()) + 3600)},
            client=client,
        )
    assert result.ok is True
    assert result.detail.startswith("The stored login is valid until ")
    assert result.detail.endswith(". Factorial did not check it with OpenAI.")
    assert FAKE not in result.detail


async def test_a_codex_subscription_past_its_expiry_is_not_ok() -> None:
    async with _client(_no_network) as client:
        result = await probe(
            "codex_subscription", {"CODEX_AUTH_JSON": _login(1_700_000_000)}, client=client
        )
    assert result.ok is False
    assert result.detail.startswith("The stored login's access token expired at 2023-11-14")
    assert result.detail.endswith(
        ". A run will try to refresh it. If runs fail with 401, save a fresh login."
    )


@pytest.mark.parametrize(
    "stored",
    [
        SecretStr(f"not json {FAKE}"),
        SecretStr(json.dumps({"tokens": {}})),
        _login(None, access=f"opaque-{FAKE}"),
        _login(None),
    ],
)
async def test_a_codex_subscription_that_does_not_parse_says_so(stored: SecretStr) -> None:
    async with _client(_no_network) as client:
        result = await probe("codex_subscription", {"CODEX_AUTH_JSON": stored}, client=client)
    assert (result.ok, result.detail) == (False, "The stored login does not parse.")


class _Sandboxes:
    def __init__(self, error: Exception | None) -> None:
        self.error = error

    async def check(self) -> None:
        if self.error is not None:
            raise self.error


async def test_the_e2b_key_is_read_by_the_clients_own_check() -> None:
    assert (await probe_e2b(_Sandboxes(None))).ok is True
    refused = await probe_e2b(_Sandboxes(AdapterUnavailable(f"401 for key {FAKE}")))
    assert refused.ok is False
    assert FAKE not in refused.detail


async def test_codex_bedrock_mints_and_posts_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "orchestrator.api.runtimes_probe.mint_bearer", lambda **_: "bedrock-api-key-probe"
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "resp", "output": []})

    async with _client(handler) as client:
        result = await probe("codex_bedrock", {}, client=client)

    assert result.ok is True
    assert result.detail == "Bedrock accepted a host-minted bearer."
    assert [(r.method, str(r.url)) for r in seen] == [
        ("POST", "https://bedrock-runtime.us-gov-west-1.amazonaws.com/openai/v1/responses")
    ]
    assert seen[0].headers.get("authorization") == "Bearer bedrock-api-key-probe"
    assert b"us-gov.xai.grok-4.6" in seen[0].content
    assert "bedrock-api-key-probe" not in result.detail


async def test_codex_bedrock_mint_failure_is_local(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator.factorial.bedrock_token import BedrockMintError

    monkeypatch.setattr(
        "orchestrator.api.runtimes_probe.mint_bearer",
        lambda **_: (_ for _ in ()).throw(BedrockMintError("the factory host has no AWS role")),
    )
    async with _client(lambda request: httpx.Response(500)) as client:
        result = await probe("codex_bedrock", {}, client=client)
    assert (result.ok, result.local) == (False, True)
    assert "no AWS role" in result.detail


async def test_codex_bedrock_commercial_posts_to_that_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "orchestrator.api.runtimes_probe.mint_bearer", lambda **_: "bedrock-api-key-probe"
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "resp", "output": []})

    async with _client(handler) as client:
        result = await probe(
            "codex_bedrock",
            {},
            client=client,
            details={"region": "us-east-1", "model": "us.xai.grok-4.6"},
        )

    assert result.ok is True
    assert str(seen[0].url) == (
        "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/responses"
    )
    assert b"us.xai.grok-4.6" in seen[0].content
    assert b"us-gov" not in seen[0].content

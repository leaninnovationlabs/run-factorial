"""The session verdict on an E2B build: an E2B tag the builder assigns once the session
smoke passes, and the refusal an E2B adapter answers for a build without one (decision 25)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fake_e2b import FakeSandboxClient

from runtime.adapters.e2b import ClaudeE2bAdapter
from runtime.adapters.e2b import client as client_module
from runtime.adapters.e2b.client import (
    E2bSandboxClient,
    record_session_verdict,
    session_verdicts,
    template_builds,
    verdict_tag,
    verified_builds,
)
from runtime.contract import AdapterUnavailable, SpawnError, SpawnReason

BUILD = "0f1e2d3c-0000-4000-8000-000000000001"
PIN = f"factorial-claude-2-1-284:{BUILD}"


def adapter_on(tmp_path: Path, client: FakeSandboxClient) -> ClaudeE2bAdapter:
    return ClaudeE2bAdapter(
        template=PIN,
        state_root=tmp_path / "state",
        deployment="test",
        client=client,
        poll_interval=0,
    )


async def test_a_verified_pin_is_cached_and_an_unverified_one_is_asked_again(
    tmp_path: Path,
) -> None:
    """A build's verdict never changes once given, so asking once is enough; a missing one
    may be recorded at any time by `--interactive-smoke`, so it is never remembered."""
    client = FakeSandboxClient()
    adapter = adapter_on(tmp_path, client)

    refusal = await adapter.spawn_refusal()
    assert refusal is not None and PIN in refusal and "--interactive-smoke" in refusal
    assert await adapter.spawn_refusal() is not None
    assert client.verdict_checks == [PIN, PIN]

    client.verdicts.add(PIN)
    assert await adapter.spawn_refusal() is None
    assert await adapter.spawn_refusal() is None
    assert client.verdict_checks == [PIN, PIN, PIN]


def test_a_verdict_is_a_tag_that_names_its_own_build() -> None:
    other = "0f1e2d3c-0000-4000-8000-000000000002"
    tags = [
        {"tag": verdict_tag(BUILD), "buildID": BUILD},
        {"tag": verdict_tag(BUILD), "buildID": other},  # moved onto another build by hand
        {"tag": "default", "buildID": other},
    ]

    assert verified_builds(tags) == frozenset({BUILD})


def _e2b(monkeypatch: pytest.MonkeyPatch, routes: dict[str, httpx.Response]) -> None:
    """E2B's REST API as `routes` answers it: anything else is a 404."""

    def answer(request: httpx.Request) -> httpx.Response:
        return routes.get(request.url.path, httpx.Response(404, json={"message": "not found"}))

    @contextlib.asynccontextmanager
    async def api(api_key: str) -> AsyncIterator[httpx.AsyncClient]:
        transport = httpx.MockTransport(answer)
        async with httpx.AsyncClient(base_url="https://api.e2b.test", transport=transport) as http:
            yield http

    monkeypatch.setattr(client_module, "_e2b_api", api)


async def test_the_listing_and_the_gate_read_the_same_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Runtime page and the worker must agree on a build, or the page offers a session the
    worker refuses."""
    other = "0f1e2d3c-0000-4000-8000-000000000002"
    build = {
        "status": "ready",
        "createdAt": "2026-10-01T09:00:00Z",
        "cpuCount": 2,
        "memoryMB": 4096,
    }
    _e2b(
        monkeypatch,
        {
            "/templates": httpx.Response(
                200, json=[{"templateID": "t1", "names": ["team/factorial-claude-2-1-284"]}]
            ),
            "/templates/t1": httpx.Response(
                200, json={"builds": [build | {"buildID": BUILD}, build | {"buildID": other}]}
            ),
            "/templates/t1/tags": httpx.Response(
                200, json=[{"tag": verdict_tag(BUILD), "buildID": BUILD}]
            ),
            "/templates/factorial-claude-2-1-284/tags": httpx.Response(
                200, json=[{"tag": verdict_tag(BUILD), "buildID": BUILD}]
            ),
        },
    )

    listed = {b.pin: b.verified for b in await template_builds("e2b_test")}

    assert listed == {PIN: True, f"factorial-claude-2-1-284:{other}": False}
    assert await session_verdicts("e2b_test", "factorial-claude-2-1-284") == {BUILD}
    assert await session_verdicts("e2b_test", "a-template-since-deleted") == frozenset()


@pytest.mark.parametrize("status", [403, 503])
async def test_a_listing_whose_tags_fail_still_lists_the_builds_unverified(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """A plan without tags, or a tags call that failed, must not hide every build from the
    page that chooses one."""
    build = {"status": "ready", "createdAt": "2026-10-01T09:00:00Z"}
    _e2b(
        monkeypatch,
        {
            "/templates": httpx.Response(
                200, json=[{"templateID": "t1", "names": ["factorial-claude-2-1-284"]}]
            ),
            "/templates/t1": httpx.Response(
                200, json={"builds": [build | {"buildID": BUILD, "cpuCount": 2, "memoryMB": 4096}]}
            ),
            "/templates/t1/tags": httpx.Response(status, json={"message": "no"}),
        },
    )

    assert [(b.pin, b.verified) for b in await template_builds("e2b_test")] == [(PIN, False)]


async def test_a_tag_e2b_put_on_another_build_is_no_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from e2b import AsyncTemplate

    assigned: list[tuple[str, list[str]]] = []
    landed = {"build_id": BUILD}

    async def assign_tags(target: str, tags: list[str], **opts: object) -> SimpleNamespace:
        assigned.append((target, tags))
        return SimpleNamespace(build_id=landed["build_id"], tags=tags)

    monkeypatch.setattr(AsyncTemplate, "assign_tags", staticmethod(assign_tags))

    await record_session_verdict("e2b_test", PIN)
    assert assigned == [(PIN, [verdict_tag(BUILD)])]

    landed["build_id"] = "0f1e2d3c-0000-4000-8000-000000000002"
    with pytest.raises(RuntimeError, match="not " + BUILD):
        await record_session_verdict("e2b_test", PIN)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("no route to E2B"),
        httpx.HTTPStatusError(
            "busy",
            request=httpx.Request("GET", "https://api.e2b.test"),
            response=httpx.Response(503),
        ),
    ],
)
async def test_e2b_not_answering_is_unavailable_never_a_refusal(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """A refusal escalates the ticket for good; an E2B outage must only delay its spawn."""

    async def verdicts(api_key: str, template: str) -> frozenset[str]:
        raise error

    monkeypatch.setattr(client_module, "session_verdicts", verdicts)

    with pytest.raises(AdapterUnavailable):
        await E2bSandboxClient("e2b_test").session_verified(PIN)


async def test_a_key_e2b_refuses_is_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an operator fixes a key: retried, the ticket would wait for ever."""

    async def verdicts(api_key: str, template: str) -> frozenset[str]:
        raise httpx.HTTPStatusError(
            "unauthorized",
            request=httpx.Request("GET", "https://api.e2b.test"),
            response=httpx.Response(401),
        )

    monkeypatch.setattr(client_module, "session_verdicts", verdicts)
    with pytest.raises(SpawnError) as excinfo:
        await E2bSandboxClient("e2b_test").session_verified(PIN)
    assert excinfo.value.reason is SpawnReason.CREDENTIAL

    adapter = ClaudeE2bAdapter(
        api_key="e2b_test", template=PIN, state_root=tmp_path / "state", deployment="test"
    )
    refusal = await adapter.spawn_refusal()
    assert refusal is not None and "key" in refusal

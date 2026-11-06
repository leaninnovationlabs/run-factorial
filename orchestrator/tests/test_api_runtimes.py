"""The runtime registry's API: CRUD, credentials, the device-auth login, and the two places a
`runtime:` slug is checked against the registry (sync and readiness).

Postgres-backed like `test_api.py`, and reusing its harness. The login tests never spawn
`codex`: the `LoginRunner` seam in `api.deps` is overridden with a fake that prints what the
CLI prints and writes `auth.json` when told to.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import yaml
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api import device_login
from orchestrator.api.deps import (
    config_source_dependency,
    login_runner_dependency,
    login_sessions_dependency,
    probe_http_dependency,
    runtime_adapter_dependency,
)
from orchestrator.api.routes import runtimes as runtimes_route
from orchestrator.api.services.organizations import default_organization_id
from orchestrator.config import ProcessConfig
from orchestrator.db.models import (
    Event,
    ProcessVersion,
    Project,
    Runtime,
    RuntimeCredential,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketSource,
    TicketSourceKind,
    TicketStatus,
)
from orchestrator.events import EventType, append
from orchestrator.factorial import RepoConfigSource
from orchestrator.github import MergeProvenance
from orchestrator.secrets import store
from orchestrator.settings import RuntimeSettings
from runtime.adapters.codex_cli.login import start_device_login
from runtime.adapters.e2b.client import TemplateBuild
from tests.api_harness import build_app, client_for, make_settings, seed_project, seed_ticket
from tests.api_probes import (
    MINI_SHA,
    ProbeContents,
    mini_config_files,
    probe_app,
    seed_probe_project,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.usefixtures("master_key"),
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest.fixture(autouse=True)
async def _no_login_outlives_its_test() -> AsyncIterator[None]:
    """What the API's lifespan does at shutdown, per test: a login left pending would otherwise
    keep its watcher and its scratch home — a fake `auth.json` here, a real one on a host."""
    yield
    from orchestrator.api.device_login import close_logins

    await close_logins()


#: What `codex login --device-auth` prints before it waits: a URL and a one-time code.
DEVICE_PROMPT = (
    "Follow these steps to sign in with your ChatGPT account:\n"
    "1. Open this URL in your browser: https://auth.openai.com/codex/device\n"
    "2. Enter this one-time code: WXYZ-7Q2AB\n"
)
AUTH_JSON = '{"tokens": {"access_token": "eyJ-fake", "refresh_token": "rt-fake"}}'
E2B_TEMPLATE = "factorial-claude:00000000-0000-0000-0000-000000000001"
SENTINEL = "sk-sentinel-never-echo-0000"


def _slug() -> str:
    return f"rt-{uuid4().hex[:10]}"


_REQUIRED_DETAILS: dict[str, dict[str, str]] = {"codex_bedrock": {"region": "us-east-1"}}


async def _create(
    client,
    auth: dict[str, str],
    *,
    type: str,
    slug: str | None = None,
    details: dict[str, str] | None = None,
) -> dict:
    slug = slug or _slug()
    response = await client.post(
        "/api/orgs/default/runtimes",
        headers=auth,
        json={
            "slug": slug,
            "name": f"Runtime {slug}",
            "type": type,
            "details": details or _REQUIRED_DETAILS.get(type, {}),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------------------------------------------------------------ CRUD


async def test_runtimes_require_a_token() -> None:
    async with client_for(build_app(make_settings(), None)) as client:
        assert (await client.get("/api/orgs/default/runtimes")).status_code == 401
        assert (await client.post("/api/orgs/default/runtimes", json={})).status_code == 401


async def test_create_list_get_patch_delete(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    async with client_for(build_app(make_settings(), async_session)) as client:
        created = await _create(client, auth, type="codex_api")
        assert created["details"] == {}
        assert created["metadata"] == {}
        assert created["credential_names"] == []
        assert created["created_at"] and created["updated_at"]

        listed = (await client.get("/api/orgs/default/runtimes", headers=auth)).json()
        assert created["id"] in {row["id"] for row in listed}

        fetched = await client.get(f"/api/runtimes/{created['id']}", headers=auth)
        assert fetched.status_code == 200
        assert fetched.json()["slug"] == created["slug"]

        patched = await client.patch(
            f"/api/runtimes/{created['id']}", headers=auth, json={"name": "Renamed"}
        )
        assert patched.status_code == 200
        assert patched.json()["name"] == "Renamed"
        assert patched.json()["slug"] == created["slug"]

        deleted = await client.delete(f"/api/runtimes/{created['id']}", headers=auth)
        assert deleted.status_code == 204
        assert (await client.get(f"/api/runtimes/{created['id']}", headers=auth)).status_code == 404
        assert (
            await client.delete(f"/api/runtimes/{created['id']}", headers=auth)
        ).status_code == (404)


async def test_a_taken_slug_is_409(auth: dict[str, str], async_session: AsyncSession) -> None:
    async with client_for(build_app(make_settings(), async_session)) as client:
        first = await _create(client, auth, type="codex_api")
        again = await client.post(
            "/api/orgs/default/runtimes",
            headers=auth,
            json={"slug": first["slug"], "name": "Other", "type": "claude_api"},
        )
        assert again.status_code == 409
        assert first["slug"] in again.json()["detail"]


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"slug": "Not-A-Slug", "name": "x", "type": "codex_api"}, "slug"),
        # The schema refuses it, naming the vocabulary — not the details validator a line later.
        ({"slug": "ok-slug", "name": "x", "type": "gemini_api"}, "codex_subscription"),
        (
            {"slug": "ok-slug", "name": "x", "type": "codex_api", "details": {"region": "eu"}},
            "details",
        ),
    ],
)
async def test_bad_slug_type_or_details_is_422(
    auth: dict[str, str], async_session: AsyncSession, body: dict, fragment: str
) -> None:
    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post("/api/orgs/default/runtimes", headers=auth, json=body)
    assert response.status_code == 422
    assert fragment in response.text


@pytest.mark.parametrize("taken", [False, True])
async def test_a_refused_create_leaves_no_runtime_and_no_key(
    auth: dict[str, str], async_session: AsyncSession, master_key: None, taken: bool
) -> None:
    async with client_for(build_app(make_settings(), async_session)) as client:
        slug = (await _create(client, auth, type="claude_api"))["slug"] if taken else _slug()
        before = await _counts(async_session)
        response = await client.post(
            "/api/orgs/default/runtimes",
            headers=auth,
            json={
                "slug": slug,
                "name": "x",
                "type": "claude_api",
                # OPENAI_API_KEY is not read by claude_api; the taken slug refuses the rest.
                "credentials": {("ANTHROPIC_API_KEY" if taken else "OPENAI_API_KEY"): SENTINEL},
            },
        )
        assert response.status_code == (409 if taken else 422), response.text
        assert SENTINEL not in response.text
        assert await _counts(async_session) == before


async def _counts(session: AsyncSession) -> tuple[int, int]:
    runtimes = (await session.execute(select(func.count()).select_from(Runtime))).scalar_one()
    keys = (await session.execute(select(func.count()).select_from(RuntimeCredential))).scalar_one()
    return runtimes, keys


async def test_create_and_a_sandbox_change_follow_the_offered_list(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    settings = make_settings().model_copy(
        update={"runtime": RuntimeSettings(offered=["claude_api@e2b"])}
    )
    async with client_for(build_app(settings, async_session)) as client:
        body = {"slug": _slug(), "name": "x", "type": "claude_api"}
        refused = await client.post("/api/orgs/default/runtimes", headers=auth, json=body)
        assert refused.status_code == 422
        assert "does not offer claude_api on the host" in refused.json()["detail"]
        made = await client.post(
            "/api/orgs/default/runtimes",
            headers=auth,
            json={**body, "details": {"isolation": "e2b"}},
        )
        assert made.status_code == 201, made.text
        moved = await client.patch(
            f"/api/runtimes/{made.json()['id']}",
            headers=auth,
            json={"details": {"isolation": "local_cli"}},
        )
        assert moved.status_code == 422


async def test_check_stores_nothing_and_lists_the_given_keys_templates(
    auth: dict[str, str],
    async_session: AsyncSession,
    master_key: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Team:
        def __init__(self, *_: object, **__: object) -> None: ...

        async def check(self) -> None:
            return None

    async def builds(api_key: str) -> list[TemplateBuild]:
        return [_build(E2B_TEMPLATE, verified=True)]

    monkeypatch.setattr(runtimes_route, "template_builds", builds)
    monkeypatch.setattr(runtimes_route, "E2bSandboxClient", _Team)
    app = build_app(make_settings(), async_session)
    _vendor(app, 200, [])
    async with client_for(app) as client:
        before = await _counts(async_session)
        response = await client.post(
            "/api/orgs/default/runtimes/check",
            headers=auth,
            json={
                "type": "claude_api",
                "details": {"isolation": "e2b"},
                "credentials": {"ANTHROPIC_API_KEY": PROBED_KEY, "E2B_API_KEY": "e2b_x"},
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["model"]["ok"] and body["e2b"]["ok"]
        assert "the given" in body["model"]["detail"]
        assert [t["pin"] for t in body["templates"]] == [E2B_TEMPLATE]
        assert response.headers["cache-control"] == "no-store"
        assert PROBED_KEY not in response.text
        assert await _counts(async_session) == before


@pytest.mark.parametrize("field", ["slug", "type"])
async def test_slug_and_type_are_immutable(
    auth: dict[str, str], async_session: AsyncSession, field: str
) -> None:
    """Repos reference the slug and credentials are shaped by the type: a PATCH naming either is
    refused outright rather than silently ignored."""
    async with client_for(build_app(make_settings(), async_session)) as client:
        created = await _create(client, auth, type="codex_api")
        response = await client.patch(
            f"/api/runtimes/{created['id']}", headers=auth, json={field: "claude_api"}
        )
        assert response.status_code == 422
        assert field in response.text
        unchanged = (await client.get(f"/api/runtimes/{created['id']}", headers=auth)).json()
        assert unchanged[field] == created[field]


async def test_patch_details_are_validated_for_the_row_type(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    async with client_for(build_app(make_settings(), async_session)) as client:
        created = await _create(client, auth, type="claude_api")
        response = await client.patch(
            f"/api/runtimes/{created['id']}", headers=auth, json={"details": {"x": 1}}
        )
        assert response.status_code == 422
        ok = await client.patch(
            f"/api/runtimes/{created['id']}", headers=auth, json={"details": {}}
        )
        assert ok.status_code == 200


async def test_the_template_listing_says_which_builds_passed_the_session_smoke(
    auth: dict[str, str],
    async_session: AsyncSession,
    master_key: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read in the listing's own E2B call, so choosing a build shows its verdict. A PATCH is
    not checked: the worker refuses an unverified build at spawn, and is the authority."""
    verified, unverified = E2B_TEMPLATE, E2B_TEMPLATE[:-1] + "2"
    keys: list[str] = []

    async def builds(api_key: str) -> list[TemplateBuild]:
        keys.append(api_key)
        return [
            _build(verified, verified=True),
            _build(unverified, verified=False),
        ]

    monkeypatch.setattr(runtimes_route, "template_builds", builds)
    async with client_for(build_app(make_settings(), async_session)) as client:
        created = await _create(client, auth, type="claude_api", details={"isolation": "e2b"})
        url = f"/api/runtimes/{created['id']}"
        key_url = f"{url}/credentials/E2B_API_KEY"
        assert (await client.put(key_url, headers=auth, json={"value": "e2b_a"})).status_code == 200

        listed = await client.get(f"{url}/templates", headers=auth)

        assert listed.status_code == 200, listed.text
        assert {b["pin"]: b["verified"] for b in listed.json()} == {
            verified: True,
            unverified: False,
        }
        assert keys == ["e2b_a"]
        chosen = {"isolation": "e2b", "template": unverified}
        stored = await client.patch(url, headers=auth, json={"details": chosen})
        assert stored.status_code == 200, stored.text


def _build(pin: str, *, verified: bool) -> TemplateBuild:
    template, build_id = pin.split(":")
    return TemplateBuild(
        template=template,
        build_id=build_id,
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
        cpu_count=2,
        memory_mb=4096,
        verified=verified,
    )


async def test_metadata_is_created_replaced_whole_and_left_alone_by_a_patch_without_it(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    async with client_for(build_app(make_settings(), async_session)) as client:
        slug = _slug()
        created = await client.post(
            "/api/orgs/default/runtimes",
            headers=auth,
            json={
                "slug": slug,
                "name": "x",
                "type": "codex_api",
                "metadata": {"owner": "platform", "region": "eu"},
            },
        )
        assert created.status_code == 201, created.text
        runtime_id = created.json()["id"]
        assert created.json()["metadata"] == {"owner": "platform", "region": "eu"}

        listed = (await client.get("/api/orgs/default/runtimes", headers=auth)).json()
        assert next(row for row in listed if row["id"] == runtime_id)["metadata"] == {
            "owner": "platform",
            "region": "eu",
        }

        # A PATCH without the key leaves the map alone.
        renamed = await client.patch(
            f"/api/runtimes/{runtime_id}", headers=auth, json={"name": "Renamed"}
        )
        assert renamed.status_code == 200
        assert renamed.json()["metadata"] == {"owner": "platform", "region": "eu"}

        # A PATCH with it REPLACES the map: `region` is gone, not merged.
        replaced = await client.patch(
            f"/api/runtimes/{runtime_id}", headers=auth, json={"metadata": {"owner": "infra"}}
        )
        assert replaced.status_code == 200
        assert replaced.json()["metadata"] == {"owner": "infra"}

        cleared = await client.patch(
            f"/api/runtimes/{runtime_id}", headers=auth, json={"metadata": {}}
        )
        assert cleared.json()["metadata"] == {}


@pytest.mark.parametrize(
    ("metadata", "fragment"),
    [
        ({"has space": "x"}, "'has space'"),
        ({"owner": 1}, "'owner'"),
        ({"owner": "v" * 1025}, "1024"),
        ({f"k{i}": "v" for i in range(33)}, "32"),
    ],
)
async def test_bad_metadata_is_422_on_create_and_on_patch(
    auth: dict[str, str], async_session: AsyncSession, metadata: dict, fragment: str
) -> None:
    async with client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(
            "/api/orgs/default/runtimes",
            headers=auth,
            json={"slug": _slug(), "name": "x", "type": "codex_api", "metadata": metadata},
        )
        assert response.status_code == 422, response.text
        assert response.json()["detail"].startswith("The runtime metadata is invalid: ")
        assert fragment in response.json()["detail"]

        created = await _create(client, auth, type="codex_api")
        response = await client.patch(
            f"/api/runtimes/{created['id']}", headers=auth, json={"metadata": metadata}
        )
        assert response.status_code == 422, response.text
        assert response.json()["detail"].startswith("The runtime metadata is invalid: ")
        unchanged = (await client.get(f"/api/runtimes/{created['id']}", headers=auth)).json()
        assert unchanged["metadata"] == {}


async def _run_on(
    session: AsyncSession, runtime_id: int, status: str = StepRunStatus.RUNNING
) -> StepRun:
    project = await seed_project(session)
    ticket = await seed_ticket(session, project)
    row = StepRun(
        ticket_id=ticket.id,
        step_name="plan",
        kind=StepKind.AGENT,
        attempt=1,
        status=status,
        runtime_run_id="run-1",
        runtime_id=runtime_id,
    )
    session.add(row)
    await session.flush()
    return row


@pytest.mark.parametrize(
    "live", [StepRunStatus.RUNNING, StepRunStatus.WAITING, StepRunStatus.INTERRUPTED]
)
async def test_delete_is_refused_while_a_live_run_references_it(
    auth: dict[str, str], async_session: AsyncSession, live: str
) -> None:
    async with client_for(build_app(make_settings(), async_session)) as client:
        created = await _create(client, auth, type="codex_api")
        await _run_on(async_session, created["id"], status=live)
        response = await client.delete(f"/api/runtimes/{created['id']}", headers=auth)
        assert response.status_code == 409
        assert live in response.json()["detail"]


async def test_a_run_a_restart_abandoned_does_not_hold_its_runtime(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    async with client_for(build_app(make_settings(), async_session)) as client:
        created = await _create(client, auth, type="codex_api")
        run = await _run_on(async_session, created["id"], status=StepRunStatus.INTERRUPTED)
        ticket = await async_session.get(Ticket, run.ticket_id)
        assert ticket is not None
        ticket.generation += 1
        await async_session.flush()
        response = await client.delete(f"/api/runtimes/{created['id']}", headers=auth)
        assert response.status_code == 204


async def test_an_e2b_runtime_does_not_change_team_under_a_live_run(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    """The key names the team, and a paused sandbox resumes only with its own team's key."""
    async with client_for(build_app(make_settings(), async_session)) as client:
        created = await _create(client, auth, type="claude_api", details={"isolation": "e2b"})
        url = f"/api/runtimes/{created['id']}"
        key_url = f"{url}/credentials/E2B_API_KEY"
        choose = {"details": {"isolation": "e2b", "template": E2B_TEMPLATE}}
        # A template is chosen from the team its key names, so never before the key.
        assert (await client.patch(url, headers=auth, json=choose)).status_code == 422
        assert (await client.put(key_url, headers=auth, json={"value": "e2b_a"})).status_code == 200
        assert (await client.patch(url, headers=auth, json=choose)).status_code == 200
        await _run_on(async_session, created["id"], status=StepRunStatus.INTERRUPTED)

        same = await client.put(key_url, headers=auth, json={"value": "e2b_a"})
        assert same.status_code == 200
        other = await client.put(key_url, headers=auth, json={"value": "e2b_b"})
        assert other.status_code == 409
        assert (await client.delete(key_url, headers=auth)).status_code == 409
        # Off E2B, the worker would drop the key the paused sandbox resumes with.
        host = {"details": {"isolation": "local_cli"}}
        assert (await client.patch(url, headers=auth, json=host)).status_code == 409


async def test_a_key_for_another_team_takes_the_old_teams_template_with_it(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    """A template is private to its team: kept, it would read ready and fail at every spawn."""
    async with client_for(build_app(make_settings(), async_session)) as client:
        created = await _create(client, auth, type="claude_api", details={"isolation": "e2b"})
        url = f"/api/runtimes/{created['id']}"
        key_url = f"{url}/credentials/E2B_API_KEY"
        await client.put(key_url, headers=auth, json={"value": "e2b_a"})
        choose = {"details": {"isolation": "e2b", "template": E2B_TEMPLATE}}
        await client.patch(url, headers=auth, json=choose)

        await client.put(key_url, headers=auth, json={"value": "e2b_a"})
        assert (await client.get(url, headers=auth)).json()["details"]["template"] == E2B_TEMPLATE
        await client.put(key_url, headers=auth, json={"value": "e2b_b"})
        assert "template" not in (await client.get(url, headers=auth)).json()["details"]


async def test_delete_keeps_finished_history_with_runtime_id_nulled(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """ON DELETE SET NULL: the audit row outlives the runtime it ran on."""
    async with client_for(build_app(make_settings(), async_session)) as client:
        created = await _create(client, auth, type="codex_api")
        run = await _run_on(async_session, created["id"], status=StepRunStatus.SUCCEEDED)
        response = await client.delete(f"/api/runtimes/{created['id']}", headers=auth)
        assert response.status_code == 204
    await async_session.refresh(run)
    assert run.runtime_id is None


# ----------------------------------------------------------------------------- credentials


async def test_credentials_are_write_only(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    value = "sk-proj-never-echoed"
    async with client_for(build_app(make_settings(), async_session)) as client:
        created = await _create(client, auth, type="codex_api")
        rid = created["id"]

        stored = await client.put(
            f"/api/runtimes/{rid}/credentials/OPENAI_API_KEY", headers=auth, json={"value": value}
        )
        assert stored.status_code == 200
        assert stored.json() == {"name": "OPENAI_API_KEY"}
        assert value not in stored.text

        listed = await client.get(f"/api/runtimes/{rid}/credentials", headers=auth)
        assert listed.json() == {"names": ["OPENAI_API_KEY"]}
        assert value not in listed.text

        detail = await client.get(f"/api/runtimes/{rid}", headers=auth)
        assert detail.json()["credential_names"] == ["OPENAI_API_KEY"]
        assert value not in detail.text

        row = (
            await async_session.execute(
                select(RuntimeCredential).where(RuntimeCredential.runtime_id == rid)
            )
        ).scalar_one()
        assert value.encode() not in row.value_ciphertext
        stored = await store.get_for_runtime(async_session, rid, "OPENAI_API_KEY")
        assert stored is not None and stored.get_secret_value() == value

        # The page shows a stored credential by name and age, nothing more: the row's own
        # timestamps, and no key the value could hide behind.
        assert detail.json()["credentials"] == [
            {
                "name": "OPENAI_API_KEY",
                "created_at": row.created_at.isoformat().replace("+00:00", "Z"),
                "updated_at": row.updated_at.isoformat().replace("+00:00", "Z"),
            }
        ]
        listed = (await client.get("/api/orgs/default/runtimes", headers=auth)).json()
        assert [r["credentials"] for r in listed if r["id"] == rid] == [
            detail.json()["credentials"]
        ]
        assert value not in repr(listed)

        removed = await client.delete(
            f"/api/runtimes/{rid}/credentials/OPENAI_API_KEY", headers=auth
        )
        assert removed.status_code == 204
        again = await client.delete(f"/api/runtimes/{rid}/credentials/OPENAI_API_KEY", headers=auth)
        assert again.status_code == 404
        assert (await client.get(f"/api/runtimes/{rid}", headers=auth)).json()[
            "credential_names"
        ] == []


async def test_replacing_a_credential_keeps_created_at_and_moves_updated_at(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    """A replace is the same row rotated: the trigger (revision 0011) moves `updated_at`, the
    page reads both, and the old value is gone without ever having been shown."""
    async with client_for(build_app(make_settings(), async_session)) as client:
        created = await _create(client, auth, type="claude_api")
        rid = created["id"]
        url = f"/api/runtimes/{rid}/credentials/ANTHROPIC_API_KEY"

        assert (await client.put(url, headers=auth, json={"value": "first"})).status_code == 200
        [before] = (await client.get(f"/api/runtimes/{rid}", headers=auth)).json()["credentials"]
        # now() is transaction-start time; the replace must run in a second transaction.
        await async_session.commit()
        assert (await client.put(url, headers=auth, json={"value": "second"})).status_code == 200
        [after] = (await client.get(f"/api/runtimes/{rid}", headers=auth)).json()["credentials"]

        assert after["name"] == before["name"] == "ANTHROPIC_API_KEY"
        assert after["created_at"] == before["created_at"]
        assert after["updated_at"] > before["updated_at"]
        assert after["updated_at"] != after["created_at"]
        row = (
            await async_session.execute(
                select(RuntimeCredential).where(RuntimeCredential.runtime_id == rid)
            )
        ).scalar_one()
        assert after["updated_at"] == row.updated_at.isoformat().replace("+00:00", "Z")
        stored = await store.get_for_runtime(async_session, rid, "ANTHROPIC_API_KEY")
        assert stored is not None and stored.get_secret_value() == "second"


async def test_a_credential_name_must_be_env_var_shaped(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    async with client_for(build_app(make_settings(), async_session)) as client:
        created = await _create(client, auth, type="codex_api")
        response = await client.put(
            f"/api/runtimes/{created['id']}/credentials/not-a-name",
            headers=auth,
            json={"value": "x"},
        )
        assert response.status_code == 422
        empty = await client.put(
            f"/api/runtimes/{created['id']}/credentials/OK_NAME", headers=auth, json={"value": ""}
        )
        assert empty.status_code == 422


# ----------------------------------------------------------------------------------- login


class FakeLogin:
    """A `codex login --device-auth` child: prints `output`, then exits when the test says."""

    def __init__(self, output: str = DEVICE_PROMPT, *, exit_code: int | None = None) -> None:
        self.output = output
        self.returncode = exit_code
        self.home: Path | None = None
        self.killed = False

    async def start(self, home: Path) -> FakeLogin:
        self.home = home
        return self

    def finish(self, code: int, *, auth_json: str | None = None) -> None:
        assert self.home is not None
        if auth_json is not None:
            (self.home / "auth.json").write_text(auth_json)
        self.returncode = code

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


#: Byte-for-byte what codex-cli 0.153.2 prints (captured 2026-09-08), colour codes included: the
#: URL and the code each sit inside `\x1b[94m … \x1b[0m`. A prompt fixture without the escapes
#: passes a parser the real CLI defeats twice over — `[^\s]+` swallows the reset into the URL,
#: and `\b` finds no boundary between the `m` of the escape and the first code character.
REAL_DEVICE_PROMPT = (
    "Follow these steps to sign in with ChatGPT using device code authoriza"
    "tion:\n\n1. Open this link in your browser and sign in to your account\n "
    "  \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n\n2. Enter this one-tim"
    "e code \x1b[90m(expires in 15 minutes)\x1b[0m\n   \x1b[94m2JEO-AMB2U\x1b[0m\n"
)


async def test_the_device_prompt_parses_through_the_clis_colour_codes(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    # Exits at once, so a parser miss is a fast 502 rather than the 30-second prompt timeout.
    child = FakeLogin(REAL_DEVICE_PROMPT, exit_code=0)
    async with client_for(_login_app(async_session, child)) as client:
        created = await _create(client, auth, type="codex_subscription")
        started = await client.post(f"/api/runtimes/{created['id']}/login", headers=auth)
        assert started.status_code == 202, started.text
        assert started.json()["verification_url"] == "https://auth.openai.com/codex/device"
        assert started.json()["user_code"] == "2JEO-AMB2U"


def _login_app(session: AsyncSession, child: FakeLogin) -> FastAPI:
    app = build_app(make_settings(), session)
    app.dependency_overrides[login_runner_dependency] = lambda: child.start

    # The watcher settles OUTSIDE any request, so it opens its own session; in tests that is
    # the one shared session, or its writes would be invisible to the assertions.
    @asynccontextmanager
    async def _shared() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[login_sessions_dependency] = lambda: _shared
    return app


async def _settled(
    client, auth: dict[str, str], rid: int, session_id: str, within: float = 4.0
) -> dict:
    """Poll until the watcher has settled the login. The poll is read-only, so a test that
    asserted right after `finish()` would only ever see `pending`."""
    deadline = asyncio.get_running_loop().time() + within
    while True:
        body = (await client.get(f"/api/runtimes/{rid}/login/{session_id}", headers=auth)).json()
        if body["status"] != "pending" or asyncio.get_running_loop().time() >= deadline:
            return body
        await asyncio.sleep(0.1)


async def test_a_completed_login_is_stored_without_any_poll(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    """The user authorises in the browser and closes the tab. Nobody polls. The credential
    must still land in the store and the scratch home — holding a real login — must still go:
    a lifecycle that waits for a client to observe it leaves `auth.json` in /tmp for ever."""
    child = FakeLogin()
    async with client_for(_login_app(async_session, child)) as client:
        created = await _create(client, auth, type="codex_subscription")
        rid = created["id"]
        started = await client.post(f"/api/runtimes/{rid}/login", headers=auth)
        assert started.status_code == 202, started.text
        home = child.home
        assert home is not None
        child.finish(0, auth_json=AUTH_JSON)
        await asyncio.sleep(1.2)  # two watcher ticks; no GET in between

        assert not home.exists(), "the plaintext login must not outlive its storing"
        names = (await client.get(f"/api/runtimes/{rid}", headers=auth)).json()["credential_names"]
        assert "CODEX_AUTH_JSON" in names


async def test_an_abandoned_login_expires_and_is_cleaned_without_any_poll(
    auth: dict[str, str], async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(device_login, "_LOGIN_TTL", timedelta(seconds=0.6))
    child = FakeLogin()
    async with client_for(_login_app(async_session, child)) as client:
        created = await _create(client, auth, type="codex_subscription")
        started = await client.post(f"/api/runtimes/{created['id']}/login", headers=auth)
        assert started.status_code == 202, started.text
        home = child.home
        await asyncio.sleep(1.5)

        assert child.killed, "an expired login's child must not keep running"
        assert home is not None and not home.exists()


async def test_shutdown_kills_pending_logins_and_removes_their_homes(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    from orchestrator.api.device_login import close_logins

    child = FakeLogin()
    async with client_for(_login_app(async_session, child)) as client:
        created = await _create(client, auth, type="codex_subscription")
        started = await client.post(f"/api/runtimes/{created['id']}/login", headers=auth)
        assert started.status_code == 202, started.text
        home = child.home
        await close_logins()
        assert child.killed
        assert home is not None and not home.exists()


async def test_device_login_stores_auth_json_as_the_subscription_credential(
    auth: dict[str, str], async_session: AsyncSession, master_key: None, caplog
) -> None:
    child = FakeLogin()
    caplog.set_level(logging.DEBUG)
    async with client_for(_login_app(async_session, child)) as client:
        created = await _create(client, auth, type="codex_subscription")
        rid = created["id"]

        started = await client.post(f"/api/runtimes/{rid}/login", headers=auth)
        assert started.status_code == 202, started.text
        body = started.json()
        assert body["verification_url"] == "https://auth.openai.com/codex/device"
        assert body["user_code"] == "WXYZ-7Q2AB"
        expires = datetime.fromisoformat(body["expires_at"])
        assert timedelta(minutes=14) < expires - datetime.now(UTC) <= timedelta(minutes=15)
        assert child.home is not None and child.home.is_dir()

        pending = await client.get(f"/api/runtimes/{rid}/login/{body['session_id']}", headers=auth)
        assert pending.json()["status"] == "pending"

        child.finish(0, auth_json=AUTH_JSON)
        done = await _settled(client, auth, rid, body["session_id"])
        assert done == {
            "session_id": body["session_id"],
            "status": "complete",
            "detail": None,
        }
        assert AUTH_JSON not in repr(done)

        detail = (await client.get(f"/api/runtimes/{rid}", headers=auth)).json()
        assert detail["credential_names"] == ["CODEX_AUTH_JSON"]
        # The watcher's write is a store row like any other: the page shows it the same way.
        [bundle] = detail["credentials"]
        assert bundle["name"] == "CODEX_AUTH_JSON"
        assert datetime.fromisoformat(bundle["created_at"]) <= datetime.now(UTC)
        assert bundle["updated_at"] == bundle["created_at"]

    stored = await store.get_for_runtime(async_session, rid, "CODEX_AUTH_JSON")
    assert stored is not None and stored.get_secret_value() == AUTH_JSON
    assert not child.home.exists()  # the tempdir holding the plaintext login is gone
    # The one-time code goes to the client and nowhere else.
    assert "WXYZ-7Q2AB" not in caplog.text


async def test_device_login_failure_and_exit_without_auth_json(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    async with client_for(_login_app(async_session, child := FakeLogin())) as client:
        created = await _create(client, auth, type="codex_subscription")
        rid = created["id"]
        session_id = (await client.post(f"/api/runtimes/{rid}/login", headers=auth)).json()[
            "session_id"
        ]
        child.output += "error: device code was denied\n"
        child.finish(1)
        failed = await _settled(client, auth, rid, session_id)
        assert failed["status"] == "failed"
        assert "denied" in failed["detail"]
        assert child.home is not None and not child.home.exists()

        # Terminal states stay put; nothing is re-read.
        again = await _settled(client, auth, rid, session_id)
        assert again["status"] == "failed"

    async with client_for(_login_app(async_session, child := FakeLogin())) as client:
        session_id = (await client.post(f"/api/runtimes/{rid}/login", headers=auth)).json()[
            "session_id"
        ]
        child.finish(0)  # exit 0 but no auth.json written
        failed = await _settled(client, auth, rid, session_id)
        assert failed["status"] == "failed"
        assert "auth.json" in failed["detail"]
    assert await store.get_for_runtime(async_session, rid, "CODEX_AUTH_JSON") is None


async def test_device_login_expires_and_kills_the_child(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    child = FakeLogin()
    async with client_for(_login_app(async_session, child)) as client:
        created = await _create(client, auth, type="codex_subscription")
        rid = created["id"]
        session_id = (await client.post(f"/api/runtimes/{rid}/login", headers=auth)).json()[
            "session_id"
        ]
        device_login._LOGINS[session_id].expires_at = datetime.now(UTC) - timedelta(seconds=1)
        expired = await _settled(client, auth, rid, session_id)
        assert expired["status"] == "expired"
        assert child.killed
        assert child.home is not None and not child.home.exists()

        # A settled session past its expiry is forgotten the next time a login starts.
        assert session_id in device_login._LOGINS
        assert (await client.post(f"/api/runtimes/{rid}/login", headers=auth)).status_code == 202
        assert session_id not in device_login._LOGINS


async def test_device_login_without_a_prompt_is_a_bad_gateway(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The CLI exited before printing a URL and a code: nothing to hand the client."""
    child = FakeLogin("codex: unknown flag --device-auth\n", exit_code=2)
    async with client_for(_login_app(async_session, child)) as client:
        created = await _create(client, auth, type="codex_subscription")
        response = await client.post(f"/api/runtimes/{created['id']}/login", headers=auth)
        assert response.status_code == 502
        assert "unknown flag" in response.json()["detail"]
    assert child.home is not None and not child.home.exists()
    assert not any(s.runtime_id == created["id"] for s in device_login._LOGINS.values())


async def test_a_codex_binary_that_cannot_run_is_service_unavailable(
    auth: dict[str, str], async_session: AsyncSession, tmp_path: Path
) -> None:
    """The adapter refuses in its own terms (`AdapterUnavailable`); the API answers 503 with its
    reason, never a 500."""
    missing = str(tmp_path / "no-codex-here")
    app = build_app(make_settings(), async_session)
    app.dependency_overrides[login_runner_dependency] = lambda: partial(start_device_login, missing)
    async with client_for(app) as client:
        created = await _create(client, auth, type="codex_subscription")
        response = await client.post(f"/api/runtimes/{created['id']}/login", headers=auth)
    assert response.status_code == 503, response.text
    assert f"cannot execute `{missing}`" in response.json()["detail"]


async def test_codex_bedrock_is_created_without_a_stored_key_and_refuses_e2b(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    async with client_for(build_app(make_settings(), async_session)) as client:
        created = await _create(client, auth, type="codex_bedrock")
        assert created["type"] == "codex_bedrock"
        assert created["credential_names"] == []
        refused = await client.post(
            "/api/orgs/default/runtimes",
            headers=auth,
            json={
                "slug": _slug(),
                "name": "bedrock-e2b",
                "type": "codex_bedrock",
                "details": {"isolation": "e2b", "region": "us-east-1"},
            },
        )
        assert refused.status_code == 422
        assert "E2B" in refused.json()["detail"]


async def test_codex_bedrock_refuses_a_stored_bearer(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    async with client_for(build_app(make_settings(), async_session)) as client:
        created = await _create(client, auth, type="codex_bedrock")
        refused = await client.put(
            f"/api/runtimes/{created['id']}/credentials/AWS_BEARER_TOKEN_BEDROCK",
            headers=auth,
            json={"value": "bedrock-api-key-must-not-be-stored"},
        )
        assert refused.status_code == 409
        assert "never stored" in refused.json()["detail"]
        fetched = (await client.get(f"/api/runtimes/{created['id']}", headers=auth)).json()
        assert fetched["credential_names"] == []


async def test_login_is_refused_for_api_types_and_for_claude_which_has_no_device_flow(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """Both 409: the runtime exists and is fine, it is just not connected this way. The Claude
    answer names the whole fix — `claude setup-token` on the operator's machine, pasted under
    the type's credential name — because there is no server-side flow to point at."""
    async with client_for(_login_app(async_session, FakeLogin())) as client:
        api = await _create(client, auth, type="codex_api")
        assert (
            await client.post(f"/api/runtimes/{api['id']}/login", headers=auth)
        ).status_code == (409)
        bedrock = await _create(client, auth, type="codex_bedrock")
        refused_bedrock = await client.post(
            f"/api/runtimes/{bedrock['id']}/login", headers=auth
        )
        assert refused_bedrock.status_code == 409
        assert "does not use device sign-in" in refused_bedrock.json()["detail"]
        claude = await _create(client, auth, type="claude_subscription")
        refused = await client.post(f"/api/runtimes/{claude['id']}/login", headers=auth)
        assert refused.status_code == 409
        detail = refused.json()["detail"]
        assert "claude setup-token" in detail
        assert "`CLAUDE_CODE_OAUTH_TOKEN` on this runtime's page" in detail
        assert not any(s.runtime_id == claude["id"] for s in device_login._LOGINS.values())
        assert (await client.post("/api/runtimes/999999/login", headers=auth)).status_code == 404


async def test_login_is_refused_for_a_type_in_the_not_implemented_set(
    auth: dict[str, str], async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The set is empty today; filled here so the 501 keeps its shape for the next type."""
    monkeypatch.setattr(runtimes_route, "NOT_IMPLEMENTED_TYPES", frozenset({"codex_subscription"}))
    async with client_for(_login_app(async_session, FakeLogin())) as client:
        codex = await _create(client, auth, type="codex_subscription")
        refused = await client.post(f"/api/runtimes/{codex['id']}/login", headers=auth)
        assert refused.status_code == 501
        assert "not implemented" in refused.json()["detail"]


async def test_a_login_session_belongs_to_its_runtime(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    async with client_for(_login_app(async_session, FakeLogin())) as client:
        mine = await _create(client, auth, type="codex_subscription")
        other = await _create(client, auth, type="codex_subscription")
        session_id = (await client.post(f"/api/runtimes/{mine['id']}/login", headers=auth)).json()[
            "session_id"
        ]
        wrong = await client.get(f"/api/runtimes/{other['id']}/login/{session_id}", headers=auth)
        assert wrong.status_code == 404
        unknown = await client.get(f"/api/runtimes/{mine['id']}/login/{uuid4()}", headers=auth)
        assert unknown.status_code == 404


# ----------------------------------------------------------------------------------- sync


def _process_yaml(runtime: str) -> str:
    return f"""
specVersion: factorial/v1
kind: Process
name: mini
version: 1
runtime: {runtime}
entry:
  when: ticket_label("factorial:new")
  step: plan
steps:
  - name: plan
    kind: agent
    agent: mini-agent
    next: done
    workspace:
      repo: "{{repo}}"
      base: main
      branch: spec/{{ticket}}-{{slug}}
terminal:
  step: done
"""


def _files(runtime: str, *, manifest_kind: str) -> dict[str, str]:
    files = mini_config_files([]) | {
        ".factorial/processes/process-mini.yaml": _process_yaml(runtime)
    }
    files[".factorial/agents/mini-agent/agent.yaml"] = files[
        ".factorial/agents/mini-agent/agent.yaml"
    ].replace(
        "runtime:\n  kind: claude\n",
        f"runtime:\n  kind: {manifest_kind}\n",
    )
    return files


def _sync_app(session: AsyncSession, files: dict[str, str]) -> FastAPI:
    app = build_app(make_settings(), session)
    source = RepoConfigSource(ProbeContents(files))
    app.dependency_overrides[config_source_dependency] = lambda: source
    return app


async def test_sync_refuses_a_runtime_slug_nobody_created(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    async with client_for(
        _sync_app(async_session, _files("ghost-rt", manifest_kind="codex"))
    ) as client:
        response = await client.post(
            f"/api/projects/{project.id}/process-versions/sync", headers=auth, json={}
        )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "'ghost-rt'" in detail
    assert "Runtimes page" in detail
    assert response.json()["code"] == "runtime_missing"
    assert response.json()["missing_runtimes"] == ["ghost-rt"]
    versions = (
        await async_session.execute(
            select(ProcessVersion).where(ProcessVersion.project_id == project.id)
        )
    ).all()
    assert versions == []


async def test_sync_at_a_head_this_install_did_not_write_answers_unknown_head(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    contents = ProbeContents(_files("ghost-rt", manifest_kind="codex"))
    contents.provenance = MergeProvenance(merged_pr=None, merged_by=None, merged_by_human=False)
    app = build_app(make_settings(), async_session)
    app.dependency_overrides[config_source_dependency] = lambda: RepoConfigSource(contents)
    async with client_for(app) as client:
        response = await client.post(
            f"/api/projects/{project.id}/process-versions/sync", headers=auth, json={}
        )
    assert response.status_code == 422
    assert response.json()["code"] == "unknown_head"


async def test_sync_accepts_a_runtime_whose_family_matches(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    slug = _slug()
    async with client_for(_sync_app(async_session, _files(slug, manifest_kind="codex"))) as client:
        await _create(client, auth, type="codex_subscription", slug=slug)
        response = await client.post(
            f"/api/projects/{project.id}/process-versions/sync", headers=auth, json={}
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["agent_warnings"] == []
        assert [(r["process"], r["outcome"]) for r in body["results"]] == [("mini", "created")]
        version_id = body["results"][0]["version_id"]
        snapshot = (
            await client.get(
                f"/api/projects/{project.id}/process-versions/{version_id}", headers=auth
            )
        ).json()
        assert snapshot["content"]["runtime"] == slug


# ------------------------------------------------------------------------------- readiness


async def _seed_with_runtime(session: AsyncSession, slug: str):
    return await seed_probe_project(session, process_yaml=_process_yaml(slug))


async def _readiness(client, auth: dict[str, str], project_id: int) -> tuple[list[str], dict]:
    detail = (await client.get(f"/api/projects/{project_id}", headers=auth)).json()
    rows = {row["key"]: row for row in detail["readiness"]}
    return detail["blockers"], rows


async def test_a_runtime_that_no_longer_exists_blocks_its_lane_not_the_project(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """A lane's problems stay inside the lane (design.md §4). Blurring several lanes into one
    verdict would either hide a broken lane behind two working ones, or block a whole project
    over a lane nobody is using — so the row is `blocked`, names its lane, and activation
    proceeds."""
    project = await _seed_with_runtime(async_session, "vanished")
    async with client_for(probe_app(async_session, files=mini_config_files([]))) as client:
        blockers, rows = await _readiness(client, auth, project.id)
        assert rows["runtime"]["state"] == "blocked"
        assert rows["runtime"]["process"] == "mini"
        assert "mini" in rows["runtime"]["detail"]
        assert blockers == []
        activated = await client.put(
            f"/api/projects/{project.id}/active", headers=auth, json={"active": True}
        )
        assert activated.status_code == 200


async def test_readiness_blocks_on_a_disconnected_subscription_of_either_family(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    slug = _slug()
    project = await _seed_with_runtime(async_session, slug)
    async with client_for(probe_app(async_session, files=mini_config_files([]))) as client:
        claude = await _create(client, auth, type="claude_subscription", slug=slug)
        _, rows = await _readiness(client, auth, project.id)
        assert rows["runtime"]["state"] == "blocked"
        assert "CLAUDE_CODE_OAUTH_TOKEN" in rows["runtime"]["detail"]

        # Same slug, a codex subscription instead: blocked until its login is stored.
        assert (
            await client.delete(f"/api/runtimes/{claude['id']}", headers=auth)
        ).status_code == 204
        codex = await _create(client, auth, type="codex_subscription", slug=slug)
        _, rows = await _readiness(client, auth, project.id)
        assert rows["runtime"]["state"] == "blocked"
        assert "CODEX_AUTH_JSON" in rows["runtime"]["detail"]

        await client.put(
            f"/api/runtimes/{codex['id']}/credentials/CODEX_AUTH_JSON",
            headers=auth,
            json={"value": AUTH_JSON},
        )
        blockers, rows = await _readiness(client, auth, project.id)
        assert blockers == []
        assert rows["runtime"]["state"] == "ok"
        assert slug in rows["runtime"]["summary"]


async def test_readiness_blocks_on_an_api_key_runtime_without_its_key(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    """A bound runtime is the only source of its vendor key (decision 36): a project-stored copy
    is never read, so a runtime row holding no key blocks its lane the way a disconnected
    subscription does — every spawn in that lane would refuse."""
    slug = _slug()
    project = await _seed_with_runtime(async_session, slug)
    async with client_for(probe_app(async_session, files=mini_config_files([]))) as client:
        codex = await _create(client, auth, type="codex_api", slug=slug)
        await client.put(
            f"/api/projects/{project.id}/secrets/OPENAI_API_KEY",
            headers=auth,
            json={"value": "sk-project-copy"},
        )
        blockers, rows = await _readiness(client, auth, project.id)
        assert rows["runtime"]["state"] == "blocked"
        assert "OPENAI_API_KEY" in rows["runtime"]["detail"]
        assert "Runtime page" in rows["runtime"]["detail"]
        assert "optional" not in rows["runtime"]["detail"]
        assert blockers == []  # a lane's problems stay inside the lane (design.md §4)

        # A stray name is not the key: exact-name, as the subscription check already is.
        await client.put(
            f"/api/runtimes/{codex['id']}/credentials/OTHER", headers=auth, json={"value": "x"}
        )
        blockers, rows = await _readiness(client, auth, project.id)
        assert rows["runtime"]["state"] == "blocked"

        await client.put(
            f"/api/runtimes/{codex['id']}/credentials/OPENAI_API_KEY",
            headers=auth,
            json={"value": "sk-fake"},
        )
        blockers, rows = await _readiness(client, auth, project.id)
        assert blockers == []
        assert rows["runtime"]["state"] == "ok"
        assert rows["runtime"]["detail"] is None


async def test_readiness_treats_codex_bedrock_as_ready_without_a_stored_key(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    slug = _slug()
    project = await _seed_with_runtime(async_session, slug)
    async with client_for(probe_app(async_session, files=mini_config_files([]))) as client:
        await _create(client, auth, type="codex_bedrock", slug=slug)
        blockers, rows = await _readiness(client, auth, project.id)
        assert blockers == []
        assert rows["runtime"]["state"] == "ok"
        assert rows["runtime"]["summary"].endswith("host role")
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        assert detail["runtime_provided_secrets"] == [
            {
                "name": "AWS_BEARER_TOKEN_BEDROCK",
                "runtime_slug": slug,
                "stored": True,
                "open_tickets_reading_copy": 0,
            }
        ]


async def test_readiness_blocks_on_an_e2b_runtime_the_project_does_not_permit(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    """Every spawn on an E2B runtime the project's `runtime_allowlist` omits is refused (decision
    25). Reading `ok` there sent widgets#47 to a refusal the page never mentioned."""
    slug = _slug()
    project = await _seed_with_runtime(async_session, slug)
    async with client_for(probe_app(async_session, files=mini_config_files([]))) as client:
        runtime = await _create(
            client, auth, type="claude_api", slug=slug, details={"isolation": "e2b"}
        )
        for name in ("ANTHROPIC_API_KEY", "E2B_API_KEY"):
            await client.put(
                f"/api/runtimes/{runtime['id']}/credentials/{name}",
                headers=auth,
                json={"value": "sk-fake"},
            )
        await client.patch(
            f"/api/runtimes/{runtime['id']}",
            headers=auth,
            json={"details": {"isolation": "e2b", "template": E2B_TEMPLATE}},
        )
        blockers, rows = await _readiness(client, auth, project.id)
        assert rows["runtime"]["state"] == "blocked"
        assert "permit it on this project's Runtime page" in rows["runtime"]["detail"]
        assert blockers == []

        await client.patch(
            f"/api/projects/{project.id}",
            headers=auth,
            json={"settings": {"runtime_allowlist": [slug]}},
        )
        _, rows = await _readiness(client, auth, project.id)
        assert rows["runtime"]["state"] == "ok"


async def test_permit_changes_apply_to_the_settings_as_they_stand(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """Each change lands on the current settings, so a later change, or an Undo of an earlier
    one, never drops what came between: the page used to send a whole cached settings object."""
    project = await _seed_with_runtime(async_session, _slug())
    async with client_for(probe_app(async_session, files=mini_config_files([]))) as client:
        url = f"/api/projects/{project.id}/permits"
        await client.patch(url, headers=auth, json={"runtime_allowlist": {"add": ["a-on-e2b"]}})
        await client.patch(
            url, headers=auth, json={"egress_allowlist": {"add": ["Registry.NPMjs.org"]}}
        )
        undo = await client.patch(
            url, headers=auth, json={"runtime_allowlist": {"remove": ["a-on-e2b"]}}
        )
        assert undo.json()["settings"]["runtime_allowlist"] == []
        assert undo.json()["settings"]["egress_allowlist"] == ["registry.npmjs.org"]

        refused = await client.patch(url, headers=auth, json={"egress_allowlist": {"add": ["*"]}})
        assert refused.status_code == 422


async def test_readiness_reports_the_vendor_key_as_provided_by_the_runtime(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    """A manifest may keep declaring the vendor key — the declaration is inert once the process
    names a runtime. The secrets probe discards that name exactly as it discards `GH_TOKEN`, and
    the detail says which runtime provides it and whether that runtime holds it, so the page can
    label the row instead of asking for a project secret nothing would read."""
    slug = _slug()
    project = await _seed_with_runtime(async_session, slug)
    files = mini_config_files(["OPENAI_API_KEY", "PRODUCT_DOCS_TOKEN", "GH_TOKEN"])
    async with client_for(probe_app(async_session, files=files)) as client:
        codex = await _create(client, auth, type="codex_api", slug=slug)
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        rows = {row["key"]: row for row in detail["readiness"]}
        assert detail["declared_secrets"] == ["GH_TOKEN", "OPENAI_API_KEY", "PRODUCT_DOCS_TOKEN"]
        assert detail["runtime_provided_secrets"] == [
            {
                "name": "OPENAI_API_KEY",
                "runtime_slug": slug,
                "stored": False,
                "open_tickets_reading_copy": 0,
            }
        ]
        assert rows["secrets"]["state"] == "blocked"
        assert "PRODUCT_DOCS_TOKEN" in rows["secrets"]["detail"]
        assert "OPENAI_API_KEY" not in rows["secrets"]["detail"]

        await client.put(
            f"/api/runtimes/{codex['id']}/credentials/OPENAI_API_KEY",
            headers=auth,
            json={"value": "sk-fake"},
        )
        await client.put(
            f"/api/projects/{project.id}/secrets/PRODUCT_DOCS_TOKEN",
            headers=auth,
            json={"value": "tok"},
        )
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        assert detail["blockers"] == []
        assert detail["runtime_provided_secrets"] == [
            {
                "name": "OPENAI_API_KEY",
                "runtime_slug": slug,
                "stored": True,
                "open_tickets_reading_copy": 0,
            }
        ]


async def _delivery_lane(
    client, auth: dict[str, str], project_id: int, slug: str, *, sandbox: bool
) -> None:
    await _create(
        client, auth, type="claude_api", slug=slug, details={"isolation": "e2b"} if sandbox else {}
    )
    await client.patch(
        f"/api/projects/{project_id}",
        headers=auth,
        json={"settings": {"egress_allowlist": ["backboard.railway.com"]}},
    )


async def test_a_sandbox_lane_supplies_every_stored_secret_and_never_warns(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    """A stored secret always reaches the run, as an environment variable unless the operator
    chose a header (decision 75), so there is nothing left undelivered to warn about."""
    slug = _slug()
    project = await _seed_with_runtime(async_session, slug)
    files = mini_config_files(["RAILWAY_TOKEN_X", "GH_TOKEN"])
    async with client_for(probe_app(async_session, files=files)) as client:
        await _delivery_lane(client, auth, project.id, slug, sandbox=True)
        url = f"/api/projects/{project.id}/secrets/RAILWAY_TOKEN_X"
        await client.put(url, headers=auth, json={"value": "rw"})
        blockers, rows = await _readiness(client, auth, project.id)
        assert rows["secrets"]["state"] == "ok"
        assert blockers == []
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        assert "undelivered_secrets" not in detail
        assert detail["secret_deliveries"] == [
            {"name": "RAILWAY_TOKEN_X", "mode": "env", "host": None, "header": None}
        ]


async def test_a_missing_secret_still_blocks_a_sandbox_lane(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    slug = _slug()
    project = await _seed_with_runtime(async_session, slug)
    files = mini_config_files(["RAILWAY_TOKEN_X", "GH_TOKEN"])
    async with client_for(probe_app(async_session, files=files)) as client:
        await _delivery_lane(client, auth, project.id, slug, sandbox=True)
        _, rows = await _readiness(client, auth, project.id)
        assert rows["secrets"]["state"] == "blocked"


async def test_the_runtime_provided_row_counts_open_tickets_whose_snapshot_still_reads_the_copy(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    """Config is pinned per ticket: a ticket admitted before the process named the runtime keeps
    reading the vendor key from the project store on every later spawn and resume. The row says
    how many open tickets still do, so a Remove — or the migration script — can hold off."""
    slug = _slug()
    project = await seed_probe_project(async_session)  # v1 names no runtime
    unbound = (
        await async_session.execute(
            select(ProcessVersion).where(ProcessVersion.project_id == project.id)
        )
    ).scalar_one()
    bound = ProcessVersion(
        project_id=project.id,
        source_repo=project.github_repo,
        process_key=unbound.process_key,  # the same lane, one commit later
        content=ProcessConfig.model_validate(yaml.safe_load(_process_yaml(slug))).model_dump(
            mode="json", by_alias=True
        ),
        source_commit_sha="b" * 40,
    )
    async_session.add(bound)
    await async_session.flush()
    still_reading = await seed_ticket(async_session, project)
    still_reading.process_version_id = unbound.id
    closed = await seed_ticket(async_session, project, status=TicketStatus.DONE)
    closed.process_version_id = unbound.id
    closed.closed_at = datetime.now(UTC)
    on_bound = await seed_ticket(async_session, project)
    on_bound.process_version_id = bound.id
    await async_session.flush()

    files = mini_config_files(["OPENAI_API_KEY"])
    async with client_for(probe_app(async_session, files=files)) as client:
        await _create(client, auth, type="codex_api", slug=slug)
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        assert detail["runtime_provided_secrets"] == [
            {
                "name": "OPENAI_API_KEY",
                "runtime_slug": slug,
                "stored": False,
                "open_tickets_reading_copy": 1,
            }
        ]


async def test_a_broken_lane_is_reported_against_itself_and_leaves_the_others_alone(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """A lane's problems stay inside the lane (design.md §4). One verdict over several lanes
    would either hide a broken lane behind a working one or block a whole project over a lane
    nobody is using."""
    project = await _seed_with_runtime(async_session, "vanished")
    # A second lane naming no runtime at all: it must contribute no row and take no blame.
    async_session.add(
        ProcessVersion(
            project_id=project.id,
            source_repo=project.github_repo,
            process_key="docs",
            content=ProcessConfig.model_validate(
                yaml.safe_load(_process_yaml("ignored").replace("runtime: ignored\n", ""))
            ).model_dump(mode="json", by_alias=True),
            source_commit_sha=MINI_SHA,
        )
    )
    await async_session.flush()

    async with client_for(probe_app(async_session, files=mini_config_files([]))) as client:
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
    runtime_rows = [row for row in detail["readiness"] if row["key"] == "runtime"]
    assert [(row["process"], row["state"]) for row in runtime_rows] == [("mini", "blocked")]
    assert detail["blockers"] == []


# ---------------------------------------------------------------------- per-run adapter


class _Adapter:
    def __init__(self, tag: str) -> None:
        self.tag = tag

    async def logs(self, run_id: str) -> AsyncIterator[str]:
        yield f'{{"type":"result","subtype":"success","result":"from {self.tag}"}}'


async def test_run_logs_are_read_through_the_runs_own_runtime(
    auth: dict[str, str], async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that executed on a registry runtime streams from THAT runtime's adapter, resolved
    through the worker's cache seam; a run with no `runtime_id` has no adapter to read it."""
    from orchestrator.api import deps

    class _Cache:
        def __init__(self) -> None:
            self.seen: list[int] = []

        def for_runtime(self, row: Runtime, kind: str | None = None) -> _Adapter:
            self.seen.append(row.id)
            return _Adapter(row.slug)

    cache = _Cache()
    monkeypatch.setattr(deps, "_runtime_adapters", lambda: cache)

    app = build_app(make_settings(), async_session)
    assert runtime_adapter_dependency not in app.dependency_overrides
    async with client_for(app) as client:
        created = await _create(client, auth, type="codex_api")
        on_registry = await _run_on(async_session, created["id"], status=StepRunStatus.SUCCEEDED)
        on_none = await _run_on(async_session, created["id"], status=StepRunStatus.SUCCEEDED)
        on_none.runtime_id = None
        await async_session.flush()

        routed = await client.get(f"/api/step-runs/{on_registry.id}/activity", headers=auth)
        assert routed.status_code == 200, routed.text
        assert routed.json()["outcome"] == "success"
        assert cache.seen == [created["id"]]

        plain = await client.get(f"/api/step-runs/{on_none.id}/activity", headers=auth)
        assert plain.status_code == 503
        assert cache.seen == [created["id"]]


async def test_the_cors_preflight_allows_delete() -> None:
    from httpx import ASGITransport, AsyncClient

    from orchestrator.api.main import create_app

    app = create_app(cors_origins=["http://dash.test"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api.test") as client:
        response = await client.options(
            "/api/runtimes/1",
            headers={
                "Origin": "http://dash.test",
                "Access-Control-Request-Method": "DELETE",
            },
        )
    assert response.status_code == 200
    assert "DELETE" in response.headers["access-control-allow-methods"]


async def test_a_second_login_for_the_same_runtime_supersedes_the_first(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """Navigating away loses the page's session id; Connect again must not leave the first
    CLI and its scratch home behind, nor let two logins race to store."""
    first, second = FakeLogin(), FakeLogin()
    starts = iter([first, second])
    app = build_app(make_settings(), async_session)

    async def start(home: Path) -> FakeLogin:
        return await next(starts).start(home)

    app.dependency_overrides[login_runner_dependency] = lambda: start
    async with client_for(app) as client:
        created = await _create(client, auth, type="codex_subscription")
        rid = created["id"]
        one = (await client.post(f"/api/runtimes/{rid}/login", headers=auth)).json()
        two = (await client.post(f"/api/runtimes/{rid}/login", headers=auth)).json()
        assert one["session_id"] != two["session_id"]
        assert first.killed and first.home is not None and not first.home.exists()
        gone = (
            await client.get(f"/api/runtimes/{rid}/login/{one['session_id']}", headers=auth)
        ).json()
        assert gone["status"] == "failed" and "newer sign-in" in (gone["detail"] or "")
        assert not second.killed


async def test_deleting_a_runtime_waits_for_a_tick_that_is_spawning_on_it(
    auth: dict[str, str], async_session: AsyncSession, maker
) -> None:
    """The worker reads the row FOR SHARE for its whole tick; the delete counts live runs
    FOR UPDATE. Without both, a run seconds from referencing the row would be orphaned."""
    from orchestrator.worker.runtime_adapters import load_runtime_by_slug

    async with client_for(build_app(make_settings(), async_session)) as client:
        created = await _create(client, auth, slug="held-row", type="codex_api")
        await async_session.commit()  # the other session must be able to see the row
        async with maker() as tick:
            held = await load_runtime_by_slug(tick, await default_organization_id(tick), "held-row")
            assert held is not None
            deleting = asyncio.create_task(
                client.delete(f"/api/runtimes/{created['id']}", headers=auth)
            )
            await asyncio.sleep(0.5)
            assert not deleting.done(), "the delete must wait for the tick holding the row"
            await tick.rollback()
            assert (await deleting).status_code == 204


# ------------------------------------------------------------------------------- bindings
#
# Which projects a runtime is connected to, read three ways — the projects list's column, the
# runtimes list's count, and a runtime's own page — all from `api.bindings`, so they agree.


async def _add_version(session: AsyncSession, project: Project, seq: int, slug: str) -> None:
    """One more snapshot of the `mini` lane. `seq` only makes the commit distinct — which
    snapshot is current is registration order now, not a number in the file."""
    config = ProcessConfig.model_validate(yaml.safe_load(_process_yaml(slug)))
    session.add(
        ProcessVersion(
            project_id=project.id,
            source_repo=project.github_repo,
            process_key=config.name,
            content=config.model_dump(mode="json", by_alias=True),
            source_commit_sha=f"{seq:040x}",
        )
    )
    await session.flush()


async def test_the_projects_list_names_the_runtime_the_latest_version_binds(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    slug = _slug()
    bound = await _seed_with_runtime(async_session, slug)
    vanished = await _seed_with_runtime(async_session, "vanished")
    async with client_for(build_app(make_settings(), async_session)) as client:
        await _create(client, auth, type="codex_api", slug=slug)
        projects = (await client.get("/api/orgs/default/projects", headers=auth)).json()
        by_id = {p["id"]: p for p in projects}
    assert by_id[bound.id]["runtime"] == {"slug": slug, "name": f"Runtime {slug}"}
    # The slug is what the process says, so it shows; no row carries it, so nothing is named.
    assert by_id[vanished.id]["runtime"] == {"slug": "vanished", "name": None}


async def test_the_runtimes_list_counts_projects_by_their_latest_version_only(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    first, second = _slug(), _slug()
    await _seed_with_runtime(async_session, first)
    moved = await _seed_with_runtime(async_session, first)
    await _add_version(async_session, moved, 2, second)
    await seed_probe_project(async_session)
    async with client_for(build_app(make_settings(), async_session)) as client:
        a = await _create(client, auth, type="codex_api", slug=first)
        b = await _create(client, auth, type="claude_api", slug=second)
        assert a["connected_projects"] == 1
        assert b["connected_projects"] == 1
        runtimes = (await client.get("/api/orgs/default/runtimes", headers=auth)).json()
        listed = {r["slug"]: r for r in runtimes}
        assert listed[first]["connected_projects"] == 1
        assert listed[second]["connected_projects"] == 1
        one = (await client.get(f"/api/runtimes/{b['id']}", headers=auth)).json()
        assert one["connected_projects"] == 1
        # A project that moved to `second` at v2 no longer counts for `first`, and one more
        # naming `first` counts the moment its version is the latest.
        await _add_version(async_session, moved, 3, first)
        runtimes = (await client.get("/api/orgs/default/runtimes", headers=auth)).json()
        listed = {r["slug"]: r for r in runtimes}
        assert listed[first]["connected_projects"] == 2
        assert listed[second]["connected_projects"] == 0


async def test_a_runtimes_projects_route_lists_what_is_bound_to_it(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    slug = _slug()
    one = await _seed_with_runtime(async_session, slug)
    two = await _seed_with_runtime(async_session, slug)
    # A GitLab project on its own host: its repository link opens there, not on github.com.
    two.settings = {**(two.settings or {}), "gitlab": {"base_url": "https://gitlab.example.com"}}
    source = await async_session.scalar(
        select(TicketSource).where(TicketSource.project_id == two.id)
    )
    assert source is not None
    source.kind = TicketSourceKind.GITLAB
    await async_session.flush()
    await _seed_with_runtime(async_session, "vanished")
    await seed_probe_project(async_session)
    async with client_for(build_app(make_settings(), async_session)) as client:
        row = await _create(client, auth, type="codex_api", slug=slug)
        empty = await _create(client, auth, type="codex_api")
        listed = await client.get(f"/api/runtimes/{row['id']}/projects", headers=auth)
        assert listed.status_code == 200
        rows = listed.json()
        assert [(r["id"], r["name"], r["github_repo"]) for r in rows] == [
            (one.id, one.name, one.github_repo),
            (two.id, two.name, two.github_repo),
        ]
        assert [(r["forge"]["kind"], r["forge"]["origin"]) for r in rows] == [
            ("github", "https://github.com"),
            ("gitlab", "https://gitlab.example.com"),
        ]
        none = await client.get(f"/api/runtimes/{empty['id']}/projects", headers=auth)
        assert none.json() == []
        missing = await client.get("/api/runtimes/999999/projects", headers=auth)
        assert missing.status_code == 404


async def test_the_binding_rule_is_one_query_shared_by_every_reader(
    async_session: AsyncSession,
) -> None:
    """The helper itself: a slug with no row is reported (the projects list shows it) but binds
    nothing (no count, no row on a runtime page)."""
    from orchestrator.api.bindings import process_bindings

    slug = _slug()
    bound = await _seed_with_runtime(async_session, slug)
    vanished = await _seed_with_runtime(async_session, "vanished")
    await seed_probe_project(async_session)
    runtime = Runtime(
        organization_id=await default_organization_id(async_session),
        slug=slug,
        name="RT",
        type="codex_api",
        details={},
        metadata_={},
    )
    async_session.add(runtime)
    await async_session.flush()

    bindings = {b.project_id: b for b in await process_bindings(async_session)}
    assert bindings[bound.id].slug == slug
    assert bindings[bound.id].runtime_id == runtime.id
    assert bindings[bound.id].runtime_name == "RT"
    assert bindings[vanished.id].slug == "vanished"
    assert bindings[vanished.id].runtime_id is None
    assert bindings[vanished.id].runtime_name is None
    only = await process_bindings(async_session, runtime_ids=[runtime.id])
    assert [b.project_id for b in only] == [bound.id]


async def test_a_lane_from_another_repository_binds_no_runtime(
    async_session: AsyncSession,
) -> None:
    """A project whose repository changed offers none of the old repository's lanes, so they
    connect it to no runtime either: the three readers agree with admission."""
    from orchestrator.api.bindings import process_bindings

    project = await _seed_with_runtime(async_session, _slug())
    assert project.id in {b.project_id for b in await process_bindings(async_session)}

    project.github_repo = "acme/elsewhere"
    await async_session.flush()
    assert project.id not in {b.project_id for b in await process_bindings(async_session)}


async def test_a_runtime_is_created_on_e2b_and_a_bogus_sandbox_is_refused(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    async with client_for(build_app(make_settings(), async_session)) as client:
        created = await _create(client, auth, type="claude_api", details={"isolation": "e2b"})
        assert created["details"] == {"isolation": "e2b"}

        refused = await client.post(
            "/api/orgs/default/runtimes",
            headers=auth,
            json={
                "slug": _slug(),
                "name": "bogus",
                "type": "claude_api",
                "details": {"isolation": "firecracker"},
            },
        )
        assert refused.status_code == 422, refused.text


# ------------------------------------------------------------------------- test credential

#: A shape no vendor issues; its prefix is what a vendor's error body would echo.
PROBED_KEY = "sk-fake-probed-0000-not-a-key"


def _vendor(app: FastAPI, status_code: int, seen: list[httpx.Request]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        echo = {"error": {"message": f"Incorrect API key provided: {PROBED_KEY[:14]}****"}}
        return httpx.Response(status_code, json=echo)

    async def client() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            yield http

    app.dependency_overrides[probe_http_dependency] = client


async def test_test_credential_never_returns_logs_or_records_the_secret(
    auth: dict[str, str], async_session: AsyncSession, master_key: None, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    app = build_app(make_settings(), async_session)
    seen: list[httpx.Request] = []
    _vendor(app, 401, seen)
    async with client_for(app) as client:
        created = await _create(client, auth, type="claude_api")
        url = f"/api/runtimes/{created['id']}"
        put = await client.put(
            f"{url}/credentials/ANTHROPIC_API_KEY", headers=auth, json={"value": PROBED_KEY}
        )
        assert put.status_code == 200
        events_before = (await async_session.execute(select(func.count(Event.id)))).scalar_one()

        response = await client.post(f"{url}/test-credential", headers=auth)

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is False
    assert "401" in response.json()["detail"]
    assert [r.headers["x-api-key"] for r in seen] == [PROBED_KEY]
    assert PROBED_KEY[:14] not in response.text
    assert PROBED_KEY[:14] not in caplog.text
    events_after = (await async_session.execute(select(func.count(Event.id)))).scalar_one()
    assert events_after == events_before


async def test_test_credential_says_ok_and_checks_an_e2b_runtimes_key_beside_it(
    auth: dict[str, str],
    async_session: AsyncSession,
    master_key: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys: list[str] = []

    class _Sandboxes:
        def __init__(self, api_key: str, **_: object) -> None:
            keys.append(api_key)

        async def check(self) -> None:
            return None

    monkeypatch.setattr(runtimes_route, "E2bSandboxClient", _Sandboxes)
    app = build_app(make_settings(), async_session)
    _vendor(app, 200, [])
    async with client_for(app) as client:
        host = await _create(client, auth, type="codex_api")
        e2b = await _create(client, auth, type="codex_api", details={"isolation": "e2b"})
        for row in (host, e2b):
            url = f"/api/runtimes/{row['id']}/credentials/OPENAI_API_KEY"
            assert (await client.put(url, headers=auth, json={"value": PROBED_KEY})).is_success
        e2b_url = f"/api/runtimes/{e2b['id']}/credentials/E2B_API_KEY"
        assert (await client.put(e2b_url, headers=auth, json={"value": "e2b_a"})).is_success

        on_host = await client.post(f"/api/runtimes/{host['id']}/test-credential", headers=auth)
        on_e2b = await client.post(f"/api/runtimes/{e2b['id']}/test-credential", headers=auth)

    assert on_host.json() == {
        "ok": True,
        "detail": "OpenAI accepted the stored API key.",
        "local": False,
        "e2b": None,
    }
    assert on_e2b.json()["ok"] is True
    assert on_e2b.json()["e2b"] == {"ok": True, "detail": "E2B accepted the stored E2B_API_KEY."}
    assert keys == ["e2b_a"]


async def test_test_credential_requires_the_api_token(async_session: AsyncSession) -> None:
    async with client_for(build_app(make_settings(), async_session)) as client:
        assert (await client.post("/api/runtimes/1/test-credential")).status_code == 401


async def test_last_auth_failure_is_the_newest_refusal_on_this_runtime(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    async with client_for(build_app(make_settings(), async_session)) as client:
        created = await _create(client, auth, type="claude_subscription")
        other = await _create(client, auth, type="claude_subscription")
        url = f"/api/runtimes/{created['id']}"
        assert (await client.get(url, headers=auth)).json()["last_auth_failure"] is None

        run = await _run_on(async_session, created["id"], status=StepRunStatus.FAILED)
        elsewhere = await _run_on(async_session, other["id"], status=StepRunStatus.FAILED)
        ticket, project_id = await _ticket_of(async_session, run)

        async def failed(on: StepRun, kind: EventType, **details: object) -> None:
            on_ticket, on_project = await _ticket_of(async_session, on)
            await append(
                async_session,
                type=kind,
                project_id=on_project,
                ticket_id=on_ticket.id,
                step_run_id=on.id,
                **details,
            )

        await failed(
            run,
            EventType.AGENT_FAILED,
            failure_reason="Please run /login · API Error: 401 API key is invalid.",
        )
        await failed(
            run,
            EventType.AGENT_IDLE,
            turn_error="the turn failed\nInvalid bearer token " + "x" * 300 + "\nmore output",
        )
        await failed(
            run,
            EventType.AGENT_FAILED,
            failure_reason="postconditions unmet on #401 at 4012 tokens",
        )
        await failed(
            elsewhere,
            EventType.AGENT_FAILED,
            failure_reason="unexpected status 401 Unauthorized: Incorrect API key provided",
        )

        got = (await client.get(url, headers=auth)).json()["last_auth_failure"]
        listed = {
            row["id"]: row["last_auth_failure"]
            for row in (await client.get("/api/orgs/default/runtimes", headers=auth)).json()
        }

    repo = (
        await async_session.execute(select(Project.github_repo).where(Project.id == project_id))
    ).scalar_one()
    assert got["reason"] == ("Invalid bearer token " + "x" * 300)[:200]
    assert got["ticket"] == f"{repo}#{ticket.external_key}"
    assert got["ticket_id"] == ticket.id
    assert got["at"]
    assert listed[created["id"]] == got
    assert listed[other["id"]]["reason"] == (
        "unexpected status 401 Unauthorized: Incorrect API key provided"
    )


async def test_last_auth_failure_looks_only_at_the_newest_agent_ends(
    auth: dict[str, str], async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtimes_route, "_AUTH_FAILURE_WINDOW", 2)
    async with client_for(build_app(make_settings(), async_session)) as client:
        created = await _create(client, auth, type="claude_api")
        run = await _run_on(async_session, created["id"], status=StepRunStatus.FAILED)
        ticket, project_id = await _ticket_of(async_session, run)
        for reason in ("API Error: 401 API key is invalid.", "no PR", "no branch"):
            await append(
                async_session,
                type=EventType.AGENT_FAILED,
                project_id=project_id,
                ticket_id=ticket.id,
                step_run_id=run.id,
                failure_reason=reason,
            )

        got = await client.get(f"/api/runtimes/{created['id']}", headers=auth)

    assert got.json()["last_auth_failure"] is None


async def _ticket_of(session: AsyncSession, run: StepRun) -> tuple[Ticket, int]:
    ticket = (await session.execute(select(Ticket).where(Ticket.id == run.ticket_id))).scalar_one()
    return ticket, ticket.project_id


async def test_a_refused_credential_is_never_echoed(
    auth: dict[str, str], async_session: AsyncSession, master_key: None, caplog
) -> None:
    sentinel = "sk-sentinel-never-echo-0000"
    caplog.set_level(logging.DEBUG)
    async with client_for(build_app(make_settings(), async_session)) as client:
        row = await _create(client, auth, type="codex_api")
        put = await client.put(
            f"/api/runtimes/{row['id']}/credentials/OPENAI_API_KEY",
            headers=auth,
            json={"value": [sentinel]},
        )
        assert put.status_code == 422
        assert sentinel not in put.text
    assert sentinel not in caplog.text

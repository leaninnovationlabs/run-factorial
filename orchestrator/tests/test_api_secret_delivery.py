"""How a cloud sandbox run receives a project secret: the operator's choice, stored beside the
value and checked against the project's egress hosts (tech-stack decision 75)."""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Project
from orchestrator.secrets import store
from tests.api_harness import build_app, client_for, make_settings, seed_project

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]

NAME = "RAILWAY_TOKEN_X"
HEADER = {
    "value": "rw-value",
    "sandbox_delivery": "header",
    "egress_host": "backboard.railway.com",
    "egress_header": "project-access-token",
}


async def _project(session: AsyncSession, egress: Sequence[str]) -> Project:
    project = await seed_project(session)
    project.settings = {"egress_allowlist": list(egress)}
    await session.flush()
    return project


async def _put(
    session: AsyncSession, auth: dict[str, str], project: Project, body: dict[str, str]
) -> tuple[int, str]:
    async with client_for(build_app(make_settings(), session)) as client:
        response = await client.put(
            f"/api/projects/{project.id}/secrets/{NAME}", headers=auth, json=body
        )
    return response.status_code, response.text


async def test_a_header_delivery_is_stored_and_listed(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    project = await _project(async_session, ["backboard.railway.com"])
    status, _ = await _put(async_session, auth, project, HEADER)
    assert status == 200
    assert (await store.deliveries(async_session, project.id))[NAME] == store.Delivery(
        mode="header", host="backboard.railway.com", header="project-access-token"
    )
    async with client_for(build_app(make_settings(), async_session)) as client:
        listed = (await client.get(f"/api/projects/{project.id}/secrets", headers=auth)).json()
    assert listed["deliveries"] == [
        {
            "name": NAME,
            "mode": "header",
            "host": "backboard.railway.com",
            "header": "project-access-token",
        }
    ]


async def test_a_header_delivery_needs_one_of_the_projects_egress_hosts(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    project = await _project(async_session, ["backboard.railway.com"])
    status, text = await _put(async_session, auth, project, HEADER | {"egress_host": "sentry.io"})
    assert status == 422
    assert "sentry.io" in text and "egress hosts" in text
    assert json.loads(text)["code"] == "egress_host", "so the page shows it on the host field"
    assert await store.list_names(async_session, project.id) == []


async def test_a_wildcard_egress_host_covers_its_subdomains(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    project = await _project(async_session, ["*.railway.com"])
    status, _ = await _put(async_session, auth, project, HEADER)
    assert status == 200


async def test_a_header_delivery_needs_both_host_and_header(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    project = await _project(async_session, ["backboard.railway.com"])
    body = {key: value for key, value in HEADER.items() if key != "egress_header"}
    status, _ = await _put(async_session, auth, project, body)
    assert status == 422


async def test_a_github_host_never_takes_a_project_secret(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    project = await _project(async_session, ["api.github.com"])
    status, text = await _put(
        async_session, auth, project, HEADER | {"egress_host": "api.github.com"}
    )
    assert status == 422
    assert "api.github.com" in text


async def test_an_env_delivery_takes_no_host_or_header(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    project = await _project(async_session, ["backboard.railway.com"])
    status, _ = await _put(async_session, auth, project, HEADER | {"sandbox_delivery": "env"})
    assert status == 422
    status, _ = await _put(
        async_session, auth, project, {"value": "db-value", "sandbox_delivery": "env"}
    )
    assert status == 200
    assert (await store.deliveries(async_session, project.id))[NAME] == store.Delivery(
        mode="env", host=None, header=None
    )


async def test_a_secret_written_without_a_choice_is_an_environment_variable(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    """Every secret is supplied; the choice is only how. A write replaces the whole row, so a
    rewrite without a choice also leaves the header behind."""
    project = await _project(async_session, ["backboard.railway.com"])
    await _put(async_session, auth, project, HEADER)
    status, _ = await _put(async_session, auth, project, {"value": "rw-rotated"})
    assert status == 200
    assert (await store.deliveries(async_session, project.id))[NAME] == store.Delivery(mode="env")


async def _patch(
    session: AsyncSession, auth: dict[str, str], project: Project, body: dict[str, str]
) -> tuple[int, str]:
    async with client_for(build_app(make_settings(), session)) as client:
        response = await client.patch(
            f"/api/projects/{project.id}/secrets/{NAME}", headers=auth, json=body
        )
    return response.status_code, response.text


async def test_a_delivery_changes_without_writing_the_value_again(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    project = await _project(async_session, ["backboard.railway.com"])
    await _put(async_session, auth, project, {"value": "rw-value"})
    route = {key: value for key, value in HEADER.items() if key != "value"}

    status, text = await _patch(async_session, auth, project, route)

    assert status == 200
    assert json.loads(text) == {
        "name": NAME,
        "mode": "header",
        "host": "backboard.railway.com",
        "header": "project-access-token",
    }
    stored = await store.get(async_session, project.id, NAME)
    assert stored is not None and stored.get_secret_value() == "rw-value"
    status, _ = await _patch(async_session, auth, project, {"sandbox_delivery": "env"})
    assert status == 200
    assert (await store.deliveries(async_session, project.id))[NAME] == store.Delivery(mode="env")


async def test_a_delivery_change_is_checked_like_a_write(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    project = await _project(async_session, ["backboard.railway.com"])
    await _put(async_session, auth, project, {"value": "rw-value"})
    route = {"sandbox_delivery": "header", "egress_host": "sentry.io", "egress_header": "x-token"}
    status, text = await _patch(async_session, auth, project, route)
    assert status == 422 and json.loads(text)["code"] == "egress_host"
    assert (await store.deliveries(async_session, project.id))[NAME] == store.Delivery(mode="env")


async def test_a_delivery_change_needs_a_stored_secret(
    auth: dict[str, str], async_session: AsyncSession, master_key: None
) -> None:
    project = await _project(async_session, ["backboard.railway.com"])
    status, _ = await _patch(async_session, auth, project, {"sandbox_delivery": "env"})
    assert status == 404

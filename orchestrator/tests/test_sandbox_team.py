"""`sandbox_team`: the one answer to which E2B key and template a runtime's sandboxes use, and
the reaper's list of keys built from it. A managed row never falls back to a key stored on it."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.organizations import default_organization_id
from orchestrator.db.models import Runtime
from orchestrator.factorial.sandbox_team import SandboxTeam, sandbox_team
from orchestrator.secrets import store
from orchestrator.settings import ManagedSandbox
from orchestrator.worker.runtime_adapters import workspace_keys

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.usefixtures("master_key"),
    pytest.mark.asyncio(loop_scope="session"),
]

CLAUDE_BUILD = "factorial-claude:00000000-0000-4000-8000-000000000001"
CODEX_BUILD = "factorial-codex:00000000-0000-4000-8000-000000000002"
MANAGED = ManagedSandbox(
    api_key=SecretStr("e2b_managed_pool"), templates={"claude": CLAUDE_BUILD}
)


async def _row(
    session: AsyncSession, rt: str, details: dict[str, object], stored: str | None = None
) -> Runtime:
    slug = f"rt-{uuid4().hex[:10]}"
    row = Runtime(
        organization_id=await default_organization_id(session),
        slug=slug,
        name=slug,
        type=rt,
        details=details,
    )
    session.add(row)
    await session.flush()
    if stored is not None:
        await store.put_for_runtime(session, row.id, "E2B_API_KEY", stored)
    return row


def _plain(team: SandboxTeam) -> tuple[str | None, str | None, bool]:
    key = team.api_key.get_secret_value() if team.api_key is not None else None
    return key, team.template, team.managed


async def test_an_own_row_resolves_its_stored_key_and_template(
    async_session: AsyncSession,
) -> None:
    row = await _row(
        async_session, "claude_api", {"isolation": "e2b", "template": CODEX_BUILD}, "e2b_own"
    )

    team = await sandbox_team(async_session, row, MANAGED)

    assert _plain(team) == ("e2b_own", CODEX_BUILD, False)


async def test_a_managed_row_resolves_the_deployments_key_and_family_template(
    async_session: AsyncSession,
) -> None:
    row = await _row(async_session, "claude_api", {"isolation": "e2b", "sandbox_key": "managed"})

    team = await sandbox_team(async_session, row, MANAGED)

    assert _plain(team) == ("e2b_managed_pool", CLAUDE_BUILD, True)


async def test_a_managed_row_never_falls_back(async_session: AsyncSession) -> None:
    """A stale key left on the row is another team: a managed row reads only the deployment."""
    row = await _row(
        async_session, "claude_api", {"isolation": "e2b", "sandbox_key": "managed"}, "e2b_stale"
    )

    team = await sandbox_team(async_session, row, ManagedSandbox())

    assert _plain(team) == (None, None, True)


async def test_a_managed_row_without_its_family_template_keeps_the_key(
    async_session: AsyncSession,
) -> None:
    """Its existing sandboxes are still reached; only a new spawn needs the template."""
    row = await _row(async_session, "codex_api", {"isolation": "e2b", "sandbox_key": "managed"})

    team = await sandbox_team(async_session, row, MANAGED)

    assert _plain(team) == ("e2b_managed_pool", None, True)


async def test_the_reaper_lists_the_managed_project_with_no_managed_row(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pool's sandboxes outlive the last managed runtime, so the key is listed while it is
    set. A key equal to an own row's is listed once."""
    # The reaper reads every row in the database, and other tests' rows were sealed under
    # other master keys: only this test's rows answer.
    held: dict[int, str] = {}
    real_put = store.put_for_runtime

    async def put(session: AsyncSession, runtime_id: int, name: str, value: str) -> None:
        held[runtime_id] = value
        await real_put(session, runtime_id, name, value)

    async def get(_: AsyncSession, runtime_id: int, name: str) -> SecretStr | None:
        value = held.get(runtime_id)
        return SecretStr(value) if value is not None and name == "E2B_API_KEY" else None

    monkeypatch.setattr(store, "put_for_runtime", put)
    monkeypatch.setattr(store, "get_for_runtime", get)
    await _row(async_session, "claude_api", {"isolation": "e2b"}, "e2b_own")

    listed = [key.get_secret_value() for key in await workspace_keys(async_session, MANAGED)]

    assert "e2b_managed_pool" in listed
    assert "e2b_own" in listed

    await _row(async_session, "codex_api", {"isolation": "e2b"}, "e2b_managed_pool")
    listed = [key.get_secret_value() for key in await workspace_keys(async_session, MANAGED)]
    assert listed.count("e2b_managed_pool") == 1

"""Which E2B team a runtime's sandboxes run in: the key that reaches them and the template a new
one boots.

A runtime holds its own (`E2B_CREDENTIAL`, `details.template`), or is marked `sandbox_key:
managed` and takes the deployment's (`settings.runtime.e2b.managed`). Every reader asks
`sandbox_team`, so a managed row never falls back to a key stored on it, and the deployment's
key is read at use, never copied onto a row. The offer list is not asked: it gates creating a
runtime, never a spawn, so narrowing it leaves existing runtimes running.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Runtime
from orchestrator.factorial.runtimes import (
    E2B_CREDENTIAL,
    family,
    sandbox_key_of,
    template_of,
)
from orchestrator.secrets import store
from orchestrator.settings import ManagedSandbox

__all__ = ["SandboxTeam", "managed_gap", "sandbox_team"]


@dataclass(frozen=True, slots=True)
class SandboxTeam:
    api_key: SecretStr | None
    template: str | None
    managed: bool


async def sandbox_team(session: AsyncSession, row: Runtime, managed: ManagedSandbox) -> SandboxTeam:
    """Each part as far as it is set. A managed row whose family has no template keeps the key,
    so its existing sandboxes are still reached; only a new one needs the template."""
    if sandbox_key_of(row.details) == "managed":
        return SandboxTeam(managed.api_key, managed.templates.get(family(row.type)), managed=True)
    key = await store.get_for_runtime(session, row.id, E2B_CREDENTIAL)
    return SandboxTeam(key, template_of(row.details), managed=False)


def managed_gap(team: SandboxTeam, slug: str) -> str | None:
    """Why a managed runtime cannot start a new sandbox, or None. An own runtime's gaps are
    named where they always were: the adapter's spawn and readiness."""
    if not team.managed:
        return None
    if team.api_key is None:
        return (
            f"The Factorial-managed key is not available on this deployment, so runtime {slug!r} "
            "needs its own E2B key. Store it on the Runtime page."
        )
    if team.template is None:
        return (
            f"The Factorial-managed key no longer covers runtime {slug!r}. Store its own E2B key "
            "on the Runtime page."
        )
    return None

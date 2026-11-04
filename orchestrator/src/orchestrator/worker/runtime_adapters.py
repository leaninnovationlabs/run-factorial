"""The adapters behind registry `runtime` rows, built once per worker process and reused.

A `Runtime` row (`factorial.runtimes`) is what a process's `runtime: <slug>` resolves to. Its
adapter is constructed from the row — the registry kind, that kind's deployment options, and the
row's auth mode — and cached by row id and kind, because an adapter holds per-run state
(supervisors, children): the instance that spawned a run must be the one every later
status/interrupt/cancel/resume reaches. `ReconcileDeps.adapter_for_run` is the one seam that
looks a run's adapter up, keyed off the persisted `step_run.runtime_id` and the kind the run's
`agent_spawned` event recorded (`spawned_kind`), so editing a row never moves a live run.

An E2B runtime is also its own E2B team (decision 25): its `E2B_API_KEY` credential and its
`details.template`. The cached adapter is re-pointed at that team on every lookup that may
spawn (`use_workspace`), never rebuilt, so a live run keeps the one instance that supervises
it; the API refuses any change that would move a runtime's team while a run of it is live.

`RuntimeRefused` is a `RuntimeError` on purpose: that puts it in
`failures._PERMANENT_CONFIG_ERRORS`, so a refusal fails the attempt visibly and escalates on the
tick that saw it rather than rolling back into an attempt-1-forever spin. Every cause here is
one an operator fixes, never a retry.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Event, Runtime
from orchestrator.events import EventType
from orchestrator.factorial.runtimes import (
    E2B_CREDENTIAL,
    NOT_IMPLEMENTED_TYPES,
    RuntimeDetailsInvalid,
    auth_mode_for,
    isolation_of,
    row_registry_kind,
    template_of,
)
from orchestrator.secrets import store
from runtime.adapters import create_adapter
from runtime.contract import RuntimeAdapter

__all__ = [
    "RuntimeAdapters",
    "RuntimeRefused",
    "load_runtime_by_slug",
    "registry_build",
    "spawned_kind",
    "workspace_key",
    "workspace_keys",
]


@runtime_checkable
class SandboxWorkspace(Protocol):
    """An adapter that spawns into a hosted team the runtime row names (`E2bAdapter`)."""

    def use_workspace(self, *, api_key: SecretStr | None, template: str | None = None) -> None: ...


class RuntimeRefused(RuntimeError):
    """A run cannot go on the runtime its process names: no such row, a type the worker does not
    implement yet, or a manifest written for another family."""


class RuntimeAdapters:
    """Process-wide cache: one adapter instance per `runtime` row and registry kind.

    `build` is injected so a test can hand back fakes; production wires `registry_build`. The
    kind is part of the key because a row's details can change which kind it builds, and a run
    must keep reaching the kind that spawned it.
    """

    def __init__(
        self,
        build: Callable[[Runtime, str], RuntimeAdapter],
    ) -> None:
        self._build = build
        self._by_key: dict[tuple[int, str], RuntimeAdapter] = {}

    def cached(self, runtime_id: int, kind: str) -> RuntimeAdapter | None:
        """The instance already built for `runtime_id` and `kind`, without needing the row."""
        return self._by_key.get((runtime_id, kind))

    def for_runtime(
        self,
        row: Runtime,
        kind: str | None = None,
        *,
        held_key: SecretStr | None = None,
        repoint: bool = False,
    ) -> RuntimeAdapter:
        """The row's adapter; `kind`, when a run recorded one, pins the kind it spawned on.

        `repoint` is for the worker's lookups, which may spawn: an E2B adapter is pointed at
        the row's team, `held_key` (`workspace_key`) being the key the row holds. A lookup that
        only reads (the API's transcripts) leaves it alone."""
        if row.type in NOT_IMPLEMENTED_TYPES:
            raise RuntimeRefused(
                f"the runtime {row.slug} is of type {row.type}, which Factorial can't run yet. "
                "Point the process at another runtime"
            )
        kind = kind or row_registry_kind(row.type, row.details)
        adapter = self._by_key.get((row.id, kind))
        if adapter is None:
            adapter = self._by_key[(row.id, kind)] = self._build(row, kind)
        if repoint and isinstance(adapter, SandboxWorkspace):
            adapter.use_workspace(api_key=held_key, template=template_of(row.details))
        return adapter


def registry_build(
    options: Callable[[str], Mapping[str, Any]],
    *,
    create: Callable[..., RuntimeAdapter] = create_adapter,
) -> Callable[[Runtime, str], RuntimeAdapter]:
    """`create_adapter` for a row: the kind it runs on, that kind's deployment options
    (`worker.context.adapter_options`, passed in as `options(kind)` because that module imports
    this one), and the row's auth mode on top. Both families' adapters take the keyword under
    the same vocabulary; the deployment's own `auth_mode` setting is what the row overrides."""

    def build(row: Runtime, kind: str) -> RuntimeAdapter:
        return create(kind, **{**options(kind), "auth_mode": auth_mode_for(row.type)})

    return build


async def spawned_kind(session: AsyncSession, step_run_id: int) -> str | None:
    """The registry kind a run was spawned on, as its `agent_spawned` event recorded it.

    `None` before the spawn, and for runs whose event predates the field.
    """
    details = (
        await session.execute(
            select(Event.details)
            .where(Event.step_run_id == step_run_id, Event.type == EventType.AGENT_SPAWNED)
            .order_by(Event.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    kind = (details or {}).get("runtime")
    return kind if isinstance(kind, str) and kind else None


async def load_runtime_by_slug(
    session: AsyncSession, organization_id: int, slug: str
) -> Runtime | None:
    """The row `slug` names in the ticket's organization; slugs are unique per organization."""
    # FOR SHARE for the rest of the tick: the RUNNING step_run that will reference this row is
    # flushed and committed only after the spawn, and the API's delete counts live runs under
    # FOR UPDATE — so it waits for this tick rather than deleting out from under it.
    query = (
        select(Runtime)
        .where(Runtime.organization_id == organization_id, Runtime.slug == slug)
        .with_for_update(read=True)
    )
    return (await session.execute(query)).scalar_one_or_none()


async def workspace_key(session: AsyncSession, row: Runtime) -> SecretStr | None:
    """The E2B key an E2B runtime holds, decrypted for its adapter and never for a run."""
    try:
        if isolation_of(row.details) != "e2b":
            return None
    except RuntimeDetailsInvalid:
        return None  # `for_runtime` refuses the row itself
    return await store.get_for_runtime(session, row.id, E2B_CREDENTIAL)


async def workspace_keys(session: AsyncSession) -> list[SecretStr]:
    """Every E2B key the runtimes hold, once each: the teams the reaper lists, beside the ones
    it has seen retired."""
    rows = (
        await session.execute(select(Runtime).where(Runtime.details["isolation"].astext == "e2b"))
    ).scalars()
    keys: dict[str, SecretStr] = {}
    for row in rows:
        key = await store.get_for_runtime(session, row.id, E2B_CREDENTIAL)
        if key is not None:
            keys.setdefault(key.get_secret_value(), key)
    return list(keys.values())

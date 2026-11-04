"""The one keep rule (tech-stack decision 54): which runs' workspaces outlive them.

A workspace is kept while any run in it is live, and otherwise while its ticket has not exited
and a run in it ended less than `workspace.retention` ago, done tickets included. A rework that
adopts an earlier attempt's workspace names it as `workspace_run_id` on its `agent_spawned`, so
the rule keys on the workspace, not the run; a spawn written before that key works in its own.
The reaper keeps these (`worker.reaper`), and a runtime-served preview and an inspect shell start
only from one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from orchestrator.db.models import (
    Event,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
    TicketStatus,
    current_runs,
)
from orchestrator.events.types import EventType

__all__ = [
    "LIVE_STATUSES",
    "WHY_NOT_INSPECTABLE",
    "WHY_NOT_SERVABLE",
    "KeptRun",
    "KeptWorkspaces",
    "Uninspectable",
    "Unservable",
    "kept_workspaces",
]

LIVE_STATUSES: Final = (StepRunStatus.RUNNING, StepRunStatus.IDLE)

Unservable = Literal["released", "private", "done", "continued", "restarted", "redone"]

WHY_NOT_SERVABLE: Final[Mapping[Unservable, str]] = {
    "released": "This run's files were cleaned up.",
    "private": "This run's runtime can't serve a preview. Previews run in a cloud sandbox.",
    # No pass reaches a done ticket, so a Start would never be honoured.
    "done": "This ticket is done, so its preview can't start.",
    "continued": "A later attempt continues in this workspace. Preview that attempt instead.",
    "restarted": "The ticket was restarted, so nothing from before the restart is served.",
    "redone": "The step was redone, so this set-aside attempt is not served.",
}

#: Not `private` or `done`: a host workspace has a shell, and the inspect pass reaches a done
#: ticket (`worker.inspect`).
Uninspectable = Literal["released", "live", "continued"]

WHY_NOT_INSPECTABLE: Final[Mapping[Uninspectable, str]] = {
    "released": WHY_NOT_SERVABLE["released"],
    "live": "The agent is still running in this workspace.",
    "continued": "A later attempt continues in this workspace. Open the session on that attempt.",
}


@dataclass(frozen=True, slots=True)
class KeptRun:
    step_run_id: int
    runtime_run_id: str
    workspace: str
    status: StepRunStatus
    ticket_status: TicketStatus
    #: Spawned with a sandbox a preview can serve (`RunSpec.public`).
    public: bool
    ended_at: datetime | None = None
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class KeptWorkspaces:
    """Every run the rule returned, by `step_run.id`."""

    runs: Mapping[int, KeptRun]

    @property
    def keep(self) -> frozenset[str]:
        """The runtime run ids a reaper must not touch: every kept workspace, and every run in
        one."""
        return frozenset(
            name for run in self.runs.values() for name in (run.workspace, run.runtime_run_id)
        )

    def unservable(self, step_run_id: int) -> Unservable | None:
        """Why the run's preview cannot start, or None when it can: it was spawned public, its
        workspace is kept, and no later run works in that workspace. A live run's preview can
        start, and runs beside it."""
        run = self.runs.get(step_run_id)
        if run is None:
            return "released"
        if not run.public:
            return "private"
        if run.ticket_status == TicketStatus.DONE:
            return "done"
        return "continued" if self._continued(run) else None

    def uninspectable(self, step_run_id: int) -> Uninspectable | None:
        """Why a shell cannot open in the run's workspace, or None when it can: it ended, its
        workspace is kept, and no later run works in that workspace."""
        run = self.runs.get(step_run_id)
        if run is None:
            return "released"
        if run.status in LIVE_STATUSES:
            return "live"
        return "continued" if self._continued(run) else None

    def kept_until(self, step_run_id: int, retention: timedelta) -> datetime | None:
        """When the run's workspace is released unless its ticket exits first. None when it is
        not kept, when a later run works in it, or while a run in it is live, which keeps it
        however long that takes."""
        run = self.runs.get(step_run_id)
        if run is None or self._continued(run):
            return None
        sharing = [other for other in self.runs.values() if other.workspace == run.workspace]
        if any(other.status in LIVE_STATUSES for other in sharing):
            return None
        ended = [other.ended_at for other in sharing if other.ended_at is not None]
        return max(ended) + retention if ended else None

    def continued_by(self, step_run_id: int) -> int | None:
        """The latest later run working in the run's workspace, or None: it holds the session."""
        run = self.runs.get(step_run_id)
        if run is None:
            return None
        later = [
            other.step_run_id
            for other in self.runs.values()
            if other.workspace == run.workspace and other.step_run_id > run.step_run_id
        ]
        return max(later, default=None)

    def _continued(self, run: KeptRun) -> bool:
        return self.continued_by(run.step_run_id) is not None


async def kept_workspaces(
    session: AsyncSession, now: datetime, retention: timedelta, *, ticket_id: int | None = None
) -> KeptWorkspaces:
    spawned = aliased(Event)
    query = (
        select(
            StepRun.id,
            StepRun.runtime_run_id,
            StepRun.status,
            StepRun.ended_at,
            StepRun.attempt,
            Ticket.status,
            spawned.details["workspace_run_id"].as_string(),
            spawned.details["public"],
            # The key a spawn wrote before `public`.
            spawned.details["retain_workspace"],
        )
        .join(Ticket, Ticket.id == StepRun.ticket_id)
        .outerjoin(
            spawned,
            and_(
                # The ticket filter is for `event_ticket_idx`; the run is what selects.
                spawned.ticket_id == StepRun.ticket_id,
                spawned.step_run_id == StepRun.id,
                spawned.type == EventType.AGENT_SPAWNED,
            ),
        )
        .where(
            StepRun.kind == StepKind.AGENT,
            StepRun.runtime_run_id.is_not(None),
            or_(
                StepRun.status.in_(LIVE_STATUSES),
                and_(
                    Ticket.status != TicketStatus.EXITED,
                    # A restart or a redo abandons what came before it (decisions 67, 76): the
                    # reaper is the only release an interrupted run's workspace has.
                    current_runs(Ticket.generation),
                    StepRun.ended_at >= now - retention,
                ),
            ),
        )
        .order_by(StepRun.id, spawned.id)
    )
    if ticket_id is not None:
        query = query.where(StepRun.ticket_id == ticket_id)
    rows = await session.execute(query)
    runs: dict[int, KeptRun] = {}
    for (
        run_id,
        runtime_run_id,
        status,
        ended_at,
        attempt,
        ticket_status,
        workspace,
        public,
        legacy,
    ) in rows:
        if runtime_run_id is None:
            continue
        # The latest spawn event wins.
        runs[run_id] = KeptRun(
            step_run_id=run_id,
            runtime_run_id=runtime_run_id,
            workspace=workspace or runtime_run_id,
            status=StepRunStatus(status),
            ticket_status=TicketStatus(ticket_status),
            public=bool(public if public is not None else legacy),
            ended_at=ended_at,
            attempt=attempt,
        )
    return KeptWorkspaces(runs)

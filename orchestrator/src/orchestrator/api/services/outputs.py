"""What a run shows beside its terminal, read against the ticket's PINNED process: the step's
`preview:` block, whether it awaits a review, and the blobs `/pages/{id}` may serve for the run
(decision 58) — the files its step declares (decision 71), each as the worker recorded it. A
recorded blob the config does not select is not served, whatever link it comes with.
"""

from __future__ import annotations

from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.config import AgentStep, GateStep, Preview, ProcessConfig
from orchestrator.config.conditions import awaits_review
from orchestrator.db.models import ProcessVersion, StepRun, Ticket
from orchestrator.events.outputs import outputs_observation, select_files

__all__ = ["Servable", "pinned_config", "pinned_preview", "pinned_review_step", "servable_blobs"]


class Servable(NamedTuple):
    path: str
    #: As recorded; None where the worker could not read it, and the route checks the bytes.
    size: int | None


async def pinned_config(session: AsyncSession, ticket: Ticket) -> ProcessConfig:
    """The ticket's PINNED process: the one it was admitted under, whatever was synced since."""
    content = (
        await session.execute(
            select(ProcessVersion.content).where(ProcessVersion.id == ticket.process_version_id)
        )
    ).scalar_one()
    return ProcessConfig.model_validate(content)


async def pinned_review_step(
    session: AsyncSession, ticket: Ticket, run: StepRun
) -> AgentStep | None:
    """The run's step in the ticket's PINNED process when its postconditions call
    `request_approval()`, else None."""
    step = (await pinned_config(session, ticket)).find_step(run.step_name)
    return step if isinstance(step, AgentStep) and awaits_review(step.postconditions) else None


async def pinned_preview(session: AsyncSession, ticket: Ticket, run: StepRun) -> Preview | None:
    """The run's step's `preview:` block in the ticket's PINNED process, enabled and with something
    to show, or None."""
    step = (await pinned_config(session, ticket)).find_step(run.step_name)
    preview = None if step is None or isinstance(step, GateStep) else step.preview
    if preview is None or not preview.enabled or preview.only_a_retired_page:
        return None
    return preview


async def servable_blobs(
    session: AsyncSession, ticket: Ticket, run: StepRun
) -> dict[str, Servable]:
    """Blob -> where it was recorded, for each published declared file of the run. Two files with
    one blob are one: the first names the path."""
    preview = await pinned_preview(session, ticket, run)
    if preview is None:
        return {}
    served: dict[str, Servable] = {}
    if preview.files:
        observed = await outputs_observation(session, run)
        for selected in select_files(preview.files, observed, ticket=ticket.external_key):
            if selected.status == "published" and selected.blob and selected.path:
                served.setdefault(selected.blob, Servable(selected.path, selected.size))
    return served

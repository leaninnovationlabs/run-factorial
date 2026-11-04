"""Cost per issue (design.md §5).

Summed from `step_run`, where usage belongs — there is no cost table to drift. Per-step budgets
are not enforced anywhere (design.md §2.6): this reports, it does not gate.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter

from orchestrator.access import Permission
from orchestrator.api.access import needs
from orchestrator.api.deps import SessionDep
from orchestrator.api.schemas import CostReport, TicketCostOut
from orchestrator.events import cost_per_ticket

router = APIRouter(tags=["costs"])


@router.get("/projects/{project_id}/costs", response_model=CostReport)
@needs(Permission.ORG_READ)
async def get_costs(project_id: int, session: SessionDep) -> CostReport:
    rows = await cost_per_ticket(session, project_id)
    return CostReport(
        project_id=project_id,
        tickets=[TicketCostOut.model_validate(row) for row in rows],
        total_cost_usd=sum((row.cost_usd for row in rows), Decimal(0)),
        total_tokens=sum(row.tokens for row in rows),
    )

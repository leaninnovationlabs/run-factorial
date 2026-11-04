"""Liveness and readiness. Both are unauthenticated — a probe has no token to present."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from orchestrator.api.deps import SessionDep
from orchestrator.api.schemas import AssistantHealthOut, HealthResponse, ReadyResponse
from orchestrator.api.services.assistant_settings import load_assistant_status

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """Liveness: the process is up. No DB — a database outage must not get the API killed."""
    return HealthResponse(status="ok")


@router.get("/readyz", response_model=ReadyResponse)
async def readyz(session: SessionDep) -> ReadyResponse:
    """Readiness: the process can serve, which means it can reach Postgres. It also names the
    assistant process's last preflight, which never makes the API unready: chat is one feature,
    and while the preflight has failed a send answers 503."""
    try:
        await session.execute(text("SELECT 1"))
        assistant = await load_assistant_status(session)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"database unavailable: {type(exc).__name__}",
        ) from exc
    return ReadyResponse(
        status="ready",
        assistant=(
            None
            if assistant is None
            else AssistantHealthOut(
                ok=bool(assistant.get("ok")),
                message=assistant.get("message"),
                at=assistant.get("at"),
            )
        ),
    )

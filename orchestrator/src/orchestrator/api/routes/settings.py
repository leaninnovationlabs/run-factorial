"""An organization's assistant settings and credential (`/api/orgs/{org}/settings/assistant`).

Reading the settings needs `assistant:configure`, as changing them does; `/ready` says only
whether the assistant is on and has a credential, for the chat of anyone who may use it.

Writes are dashboard gestures: the session cookie rides along on a cross-site request, so an
`Origin` naming the dashboard is required, as for MCP tokens. The credential is write-only: no
route returns it, and a read says only whether one is stored and who set it. That holds for a
refused one too: these routes' 422 says where and why, never the `input` FastAPI's own repeats.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from orchestrator.access import Permission
from orchestrator.api.access import needs, require_dashboard_origin
from orchestrator.api.auth import PrincipalDep
from orchestrator.api.deps import OrganizationDep, SessionDep, SettingsDep
from orchestrator.api.guards import NoEchoRoute
from orchestrator.api.schemas import (
    AssistantBackendOut,
    AssistantCredentialIn,
    AssistantCredentialOut,
    AssistantReadyOut,
    AssistantSettingsOut,
)
from orchestrator.api.services import assistant_settings as svc
from orchestrator.api.services.assistant_send import SendRefused, queue_user_message
from orchestrator.assistant.config import AssistantSettings
from orchestrator.assistant.vocabulary import BACKENDS
from orchestrator.db.models import ChatConversation

__all__ = ["router"]


router = APIRouter(tags=["settings"], route_class=NoEchoRoute)

_NO_STORE: Final = {"Cache-Control": "no-store"}
_TEST_TITLE: Final = "Connection test"
_TEST_PROMPT: Final = "Reply with the single word OK."


def _from_dashboard(request: Request, settings: SettingsDep) -> None:
    require_dashboard_origin(request, settings, "Change the assistant settings from the dashboard.")


_DashboardOnly = Depends(_from_dashboard)


@router.get("/settings/assistant/ready", response_model=AssistantReadyOut)
@needs(Permission.ASSISTANT_USE)
async def get_assistant_ready(
    session: SessionDep, response: Response, organization: OrganizationDep, settings: SettingsDep
) -> AssistantReadyOut:
    response.headers.update(_NO_STORE)
    current = await svc.load_assistant_settings(session, organization.id)
    credential = AssistantCredentialOut.model_validate(
        await svc.credential_state(session, organization.id, current)
    )
    return AssistantReadyOut(
        enabled=current.enabled,
        credential_set=credential.set,
        credential_updated_at=credential.updated_at,
        ready_to_send=await svc.assistant_ready_to_send(
            session, organization.id, current, multi_org=settings.multi_org.enabled
        ),
    )


@router.get("/settings/assistant", response_model=AssistantSettingsOut)
@needs(Permission.ASSISTANT_CONFIGURE)
async def get_assistant(
    session: SessionDep, response: Response, organization: OrganizationDep, settings: SettingsDep
) -> AssistantSettingsOut:
    response.headers.update(_NO_STORE)
    current = await svc.load_assistant_settings(session, organization.id)
    state = await svc.credential_state(session, organization.id, current)
    health = await svc.load_assistant_status(session)
    return AssistantSettingsOut(
        settings=current,
        credential=AssistantCredentialOut.model_validate(state),
        backends=[
            AssistantBackendOut(
                kind=vocab.kind,
                label=vocab.label,
                auth_modes=list(vocab.credentials),
                default_model=vocab.default_model,
            )
            for vocab in BACKENDS.values()
        ],
        ready_to_send=await svc.assistant_ready_to_send(
            session, organization.id, current, multi_org=settings.multi_org.enabled
        ),
        status=health,
    )


@router.put("/settings/assistant", dependencies=[_DashboardOnly])
@needs(Permission.ASSISTANT_CONFIGURE)
async def put_assistant(
    body: AssistantSettings,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    organization: OrganizationDep,
    settings: SettingsDep,
) -> AssistantSettings:
    response.headers.update(_NO_STORE)
    if svc.host_bedrock_refused(body, multi_org=settings.multi_org.enabled):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, svc.HOST_BEDROCK_REFUSED)
    await svc.save_assistant_settings(session, organization.id, body, by=principal)
    await session.commit()
    return body


@router.put(
    "/settings/assistant/credential",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[_DashboardOnly],
)
@needs(Permission.ASSISTANT_CONFIGURE)
async def put_credential(
    body: AssistantCredentialIn,
    session: SessionDep,
    principal: PrincipalDep,
    organization: OrganizationDep,
) -> Response:
    await svc.set_assistant_credential(session, organization.id, body.value, by=principal)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=_NO_STORE)


@router.delete(
    "/settings/assistant/credential",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[_DashboardOnly],
)
@needs(Permission.ASSISTANT_CONFIGURE)
async def delete_credential(session: SessionDep, organization: OrganizationDep) -> Response:
    await svc.clear_assistant_credential(session, organization.id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=_NO_STORE)


@router.post(
    "/settings/assistant/test",
    dependencies=[_DashboardOnly],
    responses={409: {}, 429: {}, 503: {}},
)
@needs(Permission.ASSISTANT_CONFIGURE)
async def test_assistant(
    session: SessionDep,
    principal: PrincipalDep,
    organization: OrganizationDep,
    settings: SettingsDep,
) -> JSONResponse:
    """One real turn through the same gate a send meets, in a conversation the Drawer's list
    never shows. A refusal rolls the conversation back with it."""
    assistant = await svc.load_assistant_settings(session, organization.id)
    conversation = ChatConversation(
        organization_id=organization.id,
        owner=principal,
        title=_TEST_TITLE,
        backend_kind=assistant.backend,
        archived_at=datetime.now(UTC),
    )
    session.add(conversation)
    await session.flush()
    try:
        await queue_user_message(
            session,
            conversation.id,
            principal,
            text=_TEST_PROMPT,
            page=None,
            multi_org=settings.multi_org.enabled,
        )
    except SendRefused as refusal:
        await session.rollback()
        return JSONResponse(
            status_code=refusal.status,
            content={"code": refusal.code, "detail": refusal.detail},
            headers=_NO_STORE,
        )
    await session.commit()
    return JSONResponse({"conversation_id": conversation.id}, headers=_NO_STORE)

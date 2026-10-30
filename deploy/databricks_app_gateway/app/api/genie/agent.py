"""Databricks Genie Agent mode 호환 Controller."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.api.dependencies import GatewayContextDependency, GatewayServiceDependency
from app.schemas.genie import AgentModeResponseRequest

router = APIRouter(prefix="/api/2.0/genie/agents", tags=["Genie Agent mode"])


@router.post("/{agent_id}/responses")
async def create_agent_mode_response(
    agent_id: str,
    payload: AgentModeResponseRequest,
    request: Request,
    context: GatewayContextDependency,
    service: GatewayServiceDependency,
) -> Response:
    """Agent mode 질문을 전송하고 SSE 응답을 스트리밍한다."""
    return await service.proxy(
        request,
        context,
        "AGENT",
        agent_id,
        "POST",
        "/api/2.0/genie/agents/{upstream_id}/responses",
        payload.model_dump(exclude_none=True),
        conversation_id=payload.conversation_id,
        register_conversation=payload.conversation_id is None,
        include_user_context=True,
    )


@router.post("/{agent_id}/conversations/{conversation_id}/responses/{response_id}/cancel")
async def cancel_agent_mode_response(
    agent_id: str,
    conversation_id: str,
    response_id: str,
    request: Request,
    context: GatewayContextDependency,
    service: GatewayServiceDependency,
) -> Response:
    """진행 중인 Agent mode 응답을 취소한다."""
    path = (
        f"/api/2.0/genie/agents/{{upstream_id}}/conversations/{conversation_id}"
        f"/responses/{response_id}/cancel"
    )
    return await service.proxy(
        request,
        context,
        "AGENT",
        agent_id,
        "POST",
        path,
        conversation_id=conversation_id,
    )


@router.get("/{agent_id}/conversations/{conversation_id}/items")
async def list_agent_mode_conversation_items(
    agent_id: str,
    conversation_id: str,
    request: Request,
    context: GatewayContextDependency,
    service: GatewayServiceDependency,
) -> Response:
    """Agent mode 대화 Item을 커서 페이지네이션으로 조회한다."""
    path = f"/api/2.0/genie/agents/{{upstream_id}}/conversations/{conversation_id}/items"
    return await service.proxy(
        request, context, "AGENT", agent_id, "GET", path, conversation_id=conversation_id
    )

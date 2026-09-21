"""운영자용 커스텀 Chat UI와 Swagger 비노출 BFF API."""

from __future__ import annotations

from pathlib import Path

import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.core.errors import GatewayError
from app.domain.models import GatewayPrincipal, GatewayRequestContext, GenieRoute
from app.schemas.genie import CreateMessageRequest, StartConversationRequest
from app.schemas.supervisor import ApprovalDecisionRequest, UnifiedAgentResponseRequest
from app.services.agent_report_pdf import build_agent_report_pdf
from app.services.run_orchestrator import PreparedRun
from app.services.supervisor_adapter import build_approval_response_request

router = APIRouter(prefix="/custom", include_in_schema=False)

_CHAT_DIRECTORY = Path(__file__).resolve().parents[2] / "static" / "custom" / "chat"
_CHAT_INDEX = _CHAT_DIRECTORY / "index.html"
_SUPERVISOR_DIRECTORY = Path(__file__).resolve().parents[2] / "static" / "custom" / "supervisor-chat"
_SUPERVISOR_INDEX = _SUPERVISOR_DIRECTORY / "index.html"
_ASSETS = {
    "chat.css": (_CHAT_DIRECTORY / "chat.css", "text/css"),
    "chat.js": (_CHAT_DIRECTORY / "chat.js", "text/javascript"),
    "agent-mode.js": (_CHAT_DIRECTORY / "agent-mode.js", "text/javascript"),
}
_SUPERVISOR_ASSETS = {
    "supervisor.css": (_SUPERVISOR_DIRECTORY / "supervisor.css", "text/css"),
    "approval.css": (_SUPERVISOR_DIRECTORY / "approval.css", "text/css"),
    "supervisor.js": (_SUPERVISOR_DIRECTORY / "supervisor.js", "text/javascript"),
}
_LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost", "testclient"}


class RenameConversationRequest(BaseModel):
    """커스텀 Chat 대화 제목 변경 요청."""

    title: str = Field(min_length=1, max_length=1_000)


class CustomAgentResponseRequest(BaseModel):
    """Custom UI에서 Agent SSE에 전달할 최소 입력."""

    content: str = Field(min_length=1, max_length=10_000)
    conversation_id: str | None = None
    enable_viz: bool = True


def _assert_custom_chat_access(request: Request) -> None:
    settings = request.app.state.settings
    if not settings.custom_chat_ui_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    client_host = request.client.host if request.client else ""
    if settings.custom_chat_local_only and client_host not in _LOCAL_CLIENTS:
        raise HTTPException(status_code=403, detail="Custom chat UI is restricted to local access.")


async def _custom_principal(request: Request) -> GatewayPrincipal:
    _assert_custom_chat_access(request)
    settings = request.app.state.settings
    principal = await request.app.state.gateway_repository.get_custom_chat_principal()
    if principal is None:
        raise GatewayError(503, "TEMPORARILY_UNAVAILABLE", "Custom chat client is not configured.")
    return principal


def _custom_context(request: Request, principal: GatewayPrincipal) -> GatewayRequestContext:
    return GatewayRequestContext(
        principal,
        principal.sys_cd,
        "shnam",
    )


@router.get("/supervisor-chat", response_class=FileResponse)
async def custom_supervisor_chat(request: Request) -> FileResponse:
    """Supervisor 통합 API를 검증하는 전용 화면을 제공한다."""
    _assert_custom_chat_access(request)
    return FileResponse(
        _SUPERVISOR_INDEX,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/supervisor-chat/assets/{asset_name}", response_class=FileResponse)
async def custom_supervisor_asset(request: Request, asset_name: str) -> FileResponse:
    _assert_custom_chat_access(request)
    asset = _SUPERVISOR_ASSETS.get(asset_name)
    if asset is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(asset[0], media_type=asset[1], headers={"Cache-Control": "no-store"})


@router.get("/supervisor-chat/api/bootstrap")
async def custom_supervisor_bootstrap(request: Request) -> dict[str, object]:
    principal = await _custom_principal(request)
    agents = await request.app.state.gateway_repository.list_available_agents(principal)
    supervisors = [
        {
            "agent_id": row["agt_cd"],
            "name": row["agt_nm"],
            "description": row["agt_desc_cn"],
            "long_task": row["long_task_use_yn"] == "Y",
        }
        for row in agents
        if row["agt_typ_cd"] == "SUPERVISOR"
    ]
    return {"agents": supervisors}


@router.get("/supervisor-chat/api/conversations")
async def custom_supervisor_conversations(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, object]:
    """현재 검증 사용자의 Supervisor 대화 목록을 반환한다."""
    principal = await _custom_principal(request)
    rows = await request.app.state.run_repository.list_owned_conversations(
        principal, "shnam", limit
    )
    return {
        "conversations": [
            {
                "conversation_id": row["cnvs_uid"],
                "agent_id": row["agt_cd"],
                "agent_name": row["agt_nm"],
                "title": row["display_title"],
                "status": row["last_run_status"],
                "created_at": row["str_dt"],
                "updated_at": row["last_msg_dt"],
            }
            for row in rows
        ]
    }


@router.get(
    "/supervisor-chat/api/agents/{agent_id}/conversations/{conversation_id}/items"
)
async def custom_supervisor_items(
    request: Request,
    agent_id: str,
    conversation_id: str,
    limit: int = Query(default=100, ge=1, le=100),
) -> dict[str, object]:
    """현재 검증 사용자가 소유한 Supervisor 대화 Item을 반환한다."""
    principal = await _custom_principal(request)
    route = await request.app.state.gateway_repository.resolve_supervisor_route(
        principal, agent_id
    )
    if route is None:
        raise GatewayError(404, "AGENT_NOT_FOUND", "Supervisor Agent was not found.")
    rows = await request.app.state.run_repository.list_owned_items(
        principal, route, "shnam", conversation_id, 0, limit
    )
    return {
        "items": [
            {
                "item_id": f"item_{row['run_item_id']}",
                "sequence": row["item_seq_no"],
                "item_type": row["item_typ_cd"],
                "content": row["item_data"] or row["text_cn"],
            }
            for row in rows
        ]
    }


@router.post("/supervisor-chat/api/agents/{agent_id}/responses")
async def custom_supervisor_response(
    request: Request,
    agent_id: str,
    payload: UnifiedAgentResponseRequest,
) -> StreamingResponse:
    principal = await _custom_principal(request)
    prepared = await request.app.state.run_orchestrator.prepare(
        _custom_context(request, principal),
        agent_id,
        payload,
        request.state.trace_id,
        request.headers.get("idempotency-key"),
    )

    async def stream():
        async for envelope in request.app.state.run_execution_manager.subscribe(
            prepared, request.headers.get("last-event-id")
        ):
            data = json.dumps(envelope["data"], ensure_ascii=False, separators=(",", ":"))
            yield f'id: {envelope["id"]}\nevent: {envelope["event"]}\ndata: {data}\n\n'

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/supervisor-chat/api/agents/{agent_id}/runs/{run_id}/cancel")
async def custom_supervisor_cancel(
    request: Request, agent_id: str, run_id: str
) -> dict[str, object]:
    principal = await _custom_principal(request)
    route = await request.app.state.gateway_repository.resolve_supervisor_route(
        principal, agent_id
    )
    if route is None:
        raise GatewayError(404, "AGENT_NOT_FOUND", "Supervisor Agent was not found.")
    return await request.app.state.run_repository.request_cancel(
        principal, route, "shnam", run_id
    )


@router.post("/supervisor-chat/api/agents/{agent_id}/runs/{run_id}/approvals/{approval_id}")
async def custom_supervisor_approval(
    request: Request,
    agent_id: str,
    run_id: str,
    approval_id: str,
    payload: ApprovalDecisionRequest,
) -> StreamingResponse:
    """검증 UI에서 Supervisor 도구 승인 후 같은 Run 스트림을 재개한다."""
    principal = await _custom_principal(request)
    route = await request.app.state.gateway_repository.resolve_supervisor_route(principal, agent_id)
    if route is None:
        raise GatewayError(404, "AGENT_NOT_FOUND", "Supervisor Agent was not found.")
    run, approval_request, messages = await request.app.state.run_repository.decide_approval(
        principal, route, "shnam", run_id, approval_id, payload.approve, request.state.trace_id
    )
    upstream_payload = build_approval_response_request(
        messages, approval_request, payload.approve,
        allow_long_task=route.long_task_enabled,
    )
    prepared = PreparedRun(route, run, upstream_payload)

    async def stream():
        async for envelope in request.app.state.run_execution_manager.subscribe(prepared, skip_existing=True):
            data = json.dumps(envelope["data"], ensure_ascii=False, separators=(",", ":"))
            yield f'id: {envelope["id"]}\nevent: {envelope["event"]}\ndata: {data}\n\n'

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


async def _custom_route(
    request: Request,
    principal: GatewayPrincipal,
    agent_id: str,
) -> GenieRoute:
    route = await request.app.state.gateway_repository.resolve_route(principal, agent_id, "CHAT")
    if route is None:
        raise GatewayError(404, "NOT_FOUND", "The requested Genie agent is not available.")
    return route


@router.get("/chat", response_class=FileResponse)
async def custom_chat(request: Request) -> FileResponse:
    """활성화된 로컬 환경에만 커스텀 Chat UI를 제공한다."""
    _assert_custom_chat_access(request)
    return FileResponse(
        _CHAT_INDEX,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; "
                "script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.get("/chat/assets/{asset_name}", response_class=FileResponse)
async def custom_chat_asset(request: Request, asset_name: str) -> FileResponse:
    """허용 목록에 있는 Chat UI 정적 자산만 동일한 접근 정책으로 제공한다."""
    _assert_custom_chat_access(request)
    asset = _ASSETS.get(asset_name)
    if asset is None:
        raise HTTPException(status_code=404, detail="Not Found")
    path, media_type = asset
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/chat/api/bootstrap")
async def custom_chat_bootstrap(request: Request) -> dict[str, object]:
    """브라우저에 Secret이나 내부 사용자값 없이 허용 Agent 목록만 제공한다."""
    principal = await _custom_principal(request)
    agents = await request.app.state.gateway_repository.list_custom_chat_agents(principal)
    if not agents:
        raise GatewayError(503, "TEMPORARILY_UNAVAILABLE", "No custom chat agent is configured.")
    default_agent = next((item for item in agents if item["is_default"]), agents[0])
    return {"agents": agents, "default_agent_id": default_agent["agent_id"]}


@router.get("/chat/api/agents/{agent_id}/conversations")
async def custom_chat_conversations(request: Request, agent_id: str) -> dict[str, object]:
    """운영자 전용 사용자의 Chat/Agent 통합 대화 이력을 반환한다."""
    principal = await _custom_principal(request)
    mode = request.query_params.get("mode", "CHAT").upper()
    if mode not in {"CHAT", "AGENT"}:
        raise GatewayError(400, "INVALID_PARAMETER_VALUE", "mode must be CHAT or AGENT.")
    route = await request.app.state.gateway_repository.resolve_route(principal, agent_id, mode)
    if route is None:
        raise GatewayError(404, "NOT_FOUND", "The requested Genie agent is not available.")
    conversations = await request.app.state.gateway_repository.list_custom_conversations(
        principal,
        route,
        "shnam",
    )
    return {"conversations": conversations}


@router.post("/chat/api/agents/{agent_id}/start-conversation")
async def custom_chat_start_conversation(
    request: Request,
    agent_id: str,
    payload: StartConversationRequest,
) -> Response:
    """서버 내부 Principal로 새 Chat Conversation을 시작한다."""
    principal = await _custom_principal(request)
    return await request.app.state.gateway_service.proxy(
        request,
        _custom_context(request, principal),
        "CHAT",
        agent_id,
        "POST",
        "/api/2.0/genie/spaces/{upstream_id}/start-conversation",
        payload.model_dump(exclude_none=True),
        register_conversation=True,
        include_user_context=True,
    )


@router.post("/chat/api/agents/{agent_id}/conversations/{conversation_id}/messages")
async def custom_chat_create_message(
    request: Request,
    agent_id: str,
    conversation_id: str,
    payload: CreateMessageRequest,
) -> Response:
    """선택한 Conversation에 후속 질문을 전송한다."""
    principal = await _custom_principal(request)
    path = f"/api/2.0/genie/spaces/{{upstream_id}}/conversations/{conversation_id}/messages"
    return await request.app.state.gateway_service.proxy(
        request,
        _custom_context(request, principal),
        "CHAT",
        agent_id,
        "POST",
        path,
        payload.model_dump(exclude_none=True),
        conversation_id=conversation_id,
        include_user_context=True,
    )


@router.get("/chat/api/agents/{agent_id}/conversations/{conversation_id}/messages")
async def custom_chat_list_messages(
    request: Request,
    agent_id: str,
    conversation_id: str,
) -> Response:
    """기존 Conversation의 Message와 Attachment를 조회한다."""
    principal = await _custom_principal(request)
    path = f"/api/2.0/genie/spaces/{{upstream_id}}/conversations/{conversation_id}/messages"
    return await request.app.state.gateway_service.proxy(
        request,
        _custom_context(request, principal),
        "CHAT",
        agent_id,
        "GET",
        path,
        conversation_id=conversation_id,
    )


@router.get(
    "/chat/api/agents/{agent_id}/conversations/{conversation_id}/messages/{message_id}"
)
async def custom_chat_get_message(
    request: Request,
    agent_id: str,
    conversation_id: str,
    message_id: str,
) -> Response:
    """Polling 대상 Message의 최신 상태를 조회한다."""
    principal = await _custom_principal(request)
    path = (
        f"/api/2.0/genie/spaces/{{upstream_id}}/conversations/{conversation_id}"
        f"/messages/{message_id}"
    )
    return await request.app.state.gateway_service.proxy(
        request,
        _custom_context(request, principal),
        "CHAT",
        agent_id,
        "GET",
        path,
        conversation_id=conversation_id,
    )


@router.patch("/chat/api/agents/{agent_id}/conversations/{conversation_id}")
async def custom_chat_rename_conversation(
    request: Request,
    agent_id: str,
    conversation_id: str,
    payload: RenameConversationRequest,
    mode: Literal["CHAT", "AGENT"] = Query("CHAT"),
) -> dict[str, str]:
    """대화 이력에 표시할 제목을 변경한다."""
    principal = await _custom_principal(request)
    route = await request.app.state.gateway_repository.resolve_route(principal, agent_id, mode)
    if route is None:
        raise GatewayError(404, "NOT_FOUND", "The requested Genie agent is not available.")
    owner = await request.app.state.gateway_repository.assert_conversation_owner(
        principal,
        route,
        "shnam",
        conversation_id,
        mode,
    )
    await request.app.state.gateway_repository.rename_conversation(
        owner.conversation_pk,
        payload.title.strip(),
        principal.api_clnt_cd,
    )
    return {"conversation_id": conversation_id, "title": payload.title.strip()}


@router.post("/chat/api/agents/{agent_id}/agent/responses")
async def custom_agent_response(
    request: Request,
    agent_id: str,
    payload: CustomAgentResponseRequest,
) -> Response:
    """Custom UI의 Agent 요청을 Databricks SSE로 전달한다."""
    principal = await _custom_principal(request)
    body: dict[str, object] = {
        "enable_viz": payload.enable_viz,
        "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": payload.content}
        ]}],
    }
    if payload.conversation_id:
        body["conversation_id"] = payload.conversation_id
    return await request.app.state.gateway_service.proxy(
        request,
        _custom_context(request, principal),
        "AGENT",
        agent_id,
        "POST",
        "/api/2.0/genie/agents/{upstream_id}/responses",
        body,
        conversation_id=payload.conversation_id,
        register_conversation=payload.conversation_id is None,
        include_user_context=True,
        upstream_accept="text/event-stream",
        conversation_title=payload.content[:1_000],
    )


@router.get("/chat/api/agents/{agent_id}/agent/conversations/{conversation_id}/items")
async def custom_agent_items(request: Request, agent_id: str, conversation_id: str) -> Response:
    """Agent 대화 이력을 Databricks /items에서 조회한다."""
    principal = await _custom_principal(request)
    path = f"/api/2.0/genie/agents/{{upstream_id}}/conversations/{conversation_id}/items"
    return await request.app.state.gateway_service.proxy(
        request, _custom_context(request, principal), "AGENT", agent_id, "GET", path,
        conversation_id=conversation_id,
    )


@router.get("/chat/api/agents/{agent_id}/agent/conversations/{conversation_id}/reports/pdf")
async def custom_agent_report_pdf(request: Request, agent_id: str, conversation_id: str) -> Response:
    """Agent /items를 정규화해 PDF 보고서로 내려준다."""
    principal = await _custom_principal(request)
    path = f"/api/2.0/genie/agents/{{upstream_id}}/conversations/{conversation_id}/items"
    items_response = await request.app.state.gateway_service.proxy(
        request, _custom_context(request, principal), "AGENT", agent_id, "GET", path,
        conversation_id=conversation_id,
    )
    if not 200 <= items_response.status_code < 300:
        return items_response
    raw = getattr(items_response, "body", b"{}")
    payload = json.loads(raw.decode("utf-8"))
    pdf = build_agent_report_pdf(payload, conversation_id)
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="genie-report-{conversation_id}.pdf"'},
    )


@router.delete("/chat/api/agents/{agent_id}/conversations/{conversation_id}")
async def custom_chat_delete_conversation(
    request: Request,
    agent_id: str,
    conversation_id: str,
) -> Response:
    """Databricks Conversation을 삭제하고 Gateway 소유권을 비활성화한다."""
    principal = await _custom_principal(request)
    path = f"/api/2.0/genie/spaces/{{upstream_id}}/conversations/{conversation_id}"
    return await request.app.state.gateway_service.proxy(
        request,
        _custom_context(request, principal),
        "CHAT",
        agent_id,
        "DELETE",
        path,
        conversation_id=conversation_id,
        delete_conversation=True,
    )


@router.get(
    "/chat/api/agents/{agent_id}/conversations/{conversation_id}/messages/{message_id}"
    "/attachments/{attachment_id}/query-result"
)
async def custom_chat_query_result(
    request: Request,
    agent_id: str,
    conversation_id: str,
    message_id: str,
    attachment_id: str,
) -> Response:
    """Query Attachment 결과를 운영자 화면에 전달한다."""
    principal = await _custom_principal(request)
    path = (
        f"/api/2.0/genie/spaces/{{upstream_id}}/conversations/{conversation_id}"
        f"/messages/{message_id}/attachments/{attachment_id}/query-result"
    )
    return await request.app.state.gateway_service.proxy(
        request,
        _custom_context(request, principal),
        "CHAT",
        agent_id,
        "GET",
        path,
        conversation_id=conversation_id,
    )


@router.get(
    "/chat/api/agents/{agent_id}/conversations/{conversation_id}/messages/{message_id}"
    "/attachments/{attachment_id}/visualization"
)
async def custom_chat_visualization(
    request: Request,
    agent_id: str,
    conversation_id: str,
    message_id: str,
    attachment_id: str,
) -> Response:
    """완료된 Visualization Attachment 이미지를 전달한다."""
    principal = await _custom_principal(request)
    path = (
        f"/api/2.0/genie/spaces/{{upstream_id}}/conversations/{conversation_id}"
        f"/messages/{message_id}/attachments/{attachment_id}/download-visualization"
    )
    response = await request.app.state.gateway_service.proxy(
        request,
        _custom_context(request, principal),
        "CHAT",
        agent_id,
        "GET",
        path,
        conversation_id=conversation_id,
        upstream_accept="application/octet-stream",
    )
    if 200 <= response.status_code < 300:
        # Databricks는 PNG 본문을 application/octet-stream 및 attachment로 반환한다.
        # 브라우저 <img>가 안정적으로 표시하도록 성공 응답만 inline PNG로 정규화한다.
        response.headers["content-type"] = "image/png"
        response.headers["content-disposition"] = "inline"
    return response

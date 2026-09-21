"""CUBE 등 외부 시스템용 통합 AI Agent API."""

from __future__ import annotations

import base64
import json

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.dependencies import (
    GatewayContextDependency,
    GatewayRepositoryDependency,
    RunExecutionManagerDependency,
    RunOrchestratorDependency,
    RunRepositoryDependency,
)
from app.core.errors import GatewayError
from app.schemas.supervisor import (
    AgentCapabilities,
    ApprovalDecisionRequest,
    CancelRunResponse,
    RunError,
    RunItem,
    RunItemPage,
    RunResponse,
    UnifiedAgentResponseRequest,
    UnifiedAgentSummary,
)
from app.services.run_orchestrator import PreparedRun
from app.services.supervisor_adapter import build_approval_response_request

router = APIRouter(prefix="/api/v1/agents", tags=["Unified AI Agents"])


@router.get("", response_model=list[UnifiedAgentSummary])
async def list_agents(
    context: GatewayContextDependency,
    repository: GatewayRepositoryDependency,
) -> list[UnifiedAgentSummary]:
    """인증 시스템에서 선택 가능한 Agent를 실제 Databricks 리소스 없이 반환한다."""
    rows = await repository.list_available_agents(context.principal)
    result: list[UnifiedAgentSummary] = []
    for row in rows:
        mode = row["agt_mode_cd"]
        interaction_modes = ["CHAT", "AGENT"] if mode == "BOTH" else [mode]
        result.append(
            UnifiedAgentSummary(
                agent_id=row["agt_cd"],
                name=row["agt_nm"],
                description=row["agt_desc_cn"],
                agent_type=row["agt_typ_cd"],
                interaction_modes=interaction_modes,
                capabilities=AgentCapabilities(
                    streaming=mode in {"AGENT", "BOTH", "SUPERVISOR"},
                    long_task=row["long_task_use_yn"] == "Y",
                    cancel=mode in {"AGENT", "BOTH", "SUPERVISOR"},
                    approval=row["agt_typ_cd"] == "SUPERVISOR",
                ),
            )
        )
    return result


@router.post("/{agent_id}/responses")
async def create_response(
    agent_id: str,
    payload: UnifiedAgentResponseRequest,
    request: Request,
    context: GatewayContextDependency,
    orchestrator: RunOrchestratorDependency,
    manager: RunExecutionManagerDependency,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=200),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID", max_length=64),
) -> StreamingResponse:
    """Supervisor Run을 시작하고 정규화된 Gateway SSE를 반환한다."""
    prepared = await orchestrator.prepare(
        context,
        agent_id,
        payload,
        request.state.trace_id,
        idempotency_key,
    )

    async def stream():
        async for envelope in manager.subscribe(prepared, last_event_id):
            encoded = json.dumps(envelope["data"], ensure_ascii=False, separators=(",", ":"))
            yield f'id: {envelope["id"]}\nevent: {envelope["event"]}\ndata: {encoded}\n\n'

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


async def _supervisor_route(repository, context, agent_id):
    route = await repository.resolve_supervisor_route(context.principal, agent_id)
    if route is None:
        raise GatewayError(404, "AGENT_NOT_FOUND", "Supervisor Agent was not found.")
    return route


@router.post("/{agent_id}/runs/{run_id}/cancel", response_model=CancelRunResponse)
async def cancel_run(
    agent_id: str,
    run_id: str,
    request: Request,
    context: GatewayContextDependency,
    repository: GatewayRepositoryDependency,
    runs: RunRepositoryDependency,
) -> JSONResponse:
    route = await _supervisor_route(repository, context, agent_id)
    result = await runs.request_cancel(context.principal, route, context.user_id, run_id)
    result["trace_id"] = request.state.trace_id
    status_code = (
        200
        if result["status"] in {"COMPLETED", "CANCELLED", "TIMED_OUT", "FAILED"}
        else 202
    )
    body = CancelRunResponse.model_validate(result).model_dump(mode="json")
    return JSONResponse(status_code=status_code, content=body)


@router.post("/{agent_id}/runs/{run_id}/approvals/{approval_id}")
async def decide_approval(
    agent_id: str,
    run_id: str,
    approval_id: str,
    payload: ApprovalDecisionRequest,
    context: GatewayContextDependency,
    repository: GatewayRepositoryDependency,
    runs: RunRepositoryDependency,
    manager: RunExecutionManagerDependency,
) -> StreamingResponse:
    """보류 중인 도구 호출을 승인/거절하고 같은 Run의 SSE를 재개한다."""
    route = await _supervisor_route(repository, context, agent_id)
    run, approval_request, messages = await runs.decide_approval(
        context.principal, route, context.user_id, run_id, approval_id,
        payload.approve, context.principal.api_clnt_cd,
    )
    upstream_payload = build_approval_response_request(
        messages, approval_request, payload.approve,
        allow_long_task=route.long_task_enabled,
    )
    prepared = PreparedRun(route, run, upstream_payload)

    async def stream():
        async for envelope in manager.subscribe(prepared, skip_existing=True):
            encoded = json.dumps(envelope["data"], ensure_ascii=False, separators=(",", ":"))
            yield f'id: {envelope["id"]}\nevent: {envelope["event"]}\ndata: {encoded}\n\n'

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get("/{agent_id}/runs/{run_id}", response_model=RunResponse)
async def get_run(agent_id: str, run_id: str, context: GatewayContextDependency, repository: GatewayRepositoryDependency, runs: RunRepositoryDependency) -> RunResponse:
    route = await _supervisor_route(repository, context, agent_id)
    row = await runs.get_owned_run(context.principal, route, context.user_id, run_id)
    error = (
        RunError(
            error_code=row["err_cd"],
            message=row["err_msg_cn"] or "Run failed.",
            request_id=row["trace_id"],
        )
        if row["err_cd"]
        else None
    )
    return RunResponse(
        run_id=row["run_uid"],
        conversation_id=row["cnvs_uid"],
        agent_id=route.agt_cd,
        status=row["run_stts_cd"],
        started_at=row["str_dt"],
        last_event_at=row["last_evt_dt"],
        completed_at=row["end_dt"],
        error=error,
        trace_id=row["trace_id"],
    )


@router.get("/{agent_id}/conversations/{conversation_id}/items", response_model=RunItemPage)
async def list_items(
    agent_id: str,
    conversation_id: str,
    context: GatewayContextDependency,
    repository: GatewayRepositoryDependency,
    runs: RunRepositoryDependency,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> RunItemPage:
    route = await _supervisor_route(repository, context, agent_id)
    try:
        after = int(base64.urlsafe_b64decode(cursor.encode()).decode()) if cursor else 0
    except Exception as exc:
        raise GatewayError(
            400, "INVALID_PARAMETER_VALUE", "cursor is invalid."
        ) from exc
    rows = await runs.list_owned_items(context.principal, route, context.user_id, conversation_id, after, limit + 1)
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        RunItem(
            item_id=f"item_{row['run_item_id']}",
            sequence=row["item_seq_no"],
            item_type=row["item_typ_cd"],
            status=row["item_stts_cd"],
            title=row["title_nm"],
            content=row["item_data"] or row["text_cn"],
            truncated=row["trnc_yn"] == "Y",
        )
        for row in rows
    ]
    next_cursor = (
        base64.urlsafe_b64encode(str(rows[-1]["run_item_id"]).encode()).decode()
        if has_more and rows
        else None
    )
    return RunItemPage(items=items, next_cursor=next_cursor)

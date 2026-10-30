"""Gateway canonical 요청을 Supervisor endpoint 최소 JSON으로 변환한다."""

from __future__ import annotations

from typing import Any

from app.schemas.supervisor import UnifiedAgentResponseRequest


def build_supervisor_request(
    request: UnifiedAgentResponseRequest,
    history: list[dict[str, str]],
    *,
    allow_long_task: bool,
) -> dict[str, Any]:
    """허용된 필드만 사용하며 외부 요청을 그대로 전달하지 않는다."""
    inputs = [
        {"role": item["role"], "content": item["content"]}
        for item in history
        if item.get("role") in {"user", "assistant"} and item.get("content")
    ]
    inputs.append({"role": "user", "content": request.input})
    payload: dict[str, Any] = {"input": inputs, "stream": True}
    if request.long_task:
        if not allow_long_task:
            raise ValueError("long_task is not supported by this Agent")
        payload["databricks_options"] = {"long_task": True, "return_trace": True}
    return payload


def build_continuation_request(
    messages: list[dict[str, Any]],
    continue_request: dict[str, Any],
) -> dict[str, Any]:
    """공식 long-task 형식으로 다음 Supervisor 요청을 생성한다."""
    continue_request_id = continue_request.get("id")
    if (
        continue_request.get("type") != "task_continue_request"
        or not isinstance(continue_request_id, str)
        or not continue_request_id
    ):
        raise ValueError("invalid task_continue_request")
    return {
        "messages": [
            *messages,
            continue_request,
            {
                "type": "task_continue_response",
                "continue_request_id": continue_request_id,
            },
        ],
        "databricks_options": {"long_task": True},
        "stream": True,
    }


def build_approval_response_request(
    messages: list[dict[str, Any]],
    approval_request: dict[str, Any],
    approve: bool,
    *,
    allow_long_task: bool,
) -> dict[str, Any]:
    """원본 승인 요청과 사용자 결정을 포함해 Supervisor 실행을 재개한다."""
    approval_request_id = approval_request.get("id")
    if (
        approval_request.get("type") != "mcp_approval_request"
        or not isinstance(approval_request_id, str)
        or not approval_request_id
    ):
        raise ValueError("invalid mcp_approval_request")
    payload: dict[str, Any] = {
        "input": [
            *messages,
            approval_request,
            {
                "type": "mcp_approval_response",
                "id": approval_request_id,
                "approval_request_id": approval_request_id,
                "approve": approve,
            },
        ],
        "stream": True,
    }
    if allow_long_task:
        payload["databricks_options"] = {"long_task": True, "return_trace": True}
    return payload

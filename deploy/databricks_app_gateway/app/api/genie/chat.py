"""Databricks Genie Chat mode 호환 Controller."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.api.dependencies import GatewayContextDependency, GatewayServiceDependency
from app.schemas.genie import (
    CreateMessageRequest,
    MessageCommentRequest,
    MessageFeedbackRequest,
    StartConversationRequest,
)

router = APIRouter(prefix="/api/2.0/genie/spaces", tags=["Genie Chat mode"])


@router.post("/{space_id}/start-conversation")
async def start_conversation(
    space_id: str,
    payload: StartConversationRequest,
    request: Request,
    context: GatewayContextDependency,
    service: GatewayServiceDependency,
) -> Response:
    """새 Chat mode 대화를 시작한다."""
    return await service.proxy(
        request,
        context,
        "CHAT",
        space_id,
        "POST",
        "/api/2.0/genie/spaces/{upstream_id}/start-conversation",
        payload.model_dump(exclude_none=True),
        register_conversation=True,
        include_user_context=True,
    )


@router.get("/{space_id}/conversations")
async def list_conversations(
    space_id: str,
    request: Request,
    context: GatewayContextDependency,
    service: GatewayServiceDependency,
) -> Response:
    """Genie Agent의 대화 목록을 조회한다."""
    return await service.list_conversations(request, context, space_id)


@router.delete("/{space_id}/conversations/{conversation_id}")
async def delete_conversation(
    space_id: str,
    conversation_id: str,
    request: Request,
    context: GatewayContextDependency,
    service: GatewayServiceDependency,
) -> Response:
    """대화를 삭제한다."""
    return await service.proxy(
        request,
        context,
        "CHAT",
        space_id,
        "DELETE",
        f"/api/2.0/genie/spaces/{{upstream_id}}/conversations/{conversation_id}",
        conversation_id=conversation_id,
        delete_conversation=True,
    )


@router.post("/{space_id}/conversations/{conversation_id}/messages")
async def create_conversation_message(
    space_id: str,
    conversation_id: str,
    payload: CreateMessageRequest,
    request: Request,
    context: GatewayContextDependency,
    service: GatewayServiceDependency,
) -> Response:
    """기존 대화에 후속 메시지를 생성한다."""
    return await service.proxy(
        request,
        context,
        "CHAT",
        space_id,
        "POST",
        f"/api/2.0/genie/spaces/{{upstream_id}}/conversations/{conversation_id}/messages",
        payload.model_dump(exclude_none=True),
        conversation_id=conversation_id,
        include_user_context=True,
    )


@router.get("/{space_id}/conversations/{conversation_id}/messages")
async def list_conversation_messages(
    space_id: str,
    conversation_id: str,
    request: Request,
    context: GatewayContextDependency,
    service: GatewayServiceDependency,
) -> Response:
    """대화 메시지 목록을 조회한다."""
    return await service.proxy(
        request,
        context,
        "CHAT",
        space_id,
        "GET",
        f"/api/2.0/genie/spaces/{{upstream_id}}/conversations/{conversation_id}/messages",
        conversation_id=conversation_id,
    )


@router.get("/{space_id}/conversations/{conversation_id}/messages/{message_id}")
async def get_conversation_message(
    space_id: str,
    conversation_id: str,
    message_id: str,
    request: Request,
    context: GatewayContextDependency,
    service: GatewayServiceDependency,
) -> Response:
    """대화 메시지와 Attachment를 조회한다."""
    return await service.proxy(
        request,
        context,
        "CHAT",
        space_id,
        "GET",
        f"/api/2.0/genie/spaces/{{upstream_id}}/conversations/{conversation_id}/messages/{message_id}",
        conversation_id=conversation_id,
    )


@router.delete("/{space_id}/conversations/{conversation_id}/messages/{message_id}")
async def delete_conversation_message(
    space_id: str,
    conversation_id: str,
    message_id: str,
    request: Request,
    context: GatewayContextDependency,
    service: GatewayServiceDependency,
) -> Response:
    """대화 메시지를 삭제한다."""
    return await service.proxy(
        request,
        context,
        "CHAT",
        space_id,
        "DELETE",
        f"/api/2.0/genie/spaces/{{upstream_id}}/conversations/{conversation_id}/messages/{message_id}",
        conversation_id=conversation_id,
    )


@router.get(
    "/{space_id}/conversations/{conversation_id}/messages/{message_id}/attachments/{attachment_id}/query-result"
)
async def get_message_attachment_query_result(
    space_id: str,
    conversation_id: str,
    message_id: str,
    attachment_id: str,
    request: Request,
    context: GatewayContextDependency,
    service: GatewayServiceDependency,
) -> Response:
    """메시지 Query Attachment의 SQL 실행 결과를 조회한다."""
    path = (
        f"/api/2.0/genie/spaces/{{upstream_id}}/conversations/{conversation_id}"
        f"/messages/{message_id}/attachments/{attachment_id}/query-result"
    )
    return await service.proxy(
        request, context, "CHAT", space_id, "GET", path, conversation_id=conversation_id
    )


@router.post(
    "/{space_id}/conversations/{conversation_id}/messages/{message_id}/attachments/{attachment_id}/execute-query"
)
async def execute_message_attachment_query(
    space_id: str,
    conversation_id: str,
    message_id: str,
    attachment_id: str,
    request: Request,
    context: GatewayContextDependency,
    service: GatewayServiceDependency,
) -> Response:
    """만료된 Query Attachment의 SQL을 재실행한다."""
    path = (
        f"/api/2.0/genie/spaces/{{upstream_id}}/conversations/{conversation_id}"
        f"/messages/{message_id}/attachments/{attachment_id}/execute-query"
    )
    return await service.proxy(
        request, context, "CHAT", space_id, "POST", path, conversation_id=conversation_id
    )


@router.post(
    "/{space_id}/conversations/{conversation_id}/messages/{message_id}/attachments/{attachment_id}/downloads"
)
async def generate_download_full_query_result(
    space_id: str,
    conversation_id: str,
    message_id: str,
    attachment_id: str,
    request: Request,
    context: GatewayContextDependency,
    service: GatewayServiceDependency,
) -> Response:
    """전체 Query Result 다운로드 생성을 요청한다."""
    path = (
        f"/api/2.0/genie/spaces/{{upstream_id}}/conversations/{conversation_id}"
        f"/messages/{message_id}/attachments/{attachment_id}/downloads"
    )
    return await service.proxy(
        request, context, "CHAT", space_id, "POST", path, conversation_id=conversation_id
    )


@router.get(
    "/{space_id}/conversations/{conversation_id}/messages/{message_id}/attachments/{attachment_id}/downloads/{download_id}"
)
async def get_download_full_query_result(
    space_id: str,
    conversation_id: str,
    message_id: str,
    attachment_id: str,
    download_id: str,
    request: Request,
    context: GatewayContextDependency,
    service: GatewayServiceDependency,
) -> Response:
    """생성된 전체 Query Result 다운로드를 조회한다."""
    path = (
        f"/api/2.0/genie/spaces/{{upstream_id}}/conversations/{conversation_id}"
        f"/messages/{message_id}/attachments/{attachment_id}/downloads/{download_id}"
    )
    return await service.proxy(
        request, context, "CHAT", space_id, "GET", path, conversation_id=conversation_id
    )


@router.get(
    "/{space_id}/conversations/{conversation_id}/messages/{message_id}/attachments/{attachment_id}/download-visualization"
)
async def download_message_attachment_visualization(
    space_id: str,
    conversation_id: str,
    message_id: str,
    attachment_id: str,
    request: Request,
    context: GatewayContextDependency,
    service: GatewayServiceDependency,
) -> Response:
    """Visualization Attachment의 PNG 이미지를 다운로드한다."""
    path = (
        f"/api/2.0/genie/spaces/{{upstream_id}}/conversations/{conversation_id}"
        f"/messages/{message_id}/attachments/{attachment_id}/download-visualization"
    )
    return await service.proxy(
        request, context, "CHAT", space_id, "GET", path, conversation_id=conversation_id
    )


@router.post("/{space_id}/conversations/{conversation_id}/messages/{message_id}/feedback")
async def send_message_feedback(
    space_id: str,
    conversation_id: str,
    message_id: str,
    payload: MessageFeedbackRequest,
    request: Request,
    context: GatewayContextDependency,
    service: GatewayServiceDependency,
) -> Response:
    """메시지에 Feedback을 전송한다."""
    path = (
        f"/api/2.0/genie/spaces/{{upstream_id}}/conversations/{conversation_id}"
        f"/messages/{message_id}/feedback"
    )
    return await service.proxy(
        request,
        context,
        "CHAT",
        space_id,
        "POST",
        path,
        payload.model_dump(exclude_none=True),
        conversation_id=conversation_id,
    )


@router.post("/{space_id}/conversations/{conversation_id}/messages/{message_id}/comments")
async def create_message_comment(
    space_id: str,
    conversation_id: str,
    message_id: str,
    payload: MessageCommentRequest,
    request: Request,
    context: GatewayContextDependency,
    service: GatewayServiceDependency,
) -> Response:
    """메시지 Comment를 생성한다."""
    path = (
        f"/api/2.0/genie/spaces/{{upstream_id}}/conversations/{conversation_id}"
        f"/messages/{message_id}/comments"
    )
    return await service.proxy(
        request,
        context,
        "CHAT",
        space_id,
        "POST",
        path,
        payload.model_dump(exclude_none=True),
        conversation_id=conversation_id,
    )


@router.get("/{space_id}/conversations/{conversation_id}/messages/{message_id}/comments")
async def list_message_comments(
    space_id: str,
    conversation_id: str,
    message_id: str,
    request: Request,
    context: GatewayContextDependency,
    service: GatewayServiceDependency,
) -> Response:
    """메시지 Comment 목록을 조회한다."""
    path = (
        f"/api/2.0/genie/spaces/{{upstream_id}}/conversations/{conversation_id}"
        f"/messages/{message_id}/comments"
    )
    return await service.proxy(
        request, context, "CHAT", space_id, "GET", path, conversation_id=conversation_id
    )


@router.get("/{space_id}/conversations/{conversation_id}/list-comments")
async def list_conversation_comments(
    space_id: str,
    conversation_id: str,
    request: Request,
    context: GatewayContextDependency,
    service: GatewayServiceDependency,
) -> Response:
    """대화 내 전체 Comment 목록을 조회한다."""
    path = f"/api/2.0/genie/spaces/{{upstream_id}}/conversations/{conversation_id}/list-comments"
    return await service.proxy(
        request, context, "CHAT", space_id, "GET", path, conversation_id=conversation_id
    )

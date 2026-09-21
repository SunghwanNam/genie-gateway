"""Gateway 인증·라우팅 도메인 모델."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class GatewayPrincipal:
    """인증된 외부 API 호출 주체."""

    api_clnt_id: int
    api_clnt_cd: str
    sys_id: int
    sys_cd: str


@dataclass(frozen=True, slots=True)
class GatewayRequestContext:
    """인증된 시스템과 외부 시스템이 보증한 사용자 식별값."""

    principal: GatewayPrincipal
    source_system: str
    user_id: str


@dataclass(frozen=True, slots=True)
class GenieRoute:
    """외부 논리 ID와 Databricks 실제 Agent의 매핑."""

    agt_id: int
    agt_cd: str
    agt_mode_cd: str
    public_agent_id: str
    databricks_host: str | None
    databricks_space_id: str | None
    databricks_agent_id: str | None
    credential_ref: str | None = None


@dataclass(frozen=True, slots=True)
class SupervisorRoute:
    """논리 Agent ID에서 확인된 Supervisor Serving Endpoint 정책."""

    agt_id: int
    agt_cd: str
    databricks_host: str | None
    serving_endpoint_name: str
    credential_ref: str | None
    long_task_enabled: bool
    first_event_timeout_seconds: int
    idle_timeout_seconds: int
    total_timeout_seconds: int
    max_continue_count: int
    max_active_run_count: int
    history_max_turn_count: int
    history_max_char_count: int


@dataclass(frozen=True, slots=True)
class ConversationOwner:
    """Gateway가 관리하는 Databricks Conversation 소유권."""

    conversation_pk: int
    databricks_conversation_id: str
    api_client_id: int
    user_id: str
    agent_id: int


@dataclass(frozen=True, slots=True)
class GenieAttachmentSnapshot:
    """Databricks Message Attachment의 정규화된 현재 상태."""

    sequence_no: int
    databricks_attachment_id: str | None
    attachment_type_code: str
    attachment_purpose_code: str | None
    text_content: str | None
    query_title: str | None
    query_content: str | None
    statement_id: str | None
    visualization_title: str | None
    raw_data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GenieMessageSnapshot:
    """Databricks Genie Message 응답의 정규화된 현재 상태."""

    conversation_id: str
    databricks_message_id: str
    role_code: str
    content: str | None
    status_code: str
    created_at: datetime | None
    updated_at: datetime | None
    error_code: str | None
    error_message: str | None
    raw_data: dict[str, Any]
    attachments: tuple[GenieAttachmentSnapshot, ...]


@dataclass(frozen=True, slots=True)
class ApiAuditRecord:
    """API 요청 감사 이력."""

    trace_id: str
    principal: GatewayPrincipal
    route: GenieRoute | None
    method: str
    request_uri: str
    response_status: int | None
    processing_ms: int
    source_ip: str | None
    conversation_id: int | None = None
    message_id: int | None = None
    databricks_request_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    request_data: str | None = None
    response_data: str | None = None

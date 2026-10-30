"""통합 AI Agent/Supervisor 공개 API 모델."""

from __future__ import annotations

from datetime import datetime
from difflib import get_close_matches
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SUPPORTED_METADATA_KEYS = frozenset({"channel", "locale", "timezone"})


class StrictRequest(BaseModel):
    """최상위 오타를 거절하고 가까운 공개 필드명을 안내한다."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def reject_unknown_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        known = set(cls.model_fields)
        unknown = sorted(set(value) - known)
        if not unknown:
            return value
        field = unknown[0]
        suggestion = get_close_matches(field, known, n=1, cutoff=0.65)
        suffix = f" Did you mean '{suggestion[0]}'?" if suggestion else ""
        raise ValueError(f"Unknown field '{field}'.{suffix}")


class UnifiedAgentResponseRequest(StrictRequest):
    """CUBE가 Agent 유형과 무관하게 사용하는 응답 생성 요청."""

    conversation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    input: str = Field(min_length=1, max_length=16_384)
    interaction_mode: Literal["CHAT", "AGENT"] | None = None
    client_message_id: str | None = Field(default=None, min_length=1, max_length=200)
    long_task: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_input(self) -> "UnifiedAgentResponseRequest":
        normalized = self.input.strip()
        if not normalized:
            raise ValueError("input must contain non-whitespace characters")
        self.input = normalized
        self.metadata = {
            key: value
            for key, value in self.metadata.items()
            if key in SUPPORTED_METADATA_KEYS
        }
        return self


class RunStatus(StrEnum):
    """Gateway Run 상태."""

    QUEUED = "QUEUED"
    STREAMING = "STREAMING"
    CONTINUING = "CONTINUING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    FAILED = "FAILED"


class ApprovalDecisionRequest(StrictRequest):
    """보류 중인 Supervisor 도구 실행에 대한 사용자 결정."""

    approve: bool


class AgentCapabilities(BaseModel):
    """외부 Agent 목록에 제공하는 기능 정보."""

    streaming: bool = True
    long_task: bool = False
    cancel: bool = True
    approval: bool = False


class UnifiedAgentSummary(BaseModel):
    """실제 Databricks 리소스를 숨긴 논리 Agent 정보."""

    agent_id: str
    name: str
    description: str | None = None
    agent_type: Literal["GENIE", "SUPERVISOR"]
    interaction_modes: list[Literal["CHAT", "AGENT", "SUPERVISOR"]]
    capabilities: AgentCapabilities
    active: bool
    registered_at: datetime
    released_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class RunError(BaseModel):
    """외부 공개 가능한 Run 오류."""

    error_code: str
    message: str
    retryable: bool = False
    request_id: str | None = None


class RunResponse(BaseModel):
    """Run 상태 조회 응답."""

    run_id: str
    conversation_id: str
    agent_id: str
    status: RunStatus
    started_at: datetime
    last_event_at: datetime | None = None
    completed_at: datetime | None = None
    error: RunError | None = None
    trace_id: str


class CancelRunResponse(BaseModel):
    """사용자 중단 접수 응답."""

    run_id: str
    conversation_id: str
    status: RunStatus
    cancel_requested_at: datetime | None = None
    trace_id: str


class RunItem(BaseModel):
    """완료된 정규화 출력 Item."""

    item_id: str
    sequence: int = Field(ge=1)
    item_type: str
    status: str
    title: str | None = None
    content: Any = None
    truncated: bool = False


class RunItemPage(BaseModel):
    """opaque cursor 기반 Item 목록."""

    items: list[RunItem]
    next_cursor: str | None = None


class SseEventEnvelope(BaseModel):
    """정규화 SSE data 공통 envelope."""

    schema_version: Literal["1.0"] = "1.0"
    event_id: str
    sequence: int = Field(ge=1)
    run_id: str
    conversation_id: str
    agent_id: str
    timestamp: datetime
    data: dict[str, Any]

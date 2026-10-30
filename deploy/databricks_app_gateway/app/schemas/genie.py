"""Databricks Genie 요청 호환 모델."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CompatibleRequest(BaseModel):
    """Databricks의 신규 필드를 손실 없이 허용하는 요청 기반 모델."""

    model_config = ConfigDict(extra="allow")


class StartConversationRequest(CompatibleRequest):
    """Chat mode 대화 시작 요청."""

    content: str = Field(min_length=1)
    enable_visualization: bool | None = None


class CreateMessageRequest(StartConversationRequest):
    """Chat mode 후속 메시지 요청."""


class MessageCommentRequest(CompatibleRequest):
    """메시지 Comment 요청."""

    content: str = Field(min_length=1, max_length=5_000)


class MessageFeedbackRequest(CompatibleRequest):
    """메시지 Feedback 요청."""

    rating: Literal[
        "GENIE_FEEDBACK_RATING_UNSPECIFIED",
        "POSITIVE",
        "NEGATIVE",
        "NONE",
    ]
    comment: str | None = Field(default=None, max_length=5_000)


class AgentModeResponseRequest(CompatibleRequest):
    """Agent mode SSE 응답 생성 요청."""

    input: list[dict[str, Any]] = Field(
        description="Exactly one Databricks Agent user message item.",
    )
    conversation_id: str | None = Field(
        default=None,
        description="Existing Agent Mode conversation ID. Omit to start a new conversation.",
    )
    stream: bool | None = Field(
        default=None,
        description=(
            "Databricks Agent streaming option. The Gateway response is SSE, but the "
            "upstream endpoint may emit completed items without token delta events."
        ),
    )
    enable_viz: bool | None = Field(
        default=None,
        description=(
            "Conditional visualization hint forwarded to Databricks Agent Mode. "
            "It does not guarantee chart creation or a PNG response."
        ),
    )

    @model_validator(mode="after")
    def validate_input(self) -> "AgentModeResponseRequest":
        if len(self.input) != 1:
            raise ValueError("input must contain exactly one message")
        item = self.input[0]
        if item.get("type") != "message" or item.get("role") != "user":
            raise ValueError("input item must be a user message")
        content = item.get("content")
        if not isinstance(content, list) or not content:
            raise ValueError("input message content is required")
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "input_text" or not part.get("text"):
                raise ValueError("content items must contain non-empty input_text")
        return self

"""Databricks Genie Chat 응답을 메시지 저장 스냅샷으로 변환한다."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from app.domain.models import GenieAttachmentSnapshot, GenieMessageSnapshot

_USER_CONTEXT_PREFIX = re.compile(r"^\[user_id: [^\]\r\n]+\]\r?\n")


def strip_user_context(value: Any) -> Any:
    """Gateway가 Genie 요청에 삽입한 사용자 컨텍스트를 응답/이력에서 제거한다."""
    if isinstance(value, str):
        return _USER_CONTEXT_PREFIX.sub("", value, count=1)
    if isinstance(value, list):
        return [strip_user_context(item) for item in value]
    if isinstance(value, dict):
        return {key: strip_user_context(item) for key, item in value.items()}
    return value


def map_chat_message_snapshots(payload: Any) -> list[GenieMessageSnapshot]:
    """Start/Create/Get/List Message 응답에서 Message들을 추출한다."""
    messages = _message_candidates(strip_user_context(payload))
    snapshots = [snapshot for item in messages if (snapshot := _map_message(item)) is not None]
    return sorted(
        snapshots,
        key=lambda item: (
            item.created_at or datetime.min.replace(tzinfo=UTC),
            item.databricks_message_id,
        ),
    )


def _message_candidates(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    nested_message = payload.get("message")
    if isinstance(nested_message, dict):
        return [nested_message]

    listed_messages = payload.get("messages")
    if isinstance(listed_messages, list):
        return [item for item in listed_messages if isinstance(item, dict)]

    message_id = payload.get("message_id") or payload.get("id")
    conversation_id = payload.get("conversation_id")
    if isinstance(message_id, str) and isinstance(conversation_id, str):
        return [payload]
    return []


def _map_message(message: dict[str, Any]) -> GenieMessageSnapshot | None:
    message_id = _non_empty_string(message.get("message_id") or message.get("id"))
    conversation_id = _non_empty_string(message.get("conversation_id"))
    if message_id is None or conversation_id is None:
        return None

    status = _non_empty_string(message.get("status")) or "SUBMITTED"
    error = message.get("error")
    error_code: str | None = None
    error_message: str | None = None
    if isinstance(error, dict):
        error_code = _non_empty_string(error.get("type") or error.get("error_code"))
        error_message = _non_empty_string(error.get("error") or error.get("message"))
    elif isinstance(error, str):
        error_message = error or None

    attachments_value = message.get("attachments")
    attachments: list[GenieAttachmentSnapshot] = []
    if isinstance(attachments_value, list):
        for sequence_no, attachment in enumerate(attachments_value, start=1):
            if isinstance(attachment, dict):
                attachments.append(_map_attachment(attachment, sequence_no))

    return GenieMessageSnapshot(
        conversation_id=conversation_id,
        databricks_message_id=message_id,
        role_code="USER",
        content=_non_empty_string(message.get("content")),
        status_code=status,
        created_at=_epoch_datetime(message.get("created_timestamp")),
        updated_at=_epoch_datetime(message.get("last_updated_timestamp")),
        error_code=error_code,
        error_message=error_message,
        raw_data=message,
        attachments=tuple(attachments),
    )


def _map_attachment(
    attachment: dict[str, Any],
    sequence_no: int,
) -> GenieAttachmentSnapshot:
    attachment_id = _non_empty_string(attachment.get("attachment_id") or attachment.get("id"))
    text_value = attachment.get("text")
    query_value = attachment.get("query")
    # 공식 Genie API는 시각화 객체를 `viz`로 반환한다. 기존 저장 데이터와의
    # 호환성을 위해 과거에 사용한 `visualization` 필드도 fallback으로 읽는다.
    visualization_value = attachment.get("viz") or attachment.get("visualization")
    suggested_questions = attachment.get("suggested_questions")

    attachment_type = "OTHER"
    purpose: str | None = None
    text_content: str | None = None
    query_title: str | None = None
    query_content: str | None = None
    statement_id: str | None = None
    visualization_title: str | None = None

    if isinstance(text_value, dict):
        attachment_type = "TEXT"
        purpose = _non_empty_string(text_value.get("purpose"))
        text_content = _non_empty_string(text_value.get("content"))
    elif isinstance(query_value, dict):
        attachment_type = "QUERY"
        query_title = _non_empty_string(query_value.get("title"))
        query_content = _non_empty_string(query_value.get("query"))
        statement_id = _non_empty_string(query_value.get("statement_id"))
    elif isinstance(visualization_value, dict):
        attachment_type = "VISUALIZATION"
        visualization_title = _non_empty_string(visualization_value.get("title"))
    elif suggested_questions is not None:
        attachment_type = "SUGGESTED_QUESTIONS"

    return GenieAttachmentSnapshot(
        sequence_no=sequence_no,
        databricks_attachment_id=attachment_id,
        attachment_type_code=attachment_type,
        attachment_purpose_code=purpose,
        text_content=text_content,
        query_title=query_title,
        query_content=query_content,
        statement_id=statement_id,
        visualization_title=visualization_title,
        raw_data=attachment,
    )


def _epoch_datetime(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    timestamp = float(value)
    if abs(timestamp) >= 100_000_000_000:
        timestamp /= 1_000
    try:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _non_empty_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None

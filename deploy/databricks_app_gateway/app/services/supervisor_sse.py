"""chunk 경계와 무관하게 동작하는 Supervisor SSE frame parser."""

from __future__ import annotations

from dataclasses import dataclass
import json
import codecs
import logging
from typing import Any


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SupervisorSseFrame:
    event: str | None
    data: str
    event_id: str | None = None
    retry: int | None = None


class SupervisorSseParser:
    """HTTP byte chunk를 완성된 SSE frame으로 조립한다."""

    def __init__(self) -> None:
        self._buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")()

    def feed(self, chunk: bytes) -> list[SupervisorSseFrame]:
        self._buffer += self._decoder.decode(chunk)
        normalized = self._buffer.replace("\r\n", "\n").replace("\r", "\n")
        blocks = normalized.split("\n\n")
        self._buffer = blocks.pop()
        return [frame for block in blocks if (frame := self._parse(block)) is not None]

    def finalize(self) -> list[SupervisorSseFrame]:
        self._buffer += self._decoder.decode(b"", final=True)
        block, self._buffer = self._buffer, ""
        frame = self._parse(block.replace("\r\n", "\n").replace("\r", "\n"))
        return [frame] if frame is not None else []

    @staticmethod
    def _parse(block: str) -> SupervisorSseFrame | None:
        if not block:
            return None
        event = None
        event_id = None
        retry = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "event":
                event = value
            elif field == "id":
                event_id = value
            elif field == "data":
                data_lines.append(value)
            elif field == "retry":
                try:
                    retry = int(value)
                except ValueError:
                    pass
        if not data_lines:
            return None
        return SupervisorSseFrame(event, "\n".join(data_lines), event_id, retry)


@dataclass(frozen=True, slots=True)
class NormalizedSupervisorEvent:
    event_type: str
    data: dict[str, Any]
    terminal: bool = False
    upstream_item: dict[str, Any] | None = None


def normalize_supervisor_frame(
    frame: SupervisorSseFrame,
) -> NormalizedSupervisorEvent | None:
    """Responses 계열 원본 frame을 Gateway 업무 이벤트로 축약한다."""
    try:
        payload = json.loads(frame.data)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    raw_type = str(payload.get("type") or frame.event or "")
    if raw_type == "response.output_text.delta":
        return NormalizedSupervisorEvent(
            "message.delta",
            {
                "item_id": payload.get("item_id"),
                "delta": payload.get("delta", ""),
            },
        )
    if raw_type in {"response.output_item.added", "response.output_item.done"}:
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        item_type = str(item.get("type") or "unknown")
        if item_type == "task_continue_request" and raw_type.endswith("done"):
            return NormalizedSupervisorEvent(
                "run.continuing",
                {
                    "step": item.get("step"),
                },
                upstream_item=item,
            )
        if item_type == "message" and raw_type.endswith("done"):
            return NormalizedSupervisorEvent(
                "message.completed",
                {"item_id": item.get("id"), "item": item},
                upstream_item=item,
            )
        if item_type == "mcp_approval_request" and raw_type.endswith("done"):
            logger.warning(
                "Supervisor approval item observed: raw_type=%s item_type=%s "
                "item_id=%s status=%s name=%s",
                raw_type,
                item_type,
                item.get("id"),
                item.get("status"),
                item.get("name") or item.get("tool_name"),
            )
            return NormalizedSupervisorEvent(
                "approval.required",
                {
                    "item_id": item.get("id"),
                    "approval_type": item_type,
                    "status": item.get("status"),
                    "name": item.get("name") or item.get("tool_name"),
                    "server_label": item.get("server_label"),
                    "arguments": item.get("arguments"),
                },
                upstream_item=item,
            )
        suffix = "completed" if raw_type.endswith("done") else "started"
        return NormalizedSupervisorEvent(
            f"tool.{suffix}",
            {
                "item_id": item.get("id"),
                "tool_type": item_type,
                "name": item.get("name"),
                "title": item.get("title"),
                "status": item.get("status"),
            },
            upstream_item=item,
        )
    if raw_type == "response.completed":
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        return NormalizedSupervisorEvent(
            "run.completed",
            {"status": "COMPLETED", "usage": response.get("usage")},
            True,
        )
    if raw_type == "response.failed":
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        return NormalizedSupervisorEvent(
            "run.failed",
            {"status": "FAILED", "error": response.get("error")},
            True,
        )
    if raw_type and raw_type not in {
        "response.created",
        "response.in_progress",
        "response.output_text.done",
        "response.content_part.added",
        "response.content_part.done",
    }:
        logger.info("Unmapped Supervisor SSE event observed: raw_type=%s", raw_type)
    return None

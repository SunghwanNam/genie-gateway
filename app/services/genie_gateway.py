"""Genie API 호환 프록시와 시스템별 라우팅 서비스."""

from __future__ import annotations

import json
import logging
import re
import time
from copy import deepcopy
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.clients.databricks_genie import DatabricksGenieClient
from app.core.config import Settings
from app.core.errors import GatewayError
from app.domain.models import (
    ApiAuditRecord,
    ConversationOwner,
    GatewayRequestContext,
    GenieRoute,
)
from app.mappers.genie_message_mapper import map_chat_message_snapshots, strip_user_context
from app.repositories.gateway_repository import GatewayRepository

logger = logging.getLogger(__name__)

_FORWARDED_RESPONSE_HEADERS = {
    "cache-control",
    "content-disposition",
    "content-encoding",
    "content-language",
    "content-length",
    "content-type",
    "etag",
    "last-modified",
    "location",
    "retry-after",
    "x-databricks-request-id",
    "x-request-id",
}


class _SseResponseCreatedObserver:
    """첫 response.created 이벤트에서 Agent conversation ID를 추출한다."""

    def __init__(self, callback: Callable[[str], Awaitable[None]]) -> None:
        self._callback = callback
        self._buffer = b""
        self._completed = False

    async def feed(self, chunk: bytes) -> None:
        if self._completed:
            return
        self._buffer += chunk
        # Databricks가 data JSON을 여러 줄로 나누거나 이벤트 경계와 별도 chunk로
        # 전송해도 response.created의 conversation_id를 즉시 등록한다.
        direct_match = re.search(
            rb'"conversation_id"\s*:\s*"([^"\\]+)"',
            self._buffer,
        )
        if direct_match:
            await self._callback(direct_match.group(1).decode("utf-8"))
            self._completed = True
            self._buffer = b""
            return
        while True:
            lf_index = self._buffer.find(b"\n\n")
            crlf_index = self._buffer.find(b"\r\n\r\n")
            indexes = [(lf_index, 2), (crlf_index, 4)]
            indexes = [(index, width) for index, width in indexes if index >= 0]
            if not indexes:
                # 비정상적으로 큰 첫 이벤트가 메모리를 계속 점유하지 않도록 제한한다.
                if len(self._buffer) > 1_048_576:
                    self._buffer = b""
                    self._completed = True
                return
            index, width = min(indexes, key=lambda item: item[0])
            event = self._buffer[:index]
            self._buffer = self._buffer[index + width :]
            conversation_id = self._conversation_id(event)
            if conversation_id:
                await self._callback(conversation_id)
                self._completed = True
                self._buffer = b""
                return

    @staticmethod
    def _conversation_id(event: bytes) -> str | None:
        lines = event.replace(b"\r\n", b"\n").split(b"\n")
        event_name = next(
            (line[6:].strip() for line in lines if line.startswith(b"event:")),
            b"",
        )
        data_lines = [line[5:].lstrip() for line in lines if line.startswith(b"data:")]
        if not data_lines:
            return None
        try:
            payload = json.loads(b"\n".join(data_lines))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if event_name and event_name != b"response.created":
            return None
        if not isinstance(payload, dict) or payload.get("type") not in {None, "response.created"}:
            return None
        response = payload.get("response")
        if not isinstance(response, dict):
            return None
        value = response.get("conversation_id")
        return value if isinstance(value, str) and value else None


def rewrite_public_agent_ids(
    value: Any,
    public_agent_id: str,
    databricks_space_id: str | None,
    databricks_agent_id: str | None,
) -> Any:
    """Databricks 실제 ID가 응답 계약 밖으로 노출되지 않도록 논리 ID로 치환한다."""
    if isinstance(value, list):
        return [
            rewrite_public_agent_ids(item, public_agent_id, databricks_space_id, databricks_agent_id)
            for item in value
        ]
    if not isinstance(value, dict):
        return value

    rewritten: dict[str, Any] = {}
    for key, item in value.items():
        if key == "space_id" and item == databricks_space_id:
            rewritten[key] = public_agent_id
        elif key == "agent_id" and item == databricks_agent_id:
            rewritten[key] = public_agent_id
        else:
            rewritten[key] = rewrite_public_agent_ids(
                item,
                public_agent_id,
                databricks_space_id,
                databricks_agent_id,
            )
    return rewritten


class GenieGatewayService:
    """Controller와 Databricks Client 사이의 유스케이스 흐름을 조정한다."""

    def __init__(
        self,
        settings: Settings,
        repository: GatewayRepository,
        databricks_client: DatabricksGenieClient,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._databricks_client = databricks_client

    async def proxy(
        self,
        request: Request,
        context: GatewayRequestContext,
        mode: str,
        public_agent_id: str,
        method: str,
        upstream_path_template: str,
        body: dict[str, object] | None = None,
        conversation_id: str | None = None,
        register_conversation: bool = False,
        delete_conversation: bool = False,
        include_user_context: bool = False,
        upstream_accept: str | None = None,
        conversation_title: str | None = None,
    ) -> Response:
        """Agent 매핑을 해석하고 Databricks 응답 계약을 보존해 반환한다."""
        started_at = time.perf_counter()
        trace_id = request.state.trace_id
        route: GenieRoute | None = None
        owner: ConversationOwner | None = None
        message_pk: int | None = None
        request_data = self._serialize_audit_payload(body)
        try:
            route = await self._repository.resolve_route(context.principal, public_agent_id, mode)
            if route is None:
                raise GatewayError(404, "NOT_FOUND", "The requested Genie agent is not available.")

            if conversation_id:
                owner = await self._repository.assert_conversation_owner(
                    context.principal,
                    route,
                    context.user_id,
                    conversation_id,
                    mode,
                )

            actual_agent_id = route.databricks_space_id if mode == "CHAT" else route.databricks_agent_id
            if actual_agent_id is None:
                raise GatewayError(500, "INVALID_STATE", "The Genie route is incomplete.")

            upstream_path = upstream_path_template.format(upstream_id=actual_agent_id)
            upstream_body = (
                self._add_user_context(body, context.user_id, mode)
                if include_user_context
                else body
            )
            upstream = await self._databricks_client.send(
                route=route,
                method=method,
                path=upstream_path,
                query_params=self._upstream_query_params(request),
                json_body=upstream_body,
                accept=upstream_accept or request.headers.get("accept"),
            )
            upstream_status = upstream.status_code
            stream_observer: _SseResponseCreatedObserver | None = None
            if (
                register_conversation
                and mode == "AGENT"
                and "text/event-stream" in upstream.headers.get("content-type", "").lower()
            ):
                logger.info(
                    "Observing Agent SSE trace_id=%s content_type=%s content_encoding=%s",
                    trace_id,
                    upstream.headers.get("content-type"),
                    upstream.headers.get("content-encoding"),
                )
                async def register_stream_conversation(created_id: str) -> None:
                    nonlocal owner
                    if owner is None:
                        owner = await self._repository.create_conversation_owner(
                            context.principal,
                            route,
                            context.user_id,
                            created_id,
                            title=conversation_title,
                            mode=mode,
                        )
                        logger.info(
                            "Registered Agent conversation trace_id=%s conversation_id=%s",
                            trace_id,
                            created_id,
                        )

                stream_observer = _SseResponseCreatedObserver(register_stream_conversation)
            response, response_payload = await self._build_response(
                upstream,
                route,
                trace_id,
                context.user_id,
                stream_observer.feed if stream_observer else None,
            )

            if 200 <= upstream_status < 300:
                if register_conversation and response_payload is not None:
                    created_id = self._conversation_id(response_payload)
                    if not created_id:
                        raise GatewayError(502, "INTERNAL_ERROR", "Databricks did not return a conversation_id.")
                    title = self._conversation_title(response_payload)
                    # Databricks 응답 user_id는 SP/Workspace 식별자이므로 소유권에 사용하지 않는다.
                    # 소유권 사용자는 인증된 외부 시스템이 Query Parameter로 보낸 값이다.
                    owner = await self._repository.create_conversation_owner(
                        context.principal,
                        route,
                        context.user_id,
                        created_id,
                        title=title if isinstance(title, str) else None,
                        mode=mode,
                    )
                elif owner and delete_conversation:
                    await self._repository.mark_conversation_deleted(
                        owner.conversation_pk,
                        context.principal.api_clnt_cd,
                    )
                elif owner:
                    await self._repository.touch_conversation(owner.conversation_pk)

                if mode == "CHAT" and owner:
                    message_pk = await self._repository.save_chat_messages(
                        owner,
                        map_chat_message_snapshots(response_payload),
                        context.principal.api_clnt_cd,
                    )

            await self._audit(
                request=request,
                context=context,
                route=route,
                owner=owner,
                status_code=upstream_status,
                started_at=started_at,
                request_data=request_data,
                response_data=self._serialize_audit_payload(response_payload),
                databricks_request_id=(
                    upstream.headers.get("x-databricks-request-id")
                    or upstream.headers.get("x-request-id")
                ),
                message_id=message_pk,
            )
            return response
        except GatewayError as exc:
            await self._audit(
                request=request,
                context=context,
                route=route,
                owner=owner,
                status_code=exc.status_code,
                started_at=started_at,
                request_data=request_data,
                error_code=exc.error_code,
                error_message=exc.message,
            )
            raise

    async def list_conversations(
        self,
        request: Request,
        context: GatewayRequestContext,
        public_agent_id: str,
    ) -> Response:
        """시스템 SP가 공유되더라도 현재 사용자의 Conversation만 목록으로 반환한다."""
        route = await self._repository.resolve_route(context.principal, public_agent_id, "CHAT")
        if route is None:
            raise GatewayError(404, "NOT_FOUND", "The requested Genie agent is not available.")
        try:
            page_size = int(request.query_params.get("page_size", "20"))
        except ValueError as exc:
            raise GatewayError(400, "INVALID_PARAMETER_VALUE", "page_size must be an integer.") from exc
        if page_size < 1 or page_size > 100:
            raise GatewayError(400, "INVALID_PARAMETER_VALUE", "page_size must be between 1 and 100.")
        if request.query_params.get("page_token"):
            raise GatewayError(400, "INVALID_PARAMETER_VALUE", "page_token is not supported yet.")
        conversations = await self._repository.list_user_conversations(
            context.principal,
            route,
            context.user_id,
            page_size,
        )
        return JSONResponse(
            content={"conversations": conversations},
            headers={"x-request-id": request.state.trace_id},
        )

    async def _build_response(
        self,
        upstream: httpx.Response,
        route: GenieRoute,
        trace_id: str,
        user_id: str | None = None,
        stream_observer: Callable[[bytes], Awaitable[None]] | None = None,
    ) -> tuple[Response, Any | None]:
        content_type = upstream.headers.get("content-type", "").lower()
        headers = self._response_headers(upstream, trace_id)

        if "application/json" in content_type:
            raw_content = await upstream.aread()
            await upstream.aclose()
            try:
                payload = json.loads(raw_content)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return (
                    Response(
                        content=raw_content,
                        status_code=upstream.status_code,
                        headers=headers,
                        media_type="application/json",
                    ),
                    None,
                )
            rewritten = rewrite_public_agent_ids(
                payload,
                route.public_agent_id,
                route.databricks_space_id,
                route.databricks_agent_id,
            )
            rewritten = strip_user_context(rewritten)
            headers.pop("content-type", None)
            headers.pop("content-length", None)
            headers.pop("content-encoding", None)
            return JSONResponse(content=rewritten, status_code=upstream.status_code, headers=headers), rewritten

        async def stream_body() -> AsyncIterator[bytes]:
            try:
                async def observed_chunks() -> AsyncIterator[bytes]:
                    # SSE 관찰 시에는 gzip/br 등이 해제된 bytes를 사용해야 JSON의
                    # conversation_id를 읽을 수 있다. 그 외 바이너리 응답은 원문을 보존한다.
                    chunks = upstream.aiter_bytes() if stream_observer is not None else upstream.aiter_raw()
                    async for chunk in chunks:
                        if stream_observer is not None:
                            await stream_observer(chunk)
                        yield chunk

                if user_id is None:
                    async for chunk in observed_chunks():
                        yield chunk
                else:
                    needles = (
                        f"[user_id: {user_id}]\n".encode(),
                        f"[user_id: {user_id}]\\n".encode(),
                    )
                    async for chunk in self._strip_stream_values(observed_chunks(), needles):
                        yield chunk
            finally:
                await upstream.aclose()

        headers.pop("content-length", None)
        if stream_observer is not None:
            # aiter_bytes()가 이미 압축을 해제했으므로 브라우저의 이중 해제를 방지한다.
            headers.pop("content-encoding", None)
        return (
            StreamingResponse(
                stream_body(),
                status_code=upstream.status_code,
                headers=headers,
                media_type=None,
            ),
            None,
        )

    @staticmethod
    def _add_user_context(
        body: dict[str, object] | None,
        user_id: str,
        mode: str,
    ) -> dict[str, object] | None:
        """외부 요청 객체를 변경하지 않고 Genie 질의에 사용자 컨텍스트를 추가한다."""
        if body is None:
            return None
        enriched = deepcopy(body)
        prefix = f"[user_id: {user_id}]\n"
        if mode == "CHAT":
            content = enriched.get("content")
            if isinstance(content, str):
                enriched["content"] = prefix + content
            return enriched

        input_items = enriched.get("input")
        if not isinstance(input_items, list) or not input_items:
            return enriched
        first_item = input_items[0]
        if not isinstance(first_item, dict):
            return enriched
        content_items = first_item.get("content")
        if not isinstance(content_items, list):
            return enriched
        for part in content_items:
            if isinstance(part, dict) and part.get("type") == "input_text":
                text = part.get("text")
                if isinstance(text, str):
                    part["text"] = prefix + text
                    break
        return enriched

    @staticmethod
    async def _strip_stream_values(
        chunks: AsyncIterator[bytes],
        needles: tuple[bytes, ...],
    ) -> AsyncIterator[bytes]:
        """Chunk 경계를 넘어오는 SSE 문자열에서도 내부 사용자 컨텍스트를 제거한다."""
        buffer = b""
        keep = max(len(needle) for needle in needles) - 1
        async for chunk in chunks:
            buffer += chunk
            while True:
                matches = [(buffer.find(needle), needle) for needle in needles]
                matches = [(index, needle) for index, needle in matches if index >= 0]
                if matches:
                    index, needle = min(matches, key=lambda item: item[0])
                    if index:
                        yield buffer[:index]
                    buffer = buffer[index + len(needle) :]
                    continue
                if len(buffer) > keep:
                    yield buffer[:-keep]
                    buffer = buffer[-keep:]
                break
        if buffer:
            yield buffer

    @staticmethod
    def _upstream_query_params(request: Request) -> list[tuple[str, str]]:
        return [
            (key, value)
            for key, value in request.query_params.multi_items()
            if key not in {"source_system", "user_id"}
        ]

    @staticmethod
    def _conversation_id(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        value = payload.get("conversation_id") or payload.get("id")
        if not value and isinstance(payload.get("conversation"), dict):
            value = payload["conversation"].get("conversation_id") or payload["conversation"].get("id")
        if not value and isinstance(payload.get("message"), dict):
            value = payload["message"].get("conversation_id")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _conversation_title(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        value = payload.get("title")
        if not value and isinstance(payload.get("conversation"), dict):
            value = payload["conversation"].get("title")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _response_headers(upstream: httpx.Response, trace_id: str) -> dict[str, str]:
        headers = {
            key.lower(): value
            for key, value in upstream.headers.items()
            if key.lower() in _FORWARDED_RESPONSE_HEADERS
        }
        headers["x-request-id"] = trace_id
        return headers

    def _serialize_audit_payload(self, payload: Any) -> str | None:
        if not self._settings.audit_payloads or payload is None:
            return None
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return serialized if len(serialized.encode("utf-8")) <= 65_536 else None

    async def _audit(
        self,
        request: Request,
        context: GatewayRequestContext,
        route: GenieRoute | None,
        owner: ConversationOwner | None,
        status_code: int,
        started_at: float,
        request_data: str | None,
        response_data: str | None = None,
        databricks_request_id: str | None = None,
        message_id: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        processing_ms = max(round((time.perf_counter() - started_at) * 1_000), 0)
        source_ip = request.client.host if request.client else None
        request_uri = request.url.path
        if request.url.query:
            request_uri = f"{request_uri}?{request.url.query}"
        record = ApiAuditRecord(
            trace_id=request.state.trace_id,
            principal=context.principal,
            route=route,
            method=request.method,
            request_uri=request_uri,
            response_status=status_code,
            processing_ms=processing_ms,
            source_ip=source_ip,
            conversation_id=owner.conversation_pk if owner else None,
            message_id=message_id,
            databricks_request_id=databricks_request_id,
            error_code=error_code,
            error_message=error_message,
            request_data=request_data,
            response_data=response_data,
        )
        try:
            await self._repository.record_api_audit(record)
        except Exception:
            logger.exception("Failed to write API audit trace_id=%s", request.state.trace_id)

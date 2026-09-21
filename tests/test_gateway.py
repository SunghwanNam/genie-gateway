from __future__ import annotations

import gzip
import json
import unittest

import httpx
from fastapi import FastAPI
from pydantic import ValidationError
from starlette.requests import Request

from app.api.custom.admin import AgentBody, SupervisorLimitsBody
from app.clients.databricks_auth import DatabricksTokenProvider, normalize_databricks_host
from app.clients.databricks_genie import DatabricksGenieClient
from app.api.dependencies import get_gateway_request_context
from app.core.config import Settings
from app.core.errors import GatewayError
from app.core.security import get_current_principal, hash_gateway_credential
from app.domain.models import (
    ApiAuditRecord,
    ConversationOwner,
    GatewayPrincipal,
    GatewayRequestContext,
    GenieRoute,
)
from app.main import app
from app.mappers.genie_message_mapper import map_chat_message_snapshots, strip_user_context
from app.repositories.gateway_repository import GatewayRepository
from app.schemas.genie import AgentModeResponseRequest
from app.services.genie_gateway import (
    GenieGatewayService,
    _SseResponseCreatedObserver,
    rewrite_public_agent_ids,
)
from app.services.agent_report_pdf import build_agent_report_pdf


class _FakeRepository:
    def __init__(self, route: GenieRoute | None) -> None:
        self.route = route
        self.audits: list[ApiAuditRecord] = []
        self.created_owners: list[ConversationOwner] = []
        self.touched_owners: list[int] = []
        self.saved_messages: list[object] = []

    async def resolve_route(
        self,
        principal: GatewayPrincipal,
        public_agent_id: str,
        mode: str,
    ) -> GenieRoute | None:
        del principal, public_agent_id, mode
        return self.route

    async def record_api_audit(self, record: ApiAuditRecord) -> None:
        self.audits.append(record)

    async def create_conversation_owner(
        self,
        principal: GatewayPrincipal,
        route: GenieRoute,
        user_id: str,
        conversation_id: str,
        title: str | None = None,
        mode: str = "CHAT",
    ) -> ConversationOwner:
        del title, mode
        owner = ConversationOwner(100, conversation_id, principal.api_clnt_id, user_id, route.agt_id)
        self.created_owners.append(owner)
        return owner

    async def assert_conversation_owner(
        self,
        principal: GatewayPrincipal,
        route: GenieRoute,
        user_id: str,
        conversation_id: str,
        mode: str | None = None,
    ) -> ConversationOwner:
        del mode
        if user_id != "10001" or conversation_id != "conversation-1":
            raise GatewayError(403, "PERMISSION_DENIED", "The conversation does not belong to this user.")
        return ConversationOwner(100, conversation_id, principal.api_clnt_id, user_id, route.agt_id)

    async def touch_conversation(self, conversation_pk: int) -> None:
        self.touched_owners.append(conversation_pk)

    async def save_chat_messages(
        self,
        owner: ConversationOwner,
        messages: list[object],
        registration_id: str,
    ) -> int | None:
        del owner, registration_id
        self.saved_messages.extend(messages)
        return 200 if messages else None


def _request(path: str, query: bytes = b"") -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": [(b"accept", b"application/json")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "app": FastAPI(),
    }
    request = Request(scope)
    request.state.trace_id = "trace-test-001"
    return request


class _OAuthPrincipalRepository:
    async def authenticate_by_oauth_subject(self, oauth_subject: str) -> GatewayPrincipal | None:
        if oauth_subject == "cube-service-principal-id":
            return GatewayPrincipal(2, "CUBE_APP_OAUTH", 1, "CUBE")
        return None


class _ApiKeyPrincipalRepository:
    async def authenticate_by_key_hash(self, key_hash: str) -> GatewayPrincipal | None:
        if key_hash == hash_gateway_credential("cube-api-key"):
            return GatewayPrincipal(1, "CUBE_API", 1, "CUBE")
        return None


class ContractTests(unittest.TestCase):
    def test_supervisor_limits_allow_parallel_conversations_and_validate_hierarchy(self) -> None:
        limits = SupervisorLimitsBody()
        self.assertEqual(limits.gateway_max_active_runs, 40)
        self.assertEqual(limits.user_max_active_runs, 10)
        self.assertEqual(limits.user_agent_max_active_runs, 5)
        with self.assertRaises(ValidationError):
            SupervisorLimitsBody(
                gateway_max_active_runs=40,
                user_max_active_runs=4,
                user_agent_max_active_runs=5,
            )

    def test_conversation_mode_parameter_has_explicit_postgres_type(self) -> None:
        import inspect

        source = inspect.getsource(GatewayRepository.assert_conversation_owner)
        self.assertIn("CAST(:mode AS VARCHAR(20)) IS NULL", source)
        self.assertIn("cnvs_mode_cd = CAST(:mode AS VARCHAR(20))", source)

    def test_agent_sse_observer_accepts_multiline_chunked_json(self) -> None:
        captured: list[str] = []

        async def capture(conversation_id: str) -> None:
            captured.append(conversation_id)

        async def exercise() -> None:
            observer = _SseResponseCreatedObserver(capture)
            await observer.feed(b'event: response.created\ndata:\n{\n  "type": "response.created",\n')
            await observer.feed(b'  "response": {"conversation_id": "agent-conversation-1"}\n}\n\n')

        import asyncio

        asyncio.run(exercise())
        self.assertEqual(captured, ["agent-conversation-1"])

    def test_agent_report_pdf_is_generated_from_items(self) -> None:
        payload = {
            "items": [
                {"id": "answer", "type": "message", "content": [
                    {"type": "output_text", "text": "분석 결과입니다."}
                ]},
                {"id": "table", "type": "function_call_output", "output": (
                    "| 연도 | 비율 |\n|---|---|\n| 2025 | 12.5% |"
                )},
            ]
        }
        result = build_agent_report_pdf(payload, "conversation-1")
        self.assertTrue(result.startswith(b"%PDF-"))
        self.assertGreater(len(result), 1_000)

    def test_admin_agent_body_validates_ids_by_mode(self) -> None:
        space_id = "a" * 32
        agent_id = "b" * 32

        self.assertEqual(
            AgentBody(system_code="CUBE", name="Chat", mode="CHAT", space_id=space_id).mode,
            "CHAT",
        )
        self.assertEqual(
            AgentBody(system_code="CUBE", name="Agent", mode="AGENT", agent_id=agent_id).mode,
            "AGENT",
        )
        self.assertEqual(
            AgentBody(
                system_code="CUBE",
                name="Both",
                mode="BOTH",
                space_id=space_id,
                agent_id=agent_id,
            ).mode,
            "BOTH",
        )
        with self.assertRaises(ValidationError):
            AgentBody(system_code="CUBE", name="Missing", mode="AGENT", space_id=space_id)

    def test_admin_agent_body_accepts_supervisor_endpoint(self) -> None:
        valid = AgentBody.model_validate(
            {
                "system_code": "CUBE",
                "name": "Healthcare Supervisor",
                "agent_type": "SUPERVISOR",
                "mode": "SUPERVISOR",
                "serving_endpoint_name": "mas-healthcare-endpoint",
                "long_task_enabled": True,
            }
        )

        self.assertEqual(valid.serving_endpoint_name, "mas-healthcare-endpoint")
        self.assertTrue(valid.long_task_enabled)

        with self.assertRaises(ValidationError):
            AgentBody.model_validate(
                {
                    "system_code": "CUBE",
                    "name": "Broken Supervisor",
                    "agent_type": "SUPERVISOR",
                    "mode": "CHAT",
                    "serving_endpoint_name": "mas-broken-endpoint",
                }
            )

    def test_official_genie_endpoint_inventory(self) -> None:
        routes = {
            (method, route.path)
            for route in app.routes
            for method in getattr(route, "methods", set())
            if route.path.startswith("/api/2.0/genie/")
        }
        expected = {
            ("POST", "/api/2.0/genie/spaces/{space_id}/start-conversation"),
            ("GET", "/api/2.0/genie/spaces/{space_id}/conversations"),
            ("DELETE", "/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}"),
            ("POST", "/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages"),
            ("GET", "/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages"),
            ("GET", "/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages/{message_id}"),
            ("DELETE", "/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages/{message_id}"),
            ("POST", "/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages/{message_id}/feedback"),
            ("POST", "/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages/{message_id}/comments"),
            ("GET", "/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages/{message_id}/comments"),
            ("GET", "/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/list-comments"),
            ("GET", "/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages/{message_id}/attachments/{attachment_id}/query-result"),
            ("POST", "/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages/{message_id}/attachments/{attachment_id}/execute-query"),
            ("POST", "/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages/{message_id}/attachments/{attachment_id}/downloads"),
            ("GET", "/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages/{message_id}/attachments/{attachment_id}/downloads/{download_id}"),
            ("GET", "/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages/{message_id}/attachments/{attachment_id}/download-visualization"),
            ("POST", "/api/2.0/genie/agents/{agent_id}/responses"),
            ("POST", "/api/2.0/genie/agents/{agent_id}/conversations/{conversation_id}/responses/{response_id}/cancel"),
            ("GET", "/api/2.0/genie/agents/{agent_id}/conversations/{conversation_id}/items"),
        }
        self.assertEqual(routes, expected)

    def test_openapi_exposes_only_gateway_api_key_security_scheme(self) -> None:
        schema = app.openapi()
        security_schemes = schema["components"]["securitySchemes"]
        self.assertEqual(security_schemes["GatewayApiKey"]["name"], "X-API-Key")
        self.assertEqual(security_schemes["GatewayApiKey"]["in"], "header")
        self.assertNotIn("GatewayBearer", security_schemes)

        operation = schema["paths"][
            "/api/2.0/genie/spaces/{space_id}/start-conversation"
        ]["post"]
        self.assertIn({"GatewayApiKey": []}, operation["security"])
        header_parameters = {
            parameter["name"]
            for parameter in operation.get("parameters", [])
            if parameter.get("in") == "header"
        }
        self.assertFalse({"authorization", "X-API-Key"} & header_parameters)
        query_parameters = {
            parameter["name"]
            for parameter in operation.get("parameters", [])
            if parameter.get("in") == "query"
        }
        self.assertTrue({"source_system", "user_id"} <= query_parameters)

    def test_custom_chat_routes_are_hidden_from_openapi(self) -> None:
        route_paths = {route.path for route in app.routes}
        self.assertIn("/custom/chat", route_paths)
        self.assertIn("/custom/chat/assets/{asset_name}", route_paths)
        self.assertNotIn("/custom/chat", app.openapi()["paths"])
        self.assertNotIn("/custom/chat/assets/{asset_name}", app.openapi()["paths"])

    def test_agent_mode_openapi_documents_conditional_visualization(self) -> None:
        schema = app.openapi()["components"]["schemas"]["AgentModeResponseRequest"]
        properties = schema["properties"]
        self.assertIn("stream", properties)
        self.assertIn("enable_viz", properties)
        self.assertIn("without token delta", properties["stream"]["description"])
        self.assertIn("does not guarantee", properties["enable_viz"]["description"])

    def test_agent_mode_requires_exactly_one_user_message(self) -> None:
        with self.assertRaises(ValueError):
            AgentModeResponseRequest(input=[])

    def test_response_ids_are_rewritten_recursively(self) -> None:
        payload = {
            "space_id": "actual-space",
            "nested": [{"agent_id": "actual-agent"}],
            "unrelated": "actual-space",
        }
        self.assertEqual(
            rewrite_public_agent_ids(payload, "sample-agent", "actual-space", "actual-agent"),
            {
                "space_id": "sample-agent",
                "nested": [{"agent_id": "sample-agent"}],
                "unrelated": "actual-space",
            },
        )

    def test_host_normalization_rejects_unsafe_values(self) -> None:
        self.assertEqual(
            normalize_databricks_host("example.cloud.databricks.com/"),
            "https://example.cloud.databricks.com",
        )
        for value in (
            "http://example.cloud.databricks.com",
            "https://user:password@example.cloud.databricks.com",
            "https://example.cloud.databricks.com/api",
        ):
            with self.subTest(value=value), self.assertRaises(GatewayError):
                normalize_databricks_host(value)

    def test_schema_identifier_is_validated(self) -> None:
        with self.assertRaises(ValueError):
            GatewayRepository(database=object(), schema="genie_gw; DROP SCHEMA public")  # type: ignore[arg-type]

    def test_credential_hash_is_deterministic_and_not_plaintext(self) -> None:
        hashed = hash_gateway_credential("gateway-test-secret")
        self.assertEqual(len(hashed), 64)
        self.assertNotIn("gateway-test-secret", hashed)

    def test_chat_message_mapper_normalizes_official_response(self) -> None:
        snapshots = map_chat_message_snapshots(
            {
                "conversation": {"id": "conversation-1", "title": "매출 질문"},
                "message": {
                    "conversation_id": "conversation-1",
                    "message_id": "message-1",
                    "content": "전월과 비교해줘",
                    "status": "COMPLETED",
                    "created_timestamp": 1_719_769_718,
                    "last_updated_timestamp": 1_719_769_720_000,
                    "attachments": [
                        {
                            "attachment_id": "attachment-text-1",
                            "text": {
                                "content": "전월 대비 매출이 증가했습니다.",
                                "purpose": "TEXT_ATTACHMENT_PURPOSE_ANSWER",
                            },
                        },
                        {
                            "attachment_id": "attachment-query-1",
                            "query": {
                                "title": "월별 매출 비교",
                                "query": "SELECT month, SUM(amount) FROM sales GROUP BY month",
                                "statement_id": "statement-1",
                            },
                        },
                        {
                            "attachment_id": "attachment-viz-1",
                            "viz": {
                                "title": "월별 매출 추이",
                                "query_attachment_id": "attachment-query-1",
                            },
                        },
                    ],
                },
            }
        )

        self.assertEqual(len(snapshots), 1)
        message = snapshots[0]
        self.assertEqual(message.databricks_message_id, "message-1")
        self.assertEqual(message.content, "전월과 비교해줘")
        self.assertEqual(message.status_code, "COMPLETED")
        self.assertEqual(message.created_at.isoformat(), "2024-06-30T17:48:38+00:00")
        self.assertEqual(message.updated_at.isoformat(), "2024-06-30T17:48:40+00:00")
        self.assertEqual(message.attachments[0].attachment_type_code, "TEXT")
        self.assertEqual(message.attachments[0].text_content, "전월 대비 매출이 증가했습니다.")
        self.assertEqual(message.attachments[1].attachment_type_code, "QUERY")
        self.assertIn("SELECT month", message.attachments[1].query_content)
        self.assertEqual(message.attachments[2].attachment_type_code, "VISUALIZATION")
        self.assertEqual(message.attachments[2].visualization_title, "월별 매출 추이")

    def test_chat_message_mapper_handles_message_list_and_ignores_other_payloads(self) -> None:
        snapshots = map_chat_message_snapshots(
            {
                "messages": [
                    {
                        "conversation_id": "conversation-1",
                        "message_id": "message-2",
                        "content": "두 번째 질문",
                        "status": "SUBMITTED",
                    },
                    {
                        "conversation_id": "conversation-1",
                        "message_id": "message-1",
                        "content": "첫 번째 질문",
                        "status": "COMPLETED",
                    },
                ]
            }
        )
        self.assertEqual({item.databricks_message_id for item in snapshots}, {"message-1", "message-2"})
        self.assertEqual(map_chat_message_snapshots({"statement_response": {"status": "SUCCEEDED"}}), [])

    def test_user_context_is_removed_from_response_and_snapshot_raw_data(self) -> None:
        payload = {
            "conversation_id": "conversation-1",
            "message_id": "message-1",
            "content": "[user_id: 10001]\n내가 작성한 글을 찾아줘",
            "nested": {"text": "[user_id: 10001]\n내가 작성한 글을 찾아줘"},
        }
        cleaned = strip_user_context(payload)
        self.assertEqual(cleaned["content"], "내가 작성한 글을 찾아줘")
        self.assertEqual(cleaned["nested"]["text"], "내가 작성한 글을 찾아줘")

        snapshot = map_chat_message_snapshots(payload)[0]
        self.assertEqual(snapshot.content, "내가 작성한 글을 찾아줘")
        self.assertEqual(snapshot.raw_data["content"], "내가 작성한 글을 찾아줘")


class AsyncGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_visualization_proxy_forces_binary_accept_and_preserves_png_body(self) -> None:
        seen: dict[str, object] = {}
        png = b"\x89PNG\r\n\x1a\nmock-png"

        class PngStream(httpx.AsyncByteStream):
            async def __aiter__(self):  # type: ignore[no-untyped-def]
                yield png

        def handler(request: httpx.Request) -> httpx.Response:
            seen["accept"] = request.headers.get("accept")
            return httpx.Response(
                200,
                stream=PngStream(),
                headers={
                    "content-type": "application/octet-stream",
                    "content-disposition": 'attachment; filename="visualization.png"',
                },
            )

        settings = Settings(databricks_token="test-databricks-token")
        route = GenieRoute(
            10,
            "CUBE_SALES",
            "CHAT",
            "sample-agent",
            "https://workspace.cloud.databricks.com",
            "actual-space-id",
            None,
        )
        repository = _FakeRepository(route)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DatabricksTokenProvider(settings, client)
            service = GenieGatewayService(
                settings,
                repository,  # type: ignore[arg-type]
                DatabricksGenieClient(settings, client, provider),
            )
            response = await service.proxy(
                request=_request("/custom/chat/api/visualization"),
                context=GatewayRequestContext(GatewayPrincipal(1, "CUBE_API", 1, "CUBE"), "CUBE", "10001"),
                mode="CHAT",
                public_agent_id="sample-agent",
                method="GET",
                upstream_path_template=(
                    "/api/2.0/genie/spaces/{upstream_id}/conversations/conversation-1/"
                    "messages/message-1/attachments/attachment-viz-1/download-visualization"
                ),
                conversation_id="conversation-1",
                upstream_accept="application/octet-stream",
            )
            body = b"".join([chunk async for chunk in response.body_iterator])

        self.assertEqual(seen["accept"], "application/octet-stream")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/octet-stream")
        self.assertEqual(body, png)

    async def test_service_routes_to_actual_space_and_hides_actual_id(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["authorization"] = request.headers["authorization"]
            seen["body"] = json.loads(request.content)
            compressed = gzip.compress(
                json.dumps(
                    {
                        "space_id": "actual-space-id",
                        "conversation_id": "conversation-1",
                        "message_id": "message-1",
                        "status": "SUBMITTED",
                        "content": "[user_id: 10001]\n매출을 알려줘",
                        "user_id": 99999,
                    }
                ).encode()
            )
            return httpx.Response(
                200,
                content=compressed,
                headers={
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                    "x-databricks-request-id": "dbx-request-1",
                },
            )

        settings = Settings(databricks_token="test-databricks-token", audit_payloads=True)
        route = GenieRoute(
            agt_id=10,
            agt_cd="CUBE_SALES",
            agt_mode_cd="BOTH",
            public_agent_id="sample-agent",
            databricks_host="https://workspace.cloud.databricks.com",
            databricks_space_id="actual-space-id",
            databricks_agent_id="actual-agent-id",
        )
        repository = _FakeRepository(route)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            token_provider = DatabricksTokenProvider(settings, client)
            dbx_client = DatabricksGenieClient(settings, client, token_provider)
            service = GenieGatewayService(settings, repository, dbx_client)  # type: ignore[arg-type]
            principal = GatewayPrincipal(1, "CUBE_API", 1, "CUBE")
            context = GatewayRequestContext(principal, "CUBE", "10001")
            response = await service.proxy(
                request=_request(
                    "/api/2.0/genie/spaces/sample-agent/start-conversation",
                    b"source_system=CUBE&user_id=10001&test_parameter=value",
                ),
                context=context,
                mode="CHAT",
                public_agent_id="sample-agent",
                method="POST",
                upstream_path_template="/api/2.0/genie/spaces/{upstream_id}/start-conversation",
                body={"content": "매출을 알려줘"},
                register_conversation=True,
                include_user_context=True,
            )

        self.assertEqual(
            seen["url"],
            "https://workspace.cloud.databricks.com/api/2.0/genie/spaces/actual-space-id/start-conversation?test_parameter=value",
        )
        self.assertEqual(seen["authorization"], "Bearer test-databricks-token")
        self.assertEqual(seen["body"], {"content": "[user_id: 10001]\n매출을 알려줘"})
        self.assertEqual(
            json.loads(response.body),
            {
                "space_id": "sample-agent",
                "conversation_id": "conversation-1",
                "message_id": "message-1",
                "status": "SUBMITTED",
                "content": "매출을 알려줘",
                "user_id": 99999,
            },
        )
        self.assertNotIn("content-encoding", response.headers)
        self.assertEqual(len(repository.audits), 1)
        self.assertEqual(repository.audits[0].request_data, '{"content":"매출을 알려줘"}')
        self.assertEqual(repository.audits[0].databricks_request_id, "dbx-request-1")
        self.assertEqual(repository.created_owners[0].user_id, "10001")
        self.assertEqual(repository.created_owners[0].databricks_conversation_id, "conversation-1")
        self.assertEqual(len(repository.saved_messages), 1)
        self.assertEqual(repository.saved_messages[0].content, "매출을 알려줘")
        self.assertEqual(repository.saved_messages[0].raw_data["content"], "매출을 알려줘")

    async def test_agent_user_context_is_added_and_removed_from_stream(self) -> None:
        seen: dict[str, object] = {}

        class SplitStream(httpx.AsyncByteStream):
            async def __aiter__(self):  # type: ignore[no-untyped-def]
                yield b'data: {"text":"[user_'
                yield 'id: 10001]\\n내가 작성한 글을 찾아줘"}\n\n'.encode()

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                stream=SplitStream(),
                headers={"content-type": "text/event-stream"},
            )

        settings = Settings(databricks_token="test-databricks-token")
        route = GenieRoute(
            10,
            "CUBE_SALES",
            "AGENT",
            "sample-agent",
            "https://workspace.cloud.databricks.com",
            None,
            "actual-agent-id",
        )
        repository = _FakeRepository(route)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DatabricksTokenProvider(settings, client)
            service = GenieGatewayService(
                settings,
                repository,  # type: ignore[arg-type]
                DatabricksGenieClient(settings, client, provider),
            )
            original_body = {
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "내가 작성한 글을 찾아줘"}],
                    }
                ]
            }
            response = await service.proxy(
                request=_request("/api/2.0/genie/agents/sample-agent/responses"),
                context=GatewayRequestContext(
                    GatewayPrincipal(1, "CUBE_API", 1, "CUBE"),
                    "CUBE",
                    "10001",
                ),
                mode="AGENT",
                public_agent_id="sample-agent",
                method="POST",
                upstream_path_template="/api/2.0/genie/agents/{upstream_id}/responses",
                body=original_body,
                include_user_context=True,
            )
            streamed = b"".join([chunk async for chunk in response.body_iterator])

        expected_input = seen["body"]["input"][0]["content"][0]["text"]
        self.assertEqual(expected_input, "[user_id: 10001]\n내가 작성한 글을 찾아줘")
        self.assertEqual(
            original_body["input"][0]["content"][0]["text"],
            "내가 작성한 글을 찾아줘",
        )
        self.assertNotIn(b"user_id", streamed)
        self.assertIn("내가 작성한 글을 찾아줘".encode(), streamed)

    async def test_agent_stream_registers_conversation_before_created_event_is_forwarded(self) -> None:
        class CreatedStream(httpx.AsyncByteStream):
            async def __aiter__(self):  # type: ignore[no-untyped-def]
                yield (
                    b'event: response.created\n'
                    b'data: {"type":"response.created","response":'
                    b'{"id":"response-1","conversation_id":"conversation-1"}}\n\n'
                )

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                stream=CreatedStream(),
                headers={"content-type": "text/event-stream"},
            )

        settings = Settings(databricks_token="test-databricks-token")
        route = GenieRoute(10, "CUBE_SALES", "AGENT", "sample-agent", "https://workspace.cloud.databricks.com", None, "actual-agent-id")
        repository = _FakeRepository(route)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = GenieGatewayService(
                settings,
                repository,  # type: ignore[arg-type]
                DatabricksGenieClient(settings, client, DatabricksTokenProvider(settings, client)),
            )
            response = await service.proxy(
                request=_request("/api/2.0/genie/agents/sample-agent/responses"),
                context=GatewayRequestContext(GatewayPrincipal(1, "CUBE_API", 1, "CUBE"), "CUBE", "10001"),
                mode="AGENT",
                public_agent_id="sample-agent",
                method="POST",
                upstream_path_template="/api/2.0/genie/agents/{upstream_id}/responses",
                body={"input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "질문"}]}]},
                register_conversation=True,
            )
            streamed = b"".join([chunk async for chunk in response.body_iterator])

        self.assertIn(b"response.created", streamed)
        self.assertEqual(repository.created_owners[0].databricks_conversation_id, "conversation-1")

    async def test_compressed_agent_stream_is_decoded_before_conversation_observation(self) -> None:
        event = (
            b'event: response.created\n'
            b'data: {"type":"response.created","response":'
            b'{"id":"response-1","conversation_id":"conversation-gzip"}}\n\n'
        )

        class CompressedStream(httpx.AsyncByteStream):
            async def __aiter__(self):  # type: ignore[no-untyped-def]
                yield gzip.compress(event)

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                stream=CompressedStream(),
                headers={
                    "content-type": "text/event-stream",
                    "content-encoding": "gzip",
                },
            )

        settings = Settings(databricks_token="test-databricks-token")
        route = GenieRoute(10, "CUBE_SALES", "AGENT", "sample-agent", "https://workspace.cloud.databricks.com", None, "actual-agent-id")
        repository = _FakeRepository(route)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = GenieGatewayService(
                settings,
                repository,  # type: ignore[arg-type]
                DatabricksGenieClient(settings, client, DatabricksTokenProvider(settings, client)),
            )
            response = await service.proxy(
                request=_request("/api/2.0/genie/agents/sample-agent/responses"),
                context=GatewayRequestContext(GatewayPrincipal(1, "CUBE_API", 1, "CUBE"), "CUBE", "10001"),
                mode="AGENT",
                public_agent_id="sample-agent",
                method="POST",
                upstream_path_template="/api/2.0/genie/agents/{upstream_id}/responses",
                body={"input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "질문"}]}]},
                register_conversation=True,
            )
            streamed = b"".join([chunk async for chunk in response.body_iterator])

        self.assertEqual(streamed, event)
        self.assertNotIn("content-encoding", response.headers)
        self.assertEqual(
            repository.created_owners[0].databricks_conversation_id,
            "conversation-gzip",
        )

    async def test_cancel_agent_response_routes_to_upstream_after_owner_check(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"status": "CANCELLED"})

        settings = Settings(databricks_token="test-databricks-token")
        route = GenieRoute(10, "CUBE_SALES", "AGENT", "sample-agent", "https://workspace.cloud.databricks.com", None, "actual-agent-id")
        repository = _FakeRepository(route)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = GenieGatewayService(
                settings,
                repository,  # type: ignore[arg-type]
                DatabricksGenieClient(settings, client, DatabricksTokenProvider(settings, client)),
            )
            response = await service.proxy(
                request=_request("/api/2.0/genie/agents/sample-agent/conversations/conversation-1/responses/response-1/cancel"),
                context=GatewayRequestContext(GatewayPrincipal(1, "CUBE_API", 1, "CUBE"), "CUBE", "10001"),
                mode="AGENT",
                public_agent_id="sample-agent",
                method="POST",
                upstream_path_template="/api/2.0/genie/agents/{upstream_id}/conversations/conversation-1/responses/response-1/cancel",
                conversation_id="conversation-1",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            seen["url"],
            "https://workspace.cloud.databricks.com/api/2.0/genie/agents/actual-agent-id/conversations/conversation-1/responses/response-1/cancel",
        )
        self.assertEqual(repository.touched_owners, [100])

    async def test_content_is_not_enriched_without_explicit_option(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"status": "OK"})

        settings = Settings(databricks_token="test-databricks-token")
        route = GenieRoute(
            10,
            "CUBE_SALES",
            "CHAT",
            "sample-agent",
            "https://workspace.cloud.databricks.com",
            "actual-space-id",
            None,
        )
        repository = _FakeRepository(route)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DatabricksTokenProvider(settings, client)
            service = GenieGatewayService(
                settings,
                repository,  # type: ignore[arg-type]
                DatabricksGenieClient(settings, client, provider),
            )
            await service.proxy(
                request=_request("/api/2.0/genie/spaces/sample-agent/comments"),
                context=GatewayRequestContext(
                    GatewayPrincipal(1, "CUBE_API", 1, "CUBE"),
                    "CUBE",
                    "10001",
                ),
                mode="CHAT",
                public_agent_id="sample-agent",
                method="POST",
                upstream_path_template="/api/2.0/genie/spaces/{upstream_id}/comments",
                body={"content": "좋은 답변입니다."},
            )

        self.assertEqual(seen["body"], {"content": "좋은 답변입니다."})

    async def test_message_read_response_does_not_expose_user_context(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "conversation_id": "conversation-1",
                    "message_id": "message-1",
                    "content": "[user_id: 10001]\n내가 작성한 글을 찾아줘",
                },
            )

        settings = Settings(databricks_token="test-databricks-token")
        route = GenieRoute(
            10,
            "CUBE_SALES",
            "CHAT",
            "sample-agent",
            "https://workspace.cloud.databricks.com",
            "actual-space-id",
            None,
        )
        repository = _FakeRepository(route)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DatabricksTokenProvider(settings, client)
            service = GenieGatewayService(
                settings,
                repository,  # type: ignore[arg-type]
                DatabricksGenieClient(settings, client, provider),
            )
            response = await service.proxy(
                request=_request(
                    "/api/2.0/genie/spaces/sample-agent/conversations/conversation-1/messages/message-1"
                ),
                context=GatewayRequestContext(
                    GatewayPrincipal(1, "CUBE_API", 1, "CUBE"),
                    "CUBE",
                    "10001",
                ),
                mode="CHAT",
                public_agent_id="sample-agent",
                method="GET",
                upstream_path_template=(
                    "/api/2.0/genie/spaces/{upstream_id}/conversations/"
                    "conversation-1/messages/message-1"
                ),
                conversation_id="conversation-1",
            )

        self.assertEqual(json.loads(response.body)["content"], "내가 작성한 글을 찾아줘")
        self.assertEqual(repository.saved_messages[0].content, "내가 작성한 글을 찾아줘")

    async def test_request_source_system_must_match_authenticated_client(self) -> None:
        principal = GatewayPrincipal(1, "CUBE_API", 1, "CUBE")
        context = await get_gateway_request_context(principal, "cube", "10001")
        self.assertEqual(context.source_system, "CUBE")
        with self.assertRaises(GatewayError) as raised:
            await get_gateway_request_context(principal, "PORTAL", "10001")
        self.assertEqual(raised.exception.status_code, 403)

    async def test_conversation_owner_is_checked_before_databricks_call(self) -> None:
        upstream_called = False

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal upstream_called
            upstream_called = True
            return httpx.Response(200, json={})

        settings = Settings(databricks_token="test-databricks-token")
        route = GenieRoute(
            10,
            "CUBE_SALES",
            "CHAT",
            "sample-agent",
            "https://workspace.cloud.databricks.com",
            "actual-space-id",
            None,
        )
        repository = _FakeRepository(route)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DatabricksTokenProvider(settings, client)
            service = GenieGatewayService(
                settings,
                repository,  # type: ignore[arg-type]
                DatabricksGenieClient(settings, client, provider),
            )
            context = GatewayRequestContext(
                GatewayPrincipal(1, "CUBE_API", 1, "CUBE"),
                "CUBE",
                "10002",
            )
            with self.assertRaises(GatewayError) as raised:
                await service.proxy(
                    request=_request("/api/2.0/genie/spaces/sample-agent/conversations/conversation-1/messages"),
                    context=context,
                    mode="CHAT",
                    public_agent_id="sample-agent",
                    method="GET",
                    upstream_path_template=(
                        "/api/2.0/genie/spaces/{upstream_id}/conversations/conversation-1/messages"
                    ),
                    conversation_id="conversation-1",
                )
        self.assertEqual(raised.exception.status_code, 403)
        self.assertFalse(upstream_called)

    async def test_databricks_apps_oauth_uses_forwarded_principal(self) -> None:
        request = _request("/api/2.0/genie/spaces/sample-agent/start-conversation")
        request.scope["headers"].append((b"x-forwarded-user", b"cube-service-principal-id"))
        request.app.state.settings = Settings(gateway_auth_mode="DATABRICKS_APPS_OAUTH")
        request.app.state.gateway_repository = _OAuthPrincipalRepository()
        principal = await get_current_principal(request, x_api_key=None)
        self.assertEqual(principal.api_clnt_cd, "CUBE_APP_OAUTH")

    async def test_api_key_mode_requires_x_api_key_header(self) -> None:
        request = _request("/api/2.0/genie/spaces/sample-agent/start-conversation")
        request.app.state.settings = Settings(gateway_auth_mode="API_KEY")
        request.app.state.gateway_repository = _ApiKeyPrincipalRepository()

        with self.assertRaises(GatewayError) as raised:
            await get_current_principal(request, x_api_key=None)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.message, "X-API-Key header is required.")

        principal = await get_current_principal(request, x_api_key="cube-api-key")
        self.assertEqual(principal.api_clnt_cd, "CUBE_API")

    async def test_oauth_token_is_cached(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            self.assertEqual(request.url.path, "/oidc/v1/token")
            return httpx.Response(200, json={"access_token": "oauth-token", "expires_in": 3600})

        settings = Settings(
            databricks_client_id="client-id",
            databricks_client_secret="client-secret",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DatabricksTokenProvider(settings, client)
            first = await provider.get_token("https://workspace.cloud.databricks.com")
            second = await provider.get_token("https://workspace.cloud.databricks.com")

        self.assertEqual(first, "oauth-token")
        self.assertEqual(second, "oauth-token")
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()

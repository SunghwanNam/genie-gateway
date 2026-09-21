"""통합 Agent/Supervisor 공개 모델 명세 테스트."""

from __future__ import annotations

import unittest

import httpx
from pydantic import ValidationError

from app.schemas.supervisor import RunStatus, UnifiedAgentResponseRequest
from app.api.v1.agents import list_agents
from app.clients.databricks_supervisor import DatabricksSupervisorClient
from app.core.config import Settings
from app.domain.models import GatewayPrincipal, GatewayRequestContext, SupervisorRoute
from app.services.supervisor_sse import SupervisorSseParser
from app.services.supervisor_sse import normalize_supervisor_frame, SupervisorSseFrame
from app.services.supervisor_adapter import (
    build_approval_response_request,
    build_continuation_request,
    build_supervisor_request,
)
from app.services.run_orchestrator import RunOrchestrator
from app.services.run_orchestrator import PreparedRun
from app.services.run_execution_manager import RunExecutionManager
from app.repositories.run_repository import RunRecord, RunRepository
from app.core.database import Database
from datetime import datetime, timedelta, timezone


class SupervisorContractTests(unittest.TestCase):
    def test_supervisor_sse_parser_handles_chunked_multiline_frames(self) -> None:
        parser = SupervisorSseParser()
        self.assertEqual(parser.feed(b"id: upstream-1\nevent: response.output_text.delta\nda"), [])
        frames = parser.feed(b'ta: {"delta":\ndata: "hello"}\r\n\r\n: ping\r\n\r\n')

        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].event_id, "upstream-1")
        self.assertEqual(frames[0].event, "response.output_text.delta")
        self.assertEqual(frames[0].data, '{"delta":\n"hello"}')

    def test_supervisor_sse_parser_handles_split_utf8_character(self) -> None:
        parser = SupervisorSseParser()
        encoded = 'data: {"delta":"안녕"}\n\n'.encode("utf-8")
        split_at = encoded.index("안".encode("utf-8")) + 1
        self.assertEqual(parser.feed(encoded[:split_at]), [])
        frames = parser.feed(encoded[split_at:])
        self.assertEqual(frames[0].data, '{"delta":"안녕"}')

    def test_supervisor_sse_parser_finalizes_terminal_frame_without_blank_line(self) -> None:
        parser = SupervisorSseParser()
        self.assertEqual(
            parser.feed(
                b'event: response.completed\n'
                b'data: {"type":"response.completed"}'
            ),
            [],
        )

        frames = parser.finalize()

        self.assertEqual(len(frames), 1)
        normalized = normalize_supervisor_frame(frames[0])
        self.assertEqual(normalized.event_type, "run.completed")
        self.assertTrue(normalized.terminal)

    def test_supervisor_event_normalizer_maps_delta_and_terminal(self) -> None:
        delta = normalize_supervisor_frame(
            SupervisorSseFrame(
                "response.output_text.delta",
                '{"type":"response.output_text.delta","item_id":"i1","delta":"안녕"}',
            )
        )
        completed = normalize_supervisor_frame(
            SupervisorSseFrame(
                "response.completed",
                '{"type":"response.completed","response":{"usage":{"total_tokens":10}}}',
            )
        )

        self.assertEqual(delta.event_type, "message.delta")
        self.assertEqual(delta.data["delta"], "안녕")
        self.assertEqual(completed.event_type, "run.completed")
        self.assertTrue(completed.terminal)

    def test_supervisor_event_normalizer_maps_mcp_approval_request(self) -> None:
        approval = normalize_supervisor_frame(
            SupervisorSseFrame(
                "response.output_item.done",
                '{"type":"response.output_item.done","item":'
                '{"type":"mcp_approval_request","id":"approval_1",'
                '"status":"completed","name":"web_search",'
                '"arguments":"{\\"query\\":\\"public statistics\\"}"}}',
            )
        )

        self.assertEqual(approval.event_type, "approval.required")
        self.assertEqual(approval.data["approval_type"], "mcp_approval_request")
        self.assertEqual(approval.data["name"], "web_search")

    def test_approval_adapter_returns_request_and_decision_in_input(self) -> None:
        payload = build_approval_response_request(
            [{"role": "user", "content": "최신 통계를 찾아줘"}],
            {"type": "mcp_approval_request", "id": "toolu_1", "name": "web_search"},
            True,
            allow_long_task=False,
        )
        self.assertEqual(payload["input"][-2]["id"], "toolu_1")
        self.assertEqual(payload["input"][-1]["type"], "mcp_approval_response")
        self.assertTrue(payload["input"][-1]["approve"])

    def test_supervisor_adapter_builds_minimal_payload(self) -> None:
        request = UnifiedAgentResponseRequest(input="현재 질문", long_task=True)
        payload = build_supervisor_request(
            request,
            [
                {"role": "user", "content": "이전 질문"},
                {"role": "assistant", "content": "이전 답변"},
                {"role": "tool", "content": "전달 금지"},
            ],
            allow_long_task=True,
        )

        self.assertEqual(len(payload["input"]), 3)
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["databricks_options"]["long_task"], True)
        self.assertNotIn("metadata", payload)

    def test_continuation_adapter_matches_request_id(self) -> None:
        payload = build_continuation_request(
            [{"role": "user", "content": "분석해줘"}],
            {"type": "task_continue_request", "id": "continue_1", "step": 16},
        )

        self.assertNotIn("input", payload)
        self.assertEqual(payload["messages"][-2]["id"], "continue_1")
        self.assertEqual(
            payload["messages"][-1]["continue_request_id"], "continue_1"
        )
        self.assertTrue(payload["databricks_options"]["long_task"])

    def test_unknown_top_level_field_is_rejected_with_suggestion(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            UnifiedAgentResponseRequest.model_validate(
                {"input": "분석해줘", "long_taks": True}
            )

        self.assertIn("Unknown field 'long_taks'", str(caught.exception))
        self.assertIn("Did you mean 'long_task'", str(caught.exception))

    def test_unsupported_metadata_is_removed(self) -> None:
        request = UnifiedAgentResponseRequest.model_validate(
            {
                "input": "  분석해줘  ",
                "metadata": {
                    "locale": "ko-KR",
                    "channel": "CUBE",
                    "unsupported_key": "do-not-forward",
                },
            }
        )

        self.assertEqual(request.input, "분석해줘")
        self.assertEqual(
            request.metadata,
            {"locale": "ko-KR", "channel": "CUBE"},
        )

    def test_request_rejects_blank_input(self) -> None:
        with self.assertRaises(ValidationError):
            UnifiedAgentResponseRequest.model_validate({"input": "   "})

    def test_run_status_contains_terminal_and_active_states(self) -> None:
        self.assertEqual(RunStatus.STREAMING.value, "STREAMING")
        self.assertEqual(RunStatus.COMPLETED.value, "COMPLETED")
        self.assertEqual(RunStatus.TIMED_OUT.value, "TIMED_OUT")


class UnifiedAgentApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_execution_manager_replays_events_after_last_event_id(self) -> None:
        now = datetime.now(timezone.utc)
        route = SupervisorRoute(1, "SUP", None, "endpoint", None, True, 2, 2, 30, 2, 15, 20, 40000)
        prepared = PreparedRun(
            route,
            RunRecord(1, "run_1", 2, "cnvs_1", "COMPLETED", "trace", now, now + timedelta(seconds=30), True),
            {"input": [], "stream": True},
        )

        class Runs:
            async def resolve_event_sequence(self, run_pk, event_id):
                return 1

            async def list_events_after(self, run_pk, after, limit=200):
                if after >= 2:
                    return []
                return [{"evt_uid": "evt_2", "evt_seq_no": 2, "evt_typ_cd": "run.completed", "evt_data": {"status": "COMPLETED"}, "reg_dt": now}]

            async def get_run_status(self, run_pk):
                return "COMPLETED"

        class Client:
            async def send(self, route, payload):
                raise AssertionError("completed Run must not execute again")

        manager = RunExecutionManager(Runs(), Client())
        events = [event async for event in manager.subscribe(prepared, "evt_1")]
        self.assertEqual([event["id"] for event in events], ["evt_2"])

    async def test_execution_manager_continues_long_task_in_same_run(self) -> None:
        now = datetime.now(timezone.utc)
        route = SupervisorRoute(
            1, "HEALTHCARE_SUPERVISOR", None, "mas-healthcare-endpoint", None,
            True, 2, 2, 30, 2, 15, 20, 40000,
        )
        prepared = PreparedRun(
            route,
            RunRecord(
                1, "run_1", 2, "cnvs_1", "QUEUED", "trace",
                now, now + timedelta(seconds=30),
            ),
            {
                "input": [{"role": "user", "content": "분석해줘"}],
                "stream": True,
                "databricks_options": {"long_task": True},
            },
        )

        class Runs:
            def __init__(self):
                self.sequence = 0
                self.transitions = []
                self.continuations = []
                self.events = []
                self.status = "QUEUED"

            async def append_event(self, run_pk, event_type, data, registration_id, upstream_event_id=None):
                self.sequence += 1
                self.events.append({"evt_uid": f"evt_{self.sequence}", "evt_seq_no": self.sequence, "evt_typ_cd": event_type, "evt_data": data, "reg_dt": datetime.now(timezone.utc)})
                return f"evt_{self.sequence}", self.sequence

            async def transition_run(self, run_pk, current, target, modifier, **kwargs):
                self.transitions.append((current, target))
                self.status = target
                return True

            async def begin_continuation(self, run_pk, request_id, maximum, modifier):
                self.continuations.append((request_id, maximum))
                self.status = "CONTINUING"
                return len(self.continuations)

            async def claim_execution(self, *args):
                return True

            async def renew_execution_lease(self, *args):
                return True

            async def resolve_event_sequence(self, run_pk, event_id):
                return int(event_id.removeprefix("evt_"))

            async def list_events_after(self, run_pk, after, limit=200):
                return [event for event in self.events if event["evt_seq_no"] > after][:limit]

            async def get_run_status(self, run_pk):
                return self.status

            async def is_cancel_requested(self, run_pk):
                return False

            async def save_item(self, *args, **kwargs):
                return None

        class Client:
            def __init__(self):
                self.payloads = []

            async def send(self, route, payload):
                self.payloads.append(payload)
                request = httpx.Request("POST", "https://dbc.example.com/invocations")
                if len(self.payloads) == 1:
                    content = (
                        b'event: response.output_item.done\n'
                        b'data: {"type":"response.output_item.done","item":'
                        b'{"type":"task_continue_request","id":"continue_1","step":16}}\n\n'
                    )
                else:
                    content = (
                        b'event: response.completed\n'
                        b'data: {"type":"response.completed"}\n\n'
                    )
                return httpx.Response(200, request=request, content=content)

        runs = Runs()
        client = Client()
        manager = RunExecutionManager(runs, client)
        events = [event async for event in manager.subscribe(prepared)]

        self.assertEqual(
            [event["event"] for event in events],
            ["run.started", "run.continuing", "run.completed"],
        )
        self.assertEqual(runs.continuations, [("continue_1", 2)])
        self.assertEqual(events[1]["data"]["data"]["continue_count"], 1)
        self.assertNotIn("continue_request", events[1]["data"]["data"])
        self.assertEqual(client.payloads[1]["messages"][-2]["id"], "continue_1")
        self.assertEqual(
            client.payloads[1]["messages"][-1]["continue_request_id"],
            "continue_1",
        )
        self.assertEqual(
            runs.transitions,
            [
                ("QUEUED", "STREAMING"),
                ("CONTINUING", "STREAMING"),
                ("STREAMING", "COMPLETED"),
            ],
        )

    async def test_execution_manager_persists_and_streams_terminal_event(self) -> None:
        now = datetime.now(timezone.utc)
        route = SupervisorRoute(
            1, "HEALTHCARE_SUPERVISOR", None, "mas-healthcare-endpoint", None,
            True, 2, 2, 30, 20, 15, 20, 40000,
        )
        prepared = PreparedRun(
            route,
            RunRecord(
                1, "run_1", 2, "cnvs_1", "QUEUED", "trace",
                now, now + timedelta(seconds=30),
            ),
            {"input": [], "stream": True},
        )

        class Runs:
            def __init__(self):
                self.sequence = 0
                self.transitions = []
                self.events = []
                self.status = "QUEUED"

            async def append_event(self, run_pk, event_type, data, registration_id, upstream_event_id=None):
                self.sequence += 1
                self.events.append({"evt_uid": f"evt_{self.sequence}", "evt_seq_no": self.sequence, "evt_typ_cd": event_type, "evt_data": data, "reg_dt": datetime.now(timezone.utc)})
                return f"evt_{self.sequence}", self.sequence

            async def transition_run(self, run_pk, current, target, modifier, **kwargs):
                self.transitions.append((current, target))
                self.status = target
                return True

            async def claim_execution(self, *args):
                return True

            async def renew_execution_lease(self, *args):
                return True

            async def resolve_event_sequence(self, run_pk, event_id):
                return int(event_id.removeprefix("evt_"))

            async def list_events_after(self, run_pk, after, limit=200):
                return [event for event in self.events if event["evt_seq_no"] > after][:limit]

            async def get_run_status(self, run_pk):
                return self.status

            async def is_cancel_requested(self, run_pk):
                return False

            async def save_item(self, *args, **kwargs):
                self.saved_item = args

        class Client:
            async def send(self, route, payload):
                request = httpx.Request("POST", "https://dbc.example.com/invocations")
                return httpx.Response(
                    200,
                    request=request,
                    content=(
                        b'event: response.output_text.delta\n'
                        b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                        b'event: response.output_item.done\n'
                        b'data: {"type":"response.output_item.done","item":'
                        b'{"id":"msg_1","type":"message","content":'
                        b'[{"type":"output_text","text":"ok"}]}}'
                    ),
                )

        runs = Runs()
        manager = RunExecutionManager(runs, Client())
        events = [event async for event in manager.subscribe(prepared)]

        self.assertEqual(
            [event["event"] for event in events],
            ["run.started", "message.delta", "message.completed", "run.completed"],
        )
        self.assertEqual(
            runs.transitions,
            [("QUEUED", "STREAMING"), ("STREAMING", "COMPLETED")],
        )

    async def test_run_repository_rejects_invalid_terminal_transition(self) -> None:
        repository = RunRepository(Database(None))
        with self.assertRaises(ValueError):
            await repository.transition_run(1, "COMPLETED", "FAILED", "test")

    async def test_orchestrator_prepares_stable_deduplicated_run(self) -> None:
        route = SupervisorRoute(
            1, "HEALTHCARE_SUPERVISOR", None, "mas-healthcare-endpoint", None,
            True, 90, 180, 1800, 20, 15, 20, 40000,
        )

        class GatewayRepository:
            async def resolve_supervisor_route(self, principal, agent_id):
                return route

        class Runs:
            async def create_or_reuse_run(self, *args):
                self.args = args
                now = datetime.now(timezone.utc)
                return RunRecord(1, "run_1", 2, "cnvs_1", "QUEUED", "trace", now, now + timedelta(minutes=30))

            async def load_conversation_history(self, *args):
                return [{"role": "assistant", "content": "이전 답변"}]

            async def save_item(self, *args, **kwargs):
                self.saved = args

        runs = Runs()
        orchestrator = RunOrchestrator(Settings(), GatewayRepository(), runs)
        principal = GatewayPrincipal(1, "CUBE_API", 10, "CUBE")
        context = GatewayRequestContext(principal, "CUBE", "user-1")
        prepared = await orchestrator.prepare(
            context,
            "HEALTHCARE_SUPERVISOR",
            UnifiedAgentResponseRequest(input="  이번 달   분석 ", long_task=True),
            "trace",
            "cube-request-1",
        )

        self.assertEqual(prepared.run.run_uid, "run_1")
        self.assertEqual(prepared.upstream_payload["input"][-2]["content"], "이전 답변")
        self.assertEqual(prepared.upstream_payload["input"][-1]["content"], "이번 달   분석")
        self.assertEqual(len(runs.args[4]), 64)
        self.assertEqual(runs.saved[1], "user.message")

    async def test_supervisor_client_uses_registered_endpoint_without_leaking_to_api(self) -> None:
        captured: dict[str, object] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["accept"] = request.headers.get("accept")
            captured["authorization"] = request.headers.get("authorization")
            return httpx.Response(200, headers={"content-type": "text/event-stream"})

        class TokenProvider:
            async def get_token(self, host, credential_ref=None):
                captured["token_host"] = host
                captured["credential_ref"] = credential_ref
                return "test-token"

            def invalidate(self, host, credential_ref=None):
                raise AssertionError("token must not be invalidated")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            supervisor_client = DatabricksSupervisorClient(
                Settings(databricks_host="https://dbc.example.com"),
                client,
                TokenProvider(),
            )
            route = SupervisorRoute(
                1,
                "HEALTHCARE_SUPERVISOR",
                None,
                "mas-healthcare-endpoint",
                None,
                True,
                90,
                180,
                1800,
                20,
                15,
                20,
                40000,
            )
            response = await supervisor_client.send(route, {"input": [], "stream": True})
            await response.aclose()

        self.assertEqual(
            captured["url"],
            "https://dbc.example.com/serving-endpoints/mas-healthcare-endpoint/invocations",
        )
        self.assertEqual(captured["accept"], "text/event-stream")
        self.assertEqual(captured["authorization"], "Bearer test-token")

    async def test_agent_list_exposes_capabilities_without_upstream_resource(self) -> None:
        class FakeRepository:
            async def list_available_agents(self, principal):
                self.principal = principal
                return [
                    {
                        "agt_cd": "HEALTHCARE_SUPERVISOR",
                        "agt_nm": "헬스케어 분석 Agent",
                        "agt_desc_cn": "건강 데이터를 분석합니다.",
                        "agt_typ_cd": "SUPERVISOR",
                        "agt_mode_cd": "SUPERVISOR",
                        "long_task_use_yn": "Y",
                        "dbx_serving_ep_nm": "must-not-leak",
                    }
                ]

        principal = GatewayPrincipal(1, "CUBE_API", 10, "CUBE")
        context = GatewayRequestContext(principal, "CUBE", "user-1")
        result = await list_agents(context, FakeRepository())
        payload = result[0].model_dump()

        self.assertEqual(payload["agent_id"], "HEALTHCARE_SUPERVISOR")
        self.assertEqual(payload["agent_type"], "SUPERVISOR")
        self.assertTrue(payload["capabilities"]["long_task"])
        self.assertNotIn("dbx_serving_ep_nm", payload)


if __name__ == "__main__":
    unittest.main()

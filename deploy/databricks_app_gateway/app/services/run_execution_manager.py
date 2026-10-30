"""HTTP 연결과 분리된 Supervisor Run 실행 및 SSE fan-out."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
import logging
import socket
from typing import Any
from uuid import uuid4

from app.clients.databricks_supervisor import DatabricksSupervisorClient
from app.core.errors import GatewayError
from app.repositories.run_repository import (
    PAUSED_RUN_STATUSES,
    RunRepository,
    TERMINAL_RUN_STATUSES,
)
from app.services.run_orchestrator import PreparedRun
from app.services.supervisor_adapter import build_continuation_request
from app.services.supervisor_sse import (
    SupervisorSseFrame,
    SupervisorSseParser,
    normalize_supervisor_frame,
)


_END = object()
logger = logging.getLogger(__name__)


class RunExecutionManager:
    """프로세스 내 실행 Task와 구독자를 관리하고 상태는 DB에 기록한다."""

    def __init__(self, repository: RunRepository, client: DatabricksSupervisorClient) -> None:
        self._repository = repository
        self._client = client
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._subscribers: dict[str, set[asyncio.Queue[object]]] = {}
        self._lock = asyncio.Lock()
        self._owner_id = f"{socket.gethostname()}-{uuid4().hex}"
        self._lease_seconds = 30

    async def subscribe(
        self,
        prepared: PreparedRun,
        last_event_id: str | None = None,
        skip_existing: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[object] = asyncio.Queue(maxsize=256)
        run_uid = prepared.run.run_uid
        after_sequence = await self._repository.latest_event_sequence(prepared.run.run_pk) if skip_existing else (
            await self._repository.resolve_event_sequence(
                prepared.run.run_pk, last_event_id
            )
            if last_event_id
            else 0
        )
        async with self._lock:
            self._subscribers.setdefault(run_uid, set()).add(queue)
            task = self._tasks.get(run_uid)
            if task is None and not prepared.run.reused:
                task = asyncio.create_task(self._execute(prepared), name=f"supervisor-{run_uid}")
                self._tasks[run_uid] = task
        try:
            while True:
                rows = await self._repository.list_events_after(
                    prepared.run.run_pk, after_sequence
                )
                for row in rows:
                    after_sequence = int(row["evt_seq_no"])
                    yield self._stored_envelope(prepared, row)
                status = await self._repository.get_run_status(prepared.run.run_pk)
                if status in (*TERMINAL_RUN_STATUSES, *PAUSED_RUN_STATUSES) and not rows:
                    break
                try:
                    value = await asyncio.wait_for(queue.get(), timeout=1.0)
                    if value is _END and status in (*TERMINAL_RUN_STATUSES, *PAUSED_RUN_STATUSES):
                        break
                except asyncio.TimeoutError:
                    pass
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(run_uid)
                if subscribers:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(run_uid, None)

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _execute(self, prepared: PreparedRun) -> None:
        run = prepared.run
        current = "QUEUED"
        terminal_sent = False
        payload = prepared.upstream_payload
        source_messages = payload.get("input", payload.get("messages", []))
        messages = [dict(item) for item in source_messages]
        try:
            if not await self._repository.claim_execution(
                run.run_pk, self._owner_id, self._lease_seconds
            ):
                return
            next_lease_renewal = asyncio.get_running_loop().time() + 10.0
            event_id, sequence = await self._repository.append_event(
                run.run_pk, "run.started", {"status": "STREAMING"}, run.trace_id
            )
            await self._broadcast(run.run_uid, self._envelope(prepared, "run.started", sequence, event_id, {"status": "STREAMING"}))
            if not await self._repository.transition_run(run.run_pk, "QUEUED", "STREAMING", run.trace_id):
                return
            current = "STREAMING"
            while True:
                response = await self._client.send(prepared.route, payload)
                if response.status_code >= 400:
                    await response.aread()
                    raise RuntimeError(f"upstream HTTP {response.status_code}")
                parser = SupervisorSseParser()
                iterator = response.aiter_bytes().__aiter__()
                first = True
                next_chunk: asyncio.Task[bytes] | None = None
                continuation: dict[str, Any] | None = None
                completed_message_seen = False

                async def process_frame(frame: SupervisorSseFrame) -> bool:
                    """원본 SSE frame 하나를 저장·중계하고 terminal 여부를 반환한다."""
                    nonlocal continuation, current, messages, terminal_sent, completed_message_seen
                    normalized = normalize_supervisor_frame(frame)
                    if normalized is None:
                        return False
                    if current == "CONTINUING":
                        moved = await self._repository.transition_run(
                            run.run_pk,
                            "CONTINUING",
                            "STREAMING",
                            run.trace_id,
                        )
                        if not moved:
                            if await self._repository.is_cancel_requested(run.run_pk):
                                await self._cancel(prepared)
                                terminal_sent = True
                                return True
                            raise RuntimeError("continuation state transition failed")
                        current = "STREAMING"
                    if normalized.event_type in {"message.completed", "tool.completed"}:
                        await self._repository.save_item(
                            run.run_pk,
                            normalized.event_type,
                            normalized.data,
                            run.trace_id,
                            normalized.data.get("item_id"),
                        )
                        if normalized.upstream_item is not None:
                            messages.append(normalized.upstream_item)
                        if normalized.event_type == "message.completed":
                            completed_message_seen = True
                    if normalized.event_type == "approval.required":
                        approval_request = normalized.upstream_item
                        if approval_request is None:
                            raise GatewayError(
                                502,
                                "UPSTREAM_PROTOCOL_ERROR",
                                "Approval request payload is missing.",
                            )
                        approval_uid = await self._repository.create_pending_approval(
                            run.run_pk,
                            approval_request,
                            messages,
                            run.trace_id,
                        )
                        current = "WAITING_APPROVAL"
                        normalized.data["approval_id"] = approval_uid
                        await self._emit(
                            prepared,
                            normalized.event_type,
                            normalized.data,
                            frame.event_id,
                        )
                        return True
                    if normalized.event_type == "run.continuing":
                        if not prepared.route.long_task_enabled:
                            raise GatewayError(
                                502,
                                "UPSTREAM_PROTOCOL_ERROR",
                                "Unexpected continuation request was received.",
                            )
                        continuation = normalized.upstream_item
                        if continuation is None:
                            raise GatewayError(
                                502,
                                "UPSTREAM_PROTOCOL_ERROR",
                                "Continuation request payload is missing.",
                            )
                        continue_request_id = continuation.get("id")
                        if not isinstance(continue_request_id, str) or not continue_request_id:
                            raise GatewayError(
                                502,
                                "UPSTREAM_PROTOCOL_ERROR",
                                "Continuation request ID is missing.",
                            )
                        continuation_count = await self._repository.begin_continuation(
                            run.run_pk,
                            continue_request_id,
                            prepared.route.max_continue_count,
                            run.trace_id,
                        )
                        current = "CONTINUING"
                        normalized.data["continue_count"] = continuation_count
                    await self._emit(
                        prepared,
                        normalized.event_type,
                        normalized.data,
                        frame.event_id,
                    )
                    if normalized.terminal:
                        target = (
                            "COMPLETED"
                            if normalized.event_type == "run.completed"
                            else "FAILED"
                        )
                        await self._repository.transition_run(
                            run.run_pk, current, target, run.trace_id
                        )
                        if target == "FAILED":
                            logger.warning(
                                "Supervisor upstream returned a failed terminal event: "
                                "run_id=%s agent_id=%s upstream_event_id=%s error=%r",
                                run.run_uid,
                                prepared.route.agt_cd,
                                frame.event_id,
                                normalized.data.get("error"),
                            )
                        terminal_sent = True
                        return True
                    return False

                try:
                    while True:
                        remaining = (run.deadline_at - datetime.now(timezone.utc)).total_seconds()
                        if remaining <= 0:
                            raise asyncio.TimeoutError
                        timeout = (
                            prepared.route.first_event_timeout_seconds
                            if first
                            else prepared.route.idle_timeout_seconds
                        )
                        wait_deadline = asyncio.get_running_loop().time() + min(
                            timeout, remaining
                        )
                        next_chunk = asyncio.create_task(iterator.__anext__())
                        while not next_chunk.done():
                            poll_timeout = min(
                                1.0,
                                wait_deadline - asyncio.get_running_loop().time(),
                            )
                            if poll_timeout <= 0:
                                next_chunk.cancel()
                                await asyncio.gather(next_chunk, return_exceptions=True)
                                raise asyncio.TimeoutError
                            done, _ = await asyncio.wait(
                                {next_chunk}, timeout=poll_timeout
                            )
                            if done:
                                break
                            if asyncio.get_running_loop().time() >= next_lease_renewal:
                                if not await self._repository.renew_execution_lease(
                                    run.run_pk, self._owner_id, self._lease_seconds
                                ):
                                    raise RuntimeError("Run execution lease was lost")
                                next_lease_renewal = asyncio.get_running_loop().time() + 10.0
                            if await self._repository.is_cancel_requested(run.run_pk):
                                next_chunk.cancel()
                                await asyncio.gather(next_chunk, return_exceptions=True)
                                await self._cancel(prepared)
                                terminal_sent = True
                                return
                        chunk = next_chunk.result()
                        next_chunk = None
                        if asyncio.get_running_loop().time() >= next_lease_renewal:
                            if not await self._repository.renew_execution_lease(
                                run.run_pk, self._owner_id, self._lease_seconds
                            ):
                                raise RuntimeError("Run execution lease was lost")
                            next_lease_renewal = asyncio.get_running_loop().time() + 10.0
                        first = False
                        if await self._repository.is_cancel_requested(run.run_pk):
                            await self._cancel(prepared)
                            terminal_sent = True
                            return
                        for frame in parser.feed(chunk):
                            if await process_frame(frame):
                                return
                            if continuation is not None:
                                break
                        if continuation is not None:
                            break
                except StopAsyncIteration:
                    for frame in parser.finalize():
                        if await process_frame(frame):
                            return
                        if continuation is not None:
                            break
                finally:
                    if next_chunk is not None and not next_chunk.done():
                        next_chunk.cancel()
                        await asyncio.gather(next_chunk, return_exceptions=True)
                    await response.aclose()
                if continuation is None:
                    if completed_message_seen:
                        await self._emit(
                            prepared,
                            "run.completed",
                            {"status": "COMPLETED", "completion_source": "stream_eof"},
                        )
                        await self._repository.transition_run(
                            run.run_pk, current, "COMPLETED", run.trace_id
                        )
                        terminal_sent = True
                        return
                    raise RuntimeError("upstream stream ended without a terminal event")
                if await self._repository.is_cancel_requested(run.run_pk):
                    await self._cancel(prepared)
                    terminal_sent = True
                    return
                payload = build_continuation_request(messages, continuation)
                messages = [dict(item) for item in payload["messages"]]
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            if not terminal_sent:
                await self._repository.transition_run(run.run_pk, current, "TIMED_OUT", run.trace_id, error_code="UPSTREAM_TIMEOUT", error_message="Supervisor stream timed out.")
                await self._emit(prepared, "run.timed_out", {"status": "TIMED_OUT", "error": {"error_code": "UPSTREAM_TIMEOUT"}})
        except GatewayError as exc:
            if not terminal_sent:
                logger.warning(
                    "Supervisor run failed with a Gateway error: run_id=%s "
                    "agent_id=%s error_code=%s message=%s",
                    run.run_uid,
                    prepared.route.agt_cd,
                    exc.error_code,
                    exc.message,
                )
                target = "CANCELLED" if exc.error_code == "RUN_CANCEL_REQUESTED" else "FAILED"
                source = "CANCEL_REQUESTED" if target == "CANCELLED" else current
                await self._repository.transition_run(
                    run.run_pk,
                    source,
                    target,
                    run.trace_id,
                    error_code=None if target == "CANCELLED" else exc.error_code,
                    error_message=None if target == "CANCELLED" else exc.message,
                )
                event_type = "run.cancelled" if target == "CANCELLED" else "run.failed"
                await self._emit(
                    prepared,
                    event_type,
                    {"status": target, "error": None if target == "CANCELLED" else {"error_code": exc.error_code}},
                )
        except Exception:
            if not terminal_sent:
                logger.exception(
                    "Supervisor run failed unexpectedly: run_id=%s agent_id=%s",
                    run.run_uid,
                    prepared.route.agt_cd,
                )
                await self._repository.transition_run(run.run_pk, current, "FAILED", run.trace_id, error_code="UPSTREAM_ERROR", error_message="Supervisor execution failed.")
                await self._emit(prepared, "run.failed", {"status": "FAILED", "error": {"error_code": "UPSTREAM_ERROR"}})
        finally:
            await self._finish(run.run_uid)

    async def _cancel(self, prepared: PreparedRun) -> None:
        await self._repository.transition_run(
            prepared.run.run_pk,
            "CANCEL_REQUESTED",
            "CANCELLED",
            prepared.run.trace_id,
        )
        await self._emit(prepared, "run.cancelled", {"status": "CANCELLED"})

    async def _emit(self, prepared: PreparedRun, event_type: str, data: dict[str, Any], upstream_event_id: str | None = None) -> None:
        event_id, sequence = await self._repository.append_event(prepared.run.run_pk, event_type, data, prepared.run.trace_id, upstream_event_id)
        await self._broadcast(prepared.run.run_uid, self._envelope(prepared, event_type, sequence, event_id, data))

    @staticmethod
    def _envelope(prepared: PreparedRun, event_type: str, sequence: int, event_id: str, data: dict[str, Any]) -> dict[str, Any]:
        return {"event": event_type, "id": event_id, "data": {"schema_version": "1.0", "event_id": event_id, "sequence": sequence, "run_id": prepared.run.run_uid, "conversation_id": prepared.run.conversation_uid, "agent_id": prepared.route.agt_cd, "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "data": data}}

    @staticmethod
    def _stored_envelope(prepared: PreparedRun, row: dict[str, Any]) -> dict[str, Any]:
        timestamp = row["reg_dt"]
        if hasattr(timestamp, "isoformat"):
            timestamp = timestamp.isoformat().replace("+00:00", "Z")
        event_id = row["evt_uid"]
        return {
            "event": row["evt_typ_cd"],
            "id": event_id,
            "data": {
                "schema_version": "1.0",
                "event_id": event_id,
                "sequence": row["evt_seq_no"],
                "run_id": prepared.run.run_uid,
                "conversation_id": prepared.run.conversation_uid,
                "agent_id": prepared.route.agt_cd,
                "timestamp": timestamp,
                "data": row["evt_data"] or {},
            },
        }

    async def _broadcast(self, run_uid: str, event: dict[str, Any]) -> None:
        for queue in tuple(self._subscribers.get(run_uid, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def _finish(self, run_uid: str) -> None:
        for queue in tuple(self._subscribers.get(run_uid, ())):
            await queue.put(_END)
        async with self._lock:
            self._tasks.pop(run_uid, None)

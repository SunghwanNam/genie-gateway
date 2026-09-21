"""Supervisor Run 준비, 실행 정책과 상태 수명주기 조정."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.config import Settings
from app.core.errors import GatewayError
from app.domain.models import GatewayRequestContext, SupervisorRoute
from app.repositories.gateway_repository import GatewayRepository
from app.repositories.run_repository import RunRecord, RunRepository
from app.schemas.supervisor import UnifiedAgentResponseRequest
from app.services.supervisor_adapter import build_supervisor_request


@dataclass(frozen=True, slots=True)
class PreparedRun:
    route: SupervisorRoute
    run: RunRecord
    upstream_payload: dict[str, object]


class RunOrchestrator:
    """외부 요청을 DB Run과 최소 upstream 요청으로 준비한다."""

    def __init__(
        self,
        settings: Settings,
        gateway_repository: GatewayRepository,
        run_repository: RunRepository,
    ) -> None:
        self._settings = settings
        self._gateway_repository = gateway_repository
        self._run_repository = run_repository

    async def prepare(
        self,
        context: GatewayRequestContext,
        agent_id: str,
        request: UnifiedAgentResponseRequest,
        trace_id: str,
        idempotency_key: str | None = None,
    ) -> PreparedRun:
        route = await self._gateway_repository.resolve_supervisor_route(
            context.principal,
            agent_id,
        )
        if route is None:
            raise GatewayError(404, "AGENT_NOT_FOUND", "Supervisor Agent was not found.")
        if request.interaction_mode is not None:
            raise GatewayError(
                400,
                "INVALID_PARAMETER_VALUE",
                "interaction_mode is not supported for Supervisor Agent.",
            )
        if request.long_task and not route.long_task_enabled:
            raise GatewayError(
                400,
                "INVALID_PARAMETER_VALUE",
                "long_task is not supported by this Agent.",
            )
        request_hash = self._request_hash(
            context,
            route,
            request,
            idempotency_key,
        )
        deadline = datetime.now(timezone.utc) + timedelta(
            seconds=route.total_timeout_seconds
        )
        run = await self._run_repository.create_or_reuse_run(
            context.principal,
            route,
            context.user_id,
            request.conversation_id,
            request_hash,
            trace_id,
            deadline,
            self._settings.supervisor_gateway_max_active_runs,
            self._settings.supervisor_user_max_active_runs,
            self._settings.supervisor_user_agent_max_active_runs,
            bool(idempotency_key or request.client_message_id),
        )
        history = await self._run_repository.load_conversation_history(
            run.conversation_pk,
            run.run_pk,
            route.history_max_turn_count,
            route.history_max_char_count,
        )
        if not run.reused:
            await self._run_repository.save_item(
                run.run_pk,
                "user.message",
                {"role": "user", "content": request.input},
                trace_id,
            )
        payload = build_supervisor_request(
            request,
            history,
            allow_long_task=route.long_task_enabled,
        )
        return PreparedRun(route, run, payload)

    @staticmethod
    def _request_hash(
        context: GatewayRequestContext,
        route: SupervisorRoute,
        request: UnifiedAgentResponseRequest,
        idempotency_key: str | None,
    ) -> str:
        normalized_input = " ".join(request.input.split())
        dedupe_value = (
            f"idempotency:{idempotency_key.strip()}"
            if idempotency_key and idempotency_key.strip()
            else f"message:{request.client_message_id or normalized_input}"
        )
        material = "\x1f".join(
            (
                str(context.principal.api_clnt_id),
                context.user_id,
                request.conversation_id or "NEW",
                str(route.agt_id),
                dedupe_value,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

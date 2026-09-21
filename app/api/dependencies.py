"""FastAPI 공통 의존성."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query, Request

from app.core.errors import GatewayError
from app.core.security import get_current_principal
from app.domain.models import GatewayPrincipal, GatewayRequestContext
from app.services.genie_gateway import GenieGatewayService
from app.repositories.gateway_repository import GatewayRepository
from app.repositories.run_repository import RunRepository
from app.services.run_orchestrator import RunOrchestrator
from app.services.run_execution_manager import RunExecutionManager


def get_gateway_service(request: Request) -> GenieGatewayService:
    """애플리케이션 수명주기에서 생성한 Gateway Service를 반환한다."""
    return request.app.state.gateway_service


def get_gateway_repository(request: Request) -> GatewayRepository:
    """애플리케이션 수명주기의 Gateway Repository를 반환한다."""
    return request.app.state.gateway_repository


PrincipalDependency = Annotated[GatewayPrincipal, Depends(get_current_principal)]
GatewayServiceDependency = Annotated[GenieGatewayService, Depends(get_gateway_service)]
GatewayRepositoryDependency = Annotated[
    GatewayRepository,
    Depends(get_gateway_repository),
]


def get_run_orchestrator(request: Request) -> RunOrchestrator:
    return request.app.state.run_orchestrator


def get_run_execution_manager(request: Request) -> RunExecutionManager:
    return request.app.state.run_execution_manager


def get_run_repository(request: Request) -> RunRepository:
    return request.app.state.run_repository


RunOrchestratorDependency = Annotated[RunOrchestrator, Depends(get_run_orchestrator)]
RunExecutionManagerDependency = Annotated[RunExecutionManager, Depends(get_run_execution_manager)]
RunRepositoryDependency = Annotated[RunRepository, Depends(get_run_repository)]


async def get_gateway_request_context(
    principal: PrincipalDependency,
    source_system: Annotated[
        str,
        Query(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9_-]+$"),
    ],
    user_id: Annotated[
        str,
        Query(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._@-]+$"),
    ],
) -> GatewayRequestContext:
    """Gateway 전용 Query Parameter를 인증 Principal과 결합한다."""
    normalized_system = source_system.strip().upper()
    if normalized_system != principal.sys_cd.upper():
        raise GatewayError(
            403,
            "PERMISSION_DENIED",
            "source_system does not match the authenticated API client.",
        )
    return GatewayRequestContext(
        principal=principal,
        source_system=normalized_system,
        user_id=user_id.strip(),
    )


GatewayContextDependency = Annotated[
    GatewayRequestContext,
    Depends(get_gateway_request_context),
]

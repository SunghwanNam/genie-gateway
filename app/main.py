"""Genie API Gateway 애플리케이션 진입점."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError

from app.api.custom.chatbot import router as custom_chat_router
from app.api.custom.admin import router as custom_admin_router
from app.api.genie.agent import router as genie_agent_router
from app.api.genie.chat import router as genie_chat_router
from app.api.health import router as health_router
from app.api.v1.agents import router as unified_agents_router
from app.clients.databricks_auth import DatabricksTokenProvider
from app.clients.databricks_genie import DatabricksGenieClient
from app.clients.databricks_supervisor import DatabricksSupervisorClient
from app.core.config import get_settings
from app.core.database import Database
from app.core.credential_crypto import CredentialCipher
from app.core.errors import (
    GatewayError,
    gateway_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from app.core.request_context import resolve_trace_id
from app.repositories.gateway_repository import GatewayRepository
from app.repositories.run_repository import RunRepository
from app.repositories.admin_repository import AdminRepository
from app.services.genie_gateway import GenieGatewayService
from app.services.run_orchestrator import RunOrchestrator
from app.services.run_execution_manager import RunExecutionManager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """DB와 HTTP Client를 프로세스 수명주기에 맞춰 생성·종료한다."""
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    timeout = httpx.Timeout(
        connect=settings.upstream_connect_timeout_seconds,
        read=settings.upstream_read_timeout_seconds,
        write=60,
        pool=10,
    )
    http_client = httpx.AsyncClient(
        timeout=timeout,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        follow_redirects=False,
    )
    database = Database.from_settings(settings)
    repository = GatewayRepository(database, settings.database_schema)
    run_repository = RunRepository(database, settings.database_schema)
    admin_repository = AdminRepository(database, settings.database_schema)
    token_provider = DatabricksTokenProvider(settings, http_client)
    databricks_client = DatabricksGenieClient(settings, http_client, token_provider)
    supervisor_client = DatabricksSupervisorClient(settings, http_client, token_provider)
    gateway_service = GenieGatewayService(settings, repository, databricks_client)
    run_orchestrator = RunOrchestrator(settings, repository, run_repository)
    run_execution_manager = RunExecutionManager(run_repository, supervisor_client)

    app.state.settings = settings
    app.state.http_client = http_client
    app.state.database = database
    app.state.gateway_repository = repository
    app.state.run_repository = run_repository
    app.state.admin_repository = admin_repository
    app.state.credential_cipher = CredentialCipher(settings.api_key_encryption_key)
    app.state.gateway_service = gateway_service
    app.state.run_orchestrator = run_orchestrator
    app.state.run_execution_manager = run_execution_manager
    app.state.supervisor_client = supervisor_client

    if settings.gateway_dev_token:
        logger.warning("GATEWAY_DEV_TOKEN is enabled; do not use it in production.")
    if not await database.ping():
        logger.warning("Gateway database is not ready at startup.")
    else:
        reconciled = await run_repository.reconcile_interrupted_runs()
        if reconciled:
            logger.warning(
                "Reconciled %s expired Supervisor Run(s) at startup.", reconciled
            )

    try:
        yield
    finally:
        await run_execution_manager.close()
        await http_client.aclose()
        await database.dispose()


settings = get_settings()
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Databricks Genie Chat/Agent mode compatible routing gateway",
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
    lifespan=lifespan,
)

app.add_exception_handler(GatewayError, gateway_error_handler)  # type: ignore[arg-type]
app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
app.add_exception_handler(Exception, unhandled_error_handler)


@app.middleware("http")
async def add_request_context(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """요청 추적 ID를 수신·생성하고 모든 응답에 전달한다."""
    request.state.trace_id = resolve_trace_id(request)
    response = await call_next(request)
    response.headers["x-request-id"] = request.state.trace_id
    return response


@app.get("/", include_in_schema=False)
async def root(request: Request) -> dict[str, str]:
    """서비스 기본정보를 반환한다."""
    return {
        "service": request.app.state.settings.app_name,
        "version": request.app.state.settings.app_version,
        "docs": "/docs" if request.app.state.settings.docs_enabled else "disabled",
        "api_guide": "/guide",
    }


@app.get("/guide", response_class=Response, include_in_schema=False)
async def serve_api_guide() -> Response:
    """Databricks Genie Agent API 연동 가이드 HTML 문서를 반환한다."""
    from pathlib import Path
    from fastapi.responses import FileResponse

    guide_path = Path(__file__).resolve().parent / "static" / "genie_api_guide.html"
    if not guide_path.exists():
        return Response(content="API Guide HTML not found", status_code=404)
    return FileResponse(guide_path, media_type="text/html")



app.include_router(health_router)
app.include_router(genie_chat_router)
app.include_router(genie_agent_router)
app.include_router(unified_agents_router)
app.include_router(custom_chat_router)
app.include_router(custom_admin_router)

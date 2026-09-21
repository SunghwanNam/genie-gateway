"""Liveness와 Readiness API."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["Health"])


@router.get("/health/live")
async def liveness(request: Request) -> dict[str, str]:
    """프로세스 생존 상태를 반환한다."""
    return {
        "status": "ok",
        "service": request.app.state.settings.app_name,
        "version": request.app.state.settings.app_version,
    }


@router.get("/health/ready")
async def readiness(request: Request) -> JSONResponse:
    """필수 DB 연결 준비 상태를 반환한다."""
    database_ready = await request.app.state.database.ping()
    payload = {"status": "ready" if database_ready else "not_ready", "database": database_ready}
    return JSONResponse(status_code=200 if database_ready else 503, content=payload)

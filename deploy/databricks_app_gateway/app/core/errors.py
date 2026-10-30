"""Databricks 형식의 Gateway 오류 처리."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GatewayError(Exception):
    """외부 API에 노출 가능한 Gateway 오류."""

    status_code: int
    error_code: str
    message: str


async def gateway_error_handler(_: Request, exc: GatewayError) -> JSONResponse:
    """GatewayError를 Databricks 공통 오류 본문으로 변환한다."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error_code": exc.error_code, "message": exc.message},
    )


async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI 422 오류를 Databricks 호환 400 오류 형식으로 변환한다."""
    details = "; ".join(
        f"{'.'.join(str(item) for item in error['loc'])}: {error['msg']}"
        for error in exc.errors()
    )
    return JSONResponse(
        status_code=400,
        content={
            "error_code": "INVALID_PARAMETER_VALUE",
            "message": details or "Invalid request payload.",
        },
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """예상하지 못한 예외의 내부 상세를 숨긴다."""
    trace_id = getattr(request.state, "trace_id", "unknown")
    logger.exception("Unhandled gateway error trace_id=%s", trace_id, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "error_code": "INTERNAL_ERROR",
            "message": "An unexpected gateway error occurred.",
        },
        headers={"x-request-id": trace_id},
    )

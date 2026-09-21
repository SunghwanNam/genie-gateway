"""요청 추적 식별자 처리."""

from __future__ import annotations

import re
from uuid import uuid4

from fastapi import Request

_TRACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def resolve_trace_id(request: Request) -> str:
    """유효한 호출자 요청 ID를 유지하고 그렇지 않으면 새 ID를 생성한다."""
    supplied = request.headers.get("x-request-id", "")
    if _TRACE_ID_PATTERN.fullmatch(supplied):
        return supplied
    return uuid4().hex

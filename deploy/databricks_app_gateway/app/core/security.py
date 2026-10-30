"""배포 환경별 외부 시스템 인증."""

from __future__ import annotations

import hashlib
import secrets
from typing import Annotated

from fastapi import Request, Security
from fastapi.security import APIKeyHeader

from app.core.config import Settings
from app.core.errors import GatewayError
from app.domain.models import GatewayPrincipal
from app.repositories.gateway_repository import GatewayRepository

_gateway_api_key = APIKeyHeader(
    name="X-API-Key",
    scheme_name="GatewayApiKey",
    description="외부 시스템에 발급된 Gateway API Key",
    auto_error=False,
)


def hash_gateway_credential(credential: str) -> str:
    """DB 조회용 Credential SHA-256 해시를 생성한다."""
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


async def get_current_principal(
    request: Request,
    x_api_key: Annotated[str | None, Security(_gateway_api_key)] = None,
) -> GatewayPrincipal:
    """Credential을 검증하고 시스템·클라이언트 Principal을 반환한다."""
    settings: Settings = request.app.state.settings
    repository: GatewayRepository = request.app.state.gateway_repository

    credential = x_api_key.strip() if x_api_key else ""
    if settings.gateway_auth_mode == "DATABRICKS_APPS_OAUTH":
        # 이 헤더는 Databricks Apps의 인증 프록시를 통과한 요청에서만 신뢰한다.
        oauth_subject = request.headers.get("x-forwarded-user", "").strip()
        if not oauth_subject:
            raise GatewayError(401, "UNAUTHENTICATED", "Databricks Apps OAuth identity is required.")
        principal = await repository.authenticate_by_oauth_subject(oauth_subject)
    else:
        if not credential:
            raise GatewayError(401, "UNAUTHENTICATED", "X-API-Key header is required.")
        if settings.gateway_dev_token and secrets.compare_digest(credential, settings.gateway_dev_token):
            if not settings.gateway_dev_system_cd or not settings.gateway_dev_client_cd:
                raise GatewayError(
                    500,
                    "CONFIGURATION_ERROR",
                    "GATEWAY_DEV_SYSTEM_CD and GATEWAY_DEV_CLIENT_CD must be configured when GATEWAY_DEV_TOKEN is set.",
                )
            principal = await repository.get_principal_by_codes(
                settings.gateway_dev_system_cd,
                settings.gateway_dev_client_cd,
            )
        else:
            principal = await repository.authenticate_by_key_hash(hash_gateway_credential(credential))

        if settings.gateway_auth_mode == "OAUTH_AND_API_KEY":
            oauth_subject = request.headers.get("x-forwarded-user", "").strip()
            if not oauth_subject:
                raise GatewayError(401, "UNAUTHENTICATED", "Databricks Apps OAuth identity is required.")
            oauth_principal = await repository.authenticate_by_oauth_subject(oauth_subject)
            if (
                principal is None
                or oauth_principal is None
                or oauth_principal.api_clnt_id != principal.api_clnt_id
            ):
                raise GatewayError(403, "PERMISSION_DENIED", "OAuth identity and X-API-Key client do not match.")

    if principal is None:
        raise GatewayError(401, "UNAUTHENTICATED", "The supplied credential is invalid or expired.")
    return principal

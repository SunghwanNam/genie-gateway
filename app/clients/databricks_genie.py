"""Databricks Genie REST API 비동기 Client."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import httpx

from app.clients.databricks_auth import DatabricksTokenProvider, normalize_databricks_host
from app.core.config import Settings
from app.core.errors import GatewayError
from app.domain.models import GenieRoute

_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}


class DatabricksGenieClient:
    """인증, 제한적 재시도와 스트리밍을 포함한 Genie HTTP Client."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        token_provider: DatabricksTokenProvider,
    ) -> None:
        self._settings = settings
        self._client = client
        self._token_provider = token_provider

    async def send(
        self,
        route: GenieRoute,
        method: str,
        path: str,
        query_params: Sequence[tuple[str, str]],
        json_body: dict[str, object] | None,
        accept: str | None,
    ) -> httpx.Response:
        """Genie 요청을 전송하고 아직 읽지 않은 응답을 반환한다."""
        configured_host = route.databricks_host or self._settings.databricks_host
        if not configured_host:
            raise GatewayError(503, "TEMPORARILY_UNAVAILABLE", "Databricks host is not configured.")
        host = normalize_databricks_host(configured_host, self._settings.allow_insecure_upstream)
        credential_ref = (
            None
            if self._settings.gateway_auth_mode == "DATABRICKS_APPS_OAUTH"
            else route.credential_ref
        )

        safe_retry_count = self._settings.upstream_retry_attempts if method.upper() == "GET" else 0
        oauth_retry_available = True
        attempt = 0
        while True:
            token = await self._token_provider.get_token(host, credential_ref)
            headers = {
                "authorization": f"Bearer {token}",
                "accept": accept or "application/json",
                "user-agent": f"genie-api-gateway/{self._settings.app_version}",
            }
            request = self._client.build_request(
                method=method,
                url=f"{host}{path}",
                params=list(query_params),
                json=json_body,
                headers=headers,
            )
            try:
                response = await self._client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                raise GatewayError(504, "DEADLINE_EXCEEDED", "Databricks request timed out.") from exc
            except httpx.RequestError as exc:
                raise GatewayError(502, "TEMPORARILY_UNAVAILABLE", "Databricks workspace is unavailable.") from exc

            if response.status_code == 401 and oauth_retry_available and not self._settings.databricks_token:
                oauth_retry_available = False
                self._token_provider.invalidate(host, credential_ref)
                await response.aclose()
                continue

            if response.status_code in _RETRYABLE_STATUS_CODES and attempt < safe_retry_count:
                retry_after = response.headers.get("retry-after")
                await response.aclose()
                delay = self._retry_delay(attempt, retry_after)
                attempt += 1
                await asyncio.sleep(delay)
                continue
            return response

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), 10.0)
            except ValueError:
                pass
        return min(0.5 * (2**attempt), 5.0)

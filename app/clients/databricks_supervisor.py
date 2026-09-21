"""Databricks Supervisor Serving Endpoint streaming client."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from app.clients.databricks_auth import DatabricksTokenProvider, normalize_databricks_host
from app.core.config import Settings
from app.core.errors import GatewayError
from app.domain.models import SupervisorRoute


class DatabricksSupervisorClient:
    """Supervisor endpoint 인증과 아직 읽지 않은 SSE 응답 생성을 담당한다."""

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
        route: SupervisorRoute,
        json_body: dict[str, object],
    ) -> httpx.Response:
        configured_host = route.databricks_host or self._settings.databricks_host
        if not configured_host:
            raise GatewayError(503, "UPSTREAM_UNAVAILABLE", "Databricks host is not configured.")
        host = normalize_databricks_host(
            configured_host,
            self._settings.allow_insecure_upstream,
        )
        credential_ref = (
            None
            if self._settings.gateway_auth_mode == "DATABRICKS_APPS_OAUTH"
            else route.credential_ref
        )
        endpoint = quote(route.serving_endpoint_name, safe="")
        oauth_retry_available = True
        while True:
            token = await self._token_provider.get_token(host, credential_ref)
            request = self._client.build_request(
                method="POST",
                url=f"{host}/serving-endpoints/{endpoint}/invocations",
                json=json_body,
                headers={
                    "authorization": f"Bearer {token}",
                    "accept": "text/event-stream",
                    "content-type": "application/json",
                    "user-agent": f"genie-api-gateway/{self._settings.app_version}",
                },
            )
            try:
                response = await self._client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                raise GatewayError(504, "UPSTREAM_TIMEOUT", "Supervisor request timed out.") from exc
            except httpx.RequestError as exc:
                raise GatewayError(503, "UPSTREAM_UNAVAILABLE", "Supervisor endpoint is unavailable.") from exc

            if response.status_code == 401 and oauth_retry_available and not self._settings.databricks_token:
                oauth_retry_available = False
                self._token_provider.invalidate(host, credential_ref)
                await response.aclose()
                continue
            return response

"""Databricks PAT 또는 OAuth M2M 인증 토큰 제공자."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings
from app.core.errors import GatewayError


def normalize_databricks_host(host: str, allow_insecure: bool = False) -> str:
    """DB 또는 환경 변수의 Databricks Host를 안전한 Origin으로 정규화한다."""
    raw_host = host.strip()
    if "://" not in raw_host:
        raw_host = f"https://{raw_host}"
    parsed = urlsplit(raw_host)
    if not parsed.hostname or parsed.username or parsed.password:
        raise GatewayError(500, "INVALID_STATE", "Configured Databricks host is invalid.")
    if parsed.scheme != "https" and not (allow_insecure and parsed.scheme == "http"):
        raise GatewayError(500, "INVALID_STATE", "Databricks host must use HTTPS.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise GatewayError(500, "INVALID_STATE", "Databricks host must not contain a path or query.")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


@dataclass(slots=True)
class _CachedToken:
    access_token: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class _OAuthCredential:
    client_id: str
    client_secret: str
    cache_key: str


class DatabricksTokenProvider:
    """Workspace별 OAuth 토큰을 만료 전까지 캐시한다."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._cache: dict[tuple[str, str], _CachedToken] = {}
        self._credential_cache: dict[str, _OAuthCredential] = {}
        self._lock = asyncio.Lock()

    async def get_token(self, host: str, credential_ref: str | None = None) -> str:
        """정적 Token 또는 OAuth M2M Access Token을 반환한다."""
        if self._settings.databricks_token and credential_ref is None:
            return self._settings.databricks_token

        credential = await self._resolve_credential(credential_ref)
        cache_key = (host, credential.cache_key)

        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached and cached.expires_at > now + 60:
            return cached.access_token

        async with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached.expires_at > time.monotonic() + 60:
                return cached.access_token
            return await self._request_oauth_token(host, credential)

    def invalidate(self, host: str, credential_ref: str | None = None) -> None:
        """401 응답 시 Workspace Token 캐시를 제거한다."""
        if credential_ref is None:
            client_id = self._settings.databricks_client_id or "default"
            self._cache.pop((host, client_id), None)
            return
        credential = self._credential_cache.get(credential_ref)
        if credential:
            self._cache.pop((host, credential.cache_key), None)

    async def _resolve_credential(self, credential_ref: str | None) -> _OAuthCredential:
        if credential_ref is None:
            client_id = self._settings.databricks_client_id
            client_secret = self._settings.databricks_client_secret
            if not client_id or not client_secret:
                raise GatewayError(503, "TEMPORARILY_UNAVAILABLE", "Databricks authentication is not configured.")
            return _OAuthCredential(client_id, client_secret, client_id)

        cached = self._credential_cache.get(credential_ref)
        if cached:
            return cached

        if credential_ref.startswith("env:"):
            prefix = credential_ref.removeprefix("env:").strip()
            client_id = os.getenv(f"{prefix}_CLIENT_ID")
            client_secret = os.getenv(f"{prefix}_CLIENT_SECRET")
            if not client_id or not client_secret:
                raise GatewayError(503, "TEMPORARILY_UNAVAILABLE", "Referenced Databricks SP credential is not configured.")
        elif credential_ref.startswith("secretsmanager:"):
            secret_id = credential_ref.removeprefix("secretsmanager:").strip()
            client_id, client_secret = await asyncio.to_thread(self._read_aws_secret, secret_id)
        else:
            raise GatewayError(500, "INVALID_STATE", "Databricks credential reference type is unsupported.")

        credential = _OAuthCredential(client_id.strip(), client_secret.strip(), client_id.strip())
        self._credential_cache[credential_ref] = credential
        return credential

    @staticmethod
    def _read_aws_secret(secret_id: str) -> tuple[str, str]:
        try:
            import boto3

            value = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"]
            payload = json.loads(value)
            return str(payload["client_id"]), str(payload["client_secret"])
        except GatewayError:
            raise
        except Exception as exc:
            raise GatewayError(503, "TEMPORARILY_UNAVAILABLE", "Databricks SP secret could not be loaded.") from exc

    async def _request_oauth_token(self, host: str, credential: _OAuthCredential) -> str:
        try:
            response = await self._client.post(
                f"{host}/oidc/v1/token",
                auth=httpx.BasicAuth(credential.client_id, credential.client_secret),
                data={"grant_type": "client_credentials", "scope": "all-apis"},
                headers={"accept": "application/json"},
            )
        except httpx.RequestError as exc:
            raise GatewayError(502, "TEMPORARILY_UNAVAILABLE", "Databricks OAuth endpoint is unavailable.") from exc

        if response.status_code >= 400:
            raise GatewayError(502, "UNAUTHENTICATED", "Databricks OAuth authentication failed.")

        try:
            payload = response.json()
            access_token = payload["access_token"]
            expires_in = int(payload.get("expires_in", 3_600))
        except (KeyError, TypeError, ValueError) as exc:
            raise GatewayError(502, "INTERNAL_ERROR", "Databricks OAuth response is invalid.") from exc

        self._cache[(host, credential.cache_key)] = _CachedToken(
            access_token=access_token,
            expires_at=time.monotonic() + max(expires_in, 60),
        )
        return access_token

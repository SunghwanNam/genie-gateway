"""배포 환경별 PostgreSQL 연결 Credential Provider."""

from __future__ import annotations

import asyncio
import json
import ssl
from dataclasses import dataclass
from typing import Any, Protocol

import asyncpg

from app.core.config import Settings
from app.core.errors import GatewayError


class AsyncConnectionProvider(Protocol):
    """SQLAlchemy async_creator가 사용할 물리 연결 생성 계약."""

    async def connect(self) -> asyncpg.Connection[Any]: ...


@dataclass(frozen=True, slots=True)
class PostgresConnectionParameters:
    host: str
    port: int
    database: str
    user: str
    ssl_value: ssl.SSLContext | bool


def _connection_parameters(settings: Settings) -> PostgresConnectionParameters:
    missing = [
        name
        for name, value in {
            "PGHOST": settings.pg_host,
            "PGDATABASE": settings.pg_database,
            "PGUSER": settings.pg_user,
        }.items()
        if not value
    ]
    if missing:
        raise GatewayError(
            500,
            "CONFIGURATION_ERROR",
            f"Database connection settings are missing: {', '.join(missing)}.",
        )
    return PostgresConnectionParameters(
        host=settings.pg_host or "",
        port=settings.pg_port,
        database=settings.pg_database or "",
        user=settings.pg_user or "",
        ssl_value=_ssl_value(settings.pg_sslmode, settings.pg_sslrootcert),
    )


def _ssl_value(mode: str, root_certificate: str | None) -> ssl.SSLContext | bool:
    normalized = mode.strip().lower()
    if normalized in {"disable", "allow", "prefer"}:
        return False
    if normalized == "require" and root_certificate is None:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    if normalized not in {"require", "verify-ca", "verify-full"}:
        raise GatewayError(500, "CONFIGURATION_ERROR", f"Unsupported PGSSLMODE: {mode}.")
    context = ssl.create_default_context(cafile=root_certificate)
    context.check_hostname = normalized == "verify-full"
    return context


class RdsPasswordConnectionProvider:
    """환경변수 또는 AWS Secrets Manager 비밀번호로 RDS 연결을 생성한다."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._params = _connection_parameters(settings)

    async def connect(self) -> asyncpg.Connection[Any]:
        username = self._params.user
        password = self._settings.pg_password
        if self._settings.rds_secret_ref:
            username, password = await asyncio.to_thread(self._read_secret)
        if not password:
            raise GatewayError(500, "CONFIGURATION_ERROR", "RDS database password is not configured.")
        return await asyncpg.connect(
            host=self._params.host,
            port=self._params.port,
            database=self._params.database,
            user=username,
            password=password,
            ssl=self._params.ssl_value,
        )

    def _read_secret(self) -> tuple[str, str]:
        secret_id = (self._settings.rds_secret_ref or "").removeprefix("secretsmanager:").strip()
        if not secret_id:
            raise GatewayError(500, "CONFIGURATION_ERROR", "RDS_SECRET_REF is invalid.")
        try:
            import boto3

            client = boto3.client("secretsmanager", region_name=self._settings.aws_region)
            payload = json.loads(client.get_secret_value(SecretId=secret_id)["SecretString"])
            username = str(payload.get("username") or self._params.user).strip()
            password = str(payload["password"])
            return username, password
        except GatewayError:
            raise
        except Exception as exc:
            raise GatewayError(503, "TEMPORARILY_UNAVAILABLE", "RDS database secret could not be loaded.") from exc


class RdsIamConnectionProvider:
    """AWS IAM DB Auth Token을 새 물리 연결마다 발급해 RDS에 연결한다."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._params = _connection_parameters(settings)
        if settings.pg_sslmode in {"disable", "allow", "prefer"}:
            raise GatewayError(500, "CONFIGURATION_ERROR", "RDS_IAM requires PGSSLMODE=require or verify-full.")

    async def connect(self) -> asyncpg.Connection[Any]:
        token = await asyncio.to_thread(self._generate_token)
        return await asyncpg.connect(
            host=self._params.host,
            port=self._params.port,
            database=self._params.database,
            user=self._params.user,
            password=token,
            ssl=self._params.ssl_value,
        )

    def _generate_token(self) -> str:
        try:
            import boto3

            client = boto3.client("rds", region_name=self._settings.aws_region)
            return str(
                client.generate_db_auth_token(
                    DBHostname=self._params.host,
                    Port=self._params.port,
                    DBUsername=self._params.user,
                    Region=self._settings.aws_region,
                )
            )
        except Exception as exc:
            raise GatewayError(503, "TEMPORARILY_UNAVAILABLE", "RDS IAM database token could not be generated.") from exc


class LakebaseOAuthConnectionProvider:
    """Databricks SDK로 Lakebase OAuth DB Credential을 새 연결마다 발급한다."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._params = _connection_parameters(settings)
        if not settings.lakebase_endpoint:
            raise GatewayError(500, "CONFIGURATION_ERROR", "LAKEBASE_ENDPOINT is required.")
        if settings.pg_sslmode in {"disable", "allow", "prefer"}:
            raise GatewayError(500, "CONFIGURATION_ERROR", "LAKEBASE requires PGSSLMODE=require or verify-full.")
        self._workspace_client: Any = None

    async def connect(self) -> asyncpg.Connection[Any]:
        token = await asyncio.to_thread(self._generate_token)
        return await asyncpg.connect(
            host=self._params.host,
            port=self._params.port,
            database=self._params.database,
            user=self._params.user,
            password=token,
            ssl=self._params.ssl_value,
        )

    def _generate_token(self) -> str:
        try:
            if self._workspace_client is None:
                from databricks.sdk import WorkspaceClient

                self._workspace_client = WorkspaceClient()
            credential = self._workspace_client.postgres.generate_database_credential(
                endpoint=self._settings.lakebase_endpoint
            )
            if not credential.token:
                raise ValueError("Lakebase credential response has no token")
            return str(credential.token)
        except Exception as exc:
            raise GatewayError(503, "TEMPORARILY_UNAVAILABLE", "Lakebase database token could not be generated.") from exc

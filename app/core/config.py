"""환경 변수 기반 애플리케이션 설정."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from dotenv import load_dotenv
from sqlalchemy.engine import URL

load_dotenv()


def _as_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _as_positive_int(name: str, default: int) -> int:
    value = _as_int(name, default)
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _optional_env(name: str) -> str | None:
    """빈 값과 배포 예제 placeholder를 실제 설정으로 취급하지 않는다."""
    value = os.getenv(name)
    if value is None or not value.strip() or value.strip().upper() == "CHANGE_ME":
        return None
    return value.strip()


def _database_url() -> str | None:
    """DATABASE_URL 또는 표준 PostgreSQL 환경 변수로 접속 URL을 구성한다."""
    explicit_url = os.getenv("DATABASE_URL")
    if explicit_url:
        return explicit_url

    required = ("PGHOST", "PGDATABASE", "PGUSER")
    if not all(os.getenv(name) for name in required):
        return None

    url = URL.create(
        drivername="postgresql+asyncpg",
        username=os.environ["PGUSER"],
        password=os.getenv("PGPASSWORD"),
        host=os.environ["PGHOST"],
        port=_as_int("PGPORT", 5432),
        database=os.environ["PGDATABASE"],
    )
    return url.render_as_string(hide_password=False)


def _gateway_auth_mode() -> str:
    """명시 설정이 없으면 Databricks Apps 런타임 여부로 인증 모드를 결정한다."""
    configured = os.getenv("GATEWAY_AUTH_MODE")
    if configured:
        mode = configured.strip().upper()
    else:
        mode = "DATABRICKS_APPS_OAUTH" if os.getenv("DATABRICKS_APP_NAME") else "API_KEY"
    if mode not in {"API_KEY", "DATABRICKS_APPS_OAUTH", "OAUTH_AND_API_KEY"}:
        raise ValueError("GATEWAY_AUTH_MODE must be API_KEY, DATABRICKS_APPS_OAUTH, or OAUTH_AND_API_KEY")
    return mode


def _database_mode() -> str:
    """DB 인증 방식을 명시하며 기존 로컬 환경은 POSTGRES를 기본값으로 유지한다."""
    mode = os.getenv("DATABASE_MODE", "POSTGRES").strip().upper()
    if mode not in {"POSTGRES", "RDS", "RDS_IAM", "LAKEBASE"}:
        raise ValueError("DATABASE_MODE must be POSTGRES, RDS, RDS_IAM, or LAKEBASE")
    return mode


@dataclass(frozen=True, slots=True)
class Settings:
    """Gateway 실행 설정."""

    app_name: str = "Genie API Gateway"
    app_version: str = "0.1.0"
    environment: str = "local"
    log_level: str = "INFO"
    docs_enabled: bool = True
    custom_chat_ui_enabled: bool = False
    custom_chat_local_only: bool = True
    admin_ui_enabled: bool = True
    admin_session_ttl_seconds: int = 28_800
    admin_bootstrap_email: str | None = None
    api_key_encryption_key: str | None = field(default=None, repr=False)
    database_mode: str = "POSTGRES"
    database_url: str | None = field(default=None, repr=False)
    database_schema: str = "genie_gw"
    pg_host: str | None = None
    pg_port: int = 5432
    pg_database: str | None = None
    pg_user: str | None = None
    pg_password: str | None = field(default=None, repr=False)
    pg_sslmode: str = "prefer"
    pg_sslrootcert: str | None = None
    rds_secret_ref: str | None = None
    aws_region: str | None = None
    lakebase_endpoint: str | None = None
    gateway_auth_mode: str = "API_KEY"
    databricks_host: str | None = None
    databricks_token: str | None = field(default=None, repr=False)
    databricks_client_id: str | None = None
    databricks_client_secret: str | None = field(default=None, repr=False)
    gateway_dev_token: str | None = field(default=None, repr=False)
    gateway_dev_system_cd: str | None = None
    gateway_dev_client_cd: str | None = None
    audit_payloads: bool = False
    allow_insecure_upstream: bool = False
    upstream_connect_timeout_seconds: int = 10
    upstream_read_timeout_seconds: int = 1_860
    upstream_retry_attempts: int = 2
    supervisor_gateway_max_active_runs: int = 40
    supervisor_user_max_active_runs: int = 10
    supervisor_user_agent_max_active_runs: int = 5

    @classmethod
    def from_env(cls) -> "Settings":
        """환경 변수에서 설정을 생성한다."""
        dev_sys_cd = _optional_env("GATEWAY_DEV_SYSTEM_CD")
        return cls(
            app_name=os.getenv("APP_NAME", "Genie API Gateway"),
            app_version=os.getenv("APP_VERSION", "0.1.0"),
            environment=os.getenv("APP_ENV", "local"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            docs_enabled=_as_bool("DOCS_ENABLED", True),
            custom_chat_ui_enabled=_as_bool("CUSTOM_CHAT_UI_ENABLED", False),
            custom_chat_local_only=_as_bool("CUSTOM_CHAT_LOCAL_ONLY", True),
            admin_ui_enabled=_as_bool("ADMIN_UI_ENABLED", True),
            admin_session_ttl_seconds=_as_int("ADMIN_SESSION_TTL_SECONDS", 28_800),
            admin_bootstrap_email=_optional_env("ADMIN_BOOTSTRAP_EMAIL"),
            api_key_encryption_key=_optional_env("API_KEY_ENCRYPTION_KEY"),
            database_mode=_database_mode(),
            database_url=_database_url(),
            database_schema=os.getenv("DATABASE_SCHEMA", "genie_gw"),
            pg_host=_optional_env("PGHOST"),
            pg_port=_as_int("PGPORT", 5432),
            pg_database=_optional_env("PGDATABASE"),
            pg_user=_optional_env("PGUSER"),
            pg_password=_optional_env("PGPASSWORD"),
            pg_sslmode=os.getenv("PGSSLMODE", "prefer").strip().lower(),
            pg_sslrootcert=_optional_env("PGSSLROOTCERT"),
            rds_secret_ref=_optional_env("RDS_SECRET_REF"),
            aws_region=_optional_env("AWS_REGION") or _optional_env("AWS_DEFAULT_REGION"),
            lakebase_endpoint=_optional_env("LAKEBASE_ENDPOINT"),
            gateway_auth_mode=_gateway_auth_mode(),
            databricks_host=os.getenv("DATABRICKS_HOST"),
            databricks_token=_optional_env("DATABRICKS_TOKEN"),
            databricks_client_id=_optional_env("DATABRICKS_CLIENT_ID"),
            databricks_client_secret=_optional_env("DATABRICKS_CLIENT_SECRET"),
            gateway_dev_token=_optional_env("GATEWAY_DEV_TOKEN"),
            gateway_dev_system_cd=dev_sys_cd.upper() if dev_sys_cd else None,
            gateway_dev_client_cd=_optional_env("GATEWAY_DEV_CLIENT_CD"),
            audit_payloads=_as_bool("AUDIT_PAYLOADS", False),
            allow_insecure_upstream=_as_bool("ALLOW_INSECURE_UPSTREAM", False),
            upstream_connect_timeout_seconds=_as_int("UPSTREAM_CONNECT_TIMEOUT_SECONDS", 10),
            upstream_read_timeout_seconds=_as_int("UPSTREAM_READ_TIMEOUT_SECONDS", 1_860),
            upstream_retry_attempts=_as_int("UPSTREAM_RETRY_ATTEMPTS", 2),
            supervisor_gateway_max_active_runs=_as_positive_int(
                "SUPERVISOR_GATEWAY_MAX_ACTIVE_RUNS", 40
            ),
            supervisor_user_max_active_runs=_as_positive_int(
                "SUPERVISOR_USER_MAX_ACTIVE_RUNS", 10
            ),
            supervisor_user_agent_max_active_runs=_as_positive_int(
                "SUPERVISOR_USER_AGENT_MAX_ACTIVE_RUNS", 5
            ),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """프로세스 단위 설정 객체를 반환한다."""
    return Settings.from_env()

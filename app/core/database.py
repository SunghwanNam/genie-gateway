"""비동기 PostgreSQL 연결과 트랜잭션 경계."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.database_providers import (
    LakebaseOAuthConnectionProvider,
    RdsIamConnectionProvider,
    RdsPasswordConnectionProvider,
)
from app.core.errors import GatewayError


class Database:
    """SQLAlchemy 비동기 엔진을 애플리케이션 수명주기에 맞춰 관리한다."""

    def __init__(self, database_url: str | None) -> None:
        self._engine = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        if database_url:
            self._engine = create_async_engine(
                database_url,
                pool_pre_ping=True,
                pool_recycle=1_800,
            )
            self._session_factory = async_sessionmaker(
                bind=self._engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )

    @classmethod
    def from_settings(cls, settings: Settings) -> "Database":
        """설정된 DB 인증 모드에 맞는 SQLAlchemy 엔진을 구성한다."""
        if settings.database_mode == "POSTGRES":
            return cls(settings.database_url)

        database = cls(None)
        if settings.database_mode == "RDS":
            provider = RdsPasswordConnectionProvider(settings)
            pool_recycle = 1_800
        elif settings.database_mode == "RDS_IAM":
            provider = RdsIamConnectionProvider(settings)
            pool_recycle = 840
        elif settings.database_mode == "LAKEBASE":
            provider = LakebaseOAuthConnectionProvider(settings)
            pool_recycle = 2_700
        else:  # Settings 검증을 우회한 직접 생성도 안전하게 거부한다.
            raise GatewayError(500, "CONFIGURATION_ERROR", "Unsupported database mode.")

        database._engine = create_async_engine(
            "postgresql+asyncpg://",
            async_creator=provider.connect,
            pool_pre_ping=True,
            pool_recycle=pool_recycle,
        )
        database._session_factory = async_sessionmaker(
            bind=database._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        return database

    @property
    def configured(self) -> bool:
        """DB 연결 설정 존재 여부를 반환한다."""
        return self._session_factory is not None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """성공 시 commit, 실패 시 rollback하는 트랜잭션 세션을 제공한다."""
        if self._session_factory is None:
            raise GatewayError(
                status_code=503,
                error_code="TEMPORARILY_UNAVAILABLE",
                message="Gateway database is not configured.",
            )

        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def ping(self) -> bool:
        """DB 연결 상태를 확인한다."""
        if self._session_factory is None:
            return False
        try:
            async with self._session_factory() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def dispose(self) -> None:
        """연결 풀을 종료한다."""
        if self._engine is not None:
            await self._engine.dispose()

"""PostgreSQL/RDS/Lakebase 연결 모드 계약 테스트."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.core.config import Settings
from app.core.database import Database
from app.core.database_providers import (
    LakebaseOAuthConnectionProvider,
    RdsIamConnectionProvider,
    RdsPasswordConnectionProvider,
)
from app.core.errors import GatewayError


def _remote_settings(mode: str) -> Settings:
    return Settings(
        database_mode=mode,
        pg_host="database.example.com",
        pg_port=5432,
        pg_database="genie_gateway",
        pg_user="gateway_user",
        pg_password="database-password",
        pg_sslmode="require",
        aws_region="ap-northeast-2",
        lakebase_endpoint="projects/project/branches/production/endpoints/endpoint",
    )


class DatabaseModeTests(unittest.IsolatedAsyncioTestCase):
    def test_postgres_mode_preserves_static_database_url(self) -> None:
        database = Database.from_settings(
            Settings(
                database_mode="POSTGRES",
                database_url="postgresql+asyncpg://postgres:password@localhost:15432/postgres",
            )
        )
        self.assertTrue(database.configured)

    def test_rds_iam_rejects_non_tls_configuration(self) -> None:
        with self.assertRaises(GatewayError):
            RdsIamConnectionProvider(
                Settings(
                    database_mode="RDS_IAM",
                    pg_host="database.example.com",
                    pg_database="genie_gateway",
                    pg_user="gateway_user",
                    pg_sslmode="disable",
                )
            )

    async def test_rds_password_passes_configured_password(self) -> None:
        provider = RdsPasswordConnectionProvider(_remote_settings("RDS"))
        connection = object()
        with patch(
            "app.core.database_providers.asyncpg.connect",
            new=AsyncMock(return_value=connection),
        ) as connect:
            self.assertIs(await provider.connect(), connection)
        self.assertEqual(connect.await_args.kwargs["user"], "gateway_user")
        self.assertEqual(connect.await_args.kwargs["password"], "database-password")

    async def test_rds_iam_generates_token_for_each_connection(self) -> None:
        provider = RdsIamConnectionProvider(_remote_settings("RDS_IAM"))
        connection = object()
        with (
            patch.object(provider, "_generate_token", return_value="iam-token") as token,
            patch(
                "app.core.database_providers.asyncpg.connect",
                new=AsyncMock(return_value=connection),
            ) as connect,
        ):
            self.assertIs(await provider.connect(), connection)
            self.assertIs(await provider.connect(), connection)
        self.assertEqual(token.call_count, 2)
        self.assertEqual(connect.await_args.kwargs["password"], "iam-token")

    async def test_lakebase_generates_token_for_each_connection(self) -> None:
        provider = LakebaseOAuthConnectionProvider(_remote_settings("LAKEBASE"))
        connection = object()
        with (
            patch.object(provider, "_generate_token", return_value="lakebase-token") as token,
            patch(
                "app.core.database_providers.asyncpg.connect",
                new=AsyncMock(return_value=connection),
            ) as connect,
        ):
            self.assertIs(await provider.connect(), connection)
            self.assertIs(await provider.connect(), connection)
        self.assertEqual(token.call_count, 2)
        self.assertEqual(connect.await_args.kwargs["password"], "lakebase-token")

    async def test_dynamic_modes_build_configured_database(self) -> None:
        for mode in ("RDS", "RDS_IAM", "LAKEBASE"):
            with self.subTest(mode=mode):
                database = Database.from_settings(_remote_settings(mode))
                self.assertTrue(database.configured)
                await database.dispose()


if __name__ == "__main__":
    unittest.main()

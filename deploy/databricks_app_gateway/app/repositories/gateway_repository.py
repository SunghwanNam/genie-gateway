"""Gateway 인증, Agent 라우팅 및 감사 이력 Repository."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import Database
from app.core.errors import GatewayError
from app.domain.models import (
    ApiAuditRecord,
    ConversationOwner,
    GatewayPrincipal,
    GenieAttachmentSnapshot,
    GenieMessageSnapshot,
    GenieRoute,
    SupervisorRoute,
)


class GatewayRepository:
    """genie_gw 스키마에 대한 모든 DB 접근을 담당한다."""

    def __init__(self, database: Database, schema: str = "genie_gw") -> None:
        if re.fullmatch(r"[a-z_][a-z0-9_]*", schema) is None:
            raise ValueError("database schema must be a lowercase SQL identifier")
        self._database = database
        self._schema = schema

    async def authenticate_by_key_hash(self, key_hash: str) -> GatewayPrincipal | None:
        """유효한 Credential 해시로 API 클라이언트를 조회한다."""
        sql = text(
            f"""
            WITH active_key AS (
                SELECT k.api_clnt_id
                  FROM {self._schema}.api_clnt_key_hst k
                 WHERE k.key_hsh_val = :key_hash
                   AND k.revk_yn = 'N'
                   AND k.eff_strt_dt <= CURRENT_TIMESTAMP
                   AND (k.eff_end_dt IS NULL OR k.eff_end_dt > CURRENT_TIMESTAMP)
                 ORDER BY k.eff_strt_dt DESC
                 LIMIT 1
            )
            SELECT c.api_clnt_id,
                   c.api_clnt_cd,
                   s.sys_id,
                   s.sys_cd
              FROM active_key k
              JOIN {self._schema}.api_clnt_mst c
                ON c.api_clnt_id = k.api_clnt_id
               AND c.use_yn = 'Y'
              JOIN {self._schema}.ext_sys_mst s
                ON s.sys_id = c.sys_id
               AND s.use_yn = 'Y'
            """
        )
        async with self._database.session() as session:
            row = (await session.execute(sql, {"key_hash": key_hash})).mappings().one_or_none()
        return self._principal(row) if row else None

    async def get_principal_by_codes(
        self,
        system_code: str,
        client_code: str,
    ) -> GatewayPrincipal | None:
        """명시적으로 설정된 로컬 개발 Credential의 Principal을 조회한다."""
        sql = text(
            f"""
            SELECT c.api_clnt_id,
                   c.api_clnt_cd,
                   s.sys_id,
                   s.sys_cd
              FROM {self._schema}.api_clnt_mst c
              JOIN {self._schema}.ext_sys_mst s
                ON s.sys_id = c.sys_id
             WHERE s.sys_cd = :system_code
               AND c.api_clnt_cd = :client_code
               AND s.use_yn = 'Y'
               AND c.use_yn = 'Y'
            """
        )
        async with self._database.session() as session:
            row = (
                await session.execute(
                    sql,
                    {"system_code": system_code, "client_code": client_code},
                )
            ).mappings().one_or_none()
        return self._principal(row) if row else None

    async def get_custom_chat_principal(self) -> GatewayPrincipal | None:
        """Admin에서 구성한 활성 기본 라우트를 가진 첫 클라이언트를 선택한다."""
        sql=text(f"""SELECT c.api_clnt_id,c.api_clnt_cd,s.sys_id,s.sys_cd
          FROM {self._schema}.api_clnt_mst c JOIN {self._schema}.ext_sys_mst s USING(sys_id)
          WHERE c.use_yn='Y' AND s.use_yn='Y' AND EXISTS(
            SELECT 1 FROM {self._schema}.sys_ai_agt_map m JOIN {self._schema}.ai_agt_mst a USING(agt_id)
            WHERE m.sys_id=s.sys_id AND m.use_yn='Y' AND m.dflt_yn='Y' AND a.use_yn='Y')
          ORDER BY s.sys_cd,c.api_clnt_cd LIMIT 1""")
        async with self._database.session() as session:
            row=(await session.execute(sql)).mappings().one_or_none()
        return self._principal(row) if row else None

    async def list_available_agents(
        self,
        principal: GatewayPrincipal,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        """호출 시스템의 Agent 목록과 시스템별 공개 수명주기를 조회한다."""
        sql = text(
            f"""
            SELECT a.agt_cd,
                   a.agt_nm,
                   a.agt_desc_cn,
                   a.agt_typ_cd,
                   a.agt_mode_cd,
                   a.long_task_use_yn,
                   (a.use_yn = 'Y'
                    AND m.use_yn = 'Y'
                    AND m.eff_strt_dt <= CURRENT_TIMESTAMP
                    AND (m.eff_end_dt IS NULL OR m.eff_end_dt > CURRENT_TIMESTAMP)) AS active,
                   a.reg_dt AS registered_at,
                   m.eff_strt_dt AS released_at,
                   GREATEST(
                       COALESCE(a.mod_dt, a.reg_dt),
                       COALESCE(m.mod_dt, m.reg_dt)
                   ) AS updated_at,
                   CASE
                       WHEN a.use_yn = 'N' OR m.use_yn = 'N' OR m.eff_end_dt <= CURRENT_TIMESTAMP
                       THEN COALESCE(m.eff_end_dt,GREATEST(COALESCE(a.mod_dt,a.reg_dt),COALESCE(m.mod_dt,m.reg_dt)))
                       ELSE NULL
                   END AS deleted_at
              FROM {self._schema}.sys_ai_agt_map m
             JOIN {self._schema}.ai_agt_mst a
                ON a.agt_id = m.agt_id
             WHERE m.sys_id = :system_id
               AND m.eff_strt_dt <= CURRENT_TIMESTAMP
               AND (
                    :include_inactive
                    OR (
                        a.use_yn = 'Y'
                        AND m.use_yn = 'Y'
                        AND (m.eff_end_dt IS NULL OR m.eff_end_dt > CURRENT_TIMESTAMP)
                    )
               )
             ORDER BY m.route_prty_no, a.agt_nm, a.agt_id
            """
        )
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    sql,
                    {
                        "system_id": principal.sys_id,
                        "include_inactive": include_inactive,
                    },
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def resolve_supervisor_route(
        self,
        principal: GatewayPrincipal,
        public_agent_id: str,
    ) -> SupervisorRoute | None:
        """시스템에 허용된 Supervisor 논리 ID를 실제 endpoint 정책으로 변환한다."""
        sql = text(
            f"""
            SELECT a.agt_id,
                   a.agt_cd,
                   COALESCE(w.ws_url, a.dbx_ws_url) AS dbx_ws_url,
                   a.dbx_serving_ep_nm,
                   COALESCE(a.secret_ref_nm, s.dbx_cred_ref_nm) AS credential_ref,
                   a.long_task_use_yn,
                   a.first_event_timeout_sec,
                   a.idle_timeout_sec,
                   a.total_timeout_sec,
                   a.max_continue_cnt,
                   a.max_active_run_cnt,
                   a.history_max_turn_cnt,
                   a.history_max_char_cnt
              FROM {self._schema}.sys_ai_agt_map m
              JOIN {self._schema}.ext_sys_mst s
                ON s.sys_id = m.sys_id AND s.use_yn = 'Y'
              JOIN {self._schema}.ai_agt_mst a
                ON a.agt_id = m.agt_id
               AND a.use_yn = 'Y'
               AND a.agt_typ_cd = 'SUPERVISOR'
               AND a.agt_mode_cd = 'SUPERVISOR'
              LEFT JOIN {self._schema}.dbx_ws_mst w
                ON w.ws_id = a.ws_id AND w.use_yn = 'Y'
             WHERE m.sys_id = :system_id
               AND m.use_yn = 'Y'
               AND m.eff_strt_dt <= CURRENT_TIMESTAMP
               AND (m.eff_end_dt IS NULL OR m.eff_end_dt > CURRENT_TIMESTAMP)
               AND a.agt_cd = :public_agent_id
             ORDER BY m.route_prty_no, m.eff_strt_dt DESC
             LIMIT 1
            """
        )
        async with self._database.session() as session:
            row = (
                await session.execute(
                    sql,
                    {
                        "system_id": principal.sys_id,
                        "public_agent_id": public_agent_id,
                    },
                )
            ).mappings().one_or_none()
        if row is None:
            return None
        return SupervisorRoute(
            agt_id=row["agt_id"],
            agt_cd=row["agt_cd"],
            databricks_host=row["dbx_ws_url"],
            serving_endpoint_name=row["dbx_serving_ep_nm"],
            credential_ref=row["credential_ref"],
            long_task_enabled=row["long_task_use_yn"] == "Y",
            first_event_timeout_seconds=row["first_event_timeout_sec"],
            idle_timeout_seconds=row["idle_timeout_sec"],
            total_timeout_seconds=row["total_timeout_sec"],
            max_continue_count=row["max_continue_cnt"],
            max_active_run_count=row["max_active_run_cnt"],
            history_max_turn_count=row["history_max_turn_cnt"],
            history_max_char_count=row["history_max_char_cnt"],
        )

    async def authenticate_by_oauth_subject(self, oauth_subject: str) -> GatewayPrincipal | None:
        """Databricks Apps가 전달한 OAuth Principal 식별자로 클라이언트를 조회한다."""
        sql = text(
            f"""
            SELECT c.api_clnt_id,
                   c.api_clnt_cd,
                   s.sys_id,
                   s.sys_cd
              FROM {self._schema}.api_clnt_mst c
              JOIN {self._schema}.ext_sys_mst s
                ON s.sys_id = c.sys_id
             WHERE c.oauth_prnc_id = :oauth_subject
               AND c.auth_typ_cd = 'OAUTH2'
               AND c.use_yn = 'Y'
               AND s.use_yn = 'Y'
            """
        )
        async with self._database.session() as session:
            row = (
                await session.execute(sql, {"oauth_subject": oauth_subject})
            ).mappings().one_or_none()
        return self._principal(row) if row else None

    async def resolve_route(
        self,
        principal: GatewayPrincipal,
        public_agent_id: str,
        mode: str,
    ) -> GenieRoute | None:
        """인증 시스템에 허용된 활성 Genie Agent를 복합 CTE로 결정한다."""
        upstream_id_column = "dbx_space_id" if mode == "CHAT" else "dbx_agent_id"
        sql = text(
            f"""
            WITH route_candidate AS (
                SELECT a.agt_id,
                       a.agt_cd,
                       a.agt_mode_cd,
                       a.dbx_ws_url,
                       a.dbx_space_id,
                       a.dbx_agent_id,
                       s.dbx_cred_ref_nm,
                       m.route_prty_no,
                       m.dflt_yn
                  FROM {self._schema}.sys_ai_agt_map m
                  JOIN {self._schema}.ext_sys_mst s
                    ON s.sys_id = m.sys_id
                   AND s.use_yn = 'Y'
                  JOIN {self._schema}.ai_agt_mst a
                    ON a.agt_id = m.agt_id
                   AND a.use_yn = 'Y'
                 WHERE m.sys_id = :system_id
                   AND m.use_yn = 'Y'
                   AND m.eff_strt_dt <= CURRENT_TIMESTAMP
                   AND (m.eff_end_dt IS NULL OR m.eff_end_dt > CURRENT_TIMESTAMP)
                   AND (
                       a.agt_cd = :public_agent_id
                       OR a.{upstream_id_column} = :public_agent_id
                   )
                   AND a.{upstream_id_column} IS NOT NULL
                   AND (a.agt_mode_cd = :mode OR a.agt_mode_cd = 'BOTH')
                 ORDER BY CASE WHEN a.agt_cd = :public_agent_id THEN 0 ELSE 1 END,
                          m.dflt_yn DESC,
                          m.route_prty_no ASC,
                          m.eff_strt_dt DESC
                 LIMIT 1
            )
            SELECT agt_id,
                   agt_cd,
                   agt_mode_cd,
                   dbx_ws_url,
                   dbx_space_id,
                   dbx_agent_id,
                   dbx_cred_ref_nm
              FROM route_candidate
            """
        )
        async with self._database.session() as session:
            row = (
                await session.execute(
                    sql,
                    {
                        "system_id": principal.sys_id,
                        "public_agent_id": public_agent_id,
                        "mode": mode,
                    },
                )
            ).mappings().one_or_none()
        if row is None:
            return None
        return GenieRoute(
            agt_id=row["agt_id"],
            agt_cd=row["agt_cd"],
            agt_mode_cd=row["agt_mode_cd"],
            public_agent_id=public_agent_id,
            databricks_host=row["dbx_ws_url"],
            databricks_space_id=row["dbx_space_id"],
            databricks_agent_id=row["dbx_agent_id"],
            credential_ref=row["dbx_cred_ref_nm"],
        )

    async def create_conversation_owner(
        self,
        principal: GatewayPrincipal,
        route: GenieRoute,
        user_id: str,
        conversation_id: str,
        title: str | None = None,
        mode: str = "CHAT",
    ) -> ConversationOwner:
        """신규 Databricks Conversation을 인증 클라이언트와 사용자에게 귀속한다."""
        insert_sql = text(
            f"""
            INSERT INTO {self._schema}.ai_cnvs_mst (
                cnvs_uid, api_clnt_id, agt_id, usr_id, dbx_cnvs_id, cnvs_mode_cd,
                cnvs_title_nm, cnvs_stts_cd, use_yn, reg_id
            )
            VALUES (
                :conversation_uid, :api_client_id, :agent_id, :user_id, :conversation_id, :mode,
                :title, 'ACTIVE', 'Y', :registration_id
            )
            ON CONFLICT DO NOTHING
            RETURNING cnvs_id, api_clnt_id, agt_id, usr_id, dbx_cnvs_id
            """
        )
        select_sql = text(
            f"""
            SELECT cnvs_id, api_clnt_id, agt_id, usr_id, dbx_cnvs_id
              FROM {self._schema}.ai_cnvs_mst
             WHERE agt_id = :agent_id
               AND dbx_cnvs_id = :conversation_id
            """
        )
        params = {
            "conversation_uid": f"cnvs_{uuid4().hex}",
            "api_client_id": principal.api_clnt_id,
            "agent_id": route.agt_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "mode": mode,
            "title": title,
            "registration_id": principal.api_clnt_cd,
        }
        async with self._database.session() as session:
            row = (await session.execute(insert_sql, params)).mappings().one_or_none()
            if row is None:
                row = (await session.execute(select_sql, params)).mappings().one_or_none()
            if row is None:
                raise GatewayError(503, "TEMPORARILY_UNAVAILABLE", "Conversation ownership could not be saved.")
            if row["api_clnt_id"] != principal.api_clnt_id or row["usr_id"] != user_id:
                raise GatewayError(409, "RESOURCE_ALREADY_EXISTS", "Conversation ownership conflicts with an existing record.")
        return self._conversation_owner(row)

    async def assert_conversation_owner(
        self,
        principal: GatewayPrincipal,
        route: GenieRoute,
        user_id: str,
        conversation_id: str,
        mode: str | None = None,
    ) -> ConversationOwner:
        """Databricks 호출 전에 Conversation 소유권을 검증한다."""
        sql = text(
            f"""
            SELECT cnvs_id, api_clnt_id, agt_id, usr_id, dbx_cnvs_id
              FROM {self._schema}.ai_cnvs_mst
             WHERE api_clnt_id = :api_client_id
               AND agt_id = :agent_id
               AND usr_id = :user_id
               AND dbx_cnvs_id = :conversation_id
               AND (
                   CAST(:mode AS VARCHAR(20)) IS NULL
                   OR cnvs_mode_cd = CAST(:mode AS VARCHAR(20))
               )
               AND use_yn = 'Y'
               AND cnvs_stts_cd <> 'DELETED'
            """
        )
        async with self._database.session() as session:
            row = (
                await session.execute(
                    sql,
                    {
                        "api_client_id": principal.api_clnt_id,
                        "agent_id": route.agt_id,
                        "user_id": user_id,
                        "conversation_id": conversation_id,
                        "mode": mode,
                    },
                )
            ).mappings().one_or_none()
        if row is None:
            raise GatewayError(403, "PERMISSION_DENIED", "The conversation does not belong to this user.")
        return self._conversation_owner(row)

    async def touch_conversation(self, conversation_pk: int) -> None:
        """성공적으로 접근한 Conversation의 최근 사용 시각을 갱신한다."""
        sql = text(
            f"""
            UPDATE {self._schema}.ai_cnvs_mst
               SET last_msg_dt = CURRENT_TIMESTAMP,
                   mod_dt = CURRENT_TIMESTAMP
             WHERE cnvs_id = :conversation_pk
            """
        )
        async with self._database.session() as session:
            await session.execute(sql, {"conversation_pk": conversation_pk})

    async def mark_conversation_deleted(self, conversation_pk: int, modifier_id: str) -> None:
        """Databricks 삭제 성공 후 Gateway 소유권 레코드를 비활성화한다."""
        sql = text(
            f"""
            UPDATE {self._schema}.ai_cnvs_mst
               SET cnvs_stts_cd = 'DELETED',
                   use_yn = 'N',
                   end_dt = CURRENT_TIMESTAMP,
                   mod_dt = CURRENT_TIMESTAMP,
                   mod_id = :modifier_id
             WHERE cnvs_id = :conversation_pk
            """
        )
        async with self._database.session() as session:
            await session.execute(
                sql,
                {"conversation_pk": conversation_pk, "modifier_id": modifier_id},
            )

    async def list_user_conversations(
        self,
        principal: GatewayPrincipal,
        route: GenieRoute,
        user_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """공유 SP의 전체 목록 대신 Gateway 소유권 기준 사용자 대화만 반환한다."""
        sql = text(
            f"""
            SELECT dbx_cnvs_id AS conversation_id,
                   cnvs_title_nm AS title,
                   CAST(EXTRACT(EPOCH FROM str_dt) * 1000 AS BIGINT) AS created_timestamp,
                   CASE cnvs_mode_cd
                       WHEN 'AGENT' THEN 'GENIE_CONVERSATION_TYPE_AGENT'
                       ELSE 'GENIE_CONVERSATION_TYPE_CHAT'
                   END AS agent_type
              FROM {self._schema}.ai_cnvs_mst
             WHERE api_clnt_id = :api_client_id
               AND agt_id = :agent_id
               AND usr_id = :user_id
               AND dbx_cnvs_id IS NOT NULL
               AND use_yn = 'Y'
               AND cnvs_stts_cd <> 'DELETED'
             ORDER BY str_dt DESC
             LIMIT :limit
            """
        )
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    sql,
                    {
                        "api_client_id": principal.api_clnt_id,
                        "agent_id": route.agt_id,
                        "user_id": user_id,
                        "limit": limit,
                    },
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def list_custom_chat_agents(self, principal: GatewayPrincipal) -> list[dict[str, Any]]:
        """커스텀 Chat에서 사용할 수 있는 논리 Agent 이름과 기본값을 조회한다."""
        sql = text(
            f"""
            SELECT COALESCE(a.dbx_space_id, a.dbx_agent_id) AS agent_id,
                   a.dbx_space_id AS chat_agent_id,
                   a.dbx_agent_id AS agent_mode_id,
                   a.agt_nm AS agent_name,
                   m.dflt_yn AS is_default
              FROM {self._schema}.sys_ai_agt_map m
              JOIN {self._schema}.ai_agt_mst a
                ON a.agt_id = m.agt_id
               AND a.use_yn = 'Y'
             WHERE m.sys_id = :system_id
               AND m.use_yn = 'Y'
               AND m.eff_strt_dt <= CURRENT_TIMESTAMP
               AND (m.eff_end_dt IS NULL OR m.eff_end_dt > CURRENT_TIMESTAMP)
               AND (a.dbx_space_id IS NOT NULL OR a.dbx_agent_id IS NOT NULL)
             ORDER BY m.dflt_yn DESC, m.route_prty_no, a.agt_nm
            """
        )
        async with self._database.session() as session:
            rows = (
                await session.execute(sql, {"system_id": principal.sys_id})
            ).mappings().all()
        return [
            {
                "agent_id": row["agent_id"],
                "chat_agent_id": row["chat_agent_id"],
                "agent_mode_id": row["agent_mode_id"],
                "agent_name": row["agent_name"],
                "is_default": row["is_default"] == "Y",
            }
            for row in rows
        ]

    async def list_custom_conversations(
        self,
        principal: GatewayPrincipal,
        route: GenieRoute,
        user_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """커스텀 UI 사이드바용 Chat/Agent 통합 대화 이력을 조회한다."""
        sql = text(
            f"""
            SELECT c.dbx_cnvs_id AS conversation_id,
                   COALESCE(
                       NULLIF(BTRIM(c.cnvs_title_nm), ''),
                       NULLIF(BTRIM(first_message.msg_cn), ''),
                       '새 대화'
                   ) AS title,
                   CAST(EXTRACT(EPOCH FROM c.str_dt) * 1000 AS BIGINT) AS created_timestamp,
                   CAST(
                       EXTRACT(EPOCH FROM COALESCE(c.last_msg_dt, c.mod_dt, c.str_dt)) * 1000
                       AS BIGINT
                   ) AS last_updated_timestamp
                  ,c.cnvs_mode_cd AS conversation_mode
              FROM {self._schema}.ai_cnvs_mst c
              LEFT JOIN LATERAL (
                  SELECT m.msg_cn
                    FROM {self._schema}.genie_msg_hst m
                   WHERE m.cnvs_id = c.cnvs_id
                     AND m.msg_role_cd = 'USER'
                   ORDER BY m.msg_seq_no
                   LIMIT 1
              ) first_message ON TRUE
             WHERE c.api_clnt_id = :api_client_id
               AND c.agt_id = :agent_id
               AND c.usr_id = :user_id
               AND c.dbx_cnvs_id IS NOT NULL
               AND c.use_yn = 'Y'
               AND c.cnvs_stts_cd <> 'DELETED'
             ORDER BY COALESCE(c.last_msg_dt, c.mod_dt, c.str_dt) DESC
             LIMIT :limit
            """
        )
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    sql,
                    {
                        "api_client_id": principal.api_clnt_id,
                        "agent_id": route.agt_id,
                        "user_id": user_id,
                        "limit": limit,
                    },
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def rename_conversation(
        self,
        conversation_pk: int,
        title: str,
        modifier_id: str,
    ) -> None:
        """소유권 검증이 끝난 Conversation의 운영자 표시 제목을 변경한다."""
        sql = text(
            f"""
            UPDATE {self._schema}.ai_cnvs_mst
               SET cnvs_title_nm = :title,
                   mod_dt = CURRENT_TIMESTAMP,
                   mod_id = :modifier_id
             WHERE cnvs_id = :conversation_pk
            """
        )
        async with self._database.session() as session:
            await session.execute(
                sql,
                {
                    "conversation_pk": conversation_pk,
                    "title": title,
                    "modifier_id": modifier_id,
                },
            )

    async def save_chat_messages(
        self,
        owner: ConversationOwner,
        messages: list[GenieMessageSnapshot],
        registration_id: str,
    ) -> int | None:
        """Chat Message·Attachment·상태를 Conversation 단위 트랜잭션으로 Upsert한다."""
        if not messages:
            return None

        saved_message_id: int | None = None
        async with self._database.session() as session:
            # 같은 Conversation에 대한 동시 polling/메시지 생성을 직렬화해 순번 충돌을 막는다.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:conversation_pk)"),
                {"conversation_pk": owner.conversation_pk},
            )
            for message in messages:
                if message.conversation_id != owner.databricks_conversation_id:
                    raise GatewayError(
                        502,
                        "INTERNAL_ERROR",
                        "Databricks returned a message for a different conversation.",
                    )
                saved_message_id = await self._upsert_chat_message(
                    session,
                    owner,
                    message,
                    registration_id,
                )

            await session.execute(
                text(
                    f"""
                    UPDATE {self._schema}.ai_cnvs_mst
                       SET last_msg_dt = CURRENT_TIMESTAMP,
                           mod_dt = CURRENT_TIMESTAMP,
                           mod_id = :registration_id
                     WHERE cnvs_id = :conversation_pk
                    """
                ),
                {
                    "conversation_pk": owner.conversation_pk,
                    "registration_id": registration_id,
                },
            )
        return saved_message_id

    async def _upsert_chat_message(
        self,
        session: AsyncSession,
        owner: ConversationOwner,
        message: GenieMessageSnapshot,
        registration_id: str,
    ) -> int:
        existing = (
            await session.execute(
                text(
                    f"""
                    SELECT msg_id
                      FROM {self._schema}.genie_msg_hst
                     WHERE cnvs_id = :conversation_pk
                       AND dbx_msg_id = :databricks_message_id
                     FOR UPDATE
                    """
                ),
                {
                    "conversation_pk": owner.conversation_pk,
                    "databricks_message_id": message.databricks_message_id,
                },
            )
        ).mappings().one_or_none()

        params = {
            "conversation_pk": owner.conversation_pk,
            "databricks_message_id": message.databricks_message_id,
            "role_code": message.role_code,
            "content": message.content,
            "status_code": message.status_code,
            "raw_data": json.dumps(message.raw_data, ensure_ascii=False, separators=(",", ":")),
            "created_at": message.created_at,
            "updated_at": message.updated_at,
            "registration_id": registration_id,
        }
        if existing is None:
            row = (
                await session.execute(
                    text(
                        f"""
                        INSERT INTO {self._schema}.genie_msg_hst (
                            cnvs_id, dbx_msg_id, msg_seq_no, msg_role_cd, msg_cn,
                            msg_stts_cd, msg_data, dbx_crt_dt, dbx_mod_dt, reg_id
                        )
                        SELECT :conversation_pk,
                               :databricks_message_id,
                               COALESCE(MAX(msg_seq_no), 0) + 1,
                               :role_code,
                               :content,
                               :status_code,
                               CAST(:raw_data AS JSONB),
                               :created_at,
                               :updated_at,
                               :registration_id
                          FROM {self._schema}.genie_msg_hst
                         WHERE cnvs_id = :conversation_pk
                        RETURNING msg_id
                        """
                    ),
                    params,
                )
            ).mappings().one()
            message_pk = int(row["msg_id"])
        else:
            message_pk = int(existing["msg_id"])
            await session.execute(
                text(
                    f"""
                    UPDATE {self._schema}.genie_msg_hst
                       SET msg_role_cd = :role_code,
                           msg_cn = COALESCE(:content, msg_cn),
                           msg_stts_cd = :status_code,
                           msg_data = CAST(:raw_data AS JSONB),
                           dbx_crt_dt = COALESCE(:created_at, dbx_crt_dt),
                           dbx_mod_dt = COALESCE(:updated_at, dbx_mod_dt)
                     WHERE msg_id = :message_pk
                    """
                ),
                {**params, "message_pk": message_pk},
            )

        await self._sync_message_attachments(
            session,
            message_pk,
            message.attachments,
            registration_id,
        )
        await self._append_message_status(
            session,
            message_pk,
            message,
            registration_id,
        )
        return message_pk

    async def _sync_message_attachments(
        self,
        session: AsyncSession,
        message_pk: int,
        attachments: tuple[GenieAttachmentSnapshot, ...],
        registration_id: str,
    ) -> None:
        """최신 목록을 반영하되 동일 Databricks Attachment의 등록 이력을 유지한다."""
        existing_rows = (
            await session.execute(
                text(
                    f"""
                    SELECT msg_atch_id, dbx_atch_id, atch_seq_no
                      FROM {self._schema}.genie_msg_atch_dtl
                     WHERE msg_id = :message_pk
                     FOR UPDATE
                    """
                ),
                {"message_pk": message_pk},
            )
        ).mappings().all()

        existing_by_databricks_id = {
            row["dbx_atch_id"]: row
            for row in existing_rows
            if row["dbx_atch_id"] is not None
        }
        existing_by_sequence = {int(row["atch_seq_no"]): row for row in existing_rows}
        retained_ids: set[int] = set()
        matched_attachment_ids: list[int | None] = []

        for attachment in attachments:
            matched = None
            if attachment.databricks_attachment_id is not None:
                matched = existing_by_databricks_id.get(attachment.databricks_attachment_id)
            else:
                candidate = existing_by_sequence.get(attachment.sequence_no)
                if candidate is not None and candidate["dbx_atch_id"] is None:
                    matched = candidate

            matched_id = int(matched["msg_atch_id"]) if matched is not None else None
            if matched_id is not None and matched_id in retained_ids:
                matched_id = None
            if matched_id is not None:
                retained_ids.add(matched_id)
            matched_attachment_ids.append(matched_id)

        obsolete_ids = [
            int(row["msg_atch_id"])
            for row in existing_rows
            if int(row["msg_atch_id"]) not in retained_ids
        ]
        if obsolete_ids:
            await session.execute(
                text(
                    f"""
                    DELETE FROM {self._schema}.genie_msg_atch_dtl
                     WHERE msg_atch_id = ANY(CAST(:obsolete_ids AS BIGINT[]))
                    """
                ),
                {"obsolete_ids": obsolete_ids},
            )

        # 순번이 서로 교환되는 경우의 유니크 충돌을 피하도록 기존 행을 임시 순번으로 이동한다.
        if retained_ids:
            max_sequence = max(
                [int(row["atch_seq_no"]) for row in existing_rows]
                + [attachment.sequence_no for attachment in attachments]
            )
            await session.execute(
                text(
                    f"""
                    UPDATE {self._schema}.genie_msg_atch_dtl
                       SET atch_seq_no = atch_seq_no + :sequence_offset
                     WHERE msg_atch_id = ANY(CAST(:retained_ids AS BIGINT[]))
                    """
                ),
                {
                    "sequence_offset": max_sequence + len(attachments) + 1,
                    "retained_ids": list(retained_ids),
                },
            )

        for attachment, existing_id in zip(attachments, matched_attachment_ids, strict=True):
            await self._save_message_attachment(
                session,
                message_pk,
                attachment,
                registration_id,
                existing_id,
            )

    async def _save_message_attachment(
        self,
        session: AsyncSession,
        message_pk: int,
        attachment: GenieAttachmentSnapshot,
        registration_id: str,
        existing_id: int | None,
    ) -> None:

        params = {
            "message_pk": message_pk,
            "databricks_attachment_id": attachment.databricks_attachment_id,
            "sequence_no": attachment.sequence_no,
            "attachment_type_code": attachment.attachment_type_code,
            "attachment_purpose_code": attachment.attachment_purpose_code,
            "text_content": attachment.text_content,
            "query_title": attachment.query_title,
            "query_content": attachment.query_content,
            "statement_id": attachment.statement_id,
            "visualization_title": attachment.visualization_title,
            "raw_data": json.dumps(attachment.raw_data, ensure_ascii=False, separators=(",", ":")),
            "registration_id": registration_id,
        }
        if existing_id is None:
            await session.execute(
                text(
                    f"""
                    INSERT INTO {self._schema}.genie_msg_atch_dtl (
                        msg_id, dbx_atch_id, atch_seq_no, atch_typ_cd, atch_purps_cd,
                        text_cn, qry_title_nm, qry_cn, sql_stmt_id, viz_title_nm,
                        atch_data, reg_id
                    )
                    VALUES (
                        :message_pk, :databricks_attachment_id, :sequence_no,
                        :attachment_type_code, :attachment_purpose_code, :text_content,
                        :query_title, :query_content, :statement_id, :visualization_title,
                        CAST(:raw_data AS JSONB), :registration_id
                    )
                    """
                ),
                params,
            )
        else:
            await session.execute(
                text(
                    f"""
                    UPDATE {self._schema}.genie_msg_atch_dtl
                       SET dbx_atch_id = COALESCE(:databricks_attachment_id, dbx_atch_id),
                           atch_seq_no = :sequence_no,
                           atch_typ_cd = :attachment_type_code,
                           atch_purps_cd = :attachment_purpose_code,
                           text_cn = :text_content,
                           qry_title_nm = :query_title,
                           qry_cn = :query_content,
                           sql_stmt_id = :statement_id,
                           viz_title_nm = :visualization_title,
                           atch_data = CAST(:raw_data AS JSONB)
                     WHERE msg_atch_id = :message_attachment_pk
                    """
                ),
                {**params, "message_attachment_pk": existing_id},
            )

    async def _append_message_status(
        self,
        session: AsyncSession,
        message_pk: int,
        message: GenieMessageSnapshot,
        registration_id: str,
    ) -> None:
        latest = (
            await session.execute(
                text(
                    f"""
                    SELECT msg_stts_cd
                      FROM {self._schema}.genie_msg_stts_hst
                     WHERE msg_id = :message_pk
                     ORDER BY stts_seq_no DESC
                     LIMIT 1
                    """
                ),
                {"message_pk": message_pk},
            )
        ).mappings().one_or_none()
        if latest is not None and latest["msg_stts_cd"] == message.status_code:
            return

        status_data = {
            "status": message.status_code,
            "error": message.raw_data.get("error"),
            "last_updated_timestamp": message.raw_data.get("last_updated_timestamp"),
        }
        await session.execute(
            text(
                f"""
                INSERT INTO {self._schema}.genie_msg_stts_hst (
                    msg_id, stts_seq_no, msg_stts_cd, err_cd, err_msg_cn,
                    stts_data, reg_id
                )
                SELECT :message_pk,
                       COALESCE(MAX(stts_seq_no), 0) + 1,
                       :status_code,
                       :error_code,
                       :error_message,
                       CAST(:status_data AS JSONB),
                       :registration_id
                  FROM {self._schema}.genie_msg_stts_hst
                 WHERE msg_id = :message_pk
                """
            ),
            {
                "message_pk": message_pk,
                "status_code": message.status_code,
                "error_code": message.error_code,
                "error_message": message.error_message,
                "status_data": json.dumps(status_data, ensure_ascii=False, separators=(",", ":")),
                "registration_id": registration_id,
            },
        )

    async def record_api_audit(self, record: ApiAuditRecord) -> None:
        """요청·응답 메타데이터를 append-only 이력으로 저장한다."""
        sql = text(
            f"""
            INSERT INTO {self._schema}.api_req_hst (
                trace_id,
                api_clnt_id,
                agt_id,
                cnvs_id,
                msg_id,
                http_mthd_cd,
                req_uri,
                dbx_req_id,
                req_data,
                rsp_data,
                rsp_http_stts_no,
                err_cd,
                err_msg_cn,
                proc_ms,
                src_ip_val,
                req_dt,
                rsp_dt,
                reg_id
            )
            VALUES (
                :trace_id,
                :api_client_id,
                :agent_id,
                :conversation_id,
                :message_id,
                :method,
                :request_uri,
                :databricks_request_id,
                CAST(:request_data AS JSONB),
                CAST(:response_data AS JSONB),
                :response_status,
                :error_code,
                :error_message,
                CAST(:processing_ms AS BIGINT),
                CAST(:source_ip AS INET),
                CURRENT_TIMESTAMP - (CAST(:processing_ms AS BIGINT) * INTERVAL '1 millisecond'),
                CURRENT_TIMESTAMP,
                :registration_id
            )
            """
        )
        params = {
            "trace_id": record.trace_id,
            "api_client_id": record.principal.api_clnt_id,
            "agent_id": record.route.agt_id if record.route else None,
            "conversation_id": record.conversation_id,
            "message_id": record.message_id,
            "method": record.method,
            "request_uri": record.request_uri,
            "databricks_request_id": record.databricks_request_id,
            "request_data": record.request_data,
            "response_data": record.response_data,
            "response_status": record.response_status,
            "error_code": record.error_code,
            "error_message": record.error_message,
            "processing_ms": record.processing_ms,
            "source_ip": record.source_ip,
            "registration_id": record.principal.api_clnt_cd,
        }
        async with self._database.session() as session:
            await session.execute(sql, params)

    @staticmethod
    def _principal(row: Any) -> GatewayPrincipal:
        return GatewayPrincipal(
            api_clnt_id=row["api_clnt_id"],
            api_clnt_cd=row["api_clnt_cd"],
            sys_id=row["sys_id"],
            sys_cd=row["sys_cd"],
        )

    @staticmethod
    def _conversation_owner(row: Any) -> ConversationOwner:
        return ConversationOwner(
            conversation_pk=row["cnvs_id"],
            databricks_conversation_id=row["dbx_cnvs_id"],
            api_client_id=row["api_clnt_id"],
            user_id=row["usr_id"],
            agent_id=row["agt_id"],
        )

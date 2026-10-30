"""Supervisor Conversation과 Run 상태를 관리하는 Repository."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4
from typing import Any
import json

from sqlalchemy import text

from app.core.database import Database
from app.core.errors import GatewayError
from app.domain.models import GatewayPrincipal, SupervisorRoute


ACTIVE_RUN_STATUSES = ("QUEUED", "STREAMING", "CONTINUING", "WAITING_APPROVAL", "CANCEL_REQUESTED")
PAUSED_RUN_STATUSES = ("WAITING_APPROVAL",)
TERMINAL_RUN_STATUSES = ("COMPLETED", "CANCELLED", "TIMED_OUT", "FAILED")
ALLOWED_TRANSITIONS = {
    "QUEUED": {"STREAMING", "CANCEL_REQUESTED", "TIMED_OUT", "FAILED"},
    "STREAMING": {"CONTINUING", "WAITING_APPROVAL", "COMPLETED", "CANCEL_REQUESTED", "TIMED_OUT", "FAILED"},
    "CONTINUING": {"STREAMING", "CANCEL_REQUESTED", "TIMED_OUT", "FAILED"},
    "WAITING_APPROVAL": {"QUEUED", "CANCEL_REQUESTED", "TIMED_OUT", "FAILED"},
    "CANCEL_REQUESTED": {"CANCELLED", "TIMED_OUT", "FAILED"},
}


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_pk: int
    run_uid: str
    conversation_pk: int
    conversation_uid: str
    status: str
    trace_id: str
    started_at: datetime
    deadline_at: datetime
    reused: bool = False


class RunRepository:
    """DB를 단일 진실 원천으로 사용하는 Run 저장소."""

    def __init__(self, database: Database, schema: str = "genie_gw") -> None:
        if re.fullmatch(r"[a-z_][a-z0-9_]*", schema) is None:
            raise ValueError("database schema must be a lowercase SQL identifier")
        self._database = database
        self._schema = schema

    async def create_or_reuse_run(
        self,
        principal: GatewayPrincipal,
        route: SupervisorRoute,
        user_id: str,
        conversation_uid: str | None,
        request_hash: str,
        trace_id: str,
        deadline_at: datetime,
        gateway_limit: int,
        user_limit: int,
        user_agent_limit: int,
        reuse_terminal: bool = False,
    ) -> RunRecord:
        """소유권·중복·용량 검사를 동일 트랜잭션에서 수행한다."""
        async with self._database.session() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": "genie_gw.supervisor_run_capacity"},
            )
            config_rows = (
                await session.execute(
                    text(
                        f"""SELECT cfg_key_nm,cfg_val_cn FROM {self._schema}.gw_oper_cfg_mst
                             WHERE cfg_key_nm IN (
                               'SUPERVISOR_GATEWAY_MAX_ACTIVE_RUNS',
                               'SUPERVISOR_USER_MAX_ACTIVE_RUNS',
                               'SUPERVISOR_USER_AGENT_MAX_ACTIVE_RUNS'
                             )"""
                    )
                )
            ).mappings().all()
            runtime_limits = {row["cfg_key_nm"]: row["cfg_val_cn"] for row in config_rows}
            gateway_limit = self._positive_limit(runtime_limits.get("SUPERVISOR_GATEWAY_MAX_ACTIVE_RUNS"), gateway_limit)
            user_limit = self._positive_limit(runtime_limits.get("SUPERVISOR_USER_MAX_ACTIVE_RUNS"), user_limit)
            user_agent_limit = self._positive_limit(runtime_limits.get("SUPERVISOR_USER_AGENT_MAX_ACTIVE_RUNS"), user_agent_limit)
            if conversation_uid:
                conversation = (
                    await session.execute(
                        text(
                            f"""
                            SELECT cnvs_id, cnvs_uid
                              FROM {self._schema}.ai_cnvs_mst
                             WHERE cnvs_uid = :conversation_uid
                               AND api_clnt_id = :api_client_id
                               AND agt_id = :agent_id
                               AND usr_id = :user_id
                               AND cnvs_mode_cd = 'SUPERVISOR'
                               AND use_yn = 'Y'
                               AND cnvs_stts_cd <> 'DELETED'
                            """
                        ),
                        {
                            "conversation_uid": conversation_uid,
                            "api_client_id": principal.api_clnt_id,
                            "agent_id": route.agt_id,
                            "user_id": user_id,
                        },
                    )
                ).mappings().one_or_none()
                if conversation is None:
                    raise GatewayError(
                        403,
                        "CONVERSATION_ACCESS_DENIED",
                        "Conversation access is denied.",
                    )
            else:
                active_for_new_conversation = None
                if reuse_terminal:
                    active_for_new_conversation = (
                        await session.execute(
                            text(
                                f"""
                                SELECT r.run_id, r.run_uid, r.cnvs_id, c.cnvs_uid,
                                       r.run_stts_cd, r.trace_id, r.str_dt, r.deadline_dt
                                  FROM {self._schema}.ai_run_mst r
                                  JOIN {self._schema}.ai_cnvs_mst c USING (cnvs_id)
                                 WHERE c.api_clnt_id = :api_client_id
                                   AND c.agt_id = :agent_id
                                   AND c.usr_id = :user_id
                                   AND c.cnvs_mode_cd = 'SUPERVISOR'
                                   AND r.request_hash = :request_hash
                                   AND (
                                       :reuse_terminal
                                       OR r.run_stts_cd = ANY(CAST(:active_statuses AS VARCHAR[]))
                                   )
                                 ORDER BY r.str_dt DESC
                                 LIMIT 1
                                """
                            ),
                            {
                                "api_client_id": principal.api_clnt_id,
                                "agent_id": route.agt_id,
                                "user_id": user_id,
                                "request_hash": request_hash,
                                "active_statuses": list(ACTIVE_RUN_STATUSES),
                                "reuse_terminal": reuse_terminal,
                            },
                        )
                    ).mappings().one_or_none()
                if active_for_new_conversation:
                    return self._record(active_for_new_conversation, reused=True)
                conversation_uid = f"cnvs_{uuid4().hex}"
                conversation = (
                    await session.execute(
                        text(
                            f"""
                            INSERT INTO {self._schema}.ai_cnvs_mst (
                                cnvs_uid, api_clnt_id, agt_id, usr_id, cnvs_mode_cd,
                                cnvs_stts_cd, use_yn, reg_id
                            ) VALUES (
                                :conversation_uid, :api_client_id, :agent_id, :user_id,
                                'SUPERVISOR', 'CREATED', 'Y', :registration_id
                            )
                            RETURNING cnvs_id, cnvs_uid
                            """
                        ),
                        {
                            "conversation_uid": conversation_uid,
                            "api_client_id": principal.api_clnt_id,
                            "agent_id": route.agt_id,
                            "user_id": user_id,
                            "registration_id": principal.api_clnt_cd,
                        },
                    )
                ).mappings().one()

            active = (
                await session.execute(
                    text(
                        f"""
                        SELECT r.run_id, r.run_uid, r.cnvs_id, c.cnvs_uid,
                               r.run_stts_cd, r.trace_id, r.str_dt, r.deadline_dt
                          FROM {self._schema}.ai_run_mst r
                          JOIN {self._schema}.ai_cnvs_mst c USING (cnvs_id)
                         WHERE r.cnvs_id = :conversation_pk
                           AND r.request_hash = :request_hash
                           AND (
                               :reuse_terminal
                               OR r.run_stts_cd = ANY(CAST(:active_statuses AS VARCHAR[]))
                           )
                         ORDER BY r.str_dt DESC
                         LIMIT 1
                        """
                    ),
                    {
                        "conversation_pk": conversation["cnvs_id"],
                        "request_hash": request_hash,
                        "active_statuses": list(ACTIVE_RUN_STATUSES),
                        "reuse_terminal": reuse_terminal,
                    },
                )
            ).mappings().one_or_none()
            if active:
                return self._record(active, reused=True)

            counts = (
                await session.execute(
                    text(
                        f"""
                        SELECT COUNT(*) FILTER (WHERE a.agt_typ_cd = 'SUPERVISOR') AS gateway_count,
                               COUNT(*) FILTER (WHERE c.agt_id = :agent_id) AS agent_count,
                               COUNT(*) FILTER (
                                   WHERE c.api_clnt_id = :api_client_id AND c.usr_id = :user_id
                               ) AS user_count,
                               COUNT(*) FILTER (
                                   WHERE c.api_clnt_id = :api_client_id AND c.usr_id = :user_id
                                     AND c.agt_id = :agent_id
                               ) AS user_agent_count,
                               COUNT(*) FILTER (WHERE r.cnvs_id = :conversation_pk) AS conversation_count
                          FROM {self._schema}.ai_run_mst r
                          JOIN {self._schema}.ai_cnvs_mst c USING (cnvs_id)
                          JOIN {self._schema}.ai_agt_mst a USING (agt_id)
                         WHERE r.run_stts_cd = ANY(CAST(:active_statuses AS VARCHAR[]))
                        """
                    ),
                    {
                        "agent_id": route.agt_id,
                        "api_client_id": principal.api_clnt_id,
                        "user_id": user_id,
                        "conversation_pk": conversation["cnvs_id"],
                        "active_statuses": list(ACTIVE_RUN_STATUSES),
                    },
                )
            ).mappings().one()
            if counts["conversation_count"] >= 1:
                raise GatewayError(409, "RUN_ALREADY_ACTIVE", "A Run is already active for this conversation.")
            if counts["gateway_count"] >= gateway_limit:
                raise GatewayError(429, "RATE_LIMITED", "The Gateway active Run limit has been reached.")
            if counts["agent_count"] >= route.max_active_run_count:
                raise GatewayError(429, "RATE_LIMITED", "The Agent active Run limit has been reached.")
            if counts["user_count"] >= user_limit:
                raise GatewayError(429, "RATE_LIMITED", "The user active Run limit has been reached.")
            if counts["user_agent_count"] >= user_agent_limit:
                raise GatewayError(429, "RATE_LIMITED", "The user active Run limit for this Agent has been reached.")

            row = (
                await session.execute(
                    text(
                        f"""
                        INSERT INTO {self._schema}.ai_run_mst (
                            run_uid, cnvs_id, request_hash, run_stts_cd, deadline_dt,
                            trace_id, reg_id
                        ) VALUES (
                            :run_uid, :conversation_pk, :request_hash, 'QUEUED',
                            :deadline_at, :trace_id, :registration_id
                        )
                        RETURNING run_id, run_uid, cnvs_id,
                                  CAST(:conversation_uid AS VARCHAR(64)) AS cnvs_uid,
                                  run_stts_cd, trace_id, str_dt, deadline_dt
                        """
                    ),
                    {
                        "run_uid": f"run_{uuid4().hex}",
                        "conversation_uid": conversation_uid,
                        "conversation_pk": conversation["cnvs_id"],
                        "request_hash": request_hash,
                        "deadline_at": deadline_at,
                        "trace_id": trace_id,
                        "registration_id": principal.api_clnt_cd,
                    },
                )
            ).mappings().one()
            return self._record(row)

    @staticmethod
    def _positive_limit(value: Any, default: int) -> int:
        try:
            parsed = int(value)
            return parsed if parsed > 0 else default
        except (TypeError, ValueError):
            return default

    async def reconcile_interrupted_runs(self) -> int:
        """프로세스 시작 시 남은 비종료 Run을 재개하지 않고 종료 처리한다."""
        async with self._database.session() as session:
            result = await session.execute(
                text(
                    f"""
                    UPDATE {self._schema}.ai_run_mst
                       SET run_stts_cd = CASE
                               WHEN deadline_dt <= CURRENT_TIMESTAMP THEN 'TIMED_OUT'
                               ELSE 'FAILED'
                           END,
                           err_cd = CASE
                               WHEN deadline_dt <= CURRENT_TIMESTAMP THEN 'RUN_DEADLINE_EXCEEDED'
                               ELSE 'ORCHESTRATOR_RESTARTED'
                           END,
                           err_msg_cn = CASE
                               WHEN deadline_dt <= CURRENT_TIMESTAMP THEN 'Run deadline exceeded.'
                               ELSE 'The orchestrator restarted before the Run completed.'
                           END,
                           end_dt = CURRENT_TIMESTAMP,
                           execution_owner_id = NULL,
                           lease_expires_dt = NULL,
                           mod_dt = CURRENT_TIMESTAMP,
                           mod_id = 'STARTUP_RECONCILIATION'
                     WHERE run_stts_cd = ANY(CAST(:active_statuses AS VARCHAR[]))
                       AND (
                           lease_expires_dt <= CURRENT_TIMESTAMP
                           OR (
                               execution_owner_id IS NULL
                               AND str_dt <= CURRENT_TIMESTAMP - INTERVAL '60 seconds'
                           )
                       )
                    """
                ),
                {"active_statuses": list(ACTIVE_RUN_STATUSES)},
            )
            return int(result.rowcount or 0)

    async def transition_run(
        self,
        run_pk: int,
        current_status: str,
        new_status: str,
        modifier_id: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        """허용된 상태 전이만 조건부 UPDATE하여 완료·중단 경쟁을 방지한다."""
        if new_status not in ALLOWED_TRANSITIONS.get(current_status, set()):
            raise ValueError(f"invalid Run transition: {current_status} -> {new_status}")
        terminal = new_status in TERMINAL_RUN_STATUSES
        async with self._database.session() as session:
            result = await session.execute(
                text(
                    f"""
                    UPDATE {self._schema}.ai_run_mst
                       SET run_stts_cd = :new_status,
                           last_evt_dt = CURRENT_TIMESTAMP,
                           end_dt = CASE WHEN :terminal THEN CURRENT_TIMESTAMP ELSE end_dt END,
                           err_cd = :error_code,
                           err_msg_cn = :error_message,
                           execution_owner_id = CASE WHEN :terminal THEN NULL ELSE execution_owner_id END,
                           lease_expires_dt = CASE WHEN :terminal THEN NULL ELSE lease_expires_dt END,
                           mod_dt = CURRENT_TIMESTAMP,
                           mod_id = :modifier_id
                     WHERE run_id = :run_pk
                       AND run_stts_cd = :current_status
                    """
                ),
                {
                    "run_pk": run_pk,
                    "current_status": current_status,
                    "new_status": new_status,
                    "terminal": terminal,
                    "error_code": error_code,
                    "error_message": error_message,
                    "modifier_id": modifier_id,
                },
            )
            return (result.rowcount or 0) == 1

    async def claim_execution(
        self,
        run_pk: int,
        owner_id: str,
        lease_seconds: int,
    ) -> bool:
        """QUEUED Run 실행 소유권을 한 instance에만 부여한다."""
        async with self._database.session() as session:
            result = await session.execute(
                text(
                    f"""
                    UPDATE {self._schema}.ai_run_mst
                       SET execution_owner_id = :owner_id,
                           heartbeat_dt = CURRENT_TIMESTAMP,
                           lease_expires_dt = CURRENT_TIMESTAMP + make_interval(secs => :lease_seconds),
                           mod_dt = CURRENT_TIMESTAMP,
                           mod_id = :owner_id
                     WHERE run_id = :run_pk
                       AND run_stts_cd = 'QUEUED'
                       AND execution_owner_id IS NULL
                    """
                ),
                {
                    "run_pk": run_pk,
                    "owner_id": owner_id,
                    "lease_seconds": lease_seconds,
                },
            )
            return (result.rowcount or 0) == 1

    async def renew_execution_lease(
        self,
        run_pk: int,
        owner_id: str,
        lease_seconds: int,
    ) -> bool:
        async with self._database.session() as session:
            result = await session.execute(
                text(
                    f"""
                    UPDATE {self._schema}.ai_run_mst
                       SET heartbeat_dt = CURRENT_TIMESTAMP,
                           lease_expires_dt = CURRENT_TIMESTAMP + make_interval(secs => :lease_seconds)
                     WHERE run_id = :run_pk
                       AND execution_owner_id = :owner_id
                       AND run_stts_cd = ANY(CAST(:active_statuses AS VARCHAR[]))
                    """
                ),
                {
                    "run_pk": run_pk,
                    "owner_id": owner_id,
                    "lease_seconds": lease_seconds,
                    "active_statuses": list(ACTIVE_RUN_STATUSES),
                },
            )
            return (result.rowcount or 0) == 1

    async def resolve_event_sequence(self, run_pk: int, event_uid: str) -> int:
        async with self._database.session() as session:
            sequence = await session.scalar(
                text(
                    f"""SELECT evt_seq_no FROM {self._schema}.ai_run_evt_hst
                         WHERE run_id=:run_pk AND evt_uid=:event_uid"""
                ),
                {"run_pk": run_pk, "event_uid": event_uid},
            )
        if sequence is None:
            raise GatewayError(400, "INVALID_LAST_EVENT_ID", "Last-Event-ID is invalid for this Run.")
        return int(sequence)

    async def latest_event_sequence(self, run_pk: int) -> int:
        """후속 승인 스트림이 이미 전달한 Event를 재생하지 않도록 현재 끝 순번을 반환한다."""
        async with self._database.session() as session:
            sequence = await session.scalar(
                text(f"SELECT COALESCE(MAX(evt_seq_no),0) FROM {self._schema}.ai_run_evt_hst WHERE run_id=:run_pk"),
                {"run_pk": run_pk},
            )
        return int(sequence or 0)

    async def list_events_after(
        self,
        run_pk: int,
        after_sequence: int,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    text(
                        f"""SELECT evt_uid,evt_seq_no,evt_typ_cd,evt_data,reg_dt
                              FROM {self._schema}.ai_run_evt_hst
                             WHERE run_id=:run_pk AND evt_seq_no>:after_sequence
                             ORDER BY evt_seq_no LIMIT :limit"""
                    ),
                    {"run_pk": run_pk, "after_sequence": after_sequence, "limit": limit},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def get_run_status(self, run_pk: int) -> str | None:
        async with self._database.session() as session:
            status = await session.scalar(
                text(f"SELECT run_stts_cd FROM {self._schema}.ai_run_mst WHERE run_id=:run_pk"),
                {"run_pk": run_pk},
            )
        return str(status) if status is not None else None

    async def request_cancel(
        self,
        principal: GatewayPrincipal,
        route: SupervisorRoute,
        user_id: str,
        run_uid: str,
    ) -> dict[str, object]:
        """Run 소유권을 확인하고 멱등하게 중단 요청 상태를 기록한다."""
        async with self._database.session() as session:
            row = (
                await session.execute(
                    text(
                        f"""
                        SELECT r.run_id, r.run_uid, r.run_stts_cd, r.cancel_req_dt,
                               c.cnvs_uid
                          FROM {self._schema}.ai_run_mst r
                          JOIN {self._schema}.ai_cnvs_mst c USING (cnvs_id)
                         WHERE r.run_uid = :run_uid
                           AND c.api_clnt_id = :api_client_id
                           AND c.agt_id = :agent_id
                           AND c.usr_id = :user_id
                           AND c.cnvs_mode_cd = 'SUPERVISOR'
                         FOR UPDATE OF r
                        """
                    ),
                    {
                        "run_uid": run_uid,
                        "api_client_id": principal.api_clnt_id,
                        "agent_id": route.agt_id,
                        "user_id": user_id,
                    },
                )
            ).mappings().one_or_none()
            if row is None:
                raise GatewayError(404, "RUN_NOT_FOUND", "Run was not found.")
            status = row["run_stts_cd"]
            cancel_requested_at = row["cancel_req_dt"]
            if status == "WAITING_APPROVAL":
                updated = (
                    await session.execute(
                        text(
                            f"""UPDATE {self._schema}.ai_run_mst
                                   SET run_stts_cd='CANCELLED',cancel_req_dt=CURRENT_TIMESTAMP,
                                       end_dt=CURRENT_TIMESTAMP,last_evt_dt=CURRENT_TIMESTAMP,
                                       execution_owner_id=NULL,lease_expires_dt=NULL,
                                       mod_dt=CURRENT_TIMESTAMP,mod_id=:modifier_id
                                 WHERE run_id=:run_pk RETURNING cancel_req_dt"""
                        ),
                        {"run_pk": row["run_id"], "modifier_id": principal.api_clnt_cd},
                    )
                ).mappings().one()
                status = "CANCELLED"
                cancel_requested_at = updated["cancel_req_dt"]
            if status not in TERMINAL_RUN_STATUSES and status != "CANCEL_REQUESTED":
                updated = (
                    await session.execute(
                        text(
                            f"""
                            UPDATE {self._schema}.ai_run_mst
                               SET run_stts_cd = 'CANCEL_REQUESTED',
                                   cancel_req_dt = CURRENT_TIMESTAMP,
                                   last_evt_dt = CURRENT_TIMESTAMP,
                                   mod_dt = CURRENT_TIMESTAMP,
                                   mod_id = :modifier_id
                             WHERE run_id = :run_pk
                            RETURNING cancel_req_dt
                            """
                        ),
                        {"run_pk": row["run_id"], "modifier_id": principal.api_clnt_cd},
                    )
                ).mappings().one()
                status = "CANCEL_REQUESTED"
                cancel_requested_at = updated["cancel_req_dt"]
            return {
                "run_id": row["run_uid"],
                "conversation_id": row["cnvs_uid"],
                "status": status,
                "cancel_requested_at": cancel_requested_at,
            }

    async def append_event(
        self,
        run_pk: int,
        event_type: str,
        event_data: dict[str, Any],
        registration_id: str,
        upstream_event_id: str | None = None,
    ) -> tuple[str, int]:
        """Run별 순번을 잠금으로 보장하여 제어 Event를 저장한다."""
        event_uid = f"evt_{uuid4().hex}"
        async with self._database.session() as session:
            await session.execute(
                text(f"SELECT run_id FROM {self._schema}.ai_run_mst WHERE run_id=:run_pk FOR UPDATE"),
                {"run_pk": run_pk},
            )
            sequence = int(
                await session.scalar(
                    text(f"SELECT COALESCE(MAX(evt_seq_no),0)+1 FROM {self._schema}.ai_run_evt_hst WHERE run_id=:run_pk"),
                    {"run_pk": run_pk},
                )
            )
            await session.execute(
                text(
                    f"""INSERT INTO {self._schema}.ai_run_evt_hst(
                        run_id,evt_uid,evt_seq_no,evt_typ_cd,dbx_evt_id,evt_data,reg_id
                    ) VALUES(:run_pk,:event_uid,:sequence,:event_type,:upstream_event_id,
                        CAST(:event_data AS JSONB),:registration_id)"""
                ),
                {
                    "run_pk": run_pk,
                    "event_uid": event_uid,
                    "sequence": sequence,
                    "event_type": event_type,
                    "upstream_event_id": upstream_event_id,
                    "event_data": json.dumps(event_data, ensure_ascii=False),
                    "registration_id": registration_id,
                },
            )
        return event_uid, sequence

    async def list_owned_conversations(
        self,
        principal: GatewayPrincipal,
        user_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """사용자 소유 Supervisor 대화를 최근 활동 순서로 조회한다."""
        sql = text(
            f"""SELECT c.cnvs_uid, c.cnvs_title_nm, c.cnvs_stts_cd,
                       c.str_dt, COALESCE(c.last_msg_dt, c.str_dt) AS last_msg_dt,
                       a.agt_cd, a.agt_nm,
                       (SELECT r.run_stts_cd
                          FROM {self._schema}.ai_run_mst r
                         WHERE r.cnvs_id = c.cnvs_id
                         ORDER BY r.str_dt DESC LIMIT 1) AS last_run_status,
                       COALESCE(
                           c.cnvs_title_nm,
                           (SELECT i.item_data->>'content'
                              FROM {self._schema}.ai_run_item_hst i
                              JOIN {self._schema}.ai_run_mst r USING(run_id)
                             WHERE r.cnvs_id = c.cnvs_id
                               AND i.item_typ_cd = 'user.message'
                             ORDER BY i.run_item_id LIMIT 1),
                           '새 대화'
                       ) AS display_title
                  FROM {self._schema}.ai_cnvs_mst c
                  JOIN {self._schema}.ai_agt_mst a USING(agt_id)
                 WHERE c.api_clnt_id = :api_client_id
                   AND c.usr_id = :user_id
                   AND c.cnvs_mode_cd = 'SUPERVISOR'
                   AND c.use_yn = 'Y'
                   AND c.cnvs_stts_cd <> 'DELETED'
                 ORDER BY COALESCE(c.last_msg_dt, c.str_dt) DESC
                 LIMIT :limit"""
        )
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    sql,
                    {
                        "api_client_id": principal.api_clnt_id,
                        "user_id": user_id,
                        "limit": limit,
                    },
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def save_item(
        self,
        run_pk: int,
        item_type: str,
        data: dict[str, Any],
        registration_id: str,
        upstream_item_id: str | None = None,
    ) -> None:
        """완료된 Message/Tool Item을 저장한다."""
        async with self._database.session() as session:
            await session.execute(
                text(f"SELECT run_id FROM {self._schema}.ai_run_mst WHERE run_id=:run_pk FOR UPDATE"),
                {"run_pk": run_pk},
            )
            sequence = int(
                await session.scalar(
                    text(f"SELECT COALESCE(MAX(item_seq_no),0)+1 FROM {self._schema}.ai_run_item_hst WHERE run_id=:run_pk"),
                    {"run_pk": run_pk},
                )
            )
            encoded = json.dumps(data, ensure_ascii=False)
            await session.execute(
                text(
                    f"""INSERT INTO {self._schema}.ai_run_item_hst(
                        run_id,item_seq_no,dbx_item_id,item_typ_cd,item_stts_cd,
                        item_data,item_size_no,reg_id
                    ) VALUES(:run_pk,:sequence,:upstream_item_id,:item_type,'COMPLETED',
                        CAST(:item_data AS JSONB),:item_size,:registration_id)
                    ON CONFLICT (run_id,dbx_item_id) WHERE dbx_item_id IS NOT NULL DO NOTHING"""
                ),
                {
                    "run_pk": run_pk,
                    "sequence": sequence,
                    "upstream_item_id": upstream_item_id,
                    "item_type": item_type,
                    "item_data": encoded,
                    "item_size": len(encoded.encode("utf-8")),
                    "registration_id": registration_id,
                },
            )
            title = data.get("content") if item_type == "user.message" else None
            await session.execute(
                text(
                    f"""UPDATE {self._schema}.ai_cnvs_mst c
                           SET last_msg_dt = CURRENT_TIMESTAMP,
                               cnvs_title_nm = COALESCE(
                                   c.cnvs_title_nm,
                                   LEFT(CAST(:title AS TEXT), 1000)
                               ),
                               mod_dt = CURRENT_TIMESTAMP,
                               mod_id = :registration_id
                          FROM {self._schema}.ai_run_mst r
                         WHERE r.run_id = :run_pk
                           AND c.cnvs_id = r.cnvs_id"""
                ),
                {
                    "run_pk": run_pk,
                    "title": title if isinstance(title, str) else None,
                    "registration_id": registration_id,
                },
            )

    async def load_conversation_history(
        self,
        conversation_pk: int,
        exclude_run_pk: int,
        max_turns: int,
        max_characters: int,
    ) -> list[dict[str, str]]:
        """완료 Message Item을 최신 제한 안에서 upstream 대화 문맥으로 복원한다."""
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT i.item_typ_cd, i.item_data
                          FROM {self._schema}.ai_run_item_hst i
                          JOIN {self._schema}.ai_run_mst r USING (run_id)
                         WHERE r.cnvs_id = :conversation_pk
                           AND r.run_id <> :exclude_run_pk
                           AND r.run_stts_cd = 'COMPLETED'
                           AND i.item_typ_cd IN ('user.message', 'message.completed')
                         ORDER BY i.run_item_id DESC
                         LIMIT :item_limit
                        """
                    ),
                    {
                        "conversation_pk": conversation_pk,
                        "exclude_run_pk": exclude_run_pk,
                        "item_limit": max_turns * 2,
                    },
                )
            ).mappings().all()
        selected: list[dict[str, str]] = []
        size = 0
        for row in rows:
            data = row["item_data"] or {}
            if row["item_typ_cd"] == "user.message":
                role, content = "user", data.get("content")
            else:
                role, content = "assistant", self._assistant_text(data)
            if not isinstance(content, str) or not content.strip():
                continue
            if size + len(content) > max_characters:
                break
            selected.append({"role": role, "content": content})
            size += len(content)
        selected.reverse()
        return selected

    @staticmethod
    def _assistant_text(data: dict[str, Any]) -> str | None:
        item = data.get("item") if isinstance(data.get("item"), dict) else {}
        content = item.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return None
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts) or None

    async def is_cancel_requested(self, run_pk: int) -> bool:
        async with self._database.session() as session:
            status = await session.scalar(
                text(f"SELECT run_stts_cd FROM {self._schema}.ai_run_mst WHERE run_id=:run_pk"),
                {"run_pk": run_pk},
            )
        return status == "CANCEL_REQUESTED"

    async def begin_continuation(
        self,
        run_pk: int,
        continue_request_id: str,
        max_continue_count: int,
        modifier_id: str,
    ) -> int:
        """Continuation ID·횟수를 잠근 상태에서 검증하고 Run을 전환한다."""
        async with self._database.session() as session:
            row = (
                await session.execute(
                    text(
                        f"""
                        SELECT run_stts_cd, continue_request_id, continue_cnt
                          FROM {self._schema}.ai_run_mst
                         WHERE run_id = :run_pk
                         FOR UPDATE
                        """
                    ),
                    {"run_pk": run_pk},
                )
            ).mappings().one_or_none()
            if row is None:
                raise GatewayError(404, "RUN_NOT_FOUND", "Run was not found.")
            if row["run_stts_cd"] == "CANCEL_REQUESTED":
                raise GatewayError(409, "RUN_CANCEL_REQUESTED", "Run cancellation was requested.")
            if row["run_stts_cd"] != "STREAMING":
                raise GatewayError(409, "INVALID_RUN_STATE", "Run cannot continue from its current state.")
            if row["continue_request_id"] == continue_request_id:
                raise GatewayError(
                    502,
                    "UPSTREAM_PROTOCOL_ERROR",
                    "Duplicate continuation request was received.",
                )
            next_count = int(row["continue_cnt"]) + 1
            if next_count > max_continue_count:
                raise GatewayError(
                    502,
                    "CONTINUATION_LIMIT_EXCEEDED",
                    "Supervisor continuation limit was exceeded.",
                )
            await session.execute(
                text(
                    f"""
                    UPDATE {self._schema}.ai_run_mst
                       SET run_stts_cd = 'CONTINUING',
                           continue_request_id = :continue_request_id,
                           continue_cnt = :continue_count,
                           last_evt_dt = CURRENT_TIMESTAMP,
                           mod_dt = CURRENT_TIMESTAMP,
                           mod_id = :modifier_id
                     WHERE run_id = :run_pk
                    """
                ),
                {
                    "run_pk": run_pk,
                    "continue_request_id": continue_request_id,
                    "continue_count": next_count,
                    "modifier_id": modifier_id,
                },
            )
            return next_count

    async def create_pending_approval(
        self,
        run_pk: int,
        approval_request: dict[str, Any],
        context_items: list[dict[str, Any]],
        modifier_id: str,
    ) -> str:
        """승인 요청과 재개에 필요한 upstream 문맥을 저장하고 Run을 보류한다."""
        approval_uid = f"apr_{uuid4().hex}"
        upstream_id = approval_request.get("id")
        if not isinstance(upstream_id, str) or not upstream_id:
            raise GatewayError(502, "UPSTREAM_PROTOCOL_ERROR", "Approval request ID is missing.")
        async with self._database.session() as session:
            await session.execute(
                text(f"SELECT run_id FROM {self._schema}.ai_run_mst WHERE run_id=:run_pk FOR UPDATE"),
                {"run_pk": run_pk},
            )
            await session.execute(
                text(
                    f"""INSERT INTO {self._schema}.ai_run_aprv_hst(
                        aprv_uid,run_id,dbx_aprv_req_id,tool_nm,server_label,
                        args_data,req_item_data,context_data,aprv_stts_cd,reg_id
                    ) VALUES(
                        :approval_uid,:run_pk,:upstream_id,:tool_name,:server_label,
                        CAST(:arguments AS JSONB),CAST(:request_item AS JSONB),
                        CAST(:context_items AS JSONB),'PENDING',:modifier_id
                    )
                    ON CONFLICT (run_id,dbx_aprv_req_id) DO NOTHING"""
                ),
                {
                    "approval_uid": approval_uid,
                    "run_pk": run_pk,
                    "upstream_id": upstream_id,
                    "tool_name": approval_request.get("name") or approval_request.get("tool_name"),
                    "server_label": approval_request.get("server_label"),
                    "arguments": json.dumps(self._json_value(approval_request.get("arguments")), ensure_ascii=False),
                    "request_item": json.dumps(approval_request, ensure_ascii=False),
                    "context_items": json.dumps(context_items, ensure_ascii=False),
                    "modifier_id": modifier_id,
                },
            )
            result = await session.execute(
                text(
                    f"""UPDATE {self._schema}.ai_run_mst
                           SET run_stts_cd='WAITING_APPROVAL', execution_owner_id=NULL,
                               lease_expires_dt=NULL, last_evt_dt=CURRENT_TIMESTAMP,
                               mod_dt=CURRENT_TIMESTAMP, mod_id=:modifier_id
                         WHERE run_id=:run_pk AND run_stts_cd='STREAMING'"""
                ),
                {"run_pk": run_pk, "modifier_id": modifier_id},
            )
            if (result.rowcount or 0) != 1:
                raise GatewayError(409, "INVALID_RUN_STATE", "Run cannot wait for approval from its current state.")
            stored_uid = await session.scalar(
                text(f"SELECT aprv_uid FROM {self._schema}.ai_run_aprv_hst WHERE run_id=:run_pk AND dbx_aprv_req_id=:upstream_id"),
                {"run_pk": run_pk, "upstream_id": upstream_id},
            )
        return str(stored_uid or approval_uid)

    async def decide_approval(
        self,
        principal: GatewayPrincipal,
        route: SupervisorRoute,
        user_id: str,
        run_uid: str,
        approval_uid: str,
        approve: bool,
        modifier_id: str,
    ) -> tuple[RunRecord, dict[str, Any], list[dict[str, Any]]]:
        """승인 결정을 멱등 저장하고 동일 Run을 재실행 가능 상태로 전환한다."""
        async with self._database.session() as session:
            row = (
                await session.execute(
                    text(
                        f"""SELECT r.run_id,r.run_uid,r.cnvs_id,c.cnvs_uid,r.run_stts_cd,
                                   r.trace_id,r.str_dt,r.deadline_dt,a.aprv_id,a.aprv_stts_cd,
                                   a.req_item_data,a.context_data
                              FROM {self._schema}.ai_run_mst r
                              JOIN {self._schema}.ai_cnvs_mst c USING(cnvs_id)
                              JOIN {self._schema}.ai_run_aprv_hst a USING(run_id)
                             WHERE r.run_uid=:run_uid AND a.aprv_uid=:approval_uid
                               AND c.api_clnt_id=:api_client_id AND c.agt_id=:agent_id
                               AND c.usr_id=:user_id AND c.cnvs_mode_cd='SUPERVISOR'
                             FOR UPDATE OF r,a"""
                    ),
                    {"run_uid": run_uid, "approval_uid": approval_uid,
                     "api_client_id": principal.api_clnt_id, "agent_id": route.agt_id,
                     "user_id": user_id},
                )
            ).mappings().one_or_none()
            if row is None:
                raise GatewayError(404, "APPROVAL_NOT_FOUND", "Approval request was not found.")
            decision = "APPROVED" if approve else "REJECTED"
            if row["aprv_stts_cd"] != "PENDING":
                if row["aprv_stts_cd"] == decision:
                    raise GatewayError(409, "APPROVAL_ALREADY_DECIDED", "Approval was already decided.")
                raise GatewayError(409, "APPROVAL_DECISION_CONFLICT", "Approval has a different decision.")
            if row["run_stts_cd"] != "WAITING_APPROVAL":
                raise GatewayError(409, "INVALID_RUN_STATE", "Run is not waiting for approval.")
            await session.execute(
                text(f"""UPDATE {self._schema}.ai_run_aprv_hst
                              SET aprv_stts_cd=:decision,decision_dt=CURRENT_TIMESTAMP,
                                  decision_id=:modifier_id
                            WHERE aprv_id=:approval_pk"""),
                {"decision": decision, "modifier_id": modifier_id, "approval_pk": row["aprv_id"]},
            )
            await session.execute(
                text(f"""UPDATE {self._schema}.ai_run_mst
                              SET run_stts_cd='QUEUED',execution_owner_id=NULL,lease_expires_dt=NULL,
                                  last_evt_dt=CURRENT_TIMESTAMP,mod_dt=CURRENT_TIMESTAMP,mod_id=:modifier_id
                            WHERE run_id=:run_pk"""),
                {"run_pk": row["run_id"], "modifier_id": modifier_id},
            )
        return self._record({**dict(row), "run_stts_cd": "QUEUED"}), dict(row["req_item_data"]), list(row["context_data"] or [])

    @staticmethod
    def _json_value(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}

    async def get_owned_run(
        self,
        principal: GatewayPrincipal,
        route: SupervisorRoute,
        user_id: str,
        run_uid: str,
    ) -> dict[str, Any]:
        sql = text(
            f"""SELECT r.run_id,r.run_uid,r.run_stts_cd,r.str_dt,r.last_evt_dt,r.end_dt,
                       r.err_cd,r.err_msg_cn,r.trace_id,c.cnvs_uid
                  FROM {self._schema}.ai_run_mst r
                  JOIN {self._schema}.ai_cnvs_mst c USING(cnvs_id)
                 WHERE r.run_uid=:run_uid AND c.api_clnt_id=:api_client_id
                   AND c.agt_id=:agent_id AND c.usr_id=:user_id
                   AND c.cnvs_mode_cd='SUPERVISOR'"""
        )
        async with self._database.session() as session:
            row=(await session.execute(sql,{"run_uid":run_uid,"api_client_id":principal.api_clnt_id,"agent_id":route.agt_id,"user_id":user_id})).mappings().one_or_none()
        if row is None:
            raise GatewayError(404,"RUN_NOT_FOUND","Run was not found.")
        return dict(row)

    async def list_owned_items(
        self,
        principal: GatewayPrincipal,
        route: SupervisorRoute,
        user_id: str,
        conversation_uid: str,
        after_item_id: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        sql=text(f"""SELECT i.run_item_id,i.item_seq_no,i.item_typ_cd,i.item_stts_cd,
                            i.title_nm,i.text_cn,i.item_data,i.trnc_yn
                       FROM {self._schema}.ai_run_item_hst i
                       JOIN {self._schema}.ai_run_mst r USING(run_id)
                       JOIN {self._schema}.ai_cnvs_mst c USING(cnvs_id)
                      WHERE c.cnvs_uid=:conversation_uid AND c.api_clnt_id=:api_client_id
                        AND c.agt_id=:agent_id AND c.usr_id=:user_id
                        AND i.run_item_id>:after_item_id
                      ORDER BY i.run_item_id LIMIT :limit""")
        async with self._database.session() as session:
            rows=(await session.execute(sql,{"conversation_uid":conversation_uid,"api_client_id":principal.api_clnt_id,"agent_id":route.agt_id,"user_id":user_id,"after_item_id":after_item_id,"limit":limit})).mappings().all()
        return [dict(row) for row in rows]

    @staticmethod
    def _record(row, reused: bool = False) -> RunRecord:
        return RunRecord(
            run_pk=row["run_id"],
            run_uid=row["run_uid"],
            conversation_pk=row["cnvs_id"],
            conversation_uid=row["cnvs_uid"],
            status=row["run_stts_cd"],
            trace_id=row["trace_id"],
            started_at=row["str_dt"],
            deadline_at=row["deadline_dt"],
            reused=reused,
        )

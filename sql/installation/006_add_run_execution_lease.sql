-- Supervisor Run의 복수 인스턴스 실행 소유권과 만료 판정을 추가한다.
BEGIN;

SET search_path TO genie_gw, public;

DO $$
BEGIN
    IF to_regclass('genie_gw.ai_run_mst') IS NULL THEN
        RAISE EXCEPTION
            'ai_run_mst가 없습니다. 빈 DB/신규 설치에는 001_create_genie_gw_schema.sql을 실행하고, 기존 DB 업그레이드에는 005_add_supervisor_agent_foundation.sql을 먼저 실행하십시오.';
    END IF;
END
$$;

ALTER TABLE ai_run_mst
    ADD COLUMN IF NOT EXISTS execution_owner_id VARCHAR(200),
    ADD COLUMN IF NOT EXISTS lease_expires_dt TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS heartbeat_dt TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_ai_run_mst_03
    ON ai_run_mst (run_stts_cd, lease_expires_dt)
    WHERE run_stts_cd IN ('QUEUED', 'STREAMING', 'CONTINUING', 'CANCEL_REQUESTED');

COMMENT ON COLUMN ai_run_mst.execution_owner_id IS 'Supervisor Run 실행을 소유한 Gateway instance 식별자';
COMMENT ON COLUMN ai_run_mst.lease_expires_dt IS '실행 소유권 만료 시각';
COMMENT ON COLUMN ai_run_mst.heartbeat_dt IS '실행 소유 instance의 마지막 heartbeat 시각';

COMMIT;

-- 기존 Supervisor Gateway DB에 Web Search/MCP 사용자 승인 상태와 이력을 추가한다.
BEGIN;
SET search_path TO genie_gw, public;

ALTER TABLE ai_run_mst DROP CONSTRAINT IF EXISTS ck_ai_run_mst_01;
ALTER TABLE ai_run_mst ADD CONSTRAINT ck_ai_run_mst_01 CHECK (
    run_stts_cd IN ('QUEUED','STREAMING','CONTINUING','WAITING_APPROVAL',
                    'CANCEL_REQUESTED','COMPLETED','CANCELLED','TIMED_OUT','FAILED')
);

DROP INDEX IF EXISTS ix_ai_run_mst_03;
CREATE INDEX ix_ai_run_mst_03 ON ai_run_mst (run_stts_cd, lease_expires_dt)
    WHERE run_stts_cd IN ('QUEUED','STREAMING','CONTINUING','WAITING_APPROVAL','CANCEL_REQUESTED');
DROP INDEX IF EXISTS ux_ai_run_mst_02;
CREATE UNIQUE INDEX ux_ai_run_mst_02 ON ai_run_mst (cnvs_id, request_hash)
    WHERE run_stts_cd IN ('QUEUED','STREAMING','CONTINUING','WAITING_APPROVAL','CANCEL_REQUESTED');

CREATE TABLE IF NOT EXISTS ai_run_aprv_hst (
    aprv_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    aprv_uid VARCHAR(64) NOT NULL UNIQUE,
    run_id BIGINT NOT NULL REFERENCES ai_run_mst (run_id),
    dbx_aprv_req_id VARCHAR(200) NOT NULL,
    tool_nm VARCHAR(200), server_label VARCHAR(200), args_data JSONB,
    req_item_data JSONB NOT NULL, context_data JSONB NOT NULL,
    aprv_stts_cd VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    decision_dt TIMESTAMPTZ, decision_id VARCHAR(100),
    reg_dt TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reg_id VARCHAR(100) NOT NULL,
    CONSTRAINT uk_ai_run_aprv_hst_01 UNIQUE (run_id, dbx_aprv_req_id),
    CONSTRAINT ck_ai_run_aprv_hst_01 CHECK (aprv_stts_cd IN ('PENDING','APPROVED','REJECTED'))
);
COMMENT ON TABLE ai_run_aprv_hst IS 'Supervisor MCP·Web Search 도구의 사용자 승인/거절과 동일 Run 재개 문맥을 보존하는 감사 이력';
COMMENT ON COLUMN ai_run_aprv_hst.aprv_uid IS '외부 API와 UI에 노출하는 Gateway 승인 요청 식별자';
COMMENT ON COLUMN ai_run_aprv_hst.req_item_data IS '후속 요청에 되돌려 보낼 원본 mcp_approval_request Item';
COMMENT ON COLUMN ai_run_aprv_hst.context_data IS '승인 후 같은 Run을 재개하기 위한 승인 직전 upstream 대화 문맥';
CREATE INDEX IF NOT EXISTS ix_ai_run_aprv_hst_01
    ON ai_run_aprv_hst (run_id, aprv_stts_cd, reg_dt DESC);

COMMIT;

-- 기존 Genie Gateway 설치 환경을 공통 AI Agent/Run 구조로 확장한다.
-- 기존 PK, FK 값과 Genie Chat/Agent API 데이터는 보존한다.

BEGIN;

SET search_path TO genie_gw, public;

DO $$
DECLARE
    missing_objects TEXT;
BEGIN
    WITH required_object(object_nm, current_nm, legacy_nm) AS (
        VALUES
            ('Agent 기본 테이블', 'genie_gw.ai_agt_mst', 'genie_gw.genie_agt_mst'),
            ('시스템-Agent 매핑 테이블', 'genie_gw.sys_ai_agt_map', 'genie_gw.sys_genie_agt_map'),
            ('대화 기본 테이블', 'genie_gw.ai_cnvs_mst', 'genie_gw.genie_cnvs_mst')
    )
    SELECT STRING_AGG(object_nm, ', ' ORDER BY object_nm)
      INTO missing_objects
      FROM required_object
     WHERE to_regclass(current_nm) IS NULL
       AND to_regclass(legacy_nm) IS NULL;

    IF missing_objects IS NOT NULL THEN
        RAISE EXCEPTION
            'Supervisor 업그레이드 선행 테이블 누락: %. 빈 DB/신규 설치에는 005가 아니라 001_create_genie_gw_schema.sql을 실행하십시오.',
            missing_objects;
    END IF;

    IF to_regclass('genie_gw.api_req_hst') IS NULL THEN
        RAISE EXCEPTION
            'Supervisor 업그레이드 선행 테이블 누락: api_req_hst. 빈 DB/신규 설치에는 001_create_genie_gw_schema.sql을 실행하십시오.';
    END IF;
END
$$;

DO $$
BEGIN
    IF to_regclass('genie_gw.ai_agt_mst') IS NULL
       AND to_regclass('genie_gw.genie_agt_mst') IS NOT NULL THEN
        ALTER TABLE genie_agt_mst RENAME TO ai_agt_mst;
    END IF;

    IF to_regclass('genie_gw.sys_ai_agt_map') IS NULL
       AND to_regclass('genie_gw.sys_genie_agt_map') IS NOT NULL THEN
        ALTER TABLE sys_genie_agt_map RENAME TO sys_ai_agt_map;
    END IF;

    IF to_regclass('genie_gw.ai_cnvs_mst') IS NULL
       AND to_regclass('genie_gw.genie_cnvs_mst') IS NOT NULL THEN
        ALTER TABLE genie_cnvs_mst RENAME TO ai_cnvs_mst;
    END IF;
END
$$;

ALTER TABLE ai_agt_mst
    ADD COLUMN IF NOT EXISTS agt_typ_cd VARCHAR(20) NOT NULL DEFAULT 'GENIE',
    ADD COLUMN IF NOT EXISTS dbx_serving_ep_nm VARCHAR(200),
    ADD COLUMN IF NOT EXISTS long_task_use_yn CHAR(1) NOT NULL DEFAULT 'N',
    ADD COLUMN IF NOT EXISTS first_event_timeout_sec INTEGER NOT NULL DEFAULT 90,
    ADD COLUMN IF NOT EXISTS idle_timeout_sec INTEGER NOT NULL DEFAULT 180,
    ADD COLUMN IF NOT EXISTS total_timeout_sec INTEGER NOT NULL DEFAULT 1800,
    ADD COLUMN IF NOT EXISTS max_continue_cnt INTEGER NOT NULL DEFAULT 20,
    ADD COLUMN IF NOT EXISTS max_active_run_cnt INTEGER NOT NULL DEFAULT 15,
    ADD COLUMN IF NOT EXISTS max_queue_run_cnt INTEGER NOT NULL DEFAULT 30,
    ADD COLUMN IF NOT EXISTS history_max_turn_cnt INTEGER NOT NULL DEFAULT 20,
    ADD COLUMN IF NOT EXISTS history_max_char_cnt INTEGER NOT NULL DEFAULT 40000;

UPDATE ai_agt_mst
   SET agt_typ_cd = 'GENIE'
 WHERE agt_typ_cd IS NULL;

ALTER TABLE ai_agt_mst DROP CONSTRAINT IF EXISTS ck_genie_agt_mst_01;
ALTER TABLE ai_agt_mst DROP CONSTRAINT IF EXISTS ck_genie_agt_mst_02;
ALTER TABLE ai_agt_mst DROP CONSTRAINT IF EXISTS ck_genie_agt_mst_03;
ALTER TABLE ai_agt_mst DROP CONSTRAINT IF EXISTS ck_ai_agt_mst_01;
ALTER TABLE ai_agt_mst DROP CONSTRAINT IF EXISTS ck_ai_agt_mst_02;
ALTER TABLE ai_agt_mst DROP CONSTRAINT IF EXISTS ck_ai_agt_mst_03;
ALTER TABLE ai_agt_mst DROP CONSTRAINT IF EXISTS ck_ai_agt_mst_04;
ALTER TABLE ai_agt_mst DROP CONSTRAINT IF EXISTS ck_ai_agt_mst_05;

ALTER TABLE ai_agt_mst
    ADD CONSTRAINT ck_ai_agt_mst_01 CHECK (
        (agt_typ_cd = 'GENIE' AND agt_mode_cd IN ('CHAT', 'AGENT', 'BOTH'))
        OR (agt_typ_cd = 'SUPERVISOR' AND agt_mode_cd = 'SUPERVISOR')
    ),
    ADD CONSTRAINT ck_ai_agt_mst_02 CHECK (use_yn IN ('Y', 'N')),
    ADD CONSTRAINT ck_ai_agt_mst_03 CHECK (
        use_yn = 'N'
        OR (agt_typ_cd = 'GENIE' AND agt_mode_cd = 'CHAT' AND dbx_space_id IS NOT NULL)
        OR (agt_typ_cd = 'GENIE' AND agt_mode_cd = 'AGENT' AND dbx_agent_id IS NOT NULL)
        OR (agt_typ_cd = 'GENIE' AND agt_mode_cd = 'BOTH' AND dbx_space_id IS NOT NULL AND dbx_agent_id IS NOT NULL)
        OR (agt_typ_cd = 'SUPERVISOR' AND dbx_serving_ep_nm IS NOT NULL)
    ),
    ADD CONSTRAINT ck_ai_agt_mst_04 CHECK (long_task_use_yn IN ('Y', 'N')),
    ADD CONSTRAINT ck_ai_agt_mst_05 CHECK (
        first_event_timeout_sec > 0 AND idle_timeout_sec > 0 AND total_timeout_sec > 0
        AND max_continue_cnt >= 0 AND max_active_run_cnt > 0 AND max_queue_run_cnt >= 0
        AND history_max_turn_cnt > 0 AND history_max_char_cnt > 0
    );

CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_agt_mst_04
    ON ai_agt_mst (ws_id, dbx_serving_ep_nm)
    WHERE dbx_serving_ep_nm IS NOT NULL;

COMMENT ON TABLE ai_agt_mst IS 'AI Agent 공통 기본';
COMMENT ON COLUMN ai_agt_mst.agt_typ_cd IS 'Agent 유형 코드: GENIE 또는 SUPERVISOR';
COMMENT ON COLUMN ai_agt_mst.dbx_serving_ep_nm IS 'Supervisor 실제 Databricks serving endpoint 이름';
COMMENT ON TABLE sys_ai_agt_map IS '시스템-AI Agent 매핑';

ALTER TABLE ai_cnvs_mst
    ADD COLUMN IF NOT EXISTS cnvs_uid VARCHAR(64);

UPDATE ai_cnvs_mst
   SET cnvs_uid = 'cnvs_legacy_' || cnvs_id::TEXT
 WHERE cnvs_uid IS NULL;

ALTER TABLE ai_cnvs_mst
    ALTER COLUMN cnvs_uid SET NOT NULL;

ALTER TABLE ai_cnvs_mst DROP CONSTRAINT IF EXISTS ck_genie_cnvs_mst_01;
ALTER TABLE ai_cnvs_mst DROP CONSTRAINT IF EXISTS ck_ai_cnvs_mst_01;
ALTER TABLE ai_cnvs_mst
    ADD CONSTRAINT ck_ai_cnvs_mst_01 CHECK (cnvs_mode_cd IN ('CHAT', 'AGENT', 'SUPERVISOR'));

CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_cnvs_mst_uid
    ON ai_cnvs_mst (cnvs_uid);

COMMENT ON TABLE ai_cnvs_mst IS 'AI Agent 공통 대화 기본';
COMMENT ON COLUMN ai_cnvs_mst.cnvs_uid IS 'Gateway 외부 공개 Conversation 식별자';

CREATE TABLE IF NOT EXISTS ai_run_mst (
    run_id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_uid              VARCHAR(64)  NOT NULL UNIQUE,
    cnvs_id              BIGINT       NOT NULL REFERENCES ai_cnvs_mst (cnvs_id),
    request_hash         VARCHAR(64)  NOT NULL,
    run_stts_cd          VARCHAR(30)  NOT NULL DEFAULT 'QUEUED',
    dbx_response_id      VARCHAR(200),
    continue_request_id  VARCHAR(200),
    continue_cnt         INTEGER      NOT NULL DEFAULT 0 CHECK (continue_cnt >= 0),
    str_dt               TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_evt_dt          TIMESTAMPTZ,
    deadline_dt          TIMESTAMPTZ  NOT NULL,
    cancel_req_dt        TIMESTAMPTZ,
    end_dt               TIMESTAMPTZ,
    err_cd               VARCHAR(100),
    err_msg_cn           TEXT,
    trace_id             VARCHAR(64)  NOT NULL,
    reg_dt               TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reg_id               VARCHAR(100) NOT NULL,
    mod_dt               TIMESTAMPTZ,
    mod_id               VARCHAR(100),
    CONSTRAINT ck_ai_run_mst_01 CHECK (
        run_stts_cd IN (
            'QUEUED', 'STREAMING', 'CONTINUING', 'CANCEL_REQUESTED',
            'COMPLETED', 'CANCELLED', 'TIMED_OUT', 'FAILED'
        )
    ),
    CONSTRAINT ck_ai_run_mst_03 CHECK (deadline_dt > str_dt),
    CONSTRAINT ck_ai_run_mst_04 CHECK (end_dt IS NULL OR end_dt >= str_dt)
);

CREATE INDEX IF NOT EXISTS ix_ai_run_mst_01 ON ai_run_mst (cnvs_id, str_dt DESC);
CREATE INDEX IF NOT EXISTS ix_ai_run_mst_02 ON ai_run_mst (run_stts_cd, deadline_dt);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_run_mst_02
    ON ai_run_mst (cnvs_id, request_hash)
    WHERE run_stts_cd IN ('QUEUED', 'STREAMING', 'CONTINUING', 'CANCEL_REQUESTED');

CREATE TABLE IF NOT EXISTS ai_run_item_hst (
    run_item_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id               BIGINT       NOT NULL REFERENCES ai_run_mst (run_id),
    item_seq_no          INTEGER      NOT NULL CHECK (item_seq_no > 0),
    dbx_item_id          VARCHAR(200),
    item_typ_cd          VARCHAR(50)  NOT NULL,
    item_stts_cd         VARCHAR(30)  NOT NULL DEFAULT 'COMPLETED',
    role_cd              VARCHAR(20) CHECK (role_cd IS NULL OR role_cd IN ('USER', 'ASSISTANT', 'SYSTEM', 'TOOL')),
    title_nm             VARCHAR(1000),
    text_cn              TEXT,
    item_data            JSONB,
    item_size_no         BIGINT       NOT NULL DEFAULT 0 CHECK (item_size_no >= 0),
    trnc_yn              CHAR(1)      NOT NULL DEFAULT 'N' CHECK (trnc_yn IN ('Y', 'N')),
    reg_dt               TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reg_id               VARCHAR(100) NOT NULL,
    CONSTRAINT uk_ai_run_item_hst_01 UNIQUE (run_id, item_seq_no)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_run_item_hst_02
    ON ai_run_item_hst (run_id, dbx_item_id) WHERE dbx_item_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_ai_run_item_hst_01
    ON ai_run_item_hst (run_id, item_seq_no);

CREATE TABLE IF NOT EXISTS ai_run_evt_hst (
    run_evt_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id               BIGINT       NOT NULL REFERENCES ai_run_mst (run_id),
    evt_uid              VARCHAR(64)  NOT NULL UNIQUE,
    evt_seq_no           INTEGER      NOT NULL CHECK (evt_seq_no > 0),
    evt_typ_cd           VARCHAR(100) NOT NULL,
    dbx_evt_id           VARCHAR(200),
    evt_data             JSONB,
    reg_dt               TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reg_id               VARCHAR(100) NOT NULL,
    CONSTRAINT uk_ai_run_evt_hst_02 UNIQUE (run_id, evt_seq_no)
);

CREATE INDEX IF NOT EXISTS ix_ai_run_evt_hst_01
    ON ai_run_evt_hst (run_id, reg_dt, evt_seq_no);

ALTER TABLE api_req_hst
    ADD COLUMN IF NOT EXISTS run_id BIGINT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
         FROM pg_constraint
         WHERE conrelid = 'genie_gw.api_req_hst'::regclass
           AND conname = 'fk_api_req_hst_04'
    ) THEN
        ALTER TABLE api_req_hst
            ADD CONSTRAINT fk_api_req_hst_04 FOREIGN KEY (run_id)
                REFERENCES ai_run_mst (run_id);
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS ix_api_req_hst_04
    ON api_req_hst (run_id, req_dt DESC)
    WHERE run_id IS NOT NULL;

COMMIT;

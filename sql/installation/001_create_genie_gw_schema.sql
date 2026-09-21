-- Genie API Gateway 신규 설치용 최종 통합 DDL
-- 지원 대상: PostgreSQL, Amazon RDS for PostgreSQL, Databricks Lakebase
-- 특정 시스템·클라이언트·Agent 기준정보는 포함하지 않는다.
-- 신규 환경에서는 이 파일만 실행하여 최신 테이블 구조를 생성한 뒤,
-- 002_grant_runtime_role.sql과 003_verify_installation.sql을 실행한다.
-- 004~006 증분 migration은 기존 설치 환경 전용이므로 신규 환경에서는 실행하지 않는다.

BEGIN;

CREATE SCHEMA IF NOT EXISTS genie_gw;

COMMENT ON SCHEMA genie_gw IS 'Genie Chat·Agent 및 Supervisor Agent를 외부 시스템에 중계하는 API Gateway의 기준정보, 인증, 대화, 실행, 감사 및 운영 관리 스키마';

SET search_path TO genie_gw, public;

CREATE TABLE IF NOT EXISTS ext_sys_mst (
    sys_id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sys_cd              VARCHAR(50)  NOT NULL,
    sys_nm              VARCHAR(200) NOT NULL,
    sys_desc_cn         TEXT,
    dbx_cred_ref_nm     VARCHAR(500),
    use_yn              CHAR(1)      NOT NULL DEFAULT 'Y',
    reg_dt              TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reg_id              VARCHAR(100) NOT NULL,
    mod_dt              TIMESTAMPTZ,
    mod_id              VARCHAR(100),
    CONSTRAINT uk_ext_sys_mst_01 UNIQUE (sys_cd),
    CONSTRAINT ck_ext_sys_mst_01 CHECK (use_yn IN ('Y', 'N'))
);

COMMENT ON TABLE ext_sys_mst IS 'Gateway를 호출하는 CUBE·포털 등 외부 연계 시스템의 식별정보와 시스템별 Databricks 자격증명 참조를 관리하는 기준정보';
COMMENT ON COLUMN ext_sys_mst.sys_id IS '시스템 내부 식별자';
COMMENT ON COLUMN ext_sys_mst.sys_cd IS '시스템 코드';
COMMENT ON COLUMN ext_sys_mst.sys_nm IS '시스템 명칭';
COMMENT ON COLUMN ext_sys_mst.sys_desc_cn IS '시스템 설명 내용';
COMMENT ON COLUMN ext_sys_mst.dbx_cred_ref_nm IS 'EC2 모드의 시스템별 Databricks SP Secret 참조 명칭';
COMMENT ON COLUMN ext_sys_mst.use_yn IS '외부 시스템의 Gateway 사용 가능 여부(Y/N)';

CREATE TABLE IF NOT EXISTS api_clnt_mst (
    api_clnt_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sys_id              BIGINT       NOT NULL,
    api_clnt_cd         VARCHAR(100) NOT NULL,
    api_clnt_nm         VARCHAR(200) NOT NULL,
    auth_typ_cd         VARCHAR(30)  NOT NULL DEFAULT 'BEARER',
    oauth_prnc_id       VARCHAR(200),
    use_yn              CHAR(1)      NOT NULL DEFAULT 'Y',
    reg_dt              TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reg_id              VARCHAR(100) NOT NULL,
    mod_dt              TIMESTAMPTZ,
    mod_id              VARCHAR(100),
    CONSTRAINT fk_api_clnt_mst_01 FOREIGN KEY (sys_id)
        REFERENCES ext_sys_mst (sys_id),
    CONSTRAINT uk_api_clnt_mst_01 UNIQUE (sys_id, api_clnt_cd),
    CONSTRAINT ck_api_clnt_mst_01 CHECK (auth_typ_cd IN ('BEARER', 'API_KEY', 'OAUTH2')),
    CONSTRAINT ck_api_clnt_mst_02 CHECK (use_yn IN ('Y', 'N'))
);

COMMENT ON TABLE api_clnt_mst IS '외부 연계 시스템에 속한 API 호출 주체를 관리하며 인증 유형과 Databricks Apps OAuth Principal을 연결하는 클라이언트 기준정보';
COMMENT ON COLUMN api_clnt_mst.api_clnt_id IS 'API 클라이언트 내부 식별자';
COMMENT ON COLUMN api_clnt_mst.sys_id IS '클라이언트가 소속된 외부 연계 시스템 식별자';
COMMENT ON COLUMN api_clnt_mst.api_clnt_cd IS '외부 시스템 내에서 유일한 API 클라이언트 코드';
COMMENT ON COLUMN api_clnt_mst.auth_typ_cd IS '클라이언트 인증 유형 코드(BEARER, API_KEY, OAUTH2)';
COMMENT ON COLUMN api_clnt_mst.oauth_prnc_id IS 'Databricks Apps 인증 프록시가 전달하는 OAuth Principal 식별자';

CREATE INDEX IF NOT EXISTS ix_api_clnt_mst_01
    ON api_clnt_mst (sys_id, use_yn);

CREATE UNIQUE INDEX IF NOT EXISTS ux_api_clnt_mst_02
    ON api_clnt_mst (oauth_prnc_id)
    WHERE oauth_prnc_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS api_clnt_key_hst (
    api_clnt_key_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    api_clnt_id         BIGINT       NOT NULL,
    key_hsh_val         VARCHAR(128) NOT NULL,
    key_hint_cn         VARCHAR(100),
    eff_strt_dt         TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    eff_end_dt          TIMESTAMPTZ,
    revk_yn             CHAR(1)      NOT NULL DEFAULT 'N',
    reg_dt              TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reg_id              VARCHAR(100) NOT NULL,
    mod_dt              TIMESTAMPTZ,
    mod_id              VARCHAR(100),
    CONSTRAINT fk_api_clnt_key_hst_01 FOREIGN KEY (api_clnt_id)
        REFERENCES api_clnt_mst (api_clnt_id),
    CONSTRAINT uk_api_clnt_key_hst_01 UNIQUE (key_hsh_val),
    CONSTRAINT ck_api_clnt_key_hst_01 CHECK (revk_yn IN ('Y', 'N')),
    CONSTRAINT ck_api_clnt_key_hst_02 CHECK (eff_end_dt IS NULL OR eff_end_dt > eff_strt_dt)
);

COMMENT ON TABLE api_clnt_key_hst IS 'API 클라이언트별 X-API-Key의 해시·암호화 값, 유효기간 및 폐기 상태를 보관하는 인증키 수명주기 이력';
COMMENT ON COLUMN api_clnt_key_hst.api_clnt_id IS '인증키를 소유한 API 클라이언트 식별자';
COMMENT ON COLUMN api_clnt_key_hst.key_hsh_val IS 'API 인증키 해시값이며 원문 키 저장 금지';
COMMENT ON COLUMN api_clnt_key_hst.key_hint_cn IS '운영자 식별용 마스킹 힌트 내용';
COMMENT ON COLUMN api_clnt_key_hst.eff_strt_dt IS '인증키 효력 시작 시각';
COMMENT ON COLUMN api_clnt_key_hst.eff_end_dt IS '인증키 효력 종료 시각(NULL이면 별도 만료 없음)';
COMMENT ON COLUMN api_clnt_key_hst.revk_yn IS '인증키 강제 폐기 여부(Y/N)';

CREATE INDEX IF NOT EXISTS ix_api_clnt_key_hst_01
    ON api_clnt_key_hst (api_clnt_id, revk_yn, eff_strt_dt DESC);

CREATE TABLE IF NOT EXISTS ai_agt_mst (
    agt_id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    agt_cd              VARCHAR(100) NOT NULL,
    agt_nm              VARCHAR(200) NOT NULL,
    agt_typ_cd          VARCHAR(20)  NOT NULL DEFAULT 'GENIE',
    agt_mode_cd         VARCHAR(20)  NOT NULL DEFAULT 'CHAT',
    dbx_ws_url          VARCHAR(500),
    dbx_space_id        VARCHAR(64),
    dbx_agent_id        VARCHAR(64),
    dbx_serving_ep_nm   VARCHAR(200),
    dbx_rsrc_key        VARCHAR(200),
    secret_ref_nm       VARCHAR(300),
    agt_desc_cn         TEXT,
    long_task_use_yn    CHAR(1)      NOT NULL DEFAULT 'N',
    first_event_timeout_sec INTEGER  NOT NULL DEFAULT 90,
    idle_timeout_sec    INTEGER      NOT NULL DEFAULT 180,
    total_timeout_sec   INTEGER      NOT NULL DEFAULT 1800,
    max_continue_cnt    INTEGER      NOT NULL DEFAULT 20,
    max_active_run_cnt  INTEGER      NOT NULL DEFAULT 15,
    max_queue_run_cnt   INTEGER      NOT NULL DEFAULT 30,
    history_max_turn_cnt INTEGER     NOT NULL DEFAULT 20,
    history_max_char_cnt INTEGER     NOT NULL DEFAULT 40000,
    use_yn              CHAR(1)      NOT NULL DEFAULT 'N',
    reg_dt              TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reg_id              VARCHAR(100) NOT NULL,
    mod_dt              TIMESTAMPTZ,
    mod_id              VARCHAR(100),
    CONSTRAINT uk_ai_agt_mst_01 UNIQUE (agt_cd),
    CONSTRAINT ck_ai_agt_mst_01 CHECK (
        (agt_typ_cd = 'GENIE' AND agt_mode_cd IN ('CHAT', 'AGENT', 'BOTH'))
        OR (agt_typ_cd = 'SUPERVISOR' AND agt_mode_cd = 'SUPERVISOR')
    ),
    CONSTRAINT ck_ai_agt_mst_02 CHECK (use_yn IN ('Y', 'N')),
    CONSTRAINT ck_ai_agt_mst_03 CHECK (
        use_yn = 'N'
        OR (agt_typ_cd = 'GENIE' AND agt_mode_cd = 'CHAT' AND dbx_space_id IS NOT NULL)
        OR (agt_typ_cd = 'GENIE' AND agt_mode_cd = 'AGENT' AND dbx_agent_id IS NOT NULL)
        OR (agt_typ_cd = 'GENIE' AND agt_mode_cd = 'BOTH' AND dbx_space_id IS NOT NULL AND dbx_agent_id IS NOT NULL)
        OR (agt_typ_cd = 'SUPERVISOR' AND dbx_serving_ep_nm IS NOT NULL)
    ),
    CONSTRAINT ck_ai_agt_mst_04 CHECK (long_task_use_yn IN ('Y', 'N')),
    CONSTRAINT ck_ai_agt_mst_05 CHECK (
        first_event_timeout_sec > 0 AND idle_timeout_sec > 0 AND total_timeout_sec > 0
        AND max_continue_cnt >= 0 AND max_active_run_cnt > 0 AND max_queue_run_cnt >= 0
        AND history_max_turn_cnt > 0 AND history_max_char_cnt > 0
    )
);

COMMENT ON TABLE ai_agt_mst IS '외부에 공개할 논리 Agent와 Databricks Genie Space·Genie Agent·Supervisor Serving Endpoint 및 실행 정책을 연결하는 공통 Agent 기준정보';
COMMENT ON COLUMN ai_agt_mst.agt_cd IS '외부 API와 Admin에서 사용하는 논리 Agent 코드';
COMMENT ON COLUMN ai_agt_mst.agt_mode_cd IS '호출 모드 코드: CHAT, AGENT, BOTH 또는 SUPERVISOR';
COMMENT ON COLUMN ai_agt_mst.agt_typ_cd IS 'Agent 유형 코드: GENIE 또는 SUPERVISOR';
COMMENT ON COLUMN ai_agt_mst.dbx_space_id IS 'Databricks Chat mode 실제 Space 식별자';

CREATE INDEX IF NOT EXISTS ix_ai_agt_mst_02
    ON ai_agt_mst (dbx_space_id)
    WHERE dbx_space_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_ai_agt_mst_03
    ON ai_agt_mst (dbx_agent_id)
    WHERE dbx_agent_id IS NOT NULL;
COMMENT ON COLUMN ai_agt_mst.dbx_agent_id IS 'Databricks Agent mode 실제 Agent 식별자';
COMMENT ON COLUMN ai_agt_mst.dbx_serving_ep_nm IS 'Supervisor 실제 Databricks serving endpoint 이름';
COMMENT ON COLUMN ai_agt_mst.secret_ref_nm IS 'Secret 원문이 아닌 Secret 참조 명칭';
COMMENT ON COLUMN ai_agt_mst.long_task_use_yn IS 'Supervisor long-running task continuation 허용 여부(Y/N)';
COMMENT ON COLUMN ai_agt_mst.first_event_timeout_sec IS 'upstream 연결 후 첫 SSE Event 수신 제한 시간(초)';
COMMENT ON COLUMN ai_agt_mst.idle_timeout_sec IS '연속 SSE Event 미수신 허용 시간(초)';
COMMENT ON COLUMN ai_agt_mst.total_timeout_sec IS '하나의 Run에 허용하는 전체 실행 제한 시간(초)';
COMMENT ON COLUMN ai_agt_mst.max_continue_cnt IS 'long-running task 자동 continuation 최대 횟수';
COMMENT ON COLUMN ai_agt_mst.max_active_run_cnt IS '해당 Agent에서 동시에 실행할 수 있는 활성 Run 최대 수';
COMMENT ON COLUMN ai_agt_mst.max_queue_run_cnt IS '해당 Agent에서 대기할 수 있는 Run 최대 수';
COMMENT ON COLUMN ai_agt_mst.history_max_turn_cnt IS 'Supervisor 요청에 전달할 최근 대화 turn 최대 수';
COMMENT ON COLUMN ai_agt_mst.history_max_char_cnt IS 'Supervisor 요청에 전달할 대화 이력 최대 문자 수';

CREATE INDEX IF NOT EXISTS ix_ai_agt_mst_01
    ON ai_agt_mst (use_yn, agt_mode_cd);

CREATE TABLE IF NOT EXISTS sys_ai_agt_map (
    sys_genie_agt_map_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sys_id               BIGINT       NOT NULL,
    agt_id               BIGINT       NOT NULL,
    route_prty_no        INTEGER      NOT NULL DEFAULT 1,
    dflt_yn              CHAR(1)      NOT NULL DEFAULT 'N',
    eff_strt_dt          TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    eff_end_dt           TIMESTAMPTZ,
    use_yn               CHAR(1)      NOT NULL DEFAULT 'Y',
    reg_dt               TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reg_id               VARCHAR(100) NOT NULL,
    mod_dt               TIMESTAMPTZ,
    mod_id               VARCHAR(100),
    CONSTRAINT fk_sys_ai_agt_map_01 FOREIGN KEY (sys_id)
        REFERENCES ext_sys_mst (sys_id),
    CONSTRAINT fk_sys_ai_agt_map_02 FOREIGN KEY (agt_id)
        REFERENCES ai_agt_mst (agt_id),
    CONSTRAINT uk_sys_ai_agt_map_01 UNIQUE (sys_id, agt_id, eff_strt_dt),
    CONSTRAINT ck_sys_ai_agt_map_01 CHECK (route_prty_no > 0),
    CONSTRAINT ck_sys_ai_agt_map_02 CHECK (dflt_yn IN ('Y', 'N')),
    CONSTRAINT ck_sys_ai_agt_map_03 CHECK (use_yn IN ('Y', 'N')),
    CONSTRAINT ck_sys_ai_agt_map_04 CHECK (eff_end_dt IS NULL OR eff_end_dt > eff_strt_dt)
);

COMMENT ON TABLE sys_ai_agt_map IS '외부 연계 시스템이 사용할 수 있는 Agent의 권한, 기본 Agent, 라우팅 우선순위 및 적용 기간을 관리하는 매핑 기준정보';
COMMENT ON COLUMN sys_ai_agt_map.route_prty_no IS '동일 시스템 내 라우팅 우선순위 번호';
COMMENT ON COLUMN sys_ai_agt_map.dflt_yn IS '해당 외부 시스템의 기본 Agent 여부(Y/N)';
COMMENT ON COLUMN sys_ai_agt_map.eff_strt_dt IS '시스템-Agent 매핑 효력 시작 시각';
COMMENT ON COLUMN sys_ai_agt_map.eff_end_dt IS '시스템-Agent 매핑 효력 종료 시각(NULL이면 종료일 없음)';

CREATE INDEX IF NOT EXISTS ix_sys_ai_agt_map_01
    ON sys_ai_agt_map (sys_id, use_yn, route_prty_no, eff_strt_dt DESC);

CREATE UNIQUE INDEX IF NOT EXISTS ux_sys_ai_agt_map_01
    ON sys_ai_agt_map (sys_id)
    WHERE dflt_yn = 'Y' AND use_yn = 'Y' AND eff_end_dt IS NULL;

CREATE TABLE IF NOT EXISTS ai_cnvs_mst (
    cnvs_id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cnvs_uid             VARCHAR(64)  NOT NULL,
    api_clnt_id          BIGINT       NOT NULL,
    agt_id               BIGINT       NOT NULL,
    ext_sess_id          VARCHAR(200),
    usr_id               VARCHAR(200),
    dbx_cnvs_id          VARCHAR(64),
    cnvs_mode_cd         VARCHAR(20)  NOT NULL DEFAULT 'CHAT',
    cnvs_title_nm        VARCHAR(1000),
    cnvs_stts_cd         VARCHAR(50)  NOT NULL DEFAULT 'CREATED',
    str_dt               TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    end_dt               TIMESTAMPTZ,
    last_msg_dt          TIMESTAMPTZ,
    use_yn               CHAR(1)      NOT NULL DEFAULT 'Y',
    reg_dt               TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reg_id               VARCHAR(100) NOT NULL,
    mod_dt               TIMESTAMPTZ,
    mod_id               VARCHAR(100),
    CONSTRAINT uk_ai_cnvs_mst_01 UNIQUE (cnvs_uid),
    CONSTRAINT fk_ai_cnvs_mst_01 FOREIGN KEY (api_clnt_id)
        REFERENCES api_clnt_mst (api_clnt_id),
    CONSTRAINT fk_ai_cnvs_mst_02 FOREIGN KEY (agt_id)
        REFERENCES ai_agt_mst (agt_id),
    CONSTRAINT ck_ai_cnvs_mst_01 CHECK (cnvs_mode_cd IN ('CHAT', 'AGENT', 'SUPERVISOR')),
    CONSTRAINT ck_ai_cnvs_mst_02 CHECK (use_yn IN ('Y', 'N')),
    CONSTRAINT ck_ai_cnvs_mst_03 CHECK (end_dt IS NULL OR end_dt >= str_dt)
);

COMMENT ON TABLE ai_cnvs_mst IS 'API 클라이언트·사용자·Agent별 대화 소유권과 외부 세션, Databricks Conversation 및 CHAT·AGENT·SUPERVISOR 모드를 연결하는 공통 대화 기준정보';
COMMENT ON COLUMN ai_cnvs_mst.cnvs_uid IS 'Gateway 외부 공개 Conversation 식별자';
COMMENT ON COLUMN ai_cnvs_mst.api_clnt_id IS '대화를 생성한 API 클라이언트 식별자';
COMMENT ON COLUMN ai_cnvs_mst.agt_id IS '대화에 연결된 논리 Agent 식별자';
COMMENT ON COLUMN ai_cnvs_mst.ext_sess_id IS '외부 시스템 세션 식별자';
COMMENT ON COLUMN ai_cnvs_mst.usr_id IS '외부 시스템이 전달한 사용자 식별값으로 대화 소유권 검증에 사용';
COMMENT ON COLUMN ai_cnvs_mst.dbx_cnvs_id IS 'Databricks 대화 식별자';
COMMENT ON COLUMN ai_cnvs_mst.cnvs_mode_cd IS '대화 호출 모드 코드(CHAT, AGENT, SUPERVISOR)';
COMMENT ON COLUMN ai_cnvs_mst.cnvs_stts_cd IS 'Gateway가 관리하는 대화 상태 코드';
COMMENT ON COLUMN ai_cnvs_mst.last_msg_dt IS '대화의 마지막 사용자 또는 Assistant 메시지 발생 시각';

CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_cnvs_mst_01
    ON ai_cnvs_mst (agt_id, dbx_cnvs_id)
    WHERE dbx_cnvs_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_ai_cnvs_mst_01
    ON ai_cnvs_mst (api_clnt_id, usr_id, str_dt DESC);

CREATE INDEX IF NOT EXISTS ix_ai_cnvs_mst_03
    ON ai_cnvs_mst (api_clnt_id, usr_id, agt_id, dbx_cnvs_id)
    WHERE dbx_cnvs_id IS NOT NULL AND use_yn = 'Y';

CREATE INDEX IF NOT EXISTS ix_ai_cnvs_mst_04
    ON ai_cnvs_mst (api_clnt_id, usr_id, agt_id, cnvs_mode_cd, last_msg_dt DESC)
    WHERE dbx_cnvs_id IS NOT NULL AND use_yn = 'Y';

CREATE INDEX IF NOT EXISTS ix_ai_cnvs_mst_05
    ON ai_cnvs_mst (api_clnt_id, usr_id, agt_id, last_msg_dt DESC)
    WHERE dbx_cnvs_id IS NOT NULL AND use_yn = 'Y';

CREATE INDEX IF NOT EXISTS ix_ai_cnvs_mst_02
    ON ai_cnvs_mst (ext_sess_id)
    WHERE ext_sess_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS ai_run_mst (
    run_id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_uid              VARCHAR(64)  NOT NULL,
    cnvs_id              BIGINT       NOT NULL,
    request_hash         VARCHAR(64)  NOT NULL,
    run_stts_cd          VARCHAR(30)  NOT NULL DEFAULT 'QUEUED',
    dbx_response_id      VARCHAR(200),
    continue_request_id  VARCHAR(200),
    continue_cnt         INTEGER      NOT NULL DEFAULT 0,
    execution_owner_id   VARCHAR(200),
    lease_expires_dt     TIMESTAMPTZ,
    heartbeat_dt         TIMESTAMPTZ,
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
    CONSTRAINT uk_ai_run_mst_01 UNIQUE (run_uid),
    CONSTRAINT fk_ai_run_mst_01 FOREIGN KEY (cnvs_id)
        REFERENCES ai_cnvs_mst (cnvs_id),
    CONSTRAINT ck_ai_run_mst_01 CHECK (
        run_stts_cd IN (
            'QUEUED', 'STREAMING', 'CONTINUING', 'WAITING_APPROVAL', 'CANCEL_REQUESTED',
            'COMPLETED', 'CANCELLED', 'TIMED_OUT', 'FAILED'
        )
    ),
    CONSTRAINT ck_ai_run_mst_02 CHECK (continue_cnt >= 0),
    CONSTRAINT ck_ai_run_mst_03 CHECK (deadline_dt > str_dt),
    CONSTRAINT ck_ai_run_mst_04 CHECK (end_dt IS NULL OR end_dt >= str_dt)
);

COMMENT ON TABLE ai_run_mst IS 'Supervisor Agent 요청 한 건의 대기·스트리밍·continuation·중단·완료 상태와 timeout, 오류 및 복수 Gateway instance 실행 lease를 관리하는 Run 기준정보';
COMMENT ON COLUMN ai_run_mst.run_uid IS 'Gateway 외부 공개 Run 식별자';
COMMENT ON COLUMN ai_run_mst.request_hash IS '진행 중 동일 요청 제한용 SHA-256 hash';
COMMENT ON COLUMN ai_run_mst.run_stts_cd IS 'Run 상태 코드: QUEUED, STREAMING, CONTINUING, WAITING_APPROVAL, CANCEL_REQUESTED, COMPLETED, CANCELLED, TIMED_OUT, FAILED';
COMMENT ON COLUMN ai_run_mst.dbx_response_id IS 'Databricks Responses API가 발급한 응답 식별자';
COMMENT ON COLUMN ai_run_mst.continue_request_id IS '현재 처리 중인 long-running task continuation 요청 식별자';
COMMENT ON COLUMN ai_run_mst.continue_cnt IS '동일 Run에서 수행한 continuation 횟수';
COMMENT ON COLUMN ai_run_mst.execution_owner_id IS 'Supervisor Run 실행 lease를 소유한 Gateway instance 식별자';
COMMENT ON COLUMN ai_run_mst.lease_expires_dt IS 'Gateway instance의 Run 실행 소유권 만료 시각';
COMMENT ON COLUMN ai_run_mst.heartbeat_dt IS 'Run 실행 소유 Gateway instance의 마지막 heartbeat 시각';
COMMENT ON COLUMN ai_run_mst.deadline_dt IS '전체 timeout 정책으로 계산한 Run 강제 종료 예정 시각';
COMMENT ON COLUMN ai_run_mst.cancel_req_dt IS '사용자 또는 외부 시스템이 Run 중단을 요청한 시각';
COMMENT ON COLUMN ai_run_mst.trace_id IS 'Run 생성 요청부터 upstream 호출까지 연결하는 Gateway 추적 식별자';

CREATE INDEX IF NOT EXISTS ix_ai_run_mst_01
    ON ai_run_mst (cnvs_id, str_dt DESC);
CREATE INDEX IF NOT EXISTS ix_ai_run_mst_02
    ON ai_run_mst (run_stts_cd, deadline_dt);
CREATE INDEX IF NOT EXISTS ix_ai_run_mst_03
    ON ai_run_mst (run_stts_cd, lease_expires_dt)
    WHERE run_stts_cd IN ('QUEUED', 'STREAMING', 'CONTINUING', 'WAITING_APPROVAL', 'CANCEL_REQUESTED');
CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_run_mst_02
    ON ai_run_mst (cnvs_id, request_hash)
    WHERE run_stts_cd IN ('QUEUED', 'STREAMING', 'CONTINUING', 'WAITING_APPROVAL', 'CANCEL_REQUESTED');

CREATE TABLE IF NOT EXISTS ai_run_aprv_hst (
    aprv_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    aprv_uid VARCHAR(64) NOT NULL UNIQUE,
    run_id BIGINT NOT NULL REFERENCES ai_run_mst (run_id),
    dbx_aprv_req_id VARCHAR(200) NOT NULL,
    tool_nm VARCHAR(200),
    server_label VARCHAR(200),
    args_data JSONB,
    req_item_data JSONB NOT NULL,
    context_data JSONB NOT NULL,
    aprv_stts_cd VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    decision_dt TIMESTAMPTZ,
    decision_id VARCHAR(100),
    reg_dt TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reg_id VARCHAR(100) NOT NULL,
    CONSTRAINT uk_ai_run_aprv_hst_01 UNIQUE (run_id, dbx_aprv_req_id),
    CONSTRAINT ck_ai_run_aprv_hst_01 CHECK (aprv_stts_cd IN ('PENDING','APPROVED','REJECTED'))
);

COMMENT ON TABLE ai_run_aprv_hst IS 'Supervisor MCP·Web Search 도구의 사용자 승인/거절과 동일 Run 재개 문맥을 보존하는 감사 이력';
COMMENT ON COLUMN ai_run_aprv_hst.aprv_uid IS '외부 API와 UI에 노출하는 Gateway 승인 요청 식별자';
COMMENT ON COLUMN ai_run_aprv_hst.dbx_aprv_req_id IS 'Databricks mcp_approval_request 원본 식별자';
COMMENT ON COLUMN ai_run_aprv_hst.args_data IS '사용자가 검토할 도구 호출 인자 JSON';
COMMENT ON COLUMN ai_run_aprv_hst.req_item_data IS '후속 요청에 되돌려 보낼 원본 mcp_approval_request Item';
COMMENT ON COLUMN ai_run_aprv_hst.context_data IS '승인 후 같은 Run을 재개하기 위한 승인 직전 upstream 대화 문맥';
COMMENT ON COLUMN ai_run_aprv_hst.aprv_stts_cd IS '승인 상태(PENDING, APPROVED, REJECTED)';

CREATE INDEX IF NOT EXISTS ix_ai_run_aprv_hst_01
    ON ai_run_aprv_hst (run_id, aprv_stts_cd, reg_dt DESC);

CREATE TABLE IF NOT EXISTS ai_run_item_hst (
    run_item_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id               BIGINT       NOT NULL,
    item_seq_no          INTEGER      NOT NULL,
    dbx_item_id          VARCHAR(200),
    item_typ_cd          VARCHAR(50)  NOT NULL,
    item_stts_cd         VARCHAR(30)  NOT NULL DEFAULT 'COMPLETED',
    role_cd              VARCHAR(20),
    title_nm             VARCHAR(1000),
    text_cn              TEXT,
    item_data            JSONB,
    item_size_no         BIGINT       NOT NULL DEFAULT 0,
    trnc_yn              CHAR(1)      NOT NULL DEFAULT 'N',
    reg_dt               TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reg_id               VARCHAR(100) NOT NULL,
    CONSTRAINT fk_ai_run_item_hst_01 FOREIGN KEY (run_id)
        REFERENCES ai_run_mst (run_id),
    CONSTRAINT uk_ai_run_item_hst_01 UNIQUE (run_id, item_seq_no),
    CONSTRAINT ck_ai_run_item_hst_01 CHECK (item_seq_no > 0),
    CONSTRAINT ck_ai_run_item_hst_02 CHECK (role_cd IS NULL OR role_cd IN ('USER', 'ASSISTANT', 'SYSTEM', 'TOOL')),
    CONSTRAINT ck_ai_run_item_hst_03 CHECK (item_size_no >= 0),
    CONSTRAINT ck_ai_run_item_hst_04 CHECK (trnc_yn IN ('Y', 'N'))
);

COMMENT ON TABLE ai_run_item_hst IS 'Supervisor Run에서 생성된 사용자·Assistant·도구 결과 등 완료 Item을 순서대로 정규화하여 대화 재구성 및 결과 조회에 사용하는 이력';
COMMENT ON COLUMN ai_run_item_hst.item_seq_no IS 'Run 내부 Item 표시 및 재구성 순번';
COMMENT ON COLUMN ai_run_item_hst.dbx_item_id IS 'Databricks가 부여한 원본 Item 식별자';
COMMENT ON COLUMN ai_run_item_hst.item_typ_cd IS '메시지, 도구 결과, 표 등 정규화된 Item 유형 코드';
COMMENT ON COLUMN ai_run_item_hst.role_cd IS 'Item 발화 또는 생성 주체(USER, ASSISTANT, SYSTEM, TOOL)';
COMMENT ON COLUMN ai_run_item_hst.item_data IS '텍스트 외 구조화 결과를 보존하는 정규화 또는 원본 JSON';
COMMENT ON COLUMN ai_run_item_hst.item_size_no IS '저장된 Item 데이터의 계산 크기(byte)';
COMMENT ON COLUMN ai_run_item_hst.trnc_yn IS '허용 크기 초과로 Item 내용이 절삭되었는지 여부(Y/N)';

CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_run_item_hst_02
    ON ai_run_item_hst (run_id, dbx_item_id)
    WHERE dbx_item_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_ai_run_item_hst_01
    ON ai_run_item_hst (run_id, item_seq_no);

CREATE TABLE IF NOT EXISTS ai_run_evt_hst (
    run_evt_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id               BIGINT       NOT NULL,
    evt_uid              VARCHAR(64)  NOT NULL,
    evt_seq_no           INTEGER      NOT NULL,
    evt_typ_cd           VARCHAR(100) NOT NULL,
    dbx_evt_id           VARCHAR(200),
    evt_data             JSONB,
    reg_dt               TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reg_id               VARCHAR(100) NOT NULL,
    CONSTRAINT fk_ai_run_evt_hst_01 FOREIGN KEY (run_id)
        REFERENCES ai_run_mst (run_id),
    CONSTRAINT uk_ai_run_evt_hst_01 UNIQUE (evt_uid),
    CONSTRAINT uk_ai_run_evt_hst_02 UNIQUE (run_id, evt_seq_no),
    CONSTRAINT ck_ai_run_evt_hst_01 CHECK (evt_seq_no > 0)
);

COMMENT ON TABLE ai_run_evt_hst IS 'Supervisor upstream SSE와 Gateway 제어 Event를 Run별 순번으로 저장하여 실시간 전달, Last-Event-ID 재연결 및 장애 추적에 사용하는 이력';
COMMENT ON COLUMN ai_run_evt_hst.evt_uid IS 'Gateway가 외부 SSE id로 공개하는 유일 Event 식별자';
COMMENT ON COLUMN ai_run_evt_hst.evt_seq_no IS 'Run 내부 Event 발생 순번';
COMMENT ON COLUMN ai_run_evt_hst.evt_typ_cd IS '정규화된 SSE 또는 Gateway 제어 Event 유형 코드';
COMMENT ON COLUMN ai_run_evt_hst.dbx_evt_id IS 'Databricks upstream Event 식별자';
COMMENT ON COLUMN ai_run_evt_hst.evt_data IS '외부 전달과 재생에 사용하는 정규화 Event JSON';

CREATE INDEX IF NOT EXISTS ix_ai_run_evt_hst_01
    ON ai_run_evt_hst (run_id, reg_dt, evt_seq_no);

CREATE TABLE IF NOT EXISTS genie_msg_hst (
    msg_id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cnvs_id              BIGINT       NOT NULL,
    dbx_msg_id           VARCHAR(64),
    msg_seq_no           INTEGER      NOT NULL,
    msg_role_cd          VARCHAR(20)  NOT NULL,
    msg_cn               TEXT,
    msg_stts_cd          VARCHAR(50)  NOT NULL DEFAULT 'SUBMITTED',
    msg_data             JSONB,
    dbx_crt_dt           TIMESTAMPTZ,
    dbx_mod_dt           TIMESTAMPTZ,
    reg_dt               TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reg_id               VARCHAR(100) NOT NULL,
    CONSTRAINT fk_genie_msg_hst_01 FOREIGN KEY (cnvs_id)
        REFERENCES ai_cnvs_mst (cnvs_id),
    CONSTRAINT uk_genie_msg_hst_01 UNIQUE (cnvs_id, msg_seq_no),
    CONSTRAINT ck_genie_msg_hst_01 CHECK (msg_seq_no > 0),
    CONSTRAINT ck_genie_msg_hst_02 CHECK (msg_role_cd IN ('USER', 'ASSISTANT', 'SYSTEM'))
);

COMMENT ON TABLE genie_msg_hst IS 'Genie Chat mode의 사용자·Assistant·System 메시지를 Conversation별 순번과 Databricks 상태로 정규화하여 저장하는 메시지 이력';
COMMENT ON COLUMN genie_msg_hst.dbx_msg_id IS 'Databricks Genie가 발급한 메시지 식별자';
COMMENT ON COLUMN genie_msg_hst.msg_seq_no IS 'Conversation 내부 메시지 발생 순번';
COMMENT ON COLUMN genie_msg_hst.msg_role_cd IS '메시지 발화 주체(USER, ASSISTANT, SYSTEM)';
COMMENT ON COLUMN genie_msg_hst.msg_cn IS '메시지 내용이며 개인정보 및 민감정보 저장 정책 적용 대상';
COMMENT ON COLUMN genie_msg_hst.msg_stts_cd IS 'Databricks 응답을 기준으로 정규화한 메시지 처리 상태 코드';
COMMENT ON COLUMN genie_msg_hst.msg_data IS 'Databricks 메시지 원본 또는 확장 데이터';

CREATE UNIQUE INDEX IF NOT EXISTS ux_genie_msg_hst_01
    ON genie_msg_hst (cnvs_id, dbx_msg_id)
    WHERE dbx_msg_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_genie_msg_hst_01
    ON genie_msg_hst (cnvs_id, reg_dt, msg_seq_no);

CREATE TABLE IF NOT EXISTS api_req_hst (
    req_id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trace_id             VARCHAR(64)  NOT NULL,
    api_clnt_id          BIGINT       NOT NULL,
    agt_id               BIGINT,
    cnvs_id              BIGINT,
    run_id               BIGINT,
    msg_id               BIGINT,
    http_mthd_cd         VARCHAR(10)  NOT NULL,
    req_uri              VARCHAR(2000) NOT NULL,
    dbx_req_id           VARCHAR(200),
    req_data             JSONB,
    rsp_data             JSONB,
    rsp_http_stts_no     SMALLINT,
    err_cd               VARCHAR(100),
    err_msg_cn           TEXT,
    proc_ms              BIGINT,
    src_ip_val           INET,
    req_dt               TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    rsp_dt               TIMESTAMPTZ,
    reg_dt               TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reg_id               VARCHAR(100) NOT NULL,
    CONSTRAINT fk_api_req_hst_01 FOREIGN KEY (api_clnt_id)
        REFERENCES api_clnt_mst (api_clnt_id),
    CONSTRAINT fk_api_req_hst_02 FOREIGN KEY (agt_id)
        REFERENCES ai_agt_mst (agt_id),
    CONSTRAINT fk_api_req_hst_03 FOREIGN KEY (cnvs_id)
        REFERENCES ai_cnvs_mst (cnvs_id),
    CONSTRAINT fk_api_req_hst_04 FOREIGN KEY (run_id)
        REFERENCES ai_run_mst (run_id),
    CONSTRAINT fk_api_req_hst_05 FOREIGN KEY (msg_id)
        REFERENCES genie_msg_hst (msg_id),
    CONSTRAINT uk_api_req_hst_01 UNIQUE (trace_id),
    CONSTRAINT ck_api_req_hst_01 CHECK (http_mthd_cd IN ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS')),
    CONSTRAINT ck_api_req_hst_02 CHECK (rsp_http_stts_no IS NULL OR rsp_http_stts_no BETWEEN 100 AND 599),
    CONSTRAINT ck_api_req_hst_03 CHECK (proc_ms IS NULL OR proc_ms >= 0),
    CONSTRAINT ck_api_req_hst_04 CHECK (rsp_dt IS NULL OR rsp_dt >= req_dt)
);

COMMENT ON TABLE api_req_hst IS 'Gateway가 수신한 API 요청과 Databricks 호출 결과의 상태·처리시간·오류·연관 Agent/대화/Run/메시지를 추적하는 감사 이력';
COMMENT ON COLUMN api_req_hst.trace_id IS 'Gateway 전 구간 요청 추적 식별자';
COMMENT ON COLUMN api_req_hst.dbx_req_id IS 'Databricks가 응답 헤더 등에 제공한 upstream 요청 식별자';
COMMENT ON COLUMN api_req_hst.req_data IS '마스킹 정책이 적용된 선택적 요청 JSON 데이터';
COMMENT ON COLUMN api_req_hst.rsp_data IS '마스킹 정책이 적용된 선택적 응답 JSON 데이터';
COMMENT ON COLUMN api_req_hst.rsp_http_stts_no IS '외부 클라이언트에 반환한 최종 HTTP 상태 코드';
COMMENT ON COLUMN api_req_hst.proc_ms IS 'Gateway 요청 수신부터 응답 완료까지의 처리 시간(ms)';
COMMENT ON COLUMN api_req_hst.src_ip_val IS '프록시 정책을 적용해 식별한 호출 원본 IP 주소';

CREATE INDEX IF NOT EXISTS ix_api_req_hst_01
    ON api_req_hst (api_clnt_id, req_dt DESC);

CREATE INDEX IF NOT EXISTS ix_api_req_hst_02
    ON api_req_hst (cnvs_id, req_dt DESC)
    WHERE cnvs_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_api_req_hst_03
    ON api_req_hst (rsp_http_stts_no, req_dt DESC);

CREATE INDEX IF NOT EXISTS ix_api_req_hst_04
    ON api_req_hst (run_id, req_dt DESC)
    WHERE run_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS genie_msg_atch_dtl (
    msg_atch_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    msg_id               BIGINT       NOT NULL,
    dbx_atch_id          VARCHAR(64),
    atch_seq_no          INTEGER      NOT NULL,
    atch_typ_cd          VARCHAR(30)  NOT NULL,
    atch_purps_cd        VARCHAR(50),
    text_cn              TEXT,
    qry_title_nm         VARCHAR(1000),
    qry_cn               TEXT,
    sql_stmt_id          VARCHAR(100),
    viz_title_nm         VARCHAR(1000),
    atch_data            JSONB,
    reg_dt               TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reg_id               VARCHAR(100) NOT NULL,
    CONSTRAINT fk_genie_msg_atch_dtl_01 FOREIGN KEY (msg_id)
        REFERENCES genie_msg_hst (msg_id),
    CONSTRAINT uk_genie_msg_atch_dtl_01 UNIQUE (msg_id, atch_seq_no),
    CONSTRAINT ck_genie_msg_atch_dtl_01 CHECK (atch_seq_no > 0),
    CONSTRAINT ck_genie_msg_atch_dtl_02 CHECK (atch_typ_cd IN ('TEXT', 'QUERY', 'VISUALIZATION', 'SUGGESTED_QUESTIONS', 'OTHER'))
);

COMMENT ON TABLE genie_msg_atch_dtl IS 'Genie Chat 메시지에 포함된 텍스트·SQL Query·시각화·추천 질문 등의 Attachment를 유형별 컬럼과 원본 JSON으로 저장하는 상세 이력';
COMMENT ON COLUMN genie_msg_atch_dtl.atch_typ_cd IS 'Attachment 유형 코드(TEXT, QUERY, VISUALIZATION, SUGGESTED_QUESTIONS, OTHER)';
COMMENT ON COLUMN genie_msg_atch_dtl.qry_cn IS 'Genie가 생성한 SQL 또는 Query 본문';
COMMENT ON COLUMN genie_msg_atch_dtl.sql_stmt_id IS 'Query 결과 조회에 사용하는 Databricks SQL Statement 식별자';
COMMENT ON COLUMN genie_msg_atch_dtl.viz_title_nm IS '시각화 카드에 표시할 제목';
COMMENT ON COLUMN genie_msg_atch_dtl.atch_data IS 'Attachment 원본 또는 확장 JSON 데이터';

CREATE UNIQUE INDEX IF NOT EXISTS ux_genie_msg_atch_dtl_01
    ON genie_msg_atch_dtl (msg_id, dbx_atch_id)
    WHERE dbx_atch_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_genie_msg_atch_dtl_01
    ON genie_msg_atch_dtl (msg_id, atch_typ_cd, atch_seq_no);

CREATE TABLE IF NOT EXISTS genie_msg_stts_hst (
    msg_stts_hst_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    msg_id               BIGINT       NOT NULL,
    req_id               BIGINT,
    stts_seq_no          INTEGER      NOT NULL,
    msg_stts_cd          VARCHAR(50)  NOT NULL,
    err_cd               VARCHAR(100),
    err_msg_cn           TEXT,
    stts_data            JSONB,
    reg_dt               TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reg_id               VARCHAR(100) NOT NULL,
    CONSTRAINT fk_genie_msg_stts_hst_01 FOREIGN KEY (msg_id)
        REFERENCES genie_msg_hst (msg_id),
    CONSTRAINT fk_genie_msg_stts_hst_02 FOREIGN KEY (req_id)
        REFERENCES api_req_hst (req_id),
    CONSTRAINT uk_genie_msg_stts_hst_01 UNIQUE (msg_id, stts_seq_no),
    CONSTRAINT ck_genie_msg_stts_hst_01 CHECK (stts_seq_no > 0)
);

COMMENT ON TABLE genie_msg_stts_hst IS 'Genie Chat 메시지의 제출·실행·완료·실패 상태 전이를 API 요청과 연결하여 시간순으로 보존하는 상태 이력';
COMMENT ON COLUMN genie_msg_stts_hst.msg_stts_cd IS 'Databricks Genie 메시지 상태 코드';
COMMENT ON COLUMN genie_msg_stts_hst.err_cd IS '해당 상태 전이에서 확인된 Gateway 또는 Databricks 오류 코드';
COMMENT ON COLUMN genie_msg_stts_hst.stts_data IS '상태 전이 시점의 원본 또는 확장 JSON 데이터';

CREATE INDEX IF NOT EXISTS ix_genie_msg_stts_hst_01
    ON genie_msg_stts_hst (msg_id, reg_dt, stts_seq_no);

-- Admin Web 및 운영 관리 객체
CREATE TABLE IF NOT EXISTS dbx_ws_mst (
    ws_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ws_cd VARCHAR(100) NOT NULL UNIQUE,
    ws_nm VARCHAR(200) NOT NULL,
    ws_url VARCHAR(500),
    dflt_yn CHAR(1) NOT NULL DEFAULT 'N' CHECK (dflt_yn IN ('Y','N')),
    use_yn CHAR(1) NOT NULL DEFAULT 'Y' CHECK (use_yn IN ('Y','N')),
    reg_dt TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reg_id VARCHAR(100) NOT NULL,
    mod_dt TIMESTAMPTZ,
    mod_id VARCHAR(100)
);
COMMENT ON TABLE dbx_ws_mst IS 'Agent가 사용할 Databricks Workspace의 논리 코드·URL·기본 Workspace 여부를 관리하는 Admin 기준정보';
COMMENT ON COLUMN dbx_ws_mst.ws_cd IS 'Admin과 내부 라우팅에서 사용하는 Workspace 코드';
COMMENT ON COLUMN dbx_ws_mst.ws_url IS 'Databricks Workspace 기본 URL';
COMMENT ON COLUMN dbx_ws_mst.dflt_yn IS 'Agent에 Workspace가 명시되지 않았을 때 사용할 기본 Workspace 여부(Y/N)';
CREATE UNIQUE INDEX IF NOT EXISTS ux_dbx_ws_mst_01 ON dbx_ws_mst (dflt_yn) WHERE dflt_yn='Y' AND use_yn='Y';
ALTER TABLE ai_agt_mst ADD COLUMN IF NOT EXISTS ws_id BIGINT REFERENCES dbx_ws_mst(ws_id);
COMMENT ON COLUMN ai_agt_mst.ws_id IS 'Agent가 호출할 Databricks Workspace 식별자';
CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_agt_mst_04
    ON ai_agt_mst (ws_id, dbx_serving_ep_nm)
    WHERE dbx_serving_ep_nm IS NOT NULL;
ALTER TABLE api_clnt_key_hst ADD COLUMN IF NOT EXISTS key_enc_val TEXT;
ALTER TABLE api_clnt_key_hst ADD COLUMN IF NOT EXISTS key_nonce_val VARCHAR(100);
ALTER TABLE api_clnt_key_hst ADD COLUMN IF NOT EXISTS enc_key_ver_no INTEGER;
COMMENT ON COLUMN api_clnt_key_hst.key_enc_val IS 'Admin의 제한된 원문 키 재확인을 위한 AES-GCM 암호문';
COMMENT ON COLUMN api_clnt_key_hst.key_nonce_val IS 'API Key 암호화에 사용한 AES-GCM nonce의 인코딩 값';
COMMENT ON COLUMN api_clnt_key_hst.enc_key_ver_no IS '암호화 키 교체를 위한 키 버전 번호';
CREATE TABLE IF NOT EXISTS gw_adm_usr_mst (
    adm_usr_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, adm_id VARCHAR(100) NOT NULL UNIQUE,
    pwd_hsh_val TEXT NOT NULL, use_yn CHAR(1) NOT NULL DEFAULT 'Y' CHECK (use_yn IN ('Y','N')),
    last_lgn_dt TIMESTAMPTZ, pwd_mod_dt TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reg_dt TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, reg_id VARCHAR(100) NOT NULL
);
COMMENT ON TABLE gw_adm_usr_mst IS 'Gateway Admin Web에 로그인할 운영 관리자 계정과 비밀번호 해시 및 사용 상태를 관리하는 기준정보';
COMMENT ON COLUMN gw_adm_usr_mst.pwd_hsh_val IS '단방향 알고리즘으로 생성한 관리자 비밀번호 해시값';
COMMENT ON COLUMN gw_adm_usr_mst.last_lgn_dt IS '관리자 마지막 로그인 성공 시각';
CREATE TABLE IF NOT EXISTS gw_adm_sess_hst (
    adm_sess_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    adm_usr_id BIGINT NOT NULL REFERENCES gw_adm_usr_mst(adm_usr_id),
    sess_tkn_hsh_val VARCHAR(64) NOT NULL UNIQUE, eff_end_dt TIMESTAMPTZ NOT NULL,
    revk_yn CHAR(1) NOT NULL DEFAULT 'N' CHECK (revk_yn IN ('Y','N')),
    src_ip_val INET, reg_dt TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
COMMENT ON TABLE gw_adm_sess_hst IS 'Gateway Admin Web 로그인 세션의 토큰 해시, 만료, 폐기 및 접속 IP를 관리하는 세션 이력';
COMMENT ON COLUMN gw_adm_sess_hst.sess_tkn_hsh_val IS '원문을 저장하지 않는 관리자 세션 토큰 SHA-256 해시값';
COMMENT ON COLUMN gw_adm_sess_hst.eff_end_dt IS '관리자 세션 만료 시각';
CREATE TABLE IF NOT EXISTS gw_oper_cfg_mst (
    cfg_key_nm VARCHAR(100) PRIMARY KEY, cfg_val_cn TEXT,
    mod_dt TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, mod_id VARCHAR(100) NOT NULL
);
COMMENT ON TABLE gw_oper_cfg_mst IS 'Gateway의 동시 실행 한도 등 런타임 운영 정책을 Admin Web에서 관리하는 Key-Value 설정 저장소';
COMMENT ON COLUMN gw_oper_cfg_mst.cfg_key_nm IS '애플리케이션에서 정의한 운영 설정 키';
COMMENT ON COLUMN gw_oper_cfg_mst.cfg_val_cn IS '운영 설정 값이며 비밀번호·Token·Secret 원문 저장 금지';
CREATE TABLE IF NOT EXISTS gw_adm_act_hst (
    adm_act_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    adm_usr_id BIGINT REFERENCES gw_adm_usr_mst(adm_usr_id), act_typ_cd VARCHAR(50) NOT NULL,
    trgt_typ_cd VARCHAR(50), trgt_id_val VARCHAR(200), act_data JSONB, src_ip_val INET,
    reg_dt TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
COMMENT ON TABLE gw_adm_act_hst IS 'Admin Web에서 수행한 시스템·클라이언트·Workspace·Agent·라우팅·API Key 변경 작업을 대상과 접속 IP 기준으로 기록하는 감사 이력';
COMMENT ON COLUMN gw_adm_act_hst.act_typ_cd IS '관리자 작업 유형 코드';
COMMENT ON COLUMN gw_adm_act_hst.trgt_typ_cd IS '관리자 작업 대상 리소스 유형 코드';
COMMENT ON COLUMN gw_adm_act_hst.trgt_id_val IS '관리자 작업 대상의 식별값';
COMMENT ON COLUMN gw_adm_act_hst.act_data IS 'Secret 원문을 제외하고 마스킹한 관리자 작업 상세 JSON';

COMMIT;

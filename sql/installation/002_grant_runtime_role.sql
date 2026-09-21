-- Genie Gateway 런타임 DB 역할 권한 부여
--
-- Lakebase:
--   CHANGE_ME_RUNTIME_ROLE을 Databricks Apps Environment의 PGUSER 값
--   (App 전용 Service Principal Client ID)으로 변경한다.
--
-- RDS/PostgreSQL:
--   CHANGE_ME_RUNTIME_ROLE을 Gateway 접속 DB 사용자명으로 변경한다.
--
-- 본 SQL은 genie_gw DDL 소유자 또는 권한 부여가 가능한 관리 계정으로 실행한다.

BEGIN;

CREATE TEMP TABLE tmp_genie_gw_runtime_role ON COMMIT DROP AS
SELECT 'CHANGE_ME_RUNTIME_ROLE'::TEXT AS role_nm;

DO $$
DECLARE
    target_role TEXT;
BEGIN
    SELECT role_nm
      INTO target_role
      FROM tmp_genie_gw_runtime_role;

    IF target_role IS NULL
       OR BTRIM(target_role) = ''
       OR target_role LIKE 'CHANGE_ME%' THEN
        RAISE EXCEPTION 'CHANGE_ME_RUNTIME_ROLE을 실제 Gateway DB 역할명으로 변경해야 합니다.';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = target_role) THEN
        RAISE EXCEPTION 'DB 역할 % 이(가) 존재하지 않습니다.', target_role;
    END IF;

    EXECUTE format('GRANT USAGE ON SCHEMA genie_gw TO %I', target_role);
    EXECUTE format(
        'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA genie_gw TO %I',
        target_role
    );
    EXECUTE format(
        'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA genie_gw TO %I',
        target_role
    );

    -- 같은 DDL 소유자가 이후 생성하는 테이블·시퀀스에도 런타임 권한을 자동 적용한다.
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES IN SCHEMA genie_gw '
        'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
        target_role
    );
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES IN SCHEMA genie_gw '
        'GRANT USAGE, SELECT ON SEQUENCES TO %I',
        target_role
    );
END
$$;

-- 권한 확인
SELECT
    r.role_nm,
    has_schema_privilege(r.role_nm, 'genie_gw', 'USAGE') AS schema_usage_yn,
    has_table_privilege(r.role_nm, 'genie_gw.ext_sys_mst', 'SELECT') AS table_select_yn,
    has_table_privilege(r.role_nm, 'genie_gw.api_req_hst', 'INSERT') AS audit_insert_yn
FROM tmp_genie_gw_runtime_role r;

COMMIT;

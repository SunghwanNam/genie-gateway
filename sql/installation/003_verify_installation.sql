-- Genie Gateway 공통 DDL 설치 결과 검증
-- PostgreSQL, RDS PostgreSQL, Lakebase에서 동일하게 실행 가능하다.

WITH expected_table(table_nm) AS (
    VALUES
        ('ext_sys_mst'),
        ('api_clnt_mst'),
        ('api_clnt_key_hst'),
        ('ai_agt_mst'),
        ('sys_ai_agt_map'),
        ('ai_cnvs_mst'),
        ('ai_run_mst'),
        ('ai_run_aprv_hst'),
        ('ai_run_item_hst'),
        ('ai_run_evt_hst'),
        ('genie_msg_hst'),
        ('api_req_hst'),
        ('genie_msg_atch_dtl'),
        ('genie_msg_stts_hst')
        ,('dbx_ws_mst')
        ,('gw_adm_usr_mst')
        ,('gw_adm_sess_hst')
        ,('gw_oper_cfg_mst')
        ,('gw_adm_act_hst')
),
actual_table AS (
    SELECT table_name AS table_nm
      FROM information_schema.tables
     WHERE table_schema = 'genie_gw'
       AND table_type = 'BASE TABLE'
)
SELECT
    e.table_nm,
    CASE WHEN a.table_nm IS NOT NULL THEN 'OK' ELSE 'MISSING' END AS install_stts_cd
FROM expected_table e
LEFT JOIN actual_table a ON a.table_nm = e.table_nm
ORDER BY e.table_nm;

-- 필수 테이블 누락 시 오류로 종료한다.
DO $$
DECLARE
    missing_tables TEXT;
BEGIN
    WITH expected_table(table_nm) AS (
        VALUES
            ('ext_sys_mst'),
            ('api_clnt_mst'),
            ('api_clnt_key_hst'),
            ('ai_agt_mst'),
            ('sys_ai_agt_map'),
            ('ai_cnvs_mst'),
            ('ai_run_mst'),
            ('ai_run_aprv_hst'),
            ('ai_run_item_hst'),
            ('ai_run_evt_hst'),
            ('genie_msg_hst'),
            ('api_req_hst'),
            ('genie_msg_atch_dtl'),
            ('genie_msg_stts_hst')
            ,('dbx_ws_mst')
            ,('gw_adm_usr_mst')
            ,('gw_adm_sess_hst')
            ,('gw_oper_cfg_mst')
            ,('gw_adm_act_hst')
    )
    SELECT STRING_AGG(e.table_nm, ', ' ORDER BY e.table_nm)
      INTO missing_tables
      FROM expected_table e
     WHERE to_regclass(format('genie_gw.%I', e.table_nm)) IS NULL;

    IF missing_tables IS NOT NULL THEN
        RAISE EXCEPTION 'Genie Gateway 필수 테이블 누락: %', missing_tables;
    END IF;
END
$$;

DO $$
DECLARE
    missing_columns TEXT;
BEGIN
    WITH expected_column(column_nm) AS (
        VALUES ('execution_owner_id'), ('lease_expires_dt'), ('heartbeat_dt')
    )
    SELECT STRING_AGG(e.column_nm, ', ' ORDER BY e.column_nm)
      INTO missing_columns
      FROM expected_column e
     WHERE NOT EXISTS (
         SELECT 1
           FROM information_schema.columns c
          WHERE c.table_schema = 'genie_gw'
            AND c.table_name = 'ai_run_mst'
            AND c.column_name = e.column_nm
     );

    IF missing_columns IS NOT NULL THEN
        RAISE EXCEPTION 'ai_run_mst 필수 컬럼 누락: %', missing_columns;
    END IF;
END
$$;

SELECT
    'genie_gw' AS schema_nm,
    COUNT(*) AS table_cnt,
    'INSTALLATION_OK' AS install_stts_cd
FROM information_schema.tables
WHERE table_schema = 'genie_gw'
  AND table_type = 'BASE TABLE';

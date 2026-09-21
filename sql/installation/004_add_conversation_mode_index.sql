SET search_path TO genie_gw, public;

CREATE INDEX IF NOT EXISTS ix_genie_cnvs_mst_04
    ON genie_cnvs_mst (api_clnt_id, usr_id, agt_id, cnvs_mode_cd, last_msg_dt DESC)
    WHERE dbx_cnvs_id IS NOT NULL AND use_yn = 'Y';

CREATE INDEX IF NOT EXISTS ix_genie_cnvs_mst_05
    ON genie_cnvs_mst (api_clnt_id, usr_id, agt_id, last_msg_dt DESC)
    WHERE dbx_cnvs_id IS NOT NULL AND use_yn = 'Y';

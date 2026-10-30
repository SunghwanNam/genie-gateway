"""Admin Web 운영 기준정보 Repository."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from app.core.database import Database


class AdminRepository:
    def __init__(self, database: Database, schema: str) -> None:
        if re.fullmatch(r"[a-z_][a-z0-9_]*", schema) is None:
            raise ValueError("invalid schema")
        self._database, self._schema = database, schema

    async def admin_exists(self) -> bool:
        async with self._database.session() as s:
            return bool(await s.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {self._schema}.gw_adm_usr_mst)")))

    async def create_admin(self, password_hash: str) -> None:
        async with self._database.session() as s:
            await s.execute(text(f"INSERT INTO {self._schema}.gw_adm_usr_mst(adm_id,pwd_hsh_val,reg_id) VALUES ('admin',:h,'SETUP')"), {"h": password_hash})

    async def admin_password_hash(self) -> str | None:
        async with self._database.session() as s:
            return await s.scalar(text(f"SELECT pwd_hsh_val FROM {self._schema}.gw_adm_usr_mst WHERE adm_id='admin' AND use_yn='Y'"))

    async def create_session(self, token: str, ttl: int, source_ip: str | None) -> None:
        h=hashlib.sha256(token.encode()).hexdigest()
        end=datetime.now(timezone.utc)+timedelta(seconds=ttl)
        async with self._database.session() as s:
            await s.execute(text(f"""INSERT INTO {self._schema}.gw_adm_sess_hst(adm_usr_id,sess_tkn_hsh_val,eff_end_dt,src_ip_val)
            SELECT adm_usr_id,:h,:e,CAST(:ip AS INET) FROM {self._schema}.gw_adm_usr_mst WHERE adm_id='admin'"""), {"h":h,"e":end,"ip":source_ip})
            await s.execute(text(f"UPDATE {self._schema}.gw_adm_usr_mst SET last_lgn_dt=CURRENT_TIMESTAMP WHERE adm_id='admin'"))

    async def valid_session(self, token: str) -> bool:
        h=hashlib.sha256(token.encode()).hexdigest()
        async with self._database.session() as s:
            return bool(await s.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {self._schema}.gw_adm_sess_hst x JOIN {self._schema}.gw_adm_usr_mst u USING(adm_usr_id) WHERE x.sess_tkn_hsh_val=:h AND x.revk_yn='N' AND x.eff_end_dt>CURRENT_TIMESTAMP AND u.use_yn='Y')"), {"h":h}))

    async def revoke_session(self, token: str) -> None:
        h=hashlib.sha256(token.encode()).hexdigest()
        async with self._database.session() as s:
            await s.execute(text(f"UPDATE {self._schema}.gw_adm_sess_hst SET revk_yn='Y' WHERE sess_tkn_hsh_val=:h"),{"h":h})

    async def dashboard(self) -> dict[str, Any]:
        async with self._database.session() as s:
            row=(await s.execute(text(f"""SELECT
              (SELECT count(*) FROM {self._schema}.ext_sys_mst) systems,
              (SELECT count(*) FROM {self._schema}.ai_agt_mst) agents,
              (SELECT count(*) FROM {self._schema}.api_clnt_key_hst WHERE revk_yn='N') api_keys"""))).mappings().one()
        return dict(row)

    async def supervisor_limits(self, defaults: dict[str, int]) -> dict[str, int]:
        keys = tuple(defaults)
        async with self._database.session() as s:
            rows = (
                await s.execute(
                    text(f"SELECT cfg_key_nm,cfg_val_cn FROM {self._schema}.gw_oper_cfg_mst WHERE cfg_key_nm = ANY(CAST(:keys AS VARCHAR[]))"),
                    {"keys": list(keys)},
                )
            ).mappings().all()
        result = dict(defaults)
        for row in rows:
            try:
                value = int(row["cfg_val_cn"])
                if value > 0:
                    result[row["cfg_key_nm"]] = value
            except (TypeError, ValueError):
                continue
        return result

    async def update_supervisor_limits(self, values: dict[str, int]) -> None:
        async with self._database.session() as s:
            for key, value in values.items():
                await s.execute(
                    text(f"""INSERT INTO {self._schema}.gw_oper_cfg_mst(cfg_key_nm,cfg_val_cn,mod_id)
                              VALUES(:key,:value,'admin')
                              ON CONFLICT(cfg_key_nm) DO UPDATE
                              SET cfg_val_cn=EXCLUDED.cfg_val_cn,mod_dt=CURRENT_TIMESTAMP,mod_id='admin'"""),
                    {"key": key, "value": str(value)},
                )

    async def list_rows(self, kind: str, include_inactive: bool = False) -> list[dict[str, Any]]:
        if kind == "agents":
            query = text(f"""SELECT a.agt_id AS agent_id,s.sys_cd AS system_code,a.agt_nm AS agent_name,
              a.agt_typ_cd AS agent_type,a.agt_mode_cd AS agent_mode,
              a.dbx_space_id AS databricks_space_id,a.dbx_agent_id AS databricks_agent_id,
              a.dbx_serving_ep_nm AS serving_endpoint_name,a.long_task_use_yn AS long_task_enabled,
              a.first_event_timeout_sec AS first_event_timeout_seconds,
              a.idle_timeout_sec AS idle_timeout_seconds,a.total_timeout_sec AS total_timeout_seconds,
              a.max_continue_cnt AS max_continue_count,a.max_active_run_cnt AS max_active_run_count,
              CASE WHEN a.use_yn='Y' AND m.use_yn='Y'
                     AND m.eff_strt_dt<=CURRENT_TIMESTAMP
                     AND (m.eff_end_dt IS NULL OR m.eff_end_dt>CURRENT_TIMESTAMP)
                   THEN 'Y' ELSE 'N' END AS enabled,
              m.dflt_yn AS is_default,
              CASE WHEN a.use_yn='N' OR m.use_yn='N' OR m.eff_end_dt<=CURRENT_TIMESTAMP
                   THEN COALESCE(m.eff_end_dt,GREATEST(COALESCE(a.mod_dt,a.reg_dt),COALESCE(m.mod_dt,m.reg_dt)))
                   ELSE NULL END AS deleted_at
              FROM {self._schema}.ai_agt_mst a
              JOIN {self._schema}.sys_ai_agt_map m ON m.agt_id=a.agt_id
              JOIN {self._schema}.ext_sys_mst s ON s.sys_id=m.sys_id
              WHERE CAST(:include_inactive AS BOOLEAN)
                 OR (a.use_yn='Y' AND m.use_yn='Y'
                     AND m.eff_strt_dt<=CURRENT_TIMESTAMP
                     AND (m.eff_end_dt IS NULL OR m.eff_end_dt>CURRENT_TIMESTAMP))
              ORDER BY s.sys_cd,m.dflt_yn DESC,m.route_prty_no,a.agt_nm""")
            async with self._database.session() as s:
                rows=(await s.execute(query,{"include_inactive":include_inactive})).mappings().all()
            return [dict(r) for r in rows]
        queries={
            "systems": f"SELECT s.sys_cd AS system_code,s.sys_nm AS system_name,s.use_yn AS enabled FROM {self._schema}.ext_sys_mst s ORDER BY s.sys_cd",
            "clients": f"SELECT c.api_clnt_id,s.sys_cd,c.api_clnt_cd,c.api_clnt_nm,c.auth_typ_cd,c.oauth_prnc_id,c.use_yn FROM {self._schema}.api_clnt_mst c JOIN {self._schema}.ext_sys_mst s USING(sys_id) ORDER BY s.sys_cd,c.api_clnt_cd",
            "workspaces": f"SELECT ws_id,ws_cd,ws_nm,ws_url,dflt_yn,use_yn FROM {self._schema}.dbx_ws_mst ORDER BY ws_cd",
            "routes": f"SELECT m.sys_genie_agt_map_id,s.sys_cd,a.agt_cd,m.route_prty_no,m.dflt_yn,m.use_yn FROM {self._schema}.sys_ai_agt_map m JOIN {self._schema}.ext_sys_mst s USING(sys_id) JOIN {self._schema}.ai_agt_mst a USING(agt_id) ORDER BY s.sys_cd,m.route_prty_no",
        }
        async with self._database.session() as s:
            rows=(await s.execute(text(queries[kind]))).mappings().all()
        return [dict(r) for r in rows]

    async def create_system(self, p: dict[str, Any]) -> None:
        async with self._database.session() as s:
            await s.execute(text(f"INSERT INTO {self._schema}.ext_sys_mst(sys_cd,sys_nm,sys_desc_cn,use_yn,reg_id) VALUES(:code,:name,:description,'Y','admin')"),p)

    async def register_system(self, p: dict[str, Any]) -> None:
        """시스템과 시스템 전용 API 클라이언트를 한 트랜잭션으로 생성한다."""
        async with self._database.session() as s:
            await s.execute(text(f"""INSERT INTO {self._schema}.ext_sys_mst(sys_cd,sys_nm,use_yn,reg_id)
              VALUES(:code,:name,'Y','admin') ON CONFLICT(sys_cd) DO UPDATE SET sys_nm=EXCLUDED.sys_nm,use_yn='Y',mod_dt=CURRENT_TIMESTAMP,mod_id='admin'"""),p)
            await s.execute(text(f"""INSERT INTO {self._schema}.api_clnt_mst(sys_id,api_clnt_cd,api_clnt_nm,auth_typ_cd,oauth_prnc_id,use_yn,reg_id)
              SELECT sys_id,:code||'_API',:name||' API','API_KEY',NULL,'Y','admin' FROM {self._schema}.ext_sys_mst WHERE sys_cd=:code
              ON CONFLICT(sys_id,api_clnt_cd) DO UPDATE SET api_clnt_nm=EXCLUDED.api_clnt_nm,auth_typ_cd='API_KEY',oauth_prnc_id=NULL,use_yn='Y',mod_dt=CURRENT_TIMESTAMP,mod_id='admin'"""),p)

    async def register_agent_for_system(self, p: dict[str, Any]) -> None:
        """선택한 Genie API 모드의 실제 ID와 시스템 기본 라우트를 함께 생성한다."""
        async with self._database.session() as s:
            system_id=await s.scalar(text(f"SELECT sys_id FROM {self._schema}.ext_sys_mst WHERE sys_cd=:system_code AND use_yn='Y'"),p)
            if system_id is None:
                raise ValueError("System not found")
            agent_id=await s.scalar(text(f"""INSERT INTO {self._schema}.ai_agt_mst(
                agt_cd,agt_nm,agt_typ_cd,agt_mode_cd,dbx_space_id,dbx_agent_id,dbx_serving_ep_nm,
                long_task_use_yn,first_event_timeout_sec,idle_timeout_sec,total_timeout_sec,
                max_continue_cnt,max_active_run_cnt,use_yn,reg_id)
              VALUES(:agent_code,:name,:agent_type,:mode,:space_id,:dbx_agent_id,:serving_endpoint_name,
                CASE WHEN :long_task_enabled THEN 'Y' ELSE 'N' END,:first_event_timeout_seconds,
                :idle_timeout_seconds,:total_timeout_seconds,:max_continue_count,:max_active_run_count,'Y','admin')
              ON CONFLICT(agt_cd) DO UPDATE SET agt_nm=EXCLUDED.agt_nm,agt_typ_cd=EXCLUDED.agt_typ_cd,
                agt_mode_cd=EXCLUDED.agt_mode_cd,dbx_space_id=EXCLUDED.dbx_space_id,
                dbx_agent_id=EXCLUDED.dbx_agent_id,dbx_serving_ep_nm=EXCLUDED.dbx_serving_ep_nm,
                long_task_use_yn=EXCLUDED.long_task_use_yn,first_event_timeout_sec=EXCLUDED.first_event_timeout_sec,
                idle_timeout_sec=EXCLUDED.idle_timeout_sec,total_timeout_sec=EXCLUDED.total_timeout_sec,
                max_continue_cnt=EXCLUDED.max_continue_cnt,max_active_run_cnt=EXCLUDED.max_active_run_cnt,
                use_yn='Y',mod_dt=CURRENT_TIMESTAMP,mod_id='admin' RETURNING agt_id"""),p)
            await s.execute(text(f"UPDATE {self._schema}.sys_ai_agt_map SET dflt_yn='N',mod_dt=CURRENT_TIMESTAMP,mod_id='admin' WHERE sys_id=:system_id AND dflt_yn='Y' AND use_yn='Y'"),{"system_id":system_id})
            updated=(await s.execute(text(f"""UPDATE {self._schema}.sys_ai_agt_map SET route_prty_no=1,dflt_yn='Y',use_yn='Y',eff_end_dt=NULL,mod_dt=CURRENT_TIMESTAMP,mod_id='admin'
              WHERE sys_id=:system_id AND agt_id=:agent_id RETURNING sys_genie_agt_map_id"""),{"system_id":system_id,"agent_id":agent_id})).scalar_one_or_none()
            if updated is not None:
                return
            await s.execute(text(f"""INSERT INTO {self._schema}.sys_ai_agt_map(sys_id,agt_id,route_prty_no,dflt_yn,use_yn,reg_id)
              VALUES(:system_id,:agent_id,1,'Y','Y','admin')"""),{"system_id":system_id,"agent_id":agent_id})

    async def update_agent(self, agent_id: int, p: dict[str, Any]) -> bool:
        """에이전트 표시명·API 모드·실제 ID·연결 시스템을 수정한다."""
        async with self._database.session() as s:
            system_id=await s.scalar(text(f"SELECT sys_id FROM {self._schema}.ext_sys_mst WHERE sys_cd=:system_code AND use_yn='Y'"),p)
            if system_id is None:
                raise ValueError("System not found")
            updated=(await s.execute(text(f"""UPDATE {self._schema}.ai_agt_mst SET agt_nm=:name,agt_typ_cd=:agent_type,agt_mode_cd=:mode,dbx_space_id=:space_id,dbx_agent_id=:dbx_agent_id,dbx_serving_ep_nm=:serving_endpoint_name,
                long_task_use_yn=CASE WHEN :long_task_enabled THEN 'Y' ELSE 'N' END,first_event_timeout_sec=:first_event_timeout_seconds,idle_timeout_sec=:idle_timeout_seconds,total_timeout_sec=:total_timeout_seconds,max_continue_cnt=:max_continue_count,max_active_run_cnt=:max_active_run_count,use_yn='Y',mod_dt=CURRENT_TIMESTAMP,mod_id='admin'
              WHERE agt_id=:row_agent_id RETURNING agt_id"""),{**p,"row_agent_id":agent_id})).scalar_one_or_none()
            if updated is None:
                return False
            await s.execute(text(f"UPDATE {self._schema}.sys_ai_agt_map SET dflt_yn='N',mod_dt=CURRENT_TIMESTAMP,mod_id='admin' WHERE sys_id=:system_id AND agt_id<>:agent_id AND dflt_yn='Y' AND use_yn='Y'"),{"system_id":system_id,"agent_id":agent_id})
            await s.execute(text(f"""UPDATE {self._schema}.sys_ai_agt_map SET sys_id=:system_id,dflt_yn='Y',use_yn='Y',eff_end_dt=NULL,mod_dt=CURRENT_TIMESTAMP,mod_id='admin'
              WHERE agt_id=:agent_id"""),{"system_id":system_id,"agent_id":agent_id})
            return True

    async def deactivate_agent(self, agent_id: int, system_code: str) -> bool:
        """시스템 매핑을 종료하고 다른 활성 매핑이 없는 Agent를 비활성화한다."""
        async with self._database.session() as s:
            mapping=(await s.execute(text(f"""SELECT m.sys_id,m.dflt_yn
              FROM {self._schema}.sys_ai_agt_map m
              JOIN {self._schema}.ext_sys_mst x ON x.sys_id=m.sys_id
              WHERE m.agt_id=:agent_id AND x.sys_cd=:system_code
              FOR UPDATE OF m"""),{"agent_id":agent_id,"system_code":system_code})).mappings().one_or_none()
            if mapping is None:
                return False
            await s.execute(text(f"""UPDATE {self._schema}.sys_ai_agt_map
              SET use_yn='N',dflt_yn='N',eff_end_dt=COALESCE(eff_end_dt,CURRENT_TIMESTAMP),
                  mod_dt=CURRENT_TIMESTAMP,mod_id='admin'
              WHERE sys_id=:system_id AND agt_id=:agent_id"""),
              {"system_id":mapping["sys_id"],"agent_id":agent_id})
            if mapping["dflt_yn"] == "Y":
                replacement=await s.scalar(text(f"""SELECT m.agt_id
                  FROM {self._schema}.sys_ai_agt_map m
                  JOIN {self._schema}.ai_agt_mst a ON a.agt_id=m.agt_id
                  WHERE m.sys_id=:system_id AND m.agt_id<>:agent_id
                    AND m.use_yn='Y' AND a.use_yn='Y'
                    AND m.eff_strt_dt<=CURRENT_TIMESTAMP
                    AND (m.eff_end_dt IS NULL OR m.eff_end_dt>CURRENT_TIMESTAMP)
                  ORDER BY m.route_prty_no,a.agt_id LIMIT 1"""),
                  {"system_id":mapping["sys_id"],"agent_id":agent_id})
                if replacement is not None:
                    await s.execute(text(f"""UPDATE {self._schema}.sys_ai_agt_map
                      SET dflt_yn='Y',mod_dt=CURRENT_TIMESTAMP,mod_id='admin'
                      WHERE sys_id=:system_id AND agt_id=:agent_id"""),
                      {"system_id":mapping["sys_id"],"agent_id":replacement})
            has_active_mapping=bool(await s.scalar(text(f"""SELECT EXISTS(
              SELECT 1 FROM {self._schema}.sys_ai_agt_map
              WHERE agt_id=:agent_id AND use_yn='Y'
                AND eff_strt_dt<=CURRENT_TIMESTAMP
                AND (eff_end_dt IS NULL OR eff_end_dt>CURRENT_TIMESTAMP))"""),{"agent_id":agent_id}))
            if not has_active_mapping:
                await s.execute(text(f"""UPDATE {self._schema}.ai_agt_mst
                  SET use_yn='N',mod_dt=CURRENT_TIMESTAMP,mod_id='admin'
                  WHERE agt_id=:agent_id"""),{"agent_id":agent_id})
            return True

    async def restore_agent(self, agent_id: int, system_code: str) -> bool:
        """비활성 Agent와 시스템 매핑을 복구하고 종료 시각을 제거한다."""
        async with self._database.session() as s:
            mapping=(await s.execute(text(f"""SELECT m.sys_id
              FROM {self._schema}.sys_ai_agt_map m
              JOIN {self._schema}.ext_sys_mst x ON x.sys_id=m.sys_id
              WHERE m.agt_id=:agent_id AND x.sys_cd=:system_code
              FOR UPDATE OF m"""),{"agent_id":agent_id,"system_code":system_code})).mappings().one_or_none()
            if mapping is None:
                return False
            has_default=bool(await s.scalar(text(f"""SELECT EXISTS(
              SELECT 1 FROM {self._schema}.sys_ai_agt_map
              WHERE sys_id=:system_id AND dflt_yn='Y' AND use_yn='Y'
                AND eff_strt_dt<=CURRENT_TIMESTAMP
                AND (eff_end_dt IS NULL OR eff_end_dt>CURRENT_TIMESTAMP))"""),
              {"system_id":mapping["sys_id"]}))
            await s.execute(text(f"""UPDATE {self._schema}.ai_agt_mst
              SET use_yn='Y',mod_dt=CURRENT_TIMESTAMP,mod_id='admin'
              WHERE agt_id=:agent_id"""),{"agent_id":agent_id})
            await s.execute(text(f"""UPDATE {self._schema}.sys_ai_agt_map
              SET use_yn='Y',dflt_yn=:default_yn,eff_end_dt=NULL,
                  mod_dt=CURRENT_TIMESTAMP,mod_id='admin'
              WHERE sys_id=:system_id AND agt_id=:agent_id"""),
              {"system_id":mapping["sys_id"],"agent_id":agent_id,"default_yn":"N" if has_default else "Y"})
            return True

    async def client_id_for_system(self, system_code: str) -> int | None:
        async with self._database.session() as s:
            value=await s.scalar(text(f"SELECT c.api_clnt_id FROM {self._schema}.api_clnt_mst c JOIN {self._schema}.ext_sys_mst x USING(sys_id) WHERE x.sys_cd=:code AND c.use_yn='Y' ORDER BY c.api_clnt_id LIMIT 1"),{"code":system_code})
        return int(value) if value is not None else None

    async def create_client(self, p: dict[str, Any]) -> None:
        async with self._database.session() as s:
            await s.execute(text(f"""INSERT INTO {self._schema}.api_clnt_mst(sys_id,api_clnt_cd,api_clnt_nm,auth_typ_cd,oauth_prnc_id,use_yn,reg_id)
              SELECT sys_id,:code,:name,:auth_type,:oauth_principal,'Y','admin' FROM {self._schema}.ext_sys_mst WHERE sys_cd=:system_code"""),p)

    async def create_workspace(self, p: dict[str, Any]) -> None:
        async with self._database.session() as s:
            await s.execute(text(f"INSERT INTO {self._schema}.dbx_ws_mst(ws_cd,ws_nm,ws_url,dflt_yn,use_yn,reg_id) VALUES(:code,:name,:url,:is_default,'Y','admin')"),p)

    async def create_agent(self, p: dict[str, Any]) -> None:
        async with self._database.session() as s:
            await s.execute(text(f"""INSERT INTO {self._schema}.ai_agt_mst(agt_cd,agt_nm,agt_mode_cd,ws_id,dbx_space_id,dbx_agent_id,use_yn,reg_id)
              SELECT :code,:name,:mode,w.ws_id,:space_id,:agent_id,'Y','admin' FROM (SELECT 1) x LEFT JOIN {self._schema}.dbx_ws_mst w ON w.ws_cd=:workspace_code"""),p)

    async def create_route(self, p: dict[str, Any]) -> None:
        async with self._database.session() as s:
            await s.execute(text(f"""INSERT INTO {self._schema}.sys_ai_agt_map(sys_id,agt_id,route_prty_no,dflt_yn,use_yn,reg_id)
              SELECT s.sys_id,a.agt_id,:priority,:is_default,'Y','admin' FROM {self._schema}.ext_sys_mst s CROSS JOIN {self._schema}.ai_agt_mst a WHERE s.sys_cd=:system_code AND a.agt_cd=:agent_code"""),p)

    async def issue_key(self, client_id: int, key_hash: str, encrypted: str, nonce: str, hint: str) -> int:
        async with self._database.session() as s:
            return int(await s.scalar(text(f"""INSERT INTO {self._schema}.api_clnt_key_hst(api_clnt_id,key_hsh_val,key_enc_val,key_nonce_val,enc_key_ver_no,key_hint_cn,reg_id)
              VALUES(:cid,:h,:e,:n,1,:hint,'admin') RETURNING api_clnt_key_id"""),{"cid":client_id,"h":key_hash,"e":encrypted,"n":nonce,"hint":hint}))

    async def encrypted_key(self, key_id: int) -> tuple[str,str] | None:
        async with self._database.session() as s:
            row=(await s.execute(text(f"SELECT key_enc_val,key_nonce_val FROM {self._schema}.api_clnt_key_hst WHERE api_clnt_key_id=:id AND revk_yn='N'"),{"id":key_id})).mappings().one_or_none()
        return (row["key_enc_val"],row["key_nonce_val"]) if row and row["key_enc_val"] and row["key_nonce_val"] else None

    async def list_keys(self) -> list[dict[str, Any]]:
        async with self._database.session() as s:
            rows=(await s.execute(text(f"""SELECT k.api_clnt_key_id,s.sys_cd AS system_code,k.key_hint_cn,k.eff_strt_dt,k.eff_end_dt,k.revk_yn,
              k.key_enc_val,k.key_nonce_val,(k.key_enc_val IS NOT NULL) recoverable_yn FROM {self._schema}.api_clnt_key_hst k JOIN {self._schema}.api_clnt_mst c USING(api_clnt_id) JOIN {self._schema}.ext_sys_mst s USING(sys_id)
              ORDER BY k.reg_dt DESC"""))).mappings().all()
        return [dict(r) for r in rows]

    async def revoke_key(self, key_id: int) -> None:
        async with self._database.session() as s:
            await s.execute(text(f"UPDATE {self._schema}.api_clnt_key_hst SET revk_yn='Y',mod_dt=CURRENT_TIMESTAMP,mod_id='admin' WHERE api_clnt_key_id=:id"),{"id":key_id})

    async def audit(self, action: str, target_type: str, target_id: str, source_ip: str | None) -> None:
        async with self._database.session() as s:
            await s.execute(text(f"""INSERT INTO {self._schema}.gw_adm_act_hst(adm_usr_id,act_typ_cd,trgt_typ_cd,trgt_id_val,src_ip_val)
              SELECT adm_usr_id,:a,:t,:i,CAST(:ip AS INET) FROM {self._schema}.gw_adm_usr_mst WHERE adm_id='admin'"""),{"a":action,"t":target_type,"i":target_id,"ip":source_ip})

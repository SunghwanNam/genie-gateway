-- 기존 단일 관리자 ID를 shnam에서 admin으로 변경한다.
-- 테이블 구조와 PK/FK는 변경하지 않으며, 기존 비밀번호·세션·감사 이력을 유지한다.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM genie_gw.gw_adm_usr_mst
         WHERE adm_id = 'admin'
    ) AND EXISTS (
        SELECT 1
          FROM genie_gw.gw_adm_usr_mst
         WHERE adm_id = 'shnam'
    ) THEN
        RAISE EXCEPTION 'Both admin and shnam administrator rows exist; resolve the duplicate before migration';
    END IF;

    UPDATE genie_gw.gw_adm_usr_mst
       SET adm_id = 'admin'
     WHERE adm_id = 'shnam';
END
$$;

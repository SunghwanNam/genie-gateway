-- 개발·검증 환경 전용 초기화 DDL
--
-- 주의: genie_gw 스키마와 그 안의 테이블, 데이터, 인덱스, 제약조건을 모두 삭제한다.
-- 운영·공용 Lakebase에서는 실행하지 않는다.
-- 실행 후 001_create_genie_gw_schema.sql을 실행하여 최신 스키마를 새로 생성한다.

BEGIN;

DROP SCHEMA IF EXISTS genie_gw CASCADE;

COMMIT;

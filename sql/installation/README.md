# Genie Gateway DB 설치

PostgreSQL, Amazon RDS for PostgreSQL, Databricks Lakebase는 동일한 `genie_gw` 스키마를 사용합니다.

## 신규 설치 순서

1. `001_create_genie_gw_schema.sql`
2. `002_grant_runtime_role.sql`의 `CHANGE_ME_RUNTIME_ROLE` 변경 후 실행
3. `003_verify_installation.sql`

`001_create_genie_gw_schema.sql`에는 Supervisor Run 테이블과 복수 instance lease 컬럼까지
현재 버전의 전체 구조가 포함되어 있다. 빈 DB에서는 `004`~`007`을 추가 실행하지 않는다.

## 개발·검증 환경 전체 초기화

기존 데이터를 모두 버리고 최신 구조로 다시 만들 때만 다음 순서로 실행한다.

1. `000_reset_genie_gw_schema_DEVELOPMENT_ONLY.sql`
2. `001_create_genie_gw_schema.sql`
3. `002_grant_runtime_role.sql`의 역할명 변경 후 실행
4. `003_verify_installation.sql`

`000_reset_genie_gw_schema_DEVELOPMENT_ONLY.sql`은 `genie_gw` 스키마를 `CASCADE`로 삭제한다.
운영·공용 Lakebase 및 보존할 데이터가 있는 환경에서는 실행하지 않는다.

## Lakebase

Lakebase App Resource가 생성한 App SP 역할명을 Databricks Apps Environment의 `PGUSER`에서 확인하고 `002_grant_runtime_role.sql`에 입력합니다. DDL과 권한 SQL은 스키마 생성 및 권한 부여가 가능한 관리 사용자로 실행합니다.

## 운영 기준정보

설치 SQL은 Gateway 기동에 필요한 스키마, 테이블 및 DB 권한만 구성하며 운영 기준정보를 INSERT하지 않습니다. 다음 정보는 설치 후 Admin Web에서 등록·변경합니다.

- 외부 연계 시스템 및 API 클라이언트
- X-API-Key 발급·조회·폐기 정보
- Databricks Workspace 및 Genie Agent
- 시스템별 Agent 라우팅

`gw_oper_cfg_mst`는 Admin Web의 Gateway 운영 설정 저장소입니다. Supervisor 전체·사용자·사용자별 동일 Agent 활성 Run 한도는 이 테이블 값을 새 Run 생성 시 읽어 즉시 적용합니다. 비밀번호·Token·Secret 원문은 저장하지 않습니다.

기존 `sql/configuration`, `sql/credentials` 수동 SQL은 Admin Web으로 대체되어 배포본에서 제거했습니다.

# 기존 설치 환경 업데이트

기존 Genie Gateway 데이터와 PK/FK를 보존해야 하는 DB에는 다음 순서로 적용한다.

1. `004_add_conversation_mode_index.sql`
2. `005_add_supervisor_agent_foundation.sql`
3. `006_add_run_execution_lease.sql`
4. `007_add_supervisor_approval.sql`
5. `008_rename_default_admin.sql`
6. `002_grant_runtime_role.sql` 재실행
7. `003_verify_installation.sql`

`005_add_supervisor_agent_foundation.sql`은 기존 PK·FK·데이터를 유지하면서 Agent·Conversation
Master를 `ai_*` 공통 명칭으로 일반화하고 Supervisor 정책 컬럼과 Run·Item·Event 테이블을
추가한다. 이 migration과 공통 테이블명을 사용하는 애플리케이션 버전은 같은 배포 단위로
적용해야 한다.

`006_add_run_execution_lease.sql`은 복수 Gateway instance의 Supervisor 중복 실행을 막기 위한
실행 소유자, lease 만료와 heartbeat 컬럼을 추가한다. 해당 애플리케이션 버전 배포 전에
적용해야 한다.

`007_add_supervisor_approval.sql`은 `WAITING_APPROVAL` Run 상태와 Web Search/MCP 승인 감사
테이블을 추가한다. 새 테이블 권한 적용을 위해 실제 Runtime 역할명으로 `002`를 다시 실행한
후 애플리케이션을 배포한다.

`008_rename_default_admin.sql`은 기존 단일 관리자 ID `shnam`을 `admin`으로 변경한다. 관리자
행의 PK는 유지되므로 기존 비밀번호, 세션 및 감사 이력은 보존된다. `admin`과 `shnam` 행이
동시에 존재하면 데이터 충돌을 피하기 위해 명시적인 오류로 중단한다.

`005`는 기존/공통 Agent·Conversation 테이블과 `api_req_hst`가 없으면 명시적인 오류로
중단한다. `006`은 `ai_run_mst`가 없으면 중단하며, 이 경우 신규 설치라면 `001`을 실행하고
기존 설치라면 `005`부터 실행한다. 두 migration은 데이터를 삭제하지 않는다.

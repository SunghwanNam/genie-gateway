# Genie Conversation Gateway 개발 명세

## 1. 문서 목적

이 문서는 Databricks Genie Conversation API 연동용 FastAPI Gateway의 현재 구현 기준과
후속 변경 원칙을 함께 관리하는 개발 명세다. 계획 문서가 아니라 실제 소스코드,
`app.yaml`, 설치 SQL과 테스트 동작을 기준으로 유지한다.

- 외부 시스템은 X-API-Key로 Gateway 인증, Databricks Apps에서는 플랫폼 OAuth 및 `CAN USE` 검증을 선행
- `source_system`, `user_id` 기반 호출 주체 구분
- 사용자별 `conversation_id` 소유권 저장 및 검증
- 시스템과 Databricks 실제 `space_id`/`agent_id`별 허용 라우팅
- Databricks 공식 Genie API 요청·응답 구조 최대한 유지
- 배포 환경에 따라 Lakebase 또는 PostgreSQL/RDS 사용

구현자는 변경에 앞서 기존 프로젝트 구조, API 경로, Databricks 클라이언트, 인증 처리,
설정 로딩 및 테스트 코드를 먼저 조사하고 재사용해야 한다. 기존 동작을 불필요하게
재작성하거나 프레임워크를 교체하지 않는다.

### 문서 상태 구분

- **구현 완료**: 현재 소스코드와 테스트에서 확인된 기능
- **후속 범위**: 설계 방향은 정했으나 아직 구현하지 않은 기능
- Supervisor Agent 연동은 별도
  [`supervisor_agent_gateway_implementation_spec.md`](supervisor_agent_gateway_implementation_spec.md)에서 관리

---

## 2. 확정 아키텍처

```text
외부 연계 시스템
  -> FastAPI Gateway
       1) 시스템 코드 및 사용자 식별값 검증
       2) 시스템과 Databricks 실제 space_id/agent_id별 허용 Route 확인
       3) 배포 환경에 설정된 Databricks SP 자격증명 선택
       4) conversation_id 소유권 저장 또는 검증
       5) 질문에 [user_id: <value>] 내부 컨텍스트 삽입
       6) Databricks 공식 Genie API 호출
  -> Genie Agent
  -> 내부 컨텍스트를 제거한 응답 반환
```

### 배포 대안

| 구분 | FastAPI | Conversation 저장소 |
|---|---|---|
| 방안 A | Databricks Apps | Lakebase |
| 방안 B | EC2 | Amazon RDS for PostgreSQL |

두 배포안의 릴리스 입력은 `deploy/package_release.sh <VERSION>`으로 함께 생성한다.
Databricks Apps에는 `databricks_app_gateway-<VERSION>.zip`의 최소 실행 구성을 사용하고,
EC2 실행 파일은 `genie-gateway-build-source-<VERSION>.tar.gz`를 대상과 동일한
Amazon Linux 2023 및 CPU 아키텍처에서 Nuitka로 빌드한다. 개발 PC의 OS에서 EC2용
실행 파일을 교차 빌드하지 않는다.
EC2의 서비스 계정과 systemd Unit은 `genie-gateway`, 설치 루트는
`/opt/genie-gateway`, 환경설정 디렉터리는 `/etc/genie-gateway`로 통일한다.

DB 접근 코드는 저장소 인터페이스로 분리하여 두 배포안에서 동일한 서비스 로직을 사용한다. EC2 내부에 PostgreSQL을 직접 설치하는 구성은 개발·검증 환경에서만 허용하고 운영 기본안으로 채택하지 않는다.

---

## 3. 구현 원칙

1. 기존 FastAPI와 Databricks 연동 코드를 최대한 재사용한다.
2. 외부 API는 Databricks 공식 Genie Conversation API 구조를 최대한 유지한다.
3. `source_system`, `user_id`는 게이트웨이 전용 Query Parameter다.
4. 게이트웨이 전용 필드는 Databricks Query Parameter나 동명 Body 필드로 전달하지 않는다. `user_id`는 Chat 질문과 Agent `input_text`의 내부 컨텍스트로만 삽입한다.
5. 사용자별 SP를 만들지 않는다. 현재 Apps 배포에서는 App 전용 SP를 상향 호출 주체로 사용한다.
6. `conversation_id`는 외부 시스템과 Databricks에서 공통 식별자로 사용한다. 별도 `room_id`를 만들지 않는다.
7. `user_id`는 표시용 사용자명보다 외부 시스템의 변경 불가능한 내부 사용자 고유 식별값을 사용한다.
8. 사용자 입력으로 받은 시스템 코드나 테이블명을 SQL 문자열에 직접 삽입하지 않는다.
9. 대화 소유권 검증 실패는 Databricks 호출 전에 `403 Forbidden`으로 종료한다.
10. 이번 범위에서 사용자별 Unity Catalog Row Filter/Column Mask 연동 및 OAuth U2M/OBO는 구현하지 않는다.

---

## 4. 보안 경계

현재 Databricks Apps 배포에서 Databricks가 인식하는 상향 호출 주체는 App 전용 SP다. 외부 시스템의 OAuth 주체와 최종 사용자 식별값은 Databricks 상향 호출 주체가 아니다. 외부 시스템이 인증된 사용자 세션에서 확정한 변경 불가능한 내부 사용자 고유 식별값은 Gateway의 Conversation 소유권과 Genie 질의 컨텍스트로 사용한다.

- `user_id`: 대화 소유권 검증, 감사 로그 및 Genie 질의 컨텍스트 용도
- `source_system`: 호출 자격증명에 귀속된 시스템과의 교차검증 및 Agent 라우팅 용도
- SP 권한: 배포 환경에 따라 허용된 Genie Agent, SQL Warehouse 및 UC 데이터 범위
- 개인별 데이터 행·컬럼 권한: 이번 구현 범위 아님

외부 시스템의 프런트엔드가 임의로 입력한 `user_id`를 신뢰하면 안 된다. `user_id`는 외부 시스템 백엔드가 로그인 세션에서 확정하여 서버 간 요청으로 전달해야 한다.

Databricks Apps 배포의 서버 간 호출은 외부 시스템 SP의 OAuth M2M Access Token과 Gateway가 발급한 시스템 전용 X-API-Key를 함께 사용한다. Apps 프록시는 `Authorization: Bearer` Token과 App `CAN USE` 권한을 검증하고, Gateway는 X-API-Key에 귀속된 시스템과 `source_system` 일치를 검증한다. OAuth만 통과했거나 X-API-Key만 제시한 요청은 허용하지 않는다. 일반 PostgreSQL/RDS에 독립 배포한 Gateway는 Apps 프록시가 없으므로 X-API-Key를 외부 진입 인증으로 사용한다.

요청의 `source_system`은 코드 테이블과 환경설정의 허용 목록에 모두 존재해야 하며, 호출 자격증명에 허용된 시스템과 일치해야 한다. 불일치 시 `403 Forbidden`을 반환한다.

---

## 5. 데이터 모델

### 5.1 Conversation 소유권 테이블

기존 `ai_cnvs_mst`의 `api_clnt_id`, `usr_id`, `agt_id`, `dbx_cnvs_id`를
Conversation 소유권 원장으로 재사용한다. 별도 소유권 테이블을 중복 생성하지 않는다.

소유권을 결정하는 실제 컬럼은 다음과 같다.

| 의미 | 실제 컬럼 |
|---|---|
| 인증된 외부 API 클라이언트 | `api_clnt_id` |
| 외부 시스템이 Query Parameter로 전달한 사용자 고유 식별값 | `usr_id` |
| 대화 시작 시 확정된 Gateway Agent | `agt_id` |
| Databricks가 발급한 Conversation ID | `dbx_cnvs_id` |
| 상태 및 사용 여부 | `cnvs_stts_cd`, `use_yn` |

동일한 `usr_id`는 여러 개의 `dbx_cnvs_id`를 가질 수 있다. 후속 질문 또는 조회 성공 시 `last_msg_dt`를 갱신한다.

### 5.2 시스템 및 API 클라이언트 테이블

- `ext_sys_mst`: `sys_cd`와 EC2용 `dbx_cred_ref_nm` 관리
- `api_clnt_mst`: 시스템별 API 클라이언트 관리. `oauth_prnc_id`는 OAuth 인증 모드를 선택할 경우에만 사용
- `api_clnt_key_hst`: 배포 환경 공통 X-API-Key 해시·유효기간·폐기 여부 관리

실제 Secret 원문은 DB에 저장하지 않고 `dbx_cred_ref_nm`에 환경변수 또는 Secrets Manager 참조만 저장한다.

### 5.3 Agent 라우팅 테이블

- `ai_agt_mst.dbx_space_id`: 외부 Chat mode 경로와 Databricks 상향 호출에 사용하는 실제 Space ID
- `ai_agt_mst.dbx_agent_id`: 외부 Agent mode 경로와 Databricks 상향 호출에 사용하는 실제 Agent ID
- `sys_ai_agt_map`: 인증 시스템과 허용 Space/Agent의 매핑
- `/custom/admin`의 에이전트 등록·수정 화면에서 `CHAT`, `AGENT`, `BOTH` 모드를 선택한다. `CHAT`은 Space ID, `AGENT`는 Agent ID, `BOTH`는 두 값을 모두 필수로 저장한다.
- `/custom/chat`은 활성 시스템-Agent 매핑을 드롭다운으로 제공한다. Agent 선택 시 새 대화 상태로 전환하고 해당 Agent의 대화 이력만 조회하며, 선택 Agent와 모드는 URL Query Parameter에 유지한다.
- 공개 Agent mode에는 `conversation_id`·`response_id` 기반 Cancel API가 구현되어 있으며
  Databricks 호출 전에 Conversation 소유권을 검증한다. 사용자 중단 UI를 구현할 때는
  `response.created`에서 두 식별자를 보관하고 동일한 `source_system`·`user_id`로 Cancel
  API를 먼저 호출한 뒤 SSE 연결을 닫아야 한다. 현재 `/custom/chat`에는 사용자 중단 버튼과
  이 Cancel 호출 연결이 아직 구현되어 있지 않다. 단순 연결 종료만으로 Databricks 작업
  취소를 보장하지 않는다.

현재 API에는 `domain_code` 파라미터가 없다. Chat mode는 `source_system + URL의 space_id`로 Route를 결정한다.

### 5.4 Message·Attachment·상태 이력 테이블

- `genie_msg_hst`: Chat Message 원본, 역할, 현재 상태 및 표시용 질문 저장
- `genie_msg_atch_dtl`: Text, Query, Visualization, Suggested Questions Attachment 정규화 저장
- `genie_msg_stts_hst`: Message 상태가 변경된 경우에만 상태 전이 추가
- `api_req_hst`: 요청 경로, 상태, 처리 시간, 오류와 선택적 Payload 감사 이력
- `AUDIT_PAYLOADS=false`이어도 정규화된 Chat Message 이력은 저장하고 중복 감사
  Payload JSON만 생략
- Agent mode item은 현재 별도 Message 테이블에 저장하지 않고 Databricks `/items`를 원본으로 사용

---

## 6. 환경설정

### 6.1 운영 설정 저장소

`genie_gw.gw_oper_cfg_mst`는 Admin Web에서 관리할 Gateway 런타임 운영 설정 저장소다. 현재는 Supervisor 전체·사용자·사용자별 동일 Agent 활성 Run 한도를 저장하며, 새 Run 생성 트랜잭션에서 조회해 즉시 적용한다.

- 사용 예정 위치: `app/repositories/admin_repository.py` 또는 별도 `OperatingConfigRepository`
- 변경 진입점: `/custom/admin` 운영 설정 API 및 화면
- 저장 대상: 기능 플래그, 제한값, 타임아웃 등 재기동 없이 관리할 필요가 있는 비민감 운영 값
- 저장 금지: 비밀번호, API Key 원문, OAuth Token, Client Secret 등 비밀정보
- 구현 시 고려사항: 허용 키 목록과 타입 검증, 기본값 처리, 변경 감사 기록(`gw_adm_act_hst`), 필요 시 캐시 무효화

예시:

```env
GATEWAY_AUTH_MODE=API_KEY
DATABRICKS_HOST=https://<workspace-host>
DATABASE_URL=postgresql+asyncpg://...
```

현재 주요 런타임 설정:

| 설정 | 의미 | 기본값 |
|---|---|---:|
| `CUSTOM_CHAT_UI_ENABLED` | `/custom/chat` 활성화 | `false` |
| `CUSTOM_CHAT_LOCAL_ONLY` | Custom Chat의 loopback 접근 제한 | `true` |
| `ADMIN_UI_ENABLED` | `/custom/admin` 활성화 | `true` |
| `AUDIT_PAYLOADS` | 요청·응답 Payload 감사 저장 | `false` |
| `UPSTREAM_CONNECT_TIMEOUT_SECONDS` | Databricks 연결 제한시간 | `10` |
| `UPSTREAM_READ_TIMEOUT_SECONDS` | Databricks 응답 읽기 제한시간 | `1860` |
| `UPSTREAM_RETRY_ATTEMPTS` | 제한적 상향 호출 재시도 횟수 | `2` |
| `SUPERVISOR_GATEWAY_MAX_ACTIVE_RUNS` | Gateway 전체 Supervisor 활성 Run 한도 | `40` |
| `SUPERVISOR_USER_MAX_ACTIVE_RUNS` | 사용자별 전체 Supervisor 활성 Run 한도 | `10` |
| `SUPERVISOR_USER_AGENT_MAX_ACTIVE_RUNS` | 사용자별 동일 Supervisor 활성 Run 한도 | `5` |
| `DATABASE_MODE` | `POSTGRES`, `RDS`, `RDS_IAM`, `LAKEBASE` | `POSTGRES` |

요구사항:

- 환경변수 변경 후 Gateway 재시작 또는 Databricks App 재배포로 반영 가능해야 한다.
- 실제 SP Client Secret은 Git, 소스코드 및 평문 설정 파일에 저장하지 않는다.
- EC2/RDS에서는 `GATEWAY_AUTH_MODE=API_KEY`와 시스템별 X-API-Key를 사용한다.
- EC2의 `ext_sys_mst.dbx_cred_ref_nm`은 `env:<PREFIX>` 또는 `secretsmanager:<secret-id>` 참조를 사용한다.
- Databricks Apps에서는 현재 `GATEWAY_AUTH_MODE=API_KEY`를 사용한다. Apps 프록시가 외부 SP의 OAuth와 App `CAN USE` 권한을 먼저 검증하고, Gateway는 X-API-Key와 `source_system` 일치를 추가 검증한다.
- Databricks Apps의 상향 Genie 호출에는 Apps가 주입한 App 전용 SP 자격증명을 사용한다.
- Databricks Apps 외부 호출자는 별도의 외부 시스템 SP에 App `CAN USE` 권한을 부여받고, Workspace `/oidc/v1/token`에서 OAuth M2M access token을 발급하여 `Authorization: Bearer`로 전달한다. `client_credentials` 방식은 refresh token 대신 `expires_in` 만료 전에 Token Endpoint를 다시 호출한다.
- 운영 전환 기준으로 외부 시스템은 위 OAuth Bearer Token과 Gateway가 발급한 `X-API-Key`를 모든 Apps API 요청에 함께 전달한다.
- Apps 연계 대상 시스템은 자체 SP Client ID/Secret으로 OAuth M2M Token을 발급·갱신하고 `Authorization: Bearer` 헤더에 전달한다. Gateway Admin에는 OAuth Token이나 Client Secret을 등록하지 않는다.
- Gateway 관리자는 연계 대상 SP에 App `CAN USE` 권한을 부여하고, Gateway Admin에서 해당 시스템·API 클라이언트·X-API-Key·허용 Agent Route를 등록한다.
- EC2에서는 AWS Secrets Manager 또는 Systems Manager Parameter Store 사용을 우선하되, 시스템별 SP 저장·접근권한·교체·장애 대응 방식은 운영 관점에서 추후 협의한다.
- EC2/API_KEY 모드의 외부 요청은 `X-API-Key`가 필수이며 Gateway Bearer 인증은 제공하지 않는다.
- Databricks PAT 및 SP 자격증명은 Gateway의 상향 호출 전용이며 외부 시스템과 Swagger에 노출하지 않는다.
- 애플리케이션 시작 시 허용된 시스템 코드와 필요한 자격증명 설정을 검증한다.
- 필수 설정이 누락되면 요청 시점이 아니라 애플리케이션 시작 시 명확한 오류로 실패시킨다.

---

## 7. 외부 API 인터페이스 정의

기존 API 경로와 응답 모델이 있으면 이를 우선 유지한다. 아래는 필수 의미를 정의한 기준 예시다.

현재 공개 API는 FastAPI `/docs`와 `/openapi.json`에 노출하며 Custom UI BFF는 노출하지 않는다.

- Chat mode: `/api/2.0/genie/spaces/{space_id}/...`
- Agent response: `POST /api/2.0/genie/agents/{agent_id}/responses`
- Agent cancel: `POST /api/2.0/genie/agents/{agent_id}/conversations/{conversation_id}/responses/{response_id}/cancel`
- Agent items: `GET /api/2.0/genie/agents/{agent_id}/conversations/{conversation_id}/items`
- 상태 확인: `GET /health/live`, `GET /health/ready`

### 7.1 최초 대화 시작

`POST /api/2.0/genie/spaces/{space_id}/start-conversation?source_system=SAMPLE&user_id=user-10001`

```json
{
  "content": "지난달 매출을 알려줘",
  "enable_visualization": false
}
```

처리 순서:

1. 호출 시스템 인증
2. Query Parameter `source_system`, `user_id`와 Body `content` 검증
3. 시스템 코드 활성 여부 확인
4. 호출 자격증명과 `source_system` 일치 여부 확인
5. 인증 시스템과 URL의 Databricks 실제 `space_id`로 허용 Agent Route 조회
6. 배포 환경에 맞는 SP 토큰 획득
7. Databricks `start-conversation` 호출
8. 요청의 `user_id`와 성공 응답의 `conversation_id`를 `ai_cnvs_mst`에 저장
9. 응답 Message와 Attachment 및 상태를 정규화해 메시지 이력 테이블에 저장
10. 기존 응답 구조를 유지하여 외부 시스템에 반환

중요: Databricks 호출이 실패하면 소유권 레코드를 저장하지 않는다.

### 7.2 후속 메시지

`POST /api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages?source_system=SAMPLE&user_id=user-10001`

```json
{
  "content": "전월과 비교해줘"
}
```

처리 순서:

1. 호출 시스템 인증 및 입력 검증
2. `(api_clnt_id, user_id, agt_id, conversation_id, ACTIVE)` 소유권 확인
3. 소유권 레코드에 저장된 `agt_id` 사용
4. 배포 환경에 맞는 SP로 Databricks 메시지 API 호출
5. Databricks 호출 직전 `content`에 `[user_id: <value>]` 컨텍스트 삽입
6. 성공 응답의 Message·Attachment·상태를 Message ID 기준으로 멱등 Upsert
7. 성공 시 `last_msg_dt` 갱신
8. 삽입 컨텍스트를 제거한 Databricks 응답 반환

후속 요청에서는 URL의 `space_id`가 소유권 레코드의 `agt_id`와 일치해야 하며 기존 Conversation의 Agent를 변경할 수 없다.

### 7.3 메시지 상태/결과 조회

기존 프로젝트가 비동기 Polling API를 제공한다면 해당 경로를 유지한다. 모든 조회 API에서도 Databricks 호출 전에 동일한 Conversation 소유권 검증을 수행한다. Start/Create/Get/List Message의 JSON 응답을 Mapper에 전달하며, 같은 `dbx_msg_id`는 현재 상태를 갱신하고 상태 코드가 바뀐 경우에만 `genie_msg_stts_hst`에 이력을 추가한다.

저장 기준:

- `genie_msg_hst`: Databricks Message ID, 사용자 질문 `content`, 현재 상태와 원본 Message JSON
- `genie_msg_atch_dtl`: `attachments[].text/query/viz/suggested_questions` 정규화 값과 원본 Attachment JSON (`visualization`은 이전 형식 호환용 fallback)
- Custom Chat 시각화 프록시는 Databricks에 `Accept: application/octet-stream`을 보내며, 성공한 PNG 바이너리 응답을 브라우저 렌더링용 `Content-Type: image/png`, `Content-Disposition: inline`으로 반환
- Message `COMPLETED`와 visualization 렌더링 완료 사이의 지연을 고려하여 프론트는 이미지 API를 브라우저 캐시 없이 제한 재조회하고 최신 PNG Blob으로 교체
- `genie_msg_stts_hst`: `SUBMITTED`, `ASKING_AI`, `EXECUTING_QUERY`, `COMPLETED`, `FAILED` 등 상태 전이
- `api_req_hst`: 호출 메타데이터. `AUDIT_PAYLOADS=false`이면 중복 Payload JSON은 저장하지 않음
- 기존 Conversation은 Message 목록 또는 단건 조회 시 Databricks 응답을 기준으로 Backfill

필수 식별값:

- `source_system`
- `user_id`
- `conversation_id`
- 필요한 경우 `message_id`

### 7.4 응답 형식

Databricks 원본 응답 호환성을 우선한다. 별도 단순 응답 모델이 이미 있으면 다음 의미를 보존한다.

Databricks 응답의 `user_id`는 Databricks 내부 사용자 또는 SP 식별자이며 외부 시스템의 사용자 고유 식별값과 다르다. 공식 응답 호환성을 위해 그대로 반환하지만 Conversation 소유권 저장·검증에는 사용하지 않는다. 소유권의 사용자 값은 항상 Query Parameter `user_id`다.

```json
{
  "conversation_id": "01f19abc...",
  "message_id": "01f19def...",
  "status": "COMPLETED",
  "answer": "지난달 총매출은 **120억 원**입니다.",
  "format": "markdown"
}
```

`attachments[].text.content`는 JSON 문자열이며 Markdown 문법을 포함할 수 있다. Databricks는 별도의 Markdown Content-Type을 제공하지 않으므로, Gateway가 단순 응답 모델을 제공하는 경우에만 `format: markdown`을 API 명세에 명시한다.

---

## 8. 내부 컴포넌트

기존 모듈 구조에 맞춰 최소 변경한다. 신규 추상화가 필요한 경우 다음 역할만 둔다.

```text
API Router
  -> ConversationService
       -> GenieMessageMapper
       -> SourceSystemRepository
       -> AgentRouteRepository
       -> ConversationOwnerRepository
       -> DatabricksGenieClient
       -> CredentialProvider
```

### 필수 인터페이스 의미

```python
class ConversationOwnerRepository:
    async def create_owner(...): ...
    async def assert_owner(...): ...
    async def touch(...): ...

class AgentRouteRepository:
    async def get_active_route(principal: GatewayPrincipal, space_id: str): ...

class CredentialProvider:
    async def get_databricks_credential(credential_ref: str | None): ...
```

기존 Databricks 클라이언트가 있으면 새로 만들지 말고 시스템별 토큰 또는 인증 설정을 주입할 수 있도록 최소 수정한다.

---

## 9. 입력 검증 및 오류 코드

| 상황 | HTTP 상태 |
|---|---:|
| 필수 파라미터 누락 또는 형식 오류 | 400 또는 422 |
| 외부 시스템 호출 인증 실패 | 401 |
| 호출 자격증명과 `source_system` 불일치 | 403 |
| Conversation 소유권 불일치 | 403 |
| 시스템 또는 Agent Route 없음 | 404 |
| 비활성 시스템/Route | 403 |
| Databricks 인증 실패 | 502 |
| Databricks 요청 실패/시간 초과 | 502 또는 504 |
| DB 장애 | 503 |

오류 응답에 SP Secret, Databricks Access Token, 내부 SQL, 전체 스택 트레이스를 포함하지 않는다.

---

## 10. 동시성 및 정합성

- Conversation 생성 성공 후 소유권 저장이 실패하면 성공 응답을 외부에 반환하지 않는다.
- 동일 Conversation 소유권 저장 요청은 멱등하게 처리한다.
- 다른 사용자에게 이미 연결된 동일 `conversation_id`의 중복 등록을 차단한다.
- DB 트랜잭션은 짧게 유지하고 Databricks 원격 호출 중 DB 트랜잭션을 열어두지 않는다.
- 토큰 캐시는 시스템별로 분리하고 만료 전에 갱신한다.
- FastAPI 다중 인스턴스 환경에서도 로컬 메모리를 Conversation 소유권의 원본으로 사용하지 않는다.

---

## 11. 로깅 및 감사

구조화 로그에 다음 값을 남긴다.

- `request_id`
- `source_system`
- `user_id` 또는 정책에 따른 해시값
- `space_id` 또는 내부 `agt_id`
- `conversation_id`
- `message_id`
- Databricks 응답 상태
- 처리 시간
- 오류 유형

질문 본문과 답변 본문은 민감정보가 포함될 수 있으므로 기본 로그에 남기지 않는다. 필요 시 별도의 마스킹·보존 정책을 적용한다. Token과 Secret은 어떤 로그에도 남기지 않는다.

---

## 12. 테스트 요구사항

### 단위 테스트

- 허용된 시스템 코드 정상 처리
- 미등록/비활성 시스템 차단
- 자격증명과 시스템 코드 불일치 차단
- 신규 Conversation 저장 성공
- A 사용자의 Conversation을 B 사용자가 요청할 때 403
- 후속 요청에서 저장된 Agent Route 사용
- Databricks 실패 시 소유권 미저장
- DB 저장 실패 시 성공 응답 미반환
- Markdown 포함 텍스트 응답 보존
- Secret과 Token 로그 미노출
- Chat 시작·후속 질문에 `user_id` 컨텍스트 삽입
- Agent `input_text`에 `user_id` 컨텍스트 삽입
- Comment·Feedback Body에는 사용자 컨텍스트 미삽입
- 외부 JSON/SSE 응답과 저장 Message 이력에서 삽입 컨텍스트 제거

### 통합 테스트

- 실제 또는 Mock Genie API를 이용한 최초 대화 → Polling → 후속 질문 흐름
- 동일 사용자의 복수 Conversation 관리
- 서로 다른 시스템에서 같은 `user_id`를 사용해도 격리됨
- FastAPI 재시작 후 Conversation 소유권 유지
- 외부 시스템 X-API-Key와 Databricks 상향 호출용 SP 자격증명이 혼용되거나 외부에 노출되지 않음
- 외부 시스템 SP의 OAuth M2M Token과 App `CAN USE` 권한으로 Apps 프록시 통과
- OAuth Token 누락, App `CAN USE` 누락 또는 X-API-Key 누락 시 각 인증 경계에서 요청 차단

---

## 13. Codex 작업 절차

1. 저장소의 `README`, `AGENTS.md`, 설정 파일 및 테스트 실행 방법 확인
2. 기존 FastAPI 라우터, Databricks 클라이언트, 인증 모듈 및 응답 모델 파악
3. 변경 영향 파일과 최소 구현 계획 제시
4. 신규 설치용 최종 통합 DDL 반영 및 검증
5. Query Parameter `source_system`, `user_id` 입력 인터페이스 반영
6. 시스템 코드·Agent Route·Conversation Owner Repository 구현
7. 시스템별 Credential Provider 연결
8. 최초 대화 저장 및 후속/조회 소유권 검증 적용
9. 단위·통합 테스트 추가
10. 기존 테스트와 신규 테스트 실행
11. 환경변수 예제와 운영 절차 문서 갱신

Codex는 기존 코드와 충돌하거나 현재 인증 방식이 불명확할 경우 임의로 대규모 리팩터링하지 말고, 확인된 구조와 최소 변경안을 먼저 보고한다.

---

## 14. 완료 조건

- Databricks Apps에서는 App 전용 SP로 Genie API를 호출하며, 외부 요청은 Apps 프록시 검증 후 X-API-Key와 `source_system` 일치를 검증한다.
- 모든 Gateway API에서 `source_system`, `user_id`를 Query Parameter로 받는다.
- 후속 및 조회 요청에서 `source_system`, `user_id`로 Conversation 소유권을 검증한다.
- 다른 사용자의 Conversation 접근이 403으로 차단된다.
- `conversation_id`를 별도 `room_id`로 변환하지 않는다.
- 시스템과 Databricks 실제 `space_id`/`agent_id`별 허용 Route가 테이블로 관리된다.
- SP Secret이 소스코드, DB 평문 컬럼 및 로그에 저장되지 않는다.
- Databricks 공식 요청/응답과 기존 FastAPI 인터페이스가 불필요하게 변경되지 않는다.
- Chat 시작·후속 질문과 Agent API에 `user_id` 컨텍스트가 내부 삽입되며 외부 응답·SSE·Chat 이력에는 노출되지 않는다.
- Lakebase와 RDS PostgreSQL 중 선택된 저장소에서 재기동 후에도 매핑이 유지된다.
- 신규 테스트와 기존 회귀 테스트가 모두 통과한다.

---

## 15. 이번 범위에서 제외

- 사용자별 Service Principal 생성
- 외부 시스템 최종 사용자의 Databricks OAuth U2M/OBO 구현
- `user_id`를 이용한 Unity Catalog 개인별 Row Filter/Column Mask 적용
- 질문에 삽입한 `user_id` 컨텍스트만으로 사용자별 데이터 접근권한을 강제하는 방식
- 별도의 `room_id` 생성 및 Conversation ID 변환 계층
- FastAPI 프레임워크 교체 또는 전체 재작성
## 16. Custom UI Chat·Agent 구현 상태

- 적용 범위: `/custom/chat` 및 `/custom/chat/api` 내부 BFF
- 공개 API 영향: 없음. 기존 Space ID/Agent ID별 공개 엔드포인트 인터페이스를 유지한다.
- 모드 전환: `CHAT`과 `AGENT` 전환 시 활성 Conversation을 초기화한다.
- 통합 이력: Chat/Agent 목록은 함께 반환하되 각 항목에 `conversation_mode`를 포함한다.
  이력 선택은 항목 자체의 모드에 따라 `/messages` 또는 `/items`를 호출하고, 소유권
  검증에는 `cnvs_mode_cd`를 적용한다. 새 Agent 대화는 SSE `response.created`에서
  Conversation ID가 등록되는 즉시 사이드바 이력을 갱신한다.
  Gateway SSE 관찰기는 JSON이 여러 chunk 또는 여러 줄로 분리돼도 첫
  `conversation_id`를 추출하여 `AGENT` 소유권을 등록한다. 업스트림 SSE가 gzip/br로
  압축된 경우에는 Gateway에서 먼저 압축을 해제한 바이트를 관찰하고, 브라우저에는
  중복 압축 해제가 발생하지 않도록 `Content-Encoding` 헤더를 제거해 전달한다.
- Agent 응답: POST 기반 SSE를 `fetch` 스트림으로 읽고 `output_index`/item ID 기준으로
  갱신한다. 처리 과정은 `response.output_item.done` 이후에만 한 번 표시한다. 실행 코드
  상세는 1차 운영 화면에서 숨기며 `SHOW_AGENT_CODE` 기능 플래그로 다시 활성화할 수 있다.
  완료된 `function_call`과 `function_call_output`은 `call_id`로 연결해 독립된 조회 결과
  카드로 표시하고, 빈 결과는 접힌 상태로 축약한다. 최종 assistant `message`는 실행
  결과와 섞지 않고 별도의 인사이트 요약 영역에 렌더링한다.
- Agent 요청의 `stream`과 `enable_viz`는 OpenAPI에 명시한다. `enable_viz=true`는
  Databricks에 전달하는 조건부 시각화 요청 힌트이며 차트 생성이나 PNG 반환을 보장하지
  않는다. Agent Mode는 Chat Mode Visualization Attachment API와 달리 Output Item·Tool
  결과 데이터를 반환할 수 있으므로 외부 화면은 이를 표·차트 컴포넌트로 렌더링한다.
- Gateway는 Databricks가 보내지 않은 token delta나 공개 SSE heartbeat를 임의 생성하지
  않는다. 조용한 구간의 연결 상태는 공식 terminal event, HTTP 연결, Run 상태와 설정된
  timeout을 기준으로 판단한다.
- Markdown: 제목, 목록, 표, 코드, 강조와 HTTP(S) 링크를 블록/인라인 단계로 분리해
  렌더링한다. Genie의 이스케이프된 citation 링크는 전체 URL 대신 `[1]` 형태로 표시한다.
- Agent 이력: Databricks Agent `/items`를 원본으로 사용하며 1차 구현에서는 Agent item
  저장 테이블을 추가하지 않는다.
- 외부 연계의 대화 목록은 Chat Space 목록, Agent 소유권·`/items`, Supervisor
  Conversation/Run 이력을 별도로 조회한다. CUBE가 통합 목록을 만들 때는 각 항목의 모드와
  `agent_id`를 보존하여 상세 조회 API를 구분한다.
- PDF: Gateway가 `/items`를 재조회한 뒤 보고서, Markdown 표, 실행 정보를 PDF로 생성한다.
- 공유: Web Share API를 우선 사용하고 미지원 브라우저에서는 현재 대화 URL을 복사한다.
- 오류 진단: 상세 조회 실패 시 HTTP method, 요청 경로, 상태 코드, Gateway 오류 코드와
  `x-request-id`를 화면에 표시하여 서버 로그의 trace ID와 대조할 수 있게 한다.

추가 구현 상태:

- Agent/채팅 선택 버튼은 입력창 내부 같은 행 우측에 배치한다.
- 모드 전환 시 기존 Conversation을 이어 쓰지 않고 새 대화를 시작한다.
- 사이드바는 Chat/Agent Conversation을 함께 표시하고 항목 자체의 모드로 상세 조회 경로를 결정한다.
- Agent Conversation은 삭제 API가 없어 Custom UI에서 삭제 동작을 숨긴다.
- Chat Conversation 제목 변경·삭제와 ID 복사를 지원한다.
- Custom UI 자산은 허용 목록으로만 제공하고 `Cache-Control: no-store`, CSP 및
  `X-Content-Type-Options: nosniff`를 적용한다.
- Agent 결과 카드에서 빈 결과는 축약하고, 최종 assistant 응답은 실행 결과 아래의 독립된
  인사이트 영역으로 분리한다.

## 17. 현재 테스트로 확인하는 회귀 항목

- 공식 Genie 공개 경로 목록과 OpenAPI 보안 스키마
- Custom UI API의 OpenAPI 비노출
- Agent 요청의 단일 user message 검증
- Agent SSE 사용자 컨텍스트 삽입·제거
- chunk/멀티라인 SSE의 Conversation ID 관찰과 선등록
- gzip/br 응답 압축 해제 후 관찰 및 `Content-Encoding` 제거
- Agent Cancel 요청의 소유권 검증과 상향 라우팅
- Chat Visualization의 binary Accept와 PNG body 보존
- Chat Message Mapper와 상태·Attachment 정규화
- API Key와 `source_system` 교차검증
- Databricks Apps OAuth Principal 처리와 API Key 모드
- PostgreSQL, RDS, RDS IAM, Lakebase DB 연결 방식
- Agent `/items` 기반 PDF 생성

## 18. 후속 개발 문서

- Supervisor Agent: [`supervisor_agent_gateway_implementation_spec.md`](supervisor_agent_gateway_implementation_spec.md)
- SSO/OAuth·사용자별 UC 권한: [`추후_개발_과제.md`](추후_개발_과제.md)

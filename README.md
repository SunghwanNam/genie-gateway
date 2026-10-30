# Genie API Gateway

외부 시스템이 Gateway 논리 Space/Agent ID 또는 기존 Databricks 실제 ID로 Genie API 경로를 호출하면,
Gateway가 X-API-Key와 시스템별 사용 권한을 검증한 뒤 등록된 Databricks 실제 ID로 중계하는
FastAPI 게이트웨이입니다.

## 동작 구조

```text
외부 시스템 ── X-API-Key ──> FastAPI Gateway
                                      ├─ PostgreSQL: 클라이언트 인증
                                      ├─ PostgreSQL: 시스템별 Agent 라우팅
                                      ├─ PostgreSQL: API 감사 이력
                                      └─ Databricks Genie API
```

- Controller → Service → Repository 구조
- 모든 외부 I/O 비동기 처리
- Agent 목록의 논리 `agent_id`를 공식 호환 API 경로에 사용하면 Gateway가 등록된 Databricks 실제 ID로 변환
- 기존 Databricks Space/Agent ID 호출도 하위 호환을 위해 유지
- Chat mode JSON/PNG 및 Agent mode SSE 스트리밍 전달
- `api_req_hst`에 요청 메타데이터 저장
- Chat 질문은 `genie_msg_hst`, AI 답변·SQL·시각화는 `genie_msg_atch_dtl`, 상태 전이는 `genie_msg_stts_hst`에 정규화 저장
- `AUDIT_PAYLOADS=false`이면 감사 테이블의 중복 요청·응답 JSON만 저장하지 않으며 정규화된 Chat 이력은 계속 저장
- `source_system`, `user_id`를 Gateway 전용 Query Parameter로 받고 Databricks Query Parameter로는 전달하지 않음. 단, 통합 Agent 목록은 시스템 단위 조회이므로 `user_id`가 선택 사항
- Chat 시작·후속 질문과 Agent `input_text`에는 Gateway가 `[user_id: <value>]`를 내부 컨텍스트로 삽입
- 삽입된 사용자 컨텍스트는 외부 API 응답, SSE, 정규화된 Chat 이력과 감사 Payload에서 제거
- `ai_cnvs_mst` 기준 사용자별 Conversation 소유권 검증
- 요청 `user_id`는 `ai_cnvs_mst.usr_id`에 저장하며 Databricks 응답 `user_id`는 소유권에 사용하지 않음

## 구현 API

Databricks 공식 경로를 그대로 사용하되 `{space_id}`와 `{agent_id}`에는 Agent 목록이 반환한
Gateway 논리 ID를 권장합니다. 기존 Databricks 실제 ID도 계속 호환합니다.

- Chat mode: `/api/2.0/genie/spaces/{space_id}/...`
  - 대화 시작·목록·삭제
  - 메시지 생성·목록·조회·삭제
  - Query Result 조회·재실행·전체 다운로드
  - Visualization PNG 다운로드
  - Feedback·Comment 생성 및 조회
- Agent mode: `/api/2.0/genie/agents/{agent_id}/responses`
- Agent mode: `/api/2.0/genie/agents/{agent_id}/conversations/{conversation_id}/responses/{response_id}/cancel`
- Agent mode: `/api/2.0/genie/agents/{agent_id}/conversations/{conversation_id}/items`
- 통합 Agent 목록: `GET /api/v1/agents?source_system=CUBE` (`user_id`는 선택, `include_inactive=true`이면 시스템별 비활성 매핑 포함)
- Supervisor 응답(SSE): `POST /api/v1/agents/{agent_id}/responses`
- Supervisor Run 상태: `GET /api/v1/agents/{agent_id}/runs/{run_id}`
- Supervisor Run 중단: `POST /api/v1/agents/{agent_id}/runs/{run_id}/cancel`
- Supervisor 도구 승인/거절(SSE 재개): `POST /api/v1/agents/{agent_id}/runs/{run_id}/approvals/{approval_id}`
- Supervisor 완료 Item: `GET /api/v1/agents/{agent_id}/conversations/{conversation_id}/items`
- 운영: `/health/live`, `/health/ready`

상세 스키마는 실행 후 `/docs` 또는 `/openapi.json`에서 확인할 수 있습니다.

외부 개발자는 Gateway 전용 문법을 새로 익힐 필요 없이 아래 Databricks 공식 문서의 Genie 요청 형식을 그대로 사용할 수 있습니다.

- [Databricks Workspace API Reference](https://docs.databricks.com/api/workspace/)
- [Genie Conversation API](https://docs.databricks.com/aws/en/genie-agents/conversation-api)
- [Genie Agent mode: Create response](https://docs.databricks.com/api/genie/v1/agent-mode-create-response)
- [Genie Agent mode: Cancel response](https://docs.databricks.com/api/genie/v1/genie-cancel-response)

## 로컬 실행

Python 3.11 이상과 PostgreSQL 15 이상을 권장합니다.

```bash
cp .env.example .env
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 38085
```

현재 로컬 PostgreSQL 컨테이너를 직접 사용할 때 애플리케이션을 호스트에서 실행하면 DB 호스트는 `localhost:15432`, Gateway를 Docker에서 실행하면 `host.docker.internal:15432`를 사용합니다.

### 커스텀 Chat 시연 화면

로컬 운영자 테스트용 화면은 `http://localhost:38085/custom/chat`에서 제공합니다.

```env
CUSTOM_CHAT_UI_ENABLED=true
CUSTOM_CHAT_LOCAL_ONLY=true
```

- `/custom/chat`과 `/custom/chat/assets/*`는 Swagger 및 `/openapi.json`에 노출하지 않음
- `CUSTOM_CHAT_UI_ENABLED=false`이면 `404` 반환
- `CUSTOM_CHAT_LOCAL_ONLY=true`이면 localhost 이외의 접근을 `403`으로 차단
- Agent와 기본 라우팅은 `/custom/admin`에서 등록
- Admin Web의 기본 관리자 ID는 `admin`이며 기존 설치는 `008_rename_default_admin.sql`을 한 번 적용
- Agent 등록 시 API 모드(`CHAT`, `AGENT`, `BOTH`)를 선택하고, `CHAT`은 Genie Space ID, `AGENT`는 Agent ID, `BOTH`는 두 ID를 모두 입력
- Admin Web의 `삭제`는 물리 삭제가 아니라 시스템-Agent 매핑의 소프트 삭제다. 기본 목록에서는 숨기고 `비활성 포함`에서 조회·복구할 수 있으며 기존 대화와 요청 이력은 유지한다.
- Agent 소프트 삭제 시 매핑의 `use_yn='N'`, `dflt_yn='N'`, `eff_end_dt=현재시각`을 기록하고, 다른 활성 시스템 매핑이 없을 때만 Agent 마스터를 비활성화한다. 기본 Agent를 삭제하면 다음 활성 Agent를 기본으로 승격한다.
- 상단 Agent 드롭다운에는 인증 시스템에 연결된 활성 Agent만 표시하며, Agent를 변경하면 해당 Agent의 대화 이력으로 전환
- 선택한 Agent와 `CHAT`·`AGENT` 모드는 URL Query Parameter에 유지하며, 지원하지 않는 모드 버튼은 비활성화
- 기존 Gateway Service를 호출하므로 Agent 라우팅, Conversation 소유권 및 Message Mapper를 동일하게 검증
- Genie의 `attachments[].viz`는 시각화 다운로드 API로 조회하며, Custom Chat은 성공한 PNG 바이너리를 브라우저 표시용 `image/png`·`inline` 응답으로 전달
- Message 완료 직후 시각화 PNG가 아직 준비되지 않을 수 있으므로, 프론트는 이미지 API를 `no-store`로 제한 재조회하여 준비된 차트로 갱신

스키마 적용:

```bash
docker exec -i postgres15 psql -U postgres -d sunghwan < sql/installation/001_create_genie_gw_schema.sql
```

> 실제 DB 사용자명은 로컬 컨테이너 설정에 맞게 변경합니다. 시스템·클라이언트·Workspace·Agent·라우팅·API Key는 `/custom/admin`에서 등록합니다.
> `001`은 최신 전체 DDL이므로 빈 DB에는 `004`~`007` migration을 별도로 실행하지 않습니다.

## 호출 예시

Chat mode 대화 시작:

```bash
curl -X POST 'http://localhost:38085/api/2.0/genie/spaces/sample-agent/start-conversation?source_system=SAMPLE&user_id=10001' \
  -H 'X-API-Key: YOUR_CUBE_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"content":"이번 달 매출을 요약해줘"}'
```

Agent mode SSE:

```bash
curl -N -X POST 'http://localhost:38085/api/2.0/genie/agents/sample-agent/responses?source_system=SAMPLE&user_id=10001' \
  -H 'X-API-Key: YOUR_CUBE_API_KEY' \
  -H 'Accept: text/event-stream' \
  -H 'Content-Type: application/json' \
  -d '{"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"이번 달 매출을 요약해줘"}]}]}'
```

EC2/RDS 배포의 외부 호출자는 Databricks Credential이 아니라 Gateway Credential을 사용합니다. 인증용 SHA-256 해시와 관리자 원문 조회용 AES-GCM 암호문을 분리 저장합니다.

Swagger UI에서는 우측 상단 `Authorize`를 누르고 `GatewayApiKey`에 키를 한 번만 입력합니다. 이후 모든 API 요청에 `X-API-Key`가 자동으로 적용됩니다. EC2의 `API_KEY` 모드에서는 `X-API-Key`가 필수이며 `Authorization: Bearer`로 대체할 수 없습니다.

외부 시스템은 Body에 사용자 표식을 직접 추가하지 않습니다. Gateway가 Query Parameter `user_id`를 이용해 Genie에 전달할 질문을 다음과 같이 구성하며, 외부 응답과 대화 이력에는 원래 질문만 노출합니다.

```text
[user_id: 10001]
이번 달 매출을 요약해줘
```

Databricks 공식 응답의 `user_id`는 Databricks 내부 사용자 또는 SP 식별자일 수 있으며 Query Parameter로 받은 CUBE 사번과 다른 값입니다. Gateway는 공식 응답 호환성을 위해 이 값을 그대로 반환하고, 소유권은 요청 `user_id`와 응답 `conversation_id`를 `ai_cnvs_mst`에 연결하여 판단합니다.

## 배포 환경별 인증

### EC2/RDS

- `GATEWAY_AUTH_MODE=API_KEY`
- 외부 시스템은 시스템별 `X-API-Key` 사용
- API Key에 귀속된 시스템과 `source_system`이 다르면 `403`
- `ext_sys_mst.dbx_cred_ref_nm`으로 시스템별 Databricks SP 선택
- `env:<PREFIX>` 또는 `secretsmanager:<secret-id>` 형식 지원
- Secrets Manager 값은 `{"client_id":"...","client_secret":"..."}` JSON 형식
- 운영 EC2의 시스템별 SP 저장·권한·교체 정책과 Secrets Manager 적용 방식은 운영 담당자와 추후 협의 필요

### Databricks Apps/Lakebase

- `GATEWAY_AUTH_MODE=API_KEY` (`app.yaml` 기본값)
- 외부 시스템 SP가 OAuth M2M Bearer 토큰으로 App의 `/api/` 경로 호출
- Databricks Apps 인증 프록시가 OAuth 토큰과 App `CAN USE` 권한을 먼저 검증
- 외부 시스템은 Gateway가 발급한 `X-API-Key`도 반드시 함께 전송
- Gateway는 X-API-Key에 귀속된 시스템과 `source_system` 일치 여부를 추가 검증
- Gateway의 Databricks 호출에는 App 전용 SP 환경변수 사용

#### 외부 연계 시스템 준비

Databricks Apps의 `/api/` 엔드포인트를 외부 시스템이 서버 간 호출하려면 다음을 사전에 준비합니다.

1. 연계 대상 시스템용 Databricks Service Principal 생성 및 Workspace 할당
2. 해당 SP에 Gateway App `CAN USE` 권한 부여
3. SP Client ID/Secret으로 OAuth M2M Access Token 발급 및 만료 전 갱신
4. Gateway Admin에서 시스템·API 클라이언트 등록 후 시스템 전용 X-API-Key 발급

외부 시스템은 매 요청에 두 인증 헤더를 함께 전달합니다.

```http
Authorization: Bearer <Databricks OAuth M2M Access Token>
X-API-Key: <Gateway가 발급한 시스템 전용 API Key>
```

`Authorization`은 Databricks Apps 접근 인증, `X-API-Key`는 Gateway 업무 시스템 인증입니다. OAuth Token과 SP Client Secret은 Gateway DB에 저장하지 않으며 연계 시스템이 안전하게 보관하고 갱신합니다. 로컬 Gateway의 Swagger는 Apps 프록시를 통하지 않으므로 X-API-Key만 요구하고, Apps에 배포된 Swagger는 Databricks 로그인 및 App `CAN USE` 권한을 추가로 요구합니다.

공식 연동 방법: [Connect to an API Databricks app using token authentication](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/connect-local)

## Databricks 상향 인증

아래 중 하나를 설정합니다.

1. OAuth M2M 권장: `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET`
2. 개발·검증용 PAT: `DATABRICKS_TOKEN`

위 자격증명은 Gateway가 Databricks를 호출할 때만 사용하는 내부 설정이다. 외부 시스템과 Swagger 사용자에게 노출하거나 전달하지 않는다.

`ai_agt_mst.dbx_ws_url`이 있으면 Agent별 Workspace를 사용하고, 없으면 `DATABRICKS_HOST`를 사용합니다. Secret 원문은 DB에 저장하지 않습니다.

## 배포

EC2 빌드용 소스 패키지와 Databricks Apps 배포 ZIP은 한 번에 재현 가능하게 생성한다.

```bash
./deploy/package_release.sh <VERSION>
```

생성물과 체크섬은 `build/` 아래에 저장된다. 자세한 구성과 Apps 동기화 절차는
[deploy/README.md](deploy/README.md), Amazon Linux 실행 패키지 빌드·설치는
[deploy/ec2/README.md](deploy/ec2/README.md)를 참조한다.

외부 연계 API 명세서는 실행 중인 Gateway의 `/guide`에서 확인한다. 명세는 Databricks Genie Conversation API Path·Body·응답 인터페이스를 기준으로 하며, Gateway 전용 입력인 `X-API-Key`, `source_system`, `user_id`와 대화 소유권 제약을 별도로 표시한다.

Amazon Linux 2023 단일 EC2에 PostgreSQL 15와 컴파일된 Gateway를 tar.gz로 설치하는 절차는 [deploy/ec2/README.md](deploy/ec2/README.md)를 참조한다. 서버에는 원본 Python 소스 대신 Nuitka standalone 실행 파일을 배포하며, 설정은 `/etc/genie-gateway/gateway.env`, 실행 파일은 `/opt/genie-gateway/runtime/current`에서 관리한다.

Docker/EC2:

```bash
docker compose -f docker-compose-ext-api-gtw.yml up --build -d
```

Databricks Apps는 저장소 루트의 `app.yaml`을 사용합니다. Apps가 주입하는 `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET`, `UVICORN_HOST`, `UVICORN_PORT`를 그대로 인식합니다. PostgreSQL은 Apps 런타임에서 접근 가능한 주소여야 하며, 노트북의 로컬 컨테이너 주소는 사용할 수 없습니다.

Admin Web 최초 설정은 로컬 환경에서는 loopback 접근만 허용하고, Databricks Apps에서는
`X-Forwarded-Email`과 `ADMIN_BOOTSTRAP_EMAIL`이 정확히 일치하는 사용자만 허용합니다.

### 데이터베이스 연결 모드

Repository와 `genie_gw` 스키마는 모든 모드에서 동일하며 `DATABASE_MODE`로 연결 인증만 선택합니다.

| 모드 | 용도 | 필수 설정 |
|---|---|---|
| `POSTGRES` | 로컬·일반 PostgreSQL | `DATABASE_URL` |
| `RDS` | RDS 비밀번호 인증 | `PGHOST`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` 또는 `RDS_SECRET_REF` |
| `RDS_IAM` | RDS IAM DB 인증 | 공통 PG 설정, `AWS_REGION`, AWS 자격증명, `rds-db:connect` 권한 |
| `LAKEBASE` | Lakebase OAuth DB 인증 | 공통 PG 설정, `LAKEBASE_ENDPOINT`, Databricks 인증 환경변수 |

`RDS_SECRET_REF`는 `secretsmanager:<secret-id>` 형식이며 Secret JSON은 `username`, `password`를 사용합니다. `RDS_IAM`은 새 물리 연결마다 IAM DB Auth Token을 발급하고 TLS를 강제합니다. DB 사용자에는 PostgreSQL `rds_iam` 역할이 필요합니다.

`LAKEBASE`는 Databricks SDK의 `generate_database_credential()`로 새 물리 연결마다 OAuth DB Credential을 발급합니다. Databricks Apps에서는 Lakebase Resource가 제공하는 `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGSSLMODE`를 사용하고 `LAKEBASE_ENDPOINT`를 App 환경변수로 설정합니다. App SP와 Lakebase PostgreSQL 역할에는 `genie_gw` 스키마 및 테이블 사용 권한이 필요합니다.

기존 로컬 `.env`에 `DATABASE_MODE`가 없으면 기본값은 `POSTGRES`이므로 기존 `DATABASE_URL` 실행 방식이 유지됩니다.

### DB 설치 순서

RDS, Lakebase 및 일반 PostgreSQL은 동일한 `genie_gw` DDL을 사용합니다. 애플리케이션은 운영 DB에 DDL을 자동 실행하지 않습니다.

1. `sql/installation/001_create_genie_gw_schema.sql` 실행
2. `sql/installation/002_grant_runtime_role.sql` 실행
3. `sql/installation/003_verify_installation.sql` 실행
4. `/custom/admin` 최초 설정 후 시스템·클라이언트·Workspace·Agent·라우팅·API Key 등록

DDL에는 특정 시스템 기준정보를 포함하지 않습니다. 동일 설치 SQL을 모든 환경에 적용하고 CUBE, 포털, 지식관리 등 시스템별 기준정보는 `/custom/admin`에서 별도 관리합니다.

기존 Genie Gateway DB를 Supervisor 기반 구조로 확장할 때는 애플리케이션 배포와 같은 작업
단위에서 `004_add_conversation_mode_index.sql`,
`005_add_supervisor_agent_foundation.sql`, `006_add_run_execution_lease.sql`,
`007_add_supervisor_approval.sql`, `002_grant_runtime_role.sql`,
`003_verify_installation.sql` 순서로 실행합니다.
`005`는 기존 PK·FK·데이터를 보존하면서 Agent·Conversation 테이블을 `ai_*` 공통 명칭으로
변경하고 Run·Item·Event 테이블을 추가하며, `006`은 Run 실행 lease 컬럼을, `007`은
Web Search/MCP 승인 상태·이력 테이블을 추가합니다.

개발·검증 환경을 완전히 초기화할 때만
`000_reset_genie_gw_schema_DEVELOPMENT_ONLY.sql` 실행 후 `001`부터 다시 적용합니다.
초기화 SQL은 `genie_gw` 스키마와 모든 데이터를 삭제하므로 운영·공용 Lakebase에서는
실행하지 않습니다. 세부 분기는 `sql/installation/README.md`를 따릅니다.

## 테스트

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q app tests
```

## 현재 구현 범위와 다음 단계

현재 구현된 주요 기능은 다음과 같습니다.

- Databricks 공식 Genie Chat/Agent 경로를 유지하는 비동기 프록시
- 배포 환경별 인증, X-API-Key 검증, 시스템별 Workspace·Agent 라우팅
- `ai_cnvs_mst` 기반 Conversation 소유권과 `CHAT`/`AGENT` 모드 구분
- Chat Message·Attachment·상태의 정규화 저장 및 Message ID 기준 멱등 Upsert
- Message 목록·단건 조회를 이용한 기존 Conversation 이력 Backfill
- Agent mode SSE의 사용자 컨텍스트 삽입·제거와 `response.created` 기반 소유권 등록
- chunk/멀티라인 SSE와 gzip/br 압축 응답 관찰
- Custom UI의 Chat/Agent 전환, 통합 이력, Markdown·citation, 결과 카드 렌더링
- `/custom/supervisor-chat` 기반 Supervisor SSE·long task·중단·저장 대화 이력 검증 화면
- Agent `/items` 기반 이력 조회와 PDF 보고서 생성, Web Share/링크 복사
- Chat Visualization 바이너리 프록시와 PNG inline 응답, 준비 지연 시 제한 재조회
- PostgreSQL, RDS 비밀번호·IAM 인증, Lakebase OAuth DB Credential 연결 모드
- `/custom/admin` 기반 시스템·클라이언트·Workspace·Agent·API Key 관리

현재 Agent mode의 item은 별도 메시지 테이블에 정규화 저장하지 않고 Databricks `/items`를
원본으로 사용합니다. 개인정보 마스킹·보존기간·삭제 정책과 운영 설정 테이블의 런타임
적용은 후속 범위입니다.

Supervisor 기반 구현은 Agent 등록·논리 목록·라우팅, Serving Endpoint streaming client,
최소 요청 adapter, SSE parser/event normalizer, DB 기반 Run 생성·중복 제한·15/40/2
동시 실행 한도와 조건부 상태 전이를 반영합니다. 외부 통합 SSE 응답, Run 상태·중단,
Conversation 완료 Item 조회도 연결되어 있습니다. HTTP SSE 연결이 종료되어도 동일 프로세스의
Run Task는 계속되며, 중단 상태는 최대 약 1초 간격으로 확인합니다.

`long_task=true` 요청은 `task_continue_request`를 감지해 같은 Run에서 자동 후속 호출하며,
continuation ID 중복·최대 횟수·전체 deadline과 중단 요청을 DB 상태로 검사합니다. 아직 남은
Supervisor 범위 중 `/custom/supervisor-chat`의 저장 대화 목록·완료 Item 복원 화면까지 구현되었습니다. `message.completed` 뒤
`response.completed` 없이 정상 종료하는 endpoint는 Gateway가 `run.completed`를 보완 생성합니다. 남은 작업은 실제
Databricks Supervisor endpoint 3개 이상을 등록한 통합 시험입니다. 복수 Gateway instance의 중복 upstream 실행은 DB execution lease로 차단하고,
`Last-Event-ID` 이후 Event는 DB에서 재생합니다. 실행 Task와 즉시 fan-out은 프로세스 로컬이며
다른 instance의 구독자는 DB Event를 최대 약 1초 간격으로 조회합니다.
후속 질문은 완료된 사용자·Assistant Item을 기준으로 최신 20 turn 또는 40,000자 중 먼저
도달하는 범위만 upstream history로 재구성합니다.

Supervisor가 Web Search를 선택하면 원본 `mcp_approval_request`를 `approval.required`로
정규화하고 Run을 `WAITING_APPROVAL` 상태로 보류합니다. 승인 요청과 재개 문맥은
`ai_run_aprv_hst`에 저장됩니다. 외부 클라이언트는 이벤트의 `approval_id`로 승인/거절 API를
호출하고, 반환되는 SSE를 이어 읽으면 동일 `run_id`에서 검색 및 답변 생성이 계속됩니다.
기존 DB 업그레이드는 애플리케이션 배포 전에 `007_add_supervisor_approval.sql` 실행이 필요합니다.

다음 주요 개발 범위는 CUBE AI Agent 목록에서 선택할 수 있는 Supervisor Agent 연동입니다.
Supervisor 상세 범위, API 인터페이스 정의, 데이터 모델, `long_task`, timeout, 사용자 중단,
중복 실행 방지 및 단계별 작업은
[Supervisor Agent Gateway 개발 명세](txt_docs/supervisor_agent_gateway_implementation_spec.md)를
기준으로 진행합니다.

Supervisor 외부 연동의 `/guide`에는 통합 API의 정상 응답, Run 상태, SSE Event,
HTTP 상태와 업무 오류 코드 전체 표 및 대표 성공·실패·timeout·중단 예제를 함께 제공합니다.
Databricks 원본 Responses Event와 실제 serving endpoint는 외부 명세에 직접 노출하지 않습니다.

Supervisor의 Item·Streaming·MCP 승인 흐름은 OpenAI Responses API의 개념을 참고하지만,
외부 연계 기준은 Gateway가 정규화한 `/api/v1/agents/...` API와 SSE 이벤트입니다. Gateway는
Databricks에 없는 공개 heartbeat를 임의 생성하지 않으며, 클라이언트는 Run 상태와 Agent별
timeout을 기준으로 연결 상태를 판단합니다. 답변에 포함된 Databricks 링크는 원본 근거 링크로
유지하고 실제 열람 권한은 Databricks가 검증합니다.

Agent Mode의 `enable_viz`는 조건부 시각화 요청 힌트이며 차트 또는 PNG 생성을 보장하지
않습니다. CUBE는 Output Item과 Tool 결과 데이터를 표·차트 컴포넌트로 렌더링하고,
시각화 Item이 없으면 텍스트·표 결과만 표시합니다. Chat, Agent, Supervisor 대화 이력은
각 모드의 저장·조회 구조가 다르므로 외부 화면에서 별도 목록으로 조회한 뒤 필요할 때만
화면 모델로 통합합니다.
통합 요청의 최상위 필드는 엄격하게 검증하여 오타를 HTTP 400으로 안내하고, `metadata`의
미지원 키는 제거한 뒤 Agent 유형과 capability에 필요한 최소 필드만 Databricks에 전달합니다.
Supervisor 대화 Context는 최신 20 turn 또는 40,000자 중 먼저 도달하는 범위로 제한하고,
현재 질문은 항상 포함하며 오래된 turn부터 제외합니다. 200~300명 사용자 환경의 운영
시작값은 Supervisor Agent별 활성 Run 15개, Gateway 전체 40개, 사용자 전체 10개,
사용자별 동일 Supervisor 5개로 두고 실제
endpoint 부하 테스트 결과에 따라 조정하는 안을 기준으로 합니다.
전체·사용자·사용자별 동일 Supervisor 한도는 `/custom/admin`의 **Supervisor 동시 실행 설정**에서
재기동 없이 변경할 수 있고, 환경변수는 DB 설정이 없을 때의 기본값입니다. 동일 대화의 활성
Run은 순서 보장을 위해 1개로 고정하며 Agent 전체 한도는 Admin의 Agent 등록값으로 관리합니다.
EC2 초기 검증은 단일 8 vCPU·32 GiB 범용 인스턴스로 충분한 것으로 보며, 운영 가용성이
필요하면 4 vCPU·16 GiB 두 대를 ALB/ASG로 서로 다른 AZ에 배치하는 구성을 우선 검토합니다.

## Custom UI Chat / Agent 모드

`/custom/chat`은 공개 Genie 호환 API와 별개인 내부 BFF 화면이다. 화면의 `Agent / 채팅`
전환 버튼은 각각 Databricks Agent mode SSE와 기존 Chat mode polling을 사용하며, 모드를
바꾸면 새 대화를 시작한다. 공개 `/api/2.0/genie/spaces/...` 및
`/api/2.0/genie/agents/...` 요청·응답 인터페이스는 변경하지 않는다.

대화 소유권은 기존 `ai_cnvs_mst.cnvs_mode_cd`(`CHAT`, `AGENT`)로 구분한다. 사이드바는
두 모드의 이력을 통합해서 보여주고, 선택한 이력의 모드에 따라 Chat `/messages` 또는
Agent `/items`를 조회한다. Agent
이력은 별도 메시지 테이블에 저장하지 않고 Databricks
`GET .../conversations/{conversation_id}/items`를 조회한다. Agent 결과의 PDF는 Custom
BFF가 같은 `/items`를 서버에서 재조회하여 생성하며, 공유 1차 버전은 소유권 검증을 받는
Custom UI 링크를 Web Share API 또는 클립보드로 전달한다.

## 개발 문서

- [Genie Conversation Gateway 개발 명세](txt_docs/genie_gateway_implementation_spec.md)
- [Supervisor Agent Gateway 개발 명세](txt_docs/supervisor_agent_gateway_implementation_spec.md)
- [추후 개발 과제](txt_docs/추후_개발_과제.md)
- [DB 설치 및 업데이트](sql/installation/README.md)
- 외부 연계 API 명세: 실행 중인 Gateway의 `/guide`

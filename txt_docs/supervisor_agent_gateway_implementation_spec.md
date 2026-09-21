# Supervisor Agent Gateway 개발 명세

## 1. 문서 목적과 상태

이 문서는 Databricks Supervisor Agent를 Gateway와 CUBE AI Agent 채팅방에 연동하기 위한
개발 기준을 정의한다. 데이터 모델, Admin, 통합 목록, Supervisor SSE 실행, Run 상태·중단과
완료 Item 조회, `long_task` continuation, DB 실행 lease, Event 재생과 Custom 검증 화면까지
구현되었다. 실제 endpoint 통합 시험은 배포 환경 확인 범위다. 경로·테이블·상태 코드는 구현과 함께 README,
외부 API 명세, 설치 SQL을
동시에 갱신한다.

기존 Genie Chat/Agent의 현재 구현 기준은
[`genie_gateway_implementation_spec.md`](genie_gateway_implementation_spec.md)를 따른다.

### 1차 목표

- CUBE AI Agent 목록에서 일반 Genie와 Supervisor Agent를 함께 선택
- Supervisor serving endpoint를 최소 3개 이상 등록하고 수량 제한 없이 확장
- CUBE는 Gateway의 공개 Agent ID를 사용하고 Databricks 내부 endpoint 이름은 알지 않음
- SSE 응답, `long_task`, timeout, 사용자 중단과 실행 이력을 Gateway가 통합 처리
- CUBE와 별개로 `/custom` 아래 Supervisor 전용 검증 화면 제공
- 개발계에서 실제 외부 메신저 통합 시험이 가능한 수준 확보

### 1차 범위가 아닌 항목

- 완전한 서버 측 강제 취소 보장
- SSE `Last-Event-ID` 기반 재생과 무손실 재연결
- 복수 Gateway 인스턴스 간 분산 실행 소유권·취소 신호
- 운영 부하시험, 세밀한 Rate Limit, 운영 알람
- CUBE 자체 AI Agent 관리·채팅 화면 개발

---

## 2. 사용자 및 시스템 흐름

```text
CUBE AI Agent 목록
  ├─ 일반 Genie Agent
  │    ├─ CHAT
  │    ├─ AGENT
  │    └─ BOTH
  └─ Supervisor Agent
       └─ SUPERVISOR

CUBE 채팅방
  -> Gateway 공개 Agent ID
  -> Agent Resource 조회
       ├─ GENIE_*   -> 기존 Genie Chat/Agent Adapter
       └─ SUPERVISOR -> Supervisor Responses Adapter
  -> 정규화된 SSE 응답
```

CUBE는 에이전트 유형별 Databricks API를 직접 구분하지 않는다. Gateway가 Agent 유형을
조회하여 내부 상향 endpoint와 요청·응답 변환 방식을 결정한다.

Agent를 변경할 때 기존 Databricks Conversation을 다른 Agent에 재사용하지 않는다.
동일 CUBE 채팅방을 유지해야 하는 경우에도 Gateway Conversation은 Agent별로 분리한다.

---

## 3. 확정 설계 원칙

1. 기존 Genie 공개 API 인터페이스를 변경하지 않는다.
2. Supervisor는 별도 리소스 유형과 Adapter로 구현하되 인증, 감사, 라우팅, 오류 처리의
   공통 계층은 재사용한다.
3. CUBE에는 Databricks serving endpoint 이름 대신 Gateway 공개 `agent_id`를 제공한다.
4. Supervisor 등록 수를 코드로 제한하지 않으며 3개 이상의 endpoint를 지원한다.
5. Databricks 전용 SSE 이벤트와 continuation 구조는 Gateway 내부에서 정규화한다.
6. 외부 클라이언트의 Idempotency-Key를 필수로 요구하지 않는다.
7. 실행 식별과 중복 실행 판단을 구분한다. 모든 요청에는 Gateway `run_id`를 발급하고,
   진행 중 동일 요청은 `request_hash`로 제한적으로 차단한다.
8. 원격 호출 중 DB 트랜잭션을 유지하지 않는다.
9. 사용자 중단은 `conversation_id`가 아닌 `run_id` 단위로 처리한다.
10. Supervisor의 단일 upstream 호출을 즉시 강제 종료할 수 있다는 보장을 API 명세에
    포함하지 않는다.
11. 원본 Item·Streaming·MCP Approval 구조는 OpenAI Responses API 개념을 참고하되, CUBE가
    구현하는 외부 인터페이스는 Gateway 정규화 API와 SSE Event를 기준으로 한다.
12. Supervisor 답변에 포함된 Databricks 원본 근거 링크는 제거하지 않는다. 링크 접근 권한은
    Databricks가 검증하며 권한이 없는 외부 개발자·사용자는 원본 화면을 열 수 없다.

---

## 4. 리소스 및 라우팅 모델

### 4.1 확정 리소스 구조

현재 `genie_agt_mst`는 Agent 기본정보, 실제 Space/Agent ID, 시스템 매핑,
Conversation FK의 기준 테이블이다. Supervisor용 Master를 별도로 추가하면 공통 `agent_id`
조회마다 두 Master를 UNION하거나 다형 FK를 사용해야 하므로 권장하지 않는다.

확정안은 `genie_agt_mst`를 공통 `ai_agt_mst`로 일반화하고 기존 PK `agt_id`를 그대로
유지하는 것이다. PostgreSQL table rename과 컬럼 추가 migration으로 기존 PK·FK·데이터를
보존하고 애플리케이션 SQL을 단계적으로 변경한다.

대안 비교:

| 대안 | 장점 | 단점 | 판단 |
|---|---|---|---|
| 기존 `genie_agt_mst`에 Supervisor 컬럼만 추가 | 코드 변경 최소 | 테이블 이름과 의미 불일치, 향후 유형 확장 곤란 | 임시안 |
| Supervisor Master 별도 생성 | 기존 Genie 영향 최소 | 통합 Agent 조회, 시스템 매핑, Conversation FK가 복잡 | 비권장 |
| 공통 `ai_agt_mst`로 일반화 | 단일 `agent_id`, 기존 FK 보존, 통합 목록·라우팅 단순 | migration과 Repository SQL 변경 필요 | **확정** |

확정 Master는 다음 정보를 관리한다.

| 항목 | 의미 |
|---|---|
| `agt_id` | 기존 identity PK. 내부 FK와 조인에 사용 |
| `agt_cd` | Gateway가 외부에 제공하는 안정적인 논리 `agent_id` |
| `agt_typ_cd` | `GENIE` 또는 `SUPERVISOR` |
| `agt_mode_cd` | Genie인 경우 `CHAT`, `AGENT`, `BOTH`; Supervisor는 `SUPERVISOR` |
| `ws_id` | 상향 호출 Workspace 참조 |
| `dbx_space_id` | Genie Chat용 실제 Space ID |
| `dbx_agent_id` | Genie Agent mode 실제 Agent ID |
| `dbx_serving_ep_nm` | Supervisor 실제 serving endpoint 이름 |
| `long_task_use_yn` | Supervisor 장기작업 허용 여부 |
| `first_event_timeout_sec` | 첫 SSE 이벤트 제한시간 |
| `idle_timeout_sec` | 이벤트 idle 제한시간 |
| `total_timeout_sec` | continuation 포함 전체 Run 제한시간 |
| `max_continue_cnt` | continuation 최대 횟수 |
| `use_yn` | 사용 여부 |

단일 Agent 레코드에는 유형에 필요한 Databricks 식별자만 저장한다. 유형별 정합성은 CHECK
제약으로 보장한다.

```text
GENIE + CHAT       -> dbx_space_id 필수
GENIE + AGENT      -> dbx_agent_id 필수
GENIE + BOTH       -> dbx_space_id, dbx_agent_id 필수
SUPERVISOR         -> dbx_serving_ep_nm 필수
```

Supervisor 정책값은 Agent마다 달라질 가능성이 있고 라우팅 시 항상 함께 필요하므로 1차에는
별도 정책 테이블보다 Master 컬럼으로 관리한다. 정책 종류가 늘거나 환경별 override가 필요해질
때 `ai_agt_exec_plcy` 같은 별도 테이블로 분리한다.

시스템 매핑 테이블은 `sys_genie_agt_map`을 `sys_ai_agt_map`으로 일반화하고 기존
`sys_genie_agt_map_id`, `agt_id`, 유효기간, 기본 Agent, 우선순위 값을 보존한다. 한 시스템에
여러 Supervisor를 연결할 수 있으며 현재의 "시스템당 활성 기본 Agent 1개" 제약은 유지한다.

Supervisor endpoint는 Admin에서 등록·수정·비활성화하며 Secret과 Access Token 원문은
저장하지 않는다. endpoint 이름은 동일 Workspace 안에서 유일하므로 `(ws_id,
dbx_serving_ep_nm)` 부분 Unique Index를 권장한다.

### 4.1.1 논리 `agent_id` 전환 주의사항

현재 공개 Genie API는 URL의 실제 `dbx_space_id` 또는 `dbx_agent_id`로 Route를 조회한다.
신규 통합 SSE API는 `agt_cd`를 외부 논리 `agent_id`로 사용한다. 따라서 1차 migration에서
기존 Genie API 조회를 즉시 `agt_cd`로 바꾸지 않는다.

Repository에는 조회 목적을 분리한다.

```text
resolve_genie_route_by_upstream_id()  # 기존 /api/2.0/genie 호환 API
resolve_agent_by_code()               # 신규 통합 Agent API
```

이렇게 하면 기존 외부 API 명세를 유지하면서 신규 CUBE 통합 API만 논리 `agent_id`를 사용할
수 있다.

### 4.1.2 1차 migration 방향

1. `genie_agt_mst`를 `ai_agt_mst`로 rename하고 기존 `agt_id`·데이터·FK 보존
2. `agt_typ_cd`를 추가하고 기존 레코드를 `GENIE`로 backfill
3. Supervisor endpoint·timeout·continuation 정책 컬럼 추가
4. 기존 `agt_mode_cd` CHECK와 활성 리소스 CHECK를 유형별 조건으로 교체
5. `sys_genie_agt_map`을 `sys_ai_agt_map`으로 rename하고 기존 매핑 보존
6. 신규 설치 DDL과 기존 설치용 migration SQL을 분리 작성
7. 배포 전후 검증 SQL로 레코드 수, FK, 기본 Agent, 기존 Route 조회 결과 비교

### 4.2 CUBE 제공 메타데이터 예시

```json
{
  "agent_id": "healthcare-supervisor",
  "name": "헬스케어 분석 Agent",
  "agent_type": "SUPERVISOR",
  "capabilities": {
    "streaming": true,
    "long_task": true,
    "cancel": true
  }
}
```

`cancel=true`는 Gateway가 스트림과 후속 continuation을 중단할 수 있다는 의미다.
Databricks 내부 단일 도구 호출의 즉시 강제 종료를 의미하지 않는다.

### 4.3 CUBE Agent 등록·선택 기준

CUBE 사용자와 CUBE 관리 화면에는 Databricks Supervisor serving endpoint를 직접
선택시키지 않는다. 사용자가 선택하는 대상은 Gateway에 등록된 논리 Agent이며,
Gateway Admin에서 논리 Agent와 실제 Databricks 리소스를 연결한다.

```text
CUBE 사용자
  -> "헬스케어 분석 Agent" 선택
  -> CUBE가 Gateway agent_id 전달
  -> Gateway가 agent_type과 내부 리소스 조회
       ├─ GENIE      -> dbx_space_id / dbx_agent_id
       └─ SUPERVISOR -> serving_endpoint_name
```

역할별 노출 범위:

| 대상 | 확인·선택하는 값 |
|---|---|
| CUBE 일반 사용자 | Agent 이름, 설명, 아이콘, 사용 가능 여부 |
| CUBE Agent 관리자 | Gateway `agent_id`, Agent 유형, 기능 정보 |
| Gateway 관리자 | 실제 Workspace, Space/Agent ID 또는 Supervisor endpoint |
| Gateway 내부 Adapter | 선택된 리소스의 실제 Databricks 호출 경로 |

CUBE에 실제 endpoint URL을 전달하면 endpoint 변경·재배포 시 CUBE 설정도 함께 수정해야
하고, Databricks 내부 구조와 권한 정보가 외부 시스템에 결합된다. 따라서 실제 endpoint는
Gateway 설정으로만 관리한다.

CUBE가 저장해야 하는 최소값은 Gateway `agent_id`다. 목록 API는 표시 정보와 함께
`agent_type`, `interaction_modes`, `capabilities`를 반환할 수 있지만 실제 Databricks endpoint
URL이나 이름은 반환하지 않는다.

```json
{
  "agent_id": "healthcare-supervisor",
  "name": "헬스케어 분석 Agent",
  "agent_type": "SUPERVISOR",
  "interaction_modes": ["SUPERVISOR"],
  "capabilities": {
    "streaming": true,
    "long_task": true,
    "cancel": true
  }
}
```

일반 Genie Agent는 `interaction_modes`에 `CHAT`, `AGENT` 또는 두 값을 반환한다. 두 모드를
지원하는 Agent에서 CUBE가 사용자에게 모드까지 선택시킬지, 등록 시 정한 기본 모드를 사용할지는
CUBE 화면 요구사항 확인 후 확정한다.

---

## 5. 외부 API 인터페이스 정의

신규 CUBE 연계에는 Gateway 공개 Agent ID를 기준으로 한 통합 인터페이스를 제공한다.
Supervisor의 Databricks 경로·이벤트 형식은 Gateway 내부 구현으로 감추며 기존
`/api/2.0/genie/...` API는 변경하지 않는다.

### 5.0 CUBE 호출 원칙

CUBE가 Supervisor Agent를 선택한 뒤 Databricks endpoint URL로 직접 호출하는 방식은 사용하지
않는다. 다음 두 방식 중 **통합 Agent API**를 적용한다.

| 방식 | CUBE 처리 | 평가 |
|---|---|---|
| 유형별 경로 분기 | `agent_type`을 보고 Genie/Supervisor Gateway 경로를 각각 호출 | 1차 구현은 가능하나 CUBE 결합도 증가 |
| 통합 Agent API | 선택한 `agent_id`를 동일 경로로 호출하고 Gateway가 내부 Adapter 결정 | **확정** |

기존 `/api/2.0/genie/...` API는 외부 개발자에게 이미 제공된 인터페이스이므로 그대로 유지한다.
통합 Agent API는 CUBE AI Agent 기능을 위한 신규 인터페이스로 추가하며, 기존 경로를 변경하거나
제거하지 않는다.

### 5.1 공통 요청 규칙

공통 query parameter:

| 이름 | 필수 | 설명 |
|---|---|---|
| `source_system` | Y | 등록된 호출 시스템 코드. 예: `CUBE` |
| `user_id` | Y | 호출 시스템 안에서 사용자를 유일하게 식별하는 값 |

공통 request header:

| 이름 | 필수 | 설명 |
|---|---|---|
| `Authorization` 또는 기존 API Key header | Y | 현재 Gateway 인증 방식 유지 |
| `Accept: text/event-stream` | 응답 생성 시 Y | streaming 응답 명시 |
| `Idempotency-Key` | N | 외부 시스템이 고유 키를 제공할 수 있을 때 사용 |
| `x-request-id` | N | 유효한 값은 유지하고 미지정·형식 오류 시 Gateway가 새 값 발급 |

`user_id`는 CUBE 테이블의 특정 PK를 의미하지 않는다. Gateway의 Conversation 소유권 확인과
Supervisor 사용자 컨텍스트 전달에 사용하는 외부 사용자 식별자다.

### 5.2 응답 생성

`POST /api/v1/agents/{agent_id}/responses?source_system=CUBE&user_id=user-10001`

```json
{
  "conversation_id": "cnvs_01...",
  "input": "이번 달 주요 지표와 이상 징후를 분석해줘",
  "client_message_id": null,
  "long_task": true
}
```

| 필드 | 필수 | 규칙 |
|---|---|---|
| `conversation_id` | N | 없으면 새 Gateway Conversation 생성, 있으면 소유권 검증 |
| `input` | Y | 1차에는 비어 있지 않은 문자열만 지원 |
| `interaction_mode` | 조건부 | `BOTH` Genie Agent만 `CHAT` 또는 `AGENT` 지정. 단일 모드는 생략 가능 |
| `client_message_id` | N | CUBE가 제공할 수 있을 때 중복 방지에 우선 사용 |
| `long_task` | N | Supervisor이면서 기능이 활성화된 Agent에만 적용 |
| `metadata` | N | 확장 정보. Gateway 허용 목록에 포함된 키만 내부 변환 대상으로 사용 |

Supervisor 요청에 `interaction_mode`를 전달하거나 지원하지 않는 Agent에 `long_task=true`를
전달하면 묵시적으로 무시하지 않고 `INVALID_PARAMETER` 오류를 반환한다.

입력 정규화 정책은 다음과 같이 적용한다.

- 최상위 JSON은 엄격하게 검증하고 정의되지 않은 필드는 `400 INVALID_PARAMETER_VALUE` 반환
- 알려진 필드명의 단순 오타로 판단할 수 있으면 오류 `message`에 잘못된 필드와 권장 필드명 표시
- `metadata`는 object만 허용하며 미지원 키는 오류 없이 제거
- 미지원 `metadata` 키와 값은 Databricks 요청·감사 payload·로그에 전달하거나 저장하지 않음
- Gateway가 Agent 유형과 capability를 확인하여 Databricks에 필요한 최소 JSON만 생성
- 외부 요청 JSON을 그대로 upstream에 pass-through하지 않음

예를 들어 `long_taks`는 최상위 필드 오타이므로 다음과 같이 거절한다.

```json
{
  "error_code": "INVALID_PARAMETER_VALUE",
  "message": "Unknown field 'long_taks'. Did you mean 'long_task'?"
}
```

반면 `metadata`의 `unsupported_key`는 외부 요청을 실패시키지 않고 제거한다. 1차에는 임의의
endpoint별 요청 JSON template을 DB에 저장하지 않는다. Supervisor별 추가 입력이 실제로
필요해질 때만 공개 metadata key와 upstream `custom_inputs` key를 연결하는 제한된 allowlist
설정을 추가한다.

요청 성공 시 `text/event-stream`으로 응답한다. 첫 이벤트는 반드시 `run.started`이며
Gateway가 발급한 공개 `run_id`와 `conversation_id`를 전달한다. DB의 `run_uid`, `cnvs_uid`가
각각 외부 `run_id`, `conversation_id`에 대응한다.

```text
id: evt_01...
event: run.started
data: {"schema_version":"1.0","event_id":"evt_01...","sequence":1,"run_id":"run_01...","conversation_id":"cnvs_01...","agent_id":"healthcare-supervisor","timestamp":"2026-09-16T10:00:00Z","data":{"status":"STREAMING"}}
```

응답 header:

```text
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache, no-transform
X-Accel-Buffering: no
x-request-id: <gateway-trace-id>
```

SSE `data`는 항상 단일 JSON object다. Gateway는 Databricks가 제공하지 않은 공개 heartbeat를
임의 생성하지 않는다. 따라서 이벤트가 없는 구간에 `: ping` comment가 주기적으로
전달된다고 가정할 수 없으며, CUBE는 HTTP 연결·Run 상태·아래 timeout 정책으로 상태를 판단한다.

### 5.3 실행 중단

`POST /api/v1/agents/{agent_id}/runs/{run_id}/cancel?source_system=CUBE&user_id=user-10001`

- 요청 사용자의 Conversation·Run 소유권 확인
- 실행 상태를 `CANCEL_REQUESTED`로 변경
- 활성 upstream HTTP stream 취소 시도
- 이후 `task_continue_response` 전송 차단
- 종료가 확인되면 `CANCELLED`로 변경
- 활성 Run의 중단 접수는 HTTP `202`, 이미 Terminal인 Run은 HTTP `200`과 현재 상태 반환
- 같은 Run에 대한 반복 중단 요청은 새 상태를 만들지 않는 멱등 동작

응답 예시:

```json
{
  "run_id": "run_01...",
  "conversation_id": "cnvs_01...",
  "status": "CANCEL_REQUESTED",
  "cancel_requested_at": "2026-09-16T10:01:30Z",
  "trace_id": "trace_01..."
}
```

### 5.4 상태 및 이력 조회

```text
GET /api/v1/agents/{agent_id}/conversations/{conversation_id}
GET /api/v1/agents/{agent_id}/conversations/{conversation_id}/items?cursor=...&limit=50
GET /api/v1/agents/{agent_id}/runs/{run_id}
```

SSE 연결이 끊어진 클라이언트는 `run_id`로 최종 상태와 저장된 item을 조회한다. 1차에서는
과거 delta 전체 재생을 보장하지 않는다.

Run 조회 응답 예시:

```json
{
  "run_id": "run_01...",
  "conversation_id": "cnvs_01...",
  "agent_id": "healthcare-supervisor",
  "status": "COMPLETED",
  "started_at": "2026-09-16T10:00:00Z",
  "last_event_at": "2026-09-16T10:02:15Z",
  "completed_at": "2026-09-16T10:02:15Z",
  "error": null,
  "trace_id": "trace_01..."
}
```

Item 목록은 완료된 정규화 Item을 `sequence` 순서로 반환한다. `limit` 기본값은 50, 최댓값은
100으로 두고 opaque cursor 방식을 사용한다.

### 5.5 Run 상태 코드

| 상태 | Terminal | 의미 |
|---|---:|---|
| `QUEUED` | N | Run 생성 완료, upstream 호출 전 |
| `STREAMING` | N | upstream 응답 수신 및 외부 SSE 전달 중 |
| `CONTINUING` | N | 장기작업 checkpoint 수신 후 continuation 처리 중 |
| `CANCEL_REQUESTED` | N | 사용자 중단 접수, 추가 continuation 차단 |
| `COMPLETED` | Y | 정상 완료 |
| `CANCELLED` | Y | 중단 처리 완료 |
| `TIMED_OUT` | Y | Gateway timeout 또는 전체 deadline 초과 |
| `FAILED` | Y | timeout·사용자 중단 이외 오류 종료 |

`QUEUED`에서 첫 upstream 호출 직전에 `STREAMING`으로 전이한다. continuation을 시작할 때
`CONTINUING`, 새 upstream stream을 수신하면 다시 `STREAMING`으로 전이할 수 있다.

### 5.6 정규화 SSE 공통 Envelope

모든 업무 Event의 `data` JSON은 다음 필드를 갖는다.

| 필드 | 의미 |
|---|---|
| `schema_version` | 이벤트 명세 버전. 1차 `1.0` |
| `event_id` | Gateway Event 공개 식별자. SSE `id`와 동일 |
| `sequence` | Run 안에서 1부터 증가하는 순번 |
| `run_id` | Gateway 공개 Run ID |
| `conversation_id` | Gateway 공개 Conversation ID |
| `agent_id` | Gateway 논리 Agent ID |
| `timestamp` | UTC ISO-8601 시각 |
| `data` | Event 유형별 payload |

원본 Databricks event 이름이나 payload는 외부에 병행 제공하지 않는다. 필요한 upstream ID는
Gateway 내부 Item/Event 이력에 저장하고 외부 payload에는 기능 구현에 필요한 값만 노출한다.

### 5.7 정규화 SSE 이벤트

| 이벤트 | `data` 주요 필드 | 의미 |
|---|---|---|
| `run.started` | `status` | Gateway Run·Conversation 식별자 발급 |
| `message.delta` | `item_id`, `delta` | Assistant 텍스트 증분 |
| `message.completed` | `item_id`, `text` | 완성된 Assistant 메시지 |
| `tool.started` | `item_id`, `tool_type`, `name`, `title` | 하위 Agent 또는 도구 실행 시작 |
| `tool.completed` | `item_id`, `tool_type`, `name`, `title`, `status` | 도구 실행 완료. 대용량 결과는 Item 조회로 제공 |
| `item.completed` | `item_id`, `item_type`, `title`, `content` | 표·출처·파일 등 정규화된 완료 Item |
| `run.continuing` | `continue_count`, `step` | `long_task` continuation 진행 |
| `approval.required` | `approval_id`, `name`, `server_label`, `arguments` | Web Search/MCP 실행 전 사용자 승인 대기 |
| `run.completed` | `status`, `usage` | 정상 완료 |
| `run.failed` | `status`, `error` | 오류 종료 |
| `run.cancelled` | `status` | 사용자 중단 처리 완료 |
| `run.timed_out` | `status`, `error` | Gateway timeout 또는 deadline 초과 |

`message.delta`는 DB에 전부 저장하지 않으며 `message.completed`의 완성 텍스트를
`ai_run_item_hst`에 저장한다. 표처럼 큰 결과는 `item.completed`에 요약·식별자만 전달하고
전체 데이터는 Item 조회 API로 가져오게 한다.

Terminal Event는 Run당 하나만 외부에 전송한다. 연결 종료 자체를 완료로 간주하지 않고
`run.completed`, `run.failed`, `run.cancelled`, `run.timed_out` 중 하나를 수신해야 한다.

### 5.8 오류 형식

SSE 시작 전 오류는 기존 Gateway 공통 JSON 오류 형식을 유지하고 추적값은 응답
`x-request-id` header로 반환한다.

```json
{
  "error_code": "CONVERSATION_ACCESS_DENIED",
  "message": "Conversation access is denied."
}
```

SSE 시작 후에는 HTTP 상태를 바꿀 수 없으므로 `run.failed` 또는 `run.timed_out`의 `error`로
전달한다.

```json
{
  "status": "FAILED",
  "error": {
    "error_code": "UPSTREAM_ERROR",
    "message": "The agent execution failed.",
    "retryable": true,
    "request_id": "trace_01..."
  }
}
```

외부 `message`에는 token, endpoint URL, 원본 DB 오류와 stack trace를 포함하지 않는다.
상세 오류는 `trace_id`로 Gateway 로그와 감사 이력에서 확인한다.

### 5.9 HTTP 상태 코드

| HTTP | 오류 코드 예시 | 조건 |
|---:|---|---|
| 400 | `INVALID_PARAMETER` | 잘못된 mode, 빈 input, 미지원 long_task |
| 401 | `UNAUTHENTICATED` | 인증 실패 |
| 403 | `AGENT_ACCESS_DENIED`, `CONVERSATION_ACCESS_DENIED` | Agent 또는 대화·Run 소유권 없음 |
| 404 | `AGENT_NOT_FOUND`, `CONVERSATION_NOT_FOUND`, `RUN_NOT_FOUND` | 리소스 없음 |
| 409 | `RUN_ALREADY_ACTIVE` | 정책상 기존 활성 Run으로 연결할 수 없는 충돌 |
| 429 | `RATE_LIMITED` | Gateway 또는 upstream 제한 |
| 502 | `UPSTREAM_ERROR` | Databricks 오류·비정상 응답 |
| 503 | `UPSTREAM_UNAVAILABLE` | endpoint 준비 안 됨·일시적 연결 실패 |
| 504 | `UPSTREAM_TIMEOUT` | SSE 시작 전 첫 이벤트 timeout |

---

## 6. Conversation·Run·Event 데이터 모델

장기작업과 사용자 중단은 Conversation 소유권만으로 관리할 수 없다. 최소한 Run 상태와
완료 Item 및 실행 Event 저장 구조가 필요하다. 현재 스키마를 기준으로 다음 구조를 권고한다.

```text
ai_cnvs_mst
  ├─ genie_msg_hst ─ genie_msg_atch_dtl / genie_msg_stts_hst
  │    기존 Genie Chat 메시지 정규화 전용
  └─ ai_run_mst
       ├─ ai_run_item_hst   완료된 출력 Item
       └─ ai_run_evt_hst    실행 수명주기·제어 Event
```

Conversation은 모든 Agent 유형의 공통 소유권과 대화 경계를 담당하고, Run은 한 번의 실행과
중단·timeout·continuation을 담당한다. Item과 Event는 서로 수명주기와 조회 목적이 다르므로
한 테이블로 합치지 않는다.

### 6.1 Conversation

- `genie_cnvs_mst`를 공통 `ai_cnvs_mst`로 일반화하고 기존 `cnvs_id`, FK와 데이터를 보존
- Gateway 공개 식별자인 `cnvs_uid`를 ULID/UUID 계열로 추가하고 Unique로 관리
- 기존 Genie API는 지금처럼 upstream `dbx_cnvs_id`를 외부 `conversation_id`로 사용하여
  기존 API 명세를 유지
- 신규 통합 Agent API는 Gateway `cnvs_uid`를 외부 `conversation_id`로 사용
- `dbx_cnvs_id`는 Agent 유형에 따라 존재할 수 있는 nullable upstream 식별자로 유지
- `cnvs_mode_cd`는 `CHAT`, `AGENT`, `SUPERVISOR`로 확장
- 소유권 기준은 `api_clnt_id + usr_id + agt_id`이며, 모든 조회는 이 조건을 검증
- `ext_sess_id`에는 CUBE 채팅방·스레드 ID를 선택적으로 저장할 수 있으나 소유권 식별자로
  사용하지 않음
- 동일 CUBE 채팅방에서 Agent를 변경하면 새 Gateway Conversation을 생성하고 기존
  Conversation을 다른 Agent에 재사용하지 않음

`ext_sess_id` 하나에 여러 Agent Conversation이 연결될 수 있으므로 이를 Unique로 만들지
않는다. 대화 목록은 CUBE 세션이 아니라 `api_clnt_id`, `usr_id`, `agt_id`, 최근 메시지·실행
시각을 기준으로 조회한다.

Chat·Agent·Supervisor는 원본 저장소와 상세 조회 API가 다르므로 Gateway 공개 대화 목록도
모드별로 별도 구성한다. CUBE가 한 화면에서 통합 이력을 보여줄 경우 각 목록을 화면 모델로
합치되 `agent_id`, 모드, 원본 `conversation_id`를 함께 보관해 상세 조회 경로를 결정한다.

### 6.2 Run

한 번의 사용자 요청과 그 요청에서 파생된 continuation 전체를 하나의 Run으로 관리한다.
`run_id`는 DB identity PK, `run_uid`는 API에 노출하는 안정적인 식별자로 구분한다.

권장 `ai_run_mst` 필드:

| 필드 | 의미 |
|---|---|
| `run_id` | DB 내부 identity PK |
| `run_uid` | API에 노출하는 ULID/UUID 계열 Unique 식별자 |
| `cnvs_id` | `ai_cnvs_mst` FK |
| `request_hash` | 제한적 중복 판단용 SHA-256 값 |
| `run_stts_cd` | 실행 상태 |
| `dbx_response_id` | Databricks 응답 식별자 |
| `continue_request_id` | 최근 continuation 식별자 |
| `continue_cnt` | continuation 수행 횟수 |
| `str_dt`, `last_evt_dt` | 실행·최근 이벤트 시간 |
| `deadline_dt` | continuation을 포함한 전체 실행 종료 시각 |
| `cancel_req_dt`, `end_dt` | 중단 요청·종료 감사 정보 |
| `err_cd`, `err_msg_cn`, `trace_id` | 오류 추적 정보 |

Run에 `api_clnt_id`, `usr_id`, `agt_id`를 중복 저장하지 않고 Conversation 조인으로 소유권과
Agent를 확인한다. 변경 이력의 불변 snapshot이 별도로 필요해질 때만 audit JSON 또는 snapshot
컬럼을 추가한다.

기존 `api_req_hst`에는 nullable `run_id` FK를 추가한다. 최초 streaming 요청, continuation,
중단 요청은 각각 독립된 HTTP 요청 이력이지만 같은 Run을 참조할 수 있다.

### 6.3 Item/Event

#### `ai_run_item_hst`

- 최종 Message, Tool 결과, 표·파일 메타데이터처럼 다시 조회할 완료 Item 저장
- `run_id`, `item_seq_no`, `dbx_item_id`, `item_typ_cd`, `item_stts_cd`, `role_cd`,
  `text_cn`, `item_data` 관리
- `(run_id, item_seq_no)` Unique와 upstream ID가 있을 때 `(run_id, dbx_item_id)` 부분
  Unique 적용
- streaming delta를 매번 INSERT하지 않고 완료된 정규화 결과와 필요한 원본 JSON만 저장

#### `ai_run_evt_hst`

- 실행 시작, 도구 시작·완료, continuation 요청, 중단, timeout, 완료·실패 등 수명주기 Event 저장
- `run_id`, `evt_seq_no`, `evt_typ_cd`, `dbx_evt_id`, `evt_data`, `reg_dt` 관리
- `(run_id, evt_seq_no)` Unique로 한 Run 안의 순서를 보장
- 1차에는 제어·상태 Event만 영속화하고 모든 text delta는 저장하지 않음
- 향후 `Last-Event-ID` 기반 무손실 SSE 재생이 요구되면 delta 보존 정책과 보존기간을 별도로
  확정

Item/Event 공통으로 token, Secret, 불필요한 개인정보 원문을 저장하지 않는다. 원본 JSON을
저장할 때도 기존 API 감사 이력과 동일한 마스킹·보존 정책을 적용한다.

### 6.4 상태 전이

```text
QUEUED
  -> STREAMING
  -> CONTINUING -> STREAMING
  -> COMPLETED

  -> CANCEL_REQUESTED -> CANCELLED
  -> TIMED_OUT
  -> FAILED
```

Terminal 상태에서 다른 Terminal 상태로 임의 변경하지 않는다. Run 상태 변경은 조건부
UPDATE 또는 잠금으로 중복 완료·취소 경쟁을 방지한다.

### 6.5 인덱스와 무결성 권고

| 대상 | 권고 인덱스·제약 |
|---|---|
| `ai_cnvs_mst` | `UNIQUE(cnvs_uid)`, 활성 대화 조회용 `(api_clnt_id, usr_id, agt_id, last_msg_dt DESC)` |
| `ai_run_mst` | `UNIQUE(run_uid)`, `(cnvs_id, str_dt DESC)`, `(run_stts_cd, deadline_dt)` |
| 활성 중복 요청 | `(cnvs_id, request_hash)`에 활성 상태만 적용하는 Partial Unique Index |
| `ai_run_item_hst` | `UNIQUE(run_id, item_seq_no)`, upstream item ID 부분 Unique |
| `ai_run_evt_hst` | `UNIQUE(run_id, evt_seq_no)`, `(run_id, reg_dt)` |
| `api_req_hst` | `(run_id, req_dt DESC)` 부분 인덱스 |

활성 중복 요청의 대상 상태는 `QUEUED`, `STREAMING`, `CONTINUING`, `CANCEL_REQUESTED`이다.
완료 후 짧은 dedupe window 처리는 Partial Unique Index만으로 해결하지 않고 Run 생성
트랜잭션에서 최근 종료 Run을 조회하여 판단한다.

### 6.6 기존 테이블 재사용 범위

| 기존 테이블 | 처리 방향 | 이유 |
|---|---|---|
| `genie_cnvs_mst` | `ai_cnvs_mst`로 일반화 | 공통 대화 소유권과 이력 목록의 기준 |
| `genie_msg_hst` | 이름과 역할 유지 | 기존 Genie Chat 메시지 정규화 전용 |
| `genie_msg_atch_dtl` | 유지 | Genie Chat Attachment 구조에 특화 |
| `genie_msg_stts_hst` | 유지 | Genie Message 상태 이력에 특화 |
| `api_req_hst` | `run_id` FK 추가 | 한 Run에 속한 최초·continuation·중단 HTTP 요청 추적 |

Supervisor Item/Event를 `genie_msg_hst`와 Attachment 테이블에 저장하지 않는다. 두 응답
모델을 하나로 강제하면 nullable 컬럼과 유형 분기가 늘고, 향후 Databricks 이벤트 변화가 기존
Genie Chat 이력에 영향을 준다.

---

## 7. 중복 실행 방지

외부 메신저가 안정적인 `client_message_id`나 Idempotency-Key를 제공할 수 없다는 전제로
다음 방식을 사용한다.

1. 모든 요청에 Gateway `run_id` 발급
2. 다음 값을 정규화하여 `request_hash` 생성

```text
client_id + user_id + conversation_id + agent_id + normalized_input
```

3. 같은 hash의 `QUEUED`, `STREAMING`, `CONTINUING`, `CANCEL_REQUESTED` Run이 있으면 새
   upstream 실행을 만들지 않고 기존 Run을 반환
4. 짧은 dedupe window 안의 동일 요청도 기존 Run으로 연결
5. 완료된 동일 질문은 window 이후 사용자의 의도적인 재질문으로 보고 새 Run 허용

이 방식은 완전한 멱등성을 보장하지 않는다. 외부 시스템이 추후 고유 키를 제공하면
`Idempotency-Key` 또는 `client_message_id`를 선택적으로 받아 더 강한 중복 방지를 적용한다.

---

## 8. `long_task` 처리

### 8.1 Adapter 구성

기존 `DatabricksTokenProvider`와 `normalize_databricks_host()`는 재사용하고, Genie 경로에
특화된 `DatabricksGenieClient`는 직접 확장하지 않는다.

```text
Unified Agent API
  -> AgentResponseService
       -> RunOrchestrator
            ├─ GenieAdapter
            └─ SupervisorAdapter
                 ├─ DatabricksSupervisorClient
                 ├─ SupervisorSseParser
                 └─ SupervisorEventNormalizer
```

| 컴포넌트 | 책임 |
|---|---|
| `AgentResponseService` | 인증 컨텍스트·Agent 조회·Conversation 소유권·공통 응답 시작 |
| `RunOrchestrator` | Run 생성, 상태 전이, deadline, continuation, 중단 경쟁 제어 |
| `SupervisorAdapter` | Gateway canonical 요청을 Databricks 요청으로 변환 |
| `DatabricksSupervisorClient` | OAuth, HTTP streaming, 응답 종료와 연결 정리 |
| `SupervisorSseParser` | chunk·멀티라인 경계를 고려한 원본 SSE frame 파싱 |
| `SupervisorEventNormalizer` | 원본 Responses event를 Gateway SSE 및 Item/Event로 변환 |

Client는 HTTP와 인증만 담당하고 DB 상태를 직접 변경하지 않는다. Parser는 순수한 frame 파싱,
Normalizer는 event 변환만 담당하며 상태 전이는 Run Orchestrator가 단일 책임으로 수행한다.

### 8.2 Upstream 호출

등록된 `dbx_serving_ep_nm`을 기준으로 다음 Model Serving 경로를 호출한다.

```text
POST {workspace_host}/serving-endpoints/{endpoint_name}/invocations
Authorization: Bearer <workspace OAuth token>
Accept: text/event-stream
Content-Type: application/json
```

실제 endpoint의 Get code가 `/serving-endpoints/responses`와 body의 `model` 방식을 제공하는
경우에도 외부 API 명세는 변경하지 않는다. 두 Databricks 호출 형식의 차이는
`DatabricksSupervisorClient` 설정으로 흡수하고 Agent Master에는 endpoint 이름만 저장한다.

최초 요청 변환 예시:

```json
{
  "input": [
    {
      "role": "user",
      "content": "이번 달 주요 지표와 이상 징후를 분석해줘"
    }
  ],
  "stream": true,
  "databricks_options": {
    "long_task": true,
    "return_trace": true
  },
  "custom_inputs": {
    "user_id": "user-10001",
    "source_system": "CUBE",
    "gateway_conversation_id": "cnvs_01...",
    "gateway_run_id": "run_01..."
  }
}
```

- `stream`은 Gateway가 항상 `true`로 강제
- `long_task`는 외부 요청과 Agent 정책이 모두 허용할 때만 `true`
- `custom_inputs` 지원 여부와 필드명은 실제 Supervisor endpoint의 input schema로 배포 전 검증
- endpoint가 받지 않는 `custom_inputs`는 전송하지 않으며 프롬프트 문자열에 내부 식별자를
  임의 삽입하지 않음
- Access Token, 실제 endpoint 이름과 내부 trace 원문은 외부 SSE에 포함하지 않음

Supervisor endpoint가 서버 측 Conversation을 보장한다고 가정하지 않는다. 후속 질문의
upstream `input`은 Gateway에 저장된 완료 Message/Item을 기준으로 재구성한다. 전 대화 원문을
무제한 전송하지 않고 다음 기준을 적용한다.

- 최신 20 turn 또는 40,000자 중 먼저 도달하는 범위만 포함
- 1 turn은 사용자 요청과 그에 대응하는 Assistant 최종 응답 한 쌍으로 계산
- 현재 사용자 질문은 제한과 관계없이 항상 포함
- 제한을 초과하면 가장 오래된 완료 turn부터 제외
- Tool 원본 payload 전체가 아니라 최종 메시지와 필요한 결과 요약만 포함
- 1차에는 오래된 대화 자동 요약을 수행하지 않음
- 제한값은 Agent 정책으로 override할 수 있으나 Databricks 요청 payload 상한보다 항상 작게 유지

### 8.3 SSE 수신과 정규화

Parser는 다음 조건을 모두 처리한다.

- HTTP chunk 하나에 여러 SSE frame이 포함되는 경우
- 하나의 frame이 여러 chunk로 분리되는 경우
- 여러 `data:` line으로 구성된 event
- `\r\n`과 `\n` line ending
- comment heartbeat와 빈 data
- gzip/br가 HTTP client에서 해제된 뒤 전달되는 bytes
- 알 수 없는 event type

알 수 없는 원본 event는 즉시 실패시키지 않고 내부 `ai_run_evt_hst`에 제한적으로 기록한 뒤
외부 전송은 생략한다. 단, terminal 판단에 필요한 필수 event를 끝까지 받지 못하면
`UPSTREAM_PROTOCOL_ERROR`로 실패한다.

### 8.4 Continuation 처리

Supervisor 요청은 `stream=true`, 필요 시 `databricks_options.long_task=true`로 호출한다.
Gateway는 `response.output_item.done`의 Item 유형이 `task_continue_request`이면 다음을
수행한다.

1. 원본 응답·Run·continuation 식별자 저장
2. 현재 upstream stream을 정상 종료하고 Run을 `CONTINUING`으로 조건부 변경
3. 취소 요청, 전체 deadline과 최대 continuation 횟수 확인
4. 외부 SSE에 `run.continuing` 이벤트 전달
5. 원본 `task_continue_request`와 대응하는 `task_continue_response`를 포함해 후속 요청
6. 새 upstream event 수신 시 `STREAMING`으로 복귀
7. 최종 완료 또는 Terminal 상태까지 반복

후속 요청의 핵심 Item:

```json
[
  {
    "type": "task_continue_request",
    "id": "continue_01...",
    "step": 16
  },
  {
    "type": "task_continue_response",
    "continue_request_id": "continue_01..."
  }
]
```

`continue_request_id`가 일치하지 않거나 같은 요청 ID가 두 번 처리되면
`UPSTREAM_PROTOCOL_ERROR`로 종료한다. continuation 전환과 `continue_cnt` 증가는 같은 DB
트랜잭션에서 조건부 UPDATE로 처리하여 중복 후속 호출을 막는다.

한 번의 모델 또는 도구 호출 중에는 checkpoint가 발생하지 않을 수 있다. 따라서
`long_task`를 사용하더라도 단일 호출 timeout과 전체 Run deadline을 별도로 관리한다.

### 8.5 재시도 정책

- Run 생성 전 OAuth token 발급 실패는 upstream 실행이 시작되지 않았으므로 제한적으로 재시도
- 401은 Token cache를 한 번 무효화하고 재발급 후 한 번만 재시도
- 연결 수립 전 429·502·503·504는 `Retry-After`와 deadline 범위 안에서 제한적으로 재시도
- POST body가 upstream에 전달됐을 가능성이 있거나 SSE event를 하나라도 수신한 뒤에는 자동
  재시도하지 않음
- continuation도 동일 원칙을 적용하며 `continue_request_id`만으로 upstream 멱등성을
  가정하지 않음

재시도 횟수와 지연은 Run Event에 기록하되 외부에는 동일한 `run_id`를 유지한다.

### 8.6 외부 SSE 연결과 Run 실행 분리

외부 `StreamingResponse` generator가 Supervisor upstream 실행을 직접 소유하지 않는다.
Run Orchestrator는 독립 Task로 실행하고 외부 SSE writer는 내부 bounded queue를 구독한다.

```text
Supervisor upstream -> Run Orchestrator -> DB Item/Event
                                      └-> bounded event queue -> CUBE SSE
```

- CUBE 연결이 끊어져도 명시적 Cancel 요청이 없으면 Run은 계속 진행
- 구독자가 사라지면 text delta를 메모리에 무한 적재하지 않고 완료 Item·상태만 DB에 저장
- Cancel은 프로세스 메모리 flag와 DB의 `CANCEL_REQUESTED` 상태를 함께 확인
- DB 상태를 권위 상태로 사용하여 Cancel 요청이 다른 App instance에 도착해도 다음 Event 또는
  continuation 경계에서 중단 가능
- App 프로세스 재시작 후 진행 중 upstream stream을 이어받는 기능은 1차 범위에서 제외
- 시작 시 오래된 비종료 Run을 조회하여 deadline 초과는 `TIMED_OUT`, 실행 Task 유실은
  `FAILED/ORCHESTRATOR_RESTARTED`로 정리하는 reconciliation 수행

완전한 실행 복구와 instance 간 즉시 취소가 필요해지면 2차에서 durable queue/worker와 lease
구조를 도입한다. 1차에는 DB 상태 기반 복구와 endpoint별 동시 실행 제한을 적용한다.

참고 문서:
https://docs.databricks.com/gcp/en/agents/agent-bricks/multi-agent-supervisor-long-running-tasks

---

## 9. Timeout과 사용자 중단

### 9.1 Timeout 구분

| 구분 | 초기 권장값 | 처리 |
|---|---:|---|
| 연결 timeout | 10초 | endpoint 접속 실패 |
| 첫 이벤트 timeout | 60초 | 최초 SSE 이벤트 미수신 |
| idle timeout | 120초 | 장시간 이벤트 없음 |
| 전체 Run deadline | 30분 | continuation 포함 전체 제한 |
| continuation 최대 횟수 | 20회 | 무한 반복 차단 |

값은 환경변수 또는 Agent별 정책으로 관리한다. 기존 Genie의
`UPSTREAM_READ_TIMEOUT_SECONDS`와 Supervisor Run deadline의 의미를 혼용하지 않는다.

timeout 판정 책임:

| 구분 | 판정 위치 | 종료 처리 |
|---|---|---|
| 연결 timeout | `DatabricksSupervisorClient` | SSE 시작 전 HTTP 504 또는 실행 중 `run.timed_out` |
| 첫 이벤트 timeout | `RunOrchestrator` | upstream close 후 `TIMED_OUT` |
| idle timeout | `RunOrchestrator` | 최근 정상 frame 시각 기준 `TIMED_OUT` |
| 전체 deadline | `RunOrchestrator` | continuation 여부와 무관하게 `TIMED_OUT` |
| continuation 한도 | `RunOrchestrator` | `CONTINUATION_LIMIT_EXCEEDED`로 `FAILED` |

Gateway가 별도 heartbeat comment를 발행하지 않으므로 `last_evt_dt`와 idle timeout은 실제
Databricks/Gateway 업무 Event 기준으로만 갱신·판정한다. 인프라 proxy의 idle timeout은 Agent의
최대 무이벤트 구간보다 충분히 길게 설정한다.

### 9.2 사용자 중단 의미

Gateway가 보장하는 동작:

- 브라우저 또는 CUBE에 대한 SSE 응답 중단
- 활성 upstream HTTP stream 취소 시도
- Run 상태와 중단 요청 시각 저장
- 아직 실행하지 않은 continuation 차단
- 외부 조회 API에서 취소 상태 제공

Gateway가 보장하지 않는 동작:

- 이미 Databricks 내부에서 수행 중인 단일 모델·도구 호출의 즉시 강제 종료

Custom UI는 `AbortController`로 브라우저 stream을 닫기 전에 Cancel API를 호출한다.

### 9.3 중단·완료 경쟁 조건

중단과 upstream 완료가 동시에 발생할 수 있으므로 다음 우선순위를 적용한다.

1. Run이 이미 `COMPLETED`, `FAILED`, `TIMED_OUT`, `CANCELLED`이면 Terminal 상태 유지
2. `CANCEL_REQUESTED` 이후 수신한 continuation 요청은 실행하지 않음
3. `CANCEL_REQUESTED` 이후 upstream 정상 완료가 먼저 확정되면 `COMPLETED`를 허용하고 Cancel
   응답에는 실제 최종 상태 반환
4. upstream stream close가 확인되고 정상 완료가 없으면 `CANCELLED`
5. 외부 SSE 연결만 끊어진 경우 자동 Cancel하지 않고 Run은 계속 수행하며 상태 조회 허용

모든 Terminal 전이는 `WHERE run_stts_cd IN (...)` 조건부 UPDATE로 한 행만 성공하게 한다.
프로세스 메모리의 Task cancel만으로 상태를 확정하지 않는다.

### 9.4 200~300명 사용자 기준 초기 운영 한도

등록 사용자 수와 동시 실행 수는 같지 않다. 1차에는 피크 시간 활성 비율 약 10%를 가정하여
20~40개 동시 Run을 목표로 시작하고, 부하 테스트와 실제 P95 실행시간으로 상한을 조정한다.

권장 초기값:

| 항목 | 개발계 | 운영 시작값 | 처리 원칙 |
|---|---:|---:|---|
| Supervisor Agent별 활성 Run | 5 | 15 | Agent별 semaphore와 DB 활성 Run 수 동시 확인 |
| Gateway 전체 활성 Supervisor Run | 10 | 40 | 모든 Agent 합산 보호 한도 |
| 사용자별 전체 활성 Run | 4 | 10 | 서로 다른 Supervisor 사용을 포함한 사용자 합산 한도 |
| 사용자별 동일 Agent 활성 Run | 2 | 5 | 같은 Supervisor의 서로 다른 대화는 병렬 허용 |
| 동일 대화 활성 Run | 1 | 1 | 답변 순서와 Context 정합성을 위해 직렬화 |
| Agent별 대기 Run | 10 | 30 | 초과 시 HTTP 429, `Retry-After` 제공 |
| 사용자 요청 시작 rate | 분당 3회 | 분당 6회 | 짧은 burst 허용, 장기작업 남용 차단 |
| 대화 이력 | 10 turn/20,000자 | 20 turn/40,000자 | 먼저 도달하는 기준 적용 |
| 첫 Event timeout | 60초 | 90초 | endpoint cold start 관측 후 조정 |
| idle timeout | 120초 | 180초 | 공개 heartbeat가 아닌 실제 업무 Event 기준 |
| 전체 Run deadline | 10분 | 30분 | continuation 전체 포함 |
| continuation 최대 횟수 | 10회 | 20회 | 초과 시 `CONTINUATION_LIMIT_EXCEEDED` |
| 단일 완료 Item 저장 크기 | 512 KiB | 1 MiB | 초과 시 축약·truncated 표시, 원본 무제한 저장 금지 |
| Run 전체 Item 저장 크기 | 5 MiB | 10 MiB | 초과 시 이후 대용량 Item 축약 |
| 외부 `metadata` JSON 크기 | 8 KiB | 16 KiB | object, 최대 32개 key |
| 질문 `input` 크기 | 8 KiB | 16 KiB | 초과 시 HTTP 400 |

초기 운영값은 endpoint 3개가 균등하게 사용된다는 보장이 없으므로 Agent별 한도 15와 Gateway
전체 한도 40을 동시에 적용한다. 특정 Agent에 사용량이 몰려도 다른 Agent의 실행 여유를 모두
소진하지 못하게 한다.

제한 도달 시 무조건 실패시키지 않고 다음 순서로 처리한다.

1. 같은 요청의 활성 Run이 있으면 기존 `run_id` 반환
2. 사용자별 또는 동일 Agent 한도 초과는 HTTP 429와 `Retry-After`
3. Agent 활성 한도에만 도달한 경우 제한된 `QUEUED` 상태 허용
4. Agent 대기 한도까지 차면 HTTP 429
5. 대기시간이 60초를 넘으면 `QUEUE_TIMEOUT`으로 종료

운영 시작 전 최소 2단계 부하 테스트를 수행한다.

- 1단계: 각 Supervisor endpoint 단독 5·10·15 동시 Run에서 P50/P95/P99, 429, 오류율 측정
- 2단계: 세 endpoint 합산 20·30·40 동시 Run과 CUBE SSE disconnect·Cancel 혼합 시험

조정 기준:

- P95 첫 Event가 60초를 넘거나 upstream 429가 1% 이상이면 Agent별 활성 한도 축소 또는
  endpoint capacity 상향
- Gateway CPU/Memory가 70%를 지속 초과하거나 DB pool 대기가 발생하면 App instance 또는
  DB pool 조정
- 대기 Run이 상시 발생하면 사용자 rate 제한이 아니라 endpoint provisioned concurrency와
  전체 동시 실행 수를 함께 재산정

Databricks Apps 운영은 가능하면 2개 instance로 시작하여 가용성을 확보하되, horizontal
scaling의 session affinity는 best-effort이므로 실행 상태를 instance memory에만 저장하지 않는다.
개발계는 1개 instance로 시작한다. Supervisor endpoint는 개발계를 제외하고 scale-to-zero를
피하고 baseline traffic을 처리할 최소 capacity를 확보한다.

### 9.5 EC2 초기 배치 권고

Agent별 활성 Run 15개, Gateway 전체 활성 Supervisor Run 40개 기준으로 Gateway는 모델 추론을
직접 수행하지 않고 HTTP streaming, Event 파싱, DB 기록과 제한적인 PDF 생성을 담당한다.
따라서 단일 `8 vCPU / 32 GiB` 범용 EC2는 처리 용량 측면에서 충분한 초기 사양이다.

권장 단계:

| 단계 | 권장 구성 | 용도 |
|---|---|---|
| 개발·성능 검증 | `8 vCPU / 32 GiB` 1대 | 40 Run 부하 시험과 병목 측정 |
| 운영 초기 권장 | `4 vCPU / 16 GiB` 2대 + ALB + ASG | 두 AZ 가용성, 무중단 교체와 부하 분산 |
| 단일 서버 운영 불가피 | `8 vCPU / 32 GiB` 1대 | 용량은 충분하나 장애·배포 시 전체 중단 수용 |

x86_64 기준 예시는 `m7i.2xlarge` 또는 `m7a.2xlarge`, ARM 호환 검증이 끝난 경우
`m7g.2xlarge` 계열을 사용할 수 있다. 인스턴스 유형은 조직 표준과 Region 가용성을 우선한다.

EC2/ALB 적용 조건:

- Uvicorn worker는 4개로 시작하고 worker별 동시 Run이 아니라 DB 기준 전체 40개 제한 적용
- DB connection pool은 worker별 5개 수준에서 시작하여 총 connection 수를 DB 상한 안에 유지
- Gateway 애플리케이션 heartbeat에 의존하지 않음
- ALB idle timeout은 기본 60초보다 큰 300초로 시작하되, 실제 Agent 최대 무이벤트 구간과
  공식 Databricks 이벤트 특성을 부하·장기작업 시험으로 확인하여 더 길게 조정
- ALB target health check는 `/health/ready`, 인스턴스 생존 확인은 `/health/live` 사용
- 운영 이중화 시 서로 다른 AZ에 배치하고 ASG desired/min 2로 시작
- CPU 70%, Memory 70%, 활성 Run, Event queue, DB pool wait, 429 비율을 모니터링

40개 SSE 연결 자체는 8 vCPU를 요구하는 부하는 아니다. 사양 확정은 PDF 동시 생성, 응답 Item
크기, DB latency와 JSON 변환량을 포함한 40 Run 혼합 부하 테스트 결과로 결정한다.

---

## 10. Custom Supervisor 검증 화면

권장 경로: `/custom/supervisor-chat`

최소 기능:

- 등록된 Supervisor 선택
- 새 Conversation 및 기존 이력 선택
- POST 기반 SSE 수신과 텍스트 증분 표시
- 하위 Agent·Tool 실행 상태와 완료 결과 표시
- `long_task` continuation 상태 표시
- 사용자 중단 버튼과 현재 Run 상태
- timeout, Gateway 오류 코드, `trace_id`, 요청 경로 표시
- 완료 item 기반 결과 재조회

기존 `/custom/chat`의 공통 레이아웃·Markdown·citation·PDF 코드를 재사용할 수 있으나,
Supervisor event parser와 Run 상태 관리는 별도 모듈로 분리한다.

---

## 11. 인증·권한·감사

- CUBE 요청은 현재 Gateway 인증 방식과 `source_system`, `user_id` 검증을 재사용
- 공개 Agent ID와 시스템별 허용 Route 검증 후에만 serving endpoint 호출
- App SP에는 등록된 Supervisor endpoint와 하위 Agent·Tool 실행에 필요한 최소 권한 부여
- 구조화 로그에는 `request_id`, `trace_id`, `run_id`, `conversation_id`, `agent_id`, 상태,
  처리 시간을 기록
- 질문·답변 원문, Access Token, Client Secret과 도구의 비밀 출력은 기본 로그에 저장하지 않음

---

## 12. 오류 인터페이스

| 상황 | HTTP 또는 SSE 처리 |
|---|---|
| 미등록·비활성 Agent | HTTP 404 또는 403 |
| 다른 사용자의 Conversation·Run | HTTP 403 |
| 잘못된 요청 형식 | HTTP 400 또는 422 |
| serving endpoint 인증·권한 실패 | HTTP 502, 표준 Gateway 오류 코드 |
| 연결 timeout | HTTP 504 또는 `run.failed` |
| 실행 중 idle/전체 timeout | `run.timed_out` 후 Terminal 상태 저장 |
| 사용자 중단 | Cancel API 응답 및 `run.cancelled` |
| DB 장애 | HTTP 503 또는 `run.failed` |

SSE가 시작된 이후 발생한 오류는 HTTP 상태를 변경할 수 없으므로 표준 오류 event와
Terminal Run 상태로 전달한다.

---

## 13. 구현 단계

### 단계 1. 현재 구조 기준선 확정

- 기존 Agent/Admin/Conversation/감사 테이블 재사용 범위 결정
- 통합 Agent 목록의 공개 응답 모델 확정
- CUBE 요청·응답 및 SSE 지원 제약 확인
- Supervisor endpoint 3개와 App SP 권한 검증

현재 결정 및 확인 상태:

| 항목 | 상태 | 결정·확인 내용 |
|---|---|---|
| CUBE 선택 대상 | 결정 | 실제 endpoint가 아닌 Gateway 논리 `agent_id` 선택 |
| 실제 endpoint 관리 위치 | 결정 | Gateway Admin과 DB에서만 관리 |
| 기존 Genie API | 결정 | `/api/2.0/genie/...` 인터페이스 유지 |
| CUBE 신규 호출 방식 | 확정 | `/api/v1/agents/{agent_id}/responses` 통합 인터페이스 |
| 일반 Genie BOTH 처리 | 확인 필요 | CUBE에서 모드 선택 또는 등록 기본 모드 사용 |
| CUBE SSE 제한 | 확인 필요 | 최대 연결시간, proxy buffering, 자동 재시도 여부 |
| Supervisor 리소스 3개 | 확인 필요 | endpoint 이름, Workspace, App SP `CAN QUERY` 권한 |
| Agent Master | 확정 | `genie_agt_mst`를 `ai_agt_mst`로 일반화하고 기존 `agt_id`·FK 보존 |
| 시스템 매핑 | 확정 | `sys_genie_agt_map`을 `sys_ai_agt_map`으로 일반화하고 기존 데이터 보존 |
| 기존 Genie Route 조회 | 결정 | 실제 Space/Agent ID 조회를 유지하여 기존 API 명세 보호 |
| 신규 통합 Route 조회 | 확정 | `agt_cd`를 논리 `agent_id`로 사용 |
| Supervisor 실행 정책 | 확정 | 1차에는 Agent Master 컬럼, 정책 증가 시 별도 테이블 분리 |
| 공통 Conversation | 확정 | `genie_cnvs_mst`를 `ai_cnvs_mst`로 일반화하고 `cnvs_uid` 추가 |
| Supervisor 실행 이력 | 확정 | `ai_run_mst`, `ai_run_item_hst`, `ai_run_evt_hst` 분리 |
| Run 상태 코드 | 확정 | 비종료 4종과 Terminal 4종으로 구분 |
| 통합 SSE 이벤트 | 확정 | 공통 Envelope와 정규화 Event만 외부 제공 |
| 요청 입력 정규화 | 확정 | 최상위 오타는 400, 미지원 metadata는 제거, capability별 최소 JSON 생성 |
| Supervisor Adapter 분리 | 구현 | Client·Parser·Normalizer·Run Orchestrator 책임 분리 |
| continuation 처리 | 구현 | 동일 Run에서 조건부 상태 전이 후 자동 후속 호출 |
| 대화 Context 제한 | 확정 | 최신 20 turn 또는 40,000자, 현재 질문 유지, 오래된 turn 제외 |
| 실행 지속성 | 확정 | 외부 SSE 단절과 Run 분리, DB 상태 기준, 재시작 Run 자동 재개 제외 |
| timeout·중단 경쟁 | 구현 | Terminal 상태 보호, 조건부 UPDATE와 실행 중 DB 중단 상태 확인 |
| 초기 운영 한도 | 구현 | Agent 15, 전체 40, 사용자 전체 10, 사용자별 동일 Agent 5, 동일 대화 1 |
| EC2 초기 사양 | 검토안 | 단일 8 vCPU/32 GiB는 충분, 운영 가용성은 4 vCPU/16 GiB 2대 권장 |

### 단계 2. 데이터 모델과 설치 SQL

- Agent 리소스 유형 확장
- Run 및 Item/Event 테이블 설계
- 상태·소유권·request hash 인덱스 설계
- 신규 설치 통합 DDL과 기존 환경 migration/rollback 작성

### 단계 3. Supervisor Adapter

- Databricks Responses streaming client
- 인증·endpoint 라우팅
- 원본 event parser와 Gateway event normalizer
- upstream 오류 매핑

### 단계 4. Run Orchestrator

- Run 생성과 상태 전이
- `long_task` continuation
- timeout/deadline/최대 반복
- Cancel API와 stream 종료
- request hash 기반 중복 실행 차단

### 단계 5. 이력·Custom UI

- Conversation·Run·Item 조회
- `/custom/supervisor-chat`
- 중단 버튼과 오류 진단
- 완료 결과 렌더링

### 단계 6. CUBE 외부 연동

- 외부 API 명세 및 호출 예제
- Agent 목록과 capability 정보
- SSE, 중단, 상태 조회 예제
- `/guide`에 정상 응답, Run 상태, SSE Event, HTTP 상태 및 업무 오류 코드 전체 표 제공
- `/guide`에 대표 성공·실패·timeout·중단 요청/응답 예제 제공
- 기존 Genie 외부 API 회귀 확인

### 단계 7. 검증과 배포

- endpoint 3개 이상 등록·호출
- continuation 반복과 최대 횟수
- 중복 요청, 중단, timeout, disconnect
- 권한 오류, endpoint 장애, DB 오류
- README, `/guide`, 설치 문서와 배포 설정 동기화

---

## 14. 테스트 요구사항

### 단위 테스트

- Supervisor 리소스 입력 검증과 등록 수 무제한
- 공개 Agent ID에서 올바른 endpoint로 라우팅
- 원본 SSE를 정규화 event로 변환
- `task_continue_request`와 응답 item 연결
- continuation 최대 횟수와 전체 deadline
- Run 상태 전이와 Terminal 상태 보호
- request hash와 진행 중 중복 차단
- 사용자 중단 이후 continuation 미실행
- 다른 사용자의 Run 접근 차단
- Token·Secret 로그 미노출

### 통합 테스트

- CUBE 형식 요청 → Gateway → 실제 Supervisor SSE
- 세 개 이상의 Supervisor endpoint 선택 호출
- 새 Conversation과 후속 질문
- 장기작업 continuation 1회 이상
- 실행 중 사용자 중단과 상태 조회
- 네트워크 연결 종료 후 Run 최종 상태 조회
- `/custom/supervisor-chat`의 새 대화·이력·중단
- 기존 Genie Chat/Agent API 회귀

---

## 15. 1차 완료 조건

- Admin에서 Supervisor endpoint를 3개 이상 등록하고 CUBE Agent 목록에 제공 가능
- 공개 Agent ID로 올바른 endpoint를 호출하고 Databricks 식별자를 외부에 강제 노출하지 않음
- SSE 텍스트와 도구 실행 상태를 CUBE 및 Custom UI에서 수신 가능
- `long_task` continuation을 Gateway가 자동 수행
- Run별 timeout, 사용자 중단, Terminal 상태와 이력 조회 가능
- 외부 고유 키가 없어도 진행 중 동일 요청의 중복 실행을 제한
- 오류 응답의 `x-request-id`와 외부에 공개 가능한 진단 정보로 서버 로그 추적 가능
- `/guide`에서 정상·실패·오류·중단·timeout 관련 상태와 코드 확인 가능
- 기존 Genie Chat/Agent API와 Custom UI 회귀 테스트 통과
- README, 외부 API 명세, 설치 SQL 및 관련 가이드 동기화

---

## 16. 관련 문서

- 현재 Genie 구현: [`genie_gateway_implementation_spec.md`](genie_gateway_implementation_spec.md)
- 장기 후속 과제: [`추후_개발_과제.md`](추후_개발_과제.md)
- DB 설치: [`../sql/installation/README.md`](../sql/installation/README.md)
- 외부 API 명세 화면: `../app/static/genie_api_guide.html`
- Supervisor 장기작업:
  https://docs.databricks.com/gcp/en/agents/agent-bricks/multi-agent-supervisor-long-running-tasks
- Responses Agent streaming 이벤트:
  https://docs.databricks.com/aws/en/agents/custom-agents/author-agent
- 배포 Agent 호출:
  https://docs.databricks.com/aws/en/agents/custom-agents/query-agent
- OpenAI Responses API 참고 모델(Item·Streaming·MCP Approval):
  https://platform.openai.com/docs/api-reference/responses

OpenAI 문서는 개념 참고 자료이며 CUBE 구현 기준은 Gateway `/guide`의 API 명세다.

---

## 17. 구현 현황

### 완료: 단계 2~4의 기본 실행 구조

- 신규 설치 DDL의 `ai_agt_mst`, `sys_ai_agt_map`, `ai_cnvs_mst` 공통 명칭 반영
- Supervisor endpoint·long task·timeout·Agent별 활성 Run 15개·대화 Context 제한 컬럼 반영
- 외부 공개용 `cnvs_uid`와 `ai_run_mst`, `ai_run_item_hst`, `ai_run_evt_hst` 추가
- 기존 설치본의 PK·FK·데이터를 보존하는 `005_add_supervisor_agent_foundation.sql` 추가
- 기존 설치본의 Run lease 컬럼을 증분 추가하는 `006_add_run_execution_lease.sql` 추가
- 기존 설치본에 `WAITING_APPROVAL` 상태와 `ai_run_aprv_hst`를 추가하는
  `007_add_supervisor_approval.sql` 추가
- 개발·검증 환경에서만 전체 스키마를 제거하는
  `000_reset_genie_gw_schema_DEVELOPMENT_ONLY.sql`과 신규/업그레이드 실행 경로 분리
- 애플리케이션 Repository의 공통 테이블명 전환 및 신규 Conversation UID 생성 반영
- PostgreSQL 15 신규 설치와 기존 `genie_*` 모사 migration·설치 검증 SQL 실행 완료
- 통합 요청·Run 상태·Cancel·Item·SSE envelope Pydantic 모델과 오타 안내 검증 추가
- Admin API/UI의 `GENIE`·`SUPERVISOR` 등록, Serving Endpoint, long task·timeout·Agent 한도 입력 추가
- 실제 Databricks 리소스를 숨기는 `GET /api/v1/agents` 통합 Agent 목록 API 추가
- 시스템 권한과 논리 Agent ID로 Supervisor Serving Endpoint·실행 정책을 조회하는 라우팅 추가
- OAuth/PAT를 재사용하는 Supervisor `/serving-endpoints/{name}/invocations` streaming client 추가
- chunk·멀티라인·CRLF·heartbeat를 처리하는 SSE parser와 기본 Responses event normalizer 추가
- 허용된 history·현재 질문·long task 옵션만 전송하는 최소 JSON adapter 추가
- DB advisory lock 기반 Run 생성·활성 중복 재사용과 Gateway/Agent/사용자 한도 검사 추가
- 조건부 Run 상태 전이, Terminal 보호, 멱등 Cancel 요청과 재시작 정리 Repository 추가
- Idempotency-Key·client message·정규화 질문 기반 request hash 생성 Orchestrator 추가
- `POST /api/v1/agents/{agent_id}/responses` 정규화 SSE Controller와 HTTP 연결에서 분리된
  프로세스 로컬 실행 Task 추가
- Run Event와 완료 Item 저장, Run별 순번 보장 및 SSE 구독자 fan-out 추가
- `GET /api/v1/agents/{agent_id}/runs/{run_id}` 상태 조회와
  `POST /api/v1/agents/{agent_id}/runs/{run_id}/cancel` 중단 요청 추가
- `GET /api/v1/agents/{agent_id}/conversations/{conversation_id}/items` opaque cursor 조회 추가
- upstream 첫 Event·idle·전체 deadline 적용 및 실행 중 중단 상태 최대 약 1초 간격 확인
- 공식 형식의 `messages`에 원본 `task_continue_request`와 대응하는
  `task_continue_response`를 포함하는 자동 continuation 추가
- Continuation ID 중복·최대 횟수·전체 deadline·중단 요청 검사와
  `STREAMING → CONTINUING → STREAMING` 조건부 상태 전이 추가
- `execution_owner_id`와 만료 lease를 이용한 instance 간 중복 실행 선점 방지 추가
- 저장 Event를 기준으로 `Last-Event-ID` 이후 SSE 재생 및 타 instance Run 구독 추가
- 명시적 `Idempotency-Key` 또는 `client_message_id` 재요청은 Terminal Run도 재사용
- `/custom/supervisor-chat`에서 등록 Supervisor 선택, 새 대화·후속 질문, long task Event,
  실행 중 중단을 검증하는 전용 UI와 비공개 BFF 추가
- Custom UI의 Supervisor 대화 목록 조회와 저장된 사용자·Assistant 완료 Item 복원 추가
- `message.completed` 이후 terminal event 없이 정상 EOF로 종료하는 endpoint에 대해 Gateway
  `run.completed`를 보완 생성하여 Run을 Terminal 상태로 정리
- 사용자 질문과 완료 Assistant Message를 Item으로 저장하고 Agent별 20 turn·40,000자 제한에
  따라 후속 요청 history를 재구성
- 실제 `mcp_approval_request`를 `approval.required`로 정규화하고 승인 요청 원문·인자·재개
  문맥을 `ai_run_aprv_hst`에 저장
- `POST /api/v1/agents/{agent_id}/runs/{run_id}/approvals/{approval_id}` 승인/거절 API와
  동일 Run SSE 재개, Custom UI의 웹 검색 승인/거절 버튼 추가
- Supervisor 실행 경로를 포함한 단위·인터페이스 테스트 54건 통과

### 다음 구현

- 실제 Supervisor endpoint 3개 이상 등록·호출 통합 시험
- Web Search 승인/거절 실제 endpoint 통합 시험과 반복 승인 요청 시나리오 검증

이 시점에는 기존 Genie Chat/Agent 외부 API 인터페이스가 변경되지 않는다. Supervisor 공개
endpoint와 전체 활성 Run 40개·사용자 전체 10개·사용자별 동일 Agent 5개 제한은 DB 기준으로
적용한다. 세 Gateway 한도는 `/custom/admin`에서 변경하며 동일 대화는 활성 Run 1개로 고정한다. 프로세스 로컬
Task와 fan-out을 사용하므로 1차 개발계는 단일 instance를 기준으로 한다.

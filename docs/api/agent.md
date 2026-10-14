# Agent API

[서비스 문서](../README.md) / [ai-svc](README.md)

Agent run·artifact·Tool 인가 내부 API다. 공개 Gateway 계약은
[`document-svc Agent API`](https://github.com/FruitionKR/Fruition-document/blob/main/docs/api/agent.md)다. Agent turn은 Kafka `ai.agent.command`로만 실행하며(동기 `POST /agent/turn`은 호출자가 없어 없앴다),
`/agent/runs/**`와 artifact 조회·Tool 인가·상태 조회는 Backend가 내부 HTTP로 호출한다.
`POST /internal/agent/runs/artifacts/register`는 현재 저장소 안에 운영 호출자가 없다.

- API 수: 11

## API 목차

| API | 목적 |
|---|---|
| [`GET /agent/runs/{run_id}`](#summary-get-agent-runs-run-id) | 승인형 Agent run의 현재 계획과 상태를 조회합니다. |
| [`POST /agent/runs/{run_id}/approve`](#summary-post-agent-runs-run-id-approve) | 현재 계획의 version과 operation hash를 검증해 승인합니다. |
| [`POST /agent/runs/{run_id}/cancel`](#summary-post-agent-runs-run-id-cancel) | 실행 가능한 Agent run을 취소합니다. |
| [`POST /agent/runs/{run_id}/reject`](#summary-post-agent-runs-run-id-reject) | 현재 Agent 계획을 거절합니다. |
| [`POST /agent/runs/{run_id}/revise`](#summary-post-agent-runs-run-id-revise) | 사용자 지시로 새 계획을 요청합니다. |
| [`POST /internal/agent/runs/artifacts/list`](#summary-post-internal-agent-runs-artifacts-list) | Agent 실행에 등록된 artifact 목록을 조회합니다. |
| [`POST /internal/agent/runs/artifacts/register`](#summary-post-internal-agent-runs-artifacts-register) | Agent 실행 결과 artifact를 등록합니다. |
| [`POST /internal/agent/runs/artifacts/resolve`](#summary-post-internal-agent-runs-artifacts-resolve) | Agent artifact의 저장 위치와 메타데이터를 확인합니다. |
| [`POST /internal/agent/runs/tool-authorizations/execute`](#summary-post-internal-agent-runs-tool-authorizations-execute) | Agent Tool 변경 작업의 실행 권한을 검증합니다. |
| [`POST /internal/agent/runs/tool-authorizations/read`](#summary-post-internal-agent-runs-tool-authorizations-read) | Agent Tool 읽기 작업의 실행 권한을 검증합니다. |
| [`GET /internal/agent/runs/{run_id}`](#summary-get-internal-agent-runs-run-id) | Markdown Agent 실행 상태와 결과를 조회합니다. |

`/agent/runs/*` API는 `AGENT_SKILLS_ENABLED=true`일 때만 노출되며
`X-Agent-Service-Token`으로 보호한다.

## 한눈에 보기

<a id="summary-get-agent-runs-run-id"></a>
### `GET /agent/runs/{run_id}`

| 항목 | 내용 |
|---|---|
| 목적 | 승인형 Agent run의 현재 계획과 상태를 조회합니다. |
| 입력 | **Path** — `run_id`: `string`<br>**Query** — `workspace_id`, `user_id`: `string`<br>**Header** — `X-Agent-Service-Token`: 필수 |
| 출력 | `200` — `AgentRunResponse` |
| 조건 | `AGENT_SKILLS_ENABLED=true`일 때만 노출되며 workspace·user 소유권을 확인합니다. |
| 주요 오류 | `401` 토큰 불일치<br>`404` run 없음<br>`503` 토큰 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-get-agent-runs-run-id"></a>
### `GET /agent/runs/{run_id}` 상세

#### 1. Method + Path

`GET /agent/runs/{run_id}`

#### 2. 목적

계획 승인 화면에 필요한 plan version, operation hash, 작업 목록과 run 상태를 반환합니다.

#### 3. Auth 필요 여부

- 필요: `X-Agent-Service-Token`

#### 4. Request body

- Body 없음
- path: `run_id`
- query: `workspace_id`, `user_id`

#### 5. Response body

- HTTP `200`: 조회 성공

```json
{
  "id": "run_123",
  "workspace_id": "workspace_123",
  "action": "workspace_workflow",
  "skill_version_id": null,
  "status": "awaiting_approval",
  "request_summary": "현재 문서에 편집안을 반영해줘",
  "error_code": null,
  "plan": {
    "id": "plan_123",
    "version": 1,
    "summary": "승인된 편집안을 현재 문서에 반영합니다.",
    "operation_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "status": "awaiting_approval",
    "operations": [
      {
        "id": "operation_123",
        "sequence": 1,
        "tool_name": "apply_document_edit",
        "target_type": "document",
        "target_id": "doc_123",
        "base_version": 3,
        "source_parent_id": null,
        "destination_parent_id": null,
        "arguments": {},
        "reason": "사용자가 확인한 편집안을 반영합니다.",
        "depends_on": [],
        "status": "pending",
        "error_code": null
      }
    ]
  }
}
```

#### 6. Error response

- `401`: Agent 서비스 토큰 누락·불일치
- `404`: 요청 scope에 해당하는 run 없음
- `503`: Agent 서비스 토큰 미설정

#### 7. Pagination / filtering

- 페이지네이션 없음
- `workspace_id`, `user_id`로 소유권 scope 제한

#### 8. 권한 규칙

서버가 인증한 workspace·user와 run 소유권이 모두 일치해야 합니다.

#### 9. 예시 요청/응답

```bash
curl "$PIPELINE/agent/runs/run_123?workspace_id=workspace_123&user_id=user_123" \
  -H 'X-Agent-Service-Token: <value>'
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: get_agent_run_agent_runs__run_id__get`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentRunStatusRequester.java`:45-47,61-62,106-113 (`app.agent.run-endpoint`, `X-Agent-Service-Token`).
- 아웃바운드 호출: 없음(AI DB 조회).
- 미연동 표시: 없음.

</details>

<a id="summary-post-agent-runs-run-id-approve"></a>
### `POST /agent/runs/{run_id}/approve`

| 항목 | 내용 |
|---|---|
| 목적 | 사용자가 확인한 현재 plan version과 operation hash를 검증해 실행을 승인합니다. |
| 입력 | **Path** — `run_id`<br>**Body** — workspace·user·plan version·operation hash |
| 출력 | `200` — `status=executing`인 `AgentRunResponse` |
| 조건 | run과 plan이 모두 `awaiting_approval`이고 승인값이 현재 계획과 정확히 일치해야 합니다. |
| 주요 오류 | `409` 계획 변경·상태 충돌<br>`422` 요청 검증 실패 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-agent-runs-run-id-approve"></a>
### `POST /agent/runs/{run_id}/approve` 상세

#### 1. Method + Path

`POST /agent/runs/{run_id}/approve`

#### 2. 목적

조회한 계획과 같은 version·hash에만 승인을 기록하고 execution job을 등록합니다.

#### 3. Auth 필요 여부

- 필요: `X-Agent-Service-Token`

#### 4. Request body

```json
{
  "workspace_id": "workspace_123",
  "user_id": "user_123",
  "plan_version": 1,
  "operation_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

#### 5. Response body

- HTTP `200`: 승인 성공
- `AgentRunResponse`; 승인 성공 직후 `status`는 `executing`입니다.

```json
{
  "id": "run_123",
  "workspace_id": "workspace_123",
  "action": "workspace_workflow",
  "skill_version_id": null,
  "status": "executing",
  "request_summary": "현재 문서에 편집안을 반영해줘",
  "error_code": null,
  "plan": null
}
```

#### 6. Error response

- `401` 토큰 불일치, `409` 현재 계획·상태 불일치, `422` 입력 검증 실패, `503` 토큰 미설정

#### 7. Pagination / filtering

- 지원하지 않음

#### 8. 권한 규칙

workspace·user scope, plan version, operation hash를 모두 검증합니다.

#### 9. 예시 요청/응답

```bash
curl -X POST "$PIPELINE/agent/runs/run_123/approve" \
  -H 'X-Agent-Service-Token: <value>' -H 'Content-Type: application/json' \
  --data '{"workspace_id":"workspace_123","user_id":"user_123","plan_version":1,"operation_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: approve_agent_run_agent_runs__run_id__approve_post`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentRunStatusRequester.java`:69 → 84-90 (`app.agent.run-endpoint`, `X-Agent-Service-Token`).
- 아웃바운드 호출: 승인 실행은 Tool gateway를 거쳐 document-svc `POST /internal/agent/tools/execute/{tool_name}`를 호출한다(`app/modules/agent_run/infrastructure/backend_tool_gateway.py:51`).
- 미연동 표시: 없음.

</details>

<a id="summary-post-agent-runs-run-id-cancel"></a>
### `POST /agent/runs/{run_id}/cancel`

| 항목 | 내용 |
|---|---|
| 목적 | Agent run의 후속 실행을 막고 이미 실행한 변경의 역순 복구를 요청합니다. |
| 입력 | **Path** — `run_id`<br>**Body** — `workspace_id`, `user_id` |
| 출력 | `200` — 현재 취소·복구 상태를 담은 `AgentRunResponse` |
| 조건 | 일반 completed·rejected는 거절합니다. markdown_turn은 completed여도 연결된 하위 작업과 Skill 게시의 복구를 요청합니다. failed·partial_failed·conflicted·rollback_failed는 복구를 요청할 수 있고, 취소 진행·완료 상태의 재요청은 멱등입니다. |
| 주요 오류 | `409` run 없음 또는 취소할 수 없는 상태 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-agent-runs-run-id-cancel"></a>
### `POST /agent/runs/{run_id}/cancel` 상세

#### 1. Method + Path

`POST /agent/runs/{run_id}/cancel`

#### 2. 목적

대기 job과 아직 시작하지 않은 operation을 함께 취소하고 `rollback` job을 등록합니다. Python worker가 변경 전 상태와 실행 결과를 이용해 역작업을 역순으로 실행합니다. 복구 도구 호출에는 별도 멱등키와 기록된 정확한 인자의 승인 검사를 적용합니다.

상위 `markdown_turn`과 하위 run의 연결은 하위 생성과 같은 DB 트랜잭션에서 `result.run_id`에 기록합니다. 상위 취소는 이미 완료된 하위 작업도 복구 대상으로 삼으며, 하위가 `cancelled`가 된 뒤 상위 취소를 완료합니다. 실행 중인 상위 턴의 늦은 결과는 외부에 발행하지 않습니다.

상태는 `cancel_requested` → `rolling_back` → `cancelled`로 진행합니다. 실제 결과를 확인할 수 없거나 다른 사용자의 후속 변경과 충돌하거나 복구 도구가 실패하면 `rollback_failed`와 `error_code`를 남깁니다. 도구의 429·5xx 응답처럼 재시도할 수 있는 오류는 바로 확정하지 않고 job backoff(5초 간격, 최대 3회)로 다시 시도하며, 소진한 뒤에 `rollback_failed`로 닫습니다. 재요청하면 기존 복구 기록과 멱등키로 재시도합니다. 내용·이름·위치·순서를 복원하며 버전과 감사 기록은 이전 숫자로 되감지 않습니다.

Backend는 `delete_folder(require_empty=true)`로 빈 폴더를 잠금 상태에서 확인해 삭제하고, `delete_document`는 일반 문서 삭제와 같게 문서를 휴지통으로 옮기고 파생 Wiki 정리는 `document_deleted` command로 비동기 처리합니다. AI는 정리 완료를 기다리거나 확인하지 않습니다. Query·수집·변환·Skill 게시 등 공개 AI 기능의 전체 취소는 [공통 작업 취소 API](tasks.md)를 사용합니다. 이 내부 Agent API만 직접 호출하면 core DB의 채팅·업무 변경까지 복구하지 않습니다.

#### 3. Auth 필요 여부

- 필요: `X-Agent-Service-Token`

#### 4. Request body

```json
{"workspace_id":"workspace_123","user_id":"user_123"}
```

#### 5. Response body

- HTTP `200`: 취소 요청 접수 또는 현재 상태 반환
- 신규 요청은 `status=cancel_requested`; `cancelled`는 복구 확인이 끝난 상태입니다.

```json
{
  "id": "run_123",
  "workspace_id": "workspace_123",
  "action": "workspace_workflow",
  "skill_version_id": null,
  "status": "cancel_requested",
  "request_summary": "현재 문서에 편집안을 반영해줘",
  "error_code": null,
  "plan": null
}
```

#### 6. Error response

- `401` 토큰 불일치, `409` 상태 충돌, `422` 입력 검증 실패, `503` 토큰 미설정

#### 7. Pagination / filtering

- 지원하지 않음

#### 8. 권한 규칙

workspace·user와 run 소유권이 일치해야 합니다.

#### 9. 예시 요청/응답

```bash
curl -X POST "$PIPELINE/agent/runs/run_123/cancel" \
  -H 'X-Agent-Service-Token: <value>' -H 'Content-Type: application/json' \
  --data '{"workspace_id":"workspace_123","user_id":"user_123"}'
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: cancel_agent_run_agent_runs__run_id__cancel_post`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentRunStatusRequester.java`:77 → 84-90 (`app.agent.run-endpoint`, `X-Agent-Service-Token`).
- 아웃바운드 호출: 없음(AI DB 갱신).
- 미연동 표시: 없음.

</details>

<a id="summary-post-agent-runs-run-id-reject"></a>
### `POST /agent/runs/{run_id}/reject`

| 항목 | 내용 |
|---|---|
| 목적 | 승인 대기 중인 현재 계획을 거절합니다. |
| 입력 | **Path** — `run_id`<br>**Body** — `workspace_id`, `user_id` |
| 출력 | `200` — `status=rejected`인 `AgentRunResponse` |
| 조건 | run과 plan이 모두 `awaiting_approval`이어야 합니다. |
| 주요 오류 | `409` 승인 대기 상태가 아님 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-agent-runs-run-id-reject"></a>
### `POST /agent/runs/{run_id}/reject` 상세

#### 1. Method + Path

`POST /agent/runs/{run_id}/reject`

#### 2. 목적

현재 계획에 거절 결정을 기록하고 run을 terminal 상태로 종료합니다.

#### 3. Auth 필요 여부

- 필요: `X-Agent-Service-Token`

#### 4. Request body

```json
{"workspace_id":"workspace_123","user_id":"user_123"}
```

#### 5. Response body

- HTTP `200`: 거절 성공
- `AgentRunResponse`; 성공 시 `status=rejected`

```json
{
  "id": "run_123",
  "workspace_id": "workspace_123",
  "action": "workspace_workflow",
  "skill_version_id": null,
  "status": "rejected",
  "request_summary": "현재 문서에 편집안을 반영해줘",
  "error_code": null,
  "plan": null
}
```

#### 6. Error response

- `401` 토큰 불일치, `409` 상태 충돌, `422` 입력 검증 실패, `503` 토큰 미설정

#### 7. Pagination / filtering

- 지원하지 않음

#### 8. 권한 규칙

workspace·user와 run 소유권이 일치해야 합니다.

#### 9. 예시 요청/응답

```bash
curl -X POST "$PIPELINE/agent/runs/run_123/reject" \
  -H 'X-Agent-Service-Token: <value>' -H 'Content-Type: application/json' \
  --data '{"workspace_id":"workspace_123","user_id":"user_123"}'
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: reject_agent_run_agent_runs__run_id__reject_post`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentRunStatusRequester.java`:73 → 84-90 (`app.agent.run-endpoint`, `X-Agent-Service-Token`).
- 아웃바운드 호출: 없음(AI DB 갱신).
- 미연동 표시: 없음.

</details>

<a id="summary-post-agent-runs-run-id-revise"></a>
### `POST /agent/runs/{run_id}/revise`

| 항목 | 내용 |
|---|---|
| 목적 | 기존 계획을 폐기하고 사용자 지시로 새 계획을 생성하도록 등록합니다. |
| 입력 | **Path** — `run_id`<br>**Body** — `workspace_id`, `user_id`, `instruction` |
| 출력 | `200` — `status=queued`인 `AgentRunResponse` |
| 조건 | `awaiting_approval` 또는 `clarification_required` 상태에서만 허용합니다. |
| 주요 오류 | `409` 수정할 수 없는 상태 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-agent-runs-run-id-revise"></a>
### `POST /agent/runs/{run_id}/revise` 상세

#### 1. Method + Path

`POST /agent/runs/{run_id}/revise`

#### 2. 목적

이전 계획을 superseded 처리하고 새 planning job을 등록합니다.

#### 3. Auth 필요 여부

- 필요: `X-Agent-Service-Token`

#### 4. Request body

```json
{
  "workspace_id": "workspace_123",
  "user_id": "user_123",
  "instruction": "문서를 저장하지 말고 미리보기만 다시 만들어줘"
}
```

#### 5. Response body

- HTTP `200`: 수정 요청 접수 성공
- `AgentRunResponse`; 성공 시 `status=queued`, `plan=null`

```json
{
  "id": "run_123",
  "workspace_id": "workspace_123",
  "action": "workspace_workflow",
  "skill_version_id": null,
  "status": "queued",
  "request_summary": "문서를 저장하지 말고 미리보기만 다시 만들어줘",
  "error_code": null,
  "plan": null
}
```

#### 6. Error response

- `401` 토큰 불일치, `409` 상태 충돌, `422` 입력 검증 실패, `503` 토큰 미설정

#### 7. Pagination / filtering

- 지원하지 않음

#### 8. 권한 규칙

workspace·user와 run 소유권이 일치해야 합니다.

#### 9. 예시 요청/응답

```bash
curl -X POST "$PIPELINE/agent/runs/run_123/revise" \
  -H 'X-Agent-Service-Token: <value>' -H 'Content-Type: application/json' \
  --data '{"workspace_id":"workspace_123","user_id":"user_123","instruction":"문서를 저장하지 말고 미리보기만 다시 만들어줘"}'
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: revise_agent_run_agent_runs__run_id__revise_post`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentRunStatusRequester.java`:81 → 84-90 (`app.agent.run-endpoint`, `X-Agent-Service-Token`).
- 아웃바운드 호출: 없음(AI DB 갱신).
- 미연동 표시: 없음.

</details>

<a id="summary-post-internal-agent-runs-artifacts-list"></a>
### `POST /internal/agent/runs/artifacts/list`

| 항목 | 내용 |
|---|---|
| 목적 | Agent 실행에 등록된 artifact 목록을 조회합니다. |
| 입력 | **Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `AgentArtifactListRequest` |
| 출력 | `200` 성공 — 배열<`AgentArtifactResponse`> |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-internal-agent-runs-artifacts-list"></a>
### `POST /internal/agent/runs/artifacts/list` 상세

#### 1. Method + Path

`POST /internal/agent/runs/artifacts/list`

#### 2. 목적

Agent 실행에 등록된 artifact 목록을 조회합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`AgentArtifactListRequest`)

```json
{
  "run_id": "string",
  "user_id": "string",
  "workspace_id": "string"
}
```

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`Response List Agent Artifacts Internal Agent Runs Artifacts List Post`)

```json
[
  {
    "base_version": 1,
    "content_hash": "string",
    "document_id": "string",
    "id": "string",
    "purpose": "string",
    "target": {
    }
  }
]
```

#### 6. Error response

- HTTP `401`: 내부 인증 토큰 누락 또는 불일치
- HTTP `503`: 내부 인증 미설정

| HTTP 상태 | 설명 | 응답 스키마 |
|---|---|---|
| `422` | Validation Error | `HTTPValidationError` |

```json
{
  "detail": [
    {
      "ctx": {
      },
      "input": {
      },
      "loc": [
        "string"
      ],
      "msg": "string",
      "type": "string"
    }
  ]
}
```

#### 7. Pagination / filtering

- 페이지네이션: 지원하지 않음
- 필터링: 지원하지 않음

#### 8. 권한 규칙

- 올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.
- 요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다.

#### 9. 예시 요청/응답

```bash
curl -X POST "$PIPELINE/internal/agent/runs/artifacts/list" \
  -H 'X-Internal-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"run_id":"<value>","user_id":"<value>","workspace_id":"<value>"}'
```

```json
[
  {
    "base_version": 1,
    "content_hash": "string",
    "document_id": "string",
    "id": "string",
    "purpose": "string",
    "target": {
    }
  }
]
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: list_agent_artifacts_internal_agent_runs_artifacts_list_post`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentArtifactClient.java`:39 (`app.agent.status-endpoint` + `/artifacts`).
- 아웃바운드 호출: 없음.
- 미연동 표시: 없음.

[↑ 요약으로 돌아가기](#summary-post-internal-agent-runs-artifacts-list)

</details>

<a id="summary-post-internal-agent-runs-artifacts-register"></a>
### `POST /internal/agent/runs/artifacts/register`

| 항목 | 내용 |
|---|---|
| 목적 | Agent 실행 결과 artifact를 등록합니다. |
| 입력 | **Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `AgentArtifactRegisterRequest` |
| 출력 | `200` 성공 — `AgentArtifactResponse` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-internal-agent-runs-artifacts-register"></a>
### `POST /internal/agent/runs/artifacts/register` 상세

#### 1. Method + Path

`POST /internal/agent/runs/artifacts/register`

#### 2. 목적

Agent 실행 결과 artifact를 등록합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`AgentArtifactRegisterRequest`)

```json
{
  "artifact_id": "string",
  "base_version": 1.0,
  "content_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "document_id": "string",
  "markdown": "string",
  "purpose": "string",
  "run_id": "string",
  "target": {
  },
  "user_id": "string",
  "workspace_id": "string"
}
```

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`AgentArtifactResponse`)

```json
{
  "base_version": 1,
  "content_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "document_id": "string",
  "id": "string",
  "purpose": "string",
  "target": {
  }
}
```

#### 6. Error response

- HTTP `401`: 내부 인증 토큰 누락 또는 불일치
- HTTP `503`: 내부 인증 미설정

| HTTP 상태 | 설명 | 응답 스키마 |
|---|---|---|
| `422` | Validation Error | `HTTPValidationError` |

```json
{
  "detail": [
    {
      "ctx": {
      },
      "input": {
      },
      "loc": [
        "string"
      ],
      "msg": "string",
      "type": "string"
    }
  ]
}
```

#### 7. Pagination / filtering

- 페이지네이션: 지원하지 않음
- 필터링: 지원하지 않음

#### 8. 권한 규칙

- 올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.
- 요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다.

#### 9. 예시 요청/응답

```bash
curl -X POST "$PIPELINE/internal/agent/runs/artifacts/register" \
  -H 'X-Internal-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"artifact_id":"<value>","base_version":1,"content_hash":"sha256:50d858e0985ecc7f60418aaf0cc5ab587f42c2570a884095a9e8ccacd0f6545c","document_id":"<value>","markdown":"example","purpose":"<value>","run_id":"<value>","target":{},"user_id":"<value>","workspace_id":"<value>"}'
```

```json
{
  "base_version": 1,
  "content_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "document_id": "string",
  "id": "string",
  "purpose": "string",
  "target": {
  }
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: register_agent_artifact_internal_agent_runs_artifacts_register_post`)

#### 연동

- 인바운드 호출자: **호출자 없음.** `PipelineAgentArtifactClient`는 `/list`·`/resolve`만 호출한다(Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentArtifactClient.java`:39,59). `artifacts/register` 문자열은 document-svc 전체에서 검색되지 않는다.
- 아웃바운드 호출: 없음.
- 미연동 표시: **미연동.** ai-svc 내부에서도 이 route를 호출하는 경로가 없다.

[↑ 요약으로 돌아가기](#summary-post-internal-agent-runs-artifacts-register)

</details>

<a id="summary-post-internal-agent-runs-artifacts-resolve"></a>
### `POST /internal/agent/runs/artifacts/resolve`

| 항목 | 내용 |
|---|---|
| 목적 | Agent artifact의 저장 위치와 메타데이터를 확인합니다. |
| 입력 | **Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `AgentArtifactResolveRequest` |
| 출력 | `200` 성공 — `AgentArtifactResolveResponse` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-internal-agent-runs-artifacts-resolve"></a>
### `POST /internal/agent/runs/artifacts/resolve` 상세

#### 1. Method + Path

`POST /internal/agent/runs/artifacts/resolve`

#### 2. 목적

Agent artifact의 저장 위치와 메타데이터를 확인합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`AgentArtifactResolveRequest`)

```json
{
  "artifact_id": "string",
  "base_version": 1.0,
  "content_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "document_id": "string",
  "purpose": "string",
  "run_id": "string",
  "target": {
  },
  "user_id": "string",
  "workspace_id": "string"
}
```

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`AgentArtifactResolveResponse`)

```json
{
  "base_version": 1,
  "content_hash": "string",
  "document_id": "string",
  "id": "string",
  "markdown": "string",
  "purpose": "string",
  "target": {
  }
}
```

#### 6. Error response

- HTTP `401`: 내부 인증 토큰 누락 또는 불일치
- HTTP `503`: 내부 인증 미설정

| HTTP 상태 | 설명 | 응답 스키마 |
|---|---|---|
| `422` | Validation Error | `HTTPValidationError` |

```json
{
  "detail": [
    {
      "ctx": {
      },
      "input": {
      },
      "loc": [
        "string"
      ],
      "msg": "string",
      "type": "string"
    }
  ]
}
```

#### 7. Pagination / filtering

- 페이지네이션: 지원하지 않음
- 필터링: 지원하지 않음

#### 8. 권한 규칙

- 올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.
- 요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다.

#### 9. 예시 요청/응답

```bash
curl -X POST "$PIPELINE/internal/agent/runs/artifacts/resolve" \
  -H 'X-Internal-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"artifact_id":"<value>","base_version":1,"content_hash":"sha256:50d858e0985ecc7f60418aaf0cc5ab587f42c2570a884095a9e8ccacd0f6545c","document_id":"<value>","purpose":"<value>","run_id":"<value>","target":{},"user_id":"<value>","workspace_id":"<value>"}'
```

```json
{
  "base_version": 1,
  "content_hash": "string",
  "document_id": "string",
  "id": "string",
  "markdown": "string",
  "purpose": "string",
  "target": {
  }
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: resolve_agent_artifact_internal_agent_runs_artifacts_resolve_post`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentArtifactClient.java`:59.
- 아웃바운드 호출: 없음.
- 미연동 표시: 없음.

[↑ 요약으로 돌아가기](#summary-post-internal-agent-runs-artifacts-resolve)

</details>

<a id="summary-post-internal-agent-runs-tool-authorizations-execute"></a>
### `POST /internal/agent/runs/tool-authorizations/execute`

| 항목 | 내용 |
|---|---|
| 목적 | Agent Tool 변경 작업의 실행 권한을 검증합니다. |
| 입력 | **Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `AgentToolExecuteAuthorizationRequest` |
| 출력 | `204` 성공 |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-internal-agent-runs-tool-authorizations-execute"></a>
### `POST /internal/agent/runs/tool-authorizations/execute` 상세

#### 1. Method + Path

`POST /internal/agent/runs/tool-authorizations/execute`

#### 2. 목적

Agent Tool 변경 작업의 실행 권한을 검증합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`AgentToolExecuteAuthorizationRequest`)

```json
{
  "arguments": {
  },
  "operation_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "operation_id": "string",
  "plan_id": "string",
  "plan_version": 1.0,
  "run_id": "string",
  "tool_name": "string",
  "user_id": "string",
  "workspace_id": "string"
}
```

#### 5. Response body

- HTTP `204`: Successful Response
- Body: 없음

#### 6. Error response

- HTTP `401`: 내부 인증 토큰 누락 또는 불일치
- HTTP `503`: 내부 인증 미설정

| HTTP 상태 | 설명 | 응답 스키마 |
|---|---|---|
| `422` | Validation Error | `HTTPValidationError` |

```json
{
  "detail": [
    {
      "ctx": {
      },
      "input": {
      },
      "loc": [
        "string"
      ],
      "msg": "string",
      "type": "string"
    }
  ]
}
```

#### 7. Pagination / filtering

- 페이지네이션: 지원하지 않음
- 필터링: 지원하지 않음

#### 8. 권한 규칙

- 올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.
- 요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다.

#### 9. 예시 요청/응답

```bash
curl -X POST "$PIPELINE/internal/agent/runs/tool-authorizations/execute" \
  -H 'X-Internal-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"arguments":{},"operation_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","operation_id":"<value>","plan_id":"<value>","plan_version":1,"run_id":"<value>","tool_name":"<value>","user_id":"<value>","workspace_id":"<value>"}'
```

```json
{
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: authorize_agent_tool_execute_internal_agent_runs_tool_authorizations_execute_post`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentToolAuthorizationClient.java`:39 → 48 (`app.agent.status-endpoint` + `/tool-authorizations`).
- 아웃바운드 호출: 없음.
- 미연동 표시: 없음.

[↑ 요약으로 돌아가기](#summary-post-internal-agent-runs-tool-authorizations-execute)

</details>

<a id="summary-post-internal-agent-runs-tool-authorizations-read"></a>
### `POST /internal/agent/runs/tool-authorizations/read`

| 항목 | 내용 |
|---|---|
| 목적 | Agent Tool 읽기 작업의 실행 권한을 검증합니다. |
| 입력 | **Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `AgentToolReadAuthorizationRequest` |
| 출력 | `204` 성공 |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-internal-agent-runs-tool-authorizations-read"></a>
### `POST /internal/agent/runs/tool-authorizations/read` 상세

#### 1. Method + Path

`POST /internal/agent/runs/tool-authorizations/read`

#### 2. 목적

Agent Tool 읽기 작업의 실행 권한을 검증합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`AgentToolReadAuthorizationRequest`)

```json
{
  "run_id": "string",
  "user_id": "string",
  "workspace_id": "string"
}
```

#### 5. Response body

- HTTP `204`: Successful Response
- Body: 없음

#### 6. Error response

- HTTP `401`: 내부 인증 토큰 누락 또는 불일치
- HTTP `503`: 내부 인증 미설정

| HTTP 상태 | 설명 | 응답 스키마 |
|---|---|---|
| `422` | Validation Error | `HTTPValidationError` |

```json
{
  "detail": [
    {
      "ctx": {
      },
      "input": {
      },
      "loc": [
        "string"
      ],
      "msg": "string",
      "type": "string"
    }
  ]
}
```

#### 7. Pagination / filtering

- 페이지네이션: 지원하지 않음
- 필터링: 지원하지 않음

#### 8. 권한 규칙

- 올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.
- 요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다.

#### 9. 예시 요청/응답

```bash
curl -X POST "$PIPELINE/internal/agent/runs/tool-authorizations/read" \
  -H 'X-Internal-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"run_id":"<value>","user_id":"<value>","workspace_id":"<value>"}'
```

```json
{
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: authorize_agent_tool_read_internal_agent_runs_tool_authorizations_read_post`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentToolAuthorizationClient.java`:34 → 48.
- 아웃바운드 호출: 없음.
- 미연동 표시: 없음.

[↑ 요약으로 돌아가기](#summary-post-internal-agent-runs-tool-authorizations-read)

</details>

<a id="summary-get-internal-agent-runs-run-id"></a>
### `GET /internal/agent/runs/{run_id}`

| 항목 | 내용 |
|---|---|
| 목적 | Markdown Agent 실행 상태와 결과를 조회합니다. |
| 입력 | **Path** — `run_id`: `string`<br>**Query** — `workspace_id`: `string`, `user_id`: `string`<br>**Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null` |
| 출력 | `200` 성공 — `MarkdownAgentRunStatusResponse` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>필터링: `workspace_id`, `user_id`<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-get-internal-agent-runs-run-id"></a>
### `GET /internal/agent/runs/{run_id}` 상세

#### 1. Method + Path

`GET /internal/agent/runs/{run_id}`

#### 2. 목적

Markdown Agent 실행 상태와 결과를 조회합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| path | `run_id` | `string` | 예 | - |
| query | `workspace_id` | `string` | 예 | - |
| query | `user_id` | `string` | 예 | - |
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Body: 없음

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`MarkdownAgentRunStatusResponse`)

```json
{
  "apply_operation_id": "string",
  "base_version": 1,
  "document_id": "string",
  "error_code": "string",
  "id": "string",
  "result": {
  },
  "status": "string"
}
```

#### 6. Error response

- HTTP `401`: 내부 인증 토큰 누락 또는 불일치
- HTTP `503`: 내부 인증 미설정

| HTTP 상태 | 설명 | 응답 스키마 |
|---|---|---|
| `422` | Validation Error | `HTTPValidationError` |

```json
{
  "detail": [
    {
      "ctx": {
      },
      "input": {
      },
      "loc": [
        "string"
      ],
      "msg": "string",
      "type": "string"
    }
  ]
}
```

#### 7. Pagination / filtering

- 페이지네이션: 지원하지 않음
- 필터링: `workspace_id`, `user_id`

#### 8. 권한 규칙

- 올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.
- 요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다.

#### 9. 예시 요청/응답

```bash
curl -X GET "$PIPELINE/internal/agent/runs/<value>?workspace_id=<value>&user_id=<value>" \
  -H 'X-Internal-Token: <value>'
```

```json
{
  "apply_operation_id": "string",
  "base_version": 1,
  "document_id": "string",
  "error_code": "string",
  "id": "string",
  "result": {
  },
  "status": "string"
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: get_markdown_agent_run_internal_agent_runs__run_id__get`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentRunStatusRequester.java`:118-120 (`app.agent.status-endpoint`, `X-Internal-Token`).
- 아웃바운드 호출: 없음.
- 미연동 표시: 없음.

[↑ 요약으로 돌아가기](#summary-get-internal-agent-runs-run-id)

</details>

질의 답변은 평가자가 활성화된 경우에만 `query_evaluating`(답변 검토)을 평가 호출 직전에 발행하고, 검토 후 재작성하는 경우 `answer_retrying`을 발행합니다. 일반 대화 등 평가자를 호출하지 않는 경로에는 검토 단계를 표시하지 않습니다.

첫 문답은 최종 답변 이벤트를 먼저 전송한 뒤 같은 모델로 짧은 채팅 제목을 생성합니다. 제목 생성은 다음 문답 처리를 막지 않는 별도 작업으로 실행하고, 정상 종료 시 진행 중인 제목 전송을 마친 뒤 producer를 닫습니다. 별도의 `session_title` 이벤트로 document 서비스에 저장하며, 실패해도 완료된 답변은 바뀌지 않습니다. 이전 대화가 있으면 제목을 다시 생성하지 않습니다.

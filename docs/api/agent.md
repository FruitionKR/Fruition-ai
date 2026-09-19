# Agent API

[서비스 문서](../README.md) / [ai-svc](README.md)

Agent turn과 run·artifact·Tool 인가 내부 API다. 공개 Gateway 계약은
[`document-svc Agent API`](https://github.com/FruitionKR/Fruition-document/blob/main/docs/api/agent.md)다. Agent turn은 운영 경로에서 Kafka로 실행하며,
`/agent/runs/**`와 artifact 조회·Tool 인가·상태 조회는 Backend가 내부 HTTP로 호출한다.
`POST /internal/agent/runs/artifacts/register`는 현재 저장소 안에 운영 호출자가 없다.

- API 수: 12

## API 목차

| API | 목적 |
|---|---|
| [`POST /agent/turn`](#summary-post-agent-turn) | Agent 요청을 분류하고 Query·문서 생성·편집 작업을 실행합니다. |
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

<a id="summary-post-agent-turn"></a>
### `POST /agent/turn`

| 항목 | 내용 |
|---|---|
| 목적 | Agent 요청을 분류하고 Query·문서 생성·편집 작업을 실행합니다. |
| 입력 | **Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`, `X-Agent-Service-Token`(조건부 필수: `AGENT_SKILLS_ENABLED=true`): `string` / `null`<br>**Body** — `AgentTurnRequestBody` |
| 출력 | `200` 성공 — `AgentTurnResponse` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>`AGENT_SKILLS_ENABLED=true`이면 Agent 서비스 토큰도 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 또는 Agent 서비스 인증 토큰 누락·불일치<br>`503` 내부 또는 Agent 서비스 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-agent-turn"></a>
### `POST /agent/turn` 상세

`edit_goal`은 내용 변환, `edit_operation`은 교체·추가 방식, `edit_destination`은 적용 위치를 각각 나타냅니다. `insert_after`는 기존 Markdown을 교체하지 않고 대상 뒤에 새 Markdown만 삽입합니다.

문서 편집·생성 결과의 `message`에는 실제 스킬 사용 여부와 변경 요약, 미적용 상태 안내가 담깁니다. 문서에 적용할 `edit.replacement_markdown`·`generated_markdown.markdown`에는 이 안내를 섞지 않습니다.

비동기 worker 실행에서는 기존 진행 이벤트 경로로 `agent.routing`(요청·스킬 확인), `agent.routed`(처리 유형 결정), `agent.generating`(편집안·초안 작성), `agent.retrying`(검증 실패 후 재작성), `agent.evaluating`(편집안 AI 검토), `agent.finalizing`(결과 정리)을 발행합니다. 생성 단계는 실제 Markdown 생성 직전에만 발행하며, 완료 여부는 기존 최종 결과 이벤트로 판단합니다.

#### 1. Method + Path

`POST /agent/turn`

#### 2. 목적

Agent 요청을 분류하고 Query·문서 생성·편집 작업을 실행합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.
- `AGENT_SKILLS_ENABLED=true`이면 `X-Agent-Service-Token`도 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |
| header | `X-Agent-Service-Token` | `string` | 조건부 (`AGENT_SKILLS_ENABLED=true`) | - |

- Content-Type: `application/json` (`AgentTurnRequestBody`)

아래 예시는 직전 Markdown 편집 미리보기를 그대로 저장하는 요청입니다.

```json
{
  "message": "이대로 저장해줘",
  "provider": "openai",
  "model": "gpt-5-nano",
  "workspace_id": "workspace_123",
  "user_id": "user_123",
  "document_id": "doc_123",
  "base_version": 3,
  "active_markdown_context": {
    "markdown": "# 회의록\n\n기존 내용",
    "target": {
      "type": "whole_document",
      "start_line": 1,
      "end_line": 3
    }
  },
  "conversation_context": {
    "recent_conversation_summary": null,
    "recent_messages": [
      {
        "role": "assistant",
        "content": "편집 미리보기를 만들었습니다.",
        "action": "markdown_edit",
        "run_id": "agent_preview_123",
        "agent_route": {
          "action": "markdown_edit",
          "retrieval_source": "workspace",
          "document_operation": "edit",
          "persist": false,
          "edit_goal": "other",
          "edit_operation": "replace",
          "edit_destination": "target",
          "selected_skill_id": null
        }
      }
    ],
    "reference_context": null,
    "pending_skill_proposal": null
  }
}
```

저장·반영을 명시한 열린 문서 편집을 승인 계획으로 만들 때는 `active_markdown_context`와 함께
`document_id`·`base_version`을 전달한다. 파이프라인은 해당 문서와 버전에만 적용 가능한
`apply_document_edit` 아티팩트를 만들어 계획 범위를 제한한다.
미리보기만 필요한 일반 편집은 두 필드를 생략할 수 있다.

Query·Markdown·Ingest·Lint의 LLM 호출은 공통 client에서 문서·검색·대화 내용을 untrusted data로
격리하고 숫자 개인정보를 provider 전송 전에 마스킹한다. 일반 문서 안의 보안 예문이나 연락처는
Skill 지시문처럼 실행 가능한 명령으로 간주하지 않으며, 저장은 승인 계획을 거친다. Skill 지시문과
Agent 실행 계획에는 별도의 권한·tool·승인 검사를 계속 적용한다.

`conversation_context.recent_messages[].action`은 이전 assistant 응답의 action을 전달하는 선택 필드다.
라우터는 이를 멀티턴 연속성 힌트로만 사용하며 현재 요청의 명시적 의도를 우선한다.
`recent_messages[].agent_route`는 Backend가 저장된 실행 결과에서 조립한 이전 route 힌트다.
라우터는 생략된 검색 출처·문서 작업을 이해하는 데만 사용하며 현재 요청과 충돌하면 현재 요청을 우선한다.
`recent_messages[].run_id`도 선택 필드이며, 사용자가 이전 Markdown 미리보기를 저장하겠다고 확인하면
같은 workspace·user의 완료 결과를 조회해 생성 또는 편집 결과를 재사용한다. 편집은 document·base version과
`source_markdown_sha256`까지 일치해야 한다. 일치하는 실행을 확인할 수 없으면 새 승인 작업을 만들지 않고
미리보기를 다시 요청한다.
복합 요청은 `retrieval_source`(`none|workspace|web`),
`document_operation`(`none|create|edit`), `persist`, `required_capabilities`, `edit_goal`,
`edit_operation`(`replace|insert_after`), `edit_destination`(`target|document_end`)으로 분해한 뒤 전체 조합을
대표하는 action을 선택한다. `required_capabilities`는 요청의 모든 절에 필요한
`document-create|document-edit|folder-organize|template`을 담는다. 서버는 일부 capability만 가진
Skill을 선택하지 않으며, 선택된 Skill의 capability별 확정 Tool 권한을 합쳐 planner에 전달한다.
라우터는 Query·대화·Markdown 편집·Markdown 생성의 실행 영역과 세부 실행 필드를 한 번에 확정한다.
선택된 실행기는 요청을 다시 분류하거나 다른 실행기로 넘기지 않고 해당 action만 수행한다.
따라서 `retrieval_source`, `edit_goal`, `edit_operation`, `edit_destination`은 실행기가 덮어쓸 수 없는
실행 입력이다. 주제 없는 `검색해줘` 같은 요청은 라우터가 `conversation_reply`로 보내 검색 대상을 묻고,
구조상 필요한 편집 위치를 확인할 수 없을 때만 애플리케이션이 `clarify`로 종료한다.
로컬 guard가 처리하지 않는 요청은 문맥 기반 라우터가 한 번 분류한다. `folder_organize`와
`workspace_workflow`는 별도 직접 변이 검증 AI를 호출하거나 분류 일치를 요구하지 않고 planning job을
등록한다. 기존 플래너가 현재 사용자 요청 원문 전체(`instruction`)와 분류 힌트(`routing_action`),
계층 스냅샷·Skill 권한·등록된 본문 아티팩트를 받아 변경 의도 검증과 구체적인 계획 작성을 한 번에 수행한다.
분류 힌트·폴더/문서 이름·Skill 지시·아티팩트는 사용자 변경 승인 근거가 아니며, 각 변경은 사용자 요청에
근거해야 한다. 서로 독립된 여러 작업이나 `folder_organize`/`workspace_workflow` 분류 차이 자체는
계획 차단 사유가 아니다. 본문 생성·편집 미리보기는 기존 실행 입력에 따라 준비하며, 실제 변경은 승인 뒤 수행한다.
플래너가 `intent_confirmed=true`로 반환한 계획은 Tool 권한·대상·버전·아티팩트 계약 검사를 거쳐
`awaiting_approval`로 저장한다. `intent_confirmed=false`와 빈 operations를 반환하면 계획을 저장하지 않고
`clarification_required`(`error_code=mutation_intent_required`)로 추가 요청을 기다린다. 모델 호출이나 응답 계약
오류는 job 실패로 처리하며 사용자 의도 미확인으로 바꾸지 않는다. 별도의 후속 플래닝 호출은 없고,
승인된 plan version·operation hash가 일치해야 변경 도구를 실행한다. 사용자가 계획 수정을 요청하면 새 계획을 작성한다.
`insert_after + document_end`는 활성 선택 영역이 있더라도 전체 문서를 위치 기준으로 사용하므로
선택 영역 교체로 바뀌지 않는다.
서버는 검색·문서·Skill 작업 의도를 정규식으로 재판정하지 않는다. Skill 생성·완료 작업 일반화·기존 Skill 사용과
현재 거절 의도는 라우터가 사용자 요청과 대화 맥락으로 구분하며, 참조 문서나 assistant 발언은 요청 근거가 아니다.
`스킬로 저장해줘` 같은 표현도 재사용 가능한 Skill을 요청하는 의미로 해석한다. 완료 작업의 원본 선택이나
개인·팀 범위가 부족하면 기존 초안 절차에서 추가 정보를 요청하고, 초안은 검토 후 별도 게시 승인을 받는다.
action과 필드 조합·응답 형식·웹 검색 권한 계약이 맞지 않을 때만 LLM에 한 번 재요청한다.
두 번째 응답도 계약을 만족하지 못하면 HTTP 422로 종료한다.
Workspace 작업 기능이 비활성화된 경우에는 사용자 요청 오류인 `invalid_agent_turn_request`가 아니라
설정 오류 `agent_not_configured`로 종료한다. 동기 API는 HTTP 500, 비동기 작업은 해당 오류 코드로 실패를 기록하며
작업 계획을 생성하지 않는다.
응답 action은 내부 문서 근거 조회인 `chat_answer`, 대화 맥락만으로 작성·형식을 이어가는
`conversation_reply`, 열린 Markdown을 변경하는 `markdown_edit`를 구분한다.
내부 문서 근거 조회와 새 문서 저장 또는 열린 문서 편집을 함께 요청하면 Query 파이프라인이 먼저
평가한 답변과 evidence snippet을 Markdown 생성·편집 입력에 포함한다. 허용된 웹 검색은 새 문서
생성 요청에서 같은 흐름을 사용하며 `allow_web_search=true`일 때만 실행한다. 생성 결과는
`create_document`, 편집 결과는 `apply_document_edit` 아티팩트로 `workspace_workflow` 승인 계획에
전달한다.

#### 5. Response body

- HTTP `200`: `AgentTurnResponse`
- action에 따라 아래 결과 필드 하나를 중심으로 사용합니다.

| action | 주요 결과 |
|---|---|
| `chat_answer` | `chat` |
| `conversation_reply`, `clarify`, `reject` | `message` |
| `markdown_edit` | `edit`, `source_markdown_sha256` |
| `markdown_create` | `generated_markdown` |
| `workspace_workflow`, `folder_organize` | `run_id`, `run_status` |
| `skill_authoring`, `skill_draft_proposal` | `skill_authoring` |

Markdown 편집 미리보기 응답 예시:

```json
{
  "action": "markdown_edit",
  "route": {
    "action": "markdown_edit",
    "confidence": 0.98,
    "reason": "현재 문서 편집 미리보기 요청",
    "edit_goal": "other",
    "edit_operation": "replace",
    "edit_destination": "target",
    "selected_skill_id": null,
    "skill_candidates": [],
    "retrieval_source": "workspace",
    "document_operation": "edit",
    "persist": false,
    "required_capabilities": ["document-edit"]
  },
  "updated_conversation_summary": null,
  "message": null,
  "chat": null,
  "edit": {
    "operation": "replace",
    "requested_target": {
      "type": "whole_document",
      "start_line": 1,
      "end_line": 3
    },
    "actual_target": {
      "type": "whole_document",
      "start_line": 1,
      "end_line": 3
    },
    "scope_expanded": false,
    "changed": true,
    "summary": "회의록을 정리했습니다.",
    "replacement_markdown": "# 회의록\n\n- 결정 사항"
  },
  "source_markdown_sha256": "1f2f993be5295526ba6702d640d759663846f1605fa86254905978244c3451d3",
  "generated_markdown": null,
  "skill_candidates": [],
  "run_id": null,
  "run_status": null,
  "skill_authoring": null
}
```

#### 6. Error response

- HTTP `401`: 내부 또는 Agent 서비스 인증 토큰 누락·불일치
- HTTP `503`: 내부 또는 Agent 서비스 인증 미설정

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
- `AGENT_SKILLS_ENABLED=true`이면 올바른 Agent 서비스 토큰도 필요하다.
- 요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다.

#### 9. 예시 요청/응답

직전 assistant 메시지의 `run_id`와 canonical `agent_route`를 함께 보내면, AI는 저장된
미리보기 결과와 현재 editor snapshot을 검증한 뒤 새 LLM 편집을 만들지 않고 승인 run을 시작합니다.

```bash
curl -X POST "$PIPELINE/agent/turn" \
  -H 'X-Internal-Token: <value>' \
  -H 'X-Agent-Service-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"message":"이대로 저장해줘","provider":"openai","model":"gpt-5-nano","workspace_id":"workspace_123","user_id":"user_123","document_id":"doc_123","base_version":3,"active_markdown_context":{"markdown":"# 회의록\n\n기존 내용","target":{"type":"whole_document","start_line":1,"end_line":3}},"conversation_context":{"recent_messages":[{"role":"assistant","content":"편집 미리보기를 만들었습니다.","action":"markdown_edit","run_id":"agent_preview_123","agent_route":{"action":"markdown_edit","retrieval_source":"workspace","document_operation":"edit","persist":false,"edit_goal":"other","edit_operation":"replace","edit_destination":"target","selected_skill_id":null}}]}}'
```

```json
{
  "action": "workspace_workflow",
  "route": {
    "action": "workspace_workflow",
    "confidence": 1.0,
    "reason": "확인한 편집안 저장 요청",
    "edit_goal": "other",
    "edit_operation": "replace",
    "edit_destination": "target",
    "selected_skill_id": null,
    "skill_candidates": [],
    "retrieval_source": "workspace",
    "document_operation": "edit",
    "persist": true,
    "required_capabilities": ["document-edit"]
  },
  "updated_conversation_summary": null,
  "message": null,
  "chat": null,
  "edit": null,
  "source_markdown_sha256": null,
  "generated_markdown": null,
  "skill_candidates": [],
  "run_id": "run_123",
  "run_status": "queued",
  "skill_authoring": null
}
```

현재 Markdown의 SHA-256, document, base version 또는 소유권이 이전 미리보기와 다르면
`workspace_workflow` run을 만들지 않고 `action=clarify`를 반환합니다.

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: handle_agent_turn_agent_turn_post`)

[↑ 요약으로 돌아가기](#summary-post-agent-turn)

</details>

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

상태는 `cancel_requested` → `rolling_back` → `cancelled`로 진행합니다. 실제 결과를 확인할 수 없거나 다른 사용자의 후속 변경과 충돌하거나 복구 도구가 실패하면 `rollback_failed`와 `error_code`를 남깁니다. 재요청하면 기존 복구 기록과 멱등키로 재시도합니다. 내용·이름·위치·순서를 복원하며 버전과 감사 기록은 이전 숫자로 되감지 않습니다.

Backend는 `delete_folder(require_empty=true)`로 빈 폴더를 잠금 상태에서 확인해 삭제하고, `delete_document`는 연결된 수집·Wiki 작업의 복구가 끝난 뒤 삭제합니다. Query·수집·변환·Skill 게시 등 공개 AI 기능의 전체 취소는 [공통 작업 취소 API](tasks.md)를 사용합니다. 이 내부 Agent API만 직접 호출하면 core DB의 채팅·업무 변경까지 복구하지 않습니다.

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

[↑ 요약으로 돌아가기](#summary-get-internal-agent-runs-run-id)

</details>

질의 답변은 평가자가 활성화된 경우에만 `query_evaluating`(답변 검토)을 평가 호출 직전에 발행하고, 검토 후 재작성하는 경우 `answer_retrying`을 발행합니다. 일반 대화 등 평가자를 호출하지 않는 경로에는 검토 단계를 표시하지 않습니다.

첫 문답은 최종 답변 이벤트를 먼저 전송한 뒤 같은 모델로 짧은 채팅 제목을 생성합니다. 별도의 `session_title` 이벤트로 document 서비스에 저장하며, 실패해도 완료된 답변은 바뀌지 않습니다. 이전 대화가 있으면 제목을 다시 생성하지 않습니다.

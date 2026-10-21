# Skills API

[서비스 문서](../README.md) / [ai-svc](README.md)

Skill 조회·작성·게시·설정 내부 API다. 공개 Gateway 계약은
[`document-svc Skills API`](https://github.com/FruitionKR/Fruition-document/blob/main/docs/api/skills.md)다. Backend가 사용자·워크스페이스·모델 정보를
검증해 추가한 뒤 6개 관리 API와 테스트 실행을 내부 HTTP로 호출한다. draft-from-runs·preview는 ai-svc 내부 기능이다.

- API 수: 9

## API 목차

| API | 목적 |
|---|---|
| [`POST /skills/tasks`](#summary-post-skills-tasks) | 작성·게시·수정을 취소 가능한 작업으로 실행합니다. |
| [`POST /skills/test-run`](#summary-post-skills-test-run) | 게시 전 Skill 정의를 저장하지 않고 문서 하나에 적용해 봅니다. |
| [`GET /skills`](#summary-get-skills) | 사용 가능한 Skill 목록을 조회합니다. |
| [`POST /skills/draft-from-runs/preview`](#summary-post-skills-draft-from-runs-preview) | 완료된 Agent 실행 결과로 게시 전 Skill 초안을 만듭니다. |
| [`POST /skills/preview`](#summary-post-skills-preview) | Skill 지침과 권한을 게시 전에 미리 검증합니다. |
| [`GET /skills/{skill_id}`](#summary-get-skills-skill-id) | Skill 상세 정보를 조회합니다. |
| [`DELETE /skills/{skill_id}`](#delete-skill) | Skill과 버전을 삭제하고 실행 기록을 보존합니다. |
| [`POST /skills/{skill_id}/disable`](#summary-post-skills-skill-id-disable) | Skill을 비활성화합니다. |
| [`POST /skills/{skill_id}/enable`](#summary-post-skills-skill-id-enable) | Skill을 활성화합니다. |

실행 결과 기반 초안 API는 `AGENT_SKILLS_ENABLED=true`, 나머지 8개 API는
`SKILL_API_ENABLED=true`일 때 노출된다. 모두 `X-Agent-Service-Token`으로 보호한다.
Skill 작성 분류는 완성된 동작에 필요한 capability를 모두 반환하며, 서버가 각 capability의
canonical Tool 집합을 합쳐 `allowed_tools`를 확정한다. 따라서 문서 편집 후 이동처럼 복합적인
Skill은 `document-edit`와 `folder-organize`를 함께 가지며 어느 한쪽 권한만으로 축약되지 않는다.

team Skill의 작성·게시·수정·삭제·켜고 끄기는 워크스페이스 OWNER와, OWNER에게 `skill.author` 권한을 받은 MEMBER만 한다
(Fruition-access ADR 0019). 권한은 access 내부 인가 응답의 `permissions`로 확인하며, 작성은 LLM을 호출하기 전에 거절한다.
개인 Skill은 소유자만 관리한다.

## 한눈에 보기

<a id="summary-get-skills"></a>
### `GET /skills`

| 항목 | 내용 |
|---|---|
| 목적 | 사용 가능한 Skill 목록을 조회합니다. |
| 입력 | **Query** — `workspace_id`: `string`, `user_id`: `string`<br>**Header** — `X-Agent-Service-Token`(필수, 인증 계층 검증): `string` / `null` |
| 출력 | `200` 성공 — 배열<`SkillResponse`> |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>필터링: `workspace_id`, `user_id`<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-get-skills"></a>
### `GET /skills` 상세

#### 1. Method + Path

`GET /skills`

#### 2. 목적

사용 가능한 Skill 목록을 조회합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| query | `workspace_id` | `string` | 예 | - |
| query | `user_id` | `string` | 예 | - |
| header | `X-Agent-Service-Token` | `X-Agent-Service-Token` | 예 (인증 계층 검증) | - |

- Body: 없음

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`Response List Skills Skills Get`)

```json
[
  {
    "enabled_version": {
      "allowed_tools": [
        "string"
      ],
      "capabilities": [
        "string"
      ],
      "description": "string",
      "id": "string",
      "instructions_markdown": "string",
      "lint_result": {
      },
      "name": "string",
      "result_mode": "auto",
      "requires_source": false,
      "status": "string",
      "targets": [
        "current_document"
      ],
      "version": 0
    },
    "id": "string",
    "latest_version": {
      "allowed_tools": [
        "string"
      ],
      "capabilities": [
        "string"
      ],
      "description": "string",
      "id": "string",
      "instructions_markdown": "string",
      "lint_result": {
      },
      "name": "string",
      "result_mode": "auto",
      "requires_source": false,
      "status": "string",
      "targets": [
        "current_document"
      ],
      "version": 0
    },
    "owner_user_id": "string",
    "scope_type": "string",
    "slug": "string",
    "status": "string",
    "workspace_id": "string"
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
- 필터링: `workspace_id`, `user_id`

#### 8. 권한 규칙

- 올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.
- 요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다.

#### 9. 예시 요청/응답

```bash
curl -X GET "$PIPELINE/skills?workspace_id=<value>&user_id=<value>" \
  -H 'X-Agent-Service-Token: <value>'
```

```json
[
  {
    "enabled_version": {
      "allowed_tools": [
        "string"
      ],
      "capabilities": [
        "string"
      ],
      "description": "string",
      "id": "string",
      "instructions_markdown": "string",
      "lint_result": {
      },
      "name": "string",
      "result_mode": "auto",
      "requires_source": false,
      "status": "string",
      "targets": [
        "current_document"
      ],
      "version": 0
    },
    "id": "string",
    "latest_version": {
      "allowed_tools": [
        "string"
      ],
      "capabilities": [
        "string"
      ],
      "description": "string",
      "id": "string",
      "instructions_markdown": "string",
      "lint_result": {
      },
      "name": "string",
      "result_mode": "auto",
      "requires_source": false,
      "status": "string",
      "targets": [
        "current_document"
      ],
      "version": 0
    },
    "owner_user_id": "string",
    "scope_type": "string",
    "slug": "string",
    "status": "string",
    "workspace_id": "string"
  }
]
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/skill/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: list_skills_skills_get`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/skill/repository/PipelineSkillRequester.java`:58-63 → 114 (`app.skill.endpoint`, `X-Agent-Service-Token`).
- 아웃바운드 호출: 없음(AI DB 조회).
- 미연동 표시: 없음.

[↑ 요약으로 돌아가기](#summary-get-skills)

</details>

<a id="summary-post-skills-tasks"></a>
### `POST /skills/tasks`

#### 1. Method + Path

`POST /skills/tasks`

#### 2. 목적

Skill 작성·게시·수정을 작업 ID로 기록하고 취소 시 게시 전 상태를 복원합니다.

#### 3. Auth 필요 여부

`X-Agent-Service-Token`이 필요합니다. Backend가 사용자와 workspace 권한을 검증합니다.

#### 4. Request body

공통 envelope는 `run_id`, `workspace_id`, `user_id`, `kind`, `payload`입니다.
`kind`는 `skill_author`, `skill_publish`, `skill_update` 중 하나이고 수정에는 `skill_id`도 필요합니다.
`payload`는 [공개 Skill API](https://github.com/FruitionKR/Fruition-document/blob/main/docs/api/skills.md)의 해당 body에 backend가 검증한
`workspace_id`, `user_id`, `provider`, `model`을 넣은 값입니다. envelope와 payload의 actor는 같아야 합니다.
게시에는 초안의 `capabilities`, `allowed_tools`를 전달하며 서버가 다시 검증합니다.
`capabilities`(게시 필수, 수정 선택)는 요청 스키마가 문자열 배열로만 받고, 빈 배열·허용 값(`document-create`, `document-edit`, `folder-organize`, `template`) 밖의 값·중복은 LLM 검토 전에 `400 {"detail": {"code": "invalid_capabilities", "message"}}`로 거절합니다(게시·수정 같음). 필드 누락(게시)이나 배열이 아닌 값처럼 타입이 틀리면 지금처럼 `422`입니다.
게시(`skill_publish`)의 `allowed_tools`는 선택입니다. 생략하거나 `[]`이면 서버가 `capabilities`별 canonical Tool의
합집합(`policy.canonical_allowed_tools`: 계획 읽기 `list_root_items`·`list_folder_children`을 앞에 두고 나머지는 이름순)으로
계산해 같은 검증을 거치고, 응답 `allowed_tools`로 돌려줍니다. 값을 보내면 지금처럼 검증만 합니다.

문서 Skill(`document-create`·`document-edit` 포함)은 게시·수정 payload에 실행 대상 `targets`와 결과 방식 `result_mode`를 선택으로 담습니다.

| 필드 | 타입 | 값 | 규칙 |
|---|---|---|---|
| `targets` | `string[]` | `current_document`, `selection` (최대 2개, 중복 불가) | 게시에서 생략하거나 `[]`이면 `["current_document"]`. 수정에서 생략(키 없음)하면 기존 값 유지, `[]`이면 기본값으로 되돌림. 저장 순서는 `current_document`, `selection`으로 고정 |
| `result_mode` | `string` / `null` | `create`(새 문서), `edit`(현재 문서 수정), `auto`(상황에 맞게) | 게시에서 생략하면 `null`. 수정에서 생략하면 기존 값 유지 |

- `result_mode`는 `capabilities`와 맞아야 합니다: `create`→`document-create`, `edit`→`document-edit`, `auto`→둘 다. 어긋나면 `400 {"detail": {"code": "invalid_result_mode"}}`이며 LLM 검토 전에 거절합니다(수정은 보낸 `capabilities`, 생략했으면 이전 버전에서 이어받은 capabilities로 검사).
- 문서 Skill이 아닌데 `targets`나 `result_mode`를 보내거나 `targets`가 중복이면 `400` `invalid_targets`/`invalid_result_mode`입니다.
- 수정(`skill_update`)은 보낸 필드만 바꾸는 부분 수정입니다. 지침(`instructions_markdown`)을 고쳐도 의도를 다시 분류하지 않고 이전 버전의 `capabilities`·`allowed_tools`를 그대로 이어받으므로, 보내지 않은 `targets`·`result_mode`도 그대로 유지됩니다(지침이 바뀌면 안전 검토만 다시 합니다). 의도 분류는 작성(`skill_author`) 초안에서만 합니다.
- 수정에서 Skill 종류·결과 방식을 바꾸려면 `capabilities`(선택, `string[]`, 1개 이상)를 보냅니다. 값과 검증은 게시와 같습니다(빈 배열·허용 값 밖·중복은 `400` `invalid_capabilities`, LLM 검토 전에 거절). 보내면:
  - `allowed_tools`는 새 `capabilities`로 다시 계산합니다(`policy.canonical_allowed_tools`).
  - 문서 Skill이 아니게 되면 `targets`는 `[]`, `result_mode`는 `null`로 저장합니다. 이때 `targets`·`result_mode`를 보내면 게시와 같이 `400` `invalid_targets`/`invalid_result_mode`입니다.
  - 문서 Skill이면 생략한 `result_mode`는 이전 값 대신 `capabilities`에서 정합니다(`document-edit`→`edit`, `document-create`→`create`, 둘 다→`auto`). 생략한 `targets`는 이전 버전도 문서 Skill이었으면 이어받고, 아니면 `["current_document"]`입니다. 보낸 `result_mode`가 `capabilities`와 맞지 않으면 `400` `invalid_result_mode`입니다.
  - `requires_source`는 위 규칙과 같습니다(생략하면 이전 값, 새 문서를 만들지 않는 Skill이 되면 `false`).
  - 예: `["document-edit","folder-organize"]`·`targets: ["current_document"]`로 잘못 저장된 폴더 정리 Skill에 `capabilities: ["folder-organize"]`를 보내면 폴더 정리 Tool 표, `targets: []`, `result_mode: null`로 복구됩니다.
  - 생략하면 지금처럼 이전 버전 값을 이어받고 다시 분류하지 않습니다.
- 응답(`SkillAuthoringResponse`, `SkillVersionResponse`)에 두 필드가 포함됩니다. 컬럼 추가 전에 게시된 문서 Skill은 지금처럼 선택 범위 편집을 유지하도록 `targets: ["current_document", "selection"]`, `result_mode: null`로 읽습니다. 이런 Skill을 수정하면서 `targets`를 생략하면 이 값을 이어받습니다.
- Agent 실행 반영: `targets`가 `["selection"]`뿐이면 에디터 선택 범위(`target.type="selection"`)가 없을 때 편집하지 않고 되묻고, `["current_document"]`뿐이면 선택이 있어도 문서 전체를 편집합니다. `result_mode`가 `create`/`edit`이면 라우터가 고른 문서 동작을 그 방식으로 맞추고(`/slug`만 보낸 실행은 고른 문서·폴더로 동작: [Agent API](agent.md#할-일-없이-slug만-보낸-턴skill-실행)), `auto`는 문서가 열려 있고 라우터 확신도가 0.7 미만이면 새 문서/수정 중 무엇인지 되묻습니다.

새 문서를 만드는 Skill은 게시·수정 payload에 원문 필요 여부 `requires_source`를 선택으로 담습니다.

| 필드 | 타입 | 값 | 규칙 |
|---|---|---|---|
| `requires_source` | `boolean` / `null` | `true`(고른 문서·폴더 본문을 원문으로 읽음), `false`(원문 없이 위치로만 씀) | 보내면 그 값을 저장한다. 생략(`null`)하면 아래 규칙으로 정한다 |

- 정하는 순서: 사용자가 보낸 값 → 지침에 `{문서}` 자리표시자가 있으면 `true` → `template` Skill이면 `false` → 의도 분류 LLM 판단(작성 초안의 `capabilities` 분류와 같은 호출의 `requires_source` 필드) → `false`.
- 새 문서를 만들지 않는 Skill(결과 방식 `edit`, `document-edit`·`folder-organize`만 있는 Skill)에서는 의미가 없어 보낸 값을 무시하고 `false`로 저장합니다(거절하지 않음).
- 작성(`skill_author`) 초안 응답의 `requires_source`는 규칙·분류로 판단한 값입니다. 게시는 의도를 다시 분류하지 않으므로, 초안의 판단을 유지하려면 게시 payload에 초안의 `requires_source`(사용자가 바꾼 값)를 그대로 보내야 합니다. 생략하면 `{문서}`·`template` 규칙만 적용되고 그 밖은 `false`입니다.
- 수정에서 생략하면 지침을 고쳤어도 이전 값을 이어받습니다(이전 버전이 새 문서를 만들지 않던 Skill이면 위 규칙으로 정합니다).
- 응답(`SkillAuthoringResponse`, `SkillVersionResponse`)에 항상 `boolean`으로 포함됩니다. 컬럼 추가 전에 게시된 Skill(저장값 없음)은 이전 동작(명시한 생성 Skill은 고른 문서를 원문으로 씀)을 지키도록 조회할 때 계산합니다: `template`이 있으면 `false`, `document-create`가 있고 결과 방식이 `edit`가 아니면(`create`·`auto`·`null`) `true`, 그 밖은 `false`. 이 값은 수정에서 `requires_source`를 생략하면 그대로 이어받습니다.
- Agent 실행 반영: [Agent API](agent.md#할-일-없이-slug만-보낸-턴skill-실행)의 새 문서 원문 규칙을 따릅니다.

#### 5. Response body

작성은 초안, 게시·수정은 Skill 응답에 `run_id`를 추가해 `200`으로 반환합니다.

#### 6. Error response

입력 검증은 `422`, 참조 길이 초과는 `413`, 권한·게시 검증 실패는 해당 Skill 오류를 반환합니다.
취소·충돌 상태는 [작업 상태 API](tasks.md)에서 확인합니다.
처리가 55초(document 대기 60초보다 짧게)를 넘으면 `504` `deadline_exceeded`로 실패합니다. 게시·수정은 반영하지 않고 변경을 되돌리며, 같은 `run_id`로 다시 보내면 `409`입니다.

작성(`skill_author`) 요청과 게시·수정의 실행 옵션 검증이 거절되면 `400 {"detail": {"code", "message"}}`로 사유를 구분합니다.
위험 표현은 오류가 아니라 `200` `status: "blocked"`와 `issues[]`로 반환합니다.

| code | 의미 |
|---|---|
| `intent_ambiguous` | 수행할 작업을 특정할 수 없음(무의미한 문자열 포함) |
| `intent_unsupported` | Agent가 지원하지 않는 작업(메시지 발송, 외부 서비스 등) |
| `invalid_capabilities` | 게시·수정의 `capabilities`가 빈 배열이거나 허용 값 밖의 값·중복을 포함 |
| `invalid_instruction_length` | 지침이 비었거나 최대 길이를 넘음 |
| `invalid_name` | Skill 이름 형식 오류 |
| `invalid_reference` | 참조 문서 개수·중복·빈 id, 접근 불가, 빈 본문, 문서별 40,000자·합계 80,000자 초과 |
| `invalid_result_mode` | 게시·수정의 `result_mode`가 `capabilities`와 맞지 않거나 문서 Skill이 아님 |
| `invalid_targets` | 게시·수정의 `targets` 중복, 또는 문서 Skill이 아닌데 `targets`를 보냄 |
| `skill_request_invalid` | 그 밖의 작성 실패(LLM 출력 계약 위반 등) |

단, 이름 형식(`name` 패턴)과 참조 문서 개수(최대 3개)는 요청 스키마가 먼저 검증하므로 `400` 코드가 아니라 `422` `HTTPValidationError`로 반환됩니다.

실패로 닫힌 작업을 같은 `run_id`로 다시 요청하면 작업을 재실행하지 않습니다.
거절 코드로 실패했으면 같은 `code`로 `400`, 그 밖의 실패는 `409`(`code`: 저장된 `error_code`, 보통 `task_failed`)를 반환합니다.
같은 `run_id`에 다른 command를 보내면 `409` `task_command_mismatch`입니다.
취소된 작업은 `409` `task_cancelled`, 아직 실행 중인 작업을 다시 보내면 `409` `task_in_progress`입니다.

#### 7. Pagination / filtering

없음.

#### 8. 권한 규칙

같은 `run_id`는 actor·kind·payload가 모두 같을 때만 재사용할 수 있습니다.
완료 응답은 재생하며, 취소한 작업은 다시 실행하지 않습니다.

#### 9. 예시 요청/응답

```json
{"run_id":"skill_example","kind":"skill_author","workspace_id":"ws_example","user_id":"user_example","payload":{"workspace_id":"ws_example","user_id":"user_example","scope_type":"personal","instruction":"문서를 주제별로 정리하는 Skill을 만들어 줘","provider":"openai","model":"gpt-6-luna"}}
```

각 payload의 필수 필드와 응답 스키마는 생성 OpenAPI 및 공개 Skill 계약을 따릅니다.

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/skill/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: execute_skill_task_skills_tasks_post`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/skill/repository/PipelineSkillRequester.java`:102-107 → 124 (`app.skill.endpoint`, `X-Agent-Service-Token`). `kind`는 `skill_author`·`skill_publish`·`skill_update`다.
- 아웃바운드 호출: LLM provider. 참조 읽기는 document-svc `POST /internal/agent/skill-authoring/references/read`(`pipeline/app/modules/skill/infrastructure/backend_skill_reference_reader.py`:27, `DOCUMENT_INTERNAL_BASE_URL`), 권한 확인은 access-svc `GET /internal/authz/workspaces/{workspace_id}/users/{user_id}`(`pipeline/app/modules/skill/infrastructure/workspace_authorization.py`:19).
- 미연동 표시: 없음.

<a id="summary-post-skills-test-run"></a>
### `POST /skills/test-run`

| 항목 | 내용 |
|---|---|
| 목적 | 게시 전 Skill 정의를 저장하지 않고 문서 하나에 적용해 섹션별 변경 전/후를 돌려줍니다. |
| 입력 | **Header** — `X-Agent-Service-Token`: 필수, `X-Request-Id`(필수, 사용량 원장·작업 `run_id`)<br>**Body** — `SkillTestRunRequest` |
| 출력 | `200` — `SkillTestRunResponse` (`completed` 또는 `blocked`) |
| 조건 | `SKILL_API_ENABLED=true`. Skill·Skill 버전·Agent run·문서를 만들거나 바꾸지 않고, 문서 쓰기 Tool을 호출하지 않습니다. |
| 주요 오류 | `400` `test_run_unsupported`(폴더 정리 Skill)·`request_id_required`·거절 code·범위 오류<br>`409` 취소·실행 중·같은 `run_id` 재요청<br>`422` 요청 검증 실패<br>`504` `deadline_exceeded` |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-skills-test-run"></a>
### `POST /skills/test-run` 상세

#### 1. Method + Path

`POST /skills/test-run`

#### 2. 목적

"스킬 만들기" 시트의 테스트 실행 카드용입니다. document-svc가 actor·워크스페이스 모델·문서 본문을 검증해 채워 보냅니다.

1. capability가 `folder-organize`를 포함하면 LLM 호출 없이 `400 {"detail": {"code": "test_run_unsupported"}}`입니다.
2. 게시(`skill_publish`)와 같은 안전 검토(`AuthorSkillUseCase.execute(... authoring_mode="preserve")`, Tool 목록은 `capabilities`로 계산)를 먼저 거칩니다. 막히면 편집 LLM을 부르지 않고 `200 {"status": "blocked", "changes": [], "issues": [...]}`를 돌려줍니다(`issues`는 `/skills/tasks` 작성의 `blocked`와 같은 형식).
3. `document-edit`를 가진 Skill은 Agent `markdown_edit`와 같은 `GenerateMarkdownEditUseCase`에 검토된 초안 지침을 `skill_instructions`로 넣어 편집안을 만들고, 편집이 닿은 섹션을 `changes`, 닿지 않은 섹션 제목을 `untouched_sections`, 적용한 전체 본문을 `result_markdown`으로 돌려줍니다.
4. 문서 생성형(`document-create`·`template`, `document-edit` 없음)은 고른 문서를 참고 자료로 새 문서를 만들어 `result_markdown`만 돌려줍니다(`changes`는 빈 배열).

모델은 요청의 `provider`·`model`이며 워크스페이스 모델 정책(`AI_ALLOWED_LLM_MODELS`, Claude 불가)을 벗어나면 워크스페이스 기본 모델로 대체합니다(게시와 같음).

#### 3. Auth 필요 여부

`X-Agent-Service-Token`이 필요합니다. 문서 읽기 권한과 크레딧은 document-svc가 확인합니다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Request-Id` | `string` | 예 | 사용량 원장 `run_id`이자 작업 journal `run_id`(`kind=skill_test_run`) |
| body | `definition` | object | 예 | `name`, `description`, `instructions_markdown`, `capabilities`. `allowed_tools`는 받지 않습니다(`422`). |
| body | `document` | object | 예 | `document_id`, `base_version`, `markdown` |
| body | `target` | object | 아니오 | `selection`·`current_section`·`whole_document`, 1-based `start_line`·`end_line`. 생략하면 문서 전체. 줄 범위를 벗어나면 LLM 호출 전 `400` |

```json
{
  "workspace_id": "ws_123",
  "user_id": "user_123",
  "provider": "openai",
  "model": "gpt-6-luna",
  "definition": {
    "name": "research-summary",
    "description": "연구 문서의 개요를 핵심 문장 3개로 정리합니다.",
    "instructions_markdown": "'연구 개요' 섹션만 고친다. 다른 섹션은 바꾸지 않는다.",
    "capabilities": ["document-edit"]
  },
  "document": {"document_id": "doc_123", "base_version": 7, "markdown": "# 연구 개요\n...\n# 방법\n..."},
  "target": {"type": "selection", "start_line": 1, "end_line": 6}
}
```

#### 5. Response body

```json
{
  "status": "completed",
  "changes": [
    {"section": "연구 개요", "start_line": 1, "end_line": 6, "before": "...", "after": "..."}
  ],
  "untouched_sections": ["방법", "결과"],
  "result_markdown": "# 연구 개요\n...",
  "issues": []
}
```

- `changes`는 편집이 닿은 원본 섹션들을 하나의 블록으로 묶습니다. 여러 섹션에 걸치면 `section`이 제목을 ` · `로 잇고, `start_line`·`end_line`은 원본 기준입니다. 제목 전 본문은 `(제목 없음)` 섹션입니다.
- 바뀐 내용이 없으면 `changes`는 빈 배열이고 모든 섹션이 `untouched_sections`에 들어갑니다.

#### 6. Error response

| 상태 | code | 의미 |
|---|---|---|
| `400` | `test_run_unsupported` | 폴더 정리 Skill |
| `400` | `request_id_required` | `X-Request-Id` 없음 |
| `400` | `invalid_name` 등 | 작성 거절 code(`/skills/tasks`와 같음) |
| `409` | `task_cancelled`·`task_in_progress`·`task_command_mismatch` | 취소됨·실행 중·다른 actor로 같은 `run_id` |
| `409` | `task_already_completed` | 끝난 `run_id` 재요청. 결과에 문서 본문이 있어 보관하지 않으므로 새 `run_id`로 다시 보냅니다. |
| `504` | `deadline_exceeded` | 55초 초과 |

#### 7. Pagination / filtering

없음.

#### 8. 권한 규칙

- 저장하지 않음: `skills`·`skill_versions`·`agent_runs` 행을 만들지 않고 `apply_document_edit`·`create_document` 등 문서 쓰기 Tool을 호출하지 않습니다.
- 실행 추적: 작업 journal(`ai_task_runs`)에 본문 없는 신원 command(`run_id`, `kind=skill_test_run`, `workspace_id`, `user_id`)만 남기고, 끝나면 결과도 `{"scrubbed": true}`로 지웁니다.
- 사용량: 안전 검토·편집 LLM 호출을 `X-Request-Id` run(`kind=skill_test_run`)으로 원장에 기록합니다(과금 대상).
- 취소: 55초 내부 deadline과 공통 취소 API는 `/skills/tasks`와 같습니다. document가 보내는 취소 command(본문 대신 `markdown_sha256`)는 `kind=skill_test_run`이면 같은 신원 command로 맞춰 등록하므로 해시가 어긋나지 않고, 되돌릴 변경이 없어 `cancelled`로 끝납니다([작업 취소](tasks.md)).

#### 9. 예시 요청/응답

위 4·5절과 같습니다.

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/skill/interfaces/http/routes.py` (`run_skill_test`)
- 실행: `pipeline/app/modules/skill/application/skill_test_run.py`, 섹션 변경: `pipeline/app/modules/skill/domain/section_changes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: run_skill_test_skills_test_run_post`)

#### 연동

- 인바운드 호출자: Fruition-document `PipelineSkillRequester.testRun`(FruitionKR/Fruition-document#148, `X-Request-Id`=run_id).
- 아웃바운드 호출: LLM provider. 문서·access 서비스는 호출하지 않습니다.
- 미연동 표시: document#148 배포 전에는 호출자 없음.

</details>

[↑ 요약으로 돌아가기](#summary-post-skills-test-run)

<a id="summary-post-skills-draft-from-runs-preview"></a>
### `POST /skills/draft-from-runs/preview`

| 항목 | 내용 |
|---|---|
| 목적 | 완료된 Agent 실행의 성공 작업을 일반화해 게시 전 Skill 초안을 만듭니다. |
| 입력 | **Header** — `X-Agent-Service-Token`: 필수, `X-Request-Id`(선택, 사용량 원장 run_id): `string` / `null`<br>**Body** — `SkillDraftProposalRequest` |
| 출력 | `200` — `SkillAuthoringResponse` |
| 조건 | `AGENT_SKILLS_ENABLED=true`일 때만 노출됩니다. source run은 completed 상태이며 성공 operation이 하나 이상이어야 합니다. |
| 주요 오류 | `400` source·지침·보안 검증 실패<br>`422` 요청 검증 실패 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-skills-draft-from-runs-preview"></a>
### `POST /skills/draft-from-runs/preview` 상세

#### 1. Method + Path

`POST /skills/draft-from-runs/preview`

#### 2. 목적

수동 내부 호출로 전달한 Agent run 요약에서 재사용 가능한 절차만 추출합니다.
결과는 바로 게시하지 않고 기존 Skill 보안 검사를 거친 `proposal_ready`, `blocked` 또는
`clarification_required` 응답으로 반환합니다.

현재 제품의 Agent 경로는 이 HTTP endpoint를 호출하지 않습니다. Backend의 `AgentTurnService`가
선택한 run을 workspace·user 범위와 완료 상태로 검증해 Kafka `ai.agent.command`에 싣고,
ai-svc Agent handler가 같은 application use case를 직접 호출합니다.

#### 3. Auth 필요 여부

- 필요: `X-Agent-Service-Token`

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Agent-Service-Token` | `X-Agent-Service-Token` | 예 (인증 계층 검증) | - |
| header | `X-Request-Id` | `string` | 아니오 | 사용량 원장 `run_id`. 없으면 `unattributed`로 기록한다. |

```json
{
  "provider": "openai",
  "model": "gpt-6-luna",
  "workspace_id": "workspace_123",
  "user_id": "user_123",
  "scope_type": "personal",
  "source_runs": [
    {
      "run_id": "run_123",
      "status": "completed",
      "request_summary": "주간 회의록 문서를 만들고 업무 폴더로 이동",
      "plan_summary": "회의록 생성 후 지정 폴더로 이동",
      "successful_operations": [
        {
          "tool_name": "create_document",
          "reason": "회의록 문서를 생성했습니다."
        },
        {
          "tool_name": "move_document",
          "reason": "업무 폴더로 이동했습니다."
        }
      ]
    }
  ],
  "user_directives": ["회의록 형식을 유지해줘"],
  "excluded_literals": ["2026년 8월 18일"]
}
```

#### 5. Response body

- HTTP `200`: Skill 초안 생성 성공

```json
{
  "status": "proposal_ready",
  "question": null,
  "skill_id": null,
  "version_id": null,
  "scope_type": "personal",
  "name": "weekly-meeting-notes",
  "description": "주간 회의록을 만들고 지정 폴더에 정리합니다.",
  "skill_markdown": "---\nname: weekly-meeting-notes\n---\n",
  "instructions_markdown": "회의 내용을 회의록 형식으로 작성하고 지정 폴더에 정리합니다.",
  "capabilities": ["document-create", "folder-organize"],
  "allowed_tools": ["create_document", "move_document"],
  "issues": []
}
```

#### 6. Error response

- `400`: source run 의미, 사용자 지침 또는 초안 검증 실패
- `401`: Agent 서비스 토큰 누락·불일치
- `422`: 요청 schema 검증 실패
- `503`: Agent 서비스 토큰 미설정

#### 7. Pagination / filtering

- 페이지네이션과 필터링을 지원하지 않음

#### 8. 권한 규칙

이 endpoint는 전달받은 run 요약의 소유권을 자체 조회하지 않습니다. 수동으로 호출할 때도
workspace·user 소유권과 완료 상태를 확인한 canonical run 결과만 전달해야 합니다.

#### 9. 예시 요청/응답

위 request·response 예시와 같습니다. 실제 게시에는 별도로 `POST /skills/tasks` (`kind=skill_publish`) 승인이 필요합니다.

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/skill/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: propose_skill_draft_skills_draft_from_runs_preview_post`)

#### 연동

- 인바운드 호출자: **호출자 없음.** document-svc에 `draft-from-runs` 문자열이 없다. ai-svc 내부 기능이다.
- 아웃바운드 호출: LLM provider.
- 미연동 표시: **미연동.** 추가로 `AGENT_SKILLS_ENABLED=true`가 아니면 노출 자체가 되지 않는다(기본값 false).

</details>

<a id="summary-post-skills-preview"></a>
### `POST /skills/preview`

| 항목 | 내용 |
|---|---|
| 목적 | Skill 지침과 권한을 게시 전에 미리 검증합니다. |
| 입력 | **Header** — `X-Agent-Service-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `SkillDefinitionRequest` |
| 출력 | `200` 성공 — `SkillPreviewResponse` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-skills-preview"></a>
### `POST /skills/preview` 상세

#### 1. Method + Path

`POST /skills/preview`

#### 2. 목적

Skill 지침과 권한을 게시 전에 미리 검증합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Agent-Service-Token` | `X-Agent-Service-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`SkillDefinitionRequest`)

`allowed_tools`를 생략하거나 `[]`로 보내면 게시와 같은 규칙으로 `capabilities`에서 계산해 검증합니다.

```json
{
  "allowed_tools": [
    "list_root_items"
  ],
  "capabilities": [
    "document-create"
  ],
  "description": "string",
  "instructions_markdown": "string",
  "name": "example",
  "user_id": "string"
}
```

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`SkillPreviewResponse`)

```json
{
  "has_blocked_issues": true,
  "lint_result": {
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
curl -X POST "$PIPELINE/skills/preview" \
  -H 'X-Agent-Service-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"allowed_tools":["list_root_items"],"capabilities":["document-create"],"description":"회의록 요약 Skill","instructions_markdown":"회의 내용을 결정 사항과 할 일로 정리한다.","name":"meeting-summary","user_id":"<value>"}'
```

```json
{
  "has_blocked_issues": true,
  "lint_result": {
  }
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/skill/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: preview_skill_skills_preview_post`)

#### 연동

- 인바운드 호출자: **호출자 없음.** document-svc에 `skills/preview` 문자열이 없다. 작성·게시·수정은 모두 `POST /skills/tasks`로 간다.
- 아웃바운드 호출: LLM provider.
- 미연동 표시: **미연동.**

[↑ 요약으로 돌아가기](#summary-post-skills-preview)

</details>

<a id="summary-get-skills-skill-id"></a>
### `GET /skills/{skill_id}`

| 항목 | 내용 |
|---|---|
| 목적 | Skill 상세 정보를 조회합니다. |
| 입력 | **Path** — `skill_id`: `string`<br>**Query** — `workspace_id`: `string`, `user_id`: `string`<br>**Header** — `X-Agent-Service-Token`(필수, 인증 계층 검증): `string` / `null` |
| 출력 | `200` 성공 — `SkillResponse` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>필터링: `workspace_id`, `user_id`<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-get-skills-skill-id"></a>
### `GET /skills/{skill_id}` 상세

#### 1. Method + Path

`GET /skills/{skill_id}`

#### 2. 목적

Skill 상세 정보를 조회합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| path | `skill_id` | `string` | 예 | - |
| query | `workspace_id` | `string` | 예 | - |
| query | `user_id` | `string` | 예 | - |
| header | `X-Agent-Service-Token` | `X-Agent-Service-Token` | 예 (인증 계층 검증) | - |

- Body: 없음

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`SkillResponse`)

```json
{
  "enabled_version": {
    "allowed_tools": [
      "string"
    ],
    "capabilities": [
      "string"
    ],
    "description": "string",
    "id": "string",
    "instructions_markdown": "string",
    "lint_result": {
    },
    "name": "string",
    "result_mode": null,
    "requires_source": false,
    "status": "string",
    "targets": [
      "current_document"
    ],
    "version": 1
  },
  "id": "string",
  "latest_version": {
    "allowed_tools": [
      "string"
    ],
    "capabilities": [
      "string"
    ],
    "description": "string",
    "id": "string",
    "instructions_markdown": "string",
    "lint_result": {
    },
    "name": "string",
    "result_mode": null,
    "requires_source": false,
    "status": "string",
    "targets": [
      "current_document"
    ],
    "version": 1
  },
  "owner_user_id": "string",
  "scope_type": "string",
  "slug": "string",
  "status": "string",
  "workspace_id": "string"
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
curl -X GET "$PIPELINE/skills/<value>?workspace_id=<value>&user_id=<value>" \
  -H 'X-Agent-Service-Token: <value>'
```

```json
{
  "enabled_version": {
    "allowed_tools": [
      "string"
    ],
    "capabilities": [
      "string"
    ],
    "description": "string",
    "id": "string",
    "instructions_markdown": "string",
    "lint_result": {
    },
    "name": "string",
    "result_mode": null,
    "requires_source": false,
    "status": "string",
    "targets": [
      "current_document"
    ],
    "version": 1
  },
  "id": "string",
  "latest_version": {
    "allowed_tools": [
      "string"
    ],
    "capabilities": [
      "string"
    ],
    "description": "string",
    "id": "string",
    "instructions_markdown": "string",
    "lint_result": {
    },
    "name": "string",
    "result_mode": null,
    "requires_source": false,
    "status": "string",
    "targets": [
      "current_document"
    ],
    "version": 1
  },
  "owner_user_id": "string",
  "scope_type": "string",
  "slug": "string",
  "status": "string",
  "workspace_id": "string"
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/skill/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: get_skill_skills__skill_id__get`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/skill/repository/PipelineSkillRequester.java`:66-70 → 114.
- 아웃바운드 호출: 없음.
- 미연동 표시: 없음.

[↑ 요약으로 돌아가기](#summary-get-skills-skill-id)

</details>

<a id="summary-post-skills-skill-id-disable"></a>
### `POST /skills/{skill_id}/disable`

| 항목 | 내용 |
|---|---|
| 목적 | Skill을 비활성화합니다. |
| 입력 | **Path** — `skill_id`: `string`<br>**Header** — `X-Agent-Service-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `SkillActorRequest` |
| 출력 | `200` 성공 — `SkillResponse` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-skills-skill-id-disable"></a>
### `POST /skills/{skill_id}/disable` 상세

#### 1. Method + Path

`POST /skills/{skill_id}/disable`

#### 2. 목적

Skill을 비활성화합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| path | `skill_id` | `string` | 예 | - |
| header | `X-Agent-Service-Token` | `X-Agent-Service-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`SkillActorRequest`)

```json
{
  "user_id": "string",
  "workspace_id": "string"
}
```

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`SkillResponse`)

```json
{
  "enabled_version": {
    "allowed_tools": [
      "string"
    ],
    "capabilities": [
      "string"
    ],
    "description": "string",
    "id": "string",
    "instructions_markdown": "string",
    "lint_result": {
    },
    "name": "string",
    "result_mode": null,
    "requires_source": false,
    "status": "string",
    "targets": [
      "current_document"
    ],
    "version": 1
  },
  "id": "string",
  "latest_version": {
    "allowed_tools": [
      "string"
    ],
    "capabilities": [
      "string"
    ],
    "description": "string",
    "id": "string",
    "instructions_markdown": "string",
    "lint_result": {
    },
    "name": "string",
    "result_mode": null,
    "requires_source": false,
    "status": "string",
    "targets": [
      "current_document"
    ],
    "version": 1
  },
  "owner_user_id": "string",
  "scope_type": "string",
  "slug": "string",
  "status": "string",
  "workspace_id": "string"
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
curl -X POST "$PIPELINE/skills/<value>/disable" \
  -H 'X-Agent-Service-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"user_id":"<value>","workspace_id":"<value>"}'
```

```json
{
  "enabled_version": {
    "allowed_tools": [
      "string"
    ],
    "capabilities": [
      "string"
    ],
    "description": "string",
    "id": "string",
    "instructions_markdown": "string",
    "lint_result": {
    },
    "name": "string",
    "result_mode": null,
    "requires_source": false,
    "status": "string",
    "targets": [
      "current_document"
    ],
    "version": 1
  },
  "id": "string",
  "latest_version": {
    "allowed_tools": [
      "string"
    ],
    "capabilities": [
      "string"
    ],
    "description": "string",
    "id": "string",
    "instructions_markdown": "string",
    "lint_result": {
    },
    "name": "string",
    "result_mode": null,
    "requires_source": false,
    "status": "string",
    "targets": [
      "current_document"
    ],
    "version": 1
  },
  "owner_user_id": "string",
  "scope_type": "string",
  "slug": "string",
  "status": "string",
  "workspace_id": "string"
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/skill/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: disable_skill_skills__skill_id__disable_post`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/skill/repository/PipelineSkillRequester.java`:82-83 → 124.
- 아웃바운드 호출: 없음.
- 미연동 표시: 없음.

[↑ 요약으로 돌아가기](#summary-post-skills-skill-id-disable)

</details>

<a id="summary-post-skills-skill-id-enable"></a>
### `POST /skills/{skill_id}/enable`

| 항목 | 내용 |
|---|---|
| 목적 | Skill을 활성화합니다. |
| 입력 | **Path** — `skill_id`: `string`<br>**Header** — `X-Agent-Service-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `SkillActorRequest` |
| 출력 | `200` 성공 — `SkillResponse` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-skills-skill-id-enable"></a>
### `POST /skills/{skill_id}/enable` 상세

#### 1. Method + Path

`POST /skills/{skill_id}/enable`

#### 2. 목적

Skill을 활성화합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| path | `skill_id` | `string` | 예 | - |
| header | `X-Agent-Service-Token` | `X-Agent-Service-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`SkillActorRequest`)

```json
{
  "user_id": "string",
  "workspace_id": "string"
}
```

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`SkillResponse`)

```json
{
  "enabled_version": {
    "allowed_tools": [
      "string"
    ],
    "capabilities": [
      "string"
    ],
    "description": "string",
    "id": "string",
    "instructions_markdown": "string",
    "lint_result": {
    },
    "name": "string",
    "result_mode": null,
    "requires_source": false,
    "status": "string",
    "targets": [
      "current_document"
    ],
    "version": 1
  },
  "id": "string",
  "latest_version": {
    "allowed_tools": [
      "string"
    ],
    "capabilities": [
      "string"
    ],
    "description": "string",
    "id": "string",
    "instructions_markdown": "string",
    "lint_result": {
    },
    "name": "string",
    "result_mode": null,
    "requires_source": false,
    "status": "string",
    "targets": [
      "current_document"
    ],
    "version": 1
  },
  "owner_user_id": "string",
  "scope_type": "string",
  "slug": "string",
  "status": "string",
  "workspace_id": "string"
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
curl -X POST "$PIPELINE/skills/<value>/enable" \
  -H 'X-Agent-Service-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"user_id":"<value>","workspace_id":"<value>"}'
```

```json
{
  "enabled_version": {
    "allowed_tools": [
      "string"
    ],
    "capabilities": [
      "string"
    ],
    "description": "string",
    "id": "string",
    "instructions_markdown": "string",
    "lint_result": {
    },
    "name": "string",
    "result_mode": null,
    "requires_source": false,
    "status": "string",
    "targets": [
      "current_document"
    ],
    "version": 1
  },
  "id": "string",
  "latest_version": {
    "allowed_tools": [
      "string"
    ],
    "capabilities": [
      "string"
    ],
    "description": "string",
    "id": "string",
    "instructions_markdown": "string",
    "lint_result": {
    },
    "name": "string",
    "result_mode": null,
    "requires_source": false,
    "status": "string",
    "targets": [
      "current_document"
    ],
    "version": 1
  },
  "owner_user_id": "string",
  "scope_type": "string",
  "slug": "string",
  "status": "string",
  "workspace_id": "string"
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/skill/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: enable_skill_skills__skill_id__enable_post`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/skill/repository/PipelineSkillRequester.java`:82-83 → 124.
- 아웃바운드 호출: 없음.
- 미연동 표시: 없음.

[↑ 요약으로 돌아가기](#summary-post-skills-skill-id-enable)

</details>

<a id="delete-skill"></a>
### `DELETE /skills/{skill_id}`

#### 1. Method + Path

`DELETE /skills/{skill_id}`

#### 2. 목적

Skill과 그 버전을 삭제하고 과거 실행 기록은 보존합니다.

#### 3. Auth 필요 여부

- 필요
- `SKILL_API_ENABLED=true`에서 노출하며 `X-Agent-Service-Token` 인증이 필요하다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| path | `skill_id` | `string` | 예 | 삭제할 Skill |
| query | `workspace_id` | `string` | 예 | 공개 API에서 검증한 workspace |
| query | `user_id` | `string` | 예 | 공개 API에서 검증한 actor |
| header | `X-Agent-Service-Token` | `string` | 예 (인증 계층 검증) | - |

- 본문 없음.

#### 5. Response body

- HTTP `204`: 응답 본문 없음.

#### 6. Error response

| HTTP 상태 | 설명 |
|---|---|
| `404` | 존재하지 않거나 삭제할 수 없는 스킬 |
| `422` | 요청 검증 실패 — `HTTPValidationError` |
| `401` | Agent 서비스 토큰 누락 또는 불일치 |
| `503` | Agent 서비스 인증 미설정 |

#### 7. Pagination / filtering

- 페이지네이션: 지원하지 않음
- 필터링: 지원하지 않음 (`workspace_id`·`user_id`는 actor scope다)

#### 8. 권한 규칙

- 개인 스킬은 소유자, team 스킬은 해당 워크스페이스 OWNER와 `skill.author` 권한을 받은 MEMBER만 삭제할 수 있다.
  기존 관리 권한으로 행을 잠근 뒤 같은 트랜잭션에서 삭제한다.
- 스킬의 모든 버전과 버전 출처는 CASCADE로 삭제한다. 과거 `agent_runs`는 유지하고
  `skill_version_id`만 NULL로 바뀐다. 실행 중 작업을 취소하는 API는 아니다.
- 삭제 후 목록·커맨드 검색·새 에이전트 스킬 선택에서 제외되며, 같은 커맨드로 새 스킬을
  만들 수 있다.

#### 9. 예시 요청/응답

```bash
curl -X DELETE "$PIPELINE/skills/skill_example?workspace_id=ws_example&user_id=user_example" \
  -H 'X-Agent-Service-Token: <value>'
```

`204 No Content`, 응답 본문 없음.

PostgreSQL 권한·삭제 검증: `SKILL_TEST_DSN`을 지정하고
`pytest tests/modules/skill/test_delete_skill_postgres.py` 실행.
임시 스키마에서 검증한 뒤 전체 롤백한다.

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/skill/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: delete_skill_skills__skill_id__delete`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/skill/repository/PipelineSkillRequester.java`:85-93 (`app.skill.endpoint`, `X-Agent-Service-Token`).
- 아웃바운드 호출: 없음(AI DB 삭제).
- 미연동 표시: 없음.

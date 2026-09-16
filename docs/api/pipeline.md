# Wiki Ingest Pipeline API

[서비스 문서](../README.md) / [ai-svc](README.md)

문서·채팅 Wiki ingest 실행과 run 상태·로그를 제공하는 내부 API다. 공개 Gateway 계약은
문서 [`ingest API`](https://github.com/FruitionKR/Fruition-document/blob/main/docs/api/ai.md)와 [`채팅 Wiki 내보내기 API`](https://github.com/FruitionKR/Fruition-document/blob/main/docs/api/chat.md)다.
현재 Backend는 POST 실행 API를 호출하지 않고 Kafka `ai.ingest.command`를 발행한다.
`GET /pipeline/runs/{run_id}`만 상태 확인용 내부 HTTP로 사용하며, 나머지는 수동·운영 인터페이스다.

- API 수: 7

## API 목차

| API | 목적 |
|---|---|
| [`POST /chat-wiki/runs`](#summary-post-chat-wiki-runs) | 채팅 내용을 Wiki에 반영하는 pipeline run을 실행합니다. |
| [`POST /pipeline/reingest-runs`](#summary-post-pipeline-reingest-runs) | 문서 재편입 pipeline run을 실행합니다. |
| [`POST /pipeline/runs`](#summary-post-pipeline-runs) | 문서 ingest pipeline run을 실행합니다. |
| [`GET /pipeline/runs/{run_id}`](#summary-get-pipeline-runs-run-id) | pipeline run의 상태와 결과를 조회합니다. |
| [`GET /pipeline/runs/{run_id}/logs`](#summary-get-pipeline-runs-run-id-logs) | pipeline run 로그를 조회합니다. |
| [`GET /documents/{document_id}`](#summary-get-documents-document-id) | pipeline 처리를 위한 문서 정보를 조회합니다. |
| [`GET /health`](#summary-get-health) | ai-svc의 상태를 확인합니다. |

## 한눈에 보기

<a id="summary-post-chat-wiki-runs"></a>
### `POST /chat-wiki/runs`

| 항목 | 내용 |
|---|---|
| 목적 | 채팅 내용을 Wiki에 반영하는 pipeline run을 실행합니다. |
| 입력 | **Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `ChatWikiRunIn` |
| 출력 | `200` 성공 — `PipelineRunOut` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-chat-wiki-runs"></a>
### `POST /chat-wiki/runs` 상세

#### 1. Method + Path

`POST /chat-wiki/runs`

#### 2. 목적

채팅 내용을 Wiki에 반영하는 pipeline run을 실행합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`ChatWikiRunIn`)

운영 Gateway는 이 HTTP API를 호출하지 않는다. Backend가 검증한 채팅을 문서로 저장하고
Kafka command를 발행하면 ingest worker가 같은 실행 로직을 호출한다. 아래는 수동 내부 호출에
필요한 최소 body이며 prompt 경로와 evaluator 설정은 ai-svc 기본값을 사용한다.

```json
{
  "document_id": "chatdoc_123",
  "provider": "openai",
  "model": "gpt-5-nano",
  "selection_mode": "partial",
  "input_markdown": "# Chat Export\n\nQ : 검색 인덱싱은 어떻게 동작하나요?\nA : 역색인을 사용합니다.",
  "input_blocks": [
    {
      "block_id": "session_1b9f4c7e2a8d4f1e:pair_0a97d25e4f83",
      "text": "Q : 검색 인덱싱은 어떻게 동작하나요?\nA : 역색인을 사용합니다."
    }
  ]
}
```

`input_markdown`과 `input_blocks`는 둘 다 필수다. 문답 경계와 `session_id:pair_id` provenance는 backend가 확정해
`input_blocks`로 넘기고, pipeline은 Markdown을 다시 쪼개지 않는다. `block_id`가 그대로 `source_blocks.block_id`가
되므로 Markdown 본문에는 이 id가 들어가지 않는다. 채팅 Wiki export는 항상 독립 source page를 생성하며
`selection_mode`는 기존 저장·command 계약에 맞춰 `partial`만 허용한다.

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`PipelineRunOut`)

```json
{
  "log_path": "string",
  "manifest": {
  },
  "output_dir": "string",
  "run_id": "string",
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
- 필터링: 지원하지 않음

#### 8. 권한 규칙

- 올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.
- 요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다.

#### 9. 예시 요청/응답

```bash
curl -X POST "$PIPELINE/chat-wiki/runs" \
  -H 'X-Internal-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"document_id":"chatdoc_123","provider":"openai","model":"gpt-5-nano","selection_mode":"partial","input_markdown":"# Chat Export\n\nQ : 질문\nA : 답변","input_blocks":[{"block_id":"session_1:pair_1","text":"Q : 질문\nA : 답변"}]}'
```

```json
{
  "log_path": "string",
  "manifest": {
  },
  "output_dir": "string",
  "run_id": "string",
  "status": "string"
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/wiki_ingestion/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: run_chat_wiki_endpoint_chat_wiki_runs_post`)

[↑ 요약으로 돌아가기](#summary-post-chat-wiki-runs)

</details>

<a id="summary-post-pipeline-reingest-runs"></a>
### `POST /pipeline/reingest-runs`

| 항목 | 내용 |
|---|---|
| 목적 | 문서 재편입 pipeline run을 실행합니다. |
| 입력 | **Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `ReingestRunIn` |
| 출력 | `200` 성공 — `PipelineRunOut` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-pipeline-reingest-runs"></a>
### `POST /pipeline/reingest-runs` 상세

#### 1. Method + Path

`POST /pipeline/reingest-runs`

#### 2. 목적

문서 재편입 pipeline run을 실행합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`ReingestRunIn`)

현재 Gateway·Backend에는 이 HTTP endpoint의 호출자가 없다. 수동 재편입 시 필요한 최소 body다.

```json
{
  "document_id": "doc_123",
  "provider": "openai",
  "model": "gpt-5-nano",
  "input_markdown": "# 갱신된 문서"
}
```

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`PipelineRunOut`)

```json
{
  "log_path": "string",
  "manifest": {
  },
  "output_dir": "string",
  "run_id": "string",
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
- 필터링: 지원하지 않음

#### 8. 권한 규칙

- 올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.
- 요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다.

#### 9. 예시 요청/응답

```bash
curl -X POST "$PIPELINE/pipeline/reingest-runs" \
  -H 'X-Internal-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"document_id":"doc_123","provider":"openai","model":"gpt-5-nano","input_markdown":"# 갱신된 문서"}'
```

```json
{
  "log_path": "string",
  "manifest": {
  },
  "output_dir": "string",
  "run_id": "string",
  "status": "string"
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/wiki_ingestion/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: run_reingest_pipeline_endpoint_pipeline_reingest_runs_post`)

[↑ 요약으로 돌아가기](#summary-post-pipeline-reingest-runs)

</details>

<a id="summary-post-pipeline-runs"></a>
### `POST /pipeline/runs`

| 항목 | 내용 |
|---|---|
| 목적 | 문서 ingest pipeline run을 실행합니다. |
| 입력 | **Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `PipelineRunIn` |
| 출력 | `200` 성공 — `PipelineRunOut` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-pipeline-runs"></a>
### `POST /pipeline/runs` 상세

#### 1. Method + Path

`POST /pipeline/runs`

#### 2. 목적

문서 ingest pipeline run을 실행합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`PipelineRunIn`)

운영 Gateway는 Kafka를 사용한다. 아래는 같은 ingest를 수동 내부 HTTP로 실행할 때 필요한 최소 body다.

```json
{
  "document_id": "doc_123",
  "provider": "openai",
  "model": "gpt-5-nano"
}
```

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`PipelineRunOut`)

```json
{
  "log_path": "string",
  "manifest": {
  },
  "output_dir": "string",
  "run_id": "string",
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
- 필터링: 지원하지 않음

#### 8. 권한 규칙

- 올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.
- 요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다.

#### 9. 예시 요청/응답

```bash
curl -X POST "$PIPELINE/pipeline/runs" \
  -H 'X-Internal-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"document_id":"doc_123","provider":"openai","model":"gpt-5-nano"}'
```

```json
{
  "log_path": "string",
  "manifest": {
  },
  "output_dir": "string",
  "run_id": "string",
  "status": "string"
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/wiki_ingestion/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: run_pipeline_endpoint_pipeline_runs_post`)

[↑ 요약으로 돌아가기](#summary-post-pipeline-runs)

</details>

<a id="summary-get-pipeline-runs-run-id"></a>
### `GET /pipeline/runs/{run_id}`

| 항목 | 내용 |
|---|---|
| 목적 | pipeline run의 상태와 결과를 조회합니다. |
| 입력 | **Path** — `run_id`: `string`<br>**Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null` |
| 출력 | `200` 성공 — `object` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-get-pipeline-runs-run-id"></a>
### `GET /pipeline/runs/{run_id}` 상세

#### 1. Method + Path

`GET /pipeline/runs/{run_id}`

#### 2. 목적

pipeline run의 상태와 결과를 조회합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| path | `run_id` | `string` | 예 | - |
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Body: 없음

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`Response Get Pipeline Run Pipeline Runs  Run Id  Get`)

```json
{
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
curl -X GET "$PIPELINE/pipeline/runs/<value>" \
  -H 'X-Internal-Token: <value>'
```

```json
{
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/wiki_ingestion/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: get_pipeline_run_pipeline_runs__run_id__get`)

[↑ 요약으로 돌아가기](#summary-get-pipeline-runs-run-id)

</details>

<a id="summary-get-pipeline-runs-run-id-logs"></a>
### `GET /pipeline/runs/{run_id}/logs`

| 항목 | 내용 |
|---|---|
| 목적 | pipeline run 로그를 조회합니다. |
| 입력 | **Path** — `run_id`: `string`<br>**Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null` |
| 출력 | `200` 성공 — `string` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-get-pipeline-runs-run-id-logs"></a>
### `GET /pipeline/runs/{run_id}/logs` 상세

#### 1. Method + Path

`GET /pipeline/runs/{run_id}/logs`

#### 2. 목적

pipeline run 로그를 조회합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| path | `run_id` | `string` | 예 | - |
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Body: 없음

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `text/plain`

```text
string
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
curl -X GET "$PIPELINE/pipeline/runs/<value>/logs" \
  -H 'X-Internal-Token: <value>'
```

```text
string
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/wiki_ingestion/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: get_pipeline_logs_pipeline_runs__run_id__logs_get`)

[↑ 요약으로 돌아가기](#summary-get-pipeline-runs-run-id-logs)

</details>

<a id="summary-get-documents-document-id"></a>
### `GET /documents/{document_id}`

| 항목 | 내용 |
|---|---|
| 목적 | pipeline 처리를 위한 문서 정보를 조회합니다. |
| 입력 | **Path** — `document_id`: `string`<br>**Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null` |
| 출력 | `200` 성공 — `object` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-get-documents-document-id"></a>
### `GET /documents/{document_id}` 상세

#### 1. Method + Path

`GET /documents/{document_id}`

#### 2. 목적

pipeline 처리를 위한 문서 정보를 조회합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| path | `document_id` | `string` | 예 | - |
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Body: 없음

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`Response Get Document Documents  Document Id  Get`)

```json
{
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
curl -X GET "$PIPELINE/documents/<value>" \
  -H 'X-Internal-Token: <value>'
```

```json
{
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/wiki_ingestion/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: get_document_documents__document_id__get`)

[↑ 요약으로 돌아가기](#summary-get-documents-document-id)

</details>

<a id="summary-get-health"></a>
### `GET /health`

| 항목 | 내용 |
|---|---|
| 목적 | ai-svc의 상태를 확인합니다. |
| 입력 | 없음 |
| 출력 | `200` 성공 — `object` |
| 조건 | 인증 불필요<br>인증 없이 호출할 수 있다.<br>공개 API이므로 별도의 사용자 권한 검증이 없다. |
| 주요 오류 | 공통 오류 계약 적용 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-get-health"></a>
### `GET /health` 상세

#### 1. Method + Path

`GET /health`

#### 2. 목적

ai-svc의 상태를 확인합니다.

#### 3. Auth 필요 여부

- 불필요
- 인증 없이 호출할 수 있다.

#### 4. Request body

- 없음

- Body: 없음

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`Response Health Health Get`)

```json
{
}
```

#### 6. Error response

- 명세에 별도 오류 응답이 정의되어 있지 않다.

#### 7. Pagination / filtering

- 페이지네이션: 지원하지 않음
- 필터링: 지원하지 않음

#### 8. 권한 규칙

- 공개 API이므로 별도의 사용자 권한 검증이 없다.

#### 9. 예시 요청/응답

```bash
curl -X GET "$PIPELINE/health"
```

```json
{
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/wiki_ingestion/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: health_health_get`)

[↑ 요약으로 돌아가기](#summary-get-health)

</details>

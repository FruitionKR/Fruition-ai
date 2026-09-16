# Wiki API

[서비스 문서](../README.md) / [ai-svc](README.md)

Wiki 조회·페이지 관리·lint·복구 내부 API다. 공개 Gateway 계약은
[`document-svc Wiki API`](https://github.com/FruitionKR/Fruition-document/blob/main/docs/api/wiki.md)다. 조회·페이지 관리는 내부 HTTP로 중계하고,
lint·복구는 Kafka `ai.maintenance.command` worker가 실행한다. ingest 실행과 run 조회는
[`Wiki Ingest Pipeline API`](pipeline.md)에서 다룬다.

- API 수: 10

## API 목차

| API | 목적 |
|---|---|
| [`GET /wiki/documents/{document_id}/context`](#summary-get-wiki-documents-document-id-context) | 문서와 연결된 Wiki 문맥을 조회합니다. |
| [`GET /wiki/graph`](#summary-get-wiki-graph) | 워크스페이스 Wiki 그래프를 조회합니다. |
| [`POST /wiki/ingest-restore-runs`](#summary-post-wiki-ingest-restore-runs) | ingest 작업의 Wiki 변경을 복원합니다. |
| [`POST /wiki/lint-restore-runs`](#summary-post-wiki-lint-restore-runs) | lint 작업의 Wiki 변경을 복원합니다. |
| [`POST /wiki/maintenance/lint`](#summary-post-wiki-maintenance-lint) | 워크스페이스 Wiki의 정합성을 검사합니다. |
| [`POST /wiki/pages/lookup`](#summary-post-wiki-pages-lookup) | 조건에 맞는 Wiki 페이지를 조회합니다. |
| [`GET /wiki/pages/{page_id}`](#summary-get-wiki-pages-page-id) | Wiki 페이지 상세 정보를 조회합니다. |
| [`PATCH /wiki/pages/{page_id}/rename`](#summary-patch-wiki-pages-page-id-rename) | Wiki 페이지 이름을 변경합니다. |
| [`DELETE /wiki/workspaces/{workspace_id}/documents/{document_id}`](#summary-delete-wiki-workspaces-workspace-id-documents-document-id) | 문서에서 파생된 Wiki 데이터를 삭제합니다. |
| [`GET /wiki/workspaces/{workspace_id}/last-updated`](#summary-get-wiki-workspaces-workspace-id-last-updated) | 워크스페이스 Wiki의 마지막 갱신 시각을 조회합니다. |

## 한눈에 보기

<a id="summary-get-wiki-documents-document-id-context"></a>
### `GET /wiki/documents/{document_id}/context`

| 항목 | 내용 |
|---|---|
| 목적 | 문서와 연결된 Wiki 문맥을 조회합니다. |
| 입력 | **Path** — `document_id`: `string`<br>**Query** — `workspace_id`: `string`<br>**Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null` |
| 출력 | `200` 성공 — `object` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>필터링: `workspace_id`<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-get-wiki-documents-document-id-context"></a>
### `GET /wiki/documents/{document_id}/context` 상세

#### 1. Method + Path

`GET /wiki/documents/{document_id}/context`

#### 2. 목적

문서와 연결된 Wiki 문맥을 조회합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| path | `document_id` | `string` | 예 | - |
| query | `workspace_id` | `string` | 예 | - |
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Body: 없음

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`Response Get Document Wiki Context Wiki Documents  Document Id  Context Get`)

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
- 필터링: `workspace_id`

#### 8. 권한 규칙

- 올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.
- 요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다.

#### 9. 예시 요청/응답

```bash
curl -X GET "$PIPELINE/wiki/documents/<value>/context?workspace_id=<value>" \
  -H 'X-Internal-Token: <value>'
```

```json
{
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/wiki_ingestion/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: get_document_wiki_context_wiki_documents__document_id__context_get`)

[↑ 요약으로 돌아가기](#summary-get-wiki-documents-document-id-context)

</details>

<a id="summary-get-wiki-graph"></a>
### `GET /wiki/graph`

| 항목 | 내용 |
|---|---|
| 목적 | 워크스페이스 Wiki 그래프를 조회합니다. |
| 입력 | **Query** — `workspace_id`: `string`<br>**Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null` |
| 출력 | `200` 성공 — `object` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>필터링: `workspace_id`<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-get-wiki-graph"></a>
### `GET /wiki/graph` 상세

#### 1. Method + Path

`GET /wiki/graph`

#### 2. 목적

워크스페이스 Wiki 그래프를 조회합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| query | `workspace_id` | `string` | 예 | - |
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Body: 없음

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`Response Get Wiki Graph Wiki Graph Get`)

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
- 필터링: `workspace_id`

#### 8. 권한 규칙

- 올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.
- 요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다.

#### 9. 예시 요청/응답

```bash
curl -X GET "$PIPELINE/wiki/graph?workspace_id=<value>" \
  -H 'X-Internal-Token: <value>'
```

```json
{
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/wiki_ingestion/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: get_wiki_graph_wiki_graph_get`)

[↑ 요약으로 돌아가기](#summary-get-wiki-graph)

</details>

<a id="summary-post-wiki-ingest-restore-runs"></a>
### `POST /wiki/ingest-restore-runs`

| 항목 | 내용 |
|---|---|
| 목적 | ingest 작업의 Wiki 변경을 복원합니다. |
| 입력 | **Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `IngestOperationRestoreIn` |
| 출력 | `200` 성공 — `object` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-wiki-ingest-restore-runs"></a>
### `POST /wiki/ingest-restore-runs` 상세

#### 1. Method + Path

`POST /wiki/ingest-restore-runs`

#### 2. 목적

ingest 작업의 Wiki 변경을 복원합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`IngestOperationRestoreIn`)

```json
{
  "cancel_operation_ids": [
    "string"
  ],
  "deleted_pages": [
    "string"
  ],
  "operation_id": "string",
  "rebuild_pages": [
    {
      "keep_contributions": [
        {
          "document_id": "string",
          "operation_id": "string"
        }
      ],
      "page_id": "string"
    }
  ],
  "restore_to_operation_id": "string",
  "source_page": {
    "document_id": "string",
    "page_id": "string"
  },
  "workspace_id": "string"
}
```

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`Response Restore Ingest Operation Wiki Ingest Restore Runs Post`)

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
curl -X POST "$PIPELINE/wiki/ingest-restore-runs" \
  -H 'X-Internal-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"cancel_operation_ids":["op_cancelled"],"deleted_pages":["page_deleted"],"operation_id":"op_restore","rebuild_pages":[{"keep_contributions":[{"document_id":"doc_kept","operation_id":"op_kept"}],"page_id":"page_rebuild"}],"restore_to_operation_id":"op_target","source_page":{"document_id":"doc_source","page_id":"page_source"},"workspace_id":"workspace_01"}'
```

```json
{
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/wiki_ingestion/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: restore_ingest_operation_wiki_ingest_restore_runs_post`)

[↑ 요약으로 돌아가기](#summary-post-wiki-ingest-restore-runs)

</details>

<a id="summary-post-wiki-lint-restore-runs"></a>
### `POST /wiki/lint-restore-runs`

| 항목 | 내용 |
|---|---|
| 목적 | lint 작업의 Wiki 변경을 복원합니다. |
| 입력 | **Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `LintOperationRestoreIn` |
| 출력 | `200` 성공 — `object` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-wiki-lint-restore-runs"></a>
### `POST /wiki/lint-restore-runs` 상세

#### 1. Method + Path

`POST /wiki/lint-restore-runs`

#### 2. 목적

lint 작업의 Wiki 변경을 복원합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`LintOperationRestoreIn`)

```json
{
  "deleted_pages": [
    "string"
  ],
  "operation_id": "string",
  "rebuild_pages": [
    {
      "keep_contributions": [
        {
          "document_id": "string",
          "operation_id": "string"
        }
      ],
      "page_id": "string"
    }
  ],
  "target_operation_id": "string",
  "workspace_id": "string"
}
```

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`Response Restore Lint Operation Wiki Lint Restore Runs Post`)

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
curl -X POST "$PIPELINE/wiki/lint-restore-runs" \
  -H 'X-Internal-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"deleted_pages":["page_deleted"],"operation_id":"op_lint_restore","rebuild_pages":[{"keep_contributions":[{"document_id":"doc_kept","operation_id":"op_kept"}],"page_id":"page_rebuild"}],"target_operation_id":"op_lint_target","workspace_id":"workspace_01"}'
```

```json
{
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/wiki_ingestion/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: restore_lint_operation_wiki_lint_restore_runs_post`)

[↑ 요약으로 돌아가기](#summary-post-wiki-lint-restore-runs)

</details>

<a id="summary-post-wiki-maintenance-lint"></a>
### `POST /wiki/maintenance/lint`

| 항목 | 내용 |
|---|---|
| 목적 | 워크스페이스 Wiki의 정합성을 검사합니다. |
| 입력 | **Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `WikiLintIn` |
| 출력 | `200` 성공 — `WikiLintOut` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-wiki-maintenance-lint"></a>
### `POST /wiki/maintenance/lint` 상세

#### 1. Method + Path

`POST /wiki/maintenance/lint`

#### 2. 목적

워크스페이스 Wiki의 정합성을 검사합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`WikiLintIn`)

```json
{
  "dry_run": true,
  "materialize_promotions": true,
  "model": "string",
  "operation_id": "string",
  "provider": "openai",
  "user_id": "local-user",
  "workspace_id": "local-workspace"
}
```

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`WikiLintOut`)

```json
{
  "active_path": "string",
  "applied_cluster_reconciliation": {
  },
  "applied_reconciliations": [],
  "changed_pages": [],
  "cluster_count": 1,
  "invalid_promotions": [],
  "invalid_relations": [],
  "materialized_promotions": [],
  "materialized_relations": [],
  "merged_promotions": [],
  "needs_review": [],
  "orphan_refs": [],
  "promotion_candidates": [],
  "reconciliation_candidates": [],
  "relation_candidates": [],
  "source_ref_count": 0,
  "user_id": "local-user",
  "workspace_id": "local-workspace"
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
curl -X POST "$PIPELINE/wiki/maintenance/lint" \
  -H 'X-Internal-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"dry_run":true,"materialize_promotions":true,"model":"gpt-5-nano","operation_id":"<value>","provider":"openai","user_id":"local-user","workspace_id":"local-workspace"}'
```

```json
{
  "active_path": "string",
  "applied_cluster_reconciliation": {
  },
  "applied_reconciliations": [],
  "changed_pages": [],
  "cluster_count": 1,
  "invalid_promotions": [],
  "invalid_relations": [],
  "materialized_promotions": [],
  "materialized_relations": [],
  "merged_promotions": [],
  "needs_review": [],
  "orphan_refs": [],
  "promotion_candidates": [],
  "reconciliation_candidates": [],
  "relation_candidates": [],
  "source_ref_count": 0,
  "user_id": "local-user",
  "workspace_id": "local-workspace"
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/wiki_ingestion/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: lint_wiki_workspace_wiki_maintenance_lint_post`)

[↑ 요약으로 돌아가기](#summary-post-wiki-maintenance-lint)

</details>

<a id="summary-post-wiki-pages-lookup"></a>
### `POST /wiki/pages/lookup`

| 항목 | 내용 |
|---|---|
| 목적 | 조건에 맞는 Wiki 페이지를 조회합니다. |
| 입력 | **Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `WikiPageLookupIn` |
| 출력 | `200` 성공 — 배열<`object`> |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-wiki-pages-lookup"></a>
### `POST /wiki/pages/lookup` 상세

#### 1. Method + Path

`POST /wiki/pages/lookup`

#### 2. 목적

조건에 맞는 Wiki 페이지를 조회합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`WikiPageLookupIn`)

```json
{
  "page_ids": [
    "string"
  ],
  "workspace_id": "string"
}
```

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`Response Lookup Wiki Pages Wiki Pages Lookup Post`)

```json
[
  {
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
curl -X POST "$PIPELINE/wiki/pages/lookup" \
  -H 'X-Internal-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"page_ids":["<value>"],"workspace_id":"<value>"}'
```

```json
[
  {
  }
]
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/wiki_ingestion/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: lookup_wiki_pages_wiki_pages_lookup_post`)

[↑ 요약으로 돌아가기](#summary-post-wiki-pages-lookup)

</details>

<a id="summary-get-wiki-pages-page-id"></a>
### `GET /wiki/pages/{page_id}`

| 항목 | 내용 |
|---|---|
| 목적 | Wiki 페이지 상세 정보를 조회합니다. |
| 입력 | **Path** — `page_id`: `string`<br>**Query** — `workspace_id`: `string`<br>**Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null` |
| 출력 | `200` 성공 — `object` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>필터링: `workspace_id`<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-get-wiki-pages-page-id"></a>
### `GET /wiki/pages/{page_id}` 상세

#### 1. Method + Path

`GET /wiki/pages/{page_id}`

#### 2. 목적

Wiki 페이지 상세 정보를 조회합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| path | `page_id` | `string` | 예 | - |
| query | `workspace_id` | `string` | 예 | - |
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Body: 없음

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`Response Get Wiki Page Wiki Pages  Page Id  Get`)

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
- 필터링: `workspace_id`

#### 8. 권한 규칙

- 올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.
- 요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다.

#### 9. 예시 요청/응답

```bash
curl -X GET "$PIPELINE/wiki/pages/<value>?workspace_id=<value>" \
  -H 'X-Internal-Token: <value>'
```

```json
{
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/wiki_ingestion/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: get_wiki_page_wiki_pages__page_id__get`)

[↑ 요약으로 돌아가기](#summary-get-wiki-pages-page-id)

</details>

<a id="summary-patch-wiki-pages-page-id-rename"></a>
### `PATCH /wiki/pages/{page_id}/rename`

| 항목 | 내용 |
|---|---|
| 목적 | Wiki 페이지 이름을 변경합니다. |
| 입력 | **Path** — `page_id`: `string`<br>**Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `WikiPageRenameIn` |
| 출력 | `200` 성공 — `object` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-patch-wiki-pages-page-id-rename"></a>
### `PATCH /wiki/pages/{page_id}/rename` 상세

#### 1. Method + Path

`PATCH /wiki/pages/{page_id}/rename`

#### 2. 목적

Wiki 페이지 이름을 변경합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| path | `page_id` | `string` | 예 | - |
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`WikiPageRenameIn`)

```json
{
  "title": "string",
  "update_slug": true,
  "user_id": "string",
  "workspace_id": "string"
}
```

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`Response Rename Wiki Page Wiki Pages  Page Id  Rename Patch`)

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
curl -X PATCH "$PIPELINE/wiki/pages/<value>/rename" \
  -H 'X-Internal-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"title":"<value>","update_slug":true,"user_id":"<value>","workspace_id":"<value>"}'
```

```json
{
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/wiki_ingestion/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: rename_wiki_page_wiki_pages__page_id__rename_patch`)

[↑ 요약으로 돌아가기](#summary-patch-wiki-pages-page-id-rename)

</details>

<a id="summary-delete-wiki-workspaces-workspace-id-documents-document-id"></a>
### `DELETE /wiki/workspaces/{workspace_id}/documents/{document_id}`

| 항목 | 내용 |
|---|---|
| 목적 | 문서에서 파생된 Wiki 데이터를 삭제합니다. |
| 입력 | **Path** — `workspace_id`: `string`, `document_id`: `string`<br>**Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null` |
| 출력 | `200` 성공 — `object` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-delete-wiki-workspaces-workspace-id-documents-document-id"></a>
### `DELETE /wiki/workspaces/{workspace_id}/documents/{document_id}` 상세

#### 1. Method + Path

`DELETE /wiki/workspaces/{workspace_id}/documents/{document_id}`

#### 2. 목적

문서에서 파생된 Wiki 데이터를 삭제합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| path | `workspace_id` | `string` | 예 | - |
| path | `document_id` | `string` | 예 | - |
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Body: 없음

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json`

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
curl -X DELETE "$PIPELINE/wiki/workspaces/<value>/documents/<value>" \
  -H 'X-Internal-Token: <value>'
```

```json
{
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/wiki_ingestion/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: delete_document_wiki_data_wiki_workspaces__workspace_id__documents__document_id__delete`)

[↑ 요약으로 돌아가기](#summary-delete-wiki-workspaces-workspace-id-documents-document-id)

</details>

<a id="summary-get-wiki-workspaces-workspace-id-last-updated"></a>
### `GET /wiki/workspaces/{workspace_id}/last-updated`

| 항목 | 내용 |
|---|---|
| 목적 | 워크스페이스 Wiki의 마지막 갱신 시각을 조회합니다. |
| 입력 | **Path** — `workspace_id`: `string`<br>**Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null` |
| 출력 | `200` 성공 — `object` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-get-wiki-workspaces-workspace-id-last-updated"></a>
### `GET /wiki/workspaces/{workspace_id}/last-updated` 상세

#### 1. Method + Path

`GET /wiki/workspaces/{workspace_id}/last-updated`

#### 2. 목적

워크스페이스 Wiki의 마지막 갱신 시각을 조회합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| path | `workspace_id` | `string` | 예 | - |
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Body: 없음

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`Response Get Last Wiki Updated Wiki Workspaces  Workspace Id  Last Updated Get`)

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
curl -X GET "$PIPELINE/wiki/workspaces/<value>/last-updated" \
  -H 'X-Internal-Token: <value>'
```

```json
{
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/wiki_ingestion/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: get_last_wiki_updated_wiki_workspaces__workspace_id__last_updated_get`)

[↑ 요약으로 돌아가기](#summary-get-wiki-workspaces-workspace-id-last-updated)

</details>

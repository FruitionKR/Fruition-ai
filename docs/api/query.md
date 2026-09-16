# Query API

[서비스 문서](../README.md) / [ai-svc](README.md)

Wiki 기반 질의 실행 내부 API다. 공개 Gateway 계약은
[`document-svc Query API`](https://github.com/FruitionKR/Fruition-document/blob/main/docs/api/query.md)다. 공개 동기·비동기 Query는 Kafka `ai.query.command` worker가 같은 application 로직을 실행한다.
이 HTTP API는 운영용 단발 호출이며 공통 작업 취소에 등록되지 않는다.

- API 수: 1

## API 목차

| API | 목적 |
|---|---|
| [`POST /query`](#summary-post-query) | Wiki와 선택적 웹 검색을 바탕으로 질의에 답합니다. |

## 한눈에 보기

<a id="summary-post-query"></a>
### `POST /query`

| 항목 | 내용 |
|---|---|
| 목적 | Wiki와 선택적 웹 검색을 바탕으로 질의에 답합니다. |
| 입력 | **Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `QueryRequest` |
| 출력 | `200` 성공 — `QueryResponse` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-query"></a>
### `POST /query` 상세

#### 1. Method + Path

`POST /query`

#### 2. 목적

Wiki와 선택적 웹 검색을 바탕으로 질의에 답합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`QueryRequest`)

```json
{
  "workspace_id": "workspace_123",
  "question": "Wiki ingest는 어떤 단계로 동작해?",
  "provider": "openai",
  "model": "gpt-5-nano",
  "allow_web_search": false,
  "recent_messages": [
    {
      "role": "user",
      "content": "Wiki 구조를 설명해줘"
    }
  ]
}
```

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`QueryResponse`)

```json
{
  "answer": "string",
  "error_code": "string",
  "evidence_snippets": [
    {
      "rank": 1,
      "source_block_ids": [
        "string"
      ],
      "source_document_id": "string",
      "source_refs": [
        {
          "source_block_id": "string",
          "source_document_id": "string"
        }
      ],
      "text": "string"
    }
  ],
  "graph_context": {
    "edges": [
      {
        "from_page_id": "string",
        "link_type": "string",
        "role": "string",
        "score": 0.0,
        "to_page_id": "string"
      }
    ],
    "nodes": [
      {
        "depth": 0,
        "id": "string",
        "page_type": "string",
        "relevance_score": 0.0,
        "role": "string",
        "slug": "string",
        "title": "string"
      }
    ]
  },
  "related_pages": [
    {
      "depth": 1,
      "id": "string",
      "page_type": "string",
      "relevance_score": 1,
      "role": "string",
      "slug": "string",
      "title": "string"
    }
  ],
  "result_count": 1,
  "traversal_paths": [
    {
      "edges": [
        {
          "from_page_id": "string",
          "link_type": "string",
          "role": "string",
          "score": 0.0,
          "to_page_id": "string"
        }
      ],
      "nodes": [
        "string"
      ],
      "path_id": "string",
      "role": "string",
      "score": 1,
      "stop_reason": "string",
      "used_for_answer": true
    }
  ],
  "updated_conversation_summary": "string",
  "web_search_executed": true,
  "web_search_requested": true
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
curl -X POST "$PIPELINE/query" \
  -H 'X-Internal-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"workspace_id":"workspace_123","question":"Wiki ingest는 어떤 단계로 동작해?","provider":"openai","model":"gpt-5-nano","allow_web_search":false,"recent_messages":[{"role":"user","content":"Wiki 구조를 설명해줘"}]}'
```

```json
{
  "answer": "string",
  "error_code": "string",
  "evidence_snippets": [
    {
      "rank": 1,
      "source_block_ids": [
        "string"
      ],
      "source_document_id": "string",
      "source_refs": [
        {
          "source_block_id": "string",
          "source_document_id": "string"
        }
      ],
      "text": "string"
    }
  ],
  "graph_context": {
    "edges": [
      {
        "from_page_id": "string",
        "link_type": "string",
        "role": "string",
        "score": 0.0,
        "to_page_id": "string"
      }
    ],
    "nodes": [
      {
        "depth": 0,
        "id": "string",
        "page_type": "string",
        "relevance_score": 0.0,
        "role": "string",
        "slug": "string",
        "title": "string"
      }
    ]
  },
  "related_pages": [
    {
      "depth": 1,
      "id": "string",
      "page_type": "string",
      "relevance_score": 1,
      "role": "string",
      "slug": "string",
      "title": "string"
    }
  ],
  "result_count": 1,
  "traversal_paths": [
    {
      "edges": [
        {
          "from_page_id": "string",
          "link_type": "string",
          "role": "string",
          "score": 0.0,
          "to_page_id": "string"
        }
      ],
      "nodes": [
        "string"
      ],
      "path_id": "string",
      "role": "string",
      "score": 1,
      "stop_reason": "string",
      "used_for_answer": true
    }
  ],
  "updated_conversation_summary": "string",
  "web_search_executed": true,
  "web_search_requested": true
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/query/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: answer_query_query_post`)

[↑ 요약으로 돌아가기](#summary-post-query)

</details>

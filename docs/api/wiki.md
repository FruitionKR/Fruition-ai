# Wiki API

[서비스 문서](../README.md) / [ai-svc](README.md)

Wiki 조회·페이지 관리·lint·복구 내부 API다. 공개 Gateway 계약은
[`document-svc Wiki API`](https://github.com/FruitionKR/Fruition-document/blob/main/docs/api/wiki.md)다. 조회·페이지 관리는 내부 HTTP로 중계하고,
lint·복구는 Kafka `ai.maintenance.command` worker가 실행한다. ingest 실행과 run 조회는
[`Wiki Ingest Pipeline API`](pipeline.md)에서 다룬다.

위키는 워크스페이스 공유 자산이다([ADR 0033](../adr/0033-workspace-shared-wiki.md)). 객체 저장소 키는 모두
`wiki/{workspace_id}/` 아래에 있다: 페이지 본문 `sources/{document_id}.md`·`concepts/{slug}.md`, 의미 클러스터
`clusters/active.md`·`clusters/archived.md`, 편입·lint 로그 `logs/{날짜}.md`, 페이지 이력 `pages/{page_id}/ops/`.

편입은 개념으로 바로 확정하지 않은 후보의 근거를 `clusters/active.md`의 의미 클러스터에 쌓는다. 클러스터 배정 판정에는
`active.md`가 24,000자를 넘으면 이번 후보와 가까운 클러스터(임베딩 0.75 + BM25 0.25, 임베딩 서버가 없으면 BM25만)부터
24,000자까지 섹션 단위로 보낸다. 승격 후보
(`Promotion.status: candidate`)는 근거를 덧붙인 뒤 두 경로로만 정한다. 이번 문서의 핵심 개념과 이름·별칭이 같은 클러스터,
이름이 달라도 Jev 쌍 판정이 같은 개념으로 고른 클러스터는 그 개념(`target: concept:{slug}`)으로, 근거가 덧붙은 클러스터는
클러스터마다 쌓인 근거를 본 LLM 판정으로 정한다. LLM 판정과 lint 승격은 세부 항목(근거 줄 용어를 이름 정규화로 묶은 것, 클러스터 자신의 이름 제외)이 `P`개(초기값 5) 이상인
클러스터만 받고, 모자란 후보는 `invalid_promotions`에 남긴다. 핵심 개념과 맞춘 후보는 개수를 보지 않는다([ADR 0042](../adr/0042-hierarchical-concept-pages.md)).
lint는 승격 후보를 `target`·같은 slug(클러스터 id나 생성한 페이지 slug)의 개념에 병합하거나, 생성한 개념 페이지를
기존 개념과 Jev로 쌍 판정해 같은 개념이면 그 개념에 병합(클러스터 대표명·id를 별칭으로 덧붙임)하고, 아니면 새 페이지를 만든다. 관련 개념 링크는 잇지 않는다.
세부 항목은 승격 페이지(병합 대상 개념)의 하위 항목으로 lint 기록 `entry_changes`에 남아 lint 복원 시 함께 빠지고, 그 근거 문서는 페이지의 문서 연결이 된다. 클러스터의 관계 후보(`### Core Relation Candidates`)와 그 링크 생성, 결과의 `relation_candidates`·`invalid_relations`·`materialized_relations`는 지웠다(ADR 0042). Jev를 쓸 수 없으면 이름·slug 일치만 본다. 승격한 클러스터는
`archived.md`로 옮기고, lint 복원은 이를 `active.md`로 되돌린다([ADR 0037](../adr/0037-meaning-cluster-promotion.md), [ADR 0038](../adr/0038-concept-to-concept-links.md)).

- API 수: 11

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
| [`PUT /wiki/pages/{page_id}/manual-edit`](#summary-put-wiki-pages-page-id-manual-edit) | 사람이 고친 Wiki 본문을 수동 기여로 저장합니다. |
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
  "pages": [
    { "id": "page_…", "page_type": "source", "title": "레드블랙트리", "slug": "doc-…", "relation_type": "source_of", "confidence": 1.0 }
  ],
  "source_content_hash": "9f2c…",
  "source_blocks": [
    { "block_id": "B0005", "text": "# 서문", "position": 1, "line_start": 1, "line_end": 1, "block_type": "heading" },
    { "block_id": "B0001", "text": "# 레드블랙트리", "position": 2, "line_start": 3, "line_end": 3, "block_type": "heading" }
  ]
}
```

- `source_blocks`는 문서 순서(`position`)로 정렬된다. 재편입은 변경 없는 블록의 ID를 유지하고 새 블록에 `최대번호+1`을 주므로 `block_id` 순서는 문서 순서가 아니다.
- `line_start`·`line_end`: 편입 입력 Markdown을 `\n`으로 나눈 1-based 양끝 포함 줄 범위. 채팅 문서(문답 단위 블록)와 위치 저장 이전 블록은 `null`이며, 이때 `position`이 없는 블록은 뒤로 정렬된다.
- `block_type`: `heading`·`paragraph`·`code`·`list` 중 하나. 채팅 문서와 기존 블록은 `null`.
- `source_content_hash`: 현재 블록 집합을 만든 편입 입력 Markdown의 SHA-256 hex. 문서의 현재 `content_hash`와 다르면 블록 줄 범위가 현재 본문과 어긋났을 수 있으므로 텍스트 대조로 대체한다. 블록이 없거나 기록 이전 편입이면 `null`.

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/wiki_ingestion/interfaces/http/routes.py`
- 조회: `pipeline/app/modules/wiki_ingestion/infrastructure/postgres_wiki_ingestion_repository.py` (`get_document_wiki_context`)
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: get_document_wiki_context_wiki_documents__document_id__context_get`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/wiki/repository/PipelineWikiStateRequester.java`:63 (`app.wiki-state.endpoint`).
- 아웃바운드 호출: 없음.
- 미연동 표시: 없음.

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

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/wiki/repository/PipelineWikiStateRequester.java`:35.
- 아웃바운드 호출: 없음.
- 미연동 표시: 없음.

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

- 소스 → 개념 자동 링크(`source_mentions_concept`)는 복원 결과의 문서 연결(`source_document_ids`, 재생 결과 남은 유효 근거의 문서)에서
  다시 만든다(ADR 0042 결정 5). source 페이지를 스냅샷으로 되돌려도 이 링크는 지우지 않고, 그 스냅샷까지의 수동 기여로 수동
  `[[slug]]` 의도를 다시 재생한다. 응답 `changed_pages`의 source 페이지에는 `snapshot_operation_id`(= `restore_to_operation_id`)가 실린다.
- 복원 command 계약(ADR 0042 최종 설계): document-svc는 대상 작업의 행(`operation_id`)과 그 작업이 이은 행(`linked_by_operation_id`)만 끄고,
  어떤 행도 다시 켜지 않는다. 그 행이 있던 페이지 중 남은 활성 행이 있으면 `rebuild_pages`(남은 행을 적용 순서대로 `keep_contributions`),
  없으면 `deleted_pages`다. 남은 행이 lint 기록뿐이라는 이유로 `deleted_pages`에 넣지 않는다. 클러스터 승격 페이지는 lint 기록이 근거라,
  넣으면 그 항목의 분리를 되돌릴 때 승격 페이지까지 사라진다.
- 재조립·삭제로 가리키던 하위 항목이 없어진 참조(`entry_reference`)는 같은 복원에서 참조 사본·본문 목록·간선을 함께 정리한다.
  그 참조를 가진 다른 살아 있는 페이지(`rebuild_pages`에 없음)는 활성 원장 기록으로 다시 그려 응답 `changed_pages`에 더한다
  (`markdown_key`는 `ops/{복원 작업}.md`). document-svc는 그 페이지의 새 버전만 남기고 원장 행은 바꾸지 않는다. 기록은 다음 lint가 지운다.
- 재조립한 페이지의 나가는 링크는 재생 결과(`supported_links`)로 바꾼다. lint 복원도 같다.
- 재편입 대체 뒤 재조립(빼는 작업 없는 재조립 명령): 같은 문서를 다시 편입하면 document-svc가 그 문서의 이전 편입 행을 대체해 끈다.
  새 편입의 `changed_pages`에 없는데 그 문서 근거를 잃은 페이지는 document-svc가 Kafka 명령(`kind`는 임시로 `rebuild_pages`, document-svc와
  이름을 맞추는 중)으로 보낸다. 본문은 복원과 같은 `operation_id`·`workspace_id`·`rebuild_pages`(`keep_contributions`)·`deleted_pages`이고
  빼는 작업(`cancel_operation_ids`·`target_operation_id`)과 `source_page`는 없다. ai는 남은 기여를 재생해 다시 만들고, 결과는 복원과 같은
  `changed_pages`·`deleted_pages`(재생이 삭제로 끝난 페이지 포함)이며 `operation_type`은 `page_rebuild`다. 나가는 링크·문서 연결·소스 → 개념
  자동 링크도 재생 결과로 바꿔, 대체된 문서의 근거와 링크가 그 페이지에서 빠진다. 편입 자체는 이 페이지들을 추측해 다시 만들지 않고, 자기가
  기여한 페이지만 쓴다.
- 되돌리기는 그 작업의 효과만 뺀다(ADR 0042). `rebuild_pages`의 재생이 삭제 기록(다시 합치기의 lint `content_action: "delete"`)에서
  끝나거나, 남은 유효 근거가 없으면(남은 하위 항목·근거 단위·lint가 아닌 기록의 직접 개념 기여·수동 기여가 모두 없다. 분리·링크
  lint 기록만으로는 남기지 않는다) 그 페이지는 지우고 응답의 `deleted_pages`에 더한다(`changed_pages`에는 없다). document-svc는
  이를 삭제로 기록한다. lint 복원도 같다.
- 재생 결과가 뒤 작업과 충돌하면(같은 하위 항목이 두 페이지에 재생되거나, 다시 쓰지 않는 다른 페이지가 이미 그 항목의 집이면)
  ai DB 현재 상태를 쓰기 전에 실패 코드 `restore_conflict`로 실패한다(재조립 본문 객체 `ops/{복원 작업}.md`만 남을 수 있다).
  document-svc는 원장을 바꾸지 않고 복원을 실패로 표시한다. 뒤 작업부터 되돌려야 한다. lint 복원도 같다.
- `keep_contributions`에 수동 기여가 있으면 concept 페이지는 마지막 수동 본문을 기준으로 삼고, 그 뒤 AI 기여의 근거만 덧붙인다. 이름·요약은 덮어쓰지 않는다. 링크는 수동 기여의 추가·삭제까지 적용 순서대로 재생한다([ADR 0031](../adr/0031-manual-wiki-contributions.md)).
- source 페이지를 수동 기여 스냅샷으로 되돌릴 때도 이름·요약을 본문에서 다시 읽지 않는다.

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
          "document_id": "string | null",
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

#### 연동

- 인바운드 호출자: **HTTP 호출자 없음.** document-svc는 복구 command를 Kafka `ai.maintenance.command`로 발행한다(Fruition-document `src/main/java/fruition/core/aihistory/service/RestoreExecuteService.java`:132). ai-svc는 `app/workers/task_worker.py`의 `kind: restore_ingest`·`restore_lint` 분기로 처리한다.
- 아웃바운드 호출: 없음(AI DB 복구).
- 미연동 표시: HTTP는 미연동. 같은 로직이 Kafka로 연결되어 있다.

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

lint가 승격·병합하며 `clusters/archived.md`로 옮긴 클러스터도 되돌린다. lint는 클러스터를 옮길 때 그 클러스터가 승격·병합된
페이지의 lint 기록 `wiki/{workspace_id}/pages/{page_id}/ops/{lint_operation_id}.json`의 `archived_clusters`
(`cluster_id`, 원래 섹션 `section`, archived.md에 덧붙인 항목 `archived_section`)에 남긴다. 복원은 `rebuild_pages`·`deleted_pages`의
대상 lint 기록에서 이를 읽어 원래 섹션을 `active.md`에 넣고, archived.md에서 같은 `archived_section` 항목을 지운다. lint 뒤에
같은 id의 클러스터가 `active.md`에 새로 생겼으면 덮어쓰지 않고 건너뛴다. 응답의 `cluster_changes`는 쓴 객체 키
(`written_object_keys`), 되돌린 클러스터(`restored_cluster_ids`), 건너뛴 클러스터(`skipped_cluster_ids`)다.

하위 항목 분리·다시 합치기의 복원은 방향 전환 없이 기록 재생으로 한다(ADR 0042). 분리를 되돌리면 자식에 남은 기여로 자식을
다시 재생하고, 남은 근거가 없으면 지운다. 지운 자식을 가리키던 뒤 링크는 부모로 옮기지 않고 사라진다. 다시 합치기를 되돌리면
합친 lint 행(자식의 삭제 기록, 부모의 `revive`, 다른 페이지가 자식으로 오던 링크를 지운 기록)과 그 lint가 이은 행이 빠져 자식이
재생되고, 그 lint 기록이 지웠던 링크(`removed_links`) 중 남은 기록이 아직 잇는 것만 `link_changes.restored_links`로 되살린다.

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
          "document_id": "string | null",
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

#### 연동

- 인바운드 호출자: **HTTP 호출자 없음.** document-svc는 복구 command를 Kafka `ai.maintenance.command`로 발행한다(Fruition-document `src/main/java/fruition/core/aihistory/service/RestoreExecuteService.java`:132). ai-svc는 `app/workers/task_worker.py`의 `kind: restore_ingest`·`restore_lint` 분기로 처리한다.
- 아웃바운드 호출: 없음(AI DB 복구).
- 미연동 표시: HTTP는 미연동. 같은 로직이 Kafka로 연결되어 있다.

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

하위 항목 재검사([ADR 0042](../adr/0042-hierarchical-concept-pages.md) 3단계)도 함께 한다. 지난 lint 뒤 바뀐 개념 페이지의
하위 항목으로 분리·여러 부모 승격·다시 합치기를 정하고 사라진 항목을 가리키는 참조를 지운다(형식은
[data-model](../data-model.md)). 분리한 부모의 개념 관계(`concept_related_to`)는 Jev 같음·관련 판정으로
자식·부모 중 관련인 쪽으로 옮기고, Jev를 쓸 수 없으면 옮기지 않는다(ADR 0042 결정 6). 바뀐 페이지와 그 가까이의 페이지는
`part_of` 판정(Jev, 꺼져 있으면 이 lint의 `provider`·`model`)으로 다른 페이지나 그 하위 항목 아래 하위 항목으로 통째로 옮긴다(ADR 0042
페이지 이동, dry run은 판정하지 않는다). 응답의 `entry_decisions`는 판정(`action`·`page_id`·`entry_id`, 하지 못했으면 `skipped_reason`.
페이지 이동은 `action: "move"`와 `target_page_id`·`parent_entry_id`),
`deferred_entry_pages`는 다음 lint가 다시 볼 페이지, `deleted_pages`는 부모로 합쳐 지운 자식 페이지와 다른 페이지로 옮겨 지운 페이지다. 그 페이지에는 이 lint의
재생 가능한 삭제 기록(`content_action: "delete"`)이 남고, 그 기록은 `changed_pages`와 `ledger_contributions`에 함께 실린다
(document-svc는 `changed_pages`에 없는 페이지의 원장 행을 받지 않는다). 삭제 기록은 그때까지의 기록을 모두 지운 것과 같아,
재생이 삭제 기록에서 끝나면 페이지가 없다. `ledger_contributions`(`page_id`·`operation_id`·`document_id`·`contribution_key`·
`linked_by_operation_id`)는 document-svc가 기여 원장에 페이지별 행으로 더할 순서대로의 목록이다. 이 lint가 저장한 기록(`operation_artifacts`)은 하위 항목·승격·병합·링크만 바꾼 기록 모두 행이 되며,
`operation_id`가 이 lint, `document_id`·`linked_by_operation_id`가 null이다. 원래 작업 id로 다른 페이지에 이은 편입 기록은
`operation_id`·`document_id`가 원래 편입이고 `linked_by_operation_id`가 이 lint다. 같은 작업을 같은 페이지에 여러 번 이으면 한
기록·한 행으로 합친다. document-svc는 어떤 행도 일괄 비활성화하지 않고 다시 켜지도 않는다. 합친 자식의 기존 기여도 그대로
활성이다. 이 lint를 복원할 때는 `operation_id`나 `linked_by_operation_id`가 이 lint인 행만 끄고, 그 행이 있던 페이지를 남은 활성
기여로 `rebuild_pages`에 넣는다. 남은 활성 행이 하나도 없는 페이지만 `deleted_pages`다. 남은 행이 lint 기록뿐이어도 `rebuild_pages`로
넘기고, 페이지를 지울지는 ai 재생이 정한다(클러스터 승격 페이지는 lint 기록의 근거로 남는다. 분리·링크 lint 기록만 남은 페이지는 재생이
지우고 응답 `deleted_pages`로 돌려준다).

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
  "materialized_promotions": [],
  "merged_promotions": [],
  "needs_review": [],
  "orphan_refs": [],
  "promotion_candidates": [],
  "reconciliation_candidates": [],
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
  --data '{"dry_run":true,"materialize_promotions":true,"model":"gpt-6-luna","operation_id":"<value>","provider":"openai","user_id":"local-user","workspace_id":"local-workspace"}'
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
  "materialized_promotions": [],
  "merged_promotions": [],
  "needs_review": [],
  "orphan_refs": [],
  "promotion_candidates": [],
  "reconciliation_candidates": [],
  "source_ref_count": 0,
  "user_id": "local-user",
  "workspace_id": "local-workspace"
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/wiki_ingestion/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: lint_wiki_workspace_wiki_maintenance_lint_post`)

#### 연동

- 인바운드 호출자: **HTTP 호출자 없음.** document-svc는 Kafka `ai.maintenance.command`(`kind: lint`)를 발행한다(Fruition-document `src/main/java/fruition/core/wikimaintenance/service/WikiMaintenanceService.java`:66).
- 아웃바운드 호출: LLM provider(승격 생성), Jev·임베딩 서버(승격 개념 판정, `JEV_CONCEPT_MERGE_ENABLED`와 `EMBEDDING_SERVICE_URL`이 있을 때).
- 미연동 표시: HTTP는 미연동. 같은 로직이 Kafka로 연결되어 있다.

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

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/wiki/repository/PipelineWikiStateRequester.java`:54.
- 아웃바운드 호출: 없음.
- 미연동 표시: 없음.

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

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/wiki/repository/PipelineWikiStateRequester.java`:43.
- 아웃바운드 호출: 없음.
- 미연동 표시: 없음.

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
- 페이지는 `workspace_id`로만 찾고 작성자(`user_id`)를 확인하지 않는다. 누가 이름을 바꿀 수 있는지는 document가 정한다(ADR 0033). 공개 이름 변경 API는 document에서 닫혀 있다.

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

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/wiki/repository/PipelineWikiPageRequester.java`:37 (`app.wiki-page.endpoint`).
- 아웃바운드 호출: 없음.
- 미연동 표시: 없음.

[↑ 요약으로 돌아가기](#summary-patch-wiki-pages-page-id-rename)

</details>

<a id="summary-put-wiki-pages-page-id-manual-edit"></a>
### `PUT /wiki/pages/{page_id}/manual-edit`

| 항목 | 내용 |
|---|---|
| 목적 | 사람이 고친 Wiki 본문을 수동 기여로 저장합니다. |
| 입력 | **Path** — `page_id`: `string`<br>**Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `WikiPageManualEditIn` |
| 출력 | `200` 성공 — `object` (document-svc가 기록할 `changed_pages` 항목) |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>`workspace_id`의 활성 페이지를 고친다. 작성자(`user_id`)는 확인하지 않으며, 누가 고칠 수 있는지는 document가 정한다(ADR 0033).<br>revision·`base_revision` 충돌 검사는 호출자(document-svc)가 맡는다. |
| 주요 오류 | `404` 페이지 없음·다른 워크스페이스·비활성<br>`422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-put-wiki-pages-page-id-manual-edit"></a>
### `PUT /wiki/pages/{page_id}/manual-edit` 상세

#### 1. Method + Path

`PUT /wiki/pages/{page_id}/manual-edit`

#### 2. 목적

사람이 고친 source·concept 페이지 본문을 수동 기여(`artifact_type: "manual"`)로 저장하고 현재 상태에 반영한다. 정책은 [ADR 0031](../adr/0031-manual-wiki-contributions.md)를 따른다.

- `wiki/{workspace_id}/pages/{page_id}/ops/{operation_id}.md`(본문)와 `.json`(수동 기여)을 쓴다.
- `wiki_pages.markdown_uri`를 새 본문으로 바꾼다. 이름·요약은 바꾸지 않는다(이름은 `rename`으로만 바꾼다).
- 이전 본문과 새 본문의 `[[slug]]` 차이를 `wiki_page_links`에 반영한다. 지운 slug는 그 대상으로 가는 edge를 종류와 무관하게 지우고, 새 slug는 같은 워크스페이스의 활성 페이지가 있을 때만 edge를 만든다(source 페이지는 `source_mentions_concept`, concept 페이지는 `related_to`). source 페이지의 개념 링크는 수동 의도(`wiki_source_manual_links`)로 남겨 자동 파생 링크와 합친다. 자동 링크가 있어도 새 slug는 수동 지원으로 남고, 수동 삭제가 자동보다 우선한다(ADR 0042 결정 5).
- 본문의 블록 참조(`[doc:B0001]`)로 임베딩 단위를 다시 만들고 임베딩 job을 시작한다.
- 같은 `operation_id` 재요청은 다시 쓰지 않고 같은 결과를 돌려준다. 이때 `content_hash`는 요청 본문이 아니라 저장된 본문의 해시다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| path | `page_id` | `string` | 예 | - |
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`WikiPageManualEditIn`)

```json
{
  "markdown": "string",
  "operation_id": "string",
  "user_id": "string",
  "workspace_id": "string"
}
```

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json`

```json
{
  "page_id": "string",
  "page_type": "concept",
  "markdown_key": "wiki/<workspace_id>/pages/<page_id>/ops/<operation_id>.md",
  "contribution_key": "wiki/<workspace_id>/pages/<page_id>/ops/<operation_id>.json",
  "content_hash": "sha256:<hex>"
}
```

#### 6. Error response

- HTTP `401`: 내부 인증 토큰 누락 또는 불일치
- HTTP `404`: 페이지 없음·다른 워크스페이스·비활성
- HTTP `503`: 내부 인증 미설정

| HTTP 상태 | 설명 | 응답 스키마 |
|---|---|---|
| `422` | Validation Error | `HTTPValidationError` |

#### 7. Pagination / filtering

- 페이지네이션: 지원하지 않음
- 필터링: 지원하지 않음

#### 8. 권한 규칙

- 올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.
- 페이지는 `workspace_id`로만 찾고 작성자(`user_id`)를 확인하지 않는다. 누가 고칠 수 있는지는 document가 정한다(ADR 0033). `user_id`는 수동 기여 JSON에 귀속 정보로 남는다.

#### 9. 예시 요청/응답

```bash
curl -X PUT "$PIPELINE/wiki/pages/<value>/manual-edit" \
  -H 'X-Internal-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"markdown":"<value>","operation_id":"<value>","user_id":"<value>","workspace_id":"<value>"}'
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/wiki_ingestion/interfaces/http/routes.py`
- 저장: `pipeline/app/modules/wiki_ingestion/infrastructure/postgres_wiki_ingestion_repository.py` (`save_manual_wiki_edit`)
- 링크 차이: `pipeline/app/modules/wiki_ingestion/domain/manual_wiki_edit.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: save_manual_wiki_edit_wiki_pages__page_id__manual_edit_put`)

#### 연동

- 인바운드 호출자: 미연동. document-svc가 base_revision 검사 뒤 동기 호출하고, 응답 항목으로 수동 기여(`wiki_page_contributions`)와 버전(`wiki_page_versions`)을 기록해야 한다.
  - `wiki_page_contributions.object_key`에는 응답의 `contribution_key`(`.json`)를 기록한다. `markdown_key`를 기록하면 재편입이 수동 기여를 알아보지 못해 source 본문을 덮어쓴다.
  - `wiki_page_versions.markdown_key`에는 응답의 `markdown_key`를 기록한다.
- 아웃바운드 호출: 없음.
- 미연동 표시: document-svc 호출부(별도 이슈).

[↑ 요약으로 돌아가기](#summary-put-wiki-pages-page-id-manual-edit)

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

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/wiki/repository/PipelineWikiStateRequester.java`:70.
- 아웃바운드 호출: 없음.
- 미연동 표시: 없음.

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

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/wiki/repository/PipelineWikiStateRequester.java`:77.
- 아웃바운드 호출: 없음.
- 미연동 표시: 없음.

[↑ 요약으로 돌아가기](#summary-get-wiki-workspaces-workspace-id-last-updated)

</details>

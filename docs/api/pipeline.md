# Wiki Ingest Pipeline API

[서비스 문서](../README.md) / [ai-svc](README.md)

문서·채팅 Wiki ingest run의 상태·로그를 제공하는 내부 API다. 공개 Gateway 계약은
문서 [`ingest API`](https://github.com/FruitionKR/Fruition-document/blob/main/docs/api/ai.md)와 [`채팅 Wiki 내보내기 API`](https://github.com/FruitionKR/Fruition-document/blob/main/docs/api/chat.md)다.
ingest 실행은 HTTP로 받지 않는다. Backend가 Kafka `ai.ingest.command`(`kind: document`·`chat_wiki`, Fruition-document `src/main/java/fruition/core/document/repository/IngestCommandOutbox.java`:56)를 발행하고
`app/workers/ingest_worker.py`가 실행한다. 실행 중 문서 본문은 document-svc `GET /internal/documents/{document_id}/pipeline-source`(`app/modules/wiki_ingestion/infrastructure/backend_document_reader.py:19`)로 읽고,
기여 기록은 `POST /internal/wiki/contributions`(같은 파일 52행)로 보낸다. 수동 실행은 `pipeline/run_lab.py` CLI를 쓴다.
`GET /pipeline/runs/{run_id}`만 상태 확인용 내부 HTTP로 사용한다.

`ai.ingest.command`에는 편입 외에 문서 단위 bookkeeping command가 있다. 둘 다 작업 journal·크레딧 예약·LLM 호출 없이 처리하고,
파기한 워크스페이스면 `ScopePurgedError`로 폐기한다. 결과 event는 각각 `document_cleanup`, `document_rename`, `document_purge` kind이며 document는 처리하지 않고 건너뛴다. 형식이 맞지 않는 command와 이 worker가 모르는 kind는 실패 event를 내고 offset을 넘겨 파티션을 막지 않는다.
- `document_purged`: `{run_id, kind, document_id, workspace_id}`. 문서를 영구 삭제했을 때 온다(휴지통 이동에는 오지 않는다). AI에 남은 본문 복사본을 지운다. 지우는 범위와 재시도는 [agent.md](agent.md#document-purge)를 본다. 객체 삭제가 실패해도 예외를 올리지 않아 offset이 멈추지 않는다.
- `document_deleted`: `{run_id, kind, document_id, workspace_id}`. 문서 파생 Wiki 데이터를 정리한다.
- `document_renamed`: `{run_id, kind, document_id, workspace_id, title}`. `title`은 새 표시 이름(파일명에서 확장자를 뗀 이름)이며 비어 있으면 폐기한다.
  해당 문서의 소스 페이지 제목(`wiki_pages.title`)만 바꾼다. 본문·개념 페이지는 그대로이고, 임베딩 텍스트에 들어간 옛 제목은 다음 편입 때 새 제목으로 다시 만든다.
  같은 문서의 편입과 같은 파티션(key=`document_id`)으로 순서대로 처리하므로 편입 결과가 새 제목을 덮어쓰지 않는다. 아직 편입되지 않은 문서는 바꿀 행이 없어 아무 일도 하지 않고, 같은 command를 다시 받아도 결과가 같다.

ingest command의 범위와 제목 규칙:
- `workspace_id`가 문서의 워크스페이스와 다르면 `409`다. `user_id`는 편입을 요청한 사람으로 기록하는 귀속 정보라
  문서 소유자와 달라도 된다(공동 편집). 없으면 문서 소유자로 기록한다.
- 문서 편입·재편입의 소스 페이지 제목은 `display_name`이다. 없으면 `pipeline-source` 응답의 `filename`에서
  마지막 확장자를 뗀 이름(document `Document.displayNameOf`와 같은 규칙)을 쓴다. 채팅 Wiki 내보내기는 이 값을 쓰지 않고
  생성 제목을 유지한다. document가 그 제목으로 내보내기 파일 이름을 정하기 때문이다.

- API 수: 4

## API 목차

| API | 목적 |
|---|---|
| [`GET /pipeline/runs/{run_id}`](#summary-get-pipeline-runs-run-id) | pipeline run의 상태와 결과를 조회합니다. |
| [`GET /pipeline/runs/{run_id}/logs`](#summary-get-pipeline-runs-run-id-logs) | pipeline run 로그를 조회합니다. |
| [`GET /documents/{document_id}`](#summary-get-documents-document-id) | pipeline 처리를 위한 문서 정보를 조회합니다. |
| [`GET /health`](#summary-get-health) | ai-svc의 상태를 확인합니다. |

## 한눈에 보기

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

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/document/repository/PipelineRunStatusRequester.java`:30 (`app.pipeline-run.endpoint`).
- 아웃바운드 호출: 없음(AI DB 조회).
- 미연동 표시: 없음.

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

#### 연동

- 인바운드 호출자: **호출자 없음.** document-svc에 `/logs`를 호출하는 코드가 없다. 운영자 수동 조회용이다.
- 아웃바운드 호출: 없음(로그 저장소 조회).
- 미연동 표시: **미연동.** 서비스 간 호출자가 없다.

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

- 진입점: `pipeline/api.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: get_document_documents__document_id__get`)

#### 연동

- 인바운드 호출자: **호출자 없음.** 문서 조회의 실제 방향은 반대다 — ai-svc가 document-svc `GET /internal/documents/{document_id}/pipeline-source`(Fruition-document `src/main/java/fruition/core/document/controller/InternalDocumentController.java`:55)를 호출한다.
- 아웃바운드 호출: 없음(AI DB 조회).
- 미연동 표시: **미연동.** 운영 점검용으로만 남아 있다.

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

- 진입점: `pipeline/api.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: health_health_get`)

#### 연동

- 인바운드 호출자: 서비스 간 호출자 없음. 컨테이너·오케스트레이터 probe용이다.
- 아웃바운드 호출: 없음.
- 미연동 표시: 해당 없음(운영 probe).

[↑ 요약으로 돌아가기](#summary-get-health)

</details>

후처리 run의 manifest에는 `stage_timings`를 기록한다. 각 행은 `stage`, `attempt`, `elapsed_seconds`, `status`이며 실제 수행한 단계만 기록하고 재시도·실패 시간도 보존한다. 클러스터 판정·저장, 임베딩, 평가 질문 생성, 검색 준비·질문별 검색, 근거 평가를 구분하며 큐 대기나 전체 처리 시간과는 다르다. 임베딩은 편입이 저장한 페이지(`page_ids`)를 대상으로 클러스터 판정·저장과 동시에 돌므로 두 단계 시간은 겹친다. 클러스터 저장은 의미 클러스터 파일만 쓰고 위키 페이지를 고치지 않는다(같은 개념 갱신은 편입 저장이 반영한다). 차등 편입의 평가 질문은 기존 3문항 안에서 추가·수정 블록과 유지 블록을 함께 포함한다. 인용 검증 후 이 범위를 충족하지 못하면 `needs_review`로 남기며 질문 생성을 추가 호출하지 않는다. 같은 프로세스 안에서 세 번 시도해도 단계가 실패하면, 취소가 아닌 한 run을 실패로 끝내지 않는다. 성공한 단계 결과는 남기고 `quality_status=needs_review`로 완료하며, 결과에 `error`(실패 코드)와 `failed_stage`(마지막으로 실패한 단계)를 싣는다(ADR 0044). 취소·마감 초과·원본 삭제는 실패로 끝내고 변경을 되돌린다.

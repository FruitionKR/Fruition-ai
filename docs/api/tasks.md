# AI 내부 작업 취소 API

[서비스 문서](../README.md) · [API 목차](README.md)

Document의 [공개 취소 API](https://github.com/FruitionKR/Fruition-document/blob/main/docs/api/tasks.md)가 호출합니다. 사용자 클라이언트에서 직접 호출하지 않습니다.

| Method + Path | 인증 | 입력 | 출력·목적 |
|---|---|---|---|
| `POST /internal/ai/tasks/{run_id}/cancel` | `X-Internal-Token` | `workspace_id`, `user_id`, 선택 `command` | `200`: AI 취소 상태. 전달 전 command도 등록해 실행 차단 |
| `GET /internal/ai/tasks/{run_id}` | `X-Internal-Token` | query `workspace_id`, `user_id` | `200`: AI 복구 상태 |
| `POST /internal/ai/tasks/{run_id}/rollback-backend` | `X-Internal-Token` | 같은 actor와 선택 `command` | `204`: Python이 업무 변경을 역순 복구. AI 복구 미완료·충돌 `409` |

취소 command의 ID·actor는 경로·body와 같아야 하며 기존 command와 내용이 다르면 `409`입니다.
내부 상태 응답의 `error_code`는 오류가 없을 때 `null`입니다. 목록 pagination은 없습니다.
실패(`failed`)로 닫힌 작업의 `error_code`는 실패 예외가 사유 코드를 가지면 그 코드(예: Skill 거절 `intent_ambiguous`), 없으면 `task_failed`입니다.

## 실패 코드 계약

ingest 결과 이벤트(`kind=document`·`chat_wiki` 등 ingest worker 발행)는 최상위 불리언 `post_ingest_dispatched`를 항상 담습니다. 이 편입에서 post_ingest command를 실제로 발행했을 때만 `true`이고, 발행하지 않은 경우(실패 이벤트, 문서 이름 변경·삭제 같은 bookkeeping, manifest에 `post_ingest`가 없는 편입)는 `false`입니다. 재전달은 저장된 run 상태에서 같은 판단을 다시 내리므로 원래 이벤트와 같은 값입니다. Document는 `false`면 post_ingest 결과를 기다리지 않아야 합니다.

`kind=post_ingest` 결과 이벤트는 원본 command(`request`)를 싣지 않는 대신, 어느 편입의 후속인지 알 수 있게 부모 편입 run id를 최상위 `ingest_run_id`에 담습니다.

`ai.task.event`의 실패 이벤트 `error`, `agent_runs.error_code`, `pipeline_runs.error`, ingest·post_ingest 실패 payload의 `summary`는 예외 원문이 아니라 코드만 담습니다. 사용자에게 보이는 문구는 Document가 코드별로 정하며, 예외 원문은 AI 로그에만 남습니다.

| 코드 | 의미 |
|---|---|
| `provider_rate_limited` | 모델 공급사 429·rate limit |
| `provider_unavailable` | 공급사 5xx·시간 초과·연결 오류·과부하 |
| `input_rejected` | 검증 실패·`ValueError` 등 처리할 수 없는 입력 |
| `deadline_exceeded` | 호출자가 기다리는 시간 초과 |
| `cancelled` | 취소 요청·대상 비활성화·파기된 범위 |
| `internal_error` | 그 외 모든 실패 |
| `mutation_intent_required` | Agent 계획 단계에서 변경 의도를 확인해야 하는 되묻기(clarification) 사유 |

기능별 기존 코드(`agent_turn_failed`, `agent_turn_cancelled`, `invalid_agent_turn_request`, `agent_not_configured`, `agent_turn_route_contract_failed`, `markdown_*`, `SKILL_NOT_FOUND`, `SKILL_DISABLED`, `invalid_llm_selection`, `agent_turn_interrupted`, `task_failed`, `interrupted_attempts_exhausted`, `rollback_child_failed`, `MEETING_NOTES_*`, `rollback_*`)는 그대로 유지합니다. 분류는 `app/core/failure_codes.py`의 `failure_code(exc)`가 맡으며, 우리 예외(`TaskFailedError` 등)의 `code`만 그대로 쓰고 SDK 예외의 본문 code는 무시합니다. 저장된 값이 코드 모양이 아니면 재전달 때 `task_failed`로 바꿉니다.


상태 의미·복구 순서·검증 범위는 [공통 계약](https://github.com/FruitionKR/Fruition-flatform/blob/main/docs/api/ai-task-cancellation.md)을 따릅니다. 업무 DB 복구 API는 [Document](https://github.com/FruitionKR/Fruition-document/blob/main/docs/api/tasks.md#업무-변경-복구-내부-api)가 제공합니다.

## PDF 변환 command

document가 Kafka `ai.convert.command`(env `CONVERT_COMMAND_TOPIC`)에 쪽 범위 단위로 발행하고, converter worker가 한 번에 하나씩 처리한다. 결과는 `ai.task.event`로 돌려준다. 근거는 [ADR 0034](../adr/0034-async-pdf-conversion.md)를 따른다.

Kafka key는 `run_id`(batch id)다. 같은 문서의 batch가 파티션에 퍼져 병렬로 처리된다. 같은 문서의 batch는 AI가 서로 독립으로 처리하며, 쪽 순서 병합은 document가 한다.

```json
{"run_id": "convert:<document_id>:<start_page>", "parent_run_id": "convert:<document_id>", "kind": "convert_batch",
 "workspace_id": "ws_...", "user_id": "...", "document_id": "<placeholder 문서 id>",
 "source_key": "sources/documents/<document_id>/original", "start_page": 0, "page_count": 10,
 "provider": "gemini", "model": "..."}
```

| 필드 | 규칙 |
|---|---|
| `run_id` | batch 식별자. 같은 `run_id`의 재전달은 저장된 결과를 돌려주고 다시 변환하지 않는다. 같은 `run_id`에 다른 내용을 보내면 `input_rejected` |
| `parent_run_id` | document가 정산하는 변환 run. 모델 사용량(`document_conversion`)은 이 값으로 귀속한다 |
| `source_key` | object storage의 원본 PDF key. `sources/documents/{id}/original` 형식만 받는다(`s3://` 불가). converter가 범위 읽기로 직접 읽는다 |
| `start_page` | 0부터. 전체 쪽수 이상이면 `input_rejected` |
| `page_count` | 1~50. 전체 쪽수를 넘는 끝은 마지막 쪽으로 줄인다 |

성공 이벤트(`status: succeeded`, key = `run_id`, 원본 command `request`는 싣지 않는다)의 `payload`:

| 필드 | 의미 |
|---|---|
| `document_id` | command의 `document_id` |
| `page_start`, `page_end` | 실제 변환한 쪽(1부터, 끝 포함) |
| `total_pages` | 원본 PDF의 전체 쪽수 |
| `result_key` | `tmp/convert/{workspace_id}/{document_id}/{start_page}.md`. UTF-8 Markdown이며 이미지는 `data:` URI로 포함한다. document가 읽은 뒤 쓰지 않는 object는 lifecycle이 지운다 |

실패 이벤트(`status: failed`)의 `error`는 위 [실패 코드](#실패-코드-계약)의 공통 코드다: 암호·손상 PDF, 범위·`source_key` 위반은 `input_rejected`, 전체 마감(`CONVERT_BATCH_DEADLINE_SECONDS`, 기본 7200초) 초과는 `deadline_exceeded`, 취소는 `cancelled`, 그 외는 `internal_error`. 예외 원문은 싣지 않는다.

변환 시도가 실패하거나 시도 제한(`CONVERT_ATTEMPT_TIMEOUT_SECONDS`, 기본 1500초)을 넘기면 AI가 쪽 범위를 절반으로 나눠 1쪽까지 다시 시도한다. 1쪽도 실패하면 batch가 실패한다. 입력 거절과 취소는 나누지 않는다. 취소는 위 `POST /internal/ai/tasks/{run_id}/cancel`에 batch의 `run_id`와 `command`를 보내며, 진행 중인 변환은 약 2초 안에 중단된다. `parent_run_id`로는 취소할 수 없고 batch마다 보내야 한다.

## 모델별 사용량 조회

`GET /usage/models?workspace_id=...&user_id=...&from_at=...&to_at=...`

- `X-Internal-Token` 인증이 필요하다. 백엔드가 인가한 workspace와 로그인 사용자를 전달한다.
- 기간은 시간대를 포함하는 ISO 8601, 시작 포함·종료 제외다. 생략하면 UTC 이번 달 시작부터 현재까지 조회한다.
- 응답: `workspace_id`, `user_id`, `from_at`, `to_at`, `models`.
- `models[]`: `provider`, `requested_model`, `model`, `calls`, `failed_calls`, `unfinished_calls`, `unknown_usage_calls`, `known_input_tokens`, `known_output_tokens`, `known_cached_input_tokens`, `known_cache_creation_tokens`, `known_reasoning_tokens`, `unknown_cache_usage_calls`.
- 캐시 입력은 전체 입력의 일부, reasoning은 전체 출력의 일부이므로 단순 합산하지 않는다. 알려진 토큰 합계와 사용량 미확인 호출 수를 함께 표시한다. 현재 API는 토큰 조회이며 가격표·크레딧·세금을 반영한 금액을 반환하지 않는다.
- `unfinished_calls`는 `started`와 `abandoned` 호출을 함께 센다. 취소된 TTS 호출(`cancelled`)은 받은 사용량이 토큰 합계에 포함되며 `failed_calls`·`unfinished_calls`에는 세지 않는다.
- AI DB `ai_model_usage`에 공통 ChatCompletions 클라이언트·Jev·OpenAI 음성 호출의 시작과 응답 사용량을 별도 트랜잭션으로 기록한다. 호출 전 원장 기록 실패 시 모델을 호출하지 않는다. 응답 기록 실패·프로세스 중단 시 시작 기록이 `started`로 남고, 1시간이 지나면 사용량 조회 직전에 `abandoned`로 닫힌다. 닫힌 뒤 늦게 끝난 호출은 상태와 `finished_at`을 유지하고 사용량만 채운다. 기간 조회는 같은 행을 두 번 돌려주지 않으므로, 늦게 채워진 사용량은 `run_id` 조회로 확인한다. 실패·취소로 사용량 원장을 롤백하지 않는다.
- Kafka 작업(ingest/query/agent/maintenance), journal 기반 Skill 작업, Agent worker와 채팅 제목 생성의 사용자 컨텍스트를 전달한다. 의미 추출·회의록 묶음 스레드(회의록은 Kafka command `kind: meeting_notes`, run_id는 command의 `run_id`)에는 `copy_context`가 컨텍스트를 전달한다.
- HTTP 동기 경로(`/wiki-schema/preview`, `/skills/draft-from-runs/preview`, `/speech/transcriptions`, `/speech/synthesis`, `WS /speech/transcriptions/live`)는 document가 보낸 `X-Request-Id` 헤더(최대 128자)를 `run_id`로, 요청의 `workspace_id`·`user_id`를 귀속 대상으로 기록한다.
- 귀속 값(`run_id`·`workspace_id`·`user_id`)이 빠진 과금 호출은 건너뛰지 않고 그 값을 `unattributed`로 기록하며 경고 로그를 남긴다. 누락 경로가 없어지면 거부로 바꾼다.
- PDF converter의 selective repair(OpenAI·Gemini·Claude 직접 호출)는 kind `document_conversion`으로 기록한다. converter worker가 `convert_batch` command의 `parent_run_id`를 `run_id`로, command의 `workspace_id`·`user_id`를 귀속 대상으로 env `MODEL_USAGE_SCOPE`(JSON)에 담아 restoration 하위 프로세스에 넘긴다(document가 변환 run 단위로 정산한다). 공급사 usage 원값은 아래 토큰 포함 관계에 맞춰 정규화하며, 응답 본문 파싱에 실패해도 받은 사용량은 남긴다. 원장 시작 기록이 실패하면(`AI_DATABASE_URL` 누락, DB 연결 오류) selective repair가 공급사를 호출하지 않고 기본 변환 결과를 유지한다. 응답을 받은 뒤 사용량 갱신만 실패하면 응답은 그대로 쓰고 경고 로그를 남기며, 그 행은 `started`로 남았다가 `abandoned`로 닫힌다. actor가 빠진 변환 요청은 converter 운영 로그에도 경고로 남는다.
- 음성은 토큰 대신 오디오 단위를 함께 남긴다. 파일 전사는 공급사 usage(토큰 또는 `duration` 초), TTS(`gpt-realtime-2.1-mini`)는 입력 문자 수(`input_characters`)와 `response.done`의 토큰 usage(입력 텍스트·출력 오디오 토큰), 실시간 전사는 확정 구간(commit)마다 한 행으로 보낸 PCM 길이(`audio_seconds`)와 구간 usage를 기록한다. 전사를 받지 못하고 끝난 구간은 `failed`로 남는다.
- 운영 경로의 Jev 호출은 provider `typesafe`, model `jev-1.13.0`으로 같은 원장에 기록한다. 사용자 실행 컨텍스트(scope)가 없는 독립 스크립트·실험용 호출은 경고 로그만 남기고 기록하지 않는다. 자체 호스팅 임베딩(bge-m3)은 이 원장에 포함되지 않는다. 과거 로그를 소급 집계하지 않는다. SDK 내부 재시도의 미수신 응답 사용량은 확보할 수 없으므로 청구서 대체 자료가 아니다.

## 호출 단위 사용량 조회

`GET /internal/model-usage/calls?run_id=...` 또는 `GET /internal/model-usage/calls?finished_from=...&finished_to=...`

- `X-Internal-Token` 인증이 필요하다. AI는 금액을 계산하지 않으며, document가 호출 시점 단가를 적용하도록 호출 단위 행을 돌려준다.
- `run_id`: 실행 종료 후 그 실행의 호출을 가져가는 주 경로다. `started` 행도 포함한다.
- `finished_from`·`finished_to`: 일 단위 대사용이다. 시간대를 포함하는 ISO 8601이며 `finished_at` 기준 시작 포함·종료 제외다. 끝나지 않은 행은 빠지므로 1시간 이상 지난 구간만 조회해야 늦게 커밋된 행과 `abandoned` 전환을 놓치지 않는다.
- 두 방식은 함께 쓸 수 없고 하나는 반드시 지정해야 한다. 위반하면 400이다.
- 응답: `calls[]` — `id`, `run_id`, `workspace_id`, `user_id`, `kind`, `provider`, `requested_model`, `model`, `status`(`started`·`succeeded`·`failed`·`abandoned`·`cancelled`), `input_tokens`, `output_tokens`, `cached_input_tokens`, `cache_creation_tokens`, `reasoning_tokens`, `audio_seconds`, `input_characters`, `started_at`, `finished_at`. 알 수 없는 사용량은 `null`이다.

### 토큰 포함 관계

LangChain usage는 공급사 원값을 다음처럼 정규화한다. 금액을 계산할 때 하위 항목을 상위 항목에 더하지 않는다.

| provider | `input_tokens` | `output_tokens` |
|---|---|---|
| `openai` (Chat) | `cached_input_tokens` 포함 | `reasoning_tokens` 포함 |
| `anthropic` | `cached_input_tokens`·`cache_creation_tokens` 포함 (공급사 원값에 LangChain이 더한다) | `reasoning_tokens` 포함 |
| `gemini` | `cached_input_tokens`·서버 tool prompt 포함 | `reasoning_tokens`(thoughts) 포함 |
| `openai` 음성 | 전사: 오디오·텍스트 입력 토큰 합계, TTS: 입력 텍스트 토큰(공급사 usage 원값) | 전사: 텍스트 토큰, TTS: 오디오 토큰 |
| `typesafe` (Jev) | Jev 응답 usage 원값 | Jev 응답 usage 원값 |

비캐시 입력은 `input_tokens - cached_input_tokens`(Anthropic은 `- cache_creation_tokens`도 뺀다), 비추론 출력은 `output_tokens - reasoning_tokens`다.

## 연동

다른 도메인 문서와 같은 세 항목을 API마다 기록한다. 인바운드는 document-svc의 Java client,
아웃바운드는 ai-svc가 다시 호출하는 대상이다.

### `POST /internal/ai/tasks/{run_id}/cancel`

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/aitask/repository/PipelineTaskCancellationClient.java`:26. base URL은 `app.agent.status-endpoint`의 host에 `/internal/ai/tasks`를 붙여 만든다(같은 파일 22행).
- 아웃바운드 호출: 없음(AI DB의 취소 command 등록).
- 미연동 표시: 없음.

### `GET /internal/ai/tasks/{run_id}`

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/aitask/repository/PipelineTaskCancellationClient.java`:38-40.
- 아웃바운드 호출: 없음(AI DB 조회).
- 미연동 표시: 없음.

### `POST /internal/ai/tasks/{run_id}/rollback-backend`

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/aitask/repository/PipelineTaskCancellationClient.java`:32.
- 아웃바운드 호출: document-svc `POST /internal/agent/tools/rollback/{run_id}/changes`, 이어서 각 변경의 `.../changes/{change_id}`와 `.../rollback/{run_id}/finalize-edits`를 `X-Agent-Service-Token`으로 호출한다(`pipeline/app/modules/task_cancellation/infrastructure/backend_rollback.py`:12,18,20). base URL은 `AGENT_BACKEND_URL`(기본 `http://document-svc:8080`)이다.
- 미연동 표시: 없음.

### `GET /usage/models`

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/usage/service/ModelUsageService.java`:35-40 (`app.model-usage.endpoint`, 기본값 `http://localhost:8000/usage/models`).
- 아웃바운드 호출: 없음(AI DB `ai_model_usage` 조회).
- 미연동 표시: 없음.

### `GET /internal/model-usage/calls`

- 인바운드 호출자: **호출자 없음**. Fruition-document 크레딧 과금(FruitionKR/Fruition-document#77)이 연결할 예정이다.
- 아웃바운드 호출: 없음(AI DB `ai_model_usage` 조회·`abandoned` 정리).
- 미연동 표시: document가 HTTP 동기 호출에 `X-Request-Id`를 보내기 전에는 해당 호출이 `run_id='unattributed'`로 남는다.

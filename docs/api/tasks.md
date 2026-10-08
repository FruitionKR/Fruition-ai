# AI 내부 작업 취소 API

[서비스 문서](../README.md) · [API 목차](README.md)

Document의 [공개 취소 API](https://github.com/FruitionKR/Fruition-document/blob/main/docs/api/tasks.md)가 호출합니다. 사용자 클라이언트에서 직접 호출하지 않습니다.

| Method + Path | 인증 | 입력 | 출력·목적 |
|---|---|---|---|
| `POST /internal/ai/tasks/{run_id}/cancel` | `X-Internal-Token` | `workspace_id`, `user_id`, 선택 `command` | `200`: AI 취소 상태. 전달 전 command도 등록해 실행 차단 |
| `GET /internal/ai/tasks/{run_id}` | `X-Internal-Token` | query `workspace_id`, `user_id` | `200`: AI 복구 상태 |
| `POST /internal/ai/tasks/{run_id}/rollback-backend` | `X-Internal-Token` | 같은 actor와 선택 `command` | `204`: Python이 업무 변경을 역순 복구. AI 복구 미완료·충돌 `409` |
| `POST /internal/ai/tasks/documents/{document_id}/cancel` | `X-Internal-Token` | `workspace_id`, `user_id` | `200`: `cleanup_complete`로 연결된 수집·Wiki 복구 완료 확인 |

취소 command의 ID·actor는 경로·body와 같아야 하며 기존 command와 내용이 다르면 `409`입니다.
내부 상태 응답의 `error_code`는 오류가 없을 때 `null`입니다. 목록 pagination은 없습니다.
실패(`failed`)로 닫힌 작업의 `error_code`는 실패 예외가 사유 코드를 가지면 그 코드(예: Skill 거절 `intent_ambiguous`), 없으면 `task_failed`입니다.


상태 의미·복구 순서·검증 범위는 [공통 계약](https://github.com/FruitionKR/Fruition-flatform/blob/main/docs/api/ai-task-cancellation.md)을 따릅니다. 업무 DB 복구 API는 [Document](https://github.com/FruitionKR/Fruition-document/blob/main/docs/api/tasks.md#업무-변경-복구-내부-api)가 제공합니다.

## 모델별 사용량 조회

`GET /usage/models?workspace_id=...&user_id=...&from_at=...&to_at=...`

- `X-Internal-Token` 인증이 필요하다. 백엔드가 인가한 workspace와 로그인 사용자를 전달한다.
- 기간은 시간대를 포함하는 ISO 8601, 시작 포함·종료 제외다. 생략하면 UTC 이번 달 시작부터 현재까지 조회한다.
- 응답: `workspace_id`, `user_id`, `from_at`, `to_at`, `models`.
- `models[]`: `provider`, `requested_model`, `model`, `calls`, `failed_calls`, `unfinished_calls`, `unknown_usage_calls`, `known_input_tokens`, `known_output_tokens`, `known_cached_input_tokens`, `known_cache_creation_tokens`, `known_reasoning_tokens`, `unknown_cache_usage_calls`.
- 캐시 입력은 전체 입력의 일부, reasoning은 전체 출력의 일부이므로 단순 합산하지 않는다. 알려진 토큰 합계와 사용량 미확인 호출 수를 함께 표시한다. 현재 API는 토큰 조회이며 가격표·크레딧·세금을 반영한 금액을 반환하지 않는다.
- `unfinished_calls`는 `started`와 `abandoned` 호출을 함께 센다.
- AI DB `ai_model_usage`에 공통 ChatCompletions 클라이언트·Jev·OpenAI 음성 호출의 시작과 응답 사용량을 별도 트랜잭션으로 기록한다. 호출 전 원장 기록 실패 시 모델을 호출하지 않는다. 응답 기록 실패·프로세스 중단 시 시작 기록이 `started`로 남고, 1시간이 지나면 사용량 조회 직전에 `abandoned`로 닫힌다. 닫힌 뒤 늦게 끝난 호출은 상태와 `finished_at`을 유지하고 사용량만 채운다. 기간 조회는 같은 행을 두 번 돌려주지 않으므로, 늦게 채워진 사용량은 `run_id` 조회로 확인한다. 실패·취소로 사용량 원장을 롤백하지 않는다.
- Kafka 작업(ingest/query/agent/maintenance), journal 기반 Skill 작업, Agent worker와 채팅 제목 생성의 사용자 컨텍스트를 전달한다. 의미 추출·회의록 묶음 스레드에는 `copy_context`가 컨텍스트를 전달한다.
- HTTP 동기 경로(`POST /agent/turn`, `/query`, `/meeting-notes/preview`, `/wiki-schema/preview`, `/skills/draft-from-runs/preview`, `/speech/transcriptions`, `/speech/synthesis`, `WS /speech/transcriptions/live`)는 document가 보낸 `X-Request-Id` 헤더(최대 128자)를 `run_id`로, 요청의 `workspace_id`·`user_id`를 귀속 대상으로 기록한다.
- 귀속 값(`run_id`·`workspace_id`·`user_id`)이 빠진 과금 호출은 건너뛰지 않고 그 값을 `unattributed`로 기록하며 경고 로그를 남긴다. 누락 경로가 없어지면 거부로 바꾼다.
- 음성은 토큰 대신 오디오 단위를 함께 남긴다. 파일 전사는 공급사 usage(토큰 또는 `duration` 초), TTS는 입력 문자 수(`input_characters`)와 SSE 완료 이벤트의 토큰 usage(입력 텍스트·출력 오디오 토큰), 실시간 전사는 확정 구간(commit)마다 한 행으로 보낸 PCM 길이(`audio_seconds`)와 구간 usage를 기록한다. 전사를 받지 못하고 끝난 구간은 `failed`로 남는다.
- 운영 경로의 Jev 호출은 provider `typesafe`, model `jev-1.13.0`으로 같은 원장에 기록한다. 사용자 실행 컨텍스트(scope)가 없는 독립 스크립트·실험용 호출은 경고 로그만 남기고 기록하지 않는다. 별도 PDF converter의 selective repair와 자체 호스팅 임베딩(bge-m3)은 이 원장에 포함되지 않는다. 과거 로그를 소급 집계하지 않는다. SDK 내부 재시도의 미수신 응답 사용량은 확보할 수 없으므로 청구서 대체 자료가 아니다.

## 호출 단위 사용량 조회

`GET /internal/model-usage/calls?run_id=...` 또는 `GET /internal/model-usage/calls?finished_from=...&finished_to=...`

- `X-Internal-Token` 인증이 필요하다. AI는 금액을 계산하지 않으며, document가 호출 시점 단가를 적용하도록 호출 단위 행을 돌려준다.
- `run_id`: 실행 종료 후 그 실행의 호출을 가져가는 주 경로다. `started` 행도 포함한다.
- `finished_from`·`finished_to`: 일 단위 대사용이다. 시간대를 포함하는 ISO 8601이며 `finished_at` 기준 시작 포함·종료 제외다. 끝나지 않은 행은 빠지므로 1시간 이상 지난 구간만 조회해야 늦게 커밋된 행과 `abandoned` 전환을 놓치지 않는다.
- 두 방식은 함께 쓸 수 없고 하나는 반드시 지정해야 한다. 위반하면 400이다.
- 응답: `calls[]` — `id`, `run_id`, `workspace_id`, `user_id`, `kind`, `provider`, `requested_model`, `model`, `status`(`started`·`succeeded`·`failed`·`abandoned`), `input_tokens`, `output_tokens`, `cached_input_tokens`, `cache_creation_tokens`, `reasoning_tokens`, `audio_seconds`, `input_characters`, `started_at`, `finished_at`. 알 수 없는 사용량은 `null`이다.

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

### `POST /internal/ai/tasks/documents/{document_id}/cancel`

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/aitask/repository/PipelineTaskCancellationClient.java`:45.
- 아웃바운드 호출: 없음(AI DB 조회).
- 미연동 표시: 없음.

### `GET /usage/models`

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/usage/service/ModelUsageService.java`:35-40 (`app.model-usage.endpoint`, 기본값 `http://localhost:8000/usage/models`).
- 아웃바운드 호출: 없음(AI DB `ai_model_usage` 조회).
- 미연동 표시: 없음.

### `GET /internal/model-usage/calls`

- 인바운드 호출자: **호출자 없음**. Fruition-document 크레딧 과금(FruitionKR/Fruition-document#77)이 연결할 예정이다.
- 아웃바운드 호출: 없음(AI DB `ai_model_usage` 조회·`abandoned` 정리).
- 미연동 표시: document가 HTTP 동기 호출에 `X-Request-Id`를 보내기 전에는 해당 호출이 `run_id='unattributed'`로 남는다.

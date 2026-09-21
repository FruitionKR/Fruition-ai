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


상태 의미·복구 순서·검증 범위는 [공통 계약](https://github.com/FruitionKR/Fruition-flatform/blob/main/docs/api/ai-task-cancellation.md)을 따릅니다. 업무 DB 복구 API는 [Document](https://github.com/FruitionKR/Fruition-document/blob/main/docs/api/tasks.md#업무-변경-복구-내부-api)가 제공합니다.

## 모델별 사용량 조회

`GET /usage/models?workspace_id=...&user_id=...&from_at=...&to_at=...`

- `X-Internal-Token` 인증이 필요하다. 백엔드가 인가한 workspace와 로그인 사용자를 전달한다.
- 기간은 시간대를 포함하는 ISO 8601, 시작 포함·종료 제외다. 생략하면 UTC 이번 달 시작부터 현재까지 조회한다.
- 응답: `workspace_id`, `user_id`, `from_at`, `to_at`, `models`.
- `models[]`: `provider`, `requested_model`, `model`, `calls`, `failed_calls`, `unfinished_calls`, `unknown_usage_calls`, `known_input_tokens`, `known_output_tokens`, `known_cached_input_tokens`, `known_cache_creation_tokens`, `known_reasoning_tokens`, `unknown_cache_usage_calls`.
- 캐시 입력은 전체 입력의 일부, reasoning은 전체 출력의 일부이므로 단순 합산하지 않는다. 알려진 토큰 합계와 사용량 미확인 호출 수를 함께 표시한다. 현재 API는 토큰 조회이며 가격표·크레딧·세금을 반영한 금액을 반환하지 않는다.
- AI DB `ai_model_usage`에 공통 ChatCompletions 클라이언트의 호출 시작과 응답 사용량을 별도 트랜잭션으로 기록한다. 호출 전 원장 기록 실패 시 모델을 호출하지 않는다. 응답 기록 실패·프로세스 중단 시 시작 기록이 미완료로 남는다. 실패·취소로 사용량 원장을 롤백하지 않는다.
- Kafka 작업(ingest/query/agent/maintenance), journal 기반 Skill 작업, Agent worker와 채팅 제목 생성의 사용자 컨텍스트를 전달한다. 의미 추출 스레드에는 기존 `copy_context` 경로가 컨텍스트를 전달한다.
- 사용자 실행 컨텍스트가 없는 독립 스크립트, 별도 PDF converter, 로컬 임베딩 및 실험용 Jev 직접 호출은 이 원장에 자동 포함되지 않는다. 과거 로그를 소급 집계하지 않는다. SDK 내부 재시도의 미수신 응답 사용량은 확보할 수 없으므로 청구서 대체 자료가 아니다.

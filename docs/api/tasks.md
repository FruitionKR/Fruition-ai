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

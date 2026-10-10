# AI 데이터 파기 API

[서비스 문서](../README.md) · [API 목차](README.md)

사용자 탈퇴나 워크스페이스 영구 삭제 때 **Access**가 Document 파기가 성공한 뒤에 이어서 호출합니다([Fruition-access#29](https://github.com/FruitionKR/Fruition-access/issues/29)). 순서와 근거는 [ADR 0029](../adr/0029-ai-data-purge-contract.md)를 따릅니다. 사용자 클라이언트에서 직접 호출하지 않습니다.

| Method + Path | 인증 | 입력 | 출력·목적 |
|---|---|---|---|
| `POST /internal/ai/purge/workspaces` | `X-Internal-Token` | `workspace_ids`(1개 이상) | `200`: 워크스페이스 AI 데이터 전체 파기 |
| `POST /internal/ai/purge/users` | `X-Internal-Token` | `user_id` | `200`: 공유 워크스페이스에 남는 사용자 개인 AI 데이터 파기 |

- 응답 `deleted`는 테이블별로 지운 행 수와 `s3_objects`(삭제 요청한 S3 객체 수)다. CASCADE로 함께 지워진 자식 행은 세지 않는다.
- 여러 번 호출해도 결과가 같다. 이미 지운 범위는 0건으로 지나간다. 실패하면 `500`이며 호출자가 재시도한다.
- S3 객체를 먼저 지우고 DB 행은 워크스페이스·사용자 단위 한 트랜잭션으로 지운다. DB를 먼저 지우면 S3 실패 뒤 재호출할 때 지울 키를 찾을 수 없기 때문이다.
- 호출자는 해당 범위의 AI 작업 전송을 멈춘 뒤 호출한다. 실행 중이던 작업은 작업 기록(`ai_task_runs`)이 지워져 이후 변경이 거부된다.
- 파기한 workspace_id·user_id는 삭제보다 먼저 `ai_purged_scopes`에 남긴다. Kafka 재전달 등으로 그 범위의 작업이 다시 오면 task·ingest worker가 실행하지 않고 실패 이벤트로 확정하고(`document_deleted` command 포함), 편집 이벤트 consumer는 건너뛴다. 이 기록은 식별자만 담는다.
- 보관 예외: AI 실행 로그(`pipeline-runs/{run_id}/pipeline.log`)는 지우지 않는다(AI S3 역할에 삭제 권한이 없다). 로그에는 단계·ID·개수·소요 시간·오류 메시지만 있고, S3 30일 lifecycle로 만료된다. S3 이전 버전도 30일 뒤 만료되고(flatform lifecycle), 이미 발행된 Kafka 메시지는 최대 72시간 보관된다.
- `ai_model_usage`는 워크스페이스 사용량 정산에 쓰므로 어느 쪽에서도 지우지 않는다. 보관 기간은 법률 검토 뒤 정한다.

## 파기 범위

| 대상 | 워크스페이스 파기 | 사용자 파기 |
|---|---|---|
| 위키(`wiki_pages`와 링크·임베딩, `source_blocks`·스냅샷, `document_derived_state`, `wiki_source_tombstones`) | 지움 | 남김(공용) |
| 다른 워크스페이스가 참조하지 않게 된 `wiki_embedding_vectors` | 지움 | - |
| `pipeline_runs` | 지움 | 남김 |
| `wiki_schemas` | 지움 | 본인 것 지움 |
| 스킬(`skills`와 버전·출처) | 팀 스킬 지움 | 개인 스킬 지움 |
| 에이전트 기록(`agent_runs`와 계획·승인·작업·산출물, LangGraph checkpoint) | 지움 | 본인 것 지움 |
| 작업 기록(`ai_task_runs`·`ai_task_changes`) | 지움 | 본인 것 지움(상태 무관) |
| S3 객체 | `wiki/*/{workspace_id}/`(`wiki/` 아래 사용자 prefix를 S3 목록으로 찾음), `wiki/{workspace_id}/`, 에이전트 산출물. 실행 로그(`pipeline-runs/`)는 제외 | 본인 에이전트 산출물 |
| `ai_model_usage` | 남김 | 남김 |
| `ai_purged_scopes` | 워크스페이스 ID 추가 | 사용자 ID 추가 |

구현: `pipeline/app/modules/data_purge/`.

## 연동

### `POST /internal/ai/purge/workspaces`, `POST /internal/ai/purge/users`

- 인바운드 호출자: Access `DataPurgeRequestJob`(Document 파기 성공 후).
- 아웃바운드 호출: S3 객체 삭제와 AI DB 행 삭제.
- 미연동 표시: Access 호출 연결 대기([Fruition-access#29](https://github.com/FruitionKR/Fruition-access/issues/29)).

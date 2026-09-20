# AI 데이터 모델

스키마 원본은 `pipeline/db/ai_schema.sql`입니다. 다른 서비스의 DB를 직접 수정하지 않습니다.

### ai_db (ai-svc)

| 테이블 | 소유 | 용도 | 핵심 컬럼/관계 |
|---|---|---|---|
| wiki_schemas | ai-svc | 워크스페이스·사용자별 Wiki 생성 규칙 | active 스키마는 소유 범위당 최대 1개(부분 unique index) |
| document_derived_state | ai-svc | 문서 파생물 stale 추적 | `document.edit.event` consumer가 갱신 |
| wiki_pages·document_wiki_links·wiki_page_links | ai-svc | Wiki 현재 상태와 문서/페이지 관계 | workspace 범위 unique, DB 밖 document ID는 논리 참조 |
| source_blocks | ai-svc | 문서 block 텍스트 | 복합 PK `(block_id, document_id)` |
| pipeline_runs | ai-svc | pipeline 실행 상태 | Spring이 만든 `run_id`, `user_id`·`workspace_id` 보존. ingest manifest의 `post_ingest.status`는 `running/retrying/ready/needs_review` 품질 진단 상태를 보존 |
| wiki_page_embeddings·wiki_embedding_vectors·wiki_embedding_units | ai-svc | 검색용 embedding과 페이지 embedding 재처리 예약 | `wiki_page_embeddings.status`의 `pending`·`failed`는 maintenance worker가 재처리, page FK는 ai_db 내부, document ID는 논리 참조 |
| ai_task_runs·ai_task_changes | ai-svc | 비 Agent 작업·부모/자식·AI DB/객체 저장소 변경 기록 | command hash, 취소 상태, 역순 복구 기록 |
| skills·skill_versions·skill_version_sources | ai-svc | 개인·팀 Skill과 게시 version·생성 근거 | 개인은 `owner_user_id`, 팀은 `workspace_id`; 팀 권한은 access-svc 조회 |
| agent_runs·agent_plans·agent_plan_operations | ai-svc | Agent 실행·승인 대상 plan·operation | Markdown command는 Spring이 공급한 run ID와 envelope hash를 영속. 완료 `result`는 route를, 실패 `result`는 error code·예외 유형과 route 계약 교정 사유를 보존 |
| agent_approvals·agent_jobs·agent_tool_executions·agent_run_artifacts | ai-svc | 승인·lease/retry·Tool 멱등 실행·비동기 artifact | run/plan/operation FK, Tool 호출 수 40회 제한 |
| checkpoint_migrations·checkpoints·checkpoint_blobs·checkpoint_writes | ai-svc | LangGraph Agent 중단·재개 상태 | `PostgresSaver`가 `AI_DATABASE_URL`로 사용 |

Concept 본문 persistence는 ingest와 lint `materialize=true`가 같은 `(user_id, workspace_id)` PostgreSQL transaction advisory lock을 사용해 최종 object read-modify-write부터 DB commit까지 직렬화한다.

LLM provider/model은 workspace 설정 또는 chat/request에서 snapshot되어 command와 실행에 전달된다. API key는 DB·Kafka payload·log에 저장하지 않고 ai-svc secret env에서만 읽으며, 기존 AI 작업 로그 조회/결과 경로에는 LLM 설정 컬럼이 없다.

문서 편집 저장은 document-svc가 소유한 `core_db` PostgreSQL transaction에서 본문·편집 상태·write receipt·content version·asset/reference·Agent 적용 감사·`document_edit_outbox`를 함께 commit 또는 rollback한다. V39는 `document_edit_states`와 `document_content_versions`가 모두 빈 상태에서 시작하는 fresh cutover이며, 기존 Mongo 편집 데이터와 두 PostgreSQL table의 폐기는 대상별 승인을 전제로 한다. 기존 편집 데이터 import, fallback, dual-write를 사용하지 않는다. 결정 근거: [adr/0016](https://github.com/FruitionKR/Fruition-document/blob/main/docs/adr/0016-consolidate-document-body-into-postgres.md). S3/MinIO object upload는 transaction 밖이므로 실패·무변경 저장 시 업로드 호출자가 object를 정리한다. outbox publisher는 `created_at,event_id` 순으로 처리하고 첫 실패에서 cycle을 중단하며 현재 1 replica 전제를 둔다.

음성 API는 새 테이블을 만들지 않는다. 음성 bytes·실시간 미확정 전사는 요청/연결 안에서만
처리한다. 확정 전사와 원본 녹음의 영구 저장은 호출 서비스가 소유하며 AI는 회의록 초안을
반환한다. 연결 종료 전에 `completed` 구간을 저장하는 계약은 [음성 API](api/speech.md)를 따른다.

# ai-svc 내부 API

[서비스 문서](../README.md)

Query·Agent·Wiki·Skill pipeline의 서비스 간·운영 API다. 로컬 base URL은
`http://localhost:8000`이며 Gateway나 프론트엔드에서 직접 호출하지 않는다.

| 도메인 | API 수 | 현재 Gateway 연결 |
|---|---:|---|
| [Agent](agent.md) | 12 | turn은 Kafka, run·artifact 조회·Tool 인가는 내부 HTTP; artifact register는 현재 운영 호출자 없음 |
| [Wiki Ingest](pipeline.md) | 7 | ingest는 Kafka, run 상태는 내부 HTTP, 나머지는 운영용 |
| [Query](query.md) | 1 | 공개 동기·비동기 모두 Kafka, 이 HTTP는 호출자 없음 |
| [음성·회의록](speech.md) | HTTP 3 + WS 1 | 전사·실시간·회의록은 내부 HTTP/WS로 연결, 합성은 호출자 없음, 화면은 전부 미연동 |
| [Skills](skills.md) | 8 | 작업 실행·조회·설정은 내부 HTTP, draft·preview는 ai-svc 내부 기능 |
| [작업 취소·사용량](tasks.md) | 6 | 취소·상태·역순 복구와 모델 사용량 조회를 내부 HTTP로 연결, 호출 단위 조회는 호출자 없음 |
| [AI 데이터 파기](purge.md) | 2 | 내부 HTTP, Document 쪽 호출 연결 전 |
| [Wiki](wiki.md) | 10 | 조회·페이지 관리는 내부 HTTP, lint·복구는 Kafka |
| [Wiki Schema](wiki-schema.md) | 4 | 내부 HTTP |
| [임베딩 서버](embedding.md) | 2 | 별도 프로세스의 내부 HTTP, 배포 전 호출자 없음 |

Agent 승인 run 5개와 실행 결과 기반 Skill 초안 API는 `AGENT_SKILLS_ENABLED=true`,
나머지 Skill API 7개는 `SKILL_API_ENABLED=true`일 때 노출된다. 이 문서는 선택 기능을
모두 켠 전체 계약을 기준으로 한다.

## 연동 요약

각 API 문서는 `#### 10. 구현 파일` 바로 뒤에 `#### 연동` 소절을 두고 **인바운드 호출자 /
아웃바운드 호출 / 미연동 표시** 세 줄을 같은 형식으로 적는다. 10개 번호 항목은 그대로
유지하며 연동은 번호 없는 소절이다. 산문 형식인 [음성·회의록](speech.md)과
[작업 취소·사용량](tasks.md)·[AI 데이터 파기](purge.md)는 문서 끝에 같은 세 항목의 `## 연동` 절을 둔다.

읽는 방법: 인바운드 호출자는 ai-svc를 호출하는 document-svc의 Java client(또는 Kafka
producer), 아웃바운드 호출은 ai-svc가 다시 호출하는 대상이다. **호출자 없음**은 저장소를
읽어 확인한 결과이고, 코드에서 확정하지 못한 경우에만 **호출자 미확인**으로 적는다.

### 전제

- ai-svc는 Gateway에 노출되지 않는다. 모든 라우터는 `pipeline/api.py`에서
  `include_internal_router(..., internal_token_dependencies)`로 등록되고 같은 함수가
  `INTERNAL_TOKEN_ROUTE_PATTERNS`를 채워 middleware 검사 대상을 맞춘다.
  `/agent/runs/**`·`/skills/**`는 `X-Agent-Service-Token`만으로 보호한다.
- document-svc의 ai-svc 호출은 대부분 `fruition.shared.http.PipelineClientFactory`가
  만든 `RestClient`(기본 헤더 `X-Internal-Token`)를 쓴다.
  `PipelineSkillRequester`와 `PipelineAgentRunStatusRequester`의 autonomous run client만
  `X-Agent-Service-Token`을 쓴다.

### 인바운드 (document-svc → ai-svc)

Java 경로는 Fruition-document `src/main/java/fruition/` 기준이다.

| ai-svc API | 호출자 | 비고 |
|---|---|---|
| `POST /query` | **호출자 없음** | `app.query.endpoint`는 선언만 있고 읽는 코드가 없다 |
| `POST /agent/turn` | **HTTP 호출자 없음** | Kafka `ai.agent.command`(`core/agent/service/AgentTurnService.java`:131) |
| `GET /agent/runs/{run_id}`, `/approve`, `/reject`, `/cancel`, `/revise` | `core/agent/repository/PipelineAgentRunStatusRequester.java` | `app.agent.run-endpoint` |
| `GET /internal/agent/runs/{run_id}` | 같은 파일 118-120 | `app.agent.status-endpoint` |
| `POST /internal/agent/runs/tool-authorizations/{read,execute}` | `core/agent/repository/PipelineAgentToolAuthorizationClient.java`:34,39 | |
| `POST /internal/agent/runs/artifacts/{list,resolve}` | `core/agent/repository/PipelineAgentArtifactClient.java`:39,59 | |
| `POST /internal/agent/runs/artifacts/register` | **호출자 없음** | |
| `POST /pipeline/runs`, `/pipeline/reingest-runs`, `/chat-wiki/runs` | **HTTP 호출자 없음** | Kafka `ai.ingest.command`(`core/document/repository/IngestCommandOutbox.java`:56) |
| `GET /pipeline/runs/{run_id}` | `core/document/repository/PipelineRunStatusRequester.java`:30 | |
| `GET /pipeline/runs/{run_id}/logs` | **호출자 없음** | |
| `GET /documents/{document_id}` | **호출자 없음** | 방향이 반대다(아래 아웃바운드 참고) |
| `GET /health` | 서비스 간 호출자 없음 | probe 전용 |
| `GET /wiki/graph`, `POST /wiki/pages/lookup`, `GET /wiki/pages/{page_id}`, `GET /wiki/documents/{document_id}/context`, `DELETE /wiki/workspaces/.../documents/...`, `GET /wiki/workspaces/.../last-updated` | `core/wiki/repository/PipelineWikiStateRequester.java` | `app.wiki-state.endpoint` |
| `PATCH /wiki/pages/{page_id}/rename` | `core/wiki/repository/PipelineWikiPageRequester.java`:37 | `app.wiki-page.endpoint` |
| `POST /wiki/maintenance/lint`, `/wiki/ingest-restore-runs`, `/wiki/lint-restore-runs` | **HTTP 호출자 없음** | Kafka `ai.maintenance.command`(`core/wikimaintenance/service/WikiMaintenanceService.java`:66, `core/aihistory/service/RestoreExecuteService.java`:132) |
| `POST /wiki-schema/preview`, `/drafts`, `/{schema_id}/activate`, `GET /wiki-schema/active` | `core/wikischema/repository/PipelineWikiSchemaRequester.java`:31,35,39,44-50 | 순수 passthrough |
| `POST /skills/tasks`, `GET /skills`, `GET /skills/{id}`, `DELETE /skills/{id}`, `POST /skills/{id}/{enable,disable}` | `core/skill/repository/PipelineSkillRequester.java` | `app.skill.endpoint` |
| `POST /skills/preview`, `POST /skills/draft-from-runs/preview` | **호출자 없음** | |
| `POST /speech/transcriptions` | `core/speech/SpeechTranscriptionClient.java`:31-38, `core/meeting/MeetingTranscriptionWorker.java`:69-72 | |
| `POST /speech/synthesis` | **호출자 없음** | 중계 설정조차 없다 |
| `WS /speech/transcriptions/live` | `core/meeting/MeetingLiveHandler.java`:146-152 | |
| `POST /meeting-notes/preview` | `core/meeting/MeetingNotesClient.java`:32 | |
| `POST /internal/ai/tasks/{run_id}/cancel`, `GET /internal/ai/tasks/{run_id}`, `POST .../rollback-backend`, `POST /internal/ai/tasks/documents/{document_id}/cancel` | `core/aitask/repository/PipelineTaskCancellationClient.java`:26,38-40,32,45 | |
| `GET /usage/models` | `core/usage/service/ModelUsageService.java`:35-40 | |
| `GET /internal/model-usage/calls` | **호출자 없음** | Document 크레딧 과금 구현 전 |
| `POST /internal/ai/purge/workspaces`, `POST /internal/ai/purge/users` | **호출자 없음** | Document 파기 API 구현 전 |

### 아웃바운드 (ai-svc → 외부)

| 대상 | 경로 | ai-svc 구현 |
|---|---|---|
| document-svc (`DOCUMENT_INTERNAL_BASE_URL`) | `GET /internal/documents/{document_id}/pipeline-source` | `app/modules/wiki_ingestion/infrastructure/backend_document_reader.py`:19 |
| document-svc (`DOCUMENT_INTERNAL_BASE_URL`) | `POST /internal/wiki/contributions` | 같은 파일 52 |
| document-svc (`DOCUMENT_INTERNAL_BASE_URL`) | `POST /internal/agent/skill-authoring/references/read` | `app/modules/skill/infrastructure/backend_skill_reference_reader.py`:27 |
| document-svc (`AGENT_BACKEND_URL`, `X-Agent-Service-Token`) | `POST /internal/agent/tools/read/{tool_name}`, `POST /internal/agent/tools/execute/{tool_name}` | `app/modules/agent_run/infrastructure/backend_tool_gateway.py`:27,51 |
| document-svc (`AGENT_BACKEND_URL`, `X-Agent-Service-Token`) | `POST /internal/agent/tools/rollback/{run_id}/changes`, `.../changes/{change_id}`, `.../rollback/{run_id}/finalize-edits` | `app/modules/task_cancellation/infrastructure/backend_rollback.py`:12,18,20 |
| access-svc (`ACCESS_INTERNAL_BASE_URL`) | `GET /internal/authz/workspaces/{workspace_id}/users/{user_id}` | `app/modules/skill/infrastructure/workspace_authorization.py`:19 |
| OpenAI | `/v1/audio/transcriptions`, `/v1/audio/speech`, `wss://.../v1/realtime?intent=transcription` | `app/modules/speech/infrastructure/openai_speech.py`:40,65,97 |
| OpenAI · Gemini · Claude | ChatCompletions / Responses / messages | `app/modules/wiki_generation/infrastructure/chat_completions_llm.py`, `app/modules/document_restoration/infrastructure/selective_repair_with_provider.py`:55,57,59 |
| Tavily | `POST https://api.tavily.com/search` | `app/modules/query/infrastructure/web_search.py`:14,73 |

`DOCUMENT_INTERNAL_BASE_URL`·`AGENT_BACKEND_URL`·`ACCESS_INTERNAL_BASE_URL`은 서로 다른
설정이다. `AGENT_BACKEND_URL`만 기본값(`http://document-svc:8080`)이 있고 나머지는
미설정이면 해당 기능이 `RuntimeError`로 실패한다.

### Kafka

| 토픽 | 방향 | ai-svc 구현 | document-svc 발행 |
|---|---|---|---|
| `ai.ingest.command` | 수신 | `app/workers/ingest_worker.py`:52 | `core/document/repository/IngestCommandOutbox.java`:56,73 |
| `ai.query.command` | 수신 | `app/workers/task_worker.py`:82 (`AI_COMMAND_TOPIC`) | `core/query/service/QueryRunService.java`:74 |
| `ai.agent.command` | 수신 | 같은 worker를 다른 토픽으로 배치 | `core/agent/service/AgentTurnService.java`:131 |
| `ai.maintenance.command` | 수신·발행 | `app/workers/task_worker.py`, ingest 후속은 `ingest_worker.py`:56 | `core/wikimaintenance/service/WikiMaintenanceService.java`:66, `core/aihistory/service/RestoreExecuteService.java`:132 |
| `ai.task.event` | 발행 | `app/workers/{ingest,task}_worker.py` | 소비: `core/aitask/service/AiTaskResultConsumer.java` |
| `document.edit.event` | 수신 | `app/workers/edit_event_consumer.py`:34 | `core/document/service/PostgresDocumentEditOutboxPublisher.java`:69 |

`task_worker.py`는 하나의 실행 파일이고 `kind`(`query`·`agent`·`lint`·`post_ingest`·
`restore_ingest`·`restore_lint`)로 분기한다(`app/workers/task_worker.py`:824-849).
document-svc는 모든 AI command를 Postgres outbox에 쓰고
`core/document/service/AiCommandOutboxPublisher.java`:48이 발행한다.

### Feature flag 주의

`pipeline/api.py`:35-44 기준으로 `AGENT_SKILLS_ENABLED`의 기본값은 `false`,
`SKILL_API_ENABLED`의 기본값은 `true`다. 이 저장소에는 배포 매니페스트가 없으므로 운영의
실제 값은 확인할 수 없다. **flag로 꺼진 route는 "미연동"과 다르다** — 호출자는 있으나
노출되지 않을 수 있다. `/agent/runs/**`와 `POST /skills/draft-from-runs/preview`는
기본값에서 노출되지 않지만 `/agent/runs/**`에는 document-svc 호출자가 있다.

### 진행 중

`GET /wiki-schema/drafts`(query `workspace_id`, `user_id`; 응답 `{"wiki_schemas": [...]}`)는
다른 작업자가 추가하는 중이며 `origin/main`에는 없다. 이 문서의 연동 표에도 넣지 않았다.

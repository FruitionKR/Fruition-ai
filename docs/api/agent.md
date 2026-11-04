# Agent API

[서비스 문서](../README.md) / [ai-svc](README.md)

Agent run·artifact·Tool 인가 내부 API다. 공개 Gateway 계약은
[`document-svc Agent API`](https://github.com/FruitionKR/Fruition-document/blob/main/docs/api/agent.md)다. Agent turn은 Kafka `ai.agent.command`로만 실행하며(동기 `POST /agent/turn`은 호출자가 없어 없앴다),
`/agent/runs/**`와 artifact 조회·Tool 인가·상태 조회는 Backend가 내부 HTTP로 호출한다.
`POST /internal/agent/runs/artifacts/register`는 현재 저장소 안에 운영 호출자가 없다.

`ai.agent.command`의 선택 키 `folder_id`(document-svc가 검증한 폴더 UUID)는 작업 대상 폴더다. 지정하지 않은 턴은
키를 싣지 않으며 기존처럼 워크스페이스 전체·최상위(루트)를 대상으로 한다. 지정하면 다음과 같이 쓴다.

- 이 턴이 만든 승인형 run(`folder_organize`·`workspace_workflow`)에 `target_folder_id`로 저장하고 `AgentRunResponse`에 돌려준다.
- 계획 단계는 계층 스냅샷을 그 폴더와 하위 항목으로 줄여 모델에 주고, 모든 작업이 그 안에 머물게 한다. 최상위로 옮기는 작업이 나오면 계획을 거절한다.
- 위치를 정하지 않은(최상위) `create_folder`·`create_document`는 그 폴더 안에 만든다(`parent_folder_id`·`folder_id`와 `destination_parent_id`).
- 결과 방식이 `create`인 Skill은 열린 문서 없이도(루트·폴더만 고른 턴) 새 문서 초안(`markdown_create`)이나 저장 계획(`workspace_workflow` + `create_document`)으로 실행한다. 초안 문서는 document-svc·클라이언트가 같은 `folder_id`로 만든다.

### 작업 탭 안내 종류(`notice_kind`)

라우트의 선택 항목 `notice_kind`는 사용자에게 무엇을 알릴지 정한다. 없으면(`null`) 기존 문구를 쓴다.

| action | notice_kind | 결과 `message` |
|---|---|---|
| `clarify`(작업 탭, 문서 동작 없음, Skill 후보 없음) | `chat_tab` | "작업 탭에서는 … 채팅 탭에서 해 주세요."(첨부 없는 답변·잡담) |
| | `details` | 작업은 분명하지만 사용자의 말로 결과가 정해지지 않을 때 LLM(`prompts/task_clarification.system.md`)이 만든 질문 한 문장. 실패하면 "작업에 필요한 내용을 조금 더 알려 주세요." |
| | `web_disabled` | "웹 검색이 꺼져 있어요. 입력창에서 웹 검색을 켠 뒤 다시 요청해 주세요."(`allow_web_search`가 꺼져 있을 때만) |
| `clarify`(작업·채팅 탭, 문서 동작 없음, 첨부 있음) | `attachment_target` | "첨부한 문서 중 어느 문서를 고칠까요? …"와 번호를 붙인 제목 목록 |
| `reject` | `sensitive_data`·`unsupported`·`policy` | 민감한 값·워크스페이스 밖 작업·규칙 우회별 거절 이유 |
| | `display_unsupported` | "표 너비·글꼴·글자 색처럼 화면에 보이는 모양은 Markdown에 저장되지 않아 AI가 바꿀 수 없어요."(작업 탭·채팅 탭 모두. 표 열 정렬처럼 Markdown으로 나타낼 수 있는 변경은 편집) |
| `markdown_edit`·`markdown_create`·`workspace_workflow` | `skill_off` | 결과 메시지 끝에 "스킬 사용이 꺼져 있어 스킬 없이 처리했어요."(`skill_mode=off`일 때만) |

- 실행기가 찾을 수 있는 정보(열린 문서 내용, 설명에 맞는 문서, 폴더 위치)는 묻지 않는다. 되물은 뒤 사용자가 판단을 맡기면 원래 작업을 진행한다.
- 첨부(`reference_document_ids`)가 없는 작업 탭에서 답변 갈래(`chat_answer`·`conversation_reply`)가 나오면 `clarify` + `chat_tab`으로 바꾼다.
- 첨부가 있는 작업 탭은 답변 갈래를 그대로 실행한다. 첨부 UI가 작업 탭에만 있어 채팅 탭 안내로는 답을 얻을 수 없기 때문이다. 라우터가 그래도 `clarify`(`chat_tab` 또는 안내 종류 없음, 문서 동작 없음, Skill 후보 없음)를 고르면 `chat_answer`(`retrieval_source=workspace`)로 바꾼다. 응답·기록은 채팅 탭의 답변 갈래와 같다. 첨부를 읽지 못하면 답변하지 않고 읽기 실패 이유를 담은 `clarify`로 끝나며 `chat_tab`이 아니다.
- Jev 라우팅도 같은 계약에서 선택지를 만든다(작업 탭 clarify 3종, reject 4종, `skill_mode=off`일 때 문서 작업의 `skill_off`). 첨부 턴은 LLM 라우터가 처리하므로 `attachment_target`은 Jev 선택지에 없다.

### 되묻기 이유 코드(`clarify_reason`)

`action=clarify` 결과의 선택 항목이다. 화면이 `message` 문구와 비교하지 않고 갈래를 정하도록 추가했고, 코드가 없는 되묻기와 다른 action은 `null`이다.

| clarify_reason | 뜻 | `message` |
|---|---|---|
| `markdown_document_required` | 편집 요청인데 요청에 편집할 Markdown 문맥이 없다. 문서를 열지 않았거나 화면이 문맥을 보내지 않은 경우(보기 권한 문서 등)다. AI는 권한을 모르므로 이유는 화면이 자신의 권한 정보와 함께 판단한다. | "수정할 Markdown 문서를 연 뒤 다시 요청해 주세요."(첨부 문서가 있으면 "어느 문서를 고칠지 알 수 없어요. 첨부한 문서를 고치려면 \"첨부한 문서를 고쳐 줘\"처럼 말해 주시고, 다른 문서를 고치려면 그 문서를 연 뒤 다시 요청해 주세요.". 편집 버전을 주지 않는 구 document-svc에서 첨부 수정을 요청하면 "첨부한 문서는 참고 자료로만 읽어요. 첨부한 문서를 고치려면 그 문서를 연 뒤 다시 요청해 주세요.") |
| `attachment_target_required` | 첨부 중 어느 문서를 고칠지 정할 수 없다. 화면은 첨부 칩을 유지한다. | "첨부한 문서 중 어느 문서를 고칠까요? …"와 번호를 붙인 제목 목록 |

### 첨부 참고 문서(`reference_document_ids`)

`ai.agent.command`의 선택 키 `reference_document_ids`(문자열 배열, 최대 3개)는 사용자가 이 turn에 첨부해 document-svc가 검증한 문서다. 구 document-svc는 키를 싣지 않으며, 키가 없거나 `null`·빈 배열이면 기존과 같다. 첨부는 근거로 쓰고, 라우터가 수정 대상으로 고른 첨부만 고친다.

- 본문은 Skill 참조 문서와 같은 document-svc `POST /internal/agent/skill-authoring/references/read`(`X-Agent-Service-Token`)로 라우팅 뒤 필요한 갈래에서만 읽는다. 일반 turn은 선택 필드 `max_chars`를 보내고 응답의 `truncated`로 잘렸는지 안다. 원본(ORIGINAL) 문서는 AI DB의 `source_blocks`에서 읽고 여기서 자른다. Skill 작성 경로는 `max_chars`를 보내지 않으며 동작이 같다.
- 글자 수: 문서당 40,000자, 합계 80,000자(Skill 경로 상한과 같다). 답변 갈래(`chat_answer`·`conversation_reply`)는 입력이 근거 8개(7,200자)를 전제로 하고 재작성·평가로 반복되므로 합계 30,000자(열린 문서 상한과 같다)만 읽는다. 남은 합계를 남은 문서 수로 나눈 값을 문서별 `max_chars`로 쓰므로 앞 문서가 짧으면 남은 양이 뒤 문서로 넘어간다.
- 제목: 응답에 선택 필드 `display_name`이 있으면 줄바꿈·제어 문자를 정리해 한 줄(최대 200자)로 만들어 `reference_documents[].title`과 답변 근거의 `document_title`에 싣는다. 구 document-svc처럼 없으면 싣지 않는다. 제목도 신뢰하지 않는 데이터다.
- 라우터는 `reference_documents.count`와 `titles`만 받는다(본문·id 없음). 라우팅 전에 문서마다 `max_chars=1`로 읽어 제목을 정리한다. 읽지 못한 문서는 빈 제목으로 두어 순번을 유지하고, 실패 안내는 본문을 읽는 단계가 정한다. Jev에는 보내지 않고 첨부 턴은 LLM 라우터가 처리한다.

| 갈래 | 첨부 사용 |
|---|---|
| `chat_answer`(작업 탭 포함) | 문단을 900자 이하 조각으로 나눠 위키 근거 앞에 고정 근거로 넣는다. `evidence_snippets[].source_document_id`가 첨부 문서이고, 원본 문서는 `source_block_ids`·`source_refs`가 실제 블록을, 편집 문서는 빈 배열이다(블록이 없다). 위키에 근거가 없어도 답한다(`stop_reason=reference_documents`). |
| `conversation_reply`(작업 탭 포함) | `reference_context.reference_documents`. 인용 없음. |
| `markdown_create`·`workspace_workflow`(create) | `reference_context.reference_documents`. 인용 흐름이 없어 본문에 근거 번호를 달지 않는다. 열린 문서 없이 동작한다. |
| `markdown_edit`·`workspace_workflow`(edit), `attached_edit_targets` 없음 | 열린 문서(`active_markdown_context`)만 편집하고 첨부는 `reference_context.reference_documents`로 준다. 이미 만든 미리보기를 저장할 때는 읽지 않는다. |
| `workspace_workflow`(edit), `attached_edit_targets` 있음 | 아래 [첨부 문서 수정](#첨부-문서-수정route-attached_edit_targets)을 따른다. |
| 그 밖(`folder_organize`·`folder_lookup`·`skill_authoring`·`skill_draft_proposal`·`reject`, 작업 탭 `clarify`) | 읽지 않는다. |

라우터는 첨부 질문과 대상을 말하지 않은 "요약해 줘"를 `chat_answer`로, "요약해서 새 문서로 만들어 줘"를 `markdown_create`로, 열린 문서를 지목한 "열린 문서를 요약해 줘"(작업 탭)를 `markdown_edit`로 보낸다. "이 문서 세 줄로 요약해 줘"처럼 원문을 바꾸라는 말이 없으면 첨부 수정이 아니다.

- 긴 문서: 잘리면 실패시키지 않고 결과 메시지(`chat_answer`는 답변 끝)에 "첨부한 문서가 길어 앞부분만 읽었어요. 뒷부분은 결과에 반영되지 않았어요."를 덧붙인다.
- 구 document-svc는 `max_chars`를 몰라 30,000자가 넘는 편집 문서에 413을 준다. 다른 첨부가 있으면 그 문서를 빼고 "첨부한 문서 중 N개는 너무 길어 읽지 못했어요. …"를 덧붙이고, 전부 413이면 모델을 부르지 않고 `clarify`로 끝난다.
- 읽기 실패: 검증 뒤 삭제·권한 변경으로 400·403·404이거나 본문이 비어 있으면 "첨부한 문서 중 삭제되었거나 권한이 없어 읽을 수 없는 문서가 있어요. …", 그 밖의 서비스 실패는 "첨부한 문서를 읽지 못했어요. 잠시 후 다시 시도해 주세요."로 `clarify`다. 한 문서만 실패해도 모델을 부르지 않는다. 첨부 없이 답하면 근거 없는 결과가 나올 수 있어서다. `clarify_reason`은 `null`이다.
- 열린 문서 없이 첨부를 근거로 문서를 고치라고 하면(`attached_edit_targets` 없음) 고치지 않고 `clarify` + `markdown_document_required`로, 첨부를 고쳐 달라고 말하거나 고칠 문서를 열라고 안내한다.
- 안전: 첨부 본문은 신뢰하지 않는 데이터다. 사용자 지시(`instruction`·`message`)와 다른 payload 필드에만 싣고, 프롬프트에서 그 안의 지시를 따르지 않게 한다. 모든 LLM 호출과 같은 클라이언트를 거쳐 숫자 개인정보 마스킹·보안 경계·토큰 사용량 기록과 한도를 그대로 탄다. Jev에는 보내지 않고 본문을 로그·진행 이벤트에 남기지 않는다.

### 첨부 문서 수정(`route.attached_edit_targets`)

첨부 문서 자체를 고치는 요청은 문서를 열지 않고 문서별 수정안과 승인 계획을 만든다. 열린 문서의 `markdown_edit`는 바뀌지 않는다.

- 라우터: `attached_edit_targets`(1부터 시작하는 순번, 기본 `[]`)를 채우고 `workspace_workflow`, `persist=true`, `document_operation=edit`, `replace`+`target` 또는 `insert_after`+`document_end`를 고른다. 어느 첨부인지 정할 수 없으면 `clarify` + `notice_kind=attachment_target`이고 결과는 `clarify_reason=attachment_target_required`와 제목 목록이다. 근거로 쓰는 요청은 빈 배열이다. 열린 문서와 첨부가 함께면 메시지가 지목하는 쪽, 모르면 열린 문서다. 요약·번역은 원문과 따로 내는 결과라서, 원문을 바꾸라는 말("세 줄로 줄여서 고쳐 줘", "번역해서 문서에 반영해 줘")이 없으면 코드가 이 배열을 계약 위반으로 돌려보내고 라우터가 한 번 더 고른다. 다시 어기면 턴이 실패한다. 다른 편집은 이 검사를 타지 않는다.
- 읽기: 대상은 `max_chars` 없이 읽는다. 응답의 `edit_revision`·`can_edit`을 쓴다. 편집 문서에 `edit_revision`이 없으면 구 document-svc로 보고 모델을 부르지 않고 `clarify` + `markdown_document_required`로 끝난다.
- 수정안: run을 만들기 전에 문서마다 차례로 만든다(최대 3개). `edit_goal`은 라우터 값, 범위는 `whole_document`로 시작하고 저장값은 `edit.actual_target`이다. 대상이 아닌 첨부만 `reference_context.reference_documents`로 준다. 검색(`retrieval_source`)은 턴에 한 번만 한다. 모델 호출은 턴의 사용량 scope에 기록된다.
- 저장: Markdown과 미리 보기 JSON을 object storage에, 메타데이터를 `agent_run_artifacts`에 둔다. `expires_at`은 1일, 객체당 5MiB다. 본문은 로그와 진행 이벤트에 남기지 않는다. 만료 뒤 정리는 아래 [만료된 수정안 정리](#expired-artifact-cleanup)를 따른다.
- 계획: 미리 보기가 있는 artifact가 있는 `workspace_workflow`는 트리를 읽거나 모델을 부르지 않고 `apply_document_edit` 작업을 첨부 순서대로 만든다. `reason`은 수정안 요약, `summary`는 "아직 바꾸지 않았습니다. 승인하면 문서 N개를 수정합니다."다. 선택한 Skill의 `allowed_tools`는 그대로 검사하고, `depends_on`은 비어 있으며, run의 `target_folder_id`는 `null`이다. 큰 변경에 대한 경고는 만들지 않는다. 계획 전에 수정안이 만료됐으면 job이 실패한다. `revise`도 같은 수정안으로 다시 계획하므로 지시가 내용에 반영되지 않고, 수정안이 만료됐으면 `409`다.

계획에서 빠지는 문서는 나머지를 진행하고 결과 메시지에 제목과 이유를 덧붙인다.

| 이유 | 안내 | 남은 문서가 없을 때 |
|---|---|---|
| `can_edit=false`, 읽기 400·403·404, 원본 문서 | 편집 권한이 없어 제외 | 권한 문제뿐이면 `clarify` "편집 권한이 없어요. …", `clarify_reason=null` |
| 413, 30,000자 초과, 결과가 5MiB 초과 | 너무 길어 수정하지 못함 | `clarify` + 그 안내 |
| 본문이 비어 있음 | 내용이 없어 수정하지 못함 | `clarify` + 그 안내 |
| `changed=false` 또는 결과가 원문과 같음 | 바꿀 내용이 없어 계획에서 뺌 | 변경 없음뿐이면 `clarify` + `NO_CHANGES_MESSAGE` |
| 수정안 생성 실패 | 수정안을 만들지 못함 | 턴 실패(일반 편집과 같음) |

읽기 서비스 오류(5xx 등)와 근거 첨부를 읽지 못한 경우는 기존 첨부 안내로 `clarify`다.

실행 결과는 작업마다 정해지고 한 작업이 실패해도 나머지는 계속한다.

| 상황 | 작업 상태 | `error_code` |
|---|---|---|
| 계획 뒤 겹치지 않는 변경 | `succeeded`(document-svc가 병합) | |
| 겹치는 변경 | `conflicted` | `tool_http_409` |
| 다른 사람이 편집 중 | `failed`(재시도 없음) | `tool_http_423` |
| 권한 회수 | `forbidden` | `tool_http_403` |
| 문서 삭제 | `failed` | `tool_http_404` |
| 승인 전에 문서를 영구 삭제 | `skipped` | `document_purged` |
| 실행 전 스냅샷이 맞지 않음 | `failed` | `rollback_snapshot_invalid` |

run 상태는 `finish_run_from_operations`가 정한다. 겹침이 하나라도 있으면 다른 문서가 성공했어도 `conflicted`이고, 그 밖의 실패가 섞이면 `partial_failed`, 전부 실패면 `failed`다. 롤백 스냅샷은 `apply_document_edit`의 버전을 비교하지 않는다. 비교하면 계획 뒤 변경이 document-svc 병합에 닿지 못한다. 스냅샷을 읽지 못한 작업은 요청을 보내지 않았으므로 롤백에서 건너뛴다.

<a id="expired-artifact-cleanup"></a>
#### 만료된 수정안 정리

- agent worker가 1시간마다(시작 직후 포함) `expires_at < now() - AGENT_ARTIFACT_CLEANUP_GRACE_SECONDS`(기본 86400초, 음수면 기동 실패)인 artifact의 Markdown·미리 보기 객체와 행을 지운다. 한 주기 100건이고, 100건을 다 지우면 60초 뒤 다시 돈다. 로그에는 건수와 artifact id만 남긴다.
- `expires_at IS NULL`(되돌리기에 쓴 artifact)과 `queued`·`planning`·`executing`·`verifying`·`cancel_requested`·`rolling_back` run의 artifact는 지우지 않는다. 승인 뒤 실행까지 끝난 수정안은 `expires_at`이 그대로라 끝난 뒤 함께 지워진다. 되돌리기는 실행 전 스냅샷을 쓰므로 이 수정안이 필요 없다.
- 여러 worker가 겹쳐도 `FOR UPDATE SKIP LOCKED`로 한 번만 처리한다. 승인·`revise`와는 run 행 잠금으로 겹치지 않는다. 진행 중인 승인이 있으면 그 run을 건너뛰고, 정리가 먼저면 승인은 기다렸다가 `409`를 받는다.
- 객체 삭제에 실패하면 행을 남겨 다음 주기에 다시 시도한다. 없는 객체는 성공으로 본다. 한 주기에 3건 실패하면 저장소 장애로 보고 그 주기를 끝낸다. 키가 `agent-runs/{sha256(run_id)}/` 밖이면 객체도 행도 건드리지 않고 오류 로그를 남긴다.
- 버킷은 버전 관리 중이라 삭제는 현재 버전을 지우는 것이다. 이전 버전은 S3 lifecycle(flatform ADR 0023)로 30일 뒤 사라진다.
- 휴지통 이동(`document_deleted`)은 복구할 수 있어 수정안을 지우지 않는다. 문서를 영구 삭제하면 아래 절차로 바로 지운다.

<a id="document-purge"></a>
#### 문서 영구 삭제(`document_purged`)

document-svc `DocumentTrashPurgeWorker`가 문서 행을 지우는 트랜잭션에서 outbox로 보낸다. ingest worker가 처리하고 run·plan·operation·실행 행은 이력으로 남긴 채 본문이 든 값만 지운다. 워크스페이스 파기는 이 command 없이 `purge_workspace`가 통째로 지운다.

| 대상 | 처리 |
|---|---|
| `agent_tool_executions`의 되돌리기 스냅샷(`undo:*`) | `response_metadata`를 `{"document_purged": true}`로 바꾼다 |
| `agent_tool_executions`의 실행 결과 | `id`·`document_id`·`current_version`만 남긴다 |
| `agent_run_artifacts`(수정안·미리 보기·`undo-*`·이 문서를 만든 `create_document`)와 객체 | 키 단위로 객체를 지우고 행을 지운다. 같은 run의 다른 문서는 건드리지 않는다 |
| `agent_plan_operations` | `apply_document_edit`·`create_document`의 `reason`을 안내 문구로 바꾸고, `pending`이면 `skipped`(`document_purged`)로 둔다 |
| `agent_runs.result`(이 문서의 `markdown_turn`) | `edit`·`generated_markdown`을 뺀다 |
| 근거 문장(`ai_task_runs.result`, `agent_runs.result`, `pipeline_runs.manifest`, `document_quality_reviews.evaluations`) | `source_document_id`가 이 문서인 항목의 `text`·`expected_claims`만 비운다 |
| `ai_task_runs`(편입 kind) | `command`의 `input_markdown`·`input_blocks`를 빼고 `result`를 `{"scrubbed": true}`로 바꾼다 |
| `pipeline_runs.manifest`(이 문서의 run. post_ingest는 `document_id`가 없어 같은 id의 `ai_task_runs.command`로 찾는다) | `{"scrubbed": true}`로 바꾼다 |
| `ai_task_changes` | 이 문서·소스 페이지·이 문서 run의 변경 기록을 지운다(소스 페이지·소스 객체 기록 포함). 작업 취소가 그 값을 되살리지 않는다. 다른 문서 run의 `pipeline_runs`·`document_quality_reviews` 기록은 이 문서의 근거만 비운다 |
| `wiki/{ws}/clusters/active.md`·`archived.md`, `wiki/{ws}/logs/*.md`와 그 객체 변경 기록 | 이 문서를 인용한 근거 줄과 이 문서의 편입 항목만 뺀다. 근거가 없어진 클러스터는 지우고, 다른 문서가 남은 클러스터의 대표명·승격 사유는 남긴다. 변경 기록에도 같은 변환을 적용해 작업 취소의 대조가 맞고 본문이 되살아나지 않는다. 개념 잠금을 5초 안에 못 잡으면 다음 주기로 넘긴다 |
| `pipeline_runs.manifest` | `{"scrubbed": true}`로 바꾼다 |
| 소스 페이지 객체(`wiki/{ws}/sources/{document_id}.md`, `wiki/{ws}/pages/{page}/`), `wiki_pages.summary`·`markdown_uri` | 객체를 지우고 요약은 빈 값, `markdown_uri`는 NULL로 둔다. 행은 `deleted`로 남긴다. 위키 조회·그래프·임베딩은 활성 페이지만 읽고, 로그 되돌리기는 이 페이지를 건너뛰고 개념 페이지를 복구한다 |
| `source_blocks`·`source_block_snapshots`·`document_quality_reviews`·`document_derived_state` | 행을 지운다 |

- core_db의 소스 페이지 버전 본문은 document가 지운다. core_db에는 페이지 종류가 없어 document만으로는 소스 페이지를 가릴 수 없으므로, AI가 결과 event(`document_purge`)의 `payload.source_page_ids`로 알린다.
- 남기는 것: 사용자 메시지(`request_summary`), AI 답변, 여러 문서 파생(개념 페이지·개념 기여, 클러스터), Skill 본문, `ai_model_usage`.
- 취소 API(`/internal/ai/tasks/{run_id}/cancel`)는 document가 본문을 지운 command(`document_purged: true`)를 받으면 해시 대조와 등록을 건너뛴다. 실행이 없으면 `409`다.
- `document_deleted`가 처리되지 못해 소스 페이지가 아직 활성이면 같은 정리를 먼저 돌린다. 개념 잠금 시간 초과는 실패로 기록하고 다음 주기에 다시 시도한다.
- 스캔 뒤에 결과를 쓸 수 있는 run이 있으면 끝내지 않고 다음 주기에 다시 훑는다. 다른 문서의 run도 질의·편입·lint로 이 문서의 근거를 읽으므로 문서 id로 좁히지 않는다. 위키를 읽지 않는 `convert_batch`·`meeting_notes`·`meeting_transcription`은 기다리지 않는다. 요청 뒤 하루가 지나면 기다리지 않고 그 시도의 스캔으로 끝내며(그때 아직 도는 run은 경고 로그에 건수를 남긴다), 그 뒤 이 run들이 쓰는 근거는 AI DB에 남을 수 있다. document의 `scrubOrphans`는 core_db만 정리해 이 값을 지우지 않는다.
- `ai_document_purges`에 요청을 먼저 커밋한다. 객체 삭제나 다른 처리가 실패해도 offset은 넘어가고, `finished_at`이 빈 행을 agent worker가 1시간마다 오래 못 본 것부터 다시 시도한다(한 주기 20건, 3건 실패하면 그 주기 종료). 재전달해도 결과가 같다. 키가 `agent-runs/{sha256(run_id)}/` 밖이면 지우지 않고 오류 로그를 남기며 대기로 둔다. 로그에는 건수와 id만 남긴다.
- 지운 뒤 승인: 나머지 문서는 그대로 승인·실행하고 run은 `partial_failed`로 끝난다. 실행할 작업이 영구 삭제 문서뿐이면 `409`다. `revise`도 수정안이 없어 `409`이고, 미리 보기는 나머지 문서만 돌려준다.
- 지운 뒤 되돌리기: 그 문서 단계는 document-svc를 부르지 않고 건너뛰고 나머지 문서는 되돌린다. 끝에 run은 `rollback_failed`(`rollback_document_purged`)로 닫히고 `finished_at`을 채운다. 다시 요청해도 결과가 같고, 다른 `rollback_failed`와 달리 90일 정리 대상이다. 상위 `markdown_turn`도 같은 코드를 받는다.

배포 전 `migrate_ai_schema`로 `ai_document_purges`를 만들어야 한다. 이 표가 없으면 ingest worker와 agent worker의 스키마 확인이 실패한다. `last_scan_at` 열이 없으면 준비 상태 확인은 통과하지만(테이블만 본다) 영구 삭제 처리가 실패한다. 같은 명령이 `ai_task_runs`의 인덱스를 `CONCURRENTLY`로 만들고, 못 만들어도 삭제는 동작한다([절차](../script.md#영구-삭제가-쓰는-작업-기록-인덱스)).

배포 전 `migrate_ai_schema`로 `agent_run_artifacts`의 새 열을 적용해야 한다. 열이 없으면 준비 상태 확인은 통과하지만(테이블만 본다) artifact를 쓰는 모든 `workspace_workflow`가 실패한다.

### 폴더 내용 조회(`folder_lookup`)

작업 탭에서 "회의록 폴더에 뭐가 있어?"처럼 폴더 내용을 물으면 라우터가 `folder_lookup`(retrieval·문서 동작 없음, persist false, 기능 없음)을 고른다.
작업 탭(`mode=task`) 턴에서만 쓴다. 읽기 전용이라 run을 만들지 않고 결과 `message`로만 답한다.

- 트리는 document-svc `POST /internal/agent/folder-lookup/tree`로 읽는다(사이드바 트리와 같은 범위, `X-Agent-Service-Token`).
- 메시지가 어느 폴더를 지목했는지는 `JEV_FOLDER_LOOKUP_ENABLED`가 켜져 있으면 Jev가, 아니면(또는 Jev 실패·선택지 255개 초과 시) 이번 턴의 LLM이
  판단한다(`prompts/agent_folder_lookup.system.md`, [ADR-0027](../adr/0027-jev-selective-judge.md)). LLM 판단은 폴더 이름·경로 목록
  (최대 500개, 폴더 id 대신 `f1` 같은 짧은 키)과 메시지·최근 대화 6개를 보내고 지목한 폴더 키를 받는다. 조사·따옴표·띄어쓰기와
  상관없이 뜻으로 판단하고, 질문 속 일반 단어(`회의록 폴더에 문서 뭐 있어?`의 `문서`)나 문서 이름(`기획서`)은 폴더로 보지 않는다.
  목록에 없는 키는 버린다.
- 지목한 폴더가 없을 때만 `folder_id`(사이드바에서 고른 폴더)를 쓴다. 폴더 정리·Skill과 같은 규칙이다.
- 하나면 바로 아래 하위 폴더·문서 이름을 최대 50개 보여 주고 문서 내용은 채팅 탭에서 물으라고 안내한다. 여러 개면 경로를 보여 주고 되묻는다.
  없으면 폴더 이름을 정확히 적거나 폴더를 고르라고 안내한다. 문서 본문은 읽지 않는다.
- 트리를 읽지 못하거나 지목 판단 응답이 형식에 맞지 않으면 안내를 `message`로 돌려준다. document-svc가 400·403·404면 "이 워크스페이스의 폴더 목록을 읽을 수 없어요.",
  그 밖의 응답·연결 실패는 "폴더 목록을 읽지 못했어요. 잠시 후 다시 시도해 주세요."다.
- `DOCUMENT_INTERNAL_BASE_URL`·`AGENT_INTERNAL_TOKEN`은 이제 `AGENT_SKILLS_ENABLED`와 무관하게 agent command worker 기동 시 확인한다.

### 할 일 없이 `/slug`만 보낸 턴(Skill 실행)

`message`가 `/slug`뿐이면(사이드바 "실행"처럼 대상만 고른 경우) 라우터를 거치지 않고 Skill 정의와 고른 대상으로 실행한다.
고른 문서는 `active_markdown_context`(+`document_id`·`base_version`), 고른 폴더는 `folder_id`로 보낸다.
결과 방식이 없는(이전에 게시한) 문서 Skill은 기능으로 정한다: 생성·편집(또는 `template`)이면 `auto`, 생성만이면 `create`, 편집만이면 `edit`.

| Skill | 문서를 고름 | 폴더만 고름 / 대상 없음 |
|---|---|---|
| `result_mode=edit` | Skill 지침으로 편집안(`markdown_edit`). `targets`가 `["selection"]`뿐인데 선택 범위가 없으면 `clarify` | `clarify`(수정할 문서를 열라는 안내) |
| `result_mode=create`, `requires_source=true` | 그 문서를 원문으로 새 문서 초안(`markdown_create`) | 폴더: 폴더 바로 아래 Markdown 문서들을 원문으로 새 문서 초안. 대상 없음: 입력 없이 새 문서 초안 |
| `result_mode=create`, `requires_source=false` | 원문 없이 새 문서 초안(문서는 위치로만 쓴다) | 원문 없이 새 문서 초안(폴더는 위치로만 쓴다) |
| `result_mode=auto` | `clarify`("새 문서로 만들까요, 지금 문서를 수정할까요?", 문서 동작 없음) | 새 문서 초안(`markdown_create`) |
| 폴더 정리(문서 기능 없음) | 워크스페이스 전체 정리 계획(`folder_organize` run) | `folder_id` 범위(없으면 워크스페이스 전체)의 정리 계획(`folder_organize` run, `target_folder_id`) |

- 원문은 Skill 버전의 `requires_source`([Skill API](skills.md))가 `true`일 때만 쓴다. `false`면 문서·폴더를 골라도 원문 없이
  새 문서 위치로만 쓴다. 문서를 고른 상태에서 `requires_source=false` Skill을 실행할 때 클라이언트는 그 문서의 부모 폴더를 `folder_id`로 보내며,
  `active_markdown_context`가 와도 원문이나 `{문서}` 값으로 쓰지 않는다(`{문서}`는 `(현재 문서 없음)`).
- 문서 원문은 생성 payload의 `reference_context.source_document`(`document_id`, `markdown`)로 싣고, 지침의 `{문서}`는 그 위치를 가리킨다.
  문서와 `folder_id`가 함께 오면 문서가 원문이다.
- 폴더 원문(`requires_source=true`, 문서 없이 `folder_id`만)은 document-svc `POST /internal/agent/skill-execution/folder-sources/read`로
  그 폴더 **바로 아래**(하위 폴더 제외) Markdown 문서 중 사용자가 읽을 수 있는 문서를 읽는다. 최근 수정순 최대 20개, 본문 합계 60,000자까지
  싣고(상한을 넘는 문서에서 멈추며, 첫 문서 하나가 상한을 넘으면 그 문서 앞부분만), `reference_context.source_documents`
  (`document_id`, `title`, `updated_at`, `markdown` 배열)로 싣는다. 지침의 `{문서}`는 그 위치를 가리킨다.
  일부만 읽었으면 결과 `message`에 "폴더 문서 N개 중 최근 수정한 M개만 읽었어요."(본문을 자른 경우 "폴더 문서가 길어 일부 내용만 읽었어요.")를 덧붙인다.
  폴더를 읽을 수 없거나(403·404) 읽을 문서가 없으면 초안을 만들지 않고 `clarify`로 다른 대상을 고르라고 안내한다.
- `/slug`만 보낸 턴은 할 일이 없으므로 앞선 대화(`recent_conversation_summary`·`recent_messages`·`reference_context`)를 생성 payload에
  싣지 않는다. 원문은 명시한 문서·폴더뿐이다(앞 턴 내용이 초안에 섞이지 않게).
- 할 일을 붙인 턴(`/slug 할 일`)도 명시한 Skill로 새 문서를 만들면 같은 원문 규칙(문서·폴더)을 따른다. 이 턴은 할 일이 대화를 가리킬 수 있어
  대화 맥락을 함께 싣되, 생성 프롬프트는 명시한 원문을 대화보다 우선한다.
- 할 일을 붙인 턴은 라우터를 거치며 결과 방식으로 동작을 맞춘다: `create` Skill은 새 문서, `edit` Skill은 라우터가 새 문서를 골라도 문서 편집,
  `auto` Skill은 문서가 열려 있고 확신도가 0.7 미만이면 위와 같은 `clarify`. 되묻기에는 `/slug 새 문서로 만들어 줘`나 `/slug 지금 문서를 수정해 줘`처럼 답하면 된다.

열린 문서 크기 상한(`MAX_ACTIVE_DOCUMENT_CHARS = 30,000`자):

- Skill 없는 새 문서 요청은 열린 문서를 `reference_context.active_document`(`document_id`, `markdown`)로 싣는다. 사용자가 고른 원문이
  아니므로 생성 프롬프트는 요청이 그 문서를 가리킬 때("이 회의록을 바탕으로" 등)만 쓴다. 열린 문서가 상한을 넘으면 싣지 않고 초안은 만들며,
  결과 `message`에 "열린 문서가 길어 초안에 참고하지 않았어요. … 위키에 올린 뒤 위키 내용을 기준으로 다시 요청해 주세요."를 덧붙인다.
  저장 계획(`workspace_workflow` 생성)도 원문 안내(긴 열린 문서, 폴더 문서 일부만 읽음)가 있으면 document-svc 기본 문구와 같은
  "작업 계획을 만들었습니다. 미리보기에서 확인해 주세요." 뒤에 붙여 `message`로 돌려준다.
- 편집(`markdown_edit`, `workspace_workflow` 편집)은 편집 범위(선택 영역·현재 섹션, 문서 전체 편집이면 문서 전체)가 상한을 넘으면
  편집안을 만들지 않고 `clarify`로 수정할 부분이나 현재 섹션을 고르라고 안내한다. 편집기는 편집 범위와 앞뒤 몇 줄만 LLM에 보낸다.
- 편집기는 편집안을 만들기 전에 요청 문장을 한 번 더 분류해 결과 형식·추가 전용·구조 변경·번역 언어를 정하고, 출력 검사는 요청 문장의 단어 대신 이 값을 본다([ADR-0043](../adr/0043-edit-detail-classification.md)). 원문에 있던 HTML(편집기가 빈 줄로 저장한 `<br />` 등)은 편집 결과에 남을 수 있고, 새로 생긴 HTML만 막는다.
  이미 만든 미리보기를 저장하는 턴은 LLM을 다시 부르지 않으므로 이 상한을 보지 않는다.

- API 수: 11

## API 목차

| API | 목적 |
|---|---|
| [`GET /agent/runs/{run_id}`](#summary-get-agent-runs-run-id) | 승인형 Agent run의 현재 계획과 상태를 조회합니다. |
| [`GET /agent/runs/{run_id}/edit-previews`](#summary-get-agent-runs-run-id-edit-previews) | 첨부 문서 수정 계획의 문서별 변경을 조회합니다. |
| [`POST /agent/runs/{run_id}/approve`](#summary-post-agent-runs-run-id-approve) | 현재 계획의 version과 operation hash를 검증해 승인합니다. |
| [`POST /agent/runs/{run_id}/cancel`](#summary-post-agent-runs-run-id-cancel) | 실행 가능한 Agent run을 취소합니다. |
| [`POST /agent/runs/{run_id}/reject`](#summary-post-agent-runs-run-id-reject) | 현재 Agent 계획을 거절합니다. |
| [`POST /agent/runs/{run_id}/revise`](#summary-post-agent-runs-run-id-revise) | 사용자 지시로 새 계획을 요청합니다. |
| [`POST /internal/agent/runs/artifacts/list`](#summary-post-internal-agent-runs-artifacts-list) | Agent 실행에 등록된 artifact 목록을 조회합니다. |
| [`POST /internal/agent/runs/artifacts/register`](#summary-post-internal-agent-runs-artifacts-register) | Agent 실행 결과 artifact를 등록합니다. |
| [`POST /internal/agent/runs/artifacts/resolve`](#summary-post-internal-agent-runs-artifacts-resolve) | Agent artifact의 저장 위치와 메타데이터를 확인합니다. |
| [`POST /internal/agent/runs/tool-authorizations/execute`](#summary-post-internal-agent-runs-tool-authorizations-execute) | Agent Tool 변경 작업의 실행 권한을 검증합니다. |
| [`POST /internal/agent/runs/tool-authorizations/read`](#summary-post-internal-agent-runs-tool-authorizations-read) | Agent Tool 읽기 작업의 실행 권한을 검증합니다. |
| [`GET /internal/agent/runs/{run_id}`](#summary-get-internal-agent-runs-run-id) | Markdown Agent 실행 상태와 결과를 조회합니다. |

`/agent/runs/*` API는 `AGENT_SKILLS_ENABLED=true`일 때만 노출되며
`X-Agent-Service-Token`으로 보호한다.

## 한눈에 보기

<a id="summary-get-agent-runs-run-id"></a>
### `GET /agent/runs/{run_id}`

| 항목 | 내용 |
|---|---|
| 목적 | 승인형 Agent run의 현재 계획과 상태를 조회합니다. |
| 입력 | **Path** — `run_id`: `string`<br>**Query** — `workspace_id`, `user_id`: `string`<br>**Header** — `X-Agent-Service-Token`: 필수 |
| 출력 | `200` — `AgentRunResponse` |
| 조건 | `AGENT_SKILLS_ENABLED=true`일 때만 노출되며 workspace·user 소유권을 확인합니다. |
| 주요 오류 | `401` 토큰 불일치<br>`404` run 없음<br>`503` 토큰 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-get-agent-runs-run-id"></a>
### `GET /agent/runs/{run_id}` 상세

#### 1. Method + Path

`GET /agent/runs/{run_id}`

#### 2. 목적

계획 승인 화면에 필요한 plan version, operation hash, 작업 목록과 run 상태를 반환합니다.

#### 3. Auth 필요 여부

- 필요: `X-Agent-Service-Token`

#### 4. Request body

- Body 없음
- path: `run_id`
- query: `workspace_id`, `user_id`

#### 5. Response body

- HTTP `200`: 조회 성공

```json
{
  "id": "run_123",
  "workspace_id": "workspace_123",
  "action": "workspace_workflow",
  "skill_version_id": null,
  "status": "awaiting_approval",
  "request_summary": "현재 문서에 편집안을 반영해줘",
  "error_code": null,
  "clarification_question": null,
  "target_folder_id": null,
  "rule_summary": null,
  "plan": {
    "id": "plan_123",
    "version": 1,
    "summary": "승인된 편집안을 현재 문서에 반영합니다.",
    "operation_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "status": "awaiting_approval",
    "approved_by": null,
    "operations": [
      {
        "id": "operation_123",
        "sequence": 1,
        "tool_name": "apply_document_edit",
        "target_type": "document",
        "target_id": "doc_123",
        "base_version": 3,
        "source_parent_id": null,
        "destination_parent_id": null,
        "arguments": {},
        "reason": "사용자가 확인한 편집안을 반영합니다.",
        "depends_on": [],
        "status": "pending",
        "error_code": null,
        "warning": null,
        "suggested_selected": true
      }
    ]
  }
}
```

`clarification_question`은 계획 단계가 사용자 요청만으로 변경 의도·범위를 확정하지 못해
`status=clarification_required`, `error_code=mutation_intent_required`가 된 경우에만 채워집니다.
무엇이 불명확한지, 어떤 정보를 주면 되는지 사용자에게 설명하는 한국어 문장이며, 그대로 화면에 보여 줄 수 있습니다.
실행 중 Tool 예산 소진처럼 다른 이유의 `clarification_required`와 그 밖의 모든 상태에서는 `null`입니다.
`revise`로 다시 계획을 요청하면 비워집니다.
Tool 호출 예산은 계획 버전마다 40회이며 `revise`로 새 계획을 요청하면 다시 40회가 됩니다. 실행 후 검증 읽기는 예산을 쓰지 않습니다.

`target_folder_id`는 턴에서 지정한 작업 대상 폴더(`ai.agent.command`의 `folder_id`)이며, 지정하지 않았으면 `null`입니다.
`rule_summary`는 선택한 Skill의 규칙 요약으로, run을 만들 때의 Skill 설명(`skill_versions.description`)을 보존한 값입니다.
Skill 없이 실행했으면 `null`입니다.

`plan.approved_by`는 승인 주체입니다. 승인 화면에서 사용자가 직접 승인했으면 `user`, 사용자의 자동 승인 설정에 따라
document-svc가 대신 승인했으면 `auto:user-setting`이며, 승인 전이면 `null`입니다(`agent_approvals.approved_by`).

`plan.operations[]`의 `warning`·`suggested_selected`는 승인 화면 안내용 검토 결과이며 실행 내용이 아니라
`operation_hash`에 포함하지 않습니다. 계획 생성 직후 계층 스냅샷에 계획을 적용한 결과로 판정합니다.

- `warning`: `{"type": string, "message": string}` 또는 `null`. `message`는 그대로 보여 줄 한국어 문장입니다.
  - `duplicate_name`: 작업 결과 같은 폴더에 같은 종류(폴더·문서)·같은 이름의 항목이 둘 이상 생깁니다(공백·대소문자·NFC·문서의 `.md` 차이는 무시). 기존 항목과 겹치면 그 작업을, 계획이 만든·옮긴 항목끼리 겹치면 나중 작업을 표시합니다. 같은 항목을 바꾸는 이름 변경·이동 작업에 모두 붙습니다.
  - `depends_on_unselected`: 기본 선택에서 빠진 작업에 의존(`depends_on`)하는 작업입니다.
- `suggested_selected`: 화면의 기본 선택 여부입니다. `warning`이 있으면 `false`입니다. 기본 선택 그대로 `operation_ids`로 승인해도 의존 작업 누락이 생기지 않습니다.

```json
{
  "id": "run_456",
  "workspace_id": "workspace_123",
  "action": "folder_organize",
  "skill_version_id": "skill_version_123",
  "status": "awaiting_approval",
  "request_summary": "날짜_분류_제목 형식으로 정리해줘",
  "error_code": null,
  "clarification_question": null,
  "target_folder_id": "44444444-4444-4444-4444-444444444444",
  "rule_summary": "문서 이름을 날짜_분류_제목 형식으로 바꾸고 분류별 폴더로 옮깁니다.",
  "plan": {
    "id": "plan_456",
    "version": 1,
    "summary": "아직 바꾸지 않았습니다. 승인하면 아래 작업을 실행합니다.",
    "operation_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "status": "awaiting_approval",
    "operations": [
      {
        "id": "plan_456-op-1",
        "sequence": 1,
        "tool_name": "rename_document",
        "target_type": "document",
        "target_id": "doc_1",
        "base_version": 2,
        "source_parent_id": "44444444-4444-4444-4444-444444444444",
        "destination_parent_id": null,
        "arguments": {"document_id": "doc_1", "display_name": "2026-10-19_회의_주간회의", "base_version": 2},
        "reason": "규칙에 맞게 이름을 바꿉니다.",
        "depends_on": [],
        "status": "pending",
        "error_code": null,
        "warning": {"type": "duplicate_name", "message": "같은 위치에 이름이 같은 문서 '2026-10-19_회의_주간회의'이(가) 있어 중복일 수 있어요."},
        "suggested_selected": false
      }
    ]
  }
}
```

```json
{
  "id": "run_123",
  "workspace_id": "workspace_123",
  "action": "folder_organize",
  "skill_version_id": null,
  "status": "clarification_required",
  "request_summary": "문서를 날짜별 폴더로 정리해줘",
  "error_code": "mutation_intent_required",
  "clarification_question": "각 문서의 생성일 정보가 없어 날짜별로 폴더를 만들 수 없습니다. 문서별 날짜를 알려 주시거나 다른 기준을 선택해 주세요.",
  "plan": null
}
```

#### 6. Error response

- `401`: Agent 서비스 토큰 누락·불일치
- `404`: 요청 scope에 해당하는 run 없음
- `503`: Agent 서비스 토큰 미설정

#### 7. Pagination / filtering

- 페이지네이션 없음
- `workspace_id`, `user_id`로 소유권 scope 제한

#### 8. 권한 규칙

서버가 인증한 workspace·user와 run 소유권이 모두 일치해야 합니다.

#### 9. 예시 요청/응답

```bash
curl "$PIPELINE/agent/runs/run_123?workspace_id=workspace_123&user_id=user_123" \
  -H 'X-Agent-Service-Token: <value>'
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: get_agent_run_agent_runs__run_id__get`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentRunStatusRequester.java`:45-47,61-62,106-113 (`app.agent.run-endpoint`, `X-Agent-Service-Token`).
- 아웃바운드 호출: 없음(AI DB 조회).
- 미연동 표시: 없음.

</details>

<a id="summary-get-agent-runs-run-id-edit-previews"></a>
### `GET /agent/runs/{run_id}/edit-previews`

| 항목 | 내용 |
|---|---|
| 목적 | 첨부 문서 수정 계획의 문서별 변경을 조회합니다. |
| 입력 | **Path** — `run_id`: `string`<br>**Query** — `workspace_id`, `user_id`: `string`<br>**Header** — `X-Agent-Service-Token` |
| 출력 | `200` — `AgentRunEditPreviewsResponse` |
| 조건 | `AGENT_SKILLS_ENABLED=true`일 때만 노출되며 workspace·user 소유권을 확인합니다. 문서별 읽기 권한과 현재 제목은 document-svc 프록시가 확인합니다. |
| 주요 오류 | `401` 토큰 불일치<br>`404` run 없음<br>`503` 토큰 미설정 |

```ts
type AgentRunEditPreviews = { items: AgentDocumentEditPreview[]; expires_at: string | null };
type AgentDocumentEditPreview = {
  operation_id: string; document_id: string; display_name: string;
  base_version: number; summary: string; added_lines: number; removed_lines: number;
  hunks: { before_start: number; after_start: number; lines: { type: "context" | "delete" | "insert"; text: string }[] }[];
};
```

줄 번호는 1부터 시작하고 문맥은 3줄이다. 계획이 없거나 미리 보기가 없는 run은 `items: []`, `expires_at: null`이다. 객체가 남아 있으면 만료 뒤에도 내용을 돌려주므로 화면이 `expires_at`으로 수락을 막는다. [정리](#expired-artifact-cleanup)로 수정안이 지워지면 그 문서는 `items`에서 빠지고 `expires_at`은 지난 시각이다. 응답 형식은 같고 5xx가 아니다.

<a id="summary-post-agent-runs-run-id-approve"></a>
### `POST /agent/runs/{run_id}/approve`

| 항목 | 내용 |
|---|---|
| 목적 | 사용자가 확인한 현재 plan version과 operation hash를 검증해 실행을 승인합니다. 고른 작업만 실행하는 부분 승인도 받습니다. |
| 입력 | **Path** — `run_id`<br>**Body** — workspace·user·plan version·operation hash·선택 `operation_ids`·선택 `approved_by` |
| 출력 | `200` — `status=executing`인 `AgentRunResponse` |
| 조건 | run과 plan이 모두 `awaiting_approval`이고 승인값이 현재 계획과 정확히 일치해야 합니다. 승인하는 작업이 쓰는 artifact가 만료됐거나 지워졌으면 거절하고 run은 `awaiting_approval`로 남습니다. 거절·취소는 그대로 됩니다. |
| 주요 오류 | `400` 작업 선택 오류<br>`409` 계획 변경·상태 충돌·수정안 만료<br>`422` 요청 검증 실패 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-agent-runs-run-id-approve"></a>
### `POST /agent/runs/{run_id}/approve` 상세

#### 1. Method + Path

`POST /agent/runs/{run_id}/approve`

#### 2. 목적

조회한 계획과 같은 version·hash에만 승인을 기록하고 execution job을 등록합니다.

`operation_ids`(1~20개)를 보내면 고른 작업(`plan.operations[].id`)만 실행합니다. 고르지 않은 작업은
승인 시점에 `status=skipped`, `error_code=not_selected`가 되며 실행·검증·복구 대상이 아닙니다.
`plan_version`·`operation_hash`는 부분 승인이어도 계획 전체 기준으로 검사합니다. 계획의 모든 작업을 고르면
생략한 것과 같습니다(전체 실행). 고른 작업만 성공하면 run은 `completed`이며, 고르지 않은 작업은
`partial_failed` 판정에 포함하지 않습니다. 고른 목록은 `agent_approvals.operation_ids`에 남습니다.

`approved_by`는 승인 주체이며 `agent_approvals.approved_by`에 남습니다. 사용자가 승인 화면에서 직접 승인하면 `user`(기본값),
사용자의 자동 승인 설정에 따라 document-svc가 대신 승인하면 `auto:user-setting`입니다. 버전·hash 검사는 둘이 같습니다.

#### 3. Auth 필요 여부

- 필요: `X-Agent-Service-Token`

#### 4. Request body

```json
{
  "workspace_id": "workspace_123",
  "user_id": "user_123",
  "plan_version": 1,
  "operation_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "operation_ids": ["plan_123-op-1", "plan_123-op-3"]
}
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `plan_version` | `integer` | 예 | 승인할 plan 버전 |
| `operation_hash` | `string` | 예 | 계획 전체의 operation hash(64자) |
| `operation_ids` | `string[]` \| `null` | 아니오 | 실행할 작업 ID(1~20개). 생략·`null`이면 계획 전체 |
| `approved_by` | `string` | 아니오 | 승인 주체. `user`(기본값) 또는 `auto:user-setting`. 그 밖의 값은 `422` |

#### 5. Response body

- HTTP `200`: 승인 성공
- `AgentRunResponse`; 승인 성공 직후 `status`는 `executing`입니다.

```json
{
  "id": "run_123",
  "workspace_id": "workspace_123",
  "action": "workspace_workflow",
  "skill_version_id": null,
  "status": "executing",
  "request_summary": "현재 문서에 편집안을 반영해줘",
  "error_code": null,
  "clarification_question": null,
  "plan": null
}
```

#### 6. Error response

- `400`: `operation_ids` 빈 배열·20개 초과·중복·계획에 없는 ID·고른 작업이 고르지 않은 작업에 의존(`depends_on`). 빈 배열·20개 초과·중복은 계획 확인보다 먼저 거절합니다.
- `401` 토큰 불일치, `409` 현재 계획·상태 불일치, `422` 그 밖의 입력 검증 실패, `503` 토큰 미설정

#### 7. Pagination / filtering

- 지원하지 않음

#### 8. 권한 규칙

workspace·user scope, plan version, operation hash를 모두 검증합니다.

#### 9. 예시 요청/응답

```bash
curl -X POST "$PIPELINE/agent/runs/run_123/approve" \
  -H 'X-Agent-Service-Token: <value>' -H 'Content-Type: application/json' \
  --data '{"workspace_id":"workspace_123","user_id":"user_123","plan_version":1,"operation_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: approve_agent_run_agent_runs__run_id__approve_post`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentRunStatusRequester.java`:73-76(사용자 승인)·83-86(자동 승인, `approved_by=auto:user-setting`) → 101-107 (`app.agent.run-endpoint`, `X-Agent-Service-Token`).
- 아웃바운드 호출: 승인 실행은 Tool gateway를 거쳐 document-svc `POST /internal/agent/tools/execute/{tool_name}`를 호출한다(`app/modules/agent_run/infrastructure/backend_tool_gateway.py:52`).
- 아웃바운드 호출(run 상태 알림): agent worker는 job을 끝낸 뒤 run이 끝난 상태(`completed`·`partial_failed`·`failed`·`conflicted`·`rejected`·`cancelled`·`rollback_failed`)나 `clarification_required`이면, 또는 planning job 뒤 `awaiting_approval`이면 document-svc `POST /internal/agent/runs/{run_id}/plan-ready`(body `workspace_id`·`user_id`, `X-Agent-Service-Token`)를 job마다 한 번 호출한다(`app/modules/agent_run/infrastructure/agent_worker.py`의 `_notify_document_service` → `backend_tool_gateway.py`의 `notify_plan_ready`). `markdown_turn` 복구는 알리지 않는다. document-svc는 사용량 수집을 대기열에 넣고, `awaiting_approval`이면 사용자의 자동 승인 설정과 대상 권한을 보고 이 API를 `approved_by=auto:user-setting`으로 부를지 정하며, 끝난 상태면 run을 닫는다. 알림이 실패해도 경고만 남기고 job 결과는 바뀌지 않는다. 계획은 승인 대기로 남아 사용자가 직접 승인할 수 있고, 사용량은 종료 조회나 일일 대사가 가져간다.
- 미연동 표시: 없음.

</details>

<a id="summary-post-agent-runs-run-id-cancel"></a>
### `POST /agent/runs/{run_id}/cancel`

| 항목 | 내용 |
|---|---|
| 목적 | Agent run의 후속 실행을 막고 이미 실행한 변경의 역순 복구를 요청합니다. |
| 입력 | **Path** — `run_id`<br>**Body** — `workspace_id`, `user_id` |
| 출력 | `200` — 현재 취소·복구 상태를 담은 `AgentRunResponse` |
| 조건 | 일반 completed·rejected는 거절합니다. markdown_turn은 completed여도 연결된 하위 작업과 Skill 게시의 복구를 요청합니다. failed·partial_failed·conflicted·rollback_failed는 복구를 요청할 수 있고, 취소 진행·완료 상태의 재요청은 멱등입니다. |
| 주요 오류 | `409` run 없음 또는 취소할 수 없는 상태 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-agent-runs-run-id-cancel"></a>
### `POST /agent/runs/{run_id}/cancel` 상세

#### 1. Method + Path

`POST /agent/runs/{run_id}/cancel`

#### 2. 목적

대기 job과 아직 시작하지 않은 operation을 함께 취소하고 `rollback` job을 등록합니다. Python worker가 변경 전 상태와 실행 결과를 이용해 역작업을 역순으로 실행합니다. 복구 도구 호출에는 별도 멱등키와 기록된 정확한 인자의 승인 검사를 적용합니다.

상위 `markdown_turn`과 하위 run의 연결은 하위 생성과 같은 DB 트랜잭션에서 `result.run_id`에 기록합니다. 상위 취소는 이미 완료된 하위 작업도 복구 대상으로 삼으며, 하위가 `cancelled`가 된 뒤 상위 취소를 완료합니다. 실행 중인 상위 턴의 늦은 결과는 외부에 발행하지 않습니다.

영구 삭제한 문서의 단계는 건너뛰고 끝에 `rollback_failed`(`rollback_document_purged`)를 남깁니다. 상태는 `cancel_requested` → `rolling_back` → `cancelled`로 진행합니다. 실제 결과를 확인할 수 없거나 다른 사용자의 후속 변경과 충돌하거나 복구 도구가 실패하면 `rollback_failed`와 `error_code`를 남깁니다. 도구의 429·5xx 응답처럼 재시도할 수 있는 오류는 바로 확정하지 않고 job backoff(5초 간격, 최대 3회)로 다시 시도하며, 소진한 뒤에 `rollback_failed`로 닫습니다. 재요청하면 기존 복구 기록과 멱등키로 재시도합니다. 내용·이름·위치·순서를 복원하며 버전과 감사 기록은 이전 숫자로 되감지 않습니다.

Backend는 `delete_folder(require_empty=true)`로 빈 폴더를 잠금 상태에서 확인해 삭제하고, `delete_document`는 일반 문서 삭제와 같게 문서를 휴지통으로 옮기고 파생 Wiki 정리는 `document_deleted` command로 비동기 처리합니다. AI는 정리 완료를 기다리거나 확인하지 않습니다. Query·수집·변환·Skill 게시 등 공개 AI 기능의 전체 취소는 [공통 작업 취소 API](tasks.md)를 사용합니다. 이 내부 Agent API만 직접 호출하면 core DB의 채팅·업무 변경까지 복구하지 않습니다.

#### 3. Auth 필요 여부

- 필요: `X-Agent-Service-Token`

#### 4. Request body

```json
{"workspace_id":"workspace_123","user_id":"user_123"}
```

#### 5. Response body

- HTTP `200`: 취소 요청 접수 또는 현재 상태 반환
- 신규 요청은 `status=cancel_requested`; `cancelled`는 복구 확인이 끝난 상태입니다.

```json
{
  "id": "run_123",
  "workspace_id": "workspace_123",
  "action": "workspace_workflow",
  "skill_version_id": null,
  "status": "cancel_requested",
  "request_summary": "현재 문서에 편집안을 반영해줘",
  "error_code": null,
  "clarification_question": null,
  "plan": null
}
```

#### 6. Error response

- `401` 토큰 불일치, `409` 상태 충돌, `422` 입력 검증 실패, `503` 토큰 미설정

#### 7. Pagination / filtering

- 지원하지 않음

#### 8. 권한 규칙

workspace·user와 run 소유권이 일치해야 합니다.

#### 9. 예시 요청/응답

```bash
curl -X POST "$PIPELINE/agent/runs/run_123/cancel" \
  -H 'X-Agent-Service-Token: <value>' -H 'Content-Type: application/json' \
  --data '{"workspace_id":"workspace_123","user_id":"user_123"}'
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: cancel_agent_run_agent_runs__run_id__cancel_post`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentRunStatusRequester.java`:77 → 84-90 (`app.agent.run-endpoint`, `X-Agent-Service-Token`).
- 아웃바운드 호출: 없음(AI DB 갱신).
- 미연동 표시: 없음.

</details>

<a id="summary-post-agent-runs-run-id-reject"></a>
### `POST /agent/runs/{run_id}/reject`

| 항목 | 내용 |
|---|---|
| 목적 | 승인 대기 중인 현재 계획을 거절합니다. |
| 입력 | **Path** — `run_id`<br>**Body** — `workspace_id`, `user_id` |
| 출력 | `200` — `status=rejected`인 `AgentRunResponse` |
| 조건 | run과 plan이 모두 `awaiting_approval`이어야 합니다. |
| 주요 오류 | `409` 승인 대기 상태가 아님 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-agent-runs-run-id-reject"></a>
### `POST /agent/runs/{run_id}/reject` 상세

#### 1. Method + Path

`POST /agent/runs/{run_id}/reject`

#### 2. 목적

현재 계획에 거절 결정을 기록하고 run을 terminal 상태로 종료합니다.

#### 3. Auth 필요 여부

- 필요: `X-Agent-Service-Token`

#### 4. Request body

```json
{"workspace_id":"workspace_123","user_id":"user_123"}
```

#### 5. Response body

- HTTP `200`: 거절 성공
- `AgentRunResponse`; 성공 시 `status=rejected`

```json
{
  "id": "run_123",
  "workspace_id": "workspace_123",
  "action": "workspace_workflow",
  "skill_version_id": null,
  "status": "rejected",
  "request_summary": "현재 문서에 편집안을 반영해줘",
  "error_code": null,
  "clarification_question": null,
  "plan": null
}
```

#### 6. Error response

- `401` 토큰 불일치, `409` 상태 충돌, `422` 입력 검증 실패, `503` 토큰 미설정

#### 7. Pagination / filtering

- 지원하지 않음

#### 8. 권한 규칙

workspace·user와 run 소유권이 일치해야 합니다.

#### 9. 예시 요청/응답

```bash
curl -X POST "$PIPELINE/agent/runs/run_123/reject" \
  -H 'X-Agent-Service-Token: <value>' -H 'Content-Type: application/json' \
  --data '{"workspace_id":"workspace_123","user_id":"user_123"}'
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: reject_agent_run_agent_runs__run_id__reject_post`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentRunStatusRequester.java`:73 → 84-90 (`app.agent.run-endpoint`, `X-Agent-Service-Token`).
- 아웃바운드 호출: 없음(AI DB 갱신).
- 미연동 표시: 없음.

</details>

<a id="summary-post-agent-runs-run-id-revise"></a>
### `POST /agent/runs/{run_id}/revise`

| 항목 | 내용 |
|---|---|
| 목적 | 기존 계획을 폐기하고 사용자 지시로 새 계획을 생성하도록 등록합니다. |
| 입력 | **Path** — `run_id`<br>**Body** — `workspace_id`, `user_id`, `instruction` |
| 출력 | `200` — `status=queued`인 `AgentRunResponse` |
| 조건 | `awaiting_approval` 또는 `clarification_required` 상태에서만 허용합니다. 현재 계획이 쓰는 artifact가 만료됐거나 지워졌으면 거절합니다. |
| 주요 오류 | `409` 수정할 수 없는 상태·수정안 만료 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-agent-runs-run-id-revise"></a>
### `POST /agent/runs/{run_id}/revise` 상세

#### 1. Method + Path

`POST /agent/runs/{run_id}/revise`

#### 2. 목적

이전 계획을 superseded 처리하고 새 planning job을 등록합니다.

#### 3. Auth 필요 여부

- 필요: `X-Agent-Service-Token`

#### 4. Request body

```json
{
  "workspace_id": "workspace_123",
  "user_id": "user_123",
  "instruction": "문서를 저장하지 말고 미리보기만 다시 만들어줘"
}
```

#### 5. Response body

- HTTP `200`: 수정 요청 접수 성공
- `AgentRunResponse`; 성공 시 `status=queued`, `plan=null`

```json
{
  "id": "run_123",
  "workspace_id": "workspace_123",
  "action": "workspace_workflow",
  "skill_version_id": null,
  "status": "queued",
  "request_summary": "문서를 저장하지 말고 미리보기만 다시 만들어줘",
  "error_code": null,
  "clarification_question": null,
  "plan": null
}
```

#### 6. Error response

- `401` 토큰 불일치, `409` 상태 충돌, `422` 입력 검증 실패, `503` 토큰 미설정

#### 7. Pagination / filtering

- 지원하지 않음

#### 8. 권한 규칙

workspace·user와 run 소유권이 일치해야 합니다.

#### 9. 예시 요청/응답

```bash
curl -X POST "$PIPELINE/agent/runs/run_123/revise" \
  -H 'X-Agent-Service-Token: <value>' -H 'Content-Type: application/json' \
  --data '{"workspace_id":"workspace_123","user_id":"user_123","instruction":"문서를 저장하지 말고 미리보기만 다시 만들어줘"}'
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: revise_agent_run_agent_runs__run_id__revise_post`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentRunStatusRequester.java`:81 → 84-90 (`app.agent.run-endpoint`, `X-Agent-Service-Token`).
- 아웃바운드 호출: 없음(AI DB 갱신).
- 미연동 표시: 없음.

</details>

<a id="summary-post-internal-agent-runs-artifacts-list"></a>
### `POST /internal/agent/runs/artifacts/list`

| 항목 | 내용 |
|---|---|
| 목적 | Agent 실행에 등록된 artifact 목록을 조회합니다. |
| 입력 | **Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `AgentArtifactListRequest` |
| 출력 | `200` 성공 — 배열<`AgentArtifactResponse`> |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-internal-agent-runs-artifacts-list"></a>
### `POST /internal/agent/runs/artifacts/list` 상세

#### 1. Method + Path

`POST /internal/agent/runs/artifacts/list`

#### 2. 목적

Agent 실행에 등록된 artifact 목록을 조회합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`AgentArtifactListRequest`)

```json
{
  "run_id": "string",
  "user_id": "string",
  "workspace_id": "string"
}
```

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`Response List Agent Artifacts Internal Agent Runs Artifacts List Post`)

```json
[
  {
    "base_version": 1,
    "content_hash": "string",
    "document_id": "string",
    "id": "string",
    "purpose": "string",
    "target": {
    }
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
curl -X POST "$PIPELINE/internal/agent/runs/artifacts/list" \
  -H 'X-Internal-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"run_id":"<value>","user_id":"<value>","workspace_id":"<value>"}'
```

```json
[
  {
    "base_version": 1,
    "content_hash": "string",
    "document_id": "string",
    "id": "string",
    "purpose": "string",
    "target": {
    }
  }
]
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: list_agent_artifacts_internal_agent_runs_artifacts_list_post`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentArtifactClient.java`:39 (`app.agent.status-endpoint` + `/artifacts`).
- 아웃바운드 호출: 없음.
- 미연동 표시: 없음.

[↑ 요약으로 돌아가기](#summary-post-internal-agent-runs-artifacts-list)

</details>

<a id="summary-post-internal-agent-runs-artifacts-register"></a>
### `POST /internal/agent/runs/artifacts/register`

| 항목 | 내용 |
|---|---|
| 목적 | Agent 실행 결과 artifact를 등록합니다. |
| 입력 | **Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `AgentArtifactRegisterRequest` |
| 출력 | `200` 성공 — `AgentArtifactResponse` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-internal-agent-runs-artifacts-register"></a>
### `POST /internal/agent/runs/artifacts/register` 상세

#### 1. Method + Path

`POST /internal/agent/runs/artifacts/register`

#### 2. 목적

Agent 실행 결과 artifact를 등록합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`AgentArtifactRegisterRequest`)

```json
{
  "artifact_id": "string",
  "base_version": 1.0,
  "content_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "document_id": "string",
  "markdown": "string",
  "purpose": "string",
  "run_id": "string",
  "target": {
  },
  "user_id": "string",
  "workspace_id": "string"
}
```

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`AgentArtifactResponse`)

```json
{
  "base_version": 1,
  "content_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "document_id": "string",
  "id": "string",
  "purpose": "string",
  "target": {
  }
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
curl -X POST "$PIPELINE/internal/agent/runs/artifacts/register" \
  -H 'X-Internal-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"artifact_id":"<value>","base_version":1,"content_hash":"sha256:50d858e0985ecc7f60418aaf0cc5ab587f42c2570a884095a9e8ccacd0f6545c","document_id":"<value>","markdown":"example","purpose":"<value>","run_id":"<value>","target":{},"user_id":"<value>","workspace_id":"<value>"}'
```

```json
{
  "base_version": 1,
  "content_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "document_id": "string",
  "id": "string",
  "purpose": "string",
  "target": {
  }
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: register_agent_artifact_internal_agent_runs_artifacts_register_post`)

#### 연동

- 인바운드 호출자: **호출자 없음.** `PipelineAgentArtifactClient`는 `/list`·`/resolve`만 호출한다(Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentArtifactClient.java`:39,59). `artifacts/register` 문자열은 document-svc 전체에서 검색되지 않는다.
- 아웃바운드 호출: 없음.
- 미연동 표시: **미연동.** ai-svc 내부에서도 이 route를 호출하는 경로가 없다.

[↑ 요약으로 돌아가기](#summary-post-internal-agent-runs-artifacts-register)

</details>

<a id="summary-post-internal-agent-runs-artifacts-resolve"></a>
### `POST /internal/agent/runs/artifacts/resolve`

| 항목 | 내용 |
|---|---|
| 목적 | Agent artifact의 저장 위치와 메타데이터를 확인합니다. |
| 입력 | **Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `AgentArtifactResolveRequest` |
| 출력 | `200` 성공 — `AgentArtifactResolveResponse` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-internal-agent-runs-artifacts-resolve"></a>
### `POST /internal/agent/runs/artifacts/resolve` 상세

#### 1. Method + Path

`POST /internal/agent/runs/artifacts/resolve`

#### 2. 목적

Agent artifact의 저장 위치와 메타데이터를 확인합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`AgentArtifactResolveRequest`)

```json
{
  "artifact_id": "string",
  "base_version": 1.0,
  "content_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "document_id": "string",
  "purpose": "string",
  "run_id": "string",
  "target": {
  },
  "user_id": "string",
  "workspace_id": "string"
}
```

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`AgentArtifactResolveResponse`)

```json
{
  "base_version": 1,
  "content_hash": "string",
  "document_id": "string",
  "id": "string",
  "markdown": "string",
  "purpose": "string",
  "target": {
  }
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
curl -X POST "$PIPELINE/internal/agent/runs/artifacts/resolve" \
  -H 'X-Internal-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"artifact_id":"<value>","base_version":1,"content_hash":"sha256:50d858e0985ecc7f60418aaf0cc5ab587f42c2570a884095a9e8ccacd0f6545c","document_id":"<value>","purpose":"<value>","run_id":"<value>","target":{},"user_id":"<value>","workspace_id":"<value>"}'
```

```json
{
  "base_version": 1,
  "content_hash": "string",
  "document_id": "string",
  "id": "string",
  "markdown": "string",
  "purpose": "string",
  "target": {
  }
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: resolve_agent_artifact_internal_agent_runs_artifacts_resolve_post`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentArtifactClient.java`:59.
- 아웃바운드 호출: 없음.
- 미연동 표시: 없음.

[↑ 요약으로 돌아가기](#summary-post-internal-agent-runs-artifacts-resolve)

</details>

<a id="summary-post-internal-agent-runs-tool-authorizations-execute"></a>
### `POST /internal/agent/runs/tool-authorizations/execute`

| 항목 | 내용 |
|---|---|
| 목적 | Agent Tool 변경 작업의 실행 권한을 검증합니다. |
| 입력 | **Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `AgentToolExecuteAuthorizationRequest` |
| 출력 | `204` 성공 |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-internal-agent-runs-tool-authorizations-execute"></a>
### `POST /internal/agent/runs/tool-authorizations/execute` 상세

#### 1. Method + Path

`POST /internal/agent/runs/tool-authorizations/execute`

#### 2. 목적

Agent Tool 변경 작업의 실행 권한을 검증합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`AgentToolExecuteAuthorizationRequest`)

```json
{
  "arguments": {
  },
  "operation_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "operation_id": "string",
  "plan_id": "string",
  "plan_version": 1.0,
  "run_id": "string",
  "tool_name": "string",
  "user_id": "string",
  "workspace_id": "string"
}
```

#### 5. Response body

- HTTP `204`: Successful Response
- Body: 없음

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
curl -X POST "$PIPELINE/internal/agent/runs/tool-authorizations/execute" \
  -H 'X-Internal-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"arguments":{},"operation_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","operation_id":"<value>","plan_id":"<value>","plan_version":1,"run_id":"<value>","tool_name":"<value>","user_id":"<value>","workspace_id":"<value>"}'
```

```json
{
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: authorize_agent_tool_execute_internal_agent_runs_tool_authorizations_execute_post`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentToolAuthorizationClient.java`:39 → 48 (`app.agent.status-endpoint` + `/tool-authorizations`).
- 아웃바운드 호출: 없음.
- 미연동 표시: 없음.

[↑ 요약으로 돌아가기](#summary-post-internal-agent-runs-tool-authorizations-execute)

</details>

<a id="summary-post-internal-agent-runs-tool-authorizations-read"></a>
### `POST /internal/agent/runs/tool-authorizations/read`

| 항목 | 내용 |
|---|---|
| 목적 | Agent Tool 읽기 작업의 실행 권한을 검증합니다. |
| 입력 | **Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null`<br>**Body** — `AgentToolReadAuthorizationRequest` |
| 출력 | `204` 성공 |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-post-internal-agent-runs-tool-authorizations-read"></a>
### `POST /internal/agent/runs/tool-authorizations/read` 상세

#### 1. Method + Path

`POST /internal/agent/runs/tool-authorizations/read`

#### 2. 목적

Agent Tool 읽기 작업의 실행 권한을 검증합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Content-Type: `application/json` (`AgentToolReadAuthorizationRequest`)

```json
{
  "run_id": "string",
  "user_id": "string",
  "workspace_id": "string"
}
```

#### 5. Response body

- HTTP `204`: Successful Response
- Body: 없음

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
curl -X POST "$PIPELINE/internal/agent/runs/tool-authorizations/read" \
  -H 'X-Internal-Token: <value>' \
  -H 'Content-Type: application/json' \
  --data '{"run_id":"<value>","user_id":"<value>","workspace_id":"<value>"}'
```

```json
{
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: authorize_agent_tool_read_internal_agent_runs_tool_authorizations_read_post`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentToolAuthorizationClient.java`:34 → 48.
- 아웃바운드 호출: 없음.
- 미연동 표시: 없음.

[↑ 요약으로 돌아가기](#summary-post-internal-agent-runs-tool-authorizations-read)

</details>

<a id="summary-get-internal-agent-runs-run-id"></a>
### `GET /internal/agent/runs/{run_id}`

| 항목 | 내용 |
|---|---|
| 목적 | Markdown Agent 실행 상태와 결과를 조회합니다. |
| 입력 | **Path** — `run_id`: `string`<br>**Query** — `workspace_id`: `string`, `user_id`: `string`<br>**Header** — `X-Internal-Token`(필수, 인증 계층 검증): `string` / `null` |
| 출력 | `200` 성공 — `MarkdownAgentRunStatusResponse` |
| 조건 | 인증 필요<br>서비스 간 내부 인증 토큰을 검증한다.<br>필터링: `workspace_id`, `user_id`<br>올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.<br>요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다. |
| 주요 오류 | `422` 요청 검증 실패 — `HTTPValidationError`<br>`401` 내부 인증 토큰 누락 또는 불일치<br>`503` 내부 인증 미설정 |

<details>
<summary>상세 계약 보기</summary>

<a id="detail-get-internal-agent-runs-run-id"></a>
### `GET /internal/agent/runs/{run_id}` 상세

#### 1. Method + Path

`GET /internal/agent/runs/{run_id}`

#### 2. 목적

Markdown Agent 실행 상태와 결과를 조회합니다.

#### 3. Auth 필요 여부

- 필요
- 서비스 간 내부 인증 토큰을 검증한다.

#### 4. Request body

| 위치 | 이름 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| path | `run_id` | `string` | 예 | - |
| query | `workspace_id` | `string` | 예 | - |
| query | `user_id` | `string` | 예 | - |
| header | `X-Internal-Token` | `X-Internal-Token` | 예 (인증 계층 검증) | - |

- Body: 없음

#### 5. Response body

- HTTP `200`: Successful Response
- Content-Type: `application/json` (`MarkdownAgentRunStatusResponse`)

```json
{
  "apply_operation_id": "string",
  "base_version": 1,
  "document_id": "string",
  "error_code": "string",
  "id": "string",
  "result": {
  },
  "status": "string"
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
- 필터링: `workspace_id`, `user_id`

#### 8. 권한 규칙

- 올바른 내부 서비스 토큰을 가진 서비스만 호출할 수 있다.
- 요청에 포함된 workspace/user scope는 해당 route의 서비스 계층에서 추가 검증한다.

#### 9. 예시 요청/응답

```bash
curl -X GET "$PIPELINE/internal/agent/runs/<value>?workspace_id=<value>&user_id=<value>" \
  -H 'X-Internal-Token: <value>'
```

```json
{
  "apply_operation_id": "string",
  "base_version": 1,
  "document_id": "string",
  "error_code": "string",
  "id": "string",
  "result": {
  },
  "status": "string"
}
```

#### 10. 구현 파일

- 진입점: `pipeline/app/modules/agent_run/interfaces/http/routes.py`
- 기계 판독 계약: `pipeline/api-specs/openapi.yaml` (`operationId: get_markdown_agent_run_internal_agent_runs__run_id__get`)

#### 연동

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/agent/repository/PipelineAgentRunStatusRequester.java`:118-120 (`app.agent.status-endpoint`, `X-Internal-Token`).
- 아웃바운드 호출: 없음.
- 미연동 표시: 없음.

[↑ 요약으로 돌아가기](#summary-get-internal-agent-runs-run-id)

</details>

질의 답변은 평가자가 활성화된 경우에만 `query_evaluating`(답변 검토)을 평가 호출 직전에 발행하고, 검토 후 재작성하는 경우 `answer_retrying`을 발행합니다. 일반 대화 등 평가자를 호출하지 않는 경로에는 검토 단계를 표시하지 않습니다.

첫 문답은 최종 답변 이벤트를 먼저 전송한 뒤 같은 모델로 짧은 채팅 제목을 생성합니다. 제목 생성은 다음 문답 처리를 막지 않는 별도 작업으로 실행하고, 정상 종료 시 진행 중인 제목 전송을 마친 뒤 producer를 닫습니다. 별도의 `session_title` 이벤트로 document 서비스에 저장하며, 실패해도 완료된 답변은 바뀌지 않습니다. 이전 대화가 있으면 제목을 다시 생성하지 않습니다. agent·query command의 선택 필드 `session_title_needed`가 `false`면 사용자가 제목을 정한 세션이므로 제목을 생성하지 않고 사용량 원장 행도 열지 않습니다. 키가 없거나(구 document-svc) boolean이 아니면 없는 것으로 보고 위 규칙대로 동작합니다.

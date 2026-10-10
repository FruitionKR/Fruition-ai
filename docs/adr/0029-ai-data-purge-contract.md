# 0029. AI 데이터 파기 계약과 보관 예외

## Status
Accepted

## Context
탈퇴한 사용자와 영구 삭제된 워크스페이스의 AI 데이터(위키, 스킬, 에이전트 기록, 질의 원문이 담긴 작업 기록, S3 객체)는 파기돼야 한다(Fruition-ai#61). 파기 API는 만들었지만 호출하는 곳이 없었고, 점검에서 다음 결함이 나왔다.

- 파기한 워크스페이스로 재전달되거나 늦게 도착한 편집 이벤트(`document.edit.event`)와 `document_deleted` command가 `document_derived_state`·`wiki_source_tombstones` 행을 되살린다(Fruition-ai#70). 두 경로는 사용자 정보가 없거나 작업 journal을 거치지 않아 `reject_purged`로 막히지 않았다.
- 워크스페이스 파기가 S3 사용자 prefix(`wiki/{user_id}/{workspace_id}/`)를 DB 행에서 모았다. 사용자를 먼저 파기했거나 lint 산출물만 남은 사용자는 DB에 흔적이 없어 객체가 남는다(Fruition-ai#74).
- 파기가 실행 로그(`pipeline-runs/{run_id}/pipeline.log`)도 지우려 했지만 AI의 S3 역할에는 그 prefix 삭제 권한이 없다.

## Decision
- **API 의미**: `POST /internal/ai/purge/workspaces`·`/users`는 멱등이다. S3 객체를 먼저 지우고 DB 행은 범위 단위 한 트랜잭션으로 지운다(DB를 먼저 지우면 S3 실패 뒤 지울 키를 찾을 수 없다). 삭제에 앞서 `ai_purged_scopes`에 범위를 남기고, 재전달된 작업은 이 기록으로 거절한다. 거절 대상에는 작업 journal을 거치는 command, 편집 이벤트(건너뜀), `document_deleted` command(`ScopePurgedError`로 폐기)가 모두 포함된다. `document_deleted`는 성공 처리하지 않고 기존 폐기 경로의 실패 이벤트로 확정한다. 행을 만들지 않고 offset이 전진해 재시작 루프가 생기지 않는다. 확인은 `workspace_purged(conn, workspace_id)` 하나를 공유한다.
- **호출자와 순서**: Access가 Document 파기가 성공한 뒤 AI 파기를 부른다. Document 파기가 한 트랜잭션으로 미발행 outbox를 지우므로 "보내지 않은 명령이 남은 채 AI 파기가 끝나는" 경합이 없고, 이미 나간 메시지는 `ai_purged_scopes`가 거절한다. Document는 AI를 알 필요가 없다.
- **실행 로그는 지우지 않는다**: 로그에는 단계·ID·개수·소요 시간·오류 메시지만 있고 문서 본문이 없다. `pipeline-runs/` prefix의 30일 만료 lifecycle(Fruition-flatform)로 정리한다. S3 이전 버전도 30일 뒤 만료되고, 이미 발행된 Kafka 메시지는 최대 72시간 보관된다.
- **`ai_model_usage`는 정산용으로 남긴다.** 보관 기간은 법률 검토 뒤 정한다.
- **사용자 prefix는 S3 목록으로 찾는다**: `wiki/`를 구분자 `/`로 나열한 하위 prefix마다 `wiki/{p}/{workspace_id}/` 아래를 지우고, `wiki/{workspace_id}/`도 지운다. DB에서 사용자 목록을 만드는 코드는 제거한다.

## Alternatives
- **호출 순서 A, Document가 동기로 AI를 호출**: Document가 AI를 알아야 하고 Document 파기가 AI 장애에 묶인다. 기각.
- **호출 순서 C, Kafka 파기 이벤트**: 소비 지연 동안 파기 완료를 알 수 없고, 실패 재시도와 완료 표시를 호출자가 확인하기 어렵다. 동기 API와 Access의 backoff 재시도가 더 단순하다. 기각.
- **AI 역할에 `pipeline-runs/*` 삭제 권한 부여**: 문서 본문이 없는 로그를 위해 권한을 넓히고 사용자별 로그 색인을 유지해야 한다. lifecycle 일괄 만료로 충분하다. 기각.
- **DB 합집합으로 사용자 prefix 수집(기존 방식)**: 사용자를 먼저 파기했거나 lint 산출물만 있는 사용자를 놓친다. 기각.

## Consequences
- 파기 요청이 와도 실행 로그는 최대 30일 남는다. 개인정보 처리 안내에 이 보관 예외를 반영해야 한다.
- 워크스페이스 파기 시 `wiki/` 최상위 prefix를 나열하므로 비용이 사용자 수에 비례한다(`ponytail:` 주석). 사용자가 매우 많아지면 객체 키 구조를 바꿔야 한다.
- 배포 순서는 flatform(lifecycle·권한) → AI(이 변경) → Document#88 → Access#29다.

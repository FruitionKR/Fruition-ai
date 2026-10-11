# 0032. 동기 경로는 호출자보다 짧은 마감을 두고, 재전달된 실행 중 작업은 기다리며, 문서 삭제 정리는 비동기로 일원화한다

## Status
Accepted

## Context
- **시간 예산(Fruition-ai#75)**: Skill 게시·수정(document 60초), 위키 스키마 preview·drafts(60초), 채팅 음성 전사(ALB idle 120초), document→converter 호출(READ_TIMEOUT 900초)은 호출자가 먼저 포기한다. 그런데 AI는 LLM 180초 × 재시도 2처럼 훨씬 오래 실행해, 호출자가 포기한 뒤에도 과금되고 결과(Skill 게시, 스키마 초안)를 저장했다. 사용자가 다시 시도하면 이중 과금되거나 결과가 중복됐다.
- **재전달(Fruition-ai#76)**: KEDA가 worker를 늘리면 Kafka 리밸런스가 일어난다. 소비자는 수동 커밋이고 리밸런스 리스너가 없어, 커밋되지 않은 실행 중 메시지가 새 소유자에게 다시 전달된다. 새 worker는 원래 worker가 쥔 `pg_try_advisory_lock`을 얻지 못해 `TaskAlreadyExecutingError`를 받고, 소비 루프가 이를 재시도할 수 없는 실패로 보고 `raise`해 프로세스가 종료됐다. 재시작해도 같은 메시지를 받아 CrashLoopBackOff가 이어졌고, 부하로 확장한 바로 그때 처리량이 줄었다. 완료된 run이 재전달되면 저장된 결과를 돌려주면서도 세션 제목 LLM을 다시 호출해 과금했다.
- **문서 삭제 정리(Fruition-ai#77)**: Agent 복구의 `delete_document`는 document가 AI의 `POST /tasks/documents/{id}/cancel`을 동기로 불러 정리 완료를 확인했다. 취소 범위는 workspace+user의 run인데 완료 판정은 문서 전체(다른 사용자의 run, `document_wiki_links`, `source_blocks`) 기준이라, 다른 사용자의 파생 데이터가 있으면 `cleanup_complete=false`가 영구히 나와 503이 반복되고 복구가 `rollback_failed`로 끝났다. 일반 문서 삭제는 이미 `document_deleted` command로 비동기 정리한다.

## Decision
1. **짧은 동기 경로는 호출자 timeout보다 짧은 AI 전체 마감을 둔다.** Skill 게시·수정과 위키 스키마 preview·drafts는 55초, 음성 전사는 호출자가 `deadline_seconds`로 정한다(document는 채팅에 100초를 보내고, 회의 녹음 파일은 보내지 않아 공급사 단계마다 최대 900초), converter는 대기열 대기를 포함한 요청 전체 840초다. 마감은 `ensure_task_active()`와 같은 확인 지점(`app/core/pipeline_control.py`의 `request_deadline`)으로 구현한다. 모델 호출 전에 남은 시간을 확인해 지났으면 호출하지 않고, 호출 중에는 공급사 timeout을 남은 시간으로 줄이며 내부 재시도는 끈다. 저장 직전에도 확인해 지났으면 Skill 게시와 스키마 초안 저장을 하지 않는다(`504 deadline_exceeded`). converter는 단계별 timeout을 남은 시간으로 줄이고 대기열 대기가 마감을 넘으면 `504`로 포기한다. Skill은 여기에 더해 document가 timeout 때 같은 `run_id`로 취소를 보내 journal이 남은 변경을 되돌린다(A+B).
2. **길이를 예측할 수 없는 작업은 동기 마감으로 줄이지 않고 비동기 작업으로 옮긴다.** 회의록은 후속 PR에서, PDF 변환은 별도 이슈에서 다룬다. 이번 변경은 두 경로를 건드리지 않는다.
3. **재전달된 실행 중 작업은 종료하지 않고 seek해 기다린다.** `TaskAlreadyExecutingError`를 받으면 해당 offset으로 `seek`한 뒤 5초(고정)를 쉬고 다시 poll한다. 원래 worker가 끝나면 같은 메시지가 저장된 결과로 재생돼 정상 커밋된다. 재생한 결과(`ReplayedResult`)로는 세션 제목 생성을 건너뛴다. 충돌한 쪽이 무작위 대신 고정 간격으로 물러나는 CSMA/CA와 같은 방식이다.
4. **문서 삭제 정리는 항상 `document_deleted` command로 워크스페이스 전체에 비동기 처리한다.** Agent 복구의 `delete_document`도 일반 삭제와 같은 경로를 쓰고, 동기 취소 API(`POST /internal/ai/tasks/documents/{document_id}/cancel`)와 `cleanup_complete` 확인은 없앤다. 대신 정리 뒤 늦게 도착하는 후속 작업이 데이터를 되살리지 못하게 한다. `document_deleted` 처리가 기록하는 `wiki_source_tombstones.deleted_at`(삭제마다 갱신)이 부모 ingest run의 생성 시각보다 늦으면 post_ingest는 모델을 호출하거나 개념 페이지·근거를 쓰지 않는다. 시각 비교라서 휴지통에서 복구해 같은 `document_id`로 다시 만든 ingest run은 막지 않는다. 확인은 삭제 정리와 같은 워크스페이스 잠금 안에서 한다.
5. Agent 복구의 재시도할 수 있는 도구 오류(429·5xx)는 `rollback_failed`로 확정하지 않고 예외로 올려 job backoff(최대 3회)에 맡긴다.

## Alternatives
- **리밸런스 리스너에서 처리 중인 메시지 완료를 기다리기(#76 B안)**: `on_partitions_revoked`에서 기다리면 리밸런스가 멈춰 있는 동안 같은 그룹의 모든 소비자 할당이 지연된다. 처리 시간이 분 단위(max_poll_interval 30분)인 작업에서 그룹 전체가 그만큼 멈추고, 기다리는 동안 heartbeat·세션 timeout과 겹쳐 연쇄 리밸런스를 만들 수 있다. 소유권을 잃은 뒤에도 실행이 끝나지 않는 경우를 어차피 락 충돌로 처리해야 해서 seek 경로가 더 필요하다. 기각.
- **동기 정리 확인 유지(취소 범위를 문서 전체로 넓히기)**: 다른 사용자의 진행 중 편입까지 취소해야 하고, 취소 범위(사용자)와 완료 판정(문서)의 범위 불일치가 교착을 만든 원인이라 범위를 맞추더라도 호출자가 정리 완료를 기다리는 구조가 남는다. 일반 삭제는 이미 비동기이므로 두 경로를 둘 이유가 없다. 기각.
- **호출자 timeout만 늘리기(C안, #75)**: ALB idle 120초에 묶인 경로에는 쓸 수 없고, 길이를 예측할 수 없는 작업은 어떤 값으로 늘려도 같은 문제가 남는다. 기각.
- **호출자 취소만 두기(B안, #75)**: 취소는 호출자가 포기한 뒤에야 도착해 그 전의 비용을 막지 못하고, journal이 없는 경로(위키 스키마)는 되돌릴 대상도 없다. 단독으로는 기각하고, journal이 있는 Skill에서만 A의 보완으로 쓴다.
- **삭제 tombstone을 영구 차단으로 두기**: 휴지통에서 복구한 문서를 같은 `document_id`로 다시 ingest할 수 없게 된다. 기각.
- **재전달 대기 간격을 지수 backoff·지터로 하기**: 충돌 상대가 하나(원래 worker)이고 대기 중에도 poll을 계속하므로 단순한 고정 간격으로 충분하다. 필요해지면 바꾼다.

## Consequences
- 후속: 회의록은 `ai.maintenance.command`의 `kind: meeting_notes` 비동기 작업으로 옮겼고 `POST /meeting-notes/preview`는 삭제했다. 동기 마감은 두지 않고 공급사 timeout에 맡기며, 실패는 `MEETING_NOTES_*` 코드로 알린다([음성·회의록](../api/speech.md#회의록-초안)).
- 마감을 넘기면 작업이 실패하고 같은 `run_id`로 재시도할 수 없다(새 `run_id` 필요). 이전보다 이중 과금과 중복 결과가 줄어든다.
- 마감 중인 LLM 호출은 내부 재시도 없이 한 번만 시도한다. 일시 오류는 사용자의 재시도로 넘어간다. 공급사 timeout은 연결·읽기 단위라 마감을 약간 넘길 수 있고, 호출 뒤 확인이 결과 저장을 막는다.
- 리밸런스로 재전달된 실행 중 메시지는 원래 worker가 끝날 때까지 해당 파티션 소비를 5초 간격으로 확인하며 기다린다(poll을 계속하므로 max_poll_interval을 넘지 않는다). worker가 크래시 루프에 빠지지 않는다.
- 문서 삭제 정리는 비동기이므로 Agent 복구가 `delete_document` 직후 정리 완료를 확인하지 않는다. document는 동기 사전 확인을 제거해야 하고, AI를 먼저 배포한다(AI는 더 이상 `cleanup_complete`를 요구하지 않는다).
- 한계: 삭제 뒤 문서를 복구해 다시 ingest한 run이 끝나면 tombstone이 지워지므로, 그 뒤에야 도착하는 삭제 전 ingest의 post_ingest는 막지 못한다. 이 post_ingest는 복구된 문서의 이전 ingest 근거를 개념 페이지에 덧붙이는 정도이며, 문서가 다시 살아 있으므로 데이터 유실·잔존 문제는 아니다.
- lint·restore_ingest·restore_lint는 문서 삭제 뒤에도 문서 데이터를 새로 만들지 않는다(lint는 정리 뒤 상태를 읽고 같은 워크스페이스 잠금으로 직렬화되며, restore는 `wiki_source_tombstones`로 삭제된 문서의 페이지·링크를 되살리지 않는다). ingest는 같은 파티션(문서 ID 키)에서 `document_deleted`와 순서가 보장된다.

# 0030. 동기 HTTP 호출의 사용량 귀속 ID는 X-Request-Id 헤더로 받는다

## Status
Accepted

개정 2026-10-11: Skill `POST /skills/tasks`의 본문 `run_id` 예외를 명시했다.

## Context
모델 호출 사용량은 `run_id`로 귀속해 원장(`ai_model_usage`)에 남긴다(Fruition-ai#62). 동기 HTTP 계약은 `run_id`를 `X-Request-Id` 헤더로 받는 것으로 정했다. 그런데 Fruition-document가 `run_id`를 본문이나 쿼리로 보내 회의록 API는 422를 돌려주고, 나머지 호출은 귀속되지 않은(`unattributed`) 행으로 남았다(Fruition-document#87, #94). 계약이 문서화돼 있지 않아 호출자마다 다르게 읽었다.

## Decision
- 모든 동기 HTTP 호출은 사용량 귀속 `run_id`를 `X-Request-Id` 헤더로 받는다. WebSocket 핸드셰이크와 converter(Fruition-ai#82)도 같다.
- Kafka command는 본문에 이미 `run_id`가 있으므로 그대로 본문에서 읽는다.
- **예외: Skill `POST /skills/tasks`.** 이 경로는 Kafka command와 같은 작업 journal 명령(`SkillTaskRequest`: `run_id`·`kind`·`workspace_id`·`user_id`·`skill_id`·`payload`)을 HTTP로 실행한다. `run_id`는 journal이 작업을 식별·멱등 처리하는 명령 필드이므로 journal 실행기(`postgres_task_journal.execute`)가 명령 본문으로 `usage_scope`를 만들어 본문의 `run_id`가 귀속에 쓰이고(응답에도 `run_id`를 돌려준다), `X-Request-Id`는 읽지 않는다. 호출자는 Fruition-document `PipelineSkillRequester.task()`다. 명령 본문과 귀속 ID가 갈라지지 않게 이 본문 필드를 헤더로 바꾸지 않는다. 같은 `/skills` 아래여도 journal을 거치지 않는 `POST /skills/draft-from-runs/preview`는 헤더를 쓴다.
- 헤더가 없거나 형식이 틀린 호출은 기존대로 `unattributed`로 기록한다. 본문·쿼리의 `run_id`는 읽지 않는다.

## Alternatives
- **헤더와 본문을 모두 받기**: 호출자 오류를 가리고 우선순위·충돌 규칙이 필요한 호환 레이어가 된다. 기각.
- **모두 본문으로 통일**: WebSocket과 multipart 업로드(converter)는 본문 필드로 싣기 어색하고, 이미 확립된 헤더 계약을 깨 호출자를 모두 고쳐야 한다. 기각.

## Consequences
- 호출자(Document 등)가 본문·쿼리의 `run_id`를 `X-Request-Id`로 옮겨야 한다(Fruition-document#87, #94).
- 새 동기 엔드포인트도 별도 합의 없이 같은 헤더를 쓴다. 귀속 실패는 에러가 아니라 `unattributed` 행으로 드러난다.

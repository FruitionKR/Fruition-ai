# AI 스킬 분석 outbox

`ANALYTICS_ENABLED=false`가 기본이며 false일 때 기록과 relay가 모두 멈춥니다.
`POSTHOG_PROJECT_TOKEN`은 공개 프로젝트 토큰(`phc_` 접두사 및 영숫자/밑줄/하이픈)만 허용합니다.
비어 있거나 개인 키 등 다른 형식이면 relay만 시작하지 않으며 키 없이도 기록/테스트가 가능합니다.
`ANALYTICS_ENVIRONMENT`는 `local`(기본), `staging`, `production`, `APP_VERSION`은 배포 이미지 버전입니다.
`POSTHOG_HOST` 기본값은 `https://us.i.posthog.com`입니다. HTTPS origin(443)을 허용하며 사용자정보·경로·쿼리를 금지합니다.
`local` 환경에서는 mock용 HTTP `localhost`, `127.0.0.1`, `::1`, `host.docker.internal`과 포트도 허용합니다.
환경/활성화 값 오류는 기록·relay를 끄고, 토큰/호스트 오류는 relay만 끕니다. 설정값이나 예외 원문 없이
`analytics_config_invalid` 경고를 남기며 FastAPI 기동과 스킬 저장은 계속됩니다. 토큰은 Settings repr에서도 숨깁니다.

기존 `pipeline/db/ai_schema.sql` 멱등 migration에 `analytics_outbox`가 포함됩니다.
운영 migration 적용 및 runtime role의 SELECT/INSERT/UPDATE 권한을 확인한 뒤 활성화합니다.
스킬 생성이 먼저 트랜잭션을 시작하고, outbox INSERT는 같은 연결의 savepoint 안에서 실행됩니다.
INSERT 장애는 payload/예외 원문 없는 `analytics_outbox_insert_failed` 경고를 남기고 제품 변경은 커밋합니다.
이 fail-open 경로는 분석 누락을 허용하므로 경고를 모니터링해야 합니다.

API lifespan이 relay를 시작/종료합니다. API가 worker가 생성한 공유 DB 행도 발송하므로 별도 worker relay는 없습니다.
API 프로세스가 반드시 실행 중이어야 발송됩니다. 여러 API replica는 `FOR UPDATE SKIP LOCKED`로 행을 나눕니다.
HTTP 타임아웃 5초, 최대 8회, 지수 backoff(상한 3600초)를 사용합니다.
전송 후 DB 커밋 전에 중단되면 동일 payload를 재전송하는 at-least-once 방식입니다.
종료는 진행 중 요청을 취소하고 DB 트랜잭션을 롤백합니다. 이벤트 UUID와 발생 UTC 시각은 바뀌지 않습니다.
UUID는 NAMESPACE_URL의 `fruition.analytics.v1:skill_created:{skill_id}` UUIDv5입니다.

허용 속성은 `event_id`, `schema_version=1`, `event_source=worker`, `environment`, `app_version`,
`skill_id`, `scope`, 원 요청자 `user_id`, 팀 스킬의 `workspace_id`와 `$groups.workspace`뿐입니다.
일반 속성 `workspace_id`로 유료 Group analytics 없이도 workspace를 필터링할 수 있습니다.
`distinct_id`도 원 요청자입니다. 개인 스킬은 workspace 일반 속성과 그룹을 모두 생략합니다.
이름·지침·본문·URL·오류 원문을 builder에 전달할 수 없습니다.

운영자가 다음 SQL로 상태를 확인할 수 있습니다. 로그 `analytics_relay_delivery_failed state=dead`도 경보 대상입니다.

```sql
SELECT state, count(*), min(created_at) FROM analytics_outbox GROUP BY state;
```

원인을 수정하고 승인된 dead 행만 `state='pending', attempts=0, next_attempt_at=now()`로 바꿔 재처리합니다.
payload를 수정하지 않습니다. sent 행 보관/정리 주기는 운영 정책에서 정해야 합니다.

테스트는 `ANALYTICS_TEST_DSN`에 전용 PostgreSQL을 지정하고 `PYTHONPATH=pipeline pytest pipeline/tests/test_analytics.py`로 실행합니다.
무작위 임시 스키마를 생성/삭제하며 실제 migration을 두 번 적용합니다. HTTP는 MockTransport만 사용합니다.
실제 PostHog 전송, 배포, 운영 migration 및 실데이터 검증은 이 변경에서 실행하지 않았습니다.

## Agent 질의 실패 계약

실제 `chat_answer` 실행 직전 `agent_runs.result.action`을 저장합니다. 조회/생성 실패나 프로세스 중단 후
Kafka 재전달에서도 기존 실패 envelope(`run_id`, `status=failed`, `request.message_context`, `error` 코드)에
`payload: {"action": "chat_answer"}`를 실어 Document가 `query_failed`를 분류할 수 있습니다.
원 command 해시가 일치하고 상태가 `failed`인 run만 사용합니다. 편집·스킬·라우팅 전 실패와 취소는
이 marker를 보내지 않습니다. 질문/답변/오류 원문을 분석 outbox로 추가하지 않습니다.

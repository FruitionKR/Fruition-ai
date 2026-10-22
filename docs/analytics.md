# AI 스킬 분석 outbox

`ANALYTICS_ENABLED=false`가 기본이며 false일 때 기록과 relay가 모두 멈춥니다.
`POSTHOG_PROJECT_TOKEN`이 비어 있으면 relay만 시작하지 않습니다. 키 없이도 기록/테스트가 가능합니다.
`ANALYTICS_ENVIRONMENT`는 `local`(기본), `staging`, `production`, `APP_VERSION`은 배포 이미지 버전입니다.
`POSTHOG_HOST` 기본값은 `https://us.i.posthog.com`입니다. 개인 API 키는 사용하지 않습니다.

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
`skill_id`, `scope`, 원 요청자 `user_id`, 팀 스킬의 `$groups.workspace`뿐입니다.
`distinct_id`도 원 요청자입니다. 개인 스킬은 제품 DB에서 workspace가 없으므로 그룹을 보내지 않습니다.
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

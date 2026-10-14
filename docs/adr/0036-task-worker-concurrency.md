# 0036. ai task worker는 key별 순서를 지키며 여러 메시지를 동시에 처리한다

## Status
Accepted

## Context
- flatform ADR-0006(2026-08, 비동기 AI 작업)은 AI 작업을 Kafka command로 보내도록 정했지만 worker 하나가 동시에 몇 건을 처리할지는 정하지 않았다. `task_worker.consume()`은 `max_poll_records=1`로 한 건을 받아 처리가 끝날 때까지 기다린 뒤 다음 poll을 했다.
- 그래서 pod 하나는 질의 하나만 처리한다. KEDA 최대 4 pod이면 클러스터 전체의 동시 질의가 4건이고, 나머지 사용자는 앞선 질의 뒤에서 기다린다. worker의 시간 대부분은 LLM 응답 I/O 대기다.
- Kafka key는 query·agent command가 `session_id`, `ai.maintenance.command`의 lint·restore·meeting_notes가 `workspace_id`다. 같은 key의 작업은 순서를 지켜야 한다(한 세션의 다음 질문은 앞선 답변의 대화 맥락에 기대고, 같은 워크스페이스의 maintenance 작업은 서로 데이터를 바꾼다).
- journal(`postgres_task_journal`)은 run_id 단위로 실행을 멱등하게 한다. 실행 중인 run이 재전달되면 `TaskAlreadyExecutingError`가 나고, 끝난 run은 저장된 결과를 재생한다(ADR 0032).

## Decision
1. worker 하나가 `AI_TASK_CONCURRENCY`(기본 1)건을 동시에 처리한다. 기본값 1은 이전 동작과 같다. 블로킹 handler는 `max_workers=N`인 전용 `ThreadPoolExecutor`에서 실행하고, `asyncio.to_thread`와 같게 `copy_context()`로 usage scope·취소 scope contextvars를 넘긴다.
2. **같은 Kafka key는 동시에 실행하지 않고 offset 순서로 실행한다.** 다른 key는 N건까지 병렬이다. key가 없는 메시지는 순서 제약이 없다. 이 규칙과 offset 추적은 Kafka·asyncio를 모르는 `OrderedDispatcher`가 맡고, `consume()`은 fetch·실행·발행·커밋만 한다.
3. **커밋은 partition별로 "그 offset과 그 앞에서 fetch한 모든 offset이 끝난" 지점까지만 한다.** 뒤 offset이 먼저 끝나도 앞 offset이 끝나기 전에는 커밋하지 않는다. 이벤트 발행(`send_and_wait`)이 끝난 뒤에 완료로 표시한다. 크래시·리밸런스로 끝났지만 커밋하지 못한 메시지가 재전달되면 journal이 저장된 결과를 재생하고 같은 `event_id`의 이벤트를 다시 발행한다. 소비 측은 `event_id`로 중복을 거르므로 무해하다. 재생한 결과로는 세션 제목을 다시 만들지 않는다(ADR 0032).
4. **용량은 실행 중 + 같은 key 뒤에서 대기 중인 메시지 수로 센다.** 가득 차면 할당된 partition을 `pause`한 채 `getmany`만 계속 불러 `max_poll_interval_ms`를 넘기지 않고, 자리가 나면 `resume`한다.
5. **`TaskAlreadyExecutingError`는 seek하지 않는다(ADR 0032의 seek 대기를 대체한다).** 같은 partition의 다른 offset이 실행 중일 수 있어 seek하면 그 메시지들이 다시 fetch된다. 대신 그 메시지를 실행 중으로 둔 채 `REDELIVERY_BACKOFF_SECONDS`마다 다시 시도해 원래 worker가 끝나면 재생 결과로 완료한다. 그 offset 뒤의 같은 partition 커밋만 막히고, 다른 key·partition은 계속 처리한다.
6. **리밸런스(`ConsumerRebalanceListener`)**: partition이 회수되면 끝난 연속 offset까지 커밋하고, 시작하지 않은 대기 메시지는 버린다. 실행 중이던 메시지는 끝까지 처리해 결과를 발행하되 그 partition의 커밋은 건너뛴다. 새 소유자가 받는 재전달은 3·5와 같이 journal이 처리한다.
7. **재시도할 수 없는(non-durable) 실패는 이전처럼 프로세스를 종료한다.** 그 offset은 커밋하지 않는다. 한 건이 실패하면 새 fetch와 같은 key 뒤 대기 메시지의 시작을 멈추고, 다른 in-flight 작업은 끝까지 처리해 발행·커밋한 뒤 예외를 올린다. 중간에 취소하면 이미 과금된 호출의 결과를 버리게 되고, 끝난 작업은 어차피 journal이 보존하기 때문이다. 재시작 후 Kafka가 실패한 offset부터 재전달한다. durable 실패(failed 이벤트를 발행하고 커밋)는 그대로다.
8. SIGTERM·SIGINT은 새 fetch를 멈추고, 시작하지 않은 대기 메시지는 두며, in-flight 작업이 기존 마감 안에서 끝나기를 기다린 뒤 커밋하고 종료한다. 재전달 대기 중인 메시지는 대기를 포기한다(커밋하지 않으므로 재전달된다).

## Alternatives
- **KEDA만 조정(pod 수·max 늘리기)**: pod는 LLM I/O 대기에 대부분 놀고 있어 동시성을 pod 수로 사는 것은 비싸다. 리밸런스가 잦아져 재전달 대기도 늘고, 클러스터 크기만큼 DB 연결·메모리가 같이 늘어난다. 기각.
- **partition 수만 늘리기**: 동시성의 상한이 `pod 수 × 1`로 남고, partition 수는 줄일 수 없으며 늘리면 key→partition 매핑이 바뀌어 순서 보장이 한 번 깨진다. pod 안의 동시성 문제는 해결하지 못한다. 기각.
- **key를 무시한 병렬 처리**: 구현은 가장 단순하지만 같은 세션·워크스페이스의 작업이 뒤바뀌거나 겹쳐 대화 맥락이 틀어지고 maintenance 작업이 서로 덮어쓴다. 기각.
- **메시지마다 별도 consumer·프로세스**: 격리는 좋지만 그룹 멤버 수가 늘어 리밸런스가 잦고 프로세스·연결 비용이 크다. 기각.
- **non-durable 실패 시 다른 in-flight 작업 취소**: 종료는 빠르지만 이미 모델 비용을 쓴 작업의 결과를 버린다. 기각.

## Consequences
- pod당 동시 처리량이 N배가 된다. flatform은 query worker 8, agent worker 4, maintenance worker 1(`AI_TASK_CONCURRENCY`)로 설정한다. maintenance는 한 워크스페이스 key로 직렬화되고 작업이 무거워 1을 유지한다.
- `AI_TASK_MAX_POLL_INTERVAL_MS`는 그대로다. 용량이 차도 poll을 계속하므로 대기 때문에 넘지 않는다.
- 같은 key의 메시지가 fetch 용량을 차지한다. 한 세션이 연속으로 보낸 메시지는 N칸을 점유할 수 있으나, 다른 key는 자리가 나는 대로 이어서 처리된다.
- 커밋되지 않은 메시지는 in-flight여도 consumer lag에 남는다. KEDA `lagThreshold`는 pod당 N건이 항상 lag에 잡히는 것을 반영해 올려야 한다(기준: 기존 값 × N 이상, 또는 `lagThreshold = N`건 처리 중 + 대기 허용치).
- 동시 실행하는 handler는 같은 프로세스의 스레드이므로 handler가 쓰는 프로세스 전역 상태는 스레드 안전해야 한다. 현재 확인한 전역 상태(취소·마감·usage scope)는 contextvars다.
- 후속: `ingest_worker`는 Concept 잠금과 자체 소비 루프가 있어 이번 범위에서 제외했다. 같은 방식을 적용하려면 잠금 순서를 먼저 검토해야 한다.

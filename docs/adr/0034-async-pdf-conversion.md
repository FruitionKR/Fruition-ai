# 0034. PDF 변환은 Kafka command로 쪽 범위 단위 비동기 처리하고, 실패한 범위는 절반으로 나눠 다시 시도한다

## Status
Accepted

## Context
- document는 converter `/convert-source-batch`를 동기로 호출하며 900초(`READ_TIMEOUT`)를 기다렸다. converter 마감은 대기열 대기를 포함해 840초였다([ADR 0032](0032-time-budget-and-redelivery.md) 결정 1). 큰 PDF의 한 batch(10쪽)가 마감 안에 끝나지 못하면 실패했고, document가 최대 3회 다시 불러 매번 selective repair 모델 비용이 다시 들었다(Fruition-ai#88).
- ADR 0032 결정 2는 길이를 예측할 수 없는 작업을 동기 마감으로 줄이지 않고 비동기 작업으로 옮기기로 했다. 회의록은 `ai.maintenance.command`로 옮겼고 PDF 변환이 남았다.
- converter는 Kafka를 쓰지 않는 HTTP 서비스였고, 원본 PDF는 document가 만든 presigned URL(`CONVERTER_SOURCE_HOSTS` 허용 목록)로 범위 읽기했다.

## Decision
1. **command·event.** document가 `ai.convert.command`(env `CONVERT_COMMAND_TOPIC`)에 `kind: convert_batch` command를 쪽 범위(최대 50쪽) 단위로 발행하고, converter worker가 `ai.task.event`로 결과를 돌려준다. 계약은 [tasks.md](../api/tasks.md#pdf-변환-command)를 따른다.
2. **D1. 결과는 object storage로.** worker가 batch의 Markdown을 `tmp/convert/{workspace_id}/{document_id}/{start_page}.md`에 쓰고 이벤트에는 key만 싣는다. 이미지를 `data:` URI로 포함한 Markdown은 Kafka 메시지 한도를 쉽게 넘는다.
3. **D2. batch는 서로 독립.** document가 한 문서의 batch를 최대 3개 동시에 발행하고 쪽 순서로 병합한다. AI는 각 command를 독립으로 처리한다.
4. **D3. 원본은 storage에서 직접 읽는다.** worker가 `source_key`를 IRSA(AWS)·MinIO 자격(로컬)으로 `get_object(offset, length)` 범위 읽기한다. presigned URL은 쓰지 않는다. 1MiB 블록 16개(16MiB) 캐시와 PDF 단일 object 64MiB 한도는 유지하고 파일 전체를 버퍼링하지 않는다. `source_key`는 `sources/documents/{id}/original` 형식만 받는다(원본 key에는 workspace가 없다).
5. **D4. 경로는 하나.** AWS와 로컬이 같다. HTTP 경로 `/convert`·`/convert-source-batch`·`/health`와 `S3RangeReader` URL 로직, `CONVERTER_SOURCE_HOSTS`, `PDF_BATCH_CONCURRENCY`, `CONVERT_REQUEST_DEADLINE_SECONDS`, `PDF_PAGES_PER_BATCH`, `RESTORATION_TIMEOUT_SECONDS`(시도별 제한으로 대체)를 삭제했다. 호환 레이어는 없다.
6. **D5. 실패한 범위는 절반으로 나눈다.** 변환 시도가 실패하거나 시도별 제한(`CONVERT_ATTEMPT_TIMEOUT_SECONDS`, 기본 1500초)을 넘기면 범위를 절반으로 나눠 각각 다시 시도하고, 1쪽도 실패하면 batch가 실패한다. 나눈 결과는 쪽 순서로 이어 붙인다. 입력 거절(`input_rejected`: 암호·손상 PDF)과 취소는 나누지 않는다. command 전체는 `CONVERT_BATCH_DEADLINE_SECONDS`(기본 7200초) 안에 끝나야 하며, 지나면 더 나누지 않고 `deadline_exceeded`로 실패한다.
7. **멱등·취소.** run은 기존 작업 journal(`ai_task_runs`)에 batch `run_id`로 등록한다. 재전달된 완료 run은 저장된 결과를 돌려주고 변환하지 않으므로 selective repair 비용이 다시 들지 않는다. 실행 중 재전달은 converter가 seek해 기다린다(ADR 0036 이후 `task_worker`는 seek하지 않는다). `POST /internal/ai/tasks/{run_id}/cancel`이 run을 `cancel_requested`로 바꾸면 변환 중인 worker의 감시 스레드가 2초 주기 DB 확인으로 이를 보고 취소 `Event`를 켜며, 변환 하위 프로세스 그룹을 종료한다.
8. **Kafka key는 batch `run_id`.** `document_id`를 key로 쓰면 한 문서의 batch가 한 파티션에 몰려 D2의 병렬 처리가 막힌다. `max_poll_records=1`이라 batch는 소비자 하나가 한 번에 하나씩 처리한다.
9. **사용량 귀속.** 모델 사용량(`document_conversion`)의 `run_id`는 batch가 아니라 `parent_run_id`(document가 `convert:{document_id}`로 정산하는 변환 run)다.

시간 계산: 시도 하나는 최대 1500초, command 전체는 7200초다. 모든 범위가 끝까지 실패하는 최악(50쪽 → 99번 시도)도 마감이 7200초에서 끊는다. 진행 중 시도는 남은 시간으로 줄어 마감에 맞춰 종료된다. `max_poll_interval_ms`는 마감 + 600초(종료·정리 여유) = 7,800,000ms라 최악에도 리밸런스되지 않는다.

## Alternatives
- **작업 ID + 상태 조회(HTTP 폴링)**: converter에 상태 저장소와 조회 API를 새로 두어야 하고, 멱등·취소·재전달을 이미 갖춘 Kafka+journal 경로와 중복된다. 기각.
- **결과를 이벤트에 직접 싣기**: `data:` URI 이미지로 메시지 한도를 넘는다. 기각.
- **document_id를 Kafka key로**: 한 문서의 batch가 한 파티션에서 직렬 처리돼 동시 3개 발행이 의미가 없다. 기각.
- **presigned URL 유지(로컬 한정 폴백 포함)**: 경로가 둘이 되고 허용 호스트 설정이 남는다. IRSA·MinIO 자격으로 한 경로를 쓴다. 기각.
- **batch 크기만 줄이기·timeout만 늘리기**: 어떤 값이든 예측할 수 없는 길이에는 같은 문제가 남는다. 기각.
- **실패한 batch 전체를 같은 크기로 재시도**: 한 쪽 때문에 계속 실패하는 batch가 전체 비용을 반복한다. 절반으로 나누면 성공한 범위를 보존하고 실패 쪽을 분리한다. 기각.

## Consequences
- 큰 PDF가 동기 마감 때문에 실패하지 않는다. 같은 `run_id`의 재전달은 비용을 다시 쓰지 않는다.
- 나눠 재시도하면 시도마다 실패 전 비용이 이미 쓰였다. 나눈 범위 안에서 같은 쪽에 대한 selective repair가 반복될 수 있다.
- converter 전용 DB role에 journal 테이블 권한(`ai_task_runs` SELECT·INSERT·UPDATE, `ai_task_changes`·`ai_purged_scopes` SELECT)이 추가된다. 업무 테이블은 건드리지 않는다.
- converter는 `sources/` 읽기와 `tmp/convert/` 쓰기 권한이 필요하고 `tmp/convert/`는 lifecycle로 정리한다. HTTP 서버가 없어 probe는 ingest-worker처럼 프로세스 생존 수준이다.
- 취소 요청이 command보다 먼저 도착해도 cancel에 `command`를 실으면 run이 등록돼 실행이 막힌다. `parent_run_id`로는 취소할 수 없어 batch마다 취소해야 한다.
- worker는 SIGTERM을 받아도 진행 중인 command를 끝내고(최대 7200초) 종료한다. Pod의 `terminationGracePeriodSeconds`가 짧으면 강제 종료되고 같은 command가 재전달돼 다시 실행된다(시도 횟수 상한 `AI_TASK_MAX_INTERRUPTED_ATTEMPTS`).

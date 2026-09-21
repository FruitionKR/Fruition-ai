# AI 변경 기록

## 2026-09-21

- converter에 `/convert-source-batch`를 추가했습니다. 승인된 S3 HTTPS 호스트(`CONVERTER_SOURCE_HOSTS`)에서 1MiB Range 요청과 16MiB 캐시로 PDF를 읽고 pypdf로 페이지 묶음(`PDF_PAGES_PER_BATCH` 기본 10, 1~50)만 추출해 기존 복원 엔진에 전달합니다. 원본 전체를 디스크·메모리에 내려받지 않으며 단일 PDF object 읽기는 64MiB로 제한합니다. `PDF_BATCH_CONCURRENCY`(기본 1)로 동시 배치 수를 제한합니다.
- wiki 생성의 packet future를 설정된 worker 수만큼만 대기/실행하도록 바꿨습니다. 전체 packet future를 한꺼번에 만들지 않습니다.
- 결과 없이 중단된 agent 턴(`agent_runs.status=executing`)의 Kafka 재전달로 worker가 `Agent run is already executing`을 던지며 종료·재시작을 반복하던 문제를 수정했습니다. 실행 잠금을 보유한 상태의 `executing`은 고아 실행이므로 `failed`(`agent_turn_interrupted`)로 commit한 뒤 durable 실패 이벤트를 내고 offset을 진행합니다. 편집 부작용 중복을 막기 위해 재실행하지 않습니다.
- CI가 `converter/test_app.py`를 실행하도록 추가했습니다. 3GiB sparse 원본의 Range 페이지 추출·체크포인트 테스트 8개, 실제 PostgreSQL에서 interrupted 실패가 commit되는 테스트 1개를 포함해 pipeline 1,279개·하위 170개를 통과했습니다(격리 DSN·실모델 조건부 12개 skip). DB schema 변경은 없습니다.

## 2026-09-20

- ingest 최종 처리와 결과 저장이 같은 workspace의 PostgreSQL 잠금을 서로 다른 연결로 중복 획득해 자기 자신을 기다리던 오류를 수정했습니다.
- 같은 DB·workspace·run의 동기 중첩 호출은 현재 스레드가 가진 잠금을 재사용합니다. 다른 작업자나 run의 상호 배제와 최외곽 종료 시 잠금 해제는 유지합니다. DB schema 변경은 없습니다.
- 실제 PostgreSQL에서 기존 코드의 timeout을 재현했고, 중첩·동시 작업자·다른 run·예외 해제를 포함한 잠금 테스트 10개를 통과했습니다. 전체 AI 테스트는 1,265개·하위 테스트 170개 통과, 기존 조건부 테스트 24개 skip입니다. 새 PostgreSQL 잠금 테스트 4개는 모두 실행했습니다.

## 2026-09-17

- AWS Kafka mTLS에 맞춰 worker의 모든 producer·consumer에 공통 SSL 연결 설정을 적용했습니다. 인증서·호스트를 검증하며 설정 오류 시 평문으로 전환하지 않습니다.
- 로컬 PLAINTEXT 기본값은 유지합니다. TLS 설정·생성 지점 검증 테스트 5개를 통과했습니다.
- 플랫폼 KafkaUser/인증서 설정과 새 이미지를 함께 배포해야 하며, 인증서 갱신 후 클라이언트 순차 재시작이 필요합니다.

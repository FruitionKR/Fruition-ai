# AI 변경 이력

## 2026-10-22

- Agent 분석 분류의 선택적 DB 저장·조회 장애가 질의와 Kafka 실패 발행을 막던 문제를 재현하고 격리했습니다. 고정 경고만 남기고 저장·조회할 수 없는 실패 분류는 생략하며 제품 journal 실패·재시도는 유지합니다. PostgreSQL trigger 저장 거부와 조회 장애 회귀 3개를 추가해 성공 질의 유지, 일반 실패 발행·재전달, 임의 chat 분류 방지를 확인했습니다. Worker/Agent **467 passed, 15 skipped, 87 subtests passed**, 전체 pipeline **1998 passed, 7 skipped, 326 subtests passed**를 확인했습니다. 생략된 DB 테스트 파일은 전용 DSN 추가 후 **18 passed**로 보완했고 실서비스·모델 E2E 6개만 미실행입니다.

- 스킬 최초 게시와 같은 AI DB 트랜잭션에 `skill_created` outbox를 기록하고, savepoint로 분석 INSERT 장애를 격리해 제품 저장을 유지합니다.
- 기본 비활성 설정, API lifespan의 post-commit relay, 고정 UTC 시각/UUIDv5, 제한된 재시도와 dead 상태 관측을 추가했습니다. 공개 프로젝트 토큰이 없으면 발송하지 않습니다.
- 스킬 조회·목록·게시 응답에 최초 작성자 `created_by`를 추가해 Document가 `scope_type`과 함께 실행 귀속을 판단할 수 있습니다.
- Agent가 실제 `chat_answer` 실행 전에 갈래를 DB에 보존해 실패·재전달·프로세스 중단 복구 이벤트에도 `payload.action`을 포함합니다. 편집·스킬 실패와 취소는 질의로 집계하지 않습니다.
- 팀 스킬 이벤트에 일반 `workspace_id`와 `$groups.workspace`를 함께 넣고 개인 스킬은 둘 다 생략합니다. 공개 `phc_` 토큰만 relay를 활성화하며 잘못된 설정은 값 없는 경고와 함께 분석만 비활성화합니다. 로컬 HTTP mock(Docker gateway 포함)을 지원합니다.
- 회의록 병렬 호출 순서 가정 실패를 변경 전 dev와 현재 브랜치에서 재현했습니다. 호출 시작 순서는 보장되지 않으므로 정확한 batch 구성과 최종 결과 순서를 검증하도록 수정했고 제품 병합 순서는 유지합니다.
- 전용 PostgreSQL 16과 MockTransport로 전체 pipeline **1996 passed, 6 skipped(실서비스/모델 E2E만), 326 subtests passed**, converter **34 passed, 24 subtests passed**를 확인했습니다. document_restoration 테스트도 포함했습니다. Gitleaks PR 전체 diff 0건, tracked tree의 기존 redaction 테스트 fixture 1건은 합성 값으로 확인했습니다.
- 실데이터 전송·배포는 수행하지 않았습니다. 활성화 전 migration/권한 및 API 프로세스 설정이 필요하며 fail-open 기록 누락 경고와 dead 행을 모니터링해야 합니다.

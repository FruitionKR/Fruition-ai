# AI 변경 이력

## 2026-10-22

- 스킬 최초 게시와 같은 AI DB 트랜잭션에 `skill_created` outbox를 기록하고, savepoint로 분석 INSERT 장애를 격리해 제품 저장을 유지합니다.
- 기본 비활성 설정, API lifespan의 post-commit relay, 고정 UTC 시각/UUIDv5, 제한된 재시도와 dead 상태 관측을 추가했습니다. 공개 프로젝트 토큰이 없으면 발송하지 않습니다.
- 스킬 조회·목록·게시 응답에 최초 작성자 `created_by`를 추가해 Document가 `scope_type`과 함께 실행 귀속을 판단할 수 있습니다.
- PostgreSQL/MockTransport 기반 원자성·롤백·fail-open·중복·재시도·취소·수명주기와 스킬/API 회귀 테스트 221개 통과. 전체 테스트 1899개 통과, 69개 생략, 회의 요약 병렬 호출 순서 테스트 1개 실패를 확인했고, 변경 전 `origin/dev` 전체 실행에서도 동일 실패를 재현했습니다.
- 실데이터 전송·배포는 수행하지 않았습니다. 활성화 전 migration/권한 및 API 프로세스 설정이 필요하며 fail-open 기록 누락 경고와 dead 행을 모니터링해야 합니다.

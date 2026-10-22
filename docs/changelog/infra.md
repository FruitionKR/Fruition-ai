# 인프라 변경 이력

## 2026-10-22

- AI CI의 기존 격리 PostgreSQL 서비스에 `ANALYTICS_TEST_DSN`을 연결해 outbox 및 Agent 실패 이벤트 통합 테스트가 생략되지 않게 했습니다.
- 로컬 PostgreSQL 16에서 전체 pipeline 1996개와 converter 34개 테스트가 통과했습니다. 실서비스/모델 E2E 6개는 실행하지 않았고 배포·실제 분석 전송은 하지 않았습니다.

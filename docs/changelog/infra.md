# 인프라 변경 기록

## 2026-09-20

- AI CI에 격리 PostgreSQL 16 서비스를 추가해 실제 세션 간 advisory lock 회귀 테스트를 항상 실행합니다. DB를 사용하지 않는 mock 테스트만으로 중첩 잠금 오류가 통과하던 공백을 보완했습니다.
- CI 접속 정보는 일회성 시험 DB 전용이며 운영 자격 증명을 사용하지 않습니다. workflow actionlint 검증을 통과했습니다.

## 2026-09-17

- Pipeline·Converter 실행 사용자를 UID/GID 10001로 통일하고 HOME·캐시·작업 디렉터리의 필요한 쓰기 권한만 준비했습니다.
- 런타임 권한 smoke를 통과했으며 Converter의 읽기 전용 root filesystem도 확인했습니다. 전체 업무 이미지 빌드·기동은 별도 검증 대상입니다.

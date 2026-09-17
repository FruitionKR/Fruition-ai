# 인프라 변경 기록

## 2026-09-17

- Pipeline·Converter 실행 사용자를 UID/GID 10001로 통일하고 HOME·캐시·작업 디렉터리의 필요한 쓰기 권한만 준비했습니다.
- 런타임 권한 smoke를 통과했으며 Converter의 읽기 전용 root filesystem도 확인했습니다. 전체 업무 이미지 빌드·기동은 별도 검증 대상입니다.

# ai-svc 내부 API

[서비스 문서](../README.md)

Query·Agent·Wiki·Skill pipeline의 서비스 간·운영 API다. 로컬 base URL은
`http://localhost:8000`이며 Gateway나 프론트엔드에서 직접 호출하지 않는다.

| 도메인 | API 수 | 현재 Gateway 연결 |
|---|---:|---|
| [Agent](agent.md) | 12 | turn은 Kafka, run·artifact 조회·Tool 인가는 내부 HTTP; artifact register는 현재 운영 호출자 없음 |
| [Wiki Ingest](pipeline.md) | 7 | ingest는 Kafka, run 상태는 내부 HTTP, 나머지는 운영용 |
| [Query](query.md) | 1 | 공개 동기·비동기 모두 Kafka, 이 HTTP는 운영용 |
| [음성·회의록](speech.md) | HTTP 3 + WS 1 | AI 내부 API, 사용자 Gateway·화면은 별도 연동 |
| [Skills](skills.md) | 7 | 작업 실행·조회·설정은 내부 HTTP, draft·preview는 ai-svc 내부 기능 |
| [작업 취소](tasks.md) | 4 | 취소·상태·역순 복구를 내부 HTTP로 연결 |
| [Wiki](wiki.md) | 10 | 조회·페이지 관리는 내부 HTTP, lint·복구는 Kafka |
| [Wiki Schema](wiki-schema.md) | 4 | 내부 HTTP |

Agent 승인 run 5개와 실행 결과 기반 Skill 초안 API는 `AGENT_SKILLS_ENABLED=true`,
나머지 Skill API 6개는 `SKILL_API_ENABLED=true`일 때 노출된다. 이 문서는 선택 기능을
모두 켠 전체 계약을 기준으로 한다.

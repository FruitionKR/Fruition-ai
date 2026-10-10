# AI 구조

AI 저장소는 FastAPI pipeline, Kafka worker와 PDF converter를 소유합니다.

- `pipeline/app/modules/`: 도메인별 application·domain·infrastructure·interfaces
- `pipeline/app/workers/`: Kafka 작업 실행
- `pipeline/db/ai_schema.sql`: AI 저장소 스키마
- `pipeline/api-specs/openapi.yaml`: 내부 HTTP 계약
- `converter/`: 변환 HTTP 서버. pipeline의 문서 복원 코드·Rust 도구를 사용하므로 같은 저장소에서 빌드합니다.

pipeline 이미지 하나를 API와 각 worker가 command·환경변수를 달리해 사용합니다. converter는 별도 이미지이며, selective repair의 모델 호출을 기록하려고 ai_db의 `ai_model_usage`에만 씁니다. 접속 계정은 `ai_runtime`이 아닌 converter 전용 role(`AI_DATABASE_URL`)이고, 권한은 `ai_model_usage`의 INSERT, `finish_call`이 갱신하는 사용량·상태 컬럼의 UPDATE(귀속 컬럼 `run_id`·`workspace_id`·`user_id` 제외), `id`·`status`·`finished_at` 컬럼 SELECT뿐입니다. 다른 행의 사용량 값까지 막으려면 RLS가 필요합니다. role 생성과 CONNECT·schema USAGE는 platform `init-db-isolation.sh`가, 테이블 권한은 스키마 적용 경로(`migrate_ai_schema`·`ensure_ai_schema`)가 `AI_DB_CONVERTER_ROLE`로 부여합니다. AI는 ai_db와 자신의 S3 prefix를 소유하며, Document의 업무 변경은 내부 API를 통해 처리합니다.

[API 계약](api/README.md)과 [전체 통신 구조](https://github.com/FruitionKR/Fruition-flatform/blob/main/docs/architecture.md)를 참고하세요.

음성 대화는 명령용 마이크 발화 → STT → 기존 Agent → Query 답변에 한한 TTS로 연결한다. `speech`는 마이크 발화/실시간
전사와 음성 합성을 제공하며 `meeting_notes`는 확정 전사에서 근거 구간을 가진 회의록 초안을
생성한다. 마이크 오디오의 bytes 전송은 채팅 파일 첨부 기능을 뜻하지 않는다. 회의 발언은 수행 자료로만
사용하고 명령으로 실행하지 않는다. 실시간 오디오는 내부 WebSocket으로 처리하고
Agent 업무 실행은 기존 Kafka 경로를 유지한다. 사용자 인증·전사 저장·문서 적용은 호출 서비스의
책임이다. [음성 API와 연동 범위](api/speech.md)를 참고한다.

## 선택적 Jev 판단

Agent 라우팅, Query 근거 선택, ingest 개념 병합은 기능별 설정(`JEV_ROUTING_ENABLED`·`JEV_EVIDENCE_ENABLED`·`JEV_CONCEPT_MERGE_ENABLED`)과 `TYPESAFE_API_KEY`가 있을 때만 TypeSafe Jev로 판정한다. 설정이 꺼져 있거나 키가 없으면 기존 LLM·검색 경로를 쓴다. Jev 호출이 실패하거나 크레딧이 소진되면 그 요청을 기존 경로로 처리한다. 경로별 범위와 대체 조건은 [ADR-0027](adr/0027-jev-selective-judge.md)을 참고한다.

## 모델 사용량 책임

AI는 ai_db의 호출별 사용량 원장을 소유하고 내부 API로 모델·입력·출력·캐시·추론 토큰과 미확인 호출 수를 전달한다. 백엔드는 workspace 멤버 및 로그인 사용자 범위를 강제한다. 단가 관리·금액 환산·크레딧·세금 정책은 백엔드 책임이며 이 사용량 API는 청구 금액을 반환하지 않는다.

## AI 출력의 외부 이미지·링크 무력화

AI가 만든 Markdown은 저장·반환하기 전에 `app/core/ai_markdown_sanitizer.py`로 외부 이미지를 `외부 이미지(host)`, 외부 링크를 `텍스트 (host)` 글자로 바꾼다. 원문에 숨은 지시로 AI가 외부 이미지를 출력하면 화면을 열기만 해도 내용이 외부로 나가기 때문이다. 적용 범위는 채팅 답변, Agent 대화 응답·문서 생성, Agent 문서 편집(원문에 있던 링크는 유지), 회의록, 위키 페이지 저장이다. AI가 만든 Skill 본문에 외부 이미지·링크가 있으면 `external_link` 이슈로 막는다. 범위와 기각한 대안은 [ADR-0028](adr/0028-ai-output-external-link-neutralization.md)을 참고한다.

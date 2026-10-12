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
생성한다(Kafka `ai.maintenance.command`의 `kind: meeting_notes` 비동기 작업, 최악 약 720초). 마이크 오디오의 bytes 전송은 채팅 파일 첨부 기능을 뜻하지 않는다. 회의 발언은 수행 자료로만
사용하고 명령으로 실행하지 않는다. 실시간 오디오는 내부 WebSocket으로 처리하고
Agent 업무 실행은 기존 Kafka 경로를 유지한다. 사용자 인증·전사 저장·문서 적용은 호출 서비스의
책임이다. [음성 API와 연동 범위](api/speech.md)를 참고한다.

## 처리 시간 예산과 재전달

호출자가 기다리는 시간보다 AI의 최악 처리 시간이 길면 호출자가 포기한 뒤에도 모델 호출이 과금되고 결과가 저장된다. 그래서 짧은 동기 경로는 호출자 timeout보다 짧은 AI 마감을 둔다: Skill 게시·수정과 위키 스키마 preview·drafts 55초(document 60초), 음성 전사는 호출자가 보낸 `deadline_seconds`(채팅은 document가 100초를 보내며 ALB idle은 120초, 회의 녹음 파일은 보내지 않아 공급사 단계마다 최대 900초), converter 요청 전체 840초(document 900초, `/convert-source-batch`의 대기열 대기 포함). 마감이 지나면 모델 호출을 시작하지 않고 결과를 저장하지 않으며 실패한다. 길이를 예측할 수 없는 작업은 비동기 작업으로 옮긴다. 근거는 [ADR 0032](adr/0032-time-budget-and-redelivery.md)를 따른다.

Kafka 리밸런스로 실행 중인 작업이 다른 worker에 재전달되면 새 worker는 종료하지 않고 5초 간격으로 기다린다(동시 처리 이후에는 seek하지 않고 제자리 대기, 아래 Worker 동시 처리). 원래 worker가 끝나면 같은 메시지가 저장된 결과로 재생돼 커밋되며, 재생한 결과로는 세션 제목 생성 같은 부수 효과를 다시 실행하지 않는다. 문서 삭제 정리는 동기 취소 API 없이 `document_deleted` command로 워크스페이스 전체에 비동기 처리한다.

## Worker 동시 처리

Query·Agent·maintenance worker는 `AI_TASK_CONCURRENCY`(기본 1)건을 한 프로세스에서 동시에 처리한다. 같은 Kafka key(query·agent는 `session_id`, maintenance는 `workspace_id`)는 offset 순서로 하나씩 실행하고 다른 key는 병렬이다. offset은 앞선 fetch 분이 모두 끝난 지점까지만 partition별로 커밋하며, 용량이 차면 partition을 pause한 채 poll을 계속한다. 재전달된 실행 중 작업은 seek하지 않고 제자리에서 5초 간격으로 기다리고, 리밸런스 때는 끝난 연속 offset만 커밋한다. 재시도할 수 없는 실패는 다른 in-flight 작업이 끝난 뒤 프로세스를 종료해 재전달에 맡긴다. 근거는 [ADR 0036](adr/0036-task-worker-concurrency.md)을 따른다.

## 선택적 Jev 판단

Agent 라우팅, Query 근거 선택, ingest 개념 병합은 기능별 설정(`JEV_ROUTING_ENABLED`·`JEV_EVIDENCE_ENABLED`·`JEV_CONCEPT_MERGE_ENABLED`)과 `TYPESAFE_API_KEY`가 있을 때만 TypeSafe Jev로 판정한다. 설정이 꺼져 있거나 키가 없으면 기존 LLM·검색 경로를 쓴다. Jev 호출이 실패하거나 크레딧이 소진되면 그 요청을 기존 경로로 처리한다. 경로별 범위와 대체 조건은 [ADR-0027](adr/0027-jev-selective-judge.md)을 참고한다.

## 모델 사용량 책임

AI는 ai_db의 호출별 사용량 원장을 소유하고 내부 API로 모델·입력·출력·캐시·추론 토큰과 미확인 호출 수를 전달한다. 백엔드는 workspace 멤버 및 로그인 사용자 범위를 강제한다. 단가 관리·금액 환산·크레딧·세금 정책은 백엔드 책임이며 이 사용량 API는 청구 금액을 반환하지 않는다.

## AI 출력의 외부 이미지·링크 무력화

AI가 만든 Markdown은 저장·반환하기 전에 `app/core/ai_markdown_sanitizer.py`로 외부 이미지를 `외부 이미지(host)`, 외부 링크를 `텍스트 (host)` 글자로 바꾼다. 원문에 숨은 지시로 AI가 외부 이미지를 출력하면 화면을 열기만 해도 내용이 외부로 나가기 때문이다. 적용 범위는 채팅 답변, Agent 대화 응답·문서 생성, Agent 문서 편집(원문에 있던 링크는 유지), 회의록, 위키 페이지 저장이다. AI가 만든 Skill 본문에 외부 이미지·링크가 있으면 `external_link` 이슈로 막는다. 범위와 기각한 대안은 [ADR-0028](adr/0028-ai-output-external-link-neutralization.md)을 참고한다.

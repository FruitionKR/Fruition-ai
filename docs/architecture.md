# AI 구조

AI 저장소는 FastAPI pipeline, Kafka worker와 PDF converter를 소유합니다.

- `pipeline/app/modules/`: 도메인별 application·domain·infrastructure·interfaces
- `pipeline/app/workers/`: Kafka 작업 실행
- `pipeline/db/ai_schema.sql`: AI 저장소 스키마
- `pipeline/api-specs/openapi.yaml`: 내부 HTTP 계약
- `converter/`: 변환 HTTP 서버. pipeline의 문서 복원 코드·Rust 도구를 사용하므로 같은 저장소에서 빌드합니다.

pipeline 이미지 하나를 API와 각 worker가 command·환경변수를 달리해 사용합니다. converter는 별도 이미지입니다. AI는 ai_db와 자신의 S3 prefix를 소유하며, Document의 업무 변경은 내부 API를 통해 처리합니다.

[API 계약](api/README.md)과 [전체 통신 구조](https://github.com/FruitionKR/Fruition-flatform/blob/main/docs/architecture.md)를 참고하세요.

음성 대화는 명령용 마이크 발화 → STT → 기존 Agent → Query 답변에 한한 TTS로 연결한다. `speech`는 마이크 발화/실시간
전사와 음성 합성을 제공하며 `meeting_notes`는 확정 전사에서 근거 구간을 가진 회의록 초안을
생성한다. 마이크 오디오의 bytes 전송은 채팅 파일 첨부 기능을 뜻하지 않는다. 회의 발언은 수행 자료로만
사용하고 명령으로 실행하지 않는다. 실시간 오디오는 내부 WebSocket으로 처리하고
Agent 업무 실행은 기존 Kafka 경로를 유지한다. 사용자 인증·전사 저장·문서 적용은 호출 서비스의
책임이다. [음성 API와 연동 범위](api/speech.md)를 참고한다.

## 모델 사용량 책임

AI는 ai_db의 호출별 사용량 원장을 소유하고 내부 API로 모델·입력·출력·캐시·추론 토큰과 미확인 호출 수를 전달한다. 백엔드는 workspace 멤버 및 로그인 사용자 범위를 강제한다. 단가 관리·금액 환산·크레딧·세금 정책은 백엔드 책임이며 이 사용량 API는 청구 금액을 반환하지 않는다.

# AI 구조

AI 저장소는 FastAPI pipeline, Kafka worker와 PDF converter를 소유합니다.

- `pipeline/app/modules/`: 도메인별 application·domain·infrastructure·interfaces
- `pipeline/app/workers/`: Kafka 작업 실행
- `pipeline/db/ai_schema.sql`: AI 저장소 스키마
- `pipeline/api-specs/openapi.yaml`: 내부 HTTP 계약
- `converter/`: 변환 HTTP 서버. pipeline의 문서 복원 코드·Rust 도구를 사용하므로 같은 저장소에서 빌드합니다.

pipeline 이미지 하나를 API와 각 worker가 command·환경변수를 달리해 사용합니다. converter는 별도 이미지입니다. AI는 ai_db와 자신의 S3 prefix를 소유하며, Document의 업무 변경은 내부 API를 통해 처리합니다.

[API 계약](api/README.md)과 [전체 통신 구조](https://github.com/FruitionKR/Fruition-flatform/blob/main/docs/architecture.md)를 참고하세요.

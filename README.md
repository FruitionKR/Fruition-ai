# Fruition AI

Fruition에서 **문서를 지식으로 정리하고, 근거를 찾아 답하며, 문서 작업을 돕는 AI 서비스**입니다. 문서 변환부터 위키 생성, 질의응답, 편집 제안까지의 처리를 담당합니다.

> Fruition의 기획 배경, 주요 기능과 사용 화면은 [Fruition 서비스 소개](https://github.com/FruitionKR/Fruition-frontend#readme)를 확인하세요.

[서비스 구조](docs/architecture.md) · [실행·검증 안내](docs/script.md) · [API 계약](docs/api/README.md) · [전체 문서](docs/README.md)

## 어떤 일을 하나요?

| 기능 | AI 서비스의 역할 |
|---|---|
| 문서 변환 | PDF를 Markdown으로 변환하고 문서 구조를 복원합니다. |
| 위키 구성 | 문서에서 개념과 근거를 추출해 위키 페이지로 정리합니다. |
| 근거 기반 질의응답 | 문서·위키에서 관련 내용을 검색해 질문에 답합니다. |
| 문서 작업 지원 | Agent와 Skill을 통해 문서 생성·편집 제안을 처리합니다. |
| 음성·회의록 | 음성을 텍스트로 전사하고 회의록 초안을 생성합니다. Query 답변에는 음성 합성을 제공합니다. |

사용자 인증과 접근 권한은 Access가, 문서 관리와 결과 적용은 Document가 담당합니다. AI는 자체 데이터베이스(`ai_db`)와 저장소 영역을 관리하며, Document의 업무 데이터 변경은 내부 API를 통해 요청합니다.

## 저장소 구조

| 경로 | 내용 |
|---|---|
| `pipeline/` | FastAPI API와 Kafka worker, AI 기능 구현 |
| `pipeline/app/modules/` | 위키·검색·Agent·Skill·음성 등 기능별 모듈 |
| `pipeline/app/workers/` | 메시지를 받아 비동기 작업을 실행하는 worker |
| `pipeline/db/` | AI 데이터베이스 스키마와 관련 파일 |
| `pipeline/api-specs/openapi.yaml` | 내부 HTTP API 명세 |
| `converter/` | PDF 변환 전용 Kafka worker |
| `docs/` | 설계, 데이터 소유권, API와 운영 문서 |

**pipeline은 API와 worker가 같은 이미지를 사용하고, 실행 명령과 환경변수로 역할을 나눕니다.** converter는 별도 이미지이며 HTTP 서버가 없습니다. pipeline의 문서 복원 코드와 Rust 도구를 함께 사용하므로 AI 저장소 루트를 빌드 컨텍스트로 사용합니다.

## 개발 환경 준비

Python **3.12**를 사용합니다. 아래 명령은 AI 저장소 루트에서 시작합니다.

```bash
cd pipeline
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

새로 clone했거나 디렉터리를 옮겼다면 가상환경을 새로 만드세요. 기존 가상환경의 `pip`·`pytest` 실행 파일에는 이전 절대 경로가 남을 수 있습니다. 도구는 위 예시처럼 `.venv/bin/python -m ...` 형태로 실행합니다.

## 검증

`pipeline/`에서 기본 테스트를 실행합니다.

```bash
.venv/bin/python -m pytest -q --ignore=tests/modules/document_restoration
```

GitHub CI도 기본 pipeline 테스트를 실행합니다. 문서 복원 테스트는 별도 의존성이 필요하며, 실제 모델·외부 서비스 E2E는 기본 검증에 포함되지 않습니다. 전체 스택 E2E를 실행하려면 `LIVE_TASK_ENV_FILE=/절대/경로/.env`와 `RUN_LIVE_TASK_E2E=1`을 명시해야 합니다. 자세한 조건은 [실행·검증 안내](docs/script.md)를 확인하세요.

## 실행

API를 실행하기 전에 DB·Kafka·Redis·S3, 내부 API의 연결 정보와 사용할 모델의 API 키를 환경변수로 설정합니다. 필요한 설정은 [pipeline 상세 안내](pipeline/README.md)에 있습니다.

`pipeline/`에서 실행합니다.

```bash
.venv/bin/python -m uvicorn api:app --host 0.0.0.0 --port 8000
```

이 명령은 **API만 실행**합니다. 위키 편입·질의·Agent 등 비동기 기능에는 해당 Kafka worker와 연결 서비스가 함께 필요합니다. 전체 스택은 [Platform의 통합 실행 안내](https://github.com/FruitionKR/Fruition-flatform#readme)를 따라 실행하세요.

### 컨테이너 이미지 빌드

다음 명령은 **AI 저장소 루트**에서 실행합니다.

```bash
docker build -t fruition-pipeline:local pipeline
docker build -t fruition-converter:local -f converter/Dockerfile .
```

## 더 알아보기

| 궁금한 내용 | 문서 |
|---|---|
| Fruition은 어떤 서비스인가요? | [서비스 소개](https://github.com/FruitionKR/Fruition-frontend#readme) |
| AI 기능과 worker는 어떻게 연결되나요? | [서비스 구조](docs/architecture.md) |
| AI가 관리하는 데이터는 무엇인가요? | [데이터 소유권](docs/data-model.md) |
| 환경변수와 모델 설정은 어떻게 하나요? | [pipeline 상세 안내](pipeline/README.md) |
| 테스트·평가·실행 절차는 어디에 있나요? | [실행·검증 안내](docs/script.md) |
| 다른 서비스가 AI를 어떻게 호출하나요? | [API 계약](docs/api/README.md) |
| 전체 서비스는 어떻게 실행·배포하나요? | [Platform](https://github.com/FruitionKR/Fruition-flatform#readme) |

## 저작권 및 라이선스 / Copyright and License

**한국어**

저작권 (c) 2026 Fruition 팀. 모든 권리 보유.

Fruition 팀이 저작권을 보유하는 코드·문서·자산의 무단 사용을 금지합니다. 상업적·비상업적 목적의 사용·복제·수정·배포·재라이선스·판매에는 Fruition 팀의 사전 서면 허가가 필요합니다. 제3자 구성요소에는 각 라이선스가 적용됩니다. 적용 범위와 예외는 [LICENSE](LICENSE)를 참고하세요.

**English**

Copyright (c) 2026 Team Fruition. All rights reserved.

Unauthorized use of code, documentation, and assets copyrighted by Team Fruition is prohibited. Use, copying, modification, distribution, sublicensing, or sale for commercial or non-commercial purposes requires prior written permission from Team Fruition. Third-party components remain subject to their own licenses. See [LICENSE](LICENSE) for the scope and exceptions.

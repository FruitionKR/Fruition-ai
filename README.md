# Fruition AI

[한국어](#한국어) · [English](#english)

## 한국어

이 폴더 전체가 독립 GitHub 저장소 루트가 됩니다.

- `pipeline/`: FastAPI API, Kafka worker, ai_db 스키마·마이그레이션, API 명세
- `converter/`: PDF 변환 HTTP 서비스. pipeline의 문서 복원 코드와 Rust 도구를 함께 사용합니다.

Python 3.12 환경에서 실행합니다.

```bash
cd pipeline
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q --ignore=tests/modules/document_restoration
# AI 저장소 루트로 돌아와 이미지별 빌드
cd ..
docker build -t fruition-pipeline:local pipeline
docker build -t fruition-converter:local -f converter/Dockerfile .
```

pipeline 이미지는 API와 각 worker가 command·환경변수를 달리해 사용합니다. converter 빌드 컨텍스트는 AI 루트입니다. 환경변수와 사용법은 [pipeline 안내](pipeline/README.md)를 참고하세요. DB·Kafka·Redis·S3 및 내부 API는 외부 연결 정보로 주입합니다.

기존 가상환경은 경로 이동 시 pip·pytest 실행 파일의 절대 경로가 바뀌므로 새 clone에서는 위 명령으로 생성합니다. 기존 환경을 사용하는 경우 `.venv/bin/python -m ...` 형태로 실행합니다. 실제 전체 스택 E2E는 `LIVE_TASK_ENV_FILE=/절대/경로/.env RUN_LIVE_TASK_E2E=1`을 명시해야 합니다.

GitHub CI는 기본 pipeline 테스트를 실행합니다. 무거운 문서 복원 테스트와 실제 모델·서비스 E2E는 기본 검증에서 제외됩니다.

설계·API·데이터·실행 문서는 [docs 안내](docs/README.md)에서 관리합니다.

## English

This directory is the independent GitHub repository root.

- `pipeline/`: FastAPI API, Kafka workers, the ai_db schema and migrations, and API specifications.
- `converter/`: PDF conversion HTTP service that also uses pipeline document restoration code and Rust tools.

Use Python 3.12.

```bash
cd pipeline
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q --ignore=tests/modules/document_restoration
# Return to the AI repository root to build each image.
cd ..
docker build -t fruition-pipeline:local pipeline
docker build -t fruition-converter:local -f converter/Dockerfile .
```

The API and workers share the pipeline image with different commands and environment variables. The converter build context is the AI repository root. See the [pipeline guide](pipeline/README.md) for configuration and usage. Inject connection settings for databases, Kafka, Redis, S3, and internal APIs.

Create a new virtual environment after cloning: moving an existing environment changes the absolute paths embedded in pip and pytest entry points. When reusing an existing environment, invoke tools through `.venv/bin/python -m ...`. Live full-stack E2E tests require explicit `LIVE_TASK_ENV_FILE=/absolute/path/.env RUN_LIVE_TASK_E2E=1` settings.

GitHub CI runs the baseline pipeline tests. Heavy document restoration tests and live model/service E2E tests are excluded from baseline validation.

See the [documentation index](docs/README.md) for architecture, API, data, and execution documentation.

## 저작권 및 라이선스 / Copyright and License

**한국어**

저작권 (c) 2026 Fruition 팀. 모든 권리 보유.

Fruition 팀이 저작권을 보유하는 코드·문서·자산의 무단 사용을 금지합니다. 상업적·비상업적 목적의 사용·복제·수정·배포·재라이선스·판매에는 Fruition 팀의 사전 서면 허가가 필요합니다. 제3자 구성요소에는 각 라이선스가 적용됩니다. 적용 범위와 예외는 [LICENSE](LICENSE)를 참고하세요.

**English**

Copyright (c) 2026 Team Fruition. All rights reserved.

Unauthorized use of code, documentation, and assets copyrighted by Team Fruition is prohibited. Use, copying, modification, distribution, sublicensing, or sale for commercial or non-commercial purposes requires prior written permission from Team Fruition. Third-party components remain subject to their own licenses. See [LICENSE](LICENSE) for the scope and exceptions.

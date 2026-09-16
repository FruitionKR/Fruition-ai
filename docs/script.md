# AI 빌드·테스트·실행

AI 저장소 루트에서 시작합니다. Python 3.12를 사용합니다.

```bash
cd pipeline
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q --ignore=tests/modules/document_restoration
.venv/bin/python -m uvicorn api:app --host 0.0.0.0 --port 8000
```

API 실행에는 DB·Redis·Kafka·S3·내부 API 주소와 사용하는 모델의 키를 환경변수로 주입합니다. [pipeline 상세 안내](../pipeline/README.md)를 참고하세요.

```bash
# AI 저장소 루트에서 실행
docker build -t fruition-pipeline:local pipeline
docker build -t fruition-converter:local -f converter/Dockerfile .
```

문서 복원 테스트는 별도 의존성이 필요합니다. 실제 전체 스택 E2E는 `LIVE_TASK_ENV_FILE=/절대/경로/.env RUN_LIVE_TASK_E2E=1`을 명시해야 하며 기본 테스트에 포함되지 않습니다. 전체 서비스 기동은 platform의 통합 스크립트가 담당합니다.

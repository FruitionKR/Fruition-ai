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

## Markdown 편집 평가자 비교

`pipeline/`에서 실행한다. 생성·재시도에는 `OPENAI_API_KEY`, 별도 블라인드 평가에는 `GEMINI_API_KEY` 환경 변수가 필요하며 실제 모델 비용이 발생한다. 저장된 최초 초안은 합성 편집 사례이며, 재생 이후 호출만 새로 실행한다.

```bash
python evaluate_markdown_edit.py --baseline-ref d0527a060b38958ee945aa4b870bd6abe8abc713 \
  --replay evals/markdown_edit_evaluator/first_drafts.json \
  --workers 6 --output /tmp/markdown-edit-paired.json
python review_markdown_edit_results.py --results /tmp/markdown-edit-paired.json \
  --calibration evals/markdown_edit_evaluator/judge_cases.json \
  --output /tmp/markdown-edit-reviewed.json
```

기준 커밋은 이 저장소의 평가자 도입 전 코드다. 원본 저장소에서 측정한 기존 결과와 기준 커밋이 다르므로 동일한 수치를 보장하지 않는다. 기준 커밋을 포함한 Git 이력이 필요하다.

평가자 제거 대조군은 실험 코드에서만 사용하며 서비스에는 평가 우회 옵션이 없다. 별도 평가자는 버전 정보를 받지 않고 동일한 출력은 한 번만 평가한다. 성공 건수는 결과 반환·코드 검사·별도 모델 평가를 모두 통과한 경우만 센다. 평가자 자체의 통과율을 정답률로 세지 않는다.

초안을 새로 수집하려면 `--replay`를 빼고 `--repeat 3`으로 실행한다. [측정 결과와 한계](adr/0011-user-approved-markdown-edits.md#검증)를 함께 확인한다.

## 음성 대화·회의록 API 검증

`OPENAI_API_KEY`, `INTERNAL_CALLBACK_TOKEN`, `ACCESS_INTERNAL_BASE_URL`을 서버 환경에
주입한다. 기존 Access workspace 권한 조회가 가능해야 한다. 실제 모델 호출에는 비용이 발생한다.
`pipeline/`에서 실행한다.

```bash
python -m pytest -q tests/modules/speech tests/modules/meeting_notes
python -m uvicorn api:app --host 127.0.0.1 --port 8000
```

Agent 마이크 발화는 multipart가 아닌 오디오 bytes로 전사한다. 이는 채팅 파일 첨부 기능이 아니다.
명령용 마이크 발화의 전사만 기존 Agent 입력으로 전달한다. 회의 실시간 음성은 PCM16 mono 24 kHz와
명시적인 commit/finish를 사용한다. [요청 형식과 호출 순서](api/speech.md)를 참고한다.
Query 답변에서만 TTS를 호출한다. 회의 전사는 명령으로 실행하지 않고 회의록 작성 자료로 전달하며,
회의록은 초안 반환 후 기존 문서 승인 경로에 연결한다.
테스트는 가짜 제공자를 사용하며 원본 음성 파일이나 실제 비밀정보를 저장소에 추가하지 않는다.

## 모델 사용량 원장 적용

분리 AI 저장소의 `pipeline/`에서 migration 권한의 `AI_DB_MIGRATION_URL`을 주입하고 `python -m app.modules.wiki_ingestion.infrastructure.migrate_ai_schema`를 실행한다. 멱등 DDL이 `ai_model_usage`와 인덱스를 생성한다. 기존 runtime의 ai_runtime 권한에 INSERT/UPDATE/SELECT가 포함되어야 한다. 그 뒤 새 pipeline API·worker 이미지를 적용한다. 사용량 원장은 작업 취소 복구 대상이 아니며 이전 호출은 소급 기록하지 않는다.

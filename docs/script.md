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

편집 세부 분류기([ADR-0043](adr/0043-edit-detail-classification.md))는 실제 모델로 평가셋을 돌려 정답과 비교한다. 선택한 provider의 API 키가 필요하고 비용이 든다.

```bash
python evaluate_markdown_edit_detail.py --provider openai --model gpt-6-luna
python evaluate_markdown_edit_detail.py --provider openai --model gpt-6-luna --jev  # Jev 켬. TYPESAFE_API_KEY 필요, 대체 횟수를 jev_fallback_calls로 출력
```

## Jev 켬/끔 평가

라우팅·개념 병합·RAG 근거 선택에서 Jev를 켰을 때와 껐을 때를 같은 정답 세트로 비교한다. 정답 세트, 실행기, 채점 방식과 평가 전용 설정은 [`pipeline/evals/jev/README.md`](../pipeline/evals/jev/README.md)에 있다. 모든 평가의 LLM은 `gpt-6-luna` 하나로 고정하고, 채점은 Claude(`claude -p`)가 한다. 실제 모델 호출 비용이 들고, RAG 평가는 로컬 전체 스택과 `claude` CLI가 필요하다. PDF Converter 유형별 평가는 [`pipeline/evals/pdf/README.md`](../pipeline/evals/pdf/README.md)에 있다(원문 PDF는 저장소 밖에서 받아 만든다).

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

분리 AI 저장소의 `pipeline/`에서 migration 권한의 `AI_DB_MIGRATION_URL`을 주입하고 `python -m app.modules.wiki_ingestion.infrastructure.migrate_ai_schema`를 실행한다. 멱등 DDL이 `ai_model_usage`와 인덱스를 생성한다. 기존 runtime의 ai_runtime 권한에 INSERT/UPDATE/SELECT가 포함되어야 한다. `AI_DB_CONVERTER_ROLE`(예: `ai_converter`)을 함께 주입하면 같은 트랜잭션에서 converter 전용 role의 테이블 단위 UPDATE를 회수한 뒤 `ai_model_usage` INSERT, `finish_call`이 SET하는 컬럼의 UPDATE, `id`·`status`·`finished_at` 컬럼 SELECT를 부여한다. 비우면 부여를 건너뛰고 경고를 남기며, role이 없으면 적용이 실패한다. role은 platform `init-db-isolation.sh`로 먼저 만든다. 그 뒤 새 pipeline API·worker 이미지를 적용한다. 사용량 원장은 작업 취소 복구 대상이 아니며 이전 호출은 소급 기록하지 않는다.

## 영구 삭제가 쓰는 작업 기록 인덱스

`idx_ai_task_runs_workspace_status`와 `idx_ai_task_runs_command_document`는 `db/ai_schema.sql`에 없다. 그 파일은 한 트랜잭션이고 `ai_task_runs`의 `ALTER TABLE`이 ACCESS EXCLUSIVE를 커밋까지 쥐어, 안에서 인덱스를 만들면 빌드 내내 작업 기록이 멈춘다. `migrate_ai_schema`가 스키마를 커밋한 뒤 autocommit 연결에서 `CREATE INDEX CONCURRENTLY`로 만든다. 평소 배포에서 사람이 할 일은 없다.

- 인덱스가 없어도 영구 삭제는 `ai_task_runs`를 순차로 훑어 그대로 동작한다. 느려질 뿐이다.
- 기동 경로(`ensure_ai_schema`, `AI_DB_MIGRATION_URL`을 주입한 Compose·kind)는 인덱스를 만들지 않는다. 필요하면 `migrate_ai_schema`를 실행한다.
- 스키마 적용과 인덱스 빌드는 advisory lock(`732004150`)으로 겹치지 않는다. 빌드 중에 스키마를 적용하는 인스턴스는 표가 아니라 이 잠금에서 기다려 다른 읽기·쓰기를 막지 않는다. 이 잠금은 이 변경 이후 이미지에만 있다.
- 시간 제한은 인덱스마다 180초(잠금 대기 30초)다. AWS migration Job의 600초 deadline을 넘기지 않으려는 값이다. 넘기면 Job을 실패시키지 않고 `ai_db 인덱스 미생성` 줄을 출력한 뒤 끝나고, 다음 `migrate_ai_schema`가 잔해를 지우고 다시 만든다.

`ai-migration` Job 로그에 `ai_db 인덱스 미생성`이 있거나 아래 확인 결과가 두 행이 아니면, 표가 커서 시간 제한 안에 끝나지 않는 경우이므로 다음을 수행한다.

1. **이 수동 절차는 public schema fingerprint를 바꾼다.** 직전 성공 release로의 자동 rollback과 같은 SHA 재시도가 막히므로 다음 배포 직전에, 또는 복구 검토를 감수할 수 있을 때 수행한다. 다음 배포의 Job은 이미 있는 인덱스를 건너뛰고 새 fingerprint를 기록한다.
2. 부하가 낮을 때, 진행 중인 스키마 적용이 없는지 확인하고 migration 계정(표 소유자)으로 접속한다. 트랜잭션 안에서 실행할 수 없으므로 `BEGIN` 없이 한 문장씩 실행한다.

   ```sql
   SET lock_timeout = '30s';
   SET statement_timeout = 0;
   SELECT pg_advisory_lock(732004150);
   DROP INDEX CONCURRENTLY IF EXISTS idx_ai_task_runs_workspace_status;  -- 아래 확인에서 indisvalid=false이거나 없을 때만
   CREATE INDEX CONCURRENTLY idx_ai_task_runs_workspace_status ON ai_task_runs (workspace_id, status);
   DROP INDEX CONCURRENTLY IF EXISTS idx_ai_task_runs_command_document;  -- 위와 같다
   CREATE INDEX CONCURRENTLY idx_ai_task_runs_command_document ON ai_task_runs ((command->>'document_id')) WHERE command->>'document_id' IS NOT NULL;
   SELECT pg_advisory_unlock(732004150);
   ```

3. 두 행 모두 `indisvalid = true`인지 확인한다.

   ```sql
   SELECT indexrelid::regclass, indisvalid FROM pg_index
   WHERE indexrelid::regclass::text IN ('idx_ai_task_runs_workspace_status', 'idx_ai_task_runs_command_document');
   ```

`lock_timeout` 오류로 실패하면 긴 트랜잭션이 끝나기를 기다린다. `pg_stat_activity`에서 찾을 수 있고, 실패한 빌드는 `indisvalid = false`로 남으므로 다시 지우고 만든다.

## 과거 파기 누락분 정리

access가 AI 파기 API를 부르기 전에 운영에서 직접 document 데이터를 파기한 워크스페이스·사용자는 AI 데이터(위키, 스킬, 에이전트 기록, S3 객체)가 남아 있다. 일회성 스크립트 `pipeline/reconcile_purged_scopes.py`가 access에 없는 ID를 찾아 기존 파기 함수(`purge_workspace`·`purge_user`)로 지운다. 파기 함수는 멱등이라 재실행해도 같다. `ai_model_usage`는 정산용이라 지우지 않는다.

1. access DB에서 **현재 존재하는** ID를 내보내고, 내보낸 시각을 기록한다. 삭제 유예 중인 워크스페이스(`deleted_at`이 있는 행)는 아직 파기 대상이 아니므로 포함한다.

   ```sql
   SELECT now();  -- 내보낸 시각. --exported-at에 시간대 포함 ISO-8601로 쓴다.
   \copy (SELECT id FROM workspaces) TO 'workspace_ids.txt'
   \copy (SELECT id FROM users) TO 'user_ids.txt'
   ```

2. `pipeline/`에서 AI DB(`AI_DATABASE_URL`)와 S3 환경변수를 주입하고 dry-run으로 파기 대상을 확인한다. 파일이 비어 있으면 전체 파기를 막기 위해 실행을 거부한다.

   ```bash
   python reconcile_purged_scopes.py --workspace-ids-file workspace_ids.txt --user-ids-file user_ids.txt \
     --exported-at 2026-10-11T12:00:00+09:00
   ```

   AI 테이블에서 가장 이르게 나타난 시각이 `--exported-at`보다 1시간 넘게 앞선 ID만 `to_purge_*`에 오른다. export 뒤에 생긴 ID와 시각 근거가 없는 ID는 `skipped_*`에 사유와 함께 남고 파기하지 않는다.

3. `to_purge_workspaces`·`to_purge_users`(ID별 최초 확인 시각)가 의도한 목록인지 확인한 뒤 `--apply`를 붙여 실행한다.

# Fruition LLM Pipeline

Spring 백엔드가 업로드한 Markdown/text 문서를 받아 LLM Wiki 산출물을 만드는 FastAPI 워커입니다. 기본 실행은 Source와 Concept page를 추출·정규화한 데이터에서 backend skeleton으로 조립합니다.

## 실행방법

### API 실행

pipeline 디렉터리에서 의존성을 설치하고 환경변수를 주입한 뒤 실행합니다.

```bash
.venv/bin/python -m uvicorn api:app --host 0.0.0.0 --port 8000
```

통합 저장소에서 개발할 때는 platform의 `scripts/ai-up.sh`와 `infra/compose.ai.yml`을 사용합니다. 독립 AI 저장소에는 통합 인프라가 포함되지 않습니다. 필수 연결 정보와 모델 키는 환경변수로 주입합니다.

```env
OPENAI_API_KEY=...
GEMINI_API_KEY=...
ANTHROPIC_API_KEY=...
WIKI_SEMANTIC_MAX_WORKERS=4
PIPELINE_API_PORT=8000
AI_DATABASE_URL=postgresql://...@postgresql:5432/ai_db
S3_ENDPOINT=http://minio:9000
```

Web 검색을 허용한 Query의 근거와 Web 전환은 기본적으로 LLM evaluator가 검사합니다. 모든
Query를 검사하려면 `QUERY_EVALUATOR_MODE=llm`, evaluator를 끄려면 `disabled`를 사용합니다.
내부 검색은 기본적으로 `QUERY_EMBEDDING_MODE=bge-m3`의 keyword+vector hybrid를 사용하고,
일반 Query는 `QUERY_EVIDENCE_LIMIT=8`, post-ingest 단일 주장 평가는
`POST_INGEST_EVIDENCE_LIMIT=3`에 따라 source ref 중복 제거 후 전역 상위 근거만 반환합니다.
LangGraph evaluator loop를 LangSmith에서 확인하려면 아래 tracing 값도 platform의 `infra/.env`에 설정합니다.

```env
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_PROJECT=local-pilot-dev
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
QUERY_EVALUATOR_MODE=web
QUERY_EVALUATOR_MAX_ATTEMPTS=2
```

LLM 호출은 `openai/gpt-5-nano`(기본, reasoning `medium`), `gemini/gemini-3.1-flash-lite`(reasoning `low`), `claude/claude-sonnet-5`(extended thinking 없음)만 지원합니다. provider/model은 API·DB·Kafka payload에서 선택하며 env override는 없습니다. base URL은 provider별로 고정하고 key는 `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY` 중 선택 provider의 값만 사용합니다. live provider 호출에는 key가 필요하지만 mock 통합 테스트에는 필요하지 않습니다. 실제 비밀값은 platform의 `infra/.env`에만 두고 커밋하지 않습니다.

Wiki ingest의 독립 semantic packet은 기본 4개까지 병렬 처리합니다. 직렬 비교나 provider rate limit 조정은 `WIKI_SEMANTIC_MAX_WORKERS`로 설정합니다.

LangSmith Cloud region은 계정을 만든 URL과 맞아야 합니다.

- US 기본값: `LANGSMITH_ENDPOINT=https://api.smith.langchain.com`
- APAC/Sydney: `LANGSMITH_ENDPOINT=https://apac.api.smith.langchain.com`
- EU: `LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com`
- AWS US: `LANGSMITH_ENDPOINT=https://aws.api.smith.langchain.com`

한국에서 새 workspace를 만들 수 있다면 APAC/Sydney가 가장 가까운 SaaS region입니다. 이미 `smith.langchain.com`에서 만든 기본 US workspace의 API key라면 US endpoint를 그대로 써야 합니다.

### LangGraph Studio에서 evaluator graph 보기

`langgraph.json`은 이 디렉터리의 `langgraph.json`에 있습니다. 이 설정은 `query_evaluator`, `wiki_ingest_evaluator` graph를 Studio/Agent Server에 노출합니다. `wiki_ingest_evaluator` Studio graph는 production과 같은 topology builder를 사용하며 구조와 retry 분기를 시각화·시뮬레이션합니다. 실제 LLM 입출력은 pipeline 실행 후 LangSmith trace에서 확인합니다.

```bash
cd pipeline
./.venv/bin/python -m pip install -U "langgraph-cli[inmem]"
./.venv/bin/langgraph dev
```

Studio에서 `query_evaluator` 또는 `wiki_ingest_evaluator` graph를 선택할 수 있습니다. `query_evaluator`는 아래 형태의 입력으로 실행할 수 있습니다.

```json
{
  "question": "Query evaluator graph 구조는 어디서 실행되나요?",
  "resolved_retrieval_question": "Query evaluator graph 구조는 어디서 실행되나요?",
  "answer": "LangGraph evaluator graph는 infrastructure 모듈에서 실행됩니다. [1]",
  "stop_reason": "answer_context_selected",
  "max_attempts": 1,
  "related_pages": [
    {
      "id": "query-evaluator-graph",
      "page_type": "concept",
      "title": "Query Evaluator Graph",
      "role": "concept",
      "score": 0.95,
      "summary": "Query evaluator graph는 LangGraph infrastructure 모듈로 분리되어 실행된다."
    }
  ],
  "evidence_snippets": [
    {
      "rank": 1,
      "source_document_id": "local-graph-guide",
      "source_block_ids": ["graph-1"],
      "text": "Query evaluator graph는 application use case가 아니라 infrastructure의 LangGraphQueryEvaluatorGraph에서 실행된다."
    }
  ]
}
```

`wiki_ingest_evaluator`는 production topology와 분기를 재현하는 Studio 시뮬레이션 graph입니다. 다음 입력은 첫 평가에서 `B0002` 문제를 발견해 `targeted_patch`로 이동한 뒤, 두 번째 평가에서 통과하는 흐름을 만든다.

```json
{
  "semantic_system_prompt": "원문의 의미 구조를 추출하세요.",
  "evaluation_enabled": true,
  "max_attempts": 2,
  "attempt": 1,
  "notes": [
    {
      "chunk_id": "chunk_0001",
      "evidence_claims": [
        {
          "claim": "하나의 넓은 주장",
          "anchor_block_ids": ["B0002"]
        }
      ]
    }
  ],
  "normalized": {
    "semantic_notes": [],
    "concept_ledger": [],
    "evidence_units": [
      {
        "evidence_id": "ev_0001",
        "anchor_reference_ids": ["B0002"]
      }
    ]
  },
  "evaluation_sequence": [
    {
      "passed": false,
      "retry_recommended": true,
      "retry_feedback": "ev_0001을 원자 주장으로 분리하세요.",
      "scores": {"overall": 0.6},
      "issues": [
        {
          "type": "evidence_too_broad",
          "target": ["ev_0001"]
        }
      ]
    },
    {
      "passed": true,
      "retry_recommended": false,
      "retry_feedback": "",
      "scores": {"overall": 0.9},
      "issues": []
    }
  ]
}
```

여기서 `notes`와 `normalized`는 의미 추출·정규화 결과를 대신하고, `evaluation_sequence`는 evaluator가 차례로 반환할 결과를 대신한다. Studio 출력의 `evaluations`에서 분기별 평가와 `retry_mode`를 확인할 수 있다. 실제 문서·source block·모델 입출력으로 실행하려면 pipeline을 실행하고 LangSmith trace를 확인한다.

### CLI 실행

`llmPipeline` 폴더에서 실행합니다.

```bash
export OPENAI_API_KEY=...
python run_lab.py \
  --input examples/llm-wiki.md \
  --out runs/llm_wiki_demo \
  --mode api \
  --provider openai \
  --model gpt-5-nano \
  --source-page-mode skeleton \
  --concept-page-mode skeleton \
  --max-eval-attempts 2
```

기본 정책:

- `source-page-mode=auto`: `skeleton`
- `concept-page-mode=auto`: `skeleton`
- Wiki evaluator loop는 기본 활성화되며, CLI에서 끄려면 `--no-wiki-evaluation-loop`를 사용
- refs는 `B0001` 같은 짧은 block id로 통일
- 중간 raw/debug JSON은 저장하지 않음
- 디버깅이 필요하면 `--save-debug-json` 사용

### PDF 문서 복원 CLI

문서 복원 전용 Python 의존성을 설치하고 `llmPipeline` 폴더에서 모듈 CLI를 실행합니다. 기본 mode는 `crop-first`이며 AnyDoc 본문과 Heron 특수 영역 crop을 조립해 `final/{document_slug}.restored.md`를 만듭니다. 선택 provider의 API key가 없거나 요청이 모두 실패하면 선택 보완을 건너뛰고 이 baseline 결과를 게시합니다.

```bash
python -m pip install -r requirements-document-restoration.txt
python -m app.modules.document_restoration.interfaces.cli \
  --pdf-file /path/to/paper.pdf \
  --output-dir /path/to/output \
  --document-slug paper
```

이미 생성한 Docling 결과를 재사용한다면 JSON과 Markdown을 함께 전달합니다.

```bash
python -m app.modules.document_restoration.interfaces.cli \
  --pdf-file /path/to/paper.pdf \
  --docling-json /path/to/docling.json \
  --docling-markdown /path/to/docling.md \
  --output-dir /path/to/output \
  --document-slug paper
```

표·수식·손상 본문을 원본 이미지와 대조해 선택 보완하려면 provider와 해당 provider의 API key를 지정합니다. model을 생략하면 선택 provider의 catalog 모델을 사용하며, 지원 조합은 공통 AI model catalog와 동일합니다. 그림은 모델 대상에서 제외하고 crop asset을 보존합니다.

```bash
export GEMINI_API_KEY=...

python -m app.modules.document_restoration.interfaces.cli \
  --pdf-file /path/to/paper.pdf \
  --output-dir /path/to/output \
  --document-slug paper \
  --mode selective-repair \
  --selective-provider gemini \
  --selective-model gemini-3.1-flash-lite
```

키는 선택 provider에 따라 `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY` 중 하나만 읽습니다. `--selective-model`을 생략하면 선택 provider의 catalog 모델을 사용하고, 병렬도는 `--selective-max-workers`로 변경할 수 있습니다.

기존 OCR·규칙·SLLM·Vision 전체 복원이 필요한 경우에만 `full-repair`를 명시합니다. 전용 requirements에는 수식 image-to-LaTeX 근거를 생성하는 `pix2tex`가 포함됩니다. `tesseract`는 별도 시스템 명령으로 설치되어 있어야 하며, Paddle FormulaRecognition은 `paddleocr`이 설치된 환경에서 선택적으로 사용됩니다.

```bash
python -m app.modules.document_restoration.interfaces.cli \
  --pdf-file /path/to/paper.pdf \
  --output-dir /path/to/output \
  --document-slug paper \
  --mode full-repair \
  --use-local-sllm \
  --use-local-vision
```

조립된 Markdown을 local-first evaluator로 검사하려면 다음 CLI를 사용합니다.

```bash
python -m app.modules.document_evaluation.interfaces.local_cli \
  --markdown-file /path/to/output/final/paper.restored.md \
  --pdf-file /path/to/paper.pdf \
  --output-file /path/to/output/final/paper.evaluation.json \
  --output-markdown-file /path/to/output/final/paper.evaluated.md \
  --output-report-file /path/to/output/final/paper.evaluator_report.md
```

외부 evaluator에 전달할 job만 만들 때는 `app.modules.document_evaluation.interfaces.cli`를 사용합니다. 외부 API 환경변수가 없으면 API를 호출하지 않고 `pending_external_evaluator` job을 생성합니다.

### FastAPI 실행

개발 환경에서 직접 띄울 때:

```bash
uvicorn api:app --host 0.0.0.0 --port 8000 --reload
```

주요 endpoint:

```text
GET  /health
GET  /documents/{document_id}
POST /pipeline/runs
GET  /pipeline/runs/{run_id}
GET  /pipeline/runs/{run_id}/logs
```

`POST /documents`는 없습니다. 문서 업로드와 `documents` row 생성은 Spring이 담당합니다.
DB 스키마 생성과 버전 관리는 Backend Flyway가 담당합니다. pipeline API는 시작할 때 필수 테이블이 적용됐는지만 확인하므로 Backend migration을 먼저 실행해야 합니다.

## 코드파일별 하는 일

### `api.py`

FastAPI 서버입니다.

- 일반 문서는 Spring이 먼저 생성한 `document_id`로 pipeline run 생성
- 채팅 full 누적은 기존 `document_id`와 신규 대화 `input_markdown`을 함께 받을 수 있음
- Spring/DB 문서 입력이면 MinIO에서 Markdown/text를 읽어 로컬 입력 파일로 materialize
- background task 또는 `wait=true` 동기 실행 지원
- 실행 결과를 `pipeline_runs`에 저장
- 진행 로그 조회 API 제공

### `run_lab.py`

기존 CLI/API 호환 진입점이자 pipeline dependency composition root입니다.

- CLI/API 인자 해석
- `.env` 로드와 OpenAI-compatible API 설정
- block extraction, packet build, concept resolution, page assembly 단계 연결
- 의미 추출·평가·보정·재시도 LangGraph에 infrastructure adapter 주입
- `pipeline.log`, `normalized.json`, `manifest.json`, `wiki/links.json`, `review_report.md` 생성
- `--save-debug-json`이 켜진 경우에만 raw LLM output과 packet/debug JSON 저장

### `app/modules/wiki_generation/application/run_generation_loop.py`

Wiki 생성 evaluator의 종료 조건, retry prompt, 명확한 observation 보정 정책을 담당합니다.

- evaluator feedback을 다음 의미 추출 prompt에 반영하는 정책 제공
- 최대 시도 횟수와 종료 조건 제공
- 명확한 observation 보정 정책 제공

### `app/modules/wiki_generation/infrastructure/wiki_generation_evaluator_graph.py`

Wiki ingest의 의미 추출·정규화·평가·보정·재시도를 LangGraph로 실행합니다.

- `semantic_generation`, `normalize`, `evaluate`, `repair`, `reevaluate`, `prepare_retry`, `targeted_patch` node 구성
- evaluator 통과 또는 최대 시도 도달 시 종료
- actionable `issues`가 있으면 `passed=false`로 강제하고 target block 주변 문맥과 기존 target 항목만 사용해 patch·재평가
- patch 생성이나 검증에 실패하면 관련 chunk 재생성, target을 해석하지 못하면 전체 재생성으로 fallback
- 통과를 막지 않는 선택적 제안은 `warnings`로 분리
- 시도별 상세 평가 결과를 pipeline manifest와 DB run manifest에 보관
- LangSmith 설정이 유효하면 graph node와 하위 LLM span 기록

### `app/modules/wiki_generation/application/judge_candidates.py`

section/mention evidence candidate의 application 판단 정책입니다.

- concept resolution 응답의 기존 concept 갱신 또는 relation candidate 판정 결과 검증
- existing active cluster 병합·신규 생성·검토 필요 판정
- LLM JSON 결과의 candidate/concept 식별자와 허용 enum 검증

Kafka ingest는 concept 기여 이력에 필요한 갱신 판단까지만 동기 실행합니다. Meaning Cluster 묶음 판단은 ingest 저장 후 `ai.maintenance.command`의 `post_ingest` 작업에서 실행하고, 최대 3개 source-grounded 질의의 Source+Concept retrieval 평가를 이어서 수행합니다. semantic 검색 모드에서는 변경 page와 Source block·Concept evidence unit을 같은 BGE-M3 모델로 임베딩하고 keyword·unit vector 후보를 함께 검색합니다. evidence selector는 source ref 중복을 제거한 전역 상위 3개만 evaluator에 전달합니다. `text-only`·`bm25`·`lexical` 모드에서는 대상 수를 skip으로 checkpoint하고 모델을 로드하지 않습니다. 평가 질문과 기대 사실은 저장된 원문 `source_blocks`에서 한 번에 만들고 실제 인용 여부·중복·행정 메타데이터 여부를 코드로 검증합니다. 각 질문은 실제 Query와 같은 후보 검색·graph 탐색·evidence selector를 실행하지만 답변은 생성하지 않습니다. 검색 evidence는 한 번의 batch evaluator가 질문 정합성과 answerability·recall·precision·원문 provenance·모순 여부를 함께 판단하며 gold source ref hit/rank도 기록합니다. evaluator가 case를 누락하면 결과를 저장하지 않고 재시도합니다. 유효 문항이 0개일 때만 재시도하고 3개 미만이면 실행을 실패시키지 않고 `needs_review`로 기록합니다. source→normalized Wiki evaluator와 retrieval 3문항이 모두 통과하면 `ready`, 누락·잘못된 근거·평가 미통과는 `needs_review`로 기록합니다. 후속 run은 같은 worker 프로세스에서 `pipeline_runs.manifest` checkpoint를 재사용해 최대 3회 재개하며 프로세스 장애 때만 Kafka가 command를 재전달합니다. 현재 Wiki page는 버전 없이 제자리 갱신되므로 이 품질 상태는 진단 상태이며 Query 노출을 차단하지 않습니다.

### `app/modules/wiki_generation/infrastructure/generation_loop_adapters.py`

generation loop port의 실제 LLM·debug artifact adapter입니다.

- semantic packet별 LLM 추출
- evaluator target 항목의 `replace/remove/add` patch 생성과 source anchor 검증
- evaluator completion 호출
- `--save-debug-json` 평가 산출물 기록
- retry를 실행한 경우 `raw_llm_outputs/wiki_evaluation/attempt_NN.retry.json`에 최종 `retry_mode`와 적용한 patch operation 기록

### `app/modules/wiki_generation/infrastructure/pipeline_log.py`

파일 기반 pipeline log와 선택적 callback HTTP 전송을 담당합니다.

### `app/modules/wiki_generation/infrastructure/extract.py`

Markdown 문서를 `SourceDocument`와 `SourceBlock` 목록으로 분해합니다.

- 문서 제목 추정
- `B0001` 같은 짧은 block id 생성
- DB/export용 `source_reference_id` 유지

### `app/modules/wiki_generation/infrastructure/packet.py`

block 목록을 LLM 입력 packet으로 나눕니다.

- `max_packet_chars` 기준 chunking
- `overlap_blocks` 적용
- LLM에는 짧은 `[B0001]` anchor만 전달

### `app/modules/wiki_generation/infrastructure/chat_completions_llm.py`

OpenAI-compatible chat completions client와 LLM stage wrapper입니다.

- semantic extraction
- concept resolution
- legacy full concept page generation
- JSON output 파싱과 부분 복구

### `app/modules/wiki_generation/infrastructure/prompt_io.py`

LLM user prompt를 만듭니다.

- semantic extraction prompt
- concept resolution prompt
- legacy concept page prompt
- concept별 관련 source block 수집

### `app/modules/wiki_generation/infrastructure/normalize.py`

LLM semantic extraction 결과를 backend 구조로 정규화합니다.

- concept slug 정규화와 같은 slug 병합
- evidence unit 생성
- concept mention expansion
- missing related hint 수집
- refs를 짧은 `B0001` 형태로 검증/유지

### `app/modules/wiki_generation/infrastructure/concept_resolution.py`

concept 후보의 의미적 병합/링킹을 정규화하고 적용합니다.

- 새로 뽑힌 concept끼리 intra-batch 비교 결과 반영
- 기존 wiki concept와의 merge/link 반영
- missing related hint를 current/existing concept로 매핑
- evidence related slug 재매핑

### `app/modules/wiki_generation/infrastructure/assemble.py`

최종 wiki markdown과 graph/review 파일을 조립합니다.

- source page 생성
- concept page skeleton 생성
- source key point ref를 concept key point로 내려보냄
- 같은 evidence와 source key point 기반 related concept 생성
- `wiki/links.json`, `review_report.md` 생성

### `app/modules/wiki_ingestion/infrastructure/postgres_wiki_ingestion_repository.py`

PostgreSQL persistence 계층입니다.

- `documents`, `pipeline_runs`, `wiki_pages`, `document_wiki_links`, `wiki_page_links` 관리
- 성공 시 wiki page와 graph edge upsert
- 실패 시 document/pipeline status와 error 저장

### `app/modules/wiki_ingestion/infrastructure/object_storage.py`

MinIO/S3 compatible object storage에서 텍스트 객체를 읽습니다.

### `app/modules/wiki_generation/domain/entities.py`

파이프라인 공용 dataclass 모델입니다.

### `app/modules/wiki_ingestion/infrastructure/file_io.py`

파일/JSON/log 입출력 유틸리티입니다.

### `app/modules/wiki_generation/domain/text_utils.py`

slug, SHA1, 공백 정규화, 중복 제거 같은 문자열 유틸리티입니다.


### `prompts/*.system.md`

LLM stage별 system prompt입니다.

- `semantic_extraction.system.md`
- `concept_resolution.system.md`
- `concept_page_generation.system.md`

## 실행흐름

### 1. 입력 결정

CLI는 `--input` 파일을 사용합니다. FastAPI 일반 문서 API는 Spring이 먼저 생성한 `document_id`로 저장된 Markdown을 읽습니다. 채팅 full 누적에서만 `/chat-wiki/runs`가 신규 대화 `input_markdown`을 우선 입력으로 사용할 수 있습니다.

### 2. Block Extraction

`MarkdownBlockExtractor`가 문서를 block으로 나누고 `B0001` anchor를 붙입니다.

전달 데이터:

- `document`
- `blocks`

### 3. Packet Build

`SemanticPacketBuilder`가 block 목록을 LLM 입력 packet으로 나눕니다.

전달 데이터:

- `packets`

### 4. Semantic Extraction

LLM이 packet별로 다음 JSON을 반환합니다.

- `semantic_summary`
- `key_points`
- `concept_candidates`
- `evidence_claims`

### 5. Normalize

backend가 loose LLM output을 정규화합니다.

- concept ledger 생성
- evidence units 생성
- missing related hints 수집
- mention/display refs 계산

### 6. Concept Resolution

Concept canonicalization과 section/mention 갱신 후보 판단을 서로 독립된 LLM 호출로 병렬 실행합니다. resolution 응답은 backend 기본값과 다른 예외만 반환하고, 갱신 후보 결과는 resolution 완료 후 canonical slug로 remap합니다.

- current batch concept끼리 synonym merge
- existing wiki concept와 synonym merge
- missing related hint를 current/existing concept로 매핑
- existing/current concept의 근거 갱신 또는 relation candidate 분류

resolution 응답에서 빠진 concept는 신규 생성, hint는 unresolved로 처리합니다. 근거 없는 concept 관계는 생성하지 않습니다. 갱신 후보 중 `not_same_concept`는 후속 cluster 판단 대상으로 처리합니다.

`pipeline.log`에는 resolution, update judge, 병렬 wall, 병렬 절감, 전체 pipeline 소요 시간을 기록합니다.

기존 wiki를 비교하려면 `--existing-wiki-dir`를 넘깁니다.

### 7. Source Page Assembly

backend가 source page markdown을 생성합니다.

파일명은 source title slug를 사용합니다.

```text
wiki/sources/{title-slug}.md
```

### 8. Concept Page Assembly

기본은 `concept-page-mode=skeleton`입니다.

- definition/why/evidence는 normalized data에서 조립
- key points는 source key point와 concept refs가 겹치는 항목을 가져옴
- related concepts는 shared evidence와 shared source key point 기반으로 채움

`concept-page-mode=api` 또는 `full-llm`을 명시하면 별도의 Concept page 생성 LLM을 사용합니다.

### 9. Link/Review/Manifest

최종 산출물을 생성합니다.

```text
wiki/links.json
review_report.md
normalized.json
manifest.json
pipeline.log
```

FastAPI에서 `document_id`로 실행한 경우 성공 시 DB에도 저장합니다.

## 예시

### CLI로 `llm-wiki.md` 실행

```bash
export OPENAI_API_KEY=...
python run_lab.py \
  --input examples/llm-wiki.md \
  --out runs/llm_wiki_latest \
  --mode api \
  --provider openai \
  --model gpt-5-nano \
  --source-page-mode skeleton \
  --concept-page-mode skeleton
```

확인할 파일:

```text
runs/llm_wiki_latest/wiki/sources/*.md
runs/llm_wiki_latest/wiki/concepts/*.md
runs/llm_wiki_latest/wiki/links.json
runs/llm_wiki_latest/review_report.md
runs/llm_wiki_latest/manifest.json
```

### FastAPI에 inline Markdown으로 실행

```bash
curl -X POST http://localhost:8000/pipeline/runs \
  -H "Content-Type: application/json" \
  -H "X-Internal-Token: ${INTERNAL_CALLBACK_TOKEN}" \
  -d '{
    "document_id": "doc_inline",
    "input_markdown": "# LLM Wiki\n\nLLM이 지속 관리하는 위키...",
    "provider": "openai",
    "model": "gpt-5-nano",
    "out": "runs/api_inline_llm_wiki",
    "mode": "api",
    "source_page_mode": "skeleton",
    "concept_page_mode": "skeleton",
    "wait": true
  }'
```

### Spring/DB 문서로 실행

```bash
curl -X POST http://localhost:8000/pipeline/runs \
  -H "Content-Type: application/json" \
  -H "X-Internal-Token: ${INTERNAL_CALLBACK_TOKEN}" \
  -d '{
    "document_id": "doc_123",
    "provider": "openai",
    "model": "gpt-5-nano",
    "mode": "api",
    "source_page_mode": "skeleton",
    "concept_page_mode": "skeleton",
    "log_callback_url": "http://host.docker.internal:8080/internal/pipeline/logs"
  }'
```

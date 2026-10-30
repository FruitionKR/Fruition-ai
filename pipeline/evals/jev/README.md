# Jev 켬/끔·AI 기능 평가

라우팅, 개념 병합, RAG 근거 선택에서 Jev(`JEV_*_ENABLED`)를 켰을 때와 껐을 때를 같은 정답 세트로 비교한다. 문서 편집, 문서 관리, Skill은 운영 기능 경로로 실행해 성공 수를 세고, RAG는 Wiki 없는 일반 RAG 대조군과 주장 단위로 비교한다.

## 정답 세트

| 파일 | 내용 |
|---|---|
| `routing.jsonl` | 라우팅 102건. `expected` 1개와 `acceptable` 대안 |
| `concept.json` | 기존 개념 58개와 새 개념 88건. `expected`(`merge_into`·`link_to`·`keep_new`), `also_link`, `acceptable` |
| `rag.json` | 질문 100건. `expected_answer`, `acceptable_answers`, 정답 인용문(`gold`), 답이 없는 질문(`answerable=false`) |
| `edit.json` | 문서 편집 30건. corpus 발췌·대상 범위·요청, 코드 검사(`checks`), 판정 기준(`criteria`) |
| `organize.json` | 문서 관리 26건. 폴더 정리 20건(`hierarchy`·요청·최종 배치 정답, 되묻기 정답 포함)과 폴더 조회 6건 |
| `skill.json` | Skill 24건. 의도 분류 12, Skill 작성 6, 시험 실행 6 |
| `corpus/` | RAG 원문 11개(편집·Skill 발췌에도 사용) |
| `gold.lock.json` | 위 파일의 sha256과 개정 이력(`revisions`) |

- 모든 실행기는 시작할 때 `gold.lock.json`과 파일을 대조하고, 다르면 실행하지 않는다.
- 정답을 바꿀 때는 기대 답을 그대로 두고 허용 답만 더한다. 바꾼 사례와 이유를 `revisions`에 남기고 sha256을 다시 계산한다.
- 프롬프트에 평가 문장이나 평가셋 예시를 넣지 않는다.

## 원문 출처

평가용으로 발췌·변환한 공개 문서다. 일부(통계 보도자료의 시점, 법령 개정일 등)는 평가를 위해 고쳤다. 원문 라이선스를 따르며, 배포 전에 다시 확인한다.

| 파일 | 출처 | 라이선스 |
|---|---|---|
| `en-postgres-mvcc.md` | PostgreSQL 문서 13장 Concurrency Control | PostgreSQL License |
| `en-rag-paper.md` | Lewis et al., Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks (arXiv:2005.11401) | 저자 권리(arXiv 배포) |
| `en-sre-postmortem.md` | Google, Site Reliability Engineering 15장 Postmortem Culture | CC BY-NC-ND 4.0 |
| `ko-ai-ethics.md` | 관계부처 합동, 사람이 중심이 되는 인공지능 윤리기준(2020) | 공공저작물 |
| `ko-employment-2026-09.md` | 국가데이터처 경제활동인구조사 보도자료 형식 | 공공저작물 |
| `ko-k8s-deployment.md`, `ko-k8s-pod.md` | Kubernetes 한국어 문서 | CC BY 4.0 |
| `ko-labor-standards-ch4.md`, `ko-privacy-act-excerpt.md` | 근로기준법 제4장, 개인정보 보호법 발췌 | 법령(저작권법 제7조 보호 제외) |
| `ko-wiki-ir.md`, `ko-wiki-rag.md` | 한국어 위키백과 '정보 검색', '검색증강생성' | CC BY-SA 4.0 |

## 실행

`pipeline/`의 코드를 쓰므로 `PYTHONPATH`에 평가할 체크아웃의 `pipeline/`을 둔다. 모든 단계가 실제 모델을 호출해 비용이 든다. 답변·편입 모델은 Claude Haiku(`claude-haiku-4-5-20251001`)이고, 각 사례를 3회 반복한다.

```bash
cd pipeline/evals/jev/tools
export PYTHONPATH=/절대/경로/pipeline
export JEV_EVAL_ENV_FILE=/절대/경로/.env   # ANTHROPIC_API_KEY, TYPESAFE_API_KEY. 이미 환경변수에 있으면 생략

python run_routing.py [--dry-run] [--limit N]   # 라우팅 102건 × 켬·끔 × 3
python run_concept.py [--dry-run] [--limit N]   # 개념 88건 × 켬·끔 × 3
```

```bash
python run_edit.py                                   # 문서 편집 30건 × 3 → grade_features.py edit 로 판정
python run_organize.py                               # 폴더 정리 20건 + 조회 6건 × 3
python run_skill.py                                  # 의도 분류·작성(켬/끔) + 시험 실행 × 3 → grade_features.py skill
python grade_features.py edit ../results/<날짜>/edit/rows.jsonl
python grade_features.py skill ../results/<날짜>/skill/rows.jsonl
```

RAG는 로컬 스택(platform 통합 스크립트로 띄운 PostgreSQL, MinIO, document-svc, access-svc, 각 워커 컨테이너)이 필요하다.

```bash
python rag_prepare.py --name rag                   # 새 계정·워크스페이스에 corpus를 올리고 Haiku로 편입
python run_rag.py ../results/<날짜>/rag/workspace.json --name rag   # 질문 100건 × 켬·끔 × 3
python grade_rag.py ../results/<날짜>/rag/rows.jsonl              # Codex 채점(codex CLI 필요)
python analyze_rag.py ../results/<날짜>/rag                       # 집계
python run_rag_baseline.py                                         # 일반 RAG 대조군(원문 청크 검색, 같은 답변 모델) × 3
python grade_rag.py ../results/<날짜>/rag_baseline/rows.jsonl      # 대조군도 같은 방식으로 채점
python grade_claims.py ../results/<날짜>/claims ../results/<날짜>/rag/rows.jsonl:on ../results/<날짜>/rag_baseline/rows.jsonl
```

- 편입이 끊기면 `python rag_prepare.py --resume ../results/<날짜>/rag/workspace.json`으로 같은 계정·워크스페이스에서 끝난 문서를 건너뛰고 이어서 편입한다. 결과도 그 폴더에 쓴다.
- 결과는 `results/<날짜>/<이름>/`에 저장되며 저장소에 넣지 않는다.

## 채점

- **라우팅·개념:** 코드로 채점한다. 기대 답이나 허용 답 중 하나와 같으면 정답이다. 개념 `link_to`는 연결 대상에 `canonical_slug`나 `also_link` 중 하나가 있으면 정답이고, 그 밖의 대상은 추가 연결로 센다.
  - 추가 연결이 정의상 타당한지는 `python grade_links.py ../results/<날짜>/concept/rows.jsonl`로 Codex가 쌍마다 valid/weak/invalid로 채점한다(결과 `link_grades.json`). valid인 대상은 정답셋의 `also_link`·허용 답에 더할 후보다.
- **RAG:** 실행 행마다 선택기가 고른 근거 전체(`evidence`), 답변이 인용한 근거(`cited_evidence`), 답변 모델이 받은 프롬프트(`answer_context`)를 저장한다. 최종 결과의 근거는 인용된 것만 남으므로(`renumber_used_evidence`), 근거 선택은 반드시 `evidence`로 본다. 켬/끔을 가리고 순서를 섞어 Codex가 다음을 채점한다.
  - 근거 충분성: 고른 근거만으로 기대 답이나 허용 답을 만들 수 있는지(sufficient/partial/none). 같은 질문에 같은 근거가 나온 실행은 한 번만 채점한다.
  - 정답: 기대 답이나 허용 답을 말하면 correct. 질문이 묻는 사실이 빠지거나 틀렸을 때만 partial이다.
  - 환각: 답변 프롬프트에 없는 구체적 사실을 단정했는지. 다른 근거로 답했어도 받은 문맥에 있으면 환각이 아니다.
  - 정답 인용문이 고른 근거에 그대로 들어 있는지(코드 재현율)도 함께 센다.

- **문서 편집:** 실행 오류가 없고, 코드 검사(대상 밖 줄 그대로, 보존·필수·금지 문자열, 형식, 원문과 같은 결과 아님)와 Codex의 판정 기준 채점을 모두 통과하면 성공이다.
- **문서 관리:** 정리 계획을 메모리 폴더 트리에 적용해 최종 배치(이동 위치, 같은 폴더로 묶일 문서, 이름, 건드리면 안 되는 항목, 요약 안내)를 정답과 비교한다. 되묻기가 정답인 사례는 되물었을 때만 성공이다. 범위가 있는 사례는 운영 worker처럼 `scoped_hierarchy`로 잘라 계획을 만든다. 조회는 고른 폴더 id 집합이 정답과 같으면 성공이다.
- **Skill:** 의도 분류는 판단·Skill 종류·참고 방식·원문 필요 여부가 정답과 같으면, 작성은 상태·기능·초안 검사가 맞으면 성공이다. 시험 실행은 `completed`이고 코드 검사와 Codex 판정을 통과하면 성공이다.
- **RAG 주장 단위 검증:** 답변을 원자 주장으로 나누고 Codex가 corpus 원문 파일을 직접 읽어 supported/unsupported/contradicted로 판정한다. 근거 없는 주장 비율 = (unsupported + contradicted) / 전체 주장.

## 평가 전용 설정

운영과 다르게 평가 스크립트 안에서만 바꾼 것이다.

- 편입과 개념 병합에도 Claude(Haiku)를 쓴다. 운영 편입은 워크스페이스 공용 설정에서 Claude를 Gemini로 바꾼다.
- Claude 출력 한도를 32k, 요청 시간 한도를 600초로 둔다. 편입 평가 구간을 4만 자로 줄인다(Haiku 입력 한도 200k).
- 임베딩은 임베딩 서버 대신 같은 BGE-M3를 프로세스에 올리고, 운영과 같은 `MAX_TEXT_CHARS` 한도를 건다. 로컬 모델은 동시 호출에서 죽어 호출을 직렬화한다.
- `en-rag-paper.md`의 NUL 문자는 올릴 때만 지운다(PostgreSQL text에 들어가지 않음, 정답 인용문에는 없음).

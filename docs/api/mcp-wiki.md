# MCP 위키 검색·페이지 조회·draft intake 내부 API

[서비스 문서](../README.md) · [API 목차](README.md) · [교차 서비스 계약](../adr/0046-mcp-cross-service-contract.md)

document-svc가 현재 MCP grant와 workspace 권한을 확인한 뒤 service authentication으로 호출하는, 생성형 LLM 없는 위키 read API다. 사용자 bearer token은 받지 않으며 `X-Internal-Token`만 사용한다. 검색과 bounded page read는 서로 다른 DTO와 identity를 가진다.

## 1. Endpoint

검색 endpoint는 `POST /internal/mcp/wiki/search`다. 이 문서 후반의 page read는 기존 `GET /wiki/pages/{page_id}`의 opt-in mode다.

요청과 응답의 `schema_version`은 `mcp.wiki-search.v1`이다. 객체는 중첩 actor까지 unknown field를 거절한다. actor의 `origin`, `spec_version`, `tool_name`은 각각 `MCP_CLIENT`, `2025-11-25`, `search_wiki`만 허용한다.

## 2. 요청

```json
{
  "schema_version": "mcp.wiki-search.v1",
  "actor": {
    "invocation_id": "123e4567-e89b-42d3-a456-426614174000",
    "grant_id": "123e4567-e89b-42d3-b456-426614174001",
    "user_id": "user-id",
    "workspace_id": "workspace-id",
    "client_id": "https://client.example/cimd.json",
    "policy_generation": 1,
    "origin": "MCP_CLIENT",
    "spec_version": "2025-11-25",
    "tool_name": "search_wiki"
  },
  "query": "검색어",
  "cursor": null,
  "page_size": 10
}
```

첫 요청의 `cursor`는 `null`이다. 다음 요청은 cursor와 함께 최초의 `query`, `page_size`, actor user/workspace/grant/client/policy/spec/tool을 그대로 보내야 한다. `invocation_id`는 요청별 추적 ID라 cursor 소유권에는 포함하지 않는다.

## 3. 응답

```json
{
  "schema_version": "mcp.wiki-search.v1",
  "results": [{
    "page_id": "existing-page-id",
    "page_type": "CONCEPT",
    "title": "페이지 제목",
    "version": 1700000000000000,
    "content_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "snippet": "길이가 제한된 근거",
    "evidence": [{"document_id": "doc_a", "block_id": "B0001"}]
  }],
  "next_cursor": null,
  "truncated": false,
  "data_boundary": "UNTRUSTED_WORKSPACE_CONTENT"
}
```

`page_type`은 `SOURCE | CONCEPT`다. page ID는 기존 varchar 도메인 ID를 그대로 쓰며 UUID로 바꾸지 않는다. `version`은 published page의 `updated_at` microsecond 값이고, `content_hash`는 그 변경 이후 완료된 현재 embedding model의 stored page representation hash다. 결과에는 raw Markdown/object URI/internal payload가 없고 title/snippet/evidence만 있으며 모든 workspace text는 `UNTRUSTED_WORKSPACE_CONTENT` 데이터다. link/image 객체는 반환하지 않는다.

`snippet`은 별도 page read로 이어지는 의도적으로 길이를 제한한 검색 excerpt다. snippet 상한에서 잘릴 수 있지만 이는 결과 pagination을 뜻하지 않으므로 top-level `truncated`는 `next_cursor` 유무만 나타낸다.

## 4. 검색 경계

- active page와 요청 actor의 exact `workspace_id`만 SQL 후보에 포함한다.
- 완전한 workspace 검색 scope가 명시적 page/unit/text-byte/vector-dimension cap 안인지 cap+1 metadata probe로 먼저 판정한다. 초과하면 재시도할 수 없는 `503 DEPENDENCY_UNAVAILABLE`로 거부하고 unit text rank/vector unnest를 실행하지 않는다.
- admission에 성공한 scope는 전체 page metadata와 unit text의 lexical score, 전체 current page vector의 semantic score를 계산한 후에만 candidate limit을 적용한다. evidence 배열과 statement timeout도 응답 slicing 전에 적용한다.
- 현재 page update 뒤 완료된 stored page vector만 후보 publication identity에 쓴다. relevant unit의 완료 stored vector가 있으면 semantic rerank에 쓰고, 없으면 semantic score 0으로 두며 본문을 즉석 임베딩하지 않는다. 질문은 한 번만 임베딩한다.
- 기존 `Bm25Searcher`, stored dense score, 기존 document-qualified source reference parser를 쓴다. 답변 생성기, evaluator, summarizer, Agent 경로는 만들거나 호출하지 않는다.
- 새 검색(cursor 없음)에서는 웹 질의와 같은 `JEV_EVIDENCE_ENABLED`·`TYPESAFE_API_KEY`·응답 예산·묶음 규칙으로 Jev 근거 판정을 한 번 부른다. 판정 후보는 이미 bounded된 검색 후보의 snippet unit(본문·document-qualified block ref·unit 종류·가중치)뿐이며 workspace 전체 unit을 다시 읽지 않는다. 본문·블록·문서가 같은 후보는 하나로 묶고, 이미 고른 블록만 가리키는 후보는 건너뛰며, 최대 `QUERY_EVIDENCE_LIMIT`(기본 8)개를 고른다. 고른 후보를 고른 순서로 앞에 두고 나머지는 하이브리드 순서를 유지한다. Jev가 꺼져 있거나 쓸 수 없으면 하이브리드 순서만 쓰며 서버 생성형 LLM을 부르지 않는다. 호출은 model usage ledger의 `kind=mcp_wiki_search_jev_evidence`로 기록하며 읽기 요금은 바꾸지 않는다.
- query embedding 호출은 model usage ledger의 `kind=mcp_wiki_search_query_embedding`으로 answer generation과 분리해 기록한다.
- 검색 실행은 API event loop 밖의 worker thread에서 수행하고, deadline-aware remote embedding transport만 허용한다. 로컬 model fallback은 이 endpoint에서 지원하지 않는다.

Candidate retrieval은 admission에 성공한 작은 scope에서 lexical match와 semantic-only page를 모두 유지한다. 현재 array vector schema에는 index-supported nearest-neighbor 연산이 없으므로 cap을 넘는 workspace는 임의 prefix 검색으로 recall을 버리지 않고 명시적 capacity error로 거부한다. 대규모 workspace 지원은 실제 vector index/schema·deployment 결정 후에 별도로 해결해야 한다.

## 5. Cursor

Cursor는 HMAC-SHA256으로 서명한 stateless payload다. user/workspace/grant/client/policy/spec/tool, query hash, page size, 정렬 버전, offset, 전체 bounded 후보의 ordered page ID/version/content hash/document-qualified evidence locator를 묶는다. durable snapshot/cache는 만들지 않는다.

다음 페이지마다 같은 bounded 검색을 다시 실행하되 Jev와 하이브리드 정렬은 다시 하지 않는다. 현재 후보의 page ID/version/hash/evidence가 cursor에 고정한 후보 집합과 같으면 cursor의 순서를 그대로 쓴다. 서명·소유권·반복 인자가 다르면 `HANDLE_NOT_FOUND`, 후보 집합이나 page version/hash/evidence가 바뀌면 `STALE_CURSOR`다. TTL은 이 slice에서 임의로 만들지 않았다.

## 6. 설정과 비활성 상태

다음 값은 모두 양의 정수로 명시해야 하며 운영 기본값이 없다.

- `MCP_WIKI_SEARCH_MAX_STRING_CHARS`
- `MCP_WIKI_SEARCH_MAX_PAGE_SIZE`
- `MCP_WIKI_SEARCH_MAX_CANDIDATES` (`MAX_PAGE_SIZE` 이상)
- `MCP_WIKI_SEARCH_MAX_CANDIDATE_BYTES`
- `MCP_WIKI_SEARCH_DEADLINE_MS`
- `MCP_WIKI_SEARCH_MAX_REQUEST_BYTES`
- `MCP_WIKI_SEARCH_MAX_RESPONSE_BYTES`
- `MCP_WIKI_SEARCH_MAX_SCAN_PAGES` (`MAX_CANDIDATES` 이상)
- `MCP_WIKI_SEARCH_MAX_SCAN_UNITS`
- `MCP_WIKI_SEARCH_MAX_VECTOR_DIMENSIONS`

`MCP_WIKI_SEARCH_CURSOR_HMAC_KEY`는 UTF-8 32 bytes 이상이어야 한다. 하나라도 없거나 잘못되면 API는 fail-closed `503 DEPENDENCY_UNAVAILABLE`이다. 테스트 값은 focused test가 직접 주입하며 제품 상한이나 TTL을 뜻하지 않는다.

`MAX_CANDIDATE_BYTES`는 candidate response만이 아니라 admission에서 검사하는 전체 title/slug/summary/unit text UTF-8 byte 합의 상한이다. page/unit/vector cap을 넘는 workspace는 같은 상태에서 재시도해도 성공하지 않는 capacity error를 반환한다.

Query embedding에는 `EMBEDDING_SERVICE_URL`, `INTERNAL_CALLBACK_TOKEN`, `EMBEDDING_MODEL_NAME`이 필요하다. Remote transport는 남은 MCP deadline보다 오래 기다리지 않으며, service URL이 없으면 local inference로 fallback하지 않고 fail-closed `503 DEPENDENCY_UNAVAILABLE`이다.

Embedding/provider, bounded SQL statement, BM25 검색의 deadline check는 worker thread에서 수행되어 API event loop를 막지 않는다. 다만 model-usage ledger/DB connection 수립과 이미 시작한 Python BM25 함수는 강제 중단되지 않으며, 입력 cap과 전·후 deadline check로 제한한다.

## 7. 오류

도메인 오류는 ADR envelope를 쓴다. 주요 code는 `INVALID_INPUT`, `INVALID_HANDLE_FORMAT`, `HANDLE_NOT_FOUND`, `STALE_CURSOR`, `REQUEST_TOO_LARGE`, `RESPONSE_LIMIT_EXCEEDED`, `DEPENDENCY_UNAVAILABLE`다. provider/DB 예외나 query/snippet/source body는 오류에 넣지 않는다.

## 8. 보안

라우터는 `pipeline/api.py`의 `include_internal_router(..., internal_token_dependencies)`로 등록돼 body validation 전에 `X-Internal-Token`을 검사한다. MCP JWT나 end-user bearer를 AI로 전달하지 않는다. 현재 grant/membership/provenance 재검증과 외부 link/image 표시 정책은 document-svc가 소유한다.

## 9. 수명주기

검색은 active published rows만 읽고 draft/source snapshot/cursor row를 만들지 않는다. Cursor가 stateless라 AI purge/TTL 대상도 추가하지 않는다. page 변경은 다음 요청에서 새 bounded snapshot과 비교해 stale 처리한다.

## 10. Bounded `get_wiki_page`

MCP public tool input은 existing bounded ID grammar의 `{ "page_id": "..." }`뿐이다. document-svc가 검증된 authorization context의 workspace로 다음을 호출한다.

`GET /wiki/pages/{page_id}?workspace_id=<validated context>&mcp_read=true`

`mcp_read` 없는 legacy GET은 바꾸지 않는다. AI mode는 다음 일곱 text field만 반환한다.

```json
{
  "schema_version": "mcp.wiki-page.v1",
  "page_id": "page-a",
  "page_type": "CONCEPT",
  "title": "페이지 제목",
  "body": "# 제목\n\n본문\n",
  "content_hash": "sha256:0cbe37d6300159221d66c8bdc45b489caa97abdc10726d12af7a2417497f14c8",
  "data_boundary": "UNTRUSTED_WORKSPACE_CONTENT"
}
```

표시한 hash는 예시 body의 exact UTF-8 bytes SHA-256이다. 빈 `body`도 유효하다. 응답에는 `version`, evidence/quote, cursor, chunk, `next_cursor`, `truncated`, raw-hash field가 없다.

AI는 validated workspace의 active page만 읽고 read 중 metadata/body가 바뀌지 않았는지 확인한다. exact raw returned UTF-8 body를 hash하며, Document는 그 internal hash를 먼저 검증한 뒤 기존 Markdown sanitizer를 title/body에 적용하고 exact sanitized UTF-8 body의 public hash를 반환한다. public hash는 returned representation만 식별하며 raw storage identity, search embedding/version/evidence identity, approval/apply identity, I8 publication identity가 아니다.

page limit은 모두 양수로 명시한다: `MCP_WIKI_PAGE_MAX_ID_CHARS`, `MCP_WIKI_PAGE_MAX_METADATA_BYTES`, `MCP_WIKI_PAGE_MAX_BODY_BYTES`, `MCP_WIKI_PAGE_MAX_RESPONSE_BYTES`, `MCP_WIKI_PAGE_DEADLINE_MS`, `MCP_WIKI_PAGE_DB_CONNECT_TIMEOUT_MS`, `MCP_WIKI_PAGE_DB_STATEMENT_TIMEOUT_MS`, `MCP_WIKI_PAGE_STORAGE_CONNECT_TIMEOUT_MS`, `MCP_WIKI_PAGE_STORAGE_READ_TIMEOUT_MS`. response bytes는 body bytes보다 커야 하며 운영 기본값은 문서화하지 않는다. oversize metadata/body/envelope는 자르지 않고 `413 RESPONSE_LIMIT_EXCEEDED`; invalid ID는 `400 INVALID_INPUT`; 없는 page는 `404 PAGE_NOT_FOUND`; read 중 변경은 retryable `409 PAGE_CHANGED`; invalid configuration, storage/DB failure, bad UTF-8, timeout은 `503 DEPENDENCY_UNAVAILABLE`이다.

구현은 bounded current-body consistency check만 증명하며 immutable publication, atomic cross-store snapshot, live provider 보장, hard end-to-end deadline을 뜻하지 않는다. I8을 기다리지 않는다.

## 11. 구현 파일

- application/cursor: `pipeline/app/modules/mcp_read/application/wiki_search.py`
- bounded PostgreSQL candidate adapter: `pipeline/app/modules/mcp_read/infrastructure/postgres_wiki_search_repository.py`
- bounded page read: `pipeline/app/modules/mcp_read/application/wiki_page.py`, `infrastructure/bounded_wiki_page_repository.py`, and the `mcp_read=true` branch in `wiki_ingestion/interfaces/http/routes.py`
- exact DTO/route: `pipeline/app/modules/mcp_read/interfaces/http/{schemas,routes}.py`
- composition root: `pipeline/api.py`

## 12. 연동 상태와 한계

- bounded four-tool code slice: `search_wiki`, `find_documents`, `get_document`, `get_wiki_page`가 catalog에 있고, Document page client가 current authorization 뒤 bounded AI GET mode를 호출한다.
- 아웃바운드 호출: deadline-aware embedding service로 query embedding 1회; 미설정이면 fail closed
- inherited controlled evidence: AI는 focused 48개와 real PostgreSQL/MinIO 18개, Document는 real PostgreSQL/MinIO와 controlled HTTPS를 포함한 10개(6+2+2) 검사가 있다. incoming SDK harness는 MockHttp 기반이다.
- 이는 release 또는 I2 전체 readiness가 아니다. live socket/proxy/Claude client proof가 없고, 이전 architectural failure 여섯 건도 passed로 기록하지 않는다. MCP public transport/current access authorization과 I3–I6/I8–I9 release work는 이 증거 범위 밖이다.

## 13. I9 snapshot/window intake, finalize, and internal full preview

이 slice는 공개 MCP tool을 추가하지 않고, document-svc가 현재 grant/policy generation/membership, `wiki.ingest`, 문서 read를 원본 판정한 뒤 호출하는 service-only endpoint를 제공한다. command endpoint는 모두 `X-Agent-Service-Token`, exact `mcp.wiki-draft.v1`, unknown-field 거절을 사용한다.

| endpoint | exact command | 결과 |
|---|---|---|
| `POST /internal/mcp/wiki/ingest-runs` | `{schema_version,actor,idempotency_key,request_hash,document_id}` | server-created run/draft/source snapshot과 `ACTIVE_WINDOWING`, next ordinal |
| `POST /internal/mcp/wiki/ingest-runs/{run_id}/resume` | `{schema_version,actor}` | 같은 owner/grant/client/generation의 현재 intake 위치 |
| `POST /internal/mcp/wiki/ingest-runs/{run_id}/windows/read` | `{schema_version,actor,window_ordinal}` | bounded ordered immutable blocks, UUID window, canonical window hash, `UNTRUSTED_WORKSPACE_CONTENT` |
| `POST /internal/mcp/wiki/ingest-runs/{run_id}/windows/{window_id}/submissions` | `{schema_version,actor,idempotency_key,request_hash,snapshot_id,window_hash,contributions}` | validated structured contribution을 draft에만 저장하고 next ordinal로 진행 |
| `POST /internal/mcp/wiki/ingest-runs/{run_id}/finalize` | `{schema_version,actor,idempotency_key,request_hash,snapshot_id,expected_submitted_window_count}` | 모든 ordered window를 검증하고 frozen `draft_version=1`과 `AWAITING_WEB_APPROVAL` response를 한 번만 생성 |
| `GET /internal/mcp/wiki/drafts/{draft_id}/preview?draft_version=1` | body 없음; `X-MCP-Workspace-Id`, `X-MCP-User-Id` | owner-bound exact `mcp.wiki-preview.v1` full preview; missing/wrong owner는 `404` |

Start는 client source/version/hash/bytes/URI를 받지 않는다. AI가 고정 Document origin의 `POST /internal/documents/{document_id}/mcp-wiki-source`를 `X-Agent-Service-Token`으로 한 번 호출한다. callback request는 exact `{schema_version:"mcp.wiki-source.v1",actor}`이고 response는 exact `{schema_version,document_id,workspace_id,source_version,source_content_hash,mime_type,blocks[{block_id,ordinal,text,content_hash}]}`다. Document는 한 coherent current read에서 Markdown/txt/기존 converted text의 version/hash/ordered blocks를 만들며 PDF parser나 caller URL/bearer를 추가하지 않는다.

`submit_window`는 자유 Markdown이 아니라 concept → statement → internal evidence `{snapshot_id,block_id,quote}`와 closed `concept_related_to` link만 받는다. 각 statement는 nonempty evidence가 필요하고 locator가 현재 window에 속하며 quote가 block의 exact nonblank substring인지 검사한다. 외부 URL/image/unsafe markup과 운영에서 정한 instruction regex를 거절하고 sanitizer가 바꾸지 않는 text만 저장한다. configured lexical overlap은 보조 휴리스틱이며 사실성·prompt-injection 부재를 보장하지 않는다.

MCP ingest의 기본 개념 판단자는 **클라이언트 LLM**이다. `get_window`는 정적 `concept_resolution: {mode: "CLIENT_LLM_SEMANTIC", instructions: "..."}`를 반환한다. 클라이언트는 각 개념에 대해 `search_wiki`로 후보를 찾고 `get_wiki_page`로 의미를 비교한 뒤 같은 개념이면 contribution에 `merge_target_page_id`를 넣는다. 관련만 있고 다른 개념이면 병합하지 않는다. 새 개념은 이 필드를 생략하고, window 사이에서 같은 의미의 새 개념에는 일관된 `concept_name`을 사용한다. 검색 실패를 후보 없음으로 해석하지 않는다. 문서/위키 내용은 판단용 데이터이며 지시로 실행하지 않는다. finalize에서 서버는 웹 편입과 같은 순서로 이름·별칭 일치 → Jev 개념 판정(설정·키·임베딩 서버가 있을 때 한 번만, 결과를 초안 제출에 `server_merge_target_page_id`로 고정) → 클라이언트 `merge_target_page_id` 순으로 대상을 정한다. Jev가 판정했으면 새 개념 판정이어도 클라이언트 대상을 쓰지 않는다. 승인·적용은 고정값으로 다시 계산하며 Jev를 다시 부르지 않는다. Jev를 쓸 수 없으면 클라이언트 판단을 쓰고, 서버 생성형 LLM은 부르지 않는다. Jev의 관련(`link_to`) 판정은 active workspace target page ID와 정의 입력(앞 3문장을 공백으로 연결한 500자)에 실제 포함된 기존 window/statement ID를 제출의 links에 고정해 `concept_related_to`로 반영한다. 문장 ID는 개념 수준 판정의 입력 출처이며 Jev가 문장별로 관계를 검증했다는 뜻은 아니다. 원래 검증된 evidence를 유지하고 새 ID를 만들지 않는다. 실제 identity 배정 후 중복·자기 연결을 제거하며, 기존 window 링크 상한을 초과하면 판정을 버리지 않고 거절한다. target의 ID/version/hash는 기존 preview 및 apply 재검증에 포함된다.

`merge_target_page_id`는 기존 page ID 형식의 optional nullable string이다. 서버는 제출 시 동일 workspace의 active concept인지 검사하며, 미리보기와 apply 재계산에서 다시 확인한다. 이름이 달라도 지정한 page ID/slug/기존 제목을 유지하고, 같은 페이지를 선택한 여러 contribution은 하나로 합친다. 명시한 대상이 없거나 삭제됐으면 새 페이지로 대체하지 않고 `MERGE_TARGET_CHANGED`로 거절한다. 같은 개념명에 상충하는 명시적 대상이 제출되면 finalize가 같은 오류로 거절한다. 적용 시 대상이 변하면 기존 `PUBLIC_STATE_CHANGED` conflict 처리로 게시하지 않는다. 기존 페이지 전체를 다른 페이지로 합치는 작업은 기존 lint 경로이고, 이 필드는 **새 문서의 기여를 기존 개념으로 편입**한다.

병합 대상은 normalized contribution 및 submit/finalize/draft hash에 포함된다. `null`은 생략으로 정규화하며 이전 클라이언트의 요청·저장 draft는 기존 이름/별칭 판정을 유지한다. document-svc의 `mcp-tools.v2`는 정적 tool description에 위 검색→의미 비교→대상 지정 순서를 기본 절차로 명시하고, `get_window` 응답과 `submit_window` contribution schema·hash normalization에서 이 필드를 전달한다. 두 서비스 계약은 함께 배포해야 한다. 위키 ingest 도구는 여전히 staged이며 공개 catalog의 네 read tool과 별개다. 이 변경이 ingest 도구의 공개 활성화를 뜻하지는 않는다.

Request hash는 I0 canonical JSON으로 actor `invocation_id`만 제외하며 tool, run/window path identity, immutable source mapping과 internal composite locators를 포함한다. 같은 scoped key/hash는 저장 response를 반환하고 다른 hash는 `IDEMPOTENCY_CONFLICT`; PostgreSQL unique key와 row/document advisory lock이 동시 retry를 직렬화한다. owner/workspace/user/grant/client/generation이 다르면 `404`다.

Finalize request hash에는 run path, server snapshot mapping, ordered window/hash/accepted-submission hash, normalized stored contributions와 internal evidence locator/quote가 들어간다. transport invocation metadata만 빠진다. complete sequence/count/source coverage가 아니면 전부 거절하며, 같은 intent의 동시 또는 다른-key retry는 최초 frozen response를 반환하고 바뀐 intent는 충돌한다. finalize 뒤 submission은 수정할 수 없다.

Full preview는 기존 contribution JSON replay와 pending-ledger reconciliation, I7 title/verified-alias identity 및 occupied-slug allocation, common invalidation replay와 dangling-entry-reference prune를 공유한다. source page before/after, submitted statements와 exact evidence, old-evidence invalidation, no-new-contribution page deletion, entry/link/reference cascade, unique deleted-page revival의 manual link를 stable change ID 순서로 모두 담는다. 원장 재생으로 사라지는 개념 문장도 `CONCEPT_STATEMENT/REMOVE`로 표시하며 교체는 `REMOVE`와 `ADD`로 표시한다. active page의 remote/pending ledger가 없거나 모순되면 `LEDGER_RECONCILIATION_PENDING`으로 전체 finalize를 보류한다.

`public_state_hash`는 실제 영향 closure의 committed body URI/version/hash와 entry/link/reference/manual/document-link baseline을 묶고, `draft_hash`는 INGEST kind, source/target snapshots, normalized submissions, 전체 ordered changes, public-state hash를 묶는다. draft에는 이 immutable 계산 입력과 preview만 저장하며 별도 historical wiki store를 만들지 않는다. 계산은 public page/body/contribution/outbox/journal/cache/callback을 쓰지 않고 object storage는 기존 committed body/contribution을 읽기만 한다.

`pipeline_runs`와 MCP run은 같은 document advisory fence를 사용해 수동/자동/upload-conversion ingest와 동시 active ownership을 만들지 않는다. 인간 window 처리 동안 workspace concept lock은 잡지 않는다. task journal cancel은 unpublished run을 `CANCELLED`로 닫고 maintenance worker는 stored configured inactivity expiry를 `EXPIRED`로 닫는다. user/workspace purge fence 뒤 run/block/window/submission/idempotency를 지우며 delayed command는 되살리지 않는다.

다음 값은 제품 기본값 없이 양수로 명시해야 start/read가 열린다: request/source-response/source-block/source-text/window-block/window-text bounds, user/workspace active counts, active storage bytes, run TTL, source timeout. Submission은 contribution/statement/evidence/link/name/text/quote bounds 외에 `MCP_WIKI_DRAFT_LEXICAL_OVERLAP_THRESHOLD`, nonempty `MCP_WIKI_DRAFT_INSTRUCTION_PATTERNS_JSON`, validation policy version과 evalset ID까지 명시하지 않으면 `503 DEPENDENCY_UNAVAILABLE`로 fail closed 한다. Finalize는 `MCP_WIKI_DRAFT_PREVIEW_MAX_ITEMS`, `MCP_WIKI_DRAFT_PREVIEW_MAX_BYTES`, `MCP_WIKI_DRAFT_PREVIEW_MAX_SOURCE_BYTES`를 모두 요구하고 하나라도 넘으면 truncated preview 없이 `PREVIEW_TOO_LARGE`로 전부 거절한다. 정확한 env 이름은 `application.py`의 config map이 source of truth다.

`AWAITING_WEB_APPROVAL`도 document active-ingest fence와 TTL/취소/task-journal/purge 대상이다. cancel/expiry/purge는 unpublished preview를 지우며 late finalize/read는 과거 성공을 재생하거나 상태를 되살리지 않는다. Preview/finalize 자체는 web approval, fee/settlement, frontend를 구현하거나 성공으로 응답하지 않는다.

`APPLYING`과 `UNKNOWN`은 TTL이 지나도 문서 점유와 활성 개수·저장량 한도에 포함된다. 미공개 run이 이번 정리에서 `EXPIRED`로 전환되면 private 원문 블록·window·submission을 삭제하되 run metadata와 멱등 기록은 유지한다. 미확정 반영과 확정 결과는 이 정리로 삭제하지 않는다.

## 14. I9 explicit-approved INGEST/LINT apply and publication result

Apply는 `X-Agent-Service-Token`으로 보호된 `POST /internal/mcp/wiki/drafts/{draft_id}/apply`와 exact `mcp.wiki-apply.v1` body를 사용한다. actor identity/provenance, approval/operation pair, draft version/hash, full public-state hash, `EXPLICIT_WEB` mode를 고정하며 canonical hash에서는 `actor.invocation_id`만 제외한다. 같은 pair/body retry는 저장된 result를 반환하고, approval 또는 operation의 재결합이나 포함 field 변경은 `IDEMPOTENCY_CONFLICT`다. `draft_kind=INGEST`와 `LINT` 모두 같은 claim/result/publication transaction을 사용하고 kind별 finalize calculator만 dispatch한다.

첫 claim 뒤 AI는 고정 Document endpoint `POST /internal/mcp/wiki/approvals/lookup`에 exact `mcp.wiki-approval-lookup.v1`을 보내고 exact `{schema_version,status,reason}`만 받는다. `VALID`은 Document가 persisted explicit approval, pair/draft/actor/source binding, full open reservation, current approved-execution Access permission과 authoritative source를 모두 확인했다는 뜻이다. `UNKNOWN`은 `AUTHZ_UNAVAILABLE`로 result를 남겨 대사 대상으로 유지하고, `FENCED`는 `HALTED`, invalid/not-found는 `CONFLICTED`다. 이 repository에는 해당 Document producer가 없으며 운영 성공은 그 producer와 reservation/fee reconciliation slice에 의존한다.

`VALID` 뒤 common concept lock/DB transaction에서 finalize와 같은 kind별 pure calculator를 fresh ledger/pending/public state로 다시 실행한다. source/target snapshot, complete changes, draft hash와 public-state hash 중 하나라도 다르면 public write 없이 전체 conflict다. 성공은 기존 immutable operation-key writer를 사용해 새 object만 준비하고, page/body ref, entry/link/reference/document link, pending contribution, draft/result와 legacy `ai.wiki.publication.v1` outbox, 별도 body-free `mcp.wiki.publication.v1` outbox를 한 DB commit에서 공개한다. LINT의 main contribution은 승인된 operation identity로 다시 묶고 linked merge contribution은 원 operation identity와 `linked_by_operation_id`를 보존한다. object upload 실패나 DB rollback은 이전 public state를 유지하며, commit 뒤 response loss는 immutable stored result로 복구한다.

Body-free read는 `GET /internal/mcp/wiki/publication-results/{approval_id}/{operation_id}`이며 같은 service token과 trusted Document-derived `X-MCP-Workspace-Id`, `X-MCP-User-Id`를 요구한다. exact `mcp.wiki-result.v1` state는 `APPLYING | PUBLISHED | NO_CHANGE | CONFLICTED | HALTED | UNKNOWN | NOT_FOUND`; `PUBLISHED/NO_CHANGE`만 immutable terminal publication evidence다. absence나 transport failure는 미실행 증거가 아니다. Exact I0 event는 durable MCP outbox에서 service-auth `POST /internal/mcp/wiki/publications/receipt`로 at-least-once 전달하며 `MCP_WIKI_PUBLICATION_RECEIPT_TIMEOUT_SECONDS`가 필수다. 2xx만 sent로 표시하고 transport/409는 pending retry로 남긴다. 이 body-free HTTP receipt는 legacy normalized-result Kafka outbox와 별개이며, AI delivery 자체는 Document fee decision의 내용을 추론하거나 remote ledger와 원자성을 주장하지 않는다.

검색 인덱싱 깨우기: Document `McpWikiApprovalService.acceptPublication`은 변경이 있는 publication event를 받으면 같은 inbox transaction에서 `ai_command_outbox`에 `{kind:"mcp_publication_index", run_id:"mcp-index:<event_id>", workspace_id}`를 쌓고 기존 publisher가 `ai.maintenance.command`로 보낸다(같은 event 재수신은 `run_id` UNIQUE로 한 번). maintenance worker는 누락 임베딩을 전역 복구한 뒤에만 offset을 커밋한다. DB·모델 같은 인프라 실패는 커밋하지 않아 재전달되고, 특정 페이지 실패(예: Markdown 누락)는 `failed`/미완료로 남겨 커밋하며 기동·10분 주기·다음 깨우기 복구가 다시 시도한다. worker가 0대면 그 재시도는 다음 maintenance command까지 미뤄진다.

Apply claim 뒤 inactivity expiry는 unresolved approved publication을 제거하지 않는다. purge는 먼저 `ai_purged_scopes` fence와 write locks를 commit한 뒤 body/object/result/outbox를 owning-scope 순서로 지운다. publication 전 cancel은 `HALTED/CANCELLED`로 닫고 unpublished immutable objects는 existing recovery cleanup 대상이다. atomic commit 뒤 cancel은 public rows나 immutable result를 generic rollback하지 않으며 기존 explicit restore/replay command를 사용한다.

## 15. I9 structured LINT draft and complete preview

`POST /internal/mcp/wiki/lint-drafts`는 `X-Agent-Service-Token`과 exact `mcp.wiki-draft.v1` body를 요구한다. command는 actor/idempotency/request hash, current `target_snapshot[{page_id,version,content_hash}]`, server-created `source_snapshots[{snapshot_id,document_id,source_version,source_content_hash}]`, ordered operations만 받는다. operation kind는 `UPSERT_STATEMENT`, `REMOVE_STATEMENT`, `MERGE_CONCEPTS`, `ADD_LINK`, `REMOVE_LINK`, `DELETE_ORPHAN_PAGE` 여섯 개로 닫혀 있으며 arbitrary Markdown, caller URL, free-form relation, LLM instruction을 받지 않는다.

AI는 처음 받아들이는 command마다 승인된 Document source callback을 document별로 다시 읽어 mapping의 version/hash를 비교하고, snapshot ID를 draft/source block에 결합한다. `UPSERT_STATEMENT`만 internal snapshot/block의 exact substring quote가 필요하며 ingest와 같은 sanitizer, no-external-URL, instruction-pattern, lexical-overlap 정책을 사용한다. structure-only operation은 evidence가 없고 configured versioned relation enum만 허용한다. 현재 실제 Document producer/socket adapter는 아직 없으므로 controlled callback proof가 이 boundary의 production integration을 뜻하지 않는다.

Target page의 current version/body hash, I7 identity/slug, closed merge-back rule, statement global identity/hash, link/reference closure를 모두 확인한다. contribution ledger와 pending ledger가 없거나 모순되거나 object가 누락되면 `LEDGER_RECONCILIATION_PENDING`으로 전체 실패한다. common contribution replay, exact evidence invalidation, entry/reference prune를 사용해 statement/body/entry/link/reference/delete/revival cascade를 stable change ID로 계산하고, delete는 surviving contribution/evidence/inbound reference가 없을 때만 허용한다.

선택한 target만 대상으로 production `_plan_page_moves`의 Jev `part_of`·embedding 후보 선정과 이동/참조/기여 규칙을 재사용한다. 전체 workspace maintenance가 아니며 snapshot 밖 후보는 추론 대상에서 제외한다. snapshot 누락, identity/version/hash 변경, 외부 inbound reference는 후보 제외로 무시하지 않고 기존 conflict/closure 검사로 거절한다. client operation이 바꾸는 페이지는 같은 회차의 이동 대상으로 쓰지 않는다. 선택한 이동과 참조 재작성, 원래 작업 ID의 linked contribution은 `calculation_inputs.server_effects`에 고정하고 기존 ENTRY/PAGE_DELETE/REFERENCE 등의 preview 및 hash에 반영한다. apply는 고정 효과를 contribution replay로 재계산하며 Jev·embedding을 다시 부르지 않는다. Jev 또는 embedding 설정/서비스가 없으면 추론 이동 없이 유효한 client operation만 유지하고 내부 생성형 LLM으로 대체하지 않는다.

Production 결정6(split-parent 관계 재판정)은 이번 lint에서 **새로 분리한 페이지**를 입력으로 삼는다. 기존 active target만 받는 현재 MCP 계약에는 해당하지 않으므로 새 identity를 만들거나 과거 split parent를 재판정하지 않는다. 이 범위는 ingest의 related/link_to 입력 출처 및 고정 링크 처리와 별개다.

성공 response는 `{schema_version,draft_id,draft_version:1,draft_hash,public_state_hash,state:"AWAITING_WEB_APPROVAL"}`이며 같은 owner가 기존 preview route에서 exact complete LINT preview를 읽는다. preview 생성은 public page/body/object, contribution artifact, pending ledger, journal change, publication/outbox/cache를 쓰지 않는다. operation/target/source/total-source/deletion/structural-change/preview item/preview byte 한계는 모두 명시 설정이며 초과하면 truncation 없이 `PREVIEW_TOO_LARGE`다.

Lint 전용 필수 설정은 `MCP_WIKI_DRAFT_LINT_MAX_OPERATIONS`, `MCP_WIKI_DRAFT_LINT_MAX_TARGET_PAGES`, `MCP_WIKI_DRAFT_LINT_MAX_SOURCE_SNAPSHOTS`, `MCP_WIKI_DRAFT_LINT_MAX_TOTAL_SOURCE_BYTES`, `MCP_WIKI_DRAFT_LINT_MAX_STRUCTURAL_CHANGES`, `MCP_WIKI_DRAFT_LINT_MAX_DELETES`, `MCP_WIKI_DRAFT_LINT_TTL_SECONDS`, nonempty `MCP_WIKI_DRAFT_LINT_RELATIONS_JSON`, `MCP_WIKI_DRAFT_LINT_POLICY_VERSION`다. 기존 source/submission/preview 필수 설정도 함께 적용되며 business default는 없다.

LINT draft는 ingest run/window/task를 가장하지 않고 shared `mcp_wiki_drafts`에 nullable run identity와 자체 actor/state/TTL로 저장한다. `mcp_wiki_lint` task journal cancellation, expiry, user/workspace purge가 unpublished preview와 private source blocks를 삭제하며 terminal row를 되살리지 않는다. Explicit apply는 fresh LINT replay/target closure를 다시 검증한 뒤 INGEST와 같은 immutable writer/result/outbox/recovery를 사용한다. Public tool registration, web approval UI와 frontend는 별도 release gate이고, Document의 approval/receipt/fee producer가 실제 cross-service authority와 settlement를 소유한다.

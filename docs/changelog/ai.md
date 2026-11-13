# AI 변경 기록

## 2026-11-13 — 제품 도움말 재판단과 MCP 공통 가이드

- 근거 부족 시 제품 사용법을 한 번 재판단해 공식 안내로 전환한다. 웹 검색 비활성화도 지원하고, 첨부 문서 질의·정확한 안전 거절·일반 질문을 보존한다.
- 공개 안내를 `product_guide.md`로 분리하고 MCP 연결·권한·위키 편입·승인/직접 업로드·비용·오류 대응을 추가했다. 웹 Chat/Agent 도움말과 MCP 조회가 같은 원문을 읽는다.
- 내부 인증이 필요한 `GET /internal/mcp/product-guide`는 공개 가이드만 반환한다. 모델 호출·문서 조회·usage 기록은 없으며 시스템 프롬프트는 반환하지 않는다.

## 2026-11-11

- Kafka 복구 성공 결과를 publication과 같은 canonical 형식으로 발행한다. 과거 HTTP 형식으로 저장된 journal 결과를 재생할 때도 operation_type과 페이지 필드를 정규화한다. HTTP 응답은 유지하며 worker·복구 계약 테스트 153개 통과(2개 격리 DB 테스트 제외).

## 2026-11-10

### MCP Jev 호출을 document 크레딧 판정으로 제한하고 사용자에게 귀속

- `search`, ingest `finalize`, `lint-drafts`가 최상위 `jev_allowed`(없으면 false)를 받는다. false면 Jev를 만들지 않고 기존 대체 경로(하이브리드 순서, 외부 AI 판단, 클라이언트 lint 작업)를 쓴다. 값은 request hash·멱등성 밖이다.
- finalize·lint의 Jev 호출을 `mcp_wiki_ingest_jev_concept`·`mcp_wiki_lint_jev_concept` kind로 run·workspace·행위자에게 기록한다.

## 2026-11-08

### AI 기동 상태와 위키 검색 준비 완료 구분

- 모델 적재 중 응답을 장애와 구분하고, 검색 인덱스 완료를 후속 품질 평가 전에 부모 편입 실행에 기록한다. dev 기준 관련 테스트 179개와 하위 사례 33개가 통과했고 2개는 건너뛰었다.

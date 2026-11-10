# AI 변경 기록

## 2026-11-10

### MCP Jev 호출을 document 크레딧 판정으로 제한하고 사용자에게 귀속

- `search`, ingest `finalize`, `lint-drafts`가 최상위 `jev_allowed`(없으면 false)를 받는다. false면 Jev를 만들지 않고 기존 대체 경로(하이브리드 순서, 외부 AI 판단, 클라이언트 lint 작업)를 쓴다. 값은 request hash·멱등성 밖이다.
- finalize·lint의 Jev 호출을 `mcp_wiki_ingest_jev_concept`·`mcp_wiki_lint_jev_concept` kind로 run·workspace·행위자에게 기록한다.

## 2026-11-08

### AI 기동 상태와 위키 검색 준비 완료 구분

- 모델 적재 중 응답을 장애와 구분하고, 검색 인덱스 완료를 후속 품질 평가 전에 부모 편입 실행에 기록한다. dev 기준 관련 테스트 179개와 하위 사례 33개가 통과했고 2개는 건너뛰었다.

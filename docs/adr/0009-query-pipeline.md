# 0009. Use an Evidence-Grounded Pipeline for Query

## Status
Accepted

## Context
embedding top-k만 사용하는 RAG는 lexical match와 Wiki graph를 충분히 활용하지 못하고, 답변이 어떤 원문 근거에서 만들어졌는지 분리해 보여주기 어렵다. 현재 Query 흐름은 질문 재작성부터 답변 평가까지 실행 순서가 정해져 있다.

## Decision
Query는 hybrid retrieval과 graph traversal로 원문 evidence를 수집한 뒤 답변하는 고정 pipeline으로 운영한다.

## Alternatives Considered
- embedding top-k만 사용하는 RAG: lexical match와 graph 경로를 활용하기 어려워 기각했다.
- BM25/text search만 사용: 의미적으로 연결된 Page를 찾기 어려워 기각했다.
- Query Agent가 검색 전략을 계획: 현재는 고정 pipeline으로 충분해 보류했다.

## Consequences/Tradeoffs
### Positive
- 답변과 원문 근거, 관련 Page와 graph 경로를 분리해 제공할 수 있다.
- 내부 근거가 부족한 경우에만 Web fallback이나 근거 부족 응답을 선택할 수 있다.

### Negative
- rewrite, retrieval, traversal, evidence, answer와 evaluator 단계를 순차적으로 실행해야 한다.
- score threshold와 Web fallback 조건을 실제 질문으로 계속 검증해야 한다.

## Follow-up
복합적인 검색 계획과 feedback loop가 필요해질 때 Query Agent 전환을 다시 검토한다.

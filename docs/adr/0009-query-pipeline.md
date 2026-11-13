# 0009. Use an Evidence-Grounded Pipeline for Query

## Status
Accepted

## Context
embedding top-k만 사용하는 RAG는 lexical match와 Wiki graph를 충분히 활용하지 못하고, 답변이 어떤 원문 근거에서 만들어졌는지 분리해 보여주기 어렵다. 현재 Query 흐름은 질문 재작성부터 답변 평가까지 실행 순서가 정해져 있다.

## Decision
Query는 hybrid retrieval과 graph traversal로 원문 evidence를 수집한 뒤 답변하는 고정 pipeline으로 운영한다.

채팅 입구에서 제품 사용법 질문은 공식 화면 가이드로 답한다. `위키 편입`처럼 고유 UI 기능의 사용법은 서비스명을 생략해도 이 경로에 해당한다. 처음에 일반 질의로 분류했더라도 evaluator가 `unsupported` 또는 `web_fallback`을 선택하거나 답변 수정 시도가 끝나도 근거 부족이면 웹 검색·근거 부족 응답 전에 사용법 여부를 한 번 재확인한다. evaluator가 꺼진 경우에도 낮은 관련도에 따른 웹 전환 또는 근거 없음에서 재확인한다. 일반 지식·문서 질문과 실제 편집 요청은 기존 질의를 유지하며, 첨부 근거가 있는 요청과 Agent 내부 질의는 도움말로 전환하지 않는다. 평가자가 정확하다고 판정한 안전한 거절은 재분류하지 않는다. 재확인하는 요청에는 분류 모델 호출이 최대 한 번 추가된다.

evidence 점수의 키워드(BM25) 비중은 질문 언어로 정한다. 한글이 있으면 10%, 없으면 40%다. Jev 한영 교차 실험에서 같은 wiki 단위 근거를 가장 앞에 찾은 비중이며, 한국어 질문은 영어 원문 근거와 표기가 달라 키워드 일치 효과가 작다.

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

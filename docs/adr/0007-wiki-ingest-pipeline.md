# 0007. Use a Deterministic Pipeline for Wiki Ingest

## Status
Accepted

## Context
Wiki Ingest의 실행 순서와 다음 단계는 고정되어 있다. LLM은 의미 추출과 Concept 병합 판단에는 필요하지만, ref 계산과 최종 Page 구조까지 맡기면 존재하지 않는 ref와 schema 오류가 반복됐다.

## Decision
Wiki Ingest는 코드가 실행 순서·조립·검증을 통제하고 LLM은 제한된 의미 판단만 수행하는 결정적 pipeline으로 운영한다.

## Alternatives Considered
- Agent/A2A가 다음 작업을 선택: 현재 흐름은 실행 순서가 고정되어 있어 기각했다.
- LLM이 최종 DB row와 Page 전체를 작성: ref와 구조 오류가 반복되어 기각했다.

## Consequences/Tradeoffs
### Positive
- LLM의 의미 판단을 사용하면서도 Page 형식과 ref 무결성을 코드로 보장할 수 있다.
- 같은 입력에 대한 실행 단계와 검증 지점이 명확하다.

### Negative
- 의미 추출, 정규화, Concept resolution, Page assembly와 validation 단계를 각각 운영해야 한다.
- 단계가 늘어나 LLM 호출과 전체 처리 시간이 증가할 수 있다.

## Follow-up
자율적인 작업 선택이 필요한 요구가 생기기 전에는 Agent/A2A로 전환하지 않는다.

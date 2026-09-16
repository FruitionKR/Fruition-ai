# 0008. Model Wiki Knowledge as Source and Concept Pages

## Status
Accepted

## Context
원본 문서와 문서에서 추출한 지식은 수명과 역할이 다르다. 원본 문서는 출처 단위로 조회해야 하고, 같은 Concept은 여러 문서의 evidence를 함께 가져야 한다.

## Decision
Wiki graph는 원본 문서를 대표하는 Source Page와 원문 evidence를 통합하는 Concept Page를 node로 사용하며, 원본 파일 자체는 node로 만들지 않는다.

## Alternatives Considered
- 원본 파일을 graph node로 사용: 원본은 document_id와 source reference로 다시 조회하고, 사람이 읽는 graph node는 Source Page로 통일하기로 했다.
- Source Page 요약에서 Concept Page 생성: 요약 과정에서 근거가 손실될 수 있어 원문 evidence에서 생성하기로 했다.

## Consequences/Tradeoffs
### Positive
- 문서 단위 출처와 여러 문서를 아우르는 지식 단위를 분리할 수 있다.
- Concept Page가 원문 evidence를 직접 추적할 수 있다.

### Negative
- Source와 Concept 사이의 link와 evidence mapping을 별도로 관리해야 한다.
- 동일 Concept의 identity와 여러 Source contribution을 reconciliation해야 한다.

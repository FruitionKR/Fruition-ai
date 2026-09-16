# 0014. Reconcile Wiki Contributions with Lint

## Status
Accepted

## Context
수정된 Source를 append ingest하면 삭제된 evidence와 edge가 남는다. 반대로 변경 때마다 Wiki 전체를 재생성하면 변하지 않은 Concept과 Query 경로까지 LLM 결과에 따라 흔들릴 수 있다.

## Decision
Source 변경은 각 문서의 최신 contribution을 기준으로 Lint가 canonical Concept과 graph를 reconciliation한다.

## Alternatives Considered
- 수정본을 append ingest: stale evidence와 edge가 남아 기각했다.
- 변경 때마다 Wiki 전체를 재생성: 변경되지 않은 Concept과 Query 경로까지 흔들려 기각했다.
- Ingest가 canonical Wiki까지 정리: Source별 기여 수집과 전체 reconciliation의 책임을 분리하기 위해 기각했다.

## Consequences/Tradeoffs
### Positive
- 수정·삭제된 근거를 제거하면서 변경되지 않은 판단을 보존할 수 있다.
- 한 Source의 기여가 사라져도 다른 Source의 evidence가 남은 Concept은 유지할 수 있다.
- dry-run으로 변경 후보를 확인한 뒤 실제 reconciliation을 실행할 수 있다.

### Negative
- stable block matching과 Source별 contribution 이력이 필요하다.
- 순수 추가 문장이 기존 claim을 반박하는 경우는 단순 block diff만으로 판단할 수 없다.
- DB와 object storage 변경을 함께 관리해야 한다.

## Follow-up
contradiction, stale claim과 data gap 탐지는 별도 결정 전까지 범위에 포함하지 않는다.

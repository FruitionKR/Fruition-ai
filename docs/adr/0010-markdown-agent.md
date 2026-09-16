# 0010. Route Markdown Work through a Unified Agent Turn

## Status
Implemented

## Context
일반 대화, Markdown 편집과 새 문서 생성은 결과 구조가 다르지만 하나의 대화 안에서 이어진다. 기능마다 별도 진입점을 두면 routing과 대화 context 처리가 중복된다.

## Decision
Markdown 관련 요청은 하나의 Agent Turn이 분류하고, action으로 chat·edit·create·clarify·reject 등의 결과를 구분한다.

## Alternatives Considered
- chat, edit와 create API를 각각 분리: routing과 공통 대화 context가 중복되어 기각했다.
- 편집 요청을 별도 화면과 흐름에서만 실행: 일반 chat 중 들어오는 편집 의도를 처리하기 어려워 기각했다.

## Consequences/Tradeoffs
### Positive
- 하나의 대화 context에서 질문, 편집과 생성을 이어갈 수 있다.
- 공통 결과 envelope의 action으로 후속 UI를 분기할 수 있다.

### Negative
- action이 늘어날수록 Router와 공통 응답 계약이 함께 확장된다.
- Frontend가 action별 결과 형식을 구분해야 한다.

## Follow-up
Template 기반 전체 문서 변환과 markdown_edit_suggestion은 별도 결정 전까지 action에 추가하지 않는다.

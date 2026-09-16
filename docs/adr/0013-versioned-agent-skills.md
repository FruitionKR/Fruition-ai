# 0013. Publish Agent Skills as Validated Immutable Versions

## Status
Implemented

## Context
Skill은 Agent에 반복 주입되는 실행 지침이므로, 수정된 내용을 즉시 덮어쓰면 어떤 지침으로 작업했는지 추적하기 어렵다. 자연어와 참조 문서에서 만든 지침은 게시 전에 보안과 Tool 범위도 확인해야 한다.

## Decision
검증과 사용자 승인을 통과한 Skill만 불변 version으로 append하고, enabled version pointer가 실행할 version을 가리키게 한다.

## Alternatives Considered
- 사용자 입력을 즉시 실행 가능한 Skill로 저장: 검토와 보안 검증을 거치지 않아 기각했다.
- 기존 SkillVersion을 덮어쓰기: 이전 실행 지침을 추적할 수 없어 기각했다.
- proposal을 실행 가능한 DB draft로 저장: 게시 전 proposal은 대화 상태에서 검토하고 승인된 결과만 저장하기로 했다.

## Consequences/Tradeoffs
### Positive
- AgentRun이 사용한 SkillVersion을 추적할 수 있다.
- 새 version에 문제가 있어도 이전 정의를 보존할 수 있다.
- 자동 routing 대상과 저장된 version 이력을 분리할 수 있다.

### Negative
- 작성과 게시가 분리되어 사용자 승인 단계가 추가된다.
- 같은 Skill을 수정할 때 새 version과 enabled pointer를 함께 관리해야 한다.

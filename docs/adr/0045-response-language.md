# 0045. 요청자 화면 언어(response_language)로 사용자에게 보이는 생성 결과의 언어를 정한다

## 맥락
프론트가 한국어/영어 화면 전환을 지원하고 모든 요청에 `Accept-Language: ko|en`을 보낸다. document-svc는 이 값을 AI command에 `response_language`로 싣는다. 지금까지 AI 답변·계획 안내·고정 문구는 한국어(또는 질문 언어)로만 나왔다. 한편 위키 페이지와 문서 본문은 워크스페이스가 함께 쓰는 자산이라 요청자 한 사람의 화면 언어로 바꾸면 다른 구성원에게 섞인 언어의 자산이 남는다.

## 결정
1. **`response_language`("ko"|"en")는 선택 필드다.** 없거나 다른 값이면 지금과 똑같이 동작한다(기존 프롬프트, 한국어 고정 문구).
2. **실행 단위 진입점에서 contextvar로 정한다.** task worker는 command마다(`_handle_controlled`), agent worker는 job마다 `response_language_scope`를 연다. 프롬프트 조립과 고정 문구가 `app/core/response_language.py`로 값을 읽는다. contextvars라 `copy_context`로 넘긴 스레드(회의록 묶음 호출 등)에도 이어진다.
3. **적용 범위는 요청자에게 보이는 결과다.** 질의 답변(`output_language`로 전달), Agent 대화 답변·되묻기 질문·안내·거절 문구, 편집·새 문서의 `summary`, 승인형 계획의 summary·reason·되묻기 질문·경고·고정 안내, 진행 이벤트 message, 회의록 초안(문장과 머리글), 첫 문답의 채팅 제목이다.
4. **문서 본문과 위키는 바꾸지 않는다.** 편집안·새 문서 본문은 기존 규칙(문서·지시 언어)을 따르고, 위키 편입(ingest·post_ingest·lint)은 `response_language`를 쓰지 않아 원문 언어를 따른다.
5. **비동기 작업은 요청 시점의 언어를 쓴다.** Kafka command는 outbox에 저장된 command 자체가 언어를 담는다. 나중에 agent worker가 처리하는 승인형 run(계획·수정 요청·실행)은 시작한 턴의 언어를 `agent_runs.response_language`에 저장해 쓴다.
6. **고정 문구는 한국어 값을 유지하고 경계에서 바꾼다.** 상수는 `register_en(ko, en)`으로 영어를 등록하고 한국어 값을 그대로 두어 기존 비교(예: 문서가 필요한 되묻기 판별)를 깨지 않는다. Agent 결과의 `message`·`chat.answer`와 진행 이벤트 message에서 등록한 문장만 바꾼다. 값을 채운 문구는 `localized(ko, en)`로 만든다.

## 대안과 기각 사유
- **모든 함수 인자로 언어를 넘기기:** 질의·Agent·계획·회의록까지 수십 개 시그니처를 바꿔야 한다. 사용량 원장 귀속(`usage_scope`)과 같은 contextvar 방식이 이미 있다.
- **기존 `output_language`로만 처리:** 편집·새 문서 본문 언어까지 바꿔 공유 자산의 언어가 요청자마다 달라진다.
- **ko일 때는 지시하지 않기:** 계약은 "답변은 요청 언어를 따른다"이다. ko 화면에서 영어로 질문해도 한국어로 답한다. 값이 없을 때만 질문 언어를 따른다.

## 결과
- document가 값을 싣지 않으면 동작이 같다. 회의록 command는 모르는 필드를 거부하므로 AI를 먼저 배포한다.
- 영어 결과의 계획 안내 문장은 형식을 고정했다. 프론트가 문장 패턴으로 안내를 분리하므로 영어 패턴을 함께 맞춘다.
- 등록하지 않은 한국어 고정 문구(Skill 작성 흐름의 일부 안내, 웹 검색·평가의 내부 진행 문구, 원문 대체 안내 등)는 한국어로 남는다.

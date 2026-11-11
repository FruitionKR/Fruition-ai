# 0047. MCP 제안 승인 화면은 URL 모드 elicitation으로 자동으로 연다

- 상태: 제안
- 범위: Fruition-document, Fruition-frontend
- 바꾸는 결정: [0046](0046-mcp-cross-service-contract.md) 결정 1-5의 "elicitation은 제공하지 않는다"에 승인 화면 URL만 예외를 둔다
- 기준 커밋: ai `879f528`, document `abb7a7f`

## 맥락

MCP 쓰기 tool은 제안만 만들고 `review_url`을 돌려준다. 사람이 웹에서 승인하기 전에는 아무것도 저장되지 않는다(0046 결정 1-6). 대상 tool은 `propose_document_edit`, `propose_plan`, `finalize_ingest`, `submit_lint_changes`, `propose_skill_definition`, `finalize_pdf_conversion` 여섯 개다.

- **승인 화면이 저절로 열리지 않는다.** Claude Code 실제 세션에서 사용자는 URL을 따로 안내받아야 했다. 사용자는 승인이 필요할 때 화면이 바로 뜨기를 원한다.
- **설명 문구는 클라이언트 재량이다.** Fruition-document #253은 `review_url`을 반환하는 tool 설명에 "반환된 review_url을 바로 열어 달라"를 넣는다. 모델이 따를지, 클라이언트가 브라우저를 열 수 있는지는 보장되지 않는다.
- **MCP 표준에 같은 문제의 해법이 있다.** 2025-11-25 사양의 URL 모드 elicitation은 서버가 클라이언트에게 URL을 열어 달라고 요청하는 기능이다. 인증·결제처럼 MCP 클라이언트를 거치면 안 되는 상호작용을 위해 만들어졌다. 클라이언트는 전체 URL과 도메인을 보여 주고 사용자 동의를 받은 뒤에만 연다. 응답의 `accept`는 "열기에 동의했다"는 뜻이지 상호작용이 끝났다는 뜻이 아니다([2025-11-25 Elicitation](https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation)).
- **2026-07-28 사양은 전달 방식을 바꿨다.** 서버가 먼저 보내는 `elicitation/create` 요청을 없애고, tool 결과로 `InputRequiredResult`(`resultType: "input_required"`)를 돌려준 뒤 클라이언트가 `inputResponses`와 `requestState`를 붙여 원래 요청을 다시 보내는 MRTR 방식으로 바꿨다. 클라이언트 capability는 요청마다 `_meta`에 실린다. `notifications/elicitation/complete`와 `elicitationId`는 삭제됐다([2026-07-28 변경점](https://modelcontextprotocol.io/specification/2026-07-28/changelog), [MRTR](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr), [2026-07-28 Elicitation](https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation)).
- **지금 서버는 서버 발신 요청을 보낼 수 없다.** document MCP는 `McpStatelessSyncServer`와 `HttpServletStatelessServerTransport`로 동작한다(`McpTransportConfig.java:47-57`). stateless tool handler는 `McpTransportContext`와 `CallToolRequest`만 받는다. `createElicitation`은 세션이 있는 `McpSyncServerExchange`에만 있다. 고정한 SDK `mcp-core:2.1.0`(`build.gradle:56-57`)은 `ElicitUrlRequest`와 `-32042`(`URL_ELICITATION_REQUIRED`) 상수를 갖지만 프로토콜은 2025-11-25까지만 안다. 2026-07-28의 `InputRequiredResult`는 `2.2.0-M2` 마일스톤의 `io.modelcontextprotocol.modern` 패키지에만 있다.

클라이언트 지원은 다음과 같다(2026-11-11 확인).

| 클라이언트 | URL 모드 | 근거 |
|---|---|---|
| Claude Code | 지원. 2026-07-28 연결은 2.1.281, 2025-11-25 연결은 2.1.287부터. 클라우드 세션은 2.1.317부터 링크 카드 | [공식 CHANGELOG](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md) |
| claude.ai·Claude 데스크톱 connector | 확인 못 함. 공식 문서에 elicitation 언급이 없다 | [custom connector 안내](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp) |
| Cursor | 확인 못 함. form 지원 주장만 있고 공식 문서를 찾지 못했다 | — |
| Codex CLI | 부분 지원으로 보임. 0.131.0은 `auth_elicitation` 설정을 켰을 때만 URL capability를 알리고, 일반 서버용 URL 모드 요청 이슈(#29344)가 열려 있다 | 2차 출처만 있음 |

## 결정

1. **승인 대기 제안을 만든 tool 호출은, 클라이언트가 URL 모드를 지원하면 승인 화면 URL elicitation을 함께 보낸다.** 위 여섯 tool이 대상이다. 읽기 tool과 `start_*`·`submit_window` 같은 중간 단계는 보내지 않는다.
2. **2026-07-28 MRTR 방식으로만 구현한다.** stateless 서버를 그대로 두고, capability는 요청 `_meta`의 `elicitation.url`로 판단한다. 2025-11-25 연결에는 elicitation을 보내지 않는다. 선행 조건은 Java SDK의 2026-07-28 정식 릴리스와 0046 결정 1-4의 프로토콜·SDK 버전 변경이다. 그전까지는 #253 설명 문구가 유일한 경로다.
3. **흐름은 두 번의 호출이다.**
   - 첫 호출: 지금과 같이 검증하고 제안을 저장한다. 저장이 commit된 뒤에만 `InputRequiredResult`를 돌려준다. `inputRequests`에는 `open_review` 키 하나로 `elicitation/create`(`mode: "url"`)를 넣고, `url`은 지금 반환하는 `review_url` 그대로, `message`는 서버 고정 문구("Fruition에서 제안을 검토하고 승인해 주세요. 승인 전에는 아무것도 저장되지 않습니다.")다. 제목·본문·사용자 입력은 넣지 않는다.
   - 재시도: `requestState`를 검증한 뒤 제안을 새로 만들지 않고 저장된 제안을 찾아 지금과 같은 최종 결과(`run_id` 또는 `draft_id`, `review_url`, 상태)를 돌려준다. 다시 elicitation을 보내지 않는다.
   - URL 모드를 알리지 않은 클라이언트에는 첫 호출에서 바로 최종 결과를 돌려준다. 지금과 같다.
4. **승인을 기다리지 않는다.** 재시도는 `accept`·`decline`·`cancel`과 상관없이 즉시 최종 결과를 돌려준다. 사양은 서버가 완료까지 기다려도 된다고 하지만, 웹 승인은 몇 분에서 며칠까지 걸리고 그동안 HTTP 요청과 tool 호출을 붙잡을 수 없다.
5. **elicitation은 승인 수단이 아니다.** `accept`는 "링크 열기에 동의함"일 뿐이며 제안 상태를 바꾸지 않는다. `decline`·`cancel`도 제안을 거절하거나 취소하지 않는다. 제안은 `awaiting_approval`(문서·계획) 또는 `AWAITING_WEB_APPROVAL`(위키·변환 draft)로 남고, 승인·거절은 웹에서만 한다. form 모드는 쓰지 않는다. MCP 안의 승인 tool 금지(0046 결정 1-5)는 그대로다.
6. **URL은 자사 승인 경로만 허용한다.** `url`은 `app.mcp.review-origin`(https, path·query·fragment 없음)으로 만든 기존 `review_url`과 같아야 하고, tool별 고정 경로(예: `/mcp/workspaces/{workspace_id}/runs/{run_id}`, `/mcp/workspaces/{workspace_id}/wiki-drafts/{draft_id}`)만 쓴다. query·fragment·토큰·코드·사용자 식별 정보를 넣지 않는다. 0046대로 이 URL은 인증 수단이 아니며, 승인 화면은 로그인한 사용자가 제안을 만든 사용자·workspace와 같은지 다시 확인한다. 사양의 "URL을 연 사람이 elicitation을 만든 사용자와 같은지 확인" 요구도 이것으로 충족한다.
7. **`requestState`는 HMAC으로 서명하고 다시 확인한다.** 내용은 `{v, user_id, workspace_id, grant_id, client_id, tool_name, handle, request_hash, exp}`이고 `handle`은 제안 ID다. 재시도는 서명, 만료, 현재 토큰의 user·workspace·grant·client, tool 이름, 인자 hash를 모두 대조하고 하나라도 다르면 거절한다. 재시도도 일반 tool 호출처럼 현재 인가(`TOOL_CALL`)를 다시 거친다. 재시도는 저장된 결과를 읽기만 하므로 한 번만 쓰이게 강제하지 않는다. `exp` 값은 launch gate에서 정한다.
8. **재시도와 중복 호출은 같은 제안을 돌려준다.** MRTR 재시도는 `requestState`의 handle로 찾는다. `requestState` 없이 같은 `idempotency_key`로 다시 부른 호출은 기존 멱등 규칙대로 같은 제안을 돌려주고, 제안이 아직 승인 대기일 때만 elicitation을 다시 보낸다. 클라이언트가 재시도하지 않아도 제안은 저장돼 있고 같은 key의 재호출로 찾을 수 있다.
9. **감사 기록은 audit v1을 바꾸지 않는다.** 첫 호출과 재시도는 각각 tool attempt이므로 지금처럼 한 행씩 남는다. `accept`·`decline`·`cancel`은 승인 증거가 아니므로 audit에 넣지 않는다. 출시 판단에 쓸 수 있게 tool·action별 개수만 사용자 식별자 없이 metric으로 센다.
10. **기능 플래그로 켠다.** `app.mcp.review-elicitation.enabled`는 기본 off다. 켜도 URL capability를 알린 클라이언트에만 적용한다.

## 대안과 기각 사유

- **설명 문구만 쓰기(#253):** 바로 쓸 수 있고 2026-07-28 전에도 동작하지만 모델과 클라이언트 재량이다. 열리지 않아도 서버가 알 수 없다. 이 ADR이 구현되기 전의 경로이자, URL 모드를 지원하지 않는 클라이언트의 대체 경로로 남긴다.
- **서버가 브라우저를 직접 열기:** 서버는 원격에 있고 사용자 기기에 접근할 수 없다. 불가능하다.
- **웹에 열린 Fruition 탭으로 알림 보내기(웹 푸시·SSE):** 사용자가 Fruition 탭을 열어 두지 않았으면 효과가 없고, 브라우저 알림 권한과 새 실시간 채널이 필요하다. MCP 대화 맥락과도 떨어진다. 나중에 보조 알림으로 더할 수는 있지만 "승인할 때 화면이 뜬다"를 대신하지 못한다.
- **form 모드로 MCP 안에서 승인받기:** 클라이언트와 모델이 승인 응답을 만들거나 바꿀 수 있다. 가격·전체 preview를 보여 주는 웹 승인 경계(0046)와 MCP 안 승인 금지 원칙을 깬다.
- **2025-11-25 stateful 방식(세션 + SSE 위 `elicitation/create`):** Streamable HTTP를 세션과 SSE가 있는 transport로 바꾸고 인스턴스 간 세션 고정이 필요하다. 0046은 세션을 상태의 근거로 쓰지 않기로 했고, 2026-07-28은 이 방식을 없앴다. 곧 버릴 경로를 새로 만드는 일이다.
- **2025-11-25 `URLElicitationRequiredError`(-32042):** JSON-RPC 오류라 stateless로도 보낼 수 있다. 하지만 stateless 서버는 초기화 때 받은 capability를 요청마다 알 수 없어 "지원하지 않는 모드를 보내지 말라"를 지킬 수 없다. 제안을 이미 저장했는데 tool 결과가 오류가 되어 모델이 `run_id`를 받지 못한다. 사양도 "요청을 처리할 수 없을 때"만 쓰라고 한다.
- **승인 완료까지 tool 호출을 붙잡기:** 결정 4의 이유로 기각한다.

## 결과

- Claude Code는 2026-07-28 연결에서 승인 화면 열기 동의창을 띄울 수 있다. 다른 클라이언트는 지원이 확인될 때까지 `review_url`과 설명 문구로 동작한다. 클라이언트가 동의창에서 전체 URL과 도메인을 보여 주므로 승인 화면 도메인이 사용자에게 익숙해야 한다.
- 구현은 2026-07-28 정식 SDK가 나오고 0046 결정 1-4를 바꾼 뒤에 시작한다. 그전에는 문서·설정 변경이 없다.
- 0046 결정 1-5는 "승인 화면 URL 모드 elicitation만 예외"로 읽는다. 이 ADR이 수락되면 0046 본문도 그렇게 고친다.
- #253의 "바로 열어 달라" 문구와 elicitation이 함께 동작하면 같은 화면이 두 번 열릴 수 있다. 구현할 때 문구를 "사용자에게 review_url을 보여 달라"로 낮출지 정한다.
- 필요한 테스트:
  - capability 분기: `_meta`에 `elicitation.url`이 있으면 `InputRequiredResult`, 없거나 form만 있으면 지금과 같은 최종 결과, 플래그 off면 항상 최종 결과.
  - 저장 순서: 제안 저장이 실패하거나 rollback되면 elicitation을 보내지 않는다.
  - 재시도: `accept`·`decline`·`cancel`·응답 누락 모두 같은 최종 결과이고 제안 상태가 바뀌지 않으며 두 번째 elicitation이 없다. 같은 `idempotency_key` 재호출도 제안을 하나만 만든다.
  - `requestState` 위조: 서명 변조, 만료, 다른 user·workspace·grant·client, 다른 tool, 다른 인자 hash를 각각 거절한다. grant 회수나 정책 비활성화 뒤의 재시도는 현재 인가에서 막힌다.
  - URL: 설정된 origin과 tool별 경로만 나가고 query·fragment·토큰이 없다. 다른 사용자가 같은 URL을 열면 승인 화면이 거절한다(문서·계획·Skill·변환·위키 draft 모두).
  - audit: 첫 호출과 재시도가 v1 필드 그대로 남고 elicitation action·URL·message가 없다.
  - 실제 Claude Code에서 동의창, 브라우저 열림, 재시도 시점을 확인한다.

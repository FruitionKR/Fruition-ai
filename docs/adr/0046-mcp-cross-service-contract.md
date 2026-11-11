# 0046. MCP 서버의 서비스 경계 계약을 먼저 고정한다

- 상태: I0 확정; I2의 bounded 네 read tool code slice는 통제된 통합 증거가 있으나 I2 전체·읽기 출시는 미완료
- 범위: Fruition-access, Fruition-document, Fruition-ai, Fruition-frontend
- 기준 커밋: access `9598906`, document `ba4739d`, ai `2f92132`, frontend `e2742d4`
- 보존된 원안: [사용자 모델이 작업하는 MCP 서버 구현 계획](../plans/0046-mcp-server-source-plan.md)

## 맥락

MCP는 access의 OAuth 인가, document의 리소스 인가·승인·수수료, ai의 검색·Agent·위키 공개, frontend의 동의·미리보기·상태 표시를 한 경로로 묶는다. 각 저장소가 따로 구현을 시작하면 토큰 audience, 회수 시점, 상태 이름, 중복 청구 기준이 달라질 수 있다. 이 ADR은 I0의 저장소 간 계약이며, 보존된 원안의 I0~I9 요구사항은 이 문서가 명시적으로 바꾼 부분 외에는 그대로 유효하다.

이 문서는 프로덕션 구현이나 실제 OAuth·DB 검증 완료를 뜻하지 않는다. 외부 클라이언트 식별자, 가격, 보존 기간, 용량·호출 상한은 확인되거나 결정되지 않았으므로 값을 만들지 않는다.

## 기존 코드에서 재사용할 경계

| 저장소 | 재사용 | 보완이 필요한 이유 |
|---|---|---|
| access | RSA JWT의 `iss`·`aud` 검증과 발급(`JwtTokenProvider`), 기존 로그인, 원본 멤버 조회(`findActiveMember`·`effectivePermissions`) | 기존 JWT audience는 `fruition-api` 하나이고 MCP grant·workspace가 없다. 로그인 교환 코드는 사용자와 선택적 PKCE challenge만 저장한다. refresh는 옛 토큰을 폐기하지만 계열·재사용 탐지·후속 토큰 폐기가 없다. |
| document | `WorkspaceAccessGuard`, `DocumentAccessPolicy`, Agent 승인/실행, append-only credit 원장과 누적 정산 멱등 | 기존 guard는 300초 권한 캐시를 먼저 읽으므로 MCP의 현재 원본 판정에 쓸 수 없다. `CreditService.reserve`의 부분 예약과 `releaseStale`은 MCP 전액 예약·영수증 대사 계약과 다르다. `prepareToolApply`는 실행 영수증이 아니다. |
| ai | 내부 토큰/Agent 서비스 토큰 분리, 저장 벡터 검색, Agent plan/artifact 검증, task journal, 기여 재조립, purge fence | MCP provenance·grant/client·policy generation과 `halted`가 없다. 기존 run 생성은 planning job을 만든다. 기존 위키 본문은 불변 객체와 DB 공개 참조 계약이 아니다. |
| frontend | plan version/hash 승인, 부분 승인, Markdown diff, `skipHtml`, `inert-external`, 취소/복구 상태 표시 | MCP 동의·연결·가격·출처·ingest preview가 없고 `halted`를 모른다. 기존 revise/자동 승인 UI를 MCP에 노출하면 안 된다. |

근거 위치는 [검토 근거](#검토-근거)에 적는다.

## 결정 1: 배치와 프로토콜

1. 외부 MCP Streamable HTTP 엔드포인트와 tool 정의는 document-svc가 소유한다. document-svc는 MCP access token을 검증하는 resource server다.
2. access-svc가 OAuth authorization server와 grant·workspace 정책을 소유한다. MCP 서버는 토큰을 발급하거나 다른 서비스로 전달하지 않는다.
3. ai-svc는 MCP 프로토콜을 처리하지 않는다. document-svc만 기존 서비스 토큰으로 ai-svc를 호출한다.
4. MCP 버전은 `2025-11-25`, Java SDK는 호환되는 정식 `2.1.x`로 시작한다. 세션은 상태의 소유권 증거가 아니며 지속 상태는 아래 handle로 식별한다.
5. OAuth만 지원한다. PAT, 조직 공용 고정 헤더, Dynamic Client Registration, sampling, elicitation, MCP 안의 승인 tool은 제공하지 않는다. 승인 화면을 여는 URL 모드 elicitation만 [0047](0047-mcp-approval-url-elicitation.md)이 예외로 제안한다(제안 상태).
6. 쓰기는 제안만 만들고 사람의 웹 승인 뒤 실행한다. MCP 경로에서 생성형 서버 LLM을 호출하지 않는다.

### Transport, tool definition, audit

- 운영 MCP resource/Streamable HTTP endpoint와 OAuth redirect/metadata URL은 HTTPS만 허용한다. TLS termination 뒤에는 신뢰한 proxy의 canonical scheme/host만 사용하고 client가 보낸 forwarded header를 그대로 신뢰하지 않는다. cleartext 또는 canonical resource와 다른 scheme/host는 redirect로 보정하지 않고 거절한다.
- Streamable HTTP request의 `Origin`이 있으면 canonical origin allowlist와 scheme/host/port를 exact 비교한다. 허용 origin은 통과하고 malformed/null/mismatched origin은 data/tool 처리 전에 403으로 거절한다. native client가 `Origin`을 보내지 않는 경우는 OAuth/token/current-grant 검사를 모두 거친 뒤 허용하며, missing origin을 anonymous/auth bypass로 쓰지 않는다. 지원하는 POST와 GET/DELETE session route에 같은 규칙을 적용한다.
- request body는 decompression 후 4 MiB hard limit이며 초과 body를 parse하거나 일부 수락하지 않는다. endpoint별 더 작은 상한 값은 해당 release gate에서 정한다.
- 공개 tool 이름·설명·input JSON Schema는 source-controlled static `mcp-tools.v1` catalog다. v1 이름은 `search_wiki`, `get_wiki_page`, `find_documents`, `get_document`, `propose_document_edit`, `propose_plan`, `start_ingest`, `resume_ingest`, `get_window`, `submit_window`, `finalize_ingest`, `submit_lint_changes`의 닫힌 목록이며(웹 직접 업로드를 감싸는 `start_document_upload`, `get_upload_part_urls`, `complete_document_upload` 추가와 업로드의 승인 예외는 [ADR 0048](0048-mcp-document-upload.md)) 단계별로 gate를 통과한 subset만 노출한다. 승인/apply/result 조회는 MCP tool이 아니다. catalog/schema가 바뀌면 version과 변경 내역을 함께 올린다. user/workspace가 만든 title/content, client text, Skill name/body를 definition/description/schema/default/example/enum에 넣지 않는다. 팀 Skill과 write-before-launch tool은 목록에도 노출하지 않는다.
- document는 모든 tool attempt에 unknown field 없는 body-free audit v1을 기록한다: `{audit_id, occurred_at, user_id, workspace_id, client_id, grant_id, tool_name, target_ids, result, error_code, origin}`. `target_ids`는 검증된 existing ID의 중복 없는 목록이고, target 없는 search는 빈 목록이다. `result`는 `ALLOWED | DENIED | SUCCEEDED | FAILED | UNKNOWN`; audit에는 request/response body, query, snippet, Markdown, quote, token, authorization code, artifact를 넣지 않는다. 보존 기간은 launch gate다.
- 모든 write 결과/run/draft/receipt/publication event에는 server-resolved `user_id` author와 `origin=MCP_CLIENT`, grant/client를 남긴다. downstream service audit도 이 provenance를 쓰며 MCP bearer token이나 body를 복사하지 않는다.

## 결정 2: Spring Authorization Server 적합성

access에는 **Spring Authorization Server(SAS)를 프로토콜 골격으로 도입한다.** 현재 access가 Spring Boot 3.5.14·Java 21·Spring Security를 쓰므로 같은 3.5 계열과 호환되는 Boot 관리 SAS 1.5 계열을 I1에서 고정한다. Spring Security 7/Boot 4로 올리는 일은 I1의 필요조건이 아니다.

SAS를 선택하는 근거는 다음과 같다.

- authorization code, consent, PKCE public client, refresh token, authorization-server metadata, JWK, revocation endpoint와 별도 authorization-server filter chain을 제공한다.
- `RegisteredClientRepository`, `OAuth2AuthorizationService`, token generator, endpoint converter/provider/validator를 교체할 수 있어 allowlist·Postgres 영속화·추가 검증을 붙일 수 있다.
- JPA 가이드는 출발점일 뿐 운영 스키마를 그대로 보장하지 않는다고 명시하므로 grant와 정책을 SAS 테이블에 억지로 넣지 않고 access 소유 테이블로 분리한다.

다만 기본 설정만으로 이 계약을 충족한다고 보지 않는다.

- 공식 PKCE public-client 가이드는 public client에 refresh token을 발급하지 않는다고 명시한다. Claude의 `none` 클라이언트와 refresh 회전을 함께 지원하려면 I1에서 실제 SAS 버전의 동작을 검증하고, 필요하면 token endpoint provider를 확장해야 한다.
- `reuseRefreshTokens(false)`는 회전만 제공한다. 이미 사용한 토큰의 계열 재사용 탐지, 현재 활성 계열 폐기, grant 보안 회수, 오류 응답과 폐기의 독립 커밋은 access가 구현한다.
- MCP가 요구하는 RFC 8707 `resource`를 authorization/token 요청 양쪽에서 고정하고 코드에 결합하는 로직, `aud`를 MCP URI로 만드는 로직, CIMD URL allowlist/SSRF 방어, `client_id_metadata_document_supported` metadata는 custom validator/converter/metadata response가 맡는다.
- DCR endpoint는 켜지 않는다. `RegisteredClientRepository`는 서버 allowlist에서만 읽는다.

I1 시작 시 사용할 공식 근거:

- [Spring Authorization Server 기능 목록](https://docs.spring.io/spring-authorization-server/reference/overview.html)
- [설정 모델과 교체 가능한 core service](https://docs.spring.io/spring-authorization-server/reference/configuration-model.html)
- [authorization/token endpoint 확장 지점](https://docs.spring.io/spring-authorization-server/reference/protocol-endpoints.html)
- [PKCE public client와 refresh token 제한](https://docs.spring.io/spring-authorization-server/reference/guides/how-to-pkce.html)
- [JPA core service 구현 가이드](https://docs.spring.io/spring-authorization-server/reference/guides/how-to-jpa.html)
- [refresh token 회전 설정](https://docs.spring.io/spring-authorization-server/reference/api/java/org/springframework/security/oauth2/server/authorization/settings/TokenSettings.html)

## 결정 3: 공통 표기

- JSON 필드는 `snake_case`, enum 값은 아래에 적은 대문자 문자열을 쓴다.
- 새 opaque ID(`grant_id`, `invocation_id`, `approval_id`, `receipt_id`, `draft_id`, `event_id`)는 서버가 만든 UUIDv4 문자열이다. 기존 document ID(`doc_`, `chatdoc_` 포함), Agent operation ID, page ID 형식은 바꾸지 않는다.
- 시각은 RFC 3339 UTC 문자열이다.
- 새 content hash는 `sha256:<64 lowercase hex>`, 기존 `operation_hash`는 호환성을 위해 64 lowercase hex를 유지한다.
- 모든 요청의 알 수 없는 필드는 거절한다. ID 형식 검사 뒤에도 workspace·user·grant 소유권을 별도로 확인한다.
- `user_id`, `workspace_id`, `client_id`는 tool 인자에서 받지 않는다. 검증된 토큰과 access의 grant 판정 결과에서 만든다.

## 결정 4: 토큰, scope, client, grant

### MCP JWT claim

MCP access token은 다음 claim을 모두 가진다.

```json
{
  "iss": "<access issuer>",
  "aud": "<canonical MCP resource URI>",
  "sub": "<user_id>",
  "exp": 0,
  "iat": 0,
  "jti": "<opaque id>",
  "grant_id": "<uuid>",
  "workspace_id": "<workspace id>",
  "client_id": "<exact allowlisted CIMD URL>",
  "policy_generation": 0,
  "scope": "read write_propose wiki_write"
}
```

`scope`에는 동의한 값만 공백 구분으로 넣는다. 웹 토큰은 계속 `aud=fruition-api`만 받고, MCP endpoint는 그 토큰을 거절한다. 기존 웹 API는 MCP URI audience 토큰을 거절한다. document는 서명·알고리즘·`kid`·`iss`·정확한 `aud`·`exp`·`sub`를 먼저 검증하고, 이어 access의 현재 판정을 호출한다.

### scope

| scope | 허용 범위 | 추가 권한 |
|---|---|---|
| `read` | `search_wiki`, `get_wiki_page`, `find_documents`, `get_document` | 현재 멤버여야 한다. |
| `write_propose` | `propose_document_edit`, `propose_plan` | 대상별 현재 document/folder 정책을 적용한다. |
| `wiki_write` | ingest tool, `submit_lint_changes` | ingest는 `wiki.ingest`, lint는 `wiki.lint`가 필요하다. |

유효 scope는 `token scopes ∩ current grant scopes ∩ current workspace allowed scopes`다. scope 자동 확대는 없다. step-up이 실제 클라이언트에서 지원되지 않으면 연결을 다시 맺어 동의받는다.

### client와 authorization code

- `client_id`는 allowlist에 있는 HTTPS CIMD URL 전체 문자열과 정확히 같아야 한다. redirect를 따라가지 않는다.
- 네트워크 요청 전에 allowlist와 rate limit을 검사한다. 조회는 사설·loopback·link-local·metadata 주소, 과대 응답, 시간 초과를 차단한다.
- `token_endpoint_auth_method=none`(client secret 없음)과 S256 PKCE만 허용한다. `client_id`는 계속 exact allowlisted CIMD URL이며 authorization/token 요청과 code에 같은 값으로 결합한다. literal `none`이나 다른 client ID로 바꿀 수 없다. DCR은 없다.
- authorization code에는 실제 redirect URI(실제 loopback port 포함), client ID, MCP resource, S256 challenge, user, workspace, scopes, `grant_id`, `policy_generation`을 묶는다. 일회용 소비는 동시 교환에도 한 번만 성공한다.
- 등록 redirect와 요청 redirect 비교에서만 허용된 loopback의 port 예외를 적용한다. code 교환은 code에 고정된 실제 URI와 정확히 같아야 한다.

### grant와 정책 세대

grant는 `ACTIVE`, `USER_DISCONNECTED`, `ADMIN_REVOKED`, `SECURITY_REVOKED`, `PURGED` 중 하나다. terminal grant를 다시 활성화하지 않고 재연결은 새 grant를 만든다. authorization code, access token, refresh token의 만료는 각 credential을 무효화할 뿐 grant 만료 상태나 별도 revocation reason을 만들지 않는다.

workspace MCP 정책은 기본 `DISABLED`이며 `generation`을 가진다. 최초 행의 generation은 1이다. enabled 또는 allowed scope가 실제로 바뀔 때 같은 트랜잭션에서 1 증가한다. no-op 갱신은 증가시키지 않는다. `DISABLED → ENABLED`도 새 세대이므로 옛 proposal/draft가 살아나지 않는다.

grant의 revocation reason은 다음 닫힌 목록이다.

| reason | grant 상태 | 승인 전 proposal/draft | 이미 명시적으로 웹 승인한 실행 |
|---|---|---|---|
| `USER_DISCONNECT` | `USER_DISCONNECTED` | 무효 | 현재 멤버십·권한과 관리자 정책이 유효하면 계속 |
| `ADMIN_GRANT_REVOKE` | `ADMIN_REVOKED` | 무효 | 다음 operation 경계에서 `halted` |
| `WORKSPACE_POLICY_DISABLED` | `ADMIN_REVOKED` | 무효 | 다음 operation 경계에서 `halted` |
| `REFRESH_REUSE` | `SECURITY_REVOKED` | 무효 | 다음 operation 경계에서 `halted` |
| `CLIENT_DISABLED` | `SECURITY_REVOKED` | 무효 | 다음 operation 경계에서 `halted` |
| `USER_PURGED` / `WORKSPACE_PURGED` | `PURGED` | 삭제·무효 | 새 변경 금지, 정산/파기 계약 적용 |

멤버십 종료와 권한 회수는 현재 원본 판정의 deny reason이며 완료 변경을 자동 복구하지 않는다. 필요하면 grant도 별도 회수하지만 권한 캐시 만료를 기다리지 않는다.

## 결정 5: access 현재 인가 DTO

document → access, `X-Internal-Token`:

`POST /internal/mcp/grants/{grant_id}/authorize`

```json
{
  "token_context": {
    "subject": "<sub>",
    "workspace_id": "<claim>",
    "client_id": "<claim>",
    "policy_generation": 1,
    "scopes": ["read"]
  },
  "action": {
    "phase": "TOOL_CALL",
    "required_scopes": ["read"],
    "required_permissions": [],
    "execution_basis": "ACTIVE_GRANT",
    "approved_policy_generation": null
  }
}
```

`phase`는 `TOOL_CALL | PROPOSAL_CREATE | WEB_APPROVAL | OPERATION_BOUNDARY | READ_RECEIPT`, `execution_basis`는 `ACTIVE_GRANT | EXPLICIT_WEB_APPROVAL`이다. `EXPLICIT_WEB_APPROVAL`은 `OPERATION_BOUNDARY`에서만 허용하고 document가 저장한 승인 세대를 `approved_policy_generation`으로 보낸다.

응답:

```json
{
  "decision": "ALLOW",
  "deny_reason": null,
  "grant": {
    "grant_id": "<uuid>",
    "status": "ACTIVE",
    "revocation_reason": null,
    "user_id": "<id>",
    "workspace_id": "<id>",
    "client_id": "<exact CIMD URL>",
    "scopes": ["read"]
  },
  "policy": {
    "enabled": true,
    "generation": 1,
    "allowed_scopes": ["read"]
  },
  "current_access": {
    "role": "MEMBER",
    "permissions": []
  },
  "effective_scopes": ["read"],
  "checked_at": "<RFC 3339 UTC time>"
}
```

`decision`은 `ALLOW | DENY`. deny reason은 `TOKEN_CONTEXT_MISMATCH | GRANT_INACTIVE | POLICY_DISABLED | POLICY_GENERATION_MISMATCH | NOT_MEMBER | PERMISSION_REVOKED | INSUFFICIENT_SCOPE | CLIENT_DISABLED | PURGED | AUTHZ_UNAVAILABLE` 중 하나다. access는 매 요청에 grant·정책·`findActiveMember`·`effectivePermissions`를 같은 현재 판정으로 읽는다. 기존 `authz:role:v2`를 읽거나 양성 결과를 캐시하지 않는다. DB/서비스 실패는 `AUTHZ_UNAVAILABLE`이며 document는 fail-closed 한다.

`USER_DISCONNECT` 예외는 `execution_basis=EXPLICIT_WEB_APPROVAL`, 승인 세대가 현재 세대와 같고, 현재 멤버십·권한이 유효하며, 관리자/보안 회수가 없을 때만 적용한다. caller가 보낸 token context와 저장 grant가 한 필드라도 다르면 deny 한다.

## 결정 6: 내부 actor와 provenance

document가 access 판정으로 만든 다음 DTO를 actor-bearing MCP 전용 요청에 넣는다. `get_wiki_page`의 page GET은 결정 7에서 정의한 예외로 DTO를 넣지 않는다. MCP JWT는 전달하지 않는다.

```json
{
  "invocation_id": "<uuid>",
  "grant_id": "<uuid>",
  "user_id": "<id>",
  "workspace_id": "<id>",
  "client_id": "<exact CIMD URL>",
  "policy_generation": 1,
  "origin": "MCP_CLIENT",
  "spec_version": "2025-11-25",
  "tool_name": "search_wiki"
}
```

`origin`은 `MCP_CLIENT | WEB_AGENT | SYSTEM`의 닫힌 목록이다. MCP 전용 endpoint는 `MCP_CLIENT`만 받는다. document가 필드를 만들고 ai는 run/draft/artifact/outbox에 `grant_id`, `client_id`, `policy_generation`, `origin`, `invocation_id`를 불변으로 저장한다. 클라이언트가 보낸 이름이나 provenance는 저장 근거가 아니다. 표시 이름은 access의 allowlist snapshot에서 읽고 앱 진위 표시로 쓰지 않는다.

## 결정 7: 네 read tool의 exact DTO와 AI 호출 경계

네 tool의 외부 입력에는 actor ID를 넣지 않는다. document가 검증된 token/grant로 [내부 actor](#결정-6-내부-actor와-provenance)를 만들고, actor-bearing 요청인 `search_wiki`에 이를 붙여 ai를 호출한다. `get_wiki_page`는 검증된 workspace와 service authentication으로 기존 GET을 호출하며 actor를 붙이지 않는다. 모든 객체는 unknown field를 거절하고, 모든 문자열·배열·본문 chunk·page size는 I2/I3에서 확정하는 양수 상한을 넘으면 일부를 조용히 버리지 않고 `INVALID_INPUT`, `REQUEST_TOO_LARGE` 또는 `RESPONSE_LIMIT_EXCEEDED`로 거절한다. 단, `search_wiki`·`find_documents`의 snippet은 원문 필드가 아니라 별도 조회 tool로 이어지는 의도적으로 길이를 제한한 검색 excerpt이므로 정해진 snippet 상한에서 자를 수 있고, pagination의 `truncated` 의미를 바꾸지 않는다. 아래 `page_size`의 예시 `1`은 기본값이나 상한이 아니다.

공통 응답의 `data_boundary`는 항상 `UNTRUSTED_WORKSPACE_CONTENT`다. text는 tool definition이나 지시로 합치지 않고 content block의 데이터로만 반환하며 link/image를 inert representation으로 바꾼다. pagination을 쓰는 `search_wiki`·`find_documents`·`get_document`에서만 `next_cursor`가 있으면 `truncated=true`, 없으면 `false`다. 그 cursor는 opaque이며 user·workspace·grant·tool·정규화한 query/target·정렬·page version/content hash에 묶고, 다음 page/chunk에서도 access를 다시 판정한다. cursor가 가리키는 version이 바뀌면 새 내용을 섞지 않고 `STALE_CURSOR`를 반환한다. `get_wiki_page`는 cursor나 chunk를 쓰지 않는 단일 bounded body read다.

### 위키 evidence locator

위키 근거의 식별자는 실제 source of truth마다 block ID 단독이 아닌 합성 locator다. 공개 `search_wiki` evidence와 그 search cursor는 저장된 exact `{ "document_id": "doc_<id>", "block_id": "B0001" }`를 쓰며, quote가 필요한 경우에만 `quote`를 더한다. document는 반환 전 현재 workspace/read 접근과 search result의 page version/content hash, document의 block provenance를 서버 저장 상태와 대조한다. 새 I9 ingest/lint draft의 ai 내부·저장·hash·apply 경계만 unknown field 없는 `{ "snapshot_id": "<server-created uuid>", "block_id": "B0001" }`를 쓴다. `(document_id, block_id)`와 `(snapshot_id, block_id)` 쌍은 각 list 안에서 unique하며 `block_id`만으로 dedupe·조인·충돌 판정하지 않는다.

I9 draft가 internal locator를 반환하거나 저장할 때는 같은 immutable body에 server-created `source_snapshots[{snapshot_id, document_id, source_version, source_content_hash}]`를 두고 모든 참조 snapshot을 정확히 한 번 mapping한다. 한 body/draft에서 `snapshot_id`와 `document_id`는 각각 unique해야 하며 하나의 document를 두 snapshot으로 모호하게 projection하지 않는다. document는 mapping의 workspace, read 권한, version/hash, block 소유를 서버 저장 상태와 대조한 뒤 frontend에만 document locator로 projection하며 client가 보낸 mapping은 받지 않는다. published search cursor의 canonical payload/hash에는 ordered document-qualified locator와 published page version/content hash를 포함한다. `submit_window`의 외부 evidence는 document locator로 받아 run에 고정된 단일 source snapshot의 internal locator로 치환하고, lint는 검증된 `source_documents` mapping으로 같이 치환한다. preview는 internal locator를 hash하고 frontend에만 document locator를 보이며, apply는 저장된 snapshot ID·version·hash·block을 다시 검증하고 document ID로 재매핑하지 않는다.

### `search_wiki`

External input은 `{ "query": "<nonblank bounded text>", "cursor": null, "page_size": 1 }`이다. initial request의 cursor는 null이고 continuation에서는 query와 page_size도 최초 값과 정확히 같아야 한다. document → ai는 `X-Internal-Token`으로 다음 versioned DTO를 쓴다.

`POST /internal/mcp/wiki/search`

```json
{
  "schema_version": "mcp.wiki-search.v1",
  "actor": { "invocation_id": "<uuid>", "grant_id": "<uuid>", "user_id": "<id>", "workspace_id": "<id>", "client_id": "<url>", "policy_generation": 1, "origin": "MCP_CLIENT", "spec_version": "2025-11-25", "tool_name": "search_wiki" },
  "query": "<nonblank bounded text>",
  "cursor": null,
  "page_size": 1
}
```

```json
{
  "schema_version": "mcp.wiki-search.v1",
  "results": [{
    "page_id": "<existing wiki page id>",
    "page_type": "SOURCE",
    "title": "<bounded untrusted text>",
    "version": 1,
    "content_hash": "sha256:<64 lowercase hex>",
    "snippet": "<bounded untrusted text>",
    "evidence": [{ "document_id": "doc_<id>", "block_id": "B0001" }]
  }],
  "next_cursor": null,
  "truncated": false,
  "data_boundary": "UNTRUSTED_WORKSPACE_CONTENT"
}
```

`page_type`은 `SOURCE | CONCEPT`이다. 위 response는 published read의 internal/external exact DTO이며 `evidence`는 그대로 `[{document_id, block_id}]`로 반환한다. `evidence`는 빈 배열일 수 있지만, 값이 있으면 document가 현재 workspace/read 접근과 해당 고정 page version/content hash의 document block provenance를 검증해야 한다. page ID와 block ID는 기존 wiki 도메인 validator와 길이 상한을 통과해야 하고 UUID로 일괄 변환하지 않는다. 후보 조회 단계부터 개수·총 text bytes·deadline 상한을 적용한다. 질문만 임베딩하고 저장 vector가 없는 본문은 `without_live_embedding()`으로 즉석 임베딩하지 않는다. 답변 생성기, 평가기, 요약기, `/agent/turn`은 호출하지 않는다.

### `get_wiki_page`

External input은 기존 bounded page ID grammar를 통과한 `{ "page_id": "<existing wiki page id>" }`뿐이다. client가 workspace/user/cursor를 보내지 않으며 document가 현재 authorization을 검증해 workspace context를 만든다. document는 웹 상세 조회(`WikiService.read`, 기존 AI route `GET /wiki/pages/{page_id}?workspace_id=<validated context>`)를 그대로 써서 웹과 같은 순서(AI 본문 → 최신 `wiki_page_versions.markdown` → 저장 객체)로 본문을 고른다. MCP 전용 AI route는 두지 않는다(2026-11 `mcp_read` 분기 제거).

```json
{
  "schema_version": "mcp.wiki-page.v1",
  "page_id": "<id>",
  "page_type": "CONCEPT",
  "title": "<bounded untrusted text>",
  "body": "# 제목\n\n본문\n",
  "content_hash": "sha256:0cbe37d6300159221d66c8bdc45b489caa97abdc10726d12af7a2417497f14c8",
  "data_boundary": "UNTRUSTED_WORKSPACE_CONTENT"
}
```

응답은 정확히 `schema_version`, `page_id`, `page_type`, `title`, `body`, `content_hash`, `data_boundary`의 일곱 text field다. `version`, `evidence`, `quote`, `next_cursor`, `truncated`, chunk 또는 raw-hash field는 없다. body는 한 번에 반환되며 비어 있을 수 있다. body·metadata·전체 response 중 어느 하나가 bounded limit을 넘으면 조용히 자르지 않고 `RESPONSE_LIMIT_EXCEEDED`로 거절한다.

document는 본문을 고른 뒤 page metadata(`page_type`, `title`, `markdown_uri`, `status`, `updated_at`)와 최신 revision을 다시 읽어 read 중 바뀌었거나 active가 아니면 retryable `PAGE_CHANGED`로 거절한다. 전체 읽기는 `MCP_WIKI_PAGE_DEADLINE_MS` 안에 끝나야 하고 넘으면 `DEPENDENCY_UNAVAILABLE`이다. 저장 객체를 읽었는데 비어 있는 본문은 빈 `body`로 정상 반환하고, 끝내 본문을 구하지 못하면 `DEPENDENCY_UNAVAILABLE`이다. document는 기존 Markdown sanitizer로 title/body를 neutralize하고, public `content_hash`를 **반환한 sanitized body의 exact UTF-8 SHA-256**으로 계산한다. 따라서 identity를 섞지 않는다: 저장 본문 identity, MCP public returned-representation identity, 그리고 검색의 version/hash/evidence identity는 각각 별개다. 이 read는 immutable publication, approval/apply identity 또는 I8 publication identity를 주장하지 않으며 I8을 기다리지 않는다.

### `find_documents`

document 내부에서 처리하며 external input은 `{ "query": "<nonblank bounded text>", "cursor": null, "page_size": 1 }`이다. `DocumentAccessPolicy`의 현재 access context로 검색 후보부터 workspace와 view 권한을 제한한다.

```json
{
  "schema_version": "mcp.document-search.v1",
  "results": [{
    "document_id": "doc_<existing id>",
    "display_name": "<bounded untrusted text>",
    "current_version": 1,
    "content_hash": "sha256:<64 lowercase hex>",
    "snippet": "<bounded untrusted text>",
    "evidence_block_ids": ["B0001"]
  }],
  "next_cursor": null,
  "truncated": false,
  "data_boundary": "UNTRUSTED_WORKSPACE_CONTENT"
}
```

### `get_document`

document 내부에서 처리하며 external input은 `{ "document_id": "<existing document id>", "cursor": null }`이다.

```json
{
  "schema_version": "mcp.document-read.v1",
  "document_id": "doc_<existing id>",
  "display_name": "<bounded untrusted text>",
  "current_version": 1,
  "content_hash": "sha256:<64 lowercase hex>",
  "blocks": [{ "block_id": "B0001", "ordinal": 1, "text": "<bounded untrusted text>" }],
  "next_cursor": null,
  "truncated": false,
  "data_boundary": "UNTRUSTED_WORKSPACE_CONTENT"
}
```

`document_id`는 기존 `doc_`·`chatdoc_` 포함 문자열 ID validator와 길이 상한을 재사용하고 UUID를 요구하지 않는다. `current_version`과 `ordinal`은 1 이상의 integer다. block ID는 그 document version에 실제 속해야 하며 중복할 수 없다. cursor는 document version/hash에 묶여 content를 block 경계에서만 나눈다. `find_documents`와 `get_document` 모두 다른 workspace/owner의 ID를 `HANDLE_NOT_FOUND`와 구분되지 않는 `RESOURCE_NOT_FOUND`로 답한다.

## 결정 8: MCP 제안과 승인 DTO

외부 `propose_document_edit` input은 unknown field 없는 `{idempotency_key, document_id, base_version, target, instruction, replacement_markdown}`이다. document/base/target은 아래 validator를 쓰고 instruction은 nonblank bounded untrusted text, replacement는 artifact Markdown이다. document가 one-operation proposal과 artifact ID/hash를 만든다. 외부 `propose_plan` input은 unknown field 없는 `{idempotency_key, request_summary, target_folder_id, operations, artifacts}`이며 operation/artifact는 아래 exact v1 shape다. 외부 input에는 actor, workspace/user/client, provenance, request/operation hash, status/warning을 받지 않는다.

document → ai, `X-Agent-Service-Token`:

`POST /agent/runs/mcp-proposals`

```json
{
  "schema_version": "mcp.agent-proposal.v1",
  "actor": { "invocation_id": "<uuid>", "grant_id": "<uuid>", "user_id": "<id>", "workspace_id": "<id>", "client_id": "<url>", "policy_generation": 1, "origin": "MCP_CLIENT", "spec_version": "2025-11-25", "tool_name": "propose_document_edit" },
  "idempotency_key": "<client supplied bounded string>",
  "request_hash": "sha256:<64 lowercase hex>",
  "proposal": {
    "instruction": "<raw untrusted instruction>",
    "request_summary": "<untrusted text>",
    "target_folder_id": null,
    "operations": [{
      "id": "op-1",
      "sequence": 1,
      "tool_name": "apply_document_edit",
      "target_type": "document",
      "target_id": "doc_<existing id>",
      "base_version": 1,
      "source_parent_id": null,
      "destination_parent_id": null,
      "arguments": {
        "document_id": "doc_<existing id>",
        "base_version": 1,
        "target": { "type": "selection", "start_line": 1, "end_line": 1 },
        "content_artifact_id": "artifact-1",
        "content_hash": "sha256:<64 lowercase hex>"
      },
      "reason": "<bounded untrusted text>",
      "depends_on": []
    }],
    "artifacts": [{
      "id": "artifact-1",
      "content_hash": "sha256:<64 lowercase hex>",
      "purpose": "apply_document_edit",
      "document_id": "doc_<existing id>",
      "base_version": 1,
      "target": { "type": "selection", "start_line": 1, "end_line": 1 },
      "markdown": "<replacement Markdown>"
    }]
  }
}
```

응답:

```json
{
  "schema_version": "mcp.agent-proposal.v1",
  "run_id": "<uuid>",
  "plan_id": "<uuid>",
  "plan_version": 1,
  "operation_hash": "<64 hex>",
  "status": "awaiting_approval"
}
```

`proposal.operations`는 1~20개이고 `sequence`는 1부터 빈틈없이 증가한다. operation/artifact ID는 요청 안에서 unique한 nonblank bounded string이며 기존 Agent operation ID 형식을 유지한다. `depends_on`은 같은 plan의 앞선 operation ID만 중복 없이 operation sequence 순으로 둔다. request operation에는 `status`, `error_code`, `warning`, `suggested_selected`, 실행 결과를 넣을 수 없다. 이들은 server-owned response field다. `target_folder_id`와 folder ID는 UUID 또는 null(root)이고, document ID는 기존 문자열 validator를 쓴다. version은 1 이상의 integer다. boolean, fractional number, overflow integer는 integer로 받지 않는다.

`proposal.instruction`은 `propose_document_edit`에서 필수 string, `propose_plan`에서 exact null이다. one-operation mapping은 외부 `instruction`을 JSON decode한 그대로 `proposal.instruction`에 보존하고, 기존 UI/plan field인 `request_summary`와 유일 operation의 `reason`에는 그 instruction의 계약상 trim된 값을 넣는다. nonblank/길이 검사는 trim 값으로 하지만 raw instruction에는 trim, Unicode normalization, newline 변환, 공백 축약을 적용하지 않는다. 따라서 같은 normalized summary/reason·operation·artifact와 같은 idempotency key라도 raw instruction이 다르면 다른 request hash이고 `IDEMPOTENCY_CONFLICT`다. `propose_plan`은 외부 `request_summary`를 계약대로 trim하며 임의 instruction을 파생하지 않는다.

operation top-level shape와 argument key 집합은 기존 `AgentPlanOperation`/document `AgentToolService` v1 계약을 그대로 versioning한 다음 닫힌 표다. `source_parent_id`/`destination_parent_id`는 hierarchy snapshot에서 검증한 direct existing parent이며 root는 null이다. create는 source가 null이고, rename/edit는 두 parent가 같으며, move만 두 값이 달라질 수 있다. destination argument가 아래 operation-result ref이면 아직 existing parent ID가 없으므로 top-level `destination_parent_id`는 null이고 dependency/ref가 binding을 대신한다.

| `tool_name` | target/base 규칙 | exact `arguments` |
|---|---|---|
| `create_folder` | `target_type=folder`, target/base null | `name`(nonblank bounded text), `parent_folder_id`(folder ID/null/아래 result ref) |
| `rename_folder` | existing folder, positive base | `folder_id`, `name`, `base_version` |
| `move_folder` | existing folder, positive base | `folder_id`, `parent_folder_id`(folder ID/null/result ref), `position`(null 또는 0 이상 integer), `base_version` |
| `move_document` | existing document, positive base | `document_id`, `folder_id`(folder ID/null/result ref), `position`(null 또는 0 이상 integer), `base_version` |
| `rename_document` | existing document, positive base | `document_id`, `display_name`(nonblank bounded text), `base_version` |
| `create_document` | `target_type=document`, target/base null | `display_name`, `folder_id`(folder ID/null/result ref), `content_artifact_id`, `content_hash` |
| `create_document`(MCP PDF 변환본) | `target_type=document`, target/base null | `display_name`, `folder_id`(folder ID/null/result ref), `content_result_key`(`tmp/mcp-convert/{workspace_id}/{conversion_id}/final.json`), `content_hash`, `source_document_id`, `source_content_hash`. artifact를 싣지 않는다 |
| `apply_document_edit` | existing document, positive base | `document_id`, `base_version`, `target`, `content_artifact_id`, `content_hash` |

PDF 변환본 `create_document`는 Fruition-document `finalize_pdf_conversion`의 MCP proposal에서만 받고 내부 Agent planning(LLM) 계획은 받지 않는다. 본문은 artifact가 아니라 같은 workspace·user의 MCP PDF 변환 세션이 finalize한 결과 key와 그 `content_hash`다. document가 승인·실행 때 원본 PDF 권한과 `source_content_hash`를 다시 확인하고, 실행 때 key에서 결과를 읽어 hash를 확인한 뒤 웹 변환과 같은 코드로 저장한다(`docs/api/agent.md`). 아래 artifact 참조 규칙(모든 문서 mutation이 artifact 하나를 참조)은 이 변형에 적용하지 않는다.

result reference는 다른 object 모양을 허용하지 않는다. destination folder는 `{ "$operation_result": "<earlier create_folder operation id>", "field": "id" }`, version은 `{ "$operation_result": "<earlier same-target operation id>", "field": "current_version" }`만 허용한다. reference한 operation은 `depends_on`에 있어야 한다. direct scalar target/base/parent는 대응 top-level 값과 같아야 하고 result ref는 위 null/dependency 규칙을 따라야 한다. hierarchy snapshot에 없는 target/destination, 자기 하위 folder 이동, target folder 자체의 이동/rename, folder 범위 이탈도 거절한다.

`target`/artifact source range는 unknown field 없는 `{ "type", "start_line", "end_line" }`이다. `type`은 `selection | current_section | whole_document`, line은 base version 기준 1-based inclusive integer이고 `1 <= start_line <= end_line <= line_count`다. `whole_document`는 `start_line=1`, `end_line=line_count`여야 한다. artifact는 unknown field 없는 `{id, content_hash, purpose, document_id, base_version, target, markdown}`이다. `purpose`는 `create_document | apply_document_edit`; create artifact의 document/base/target은 null, edit artifact는 operation의 document/base/target과 모두 같아야 한다. artifact는 정확히 한 mutation operation에서 참조되고 참조되지 않은 artifact는 거절한다. `content_hash`는 exact UTF-8 markdown bytes의 `sha256:<64 lowercase hex>`이며 `markdown`은 4 MiB 전체 request 제한과 I4에서 정할 더 작은 field/storage 상한을 모두 지킨다.

ai는 LLM planning job 없이 결정적 정규화·hierarchy/artifact/folder 범위·Markdown 검증을 통과한 plan과 artifact를 원자적으로 저장한다. `create_with_planning_job`을 호출하지 않는다. 이름/본문 중복 의심은 기존처럼 warning과 `suggested_selected=false`이며 제출 전체 실패가 아니다. 반면 current hierarchy/base content만으로 확정되는 obvious no-op(rename 결과가 현재 normalized name과 같음, move의 resolved parent/position이 같음, edit artifact 적용 결과가 대상 범위와 byte-for-byte 같음)가 하나라도 있으면 run/artifact/idempotency success를 만들지 않고 제출 전체를 `OBVIOUS_NO_OP`로 거절한다. 오류의 `operation_error`에 offending `operation_id`, `sequence`, `tool_name`, `reason_code=UNCHANGED_TARGET`를 넣는다. create의 이름/본문 중복은 no-op으로 추론하지 않고 위 warning 경로를 유지한다. 실행 경합이나 실행기 정규화 뒤에만 알 수 있는 no-op은 receipt `NO_CHANGE/changed=false` fallback으로 남긴다. provenance는 server field이고 변경할 수 없다. MCP run에는 revise가 없다.

### canonical body와 hash

모든 canonical JSON은 duplicate object key, NaN/Infinity, invalid UTF-8을 먼저 거절한 뒤 Python의 기존 `_canonical_json`/`_operation_hash`와 같은 규칙(`ensure_ascii=false`, object key lexicographic sort, insignificant whitespace 없음, UTF-8, array order 보존)으로 직렬화한다. domain normalization은 trim이 이미 계약인 name/summary/reason, resolved result reference, normalized dependency order, folder scope만 수행한다. Markdown, quote, instruction, ID에는 Unicode normalization이나 trim을 적용하지 않는다.

- artifact `content_hash`는 위 exact markdown bytes의 SHA-256이다.
- proposal `request_hash`는 canonical `{schema_version, actor: actor_without_invocation_id, proposal}`의 SHA-256이다. 직렬화한 key는 계속 `actor`이고 그 object에서 `invocation_id`만 뺀다. `actor.invocation_id`, `idempotency_key`, `request_hash`, HTTP header, trace ID, delivery attempt/time은 transport-only라 제외한다. grant/user/workspace/client/policy generation/origin/spec/tool, raw `proposal.instruction`, normalized summary/reason/operations, artifact Markdown를 포함한다. 따라서 새 `invocation_id`로 보낸 legitimate retry는 같은 hash이고, raw instruction·content·actor identity 중 하나라도 바뀐 retry는 conflict다.
- 기존 64-hex `operation_hash`는 sequence로 정렬한 operation 각각의 `{id, sequence, tool_name, target_type, target_id, base_version, source_parent_id, destination_parent_id, arguments, depends_on}`만 위 serializer로 hash한다. reason, artifact body, status/error/warning/suggested-selected는 제외하되 arguments의 artifact ID/hash/target 때문에 실행 content에 결합된다.
- document 승인 idempotency의 canonical body는 `{schema_version, approval_id, run_id, plan_version, operation_hash, selected_operation_ids_in_plan_order, grant_id, client_id, policy_generation, approval_mode, price_snapshot}`이다. document→ai 승인 body의 canonical form은 가격을 제외한 `{schema_version, approval_id, run_id, plan_version, operation_hash, selected_operation_ids_in_plan_order, grant_id, client_id, policy_generation, approval_mode}`다. 두 form 모두 invocation/request/trace/time/header를 제외한다. 같은 approval ID의 다른 canonical body는 `IDEMPOTENCY_CONFLICT`다.

I4는 아래 canonical UTF-8 preimage를 golden vector `instruction-v1`로 고정한다. `actor.invocation_id`은 이미 제외된 상태다.

```json
{"actor":{"client_id":"https://client.example/metadata","grant_id":"00000000-0000-4000-8000-000000000001","origin":"MCP_CLIENT","policy_generation":1,"spec_version":"2025-11-25","tool_name":"propose_document_edit","user_id":"user-1","workspace_id":"workspace-1"},"proposal":{"artifacts":[{"base_version":1,"content_hash":"sha256:2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881","document_id":"doc_1","id":"artifact-1","markdown":"x","purpose":"apply_document_edit","target":{"end_line":1,"start_line":1,"type":"selection"}}],"instruction":"Fix title","operations":[{"arguments":{"base_version":1,"content_artifact_id":"artifact-1","content_hash":"sha256:2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881","document_id":"doc_1","target":{"end_line":1,"start_line":1,"type":"selection"}},"base_version":1,"depends_on":[],"destination_parent_id":null,"id":"op-1","reason":"Fix title","sequence":1,"source_parent_id":null,"target_id":"doc_1","target_type":"document","tool_name":"apply_document_edit"}],"request_summary":"Fix title","target_folder_id":null},"schema_version":"mcp.agent-proposal.v1"}
```

| vector | 변경 | expected request hash / 결과 |
|---|---|---|
| `instruction-v1` | 위 preimage | `sha256:9e02041dfc0c922640e56e612b235890e99d3b1074d13a67893fdfb577725c03` |
| `instruction-v1-new-invocation` | canonical body는 같고 제외된 `actor.invocation_id`만 변경 | 같은 hash, same-key replay |
| `instruction-v2` | `proposal.instruction` 하나만 raw `"Fix title "`로 변경; normalized `request_summary`/`reason`과 operation/artifact는 같음 | `sha256:5c959495edcb3e8504cd4b8767b390baa7e21328819f1ab0bee12dc81a90be2f`; `instruction-v1`과 같은 key면 conflict |

document가 반환하는 external tool result는 `run_id`, 승인 화면으로 가는 자사 HTTPS URL, `status=awaiting_approval`만 포함한다. 승인 URL은 인증 수단이 아니며 웹에서 사용자·workspace와 현재 정책을 다시 확인한다.

명시적 웹 승인에서 document가 `approval_id`를 만들고 가격 snapshot과 전액 예약을 먼저 커밋한 뒤 ai에 보낸다.

`POST /agent/runs/{run_id}/approve`

```json
{
  "schema_version": "mcp.agent-approval.v1",
  "workspace_id": "<id>",
  "user_id": "<id>",
  "plan_version": 1,
  "operation_hash": "<64 hex>",
  "operation_ids": ["<operation id>"],
  "approval_id": "<uuid>",
  "approval_mode": "EXPLICIT_WEB",
  "grant_id": "<uuid>",
  "client_id": "<url>",
  "policy_generation": 1
}
```

`approval_mode`은 MCP provenance run에서 `EXPLICIT_WEB`만 허용한다. `auto:user-setting`, plan-ready 자동 승인, 직접 내부 API 자동 승인, 다른 grant/client/generation은 거절한다. 같은 `approval_id`와 같은 canonical body는 같은 결과를 돌려주고, 같은 ID의 다른 body는 conflict다. 취소·예약 해제된 approval ID의 지연 enqueue는 거절한다.

가격은 ai 요청에 넣지 않는다. document의 Agent approval `price_snapshot`은 unknown field 없는 `{unit_price_minor, plan_cap_minor, currency, price_version, maximum_charge_minor}`다. 금액은 0 이상의 integer minor unit, currency는 configured ISO 4217 code, price version은 nonblank bounded immutable ID이며 `maximum_charge_minor=min(selected_count*unit_price_minor, plan_cap_minor)`다. approval record는 이 snapshot과 선택 operation IDs, grant/client/policy generation을 함께 고정한다. 실제 숫자·currency·price version ID는 출시 gate에서 결정한다.

## 결정 9: 실행 영수증, 위키 결과 이벤트, 수수료

### document operation receipt

각 document 변경과 같은 document DB 트랜잭션에서 다음 본문 없는 영수증을 쓴다.

```json
{
  "receipt_id": "<uuid>",
  "approval_id": "<uuid>",
  "operation_id": "<stable plan operation id>",
  "attempt_id": "<uuid>",
  "workspace_id": "<id>",
  "user_id": "<id>",
  "grant_id": "<uuid>",
  "client_id": "<url>",
  "tool_name": "apply_document_edit",
  "outcome": "COMMITTED",
  "changed": true,
  "resource_type": "DOCUMENT",
  "resource_id": "<id>",
  "before_version": 1,
  "after_version": 2,
  "committed_at": "<time>"
}
```

`outcome`은 `COMMITTED | NO_CHANGE`; `NO_CHANGE`는 `changed=false`다. 실패·timeout은 영수증으로 위장하지 않고 별도 attempt에 `STARTED | FAILED | UNKNOWN`을 남긴다. `(approval_id, operation_id)`는 성공 영수증 하나만 허용한다. 서비스 인증 read-only 조회는 actor 범위와 approval/operation을 모두 요구하고 `COMMITTED | NO_CHANGE | IN_PROGRESS | FAILED | UNKNOWN | NOT_FOUND`를 돌려준다. execute 재호출로 대사하지 않는다.

수수료는 `COMMITTED && changed=true`인 고유 operation만 센다. `NO_CHANGE`, 실패, 미반영은 무료다. 응답 유실이나 timeout 뒤 receipt/attempt가 확정되지 않으면 정산을 `PENDING_RECONCILIATION`으로 둔다.

### AI wiki publication event

wiki 공개 상태·불변 본문 참조·기여·링크·outbox를 같은 ai DB 트랜잭션에 기록한다. outbox event는 `mcp.wiki.publication.v1`이다.

```json
{
  "event_id": "<uuid>",
  "event_type": "mcp.wiki.publication.v1",
  "approval_id": "<uuid>",
  "operation_id": "<stable operation id>",
  "draft_id": "<uuid>",
  "workspace_id": "<id>",
  "user_id": "<id>",
  "grant_id": "<uuid>",
  "client_id": "<url>",
  "kind": "INGEST",
  "outcome": "PUBLISHED",
  "changed": true,
  "published_reference_hash": "sha256:<hex>",
  "committed_at": "<time>"
}
```

`kind`는 `INGEST | LINT`, `outcome`은 `PUBLISHED | NO_CHANGE`. `(approval_id, operation_id)`은 한 결과만 허용한다. document는 event ID로 inbox dedupe하고 결과 조회로 누락/순서 역전을 대사한다. ai run terminal status는 청구 증거가 아니다.

### 수수료 소유권

- document가 가격, 전액 예약, 수수료 원장, 차감·해제·조정, receipt/event 대사를 소유한다.
- ai `/usage`는 provider/model/token 원장 그대로 유지한다.
- 기존 `CreditService.admit`의 부분 예약과 `releaseStale`은 MCP reservation에 적용하지 않는다. reservation type을 분리한다.
- 실제 성공한 승인 변경 수와 approval별 상한으로 청구한다. 부분 실패·취소·`halted` 뒤에도 확정 성공분은 청구한다. 이후 복구는 환불 사유가 아니며 오청구는 append-only adjustment로 정정한다.
- 읽기 tool은 변경 건별 수수료 대상이 아니고 호출 한도 대상이다. 요금제 포함 여부와 한도 값은 미결이다.

## 결정 10: handle과 오류

### handle binding

| handle | 생성자 | 서버 결합 값 | 재조회 규칙 |
|---|---|---|---|
| `grant_id` | access | user, workspace, client, scopes, policy generation | access 현재 상태 필수 |
| `Mcp-Session-Id` | document SDK | user, workspace, grant | POST와 지원 GET/DELETE마다 token+grant+소유권 재검사 |
| search `cursor` | ai/document | user, workspace, grant, query hash, ordering, published page version/content hash, ordered document-qualified evidence locators | 매 page 재인가; 다른 owner에는 not found |
| `run_id` | ai | user, workspace, grant, client, provenance | 다른 owner/grant에는 not found |
| `draft_id` | ai | run, source snapshots/document mapping, composite evidence locators, grant, client, policy generation | 승인 전 현재 grant/policy 필수 |
| `window_id` | ai | draft, ordinal, source snapshot hash | 순서·중복·payload hash 검사 |
| `approval_id` | document | run/draft version+hash, selected operations, price snapshot, actor | 동일 canonical body만 멱등 |
| `operation_id` | ai plan | plan/approval | receipt/event unique domain key |
| `receipt_id` / `event_id` | 소유 서비스 | approval+operation | 본문 없는 대사 전용 |
| `idempotency_key` | MCP client | grant, user, workspace, tool, target handle | 인가 뒤 조회; 같은 key 다른 request hash는 conflict |

handle은 충분한 인증 정보가 아니다. 존재하지 않거나 다른 user/workspace/grant의 handle은 정보 노출을 피하려고 `HANDLE_NOT_FOUND`로 동일 응답한다.

### 오류 envelope

OAuth endpoint는 RFC OAuth error를 쓴다. MCP HTTP/tool adapter와 내부 MCP API의 도메인 오류는 다음 envelope를 쓴다.

```json
{
  "error": {
    "code": "STALE_DRAFT",
    "message": "<safe user-facing message>",
    "retryable": false,
    "handle": "<optional existing handle>",
    "state": "<optional current state>",
    "operation_error": null
  }
}
```

`operation_error`는 operation별 입력 오류에만 `{ "operation_id": "<id>", "sequence": 1, "tool_name": "<closed tool>", "reason_code": "<closed reason>" }`이고 그 외에는 null이다. 본문, arguments, quote, provider 예외는 넣지 않는다.

| HTTP | code | 의미 |
|---|---|---|
| 400 | `INVALID_INPUT`, `INVALID_HANDLE_FORMAT`, `UNSUPPORTED_TOOL`, `OBVIOUS_NO_OP` | schema/형식/닫힌 tool 목록 오류; no-op은 offending operation 포함, 제출 전체 미생성 |
| 401 | `INVALID_TOKEN` | 서명·issuer·audience·만료·필수 claim 오류; `WWW-Authenticate`에 `resource_metadata` 포함 |
| 402 | `INSUFFICIENT_CREDIT` | 최대 청구액 전액 예약 불가 |
| 403 | `INSUFFICIENT_SCOPE` | `WWW-Authenticate`에 `error`, 필요한 `scope`, `resource_metadata` 포함 |
| 403 | `MCP_POLICY_DISABLED`, `GRANT_REVOKED`, `PERMISSION_REVOKED` | 현재 access 판정 거절 |
| 404 | `HANDLE_NOT_FOUND`, `RESOURCE_NOT_FOUND` | 없음과 다른 owner를 구분하지 않음 |
| 409 | `IDEMPOTENCY_CONFLICT`, `IDEMPOTENCY_IN_PROGRESS`, `STALE_PLAN`, `STALE_DRAFT`, `STALE_CURSOR`, `SOURCE_CHANGED`, `PUBLIC_STATE_CHANGED`, `ACTIVE_RUN_EXISTS`, `APPROVAL_CANCELLED` | 재시도/경합/고정 snapshot 불일치 |
| 410 | `HANDLE_EXPIRED` | TTL이 확정된 뒤 만료된 draft/cursor |
| 413 | `REQUEST_TOO_LARGE`, `RESPONSE_LIMIT_EXCEEDED`, `PREVIEW_TOO_LARGE` | 4 MiB body 또는 출시 시 확정할 response/preview 상한 초과; 조용한 truncate 금지 |
| 429 | `RATE_LIMITED` | client/user/workspace/IP 한도 |
| 503 | `AUTHZ_UNAVAILABLE`, `DEPENDENCY_UNAVAILABLE`, `RECONCILIATION_PENDING` | fail-closed 또는 미확정 |

scope 부족은 JSON-RPC tool 오류만으로 바꾸지 않는다. 외부 오류에는 provider/DB 예외 원문이나 본문을 넣지 않는다.

## 결정 11: 상태 전이

### OAuth/grant/policy

```text
workspace policy: DISABLED(g) -> ENABLED(g+1) -> DISABLED(g+2) -> ENABLED(g+3)
grant: ACTIVE -> USER_DISCONNECTED | ADMIN_REVOKED | SECURITY_REVOKED | PURGED
refresh: ACTIVE(n) -> ROTATED(n+1); reused(n) -> family REVOKED + grant SECURITY_REVOKED
```

terminal grant와 옛 policy generation은 되살리지 않는다.

### MCP Agent proposal

```text
validated -> awaiting_approval
awaiting_approval -> rejected | invalidated | executing
executing -> verifying -> completed | partial_failed | failed | conflicted
executing/verifying --admin/security/policy stop at next boundary--> halted
executing/verifying --user cancel--> cancel_requested -> rolling_back -> cancelled | rollback_failed
```

MCP에는 planning/clarification/revise/자동 승인을 만들지 않는다. 기존 schema 호환 때문에 run row가 `queued`를 잠깐 거쳐야 한다면 같은 트랜잭션 밖으로 관찰되면 안 된다. 예산 소진은 미착수 operation을 `skipped`로 기록하고 기존 집계로 끝낸다. `halted`는 자동 rollback을 하지 않으며 사용자가 별도 복구를 요청할 수 있다.

사용자 연결 해제는 승인 전 `invalidated`, 승인 후 현재 권한이 있으면 계속이다. 관리자 비활성화·관리자 회수·refresh 재사용은 승인 후에도 다음 operation 경계에서 `halted`다. 이미 인가된 단일 operation의 늦은 commit까지 되돌렸다고 주장하지 않고 receipt로 대사한다.

### ingest/lint draft

```text
ACTIVE_WINDOWING -> READY_TO_FINALIZE -> AWAITING_WEB_APPROVAL
AWAITING_WEB_APPROVAL -> REJECTED | INVALIDATED | APPLYING
APPLYING -> PUBLISHED | NO_CHANGE | CONFLICTED | HALTED | UNKNOWN
ACTIVE_WINDOWING/READY_TO_FINALIZE -> EXPIRED | CANCELLED
```

`start_ingest`가 source version/hash/block snapshot을 고정한다. `get_window`/`submit_window`는 같은 snapshot만 쓴다. finalize는 전체 연쇄 변경과 공개 상태 지문을 draft hash에 넣는다. 승인 뒤 draft는 불변이다. apply 직전 source와 공개 상태를 다시 계산해 다르면 `CONFLICTED`이고 새 preview가 필요하다.

### 수수료

```text
RESERVED -> EXECUTING -> PENDING_RECONCILIATION -> SETTLED
RESERVED -> RELEASED
SETTLED -> ADJUSTED   (오청구 정정만)
```

run terminal과 settlement terminal은 별도다. `UNKNOWN` operation이 있으면 run이 끝나도 예약을 자동 해제하지 않는다. 처리 기한은 미결 gate다.

### 불변 위키 공개

```text
draft body -> immutable object uploaded (not visible)
-> ai DB transaction commits body reference + contributions + links + state + outbox
-> visible to read/search/embedding
-> unreferenced object cleanup after retention decision
```

업로드 실패나 DB rollback은 기존 공개 참조를 유지한다. I8은 `rebuild_concept_page`를 호출하거나 page body/reference를 publish/rebuild하는 **모든** 경로를 하나의 immutable publication writer 뒤로 옮긴다. 여기에는 웹 수동/업로드·변환 후 자동/MCP ingest, 수동 편집, lint, post-ingest promotion, `_rebuild_invalidated_pages`, `_prune_dangling_references`, contribution replay/rebuild, restore가 포함된다. 새 경로뿐 아니라 legacy journal recovery와 옛 restore/replay가 stable object key를 다시 쓰거나 DB reference transaction을 우회하는 것도 금지한다. 기존 고정 객체는 읽기 호환만 유지하고 어떤 writer/recovery도 덮어쓰지 않는다. I7의 identity propagation과 I8의 publication propagation은 별도 gate다.

## 결정 12: 위키 ingest 검증과 수명주기

### I9 command DTO

I9 mutation endpoint는 `X-Agent-Service-Token`과 `mcp.wiki-draft.v1`을 쓴다. 모든 object는 unknown field를 거절한다. 새 state를 만드는 `start_ingest`, `submit_window`, `finalize_ingest`, `submit_lint_changes` command는 `actor`, `idempotency_key`, `request_hash`를 가진다. owner-scoped state read인 `resume_ingest`와 `get_window`는 exact actor와 handle/ordinal만 받는다. request hash/idempotency canonicalization은 결정 8과 같아서 새 retry의 `actor.invocation_id`만 제외하고 actor identity와 command content를 모두 포함한다. evidence가 있는 command는 document가 검증·치환한 internal `{snapshot_id, block_id, quote}`와 server-stored snapshot mapping을 hash하며 external `{document_id, block_id, quote}`나 block ID 단독을 hash input으로 쓰지 않는다. 동일 key/same hash는 저장한 이전 response를, same key/different hash는 `IDEMPOTENCY_CONFLICT`를 반환한다. path의 run/draft/window도 hash와 idempotency scope에 포함한다.

**MCP 의미 병합 계약 추가 (`mcp-tools.v2`)**: 클라이언트 LLM이 `search_wiki` → `get_wiki_page`로 의미상 동일한 기존 개념을 판단하는 것을 ingest의 기본 절차로 한다. `get_window` 응답에 코드가 정한 `concept_resolution: {mode: "CLIENT_LLM_SEMANTIC", instructions: string}`를 추가하고, contribution에 optional nullable `merge_target_page_id`를 추가한다. 선택한 대상은 동일 workspace의 active concept이어야 하며 제목/slug를 클라이언트가 변경할 수 없다. 서버는 명시적 대상을 이름 매칭보다 우선하고 기존 제목을 유지하며, 같은 페이지로 향하는 contribution을 합친다. 관련만 있는 개념은 분리하고 동등한 기존 개념이 없으면 필드를 생략한다. 새 개념끼리는 window 전반에 동일한 concept_name을 사용한다. 검색 실패는 후보 없음이 아니다. 서버 생성형 LLM은 호출하지 않는다. (2026-11-09 변경) 서버는 명시적 대상보다 이름 일치와 Jev 개념 판정을 먼저 적용하고, Jev 판정은 finalize에서 한 번만 내려 초안에 고정한다. 클라이언트 대상은 둘 다 판정하지 않은 개념에만 쓴다.

내부 `mcp.wiki-draft.v1`은 기존 요청과 frozen draft를 유지하는 호환 확장이다. null은 생략으로 정규화하고 non-null 대상은 normalized contribution, request/accepted-submission/finalize/draft hash에 포함한다. 대상 부재/삭제/잘못된 scope/type 또는 동일 개념의 상충한 명시적 대상은 `MERGE_TARGET_CHANGED`로 거절하며, 승인 뒤 apply 재계산 실패는 `PUBLIC_STATE_CHANGED`다. document-svc `mcp-tools.v2`의 정적 tool description, contribution schema, 응답 projection과 canonical hash normalization이 이 계약을 구현한다. 두 서비스를 함께 배포해야 하며, ingest 도구의 staged/public release gate는 별도다. 다음 v1 shape는 legacy 호환 기준이며 v2에서는 위 필드가 추가된다.

외부 `mcp-tools.v1` input은 server fields(`schema_version`, `actor`, `request_hash`)를 받지 않는 다음 exact mapping이다. document가 idempotency key를 보존하고 actor/request hash를 만든다.

| tool | exact external input |
|---|---|
| `start_ingest` | `{idempotency_key, document_id}` |
| `resume_ingest` | `{run_id}` |
| `get_window` | `{run_id, window_ordinal}` |
| `submit_window` | `{idempotency_key, run_id, window_id, snapshot_id, window_hash, contributions}`; contribution의 evidence는 external exact `{document_id, block_id, quote}` |
| `finalize_ingest` | `{idempotency_key, run_id, snapshot_id, expected_submitted_window_count}` |
| `submit_lint_changes` | `{idempotency_key, target_snapshot, source_documents, operations}`; `source_documents` item은 exact `{document_id, current_version, content_hash}`, statement evidence는 external에서 `{document_id, block_id, quote}` |

document는 lint source document의 current version/hash/read 권한을 다시 확인해 server snapshot을 만들고, evidence의 document ID를 internal `snapshot_id`로 치환한다. mismatch는 `SOURCE_CHANGED`다. client가 actor, workspace, page slug, snapshot mapping 또는 evidence 안의 internal snapshot ID를 tool input에 넣을 수 없다. ingest의 top-level `snapshot_id`는 `start_ingest`/`get_window`가 반환한 owner-scoped state handle을 그대로 echo하는 경우만 허용하고 client가 새로 만들거나 매핑으로 쓰지 않는다.

`start_ingest` — `POST /internal/mcp/wiki/ingest-runs`

```json
{
  "schema_version": "mcp.wiki-draft.v1",
  "actor": { "invocation_id": "<uuid>", "grant_id": "<uuid>", "user_id": "<id>", "workspace_id": "<id>", "client_id": "<url>", "policy_generation": 1, "origin": "MCP_CLIENT", "spec_version": "2025-11-25", "tool_name": "start_ingest" },
  "idempotency_key": "<bounded string>",
  "request_hash": "sha256:<64 lowercase hex>",
  "document_id": "doc_<existing id>"
}
```

```json
{
  "schema_version": "mcp.wiki-draft.v1",
  "run_id": "<uuid>",
  "draft_id": "<uuid>",
  "state": "ACTIVE_WINDOWING",
  "source_snapshot": {
    "snapshot_id": "<uuid>",
    "document_id": "doc_<existing id>",
    "source_version": 1,
    "source_content_hash": "sha256:<64 lowercase hex>",
    "block_count": 1
  },
  "next_window_ordinal": 1
}
```

document가 `wiki.ingest`와 document read를 현재 판정으로 확인한 뒤 호출한다. ai가 document callback에서 source version/hash/ordered blocks를 한 번 읽어 snapshot을 만든다. client가 source bytes/version을 공급하거나 run 중 교체할 수 없다. document별 공통 active-ingest lock이 이미 있으면 `ACTIVE_RUN_EXISTS`다.

`resume_ingest` — `POST /internal/mcp/wiki/ingest-runs/{run_id}/resume` body는 exact `{schema_version, actor}`다. 저장된 owner/grant/client/snapshot을 다시 확인하고 다음 response를 반환한다. terminal/expired/purged run은 되살리지 않는다.

```json
{
  "schema_version": "mcp.wiki-draft.v1",
  "run_id": "<uuid>",
  "draft_id": "<uuid>",
  "state": "ACTIVE_WINDOWING",
  "snapshot_id": "<uuid>",
  "next_window_ordinal": 1,
  "submitted_window_count": 0
}
```

`get_window` — `POST /internal/mcp/wiki/ingest-runs/{run_id}/windows/read`, exact body `{schema_version, actor, window_ordinal}`. ordinal은 1 이상의 integer이며 server가 정한 다음 ordinal만 읽는다.

```json
{
  "schema_version": "mcp.wiki-draft.v1",
  "run_id": "<uuid>",
  "draft_id": "<uuid>",
  "snapshot_id": "<uuid>",
  "window_id": "<uuid>",
  "window_ordinal": 1,
  "window_hash": "sha256:<64 lowercase hex>",
  "blocks": [{ "block_id": "B0001", "ordinal": 1, "text": "<bounded untrusted source text>", "content_hash": "sha256:<64 lowercase hex>" }],
  "has_more": false,
  "data_boundary": "UNTRUSTED_WORKSPACE_CONTENT"
}
```

window hash는 ordered `{snapshot_id, window_ordinal, blocks[{block_id, ordinal, content_hash}]}` canonical body의 hash다. block ID/ordinal은 snapshot에서 unique하며 text는 hash와 일치해야 한다. window block/text 상한은 I9 gate에서 정하고 초과 source를 일부 승인하지 않는다.

`submit_window` — `POST /internal/mcp/wiki/ingest-runs/{run_id}/windows/{window_id}/submissions`

```json
{
  "schema_version": "mcp.wiki-draft.v1",
  "actor": { "invocation_id": "<uuid>", "grant_id": "<uuid>", "user_id": "<id>", "workspace_id": "<id>", "client_id": "<url>", "policy_generation": 1, "origin": "MCP_CLIENT", "spec_version": "2025-11-25", "tool_name": "submit_window" },
  "idempotency_key": "<bounded string>",
  "request_hash": "sha256:<64 lowercase hex>",
  "snapshot_id": "<uuid>",
  "window_hash": "sha256:<64 lowercase hex>",
  "contributions": [{
    "concept_name": "<nonblank bounded text>",
    "statements": [{
      "statement_id": "statement-1",
      "text": "<bounded text>",
      "evidence": [{ "snapshot_id": "<uuid>", "block_id": "B0001", "quote": "<exact substring>" }]
    }],
    "links": [{ "source_statement_id": "statement-1", "target_concept_name": "<nonblank bounded text>", "relation": "concept_related_to" }]
  }]
}
```

contribution/concept/statement/evidence/link objects는 모두 exact shape다. document는 external evidence의 `document_id`가 run의 server-stored source snapshot mapping과 정확히 같은지 검증하고 internal `{snapshot_id, block_id, quote}`로 치환한다. client가 evidence 안에 `snapshot_id`를 보내거나 다른 document ID를 쓰면 거절한다. 목록과 text는 I9 gate의 양수 상한을 지키고 ID/name은 request 안에서 unique하다. 각 statement는 하나 이상의 evidence를 가지며 composite locator의 block은 이 window와 snapshot에 속하고 quote는 block text의 nonblank exact substring이어야 한다. link relation은 v1에서 `concept_related_to` 하나뿐이고 source statement가 같은 contribution에 있어야 한다. URL/image, instruction pattern, 자유 Markdown, client-selected slug는 거절한다. 개념의 `merge_target_page_id`만 기존 active workspace concept을 선택하는 용도로 허용한다. `accepted_submission_hash`는 ordered internal composite locator와 run의 immutable snapshot mapping을 포함한 normalized submission hash다. response는 `{schema_version, run_id, draft_id, window_id, accepted_submission_hash, state, next_window_ordinal}` exact object다.

`finalize_ingest` — `POST /internal/mcp/wiki/ingest-runs/{run_id}/finalize`, command fields는 `{schema_version, actor, idempotency_key, request_hash, snapshot_id, expected_submitted_window_count}`다. count는 1 이상의 integer이고 모든 window가 순서대로 exactly once 제출돼야 한다. finalize는 공개하지 않고 아래 full preview를 만들며 response는 `{schema_version, run_id, draft_id, draft_version, draft_hash, public_state_hash, state:"AWAITING_WEB_APPROVAL"}`다. concurrent finalize는 domain key `(run_id, draft_version)`으로 한 결과만 만든다.

`submit_lint_changes` — `POST /internal/mcp/wiki/lint-drafts`

```json
{
  "schema_version": "mcp.wiki-draft.v1",
  "actor": { "invocation_id": "<uuid>", "grant_id": "<uuid>", "user_id": "<id>", "workspace_id": "<id>", "client_id": "<url>", "policy_generation": 1, "origin": "MCP_CLIENT", "spec_version": "2025-11-25", "tool_name": "submit_lint_changes" },
  "idempotency_key": "<bounded string>",
  "request_hash": "sha256:<64 lowercase hex>",
  "target_snapshot": [{ "page_id": "<existing page id>", "version": 1, "content_hash": "sha256:<64 lowercase hex>" }],
  "source_snapshots": [{ "snapshot_id": "<server-created uuid>", "document_id": "doc_<id>", "source_version": 1, "source_content_hash": "sha256:<64 lowercase hex>" }],
  "operations": [{
    "operation_id": "lint-1",
    "kind": "REMOVE_LINK",
    "target_page_id": "<existing page id>",
    "arguments": { "source_ref": "<existing ref>", "target_ref": "<existing ref>", "relation": "concept_related_to" },
    "depends_on": []
  }]
}
```

document가 `wiki.lint`와 source document read를 확인한다. `source_snapshots`는 document/ai가 현재 원문에서 server-side로 고정해 actor에게 허용한 집합이며 client가 임의 snapshot ID/version/hash를 만들 수 없다. target/source snapshot은 nonempty/unique이고 operation은 1~I9 gate 상한, unique ID, 앞선 dependency만 허용한다. `kind`와 exact arguments는 다음 닫힌 v1 목록이다.

| kind | exact arguments | quote 규칙 |
|---|---|---|
| `UPSERT_STATEMENT` | `{statement_id|null, text, evidence[{snapshot_id, block_id, quote}]}` | 새/변경 statement마다 허용 source snapshot의 exact substring quote 필수 |
| `REMOVE_STATEMENT` | `{statement_id, expected_text_hash}` | 새 문장이 아니므로 quote 없음; target/hash와 surviving references 정합성 검사 |
| `MERGE_CONCEPTS` | `{source_page_ids, destination_page_id}` | structure-only; quote 없음; name identity, references, contributions, 삭제/부활 정합성 검사 |
| `ADD_LINK` | `{source_ref, target_ref, relation}` | structure-only; quote 없음; 양 끝 존재/owner/relation/중복 검사 |
| `REMOVE_LINK` | `{source_ref, target_ref, relation}` | structure-only; quote 없음; exact existing link와 orphan 영향 검사 |
| `DELETE_ORPHAN_PAGE` | `{page_id, expected_content_hash}` | structure-only; quote 없음; surviving contribution/evidence/inbound reference가 없음 확인 |

`relation`은 I7/I8 common writer의 existing versioned relation enum만 받고 arbitrary text를 받지 않는다. `UPSERT_STATEMENT` 이외 kind에 statement/evidence/quote를 넣거나 structure-only change에 문장 quote를 강제하지 않는다. 모든 kind는 target page version/hash 집합, 전체 cascade와 I7 identity/slug 규칙을 검증한다. response는 ingest finalize와 같은 `{schema_version, draft_id, draft_version, draft_hash, public_state_hash, state:"AWAITING_WEB_APPROVAL"}`다.

### Full preview, approval, apply, result DTO

`GET /internal/mcp/wiki/drafts/{draft_id}/preview?draft_version=...`는 service-authenticated document 조회이며 다음 internal exact response를 준다. frontend에는 document가 current user/workspace/policy와 source mapping을 확인한 뒤 아래에 정의한 safe external projection을 제공한다.

```json
{
  "schema_version": "mcp.wiki-preview.v1",
  "draft_id": "<uuid>",
  "draft_kind": "INGEST",
  "draft_version": 1,
  "draft_hash": "sha256:<64 lowercase hex>",
  "source_snapshots": [{ "snapshot_id": "<uuid>", "document_id": "doc_<id>", "source_version": 1, "source_content_hash": "sha256:<64 lowercase hex>" }],
  "target_pages": [{ "page_id": "<id>", "version": 1, "content_hash": "sha256:<64 lowercase hex>" }],
  "public_state_hash": "sha256:<64 lowercase hex>",
  "changes": [{
    "change_id": "<stable id>",
    "kind": "CONCEPT_STATEMENT",
    "action": "ADD",
    "page_id": "<id>",
    "subject_id": "statement-1",
    "before_text": null,
    "after_text": "<bounded text>",
    "evidence": [{ "snapshot_id": "<uuid>", "block_id": "B0001", "quote": "<exact source substring>" }]
  }],
  "complete": true
}
```

`draft_kind`은 `INGEST | LINT`; change `kind`는 `SOURCE_PAGE | CONCEPT_STATEMENT | EVIDENCE_INVALIDATION | PAGE_DELETE | ENTRY | LINK | REFERENCE | REVIVAL_LINK`, action은 `ADD | UPDATE | REMOVE | DELETE | REVIVE`다. before/after는 해당 action에 따라 하나가 null일 수 있고 internal evidence는 `{snapshot_id, block_id, quote}` exact object이며 empty 가능하나 `CONCEPT_STATEMENT`의 새/변경 문장에는 필수다. document는 preview `source_snapshots`을 서버 상태와 대조한 뒤 frontend projection에서만 evidence를 `{document_id, block_id, quote}`로 바꾸고 internal snapshot ID는 노출하지 않는다. changes는 stable `change_id`로 정렬한 전체 cascade이며 `complete=false` response는 승인할 수 없다. preview 상한을 넘으면 `PREVIEW_TOO_LARGE`; 잘린 preview를 반환하지 않는다. `draft_hash`는 kind, source/target snapshots, normalized submissions/operations의 internal composite locators, ordered complete changes의 internal composite locators/quotes와 `public_state_hash`의 canonical hash다. external projection은 hash input이 아니다.

브라우저 → document explicit approval은 unknown field 없는 `{draft_id, draft_version, draft_hash, public_state_hash}`다. 부분 선택은 없고 document가 `approval_id`와 price snapshot/full reservation을 만든다. document → ai apply command는 다음 exact body다.

```json
{
  "schema_version": "mcp.wiki-apply.v1",
  "actor": { "invocation_id": "<uuid>", "grant_id": "<uuid>", "user_id": "<id>", "workspace_id": "<id>", "client_id": "<url>", "policy_generation": 1, "origin": "MCP_CLIENT", "spec_version": "2025-11-25", "tool_name": "apply_wiki_draft" },
  "approval_id": "<uuid>",
  "operation_id": "<stable document-owned id>",
  "draft_id": "<uuid>",
  "draft_version": 1,
  "draft_hash": "sha256:<64 lowercase hex>",
  "public_state_hash": "sha256:<64 lowercase hex>",
  "approval_mode": "EXPLICIT_WEB"
}
```

`mcp.wiki-apply.v1` canonical body는 결정 8의 serializer로 직렬화한 exact `{schema_version, actor:{grant_id,user_id,workspace_id,client_id,policy_generation,origin,spec_version,tool_name}, approval_id, operation_id, draft_id, draft_version, draft_hash, public_state_hash, approval_mode}`이고 SHA-256 `apply_request_hash`로 저장한다. actor의 여덟 field는 모두 server-resolved immutable identity/provenance이며 `origin=MCP_CLIENT`, `spec_version=2025-11-25`, `tool_name=apply_wiki_draft`, `approval_mode=EXPLICIT_WEB`이어야 한다. canonical body에서 제외하는 값은 `actor.invocation_id`과 JSON body가 아닌 transport metadata인 HTTP header(인증/service token 포함), trace/span ID, enqueue/delivery/received time, delivery attempt number뿐이다. exact DTO의 다른 body field를 암묵적으로 제외하지 않고 unknown field는 거절한다.

첫 수락 시 `approval_id`와 `operation_id`를 각각 상대 ID·`apply_request_hash`에 원자적으로 고정한다. 같은 approval/operation과 같은 canonical body의 retry는 새 invocation/trace/time/attempt/header와 무관하게 저장된 apply/result를 반환하며 재공개·재청구하지 않는다. 같은 approval/operation에서 포함 field 하나라도 다르거나, 같은 approval ID를 다른 operation에 또는 같은 operation ID를 다른 approval에 재사용하면 `IDEMPOTENCY_CONFLICT`다. approval 후 draft는 수정하지 않는다. apply는 draft에 저장된 source snapshot ID·document mapping·version·content hash·composite block locator와 전체 public state를 common locks/transaction 안에서 다시 계산한다. 불일치는 각각 `SOURCE_CHANGED`/`PUBLIC_STATE_CHANGED`; 부분 공개는 없다.

`GET /internal/mcp/wiki/publication-results/{approval_id}/{operation_id}`는 service-authenticated, owner-scoped, body-free reconciliation 조회다.

```json
{
  "schema_version": "mcp.wiki-result.v1",
  "approval_id": "<uuid>",
  "operation_id": "<stable id>",
  "draft_id": "<uuid>",
  "state": "PUBLISHED",
  "changed": true,
  "event_id": "<uuid>",
  "published_reference_hash": "sha256:<64 lowercase hex>",
  "committed_at": "<time>",
  "error_code": null
}
```

state는 `APPLYING | PUBLISHED | NO_CHANGE | CONFLICTED | HALTED | UNKNOWN | NOT_FOUND`; `PUBLISHED/NO_CHANGE`만 immutable event/result terminal이고 `NO_CHANGE`는 `changed=false`다. `UNKNOWN`은 실패로 덮거나 무료 처리하지 않고 document settlement를 `PENDING_RECONCILIATION`에 둔다. `NOT_FOUND`도 진행 중/지연 delivery 가능성이 사라졌다는 별도 fence evidence 없이는 미실행 증거가 아니다.

- `submit_window`는 자유 Markdown이 아니라 concept, statement, window 안 block refs, 각 statement의 원문 substring quote, links의 구조화 JSON만 받는다.
- 외부 link/image와 instruction pattern을 거절하고 sanitizer를 적용한다. 어휘 겹침과 instruction pattern의 값은 평가셋 gate다. 사실성 보장으로 표현하지 않는다.
- 전체 변경 preview에는 source before/after, concept별 문장, 무효화 근거, 삭제 페이지, 항목·링크·참조의 연쇄 변경과 부활 링크가 포함된다. 일부만 보여 주고 승인받지 않는다.
- 이름 동일성은 title/true alias의 `name_key`로 결정한다. slug와 slug-derived/unknown alias는 병합 근거가 아니다. 삭제 page 부활, C/C#/C++ 충돌, 후보 소진 규칙은 웹/MCP 공통 writer에서 처리한다.
- 문서당 수동/자동/MCP 활성 ingest run은 하나다. workspace concept lock은 별도로 유지한다.

### Service-owned lifecycle matrix

TTL expiry와 user/workspace purge는 같은 동작이 아니다. TTL worker도 owner/fence/reference를 확인하며, purge는 먼저 durable fence/deny를 commit한 뒤 service별 정리와 대사를 수행한다. outbox/inbox delivery는 at-least-once이고 unique domain key로 dedupe한다. retry limit에 도달한 message/result는 버리지 않고 `RETRY_EXHAUSTED`/`PENDING_RECONCILIATION`으로 operator-visible queue에 남긴다. backoff, retry count, deadline, retention 숫자는 모두 launch gate다.

| owner | record/object families | purge ordering and fence | TTL, retry, reconciliation | body-free surviving evidence | frontend lifecycle projection |
|---|---|---|---|---|---|
| access | grant, workspace policy generation, authorization code, refresh family/token, consent/client snapshot, connection audit, invalidation outbox | user/workspace purge 또는 security/admin revoke를 grant/policy에 먼저 commit해 새 token/tool/approval을 deny한다. code를 consume/revoke하고 refresh family를 revoke한 뒤 credential/consent를 정책대로 삭제한다. delayed refresh/code/invalidation은 fence를 확인해 grant/data를 만들지 않는다. | authorization-code/access/refresh expiry는 해당 credential cleanup이며 grant expiry state를 만들지 않는다. invalidation outbox는 document/ai ack 또는 retry exhaustion까지 유지하고 exhaustion은 admin reconciliation 대상이다. | audit ID/time/user/workspace/client/grant/action/result/error와 purge/revocation fence만 허용 범위/기간 안에서 남길 수 있다. code/token/CIMD response/body는 남기지 않는다. | connection states are `ACTIVE`, `USER_DISCONNECTED`, `ADMIN_REVOKED`, `SECURITY_REVOKED`, `PURGED`; retry failure는 연결을 active로 되돌리지 않는다. |
| document | MCP session/rate-limit state, approval and immutable price snapshot, full reservation, execution attempt/receipt, platform-fee ledger/adjustment, publication inbox, settlement/reconciliation job, audit, outbox | purge fence와 current-authz deny를 먼저 commit하고 pending approval/enqueue를 cancel한다. receipt/event를 조회해 확정 성공분을 settle하고 확정 미사용분만 release한다. unknown은 purge만으로 무료/해제로 만들지 않는다. 그 뒤 preview/artifact/body projection과 허용되지 않은 actor data를 삭제한다. delayed approval/event/outbox handler는 fence와 cancelled approval을 확인한다. | session/cursor/approval/idempotency TTL은 referenced receipt/reservation/reconciliation을 삭제하지 않는다. inbox/outbox는 event/domain key로 dedupe하고 retry exhaustion이나 missing AI result는 periodic read-only result reconciliation으로 보낸다. `releaseStale`/model usage collector는 MCP reservation을 건드리지 않는다. | 법/정산 정책이 허용한 최소 fee ledger, adjustment, receipt/event dedupe key, amount/price version, audit/purge fence만 body 없이 남길 수 있다. query/Markdown/diff/quote/artifact는 금지한다. 보존 불가/기한 만료 unknown 처리 정책은 미결 gate다. | approval states are `AWAITING`, `INVALIDATED`, `REJECTED`, `EXECUTING`; settlement states are `RESERVED`, `PENDING_RECONCILIATION`, `SETTLED`, `RELEASED`, `ADJUSTED`. run terminal과 합치지 않는다. |
| ai | MCP proposal/run/operation, ingest/lint run/draft/window, source snapshot/block body, artifact/object, task journal, idempotency, immutable unpublished object, publication outbox/result/inbox, `ai_purged_scopes` | `ai_purged_scopes` fence를 먼저 commit하고 create/resume/submit/finalize/apply/replay를 deny한다. active jobs를 halt/cancel하고 committed publication/result를 document와 대사한 뒤 draft/snapshot/artifact/unpublished object를 삭제한다. delayed job/event와 retry는 fence를 확인해 row/object/page/fee event를 재생성하지 않는다. | inactive draft/window/artifact/idempotency는 결정된 TTL 뒤 cleanup하되 active/current/recovery reference 및 unresolved publication/settlement evidence는 보존한다. publication outbox는 ack 또는 retry exhaustion까지 durable하며 exhaustion도 result lookup 가능한 `UNKNOWN`으로 남긴다. object cleanup은 current/in-flight/recovery reference를 지우지 않는다. | event ID, approval/operation/draft ID, actor IDs, outcome/changed, published reference hash, time, purge fence 등 최소 dedupe/reconciliation evidence만 허용 정책 안에서 body 없이 남긴다. source block/statement/quote/Markdown은 남기지 않는다. | draft result states are `APPLYING`, `PUBLISHED`, `NO_CHANGE`, `CONFLICTED`, `HALTED`, `UNKNOWN`; `UNKNOWN`은 실패/무료가 아니다. |
| frontend | durable truth 없음; backend connection/approval/run/draft/settlement projection과 local ephemeral UI state만 사용 | purge/revoke response를 받으면 local cached preview/content를 버리고 재연결로 옛 handle을 되살리지 않는다. frontend cache 삭제가 backend purge 증거는 아니다. | retry는 safe GET/status/result 조회만 한다. mutation/approval을 새 ID로 자동 재제출하지 않으며 backend `RETRY_EXHAUSTED`/pending 상태를 숨기지 않는다. | browser storage에 body-free evidence를 장기 보존하지 않는다. 필요한 audit/fee evidence는 owning backend에서만 조회한다. | `invalidated`, `expired`, `halted`, operation `UNKNOWN`, settlement `PENDING_RECONCILIATION`, purge/retry pending을 별도 표시하고 terminal execution/settlement을 합치지 않는다. |

파기 완료는 각 owner의 fence, body/object 삭제, outbox/inbox 상태, unresolved reservation/result 수와 허용된 body-free evidence를 service receipt로 보고할 때만 표시한다. frontend는 하나라도 pending이면 “파기 진행 중/정산 대사 중”으로 유지한다. 숫자 retention/TTL/retry/reconciliation deadline과 body-free evidence가 허용되지 않을 때 unknown fee를 어떻게 처리할지는 출시 전 명시적으로 결정한다.

I7의 concept identity 변경은 resolver 한 파일로 끝나지 않는다. `name_key`와 alias provenance를 하나의 공통 판정으로 만든 뒤 다음 전체 경계에 전파한다.

- normalization: `wiki_generation/domain/text_utils.py`, `wiki_generation/infrastructure/normalize.py`
- resolution/ledger: `jev_concept_resolver.py`의 `NameMatchConceptResolver`·`_exact_name_match`, `infrastructure/concept_resolution.py`의 `merge_concept_ledger`
- promotion: `infrastructure/post_ingest_cluster.py`의 `_core_concept_promotions`
- allocation/persistence: `concept_entry_ingest.py`, `postgres_wiki_output_persistence.py`, `postgres_wiki_ingestion_repository.py`, `postgres_wiki_writer.py`, `workspace_concept_lock.py`
- replay consumers: `_prune_dangling_references`, `_rebuild_invalidated_pages`, lint, manual edit, re-ingest, restore와 `rebuild_concept_page` 호출부

과거에 이미 합쳐진 데이터를 자동 분리하지 않는다. I8은 이 공통 identity/locked slug allocation 결과만 소비한다.

## frontend 계약

frontend는 다음을 기존 UI 패턴에 연결한다.

- access 동의 화면: workspace, exact redirect host, scopes, allowlisted client 표시 이름, 앱 진위 미검증 경고, CSRF/clickjacking 방어.
- 연결 관리: client/workspace/scopes/created/last-used/status, 사용자 연결 해제, 관리자 기본-off 정책/강제 회수. 연결 해제가 이미 승인한 실행 취소가 아님을 표시하고 별도 취소/복구로 연결한다.
- 승인 화면: `origin=MCP_CLIENT`, server-resolved client, grant, plan/draft version+hash, 선택 작업, 단가·상한·price version·최대액, 부분 성공 과금·복구 무환불을 표시한다. revise/자동 승인 버튼은 없다.
- 문서 preview는 기존 diff·`skipHtml`·`inert-external`을 재사용한다. ingest/lint 제목·인용도 text로 렌더하고 외부 image 요청이 없어야 한다.
- 상태 mapping에 `invalidated`, `halted`, operation `UNKNOWN`, settlement `PENDING_RECONCILIATION`, draft 상태를 추가한다. `halted`는 취소/복구 중이 아니며 “관리자 또는 보안 정책으로 중단됨, 자동 복구되지 않음”으로 표시한다.
- 완료/부분 실패/취소/halted와 정산 상태를 합치지 않는다. 성공 여부가 미확정이면 청구액을 확정 표시하지 않는다.

## 계약 테스트 inventory

테스트 이름은 구현 시 저장소 관례에 맞추되 아래 행을 삭제하거나 정적 검토로 대체할 수 없다.

| 단계 | 필수 검사 |
|---|---|
| I1 access | metadata 익명 조회와 데이터 401; S256 only와 `token_endpoint_auth_method=none`; exact CIMD `client_id`가 authorization/token/code에서 literal `none` 또는 다른 ID로 대체되지 않음; authorization code 만료/재사용/동시 교환; verifier/client/redirect/actual loopback port/resource 바꿔치기; user/workspace/scope 불변; allowlist 전 CIMD network 0회; SSRF/redirect/size/time; public client refresh 실제 동작; 회전 동시성·응답 유실·reuse family revoke·오류 뒤 revoke commit; 임의 invalid token의 다른 grant 무회수; credential expiry가 grant expiry를 만들지 않음; policy off→on generation; 원본 membership/permission과 access 장애 fail-closed |
| I2 document/ai read | 네 read DTO의 unknown field/type/identifier/version/hash/evidence/truncation 검증; `get_wiki_page`의 page-id-only input, exact seven-field `mcp.wiki-page.v1` response, internal raw-body hash 검증 뒤 sanitizer 적용과 sanitized public-body hash, empty body, no cursor/chunk/evidence/quote/version 및 oversize 거절; search의 stored `{document_id,block_id}` evidence, current workspace/read/provenance 및 search page version/content hash 검증; draft snapshot 없이 historical 두 document가 모두 `B0001`을 가질 때 별도 search evidence로 남고 dedupe/cursor hash가 충돌하지 않으며 안전한 document locator로 반환됨; stale/cross workspace/user/grant search·document cursor와 page version 변경 거절; 양방향 audience 거절; bad signature/issuer/expiry/scope; every ID/cursor/session cross-owner; POST와 지원 GET/DELETE session ownership; HTTPS-only와 allowed/missing/malformed/mismatched `Origin`; decompressed body 4 MiB; no token passthrough; `doc_`/`chatdoc_`와 길이; static `mcp-tools.v1` schema에 user/workspace text·Skill 없음; no generative LLM/evaluator/summarizer; missing stored vector 본문 embedding 0회; candidate count/text/time/response bounds, 필수 field/result의 silent truncation 없음, 계약상 bounded search snippet excerpt 적용 |
| I3 launch/UI | 실제 Claude client별 CIMD/redirect/discovery/refresh/reconnect/step-up; 관리자 default off; 연결 해제 즉시 새 read 차단; stale authz cache가 남아도 탈퇴 읽기 차단; access 장애 read 차단; consent CSRF/clickjacking/identity warning; HTTPS/proxy canonical URL; tool catalog version/change review와 write-before-launch/팀 Skill 미노출; audit exact user/workspace/client/grant/tool/target/result/origin fields, body/token/query/snippet 미기록; write author+MCP origin |
| I4 proposal | exact operation/artifact/target/result-reference/tool-argument schemas, bounds, unknown field 거절; planner/evaluator 미호출; hierarchy/artifact/hash/purpose/folder scope 위조 거절; artifact UTF-8 hash와 deterministic normalized request/operation hash golden vectors; external raw instruction의 exact `proposal.instruction`·trimmed summary/reason mapping; `instruction-v1`/new-invocation/`instruction-v2` golden hash 재현과 같은 key의 raw instruction-only 변경 conflict; retry의 새 invocation/trace/time은 same hash, actor/content 변경은 conflict; obvious no-op 하나가 offending operation과 함께 전체 submission을 거절하고 run/artifact 없음; duplicate warning은 no-op과 구분; origin 위조 거절; auto approval setting/plan-ready/internal auto API로 실행 불가; explicit web approval만 가능; MCP revise 불가; 기존 web planner 회귀 |
| I5 approval/execution/fees | approval canonical body/operation hash/price snapshot 고정과 selected operation plan-order normalization; full reserve or 402; duplicate/lost/late/cancelled approval; current permission at approval and every operation; user disconnect post-approval continues; admin/security halt next boundary; timeout/5xx/409/late commit receipt reconciliation; proposal에서만 확정 가능한 no-op은 I4 거절, execution-only `NO_CHANGE`는 free; duplicate receipt/charge once; cap; partial success; cancel/rollback no refund; releaseStale/model usage isolation; no web polling dependency |
| I6 plan | 1~20 operations; partial approval dependency; partial failure billing; retry budget exhaustion leaves unstarted skipped and attempted unknown; no clarification/server replan; step limit treated as failure; uncertain create duplicate candidate holds only matching operation; unrelated proposal allowed |
| I7 concept | C/C#/C++ same document/order/concurrency/reingest/promotion; title/true alias vs slug-derived/unknown alias; deleted same-concept revival vs other/ambiguous deleted pages; manual links; candidate exhaustion explicit fail; evidence/link mapping; preview slug change requires reapproval; legacy read compatibility |
| I8 publication | manual/auto/MCP ingest, manual edit, lint, post-ingest promotion, invalidation rebuild, prune, contribution replay/rebuild, restore가 common immutable writer 사용; legacy journal/restore/replay stable-key overwrite와 bypass 불가; upload failure, DB rollback, process death, response loss; old reference preserved; body reference+ledger+links+state+outbox atomic visibility; unpublished not read/search/embed; no overwrite; cleanup keeps active/in-flight/recovery objects; remote ledger/pending mismatch blocks |
| I9 ingest/lint | start/resume/get/submit/finalize/lint exact DTO, unknown field/bounds/owner/hash/idempotency; source snapshot/window/document active-run lock; submit/lint external `{document_id,block_id,quote}`가 internal `{snapshot_id,block_id,quote}`로 검증·치환되고 preview/draft hash/apply까지 같은 immutable snapshot mapping을 유지함; 두 source document가 모두 `B0001`을 가진 ingest/lint preview에서 locator·submission/draft hash가 충돌하지 않고 external projection도 document를 구분함; structured contribution/quote substring/instruction/external link; lint closed kind/argument list, statement upsert quote 필수와 structure-only merge/link/orphan quote 면제 및 type-specific consistency; complete full-cascade preview/hash와 partial-preview approval 금지; explicit full approval과 exact apply canonical body; 새 invocation/trace/time/attempt/header만 바꾼 same-body retry는 같은 result; schema version, approval ID, operation ID, draft ID/version/hash, public-state hash, approval mode, actor의 grant/user/workspace/client/policy generation/origin/spec/tool을 각각 하나씩 바꾸는 field-mutation vector는 conflict/스키마 거절이고 approval↔operation 재결합도 conflict; source/public fingerprint race; concurrent finalize; immutable result lookup and delayed/duplicate/out-of-order event; purge/expiry no resurrection; published once billed once, unpublished free, UNKNOWN pending; preview external request/script 0; cancellation/recovery |
| lifecycle access | purge/revoke fence precedes code/refresh/consent cleanup; credential TTL never creates grant expiry; invalidation outbox retry exhaustion remains visible/reconcilable; delayed credential/message cannot recreate grant; allowed body-free audit/fence only |
| lifecycle document | purge fence/cancel approval before body deletion; receipt/event reconciliation before settle/release; fee ledger/adjustment/audit/outbox/inbox/receipt included; TTL/releaseStale never deletes referenced or unknown evidence/reservation; retry exhaustion/result absence stays pending; delayed approval/event cannot restore data or fee; body-free evidence fields only |
| lifecycle ai/frontend | `ai_purged_scopes` before run/draft/snapshot/window/artifact/idempotency/object/outbox/inbox/journal cleanup; delayed job/replay/event no resurrection; unpublished/current/in-flight/recovery object cleanup safety; retry exhaustion/result UNKNOWN lookup; frontend purge/retry pending, expired/invalidated/halted/UNKNOWN/PENDING_RECONCILIATION mapping without terminal collapse |

실제 DB, object storage failure injection, remote OAuth 연결, 보안 점검은 실행 증거가 있어야만 통과로 기록한다.

## I0~I9 의존성 gate

| 단계 | 선행 | 계약 완료 기준 |
|---|---|---|
| I0 | 없음 | 이 ADR, 보존 원안, DTO/state/error/test/gate 목록 리뷰 완료 |
| I1 | I0 | access OAuth+current authorization 구현 및 I1 tests |
| I2 | I0; 인증 통합은 I1 | document MCP read와 ai search, 양방향 audience/tenant/no-LLM tests |
| I3 읽기 출시 | I1+I2 | frontend/관리자/회수/audit/limits와 실제 client 검증. write tools 미노출 |
| I4 | I0+I2 | LLM 없는 proposal와 explicit approval provenance |
| I5 문서 편집 출시 | I1+I4 | current authz, receipt, full reserve, settlement, halted, frontend와 unresolved fee/retention gates 결정 |
| I6 다단계 출시 | I5 | 부분 승인/실패/예산/unknown create 검증 |
| I7 | I0; I6와 파일 비충돌 시 병행 | concept identity/slug 공통 계약 |
| I8 | I7 | 웹/MCP 공통 불변 publication writer와 failure injection |
| I9 ingest/lint 출시 | I5+I7+I8 | snapshot/window/draft/preview/event/fees/purge E2E와 4단계 gates |

읽기 출시는 I7~I9를 기다리지 않지만 I1~I3를 줄이지 않는다. I2 구현은 I1 없이 병행할 수 있어도 authentication integration 합격은 I1 뒤다.

## 미결 launch gate

다음은 구현자가 임의 값을 넣지 않는다.

1. canonical public MCP URI, access issuer와 protected-resource/authorization metadata의 정확한 운영 URL, HTTPS termination/trusted-proxy 설정과 exact allowed `Origin` 목록.
2. Claude web/Code의 실제 CIMD URL, client metadata/credentials, 정확한 redirect URI와 step-up 지원 여부. allowlist 등록만으로 검증 완료가 아니다.
3. SAS 1.5 계열에서 `none` public client refresh 발급·회전 확장, RFC 8707 resource 결합, custom metadata의 conformance 결과와 고정 patch version.
4. access token/refresh token/authorization code TTL. “짧게”만 결정됐고 숫자는 미결이며 별도 grant TTL은 없다.
5. read 호출/page/chunk/query/snippet/evidence 한도, 후보 개수·text bytes·deadline 상한 값과 요금제 포함 여부.
6. 성공 변경 단가, approval 상한, price version, ingest 원문 분량 산정 단위·구간/산식, lint 단가.
7. proposal text/name/artifact storage 상한, ingest window/block/contribution/statement/evidence/link/lint-operation/preview 상한, draft/preview/idempotency/cursor/session TTL, user/workspace 활성 개수·저장량 상한.
8. service별 retry count/backoff/exhaustion 운영 책임, 감사·정산·receipt/event/purge-fence 최소 body-free 증빙의 허용 필드와 보존/파기 기간, unknown reservation 대사/해제·조정 기한과 보존 불가 시 정책.
9. 어휘 겹침 threshold, instruction pattern, preview 상한과 이를 정할 평가셋, I7/I8에서 고정할 versioned wiki relation enum.
10. 외부 보안 점검/침투 테스트 범위와 책임자.
11. access credential/grant, document receipt/fee/audit/outbox/inbox, ai draft/window/object/publication outbox의 물리 schema·migration 이름. 논리 DTO와 소유권은 이 ADR로 확정됐다.
12. MCP Java SDK의 고정 `2.1.x` patch와 dependency/security scan 결과.

## 검토 근거

### access

- `build.gradle:1-44`: Java 21, Spring Boot 3.5.14, Security/OAuth client는 있으나 authorization-server 의존성은 없다.
- `src/main/java/fruition/access/security/SecurityConfig.java:75-136`: 현재 단일 filter chain, OAuth login 임시 session, 전역 CSRF disable, 공개/internal 경로. authorization-server 전용 chain과 동의 CSRF가 필요하다.
- `src/main/java/fruition/shared/security/JwtTokenProvider.java:98-106,162-192,222-238`: RS256·kid·issuer·audience·expiry/sub 검증과 단일 configured audience 발급.
- `src/main/java/fruition/access/security/oauth/OAuthExchangeCodeStore.java:15-62`: 현재 Redis code는 user와 선택적 challenge만 가지며 GETDEL 1회 소비다.
- `src/main/java/fruition/access/user/service/AuthService.java:136-150`: 기존 refresh는 row revoke 후 새 pair를 내지만 token family/reuse 대응이 없다.
- `src/main/java/fruition/access/workspace/controller/InternalAuthzController.java:48-59`: 원본 `findActiveMember`와 `effectivePermissions`를 재사용할 수 있다.

### document

- `src/main/java/fruition/core/authz/WorkspaceAccessGuard.java:21-40,119-150,179-197`: 기존 300초 cache-first 판정과 access 장애 fail-closed. MCP는 cache-first를 우회해야 한다.
- `src/main/java/fruition/core/document/service/DocumentAccessPolicy.java:18-28,44-66,100-137,150-207`: 문서/폴더 edit·view와 owner/OWNER 예외를 재사용하되 현재 Access context를 주입할 경로가 필요하다.
- `src/main/java/fruition/core/agent/repository/AgentRunCommandRepository.java:40-52`: `prepareToolApply`는 projection insert라 commit receipt가 아니다.
- `src/main/java/fruition/core/agent/service/AgentToolService.java:49-70,135-171,345-404`: 일곱 mutation의 exact argument key, folder/document ID, nullable nonnegative position, positive base-version validator를 proposal v1이 재사용한다.
- `src/main/java/fruition/core/agent/service/AgentToolService.java:231-244`: `prepareToolApply`는 별도 `REQUIRES_NEW` 준비 트랜잭션이므로 실제 문서 변경과 같은 트랜잭션의 영수증으로 쓸 수 없다.
- `src/main/java/fruition/core/usage/service/CreditService.java:23-36,65-87,136-171,257-274`: 현재 부분 예약·usage 합계 정산·stale release는 MCP 수수료와 분리해야 한다.
- `src/main/java/fruition/core/agent/service/AgentTurnService.java:365-396,438-472`: 기존 자동 승인·승인·revise/status 경계에 MCP provenance 차단이 전파돼야 한다.

### ai

- `pipeline/api.py:48-66,91-130`: `X-Internal-Token`과 `X-Agent-Service-Token` 경계가 이미 분리돼 있다.
- `pipeline/app/modules/agent_run/interfaces/http/schemas.py:7-40,60-99,151-206`: 기존 actor/approval/artifact/plan DTO는 grant/client/provenance/approval ID가 없다.
- `pipeline/app/modules/agent_run/domain/plan.py:8-22,55-82,92-122,173-193`: 기존 일곱 `PlanToolName`, operation fields/dependency 규칙과 64-hex operation hash canonical field set을 proposal v1의 호환 기준으로 쓴다.
- `pipeline/app/modules/agent_run/infrastructure/chat_completions_plan_generator.py:401-557`: hierarchy, exact argument sets, artifact binding, target source-range와 operation-result reference validator를 web/MCP 공통 결정 경계로 옮긴다.
- `pipeline/app/modules/agent_run/infrastructure/postgres_agent_run_repository.py:142-230`: run/plan/tool은 workspace+user+run에 묶이지만 grant/client binding이 없다.
- `pipeline/app/modules/task_cancellation/infrastructure/postgres_task_journal.py:47-80,121-179`: cancel은 rollback으로 이어지므로 rollback 없는 `halted`를 별도로 추가해야 한다.
- `pipeline/app/modules/data_purge/infrastructure/postgres_purge.py:99-131`: 기존 agent/task/artifact purge에 MCP tables/objects가 전파돼야 한다.
- `pipeline/app/modules/query/infrastructure/stored_wiki_page_embedding_search.py:31-42`: `without_live_embedding()` 재사용 근거.
- `pipeline/app/modules/query/interfaces/http/dependencies.py:31-83`: 기존 `build_answer_query_use_case`는 답변 생성·평가기·요약기와 선택적 web search를 조립하므로 MCP read에서 재사용하지 않는다.
- `pipeline/app/modules/wiki_generation/domain/text_utils.py:60`, `wiki_generation/infrastructure/jev_concept_resolver.py:100-425`, `wiki_generation/infrastructure/normalize.py:386-394`, `wiki_generation/infrastructure/concept_resolution.py:161-217`, `wiki_generation/infrastructure/post_ingest_cluster.py:83-108`: I7 identity가 normalization/resolver/ledger/promotion에 걸친다는 근거.
- `pipeline/app/modules/wiki_ingestion/infrastructure/postgres_wiki_output_persistence.py:280-419`, `postgres_wiki_ingestion_repository.py:307-404,2465-2469,3109-3423,3552-3567,3627-3638`: identity와 replay/prune/persistence가 같은 변경 범위라는 근거.

### frontend

- `web/src/features/agent-chat/lib/agentPlan.ts:1-49,143-175,201-227,262-264`: 현재 plan DTO·상태/승인 가능 조건과 실패 표시.
- `web/src/features/agent-chat/api/agentPlan.ts:39-66`: 승인에 plan version/hash/선택 operation을 이미 고정한다.
- `web/src/shared/ui/markdown/MarkdownViewer.tsx:36-55,114-118,224-235`: 외부 link inert 처리와 `skipHtml` 재사용 근거.
- `web/src/features/agent-chat/ui/MarkdownEditPreview.tsx:28-64`: 기존 text diff/rendered preview와 사람 승인 UI.
- `web/src/features/agent-chat/lib/taskThread.ts:156-168,221-254`: 현재 cancel/rollback/failed 상태 mapping에 `halted`·settlement를 추가해야 한다.

## 결과

- I1~I9는 이 DTO·enum·상태·소유권을 깨는 구현을 먼저 ADR 변경 없이 도입할 수 없다.
- access의 availability와 원본 조회 지연이 MCP 호출에 직접 영향을 준다. 실패하면 읽기도 허용하지 않는다.
- 사용자는 MCP 대화에서 제안하고 웹에서 승인한다. 이 불편은 저장형 injection과 자동 승인 위험을 줄이기 위한 의도된 경계다.
- 원문 최종 조회와 ai DB commit은 분산 트랜잭션이 아니다. 이후 편집 이벤트의 stale 표시로 수렴하며 절대 최신성을 보장하지 않는다.
- 사용자 클라이언트의 다른 connector로의 유출은 서버가 막을 수 없다. 관리자 기본-off와 고지로 남는 위험을 드러낸다.

# 0048. MCP 원본 파일 업로드는 짧은 PUT 주소 발급 → 완료 검증 → 웹 승인으로 한다

- 상태: 제안
- 범위: Fruition-document, Fruition-ai, Fruition-frontend, Fruition-flatform
- 기준 커밋: ai `879f528`, document `abb7a7f`(열린 PR #253은 catalog를 `mcp-tools.v4`로 올린다)
- 고치는 결정: [ADR 0046](0046-mcp-cross-service-contract.md) 결정 1의 닫힌 tool 목록, 결정 8의 operation 표

## 맥락

MCP 클라이언트는 워크스페이스 문서를 읽고, 편집·계획·위키 편입·PDF 변환을 제안할 수 있다. 하지만 사용자 컴퓨터에 있는 원본 파일(Markdown, txt, PDF)을 워크스페이스에 넣는 도구가 없다. 지금은 사용자가 웹에서 직접 올린 뒤에야 `start_ingest`나 `start_pdf_conversion`을 부를 수 있다. 두 도구 모두 이미 있는 `document_id`를 받는다(`McpPdfTools.java:144-147`, ADR 0046 결정 12의 `start_ingest {idempotency_key, document_id}`).

사용자가 정한 전제는 다음 세 가지다. 이 ADR은 이 범위 안에서 설계한다.

1. 올린 파일은 웹에서 명시적으로 승인한 뒤에만 워크스페이스에 반영한다. "MCP 쓰기는 모두 제안 → 웹 승인"이라는 ADR 0046 결정 1-6과 같다.
2. tool 인자로 base64나 바이트를 보내는 경로는 두지 않는다. 파일을 HTTP로 보낼 수 없는 클라이언트에는 Fruition 웹에서 올리라고 안내한다. ADR 0046의 요청 상한 4 MiB(결정 1)로는 100 MiB PDF를 어차피 담을 수 없다.
3. 서버가 짧게 유효한 업로드 주소를 주고, 클라이언트가 바이트를 PUT한 뒤, 완료 도구로 크기·hash·형식을 검증한다.

### 같은 문제를 푸는 기존 제품

- Miro MCP `image_get_upload_url`: 한 번 쓰는 `upload_url`을 주고, 인증 헤더 없이 원본 바이트를 PUT하게 한다. 설명에 curl 예시가 있고, 메모리에만 있는 바이트는 먼저 파일로 쓰라고 안내한다. 크기와 허용 형식도 설명에 적는다. 같은 서버의 `canvas_get_html_upload_url`은 inline 전송이 너무 크거나 거절될 때 쓰는 별도 경로이며, 슬롯마다 만료 시간이 있다.
- Slack `files.getUploadURLExternal` → 바이트 전송 → `files.completeUploadExternal`: 주소 발급, 전송, 완료 확정의 3단계다. 완료를 확정하기 전에는 채널에 보이지 않는다.
- Fruition 웹: PDF는 이미 같은 구조를 쓴다. 서명한 ticket과 조각별 presigned PUT(15분)으로 `tmp/document-uploads/`에 올리고, 확정할 때 다시 검증한다(`DocumentDirectUploadService.java:49,67-91,132-181`, `DirectMultipartUpload.java:57-67,89-105`).

### 웹 업로드 경로에서 확인한 사실

| 항목 | 현재 동작 | 근거 |
|---|---|---|
| 허용 형식 | 확장자로 서버가 MIME을 정한다. `.pdf`→`application/pdf`, `.md`/`.markdown`→`text/markdown`이고, `.txt`는 `.md` 이름의 편집 문서로 받는다. 클라이언트가 보낸 Content-Type은 믿지 않는다. | `DocumentService.java:344-346,916-926` |
| 내용 검사 | PDF는 앞 5바이트가 `%PDF-`인지 본다. Markdown은 UTF-8 검증을 거치고 5 MiB 이하만 받는다. | `DocumentService.java:930-934`, `DocumentDirectUploadService.java:166-171`, `DocumentService.java:893-900`, `DocumentEditingRules.java:23` |
| 크기·용량 | PDF 한 개는 기본 100 MiB(`app.upload.max-pdf-bytes`)다. 사용자 용량은 5 GiB이며, 시작할 때 잠금 없이 `precheck`하고 문서 행을 쓰는 트랜잭션에서 `enforce`한다. 용량에는 `documents`·`document_assets`·`meetings` 바이트만 들어간다. | `UploadLimitService.java:35-36,44-67,75-83` |
| 파일 이름 | 1~255자이고, 경로 구분자와 제어 문자를 막는다. 같은 폴더에 같은 이름이 있으면 번호를 붙인다. | `DocumentService.java:3321-3332,454-460` |
| 중복 | 내용 기준 중복 탐지는 없다. 같은 key와 같은 request hash로 다시 보내면 멱등 재생만 한다. | `DocumentService.java:358-379` |
| 악성코드 검사 | 웹 경로에 없다. | 업로드 경로 전체 |
| 원본 저장 | `sources/documents/{id}/original`에 둔다. 직접 업로드한 PDF는 versionId/ETag를 고정해 서버 안에서 compose copy한다. | `DocumentService.java:405-421`, `DocumentDirectUploadService.java:164-165,193-194,206-212` |
| PDF hash | 직접 업로드한 PDF의 `content_hash`는 바이트 SHA-256이 아니라 `sha256(ETag:size)`다. 서버를 거친 PDF는 파일 전체 SHA-256이다. MCP 변환은 이 `content_hash`를 `source_content_hash`로 대조한다. | `DocumentDirectUploadService.java:199-205`, `DocumentService.java:357,489-492`, `McpDocumentDataGateway.java:109-121` |
| 업로드 자동 편입 | Markdown/txt 업로드가 커밋된 직후 워크스페이스 `auto_ingest_markdown_on_upload`와 `wiki.ingest`를 보고 서버 편입을 등록한다. 결과는 `wiki_ingest`로 응답한다(PR #251). 가져오기 세션과 스킬 참고 문서는 자동 편입하지 않는다. PDF 변환본은 `auto_ingest_pdf_on_convert`를 따른다. | `DocumentService.java:495-506,1940-1964,1999-2040,305-323` |
| 임시 객체 정리 | 버킷 전체에서 미완료 multipart를 7일 뒤 중단하고, `tmp/`는 7일, `tmp/convert/`는 1일 뒤 지운다. document에는 `tmp/` delete 권한이 없다. converter는 `sources/documents/*`만 읽는다. | flatform `infra/terraform/s3.tf:75-116,168-181`, `docs/api/tasks.md:65` |
| MCP 쓰기 도구 | `write_propose` scope에서 PDF 변환 도구와 같은 `PROPOSAL_ONLY` annotation을 쓴다. 오류는 지금 `{"error":"<CODE>"}`만 돌려준다. | Fruition-document `docs/api/mcp-read.md:12-15`, `McpPdfTools.java:64-65`, `McpToolCatalog.java:117-125` |

## 결정 1: 도구 두 개와 스키마

ADR 0046의 닫힌 tool 목록에 `create_document_upload`와 `complete_document_upload`를 더한다. 두 도구 모두 document-svc가 소유하고, scope는 `write_propose`, annotation은 `PROPOSAL_ONLY`다. 승인·적용·결과 조회는 여전히 MCP tool이 아니다.

### `create_document_upload`

입력은 unknown field 없는 `{idempotency_key, file_name, size_bytes, content_hash, target_folder_id}`다.

- `file_name`: 웹과 같은 파일 이름 검사(`validateFilename`)를 거치고, 확장자로 형식을 정한다. 허용 형식은 웹과 같이 PDF·Markdown·txt뿐이다. 웹이 받는 형식이 늘면 같이 늘린다.
- `size_bytes`: 1 이상의 integer이고, 형식별 상한(PDF `max-pdf-bytes`, Markdown/txt 5 MiB) 이하여야 한다.
- `content_hash`: 원본 바이트 그대로의 `sha256:<64 lowercase hex>`다(ADR 0046 결정 3).
- `target_folder_id`: UUID 또는 null(루트)이다. 웹과 같은 `requirePlacement` 판정을 거친다.
- `mime_type`은 받지 않는다. 서버가 확장자로 정하고 PUT 헤더에 넣어 돌려준다. 클라이언트가 보낸 형식을 믿지 않는 웹 규칙과 같다.

출력은 다음과 같다.

```json
{
  "upload_id": "<uuid>",
  "upload_url": "<presigned https PUT url>",
  "method": "PUT",
  "required_headers": { "Content-Type": "application/pdf" },
  "expires_at": "<RFC 3339 UTC>",
  "web_upload_url": "<자사 HTTPS 웹 업로드 화면 URL>",
  "next_step": "<코드가 정한 고정 문구>"
}
```

사용자가 예로 든 `max_bytes`는 넣지 않는다. 정확한 크기를 미리 신고하고, 상한을 넘으면 이 도구가 바로 거절하기 때문이다.

### `complete_document_upload`

입력은 unknown field 없는 `{idempotency_key, upload_id}`다. 출력은 `{upload_id, state:"PROPOSED", run_id, status:"awaiting_approval", review_url, next_step}`다. `review_url`은 ADR 0046 결정 8과 같은 자사 승인 화면이고, PR #253의 `REVIEW_APPROVAL` 문구를 그대로 쓴다.

[결정 4](#결정-4-연쇄-작업의-승인-횟수)의 U2에서 선택 필드 `propose`(boolean, 기본 `true`)를 더한다. `false`이면 제안을 만들지 않고 `{upload_id, state:"VERIFIED", expires_at, next_step}`를 돌려준다.

### 승인 operation

ADR 0046 결정 8의 operation 표에 `create_document_from_upload`를 더한다.

| `tool_name` | target/base 규칙 | exact `arguments` |
|---|---|---|
| `create_document_from_upload` | `target_type=document`, target/base null | `upload_id`, `display_name`, `folder_id`(folder ID/null/result ref), `content_hash` |

artifact는 없다. 원본 바이트는 Agent plan이나 ai에 싣지 않는다. 승인 화면은 document에서 파일 이름·형식·크기·hash를 보여 주고, Markdown/txt는 기존 `skipHtml` 미리보기로 보여 준다. 승인 요금은 지금 `create_document`와 같은 billable operation 규칙을 따른다(`McpAgentApprovalService.java:381,420,444-453`).

## 결정 2: 저장 위치, 검증, 정리

### 승인 전 상태

| 대상 | 승인 전 | 승인 뒤 |
|---|---|---|
| 업로드 바이트 | `tmp/mcp-uploads/{workspace_id}/{user_id}/{upload_id}`. 문서 트리·검색·위키·임베딩·용량 합계에 없고, 다른 사용자에게 보이지 않는다. | `sources/documents/{id}/original`로 서버 안에서 copy하고, staged 객체는 지운다. |
| document core_db `mcp_document_uploads` | actor(user/workspace/grant/client/policy generation), 파일 이름, 서버가 정한 MIME, 신고 크기·hash, 검증한 versionId/ETag, state, 만료 시각, 멱등 키·request hash를 둔다. 본문은 없다. | terminal state와 만든 `document_id`만 남긴다. |
| `documents` 행, 편집 상태, 위키 페이지, 크레딧 청구 | 만들지 않는다. | 승인 실행에서 만든다. |

`documents` 행을 숨김 상태로 미리 만들지 않는다. 그렇게 하면 용량 합계, 트리, 검색, purge가 모두 숨김 필터를 알아야 한다. 업로드 상태는 별도 표가 소유한다.

state는 `AWAITING_BYTES → VERIFIED → PROPOSED → APPLIED | REJECTED | INVALIDATED`, `AWAITING_BYTES | VERIFIED → EXPIRED`, 검증 실패는 `FAILED`다. 모두 terminal이고 되살리지 않는다.

### 주소 발급

- 단일 presigned PUT 하나를 주고, 서명에 `Content-Type`을 묶는다. 실제 저장소가 지원하면 `x-amz-checksum-sha256`도 서명에 넣어 저장소가 바이트 무결성을 먼저 거르게 한다. 지원 여부는 구현할 때 S3·local MinIO로 확인한다. 지원하지 않아도 아래 완료 검증이 최종 판정이다.
- URL TTL은 15분이다. 웹 조각 URL과 같은 값이다(`DirectMultipartUpload.java:100`). URL은 bearer와 같으므로 로그와 감사에 남기지 않는다(`McpPdfTools.java:56`과 같다).
- 같은 key와 같은 request hash로 다시 부르면, `AWAITING_BYTES`인 동안은 같은 `upload_id`에 새로 서명한 URL을 준다. URL은 저장한 응답에 넣지 않는다.
- 클라이언트가 닿는 주소로 서명한다. local 모드에서는 PDF 창 이미지와 같은 서명 전용 endpoint 설정을 쓴다(`McpPdfTools.java:84-109`).
- 만들 때 `UploadLimitService.precheck`를 부른다. staged 바이트는 용량 합계에 들어가지 않으므로, 사용자별 활성 staged 업로드 개수와 바이트에 별도 상한을 둔다. 값은 launch gate다.

### 완료 검증

`complete_document_upload`는 행을 잠근 뒤 다음을 차례로 한다. 하나라도 실패하면 `FAILED`로 바꾸고 객체를 지운다. 새 업로드는 새 key로 시작한다.

1. 객체가 없으면 `UPLOAD_NOT_RECEIVED`(409, retryable)를 돌려주고 state는 바꾸지 않는다. URL이 만료됐으면 `HANDLE_EXPIRED`다.
2. `stat` 크기와 신고 크기가 같은지 본다. 다르면 `UPLOAD_SIZE_MISMATCH`다.
3. versionId/ETag를 고정하고 객체 전체를 stream하며 SHA-256을 계산한다. 신고 hash와 다르면 `UPLOAD_HASH_MISMATCH`다. 고정한 versionId/ETag는 승인 copy에서 `matchETag`로 다시 확인한다. 그래서 완료 뒤 아직 유효한 URL로 다시 PUT하면 승인이 실패한다.
4. 형식을 검사한다. PDF는 `%PDF-` signature를, Markdown/txt는 웹과 같은 `DocumentEditingRules.markdown`(UTF-8·크기)을 거친다. 실패하면 `UNSUPPORTED_FILE_TYPE`이다.
5. 현재 grant와 `requirePlacement`를 다시 확인한 뒤 제안을 만든다.

웹과 마찬가지로 내용 기준 중복 탐지와 악성코드 검사는 하지 않는다.

### 승인 실행

웹 업로드와 같은 `uploadOnce` 경로를 `StoredOriginal`로 탄다(`DocumentService.java:326-388`). 이름 번호 붙이기, `requirePlacement`, 용량 `enforce`, 멱등성, 실패 시 원본 정리가 그대로 적용된다.

달라지는 점은 세 가지다.

- `content_hash`와 `original_sha256`에 검증한 바이트 SHA-256을 넣는다. 직접 업로드처럼 `sha256(ETag:size)`를 넣으면, 클라이언트가 알고 있는 hash로 `start_pdf_conversion`을 부를 수 없다.
- origin은 `MCP_CLIENT` provenance를 남긴다.
- 업로드 자동 편입을 하지 않는다([결정 5](#결정-5-웹-업로드-자동-편입과의-관계)).

### 정리와 purge

- 거절·무효화·만료·실패 때 document가 staged 객체를 지운다. 이를 위해 platform에서 document에 `tmp/mcp-uploads/*` delete 권한을 더한다. `tmp/` 7일 lifecycle은 그 뒤의 안전망이다. 따라서 staged 업로드의 최대 수명(TTL 값은 launch gate)은 7일보다 짧아야 한다.
- grant 회수, 정책 비활성화, 세대 변경은 ADR 0046 결정 4의 표대로 승인 전 업로드를 `INVALIDATED`로 만든다.
- user/workspace purge는 ADR 0046 lifecycle matrix의 document 행 규칙을 따른다. 먼저 fence를 commit하고, 진행 중인 complete와 승인을 막은 뒤, 행과 staged 객체를 지운다. fence 뒤에 늦게 도착한 complete나 승인은 업로드를 되살리지 않는다.

### 감사와 멱등

- 감사는 ADR 0046 audit v1을 그대로 쓴다. `target_ids`는 create가 `[target_folder_id]` 또는 빈 목록, complete가 `[upload_id]`다. 파일 이름, URL, hash, 바이트는 넣지 않는다.
- create의 request hash는 `{file_name, size_bytes, content_hash, target_folder_id}`와 actor identity로 만든다. complete는 `upload_id`로 만든다. 같은 key에 다른 hash가 오면 `IDEMPOTENCY_CONFLICT`다. `upload_id`는 ADR 0046 handle 표의 `run_id`처럼 user/workspace/grant/client에 묶이고, 다른 소유자에게는 `HANDLE_NOT_FOUND`다.

## 결정 3: PUT할 수 없는 클라이언트에 대한 안내

서버는 클라이언트가 PUT할 수 있는지 알 수 없다. 그래서 정적 tool description과 응답의 고정 문구로 안내한다. 두 곳 모두 코드에 고정한 영어 문구이고, 워크스페이스 내용·파일 이름·사용자 텍스트를 넣지 않는다(ADR 0046 결정 1).

- `create_document_upload` description(Miro 방식): "Use only if you can send an HTTP PUT of the file's exact bytes to upload_url yourself, for example `curl -X PUT -H 'Content-Type: <required>' --data-binary @file '<upload_url>'`, with no auth header. If you cannot send HTTP requests, do not call this tool and do not paste file contents into other tools; tell the user to upload the file in Fruition web." 이어서 허용 형식과 크기 상한을 적는다.
- create 응답의 `next_step`: PUT한 뒤 complete를 부르고, PUT할 수 없으면 사용자에게 `web_upload_url`을 주라고 안내한다. `web_upload_url`은 workspace·folder ID만 담은 자사 URL이며 데이터 필드로 돌려준다.
- `UPLOAD_NOT_RECEIVED`는 retryable 오류다. 지금 오류 응답은 코드만 담으므로(`McpToolCatalog.java:117-125`) 안내는 위 description과 create 응답이 맡는다.

## 결정 4: 연쇄 작업의 승인 횟수

PDF를 위키까지 넣는 단순한 연쇄는 업로드 승인 → 변환 → 변환본 승인 → 편입 → 위키 초안 승인으로, 승인이 3번이다.

**권고는 (b) staged 연쇄 + 마지막 한 번의 묶음 승인이다. 두 층으로 나눠 구현한다.**

### U1: 업로드 단독

결정 1~3과 업로드만 담은 제안을 만든다. 연쇄 작업은 승인으로 문서가 생긴 뒤 기존 `document_id` 도구로 잇는다. Markdown은 승인 2번, PDF는 3번이다. 이 경로는 U2 뒤에도 기존 문서용으로 그대로 남으므로 임시 호환 계층이 아니다.

### U2: staged 원본 위에서 변환·편입 초안까지 만들고 승인은 한 번

- `start_pdf_conversion`은 `document_id` 대신 `upload_id`를 받을 수 있다. `source_content_hash`는 검증한 SHA-256이다. converter가 staged key를 읽도록 `tasks.md`의 `source_key` 형식과 platform 읽기 권한을 넓힌다.
- `finalize_pdf_conversion`에 선택 필드 `propose`(기본 `true`)를 더한다.
  - `true`이면 묶음 `[create_document_from_upload(PDF), create_document(변환본, source_document_id=앞 operation 결과)]`을 제안한다.
  - `false`이면 조립한 Markdown을 staged 영역으로 복사해 두고 `conversion_id`를 편입 원천으로 남긴다. `tmp/convert/` 1일 lifecycle보다 오래 승인을 기다릴 수 있어야 하기 때문이다.
  - 이미 있는 PDF 문서에서 출발한 변환도 `false`를 쓰면 승인이 2번에서 1번으로 줄어든다.
- `start_ingest`는 `document_id | upload_id | conversion_id` 중 정확히 하나를 받는다. staged 원천의 source snapshot은 `document_id` 없이 `upload_id`나 `conversion_id`와 version 1·content hash를 갖는다. ADR 0046 결정 12의 `source_snapshots` mapping은 승인 실행에서 새로 만든 `document_id`로 묶는다.
- `finalize_ingest`는 앞의 문서 operation과 위키 초안을 한 run으로 묶어 제안한다. 위키 부분은 ADR 0046의 full preview 승인 규칙을 그대로 따른다. 사용자는 위키만 빼고 승인할 수 있다. 의존하는 앞 operation을 빼면 뒤 operation도 함께 빠진다.
- 승인을 실행할 때 document가 문서(원본, 변환본)를 한 core_db 트랜잭션에서 먼저 만든다. 이어 ai가 snapshot hash와 만든 문서의 hash가 같은지 확인한 뒤 위키를 공개한다. 위키 적용이 `CONFLICTED`나 `UNKNOWN`이면 이미 만든 문서는 남고, 사용자는 그 문서로 편입을 다시 시작한다. 분산 트랜잭션이 아니라는 ADR 0046 결과와 같은 성격이다.
- 승인 전에는 결정 2의 staged 바이트와 기존 ai 상태(변환 세션, 편입 run·draft, `tmp/convert/mcp/` 묶음)만 있다. 문서, 위키, 청구는 없다. 예외는 지금도 finalize에서 크레딧을 판정한 뒤 실행되는 Jev 판정 비용(PR #254)이며, 이 동작은 바꾸지 않는다.

### 대안과 기각 사유

| 대안 | 장점 | 기각 사유 |
|---|---|---|
| (a) 단계마다 승인 | 새 서비스 간 작업이 없다. 승인마다 확정된 결과를 본다. | PDF는 승인이 3번이고, 중간 상태(위키 없는 원본·변환본)가 워크스페이스에 먼저 보인다. 사용자가 원한 "마지막 한 번"과 다르다. U1에서는 이 동작을 그대로 쓴다. |
| (c) 업로드 승인 때 웹 자동 파이프라인(서버 변환·편입) 실행 | 승인이 한 번이고 웹 업로드와 똑같이 동작한다. | 위키 결과를 미리 보지 않고 공개하고, 서버 생성형 LLM과 사용자 크레딧을 쓴다. MCP를 쓰는 이유(클라이언트 LLM이 작업)와 ADR 0046 결정 1-6에 맞지 않는다. |
| (d) 처음에 의도만 승인하고 결과는 자동 공개 | 승인이 한 번이다. | I9의 full preview 승인 규칙을 어긴다. |
| (b)를 숨김 `documents` 행으로 구현 | 기존 `document_id` 도구를 그대로 쓴다. | 용량·트리·검색·purge 전부에 숨김 필터가 필요하고, 하나라도 빠지면 승인 전 내용이 새어 나간다. |

U2에 드는 비용은 converter 원천 key, ai의 staged snapshot과 승인 시 mapping 바인딩, 묶음 승인 화면, 실행 순서의 네 가지다. 저장소 네 곳이 바뀌므로 U2는 U1이 운영에서 쓰인 뒤 별도 gate로 연다.

## 결정 5: 웹 업로드 자동 편입과의 관계

MCP 업로드를 승인해 만든 문서는 워크스페이스 `auto_ingest_markdown_on_upload` 설정과 상관없이 자동 편입하지 않는다. 가져오기 세션이 자동 편입을 건너뛰는 것과 같은 분기를 탄다(`DocumentService.java:501-506`). 이유는 세 가지다.

- 편입은 클라이언트가 `start_ingest`로 하거나, U2에서는 묶음에 이미 들어 있다. 서버 편입까지 돌면 같은 문서를 두 번 편입한다.
- MCP 경로에서 서버 생성형 LLM을 부르지 않는다.
- 사용자가 승인 화면에서 보지 않은 크레딧이 나가지 않는다.

이중 편입은 기존 document advisory fence가 같은 문서의 동시 active run을 막는 것으로 한 번 더 걸러진다(`docs/api/mcp-wiki.md:194`). U2 묶음으로 만든 PDF 변환본도 `auto_ingest_pdf_on_convert`를 적용하지 않는다.

## 결정 6: catalog version

- U1은 tool 두 개를 더하고 ADR 0046 operation 표를 바꾼다. 그 시점 catalog version을 하나 올린다. 열린 PR #253이 먼저 병합되면 `mcp-tools.v5`가 된다.
- U2는 `complete_document_upload`·`start_pdf_conversion`·`finalize_pdf_conversion`·`start_ingest` 스키마를 바꾸므로 다시 하나 올린다.
- 변경 기록은 Fruition-document `docs/api/mcp-read.md`에 남긴다.

## 대안과 기각 사유

| 대안 | 기각 사유 |
|---|---|
| tool 인자로 base64/inline 바이트 보내기 | 사용자 결정 2. 4 MiB 요청 상한과 모델 context 비용 때문에도 맞지 않는다. |
| 웹처럼 S3 multipart 조각 URL 여러 개 | 상한이 100 MiB여서 단일 PUT 한도(5 GB) 안에 들어간다. 조각을 나눠 보내는 일은 agent 쪽 실패 지점만 늘린다. |
| S3 presigned POST policy(`content-length-range`) | 저장소가 크기를 강제하는 장점은 있다. 하지만 multipart form 전송이 필요해 클라이언트가 더 어렵고, 사용자가 PUT을 정했다. 크기는 완료 검증이 최종 판정한다. |
| Fruition API가 바이트를 중계하는 PUT endpoint | ALB·앱 메모리·요청 상한을 거치고, 웹 직접 업로드가 이미 presigned로 피한 부하를 되살린다. |
| 웹 `/documents/uploads` API를 MCP 토큰으로 호출 | ADR 0046 결정 4가 웹 API와 MCP audience를 서로 거절하도록 정했다. |
| 도구 하나(주소 발급과 완료를 같은 이름의 반복 호출로 처리) | 입력 모양이 단계마다 달라져 strict schema와 멱등 판정이 흐려진다. Miro·Slack도 발급과 완료를 나눈다. |

## 결과

- 클라이언트가 원본 파일을 바이트 그대로 올릴 수 있다. 반영은 웹 승인 뒤에만 일어나고, 승인 전 바이트는 7일 lifecycle 안전망이 있는 숨은 staging에만 있다.
- PUT하지 못하는 클라이언트(일반 채팅 앱 등)는 웹 업로드를 안내받는다. 기능 차이가 남는다는 점을 의도적으로 받아들인다.
- MCP로 만든 PDF 문서의 `content_hash`는 바이트 SHA-256이고, 웹 직접 업로드 PDF는 ETag 지문이다. 두 값이 다르게 생성된다는 점을 문서화한다.
- U1에서는 연쇄 승인이 여러 번이다. U2 비용(저장소 네 곳)을 치르면 한 번으로 줄어든다.
- 구현 단계에서 document migration, platform IAM(`tmp/mcp-uploads/*` 쓰기·삭제, U2의 converter 읽기), frontend 승인 화면 변경이 필요하다. 이 ADR은 코드 변경을 포함하지 않는다.

## 미결 질문

1. MCP 업로드 승인에도 지금 `create_document`와 같은 승인 요금을 받을지, 웹 업로드처럼 무료로 할지.
2. staged 업로드의 최대 수명(7일 미만)과 사용자별 활성 staged 개수·바이트 상한 값.
3. 웹에 없는 악성코드 검사를 MCP 경로에 먼저 둘지, 아니면 웹과 함께 별도로 결정할지.
4. PUT하지 못하지만 Markdown 본문을 대화에 가진 클라이언트가 기존 `propose_plan` `create_document`로 새 문서를 제안하는 것을 허용할지. 지금 결정 3의 description은 막는 쪽이다.
5. U2를 열지, 언제 열지.

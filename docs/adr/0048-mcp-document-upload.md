# 0048. MCP 원본 파일 업로드는 웹 직접 업로드 API를 그대로 실행한다

- 상태: 제안
- 범위: Fruition-document, Fruition-ai(문서)
- 기준 커밋: ai `f41a57c`, document `072f7c6`(catalog `mcp-tools.v4`)
- 고치는 결정: [ADR 0046](0046-mcp-cross-service-contract.md) 결정 1의 닫힌 tool 목록과 1-6(쓰기는 제안만)

## 개정 기록

- 2026-11-11 첫 제안(PR #258)은 MCP 전용 업로드 경로를 만들었다. 전용 staging(`tmp/mcp-uploads/`, `mcp_document_uploads`), 웹 승인 operation `create_document_from_upload`, 바이트 SHA-256 `content_hash`, staged 연쇄와 묶음 승인(U2)이 그 내용이었다.
- 2026-11-11 이 개정은 사용자가 정한 원칙에 따라 그 경로를 모두 없앤다. 원칙은 "MCP 도구는 별도 경로를 만들지 않고 기존 웹 API를 그대로 실행한다. 다른 점은 서버가 하던 AI 단계를 사용자의 AI가 한다는 것뿐이다"이다.
  - 업로드는 AI 단계가 아니다. 그래서 웹과 같이 승인 없이 완료 즉시 문서가 된다.
  - 승인은 사용자 AI가 만든 결과(PDF 변환본, 위키 초안)에만 남는다. 이 승인은 기존 도구가 이미 하고 있다.
  - 그 결과 이전 안의 staging 표·prefix, 승인 operation, U2, 별도 hash 규칙이 필요 없어졌다.

## 맥락

MCP 클라이언트는 문서를 읽고, 편집·편입·PDF 변환을 제안할 수 있다. 하지만 사용자 컴퓨터의 원본 파일(Markdown, txt, PDF)을 올리는 도구가 없다. `start_ingest`와 `start_pdf_conversion`은 이미 있는 `document_id`를 받는다.

웹에는 업로드 경로가 둘 있다.

| 경로 | 형식 | 동작 | 근거(document `072f7c6`) |
|---|---|---|---|
| 일반 업로드 `POST /api/workspaces/{ws}/documents` (multipart) | PDF·Markdown·txt | 확장자로 MIME을 정하고 `.txt`는 `.md`로 받는다. Markdown은 UTF-8·5 MiB 검사 후 자동 편입 판정을 한다. | `DocumentService.java:342-357,893-926,501-506` |
| 직접 업로드 `POST .../documents/uploads` → `/parts` → `/complete` | PDF만 | 서명 ticket(1일), `tmp/document-uploads/{ws}/{user}/{uuid}`로 조각별 presigned PUT(15분, 한 번에 32개까지)을 보낸다. 확정 때 조립, 크기·Content-Type·`%PDF-` 검사, versionId/ETag 고정을 하고 일반 업로드와 같은 `DocumentService.upload`로 등록한다. | `DocumentDirectUploadService.java:52-181`, `DirectMultipartUpload.java:57-133` |

MCP 요청 body는 decompression 후 4 MiB가 상한이다(ADR 0046 결정 1). 또한 사용자 결정으로 base64 인자는 두지 않는다. 그래서 바이트를 tool 인자로 받지 않고 직접 업로드처럼 presigned PUT으로 받는다. Miro MCP `image_get_upload_url`과 Slack `files.getUploadURLExternal` → `files.completeUploadExternal`도 같은 "주소 발급 → PUT → 완료" 구조다.

## 결정 1: 웹 직접 업로드 API가 Markdown·txt도 받는다

MCP용으로 새 경로를 만들지 않는다. 기존 직접 업로드 API가 일반 업로드와 같은 형식 규칙을 따르도록 넓힌다. 판정 코드는 `DocumentService`의 기존 helper를 함께 쓰고, 복사하지 않는다.

- **start**: 파일 이름 검사(`validateFilename`), `.txt`→`.md`, 확장자 MIME 판정(`resolveMimeType`)을 일반 업로드와 같이 한다. 크기는 PDF면 `requirePdfSize`(100 MiB), Markdown이면 `MAX_MARKDOWN_BYTES`(5 MiB)로 미리 거절한다. 서명할 Content-Type은 판정한 MIME이다. 지원하지 않는 확장자는 일반 업로드와 같은 `415 UNSUPPORTED_FILE_TYPE`이다. 지금은 `400 "올바른 PDF 파일명이 필요합니다."`를 돌려준다.
- **complete**: 조립 객체의 크기와 Content-Type이 ticket과 같아야 한다. `%PDF-` signature는 PDF만 검사한다. Markdown의 UTF-8·크기는 등록 단계의 기존 `readUploadedMarkdown`이 검사한다. 등록은 지금처럼 `DocumentService.upload`가 한다.
- **PDF 동작은 바꾸지 않는다.** ticket, 경로, 조각 크기, 서명 TTL, 한도, versionId/ETag 고정, `content_hash`(PDF는 `sha256(ETag:size)`, Markdown은 본문 SHA-256인 기존 의미)가 모두 그대로다.
- **웹 영향**: 프론트는 지금처럼 Markdown·txt를 일반 업로드로 보내도 된다. 바꿀 것은 없다. 웹이 직접 업로드로 Markdown을 보내면 일반 업로드와 같이 자동 편입 판정(`wiki_ingest`)을 받는다. 가져오기 세션 헤더 규칙도 같다.

## 결정 2: MCP 도구는 그 서비스 메서드를 감싸기만 한다

| 도구 | 웹 API | 입력 | 출력 |
|---|---|---|---|
| `start_document_upload` | `POST .../uploads` | `{filename, size, folder_id}` | `{ticket, part_size, part_count, next_step}` |
| `get_upload_part_urls` | `POST .../uploads/parts` | `{ticket, first_part, count}` | `{parts:[{part_number, url}]}` |
| `complete_document_upload` | `POST .../uploads/complete` | `{idempotency_key, ticket}` | 웹 `DocumentUploadResponse` 필드 + `content_hash` + `next_step` |

- 웹 요청·응답 필드 이름과 의미를 그대로 쓴다.
- MCP에만 있는 필드는 셋이다.
  - `idempotency_key`: 웹은 `Idempotency-Key` 헤더로 받지만 MCP tool에는 헤더가 없어서 필드로 받는다.
  - `next_step`: 기존 MCP 도구처럼 다음에 부를 도구를 알려 주는 코드 고정 문구다. start는 조각 PUT 방법을, complete는 `start_ingest`(Markdown) 또는 `start_pdf_conversion`(PDF, `content_hash`를 `source_content_hash`로) 안내를 담는다.
  - `content_hash`: 웹 응답에는 없다. 그러나 `start_pdf_conversion`이 `source_content_hash`로 요구하고, 클라이언트는 이 값(PDF는 ETag 지문)을 스스로 계산할 수 없다. 그래서 `documents.content_hash`를 `sha256:` 형식으로 돌려준다.
- 웹의 `abort`와 가져오기 세션 헤더(`X-Fruition-Import-Id`)는 노출하지 않는다. 버려진 multipart는 버킷 lifecycle이 7일 뒤 정리하고(flatform `infra/terraform/s3.tf:78-85`), 가져오기는 웹 Notion 가져오기 기능이기 때문이다.
- **인가**: 웹과 같은 `requireMember`·`requirePlacement`·ticket의 user/workspace/key 대조를 그대로 탄다. 그 앞에서 MCP filter가 매 요청 현재 grant를 판정한다. scope는 기존 쓰기 scope인 `write_propose`다.
- **감사**: 다른 도구와 같은 body-free audit v1이다. `target_ids`는 start의 `folder_id`(있으면)뿐이다. ticket·URL·파일 이름은 남기지 않는다.
- ADR 0046 결정 1-6("쓰기는 제안만 만들고 웹 승인 뒤 실행")에서 업로드는 예외다. 업로드는 사람이 고른 파일을 그대로 저장할 뿐 AI가 만든 내용이 아니므로, 웹과 같이 승인 없이 저장한다.

## 결정 3: MCP 업로드는 서버 자동 편입을 하지 않는다

업로드 뒤 편입·변환은 사용자 AI가 `start_ingest`나 `start_pdf_conversion`으로 한다. 서버도 자동 편입을 돌리면 같은 문서를 두 번 편입하고 서버 LLM을 쓴다. 그래서 가져오기 세션이 쓰는 기존 분기(`DocumentService.java:501-506`, `wiki_ingest=NOT_APPLICABLE`)를 MCP 업로드에도 태운다.

가져오기 경로(`uploadImported`) 자체는 재사용하지 않는다. 그 경로는 열린 가져오기 세션을 요구하고, 항목 수를 세며, 문서를 `notion_import`로 표시하기 때문이다. 대신 그 분기를 정하는 기존 `UploadAccounting` 값이 "자동 편입 여부"를 갖게 하고, 가져오기와 MCP가 둘 다 `false`를 쓴다. 문서 origin은 웹과 같은 `upload`이고, MCP 출처는 audit에 남는다.

## 결정 4: PUT할 수 없는 클라이언트

정적 description(워크스페이스 내용 없음, ADR 0046 결정 1)에 다음을 적는다. 각 part URL에 해당 바이트 구간을 인증 헤더 없이 PUT하고(curl 예시), 모든 조각을 보낸 뒤 complete를 부르라고 한다. HTTP 요청을 보낼 수 없으면 이 도구를 부르지 말고, 파일 내용을 다른 도구에 붙여 넣지도 말고, 사용자에게 Fruition 웹에서 올리라고 안내하라고 한다. base64 경로는 없다.

## 결정 5: catalog version

도구 세 개를 더하므로 `mcp-tools.v4` → `mcp-tools.v5`로 올리고, Fruition-document `docs/api/mcp-read.md`에 기록한다.

## 대안과 기각 사유

| 대안 | 기각 사유 |
|---|---|
| MCP 전용 staging과 웹 승인(첫 제안) | 웹 업로드와 다른 두 번째 경로가 생기고, 승인·정리·hash 규칙을 따로 유지해야 한다. 업로드는 AI 결과가 아니므로 승인할 대상이 아니다. |
| Markdown·txt는 일반 multipart 업로드를 MCP로 감싸기 | 바이트가 tool 인자(base64)로 와야 한다. 사용자 결정 2와 4 MiB 상한에 막힌다. |
| MCP 전용 단일 presigned PUT | 직접 업로드의 multipart와 다른 경로가 된다. 100 MiB 이하 PDF와 Markdown은 조각 크기 64 MiB 기준 1~2조각이라 multipart의 부담도 작다. |
| 가져오기 세션으로 MCP 업로드 감싸기(자동 편입 skip) | 세션 생성·항목 수·`notion_import` 표시가 MCP 업로드 의미와 맞지 않는다. |

## 결과

- 웹과 MCP가 업로드 API 하나를 같이 쓴다. 웹 직접 업로드는 Markdown·txt도 받게 되고, PDF 동작은 그대로다.
- MCP 업로드의 보호 수준(형식·크기·용량·권한 검사)은 웹 업로드와 같다. 새 악성코드 검사는 두지 않는다.
- 업로드 직후 문서가 워크스페이스에 보인다. 편입·변환 결과만 기존 도구의 웹 승인을 거친다.
- local 저장소 모드에서는 part URL이 서버 안 저장소 주소로 서명되어 MCP 클라이언트가 닿지 못할 수 있다. 웹과 같은 서명을 쓰는 것이며, 배포 설정 문제로 남긴다.

## 미결 질문

1. 승인 없는 직접 쓰기를 기존 `write_propose` scope에 둘지, 새 scope(예: `upload`)로 동의를 따로 받을지. 이 개정은 access 변경 없이 `write_propose`를 쓴다.

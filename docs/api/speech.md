# 음성 대화·회의록 API

AI 내부 API다. 호출 서비스는 로그인 사용자의 workspace 권한을 검증하고
`X-Internal-Token`을 전달한다. AI에서도 Access의 workspace membership을 확인한다.
모든 음성·회의록 요청(WebSocket 포함)은 선택 헤더 `X-Request-Id`(최대 128자)를 받아 사용량 원장의
`run_id`로 쓴다. 없으면 `unattributed`로 기록한다([사용량 기록](tasks.md#모델별-사용량-조회)).
브라우저에는 내부 토큰과 OpenAI API 키를 전달하지 않는다.

## Agent 음성 대화

1. 사용자가 **Agent의 마이크 입력으로 말한 요청**을 `POST /speech/transcriptions`로 전사한다.
   마이크 발화를 오디오 bytes로 전송하는 것이며, 채팅의 녹음 파일 첨부 기능을 뜻하지 않는다.
2. 이 명령용 마이크 입력의 확정 `text`만 기존 Agent의 `message`로 전달한다. 대화·문서 문맥, 모델 선택,
   Kafka 작업 실행 및 문서 승인 계약은 기존 경로를 사용한다. 빈 전사는 전달하지 않는다.
3. 완료된 Agent 결과의 **`action == "chat_answer"`일 때만** `chat.answer`를
   `POST /speech/synthesis`로 보낸다. Query 중간 출력·검색 문맥·생각 과정은 읽지 않는다.
   `conversation_reply`, 편집, 생성, 승인 안내, 오류 메시지도 읽지 않는다.
4. 음성 생성이 실패해도 텍스트 답변은 유지한다. 재시도는 TTS만 수행하며 Agent를 다시 실행하지 않는다.

### `POST /speech/transcriptions`

- Query: `workspace_id`, `user_id` (각 1–128자).
- Body: multipart가 아닌 마이크 발화의 오디오 bytes. 아래 형식은 전송 인코딩이며 파일 첨부 UI 계약이 아니다.
- Content-Type: `audio/wav`, `audio/mpeg`, `audio/mp4`, `audio/webm`.
- 최대 24 MiB. 빈 파일은 거부한다. 모델: `gpt-transcribe`.
- 성공: `{"text":"사용자 발화"}`. 빈 text는 발화 없음으로 처리한다.
- 오류: 인증 `401/503`, workspace 거부 `403`, 크기 `413`, 형식 `415`, 빈 입력 `422`, 제공자 실패 `502`.

이 API는 전사만 반환하고 Agent를 실행하지 않는다. 오디오 bytes만으로 마이크 발화와
첨부 파일의 출처를 판별할 수 없으므로 호출 서비스가 입력 경로를 구분해야 한다.
현재 기능 범위에 채팅 파일 첨부는 포함하지 않는다. 추후 녹음 파일 첨부를 지원하더라도
그 전사는 요약·회의록 작성의 자료로 취급하며 Agent의 명령 `message`로 자동 전달하지 않는다.

### `POST /speech/synthesis`

```json
{"workspace_id":"workspace-id","user_id":"user-id","action":"chat_answer","answer":"Query 답변입니다."}
```

- action은 `chat_answer`만 허용한다. 호출 서비스는 **서버의 실제 Agent 결과에서 action·answer를
  가져와야 한다. 클라이언트가 주장하는 action으로 음성 재생 여부를 결정하면 안 된다.**
- answer: 공백 제거 후 1–4,000자. 긴 답변은 호출 서비스가 문장 단위로 나눠 순차 요청·재생한다.
- 성공: 전체 생성된 WAV bytes(PCM16 mono 24 kHz), `Content-Type: audio/wav`, `Cache-Control: no-store`. 비압축이라 약 48 KB/s다.
- 모델 `gpt-realtime-2.1-mini`(Realtime WebSocket, 원문 낭독 지침, `max_output_tokens="inf"`), 음색 `marin`. 제공자 실패는 `502`, 입력 오류는 `422`다.
- 화면에서 AI 생성 음성임을 안내한다. 재생 중지는 클라이언트가 처리한다.

## 실시간 전사

문서의 회의록 기능에서 청취하는 회의 내용은 수행 자료다. `delta`와 `completed`의 text를
Agent 명령으로 전달하지 않고, 확정 구간을 모아 회의 종료 후 `/meeting-notes/preview`에 전달한다.

`WS /speech/transcriptions/live?workspace_id=...&user_id=...`

인증된 호출 서버가 `X-Internal-Token` 헤더를 전달한다. 사용자용 Gateway는 별도 연동 대상이다.
HTTP middleware와 별도로 WebSocket handshake에서도 토큰과 workspace 권한을 확인한다.

1. 서버의 `ready`를 기다린다.
2. mono signed PCM16 little-endian, 24 kHz 음성을 binary frame으로 전송한다.
   프레임은 짝수 bytes, 최대 48,000 bytes(1초)다. WebM 파일 조각은 이 형식이 아니다.
3. 발화 경계에서 `{"type":"commit"}`을 보낸다. 구간은 100ms 이상, 30초 이하다.
   회의에서도 호출자가 주기적으로 commit하여 확정 전사를 저장한다. 자동 VAD는 사용하지 않는다.
4. 종료 시 `{"type":"finish"}`를 보낸다. 남은 음성을 commit하고 미완료 전사가 모두
   끝난 뒤 `finished`를 반환한다. 그 전에 연결을 닫으면 마지막 발화가 손실될 수 있다.

| 서버 이벤트 | 의미 |
|---|---|
| ready | 모델 설정 완료, sample_rate=24000 |
| committed | segment_id와 previous_segment_id로 발화 순서 제공 |
| delta | segment_id의 미확정 text, 화면 표시용 |
| completed | segment_id의 확정 text와 previous_segment_id, 미확정 문장을 교체 |
| finished | 전체 확정 완료, segment_count 제공 |
| error | 전사 중단, 확정 구간은 유지하고 재시도 |

완료 이벤트 도착 순서는 발화 순서와 다를 수 있다. segment_id로 중복을 제거하고
previous_segment_id로 순서를 맞춘다. 재연결한 세션 간 순서는 호출 서비스가 관리한다.
모델은 `gpt-live-transcribe`다. 화자·실명 식별, 단어별 시간 정보는 제공하지 않는다.

세션은 최대 60분, 미확정 요청은 최대 8개다. 제공자 이벤트가 60초간 없으면 종료한다.
자동 재연결·재전송은 하지 않는다. 연결이 끊긴 동안의 오디오 보관·재전송 여부는 호출
서비스가 결정하며 누락을 표시해야 한다. error를 finished로 취급하면 안 된다.

AI는 오디오와 전사를 영구 저장하지 않는다. 호출 서비스는 completed마다 확정 전사를
저장해야 한다. 원본 녹음 보관·재생은 Document/스토리지 연동 범위다.

## 회의록 초안

`POST /meeting-notes/preview`

```json
{
  "workspace_id":"workspace-id",
  "user_id":"user-id",
  "provider":"openai",
  "model":"gpt-6-luna",
  "display_name":"출시 회의",
  "segments":[{"id":"item_1","text":"출시는 다음 주 금요일로 확정하겠습니다."}]
}
```

`provider`·`model`은 필수이며 워크스페이스에서 고른 모델을 그대로 보낸다. 지원하지 않는
provider/model이거나 빠지면 `422`, 해당 provider의 API 키가 없으면 `503`이다.

회의 녹음 종료 후 확정 전사를 발화 순서대로 전달한다. 구간 ID는 중복 없는 영문·숫자·`_`·`-`,
최대 128자다. 최대 1,000구간, 구간별 10,000자, 전체 100,000자다. 초과하면 `422`이며
내용을 조용히 자르지 않는다.

결과: display_name, markdown, summary, decisions, action_items, open_questions.
네 배열의 각 항목은 text와 source_segment_ids를 가진다. 존재하지 않는 구간을
참조하거나 근거가 없으면 `502`로 실패한다. 이는 참조 유효성 검증이며 의미적 정확성을
보장하지 않는다. 모델은 요청의 `provider`·`model`로 호출한다.

회의 발언은 자료로만 취급하고 Agent 라우터·Tool 실행에 전달하지 않는다. 담당자·기한·합의를
추측하지 않도록 지시한다. 결과는 초안이며 문서 반영은 기존 승인·버전 검증을 거쳐야 한다.
실패 시 호출 서비스가 전사를 유지하고 초안 생성만 재시도한다.

현재 구현은 `GenerateMeetingNotes` → `ChatMeetingNotes`의 별도 초안 생성 경로다.
Agent의 `GenerateMarkdownEditUseCase`나 편집 평가·재시도 로직을 호출하지 않으며,
기존 승인·문서 수정 경로에도 아직 연결하지 않았다. 응답의 Markdown과 근거 배열을
반환할 뿐 DB·S3에 저장하거나 문서 본문을 변경하지 않는다.

## 연동 범위와 검증

### Document 저장 경로 재사용

Document 저장소의 `main` 커밋 `99d4b55f` 기준으로 다음 기존 API를 사용할 수 있다.
AI의 요청·응답 이름은 문서 생성 DTO에 맞춰 `display_name`, `markdown`을 사용한다.
이전 `title` 입력은 지원하지 않는다.

| 목적 | 기존 Document API | 호출자가 전달할 내용 |
|---|---|---|
| 새 문서로 저장 | `POST /api/workspaces/{workspace_id}/documents/markdown` | JSON `display_name`, `markdown`, 선택 `folder_id`; 필수 `Idempotency-Key` 헤더 |
| 열린 문서에 반영 | `PUT /api/workspaces/{workspace_id}/documents/{document_id}/content` | multipart `markdown`(반영 후 전체 본문), `base_revision`, `revision_write_id` |

새 문서 생성에는 응답 중 `display_name`, `markdown`만 골라 전달한다. `folder_id`와
멱등 키는 호출자가 관리한다. 같은 저장 재시도에는 같은 키와 같은 본문을 사용한다.
`summary` 등 근거 배열은 저장 요청의 필드가 아니므로 통째로 전달하지 않는다.

열린 문서에는 현재 본문과 회의록을 사용자 선택 위치에서 합친 **전체 Markdown**을 저장한다.
AI 응답의 회의록만 전체 본문으로 보내 기존 내용을 덮어쓰면 안 된다. 본문을 읽을 때의
`edit_revision`을 `base_revision`으로 전달하고 같은 저장 재시도에는 같은 `revision_write_id`를
사용한다. 충돌 `409`는 본문을 재조회하고 변경 내용을 다시 확인해야 한다.

Document는 workspace/문서 소유권·편집 가능 여부·잠금·revision을 검사한다. AI의 workspace
membership 검증만으로 문서 저장 권한이 보장되지 않는다. 새 문서의 편집 본문은 PostgreSQL
`document_edit_states`에 저장되며, 생성 시 원본 Markdown은 기존 S3/MinIO 경로에도 저장된다.
본문 변경 역시 기존 PostgreSQL 편집 저장 경로를 따른다. AI가 직접 저장소에 쓰지 않는다.

Agent 작업 이력 연결용 `apply_operation_id`는 Backend가 발급·검증하는 적용 표다.
회의록 API는 이 표를 발급하지 않으므로 임의 값을 만들거나 `source=agent`만 붙여 Agent
승인 이력이 남는다고 간주하면 안 된다. 기존 저장 API로 사용자가 수락한 본문을 저장하는 것과
Agent 승인·감사 경로에 회의록 작업을 등록하는 것은 별도 연동이다.

확인한 구현: [생성 DTO](https://github.com/FruitionKR/Fruition-document/blob/99d4b55fbbfa4ffd0bbdc50e2931873c2889dd2f/src/main/java/fruition/core/document/dto/MarkdownDocumentCreateRequest.java),
[저장 API](https://github.com/FruitionKR/Fruition-document/blob/99d4b55fbbfa4ffd0bbdc50e2931873c2889dd2f/src/main/java/fruition/core/document/controller/DocumentController.java),
[생성·저장 서비스](https://github.com/FruitionKR/Fruition-document/blob/99d4b55fbbfa4ffd0bbdc50e2931873c2889dd2f/src/main/java/fruition/core/document/service/DocumentService.java).

### 별도 연동과 검증

이 저장소는 AI API만 제공한다. 마이크 권한·녹음 표시·참석자 고지·음성 재생·회의록 UI,
사용자용 Gateway, 전사/녹음 저장과 문서 반영은 프런트엔드·Document의 연동 범위다.
온라인 회의의 시스템 오디오 캡처도 클라이언트가 담당한다.
채팅에서 녹음 파일을 첨부하는 기능은 이번 범위에 포함하지 않는다.

가짜 제공자로 입력/권한·Query 전용 TTS·전사 역순 완료·종료·근거 검증을 회귀 테스트한다.
실제 마이크·실모델의 한국어 정확도, 지연, 계정별 모델 사용 가능 여부는 별도 통합 검증이 필요하다.

공식 계약: [실시간 전사](https://developers.openai.com/api/docs/guides/realtime-transcription),
[파일 전사](https://developers.openai.com/api/docs/guides/speech-to-text),
[음성 합성](https://developers.openai.com/api/docs/guides/text-to-speech).

## 연동

다른 도메인 문서와 같은 세 항목을 API마다 기록한다. 음성 사슬은 **서비스 사이는 연결되어
있으나 프런트엔드에는 진입점이 없다.** document-svc는 `/api/workspaces/{workspace_id}/meetings/**` 공개 REST API와
`/api/meetings/{meetingId}/live` WebSocket까지 올려 두었지만(Fruition-document
`src/main/java/fruition/core/meeting/MeetingController.java`,
`src/main/java/fruition/core/meeting/MeetingLiveConfig.java`:42,
`src/main/java/fruition/core/meeting/MeetingNotesController.java`),
Fruition-frontend 전체에서 `/api/meetings`·`meeting-notes`·`/speech` 호출과
`MediaRecorder`·`getUserMedia`·`navigator.mediaDevices`·`AudioContext`·`WebSocket`·`new Audio(`
사용이 모두 0건이다. 즉 녹음·실시간 표시·음성 재생 UI가 없어 사용자가 이 경로를 쓸 수 없다.

### `POST /speech/transcriptions`

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/speech/SpeechTranscriptionClient.java`:31-38 (공개 `POST /api/workspaces/{workspace_id}/speech/transcriptions` 중계), 그리고 회의 녹음 파일 전사 worker `src/main/java/fruition/core/meeting/MeetingTranscriptionWorker.java`:69-72. 둘 다 `app.speech.transcription-endpoint`를 쓴다.
- 아웃바운드 호출: OpenAI `POST https://api.openai.com/v1/audio/transcriptions`(`pipeline/app/modules/speech/infrastructure/openai_speech.py`:40), 권한 확인은 access-svc `GET /internal/authz/workspaces/{workspace_id}/users/{user_id}`(`pipeline/app/modules/skill/infrastructure/workspace_authorization.py`:19, `ACCESS_INTERNAL_BASE_URL`).
- 미연동 표시: 서비스 간 연결됨. **프런트엔드 미연동** — 호출하는 화면이 없다.

### `POST /speech/synthesis`

- 인바운드 호출자: **호출자 없음.** document-svc에 `synthesis` 문자열과 TTS endpoint 설정이 없다. `app.speech.*` 설정은 전사·실시간·회의록 세 개뿐이다(Fruition-document `src/main/resources/application.properties`:98,100,103).
- 아웃바운드 호출: OpenAI `POST https://api.openai.com/v1/audio/speech`(`pipeline/app/modules/speech/infrastructure/openai_speech.py`:65), access-svc 권한 확인.
- 미연동 표시: **전 구간 미연동.** 중계하는 backend도, 재생하는 화면도 없다.

### `WS /speech/transcriptions/live`

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/meeting/MeetingLiveHandler.java`:146-152. JDK `HttpClient.newWebSocketBuilder`로 `app.speech.live-endpoint`에 `X-Internal-Token`을 붙여 접속하고, 사용자 쪽은 `/api/meetings/{meetingId}/live`로 받는다.
- 아웃바운드 호출: OpenAI realtime `wss://api.openai.com/v1/realtime?intent=transcription`(`pipeline/app/modules/speech/infrastructure/openai_speech.py`:97), access-svc 권한 확인.
- 미연동 표시: 서비스 간 연결됨. **프런트엔드 미연동** — WebSocket을 여는 코드가 없다.

### `POST /meeting-notes/preview`

- 인바운드 호출자: Fruition-document `src/main/java/fruition/core/meeting/MeetingNotesClient.java`:32 (`app.speech.meeting-notes-endpoint`).
- 아웃바운드 호출: 요청의 `provider`·`model`로 ChatCompletions를 묶음마다 호출한다(`pipeline/app/modules/meeting_notes/infrastructure/chat_meeting_notes.py`), access-svc 권한 확인(`routes.py`의 `authorize_speech`).
- 미연동 표시: 서비스 간 연결됨. **프런트엔드 미연동.**

### 묶음 동시 호출 (`/meeting-notes/preview` 구현 기준)

`pipeline/app/modules/meeting_notes/infrastructure/chat_meeting_notes.py` 기준이다.

- 전사를 순서대로 `BATCH_CHAR_BUDGET = 25000`자 단위로 묶고, 묶음을 `ThreadPoolExecutor`로
  최대 `MAX_CONCURRENT_BATCHES = 4`개까지 동시에 호출한다. 소요 시간은 묶음 수가 아니라
  호출 예산이 정한다.
- 묶음 호출 하나는 `BATCH_TIMEOUT_SECONDS = 60`, 재시도 `MAX_BATCH_RETRIES = 1`,
  출력 예산 `MAX_OUTPUT_TOKENS = 16000`이다.
- 응답 JSON이 잘리면(`JsonParseError`) 그 묶음을 절반으로 쪼개 다시 묻는다. 라운드 상한은
  `MAX_SPLIT_ROUNDS = 2`이고, 더 쪼갤 수 없거나 라운드가 남지 않으면 실패로 올린다.
  최악의 경우 60초 × (재시도 1 + 1) × 라운드 2 = 240초다.
- 묶음별 결과는 완료 순서가 아니라 전사 순서로 합친다. 각 항목의 `source_segment_ids`는
  그 묶음에 실제로 있는 구간 id만 남기고, 근거가 하나도 남지 않으면 항목을 버린다.
  합칠 때 섹션당 `MAX_SECTION_ITEMS = 100`개, 항목당 `MAX_ITEM_CHARS = 2000`자로 맞춘다.
- 전송 오류는 그대로 올라오며 부분 회의록을 만들지 않는다.

### 위 "Document 저장 경로 재사용" 표의 현재 상태

그 표는 AI 응답을 기존 문서 저장 API로 직접 보내는 안을 적은 것이고, document-svc는 그 뒤
전용 경로를 구현했다. 현재는 `POST /api/workspaces/{workspace_id}/meetings/{meeting_id}/notes`로
초안 버전을 만들고 `.../notes/{version}/append-preview`·`.../notes/{version}/apply`로
문서에 반영한다(Fruition-document `src/main/java/fruition/core/meeting/MeetingNotesController.java`:54,75,98,126).
ai-svc가 저장하지 않는다는 서술은 그대로 유효하다. 저장·버전·멱등 키 책임은 document-svc에 있다.

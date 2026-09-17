# 음성 대화·회의록 API

AI 내부 API다. 호출 서비스는 로그인 사용자의 workspace 권한을 검증하고
`X-Internal-Token`을 전달한다. AI에서도 Access의 workspace membership을 확인한다.
브라우저에는 내부 토큰과 OpenAI API 키를 전달하지 않는다.

## Agent 음성 대화

1. 녹음을 `POST /speech/transcriptions`로 전사한다.
2. 확정 `text`를 기존 Agent의 `message`로 전달한다. 대화·문서 문맥, 모델 선택,
   Kafka 작업 실행 및 문서 승인 계약은 기존 경로를 사용한다. 빈 전사는 전달하지 않는다.
3. 완료된 Agent 결과의 **`action == "chat_answer"`일 때만** `chat.answer`를
   `POST /speech/synthesis`로 보낸다. Query 중간 출력·검색 문맥·생각 과정은 읽지 않는다.
   `conversation_reply`, 편집, 생성, 승인 안내, 오류 메시지도 읽지 않는다.
4. 음성 생성이 실패해도 텍스트 답변은 유지한다. 재시도는 TTS만 수행하며 Agent를 다시 실행하지 않는다.

### `POST /speech/transcriptions`

- Query: `workspace_id`, `user_id` (각 1–128자).
- Body: multipart가 아닌 음성 파일 bytes.
- Content-Type: `audio/wav`, `audio/mpeg`, `audio/mp4`, `audio/webm`.
- 최대 24 MiB. 빈 파일은 거부한다. 모델: `gpt-transcribe`.
- 성공: `{"text":"사용자 발화"}`. 빈 text는 발화 없음으로 처리한다.
- 오류: 인증 `401/503`, workspace 거부 `403`, 크기 `413`, 형식 `415`, 빈 입력 `422`, 제공자 실패 `502`.

### `POST /speech/synthesis`

```json
{"workspace_id":"workspace-id","user_id":"user-id","action":"chat_answer","answer":"Query 답변입니다."}
```

- action은 `chat_answer`만 허용한다. 호출 서비스는 **서버의 실제 Agent 결과에서 action·answer를
  가져와야 한다. 클라이언트가 주장하는 action으로 음성 재생 여부를 결정하면 안 된다.**
- answer: 공백 제거 후 1–4,000자. 긴 답변은 호출 서비스가 문장 단위로 나눠 순차 요청·재생한다.
- 성공: 전체 생성된 MP3 bytes, `Content-Type: audio/mpeg`, `Cache-Control: no-store`.
- 모델 `gpt-4o-mini-tts`, 음색 `coral`. 제공자 실패는 `502`, 입력 오류는 `422`다.
- 화면에서 AI 생성 음성임을 안내한다. 재생 중지는 클라이언트가 처리한다.

## 실시간 전사

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
  "title":"출시 회의",
  "segments":[{"id":"item_1","text":"출시는 다음 주 금요일로 확정하겠습니다."}]
}
```

종료 후 확정 전사를 발화 순서대로 전달한다. 구간 ID는 중복 없는 영문·숫자·`_`·`-`,
최대 128자다. 최대 1,000구간, 구간별 10,000자, 전체 100,000자다. 초과하면 `422`이며
내용을 조용히 자르지 않는다.

결과: title, markdown, summary, decisions, action_items, open_questions.
네 배열의 각 항목은 text와 source_segment_ids를 가진다. 존재하지 않는 구간을
참조하거나 근거가 없으면 `502`로 실패한다. 이는 참조 유효성 검증이며 의미적 정확성을
보장하지 않는다. 모델은 기존 보안·개인정보 마스킹 경로의 `gpt-5-nano`다.

회의 발언은 자료로만 취급하고 Agent 라우터·Tool 실행에 전달하지 않는다. 담당자·기한·합의를
추측하지 않도록 지시한다. 결과는 초안이며 문서 반영은 기존 승인·버전 검증을 거쳐야 한다.
실패 시 호출 서비스가 전사를 유지하고 초안 생성만 재시도한다.

## 연동 범위와 검증

이 저장소는 AI API만 제공한다. 마이크 권한·녹음 표시·참석자 고지·음성 재생·회의록 UI,
사용자용 Gateway, 전사/녹음 저장과 문서 반영은 프런트엔드·Document의 연동 범위다.
온라인 회의의 시스템 오디오 캡처도 클라이언트가 담당한다.

가짜 제공자로 입력/권한·Query 전용 TTS·전사 역순 완료·종료·근거 검증을 회귀 테스트한다.
실제 마이크·실모델의 한국어 정확도, 지연, 계정별 모델 사용 가능 여부는 별도 통합 검증이 필요하다.

공식 계약: [실시간 전사](https://developers.openai.com/api/docs/guides/realtime-transcription),
[파일 전사](https://developers.openai.com/api/docs/guides/speech-to-text),
[음성 합성](https://developers.openai.com/api/docs/guides/text-to-speech).

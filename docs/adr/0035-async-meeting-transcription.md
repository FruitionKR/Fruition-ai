# 0035. 회의 녹음 전사는 Kafka 비동기 작업으로 옮기고, AI가 녹음을 구간으로 나눠 구간 단위로 멱등하게 전사한다

## Status
Accepted

## Context
- 회의 녹음 파일은 `POST /speech/transcriptions` 한 번으로 동기 전사했다(Fruition-ai#89). 파일은 24 MiB까지라 mp3로 약 25분이 한계였고, 처리 시간이 녹음 길이에 비례하는데도 document 백그라운드 worker가 HTTP 응답을 기다렸다. ADR 0032 결정 2("길이를 예측할 수 없는 작업은 비동기로")의 대상이다.
- 회의록(#87)은 이미 `ai.maintenance.command`의 비동기 작업이다.

## Decision
- **M1. Kafka 비동기.** `kind: meeting_transcription` command를 `ai.maintenance.command`로 받고 결과를 `ai.task.event`로 낸다. 회의록과 같은 journal 등록·취소·재전달 경로를 쓴다.
- **M2. AI가 구간을 나눈다.** PyAV로 디코딩해 16 kHz mono PCM16으로 바꾸고 목표 10분 구간 WAV(약 19 MB, 공급사 한도 25 MB 미만)로 자른다. 구간은 최대 4개씩(`MEETING_TRANSCRIPTION_CONCURRENCY`) 전사해 순서대로 잇는다. 구간 전사는 체크포인트로 남겨 같은 run이 재전달돼도 끝난 구간을 다시 과금하지 않는다.
- **M3. S3 직접 업로드.** 브라우저가 녹음을 S3에 직접 올리고(document의 기존 multipart 업로드), AI가 `recording_key`로 읽는다. Kafka에는 객체 키만 싣는다.
- **M4. 길이 상한 3시간.** 24 MiB 한도는 없앤다. AI가 디코딩한 길이로 판정하고 넘으면 공급사 호출 전에 `MEETING_RECORDING_TOO_LONG`으로 실패한다(파일 크기 500 MiB는 document가 지킨다).
- **구간 경계 규칙.** 구간은 목표 길이(10분)를 넘지 않는다. 목표 직전 20초 안에서 에너지(제곱합)가 가장 낮은 100 ms 창을 찾아 그 가운데에서 자른다(같으면 목표에 가까운 쪽). 겹침이 없어 단어가 중복되지 않고, 무음에서 자르므로 말이 잘릴 가능성이 낮다. 0.1초보다 짧은 꼬리는 버린다.
- **체크포인트.** `pipeline-runs/meeting-transcription/<workspace_id>/<sha256(run_id) 앞 32자>/<index>.txt`. 전사는 개인정보라 run이 끝나면(성공·실패·취소, 재생 포함) 지우고, 다른 worker가 실행 중인 재전달에서는 지우지 않는다. 워크스페이스 파기는 접두사 전체를, 사용자 파기는 그 사용자의 `meeting_transcription` run 접두사를 지운다. 스토리지 lifecycle은 최후 보루다. 객체 작업 변경 기록(롤백)에는 얹지 않는다. 롤백하면 재전달이 건너뛸 체크포인트가 사라지기 때문이다.
- **journal.** 전사는 document가 소유한다. 회의록(ADR 0033 결정 6, ai#97)과 같이 종료한 run의 result 본문과 command의 `recording_key`를 지우고 hash·상태·`error_code`만 남긴다. 완료된 run의 재전달은 공급사를 부르지 않고 `{"scrubbed": true}`를 재생한다.
- **마감과 취소.** 전체 마감 `MEETING_TRANSCRIPTION_DEADLINE_SECONDS`(1500초)는 worker `max_poll_interval`(1800초)보다 짧다. 취소와 마감은 구간 사이와 공급사 호출 직전에 확인하고, 한 구간이 실패하면 남은 구간을 호출하지 않는다.
- **오류.** 코드만 낸다: `MEETING_RECORDING_TOO_LONG`, `MEETING_RECORDING_UNREADABLE`, `MEETING_TRANSCRIPT_EMPTY`, `MEETING_TRANSCRIPT_TOO_LONG`(100,000자), 공통 코드. 예외 원문은 싣지 않는다.

## Alternatives
- **서버에서 구간 대신 공급사에 통째로 보내기**: 공급사 파일 한도(25 MB)에 막혀 길이 제한이 남는다. 기각.
- **구간마다 겹침(overlap) 두고 중복 제거**: 중복 단어를 걸러내는 로직이 필요하고 경계 오류가 남는다. 무음 경계가 더 단순하다. 기각.
- **체크포인트를 journal(DB)에 저장**: 전사를 DB에 남기면 개인정보 보관 범위가 넓어지고 journal scrub 규칙과 충돌한다. 임시 S3 객체와 종료 시 삭제가 낫다. 기각.
- **브라우저가 구간을 잘라 업로드**: 클라이언트마다 코덱·구현이 달라 경계 품질을 보장할 수 없다. 기각.
- **완료 결과를 journal에 계속 보관해 재생**: 재전달 때 전사를 복원할 수 있지만 개인정보를 AI에 남긴다. 첫 이벤트 유실은 document의 새 run 재시도로 해결한다. 기각.

## Consequences
- 24 MiB 한도가 사라지고 최대 3시간 녹음을 전사한다. 처리 시간은 녹음 길이에 비례해 최대 마감 1500초를 넘으면 실패한다.
- flatform: pipeline worker에 `meetings/*` 읽기, 체크포인트 접두사 읽기·쓰기·삭제 IAM, 임시 디스크 1.2 GiB 이상, 메모리 여유가 필요하다. PyAV wheel이 FFmpeg를 포함해 apt 의존성은 없다.
- 디코딩·내려받기는 스레드에서 돌아 마감에 걸려도 끝날 때까지 돈다. 더 길어지면 프로세스 분리를 검토한다.
- `POST /speech/transcriptions`는 채팅 음성 받아쓰기용으로 그대로 둔다. 오류 코드만 공급사 429(`provider_rate_limited`)와 시간 초과(`deadline_exceeded`)를 구분한다(HTTP 응답은 502 그대로).

# 문서 비용 사전 확인

[API 목록](README.md)

`POST /internal/documents/pdf-cost-features`는 `X-Internal-Token`으로 인증하고
`{"source_key":"sources/documents/<document-id>/original"}`를 받는다. document-svc가 원본 접근 권한을 확인한 뒤 부른다.
유료 공급사 호출은 하지 않는다.

응답은 `pages`, `text_bytes`(UTF-8), `images`, `scanned_pages`(추출 글자 40자 미만인 쪽)다.

- 저장소 크기를 먼저 보고 `PDF_COST_MAX_BYTES`(기본 128MB)를 넘으면 내려받지 않고 413을 돌려준다.
- 파싱은 `python -I` 하위 프로세스에서 한다. CPU 시간·메모리(Linux)·실행 시간(`PDF_COST_TIMEOUT_SECONDS`, 기본 20초)을 제한한다.
  앞쪽 `PDF_COST_MAX_PAGES`(기본 300)쪽만 읽고 나머지 쪽은 읽은 쪽의 비율로 외삽한다.
- 암호화·빈 PDF, 파서 실패·크래시·시간 초과는 422다. document는 이를 영구적인 "읽을 수 없는 PDF"로 기록한다.
- 동시 파싱은 `PDF_COST_CONCURRENCY`(기본 2)개다. 10초 안에 자리가 나지 않으면 503을 돌려준다.
- 저장소 오류는 그대로 오류다. 0원 견적으로 바꾸지 않는다.

배포 순서: 이 엔드포인트를 document-svc의 비용 사전 확인보다 먼저 배포한다.

## 연동

- 인바운드 호출자: document-svc `DocumentCostEstimator.inspect`(PDF 변환 시작, 가져오기 변환 예상)
- 아웃바운드 호출: 객체 저장소 `stat_object`·`fget_object`
- 미연동 표시: 없음

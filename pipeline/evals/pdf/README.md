# PDF Converter 유형별 평가

운영 converter(`converter/`, docling + selective repair)가 유형별 PDF를 Markdown으로 얼마나 온전하게 바꾸는지 잰다.

## 평가셋

- `cases.json`: 유형 5개(text·table·multicolumn·scan·mixed) × 3개 = 15개. 출처(arXiv는 고정 버전)·라이선스·원본 쪽 범위·원본 sha256(`original_sha256`)·스캔 재현 설정(`derivation`)을 적는다.
- `sources/<id>.pdf`: 원본에서 대표 쪽(최대 10쪽)만 잘라 둔 파일. **저장소에 넣지 않는다**(`.gitignore`). 원본을 `source_url`에서 받아 `<원본 폴더>/<id>.pdf`로 두고 `python tools/prepare_sources.py <원본 폴더>`로 만든다. 원본 sha256이 다르면(법령 개정·첨부 교체) 그 사례는 만들지 않는다. 모두 공공누리 제1유형, CC BY 4.0, 퍼블릭 도메인, 법령(저작권법 제7조) 중 하나다.
  - 스캔 재현본 2개(법령 9~11쪽, 인공지능 윤리원칙 16~19쪽)와 1978년 실제 스캔(원래 OCR 글자 정보를 버림)은 `derivation.rasterize` 설정으로 글자 정보 없는 흑백 JPEG 이미지 PDF로 만든다.
- `gold/<id>.json`: 쪽 이미지를 보고 옮겨 적은 필수 문장(읽는 순서대로)과 표 행. converter 출력을 보고 만들지 않는다.
- `gold.lock.json`: 정답 검수 뒤 `python tools/run_pdf.py --write-lock`으로 고정한다. `cases.json`·`gold/`는 파일 sha256, `sources/`는 내용(쪽별 글자·이미지) 기준 sha256이다. PDF는 저장할 때마다 문서 ID·생성 시각이 바뀌어 바이트로는 다시 만들 수 없다. 실행기는 lock과 다르면 멈춘다. 개정 이력은 `revisions`에 있다.
- 정답은 쪽 이미지를 보고 AI(Claude)가 작성하고 다른 AI가 독립 검수했다. 사람 검수를 거치지 않았다.

## 실행

```bash
docker build -f converter/Dockerfile -t fruition-converter:<커밋> .   # 저장소 루트에서
cd pipeline/evals/pdf/tools
python run_pdf.py --image fruition-converter:<커밋>                   # 사례마다 3회, results/<날짜>/
python grade_pdf.py ../results/<날짜>                                 # grades.jsonl, summary.json
python test_grade_pdf.py                                              # 채점 규칙 확인(모델 호출 없음)
```

- 변환은 운영과 같은 `process_pdf`(crop-first + selective repair)이고, repair 모델은 평가 모델 `gpt-6-luna`다.
- 컨테이너 env(키·DB)는 실행 중인 converter 컨테이너(`--env-from`, 기본 `fruition-markitdown`)에서 출력 없이 임시 파일로 넘긴다.

## 판정

- **내용 누락:** 결과에 필수 문장이 없으면 1건. NFKC 정규화 후 공백과 Markdown·HTML 기호를 지우고 비교하므로 줄바꿈·띄어쓰기 차이는 오류가 아니다.
- **순서 오류:** 찾은 필수 문장끼리 이웃한 쌍의 순서가 원문과 반대면 1건.
- **표 오류:** 정답 표 행의 셀이 결과의 한 표 행(Markdown `|` 행 또는 HTML `<tr>`) 안에 순서대로 없으면 행마다 1건.
- **복원율:** (필수 문장 수 − 내용 누락) / 필수 문장 수. 표 행 복원율도 같은 방식으로 센다. 표본 검사라 쪽 전체 누락 같은 손실을 과소평가한다. 발표 수치는 문서별 요소(제목·본문·표 셀·수식·이미지) 전수 대조로 냈다.
- **변환 성공(참고):** 실행이 끝나고 누락·순서·표 오류가 모두 0건.
- 처리 시간은 컨테이너 시작을 포함한 사례별 변환 시간이다.

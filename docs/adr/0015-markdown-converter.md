# 0015. Use an AnyDoc Crop-First Pipeline for Markdown Conversion

## Status
Accepted

## Context
동일한 4개 PDF의 445개 block을 비교한 실험에서 fresh Docling 기준선은 201/445(45.17%), AnyDoc 본문과 원본 crop을 결합한 경로는 400/445(89.89%)의 실사용 정확도를 보였다. 모든 block을 모델로 처리하는 방식은 token 비용 때문에 기본 경로로 사용하기 어려웠다.

## Decision
PDF Markdown 변환의 기본 경로는 AnyDoc으로 본문을 추출하고 표·수식은 원본 crop으로 복원하며 그림은 원본 asset을 보존하는 crop-first pipeline을 사용한다.

## Alternatives Considered
- fresh Docling을 기본 경로로 유지: 동일 block 비교에서 정확도가 낮아 기각했다.
- 모든 block을 모델로 복원: 정확도 비교 기준으로는 유효하지만 token 비용 때문에 기각했다.
- AnyDoc으로 표·수식까지 복원: AnyDoc은 본문 추출에 사용하고 특수 영역은 원본 crop을 사용하기로 했다.

## Consequences/Tradeoffs
### Positive
- 비교 실험에서 fresh Docling보다 높은 실사용 정확도를 보였다.
- 모델이 처리할 영역을 제한해 전체 block을 처리하는 방식보다 token 사용을 줄일 수 있다.
- 그림은 재생성하지 않고 원본 asset을 유지할 수 있다.

### Negative
- 본문 추출, 영역 검출, crop 복원과 결과 조립을 함께 운영해야 한다.
- 긴 표·수식 처리에서 tail latency가 발생할 수 있다.
- 현재 정확도 비교는 30페이지 실험이므로 전체 문서 유형을 대표하지 않는다.

## Follow-up
- 현재 `docling-only`인 제품 기본 mode를 AnyDoc crop-first로 전환한다.
- 전체 blind 평가와 대용량 PDF 처리 시간을 추가로 검증한다.

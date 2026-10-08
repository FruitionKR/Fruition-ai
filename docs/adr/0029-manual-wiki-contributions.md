# 0029. 사람이 고친 Wiki 본문을 수동 기여로 보존한다

## Status
Accepted

## Context
맥 데스크톱 앱은 AI가 만든 source·concept 페이지를 사람이 직접 고치게 한다(Fruition-ai#59). 그런데 Wiki 본문은 사람의 수정을 남기는 구조가 아니었다.

- concept 페이지 복구(`restore_ingest`)는 남길 기여의 JSON(개념·근거·링크)으로 템플릿을 다시 생성한다(`rebuild_concept_page`). 그래서 사람의 수정이 사라진다.
- source 페이지는 재편입 때 문서에서 본문을 다시 만든다.
- 기여 원장(`wiki_page_contributions`)과 revision(`wiki_page_versions`)은 document-svc가 소유한다. ingest·lint·복구 결과를 받을 때마다 `revision`을 하나씩 올린다. 따라서 "AI가 다시 조립하면 revision이 오른다"는 이미 충족돼 있다.
- 그래프 edge(`wiki_page_links`)는 본문이 아니라 기여 JSON의 구조화된 링크에서 나온다. 그래서 본문의 `[[slug]]`를 지워도 edge는 남는다.
- 인용은 본문에 블록 참조(`[doc:B0001]`)로 저장된다. 검색 근거(`wiki_embedding_units.block_refs`)는 본문에서 계산된다.

## Decision
- 사람의 수정은 별도 작업 하나의 **수동 기여**로 저장한다. 경로는 `ops/{operation_id}.md`(본문)와 `.json`(`artifact_type: "manual"`, `added_links`, `removed_links`)이다.
  - document-svc가 `base_revision`을 검사한 뒤 `PUT /wiki/pages/{page_id}/manual-edit`을 동기 호출한다.
  - 응답 항목으로 기여와 버전을 기록한다. 기여로 남으므로 작업 로그 되돌리기의 대상이 된다.
- **링크**: 저장할 때 이전 본문과 새 본문의 `[[slug]]` 차이만 edge에 반영한다.
  - 지운 slug는 그 대상으로 가는 edge를 종류와 무관하게 지운다.
  - 새 slug는 같은 소유 범위의 활성 페이지가 있을 때만 edge를 만든다. 종류는 source 페이지면 `source_mentions_concept`, concept 페이지면 `related_to`다.
  - 손대지 않은 링크와, 본문에 없던 기존 edge는 그대로 둔다.
  - 없는 slug는 본문에만 남긴다. slug는 정확히 일치해야 한다(제목으로 찾지 않는다).
- **인용·임베딩**: 저장된 본문의 블록 참조로 임베딩 단위를 다시 만들고 임베딩 job을 시작한다. 근거 문서 연결(`document_wiki_links`)은 바꾸지 않는다.
- **이름·요약**: 수동 저장과, 수동 기여가 있는 복구는 `title`·`summary`를 바꾸지 않는다. 이름은 `rename` API로만 바꾼다. 본문 첫 `# 제목`은 없을 수 있어 이름의 근거로 쓰지 않는다.
- **concept 복구**: 남길 기여에 수동 기여가 있으면 마지막 수동 본문을 기준으로 삼는다.
  - 그 뒤 AI 기여의 근거만 재편입과 같은 방식(`append_concept_evidence`)으로 덧붙인다. 그 전 AI 기여는 사람이 본 본문에 이미 들어 있다.
  - 링크는 수동 기여의 추가·삭제까지 적용 순서대로 재생한다(`replay_supported_links`). lint의 고아 링크 판정도 기여를 `sequence_revision` 순으로 재생한다.
  - 사람이 쓴 외부 링크는 무력화하지 않는다. AI가 덧붙인 근거의 외부 링크만 무력화한다([0028](0028-ai-output-external-link-neutralization.md)).
- **재편입**: concept 페이지는 원래부터 현재 본문 위에 근거를 덧붙이므로 수정이 남는다. source 페이지는 활성 수동 기여가 있으면 가장 최근 수동 본문을 유지하고, 링크·블록·임베딩 같은 구조만 갱신한다.

## Alternatives
- **사람의 수정을 기여가 아닌 `wiki_pages` 본문 덮어쓰기로만 저장**: 다음 복구나 재편입 때 사라진다. 되돌리기 대상도 되지 않는다.
- **임베딩만 다시 계산하고 링크는 기여 JSON 기준으로 유지**: 본문에서 지운 연결이 그래프에 남아 본문과 그래프가 어긋난다.
- **본문의 `[[slug]]` 전체를 edge의 원본으로 삼기**: 본문에 표시하지 않는 lint·cluster 관계 edge까지 지우게 된다. 차이만 반영하면 사람이 손댄 링크만 바뀐다.
- **본문 첫 `# 제목`을 이름으로 반영**: 제목 줄이 없는 본문이 있고, 본문 편집이 이름 변경을 겸하면 rename의 slug 정책과 갈린다.

## Consequences
- 수동 저장은 동기 HTTP라서 데스크톱 앱 저장 응답에 새 revision을 담을 수 있다.
- document-svc는 수동 작업을 작업 로그 operation으로 기록하고, 복구할 때 `keep_contributions`에 수동 기여를 포함해야 한다. 이 부분은 별도 이슈다.
- 수동 기여 이후 AI 기여가 같은 링크를 다시 추가하면 replay가 edge를 되살린다. AI가 근거를 새로 찾은 것으로 본다.
- source 페이지는 마지막 기여의 스냅샷으로 복구한다. 수동 수정 뒤 재편입이 있으면 그 재편입 스냅샷이 이미 수동 본문이므로, 수동 기여만 되돌려도 본문은 바뀌지 않는다.
- 남길 기여가 수동 기여뿐인 concept 페이지는 근거 문서 연결과 임베딩 단위가 비게 된다.

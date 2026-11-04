"""영구 삭제한 문서의 항목을 워크스페이스 공유 Markdown(클러스터, 편입 로그)에서 뺀다.

객체와 작업 기록의 값에 같은 변환을 쓴다. 결정적이고 반복해도 같아서, 지운 뒤에도 작업 취소의 "현재 값 = 변경 후 값" 대조가 맞고
되돌린 값에 본문이 없다. 이 문서를 인용하지 않는 부분은 한 글자도 바꾸지 않는다.
"""

from __future__ import annotations

import re

from app.modules.wiki_generation.infrastructure.meaning_cluster_artifact import _id_fragment


_CLAIM_START = re.compile(r"^\s*- (?:claim_[^:]+|ev_[^:]+):")
_CLUSTER_START = re.compile(r"^(?=## cluster:)", re.MULTILINE)
_LOG_SECTION_START = re.compile(r"^(?=## \d{4}-\d{2}-\d{2} (?:ingest|lint): )", re.MULTILINE)
_SOURCE_REFS = re.compile(r"^(\s*source_refs: \[)(.*)(\]\s*)$")


def without_document_clusters(markdown: str, document_id: str) -> str:
    """이 문서를 인용한 근거 줄과 승격 출처를 지운다.

    근거가 있던 클러스터에서 근거가 모두 빠지면 대표명·승격 사유와 함께 지운다. 근거 없이 승격 표시만 있는 클러스터는
    다른 문서의 출처가 남아 있으면 그 문서의 것이라 이 문서의 id만 빼고, 출처가 비면 지운다.
    """
    if document_id not in markdown and f"claim_{_id_fragment(document_id)}_" not in markdown:
        return markdown
    return "".join(_without_document(part, document_id) for part in _CLUSTER_START.split(markdown))


def without_document_log(markdown: str, document_id: str) -> str:
    """이 문서의 편입 항목을 지운다. 린트 항목과 다른 문서의 항목은 그대로다."""
    heading = re.compile(rf"^## \d{{4}}-\d{{2}}-\d{{2}} ingest: {re.escape(document_id)}\s*$", re.MULTILINE)
    return "".join(part for part in _LOG_SECTION_START.split(markdown) if not heading.match(part))


def _without_document(section: str, document_id: str) -> str:
    if not section.startswith("## cluster:"):
        return section
    citation = re.compile(rf"(?<![A-Za-z0-9_.-]){re.escape(document_id)}:")
    own_claim = f"claim_{_id_fragment(document_id)}_"
    kept: list[str] = []
    remaining_claims = 0
    removed_claims = False
    source_removed = False
    sources_left = False
    lines = section.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        if not _CLAIM_START.match(line):
            refs = _without_source(line, document_id)
            if refs != line:
                source_removed = True
                sources_left = bool(_SOURCE_REFS.match(refs).group(2).strip())
            kept.append(refs)
            index += 1
            continue
        end = _claim_end(lines, index)
        claim = "\n".join(lines[index:end])
        if citation.search(claim) or own_claim in line:
            removed_claims = True
        else:
            kept.extend(lines[index:end])
            remaining_claims += 1
        index = end
    if not removed_claims and not source_removed:
        return section
    return "\n".join(kept) if remaining_claims or (not removed_claims and sources_left) else ""


def _claim_end(lines: list[str], start: int) -> int:
    """근거 원문에 줄바꿈이 있으면 인용은 마지막 줄에 있다. 다음 근거나 제목 앞까지가 한 근거다. 끝의 빈 줄은 근거가 아니다."""
    end = start + 1
    while end < len(lines) and not (_CLAIM_START.match(lines[end]) or lines[end].startswith("#")):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return end


def _without_source(line: str, document_id: str) -> str:
    match = _SOURCE_REFS.match(line)
    if not match:
        return line
    sources = [part.strip() for part in match.group(2).split(",") if part.strip()]
    if document_id not in sources:
        return line
    return match.group(1) + ", ".join(source for source in sources if source != document_id) + match.group(3)

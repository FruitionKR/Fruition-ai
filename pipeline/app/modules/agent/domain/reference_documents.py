"""Agent turn에 사용자가 첨부한 참고 문서를 어디까지 읽어 어떻게 근거로 쓸지 정한다.

본문은 Skill 참조 문서와 같은 경로로 읽고 같은 상한을 쓴다. Skill 작성은 상한을 넘으면 거절하지만,
일반 turn은 max_chars로 앞부분만 읽어 싣고 결과 메시지로 알린다.
"""

import re
import unicodedata
from dataclasses import dataclass, replace

from app.modules.agent.domain.folder_sources import TRUNCATED_SUFFIX
from app.modules.query.domain.entities import EvidenceSnippet, SourceReference
from app.modules.skill.domain.entities import SkillAuthoringReference
from app.modules.skill.domain.policy import MAX_REFERENCE_CHARS, MAX_TOTAL_REFERENCE_CHARS

# 답변 근거 조각 크기. 답변 입력 formatter가 근거 하나를 자르는 길이(max_paragraph_chars)와 같다.
MAX_REFERENCE_EVIDENCE_CHARS = 900
# 답변 갈래(chat_answer·conversation_reply)가 읽는 첨부 합계. 답변 입력은 근거 8개(7,200자)를 전제로 하고
# 재작성·평가로 반복되므로 합계 상한 대신 열린 문서를 LLM에 싣는 상한(30,000자)에 맞춘다.
MAX_REFERENCE_ANSWER_CHARS = 30_000
MAX_REFERENCE_TITLE_CHARS = 200
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


class ReferenceDocumentUnavailableError(Exception):
    """첨부한 문서를 근거로 쓸 수 없다. message는 사용자에게 보여 줄 문장이다."""


@dataclass(frozen=True)
class SelectedReferenceDocuments:
    documents: tuple[SkillAuthoringReference, ...] = ()
    # 길어서 document-svc가 거절한 문서 수. max_chars를 모르는 구 document-svc만 413을 준다.
    too_long_count: int = 0
    # 첨부가 아니라 열린 문서를 실었을 때의 그 문서 id다. 잘렸다는 안내에서 첨부와 구분한다.
    open_document_id: str | None = None


def clean_reference_title(title: str) -> str:
    """제목을 한 줄로 만든다. 줄바꿈으로 프롬프트 구조를 흉내 내거나 제어 문자로 글자를 숨기지 못하게 한다."""
    visible = "".join(
        " " if character.isspace() else character
        for character in title
        if character.isspace() or unicodedata.category(character) not in {"Cc", "Cf"}
    )
    return " ".join(visible.split())[:MAX_REFERENCE_TITLE_CHARS].rstrip()


def reference_char_limit(remaining_chars: int, documents_left: int) -> int:
    """다음 문서에 쓸 글자 수. 남은 합계를 남은 문서끼리 나눠, 앞 문서가 짧으면 남은 양이 뒤로 넘어간다."""
    return min(MAX_REFERENCE_CHARS, remaining_chars // documents_left)


def clip_reference_document(document: SkillAuthoringReference, limit: int) -> SkillAuthoringReference:
    """limit을 넘지 않게 자른다. document-svc가 자르지 않는 원본 문서(본문이 AI 저장소에 있다)도 여기서 자른다."""
    if not document.truncated and len(document.markdown) <= limit:
        return document
    body_limit = limit - len(TRUNCATED_SUFFIX)
    return replace(
        document,
        markdown=document.markdown[:body_limit].rstrip() + TRUNCATED_SUFFIX,
        source_blocks=_blocks_within(document.source_blocks, body_limit),
        truncated=True,
    )


def reference_document_notice(selected: SelectedReferenceDocuments) -> str | None:
    """첨부나 열린 문서의 일부를 쓰지 못했을 때 결과 메시지에 덧붙일 안내."""
    sentences = []
    if selected.too_long_count:
        sentences.append(
            f"첨부한 문서 중 {selected.too_long_count}개는 너무 길어 읽지 못했어요."
            " 이 문서를 근거로 쓰려면 위키에 올린 뒤 위키 내용을 기준으로 다시 요청해 주세요."
        )
    truncated = [document for document in selected.documents if document.truncated]
    if any(document.id == selected.open_document_id for document in truncated):
        sentences.append("열린 문서가 길어 앞부분만 읽었어요. 뒷부분은 결과에 반영되지 않았어요.")
    if any(document.id != selected.open_document_id for document in truncated):
        sentences.append("첨부한 문서가 길어 앞부분만 읽었어요. 뒷부분은 결과에 반영되지 않았어요.")
    return " ".join(sentences) or None


def reference_documents_context(selected: SelectedReferenceDocuments) -> list[dict[str, str]]:
    """문서 생성·편집·대화 답변 payload의 reference_context.reference_documents 값."""
    return [
        {"document_id": document.id, **({"title": document.name} if document.name else {}), "markdown": document.markdown}
        for document in selected.documents
    ]


def reference_evidence_snippets(selected: SelectedReferenceDocuments) -> tuple[EvidenceSnippet, ...]:
    """질의 답변에 고정 근거로 넣을 조각. 요약처럼 문서 전체가 필요한 질문이 있어 관련도로 고르지 않는다."""
    snippets: list[EvidenceSnippet] = []
    for document in selected.documents:
        for block_ids, text in _document_chunks(document):
            snippets.append(
                EvidenceSnippet(
                    rank=len(snippets) + 1,
                    source_document_id=document.id,
                    source_block_ids=block_ids,
                    text=text,
                    source_refs=[SourceReference(document.id, block_id) for block_id in block_ids],
                    reference_chunk=len(snippets),
                    reference_title=document.name or None,
                    reference_is_open_document=document.id == selected.open_document_id,
                )
            )
    return tuple(snippets)


def _blocks_within(blocks: tuple[tuple[str, str], ...], limit: int) -> tuple[tuple[str, str], ...]:
    """잘린 본문 안에 드는 블록. 넘치는 블록은 앞부분만 남긴다."""
    kept: list[tuple[str, str]] = []
    remaining = limit
    for block_id, text in blocks:
        if remaining <= 0:
            break
        kept.append((block_id, text[:remaining]))
        remaining -= len(text)
    return tuple(kept)


def _document_chunks(document: SkillAuthoringReference) -> list[tuple[list[str], str]]:
    """문단(원본 문서는 블록)을 이어 붙여 MAX_REFERENCE_EVIDENCE_CHARS 이하 조각으로 만든다."""
    segments = document.source_blocks or tuple(("", part) for part in _PARAGRAPH_BREAK.split(document.markdown))
    chunks: list[tuple[list[str], str]] = []
    block_ids: list[str] = []
    text = ""
    for block_id, segment in segments:
        for piece in _bounded_pieces(segment.strip()):
            if text and len(text) + 2 + len(piece) > MAX_REFERENCE_EVIDENCE_CHARS:
                chunks.append((block_ids, text))
                block_ids, text = [], ""
            text = f"{text}\n\n{piece}" if text else piece
            if block_id and block_id not in block_ids:
                block_ids = [*block_ids, block_id]
    if text:
        chunks.append((block_ids, text))
    return chunks


def _bounded_pieces(segment: str) -> list[str]:
    return [
        segment[start : start + MAX_REFERENCE_EVIDENCE_CHARS]
        for start in range(0, len(segment), MAX_REFERENCE_EVIDENCE_CHARS)
    ]

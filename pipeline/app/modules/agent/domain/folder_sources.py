"""원문이 필요한 생성 Skill을 폴더에 실행할 때 읽을 폴더 문서를 고른다.

폴더 바로 아래(하위 폴더 제외) Markdown 문서 중 사용자가 읽을 수 있는 문서만 document-svc가 돌려준다.
여기서는 최근 수정순으로 최대 MAX_FOLDER_SOURCE_DOCUMENTS개, 본문 합계 MAX_FOLDER_SOURCE_CHARS자까지만 싣는다.
"""

from dataclasses import dataclass

MAX_FOLDER_SOURCE_DOCUMENTS = 20
# 새 문서 생성 payload에 싣는 원문 합계 상한. 위키 근거 질문 생성의 원문 상한(MAX_QUESTION_SOURCE_CHARS)과 같다.
MAX_FOLDER_SOURCE_CHARS = 60_000
TRUNCATED_SUFFIX = "\n…(이하 생략)"


@dataclass(frozen=True)
class FolderSourceDocument:
    document_id: str
    title: str
    markdown: str
    updated_at: str | None = None


@dataclass(frozen=True)
class FolderSourceListing:
    """document-svc 응답. total_documents는 폴더 바로 아래 읽을 수 있는 Markdown 문서 전체 수다."""

    total_documents: int
    documents: tuple[FolderSourceDocument, ...]


@dataclass(frozen=True)
class SelectedFolderSources:
    documents: tuple[FolderSourceDocument, ...]
    total_documents: int
    # 본문이 상한에 걸려 뒷부분을 자른 문서 id.
    truncated_document_ids: tuple[str, ...] = ()

    @property
    def read_documents(self) -> int:
        return len(self.documents)

    @property
    def is_partial(self) -> bool:
        return self.read_documents < self.total_documents or bool(self.truncated_document_ids)


class FolderSourceUnavailableError(Exception):
    """폴더를 읽을 수 없다(권한 없음·없는 폴더·서비스 오류). message는 사용자에게 보여 줄 문장이다."""


def select_folder_sources(
    listing: FolderSourceListing,
    *,
    max_documents: int = MAX_FOLDER_SOURCE_DOCUMENTS,
    max_total_chars: int = MAX_FOLDER_SOURCE_CHARS,
) -> SelectedFolderSources:
    """최근 수정순으로 문서를 담는다. 합계 상한을 넘는 문서에서 멈춘다.

    첫 문서 하나만으로 상한을 넘으면 그 문서의 앞부분만 담아 원문이 비지 않게 한다.
    """
    candidates = sorted(
        (document for document in listing.documents if document.markdown.strip()),
        key=lambda document: document.updated_at or "",
        reverse=True,
    )[:max_documents]
    selected: list[FolderSourceDocument] = []
    truncated: list[str] = []
    used_chars = 0
    for document in candidates:
        remaining = max_total_chars - used_chars
        if len(document.markdown) <= remaining:
            selected.append(document)
            used_chars += len(document.markdown)
            continue
        if not selected:
            clipped = document.markdown[:remaining].rstrip() + TRUNCATED_SUFFIX
            selected.append(FolderSourceDocument(document.document_id, document.title, clipped, document.updated_at))
            truncated.append(document.document_id)
        break
    total = max(listing.total_documents, len(listing.documents))
    return SelectedFolderSources(
        documents=tuple(selected),
        total_documents=total,
        truncated_document_ids=tuple(truncated),
    )


def folder_source_notice(sources: SelectedFolderSources) -> str | None:
    """일부만 읽었을 때 결과 메시지에 덧붙일 안내."""
    if not sources.is_partial:
        return None
    if sources.read_documents < sources.total_documents:
        return (
            f"폴더 문서 {sources.total_documents}개 중 최근 수정한 {sources.read_documents}개만 읽었어요."
            " 나머지 문서는 초안에 반영되지 않았어요."
        )
    return "폴더 문서가 길어 일부 내용만 읽었어요. 뒷부분은 초안에 반영되지 않았어요."

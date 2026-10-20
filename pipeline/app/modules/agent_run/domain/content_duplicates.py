"""폴더 정리 계획을 검토할 때 본문이 거의 같은 문서를 결정적으로 찾는다.

본문을 정규화(NFC·대소문자·공백·문장부호 제거)한 뒤 글자 3-gram 집합의 Jaccard 유사도로 비교한다.
LLM 판단에 기대지 않으므로 같은 입력이면 늘 같은 결과가 나온다.
"""

from dataclasses import dataclass
import re
import unicodedata

# 정규화한 본문 3-gram Jaccard가 이 값 이상이면 내용이 거의 같은 문서로 본다.
CONTENT_DUPLICATE_THRESHOLD = 0.85
# 정규화한 본문이 이보다 짧으면 비교하지 않는다. 빈 문서·제목만 있는 문서끼리 중복으로 잡지 않게 한다.
MIN_COMPARABLE_CHARS = 20
_SHINGLE_SIZE = 3
_NON_WORD = re.compile(r"[\W_]+")
# 계획 summary에 덧붙이는 안내. 프론트가 문장을 그대로 찾아 따로 보여 줄 수 있게 형식을 고정한다.
UNPLANNED_DUPLICATE_NOTE = "계획에 없는 문서 중 내용이 거의 같은 문서가 있어요: {pairs}."
UNCHECKED_DOCUMENTS_NOTE = "문서 {count}개는 내용 중복을 확인하지 못했어요."


@dataclass(frozen=True)
class ContentDuplicate:
    document_id: str
    document_name: str
    original_id: str
    original_name: str


@dataclass(frozen=True)
class ContentReview:
    """duplicates는 중복 문서 id별 원본, unchecked_documents는 본문을 읽지 못해 비교하지 못한 문서 수다."""

    duplicates: dict[str, ContentDuplicate]
    unchecked_documents: int = 0


def normalize_content(markdown: str) -> str:
    """띄어쓰기·문장부호·Markdown 기호 차이는 무시하고 글자와 숫자만 남긴다."""
    return _NON_WORD.sub("", unicodedata.normalize("NFC", markdown).casefold())


def find_content_duplicates(
    documents: list[dict[str, object]],
    contents: dict[str, str],
) -> dict[str, ContentDuplicate]:
    """본문이 거의 같은 문서 묶음마다 가장 먼저 만든 문서를 원본으로 두고, 나머지를 중복으로 돌려준다.

    documents는 hierarchy 문서 항목(id·name·created_at 등), contents는 문서 id별 Markdown 본문이다.
    본문이 없는 문서는 비교하지 않는다.
    """
    ordered = sorted(
        (item for item in documents if str(item.get("id")) in contents),
        key=lambda item: (_created_key(item), str(item.get("name") or ""), str(item.get("id"))),
    )
    normalized = {str(item["id"]): normalize_content(contents[str(item["id"])]) for item in ordered}
    candidates = [item for item in ordered if len(normalized[str(item["id"])]) >= MIN_COMPARABLE_CHARS]
    shingles = {str(item["id"]): _shingles(normalized[str(item["id"])]) for item in candidates}
    parent: dict[str, str] = {str(item["id"]): str(item["id"]) for item in candidates}
    position = {str(item["id"]): index for index, item in enumerate(candidates)}

    def root(document_id: str) -> str:
        while parent[document_id] != document_id:
            parent[document_id] = parent[parent[document_id]]
            document_id = parent[document_id]
        return document_id

    for index, item in enumerate(candidates):
        left = str(item["id"])
        for other in candidates[index + 1:]:
            right = str(other["id"])
            if root(left) == root(right):
                continue
            if normalized[left] == normalized[right] or _jaccard(shingles[left], shingles[right]) >= CONTENT_DUPLICATE_THRESHOLD:
                # 정렬 순서상 앞선(먼저 만든) 문서가 묶음의 원본이 되도록 뒤 문서를 앞 문서 아래에 붙인다.
                first, second = sorted((root(left), root(right)), key=position.__getitem__)
                parent[second] = first

    names = {str(item["id"]): str(item.get("name") or "") for item in candidates}
    duplicates: dict[str, ContentDuplicate] = {}
    for item in candidates:
        document_id = str(item["id"])
        original_id = root(document_id)
        if original_id != document_id:
            duplicates[document_id] = ContentDuplicate(document_id, names[document_id], original_id, names[original_id])
    return duplicates


def content_review_summary(summary: str, review: ContentReview, planned_document_ids: set[str]) -> str:
    """작업이 없어 승인 화면 줄로 보여 줄 수 없는 중복과, 확인하지 못한 문서 수를 summary 끝에 덧붙인다."""
    notes: list[str] = []
    unplanned = [
        f"'{duplicate.document_name}'와(과) '{duplicate.original_name}'"
        for duplicate in review.duplicates.values()
        if duplicate.document_id not in planned_document_ids
    ]
    if unplanned:
        notes.append(UNPLANNED_DUPLICATE_NOTE.format(pairs=", ".join(unplanned)))
    if review.unchecked_documents > 0:
        notes.append(UNCHECKED_DOCUMENTS_NOTE.format(count=review.unchecked_documents))
    return " ".join([summary.strip(), *notes]).strip()


def _created_key(item: dict[str, object]) -> str:
    for key in ("created_at", "uploaded_at", "updated_at"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return "~"


def _shingles(text: str) -> frozenset[str]:
    if len(text) <= _SHINGLE_SIZE:
        return frozenset({text})
    return frozenset(text[index:index + _SHINGLE_SIZE] for index in range(len(text) - _SHINGLE_SIZE + 1))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    union = len(left | right)
    return len(left & right) / union if union else 0.0

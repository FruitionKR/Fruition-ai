"""폴더 정리 계획을 검토할 때 본문이 거의 같은 문서를 결정적으로 찾는다.

본문을 정규화(NFC·대소문자·공백·문장부호 제거)한 뒤 글자 3-gram 집합의 Jaccard 유사도로 비교한다.
LLM 판단에 기대지 않으므로 같은 입력이면 늘 같은 결과가 나온다.
"""

from collections.abc import Callable
from dataclasses import dataclass
import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

# 정규화한 본문 3-gram Jaccard가 이 값 이상이면 내용이 거의 같은 문서로 본다.
CONTENT_DUPLICATE_THRESHOLD = 0.85
# 정규화한 본문이 이보다 짧으면 비교하지 않는다. 빈 문서·제목만 있는 문서끼리 중복으로 잡지 않게 한다.
MIN_COMPARABLE_CHARS = 20
_SHINGLE_SIZE = 3
# 비교 비용 상한. 넘으면 남은 비교는 하지 않고, 다른 문서와 끝까지 비교하지 못한 문서를 확인하지 못한 문서로 센다.
# 둘 다 입력 크기로만 정해지는 결정적 상한이라 같은 입력이면 늘 같은 결과가 나온다(벽시계 시간 예산은 쓰지 않는다).
# - 3-gram 집합에 담을 3-gram 수 합계(정규화 길이 - 2로 센 상한). 메모리를 묶는다. 무작위 본문 최악에서 약 350MB.
MAX_TOTAL_SHINGLES = 2_000_000
# - Jaccard 계산에서 훑는 3-gram 수 합계(쌍마다 작은 쪽 집합 크기). 시간을 묶는다. 무작위 본문 최악에서 약 3초.
MAX_COMPARISON_WORK = 150_000_000
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
    """duplicates는 중복 문서 id별 원본, unchecked_documents는 본문을 읽지 못했거나 비교 상한에 걸려 다른 문서와
    끝까지 비교하지 못한 문서 수다."""

    duplicates: dict[str, ContentDuplicate]
    unchecked_documents: int = 0


def normalize_content(markdown: str) -> str:
    """띄어쓰기·문장부호·Markdown 기호 차이는 무시하고 글자와 숫자만 남긴다."""
    return _NON_WORD.sub("", unicodedata.normalize("NFC", markdown).casefold())


def find_content_duplicates(
    documents: list[dict[str, object]],
    contents: dict[str, str],
) -> ContentReview:
    """본문이 거의 같은 문서 묶음마다 가장 먼저 만든 문서를 원본으로 두고, 나머지를 중복으로 돌려준다.

    documents는 hierarchy 문서 항목(id·name·created_at 등), contents는 문서 id별 Markdown 본문이다.
    본문이 없는 문서는 비교하지 않고 세지도 않는다(읽기 실패는 호출부가 센다). 비교 상한에 걸리면 그때까지 찾은
    중복과 함께, 다른 문서와 끝까지 비교하지 못한 문서 수를 unchecked_documents로 돌려준다.
    """
    ordered = sorted(
        (item for item in documents if str(item.get("id")) in contents),
        key=lambda item: (_created_key(item), str(item.get("name") or ""), str(item.get("id"))),
    )
    normalized = {str(item["id"]): normalize_content(contents[str(item["id"])]) for item in ordered}
    candidates = [item for item in ordered if len(normalized[str(item["id"])]) >= MIN_COMPARABLE_CHARS]
    parent: dict[str, str] = {str(item["id"]): str(item["id"]) for item in candidates}
    position = {str(item["id"]): index for index, item in enumerate(candidates)}

    def root(document_id: str) -> str:
        while parent[document_id] != document_id:
            parent[document_id] = parent[parent[document_id]]
            document_id = parent[document_id]
        return document_id

    def union(left: str, right: str) -> None:
        # 정렬 순서상 앞선(먼저 만든) 문서가 묶음의 원본이 되도록 뒤 묶음을 앞 묶음 아래에 붙인다.
        first, second = sorted((root(left), root(right)), key=position.__getitem__)
        if first != second:
            parent[second] = first

    # 정규화 본문이 완전히 같은 문서는 비교 없이 먼저 묶고, 묶음마다 대표 하나만 유사도 비교에 쓴다.
    representatives: dict[str, str] = {}
    group_sizes: dict[str, int] = {}
    for item in candidates:
        document_id = str(item["id"])
        representative = representatives.setdefault(normalized[document_id], document_id)
        group_sizes[representative] = group_sizes.get(representative, 0) + 1
        union(representative, document_id)
    # 짧은 본문부터 3-gram 수 상한 안에 드는 만큼만 집합을 만든다. 정렬이 길이 순이라 한 번 넘으면 뒤도 모두 넘는다.
    by_length = sorted(representatives.items(), key=lambda entry: (len(entry[0]), position[entry[1]]))
    shingles: dict[str, frozenset[str]] = {}
    shingle_total = 0
    unchecked_ids: list[str] = []
    for index, (text, document_id) in enumerate(by_length):
        shingle_total += max(len(text) - _SHINGLE_SIZE + 1, 1)
        if shingle_total > MAX_TOTAL_SHINGLES:
            unchecked_ids = [skipped_id for _text, skipped_id in by_length[index:]]
            break
        shingles[document_id] = _shingles(text)
    # 3-gram 수가 작은 순으로 비교한다. |A| <= |B|이면 Jaccard <= |A|/|B|라서, 비가 기준보다 작아지면
    # 그 뒤 문서는 모두 기준을 넘을 수 없어 안쪽 반복을 끝낸다.
    ordered_ids = sorted(shingles, key=lambda document_id: (len(shingles[document_id]), position[document_id]))
    unchecked_ids += _compare(ordered_ids, shingles, root, union)
    duplicates = _duplicates(candidates, root)
    if not unchecked_ids:
        return ContentReview(duplicates)
    # 비교가 남은 대표 문서의 묶음(완전 일치 문서 포함)을 확인하지 못한 문서로 센다.
    unchecked = sum(group_sizes[document_id] for document_id in unchecked_ids)
    logger.warning(
        "본문 중복 비교가 상한을 넘어 일부 비교를 생략함: documents=%s unchecked=%s shingles=%s",
        len(candidates),
        unchecked,
        shingle_total,
    )
    return ContentReview(duplicates, unchecked)


def _compare(
    ordered_ids: list[str],
    shingles: dict[str, frozenset[str]],
    root: Callable[[str], str],
    union: Callable[[str, str], None],
) -> list[str]:
    """유사한 문서를 묶고, 비교량 상한에 걸리면 더 큰 문서와의 비교가 남은 문서(left부터 뒤)를 돌려준다."""
    work = 0
    for index, left in enumerate(ordered_ids):
        left_shingles = shingles[left]
        for right in ordered_ids[index + 1:]:
            right_shingles = shingles[right]
            if len(left_shingles) < CONTENT_DUPLICATE_THRESHOLD * len(right_shingles):
                break
            if root(left) == root(right):
                continue
            work += len(left_shingles)
            if work > MAX_COMPARISON_WORK:
                return ordered_ids[index:]
            if _jaccard(left_shingles, right_shingles) >= CONTENT_DUPLICATE_THRESHOLD:
                union(left, right)
    return []


def _duplicates(candidates: list[dict[str, object]], root: Callable[[str], str]) -> dict[str, ContentDuplicate]:
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
    intersection = len(left & right)
    union = len(left) + len(right) - intersection
    return intersection / union if union else 0.0

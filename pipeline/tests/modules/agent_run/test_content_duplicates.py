from dataclasses import dataclass, replace
from unittest.mock import MagicMock, patch

from app.modules.agent.domain.folder_sources import FolderSourceUnavailableError
from app.modules.agent_run.domain.content_duplicates import (
    ContentDuplicate,
    ContentReview,
    content_review_summary,
    find_content_duplicates,
)
from app.modules.agent_run.domain.plan import AgentPlan, AgentPlanOperation
from app.modules.agent_run.domain.plan_review import review_content_duplicates
from app.modules.agent_run.infrastructure.agent_worker import AgentWorker
from tests.modules.agent_run.test_agent_worker import _executing_context

MEETING = "# 주간 회의 10/19\n\n- 참석: 김철수, 이영희\n- 안건: 3분기 실적 점검, 신규 채용 계획\n- 결정: 채용 공고는 다음 주 월요일에 올린다.\n"
MEETING_SPACED = "#  주간회의 10/19\n\n* 참석 : 김철수, 이영희\n* 안건 : 3분기 실적 점검,  신규 채용 계획\n* 결정 : 채용 공고는 다음 주 월요일에 올린다 (확정)\n"
BUDGET = "# 예산 계획\n\n- 마케팅 예산은 전년 대비 10% 늘린다.\n- 개발 인력 충원 예산을 따로 잡는다.\n"


def _doc(document_id: str, name: str, created_at: str, parent_id: str | None = "folder-1") -> dict[str, object]:
    return {"id": document_id, "type": "document", "name": name, "created_at": created_at, "parent_id": parent_id, "version": 1}


def _rename(sequence: int, document_id: str, depends_on: tuple[str, ...] = ()) -> AgentPlanOperation:
    return AgentPlanOperation(
        id=f"plan-1-op-{sequence}", sequence=sequence, tool_name="rename_document", target_type="document",
        target_id=document_id, base_version=1, source_parent_id=None, destination_parent_id=None,
        arguments={"document_id": document_id, "display_name": f"새 이름 {sequence}", "base_version": 1},
        reason="이름 형식 맞춤", depends_on=depends_on,
    )


def _plan(*operations: AgentPlanOperation) -> AgentPlan:
    return AgentPlan(id="plan-1", run_id="run-1", version=1, summary="이름을 바꿉니다.", operation_hash="h" * 64,
                     operations=operations)


def test_nearly_identical_documents_are_grouped_under_the_earliest_one() -> None:
    documents = [
        _doc("doc-b", "주간 회의 1019.md", "2026-10-19T02:00:00Z"),
        _doc("doc-a", "주간회의_1019.md", "2026-10-19T01:00:00Z"),
        _doc("doc-c", "예산.md", "2026-10-18T01:00:00Z"),
    ]
    contents = {"doc-a": MEETING, "doc-b": MEETING_SPACED, "doc-c": BUDGET}

    duplicates = find_content_duplicates(documents, contents)

    assert duplicates == {"doc-b": ContentDuplicate("doc-b", "주간 회의 1019.md", "doc-a", "주간회의_1019.md")}


def test_short_or_unread_documents_are_not_compared() -> None:
    documents = [_doc("doc-a", "빈 문서", "1"), _doc("doc-b", "빈 문서 2", "2"), _doc("doc-c", "본문 없음", "3")]
    assert find_content_duplicates(documents, {"doc-a": "# 메모", "doc-b": "# 메모\n"}) == {}


def test_content_duplicate_unselects_operation_and_dependents_but_keeps_name_warning() -> None:
    named = replace(_rename(3, "doc-d"), warning={"type": "duplicate_name", "message": "이름 중복"}, suggested_selected=False)
    plan = _plan(_rename(1, "doc-a"), _rename(2, "doc-b"), named, _rename(4, "doc-x", depends_on=("plan-1-op-2",)))
    duplicates = {
        "doc-b": ContentDuplicate("doc-b", "주간 회의 1019.md", "doc-a", "주간회의_1019.md"),
        "doc-d": ContentDuplicate("doc-d", "d", "doc-a", "주간회의_1019.md"),
    }

    reviewed = review_content_duplicates(plan, duplicates)

    original, duplicate, name_dup, dependent = reviewed.operations
    assert original.warning is None and original.suggested_selected
    assert duplicate.warning == {"type": "duplicate_content", "message": "'주간회의_1019.md'와(과) 내용이 거의 같아 중복일 수 있어요."}
    assert not duplicate.suggested_selected
    assert name_dup.warning["type"] == "duplicate_name"
    assert dependent.warning["type"] == "depends_on_unselected" and not dependent.suggested_selected
    assert reviewed.operation_hash == plan.operation_hash


def test_summary_mentions_unplanned_duplicates_and_unchecked_documents() -> None:
    review = ContentReview({"doc-b": ContentDuplicate("doc-b", "B.md", "doc-a", "A.md")}, unchecked_documents=2)
    assert content_review_summary("이름을 바꿉니다.", review, set()) == (
        "이름을 바꿉니다. 계획에 없는 문서 중 내용이 거의 같은 문서가 있어요: 'B.md'와(과) 'A.md'."
        " 문서 2개는 내용 중복을 확인하지 못했어요."
    )
    assert content_review_summary("이름을 바꿉니다.", replace(review, unchecked_documents=0), {"doc-b"}) == "이름을 바꿉니다."


@dataclass(frozen=True)
class _Document:
    document_id: str
    markdown: str


@dataclass(frozen=True)
class _Listing:
    total_documents: int
    documents: tuple[_Document, ...]


def _plan_with(worker_reader: MagicMock, hierarchy: list[dict[str, object]], plan: AgentPlan, action: str = "folder_organize"):
    repository = MagicMock()
    context = _executing_context()
    repository.load_context.return_value = replace(
        context, run=replace(context.run, status="queued", skill_version_id=None, action=action),
    )
    repository.next_plan_version.return_value = 1
    plans, gateway, generator = MagicMock(), MagicMock(), MagicMock()
    generator.generate.return_value = plan
    worker = AgentWorker(repository, plans, gateway, generator, folder_content_reader=worker_reader)
    with (
        patch.object(worker, "_inspect_hierarchy", return_value=hierarchy),
        patch.object(worker, "_load_content_artifacts", return_value=()),
    ):
        worker._plan_run("run-1")
    return generator.generate.call_args.kwargs, plans.save_plan.call_args.args[1]


def test_folder_organize_planning_marks_content_duplicates_from_folder_contents() -> None:
    hierarchy = [
        {"id": "folder-1", "type": "folder", "name": "회의", "parent_id": None},
        _doc("doc-a", "주간회의_1019.md", "2026-10-19T01:00:00Z"),
        _doc("doc-b", "주간 회의 1019.md", "2026-10-19T02:00:00Z"),
        _doc("doc-c", "예산.md", "2026-10-18T01:00:00Z"),
    ]
    reader = MagicMock()
    reader.read.return_value = _Listing(3, (_Document("doc-a", MEETING), _Document("doc-b", MEETING_SPACED), _Document("doc-c", BUDGET)))

    kwargs, saved = _plan_with(reader, hierarchy, _plan(_rename(1, "doc-a"), _rename(2, "doc-b"), _rename(3, "doc-c")))

    reader.read.assert_called_once_with(workspace_id="workspace-1", user_id="user-1", folder_id="folder-1", max_documents=50)
    planner_docs = {item["id"]: item for item in kwargs["hierarchy"]}
    assert planner_docs["doc-b"]["content_duplicate_of"] == "doc-a"
    assert "content_duplicate_of" not in planner_docs["doc-a"]
    assert [operation.suggested_selected for operation in saved.operations] == [True, False, True]
    assert saved.operations[1].warning["type"] == "duplicate_content"
    assert saved.summary == "이름을 바꿉니다."


def test_folder_read_failure_and_limits_keep_planning_with_notice() -> None:
    hierarchy = [
        {"id": "folder-1", "type": "folder", "name": "회의", "parent_id": None},
        {"id": "folder-2", "type": "folder", "name": "기타", "parent_id": None},
        _doc("doc-a", "a.md", "1"),
        _doc("doc-b", "b.md", "2"),
        _doc("doc-c", "c.md", "3", parent_id="folder-2"),
        _doc("doc-root", "root.md", "4", parent_id=None),
    ]
    reader = MagicMock()

    def read(*, folder_id: str, **_kwargs: object) -> _Listing:
        if folder_id == "folder-2":
            raise FolderSourceUnavailableError("폴더 문서를 읽지 못했어요.")
        # 폴더 문서 3개 중 2개만 돌려주고, 그중 하나는 상한보다 길다.
        return _Listing(3, (_Document("doc-a", MEETING), _Document("doc-b", "가" * 50_001)))

    reader.read.side_effect = read

    _kwargs, saved = _plan_with(reader, hierarchy, _plan(_rename(1, "doc-a")))

    # 최상위 1 + folder-2 읽기 실패 1 + 돌려받지 못한 1 + 너무 긴 1
    assert saved.summary == "이름을 바꿉니다. 문서 4개는 내용 중복을 확인하지 못했어요."
    assert saved.operations[0].suggested_selected


def test_content_check_runs_only_for_folder_organize() -> None:
    reader = MagicMock()
    hierarchy = [_doc("doc-a", "a.md", "1"), _doc("doc-b", "b.md", "2")]
    _kwargs, saved = _plan_with(reader, hierarchy, _plan(_rename(1, "doc-a")), action="workspace_workflow")
    reader.read.assert_not_called()
    assert saved.summary == "이름을 바꿉니다."


def _all_pairs_duplicates(documents: list[dict[str, object]], contents: dict[str, str]) -> dict[str, ContentDuplicate]:
    """최적화 전 방식(모든 쌍을 합집합으로 비교)을 그대로 옮긴 기준 구현."""
    from app.modules.agent_run.domain import content_duplicates as module

    ordered = sorted(
        (item for item in documents if str(item.get("id")) in contents),
        key=lambda item: (module._created_key(item), str(item.get("name") or ""), str(item.get("id"))),
    )
    normalized = {str(item["id"]): module.normalize_content(contents[str(item["id"])]) for item in ordered}
    candidates = [item for item in ordered if len(normalized[str(item["id"])]) >= module.MIN_COMPARABLE_CHARS]
    shingles = {str(item["id"]): module._shingles(normalized[str(item["id"])]) for item in candidates}
    parent = {str(item["id"]): str(item["id"]) for item in candidates}
    position = {str(item["id"]): index for index, item in enumerate(candidates)}

    def root(document_id: str) -> str:
        while parent[document_id] != document_id:
            document_id = parent[document_id]
        return document_id

    for index, item in enumerate(candidates):
        left = str(item["id"])
        for other in candidates[index + 1:]:
            right = str(other["id"])
            if root(left) == root(right):
                continue
            union = len(shingles[left] | shingles[right])
            jaccard = len(shingles[left] & shingles[right]) / union if union else 0.0
            if normalized[left] == normalized[right] or jaccard >= module.CONTENT_DUPLICATE_THRESHOLD:
                first, second = sorted((root(left), root(right)), key=position.__getitem__)
                parent[second] = first
    names = {str(item["id"]): str(item.get("name") or "") for item in candidates}
    return {
        str(item["id"]): ContentDuplicate(str(item["id"]), names[str(item["id"])], root(str(item["id"])), names[root(str(item["id"]))])
        for item in candidates
        if root(str(item["id"])) != str(item["id"])
    }


def test_optimized_detection_matches_all_pairs_comparison() -> None:
    import random

    rng = random.Random(20261021)
    words = ["회의", "예산", "채용", "실적", "계획", "점검", "마케팅", "개발", "공고", "결정", "alpha", "beta", "42"]
    for _ in range(40):
        bases = [" ".join(rng.choice(words) for _ in range(rng.randint(4, 40))) for _ in range(4)]
        documents: list[dict[str, object]] = []
        contents: dict[str, str] = {}
        for index in range(rng.randint(2, 30)):
            base = rng.choice(bases).split()
            for _ in range(rng.randint(0, 4)):  # 단어 몇 개를 바꾸거나 지워 거의 같은·조금 다른 본문을 만든다.
                position = rng.randrange(len(base))
                if rng.random() < 0.5:
                    base[position] = rng.choice(words)
                elif len(base) > 1:
                    del base[position]
            document_id = f"doc-{index}"
            documents.append(_doc(document_id, f"{index}.md", f"2026-10-{rng.randint(10, 28)}T00:00:00Z"))
            contents[document_id] = " ".join(base) + ("." if rng.random() < 0.5 else "")

        assert find_content_duplicates(documents, contents) == _all_pairs_duplicates(documents, contents)


def test_identical_documents_are_grouped_even_when_comparison_cap_is_reached() -> None:
    documents = [_doc(f"doc-{index}", f"{index}.md", f"2026-10-{10 + index}T00:00:00Z") for index in range(4)]
    contents = {"doc-0": MEETING, "doc-1": MEETING_SPACED, "doc-2": MEETING + "  ", "doc-3": BUDGET}

    with patch("app.modules.agent_run.domain.content_duplicates.MAX_CONTENT_COMPARISONS", 0):
        duplicates = find_content_duplicates(documents, contents)

    # 정규화 본문이 같은 doc-2만 묶이고, Jaccard 비교가 필요한 doc-1은 상한 때문에 비교하지 않는다.
    assert set(duplicates) == {"doc-2"} and duplicates["doc-2"].original_id == "doc-0"

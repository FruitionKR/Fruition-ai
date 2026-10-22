from app.modules.agent_run.domain.plan import AgentPlanOperation, build_agent_plan
from app.modules.agent_run.domain.plan_review import review_duplicate_names


HIERARCHY = [
    {"id": "folder-meeting", "type": "folder", "name": "회의", "parent_id": None, "current_version": 1},
    {"id": "doc-notes", "type": "document", "name": "주간 회의록", "parent_id": "folder-meeting", "current_version": 1},
    {"id": "doc-loose", "type": "document", "name": "주간 회의록", "parent_id": None, "current_version": 2},
    {"id": "doc-plan", "type": "document", "name": "기획안", "parent_id": None, "current_version": 1},
]


def _operation(sequence: int, tool_name: str, arguments: dict, *, target_id=None, depends_on=()) -> AgentPlanOperation:
    target_type = "folder" if tool_name.endswith("folder") else "document"
    return AgentPlanOperation(
        id=f"plan-1-op-{sequence}",
        sequence=sequence,
        tool_name=tool_name,  # type: ignore[arg-type]
        target_type=target_type,  # type: ignore[arg-type]
        target_id=target_id,
        base_version=None if target_id is None else 1,
        source_parent_id=None,
        destination_parent_id=None,
        arguments=arguments,
        reason="정리",
        depends_on=tuple(f"plan-1-op-{dependency}" for dependency in depends_on),
    )


def _plan(*operations: AgentPlanOperation):
    return build_agent_plan("plan-1", "run-1", 1, "정리", operations)


def _by_id(plan):
    return {operation.id: operation for operation in plan.operations}


def test_moving_into_folder_with_same_named_document_is_suspected_duplicate() -> None:
    plan = _plan(
        _operation(1, "move_document", {"document_id": "doc-loose", "folder_id": "folder-meeting",
                                        "position": None, "base_version": 2}, target_id="doc-loose"),
        _operation(2, "move_document", {"document_id": "doc-plan", "folder_id": "folder-meeting",
                                        "position": None, "base_version": 1}, target_id="doc-plan"),
    )

    reviewed = _by_id(review_duplicate_names(plan, HIERARCHY))

    duplicate = reviewed["plan-1-op-1"]
    assert duplicate.suggested_selected is False
    assert duplicate.warning == {
        "type": "duplicate_name",
        "message": "같은 위치에 이름이 같은 문서 '주간 회의록'이(가) 있어 중복일 수 있어요.",
    }
    assert reviewed["plan-1-op-2"].suggested_selected is True
    assert reviewed["plan-1-op-2"].warning is None


def test_review_keeps_hash_and_execution_content() -> None:
    plan = _plan(
        _operation(1, "rename_document", {"document_id": "doc-plan", "display_name": "주간 회의록.md",
                                          "base_version": 1}, target_id="doc-plan"),
    )

    reviewed = review_duplicate_names(plan, HIERARCHY)

    # 최상위에 같은 이름(.md 제외·대소문자 무시) 문서가 있어 중복 의심이다. 해시는 그대로다.
    assert reviewed.operations[0].warning["type"] == "duplicate_name"
    assert reviewed.operation_hash == plan.operation_hash
    assert reviewed.operations[0].arguments == plan.operations[0].arguments


def test_creating_existing_folder_name_unselects_dependent_moves() -> None:
    plan = _plan(
        _operation(1, "create_folder", {"name": " 회의 ", "parent_folder_id": None}),
        _operation(2, "move_document", {"document_id": "doc-plan",
                                        "folder_id": {"$operation_result": "plan-1-op-1", "field": "id"},
                                        "position": None, "base_version": 1},
                   target_id="doc-plan", depends_on=(1,)),
        _operation(3, "create_folder", {"name": "참고", "parent_folder_id": None}),
    )

    reviewed = _by_id(review_duplicate_names(plan, HIERARCHY))

    assert reviewed["plan-1-op-1"].warning["type"] == "duplicate_name"
    assert reviewed["plan-1-op-1"].suggested_selected is False
    assert reviewed["plan-1-op-2"].warning["type"] == "depends_on_unselected"
    assert reviewed["plan-1-op-2"].suggested_selected is False
    assert reviewed["plan-1-op-3"].suggested_selected is True


def test_two_planned_items_with_same_name_flag_only_the_later_one() -> None:
    plan = _plan(
        _operation(1, "create_folder", {"name": "2026-10-19", "parent_folder_id": None}),
        _operation(2, "create_folder", {"name": "2026-10-19", "parent_folder_id": None}),
    )

    reviewed = _by_id(review_duplicate_names(plan, HIERARCHY))

    assert reviewed["plan-1-op-1"].suggested_selected is True
    assert reviewed["plan-1-op-2"].suggested_selected is False


def test_folder_and_document_with_same_name_or_renamed_away_are_not_duplicates() -> None:
    plan = _plan(
        # 같은 이름이어도 폴더와 문서는 중복이 아니다.
        _operation(1, "create_folder", {"name": "기획안", "parent_folder_id": None}),
        # 기존 문서를 다른 이름으로 바꾸면 겹치지 않는다.
        _operation(2, "rename_document", {"document_id": "doc-loose", "display_name": "지난 회의록",
                                          "base_version": 2}, target_id="doc-loose"),
        _operation(3, "move_document", {"document_id": "doc-loose", "folder_id": "folder-meeting",
                                        "position": None, "base_version": 3}, target_id="doc-loose"),
    )

    reviewed = review_duplicate_names(plan, HIERARCHY)

    assert reviewed is plan

from typing import Any

from app.core.response_language import localized
from app.modules.agent_run.domain.entities import ContentArtifactReference
from app.modules.agent_run.domain.plan import (
    AgentPlan,
    AgentPlanOperation,
    AgentPlanOutOfScope,
    build_agent_plan,
)


ALLOWED_PLAN_TOOLS = {
    "create_folder",
    "rename_folder",
    "move_folder",
    "move_document",
    "rename_document",
    "create_document",
    "apply_document_edit",
}
MOVE_DESTINATION_KEYS = {"move_folder": "parent_folder_id", "move_document": "folder_id"}
TARGET_FOLDER_TOOLS = {"rename_folder", "move_folder"}
TARGET_FOLDER_NOTE = "대상 폴더 자체는 바꿀 수 없어 그 폴더의 이름 변경·이동 작업 {count}개는 계획에서 뺐어요."
OUT_OF_SCOPE_NOTE = "대상 폴더 밖으로 옮기는 작업 {count}개는 계획에서 뺐어요."
# 영어 요청의 같은 안내. 프론트가 문장 형식으로 찾아 따로 보여 주므로 형식을 고정한다.
TARGET_FOLDER_NOTE_EN = (
    "Removed {count} rename or move operations on the target folder itself from the plan "
    "because the target folder can't be changed."
)
OUT_OF_SCOPE_NOTE_EN = "Removed {count} operations that move items outside the target folder from the plan."
# MCP 외부 AI PDF 변환본 생성. 원본 PDF와 그 hash를 승인 인자에 고정하고 Document가 승인·실행 때 다시 확인한다.
SOURCE_BOUND_CREATE_DOCUMENT_ARGUMENTS = {
    "display_name", "folder_id", "content_artifact_id", "content_hash",
    "source_document_id", "source_content_hash",
}


def prepare_agent_plan(
    *,
    run_id: str,
    plan_id: str,
    version: int,
    candidate: dict[str, Any],
    hierarchy: list[dict[str, object]],
    content_artifacts: tuple[ContentArtifactReference, ...] = (),
    allowed_tools: set[str],
    target_folder_id: str | None = None,
    allow_source_bound_create: bool = False,
) -> AgentPlan:
    candidate = _place_creates_in_target_folder(candidate, target_folder_id)
    candidate = _scope_moves_to_target_folder(candidate, target_folder_id, hierarchy, plan_id)
    plan = normalize_plan_candidate(run_id, plan_id, version, candidate)
    if any(operation.tool_name not in allowed_tools for operation in plan.operations):
        raise ValueError("Agent plan contains a tool outside the trusted mutation scope.")
    _validate_plan_against_hierarchy(
        plan, hierarchy, content_artifacts, target_folder_id, allow_source_bound_create
    )
    return plan


def _place_creates_in_target_folder(value: dict[str, Any], target_folder_id: str | None) -> dict[str, Any]:
    """대상 폴더가 있으면 위치를 정하지 않은(최상위) 생성 작업을 그 폴더 안으로 둔다.

    대상 폴더는 사용자가 턴에서 고른 새 문서 위치이므로, 모델이 최상위로 둔 생성도 그 폴더에 만든다.
    """
    raw_operations = value.get("operations")
    if target_folder_id is None or not isinstance(raw_operations, list):
        return value
    destination_keys = {"create_folder": "parent_folder_id", "create_document": "folder_id"}
    placed: list[object] = []
    for raw in raw_operations:
        tool_name = raw.get("tool_name") if isinstance(raw, dict) else None
        key = destination_keys.get(tool_name) if isinstance(tool_name, str) else None
        arguments = raw.get("arguments") if key else None
        if key and isinstance(arguments, dict) and key in arguments and arguments[key] is None:
            raw = {
                **raw,
                "arguments": {**arguments, key: target_folder_id},
                "destination_parent_id": target_folder_id,
            }
        placed.append(raw)
    return {**value, "operations": placed}


def _scope_moves_to_target_folder(
    value: dict[str, Any],
    target_folder_id: str | None,
    hierarchy: list[dict[str, object]],
    plan_id: str,
) -> dict[str, Any]:
    """대상 폴더가 있으면 작업을 그 폴더 범위로 맞춘다.

    - 대상 폴더 자신의 rename_folder·move_folder는 뺀다. 대상 폴더는 작업 범위 자체라, 옮기면(하위로 옮기는 것
      포함) 순환이 되고 이름을 바꾸면 범위 밖(부모 폴더)에 보이는 변경이 된다. 목적지 검사보다 먼저 본다.
    - 목적지를 비운(null) 이동은 대상 폴더로 옮기는 것으로 본다(최상위 이동은 범위 밖이라 허용하지 않는다).
    - 같은 계획의 create_folder 결과 참조나 범위 안 폴더 id는 그대로 둔다. 범위 안 폴더는 hierarchy(대상 폴더와
      하위 항목만 담긴 스냅샷)에 있는 폴더다.
    - 범위 밖 폴더 id로 옮기는 작업은 뺀다.
    뺀 작업에 기대는 작업도 함께 빼고 사유별로 요약에 알린다. 남는 작업이 없으면 AgentPlanOutOfScope로 실패시킨다.
    """
    raw_operations = value.get("operations")
    if target_folder_id is None or not isinstance(raw_operations, list):
        return value
    scope_folder_ids = {
        str(item.get("id")) for item in hierarchy if item.get("type") == "folder" and item.get("id") is not None
    }
    placed: list[object] = []
    target_folder_changes: set[int] = set()
    out_of_scope: set[int] = set()
    for sequence, raw in enumerate(raw_operations, start=1):
        tool_name = raw.get("tool_name") if isinstance(raw, dict) else None
        if tool_name in TARGET_FOLDER_TOOLS and _targets_folder(raw, target_folder_id):
            target_folder_changes.add(sequence)
            placed.append(raw)
            continue
        key = MOVE_DESTINATION_KEYS.get(tool_name) if isinstance(tool_name, str) else None
        arguments = raw.get("arguments") if key else None
        if key and isinstance(arguments, dict) and key in arguments:
            destination = arguments[key]
            if destination is None:
                raw = {
                    **raw,
                    "arguments": {**arguments, key: target_folder_id},
                    "destination_parent_id": target_folder_id,
                }
            elif isinstance(destination, str) and destination not in scope_folder_ids:
                out_of_scope.add(sequence)
            elif isinstance(destination, str) and raw.get("destination_parent_id") is None:
                raw = {**raw, "destination_parent_id": destination}
        placed.append(raw)
    if not target_folder_changes and not out_of_scope:
        return {**value, "operations": placed}
    # 대상 폴더 작업에 기대는 작업은 그 사유로, 범위 밖 이동에만 기대는 작업은 범위 밖 사유로 센다.
    target_folder_changes = _with_dependents(placed, target_folder_changes, plan_id)
    out_of_scope = _with_dependents(placed, out_of_scope | target_folder_changes, plan_id) - target_folder_changes
    dropped = target_folder_changes | out_of_scope
    kept = [(sequence, raw) for sequence, raw in enumerate(placed, start=1) if sequence not in dropped]
    if not kept:
        raise AgentPlanOutOfScope()
    renumbered = {old: new for new, (old, _raw) in enumerate(kept, start=1)}
    operations = [_renumber_operation(raw, renumbered, plan_id) for _sequence, raw in kept]
    summary = value.get("summary")
    notes = [
        note.format(count=len(sequences))
        for note, sequences in (
            (localized(TARGET_FOLDER_NOTE, TARGET_FOLDER_NOTE_EN), target_folder_changes),
            (localized(OUT_OF_SCOPE_NOTE, OUT_OF_SCOPE_NOTE_EN), out_of_scope),
        )
        if sequences
    ]
    return {
        **value,
        "summary": (
            " ".join([summary.strip(), *notes]) if isinstance(summary, str) and summary.strip() else summary
        ),
        "operations": operations,
    }


def _targets_folder(raw: dict[str, Any], folder_id: str) -> bool:
    """작업 대상이 folder_id인지 본다. target_id와 arguments.folder_id 중 하나라도 같으면 그 폴더를 바꾸는 작업이다."""
    arguments = raw.get("arguments")
    argument_id = arguments.get("folder_id") if isinstance(arguments, dict) else None
    return folder_id in {_optional_text(raw.get("target_id")), _optional_text(argument_id)}


def _with_dependents(placed: list[object], seeds: set[int], plan_id: str) -> set[int]:
    """seeds 작업과, depends_on이나 결과 참조로 그 작업들에 (간접으로라도) 기대는 작업의 순번을 돌려준다."""
    closure = set(seeds)
    if not closure:
        return closure
    for sequence, raw in enumerate(placed, start=1):
        if sequence in closure or not isinstance(raw, dict):
            continue
        dependencies = raw.get("depends_on", [])
        references = _operation_references(raw.get("arguments"))
        if (isinstance(dependencies, list) and closure.intersection(dependencies)) or any(
            f"{plan_id}-op-{index}" in references for index in closure
        ):
            closure.add(sequence)
    return closure


def _renumber_operation(raw: object, renumbered: dict[int, int], plan_id: str) -> object:
    """작업을 뺀 뒤 남은 작업의 depends_on 순번과 결과 참조 id를 새 순번으로 바꾼다."""
    if not isinstance(raw, dict):
        return raw
    references = {f"{plan_id}-op-{old}": f"{plan_id}-op-{new}" for old, new in renumbered.items()}
    dependencies = raw.get("depends_on", [])
    return {
        **raw,
        "arguments": _rewrite_references(raw.get("arguments"), references),
        **(
            {"depends_on": [renumbered.get(item, item) for item in dependencies]}
            if isinstance(dependencies, list)
            else {}
        ),
    }


def _rewrite_references(value: object, references: dict[str, str]) -> object:
    if isinstance(value, dict):
        if set(value) == {"$operation_result", "field"} and isinstance(value["$operation_result"], str):
            return {**value, "$operation_result": references.get(value["$operation_result"], value["$operation_result"])}
        return {key: _rewrite_references(item, references) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_references(item, references) for item in value]
    return value


def normalize_plan_candidate(
    run_id: str,
    plan_id: str,
    version: int,
    value: dict[str, Any],
) -> AgentPlan:
    summary = value.get("summary")
    raw_operations = value.get("operations")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("Agent plan summary is required.")
    if not isinstance(raw_operations, list) or not 1 <= len(raw_operations) <= 20:
        raise ValueError("Agent plan must contain between 1 and 20 operations.")
    operations: list[AgentPlanOperation] = []
    for index, raw in enumerate(raw_operations, start=1):
        if not isinstance(raw, dict):
            raise ValueError("Agent plan operation must be an object.")
        tool_name = _required_text(raw, "tool_name")
        if tool_name not in ALLOWED_PLAN_TOOLS:
            raise ValueError("Agent plan contains an unsupported tool.")
        target_type = _required_text(raw, "target_type")
        if target_type not in {"folder", "document"}:
            raise ValueError("Agent plan target_type is invalid.")
        arguments = raw.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("Agent plan arguments must be an object.")
        raw_dependencies = raw.get("depends_on", [])
        if not isinstance(raw_dependencies, list) or not all(isinstance(item, int) for item in raw_dependencies):
            raise ValueError("Agent plan dependencies must be operation sequence numbers.")
        operations.append(
            AgentPlanOperation(
                id=f"{plan_id}-op-{index}",
                sequence=index,
                tool_name=tool_name,  # type: ignore[arg-type]
                target_type=target_type,  # type: ignore[arg-type]
                target_id=_optional_text(raw.get("target_id")),
                base_version=_optional_int(raw.get("base_version")),
                source_parent_id=_optional_text(raw.get("source_parent_id")),
                destination_parent_id=_optional_text(raw.get("destination_parent_id")),
                arguments=arguments,
                reason=_required_text(raw, "reason"),
                depends_on=tuple(f"{plan_id}-op-{dependency}" for dependency in raw_dependencies),
            )
        )
    return build_agent_plan(plan_id, run_id, version, summary, tuple(operations))


def _required_text(value: dict[str, Any], key: str) -> str:
    text = _optional_text(value.get(key))
    if text is None:
        raise ValueError(f"Agent plan {key} is required.")
    return text


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("Agent plan base_version must be an integer or null.")
    return value


def _validate_plan_against_hierarchy(
    plan: AgentPlan,
    hierarchy: list[dict[str, object]],
    content_artifacts: tuple[ContentArtifactReference, ...],
    target_folder_id: str | None = None,
    allow_source_bound_create: bool = False,
) -> None:
    items = {str(item.get("id")): item for item in hierarchy if item.get("id") is not None}
    # 대상 폴더는 작업 범위 자체다. 옮기면 자기 하위로 들어가는 순환이 되고 이름을 바꾸면 범위 밖(부모 폴더)에
    # 보이는 변경이 되는데 형제 항목은 스냅샷에 없어 이름 중복도 확인할 수 없다. 삭제 도구는 계획에 없다.
    if target_folder_id is not None and any(
        operation.tool_name in {"rename_folder", "move_folder"} and operation.target_id == target_folder_id
        for operation in plan.operations
    ):
        raise ValueError("Agent plan cannot move or rename the target folder itself.")
    operations = {operation.id: operation for operation in plan.operations}
    operation_ids = {operation.id for operation in plan.operations}
    required_arguments = {
        "create_folder": {"name", "parent_folder_id"},
        "rename_folder": {"folder_id", "name", "base_version"},
        "move_folder": {"folder_id", "parent_folder_id", "position", "base_version"},
        "move_document": {"document_id", "folder_id", "position", "base_version"},
        "rename_document": {"document_id", "display_name", "base_version"},
        "create_document": {"display_name", "folder_id", "content_artifact_id", "content_hash"},
        "apply_document_edit": {
            "document_id",
            "base_version",
            "target",
            "content_artifact_id",
            "content_hash",
        },
    }
    for operation in plan.operations:
        source_bound = (
            allow_source_bound_create
            and operation.tool_name == "create_document"
            and set(operation.arguments) == SOURCE_BOUND_CREATE_DOCUMENT_ARGUMENTS
        )
        if not source_bound and set(operation.arguments) != required_arguments[operation.tool_name]:
            raise ValueError("Agent plan arguments do not match the tool contract.")
        expected_target_type = "folder" if operation.tool_name in {
            "create_folder",
            "rename_folder",
            "move_folder",
        } else "document"
        if operation.target_type != expected_target_type:
            raise ValueError("Agent plan target_type does not match the tool contract.")
        if operation.tool_name in {"create_folder", "create_document"}:
            if operation.target_id is not None or operation.base_version is not None:
                raise ValueError("Create operations cannot have an existing target or base_version.")
        else:
            item = items.get(operation.target_id or "")
            if item is None or item.get("type") != operation.target_type:
                raise ValueError("Agent plan target must exist in the hierarchy snapshot.")
            if (
                operation.tool_name != "apply_document_edit"
                and item.get("current_version") != operation.base_version
            ):
                raise ValueError("Agent plan base_version must match the hierarchy snapshot.")
            id_key = "folder_id" if operation.target_type == "folder" else "document_id"
            if operation.arguments.get(id_key) != operation.target_id:
                raise ValueError("Agent plan target id must match the tool arguments.")
            argument_base_version = operation.arguments.get("base_version")
            if argument_base_version != operation.base_version and not _valid_base_version_reference(
                operation,
                argument_base_version,
                operations,
            ):
                raise ValueError("Agent plan base_version must match the tool arguments.")
        destination_key = {
            "create_folder": "parent_folder_id",
            "create_document": "folder_id",
            "move_folder": "parent_folder_id",
            "move_document": "folder_id",
        }.get(operation.tool_name)
        if destination_key:
            destination = operation.arguments.get(destination_key)
            if isinstance(destination, dict) and (
                (
                    operation.tool_name in {"create_folder", "create_document"}
                    and operation.destination_parent_id is not None
                )
                or not _valid_created_folder_reference(operation, destination, operations)
            ):
                raise ValueError("Agent plan destination must match the tool arguments.")
            if not isinstance(destination, dict) and destination != operation.destination_parent_id:
                raise ValueError("Agent plan destination must match the tool arguments.")
            if isinstance(destination, str):
                target_folder = items.get(destination)
                if target_folder is None or target_folder.get("type") != "folder":
                    raise ValueError("Agent plan destination folder must exist.")
        if operation.tool_name in {"create_document", "apply_document_edit"}:
            _validate_artifact_arguments(operation)
            _validate_artifact_reference(operation, content_artifacts)
        references = _operation_references(operation.arguments)
        if not references.issubset(set(operation.depends_on)) or not references.issubset(operation_ids):
            raise ValueError("Agent plan result references must be declared dependencies.")


def _validate_artifact_arguments(operation: AgentPlanOperation) -> None:
    artifact_id = operation.arguments.get("content_artifact_id")
    content_hash = operation.arguments.get("content_hash")
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise ValueError("Document mutation requires content_artifact_id.")
    if not isinstance(content_hash, str) or not content_hash.strip():
        raise ValueError("Document mutation requires content_hash.")
    if operation.tool_name == "apply_document_edit":
        target = operation.arguments.get("target")
        if not isinstance(target, dict) or set(target) != {"type", "start_line", "end_line"}:
            raise ValueError("apply_document_edit requires an exact target object.")
        if target.get("type") not in {"selection", "current_section", "whole_document"}:
            raise ValueError("apply_document_edit target type is invalid.")
        start_line = target.get("start_line")
        end_line = target.get("end_line")
        if (
            not isinstance(start_line, int)
            or isinstance(start_line, bool)
            or not isinstance(end_line, int)
            or isinstance(end_line, bool)
            or start_line < 1
            or end_line < start_line
        ):
            raise ValueError("apply_document_edit target lines are invalid.")


def _validate_artifact_reference(
    operation: AgentPlanOperation,
    content_artifacts: tuple[ContentArtifactReference, ...],
) -> None:
    artifact = next(
        (
            candidate
            for candidate in content_artifacts
            if candidate.id == operation.arguments.get("content_artifact_id")
        ),
        None,
    )
    if artifact is None:
        raise ValueError("Document mutation artifact was not supplied by trusted context.")
    if artifact.content_hash != operation.arguments.get("content_hash"):
        raise ValueError("Document mutation content_hash does not match its artifact.")
    if artifact.purpose != operation.tool_name:
        raise ValueError("Document mutation artifact purpose does not match the tool.")
    if operation.tool_name == "apply_document_edit" and (
        artifact.document_id != operation.target_id
        or artifact.base_version != operation.base_version
        or artifact.target != operation.arguments.get("target")
    ):
        raise ValueError("Document edit artifact does not match the approved target.")


def _operation_references(value: object) -> set[str]:
    if isinstance(value, dict):
        if set(value) == {"$operation_result", "field"} and isinstance(value["$operation_result"], str):
            return {value["$operation_result"]}
        return set().union(*(_operation_references(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_operation_references(item) for item in value), set())
    return set()


def _valid_created_folder_reference(
    operation: AgentPlanOperation,
    value: dict[str, object],
    operations: dict[str, AgentPlanOperation],
) -> bool:
    if set(value) != {"$operation_result", "field"} or value.get("field") != "id":
        return False
    dependency_id = value.get("$operation_result")
    dependency = operations.get(dependency_id) if isinstance(dependency_id, str) else None
    return (
        dependency is not None
        and dependency.tool_name == "create_folder"
        and dependency.id in operation.depends_on
    )


def _valid_base_version_reference(
    operation: AgentPlanOperation,
    value: object,
    operations: dict[str, AgentPlanOperation],
) -> bool:
    if not isinstance(value, dict) or set(value) != {"$operation_result", "field"}:
        return False
    dependency_id = value.get("$operation_result")
    dependency = operations.get(dependency_id) if isinstance(dependency_id, str) else None
    return (
        value.get("field") == "current_version"
        and dependency is not None
        and dependency.id in operation.depends_on
        and dependency.target_id == operation.target_id
    )

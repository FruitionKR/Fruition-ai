"""승인 화면에서 기본 선택을 정하는 계획 검토(중복 의심 표시).

계획을 적용한 뒤의 위치·이름을 계산해, 같은 폴더에 같은 종류·같은 이름의 항목이 둘 이상 생기면 중복으로 의심한다.
실행 내용은 바꾸지 않고 operation의 warning·suggested_selected만 채운다.
"""

from dataclasses import replace
import unicodedata

from app.modules.agent_run.domain.plan import AgentPlan, AgentPlanOperation

DUPLICATE_NAME_WARNING = "duplicate_name"
DEPENDS_ON_UNSELECTED_WARNING = "depends_on_unselected"

_NAMING_TOOLS = {
    "create_folder": ("folder", "parent_folder_id", "name"),
    "create_document": ("document", "folder_id", "display_name"),
    "rename_folder": ("folder", None, "name"),
    "rename_document": ("document", None, "display_name"),
    "move_folder": ("folder", "parent_folder_id", None),
    "move_document": ("document", "folder_id", None),
}
_TYPE_LABEL = {"folder": "폴더", "document": "문서"}


def review_duplicate_names(plan: AgentPlan, hierarchy: list[dict[str, object]]) -> AgentPlan:
    """중복 의심 작업은 warning을 달고 기본 선택에서 뺀다. 그 작업에 의존하는 작업도 함께 뺀다."""
    state: dict[str, dict[str, object]] = {
        str(item["id"]): {
            "type": item.get("type"),
            "parent": _text(item.get("parent_id")),
            "name": _text(item.get("name")) or "",
            "touched_at": 0,
        }
        for item in hierarchy
        if item.get("id") is not None
    }
    touching: dict[str, list[str]] = {}
    for operation in sorted(plan.operations, key=lambda item: item.sequence):
        key = _apply(state, operation)
        if key is not None:
            touching.setdefault(key, []).append(operation.id)

    warnings: dict[str, dict[str, str]] = {}
    for key, operation_ids in touching.items():
        duplicate = _duplicate_of(state, key)
        if duplicate is None:
            continue
        entry = state[key]
        message = (
            f"같은 위치에 이름이 같은 {_TYPE_LABEL.get(str(entry['type']), '항목')} "
            f"'{entry['name']}'이(가) 있어 중복일 수 있어요."
        )
        for operation_id in operation_ids:
            warnings[operation_id] = {"type": DUPLICATE_NAME_WARNING, "message": message}

    unselected = set(warnings)
    for operation in sorted(plan.operations, key=lambda item: item.sequence):
        if operation.id not in unselected and any(dependency in unselected for dependency in operation.depends_on):
            unselected.add(operation.id)
            warnings[operation.id] = {
                "type": DEPENDS_ON_UNSELECTED_WARNING,
                "message": "중복이 의심되는 작업에 이어지는 작업이라 함께 선택에서 뺐어요.",
            }
    if not warnings:
        return plan
    return replace(
        plan,
        operations=tuple(
            replace(operation, warning=warnings[operation.id], suggested_selected=False)
            if operation.id in warnings
            else operation
            for operation in plan.operations
        ),
    )


def _apply(state: dict[str, dict[str, object]], operation: AgentPlanOperation) -> str | None:
    rule = _NAMING_TOOLS.get(operation.tool_name)
    if rule is None:
        return None
    item_type, parent_key, name_key = rule
    created = operation.tool_name.startswith("create_")
    key = operation.id if created else operation.target_id
    if key is None:
        return None
    entry = dict(state.get(key) or {"type": item_type, "parent": None, "name": ""})
    if parent_key is not None:
        entry["parent"] = _destination(operation.arguments.get(parent_key))
    if name_key is not None:
        entry["name"] = _text(operation.arguments.get(name_key)) or ""
    entry["touched_at"] = operation.sequence
    state[key] = entry
    return key


def _duplicate_of(state: dict[str, dict[str, object]], key: str) -> str | None:
    """key와 위치·종류·이름이 같은 다른 항목. 둘 다 계획이 바꿨으면 나중에 바꾼 쪽만 중복으로 본다."""
    entry = state[key]
    name = _normalized(entry["name"], entry["type"])
    if not name:
        return None
    for other_key, other in state.items():
        if (
            other_key == key
            or other["type"] != entry["type"]
            or other["parent"] != entry["parent"]
            or _normalized(other["name"], other["type"]) != name
        ):
            continue
        if 0 < int(other["touched_at"]) and int(entry["touched_at"]) < int(other["touched_at"]):
            continue
        return other_key
    return None


def _destination(value: object) -> str | None:
    """폴더 ID 문자열이나 앞선 create_folder 결과 참조를 상태의 부모 키로 바꾼다."""
    if isinstance(value, dict) and isinstance(value.get("$operation_result"), str):
        return str(value["$operation_result"])
    return _text(value)


def _normalized(name: object, item_type: object) -> str:
    text = unicodedata.normalize("NFC", str(name or "")).strip().casefold()
    return text.removesuffix(".md").strip() if item_type == "document" else text


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

from typing import Any


def compensation_request(
    tool_name: str,
    before: dict[str, Any],
    response: dict[str, Any],
    version: int,
    artifact_id: str,
) -> tuple[str, dict[str, Any]]:
    """모델의 계획 대신 실행 전에 기록한 값으로만 역작업을 구성한다."""
    target_id = response.get("document_id" if tool_name == "apply_document_edit" else "id")
    if not isinstance(target_id, str) or not target_id:
        raise ValueError("rollback_target_missing")
    if type(version) is not int or version < 1:
        raise ValueError("rollback_version_missing")
    target_type = "folder" if tool_name.endswith("folder") else "document"
    arguments: dict[str, Any] = {f"{target_type}_id": target_id, "base_version": version}
    if tool_name in {"create_folder", "create_document"}:
        if target_type == "folder":
            # 내부 삭제 도구는 트랜잭션 안에서 비어 있는 폴더만 삭제해야 한다.
            arguments["require_empty"] = True
        return f"delete_{target_type}", arguments
    if before.get("id") != target_id:
        raise ValueError("rollback_snapshot_target_mismatch")
    if tool_name in {"rename_folder", "rename_document"}:
        key = "name" if target_type == "folder" else "display_name"
        arguments[key] = before[key]
    elif tool_name in {"move_folder", "move_document"}:
        key = "parent_folder_id" if target_type == "folder" else "folder_id"
        arguments[key] = before["parent_id"]
        arguments["position"] = before["position"]
    elif tool_name == "apply_document_edit":
        arguments.update(
            content_artifact_id=artifact_id,
            content_hash="sha256:" + before["content_hash"],
            target={
                "type": "whole_document",
                "start_line": 1,
                "end_line": max(1, len(before["markdown"].splitlines())),
            },
        )
    else:
        raise ValueError("rollback_tool_unsupported")
    return tool_name, arguments

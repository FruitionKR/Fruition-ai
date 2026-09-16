"""Agent 실행 기록에 따른 역순 복구.

백엔드 연결 계약:
- delete_folder(folder_id, base_version, require_empty=True)는 원자적으로 빈 폴더만 삭제한다.
- delete_document(document_id, base_version)는 파생 Wiki 정리가 끝나야 cleanup_complete=True를 반환한다.
- 삭제에도 기존 execute 경로의 승인 검사와 Idempotency-Key 처리가 필요하다.
"""

import hashlib
from typing import Any

from app.modules.agent_run.application.ports import (
    AgentJobRepositoryPort, AgentPlanRepositoryPort, AgentToolGatewayPort, ToolGatewayError,
)
from app.modules.agent_run.domain.compensation import compensation_request
from app.modules.agent_run.domain.entities import AgentRunContext
from app.modules.agent_run.domain.plan import AgentPlan, AgentPlanOperation


class RollbackAgentRunUseCase:
    def __init__(self, repository: AgentJobRepositoryPort, artifacts: AgentPlanRepositoryPort,
                 gateway: AgentToolGatewayPort) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._gateway = gateway

    def prepare(self, context: AgentRunContext, plan: AgentPlan, operation: AgentPlanOperation,
                arguments: dict[str, Any]) -> bool:
        # 재시작 시 원복할 이전 값을 현재 상태로 덮어쓰지 않는다.
        if self._repository.get_undo_record(context.run.id, operation.id) is not None:
            return True
        before = self._snapshot(context, operation.tool_name, arguments)
        return self._repository.prepare_undo(
            context.run.id, plan.id, operation.id, operation.tool_name, arguments, before,
        )

    def execute(self, run_id: str) -> None:
        if not self._repository.mark_run_status(
            run_id, ("cancel_requested", "rolling_back"), "rolling_back",
        ):
            return
        context = self._repository.load_context(run_id)
        if context.run.action == "markdown_turn":
            self._repository.finish_rollback(run_id, self._repository.parent_rollback_error(run_id))
            return
        versions: dict[tuple[str, str], int] = {}
        for record in self._repository.load_undo_records(run_id):
            try:
                # HTTP 호출 전에 영속 실행 기록을 남긴다. 그 기록이 없으면 실행하지 않은 단계다.
                if record["forward_status"] is None:
                    if record["id"] is not None:
                        self._repository.complete_undo(record["id"], {})
                    continue
                if (record["forward_status"] == "failed" and record.get("forward_attempt") == 1
                        and record.get("forward_error_code") in {"tool_http_400", "tool_http_401", "tool_http_403", "tool_http_404", "tool_http_409", "tool_http_422"}):
                    self._repository.complete_undo(record["id"], {})
                    continue
                if record["id"] is None:
                    raise ValueError("rollback_snapshot_missing")
                if record["forward_status"] != "succeeded":
                    # 응답이 없으면 요청이 서버에 반영됐는지 단정할 수 없다.
                    raise ValueError("rollback_forward_outcome_unknown")
                response = record["forward_response"]
                target_type = "folder" if record["tool_name"].endswith("folder") else "document"
                content_edit = record["tool_name"] == "apply_document_edit"
                target = ("content" if content_edit else target_type, response.get("document_id" if content_edit else "id"))
                metadata = record["response_metadata"]
                if record["status"] == "undo_done":
                    restored = metadata["undo_response"]
                else:
                    version = versions.get(target, response.get("current_version"))
                    restored = self._undo(context, record, version)
                if type(restored.get("current_version")) is int:
                    versions[target] = restored["current_version"]
            except ToolGatewayError as exc:
                self._repository.finish_rollback(run_id, f"rollback_tool_http_{exc.status_code or 'unavailable'}")
                return
            except (ValueError, KeyError, TypeError) as exc:
                codes = {"rollback_snapshot_missing", "rollback_forward_outcome_unknown",
                         "rollback_target_missing", "rollback_version_missing", "rollback_snapshot_target_mismatch",
                         "rollback_tool_unsupported", "rollback_breadcrumb_mismatch", "rollback_verification_failed"}
                code = str(exc) if str(exc) in codes else "rollback_snapshot_invalid"
                self._repository.finish_rollback(run_id, f"{code}:{record['operation_id']}")
                return
        self._repository.finish_rollback(run_id)

    def _undo(self, context: AgentRunContext, record: dict[str, Any], version: int) -> dict[str, Any]:
        metadata = record["response_metadata"]
        before = metadata["before"]
        if record["status"] == "undo_running":
            # 응답 유실 후에도 동일한 멱등키에는 동일한 인자를 사용한다.
            tool, arguments = metadata["undo_tool"], metadata["undo_arguments"]
        else:
            artifact_id = f"undo-{record['id']}-{version}"
            tool, arguments = compensation_request(
                record["tool_name"], before, record["forward_response"], version, artifact_id,
            )
            if tool == "apply_document_edit":
                self._artifacts.register_artifact(
                    run_id=context.run.id, workspace_id=context.run.workspace_id, user_id=context.run.user_id,
                    artifact_id=artifact_id, content_hash=arguments["content_hash"], purpose=tool,
                    document_id=arguments["document_id"], base_version=version,
                    target=arguments["target"], markdown=before["markdown"],
                )
            self._repository.save_undo_request(record["id"], tool, arguments)
        restored = self._gateway.execute(
            tool, run_id=context.run.id, workspace_id=context.run.workspace_id, user_id=context.run.user_id,
            plan_id=record["plan_id"], plan_version=record["plan_version"], operation_hash=record["operation_hash"],
            operation_id=record["id"], idempotency_key=f"agent-undo:{record['id']}", arguments=arguments,
        )
        if not self._verify(context, tool, arguments, before, restored):
            raise ValueError("rollback_verification_failed")
        self._repository.complete_undo(record["id"], restored)
        return restored

    def _snapshot(self, context: AgentRunContext, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool in {"create_folder", "create_document"}:
            return {}
        if tool == "apply_document_edit":
            before = self._read(context, "get_document_content", {"document_id": arguments["document_id"]})
            version = before["edit_revision"]
        elif tool == "rename_document":
            before = self._read(context, "get_document_metadata", {"document_id": arguments["document_id"]})
            version = before["current_version"]
        else:
            target_type = "folder" if tool.endswith("folder") else "document"
            before = self._placement(context, target_type, arguments[f"{target_type}_id"])
            version = before["current_version"]
        target_id = arguments.get("folder_id") if tool.endswith("folder") else arguments.get("document_id")
        if before.get("id") != target_id:
            raise ValueError("rollback_snapshot_target_mismatch")
        if tool == "apply_document_edit":
            content_hash = hashlib.sha256(before["markdown"].encode("utf-8")).hexdigest()
            if before["content_hash"] != content_hash:
                raise ValueError("rollback_snapshot_hash_mismatch")
        if version != arguments["base_version"]:
            raise ValueError("rollback_snapshot_version_conflict")
        return before

    def _placement(self, context: AgentRunContext, target_type: str, target_id: str) -> dict[str, Any]:
        path = self._read(context, "get_breadcrumb", {f"{target_type}_id": target_id})["path"]
        if not path or path[-1]["id"] != target_id or path[-1]["type"] != target_type:
            raise ValueError("rollback_breadcrumb_mismatch")
        parent_id = path[-2]["id"] if len(path) > 1 else None
        response = self._read(context, "list_root_items", {}) if parent_id is None else self._read(
            context, "list_folder_children", {"folder_id": parent_id},
        )
        for position, item in enumerate(response["items"]):
            if item["id"] == target_id and item["type"] == target_type:
                return {**item, "parent_id": parent_id, "position": position}
        raise ValueError("rollback_target_missing")

    def _verify(self, context: AgentRunContext, tool: str, arguments: dict[str, Any],
                before: dict[str, Any], restored: dict[str, Any]) -> bool:
        target_type = "folder" if tool.endswith("folder") else "document"
        target_id = arguments[f"{target_type}_id"]
        if tool.startswith("delete_"):
            # 문서 삭제로 파생된 Wiki 정리까지 끝났다는 서버 확인이 필요하다.
            if tool == "delete_document" and restored.get("cleanup_complete") is not True:
                return False
            try:
                self._read(context, "get_breadcrumb", {f"{target_type}_id": target_id})
            except ToolGatewayError as exc:
                if exc.status_code == 404:
                    return True
                raise
            return False
        if restored.get("document_id" if tool == "apply_document_edit" else "id") != target_id:
            return False
        if tool == "apply_document_edit":
            current = self._read(context, "get_document_content", {"document_id": target_id})
            return (current["edit_revision"] == restored.get("current_version")
                    and current["content_hash"] == before["content_hash"])
        if tool == "rename_document":
            current = self._read(context, "get_document_metadata", {"document_id": target_id})
            matches = current["display_name"] == before["display_name"]
        else:
            current = self._placement(context, target_type, target_id)
            matches = (current["name"] == before["name"] if tool == "rename_folder" else
                       (current["parent_id"], current["position"]) == (before["parent_id"], before["position"]))
        return matches and current["current_version"] == restored.get("current_version")

    def _read(self, context: AgentRunContext, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # 복구·검증은 모델의 추가 행동이 아니므로 모델 도구 예산과 분리한다.
        if tool == "get_breadcrumb":
            arguments = {"folder_id": None, "document_id": None, **arguments}
        return self._gateway.read(tool, run_id=context.run.id, workspace_id=context.run.workspace_id,
                                  user_id=context.run.user_id, arguments=arguments)

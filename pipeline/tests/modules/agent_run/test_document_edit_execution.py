"""첨부 문서 수정 run의 결정적 계획, 실행, 롤백 스냅샷."""

import hashlib
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from app.modules.agent_run.application.ports import ToolGatewayError
from app.modules.agent_run.application.rollback_agent_run import RollbackAgentRunUseCase
from app.modules.agent_run.domain.entities import AgentRun, AgentRunContext, DocumentEditArtifact
from app.modules.agent_run.domain.plan import AgentPlan, AgentPlanOperation, build_agent_plan
from app.modules.agent_run.infrastructure.agent_worker import AgentWorker

TARGET = {"type": "whole_document", "start_line": 1, "end_line": 3}


def _artifact(artifact_id: str = "artifact-1", document_id: str = "doc-1", **values: object) -> DocumentEditArtifact:
    return DocumentEditArtifact(
        **{
            "id": artifact_id,
            "content_hash": "sha256:" + "a" * 64,
            "document_id": document_id,
            "base_version": 7,
            "target": TARGET,
            "summary": f"{document_id} 요약",
            "expired": False,
            **values,
        }
    )


def _context(
    action: str = "workspace_workflow",
    status: str = "queued",
    skill_version_id: str | None = None,
    allowed_tools: tuple[str, ...] = (),
) -> AgentRunContext:
    return AgentRunContext(
        run=AgentRun(
            id="run-1",
            workspace_id="workspace-1",
            user_id="user-1",
            action=action,
            skill_version_id=skill_version_id,
            status=status,
            request_summary="첨부를 고쳐줘",
        ),
        skill_instructions=None,
        allowed_tools=allowed_tools,
    )


def _worker(context: AgentRunContext, artifacts: tuple[DocumentEditArtifact, ...]):
    repository, plans, gateway, generator = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    repository.load_context.return_value = context
    repository.next_plan_version.return_value = 1
    plans.load_document_edit_artifacts.return_value = artifacts
    return AgentWorker(repository, plans, gateway, generator), repository, plans, gateway, generator


class TestDeterministicPlan:
    def test_builds_contract_operations_without_tree_or_model(self) -> None:
        artifacts = (_artifact("artifact-1", "doc-1"), _artifact("artifact-2", "doc-2", base_version=3))
        worker, _, plans, gateway, generator = _worker(_context(), artifacts)

        worker._plan_run("run-1")

        generator.generate.assert_not_called()
        gateway.read.assert_not_called()
        plan = plans.save_plan.call_args.args[1]
        assert plan.summary == "아직 바꾸지 않았습니다. 승인하면 문서 2개를 수정합니다."
        assert [operation.sequence for operation in plan.operations] == [1, 2]
        first, second = plan.operations
        assert (first.tool_name, first.target_type, first.target_id, first.base_version) == (
            "apply_document_edit", "document", "doc-1", 7,
        )
        assert first.arguments == {
            "document_id": "doc-1",
            "base_version": 7,
            "target": TARGET,
            "content_artifact_id": "artifact-1",
            "content_hash": "sha256:" + "a" * 64,
        }
        assert first.reason == "doc-1 요약"
        assert second.base_version == 3
        assert first.depends_on == second.depends_on == ()
        assert first.warning is None and first.suggested_selected is True

    def test_skill_without_edit_tool_is_rejected(self) -> None:
        context = _context(skill_version_id="version-1", allowed_tools=("list_root_items",))
        worker, _, plans, _, _ = _worker(context, (_artifact(),))

        with pytest.raises(ValueError, match="not allowed by the selected Skill"):
            worker._plan_run("run-1")

        plans.save_plan.assert_not_called()

    def test_skill_with_edit_tool_is_allowed(self) -> None:
        context = _context(skill_version_id="version-1", allowed_tools=("apply_document_edit",))
        worker, _, plans, _, _ = _worker(context, (_artifact(),))

        worker._plan_run("run-1")

        plans.save_plan.assert_called_once()

    def test_expired_artifact_fails_instead_of_falling_back_to_the_model(self) -> None:
        worker, _, plans, _, generator = _worker(_context(), (_artifact(expired=True),))

        with pytest.raises(ValueError, match="expired"):
            worker._plan_run("run-1")

        generator.generate.assert_not_called()
        plans.save_plan.assert_not_called()

    def test_workflow_without_preview_artifacts_keeps_the_llm_plan(self) -> None:
        worker, _, plans, _, generator = _worker(_context(), ())
        generator.generate.return_value = replace(_plan(), status="awaiting_approval")

        with (
            patch.object(worker, "_inspect_hierarchy", return_value=[]),
            patch.object(worker, "_load_content_artifacts", return_value=()) as loaded,
        ):
            worker._plan_run("run-1")

        generator.generate.assert_called_once()
        loaded.assert_called_once()
        plans.save_plan.assert_called_once()

    def test_folder_organize_never_looks_for_preview_artifacts(self) -> None:
        worker, _, plans, _, generator = _worker(_context(action="folder_organize"), ())
        generator.generate.return_value = replace(_plan(), status="awaiting_approval")

        with patch.object(worker, "_inspect_hierarchy", return_value=[]):
            worker._plan_run("run-1")

        plans.load_document_edit_artifacts.assert_not_called()
        generator.generate.assert_called_once()


def _operation(operation_id: str = "op-1", document_id: str = "doc-1", tool: str = "apply_document_edit") -> AgentPlanOperation:
    return AgentPlanOperation(
        id=operation_id, sequence=1, tool_name=tool, target_type="document", target_id=document_id,
        base_version=7, source_parent_id=None, destination_parent_id=None,
        arguments={"document_id": document_id, "base_version": 7, "target": TARGET,
                   "content_artifact_id": "artifact-1", "content_hash": "sha256:" + "a" * 64},
        reason="요약",
    )


def _plan(*operations: AgentPlanOperation) -> AgentPlan:
    return replace(build_agent_plan("plan-1", "run-1", 1, "요약", operations or (_operation(),)), status="approved")


def _snapshot(markdown: str = "본문", revision: int = 9) -> dict[str, object]:
    return {
        "id": "doc-1",
        "markdown": markdown,
        "content_hash": hashlib.sha256(markdown.encode()).hexdigest(),
        "edit_revision": revision,
    }


def _executing(gateway: MagicMock) -> tuple[AgentWorker, MagicMock]:
    repository = MagicMock()
    repository.get_undo_record.return_value = None
    repository.prepare_undo.return_value = True
    repository.mark_operation.return_value = True
    repository.reserve_tool_call.return_value = True
    repository.load_context.return_value = _context(status="executing")
    return AgentWorker(repository, MagicMock(), gateway, MagicMock()), repository


class TestExecuteOperation:
    @pytest.mark.parametrize(
        ("status_code", "operation_status", "error_code"),
        [
            (409, "conflicted", "tool_http_409"),
            (423, "failed", "tool_http_423"),
            (403, "forbidden", "tool_http_403"),
            (404, "failed", "tool_http_404"),
        ],
    )
    def test_tool_outcomes_map_to_the_contract_table(self, status_code: int, operation_status: str, error_code: str) -> None:
        gateway = MagicMock()
        gateway.read.return_value = _snapshot()
        gateway.execute.side_effect = ToolGatewayError(status_code, False)
        worker, repository = _executing(gateway)
        plan = _plan()

        assert worker._execute_operation(repository.load_context("run-1"), plan, plan.operations[0], {}) is None

        gateway.execute.assert_called_once()
        repository.mark_operation.assert_called_with("op-1", ("running",), operation_status, error_code)

    def test_changed_revision_after_planning_still_reaches_the_merge(self) -> None:
        gateway = MagicMock()
        gateway.read.return_value = _snapshot(revision=9)  # 계획은 base_version 7
        gateway.execute.return_value = {"document_id": "doc-1", "current_version": 10, "content_hash": "h"}
        worker, repository = _executing(gateway)
        plan = _plan()

        response = worker._execute_operation(repository.load_context("run-1"), plan, plan.operations[0], {})

        assert response["current_version"] == 10
        repository.mark_operation.assert_called_with("op-1", ("running",), "succeeded")

    @pytest.mark.parametrize(
        ("status_code", "operation_status"), [(404, "failed"), (403, "forbidden")], ids=["deleted", "revoked"]
    )
    def test_snapshot_read_failure_fails_only_that_operation_without_a_forward_record(
        self, status_code: int, operation_status: str
    ) -> None:
        gateway = MagicMock()
        gateway.read.side_effect = ToolGatewayError(status_code, False)
        worker, repository = _executing(gateway)
        plan = _plan()

        assert worker._execute_operation(repository.load_context("run-1"), plan, plan.operations[0], {}) is None

        gateway.execute.assert_not_called()
        repository.mark_operation.assert_called_with("op-1", ("running",), operation_status, f"tool_http_{status_code}")
        # 전방 기록이 없어야 롤백이 '실행하지 않은 단계'로 건너뛴다.
        repository.save_tool_execution.assert_not_called()

    def test_retryable_snapshot_read_retries_then_executes(self) -> None:
        gateway = MagicMock()
        gateway.read.side_effect = [ToolGatewayError(503, True), _snapshot()]
        gateway.execute.return_value = {"document_id": "doc-1", "current_version": 10, "content_hash": "h"}
        worker, repository = _executing(gateway)
        plan = _plan()

        assert worker._execute_operation(repository.load_context("run-1"), plan, plan.operations[0], {}) is not None

        assert gateway.read.call_count == 2
        gateway.execute.assert_called_once()

    def test_retryable_snapshot_read_exhausted_fails_only_that_operation(self) -> None:
        gateway = MagicMock()
        gateway.read.side_effect = ToolGatewayError(503, True)
        worker, repository = _executing(gateway)
        plan = _plan()

        assert worker._execute_operation(repository.load_context("run-1"), plan, plan.operations[0], {}) is None

        assert gateway.read.call_count == 3
        repository.mark_operation.assert_called_with("op-1", ("running",), "failed", "tool_http_503")

    def test_invalid_snapshot_fails_only_an_edit_operation(self) -> None:
        gateway = MagicMock()
        gateway.read.return_value = {**_snapshot(), "content_hash": "wrong"}
        worker, repository = _executing(gateway)
        plan = _plan()

        assert worker._execute_operation(repository.load_context("run-1"), plan, plan.operations[0], {}) is None

        gateway.execute.assert_not_called()
        repository.mark_operation.assert_called_with("op-1", ("running",), "failed", "rollback_snapshot_invalid")

    def test_invalid_snapshot_of_other_tools_still_fails_the_job(self) -> None:
        gateway = MagicMock()
        gateway.read.return_value = {"id": "doc-1", "display_name": "이름", "current_version": 2}
        worker, repository = _executing(gateway)
        operation = replace(_operation(tool="rename_document"), base_version=7)
        plan = _plan(operation)

        with pytest.raises(ValueError, match="rollback_snapshot_version_conflict"):
            worker._execute_operation(repository.load_context("run-1"), plan, plan.operations[0], {})

    def test_one_failure_leaves_the_other_operation_runnable(self) -> None:
        first, second = _operation("op-1", "doc-1"), replace(_operation("op-2", "doc-2"), sequence=2)
        plan = _plan(first, second)
        repository = MagicMock()
        repository.load_context.return_value = _context(status="executing")
        repository.load_current_plan.return_value = replace(
            plan, operations=(replace(first, status="failed", error_code="tool_http_423"), second)
        )
        repository.load_operation_results.return_value = {}
        repository.remaining_tool_calls.return_value = 10
        worker = AgentWorker(repository, MagicMock(), MagicMock(), MagicMock())

        with patch.object(worker, "_execute_operation", return_value={"current_version": 2}) as execute:
            state = worker._execute_step_node(
                {"run_id": "run-1", "plan_id": plan.id, "plan_version": plan.version, "operation_hash": plan.operation_hash}
            )

        assert execute.call_args.args[2].id == "op-2"
        assert state["outcome"] == "continue"
        repository.mark_operation.assert_not_called()


class TestRollbackSnapshot:
    def _use_case(self, read: dict[str, object]) -> RollbackAgentRunUseCase:
        gateway = MagicMock()
        gateway.read.return_value = read
        return RollbackAgentRunUseCase(MagicMock(), MagicMock(), gateway)

    def test_document_edit_skips_version_comparison_but_keeps_current_snapshot(self) -> None:
        snapshot = _snapshot(revision=9)

        before = self._use_case(snapshot)._snapshot(
            _context(status="executing"), "apply_document_edit", {"document_id": "doc-1", "base_version": 7}
        )

        assert before["edit_revision"] == 9

    def test_document_edit_still_rejects_broken_snapshot(self) -> None:
        with pytest.raises(ValueError, match="rollback_snapshot_hash_mismatch"):
            self._use_case({**_snapshot(), "content_hash": "wrong"})._snapshot(
                _context(status="executing"), "apply_document_edit", {"document_id": "doc-1", "base_version": 9}
            )

    def test_other_tools_keep_version_comparison(self) -> None:
        with pytest.raises(ValueError, match="rollback_snapshot_version_conflict"):
            self._use_case({"id": "doc-1", "display_name": "이름", "current_version": 9})._snapshot(
                _context(status="executing"), "rename_document", {"document_id": "doc-1", "base_version": 7}
            )

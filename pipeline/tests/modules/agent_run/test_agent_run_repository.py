import hashlib
import unittest
from unittest.mock import MagicMock, patch

from app.modules.agent_run.infrastructure.postgres_agent_run_repository import (
    PostgresAgentRunRepository,
    _canonical_json,
    _validate_artifact_metadata,
    _validate_content_hash,
)
from app.modules.agent_run.domain.entities import AgentRun, StartAgentRunArtifact
from app.modules.wiki_ingestion.infrastructure import (
    postgres_wiki_ingestion_repository as database,
)


class AgentRunRepositoryTest(unittest.TestCase):
    def test_authorizes_only_exact_approved_running_operation_arguments(self) -> None:
        connection = MagicMock()
        operation_result = MagicMock()
        operation_result.fetchone.return_value = {
            "arguments": {
                "name": "하위 폴더",
                "parent_folder_id": {
                    "$operation_result": "operation-1",
                    "field": "id",
                },
            }
        }
        executions_result = MagicMock()
        executions_result.fetchall.return_value = [
            {
                "operation_id": "operation-1",
                "response_metadata": {"id": "folder-1"},
            }
        ]
        connection.execute.side_effect = [operation_result, executions_result]
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection

        with patch.object(database, "connect_ai", return_value=connection_context):
            authorized = PostgresAgentRunRepository().authorize_tool_execute(
                run_id="run-1",
                workspace_id="workspace-1",
                user_id="user-1",
                plan_id="plan-1",
                plan_version=2,
                operation_hash="a" * 64,
                operation_id="operation-2",
                tool_name="create_folder",
                arguments={"name": "하위 폴더", "parent_folder_id": "folder-1"},
            )

        self.assertTrue(authorized)
        query, parameters = connection.execute.call_args_list[0].args
        self.assertIn("run.current_plan_id", query)
        self.assertIn("operation.status = 'running'", query)
        self.assertIn("approval.decision = 'approved'", query)
        self.assertEqual(parameters[-1], "create_folder")

    def test_rejects_tampered_or_type_changed_approved_arguments(self) -> None:
        for actual_arguments in (
            {"name": "변조", "base_version": 1},
            {"name": "승인", "base_version": True},
        ):
            with self.subTest(arguments=actual_arguments):
                connection = MagicMock()
                operation_result = MagicMock()
                operation_result.fetchone.return_value = {
                    "arguments": {"name": "승인", "base_version": 1}
                }
                executions_result = MagicMock()
                executions_result.fetchall.return_value = []
                connection.execute.side_effect = [operation_result, executions_result]
                connection_context = MagicMock()
                connection_context.__enter__.return_value = connection

                with patch.object(database, "connect_ai", return_value=connection_context):
                    authorized = PostgresAgentRunRepository().authorize_tool_execute(
                        run_id="run-1",
                        workspace_id="workspace-1",
                        user_id="user-1",
                        plan_id="plan-1",
                        plan_version=1,
                        operation_hash="a" * 64,
                        operation_id="operation-1",
                        tool_name="rename_folder",
                        arguments=actual_arguments,
                    )

                self.assertFalse(authorized)

    def test_json_comparison_distinguishes_boolean_from_integer(self) -> None:
        self.assertNotEqual(_canonical_json(True), _canonical_json(1))

    def test_revise_clears_previous_clarification_error_code(self) -> None:
        instruction = "문서를 정리해줘. " * 150 + "기존 이름은 바꾸지 마."
        connection = MagicMock()
        locked_result = MagicMock()
        locked_result.fetchone.return_value = _run_row(
            status="clarification_required",
            error_code="react_step_limit_exceeded",
            current_plan_id="plan-1",
        )
        supersede_result = MagicMock()
        update_result = MagicMock()
        update_result.fetchone.return_value = _run_row(
            status="queued",
            error_code=None,
            current_plan_id=None,
            request_summary=instruction,
        )
        insert_result = MagicMock()
        connection.execute.side_effect = [
            locked_result,
            supersede_result,
            update_result,
            insert_result,
        ]
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection

        with patch.object(database, "connect_ai", return_value=connection_context):
            run = PostgresAgentRunRepository().revise(
                "workspace-1",
                "user-1",
                "run-1",
                instruction,
                "job-1",
            )

        update_query = connection.execute.call_args_list[2].args[0]
        self.assertIn("error_code = NULL", update_query)
        self.assertIsNone(run.error_code)
        self.assertEqual(connection.execute.call_args_list[2].args[1], (instruction, "run-1"))

    def test_repository_uses_ai_database_connection(self) -> None:
        connection = MagicMock()
        result = MagicMock()
        result.fetchone.return_value = None
        connection.execute.return_value = result
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection

        with patch.object(database, "connect_ai", return_value=connection_context) as connect_ai:
            PostgresAgentRunRepository().get_for_user("workspace-1", "user-1", "run-1")

        connect_ai.assert_called_once_with()

    def test_artifact_validation_rejects_hash_and_wrong_target(self) -> None:
        with self.assertRaises(ValueError):
            _validate_content_hash("# 문서\n", "sha256:wrong")
        with self.assertRaises(ValueError):
            _validate_artifact_metadata(
                "create_document", "document-1", None, None
            )
        with self.assertRaises(ValueError):
            _validate_artifact_metadata(
                "apply_document_edit",
                "document-1",
                2,
                {"type": "whole_document", "start_line": 0, "end_line": 3},
            )

    def test_artifact_resolution_is_workspace_and_user_scoped(self) -> None:
        connection = MagicMock()
        result = MagicMock()
        result.fetchone.return_value = None
        connection.execute.return_value = result
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection

        with patch.object(database, "connect_ai", return_value=connection_context):
            artifact = PostgresAgentRunRepository().resolve_artifact(
                run_id="run-1",
                workspace_id="workspace-2",
                user_id="user-1",
                artifact_id="artifact-1",
                content_hash="sha256:abc",
                purpose="create_document",
                document_id=None,
                base_version=None,
                target=None,
            )

        self.assertIsNone(artifact)
        self.assertIn("workspace_id = %s", connection.execute.call_args.args[0])
        self.assertEqual(connection.execute.call_args.args[1][2], "workspace-2")
        self.assertEqual(connection.execute.call_args.args[1][3], "user-1")

    def test_register_artifact_stores_only_object_key_and_scoped_metadata(self) -> None:
        markdown = "# 문서\n"
        content_hash = "sha256:" + hashlib.sha256(markdown.encode()).hexdigest()
        connection = MagicMock()
        run_result = MagicMock()
        run_result.fetchone.return_value = {"id": "run-1"}
        existing_result = MagicMock()
        existing_result.fetchone.return_value = None
        inserted_result = MagicMock()
        inserted_result.fetchone.return_value = {
            "id": "artifact-1",
            "run_id": "run-1",
            "workspace_id": "workspace-1",
            "user_id": "user-1",
            "content_hash": content_hash,
            "purpose": "create_document",
            "document_id": None,
            "base_version": None,
            "target": None,
        }
        connection.execute.side_effect = [run_result, existing_result, inserted_result]
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection

        with (
            patch.object(database, "connect_ai", return_value=connection_context),
            patch(
                "app.modules.agent_run.infrastructure.postgres_agent_run_repository.write_text_object"
            ) as write_text,
            patch(
                "app.modules.agent_run.infrastructure.postgres_agent_run_repository.delete_object"
            ) as delete_object,
        ):
            result = PostgresAgentRunRepository().register_artifact(
                run_id="run-1",
                workspace_id="workspace-1",
                user_id="user-1",
                artifact_id="artifact-1",
                content_hash=content_hash,
                purpose="create_document",
                document_id=None,
                base_version=None,
                target=None,
                markdown=markdown,
            )

        self.assertEqual(result["id"], "artifact-1")
        write_text.assert_called_once()
        delete_object.assert_not_called()
        self.assertNotIn("markdown", result)

    def test_register_artifact_deletes_object_when_db_insert_fails(self) -> None:
        markdown = "# 문서\n"
        content_hash = "sha256:" + hashlib.sha256(markdown.encode()).hexdigest()
        connection = MagicMock()
        run_result = MagicMock()
        run_result.fetchone.return_value = {"id": "run-1"}
        existing_result = MagicMock()
        existing_result.fetchone.return_value = None
        connection.execute.side_effect = [run_result, existing_result, RuntimeError("db failure")]
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection

        with (
            patch.object(database, "connect_ai", return_value=connection_context),
            patch(
                "app.modules.agent_run.infrastructure.postgres_agent_run_repository.write_text_object"
            ) as write_text,
            patch(
                "app.modules.agent_run.infrastructure.postgres_agent_run_repository.delete_object"
            ) as delete_object,
        ):
            with self.assertRaisesRegex(RuntimeError, "db failure"):
                PostgresAgentRunRepository().register_artifact(
                    run_id="run-1",
                    workspace_id="workspace-1",
                    user_id="user-1",
                    artifact_id="artifact-1",
                    content_hash=content_hash,
                    purpose="create_document",
                    document_id=None,
                    base_version=None,
                    target=None,
                    markdown=markdown,
                )

        write_text.assert_called_once()
        delete_object.assert_called_once_with(write_text.call_args.args[0])

    def test_register_artifact_rejects_cross_workspace_run(self) -> None:
        connection = MagicMock()
        result = MagicMock()
        result.fetchone.return_value = None
        connection.execute.return_value = result
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection

        with patch.object(database, "connect_ai", return_value=connection_context):
            with self.assertRaises(KeyError):
                PostgresAgentRunRepository().register_artifact(
                    run_id="run-1",
                    workspace_id="workspace-2",
                    user_id="user-1",
                    artifact_id="artifact-1",
                    content_hash="sha256:" + hashlib.sha256("# 문서\n".encode()).hexdigest(),
                    purpose="create_document",
                    document_id=None,
                    base_version=None,
                    target=None,
                    markdown="# 문서\n",
                )

    def test_create_run_writes_creation_artifact_before_planning_job(self) -> None:
        markdown = "# 문서\n"
        content_hash = "sha256:" + hashlib.sha256(markdown.encode()).hexdigest()
        run = AgentRun(
            id="run-1",
            workspace_id="workspace-1",
            user_id="user-1",
            action="workspace_workflow",
            skill_version_id=None,
            status="queued",
            request_summary="문서를 만들어줘",
        )
        artifact = StartAgentRunArtifact("artifact-1", content_hash, markdown)
        connection = MagicMock()
        run_result = MagicMock()
        run_result.fetchone.return_value = _run_row(
            status="queued", error_code=None, current_plan_id=None
        )
        connection.execute.side_effect = [run_result, MagicMock(), MagicMock()]
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection

        with (
            patch.object(database, "connect_ai", return_value=connection_context),
            patch(
                "app.modules.agent_run.infrastructure.postgres_agent_run_repository.write_text_object"
            ) as write_text,
        ):
            PostgresAgentRunRepository().create_with_planning_job(run, "job-1", artifact)

        write_text.assert_called_once()
        self.assertEqual(connection.execute.call_count, 3)
        self.assertIn("agent_runs", connection.execute.call_args_list[0].args[0])
        self.assertIn("agent_run_artifacts", connection.execute.call_args_list[1].args[0])
        self.assertIn("agent_jobs", connection.execute.call_args_list[2].args[0])
        self.assertEqual(
            connection.execute.call_args_list[1].args[1][1:4],
            ("run-1", "workspace-1", "user-1"),
        )
        self.assertNotIn(markdown, connection.execute.call_args_list[0].args[0])

    def test_create_run_deletes_creation_object_when_db_insert_fails(self) -> None:
        markdown = "# 문서\n"
        content_hash = "sha256:" + hashlib.sha256(markdown.encode()).hexdigest()
        run = AgentRun(
            id="run-1",
            workspace_id="workspace-1",
            user_id="user-1",
            action="workspace_workflow",
            skill_version_id=None,
            status="queued",
            request_summary="문서를 만들어줘",
        )
        artifact = StartAgentRunArtifact("artifact-1", content_hash, markdown)
        connection = MagicMock()
        run_result = MagicMock()
        run_result.fetchone.return_value = _run_row(
            status="queued", error_code=None, current_plan_id=None
        )
        connection.execute.side_effect = [run_result, MagicMock(), RuntimeError("db failure")]
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection

        with (
            patch.object(database, "connect_ai", return_value=connection_context),
            patch(
                "app.modules.agent_run.infrastructure.postgres_agent_run_repository.write_text_object"
            ) as write_text,
            patch(
                "app.modules.agent_run.infrastructure.postgres_agent_run_repository.delete_object"
            ) as delete_object,
        ):
            with self.assertRaisesRegex(RuntimeError, "db failure"):
                PostgresAgentRunRepository().create_with_planning_job(run, "job-1", artifact)

        delete_object.assert_called_once_with(write_text.call_args.args[0])

    def test_create_run_persists_edit_artifact_metadata(self) -> None:
        markdown = "# 문서\n\n수정 결과"
        content_hash = "sha256:" + hashlib.sha256(markdown.encode()).hexdigest()
        target = {"type": "selection", "start_line": 3, "end_line": 3}
        run = AgentRun(
            id="run-1",
            workspace_id="workspace-1",
            user_id="user-1",
            action="workspace_workflow",
            skill_version_id=None,
            status="queued",
            request_summary="문서를 수정해줘",
        )
        artifact = StartAgentRunArtifact(
            "artifact-1",
            content_hash,
            markdown,
            purpose="apply_document_edit",
            document_id="document-1",
            base_version=3,
            target=target,
        )
        connection = MagicMock()
        run_result = MagicMock()
        run_result.fetchone.return_value = _run_row(
            status="queued", error_code=None, current_plan_id=None
        )
        connection.execute.side_effect = [run_result, MagicMock(), MagicMock()]
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection

        with (
            patch.object(database, "connect_ai", return_value=connection_context),
            patch(
                "app.modules.agent_run.infrastructure.postgres_agent_run_repository.write_text_object"
            ),
        ):
            PostgresAgentRunRepository().create_with_planning_job(run, "job-1", artifact)

        parameters = connection.execute.call_args_list[1].args[1]
        self.assertEqual(parameters[5], "apply_document_edit")
        self.assertEqual(parameters[7:9], ("document-1", 3))
        self.assertEqual(parameters[9].obj, target)


def _run_row(
    *,
    status: str,
    error_code: str | None,
    current_plan_id: str | None,
    request_summary: str = "문서를 정리해줘",
) -> dict[str, object]:
    return {
        "id": "run-1",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "action": "folder_organize",
        "skill_version_id": None,
        "status": status,
        "request_summary": request_summary,
        "current_plan_id": current_plan_id,
        "error_code": error_code,
        "created_at": None,
        "updated_at": None,
        "finished_at": None,
    }


if __name__ == "__main__":
    unittest.main()

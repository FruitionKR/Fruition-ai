import hashlib
import unittest

from app.modules.agent.domain.exceptions import AgentConfigurationError
from app.modules.agent_run.application.start_agent_run import StartAgentRunUseCase
from app.modules.agent_run.domain.entities import AgentRun, StartAgentRunContent, StartAgentRunRequest


class RecordingRepository:
    def __init__(self) -> None:
        self.arguments: tuple[object, ...] | None = None

    def create_with_planning_job(self, *arguments: object) -> AgentRun:
        self.arguments = arguments
        run = arguments[0]
        assert isinstance(run, AgentRun)
        return run


class StartAgentRunTest(unittest.TestCase):
    def test_disabled_feature_is_configuration_error_without_creating_a_run(self) -> None:
        repository = RecordingRepository()
        with self.assertRaises(AgentConfigurationError):
            StartAgentRunUseCase(repository, feature_enabled=False).start(
                StartAgentRunRequest(
                    workspace_id="workspace-1", user_id="user-1",
                    instruction="현재까지 업로드 한 문서, 알맞은 폴더 이름 생성해서 주제별로 정리해 줘",
                )
            )
        self.assertIsNone(repository.arguments)

    def test_planner_receives_full_request_including_trailing_constraints(self) -> None:
        repository = RecordingRepository()
        instruction = "문서를 주제별로 정리해줘. " + "참고 내용 " * 200 + "기존 폴더 이름은 변경하지 마."
        StartAgentRunUseCase(repository).start(
            StartAgentRunRequest(
                workspace_id="workspace-1", user_id="user-1",
                instruction=instruction, provider="openai", model="gpt-5-nano",
            )
        )
        self.assertEqual(repository.arguments[0].request_summary, instruction)

    def test_creation_markdown_gets_generated_artifact_id_and_sha256(self) -> None:
        repository = RecordingRepository()
        markdown = "# 생성 문서\n"

        run_id, status = StartAgentRunUseCase(repository).start(  # type: ignore[arg-type]
            StartAgentRunRequest(
                workspace_id="workspace-1",
                user_id="user-1",
                instruction="문서를 만들어줘",
                action="workspace_workflow",
                provider="openai",
                model="gpt-5-nano",
                content=StartAgentRunContent(markdown=markdown),
            )
        )

        self.assertTrue(run_id)
        self.assertEqual(status, "queued")
        self.assertIsNotNone(repository.arguments)
        artifact = repository.arguments[2]  # type: ignore[index]
        self.assertTrue(getattr(artifact, "id"))
        self.assertEqual(
            getattr(artifact, "content_hash"),
            "sha256:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(getattr(artifact, "markdown"), markdown)

    def test_edit_markdown_keeps_approved_target_metadata(self) -> None:
        repository = RecordingRepository()
        markdown = "# 문서\n\n수정 결과"
        target = {"type": "selection", "start_line": 3, "end_line": 3}

        StartAgentRunUseCase(repository).start(  # type: ignore[arg-type]
            StartAgentRunRequest(
                workspace_id="workspace-1",
                user_id="user-1",
                instruction="수정안을 저장해줘",
                action="workspace_workflow",
                provider="openai",
                model="gpt-5-nano",
                content=StartAgentRunContent(
                    markdown=markdown,
                    purpose="apply_document_edit",
                    document_id="document-1",
                    base_version=3,
                    target=target,
                ),
            )
        )

        artifact = repository.arguments[2]  # type: ignore[index,union-attr]
        self.assertEqual(getattr(artifact, "purpose"), "apply_document_edit")
        self.assertEqual(getattr(artifact, "document_id"), "document-1")
        self.assertEqual(getattr(artifact, "base_version"), 3)
        self.assertEqual(getattr(artifact, "target"), target)


if __name__ == "__main__":
    unittest.main()

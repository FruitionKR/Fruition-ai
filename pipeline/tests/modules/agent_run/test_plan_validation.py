import copy
import unittest

from app.core.response_language import response_language_scope
from app.modules.agent_run.application.plan_validation import prepare_agent_plan
from app.modules.agent_run.domain.entities import ContentArtifactReference
from app.modules.agent_run.infrastructure.chat_completions_plan_generator import ChatCompletionsPlanGenerator


HIERARCHY = [
    {"id": "folder-9", "type": "folder", "current_version": 1, "parent_id": None},
    {"id": "folder-child", "type": "folder", "current_version": 1, "parent_id": "folder-9"},
    {"id": "document-1", "type": "document", "current_version": 3, "parent_id": "folder-9"},
]


class StaticPlanClient:
    def __init__(self, candidate: dict[str, object]) -> None:
        self.candidate = candidate
        self.calls = 0

    def complete_json(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        self.calls += 1
        return copy.deepcopy(self.candidate)


def _move_candidate(*, folder_id: object = "folder-child", depends_on: list[int] | None = None) -> dict[str, object]:
    return {
        "intent_confirmed": True,
        "summary": "문서를 옮깁니다.",
        "operations": [
            {
                "tool_name": "move_document",
                "target_type": "document",
                "target_id": "document-1",
                "base_version": 3,
                "source_parent_id": "folder-9",
                "destination_parent_id": folder_id if isinstance(folder_id, str) else None,
                "arguments": {
                    "document_id": "document-1",
                    "folder_id": folder_id,
                    "position": None,
                    "base_version": 3,
                },
                "reason": "분류 폴더로 옮깁니다.",
                "depends_on": depends_on or [],
            }
        ],
    }


def _edit_candidate() -> dict[str, object]:
    target = {"type": "selection", "start_line": 3, "end_line": 5}
    return {
        "summary": "문서를 수정합니다.",
        "operations": [
            {
                "tool_name": "apply_document_edit",
                "target_type": "document",
                "target_id": "document-1",
                "base_version": 3,
                "source_parent_id": "folder-9",
                "destination_parent_id": None,
                "arguments": {
                    "document_id": "document-1",
                    "base_version": 3,
                    "target": target,
                    "content_artifact_id": "artifact-1",
                    "content_hash": "sha256:trusted",
                },
                "reason": "선택한 문장을 수정합니다.",
                "depends_on": [],
            }
        ],
    }


def _prepare(
    candidate: dict[str, object],
    *,
    allowed_tools: set[str] | None = None,
    hierarchy: list[dict[str, object]] = HIERARCHY,
    content_artifacts: tuple[ContentArtifactReference, ...] = (),
    target_folder_id: str | None = None,
):
    return prepare_agent_plan(
        run_id="run-1",
        plan_id="plan-1",
        version=1,
        candidate=candidate,
        hierarchy=hierarchy,
        content_artifacts=content_artifacts,
        allowed_tools={"move_document"} if allowed_tools is None else allowed_tools,
        target_folder_id=target_folder_id,
    )


class PlanValidationTest(unittest.TestCase):
    def test_direct_preparation_matches_web_after_scope_and_dependency_renumbering(self) -> None:
        candidate = {
            "intent_confirmed": True,
            "summary": "폴더를 만들고 문서를 정리합니다.",
            "operations": [
                {
                    "tool_name": "rename_folder",
                    "target_type": "folder",
                    "target_id": "folder-9",
                    "base_version": 1,
                    "source_parent_id": None,
                    "destination_parent_id": None,
                    "arguments": {"folder_id": "folder-9", "name": "정리", "base_version": 1},
                    "reason": "대상 폴더 이름을 바꿉니다.",
                    "depends_on": [],
                },
                {
                    "tool_name": "create_folder",
                    "target_type": "folder",
                    "target_id": None,
                    "base_version": None,
                    "source_parent_id": None,
                    "destination_parent_id": None,
                    "arguments": {"name": "회의록", "parent_folder_id": None},
                    "reason": "분류 폴더를 만듭니다.",
                    "depends_on": [],
                },
                {
                    "tool_name": "move_document",
                    "target_type": "document",
                    "target_id": "document-1",
                    "base_version": 3,
                    "source_parent_id": "folder-9",
                    "destination_parent_id": None,
                    "arguments": {
                        "document_id": "document-1",
                        "folder_id": {"$operation_result": "plan-1-op-2", "field": "id"},
                        "position": None,
                        "base_version": 3,
                    },
                    "reason": "새 폴더로 옮깁니다.",
                    "depends_on": [2],
                },
            ],
        }
        allowed_tools = {"create_folder", "move_document"}
        direct = _prepare(
            copy.deepcopy(candidate),
            allowed_tools=allowed_tools,
            target_folder_id="folder-9",
        )
        client = StaticPlanClient(candidate)
        web = ChatCompletionsPlanGenerator(client, "system").generate(  # type: ignore[arg-type]
            run_id="run-1",
            plan_id="plan-1",
            version=1,
            instruction="이 폴더를 정리해줘",
            routing_action="folder_organize",
            hierarchy=HIERARCHY,
            skill_instructions=None,
            allowed_tools=tuple(allowed_tools),
            target_folder_id="folder-9",
        )

        self.assertEqual(client.calls, 1)
        self.assertEqual(direct, web)
        self.assertEqual([operation.id for operation in direct.operations], ["plan-1-op-1", "plan-1-op-2"])
        self.assertEqual(direct.operations[1].depends_on, ("plan-1-op-1",))
        self.assertEqual(
            direct.operations[1].arguments["folder_id"],
            {"$operation_result": "plan-1-op-1", "field": "id"},
        )
        self.assertEqual(direct.operation_hash, web.operation_hash)
        self.assertIn("작업 1개는 계획에서 뺐어요", direct.summary)

        with response_language_scope("en"):
            english = _prepare(
                copy.deepcopy(candidate),
                allowed_tools=allowed_tools,
                target_folder_id="folder-9",
            )
        self.assertIn("Removed 1 rename or move operations", english.summary)

    def test_rejects_forged_artifact_identity_hash_purpose_target_and_document(self) -> None:
        trusted_target = {"type": "selection", "start_line": 3, "end_line": 5}
        trusted = ContentArtifactReference(
            id="artifact-1",
            content_hash="sha256:trusted",
            purpose="apply_document_edit",
            document_id="document-1",
            base_version=3,
            target=trusted_target,
        )
        cases = {
            "id": ("content_artifact_id", "forged", trusted, "not supplied"),
            "hash": ("content_hash", "sha256:forged", trusted, "content_hash"),
            "purpose": (
                "content_artifact_id",
                "artifact-1",
                ContentArtifactReference(
                    id="artifact-1",
                    content_hash="sha256:trusted",
                    purpose="create_document",
                ),
                "purpose",
            ),
            "target": (
                "target",
                {"type": "selection", "start_line": 4, "end_line": 5},
                trusted,
                "approved target",
            ),
            "range": (
                "target",
                {"type": "selection", "start_line": 0, "end_line": 5},
                trusted,
                "target lines",
            ),
            "document": ("document_id", "document-2", trusted, "target id"),
        }
        for name, (argument, value, artifact, message) in cases.items():
            with self.subTest(name=name):
                candidate = _edit_candidate()
                operation = candidate["operations"][0]  # type: ignore[index]
                operation["arguments"][argument] = value  # type: ignore[index]
                with self.assertRaisesRegex(ValueError, message):
                    _prepare(
                        candidate,
                        allowed_tools={"apply_document_edit"},
                        content_artifacts=(artifact,),
                    )

    def test_rejects_untrusted_tool_hierarchy_destination_and_dependency(self) -> None:
        missing_target = _move_candidate()
        missing_target["operations"][0]["target_id"] = "missing"  # type: ignore[index]
        cases = (
            (
                "trusted mutation scope",
                _move_candidate(),
                {"rename_document"},
                HIERARCHY,
            ),
            (
                "target must exist",
                missing_target,
                {"move_document"},
                HIERARCHY,
            ),
            (
                "destination folder must exist",
                _move_candidate(folder_id="outside-folder"),
                {"move_document"},
                HIERARCHY,
            ),
            (
                "dependency must reference",
                _move_candidate(depends_on=[2]),
                {"move_document"},
                HIERARCHY,
            ),
        )
        for message, candidate, allowed_tools, hierarchy in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    _prepare(candidate, allowed_tools=allowed_tools, hierarchy=hierarchy)

    def test_enforces_one_to_twenty_operations(self) -> None:
        for count in (0, 21):
            with self.subTest(count=count):
                candidate = _move_candidate()
                candidate["operations"] = copy.deepcopy(candidate["operations"]) * count
                with self.assertRaisesRegex(ValueError, "between 1 and 20"):
                    _prepare(candidate)


if __name__ == "__main__":
    unittest.main()

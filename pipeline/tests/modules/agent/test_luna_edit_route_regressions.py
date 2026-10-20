"""gpt-6-luna 통합 테스트에서 실패한 편집 route·Skill 자리표시자 회귀 테스트."""

import unittest

from app.modules.agent.application.handle_agent_turn import HandleAgentTurnUseCase
from app.modules.agent.domain.entities import ActiveMarkdownContext, AgentTurnRequest, AgentTurnRoute
from app.modules.agent.domain.exceptions import AgentTurnRouteContractError
from app.modules.agent.infrastructure.chat_completions_turn_router import ChatCompletionsTurnRouter
from app.modules.markdown_edit.application.generate_markdown_document import GenerateMarkdownDocumentUseCase
from app.modules.markdown_edit.application.generate_markdown_edit import GenerateMarkdownEditUseCase
from app.modules.markdown_edit.domain.entities import (
    MarkdownEditOperation,
    MarkdownEditResult,
    MarkdownEditTarget,
)
from app.modules.skill.application.select_skill import SelectSkillUseCase
from app.modules.skill.domain.entities import Skill, SkillVersion
from app.modules.skill.domain.instruction_placeholders import (
    EDIT_PAYLOAD_DOCUMENT_REFERENCE,
    NO_SELECTION,
    REFERENCE_CONTEXT_DOCUMENT_REFERENCE,
)
from tests.modules.agent.test_chat_completions_turn_router import SequenceJsonClient
from tests.modules.agent.test_handle_agent_turn import (
    FakeQueryUseCase,
    FixedRouter,
    FixedSkillRepository,
    RecordingMarkdownEditor,
)

DOCUMENT = (
    "# 토양 수분 센서\n\n"
    "이 문서는 가상의 스마트팜 토양 센서 점검 기록이다.\n\n"
    "## 센서 기록\n\n"
    "운영자는 배액률을 함께 살핀다.\n"
)


def luna_insert_after_target_route(action: str = "markdown_edit") -> dict[str, object]:
    """luna가 "첫 문단 끝에 문장 추가"에 실제로 낸 route 모양(섹션 없는 insert_after+target)."""
    is_clarify = action == "clarify"
    return {
        "action": action,
        "confidence": 0.93,
        "retrieval_source": "none",
        "document_operation": "edit",
        "persist": False,
        "required_capabilities": [] if is_clarify else ["document-edit"],
        "edit_goal": "other",
        "edit_operation": "insert_after",
        "edit_destination": "target",
        "selected_skill_id": None,
        "skill_candidates": [],
        "reason": "첫 문단 끝에 문장을 추가하는 편집 요청입니다.",
    }


def document_request(target: MarkdownEditTarget | None = None) -> AgentTurnRequest:
    return AgentTurnRequest(
        message="이 문서의 첫 문단 끝에 '센서는 주 1회 보정한다.' 문장을 추가해 줘",
        active_markdown_context=ActiveMarkdownContext(markdown=DOCUMENT, target=target),
    )


class InsertAfterWithoutSectionRouteTest(unittest.TestCase):
    def test_repairs_luna_insert_after_target_to_replace_without_retry(self) -> None:
        for target in (None, MarkdownEditTarget(type="whole_document", start_line=1, end_line=8)):
            with self.subTest(target=target):
                client = SequenceJsonClient([luna_insert_after_target_route()])
                router = ChatCompletionsTurnRouter(client, "system")  # type: ignore[arg-type]

                route = router.route(document_request(target))

                self.assertEqual(route.action, "markdown_edit")
                self.assertEqual(route.edit_operation, "replace")
                self.assertEqual(route.edit_destination, "target")
                self.assertEqual(len(client.calls), 1)

    def test_repairs_selection_target_and_section_clarify(self) -> None:
        selection = MarkdownEditTarget(type="selection", start_line=3, end_line=3)
        client = SequenceJsonClient([luna_insert_after_target_route("clarify")])
        router = ChatCompletionsTurnRouter(client, "system")  # type: ignore[arg-type]

        route = router.route(document_request(selection))

        self.assertEqual(route.action, "markdown_edit")
        self.assertEqual(route.document_operation, "edit")
        self.assertEqual(route.required_capabilities, ("document-edit",))
        self.assertEqual(route.edit_operation, "replace")
        self.assertEqual(len(client.calls), 1)

    def test_keeps_insert_after_for_current_section(self) -> None:
        section = MarkdownEditTarget(type="current_section", start_line=5, end_line=8)
        client = SequenceJsonClient([luna_insert_after_target_route()])
        router = ChatCompletionsTurnRouter(client, "system")  # type: ignore[arg-type]

        route = router.route(document_request(section))

        self.assertEqual(route.edit_operation, "insert_after")
        self.assertEqual(route.edit_destination, "target")

    def test_without_markdown_keeps_contract_failure(self) -> None:
        client = SequenceJsonClient([luna_insert_after_target_route(), luna_insert_after_target_route()])
        router = ChatCompletionsTurnRouter(client, "system")  # type: ignore[arg-type]

        with self.assertRaises(AgentTurnRouteContractError):
            router.route(AgentTurnRequest(message="첫 문단 끝에 문장을 추가해 줘"))


def tag_skill() -> Skill:
    return Skill(
        id="skill-tag",
        workspace_id="workspace-1",
        scope_type="personal",
        owner_user_id="user-1",
        slug="tag-r3",
        status="enabled",
        enabled_version=SkillVersion(
            id="version-tag",
            skill_id="skill-tag",
            version=2,
            name="tag-r3",
            description="문서 끝에 태그 줄을 붙인다",
            instructions_markdown=(
                "문서 내용은 바꾸지 않는다. 문서 맨 끝에 다음 한 줄을 그대로 추가한다:\n"
                "TAG[{요청}] DOC[{문서}] SEL[{선택한 부분}] KEEP[{이름}]"
            ),
            capabilities=("document-create", "document-edit"),
            # 지침에 `{문서}`가 있어 저장소가 원문 필요(requires_source)로 읽는 Skill이다.
            requires_source=True,
            status="published",
        ),
    )


class SkillPlaceholderAgentTurnTest(unittest.TestCase):
    def _use_case(self, route: AgentTurnRoute) -> tuple[HandleAgentTurnUseCase, RecordingMarkdownEditor]:
        editor = RecordingMarkdownEditor(
            MarkdownEditResult(
                edit=MarkdownEditOperation(
                    operation="insert_after",
                    target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=8),
                    summary="문서 끝에 태그 줄을 추가했습니다.",
                    replacement_markdown="\nTAG[태그 줄을 붙여 줘]",
                )
            )
        )
        use_case = HandleAgentTurnUseCase(
            router=FixedRouter(route),
            query_use_case=FakeQueryUseCase(),  # type: ignore[arg-type]
            markdown_edit_use_case=GenerateMarkdownEditUseCase(editor),
            markdown_create_use_case=GenerateMarkdownDocumentUseCase(editor),
            skill_selector=SelectSkillUseCase(FixedSkillRepository(tag_skill())),  # type: ignore[arg-type]
        )
        return use_case, editor

    def test_edit_substitutes_placeholders_and_references_document(self) -> None:
        use_case, editor = self._use_case(
            AgentTurnRoute(
                action="markdown_edit",
                confidence=0.95,
                reason="문서 끝 태그 줄",
                edit_goal="other",
                edit_operation="insert_after",
                edit_destination="document_end",
                selected_skill_id="skill-tag",
                document_operation="edit",
                required_capabilities=("document-edit",),
            )
        )

        use_case.execute(
            AgentTurnRequest(
                message="/tag-r3 태그 줄을 붙여 줘",
                workspace_id="workspace-1",
                user_id="user-1",
                active_markdown_context=ActiveMarkdownContext(markdown=DOCUMENT),
            )
        )

        instructions = editor.requests[0].skill_instructions or ""
        self.assertIn(
            f"TAG[태그 줄을 붙여 줘] DOC[{EDIT_PAYLOAD_DOCUMENT_REFERENCE}] SEL[{NO_SELECTION}] KEEP[{{이름}}]",
            instructions,
        )
        self.assertNotIn("배액률", instructions)

    def test_selection_fills_selected_text(self) -> None:
        use_case, editor = self._use_case(
            AgentTurnRoute(
                action="markdown_edit",
                confidence=0.95,
                reason="선택 부분 편집",
                edit_goal="other",
                edit_operation="replace",
                edit_destination="target",
                selected_skill_id="skill-tag",
                document_operation="edit",
                required_capabilities=("document-edit",),
            )
        )
        editor.result = MarkdownEditResult(
            edit=MarkdownEditOperation(
                operation="replace",
                target=MarkdownEditTarget(type="selection", start_line=3, end_line=3),
                summary="선택 부분을 바꿨습니다.",
                replacement_markdown="바뀐 문장",
            )
        )

        use_case.execute(
            AgentTurnRequest(
                message="/tag-r3 선택한 문장에 적용해 줘",
                workspace_id="workspace-1",
                user_id="user-1",
                active_markdown_context=ActiveMarkdownContext(
                    markdown=DOCUMENT,
                    target=MarkdownEditTarget(type="selection", start_line=3, end_line=3),
                ),
            )
        )

        self.assertIn(
            "SEL[이 문서는 가상의 스마트팜 토양 센서 점검 기록이다.]",
            editor.requests[0].skill_instructions or "",
        )

    def test_create_carries_active_document_as_source_and_references_it(self) -> None:
        use_case, editor = self._use_case(
            AgentTurnRoute(
                action="markdown_create",
                confidence=0.95,
                reason="새 문서",
                edit_goal="create_from_chat",
                selected_skill_id="skill-tag",
                document_operation="create",
                required_capabilities=("document-create",),
            )
        )

        use_case.execute(
            AgentTurnRequest(
                message="/tag-r3 새 문서로 만들어 줘",
                workspace_id="workspace-1",
                user_id="user-1",
                active_markdown_context=ActiveMarkdownContext(markdown=DOCUMENT),
            )
        )

        # 명시한 Skill의 새 문서는 열린 문서를 원문(reference_context.source_document)으로 한 번만 싣는다.
        create_request = editor.create_requests[0]
        instructions = create_request.skill_instructions or ""
        self.assertEqual(create_request.reference_context["source_document"]["markdown"], DOCUMENT)  # type: ignore[index]
        self.assertIn(f"DOC[{REFERENCE_CONTEXT_DOCUMENT_REFERENCE}]", instructions)
        self.assertNotIn("배액률", instructions)
        self.assertIn("TAG[새 문서로 만들어 줘]", instructions)

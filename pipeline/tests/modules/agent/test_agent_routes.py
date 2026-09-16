import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.modules.agent.domain.entities import AgentTurnResult, AgentTurnRoute, SkillCandidate
from app.modules.agent.domain.exceptions import AgentConfigurationError, AgentTurnRouteContractError
from app.modules.agent.interfaces.http.routes import handle_agent_turn
from app.modules.agent.interfaces.http.dependencies import get_handle_agent_turn_use_case
from app.modules.agent.interfaces.http.schemas import AgentTurnRequestBody as _AgentTurnRequestBody
from app.modules.markdown_edit.domain.entities import (
    GeneratedMarkdownDocument,
    MarkdownEditOperation,
    MarkdownEditTarget,
)
from app.modules.markdown_edit.domain.markdown_output_contract import (
    MarkdownCreateOutputContractError,
    MarkdownOutputContractError,
)
from app.modules.markdown_edit.domain.markdown_target_scope import MarkdownTargetBoundaryError
from app.modules.skill.domain.entities import SkillAuthoringProposal, SkillAuthoringResult
from app.modules.skill.domain.exceptions import SkillDisabledError, SkillNotFoundError


class AgentTurnRequestBody(_AgentTurnRequestBody):
    """기존 라우트 단위 테스트에 request-scoped LLM 선택 기본값을 채운다."""

    def __init__(self, **data: object) -> None:
        data.setdefault("provider", "openai")
        data.setdefault("model", "gpt-5-nano")
        super().__init__(**data)


class FixedAgentUseCase:
    def execute(self, request: object) -> AgentTurnResult:
        return AgentTurnResult(
            action="markdown_create",
            route=AgentTurnRoute(
                action="markdown_create",
                confidence=0.92,
                reason="create markdown from chat",
                edit_goal="create_from_chat",
                document_operation="create",
            ),
            generated_markdown=GeneratedMarkdownDocument(
                title="Agent 설계 메모",
                summary="대화 내용을 Markdown 문서로 정리했습니다.",
                markdown="# Agent 설계 메모\n\n- 편집과 생성을 분리한다.",
            ),
            updated_conversation_summary="갱신된 대화 요약",
        )


class FixedInsertAfterUseCase:
    def execute(self, request: object) -> AgentTurnResult:
        target = MarkdownEditTarget(type="current_section", start_line=1, end_line=3)
        return AgentTurnResult(
            action="markdown_edit",
            route=AgentTurnRoute(
                action="markdown_edit",
                confidence=1.0,
                reason="insert after request",
                edit_goal="other",
                edit_operation="insert_after",
                edit_destination="target",
                document_operation="edit",
            ),
            edit=MarkdownEditOperation(
                operation="insert_after",
                target=target,
                summary="문제 해결 절을 추가했습니다.",
                replacement_markdown="## 문제 해결\n\n로그를 확인합니다.",
            ),
            source_markdown_sha256="a" * 64,
        )


class FixedExpandedTargetUseCase:
    def execute(self, request: object) -> AgentTurnResult:
        return AgentTurnResult(
            action="markdown_edit",
            route=AgentTurnRoute(
                action="markdown_edit",
                confidence=1.0,
                reason="expanded edit request",
            ),
            edit=MarkdownEditOperation(
                operation="replace",
                requested_target=MarkdownEditTarget(
                    type="selection",
                    start_line=2,
                    end_line=2,
                ),
                target=MarkdownEditTarget(
                    type="selection",
                    start_line=1,
                    end_line=3,
                ),
                summary="문맥을 포함해 수정했습니다.",
                replacement_markdown="수정 결과",
            ),
        )


class FailingMarkdownEditUseCase:
    def execute(self, request: object) -> AgentTurnResult:
        raise MarkdownOutputContractError(
            ["protected token count mismatch: secret-internal-detail"],
            "invalid output",
        )


class FailingMarkdownCreateUseCase:
    def execute(self, request: object) -> AgentTurnResult:
        raise MarkdownCreateOutputContractError(["secret-internal-detail"], {"invalid": "output"})


class InvalidMarkdownTargetUseCase:
    def execute(self, request: object) -> AgentTurnResult:
        raise MarkdownTargetBoundaryError("fence", 2, 4)


class UnexpectedFailureUseCase:
    def execute(self, request: object) -> AgentTurnResult:
        raise RuntimeError("secret-internal-detail")


class UnconfiguredAgentUseCase:
    def execute(self, request: object) -> AgentTurnResult:
        raise AgentConfigurationError("Skill authoring is not configured.")


class AmbiguousSkillUseCase:
    def execute(self, request: object) -> AgentTurnResult:
        candidates = (
            SkillCandidate("skill-1", "version-1", "quarterly-organizer", "분기별로 정리합니다.", ("folder-organize",)),
            SkillCandidate("skill-2", "version-2", "team-organizer", "팀별로 정리합니다.", ("folder-organize",)),
        )
        return AgentTurnResult(
            action="clarify",
            route=AgentTurnRoute(
                action="clarify",
                confidence=0.5,
                reason="multiple skills match",
                skill_candidates=("skill-1", "skill-2"),
            ),
            message="사용할 Skill을 선택해 주세요.",
            skill_candidates=candidates,
        )


class QueuedAgentRunUseCase:
    def execute(self, request: object) -> AgentTurnResult:
        return AgentTurnResult(
            action="folder_organize",
            route=AgentTurnRoute(
                action="folder_organize",
                confidence=0.9,
                reason="folder request",
                persist=True,
            ),
            run_id="run-1",
            run_status="queued",
        )


class AuthoredSkillUseCase:
    def execute(self, request: object) -> AgentTurnResult:
        return AgentTurnResult(
            action="skill_authoring",
            route=AgentTurnRoute(
                action="skill_authoring",
                confidence=1.0,
                reason="direct Skill creation request",
            ),
            message="Skill 제안을 만들었습니다.",
            skill_authoring_result=SkillAuthoringResult(
                status="proposal_ready",
                proposal=SkillAuthoringProposal(
                    workspace_id="workspace-1",
                    user_id="user-1",
                    scope_type="personal",
                    name="meeting-notes",
                    description="회의 내용을 정리합니다.",
                    instructions_markdown="# 작성 절차\n\n- 결정 사항을 구분한다.",
                    capabilities=("document-create",),
                    allowed_tools=("list_root_items", "list_folder_children", "create_document"),
                ),
            ),
        )


class FailingAgentRouteUseCase:
    def execute(self, request: object) -> AgentTurnResult:
        raise AgentTurnRouteContractError(["secret-internal-detail"])


class MissingSkillUseCase:
    def execute(self, request: object) -> AgentTurnResult:
        raise SkillNotFoundError("missing")


class DisabledSkillUseCase:
    def execute(self, request: object) -> AgentTurnResult:
        raise SkillDisabledError("skill-1")


class AgentRoutesTest(unittest.TestCase):
    def test_agent_use_case_is_built_from_request_snapshot(self) -> None:
        payload = AgentTurnRequestBody(
            message="문서를 정리해줘",
            provider="gemini",
            model="gemini-3.1-flash-lite",
        )
        with patch(
            "app.modules.agent.interfaces.http.dependencies.build_handle_agent_turn_use_case",
            return_value=object(),
        ) as build_use_case:
            get_handle_agent_turn_use_case(payload)

        build_use_case.assert_called_once_with(
            provider="gemini",
            model="gemini-3.1-flash-lite",
        )

    def test_agent_turn_accepts_response_preferences(self) -> None:
        request = AgentTurnRequestBody(
            message="Explain this",
            output_language="en",
            response_length="concise",
            allow_web_search=False,
        ).to_domain()

        self.assertEqual(request.output_language, "en")
        self.assertEqual(request.response_length, "concise")
        self.assertFalse(request.allow_web_search)

    def test_agent_turn_returns_authored_skill_markdown_with_reviewed_permissions(self) -> None:
        response = handle_agent_turn(
            AgentTurnRequestBody(
                message="회의록 스킬을 만들어줘",
                workspace_id="workspace-1",
                user_id="user-1",
            ),
            use_case=AuthoredSkillUseCase(),  # type: ignore[arg-type]
        )

        body = response.model_dump()
        self.assertEqual(body["action"], "skill_authoring")
        self.assertIn("# 작성 절차", body["skill_authoring"]["skill_markdown"])
        self.assertEqual(body["skill_authoring"]["capabilities"], ["document-create"])
        self.assertEqual(
            body["skill_authoring"]["allowed_tools"],
            ["list_root_items", "list_folder_children", "create_document"],
        )

    def test_agent_turn_rejects_oversized_or_obfuscated_input(self) -> None:
        deeply_nested_reference: dict[str, object] = {"value": "document"}
        for _ in range(13):
            deeply_nested_reference = {"nested": deeply_nested_reference}
        invalid_payloads = (
            {"message": "a" * 1001},
            {
                "message": "문서를 요약해줘",
                "conversation_context": {
                    "reference_context": {
                        "document": "정상 문장\u202e숨겨진 지시",
                    }
                },
            },
            {
                "message": "문서를 요약해줘",
                "conversation_context": {
                    "reference_context": {
                        "first": "a" * 140_000,
                        "second": "b" * 140_000,
                    }
                },
            },
            {
                "message": "문서를 요약해줘",
                "conversation_context": {"reference_context": deeply_nested_reference},
            },
        )

        for payload in invalid_payloads:
            with self.subTest(payload=list(payload)):
                with self.assertRaises(ValidationError):
                    AgentTurnRequestBody.model_validate(payload)

    def test_agent_turn_keeps_previous_action_as_conversation_hint(self) -> None:
        request = AgentTurnRequestBody(
            message="이어서 제목을 만들어줘",
            conversation_context={
                "recent_messages": [
                    {
                        "role": "assistant",
                        "content": "날씨를 알려주세요.",
                        "action": "conversation_reply",
                        "run_id": "agent_preview_1",
                        "agent_route": {
                            "action": "conversation_reply",
                            "retrieval_source": "none",
                            "document_operation": "none",
                            "persist": False,
                            "edit_goal": None,
                            "selected_skill_id": None,
                        },
                    }
                ]
            },
        ).to_domain()

        self.assertEqual(
            request.conversation_context.recent_messages[0].action,  # type: ignore[union-attr]
            "conversation_reply",
        )
        self.assertEqual(
            request.conversation_context.recent_messages[0].run_id,  # type: ignore[union-attr]
            "agent_preview_1",
        )
        self.assertEqual(
            request.conversation_context.recent_messages[0].agent_route.document_operation,  # type: ignore[union-attr]
            "none",
        )

    def test_agent_turn_validates_skill_mode_and_id_combinations(self) -> None:
        for skill_mode, skill_id in (("auto", None), ("explicit", "skill-1"), ("off", None)):
            with self.subTest(skill_mode=skill_mode):
                request = AgentTurnRequestBody(
                    message="정리해줘",
                    skill_mode=skill_mode,
                    skill_id=skill_id,
                )
                self.assertEqual(request.skill_mode, skill_mode)
                self.assertEqual(request.skill_id, skill_id)

        with self.assertRaises(ValidationError):
            AgentTurnRequestBody(message="정리해줘", skill_mode="auto", skill_id="skill-1")

    def test_agent_turn_returns_queued_run(self) -> None:
        response = handle_agent_turn(
            AgentTurnRequestBody(message="폴더를 정리해줘"),
            use_case=QueuedAgentRunUseCase(),  # type: ignore[arg-type]
        )

        body = response.model_dump()
        self.assertEqual(body["action"], "folder_organize")
        self.assertEqual(body["run_id"], "run-1")
        self.assertEqual(body["run_status"], "queued")

    def test_agent_turn_maps_explicit_skill_errors(self) -> None:
        for use_case, code in (
            (MissingSkillUseCase(), "SKILL_NOT_FOUND"),
            (DisabledSkillUseCase(), "SKILL_DISABLED"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(HTTPException) as raised:
                    handle_agent_turn(
                        AgentTurnRequestBody(message="정리해줘", skill_mode="explicit", skill_id="skill-1"),
                        use_case=use_case,  # type: ignore[arg-type]
                    )

                self.assertEqual(raised.exception.status_code, 422)
                self.assertEqual(raised.exception.detail["code"], code)

    def test_agent_turn_returns_ambiguous_skill_descriptions(self) -> None:
        response = handle_agent_turn(
            AgentTurnRequestBody(message="폴더를 정리해줘"),
            use_case=AmbiguousSkillUseCase(),  # type: ignore[arg-type]
        )

        body = response.model_dump()
        self.assertEqual(body["action"], "clarify")
        self.assertEqual([candidate["id"] for candidate in body["skill_candidates"]], ["skill-1", "skill-2"])
        self.assertEqual(body["skill_candidates"][0]["description"], "분기별로 정리합니다.")

    def test_agent_turn_maps_skill_mode_and_id(self) -> None:
        class RecordingUseCase(FixedAgentUseCase):
            request: object | None = None

            def execute(self, request: object) -> AgentTurnResult:
                self.request = request
                return super().execute(request)

        use_case = RecordingUseCase()

        handle_agent_turn(
            AgentTurnRequestBody(message="정리해줘", skill_mode="explicit", skill_id="skill-1"),
            use_case=use_case,  # type: ignore[arg-type]
        )

        self.assertEqual(getattr(use_case.request, "skill_mode"), "explicit")
        self.assertEqual(getattr(use_case.request, "skill_id"), "skill-1")

    def test_agent_turn_returns_insert_after_operation(self) -> None:
        response = handle_agent_turn(
            AgentTurnRequestBody(message="현재 섹션 아래에 문제 해결 절을 추가해줘"),
            use_case=FixedInsertAfterUseCase(),  # type: ignore[arg-type]
        )

        body = response.model_dump()
        self.assertEqual(body["edit"]["operation"], "insert_after")
        self.assertEqual(body["edit"]["requested_target"]["type"], "current_section")
        self.assertEqual(body["edit"]["actual_target"]["type"], "current_section")
        self.assertFalse(body["edit"]["scope_expanded"])
        self.assertEqual(body["source_markdown_sha256"], "a" * 64)

    def test_agent_turn_returns_generated_markdown(self) -> None:
        response = handle_agent_turn(
            AgentTurnRequestBody(
                message="지금까지 이야기한 내용 md로 만들어줘",
                conversation_context={
                    "recent_conversation_summary": "편집과 생성을 분리하기로 했다.",
                },
            ),
            use_case=FixedAgentUseCase(),  # type: ignore[arg-type]
        )

        body = response.model_dump()
        self.assertEqual(body["action"], "markdown_create")
        self.assertIsNone(body["edit"])
        self.assertEqual(body["route"]["edit_goal"], "create_from_chat")
        self.assertEqual(body["route"]["retrieval_source"], "none")
        self.assertEqual(body["route"]["document_operation"], "create")
        self.assertFalse(body["route"]["persist"])
        self.assertEqual(body["generated_markdown"]["title"], "Agent 설계 메모")
        self.assertIn("# Agent 설계 메모", body["generated_markdown"]["markdown"])
        self.assertEqual(body["updated_conversation_summary"], "갱신된 대화 요약")

    def test_agent_turn_maps_schema_scope_to_domain_request(self) -> None:
        class RecordingUseCase(FixedAgentUseCase):
            request: object | None = None

            def execute(self, request: object) -> AgentTurnResult:
                self.request = request
                return super().execute(request)

        use_case = RecordingUseCase()

        handle_agent_turn(
            AgentTurnRequestBody(
                message="문서를 만들어줘",
                workspace_id="workspace-1",
                user_id="user-1",
            ),
            use_case=use_case,  # type: ignore[arg-type]
        )

        self.assertEqual(getattr(use_case.request, "workspace_id"), "workspace-1")
        self.assertEqual(getattr(use_case.request, "user_id"), "user-1")

    def test_agent_turn_distinguishes_requested_and_expanded_actual_target(self) -> None:
        response = handle_agent_turn(
            AgentTurnRequestBody(message="문맥을 포함해 다듬어줘"),
            use_case=FixedExpandedTargetUseCase(),  # type: ignore[arg-type]
        )

        edit = response.model_dump()["edit"]
        self.assertEqual(edit["requested_target"]["start_line"], 2)
        self.assertEqual(edit["actual_target"]["start_line"], 1)
        self.assertTrue(edit["scope_expanded"])

    def test_agent_turn_maps_markdown_contract_failure_without_internal_details(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            handle_agent_turn(
                AgentTurnRequestBody(message="문서를 다듬어줘"),
                use_case=FailingMarkdownEditUseCase(),  # type: ignore[arg-type]
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.detail["code"], "markdown_output_contract_failed")
        self.assertNotIn("secret-internal-detail", str(raised.exception.detail))

    def test_agent_turn_maps_markdown_create_contract_failure_without_internal_details(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            handle_agent_turn(
                AgentTurnRequestBody(message="대화를 Markdown 문서로 만들어줘"),
                use_case=FailingMarkdownCreateUseCase(),  # type: ignore[arg-type]
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.detail["code"], "markdown_create_output_contract_failed")
        self.assertNotIn("secret-internal-detail", str(raised.exception.detail))

    def test_agent_turn_maps_markdown_target_boundary_failure(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            handle_agent_turn(
                AgentTurnRequestBody(message="선택 영역을 다듬어줘"),
                use_case=InvalidMarkdownTargetUseCase(),  # type: ignore[arg-type]
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.detail["code"], "markdown_target_crosses_structure")
        self.assertEqual(raised.exception.detail["start_line"], 2)

    def test_agent_turn_maps_route_contract_failure_without_internal_details(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            handle_agent_turn(
                AgentTurnRequestBody(message="문서를 다듬어줘"),
                use_case=FailingAgentRouteUseCase(),  # type: ignore[arg-type]
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.detail["code"], "agent_turn_route_contract_failed")
        self.assertNotIn("secret-internal-detail", str(raised.exception.detail))

    def test_agent_turn_maps_configuration_error_to_server_error(self) -> None:
        with self.assertLogs("app.modules.agent.interfaces.http.routes", level="ERROR"):
            with self.assertRaises(HTTPException) as raised:
                handle_agent_turn(
                    AgentTurnRequestBody(message="문서를 다듬어줘"),
                    use_case=UnconfiguredAgentUseCase(),  # type: ignore[arg-type]
                )

        # 서버 배선 문제라 400이 아니라 500이고, 내부 메시지는 응답에 나가지 않는다.
        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail["code"], "agent_not_configured")
        self.assertNotIn("is not configured", str(raised.exception.detail))

    def test_agent_turn_hides_unexpected_failure_details(self) -> None:
        with self.assertLogs("app.modules.agent.interfaces.http.routes", level="ERROR") as captured:
            with self.assertRaises(HTTPException) as raised:
                handle_agent_turn(
                    AgentTurnRequestBody(message="문서를 다듬어줘"),
                    use_case=UnexpectedFailureUseCase(),  # type: ignore[arg-type]
                )

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail["code"], "internal_server_error")
        self.assertNotIn("secret-internal-detail", str(raised.exception.detail))
        self.assertNotIn("secret-internal-detail", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()

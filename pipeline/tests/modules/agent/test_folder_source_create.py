"""원문이 필요한 생성 Skill(requires_source)을 폴더·문서에 실행할 때의 원문 구성."""

import json
import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from app.modules.agent.application.handle_agent_turn import HandleAgentTurnUseCase
from app.modules.agent.domain.entities import (
    ActiveMarkdownContext,
    AgentConversationContext,
    AgentTurnRequest,
    AgentTurnRoute,
)
from app.modules.agent.domain.folder_sources import (
    MAX_FOLDER_SOURCE_CHARS,
    MAX_FOLDER_SOURCE_DOCUMENTS,
    FolderSourceDocument,
    FolderSourceListing,
    FolderSourceUnavailableError,
    folder_source_notice,
    select_folder_sources,
)
from app.modules.agent.infrastructure.backend_folder_source_reader import (
    FOLDER_NOT_ACCESSIBLE_MESSAGE,
    BackendFolderSourceReader,
    build_folder_source_reader,
)
from app.modules.markdown_edit.application.generate_markdown_document import (
    GenerateMarkdownDocumentUseCase,
)
from app.modules.markdown_edit.application.generate_markdown_edit import (
    GenerateMarkdownEditUseCase,
)
from app.modules.markdown_edit.domain.entities import (
    MarkdownEditOperation,
    MarkdownEditResult,
    MarkdownEditTarget,
)
from app.modules.query.domain.entities import ConversationMessage
from app.modules.skill.application.select_skill import SelectSkillUseCase
from app.modules.skill.domain.entities import Skill
from app.modules.skill.domain.instruction_placeholders import REFERENCE_CONTEXT_FOLDER_REFERENCE
from tests.modules.agent.test_handle_agent_turn import (
    CREATE_ROUTE,
    OPTIONS_MARKDOWN,
    FakeQueryUseCase,
    FixedRouter,
    FixedSkillRepository,
    RecordingAgentRunStarter,
    RecordingMarkdownEditor,
    options_skill,
)


def _document(index: int, markdown: str | None = None, updated_at: str | None = None) -> FolderSourceDocument:
    return FolderSourceDocument(
        document_id=f"doc-{index}",
        title=f"문서 {index}",
        markdown=markdown if markdown is not None else f"# 문서 {index}\n\n본문 {index}",
        updated_at=updated_at or f"2026-10-{index:02d}T00:00:00Z",
    )


class RecordingFolderSourceReader:
    def __init__(self, listing: FolderSourceListing | Exception) -> None:
        self.listing = listing
        self.calls: list[dict[str, object]] = []

    def read(self, **kwargs: object) -> FolderSourceListing:
        self.calls.append(kwargs)
        if isinstance(self.listing, Exception):
            raise self.listing
        return self.listing


class SelectFolderSourcesTest(unittest.TestCase):
    def test_orders_by_recent_update_and_limits_document_count(self) -> None:
        listing = FolderSourceListing(
            total_documents=25,
            documents=tuple(_document(index) for index in range(1, 26)),
        )

        selected = select_folder_sources(listing)

        self.assertEqual(selected.read_documents, MAX_FOLDER_SOURCE_DOCUMENTS)
        self.assertEqual(selected.documents[0].document_id, "doc-25")
        self.assertEqual(selected.documents[-1].document_id, "doc-6")
        self.assertEqual(
            folder_source_notice(selected),
            "폴더 문서 25개 중 최근 수정한 20개만 읽었어요. 나머지 문서는 초안에 반영되지 않았어요.",
        )

    def test_stops_at_total_character_budget(self) -> None:
        half = "가" * (MAX_FOLDER_SOURCE_CHARS // 2)
        listing = FolderSourceListing(
            total_documents=3,
            documents=(_document(3, half), _document(2, half), _document(1, "짧은 문서")),
        )

        selected = select_folder_sources(listing)

        self.assertEqual([document.document_id for document in selected.documents], ["doc-3", "doc-2"])
        self.assertIn("3개 중 최근 수정한 2개만", folder_source_notice(selected) or "")

    def test_clips_single_oversized_document_instead_of_reading_nothing(self) -> None:
        listing = FolderSourceListing(
            total_documents=1,
            documents=(_document(1, "나" * (MAX_FOLDER_SOURCE_CHARS + 10)),),
        )

        selected = select_folder_sources(listing)

        self.assertEqual(selected.truncated_document_ids, ("doc-1",))
        self.assertLessEqual(len(selected.documents[0].markdown), MAX_FOLDER_SOURCE_CHARS + 20)
        self.assertIn("일부 내용만", folder_source_notice(selected) or "")

    def test_complete_read_has_no_notice_and_skips_blank_documents(self) -> None:
        listing = FolderSourceListing(total_documents=2, documents=(_document(1), _document(2, "   ")))

        selected = select_folder_sources(listing)

        self.assertEqual([document.document_id for document in selected.documents], ["doc-1"])
        # 빈 문서도 읽을 수 있는 문서 수에는 들어가므로 일부만 읽은 것으로 안내한다.
        self.assertTrue(selected.is_partial)
        complete = select_folder_sources(FolderSourceListing(total_documents=1, documents=(_document(1),)))
        self.assertIsNone(folder_source_notice(complete))


class FolderSourceCreateTest(unittest.TestCase):
    def _use_case(
        self,
        skill: Skill,
        reader: RecordingFolderSourceReader | None,
        route: AgentTurnRoute | None = None,
    ) -> tuple[HandleAgentTurnUseCase, RecordingMarkdownEditor]:
        editor = RecordingMarkdownEditor(
            MarkdownEditResult(
                edit=MarkdownEditOperation(
                    operation="replace",
                    target=MarkdownEditTarget(type="whole_document", start_line=1, end_line=5),
                    summary="unused",
                    replacement_markdown="unused",
                )
            )
        )
        use_case = HandleAgentTurnUseCase(
            router=FixedRouter(route or AgentTurnRoute(action="conversation_reply", confidence=1.0, reason="empty")),
            query_use_case=FakeQueryUseCase(),  # type: ignore[arg-type]
            markdown_edit_use_case=GenerateMarkdownEditUseCase(editor),
            markdown_create_use_case=GenerateMarkdownDocumentUseCase(editor),
            skill_selector=SelectSkillUseCase(FixedSkillRepository(skill)),  # type: ignore[arg-type]
            agent_run_starter=RecordingAgentRunStarter(),  # type: ignore[arg-type]
            folder_source_reader=reader,
        )
        return use_case, editor

    def _request(self, *, message: str = "/brief", document: bool = False, **values: object) -> AgentTurnRequest:
        document_values: dict[str, object] = (
            {
                "active_markdown_context": ActiveMarkdownContext(markdown=OPTIONS_MARKDOWN),
                "document_id": "document-1",
                "base_version": 3,
            }
            if document
            else {}
        )
        return AgentTurnRequest(
            message=message,
            workspace_id="workspace-1",
            user_id="user-1",
            mode="task",
            **document_values,  # type: ignore[arg-type]
            **values,  # type: ignore[arg-type]
        )

    def _source_skill(self, requires_source: bool = True) -> Skill:
        skill = options_skill(("document-create",), (), "create", requires_source=requires_source)
        assert skill.enabled_version is not None
        return replace(
            skill,
            enabled_version=replace(skill.enabled_version, instructions_markdown="{문서}를 새 문서로 정리한다."),
        )

    def test_bare_create_on_folder_reads_folder_documents_as_source(self) -> None:
        reader = RecordingFolderSourceReader(
            FolderSourceListing(total_documents=2, documents=(_document(1), _document(2)))
        )
        use_case, editor = self._use_case(self._source_skill(), reader)

        result = use_case.execute(self._request(folder_id="folder-9"))

        self.assertEqual(result.action, "markdown_create")
        self.assertEqual(
            reader.calls,
            [{"workspace_id": "workspace-1", "user_id": "user-1", "folder_id": "folder-9", "max_documents": 20}],
        )
        create_request = editor.create_requests[0]
        sources = create_request.reference_context["source_documents"]  # type: ignore[index]
        self.assertEqual([source["document_id"] for source in sources], ["doc-2", "doc-1"])
        self.assertEqual(sources[0]["markdown"], "# 문서 2\n\n본문 2")
        self.assertNotIn("source_document", create_request.reference_context or {})
        self.assertIn(REFERENCE_CONTEXT_FOLDER_REFERENCE, create_request.skill_instructions or "")
        self.assertIn("고른 폴더의 문서들을 원문으로", create_request.instruction)
        self.assertNotIn("만 읽었어요", result.message or "")

    def test_partial_folder_read_is_reported_in_result_message(self) -> None:
        reader = RecordingFolderSourceReader(
            FolderSourceListing(total_documents=30, documents=tuple(_document(index) for index in range(1, 21)))
        )
        use_case, _ = self._use_case(self._source_skill(), reader)

        result = use_case.execute(self._request(folder_id="folder-9"))

        self.assertIn("폴더 문서 30개 중 최근 수정한 20개만 읽었어요.", result.message or "")

    def test_bare_create_does_not_mix_previous_conversation_into_source(self) -> None:
        reader = RecordingFolderSourceReader(FolderSourceListing(total_documents=1, documents=(_document(1),)))
        use_case, editor = self._use_case(self._source_skill(), reader)
        conversation = AgentConversationContext(
            recent_conversation_summary="앞 턴의 회의 내용: 출시 일정 논의",
            recent_messages=(ConversationMessage(role="user", content="회의 내용을 정리했어"),),
            reference_context={"referents": {"회의": "출시 일정"}},
        )

        use_case.execute(self._request(folder_id="folder-9", conversation_context=conversation))

        create_request = editor.create_requests[0]
        self.assertIsNone(create_request.conversation_summary)
        self.assertEqual(set((create_request.reference_context or {}).keys()), {"source_documents"})

    def test_create_with_task_text_on_folder_also_reads_folder(self) -> None:
        reader = RecordingFolderSourceReader(FolderSourceListing(total_documents=1, documents=(_document(1),)))
        use_case, editor = self._use_case(self._source_skill(), reader, CREATE_ROUTE)

        result = use_case.execute(self._request(message="/brief 주간 보고로 묶어 줘", folder_id="folder-9"))

        self.assertEqual(result.action, "markdown_create")
        self.assertEqual(len(reader.calls), 1)
        self.assertIn("source_documents", editor.create_requests[0].reference_context or {})

    def test_document_target_wins_over_folder_for_source_skill(self) -> None:
        reader = RecordingFolderSourceReader(FolderSourceListing(total_documents=1, documents=(_document(1),)))
        use_case, editor = self._use_case(self._source_skill(), reader)

        use_case.execute(self._request(document=True, folder_id="folder-parent"))

        self.assertEqual(reader.calls, [])
        self.assertEqual(
            editor.create_requests[0].reference_context["source_document"]["document_id"],  # type: ignore[index]
            "document-1",
        )

    def test_skill_without_source_on_folder_creates_by_location_only(self) -> None:
        reader = RecordingFolderSourceReader(FolderSourceListing(total_documents=1, documents=(_document(1),)))
        use_case, editor = self._use_case(options_skill(("document-create",), (), "create"), reader)

        result = use_case.execute(self._request(folder_id="folder-9"))

        self.assertEqual(result.action, "markdown_create")
        self.assertEqual(reader.calls, [])
        self.assertEqual(editor.create_requests[0].reference_context, {})

    def test_skill_without_source_on_document_does_not_use_document_as_source(self) -> None:
        skill = self._source_skill(requires_source=False)
        use_case, editor = self._use_case(skill, RecordingFolderSourceReader(FolderSourceListing(0, ())))

        result = use_case.execute(self._request(document=True, folder_id="folder-parent"))

        self.assertEqual(result.action, "markdown_create")
        create_request = editor.create_requests[0]
        self.assertNotIn("source_document", create_request.reference_context or {})
        # 원문을 쓰지 않으므로 `{문서}`에도 열린 문서 본문을 넣지 않는다.
        self.assertNotIn("첫 문단", create_request.skill_instructions or "")
        self.assertIn("(현재 문서 없음)", create_request.skill_instructions or "")

    def test_inaccessible_folder_asks_for_another_target(self) -> None:
        reader = RecordingFolderSourceReader(FolderSourceUnavailableError(FOLDER_NOT_ACCESSIBLE_MESSAGE))
        use_case, editor = self._use_case(self._source_skill(), reader)

        result = use_case.execute(self._request(folder_id="folder-9"))

        self.assertEqual(result.action, "clarify")
        self.assertEqual(result.message, FOLDER_NOT_ACCESSIBLE_MESSAGE)
        self.assertEqual(editor.create_requests, [])

    def test_folder_without_readable_documents_asks_for_another_target(self) -> None:
        use_case, editor = self._use_case(
            self._source_skill(),
            RecordingFolderSourceReader(FolderSourceListing(total_documents=0, documents=())),
        )

        result = use_case.execute(self._request(folder_id="folder-9"))

        self.assertEqual(result.action, "clarify")
        self.assertIn("읽을 수 있는 Markdown 문서가 없어요", result.message or "")
        self.assertEqual(editor.create_requests, [])


class BackendFolderSourceReaderTest(unittest.TestCase):
    def test_calls_document_service_folder_source_endpoint(self) -> None:
        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "total_documents": 3,
                "items": [
                    {"document_id": "doc-1", "display_name": "회의록", "updated_at": "2026-10-01T00:00:00Z",
                     "markdown": "# 회의록"},
                    {"document_id": "doc-2", "display_name": "원본.pdf", "markdown": None},
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        response.__enter__.return_value = response

        with patch.dict(
            "os.environ",
            {"DOCUMENT_INTERNAL_BASE_URL": "http://document-svc:8080", "AGENT_INTERNAL_TOKEN": "service-token"},
            clear=True,
        ), patch(
            "app.modules.agent.infrastructure.backend_folder_source_reader.urlopen",
            return_value=response,
        ) as urlopen_mock:
            listing = build_folder_source_reader().read(
                workspace_id="workspace-1",
                user_id="user-1",
                folder_id="folder-9",
                max_documents=20,
            )

        request = urlopen_mock.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://document-svc:8080/internal/agent/skill-execution/folder-sources/read",
        )
        self.assertEqual(request.get_header("X-agent-service-token"), "service-token")
        self.assertEqual(
            json.loads(request.data),
            {"workspace_id": "workspace-1", "user_id": "user-1", "folder_id": "folder-9", "max_documents": 20},
        )
        self.assertEqual(listing.total_documents, 3)
        # Markdown 본문이 없는 원본 문서는 원문으로 쓰지 않는다.
        self.assertEqual(listing.documents, (FolderSourceDocument("doc-1", "회의록", "# 회의록", "2026-10-01T00:00:00Z"),))

    def test_permission_errors_become_user_facing_unavailable_error(self) -> None:
        reader = BackendFolderSourceReader("http://document-svc:8080", "service-token")
        for status in (403, 404):
            with self.subTest(status=status), patch(
                "app.modules.agent.infrastructure.backend_folder_source_reader.urlopen",
                side_effect=HTTPError("url", status, "denied", {}, None),  # type: ignore[arg-type]
            ), self.assertRaises(FolderSourceUnavailableError) as raised:
                reader.read(workspace_id="workspace-1", user_id="user-1", folder_id="folder-9", max_documents=20)
            self.assertEqual(str(raised.exception), FOLDER_NOT_ACCESSIBLE_MESSAGE)


if __name__ == "__main__":
    unittest.main()

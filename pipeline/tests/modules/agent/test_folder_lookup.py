import unittest

from app.modules.agent.application.handle_agent_turn import HandleAgentTurnUseCase
from app.modules.agent.domain.entities import AgentTurnRequest, AgentTurnRoute
from app.modules.agent.domain.folder_lookup import (
    FOLDER_LOOKUP_READ_FAILED_MESSAGE,
    FOLDER_NOT_FOUND_MESSAGE,
    MAX_LISTED_ITEMS,
    FolderLookupUnavailableError,
    find_folders,
    folder_entries,
    folder_lookup_message,
)
from app.modules.agent.infrastructure.chat_completions_turn_router import _normalize_route, _route_failures
from app.modules.agent.infrastructure.jev_turn_router import route_options


def folder(folder_id: str, name: str, *children: dict) -> dict:
    return {"type": "folder", "id": folder_id, "name": name, "children": list(children)}


def document(name: str) -> dict:
    return {"type": "document", "id": f"doc_{name}", "name": f"{name}.md", "document": {"display_name": name}}


TREE = [
    folder("f-minutes", "회의록", folder("f-2026", "2026 회의록", document("10월 회의")), document("킥오프")),
    folder("f-plan", "기획", folder("f-plan-minutes", "회의록")),
    folder("f-empty", "보관"),
]


class FolderLookupDomainTest(unittest.TestCase):
    def test_flattens_tree_with_paths_and_direct_children(self) -> None:
        entries = {entry.id: entry for entry in folder_entries(TREE)}

        self.assertEqual(entries["f-2026"].path, ("회의록", "2026 회의록"))
        self.assertEqual(entries["f-minutes"].child_folders, ("2026 회의록",))
        # 문서는 파일명이 아니라 사람이 붙인 이름으로 보여 준다.
        self.assertEqual(entries["f-minutes"].child_documents, ("킥오프",))

    def test_longer_name_wins_over_contained_shorter_name(self) -> None:
        matches = find_folders(folder_entries(TREE), "2026 회의록 폴더에 뭐 있어?", None)

        self.assertEqual([entry.id for entry in matches], ["f-2026"])

    def test_same_name_in_two_places_asks_for_path(self) -> None:
        matches = find_folders(folder_entries(TREE), "회의록 폴더에 뭐가 있어?", None)

        self.assertEqual({entry.id for entry in matches}, {"f-minutes", "f-plan-minutes"})
        message = folder_lookup_message(matches)
        self.assertIn("여러 개", message)
        self.assertIn("기획 / 회의록", message)

    def test_uses_selected_folder_when_message_names_none(self) -> None:
        matches = find_folders(folder_entries(TREE), "이 폴더에 뭐 있어?", "f-2026")

        self.assertEqual(folder_lookup_message(matches).splitlines()[0],
                         "'2026 회의록' 폴더(회의록 / 2026 회의록)에 하위 폴더 0개, 문서 1개가 있어요.")

    def test_not_found_and_empty_folder_messages(self) -> None:
        entries = folder_entries(TREE)

        self.assertEqual(folder_lookup_message(find_folders(entries, "없는 폴더에 뭐 있어?", None)), FOLDER_NOT_FOUND_MESSAGE)
        self.assertEqual(folder_lookup_message(find_folders(entries, "보관 폴더 보여줘", None)), "'보관' 폴더는 비어 있어요.")

    def test_caps_listed_items(self) -> None:
        big = folder("f-big", "자료", *(document(f"문서{index}") for index in range(MAX_LISTED_ITEMS + 3)))

        message = folder_lookup_message(find_folders(folder_entries([big]), "자료 폴더", None))

        self.assertIn(f"- 문서{MAX_LISTED_ITEMS - 1}", message)
        self.assertNotIn(f"- 문서{MAX_LISTED_ITEMS}\n", message)
        self.assertIn("외 3개", message)


LOOKUP_ROUTE = AgentTurnRoute(action="folder_lookup", confidence=0.9, reason="folder contents")


class FixedRouter:
    def route(self, request: AgentTurnRequest) -> AgentTurnRoute:
        return LOOKUP_ROUTE


class FakeTreeReader:
    def __init__(self, result: list[dict] | Exception) -> None:
        self.result = result

    def read(self, *, workspace_id: str, user_id: str):
        if isinstance(self.result, Exception):
            raise self.result
        return folder_entries(self.result)


class FolderLookupTurnTest(unittest.TestCase):
    def _execute(self, reader: FakeTreeReader):
        use_case = HandleAgentTurnUseCase(
            router=FixedRouter(),
            query_use_case=None,  # type: ignore[arg-type]
            markdown_edit_use_case=None,  # type: ignore[arg-type]
            markdown_create_use_case=None,  # type: ignore[arg-type]
            folder_tree_reader=reader,
        )
        return use_case.execute(AgentTurnRequest(
            message="2026 회의록 폴더에 뭐가 있어?", workspace_id="ws-1", user_id="user-1", mode="task",
        ))

    def test_lists_named_folder_contents(self) -> None:
        result = self._execute(FakeTreeReader(TREE))

        self.assertEqual(result.action, "folder_lookup")
        self.assertIn("- 10월 회의", result.message or "")

    def test_tree_read_failure_returns_message(self) -> None:
        result = self._execute(FakeTreeReader(FolderLookupUnavailableError(FOLDER_LOOKUP_READ_FAILED_MESSAGE)))

        self.assertEqual(result.action, "folder_lookup")
        self.assertEqual(result.message, FOLDER_LOOKUP_READ_FAILED_MESSAGE)


class FolderLookupRouteContractTest(unittest.TestCase):
    def test_route_contract_accepts_folder_lookup_and_jev_offers_it_in_task_mode(self) -> None:
        request = AgentTurnRequest(message="회의록 폴더에 뭐가 있어?", mode="task")
        value = {
            "action": "folder_lookup",
            "retrieval_source": "none",
            "document_operation": "none",
            "persist": False,
            "required_capabilities": [],
            "edit_goal": None,
            "edit_operation": None,
            "edit_destination": None,
            "confidence": 1.0,
            "reason": "folder contents",
        }

        route, failures = _normalize_route(value)

        self.assertEqual(failures + _route_failures(route, request), [])
        self.assertIn("folder_lookup", {option["action"] for option in route_options(request)})

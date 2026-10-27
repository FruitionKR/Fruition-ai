import unittest

from app.modules.agent.application.handle_agent_turn import HandleAgentTurnUseCase
from app.modules.agent.domain.entities import AgentTurnRequest, AgentTurnRoute
from app.modules.agent.domain.folder_lookup import (
    FOLDER_LOOKUP_READ_FAILED_MESSAGE,
    FOLDER_NOT_FOUND_MESSAGE,
    MAX_LISTED_ITEMS,
    FOLDER_REFERENCE_FAILED_MESSAGE,
    MAX_FOLDER_CANDIDATES,
    FolderLookupUnavailableError,
    folder_entries,
    folder_lookup_message,
    pick_folders,
)
from app.modules.agent.infrastructure.chat_completions_folder_resolver import ChatCompletionsFolderResolver
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

    def test_referenced_folder_wins_over_selected_folder(self) -> None:
        entries = folder_entries(TREE)

        self.assertEqual([e.id for e in pick_folders(entries, ["f-empty"], "f-2026")], ["f-empty"])

    def test_selected_folder_is_used_only_without_a_reference(self) -> None:
        entries = folder_entries(TREE)

        self.assertEqual([e.id for e in pick_folders(entries, [], "f-2026")], ["f-2026"])
        self.assertEqual(pick_folders(entries, [], None), [])

    def test_two_referenced_folders_ask_with_paths(self) -> None:
        message = folder_lookup_message(pick_folders(folder_entries(TREE), ["f-minutes", "f-plan-minutes"], None))

        self.assertIn("여러 개", message)
        self.assertIn("기획 / 회의록", message)

    def test_unknown_referenced_id_is_ignored(self) -> None:
        self.assertEqual(pick_folders(folder_entries(TREE), ["missing"], None), [])

    def test_not_found_and_empty_folder_messages(self) -> None:
        entries = folder_entries(TREE)

        self.assertEqual(folder_lookup_message(pick_folders(entries, [], None)), FOLDER_NOT_FOUND_MESSAGE)
        self.assertEqual(folder_lookup_message(pick_folders(entries, ["f-empty"], None)), "'보관' 폴더는 비어 있어요.")

    def test_caps_listed_items(self) -> None:
        big = folder("f-big", "자료", *(document(f"문서{index}") for index in range(MAX_LISTED_ITEMS + 3)))

        message = folder_lookup_message(pick_folders(folder_entries([big]), ["f-big"], None))

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


class FakeResolver:
    def __init__(self, ids: list[str] | Exception) -> None:
        self.ids = ids
        self.calls: list[tuple[str, int]] = []

    def resolve(self, request: AgentTurnRequest, folders):
        self.calls.append((request.message, len(folders)))
        if isinstance(self.ids, Exception):
            raise self.ids
        return self.ids


class FolderLookupTurnTest(unittest.TestCase):
    def _execute(self, reader: FakeTreeReader, resolver: FakeResolver | None = None, folder_id: str | None = None):
        use_case = HandleAgentTurnUseCase(
            router=FixedRouter(),
            query_use_case=None,  # type: ignore[arg-type]
            markdown_edit_use_case=None,  # type: ignore[arg-type]
            markdown_create_use_case=None,  # type: ignore[arg-type]
            folder_tree_reader=reader,
            folder_reference_resolver=resolver or FakeResolver(["f-2026"]),
        )
        return use_case.execute(AgentTurnRequest(
            message="2026 회의록 폴더에 뭐가 있어?", workspace_id="ws-1", user_id="user-1", mode="task", folder_id=folder_id,
        ))

    def test_resolver_failure_returns_message(self) -> None:
        result = self._execute(FakeTreeReader(TREE), FakeResolver(FolderLookupUnavailableError(FOLDER_REFERENCE_FAILED_MESSAGE)))

        self.assertEqual(result.message, FOLDER_REFERENCE_FAILED_MESSAGE)

    def test_falls_back_to_selected_folder_when_nothing_is_referenced(self) -> None:
        result = self._execute(FakeTreeReader(TREE), FakeResolver([]), folder_id="f-empty")

        self.assertEqual(result.message, "'보관' 폴더는 비어 있어요.")

    def test_lists_named_folder_contents(self) -> None:
        result = self._execute(FakeTreeReader(TREE))

        self.assertEqual(result.action, "folder_lookup")
        self.assertIn("- 10월 회의", result.message or "")

    def test_tree_read_failure_returns_message(self) -> None:
        result = self._execute(FakeTreeReader(FolderLookupUnavailableError(FOLDER_LOOKUP_READ_FAILED_MESSAGE)))

        self.assertEqual(result.action, "folder_lookup")
        self.assertEqual(result.message, FOLDER_LOOKUP_READ_FAILED_MESSAGE)


class StubJsonClient:
    def __init__(self, response: object) -> None:
        self.response = response
        self.payloads: list[dict] = []

    def complete_json(self, system_prompt: str, user_prompt: str):
        import json

        self.payloads.append(json.loads(user_prompt))
        return self.response


class ChatCompletionsFolderResolverTest(unittest.TestCase):
    def test_maps_short_keys_back_to_folder_ids_and_drops_unknown_keys(self) -> None:
        client = StubJsonClient({"folders": ["f2", "f99", 3]})
        resolver = ChatCompletionsFolderResolver(client, "system")  # type: ignore[arg-type]
        entries = folder_entries(TREE)

        ids = resolver.resolve(AgentTurnRequest(message="2026 회의록 폴더에 뭐 있어?"), entries)

        self.assertEqual(ids, [entries[1].id])
        sent = client.payloads[0]["folders"]
        self.assertEqual(sent[1], {"key": "f2", "name": entries[1].name, "path": " / ".join(entries[1].path)})
        # 실제 폴더 id는 모델에 보내지 않는다.
        self.assertNotIn(entries[1].id, json_text(client.payloads[0]))

    def test_caps_candidates_and_rejects_malformed_output(self) -> None:
        many = folder_entries([folder(f"id-{i}", f"폴더{i}") for i in range(MAX_FOLDER_CANDIDATES + 5)])
        client = StubJsonClient({"folders": "f1"})
        resolver = ChatCompletionsFolderResolver(client, "system")  # type: ignore[arg-type]

        with self.assertRaises(FolderLookupUnavailableError):
            resolver.resolve(AgentTurnRequest(message="폴더1에 뭐 있어?"), many)
        self.assertEqual(len(client.payloads[0]["folders"]), MAX_FOLDER_CANDIDATES)


def json_text(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


class FolderTreeReaderTest(unittest.TestCase):
    def test_permission_errors_and_dropped_connections_become_messages(self) -> None:
        from http.client import RemoteDisconnected
        from unittest.mock import patch
        from urllib.error import HTTPError

        from app.modules.agent.domain.folder_lookup import FOLDER_LOOKUP_FORBIDDEN_MESSAGE
        from app.modules.agent.infrastructure import backend_folder_tree_reader as module

        reader = module.BackendFolderTreeReader("http://document-svc", "token")
        cases = [
            (HTTPError("http://document-svc", 404, "not found", {}, None), FOLDER_LOOKUP_FORBIDDEN_MESSAGE),
            (RemoteDisconnected("closed"), FOLDER_LOOKUP_READ_FAILED_MESSAGE),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__), patch.object(module, "urlopen", side_effect=error):
                with self.assertRaises(FolderLookupUnavailableError) as raised:
                    reader.read(workspace_id="ws-1", user_id="user-1")
                self.assertEqual(str(raised.exception), expected)


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

        # 채팅 탭 등 작업 탭이 아닌 턴에서는 쓰지 않는다.
        chat_request = AgentTurnRequest(message=request.message)
        self.assertIn("folder_lookup is available only in task mode", _route_failures(route, chat_request))
        self.assertNotIn("folder_lookup", {option["action"] for option in route_options(chat_request)})


class FakeJevClient:
    def __init__(self, pick) -> None:
        self.pick = pick
        self.questions: list[dict] = []

    def choose(self, state: str, questions: dict) -> dict:
        from app.core.jev_client import JevUnavailable

        self.questions.append(questions)
        if isinstance(self.pick, Exception):
            raise self.pick
        criteria = questions["folder"]["criteria"]
        key = next(k for k, text in criteria.items() if self.pick(k, text))
        return {"folder": {"choice": key, "confidence": 0.9}}


class JevFolderResolverTest(unittest.TestCase):
    def _resolver(self, client: FakeJevClient, fallback_ids: list[str] | None = None):
        from app.modules.agent.infrastructure.jev_folder_resolver import JevFolderResolver

        fallback = FakeResolver(fallback_ids or ["fallback"])
        return JevFolderResolver(client, fallback), fallback  # type: ignore[arg-type]

    def test_offers_each_folder_a_same_name_group_and_no_reference(self) -> None:
        from app.modules.agent.infrastructure.jev_folder_resolver import folder_choices

        choices = folder_choices(folder_entries(TREE))

        self.assertEqual(choices["none"][1], ())
        groups = [ids for text, ids in choices.values() if "2곳 전부" in text]
        self.assertEqual(groups, [("f-minutes", "f-plan-minutes")])
        self.assertEqual(len(choices), 1 + len(folder_entries(TREE)) + 1)

    def test_group_choice_returns_every_same_name_folder_and_none_returns_empty(self) -> None:
        group, _ = self._resolver(FakeJevClient(lambda key, text: "2곳 전부" in text))
        nothing, _ = self._resolver(FakeJevClient(lambda key, text: key == "none"))
        request = AgentTurnRequest(message="회의록 폴더에 뭐 있어?")

        self.assertEqual(group.resolve(request, folder_entries(TREE)), ["f-minutes", "f-plan-minutes"])
        self.assertEqual(nothing.resolve(request, folder_entries(TREE)), [])

    def test_falls_back_to_llm_when_jev_fails_or_choices_exceed_limit(self) -> None:
        from app.core.jev_client import JevUnavailable

        failing, fallback = self._resolver(FakeJevClient(JevUnavailable("down")))
        self.assertEqual(failing.resolve(AgentTurnRequest(message="회의록"), folder_entries(TREE)), ["fallback"])

        client = FakeJevClient(lambda key, text: True)
        many, fallback = self._resolver(client)
        entries = folder_entries([folder(f"id-{i}", f"폴더{i}") for i in range(300)])
        self.assertEqual(many.resolve(AgentTurnRequest(message="폴더1"), entries), ["fallback"])
        self.assertEqual(client.questions, [])

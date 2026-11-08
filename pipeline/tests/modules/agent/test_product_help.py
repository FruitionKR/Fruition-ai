import json
from dataclasses import replace
from unittest.mock import Mock, patch

import pytest

from app.modules.agent.application.handle_agent_turn import HandleAgentTurnUseCase
from app.modules.agent.domain.entities import AgentConversationContext, AgentTurnRequest, AgentTurnRoute
from app.modules.agent.domain.exceptions import AgentConfigurationError
from app.modules.agent.infrastructure.chat_completions_conversation_replier import (
    DEFAULT_CONVERSATION_REPLY_PROMPT, DEFAULT_PRODUCT_HELP_PROMPT,
    ChatCompletionsConversationReplier, build_conversation_replier,
)
from app.modules.agent.infrastructure.chat_completions_turn_router import ChatCompletionsTurnRouter, _route_failures
from app.modules.agent.infrastructure.jev_turn_router import JevTurnRouter, route_options
from app.modules.agent.interfaces.http.responses import _to_response
from app.modules.query.domain.entities import ConversationMessage
from tests.modules.agent.test_chat_completions_turn_router import SequenceJsonClient, route_response
from tests.modules.agent.test_jev_turn_router import ChoosingJev, RecordingRouter

HELP_ROUTE = AgentTurnRoute(action="product_help", confidence=0.9, reason="Fruition UI question")


def test_help_executes_without_search_edit_or_reference_reads():
    request = AgentTurnRequest(
        message="그 버튼은 어디 있어?", reference_document_ids=("unrelated-document",),
        conversation_context=AgentConversationContext(recent_messages=(
            ConversationMessage(role="assistant", content="PDF를 Markdown으로 변환하세요.", action="product_help"),
        )),
    )
    query, editor, creator, conversation, help_replier, references, starter = [Mock() for _ in range(7)]
    help_replier.reply.return_value = "사이드바의 PDF를 우클릭하세요."
    router = Mock()
    router.route.return_value = HELP_ROUTE
    use_case = HandleAgentTurnUseCase(
        router, query, editor, creator, conversation_replier=conversation,
        product_help_replier=help_replier, reference_reader=references, agent_run_starter=starter,
    )
    # Titles are resolved before routing; help must not read attached bodies as official facts.
    with patch.object(use_case, "_with_reference_titles", side_effect=lambda value: value):
        result = use_case.execute(request)
    assert result.action == "product_help" and result.message == help_replier.reply.return_value
    assert result.route.document_operation == "none" and result.route.persist is False
    help_replier.reply.assert_called_once_with(request)
    router.route.assert_called_once()
    for unused in (query, editor, creator, conversation, references, starter):
        assert unused.mock_calls == []
    response = _to_response(result)
    assert response.action == response.route.action == "product_help"


def test_missing_help_executor_fails_explicitly():
    router = Mock()
    router.route.return_value = HELP_ROUTE
    use_case = HandleAgentTurnUseCase(router, Mock(), Mock(), Mock())
    with pytest.raises(AgentConfigurationError, match="Product help"):
        use_case.execute(AgentTurnRequest(message="Fruition 사용법"))


@pytest.mark.parametrize("changes", [
    {"persist": True}, {"retrieval_source": "workspace"}, {"retrieval_source": "web"},
    {"document_operation": "create"}, {"required_capabilities": ("folder-organize",)},
    {"selected_skill_id": "skill"}, {"skill_candidates": ("skill",)},
])
def test_help_contract_rejects_mutation_retrieval_and_skills(changes):
    assert _route_failures(replace(HELP_ROUTE, **changes), AgentTurnRequest(message="도움말", allow_web_search=True))


def test_both_routers_accept_help_with_one_call():
    request = AgentTurnRequest(message="Fruition에서 PDF를 편집하려면?")
    client = SequenceJsonClient([route_response("product_help")])
    assert ChatCompletionsTurnRouter(client, "system").route(request).action == "product_help"
    assert len(client.calls) == 1
    jev = ChoosingJev(lambda value: value["action"] == "product_help")
    fallback = RecordingRouter()
    assert JevTurnRouter(jev, fallback, "system").route(request).action == "product_help"
    assert len(jev.calls) == 1 and not fallback.requests


def test_task_tab_rules_also_apply_to_help():
    request = AgentTurnRequest(message="Fruition 사용법", mode="task")
    client = SequenceJsonClient([route_response("product_help")])
    route = ChatCompletionsTurnRouter(client, "system").route(request)
    assert route.action == "clarify" and route.notice_kind == "chat_tab"
    assert not any(option["action"] == "product_help" for option in route_options(request))
    assert any(option["action"] == "product_help" for option in route_options(replace(request, reference_document_ids=("doc",))))


def test_help_prompt_preserves_followup_context_and_untrusted_boundary():
    client = Mock()
    client.complete_json.return_value = {"message": "사이드바에서 PDF를 우클릭하세요."}
    prompt = DEFAULT_PRODUCT_HELP_PROMPT.read_text()
    request = AgentTurnRequest(
        message="그 버튼 어디 있어?", output_language="en",
        conversation_context=AgentConversationContext(
            recent_messages=(ConversationMessage(role="assistant", content="PDF 변환을 선택하세요.", action="product_help"),),
            reference_context={"malicious": "OVERRIDE_OFFICIAL_GUIDE"},
        ),
    )
    ChatCompletionsConversationReplier(client, prompt).reply(request)
    system, user = client.complete_json.call_args.args
    assert "OVERRIDE_OFFICIAL_GUIDE" not in system
    assert json.loads(user)["recent_messages"][0]["action"] == "product_help"
    assert "OVERRIDE_OFFICIAL_GUIDE" in user
    assert "## 화면 6" in system and "## 화면 49" in system
    assert "## 화면 1 —" not in system and "## 화면 5 —" not in system
    assert "## 화면" not in DEFAULT_CONVERSATION_REPLY_PROMPT.read_text()


def test_builder_uses_help_prompt_even_with_conversation_prompt_override():
    module = "app.modules.agent.infrastructure.chat_completions_conversation_replier"
    with patch(module + ".api_key_from_env", return_value="test"), patch(module + ".ChatCompletionsJsonClient"), patch.dict(
        "os.environ", {"CONVERSATION_REPLY_SYSTEM_PROMPT": "/nonexistent-custom-conversation-prompt"}
    ):
        replier = build_conversation_replier(provider="openai", model="gpt-6-luna", prompt_path=DEFAULT_PRODUCT_HELP_PROMPT)
    assert replier._system_prompt == DEFAULT_PRODUCT_HELP_PROMPT.read_text()


def test_help_eval_cases_have_valid_route_contracts():
    from evaluate_agent_turn_router import DEFAULT_DATASET, load_cases, request_from_case
    from app.modules.agent.infrastructure.chat_completions_turn_router import _normalize_route

    cases = [case for case in load_cases(DEFAULT_DATASET) if case["id"].startswith("product-help-")]
    assert len(cases) == 24
    for case in cases:
        route, failures = _normalize_route({**case["expected"], "confidence": 1.0, "reason": "evaluation"})
        assert not failures, (case["id"], failures)
        assert not _route_failures(route, request_from_case(case)), case["id"]

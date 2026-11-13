import json
from dataclasses import replace
from unittest.mock import Mock, patch

import pytest

from app.modules.agent.application.handle_agent_turn import HandleAgentTurnUseCase
from app.modules.agent.domain.entities import AgentConversationContext, AgentTurnRequest, AgentTurnRoute
from app.modules.agent.domain.exceptions import AgentConfigurationError
from app.modules.agent.infrastructure.chat_completions_conversation_replier import (
    DEFAULT_CONVERSATION_REPLY_PROMPT, DEFAULT_PRODUCT_HELP_PROMPT, load_product_help_prompt,
    ChatCompletionsConversationReplier, build_conversation_replier,
)
from app.modules.agent.infrastructure.chat_completions_turn_router import ChatCompletionsTurnRouter, _route_failures
from app.modules.agent.infrastructure.jev_turn_router import JevTurnRouter, route_options
from app.modules.agent.interfaces.http.responses import _to_response
from app.modules.query.domain.entities import ConversationMessage
from tests.modules.agent.test_chat_completions_turn_router import SequenceJsonClient, route_response
from tests.modules.agent.test_jev_turn_router import ChoosingJev, RecordingRouter

HELP_ROUTE = AgentTurnRoute(action="product_help", confidence=0.9, reason="Fruition UI question")


@pytest.mark.parametrize("mode,document_ids", [(None, ("unrelated-document",)), ("task", ()), ("task", ("unrelated-document",))])
def test_help_executes_without_search_edit_or_reference_reads(mode, document_ids):
    request = AgentTurnRequest(
        message="그 버튼은 어디 있어?", mode=mode, reference_document_ids=document_ids,
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
    assert route.action == "product_help" and route.notice_kind is None
    assert any(option["action"] == "product_help" for option in route_options(request))
    assert any(option["action"] == "product_help" for option in route_options(replace(request, reference_document_ids=("doc",))))


def test_help_prompt_preserves_followup_context_and_untrusted_boundary():
    client = Mock()
    client.complete_json.return_value = {"message": "사이드바에서 PDF를 우클릭하세요."}
    prompt = load_product_help_prompt()
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
    assert replier._system_prompt == load_product_help_prompt()


def test_help_eval_cases_have_valid_route_contracts():
    from evaluate_agent_turn_router import DEFAULT_DATASET, load_cases, request_from_case
    from app.modules.agent.infrastructure.chat_completions_turn_router import _normalize_route

    cases = [case for case in load_cases(DEFAULT_DATASET) if case["id"].startswith("product-help-")]
    assert len(cases) == 24
    for case in cases:
        route, failures = _normalize_route({**case["expected"], "confidence": 1.0, "reason": "evaluation"})
        assert not failures, (case["id"], failures)
        assert not _route_failures(route, request_from_case(case)), case["id"]


@pytest.mark.parametrize(('screen', 'output', 'expected'), [
    ('en', None, 'English'), ('ko', None, 'Korean'),
    ('en', 'ko', 'Korean'), ('ko', 'en', 'English'),
    ('en', 'document', 'English'), ('ko', 'document', 'Korean'),
    (None, None, None), (None, 'document', None),
])
def test_help_language_precedence_keeps_ui_language_separate(screen, output, expected):
    from app.core.response_language import response_language_scope

    client = Mock()
    client.complete_json.return_value = {'message': 'Answer'}
    replier = ChatCompletionsConversationReplier(client, load_product_help_prompt())
    router = Mock()
    router.route.return_value = HELP_ROUTE
    use_case = HandleAgentTurnUseCase(router, Mock(), Mock(), Mock(), product_help_replier=replier)
    with response_language_scope(screen):
        use_case.execute(AgentTurnRequest(message='Fruition PDF 편집 방법', output_language=output))
    system = client.complete_json.call_args.args[0]
    assert f'UI language: {screen or "unknown"}' in system
    assert 'Use the dominant language of the evidence document;' not in system
    if expected:
        assert f'Write the response in {expected}.' in system
    else:
        assert 'Write the response in English.' not in system
        assert 'Write the response in Korean.' not in system


def test_regular_conversation_keeps_document_language_preference():
    client = Mock()
    client.complete_json.return_value = {'message': 'Answer'}
    replier = ChatCompletionsConversationReplier(client, 'conversation prompt')
    replier.reply(AgentTurnRequest(message='Explain it', output_language='document'))
    assert 'Use the dominant language of the evidence document;' in client.complete_json.call_args.args[0]


def test_worker_passes_screen_language_to_help_and_does_not_leak_between_commands():
    from unittest.mock import MagicMock
    from app.workers import task_worker
    from app.core.response_language import current_response_language

    client = Mock()
    client.complete_json.return_value = {'message': 'Answer'}
    replier = ChatCompletionsConversationReplier(client, load_product_help_prompt())
    router = Mock()
    router.route.return_value = HELP_ROUTE
    publisher = Mock()
    use_case = HandleAgentTurnUseCase(
        router, Mock(), Mock(), Mock(), product_help_replier=replier, event_publisher=publisher,
    )
    base = dict(run_id='language-test', kind='agent', workspace_id='workspace', user_id='user',
                message='Fruition PDF 사용법', provider='openai', model='gpt-6-luna')
    with (
        patch.object(task_worker, '_register_agent_command', return_value=('execute', None)),
        patch.object(task_worker.database, 'connect_ai', return_value=MagicMock()),
        patch.object(task_worker, 'build_handle_agent_turn_use_case', return_value=use_case),
        patch.object(task_worker, '_execute_controlled', side_effect=task_worker._handle_agent),
    ):
        for screen in ['en', 'ko', None]:
            command = base | ({'response_language': screen} if screen else {})
            result = task_worker._handle_controlled(command, publisher)
            assert result['action'] == 'product_help' and result['message'] == 'Answer'
            assert f'UI language: {screen or "unknown"}' in client.complete_json.call_args.args[0]
            assert current_response_language() is None
    routed = [call.args[1] for call in publisher.publish.call_args_list if call.args[0] == 'agent.routed']
    assert routed == ['Explaining how to use Fruition.', 'Fruition 사용 방법을 안내하고 있어요.', 'Fruition 사용 방법을 안내하고 있어요.']

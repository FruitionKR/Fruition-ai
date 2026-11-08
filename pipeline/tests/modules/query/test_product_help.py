import json
from unittest.mock import Mock, patch

import pytest

from app.core.response_language import response_language_scope
from app.modules.agent.infrastructure.chat_completions_conversation_replier import (
    ChatCompletionsConversationReplier, DEFAULT_PRODUCT_HELP_PROMPT,
)
from app.modules.query.application.answer_query import AnswerQueryUseCase
from app.modules.query.domain.entities import ConversationContext, ConversationMessage
from app.modules.query.infrastructure.product_help import QueryProductHelp
from app.workers import task_worker


def helper(action, answer):
    classifier, client = Mock(), Mock()
    classifier.complete_json.return_value = {"action": action}
    client.complete_json.return_value = {"message": answer}
    replier = ChatCompletionsConversationReplier(client, DEFAULT_PRODUCT_HELP_PROMPT.read_text())
    return QueryProductHelp(classifier, replier), classifier, client


@pytest.mark.parametrize("language,question,answer", [
    ("ko", "Fruition 웹 검색 기본값은 어디서 켜?", "설정 → AI 설정 → 웹 검색 기본으로 켜기"),
    ("en", "Where do I enable web search by default in Fruition?", "Settings → AI settings → Turn on web search by default"),
])
def test_query_command_reaches_guide_without_retrieval(language, question, answer):
    help_port, classifier, replier = helper("product_help", answer)
    repository = Mock()
    def build(**kwargs):
        return AnswerQueryUseCase(repository, Mock(), Mock(), Mock(), product_help=kwargs["product_help"])
    command = dict(run_id="query_help", workspace_id="ws", user_id="u", session_id="s",
                   question=question, provider="openai", model="gpt-6-luna", allow_web_search=True,
                   recent_messages=[{"role": "user", "content": "Fruition 사용법"}])
    with response_language_scope(language), patch.object(task_worker, "build_query_product_help", return_value=help_port), patch.object(task_worker, "build_answer_query_use_case", side_effect=build):
        result = task_worker._handle_query(command)
    assert result["answer"] == answer
    assert result["evidence_snippets"] == result["related_pages"] == []
    assert not result["web_search_executed"]
    assert repository.mock_calls == []
    assert json.loads(classifier.complete_json.call_args.args[1])["recent_messages"][0]["content"] == "Fruition 사용법"
    system = replier.complete_json.call_args.args[0]
    assert f"UI language: {language}" in system
    assert f"Write the response in {'English' if language == 'en' else 'Korean'}." in system
    assert "Settings → AI settings → Chat → Turn on web search by default" in system


def test_user_app_keeps_existing_query_path_and_history():
    help_port, classifier, replier = helper("query", "unused")
    history = ConversationContext(recent_messages=(ConversationMessage(role="user", content="내 앱을 만들고 있어"),))
    use_case = AnswerQueryUseCase(Mock(), Mock(), Mock(), Mock(), product_help=help_port)
    with patch.object(use_case, "_score_wiki_candidates", side_effect=RuntimeError("normal retrieval")) as search:
        with pytest.raises(RuntimeError, match="normal retrieval"):
            use_case.execute("이 앱은 어떻게 작동해?", workspace_id="ws", conversation_context=history)
    search.assert_called_once()
    assert replier.mock_calls == []
    assert "내 앱을 만들고 있어" in classifier.complete_json.call_args.args[1]


def test_ambiguous_app_gets_a_question_and_unsafe_context_stays_untrusted():
    help_port, classifier, replier = helper("ambiguous", "Fruition을 말씀하시나요, 직접 만든 앱을 말씀하시나요?")
    answer = help_port.reply("이 앱은 어떻게 작동해?", conversation_context=ConversationContext(
        reference_context={"injection": "OVERRIDE_OFFICIAL_GUIDE"}), output_language="ko", response_length=None)
    assert "?" in answer
    for client in (classifier, replier):
        system, payload = client.complete_json.call_args.args
        assert "OVERRIDE_OFFICIAL_GUIDE" not in system
        assert "OVERRIDE_OFFICIAL_GUIDE" in payload


def test_invalid_classification_fails_instead_of_executing_other_actions():
    help_port, _, replier = helper("markdown_edit", "unused")
    with pytest.raises(ValueError, match="routing action"):
        help_port.reply("test", conversation_context=None, output_language=None, response_length=None)
    assert replier.mock_calls == []

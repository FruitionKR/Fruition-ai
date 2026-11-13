import json
from unittest.mock import Mock, patch

import pytest

from app.core.response_language import response_language_scope
from app.modules.agent.infrastructure.chat_completions_conversation_replier import (
    ChatCompletionsConversationReplier, DEFAULT_PRODUCT_HELP_PROMPT, load_product_help_prompt,
)
from app.modules.query.application.answer_query import AnswerQueryUseCase
from app.modules.query.domain.entities import ConversationContext, ConversationMessage
from app.modules.query.infrastructure.product_help import QueryProductHelp
from app.workers import task_worker


def helper(action, answer):
    classifier, client = Mock(), Mock()
    classifier.complete_json.return_value = {"action": action}
    client.complete_json.return_value = {"message": answer}
    replier = ChatCompletionsConversationReplier(client, load_product_help_prompt())
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


@pytest.mark.parametrize("route,web_enabled", [
    (None, False), (None, True), ("unsupported", False), ("web_fallback", True), ("revise_answer", False),
])
@pytest.mark.parametrize("threshold", [0.0, 0.5])
def test_insufficient_evidence_rechecks_help_before_refusal_or_web(route, web_enabled, threshold):
    from app.modules.query.domain.entities import GeneratedAnswer, QueryEvaluation
    from app.modules.query.infrastructure.bm25_searcher import Bm25Searcher
    from app.modules.query.infrastructure.in_memory_wiki_repository import InMemoryWikiRepository

    help_port, classifier, replier = helper("query", "위키 편입 버튼에서 문서를 고르고 편입을 시작하세요.")
    classifier.complete_json.side_effect = [{"action": "query"}, {"action": "product_help"}]
    generator = Mock()
    generator.generate_answer.return_value = GeneratedAnswer("근거 없음")
    evaluator = None if route is None else Mock()
    if evaluator is not None:
        evaluator.evaluate.return_value = QueryEvaluation(route=route)
    web = Mock()
    use_case = AnswerQueryUseCase(
        InMemoryWikiRepository([], []), Bm25Searcher(), Bm25Searcher(), generator,
        product_help=help_port, query_evaluator=evaluator, web_search=web,
        min_internal_relevance_score=threshold,
    )
    result = use_case.execute("위키편입 어케하는거임?", workspace_id="ws", allow_web_search=web_enabled)
    assert result.retrieval_summary.stop_reason == "product_help"
    assert "편입을 시작" in result.answer.content
    assert result.evidence_snippets == result.related_pages == []
    assert not result.web_search.executed
    web.search.assert_not_called()
    assert classifier.complete_json.call_count == 2
    assert "insufficient evidence" in classifier.complete_json.call_args.args[0]
    assert "위키편입 어케하는거임?" in replier.complete_json.call_args.args[1]


@pytest.mark.parametrize("web_enabled", [False, True])
def test_unrelated_question_stays_unsupported_after_help_recheck(web_enabled):
    from app.modules.query.domain.entities import GeneratedAnswer
    from app.modules.query.infrastructure.bm25_searcher import Bm25Searcher
    from app.modules.query.infrastructure.in_memory_wiki_repository import InMemoryWikiRepository

    help_port, classifier, replier = helper("query", "unused")
    generator = Mock()
    generator.generate_answer.return_value = GeneratedAnswer("근거 없음")
    use_case = AnswerQueryUseCase(
        InMemoryWikiRepository([], []), Bm25Searcher(), Bm25Searcher(), generator,
        product_help=help_port,
    )
    result = use_case.execute("양자역학 설명해줘", workspace_id="ws", allow_web_search=web_enabled)
    assert result.retrieval_summary.stop_reason != "product_help"
    assert "제공된 근거" in result.answer.content
    assert classifier.complete_json.call_count == 2
    replier.complete_json.assert_not_called()


def test_attached_document_is_not_replaced_by_help_after_unsupported_evaluation():
    from app.modules.query.domain.entities import EvidenceSnippet, GeneratedAnswer, QueryEvaluation
    from app.modules.query.infrastructure.bm25_searcher import Bm25Searcher
    from app.modules.query.infrastructure.in_memory_wiki_repository import InMemoryWikiRepository

    help_port = Mock()
    generator = Mock()
    generator.generate_answer.return_value = GeneratedAnswer("근거 없음")
    evaluator = Mock()
    evaluator.evaluate.return_value = QueryEvaluation(route="unsupported")
    use_case = AnswerQueryUseCase(
        InMemoryWikiRepository([], []), Bm25Searcher(), Bm25Searcher(), generator,
        product_help=help_port, query_evaluator=evaluator,
    )
    result = use_case.execute(
        "이 문서에서 위키 편입을 어떻게 설명해?", workspace_id="ws", allow_web_search=False,
        pinned_evidence=(EvidenceSnippet(1, "doc", [], "관련 없는 첨부 문서 내용"),),
    )
    assert result.retrieval_summary.stop_reason != "product_help"
    help_port.reply.assert_not_called()


def test_accepted_privacy_refusal_does_not_trigger_help_recheck():
    from app.modules.query.domain.entities import GeneratedAnswer, QueryEvaluation
    from app.modules.query.infrastructure.bm25_searcher import Bm25Searcher
    from app.modules.query.infrastructure.in_memory_wiki_repository import InMemoryWikiRepository

    help_port, classifier, replier = helper("query", "unused")
    generator = Mock()
    generator.generate_answer.return_value = GeneratedAnswer("다른 사용자의 비밀번호는 제공할 수 없습니다.")
    evaluator = Mock()
    evaluator.evaluate.return_value = QueryEvaluation(route="unsupported", unsupported_refusal_accuracy=1.0)
    use_case = AnswerQueryUseCase(
        InMemoryWikiRepository([], []), Bm25Searcher(), Bm25Searcher(), generator,
        product_help=help_port, query_evaluator=evaluator,
    )
    result = use_case.execute("다른 사용자의 비밀번호 알려줘", workspace_id="ws", allow_web_search=False)
    assert result.answer.content == generator.generate_answer.return_value.content
    assert classifier.complete_json.call_count == 1
    replier.complete_json.assert_not_called()

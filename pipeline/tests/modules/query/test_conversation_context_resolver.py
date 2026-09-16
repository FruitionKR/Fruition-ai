import unittest

from app.modules.query.application.conversation_context_resolver import (
    RECENT_MESSAGE_LIMIT,
    contextualize_question,
    evidence_question,
    update_conversation_summary,
)
from app.modules.query.domain.entities import ConversationContext, ConversationMessage


class FixedSummarizer:
    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.calls: list[tuple[str | None, tuple[ConversationMessage, ...]]] = []

    def summarize(
        self,
        previous_summary: str | None,
        messages: tuple[ConversationMessage, ...],
    ) -> str:
        self.calls.append((previous_summary, messages))
        return self.summary


class FailingSummarizer:
    def summarize(
        self,
        previous_summary: str | None,
        messages: tuple[ConversationMessage, ...],
    ) -> str:
        raise RuntimeError("summary provider unavailable")


class ConversationContextResolverTest(unittest.TestCase):
    def test_returns_original_question_without_conversation_context(self) -> None:
        question = "RAG가 뭐야?"

        self.assertEqual(contextualize_question(question, None), question)
        self.assertEqual(evidence_question(question, None, question), question)

    def test_contextualizes_follow_up_question_with_referents_and_reference_context(self) -> None:
        question = "그거랑 RAG 차이는?"
        context = ConversationContext(
            recent_conversation_summary="사용자는 Persistent Wiki와 RAG의 차이를 이어서 묻고 있다.",
            reference_context={
                "active_topic": {"canonical": "Persistent Wiki", "aliases": ["지속적 위키"]},
                "recent_concepts": ["Persistent Wiki", "RAG"],
                "referents": {"그거": {"canonical": "Persistent Wiki", "aliases": ["지속적 위키"]}},
            },
        )

        resolved = contextualize_question(question, context)

        self.assertEqual(
            resolved,
            (
                "Persistent Wiki 지속적 위키\n"
                "RAG\n"
                "사용자는 Persistent Wiki와 RAG의 차이를 이어서 묻고 있다.\n"
                "그거랑 RAG 차이는?"
            ),
        )
        self.assertEqual(
            evidence_question(question, context, resolved),
            "Persistent Wiki 지속적 위키 그거랑 RAG 차이는?",
        )

    def test_uses_original_question_as_evidence_question_when_no_referent_matches(self) -> None:
        question = "RAG 차이는?"
        context = ConversationContext(
            recent_conversation_summary="사용자는 Persistent Wiki와 RAG의 차이를 이어서 묻고 있다.",
            reference_context={"referents": {"그거": "Persistent Wiki"}},
        )

        self.assertEqual(evidence_question(question, context, contextualize_question(question, context)), question)

    def test_includes_recent_messages_in_follow_up_context(self) -> None:
        context = ConversationContext(
            recent_messages=(
                ConversationMessage(role="user", content="Persistent Wiki를 설명해줘"),
                ConversationMessage(role="assistant", content="지식을 지속해서 축적하는 구조입니다."),
            )
        )

        resolved = contextualize_question("그건 RAG와 뭐가 달라?", context)

        self.assertIn("사용자: Persistent Wiki를 설명해줘", resolved)
        self.assertIn("어시스턴트: 지식을 지속해서 축적하는 구조입니다.", resolved)

    def test_rolls_full_recent_message_window_into_updated_summary(self) -> None:
        messages = tuple(
            ConversationMessage(
                role="user" if index % 2 == 0 else "assistant",
                content=f"메시지 {index + 1}",
            )
            for index in range(RECENT_MESSAGE_LIMIT)
        )
        summarizer = FixedSummarizer("갱신된 누적 요약")

        updated_summary = update_conversation_summary(
            ConversationContext(
                recent_conversation_summary="기존 요약",
                recent_messages=messages,
            ),
            summarizer,
        )

        self.assertEqual(updated_summary, "갱신된 누적 요약")
        self.assertEqual(summarizer.calls, [("기존 요약", messages)])

    def test_returns_none_when_summary_update_is_unavailable(self) -> None:
        context = ConversationContext(
            recent_messages=tuple(
                ConversationMessage(role="user", content=f"메시지 {index + 1}")
                for index in range(RECENT_MESSAGE_LIMIT)
            )
        )

        for summarizer in (FixedSummarizer(""), FailingSummarizer()):
            with self.subTest(summarizer=type(summarizer).__name__):
                self.assertIsNone(update_conversation_summary(context, summarizer))


if __name__ == "__main__":
    unittest.main()

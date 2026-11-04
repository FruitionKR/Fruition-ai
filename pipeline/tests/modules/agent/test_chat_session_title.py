import unittest
from unittest.mock import patch

from app.modules.agent.infrastructure.chat_session_title import generate_chat_session_title, needs_chat_session_title


class ChatSessionTitleTest(unittest.TestCase):
    def test_title_uses_first_question_and_answer_only(self):
        command = {"kind": "agent", "session_id": "session", "provider": "openai", "model": "gpt-5-nano", "message": "위키는 무엇인가요?"}
        with patch("app.modules.agent.infrastructure.chat_session_title.ChatCompletionsJsonClient") as client:
            client.return_value.complete_json.return_value = {"title": "위키의 개념과 활용"}
            self.assertEqual(generate_chat_session_title(command, {"message": "위키는 지식을 모읍니다."}), "위키의 개념과 활용")
            self.assertIn("위키는 지식을 모읍니다.", client.return_value.complete_json.call_args.args[1])
            command["conversation_context"] = {"recent_messages": [{"role": "user", "content": "지난 질문"}]}
            self.assertIsNone(generate_chat_session_title(command, {"message": "다음 답변"}))
            client.assert_called_once()

    def test_query_title_and_invalid_model_output(self):
        command = {"kind": "query", "session_id": "session", "provider": "openai", "model": "gpt-5-nano", "question": "질문"}
        with patch("app.modules.agent.infrastructure.chat_session_title.ChatCompletionsJsonClient") as client:
            for invalid in (None, "", "새 채팅", "가" * 31):
                client.return_value.complete_json.return_value = {"title": invalid}
                self.assertIsNone(generate_chat_session_title(command, {"answer": "답변"}))

    def test_needs_title_matches_when_generation_calls_the_model(self):
        first = {"kind": "agent", "session_id": "session", "provider": "openai", "model": "m", "message": "질문"}
        self.assertTrue(needs_chat_session_title(first, {"message": "답변"}))
        self.assertTrue(needs_chat_session_title({**first, "kind": "query", "question": "질문", "message": None}, {"answer": "답변"}))
        cases = [
            ({**first, "kind": "ingest"}, {"message": "답변"}),
            ({**first, "session_id": None}, {"message": "답변"}),
            ({**first, "conversation_context": {"recent_messages": [{"role": "user", "content": "전"}]}}, {"message": "답변"}),
            ({**first, "conversation_context": {"recent_conversation_summary": "요약"}}, {"message": "답변"}),
            ({**first, "message": ""}, {"message": "답변"}),
            (first, {}),
        ]
        with patch("app.modules.agent.infrastructure.chat_session_title.ChatCompletionsJsonClient") as client:
            for command, result in cases:
                self.assertFalse(needs_chat_session_title(command, result))
                self.assertIsNone(generate_chat_session_title(command, result))
            client.assert_not_called()

    def test_session_title_needed_false_skips_both_kinds_without_model_call(self):
        turns = [
            ({"kind": "agent", "session_id": "session", "provider": "openai", "model": "m", "message": "질문"}, {"message": "답변"}),
            ({"kind": "query", "session_id": "session", "provider": "openai", "model": "m", "question": "질문"}, {"answer": "답변"}),
        ]
        with patch("app.modules.agent.infrastructure.chat_session_title.ChatCompletionsJsonClient") as client:
            for first, result in turns:
                command = {**first, "session_title_needed": False}
                self.assertFalse(needs_chat_session_title(command, result))
                self.assertIsNone(generate_chat_session_title(command, result))
            client.assert_not_called()

    def test_true_missing_and_invalid_session_title_needed_keep_old_behavior(self):
        # 구 document는 키를 싣지 않는다. 잘못된 타입도 없는 것으로 봐서 제목을 잃지 않는다.
        turns = [
            ({"kind": "agent", "session_id": "session", "provider": "openai", "model": "m", "message": "질문"}, {"message": "답변"}),
            ({"kind": "query", "session_id": "session", "provider": "openai", "model": "m", "question": "질문"}, {"answer": "답변"}),
        ]
        values = [{"session_title_needed": value} for value in (True, None, "false", "", 0, 1, [], {})] + [{}]
        for first, result in turns:
            for value in values:
                with self.subTest(kind=first["kind"], value=value):
                    self.assertTrue(needs_chat_session_title({**first, **value}, result))

import unittest
from unittest.mock import patch

from app.modules.agent.infrastructure.chat_session_title import generate_chat_session_title


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

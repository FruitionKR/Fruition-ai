from __future__ import annotations

import os
import unittest
from unittest import mock

from app.modules.document_evaluation.infrastructure.chat_completions_document_evaluator import (
    build_optional_document_evaluator,
)


ENV_NAMES = (
    "OPENAI_API_KEY",
)


class OptionalDocumentEvaluatorTest(unittest.TestCase):
    def test_returns_none_when_api_is_not_configured(self) -> None:
        clean_env = {name: "" for name in ENV_NAMES}

        with mock.patch.dict(os.environ, clean_env):
            evaluator = build_optional_document_evaluator()

        self.assertIsNone(evaluator)

    def test_uses_fixed_openai_configuration(self) -> None:
        clean_env = {name: "" for name in ENV_NAMES}
        clean_env["OPENAI_API_KEY"] = "openai-key"

        with mock.patch.dict(os.environ, clean_env):
            evaluator = build_optional_document_evaluator()

        assert evaluator is not None
        self.assertEqual(evaluator._client.provider, "openai")  # type: ignore[attr-defined]
        self.assertEqual(evaluator._client.config.model, "gpt-5-nano")  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import patch

from app.core.llm_env import (
    SUPPORTED_LLM_MODELS,
    api_key_from_env,
    float_env,
    inference_profile,
    int_env,
    optional_int_env,
    provider_api_key_env,
    resolve_llm_provider,
    resolve_llm_provider_defaults,
    resolve_llm_selection,
)


class LlmEnvTest(unittest.TestCase):
    def test_supported_selection_and_openai_default(self) -> None:
        self.assertEqual(resolve_llm_provider(), "openai")
        self.assertEqual(
            SUPPORTED_LLM_MODELS,
            {
                "openai": "gpt-5-nano",
                "gemini": "gemini-3.1-flash-lite",
                "claude": "claude-sonnet-5",
            },
        )
        for provider, model in SUPPORTED_LLM_MODELS.items():
            self.assertEqual(resolve_llm_selection(provider, model), (provider, model))

    def test_rejects_partial_or_unsupported_selection(self) -> None:
        for provider, model in ((None, "gpt-5-nano"), ("openai", None), (None, None)):
            with self.subTest(provider=provider, model=model):
                with self.assertRaises(ValueError):
                    resolve_llm_selection(provider, model)

        for provider, model in (("upstage", "solar-pro2"), ("openai", "other-model")):
            with self.subTest(provider=provider, model=model):
                with self.assertRaises(ValueError):
                    resolve_llm_selection(provider, model)

    def test_provider_api_key_envs_are_fixed(self) -> None:
        expected = {
            "openai": "OPENAI_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "claude": "ANTHROPIC_API_KEY",
        }
        for provider, key_env in expected.items():
            with self.subTest(provider=provider):
                self.assertEqual(provider_api_key_env(provider), key_env)

    def test_provider_defaults_reject_key_env_override(self) -> None:
        with self.assertRaisesRegex(ValueError, "fixed to OPENAI_API_KEY"):
            resolve_llm_provider_defaults(provider="openai", api_key_env="LLM_API_KEY")

    def test_api_key_is_read_only_from_selected_provider_env(self) -> None:
        with patch.dict(
            "os.environ",
            {"OPENAI_API_KEY": "openai-key", "LLM_API_KEY": "legacy-key"},
            clear=True,
        ):
            self.assertEqual(api_key_from_env(provider="openai"), "openai-key")
            self.assertIsNone(api_key_from_env(provider="gemini"))

    def test_reasoning_profile_is_provider_specific(self) -> None:
        self.assertEqual(inference_profile("openai", "gpt-5-nano"), {"reasoning_effort": "medium"})
        self.assertEqual(
            inference_profile("gemini", "gemini-3.1-flash-lite"),
            {"reasoning_effort": "low"},
        )
        self.assertEqual(inference_profile("claude", "claude-sonnet-5"), {})

    def test_resolves_numeric_env_values(self) -> None:
        with patch.dict(
            "os.environ",
            {"FLOAT_VALUE": "0.4", "INT_VALUE": "7", "OPTIONAL_INT": "9"},
            clear=True,
        ):
            self.assertEqual(float_env("FLOAT_VALUE", 0.0), 0.4)
            self.assertEqual(int_env("INT_VALUE", 0), 7)
            self.assertEqual(optional_int_env("OPTIONAL_INT"), 9)

        with patch.dict("os.environ", {"FLOAT_VALUE": "bad", "INT_VALUE": "bad", "OPTIONAL_INT": "bad"}, clear=True):
            self.assertEqual(float_env("FLOAT_VALUE", 0.2), 0.2)
            self.assertEqual(int_env("INT_VALUE", 3), 3)
            self.assertIsNone(optional_int_env("OPTIONAL_INT"))

    def test_rejects_unknown_provider(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported provider"):
            resolve_llm_provider("unknown")


if __name__ == "__main__":
    unittest.main()

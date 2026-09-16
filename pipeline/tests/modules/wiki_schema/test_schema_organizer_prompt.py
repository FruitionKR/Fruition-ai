from pathlib import Path
import unittest


PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "wiki_schema_organizer.system.md"


class SchemaOrganizerPromptTest(unittest.TestCase):
    def test_concept_preferences_are_evidence_based_candidate_hints(self) -> None:
        prompt = PROMPT_PATH.read_text(encoding="utf-8")

        self.assertIn("evidence-based candidate hints", prompt)
        self.assertIn("not mandatory outputs", prompt)
        self.assertIn("Do not invent concepts unsupported by source evidence", prompt)

    def test_prompt_prevents_concept_duplication_in_global(self) -> None:
        prompt = PROMPT_PATH.read_text(encoding="utf-8")

        self.assertIn("Do not put concept extraction preferences in `global_markdown`", prompt)
        self.assertIn("Do not duplicate concept preferences", prompt)
        self.assertIn("declarative configuration style", prompt)

    def test_prompt_requires_bullet_format_and_safe_preference_preservation(self) -> None:
        prompt = PROMPT_PATH.read_text(encoding="utf-8")

        self.assertIn('each start with "- "', prompt)
        self.assertIn("Preserve every safe preference", prompt)
        self.assertIn("must go to `global_markdown`", prompt)
        self.assertIn("must go to `query_markdown`", prompt)
        self.assertIn("must go to `edit_markdown`", prompt)


if __name__ == "__main__":
    unittest.main()

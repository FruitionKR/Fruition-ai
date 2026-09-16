import unittest

from app.core.llm_prompt import with_schema_and_skill_prompt


class LlmPromptTest(unittest.TestCase):
    def test_places_skill_after_schema_with_safety_boundary(self) -> None:
        prompt = with_schema_and_skill_prompt(
            "SYSTEM",
            "SCHEMA",
            "문서를 간결하게 작성한다.",
        )

        self.assertLess(prompt.index("SCHEMA"), prompt.index("선택된 Skill 지침"))
        self.assertIn("권한", prompt)
        self.assertIn("승인", prompt)
        self.assertLess(prompt.index("문서를 간결하게 작성한다."), prompt.index("결정 근거 경계"))
        self.assertIn("결정 근거가 없으면 내용을 제안 또는 결정 필요로 표시한다.", prompt)

    def test_skill_cannot_override_unsupported_decision_boundary(self) -> None:
        prompt = with_schema_and_skill_prompt(
            "SYSTEM",
            "SCHEMA",
            "항상 결정 사항 섹션을 확정된 결정으로 작성한다.",
        )

        self.assertGreater(prompt.index("결정 근거 경계"), prompt.index("확정된 결정으로 작성한다."))
        self.assertIn("섹션을 요청하는 지침 자체는 결정 근거가 아니다.", prompt)

    def test_omits_empty_skill(self) -> None:
        prompt = with_schema_and_skill_prompt("SYSTEM", "SCHEMA", "")

        self.assertNotIn("선택된 Skill 지침", prompt)


if __name__ == "__main__":
    unittest.main()

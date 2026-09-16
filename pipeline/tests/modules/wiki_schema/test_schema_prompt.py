import unittest

from app.modules.wiki_schema.application.build_workspace_schema_prompt import build_workspace_schema_prompt
from app.modules.wiki_schema.application.select_schema_fragments import select_schema_fragments
from app.modules.wiki_schema.domain.entities import SchemaFragments


class SchemaPromptTest(unittest.TestCase):
    def test_query_selects_only_global_and_query_fragments(self) -> None:
        fragments = SchemaFragments(
            global_markdown="## 공통\n- 한국어 기술 문서 문체를 따른다.",
            query_markdown="## 질문 답변\n- 결론을 먼저 제시한다.",
            edit_markdown="## 편집\n- 수식과 단위는 변경하지 않는다.",
        )

        selected = select_schema_fragments(fragments, "query")

        self.assertIn("## 공통", selected)
        self.assertIn("## 질문 답변", selected)
        self.assertNotIn("## 편집", selected)

    def test_edit_selects_only_global_and_edit_fragments(self) -> None:
        fragments = SchemaFragments(
            global_markdown="## 공통\n- 한국어 기술 문서 문체를 따른다.",
            query_markdown="## 질문 답변\n- 결론을 먼저 제시한다.",
            edit_markdown="## 편집\n- 수식과 단위는 변경하지 않는다.",
        )

        selected = select_schema_fragments(fragments, "edit")

        self.assertIn("## 공통", selected)
        self.assertIn("## 편집", selected)
        self.assertNotIn("## 질문 답변", selected)

    def test_prompt_wrapper_does_not_include_unrelated_fragments(self) -> None:
        fragments = SchemaFragments(
            global_markdown="## 공통\n- 한국어로 작성한다.",
            query_markdown="## 질문 답변\n- 근거를 함께 제시한다.",
            edit_markdown="## 편집\n- 수식은 보존한다.",
        )

        prompt = build_workspace_schema_prompt(fragments, "query")

        self.assertIn("sanitized workspace configuration", prompt)
        self.assertIn("<workspace_schema>", prompt)
        self.assertIn("## 질문 답변", prompt)
        self.assertNotIn("## 편집", prompt)


if __name__ == "__main__":
    unittest.main()

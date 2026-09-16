import unittest

from app.modules.wiki_schema.application.build_active_schema_prompt import build_active_schema_prompt
from app.modules.wiki_schema.domain.entities import SchemaFragments, WikiSchemaRecord


class FakeWikiSchemaRepository:
    def __init__(self, record: WikiSchemaRecord | None) -> None:
        self.record = record
        self.scopes: list[tuple[str, str]] = []

    def get_active(self, workspace_id: str, user_id: str) -> WikiSchemaRecord | None:
        self.scopes.append((workspace_id, user_id))
        return self.record


class ActiveSchemaPromptTest(unittest.TestCase):
    def test_builds_feature_scoped_schema_prompt(self) -> None:
        repository = FakeWikiSchemaRepository(
            WikiSchemaRecord(
                id="schema-1",
                workspace_id="ws-1",
                user_id="user-1",
                name="기본 schema",
                raw_markdown="raw",
                fragments=SchemaFragments(
                    global_markdown="- 한국어로 작성한다.",
                    query_markdown="- 근거를 함께 제시한다.",
                    edit_markdown="- 수식과 단위는 보존한다.",
                ),
                preview_markdown="preview",
                issues=[],
                status="active",
            )
        )

        prompt = build_active_schema_prompt(repository, "query", "ws-1", "user-1")  # type: ignore[arg-type]

        self.assertIn("<workspace_schema>", prompt)
        self.assertIn("한국어로 작성", prompt)
        self.assertIn("근거를 함께 제시", prompt)
        self.assertNotIn("수식과 단위", prompt)
        self.assertEqual(repository.scopes, [("ws-1", "user-1")])

    def test_returns_empty_prompt_without_active_schema(self) -> None:
        prompt = build_active_schema_prompt(FakeWikiSchemaRepository(None), "query", "ws-1", "user-1")  # type: ignore[arg-type]

        self.assertEqual(prompt, "")


if __name__ == "__main__":
    unittest.main()

import unittest

from app.modules.wiki_schema.domain.entities import SchemaFilterResult, SchemaFragments, SchemaIssue
from app.modules.wiki_schema.interfaces.http.routes import preview_wiki_schema
from app.modules.wiki_schema.interfaces.http.schemas import WikiSchemaPreviewRequest


class FakeOrganizeSchemaUseCase:
    def __init__(self, result: SchemaFilterResult) -> None:
        self.result = result
        self.requests: list[str] = []

    def execute(self, raw_markdown: str) -> SchemaFilterResult:
        self.requests.append(raw_markdown)
        return self.result


class WikiSchemaPreviewRoutesTest(unittest.TestCase):
    def test_returns_schema_preview_response(self) -> None:
        use_case = FakeOrganizeSchemaUseCase(
            SchemaFilterResult(
                fragments=SchemaFragments(
                    global_markdown="- 답변은 한국어 기술 문서 문체를 따른다.",
                    edit_markdown="- 수식과 단위는 변경하지 않는다.",
                ),
                issues=[
                    SchemaIssue(
                        severity="blocked",
                        category="policy_weakening",
                        text="출처 없이 단정",
                        reason="근거 또는 불확실성 정책을 약화하는 요청입니다.",
                    )
                ],
            )
        )

        response = preview_wiki_schema(
            payload=WikiSchemaPreviewRequest(raw_markdown="테스트 schema"),
            use_case=use_case,  # type: ignore[arg-type]
        )

        self.assertEqual(use_case.requests, ["테스트 schema"])
        self.assertTrue(response.has_blocked_issues)
        self.assertIn("적용될 Schema 설정", response.preview_markdown)
        self.assertIn("수식과 단위", response.fragments.edit_markdown)
        self.assertEqual(response.issues[0].category, "policy_weakening")


if __name__ == "__main__":
    unittest.main()

import unittest

from app.modules.wiki_schema.domain.entities import SchemaFragments, WikiSchemaRecord
from app.modules.wiki_schema.interfaces.http.routes import (
    activate_wiki_schema,
    create_wiki_schema_draft,
    get_active_wiki_schema,
)
from app.modules.wiki_schema.interfaces.http.schemas import CreateWikiSchemaDraftRequest


class FakeCreateSchemaDraftUseCase:
    def execute(self, raw_markdown: str, workspace_id: str, user_id: str, name: str = "default") -> WikiSchemaRecord:
        return WikiSchemaRecord(
            id="schema-1",
            workspace_id=workspace_id,
            user_id=user_id,
            name=name,
            raw_markdown=raw_markdown,
            fragments=SchemaFragments(global_markdown="- 한국어로 작성한다."),
            preview_markdown="# 적용될 Schema 설정",
            issues=[],
            status="draft",
        )


class FakeActivateSchemaUseCase:
    def execute(self, schema_id: str) -> WikiSchemaRecord:
        return WikiSchemaRecord(
            id=schema_id,
            workspace_id="ws-1",
            user_id="user-1",
            name="기본 schema",
            raw_markdown="raw",
            fragments=SchemaFragments(global_markdown="- 한국어로 작성한다."),
            preview_markdown="# 적용될 Schema 설정",
            issues=[],
            status="active",
        )


class FakeGetActiveSchemaUseCase:
    def execute(self, workspace_id: str, user_id: str) -> WikiSchemaRecord | None:
        return WikiSchemaRecord(
            id="schema-1",
            workspace_id=workspace_id,
            user_id=user_id,
            name="기본 schema",
            raw_markdown="raw",
            fragments=SchemaFragments(global_markdown="- 한국어로 작성한다."),
            preview_markdown="# 적용될 Schema 설정",
            issues=[],
            status="active",
        )


class SchemaStorageRoutesTest(unittest.TestCase):
    def test_create_draft_route_returns_saved_schema(self) -> None:
        response = create_wiki_schema_draft(
            payload=CreateWikiSchemaDraftRequest(
                raw_markdown="답변은 한국어로 해줘.",
                workspace_id="ws-1",
                user_id="user-1",
                name="기본 schema",
            ),
            use_case=FakeCreateSchemaDraftUseCase(),  # type: ignore[arg-type]
        )

        self.assertEqual(response.wiki_schema.id, "schema-1")
        self.assertEqual(response.wiki_schema.status, "draft")
        self.assertEqual(response.wiki_schema.workspace_id, "ws-1")
        self.assertEqual(response.wiki_schema.user_id, "user-1")

    def test_activate_route_returns_active_schema(self) -> None:
        response = activate_wiki_schema(
            schema_id="schema-1",
            use_case=FakeActivateSchemaUseCase(),  # type: ignore[arg-type]
        )

        self.assertEqual(response.id, "schema-1")
        self.assertEqual(response.status, "active")

    def test_get_active_route_returns_active_schema(self) -> None:
        response = get_active_wiki_schema(
            workspace_id="ws-1",
            user_id="user-1",
            use_case=FakeGetActiveSchemaUseCase(),  # type: ignore[arg-type]
        )

        self.assertIsNotNone(response)
        self.assertEqual(response.workspace_id, "ws-1")
        self.assertEqual(response.user_id, "user-1")
        self.assertEqual(response.status, "active")


if __name__ == "__main__":
    unittest.main()

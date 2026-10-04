import unittest

from fastapi import HTTPException

from app.modules.wiki_schema.domain.entities import SchemaFragments, WikiSchemaRecord
from app.modules.wiki_schema.interfaces.http.routes import list_wiki_schema_drafts


def _record(schema_id: str, workspace_id: str, user_id: str) -> WikiSchemaRecord:
    return WikiSchemaRecord(
        id=schema_id,
        workspace_id=workspace_id,
        user_id=user_id,
        name=schema_id,
        raw_markdown="raw",
        fragments=SchemaFragments(global_markdown="- 한국어로 작성한다."),
        preview_markdown="# 적용될 Schema 설정",
        issues=[],
        status="draft",
    )


class FakeListSchemaDraftsUseCase:
    def __init__(self, records: list[WikiSchemaRecord]) -> None:
        self._records = records

    def execute(self, workspace_id: str, user_id: str) -> list[WikiSchemaRecord]:
        return self._records


class FailingListSchemaDraftsUseCase:
    def execute(self, workspace_id: str, user_id: str) -> list[WikiSchemaRecord]:
        raise ValueError("workspace_id is required.")


class SchemaDraftListRoutesTest(unittest.TestCase):
    def test_route_returns_drafts_in_use_case_order(self) -> None:
        response = list_wiki_schema_drafts(
            workspace_id="ws-1",
            user_id="user-1",
            use_case=FakeListSchemaDraftsUseCase(  # type: ignore[arg-type]
                [_record("schema-2", "ws-1", "user-1"), _record("schema-1", "ws-1", "user-1")]
            ),
        )

        self.assertEqual([schema.id for schema in response.wiki_schemas], ["schema-2", "schema-1"])
        self.assertEqual(response.wiki_schemas[0].workspace_id, "ws-1")
        self.assertEqual(response.wiki_schemas[0].user_id, "user-1")
        self.assertEqual(response.wiki_schemas[0].status, "draft")

    def test_route_returns_empty_list(self) -> None:
        response = list_wiki_schema_drafts(
            workspace_id="ws-1",
            user_id="user-1",
            use_case=FakeListSchemaDraftsUseCase([]),  # type: ignore[arg-type]
        )

        self.assertEqual(response.wiki_schemas, [])

    def test_route_maps_value_error_to_400(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            list_wiki_schema_drafts(
                workspace_id="  ",
                user_id="user-1",
                use_case=FailingListSchemaDraftsUseCase(),  # type: ignore[arg-type]
            )

        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()

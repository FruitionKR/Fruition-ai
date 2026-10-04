import unittest

from app.modules.wiki_schema.application.create_schema_draft import CreateSchemaDraftUseCase
from app.modules.wiki_schema.application.list_schema_drafts import ListSchemaDraftsUseCase
from app.modules.wiki_schema.domain.entities import SchemaFragments, SchemaOrganizerCandidate, WikiSchemaRecord


class FakeWikiSchemaRepository:
    """list_drafts 는 workspace_id + user_id 로 범위를 좁히고 최신순으로 반환한다."""

    def __init__(self, records: list[WikiSchemaRecord]) -> None:
        self.records = list(records)

    def list_drafts(self, workspace_id: str, user_id: str) -> list[WikiSchemaRecord]:
        matched = [
            record
            for record in self.records
            if record.workspace_id == workspace_id and record.user_id == user_id and record.status == "draft"
        ]
        return list(reversed(matched))


def _record(schema_id: str, workspace_id: str = "ws-1", user_id: str = "user-1", status: str = "draft") -> WikiSchemaRecord:
    return WikiSchemaRecord(
        id=schema_id,
        workspace_id=workspace_id,
        user_id=user_id,
        name=schema_id,
        raw_markdown="raw",
        fragments=SchemaFragments(global_markdown="- 한국어로 작성한다."),
        preview_markdown="# 적용될 Schema 설정",
        issues=[],
        status=status,
    )


class ListSchemaDraftsUseCaseTest(unittest.TestCase):
    def test_returns_drafts_of_workspace_user_in_latest_first_order(self) -> None:
        repository = FakeWikiSchemaRepository([_record("schema-1"), _record("schema-2")])

        records = ListSchemaDraftsUseCase(repository).execute("ws-1", "user-1")

        self.assertEqual([record.id for record in records], ["schema-2", "schema-1"])

    def test_excludes_other_workspace_and_other_user_drafts(self) -> None:
        repository = FakeWikiSchemaRepository(
            [
                _record("mine"),
                _record("other-workspace", workspace_id="ws-2"),
                _record("other-user", user_id="user-2"),
            ]
        )

        records = ListSchemaDraftsUseCase(repository).execute("ws-1", "user-1")

        self.assertEqual([record.id for record in records], ["mine"])

    def test_returns_empty_list_when_no_draft_exists(self) -> None:
        repository = FakeWikiSchemaRepository([_record("activated", status="active")])

        self.assertEqual(ListSchemaDraftsUseCase(repository).execute("ws-1", "user-1"), [])

    def test_requires_workspace_id_and_user_id(self) -> None:
        use_case = ListSchemaDraftsUseCase(FakeWikiSchemaRepository([]))

        with self.assertRaises(ValueError):
            use_case.execute("  ", "user-1")
        with self.assertRaises(ValueError):
            use_case.execute("ws-1", "  ")

    def test_created_draft_is_reachable_through_listing(self) -> None:
        class FakeOrganizer:
            def organize(self, raw_markdown: str) -> SchemaOrganizerCandidate:
                return SchemaOrganizerCandidate(fragments=SchemaFragments(global_markdown="- 한국어로 작성한다."))

        repository = FakeWikiSchemaRepository([])
        repository.save = lambda record: (repository.records.append(record), record)[1]  # type: ignore[attr-defined]
        draft = CreateSchemaDraftUseCase(FakeOrganizer(), repository).execute(  # type: ignore[arg-type]
            raw_markdown="답변은 한국어로 해줘.",
            workspace_id="ws-1",
            user_id="user-1",
            name="기본 schema",
        )

        records = ListSchemaDraftsUseCase(repository).execute("ws-1", "user-1")

        self.assertEqual([record.id for record in records], [draft.id])


if __name__ == "__main__":
    unittest.main()

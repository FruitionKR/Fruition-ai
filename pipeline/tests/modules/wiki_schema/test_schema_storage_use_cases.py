import unittest

from app.modules.wiki_schema.application.activate_schema import ActivateSchemaUseCase
from app.modules.wiki_schema.application.create_schema_draft import CreateSchemaDraftUseCase
from app.modules.wiki_schema.application.get_active_schema import GetActiveSchemaUseCase
from app.modules.wiki_schema.domain.entities import SchemaFragments, SchemaOrganizerCandidate, WikiSchemaRecord


class FakeSchemaOrganizer:
    def organize(self, raw_markdown: str) -> SchemaOrganizerCandidate:
        return SchemaOrganizerCandidate(
            fragments=SchemaFragments(
                global_markdown="- 답변은 한국어로 작성한다.",
                concept_markdown="- 모터 종류\n- 위 항목은 문서 근거가 있을 때 concept 후보로 우선 검토한다.",
            )
        )


class FakeWikiSchemaRepository:
    def __init__(self) -> None:
        self.records: dict[str, WikiSchemaRecord] = {}

    def save(self, record: WikiSchemaRecord) -> WikiSchemaRecord:
        self.records[record.id] = record
        return record

    def get(self, schema_id: str) -> WikiSchemaRecord | None:
        return self.records.get(schema_id)

    def activate(self, schema_id: str) -> WikiSchemaRecord:
        record = self.records[schema_id]
        activated = WikiSchemaRecord(
            id=record.id,
            workspace_id=record.workspace_id,
            user_id=record.user_id,
            name=record.name,
            raw_markdown=record.raw_markdown,
            fragments=record.fragments,
            preview_markdown=record.preview_markdown,
            issues=record.issues,
            status="active",
        )
        self.records[schema_id] = activated
        return activated

    def get_active(self, workspace_id: str, user_id: str) -> WikiSchemaRecord | None:
        for record in self.records.values():
            if record.workspace_id == workspace_id and record.user_id == user_id and record.status == "active":
                return record
        return None


class SchemaStorageUseCasesTest(unittest.TestCase):
    def test_creates_draft_with_preview(self) -> None:
        repository = FakeWikiSchemaRepository()
        use_case = CreateSchemaDraftUseCase(
            organizer=FakeSchemaOrganizer(),
            repository=repository,
        )

        record = use_case.execute(
            raw_markdown="답변은 한국어로 해줘.",
            workspace_id="ws-1",
            user_id="user-1",
            name="기본 schema",
        )

        self.assertEqual(record.workspace_id, "ws-1")
        self.assertEqual(record.user_id, "user-1")
        self.assertEqual(record.name, "기본 schema")
        self.assertEqual(record.status, "draft")
        self.assertIn("적용될 Schema 설정", record.preview_markdown)
        self.assertIn(record.id, repository.records)

    def test_activates_and_reads_active_schema(self) -> None:
        repository = FakeWikiSchemaRepository()
        draft = CreateSchemaDraftUseCase(FakeSchemaOrganizer(), repository).execute(
            raw_markdown="답변은 한국어로 해줘.",
            workspace_id="ws-1",
            user_id="user-1",
            name="기본 schema",
        )

        activated = ActivateSchemaUseCase(repository).execute(draft.id)
        active = GetActiveSchemaUseCase(repository).execute("ws-1", "user-1")

        self.assertEqual(activated.status, "active")
        self.assertEqual(active, activated)


if __name__ == "__main__":
    unittest.main()

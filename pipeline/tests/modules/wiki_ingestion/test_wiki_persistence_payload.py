import unittest
from pathlib import Path

from app.modules.wiki_ingestion.infrastructure.wiki_persistence_payload import (
    markdown_title,
    page_payload,
    resolve_page_id,
    source_summary,
    stored_manifest,
)
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as repository


class WikiPersistencePayloadTest(unittest.TestCase):
    def test_builds_page_payload_from_dict_or_path(self) -> None:
        self.assertEqual(
            page_payload({"slug": "source-a", "markdown": "# Source A\n"}),
            {"slug": "source-a", "markdown": "# Source A\n"},
        )

        path = Path(self._tmp_dir) / "concept-a.md"
        path.write_text("# Concept A\n\n본문", encoding="utf-8")

        payload = page_payload(path)

        self.assertEqual(payload["slug"], "concept-a")
        self.assertEqual(payload["title"], "Concept A")
        self.assertEqual(payload["markdown"], "# Concept A\n\n본문")

    def test_stored_manifest_removes_large_runtime_payloads(self) -> None:
        manifest = {
            "source_page": {"slug": "source-a", "markdown": "# Source", "source_extraction_artifact": {}},
            "source_extraction_artifact": {
                "summary": "요약",
                "max_block_number": 12,
            },
            "concept_pages": [{"slug": "concept-a", "markdown": "# Concept"}],
            "concept_contributions": {
                "concept-a": {
                    "operation_id": "op-1",
                    "evidence_units": [{"evidence_id": "ev-1"}],
                }
            },
            "normalized": {"large": True},
            "source_blocks": [{"block_id": "B0001"}],
            "source_block_changes": {
                "added_block_ids": ["B0002"],
                "invalidated_block_ids": [],
            },
            "generation_evaluations": [{"passed": False, "issues": [{"type": "missing_ref"}]}],
            "generation_evaluation_status": "unresolved",
            "source_contribution": {
                "concept_slugs": ["concept-a"],
                "claim_signatures": [["cluster-a", "claim-a", "주장"]],
            },
            "meaning_clusters": {
                "active_markdown": "# Active",
                "log_markdown": "# Log",
                "clusters": [{"id": "a"}],
                "active_uri": "s3://active",
            },
        }

        stored = stored_manifest(manifest)

        self.assertNotIn("normalized", stored)
        self.assertNotIn("source_blocks", stored)
        self.assertNotIn("concept_contributions", stored)
        self.assertEqual(
            stored["source_extraction_artifact"]["max_block_number"],
            12,
        )
        self.assertEqual(
            stored["source_block_changes"],
            manifest["source_block_changes"],
        )
        self.assertEqual(stored["source_page"], {"slug": "source-a"})
        self.assertEqual(stored["concept_pages"], [{"slug": "concept-a"}])
        self.assertEqual(stored["meaning_clusters"], {"active_uri": "s3://active"})
        self.assertEqual(stored["generation_evaluations"], manifest["generation_evaluations"])
        self.assertEqual(stored["generation_evaluation_status"], "unresolved")
        self.assertEqual(
            stored["source_contribution"],
            manifest["source_contribution"],
        )

    def test_resolves_summary_title_and_link_page_ids(self) -> None:
        self.assertEqual(
            source_summary({"semantic_notes": [{"semantic_summary": "첫 요약"}], "document": {"title": "문서 제목"}}),
            "첫 요약",
        )
        self.assertEqual(
            source_summary(
                {"semantic_notes": [{"semantic_summary": "첫 요약"}], "document": {"title": "문서 제목"}},
                {"source_extraction_artifact": {"summary": "평가 후 전체 요약"}},
            ),
            "평가 후 전체 요약",
        )
        self.assertEqual(markdown_title("본문\n# 제목\n내용"), "제목")
        self.assertEqual(resolve_page_id("source:doc", "source-id", {}), "source-id")
        self.assertEqual(resolve_page_id("concept:test", "source-id", {"test": "concept-id"}), "concept-id")
        self.assertIsNone(resolve_page_id("concept:missing", "source-id", {}))

    def test_latest_source_page_context_requires_source_page_row(self) -> None:
        class FakeConn:
            def execute(self, _query: str, _params: tuple[str, str, str]) -> "FakeConn":
                return self

            def fetchone(self) -> None:
                return None

            def __enter__(self) -> "FakeConn":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        original_artifact = repository.latest_source_extraction_artifact
        original_connect = repository.connect
        try:
            repository.latest_source_extraction_artifact = lambda _document_id: {"document_id": "chat-doc-1"}  # type: ignore[assignment]
            repository.connect = lambda: FakeConn()  # type: ignore[assignment]

            context = repository.latest_source_page_context("chat-doc-1", "user-1", "workspace-1")
        finally:
            repository.latest_source_extraction_artifact = original_artifact  # type: ignore[assignment]
            repository.connect = original_connect  # type: ignore[assignment]

        self.assertIsNone(context)

    def test_latest_source_page_context_slugifies_document_id(self) -> None:
        document_id = "chatdoc_1ce10f8ddc0a4c778c2637b32d13061b"
        params: tuple[str, str, str] | None = None

        class FakeConn:
            def execute(self, _query: str, query_params: tuple[str, str, str]) -> "FakeConn":
                nonlocal params
                params = query_params
                return self

            def fetchone(self) -> dict[str, str]:
                return {"markdown_uri": "s3://source.md"}

            def __enter__(self) -> "FakeConn":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        original_artifact = repository.latest_source_extraction_artifact
        original_connect = repository.connect
        original_read = repository._read_optional_text_object
        try:
            repository.latest_source_extraction_artifact = lambda _document_id: {"document_id": document_id}  # type: ignore[assignment]
            repository.connect = lambda: FakeConn()  # type: ignore[assignment]
            repository._read_optional_text_object = lambda _uri: "# Source"  # type: ignore[assignment]

            context = repository.latest_source_page_context(document_id, "user-1", "workspace-1")
        finally:
            repository.latest_source_extraction_artifact = original_artifact  # type: ignore[assignment]
            repository.connect = original_connect  # type: ignore[assignment]
            repository._read_optional_text_object = original_read  # type: ignore[assignment]

        self.assertEqual(params, ("chatdoc-1ce10f8ddc0a4c778c2637b32d13061b", "user-1", "workspace-1"))
        self.assertEqual(context, {"artifact": {"document_id": document_id}, "source_markdown": "# Source"})

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_dir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()


if __name__ == "__main__":
    unittest.main()

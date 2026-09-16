import os
import unittest
from unittest.mock import MagicMock, patch

import pytest
from minio.error import S3Error

from app.modules.query.application.evidence_selector import EvidenceSelector
from app.modules.query.domain.entities import (
    RetrievedPage,
    WikiEmbeddingUnit,
    WikiPage,
)
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_writer
from app.modules.wiki_ingestion.infrastructure.postgres_wiki_writer import persist_embedding_units


class RecordingCursor:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict]:
        return self._rows

    def fetchone(self) -> dict:
        return self._rows[0]


class RecordingConnection:
    def __init__(self, existing_vector_id: str | None = None) -> None:
        self.batches: list[list[tuple]] = []
        self.vector_rows: list[tuple] = []
        self._current_batch: list[tuple] | None = None
        self._existing_vector_id = existing_vector_id

    def execute(self, query: str, params: tuple) -> RecordingCursor:
        if "SELECT DISTINCT embedding_vector_id" in query:
            return RecordingCursor([])
        if "DELETE FROM wiki_embedding_units" in query:
            self._current_batch = []
            self.batches.append(self._current_batch)
        elif "INSERT INTO wiki_embedding_units" in query:
            assert self._current_batch is not None
            self._current_batch.append(params)
        elif "INSERT INTO wiki_embedding_vectors" in query:
            self.vector_rows.append(params)
            return RecordingCursor([{"id": self._existing_vector_id or params[0]}])
        return RecordingCursor([])


class ContainsSearch:
    def score(self, query: str, documents: list[str]) -> list[float]:
        return [1.0 if query.lower() in document.lower() else 0.0 for document in documents]


def _units(rows: list[tuple]) -> list[WikiEmbeddingUnit]:
    return [
        WikiEmbeddingUnit(
            id=row[0],
            page_id=row[2],
            source_document_id=row[3],
            unit_type=row[4],
            source_block_ids=row[5],
            text=row[6],
            weight=row[7],
        )
        for row in rows
    ]


class PersistEmbeddingUnitsTest(unittest.TestCase):
    def test_raw_marker_is_returned_as_source_page_evidence(self) -> None:
        conn = RecordingConnection()
        persist_embedding_units(
            conn,
            "page-1",
            "doc-1",
            "## Key Points\n- semantic claim [B0001]",
            [{"block_id": "B0001", "text": "UNIQUE_RAW_MARKER"}],
        )

        evidence = EvidenceSelector(
            embedding_search=ContainsSearch(),
            text_search=ContainsSearch(),
        ).select(
            "UNIQUE_RAW_MARKER",
            [
                RetrievedPage(
                    page=WikiPage(
                        id="page-1",
                        page_type="source",
                        title="Source",
                        slug="source",
                        summary="Summary",
                    ),
                    score=1.0,
                    role="seed_source",
                )
            ],
            {"page-1": _units(conn.batches[0])},
        )

        self.assertEqual(evidence[0].text, "UNIQUE_RAW_MARKER")
        self.assertEqual(evidence[0].source_document_id, "doc-1")
        self.assertEqual(evidence[0].source_block_ids, ["B0001"])

    def test_reingest_replaces_old_raw_block_unit(self) -> None:
        conn = RecordingConnection()
        persist_embedding_units(
            conn,
            "page-1",
            "doc-1",
            "# Source",
            [{"block_id": "B0001", "text": "OLD_MARKER"}],
        )
        persist_embedding_units(
            conn,
            "page-1",
            "doc-1",
            "# Source",
            [{"block_id": "B0001", "text": "NEW_MARKER"}],
        )

        self.assertEqual([row[6] for row in conn.batches[0]], ["OLD_MARKER"])
        self.assertEqual([row[6] for row in conn.batches[1]], ["NEW_MARKER"])

    def test_raw_units_do_not_replace_semantic_units(self) -> None:
        conn = RecordingConnection()
        with patch.dict(os.environ, {"EMBEDDING_MODEL_NAME": ""}):
            persist_embedding_units(
                conn,
                "page-1",
                "doc-1",
                "## Key Points\n- semantic claim [B0001]",
                [{"block_id": "B0001", "text": "raw source text"}],
            )

        self.assertEqual(
            {unit.unit_type for unit in _units(conn.batches[0])},
            {"key_point", "source_block"},
        )
        self.assertEqual(
            {(row[1], row[3]) for row in conn.vector_rows},
            {
                ("BAAI/bge-m3", "semantic claim"),
                ("BAAI/bge-m3", "raw source text"),
            },
        )

    def test_vector_id_changes_with_embedding_model(self) -> None:
        first = RecordingConnection()
        second = RecordingConnection()

        with patch.dict(os.environ, {"EMBEDDING_MODEL_NAME": "model-a"}):
            persist_embedding_units(
                first,
                "page-1",
                "doc-1",
                "",
                [{"block_id": "B0001", "text": "same text"}],
            )
        with patch.dict(os.environ, {"EMBEDDING_MODEL_NAME": "model-b"}):
            persist_embedding_units(
                second,
                "page-1",
                "doc-1",
                "",
                [{"block_id": "B0001", "text": "same text"}],
            )

        self.assertNotEqual(first.vector_rows[0][0], second.vector_rows[0][0])

    def test_existing_vector_id_is_used_for_unit_reference(self) -> None:
        conn = RecordingConnection(existing_vector_id="embedding:existing")

        persist_embedding_units(
            conn,
            "page-1",
            "doc-1",
            "",
            [{"block_id": "B0001", "text": "same text"}],
        )

        self.assertEqual(conn.batches[0][0][1], "embedding:existing")


def test_optional_text_read_only_absorbs_missing_object() -> None:
    missing = S3Error(
        MagicMock(),
        "NoSuchKey",
        "missing",
        "wiki/missing.md",
        "request-1",
        "host-1",
    )

    with patch.object(postgres_wiki_writer, "read_text_object", side_effect=missing):
        assert postgres_wiki_writer.read_optional_text_object("wiki/missing.md") == ""
    with (
        patch.object(
            postgres_wiki_writer,
            "read_text_object",
            side_effect=RuntimeError("storage unavailable"),
        ),
        pytest.raises(RuntimeError, match="storage unavailable"),
    ):
        postgres_wiki_writer.read_optional_text_object("wiki/active.md")


if __name__ == "__main__":
    unittest.main()

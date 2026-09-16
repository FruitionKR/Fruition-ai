import unittest

from app.modules.query.application.source_references import (
    has_global_source_refs,
    is_block_ref_only,
    legacy_source_fields,
    remove_block_refs,
    source_block_ids,
    source_references,
    source_references_from_ids,
)


class SourceReferencesTest(unittest.TestCase):
    def test_parses_local_and_global_source_refs(self) -> None:
        refs = source_references("근거 [B0001, doc_b:B0002, B0001]", "doc_a")

        self.assertEqual(
            [(ref.source_document_id, ref.source_block_id) for ref in refs],
            [("doc_a", "B0001"), ("doc_b", "B0002")],
        )
        self.assertEqual(source_block_ids("근거 [B0001, doc_b:B0002, B0001]"), ["B0001", "B0002"])

    def test_ignores_invalid_refs_and_legacy_fields_use_first_document(self) -> None:
        refs = source_references_from_ids(["web", "doc_a:B0001", "doc_b:B0002", "bad"], "doc_default")

        self.assertEqual(
            [(ref.source_document_id, ref.source_block_id) for ref in refs],
            [("doc_a", "B0001"), ("doc_b", "B0002")],
        )
        self.assertEqual(legacy_source_fields(refs, "doc_default"), ("doc_a", ["B0001"]))

    def test_detects_and_removes_block_refs(self) -> None:
        self.assertTrue(has_global_source_refs("근거 [doc_a:B0001]"))
        self.assertTrue(is_block_ref_only("[B0001] [doc_b:B0002]"))
        self.assertEqual(remove_block_refs("근거 문장입니다. [doc_a:B0001, B0002]"), "근거 문장입니다.")


if __name__ == "__main__":
    unittest.main()

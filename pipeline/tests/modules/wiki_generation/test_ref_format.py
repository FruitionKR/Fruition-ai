import unittest

from app.modules.wiki_generation.infrastructure.ref_format import cite_global_refs, cite_refs, display_ref, global_refs, ref_label


class RefFormatTest(unittest.TestCase):
    def test_ref_label_shortens_backend_reference_id(self) -> None:
        self.assertEqual(ref_label("ref_doc_md_b0007"), "B0007")
        self.assertEqual(ref_label("B0008"), "B0008")

    def test_cite_refs_globalizes_local_block_ids(self) -> None:
        self.assertEqual(cite_refs(["B0001", "B0001", "doc_b:B0002"], "doc_a"), " [doc_a:B0001, doc_b:B0002]")

    def test_display_ref_shortens_global_backend_reference_id(self) -> None:
        self.assertEqual(display_ref("doc_a:ref_doc_md_b0003"), "doc_a:B0003")

    def test_global_refs_and_global_citations_keep_order(self) -> None:
        self.assertEqual(global_refs("doc_a", ["B0001", "doc_b:B0002", "B0001"]), ["doc_a:B0001", "doc_b:B0002"])
        self.assertEqual(cite_global_refs(["doc_a:B0001", "doc_a:B0001"]), " [doc_a:B0001]")


if __name__ == "__main__":
    unittest.main()

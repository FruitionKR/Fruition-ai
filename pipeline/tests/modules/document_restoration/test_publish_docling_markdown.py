import tempfile
import unittest
from pathlib import Path

from app.modules.document_restoration.infrastructure.publish_docling_markdown import (
    publish_docling_markdown,
)


class PublishDoclingMarkdownTest(unittest.TestCase):
    def test_copies_docling_markdown_to_final_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_file = root / "docling.md"
            output_file = root / "final" / "paper.restored.md"
            input_file.write_text("# Docling", encoding="utf-8")

            publish_docling_markdown(input_file, output_file)

            self.assertEqual(output_file.read_text(encoding="utf-8"), "# Docling")

    def test_requires_markdown_when_only_cached_json_was_supplied(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            with self.assertRaisesRegex(
                FileNotFoundError,
                "--docling-markdown",
            ):
                publish_docling_markdown(
                    root / "missing.md",
                    root / "final" / "paper.restored.md",
                )


if __name__ == "__main__":
    unittest.main()

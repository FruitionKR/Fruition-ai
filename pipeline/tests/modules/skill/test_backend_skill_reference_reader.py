import unittest
from unittest.mock import MagicMock, patch

from app.modules.skill.infrastructure.backend_skill_reference_reader import (
    BackendSkillReferenceReader,
    build_skill_reference_reader,
)


class BackendSkillReferenceReaderTest(unittest.TestCase):
    def test_uses_document_service_boundary(self) -> None:
        response = MagicMock()
        response.read.return_value = b'{"document_role":"EDITABLE","markdown":"# current"}'
        response.__enter__.return_value = response

        with patch.dict(
            "os.environ",
            {
                "DOCUMENT_INTERNAL_BASE_URL": "http://document-svc:8080",
                "AGENT_BACKEND_URL": "http://wrong-boundary:8080",
                "AGENT_INTERNAL_TOKEN": "service-token",
            },
            clear=True,
        ), patch(
            "app.modules.skill.infrastructure.backend_skill_reference_reader.urlopen",
            return_value=response,
        ) as urlopen_mock:
            build_skill_reference_reader().read(
                workspace_id="workspace-1",
                user_id="user-1",
                document_id="document-1",
            )

        self.assertEqual(
            urlopen_mock.call_args.args[0].full_url,
            "http://document-svc:8080/internal/agent/skill-authoring/references/read",
        )

    def test_original_uses_ai_db_source_blocks(self) -> None:
        response = MagicMock()
        response.read.return_value = b'{"document_role":"ORIGINAL"}'
        response.__enter__.return_value = response
        reader = BackendSkillReferenceReader("http://document-svc:8080", "service-token")

        with patch(
            "app.modules.skill.infrastructure.backend_skill_reference_reader.urlopen",
            return_value=response,
        ), patch(
            "app.modules.skill.infrastructure.backend_skill_reference_reader.list_source_blocks",
            return_value=[{"text": "첫 번째 블록"}, {"text": "두 번째 블록"}],
        ) as source_reader:
            reference = reader.read(
                workspace_id="workspace-1",
                user_id="user-1",
                document_id="document-1",
            )

        source_reader.assert_called_once_with("document-1")
        self.assertEqual(reference.markdown, "첫 번째 블록\n\n두 번째 블록")

    def test_original_without_source_blocks_is_inaccessible(self) -> None:
        response = MagicMock()
        response.read.return_value = b'{"document_role":"ORIGINAL"}'
        response.__enter__.return_value = response
        reader = BackendSkillReferenceReader("http://document-svc:8080", "service-token")

        with patch(
            "app.modules.skill.infrastructure.backend_skill_reference_reader.urlopen",
            return_value=response,
        ), patch(
            "app.modules.skill.infrastructure.backend_skill_reference_reader.list_source_blocks",
            return_value=[],
        ), self.assertRaisesRegex(ValueError, "not accessible"):
            reader.read(
                workspace_id="workspace-1",
                user_id="user-1",
                document_id="document-1",
            )


if __name__ == "__main__":
    unittest.main()

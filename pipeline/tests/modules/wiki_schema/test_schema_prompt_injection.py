import unittest

from app.modules.markdown_edit.domain.entities import MarkdownEditRequest, MarkdownEditTarget
from app.modules.markdown_edit.infrastructure.chat_completions_markdown_editor import ChatCompletionsMarkdownEditor
from app.modules.query.domain.entities import GraphContext, QueryContext
from app.modules.query.infrastructure.query_chat_answer_generator import QueryChatAnswerGenerator
from app.modules.wiki_generation.domain.entities import SemanticPacket, SourceBlock
from app.modules.wiki_generation.infrastructure.chat_completions_llm import (
    GenericChatCompletionsConceptPageGenerator,
    GenericChatCompletionsExtractor,
    GenericChatCompletionsSectionPolisher,
)


class FakeTextClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def complete_text(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        return "답변"


class FakeJsonClient:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict:
        self.calls.append((system_prompt, user_prompt))
        return self.response

    def complete_text(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        return '{"section": "summary", "title": "", "text": "정리", "anchor_block_ids": [], "items": []}'


def schema_prompt(
    feature: str,
    workspace_id: str | None = None,
    user_id: str | None = None,
) -> str:
    return f"<workspace_schema>\n## {feature}\n- active schema\n</workspace_schema>"


def scoped_schema_prompt(
    feature: str,
    workspace_id: str | None,
    user_id: str | None,
) -> str:
    if not workspace_id or not user_id:
        return ""
    return schema_prompt(feature)


class SchemaPromptInjectionTest(unittest.TestCase):
    def test_query_generator_injects_query_schema_prompt(self) -> None:
        client = FakeTextClient()
        generator = QueryChatAnswerGenerator(
            client,
            schema_prompt_provider=scoped_schema_prompt,
        )  # type: ignore[arg-type]

        generator.generate_answer(
            QueryContext(
                question="질문",
                graph_context=GraphContext(),
                traversal_paths=[],
                related_pages=[],
                evidence_snippets=[],
                answer_context="# User Question\n질문",
                workspace_id="workspace-1",
                user_id="user-1",
            )
        )

        self.assertIn("## query", client.calls[0][0])

    def test_query_generator_does_not_inject_schema_without_scope(self) -> None:
        client = FakeTextClient()
        generator = QueryChatAnswerGenerator(
            client,
            schema_prompt_provider=scoped_schema_prompt,
        )  # type: ignore[arg-type]

        generator.generate_answer(
            QueryContext(
                question="질문",
                graph_context=GraphContext(),
                traversal_paths=[],
                related_pages=[],
                evidence_snippets=[],
                answer_context="# User Question\n질문",
            )
        )

        self.assertNotIn("<workspace_schema>", client.calls[0][0])

    def test_markdown_editor_injects_edit_schema_prompt(self) -> None:
        client = FakeJsonClient(
            {
                "operation": "replace",
                "actual_target": {
                    "type": "selection",
                    "start_line": 1,
                    "end_line": 1,
                },
                "summary": "수정",
                "replacement_markdown": "결과",
            }
        )
        editor = ChatCompletionsMarkdownEditor(
            client=client,  # type: ignore[arg-type]
            system_prompt="edit system",
            schema_prompt_provider=scoped_schema_prompt,
        )

        editor.generate_edit(
            MarkdownEditRequest(
                instruction="줄여줘",
                markdown="원문",
                target=MarkdownEditTarget(type="selection", start_line=1, end_line=1),
                workspace_id="workspace-1",
                user_id="user-1",
            )
        )

        self.assertIn("## edit", client.calls[0][0])

    def test_markdown_editor_does_not_inject_schema_without_scope(self) -> None:
        client = FakeJsonClient(
            {
                "operation": "replace",
                "actual_target": {
                    "type": "selection",
                    "start_line": 1,
                    "end_line": 1,
                },
                "summary": "수정",
                "replacement_markdown": "결과",
            }
        )
        editor = ChatCompletionsMarkdownEditor(
            client=client,  # type: ignore[arg-type]
            system_prompt="edit system",
            schema_prompt_provider=scoped_schema_prompt,
        )

        editor.generate_edit(
            MarkdownEditRequest(
                instruction="줄여줘",
                markdown="원문",
                target=MarkdownEditTarget(type="selection", start_line=1, end_line=1),
            )
        )

        self.assertNotIn("<workspace_schema>", client.calls[0][0])

    def test_ingest_extractor_injects_ingest_schema_prompt(self) -> None:
        client = FakeJsonClient({})
        extractor = GenericChatCompletionsExtractor(
            client=client,  # type: ignore[arg-type]
            system_prompt="ingest system",
            schema_prompt_provider=schema_prompt,
        )

        extractor.extract(SemanticPacket(chunk_id="chunk-1", document_id="doc-1", block_ids=["B0001"], text="[B0001] text"))

        self.assertIn("## ingest", client.calls[0][0])

    def test_concept_generator_injects_concept_schema_prompt(self) -> None:
        client = FakeJsonClient({})
        generator = GenericChatCompletionsConceptPageGenerator(
            client=client,  # type: ignore[arg-type]
            system_prompt="concept system",
            schema_prompt_provider=schema_prompt,
        )

        generator.generate(
            concept={"slug": "motor", "title": "Motor"},
            evidence_units=[],
            source_blocks=[
                SourceBlock(
                    document_id="doc-1",
                    block_id="B0001",
                    source_reference_id="ref-1",
                    text="motor",
                    line_start=1,
                    line_end=1,
                )
            ],
        )

        self.assertIn("## concept", client.calls[0][0])

    def test_section_polisher_injects_edit_schema_prompt(self) -> None:
        client = FakeJsonClient({})
        polisher = GenericChatCompletionsSectionPolisher(
            client=client,  # type: ignore[arg-type]
            system_prompt="polish system",
            schema_prompt_provider=schema_prompt,
        )

        polisher.polish(
            payload={"section": "summary", "page_type": "source", "draft": {}, "context": {}, "evidence": []},
            source_blocks=[],
        )

        self.assertIn("## edit", client.calls[0][0])


if __name__ == "__main__":
    unittest.main()

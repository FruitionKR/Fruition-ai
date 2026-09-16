from app.modules.wiki_embedding.application.ports import MarkdownReaderPort
from app.modules.wiki_ingestion.infrastructure.object_storage import read_text_object


class MinioMarkdownReader(MarkdownReaderPort):
    def read_markdown(self, markdown_uri: str) -> str:
        return read_text_object(markdown_uri)

